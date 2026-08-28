"""Fetch-only git smart HTTP for one bare mailbox repo."""

from __future__ import annotations

import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _cgi_split(raw: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    header, _, body = raw.partition(b"\r\n\r\n")
    if header == raw:
        header, _, body = raw.partition(b"\n\n")
    status = 200
    headers = []
    for line in header.decode("iso-8859-1").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() == "status":
            status = int(value.split()[0])
        else:
            headers.append((key.strip(), value.strip()))
    return status, headers, body


class GitHTTPServer:
    def __init__(self, bare: Path, host: str = "0.0.0.0", port: int = 8741):
        self.bare = Path(bare)
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self.host in ("127.0.0.1", "localhost"):
            host = "127.0.0.1"
        elif self.host in ("0.0.0.0", "::"):
            host = lan_ip()
        else:
            host = self.host
        return f"http://{host}:{self.port}/mailbox.git"

    def start(self) -> "GitHTTPServer":
        if self._httpd:
            return self
        bare_parent = str(self.bare.resolve().parent)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                self._cgi()

            def do_POST(self):
                self._cgi()

            def _cgi(self):
                parsed = urlparse(self.path)
                path = parsed.path
                if path != "/mailbox.git" and not path.startswith("/mailbox.git/"):
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self.send_error(400)
                    return
                if length < 0:
                    self.send_error(400)
                    return
                if length > 16 * 1024 * 1024:
                    self.send_error(413)
                    return
                env = os.environ.copy()
                env.update({
                    "GIT_PROJECT_ROOT": bare_parent,
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": path,
                    "QUERY_STRING": parsed.query,
                    "REQUEST_METHOD": self.command,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                    "REMOTE_ADDR": self.client_address[0],
                })
                proc = subprocess.run(
                    ["git", "http-backend"],
                    input=self.rfile.read(length) if length else b"",
                    capture_output=True,
                    env=env,
                )
                status, headers, body = _cgi_split(proc.stdout)
                if proc.returncode != 0 and not proc.stdout:
                    self.send_error(500, proc.stderr.decode("utf-8", "replace")[:200])
                    return
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        try:
            httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError:
            httpd = ThreadingHTTPServer((self.host, 0), Handler)
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if not self._httpd:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2)
        self._httpd = None
        self._thread = None
