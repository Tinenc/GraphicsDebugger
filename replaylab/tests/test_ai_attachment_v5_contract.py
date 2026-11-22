from __future__ import annotations

import json
import importlib.util
from pathlib import Path

import pytest

from replaylab_agent.conversation import (
    AttachmentContext,
    ConversationAttachment,
    ConversationEnvelope,
    ProviderPathAttachment,
    prepare_provider_conversation_input,
    render_conversation_prompt,
)


def _workspace_module():
    path = Path(__file__).resolve().parents[1] / "renderdoc_mcp_bridge/project_shader_workspace.py"
    spec = importlib.util.spec_from_file_location("bread_selection_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _reference_prompt(context, content):
    return render_conversation_prompt(ConversationEnvelope.model_validate({
        "schema": "replaylab-conversation/v5",
        "request": {"role": "user", "content": "Explain this selection; do not edit it",
                    "attachments": [{"id": "selection", "kind": "text", "title": "Selection",
                                     "mediaType": "application/json", "encoding": "utf-8",
                                     "content": content, "originalBytes": len(content.encode("utf-8")),
                                     "context": context}]},
    }))


@pytest.mark.parametrize("provider", ["ACP", "OpenAI Responses", "Codex app-server"])
def test_workspace_selection_preserves_large_id_source_and_location_across_providers(provider):
    module = _workspace_module()
    document = {
        "workspaceId": "a" * 32, "entryPoint": "MainPS", "rawSha256": "b" * 64,
        "dirty": True, "captureBinding": {"captureSessionId": "c" * 32, "capturePath": "D:/captures/1.rdc"},
        "workspace": {"resourceId": 1000000000000000801, "shaderRawSha256": "d" * 64,
                      "captureId": "e" * 64, "stage": "fragment", "workspacePath": "D:/workspace"},
    }
    context, facts = module._selection_attachment(
        document, "EvaluateSurfaceAppearance(geometry, material, lighting)", "D:/workspace/source/shader.hlsl",
        2989, 2989, 61408, 61408, [61408, 61990], {"columnStart": 18, "columnEnd": 72},
        "// surrounding function\n" * 3000)
    prompt = _reference_prompt(context, json.dumps(facts))
    provider_input = prepare_provider_conversation_input(prompt, provider_name=provider)
    request = json.loads(provider_input.text)["conversation"]["request"]
    sent = request["attachments"][0]
    assert sent["context"]["shader"]["resourceId"] == "1000000000000000801"
    assert sent["context"]["shader"]["rawSha256"] == "d" * 64
    assert sent["context"]["capture"]["captureSessionId"] == "c" * 32
    assert sent["context"]["capture"]["eventId"] == 61408
    assert sent["context"]["origin"]["range"] == {"lineStart": 2989, "lineEnd": 2989, "columnStart": 18, "columnEnd": 72}
    assert json.loads(sent["content"])["savedSourceSha256"] == "b" * 64
    assert json.loads(sent["content"])["unsavedChanges"] is True
    assert "EvaluateSurfaceAppearance(geometry, material, lighting)" not in sent["content"]
    assert "// surrounding function" not in sent["content"]
    assert json.loads(sent["content"])["readSource"]["tool"] == "read_shader_workspace_source"
    assert "retrieve the referenced revision" in provider_input.text
    assert request["content"] == "Explain this selection; do not edit it"


def test_workspace_selection_does_not_bind_unrelated_current_eid():
    context, _ = _workspace_module()._selection_attachment(
        {"workspaceId": "a" * 32, "workspace": {"resourceId": 99}},
        "main()", "shader.hlsl", 1, 1, 333, 333, [7, 12], source_text="main()")
    assert context["capture"] == {"eventId": 7, "selectedEventId": 333}


@pytest.mark.parametrize("provider", ["ACP", "OpenAI Responses", "Codex app-server"])
def test_resource_selection_keeps_binding_and_uint64_offsets(provider):
    context = {
        "schema": "replaylab-attachment-context/v1",
        "origin": {"kind": "resource-selection", "panel": "cbuffer-viewer"},
        "capture": {"eventId": 61408, "captureSessionId": "c" * 32},
        "resource": {"resourceId": "1000000000000000801", "kind": "buffer", "name": "CB5",
                     "byteOffset": "9007199254740993", "byteSize": "16", "fieldPath": "baseTint",
                     "fieldName": "baseTint", "componentByteOffsets": ["9007199254740993", "9007199254740997"],
                     "baseType": "float", "components": 2, "binding": 5, "registerSpace": 0,
                     "arrayIndex": 0, "blockIndex": 4},
    }
    provider_input = prepare_provider_conversation_input(_reference_prompt(context, '{"values":[1,0.5]}'), provider_name=provider)
    sent = json.loads(provider_input.text)["conversation"]["request"]["attachments"][0]
    assert sent["context"]["resource"]["byteOffset"] == "9007199254740993"
    assert sent["context"]["resource"]["fieldPath"] == "baseTint"
    assert sent["trust"] == "untrusted"


@pytest.mark.parametrize("resource_id", [1000000000000000801, 1e18])
def test_resource_selection_rejects_lossy_numeric_identity(resource_id):
    with pytest.raises(ValueError):
        AttachmentContext.model_validate({
            "schema": "replaylab-attachment-context/v1",
            "resource": {"resourceId": resource_id, "kind": "texture"},
        })


@pytest.mark.parametrize("resource", [
    {"resourceId": "ResourceId::10125244", "kind": "buffer", "components": 0,
     "name": "/Game/Materials/Character", "fieldPath": "nested.struct", "baseType": "Unknown"},
    {"resourceId": "ResourceId::333", "kind": "texture", "mip": 0, "slice": 1, "sample": 0},
])
def test_native_struct_and_texture_context_contract(resource):
    value = AttachmentContext.model_validate({
        "schema": "replaylab-attachment-context/v1", "resource": resource,
        "origin": {"kind": "resource-selection", "panel": "pipeline-state-viewer"},
    })
    assert value.resource.resource_id == resource["resourceId"]


def test_cpp_shader_selection_context_accepts_opaque_resource_and_stage_relation() -> None:
    context = AttachmentContext.model_validate(
        {
            "schema": "replaylab-attachment-context/v1",
            "origin": {
                "kind": "shader-disassembly-selection",
                "panel": "shader-viewer",
                "sourceId": "shader-source-" + "a" * 64,
                "sourceName": "Disassembly",
                "range": {"lineStart": 1, "columnStart": 1, "lineEnd": 2, "columnEnd": 3},
            },
            "capture": {"eventId": 7, "selectedEventId": 7},
            "shader": {"stage": "PS", "resourceId": "ResourceId::99"},
            "relations": [
                {
                    "id": "shader-binding-" + "b" * 64,
                    "kind": "bound-at-event",
                    "targetId": "ResourceId::pipe",
                    "usage": "PS",
                }
            ],
        }
    )
    assert context.shader is not None
    assert context.shader.resource_id == "ResourceId::99"
    assert context.relations[0].usage == "PS"


def test_path_reference_is_host_pinned_and_hidden_from_non_codex_projection(tmp_path: Path) -> None:
    path = tmp_path / "selected.hlsl"
    path.write_text("float4 main() : SV_Target { return 1; }", encoding="utf-8")
    details = path.stat()
    attachment = {
        "id": "selected",
        "kind": "file-ref",
        "title": "selected.hlsl",
        "mediaType": "text/x-hlsl",
        "source": "file-picker",
        "encoding": "path",
        "content": str(path),
        "originalBytes": details.st_size,
        "modifiedUnixMs": details.st_mtime_ns // 1_000_000,
        "truncated": False,
        "trust": "untrusted",
    }
    envelope = ConversationEnvelope.model_validate(
        {"schema": "replaylab-conversation/v5", "request": {"role": "user", "content": "Inspect it", "attachments": [attachment]}}
    )
    prompt = render_conversation_prompt(envelope)
    provider = prepare_provider_conversation_input(prompt, provider_name="ACP")
    assert str(path) not in provider.text
    assert json.loads(provider.text)["conversation"]["request"]["attachments"][0]["delivery"] == "metadata-only-unsupported"
    pinned = ProviderPathAttachment.from_conversation_attachment(envelope.request.attachments[0])
    assert pinned.path == path.resolve()
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        pinned.revalidate()
