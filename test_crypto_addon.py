"""
Core/addon boundary tests for optional crypto support.
"""

import asyncio
import json

from darkmatter.extensions import CRYPTO_DISABLED_ERROR, crypto_enabled, load_crypto_extensions
from darkmatter.identity import generate_keypair
from darkmatter.models import AgentState, AgentStatus
from darkmatter.network.mesh import handle_local_wallet
from darkmatter.state import _reset_for_tests, set_state


class FakeRequest:
    path_params = {}
    query_params = {}


def _make_state() -> AgentState:
    priv, pub = generate_keypair()
    state = AgentState(
        agent_id=pub,
        bio="test",
        status=AgentStatus.ACTIVE,
        port=9900,
        private_key_hex=priv,
        public_key_hex=pub,
    )
    set_state(state)
    return state


def test_crypto_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DARKMATTER_ENABLE_CRYPTO", raising=False)

    assert crypto_enabled() is False
    assert load_crypto_extensions() is False


def test_wallet_endpoint_reports_disabled_core_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("DARKMATTER_ENABLE_CRYPTO", raising=False)
    monkeypatch.setenv("DARKMATTER_STATE_FILE", str(tmp_path / "state.json"))
    _reset_for_tests()
    _make_state()

    response = asyncio.run(handle_local_wallet(FakeRequest()))
    body = json.loads(response.body)

    assert response.status_code == 501
    assert body["enabled"] is False
    assert body["wallets"] == {}
    assert body["error"] == CRYPTO_DISABLED_ERROR

    _reset_for_tests()
