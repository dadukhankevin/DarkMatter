"""Session cards (machine-read facts + self-reported objective) and selector addressing."""
import io
import json
import time

import pytest

from darkmatter.collaboration import Collaboration
from darkmatter.collaboration_cli import execute, main
from darkmatter.facts import label, matches, valid_facts, workspace_facts
from darkmatter.gitbox.gitutil import git, init_repo


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "Parser"
    init_repo(root)
    (root / "README.md").write_text("x")
    git(root, "add", ".")
    git(root, "commit", "-m", "Add tokenizer")
    git(root, "checkout", "-b", "fix/unicode-escapes")
    (root / "README.md").write_text("changed")
    (root / "lexer.py").write_text("new")
    return root


def test_facts_come_from_git(repo, tmp_path):
    facts = workspace_facts(repo)
    assert facts["branch"] == "fix/unicode-escapes"
    assert set(facts["changed"]) == {"README.md", "lexer.py"} and facts["changed_count"] == 2
    assert facts["last_commit"] == "Add tokenizer"
    assert valid_facts(facts)
    assert workspace_facts(tmp_path / "not-a-repo") == {}


def test_repository_hooks_and_fsmonitor_never_run(repo, tmp_path):
    """Reading facts must not execute code configured in the repository."""
    marker = tmp_path / "ran"
    script = tmp_path / "fsmonitor.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(script))
    workspace_facts(repo)
    assert not marker.exists()


def test_facts_from_peers_are_bounded():
    assert not valid_facts({"branch": "x" * 500})
    assert not valid_facts({"changed": ["a"] * 50})
    assert not valid_facts({"branch": "main", "command": "rm -rf /"})
    assert valid_facts(None) and valid_facts({})


def test_labels_and_loose_matching():
    card = {"id": "ab" * 32, "client": "codex", "project": "Parser", "host": "Daniels-Mac-mini",
            "facts": {"branch": "fix/unicode-escapes"}}
    assert label(card) == "codex · Parser@fix/unicode-escapes · Daniels-Mac-mini"
    assert matches(card, {"host": "Mac mini"}) and matches(card, {"branch": "unicode"})
    assert matches(card, {"session": "abab"}) and not matches(card, {"host": "MacBook"})
    assert not matches(card, {"project": "Parser", "client": "cursor"})


def test_status_shows_cards_and_hooks_keep_facts_fresh(repo, monkeypatch, capsys):
    me = Collaboration(repo, "me", "claude-code")
    event = {"cwd": str(repo), "session_id": "worker", "hook_event_name": "SessionStart"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    main(["hook", "--client", "codex"])
    start = capsys.readouterr().out
    assert "objective" in start  # Nudge: say what you're doing.
    worker = Collaboration(repo, "worker", "codex")
    card = next(p for p in execute(me, "status")["peers"] if p["id"] == worker.agent_id)
    assert card["facts"]["branch"] == "fix/unicode-escapes"
    assert card["label"].startswith("codex · Parser@fix/unicode-escapes · ")
    execute(worker, "join", objective="Escaping edge cases in the lexer")
    card = next(p for p in execute(me, "status")["peers"] if p["id"] == worker.agent_id)
    assert card["objective"] == "Escaping edge cases in the lexer" and card["objective_at"] > time.time() - 60
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    main(["hook", "--client", "codex"])
    assert "objective" not in capsys.readouterr().out  # No nudge once it is set.


def test_any_picks_first_available_and_receiver_sees_how(tmp_path):
    sender = Collaboration(tmp_path / "Web", "sender", "claude-code")
    busy = Collaboration(tmp_path / "Api", "busy", "codex")
    idle = Collaboration(tmp_path / "Api", "idle", "codex")
    other = Collaboration(tmp_path / "Docs", "other", "cursor")
    busy.join(availability="busy")
    idle.join(availability="idle")
    other.join(availability="idle")
    result = execute(sender, "send", match={"project": "api"}, mode="any", content="Can someone check the API?")
    assert [s["to"] for s in result["sent"]] == [idle.agent_id]
    message = idle.read()["messages"][0]
    assert message["addressed"] == {"mode": "any", "match": {"project": "api"}, "matched": 1}
    assert busy.read()["messages"] == [] and other.read()["messages"] == []
    inbox = execute(idle, "read")["messages"][0]
    assert inbox["from_label"].startswith("claude-code · Web")
    direct = execute(sender, "send", recipient=busy.agent_id, content="Just you")
    assert busy.read()["messages"][0]["addressed"] == {"mode": "direct"} and direct["success"]


def test_all_fans_out_and_bad_selectors_are_refused(tmp_path):
    sender = Collaboration(tmp_path / "Web", "sender", "claude-code")
    targets = [Collaboration(tmp_path / "Api", f"s{i}", "codex") for i in range(3)]
    for target in targets:
        target.join()
    result = execute(sender, "send", match={"client": "codex"}, mode="all", content="Heads up: rebasing main")
    assert len(result["sent"]) == 3 and len({s["id"] for s in result["sent"]}) == 3
    for target in targets:
        assert target.read()["messages"][0]["addressed"]["mode"] == "all"
    with pytest.raises(ValueError, match="No reachable session"):
        execute(sender, "send", match={"host": "nonexistent-machine"}, content="hi")
    for bad in ({}, {"password": "x"}, {"host": ""}, {"host": 5}):
        with pytest.raises(ValueError, match="match"):
            execute(sender, "send", match=bad, content="hi")
    with pytest.raises(ValueError, match="mode"):
        execute(sender, "send", match={"client": "codex"}, mode="most", content="hi")


def test_forged_addressing_from_a_peer_is_marked_not_trusted(tmp_path):
    sender = Collaboration(tmp_path / "Web", "sender", "claude-code")
    target = Collaboration(tmp_path / "Api", "t", "codex")
    target.join()
    with pytest.raises(ValueError):
        sender.send(target.agent_id, "hi", addressed={"mode": "admin"})
