from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest

from src import server


def test_generated_image_is_admitted_only_from_current_owned_workspace(tmp_path,monkeypatch):
    owned=tmp_path/'owned';owned.mkdir()
    image=owned/'field.png';image.write_bytes(b'controlled-image')
    outside=tmp_path/'outside.png';outside.write_bytes(b'private')
    monkeypatch.setenv('REPLAYLAB_AGENT_WORKSPACE_ROOT',str(owned))
    monkeypatch.setattr(server,'_evidence_roots',lambda:[])
    assert server._within_evidence_roots(str(image))==image
    with pytest.raises(ValueError,match='outside'):
        server._within_evidence_roots(str(outside))
    monkeypatch.delenv('REPLAYLAB_AGENT_WORKSPACE_ROOT')
    with pytest.raises(ValueError,match='outside'):
        server._within_evidence_roots(str(image))


PS_723 = 1000000000000000723
PS_755 = 1000000000000000755


def test_external_mcp_resource_schema_and_invocation_preserve_exact_decimal_string():
    # Exercise the actual deferred external MCP registration, metadata, argument
    # validation and wrapper, replacing only the final IPC call.
    proxy = server._DeferredFastMCP()
    proxy.tool()(server.get_texture_info)
    application = proxy._build()
    tools = asyncio.run(application.list_tools())
    schema = tools[0].inputSchema["properties"]["resource_id"]
    assert {branch["type"] for branch in schema["anyOf"]} == {"integer", "string"}

    with mock.patch.object(server, "_call", return_value=json.dumps({"found": True})) as call:
        asyncio.run(application.call_tool("get_texture_info", {"resource_id": str(PS_723)}))
        call.assert_called_once_with("get_texture_info", {"resource_id": PS_723})
        assert type(call.call_args.args[1]["resource_id"]) is int


def test_sync_resource_wrapper_preserves_python_integers_and_unrelated_arguments():
    def resource_probe(resource_id: int, event_id: int) -> tuple[int, int]:
        return resource_id, event_id

    wrapped = server._accept_string_resource_ids(resource_probe)
    assert wrapped(PS_723, 11384) == (PS_723, 11384)
    assert wrapped(str(PS_755), 11384) == (PS_755, 11384)


def test_uv_selection_enum_survives_resource_id_transport_and_instrumentation():
    proxy = server._DeferredFastMCP()
    proxy.tool()(server.get_mesh_uv_link)
    proxy.tool(structured_output=False)(server.capture_mesh_uv_evidence)
    tools = asyncio.run(proxy._build().list_tools())
    assert {tool.name for tool in tools} == {'get_mesh_uv_link', 'capture_mesh_uv_evidence'}
    for tool in tools:
        properties = tool.inputSchema['properties']
        assert properties['selection_mode']['enum'] == ['point', 'edge', 'face']
        expected = {'integer', 'string', 'null'} if tool.name == 'capture_mesh_uv_evidence' else {'integer', 'string'}
        assert {branch['type'] for branch in properties['texture_resource_id']['anyOf']} == expected


def test_async_optional_resource_wrapper_preserves_none_and_legacy_resource_prefix():
    async def resource_probe(texture_id: int | None = None) -> int | None:
        return texture_id

    wrapped = server._accept_string_resource_ids(resource_probe)
    assert asyncio.run(wrapped()) is None
    assert asyncio.run(wrapped(None)) is None
    assert asyncio.run(wrapped("ResourceId::" + str(PS_723))) == PS_723


def test_mesh_evidence_requires_an_explicit_texture_choice_when_inputs_are_bound():
    observed = dict(target=dict(kind='uv', eventId=1291, textureResourceId='0'),
                    channelChoices=['_input1'], textureResourceIds=[66250, 4047])
    context = json.dumps(dict(capture=dict(captureSessionId='a'*32)))
    with mock.patch.object(server, 'get_current_renderdoc_context', return_value=context), \
         mock.patch.object(server.ipc, 'call', return_value=observed) as call:
        result = json.loads(server.capture_mesh_uv_evidence(1291, inline=False)[0])
    assert result['textureSelectionRequired'] and not result['ready']
    assert result['textureResourceIds'] == [66250, 4047]
    assert call.call_count == 1 and call.call_args.args[0] == 'get_mesh_uv_navigation_target'


@pytest.mark.parametrize("value", [float(PS_723), float("inf"), float("nan"), True])
def test_transport_coercion_rejects_unsafe_floats_and_booleans(value):
    with pytest.raises(ValueError):
        server._coerce_transport_resource_id(value, "resource_id", False)


def test_frozen_shader_resource_identity_never_equates_distinct_large_ids():
    # Both real resources share an IEEE-754 value. Equality of that value must
    # never substitute for equality of the requested captured Shader identity.
    assert float(PS_723) == float(PS_755)
    assert server._coerce_resource_id(str(PS_723), "expected_resource_id") == PS_723
    assert server._resource_id_matches(PS_723, PS_723)
    assert server._resource_id_matches(None, PS_723)
    assert not server._resource_id_matches(PS_723, PS_755)
    assert not server._resource_id_matches(1000000000000000800, PS_723)
    with pytest.raises(ValueError, match="decimal string"):
        server._coerce_resource_id(float(PS_723), "expected_resource_id")
    assert server._coerce_resource_id(17.0, "expected_resource_id") == 17
