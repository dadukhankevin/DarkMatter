#!/usr/bin/env python3
"""
DarkMatter Economy & Trust Test Suite.

Tests the antimatter economy (match game, elder routing, antimatter signals), wallet
derivation, trust/impression adjustments, and Solana devnet operations.

Three tiers:
  - Tier 1: Pure in-memory (no network, no blockchain)
  - Tier 2: ASGI integration (in-process HTTP, mocked Solana)
  - Tier 3: Solana devnet (real blockchain, opt-in via RUN_DEVNET_TESTS=1)

Standalone async script — same conventions as test_identity.py and test_network.py.

Usage:
    python3 test_economy.py                        # Tiers 1+2 (~5s)
    RUN_DEVNET_TESTS=1 python3 test_economy.py     # All tiers (~60s with devnet)
"""

import asyncio
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
import uuid
import random as _random
from datetime import datetime, timezone, timedelta

# Force devnet RPC before importing darkmatter
os.environ["DARKMATTER_SOLANA_RPC"] = "https://api.devnet.solana.com"

import httpx
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# darkmatter/ package imports
# ---------------------------------------------------------------------------

from darkmatter.state import get_state, set_state, save_state
from darkmatter.models import AgentState, AgentStatus, Connection, Impression, AntiMatterSignal, QueuedMessage
from darkmatter.identity import generate_keypair
from darkmatter.app import create_app
from darkmatter.config import SOLANA_AVAILABLE, LAMPORTS_PER_SOL
from darkmatter.wallet.solana import (
    _resolve_spl_token,
    _get_solana_wallet_address,
    get_solana_balance,
    send_solana_sol,
    send_solana_token,
)
from darkmatter.wallet.antimatter import (
    adjust_trust,
    select_elder,
    antimatter_signal_to_dict,
    antimatter_signal_from_dict,
    log_antimatter_event,
    verify_commitment,
    make_commitment,
    run_antimatter_match,
    resolve_antimatter,
    initiate_antimatter_from_payment,
    antimatter_timeout_watchdog,
    set_network_fns,
)
from darkmatter.network.mesh import _gather_peer_trust
from darkmatter.network.transport import SendResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"

results: list[tuple[str, bool, str]] = []


def report(name: str, passed: bool, detail: str = "") -> None:
    mark = f"{GREEN}✓{RESET}" if passed else f"{RED}✗{RESET}"
    print(f"  {mark} {name}")
    if detail and not passed:
        print(f"      {detail}")
    results.append((name, passed, detail))


def make_state_file() -> str:
    """Return a path to a fresh temp state file (caller cleans up)."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="dm_test_")
    os.close(fd)
    os.unlink(path)  # start with no file
    return path


def create_agent(state_path: str, *, port: int = 9900) -> tuple:
    """Create a fresh DarkMatter app + state, isolated from other agents.

    Returns (app, state) tuple.
    """
    set_state(None)

    os.environ["DARKMATTER_PORT"] = str(port)
    os.environ["DARKMATTER_DISCOVERY"] = "false"
    os.environ.pop("DARKMATTER_AGENT_ID", None)
    os.environ.pop("DARKMATTER_MCP_TOKEN", None)
    os.environ.pop("DARKMATTER_GENESIS", None)

    priv, pub = generate_keypair()
    set_state(AgentState(
        agent_id=pub,
        bio="A DarkMatter mesh agent.",
        status=AgentStatus.ACTIVE,
        port=port,
        private_key_hex=priv,
        public_key_hex=pub,
    ))
    app = create_app()
    state = get_state()
    return app, state


def use_agent(state):
    """Set the global state to this agent's state before making requests."""
    set_state(state)


async def connect_agents(app_a, state_a, app_b, state_b) -> None:
    """Connect agent B to agent A via accept_pending + connection_accepted."""
    use_agent(state_a)
    async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
        resp = await client.post("/__darkmatter__/connection_request", json={
            "from_agent_id": state_b.agent_id,
            "from_agent_url": f"http://localhost:{state_b.port}/mcp",
            "from_agent_bio": f"Agent {state_b.agent_id[:8]}",
            "from_agent_public_key_hex": state_b.public_key_hex,
        })
    data = resp.json()
    request_id = data.get("request_id")

    async with httpx.AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
        await client.post("/__darkmatter__/accept_pending", json={"request_id": request_id})

    use_agent(state_b)
    state_b.pending_outbound[f"http://localhost:{state_a.port}/mcp"] = state_a.agent_id
    async with httpx.AsyncClient(transport=ASGITransport(app=app_b), base_url="http://test") as client:
        await client.post("/__darkmatter__/connection_accepted", json={
            "agent_id": state_a.agent_id,
            "agent_url": f"http://localhost:{state_a.port}/mcp",
            "agent_bio": state_a.bio,
            "agent_public_key_hex": state_a.public_key_hex,
        })


# ---------------------------------------------------------------------------
# Tier 1: Pure In-Memory Tests
# ---------------------------------------------------------------------------


async def test_adjust_trust_increment() -> None:
    """adjust_trust increments score from 0."""
    path = make_state_file()
    try:
        _, state = create_agent(path)
        adjust_trust(state, "peer-1", 0.05)
        imp = state.impressions.get("peer-1")
        report("score increases from 0", imp is not None and imp.score == 0.05,
               f"got: {imp}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_adjust_trust_preserves_note() -> None:
    """adjust_trust preserves existing note field."""
    path = make_state_file()
    try:
        _, state = create_agent(path)
        state.impressions["peer-1"] = Impression(score=0.5, note="good peer")
        adjust_trust(state, "peer-1", 0.1)
        imp = state.impressions["peer-1"]
        report("note preserved", imp.note == "good peer", f"got: {imp.note!r}")
        report("score updated", imp.score == 0.6, f"got: {imp.score}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_adjust_trust_clamps_upper() -> None:
    """adjust_trust clamps to 1.0."""
    path = make_state_file()
    try:
        _, state = create_agent(path)
        state.impressions["peer-1"] = Impression(score=0.95)
        adjust_trust(state, "peer-1", 0.1)
        report("clamped to 1.0", state.impressions["peer-1"].score == 1.0,
               f"got: {state.impressions['peer-1'].score}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_adjust_trust_clamps_lower() -> None:
    """adjust_trust clamps to -1.0."""
    path = make_state_file()
    try:
        _, state = create_agent(path)
        state.impressions["peer-1"] = Impression(score=-0.95)
        adjust_trust(state, "peer-1", -0.1)
        report("clamped to -1.0", state.impressions["peer-1"].score == -1.0,
               f"got: {state.impressions['peer-1'].score}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_adjust_trust_rounding() -> None:
    """adjust_trust rounds to 4 decimal places."""
    path = make_state_file()
    try:
        _, state = create_agent(path)
        adjust_trust(state, "peer-1", 0.00001)
        adjust_trust(state, "peer-1", 0.00002)
        score = state.impressions["peer-1"].score
        # 0.00001 + 0.00002 = 0.00003, rounded to 4 dp = 0.0
        report("4 decimal places", len(str(score).split(".")[-1]) <= 4,
               f"got: {score}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_wallet_derivation_determinism() -> None:
    """Same key produces same wallet address twice."""
    if not SOLANA_AVAILABLE:
        report("wallet derivation determinism (skipped — solana not installed)", True)
        return

    priv, _ = generate_keypair()
    addr1 = _get_solana_wallet_address(priv)
    addr2 = _get_solana_wallet_address(priv)
    report("deterministic derivation", addr1 == addr2, f"{addr1} vs {addr2}")


async def test_wallet_derivation_different_keys() -> None:
    """Different keys produce different wallets."""
    if not SOLANA_AVAILABLE:
        report("different keys -> different wallets (skipped)", True)
        return

    priv1, _ = generate_keypair()
    priv2, _ = generate_keypair()
    addr1 = _get_solana_wallet_address(priv1)
    addr2 = _get_solana_wallet_address(priv2)
    report("different keys -> different wallets", addr1 != addr2,
           f"both: {addr1}")


async def test_wallet_address_valid_base58() -> None:
    """Wallet address is valid base58 format."""
    if not SOLANA_AVAILABLE:
        report("valid base58 address (skipped)", True)
        return

    priv, _ = generate_keypair()
    addr = _get_solana_wallet_address(priv)
    valid_chars = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    is_valid = 32 <= len(addr) <= 44 and all(c in valid_chars for c in addr)
    report("valid base58 address", is_valid, f"got: {addr!r} (len={len(addr)})")


async def test_resolve_spl_token_known() -> None:
    """_resolve_spl_token resolves DM, USDC, USDT; unknown returns None."""
    dm = _resolve_spl_token("DM")
    usdc = _resolve_spl_token("usdc")
    usdt = _resolve_spl_token("USDT")
    unknown = _resolve_spl_token("NOPE")

    report("DM resolves", dm is not None and dm[0] == "5DxioZwEeAKpBaYC5veTHArKE55qRDSmb5RZ6VwApump",
           f"got: {dm}")
    report("USDC resolves (case insensitive)", usdc is not None and usdc[1] == 6,
           f"got: {usdc}")
    report("USDT resolves", usdt is not None, f"got: {usdt}")
    report("unknown returns None", unknown is None, f"got: {unknown}")


async def test_antimatter_signal_round_trip() -> None:
    """antimatter_signal_to_dict -> antimatter_signal_from_dict preserves all fields."""
    sig = AntiMatterSignal(
        signal_id="am-test123",
        original_tx="tx-abc",
        sender_agent_id="sender-1",
        amount=0.005,
        token="SOL",
        token_decimals=9,
        sender_superagent_wallet="wallet-xyz",
        callback_url="http://localhost:9900/__darkmatter__/antimatter_result",
        hops=3,
        max_hops=10,
        created_at="2026-01-01T00:00:00+00:00",
        path=["a", "b", "c"],
    )

    d = antimatter_signal_to_dict(sig)
    restored = antimatter_signal_from_dict(d)

    checks = [
        ("signal_id", sig.signal_id, restored.signal_id),
        ("original_tx", sig.original_tx, restored.original_tx),
        ("sender_agent_id", sig.sender_agent_id, restored.sender_agent_id),
        ("amount", sig.amount, restored.amount),
        ("token", sig.token, restored.token),
        ("token_decimals", sig.token_decimals, restored.token_decimals),
        ("sender_superagent_wallet", sig.sender_superagent_wallet, restored.sender_superagent_wallet),
        ("callback_url", sig.callback_url, restored.callback_url),
        ("hops", sig.hops, restored.hops),
        ("max_hops", sig.max_hops, restored.max_hops),
        ("created_at", sig.created_at, restored.created_at),
        ("path", sig.path, restored.path),
    ]

    all_ok = all(expected == actual for _, expected, actual in checks)
    failures = [(name, expected, actual) for name, expected, actual in checks if expected != actual]
    report("round-trip preserves all fields", all_ok,
           f"mismatches: {failures}" if failures else "")


async def test_elder_selection_picks_qualified() -> None:
    """select_elder picks older peer with positive trust and wallet."""
    path = make_state_file()
    try:
        _, state = create_agent(path)
        # Set state created_at to "now"
        now = datetime.now(timezone.utc)
        state.created_at = now.isoformat()

        # Add a qualified elder: older, positive trust, has wallet
        elder_id = "elder-" + uuid.uuid4().hex[:8]
        state.connections[elder_id] = Connection(
            agent_id=elder_id,
            agent_url="http://localhost:9901/mcp",
            agent_bio="Elder agent",
            wallets={"solana": "FakeWallet1111111111111111111111111111111111"},
            peer_created_at=(now - timedelta(days=30)).isoformat(),
        )
        state.impressions[elder_id] = Impression(score=0.5)

        sig = AntiMatterSignal(
            signal_id="am-test",
            original_tx="tx-1",
            sender_agent_id="sender-1",
            amount=0.01,
            token="SOL",
            token_decimals=9,
            sender_superagent_wallet="",
            callback_url="http://localhost:9900/__darkmatter__/antimatter_result",
            path=[],
        )

        selected = select_elder(state, sig)
        report("elder selected", selected is not None and selected.agent_id == elder_id,
               f"got: {selected.agent_id if selected else None}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_elder_excludes_agents_in_path() -> None:
    """select_elder excludes agents already in sig.path (loop prevention)."""
    path = make_state_file()
    try:
        _, state = create_agent(path)
        now = datetime.now(timezone.utc)
        state.created_at = now.isoformat()

        elder_id = "elder-" + uuid.uuid4().hex[:8]
        state.connections[elder_id] = Connection(
            agent_id=elder_id,
            agent_url="http://localhost:9901/mcp",
            agent_bio="Elder agent",
            wallets={"solana": "FakeWallet1111111111111111111111111111111111"},
            peer_created_at=(now - timedelta(days=30)).isoformat(),
        )
        state.impressions[elder_id] = Impression(score=0.5)

        # Elder is already in path
        sig = AntiMatterSignal(
            signal_id="am-test",
            original_tx="tx-1",
            sender_agent_id="sender-1",
            amount=0.01,
            token="SOL",
            token_decimals=9,
            sender_superagent_wallet="",
            callback_url="http://localhost:9900/__darkmatter__/antimatter_result",
            path=[elder_id],
        )

        selected = select_elder(state, sig)
        report("elder excluded (in path)", selected is None,
               f"got: {selected.agent_id if selected else None}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_elder_excludes_younger_peers() -> None:
    """select_elder excludes peers younger than us."""
    path = make_state_file()
    try:
        _, state = create_agent(path)
        now = datetime.now(timezone.utc)
        state.created_at = (now - timedelta(days=60)).isoformat()  # we are old

        young_id = "young-" + uuid.uuid4().hex[:8]
        state.connections[young_id] = Connection(
            agent_id=young_id,
            agent_url="http://localhost:9901/mcp",
            agent_bio="Young agent",
            wallets={"solana": "FakeWallet1111111111111111111111111111111111"},
            peer_created_at=(now - timedelta(days=1)).isoformat(),  # younger than us
        )
        state.impressions[young_id] = Impression(score=0.5)

        sig = AntiMatterSignal(
            signal_id="am-test",
            original_tx="tx-1",
            sender_agent_id="sender-1",
            amount=0.01,
            token="SOL",
            token_decimals=9,
            sender_superagent_wallet="",
            callback_url="http://localhost:9900/__darkmatter__/antimatter_result",
            path=[],
        )

        selected = select_elder(state, sig)
        report("younger peer excluded", selected is None,
               f"got: {selected.agent_id if selected else None}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_elder_returns_none_when_empty() -> None:
    """select_elder returns None with no connections."""
    path = make_state_file()
    try:
        _, state = create_agent(path)
        sig = AntiMatterSignal(
            signal_id="am-test",
            original_tx="tx-1",
            sender_agent_id="sender-1",
            amount=0.01,
            token="SOL",
            token_decimals=9,
            sender_superagent_wallet="",
            callback_url="http://localhost:9900/__darkmatter__/antimatter_result",
            path=[],
        )
        selected = select_elder(state, sig)
        report("returns None (no connections)", selected is None,
               f"got: {selected}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_elder_weighted_selection() -> None:
    """High trust*age peer is selected more often (statistical, 200 runs)."""
    path = make_state_file()
    try:
        _, state = create_agent(path)
        now = datetime.now(timezone.utc)
        state.created_at = now.isoformat()

        # High weight elder: very old + high trust
        high_id = "high-" + uuid.uuid4().hex[:8]
        state.connections[high_id] = Connection(
            agent_id=high_id,
            agent_url="http://localhost:9901/mcp",
            agent_bio="High weight elder",
            wallets={"solana": "FakeWallet1111111111111111111111111111111111"},
            peer_created_at=(now - timedelta(days=365)).isoformat(),
        )
        state.impressions[high_id] = Impression(score=0.9)

        # Low weight elder: slightly old + low trust
        low_id = "low-" + uuid.uuid4().hex[:8]
        state.connections[low_id] = Connection(
            agent_id=low_id,
            agent_url="http://localhost:9902/mcp",
            agent_bio="Low weight elder",
            wallets={"solana": "FakeWallet2222222222222222222222222222222222"},
            peer_created_at=(now - timedelta(days=2)).isoformat(),
        )
        state.impressions[low_id] = Impression(score=0.1)

        sig = AntiMatterSignal(
            signal_id="am-test",
            original_tx="tx-1",
            sender_agent_id="sender-1",
            amount=0.01,
            token="SOL",
            token_decimals=9,
            sender_superagent_wallet="",
            callback_url="http://localhost:9900/__darkmatter__/antimatter_result",
            path=[],
        )

        counts = {high_id: 0, low_id: 0}
        for _ in range(200):
            selected = select_elder(state, sig)
            if selected:
                counts[selected.agent_id] += 1

        report("high-weight elder selected more often",
               counts[high_id] > counts[low_id],
               f"high={counts[high_id]}, low={counts[low_id]}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_antimatter_log_caps_at_100() -> None:
    """log_antimatter_event evicts oldest entries when over ANTIMATTER_LOG_MAX."""
    path = make_state_file()
    try:
        _, state = create_agent(path)
        # Fill with 100 entries
        for i in range(105):
            log_antimatter_event(state, {"type": "test", "index": i})

        report("antimatter_log capped at 100", len(state.antimatter_log) == 100,
               f"got: {len(state.antimatter_log)}")
        # Oldest should be index 5 (0-4 evicted)
        report("oldest evicted", state.antimatter_log[0].get("index") == 5,
               f"first index: {state.antimatter_log[0].get('index')}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_antimatter_log_adds_timestamp() -> None:
    """log_antimatter_event adds ISO timestamp."""
    path = make_state_file()
    try:
        _, state = create_agent(path)
        event = {"type": "test"}
        log_antimatter_event(state, event)
        report("timestamp added", "timestamp" in state.antimatter_log[-1],
               f"keys: {list(state.antimatter_log[-1].keys())}")
        # Verify it parses as ISO
        ts = state.antimatter_log[-1]["timestamp"]
        try:
            datetime.fromisoformat(ts)
            report("timestamp is valid ISO", True)
        except ValueError:
            report("timestamp is valid ISO", False, f"got: {ts!r}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ---------------------------------------------------------------------------
# Tier 2: ASGI Integration Tests (in-process HTTP, mocked Solana)
# ---------------------------------------------------------------------------


async def test_antimatter_match_valid() -> None:
    """POST antimatter_match with valid n returns pick in [0, n]."""
    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/antimatter_match", json={
                "signal_id": "am-test",
                "n": 5,
            })

        report("antimatter_match returns 200", resp.status_code == 200,
               f"got: {resp.status_code}")
        data = resp.json()
        pick = data.get("pick")
        report("pick in [0, 5]", pick is not None and 0 <= pick <= 5,
               f"got: {pick}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_antimatter_match_rejects_invalid_n() -> None:
    """antimatter_match rejects n=0, n=-1, missing n."""
    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r0 = await client.post("/__darkmatter__/antimatter_match", json={"signal_id": "x", "n": 0})
            r_neg = await client.post("/__darkmatter__/antimatter_match", json={"signal_id": "x", "n": -1})
            r_miss = await client.post("/__darkmatter__/antimatter_match", json={"signal_id": "x"})

        report("n=0 -> 400", r0.status_code == 400, f"got: {r0.status_code}")
        report("n=-1 -> 400", r_neg.status_code == 400, f"got: {r_neg.status_code}")
        report("missing n -> 400", r_miss.status_code == 400, f"got: {r_miss.status_code}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_antimatter_signal_accepts_valid() -> None:
    """antimatter_signal endpoint accepts valid signal (match game monkeypatched)."""
    import darkmatter.wallet.antimatter as _am

    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)

        # Monkeypatch run_antimatter_match to no-op
        orig = _am.run_antimatter_match
        _am.run_antimatter_match = lambda *a, **kw: asyncio.sleep(0)
        try:
            payload = {
                "signal_id": "am-test-sig",
                "original_tx": "tx-1",
                "sender_agent_id": "sender-1",
                "amount": 0.005,
                "token": "SOL",
                "callback_url": "http://localhost:9900/__darkmatter__/antimatter_result",
                "hops": 0,
                "max_hops": 10,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "path": [],
            }

            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/__darkmatter__/antimatter_signal", json=payload)

            report("antimatter_signal accepted (200)", resp.status_code == 200,
                   f"got: {resp.status_code}")
        finally:
            _am.run_antimatter_match = orig
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_antimatter_signal_rejects_expired_hops() -> None:
    """antimatter_signal rejects signal where hops >= max_hops."""
    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)

        payload = {
            "signal_id": "am-expired",
            "original_tx": "tx-1",
            "sender_agent_id": "sender-1",
            "amount": 0.005,
            "token": "SOL",
            "callback_url": "http://localhost:9900/__darkmatter__/antimatter_result",
            "hops": 10,
            "max_hops": 10,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "path": [],
        }

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/antimatter_signal", json=payload)

        report("expired hops -> 400", resp.status_code == 400,
               f"got: {resp.status_code}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_antimatter_signal_rejects_loop() -> None:
    """antimatter_signal rejects signal where our agent_id is in path."""
    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)

        payload = {
            "signal_id": "am-loop",
            "original_tx": "tx-1",
            "sender_agent_id": "sender-1",
            "amount": 0.005,
            "token": "SOL",
            "callback_url": "http://localhost:9900/__darkmatter__/antimatter_result",
            "hops": 1,
            "max_hops": 10,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "path": [state.agent_id],  # we are already in the path
        }

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/antimatter_signal", json=payload)

        report("loop detected -> 400", resp.status_code == 400,
               f"got: {resp.status_code}")
        report("error mentions loop", "loop" in resp.json().get("error", "").lower(),
               f"got: {resp.json().get('error', '')}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_antimatter_result_resolves_known_signal() -> None:
    """antimatter_result resolves a known antimatter_initiated signal -> antimatter_sent logged."""
    import darkmatter.wallet.solana as _sol
    import darkmatter.state as _state_mod

    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)

        signal_id = "am-known-1"

        # Insert a antimatter_initiated log entry
        log_antimatter_event(state, {
            "type": "antimatter_initiated",
            "signal_id": signal_id,
            "amount": 0.005,
            "token": "SOL",
            "token_decimals": 9,
        })

        # Mock send_solana_sol to succeed
        orig = _sol.send_solana_sol
        async def mock_send_success(*a, **kw):
            return {"success": True, "tx_signature": "mock-tx-123"}
        _sol.send_solana_sol = mock_send_success

        # Mock save_state to no-op
        orig_save = _state_mod.save_state
        _state_mod.save_state = lambda: None

        try:
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/__darkmatter__/antimatter_result", json={
                    "signal_id": signal_id,
                    "destination_wallet": "FakeWallet1111111111111111111111111111111111",
                    "resolved_by": "elder-1",
                    "resolution": "match",
                })

            report("antimatter_result -> 200", resp.status_code == 200,
                   f"got: {resp.status_code}")

            antimatter_sent = [e for e in state.antimatter_log if e.get("type") == "antimatter_sent" and e.get("signal_id") == signal_id]
            report("antimatter_sent logged", len(antimatter_sent) == 1,
                   f"antimatter_sent entries: {len(antimatter_sent)}")
        finally:
            _sol.send_solana_sol = orig
            _state_mod.save_state = orig_save
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_antimatter_result_idempotent() -> None:
    """Same signal_id twice -> already_resolved."""
    import darkmatter.state as _state_mod

    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)

        signal_id = "am-idem-1"

        # Insert antimatter_initiated + antimatter_sent (already resolved)
        log_antimatter_event(state, {
            "type": "antimatter_initiated",
            "signal_id": signal_id,
            "amount": 0.005,
            "token": "SOL",
        })
        log_antimatter_event(state, {
            "type": "antimatter_sent",
            "signal_id": signal_id,
            "resolution": "match",
        })

        orig_save = _state_mod.save_state
        _state_mod.save_state = lambda: None
        try:
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/__darkmatter__/antimatter_result", json={
                    "signal_id": signal_id,
                    "destination_wallet": "FakeWallet",
                    "resolved_by": "elder-1",
                    "resolution": "match",
                })

            report("idempotent -> already_resolved", resp.json().get("status") == "already_resolved",
                   f"got: {resp.json()}")
        finally:
            _state_mod.save_state = orig_save
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_match_game_guaranteed_match_resolves_to_elder() -> None:
    """Monkeypatch random to force match -> antimatter resolved via elder."""
    import darkmatter.wallet.antimatter as _am
    import darkmatter.wallet.solana as _sol
    import darkmatter.state as _state_mod

    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)
        now = datetime.now(timezone.utc)
        state.created_at = now.isoformat()

        # Add elder connection
        elder_id = "elder-match-" + uuid.uuid4().hex[:8]
        state.connections[elder_id] = Connection(
            agent_id=elder_id,
            agent_url="http://localhost:9901/mcp",
            agent_bio="Elder",
            wallets={"solana": "ElderWallet111111111111111111111111111111111"},
            peer_created_at=(now - timedelta(days=30)).isoformat(),
        )
        state.impressions[elder_id] = Impression(score=0.5)

        # Add a peer for the match game (not elder — just a participant)
        peer_id = "peer-" + uuid.uuid4().hex[:8]
        state.connections[peer_id] = Connection(
            agent_id=peer_id,
            agent_url="http://localhost:9902/mcp",
            agent_bio="Peer",
            wallets={"solana": "PeerWallet1111111111111111111111111111111111"},
            peer_created_at=(now + timedelta(days=1)).isoformat(),  # younger (not elder)
        )

        sig = AntiMatterSignal(
            signal_id="am-match-force",
            original_tx="tx-1",
            sender_agent_id="sender-1",
            amount=0.005,
            token="SOL",
            token_decimals=9,
            sender_superagent_wallet="",
            callback_url="http://test/__darkmatter__/antimatter_result",
            created_at=now.isoformat(),
            path=[],
        )

        # Force random.randint to always return 0
        orig_randint = _random.randint
        _random.randint = lambda a, b: 0

        # Mock send_solana_sol
        send_calls = []
        orig_send = _sol.send_solana_sol
        async def mock_send(*a, **kw):
            send_calls.append((a, kw))
            return {"success": True, "tx_signature": "mock-tx"}
        _sol.send_solana_sol = mock_send

        # Mock save_state
        orig_save = _state_mod.save_state
        _state_mod.save_state = lambda: None

        try:
            # Mock _network_send_fn to simulate commit-reveal peer
            peer_pick = 0
            peer_nonce = secrets.token_bytes(32)
            peer_commitment = hashlib.sha256(peer_pick.to_bytes(4, "big") + peer_nonce).hexdigest()

            async def mock_network_send(agent_id, path, payload):
                if payload.get("phase") == "commit":
                    return SendResult(
                        success=True,
                        transport_name="mock",
                        response={"peer_commitment": peer_commitment, "session_token": "mock-session"},
                    )
                elif payload.get("phase") == "reveal":
                    return SendResult(
                        success=True,
                        transport_name="mock",
                        response={"peer_pick": peer_pick, "peer_nonce": peer_nonce.hex()},
                    )
                return SendResult(success=True, transport_name="mock", response={})

            orig_network_send = _am._network_send_fn
            _am._network_send_fn = mock_network_send

            await run_antimatter_match(state, sig, is_originator=True)

            antimatter_sent = [e for e in state.antimatter_log if e.get("type") == "antimatter_sent"]
            report("match resolves to elder",
                   len(antimatter_sent) == 1 and antimatter_sent[0].get("destination") == "ElderWallet111111111111111111111111111111111",
                   f"antimatter_sent: {antimatter_sent}")
        finally:
            _random.randint = orig_randint
            _sol.send_solana_sol = orig_send
            _state_mod.save_state = orig_save
            _am._network_send_fn = orig_network_send
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_match_game_no_match_forwards_to_elder() -> None:
    """Monkeypatch random to force no-match -> signal forwarded to elder."""
    import darkmatter.wallet.antimatter as _am
    import darkmatter.state as _state_mod

    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)
        now = datetime.now(timezone.utc)
        state.created_at = now.isoformat()

        # Add elder
        elder_id = "elder-fwd-" + uuid.uuid4().hex[:8]
        state.connections[elder_id] = Connection(
            agent_id=elder_id,
            agent_url="http://localhost:9901/mcp",
            agent_bio="Elder",
            wallets={"solana": "ElderWallet111111111111111111111111111111111"},
            peer_created_at=(now - timedelta(days=30)).isoformat(),
        )
        state.impressions[elder_id] = Impression(score=0.5)

        # Add peer for match game
        peer_id = "peer-" + uuid.uuid4().hex[:8]
        state.connections[peer_id] = Connection(
            agent_id=peer_id,
            agent_url="http://localhost:9902/mcp",
            agent_bio="Peer",
            wallets={"solana": "PeerWallet1111111111111111111111111111111111"},
            peer_created_at=(now + timedelta(days=1)).isoformat(),
        )

        sig = AntiMatterSignal(
            signal_id="am-nomatch-fwd",
            original_tx="tx-1",
            sender_agent_id="sender-1",
            amount=0.005,
            token="SOL",
            token_decimals=9,
            sender_superagent_wallet="",
            callback_url="http://test/__darkmatter__/antimatter_result",
            created_at=now.isoformat(),
            path=[],
        )

        # Force no match via commit-reveal XOR protocol
        # n=2 (elder + peer), orchestrator pick=0, peer pick=1
        # Elder fails commit → excluded from XOR
        # combined = 0 XOR 1 = 1, 1 % 3 != 0 → no match
        orig_randint = _random.randint
        _random.randint = lambda a, b: 0  # orchestrator pick = 0

        # Pre-generate peer's commit with pick=1
        peer_pick_val = 1
        peer_nonce_bytes = secrets.token_bytes(32)
        peer_commit_hex = hashlib.sha256(peer_pick_val.to_bytes(4, "big") + peer_nonce_bytes).hexdigest()

        forwarded_to = []

        async def mock_network_send(agent_id, path_str, payload):
            if "antimatter_signal" in path_str:
                forwarded_to.append(agent_id)
                return SendResult(success=True, transport_name="mock", response={"accepted": True})
            if payload.get("phase") == "commit":
                # Elder (first in connections) fails commit to be excluded from XOR
                if agent_id == elder_id:
                    return SendResult(success=False, transport_name="mock", error="fail")
                return SendResult(
                    success=True,
                    transport_name="mock",
                    response={"peer_commitment": peer_commit_hex, "session_token": "mock-sess-fwd"},
                )
            elif payload.get("phase") == "reveal":
                return SendResult(
                    success=True,
                    transport_name="mock",
                    response={"peer_pick": peer_pick_val, "peer_nonce": peer_nonce_bytes.hex()},
                )
            return SendResult(success=True, transport_name="mock", response={})

        orig_network_send = _am._network_send_fn
        _am._network_send_fn = mock_network_send

        orig_save = _state_mod.save_state
        _state_mod.save_state = lambda: None

        try:
            await run_antimatter_match(state, sig, is_originator=True)

            forwarded = [e for e in state.antimatter_log if e.get("type") == "forwarded"]
            report("no match -> forwarded to elder",
                   len(forwarded) == 1 and forwarded[0].get("forwarded_to") == elder_id,
                   f"forwarded: {forwarded}")
        finally:
            _random.randint = orig_randint
            _am._network_send_fn = orig_network_send
            _state_mod.save_state = orig_save
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_match_game_no_peers_terminal() -> None:
    """Zero connections -> antimatter_kept log entry (terminal)."""
    import darkmatter.state as _state_mod

    path = make_state_file()
    try:
        _, state = create_agent(path)
        use_agent(state)

        sig = AntiMatterSignal(
            signal_id="am-terminal",
            original_tx="tx-1",
            sender_agent_id="sender-1",
            amount=0.005,
            token="SOL",
            token_decimals=9,
            sender_superagent_wallet="",
            callback_url="http://test/__darkmatter__/antimatter_result",
            created_at=datetime.now(timezone.utc).isoformat(),
            path=[],
        )

        orig_save = _state_mod.save_state
        _state_mod.save_state = lambda: None
        try:
            await run_antimatter_match(state, sig, is_originator=True)
            kept = [e for e in state.antimatter_log if e.get("type") == "antimatter_kept"]
            report("no peers -> antimatter_kept", len(kept) == 1,
                   f"antimatter_log types: {[e.get('type') for e in state.antimatter_log]}")
        finally:
            _state_mod.save_state = orig_save
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_match_game_ttl_exceeded() -> None:
    """hops >= max_hops -> timeout resolution."""
    import darkmatter.wallet.solana as _sol
    import darkmatter.state as _state_mod

    path = make_state_file()
    try:
        _, state = create_agent(path)
        use_agent(state)

        sig = AntiMatterSignal(
            signal_id="am-ttl",
            original_tx="tx-1",
            sender_agent_id="sender-1",
            amount=0.005,
            token="SOL",
            token_decimals=9,
            sender_superagent_wallet="sa-wallet",
            callback_url="http://test/__darkmatter__/antimatter_result",
            created_at=datetime.now(timezone.utc).isoformat(),
            hops=10,
            max_hops=10,
            path=[],
        )

        # Mock send_solana_sol (timeout sends to superagent wallet)
        orig_send = _sol.send_solana_sol
        async def mock_send(*a, **kw):
            return {"success": True, "tx_signature": "mock-tx"}
        _sol.send_solana_sol = mock_send

        orig_save = _state_mod.save_state
        _state_mod.save_state = lambda: None
        try:
            await run_antimatter_match(state, sig, is_originator=True)
            sent = [e for e in state.antimatter_log if e.get("type") == "antimatter_sent" and e.get("resolution") == "timeout"]
            report("TTL exceeded -> timeout", len(sent) == 1,
                   f"antimatter_log types: {[e.get('type') for e in state.antimatter_log]}")
        finally:
            _sol.send_solana_sol = orig_send
            _state_mod.save_state = orig_save
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_commit_reveal_round_trip() -> None:
    """Commit-reveal protocol: commit returns commitment+token, reveal returns pick+nonce."""
    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)

        # Phase 1: Commit
        orch_pick = 2
        orch_nonce = secrets.token_bytes(32)
        orch_commitment = hashlib.sha256(orch_pick.to_bytes(4, "big") + orch_nonce).hexdigest()

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            commit_resp = await client.post("/__darkmatter__/antimatter_match", json={
                "phase": "commit",
                "signal_id": "cr-test",
                "n": 5,
                "orchestrator_commitment": orch_commitment,
            })

        report("commit returns 200", commit_resp.status_code == 200,
               f"got: {commit_resp.status_code}")
        commit_data = commit_resp.json()
        peer_commitment = commit_data.get("peer_commitment")
        session_token = commit_data.get("session_token")
        report("commit has peer_commitment", peer_commitment is not None, f"got: {commit_data}")
        report("commit has session_token", session_token is not None, f"got: {commit_data}")

        # Phase 2: Reveal
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            reveal_resp = await client.post("/__darkmatter__/antimatter_match", json={
                "phase": "reveal",
                "session_token": session_token,
                "orchestrator_pick": orch_pick,
                "orchestrator_nonce": orch_nonce.hex(),
            })

        report("reveal returns 200", reveal_resp.status_code == 200,
               f"got: {reveal_resp.status_code}")
        reveal_data = reveal_resp.json()
        peer_pick = reveal_data.get("peer_pick")
        peer_nonce_hex = reveal_data.get("peer_nonce")
        report("reveal has peer_pick", peer_pick is not None, f"got: {reveal_data}")
        report("peer_pick in [0, 5]", peer_pick is not None and 0 <= peer_pick <= 5,
               f"got: {peer_pick}")

        # Verify peer's commitment
        if peer_commitment and peer_pick is not None and peer_nonce_hex:
            valid = verify_commitment(peer_commitment, peer_pick, peer_nonce_hex)
            report("peer commitment verifies", valid, "commitment mismatch")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_commit_reveal_bad_orchestrator_commitment() -> None:
    """Reveal with wrong orchestrator commitment is rejected."""
    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)

        # Phase 1: Commit with a real commitment
        orch_commitment = hashlib.sha256(b"\x00\x00\x00\x02" + secrets.token_bytes(32)).hexdigest()

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            commit_resp = await client.post("/__darkmatter__/antimatter_match", json={
                "phase": "commit",
                "signal_id": "cr-bad",
                "n": 5,
                "orchestrator_commitment": orch_commitment,
            })
        session_token = commit_resp.json().get("session_token")

        # Phase 2: Reveal with WRONG pick (send 3 instead of 2)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            reveal_resp = await client.post("/__darkmatter__/antimatter_match", json={
                "phase": "reveal",
                "session_token": session_token,
                "orchestrator_pick": 3,
                "orchestrator_nonce": secrets.token_hex(32),
            })

        report("bad commitment -> 400", reveal_resp.status_code == 400,
               f"got: {reveal_resp.status_code}")
        report("error mentions verification", "verification" in reveal_resp.json().get("error", "").lower(),
               f"got: {reveal_resp.json()}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_commit_reveal_expired_session() -> None:
    """Reveal with unknown/expired session token is rejected."""
    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            reveal_resp = await client.post("/__darkmatter__/antimatter_match", json={
                "phase": "reveal",
                "session_token": "nonexistent-token",
                "orchestrator_pick": 0,
                "orchestrator_nonce": secrets.token_hex(32),
            })

        report("expired session -> 400", reveal_resp.status_code == 400,
               f"got: {reveal_resp.status_code}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_commit_reveal_peer_dropout_at_commit() -> None:
    """Peer fails at commit phase -> excluded, game continues as terminal if all fail."""
    import darkmatter.wallet.antimatter as _am
    import darkmatter.state as _state_mod

    path = make_state_file()
    try:
        _, state = create_agent(path)
        use_agent(state)
        now = datetime.now(timezone.utc)
        state.created_at = now.isoformat()

        # Add one peer that will fail at commit
        peer_id = "peer-dropout-" + uuid.uuid4().hex[:8]
        state.connections[peer_id] = Connection(
            agent_id=peer_id,
            agent_url="http://localhost:9999/mcp",
            agent_bio="Peer",
            wallets={"solana": "PeerWallet1111111111111111111111111111111111"},
            peer_created_at=(now + timedelta(days=1)).isoformat(),
        )

        sig = AntiMatterSignal(
            signal_id="am-dropout-commit",
            original_tx="tx-1",
            sender_agent_id="sender-1",
            amount=0.005,
            token="SOL",
            token_decimals=9,
            sender_superagent_wallet="",
            callback_url="http://test/__darkmatter__/antimatter_result",
            created_at=now.isoformat(),
            path=[],
        )

        # Mock _network_send_fn to always fail
        async def mock_network_send_fail(agent_id, path_str, payload):
            raise ConnectionError("peer down")

        orig_network_send = _am._network_send_fn
        _am._network_send_fn = mock_network_send_fail

        orig_save = _state_mod.save_state
        _state_mod.save_state = lambda: None
        try:
            await run_antimatter_match(state, sig, is_originator=True)
            kept = [e for e in state.antimatter_log if e.get("type") == "antimatter_kept"]
            report("all peers fail commit -> terminal",
                   len(kept) == 1,
                   f"antimatter_log: {[e.get('type') for e in state.antimatter_log]}")
        finally:
            _am._network_send_fn = orig_network_send
            _state_mod.save_state = orig_save
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_legacy_match_backward_compat() -> None:
    """Request without 'phase' key uses legacy stateless random pick."""
    path = make_state_file()
    try:
        app, state = create_agent(path)
        use_agent(state)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/__darkmatter__/antimatter_match", json={
                "signal_id": "legacy-test",
                "n": 5,
            })

        report("legacy returns 200", resp.status_code == 200,
               f"got: {resp.status_code}")
        data = resp.json()
        pick = data.get("pick")
        report("legacy pick in [0, 5]", pick is not None and 0 <= pick <= 5,
               f"got: {pick}")
        report("legacy has no session_token", "session_token" not in data,
               f"got: {data}")
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_initiate_antimatter_from_payment() -> None:
    """Payment message metadata triggers antimatter_initiated log entry."""
    import darkmatter.wallet.antimatter as _am
    import darkmatter.state as _state_mod

    path = make_state_file()
    try:
        _, state = create_agent(path)
        use_agent(state)

        msg = QueuedMessage(
            message_id="msg-pay-1",
            content="payment",
            webhook="http://test/webhook",
            hops_remaining=10,
            metadata={
                "amount": 1.0,
                "antimatter_rate": 0.01,
                "token": "SOL",
                "tx_signature": "tx-pay-1",
                "sender_superagent_wallet": "sa-wallet",
            },
            from_agent_id="sender-1",
        )

        # Mock run_antimatter_match and antimatter_timeout_watchdog
        orig_match = _am.run_antimatter_match
        orig_watchdog = _am.antimatter_timeout_watchdog
        _am.run_antimatter_match = lambda *a, **kw: asyncio.sleep(0)
        _am.antimatter_timeout_watchdog = lambda *a, **kw: asyncio.sleep(0)

        orig_save = _state_mod.save_state
        _state_mod.save_state = lambda: None
        try:
            await initiate_antimatter_from_payment(state, msg)
            initiated = [e for e in state.antimatter_log if e.get("type") == "antimatter_initiated"]
            report("antimatter_initiated logged", len(initiated) == 1,
                   f"antimatter_log: {state.antimatter_log}")
            if initiated:
                report("amount is 1% of payment", initiated[0].get("amount") == 0.01,
                       f"got: {initiated[0].get('amount')}")
        finally:
            _am.run_antimatter_match = orig_match
            _am.antimatter_timeout_watchdog = orig_watchdog
            _state_mod.save_state = orig_save
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_trust_boost_on_successful_gas() -> None:
    """Successful antimatter resolution boosts trust for sender and elder."""
    import darkmatter.wallet.solana as _sol
    import darkmatter.state as _state_mod

    path = make_state_file()
    try:
        _, state = create_agent(path)
        use_agent(state)

        sig = AntiMatterSignal(
            signal_id="am-trust-boost",
            original_tx="tx-1",
            sender_agent_id="sender-trust",
            amount=0.005,
            token="SOL",
            token_decimals=9,
            sender_superagent_wallet="",
            callback_url="http://test/__darkmatter__/antimatter_result",
            path=[],
        )

        orig_send = _sol.send_solana_sol
        async def mock_send_success(*a, **kw):
            return {"success": True, "tx_signature": "mock-tx"}
        _sol.send_solana_sol = mock_send_success

        orig_save = _state_mod.save_state
        _state_mod.save_state = lambda: None
        try:
            await resolve_antimatter(state, sig, "match", "dest-wallet", is_originator=True, resolved_by="elder-trust")

            sender_imp = state.impressions.get("sender-trust")
            elder_imp = state.impressions.get("elder-trust")
            report("sender trust boosted +0.01",
                   sender_imp is not None and sender_imp.score == 0.01,
                   f"got: {sender_imp}")
            report("elder trust boosted +0.01",
                   elder_imp is not None and elder_imp.score == 0.01,
                   f"got: {elder_imp}")
        finally:
            _sol.send_solana_sol = orig_send
            _state_mod.save_state = orig_save
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_no_trust_boost_on_failed_send() -> None:
    """Failed send_solana_sol -> impressions unchanged."""
    import darkmatter.wallet.solana as _sol
    import darkmatter.state as _state_mod

    path = make_state_file()
    try:
        _, state = create_agent(path)
        use_agent(state)

        sig = AntiMatterSignal(
            signal_id="am-trust-fail",
            original_tx="tx-1",
            sender_agent_id="sender-fail",
            amount=0.005,
            token="SOL",
            token_decimals=9,
            sender_superagent_wallet="",
            callback_url="http://test/__darkmatter__/antimatter_result",
            path=[],
        )

        orig_send = _sol.send_solana_sol
        async def mock_send_fail(*a, **kw):
            return {"success": False, "error": "insufficient funds"}
        _sol.send_solana_sol = mock_send_fail

        orig_save = _state_mod.save_state
        _state_mod.save_state = lambda: None
        try:
            await resolve_antimatter(state, sig, "match", "dest-wallet", is_originator=True, resolved_by="elder-fail")

            sender_imp = state.impressions.get("sender-fail")
            elder_imp = state.impressions.get("elder-fail")
            report("sender trust unchanged on failure",
                   sender_imp is None or sender_imp.score == 0.0,
                   f"got: {sender_imp}")
            report("elder trust unchanged on failure",
                   elder_imp is None or elder_imp.score == 0.0,
                   f"got: {elder_imp}")
        finally:
            _sol.send_solana_sol = orig_send
            _state_mod.save_state = orig_save
    finally:
        if os.path.exists(path):
            os.unlink(path)


async def test_gather_peer_trust() -> None:
    """_gather_peer_trust aggregates scores from connected agents."""
    path_a = make_state_file()
    path_b = make_state_file()
    path_c = make_state_file()
    path_d = make_state_file()
    try:
        app_a, state_a = create_agent(path_a, port=9900)
        stashed_a = get_state()

        # Create 3 peers — 2 will have opinions
        app_b, state_b = create_agent(path_b, port=9901)
        app_c, state_c = create_agent(path_c, port=9902)
        app_d, state_d = create_agent(path_d, port=9903)

        # Connect all to A
        await connect_agents(app_a, stashed_a, app_b, state_b)
        use_agent(stashed_a)
        await connect_agents(app_a, stashed_a, app_c, state_c)
        use_agent(stashed_a)
        await connect_agents(app_a, stashed_a, app_d, state_d)
        use_agent(stashed_a)

        # Set impressions on B and C about a target agent
        target_id = "target-agent-xyz"
        state_b.impressions[target_id] = Impression(score=0.8, note="good")
        state_c.impressions[target_id] = Impression(score=0.4, note="ok")
        # D has no impression of target

        # Mock httpx to simulate peer queries
        import httpx as _httpx

        class MockClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                pass
            async def get(self, url, **kw):
                # Parse which agent this is
                if f":{state_b.port}/" in url or f"localhost:{state_b.port}" in url:
                    imp = state_b.impressions.get(target_id)
                    if imp:
                        return MockResp(200, {"has_impression": True, "score": imp.score, "note": imp.note})
                elif f":{state_c.port}/" in url or f"localhost:{state_c.port}" in url:
                    imp = state_c.impressions.get(target_id)
                    if imp:
                        return MockResp(200, {"has_impression": True, "score": imp.score, "note": imp.note})
                return MockResp(200, {"has_impression": False})

        class MockResp:
            def __init__(self, code, data):
                self.status_code = code
                self._data = data
            def json(self):
                return self._data

        orig_async_client = _httpx.AsyncClient
        _httpx.AsyncClient = lambda **kw: MockClient()

        try:
            result = await _gather_peer_trust(stashed_a, target_id)
            report("peers_queried = 3", result.get("peers_queried") == 3,
                   f"got: {result.get('peers_queried')}")
            report("peers_with_opinion = 2", result.get("peers_with_opinion") == 2,
                   f"got: {result.get('peers_with_opinion')}")
            # avg = (0.8 + 0.4) / 2 = 0.6
            report("avg_score = 0.6", result.get("avg_score") == 0.6,
                   f"got: {result.get('avg_score')}")
        finally:
            _httpx.AsyncClient = orig_async_client
    finally:
        for p in (path_a, path_b, path_c, path_d):
            if os.path.exists(p):
                os.unlink(p)


# ---------------------------------------------------------------------------
# Tier 3: Solana Devnet Tests (opt-in)
# ---------------------------------------------------------------------------


async def airdrop_and_wait(pubkey_str: str, lamports: int = 1_000_000_000, timeout: int = 30):
    """Request devnet airdrop and poll until balance reflects it."""
    from solders.pubkey import Pubkey as SolanaPubkey
    from solana.rpc.async_api import AsyncClient as SolanaClient

    pubkey = SolanaPubkey.from_string(pubkey_str)
    async with SolanaClient("https://api.devnet.solana.com") as client:
        # Get initial balance
        init_resp = await client.get_balance(pubkey)
        init_balance = init_resp.value

        # Request airdrop
        airdrop_resp = await client.request_airdrop(pubkey, lamports)

        # Poll until balance increases
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(2)
            resp = await client.get_balance(pubkey)
            if resp.value > init_balance:
                return resp.value / LAMPORTS_PER_SOL
        raise TimeoutError(f"Airdrop not confirmed after {timeout}s")


async def test_devnet_fresh_wallet_zero_balance() -> None:
    """Fresh keypair -> balance query succeeds, returns 0."""
    priv, _ = generate_keypair()
    addr = _get_solana_wallet_address(priv)
    result = await get_solana_balance({"solana": addr})
    report("balance query succeeds", result.get("success") is True,
           f"got: {result}")
    report("balance is 0", result.get("balance") == 0,
           f"got: {result.get('balance')}")


async def test_devnet_airdrop_balance() -> None:
    """Request 1 SOL airdrop, verify balance >= 1.0."""
    priv, _ = generate_keypair()
    addr = _get_solana_wallet_address(priv)

    try:
        balance = await airdrop_and_wait(addr, 1_000_000_000, timeout=30)
        report("airdrop confirmed", balance >= 1.0, f"balance: {balance}")
    except TimeoutError as e:
        report("airdrop confirmed", False, str(e))


async def test_devnet_sol_transfer() -> None:
    """Airdrop 2 SOL to sender, send 0.5 to recipient, verify both balances."""
    priv_sender, _ = generate_keypair()
    priv_recv, _ = generate_keypair()
    addr_sender = _get_solana_wallet_address(priv_sender)
    addr_recv = _get_solana_wallet_address(priv_recv)

    try:
        await airdrop_and_wait(addr_sender, 2_000_000_000, timeout=30)
    except TimeoutError as e:
        report("SOL transfer (airdrop failed)", False, str(e))
        return

    result = await send_solana_sol(priv_sender, {"solana": addr_sender}, addr_recv, 0.5)
    report("transfer success", result.get("success") is True,
           f"got: {result}")

    if result.get("success"):
        await asyncio.sleep(3)  # wait for confirmation
        recv_bal = await get_solana_balance({"solana": addr_recv})
        report("recipient received SOL", recv_bal.get("balance", 0) >= 0.49,
               f"balance: {recv_bal.get('balance')}")

        send_bal = await get_solana_balance({"solana": addr_sender})
        report("sender balance decreased", send_bal.get("balance", 2) < 2.0,
               f"balance: {send_bal.get('balance')}")


async def test_devnet_insufficient_funds() -> None:
    """Fresh wallet (no airdrop), send 1 SOL -> success=false."""
    priv, _ = generate_keypair()
    addr = _get_solana_wallet_address(priv)

    priv_recv, _ = generate_keypair()
    addr_recv = _get_solana_wallet_address(priv_recv)

    result = await send_solana_sol(priv, {"solana": addr}, addr_recv, 1.0)
    report("insufficient funds -> failure", result.get("success") is False,
           f"got: {result}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main() -> None:
    print(f"\n{BOLD}DarkMatter Economy & Trust Test Suite{RESET}\n")

    # --- Tier 1: Pure In-Memory ---
    tier1_tests = [
        ("1. adjust_trust increment", test_adjust_trust_increment),
        ("2. adjust_trust preserves note", test_adjust_trust_preserves_note),
        ("3. adjust_trust clamps to 1.0", test_adjust_trust_clamps_upper),
        ("4. adjust_trust clamps to -1.0", test_adjust_trust_clamps_lower),
        ("5. adjust_trust rounding", test_adjust_trust_rounding),
        ("6. Wallet derivation determinism", test_wallet_derivation_determinism),
        ("7. Different keys -> different wallets", test_wallet_derivation_different_keys),
        ("8. Wallet address valid base58", test_wallet_address_valid_base58),
        ("9. _resolve_spl_token known tokens", test_resolve_spl_token_known),
        ("10. AntiMatterSignal round-trip", test_antimatter_signal_round_trip),
        ("11. Elder picks qualified", test_elder_selection_picks_qualified),
        ("12. Elder excludes agents in path", test_elder_excludes_agents_in_path),
        ("13. Elder excludes younger peers", test_elder_excludes_younger_peers),
        ("14. Elder returns None (empty)", test_elder_returns_none_when_empty),
        ("15. Elder weighted selection", test_elder_weighted_selection),
        ("16. AntiMatter log caps at 100", test_antimatter_log_caps_at_100),
        ("17. AntiMatter log adds timestamp", test_antimatter_log_adds_timestamp),
    ]

    print(f"{BOLD}Tier 1: Pure In-Memory{RESET}")
    for label, test_fn in tier1_tests:
        print(f"\n{BOLD}{label}{RESET}")
        try:
            await test_fn()
        except Exception as e:
            report(label, False, f"EXCEPTION: {e}")

    # --- Tier 2: ASGI Integration ---
    tier2_tests = [
        ("18. antimatter_match valid pick", test_antimatter_match_valid),
        ("19. antimatter_match rejects invalid n", test_antimatter_match_rejects_invalid_n),
        ("20. antimatter_signal accepts valid", test_antimatter_signal_accepts_valid),
        ("21. antimatter_signal rejects expired hops", test_antimatter_signal_rejects_expired_hops),
        ("22. antimatter_signal rejects loop", test_antimatter_signal_rejects_loop),
        ("23. antimatter_result resolves known signal", test_antimatter_result_resolves_known_signal),
        ("24. antimatter_result idempotent", test_antimatter_result_idempotent),
        ("25. Match game: match -> elder", test_match_game_guaranteed_match_resolves_to_elder),
        ("26. Match game: no match -> forward", test_match_game_no_match_forwards_to_elder),
        ("27. Match game: no peers -> terminal", test_match_game_no_peers_terminal),
        ("28. Match game: TTL exceeded", test_match_game_ttl_exceeded),
        ("29. Commit-reveal round-trip", test_commit_reveal_round_trip),
        ("30. Bad orchestrator commitment", test_commit_reveal_bad_orchestrator_commitment),
        ("31. Expired session token", test_commit_reveal_expired_session),
        ("32. Peer dropout at commit", test_commit_reveal_peer_dropout_at_commit),
        ("33. Legacy match backward compat", test_legacy_match_backward_compat),
        ("34. initiate_antimatter_from_payment", test_initiate_antimatter_from_payment),
        ("35. Trust boost on success", test_trust_boost_on_successful_gas),
        ("36. No trust boost on failure", test_no_trust_boost_on_failed_send),
        ("37. _gather_peer_trust aggregation", test_gather_peer_trust),
    ]

    print(f"\n\n{BOLD}Tier 2: ASGI Integration{RESET}")
    for label, test_fn in tier2_tests:
        print(f"\n{BOLD}{label}{RESET}")
        try:
            await test_fn()
        except Exception as e:
            report(label, False, f"EXCEPTION: {e}")

    # --- Tier 3: Solana Devnet (opt-in) ---
    run_devnet = os.environ.get("RUN_DEVNET_TESTS", "").strip() == "1"
    if run_devnet and SOLANA_AVAILABLE:
        tier3_tests = [
            ("38. Fresh wallet zero balance", test_devnet_fresh_wallet_zero_balance),
            ("39. Airdrop + balance check", test_devnet_airdrop_balance),
            ("40. SOL transfer", test_devnet_sol_transfer),
            ("41. Insufficient funds", test_devnet_insufficient_funds),
        ]

        print(f"\n\n{BOLD}Tier 3: Solana Devnet{RESET}")
        for label, test_fn in tier3_tests:
            print(f"\n{BOLD}{label}{RESET}")
            try:
                await test_fn()
            except Exception as e:
                report(label, False, f"EXCEPTION: {e}")
    elif run_devnet and not SOLANA_AVAILABLE:
        print(f"\n\n{YELLOW}Tier 3: Skipped (solana/solders not installed){RESET}")
    else:
        print(f"\n\n{YELLOW}Tier 3: Skipped (set RUN_DEVNET_TESTS=1 to enable){RESET}")

    # Summary
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{'─' * 40}")
    if passed == total:
        print(f"{GREEN}{BOLD}All {total} checks passed.{RESET}")
    else:
        failed = total - passed
        print(f"{RED}{BOLD}{failed}/{total} checks failed.{RESET}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
