from __future__ import annotations

import hashlib
import json

import pytest
from deepagents.backends import FilesystemBackend
from langchain_core.messages import ToolMessage

from replaylab_agent.shader_file_middleware import shader_file_middleware


@pytest.mark.asyncio
@pytest.mark.parametrize("first_party,mode,valid,inline", [
    (True, "complete", True, True), (False, "complete", True, False),
    (True, "range", True, False), (True, "complete", False, False),
])
async def test_complete_file_exception_never_bypasses_other_eviction(tmp_path, first_party, mode, valid, inline):
    from types import SimpleNamespace
    source = "float4 Color;\n" * 10000
    raw = source.encode()
    text = json.dumps({"schema": "renderdoc-ai-shader-workspace-file/v1", "mode": "complete",
        "offset": 0, "text": source, "chars": len(source), "totalChars": len(source), "sizeBytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest() if valid else "0" * 64,
        "coverage": {"byteOffset": 0, "returnedBytes": len(raw), "complete": True}, "truncated": False, "applied": False})
    middleware = shader_file_middleware(FilesystemBackend(root_dir=tmp_path, virtual_mode=True), first_party_reader=first_party)
    request = SimpleNamespace(tool_call={"name": "read_shader_workspace_file", "args": {"mode": mode}, "id": "read"},
                              runtime=None, state={})

    async def handler(request):
        return ToolMessage(content=text, tool_call_id="read", name="read_shader_workspace_file")

    result = await middleware.awrap_tool_call(request, handler)
    assert (result.content == text) is inline
    if not inline:
        assert len(result.content) < len(text)
