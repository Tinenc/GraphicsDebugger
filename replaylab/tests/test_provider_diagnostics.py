from types import SimpleNamespace

import httpx
import pytest

from replaylab_agent.agent_progress import ProgressReporter
from replaylab_agent.draft_recovery_reporting import emit_draft_recovery
from replaylab_agent.errors import AuthenticationError, ConnectorError, ProviderRequestError
from replaylab_agent.native_engine import NativeAgentEngine
from replaylab_agent.provider_diagnostics import provider_failure_metadata


@pytest.mark.parametrize('status,body,expected,retryable', [
    (401, {}, 'provider-authentication', False),
    (403, {}, 'provider-permission', False),
    (429, {'error': {'code': 'insufficient_quota'}}, 'provider-quota', False),
    (429, {'code': 'organization_usage_limit_exceeded'}, 'provider-quota', False),
    (None, {'type': 'insufficient_quota', 'code': 'billing_error'}, 'provider-quota', False),
    (None, {'message': 'You have no credits remaining.'}, 'provider-quota', False),
    (None, {'type': 'server_error'}, 'provider-unavailable', True),
    (429, {}, 'provider-rate-limit', True),
    (400, {'code': 'context_length_exceeded'}, 'provider-context', False),
    (404, {'code': 'model_not_found'}, 'provider-model-unavailable', False),
    (400, {}, 'provider-configuration', False),
    (503, {}, 'provider-unavailable', True),
])
def test_classification_preserves_actual_sdk_error_through_wrappers(status, body, expected, retryable):
    inner = RuntimeError('server diagnostic api_key=secret-value')
    inner.status_code, inner.body = status, body
    outer = ConnectorError('API agent failed')
    outer.__cause__ = inner
    result = provider_failure_metadata(outer)
    assert result['errorCode'] == expected
    assert result['retryable'] is retryable
    assert 'secret-value' not in str(result)
    assert 'body' not in result


def test_classified_capacity_is_distinct_from_authentication_and_quota():
    error = ProviderRequestError('Selected model is at capacity', error_code='provider-capacity', retryable=True)
    assert provider_failure_metadata(error)['errorCode'] == 'provider-capacity'
    assert provider_failure_metadata(AuthenticationError('login rejected'))['errorCode'] == 'provider-authentication'
    assert provider_failure_metadata(httpx.ReadTimeout('stream stalled'))['errorCode'] == 'provider-timeout'
    assert provider_failure_metadata(httpx.ReadError('stream broken'))['errorCode'] == 'provider-transport'


@pytest.mark.parametrize('sdk_wrapper', [False, True])
def test_gateway_exhausted_account_pool_is_not_misreported_as_key_permission(sdk_wrapper):
    body = {'error': {'message': 'All available accounts exhausted', 'type': 'server_error'}}
    response = httpx.Response(403, json=body, request=httpx.Request('POST', 'https://gateway.example/chat/completions'))
    error = httpx.HTTPStatusError('Forbidden', request=response.request, response=response)
    if sdk_wrapper:
        wrapped = ConnectorError('deepagents request failed')
        wrapped.__cause__ = error
        error = wrapped
    result = provider_failure_metadata(error)
    assert result['errorCode'] == 'provider-capacity' and result['retryable']


@pytest.mark.parametrize('status,code,expected', [(401, '', 'provider-authentication'),
    (403, 'invalid_api_key', 'provider-authentication'), (403, 'insufficient_quota', 'provider-quota')])
def test_pool_message_never_overrides_explicit_credentials_or_quota(status, code, expected):
    error = RuntimeError('gateway error')
    error.status_code = status
    error.body = {'message': 'All available accounts exhausted', 'code': code}
    assert provider_failure_metadata(error)['errorCode'] == expected


@pytest.mark.parametrize('status', [400, 404, 422])
def test_capacity_text_cannot_override_structured_invalid_request(status):
    error = RuntimeError('Selected model is at capacity.')
    error.status_code = status
    error.body = {'code': 'invalid_request_error'}
    result = provider_failure_metadata(error)
    assert result['errorCode'] == 'provider-configuration'
    assert result['retryable'] is False


def test_progress_failure_keeps_structured_reason_and_original_diagnostic():
    events = []
    reporter = ProgressReporter(SimpleNamespace(emit=lambda *a, **k: events.append((a, k))),
                                profile_id='example', backend='codex-app-server', strategy='test')
    cause = ProviderRequestError('capacity', error_code='provider-capacity', retryable=True)
    reporter.failed('bounded technical diagnostic', cause=cause)
    data = events[0][1]['data']
    assert data['errorMessageId'] == 'task.error.provider-capacity'
    assert data['terminalStatus'] == 'failed'
    assert data['error'] == 'bounded technical diagnostic'


def test_recovered_draft_is_completed_even_when_provider_turn_failed():
    events = []
    reporter = SimpleNamespace(writer=SimpleNamespace(emit=lambda *a, **k: events.append((a, k))))
    emit_draft_recovery(reporter, {'schema': 'replaylab-shader-draft-recovery/v1',
                                 'status': 'recovered', 'manifestPath': 'draft.json'})
    assert events[0][1]['data']['status'] == 'completed'
    assert events[0][1]['data']['lifecycle'] == 'completed'


def test_capacity_progress_retains_localized_id_only_on_codex_retry_activity():
    events = []
    reporter = SimpleNamespace(backend='codex-app-server', profile_id='example',
                               writer=SimpleNamespace(emit=lambda *a, **k: events.append((a, k))))
    event = {'activityId': 'retry-1', 'activityKind': 'providerRetry',
             'activityTitleId': 'task.provider.retry-capacity', 'activityTitleArgs': [2, 1, 2],
             'activityTarget': 'retry', 'status': 'running'}
    NativeAgentEngine._project_provider_timeline(reporter, event, source='codex-app-server')
    assert events[-1][1]['data']['activityTitleId'] == 'task.provider.retry-capacity'
    NativeAgentEngine._project_provider_timeline(reporter, event, source='acp')
    assert events[-1][1]['data']['activityTitleId'] == 'task.detail.public-summary'
