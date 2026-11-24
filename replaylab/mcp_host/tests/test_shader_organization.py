from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from src.shader_live_edit import (
    SHARED_APPLY_SCHEMA,
    SHARED_ROLLBACK_TOOL,
    ShaderLiveEditManager,
)
from src.shader_workspace import ShaderWorkspaceWatchManager
from src.shader_workspace import aliases as cbuffer_aliases
from src.shader_workspace import organization as organization_module
from src.shader_workspace.leases import WorkspaceSourceTransactionLease
from src.shader_workspace.organization import (
    ShaderOrganizationError,
    ShaderOrganizationManager,
)
from src.shader_workspace.flow_graph import module_description_template


def _valid_analysis() -> str:
    return "The captured MainPS source returns its computed color target.\n"


def test_alias_review_artifact_retains_same_local_target_in_distinct_functions(tmp_path):
    renames = {
        "First::_1": "normal",
        "Second::_2": "normal",
        "Third::_3": "roughness",
    }
    payloads = ShaderOrganizationManager._alias_view_payloads(
        tmp_path, "float First() { return 1; }", renames, {}, "float First() { return 1; }"
    )
    alias_file = json.loads(payloads[tmp_path / "ai" / "aliases.json"])
    assert alias_file["renamesBySelector"] == renames
    assert alias_file["aliases"] == {"roughness": "Third::_3"}
    assert alias_file["ambiguousTargets"] == ["normal"]


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


def _compatible_result(root: Path, revision: int, raw_sha256: str) -> dict:
    replacement_id = "%032x" % (0x2000 + revision)
    return {
        "schema": SHARED_APPLY_SCHEMA,
        "operation": "apply-shared-resource",
        "binding": {
            "revision": revision,
            "candidateRawSha256": raw_sha256,
        },
        "validation": {"replayAbiValidated": True},
        "scope": "captured-resource",
        "applied": True,
        "replacement": {
            "replacementId": replacement_id,
            "revision": revision,
            "candidateRawSha256": raw_sha256,
            "originalResourceId": 17,
            "resourceScoped": True,
            "eventScoped": False,
            "allOriginalResourceUsesAffected": True,
            "originalShaderPreserved": True,
        },
        "rollback": {
            "tool": SHARED_ROLLBACK_TOOL,
            "workspacePath": str(root),
            "replacementId": replacement_id,
        },
    }


def _rejected_result(revision: int, raw_sha256: str) -> dict:
    return {
        "schema": SHARED_APPLY_SCHEMA,
        "operation": "apply-shared-resource",
        "binding": {
            "revision": revision,
            "candidateRawSha256": raw_sha256,
        },
        "validation": {"replayAbiValidated": False},
        "applied": False,
        "reason": "candidate ABI is not exactly compatible",
    }


class _FakeWorkspaceService:
    def __init__(self, root: Path, source: bytes) -> None:
        self.root = root.resolve()
        self.source_path = self.root / "source" / "shader.hlsl"
        self.source_path.parent.mkdir(parents=True)
        self.source_path.write_bytes(source)
        self.decompiled_sha = hashlib.sha256(source).hexdigest()
        self.baseline_code_sha = self._code_sha(source)
        self.workspace_id = "1" * 32
        self.capture_id = "2" * 64
        self.original_raw_sha256 = "3" * 64
        self.target_profile = {"profile": "ps_6_6", "stage": "fragment"}
        self.revision = 0
        self.latest = None

    def _code_sha(self, source: bytes) -> str:
        payload = b"changed-program" if b"semantic-change" in source else b"same-program"
        return hashlib.sha256(payload).hexdigest()

    def compile(self, workspace_path: str) -> dict:
        assert Path(workspace_path).resolve() == self.root
        source = self.source_path.read_bytes()
        source_sha = hashlib.sha256(source).hexdigest()
        self.revision += 1
        raw_sha = hashlib.sha256(b"container:" + source).hexdigest()
        code_sha = self._code_sha(source)
        self.latest = {
            "revision": self.revision,
            "sourceSha256": source_sha,
            "rawSha256": raw_sha,
            "codeSha256": code_sha,
            "abiCompatible": False,
            "requiresReplayAbiValidation": True,
            "replayAbiValidated": False,
        }
        return {
            "workspacePath": str(self.root),
            "revision": self.revision,
            "sourceSha256": source_sha,
            "rawSha256": raw_sha,
            "codeSha256": code_sha,
            "kind": "DXIL",
        }

    compile_check_calls: list[str] = []

    def compile_check(self, workspace_path: str, text: str) -> dict:
        assert Path(workspace_path).resolve() == self.root
        self.compile_check_calls.append(text)
        if "compile-error" in text:
            return {"ok": False, "diagnostics": [{"message": "synthetic compile error"}]}
        return {"ok": True, "diagnostics": []}

    def status(self, workspace_path: str) -> dict:
        assert Path(workspace_path).resolve() == self.root
        source_sha = hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        current_source_matches = source_sha == self.decompiled_sha
        candidate_matches = bool(
            current_source_matches
            and self.latest
            and self.latest.get("sourceSha256") == source_sha
            and self.latest.get("codeSha256") == self.baseline_code_sha
        )
        candidate_abi = bool(
            candidate_matches
            and self.latest.get("replayAbiValidated") is True
            and self.latest.get("abiCompatible") is True
        )
        return {
            "schema": "renderdoc-ultra-shader-workspace-status/v1",
            "workspacePath": str(self.root),
            "workspaceId": self.workspace_id,
            "capture": {"captureId": self.capture_id},
            "shader": {
                "resourceId": 17,
                "rawSha256": self.original_raw_sha256,
                "stage": "fragment",
                "entryPoint": "MainPS",
            },
            "stage": "fragment",
            "entryPoint": "MainPS",
            "targetProfile": dict(self.target_profile),
            "source": {
                "present": True,
                "path": str(self.source_path),
                "format": "hlsl",
                "currentSha256": source_sha,
                "decompiledSha256": self.decompiled_sha,
                "origin": "trusted-decompile-route",
                "lane": "executable-editable",
                "compileEligible": True,
            },
            "representation": {
                "lane": "executable-editable",
                "compileEligible": True,
            },
            "baseline": {
                "rawSha256": self.original_raw_sha256,
                "compiledInterface": {},
            },
            "revision": self.revision,
            "latestCompiled": dict(self.latest) if self.latest else None,
            "replayAbiValidated": bool(
                self.latest
                and self.latest.get("replayAbiValidated") is True
            ),
            "abiCompatible": bool(
                self.latest and self.latest.get("abiCompatible") is True
            ),
            "reconstructionFidelity": {
                "schema": "renderdoc-ultra-shader-reconstruction-fidelity/v1",
                "scope": "captured-baseline-vs-untouched-route-round-trip",
                "available": True,
                "baselineCompileVerified": True,
                "capturedCodeSha256": self.baseline_code_sha,
                "reconstructedCodeSha256": self.baseline_code_sha,
                "codeSha256Equal": True,
                "currentSourceMatchesUntouchedReconstruction": (
                    current_source_matches
                ),
                "currentCandidateMatchesUntouchedReconstruction": candidate_matches,
                "currentCandidateReplayAbiCompatible": candidate_abi,
                "classification": "exact-executable-code-match",
                "semanticEquivalenceVerified": False,
                "note": "fixed fake fidelity evidence",
            },
        }


class ShaderOrganizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        config_patch = mock.patch.dict(os.environ, {
            "REPLAYLAB_CONFIG_DIR": str(Path(self.temporary.name) / "host-config")
        })
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        self.original = b"float4 MainPS() : SV_Target { return 1.0; }\n"
        self.service = _FakeWorkspaceService(self.root, self.original)
        self.live_manager: ShaderLiveEditManager | None = None

        def validate(workspace_path: str, revision: int, raw_sha256: str) -> dict:
            self.assertEqual(Path(workspace_path).resolve(), self.root.resolve())
            self.assertEqual(revision, self.service.revision)
            self.assertEqual(raw_sha256, self.service.latest["rawSha256"])
            request_id = hashlib.sha256(
                ("request:" + str(revision) + raw_sha256).encode("ascii")
            ).hexdigest()
            report_sha256 = hashlib.sha256(
                (str(revision) + raw_sha256).encode("ascii")
            ).hexdigest()
            self.service.latest["abiCompatible"] = True
            self.service.latest["requiresReplayAbiValidation"] = False
            self.service.latest["replayAbiValidated"] = True
            self.service.latest["validationScope"] = (
                "replay-reflection-abi-compatible"
            )
            self.service.latest["semanticEquivalenceVerified"] = False
            self.service.latest["abiGate"] = {
                "status": "compatible",
                "requestId": request_id,
                "reportSha256": report_sha256,
                "baselineAbiSha256": hashlib.sha256(
                    b"organization-test-baseline-abi"
                ).hexdigest(),
                "candidateAbiSha256": hashlib.sha256(
                    ("candidate-abi:" + raw_sha256).encode("ascii")
                ).hexdigest(),
            }
            return {
                "workspacePath": str(self.root.resolve()),
                "revision": revision,
                "requestId": request_id,
                "compatible": True,
                "requiresReplayAbiValidation": False,
                "replayAbiValidated": True,
                "abiReportSha256": report_sha256,
            }

        self.manager = ShaderOrganizationManager(lambda: self.service, validate)

    def tearDown(self) -> None:
        if self.live_manager is not None:
            self.live_manager.shutdown()
        self.temporary.cleanup()

    def _begin(self) -> dict:
        opened = self.manager.begin(
            str(self.root),
            {
                "schema": "test-semantic-context/v1",
                "textures": [{"slot": 0, "role": "base-color"}],
            },
        )
        Path(opened["analysisPath"]).write_text(_valid_analysis(), encoding="utf-8")
        descriptions = module_description_template(
            Path(opened["proposalSourcePath"]).read_text(encoding="utf-8")
        )
        for module in descriptions["modules"]:
            module["description"] = "Performs the source-defined computation for this function."
        Path(opened["moduleDescriptionsPath"]).write_text(
            json.dumps(descriptions), encoding="utf-8"
        )
        self.manager.checkpoint(str(self.root), opened["organizationId"])
        return opened

    def _observation(self, unused_workspace: str, baseline: dict, candidate: dict) -> dict:
        return {
            "schema": "renderdoc-ai-shader-organization-observation/v1",
            "scope": "captured-event-output",
            "accepted": True,
            "baselineCandidateRawSha256": baseline["rawSha256"],
            "candidateRawSha256": candidate["rawSha256"],
            "baselineSourceSha256": baseline["sourceSha256"],
            "candidateSourceSha256": candidate["sourceSha256"],
            "metrics": {"maxAbsoluteError": 0.000001},
            "tolerances": {"absolute": 0.00001, "relative": 0.001},
            "sampleCount": 256,
            "semanticEquivalenceVerified": False,
            "replayRestored": True,
        }

    def _changed_proposal(self, opened: dict) -> None:
        Path(opened["proposalSourcePath"]).write_text(
            "// semantic-change\nfloat4 MainPS() : SV_Target { return 1.000001; }\n",
            encoding="utf-8",
        )
