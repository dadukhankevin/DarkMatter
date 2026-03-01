"""
Agent auto-spawn system — SpawnedAgent, spawn/kill/cleanup, prompt building.

Depends on: config, models
"""

import asyncio
import os
import shlex
import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional

from darkmatter.config import (
    AGENT_SPAWN_ENABLED,
    AGENT_SPAWN_MAX_CONCURRENT,
    AGENT_SPAWN_MAX_PER_HOUR,
    AGENT_SPAWN_COMMAND,
    AGENT_SPAWN_ARGS,
    AGENT_SPAWN_ENV_CLEANUP,
    AGENT_SPAWN_TIMEOUT,
    AGENT_SPAWN_TERMINAL,
)
from darkmatter.models import AgentState, QueuedMessage


# =============================================================================
# Spawn Tracking (ephemeral, not persisted)
# =============================================================================

@dataclass
class SpawnedAgent:
    process: Optional[asyncio.subprocess.Process]  # None in terminal mode
    message_id: str
    spawned_at: float
    pid: Optional[int]
    terminal_mode: bool = False
    pid_file: Optional[str] = None
    script_file: Optional[str] = None


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
    if not AGENT_SPAWN_ENABLED:
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
    """Check if a spawned agent is still running."""
    if not agent.terminal_mode:
        return agent.process is not None and agent.process.returncode is None

    pid = agent.pid
    if pid is None and agent.pid_file:
        try:
            pid = int(open(agent.pid_file).read().strip())
            agent.pid = pid
        except (FileNotFoundError, ValueError):
            return False
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_agent(agent: SpawnedAgent, force: bool = False) -> None:
    """Send terminate/kill signal to a spawned agent."""
    if not agent.terminal_mode:
        if agent.process is not None:
            if force:
                agent.process.kill()
            else:
                agent.process.terminate()
        return

    pid = agent.pid
    if pid is None and agent.pid_file:
        try:
            pid = int(open(agent.pid_file).read().strip())
            agent.pid = pid
        except (FileNotFoundError, ValueError):
            return
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


def cleanup_terminal_files(agent: SpawnedAgent) -> None:
    """Remove PID and script files for a finished terminal agent."""
    for path in (agent.pid_file, agent.script_file):
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def cleanup_finished_agents() -> None:
    """Remove finished agent processes from the tracking list."""
    still_running = []
    for agent in _spawned_agents:
        if not is_agent_running(agent):
            pid_display = agent.pid or "unknown"
            if agent.terminal_mode:
                print(
                    f"[DarkMatter] Terminal agent PID {pid_display} finished "
                    f"(msg={agent.message_id[:12]}...)",
                    file=sys.stderr,
                )
                cleanup_terminal_files(agent)
            else:
                print(
                    f"[DarkMatter] Spawned agent PID {pid_display} exited "
                    f"(code={agent.process.returncode if agent.process else '?'}, msg={agent.message_id[:12]}...)",
                    file=sys.stderr,
                )
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
DARKMATTER: You have received an incoming connection request. Here is your conversation context:

{context}

An agent wants to connect to you. Review the request details:
- Request ID: {request_id}
- Message: {msg.content}

Use darkmatter_list_inbox or darkmatter_status to see your current state, then decide:
- To accept: darkmatter_connection(action="accept", request_id="{request_id}")
- To reject: darkmatter_connection(action="reject", request_id="{request_id}")

Consider the agent's bio, peer trust scores, and whether this connection would be valuable.
"""

    return f"""\
DARKMATTER: You have received a message. Here is your conversation context:

{context}

Check message {msg.message_id} and respond or forward accordingly.
"""


# =============================================================================
# Spawn
# =============================================================================

async def spawn_in_terminal(msg: QueuedMessage, prompt: str, env: dict, cwd: str) -> SpawnedAgent:
    """Spawn an agent in a visible Terminal.app window (macOS only)."""
    darkmatter_dir = os.path.expanduser("~/.darkmatter")
    pids_dir = os.path.join(darkmatter_dir, "spawn_pids")
    scripts_dir = os.path.join(darkmatter_dir, "spawn_scripts")
    os.makedirs(pids_dir, exist_ok=True)
    os.makedirs(scripts_dir, exist_ok=True)

    pid_file = os.path.join(pids_dir, f"{msg.message_id}.pid")
    script_file = os.path.join(scripts_dir, f"{msg.message_id}.sh")

    env_lines = []
    for k, v in env.items():
        env_lines.append(f"export {k}={shlex.quote(v)}")
    env_block = "\n".join(env_lines)

    cmd_parts = [AGENT_SPAWN_COMMAND] + AGENT_SPAWN_ARGS + [prompt]
    cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)

    unset_block = "\n".join(f"unset {var}" for var in AGENT_SPAWN_ENV_CLEANUP)

    script_content = f"""\
#!/bin/bash
# DarkMatter spawned agent — message {msg.message_id[:12]}...
echo $$ > {shlex.quote(pid_file)}

cleanup() {{
    rm -f {shlex.quote(pid_file)}
}}
trap cleanup EXIT

{unset_block}
{env_block}

cd {shlex.quote(cwd)}

echo "[DarkMatter] Agent started for message {msg.message_id[:12]}..."
echo "PID: $$"
echo "---"

exec {cmd_str}
"""

    with open(script_file, "w") as f:
        f.write(script_content)
    os.chmod(script_file, 0o755)

    escaped_path = script_file.replace('\\', '\\\\').replace('"', '\\"')
    apple_script = f'tell application "Terminal" to do script "{escaped_path}"'
    await asyncio.create_subprocess_exec("osascript", "-e", apple_script)

    pid = None
    for _ in range(20):
        await asyncio.sleep(0.25)
        try:
            pid = int(open(pid_file).read().strip())
            break
        except (FileNotFoundError, ValueError):
            continue

    return SpawnedAgent(
        process=None,
        message_id=msg.message_id,
        spawned_at=time.monotonic(),
        pid=pid,
        terminal_mode=True,
        pid_file=pid_file,
        script_file=script_file,
    )


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
    for var in AGENT_SPAWN_ENV_CLEANUP:
        env.pop(var, None)

    import random
    env["DARKMATTER_PORT"] = str(random.randint(9200, 9299))

    try:
        if AGENT_SPAWN_TERMINAL and sys.platform == "darwin":
            agent = await spawn_in_terminal(msg, prompt, env, os.getcwd())
            _spawned_agents.append(agent)
            _spawn_timestamps.append(time.monotonic())
            pid_display = agent.pid or "pending"
            print(
                f"[DarkMatter] Spawned terminal agent (PID {pid_display}) for message {msg.message_id[:12]}... "
                f"from {msg.from_agent_id or 'unknown'}",
                file=sys.stderr,
            )
            asyncio.create_task(agent_timeout_watchdog(agent))
            return

        process = await asyncio.create_subprocess_exec(
            AGENT_SPAWN_COMMAND, *AGENT_SPAWN_ARGS, prompt,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
        )
        agent = SpawnedAgent(
            process=process,
            message_id=msg.message_id,
            spawned_at=time.monotonic(),
            pid=process.pid,
        )
        _spawned_agents.append(agent)
        _spawn_timestamps.append(time.monotonic())
        print(
            f"[DarkMatter] Spawned agent PID {process.pid} for message {msg.message_id[:12]}... "
            f"from {msg.from_agent_id or 'unknown'}",
            file=sys.stderr,
        )

        asyncio.create_task(agent_timeout_watchdog(agent))

    except FileNotFoundError:
        print(
            f"[DarkMatter] Agent spawn failed: command '{AGENT_SPAWN_COMMAND}' not found. "
            f"Set DARKMATTER_AGENT_COMMAND to the correct path.",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"[DarkMatter] Agent spawn failed: {e}", file=sys.stderr)


async def agent_timeout_watchdog(agent: SpawnedAgent) -> None:
    """Kill a spawned agent if it exceeds the timeout."""
    await asyncio.sleep(AGENT_SPAWN_TIMEOUT)
    if not is_agent_running(agent):
        return
    pid_display = agent.pid or "unknown"
    print(
        f"[DarkMatter] Spawned agent PID {pid_display} timed out after {AGENT_SPAWN_TIMEOUT}s, terminating...",
        file=sys.stderr,
    )
    try:
        kill_agent(agent, force=False)
        await asyncio.sleep(5.0)
        if is_agent_running(agent):
            print(f"[DarkMatter] Force-killing agent PID {pid_display}", file=sys.stderr)
            kill_agent(agent, force=True)
        if agent.terminal_mode:
            cleanup_terminal_files(agent)
    except ProcessLookupError:
        pass
