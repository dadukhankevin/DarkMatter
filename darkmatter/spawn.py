"""
Agent auto-spawn system — SpawnedAgent, spawn/kill/cleanup, prompt building.

Depends on: config, models
"""

import asyncio
import os
import sys
import time
from dataclasses import dataclass

import darkmatter.config as _cfg
from darkmatter.config import (
    AGENT_SPAWN_ENABLED,
    AGENT_SPAWN_MAX_CONCURRENT,
    AGENT_SPAWN_MAX_PER_HOUR,
    AGENT_SPAWN_TIMEOUT,
    ACTIVE_CLIENT,
)
from darkmatter.models import AgentState, QueuedMessage

# Capture the project directory at import time so spawned agents run here,
# not in a temporary directory.
_PROJECT_DIR = os.getcwd()


# =============================================================================
# Spawn Tracking (ephemeral, not persisted)
# =============================================================================

@dataclass
class SpawnedAgent:
    process: asyncio.subprocess.Process
    message_id: str
    spawned_at: float
    pid: int
    spawn_mcp_config: str = ""  # .mcp.json path to clean up when agent exits
    # Stdout streaming: dict of message_id → async callable(chunk: str)
    # Multiple callbacks = nested messages, chunks go to all active targets
    stdout_callbacks: dict = None  # Initialized to {} in __post_init__

    def __post_init__(self):
        if self.stdout_callbacks is None:
            self.stdout_callbacks = {}


_spawned_agents: list[SpawnedAgent] = []
_spawn_timestamps: list[float] = []


def get_spawned_agents() -> list[SpawnedAgent]:
    """Get the list of spawned agents."""
    return _spawned_agents


# =============================================================================
# Checks
# =============================================================================

def can_spawn_agent() -> tuple[bool, str]:
    """Check whether we can spawn a new agent subprocess."""
    import darkmatter.config as _cfg
    if not _cfg.AGENT_SPAWN_ENABLED:
        return False, "Agent spawning is disabled (DARKMATTER_AGENT_ENABLED=false)"

    cleanup_finished_agents()

    active = len(_spawned_agents)
    if active >= AGENT_SPAWN_MAX_CONCURRENT:
        return False, f"Concurrency limit reached ({active}/{AGENT_SPAWN_MAX_CONCURRENT})"

    now = time.monotonic()
    cutoff = now - 3600
    while _spawn_timestamps and _spawn_timestamps[0] < cutoff:
        _spawn_timestamps.pop(0)
    if len(_spawn_timestamps) >= AGENT_SPAWN_MAX_PER_HOUR:
        return False, f"Hourly rate limit reached ({len(_spawn_timestamps)}/{AGENT_SPAWN_MAX_PER_HOUR})"

    return True, ""


# =============================================================================
# Running / Kill / Cleanup
# =============================================================================

def is_agent_running(agent: SpawnedAgent) -> bool:
    """Check if a spawned agent is still running.

    For asyncio.Process, returncode stays None until the process is reaped.
    We check liveness via os.kill(pid, 0) and try os.waitpid(WNOHANG) to
    reap zombies so the slot is freed for new spawns.
    """
    if agent.process.returncode is not None:
        return False
    # Check if process is still alive
    try:
        os.kill(agent.pid, 0)
    except ProcessLookupError:
        # Process is gone
        agent.process.returncode = -1
        return False
    except PermissionError:
        # Process exists but we can't signal it — treat as alive
        return True
    # Process exists — try to reap if it's a zombie
    try:
        pid, status = os.waitpid(agent.pid, os.WNOHANG)
        if pid != 0:
            # Zombie reaped
            if os.WIFEXITED(status):
                agent.process.returncode = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                agent.process.returncode = -os.WTERMSIG(status)
            else:
                agent.process.returncode = -1
            return False
    except ChildProcessError:
        agent.process.returncode = -1
        return False
    except Exception:
        pass
    return True


def kill_agent(agent: SpawnedAgent, force: bool = False) -> None:
    """Send terminate/kill signal to a spawned agent."""
    if force:
        agent.process.kill()
    else:
        agent.process.terminate()


def cleanup_finished_agents() -> None:
    """Remove finished agent processes from the tracking list."""
    still_running = []
    for agent in _spawned_agents:
        if not is_agent_running(agent):
            print(
                f"[DarkMatter] Spawned agent PID {agent.pid} exited "
                f"(code={agent.process.returncode}, msg={agent.message_id[:12]}...)",
                file=sys.stderr,
            )
            if agent.spawn_mcp_config:
                try:
                    os.remove(agent.spawn_mcp_config)
                except Exception as e:
                    print(f"[DarkMatter] Failed to clean up spawn config {agent.spawn_mcp_config}: {e}", file=sys.stderr)
        else:
            still_running.append(agent)
    _spawned_agents.clear()
    _spawned_agents.extend(still_running)


# =============================================================================
# Prompt Building
# =============================================================================

def build_agent_prompt(state: AgentState, msg: QueuedMessage) -> str:
    """Build the prompt for a spawned agent with conversation context."""
    from darkmatter.context import build_context_feed, format_feed_for_prompt
    feed = build_context_feed(state, responding_to=msg.message_id)
    context = format_feed_for_prompt(feed, state)

    meta = msg.metadata or {}
    if meta.get("type") == "connection_request":
        request_id = meta.get("request_id", msg.message_id)
        return f"""\
CONNECTION REQUEST — Act now.

{context}

Request ID: {request_id}
Message: {msg.content}

Accept: darkmatter_connection(action="accept", request_id="{request_id}")
Reject: darkmatter_connection(action="reject", request_id="{request_id}")

After accepting, introduce yourself and share what you can help with.
"""

    return f"""\
INCOMING MESSAGE — Act now. Be proactive: reply, forward, or both.

{context}

Read message {msg.message_id} with darkmatter_inbox(message_id="{msg.message_id}"), then:
1. Call begin_message(target_agent_id=..., in_reply_to="{msg.message_id}") FIRST. This starts streaming your stdout to the receiver in real time.
2. Write your response naturally — everything you output between begin and end streams live to the receiver.
3. Call end_message(message_id=...) when done. This just signals "stop streaming" — nothing else needed.
4. If this message is better suited for another peer, forward it with darkmatter_send_message(message_id="{msg.message_id}", target_agent_id=...).
"""


# =============================================================================
# Spawn
# =============================================================================

async def spawn_agent_for_message(state: AgentState, msg: QueuedMessage,
                                   save_state_fn=None) -> None:
    """Spawn an agent subprocess to handle an incoming message."""
    print(f"[DarkMatter] DEBUG: spawn_agent_for_message called for {msg.message_id[:12]}...", file=sys.stderr)
    ok, reason = can_spawn_agent()
    if not ok:
        print(f"[DarkMatter] Not spawning agent: {reason}", file=sys.stderr)
        return

    for agent in _spawned_agents:
        if agent.message_id == msg.message_id:
            print(f"[DarkMatter] Agent already spawned for message {msg.message_id[:12]}...", file=sys.stderr)
            return

    if save_state_fn:
        save_state_fn()

    prompt = build_agent_prompt(state, msg)

    env = os.environ.copy()
    env["DARKMATTER_AGENT_ENABLED"] = "false"
    env["DARKMATTER_ENTRYPOINT_AUTOSTART"] = "false"
    for var in ACTIVE_CLIENT["env_cleanup"]:
        env.pop(var, None)

    # Write spawn MCP config to a TEMP FILE — never overwrite the project's
    # .mcp.json. Previous approach backed up and restored the project config,
    # but if the process was killed or the server crashed, the backup was never
    # restored, leaving the primary session with a broken HTTP config.
    spawn_dir = _PROJECT_DIR
    import json as _json
    import tempfile as _tempfile
    mcp_config = {
        "mcpServers": {
            "darkmatter": {
                "type": "http",
                "url": f"http://127.0.0.1:{state.port}/mcp",
            }
        }
    }
    # Write to a temp file that gets cleaned up when the agent exits
    spawn_mcp_fd, spawn_mcp_path = _tempfile.mkstemp(
        prefix="darkmatter-spawn-mcp-", suffix=".json"
    )
    with os.fdopen(spawn_mcp_fd, "w") as f:
        _json.dump(mcp_config, f)

    command = ACTIVE_CLIENT["command"]
    args = list(ACTIVE_CLIENT["args"])

    # Pass the temp MCP config via CLI flags instead of overwriting project config.
    mcp_via_cli = "mcp_stdio" in ACTIVE_CLIENT.get("capabilities", set())
    if mcp_via_cli:
        args.extend(["--mcp-config", spawn_mcp_path, "--strict-mcp-config"])
    prompt_style = ACTIVE_CLIENT.get("prompt_style", "positional")

    # Sanitize prompt for shell safety — remove chars that break quoting
    safe_prompt = prompt.replace('"', "'").replace('`', "'").replace('$', '')

    if prompt_style == "positional":
        args.append(safe_prompt)
    elif prompt_style.startswith("flag:"):
        flag_name = prompt_style.split(":", 1)[1]
        args.extend([f"--{flag_name}", safe_prompt])
    elif prompt_style == "stdin":
        stdin_pipe = asyncio.subprocess.PIPE
    else:
        args.append(safe_prompt)

    # Optionally wrap in OS-native sandbox
    exec_argv = [command] + args
    sandboxed = False
    if _cfg.AGENT_SANDBOX:
        from darkmatter.sandbox import build_sandbox_command
        sandbox_argv = build_sandbox_command(
            command=exec_argv,
            writable_roots=[spawn_dir],
            network=_cfg.AGENT_SANDBOX_NETWORK,
        )
        if sandbox_argv is not None:
            exec_argv = sandbox_argv
            sandboxed = True
        else:
            print("[DarkMatter] Sandbox unavailable, spawning without sandbox", file=sys.stderr)

    try:
        # PTY gives us a headless terminal — output streams in real time.
        import pty as _pty
        import fcntl, struct, termios
        pty_master_fd, pty_slave_fd = _pty.openpty()
        # Set PTY size to match the xterm.js widget on the frontend
        winsize = struct.pack("HHHH", 24, 120, 0, 0)  # rows=24, cols=120
        fcntl.ioctl(pty_slave_fd, termios.TIOCSWINSZ, winsize)

        process = await asyncio.create_subprocess_exec(
            *exec_argv,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=pty_slave_fd,
            stderr=asyncio.subprocess.PIPE,
            cwd=spawn_dir,
        )
        os.close(pty_slave_fd)

        # Wrap PTY master fd in an asyncio-friendly stream reader
        loop = asyncio.get_event_loop()
        pty_reader = asyncio.StreamReader()
        read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(pty_reader),
            os.fdopen(pty_master_fd, "rb", 0),
        )

        agent = SpawnedAgent(
            process=process,
            message_id=msg.message_id,
            spawned_at=time.monotonic(),
            pid=process.pid,
            spawn_mcp_config=spawn_mcp_path,
        )
        # Attach PTY reader so _drain_stdout can use it instead of process.stdout
        agent._pty_reader = pty_reader
        agent._pty_transport = read_transport
        _spawned_agents.append(agent)
        _spawn_timestamps.append(time.monotonic())
        sandbox_label = " [sandboxed]" if sandboxed else ""
        print(
            f"[DarkMatter] Spawned agent PID {process.pid}{sandbox_label} for message "
            f"{msg.message_id[:12]}... from {msg.from_agent_id or 'unknown'}",
            file=sys.stderr,
        )

        asyncio.create_task(agent_timeout_watchdog(agent))
        asyncio.create_task(_drain_stdout(agent))
        asyncio.create_task(_reap_agent_when_done(agent))

    except FileNotFoundError:
        print(
            f"[DarkMatter] Agent spawn failed: command '{command}' not found. "
            f"Set DARKMATTER_CLIENT to a valid profile or DARKMATTER_AGENT_COMMAND to the correct path.",
            file=sys.stderr,
        )
        try: os.remove(spawn_mcp_path)
        except OSError: pass
    except Exception as e:
        print(f"[DarkMatter] Agent spawn failed: {e}", file=sys.stderr)
        try: os.remove(spawn_mcp_path)
        except OSError: pass


async def _drain_stdout(agent: SpawnedAgent) -> None:
    """Read PTY output, extract only default-colored text (the actual response).

    Claude Code's TUI renders chrome (spinners, status bar, tool calls) with
    explicit ANSI foreground colors. The actual response text uses the default
    foreground (no explicit color). We parse the ANSI stream, track color state,
    and only pass through text in the default color.
    """
    reader = getattr(agent, "_pty_reader", None) or agent.process.stdout
    if not reader:
        return
    colored = False  # True when an explicit foreground color is active
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            if not agent.stdout_callbacks:
                continue
            raw = chunk.decode("utf-8", errors="replace")
            result = []
            i = 0
            n = len(raw)
            while i < n:
                if raw[i] == '\x1b':
                    if i + 1 < n and raw[i + 1] == '[':
                        # CSI sequence: \x1b[ ... <letter>
                        j = i + 2
                        while j < n and (raw[j].isdigit() or raw[j] in ';?'):
                            j += 1
                        if j < n:
                            params_str = raw[i + 2:j]
                            cmd = raw[j]
                            if cmd == 'm':  # SGR — color/style
                                parts = params_str.split(';') if params_str else ['0']
                                for p in parts:
                                    try:
                                        code = int(p)
                                    except ValueError:
                                        continue
                                    if code == 0 or code == 39:
                                        colored = False
                                    elif (30 <= code <= 37) or (90 <= code <= 97) or code == 38:
                                        colored = True
                            elif cmd == 'C' and not colored:
                                # Cursor forward → insert spaces
                                count = int(params_str) if params_str else 1
                                result.append(' ' * count)
                            elif cmd in ('B', 'E') and not colored:
                                # Cursor down / next line → newline
                                count = int(params_str) if params_str else 1
                                result.append('\n' * count)
                            i = j + 1
                        else:
                            i = j
                    elif i + 1 < n and raw[i + 1] == ']':
                        # OSC sequence — skip until BEL
                        j = i + 2
                        while j < n and raw[j] != '\x07':
                            j += 1
                        i = j + 1 if j < n else j
                    else:
                        i += 2
                elif raw[i] == '\r':
                    if i + 1 < n and raw[i + 1] == '\n':
                        if not colored:
                            result.append('\n')
                        i += 2
                    else:
                        i += 1
                else:
                    if not colored:
                        result.append(raw[i])
                    i += 1
            text = ''.join(result)
            # Filter out tool call chrome that leaks through in default color
            if text:
                import re as _re2
                text = _re2.sub(
                    r'^.*(?:darkmatter\s*-\s*(?:Begin|Complete)\s+.*\(MCP\).*|'
                    r'\(params:\s*\{.*?\}\).*|'
                    r'⏺|⎿|ctrl\+o to expand|'
                    r'"result"\s*:|"success"\s*:|"message_id"\s*:|"signaled"\s*:|"targets"\s*:).*$',
                    '', text, flags=_re2.MULTILINE
                )
                # Filter lines with long hex strings (agent IDs from tool results)
                text = _re2.sub(r'^.*[0-9a-f]{20,}.*$', '', text, flags=_re2.MULTILINE)
                # Remove lines that are just JSON fragments: { } ( ) [ ]
                text = _re2.sub(r'^\s*[{}\[\]()]+\s*$', '', text, flags=_re2.MULTILINE)
                # Collapse excessive blank lines
                text = _re2.sub(r'\n{3,}', '\n\n', text).strip()
                if text:
                    text += '\n'
            if text:
                for cb in list(agent.stdout_callbacks.values()):
                    try:
                        await cb(text)
                    except Exception:
                        pass
    except Exception:
        pass
    finally:
        transport = getattr(agent, "_pty_transport", None)
        if transport:
            transport.close()


async def _reap_agent_when_done(agent: SpawnedAgent) -> None:
    """Await process completion so returncode gets set and the slot is freed.

    Stdout is drained by _drain_stdout. We drain stderr here.
    """
    try:
        if agent.process.stderr:
            asyncio.ensure_future(agent.process.stderr.read())
        await agent.process.wait()
    except Exception:
        pass
    print(
        f"[DarkMatter] Agent PID {agent.pid} finished (code={agent.process.returncode}), "
        f"slot freed for new spawns",
        file=sys.stderr,
    )
    cleanup_finished_agents()


async def agent_timeout_watchdog(agent: SpawnedAgent) -> None:
    """Kill a spawned agent if it exceeds the timeout."""
    await asyncio.sleep(AGENT_SPAWN_TIMEOUT)
    if not is_agent_running(agent):
        return
    print(
        f"[DarkMatter] Spawned agent PID {agent.pid} timed out after {AGENT_SPAWN_TIMEOUT}s, terminating...",
        file=sys.stderr,
    )
    try:
        kill_agent(agent, force=False)
        await asyncio.sleep(5.0)
        if is_agent_running(agent):
            print(f"[DarkMatter] Force-killing agent PID {agent.pid}", file=sys.stderr)
            kill_agent(agent, force=True)
    except ProcessLookupError:
        pass
