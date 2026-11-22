import subprocess

import pytest
from deepagents.backends.filesystem import FilesystemBackend,_resolve_ripgrep_path
from replaylab_agent.filesystem_backend import Utf8FilesystemBackend
from replaylab_agent.desktop_shell import DesktopShellBackend


@pytest.mark.parametrize('backend_type',[Utf8FilesystemBackend,DesktopShellBackend])
def test_utf8_json_search_survives_chinese_windows_codepage(tmp_path,monkeypatch,backend_type):
    if _resolve_ripgrep_path() is None:pytest.skip('The bundled ripgrep is required')
    (tmp_path/'模型∮.hlsl').write_text('float wet = 1; // 阶段 → 合并\nfloat wet2 = wet;\n',encoding='utf-8')
    monkeypatch.setattr(subprocess,'_text_encoding',lambda:'gbk')
    backend=backend_type(root_dir=tmp_path,virtual_mode=True)
    result=backend.grep('wet',path='/',glob='*.hlsl',max_count=1)
    assert not result.error and result.truncated and len(result.matches)==1
    assert result.matches[0]['path']=='/模型∮.hlsl'
    assert '阶段 → 合并'in result.matches[0]['text']
    with pytest.raises(UnicodeDecodeError):
        FilesystemBackend(root_dir=tmp_path,virtual_mode=True).grep('wet',path='/')


@pytest.mark.asyncio
async def test_async_utf8_search_keeps_globs_and_native_sdk_file_tools(tmp_path,monkeypatch):
    (tmp_path/'阶段.hlsl').write_text('float wet = 1; // 渲染\n',encoding='utf-8')
    (tmp_path/'unrelated.txt').write_text('wet',encoding='utf-8')
    monkeypatch.setattr(subprocess,'_text_encoding',lambda:'gbk')
    backend=Utf8FilesystemBackend(root_dir=tmp_path,virtual_mode=True)
    result=await backend.agrep('wet',path='/',glob='*.hlsl',max_count=10)
    assert not result.error and not result.truncated
    assert len(result.matches)==1 and result.matches[0]['path']=='/阶段.hlsl'
