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
from darkmatter.state import get_state

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
    raw_output_log_path: str = ""
    # Stdout streaming: dict of message_id → async callable(chunk: str)
    # Multiple callbacks = nested messages, chunks go to all active targets
    stdout_callbacks: dict = None  # Initialized to {} in __post_init__
    _returncode: int = None  # Tracks exit code (Process.returncode is read-only in Python 3.14+)

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
    if agent._returncode is not None:
        return False
    # Also check the process object's own returncode (set by asyncio reaping)
    if agent.process.returncode is not None:
        agent._returncode = agent.process.returncode
        return False
    # Check if process is still alive
    try:
        os.kill(agent.pid, 0)
    except ProcessLookupError:
        # Process is gone
        agent._returncode = -1
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
    from darkmatter.context import get_context
    context = get_context(state, mode="full")

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
MANDATORY: When done, call darkmatter_complete_and_summarize summarizing what happened (accepted/rejected, who, why).
"""

    return f"""\
INCOMING MESSAGE — Act now. Be proactive: reply, forward, or both.

{context}

Message {msg.message_id} from {msg.from_agent_id[:12]}:
{msg.content}

HOW TO RESPOND:
- darkmatter_send_message(content="your reply", target_agent_id="{msg.from_agent_id}", in_reply_to="{msg.message_id}")
- For long tasks, send frequent status updates so the receiver sees progress (e.g. "Researching...", "Found the issue, working on a fix...").
- To forward instead: darkmatter_send_message(content="your commentary", target_agent_id=<peer>, forward_message_ids=["{msg.message_id}"]).

STAY ALIVE:
After responding, call darkmatter_wait_for_message(timeout_seconds=1200) and stay available for follow-ups. This is a conversation — keep the loop going. Respond to each new message the same way (send_message), then wait again. You are the agent for this conversation; don't exit after one reply.

WHEN TO EXIT:
Call darkmatter_complete_and_summarize ONLY when the conversation has naturally concluded — the task is done, the topic is resolved, or the human says goodbye. Write a dense summary of what you did, reference peers with @agent_id, and note anything the hivemind should know.
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

    # Consume the message from the queue — it's being delivered to the spawned agent
    from darkmatter.state import consume_message
    for i, m in enumerate(state.message_queue):
        if m.message_id == msg.message_id:
            state.message_queue.pop(i)
            consume_message(msg.message_id)
            break

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
        debug_dir = os.path.join(os.path.expanduser("~"), ".darkmatter", "agent_stdout_logs")
        os.makedirs(debug_dir, exist_ok=True)
        debug_log_path = os.path.join(
            debug_dir,
            f"{int(time.time())}-{msg.message_id[:12]}-pidpending.log",
        )

        # PTY gives us a headless terminal — output streams in real time.
        # Both stdin AND stdout must be connected to the PTY so that TUI
        # frameworks (Ink/Claude Code) can set raw mode on stdin.
        import pty as _pty
        import fcntl, struct, termios
        pty_master_fd, pty_slave_fd = _pty.openpty()
        # Set PTY size to match the xterm.js widget on the frontend
        winsize = struct.pack("HHHH", 24, 120, 0, 0)  # rows=24, cols=120
        fcntl.ioctl(pty_slave_fd, termios.TIOCSWINSZ, winsize)

        process = await asyncio.create_subprocess_exec(
            *exec_argv,
            env=env,
            stdin=pty_slave_fd,
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
            raw_output_log_path=debug_log_path.replace("pidpending", str(process.pid)),
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
        print(f"[DarkMatter] Raw agent stdout log: {agent.raw_output_log_path}", file=sys.stderr)

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
    """Read PTY output through a client-specific adapter, stream prose sections."""
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
        # Flush any remaining buffered prose
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
    """Await process completion so returncode gets set and the slot is freed.

    Stdout is drained by _drain_stdout. We drain stderr here.
    """
    stderr_text = ""
    try:
        if agent.process.stderr:
            stderr_data = await agent.process.stderr.read()
            stderr_text = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""
        await agent.process.wait()
    except Exception:
        pass
    print(
        f"[DarkMatter] Agent PID {agent.pid} finished (code={agent.process.returncode}), "
        f"slot freed for new spawns",
        file=sys.stderr,
    )
    if stderr_text:
        # Log last 2000 chars of stderr for debugging spawn failures
        tail = stderr_text[-2000:] if len(stderr_text) > 2000 else stderr_text
        print(f"[DarkMatter] Agent PID {agent.pid} stderr:\n{tail}", file=sys.stderr)
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


# =============================================================================
# Standby Agent — spawn a ready agent immediately after one finishes
# =============================================================================

async def spawn_standby_agent(state: AgentState) -> None:
    """Spawn a standby agent that waits for the next message.

    Uses the same spawn_agent_for_message path as any other agent —
    just with a "wait for activity" message. The agent calls
    wait_for_message and responds when something arrives.
    """
    import uuid
    ok, reason = can_spawn_agent()
    if not ok:
        print(f"[DarkMatter] Not spawning standby: {reason}", file=sys.stderr)
        return

    msg = QueuedMessage(
        message_id=f"standby-{uuid.uuid4().hex[:12]}",
        content="Wait for network activity.",
        from_agent_id=state.agent_id,
    )
    await spawn_agent_for_message(state, msg)
