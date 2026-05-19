"""
Current AntiMatter/trust tests.

The old match-signal economy was replaced by delegated fee verification. These
tests cover the current trust and delegate-selection primitives without RPC.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from darkmatter.identity import generate_keypair
from darkmatter.models import AgentState, AgentStatus, Connection, Impression
from darkmatter.state import _reset_for_tests, set_state
from darkmatter.trust import (
    adjust_trust,
    compute_seeded_trust,
    reciprocity_ratio,
)
from darkmatter.wallet import WalletProvider, register_provider
from darkmatter.wallet.antimatter import (
    select_delegate,
)


class FakeWalletProvider(WalletProvider):
    chain = "testchain"

    async def get_balance(self, address: str, mint: Optional[str] = None) -> dict:
        return {"success": True, "balance": 0}

    async def send(self, private_key_hex: str, wallets: dict, recipient: str,
                   amount: float, token: Optional[str] = None,
                   decimals: int = 9) -> dict:
        return {"success": True, "tx_signature": "fake"}

    def derive_address(self, private_key_hex: str) -> str:
        return "fake-wallet"

    async def get_inbound_transfers(self, address: str, sender: Optional[str] = None,
                                    token: Optional[str] = None,
                                    after_signature: Optional[str] = None,
                                    limit: int = 20) -> list[dict]:
        return []

    async def get_wallet_age(self, address: str) -> Optional[str]:
        return None

    async def attest_identity(self, private_key_hex: str, wallets: dict,
                              agent_id: str) -> dict:
        return {"success": True, "tx_signature": "attested"}

    async def verify_identity_attestation(self, address: str,
                                          agent_id: str) -> dict:
        return {"status": "none"}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKMATTER_STATE_FILE", str(tmp_path / "state.json"))
    register_provider(FakeWalletProvider())
    _reset_for_tests()
    yield
    _reset_for_tests()


def make_state() -> AgentState:
    priv, pub = generate_keypair()
    state = AgentState(
        agent_id=pub,
        bio="test",
        status=AgentStatus.ACTIVE,
        port=9900,
        private_key_hex=priv,
        public_key_hex=pub,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    set_state(state)
    return state


def test_adjust_trust_is_nonlinear_and_preserves_metadata():
    state = make_state()
    state.impressions["peer"] = Impression(
        score=0.5,
        note="bootstrap",
        msgs_sent=10,
        msgs_received=4,
        infrastructure=True,
    )

    adjust_trust(state, "peer", 0.1)

    imp = state.impressions["peer"]
    assert imp.score == 0.55
    assert imp.note == "bootstrap"
    assert imp.msgs_sent == 10
    assert imp.msgs_received == 4
    assert imp.infrastructure is True


def test_negative_trust_tracks_recovery_window():
    state = make_state()

    adjust_trust(state, "peer", -0.2)
    first = state.impressions["peer"]
    assert first.score < 0
    assert first.negative_since is not None

    adjust_trust(state, "peer", 2.0)
    recovered = state.impressions["peer"]
    assert recovered.score >= 0
    assert recovered.negative_since is None


def test_reciprocity_ratio_has_grace_and_infrastructure_exemption():
    assert reciprocity_ratio(Impression(score=0, msgs_sent=4, msgs_received=0)) == 1.0
    assert reciprocity_ratio(Impression(score=0, msgs_sent=10, msgs_received=2)) == 0.2
    assert reciprocity_ratio(
        Impression(score=0, msgs_sent=100, msgs_received=0, infrastructure=True)
    ) == 1.0


def test_seeded_trust_uses_positive_trusted_recommenders_only():
    state = make_state()
    state.impressions["trusted"] = Impression(score=0.8)
    state.impressions["distrusted"] = Impression(score=-0.9)

    seeded = compute_seeded_trust(state, [
        {"agent_id": "trusted", "score": 0.7},
        {"agent_id": "distrusted", "score": 1.0},
    ])

    assert seeded == 0.5


def test_select_delegate_requires_older_trusted_wallet_peer():
    state = make_state()
    now = datetime.now(timezone.utc)
    state.created_at = now.isoformat()

    older = "older"
    younger = "younger"
    recipient = "recipient"
    infra = "infra"

    for agent_id, age, score, infrastructure in [
        (older, now - timedelta(days=10), 0.5, False),
        (younger, now + timedelta(days=1), 0.9, False),
        (recipient, now - timedelta(days=20), 1.0, False),
        (infra, now - timedelta(days=30), 1.0, True),
    ]:
        state.connections[agent_id] = Connection(
            agent_id=agent_id,
            agent_url=f"https://{agent_id}.example",
            agent_bio="peer",
            wallets={"testchain": f"{agent_id}-wallet"},
            peer_created_at=age.isoformat(),
        )
        state.impressions[agent_id] = Impression(
            score=score,
            infrastructure=infrastructure,
        )

    delegate = select_delegate(state, exclude_agent_id=recipient)

    assert delegate is not None
    assert delegate.agent_id == older


def test_select_delegate_returns_none_without_provider_wallet():
    state = make_state()
    state.connections["peer"] = Connection(
        agent_id="peer",
        agent_url="https://peer.example",
        agent_bio="peer",
        wallets={"unknown_chain": "wallet"},
        peer_created_at="2020-01-01T00:00:00+00:00",
    )
    state.impressions["peer"] = Impression(score=1.0)

    assert select_delegate(state) is None
