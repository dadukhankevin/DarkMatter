"""Opt-in contract test against a real Codex binary, without model turns or trust writes.

Run with DARKMATTER_TEST_CODEX=/absolute/path/to/codex python -m pytest test_hook_host.py.
The host reads only a temporary home and workspace; no live client state is used.
"""

import json
import os
import selectors
import subprocess
import time

import pytest

from darkmatter.installer import SUPPORTED_TARGETS, install_target


def _list_hooks(binary, home, workspace):
    with subprocess.Popen(
        [binary, "app-server", "--stdio"],
        cwd=workspace, env={**os.environ, "CODEX_HOME": str(home)},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)

                def request(identifier, method, params):
                    process.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
                    process.stdin.flush()
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if not selector.select(timeout=1):
                            continue
                        line = process.stdout.readline()
                        assert line, "Codex app-server exited before responding"
                        response = json.loads(line)
                        if response.get("id") == identifier:
                            assert "error" not in response, response
                            return response["result"]
                    pytest.fail(f"Codex app-server timed out: {method}")

                request(1, "initialize", {"clientInfo": {"name": "darkmatter-test", "version": "1"},
                                          "capabilities": {"experimentalApi": True}})
                process.stdin.write('{"method":"initialized"}\n')
                process.stdin.flush()
                return request(2, "hooks/list", {"cwds": [str(workspace)]})["data"][0]
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_installed_hooks_parse_in_codex(tmp_path):
    binary = os.environ.get("DARKMATTER_TEST_CODEX")
    if not binary:
        pytest.skip("Set DARKMATTER_TEST_CODEX to run the real host contract test")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = next(t for t in SUPPORTED_TARGETS if t.client == "codex")
    ok, message = install_target(target, command="/test/python", display_name="test",
                                 home=tmp_path, wake=True, collaborate=True)
    assert ok, message
    home = tmp_path / ".codex"
    hooks_path = home / "hooks.json"
    original = hooks_path.read_text()
    broken = json.loads(original)
    broken["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 3630.0
    hooks_path.write_text(json.dumps(broken))
    before = _list_hooks(binary, home, workspace)
    assert before["warnings"], before
    assert not before["hooks"]

    hooks_path.write_text(original)
    after = _list_hooks(binary, home, workspace)
    assert not after["warnings"], after
    assert not after["errors"], after
    assert {h["eventName"] for h in after["hooks"]} >= {
        "stop", "sessionStart", "userPromptSubmit", "preToolUse", "postToolUse",
    }
    assert [h["eventName"] for h in after["hooks"]].count("stop") == 1
    stop = next(h for h in after["hooks"] if h["eventName"] == "stop")
    assert stop["handlerType"] == "command"
    assert stop["timeoutSec"] == 3630
    assert stop["trustStatus"] == "untrusted"  # Installation must never approve itself.
