from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src import server


def read_status(resume):
    with (
        patch.object(server, "_shader_workspace_artifact_service", return_value=SimpleNamespace(
            status=lambda path: {"workspacePath": path, "source": {"present": True}})),
        patch.object(server, "_shader_organization_manager", return_value=SimpleNamespace(resume_summary=resume)),
        patch.object(server, "_shader_workspace_response", side_effect=lambda operation, value: json.dumps(value)),
    ):
        return json.loads(server.get_shader_workspace_status("unused-workspace"))


def test_invalid_inherited_progress_remains_visible_without_hiding_source():
    result = read_status(Mock(side_effect=ValueError("invalid inherited progress")))
    assert result["source"]["present"] is True
    assert result["organization"] is None
    assert result["organizationDiagnostic"]["code"] == "organization-resume-unavailable"
    assert "invalid inherited progress" in result["organizationDiagnostic"]["message"]


def test_no_organization_is_distinct_from_failed_resume():
    result = read_status(Mock(return_value=None))
    assert result["organization"] is None
    assert "organizationDiagnostic" not in result


def test_resume_failure_diagnostic_is_bounded():
    result = read_status(Mock(side_effect=ValueError("invalid origin " * 1000)))
    assert len(result["organizationDiagnostic"]["message"]) <= 4096
    assert result["organizationDiagnostic"]["code"] == "organization-resume-unavailable"
