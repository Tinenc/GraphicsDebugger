"""Optional multi-engine reads select evidence capabilities, not the first program."""
from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from src import asset_reader_routing as routing, server, tool_providers


def reader(identifier, *, engine="unity", capabilities=None, command=None):
    operation = {"role": "asset-reader", "kind": "cli", "name": identifier + "_read",
                 "argv": ["{path}"], "patterns": ["*"], "proved": {"sample": "fixture"}}
    if capabilities is not None:
        operation["capabilities"] = capabilities
    return {"id": identifier, "roles": ["asset-reader"], "engines": [engine] if engine else [],
            "connection": {"kind": "cli", "command": command or identifier},
            "interface": {"operations": [operation]}}


@pytest.fixture
def context(tmp_path, monkeypatch):
    path = tmp_path / "surface.bundle"
    path.write_bytes(b"UnityFS\x00")
    monkeypatch.setattr(server, "_ASSET_READER_RESULTS", routing.ReaderResultCache())
    monkeypatch.setattr(server, "_load_asset_reader_selection", lambda: None)
    monkeypatch.setattr(server, "_registered_tools", lambda: ())
    monkeypatch.setattr(server, "_unpacked_asset_root", lambda: (tmp_path, {}))
    monkeypatch.setattr(server, "_read_builtin_asset_description", lambda *_args, **_kwargs: {
        "reader": "unity-serialized", "engine": "unity", "kind": "package"})
    return tmp_path, path


@pytest.mark.parametrize("payload", [
    {"kind": "material", "hasMaterials": True},
    {"kind": "package", "objects": [{"name": "M", "class": "Material"}]},
    {"kind": "material", "metadata": {"distinctiveValues": [0.2]}},
    {"kind": "material", "exports": [{"Type": "Material", "Name": "M"}]},
    {"kind": "material", "exports": {"invalid": "shape"}},
    {"kind": "material", "references": True},
])
def test_inventory_and_header_are_not_parameter_evidence(payload):
    assert routing.result_capabilities(payload) == {"classify"}


def test_deep_unity_material_and_reference_evidence_are_recognized():
    payload = {"kind": "unity-bundle", "exports": [{
        "m_SavedProperties": {"m_Floats": [["_Smoothness", 0.5]]},
        "textures": {"_MainTex": {"objectReference": {"sourceFile": "sharedassets", "pathId": 42}}}}]}
    assert routing.result_capabilities(payload) == {"classify", "properties", "references"}
    assert not routing.result_capabilities(dict(payload, succeeded=False))
    assert not routing.result_capabilities(dict(payload, parseStatus="failed"))


def test_candidates_obey_engine_capability_and_deduplicate_invocations():
    a = reader("a")
    duplicate = reader("duplicate", command="a")
    b = reader("b", capabilities=["classify"])
    c = reader("c", engine="unreal")
    generic = reader("generic", engine="")
    found = routing.reader_candidates([a, duplicate, b, c, generic], "surface.bundle",
                                      engine="unity", capability="properties", preferred="duplicate")
    assert [tool["id"] for tool, unused_op in found] == ["duplicate", "generic"]


def test_classification_reuses_builtin_without_external_calls(context, monkeypatch):
    root, path = context
    call = AsyncMock(side_effect=AssertionError("classification does not require an external tool"))
    monkeypatch.setattr(server, "_describe_with_registered_tool", call)
    result = asyncio.run(server._read_unpacked_asset_description(root, path))
    assert result["readerRouting"]["providedCapabilities"] == ["classify"]
    call.assert_not_awaited()


def test_deep_read_uses_project_choice_and_reuses_result(context, monkeypatch):
    root, path = context
    selected = {"assetRoot": str(root), "engine": "unity", "reader": "unity-serialized",
                "selections": {"unity": {"properties": {"reader": "chosen"}}}}
    monkeypatch.setattr(server, "_load_asset_reader_selection", lambda: selected)
    monkeypatch.setattr(server, "_registered_tools", lambda: [reader("alphabetical"), reader("chosen")])
    call = Mock(return_value={"kind": "material", "properties": {"_Roughness": 0.5}})
    monkeypatch.setattr(server, "_run_cli_reader_operation", call)
    first = asyncio.run(server._read_unpacked_asset_description(root, path, deep=True))
    second = asyncio.run(server._read_unpacked_asset_description(root, path, deep=True))
    assert first["reader"] == second["reader"] == "chosen"
    assert second["readerResultReused"]
    assert call.call_count == 1
    assert call.call_args.args[0]["id"] == "chosen"
    path.write_bytes(b"UnityFS\x00changed")
    asyncio.run(server._read_unpacked_asset_description(root, path, deep=True))
    assert call.call_count == 2


def test_legacy_selection_is_applied_to_classification_but_not_deep_reads(context, monkeypatch):
    root, path = context
    monkeypatch.setattr(server, "_load_asset_reader_selection", lambda: {
        "assetRoot": str(root), "engine": "unity", "reader": "selected"})
    monkeypatch.setattr(server, "_registered_tools", lambda: [reader("selected")])
    call = Mock(return_value={"kind": "mesh"})
    monkeypatch.setattr(server, "_run_cli_reader_operation", call)
    result = asyncio.run(server._read_unpacked_asset_description(root, path))
    assert result["reader"] == "selected"
    assert routing.selected_reader({"reader": "selected", "engine": "unity"}, "unity", "properties") == ""


def test_selection_from_another_game_directory_is_ignored(context):
    root, unused_path = context
    assert not routing.selection_for_root({"assetRoot": str(root / "other"), "reader": "wrong"}, root)


def test_missing_or_shallow_reader_preserves_header_and_allows_shader_work(context, monkeypatch):
    root, path = context
    monkeypatch.setattr(server, "_registered_tools", lambda: [reader("header")])
    monkeypatch.setattr(server, "_run_cli_reader_operation", Mock(return_value={"kind": "package"}))
    result = asyncio.run(server._read_unpacked_asset_description(root, path, deep=True))
    assert result["kind"] == "package" and result["reader"] == "unity-serialized"
    assert result["readerStatus"] == "capability-unavailable"
    assert result["nonblocking"] and result["fallback"]["mode"] == "capture-only"
    assert "shader-organization" in result["fallback"]["canContinue"]
    assert result["readerRouting"]["attempts"][0]["providedCapabilities"] == ["classify"]


def test_fallback_is_bounded_deduplicated_and_negative_results_are_reused(context, monkeypatch):
    root, path = context
    monkeypatch.setattr(server, "_registered_tools", lambda: [reader("a"), reader("b"), reader("c")])
    call = Mock(return_value={"readerStatus": "failed", "reason": "unsupported version"})
    monkeypatch.setattr(server, "_run_cli_reader_operation", call)
    result = asyncio.run(server._read_unpacked_asset_description(root, path, deep=True))
    asyncio.run(server._read_unpacked_asset_description(root, path, deep=True))
    assert call.call_count == 2
    assert result["readerRouting"]["eligibleReaders"] == 3
    assert len(result["readerRouting"]["attempts"]) == 2


def test_concurrent_mcp_reads_share_one_invocation(context, monkeypatch):
    from src import mcp_tool_client
    root, path = context
    tool = reader("mcp")
    tool["connection"] = {"kind": "mcp-http", "url": "http://localhost:9999/mcp"}
    operation = tool["interface"]["operations"][0]
    operation.update(kind="mcp", name="read_asset")
    async def answer(*unused_args):
        await asyncio.sleep(0.02)
        return {"kind": "material", "properties": {"_Tint": [1, 0, 0]}}
    call = AsyncMock(side_effect=answer)
    monkeypatch.setattr(mcp_tool_client, "shared_client", lambda: Mock(call=call))
    async def concurrent_reads():
        return await asyncio.gather(*[server._invoke_asset_reader(tool, operation, root, path, path.name)
                                      for unused in range(3)])
    results = asyncio.run(concurrent_reads())
    assert call.await_count == 1
    assert sum(bool(item.get("readerResultReused")) for item in results) == 2


def test_unknown_engine_custom_reader_is_supported(context, monkeypatch):
    root, path = context
    monkeypatch.setattr(server, "_read_builtin_asset_description", lambda *_a, **_k: {
        "kind": "unknown", "reader": "none"})
    monkeypatch.setattr(server, "_registered_tools", lambda: [reader("custom", engine="custom-engine")])
    monkeypatch.setattr(server, "_run_cli_reader_operation", Mock(return_value={
        "engine": "custom-engine", "kind": "material", "properties": {"Edge": 0.4}}))
    result = asyncio.run(server._read_unpacked_asset_description(root, path, deep=True))
    assert result["reader"] == "custom" and result["engine"] == "custom-engine"


def test_verify_checks_each_reader_for_same_capability_and_persists_choice(context, monkeypatch):
    root, path = context
    tools = [reader("a_header"), reader("z_deep")]
    monkeypatch.setattr(tool_providers, "load_tools", lambda: (tools, []))
    monkeypatch.setattr(server, "_registered_tools", lambda: tools)
    monkeypatch.setattr(server, "_sample_unpacked_assets", lambda *_: [path])
    store = Mock(return_value="selection.json")
    monkeypatch.setattr(server, "_store_asset_reader_selection", store)
    def answer(tool, *_args):
        return {"kind": "package"} if tool["id"] == "a_header" else {
            "kind": "material", "engine": "unity", "properties": {"_Value": 4}}
    call = Mock(side_effect=answer)
    monkeypatch.setattr(server, "_run_cli_reader_operation", call)
    result = json.loads(asyncio.run(server.verify_unpacked_asset_readers(capability="properties")))
    assert result["state"] == "ready" and result["selectedReader"] == "z_deep"
    assert [entry["reader"] for entry in result["candidates"]] == ["z_deep"]
    assert call.call_count == 2
    assert store.call_args.args[0]["selections"]["unity"]["properties"]["reader"] == "z_deep"


def test_verify_without_asset_root_is_nonblocking(monkeypatch):
    monkeypatch.setattr(server, "_unpacked_asset_root", Mock(side_effect=RuntimeError("no assets")))
    result = json.loads(asyncio.run(server.verify_unpacked_asset_readers(capability="properties")))
    assert result["state"] == "no-reader" and result["nonblocking"]


def test_material_failure_does_not_retry_uex_bypassing_router(context, monkeypatch):
    unused_root, path = context
    monkeypatch.setattr(server, "_declared_asset_operations", lambda: [])
    monkeypatch.setattr(server, "_describe_with_registered_tool", AsyncMock(return_value={
        "readerStatus": "unavailable", "readerRouting": {"attempts": [{"builtinKind": "uex"}]}}))
    duplicate = Mock(side_effect=AssertionError("a failed UEX read must not be retried"))
    monkeypatch.setattr(server, "_run_uex_preview", duplicate)
    result = asyncio.run(server._read_material_asset(path.name))
    assert not result["succeeded"] and result["nonblocking"]
    duplicate.assert_not_called()


def test_cli_reader_preserves_complete_json_and_connection_options(tmp_path, monkeypatch):
    asset = tmp_path / "asset.bin"
    asset.write_bytes(b"fixture")
    script = tmp_path / "reader.py"
    script.write_text("import json, os\nprint(json.dumps({'padding':'x'*9000, 'kind':'material',"
                      "'properties':{'Name':os.environ['BREAD_ROUTING_TEST']}}))\n", encoding="utf-8")
    tool = reader("custom", engine="", command=sys.executable)
    tool["connection"]["args"] = [str(script)]
    tool["connection"]["env"] = {"BREAD_ROUTING_TEST": "from-connection"}
    monkeypatch.setattr(server, "MAX_AGENT_RESULT_BYTES", 1024)
    result = server._run_cli_reader_operation(tool, tool["interface"]["operations"][0], asset, asset.name)
    assert result["readerStatus"] == "parsed"
    assert result["properties"]["Name"] == "from-connection"
    assert len(result["padding"]) == 9000


def test_mismatched_engine_output_is_not_attached_to_the_asset(context, monkeypatch):
    root, path = context
    monkeypatch.setattr(server, "_registered_tools", lambda: [reader("misconfigured")])
    monkeypatch.setattr(server, "_run_cli_reader_operation", Mock(return_value={
        "kind": "material", "engine": "unreal", "properties": {"WrongAsset": 1}}))
    result = asyncio.run(server._read_unpacked_asset_description(root, path, deep=True))
    assert result["fallback"]["mode"] == "capture-only"
    assert result["readerRouting"]["attempts"][0]["reason"] == "reader-engine-mismatch"


def test_large_unreal_properties_survive_the_bounded_reference_scan():
    properties = {"padding": "x" * 70000, "ScalarParameterValues": [{"Name": "Tail", "Value": 0.5}]}
    result = server._describe_from_uex_exports([
        {"Type": "MaterialInstanceConstant", "Name": "Material", "Properties": properties}], "material.uasset")
    assert result["properties"] == properties
    assert not result["propertiesTruncated"]
    assert result["referencesTruncated"]


def test_generic_asset_root_slot_does_not_create_an_unreal_profile(tmp_path, monkeypatch):
    from src import uex_profiles
    ensure = Mock(side_effect=AssertionError("generic game readers must not configure UEX"))
    monkeypatch.setattr(uex_profiles, "ensure_profile", ensure)
    assert server._uex_host_slots({"argv": ["{assetRoot}", "{path}"]}, root=tmp_path) == {
        "assetRoot": str(tmp_path)}
    ensure.assert_not_called()


def test_profile_change_invalidates_reader_cache_key(tmp_path):
    asset = tmp_path / "asset.uasset"
    asset.write_bytes(b"package")
    config = tmp_path / "profile.json"
    config.write_text('{"game":"GAME_UE5_3"}')
    tool = reader("uex", engine="unreal")
    operation = tool["interface"]["operations"][0]
    context = {"uexConfig": str(config), "uexProfile": "fixture"}
    before = routing.ReaderResultCache.key(tmp_path, asset, tool, operation, context=context)
    config.write_text('{"game":"GAME_UE5_4","pinned":true}')
    after = routing.ReaderResultCache.key(tmp_path, asset, tool, operation, context=context)
    assert before != after
