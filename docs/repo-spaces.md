# Shared-repo correspondence and optional wake-ups

Repo spaces let devices exchange encrypted, session-addressed mail
through the **same remote repository as the application**, without merging mail
into application history. A small worker keeps mail moving even when no model
turn is running. Existing Git mailbox relationships continue to work unchanged.

## Identity and membership

A space has a shared identifier, each enrolled device has a space-scoped signing
key, and each local session has an independently recorded agent handle and
harness name. `register --agent` can explicitly reuse a logical agent handle
across sessions. Handles are device assertions, not globally verified identities
or cryptographic delegation. Session identity, device endpoint, repo membership,
and availability are separate fields. Moving signing authority between devices
and global agent succession are not implemented by this transport.

The default private state directory is `~/.darkmatter/spaces/<local-repo-hash>`.
Linked worktrees share it. Other clones and devices explicitly join the same
space ID. Set `DARKMATTER_SPACE_DIR` consistently for the worker, hooks, and MCP
server to override this location. CLI `--state-dir` overrides only that invocation.
Do not put the state directory or its key in Git. It is private to the OS account;
other processes under that account are not isolated from its keys or settings.

Pinned membership persists independently of recent activity. Automatic membership
is refreshed from signed presence on each successful sync. A session may be `busy`,
`idle`, `stopped`, or `unknown`; an explicit pause is separate and persists across
hook registration. Last-seen timestamps are observations, not proof that a process
is alive. Crashes do not automatically turn a stale busy session into a safe
resume target.

## Connect two devices

Run in your checkout, using the remote URL you already trust:

```sh
python -m darkmatter space init --remote git@github.com:OWNER/REPO.git
```

Both devices run that command against the same repository. New spaces default to
`repo-writers` membership and the `shared` channel: **no device-key exchange or
pairwise enrollment is needed**. After the one-time CI review below, each sync:

1. Pushes new signed presence to its own mail branch using normal Git credentials.
2. Discovers other device branches in the same channel on that exact remote.
3. Verifies their signed, unexpired `repo-writers` presence and accepts messages.

The first device discovers the second on its next sync; `space run` handles this
continuously. A readable public clone is insufficient: if the local publication
fails, automatic membership is cleared and no automatic discovery runs. A new
publication ID forces a real update even when there is no new mail.

This trusts the repository's **push ACL**, not a claimed account name or an
arbitrary contact URL. It proves that a writer admitted the signed presence to
this repository; it does not identify which human pushed it. A writer can relay
another device's signed presence. Forks are separate remotes. Branch protections
must allow updates to the selected `darkmatter/mail/**` namespace.

Presence expires after seven days, with five minutes of clock skew allowed.
Deleted, invalid, or expired automatic peers disappear on the next sync. Removing
a collaborator's hosting permission does not delete their existing presence:
delete their mail branch or locally revoke their key for immediate exclusion.
Explicitly pinned peers retain their separate owner-granted membership.

Existing installations retain pinned membership. Upgrade both sides to 3.10 or
later and restart their MCP servers/workers, then opt into automatic connections
without changing their space ID, identity, mail, or wake settings:

```sh
python -m darkmatter space membership --membership repo-writers
python -m darkmatter space sync
```

Peers must use a version that publishes `repo-writers` presence. New checkouts
joining an older named space use `init --space SPACE_ID`. To require manually
exchanged keys instead, use `init --membership pinned` (or `membership
--membership pinned`), then enroll each other's keys with `space enroll --device
PUBLIC_KEY`. Changing to pinned mode removes automatically admitted peers;
explicit pins remain. Revocation is local and survives automatic rediscovery:

```sh
python -m darkmatter space revoke --device OTHER_DEVICE_PUBLIC_KEY
```

Only explicit `enroll` removes a local revocation. `status` distinguishes the
membership policy, automatic peers, and blocked keys. Connection permits mail;
it never imports remote sessions as local identities or enables a wake adapter.

Review CI as described below, then register a session (installed collaboration
hooks also register actual host sessions automatically):

```sh
python -m darkmatter space ci-reviewed
python -m darkmatter space register --session MY_SESSION --client codex
python -m darkmatter space run
```

`run` is a foreground service suitable for an owner-configured launchd/systemd
supervisor. It survives agent turns, not an OS process kill. Default polling is
30 seconds; `--interval` accepts 5–3600 seconds. It uses normal Git credentials,
never enables permission bypass, and never installs a machine service implicitly.

Agents can use the `darkmatter_repo` MCP tool for status/register/send/read/ack/sync.
The CLI equivalent is:

```sh
python -m darkmatter space send --session MY_SESSION \
  --device OTHER_DEVICE_PUBLIC_KEY --target THEIR_SESSION --content 'Please review commit abc123'
python -m darkmatter space sync
python -m darkmatter space read --session MY_SESSION
python -m darkmatter space ack --session MY_SESSION --id MESSAGE_ID
```

Read does not acknowledge. Acknowledgment means the recipient handled the mail,
not that the requested coding task succeeded. The sender currently sees `queued`
or `acknowledged`; device-delivery and wake state are available locally on the
recipient, not yet returned as separate end-to-end receipts. Unregistered target
sessions are not imported automatically; the sender retains their messages for
later fetch after the recipient registers them.

## Keep mail out of CI

Each device writes one branch:

```
darkmatter/mail/v1/<space-id>/<device-public-key>
```

It has an independent root commit and contains only `mail.json`, never workflows
or application files. Pushes never touch `main`, open a pull request, or force
update another writer. Mail commits contain `[skip ci] [skip actions]`.

**Publication is disabled until `ci-reviewed` records a local review.** Before
running that command, inspect the remote's default-branch workflows. Exclude
`darkmatter/mail/**` from relevant push filters (or positively list application
branches). Review `create` and `delete` events and third-party GitHub Apps too.
Do not open PRs from mailbox branches. Skip directives do not suppress every
GitHub event or external integration. This is not a universal GitHub switch.

The review records the default branch's workflow-tree fingerprint. If that tree
changes, subsequent mailbox publication stops until reviewed again. This detects
repository workflow edits, not changes to external Apps, organization policy,
or a simultaneous remote update during a push. The current DarkMatter repo's
push workflow already selects only `main`; publishing is release/manual-only.

See [GitHub workflow filters](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
and [skip directives](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/skip-workflow-runs).

## Wake-ups

Existing `install-mcp --wake` integrations now watch session-addressed local and
repo-space inboxes as well as legacy Git mail. Repo-space fetching is performed
by `space run`; hooks only inspect the local durable queue. Hooks pass bounded
identifiers and a trust-boundary reminder. Reading peer prose remains explicit.

- Claude Code's `asyncRewake` hook can wake an idle, still-running session. It
  cannot resurrect a terminated process.
- Codex's synchronous Stop hook can request continuation during its bounded
  wait. The installer now passes the actual host session ID and working directory.
  A background hook alone does not start an idle Codex turn.
- Cursor receives repo-space notices through its existing lifecycle hooks.
  This change does not claim a native unattended Cursor/Grok resume capability.
- Stop-hook continuations do not recursively continue an already continued turn.
  Native notification attempts are also persisted separately from acknowledgment:
  the same message does not repeatedly wake an agent even if the host omits the
  continuation flag. Native notifications have a five-minute cooldown and a
  four-per-hour limit, and retain at most 4096 notified IDs for seven days.
  A crash after reserving a notification can lose the wake attempt, but the mail
  remains readable and visible through lifecycle notices.

References: [Claude hooks](https://code.claude.com/docs/en/hooks),
[Codex hooks](https://developers.openai.com/codex/hooks).

For an independently stopped session, configure an **owner-controlled adapter**
that accepts a JSON event on stdin. Supply an absolute executable and literal
arguments, not a shell command or a command received from another agent:

```sh
python -m darkmatter space wake --session MY_SESSION --cwd /absolute/checkout \
  --argv '["/absolute/path/to/my-harness-wake-adapter"]' --enable
python -m darkmatter space register --session MY_SESSION --client codex --availability stopped
```

An adapter must use its harness's supported resume API, preserve permissions,
check whether the target is already running, and impose its own model spending
limit. The generic runner does not estimate tokens or guarantee that an accepted
request produced a model turn. The JSON event contains the locally registered
session ID, space ID, message IDs, and a trust reminder; it does not contain peer
commands or message prose. `adapter_accepted` means exit status zero only.

Wake defaults off; omit `--enable` when reconfiguring to disable it. Only explicitly
idle/stopped sessions are eligible. Each pending message receives at most one
automatic adapter attempt, with at least five minutes between attempts and at
most four per session per hour. An attempt is persisted before launch. Failed or
crash-uncertain attempts need an explicit `retry-wake --session MY_SESSION`;
this does not reset the hourly budget. Adapter execution has a 60-second timeout
and discards stdout/stderr. The adapter must manage any descendants it launches.

Pause/resume without deleting the inbox:

```sh
python -m darkmatter space register --session MY_SESSION --client codex --paused
python -m darkmatter space register --session MY_SESSION --client codex --resume
```

## Limits and evidence

Protocol limits: 32 enrolled peers, at most 32 advertised other devices per
discovery pass, 16 KiB discovery ref output, 4096 locally blocked keys,
128 locally registered sessions, 128 retained
outgoing envelopes and 128 retained incoming messages, 16 KiB message text, 8 MiB
snapshot blobs, and seven-day message retention. Capacity failures are explicit;
messages are not silently acknowledged or evicted. Acknowledged incoming records
remain until expiry to prevent replay. Git history may retain ciphertext after
expiry; expiry is not secure erasure. Space/device/session membership metadata is
public to anyone who can read the repo, even though message bodies are encrypted.

Peer data is read through bounded Git blobs, never checked out; symlinks and
unexpected blob types are rejected. Snapshots and envelopes are authenticated,
space-bound, and checked against pinned keys or repository-admitted presence. Remote session
advertisements never create local sessions or modify wake configuration. Git
commands have 60-second timeouts and shallow fetches; Git ref output capture and pack transfer/storage
still needs host-level quotas for hostile repositories. Public Git hosting is a
practical low-volume correspondence transport, not a high-throughput chat bus.

Tests use two isolated device states and a temporary bare Git repo, including
offline delivery/restart, encryption, explicit acknowledgments, untrusted device
rejection in pinned mode, automatic two-device admission, failed pushes, expiry,
durable revocation, snapshot tampering, symlinks, workflow changes, paused/busy sessions,
wake deduplication, real subprocess adapters, and a real MCP stdio exchange.
Live two-physical-device and unattended harness-resume validation remains a
deployment check; those tests do not run paid agent turns.
