"""
Install DarkMatter MCP entries into supported client configs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from darkmatter.store.local import atomic_write_text


DEFAULT_WAKE_TIMEOUT = 3600.0


@dataclass(frozen=True)
class InstallTarget:
    client: str
    label: str
    path: str
    format: str
    supported: bool = True


SUPPORTED_TARGETS: tuple[InstallTarget, ...] = (
    InstallTarget("claude-code", "Claude Code", "~/.claude.json", "mcpServers"),
    InstallTarget("cursor", "Cursor", "~/.cursor/mcp.json", "mcpServers"),
    InstallTarget("gemini", "Gemini CLI", "~/.gemini/settings.json", "mcpServers"),
    InstallTarget("codex", "Codex CLI", "~/.codex/config.toml", "codex_toml"),
    InstallTarget("kimi", "Kimi Code", "~/.kimi/mcp.json", "mcpServers"),
    InstallTarget("opencode", "OpenCode", "~/.config/opencode/opencode.json", "opencode"),
    InstallTarget(
        "openclaw",
        "OpenClaw",
        "",
        "none",
        supported=False,
    ),
)


def _expand(path: str, home: Path) -> Path:
    if path.startswith("~/"):
        return home / path[2:]
    return Path(path)


def _server_env(client: str, display_name: str) -> dict[str, str]:
    return {
        "DARKMATTER_CLIENT": client,
        "DARKMATTER_DISPLAY_NAME": display_name,
    }


def _stdio_entry(command: str, client: str, display_name: str) -> dict:
    return {
        "command": command,
        "args": ["-I", "-m", "darkmatter"],
        "env": _server_env(client, display_name),
    }


def _merge_json_config(path: Path, update_fn: Callable[[dict], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        with path.open() as f:
            config = json.load(f)
    else:
        config = {}
    update_fn(config)
    if path.exists():
        backup = path.with_name(path.name + ".darkmatter-backup")
        if not backup.exists():
            atomic_write_text(backup, path.read_text(), mode=0o600)
    atomic_write_text(path, json.dumps(config, indent=2) + "\n", mode=0o600)


def _install_cursor_collaboration_hooks(path: Path, command: str) -> None:
    args = ["-I", "-m", "darkmatter", "collaborate", "hook", "--client", "cursor"]
    shell_command = shlex.join([command, *args])

    def owned(handler):
        if not isinstance(handler, dict) or not isinstance(handler.get("command"), str):
            return False
        try:
            return shlex.split(handler["command"])[1:] == args
        except ValueError:
            return False

    def update(config):
        if config.get("version", 1) != 1:
            raise ValueError("Unsupported Cursor hooks version")
        config["version"] = 1
        hooks = config.setdefault("hooks", {})
        for event in ("sessionStart", "postToolUse", "sessionEnd"):
            handlers = hooks.setdefault(event, [])
            if not isinstance(handlers, list):
                raise ValueError(f"hooks.{event} must be an array")
            hooks[event] = [h for h in handlers if not owned(h)] + [
                {"command": shell_command, "timeout": 3 if event == "sessionEnd" else 10}]
    _merge_json_config(path, update)


def _install_collaboration_hooks(path: Path, command: str, client: str) -> None:
    # A single quoted command works with both clients' documented shell contract.
    shell_command = shlex.join([command, "-I", "-m", "darkmatter", "collaborate", "hook", "--client", client])

    def update(config):
        hooks = config.setdefault("hooks", {})
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "SessionEnd"):
            groups = hooks.setdefault(event, [])
            if not isinstance(groups, list):
                raise ValueError(f"hooks.{event} must be an array")
            kept = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    kept.append(group)
                    continue
                handlers = [h for h in group["hooks"] if not (
                    isinstance(h, dict) and h.get("statusMessage") == "DarkMatter local collaboration"
                )]
                if handlers:
                    kept.append({**group, "hooks": handlers})
            handler = {"type": "command", "command": shell_command,
                       "timeout": 3 if event == "SessionEnd" else 10,
                       "statusMessage": "DarkMatter local collaboration"}
            kept.append({"hooks": [handler]})
            hooks[event] = kept
    _merge_json_config(path, update)


def _is_darkmatter_wake_handler(handler: object) -> bool:
    if not isinstance(handler, dict):
        return False
    if (
        handler.get("type") == "mcp_tool"
        and handler.get("server") == "darkmatter"
        and handler.get("tool") == "darkmatter_stop_hook"
    ):
        return True
    args = handler.get("args")
    return (
        handler.get("type") == "command"
        and isinstance(args, list)
        and "darkmatter" in args
        and "wait-hook" in args
    )


def _replace_darkmatter_stop_hook(config: dict, handler: dict) -> None:
    hooks = config.setdefault("hooks", {})
    stop_groups = hooks.setdefault("Stop", [])
    if not isinstance(stop_groups, list):
        raise ValueError("hooks.Stop must be a JSON array")

    kept_groups = []
    for group in stop_groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept_groups.append(group)
            continue
        kept_handlers = [item for item in group["hooks"] if not _is_darkmatter_wake_handler(item)]
        if kept_handlers:
            updated = dict(group)
            updated["hooks"] = kept_handlers
            kept_groups.append(updated)
    kept_groups.append({"hooks": [handler]})
    hooks["Stop"] = kept_groups


def _install_claude_wake_hook(
    path: Path,
    command: str,
    timeout_seconds: float,
) -> None:
    handler = {
        "type": "command",
        "command": command,
        "args": [
            "-m",
            "darkmatter",
            "wait-hook",
            "--timeout-seconds",
            f"{timeout_seconds:g}",
        ],
        "asyncRewake": True,
        "timeout": timeout_seconds + 30,
        "statusMessage": "Waiting for DarkMatter mail",
    }
    _merge_json_config(path, lambda config: _replace_darkmatter_stop_hook(config, handler))


def _install_codex_wake_hook(path: Path, timeout_seconds: float) -> None:
    handler = {
        "type": "mcp_tool",
        "server": "darkmatter",
        "tool": "darkmatter_stop_hook",
        "input": {"timeout_seconds": timeout_seconds},
        "timeout": timeout_seconds + 30,
        "statusMessage": "Waiting for DarkMatter mail",
    }
    _merge_json_config(path, lambda config: _replace_darkmatter_stop_hook(config, handler))


def _install_mcp_servers_json(path: Path, command: str, client: str, display_name: str) -> None:
    entry = _stdio_entry(command, client, display_name)

    def update(config: dict) -> None:
        config.setdefault("mcpServers", {})
        config["mcpServers"]["darkmatter"] = entry

    _merge_json_config(path, update)


def _install_opencode(path: Path, command: str, client: str, display_name: str) -> None:
    env = _server_env(client, display_name)

    def update(config: dict) -> None:
        config.setdefault("mcp", {})
        config["mcp"]["darkmatter"] = {
            "type": "local",
            "enabled": True,
            "command": [command, "-I", "-m", "darkmatter"],
            "environment": env,
        }

    _merge_json_config(path, update)


def _strip_toml_sections(text: str, sections: set[str]) -> str:
    lines = text.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        match = re.match(r"^\[(.+)\]$", stripped)
        if match:
            section = match.group(1)
            skipping = section in sections
            if skipping:
                continue
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip()


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _install_codex_toml(path: Path, command: str, client: str, display_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    stripped = _strip_toml_sections(existing, {"mcp_servers.darkmatter", "mcp_servers.darkmatter.env"})
    env = _server_env(client, display_name)
    args = ', '.join(_toml_string(arg) for arg in ["-I", "-m", "darkmatter"])
    env_lines = "\n".join(f"{key} = {_toml_string(value)}" for key, value in env.items())
    block = (
        "[mcp_servers.darkmatter]\n"
        f"command = {_toml_string(command)}\n"
        f"args = [{args}]\n\n"
        "[mcp_servers.darkmatter.env]\n"
        f"{env_lines}\n"
    )
    content = f"{stripped}\n\n{block}" if stripped else block
    if path.exists():
        backup = path.with_name(path.name + ".darkmatter-backup")
        if not backup.exists():
            atomic_write_text(backup, existing, mode=0o600)
    atomic_write_text(path, content, mode=0o600)


def install_target(
    target: InstallTarget,
    *,
    command: str,
    display_name: str,
    home: Path,
    wake: bool = False,
    wake_timeout_seconds: float = DEFAULT_WAKE_TIMEOUT,
    collaborate: bool = False,
) -> tuple[bool, str]:
    if not target.supported:
        return False, f"{target.label}: skipped (no native MCP config to install)"

    path = _expand(target.path, home)
    try:
        if target.format == "mcpServers":
            _install_mcp_servers_json(path, command, target.client, display_name)
        elif target.format == "codex_toml":
            _install_codex_toml(path, command, target.client, display_name)
        elif target.format == "opencode":
            _install_opencode(path, command, target.client, display_name)
        else:
            return False, f"{target.label}: unsupported config format"
        wake_path = None
        if wake and target.client == "claude-code":
            wake_path = home / ".claude/settings.json"
            _install_claude_wake_hook(wake_path, command, wake_timeout_seconds)
        elif wake and target.client == "codex":
            wake_path = home / ".codex/hooks.json"
            _install_codex_wake_hook(wake_path, wake_timeout_seconds)
        if collaborate and target.client == "cursor":
            _install_cursor_collaboration_hooks(home / ".cursor/hooks.json", command)
        if collaborate and target.client in ("codex", "claude-code"):
            hook_path = home / (".codex/hooks.json" if target.client == "codex" else ".claude/settings.json")
            _install_collaboration_hooks(hook_path, command, target.client)
    except json.JSONDecodeError as exc:
        return False, f"{target.label}: invalid JSON in {path} ({exc})"
    except (OSError, ValueError) as exc:
        return False, f"{target.label}: failed to write {path} ({exc})"

    suffix = f" with wake hook at {wake_path}" if wake_path else ""
    return True, f"{target.label}: installed to {path}{suffix}"


def _target_by_client(client: str) -> InstallTarget:
    for target in SUPPORTED_TARGETS:
        if target.client == client:
            return target
    raise KeyError(client)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    client_names = [target.client for target in SUPPORTED_TARGETS]
    parser = argparse.ArgumentParser(
        prog="darkmatter install-mcp",
        description="Install DarkMatter into supported MCP client configs.",
    )
    parser.add_argument("--display-name", default=os.environ.get("DARKMATTER_DISPLAY_NAME", "darkmatter-agent"))
    parser.add_argument("--python", dest="python_cmd", default=sys.executable)
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--collaborate", action="store_true",
                        help="Install local session discovery and inbox notification hooks for Codex/Claude Code/Cursor.")
    parser.add_argument(
        "--wake",
        action="store_true",
        help="Install a Stop hook for Codex or Claude Code that waits for peer mail.",
    )
    parser.add_argument(
        "--wake-timeout",
        type=float,
        default=DEFAULT_WAKE_TIMEOUT,
        metavar="SECONDS",
        help=f"How long each wake waiter remains active (default: {DEFAULT_WAKE_TIMEOUT:g}).",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--client", action="append", dest="clients", choices=client_names)
    selection.add_argument("--all", action="store_true", help="Install into every supported native MCP client.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.wake_timeout <= 0:
        raise SystemExit("--wake-timeout must be greater than zero")
    home = Path(args.home).expanduser()

    if args.clients:
        targets = [_target_by_client(client) for client in args.clients]
    else:
        targets = [target for target in SUPPORTED_TARGETS if target.supported]

    installed = 0
    for target in targets:
        ok, message = install_target(
            target,
            command=args.python_cmd,
            display_name=args.display_name,
            home=home,
            wake=args.wake,
            wake_timeout_seconds=args.wake_timeout,
            collaborate=args.collaborate,
        )
        print(message)
        if ok:
            installed += 1

    skipped = [target.label for target in SUPPORTED_TARGETS if not target.supported]
    if skipped and not args.clients:
        print(f"Skipped: {', '.join(skipped)}")

    print(f"Installed DarkMatter MCP config for {installed} client(s).")
    print(
        "After an agent publishes a public repository, it can optionally connect "
        "to DarkMatter One, the public echo agent."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
