"""
Main agent lifecycle — spawn, kill, cleanup, respawn.

Single long-lived agent model: ONE main agent handles all messages via
wait_for_message, delegates heavy work to its own sub-agents, and respawns
when it exits (via complete_and_summarize or timeout).

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
    AGENT_SPAWN_MAX_PER_HOUR,
    AGENT_SPAWN_TIMEOUT,
    ACTIVE_CLIENT,
)
from darkmatter.models import AgentState
from darkmatter.state import get_state

# Capture the project directory at import time so spawned agents run here.
_PROJECT_DIR = os.getcwd()


# =============================================================================
# Spawn Tracking (ephemeral, not persisted)
# =============================================================================

@dataclass
class SpawnedAgent:
    process: asyncio.subprocess.Process
    label: str  # e.g. "main-agent"
    spawned_at: float
    pid: int
    spawn_mcp_config: str = ""
    raw_output_log_path: str = ""
    stdout_callbacks: dict = None
    _returncode: int = None

    def __post_init__(self):
        if self.stdout_callbacks is None:
            self.stdout_callbacks = {}


_spawned_agents: list[SpawnedAgent] = []
_spawn_timestamps: list[float] = []


def get_spawned_agents() -> list[SpawnedAgent]:
    return _spawned_agents


# =============================================================================
# Checks
# =============================================================================

def can_spawn_agent() -> tuple[bool, str]:
    """Check whether we can spawn a new agent subprocess."""
    import darkmatter.config as _cfg
    if not _cfg.AGENT_SPAWN_ENABLED:
        return False, "Agent spawning is disabled"

    cleanup_finished_agents()

    if len(_spawned_agents) > 0:
        return False, "Main agent already running"

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
    if agent._returncode is not None:
        return False
    if agent.process.returncode is not None:
        agent._returncode = agent.process.returncode
        return False
    try:
        os.kill(agent.pid, 0)
    except ProcessLookupError:
        agent._returncode = -1
        return False
    except PermissionError:
        return True
    try:
        pid, status = os.waitpid(agent.pid, os.WNOHANG)
        if pid != 0:
            if os.WIFEXITED(status):
                agent._returncode = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                agent._returncode = -os.WTERMSIG(status)
            else:
                agent._returncode = -1
            return False
    except ChildProcessError:
        agent._returncode = -1
        return False
    except Exception:
        pass
    return True


def kill_agent(agent: SpawnedAgent, force: bool = False) -> None:
    if force:
        agent.process.kill()
    else:
        agent.process.terminate()


def cleanup_finished_agents() -> None:
    still_running = []
    for agent in _spawned_agents:
        if not is_agent_running(agent):
            print(
                f"[DarkMatter] Agent PID {agent.pid} exited "
                f"(code={agent.process.returncode}, label={agent.label})",
                file=sys.stderr,
            )
            if agent.spawn_mcp_config:
                try:
                    os.remove(agent.spawn_mcp_config)
                except Exception as e:
                    print(f"[DarkMatter] Failed to clean up spawn config: {e}", file=sys.stderr)
        else:
            still_running.append(agent)
    _spawned_agents.clear()
    _spawned_agents.extend(still_running)


def is_main_agent_running() -> bool:
    cleanup_finished_agents()
    return len(_spawned_agents) > 0


# =============================================================================
# Prompt
# =============================================================================

def build_main_agent_prompt(state: AgentState) -> str:
    """Build the fixed prompt for the main agent."""
    from darkmatter.context import get_context
    context = get_context(state, mode="full")

    return f"""\
You are the MAIN AGENT for this DarkMatter node. You are long-lived — handle
messages, do work, and stay alive until your task or conversation naturally ends.

{context}

START:
Call darkmatter_wait_for_message() to receive your first message.

HOW TO WORK:
1. Reply via darkmatter_send_message(content=..., target_agent_id=<sender>, in_reply_to=<msg_id>).
2. For BIG TASKS (refactors, multi-file changes, deep research), use your CLI's
   built-in Agent tool with run_in_background=true to delegate to sub-agents.
   Send a status message to the sender, then call darkmatter_wait_for_message()
   with a short timeout (10-30s) so you can check on background work and relay results.
3. For QUICK TASKS (questions, small edits, status checks), just do them directly.
4. After handling a message, call darkmatter_wait_for_message() for the next one.
   YOU choose the timeout — short if you have background sub-agents, long if idle.
5. Connection requests arrive as messages with metadata type "connection_request".
   Accept/reject via darkmatter_connection(action="accept/reject", request_id=...).
6. Messages with from_entrypoint metadata are from a HUMAN using the chat UI.
   They can ONLY see darkmatter_send_message output — your stdout is invisible.

WHEN TO EXIT:
Call darkmatter_complete_and_summarize when:
- A conversation thread or feature is done
- The human says goodbye
- Your context is getting large and you need a fresh start
Write a dense summary. A fresh main agent will be spawned with the collapsed context.
"""


# =============================================================================
# Spawn
# =============================================================================

async def spawn_main_agent(state: AgentState) -> None:
    """Spawn the main agent if one isn't already running."""
    if is_main_agent_running():
        print("[DarkMatter] Main agent already running, skipping spawn", file=sys.stderr)
        return

    ok, reason = can_spawn_agent()
    if not ok:
        print(f"[DarkMatter] Not spawning main agent: {reason}", file=sys.stderr)
        return

    prompt = build_main_agent_prompt(state)
    await _spawn_process(state, prompt, label="main-agent")


async def _spawn_process(state: AgentState, prompt: str, label: str) -> None:
    """Launch a CLI agent subprocess with the given prompt."""

    env = os.environ.copy()
    env["DARKMATTER_AGENT_ENABLED"] = "false"
    env["DARKMATTER_ENTRYPOINT_AUTOSTART"] = "false"
    for var in ACTIVE_CLIENT["env_cleanup"]:
        env.pop(var, None)

    # Write spawn MCP config to a temp file
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
    spawn_mcp_fd, spawn_mcp_path = _tempfile.mkstemp(
        prefix="darkmatter-spawn-mcp-", suffix=".json"
    )
    with os.fdopen(spawn_mcp_fd, "w") as f:
        _json.dump(mcp_config, f)

    command = ACTIVE_CLIENT["command"]
    args = list(ACTIVE_CLIENT["args"])

    mcp_via_cli = "mcp_stdio" in ACTIVE_CLIENT.get("capabilities", set())
    if mcp_via_cli:
        args.extend(["--mcp-config", spawn_mcp_path, "--strict-mcp-config"])
    prompt_style = ACTIVE_CLIENT.get("prompt_style", "positional")

    # Sanitize prompt for shell safety
    safe_prompt = prompt.replace('"', "'").replace('`', "'").replace('$', '')

    if prompt_style == "positional":
        args.append(safe_prompt)
    elif prompt_style.startswith("flag:"):
        flag_name = prompt_style.split(":", 1)[1]
        args.extend([f"--{flag_name}", safe_prompt])
    elif prompt_style == "stdin":
        pass  # stdin_pipe handled below
    else:
        args.append(safe_prompt)

    # Optionally wrap in sandbox
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
        debug_dir = os.path.join(os.path.expanduser("~"), ".darkmatter", "agent_stdout_logs")
        os.makedirs(debug_dir, exist_ok=True)
        debug_log_path = os.path.join(
            debug_dir,
            f"{int(time.time())}-{label}-pidpending.log",
        )

        # PTY needed so Claude Code detects a terminal and runs interactively.
        # start_new_session=True isolates child from daemon's process group,
        # preventing SIGTERM propagation that previously killed the daemon.
        import pty as _pty
        import fcntl, struct, termios
        pty_master_fd, pty_slave_fd = _pty.openpty()
        winsize = struct.pack("HHHH", 24, 120, 0, 0)
        fcntl.ioctl(pty_slave_fd, termios.TIOCSWINSZ, winsize)

        process = await asyncio.create_subprocess_exec(
            *exec_argv,
            env=env,
            stdin=pty_slave_fd,
            stdout=pty_slave_fd,
            stderr=asyncio.subprocess.PIPE,
            cwd=spawn_dir,
            start_new_session=True,
        )
        os.close(pty_slave_fd)

        loop = asyncio.get_event_loop()
        pty_reader = asyncio.StreamReader()
        read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(pty_reader),
            os.fdopen(pty_master_fd, "rb", 0),
        )

        agent = SpawnedAgent(
            process=process,
            label=label,
            spawned_at=time.monotonic(),
            pid=process.pid,
            spawn_mcp_config=spawn_mcp_path,
            raw_output_log_path=debug_log_path.replace("pidpending", str(process.pid)),
        )
        agent._pty_reader = pty_reader
        agent._pty_transport = read_transport
        _spawned_agents.append(agent)
        _spawn_timestamps.append(time.monotonic())
        sandbox_label = " [sandboxed]" if sandboxed else ""
        print(
            f"[DarkMatter] Spawned {label} PID {process.pid}{sandbox_label}",
            file=sys.stderr,
        )

        asyncio.create_task(_agent_timeout_watchdog(agent))
        asyncio.create_task(_drain_stdout(agent))
        asyncio.create_task(_reap_agent_when_done(agent))

    except FileNotFoundError:
        print(
            f"[DarkMatter] Spawn failed: command '{command}' not found. "
            f"Set DARKMATTER_CLIENT to a valid profile or DARKMATTER_AGENT_COMMAND.",
            file=sys.stderr,
        )
        try: os.remove(spawn_mcp_path)
        except OSError: pass
    except Exception as e:
        print(f"[DarkMatter] Spawn failed: {e}", file=sys.stderr)
        try: os.remove(spawn_mcp_path)
        except OSError: pass


# =============================================================================
# Background tasks for a spawned agent
# =============================================================================

async def _drain_stdout(agent: SpawnedAgent) -> None:
    """Read PTY/stdout through a client-specific adapter, stream prose sections."""
    reader = getattr(agent, "_pty_reader", None) or agent.process.stdout
    if not reader:
        return
    from darkmatter.adapters import get_adapter
    adapter = get_adapter(ACTIVE_CLIENT)
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            if agent.raw_output_log_path:
                try:
                    with open(agent.raw_output_log_path, "a", encoding="utf-8") as f:
                        f.write(chunk.decode("utf-8", errors="replace"))
                except Exception:
                    pass
            if not agent.stdout_callbacks:
                continue
            for section in adapter.feed(chunk):
                for cb in list(agent.stdout_callbacks.values()):
                    try:
                        await cb(section)
                    except Exception:
                        pass
        for section in adapter.flush():
            for cb in list(agent.stdout_callbacks.values()):
                try:
                    await cb(section)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        transport = getattr(agent, "_pty_transport", None)
        if transport:
            transport.close()


async def _reap_agent_when_done(agent: SpawnedAgent) -> None:
    """Await process exit, then auto-respawn the main agent."""
    stderr_text = ""
    try:
        if agent.process.stderr:
            stderr_data = await agent.process.stderr.read()
            stderr_text = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""
        await agent.process.wait()
    except Exception:
        pass
    print(
        f"[DarkMatter] Agent PID {agent.pid} finished (code={agent.process.returncode})",
        file=sys.stderr,
    )
    if stderr_text:
        tail = stderr_text[-2000:] if len(stderr_text) > 2000 else stderr_text
        print(f"[DarkMatter] Agent PID {agent.pid} stderr:\n{tail}", file=sys.stderr)
    cleanup_finished_agents()

    # Auto-respawn
    import darkmatter.config as _cfg
    if _cfg.AGENT_SPAWN_ENABLED:
        state = get_state()
        if state:
            print("[DarkMatter] Main agent exited — spawning fresh main agent", file=sys.stderr)
            try:
                await spawn_main_agent(state)
            except Exception as e:
                print(f"[DarkMatter] Failed to respawn main agent: {e}", file=sys.stderr)


async def _agent_timeout_watchdog(agent: SpawnedAgent) -> None:
    """Kill a spawned agent if it exceeds the timeout."""
    await asyncio.sleep(AGENT_SPAWN_TIMEOUT)
    if not is_agent_running(agent):
        return
    print(
        f"[DarkMatter] Agent PID {agent.pid} timed out after {AGENT_SPAWN_TIMEOUT}s, terminating...",
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
