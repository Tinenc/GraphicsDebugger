import json
from types import SimpleNamespace

from tests.test_shader_workspace_mcp import load_server_module


def test_repair_context_exposes_exact_files_without_replaying_or_compiling(tmp_path, monkeypatch):
    server = load_server_module()
    root = tmp_path/'workspace'; (root/'baseline').mkdir(parents=True); (root/'source').mkdir()
    original = root/'baseline/shader.bin'; original.write_bytes(b'original')
    source = root/'source/shader.hlsl'; source.write_text('saved source')
    value = {'workspacePath':str(root),'workspaceId':'a'*32,'stage':'fragment','entryPoint':'main',
        'baseline':{'path':'baseline/shader.bin','rawSha256':'b'*64,'abiStatus':'unavailable'},
        'source':{'path':str(root/'source/shader.hlsl'),'currentSha256':'c'*64},'capture':{'api':'D3D12'}}
    calls = []
    monkeypatch.setattr(server, '_shader_workspace_artifact_service', lambda: SimpleNamespace(status=lambda path:(calls.append(path) or value)))
    monkeypatch.setattr(server, 'ipc', SimpleNamespace(instance_id='own-instance',call=lambda *a,**k:(_ for _ in ()).throw(AssertionError('No replay'))))
    result = json.loads(server.get_shader_repair_context(str(root)))
    assert 'error' not in result, result
    assert result['instanceId'] == 'own-instance' and result['baseline']['path'] == str(original)
    assert result['source']['path'] == str(source) and len(calls) == 1
    assert not result['grantsExecution'] and not result['applyOrCompilePerformed']
    assert result['ownerConfiguration']['reload'] == 'content-fingerprint'
    assert original.read_bytes() == b'original' and source.read_text() == 'saved source'


def test_repair_context_survives_invalid_historical_execution_metadata(tmp_path, monkeypatch):
    server = load_server_module()
    root = tmp_path/'workspace'; (root/'baseline').mkdir(parents=True); (root/'source').mkdir()
    (root/'baseline/shader.bin').write_bytes(b'original')
    (root/'source/shader.hlsl').write_text('saved source')
    value = {'workspacePath':str(root),'workspaceId':'a'*32,'stage':'fragment','entryPoint':'main',
        'baseline':{'path':'baseline/shader.bin','rawSha256':'b'*64,'abiStatus':'unavailable'},
        'source':{'path':'source/shader.hlsl','sha256':'c'*64},'capture':{'api':'D3D12'}}
    def broken(path): raise RuntimeError('toolchain refresh archive identity changed')
    monkeypatch.setattr(server, '_shader_workspace_artifact_service', lambda: SimpleNamespace(
        status=broken,source_edit_status=lambda path:value))
    monkeypatch.setattr(server, 'ipc', SimpleNamespace(instance_id='own-instance'))
    result = json.loads(server.get_shader_repair_context(str(root)))
    assert result['statusDiagnostic'] == 'toolchain refresh archive identity changed'
    assert result['source']['path'] == str(root/'source/shader.hlsl')
    assert not result['grantsExecution'] and not result['applyOrCompilePerformed']
