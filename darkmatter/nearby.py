"""Passive same-host and LAN discovery of signed contact cards."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from darkmatter.contract.contact import verify_contact_card
from darkmatter.store.local import atomic_write_text


PROTOCOL = "darkmatter.nearby.v3"
DEFAULT_GROUP = "239.255.42.99"
DEFAULT_PORT = 8742
MAX_PACKET = 16 * 1024


def _discovery_port() -> int:
    try:
        return int(os.environ.get("DARKMATTER_NEARBY_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def _registry_dir() -> Path:
    configured = os.environ.get("DARKMATTER_NEARBY_DIR", "").strip()
    if configured:
        return Path(configured)
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"darkmatter-nearby-{uid}"


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


class LocalNearbyRegistry:
    """Per-user presence records for agents on the same machine."""

    def __init__(self, agent_id: str, directory: Optional[Path] = None):
        self.agent_id = agent_id
        self.directory = directory or _registry_dir()
        self.path = self.directory / f"{agent_id}.json"

    def register(self, card: dict) -> None:
        verified = verify_contact_card(card)
        if verified["agent_id"] != self.agent_id:
            raise ValueError("nearby card does not match this agent")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        atomic_write_text(self.path, json.dumps({
            "protocol": PROTOCOL,
            "pid": os.getpid(),
            "updated_at": time.time(),
            "card": verified,
        }, sort_keys=True) + "\n", mode=0o600)

    def unregister(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            if data.get("pid") == os.getpid():
                self.path.unlink()
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def discover(self) -> list[dict]:
        if not self.directory.is_dir():
            return []
        found = []
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                pid = int(data.get("pid"))
                if data.get("protocol") != PROTOCOL or not _pid_alive(pid):
                    path.unlink(missing_ok=True)
                    continue
                card = verify_contact_card(data.get("card"))
                if card["agent_id"] == self.agent_id:
                    continue
                found.append({"card": card, "scope": "local"})
            except (ValueError, TypeError, json.JSONDecodeError, OSError):
                continue
        return found


class LANNearbyResponder:
    """Answer one-hop UDP probes with the current signed LAN contact card."""

    def __init__(
        self,
        card_factory: Callable[[], dict],
        *,
        group: str = DEFAULT_GROUP,
        port: Optional[int] = None,
    ):
        self.card_factory = card_factory
        self.group = group
        self.port = _discovery_port() if port is None else int(port)
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> "LANNearbyResponder":
        if self._socket is not None:
            return self
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", self.port))
        membership = socket.inet_aton(self.group) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.settimeout(0.25)
        self._socket = sock
        self.port = sock.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                raw, address = self._socket.recvfrom(MAX_PACKET)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                probe = json.loads(raw.decode("utf-8"))
                nonce = probe.get("nonce")
                if (
                    probe.get("protocol") != PROTOCOL
                    or probe.get("type") != "probe"
                    or not isinstance(nonce, str)
                    or len(nonce) > 64
                ):
                    continue
                card = verify_contact_card(self.card_factory())
                response = json.dumps({
                    "protocol": PROTOCOL,
                    "type": "contact",
                    "nonce": nonce,
                    "card": card,
                }, separators=(",", ":")).encode("utf-8")
                if len(response) <= MAX_PACKET:
                    self._socket.sendto(response, address)
            except (ValueError, TypeError, json.JSONDecodeError, OSError):
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._socket = None
        self._thread = None


def discover_lan(
    agent_id: str,
    timeout_seconds: float = 1.0,
    *,
    group: str = DEFAULT_GROUP,
    port: Optional[int] = None,
) -> list[dict]:
    """Probe the local multicast domain and return verified contact cards."""
    timeout_seconds = max(0.0, min(float(timeout_seconds), 5.0))
    if timeout_seconds <= 0:
        return []
    port = _discovery_port() if port is None else int(port)
    nonce = uuid.uuid4().hex
    probe = json.dumps({
        "protocol": PROTOCOL,
        "type": "probe",
        "nonce": nonce,
    }, separators=(",", ":")).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.settimeout(min(0.2, timeout_seconds))
    sock.bind(("", 0))
    for target in ((group, port), ("127.0.0.1", port)):
        try:
            sock.sendto(probe, target)
        except OSError:
            pass
    deadline = time.monotonic() + timeout_seconds
    found: dict[str, dict] = {}
    try:
        while time.monotonic() < deadline:
            sock.settimeout(max(0.01, min(0.2, deadline - time.monotonic())))
            try:
                raw, _ = sock.recvfrom(MAX_PACKET)
            except socket.timeout:
                continue
            try:
                response = json.loads(raw.decode("utf-8"))
                if (
                    response.get("protocol") != PROTOCOL
                    or response.get("type") != "contact"
                    or response.get("nonce") != nonce
                ):
                    continue
                card = verify_contact_card(response.get("card"))
                if card["agent_id"] != agent_id:
                    found[card["agent_id"]] = {"card": card, "scope": "lan"}
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
                continue
    finally:
        sock.close()
    return list(found.values())


__all__ = [
    "DEFAULT_GROUP",
    "DEFAULT_PORT",
    "LANNearbyResponder",
    "LocalNearbyRegistry",
    "PROTOCOL",
    "discover_lan",
]
