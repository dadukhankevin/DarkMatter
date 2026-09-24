"""Same-network discovery and delivery between isolated loopback nodes."""
import json
import socket
import time

import pytest

from darkmatter import network
from darkmatter.collaboration import Collaboration, network_sessions, open_database
from darkmatter.collaboration_cli import execute
from darkmatter.network import NetworkNode, decide, is_password_protected

WIRED = {"kind": "wired", "interface": "lo0", "detail": "test", "address": "127.0.0.1"}


@pytest.mark.parametrize("security, protected", [
    ("WPA2_PSK", True), ("WPA3 Personal", True), ("wpa2 wpa3", True), ("WPA2 Enterprise", True),
    ("802.1X", True), ("SAE", True), ("NONE", False), ("Open", False), ("", False), ("--", False),
    ("OWE", False), ("WEP", False),
])
def test_password_protected_networks(security, protected):
    assert is_password_protected(security) is protected


@pytest.mark.parametrize("mode, kind, allowed", [
    ("auto", "wifi-secured", True), ("auto", "wired", True), ("auto", "wifi-open", False),
    ("auto", "unknown", False), ("auto", "none", False), ("on", "wifi-open", True), ("off", "wired", False),
])
def test_policy(mode, kind, allowed):
    assert decide(mode, {"kind": kind, "detail": "x", "address": "10.0.0.2"})[0] is allowed
    assert decide("on", {"kind": "wired", "address": None})[0] is False


def test_platform_classifiers(monkeypatch):
    outputs = {}
    monkeypatch.setattr(network, "_run", lambda args: outputs.get(" ".join(args), ""))
    outputs["route -n get default"] = "   interface: utun4\n"
    outputs["ipconfig getsummary en0"] = ("  InterfaceType : WiFi\n  LinkStatusActive : TRUE\n"
                                          "  Security : WPA2_PSK\n")
    assert network._darwin() == ("wifi-secured", "en0", "WPA2_PSK")  # VPN default, LAN on Wi-Fi.
    outputs["ipconfig getsummary en0"] = "  InterfaceType : WiFi\n  LinkStatusActive : TRUE\n  Security : NONE\n"
    assert network._darwin()[0] == "wifi-open"
    outputs["route -n get default"] = "interface: en4\n"
    outputs["ipconfig getsummary en4"] = "InterfaceType : Ethernet\nLinkStatusActive : TRUE\n"
    assert network._darwin()[0] == "wired"
    outputs.clear()
    outputs["nmcli -t -f DEVICE,TYPE,STATE device"] = "wlp2s0:wifi:connected\nlo:loopback:unmanaged\n"
    outputs["nmcli -t -f ACTIVE,SECURITY device wifi list ifname wlp2s0"] = "no:WPA2\nyes:\n"
    assert network._linux()[0] == "wifi-open"
    outputs["nmcli -t -f ACTIVE,SECURITY device wifi list ifname wlp2s0"] = "yes:WPA2 WPA3\n"
    assert network._linux()[0] == "wifi-secured"
    outputs["nmcli -t -f DEVICE,TYPE,STATE device"] = ""
    assert network._linux()[0] == "unknown"
    outputs["netsh wlan show interfaces"] = "    State : connected\n    Authentication : WPA2-Personal\n"
    assert network._windows()[0] == "wifi-secured"
    outputs["netsh wlan show interfaces"] = "    State : connected\n    Authentication : Open\n"
    assert network._windows()[0] == "wifi-open"


@pytest.fixture
def machines(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKMATTER_NETWORK_MODE", "auto")
    made = []
    for name in ("laptop", "desktop"):
        directory = tmp_path / name
        board = Collaboration(tmp_path / name / "project", "session", "claude-code", directory=directory)
        board.join("Work on " + name, availability="busy")
        node = NetworkNode(directory, classify=lambda: dict(WIRED), port=0, multicast=False, host=name)
        node.refresh_policy()
        assert node.trusted, node.reason
        made.append((board, node))
    (a_board, a), (b_board, b) = made
    a.extra_targets, b.extra_targets = [("127.0.0.1", b.udp_port)], [("127.0.0.1", a.udp_port)]
    yield made
    for _, node in made:
        node.stop()
        node._close()


def _until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.05)


def _discover(a, b, a_board, b_board):
    a.announce()
    b.announce()
    _until(lambda: network_sessions(a.directory)[1] and network_sessions(b.directory)[1])


def test_machines_discover_message_and_acknowledge(machines):
    (a_board, a), (b_board, b) = machines
    _discover(a, b, a_board, b_board)
    status = execute(a_board, "status")
    peer = status["network_peers"][0]
    assert (peer["id"], peer["host"], peer["project"]) == (b_board.agent_id, "desktop", "project")
    assert peer["objective"] == "Work on desktop" and status["network"]["active"]
    with open_database(a.directory) as db:  # Remote sessions are never local participants.
        assert not db.execute("SELECT 1 FROM participants WHERE id=?", (b_board.agent_id,)).fetchone()
    sent = execute(a_board, "send", recipient=b_board.agent_id, content="Can you review?", message_id="net-1")
    assert sent["via"] == "network" and sent["delivery"] == "queued"
    assert a.pump()["sent"] == 1
    assert a_board.delivery("net-1")["delivery"] == "delivered"
    inbox = b_board.read()["messages"]
    assert [(m["id"], m["via"], m["content"], m["workspace"]) for m in inbox] == [
        ("net-1", "network", "Can you review?", "project")]  # Project name, never the sender's path.
    b_board.ack(["net-1"])
    b.pump()
    assert a_board.delivery("net-1")["delivery"] == "acknowledged"
    b_board.send(a_board.agent_id, "Done", "net-2")
    b.pump()
    assert a_board.read()["messages"][0]["content"] == "Done"


def test_open_wifi_stops_sharing_and_forgets_peers(machines):
    (a_board, a), (b_board, b) = machines
    _discover(a, b, a_board, b_board)
    a.classify = lambda: {"kind": "wifi-open", "interface": "en0", "detail": "NONE", "address": "127.0.0.1"}
    a.refresh_policy()
    assert not a.trusted and a.udp is None and a.tcp is None
    state, sessions = network_sessions(a.directory)
    assert sessions == [] and "open Wi-Fi" in state["reason"]
    with pytest.raises(ValueError, match="Unknown participant"):
        a_board.send(b_board.agent_id, "should not route")
    b.announce()  # Nothing listens any more.
    time.sleep(0.2)
    assert network_sessions(a.directory)[1] == []


def _announcement(node, **changes):
    payload = json.loads(node._announcement())
    payload.update(changes)
    return payload


def test_forged_replayed_and_oversized_announcements_are_ignored(machines):
    (a_board, a), (b_board, b) = machines
    forged = _announcement(b, host="evil")
    a._accept_announcement(forged, "127.0.0.1")  # Signature no longer matches.
    assert network_sessions(a.directory)[1] == []
    genuine = _announcement(b)
    a._accept_announcement(genuine, "127.0.0.1")
    assert network_sessions(a.directory)[1][0]["host"] == "desktop"
    older = json.loads(b._announcement())
    older_ts = genuine["ts"] - 1
    older.update(ts=older_ts)
    a._accept_announcement(older, "127.0.0.1")  # Unsigned change and stale: ignored.
    stale = _announcement(b, ts=time.time() - 3600)
    a._accept_announcement(stale, "127.0.0.1")
    assert network_sessions(a.directory)[1][0]["host"] == "desktop"
    flood = _announcement(b, sessions=[{"id": "a" * 64, "client": "x", "availability": "busy"}] * 100)
    a._accept_announcement(flood, "127.0.0.1")
    assert len(network_sessions(a.directory)[1]) == 1


def _deliver(sender, items, *, device=None):
    request = {"p": network.PROTOCOL, "t": "deliver", "device": device or sender.device, "ts": time.time(),
               "nonce": "n", "items": items}
    request["sig"] = network.sign_payload(sender.private, network.DELIVER_DOMAIN, network._json(request))
    return request


def test_delivery_requires_announced_device_address_and_roster_sender(machines, tmp_path):
    from darkmatter.contract.envelope import seal_envelope
    (a_board, a), (b_board, b) = machines
    good = seal_envelope(a_board.private_key, a_board.agent_id, b_board.agent_id, "message",
                         {"content": "hi", "workspace": "p"}, envelope_id="m1").to_public_dict()
    item = [{"kind": "message", "envelope": good}]
    assert "announce first" in b.handle_request(_deliver(a, item), "127.0.0.1")["error"]
    b._accept_announcement(_announcement(a), "127.0.0.1")
    assert "announce first" in b.handle_request(_deliver(a, item), "10.9.9.9")["error"]  # Wrong source.
    stranger = Collaboration(tmp_path / "x", "stranger", "codex", directory=tmp_path / "strangerdir")
    spoofed = seal_envelope(stranger.private_key, stranger.agent_id, b_board.agent_id, "message",
                            {"content": "hi", "workspace": "p"}, envelope_id="m2").to_public_dict()
    tampered = dict(good, id="m3")
    unknown = seal_envelope(a_board.private_key, a_board.agent_id, "c" * 64, "message",
                            {"content": "hi", "workspace": "p"}, envelope_id="m4").to_public_dict()
    response = b.handle_request(_deliver(a, [
        {"kind": "message", "envelope": spoofed}, {"kind": "message", "envelope": tampered},
        {"kind": "message", "envelope": unknown}, *item]), "127.0.0.1")
    assert response["accepted"] == ["m1"]
    assert "not a session on that machine" in response["rejected"]["m2"]
    assert "unknown recipient" in response["rejected"]["m4"]
    assert "m3" not in response["accepted"]
    assert [m["id"] for m in b_board.read()["messages"]] == ["m1"]
    bad_sig = _deliver(a, item)
    bad_sig["items"] = []
    assert b.handle_request(bad_sig, "127.0.0.1")["error"] == "bad signature"


def test_only_the_routed_machine_can_acknowledge(machines, tmp_path):
    (a_board, a), (b_board, b) = machines
    _discover(a, b, a_board, b_board)
    a_board.send(b_board.agent_id, "hello", "route-1")
    a.pump()
    third = NetworkNode(tmp_path / "third", classify=lambda: dict(WIRED), port=0, multicast=False)
    a._accept_announcement(_announcement(third), "127.0.0.1")
    a.handle_request(_deliver(third, [{"kind": "receipt", "id": "route-1", "from": b_board.agent_id}]),
                     "127.0.0.1")
    assert a_board.delivery("route-1")["delivery"] == "delivered"


def test_rate_limiter_bounds_each_source():
    limiter = network._RateLimiter()
    assert sum(limiter.allow("10.0.0.5") for _ in range(100)) == network.RATE_LIMIT
    assert limiter.allow("10.0.0.6")


def test_real_sockets_move_mail_between_running_nodes(machines):
    """Drive the nodes' own loops: announce, discover, pump, receipt."""
    import threading
    (a_board, a), (b_board, b) = machines
    for node in (a, b):
        threading.Thread(target=node.run, daemon=True).start()
    _until(lambda: network_sessions(a.directory)[1] and network_sessions(b.directory)[1], timeout=15)
    a_board.send(b_board.agent_id, "over the wire", "wire-1")
    _until(lambda: b_board.read()["messages"], timeout=10)
    b_board.ack(["wire-1"])
    _until(lambda: a_board.delivery("wire-1")["delivery"] == "acknowledged", timeout=10)
    assert isinstance(socket.gethostname(), str)


def test_device_scope_is_default_and_marks_same_project(tmp_path):
    here = Collaboration(tmp_path / "app", "one", "codex")
    other = Collaboration(tmp_path / "other-project", "two", "cursor")
    other.join("Unrelated work")
    status = execute(here, "status")
    peer = next(p for p in status["peers"] if p["id"] == other.agent_id)
    assert peer["same_project"] is False and peer["objective"] == "Unrelated work"
    assert execute(here, "send", recipient=other.agent_id, content="hello across projects")["success"]
    assert other.read()["messages"][0]["content"] == "hello across projects"


def test_idle_sessions_stay_discoverable_with_availability(tmp_path, monkeypatch):
    import io
    from darkmatter.collaboration_cli import main
    from darkmatter.mcp import tools
    from darkmatter.wakeup import session_mail_notice
    root = tmp_path / "app"
    board = Collaboration(root, "idle-one", "claude-code")
    observer = Collaboration(root, "observer", "codex")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"cwd": str(root), "session_id": "idle-one", "hook_event_name": "PostToolUse"})))
    main(["hook", "--client", "claude-code"])
    peer = lambda: next(p for p in observer.status()["peers"] if p["id"] == board.agent_id)  # noqa: E731
    assert peer()["availability"] == "busy"
    session_mail_notice(root, "idle-one", "claude-code")  # The wake waiter's poll.
    assert peer()["availability"] == "idle"
    with open_database(board.directory) as db:
        db.execute("UPDATE participants SET seen=0 WHERE id=?", (board.agent_id,))
    assert all(p["id"] != board.agent_id for p in observer.status()["peers"])
    tools._served_sessions[board.identity] = board
    tools.heartbeat_served_sessions()  # An open MCP server keeps its session visible.
    assert peer()["id"] == board.agent_id
    tools._served_sessions.clear()
