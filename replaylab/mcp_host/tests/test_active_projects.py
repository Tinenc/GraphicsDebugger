from __future__ import annotations

import hashlib
import copy
import json
import os
from pathlib import Path
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from src import active_projects as active_projects_module
from src.active_projects import (
    ActiveProjectError,
    ActiveReplayProjectService,
    CAPTURE_LIBRARY_SCHEMA,
    FUTURE_FRAME_PLAN_SCHEMA,
    PROJECT_WORKSPACE_SCHEMA,
)
from src.symbols.dxbc import fingerprint_bytes
from src.symbols.matcher import ALIAS_SCHEMA, ALIAS_VERSION, capture_identity
from src.shader_workspace.flow_graph import (
    build_shader_flow_graph,
    load_workspace_flow_graph_status,
    validate_shader_flow_graph,
)
from src.shader_workspace.organization import ShaderOrganizationManager


def _canonical_hash(value: dict, field: str) -> str:
    material = dict(value)
    material.pop(field, None)
    payload = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _dxbc(code: bytes = b"compiled instructions", metadata: bytes = b"") -> bytes:
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
        b"DXBC"
        + b"\x00" * 16
        + struct.pack("<I", 1)
        + struct.pack("<I", cursor)
        + struct.pack("<I", len(chunks))
        + b"".join(struct.pack("<I", offset) for offset in offsets)
        + b"".join(chunks)
    )


def _complete_interface() -> dict:
    return {
        "interfaceComplete": True,
        "inputSignature": [],
        "outputSignature": [],
        "readOnlyResources": [],
        "readWriteResources": [],
        "samplers": [],
        "constantBuffers": [
            {
                "name": "PerMaterial",
                "space": 0,
                "bind": 0,
                "arraySize": 1,
                "byteSize": 16,
                "bufferBacked": True,
                "inlineDataBytes": False,
                "compileConstants": False,
                "fields": [
                    {
                        "name": "BaseColor",
                        "byteOffset": 0,
                        "byteSize": 16,
                        "baseType": "Float",
                        "rows": 1,
                        "columns": 4,
                        "elements": 1,
                        "arrayByteStride": 0,
                        "matrixByteStride": 0,
                        "rowMajor": False,
                        "flags": 0,
                        "pointerTypeID": 0,
                        "bitFieldOffset": 0,
                        "bitFieldSize": 0,
                        "members": [],
                    }
                ],
            }
        ],
        "threadGroupSize": [],
        "outputTopology": "TriangleList",
        "dynamicLinkageInterfaceCount": 0,
    }


class ActiveReplayProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects_home = self.root / "projects"
        self.state_path = self.root / "state" / "active-project.json"
        self.service = ActiveReplayProjectService(
            self.state_path, self.projects_home
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _pending(self) -> dict:
        return self.service.ensure_pending("Example Game Project")

    def test_project_list_is_metadata_only_and_never_authorizes_replay_reuse(self):
        pending=self._pending()
        self.service.configure_scope(game_id="test-game",build_id="test-build")
        with mock.patch.object(self.service,"_replay_service_and_status_for",side_effect=AssertionError("No Shader hash scan in a list")), \
             mock.patch.object(self.service,"_read_shared_index",side_effect=AssertionError("No shared-artifact scan in a list")):
            listed=self.service.list_projects()
        self.assertEqual(1,listed["count"])
        self.assertEqual(pending["workspaceId"],listed["projects"][0]["workspaceId"])
        self.assertEqual("workspace-and-capture-library-metadata",listed["projects"][0]["validationScope"])
        self.assertFalse(listed["projects"][0]["policy"]["physicalKnowledgeReuseAllowed"])

    def _selection_lock_payload(
        self,
        *,
        owner_pid: int = 424242,
        process_identity: str = "windows-filetime:0000000000001234",
        token: str = "a" * 32,
    ) -> bytes:
        value = {
            "schema": active_projects_module.EXCLUSIVE_PROJECT_LOCK_SCHEMA,
            "schemaVersion": active_projects_module.EXCLUSIVE_PROJECT_LOCK_VERSION,
            "token": token,
            "ownerPid": owner_pid,
            "processStartIdentity": process_identity,
            "acquiredAt": "2026-08-28T00:00:00Z",
        }
        value["lockSha256"] = _canonical_hash(value, "lockSha256")
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def _bound(self, *, api_family: str = "d3d11") -> dict:
        self._pending()
        return self.service.configure_scope(
            game_id="example-game",
            build_id="build-1",
            platform="windows-x64",
            api_family=api_family,
        )

    def _catalog(
        self,
        label: str,
        blob: bytes,
        *,
        resource_id: int,
        include_scope: bool = True,
    ) -> tuple[Path, dict, str]:
        directory = self.root / "catalog-sources" / label
        directory.mkdir(parents=True)
        blob_path = directory / "shader.dxbc"
        blob_path.write_bytes(blob)
        fingerprint = fingerprint_bytes(blob)
        self.assertIsNotNone(fingerprint)
        shader = {
            "shaderResourceId": resource_id,
            "stage": "fragment",
            "entryPoint": "main",
            "rawPath": "shader.dxbc",
            "rawSize": len(blob),
            "rawSha256": hashlib.sha256(blob).hexdigest(),
            "codeSha256": fingerprint.code_sha256,
            **_complete_interface(),
        }
        value = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": str(directory / (label + ".rdc")),
            "api": "D3D11",
            "shaders": [shader],
            "uses": [],
        }
        if include_scope:
            value.update(
                {
                    "gameId": "example-game",
                    "buildId": "build-1",
                    "platform": "windows-x64",
                }
            )
        capture_id = capture_identity(value, [shader])
        value["captureId"] = capture_id
        path = directory / "catalog.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        returned_shader = dict(shader)
        returned_shader["rawPath"] = str(blob_path)
        return path, returned_shader, capture_id

    def _workspace_status(
        self,
        label: str,
        capture_id: str,
        shader: dict,
        *,
        flowchart: bytes | None = b"flowchart TD\n  A[Input] --> B[Color]\n",
    ) -> dict:
        workspace = self.root / "source-workspaces" / label
        (workspace / "source").mkdir(parents=True)
        source_path = workspace / "source" / "shader.hlsl"
        source_path.write_text(
            "float4 main() : SV_Target { return float4(1, 0, 0, 1); }\n",
            encoding="utf-8",
        )
        (workspace / "ai").mkdir()
        (workspace / "ai" / "analysis.md").write_text(
            "Explanatory analysis only.\n", encoding="utf-8"
        )
        if flowchart is not None:
            (workspace / "ai" / "flowchart.mmd").write_bytes(flowchart)
        manifest_path = workspace / "manifest.json"
        manifest_path.write_text('{"validated":true}\n', encoding="utf-8")
        fingerprint = fingerprint_bytes(Path(shader["rawPath"]).read_bytes())
        self.assertIsNotNone(fingerprint)
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        target_profile = {
            "kind": "DXBC",
            "stage": "fragment",
            "profile": "ps_5_0",
        }
        compiler_binding = {
            "schema": "renderdoc-ultra-shader-compiler-binding/v1",
            "schemaVersion": 1,
            "targetProfile": dict(target_profile),
            "routePlanSha256": "3" * 64,
            "hops": [
                {
                    "hop": 1,
                    "routeId": "test-hlsl-dxbc",
                    "inputFormat": "hlsl",
                    "outputFormat": "dxbc",
                    "toolId": "test-dxc",
                    "operation": "compile",
                    "toolConfigDigest": "1" * 64,
                    "executableSha256": "2" * 64,
                    "fixedFileHashes": {},
                    "argumentTemplate": ["-T", "{profile}", "-O3"],
                    "placeholders": ["profile"],
                }
            ],
            "executionAuthority": False,
            "evidenceOnly": True,
        }
        compiler_binding["bindingSha256"] = _canonical_hash(
            compiler_binding, "bindingSha256"
        )
        return {
            "schema": "renderdoc-ultra-shader-workspace-status/v1",
            "workspacePath": str(workspace),
            "manifestPath": str(manifest_path),
            "workspaceId": hashlib.md5(label.encode("utf-8")).hexdigest(),
            "capture": {"captureId": capture_id},
            "shader": {
                "resourceId": shader["shaderResourceId"],
                "rawSha256": fingerprint.raw_sha256,
            },
            "stage": "fragment",
            "entryPoint": "main",
            "targetProfile": target_profile,
            "compilerBinding": compiler_binding,
            "source": {
                "present": True,
                "path": str(source_path),
                "format": "hlsl",
                "lane": "executable-editable",
                "currentSha256": source_hash,
                "decompiledSha256": source_hash,
                "modifiedSinceDecompile": False,
            },
            "baseline": {
                "rawSha256": fingerprint.raw_sha256,
                "codeSha256": fingerprint.code_sha256,
                "abiSha256": "a" * 64,
            },
        }

    def _with_fully_validated_candidate(
        self, status: dict, candidate_blob: bytes, *, revision: int = 5
    ) -> dict:
        workspace = Path(status["workspacePath"])
        source_bytes = Path(status["source"]["path"]).read_bytes()
        revision_dir = workspace / "revisions" / ("%04d" % revision)
        revision_dir.mkdir(parents=True)
        candidate_path = revision_dir / "shader.bin"
        revision_source = revision_dir / "shader.hlsl"
        candidate_path.write_bytes(candidate_blob)
        revision_source.write_bytes(source_bytes)
        report_path = revision_dir / "abi-report.json"
        report_path.write_text('{"compatible":true}\n', encoding="utf-8")
        fingerprint = fingerprint_bytes(candidate_blob)
        self.assertIsNotNone(fingerprint)
        latest = {
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
                "reportSha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            },
        }
        Path(status["manifestPath"]).write_text(
            json.dumps({"revision": revision, "latestCompiled": latest}),
            encoding="utf-8",
        )
        status.update(
            {
                "revision": revision,
                "latestCompiled": {
                    **latest,
                    "candidateBlobPath": str(candidate_path.resolve()),
                    "applied": False,
                },
                "replayAbiValidated": True,
                "abiCompatible": True,
            }
        )
        return status

    def test_first_use_creates_persistent_pending_workspace(self) -> None:
        first = self._pending()
        second = self.service.ensure_pending("Ignored New Name")

        self.assertEqual(first["schema"], PROJECT_WORKSPACE_SCHEMA)
        self.assertEqual(first["workspaceId"], second["workspaceId"])
        self.assertEqual(first["projectRoot"], second["projectRoot"])
        self.assertEqual(first["state"], "pending-scope")
        self.assertFalse(first["scopeConfigured"])
        self.assertFalse(first["policy"]["physicalKnowledgeReuseAllowed"])
        self.assertFalse(first["policy"]["crossGameAutomaticPhysicalReuse"])
        self.assertFalse(first["policy"]["aiProvisionalAutoApply"])
        for key in (
            "captureCatalogs",
            "shaderWorkspaces",
            "sharedDictionaries",
            "sharedExplanatory",
        ):
            self.assertTrue(Path(first["paths"][key]).is_dir())
        self.assertTrue(self.state_path.is_file())

    def test_selection_lock_persists_pid_creation_identity_and_releases_exact_owner(
        self,
    ) -> None:
        lock_path = self.service._selection_lock_path
        with active_projects_module._exclusive_lock(lock_path, "test selection"):
            value = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(
                active_projects_module.EXCLUSIVE_PROJECT_LOCK_SCHEMA,
                value["schema"],
            )
            self.assertEqual(os.getpid(), value["ownerPid"])
            self.assertTrue(value["processStartIdentity"])
            self.assertRegex(value["token"], r"^[0-9a-f]{32}$")
        self.assertFalse(lock_path.exists())

    def test_live_structured_selection_lock_is_never_stolen(self) -> None:
        self._pending()
        lock_path = self.service._selection_lock_path
        raw = self._selection_lock_payload()
        lock_path.write_bytes(raw)
        with mock.patch.object(
            active_projects_module,
            "_process_lock_owner_state",
            return_value=("alive", "windows-filetime:0000000000001234"),
        ):
            with self.assertRaisesRegex(
                ActiveProjectError, "owner could not be proven inactive"
            ):
                self.service.status()
        self.assertEqual(raw, lock_path.read_bytes())

    def test_unknown_structured_selection_lock_owner_stays_fail_closed(self) -> None:
        self._pending()
        lock_path = self.service._selection_lock_path
        raw = self._selection_lock_payload()
        lock_path.write_bytes(raw)
        with mock.patch.object(
            active_projects_module,
            "_process_lock_owner_state",
            return_value=("unknown", ""),
        ):
            with self.assertRaisesRegex(
                ActiveProjectError, "owner could not be proven inactive"
            ):
                self.service.status()
        self.assertEqual(raw, lock_path.read_bytes())

    def test_dead_structured_selection_lock_is_recovered(self) -> None:
        self._pending()
        lock_path = self.service._selection_lock_path
        owner_pid = 424242
        lock_path.write_bytes(self._selection_lock_payload(owner_pid=owner_pid))
        real_probe = active_projects_module._process_lock_owner_state

        def probe(pid: int) -> tuple[str, str]:
            if pid == owner_pid:
                return "dead", ""
            return real_probe(pid)

        with mock.patch.object(
            active_projects_module, "_process_lock_owner_state", side_effect=probe
        ):
            status = self.service.status()
        self.assertEqual("Example Game Project", status["name"])
        self.assertFalse(lock_path.exists())
        self.assertEqual(
            [], list(lock_path.parent.glob(lock_path.name + ".recovered-*"))
        )

    def test_reused_pid_structured_selection_lock_is_recovered(self) -> None:
        self._pending()
        lock_path = self.service._selection_lock_path
        owner_pid = 424242
        lock_path.write_bytes(
            self._selection_lock_payload(
                owner_pid=owner_pid,
                process_identity="windows-filetime:0000000000001234",
            )
        )
        real_probe = active_projects_module._process_lock_owner_state

        def probe(pid: int) -> tuple[str, str]:
            if pid == owner_pid:
                return "alive", "windows-filetime:0000000000005678"
            return real_probe(pid)

        with mock.patch.object(
            active_projects_module, "_process_lock_owner_state", side_effect=probe
        ):
            status = self.service.status()
        self.assertEqual("Example Game Project", status["name"])
        self.assertFalse(lock_path.exists())

    def test_crashed_child_structured_lock_is_recovered_from_real_os_identity(
        self,
    ) -> None:
        lock_path = self.root / "crashed-child.lock"
        module_root = Path(active_projects_module.__file__).resolve().parents[1]
        child_code = """
import pathlib
import sys
import time
from src import active_projects
with active_projects._exclusive_lock(pathlib.Path(sys.argv[1]), "child test"):
    time.sleep(60)
"""
        environment = dict(os.environ)
        prior_python_path = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(module_root) + (
            os.pathsep + prior_python_path if prior_python_path else ""
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(lock_path)],
            cwd=str(module_root),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5.0
            while not lock_path.exists() and time.monotonic() < deadline:
                if child.poll() is not None:
                    break
                time.sleep(0.02)
            if not lock_path.exists():
                unused_stdout, stderr = child.communicate(timeout=2.0)
                self.fail("child did not publish its lock: %s" % stderr)
            value = json.loads(lock_path.read_text(encoding="utf-8"))
            # A Windows virtual-environment redirector may spawn the real
            # interpreter, so Popen.pid can identify the redirector rather
            # than the process which actually acquired the lock.  Exercise
            # recovery against the OS identity sealed into the lock itself.
            owner_pid = value["ownerPid"]
            owner_state, owner_identity = (
                active_projects_module._process_lock_owner_state(owner_pid)
            )
            self.assertEqual("alive", owner_state)
            self.assertEqual(value["processStartIdentity"], owner_identity)
            os.kill(owner_pid, signal.SIGTERM)
            child.wait(timeout=10.0)
            self.assertTrue(lock_path.exists())
            with active_projects_module._exclusive_lock(
                lock_path, "child crash recovery"
            ):
                replacement = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(os.getpid(), replacement["ownerPid"])
            self.assertFalse(lock_path.exists())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10.0)
            if child.stderr is not None:
                child.stderr.close()

    def test_unverifiable_identity_scheme_stays_fail_closed(self) -> None:
        self._pending()
        lock_path = self.service._selection_lock_path
        raw = self._selection_lock_payload(
            process_identity="process-local:" + "b" * 32
        )
        lock_path.write_bytes(raw)
        with mock.patch.object(
            active_projects_module,
            "_process_lock_owner_state",
            return_value=("alive", "windows-filetime:0000000000005678"),
        ):
            with self.assertRaisesRegex(
                ActiveProjectError, "owner could not be proven inactive"
            ):
                self.service.status()
        self.assertEqual(raw, lock_path.read_bytes())

    def test_invalid_structured_identity_never_authorises_recovery(self) -> None:
        self._pending()
        lock_path = self.service._selection_lock_path
        raw = self._selection_lock_payload(
            process_identity="windows-filetime:not-a-filetime"
        )
        lock_path.write_bytes(raw)
        with mock.patch.object(
            active_projects_module,
            "_process_lock_owner_state",
            return_value=("alive", "windows-filetime:0000000000005678"),
        ):
            with self.assertRaisesRegex(
                ActiveProjectError, "owner could not be proven inactive"
            ):
                self.service.status()
        self.assertEqual(raw, lock_path.read_bytes())

    def test_duplicate_structured_lock_keys_never_authorise_recovery(self) -> None:
        self._pending()
        lock_path = self.service._selection_lock_path
        raw = self._selection_lock_payload().replace(
            b'"ownerPid":424242,', b'"ownerPid":424242,"ownerPid":31337,'
        )
        lock_path.write_bytes(raw)
        with mock.patch.object(
            active_projects_module,
            "_process_lock_owner_state",
            return_value=("dead", ""),
        ):
            with self.assertRaisesRegex(
                ActiveProjectError, "owner could not be proven inactive"
            ):
                self.service.status()
        self.assertEqual(raw, lock_path.read_bytes())

    def test_corrupt_structured_lock_hash_never_authorises_recovery(self) -> None:
        self._pending()
        lock_path = self.service._selection_lock_path
        raw = self._selection_lock_payload().replace(
            b"2026-08-28T00:00:00Z", b"2026-08-29T00:00:00Z"
        )
        lock_path.write_bytes(raw)
        with mock.patch.object(
            active_projects_module,
            "_process_lock_owner_state",
            return_value=("dead", ""),
        ):
            with self.assertRaisesRegex(
                ActiveProjectError, "owner could not be proven inactive"
            ):
                self.service.status()
        self.assertEqual(raw, lock_path.read_bytes())

    def test_legacy_token_selection_lock_is_preserved_even_when_old(self) -> None:
        self._pending()
        lock_path = self.service._selection_lock_path
        raw = ("c" * 32 + "\r\n").encode("ascii")
        lock_path.write_bytes(raw)
        old = time.time() - 7 * 24 * 60 * 60
        os.utime(lock_path, (old, old))
        with self.assertRaisesRegex(
            ActiveProjectError, "owner could not be proven inactive"
        ):
            self.service.status()
        self.assertEqual(raw, lock_path.read_bytes())

    def test_malformed_selection_lock_is_preserved(self) -> None:
        self._pending()
        lock_path = self.service._selection_lock_path
        raw = b'{"schema":"damaged"}\n'
        lock_path.write_bytes(raw)
        with self.assertRaisesRegex(
            ActiveProjectError, "owner could not be proven inactive"
        ):
            self.service.status()
        self.assertEqual(raw, lock_path.read_bytes())

    def test_lock_release_never_removes_a_replaced_owner_payload(self) -> None:
        lock_path = self.service._selection_lock_path
        replacement = self._selection_lock_payload(
            owner_pid=31337,
            process_identity="windows-filetime:0000000000005678",
            token="d" * 32,
        )
        with active_projects_module._exclusive_lock(lock_path, "test selection"):
            lock_path.write_bytes(replacement)
        self.assertEqual(replacement, lock_path.read_bytes())

    def test_atomic_lock_publish_failure_leaves_no_partial_authoritative_lock(
        self,
    ) -> None:
        lock_path = self.service._selection_lock_path
        publish_primitive = "rename" if os.name == "nt" else "link"
        with mock.patch.object(
            active_projects_module.os,
            publish_primitive,
            side_effect=OSError("simulated publish failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated publish failure"):
                with active_projects_module._exclusive_lock(
                    lock_path, "test selection"
                ):
                    self.fail("lock body must not run")
        self.assertFalse(lock_path.exists())
        self.assertEqual(
            [], list(lock_path.parent.glob(".*.pending-*"))
        )

    def test_launcher_project_library_lists_activates_and_tracks_rdc_references(self) -> None:
        captures = self.root / "external-captures"
        captures.mkdir()
        frame_a = captures / "frame-a.rdc"
        frame_b = captures / "frame-b.RDC"
        frame_a.write_bytes(b"rdc-a")
        frame_b.write_bytes(b"rdc-b")
        assets = self.root / "unpacked-assets"
        assets.mkdir()

        created = self.service.create_project(
            name="Example Launcher Project",
            game_id="example-game",
            build_id="shipping-42",
            platform="windows-x64",
            api_family="d3d11",
            asset_root=assets,
            capture_files=[frame_a],
        )
        self.assertEqual("bound", created["state"])
        self.assertEqual(str(assets.resolve()), created["assetRoot"])
        self.assertEqual(CAPTURE_LIBRARY_SCHEMA, created["captureLibrary"]["schema"])
        self.assertEqual(1, created["captureLibrary"]["count"])
        self.assertTrue(created["captureLibrary"]["captures"][0]["available"])
        self.assertEqual(
            "embedded-lazy",
            created["captureLibrary"]["captures"][0]["thumbnail"]["state"],
        )

        updated = self.service.add_capture_files(
            created["projectRoot"], [frame_a, frame_b]
        )
        self.assertEqual(2, updated["captureLibrary"]["count"])
        listed = self.service.list_projects()
        self.assertEqual("replaylab-project-list/v1", listed["schema"])
        self.assertEqual(1, listed["count"])
        self.assertTrue(listed["projects"][0]["active"])
        self.assertEqual(created["workspaceId"], listed["projects"][0]["workspaceId"])

        frame_b.unlink()
        refreshed = self.service.project_status(created["projectRoot"])
        availability = {
            Path(item["path"]).name: item["available"]
            for item in refreshed["captureLibrary"]["captures"]
        }
        self.assertEqual({"frame-a.rdc": True, "frame-b.RDC": False}, availability)

    def test_custom_project_path_registers_and_unregister_keeps_every_file(self) -> None:
        project_root = self.root / "shared-location" / "项目 A"
        capture = self.root / "external.rdc"
        capture.write_bytes(b"portable capture reference")
        with mock.patch(
            "src.active_projects._capture_content_identity",
            side_effect=AssertionError("adding a reference must not hash the RDC"),
        ):
            created = self.service.create_project(
                name="Portable Project",
                game_id="",
                build_id="",
                project_root=project_root,
                capture_files=[capture],
            )
            self.service.add_capture_files(created["projectRoot"], [capture])

        manifest_path = project_root / ".replaylab" / "workspace.json"
        manifest_before = manifest_path.read_bytes()
        removed = self.service.unregister_project(created["projectRoot"])
        self.assertTrue(removed["removed"])
        self.assertFalse(removed["projectFilesDeleted"])
        self.assertTrue(project_root.is_dir())
        self.assertEqual(manifest_before, manifest_path.read_bytes())
        self.assertTrue(capture.is_file())
        self.assertFalse(self.state_path.exists())
        self.assertEqual(0, self.service.list_projects()["count"])

        restored = self.service.register_existing_project(project_root)
        self.assertEqual(created["workspaceId"], restored["workspaceId"])
        self.assertEqual(1, self.service.list_projects()["count"])

    def test_moved_project_stays_removable_and_can_be_registered_at_new_path(self) -> None:
        original_root = self.root / "portable" / "before-move"
        moved_root = self.root / "portable" / "after-move"
        created = self.service.create_project(
            name="Moved Project",
            game_id="",
            build_id="",
            project_root=original_root,
        )
        original_root.rename(moved_root)

        listed = self.service.list_projects()
        self.assertEqual(0, listed["count"])
        self.assertEqual(1, len(listed["rejected"]))
        self.assertEqual(str(original_root.resolve()), listed["rejected"][0]["projectRoot"])
        self.assertEqual(
            "project-validation-failed", listed["rejected"][0]["reasonId"])
        self.assertEqual(
            listed["rejected"][0]["reason"],
            listed["rejected"][0]["reasonDetail"],
        )

        removed = self.service.unregister_project(original_root)
        self.assertFalse(removed["projectFilesDeleted"])
        self.assertTrue(moved_root.is_dir())
        restored = self.service.register_existing_project(moved_root)
        self.assertEqual(created["workspaceId"], restored["workspaceId"])
        self.assertEqual(1, self.service.list_projects()["count"])

    def test_remove_capture_references_supports_missing_files_without_deleting_rdc(self) -> None:
        first = self.root / "first.rdc"
        second = self.root / "second.rdc"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        project = self.service.create_project(
            name="Capture Removal",
            game_id="",
            build_id="",
            capture_files=[first, second],
        )
        records = {
            Path(record["path"]).name: record["captureRefId"]
            for record in project["captureLibrary"]["captures"]
        }
        second.unlink()
        updated = self.service.remove_capture_references(
            project["projectRoot"], [records["first.rdc"], records["second.rdc"]]
        )
        self.assertEqual(0, updated["captureLibrary"]["count"])
        self.assertEqual(b"first", first.read_bytes())
        self.assertFalse(second.exists())

    def test_missing_registry_bootstraps_legacy_projects_once(self) -> None:
        project = self.service.create_pending(
            self.projects_home / "legacy-project", "Legacy Project"
        )
        self.service.registry_path.unlink()
        migrated = ActiveReplayProjectService(self.state_path, self.projects_home)
        listed = migrated.list_projects()
        self.assertEqual(1, listed["count"])
        self.assertEqual(project["workspaceId"], listed["projects"][0]["workspaceId"])
        migrated.unregister_project(project["projectRoot"])
        self.assertEqual(0, migrated.list_projects()["count"])
        self.assertTrue(Path(project["projectRoot"]).is_dir())

    def test_launcher_project_uses_private_workspace_scope_and_validates_rdc(self) -> None:
        pending = self.service.create_project(
            name="Project Scoped Launcher", game_id="", build_id=""
        )
        self.assertEqual("bound", pending["state"])
        self.assertTrue(pending["scopeConfigured"])
        self.assertEqual("replaylab-project", pending["scope"]["gameId"])
        self.assertEqual(pending["workspaceId"], pending["scope"]["buildId"])
        with self.assertRaisesRegex(ActiveProjectError, "both be supplied"):
            self.service.create_project(
                name="Partial Scope", game_id="game", build_id=""
            )
        bad = self.root / "not-a-capture.txt"
        bad.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ActiveProjectError, r"\.rdc"):
            self.service.create_project(
                name="Bad Project",
                game_id="game",
                build_id="build",
                capture_files=[bad],
            )
        self.assertEqual(1, len(list(self.projects_home.iterdir())))

    def test_capture_auto_match_uses_one_unambiguous_registered_path(self) -> None:
        capture = self.root / "replaceable.rdc"
        capture.write_bytes(b"capture-one")
        first = self.service.create_project(
            name="First Game", game_id="game-a", build_id="build-a",
            capture_files=[capture],
        )
        second = self.service.create_project(
            name="Second Game", game_id="game-b", build_id="build-b"
        )

        matched = self.service.match_and_activate_capture(capture)
        self.assertTrue(matched["activated"])
        self.assertFalse(matched["contentIdentityCacheHit"])
        self.assertEqual("unique-normalised-path", matched["associationMode"])
        self.assertEqual(first["workspaceId"], self.service.status()["workspaceId"])

        old_stat = capture.stat()
        capture.write_bytes(b"capture-two")
        os.utime(
            capture,
            ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1000000000),
        )
        self.service.activate(second["projectRoot"])
        replaced = self.service.match_and_activate_capture(capture)
        self.assertTrue(replaced["activated"])
        self.assertEqual(1, replaced["matchCount"])
        self.assertEqual(first["workspaceId"], self.service.status()["workspaceId"])

        self.service.activate(second["projectRoot"])
        expired = self.service.match_and_activate_capture(
            capture, activation_guard=lambda: False
        )
        self.assertEqual(1, expired["matchCount"])
        self.assertFalse(expired["activated"])
        self.assertEqual(second["workspaceId"], self.service.status()["workspaceId"])

        self.service.add_capture_files(second["projectRoot"], [capture])
        self.service.activate(second["projectRoot"])
        ambiguous = self.service.match_and_activate_capture(capture)
        self.assertFalse(ambiguous["activated"])
        self.assertEqual(2, ambiguous["matchCount"])
        self.assertEqual(second["workspaceId"], self.service.status()["workspaceId"])

    def test_legacy_path_only_capture_is_compatible_with_path_association(self) -> None:
        capture = self.root / "legacy.rdc"
        capture.write_bytes(b"legacy")
        project = self.service.create_project(
            name="Legacy", game_id="game", build_id="build",
            capture_files=[capture],
        )
        library_path = Path(project["projectRoot"]) / ".replaylab" / "capture-library.json"
        library = json.loads(library_path.read_text(encoding="utf-8"))
        library["schemaVersion"] = 1
        library["captures"][0].pop("contentIdentity")
        library["librarySha256"] = _canonical_hash(library, "librarySha256")
        library_path.write_text(json.dumps(library), encoding="utf-8")

        self.service.create_project(
            name="Other", game_id="other", build_id="other"
        )
        result = self.service.match_and_activate_capture(capture)
        self.assertTrue(result["activated"])
        self.assertEqual(1, result["matchCount"])
        self.assertEqual(project["workspaceId"], self.service.status()["workspaceId"])
        upgraded = self.service.add_capture_files(project["projectRoot"], [capture])
        self.assertEqual(2, upgraded["captureLibrary"]["schemaVersion"])
        self.assertIsNone(
            upgraded["captureLibrary"]["captures"][0]["contentIdentity"]
        )
        self.assertTrue(self.service.match_and_activate_capture(capture)["activated"])

    def test_create_project_rolls_back_publication_and_active_selection(self) -> None:
        existing = self.service.create_project(
            name="Existing", game_id="game", build_id="build"
        )
        before_directories = {item.name for item in self.projects_home.iterdir()}
        original_write = self.service._write_selection

        def fail_new_selection(root: Path, workspace_id: str) -> None:
            if workspace_id != existing["workspaceId"]:
                raise ActiveProjectError("simulated selection publish failure")
            original_write(root, workspace_id)

        with mock.patch.object(
            self.service, "_write_selection", side_effect=fail_new_selection
        ):
            with self.assertRaisesRegex(ActiveProjectError, "simulated"):
                self.service.create_project(
                    name="Must Roll Back", game_id="new-game", build_id="new-build"
                )
        self.assertEqual(
            before_directories, {item.name for item in self.projects_home.iterdir()}
        )
        self.assertEqual(existing["workspaceId"], self.service.status()["workspaceId"])

        original_status = self.service._project_status

        def fail_after_selection(project_root: str | Path):
            if Path(project_root).name.startswith("late-failure"):
                raise ActiveProjectError("simulated post-selection validation failure")
            return original_status(project_root)

        with mock.patch.object(
            self.service, "_project_status", side_effect=fail_after_selection
        ):
            with self.assertRaisesRegex(ActiveProjectError, "post-selection"):
                self.service.create_project(
                    name="Late Failure", game_id="new-game", build_id="new-build"
                )
        self.assertEqual(
            before_directories, {item.name for item in self.projects_home.iterdir()}
        )
        self.assertEqual(existing["workspaceId"], self.service.status()["workspaceId"])

    def test_failed_create_quarantine_is_never_listed_if_cleanup_fails(self) -> None:
        existing = self.service.create_project(
            name="Existing Visible", game_id="game", build_id="build"
        )
        original_status = self.service._project_status
        original_rmtree = shutil.rmtree

        def fail_after_selection(project_root: str | Path):
            if Path(project_root).name.startswith("quarantine-me"):
                raise ActiveProjectError("simulated final validation failure")
            return original_status(project_root)

        def leave_quarantine(path, *args, **kwargs):
            if ".failed-" in Path(path).name:
                return None
            return original_rmtree(path, *args, **kwargs)

        with mock.patch.object(
            self.service, "_project_status", side_effect=fail_after_selection
        ), mock.patch("src.active_projects.shutil.rmtree", side_effect=leave_quarantine):
            with self.assertRaisesRegex(ActiveProjectError, "final validation"):
                self.service.create_project(
                    name="Quarantine Me", game_id="new", build_id="new"
                )
        hidden = [
            item for item in self.projects_home.iterdir()
            if item.name.startswith(".quarantine-me.failed-")
        ]
        self.assertEqual(1, len(hidden))
        listed = self.service.list_projects()
        self.assertEqual(1, listed["count"])
        self.assertEqual(existing["workspaceId"], listed["projects"][0]["workspaceId"])
        original_rmtree(hidden[0])

    def test_project_settings_serialise_with_pinned_active_operation(self) -> None:
        active = self.service.create_project(
            name="Pending Settings", game_id="", build_id=""
        )
        assets = self.root / "serial-assets"
        assets.mkdir()
        started = threading.Event()
        finished = threading.Event()

        def configure() -> None:
            started.set()
            self.service.configure_project_settings(
                active["projectRoot"], asset_root=assets
            )
            finished.set()

        worker = threading.Thread(target=configure, daemon=True)
        with self.service.pin_active_project(
            active["workspaceId"], active["projectRoot"]
        ):
            worker.start()
            self.assertTrue(started.wait(1.0))
            self.assertFalse(finished.wait(0.1))
            self.assertIsNone(self.service.status()["assetRoot"])
        self.assertTrue(finished.wait(2.0))
        worker.join(timeout=1.0)
        self.assertEqual(str(assets.resolve()), self.service.status()["assetRoot"])

    def test_explicit_project_settings_are_atomic_and_never_switch_active(self) -> None:
        first = self.service.create_project(
            name="First", game_id="game-a", build_id="build-a"
        )
        second = self.service.create_pending(
            self.projects_home / "second-pending", "Second"
        )
        self.service.activate(first["projectRoot"])
        missing_assets = self.root / "missing-assets"

        with self.assertRaisesRegex(ActiveProjectError, "asset root"):
            self.service.configure_project_settings(
                second["projectRoot"],
                game_id="game-b",
                build_id="build-b",
                asset_root=missing_assets,
            )
        self.assertEqual(first["workspaceId"], self.service.status()["workspaceId"])
        unchanged = self.service.project_status(second["projectRoot"])
        self.assertEqual("pending-scope", unchanged["state"])
        self.assertFalse(Path(unchanged["paths"]["replayProject"]).exists())

        assets = self.root / "second-assets"
        assets.mkdir()
        configured = self.service.configure_project_settings(
            second["projectRoot"],
            game_id="game-b",
            build_id="build-b",
            platform="windows-x64",
            api_family="d3d11",
            asset_root=assets,
        )
        self.assertEqual("bound", configured["state"])
        self.assertEqual(str(assets.resolve()), configured["assetRoot"])
        self.assertEqual(first["workspaceId"], self.service.status()["workspaceId"])

        with self.assertRaisesRegex(ActiveProjectError, "scope is immutable"):
            self.service.configure_project_settings(
                second["projectRoot"],
                game_id="game-b",
                build_id="different-build",
            )
        self.assertEqual(first["workspaceId"], self.service.status()["workspaceId"])

    def test_project_pin_blocks_activation_until_operation_finishes(self) -> None:
        first = self._pending()
        second = self.service.create_pending(
            self.projects_home / "second-project", "Second Project"
        )
        self.service.activate(first["projectRoot"])
        started = threading.Event()
        finished = threading.Event()

        def switch_project() -> None:
            started.set()
            self.service.activate(second["projectRoot"])
            finished.set()

        worker = threading.Thread(target=switch_project, daemon=True)
        with self.service.pin_active_project(
            first["workspaceId"], first["projectRoot"]
        ):
            worker.start()
            self.assertTrue(started.wait(1.0))
            self.assertFalse(finished.wait(0.1))
            self.assertEqual(self.service.status()["workspaceId"], first["workspaceId"])
        self.assertTrue(finished.wait(2.0))
        worker.join(timeout=1.0)
        self.assertEqual(self.service.status()["workspaceId"], second["workspaceId"])

    def test_pending_project_blocks_physical_reuse_but_can_stage_catalog(self) -> None:
        self._pending()
        catalog, unused_shader, capture_id = self._catalog(
            "pending-frame", _dxbc(), resource_id=1, include_scope=False
        )
        staged = self.service.stage_capture_catalog(catalog)
        self.assertEqual(staged["captureId"], capture_id)
        self.assertFalse(staged["physicalKnowledgeImportAllowed"])
        self.assertTrue(Path(staged["catalogPath"]).is_file())

        plan = self.service.future_frame_plan()
        self.assertEqual(plan["schema"], FUTURE_FRAME_PLAN_SCHEMA)
        self.assertTrue(plan["blocked"])
        self.assertEqual(plan["preparationSteps"], [])
        with self.assertRaisesRegex(ActiveProjectError, "scope is not configured"):
            self.service.import_capture(catalog)

    def test_staging_enforces_expected_live_catalog_binding_before_publish(self) -> None:
        self._pending()
        cases = (
            {
                "label": "wrong-id",
                "expected_capture_id": "0" * 64,
                "expected_capture_path": None,
                "expected_api": None,
                "message": "expected exported identity",
            },
            {
                "label": "wrong-path",
                "expected_capture_id": None,
                "expected_capture_path": str(self.root / "other.rdc"),
                "expected_api": None,
                "message": "path differs",
            },
            {
                "label": "wrong-api",
                "expected_capture_id": None,
                "expected_capture_path": None,
                "expected_api": "Vulkan",
                "message": "API differs",
            },
        )
        for index, case in enumerate(cases, start=1):
            with self.subTest(case=case["label"]):
                catalog, unused_shader, capture_id = self._catalog(
                    str(case["label"]), _dxbc(bytes([index])), resource_id=index
                )
                kwargs = {
                    key: case[key]
                    for key in (
                        "expected_capture_id",
                        "expected_capture_path",
                        "expected_api",
                    )
                    if case[key] is not None
                }
                with self.assertRaisesRegex(ActiveProjectError, str(case["message"])):
                    self.service.stage_capture_catalog(catalog, **kwargs)
                self.assertFalse(
                    self.service.capture_catalog_path(capture_id).parent.exists()
                )

    def test_scope_binding_is_immutable_and_wraps_replay_project(self) -> None:
        bound = self._bound()
        self.assertEqual(bound["state"], "bound")
        self.assertEqual(bound["scope"]["gameId"], "example-game")
        self.assertEqual(bound["scope"]["apiFamily"], "d3d11")
        replay_root = Path(bound["paths"]["replayProject"])
        self.assertTrue((replay_root / "project.json").is_file())
        self.assertEqual(self.service.replay_service().status()["scope"], bound["scope"])

        idempotent = self.service.configure_scope(
            game_id="example-game",
            build_id="build-1",
            platform="windows-x64",
            api_family="Direct3D 11",
        )
        self.assertEqual(idempotent["workspaceId"], bound["workspaceId"])
        with self.assertRaisesRegex(ActiveProjectError, "scope is immutable"):
            self.service.configure_scope(
                game_id="another-game",
                build_id="build-1",
                platform="windows-x64",
                api_family="d3d11",
            )

    def test_capture_import_is_content_addressed_and_api_can_be_pinned(self) -> None:
        self._bound(api_family="")
        catalog, unused_shader, capture_id = self._catalog(
            "frame-a", _dxbc(), resource_id=11
        )
        first = self.service.import_capture(catalog)
        staged_path = Path(first["stagedCatalog"]["catalogPath"])
        staged_value = json.loads(staged_path.read_text(encoding="utf-8"))
        staged_blob = staged_path.parent / staged_value["shaders"][0]["rawPath"]
        self.assertTrue(staged_blob.is_file())
        self.assertTrue(staged_blob.resolve().is_relative_to(staged_path.parent.resolve()))
        self.assertEqual(first["reusePlan"]["captureId"], capture_id)
        self.assertTrue(first["physicalKnowledgeImported"])
        self.assertFalse(first["autoApplied"])
        self.assertEqual(self.service.status()["scope"]["apiFamily"], "d3d11")

        second = self.service.import_capture(catalog)
        self.assertTrue(second["stagedCatalog"]["idempotent"])
        self.assertTrue(second["reusePlan"]["idempotent"])

    def test_same_capture_id_preserves_distinct_catalog_revisions(self) -> None:
        self._pending()
        catalog, unused_shader, capture_id = self._catalog(
            "catalog-revisions", _dxbc(), resource_id=111
        )

        first = self.service.stage_capture_catalog(catalog)
        first_path = Path(first["catalogPath"])
        first_bytes = first_path.read_bytes()

        changed = json.loads(catalog.read_text(encoding="utf-8"))
        # captureId deliberately excludes reflection/use/diagnostic payloads.
        # A corrected exporter may therefore produce another exact catalog
        # revision for the same Shader-set identity.
        changed["exportDiagnostics"] = {"reflectionRevision": 2}
        catalog.write_text(json.dumps(changed), encoding="utf-8")

        second = self.service.stage_capture_catalog(catalog)
        second_path = Path(second["catalogPath"])
        self.assertEqual(second["captureId"], capture_id)
        self.assertNotEqual(first["catalogSha256"], second["catalogSha256"])
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(first_path.is_file())
        self.assertTrue(second_path.is_file())
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(
            second["catalogRevisionSha256"], second["catalogSha256"]
        )
        self.assertEqual(second_path.parent.name, second["catalogSha256"])

        repeated = self.service.stage_capture_catalog(catalog)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["catalogPath"], second["catalogPath"])

    def test_pending_catalog_revision_limit_fails_before_publish(self) -> None:
        self._pending()
        catalog, unused_shader, capture_id = self._catalog(
            "catalog-revision-limit", _dxbc(), resource_id=112
        )
        first = self.service.stage_capture_catalog(catalog)
        changed = json.loads(catalog.read_text(encoding="utf-8"))
        changed["exportDiagnostics"] = {"reflectionRevision": 2}
        catalog.write_text(json.dumps(changed), encoding="utf-8")

        with mock.patch(
            "src.active_projects.MAX_STAGED_CATALOG_REVISIONS", 1
        ):
            with self.assertRaisesRegex(ActiveProjectError, "revision limit"):
                self.service.stage_capture_catalog(catalog)

        expected_payload = json.loads(catalog.read_text(encoding="utf-8"))
        self.assertEqual(expected_payload["captureId"], capture_id)
        self.assertTrue(Path(first["catalogPath"]).is_file())

    def test_first_capture_scope_pin_recovers_after_snapshot_update_crash(self) -> None:
        bound = self._bound(api_family="")
        catalog, unused_shader, unused_capture = self._catalog(
            "scope-recovery", _dxbc(), resource_id=12
        )
        staged = self.service.stage_capture_catalog(catalog)

        # Simulate a process stopping after the authoritative replay project
        # imported the capture, but before the active workspace copied the API
        # refinement into its portable configuration snapshot.
        from src.projects import ReplayProjectService

        ReplayProjectService(bound["paths"]["replayProject"]).import_capture(
            staged["catalogPath"]
        )
        self.assertEqual(self.service.status()["scope"]["apiFamily"], "")

        recovered = self.service.import_capture(catalog)
        self.assertTrue(recovered["reusePlan"]["idempotent"])
        self.assertEqual(self.service.status()["scope"]["apiFamily"], "d3d11")

    def test_deterministic_paths_preserve_distinct_shader_resource_ids(self) -> None:
        status = self._pending()
        capture = "1" * 64
        raw = "2" * 64
        path_a = self.service.shader_workspace_path(
            capture_id=capture,
            stage="Pixel Shader",
            shader_resource_id=10,
            raw_sha256=raw,
            create=True,
        )
        path_b = self.service.shader_workspace_path(
            capture_id=capture,
            stage="Pixel Shader",
            shader_resource_id=20,
            raw_sha256=raw,
            create=True,
        )
        self.assertNotEqual(path_a, path_b)
        self.assertTrue(path_a.is_dir())
        self.assertTrue(path_b.is_dir())
        revision_a = self.service.shader_workspace_path(
            capture_id=capture,
            catalog_sha256="3" * 64,
            stage="Pixel Shader",
            shader_resource_id=10,
            raw_sha256=raw,
        )
        revision_b = self.service.shader_workspace_path(
            capture_id=capture,
            catalog_sha256="4" * 64,
            stage="Pixel Shader",
            shader_resource_id=10,
            raw_sha256=raw,
        )
        self.assertNotEqual(revision_a, revision_b)
        self.assertEqual(revision_a.parent.name, "v2")
        self.assertEqual(revision_b.parent.name, "v2")
        self.assertRegex(revision_a.name, r"^[0-9a-f]{64}$")
        self.assertRegex(revision_b.name, r"^[0-9a-f]{64}$")
        route_a = self.service.shader_workspace_path(
            capture_id=capture,
            catalog_sha256="3" * 64,
            route_binding_sha256="5" * 64,
            stage="Pixel Shader",
            shader_resource_id=10,
            raw_sha256=raw,
        )
        route_b = self.service.shader_workspace_path(
            capture_id=capture,
            catalog_sha256="3" * 64,
            route_binding_sha256="6" * 64,
            stage="Pixel Shader",
            shader_resource_id=10,
            raw_sha256=raw,
        )
        self.assertNotEqual(route_a, route_b)
        self.assertEqual(route_a.parent.name, "v2")
        self.assertEqual(route_b.parent.name, "v2")
        self.assertRegex(route_a.name, r"^[0-9a-f]{64}$")
        self.assertRegex(route_b.name, r"^[0-9a-f]{64}$")
        if os.name == "nt":
            longest_fixed_job_output = (
                route_a
                / "jobs"
                / ("decompile-h99-r9999-" + ("f" * 32))
                / "output.dxil.ll"
            )
            self.assertLess(len(str(longest_fixed_job_output)), 248)
        self.assertEqual(
            route_a,
            self.service.shader_workspace_path(
                capture_id=capture,
                catalog_sha256="3" * 64,
                route_binding_sha256="5" * 64,
                stage="Pixel Shader",
                shader_resource_id=10,
                raw_sha256=raw,
            ),
        )
        self.assertTrue(
            path_a.resolve().is_relative_to(
                Path(status["paths"]["shaderWorkspaces"]).resolve()
            )
        )
        self.assertEqual(
            self.service.shared_dictionary_directory("aliases"),
            self.service.shared_dictionary_directory("aliases"),
        )
        with self.assertRaisesRegex(ActiveProjectError, "unknown shared"):
            self.service.shared_dictionary_directory("../outside")

    def test_asset_root_is_explicit_read_only_and_boundary_checked(self) -> None:
        status = self._pending()
        asset_root = self.root / "unpacked-game"
        (asset_root / "Shaders").mkdir(parents=True)
        asset_file = asset_root / "Shaders" / "base.bin"
        asset_file.write_bytes(b"asset")

        configured = self.service.configure_asset_root(asset_root)
        self.assertEqual(Path(configured["assetRoot"]), asset_root.resolve())
        self.assertEqual(
            self.service.resolve_asset_path("Shaders/base.bin"), asset_file.resolve()
        )
        with self.assertRaisesRegex(ActiveProjectError, "safe relative"):
            self.service.resolve_asset_path("../outside.bin")
        with self.assertRaisesRegex(ActiveProjectError, "must not contain"):
            self.service.configure_asset_root(self.root)
        self.assertTrue(
            Path(status["projectRoot"]).resolve().is_relative_to(
                self.projects_home.resolve()
            )
        )

    def test_active_selection_and_workspace_hashes_detect_tampering(self) -> None:
        status = self._pending()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["projectRoot"] = str(self.root / "outside")
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ActiveProjectError, "selection hash mismatch"):
            self.service.status()

        # Restore a valid selection, then tamper with the portable manifest.
        self.service.activate(status["projectRoot"])
        manifest_path = Path(status["paths"]["workspaceManifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["name"] = "tampered"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ActiveProjectError, "manifest hash mismatch"):
            self.service.status()

    def test_rehashed_layout_escape_is_still_rejected(self) -> None:
        status = self._pending()
        manifest_path = Path(status["paths"]["workspaceManifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["layout"]["sharedDictionaries"] = "../../outside"
        manifest["configSha256"] = _canonical_hash(manifest, "configSha256")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ActiveProjectError, "layout is invalid"):
            self.service.status()

    def test_external_project_must_be_registered_before_activation(self) -> None:
        other_home = self.root / "other-projects"
        other = ActiveReplayProjectService(
            self.root / "other-state.json", other_home
        )
        outside = other.create_pending(other_home / "outside")
        with self.assertRaisesRegex(ActiveProjectError, "not registered"):
            self.service.activate(outside["projectRoot"])
        registered = self.service.register_existing_project(outside["projectRoot"])
        activated = self.service.activate(registered["projectRoot"])
        self.assertEqual(outside["workspaceId"], activated["workspaceId"])

    def test_activate_validates_each_project_snapshot_once(self) -> None:
        from src.projects import ReplayProjectService

        project = self._bound()
        original_read_workspace = self.service._read_workspace
        original_replay_status = ReplayProjectService.status
        calls = {"workspace": 0, "replayStatus": 0}

        def read_workspace_once(*args, **kwargs):
            calls["workspace"] += 1
            return original_read_workspace(*args, **kwargs)

        def replay_status_once(instance, *args, **kwargs):
            calls["replayStatus"] += 1
            return original_replay_status(instance, *args, **kwargs)

        with mock.patch.object(
            self.service, "_read_workspace", side_effect=read_workspace_once
        ), mock.patch.object(
            ReplayProjectService,
            "status",
            autospec=True,
            side_effect=replay_status_once,
        ):
            activated = self.service.activate(project["projectRoot"])

        self.assertEqual({"workspace": 1, "replayStatus": 1}, calls)
        self.assertEqual(project["workspaceId"], activated["workspaceId"])
        self.assertIn("captureLibrary", activated)
        self.assertIsNotNone(activated["replayProject"])

    def test_future_frame_plan_includes_verified_candidates_and_flowchart(self) -> None:
        self._bound()
        blob_a = _dxbc(b"same program", b"frame-a reflection")
        blob_b = _dxbc(b"same program", b"frame-b reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "reuse-a", blob_a, resource_id=101
        )
        catalog_b, unused_shader_b, capture_b = self._catalog(
            "reuse-b", blob_b, resource_id=909
        )
        self.service.import_capture(catalog_a)
        workspace_status = self._workspace_status(
            "workspace-a", capture_a, shader_a
        )
        self._with_fully_validated_candidate(
            workspace_status, _dxbc(b"validated edited program")
        )
        registered = self.service.register_workspace(workspace_status)
        self.assertEqual(len(registered["flowchartArtifacts"]), 1)
        self.assertFalse(
            registered["flowchartArtifacts"][0]["policy"]["physicalEvidence"]
        )

        alias_path = self.root / "verified-aliases.json"
        alias_path.write_text(
            json.dumps(
                {
                    "schema": ALIAS_SCHEMA,
                    "schemaVersion": ALIAS_VERSION,
                    "verifiedOnly": True,
                    "captureId": capture_a,
                    "aliases": [
                        {
                            "verified": True,
                            "captureId": capture_a,
                            "shaderResourceId": 101,
                            "stage": "fragment",
                            "entryPoint": "main",
                            "rawSha256": shader_a["rawSha256"],
                            "codeSha256": shader_a["codeSha256"],
                            "bindingKind": "constantBufferVariable",
                            "registerSpace": 0,
                            "bindNumber": 0,
                            "byteOffset": 0,
                            "canonicalPath": "cb0._child0",
                            "alias": "BaseColor",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.service.register_alias_table(alias_path)
        self.service.save_material_knowledge(
            {
                "schema": "renderdoc-ultra-material-session/v1",
                "captureId": capture_a,
                "captureSessionId": "b" * 32,
                "shaderResourceId": 101,
                "stage": "fragment",
                "constantBlockPath": "cb0",
                "layoutSha256": "c" * 64,
                "parameters": [
                    {
                        "parameterId": "d" * 32,
                        "path": "cb0.BaseColor",
                        "baseType": "Float",
                        "components": 4,
                        "value": [1.0, 0.5, 0.25, 1.0],
                    }
                ],
            },
            ["d" * 32],
        )

        imported = self.service.import_capture(catalog_b)
        plan = imported["reusePlan"]
        self.assertEqual(plan["matchedShaderCount"], 1)
        match = plan["matches"][0]
        source = match["workspaceCandidates"][0]
        self.assertTrue(source["sourceSeedEligible"])
        self.assertTrue(source["binaryCandidateEligible"])
        self.assertFalse(source["compileRequired"])
        self.assertTrue(source["freshCompileMayBeSkipped"])
        self.assertTrue(source["freshCaptureRebindRequired"])
        self.assertTrue(source["liveAbiRevalidationRequired"])
        self.assertEqual(len(source["flowchartCandidates"]), 1)
        flowchart = source["flowchartCandidates"][0]
        self.assertTrue(Path(flowchart["path"]).is_file())
        self.assertTrue(flowchart["explanatoryOnly"])
        self.assertFalse(flowchart["physicalEvidence"])
        self.assertFalse(flowchart["applyAuthorization"])
        self.assertEqual(len(match["aliasCandidates"]), 1)
        self.assertEqual(len(match["materialCandidates"]), 1)
        kinds = {item["kind"] for item in plan["preparationSteps"]}
        self.assertEqual(
            kinds,
            {
                "seed-reviewed-hlsl",
                "load-verified-compiled-shader-candidate",
                "load-verified-alias-candidate",
                "list-material-value-candidate",
            },
        )
        binary_step = next(
            item
            for item in plan["preparationSteps"]
            if item["kind"] == "load-verified-compiled-shader-candidate"
        )
        self.assertTrue(Path(binary_step["artifactPath"]).is_file())
        self.assertEqual(
            binary_step["compilerBindingSha256"],
            workspace_status["compilerBinding"]["bindingSha256"],
        )
        self.assertTrue(binary_step["freshCompileMayBeSkipped"])
        self.assertTrue(binary_step["requiresFreshCaptureRebind"])
        self.assertTrue(binary_step["requiresFreshLiveAbiValidation"])
        self.assertFalse(binary_step["oldReplacementIdReused"])
        self.assertFalse(binary_step["oldCaptureAbiAuthorityReused"])
        self.assertFalse(plan["activeProjectPolicy"]["physicalMutationAutoApply"])
        self.assertFalse(plan["activeProjectPolicy"]["aiProvisionalAutoApply"])
        self.assertTrue(
            plan["activeProjectPolicy"]["verifiedBinaryCandidateMaySkipCompile"]
        )

        fetched = self.service.future_frame_plan(capture_b)
        self.assertFalse(fetched["blocked"])
        self.assertEqual(len(fetched["preparationSteps"]), 4)




    def test_reviewed_shader_flow_graph_is_content_addressed_by_knowledge(self) -> None:
        self._bound()
        blob_a = _dxbc(b"same graph program", b"frame-a graph reflection")
        blob_b = _dxbc(b"same graph program", b"frame-b graph reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "graph-a", blob_a, resource_id=131
        )
        catalog_b, unused_shader_b, capture_b = self._catalog(
            "graph-b", blob_b, resource_id=913
        )
        self.service.import_capture(catalog_a)
        status = self._workspace_status("workspace-graph", capture_a, shader_a)
        self._with_fully_validated_candidate(
            status, _dxbc(b"validated graph program")
        )
        source_path = Path(status["source"]["path"])
        source = source_path.read_bytes().decode("utf-8")
        graph = build_shader_flow_graph(
            source,
            workspace_id=status["workspaceId"],
            source_path="source/shader.hlsl",
            source_sha256=status["source"]["currentSha256"],
            source_revision=status["revision"],
            stage=status["stage"],
            entry_point=status["entryPoint"],
            generated_at="2026-08-31T00:00:00Z",
            origin="organized-proposal",
            descriptions={"main": "Produces the final render-target color."},
        )
        graph_path = Path(status["workspacePath"]) / "ai" / "flow-graph.json"
        graph_path.write_text(json.dumps(graph), encoding="utf-8")
        graph_sha256 = hashlib.sha256(graph_path.read_bytes()).hexdigest()
        status["flowGraph"] = {
            **graph,
            "available": True,
            "status": "current",
            "stale": False,
            "artifactPath": str(graph_path),
            "artifactSha256": graph_sha256,
        }
        (graph_path.parent / "organize-code-gate.json").write_text(
            json.dumps(
                {
                    "schema": "renderdoc-ai-shader-organization-gate/v1",
                    "schemaVersion": 1,
                    "requestedTransformation": "organize-only",
                    "decision": "accepted-executable-editable",
                    "safeToStartLiveEdit": True,
                    "replayApplied": False,
                    "workspaceId": status["workspaceId"],
                    "finalSourceSha256": status["source"]["currentSha256"],
                    "finalRevision": status["revision"],
                    "flowGraphId": graph["graphId"],
                    "flowGraphSha256": graph_sha256,
                    "flowGraphArtifactPath": str(graph_path),
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            active_projects_module,
            "read_json_object",
            side_effect=AssertionError(
                "flow graph publication must validate its published bytes"),
        ):
            prepared = self.service._read_shader_flow_graph(status)
        self.assertEqual(prepared[0], graph_path.read_bytes())
        self.assertEqual(prepared[1], graph_sha256)
        gate_path = graph_path.parent / "organize-code-gate.json"
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        gate["flowGraphSha256"] = "0" * 64
        gate_path.write_text(json.dumps(gate), encoding="utf-8")
        # The graph is bound to current source bytes/revision directly; a stale
        # organization sidecar is not publication authority.
        self.assertEqual(self.service._read_shader_flow_graph(status), prepared)

        registered = self.service.register_workspace(status)

        self.assertEqual(len(registered["flowGraphArtifacts"]), 1)
        record = registered["flowGraphArtifacts"][0]
        self.assertEqual(record["kind"], "shader-flow-graph")
        self.assertTrue(record["policy"]["explanatoryOnly"])
        self.assertFalse(record["policy"]["physicalEvidence"])
        project_root = Path(self.service.status()["projectRoot"])
        published_graph = project_root / record["artifactPath"]
        self.assertEqual(published_graph.read_bytes(), graph_path.read_bytes())

        imported = self.service.import_capture(catalog_b)
        candidate = imported["reusePlan"]["matches"][0][
            "workspaceCandidates"
        ][0]
        self.assertEqual(len(candidate["flowGraphCandidates"]), 1)
        projected = candidate["flowGraphCandidates"][0]
        self.assertEqual(projected["kind"], "shader-flow-graph")
        self.assertEqual(Path(projected["path"]), published_graph)
        self.assertEqual(projected["sha256"], record["artifactSha256"])
        self.assertFalse(projected["applyAuthorization"])

    def test_applied_user_edit_publishes_current_source_without_old_ai_gate(self) -> None:
        from src import project_restore, server

        self._bound()
        original = _dxbc(b"editable original", b"origin reflection")
        catalog, shader, capture_id = self._catalog(
            "applied-edit-origin", original, resource_id=181
        )
        self.service.import_capture(catalog)
        status = self._workspace_status("applied-edit", capture_id, shader)
        self._with_fully_validated_candidate(status, _dxbc(b"reviewed program"))
        first = self.service.register_workspace(status)
        old_knowledge = first["knowledge"]
        workspace = Path(status["workspacePath"])
        source_path = Path(status["source"]["path"])
        edited_source = b"float4 main() : SV_Target { return float4(0, 1, 0, 1); }\n"
        source_path.write_bytes(edited_source)
        status["source"]["currentSha256"] = hashlib.sha256(edited_source).hexdigest()
        self._with_fully_validated_candidate(
            status, _dxbc(b"deliberately changed user program"), revision=6
        )
        # These files are deliberately stale: user editing must neither read
        # their old organization authorization nor attach them to the new head.
        graph_path = workspace / "ai" / "flow-graph.json"
        graph_path.write_text('{"stale": true}', encoding="utf-8")
        (workspace / "ai" / "organize-code-gate.json").write_text(
            '{"stale": true}', encoding="utf-8"
        )
        watch_lock = workspace / ".workspace-watch.lock"
        watch_lock.write_text("a" * 32 + "\n", encoding="utf-8")

        class WorkspaceService:
            def status(self, unused_path):
                return copy.deepcopy(status)

        active = self.service.status()
        with self.service.pin_active_project(active["workspaceId"], active["projectRoot"]):
            with mock.patch.object(
                server, "_shader_organization_manager",
                side_effect=AssertionError("user edit must not repeat AI organization"),
            ), mock.patch.object(server, "_workspace_active_capture_context", return_value={}), \
                 mock.patch.object(server, "_ensure_active_workspace_project_capture") as ensure_catalog, \
                 mock.patch.object(server, "_require_same_active_capture"):
                published = project_restore._register_applied_workspace_with_readback(
                    self.service, WorkspaceService(), workspace, copy.deepcopy(status)
                )
                ensure_catalog.assert_called_once_with(self.service, status, {})
        knowledge = published["knowledge"]
        self.assertNotEqual(knowledge["knowledgeId"], old_knowledge["knowledgeId"])
        self.assertEqual(knowledge["sourceSha256"], hashlib.sha256(edited_source).hexdigest())
        self.assertEqual(knowledge["compiledCandidate"]["sourceRevision"], 6)
        self.assertFalse(knowledge.get("analysisSha256"))
        self.assertEqual(published["flowchartArtifacts"], [])
        self.assertEqual(published["flowGraphArtifacts"], [])
        self.assertTrue(watch_lock.is_file())
        self.assertTrue(graph_path.is_file())
        self.assertEqual(self.service.get_workspace_knowledge(old_knowledge["knowledgeId"]), old_knowledge)

        other_catalog, unused_shader, unused_capture = self._catalog(
            "applied-edit-other-rdc",
            _dxbc(b"editable original", b"different capture reflection"),
            resource_id=9181,
        )
        plan = self.service.import_capture(other_catalog)["reusePlan"]
        candidate = plan["matches"][0]["workspaceCandidates"][0]
        self.assertEqual(candidate["knowledgeId"], knowledge["knowledgeId"])
        self.assertEqual(candidate["sourceSha256"], knowledge["sourceSha256"])
        self.assertTrue(candidate["freshCaptureRebindRequired"])
        self.assertTrue(candidate["liveAbiRevalidationRequired"])
        self.assertEqual(candidate["flowchartCandidates"], [])
        self.assertEqual(candidate["flowGraphCandidates"], [])

    def test_applied_publication_rejects_changed_source_before_project_write(self) -> None:
        self._bound()
        catalog, shader, capture_id = self._catalog(
            "applied-race", _dxbc(b"race baseline"), resource_id=182
        )
        self.service.import_capture(catalog)
        status = self._workspace_status("applied-race-workspace", capture_id, shader)
        self._with_fully_validated_candidate(status, _dxbc(b"race validated program"))
        first = self.service.register_workspace(status)
        Path(status["source"]["path"]).write_text(
            "// a newer save that has never been compiled\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ActiveProjectError, "current source"):
            self.service.register_workspace(status, include_explanatory=False)
        self.assertEqual(
            self.service.replay_service().status()["knowledgeCounts"]["workspaces"], 1
        )
        self.assertEqual(
            self.service.get_workspace_knowledge(first["knowledge"]["knowledgeId"]),
            first["knowledge"],
        )

    def test_applied_publication_requires_abi_candidate_and_preserves_head_cas(self) -> None:
        self._bound()
        catalog, shader, capture_id = self._catalog(
            "applied-cas", _dxbc(b"cas baseline"), resource_id=183
        )
        self.service.import_capture(catalog)
        status = self._workspace_status("applied-cas-workspace", capture_id, shader)
        with self.assertRaisesRegex(ActiveProjectError, "ABI-validated"):
            self.service.register_workspace(status, include_explanatory=False)
        self._with_fully_validated_candidate(status, _dxbc(b"cas first program"))
        first = self.service.register_workspace(status)
        Path(status["source"]["path"]).write_text(
            "float4 main() : SV_Target { return 0; }\n", encoding="utf-8"
        )
        status["source"]["currentSha256"] = hashlib.sha256(
            Path(status["source"]["path"]).read_bytes()
        ).hexdigest()
        self._with_fully_validated_candidate(status, _dxbc(b"cas second program"), revision=6)
        second = self.service.register_workspace(
            status, include_explanatory=False, expected_head=first["head"]
        )
        self.assertGreater(second["head"]["generation"], first["head"]["generation"])
        with self.assertRaisesRegex(ActiveProjectError, "compare-and-swap conflict"):
            self.service.register_workspace(
                status, include_explanatory=False, expected_head=first["head"]
            )

    def test_shader_flow_graph_must_bind_current_workspace_source(self) -> None:
        self._bound()
        catalog, shader, capture_id = self._catalog(
            "bad-graph-binding", _dxbc(), resource_id=132
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "workspace-bad-graph-binding", capture_id, shader
        )
        self._with_fully_validated_candidate(
            status, _dxbc(b"validated bad graph program")
        )
        source_path = Path(status["source"]["path"])
        source = source_path.read_bytes().decode("utf-8")
        other_source = source + "// unbound graph source\n"
        graph = build_shader_flow_graph(
            other_source,
            workspace_id=status["workspaceId"],
            source_path="source/shader.hlsl",
            source_sha256=hashlib.sha256(other_source.encode("utf-8")).hexdigest(),
            source_revision=status["revision"],
            stage=status["stage"],
            entry_point=status["entryPoint"],
            generated_at="2026-08-31T00:00:00Z",
            origin="organized-proposal",
        )
        graph_path = Path(status["workspacePath"]) / "ai" / "flow-graph.json"
        graph_path.write_text(json.dumps(graph), encoding="utf-8")

        registered = self.service.register_workspace(status)
        self.assertEqual(registered["flowGraphArtifacts"], [])
        self.assertIn("belongs to different source bytes",
                      registered["explanatoryDiagnostics"][0]["reason"])
        self.assertTrue(graph_path.is_file())

        self.assertEqual(
            self.service.replay_service().status()["knowledgeCounts"]["workspaces"],
            1,
        )

    def test_invalid_optional_flowchart_does_not_block_source_registration(self) -> None:
        self._bound()
        catalog, shader, capture_id = self._catalog(
            "bad-flowchart", _dxbc(), resource_id=77
        )
        self.service.import_capture(catalog)
        workspace_status = self._workspace_status(
            "workspace-bad-flowchart",
            capture_id,
            shader,
            flowchart=b"flowchart TD\x00A-->B\n",
        )
        registered = self.service.register_workspace(workspace_status)
        self.assertEqual(registered["flowchartArtifacts"], [])
        self.assertIn("NUL", registered["explanatoryDiagnostics"][0]["reason"])
        self.assertEqual(
            self.service.replay_service().status()["knowledgeCounts"]["workspaces"],
            1,
        )

    def test_source_only_draft_omits_stale_compiled_candidate(self) -> None:
        self._bound()
        catalog, shader, capture_id = self._catalog(
            "source-only-draft", _dxbc(), resource_id=133
        )
        self.service.import_capture(catalog)
        status = self._workspace_status("source-only-draft", capture_id, shader)
        self._with_fully_validated_candidate(status, _dxbc(b"old validated program"))
        source = Path(status["source"]["path"])
        source.write_text("float4 main() : SV_Target { return 0; }\n", encoding="utf-8")
        status["source"]["currentSha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        registered = self.service.register_workspace(
            status, include_compiled_candidate=False
        )
        self.assertIsNone(registered["knowledge"].get("compiledCandidate"))
        self.assertEqual(registered["knowledge"]["sourceSha256"],
                         status["source"]["currentSha256"])

    def test_ai_provisional_alias_cannot_enter_active_project(self) -> None:
        self._bound()
        catalog, shader, capture_id = self._catalog(
            "provisional", _dxbc(), resource_id=88
        )
        self.service.import_capture(catalog)
        alias_path = self.root / "provisional-alias.json"
        alias_path.write_text(
            json.dumps(
                {
                    "schema": ALIAS_SCHEMA,
                    "schemaVersion": ALIAS_VERSION,
                    "verifiedOnly": False,
                    "captureId": capture_id,
                    "aliases": [
                        {
                            "verified": False,
                            "status": "AIProvisional",
                            "captureId": capture_id,
                            "shaderResourceId": 88,
                            "stage": "fragment",
                            "entryPoint": "main",
                            "rawSha256": shader["rawSha256"],
                            "bindingKind": "constantBufferVariable",
                            "registerSpace": 0,
                            "bindNumber": 0,
                            "byteOffset": 0,
                            "canonicalPath": "cb0._child0",
                            "alias": "MaybeRoughness",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ActiveProjectError, "verified exported"):
            self.service.register_alias_table(alias_path)

    def test_content_addressed_flowchart_is_hash_verified_on_load(self) -> None:
        self._bound()
        catalog, shader, capture_id = self._catalog(
            "flowchart-tamper", _dxbc(), resource_id=99
        )
        self.service.import_capture(catalog)
        registered = self.service.register_workspace(
            self._workspace_status("workspace-flowchart", capture_id, shader)
        )
        record = registered["flowchartArtifacts"][0]
        project_root = Path(self.service.status()["projectRoot"])
        artifact = project_root / record["artifactPath"]
        artifact.write_text("flowchart TD\n  X-->Y\n", encoding="utf-8")
        with self.assertRaisesRegex(ActiveProjectError, "artifact hash mismatch"):
            self.service.status()


if __name__ == "__main__":
    unittest.main()
