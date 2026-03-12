"""
Install DarkMatter MCP entries into supported client configs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


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


def _server_env(client: str, display_name: str, port: int) -> dict[str, str]:
    return {
        "DARKMATTER_CLIENT": client,
        "DARKMATTER_DISPLAY_NAME": display_name,
        "DARKMATTER_PORT": str(port),
    }


def _stdio_entry(command: str, client: str, display_name: str, port: int) -> dict:
    return {
        "command": command,
        "args": ["-m", "darkmatter"],
        "env": _server_env(client, display_name, port),
    }


def _merge_json_config(path: Path, update_fn: Callable[[dict], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        with path.open() as f:
            config = json.load(f)
    else:
        config = {}
    update_fn(config)
    with path.open("w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def _install_mcp_servers_json(path: Path, command: str, client: str, display_name: str, port: int) -> None:
    entry = _stdio_entry(command, client, display_name, port)

    def update(config: dict) -> None:
        config.setdefault("mcpServers", {})
        config["mcpServers"]["darkmatter"] = entry

    _merge_json_config(path, update)


def _install_opencode(path: Path, command: str, client: str, display_name: str, port: int) -> None:
    env = _server_env(client, display_name, port)

    def update(config: dict) -> None:
        config.setdefault("mcp", {})
        config["mcp"]["darkmatter"] = {
            "type": "local",
            "enabled": True,
            "command": [command, "-m", "darkmatter"],
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


def _install_codex_toml(path: Path, command: str, client: str, display_name: str, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    stripped = _strip_toml_sections(existing, {"mcp_servers.darkmatter", "mcp_servers.darkmatter.env"})
    env = _server_env(client, display_name, port)
    args = ', '.join(_toml_string(arg) for arg in ["-m", "darkmatter"])
    env_lines = "\n".join(f"{key} = {_toml_string(value)}" for key, value in env.items())
    block = (
        "[mcp_servers.darkmatter]\n"
        f"command = {_toml_string(command)}\n"
        f"args = [{args}]\n\n"
        "[mcp_servers.darkmatter.env]\n"
        f"{env_lines}\n"
    )
    content = f"{stripped}\n\n{block}" if stripped else block
    path.write_text(content)


def install_target(target: InstallTarget, *, command: str, display_name: str, port: int, home: Path) -> tuple[bool, str]:
    if not target.supported:
        return False, f"{target.label}: skipped (no native MCP config to install)"

    path = _expand(target.path, home)
    try:
        if target.format == "mcpServers":
            _install_mcp_servers_json(path, command, target.client, display_name, port)
        elif target.format == "codex_toml":
            _install_codex_toml(path, command, target.client, display_name, port)
        elif target.format == "opencode":
            _install_opencode(path, command, target.client, display_name, port)
        else:
            return False, f"{target.label}: unsupported config format"
    except json.JSONDecodeError as exc:
        return False, f"{target.label}: invalid JSON in {path} ({exc})"
    except OSError as exc:
        return False, f"{target.label}: failed to write {path} ({exc})"

    # Auto-install keep-alive hook if supported for this client
    if target.client in KEEPALIVE_TARGETS:
        install_keepalive(client=target.client, python_cmd=command)

    return True, f"{target.label}: installed to {path}"


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
    parser.add_argument("--port", type=int, default=int(os.environ.get("DARKMATTER_PORT", "8100")))
    parser.add_argument("--python", dest="python_cmd", default=sys.executable)
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--client", action="append", dest="clients", choices=client_names)
    parser.add_argument("--all", action="store_true", help="Install into every supported native MCP client.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
            port=args.port,
            home=home,
        )
        print(message)
        if ok:
            installed += 1

    skipped = [target.label for target in SUPPORTED_TARGETS if not target.supported]
    if skipped and not args.clients:
        print(f"Skipped: {', '.join(skipped)}")

    print(f"Installed DarkMatter MCP config for {installed} client(s).")
    return 0


# =============================================================================
# Keep-alive hook installation
# =============================================================================

# Map of client name → settings file path and hook format
KEEPALIVE_TARGETS: dict[str, dict] = {
    "claude-code": {
        "settings_path": "~/.claude/settings.json",
        "hook_event": "Stop",
    },
    "opencode": {
        "plugin_dir": "~/.config/opencode/plugins",
        "plugin_filename": "darkmatter-keepalive.js",
        "hook_event": "session.idle",
    },
    # Future:
    # "gemini": {"settings_path": "~/.gemini/settings.json", "hook_event": "AfterAgent"},
}


def _keepalive_hook_command(python_cmd: str) -> str:
    """Build the hook command string."""
    return f"{python_cmd} -m darkmatter.hooks.keepalive"


def install_keepalive(client: str = "claude-code", python_cmd: str = sys.executable) -> tuple[bool, str]:
    """Install the keep-alive Stop hook for a supported client."""
    target = KEEPALIVE_TARGETS.get(client)
    if not target:
        return False, f"Keep-alive hook not supported for {client}"

    home = Path.home()
    path = _expand(target["settings_path"], home)
    command = _keepalive_hook_command(python_cmd)

    if client == "claude-code":
        def update(config: dict) -> None:
            config.setdefault("hooks", {})
            hooks = config["hooks"]
            hook_entry = {
                "type": "command",
                "command": command,
                "timeout": 10,
            }

            # Check if we already installed a DarkMatter keep-alive hook
            stop_hooks = hooks.get("Stop", [])
            for rule in stop_hooks:
                for h in rule.get("hooks", []):
                    if "darkmatter.hooks.keepalive" in h.get("command", ""):
                        h["command"] = command  # Update command path
                        return

            # Add new hook rule
            stop_hooks.append({"hooks": [hook_entry]})
            hooks["Stop"] = stop_hooks

        try:
            _merge_json_config(path, update)
        except (json.JSONDecodeError, OSError) as exc:
            return False, f"Failed to install keep-alive hook: {exc}"

        return True, f"Keep-alive hook installed to {path}"

    if client == "opencode":
        import shutil
        plugin_dir = _expand(target["plugin_dir"], home)
        plugin_dir.mkdir(parents=True, exist_ok=True)
        dest = plugin_dir / target["plugin_filename"]
        src = Path(__file__).parent / "hooks" / "opencode_plugin.js"
        if not src.exists():
            return False, f"Plugin source not found: {src}"
        shutil.copy2(src, dest)
        return True, f"Keep-alive plugin installed to {dest}"

    return False, f"Keep-alive hook not yet implemented for {client}"


def uninstall_keepalive(client: str = "claude-code") -> tuple[bool, str]:
    """Remove the keep-alive Stop hook for a supported client."""
    target = KEEPALIVE_TARGETS.get(client)
    if not target:
        return False, f"Keep-alive hook not supported for {client}"

    home = Path.home()

    if client == "opencode":
        dest = _expand(target["plugin_dir"], home) / target["plugin_filename"]
        if dest.exists():
            dest.unlink()
            return True, f"Keep-alive plugin removed from {dest}"
        return True, "Plugin not found — nothing to uninstall"

    path = _expand(target["settings_path"], home)

    if not path.exists():
        return True, "No settings file found — nothing to uninstall"

    if client == "claude-code":
        def update(config: dict) -> None:
            hooks = config.get("hooks", {})
            stop_hooks = hooks.get("Stop", [])
            # Remove any rule containing a darkmatter keepalive hook
            hooks["Stop"] = [
                rule for rule in stop_hooks
                if not any(
                    "darkmatter.hooks.keepalive" in h.get("command", "")
                    for h in rule.get("hooks", [])
                )
            ]
            # Clean up empty lists
            if not hooks["Stop"]:
                del hooks["Stop"]
            if not hooks:
                del config["hooks"]

        try:
            _merge_json_config(path, update)
        except (json.JSONDecodeError, OSError) as exc:
            return False, f"Failed to uninstall keep-alive hook: {exc}"

        return True, f"Keep-alive hook removed from {path}"

    return False, f"Keep-alive hook not yet implemented for {client}"


if __name__ == "__main__":
    raise SystemExit(main())
