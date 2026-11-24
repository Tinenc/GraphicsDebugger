import json

import pytest

from src.shader_workspace.alias_retention import collect_alias_retention


ORIGINAL = "a" * 64
CURRENT = "b" * 64
CAPTURE = "c" * 64
SESSION = "d" * 32


def workspace(path, candidate=CURRENT):
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(json.dumps({
        "schema": "renderdoc-ultra-shader-workspace/v1",
        "stage": "fragment", "shader": {"rawSha256": ORIGINAL},
        "latestCompiled": {"rawSha256": candidate},
        "history": [{"candidateRawSha256": "0" * 64}],
    }), encoding="utf-8")
    return path


def replacements(rows=()):
    return {"schema": "renderdoc-ultra-shared-shader-replacement-list/v1", "schemaVersion": 1,
            "captureId": CAPTURE, "captureSessionId": SESSION, "originalShadersPreserved": True,
            "replayMutation": False, "count": len(rows), "replacements": list(rows)}


def collect(path, **kwargs):
    return collect_alias_retention(path, captured_shader_sha256=ORIGINAL, stage="fragment",
        current_shader_sha256s=[CURRENT], capture_id=CAPTURE, capture_session_id=SESSION,
        **{"replacement_state_loader": replacements, **kwargs})


class Project:
    def __init__(self, records, current, fallbacks=()):
        self.records, self.current, self.fallbacks = records, current, fallbacks

    def _load(self, *, persist_migrations):
        assert persist_migrations is False
        return {"knowledge": {"workspaces": self.records}}

    def _active_heads_state(self, manifest):
        return {"workspaces": {str(i): {"knowledgeId": value} for i, value in enumerate(self.current)}}

    def _workspace_restore_fallbacks(self, manifest):
        return {key: {} for key in self.fallbacks}


def record(key, sha, **kwargs):
    return {"knowledgeId": key, "rawSha256": ORIGINAL, "stage": "fragment",
            "compiledCandidate": {"rawSha256": sha}, **kwargs}


def test_only_authoritative_project_heads_and_restore_fallback_are_protected(tmp_path):
    project = Project([record("old", "0" * 64), record("head", "1" * 64),
                       record("fallback", "2" * 64), record("other", "3" * 64, rawSha256="4" * 64)],
                      ["head", "other"], ["fallback"])
    result = collect(workspace(tmp_path), project_service=project)
    assert result["complete"]
    assert result["protectedShaderRawSha256"] == sorted([ORIGINAL, CURRENT, "1" * 64, "2" * 64])


def test_current_project_workspace_save_is_kept_without_retaining_its_history(tmp_path):
    saved = workspace(tmp_path / "other", "2" * 64)
    project = Project([record("head", "1" * 64, workspacePath=str(saved))], ["head"])
    result = collect(workspace(tmp_path / "current"), project_service=project)
    assert result["complete"]
    assert set(result["protectedShaderRawSha256"]) == {ORIGINAL, CURRENT, "1" * 64, "2" * 64}


def test_moved_project_working_copy_keeps_registered_binary_without_pin_to_old_path(tmp_path):
    project = Project([record("head", "1" * 64, workspacePath=str(tmp_path / "removed"))], ["head"])
    result = collect(workspace(tmp_path / "current"), project_service=project)
    assert result["complete"]
    assert set(result["protectedShaderRawSha256"]) == {ORIGINAL, CURRENT, "1" * 64}


def test_live_original_lineage_owners_are_kept_across_catalog_scopes(tmp_path):
    row = {"originalRawSha256": ORIGINAL, "stage": "pixel", "candidateRawSha256": "5" * 64,
           "captureId": "6" * 64, "captureSessionId": SESSION,
           "resourceScoped": True, "eventScoped": False, "originalShaderPreserved": True}
    result = collect(workspace(tmp_path), replacement_state_loader=lambda: replacements([row]))
    assert result["complete"] and "5" * 64 in result["protectedShaderRawSha256"]


@pytest.mark.parametrize("change", [
    {"captureSessionId": "0" * 32}, {"captureId": "0" * 64}, {"count": 4}, {"replayMutation": True},
])
def test_stale_or_untrustworthy_live_registry_defers_pruning_without_blocking_publication(tmp_path, change):
    result = collect(workspace(tmp_path), replacement_state_loader=lambda: {**replacements(), **change})
    assert not result["complete"]
    assert result["diagnostic"] and len(result["diagnostic"]) <= 400
    assert CURRENT in result["protectedShaderRawSha256"]


def test_read_failure_is_nonfatal_and_diagnostic_is_bounded(tmp_path):
    def failed():
        raise RuntimeError("registry busy\n" * 1000)
    result = collect(workspace(tmp_path), replacement_state_loader=failed)
    assert not result["complete"] and len(result["diagnostic"]) <= 400
    assert "\n" not in result["diagnostic"]


def test_unreadable_project_defers_cleanup(tmp_path):
    class MissingProject:
        def _load(self, **kwargs):
            raise OSError("project unavailable")
    result = collect(workspace(tmp_path), project_service=MissingProject())
    assert not result["complete"] and "project unavailable" in result["diagnostic"]


def test_wrong_workspace_lineage_defers_cleanup(tmp_path):
    workspace(tmp_path)
    manifest = tmp_path / "manifest.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["shader"]["rawSha256"] = "9" * 64
    manifest.write_text(json.dumps(value), encoding="utf-8")
    result = collect(tmp_path)
    assert not result["complete"] and "identity does not match" in result["diagnostic"]
