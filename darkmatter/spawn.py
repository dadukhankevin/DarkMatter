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
    # Stdout streaming: callback receives chunks as they arrive
    stdout_callback: object = None  # Optional async callable(chunk: str)


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
    import shutil
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
                    backup_path = agent.spawn_mcp_config + ".pre-spawn"
                    if os.path.exists(backup_path):
                        shutil.move(backup_path, agent.spawn_mcp_config)
                    else:
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
1. Reply with a substantive response — don't just acknowledge, provide value.
2. If this message is better suited for another peer, forward it to them with darkmatter_send_message(message_id="{msg.message_id}", target_agent_id=...).
3. After replying, proactively share relevant updates or ask follow-up questions.
"""


# =============================================================================
# Spawn
# =============================================================================

async def spawn_agent_for_message(state: AgentState, msg: QueuedMessage,
                                   save_state_fn=None) -> None:
    """Spawn an agent subprocess to handle an incoming message."""
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

    # Write .mcp.json in the project directory pointing to the parent's
    # MCP server via HTTP. The child shares the parent's identity and inbox.
    # We use the project directory (not a temp dir) so the child agent has
    # access to the actual codebase and project context.
    # NOTE: We back up the original .mcp.json first so cleanup can restore it
    # instead of deleting it — prevents breaking the primary session's config.
    spawn_dir = _PROJECT_DIR
    mcp_config = {
        "mcpServers": {
            "darkmatter": {
                "type": "http",
                "url": f"http://127.0.0.1:{state.port}/mcp",
            }
        }
    }
    config_file = ACTIVE_CLIENT.get("config_file", ".mcp.json")
    config_path = os.path.join(spawn_dir, config_file)
    backup_path = config_path + ".pre-spawn"
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    import json as _json
    if os.path.exists(config_path) and not os.path.exists(backup_path):
        import shutil as _shutil
        _shutil.copy2(config_path, backup_path)
    with open(config_path, "w") as f:
        _json.dump(mcp_config, f)

    command = ACTIVE_CLIENT["command"]
    args = list(ACTIVE_CLIENT["args"])
    prompt_style = ACTIVE_CLIENT.get("prompt_style", "positional")
    stdin_pipe = None

    if prompt_style == "positional":
        args.append(prompt)
        stdin_pipe = asyncio.subprocess.DEVNULL  # Don't inherit parent's stdin (may be MCP pipe)
    elif prompt_style == "stdin":
        stdin_pipe = asyncio.subprocess.PIPE
    elif prompt_style.startswith("flag:"):
        flag_name = prompt_style.split(":", 1)[1]
        args.extend([f"--{flag_name}", prompt])
        stdin_pipe = asyncio.subprocess.DEVNULL
    else:
        print(f"[DarkMatter] Unknown prompt_style '{prompt_style}', falling back to positional", file=sys.stderr)
        args.append(prompt)
        stdin_pipe = asyncio.subprocess.DEVNULL

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
        process = await asyncio.create_subprocess_exec(
            *exec_argv,
            env=env,
            stdin=stdin_pipe,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spawn_dir,
        )

        if prompt_style == "stdin" and process.stdin is not None:
            process.stdin.write(prompt.encode())
            await process.stdin.drain()
            process.stdin.close()

        agent = SpawnedAgent(
            process=process,
            message_id=msg.message_id,
            spawned_at=time.monotonic(),
            pid=process.pid,
            spawn_mcp_config=config_path,
        )
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
    except Exception as e:
        print(f"[DarkMatter] Agent spawn failed: {e}", file=sys.stderr)


async def _drain_stdout(agent: SpawnedAgent) -> None:
    """Read stdout and forward chunks to the callback if set."""
    if not agent.process.stdout:
        return
    try:
        while True:
            chunk = await agent.process.stdout.read(512)
            if not chunk:
                break
            if agent.stdout_callback:
                try:
                    await agent.stdout_callback(chunk.decode("utf-8", errors="replace"))
                except Exception:
                    pass
    except Exception:
        pass


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
