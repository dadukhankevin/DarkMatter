#!/usr/bin/env python3
"""
Installer tests for supported MCP client config formats.
"""

import json
import tempfile
from pathlib import Path

import pytest

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
    assert passed, f"{name}: {detail}"


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
                display_name="mail-agent",
                home=home,
            )
            report(f"{client} install succeeds", ok, message)
            path = home / _target(client).path[2:]
            data = json.loads(path.read_text())
            entry = data["mcpServers"]["darkmatter"]
            report(f"{client} command stored", entry["command"] == "/tmp/python", str(entry))
            report(f"{client} args stored", entry["args"] == ["-I", "-m", "darkmatter"], str(entry.get("args")))
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
            display_name="mail-agent",
            home=home,
        )
        report("codex install succeeds", ok, message)
        text = codex_path.read_text()
        report("preserves existing config", 'model = "gpt-5.4"' in text, text)
        report("adds darkmatter section", "[mcp_servers.darkmatter]" in text, text)
        report("adds codex client env", 'DARKMATTER_CLIENT = "codex"' in text, text)


def test_wake_hooks() -> None:
    print("\nTest: editable wake hooks")
    with tempfile.TemporaryDirectory(prefix="dm_installer_") as tmp:
        home = Path(tmp)
        codex_hooks = home / ".codex/hooks.json"
        codex_hooks.parent.mkdir(parents=True, exist_ok=True)
        codex_hooks.write_text(json.dumps({
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "keep-me"},
                                    {"type": "mcp_tool", "server": "darkmatter",
                                     "tool": "darkmatter_stop_hook"}]}],
            },
        }))
        ok, message = install_target(
            _target("codex"),
            command="/tmp/python",
            display_name="mail-agent",
            home=home,
            wake=True,
            wake_timeout_seconds=45,
        )
        report("codex wake install succeeds", ok, message)
        data = json.loads(codex_hooks.read_text())
        handlers = [handler for group in data["hooks"]["Stop"] for handler in group["hooks"]]
        wake = [handler for handler in handlers if handler.get("statusMessage") == "Waiting for DarkMatter mail"]
        report("codex preserves other Stop hooks", any(h.get("command") == "keep-me" for h in handlers), str(data))
        report("codex adds command Stop hook", len(wake) == 1, str(data))
        assert all(h["type"] != "mcp_tool" for h in handlers)
        assert wake[0]["type"] == "command"
        assert "-I -m darkmatter wait-hook --client codex --timeout-seconds 45" in wake[0]["command"]

        install_target(
            _target("codex"),
            command="/tmp/python",
            display_name="mail-agent",
            home=home,
            wake=True,
            wake_timeout_seconds=60,
        )
        data = json.loads(codex_hooks.read_text())
        handlers = [handler for group in data["hooks"]["Stop"] for handler in group["hooks"]]
        wake = [handler for handler in handlers if handler.get("statusMessage") == "Waiting for DarkMatter mail"]
        report("codex wake install is idempotent", len(wake) == 1, str(data))
        report("codex wake install updates timeout", wake[0]["command"].endswith("--timeout-seconds 60"), str(wake))

        ok, message = install_target(
            _target("claude-code"),
            command="/tmp/python",
            display_name="mail-agent",
            home=home,
            wake=True,
            wake_timeout_seconds=45,
        )
        report("claude wake install succeeds", ok, message)
        data = json.loads((home / ".claude/settings.json").read_text())
        handler = data["hooks"]["Stop"][-1]["hooks"][0]
        report("claude uses asyncRewake", handler["asyncRewake"] is True, str(handler))
        report("claude runs editable wait-hook args", handler["args"][-2:] == ["--timeout-seconds", "45"], str(handler))


def test_opencode_json() -> None:
    print("\nTest: OpenCode config")
    with tempfile.TemporaryDirectory(prefix="dm_installer_") as tmp:
        home = Path(tmp)
        ok, message = install_target(
            _target("opencode"),
            command="/tmp/python",
            display_name="mail-agent",
            home=home,
        )
        report("opencode install succeeds", ok, message)
        path = home / ".config/opencode/opencode.json"
        data = json.loads(path.read_text())
        entry = data["mcp"]["darkmatter"]
        report("opencode entry enabled", entry["enabled"] is True, str(entry))
        report("opencode local command array", entry["command"] == ["/tmp/python", "-I", "-m", "darkmatter"], str(entry))


@pytest.mark.parametrize("client", ["codex", "claude-code"])
@pytest.mark.parametrize("wait, expected", [(None, None), (45.0, 75), (0.25, 31)])
def test_host_timeout_is_integer(tmp_path, client, wait, expected):
    if expected is None:  # Claude listens for a day in the background; Codex blocks, so an hour.
        expected = 86430 if client == "claude-code" else 3630
    kwargs = {} if wait is None else {"wake_timeout_seconds": wait}
    for _ in range(2):
        ok, message = install_target(_target(client), command="/tmp/python",
                                     display_name="test", home=tmp_path, wake=True, **kwargs)
        assert ok, message
    path = tmp_path / (".codex/hooks.json" if client == "codex" else ".claude/settings.json")
    groups = json.loads(path.read_text())["hooks"]["Stop"]
    assert len(groups) == 1
    timeout = groups[0]["hooks"][0]["timeout"]
    assert type(timeout) is int  # 3630.0 == 3630, but the host rejects the float.
    assert timeout == expected


@pytest.mark.parametrize("wait", [0, -1, 7 * 86400 + 0.01, float("nan"), float("inf"), -float("inf")])
def test_invalid_wait_does_not_change_configs(tmp_path, wait):
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('# existing settings\n')
    ok, message = install_target(_target("codex"), command="/tmp/python",
                                 display_name="test", home=tmp_path, wake=True,
                                 wake_timeout_seconds=wait)
    assert not ok
    assert "wake timeout" in message
    assert config.read_text() == '# existing settings\n'
    assert not (config.parent / "hooks.json").exists()


def test_openclaw_skipped() -> None:
    print("\nTest: OpenClaw skip")
    with tempfile.TemporaryDirectory(prefix="dm_installer_") as tmp:
        ok, message = install_target(
            _target("openclaw"),
            command="/tmp/python",
            display_name="mail-agent",
            home=Path(tmp),
        )
        report("openclaw is skipped", not ok and "skipped" in message, message)


def main() -> int:
    test_json_clients()
    test_codex_toml()
    test_wake_hooks()
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


def test_hand_edited_wake_hook_is_replaced_not_duplicated(tmp_path):
    path = tmp_path / ".codex/hooks.json"
    path.parent.mkdir(parents=True)
    edited = "'/my python' -I -m darkmatter wait-hook --timeout-seconds 30 --client codex"
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": edited},
        {"type": "command", "command": "python -m other wait-hook"},
    ]}]}}))
    assert install_target(_target("codex"), command="/tmp/python", display_name="t", home=tmp_path, wake=True)[0]
    handlers = [h for g in json.loads(path.read_text())["hooks"]["Stop"] for h in g["hooks"]]
    commands = [h["command"] for h in handlers]
    assert edited not in commands
    assert "python -m other wait-hook" in commands
    assert sum("darkmatter wait-hook" in c for c in commands) == 1


def _stop_handlers(path):
    if not path.is_file():
        return []
    return [h for g in json.loads(path.read_text()).get("hooks", {}).get("Stop", []) for h in g["hooks"]]


def test_wake_is_default_for_claude_and_announced(tmp_path, capsys):
    from darkmatter.installer import main
    claude, codex = tmp_path / ".claude/settings.json", tmp_path / ".codex/hooks.json"
    assert main(["--home", str(tmp_path), "--python", "/tmp/python",
                 "--client", "claude-code", "--client", "codex"]) == 0
    out = capsys.readouterr().out
    assert len(_stop_handlers(claude)) == 1 and _stop_handlers(claude)[0]["asyncRewake"] is True
    assert _stop_handlers(codex) == []  # Codex Stop hooks block, so wake stays opt-in there.
    assert "Wake-ups are ON by default for Claude Code" in out
    assert "--no-wake" in out and "uses tokens" in out
    assert "Codex wake-ups are opt-in" in out


def test_explicit_wake_is_not_reannounced_and_no_wake_removes_only_ours(tmp_path, capsys):
    from darkmatter.installer import main
    claude = tmp_path / ".claude/settings.json"
    claude.parent.mkdir(parents=True)
    claude.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep-me"}]}]}}))
    args = ["--home", str(tmp_path), "--python", "/tmp/python", "--client", "claude-code"]
    assert main([*args, "--wake"]) == 0
    assert "ON by default" not in capsys.readouterr().out
    assert len(_stop_handlers(claude)) == 2
    assert main([*args, "--no-wake"]) == 0
    assert "wake hook removed" in capsys.readouterr().out
    assert [h["command"] for h in _stop_handlers(claude)] == ["keep-me"]
    assert main(args) == 0  # A later default install turns it back on and says so.
    assert "ON by default" in capsys.readouterr().out
    assert len(_stop_handlers(claude)) == 2


def test_default_install_keeps_an_opted_in_codex_wake(tmp_path):
    from darkmatter.installer import main
    codex = tmp_path / ".codex/hooks.json"
    args = ["--home", str(tmp_path), "--python", "/tmp/python", "--client", "codex"]
    assert main([*args, "--wake"]) == 0
    assert main(args) == 0
    assert len(_stop_handlers(codex)) == 1
