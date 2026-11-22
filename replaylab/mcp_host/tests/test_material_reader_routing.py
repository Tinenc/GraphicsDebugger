"""Material reads select proved capabilities without assuming an engine."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock

from src import server


def test_local_material_json_needs_no_external_program(tmp_path, monkeypatch):
    document = {"m_Name": "Coat", "m_SavedProperties": {"m_Floats": {"_Smoothness": 0.73125}}}
    (tmp_path / "coat.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(server, "_unpacked_asset_root", lambda: (tmp_path, {}))
    monkeypatch.setattr(server, "_declared_asset_operations", lambda: [])
    external = Mock(side_effect=AssertionError("JSON must not launch an unpacker"))
    monkeypatch.setattr(server, "_run_uex_preview", external)
    result = asyncio.run(server._read_material_asset("coat.json"))
    assert result["succeeded"] and result["document"] == document
    external.assert_not_called()


def test_unity_container_header_does_not_launch_uex(tmp_path, monkeypatch):
    (tmp_path / "coat.bundle").write_bytes(b"UnityFS\x00")
    monkeypatch.setattr(server, "_unpacked_asset_root", lambda: (tmp_path, {}))
    monkeypatch.setattr(server, "_declared_asset_operations", lambda: [{"name": "unity_material"}])
    monkeypatch.setattr(server, "_describe_with_registered_tool", AsyncMock(return_value=None))
    monkeypatch.setattr(server, "_read_builtin_asset_description", lambda *a: {"engine": "unity", "reader": "unity-serialized"})
    external = Mock(side_effect=AssertionError("Unity must not launch UEX"))
    monkeypatch.setattr(server, "_run_uex_preview", external)
    result = asyncio.run(server._read_material_asset("coat.bundle"))
    assert not result["succeeded"]
    assert result["operations"][0]["name"] == "unity_material"
    external.assert_not_called()


def test_agent_selected_material_operation_is_engine_neutral(monkeypatch):
    document = {"name": "Surface", "scalars": {"Edge": 0.73125}}
    monkeypatch.setattr(server, "_declared_asset_operations", lambda: [])
    call = AsyncMock(return_value=json.dumps({"succeeded": True, "stdout": json.dumps(document)}))
    monkeypatch.setattr(server, "run_asset_tool_operation", call)
    result = asyncio.run(server._read_material_asset(
        "virtual:surface", reader_operation="custom_material", reader_arguments={"object": 42}))
    assert result["succeeded"] and result["document"] == document
    call.assert_awaited_once_with(name="custom_material", arguments={"object": 42})


def test_export_receipt_cannot_be_mistaken_for_material(monkeypatch):
    monkeypatch.setattr(server, "_declared_asset_operations", lambda: [])
    monkeypatch.setattr(server, "run_asset_tool_operation", AsyncMock(return_value=json.dumps(
        {"succeeded": True, "stdout": '{"files":["A.json","B.json"]}'})))
    result = asyncio.run(server._read_material_asset("bundle", reader_operation="export", reader_arguments={}))
    assert not result["succeeded"]
    assert result["reason"] == "operation-needs-material-selection"


def test_builtin_operation_reads_one_scoped_file_without_external_reader(tmp_path, monkeypatch):
    from src.tool_runner import BUILTIN_OPERATIONS
    (tmp_path / "material.json").write_text('{"name":"Surface"}', encoding="utf-8")
    monkeypatch.setattr(server, "_unpacked_asset_root", lambda: (tmp_path, {}))
    monkeypatch.setattr(server, "_declared_asset_operations", lambda: list(BUILTIN_OPERATIONS))
    external = AsyncMock(side_effect=AssertionError("builtin read must not call an external reader"))
    monkeypatch.setattr(server, "_read_unpacked_asset_description", external)
    result = json.loads(asyncio.run(server.run_asset_tool_operation(
        "read_standalone_container", {"assetPath": str(tmp_path / "material.json")})))
    assert result["succeeded"] and result["description"]["reader"] == "text"
    assert result["description"]["relativePath"] == "material.json"
    external.assert_not_called()
    rejected = json.loads(asyncio.run(server.run_asset_tool_operation(
        "read_standalone_container", {"assetPath": "../outside.json"})))
    assert "error" in rejected
    rejected = json.loads(asyncio.run(server.run_asset_tool_operation(
        "read_standalone_container", {"assetPath": "material.json", "undeclared": True})))
    assert "error" in rejected
