"""Manual project source remains visible without inventing ABI evidence."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from src import projects, project_manual_shader
import test_shader_console as fixtures

console = fixtures.console


def manual_status(fixture, capture_id, resource_id, raw, code):
    status = fixture.workspace_status('manual-writer', capture_id, resource_id, raw, code)
    fixture.add_validated_candidate(status, fixtures._writer_dxbc(b'manual saved candidate'))
    latest = status['latestCompiled']
    receipt = project_manual_shader.receipt_identity({
        'schema': project_manual_shader.RECEIPT_SCHEMA, 'schemaVersion': 1,
        'validationScope': project_manual_shader.SCOPE, 'manualEdit': True, 'replayAbiValidated': False,
        'originalRawSha256': raw, 'candidateRawSha256': latest['rawSha256'],
        'sourceSha256': latest['sourceSha256'], 'sourceRevision': status['revision'],
        'stage': status['stage'], 'entryPoint': status['entryPoint'],
        'targetProfile': status['targetProfile'], 'api': 'd3d11', 'encoding': 'dxbc'})
    status.update(manualApplyReceipt=receipt, replayAbiValidated=False, abiCompatible=False)
    latest.update(replayAbiValidated=False, abiCompatible=False, validationScope=project_manual_shader.SCOPE,
                  manualFastCompile=True, abiGate={'status': 'skipped-manual-edit'})
    manifest_path = Path(status['manifestPath'])
    manifest = json.loads(manifest_path.read_text(encoding='utf8'))
    manifest.update(workspaceId=status['workspaceId'])
    manifest['latestCompiled'].update({key: latest[key] for key in
        ('replayAbiValidated', 'abiCompatible', 'validationScope', 'manualFastCompile', 'abiGate')})
    manifest_path.write_text(json.dumps(manifest), encoding='utf8')
    # The writer and reader must succeed even with no ABI report to borrow.
    for path in Path(status['workspacePath']).glob('revisions/*/abi-report.json'):
        path.unlink()
    return status


@pytest.mark.parametrize('inventory_backed', [False, True])
def test_real_manual_project_writer_round_trips_through_ui_library(tmp_path, inventory_backed):
    fixture = fixtures.WriterProjectShaderLibraryFixture(tmp_path)
    if inventory_backed:
        fp = fixtures.fingerprint_bytes(fixtures._writer_dxbc(b'captured original'))
        inventory = fixture.inventory(909, fp.raw_sha256)
        status = manual_status(fixture, inventory['capture']['captureId'], 909, fp.raw_sha256, fp.code_sha256)
        binding = fixture.service.reuse_snapshot_for_shader_inventory(inventory)['binding']
        saved = fixture.service.register_workspace_from_inventory(
            status, inventory['shaders'][0], inventory, binding)['knowledge']
        assert console._project_inventory_workspace_artifact_identity(saved) == projects._inventory_workspace_artifact_identity(saved)
    else:
        catalog, shader, capture_id = fixture.catalog()
        fixture.service.import_capture(catalog)
        status = manual_status(fixture, capture_id, shader['shaderResourceId'], shader['rawSha256'], shader['codeSha256'])
        saved = fixture.service.register_workspace(status, include_compiled_candidate=True,
                                                  require_current_candidate=True)['knowledge']
    value = saved['compiledCandidate']
    assert console._project_compiled_candidate_identity(value) == projects._compiled_candidate_identity(value)
    assert not any(key in value for key in ('originAbiReportSha256', 'originCandidateAbiSha256', 'originRequestId'))
    snapshot = console.load_project_shader_library_snapshot(str(fixture.selection))
    assert snapshot['available'] and not snapshot['errors'], snapshot['errors']
    assert len(snapshot['documents']) == 1
    document = snapshot['documents'][0]
    assert document['compiledCandidateAvailable'] is True
    assert document['sourceSha256'] == hashlib.sha256(Path(status['source']['path']).read_bytes()).hexdigest()
    assert document['knowledgeId'] == saved['knowledgeId']
    loaded = console.load_project_shader_source_document(str(fixture.selection),
        document['projectWorkspaceId'], document['projectId'], document['knowledgeId'], document['sourceSha256'])
    assert Path(loaded['sourcePath']).read_bytes() == Path(status['source']['path']).read_bytes()


@pytest.mark.parametrize('field,value', [('replayAbiValidated', True), ('sourceSha256', 'f' * 64),
    ('candidateRawSha256', 'f' * 64), ('sourceRevision', 999), ('schemaVersion', 999)])
def test_manual_receipt_never_accepts_changed_source_or_strict_claim(tmp_path, field, value):
    fixture = fixtures.WriterProjectShaderLibraryFixture(tmp_path)
    catalog, shader, capture_id = fixture.catalog()
    fixture.service.import_capture(catalog)
    status = manual_status(fixture, capture_id, shader['shaderResourceId'], shader['rawSha256'], shader['codeSha256'])
    saved = fixture.service.register_workspace(status, include_compiled_candidate=True)['knowledge']
    candidate = copy.deepcopy(saved['compiledCandidate'])
    candidate['manualApplyReceipt'][field] = value
    with pytest.raises(console.ShaderConsoleError):
        console._project_compiled_candidate_identity(candidate)
