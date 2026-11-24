"""Manual observations survive the Host-to-Bridge completed-result boundary."""
import copy
import os

import pytest

from test_project_restore_client import client_module, _restore_result


def manual_result(mixed=False):
    result = _restore_result()
    item = dict(status='applied', source='reuse-plan',
        workspacePath=os.path.abspath('project/manual-workspace'), workspaceId='9' * 32,
        resourceId=65557, stage='fragment', replacementId='a' * 32, knowledgeId='b' * 32,
        idempotentlyAdopted=False, reusedValidation=False,
        sourceSha256='c' * 64, candidateRawSha256='d' * 64, candidateRevision=31,
        projectSourceSha256='c' * 64, projectCandidateRawSha256='',
        sourcePreparationOnly=True, localValidatedCandidate=False,
        pairValidationCache=dict(hit=False, written=False, reason='manual-edit-unvalidated'))
    items = [item]
    if mixed:
        other = copy.deepcopy(item)
        other.update(workspacePath=os.path.abspath('project/strict-workspace'), workspaceId='8' * 32,
            resourceId=72473, replacementId='e' * 32, knowledgeId='f' * 32,
            reusedValidation=True, pairValidationCache=dict(hit=True, written=True))
        items.append(other)
    result['shaders'].update(attempted=len(items), applied=len(items), items=items)
    result['realizedReceipts'].update(produced=len(items), mutationsCreated=len(items),
        reusedValidation=int(mixed), manualEdit=1)
    result['cache']['reason'] = 'manual-project-source-and-binary'
    result['editing'] = dict(attempted=len(items), enabled=len(items), failed=0,
        items=[dict(status='enabled', workspacePath=row['workspacePath'], workspaceId=row['workspaceId'],
            liveEditId=str(i + 1) * 32, watchId=str(i + 3) * 32,
            captureBinding=dict(instanceId='6260-current', capturePath=result['capture']['path'],
                captureSessionId=result['capture']['sessionId'])) for i, row in enumerate(items)])
    return result


def validate(value):
    return client_module.ReplayLabRestoreHostClient._result(value,
        value['project']['root'], value['capture']['path'], value['capture']['sessionId'])


@pytest.mark.parametrize('mixed', [False, True])
def test_manual_and_mixed_completed_restore_preserve_editor_resume(mixed):
    value = manual_result(mixed)
    assert validate(value) is value
    assert value['editing']['enabled'] == 1 + int(mixed)
    assert value['realizedReceipts']['manualEdit'] == 1
    assert value['index']['written'] is False


@pytest.mark.parametrize('failure', ['unknown-count', 'unknown-pair-field', 'unknown-reason',
    'boolean-count', 'wrong-count', 'missing-count', 'reused-manual', 'manual-cache-hit',
    'manual-cache-written', 'persisted-manual', 'verified-manual', 'missing-candidate'])
def test_manual_result_cannot_claim_strict_receipts_or_unknown_fields(failure):
    value = manual_result(True)
    item, counts = value['shaders']['items'][0], value['realizedReceipts']
    if failure == 'unknown-count':
        counts['futureReceipt'] = 1
    elif failure == 'unknown-pair-field':
        item['pairValidationCache']['extra'] = True
    elif failure == 'unknown-reason':
        item['pairValidationCache']['reason'] = 'future-unvalidated'
    elif failure == 'boolean-count':
        counts['manualEdit'] = True
    elif failure == 'wrong-count':
        counts['manualEdit'] = 0
    elif failure == 'missing-count':
        counts.pop('manualEdit')
    elif failure == 'reused-manual':
        item['reusedValidation'] = True
        counts['reusedValidation'] = 2
    elif failure == 'manual-cache-hit':
        item['pairValidationCache']['hit'] = True
    elif failure == 'manual-cache-written':
        item['pairValidationCache']['written'] = True
    elif failure == 'persisted-manual':
        counts['persisted'] = 1
    elif failure == 'verified-manual':
        value['verifiedRdcCache'] = dict(hit=False, written=True, reason='validated-content-snapshot')
    else:
        item.pop('candidateRawSha256')
    with pytest.raises(client_module.ReplayLabRestoreClientError):
        validate(value)
