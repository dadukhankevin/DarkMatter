"""
Agent auto-spawn system — SpawnedAgent, spawn/kill/cleanup, prompt building.

Depends on: config, models
"""

import asyncio
import os
import sys
import time
from dataclasses import dataclass

from darkmatter.config import (
    AGENT_SPAWN_ENABLED,
    AGENT_SPAWN_MAX_CONCURRENT,
    AGENT_SPAWN_MAX_PER_HOUR,
    AGENT_SPAWN_TIMEOUT,
    ACTIVE_CLIENT,
)
from darkmatter.models import AgentState, QueuedMessage


# =============================================================================
# Spawn Tracking (ephemeral, not persisted)
# =============================================================================

@dataclass
class SpawnedAgent:
    process: asyncio.subprocess.Process
    message_id: str
    spawned_at: float
    pid: int
    spawn_dir: str = ""  # temp directory to clean up when agent exits


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
    """Check if a spawned agent is still running."""
    return agent.process.returncode is None


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
            if agent.spawn_dir:
                try:
                    shutil.rmtree(agent.spawn_dir, ignore_errors=True)
                except Exception:
                    pass
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

    # Create a temp working directory with .mcp.json pointing to the parent's
    # MCP server via HTTP. The child shares the parent's identity and inbox.
    import tempfile
    spawn_dir = tempfile.mkdtemp(prefix="darkmatter-spawn-")
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
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    import json as _json
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

    try:
        process = await asyncio.create_subprocess_exec(
            command, *args,
            env=env,
            stdin=stdin_pipe,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spawn_dir,
        )

        if stdin_pipe is not None:
            process.stdin.write(prompt.encode())
            await process.stdin.drain()
            process.stdin.close()

        agent = SpawnedAgent(
            process=process,
            message_id=msg.message_id,
            spawned_at=time.monotonic(),
            pid=process.pid,
            spawn_dir=spawn_dir,
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
            f"[DarkMatter] Agent spawn failed: command '{command}' not found. "
            f"Set DARKMATTER_CLIENT to a valid profile or DARKMATTER_AGENT_COMMAND to the correct path.",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"[DarkMatter] Agent spawn failed: {e}", file=sys.stderr)


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
