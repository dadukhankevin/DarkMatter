"""MCP tools talk to gitbox — no HTTP daemon."""

import asyncio
import json

import pytest

from darkmatter.gitbox.mailbox import Mailbox, reset_mailbox
from darkmatter.mcp.schemas import (
    AuditInput,
    ConfigureInput,
    ConnectionAction,
    ConnectionInput,
    MaintainInput,
    SendMessageInput,
)
from darkmatter.mcp.tools import (
    audit,
    configure,
    connection,
    list_connections,
    maintain,
    send_message,
)


class _Ctx:
    session = object()


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    reset_mailbox()
    monkeypatch.setenv("DARKMATTER_PROJECT_DIR", str(tmp_path / "a"))
    yield
    reset_mailbox()


def test_tools_introduce_and_list(tmp_path, monkeypatch):
    b_root = tmp_path / "b"
    b = Mailbox(b_root)

    out = asyncio.run(connection(
        ConnectionInput(action=ConnectionAction.INTRODUCE, contact_card=b.contact_card()),
        _Ctx(),
    ))
    data = json.loads(out)
    assert data["success"]
    assert data["peer_id"] == b.agent_id
    assert data["_remote"]

    listed = json.loads(asyncio.run(list_connections(_Ctx())))
    assert listed["count"] == 1
    assert listed["connections"][0]["peer_id"] == b.agent_id


def test_tools_send_requires_active(tmp_path):
    b = Mailbox(tmp_path / "b")
    asyncio.run(connection(
        ConnectionInput(action=ConnectionAction.INTRODUCE, contact_card=b.contact_card()),
        _Ctx(),
    ))
    out = json.loads(asyncio.run(send_message(
        SendMessageInput(content="hi", target_agent_id=b.agent_id),
        _Ctx(),
    )))
    assert out["success"] is False


def test_tools_configure_visibility_and_fetch(tmp_path):
    out = json.loads(asyncio.run(configure(
        ConfigureInput(visibility="lan", lan_port=0),
        _Ctx(),
    )))
    assert out["success"]
    assert out["locators"]["visibility"] == "lan"
    assert out["_locators"]["lan"].startswith("http://")

    b = Mailbox(tmp_path / "b")
    asyncio.run(connection(
        ConnectionInput(action=ConnectionAction.INTRODUCE, contact_card=b.contact_card()),
        _Ctx(),
    ))
    set_fetch = json.loads(asyncio.run(configure(
        ConfigureInput(peer_id=b.agent_id, fetch_every=15),
        _Ctx(),
    )))
    assert set_fetch["success"]
    assert set_fetch["relationship"]["fetch_every"] == 15
    reset_mailbox()


def test_tools_maintain_and_audit_return_raw_local_state():
    maintained = json.loads(asyncio.run(maintain(MaintainInput(), _Ctx())))
    assert maintained["success"]
    audited = json.loads(asyncio.run(audit(AuditInput(), _Ctx())))
    assert audited["success"]
    assert audited["count"] == 0
    assert audited["interpretation"].startswith("Raw signed evidence")
