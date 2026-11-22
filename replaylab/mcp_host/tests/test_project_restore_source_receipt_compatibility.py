"""Saved source is rebound to current original reflection, without an apply grant."""
import copy

import pytest

from src import project_restore
from src.shader_workspace.abi import parse_shader_abi
from tests.test_project_restore_source_admission import target
from tests.test_shader_abi import complete_interface


@pytest.fixture
def imported_source(target):
    status = copy.deepcopy(target.status)
    interface = complete_interface("fragment")
    current_abi = parse_shader_abi(interface).sha256
    legacy_interface = copy.deepcopy(interface)
    legacy_interface["outputTopology"] = ""
    old_abi = parse_shader_abi(legacy_interface).sha256
    assert old_abi != current_abi
    status["baseline"].update(abiSha256=current_abi, compiledInterface=interface)
    identity = {**target.identity, "stableKeyComplete": False, "abiHash": "1" * 64}
    knowledge = {
        "knowledgeId": "2" * 64, "sourceFormat": target.language,
        "sourceSha256": target.source_sha, "rawSha256": identity["rawSha256"],
        "baselineAbiSha256": old_abi, "stage": identity["stage"],
        "entryPoint": identity["entryPoint"], "targetProfile": status["targetProfile"],
    }
    state = {"projectId": "3" * 32, "projectPath": target.active["projectRoot"],
             "capture": {"inventoryOnly": True}}
    receipt = {
        "schema": "replaylab-verified-project-shader-source/v1",
        "projectId": state["projectId"], "projectPath": state["projectPath"],
        "knowledgeId": knowledge["knowledgeId"], "sourceSha256": target.source_sha,
        "sourceFormat": target.language, "originalRawSha256": identity["rawSha256"],
        "baselineAbiSha256": current_abi, "stage": identity["stage"],
        "entryPoint": identity["entryPoint"], "targetProfile": status["targetProfile"],
        "applied": False, "requiresCompile": True, "requiresLiveAbiRevalidation": True,
    }
    status["source"] = {
        "present": True, "lane": "executable-editable", "format": target.language,
        "origin": "verified-project-source", "currentSha256": target.source_sha,
        "projectSource": receipt,
    }
    return status, knowledge, identity, state


@pytest.mark.parametrize("old_schema", ["topology-spelling", "other-reflection-version"])
def test_fresh_receipt_uses_current_authenticated_original_baseline(imported_source, old_schema):
    values = imported_source
    if old_schema == "other-reflection-version":
        values[1]["baselineAbiSha256"] = "4" * 64
    before = copy.deepcopy(values)
    project_restore._validate_prepared_project_source(*values, source_preparation_only=True)
    assert values == before
    assert "latestCompiled" not in values[0] or values[0]["latestCompiled"] is None
    assert "replayAbiValidated" not in values[0]
    with pytest.raises(RuntimeError, match="baseline"):
        project_restore._validate_prepared_project_source(*values)


@pytest.mark.parametrize("field,value", [
    ("baseline.compiledInterface.outputTopology", "Topology.TriangleList"),
    ("baseline.abiSha256", "5" * 64),
    ("baseline.rawSha256", "5" * 64),
    ("shader.rawSha256", "5" * 64),
    ("capture.api", "GraphicsAPI.Unknown"),
    ("baseline.kind", "OTHER"),
    ("source.projectSource.baselineAbiSha256", "5" * 64),
    ("source.projectSource.originalRawSha256", "5" * 64),
    ("source.projectSource.stage", "vertex"),
    ("source.projectSource.entryPoint", "other"),
    ("source.projectSource.targetProfile", {"profile": "other"}),
    ("source.projectSource.applied", True),
    ("source.projectSource.requiresCompile", False),
    ("source.projectSource.requiresLiveAbiRevalidation", False),
    ("targetProfile", {"profile": "other"}),
])
def test_source_compatibility_still_requires_current_original_and_receipt(imported_source, field, value):
    values = imported_source
    target = values[0]
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    with pytest.raises((RuntimeError, ValueError)):
        project_restore._validate_prepared_project_source(*values, source_preparation_only=True)
