from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from replaylab_agent.connectors import codex_app_server as codex
from replaylab_agent.errors import AuthenticationError, ConnectorError, ProviderRequestError
from replaylab_agent.provider_diagnostics import provider_failure_metadata

FIXTURE = Path(__file__).parent / 'fixtures/fake_codex_app_server.py'


def connector(tmp_path, mode):
    trace = tmp_path / 'requests.jsonl'
    return codex.CodexAppServerConnector(
        [sys.executable, str(FIXTURE), mode, str(trace)], cwd=tmp_path, timeout=5,
    ), trace


def requests(trace, method):
    return [row['params'] for row in map(json.loads, trace.read_text(encoding='utf-8').splitlines())
            if row.get('method') == method]


@pytest.mark.asyncio
async def test_transient_account_routing_failure_recovers_before_exactly_one_model_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, '_ACCOUNT_READ_RETRY_DELAYS', (.001, .001))
    client, trace = connector(tmp_path, 'account-read-routing-once')
    timeline = []
    result = await client.run('capture-two sentinel', model='requested-model', on_timeline=timeline.append)
    assert result.text
    assert requests(trace, 'account/read') == [{'refreshToken': False}] * 2
    assert len(requests(trace, 'initialize')) == len(requests(trace, 'thread/start')) == 1
    turns = requests(trace, 'turn/start')
    assert len(turns) == 1 and turns[0]['model'] == 'requested-model'
    assert 'capture-two sentinel' in turns[0]['input'][0]['text']
    assert not requests(trace, 'account/login/start') and not requests(trace, 'account/logout')
    assert [e['activityTitleId'] for e in timeline if e.get('activityKind') == 'providerRetry'] == [
        'task.provider.retry-session', 'task.provider.session-ready']
    assert client._transport.process is None


@pytest.mark.asyncio
async def test_persistent_routing_failure_is_bounded_and_correctly_classified(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, '_ACCOUNT_READ_RETRY_DELAYS', (.001, .001))
    client, trace = connector(tmp_path, 'account-read-routing-always')
    with pytest.raises(ProviderRequestError) as caught:
        await client.run('capture sentinel')
    assert provider_failure_metadata(caught.value) == dict(
        errorCode='provider-timeout', errorMessageId='task.error.provider-timeout',
        errorMessageArgs=[], retryable=True)
    assert len(requests(trace, 'account/read')) == 3
    assert not requests(trace, 'thread/start') and not requests(trace, 'turn/start')
    assert client._transport.process is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['account-read-auth', 'account-read-invalid', 'account-read-internal', 'signed-out'])
async def test_authentication_configuration_and_unknown_failures_are_not_retried(tmp_path, mode):
    client, trace = connector(tmp_path, mode)
    with pytest.raises((ConnectorError, AuthenticationError)):
        await client.run('capture sentinel')
    assert len(requests(trace, 'account/read')) == 1
    assert not requests(trace, 'turn/start')


@pytest.mark.asyncio
async def test_forced_token_refresh_is_never_automatically_repeated(tmp_path):
    client, trace = connector(tmp_path, 'account-read-routing-always')
    async with client:
        with pytest.raises(ConnectorError):
            await client.account_read(refresh_token=True)
    assert requests(trace, 'account/read') == [{'refreshToken': True}]


@pytest.mark.asyncio
async def test_cancellation_in_account_backoff_starts_no_work(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, '_ACCOUNT_READ_RETRY_DELAYS', (2, 2))
    client, trace = connector(tmp_path, 'account-read-routing-always')
    waiting = asyncio.Event()
    def timeline(event):
        if event.get('activityKind') == 'providerRetry':
            waiting.set()
    task = asyncio.create_task(client.run('capture sentinel', on_timeline=timeline))
    await asyncio.wait_for(waiting.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(requests(trace, 'account/read')) == 1
    assert not requests(trace, 'thread/start') and client._transport.process is None


@pytest.mark.asyncio
async def test_account_retry_budget_cannot_be_extended_by_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, '_ACCOUNT_READ_TOTAL_TIMEOUT', .001)
    client, trace = connector(tmp_path, 'account-read-routing-always')
    with pytest.raises(ProviderRequestError):
        await client.run('capture sentinel')
    assert len(requests(trace, 'account/read')) == 1


@pytest.mark.asyncio
async def test_execution_check_uses_the_selected_codex_runtime_settings(tmp_path, monkeypatch):
    from replaylab_agent import connection_readiness as readiness, desktop_capabilities, process_env
    from replaylab_agent.profiles import Profile
    profile = Profile('probe', 'codex', model='chosen-model')
    seen = {}
    class Client:
        def __init__(self, command, **options):
            seen.update(command=command, **options)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def run(self, prompt, **options):
            seen['run'] = options
            return SimpleNamespace(text='tool receipt')
    monkeypatch.setattr(codex, 'CodexAppServerConnector', Client)
    monkeypatch.setattr(process_env, 'external_agent_environment', lambda *a, **k: {'CODEX_HOME': str(tmp_path)})
    monkeypatch.setattr(process_env, 'resolved_agent_argv', lambda *a: ['codex', 'app-server'])
    caps = desktop_capabilities.DesktopCapabilities(tmp_path)
    monkeypatch.setattr(desktop_capabilities, 'discover_desktop_capabilities', lambda: caps)
    monkeypatch.setattr(desktop_capabilities, 'codex_runtime_overrides', lambda *a: ['features.fixture=true'])
    monkeypatch.setattr(desktop_capabilities, 'codex_runtime_config', lambda *a, **k: {'features': {'fixture': True}})
    config = SimpleNamespace(inherit_desktop_capabilities=True, mcp_servers=(), runtime_disabled_skill_paths=())
    server = {'command': 'test-mcp', 'args': [], 'env': {}}
    await readiness._model_probe(profile, server, 'challenge', tmp_path, 5, tools_config=config)
    assert seen['inherit_desktop_capabilities'] is True
    assert seen['desktop_runtime_config'] == {'features': {'fixture': True}}
    assert 'features.fixture=true' in seen['command']
    assert 'features.plugins=false' not in seen['command']
    assert seen['run']['model'] == 'chosen-model'
    assert seen['expected_mcp_servers'] == {'bread'}


@pytest.mark.asyncio
async def test_account_rpc_timeout_uses_the_same_read_only_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, '_ACCOUNT_READ_RETRY_DELAYS', (.001, .001))
    client, trace = connector(tmp_path, 'success')
    original = client._transport.request
    reads = 0
    async def request(method, params, **options):
        nonlocal reads
        if method == 'account/read':
            reads += 1
            if reads == 1:
                raise ConnectorError('Codex app-server timed out during account/read') from TimeoutError()
        return await original(method, params, **options)
    monkeypatch.setattr(client._transport, 'request', request)
    result = await client.run('current capture', model='same-model')
    assert result.text and reads == 2
    assert len(requests(trace, 'thread/start')) == len(requests(trace, 'turn/start')) == 1


@pytest.mark.asyncio
async def test_acp_execution_check_preserves_the_configured_inheritance_mode(tmp_path, monkeypatch):
    from replaylab_agent import connection_readiness as readiness, native_vendor, process_env, acp_session
    from replaylab_agent.profiles import Profile
    profile = Profile('probe', 'claude-code-acp', model='chosen-model', options={'allow_unsandboxed_agent': True})
    seen = {}
    class Session:
        def __init__(self, **options): seen.update(options)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def prompt(self, prompt): return SimpleNamespace(final_text='tool receipt')
    monkeypatch.setattr(acp_session, 'ACPMainAgentSession', Session)
    monkeypatch.setattr(process_env, 'external_agent_environment', lambda *a, **k: {})
    monkeypatch.setattr(process_env, 'resolved_agent_argv', lambda *a: ['test-agent'])
    monkeypatch.setattr(native_vendor, 'prepare_workspace', lambda *a, **k: seen.update(prepared=k))
    monkeypatch.setattr(native_vendor, 'runtime_argv', lambda *a, **k: seen.update(argvOptions=k) or ['test-agent'])
    monkeypatch.setattr(native_vendor, 'session_meta', lambda *a, **k: k)
    config = SimpleNamespace(inherit_desktop_capabilities=True)
    await readiness._model_probe(profile, dict(command='test-mcp', args=[], env={}), 'challenge',
                                  tmp_path, 5, tools_config=config)
    assert seen['prepared']['inherit_desktop_capabilities'] is True
    assert seen['argvOptions']['inherit_desktop_capabilities'] is True
    assert seen['session_meta']['inherit_desktop_capabilities'] is True
    assert seen['model'] == 'chosen-model'
    assert seen['allowed_tools'] == frozenset({'bread_connection_probe'})


def test_session_recovery_progress_is_localized_only_for_the_codex_connector():
    from replaylab_agent.native_engine import NativeAgentEngine
    rows = []
    reporter = SimpleNamespace(backend='codex-app-server', profile_id='test',
        writer=SimpleNamespace(emit=lambda *a, **k: rows.append(k['data'])))
    retry = dict(activityId='session', activityKind='providerRetry', status='running',
                 activityTitleId='task.provider.retry-session', activityTitleArgs=[.5, 1, 2])
    NativeAgentEngine._project_provider_timeline(reporter, retry, source='codex-app-server')
    assert rows[-1]['activityTitleId'] == 'task.provider.retry-session'
    assert rows[-1]['statusSource'] == 'local'
    ready = {**retry, 'status': 'completed', 'activityTitleId': 'task.provider.session-ready', 'activityTitleArgs': []}
    NativeAgentEngine._project_provider_timeline(reporter, ready, source='codex-app-server')
    assert rows[-1]['activityTitleId'] == 'task.provider.session-ready'
    NativeAgentEngine._project_provider_timeline(reporter, retry, source='acp')
    assert rows[-1]['activityTitleId'] == 'task.detail.public-summary'
