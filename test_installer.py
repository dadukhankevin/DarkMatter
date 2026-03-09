#!/usr/bin/env python3
"""
Installer tests for supported MCP client config formats.
"""

import json
import tempfile
from pathlib import Path

from darkmatter.installer import SUPPORTED_TARGETS, install_target


GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"

results: list[tuple[str, bool, str]] = []


def report(name: str, passed: bool, detail: str = "") -> None:
    mark = f"{GREEN}✓{RESET}" if passed else f"{RED}✗{RESET}"
    print(f"  {mark} {name}")
    if detail and not passed:
        print(f"      {detail}")
    results.append((name, passed, detail))


def _target(client: str):
    for candidate in SUPPORTED_TARGETS:
        if candidate.client == client:
            return candidate
    raise AssertionError(f"unknown client {client}")


def test_json_clients() -> None:
    print("\nTest: JSON MCP configs")
    with tempfile.TemporaryDirectory(prefix="dm_installer_") as tmp:
        home = Path(tmp)
        for client in ("claude-code", "cursor", "gemini", "kimi"):
            ok, message = install_target(
                _target(client),
                command="/tmp/python",
                display_name="mesh-node",
                port=8123,
                home=home,
            )
            report(f"{client} install succeeds", ok, message)
            path = home / _target(client).path[2:]
            data = json.loads(path.read_text())
            entry = data["mcpServers"]["darkmatter"]
            report(f"{client} command stored", entry["command"] == "/tmp/python", str(entry))
            report(f"{client} args stored", entry["args"] == ["-m", "darkmatter"], str(entry.get("args")))
            report(
                f"{client} env stores profile",
                entry["env"]["DARKMATTER_CLIENT"] == client,
                str(entry["env"]),
            )


def test_codex_toml() -> None:
    print("\nTest: Codex TOML config")
    with tempfile.TemporaryDirectory(prefix="dm_installer_") as tmp:
        home = Path(tmp)
        codex_path = home / ".codex/config.toml"
        codex_path.parent.mkdir(parents=True, exist_ok=True)
        codex_path.write_text(
            'model = "gpt-5.4"\n'
            '[projects."/tmp/example"]\n'
            'trust_level = "trusted"\n'
        )
        ok, message = install_target(
            _target("codex"),
            command="/tmp/python",
            display_name="mesh-node",
            port=8123,
            home=home,
        )
        report("codex install succeeds", ok, message)
        text = codex_path.read_text()
        report("preserves existing config", 'model = "gpt-5.4"' in text, text)
        report("adds darkmatter section", "[mcp_servers.darkmatter]" in text, text)
        report("adds codex client env", 'DARKMATTER_CLIENT = "codex"' in text, text)


def test_opencode_json() -> None:
    print("\nTest: OpenCode config")
    with tempfile.TemporaryDirectory(prefix="dm_installer_") as tmp:
        home = Path(tmp)
        ok, message = install_target(
            _target("opencode"),
            command="/tmp/python",
            display_name="mesh-node",
            port=8123,
            home=home,
        )
        report("opencode install succeeds", ok, message)
        path = home / ".config/opencode/opencode.json"
        data = json.loads(path.read_text())
        entry = data["mcp"]["darkmatter"]
        report("opencode entry enabled", entry["enabled"] is True, str(entry))
        report("opencode local command array", entry["command"] == ["/tmp/python", "-m", "darkmatter"], str(entry))


def test_openclaw_skipped() -> None:
    print("\nTest: OpenClaw skip")
    with tempfile.TemporaryDirectory(prefix="dm_installer_") as tmp:
        ok, message = install_target(
            _target("openclaw"),
            command="/tmp/python",
            display_name="mesh-node",
            port=8123,
            home=Path(tmp),
        )
        report("openclaw is skipped", not ok and "skipped" in message, message)


def main() -> int:
    test_json_clients()
    test_codex_toml()
    test_opencode_json()
    test_openclaw_skipped()
    failed = [name for name, passed, _ in results if not passed]
    print(f"\nPassed {len(results) - len(failed)}/{len(results)} checks")
    if failed:
        print("Failed:")
        for name in failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
