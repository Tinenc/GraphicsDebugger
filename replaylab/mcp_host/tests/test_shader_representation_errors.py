"""Keep failed native Shader queries distinct from successful identity mismatches."""

import pytest

from src.shader_workspace.errors import ToolExecutionError
from src.shader_workspace.representations import publish_current_event_representation


@pytest.mark.parametrize("metadata", [{}, {"stage": "compute"}, {"stage": "fragment", "shaderId": 77}])
@pytest.mark.parametrize("message", [
    "No shader at stage 'fragment'",
    "The exact pipeline shader reflection is unavailable",
    "The captured Shader entry point is ambiguous",
])
def test_failed_bridge_query_reports_original_error_before_stage_or_identity_checks(tmp_path, metadata, message):
    with pytest.raises(ToolExecutionError) as error:
        publish_current_event_representation(tmp_path / "artifacts", event_id=42,
            requested_stage="fragment", bridge_response=dict(metadata, error=message))
    assert str(error.value) == "RenderDoc Shader query failed: " + message
    assert not (tmp_path / "artifacts").exists()


def test_successful_bridge_response_with_different_stage_still_fails_identity_check(tmp_path):
    with pytest.raises(ToolExecutionError, match="stage contradicts"):
        publish_current_event_representation(tmp_path / "artifacts", event_id=42,
            requested_stage="fragment", bridge_response={"stage": "compute", "shaderId": 77})
    assert not (tmp_path / "artifacts").exists()
