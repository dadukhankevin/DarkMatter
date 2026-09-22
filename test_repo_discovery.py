"""Admission through signed presence on an owner-selected, writable Git remote."""
import json
import time

import pytest

from darkmatter.gitbox.gitutil import GitError, git, init_repo
from darkmatter.repo_space import DOMAIN, PREFIX, TTL, RepoSpace, _json
from darkmatter.security import sign_payload


@pytest.fixture
def writers(tmp_path):
    remote = tmp_path / 'shared.git'
    init_repo(remote, bare=True)
    app = tmp_path / 'app'
    init_repo(app)
    (app / 'app.txt').write_text('unchanged')
    git(app, 'add', '.')
    git(app, 'commit', '-m', 'app')
    git(app, 'push', str(remote), 'HEAD:main')
    devices = []
    for name in ('a', 'b'):
        device = RepoSpace(tmp_path / name)
        device.initialize(str(remote))
        device.register(name, 'test-client', availability='idle')
        device.review_ci()
        devices.append(device)
    return *devices, remote


def branch(device):
    state = device.status()
    return PREFIX + state['space'] + '/' + state['device']


def rewrite_snapshot(device, remote, mutate, *, sign=True):
    path = device.transport / 'mail.json'
    signed = json.loads(path.read_text())
    mutate(signed['payload'])
    if sign:
        signed['signature'] = sign_payload(device._load()['private'], DOMAIN, _json(signed['payload']))
    path.write_text(_json(signed))
    git(device.transport, 'add', 'mail.json')
    git(device.transport, 'commit', '-m', 'test presence [skip ci]')
    git(device.transport, 'push', str(remote), 'HEAD:refs/heads/' + branch(device))


def connect(a, b):
    assert a.sync()['success']
    assert b.sync()['success']
    assert a.sync()['success']
    assert b.status()['device'] in a.status()['auto_peers']
    assert a.status()['device'] in b.status()['auto_peers']


def test_default_writers_discover_and_exchange_mail_without_key_enrollment(writers):
    a, b, remote = writers
    main = git(remote, 'rev-parse', 'main').stdout
    connect(a, b)
    assert a.status()['space'] == b.status()['space'] == 'shared'
    assert set(a.status()['sessions']) == {'a'}
    assert a._load()['wake'] == {}
    mid = a.send('a', b.status()['device'], 'b', 'hello from another device')['id']
    assert a.sync()['success']
    assert b.sync()['success']
    assert b.read('b')['messages'][0]['id'] == mid
    assert a.status()['delivery'][mid] == 'queued'
    b.ack('b', mid)
    b.sync()
    a.sync()
    assert a.status()['delivery'][mid] == 'acknowledged'
    assert git(remote, 'rev-parse', 'main').stdout == main
    assert git(remote, 'ls-tree', '--name-only', branch(a)).stdout.strip() == 'mail.json'
    assert 'hello from another device' not in git(remote, 'show', branch(a) + ':mail.json').stdout
    # Even an unchanged inbox requires a new push, not an up-to-date no-op.
    previous = git(remote, 'rev-parse', branch(a)).stdout
    assert a.sync()['success']
    assert git(remote, 'rev-parse', branch(a)).stdout != previous


def test_revoked_device_not_reenrolled_after_restart(writers):
    a, b, _ = writers
    connect(a, b)
    key = b.status()['device']
    a.enroll(key, remove=True)
    a = RepoSpace(a.directory)
    assert a.sync()['success']
    assert key not in a.status()['peers']
    assert key in a.status()['blocked_devices']
    a.enroll(key)  # Only explicit owner enrollment clears the block and pins it.
    assert key not in a.status()['blocked_devices']
    assert key not in a.status()['auto_peers']


def test_removed_branch_and_failed_publication_remove_automatic_membership(writers, monkeypatch):
    a, b, remote = writers
    connect(a, b)
    git(b.transport, 'push', str(remote), ':refs/heads/' + branch(b))
    assert a.sync()['success']
    assert a.status()['peers'] == []
    connect(a, b)
    import darkmatter.repo_space as module
    real_git = module.git

    def read_only(cwd, *args, **kwargs):
        if args[0] == 'push':
            raise GitError('write access denied')
        if args[0] == 'ls-remote' and '--heads' in args:
            pytest.fail('read-only clone must not discover or admit devices')
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(module, 'git', read_only)
    assert 'write access denied' in a.sync()['errors']['publish']
    assert a.status()['peers'] == []
    assert a.status()['peer_sessions'] == {}
    assert a.wake_once()['attempted'] == 0


@pytest.mark.parametrize('attack', ['tampered', 'expired', 'future', 'nan', 'huge-time', 'wrong-device', 'wrong-space', 'malformed-mail'])
def test_invalid_presence_cannot_auto_enroll(writers, attack):
    a, b, remote = writers
    b.sync()

    def mutate(payload):
        if attack in ('tampered', 'expired'):
            payload['published_at'] = time.time() - TTL - 1
        elif attack == 'future':
            payload['published_at'] = time.time() + 600
        elif attack == 'nan':
            payload['published_at'] = float('nan')
        elif attack == 'huge-time':
            payload['published_at'] = 10 ** 400
        elif attack == 'wrong-device':
            payload['device'] = '0' * 64
        elif attack == 'wrong-space':
            payload['space'] = 'another-space'
        else:
            payload['envelopes'] = [None]

    rewrite_snapshot(b, remote, mutate, sign=attack != 'tampered')
    assert b.status()['device'] in a.sync()['errors']
    assert a.status()['peers'] == []
    assert a.status()['peer_sessions'] == {}
    assert a.read('a')['messages'] == []


def test_auto_discovery_never_imports_sessions_or_wake_configuration(writers):
    a, b, remote = writers
    b.sync()
    rewrite_snapshot(b, remote, lambda p: p.update(wake={'a': {'argv': ['/bin/evil'], 'enabled': True}}))
    assert a.sync()['success']
    assert a.status()['auto_peers'] == [b.status()['device']]
    assert set(a.status()['sessions']) == {'a'}
    assert a._load()['wake'] == {}
    assert a.wake_once()['attempted'] == 0


def test_legacy_state_stays_pinned_until_owner_changes_policy(writers):
    a, b, _ = writers
    state = a._load()
    for field in ('membership', 'auto_peers', 'blocked_devices'):
        state.pop(field)
    a._save(state)
    b.sync()
    assert a.sync()['success']
    assert a.status()['membership'] == 'pinned'
    assert a.status()['peers'] == []
    a.set_membership('repo-writers')
    assert a.sync()['success']
    assert a.status()['auto_peers'] == [b.status()['device']]
    a.set_membership('pinned')
    assert a.status()['peers'] == []
    assert a.sync()['success']
    assert a.status()['peers'] == []


def test_pinned_peer_does_not_opt_into_automatic_connections(writers):
    a, b, _ = writers
    b.set_membership('pinned')
    b.sync()
    assert a.sync()['success']
    assert a.status()['auto_peers'] == []


def test_discovery_limit_checked_before_fetch(writers, monkeypatch):
    a, b, _ = writers
    b.sync()
    import darkmatter.repo_space as module
    monkeypatch.setattr(module, 'MAX_PEERS', 0)
    monkeypatch.setattr(a, '_fetch', lambda *args: pytest.fail('must not fetch over budget'))
    assert 'limit' in a.sync()['errors']['discovery']
    assert a.status()['peers'] == []


def test_cli_defaults_and_explicit_policy_migration(tmp_path, capsys):
    from darkmatter.repo_space_cli import main
    remote = tmp_path / 'remote'
    init_repo(remote, bare=True)
    options = ['--state-dir', str(tmp_path / 'state')]
    assert main(['init', *options, '--remote', str(remote)]) == 0
    assert json.loads(capsys.readouterr().out)['membership'] == 'repo-writers'
    assert main(['membership', *options, '--membership', 'pinned']) == 0
    assert json.loads(capsys.readouterr().out)['membership'] == 'pinned'


def test_actual_rejected_push_does_not_keep_automatic_peers(writers):
    a, b, remote = writers
    connect(a, b)
    # Advance our remote ref to an unrelated history: Git itself rejects our
    # publication. Discovery must not continue on the strength of an older push.
    other_tip = git(remote, 'rev-parse', branch(b)).stdout.strip()
    git(remote, 'update-ref', 'refs/heads/' + branch(a), other_tip)
    result = a.sync()
    assert 'publish' in result['errors']
    assert a.status()['auto_peers'] == []
    assert a.status()['peer_sessions'] == {}


def test_other_channel_is_not_discovered(writers, tmp_path):
    a, b, remote = writers
    other = RepoSpace(tmp_path / 'other')
    other.initialize(str(remote), 'another-channel')
    other.register('outsider', 'test-client')
    other.review_ci()
    other.sync()
    connect(a, b)
    assert other.status()['device'] not in a.status()['peers']


def test_expired_previously_admitted_peer_is_removed(writers):
    a, b, remote = writers
    connect(a, b)
    rewrite_snapshot(b, remote, lambda p: p.update(published_at=time.time() - TTL - 1))
    assert b.status()['device'] in a.sync()['errors']
    assert a.status()['peers'] == []


def test_real_stdio_can_discover_send_and_read_without_enrollment(writers, monkeypatch, tmp_path):
    import asyncio
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    a, b, _ = writers
    assert b.sync()['success']
    monkeypatch.setenv('DARKMATTER_SPACE_DIR', str(a.directory))

    async def exchange():
        params = StdioServerParameters(command=sys.executable, args=['-I', '-m', 'darkmatter'],
                                      env={**os.environ, 'DARKMATTER_PROJECT_DIR': str(tmp_path / 'project')})
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()

                async def call(**arguments):
                    result = await client.call_tool('darkmatter_repo', arguments)
                    assert not result.isError
                    return json.loads(result.content[0].text)

                assert (await call(action='sync'))['success']
                status = await call(action='status')
                assert status['auto_peers'] == [b.status()['device']]
                sent = await call(action='send', session_id='a', device=b.status()['device'],
                                  target='b', content='hello through MCP auto discovery')
                assert (await call(action='sync'))['success']
                assert b.sync()['success']
                received = b.read('b')['messages']
                assert received[0]['id'] == sent['id']
                b.ack('b', sent['id'])
                b.sync()
                assert (await call(action='sync'))['success']
                assert (await call(action='status'))['delivery'][sent['id']] == 'acknowledged'

    asyncio.run(exchange())
