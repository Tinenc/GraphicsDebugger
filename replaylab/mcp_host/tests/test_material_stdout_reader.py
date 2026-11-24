"""Material reads use complete, bounded and content-bound tool stdout artifacts."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from src import server, tool_runner, uex_profiles


def _receipt(tmp_path, raw=None):
    raw = raw if raw is not None else b'{"scalars":{"TailParameter":0.625}}'
    output = tmp_path / "run-output"
    output.mkdir(exist_ok=True)
    target = output / "host-stdout.txt"
    target.write_bytes(raw)
    return {"succeeded": True, "outputDirectory": str(output), "stdoutPath": str(target),
            "stdout": raw[:12].decode("utf-8", "replace"), "stdoutTruncated": True,
            "stdoutByteSize": len(raw), "stdoutFileByteSize": len(raw),
            "stdoutSha256": hashlib.sha256(raw).hexdigest(),
            "stdoutFileSha256": hashlib.sha256(raw).hexdigest(), "stdoutFileTruncated": False}


def _selected(monkeypatch, receipt):
    monkeypatch.setattr(server, "_declared_asset_operations", lambda: [])
    monkeypatch.setattr(server, "run_asset_tool_operation", AsyncMock(return_value=json.dumps(receipt)))
    return asyncio.run(server._read_material_asset(
        "selected:material", reader_operation="material_reader", reader_arguments={"object": "selected"}))


@pytest.mark.parametrize("route", ["selected-operation", "automatic-unreal"])
def test_complete_large_json_survives_real_tool_output_and_host_compaction(tmp_path, monkeypatch, route):
    script = tmp_path / "reader.py"
    script.write_text(
        "import json,sys\nsys.stdout.buffer.write(json.dumps({'padding':'x'*70000,"
        "'scalars':{'TailParameter':0.625}}).encode('utf-8'))\n", encoding="utf-8")
    operation = {"name": "material_reader", "argv": ["{program}", str(script)],
                 "inputSchema": {"type": "object", "properties": {}, "required": []},
                 "result": {"successWhen": {"exitCode": 0}}}
    receipt = tool_runner.run_operation(operation, {}, program=sys.executable, output_root=tmp_path / "output")
    assert receipt["succeeded"] and receipt["stdoutTruncated"]
    monkeypatch.setattr(server, "MAX_AGENT_RESULT_BYTES", 8000)
    compact = json.loads(server._ok_compact(receipt))
    assert "_compacted" in compact
    assert "TailParameter" not in compact["stdout"]
    if route == "selected-operation":
        result = _selected(monkeypatch, compact)
    else:
        (tmp_path / "material.uasset").write_bytes(b"cooked-package")
        monkeypatch.setattr(server, "_declared_asset_operations", lambda: [])
        monkeypatch.setattr(server, "_unpacked_asset_root", lambda: (tmp_path, {}))
        monkeypatch.setattr(server, "_load_asset_reader_selection", lambda: None)
        monkeypatch.setattr(server, "_registered_tools", lambda: [{
            "id": "fixture-uex", "roles": ["asset-reader"], "engines": ["unreal"],
            "connection": {"kind": "cli", "command": sys.executable},
            "interface": {"operations": [{**operation, "kind": "cli", "role": "asset-reader",
                "patterns": ["*.uasset"], "proved": {"sample": "fixture"}}]},
        }])
        monkeypatch.setattr(server, "_read_builtin_asset_description",
                            lambda *_args: {"engine": "unreal", "reader": "unreal-package"})
        monkeypatch.setattr(uex_profiles, "ensure_profile", lambda _root: {"mountRoot": str(tmp_path)})
        monkeypatch.setattr(uex_profiles, "uex_relative_path", lambda *_args: "Game/Content/material")
        monkeypatch.setattr(server, "_run_uex_preview", Mock(
            side_effect=AssertionError("automatic reads must use the unified capability router")))
        result = asyncio.run(server._read_material_asset("material.uasset"))
    assert result["succeeded"]
    assert result["document"]["scalars"] == {"TailParameter": 0.625}
    assert len(result["document"]["padding"]) == 70000


@pytest.mark.parametrize("fault, expected", [
    ("truncated", "truncated"), ("missing-completeness", "completeness"),
    ("oversize", "8 MiB"), ("size-metadata", "sizes differ"),
    ("bool-size", "sizes differ"), ("hash-metadata", "SHA256 evidence differs"),
    ("changed-bytes", "SHA256 does not match"), ("changed-size", "could not be verified"),
    ("missing-file", "could not be verified"), ("escape", "escapes"),
    ("traversal", "escapes"), ("relative-root", "escapes"),
    ("invalid-json", "complete UTF-8 JSON"), ("invalid-utf8", "complete UTF-8 JSON"),
])
def test_unverified_stdout_file_never_falls_back_to_valid_inline_material(tmp_path, monkeypatch, fault, expected):
    raw = b"{broken" if fault == "invalid-json" else b"\xff" if fault == "invalid-utf8" else None
    receipt = _receipt(tmp_path, raw)
    target = Path(receipt["stdoutPath"])
    # A valid preview/description must not conceal failure of the declared full output.
    receipt["stdout"] = '{"scalars":{"WrongPrefix":1}}'
    receipt["description"] = {"scalars": {"WrongDescription": 1}}
    if fault == "truncated":
        receipt["stdoutFileTruncated"] = True
    elif fault == "missing-completeness":
        del receipt["stdoutFileTruncated"]
    elif fault == "oversize":
        receipt["stdoutByteSize"] = receipt["stdoutFileByteSize"] = 8 * 1024 * 1024 + 1
    elif fault == "size-metadata":
        receipt["stdoutFileByteSize"] += 1
    elif fault == "bool-size":
        receipt["stdoutByteSize"] = True
    elif fault == "hash-metadata":
        receipt["stdoutFileSha256"] = "a" * 64
    elif fault == "changed-bytes":
        target.write_bytes(b"x" * receipt["stdoutFileByteSize"])
    elif fault == "changed-size":
        target.write_bytes(b"x")
    elif fault == "missing-file":
        target.unlink()
    elif fault == "escape":
        receipt["stdoutPath"] = str(tmp_path / "outside.txt")
    elif fault == "traversal":
        receipt["stdoutPath"] = str(Path(receipt["outputDirectory"]) / ".." / "outside.txt")
    elif fault == "relative-root":
        receipt["outputDirectory"] = "run-output"
    with pytest.raises(ValueError, match=expected):
        _selected(monkeypatch, receipt)


@pytest.mark.parametrize("success", [False, 1, "true"])
def test_failed_or_non_boolean_success_cannot_read_material_stdout(tmp_path, monkeypatch, success):
    receipt = _receipt(tmp_path)
    receipt["succeeded"] = success
    reader = Mock(side_effect=AssertionError("failed operation must not read its stdout file"))
    monkeypatch.setattr(server, "read_recovery_regular_file", reader)
    result = _selected(monkeypatch, receipt)
    assert not result["succeeded"]
    reader.assert_not_called()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_stdout_symlink_or_reparse_cannot_supply_material(tmp_path, monkeypatch, kind):
    receipt = _receipt(tmp_path)
    link = tmp_path / "linked-output" if kind == "directory" else Path(receipt["outputDirectory"]) / "linked-stdout.txt"
    target = Path(receipt["outputDirectory"]) if kind == "directory" else Path(receipt["stdoutPath"])
    try:
        link.symlink_to(target, target_is_directory=kind == "directory")
    except OSError:
        pytest.skip("creating symlinks requires an unavailable Windows privilege")
    if kind == "directory":
        receipt["outputDirectory"] = str(link)
        receipt["stdoutPath"] = str(link / "host-stdout.txt")
    else:
        receipt["stdoutPath"] = str(link)
    with pytest.raises(ValueError, match="could not be verified"):
        _selected(monkeypatch, receipt)


def test_small_legacy_stdout_and_structured_description_remain_usable(monkeypatch):
    document = {"scalars": {"LegacyParameter": 0.75}}
    for evidence in ({"stdout": json.dumps(document)}, {"description": document}):
        result = _selected(monkeypatch, {"succeeded": True, **evidence})
        assert result["succeeded"] and result["document"] == document


@pytest.mark.parametrize("metadata", [{"stdoutTruncated": True}, {"stdoutByteSize": 9000},
                                       {"stdoutSha256": "a" * 64}, {}])
def test_inline_truncation_without_full_file_is_an_explicit_error(monkeypatch, metadata):
    receipt = {"succeeded": True, "stdout": '{"scalars":{"TailParameter":', **copy.deepcopy(metadata)}
    with pytest.raises(ValueError, match="material reader"):
        _selected(monkeypatch, receipt)
