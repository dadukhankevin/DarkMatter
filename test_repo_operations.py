"""Verify narrow operation effects against isolated real Git remotes."""
import copy
import json
import time

import pytest

from darkmatter.gitbox.gitutil import git, init_repo
from darkmatter.repo_space import RepoSpace


@pytest.fixture
def pair(tmp_path):
    remote = tmp_path / 'shared.git'
    init_repo(remote, bare=True)
    a, b = RepoSpace(tmp_path / 'a'), RepoSpace(tmp_path / 'b')
    for device, session in ((a, 'a'), (b, 'b')):
        device.initialize(str(remote))
        device.register(session, 'test')
        device.review_ci()
    a.sync()
    b.sync()
    a.sync()
    return a, b, remote


def publish(device):
    result = device.publish(device.preview()['preview_id'])
    assert result['success'], result
    return result


def no_push(monkeypatch):
    import darkmatter.repo_space as module
    real_git = module.git

    def guarded(cwd, *args, **kwargs):
        assert args[0] not in ('push', 'commit'), args
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(module, 'git', guarded)


def test_fetch_reads_new_mail_without_publishing_queued_mail_or_enrolling(pair, tmp_path, monkeypatch):
    a, b, remote = pair
    outbound = a.send('a', b.status()['device'], 'b', 'DO NOT PUBLISH ME')['id']
    inbound = b.send('b', a.status()['device'], 'a', 'inbound only')['id']
    publish(b)
    stranger = RepoSpace(tmp_path / 'stranger')
    stranger.initialize(str(remote))
    stranger.review_ci()
    publish(stranger)
    refs = git(remote, 'show-ref').stdout
    before = copy.deepcopy(a.status())
    no_push(monkeypatch)
    monkeypatch.setattr(a, '_discover', lambda *args: pytest.fail('fetch cannot discover'))
    assert a.fetch()['success']
    assert a.read('a')['messages'][0]['id'] == inbound
    assert a.status()['peers'] == before['peers']
    assert a.status()['auto_peers'] == before['auto_peers']
    assert a.status()['delivery'][outbound] == 'queued'
    assert set(a._load()['outbox']) == {outbound}
    assert git(remote, 'show-ref').stdout == refs


def test_preview_local_only_and_receipts_are_identified(pair, monkeypatch):
    a, b, _ = pair
    mid = b.send('b', a.status()['device'], 'a', 'PRIVATE MESSAGE BODY')['id']
    publish(b)
    a.fetch()
    a.ack('a', mid)
    outgoing_id = next(iter(a._load()['outbox']))
    before = a.path.read_bytes()
    import darkmatter.repo_space as module
    monkeypatch.setattr(module, 'git', lambda *args, **kwargs: pytest.fail('preview must be local only'))
    preview = a.preview()
    outgoing = preview['outgoing'][0]
    assert outgoing['id'] == outgoing_id
    assert outgoing['acknowledges'] == mid
    assert outgoing['kind'] == 'receipt'
    assert outgoing['recipient_device'] == b.status()['device']
    assert 'PRIVATE MESSAGE BODY' not in json.dumps(preview)
    assert a._load()['private'] not in json.dumps(preview)
    assert a.path.read_bytes() == before


@pytest.mark.parametrize('change', ['message', 'presence', 'recipient', 'ciphertext', 'remote'])
def test_stale_preview_rejected_before_network(pair, monkeypatch, change):
    a, b, _ = pair
    a.send('a', b.status()['device'], 'b', 'approved original')
    preview = a.preview()
    state = a._load()
    if change == 'message':
        a.send('a', b.status()['device'], 'b', 'not reviewed')
    elif change == 'presence':
        a.register('another-session', 'test')
    else:
        if change == 'remote':
            state['remote'] = '/different-destination'
        else:
            envelope = next(iter(state['outbox'].values()))['envelope']
            envelope['to' if change == 'recipient' else 'ciphertext'] = 'changed'
        a._save(state)
    import darkmatter.repo_space as module
    monkeypatch.setattr(module, 'git', lambda *args, **kwargs: pytest.fail('stale preview reached network'))
    assert not a.publish(preview['preview_id'])['success']
    assert not a.publish('')['success']


def test_publish_is_exact_and_does_not_fetch_or_connect(pair, monkeypatch):
    a, b, remote = pair
    mid = a.send('a', b.status()['device'], 'b', 'reviewed')['id']
    preview = a.preview()
    peers = a.status()['peers']
    monkeypatch.setattr(a, '_discover', lambda *args: pytest.fail('publish cannot discover'))
    monkeypatch.setattr(a, '_fetch', lambda *args: pytest.fail('publish cannot receive'))
    assert a.publish(preview['preview_id'])['success']
    snapshot = json.loads(git(remote, 'show', preview['branch'] + ':mail.json').stdout)['payload']
    assert [env['id'] for env in snapshot['envelopes']] == [mid]
    assert snapshot['sessions'] == preview['presence']['sessions']
    assert a.status()['peers'] == peers
    assert a.preview()['outgoing'][0]['previously_published'] is True


def test_expiry_during_publish_preflight_rejects_before_push(pair, monkeypatch):
    a, b, remote = pair
    a.send('a', b.status()['device'], 'b', 'soon expired')
    state = a._load()
    next(iter(state['outbox'].values()))['expires'] = time.time() + 30
    a._save(state)
    preview = a.preview()
    refs = git(remote, 'show-ref').stdout
    original = a._ci_tree

    def expire(state):
        result = original(state)
        next(iter(state['outbox'].values()))['expires'] = 0
        return result

    monkeypatch.setattr(a, '_ci_tree', expire)
    assert not a.publish(preview['preview_id'])['success']
    assert git(remote, 'show-ref').stdout == refs


def test_connect_requires_unused_recent_publish_and_never_pushes_or_receives(pair, monkeypatch):
    a, b, remote = pair
    mid = b.send('b', a.status()['device'], 'a', 'mail must await fetch')['id']
    publish(b)
    assert not a.connect()['success']  # sync already consumed its publication.
    publish(a)
    refs = git(remote, 'show-ref').stdout
    no_push(monkeypatch)
    assert a.connect()['success']
    assert a.status()['auto_peers'] == [b.status()['device']]
    assert a.read('a')['messages'] == []
    assert a.fetch()['success']
    assert a.read('a')['messages'][0]['id'] == mid
    assert not a.connect()['success']
    assert git(remote, 'show-ref').stdout == refs


@pytest.mark.parametrize('change', ['expired', 'remote-ref', 'policy'])
def test_connect_rejects_invalid_proof(pair, change):
    a, b, remote = pair
    publish(a)
    if change == 'expired':
        state = a._load()
        state['connect_proof']['time'] -= 301
        a._save(state)
    elif change == 'remote-ref':
        branch = a.preview()['branch']
        git(remote, 'update-ref', '-d', 'refs/heads/' + branch)
    else:
        a.set_membership('pinned')
    assert not a.connect()['success']
    assert a.status()['auto_peers'] == []


def test_cli_requires_preview_and_reports_fetch_effects(pair, capsys):
    from darkmatter.repo_space_cli import main
    a, _, _ = pair
    args = ['--state-dir', str(a.directory)]
    assert main(['preview', *args]) == 0
    preview = json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit):
        main(['publish', *args])
    capsys.readouterr()
    assert main(['publish', *args, '--expect-preview', preview['preview_id']]) == 0
    capsys.readouterr()
    assert main(['fetch', *args]) == 0
    assert json.loads(capsys.readouterr().out)['effects']['remote_write'] is False


def test_real_stdio_fetch_and_preview_tools_have_narrow_effects(pair, monkeypatch, tmp_path):
    import asyncio
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    a, b, remote = pair
    a.send('a', b.status()['device'], 'b', 'must stay queued')
    mid = b.send('b', a.status()['device'], 'a', 'MCP inbox check')['id']
    publish(b)
    before = git(remote, 'show-ref').stdout
    monkeypatch.setenv('DARKMATTER_SPACE_DIR', str(a.directory))

    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-I', '-m', 'darkmatter'],
                                      env={**os.environ, 'DARKMATTER_PROJECT_DIR': str(tmp_path / 'project')})
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                listed = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert listed['darkmatter_repo_fetch'].annotations.readOnlyHint is False
                assert listed['darkmatter_repo_preview'].annotations.readOnlyHint is True
                assert 'expected_preview' in listed['darkmatter_repo_publish'].inputSchema['required']
                result = await client.call_tool('darkmatter_repo_fetch', {})
                assert json.loads(result.content[0].text)['success']
                result = await client.call_tool('darkmatter_repo', {'action': 'read', 'session_id': 'a'})
                assert json.loads(result.content[0].text)['messages'][0]['id'] == mid
                result = await client.call_tool('darkmatter_repo_preview', {})
                preview = json.loads(result.content[0].text)
                assert preview['outgoing'][0]['target_session'] == 'b'
                assert git(remote, 'show-ref').stdout == before
                result = await client.call_tool('darkmatter_repo_publish', {'expected_preview': 'stale'})
                assert not json.loads(result.content[0].text)['success']
                assert git(remote, 'show-ref').stdout == before
                result = await client.call_tool('darkmatter_repo_publish', {'expected_preview': preview['preview_id']})
                assert json.loads(result.content[0].text)['success']
                published = git(remote, 'show-ref').stdout
                result = await client.call_tool('darkmatter_repo_connect', {})
                assert json.loads(result.content[0].text)['success']
                assert git(remote, 'show-ref').stdout == published

    asyncio.run(run())


def test_new_receipt_invalidates_review_and_expired_mail_is_previewed_as_removal(pair):
    a, b, _ = pair
    outbound = a.send('a', b.status()['device'], 'b', 'outgoing')['id']
    publish(a)
    incoming = b.send('b', a.status()['device'], 'a', 'incoming')['id']
    publish(b)
    a.fetch()
    preview = a.preview()
    a.ack('a', incoming)
    assert not a.publish(preview['preview_id'])['success']
    state = a._load()
    state['outbox'][outbound]['expires'] = 0
    a._save(state)
    preview = a.preview()
    assert preview['removed_envelope_ids'] == [outbound]
    assert all(item['id'] != outbound for item in preview['outgoing'])


def test_failed_push_invalidates_connect_proof(pair, monkeypatch):
    from darkmatter.gitbox.gitutil import GitError
    import darkmatter.repo_space as module
    a, _, _ = pair
    publish(a)
    real_git = module.git

    def denied(cwd, *args, **kwargs):
        if args[0] == 'push':
            raise GitError('write permission denied')
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(module, 'git', denied)
    assert not a.publish(a.preview()['preview_id'])['success']
    assert not a.connect()['success']
    assert a.status()['auto_peers'] == []


def test_hook_last_seen_refresh_preserves_review_but_status_changes_do_not(pair):
    a, _, _ = pair
    preview = a.preview()
    assert preview['presence']['session_last_seen_may_refresh'] is True
    a.register('a', 'test')  # Ordinary host hook heartbeat; identity/status unchanged.
    assert a.preview()['preview_id'] == preview['preview_id']
    assert a.publish(preview['preview_id'])['success']
    a.register('a', 'test', paused=True)
    assert not a.publish(preview['preview_id'])['success']
    changed = a.preview()
    a.register('a', 'test', agent='different-agent')
    assert not a.publish(changed['preview_id'])['success']
