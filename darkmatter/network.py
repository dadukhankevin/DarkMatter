"""Same-network agent discovery and delivery, on password-protected networks only.

Admission is the network itself: on WPA/WPA2/WPA3 Wi-Fi or wired Ethernet,
machines running DarkMatter discover each other's sessions and exchange mail.
On open or public Wi-Fi, or when the network cannot be classified, the node
announces nothing, answers nothing, and accepts nothing. The owner can force it
with `darkmatter network on` or disable it with `darkmatter network off`.

One node runs per OS account (whichever MCP server or `darkmatter network run`
takes the lock first). It announces a signed roster over UDP multicast (TTL 1)
and accepts device-signed deliveries over TCP on the LAN interface address.
Message bodies are sealed end to end to recipient session keys; rosters
(hostname, client, objective, project name, git branch, uncommitted file names,
last commit subject, availability) are visible to other
machines on the same trusted network. Remote sessions are recorded only in
network_peers, never as local participants, and their text is untrusted data.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from darkmatter.collaboration import MAX_PENDING, MESSAGE_SECONDS, PRESENCE_SECONDS, local_directory, open_database
from darkmatter.contract.envelope import validate_envelope_id, verify_envelope_signature
from darkmatter.facts import host_name, valid_facts
from darkmatter.identity import derive_public_key_hex, generate_keypair
from darkmatter.security import sign_payload, verify_signed_payload
from darkmatter.store.local import atomic_write_text

PROTOCOL = "darkmatter.lan.v1"
ANNOUNCE_DOMAIN = "darkmatter.lan.announce.v1"
DELIVER_DOMAIN = "darkmatter.lan.deliver.v1"
GROUP = "239.255.42.100"
PORT = 8743
MODES = ("auto", "on", "off")
TRUSTED_KINDS = ("wifi-secured", "wired")
MAX_PACKET = 16 * 1024
MAX_REQUEST = 512 * 1024
MAX_ENVELOPE = 64 * 1024
MAX_ITEMS = 32
MAX_SESSIONS = 48
MAX_NETWORK_PEERS = 64
MAX_CONNECTIONS = 32
PEER_SECONDS = 45
ANNOUNCE_SECONDS = 10
POLICY_SECONDS = 15
STATE_SECONDS = 60
CLOCK_SKEW = 120
RATE_WINDOW, RATE_LIMIT = 10.0, 1000  # Flood guard per source address, far above normal use.
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------- network policy

def _run(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=3, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _field(text: str, name: str) -> str:
    match = re.search(rf"^\s*{re.escape(name)}\s*:\s*(.*?)\s*$", text, re.MULTILINE)
    return match.group(1) if match else ""


def is_password_protected(security: str) -> bool:
    """WPA-family personal/enterprise security counts; open, OWE and WEP do not."""
    value = security.upper().replace("-", "_").replace(" ", "_").replace(".", "_")
    if not value or value in ("NONE", "OPEN", "--") or "OWE" in value or "WEP" in value:
        return False
    return any(token in value for token in ("WPA", "PSK", "SAE", "EAP", "802_1X", "8021X", "ENTERPRISE"))


def _darwin() -> tuple[str, str, str]:
    route = re.search(r"interface:\s*(\S+)", _run(["route", "-n", "get", "default"]))
    candidates = [route.group(1)] if route else []
    if "en0" not in candidates:
        candidates.append("en0")  # A VPN default route still leaves the LAN on Wi-Fi.
    for interface in candidates:
        summary = _run(["ipconfig", "getsummary", interface])
        if _field(summary, "LinkStatusActive").upper() != "TRUE":
            continue
        kind = _field(summary, "InterfaceType").lower()
        if kind == "wifi":
            security = _field(summary, "Security") or "none"
            return ("wifi-secured" if is_password_protected(security) else "wifi-open"), interface, security
        if kind == "ethernet":
            return "wired", interface, "ethernet"
    return "none", "", "no active Wi-Fi or Ethernet interface"


def _linux() -> tuple[str, str, str]:
    devices = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"])
    if not devices:
        return "unknown", "", "cannot classify the network (NetworkManager unavailable)"
    for line in devices.splitlines():
        parts = line.split(":")
        if len(parts) < 3 or parts[2] != "connected":
            continue
        if parts[1] == "ethernet":
            return "wired", parts[0], "ethernet"
        if parts[1] == "wifi":
            for row in _run(["nmcli", "-t", "-f", "ACTIVE,SECURITY", "device", "wifi", "list",
                             "ifname", parts[0]]).splitlines():
                active, _, security = row.partition(":")
                if active == "yes":
                    return ("wifi-secured" if is_password_protected(security) else "wifi-open"), parts[0], security or "none"
            return "unknown", parts[0], "cannot read Wi-Fi security"
    return "none", "", "no connected interface"


def _windows() -> tuple[str, str, str]:
    output = _run(["netsh", "wlan", "show", "interfaces"])
    if _field(output, "State").lower() == "connected":
        security = _field(output, "Authentication") or "none"
        return ("wifi-secured" if is_password_protected(security) else "wifi-open"), _field(output, "Name"), security
    return "unknown", "", "cannot classify a non-Wi-Fi network on Windows"


def _interface_address(interface: str) -> str | None:
    if interface and sys.platform == "darwin":
        address = _run(["ipconfig", "getifaddr", interface]).strip()
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", address):
            return address
    if interface and sys.platform.startswith("linux"):
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", _run(["ip", "-4", "-o", "addr", "show", "dev", interface]))
        if match:
            return match.group(1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # No packet is sent for a UDP connect.
            address = probe.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


def _broadcast_address(interface: str, address: str | None) -> str | None:
    """Subnet broadcast: many routers and mesh systems drop multicast but pass this."""
    if not address:
        return None
    if interface and sys.platform == "darwin":
        match = re.search(r"broadcast (\d+\.\d+\.\d+\.\d+)", _run(["ifconfig", interface]))
        if match:
            return match.group(1)
    if interface and sys.platform.startswith("linux"):
        match = re.search(r"brd (\d+\.\d+\.\d+\.\d+)", _run(["ip", "-4", "-o", "addr", "show", "dev", interface]))
        if match:
            return match.group(1)
    return address.rsplit(".", 1)[0] + ".255"  # Typical home /24 when the mask is unknown.


def classify_network() -> dict:
    if sys.platform == "darwin":
        kind, interface, detail = _darwin()
    elif sys.platform.startswith("linux"):
        kind, interface, detail = _linux()
    elif sys.platform == "win32":
        kind, interface, detail = _windows()
    else:
        kind, interface, detail = "unknown", "", "unsupported platform"
    address = _interface_address(interface) if kind != "none" else None
    return {"kind": kind, "interface": interface, "detail": detail, "address": address,
            "broadcast": _broadcast_address(interface, address)}


def get_mode(directory=None) -> str:
    override = os.environ.get("DARKMATTER_NETWORK_MODE", "")
    if override in MODES:
        return override
    try:
        mode = json.loads((local_directory(directory) / "network.json").read_text()).get("mode")
    except (OSError, ValueError, AttributeError):
        return "auto"
    return mode if mode in MODES else "auto"


def set_mode(mode: str, directory=None) -> dict:
    if mode not in MODES:
        raise ValueError("mode must be auto, on or off")
    atomic_write_text(local_directory(directory) / "network.json", _json({"mode": mode}) + "\n", mode=0o600)
    return {"success": True, "mode": mode}


def decide(mode: str, network: dict) -> tuple[bool, str]:
    if mode == "off":
        return False, "network sharing is off (`darkmatter network auto` re-enables it)"
    if not network.get("address"):
        return False, "no local network address"
    if mode == "on":
        return True, "network sharing forced on for every network"
    if network["kind"] == "wifi-secured":
        return True, f"password-protected Wi-Fi ({network['detail']})"
    if network["kind"] == "wired":
        return True, "wired network"
    if network["kind"] == "wifi-open":
        return False, "open Wi-Fi: agents are not shared on public networks"
    return False, network.get("detail") or "unrecognized network"


def read_state(directory=None) -> dict:
    """The running node's last report; stale reports mean no node is running."""
    try:
        state = json.loads((local_directory(directory) / "network_state.json").read_text())
    except (OSError, ValueError):
        return {"running": False, "mode": get_mode(directory), "reason": "no network node has run yet"}
    state["running"] = time.time() - state.get("updated", 0) < STATE_SECONDS
    if not state["running"]:
        state["reason"] = "no network node running (it starts with an MCP server or `darkmatter network run`)"
    return state


def device_key(directory=None) -> tuple[str, str]:
    path = local_directory(directory) / "device.key"
    if path.is_symlink():
        raise ValueError("Device key must not be a symlink")
    if not path.exists():
        private, _ = generate_keypair()
        atomic_write_text(path, private + "\n", mode=0o600)
    private = path.read_text().strip()
    os.chmod(path, 0o600)
    return private, derive_public_key_hex(private)


# ------------------------------------------------------------------- validation

def _text(value, maximum: int) -> bool:
    return isinstance(value, str) and len(value.encode("utf-8")) <= maximum and "\0" not in value


def _valid_roster(sessions) -> bool:
    if not isinstance(sessions, list) or len(sessions) > MAX_SESSIONS:
        return False
    return all(isinstance(m, dict) and isinstance(m.get("id"), str) and _HEX64.fullmatch(m["id"])
               and _text(m.get("client"), 80) and _text(m.get("objective", ""), 512)
               and _text(m.get("project", ""), 128) and m.get("availability") in ("busy", "idle", "unknown")
               and type(m.get("objective_at", 0)) in (int, float) and valid_facts(m.get("facts"))
               for m in sessions)


def _fresh(ts) -> bool:
    return type(ts) in (int, float) and abs(time.time() - ts) <= CLOCK_SKEW


class _RateLimiter:
    def __init__(self):
        self.lock, self.seen = threading.Lock(), {}

    def allow(self, address: str) -> bool:
        now = time.monotonic()
        with self.lock:
            if len(self.seen) > 4096:
                self.seen.clear()
            recent = [t for t in self.seen.get(address, []) if now - t < RATE_WINDOW]
            if len(recent) >= RATE_LIMIT:
                self.seen[address] = recent
                return False
            self.seen[address] = recent + [now]
            return True


# ------------------------------------------------------------------------- node

class _Endpoint:
    """This machine's signed identity on the network: roster, announcements, deliveries.

    Any process can use it (a sender delivering immediately); the node adds sockets.
    """

    def __init__(self, directory=None):
        self.directory = local_directory(directory)
        self.private, self.device = device_key(self.directory)
        self.host = host_name()[:128]
        state = read_state(self.directory)
        self.tcp_port = state.get("tcp_port", 0) if state.get("running") else 0

    def _roster(self) -> list[dict]:
        with open_database(self.directory) as db:
            rows = db.execute("SELECT id, workspace, client, objective, objective_at, availability, facts "
                              "FROM participants "
                              "WHERE seen > ? ORDER BY seen DESC LIMIT ?",
                              (time.time() - PRESENCE_SECONDS, MAX_SESSIONS)).fetchall()
        return [{"id": row["id"], "client": (row["client"] or "")[:80],
                 "objective": (row["objective"] or "")[:512], "project": Path(row["workspace"]).name[:128],
                 "availability": row["availability"] if row["availability"] in ("busy", "idle") else "unknown",
                 "objective_at": row["objective_at"] or 0, "facts": self._facts(row["facts"])}
                for row in rows]

    @staticmethod
    def _facts(raw: str) -> dict:
        try:
            facts = json.loads(raw or "{}")
        except ValueError:
            return {}
        return facts if valid_facts(facts) else {}

    def _announcement(self) -> bytes:
        sessions = self._roster()
        while True:
            payload = {"p": PROTOCOL, "t": "announce", "device": self.device, "host": self.host,
                       "port": self.tcp_port, "ts": time.time(), "nonce": uuid.uuid4().hex,
                       "sessions": sessions}
            payload["sig"] = sign_payload(self.private, ANNOUNCE_DOMAIN, _json(payload))
            raw = _json(payload).encode()
            if len(raw) <= MAX_PACKET or not sessions:
                return raw
            sessions = sessions[:-1]

    def _request(self, address: str, port: int, items: list) -> dict:
        request = {"p": PROTOCOL, "t": "deliver", "device": self.device, "ts": time.time(),
                   "nonce": uuid.uuid4().hex, "items": items}
        if self.tcp_port:
            # Carry our signed roster so the receiver never depends on having heard a broadcast.
            request["announcement"] = json.loads(self._announcement())
        request["sig"] = sign_payload(self.private, DELIVER_DOMAIN, _json(request))
        with socket.create_connection((address, port), timeout=3) as conn:
            conn.settimeout(5)
            conn.sendall(_json(request).encode() + b"\n")
            data = b""
            while b"\n" not in data and len(data) <= 65536:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
        response = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
        if not isinstance(response, dict) or response.get("p") != PROTOCOL or not response.get("ok"):
            raise ValueError(str(response.get("error") if isinstance(response, dict) else "bad response"))
        return response




def deliver_pending(endpoint, *, route: str | None = None, backoff: dict | None = None) -> dict:
    """Deliver queued network mail and receipts, recording the exact error on failure."""
    with open_database(endpoint.directory) as db:
        query = ("SELECT * FROM messages WHERE origin IN ('network-out', 'network-receipt') "
                 "AND delivered=0 AND acknowledged=0 AND expires>?")
        args: list = [time.time()]
        if route:
            query, args = query + " AND route=?", args + [route]
        rows = db.execute(query + " ORDER BY created LIMIT 256", args).fetchall()
        peers = {row["device"]: row for row in db.execute(
            "SELECT * FROM network_peers WHERE seen > ?", (time.time() - PEER_SECONDS,))}
    routes: dict[str, list] = {}
    for row in rows:
        routes.setdefault(row["route"], []).append(row)
    sent, errors = 0, {}
    for device, batch in routes.items():
        batch = batch[:MAX_ITEMS]
        peer = peers.get(device)
        retry_at, delay = (backoff or {}).get(device, (0.0, 1.0))
        if backoff is not None and time.monotonic() < retry_at:
            continue
        error = None
        if peer is None:
            error = "that machine has not been heard on this network in the last 45 seconds"
        else:
            items = [{"kind": "receipt", "id": json.loads(row["envelope"])["message_id"], "from": row["sender"]}
                     if row["origin"] == "network-receipt"
                     else {"kind": "message", "envelope": json.loads(row["envelope"])["envelope"]}
                     for row in batch]
            try:
                response = endpoint._request(peer["address"], peer["port"], items)
            except (OSError, ValueError) as exc:
                error = f"{peer['host']} ({peer['address']}:{peer['port']}): {exc or type(exc).__name__}"
        if error:
            errors[device] = error
            if backoff is not None:
                backoff[device] = (time.monotonic() + delay, min(delay * 2, 60.0))
            with open_database(endpoint.directory) as db:
                db.executemany("UPDATE messages SET last_error=? WHERE id=?", [(error, row["id"]) for row in batch])
            continue
        if backoff is not None:
            backoff.pop(device, None)
        accepted, rejected = set(response.get("accepted", [])), response.get("rejected", {})
        with open_database(endpoint.directory) as db:
            for row in batch:
                if row["origin"] == "network-receipt":
                    # Receipts are best effort: delivered or refused, never retried forever.
                    db.execute("DELETE FROM messages WHERE id=?", (row["id"],))
                elif row["id"] in accepted:
                    db.execute("UPDATE messages SET delivered=1, last_error='' WHERE id=?", (row["id"],))
                    sent += 1
                elif row["id"] in rejected:
                    db.execute("UPDATE messages SET delivered=2, last_error=? WHERE id=?",
                               (str(rejected[row["id"]])[:200], row["id"]))
    return {"sent": sent, "errors": errors}


def deliver_now(directory=None, route: str | None = None) -> dict:
    """Called right after a send: connect to the peer directly instead of waiting for the node."""
    state = read_state(directory)
    if not state.get("running") or not state.get("trusted"):
        return {"sent": 0, "errors": {"network": state.get("reason") or "network sharing is not active"}}
    return deliver_pending(_Endpoint(directory), route=route)


class NetworkNode(_Endpoint):
    """Announce local sessions, learn peers, and move mail on a trusted network."""

    def __init__(self, directory=None, *, classify=classify_network, group=GROUP, port=PORT,
                 extra_targets=(), host=None, multicast=True):
        self.directory = local_directory(directory)
        self.private, self.device = device_key(self.directory)
        self.classify, self.group, self.port = classify, group, port
        self.extra_targets = list(extra_targets)  # Unicast targets, e.g. for tests.
        self.multicast = multicast
        self.host = (host or host_name())[:128]
        self.udp = self.tcp = None
        self.address, self.trusted, self.reason, self.network = None, False, "starting", {}
        self.udp_port = self.tcp_port = 0
        self.rate = _RateLimiter()
        self.slots = threading.Semaphore(MAX_CONNECTIONS)
        self.backoff: dict[str, tuple[float, float]] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle
    def run(self) -> None:
        next_policy = next_announce = 0.0
        cycle = 0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_policy:
                    self.refresh_policy()
                    next_policy = now + POLICY_SECONDS
                if self.trusted and now >= next_announce:
                    self.announce()
                    if cycle % 3 == 0:
                        # Periodic probes also find nodes whose own announcements cannot reach us.
                        self._send({"p": PROTOCOL, "t": "probe", "device": self.device})
                    cycle += 1
                    self._expire_peers()
                    next_announce = now + ANNOUNCE_SECONDS
                if self.trusted:
                    self.pump()
                self._stop.wait(1.0)
        finally:
            self._close()
            self._write_state(running=False)

    def stop(self) -> None:
        self._stop.set()

    def refresh_policy(self) -> None:
        mode = get_mode(self.directory)
        try:
            self.network = self.classify()
        except Exception as exc:  # A classifier failure must fail closed.
            self.network = {"kind": "unknown", "detail": f"classification failed: {type(exc).__name__}", "address": None}
        trusted, self.reason = decide(mode, self.network)
        address = self.network.get("address")
        if trusted and (not self.trusted or address != self.address):
            self._close()
            try:
                self._open(address)
            except OSError as exc:
                trusted, self.reason = False, f"cannot open network sockets: {exc}"
                self._close()
        elif not trusted and self.trusted:
            self._close()
        if not trusted:
            with open_database(self.directory) as db:
                db.execute("DELETE FROM network_peers")  # Never show peers from a network we left.
        self.trusted = trusted
        self._write_state(running=True, mode=mode)

    def _write_state(self, *, running: bool, mode: str | None = None) -> None:
        state = {"device": self.device, "host": self.host, "trusted": self.trusted and running,
                 "reason": self.reason, "kind": self.network.get("kind"), "address": self.address,
                 "tcp_port": self.tcp_port, "mode": mode or get_mode(self.directory),
                 "pid": os.getpid(), "updated": time.time() if running else 0}
        atomic_write_text(self.directory / "network_state.json", _json(state) + "\n", mode=0o600)

    def _open(self, address: str) -> None:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind((address, 0))  # LAN interface only, never 0.0.0.0.
        tcp.listen(16)
        tcp.settimeout(0.5)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.bind(("", self.port))
        if self.multicast:
            udp.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                           socket.inet_aton(self.group) + socket.inet_aton(address))
            udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
        udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        udp.settimeout(0.5)
        self.tcp, self.udp, self.address = tcp, udp, address
        self.broadcast = self.network.get("broadcast") if self.multicast else None
        self.tcp_port, self.udp_port = tcp.getsockname()[1], udp.getsockname()[1]
        self._threads = [threading.Thread(target=self._serve_udp, args=(udp,), daemon=True),
                         threading.Thread(target=self._serve_tcp, args=(tcp,), daemon=True)]
        for thread in self._threads:
            thread.start()
        self._send({"p": PROTOCOL, "t": "probe", "device": self.device})

    def _close(self) -> None:
        for sock in (self.udp, self.tcp):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        for thread in self._threads:
            thread.join(timeout=2)
        self.udp = self.tcp = None
        self.address, self._threads, self.tcp_port = None, [], 0

    def _send(self, message: dict | bytes, targets=None) -> None:
        if self.udp is None:
            return
        raw = message if isinstance(message, bytes) else _json(message).encode()
        # Multicast plus subnet broadcast: networks that filter one usually pass the other.
        default = [(self.group, self.port)] if self.multicast else []
        if getattr(self, "broadcast", None):
            default.append((self.broadcast, self.port))
        for target in targets or [*default, *self.extra_targets]:
            try:
                self.udp.sendto(raw, target)
            except OSError:
                pass

    def announce(self, targets=None) -> None:
        self._send(self._announcement(), targets)

    def _serve_udp(self, udp) -> None:
        while not self._stop.is_set():
            try:
                raw, (address, source_port) = udp.recvfrom(MAX_PACKET + 1)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(raw) > MAX_PACKET or not self.trusted or not self.rate.allow(address):
                continue
            try:
                message = json.loads(raw.decode("utf-8"))
                if not isinstance(message, dict) or message.get("p") != PROTOCOL:
                    continue
                if message.get("t") == "probe" and message.get("device") != self.device:
                    self.announce([(address, source_port)])
                elif message.get("t") == "announce":
                    if self._accept_announcement(message, address):
                        # New machine: answer directly, so one working direction suffices.
                        self.announce([(address, source_port)])
            except (ValueError, TypeError, KeyError, UnicodeDecodeError, OSError):
                continue

    def _accept_announcement(self, message: dict, address: str) -> bool:
        """Store a valid roster; True when the machine was not known before."""
        device = message.get("device")
        if not isinstance(device, str) or not _HEX64.fullmatch(device) or device == self.device:
            return
        port, host, ts = message.get("port"), message.get("host"), message.get("ts")
        if type(port) is not int or not 0 < port < 65536 or not _text(host, 128) or not _fresh(ts):
            return
        if not _valid_roster(message.get("sessions")) or not isinstance(message.get("nonce"), str):
            return
        unsigned = {k: v for k, v in message.items() if k != "sig"}
        if not verify_signed_payload(device, message.get("sig", ""), ANNOUNCE_DOMAIN, _json(unsigned)):
            return
        with open_database(self.directory) as db:
            row = db.execute("SELECT ts FROM network_peers WHERE device=?", (device,)).fetchone()
            if row is not None and row["ts"] is not None and ts <= row["ts"]:
                return False  # Replayed or reordered roster.
            if row is None and db.execute("SELECT COUNT(*) FROM network_peers").fetchone()[0] >= MAX_NETWORK_PEERS:
                return
            # The packet's source address, not a claimed one, is where mail goes.
            db.execute("INSERT INTO network_peers(device, host, address, port, sessions, seen, ts) "
                       "VALUES(?,?,?,?,?,?,?) ON CONFLICT(device) DO UPDATE SET host=excluded.host, "
                       "address=excluded.address, port=excluded.port, sessions=excluded.sessions, "
                       "seen=excluded.seen, ts=excluded.ts",
                       (device, host, address, port, _json(message["sessions"]), time.time(), ts))
        return row is None

    def _expire_peers(self) -> None:
        with open_database(self.directory) as db:
            db.execute("DELETE FROM network_peers WHERE seen <= ?", (time.time() - PEER_SECONDS,))

    # -- delivery (receiving side)
    def _serve_tcp(self, tcp) -> None:
        while not self._stop.is_set():
            try:
                conn, (address, _) = tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self.trusted or not self.rate.allow(address) or not self.slots.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=self._handle_connection, args=(conn, address), daemon=True).start()

    def _handle_connection(self, conn, address: str) -> None:
        try:
            conn.settimeout(5)
            data = b""
            while b"\n" not in data and len(data) <= MAX_REQUEST:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            if len(data) > MAX_REQUEST or b"\n" not in data:
                return
            try:
                response = self.handle_request(json.loads(data.split(b"\n", 1)[0].decode("utf-8")), address)
            except (ValueError, TypeError, KeyError, UnicodeDecodeError):
                response = {"p": PROTOCOL, "ok": False, "error": "malformed request"}
            conn.sendall(_json(response).encode() + b"\n")
        except OSError:
            pass
        finally:
            conn.close()
            self.slots.release()

    def handle_request(self, request: dict, address: str) -> dict:
        refuse = {"p": PROTOCOL, "ok": False}
        device, items = request.get("device"), request.get("items")
        if (request.get("p") != PROTOCOL or request.get("t") != "deliver" or not isinstance(device, str)
                or not _HEX64.fullmatch(device) or not _fresh(request.get("ts"))
                or not isinstance(items, list) or len(items) > MAX_ITEMS):
            return {**refuse, "error": "invalid request"}
        unsigned = {k: v for k, v in request.items() if k != "sig"}
        if not verify_signed_payload(device, request.get("sig", ""), DELIVER_DOMAIN, _json(unsigned)):
            return {**refuse, "error": "bad signature"}
        announcement = request.get("announcement")
        if isinstance(announcement, dict) and announcement.get("device") == device:
            self._accept_announcement(announcement, address)  # Validated and signature-checked there.
        accepted, receipts, rejected = [], [], {}
        with open_database(self.directory) as db:
            peer = db.execute("SELECT * FROM network_peers WHERE device=? AND seen > ?",
                              (device, time.time() - PEER_SECONDS)).fetchone()
            if peer is None or peer["address"] != address:
                return {**refuse, "error": "unknown device; announce first"}
            roster = {member["id"] for member in json.loads(peer["sessions"])}
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("kind") == "receipt":
                    mid, sender = item.get("id"), item.get("from")
                    if isinstance(mid, str) and isinstance(sender, str) and sender in roster:
                        # Only the machine we routed a message to may acknowledge it.
                        db.execute("UPDATE messages SET acknowledged=1, delivered=1 WHERE id=? AND "
                                   "origin='network-out' AND route=? AND recipient=?", (mid, device, sender))
                        receipts.append(mid)
                    continue
                reason, mid = self._accept_message(db, item.get("envelope"), device, roster)
                if mid is None:
                    continue
                if reason:
                    rejected[mid] = reason
                else:
                    accepted.append(mid)
        return {"p": PROTOCOL, "ok": True, "accepted": accepted, "receipts": receipts, "rejected": rejected}

    @staticmethod
    def _accept_message(db, raw, device: str, roster: set) -> tuple[str, str | None]:
        if not isinstance(raw, dict) or len(_json(raw)) > MAX_ENVELOPE:
            return "", None
        try:
            mid = validate_envelope_id(raw.get("id"))
            env = verify_envelope_signature(raw)
        except (ValueError, TypeError, KeyError):
            return "", None
        if env.type != "message":
            return "unsupported type", mid
        if env.from_id not in roster:
            return "sender is not a session on that machine", mid
        if not db.execute("SELECT 1 FROM participants WHERE id=?", (env.to_id,)).fetchone():
            return "unknown recipient", mid
        existing = db.execute("SELECT sender, recipient FROM messages WHERE id=?", (mid,)).fetchone()
        if existing:
            same = existing["sender"] == env.from_id and existing["recipient"] == env.to_id
            return ("" if same else "message id already used"), mid
        pending = db.execute("SELECT COUNT(*) FROM messages WHERE recipient=? AND acknowledged=0 AND expires>?",
                             (env.to_id, time.time())).fetchone()[0]
        if pending >= MAX_PENDING:
            return "recipient inbox is full", mid
        db.execute("INSERT INTO messages(id,sender,recipient,envelope,created,expires,origin,route) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (mid, env.from_id, env.to_id, _json({"envelope": raw, "content_digest": ""}),
                    time.time(), time.time() + MESSAGE_SECONDS, "network-in", device))
        return "", mid

    # -- delivery (sending side)
    def pump(self) -> dict:
        """Retry queued network mail; failures back off per device and record their error."""
        return deliver_pending(self, backoff=self.backoff)


def doctor(directory=None) -> dict:
    """Check every step directly: policy, local node, each peer (UDP + TCP), queued mail."""
    mode, current = get_mode(directory), classify_network()
    allowed, reason = decide(mode, current)
    state = read_state(directory)
    report = {"mode": mode, "network": current, "sharing": allowed, "reason": reason,
              "node": {"running": bool(state.get("running")), "address": state.get("address"),
                       "tcp_port": state.get("tcp_port")},
              "peers": [], "queued": []}
    with open_database(directory) as db:
        peers = [dict(r) for r in db.execute("SELECT device, host, address, port, seen FROM network_peers")]
        queued = [dict(r) for r in db.execute(
            "SELECT id, route, last_error, created FROM messages WHERE origin='network-out' AND delivered=0 "
            "AND acknowledged=0 AND expires>? ORDER BY created LIMIT 20", (time.time(),))]
    for peer in peers:
        entry = {"host": peer["host"], "address": f"{peer['address']}:{peer['port']}",
                 "last_heard_seconds": round(time.time() - peer["seen"])}
        try:
            with socket.create_connection((peer["address"], peer["port"]), timeout=2):
                entry["tcp"] = "ok"
        except OSError as exc:
            entry["tcp"] = f"failed: {exc}"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.settimeout(2)
                probe.sendto(_json({"p": PROTOCOL, "t": "probe", "device": "0" * 64}).encode(),
                             (peer["address"], PORT))
                probe.recvfrom(MAX_PACKET)
            entry["udp_probe"] = "answered"
        except OSError as exc:
            entry["udp_probe"] = f"no answer: {exc or 'timeout'}"
        report["peers"].append(entry)
    hosts = {p["device"]: p["host"] for p in peers}
    for row in queued:
        report["queued"].append({"id": row["id"], "to_machine": hosts.get(row["route"], row["route"][:12]),
                                 "waiting_seconds": round(time.time() - row["created"]),
                                 "last_error": row["last_error"] or "not attempted yet"})
    if not report["peers"]:
        report["hint"] = ("No machines heard. The other machine needs DarkMatter 3.14+ with an MCP client or "
                          "`darkmatter network run` running; run `darkmatter network doctor` there too.")
    return report


def run_if_leader(directory=None, stop: threading.Event | None = None, **options) -> bool:
    """Run the node while holding the per-account lock; return False if another holds it."""
    from darkmatter.wakeup import _try_lock, _unlock
    path = local_directory(directory) / "network.lock"
    if path.is_symlink():
        raise ValueError("Network lock must not be a symlink")
    handle = path.open("a+b")
    try:
        if not _try_lock(handle):
            return False
        node = NetworkNode(directory, **options)
        if stop is not None:
            threading.Thread(target=lambda: (stop.wait(), node.stop()), daemon=True).start()
        try:
            node.run()
        finally:
            _unlock(handle)
        return True
    finally:
        handle.close()


def start_background(directory=None) -> threading.Thread:
    """MCP servers call this: one of them becomes the node, the rest stand by."""
    def loop():
        while True:
            try:
                if get_mode(directory) != "off":
                    run_if_leader(directory)
            except Exception:  # Never take down the MCP server over networking.
                pass
            time.sleep(POLICY_SECONDS)

    thread = threading.Thread(target=loop, name="darkmatter-network", daemon=True)
    thread.start()
    return thread


__all__ = ["NetworkNode", "classify_network", "decide", "get_mode", "is_password_protected",
           "read_state", "run_if_leader", "set_mode", "start_background"]
