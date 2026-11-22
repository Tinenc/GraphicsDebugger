"""Manual source/native bytes survive publication without becoming ABI proof."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from src import project_restore, project_manual_shader, shader_saved_publication as publication
from src.projects import ReplayProjectError, _compiled_candidate_identity
from test_shader_saved_publication import rig, head
from test_active_projects import _dxbc


def prepare_manual(rig, revision=6):
    intent, result = rig.prepare(revision)
    publication.cancel(intent)
    latest = rig.status['latestCompiled']
    latest.update(validationScope='manual-edit-unvalidated', replayAbiValidated=False,
                  abiCompatible=False, requiresReplayAbiValidation=False,
                  abiGate={'status': 'skipped-manual-edit',
                           'baselineAbiSha256': rig.status['baseline']['abiSha256']})
    rig.status.update(replayAbiValidated=False, abiCompatible=False)
    path = Path(rig.status['manifestPath'])
    manifest = json.loads(path.read_text(encoding='utf8'))
    manifest['workspaceId'] = rig.status['workspaceId']
    for key in ('validationScope', 'replayAbiValidated', 'abiCompatible', 'requiresReplayAbiValidation', 'abiGate'):
        manifest['latestCompiled'][key] = copy.deepcopy(latest[key])
    path.write_text(json.dumps(manifest), encoding='utf8')
    # Access to every ABI-validation API fails this test immediately.
    intent = publication.prepare(rig.host, rig.status['workspacePath'], revision,
        latest['rawSha256'], rig.binding, manual_edit=True)
    result['validation'].update(manualEdit=True, replayAbiValidated=False,
        compatible=False, validationScope='manual-edit-unvalidated', abiValidationMode='manual-fast')
    result['replacement'].update(manualEdit=True, replayAbiValidated=False,
        validationScope='manual-edit-unvalidated', abiValidationMode='manual-fast',
        stage=rig.status['stage'], entryPoint=rig.status.get('entryPoint', ''),
        api='d3d11', encoding='dxbc')
    return intent, result


def test_manual_publication_retains_latest_bytes_without_abi_report(rig):
    intent, result = prepare_manual(rig)
    frozen = publication._read(intent)
    assert frozen['manualEdit'] is True
    assert not any(name.endswith('/abi.json') for name in frozen['files'])
    with mock.patch.object(rig.host, '_fresh_shader_candidate_validation', side_effect=AssertionError('no ABI')):
        assert publication.complete(intent, result)['status'] == 'committed'
    current = head(rig)
    binary = current['compiledCandidate']
    assert current['sourceSha256'] == result['binding']['sourceSha256']
    assert binary['rawSha256'] == result['binding']['candidateRawSha256']
    assert binary['validationScope'] == 'manual-edit-unvalidated'
    assert binary['manualApplyReceipt']['replayAbiValidated'] is False
    assert not {'originAbiReportSha256', 'originRequestId', 'originCandidateAbiSha256'} & set(binary)
    # Reopening the project parses the new candidate and still accepts its older strict head.
    assert rig.replay.status()['knowledgeCounts']['workspaces'] == 2
    rig.replay.get_workspace_knowledge(rig.initial['knowledge']['knowledgeId'])
    catalog, unused, capture = rig.f._catalog('manual-next', _dxbc(b'original program'), resource_id=9181)
    rig.replay.import_capture(catalog)
    candidate = rig.replay.reuse_plan(capture)['matches'][0]['workspaceCandidates'][0]
    assert candidate['sourceSha256'] == current['sourceSha256']
    plan = candidate['binaryCandidate']
    assert candidate['binaryCandidateEligible'] is True
    assert plan['manualEdit'] is True
    assert plan['freshLiveAbiRevalidationRequired'] is False
    assert plan['originValidationEvidenceOnly'] is False
    assert plan['applyAuthorization'] is False


@pytest.mark.parametrize('failure', ['wrong-source', 'wrong-binary', 'wrong-original', 'not-applied', 'false-abi-claim'])
def test_manual_publication_rejects_mismatched_success(rig, failure):
    intent, result = prepare_manual(rig)
    if failure == 'wrong-source':
        result['binding']['sourceSha256'] = '0' * 64
    elif failure == 'wrong-binary':
        result['replacement']['candidateRawSha256'] = '0' * 64
    elif failure == 'wrong-original':
        result['replacement']['originalRawSha256'] = '0' * 64
    elif failure == 'not-applied':
        result['applied'] = False
    else:
        result['validation']['replayAbiValidated'] = True
    with pytest.raises((RuntimeError, ValueError)):
        publication.complete(intent, result)
    assert head(rig) == rig.initial['knowledge']
    publication.cancel(intent)


def test_manual_delayed_commit_survives_restart_without_replay(rig):
    intent, result = prepare_manual(rig)
    with mock.patch.object(publication, '_publish', side_effect=OSError('temporary lock')):
        with pytest.raises(OSError):
            publication.complete(intent, result)
    assert publication._read(intent)['state'] == 'ready'
    drained = publication.drain(Path(rig.active['projectRoot']), rig.replay.root)
    assert drained['committed'] == 1
    assert head(rig)['compiledCandidate']['validationScope'] == 'manual-edit-unvalidated'


def test_manual_project_restore_calls_only_manual_apply(rig):
    intent, result = prepare_manual(rig)
    publication.cancel(intent)
    result['replacement'].update(anchorEventId=17)
    apply = mock.Mock(return_value=result)
    host = SimpleNamespace(_shader_live_edit_apply_candidate=apply,
        apply_shader_workspace_shared_resource=mock.Mock(side_effect=AssertionError('strict apply')),
        _fresh_shader_candidate_validation=mock.Mock(side_effect=AssertionError('ABI')))
    observed = project_restore._apply_workspace(host, Path(rig.status['workspacePath']), rig.status,
        expected_revision=6, expected_candidate_raw_sha256=result['binding']['candidateRawSha256'],
        expected_anchor_event_id=17, capture_session_id=rig.binding['captureSessionId'],
        project_root=Path(rig.active['projectRoot']))
    assert observed['validation']['replayAbiValidated'] is False
    assert observed['pairValidationCache']['written'] is False
    apply.assert_called_once()


def test_manual_candidate_cannot_be_relabelled_as_strict_abi(rig):
    intent, result = prepare_manual(rig)
    publication.complete(intent, result)
    binary = head(rig)['compiledCandidate']
    binary['validationScope'] = 'replay-reflection-abi-compatible'
    with pytest.raises(ReplayProjectError):
        _compiled_candidate_identity(binary)


def test_unfinished_manual_publication_never_uses_strict_pair_receipt(rig):
    intent, unused_result = prepare_manual(rig)
    with publication._GUARD:
        publication._INFLIGHT.pop(str(intent))
    with mock.patch.object(project_restore, '_load_workspace_shader_pair_validation',
                           side_effect=AssertionError('manual is not ABI proof')):
        drained = publication.drain(Path(rig.active['projectRoot']), rig.replay.root)
    assert drained['cancelled'] == 1
    assert head(rig) == rig.initial['knowledge']


from test_native_glsl_workspace import glsl_fixture
from tests.test_spirv_workspace import spirv_workspace_fixture


@pytest.mark.parametrize('failure', ['', 'wrong-original', 'wrong-source', 'changed-binary'])
def test_manual_glsl_candidate_import_reload_binds_source_without_abi(glsl_fixture, failure):
    from tests.test_shader_workspace_routes import ShaderWorkspaceRouteChainTests
    from src.shader_workspace.errors import ToolExecutionError
    f = glsl_fixture
    service, root = f['service'], f['root'] / 'manual-import'
    service.create(f['catalogPath'], f['key'], root)
    status = service.status(root)
    artifact = f['root'] / 'manual-candidate.glsl'
    artifact.write_bytes(f['blob'])
    plan = ShaderWorkspaceRouteChainTests._project_candidate_plan(artifact,
        raw_sha256=f['key'].raw_sha256, code_sha256=f['key'].raw_sha256,
        byte_size=len(f['blob']), kind='GLSL', source_sha256=f['key'].raw_sha256,
        source_revision=4, target_profile=status['targetProfile'])
    manifest = json.loads((root / 'manifest.json').read_text())
    plan['compilerBindingSha256'] = service._project_compiler_binding(manifest)['bindingSha256']
    receipt = dict(schema=project_manual_shader.RECEIPT_SCHEMA, schemaVersion=1,
        validationScope=project_manual_shader.SCOPE, manualEdit=True, replayAbiValidated=False,
        originalRawSha256=status['baseline']['rawSha256'], candidateRawSha256=plan['sha256'],
        sourceSha256=plan['sourceSha256'], sourceRevision=4, stage=status['stage'],
        entryPoint=status['entryPoint'], targetProfile=status['targetProfile'], api='opengl', encoding='glsl')
    plan.update(manualEdit=True, validationScope=project_manual_shader.SCOPE, manualApplyReceipt=receipt,
        originValidationEvidenceOnly=False, freshLiveAbiRevalidationRequired=False)
    if failure == 'wrong-original':
        receipt['originalRawSha256'] = '0' * 64
    elif failure == 'wrong-source':
        receipt['sourceSha256'] = '0' * 64
    elif failure == 'changed-binary':
        artifact.write_bytes(b'x' * len(f['blob']))
    with mock.patch.object(service, '_validated_compiled_container', side_effect=AssertionError('no ABI/container work')), \
         mock.patch.object(service, '_ensure_dedicated_baseline_dxil_disassembly', side_effect=AssertionError('no disassembly')):
        if failure:
            with pytest.raises(ToolExecutionError):
                service.hydrate_project_candidate(root, plan)
            assert service.status(root)['latestCompiled'] is None
        else:
            imported = service.hydrate_project_candidate(root, plan)
            assert imported['requiresReplayAbiValidation'] is False
            latest = service.status(root)['latestCompiled']
            assert latest['validationScope'] == project_manual_shader.SCOPE
            assert latest['abiGate']['status'] == 'skipped-manual-edit'
            assert latest['replayAbiValidated'] is False
            assert latest['projectImport']['manualApplyReceipt'] == receipt
            request = service.manual_candidate_apply_request(root)
            assert request['binding']['candidateRawSha256'] == plan['sha256']


def test_manual_restore_complete_transaction_keeps_candidate_unvalidated():
    from test_project_restore import ProjectRestoreTests
    f = ProjectRestoreTests('test_absent_index_inventories_reuses_hlsl_and_writes_local_index')
    f.setUp()
    try:
        def configure(host, replay):
            candidate = host.prepared_response['reusePlan']['matches'][0]['workspaceCandidates'][0]
            binary = candidate['binaryCandidate']
            receipt = dict(schema=project_manual_shader.RECEIPT_SCHEMA, schemaVersion=1,
                validationScope=project_manual_shader.SCOPE, manualEdit=True, replayAbiValidated=False,
                originalRawSha256=f.raw_sha, candidateRawSha256=binary['sha256'],
                sourceSha256=f.source_sha, sourceRevision=1, stage='fragment', entryPoint='main',
                targetProfile=host.target_status['targetProfile'], api='d3d11', encoding='dxbc')
            binary.update(manualEdit=True, validationScope=project_manual_shader.SCOPE,
                manualApplyReceipt=receipt, originValidationEvidenceOnly=False,
                freshLiveAbiRevalidationRequired=False)
            original_import = host.workspace_service.import_handler
            def importer(path, plan):
                imported = original_import(path, plan)
                imported.update(requiresReplayAbiValidation=False)
                latest = host.target_status['latestCompiled']
                latest.update(validationScope=project_manual_shader.SCOPE,
                    replayAbiValidated=False, abiCompatible=False,
                    abiGate={'status': 'skipped-manual-edit'})
                latest['projectImport'].update(manualEdit=True, manualApplyReceipt=receipt,
                    originValidationEvidenceOnly=False, freshLiveAbiRevalidationRequired=False)
                return imported
            host.workspace_service.import_handler = importer
            host.apply_shader_workspace_shared_resource = mock.Mock(side_effect=AssertionError('no strict apply'))
            def apply(path, revision, digest, *, expected_anchor_event_id=None):
                assert expected_anchor_event_id == 9
                status = host.target_status
                host.apply_calls += 1
                return dict(schema='renderdoc-ultra-shader-shared-resource-apply-mcp/v1',
                    scope='captured-resource', applied=True, operationId='3' * 32,
                    binding=dict(revision=revision, candidateRawSha256=digest, sourceSha256=f.source_sha),
                    validation=dict(manualEdit=True, replayAbiValidated=False,
                        validationScope=project_manual_shader.SCOPE),
                    replacement=dict(replacementId='9' * 32, operationId='3' * 32,
                        operationCreatedReplacement=True, originalShaderPreserved=True,
                        configured=True, resourceScoped=True, eventScoped=False,
                        originalResourceId=status['shader']['resourceId'], originalRawSha256=f.raw_sha,
                        candidateRawSha256=digest, stage='fragment', entryPoint='main', api='d3d11', encoding='dxbc',
                        workspaceId=status['workspaceId'], captureId=status['capture']['captureId'],
                        captureSessionId=f.session_id, anchorEventId=9, abiValidationMode='manual-fast'))
            host._shader_live_edit_apply_candidate = apply
            register = replay.inventory_register_handler
            def registered(status, *args):
                assert status['manualApplyReceipt']['replayAbiValidated'] is False
                return register(status, *args)
            replay.inventory_register_handler = registered
        result, host, replay, unused = f._run(index_present=False, auto_prepare=True,
            with_binary=True, host_mutator=configure)
        assert result['completed'], result
        assert result['shaders']['applied'] == 1
        assert host.compile_calls == 0 and host.apply_calls == 1
        assert result['cache']['reason'] == 'manual-project-source-and-binary'
        assert result['index']['written'] is False
        assert result['realizedReceipts']['manualEdit'] == 1
        assert host.target_status['latestCompiled']['replayAbiValidated'] is False
        assert not any(method == 'write_rdc_project_index' for method, unused in host.ipc.calls)
    finally:
        f.tearDown()


def test_manual_prepare_uses_local_imported_identity_without_live_capture_probes(rig):
    intent, unused_result = prepare_manual(rig)
    publication.cancel(intent)
    with mock.patch.object(rig.host, '_workspace_active_capture_context', side_effect=AssertionError('no live probe')), \
         mock.patch.object(rig.host, '_ensure_active_workspace_project_capture', side_effect=AssertionError('no repeated rebind')), \
         mock.patch.object(rig.replay, 'reuse_plan', side_effect=AssertionError('no whole-project matching')):
        intent = publication.prepare(rig.host, rig.status['workspacePath'], rig.status['revision'],
            rig.status['latestCompiled']['rawSha256'], rig.binding, manual_edit=True)
        assert publication._read(intent)['manualEdit'] is True
        publication.cancel(intent)


def test_manual_prepare_still_rejects_different_frozen_capture_path(rig):
    intent, unused_result = prepare_manual(rig)
    publication.cancel(intent)
    binding = dict(rig.binding, capturePath=str(rig.f.root / 'different.rdc'))
    with pytest.raises(RuntimeError, match='capture path changed'):
        publication.prepare(rig.host, rig.status['workspacePath'], rig.status['revision'],
            rig.status['latestCompiled']['rawSha256'], binding, manual_edit=True)


def test_manual_local_head_does_not_reload_or_renormalize_project(rig):
    replay, manifest, catalog_sha = publication._manual_project_snapshot(rig.f.service, rig.status, rig.binding)
    with mock.patch.object(replay, '_load', side_effect=AssertionError('snapshot already loaded')), \
         mock.patch.object(replay, '_capture_status_catalog_sha256', side_effect=AssertionError('catalog already normalized')):
        unused_head, knowledge = publication._head(replay, rig.status,
            manifest=manifest, catalog_sha256=catalog_sha)
    assert knowledge['knowledgeId'] == rig.initial['knowledge']['knowledgeId']
