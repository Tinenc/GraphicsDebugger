from __future__ import annotations

import ast
import copy
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import pytest

from src import projects as projects_module
from src.projects import ReplayProjectService
from src.shader_workspace.flow_graph import _algorithm_nodes, build_shader_flow_graph
from src.shader_workspace.equivalence import build_verdict
from src.shader_workspace.organization import ShaderOrganizationManager
from src.symbols.dxbc import fingerprint_bytes
from src.symbols.matcher import capture_identity


REPOSITORY = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "shader_console.py"
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "replaylab_shader_console", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


console = _load_module()


def _writer_dxbc(code=b"compiled instructions", metadata=b""):
    body = code + b"\x00" * (-len(code) % 4)
    program = struct.pack("<2I", 0x00000050, 2 + len(body) // 4) + body
    chunks = [b"SHDR" + struct.pack("<I", len(program)) + program]
    if metadata:
        chunks.append(b"RDEF" + struct.pack("<I", len(metadata)) + metadata)
    header_size = 32 + len(chunks) * 4
    offsets = []
    cursor = header_size
    for chunk in chunks:
        offsets.append(cursor)
        cursor += len(chunk)
    return (
        b"DXBC" + b"\x00" * 16 + struct.pack("<I", 1) +
        struct.pack("<I", cursor) + struct.pack("<I", len(chunks)) +
        b"".join(struct.pack("<I", offset) for offset in offsets) +
        b"".join(chunks))


def _writer_interface():
    return {
        "interfaceComplete": True,
        "inputSignature": [],
        "outputSignature": [],
        "readOnlyResources": [],
        "readWriteResources": [],
        "samplers": [],
        "constantBuffers": [],
        "threadGroupSize": [],
        "outputTopology": "TriangleList",
        "dynamicLinkageInterfaceCount": 0,
    }


def _writer_compiler_binding(target_profile):
    binding = {
        "schema": "renderdoc-ultra-shader-compiler-binding/v1",
        "schemaVersion": 1,
        "targetProfile": copy.deepcopy(target_profile),
        "routePlanSha256": "3" * 64,
        "hops": [{
            "hop": 1,
            "routeId": "writer-hlsl-dxbc",
            "inputFormat": "hlsl",
            "outputFormat": "dxbc",
            "toolId": "writer-dxc",
            "operation": "compile",
            "toolConfigDigest": "1" * 64,
            "executableSha256": "2" * 64,
            "fixedFileHashes": {},
            "argumentTemplate": ["-T", "{profile}", "-E", "{entry_point}"],
            "placeholders": ["profile", "entry_point"],
        }],
        "executionAuthority": False,
        "evidenceOnly": True,
    }
    binding["bindingSha256"] = projects_module._canonical_sha256(binding)
    return binding


class WriterProjectShaderLibraryFixture:
    PROJECT_WORKSPACE_ID = "a" * 32

    def __init__(self, root):
        self.root = root
        self.selection = root / "config" / "active-project.json"
        self.registry = self.selection.parent / "project-registry.json"
        self.project_root = root / "writer-portable-project"
        self.replay_root = (
            self.project_root / ".replaylab" / "replay-project")
        self.selection.parent.mkdir(parents=True)
        for relative in (
                ".replaylab", "captures/catalogs", "workspaces/shaders",
                "shared/dictionaries/explanatory"):
            (self.project_root / relative).mkdir(parents=True, exist_ok=True)
        ReplayProjectService.create(
            self.replay_root, "Writer integration project",
            game_id="writer-game", build_id="writer-build",
            platform="windows-x64", api_family="d3d11")
        self.service = ReplayProjectService(self.replay_root)
        self._write_active_binding()

    @staticmethod
    def _seal(value, field):
        result = dict(value)
        result[field] = console._project_canonical_sha256(result)
        return result

    @staticmethod
    def _write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def _write_active_binding(self):
        workspace_manifest = self._seal({
            "schema": console.ACTIVE_PROJECT_WORKSPACE_SCHEMA,
            "schemaVersion": 1,
            "workspaceId": self.PROJECT_WORKSPACE_ID,
            "name": "Writer integration project",
            "state": "bound",
            "scope": {
                "gameId": "writer-game",
                "buildId": "writer-build",
                "platform": "windows-x64",
                "apiFamily": "d3d11",
            },
            "assetRoot": "",
            "layout": dict(console.ACTIVE_PROJECT_LAYOUT),
            "createdAt": "2026-08-28T00:00:00Z",
            "updatedAt": "2026-08-28T00:00:00Z",
        }, "configSha256")
        self._write(
            self.project_root / ".replaylab" / "workspace.json",
            workspace_manifest)
        shared_index = self._seal({
            "schema": console.SHARED_DICTIONARY_SCHEMA,
            "schemaVersion": console.SHARED_DICTIONARY_VERSION,
            "workspaceId": self.PROJECT_WORKSPACE_ID,
            "explanatory": [],
            "updatedAt": "2026-08-28T00:00:00Z",
        }, "indexSha256")
        self._write(
            self.project_root / "shared" / "dictionaries" / "index.json",
            shared_index)
        registry = self._seal({
            "schema": console.ACTIVE_PROJECT_REGISTRY_SCHEMA,
            "schemaVersion": 1,
            "projects": [{
                "workspaceId": self.PROJECT_WORKSPACE_ID,
                "projectRoot": str(self.project_root.resolve()),
                "name": "Writer integration project",
                "registeredAt": "2026-08-28T00:00:00Z",
            }],
            "updatedAt": "2026-08-28T00:00:00Z",
        }, "registrySha256")
        self._write(self.registry, registry)
        selection = self._seal({
            "schema": console.ACTIVE_PROJECT_SELECTION_SCHEMA,
            "schemaVersion": 1,
            "workspaceId": self.PROJECT_WORKSPACE_ID,
            "projectRoot": str(self.project_root.resolve()),
            "selectedAt": "2026-08-28T00:00:00Z",
        }, "selectionSha256")
        self._write(self.selection, selection)

    def catalog(self, label="writer-capture", resource_id=101):
        directory = self.root / label
        directory.mkdir()
        raw_path = directory / "shader.dxbc"
        raw_path.write_bytes(_writer_dxbc(b"writer baseline"))
        fingerprint = fingerprint_bytes(raw_path.read_bytes())
        assert fingerprint is not None
        shader = {
            "shaderResourceId": resource_id,
            "stage": "fragment",
            "entryPoint": "main",
            "rawPath": str(raw_path),
            "rawSize": raw_path.stat().st_size,
            "rawSha256": fingerprint.raw_sha256,
            "codeSha256": fingerprint.code_sha256,
        }
        shader.update(_writer_interface())
        catalog = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": str(directory / (label + ".rdc")),
            "api": "D3D11",
            "shaders": [shader],
            "uses": [],
        }
        capture_id = capture_identity(catalog, [shader])
        catalog["captureId"] = capture_id
        catalog_path = directory / "catalog.json"
        self._write(catalog_path, catalog)
        return catalog_path, shader, capture_id

    def workspace_status(self, label, capture_id, resource_id, raw_sha256,
                         code_sha256, abi_sha256="a" * 64):
        workspace_id = hashlib.md5(label.encode("utf-8")).hexdigest()
        workspace = (
            self.project_root / "workspaces" / "shaders" / workspace_id)
        (workspace / "source").mkdir(parents=True)
        (workspace / "ai").mkdir()
        source = workspace / "source" / "shader.hlsl"
        source.write_text(
            "float4 main() : SV_Target { return 0.5; }\n", encoding="utf-8")
        (workspace / "ai" / "analysis.md").write_text(
            "Writer-produced explanatory evidence.\n", encoding="utf-8")
        manifest_path = workspace / "manifest.json"
        manifest_path.write_text('{"validated":true}\n', encoding="utf-8")
        target_profile = {
            "kind": "DXBC", "stage": "fragment", "profile": "ps_5_0"}
        evidence_path = workspace / "evidence" / "shader.dxbc.asm"
        evidence_path.parent.mkdir()
        evidence_path.write_text(
            "ps_5_0\ndcl_output o0.xyzw\nmov o0.xyzw, l(1,1,1,1)\nret\n",
            encoding="utf-8")
        evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        return {
            "schema": "renderdoc-ultra-shader-workspace-status/v1",
            "workspacePath": str(workspace),
            "manifestPath": str(manifest_path),
            "workspaceId": workspace_id,
            "capture": {"captureId": capture_id, "api": "D3D11"},
            "shader": {"resourceId": resource_id, "rawSha256": raw_sha256},
            "stage": "fragment",
            "entryPoint": "main",
            "targetProfile": target_profile,
            "compilerBinding": _writer_compiler_binding(target_profile),
            "assemblyDisassemblyArtifacts": [{
                "kind": "native-disassembly",
                "role": "baseline",
                "format": "dxbc-asm",
                "path": str(evidence_path),
                "sha256": evidence_sha256,
                "byteSize": evidence_path.stat().st_size,
                "inputFormat": "dxbc",
                "inputRawSha256": raw_sha256,
                "routePlanSha256": "4" * 64,
                "routeId": "writer-dxbc-disassembly",
                "toolId": "writer-disassembler",
                "operation": "decompile",
                "toolConfigDigest": "5" * 64,
                "executableSha256": "6" * 64,
                "physicalEvidence": True,
                "displayEvidenceOnly": True,
                "editableSource": False,
                "applyAuthorization": False,
            }],
            "source": {
                "present": True,
                "path": str(source),
                "format": "hlsl",
                "lane": "executable-editable",
                "currentSha256": source_sha256,
                "decompiledSha256": source_sha256,
                "modifiedSinceDecompile": False,
            },
            "baseline": {
                "rawSha256": raw_sha256,
                "codeSha256": code_sha256,
                "abiSha256": abi_sha256,
                "kind": "dxbc",
            },
        }

    @staticmethod
    def inventory(resource_id, raw_sha256, layout_hash="7" * 64,
                  interface_hash="8" * 64, abi_hash="a" * 64):
        stable_identity = {
            "api": "d3d11",
            "encoding": "dxbc",
            "rawSha256": raw_sha256,
            "stage": "fragment",
            "entryPoint": "main",
            "abiHash": abi_hash,
            "layoutHash": layout_hash,
        }
        stable_key = projects_module._canonical_sha256(stable_identity)
        shader = {
            "resourceId": resource_id,
            "api": "d3d11",
            "encoding": "DXBC",
            "rawSha256": raw_sha256,
            "stage": "fragment",
            "entryPoint": "main",
            "abiHash": abi_hash,
            "layoutHash": layout_hash,
            "interfaceHash": interface_hash,
            "interfaceComplete": True,
            "stableKeySha256": stable_key,
            "anchorEventId": 23,
            "constantBlocks": [],
        }
        capture_id = projects_module._canonical_sha256([stable_key])
        inventory_sha256 = projects_module._canonical_sha256({
            "api": "d3d11",
            "shaders": [{
                "resourceId": resource_id,
                "stableKeySha256": stable_key,
                "anchorEventId": 23,
                "constantBlocks": [],
            }],
        })
        return {
            "schema": "renderdoc-ultra-project-shader-inventory/v1",
            "schemaVersion": 1,
            "capture": {
                "captureId": capture_id,
                "catalogSha256": inventory_sha256,
                "inventorySha256": inventory_sha256,
                "api": "d3d11",
            },
            "shaders": [shader],
        }

    @staticmethod
    def add_validated_candidate(status, candidate_blob, revision=3):
        workspace = Path(status["workspacePath"])
        source_path = Path(status["source"]["path"])
        source_bytes = source_path.read_bytes()
        revision_dir = workspace / "revisions" / ("%04d" % revision)
        revision_dir.mkdir(parents=True)
        candidate_path = revision_dir / "shader.bin"
        revision_source = revision_dir / "shader.hlsl"
        candidate_path.write_bytes(candidate_blob)
        revision_source.write_bytes(source_bytes)
        report_path = revision_dir / "abi-report.json"
        report_path.write_text('{"compatible":true}\n', encoding="utf-8")
        fingerprint = fingerprint_bytes(candidate_blob)
        assert fingerprint is not None
        persisted = {
            "revision": revision,
            "blobPath": candidate_path.relative_to(workspace).as_posix(),
            "sourcePath": revision_source.relative_to(workspace).as_posix(),
            "sourceSha256": hashlib.sha256(source_bytes).hexdigest(),
            "rawSha256": hashlib.sha256(candidate_blob).hexdigest(),
            "codeSha256": fingerprint.code_sha256,
            "byteSize": len(candidate_blob),
            "kind": fingerprint.kind,
            "validationScope": "replay-reflection-abi-compatible",
            "requiresReplayAbiValidation": False,
            "replayAbiValidated": True,
            "abiCompatible": True,
            "semanticEquivalenceVerified": False,
            "abiGate": {
                "status": "compatible",
                "baselineAbiSha256": status["baseline"]["abiSha256"],
                "candidateAbiSha256": "b" * 64,
                "requestId": "c" * 64,
                "reportPath": report_path.relative_to(workspace).as_posix(),
                "reportSha256": hashlib.sha256(
                    report_path.read_bytes()).hexdigest(),
            },
        }
        Path(status["manifestPath"]).write_text(json.dumps({
            "revision": revision, "latestCompiled": persisted,
        }), encoding="utf-8")
        status.update({
            "revision": revision,
            "latestCompiled": dict(
                persisted, candidateBlobPath=str(candidate_path.resolve()),
                applied=False),
            "replayAbiValidated": True,
            "abiCompatible": True,
            "replacementId": 777,
            "captureSessionId": "d" * 32,
        })
        return status


class ShaderConsoleFixture:
    AUTHORIZATION_ID = "a" * 32
    WORKSPACE_ID = "b" * 32
    LIVE_ID = "c" * 32
    WATCH_ID = "d" * 32
    CAPTURE_ID = "e" * 64
    RAW_SHA256 = "f" * 64

    @property
    def capture_binding(self):
        return console.normalize_capture_binding({
            "instanceId": "1234-fixture",
            "captureSessionId": "8" * 32,
            "capturePath": self._capture_path,
        })

    def __init__(self, root: Path, workspace_name: str = "workspace", *, capture_path=None):
        self.root = root
        self._capture_path = str(capture_path or root / "test.rdc")
        self.workspace = root / workspace_name
        self.source = self.workspace / "source" / "shader.hlsl"
        self.registry = root / "shader-live-edit-authorizations.v1.json"
        self.source.parent.mkdir(parents=True)
        (self.workspace / "watch").mkdir()
        (self.workspace / "live-edit").mkdir()
        (self.workspace / "ai").mkdir()
        self.source.write_text("float4 MainPS() : SV_Target\n{\n  return 1;\n}\n", encoding="utf-8")
        self._write(
            self.workspace / "manifest.json",
            {
                "schema": console.WORKSPACE_SCHEMA,
                "schemaVersion": 1,
                "workspaceId": self.WORKSPACE_ID,
                "capture": {"captureId": self.CAPTURE_ID,
                            "capturePath": self._capture_path},
                "shader": {
                    "rawSha256": self.RAW_SHA256,
                    "stage": "fragment",
                    "entryPoint": "MainPS",
                    "resourceId": 101,
                },
                "source": {
                    "path": "source/shader.hlsl",
                    "encoding": "utf-8",
                    "lane": "executable-editable",
                    "compileEligible": True,
                },
            },
        )
        self._write(
            self.registry,
            {
                "schema": console.REGISTRY_SCHEMA,
                "schemaVersion": 1,
                "entries": [
                    {
                        "authorizationId": self.AUTHORIZATION_ID,
                        "lastLiveEditId": self.LIVE_ID,
                        "workspacePath": str(self.workspace.resolve()),
                        "workspaceIdentitySha256": console._canonical_sha256(
                            {
                                "workspaceId": self.WORKSPACE_ID,
                                "captureId": self.CAPTURE_ID,
                                "capturePath": self._capture_path,
                                "shaderKey": "fragment:101:MainPS",
                                "shaderRawSha256": self.RAW_SHA256,
                                "stage": "fragment",
                                "entryPoint": "MainPS",
                                "resourceId": 101,
                            }
                        ),
                        "debounceMs": 100,
                        "authorizedAt": "2026-08-25T04:00:00Z",
                        "updatedAt": "2026-08-25T04:00:00Z",
                        "workspaceIdentity": {
                            "workspaceId": self.WORKSPACE_ID,
                            "captureId": self.CAPTURE_ID,
                            "capturePath": self._capture_path,
                            "shaderKey": "fragment:101:MainPS",
                            "shaderRawSha256": self.RAW_SHA256,
                            "stage": "fragment",
                            "entryPoint": "MainPS",
                            "resourceId": 101,
                        },
                    }
                ],
            },
        )
        self.organization_manager = ShaderOrganizationManager(lambda: None, lambda *unused: None)
        self.write_organization_gate()
        self.write_live(state="stopped", activeReplacement=None,
                        applyAttempts=0, applySuccesses=0)
        self.write_watch(state="stopped")

    def write_organization_gate(self, level="exact-code", *, baseline_source=None,
                                proposal_source=None, rename_map=None):
        baseline = self.source.read_bytes() if baseline_source is None else baseline_source.encode("utf-8")
        proposal = baseline if proposal_source is None else proposal_source.encode("utf-8")
        baseline_sha = hashlib.sha256(baseline).hexdigest()
        proposal_sha = hashlib.sha256(proposal).hexdigest()
        if level == "rejected":
            raise ValueError("a rejected candidate cannot be published")
        final_sha = proposal_sha
        baseline_code = "2" * 64
        organized_code = baseline_code if level == "exact-code" else "9" * 64
        final_code = organized_code
        self.source.write_bytes(proposal)
        self.session_dir = self.workspace / "ai" / "organization-sessions" / ("1" * 32)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.baseline_artifact = self.session_dir / "baseline-source.hlsl"
        self.proposal_artifact = self.session_dir / "organized-proposal.hlsl"
        self.baseline_artifact.write_bytes(baseline)
        self.proposal_artifact.write_bytes(proposal)
        observation = None
        if level == "observational-pass":
            observation = {
                "schema": "renderdoc-ai-shader-organization-observation/v1",
                "scope": "captured-event-output", "accepted": True,
                "semanticEquivalenceVerified": False, "replayRestored": True,
                "baselineSourceSha256": baseline_sha, "candidateSourceSha256": proposal_sha,
                "baselineCandidateRawSha256": "6" * 64, "candidateRawSha256": "3" * 64,
                "metrics": {"maxAbsoluteError": 0.000001},
                "tolerances": {"absolute": 0.00001, "relative": 0.001},
                "sampleCount": 4,
            }
        verdict = build_verdict(accepted=True, level=level)
        gate = {
                "schema": console.ORGANIZATION_GATE_SCHEMA,
                "schemaVersion": 1,
                "policyVersion": console.ORGANIZATION_POLICY_VERSION,
                "organizationId": "1" * 32,
                "reviewedAt": "2026-08-25T04:00:00Z",
                "requestedTransformation": "organize-only",
                "workspaceId": self.WORKSPACE_ID,
                "captureId": self.CAPTURE_ID,
                "stage": "fragment",
                "entryPoint": "MainPS",
                "baselineSourceSha256": baseline_sha,
                "proposalSourceSha256": proposal_sha,
                "baselineCodeSha256": baseline_code,
                "organizedCodeSha256": organized_code,
                "baselineReviewRevision": 6,
                "baselineReviewCandidateRawSha256": "6" * 64,
                "baselineReviewAbiRequestId": "7" * 64,
                "baselineReviewAbiReportSha256": "8" * 64,
                "decision": "accepted-executable-editable",
                "codeSha256Equal": level == "exact-code",
                "equivalenceLevel": level,
                "observation": observation,
                "renameMap": dict(rename_map or {}),
                "verdict": verdict,
                "organizedSourcePromoted": True,
                "finishedCurrentDraft": True,
                "publicationContract": "validated-source",
                "flowGraphId": "",
                "flowGraphSha256": "",
                "flowGraphArtifactPath": str(self.workspace / "ai" / "flow-graph.json"),
                "baselineCompiledAndAbiValidated": True,
                "organizedCompiledAndAbiValidated": True,
                "rawRecoveryCompiledAndAbiValidated": False,
                "semanticEquivalenceVerified": False,
                "safeToStartLiveEdit": True,
                "replayApplied": False,
                "executableEditableSourcePath": "source/shader.hlsl",
                "proposalSourcePath": str(self.proposal_artifact.resolve()),
                "finalSourceSha256": final_sha,
                "finalBinding": {
                    "complete": True,
                    "sourceSha256": final_sha,
                    "revision": 7,
                    "candidateRawSha256": "3" * 64,
                    "codeSha256": final_code,
                    "abiRequestId": "4" * 64,
                    "abiReportSha256": "5" * 64,
                },
                "finalRevision": 7,
                "finalCandidateRawSha256": "3" * 64,
                "finalCodeSha256": final_code,
                "finalAbiRequestId": "4" * 64,
                "finalAbiReportSha256": "5" * 64,
            }
        self._write(self.workspace / "ai" / "organize-code-gate.json", gate)
        self.sync_organization_session()
        return gate

    def sync_organization_session(self):
        gate = json.loads((self.workspace / "ai" / "organize-code-gate.json").read_bytes())
        session = {
            "schema": "renderdoc-ai-shader-organization-session/v1",
            "schemaVersion": 1, "policyVersion": console.ORGANIZATION_POLICY_VERSION,
            "state": "completed", "createdAt": "2026-08-25T03:00:00Z",
            "completedAt": gate["reviewedAt"], "workspacePath": str(self.workspace.resolve()),
            "sourcePath": "source/shader.hlsl",
            "baselineSourcePath": self.baseline_artifact.relative_to(self.workspace).as_posix(),
            "proposalSourcePath": self.proposal_artifact.relative_to(self.workspace).as_posix(),
            "structuredFlowGraphPath": (self.session_dir / "flow-graph.json").relative_to(self.workspace).as_posix(),
            "baselineCompiledAndAbiValidated": True, "semanticEquivalenceVerified": False,
            "baselineRevision": 6, "baselineCandidateRawSha256": "6" * 64,
            "baselineAbiRequestId": "7" * 64, "baselineAbiReportSha256": "8" * 64,
            "replayApplied": False, "commentMap": {},
        }
        for name in (
            "organizationId", "workspaceId", "captureId", "stage", "entryPoint",
            "baselineSourceSha256", "proposalSourceSha256", "baselineCodeSha256",
            "organizedCodeSha256", "codeSha256Equal", "equivalenceLevel", "observation",
            "verdict", "renameMap", "decision", "organizedSourcePromoted",
            "finalBinding", "flowGraphId", "flowGraphSha256",
            "finishedCurrentDraft", "publicationContract",
        ):
            if name in gate:
                session[name] = copy.deepcopy(gate[name])
        session = self.organization_manager._seal_session(session)
        self._write(self.session_dir / "session.json", session)
        self._write(self.session_dir / "gate.json", gate)

    def write_editable_draft(self):
        baseline = self.baseline_artifact.read_bytes()
        proposal = b"// current unvalidated draft\n" + baseline
        self.source.write_bytes(proposal)
        self.proposal_artifact.write_bytes(proposal)
        source_sha = hashlib.sha256(proposal).hexdigest()
        gate_path = self.workspace / "ai" / "organize-code-gate.json"
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        gate.update({
            "decision": "editable-draft",
            "proposalSourceSha256": source_sha,
            "editableDraftSourcePath": str(self.source.resolve()),
            "safeToStartLiveEdit": False,
            "organizedSourcePromoted": False,
            "finalBinding": {"complete": False, "sourceSha256": source_sha,
                             "revision": None, "candidateRawSha256": "",
                             "codeSha256": "", "abiRequestId": "",
                             "abiReportSha256": ""},
        })
        self._write(gate_path, gate)
        session_path = self.session_dir / "session.json"
        session = json.loads(session_path.read_text(encoding="utf-8"))
        session.update({
            "state": "drafting",
            "proposalSourceSha256": source_sha,
            "syncedDraftSourceSha256": source_sha,
            "shaderProgramIdentity": {"originalRawSha256": self.RAW_SHA256},
        })
        self._write(session_path, session)
        return gate

    @staticmethod
    def _write(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def write_watch(self, events=None, **changes):
        current_sha256 = console.hashlib.sha256(self.source.read_bytes()).hexdigest()
        gate = json.loads(
            (self.workspace / "ai" / "organize-code-gate.json").read_text(
                encoding="utf-8"
            )
        )
        value = {
            "schema": console.WATCH_SCHEMA,
            "schemaVersion": 1,
            "watchId": self.WATCH_ID,
            "workspacePath": str(self.workspace.resolve()),
            "sourcePath": str(self.source.resolve()),
            "state": "running",
            "compileInProgress": False,
            "pending": False,
            "caughtUp": True,
            "initialSourceSha256": gate["finalSourceSha256"],
            "observedSourceSha256": current_sha256,
            "lastAttemptedSourceSha256": current_sha256,
            "lastCompiledSourceSha256": current_sha256,
            "latestCandidateRevision": 2,
            "compileAttempts": 1,
            "compileSuccesses": 1,
            "compileFailures": 0,
            "events": events or [],
        }
        value.update(changes)
        self._write(self.workspace / "watch" / (self.WATCH_ID + ".json"), value)

    def write_live(self, events=None, **changes):
        value = {
            "schema": console.LIVE_EDIT_SCHEMA,
            "schemaVersion": 1,
            "captureBinding": self.capture_binding,
            "liveEditId": self.LIVE_ID,
            "watchId": self.WATCH_ID,
            "workspacePath": str(self.workspace.resolve()),
            "state": "running",
            "applyAttempts": 1,
            "applySuccesses": 1,
            "applyRejections": 0,
            "applyFailures": 0,
            "activeReplacement": {"revision": 2},
            "events": events or [],
        }
        value.update(changes)
        self._write(self.workspace / "live-edit" / (self.LIVE_ID + ".json"), value)



def _console_legacy_algorithm_graph(fixture):
    calculations = [
        ("float4 color = float4(uv, 0, 1);", "读取像素坐标", ["uv"], ["color"]),
        ("float weight = 0.5;", "准备混合权重", [], ["weight"]),
        ("color = color * weight;", "缩放基础颜色", ["color", "weight"], ["color"]),
        ("float4 bias = float4(0.1, 0.1, 0.1, 0);", "准备颜色偏移", [], ["bias"]),
        ("color = color + bias;", "合并颜色偏移", ["color", "bias"], ["color"]),
        ("float gain = 1.1;", "准备曝光系数", [], ["gain"]),
        ("color = color * gain;", "调整颜色曝光", ["color", "gain"], ["color"]),
        ("float alpha = color.a;", "读取透明度", ["color"], ["alpha"]),
        ("alpha = saturate(alpha);", "约束透明度范围", ["alpha"], ["alpha"]),
        ("color.a = alpha;", "写回透明度", ["alpha"], ["color.a"]),
        ("color.rgb = saturate(color.rgb);", "约束颜色范围", ["color.rgb"], ["color.rgb"]),
        ("return color;", "输出像素颜色", ["color"], ["SV_Target0"]),
    ]
    source = "float4 MainPS(float2 uv : TEXCOORD0) : SV_Target0\n{\n" + "".join(
        "  %s\n" % statement for statement, unused_label, unused_reads, unused_writes in calculations
    ) + "}\n"
    fixture.source.write_text(source, encoding="utf-8", newline="\n")
    source_sha256 = hashlib.sha256(fixture.source.read_bytes()).hexdigest()
    stages = [
        {
            "id": "step-%02d" % index,
            "label": label,
            "function": "MainPS",
            "sourceRange": {"startLine": index + 3, "endLine": index + 3},
            "reads": reads,
            "writes": writes,
            "next": ["step-%02d" % (index + 1)] if index + 1 < len(calculations) else [],
        }
        for index, (unused_statement, label, reads, writes) in enumerate(calculations)
    ]
    graph = build_shader_flow_graph(
        source,
        workspace_id=fixture.WORKSPACE_ID,
        source_path="source/shader.hlsl",
        source_sha256=source_sha256,
        source_revision=7,
        stage="fragment",
        entry_point="MainPS",
        generated_at="2026-09-05T00:00:00Z",
        origin="organized-proposal",
        flow_spec={"stages": stages},
    )
    # Reconstruct a pre-function-only v1 artifact to retain compatibility
    # coverage. The Host no longer publishes this shape from ``flow_spec``.
    nodes, edges = _algorithm_nodes(
        source, {"stages": stages}, fixture.WORKSPACE_ID, "source/shader.hlsl")
    graph.update({
        "nodes": nodes,
        "edges": edges,
        "graphKind": "algorithm-dataflow",
        "layoutDirection": "TD",
        "truncated": {"nodes": False, "edges": False,
                      "omittedNodes": 0, "omittedEdges": 0},
        "policy": {
            "navigationOnly": True,
            "physicalEvidence": False,
            "compileAuthorization": False,
            "applyAuthorization": False,
            "callEdgesDerivedFromFinalSource": False,
            "descriptionsMayBeAiAuthored": True,
            "computationRangesBoundToFinalSource": True,
            "dataEdgesAreLexicallyBound": True,
        },
    })
    material = dict(graph)
    material.pop("graphId", None)
    material.pop("generatedAt", None)
    graph["graphId"] = console._canonical_sha256(material)[:32]
    fixture.write_organization_gate(baseline_source=source, proposal_source=source)
    _publish_console_legacy_algorithm_graph(fixture, graph)
    return graph


def _publish_console_legacy_algorithm_graph(fixture, graph):
    graph_path = fixture.workspace / "ai" / "flow-graph.json"
    fixture._write(graph_path, graph)
    gate_path = fixture.workspace / "ai" / "organize-code-gate.json"
    gate = json.loads(gate_path.read_bytes())
    gate.update({
        "flowGraphId": graph["graphId"],
        "flowGraphSha256": hashlib.sha256(graph_path.read_bytes()).hexdigest(),
        "flowGraphArtifactPath": str(graph_path.resolve()),
    })
    fixture._write(gate_path, gate)
    (fixture.session_dir / "flow-graph.json").write_bytes(graph_path.read_bytes())
    fixture.sync_organization_session()


def _validate_console_legacy_algorithm_graph(fixture, graph):
    return console._validate_shader_flow_graph_value(
        graph,
        expected_workspace_id=fixture.WORKSPACE_ID,
        expected_source_sha256=hashlib.sha256(fixture.source.read_bytes()).hexdigest(),
        expected_source_revision=7,
        expected_source_path="source/shader.hlsl",
        expected_stage="fragment",
        expected_entry_point="MainPS",
    )


class ProjectShaderLibraryFixture:
    PROJECT_WORKSPACE_ID = "1" * 32
    REPLAY_PROJECT_ID = "2" * 32
    SOURCE_WORKSPACE_ID = "3" * 32
    CAPTURE_ID = "4" * 64
    CATALOG_SHA256 = "5" * 64
    RAW_SHA256 = "6" * 64
    CODE_SHA256 = "7" * 64
    LAYOUT_SHA256 = "8" * 64
    INTERFACE_SHA256 = "9" * 64
    ABI_SHA256 = "a" * 64

    def __init__(self, root: Path, compiled_candidate: bool = False,
                 persist_origin_resource_id: bool = True):
        self.root = root
        self.selection = root / "config" / "active-project.json"
        self.registry = self.selection.parent / "project-registry.json"
        self.project_root = root / "portable-project"
        self.replay_root = self.project_root / ".replaylab" / "replay-project"
        self.project_manifest = self.replay_root / "project.json"
        self.selection.parent.mkdir(parents=True)
        for relative in (
            ".replaylab",
            "captures/catalogs",
            "workspaces/shaders",
            "shared/dictionaries/explanatory",
            ".replaylab/replay-project/knowledge/workspaces",
            ".replaylab/replay-project/knowledge/workspaces/binaries",
            ".replaylab/replay-project/knowledge/aliases",
        ):
            (self.project_root / relative).mkdir(parents=True, exist_ok=True)

        self.source_text = "float4 MainPS() : SV_Target\n{\n  return 1;\n}\n"
        self.source_sha256 = console.hashlib.sha256(
            self.source_text.encode("utf-8")).hexdigest()
        material = {
            "workspaceId": self.SOURCE_WORKSPACE_ID,
            "captureId": self.CAPTURE_ID,
            "originCatalogSha256": self.CATALOG_SHA256,
            "stage": "fragment",
            "entryPoint": "MainPS",
            "rawSha256": self.RAW_SHA256,
            "codeSha256": self.CODE_SHA256,
            "layoutHash": self.LAYOUT_SHA256,
            "interfaceHash": self.INTERFACE_SHA256,
            "interfaceComplete": True,
            "baselineAbiSha256": self.ABI_SHA256,
            "targetProfile": {"profile": "ps_6_0", "entryPoint": "MainPS"},
            "sourceSha256": self.source_sha256,
            "analysisSha256": "",
        }
        if persist_origin_resource_id:
            material["originResourceId"] = 101
        self.compiled_bytes = b"DXIL\x00verified-project-candidate"
        self.compiled_raw_sha256 = hashlib.sha256(self.compiled_bytes).hexdigest()
        self.compiled_candidate = None
        if compiled_candidate:
            compiled_identity = {
                "schema": console.COMPILED_CANDIDATE_SCHEMA,
                "schemaVersion": 1,
                "rawSha256": self.compiled_raw_sha256,
                "codeSha256": self.CODE_SHA256,
                "byteSize": len(self.compiled_bytes),
                "kind": "dxil",
                "sourceSha256": self.source_sha256,
                "sourceRevision": 1,
                "targetProfile": dict(material["targetProfile"]),
                "originBaselineAbiSha256": self.ABI_SHA256,
                "originCandidateAbiSha256": "c" * 64,
                "originAbiReportSha256": "d" * 64,
                "originRequestId": "e" * 64,
                "validationScope": "replay-reflection-abi-compatible",
                "semanticEquivalenceVerified": False,
            }
            material["compiledCandidate"] = dict(compiled_identity)
            self.compiled_candidate = dict(compiled_identity)
            self.compiled_candidate["artifactPath"] = (
                "knowledge/workspaces/binaries/%s.bin" %
                self.compiled_raw_sha256)
        self.knowledge_id = console._project_canonical_sha256(material)[:32]
        self.source = (
            self.replay_root / "knowledge" / "workspaces" /
            (self.knowledge_id + ".hlsl"))
        self.source.write_text(self.source_text, encoding="utf-8", newline="")
        record = dict(material)
        if self.compiled_candidate is not None:
            record["compiledCandidate"] = dict(self.compiled_candidate)
            self.compiled_artifact = (
                self.replay_root / "knowledge" / "workspaces" / "binaries" /
                (self.compiled_raw_sha256 + ".bin"))
            self.compiled_artifact.write_bytes(self.compiled_bytes)
        else:
            self.compiled_artifact = None
        record.update({
            "knowledgeId": self.knowledge_id,
            "sourcePath": "knowledge/workspaces/%s.hlsl" % self.knowledge_id,
            "sourceOriginPath": str(self.source.resolve()),
            "workspacePath": str((self.project_root / "workspaces" / "shaders" /
                                  self.SOURCE_WORKSPACE_ID).resolve()),
            "workspaceManifestSha256": "b" * 64,
            "registeredAt": "2026-08-25T04:00:00Z",
        })
        self.replay_value = {
            "schema": console.REPLAY_PROJECT_SCHEMA,
            "schemaVersion": 1,
            "projectId": self.REPLAY_PROJECT_ID,
            "name": "Test Replay Project",
            "scope": {
                "gameId": "test-game",
                "buildId": "test-build",
                "platform": "windows",
                "apiFamily": "d3d12",
            },
            "createdAt": "2026-08-25T04:00:00Z",
            "updatedAt": "2026-08-25T04:00:00Z",
            "captures": [{
                "captureId": self.CAPTURE_ID,
                "capturePath": str(self.root / "test.rdc"),
                "api": "D3D12",
                "catalogPath": "captures/catalogs/test.json",
                "catalogSha256": self.CATALOG_SHA256,
                "importedAt": "2026-08-25T04:00:00Z",
                "shaders": [{
                    "resourceId": 101,
                    "stage": "fragment",
                    "entryPoint": "MainPS",
                    "rawSha256": self.RAW_SHA256,
                    "codeSha256": self.CODE_SHA256,
                    "layoutHash": self.LAYOUT_SHA256,
                    "interfaceHash": self.INTERFACE_SHA256,
                    "interfaceComplete": True,
                    "encoding": "dxil",
                    "eventIds": [10],
                }],
            }],
            "knowledge": {
                "workspaces": [record], "aliases": [], "materials": [],
            },
            "activeKnowledge": {
                "revision": 1,
                "workspaces": {self.knowledge_id: 1},
                "materials": {},
            },
        }
        self.write_replay_manifest()

        self.workspace_manifest = self._seal({
            "schema": console.ACTIVE_PROJECT_WORKSPACE_SCHEMA,
            "schemaVersion": 1,
            "workspaceId": self.PROJECT_WORKSPACE_ID,
            "name": "Portable Test Project",
            "state": "bound",
            "scope": {
                "gameId": "test-game",
                "buildId": "test-build",
                "platform": "windows",
                "apiFamily": "d3d12",
            },
            "assetRoot": "",
            "layout": dict(console.ACTIVE_PROJECT_LAYOUT),
            "createdAt": "2026-08-25T04:00:00Z",
            "updatedAt": "2026-08-25T04:00:00Z",
        }, "configSha256")
        self._write(
            self.project_root / ".replaylab" / "workspace.json",
            self.workspace_manifest)
        self.shared_index = self._seal({
            "schema": console.SHARED_DICTIONARY_SCHEMA,
            "schemaVersion": console.SHARED_DICTIONARY_VERSION,
            "workspaceId": self.PROJECT_WORKSPACE_ID,
            "explanatory": [],
            "updatedAt": "2026-08-25T04:00:00Z",
        }, "indexSha256")
        self._write(
            self.project_root / "shared" / "dictionaries" / "index.json",
            self.shared_index)
        self.registry_value = self._seal({
            "schema": console.ACTIVE_PROJECT_REGISTRY_SCHEMA,
            "schemaVersion": 1,
            "projects": [{
                "workspaceId": self.PROJECT_WORKSPACE_ID,
                "projectRoot": str(self.project_root.resolve()),
                "name": "Portable Test Project",
                "registeredAt": "2026-08-25T04:00:00Z",
            }],
            "updatedAt": "2026-08-25T04:00:00Z",
        }, "registrySha256")
        self._write(self.registry, self.registry_value)
        self.selection_value = self._seal({
            "schema": console.ACTIVE_PROJECT_SELECTION_SCHEMA,
            "schemaVersion": 1,
            "workspaceId": self.PROJECT_WORKSPACE_ID,
            "projectRoot": str(self.project_root.resolve()),
            "selectedAt": "2026-08-25T04:00:00Z",
        }, "selectionSha256")
        self._write(self.selection, self.selection_value)

    @staticmethod
    def _seal(value, field):
        result = dict(value)
        result[field] = console._project_canonical_sha256(result)
        return result

    @staticmethod
    def _write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def write_replay_manifest(self):
        self._write(self.project_manifest, self.replay_value)


class ImportedProjectSourceFixture:
    def __init__(self, root, managed_name=None):
        from src.shader_workspace.project_source import source_receipt
        self.project = ProjectShaderLibraryFixture(root)
        project = self.project
        class ImportedEditor(ShaderConsoleFixture):
            RAW_SHA256 = ProjectShaderLibraryFixture.RAW_SHA256
        self.editor = (ImportedEditor(project.project_root / "workspaces" / "shaders" / "v2",
                                      workspace_name=managed_name) if managed_name else
                       ImportedEditor(project.project_root / "workspaces" / "shaders" / "target"))
        editor = self.editor
        editor.source.write_bytes(project.source.read_bytes())
        editor.write_organization_gate()
        editor.write_watch()
        editor.write_live()
        (editor.workspace / "ai" / "organize-code-gate.json").unlink()
        record = project.replay_value["knowledge"]["workspaces"][0]
        bundle = {
            "schema": "replaylab-verified-project-shader-source/v1",
            "projectId": project.REPLAY_PROJECT_ID, "projectPath": str(project.replay_root),
            "knowledgeId": project.knowledge_id, "sourceSha256": project.source_sha256,
            "sourceEntryPoint": "MainPS", "originalRawSha256": project.RAW_SHA256,
            "baselineAbiSha256": project.ABI_SHA256, "stage": "fragment", "entryPoint": "MainPS",
            "targetProfile": record["targetProfile"], "originWorkspaceId": project.SOURCE_WORKSPACE_ID,
            "originWorkspacePath": record["workspacePath"], "analysisSha256": "",
            "applied": False, "requiresCompile": True, "requiresLiveAbiRevalidation": True,
            "semanticEquivalenceVerified": False,
        }
        relative = "source-inputs/project-%s.hlsl" % project.source_sha256
        self.input_path = editor.workspace / relative
        self.input_path.parent.mkdir()
        self.input_path.write_bytes(editor.source.read_bytes())
        path = editor.workspace / "manifest.json"
        self.manifest = json.loads(path.read_bytes())
        self.manifest.update(baseline={"abiSha256": project.ABI_SHA256}, targetProfile=record["targetProfile"])
        self.manifest["source"].update(origin="verified-project-source", sha256=project.source_sha256,
                                       entryPoint="MainPS", projectSource=source_receipt(bundle, relative))
        editor._write(path, self.manifest)

    def install_graph(self):
        project = self.project
        graph = build_shader_flow_graph(project.source_text,
            workspace_id=project.SOURCE_WORKSPACE_ID, source_path="source/shader.hlsl",
            source_sha256=project.source_sha256, source_revision=7, stage="fragment",
            entry_point="MainPS", generated_at="2026-09-05T00:00:00Z", origin="organized-proposal")
        payload = json.dumps(graph, ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        relative = "shared/dictionaries/explanatory/%s.flow-graph.json" % digest
        (project.project_root / relative).write_bytes(payload)
        project.shared_index["explanatory"] = [{"knowledgeId": project.knowledge_id,
            "kind": "shader-flow-graph", "artifactPath": relative, "artifactSha256": digest,
            "registeredAt": "2026-09-05T00:00:00Z", "policy": {"explanatoryOnly": True,
            "physicalEvidence": False, "applyAuthorization": False, "aiProvisionalAutoApply": False}}]
        project.shared_index.pop("indexSha256")
        project.shared_index = project._seal(project.shared_index, "indexSha256")
        project._write(project.project_root / "shared/dictionaries/index.json", project.shared_index)
        return graph


def _open_imported_organization_fixture(tmp_path):
    fixture = ImportedProjectSourceFixture(tmp_path, managed_name="a" * 64)
    editor = fixture.editor
    graph = fixture.install_graph()
    gate = editor.write_organization_gate()
    gate.update(decision="drafting", organizedSourcePromoted=False,
                sourceRestored=True, safeToStartLiveEdit=False,
                finalBinding={"complete": False, "sourceSha256": fixture.project.source_sha256})
    # A newly opened organization preserves the canonical registered source;
    # only its separate proposal may be changed by the Agent.
    gate.pop("editableDraftSourcePath", None)
    editor._write(editor.workspace / "ai/organize-code-gate.json", gate)
    session_path = editor.session_dir / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["workspacePath"] = str(editor.workspace)
    session["state"] = "drafting"
    session["shaderProgramIdentity"] = {"originalRawSha256": editor.RAW_SHA256}
    editor._write(session_path, session)
    return fixture, graph


@pytest.mark.parametrize("registry_state", ("missing", "stale", "current"))
def test_open_organization_registered_source_remains_editable_without_auto_apply(tmp_path, registry_state):
    fixture, graph = _open_imported_organization_fixture(tmp_path)
    editor = fixture.editor
    registry_path = editor.registry
    if registry_state == "missing":
        registry_path = tmp_path / "missing-registry.json"
    elif registry_state == "stale":
        live_path = editor.workspace / "live-edit" / (editor.LIVE_ID + ".json")
        live = json.loads(live_path.read_text(encoding="utf-8"))
        live["captureBinding"]["captureSessionId"] = "9" * 32
        editor._write(live_path, live)
    before_source = editor.source.read_bytes()
    before_registry = editor.registry.read_bytes()
    before_gate = (editor.workspace / "ai/organize-code-gate.json").read_bytes()
    with mock.patch.object(console, "default_active_project_selection_path",
                           return_value=str(fixture.project.selection)):
        snapshot = console.load_console_snapshot(str(registry_path), capture_binding=editor.capture_binding)
        assert snapshot["errors"] == []
        assert len(snapshot["workspaces"]) == 1
        model = snapshot["workspaces"][0]
        assert model["editorEligible"] is True
        assert model["captureBinding"] == editor.capture_binding
        assert model["flowGraph"]["graphId"] == graph["graphId"]
        opened = console.load_authorized_source_document(
            str(registry_path), model["authorizationId"], model["workspaceId"],
            capture_binding=editor.capture_binding)
        assert opened["rawSha256"] == fixture.project.source_sha256
        assert opened["text"] == before_source.decode("utf-8")
        if registry_state == "current":
            # An already valid current-session model wins; discovery must not
            # add a duplicate tree node or replace an existing authorization.
            assert model["authorizationId"] == editor.AUTHORIZATION_ID
        else:
            assert model["editorEligibility"] == "organization-source-preview"
            assert model["readOnly"] is False
            assert model["autoApplyReady"] is False
            assert model["liveEdit"] is None and model["watch"] is None
            assert opened["readOnly"] is False
            assert opened["liveEditId"] is None and opened["watchId"] is None
            with pytest.raises(console.ShaderConsoleError):
                console._require_running_live_edit(model, str(editor.source))
    assert editor.source.read_bytes() == before_source
    assert editor.registry.read_bytes() == before_registry
    assert (editor.workspace / "ai/organize-code-gate.json").read_bytes() == before_gate


@pytest.mark.parametrize("stage,extension,compile_eligible", [
    ("fragment", "hlsl", True), ("vertex", "hlsl", False),
    ("compute", "glsl", False), ("geometry", "glsl", True),
])
@pytest.mark.parametrize("gate_decision", (None, "drafting", "awaiting-proposal-review"))
def test_managed_source_is_editable_before_organization_or_compiler_validation(
        tmp_path, stage, extension, compile_eligible, gate_decision):
    project = WriterProjectShaderLibraryFixture(tmp_path)
    fixture = ShaderConsoleFixture(
        project.project_root / "workspaces/shaders/v2", workspace_name="b" * 64)
    manifest_path = fixture.workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = fixture.source.with_suffix("." + extension)
    fixture.source.rename(target)
    manifest["stage"] = manifest["shader"]["stage"] = stage
    manifest["source"].update(path="source/shader." + extension,
                              compileEligible=compile_eligible)
    fixture._write(manifest_path, manifest)
    gate_path = fixture.workspace / "ai/organize-code-gate.json"
    if gate_decision is None:
        gate_path.unlink()
    else:
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        gate["decision"] = gate_decision
        fixture._write(gate_path, gate)
    before = {p: p.read_bytes() for p in fixture.workspace.rglob("*") if p.is_file()}
    registry = str(tmp_path / "no-live-edit.json")
    with mock.patch.object(console, "default_active_project_selection_path",
                           return_value=str(project.selection)):
        snapshot = console.load_console_snapshot(registry, capture_binding=fixture.capture_binding)
        assert snapshot["errors"] == []
        assert len(snapshot["workspaces"]) == 1
        model = snapshot["workspaces"][0]
        assert model["editorEligible"] is True and model["readOnly"] is False
        assert model["editorEligibility"] == "managed-source"
        assert model["managedSourceLocator"] is True
        assert model["activeOrganization"] is (gate_decision is not None)
        assert model["autoApplyReady"] is False
        assert model["liveEdit"] is None and model["watch"] is None
        opened = console.load_authorized_source_document(
            registry, model["authorizationId"], model["workspaceId"], fixture.capture_binding)
        assert opened["sourcePath"] == str(target)
        assert opened["text"] == target.read_bytes().decode("utf-8")
        assert opened["readOnly"] is False and opened["sourceEditable"] is True
        assert opened["unvalidatedDraft"] is True
        assert opened["liveEditId"] is None and opened["watchId"] is None
        with pytest.raises(console.ShaderConsoleError):
            console._require_running_live_edit(model, str(target))
    assert before == {p: p.read_bytes() for p in fixture.workspace.rglob("*") if p.is_file()}


@pytest.mark.parametrize("changed", ("canonical", "baseline", "session", "project-input", "capture"))
def test_open_organization_preview_rejects_changed_source_or_scope(tmp_path, changed):
    fixture, _graph = _open_imported_organization_fixture(tmp_path)
    editor = fixture.editor
    binding = editor.capture_binding
    with mock.patch.object(console, "default_active_project_selection_path",
                           return_value=str(fixture.project.selection)):
        authorization = console._managed_project_draft_authorizations(binding)[0][0]
        if changed == "canonical":
            editor.source.write_bytes(editor.source.read_bytes() + b"// unsaved newer source")
        elif changed == "baseline":
            editor.baseline_artifact.write_bytes(b"changed baseline")
        elif changed == "project-input":
            fixture.input_path.write_bytes(b"changed project input")
        elif changed == "session":
            path = editor.session_dir / "session.json"
            session = json.loads(path.read_text(encoding="utf-8"))
            session["workspaceId"] = "0" * 32
            editor._write(path, session)
        else:
            binding = dict(binding, capturePath=str(tmp_path / "another.rdc"))
        with pytest.raises(console.ShaderConsoleError):
            console.load_authorized_source_document(
                str(tmp_path / "missing-registry.json"), authorization["authorizationId"],
                editor.WORKSPACE_ID, capture_binding=binding)


class ShaderConsoleLogicTests(unittest.TestCase):
    def test_legacy_host_entry_description_preserves_source_bound_navigation(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            source_bytes = fixture.source.read_bytes()
            graph = build_shader_flow_graph(
                source_bytes.decode("utf-8"),
                workspace_id=fixture.WORKSPACE_ID,
                source_path="source/shader.hlsl",
                source_sha256=hashlib.sha256(source_bytes).hexdigest(),
                source_revision=7, stage="fragment", entry_point="MainPS",
                generated_at="2026-08-31T00:00:00Z", origin="organized-proposal",
            )
            graph_path = fixture.workspace / "ai" / "flow-graph.json"

            def publish(description):
                graph["nodes"][0]["description"] = description
                material = dict(graph)
                material.pop("graphId")
                material.pop("generatedAt")
                graph["graphId"] = console._canonical_sha256(material)[:32]
                fixture._write(graph_path, graph)
                return console.load_console_snapshot(
                    str(fixture.registry), capture_binding=fixture.capture_binding)["workspaces"][0]

            legacy = "Shader entry point; the stage result is produced here."
            loaded = publish(legacy)
            self.assertEqual("current", loaded["flowGraphStatus"]["status"])
            self.assertEqual(legacy, loaded["flowGraph"]["nodes"][0]["description"])
            # The narrow compatibility exception must not accept appended code
            # or bypass the current source identity checks.
            rejected = publish(legacy + " return color;")
            self.assertEqual("invalid", rejected["flowGraphStatus"]["status"])
            self.assertIsNone(rejected["flowGraph"])
            graph["sourceSha256"] = "0" * 64
            rejected = publish(legacy)
            self.assertEqual("invalid", rejected["flowGraphStatus"]["status"])
            self.assertIsNone(rejected["flowGraph"])
            self.assertEqual(source_bytes, fixture.source.read_bytes())

    def test_project_source_opens_editable_without_new_organization(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ImportedProjectSourceFixture(Path(temporary))
            graph = fixture.install_graph()
            editor = fixture.editor
            with mock.patch.object(console, "default_active_project_selection_path", return_value=str(fixture.project.selection)), \
                    mock.patch.object(console, "_validate_organization_gate", side_effect=AssertionError("must not require re-review")):
                document = console.load_authorized_source_document(str(editor.registry), editor.AUTHORIZATION_ID, editor.WORKSPACE_ID, capture_binding=editor.capture_binding)
                self.assertEqual(document["text"], fixture.project.source_text)
                snapshot = console.load_console_snapshot(str(editor.registry), capture_binding=editor.capture_binding)
            self.assertEqual(snapshot["errors"], [])
            model = snapshot["workspaces"][0]
            self.assertTrue(model["editorEligible"])
            self.assertTrue(model["autoApplyReady"])
            self.assertEqual(model["flowGraph"]["graphId"], graph["graphId"])
            self.assertEqual(model["flowGraph"]["nodes"][0]["source"]["workspaceId"], editor.WORKSPACE_ID)
            self.assertEqual(model["flowGraph"]["nodes"][0]["source"]["path"], "source/shader.hlsl")
            self.assertEqual(model["flowGraph"]["nodes"][0]["source"]["line"], 3)
            self.assertFalse((editor.workspace / "ai/organize-code-gate.json").exists())

    def test_project_source_rejects_tampered_initial_input_or_project_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ImportedProjectSourceFixture(Path(temporary))
            editor = fixture.editor
            original = fixture.input_path.read_bytes()
            with mock.patch.object(console, "default_active_project_selection_path", return_value=str(fixture.project.selection)):
                fixture.input_path.write_bytes(original + b"// forged")
                with self.assertRaisesRegex(console.ShaderConsoleError, "input bytes"):
                    console.load_authorized_source_document(str(editor.registry), editor.AUTHORIZATION_ID, editor.WORKSPACE_ID, capture_binding=editor.capture_binding)
                fixture.input_path.write_bytes(original)
                for key, value in (("knowledgeId", "f" * 32), ("originalRawSha256", "f" * 64),
                                   ("baselineAbiSha256", "f" * 64), ("sourceSha256", "f" * 64),
                                   ("projectId", "f" * 32)):
                    with self.subTest(key=key):
                        changed = copy.deepcopy(fixture.manifest)
                        receipt = changed["source"]["projectSource"]
                        receipt[key] = value
                        receipt.pop("receiptSha256")
                        receipt["receiptSha256"] = console._canonical_sha256(receipt)
                        editor._write(editor.workspace / "manifest.json", changed)
                        with self.assertRaises(console.ShaderConsoleError):
                            console.load_authorized_source_document(str(editor.registry), editor.AUTHORIZATION_ID, editor.WORKSPACE_ID, capture_binding=editor.capture_binding)

    def test_project_source_same_original_uses_destination_baseline_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ImportedProjectSourceFixture(Path(temporary))
            editor = fixture.editor
            changed = copy.deepcopy(fixture.manifest)
            receipt = changed["source"]["projectSource"]
            changed["baseline"]["abiSha256"] = receipt["baselineAbiSha256"] = "9" * 64
            receipt.pop("receiptSha256")
            receipt["receiptSha256"] = console._canonical_sha256(receipt)
            editor._write(editor.workspace / "manifest.json", changed)
            with mock.patch.object(console, "default_active_project_selection_path", return_value=str(fixture.project.selection)):
                opened = console.load_authorized_source_document(str(editor.registry),
                    editor.AUTHORIZATION_ID, editor.WORKSPACE_ID, capture_binding=editor.capture_binding)
            self.assertEqual(opened["text"], fixture.project.source_text)
            self.assertFalse(opened["readOnly"])

    def test_project_source_user_edits_use_existing_watch_lineage(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ImportedProjectSourceFixture(Path(temporary))
            editor = fixture.editor
            changed = "float4 MainPS() : SV_Target { return 0.5; }\n"
            editor.source.write_bytes(changed.encode("utf-8"))
            with mock.patch.object(console, "default_active_project_selection_path", return_value=str(fixture.project.selection)):
                with self.assertRaisesRegex(console.ShaderConsoleError, "lineage"):
                    console.load_authorized_source_document(str(editor.registry), editor.AUTHORIZATION_ID, editor.WORKSPACE_ID, capture_binding=editor.capture_binding)
                watch_path = editor.workspace / "watch" / (editor.WATCH_ID + ".json")
                watch = json.loads(watch_path.read_bytes())
                # A resumed watch started after an earlier intentional edit.
                watch["initialSourceSha256"] = hashlib.sha256(b"earlier user edit").hexdigest()
                watch["observedSourceSha256"] = hashlib.sha256(changed.encode("utf-8")).hexdigest()
                editor._write(watch_path, watch)
                document = console.load_authorized_source_document(str(editor.registry), editor.AUTHORIZATION_ID, editor.WORKSPACE_ID, capture_binding=editor.capture_binding)
                self.assertEqual(document["text"], changed)
    def test_project_stage_aliases_match_renderdoc_inventory_tokens(self):
        self.assertEqual(console._project_normalized_stage("raygen"), "raygen")
        self.assertEqual(
            console._project_normalized_stage("ray generation"), "raygen"
        )
        self.assertEqual(console._project_normalized_stage("task"), "task")
        self.assertEqual(
            console._project_normalized_stage("amplification"), "task"
        )

    def test_module_remains_python_36_parseable(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        ast.parse(source, filename=str(MODULE_PATH), feature_version=(3, 6))

    def test_windows_file_validation_reuses_module_pointer_types(self):
        class FakeFileApi(object):
            @staticmethod
            def get_information(_handle, _information):
                return 1

            @staticmethod
            def get_final_path(_handle, buffer, _capacity, _flags):
                buffer.value = filename
                return len(filename)

        filename = os.path.abspath("stable-ctypes-file.bin")
        pointer_cache = getattr(ctypes, "_pointer_type_cache", None)
        self.assertIsInstance(pointer_cache, dict)
        before = set(pointer_cache)
        for unused in range(100):
            console._validate_windows_file_handle(
                FakeFileApi(), 1, filename, "test file")
        self.assertEqual(before, set(pointer_cache))

    def test_windows_file_api_configures_kernel32_once(self):
        kernel32 = mock.Mock()
        for name in (
                "CreateFileW", "GetFileSizeEx", "SetFilePointerEx", "ReadFile",
                "CloseHandle", "GetFileInformationByHandle",
                "GetFinalPathNameByHandleW"):
            setattr(kernel32, name, mock.Mock())
        loader = mock.Mock(return_value=kernel32)
        with mock.patch.object(console.os, "name", "nt"), \
                mock.patch.object(console.ctypes, "WinDLL", loader, create=True), \
                mock.patch.object(console, "_WINDOWS_FILE_API", None):
            first = console._windows_file_api()
            second = console._windows_file_api()
        self.assertIs(first, second)
        loader.assert_called_once_with("kernel32", use_last_error=True)
        self.assertIs(
            first.get_information.argtypes[1],
            console._WINDOWS_FILE_INFORMATION_POINTER)

    def test_project_disassembly_identity_matches_writer_for_all_formats(self):
        common = {
            "role": "baseline",
            "sha256": "1" * 64,
            "byteSize": 32,
            "routePlanSha256": "2" * 64,
            "toolId": "writer-tool",
            "toolConfigDigest": "3" * 64,
            "executableSha256": "4" * 64,
            "physicalEvidence": True,
            "editableSource": False,
            "applyAuthorization": False,
        }
        dxil = dict(common, kind="dxil-disassembly", format="dxil-asm")
        native = []
        for artifact_format, input_format in (
                ("dxbc-asm", "dxbc"),
                ("spirv-asm", "spirv"),
                ("opengl-spirv-asm", "opengl-spirv")):
            native.append(dict(
                common,
                kind="native-disassembly",
                format=artifact_format,
                inputFormat=input_format,
                inputRawSha256="5" * 64,
                routeId="writer-route",
                operation="decompile",
                displayEvidenceOnly=True,
            ))
        expected_suffixes = (
            ".dxil-asm", ".dxbc-asm", ".spvasm", ".opengl.spvasm")
        for artifact, suffix in zip([dxil] + native, expected_suffixes):
            with self.subTest(format=artifact["format"]):
                writer_identity = (
                    projects_module._assembly_disassembly_identity(artifact))
                reader_identity = (
                    console._project_assembly_disassembly_identity(artifact))
                self.assertEqual(writer_identity, reader_identity)
                self.assertTrue(
                    console._project_assembly_disassembly_relative(
                        reader_identity).endswith(suffix))

    def test_project_library_reads_content_bound_hlsl_as_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(Path(temporary))
            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertEqual([], snapshot["errors"])
            self.assertTrue(snapshot["available"])
            self.assertEqual(fixture.REPLAY_PROJECT_ID, snapshot["projectId"])
            self.assertEqual(1, len(snapshot["documents"]))
            projected = snapshot["documents"][0]
            self.assertTrue(projected["readOnly"])
            self.assertEqual("project-library", projected["documentKind"])
            self.assertEqual(fixture.knowledge_id, projected["knowledgeId"])
            self.assertEqual(fixture.source_sha256, projected["sourceSha256"])
            self.assertEqual(101, projected["originResourceId"])
            self.assertTrue(projected["originResourceIdPersisted"])
            self.assertFalse(projected["compiledCandidateAvailable"])
            self.assertIsNone(projected["compiledCandidate"])

            opened = console.load_project_shader_source_document(
                str(fixture.selection), fixture.PROJECT_WORKSPACE_ID,
                fixture.REPLAY_PROJECT_ID, fixture.knowledge_id,
                fixture.source_sha256)
            self.assertEqual(fixture.source_text, opened["text"])
            self.assertEqual(fixture.source_sha256, opened["rawSha256"])
            self.assertTrue(opened["readOnly"])

    def test_project_library_rebinds_content_addressed_flow_graph_navigation(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(Path(temporary))
            graph = build_shader_flow_graph(
                fixture.source_text,
                workspace_id=fixture.SOURCE_WORKSPACE_ID,
                source_path="source/shader.hlsl",
                source_sha256=fixture.source_sha256,
                source_revision=7,
                stage="fragment",
                entry_point="MainPS",
                generated_at="2026-08-31T00:00:00Z",
                origin="organized-proposal",
            )
            graph_bytes = json.dumps(
                graph, ensure_ascii=False).encode("utf-8")
            digest = hashlib.sha256(graph_bytes).hexdigest()
            relative = (
                "shared/dictionaries/explanatory/%s.flow-graph.json" %
                digest)
            artifact = fixture.project_root / Path(relative)
            artifact.write_bytes(graph_bytes)
            fixture.shared_index["explanatory"] = [{
                "knowledgeId": fixture.knowledge_id,
                "kind": "shader-flow-graph",
                "artifactPath": relative,
                "artifactSha256": digest,
                "registeredAt": "2026-08-31T00:00:00Z",
                "policy": {
                    "explanatoryOnly": True,
                    "physicalEvidence": False,
                    "applyAuthorization": False,
                    "aiProvisionalAutoApply": False,
                },
            }]
            shared_material = dict(fixture.shared_index)
            shared_material.pop("indexSha256", None)
            fixture.shared_index = fixture._seal(
                shared_material, "indexSha256")
            fixture._write(
                fixture.project_root / "shared" / "dictionaries" / "index.json",
                fixture.shared_index)

            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))

            self.assertEqual([], snapshot["errors"])
            document = snapshot["documents"][0]
            self.assertEqual("current", document["flowGraphStatus"]["status"])
            self.assertEqual(graph["graphId"], document["flowGraph"]["graphId"])
            self.assertEqual(
                {
                    "workspaceId": document["documentId"],
                    "documentId": document["documentId"],
                    "path": document["sourceRelativePath"],
                    "line": 3,
                    "column": 3,
                },
                document["flowGraph"]["nodes"][0]["source"],
            )

            artifact.write_bytes(graph_bytes + b" ")
            rejected = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertTrue(rejected["available"])
            self.assertEqual([], rejected["errors"])
            self.assertEqual(1, len(rejected["documents"]))
            self.assertIsNone(rejected["documents"][0]["flowGraph"])
            self.assertEqual(
                "invalid",
                rejected["documents"][0]["flowGraphStatus"]["status"])
            self.assertRegex(
                rejected["documents"][0]["flowGraphStatus"]["error"],
                "artifact hash mismatch")

    def test_project_library_exposes_only_verified_compiled_candidate_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(
                Path(temporary), compiled_candidate=True)
            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))

            self.assertEqual([], snapshot["errors"])
            projected = snapshot["documents"][0]
            self.assertTrue(projected["readOnly"])
            self.assertTrue(projected["compiledCandidateAvailable"])
            self.assertEqual(
                "replay-reflection-abi-compatible",
                projected["compiledCandidateValidationScope"],
            )
            self.assertEqual(
                fixture.compiled_raw_sha256,
                projected["compiledCandidate"]["rawSha256"],
            )
            self.assertNotIn("artifactPath", projected["compiledCandidate"])

            opened = console.load_project_shader_source_document(
                str(fixture.selection), fixture.PROJECT_WORKSPACE_ID,
                fixture.REPLAY_PROJECT_ID, fixture.knowledge_id,
                fixture.source_sha256)
            self.assertTrue(opened["readOnly"])
            self.assertTrue(opened["compiledCandidateAvailable"])

    def test_project_library_reads_real_writer_compiler_and_disassembly_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = WriterProjectShaderLibraryFixture(Path(temporary))
            catalog, shader, capture_id = fixture.catalog()
            fixture.service.import_capture(catalog)
            status = fixture.workspace_status(
                "writer-capture-workspace", capture_id,
                shader["shaderResourceId"], shader["rawSha256"],
                shader["codeSha256"])
            registered = fixture.service.register_workspace(status)["knowledge"]

            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))

            self.assertEqual([], snapshot["errors"])
            self.assertTrue(snapshot["available"])
            self.assertEqual(1, len(snapshot["documents"]))
            document = snapshot["documents"][0]
            self.assertEqual(
                registered["compilerBinding"], document["compilerBinding"])
            self.assertEqual(
                "dxbc-asm",
                document["assemblyDisassemblyArtifacts"][0]["format"])
            self.assertNotIn(
                "artifactPath",
                document["assemblyDisassemblyArtifacts"][0])
            stored_artifact = (
                fixture.replay_root /
                registered["assemblyDisassemblyArtifacts"][0]["artifactPath"])
            self.assertEqual(
                str(stored_artifact.resolve()),
                document["assemblyDisassemblyArtifactPaths"][0])
            self.assertIn(
                str(stored_artifact.resolve()), snapshot["watchedPaths"])
            self.assertFalse(document["inventoryBacked"])

            manifest_path = fixture.replay_root / "project.json"
            original_manifest = manifest_path.read_text(encoding="utf-8")
            tampered_manifest = json.loads(original_manifest)
            tampered_manifest["knowledge"]["workspaces"][0][
                "compilerBinding"]["hops"][0]["routeId"] += "-tampered"
            manifest_path.write_text(
                json.dumps(tampered_manifest), encoding="utf-8")
            rejected_binding = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertFalse(rejected_binding["available"])
            self.assertTrue(any(
                "compiler binding hash mismatch" in error
                for error in rejected_binding["errors"]))
            manifest_path.write_text(original_manifest, encoding="utf-8")

            tampered_heads = json.loads(original_manifest)
            workspace_head = next(iter(
                tampered_heads["activeHeads"]["workspaces"].values()))
            workspace_head["generation"] = True
            manifest_path.write_text(
                json.dumps(tampered_heads), encoding="utf-8")
            rejected_head = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertFalse(rejected_head["available"])
            self.assertTrue(any(
                "active head generation" in error
                for error in rejected_head["errors"]))
            manifest_path.write_text(original_manifest, encoding="utf-8")

            stored_artifact.write_text("tampered disassembly\n", encoding="utf-8")
            rejected = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertFalse(rejected["available"])
            self.assertTrue(any(
                "disassembly artifact size or hash mismatch" in error
                for error in rejected["errors"]))

    def test_project_library_reads_real_inventory_writer_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = WriterProjectShaderLibraryFixture(Path(temporary))
            baseline = _writer_dxbc(b"inventory writer baseline")
            fingerprint = fingerprint_bytes(baseline)
            self.assertIsNotNone(fingerprint)
            inventory = fixture.inventory(
                909, fingerprint.raw_sha256)
            status = fixture.workspace_status(
                "writer-inventory-workspace",
                inventory["capture"]["captureId"], 909,
                fingerprint.raw_sha256, fingerprint.code_sha256)
            candidate = _writer_dxbc(b"inventory writer candidate")
            fixture.add_validated_candidate(status, candidate)
            binding = fixture.service.reuse_snapshot_for_shader_inventory(
                inventory)["binding"]
            registered = fixture.service.register_workspace_from_inventory(
                status, inventory["shaders"][0], inventory, binding)["knowledge"]

            second_inventory = fixture.inventory(
                707, fingerprint.raw_sha256)
            second_status = fixture.workspace_status(
                "writer-inventory-workspace-second",
                second_inventory["capture"]["captureId"], 707,
                fingerprint.raw_sha256, fingerprint.code_sha256)
            fixture.add_validated_candidate(second_status, candidate)
            second_binding = (
                fixture.service.reuse_snapshot_for_shader_inventory(
                    second_inventory)["binding"])
            repeated = fixture.service.register_workspace_from_inventory(
                second_status, second_inventory["shaders"][0],
                second_inventory, second_binding)
            self.assertTrue(repeated["idempotent"])
            self.assertTrue(repeated["observationRecorded"])
            self.assertEqual(
                registered["knowledgeId"],
                repeated["knowledge"]["knowledgeId"])

            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))

            self.assertEqual([], snapshot["errors"])
            self.assertTrue(snapshot["available"])
            self.assertEqual(1, len(snapshot["documents"]))
            document = snapshot["documents"][0]
            self.assertTrue(document["inventoryBacked"])
            self.assertEqual(
                registered["inventoryOrigin"], document["inventoryOrigin"])
            self.assertEqual(2, document["inventoryObservationCount"])
            self.assertEqual(909, document["originResourceId"])
            self.assertFalse(document["originResourceIdPersisted"])
            self.assertEqual(
                registered["compilerBinding"], document["compilerBinding"])
            self.assertEqual(
                "dxbc-asm",
                document["assemblyDisassemblyArtifacts"][0]["format"])
            self.assertTrue(document["compiledCandidateAvailable"])
            self.assertEqual(
                registered["compiledCandidate"]["rawSha256"],
                document["compiledCandidate"]["rawSha256"])

            manifest_path = fixture.replay_root / "project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["knowledge"]["workspaces"][0][
                "inventoryObservations"][0]["targetResourceId"] += 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            rejected = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertFalse(rejected["available"])
            self.assertTrue(any(
                "inventory observation hash mismatch" in error
                for error in rejected["errors"]))

    def test_project_library_rejects_changed_or_redirected_compiled_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(
                Path(temporary), compiled_candidate=True)
            fixture.compiled_artifact.write_bytes(b"tampered")
            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertFalse(snapshot["available"])
            self.assertTrue(any(
                "artifact size or hash mismatch" in item
                for item in snapshot["errors"]))

        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(
                Path(temporary), compiled_candidate=True)
            original = console._is_reparse

            def redirected(path):
                return (console._same_path(path, fixture.compiled_artifact) or
                        original(path))

            with mock.patch.object(console, "_is_reparse", side_effect=redirected):
                snapshot = console.load_project_shader_library_snapshot(
                    str(fixture.selection))
            self.assertFalse(snapshot["available"])
            self.assertTrue(any("redirected" in item for item in snapshot["errors"]))

    def test_project_library_capture_shader_match_must_be_unique(self):
        for change_metadata in (False, True):
            with self.subTest(change_metadata=change_metadata):
                with tempfile.TemporaryDirectory() as temporary:
                    fixture = ProjectShaderLibraryFixture(Path(temporary))
                    duplicate = dict(
                        fixture.replay_value["captures"][0]["shaders"][0])
                    if change_metadata:
                        duplicate["codeSha256"] = "f" * 64
                    fixture.replay_value["captures"][0]["shaders"].append(
                        duplicate)
                    fixture.write_replay_manifest()

                    snapshot = console.load_project_shader_library_snapshot(
                        str(fixture.selection))
                    self.assertFalse(snapshot["available"])
                    self.assertTrue(any(
                        "does not uniquely select one imported Shader" in item
                        for item in snapshot["errors"]))

    def test_project_library_uses_persisted_resource_id_for_duplicate_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(Path(temporary))
            duplicate = dict(
                fixture.replay_value["captures"][0]["shaders"][0])
            duplicate["resourceId"] = 202
            duplicate["codeSha256"] = "f" * 64
            fixture.replay_value["captures"][0]["shaders"].append(duplicate)
            fixture.write_replay_manifest()

            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))

            self.assertEqual([], snapshot["errors"])
            self.assertTrue(snapshot["available"])
            self.assertEqual(1, len(snapshot["documents"]))
            projected = snapshot["documents"][0]
            self.assertEqual(101, projected["originResourceId"])
            self.assertTrue(projected["originResourceIdPersisted"])

    def test_project_library_origin_resource_id_is_hash_bound(self):
        for mutation in ("remove", "change", "invalid", "noncanonical"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary:
                    fixture = ProjectShaderLibraryFixture(Path(temporary))
                    record = fixture.replay_value["knowledge"]["workspaces"][0]
                    if mutation == "remove":
                        record.pop("originResourceId")
                    elif mutation == "change":
                        record["originResourceId"] = 202
                    elif mutation == "noncanonical":
                        record["originResourceId"] = "101"
                    else:
                        record["originResourceId"] = 0
                    fixture.write_replay_manifest()

                    snapshot = console.load_project_shader_library_snapshot(
                        str(fixture.selection))

                    self.assertFalse(snapshot["available"])
                    self.assertTrue(snapshot["errors"])
                    if mutation in ("invalid", "noncanonical"):
                        self.assertTrue(any(
                            "originResourceId" in item
                            for item in snapshot["errors"]))
                    else:
                        self.assertTrue(any(
                            "knowledgeId" in item
                            for item in snapshot["errors"]))

    def test_project_library_legacy_record_requires_unique_physical_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(
                Path(temporary), persist_origin_resource_id=False)
            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertEqual([], snapshot["errors"])
            self.assertTrue(snapshot["available"])
            projected = snapshot["documents"][0]
            self.assertTrue(projected["readOnly"])
            self.assertEqual(101, projected["originResourceId"])
            self.assertFalse(projected["originResourceIdPersisted"])

            duplicate = dict(
                fixture.replay_value["captures"][0]["shaders"][0])
            duplicate["resourceId"] = 202
            fixture.replay_value["captures"][0]["shaders"].append(duplicate)
            fixture.write_replay_manifest()
            ambiguous = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertFalse(ambiguous["available"])
            self.assertTrue(any(
                "does not uniquely select one imported Shader" in item
                for item in ambiguous["errors"]))

    def test_project_library_rejects_selection_and_workspace_seal_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(Path(temporary))
            fixture.selection_value["selectedAt"] = "tampered"
            fixture._write(fixture.selection, fixture.selection_value)
            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertFalse(snapshot["available"])
            self.assertTrue(any("seal" in item for item in snapshot["errors"]))

        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(Path(temporary))
            fixture.workspace_manifest["name"] = "tampered"
            fixture._write(
                fixture.project_root / ".replaylab" / "workspace.json",
                fixture.workspace_manifest)
            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertFalse(snapshot["available"])
            self.assertTrue(any("seal" in item for item in snapshot["errors"]))

    def test_project_library_rejects_non_content_addressed_and_redirected_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(Path(temporary))
            fixture.replay_value["knowledge"]["workspaces"][0][
                "sourcePath"] = "../outside.hlsl"
            fixture.write_replay_manifest()
            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertFalse(snapshot["available"])
            self.assertTrue(any(
                "project-relative" in item or "content-addressed" in item
                for item in snapshot["errors"]))

        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(Path(temporary))
            original = console._is_reparse

            def redirected(path):
                return (console._same_path(path, fixture.source) or original(path))

            with mock.patch.object(console, "_is_reparse", side_effect=redirected):
                snapshot = console.load_project_shader_library_snapshot(
                    str(fixture.selection))
            self.assertFalse(snapshot["available"])
            self.assertTrue(any("redirected" in item for item in snapshot["errors"]))

    def test_project_library_rejects_changed_hlsl_hash_and_stale_document_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(Path(temporary))
            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertTrue(snapshot["available"])
            fixture.source.write_text(
                "float4 MainPS() : SV_Target { return 0; }", encoding="utf-8")
            changed = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertFalse(changed["available"])
            self.assertTrue(any("hash mismatch" in item for item in changed["errors"]))
            with self.assertRaisesRegex(Exception, "hash mismatch"):
                console.load_project_shader_source_document(
                    str(fixture.selection), fixture.PROJECT_WORKSPACE_ID,
                    fixture.REPLAY_PROJECT_ID, fixture.knowledge_id,
                    fixture.source_sha256)

    def test_project_library_requires_complete_active_knowledge_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ProjectShaderLibraryFixture(Path(temporary))
            fixture.replay_value["activeKnowledge"]["workspaces"] = {}
            fixture.write_replay_manifest()
            snapshot = console.load_project_shader_library_snapshot(
                str(fixture.selection))
            self.assertFalse(snapshot["available"])
            self.assertTrue(any("activeKnowledge" in item for item in snapshot["errors"]))

    def test_parses_dxc_colon_and_d3d_parenthesised_locations(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            job_input = fixture.workspace / "jobs" / "compile" / "input.hlsl"
            message = (
                f"{job_input}:12:7: error: use of undeclared identifier 'bad'\n"
                f"{fixture.source}(22,3): warning X3550: array reference cannot be unrolled"
            )
            values = console.parse_compiler_diagnostics(
                message, str(fixture.source), str(fixture.workspace)
            )
            self.assertEqual(len(values), 2)
            self.assertEqual(values[0]["severity"], "error")
            self.assertEqual((values[0]["line"], values[0]["column"]), (12, 7))
            self.assertEqual(Path(values[0]["sourceFile"]), fixture.source.resolve())
            self.assertEqual(values[1]["severity"], "warning")
            self.assertEqual((values[1]["line"], values[1]["column"]), (22, 3))

    def test_compiler_text_cannot_redirect_source_opening(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            outside = Path(temporary) / "outside.hlsl"
            outside.write_text("bad", encoding="utf-8")
            values = console.parse_compiler_diagnostics(
                f"{outside}:9:2: error: malicious diagnostic path",
                str(fixture.source), str(fixture.workspace),
            )
            self.assertEqual(values[0]["sourceFile"], "")
            self.assertEqual(values[0]["line"], 9)
            self.assertEqual(Path(values[0]["reportedFile"]), outside)

    def test_compile_failure_becomes_red_locatable_console_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            watch = {
                "events": [
                    {
                        "sequence": 1,
                        "at": "2026-08-25T04:00:00Z",
                        "type": "compileFailed",
                        "errorType": "ToolExecutionError",
                        "message": "input.hlsl:3:10: error: expected expression",
                    }
                ]
            }
            entries = console.console_entries_from_audits(
                str(fixture.workspace), str(fixture.source), watch=watch
            )
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["severity"], "error")
            self.assertEqual(entries[0]["phase"], "编译")
            self.assertEqual(entries[0]["line"], 3)
            self.assertEqual(entries[0]["column"], 10)
            self.assertEqual(Path(entries[0]["sourceFile"]), fixture.source.resolve())

    def test_structured_watcher_diagnostic_precedes_message_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            watch = {
                "events": [
                    {
                        "sequence": 1,
                        "at": "2026-08-25T04:00:00Z",
                        "type": "compileFailed",
                        "message": "generic compiler failure without a location",
                        "diagnostics": [
                            {
                                "file": str(fixture.source),
                                "line": 4,
                                "column": 2,
                                "severity": "error",
                                "message": "expected '}'",
                            }
                        ],
                    }
                ]
            }
            # This helper normally receives already-sanitised events from the
            # loader; directly supply the equivalent structured projection.
            entries = console.console_entries_from_audits(
                str(fixture.workspace), str(fixture.source), watch=watch
            )
            self.assertEqual((entries[0]["line"], entries[0]["column"]), (4, 2))
            self.assertEqual(entries[0]["text"], "expected '}'")

    def test_actual_watcher_stop_requested_event_is_visible(self):
        entries = console.console_entries_from_audits(
            "C:/workspace", "C:/workspace/source/shader.hlsl",
            watch={
                "watchId": "d" * 32,
                "events": [{
                    "sequence": 7,
                    "at": "2026-08-25T04:00:00Z",
                    "type": "stopRequested",
                }],
            },
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["eventType"], "stopRequested")
        self.assertEqual(entries[0]["severity"], "warning")
        self.assertIn("正在停止", entries[0]["text"])

    def test_diagnostic_ids_are_stable_unique_and_do_not_borrow_sequences(self):
        watch = {
            "watchId": "d" * 32,
            "events": [
                {
                    "sequence": 1,
                    "at": "2026-08-25T04:00:00Z",
                    "type": "compileFailed",
                    "message": "compile failed",
                    "diagnostics": [
                        {
                            "file": "input.hlsl",
                            "line": 3,
                            "column": 2,
                            "severity": "error",
                            "message": "expected expression",
                        },
                        {
                            "file": "input.hlsl",
                            "line": 3,
                            "column": 2,
                            "severity": "error",
                            "message": "expected expression",
                        },
                    ],
                },
                {
                    "sequence": 100,
                    "at": "2026-08-25T04:00:01Z",
                    "type": "watchStopped",
                },
            ],
        }
        first = console.console_entries_from_audits(
            "C:/workspace", "C:/workspace/source/shader.hlsl", watch=watch)
        second = console.console_entries_from_audits(
            "C:/workspace", "C:/workspace/source/shader.hlsl", watch=watch)
        self.assertEqual([item["id"] for item in first],
                         [item["id"] for item in second])
        self.assertEqual(len({item["id"] for item in first}), 3)
        diagnostics = [item for item in first if item["eventType"] == "compileFailed"]
        self.assertEqual([item["sequence"] for item in diagnostics], [1, 1])
        self.assertEqual([item["cursorSequence"] for item in diagnostics], [1, 1])
        self.assertTrue(all(":diagnostic:" in item["id"] for item in diagnostics))
        stopped = next(item for item in first if item["eventType"] == "watchStopped")
        self.assertTrue(stopped["id"].endswith(":100"))

    def test_progress_distinguishes_detection_compile_apply_and_success(self):
        self.assertEqual(
            console.derive_console_status({"pending": True}, None)["phase"], "检测"
        )
        self.assertEqual(
            console.derive_console_status({"compileInProgress": True}, None)["phase"],
            "编译",
        )
        applying = {
            "state": "running",
            "events": [
                {"sequence": 1, "at": "2026-08-25T04:00:00Z", "type": "applyStarted"}
            ],
        }
        self.assertEqual(
            console.derive_console_status({}, applying)["phase"], "ABI / 应用"
        )
        succeeded = dict(applying)
        succeeded["events"] = [
            {"sequence": 2, "at": "2026-08-25T04:00:01Z", "type": "applySucceeded"}
        ]
        succeeded["activeRevision"] = 4
        status = console.derive_console_status({}, succeeded)
        self.assertEqual(status["phase"], "完成")
        self.assertIn("revision 4", status["text"])

    def test_apply_reused_status_uses_processed_revision_not_replacement_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            fixture.write_watch(latestCandidateRevision=11)
            fixture.write_live(
                activeReplacement={"revision": 8},
                latestHandledRevision=11,
                applyReuses=1,
                events=[
                    {
                        "sequence": 1,
                        "at": "2026-08-25T04:00:00Z",
                        "type": "applyReused",
                        "revision": 11,
                    }
                ],
            )
            snapshot = console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual(snapshot["errors"], [])
            workspace = snapshot["workspaces"][0]
            self.assertEqual(workspace["liveEdit"]["activeRevision"], 8)
            self.assertEqual(workspace["liveEdit"]["latestHandledRevision"], 11)
            status = workspace["status"]
            self.assertEqual(status["phase"], "完成")
            self.assertIn("revision 11", status["text"])
            self.assertNotIn("revision 8", status["text"])

    def test_superseded_candidate_apply_failures_are_warnings_only(self):
        messages = (
            "Shader source changed after the candidate compile; wait for a fresh compile before validation/apply",
            "Shader workspace no longer contains the expected saved candidate",
        )
        for index, message in enumerate(messages, 1):
            with self.subTest(message=message):
                event = {
                    "sequence": index,
                    "at": "2026-08-25T04:00:0%dZ" % index,
                    "type": "applyFailed",
                    "revision": 10 + index,
                    "message": message,
                }
                entries = console.console_entries_from_audits(
                    "C:/workspace", "C:/workspace/source/shader.hlsl",
                    live={"events": [event]},
                )
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["severity"], "warning")
                self.assertEqual(entries[0]["phase"], "应用队列")
                self.assertIn("新的保存取代", entries[0]["text"])

                status = console.derive_console_status(
                    {}, {"state": "running", "events": [event]}
                )
                self.assertEqual(status["severity"], "warning")
                self.assertEqual(status["phase"], "应用队列")

    def test_unrelated_apply_failure_remains_a_red_error(self):
        event = {
            "sequence": 1,
            "at": "2026-08-25T04:00:00Z",
            "type": "applyFailed",
            "revision": 12,
            "message": "D3D12 rejected replacement target creation",
        }
        entries = console.console_entries_from_audits(
            "C:/workspace", "C:/workspace/source/shader.hlsl",
            live={"events": [event]},
        )
        self.assertEqual(entries[0]["severity"], "error")
        self.assertEqual(entries[0]["phase"], "ABI / 应用")

        status = console.derive_console_status(
            {}, {"state": "running", "events": [event]}
        )
        self.assertEqual(status["severity"], "error")
        self.assertEqual(status["phase"], "错误")

    def test_runtime_failure_without_compiler_location_is_not_fake_line_one(self):
        event = {
            "sequence": 1,
            "at": "2026-08-25T04:00:00Z",
            "type": "liveEditFailed",
            "message": "could not persist live-edit audit state: access denied",
        }
        entries = console.console_entries_from_audits(
            "C:/workspace", "C:/workspace/source/shader.hlsl",
            live={"events": [event]},
        )
        self.assertEqual(entries[0]["severity"], "error")
        self.assertEqual(entries[0]["sourceFile"], "")
        self.assertEqual(entries[0]["line"], 0)
        self.assertEqual(entries[0]["column"], 0)

    def test_persistent_authorization_revoke_failure_is_visible_without_source(self):
        event = {
            "sequence": 9,
            "at": "2026-08-25T04:00:00Z",
            "type": "persistentAuthorizationRevokeFailed",
            "errorType": "PermissionError",
            "message": "authorization registry remained durable",
        }
        entries = console.console_entries_from_audits(
            "C:/workspace", "C:/workspace/source/shader.hlsl",
            live={"events": [event]},
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["severity"], "error")
        self.assertEqual(entries[0]["phase"], "授权")
        self.assertIn("registry remained durable", entries[0]["text"])
        self.assertEqual(entries[0]["sourceFile"], "")
        self.assertEqual(entries[0]["line"], 0)

    def test_three_unity_severities_counts_filters_and_clear_watermark(self):
        entries = [
            {"streamKey": "a", "cursorSequence": 1, "severity": "progress"},
            {"streamKey": "a", "cursorSequence": 2, "severity": "warning"},
            {"streamKey": "b", "cursorSequence": 1, "severity": "error"},
            {"streamKey": "b", "cursorSequence": 2, "severity": "success"},
        ]
        self.assertEqual(
            console.console_severity_counts(entries),
            {"info": 2, "warning": 1, "error": 1},
        )
        filtered = console.filter_console_entries(
            entries, {"info": False, "warning": True, "error": True}
        )
        self.assertEqual(len(filtered), 2)
        cursors = console.build_console_clear_cursors(entries)
        self.assertEqual(console.console_entries_after_clear(entries, cursors), [])
        next_event = {
            "streamKey": "a", "cursorSequence": 3, "severity": "info"
        }
        self.assertEqual(
            console.console_entries_after_clear(entries + [next_event], cursors),
            [next_event],
        )

    def test_fixed_generic_journal_publishes_and_loads_without_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
                ordinary = console.publish_console_event(
                    "project", "progress", "正在准备工程", phase="工程",
                    code="projectPreparing",
                    context={
                        "operation": "prepare",
                        "projectId": "project-1",
                        "jobId": "a" * 32,
                        "failureCode": "replay-callback-failed",
                        "diagnostics": "d" * 2048,
                        "sourcePath": "C:/must/not/be/trusted.hlsl",
                        "command": "notepad.exe",
                    },
                    process_role="host",
                )
                console.publish_console_event(
                    "capture", "warning", "捕获身份需要重新确认",
                    phase="捕获", process_role="host",
                )
                console.publish_console_event(
                    "material", "error", "材质更新失败",
                    phase="材质", process_role="host",
                )
                directory = console.default_console_journal_directory()
                loaded = console._load_generic_console_entries()

            self.assertEqual(ordinary["severity"], "info")
            self.assertEqual(ordinary["context"]["jobId"], "a" * 32)
            self.assertEqual(
                ordinary["context"]["failureCode"], "replay-callback-failed")
            self.assertEqual(len(ordinary["context"]["diagnostics"]), 1024)
            self.assertNotIn("sourcePath", ordinary["context"])
            self.assertNotIn("command", ordinary["context"])
            self.assertEqual(loaded["errors"], [])
            self.assertEqual(
                console.console_severity_counts(loaded["entries"]),
                {"info": 1, "warning": 1, "error": 1},
            )
            self.assertEqual(
                [entry["module"] for entry in loaded["entries"]],
                ["project", "capture", "material"],
            )
            self.assertTrue(all(not entry["sourceFile"] for entry in loaded["entries"]))
            files = list(Path(directory).glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            before = files[0].read_bytes()
            cursors = console.build_console_clear_cursors(loaded["entries"])
            self.assertEqual(
                console.console_entries_after_clear(loaded["entries"], cursors), []
            )
            self.assertEqual(files[0].read_bytes(), before)

    @staticmethod
    def _write_console_journal(directory, session, pid, events, mtime, segment=0):
        path = directory / (
            "events.host.%d.%s.%04d.jsonl" % (pid, session, segment))
        rows = []
        for sequence, unix_ms, message in events:
            rows.append(json.dumps({
                "schema": console.JOURNAL_EVENT_SCHEMA,
                "schemaVersion": 1,
                "sessionId": session,
                "sequence": sequence,
                "unixMs": unix_ms,
                "at": "2026-08-25T04:00:%02dZ" % (sequence % 60),
                "processRole": "host",
                "processId": pid,
                "module": "host",
                "severity": "info",
                "phase": "测试",
                "code": "boundedRead",
                "message": message,
                "context": {},
            }, ensure_ascii=False, separators=(",", ":")))
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        os.utime(str(path), (mtime, mtime))
        return path

    def test_journal_reader_prefers_latest_files_and_has_global_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
                directory = Path(console.default_console_journal_directory())
                directory.mkdir(parents=True)
                for index in range(6):
                    self._write_console_journal(
                        directory, ("%032x" % (index + 1)), 100 + index,
                        [(1, 1000 + index, "event-%d" % index)],
                        1000 + index,
                    )
                with mock.patch.object(console, "MAX_JOURNAL_READ_FILES", 3), \
                        mock.patch.object(console, "MAX_JOURNAL_READ_BYTES", 4096), \
                        mock.patch.object(
                            console, "MAX_JOURNAL_READ_BYTES_PER_FILE", 4096):
                    loaded = console._load_generic_console_entries()
            self.assertEqual(loaded["errors"], [])
            self.assertEqual(
                {item["text"] for item in loaded["entries"]},
                {"event-3", "event-4", "event-5"},
            )
            self.assertLessEqual(len(loaded["watchedPaths"]), 4)

    def test_journal_reader_reads_complete_latest_tail_records_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
                directory = Path(console.default_console_journal_directory())
                directory.mkdir(parents=True)
                events = [
                    (index, 1000 + index, "event-%02d-%s" % (index, "x" * 80))
                    for index in range(1, 31)
                ]
                self._write_console_journal(
                    directory, "1" * 32, 101, events, 2000)
                with mock.patch.object(console, "MAX_JOURNAL_READ_BYTES", 1200), \
                        mock.patch.object(
                            console, "MAX_JOURNAL_READ_BYTES_PER_FILE", 1200):
                    loaded = console._load_generic_console_entries()
            self.assertEqual(loaded["errors"], [])
            self.assertGreater(len(loaded["entries"]), 0)
            self.assertLess(len(loaded["entries"]), len(events))
            self.assertEqual(loaded["entries"][-1]["sequence"], 30)
            self.assertTrue(all(item["text"].startswith("event-")
                                for item in loaded["entries"]))

    def test_journal_retention_only_removes_strict_old_generic_segments(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
                directory = Path(console.default_console_journal_directory())
                directory.mkdir(parents=True)
                paths = []
                for index in range(4):
                    paths.append(self._write_console_journal(
                        directory, ("%032x" % (index + 1)), 200 + index,
                        [(1, 2000 + index, "event-%d" % index)], 2000 + index))
                invalid = directory / "do-not-delete.txt"
                invalid.write_text("not a ReplayLab segment", encoding="utf-8")
                with mock.patch.object(console, "MAX_JOURNAL_RETENTION_FILES", 2), \
                        mock.patch.object(
                            console, "MAX_JOURNAL_RETENTION_BYTES", 1024 * 1024):
                    removed = console._enforce_console_journal_retention(
                        str(directory), protected_path=str(paths[-1]))
                remaining = list(directory.glob("events.*.jsonl"))
                self.assertEqual(len(remaining), 2)
                self.assertTrue(paths[-1].exists())
                self.assertTrue(invalid.exists())
                self.assertEqual(removed, 2)

                outside = Path(temporary) / "outside"
                with self.assertRaises(console.ShaderConsoleError):
                    console._enforce_console_journal_retention(str(outside))

    def test_journal_reader_periodically_enforces_retention_for_host_only_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
                directory = Path(console.default_console_journal_directory())
                directory.mkdir(parents=True)
                for index in range(4):
                    self._write_console_journal(
                        directory, ("%032x" % (index + 20)), 300 + index,
                        [(1, 3000 + index, "event-%d" % index)], 3000 + index)
                with mock.patch.object(console, "MAX_JOURNAL_RETENTION_FILES", 2), \
                        mock.patch.object(
                            console, "MAX_JOURNAL_RETENTION_BYTES", 1024 * 1024), \
                        mock.patch.object(
                            console, "_JOURNAL_LAST_RETENTION_MONOTONIC", 0.0):
                    loaded = console._load_generic_console_entries()
                self.assertEqual(loaded["errors"], [])
                self.assertEqual(len(list(directory.glob("events.*.jsonl"))), 2)

    def test_snapshot_aggregates_generic_and_authorized_shader_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = ShaderConsoleFixture(root)
            fixture.write_watch(events=[{
                "sequence": 1,
                "at": "2026-08-25T04:00:00Z",
                "type": "watchStarted",
            }])
            fixture.write_live(events=[])
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": temporary}):
                console.publish_console_event(
                    "pass", "info", "Pass 树已生成", phase="Pass",
                    process_role="qrenderdoc",
                )
                snapshot = console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual(snapshot["errors"], [])
            self.assertEqual(len(snapshot["entries"]), 2)
            self.assertEqual(
                {entry["module"] for entry in snapshot["entries"]},
                {"pass", "shader"},
            )

    def test_loader_uses_authorized_workspace_and_exact_audit_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            fixture.write_watch(
                events=[
                    {"sequence": 1, "at": "2026-08-25T04:00:00Z", "type": "watchStarted"},
                    {"sequence": 2, "at": "2026-08-25T04:00:01Z", "type": "compileStarted", "attempt": 1},
                    {"sequence": 3, "at": "2026-08-25T04:00:02Z", "type": "compileSucceeded", "revision": 2},
                ]
            )
            fixture.write_live(
                events=[
                    {"sequence": 1, "at": "2026-08-25T04:00:00Z", "type": "liveEditStarted"},
                    {"sequence": 2, "at": "2026-08-25T04:00:03Z", "type": "applyStarted", "revision": 2},
                    {"sequence": 3, "at": "2026-08-25T04:00:04Z", "type": "applySucceeded", "revision": 2},
                ]
            )
            snapshot = console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual(snapshot["errors"], [])
            self.assertEqual(len(snapshot["workspaces"]), 1)
            workspace = snapshot["workspaces"][0]
            self.assertEqual(workspace["watch"]["watchId"], fixture.WATCH_ID)
            self.assertEqual(workspace["liveEdit"]["liveEditId"], fixture.LIVE_ID)
            self.assertEqual(workspace["status"]["phase"], "完成")
            self.assertEqual(
                [item["eventType"] for item in workspace["entries"]],
                [
                    "watchStarted", "liveEditStarted", "compileStarted",
                    "compileSucceeded", "applyStarted", "applySucceeded",
                ],
            )

    def test_legacy_algorithm_graph_survives_bridge_document_and_console_loading(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            graph = _console_legacy_algorithm_graph(fixture)
            self.assertEqual("algorithm-dataflow", graph["graphKind"])
            self.assertEqual(12, len(graph["nodes"]))
            self.assertEqual({"data", "control"}, {edge["kind"] for edge in graph["edges"]})
            self.assertEqual(graph, _validate_console_legacy_algorithm_graph(fixture, graph))

            document = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID,
                capture_binding=fixture.capture_binding,
            )
            self.assertEqual(fixture.source.read_text(encoding="utf-8"), document["text"])
            self.assertEqual(graph["sourceSha256"], document["rawSha256"])
            snapshot = console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual([], snapshot["errors"])
            self.assertEqual(1, len(snapshot["workspaces"]))
            workspace = snapshot["workspaces"][0]
            self.assertEqual("current", workspace["flowGraphStatus"]["status"])
            projected = workspace["flowGraph"]
            self.assertEqual(graph["graphId"], projected["graphId"])
            self.assertEqual("TD", projected["layoutDirection"])
            self.assertEqual(graph["nodes"], projected["nodes"])
            self.assertEqual(graph["edges"], projected["edges"])
            self.assertEqual("读取像素坐标", projected["nodes"][0]["label"])
            self.assertEqual("输出像素颜色", projected["nodes"][-1]["label"])
            self.assertFalse(projected["policy"]["compileAuthorization"])
            self.assertFalse(projected["policy"]["applyAuthorization"])

    def test_bridge_rejects_malformed_algorithm_graph_without_hiding_editable_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            original = _console_legacy_algorithm_graph(fixture)
            source_before = fixture.source.read_bytes()
            data_index = next(index for index, edge in enumerate(original["edges"]) if edge["kind"] == "data")
            control_index = next(index for index, edge in enumerate(original["edges"]) if edge["kind"] == "control")
            cases = [
                ("unknown-top-field", ("unexpected",), True),
                ("unknown-node-field", ("nodes", 0, "unexpected"), True),
                ("unknown-edge-field", ("edges", data_index, "unexpected"), True),
                ("wrong-policy", ("policy", "applyAuthorization"), True),
                ("wrong-derived-edge-policy", ("policy", "callEdgesDerivedFromFinalSource"), True),
                ("wrong-graph-kind", ("graphKind",), "callgraph"),
                ("wrong-layout", ("layoutDirection",), "LR"),
                ("wrong-node-kind", ("nodes", 0, "kind"), "function"),
                ("unproven-node", ("nodes", 0, "inferred"), False),
                ("extra-range-field", ("nodes", 0, "sourceRange", "column"), 1),
                ("range-bool", ("nodes", 0, "sourceRange", "startLine"), True),
                ("range-zero", ("nodes", 0, "sourceRange", "startLine"), 0),
                ("range-reversed", ("nodes", 0, "sourceRange", "endLine"), 2),
                ("range-misses-navigation", ("nodes", 0, "sourceRange", "startLine"), 4),
                ("range-over-ui-bound", ("nodes", 0, "sourceRange", "endLine"), 10000001),
                ("line-over-ui-bound", ("nodes", 0, "source", "line"), 10000001),
                ("column-over-ui-bound", ("nodes", 0, "source", "column"), 10000001),
                ("label-control-character", ("nodes", 0, "label"), "读取\n坐标"),
                ("description-control-character", ("nodes", 0, "description"), "读取\t坐标"),
                ("reads-not-array", ("nodes", 0, "reads"), "uv"),
                ("too-many-writes", ("nodes", 0, "writes"), ["color"] * 33),
                ("read-value-too-long", ("nodes", 0, "reads"), ["x" * 73]),
                ("read-value-control-character", ("nodes", 0, "reads"), ["uv\x00"]),
                ("data-values-not-array", ("edges", data_index, "values"), "color"),
                ("data-values-empty", ("edges", data_index, "values"), []),
                ("data-values-too-many", ("edges", data_index, "values"), ["color"] * 33),
                ("data-value-not-text", ("edges", data_index, "values"), [1]),
                ("data-value-too-long", ("edges", data_index, "values"), ["x" * 73]),
                ("data-value-control-character", ("edges", data_index, "values"), ["color\x00"]),
                ("control-edge-with-values", ("edges", control_index, "values"), ["color"]),
                ("unknown-edge-target", ("edges", data_index, "target"), "stage-missing"),
                ("source-path-escape", ("sourcePath",), "../outside.hlsl"),
                ("node-path-escape", ("nodes", 0, "source", "path"), "../outside.hlsl"),
                ("node-absolute-path", ("nodes", 0, "source", "path"), "C:/outside.hlsl"),
                ("node-different-file", ("nodes", 0, "source", "path"), "source/other.hlsl"),
                ("source-sha-mismatch", ("sourceSha256",), "0" * 64),
                ("source-revision-mismatch", ("sourceRevision",), 8),
                ("entry-point-mismatch", ("entryPoint",), "OtherPS"),
                ("stage-mismatch", ("stage",), "vertex"),
            ]
            for label, path, value in cases:
                with self.subTest(case=label):
                    malformed = copy.deepcopy(original)
                    target = malformed
                    for part in path[:-1]:
                        target = target[part]
                    target[path[-1]] = value
                    material = dict(malformed)
                    material.pop("graphId")
                    material.pop("generatedAt")
                    malformed["graphId"] = console._canonical_sha256(material)[:32]
                    with self.assertRaises(console.ShaderConsoleError):
                        _validate_console_legacy_algorithm_graph(fixture, malformed)
                    # Bind the current artifact bytes in the fixture gate so
                    # loading must reject the graph itself, not a stale file hash.
                    _publish_console_legacy_algorithm_graph(fixture, malformed)
                    snapshot = console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
                    self.assertEqual(1, len(snapshot["workspaces"]))
                    workspace = snapshot["workspaces"][0]
                    self.assertIsNone(workspace["flowGraph"])
                    self.assertEqual("invalid", workspace["flowGraphStatus"]["status"])
                    document = console.load_authorized_source_document(
                        str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID,
                        capture_binding=fixture.capture_binding,
                    )
                    self.assertEqual(source_before.decode("utf-8"), document["text"])
            self.assertEqual(source_before, fixture.source.read_bytes())

    def test_bridge_rejects_invalid_graph_identity_but_allows_unsealed_json_whitespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            original = _console_legacy_algorithm_graph(fixture)
            malformed = copy.deepcopy(original)
            malformed["graphId"] = "0" * 32
            with self.assertRaisesRegex(console.ShaderConsoleError, "identity"):
                _validate_console_legacy_algorithm_graph(fixture, malformed)
            _publish_console_legacy_algorithm_graph(fixture, malformed)
            rejected = console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual("invalid", rejected["workspaces"][0]["flowGraphStatus"]["status"])

            _publish_console_legacy_algorithm_graph(fixture, original)
            graph_path = fixture.workspace / "ai" / "flow-graph.json"
            graph_path.write_bytes(graph_path.read_bytes() + b"\n")
            # A graph is optional navigation metadata, independent from the
            # finalized source receipt. JSON whitespace changes no identity.
            self.assertEqual(original, _validate_console_legacy_algorithm_graph(fixture, original))
            loaded = console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual("current", loaded["workspaces"][0]["flowGraphStatus"]["status"])

    def test_loader_displays_saved_draft_graph_with_existing_compile_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            source_bytes = fixture.source.read_bytes()
            graph = build_shader_flow_graph(
                source_bytes.decode("utf-8"),
                workspace_id=fixture.WORKSPACE_ID,
                source_path="source/shader.hlsl",
                source_sha256=hashlib.sha256(source_bytes).hexdigest(),
                source_revision=7, stage="fragment", entry_point="MainPS",
                generated_at="2026-08-31T00:00:00Z", origin="editable-draft",
            )
            fixture._write(fixture.workspace / "ai" / "flow-graph.json", graph)
            snapshot = console.load_console_snapshot(
                str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual(snapshot["errors"], [])
            workspace = snapshot["workspaces"][0]
            self.assertEqual(workspace["flowGraph"]["graphId"], graph["graphId"])
            self.assertEqual(workspace["flowGraphStatus"]["status"], "current")
            self.assertTrue(workspace["flowGraph"]["policy"]["navigationOnly"])
            self.assertFalse(workspace["flowGraph"]["policy"]["applyAuthorization"])
            self.assertEqual(fixture.source.read_bytes(), source_bytes)

    def test_loader_projects_only_a_source_bound_shader_flow_graph(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            source_bytes = fixture.source.read_bytes()
            source_text = source_bytes.decode("utf-8", errors="strict")
            source_sha256 = hashlib.sha256(source_bytes).hexdigest()
            graph = build_shader_flow_graph(
                source_text,
                workspace_id=fixture.WORKSPACE_ID,
                source_path="source/shader.hlsl",
                source_sha256=source_sha256,
                source_revision=7,
                stage="fragment",
                entry_point="MainPS",
                generated_at="2026-08-31T00:00:00Z",
                origin="organized-proposal",
                descriptions={"MainPS": "Builds the final pixel color."},
            )
            graph_path = fixture.workspace / "ai" / "flow-graph.json"
            fixture._write(graph_path, graph)

            snapshot = console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual(snapshot["errors"], [])
            workspace = snapshot["workspaces"][0]
            self.assertEqual(workspace["flowGraph"]["graphId"], graph["graphId"])
            self.assertEqual(workspace["flowGraphStatus"]["status"], "current")
            self.assertEqual(
                workspace["flowGraph"]["nodes"][0]["source"],
                {
                    "workspaceId": fixture.WORKSPACE_ID,
                    "documentId": fixture.WORKSPACE_ID,
                    "path": "source/shader.hlsl",
                    "line": 3,
                    "column": 3,
                },
            )

            graph["nodes"][0]["source"]["path"] = "../outside.hlsl"
            fixture._write(graph_path, graph)
            rejected = console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual(len(rejected["workspaces"]), 1)
            self.assertIsNone(rejected["workspaces"][0]["flowGraph"])
            self.assertEqual(
                rejected["workspaces"][0]["flowGraphStatus"]["status"],
                "invalid",
            )

    def test_loader_recovers_line_diagnostics_from_manifest_bound_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            source_sha256 = "1" * 64
            job = fixture.workspace / "jobs" / "compile-h01-r0001-test"
            job.mkdir(parents=True)
            stderr = job / "stderr.log"
            stderr.write_text(
                f"{job / 'input.hlsl'}:3:8: error: expected ';' after expression\n",
                encoding="utf-8",
            )
            manifest_path = fixture.workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["history"] = [
                {
                    "operation": "compile-route-chain",
                    "status": "failed",
                    "hops": [
                        {
                            "operation": "compile",
                            "status": "failed",
                            "inputSha256": source_sha256,
                            "stderrPath": str(stderr.relative_to(fixture.workspace)).replace("\\", "/"),
                        }
                    ],
                }
            ]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            fixture.write_watch(
                events=[
                    {
                        "sequence": 1,
                        "at": "2026-08-25T04:00:00Z",
                        "type": "compileFailed",
                        "errorType": "ToolExecutionError",
                        "message": "external tool exited with code 5; see authorized job",
                        "requestedSourceSha256": source_sha256,
                    }
                ],
                compileFailures=1,
                compileSuccesses=0,
                caughtUp=False,
            )
            fixture.write_live(events=[])
            snapshot = console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual(snapshot["errors"], [])
            error = snapshot["workspaces"][0]["entries"][0]
            self.assertEqual(error["severity"], "error")
            self.assertEqual((error["line"], error["column"]), (3, 8))
            self.assertIn("expected ';'", error["text"])
            self.assertEqual(Path(error["sourceFile"]), fixture.source.resolve())

    def test_loader_fails_closed_when_authorized_identity_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            manifest_path = fixture.workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["shader"]["resourceId"] = 999
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            snapshot = console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual(snapshot["workspaces"], [])
            self.assertTrue(any("identity" in error for error in snapshot["errors"]))

    def test_authorized_hlsl_document_loads_only_the_organized_lane(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            opened = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID,
                capture_binding=fixture.capture_binding,
            )
            self.assertEqual(Path(opened["sourcePath"]), fixture.source.resolve())
            self.assertEqual(opened["entryPoint"], "MainPS")
            self.assertIn("return 1", opened["text"])

    def test_host_equivalence_levels_open_and_keep_authorized_edits_readable(self):
        baseline = "float4 MainPS() : SV_Target\n{\n  float oldValue = 1;\n  return oldValue;\n}\n"
        for level in ("exact-code", "rename-only", "observational-pass"):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as temporary:
                fixture = ShaderConsoleFixture(Path(temporary))
                proposal = baseline.replace("oldValue", "reviewedValue")
                if level == "observational-pass":
                    proposal = proposal.replace("= 1", "= 0.5")
                gate = fixture.write_organization_gate(
                    level, baseline_source=baseline, proposal_source=proposal,
                    rename_map={"oldValue": "reviewedValue"} if level == "rename-only" else {},
                )
                expected = proposal
                if level != "exact-code":
                    self.assertNotEqual(gate["baselineCodeSha256"], gate["organizedCodeSha256"])
                    self.assertFalse(gate["codeSha256Equal"])
                opened = console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID,
                    capture_binding=fixture.capture_binding,
                )
                self.assertEqual(expected, opened["text"])
                self.assertEqual(hashlib.sha256(expected.encode("utf-8")).hexdigest(), opened["rawSha256"])
                self.assertEqual(1, len(console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)["workspaces"]))
                fixed_evidence = {
                    path: path.read_bytes() for path in (
                        fixture.baseline_artifact, fixture.proposal_artifact,
                        fixture.session_dir / "session.json", fixture.session_dir / "gate.json",
                    )
                }
                # The retired embedded save API remains closed. A successful
                # Host save is represented below by current source plus the
                # exact authorized running watch/live-edit lineage.
                edited = expected.replace("return ", "return 0.75 * ", 1) + "// saved through Host lineage\n"
                with self.assertRaisesRegex(console.ShaderConsoleError, "running Bread Host"):
                    console.save_authorized_source_document(
                        str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID,
                        opened["rawSha256"], edited,
                    )
                fixture.source.write_text(edited, encoding="utf-8", newline="\n")
                fixture.write_watch()
                fixture.write_live()
                reopened = console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID,
                    capture_binding=fixture.capture_binding,
                )
                self.assertEqual(edited, reopened["text"])
                self.assertNotEqual(gate["finalSourceSha256"], reopened["rawSha256"])
                self.assertEqual(fixed_evidence, {path: path.read_bytes() for path in fixed_evidence})

    def test_scoped_local_rename_receipt_does_not_hide_editable_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            baseline = (
                "float4 MainPS() : SV_Target { float _1 = 1; return _1.xxxx; }\n"
            )
            proposal = baseline.replace("_1", "normalLightDot")
            fixture.write_organization_gate(
                "rename-only", baseline_source=baseline, proposal_source=proposal,
                rename_map={"MainPS::_1": "normalLightDot"},
            )
            opened = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID,
                fixture.WORKSPACE_ID, capture_binding=fixture.capture_binding,
            )
            self.assertEqual(proposal, opened["text"])

    def test_organization_equivalence_gate_rejects_contradictory_levels_and_observations(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            baseline = "float4 MainPS() : SV_Target { return 1; }\n"
            proposal = "float4 MainPS() : SV_Target { return 0.5; }\n"
            original = fixture.write_organization_gate(
                "observational-pass", baseline_source=baseline, proposal_source=proposal
            )
            gate_path = fixture.workspace / "ai" / "organize-code-gate.json"
            cases = [
                ("unknown-level", ("equivalenceLevel",), "universal-proof"),
                ("exact-with-different-code", ("equivalenceLevel",), "exact-code"),
                ("contradictory-code-equal", ("codeSha256Equal",), True),
                ("missing-verdict", ("verdict",), None),
                ("wrong-verdict-schema", ("verdict", "schema"), "untrusted/v1"),
                ("verdict-not-accepted", ("verdict", "accepted"), False),
                ("verdict-soft-and-accepted", ("verdict", "softAccepted"), True),
                ("verdict-wrong-level", ("verdict", "level"), "exact-code"),
                ("universal-proof", ("semanticEquivalenceVerified",), True),
                ("wrong-observation-schema", ("observation", "schema"), "untrusted/v1"),
                ("wrong-observation-scope", ("observation", "scope"), "all-possible-inputs"),
                ("observation-not-accepted", ("observation", "accepted"), False),
                ("observation-no-restore", ("observation", "replayRestored"), False),
                ("observation-universal", ("observation", "semanticEquivalenceVerified"), True),
                ("missing-metrics", ("observation", "metrics"), None),
                ("missing-tolerances", ("observation", "tolerances"), None),
                ("no-samples", ("observation", "sampleCount"), 0),
                ("boolean-sample-count", ("observation", "sampleCount"), True),
                ("wrong-baseline-source", ("observation", "baselineSourceSha256"), "0" * 64),
                ("wrong-candidate-source", ("observation", "candidateSourceSha256"), "0" * 64),
                ("wrong-baseline-candidate", ("observation", "baselineCandidateRawSha256"), "0" * 64),
                ("wrong-candidate", ("observation", "candidateRawSha256"), "0" * 64),
            ]
            for label, path, value in cases:
                with self.subTest(case=label):
                    gate = copy.deepcopy(original)
                    target = gate
                    for part in path[:-1]:
                        target = target[part]
                    target[path[-1]] = value
                    fixture._write(gate_path, gate)
                    fixture.sync_organization_session()
                    with self.assertRaises(console.ShaderConsoleError):
                        console.load_authorized_source_document(
                            str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID,
                            capture_binding=fixture.capture_binding,
                        )

    def test_organization_gate_requires_completed_matching_session_and_fixed_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            paths = (fixture.baseline_artifact, fixture.proposal_artifact,
                     fixture.session_dir / "session.json", fixture.session_dir / "gate.json")
            originals = {path: path.read_bytes() for path in paths}

            def assert_rejected():
                with self.assertRaises(console.ShaderConsoleError):
                    console.load_authorized_source_document(
                        str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID,
                        capture_binding=fixture.capture_binding,
                    )

            for path in paths:
                with self.subTest(missing=path.name):
                    path.unlink()
                    assert_rejected()
                    path.write_bytes(originals[path])
            for path in (fixture.baseline_artifact, fixture.proposal_artifact):
                with self.subTest(tampered=path.name):
                    path.write_bytes(originals[path] + b"// changed fixed artifact\n")
                    assert_rejected()
                    path.write_bytes(originals[path])
            session_path = fixture.session_dir / "session.json"
            for name, value in (("state", "awaiting-proposal-review"),
                                ("workspaceId", "0" * 32), ("equivalenceLevel", "rename-only")):
                with self.subTest(session=name):
                    session = json.loads(originals[session_path])
                    session[name] = value
                    fixture._write(session_path, fixture.organization_manager._seal_session(session))
                    assert_rejected()
                    session_path.write_bytes(originals[session_path])
            gate_path = fixture.session_dir / "gate.json"
            gate = json.loads(originals[gate_path])
            gate["reviewedAt"] = "2026-08-26T00:00:00Z"
            fixture._write(gate_path, gate)
            assert_rejected()

    def test_legacy_soft_acceptance_cannot_open_editable_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            gate_path = fixture.workspace / "ai" / "organize-code-gate.json"
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            gate["decision"] = "soft-accepted-baseline"
            fixture._write(gate_path, gate)
            fixture.sync_organization_session()
            with self.assertRaises(console.ShaderConsoleError):
                console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID,
                    capture_binding=fixture.capture_binding,
                )

    def test_embedded_process_direct_save_path_is_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            before = fixture.source.read_bytes()
            with self.assertRaisesRegex(console.ShaderConsoleError, "running Bread Host"):
                console.save_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID,
                    fixture.WORKSPACE_ID, console.hashlib.sha256(before).hexdigest(),
                    before.decode("utf-8") + "// edit\\n",
                )
            self.assertEqual(fixture.source.read_bytes(), before)

    def test_post_organization_source_requires_running_watcher_lineage(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            fixture.source.write_text(
                "float4 MainPS() : SV_Target { return 2; }\\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                console.ShaderConsoleError, "unavailable|not running"
            ):
                console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID,
                    fixture.WORKSPACE_ID,
                    capture_binding=fixture.capture_binding,
                )
            fixture.write_watch()
            fixture.write_live()
            opened = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID,
                fixture.WORKSPACE_ID,
                capture_binding=fixture.capture_binding,
            )
            self.assertIn("return 2", opened["text"])
            fixture.write_watch(initialSourceSha256=hashlib.sha256(b"previous user edit").hexdigest())
            resumed = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID, capture_binding=fixture.capture_binding)
            self.assertEqual(resumed["rawSha256"], opened["rawSha256"])

    def test_unrelated_deleted_authorization_does_not_block_exact_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            registry = json.loads(fixture.registry.read_text(encoding="utf-8"))
            stale = dict(registry["entries"][0])
            stale["authorizationId"] = "6" * 32
            stale["workspacePath"] = str(fixture.root / "deleted-workspace")
            stale_identity = dict(stale["workspaceIdentity"])
            stale_identity["workspaceId"] = "7" * 32
            stale["workspaceIdentity"] = stale_identity
            stale["workspaceIdentitySha256"] = console._canonical_sha256(stale_identity)
            registry["entries"].insert(0, stale)
            fixture.registry.write_text(json.dumps(registry), encoding="utf-8")
            opened = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID,
                fixture.WORKSPACE_ID,
                capture_binding=fixture.capture_binding,
            )
            self.assertEqual(opened["workspaceId"], fixture.WORKSPACE_ID)

    def test_exact_production_registry_identity_hash_is_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            registry = json.loads(fixture.registry.read_text(encoding="utf-8"))
            registry["entries"][0]["workspaceIdentitySha256"] = "0" * 64
            fixture.registry.write_text(json.dumps(registry), encoding="utf-8")
            with self.assertRaisesRegex(console.ShaderConsoleError, "identity hash"):
                console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID,
                    fixture.WORKSPACE_ID,
                    capture_binding=fixture.capture_binding,
                )


    def test_code_workspace_rejects_source_language_change_with_stale_live_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            glsl_source = fixture.source.with_name("shader.glsl")
            fixture.source.replace(glsl_source)
            manifest_path = fixture.workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source"]["path"] = "source/shader.glsl"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(console.ShaderConsoleError, "completed Host session is inconsistent"):
                console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID, fixture.WORKSPACE_ID,
                    capture_binding=fixture.capture_binding,
                )

    def test_code_workspace_requires_the_current_strict_organization_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            gate_path = fixture.workspace / "ai" / "organize-code-gate.json"
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            gate["safeToStartLiveEdit"] = False
            gate["decision"] = "awaiting-proposal-review"
            gate_path.write_text(json.dumps(gate), encoding="utf-8")

            snapshot = console.load_console_snapshot(str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual(len(snapshot["workspaces"]), 1)
            self.assertFalse(snapshot["workspaces"][0]["editorEligible"])
            self.assertEqual(
                snapshot["workspaces"][0]["editorEligibility"],
                "organization-gate-unavailable",
            )
            with self.assertRaisesRegex(console.ShaderConsoleError, "organization gate"):
                console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID,
                    fixture.WORKSPACE_ID,
                    capture_binding=fixture.capture_binding,
                )

    def test_saved_unvalidated_draft_is_visible_without_apply_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            gate = fixture.write_editable_draft()
            fixture.write_live(state="running")
            fixture.write_watch(state="running")
            snapshot = console.load_console_snapshot(
                str(fixture.registry), capture_binding=fixture.capture_binding)
            workspace = snapshot["workspaces"][0]
            self.assertTrue(workspace["editorEligible"])
            self.assertEqual(workspace["editorEligibility"], "unvalidated-editable-draft")
            self.assertFalse(workspace["autoApplyReady"])
            opened = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID,
                fixture.WORKSPACE_ID, capture_binding=fixture.capture_binding)
            self.assertTrue(opened["unvalidatedDraft"])
            self.assertEqual(opened["rawSha256"], gate["proposalSourceSha256"])
            self.assertEqual(opened["liveEditId"], fixture.LIVE_ID)
            self.assertEqual(workspace["sourceSession"], {
                "liveEditId": fixture.LIVE_ID, "watchId": fixture.WATCH_ID})
            graph = build_shader_flow_graph(
                fixture.source.read_bytes().decode("utf-8"),
                workspace_id=fixture.WORKSPACE_ID,
                source_path="source/shader.hlsl",
                source_sha256=gate["proposalSourceSha256"],
                source_revision=0,
                stage="fragment",
                entry_point="MainPS",
                generated_at="2026-08-25T04:01:00Z",
                origin="editable-draft",
            )
            fixture._write(fixture.workspace / "ai" / "flow-graph.json", graph)
            refreshed = console.load_console_snapshot(
                str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual(refreshed["workspaces"][0]["flowGraph"]["graphId"], graph["graphId"])
            with self.assertRaises(console.ShaderConsoleError):
                console._validate_organization_gate(
                    str(fixture.workspace), workspace, str(fixture.source),
                )

    def test_saved_draft_remains_visible_after_live_session_file_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            fixture.write_editable_draft()
            (fixture.workspace / "live-edit" / (fixture.LIVE_ID + ".json")).unlink()
            snapshot = console.load_console_snapshot(
                str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual(len(snapshot["workspaces"]), 1)
            workspace = snapshot["workspaces"][0]
            self.assertTrue(workspace["editorEligible"])
            self.assertFalse(workspace["autoApplyReady"])
            opened = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID,
                fixture.WORKSPACE_ID, capture_binding=fixture.capture_binding)
            self.assertTrue(opened["unvalidatedDraft"])
            self.assertIsNone(opened["liveEditId"])

    def test_saved_draft_becomes_live_editable_only_for_current_abi_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            gate = fixture.write_editable_draft()
            fixture.write_live(state="running", activeReplacement={
                "revision": 2, "candidateRawSha256": "3" * 64})
            fixture.write_watch(state="running")
            manifest_path = fixture.workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["revision"] = 2
            manifest["latestCompiled"] = {
                "revision": 2,
                "sourceSha256": gate["proposalSourceSha256"],
                "rawSha256": "3" * 64,
                "replayAbiValidated": True,
                "abiCompatible": True,
                "requiresReplayAbiValidation": False,
                "abiGate": {"status": "compatible", "requestId": "4" * 64,
                            "reportSha256": "5" * 64},
            }
            fixture._write(manifest_path, manifest)
            ready = console.load_console_snapshot(
                str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertTrue(ready["workspaces"][0]["autoApplyReady"])
            opened = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID,
                fixture.WORKSPACE_ID, capture_binding=fixture.capture_binding)
            self.assertEqual(opened["liveEditId"], fixture.LIVE_ID)
            manifest["latestCompiled"]["sourceSha256"] = "0" * 64
            fixture._write(manifest_path, manifest)
            stale = console.load_console_snapshot(
                str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertFalse(stale["workspaces"][0]["autoApplyReady"])

    def test_managed_project_draft_is_visible_without_live_authorization_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = WriterProjectShaderLibraryFixture(root)
            shader_root = project.project_root / "workspaces" / "shaders" / "v2"
            fixture = ShaderConsoleFixture(shader_root, workspace_name="a" * 64)
            fixture.write_editable_draft()
            missing_registry = root / "no-live-authorizations.json"
            with mock.patch.object(
                console, "default_active_project_selection_path",
                return_value=str(project.selection),
            ):
                discovered, unused_discovery_paths = console._managed_project_draft_authorizations(
                    fixture.capture_binding)
                self.assertEqual(len(discovered), 1)
                snapshot = console.load_console_snapshot(
                    str(missing_registry), capture_binding=fixture.capture_binding)
                self.assertEqual(len(snapshot["workspaces"]), 1, snapshot["errors"])
                workspace = snapshot["workspaces"][0]
                self.assertEqual(workspace["editorEligibility"], "unvalidated-editable-draft")
                self.assertFalse(workspace["autoApplyReady"])
                opened = console.load_authorized_source_document(
                    str(missing_registry), workspace["authorizationId"],
                    workspace["workspaceId"], capture_binding=fixture.capture_binding)
            self.assertEqual(opened["rawSha256"],
                             hashlib.sha256(fixture.source.read_bytes()).hexdigest())
            self.assertTrue(opened["unvalidatedDraft"])
            self.assertIsNone(opened["liveEditId"])

    def test_finished_current_draft_is_visible_and_tampered_verdict_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            gate_path = fixture.workspace / "ai" / "organize-code-gate.json"
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            fixture.write_live(state="running")
            fixture.write_watch(state="running")

            snapshot = console.load_console_snapshot(
                str(fixture.registry), capture_binding=fixture.capture_binding)
            self.assertEqual(len(snapshot["workspaces"]), 1)
            self.assertTrue(snapshot["workspaces"][0]["editorEligible"])
            opened = console.load_authorized_source_document(
                str(fixture.registry), fixture.AUTHORIZATION_ID,
                fixture.WORKSPACE_ID, capture_binding=fixture.capture_binding)
            self.assertEqual(opened["rawSha256"], gate["finalSourceSha256"])

            tampered = copy.deepcopy(gate)
            tampered["verdict"]["accepted"] = False
            fixture._write(gate_path, tampered)
            with self.assertRaisesRegex(console.ShaderConsoleError,
                                        "source receipt"):
                console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID,
                    fixture.WORKSPACE_ID,
                    capture_binding=fixture.capture_binding)

    def test_code_workspace_rejects_non_executable_source_lane(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ShaderConsoleFixture(Path(temporary))
            manifest_path = fixture.workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source"]["lane"] = "explanatory-ir"
            manifest["source"]["compileEligible"] = False
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(console.ShaderConsoleError, "executable editable"):
                console.load_authorized_source_document(
                    str(fixture.registry), fixture.AUTHORIZATION_ID,
                    fixture.WORKSPACE_ID,
                    capture_binding=fixture.capture_binding,
                )

    def test_ui_projection_has_no_shell_or_process_execution_surface(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "subprocess", "os.system", "Popen(", "ShellExecute", "QProcess",
            "startDetached",
        ):
            self.assertNotIn(forbidden, source)

    def test_bridge_registers_console_without_forcing_process_power_policy(self):
        source = BRIDGE_PATH.read_text(encoding="utf-8")
        self.assertIn("_register_shader_console_ui(ctx)", source)
        self.assertIn("_unregister_shader_console_ui()", source)
        self.assertIn("ReplayLabConsolePanel", source)
        # Menu labels go through the shared project translation context, so
        # the console shows under the same (localized) group as the rest.
        self.assertIn('translate("ReplayLabProjects", "Bread AI")', source)
        self.assertIn('translate("ReplayLabProjects", "Console")', source)
        self.assertIn("qrd.WindowMenu.Tools, [_group, _console], _show)", source)
        self.assertIn("qrd.WindowMenu.Window, [_console], _show)", source)
        self.assertIn("publish_console_event", source)
        self.assertNotIn(
            "_shader_console_protocol.disable_background_power_throttling()",
            source,
        )
        self.assertNotIn("disable_background_power_throttling", MODULE_PATH.read_text(
            encoding="utf-8"))

    def test_external_ipc_poll_is_bounded_and_not_frame_rate(self):
        source = BRIDGE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BRIDGE_PATH), feature_version=(3, 6))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name)
                    and target.id == "IPC_POLL_INTERVAL_MS"
                    for target in node.targets)
        )
        interval = ast.literal_eval(assignment.value)
        self.assertGreaterEqual(interval, 250)

    def test_bridge_quiets_fixed_background_polls_but_keeps_all_failures(self):
        source = BRIDGE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BRIDGE_PATH), feature_version=(3, 6))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name)
                    and target.id == "CONSOLE_QUIET_REQUEST_METHODS"
                    for target in node.targets)
        )
        quiet = set(ast.literal_eval(assignment.value.args[0]))
        self.assertIn("ping", quiet)
        self.assertIn("get_current_renderdoc_context", quiet)
        self.assertIn("get_shader_cbuffer_alias_identity", quiet)
        self.assertIn("get_shader_workspace_ui_state", quiet)
        self.assertNotIn("claim_renderdoc_ui_intents", quiet)
        self.assertNotIn("apply_shader_candidate_shared_resource", quiet)
        self.assertIn("if lifecycle_visible:", source)
        self.assertIn('code="requestFailed"', source)
        self.assertIn('code="requestRejected"', source)

    def test_ui_filesystem_notifications_are_short_coalesced_not_immediate(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("FILESYSTEM_COALESCE_MS = 20", source)
        self.assertIn("self.change_timer.setSingleShot(True)", source)
        self.assertIn("self.change_timer.timeout.connect(self._flush_filesystem_changes)", source)
        self.assertIn("if not self.change_timer.isActive():", source)
        self.assertIn("self.timer.setInterval(self.POLL_INTERVAL_MS)", source)

    def test_console_windows_reader_requests_delete_sharing(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("CreateFileW", source)
        self.assertIn("0x1 | 0x2 | 0x4", source)


def test_verified_organization_preview_is_read_only_stable_and_capture_bound(tmp_path):
    workspace = tmp_path / "workspace"
    preview_dir = workspace / "ai" / "organization-previews"
    preview_dir.mkdir(parents=True)
    capture_path = (tmp_path / "capture.rdc").resolve()
    capture_path.write_bytes(b"rdc")
    workspace_id = "1" * 32
    capture_id = "2" * 64
    organization_id = "3" * 32
    manifest = {
        "schema": console.WORKSPACE_SCHEMA,
        "schemaVersion": 1,
        "workspaceId": workspace_id,
        "capture": {
            "captureId": capture_id,
            "capturePath": str(capture_path),
        },
        # Real captures use 64-bit ResourceIds (for example EID 11384 uses a
        # value around 1e18).  The preview loader must not apply its generic
        # 31-bit count limit to this identity.
        "shader": {"resourceId": 1000000000000000723},
        "stage": "fragment",
        "entryPoint": "MainPS",
    }
    (workspace / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    binding = {
        "instanceId": "preview-test",
        "captureSessionId": "4" * 32,
        "capturePath": str(capture_path),
    }

    def publish(text):
        raw = text.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        source = preview_dir / (digest + ".hlsl")
        source.write_bytes(raw)
        receipt = {
            "schema": console.ORGANIZATION_PREVIEW_SCHEMA,
            "schemaVersion": 1,
            "organizationId": organization_id,
            "workspaceId": workspace_id,
            "captureId": capture_id,
            "stage": "fragment",
            "entryPoint": "MainPS",
            "sourceFormat": "hlsl",
            "sourcePath": source.relative_to(workspace).as_posix(),
            "sourceSha256": digest,
            "sourceByteSize": len(raw),
            "baselineSourceSha256": "5" * 64,
            "validationTier": "segment-compile-check",
            "completedChecks": ["compile"],
            "deferredFinalChecks": ["replay-abi", "equivalence"],
            "numericVerified": False,
            "readOnly": True,
            "promoted": False,
            "canonicalSourceUpdated": False,
            "replayAbiChecked": False,
            "equivalenceChecked": False,
            "replayApplied": False,
            "verifiedAt": "2026-09-20T00:00:00Z",
        }
        (workspace / "ai" / "organization-preview.json").write_text(
            json.dumps(receipt), encoding="utf-8")
        return digest

    first_sha = publish("float4 MainPS() : SV_Target { return 1.0; }\n")
    first = console.load_verified_organization_preview(
        str(workspace), organization_id, first_sha, binding)
    unrelated_gate = workspace / "ai" / "organize-code-gate.json"
    unrelated_gate.write_text("{broken save metadata", encoding="utf-8")
    reopened = console.load_verified_organization_preview(
        str(workspace), organization_id, first_sha, binding)
    assert reopened["workspaceId"] == first["workspaceId"]
    assert reopened["completedChecks"] == ["compile"]
    unrelated_gate.unlink()
    second_sha = publish("// next segment\nfloat4 MainPS() : SV_Target { return 1.0; }\n")
    second = console.load_verified_organization_preview(
        str(workspace), organization_id, second_sha, binding)
    assert first["workspaceId"] == second["workspaceId"]
    assert second["documentKind"] == "organization-preview"
    assert second["readOnly"] is True
    assert second["resourceId"] == 1000000000000000723
    assert second["rawSha256"] == second_sha
    assert second["text"].startswith("// next segment")

    wrong_binding = dict(binding, capturePath=str((tmp_path / "other.rdc").resolve()))
    with pytest.raises(console.ShaderConsoleError, match="another capture"):
        console.load_verified_organization_preview(
            str(workspace), organization_id, second_sha, wrong_binding)
    receipt_path = workspace / "ai" / "organization-preview.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["workspaceId"] = "6" * 32
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(console.ShaderConsoleError, match="inconsistent"):
        console.load_verified_organization_preview(
            str(workspace), organization_id, second_sha, binding)


def _saved_draft_preview_fixture(tmp_path):
    fixture = ShaderConsoleFixture(tmp_path)
    gate = fixture.write_editable_draft()
    manifest_path = fixture.workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(stage=gate["stage"], entryPoint=gate["entryPoint"])
    fixture._write(manifest_path, manifest)
    # A source preview must be available without any saved live-edit grant.
    fixture.registry.unlink()
    return fixture, gate


def test_project_display_binding_uses_current_capture_without_changing_cas_target(tmp_path):
    fixture, gate = _saved_draft_preview_fixture(tmp_path)
    opened = console.load_saved_shader_source_preview(
        str(fixture.workspace), gate["proposalSourceSha256"], fixture.capture_binding)
    project_root = tmp_path / "managed-project"
    with mock.patch.object(console, "_within", return_value=True):
        display = {"schema": "replaylab-project-shader-display-binding/v1",
                   "projectId": "4" * 32, "knowledgeId": "5" * 32,
                   "sourceSha256": opened["rawSha256"], "originalRawSha256": opened["shaderRawSha256"],
                   "stage": opened["stage"], "entryPoint": opened["entryPoint"],
                   "captureId": "6" * 64, "capturePath": fixture.capture_binding["capturePath"],
                   "captureSessionId": fixture.capture_binding["captureSessionId"],
                   "resourceId": 728, "anchorEventId": 61408}
        library = {"projectId": display["projectId"], "projectRoot": str(project_root), "documents": [{
            "knowledgeId": display["knowledgeId"], "isCurrentHead": True,
            "sourceSha256": opened["rawSha256"], "rawShaderSha256": opened["shaderRawSha256"],
            "stage": opened["stage"], "entryPoint": opened["entryPoint"]}]}
        with mock.patch.object(console, "_project_library_for_console", return_value=library):
            rebound = console.bind_project_shader_source_display(opened, display)
            assert rebound["resourceId"] == 728 and rebound["eventIds"] == [61408]
            assert rebound["sourcePath"] == opened["sourcePath"]
            assert rebound["sourceWorkspaceId"] == opened["sourceWorkspaceId"]
            assert rebound["sourceCaptureId"] == opened["captureId"]
            assert rebound["replayApplied"] is False
            refreshed = console.reload_saved_shader_source_preview(rebound, fixture.capture_binding)
            assert refreshed["projectSourceBinding"] == display
            assert refreshed["resourceId"] == 728
            fixture.source.write_bytes(b"// local saved edit\n" + fixture.source.read_bytes())
            edited = console.reload_saved_shader_source_preview(rebound, fixture.capture_binding)
            assert not edited.get("projectSourceBinding") and not edited.get("projectSourceIdentity")
            assert edited["resourceId"] == 728 and edited["eventIds"] == [61408]
            assert console.reload_saved_shader_source_preview(edited, fixture.capture_binding)["resourceId"] == 728
            for field, value in (("sourceSha256", "0" * 64), ("originalRawSha256", "0" * 64),
                                 ("captureSessionId", "0" * 32), ("stage", "vertex"),
                                 ("entryPoint", "OtherPS"), ("projectId", "0" * 32),
                                 ("knowledgeId", "0" * 32)):
                with pytest.raises(console.ShaderConsoleError):
                    console.bind_project_shader_source_display(opened, dict(display, **{field: value}))
            library["documents"][0]["isCurrentHead"] = False
            with pytest.raises(console.ShaderConsoleError, match="active project head"):
                console.bind_project_shader_source_display(opened, display)


def test_current_project_source_dedup_retains_all_physical_bindings_and_hides_old_head(tmp_path):
    fixture = ShaderConsoleFixture(tmp_path)
    fixture.registry.unlink()
    identity = {"projectId": "1" * 32, "knowledgeId": "2" * 32,
                "sourceSha256": "3" * 64, "originalRawSha256": "4" * 64,
                "stage": "fragment", "entryPoint": "MainPS"}
    models = []
    for index, resource_id in enumerate((754, 3239, 3162, 728)):
        project = dict(identity)
        if index == 3:
            project.update(knowledgeId="5" * 32, sourceSha256="6" * 64)
        models.append({"workspaceId": str(index + 1) * 32,
                       "workspacePath": str(tmp_path / str(index)), "sourcePath": str(tmp_path / str(index) / "source.hlsl"),
                       "captureId": str(index + 1) * 64, "capturePath": fixture.capture_binding["capturePath"],
                       "captureBinding": fixture.capture_binding, "resourceId": resource_id,
                       "shaderRawSha256": identity["originalRawSha256"], "stage": "fragment", "entryPoint": "MainPS",
                       "sourceSha256": project["sourceSha256"], "projectSourceIdentity": project,
                       "editorEligibility": "managed-source", "autoApplyReady": index == 0})
    authorizations = [{"workspacePath": model["workspacePath"], "index": index} for index, model in enumerate(models)]
    library = {"projectId": identity["projectId"], "documents": [
        {"knowledgeId": identity["knowledgeId"], "isCurrentHead": True},
        {"knowledgeId": "5" * 32, "isCurrentHead": False}]}
    with mock.patch.object(console, "_managed_project_draft_authorizations", return_value=(authorizations, [])), \
            mock.patch.object(console, "_workspace_model", side_effect=lambda auth, unused: copy.deepcopy(models[auth["index"]])), \
            mock.patch.object(console, "_project_library_for_console", return_value=library):
        snapshot = console.load_console_snapshot(str(fixture.registry), fixture.capture_binding)
    assert not snapshot["errors"]
    assert [item["workspaceId"] for item in snapshot["workspaces"]] == [models[0]["workspaceId"]]
    assert len(snapshot["alternateWorkspaces"]) == 3
    assert {item["resourceId"] for item in snapshot["workspaces"][0]["relatedWorkspaceBindings"]} == {3239, 3162}
    assert len(snapshot["documentAliases"]) == 2
    assert next(item for item in snapshot["alternateWorkspaces"] if item["resourceId"] == 728)["historicalSource"] is True


@pytest.mark.parametrize("field", ["projectId", "knowledgeId", "originalRawSha256", "stage", "entryPoint", "sourceSha256"])
def test_logical_project_document_identity_does_not_merge_distinct_shader_or_edited_source(field):
    identity = {"projectId": "1" * 32, "knowledgeId": "2" * 32, "originalRawSha256": "3" * 64,
                "stage": "fragment", "entryPoint": "MainPS", "sourceSha256": "4" * 64}
    left = {"projectSourceIdentity": identity}
    right = {"projectSourceIdentity": dict(identity, **{field: "different"})}
    assert console._workspace_document_key(left) != console._workspace_document_key(right)


def test_legacy_decompile_locator_cannot_duplicate_current_restored_head(tmp_path):
    fixture = ShaderConsoleFixture(tmp_path)
    fixture.registry.unlink()
    identity = {"projectId": "1" * 32, "knowledgeId": "2" * 32, "originalRawSha256": "3" * 64,
                "stage": "fragment", "entryPoint": "MainPS", "sourceSha256": "4" * 64}
    current = {"workspaceId": "1" * 32, "workspacePath": str(tmp_path / "current"),
               "resourceId": 9230, "sourcePath": str(tmp_path / "current/shader.hlsl"),
               "sourceSha256": "4" * 64, "shaderRawSha256": "3" * 64, "stage": "fragment", "entryPoint": "MainPS",
               "captureBinding": fixture.capture_binding, "capturePath": fixture.capture_binding["capturePath"],
               "editorEligibility": "managed-source", "projectSourceIdentity": identity, "autoApplyReady": True}
    legacy = dict(current, workspaceId="2" * 32, workspacePath=str(tmp_path / "legacy"),
                  resourceId=723, projectSourceIdentity=None, sourceSha256="5" * 64,
                  autoApplyReady=False, managedSourceLocator=True)
    different = dict(legacy, workspaceId="3" * 32, workspacePath=str(tmp_path / "different"), shaderRawSha256="6" * 64)
    live_distinct = dict(legacy, workspaceId="4" * 32, workspacePath=str(tmp_path / "live-distinct"),
                         managedSourceLocator=False, autoApplyReady=True, resourceId=9000)
    unpublished_current = dict(legacy, workspaceId="5" * 32, workspacePath=str(tmp_path / "unpublished-current"),
                               resourceId=current["resourceId"])
    active_organization = dict(legacy, workspaceId="6" * 32, workspacePath=str(tmp_path / "active-organization"),
                               activeOrganization=True)
    second_current = dict(current, workspaceId="7" * 32, workspacePath=str(tmp_path / "second-current"), resourceId=9231)
    second_draft = dict(unpublished_current, workspaceId="8" * 32, workspacePath=str(tmp_path / "second-draft"), resourceId=9231)
    models = [current, legacy, different, live_distinct, unpublished_current, active_organization, second_current, second_draft]
    auths = [{"workspacePath": item["workspacePath"], "index": index} for index, item in enumerate(models)]
    with mock.patch.object(console, "_managed_project_draft_authorizations", return_value=(auths, [])), \
            mock.patch.object(console, "_workspace_model", side_effect=lambda auth, unused: copy.deepcopy(models[auth["index"]])), \
            mock.patch.object(console, "_project_library_for_console", return_value={"projectId": identity["projectId"],
                "documents": [{"knowledgeId": identity["knowledgeId"], "isCurrentHead": True}]}):
        snapshot = console.load_console_snapshot(str(fixture.registry), fixture.capture_binding)
    assert not snapshot["errors"]
    shown = {item["workspaceId"] for item in snapshot["workspaces"]}
    assert shown & {current["workspaceId"], second_current["workspaceId"]}
    assert shown - {current["workspaceId"], second_current["workspaceId"]} == {
        item["workspaceId"] for item in (different, live_distinct, unpublished_current, active_organization, second_draft)}
    archived = next(item for item in snapshot["alternateWorkspaces"] if item["workspaceId"] == legacy["workspaceId"])
    assert archived["supersededCaptureSource"] is True


@pytest.mark.parametrize("frozen_preview", [False, True])
def test_saved_editable_source_refresh_reads_actual_new_bytes_without_old_review_receipt(tmp_path, frozen_preview):
    fixture, gate = _saved_draft_preview_fixture(tmp_path)
    previous = console.load_saved_shader_source_preview(
        str(fixture.workspace), gate["proposalSourceSha256"], fixture.capture_binding)
    if frozen_preview:
        previous.update(sourcePath=str(fixture.workspace / "ai/frozen-preview/shader.hlsl"),
                        canonicalSourceUpdated=False)
    replacement = b"// concurrent source change\n" + fixture.source.read_bytes()
    fixture.source.write_bytes(replacement)
    refreshed = console.reload_saved_shader_source_preview(previous, fixture.capture_binding)
    assert refreshed["text"].encode("utf-8") == replacement
    assert refreshed["rawSha256"] != previous["rawSha256"]
    assert refreshed["replayApplied"] is False
    assert refreshed["sourcePath"] == str(fixture.source)


@pytest.mark.parametrize("source_format", ("hlsl", "glsl"))
def test_saved_source_preview_needs_no_compile_or_live_edit_authorization(tmp_path, source_format):
    fixture, gate = _saved_draft_preview_fixture(tmp_path)
    if source_format == "glsl":
        fixture.source = fixture.source.rename(fixture.source.with_suffix(".glsl"))
        manifest_path = fixture.workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["routePlan"] = {"editableFormat": "glsl"}
        manifest["source"]["path"] = "source/shader.glsl"
        fixture._write(manifest_path, manifest)
        gate["editableDraftSourcePath"] = str(fixture.source.resolve())
        fixture._write(fixture.workspace / "ai" / "organize-code-gate.json", gate)
        session_path = fixture.session_dir / "session.json"
        session = json.loads(session_path.read_text(encoding="utf-8"))
        session["sourcePath"] = "source/shader.glsl"
        fixture._write(session_path, session)
    preview = console.load_verified_organization_preview(
        str(fixture.workspace), gate["organizationId"],
        gate["proposalSourceSha256"], fixture.capture_binding)
    assert preview["text"] == fixture.source.read_bytes().decode("utf-8")
    assert preview["sourcePath"] == str(fixture.source.resolve())
    assert preview["sourceWorkspaceId"] == fixture.WORKSPACE_ID
    assert preview["workspaceId"] != fixture.WORKSPACE_ID
    assert preview["readOnly"] is True
    assert preview["canonicalSourceUpdated"] is True
    assert preview["replayApplied"] is False
    assert preview["validationTier"] == "saved-source"
    assert preview["completedChecks"] == ["source-save"]
    assert preview["deferredFinalChecks"] == ["compile", "replay-abi", "equivalence"]
    assert preview["receiptPath"].endswith("organize-code-gate.json")
    assert "authorizationId" not in preview
    assert "liveEditId" not in preview


@pytest.mark.parametrize("changed", (
    "final-binding", "canonical-source", "proposal", "session", "source-path",
))
def test_saved_source_preview_rejects_changed_save_binding(tmp_path, changed):
    fixture, gate = _saved_draft_preview_fixture(tmp_path)
    if changed == "final-binding":
        gate["finalBinding"]["sourceSha256"] = "0" * 64
        fixture._write(fixture.workspace / "ai" / "organize-code-gate.json", gate)
    elif changed == "canonical-source":
        fixture.source.write_text("// source changed\n", encoding="utf-8")
    elif changed == "proposal":
        fixture.proposal_artifact.write_text("// proposal changed\n", encoding="utf-8")
    elif changed == "session":
        session_path = fixture.session_dir / "session.json"
        session = json.loads(session_path.read_text(encoding="utf-8"))
        session["syncedDraftSourceSha256"] = "0" * 64
        fixture._write(session_path, session)
    elif changed == "source-path":
        manifest_path = fixture.workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"]["path"] = "../shader.hlsl"
        fixture._write(manifest_path, manifest)
    with pytest.raises(console.ShaderConsoleError):
        console.load_verified_organization_preview(
            str(fixture.workspace), gate["organizationId"],
            gate["proposalSourceSha256"], fixture.capture_binding)


@pytest.mark.parametrize("changed", ("receipt", "canonical-source"))
def test_saved_source_preview_rechecks_a_save_that_changes_during_read(tmp_path, changed):
    fixture, gate = _saved_draft_preview_fixture(tmp_path)
    read_bytes = console._read_file_bytes

    def race(path, maximum, field):
        raw = read_bytes(path, maximum, field)
        if field == "organization draft canonical source":
            if changed == "receipt":
                replaced = dict(gate, reviewedAt="changed during source read")
                fixture._write(fixture.workspace / "ai" / "organize-code-gate.json", replaced)
            else:
                fixture.source.write_text("// concurrent editor write\n", encoding="utf-8")
        return raw

    with mock.patch.object(console, "_read_file_bytes", side_effect=race):
        with pytest.raises(console.ShaderConsoleError, match="changed"):
            console.load_verified_organization_preview(
                str(fixture.workspace), gate["organizationId"],
                gate["proposalSourceSha256"], fixture.capture_binding)


def _accepted_source_preview_fixture(tmp_path):
    fixture = ShaderConsoleFixture(tmp_path)
    gate = json.loads((fixture.workspace / "ai" / "organize-code-gate.json")
                      .read_text(encoding="utf-8"))
    manifest_path = fixture.workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(stage=gate["stage"], entryPoint=gate["entryPoint"])
    fixture._write(manifest_path, manifest)
    fixture.registry.unlink()
    return fixture, gate


@pytest.mark.parametrize("stale_preview", (False, True))
def test_finalized_source_preview_displays_current_source_without_resaving(tmp_path, stale_preview):
    fixture, gate = _accepted_source_preview_fixture(tmp_path)
    if stale_preview:
        fixture._write(fixture.workspace / "ai" / "organization-preview.json", {
            "organizationId": "0" * 32, "sourceSha256": "0" * 64,
        })
    tracked = [fixture.source, fixture.session_dir / "session.json",
               fixture.workspace / "ai" / "organize-code-gate.json"]
    before = [path.read_bytes() for path in tracked]
    opened = console.load_verified_organization_preview(
        str(fixture.workspace), gate["organizationId"],
        gate["finalSourceSha256"], fixture.capture_binding)
    assert opened["sourcePath"] == str(fixture.source.resolve())
    assert opened["rawSha256"] == gate["finalSourceSha256"]
    assert opened["text"] == before[0].decode("utf-8")
    assert opened["readOnly"] is True
    assert opened["replayApplied"] is False
    assert opened["sourceWorkspaceId"] == fixture.WORKSPACE_ID
    assert opened["completedChecks"] == ["source-save"]
    assert [path.read_bytes() for path in tracked] == before


@pytest.mark.parametrize("changed", (
    "binding-source", "session-state", "session-binding", "baseline", "canonical-source",
))
def test_finalized_source_preview_rejects_stale_source_proof(tmp_path, changed):
    fixture, gate = _accepted_source_preview_fixture(tmp_path)
    if changed == "binding-source":
        gate["finalBinding"]["sourceSha256"] = "0" * 64
        fixture._write(fixture.workspace / "ai" / "organize-code-gate.json", gate)
    elif changed in ("session-state", "session-binding"):
        session_path = fixture.session_dir / "session.json"
        session = json.loads(session_path.read_text(encoding="utf-8"))
        if changed == "session-state":
            session["state"] = "drafting"
        else:
            session["finalBinding"]["sourceSha256"] = "0" * 64
        fixture._write(session_path, session)
    elif changed == "baseline":
        fixture.baseline_artifact.write_text("// changed baseline\n", encoding="utf-8")
    else:
        fixture.source.write_text("// changed canonical source\n", encoding="utf-8")
    with pytest.raises(console.ShaderConsoleError):
        console.load_verified_organization_preview(
            str(fixture.workspace), gate["organizationId"],
            gate["finalSourceSha256"], fixture.capture_binding)


def test_finalized_source_preview_rechecks_canonical_bytes_after_read(tmp_path):
    fixture, gate = _accepted_source_preview_fixture(tmp_path)
    read_bytes = console._read_file_bytes

    def race(path, maximum, field):
        raw = read_bytes(path, maximum, field)
        if field == "organization draft canonical source":
            fixture.source.write_text("// concurrent source write\n", encoding="utf-8")
        return raw

    with mock.patch.object(console, "_read_file_bytes", side_effect=race):
        with pytest.raises(console.ShaderConsoleError, match="bytes changed"):
            console.load_verified_organization_preview(
                str(fixture.workspace), gate["organizationId"],
                gate["finalSourceSha256"], fixture.capture_binding)


def test_source_catalog_excludes_console_journal_dependencies(tmp_path):
    fixture = ShaderConsoleFixture(tmp_path)
    with mock.patch.object(console, '_load_generic_console_entries',
                           side_effect=AssertionError('Source navigation must not read journals')):
        snapshot = console.load_console_snapshot(
            str(fixture.registry), fixture.capture_binding, include_console_events=False)
    assert snapshot['workspaces']
    assert snapshot['sourceDiscoveryErrors'] == []


def test_saved_manual_draft_remains_readable_when_only_canonical_text_is_newer(tmp_path):
    fixture = ShaderConsoleFixture(tmp_path)
    fixture.write_editable_draft()
    gate_path = fixture.workspace / "ai/organize-code-gate.json"
    receipt = gate_path.read_bytes()
    latest = fixture.source.read_bytes() + b"\n// manually edited after the last draft receipt\n"
    fixture.source.write_bytes(latest)
    snapshot = console.load_console_snapshot(str(fixture.registry), fixture.capture_binding)
    model = next(item for item in snapshot["workspaces"] if item["workspaceId"] == fixture.WORKSPACE_ID)
    assert model["editorEligible"] and model["manualDraft"] and not model["autoApplyReady"]
    opened = console.load_authorized_source_document(str(fixture.registry), fixture.AUTHORIZATION_ID,
        fixture.WORKSPACE_ID, capture_binding=fixture.capture_binding)
    assert opened["text"].encode("utf-8") == latest
    assert opened["sourceEditable"] and opened["unvalidatedDraft"]
    assert gate_path.read_bytes() == receipt  # Reading does not rewrite historical verification.


def test_imported_saved_draft_is_not_hidden_as_an_old_project_head(tmp_path):
    fixture = ImportedProjectSourceFixture(tmp_path, managed_name='a' * 64 + '-hlsl-deadbeef')
    editor = fixture.editor
    editor.write_organization_gate()
    editor.write_editable_draft()
    with mock.patch.object(console, 'default_active_project_selection_path',
                           return_value=str(fixture.project.selection)):
        library = console.load_project_shader_library_snapshot(str(fixture.project.selection))
        for item in library['documents']:
            item['isCurrentHead'] = False
        with mock.patch.object(console, '_project_library_for_console', return_value=library):
            snapshot = console.load_console_snapshot(str(editor.registry), editor.capture_binding)
            matches = [m for m in snapshot['workspaces'] if m['workspaceId'] == editor.WORKSPACE_ID]
            assert len(matches) == 1
            assert matches[0]['activeOrganization'] and matches[0]['editorEligible']
            assert not matches[0]['autoApplyReady']
            with console.known_source_locator(str(editor.workspace)):
                known, unused = console._managed_project_draft_authorizations_uncached(editor.capture_binding)
            assert any(m['workspaceId'] == editor.WORKSPACE_ID for m in known)




def test_console_journal_updates_reuse_catalog_but_source_changes_rebuild(tmp_path):
    source = tmp_path / 'shader.hlsl'; source.write_text('original', encoding='utf-8')
    journal = tmp_path / 'events.jsonl'; journal.write_text('', encoding='utf-8')
    catalog = {'captureBinding': None, 'watchedPaths': [str(source)], 'entries': [],
               'errors': [], 'workspaces': [], 'counts': {}}
    request = dict(stop=console.threading.Event(), registryPath='registry', captureBinding=None,
                   watchedPaths=[], previousSignature=None, force=False)
    def generic():
        return {'watchedPaths': [str(journal)], 'errors': [], 'entries': [
            {'severity': 'info', 'text': journal.read_text(), 'unixMs': 1}]}
    with mock.patch.object(console, 'load_console_snapshot', return_value=catalog) as load, \
            mock.patch.object(console, '_load_generic_console_entries', side_effect=generic):
        for index in range(5):
            journal.write_text('message ' + str(index), encoding='utf-8')
            request['changedPaths'] = [str(journal)]
            result = console._build_console_panel_snapshot(request)
            assert result['snapshot']['entries'][-1]['text'] == 'message ' + str(index)
            request.update(watchedPaths=result['snapshot']['watchedPaths'],
                           previousSignature=result['signature'], cachedCatalog=result['cachedCatalog'])
        assert load.call_count == 2  # Initial discovery and its stable warm observation.
        assert load.call_args.kwargs['include_console_events'] is False
        source.write_text('new authored source', encoding='utf-8')
        result = console._build_console_panel_snapshot(request)
        assert load.call_count == 3
        request.update(cachedCatalog=result['cachedCatalog'], captureBinding={'new': 'capture'})
        console._build_console_panel_snapshot(request)
        assert load.call_count == 4
        request['force'] = True
        console._build_console_panel_snapshot(request)
        assert load.call_count == 5


if __name__ == "__main__":
    unittest.main()
