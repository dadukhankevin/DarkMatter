"""Owner setup and persistent worker for shared-repo correspondence."""
import argparse
import json
import time
from pathlib import Path

from darkmatter.gitbox.gitutil import GitError, git
from darkmatter.repo_space import MEMBERSHIP_POLICIES, RepoSpace, default_space_directory


def _origin(cwd):
    """The checkout's own origin URL; mail uses the host's existing push permissions."""
    try:
        result = git(cwd, "remote", "get-url", "origin", check=False)
    except GitError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="darkmatter space")
    parser.add_argument("--state-dir", default=None,
                        help="Private device state; defaults to a per-repo directory under ~/.darkmatter/spaces")
    parser.add_argument("action", choices=("init", "enroll", "revoke", "register", "status", "send",
                                            "read", "ack", "fetch", "preview", "publish", "connect", "sync", "run",
                                            "wake", "retry-wake", "ci-reviewed", "membership"))
    parser.add_argument("--expect-preview", help="Required for publish: exact preview_id from the local preview")
    parser.add_argument("--remote", help="Defaults to this checkout's origin remote")
    parser.add_argument("--manual-ci", action="store_true",
                        help="Require an explicit ci-reviewed before every workflow change")
    parser.add_argument("--space")
    parser.add_argument("--membership", choices=MEMBERSHIP_POLICIES,
                        help="New spaces default to repo-writers; existing spaces retain their policy")
    parser.add_argument("--device")
    parser.add_argument("--session")
    parser.add_argument("--client", default="cli")
    parser.add_argument("--agent")
    parser.add_argument("--availability", choices=("busy", "idle", "stopped", "unknown"))
    pause = parser.add_mutually_exclusive_group()
    pause.add_argument("--paused", action="store_const", const=True, default=None, dest="paused")
    pause.add_argument("--resume", action="store_const", const=False, dest="paused")
    parser.add_argument("--target")
    parser.add_argument("--content")
    parser.add_argument("--id")
    parser.add_argument("--argv", help="Locally authorized wake command as a JSON argv array, never a shell string")
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        space = RepoSpace(args.state_dir or default_space_directory())
        action = args.action
        if action == "init":
            remote = args.remote or _origin(Path.cwd())
            if not remote:
                parser.error("init requires --remote (this checkout has no origin remote)")
            result = space.initialize(remote, args.space, membership=args.membership or "repo-writers",
                                      ci_review="manual" if args.manual_ci else "auto")
            if not args.manual_ci:
                result["ci"] = space.scan_ci()
                if result["ci"]["workflows_needing_review"]:
                    result["next_step"] = ("These workflows may run when mail branches are created. Exclude "
                                           "darkmatter/mail/** from them, or run `darkmatter space ci-reviewed` "
                                           "to accept the current workflows.")
        elif action == "membership":
            if not args.membership:
                parser.error("membership requires --membership repo-writers|pinned")
            result = space.set_membership(args.membership)
        elif action in ("enroll", "revoke"):
            result = space.enroll(args.device, remove=action == "revoke")
        elif action == "register":
            result = space.register(args.session, args.client, agent=args.agent, paused=args.paused, availability=args.availability)
        elif action == "send":
            result = space.send(args.session, args.device, args.target, args.content)
        elif action == "read":
            result = space.read(args.session)
        elif action == "ack":
            result = space.ack(args.session, args.id)
        elif action == "wake":
            result = space.configure_wake(args.session, json.loads(args.argv or "null"), args.cwd, enabled=args.enable)
        elif action == "retry-wake":
            result = space.retry_wake(args.session)
        elif action == "ci-reviewed":
            result = space.review_ci()
        elif action == "fetch":
            result = space.fetch()
        elif action == "preview":
            result = space.preview()
        elif action == "publish":
            if not args.expect_preview:
                parser.error("publish requires --expect-preview from space preview")
            result = space.publish(args.expect_preview)
        elif action == "connect":
            result = space.connect()
        elif action == "sync":
            result = space.sync()
        elif action == "run":
            if not 5 <= args.interval <= 3600:
                parser.error("--interval must be between 5 and 3600 seconds")
            while True:
                result = space.sync()
                result["wake"] = space.wake_once()
                print(json.dumps(result), flush=True)
                time.sleep(args.interval)
        else:
            result = space.status()
        print(json.dumps(result, indent=2))
        return 0 if result.get("success", True) else 1
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, GitError, TypeError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1
