"""Agents on different machines that share a Git remote talk through one tool."""
import asyncio
import json
import os
import sys
import time

import pytest

from darkmatter.collaboration import Collaboration
from darkmatter.collaboration_cli import execute
from darkmatter.gitbox.gitutil import git, init_repo
from darkmatter.repo_space import RepoSpace


@pytest.fixture
def machines(tmp_path):
    remote = tmp_path / 'origin.git'
    init_repo(remote, bare=True)
    seed = tmp_path / 'seed'
    init_repo(seed)
    (seed / 'README').write_text('project')
    git(seed, 'add', '.')
    git(seed, 'commit', '-m', 'project')
    git(seed, 'push', str(remote), 'HEAD:main')
    result = {}
    for name in ('laptop', 'desktop'):
        checkout = tmp_path / name / 'project'
        checkout.parent.mkdir()
        git(checkout.parent, 'clone', str(remote), str(checkout))
        space = RepoSpace(tmp_path / name / 'space')
        space.initialize(str(remote), ci_review='auto')
        assert space.scan_ci()['ci_reviewed']
        result[name] = {'checkout': checkout, 'space': space.directory, 'local': tmp_path / name / 'local'}
    return result


def _use(monkeypatch, machine):
    monkeypatch.setenv('DARKMATTER_SPACE_DIR', str(machine['space']))
    monkeypatch.setenv('DARKMATTER_LOCAL_DIR', str(machine['local']))


def test_execute_routes_remote_and_local_mail(machines, monkeypatch):
    laptop, desktop = machines['laptop'], machines['desktop']
    _use(monkeypatch, desktop)
    reviewer = Collaboration(desktop['checkout'], 'reviewer', 'codex')
    execute(reviewer, 'join', objective='Review parser changes')
    RepoSpace(desktop['space']).sync()
    _use(monkeypatch, laptop)
    author = Collaboration(laptop['checkout'], 'author', 'claude-code')
    local_peer = Collaboration(laptop['checkout'], 'tests', 'cursor')
    execute(local_peer, 'join', objective='Run tests')
    RepoSpace(laptop['space']).sync()
    status = execute(author, 'status')
    assert [p['id'] for p in status['peers'] if p['id'] != author.agent_id] == [local_peer.agent_id]
    remote = {p['session']: p for p in status['remote_peers']}
    assert remote['reviewer']['objective'] == 'Review parser changes'
    address = remote['reviewer']['id']
    sent = execute(author, 'send', recipient=address, content='Please review', message_id='review-1')
    assert sent['delivery'] == 'published'
    again = execute(author, 'send', recipient=address, content='Please review', message_id='review-1')
    assert again['id'] == 'review-1'
    with pytest.raises(ValueError, match='another message'):
        execute(author, 'send', recipient=address, content='Different text', message_id='review-1')
    execute(author, 'send', recipient=local_peer.agent_id, content='Run the suite', message_id='tests-1')
    _use(monkeypatch, desktop)
    state = RepoSpace(desktop['space'])._load()
    state['last_sync'] = 0  # As if the throttle window has passed since the desktop's last poll.
    RepoSpace(desktop['space'])._save(state)
    inbox = execute(reviewer, 'read')
    assert [(m['id'], m['via'], m['content']) for m in inbox['messages']] == [('review-1', 'repo', 'Please review')]
    assert inbox['messages'][0]['from'].endswith('/author')
    execute(reviewer, 'ack', ids=['review-1'])
    assert execute(reviewer, 'read')['messages'] == []
    _use(monkeypatch, laptop)
    RepoSpace(laptop['space']).sync()
    assert execute(author, 'delivery', message_id='review-1')['delivery'] == 'acknowledged'
    assert execute(author, 'delivery', message_id='tests-1')['delivery'] == 'queued'


def test_status_explains_how_to_reach_other_machines_when_unconfigured(tmp_path):
    board = Collaboration(tmp_path, 'solo', 'codex')
    status = execute(board, 'status')
    assert status['remote']['configured'] is False
    assert 'darkmatter space init' in status['remote']['hint']
    with pytest.raises(ValueError, match='space init'):
        execute(board, 'send', recipient='a' * 64 + '/other', content='hi')


def test_two_machines_find_and_message_each_other_over_real_stdio(machines):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    def server(machine, client):
        return StdioServerParameters(command=sys.executable, args=['-I', '-m', 'darkmatter'], env={
            **os.environ, 'DARKMATTER_PROJECT_DIR': str(machine['checkout']),
            'DARKMATTER_SPACE_DIR': str(machine['space']), 'DARKMATTER_LOCAL_DIR': str(machine['local']),
            'DARKMATTER_CLIENT': client, 'DARKMATTER_SPACE_SYNC_SECONDS': '5',
            'HOME': str(machine['local'].parent)})

    async def until(predicate, call, timeout=45):
        deadline = time.monotonic() + timeout
        while True:
            value = await call()
            if predicate(value):
                return value
            assert time.monotonic() < deadline, value
            await asyncio.sleep(1)

    async def run():
        async with stdio_client(server(machines['laptop'], 'claude-code')) as a_streams, \
                stdio_client(server(machines['desktop'], 'codex')) as b_streams:
            async with ClientSession(*a_streams) as a, ClientSession(*b_streams) as b:
                await a.initialize()
                await b.initialize()

                async def call(session, sid, **params):
                    result = await session.call_tool('darkmatter_collaborate', {'session_id': sid, **params})
                    assert not result.isError, result
                    return json.loads(result.content[0].text)

                await call(a, 'author', action='join', objective='Ship the parser')
                await call(b, 'reviewer', action='join', objective='Review incoming changes')
                status = await until(lambda s: any(p['session'] == 'reviewer' for p in s.get('remote_peers', [])),
                                     lambda: call(a, 'author', action='status'))
                address = next(p['id'] for p in status['remote_peers'] if p['session'] == 'reviewer')
                sent = await call(a, 'author', action='send', recipient=address, content='Ready for review')
                assert sent['success']
                inbox = await until(lambda r: r['messages'], lambda: call(b, 'reviewer', action='read'))
                assert inbox['messages'][0]['content'] == 'Ready for review'
                assert inbox['messages'][0]['from'].endswith('/author')
                await call(b, 'reviewer', action='ack', ids=[sent['id']])
                await until(lambda r: r.get('delivery') == 'acknowledged',
                            lambda: call(a, 'author', action='delivery', message_id=sent['id']))
                reply_to = inbox['messages'][0]['from']
                await call(b, 'reviewer', action='send', recipient=reply_to, content='Looks good')
                reply = await until(lambda r: r['messages'], lambda: call(a, 'author', action='read'))
                assert reply['messages'][0]['content'] == 'Looks good'

    asyncio.run(run())


def test_tool_calls_do_not_churn_hook_registration(machines, monkeypatch):
    laptop = machines['laptop']
    _use(monkeypatch, laptop)
    space = RepoSpace(laptop['space'])
    space.register('s', 'claude-code', availability='busy')
    space.sync()
    preview = space.preview()['preview_id']
    board = Collaboration(laptop['checkout'], 's', 'cli')  # e.g. an MCP entry without DARKMATTER_CLIENT
    execute(board, 'status')
    execute(board, 'read')
    assert space.status()['sessions']['s']['client'] == 'claude-code'
    assert space.preview()['preview_id'] == preview  # Nothing new to publish.


def test_selector_reaches_repo_peers_and_file_churn_does_not_push(machines, monkeypatch):
    laptop, desktop = machines['laptop'], machines['desktop']
    _use(monkeypatch, desktop)
    reviewer = Collaboration(desktop['checkout'], 'reviewer', 'codex')
    execute(reviewer, 'join', objective='Reviewing')
    desk = RepoSpace(desktop['space'])
    desk.register('reviewer', None, facts={'branch': 'review/api', 'changed': [], 'changed_count': 0,
                                           'last_commit': 'Tidy'})
    desk.sync()
    head = desk._load()['published_head']
    desk.register('reviewer', None, facts={'branch': 'review/api', 'changed': ['a.py'], 'changed_count': 1,
                                           'last_commit': 'Tidy'})
    assert not desk._needs_publish(desk._load())  # Edits alone never push immediately.
    desk.sync()
    assert desk._load()['published_head'] == head
    _use(monkeypatch, laptop)
    author = Collaboration(laptop['checkout'], 'author', 'claude-code')
    RepoSpace(laptop['space']).sync()
    card = next(p for p in execute(author, 'status')['remote_peers'] if p['session'] == 'reviewer')
    assert card['facts']['branch'] == 'review/api' and card['project'] == 'origin'
    sent = execute(author, 'send', match={'branch': 'review'}, content='Ready when you are')
    assert sent['sent'][0]['via'] == 'repo'
    _use(monkeypatch, desktop)
    state = desk._load()
    state['last_sync'] = 0
    desk._save(state)
    message = execute(reviewer, 'read')['messages'][0]
    assert message['addressed']['mode'] == 'any' and message['addressed']['match'] == {'branch': 'review'}
