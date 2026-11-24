from __future__ import annotations

import pytest

from src.shader_workspace import (
    AbiValidationError,
    ManifestValidationError,
    ShaderOrganizationError,
    ToolConfigurationError,
    ToolExecutionError,
    ToolchainRoutingError,
    classify_shader_failure,
)


@pytest.mark.parametrize("error,phase,category,retryable", [
    (ManifestValidationError("workspace route catalog bytes changed after creation"),
     "watch-compile", "workspace-toolchain-upgrade-required", True),
    (ToolchainRoutingError("no integrity-valid dxil -> hlsl route"),
     "decompile", "route-unavailable", False),
    (ToolConfigurationError("executable SHA-256 integrity mismatch"),
     "route-bind", "tool-integrity-or-configuration", False),
    (ToolExecutionError("route declared hlsl but output detected as unknown"),
     "decompile", "malformed-tool-output", False),
    (RuntimeError("unclassified workspace preparation failure"),
     "workspace-prepare", "unknown", False),
    (AbiValidationError("candidate ABI resource binding mismatch"),
     "abi-validate", "abi-mismatch", False),
    (RuntimeError("Checked Vulkan Shader replacement failed: derived pipeline rebuild failed"),
     "live-edit-apply", "backend-replacement-rejected", False),
    (RuntimeError("Ray shader replacement needs state-object shader-group and SBT evidence"),
     "live-edit-apply", "ray-library-replacement-unavailable", False),
    (RuntimeError("active capture session changed before Shader apply"),
     "live-edit-apply", "capture-or-shader-identity-conflict", False),
    (RuntimeError("replacement is no longer owned by this workspace"),
     "live-edit-apply", "replacement-ownership-conflict", False),
    (RuntimeError("workspace path already exists: C:/private/path"),
     "workspace-prepare", "workspace-path-conflict", True),
    (RuntimeError("RenderDoc bridge did not return catalogPath"),
     "workspace-prepare", "capture-catalog-export-failed", True),
    (ToolExecutionError("provider process exited with code 1: selected Shader bytes do not match expected_raw_sha256"),
     "workspace-prepare", "tool-rejected-input", False),
    (ToolExecutionError("untouched reconstructed-source baseline compile probe failed: compiler process exited with code 1"),
     "recovery-decompile-or-baseline-compile-probe", "baseline-compile-probe-failed", True),
    (ShaderOrganizationError("baseline code SHA-256 is invalid"),
     "organization-review", "organization-evidence-invalid", False),
    (ShaderOrganizationError("organization flowchart is not plain safe Mermaid"),
     "organization-review", "organization-proposal-invalid", True),
])
def test_failure_reports_bounded_facts_without_agent_instructions(error, phase, category, retryable):
    result = classify_shader_failure(error, phase=phase)
    assert result["category"] == category
    assert result["retryable"] is retryable
    assert result["phase"] == phase
    assert result["exceptionType"] == type(error).__name__
    assert result["message"]
    assert not {"allowedActions", "aiPolicy", "sourceRepairTool", "autonomousNext"} & result.keys()


def test_managed_path_violation_is_distinct_from_ordinary_collision():
    for message in (
        "workspace_path is not the active project's deterministic path",
        "deterministic Shader workspace escaped the active project",
        "existing workspace is outside the active project workspace root",
    ):
        result = classify_shader_failure(RuntimeError(message), phase="workspace-prepare")
        assert result["category"] == "workspace-path-policy-violation"
        assert result["retryable"] is False


def test_abi_repair_capability_does_not_authorize_application():
    result = classify_shader_failure(
        AbiValidationError("candidate ABI resource binding mismatch"), phase="abi-validate"
    )
    assert result["availableCapabilities"] == ["repair_shader_workspace_source_abi"]
    assert "applied" not in result


def test_wrapped_organization_failure_preserves_typed_cause():
    try:
        raise AbiValidationError("candidate ABI resource binding mismatch")
    except AbiValidationError as cause:
        try:
            raise ShaderOrganizationError("organization review failed; baseline source restored") from cause
        except ShaderOrganizationError as wrapped:
            result = classify_shader_failure(wrapped, phase="organization-review")
    assert result["category"] == "organization-abi-failed"
    assert result["cause"]["category"] == "abi-mismatch"
    assert result["availableCapabilities"] == ["repair_shader_workspace_source_abi"]
