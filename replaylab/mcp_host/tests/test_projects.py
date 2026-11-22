from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from src import projects as projects_module
from src.projects import ReplayProjectError, ReplayProjectService
from src.symbols.dxbc import fingerprint_bytes
from src.symbols.matcher import ALIAS_SCHEMA, ALIAS_VERSION, capture_identity


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


def _complete_interface(output_topology: str = "TriangleList") -> dict:
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
        "outputTopology": output_topology,
        "dynamicLinkageInterfaceCount": 0,
    }


def _compiler_binding(target_profile: dict, *, flag: str = "-O3") -> dict:
    binding = {
        "schema": "renderdoc-ultra-shader-compiler-binding/v1",
        "schemaVersion": 1,
        "targetProfile": copy.deepcopy(target_profile),
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
                "argumentTemplate": [
                    "-T",
                    "{profile}",
                    "-E",
                    "{entry_point}",
                    flag,
                ],
                "placeholders": ["profile", "entry_point"],
            }
        ],
        "executionAuthority": False,
        "evidenceOnly": True,
    }
    binding["bindingSha256"] = projects_module._canonical_sha256(binding)
    return binding


class ReplayProjectTests(unittest.TestCase):
    def test_source_registration_preserves_optional_catalog_code_identity(self):
        self.project = self.root / "vulkan-project"
        ReplayProjectService.create(self.project, "Vulkan source", game_id="example-game",
                                    build_id="build-1", platform="windows-x64", api_family="vulkan")
        self.service = ReplayProjectService(self.project)
        catalog, shader, capture_id = self._catalog("raw-fingerprint", _dxbc(), resource_id=91)
        status = self._workspace_status("raw-workspace", capture_id, shader,
                                        "float4 main() : SV_Target { return 1; }\n")
        from tests.test_recovery_assets import _make_spirv
        blob = _make_spirv()
        Path(shader["rawPath"]).write_bytes(blob)
        document = json.loads(catalog.read_text(encoding="utf-8"))
        document["api"] = "Vulkan"
        document["shaders"][0].update(codeSha256="", rawSha256=hashlib.sha256(blob).hexdigest(),
                                       rawSize=len(blob), encoding="SPIR-V")
        document["captureId"] = capture_identity(document, document["shaders"])
        catalog.write_text(json.dumps(document), encoding="utf-8")
        self.service.import_capture(catalog)
        status["capture"]["captureId"] = document["captureId"]
        status["shader"]["rawSha256"] = status["baseline"]["rawSha256"] = hashlib.sha256(blob).hexdigest()
        status["baseline"]["kind"] = "SPIRV"
        status["baseline"]["codeSha256"] = status["baseline"]["rawSha256"]
        result = self.service.register_workspace(status, include_compiled_candidate=False)
        knowledge = result["knowledge"]
        self.assertEqual(knowledge["codeSha256"], "")
        self.assertEqual(self.service.get_workspace_knowledge(knowledge["knowledgeId"]), knowledge)
        # Pre-1.13.67 native-format records put the raw hash in codeSha256.
        # They remain content-addressed and must be readable without editing
        # their immutable publication history or relaxing physical identity.
        legacy = copy.deepcopy(knowledge)
        legacy["codeSha256"] = legacy["rawSha256"]
        identity_keys = (
            "workspaceId", "captureId", "originCatalogSha256", "stage", "entryPoint",
            "rawSha256", "codeSha256", "layoutHash", "interfaceHash", "interfaceComplete",
            "baselineAbiSha256", "targetProfile", "sourceSha256", "analysisSha256",
            "publicationMode", "originResourceId", "compilerBinding",
        )
        identity = {key: legacy[key] for key in identity_keys if key in legacy}
        legacy["knowledgeId"] = projects_module._canonical_sha256(identity)[:32]
        legacy["sourcePath"] = "knowledge/workspaces/%s.hlsl" % legacy["knowledgeId"]
        (self.project / legacy["sourcePath"]).write_bytes(
            (self.project / knowledge["sourcePath"]).read_bytes())
        manifest = self.service._load()
        self.assertEqual(self.service._workspace_knowledge_from_manifest(
            manifest, legacy["knowledgeId"], record=legacy), legacy)
        for field, bad_value in (("codeSha256", "f" * 64),
                                 ("layoutHash", "f" * 64),
                                 ("interfaceHash", "f" * 64)):
            changed = {**legacy, field: bad_value}
            with self.assertRaisesRegex(ReplayProjectError, "physical identity differs"):
                self.service._workspace_knowledge_from_manifest(
                    manifest, legacy["knowledgeId"], record=changed)
        status["baseline"]["codeSha256"] = "f" * 64
        with self.assertRaisesRegex(ReplayProjectError, "code identity differs"):
            self.service.register_workspace(status, include_compiled_candidate=False)

    def test_source_only_workspace_draft_skips_compiled_candidate(self):
        catalog, shader, capture_id = self._catalog(
            "source-only-draft", _dxbc(), resource_id=91
        )
        self.service.import_capture(catalog)
        source = "float4 main() : SV_Target { return 0.75; }\n"
        status = self._workspace_status(
            "source-only-draft-workspace", capture_id, shader, source
        )
        status["latestCompiled"] = {"replayAbiValidated": True, "rawSha256": "0" * 64}
        with mock.patch.object(
            ReplayProjectService, "_validated_compiled_candidate",
            side_effect=AssertionError("draft registration must not inspect ABI"),
        ):
            result = self.service.register_workspace(
                status, include_compiled_candidate=False
            )
        knowledge = result["knowledge"]
        self.assertNotIn("compiledCandidate", knowledge)
        self.assertEqual(knowledge["publicationMode"], "source-only-draft")
        self.assertEqual(self.service.get_workspace_knowledge(knowledge["knowledgeId"]), knowledge)
        inventory = self._shader_inventory(
            resource_id=9912, raw_sha256=knowledge["rawSha256"],
            layout_hash=knowledge["layoutHash"], interface_hash=knowledge["interfaceHash"])
        snapshot = self.service.reuse_snapshot_for_shader_inventory(inventory)
        from src.project_restore import _inventory_binding_state
        from src.shader_source_publication import source_only_restore_reason
        projected = _inventory_binding_state(snapshot, inventory)["workspaces"][knowledge["knowledgeId"]]
        self.assertEqual(projected["workspaceId"], knowledge["workspaceId"])
        self.assertEqual(projected["workspacePath"], knowledge["workspacePath"])
        self.assertEqual(projected["sourceOriginPath"], knowledge["sourceOriginPath"])
        self.assertEqual(source_only_restore_reason(projected), "source-only-project-draft")
        self.assertFalse(result["applied"])
        self.assertEqual(
            (self.project / knowledge["sourcePath"]).read_text(encoding="utf-8"),
            source,
        )
        with self.assertRaisesRegex(ReplayProjectError, "cannot be required and omitted"):
            self.service.register_workspace(
                status, require_current_candidate=True,
                include_compiled_candidate=False,
            )

    def test_project_source_languages_share_original_head_and_keep_historical_artifacts(self):
        # Storage/identity fixture: compiler and live ABI behavior are covered
        # by the workspace route suites, not inferred from this mocked status.
        catalog, shader, capture_id = self._catalog("source-languages", _dxbc(), resource_id=91)
        self.service.import_capture(catalog)
        hlsl = self._workspace_status("source-hlsl", capture_id, shader, "float4 main() : SV_Target { return 1; }\n")
        hlsl_record = self.service.register_workspace(hlsl)["knowledge"]
        glsl_text = "#version 450\nlayout(location=0) out vec4 color;\nvoid main() { color=vec4(1); }\n"
        glsl = self._workspace_status("source-glsl", capture_id, shader, glsl_text)
        old_path = Path(glsl["source"]["path"])
        glsl_path = old_path.with_suffix(".glsl")
        old_path.rename(glsl_path)
        glsl["source"].update(format="glsl", path=str(glsl_path))
        registered = self.service.register_workspace(glsl)
        knowledge = registered["knowledge"]
        self.assertEqual(knowledge["sourceFormat"], "glsl")
        self.assertTrue(knowledge["sourcePath"].endswith(".glsl"))
        self.assertNotEqual(knowledge["knowledgeId"], hlsl_record["knowledgeId"])
        self.assertEqual(self.service._workspace_head_key(knowledge, self.service._load()),
                            self.service._workspace_head_key(hlsl_record, self.service._load()))
        current = self.service.current_workspace_source_head(hlsl_record["knowledgeId"])
        self.assertEqual(current["knowledge"]["knowledgeId"], knowledge["knowledgeId"])
        # Old installations published one head per language. Read/validate that
        # exact index before merging the active lanes, without rewriting IDs,
        # source artifacts or a deliberately selected historical publication.
        manifest = self.service._load()
        legacy_heads = {}
        for record in (hlsl_record, knowledge):
            legacy_heads[self.service._legacy_workspace_head_key(record, manifest)] = {
                "generation": 1, "knowledgeId": record["knowledgeId"],
                "publishedAt": record["registeredAt"],
            }
        manifest["activeHeads"]["workspaces"] = legacy_heads
        before = copy.deepcopy(manifest["knowledge"])
        self.service.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        migrated = self.service._load(persist_migrations=True)
        self.assertEqual(migrated["knowledge"], before)
        self.assertEqual(len(migrated["activeHeads"]["workspaces"]), 1)
        self.assertEqual(self.service.current_workspace_source_head(hlsl_record["knowledgeId"])
                         ["knowledge"]["knowledgeId"], knowledge["knowledgeId"])
        broken = copy.deepcopy(manifest)
        broken["activeHeads"]["workspaces"].pop(next(iter(legacy_heads)))
        with self.assertRaisesRegex(ReplayProjectError, "incomplete"):
            self.service._active_heads_state(broken)
        self.assertEqual(self.service.get_workspace_knowledge(knowledge["knowledgeId"]), knowledge)
        inventory = self._shader_inventory(
            resource_id=9912, raw_sha256=knowledge["rawSha256"],
            layout_hash=knowledge["layoutHash"], interface_hash=knowledge["interfaceHash"])
        snapshot = self.service.reuse_snapshot_for_shader_inventory(inventory)
        from src.project_restore import _inventory_binding_state
        projected = _inventory_binding_state(snapshot, inventory)["workspaces"][knowledge["knowledgeId"]]
        self.assertEqual(projected["sourceFormat"], "glsl")
        self.assertEqual(snapshot["knowledge"]["workspaces"][knowledge["knowledgeId"]]["source"]["kind"],
                         "verified-glsl")
        target = self._workspace_status("source-glsl-target", capture_id, shader, "// target\n")
        with self.assertRaisesRegex(ReplayProjectError, "source formats differ"):
            self.service.seed_workspace(knowledge["knowledgeId"], target)
        old_path = Path(target["source"]["path"])
        target_path = old_path.with_suffix(".glsl")
        old_path.rename(target_path)
        target["source"].update(format="glsl", path=str(target_path))
        seeded = self.service.seed_workspace(knowledge["knowledgeId"], target)
        self.assertTrue(seeded["requiresLiveAbiRevalidation"])
        self.assertEqual(target_path.read_bytes(), glsl_path.read_bytes())
        target_path.unlink()
        target["source"]["present"] = False
        target["capture"]["api"] = "D3D11"
        bundle = self.service.prepare_workspace_source(knowledge["knowledgeId"], target)
        self.assertEqual(bundle["sourceFormat"], "glsl")
        self.assertEqual(bundle["sourceText"], glsl_path.read_bytes().decode("utf-8"))

    def test_project_source_bundle_needs_no_decompiled_destination(self):
        catalog, shader, capture_id = self._catalog("direct-project-source", _dxbc(), resource_id=91)
        self.service.import_capture(catalog)
        origin = self._workspace_status("direct-origin", capture_id, shader, "float4 main() : SV_Target { return 1; }\n")
        knowledge = self.service.register_workspace(origin)["knowledge"]
        destination = self._workspace_status("direct-target", capture_id, shader, "temporary")
        Path(destination["source"]["path"]).unlink()
        destination["source"]["present"] = False
        destination["capture"]["api"] = "D3D11"
        bundle = self.service.prepare_workspace_source(knowledge["knowledgeId"], destination)
        self.assertEqual(bundle["sourceText"].encode("utf-8"), Path(origin["source"]["path"]).read_bytes())
        self.assertEqual(bundle["sourceEntryPoint"], "main")
        self.assertFalse(Path(destination["source"]["path"]).exists())
        self.assertTrue(bundle["requiresLiveAbiRevalidation"])
        self.assertEqual(self.service.restorable_shader_raw_hashes(), [knowledge["rawSha256"]])
        changed = copy.deepcopy(destination)
        changed["baseline"]["rawSha256"] = "e" * 64
        with self.assertRaisesRegex(ReplayProjectError, "original Shader"):
            self.service.prepare_workspace_source(knowledge["knowledgeId"], changed)
        changed = copy.deepcopy(destination)
        changed["baseline"]["abiSha256"] = "e" * 64
        refreshed = self.service.prepare_workspace_source(knowledge["knowledgeId"], changed)
        self.assertEqual(refreshed["sourceSha256"], knowledge["sourceSha256"])
        self.assertEqual(refreshed["baselineAbiSha256"], "e" * 64)
        self.assertNotEqual(refreshed["baselineAbiSha256"], knowledge["baselineAbiSha256"])
        self.assertTrue(refreshed["requiresCompile"])
        self.assertTrue(refreshed["requiresLiveAbiRevalidation"])
        self.assertFalse(refreshed["applied"])
        replaced = self._workspace_status("direct-new-head", capture_id, shader, "float4 main() : SV_Target { return 0; }\n")
        self.service.register_workspace(replaced)
        with self.assertRaisesRegex(ReplayProjectError, "active knowledge head"):
            self.service.prepare_workspace_source(knowledge["knowledgeId"], destination)

    def test_inventory_stage_aliases_use_renderdoc_protocol_tokens(self):
        self.assertEqual(projects_module._normalised_stage("raygen"), "raygen")
        self.assertEqual(
            projects_module._normalised_stage("ray generation"), "raygen"
        )
        self.assertEqual(projects_module._normalised_stage("task"), "task")
        self.assertEqual(
            projects_module._normalised_stage("amplification"), "task"
        )

    def test_seeding_existing_project_import_renews_receipt_and_immutable_input(self):
        from src.shader_workspace.project_source import source_receipt, validate_project_source

        for inventory_mode in (False, True):
            for already_seeded in (False, True):
                with self.subTest(inventory=inventory_mode, retry=already_seeded):
                    label = "renew-%s-%s" % (inventory_mode, already_seeded)
                    catalog, shader, capture_id = self._catalog(label, _dxbc(), resource_id=91)
                    self.service.import_capture(catalog)
                    old_text = "float4 main() : SV_Target { return 1; }\n"
                    new_text = "float4 main() : SV_Target { return 2; }\n"
                    origin = self._workspace_status(label + "-origin", capture_id, shader, old_text)
                    old = self.service.register_workspace(origin)["knowledge"]
                    target = self._workspace_status(label + "-target", capture_id, shader, old_text)
                    target["capture"]["api"] = "D3D11"
                    empty = copy.deepcopy(target)
                    empty["source"]["present"] = False
                    bundle = self.service.prepare_workspace_source(old["knowledgeId"], empty)
                    root = Path(target["workspacePath"])
                    input_relative = "source-inputs/project-%s.hlsl" % old["sourceSha256"]
                    old_input = root / input_relative
                    old_input.parent.mkdir()
                    old_bytes = bundle["sourceText"].encode("utf-8")
                    old_input.write_bytes(old_bytes)
                    record = {
                        "path": "source/shader.hlsl", "sha256": old["sourceSha256"],
                        "entryPoint": "main", "origin": "verified-project-source",
                        "projectSource": source_receipt(bundle, input_relative),
                    }
                    manifest = {key: copy.deepcopy(target[key]) for key in
                                ("workspaceId", "stage", "entryPoint", "targetProfile", "baseline")}
                    manifest.update(source=record, routePlan={"editableFormat": "hlsl"})
                    projects_module.atomic_write_json(root / "manifest.json", manifest)
                    target["source"].update(origin=record["origin"], projectSource=record["projectSource"])
                    new_status = self._workspace_status(label + "-new-head", capture_id, shader, new_text)
                    new_bytes = Path(new_status["source"]["path"]).read_bytes()
                    new = self.service.register_workspace(new_status)["knowledge"]
                    if already_seeded:
                        # Reproduce the old seed's partial result on the next open:
                        # desired current bytes, but the old managed receipt.
                        Path(target["source"]["path"]).write_bytes(new_bytes)
                        target["source"].update(currentSha256=new["sourceSha256"], modifiedSinceDecompile=True)
                    if not inventory_mode and not already_seeded:
                        before_manifest = (root / "manifest.json").read_bytes()
                        before_source = Path(target["source"]["path"]).read_bytes()
                        write_json = projects_module.atomic_write_json

                        def fail_manifest(path, value):
                            if Path(path) == root / "manifest.json":
                                raise OSError("simulated manifest write failure")
                            return write_json(path, value)

                        with mock.patch.object(projects_module, "atomic_write_json", side_effect=fail_manifest):
                            with self.assertRaisesRegex(OSError, "simulated manifest"):
                                self.service.seed_workspace(new["knowledgeId"], target)
                        self.assertEqual((root / "manifest.json").read_bytes(), before_manifest)
                        self.assertEqual(Path(target["source"]["path"]).read_bytes(), before_source)
                        self.assertFalse((root / ("source-inputs/project-%s.hlsl" % new["sourceSha256"])).exists())
                    if inventory_mode:
                        inventory = self._shader_inventory(
                            resource_id=91, raw_sha256=new["rawSha256"],
                            layout_hash=new["layoutHash"], interface_hash=new["interfaceHash"])
                        snapshot = self.service.reuse_snapshot_for_shader_inventory(inventory)
                        seeded = self.service.seed_workspace_from_inventory(
                            new["knowledgeId"], target, inventory["shaders"][0], inventory, snapshot["binding"])
                    else:
                        seeded = self.service.seed_workspace(new["knowledgeId"], target)
                    renewed = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
                    receipt = validate_project_source(root, renewed, renewed["source"])
                    self.assertEqual(receipt["knowledgeId"], new["knowledgeId"])
                    self.assertEqual(receipt["sourceSha256"], new["sourceSha256"])
                    self.assertEqual(receipt["baselineAbiSha256"], target["baseline"]["abiSha256"])
                    self.assertEqual((root / receipt["inputPath"]).read_bytes(), new_bytes)
                    self.assertEqual(Path(target["source"]["path"]).read_bytes(), new_bytes)
                    self.assertEqual(old_input.read_bytes(), old_bytes)
                    self.assertNotEqual(receipt["receiptSha256"], record["projectSource"]["receiptSha256"])
                    self.assertTrue(seeded["requiresCompile"])
                    self.assertTrue(receipt["requiresLiveAbiRevalidation"])
                    self.assertFalse(receipt["applied"])
                    # No historical replay/ABI grant was copied into the new receipt.
                    self.assertNotIn("latestCompiled", renewed)
                    self.assertNotIn("replacementResourceId", receipt)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        ReplayProjectService.create(
            self.project,
            "multi-frame",
            game_id="example-game",
            build_id="build-1",
            platform="windows-x64",
            api_family="d3d11",
        )
        self.service = ReplayProjectService(self.project)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_legacy_project_reads_without_writes_and_persists_heads_on_next_mutation(self) -> None:
        manifest_path = self.project / "project.json"
        created = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            created["activeKnowledge"],
            {"revision": 0, "workspaces": {}, "materials": {}},
        )
        self.assertEqual(
            created["activeHeads"],
            {"workspaces": {}, "materials": {}, "aliases": {}},
        )

        catalog, shader, capture_id = self._catalog(
            "legacy-active", _dxbc(), resource_id=91
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "legacy-active-workspace",
            capture_id,
            shader,
            "// legacy active source\n",
        )
        registered = self.service.register_workspace(status)
        legacy = json.loads(manifest_path.read_text(encoding="utf-8"))
        legacy.pop("activeKnowledge")
        legacy.pop("activeHeads")
        manifest_path.write_text(json.dumps(legacy), encoding="utf-8")
        before = manifest_path.read_bytes()

        self.service.status()

        self.assertEqual(manifest_path.read_bytes(), before)
        migrated_on_open = self.service._load(persist_migrations=False)
        token = registered["knowledge"]["knowledgeId"]
        self.assertEqual(migrated_on_open["activeKnowledge"]["revision"], 1)
        migrated_head = next(
            iter(migrated_on_open["activeHeads"]["workspaces"].values())
        )
        self.assertEqual(migrated_head["knowledgeId"], token)
        self.assertEqual(migrated_head["generation"], 1)
        after_migration = manifest_path.read_bytes()
        repeated = self.service.register_workspace(status)
        self.assertTrue(repeated["idempotent"])
        self.assertFalse(repeated["headChanged"])
        self.assertEqual(manifest_path.read_bytes(), after_migration)

        newer_status = self._workspace_status(
            "legacy-active-workspace-newer",
            capture_id,
            shader,
            "// newer active source\n",
        )
        newer = self.service.register_workspace(newer_status)
        self.assertNotEqual(manifest_path.read_bytes(), before)
        migrated = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["activeKnowledge"]["revision"], 2)
        self.assertEqual(migrated["activeKnowledge"]["workspaces"][token], 1)
        self.assertEqual(
            migrated["activeKnowledge"]["workspaces"][
                newer["knowledge"]["knowledgeId"]
            ],
            2,
        )
        self.assertEqual(migrated["activeKnowledge"]["materials"], {})
        self.assertEqual(
            migrated["activeHeads"]["workspaces"][newer["head"]["headKey"]],
            {
                "generation": 2,
                "knowledgeId": newer["knowledge"]["knowledgeId"],
                "publishedAt": newer["head"]["publishedAt"],
            },
        )

    def test_explicit_active_knowledge_corruption_fails_closed(self) -> None:
        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["activeKnowledge"] = {
            "revision": 1,
            "workspaces": {"f" * 32: 1},
            "materials": {},
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(
            ReplayProjectError, "activeKnowledge workspaces index is incomplete"
        ):
            self.service.status()

    def _catalog(
        self,
        label: str,
        blob: bytes,
        *,
        resource_id: int,
        topology: str = "TriangleList",
    ) -> tuple[Path, dict, str]:
        directory = self.root / label
        directory.mkdir()
        blob_path = directory / "shader.dxbc"
        blob_path.write_bytes(blob)
        fingerprint = fingerprint_bytes(blob)
        self.assertIsNotNone(fingerprint)
        shader = {
            "shaderResourceId": resource_id,
            "stage": "fragment",
            "entryPoint": "main",
            "rawPath": str(blob_path),
            "rawSize": len(blob),
            "rawSha256": hashlib.sha256(blob).hexdigest(),
            "codeSha256": fingerprint.code_sha256,
            **_complete_interface(topology),
        }
        value = {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": str(directory / (label + ".rdc")),
            "api": "D3D11",
            "shaders": [shader],
            "uses": [],
        }
        capture_id = capture_identity(value, [shader])
        value["captureId"] = capture_id
        catalog_path = directory / "catalog.json"
        catalog_path.write_text(json.dumps(value), encoding="utf-8")
        return catalog_path, shader, capture_id

    def _catalog_revision(
        self,
        source_catalog: Path,
        label: str,
        *,
        field_base_type: str,
        indent: int | None = None,
    ) -> Path:
        value = json.loads(source_catalog.read_text(encoding="utf-8"))
        source_blob = Path(value["shaders"][0]["rawPath"])
        directory = self.root / label
        directory.mkdir()
        blob_path = directory / "shader.dxbc"
        blob_path.write_bytes(source_blob.read_bytes())
        value["shaders"][0]["rawPath"] = str(blob_path)
        value["shaders"][0]["constantBuffers"][0]["fields"][0][
            "baseType"
        ] = field_base_type
        path = directory / "catalog.json"
        path.write_text(json.dumps(value, indent=indent), encoding="utf-8")
        return path

    def _duplicate_byte_catalog(
        self, label: str, blob: bytes, *, first_resource_id: int,
        second_resource_id: int,
    ) -> tuple[Path, dict, dict, str]:
        catalog_path, first, unused_capture_id = self._catalog(
            label, blob, resource_id=first_resource_id
        )
        value = json.loads(catalog_path.read_text(encoding="utf-8"))
        second = json.loads(json.dumps(first))
        second["shaderResourceId"] = second_resource_id
        value["shaders"].append(second)
        capture_id = capture_identity(value, value["shaders"])
        value["captureId"] = capture_id
        catalog_path.write_text(json.dumps(value), encoding="utf-8")
        return catalog_path, first, second, capture_id

    def _workspace_status(
        self,
        label: str,
        capture_id: str,
        shader: dict,
        source_text: str,
    ) -> dict:
        workspace = self.root / label
        (workspace / "source").mkdir(parents=True)
        source_path = workspace / "source" / "shader.hlsl"
        source_path.write_text(source_text, encoding="utf-8")
        (workspace / "ai").mkdir()
        (workspace / "ai" / "analysis.md").write_text(
            "AI explanation only; verify against reflection.\n", encoding="utf-8"
        )
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
            "compilerBinding": _compiler_binding(target_profile),
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
                "kind": fingerprint.kind,
            },
        }

    def _shader_inventory(
        self,
        *,
        resource_id: int,
        raw_sha256: str,
        layout_hash: str,
        interface_hash: str,
        abi_hash: str = "a" * 64,
        anchor_event_id: int | None = 23,
        api: str = "d3d11",
        encoding: str = "DXBC",
        protocol_encoding: str = "dxbc",
    ) -> dict:
        stable_identity = {
            "api": api,
            "encoding": protocol_encoding,
            "rawSha256": raw_sha256,
            "stage": "fragment",
            "entryPoint": "main",
            "abiHash": abi_hash,
            "layoutHash": layout_hash,
        }
        stable_key = hashlib.sha256(
            json.dumps(
                stable_identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        block = {
            "index": 0,
            "path": "cb0",
            "name": "PerMaterial",
            "space": 0,
            "bind": 0,
            "byteSize": 16,
            "layoutSha256": "b" * 64,
            "abiSha256": "c" * 64,
            "fields": [
                {
                    "canonicalPath": "BaseColor",
                    "name": "BaseColor",
                    "absoluteByteOffset": 0,
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
                }
            ],
        }
        shader = {
            "resourceId": resource_id,
            **{**stable_identity, "encoding": encoding},
            "interfaceHash": interface_hash,
            "interfaceComplete": True,
            "stableKeySha256": stable_key,
            "anchorEventId": anchor_event_id,
            "constantBlocks": [block],
        }
        capture_id = hashlib.sha256(
            json.dumps(
                [stable_key],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        inventory_material = {
            "api": api,
            "shaders": [
                {
                    "resourceId": resource_id,
                    "stableKeySha256": stable_key,
                    "anchorEventId": anchor_event_id,
                    "constantBlocks": [block],
                }
            ],
        }
        inventory_sha = hashlib.sha256(
            json.dumps(
                inventory_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "schema": "renderdoc-ultra-project-shader-inventory/v1",
            "schemaVersion": 1,
            "capture": {
                "captureId": capture_id,
                "catalogSha256": inventory_sha,
                "inventorySha256": inventory_sha,
                "api": api,
            },
            "shaders": [shader],
        }

    def _with_fully_validated_candidate(
        self, status: dict, candidate_blob: bytes, *, revision: int = 3
    ) -> dict:
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
        self.assertIsNotNone(fingerprint)
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
                "reportSha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            },
        }
        Path(status["manifestPath"]).write_text(
            json.dumps({"revision": revision, "latestCompiled": persisted}),
            encoding="utf-8",
        )
        status.update(
            {
                "revision": revision,
                "latestCompiled": {
                    **persisted,
                    "candidateBlobPath": str(candidate_path.resolve()),
                    "applied": False,
                },
                "replayAbiValidated": True,
                "abiCompatible": True,
                # Capture-local authority must never enter project knowledge.
                "replacementId": 777,
                "captureSessionId": "d" * 32,
            }
        )
        return status

    def test_native_glsl_candidate_requires_exact_source_binding_and_current_abi(self):
        from types import SimpleNamespace
        from src.shader_workspace import native_glsl
        unused_catalog, shader, capture = self._catalog("native-glsl-binding", _dxbc(), resource_id=91)
        source = "#version 450\nvoid main() {}\n"
        status = self._workspace_status("native-glsl-source", capture, shader, source)
        status["capture"]["api"] = "GraphicsAPI.OpenGL"
        status["source"]["format"] = "glsl"
        status["baseline"]["kind"] = "GLSL"
        status["targetProfile"] = native_glsl.profile("fragment", "OpenGL")
        status["compilerBinding"] = native_glsl.compiler_binding(
            {"targetProfile": status["targetProfile"], "routePlan": native_glsl.plan("fragment")})
        blob = Path(status["source"]["path"]).read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        with mock.patch(__name__ + ".fingerprint_bytes", return_value=SimpleNamespace(kind="GLSL", code_sha256=digest)):
            self._with_fully_validated_candidate(status, blob)
        args = {"source_bytes": blob, "workspace_root": Path(status["workspacePath"]),
                "manifest_bytes": Path(status["manifestPath"]).read_bytes(), "baseline": status["baseline"]}
        identity, actual = self.service._validated_compiled_candidate(status, **args)
        self.assertEqual(actual, blob)
        self.assertEqual(identity["kind"], "GLSL")
        self.assertEqual(identity["rawSha256"], identity["sourceSha256"])
        self.assertFalse(identity["semanticEquivalenceVerified"])
        for api in ("OpenGL", "opengl", "GraphicsAPI.OpenGL", "graphicsapiopengl"):
            with self.subTest(native_glsl_api=api):
                native_status = copy.deepcopy(status)
                native_status["capture"]["api"] = api
                self.assertEqual(
                    self.service._validated_compiled_candidate(native_status, **args)[0], identity)
        inventory = self._shader_inventory(
            resource_id=91, raw_sha256=status["baseline"]["rawSha256"],
            layout_hash="1" * 64, interface_hash="2" * 64,
            api="opengl", encoding="GLSL", protocol_encoding="glsl")
        status["capture"]["captureId"] = inventory["capture"]["captureId"]
        registered = self.service.register_workspace_from_inventory(
            status, inventory["shaders"][0], inventory,
            self.service.reuse_snapshot_for_shader_inventory(inventory)["binding"])
        knowledge = registered["knowledge"]
        self.assertEqual(self.service.get_workspace_knowledge(knowledge["knowledgeId"]), knowledge)
        snapshot = self.service.reuse_snapshot_for_shader_inventory(inventory)
        projected = snapshot["knowledge"]["workspaces"][knowledge["knowledgeId"]]
        self.assertEqual(projected["sourceFormat"], "glsl")
        self.assertEqual(projected["compiledCandidate"]["kind"], "GLSL")
        self.assertTrue(projected["compiledCandidate"]["freshLiveAbiRevalidationRequired"])
        self.assertFalse(projected["compiledCandidate"]["applyAuthorization"])
        for binding in (None, _compiler_binding(status["targetProfile"])):
            changed = copy.deepcopy(status)
            changed["compilerBinding"] = binding
            with self.assertRaises(ReplayProjectError):
                self.service._validated_compiled_candidate(changed, **args)
        changed = copy.deepcopy(status)
        changed["capture"]["api"] = "Vulkan"
        with self.assertRaisesRegex(ReplayProjectError, "OpenGL source revision"):
            self.service._validated_compiled_candidate(changed, **args)
        changed = copy.deepcopy(status)
        changed["latestCompiled"]["replayAbiValidated"] = False
        with self.assertRaisesRegex(ReplayProjectError, "inconsistent"):
            self.service._validated_compiled_candidate(changed, **args)
        changed = copy.deepcopy(identity)
        changed["sourceSha256"] = "e" * 64
        with self.assertRaisesRegex(ReplayProjectError, "source/API binding"):
            projects_module._compiled_candidate_identity(changed)

    def test_native_glsl_compiler_binding_is_evidence_only_and_content_bound(self):
        from src.shader_workspace import native_glsl
        for container in ("glsl", "opengl-spirv"):
            profile = {**native_glsl.profile("fragment", "opengl"), "containerFormat": container}
            binding = native_glsl.compiler_binding(
                {"targetProfile": profile, "routePlan": {"planSha256": "a" * 64}})
            self.assertEqual(projects_module._compiler_binding_identity(binding), binding)
            for field, value in (("executionAuthority", True), ("evidenceOnly", False),
                                 ("api", "vulkan"), ("sourceFormat", "hlsl"),
                                 ("backend", "external"), ("compilerContract", "unknown"),
                                 ("hops", []), ("bindingSha256", "b" * 64),
                                 ("routePlanSha256", "not-a-digest")):
                changed = {**binding, field: value}
                if field != "bindingSha256":
                    changed["bindingSha256"] = projects_module._canonical_sha256(
                        {key: item for key, item in changed.items() if key != "bindingSha256"})
                with self.subTest(container=container, field=field), self.assertRaises(ReplayProjectError):
                    projects_module._compiler_binding_identity(changed)

    def test_cross_frame_reuse_seeds_source_but_never_reuses_live_state(self) -> None:
        blob_a = _dxbc(b"same program", b"frame-a reflection")
        blob_b = _dxbc(b"same program", b"frame-b reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "frame-a", blob_a, resource_id=101
        )
        catalog_b, shader_b, capture_b = self._catalog(
            "frame-b", blob_b, resource_id=909
        )
        self.assertNotEqual(shader_a["rawSha256"], shader_b["rawSha256"])
        self.assertEqual(shader_a["codeSha256"], shader_b["codeSha256"])

        imported_a = self.service.import_capture(catalog_a)
        self.assertEqual(imported_a["matchedShaderCount"], 0)
        status_a = self._workspace_status(
            "workspace-a", capture_a, shader_a, "float4 main() : SV_Target { return 1; }\n"
        )
        workspace = self.service.register_workspace(status_a)

        alias_path = self.root / "aliases.json"
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
        material = self.service.save_material_knowledge(
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
        self.assertFalse(material["applied"])

        plan = self.service.import_capture(catalog_b)
        self.assertEqual(plan["matchedShaderCount"], 1)
        match = plan["matches"][0]
        self.assertEqual(
            match["workspaceCandidates"][0]["matchMode"],
            "code-interface-layout-exact",
        )
        self.assertTrue(
            match["workspaceCandidates"][0]["analysisCandidate"][
                "explanatoryOnly"
            ]
        )
        self.assertFalse(
            match["workspaceCandidates"][0]["analysisCandidate"][
                "physicalEvidence"
            ]
        )
        self.assertEqual(
            match["aliasCandidates"][0]["matchMode"],
            "code-interface-layout-exact",
        )
        self.assertTrue(
            match["materialCandidates"][0][
                "materialLayoutLiveRevalidationRequired"
            ]
        )
        self.assertFalse(plan["policy"]["autoApplyAuthorized"])
        self.assertTrue(
            plan["policy"]["liveAbiRevalidationRequiredBeforeShaderApply"]
        )
        self.assertTrue(plan["policy"]["liveMaterialLayoutRevalidationRequired"])

        status_b = self._workspace_status(
            "workspace-b", capture_b, shader_b, "// fresh decompile\n"
        )
        seeded = self.service.seed_workspace(
            workspace["knowledge"]["knowledgeId"], status_b
        )
        self.assertFalse(seeded["compiled"])
        self.assertFalse(seeded["applied"])
        self.assertTrue(seeded["requiresCompile"])
        self.assertTrue(seeded["requiresLiveAbiRevalidation"])
        self.assertEqual(
            Path(status_b["source"]["path"]).read_text(encoding="utf-8"),
            "float4 main() : SV_Target { return 1; }\n",
        )
        self.assertIsNone(seeded["supersededLocalEdit"])

        # A local edit in the destination: the plain seed refuses it; the
        # restore's superseding seed backs it up beside the workspace first.
        edited = "// my local edit\nfloat4 main() : SV_Target { return 0.5; }\n"
        Path(status_b["source"]["path"]).write_text(edited, encoding="utf-8")
        status_b["source"]["currentSha256"] = hashlib.sha256(Path(status_b["source"]["path"]).read_bytes()).hexdigest()
        status_b["source"]["modifiedSinceDecompile"] = True
        with self.assertRaisesRegex(ReplayProjectError, "still unmodified"):
            self.service.seed_workspace(workspace["knowledge"]["knowledgeId"], status_b)
        superseded = self.service.seed_workspace(
            workspace["knowledge"]["knowledgeId"], status_b, supersede_local_edit=True)
        backup = Path(superseded["supersededLocalEdit"]["backupPath"])
        self.assertEqual(backup.parent, Path(status_b["workspacePath"]) / "ai" / "local-edits")
        self.assertEqual(backup.read_text(encoding="utf-8"), edited)
        self.assertEqual(superseded["supersededLocalEdit"]["sourceSha256"],
                         status_b["source"]["currentSha256"])
        self.assertEqual(
            Path(status_b["source"]["path"]).read_text(encoding="utf-8"),
            "float4 main() : SV_Target { return 1; }\n",
        )

    def test_duplicate_bytes_persist_exact_workspace_and_material_resource(self) -> None:
        catalog, unused_first, second, capture_id = self._duplicate_byte_catalog(
            "duplicate-origin", _dxbc(b"shared physical program"),
            first_resource_id=601, second_resource_id=602,
        )
        imported = self.service.import_capture(catalog)
        status = self._workspace_status(
            "duplicate-origin-workspace", capture_id, second,
            "float4 main() : SV_Target { return 0.75; }\n",
        )

        workspace = self.service.register_workspace(status)["knowledge"]
        material = self.service.save_material_knowledge(
            {
                "schema": "renderdoc-ultra-material-session/v1",
                "captureId": capture_id,
                "catalogSha256": imported["catalogSha256"],
                "captureSessionId": "e" * 32,
                "shaderResourceId": 602,
                "stage": "fragment",
                "constantBlockPath": "cb0",
                "layoutSha256": "c" * 64,
                "parameters": [{
                    "parameterId": "d" * 32,
                    "path": "cb0.BaseColor",
                    "baseType": "Float",
                    "components": 4,
                    "value": [0.75, 0.5, 0.25, 1.0],
                }],
            },
            ["d" * 32],
        )["knowledge"]

        self.assertEqual(602, workspace["originResourceId"])
        self.assertEqual(602, material["originResourceId"])
        self.assertEqual(
            602,
            self.service.get_workspace_knowledge(
                workspace["knowledgeId"]
            )["originResourceId"],
        )
        self.assertEqual(
            602,
            self.service.get_material_knowledge(
                material["knowledgeId"]
            )["originResourceId"],
        )
        restored = self.service.restore_knowledge_snapshot(
            capture_id, imported["catalogSha256"]
        )
        self.assertIn(workspace["knowledgeId"], restored["workspaces"])
        self.assertIn(material["knowledgeId"], restored["materials"])

        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        material_record = next(
            item for item in manifest["knowledge"]["materials"]
            if item["knowledgeId"] == material["knowledgeId"]
        )
        material_record["originResourceId"] = 601
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ReplayProjectError, "knowledgeId"):
            self.service.status()

    def test_capture_bound_origin_resource_is_hash_bound_and_required_when_ambiguous(self) -> None:
        catalog, unused_first, second, capture_id = self._duplicate_byte_catalog(
            "duplicate-origin-tamper", _dxbc(b"shared tamper program"),
            first_resource_id=611, second_resource_id=612,
        )
        self.service.import_capture(catalog)
        registered = self.service.register_workspace(
            self._workspace_status(
                "duplicate-origin-tamper-workspace", capture_id, second,
                "float4 main() : SV_Target { return 0.5; }\n",
            )
        )["knowledge"]
        manifest_path = self.project / "project.json"
        baseline = manifest_path.read_bytes()

        for mutation in ("remove", "change", "invalid", "noncanonical"):
            with self.subTest(mutation=mutation):
                manifest = json.loads(baseline.decode("utf-8"))
                record = next(
                    item for item in manifest["knowledge"]["workspaces"]
                    if item["knowledgeId"] == registered["knowledgeId"]
                )
                if mutation == "remove":
                    record.pop("originResourceId")
                elif mutation == "change":
                    record["originResourceId"] = 611
                elif mutation == "noncanonical":
                    record["originResourceId"] = "612"
                else:
                    record["originResourceId"] = 0
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ReplayProjectError):
                    self.service.status()

        manifest_path.write_bytes(baseline)
        self.service.status()

    def test_unique_legacy_workspace_without_origin_resource_remains_read_only_compatible(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "legacy-workspace-origin", _dxbc(b"legacy program"), resource_id=621
        )
        self.service.import_capture(catalog)
        registered = self.service.register_workspace(
            self._workspace_status(
                "legacy-workspace-origin-source", capture_id, shader,
                "float4 main() : SV_Target { return 0.25; }\n",
            )
        )["knowledge"]
        material_registered = self.service.save_material_knowledge(
            {
                "schema": "renderdoc-ultra-material-session/v1",
                "captureId": capture_id,
                "captureSessionId": "f" * 32,
                "shaderResourceId": 621,
                "stage": "fragment",
                "constantBlockPath": "cb0",
                "layoutSha256": "c" * 64,
                "parameters": [{
                    "parameterId": "d" * 32,
                    "path": "cb0.BaseColor",
                    "baseType": "Float",
                    "components": 4,
                    "value": [0.25, 0.5, 0.75, 1.0],
                }],
            },
            ["d" * 32],
        )["knowledge"]
        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = next(
            item for item in manifest["knowledge"]["workspaces"]
            if item["knowledgeId"] == registered["knowledgeId"]
        )
        legacy_identity = {
            key: record[key]
            for key in (
                "workspaceId", "captureId", "originCatalogSha256", "stage",
                "entryPoint", "rawSha256", "codeSha256", "layoutHash",
                "interfaceHash", "interfaceComplete", "baselineAbiSha256",
                "targetProfile", "sourceSha256", "analysisSha256",
            )
        }
        legacy_id = projects_module._canonical_sha256(legacy_identity)[:32]
        old_id = record["knowledgeId"]
        old_source = self.project / record["sourcePath"]
        legacy_relative = "knowledge/workspaces/%s.hlsl" % legacy_id
        old_source.rename(self.project / legacy_relative)
        record.pop("originResourceId")
        record.pop("compilerBinding")
        record["knowledgeId"] = legacy_id
        record["sourcePath"] = legacy_relative
        activation = manifest["activeKnowledge"]["workspaces"].pop(old_id)
        manifest["activeKnowledge"]["workspaces"][legacy_id] = activation

        material_record = next(
            item for item in manifest["knowledge"]["materials"]
            if item["knowledgeId"] == material_registered["knowledgeId"]
        )
        legacy_material_identity = {
            key: material_record[key]
            for key in (
                "captureId", "originCatalogSha256", "stage", "entryPoint",
                "rawSha256", "codeSha256", "layoutHash", "interfaceHash",
                "interfaceComplete", "constantBlockPath",
                "constantBlockLayoutSha256", "parameters",
            )
        }
        legacy_material_id = projects_module._canonical_sha256(
            legacy_material_identity
        )[:32]
        material_record.pop("originResourceId")
        material_record["knowledgeId"] = legacy_material_id
        material_activation = manifest["activeKnowledge"]["materials"].pop(
            material_registered["knowledgeId"]
        )
        manifest["activeKnowledge"]["materials"][
            legacy_material_id
        ] = material_activation
        # This fixture represents a manifest from before per-identity heads.
        # Let the compatibility migration derive them from legacy activation.
        manifest.pop("activeHeads")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        self.service.status()
        legacy = self.service.get_workspace_knowledge(legacy_id)
        self.assertNotIn("originResourceId", legacy)
        legacy_material = self.service.get_material_knowledge(
            legacy_material_id
        )
        self.assertNotIn("originResourceId", legacy_material)

    def test_fully_validated_binary_is_content_addressed_cross_rdc_candidate(self) -> None:
        blob_a = _dxbc(b"binary origin program", b"frame-a reflection")
        blob_b = _dxbc(b"binary origin program", b"frame-b reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "binary-frame-a", blob_a, resource_id=141
        )
        catalog_b, unused_shader_b, unused_capture_b = self._catalog(
            "binary-frame-b", blob_b, resource_id=942
        )
        self.service.import_capture(catalog_a)
        status = self._workspace_status(
            "binary-workspace-a",
            capture_a,
            shader_a,
            "float4 main() : SV_Target { return 0.25; }\n",
        )
        candidate_blob = _dxbc(b"edited compiled program")
        self._with_fully_validated_candidate(status, candidate_blob)

        registered = self.service.register_workspace(status)
        knowledge = registered["knowledge"]
        published = knowledge["compiledCandidate"]
        self.assertEqual(
            published["schema"],
            "renderdoc-ultra-project-compiled-candidate/v1",
        )
        self.assertEqual(
            published["sourceSha256"], status["source"]["currentSha256"]
        )
        self.assertEqual(published["sourceRevision"], status["revision"])
        artifact = self.project / published["artifactPath"]
        self.assertEqual(artifact.read_bytes(), candidate_blob)
        self.assertEqual(
            artifact.name, hashlib.sha256(candidate_blob).hexdigest() + ".bin"
        )
        self.assertNotIn("replacementId", json.dumps(knowledge))
        self.assertNotIn("captureSessionId", json.dumps(knowledge))

        plan = self.service.import_capture(catalog_b)
        candidate = plan["matches"][0]["workspaceCandidates"][0]
        self.assertTrue(candidate["binaryCandidateEligible"])
        binary = candidate["binaryCandidate"]
        self.assertEqual(Path(binary["artifactPath"]).read_bytes(), candidate_blob)
        self.assertTrue(binary["integrityVerified"])
        self.assertEqual(
            binary["compilerBindingSha256"],
            status["compilerBinding"]["bindingSha256"],
        )
        self.assertTrue(binary["freshCaptureRebindRequired"])
        self.assertTrue(binary["freshLiveAbiRevalidationRequired"])
        self.assertFalse(binary["applyAuthorization"])
        self.assertFalse(binary["oldReplacementIdReused"])
        self.assertFalse(binary["oldCaptureAbiAuthorityReused"])
        self.assertTrue(plan["policy"]["verifiedBinaryCandidateMaySkipCompile"])
        self.assertTrue(
            plan["policy"]["binaryCandidateRequiresFreshLiveAbiRevalidation"]
        )

    def test_compiled_candidate_without_compiler_flags_binding_is_source_only(self) -> None:
        blob = _dxbc(b"legacy compiled program")
        catalog_a, shader_a, capture_a = self._catalog(
            "legacy-compiler-binding-a", blob, resource_id=151
        )
        catalog_b, unused_shader_b, unused_capture_b = self._catalog(
            "legacy-compiler-binding-b", blob, resource_id=152
        )
        self.service.import_capture(catalog_a)
        status = self._workspace_status(
            "legacy-compiler-binding-workspace",
            capture_a,
            shader_a,
            "float4 main() : SV_Target { return 0.75; }\n",
        )
        status.pop("compilerBinding")
        self._with_fully_validated_candidate(status, _dxbc(b"legacy candidate"))
        self.service.register_workspace(status)

        plan = self.service.import_capture(catalog_b)

        candidate = plan["matches"][0]["workspaceCandidates"][0]
        self.assertTrue(candidate["sourceSeedEligible"])
        self.assertFalse(candidate["binaryCandidateEligible"])
        self.assertIsNone(candidate["binaryCandidate"])
        self.assertIn("compiler/flags", candidate["binaryBlockedReason"])

    def test_shader_display_encoding_accepts_bridge_aliases_without_key_drift(self) -> None:
        aliases = (
            ("DXBC", "DXBC", "dxbc"),
            ("ShaderEncoding.DXBC", "DXBC", "dxbc"),
            ("ShaderEncoding.DirectXBytecode", "DXBC", "dxbc"),
            ("DirectXIL", "DXIL", "dxil"),
            ("ShaderEncoding.DXILContainer", "DXIL", "dxil"),
            ("SPV", "SPIR-V", "spirv"),
            ("ShaderEncoding.SPIRVShader", "SPIR-V", "spirv"),
            ("ShaderEncoding.OpenGLSPIRV", "OpenGL-SPIR-V", "opengl-spirv"),
            ("GLSLShader", "GLSL", "glsl"),
            ("ShaderEncoding.OpenGLGLSL", "GLSL", "glsl"),
            ("ShaderEncoding.METALLIB", "METALLIB", "metallib"),
        )
        base_identity = {
            "api": "d3d12",
            "rawSha256": "1" * 64,
            "stage": "fragment",
            "entryPoint": "main",
            "abiHash": "2" * 64,
            "layoutHash": "3" * 64,
        }
        for alias, display, protocol in aliases:
            with self.subTest(alias=alias):
                self.assertEqual(projects_module._shader_encoding(alias), display)
                actual = projects_module._stable_shader_identity_sha256(
                    {**base_identity, "encoding": alias}
                )
                expected = projects_module._canonical_sha256(
                    {**base_identity, "encoding": protocol}
                )
                self.assertEqual(actual, expected)

        with self.assertRaisesRegex(ReplayProjectError, "unsupported"):
            projects_module._shader_encoding("ShaderEncoding.Unknown")

    def test_direct_inventory_enforces_bridge_api_encoding_matrix(self) -> None:
        supported = (
            ("d3d11", "ShaderEncoding.DirectXBytecode", "dxbc", "DXBC"),
            ("d3d12", "DXBC", "dxbc", "DXBC"),
            ("d3d12", "DXILContainer", "dxil", "DXIL"),
            ("opengl", "GLSLShader", "glsl", "GLSL"),
            ("opengl", "SPIR-V", "spirv", "SPIR-V"),
            (
                "opengl",
                "ShaderEncoding.OpenGLSPIRV",
                "opengl-spirv",
                "OpenGL-SPIR-V",
            ),
            ("vulkan", "SPV", "spirv", "SPIR-V"),
        )
        for index, (api, encoding, protocol, display) in enumerate(supported):
            with self.subTest(api=api, encoding=encoding):
                inventory = self._shader_inventory(
                    resource_id=2000 + index,
                    raw_sha256=hashlib.sha256(
                        (api + encoding).encode("utf-8")
                    ).hexdigest(),
                    layout_hash="4" * 64,
                    interface_hash="5" * 64,
                    api=api,
                    encoding=encoding,
                    protocol_encoding=protocol,
                )
                normalised = projects_module._normalise_shader_inventory(inventory)
                self.assertEqual(normalised["capture"]["api"], api)
                self.assertEqual(normalised["shaders"][0]["encoding"], display)

        unsupported = self._shader_inventory(
            resource_id=2999,
            raw_sha256="6" * 64,
            layout_hash="7" * 64,
            interface_hash="8" * 64,
            api="d3d11",
            encoding="SPIR-V",
            protocol_encoding="spirv",
        )
        with self.assertRaisesRegex(ReplayProjectError, "API/encoding pair"):
            projects_module._normalise_shader_inventory(unsupported)

    def test_direct_inventory_returns_atomic_verified_reuse_snapshot(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "inventory-origin", _dxbc(b"inventory program"), resource_id=1411
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "inventory-origin-workspace",
            capture_id,
            shader,
            "float4 main() : SV_Target { return 0.5; }\n",
        )
        candidate_blob = _dxbc(b"inventory edited program")
        self._with_fully_validated_candidate(status, candidate_blob)
        registered = self.service.register_workspace(status)
        knowledge = registered["knowledge"]
        inventory = self._shader_inventory(
            resource_id=9911,
            raw_sha256=knowledge["rawSha256"],
            layout_hash=knowledge["layoutHash"],
            interface_hash=knowledge["interfaceHash"],
            anchor_event_id=None,
        )

        result = self.service.reuse_snapshot_for_shader_inventory(inventory)

        self.assertEqual(
            result["schema"],
            "renderdoc-ultra-project-shader-inventory-reuse/v1",
        )
        self.assertEqual(result["reusePlan"]["matchedShaderCount"], 1)
        match = result["reusePlan"]["matches"][0]
        self.assertEqual(match["shader"]["resourceId"], 9911)
        self.assertFalse(match["targetAnchorAvailable"])
        candidate = match["workspaceCandidates"][0]
        self.assertEqual(candidate["matchMode"], "raw-exact")
        self.assertTrue(candidate["binaryCandidateEligible"])
        self.assertFalse(candidate["binaryExecutableAutoApplyEligible"])
        self.assertTrue(candidate["targetAnchorRequiredBeforeExecutableApply"])
        artifact = result["knowledge"]["workspaces"][
            knowledge["knowledgeId"]
        ]
        self.assertEqual(
            Path(artifact["source"]["artifactPath"]).read_text(encoding="utf-8"),
            "float4 main() : SV_Target { return 0.5; }\n",
        )
        self.assertEqual(
            Path(artifact["compiledCandidate"]["artifactPath"]).read_bytes(),
            candidate_blob,
        )
        self.assertEqual(
            artifact["compiledCandidate"]["compilerBindingSha256"],
            status["compilerBinding"]["bindingSha256"],
        )
        self.assertFalse(artifact["originResourceIdReused"])
        self.assertEqual(artifact["registeredWorkspace"], {
            "workspaceId": status["workspaceId"], "path": status["workspacePath"]})
        serialized = json.dumps(result["knowledge"], sort_keys=True)
        self.assertNotIn("captureSessionId", serialized)
        self.assertNotIn("replacementId", serialized)
        self.assertNotIn("1411", serialized)
        self.assertEqual(
            self.service.validate_shader_inventory_snapshot(
                inventory, result["binding"]
            )["snapshotSha256"],
            result["snapshotSha256"],
        )
        # An old cached projection has no registered-workspace field. Its
        # unchanged binding is only a hint: validation must rebuild knowledge
        # from the authenticated manifest rather than return that projection.
        legacy = copy.deepcopy(result)
        legacy["knowledge"]["workspaces"][knowledge["knowledgeId"]].pop("registeredWorkspace")
        refreshed = self.service.validate_shader_inventory_snapshot(inventory, legacy["binding"])
        self.assertEqual(refreshed["knowledge"]["workspaces"][knowledge["knowledgeId"]]["registeredWorkspace"], {
            "workspaceId": status["workspaceId"], "path": status["workspacePath"]})

    def test_incomplete_inventory_interface_only_prepares_verified_source(self) -> None:
        for origin_complete, target_complete in ((True, False), (False, True), (False, False)):
            with self.subTest(origin_complete=origin_complete, target_complete=target_complete):
                label = "source-only-%s-%s" % (origin_complete, target_complete)
                catalog, shader, capture_id = self._catalog(label, _dxbc(label.encode()), resource_id=1641)
                catalog_value = json.loads(catalog.read_text(encoding="utf-8"))
                catalog_value["shaders"][0]["interfaceComplete"] = origin_complete
                catalog.write_text(json.dumps(catalog_value), encoding="utf-8")
                self.service.import_capture(catalog)
                source = "float4 main() : SV_Target { return 0.75; }\n"
                status = self._workspace_status(label + "-workspace", capture_id, shader, source)
                self._with_fully_validated_candidate(status, _dxbc((label + "-edited").encode()))
                knowledge = self.service.register_workspace(status)["knowledge"]
                self.assertIs(knowledge["interfaceComplete"], origin_complete)
                self.assertIsNotNone(knowledge["compiledCandidate"])
                inventory = self._shader_inventory(resource_id=9961, raw_sha256=knowledge["rawSha256"],
                    layout_hash=knowledge["layoutHash"], interface_hash=knowledge["interfaceHash"])
                inventory["shaders"][0]["interfaceComplete"] = target_complete
                before = self.service.manifest_path.read_bytes()

                snapshot = self.service.reuse_snapshot_for_shader_inventory(inventory)

                candidate = snapshot["reusePlan"]["matches"][0]["workspaceCandidates"][0]
                self.assertTrue(candidate["sourcePreparationOnly"])
                self.assertTrue(candidate["sourceSeedEligible"])
                self.assertTrue(candidate["activeCandidate"])
                self.assertEqual(candidate["matchMode"], "raw-exact")
                self.assertFalse(candidate["binaryCandidateEligible"])
                self.assertIsNone(candidate["binaryCandidate"])
                self.assertFalse(candidate["binaryExecutableAutoApplyEligible"])
                record = snapshot["knowledge"]["workspaces"][knowledge["knowledgeId"]]
                self.assertIs(record["interfaceComplete"], origin_complete)
                self.assertIs(snapshot["inventory"]["shaders"][0]["interfaceComplete"], target_complete)
                self.assertEqual(record["physicalIdentity"]["stableKeySha256"], inventory["shaders"][0]["stableKeySha256"])
                self.assertIsNone(record["compiledCandidate"])
                self.assertFalse(any(item["kind"] == "verified-compiled-candidate"
                                     for item in snapshot["binding"]["artifactRefs"]))
                self.assertEqual(Path(record["source"]["artifactPath"]).read_text(encoding="utf-8"), source)
                for flag in ("rawExactSourcePreparationAllowed", "stableKeyExactRequiredForBinary",
                             "liveAbiRevalidationRequiredBeforeShaderApply"):
                    self.assertTrue(snapshot["reusePlan"]["policy"][flag])
                self.assertFalse(snapshot["reusePlan"]["policy"]["stableKeyExactRequiredForSource"])
                self.assertFalse(snapshot["reusePlan"]["policy"]["stableKeyExactRequiredForSourceOrBinary"])
                self.assertEqual(self.service.manifest_path.read_bytes(), before)
                self.assertEqual(self.service.restore_knowledge_snapshot_for_shader_inventory(
                    inventory, expected_binding=snapshot["binding"])["snapshotSha256"], snapshot["snapshotSha256"])

    def test_source_preparation_does_not_bypass_physical_identity_or_content(self) -> None:
        catalog, shader, capture_id = self._catalog("source-only-negative", _dxbc(b"source-only-negative"), resource_id=1642)
        self.service.import_capture(catalog)
        knowledge = self.service.register_workspace(self._workspace_status(
            "source-only-negative-workspace", capture_id, shader,
            "float4 main() : SV_Target { return 0.5; }\n"))["knowledge"]
        options = dict(resource_id=9962, raw_sha256=knowledge["rawSha256"],
                       layout_hash=knowledge["layoutHash"], interface_hash=knowledge["interfaceHash"])
        for field, replacement in (("raw_sha256", "e" * 64), ("api", "d3d12")):
            with self.subTest(field=field):
                inventory = self._shader_inventory(**{**options, field: replacement})
                inventory["shaders"][0]["interfaceComplete"] = False
                result = self.service.reuse_snapshot_for_shader_inventory(inventory)
                self.assertFalse(any(match["workspaceCandidates"] for match in result["reusePlan"]["matches"]))
                self.assertEqual(result["knowledge"]["workspaces"], {})
                self.assertEqual(result["binding"]["knowledgeIds"]["workspaces"], [])
                self.assertEqual(result["reusePlan"]["matches"], [])
        inventory = self._shader_inventory(**options)
        inventory["shaders"][0]["interfaceComplete"] = False
        source_path = self.project / knowledge["sourcePath"]
        source_path.write_text("// tampered immutable project source\n", encoding="utf-8")
        with self.assertRaisesRegex(ReplayProjectError, "source.*(content|hash|changed)"):
            self.service.reuse_snapshot_for_shader_inventory(inventory)

    def test_raw_exact_source_survives_old_abi_and_layout_without_binary_authority(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "changed-reflection-origin", _dxbc(b"same original program"), resource_id=1644)
        self.service.import_capture(catalog)
        source_text = "float4 main() : SV_Target { return 0.25; }\n"
        origin = self._workspace_status("changed-reflection-source", capture_id, shader, source_text)
        self._with_fully_validated_candidate(origin, _dxbc(b"old compiled candidate"))
        knowledge = self.service.register_workspace(origin)["knowledge"]
        original_manifest = self.service.manifest_path.read_bytes()

        for field, replacement in (("abi_hash", "d" * 64), ("layout_hash", "e" * 64)):
            with self.subTest(field=field):
                options = dict(resource_id=9964, raw_sha256=knowledge["rawSha256"],
                               layout_hash=knowledge["layoutHash"], interface_hash=knowledge["interfaceHash"])
                inventory = self._shader_inventory(**{**options, field: replacement})
                snapshot = self.service.reuse_snapshot_for_shader_inventory(inventory)
                match, = snapshot["reusePlan"]["matches"]
                candidate, = match["workspaceCandidates"]
                self.assertEqual(candidate["matchMode"], "raw-exact")
                self.assertTrue(candidate["sourceSeedEligible"])
                self.assertTrue(candidate["sourcePreparationOnly"])
                self.assertTrue(candidate["requiresFreshCompile"])
                self.assertFalse(candidate["stableKeyMatched"])
                self.assertFalse(candidate["binaryCandidateEligible"])
                self.assertIsNone(candidate["binaryCandidate"])
                artifact = snapshot["knowledge"]["workspaces"][knowledge["knowledgeId"]]
                self.assertEqual(artifact["physicalIdentity"]["abiHash"], knowledge["baselineAbiSha256"])
                self.assertEqual(artifact["physicalIdentity"]["layoutHash"], knowledge["layoutHash"])
                self.assertEqual(candidate["originStableKeySha256"], artifact["physicalIdentity"]["stableKeySha256"])
                self.assertNotEqual(candidate["originStableKeySha256"], candidate["targetStableKeySha256"])
                self.assertIsNone(artifact["compiledCandidate"])
                self.assertFalse(any(ref["kind"] == "verified-compiled-candidate"
                                     for ref in snapshot["binding"]["artifactRefs"]))
                self.assertEqual(Path(artifact["source"]["artifactPath"]).read_text(encoding="utf-8"), source_text)

                target_shader = {**shader, "shaderResourceId": 9964}
                destination = self._workspace_status("changed-reflection-target-" + field,
                                                     "f" * 64, target_shader, "// fresh source\n")
                destination["baseline"]["abiSha256"] = inventory["shaders"][0]["abiHash"]
                mismatched_target = copy.deepcopy(destination)
                mismatched_target["baseline"]["abiSha256"] = "9" * 64
                with self.assertRaisesRegex(ReplayProjectError, "identity differs from inventory"):
                    self.service.seed_workspace_from_inventory(knowledge["knowledgeId"], mismatched_target,
                        inventory["shaders"][0], inventory, snapshot["binding"])
                seeded = self.service.seed_workspace_from_inventory(knowledge["knowledgeId"], destination,
                    inventory["shaders"][0], inventory, snapshot["binding"])
                self.assertEqual(seeded["matchMode"], "raw-exact")
                self.assertTrue(seeded["requiresCompile"])
                self.assertTrue(seeded["requiresLiveAbiRevalidation"])
                self.assertFalse(seeded["verifiedBinaryCandidateMaySkipCompile"])
                self.assertFalse(seeded["applied"])
                self.assertEqual(Path(destination["source"]["path"]).read_text(encoding="utf-8"), source_text)
                self.assertEqual(self.service.manifest_path.read_bytes(), original_manifest)

    def test_reuse_policy_change_invalidates_legacy_negative_routing_cache(self) -> None:
        catalog, shader, capture_id = self._catalog("old-negative-cache", _dxbc(b"old-negative-cache"), resource_id=1643)
        self.service.import_capture(catalog)
        self.service.register_workspace(self._workspace_status(
            "old-negative-cache-workspace", capture_id, shader, "float4 main() : SV_Target { return 1; }\n"))
        manifest = self.service._load()
        capture = self.service._capture(manifest, capture_id, "")
        state = self.service._reuse_knowledge_state(manifest)
        old_policy_sha256 = hashlib.sha256(b"raw-exact-or-code-interface-layout-exact-v1").hexdigest()
        with mock.patch.object(projects_module, "REUSE_MATCH_POLICY_SHA256", old_policy_sha256):
            old_binding = self.service._reuse_cache_binding(manifest, capture, state["knowledgeContentSha256"])
            unused_stored, old_cache_path = self.service._write_reuse_cache(old_binding, [])
        old_cache_bytes = old_cache_path.read_bytes()
        plan = self.service.reuse_plan(capture_id)
        self.assertEqual(plan["matchedShaderCount"], 1)
        self.assertFalse(plan["derivedCache"]["hit"])
        self.assertNotEqual(plan["derivedCache"]["cacheKey"], old_binding["cacheKey"])
        self.assertNotEqual(plan["derivedCache"]["matcherPolicySha256"], old_policy_sha256)
        self.assertEqual(old_cache_path.read_bytes(), old_cache_bytes)

    def test_inventory_snapshot_uses_latest_eligible_compiled_active_head_lazily(
        self,
    ) -> None:
        catalog, shader, capture_id = self._catalog(
            "inventory-active-head",
            _dxbc(b"inventory active-head baseline"),
            resource_id=1412,
        )
        self.service.import_capture(catalog)

        status_a = self._workspace_status(
            "inventory-active-head-a",
            capture_id,
            shader,
            "// explicitly published HLSL A\n",
        )
        compiled_a = _dxbc(b"inventory active-head compiled A")
        self._with_fully_validated_candidate(status_a, compiled_a, revision=3)
        record_a = self.service.register_workspace(status_a)["knowledge"]
        manifest_after_a = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        revision_after_a = manifest_after_a["activeKnowledge"]["revision"]

        inventory_a = self._shader_inventory(
            resource_id=9916,
            raw_sha256=record_a["rawSha256"],
            layout_hash=record_a["layoutHash"],
            interface_hash=record_a["interfaceHash"],
            anchor_event_id=23,
        )
        snapshot_before = self.service.reuse_snapshot_for_shader_inventory(
            inventory_a
        )
        old_rdc_binding = copy.deepcopy(snapshot_before["binding"])
        before_candidate = snapshot_before["reusePlan"]["matches"][0][
            "workspaceCandidates"
        ]
        self.assertEqual(len(before_candidate), 1)
        self.assertEqual(before_candidate[0]["knowledgeId"], record_a["knowledgeId"])
        self.assertTrue(before_candidate[0]["binaryCandidateEligible"])

        status_b = self._workspace_status(
            "inventory-active-head-b",
            capture_id,
            shader,
            "// explicitly published HLSL B\n",
        )
        compiled_b = _dxbc(b"inventory active-head compiled B")
        self._with_fully_validated_candidate(status_b, compiled_b, revision=7)
        record_b = self.service.register_workspace(status_b)["knowledge"]
        manifest_after_b = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )

        self.assertNotEqual(record_a["knowledgeId"], record_b["knowledgeId"])
        self.assertEqual(record_a["rawSha256"], record_b["rawSha256"])
        self.assertEqual(record_a["layoutHash"], record_b["layoutHash"])
        self.assertEqual(
            record_a["baselineAbiSha256"], record_b["baselineAbiSha256"]
        )
        self.assertNotEqual(record_a["sourceSha256"], record_b["sourceSha256"])
        self.assertNotEqual(
            record_a["compiledCandidate"]["rawSha256"],
            record_b["compiledCandidate"]["rawSha256"],
        )
        self.assertEqual(
            manifest_after_b["activeKnowledge"]["revision"],
            revision_after_a + 1,
        )
        self.assertEqual(
            manifest_after_b["activeKnowledge"]["workspaces"][
                record_a["knowledgeId"]
            ],
            revision_after_a,
        )
        self.assertEqual(
            manifest_after_b["activeKnowledge"]["workspaces"][
                record_b["knowledgeId"]
            ],
            revision_after_a + 1,
        )

        inventory_b = self._shader_inventory(
            resource_id=7712,
            raw_sha256=record_b["rawSha256"],
            layout_hash=record_b["layoutHash"],
            interface_hash=record_b["interfaceHash"],
            anchor_event_id=88,
        )
        self.assertEqual(
            inventory_a["shaders"][0]["stableKeySha256"],
            inventory_b["shaders"][0]["stableKeySha256"],
        )

        fresh_same_rdc = self.service.reuse_snapshot_for_shader_inventory(
            inventory_a
        )
        fresh_other_rdc = self.service.reuse_snapshot_for_shader_inventory(
            inventory_b
        )
        for fresh in (fresh_same_rdc, fresh_other_rdc):
            candidates = fresh["reusePlan"]["matches"][0][
                "workspaceCandidates"
            ]
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["knowledgeId"], record_b["knowledgeId"])
            self.assertTrue(candidates[0]["activeCandidate"])
            self.assertTrue(candidates[0]["binaryCandidateEligible"])
            self.assertEqual(
                candidates[0]["binaryCandidate"]["sha256"],
                record_b["compiledCandidate"]["rawSha256"],
            )
            self.assertEqual(
                fresh["binding"]["activeKnowledgeRevision"],
                revision_after_a + 1,
            )

        # Publishing advances only project knowledge. An already embedded RDC
        # binding remains untouched and is rejected lazily when that RDC is
        # opened again; no batch rewrite of historical captures is required.
        self.assertEqual(snapshot_before["binding"], old_rdc_binding)
        self.assertEqual(
            old_rdc_binding["activeKnowledgeRevision"], revision_after_a
        )
        self.assertNotEqual(
            old_rdc_binding["bindingSha256"],
            fresh_same_rdc["binding"]["bindingSha256"],
        )
        with self.assertRaisesRegex(
            ReplayProjectError, "matched knowledge IDs changed"
        ):
            self.service.validate_shader_inventory_snapshot(
                inventory_a, old_rdc_binding
            )

    def test_dxbc_disassembly_is_content_addressed_display_evidence_in_inventory(
        self,
    ) -> None:
        catalog, shader, capture_id = self._catalog(
            "inventory-dxbc-disassembly",
            _dxbc(b"inventory DXBC disassembly"),
            resource_id=1431,
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "inventory-dxbc-disassembly-workspace",
            capture_id,
            shader,
            "float4 main() : SV_Target { return 0.125; }\n",
        )
        evidence_path = (
            Path(status["workspacePath"]) / "evidence" / "shader.dxbc.asm"
        )
        evidence_path.parent.mkdir()
        evidence_path.write_text(
            "ps_5_0\ndcl_output o0.xyzw\nmov o0.xyzw, l(1,1,1,1)\nret\n",
            encoding="utf-8",
        )
        evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        status["assemblyDisassemblyArtifacts"] = [
            {
                "kind": "native-disassembly",
                "role": "baseline",
                "format": "dxbc-asm",
                "path": str(evidence_path),
                "sha256": evidence_sha256,
                "byteSize": evidence_path.stat().st_size,
                "inputFormat": "dxbc",
                "inputRawSha256": shader["rawSha256"],
                "routePlanSha256": "1" * 64,
                "routeId": "project-test-dxbc-disassembly",
                "toolId": "project-test-dxbc-tool",
                "operation": "decompile",
                "toolConfigDigest": "2" * 64,
                "executableSha256": "3" * 64,
                "physicalEvidence": True,
                "displayEvidenceOnly": True,
                "editableSource": False,
                "applyAuthorization": False,
            }
        ]
        registered = self.service.register_workspace(status)["knowledge"]
        stored = registered["assemblyDisassemblyArtifacts"][0]
        stored_path = self.project / stored["artifactPath"]
        self.assertEqual(stored_path.read_bytes(), evidence_path.read_bytes())
        self.assertEqual(stored_path.name, evidence_sha256 + ".dxbc-asm")
        self.assertTrue(stored["displayEvidenceOnly"])
        self.assertFalse(stored["editableSource"])
        self.assertFalse(stored["applyAuthorization"])

        inventory = self._shader_inventory(
            resource_id=9931,
            raw_sha256=registered["rawSha256"],
            layout_hash=registered["layoutHash"],
            interface_hash=registered["interfaceHash"],
        )
        snapshot = self.service.reuse_snapshot_for_shader_inventory(inventory)
        portable = snapshot["knowledge"]["workspaces"][
            registered["knowledgeId"]
        ]["assemblyDisassemblyArtifacts"][0]
        self.assertEqual(Path(portable["artifactPath"]), stored_path)
        self.assertEqual(portable["format"], "dxbc-asm")
        self.assertTrue(portable["displayEvidenceOnly"])
        self.assertFalse(portable["applyAuthorization"])
        disassembly_refs = [
            item
            for item in snapshot["binding"]["artifactRefs"]
            if item["kind"] == "verified-native-disassembly"
        ]
        self.assertEqual(len(disassembly_refs), 1)
        self.assertEqual(disassembly_refs[0]["sha256"], evidence_sha256)

        stored_path.write_text("ps_5_0\nret\n", encoding="utf-8")
        with self.assertRaisesRegex(
            ReplayProjectError, "disassembly artifact content/hash mismatch"
        ):
            self.service.reuse_snapshot_for_shader_inventory(inventory)

    def test_direct_inventory_allows_multiple_entry_points_on_one_resource(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "multi-entry-origin", _dxbc(b"multi-entry program"), resource_id=1441
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "multi-entry-workspace",
            capture_id,
            shader,
            "float4 main() : SV_Target { return 0.75; }\n",
        )
        knowledge = self.service.register_workspace(status)["knowledge"]
        inventory = self._shader_inventory(
            resource_id=9941,
            raw_sha256=knowledge["rawSha256"],
            layout_hash=knowledge["layoutHash"],
            interface_hash=knowledge["interfaceHash"],
            anchor_event_id=41,
        )
        second = copy.deepcopy(inventory["shaders"][0])
        second["entryPoint"] = "secondary"
        second_identity = {
            field: second[field]
            for field in (
                "api",
                "encoding",
                "rawSha256",
                "stage",
                "entryPoint",
                "abiHash",
                "layoutHash",
            )
        }
        second["stableKeySha256"] = projects_module._canonical_sha256(
            {**second_identity, "encoding": "dxbc"}
        )
        inventory["shaders"].append(second)
        inventory["capture"]["captureId"] = projects_module._canonical_sha256(
            sorted(item["stableKeySha256"] for item in inventory["shaders"])
        )
        inventory_material = {
            "api": "d3d11",
            "shaders": [
                {
                    "resourceId": item["resourceId"],
                    "stableKeySha256": item["stableKeySha256"],
                    "anchorEventId": item["anchorEventId"],
                    "constantBlocks": item["constantBlocks"],
                }
                for item in sorted(
                    inventory["shaders"],
                    key=lambda value: (
                        value["resourceId"],
                        value["stage"],
                        value["entryPoint"],
                        value["rawSha256"],
                    ),
                )
            ],
        }
        inventory_sha256 = projects_module._canonical_sha256(inventory_material)
        inventory["capture"]["catalogSha256"] = inventory_sha256
        inventory["capture"]["inventorySha256"] = inventory_sha256

        result = self.service.reuse_snapshot_for_shader_inventory(inventory)

        self.assertEqual(len(result["inventory"]["shaders"]), 2)
        self.assertEqual(
            {item["entryPoint"] for item in result["inventory"]["shaders"]},
            {"main", "secondary"},
        )
        self.assertEqual(result["reusePlan"]["matchedShaderCount"], 1)
        self.assertEqual(result["reusePlan"]["matches"][0]["shader"]["entryPoint"], "main")

    def test_inventory_registration_is_artifact_idempotent_across_rdcs(self) -> None:
        blob = _dxbc(b"portable inventory baseline")
        unused_catalog, shader, unused_capture = self._catalog(
            "inventory-register-physical", blob, resource_id=1701
        )
        inventory_a = self._shader_inventory(
            resource_id=9915,
            raw_sha256=shader["rawSha256"],
            layout_hash="1" * 64,
            interface_hash="2" * 64,
            anchor_event_id=23,
        )
        target_a = dict(shader)
        target_a["shaderResourceId"] = 9915
        status_a = self._workspace_status(
            "inventory-register-rdc-a",
            inventory_a["capture"]["captureId"],
            target_a,
            "// portable inventory HLSL\n",
        )
        candidate_blob = _dxbc(b"portable compiled candidate")
        self._with_fully_validated_candidate(
            status_a, candidate_blob, revision=3
        )

        first = self.service.register_workspace_from_inventory(
            status_a,
            inventory_a["shaders"][0],
            inventory_a,
            self.service.reuse_snapshot_for_shader_inventory(inventory_a)[
                "binding"
            ],
        )

        inventory_b = self._shader_inventory(
            resource_id=7711,
            raw_sha256=shader["rawSha256"],
            layout_hash="1" * 64,
            interface_hash="2" * 64,
            anchor_event_id=88,
        )
        target_b = dict(shader)
        target_b["shaderResourceId"] = 7711
        status_b = self._workspace_status(
            "inventory-register-rdc-b",
            inventory_b["capture"]["captureId"],
            target_b,
            "// portable inventory HLSL\n",
        )
        (Path(status_b["workspacePath"]) / "ai" / "analysis.md").write_text(
            "Different explanatory notes must not fork physical knowledge.\n",
            encoding="utf-8",
        )
        # Workspace identity and validation revision are capture-local audit
        # details. They must not fork the same portable project artifact.
        self._with_fully_validated_candidate(
            status_b, candidate_blob, revision=9
        )
        second = self.service.register_workspace_from_inventory(
            status_b,
            inventory_b["shaders"][0],
            inventory_b,
            self.service.reuse_snapshot_for_shader_inventory(inventory_b)[
                "binding"
            ],
        )

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertTrue(second["observationRecorded"])
        self.assertEqual(
            first["knowledge"]["knowledgeId"],
            second["knowledge"]["knowledgeId"],
        )
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(manifest["knowledge"]["workspaces"]), 1)
        observations = manifest["knowledge"]["workspaces"][0][
            "inventoryObservations"
        ]
        self.assertEqual(len(observations), 2)
        self.assertEqual(
            {item["targetResourceId"] for item in observations},
            {9915, 7711},
        )
        self.assertEqual(
            len({item["receiptSha256"] for item in observations}), 2
        )
        self.assertTrue(
            all(item["applyAuthorization"] is False for item in observations)
        )
        self.assertTrue(
            all(
                item["crossCaptureResourceIdReuse"] is False
                for item in observations
            )
        )

        repeated = self.service.register_workspace_from_inventory(
            status_b, inventory_b["shaders"][0], inventory_b
        )
        self.assertTrue(repeated["idempotent"])
        self.assertFalse(repeated["observationRecorded"])
        self.assertFalse(repeated["headChanged"])
        self.assertEqual(repeated["binding"], second["binding"])
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            len(
                manifest["knowledge"]["workspaces"][0][
                    "inventoryObservations"
                ]
            ),
            2,
        )
        manifest["knowledge"]["workspaces"][0]["inventoryObservations"][0][
            "targetResourceId"
        ] += 1
        (self.project / "project.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            ReplayProjectError, "inventory observation hash mismatch"
        ):
            self.service.status()

    def test_direct_inventory_recomputes_hashes_and_recovers_source_after_abi_drift(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "inventory-hash-origin", _dxbc(b"inventory hash"), resource_id=1511
        )
        self.service.import_capture(catalog)
        registered = self.service.register_workspace(
            self._workspace_status(
                "inventory-hash-workspace",
                capture_id,
                shader,
                "// exact source\n",
            )
        )
        knowledge = registered["knowledge"]
        inventory = self._shader_inventory(
            resource_id=9912,
            raw_sha256=knowledge["rawSha256"],
            layout_hash=knowledge["layoutHash"],
            interface_hash=knowledge["interfaceHash"],
        )
        tampered = json.loads(json.dumps(inventory))
        tampered["shaders"][0]["resourceId"] = 9913
        with self.assertRaisesRegex(
            ReplayProjectError, "aggregate hash mismatch"
        ):
            self.service.reuse_plan_for_shader_inventory(tampered)

        drifted = self._shader_inventory(
            resource_id=9912,
            raw_sha256=knowledge["rawSha256"],
            layout_hash=knowledge["layoutHash"],
            interface_hash=knowledge["interfaceHash"],
            abi_hash="d" * 64,
        )
        plan = self.service.reuse_plan_for_shader_inventory(drifted)
        self.assertEqual(plan["matchedShaderCount"], 1)
        candidate, = plan["matches"][0]["workspaceCandidates"]
        self.assertTrue(candidate["sourceSeedEligible"])
        self.assertTrue(candidate["sourcePreparationOnly"])
        self.assertTrue(candidate["requiresFreshCompile"])
        self.assertFalse(candidate["binaryCandidateEligible"])
        self.assertFalse(candidate["stableKeyMatched"])
        self.assertIsNone(candidate["binaryCandidate"])
        self.assertFalse(plan["policy"]["fuzzyExecutableMatchAllowed"])

    def test_seed_workspace_from_inventory_uses_fresh_resource_and_abi(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "inventory-seed-origin", _dxbc(b"inventory seed"), resource_id=1611
        )
        self.service.import_capture(catalog)
        registered = self.service.register_workspace(
            self._workspace_status(
                "inventory-seed-source",
                capture_id,
                shader,
                "// reusable inventory HLSL\n",
            )
        )
        knowledge = registered["knowledge"]
        inventory = self._shader_inventory(
            resource_id=9914,
            raw_sha256=knowledge["rawSha256"],
            layout_hash=knowledge["layoutHash"],
            interface_hash=knowledge["interfaceHash"],
        )
        target_shader = dict(shader)
        target_shader["shaderResourceId"] = 9914
        destination = self._workspace_status(
            "inventory-seed-target",
            "e" * 64,
            target_shader,
            "// fresh decompile\n",
        )
        snapshot = self.service.reuse_snapshot_for_shader_inventory(inventory)

        seeded = self.service.seed_workspace_from_inventory(
            knowledge["knowledgeId"],
            destination,
            inventory["shaders"][0],
            inventory,
            snapshot["binding"],
        )

        self.assertEqual(seeded["targetResourceId"], 9914)
        self.assertFalse(seeded["oldResourceIdReused"])
        self.assertFalse(seeded["oldReplacementIdReused"])
        self.assertFalse(seeded["oldCaptureSessionReused"])
        self.assertTrue(seeded["requiresLiveAbiRevalidation"])
        self.assertEqual(
            Path(destination["source"]["path"]).read_text(encoding="utf-8"),
            "// reusable inventory HLSL\n",
        )

        wrong_profile = self._workspace_status(
            "inventory-seed-wrong-profile",
            "f" * 64,
            target_shader,
            "// fresh decompile\n",
        )
        wrong_profile["targetProfile"]["profile"] = "ps_4_0"
        with self.assertRaisesRegex(ReplayProjectError, "target profile"):
            self.service.seed_workspace_from_inventory(
                knowledge["knowledgeId"],
                wrong_profile,
                inventory["shaders"][0],
                inventory,
            )

    def _published_source_advance_fixture(self):
        catalog, shader, capture_id = self._catalog("published-advance", _dxbc(), resource_id=1611)
        self.service.import_capture(catalog)
        previous = self._with_fully_validated_candidate(self._workspace_status(
            "published-previous", capture_id, shader, "float4 main():SV_Target { return 0.75; }\n"),
            _dxbc(b"previous candidate"), revision=21)
        old = self.service.register_workspace(previous)["knowledge"]
        current = self._with_fully_validated_candidate(self._workspace_status(
            "published-current", capture_id, shader, "float4 main():SV_Target { return 0.25; }\n"),
            _dxbc(b"current candidate"), revision=6)
        head = self.service.register_workspace(current)["knowledge"]
        self.published_head_status = current
        previous["capture"]["api"] = "D3D11"
        previous["source"]["modifiedSinceDecompile"] = True
        manifest = Path(previous["manifestPath"])
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        saved.update(workspaceId=previous["workspaceId"], baseline=previous["baseline"],
                     targetProfile=previous["targetProfile"], stage=previous["stage"],
                     entryPoint=previous["entryPoint"], source={
                         "path": "source/shader.hlsl", "sha256": previous["source"]["currentSha256"],
                         "entryPoint": previous["entryPoint"]})
        manifest.write_text(json.dumps(saved), encoding="utf-8")
        inventory = self._shader_inventory(resource_id=1611, raw_sha256=head["rawSha256"],
            layout_hash=head["layoutHash"], interface_hash=head["interfaceHash"])
        receipt = self.service.published_workspace_source_advance(head["knowledgeId"], previous)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["previousKnowledgeId"], old["knowledgeId"])
        return previous, head, inventory, receipt

    def test_unchanged_previous_publication_advances_head_with_backup(self):
        previous, head, inventory, receipt = self._published_source_advance_fixture()
        source = Path(previous["source"]["path"])
        old_bytes = source.read_bytes()
        with self.assertRaisesRegex(ReplayProjectError, "still unmodified"):
            self.service.seed_workspace_from_inventory(head["knowledgeId"], previous,
                inventory["shaders"][0], inventory)
        seeded = self.service.seed_workspace_from_inventory(head["knowledgeId"], previous,
            inventory["shaders"][0], inventory, published_source_advance=receipt)
        self.assertEqual(source.read_bytes(), (self.project / head["sourcePath"]).read_bytes())
        self.assertEqual(Path(seeded["supersededLocalEdit"]["backupPath"]).read_bytes(), old_bytes)
        self.assertEqual((Path(previous["workspacePath"]) / "revisions/0021/shader.hlsl").read_bytes(), old_bytes)
        self.assertEqual(seeded["sourceSha256"], head["sourceSha256"])
        from src.shader_workspace.project_source import validate_project_source
        updated = json.loads(Path(previous["manifestPath"]).read_text())
        imported = validate_project_source(Path(previous["workspacePath"]), updated, updated["source"])
        self.assertEqual(imported["knowledgeId"], head["knowledgeId"])
        self.assertEqual(imported["sourceSha256"], head["sourceSha256"])
        self.assertFalse(imported["applied"])
        self.assertTrue(imported["requiresCompile"])

    def test_inventory_seed_retry_renews_missing_receipt_and_retires_old_draft(self):
        self._assert_seed_retry_renews_missing_receipt(inventory_mode=True)

    def test_catalog_seed_retry_renews_missing_receipt_and_retires_old_draft(self):
        self._assert_seed_retry_renews_missing_receipt(inventory_mode=False)

    def _assert_seed_retry_renews_missing_receipt(self, *, inventory_mode):
        previous, head, inventory, receipt = self._published_source_advance_fixture()
        root = Path(previous["workspacePath"])
        source = Path(previous["source"]["path"])
        old_source = source.read_bytes()
        old_sha = hashlib.sha256(old_source).hexdigest()
        # Model the interrupted older seed: canonical bytes advanced, but the
        # captured/decompiled manifest and draft still describe the old text.
        source.write_bytes((self.project / head["sourcePath"]).read_bytes())
        previous["source"]["currentSha256"] = head["sourceSha256"]
        organization = "d" * 32
        directory = root / "ai" / "organization-sessions" / organization
        directory.mkdir(parents=True)
        proposal = directory / "organized-proposal.hlsl"
        proposal.write_bytes(old_source)
        session = directory / "session.json"
        session.write_text(json.dumps({
            "schema": "renderdoc-ai-shader-organization-session/v1",
            "organizationId": organization, "workspaceId": previous["workspaceId"],
            "originalRawSha256": previous["baseline"]["rawSha256"],
            "baselineSourceSha256": old_sha, "syncedDraftSourceSha256": old_sha,
        }))
        gate = root / "ai" / "organize-code-gate.json"
        gate.write_text(json.dumps({
            "schema": "renderdoc-ai-shader-organization-gate/v1",
            "organizationId": organization, "workspaceId": previous["workspaceId"],
            "decision": "editable-draft", "proposalSourceSha256": old_sha,
        }))
        from src.shader_workspace.project_source import archive_superseded_project_source_gate
        self.assertIsNone(archive_superseded_project_source_gate(root))
        before = {path: path.read_bytes() for path in (source, session, proposal)}
        if inventory_mode:
            self.service.seed_workspace_from_inventory(head["knowledgeId"], previous,
                inventory["shaders"][0], inventory)
        else:
            self.service.seed_workspace(head["knowledgeId"], previous)
        self.assertIsNotNone(archive_superseded_project_source_gate(root))
        self.assertFalse(gate.exists())
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_published_source_advance_preserves_unpublished_or_different_owner(self):
        previous, head, inventory, receipt = self._published_source_advance_fixture()
        for change in (
            lambda s: s["source"].update(currentSha256="e" * 64),
            lambda s: s.update(revision=22),
            lambda s: (s.update(revision=22), s["latestCompiled"].update(revision=22)),
            lambda s: s["latestCompiled"].update(rawSha256="e" * 64),
            lambda s: s.update(workspaceId="e" * 32),
            lambda s: s.update(workspacePath=str(self.root / "another-owner")),
            lambda s: s["baseline"].update(rawSha256="e" * 64),
            lambda s: s["targetProfile"].update(profile="ps_4_0"),
            lambda s: s["latestCompiled"].update(replayAbiValidated=False),
        ):
            with self.subTest(change=change):
                status = copy.deepcopy(previous)
                change(status)
                self.assertIsNone(self.service.published_workspace_source_advance(head["knowledgeId"], status))

    def test_published_source_advance_rechecks_head_source_and_candidate_under_locks(self):
        previous, head, inventory, receipt = self._published_source_advance_fixture()
        source = Path(previous["source"]["path"])
        old_bytes = source.read_bytes()
        for field in ("generation", "knowledgeId", "previousKnowledgeId", "candidateRawSha256"):
            with self.subTest(field=field):
                changed = copy.deepcopy(receipt)
                changed[field] = changed[field] + 1 if field == "generation" else "e" * len(changed[field])
                with self.assertRaisesRegex(ReplayProjectError, "source/head changed"):
                    self.service.seed_workspace_from_inventory(head["knowledgeId"], previous,
                        inventory["shaders"][0], inventory, published_source_advance=changed)
                self.assertEqual(source.read_bytes(), old_bytes)
        source.write_bytes(b"new manual edit")
        with self.assertRaisesRegex(ReplayProjectError, "source changed after"):
            self.service.seed_workspace_from_inventory(head["knowledgeId"], previous,
                inventory["shaders"][0], inventory, published_source_advance=receipt)
        self.assertEqual(source.read_bytes(), b"new manual edit")
        source.write_bytes(old_bytes)
        manifest = Path(previous["manifestPath"])
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        saved["revision"] += 1
        manifest.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaisesRegex(ReplayProjectError, "revision changed"):
            self.service.seed_workspace_from_inventory(head["knowledgeId"], previous,
                inventory["shaders"][0], inventory, published_source_advance=receipt)
        self.assertEqual(source.read_bytes(), old_bytes)

    def test_published_source_advance_rejects_head_changed_since_read(self):
        previous, head, inventory, receipt = self._published_source_advance_fixture()
        # Publish a third source under the same physical head after admission.
        source = Path(previous["source"]["path"])
        old_bytes = source.read_bytes()
        status = copy.deepcopy(previous)
        source.write_bytes(b"// next project head\n")
        status["source"]["currentSha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        status.pop("latestCompiled")
        self.service.register_workspace(status, include_compiled_candidate=False)
        source.write_bytes(old_bytes)
        with self.assertRaisesRegex(ReplayProjectError, "active knowledge head|not matched|source/head changed before seed"):
            self.service.seed_workspace_from_inventory(head["knowledgeId"], previous,
                inventory["shaders"][0], inventory, published_source_advance=receipt)
        self.assertEqual(source.read_bytes(), old_bytes)

    def test_published_source_advance_rejects_same_head_id_at_new_generation(self):
        previous, head, inventory, receipt = self._published_source_advance_fixture()
        expected = {key: receipt[key] for key in ("headKey", "generation", "knowledgeId")}
        switched = self.service.register_workspace(previous, expected_head=expected)
        restored = self.service.register_workspace(self.published_head_status, expected_head=switched["head"])
        self.assertEqual(restored["head"]["knowledgeId"], receipt["knowledgeId"])
        self.assertGreater(restored["head"]["generation"], receipt["generation"])
        before = Path(previous["source"]["path"]).read_bytes()
        with self.assertRaisesRegex(ReplayProjectError, "source/head changed"):
            self.service.seed_workspace_from_inventory(head["knowledgeId"], previous,
                inventory["shaders"][0], inventory, published_source_advance=receipt)
        self.assertEqual(Path(previous["source"]["path"]).read_bytes(), before)

    def test_pending_binary_is_not_published_and_legacy_source_stays_reusable(self) -> None:
        catalog_a, shader_a, capture_a = self._catalog(
            "pending-binary-a", _dxbc(b"pending binary"), resource_id=31
        )
        catalog_b, unused_shader_b, unused_capture_b = self._catalog(
            "pending-binary-b", _dxbc(b"pending binary"), resource_id=32
        )
        self.service.import_capture(catalog_a)
        status = self._workspace_status(
            "pending-binary-workspace", capture_a, shader_a, "// pending source\n"
        )
        status["latestCompiled"] = {
            "replayAbiValidated": False,
            "abiCompatible": False,
            "requiresReplayAbiValidation": True,
            "validationScope": "container-structure-only",
        }
        status["replayAbiValidated"] = False
        status["abiCompatible"] = False
        registered = self.service.register_workspace(status)
        self.assertNotIn("compiledCandidate", registered["knowledge"])

        plan = self.service.import_capture(catalog_b)
        candidate = plan["matches"][0]["workspaceCandidates"][0]
        self.assertTrue(candidate["sourceSeedEligible"])
        self.assertFalse(candidate["binaryCandidateEligible"])
        self.assertIsNone(candidate["binaryCandidate"])

    def test_legacy_source_without_baseline_abi_is_recovered_by_exact_raw_only(self) -> None:
        original_blob = _dxbc(b"legacy absent ABI", b"original reflection")
        catalog, shader, capture_id = self._catalog(
            "missing-abi-origin", original_blob, resource_id=1711)
        self.service.import_capture(catalog)
        source_text = "float4 main() : SV_Target { return 0.5; }\n"
        origin = self._workspace_status("missing-abi-source", capture_id, shader, source_text)
        origin["baseline"].pop("abiSha256")
        knowledge = self.service.register_workspace(origin)["knowledge"]
        self.assertEqual(knowledge["baselineAbiSha256"], "")
        self.assertNotIn("compiledCandidate", knowledge)

        target_catalog, target_shader, target_capture = self._catalog(
            "missing-abi-target", original_blob, resource_id=9711)
        plan = self.service.import_capture(target_catalog)
        candidate, = plan["matches"][0]["workspaceCandidates"]
        self.assertEqual(candidate["matchMode"], "raw-exact")
        self.assertTrue(candidate["sourceSeedEligible"])
        self.assertFalse(candidate["binaryCandidateEligible"])
        self.assertIsNone(candidate["binaryCandidate"])
        self.assertIn("fresh compile", candidate["binaryBlockedReason"])

        destination = self._workspace_status(
            "missing-abi-seed", target_capture, target_shader, "// fresh decompile\n")
        destination["baseline"]["abiSha256"] = "d" * 64
        seeded = self.service.seed_workspace(knowledge["knowledgeId"], destination)
        self.assertTrue(seeded["requiresCompile"])
        self.assertTrue(seeded["requiresLiveAbiRevalidation"])
        self.assertFalse(seeded["applied"])
        self.assertEqual(Path(destination["source"]["path"]).read_text(encoding="utf-8"), source_text)

        destination = self._workspace_status(
            "missing-abi-prepare", target_capture, target_shader, "temporary")
        Path(destination["source"]["path"]).unlink()
        destination["source"]["present"] = False
        destination["capture"]["api"] = "D3D11"
        destination["baseline"]["abiSha256"] = "d" * 64
        bundle = self.service.prepare_workspace_source(knowledge["knowledgeId"], destination)
        self.assertEqual(bundle["sourceText"].encode("utf-8"), Path(origin["source"]["path"]).read_bytes())
        self.assertEqual(bundle["baselineAbiSha256"], "d" * 64)
        self.assertTrue(bundle["requiresCompile"])
        self.assertTrue(bundle["requiresLiveAbiRevalidation"])
        self.assertFalse(bundle["applied"])

        inventory = self._shader_inventory(resource_id=9711, raw_sha256=knowledge["rawSha256"],
            layout_hash=knowledge["layoutHash"], interface_hash=knowledge["interfaceHash"], abi_hash="d" * 64)
        snapshot = self.service.reuse_snapshot_for_shader_inventory(inventory)
        candidate, = snapshot["reusePlan"]["matches"][0]["workspaceCandidates"]
        self.assertTrue(candidate["sourceSeedEligible"])
        self.assertTrue(candidate["sourcePreparationOnly"])
        self.assertTrue(candidate["requiresFreshCompile"])
        self.assertFalse(candidate["binaryCandidateEligible"])
        artifact = snapshot["knowledge"]["workspaces"][knowledge["knowledgeId"]]
        self.assertEqual(artifact["physicalIdentity"]["abiHash"], "")
        self.assertIsNone(artifact["compiledCandidate"])

        changed_catalog, changed_shader, changed_capture = self._catalog(
            "missing-abi-code-only", _dxbc(b"legacy absent ABI", b"different reflection"), resource_id=9712)
        changed_plan = self.service.import_capture(changed_catalog)
        changed_candidate, = changed_plan["matches"][0]["workspaceCandidates"]
        self.assertEqual(changed_candidate["matchMode"], "code-interface-layout-exact")
        self.assertFalse(changed_candidate["sourceSeedEligible"])
        self.assertFalse(changed_candidate["binaryCandidateEligible"])
        changed_destination = self._workspace_status(
            "missing-abi-code-target", changed_capture, changed_shader, "// fresh decompile\n")
        with self.assertRaisesRegex(ReplayProjectError, "physical ABI"):
            self.service.seed_workspace(knowledge["knowledgeId"], changed_destination)

    def test_compiled_candidate_tampering_fails_closed(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "tampered-binary", _dxbc(b"tamper baseline"), resource_id=61
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "tampered-binary-workspace", capture_id, shader, "// tamper source\n"
        )
        self._with_fully_validated_candidate(
            status, _dxbc(b"tamper candidate")
        )
        registered = self.service.register_workspace(status)
        artifact = self.project / registered["knowledge"]["compiledCandidate"][
            "artifactPath"
        ]
        artifact.write_bytes(_dxbc(b"replacement tamper"))

        with self.assertRaisesRegex(
            ReplayProjectError, "compiled candidate content/hash mismatch"
        ):
            self.service.status()

    def test_stale_validated_binary_is_omitted_after_source_changes(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "source-revision-mismatch",
            _dxbc(b"source revision baseline"),
            resource_id=71,
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "source-revision-workspace",
            capture_id,
            shader,
            "// source revision one\n",
        )
        self._with_fully_validated_candidate(
            status, _dxbc(b"source revision candidate")
        )
        Path(status["source"]["path"]).write_text(
            "// source revision two\n", encoding="utf-8"
        )

        registered = self.service.register_workspace(status)
        knowledge = registered["knowledge"]
        self.assertNotIn("compiledCandidate", knowledge)
        self.assertEqual(
            (self.project / knowledge["sourcePath"]).read_text(encoding="utf-8"),
            "// source revision two\n",
        )
        self.assertEqual(
            knowledge["sourceSha256"],
            hashlib.sha256(
                Path(status["source"]["path"]).read_bytes()
            ).hexdigest(),
        )

    def test_stale_validated_binary_still_rejects_tampered_receipt(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "stale-candidate-tamper",
            _dxbc(b"stale candidate baseline"),
            resource_id=72,
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "stale-candidate-tamper-workspace",
            capture_id,
            shader,
            "// compiled source\n",
        )
        self._with_fully_validated_candidate(
            status, _dxbc(b"stale candidate compiled program")
        )
        Path(status["source"]["path"]).write_text(
            "// newer source\n", encoding="utf-8"
        )
        report_path = (
            Path(status["workspacePath"])
            / status["latestCompiled"]["abiGate"]["reportPath"]
        )
        report_path.write_text('{"compatible":false}\n', encoding="utf-8")

        with self.assertRaisesRegex(
            ReplayProjectError, "workspace ABI report hash mismatch"
        ):
            self.service.register_workspace(status)

    def test_current_candidate_rejects_changed_compiled_source_revision(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "current-candidate-source-tamper",
            _dxbc(b"current candidate baseline"),
            resource_id=73,
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "current-candidate-source-tamper-workspace",
            capture_id,
            shader,
            "// current compiled source\n",
        )
        self._with_fully_validated_candidate(
            status, _dxbc(b"current candidate compiled program")
        )
        revision_source = (
            Path(status["workspacePath"])
            / status["latestCompiled"]["sourcePath"]
        )
        revision_source.write_text("// changed revision evidence\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ReplayProjectError,
            "workspace compiled candidate source revision changed",
        ):
            self.service.register_workspace(status)

    def test_code_match_requires_complete_identical_interface_and_layout(self) -> None:
        blob_a = _dxbc(b"same program", b"frame-a reflection")
        blob_c = _dxbc(b"same program", b"frame-c reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "frame-a", blob_a, resource_id=1
        )
        catalog_c, unused_shader_c, unused_capture_c = self._catalog(
            "frame-c", blob_c, resource_id=2, topology="LineList"
        )
        self.service.import_capture(catalog_a)
        status = self._workspace_status(
            "workspace-a", capture_a, shader_a, "// edited source\n"
        )
        self.service.register_workspace(status)
        plan = self.service.import_capture(catalog_c)
        self.assertEqual(plan["matchedShaderCount"], 0)

    def test_exact_catalog_and_knowledge_cache_skips_full_correspondence_rebuild(self) -> None:
        blob_a = _dxbc(b"same cached program", b"frame-a reflection")
        blob_b = _dxbc(b"same cached program", b"frame-b reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "cache-frame-a", blob_a, resource_id=201
        )
        catalog_b, unused_shader_b, capture_b = self._catalog(
            "cache-frame-b", blob_b, resource_id=202
        )
        self.service.import_capture(catalog_a)
        self.service.register_workspace(
            self._workspace_status(
                "cache-workspace-a", capture_a, shader_a, "// cached source a\n"
            )
        )

        first = self.service.import_capture(catalog_b)
        self.assertFalse(first["derivedCache"]["hit"])
        self.assertTrue(first["derivedCache"]["stored"])
        self.assertEqual(first["matchedShaderCount"], 1)
        cache_path = Path(first["derivedCache"]["path"])
        self.assertTrue(cache_path.is_file())
        self.assertTrue(cache_path.is_relative_to(self.project))
        cache_value = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(
            cache_value["catalogSha256"], first["catalogSha256"]
        )
        self.assertEqual(
            cache_value["knowledgeContentSha256"],
            first["derivedCache"]["knowledgeContentSha256"],
        )
        unsigned = {
            key: value for key, value in cache_value.items() if key != "sealSha256"
        }
        self.assertEqual(
            cache_value["sealSha256"],
            hashlib.sha256(
                json.dumps(
                    unsigned,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
        )

        with mock.patch.object(
            self.service,
            "_build_reuse_mappings",
            side_effect=AssertionError("exact cache hit rebuilt all correspondences"),
        ):
            repeated = self.service.reuse_plan(
                capture_b, first["catalogSha256"]
            )
        self.assertTrue(repeated["derivedCache"]["hit"])
        self.assertEqual(
            repeated["matches"][0]["workspaceCandidates"][0]["matchMode"],
            "code-interface-layout-exact",
        )
        self.assertFalse(repeated["derivedCache"]["physicalGateBypassed"])

    def test_cross_rdc_cached_sources_follow_project_head_after_editing_another_capture(self) -> None:
        blob = _dxbc(b"shared program across RDC files")
        captures = []
        for name, resource_id in (("head-frame-a", 754), ("head-frame-b", 860), ("head-frame-c", 61523)):
            catalog, shader, capture_id = self._catalog(name, blob, resource_id=resource_id)
            imported = self.service.import_capture(catalog)
            captures.append((catalog, shader, capture_id, imported["catalogSha256"]))
        status_a = self._workspace_status("head-source-a", captures[0][2], captures[0][1], "// previous organized source\n")
        old = self.service.register_workspace(status_a)
        old_id = old["knowledge"]["knowledgeId"]
        for _, _, capture_id, catalog_sha in captures:
            planned = self.service.reuse_plan(capture_id, catalog_sha)
            self.assertEqual(planned["matches"][0]["workspaceCandidates"][0]["knowledgeId"], old_id)
        # Edit and publish from another RDC. Different EIDs/resource IDs never
        # partition the logical project source or make the old cache a head.
        status_b = self._workspace_status("head-source-b", captures[1][2], captures[1][1], "// latest organized source\n")
        current = self.service.register_workspace(status_b, expected_head=old["head"])
        self.assertEqual(current["head"]["headKey"], old["head"]["headKey"])
        reopened = ReplayProjectService(self.project)
        latest_head = reopened.current_workspace_source_head(old_id)
        self.assertEqual(latest_head["headKey"], current["head"]["headKey"])
        self.assertEqual(latest_head["generation"], current["head"]["generation"])
        self.assertEqual(latest_head["knowledge"]["knowledgeId"], current["knowledge"]["knowledgeId"])
        self.assertEqual(latest_head["knowledge"]["sourceSha256"], current["knowledge"]["sourceSha256"])
        for _, _, capture_id, catalog_sha in captures:
            planned = reopened.reuse_plan(capture_id, catalog_sha)
            candidates = planned["matches"][0]["workspaceCandidates"]
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["knowledgeId"], current["knowledge"]["knowledgeId"])
            self.assertEqual(candidates[0]["sourceSha256"], current["knowledge"]["sourceSha256"])
            warmed = reopened.reuse_plan(capture_id, catalog_sha)
            self.assertTrue(warmed["derivedCache"]["hit"])
            self.assertEqual(warmed["matches"], planned["matches"])
        with self.assertRaisesRegex(ReplayProjectError, "expected_head"):
            reopened.register_workspace(status_a)
        # The original file is retained; newer project publication is explicit,
        # never an overwrite disguised as opening a different capture.
        self.assertEqual(Path(status_a["source"]["path"]).read_text(), "// previous organized source\n")

    def test_reuse_cache_invalidates_on_knowledge_or_catalog_content_change(self) -> None:
        blob_a = _dxbc(b"same invalidation program", b"frame-a reflection")
        blob_b = _dxbc(b"same invalidation program", b"frame-b reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "invalidate-frame-a", blob_a, resource_id=301
        )
        catalog_b, unused_shader_b, capture_b = self._catalog(
            "invalidate-frame-b", blob_b, resource_id=302
        )
        self.service.import_capture(catalog_a)
        self.service.register_workspace(
            self._workspace_status(
                "invalidate-workspace-a",
                capture_a,
                shader_a,
                "// invalidation source a\n",
            )
        )
        initial = self.service.import_capture(catalog_b)
        initial_cache = initial["derivedCache"]

        latest_workspace = self.service.register_workspace(
            self._workspace_status(
                "invalidate-workspace-b",
                capture_a,
                shader_a,
                "// invalidation source b\n",
            )
        )
        with mock.patch.object(
            self.service,
            "_build_reuse_mappings",
            wraps=self.service._build_reuse_mappings,
        ) as rebuilt:
            knowledge_changed = self.service.reuse_plan(
                capture_b, initial["catalogSha256"]
            )
        self.assertEqual(rebuilt.call_count, 1)
        self.assertFalse(knowledge_changed["derivedCache"]["hit"])
        self.assertNotEqual(
            knowledge_changed["derivedCache"]["knowledgeContentSha256"],
            initial_cache["knowledgeContentSha256"],
        )
        self.assertNotEqual(
            knowledge_changed["derivedCache"]["path"], initial_cache["path"]
        )
        workspace_candidates = knowledge_changed["matches"][0]["workspaceCandidates"]
        self.assertEqual(len(workspace_candidates), 1)
        self.assertEqual(
            workspace_candidates[0]["knowledgeId"],
            latest_workspace["knowledge"]["knowledgeId"],
        )
        self.assertTrue(workspace_candidates[0]["activeCandidate"])
        self.assertEqual(workspace_candidates[0]["supersededCandidateCount"], 1)
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["knowledge"]["workspaces"]), 2)

        changed_catalog = json.loads(catalog_b.read_text(encoding="utf-8"))
        changed_catalog["exportDiagnostics"] = {"revision": 2}
        catalog_b.write_text(json.dumps(changed_catalog), encoding="utf-8")
        catalog_changed = self.service.import_capture(catalog_b)
        self.assertEqual(catalog_changed["captureId"], capture_b)
        self.assertNotEqual(
            catalog_changed["catalogSha256"], knowledge_changed["catalogSha256"]
        )
        self.assertFalse(catalog_changed["derivedCache"]["hit"])
        self.assertNotEqual(
            catalog_changed["derivedCache"]["path"],
            knowledge_changed["derivedCache"]["path"],
        )

    def test_pending_head_preserves_latest_validated_replay_version_and_binds_both_sources(self):
        catalog, shader, capture_id = self._catalog("dual-source", _dxbc(), resource_id=754)
        self.service.import_capture(catalog)
        valid_status = self._workspace_status("dual-valid", capture_id, shader,
            "float4 main() : SV_Target { return 0.25; }\n")
        self._with_fully_validated_candidate(valid_status, _dxbc(b"verified program"))
        valid = self.service.register_workspace(valid_status)
        pending_status = self._workspace_status("dual-pending", capture_id, shader,
            "float4 main() : SV_Target { return needsRepair; }\n")
        pending = self.service.register_workspace(pending_status, include_compiled_candidate=False,
                                                  expected_head=valid["head"])
        original_manifest = self.service.manifest_path.read_bytes()
        inventory = self._shader_inventory(resource_id=728, raw_sha256=valid["knowledge"]["rawSha256"],
            layout_hash=valid["knowledge"]["layoutHash"], interface_hash=valid["knowledge"]["interfaceHash"])
        snapshot = self.service.reuse_snapshot_for_shader_inventory(inventory)
        candidate, = snapshot["reusePlan"]["matches"][0]["workspaceCandidates"]
        self.assertEqual(candidate["knowledgeId"], valid["knowledge"]["knowledgeId"])
        self.assertEqual(candidate["pendingSource"]["knowledgeId"], pending["knowledge"]["knowledgeId"])
        self.assertEqual(set(snapshot["binding"]["knowledgeIds"]["workspaces"]),
                         {valid["knowledge"]["knowledgeId"], pending["knowledge"]["knowledgeId"]})
        self.assertEqual(self.service.manifest_path.read_bytes(), original_manifest)
        from src import shader_source_publication
        original_reason = shader_source_publication.source_only_restore_reason
        def revised_reason(knowledge, project_path=None):
            if knowledge.get("knowledgeId") == pending["knowledge"]["knowledgeId"]:
                return "legacy-saved-organization-draft"
            return original_reason(knowledge, project_path)
        with mock.patch.object(shader_source_publication, "source_only_restore_reason", side_effect=revised_reason):
            changed_reason = self.service.reuse_snapshot_for_shader_inventory(inventory)
        self.assertNotEqual(changed_reason["binding"], snapshot["binding"])
        self.assertEqual(changed_reason["reusePlan"]["matches"][0]["workspaceCandidates"][0]["pendingSource"]["reason"],
                         "legacy-saved-organization-draft")
        # Matching code with different captured container bytes is useful for
        # general source discovery, but cannot borrow this head's fallback.
        variant_catalog, unused_shader, variant_capture = self._catalog(
            "dual-variant", _dxbc(metadata=b"different original container"), resource_id=861)
        variant = self.service.import_capture(variant_catalog)
        variant_plan = self.service.reuse_plan(variant_capture, variant["catalogSha256"])
        self.assertFalse(any(candidate.get("pendingSource") for match in variant_plan["matches"]
                             for candidate in match["workspaceCandidates"]))
        destination = copy.deepcopy(valid_status)
        destination["source"].update(present=False)
        destination["capture"]["api"] = "D3D11"
        self.assertEqual(self.service.prepare_workspace_source(valid["knowledge"]["knowledgeId"], destination)["sourceSha256"],
                         valid["knowledge"]["sourceSha256"])
        self.assertEqual(self.service.prepare_workspace_source(pending["knowledge"]["knowledgeId"], destination)["sourceSha256"],
                         pending["knowledge"]["sourceSha256"])
        repaired_status = self._workspace_status("dual-repaired", capture_id, shader,
            "float4 main() : SV_Target { return 0.5; }\n")
        self._with_fully_validated_candidate(repaired_status, _dxbc(b"repaired verified"))
        repaired = self.service.register_workspace(repaired_status, expected_head=pending["head"])
        after = self.service.reuse_snapshot_for_shader_inventory(inventory)
        new, = after["reusePlan"]["matches"][0]["workspaceCandidates"]
        self.assertEqual(new["knowledgeId"], repaired["knowledge"]["knowledgeId"])
        self.assertNotIn("pendingSource", new)
        self.assertNotEqual(after["binding"], snapshot["binding"])
        with self.assertRaisesRegex(ReplayProjectError, "active knowledge head"):
            self.service.prepare_workspace_source(valid["knowledge"]["knowledgeId"], destination)

    def test_historical_workspace_requires_cas_before_head_reactivation(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "workspace-reactivation", _dxbc(), resource_id=311
        )
        imported = self.service.import_capture(catalog)
        status_a = self._workspace_status(
            "workspace-reactivation-a",
            capture_id,
            shader,
            "// source A\n",
        )
        status_b = self._workspace_status(
            "workspace-reactivation-b",
            capture_id,
            shader,
            "// source B\n",
        )
        publication_a = self.service.register_workspace(status_a)
        record_a = publication_a["knowledge"]
        publication_b = self.service.register_workspace(status_b)
        record_b = publication_b["knowledge"]
        before = self.service.restore_binding_state(
            capture_id, imported["catalogSha256"]
        )
        before_plan = self.service.reuse_plan(
            capture_id, imported["catalogSha256"]
        )
        self.assertEqual(
            before_plan["matches"][0]["workspaceCandidates"][0]["knowledgeId"],
            record_b["knowledgeId"],
        )

        with self.assertRaisesRegex(
            ReplayProjectError, "without expected_head"
        ):
            self.service.register_workspace(status_a)
        unchanged = self.service.restore_binding_state(
            capture_id, imported["catalogSha256"]
        )
        self.assertEqual(
            before["knowledgeContentSha256"],
            unchanged["knowledgeContentSha256"],
        )

        repeated = self.service.register_workspace(
            status_a, expected_head=publication_b["head"]
        )
        self.assertFalse(repeated["idempotent"])
        self.assertTrue(repeated["headChanged"])
        self.assertEqual(repeated["head"]["generation"], 3)
        after = self.service.restore_binding_state(
            capture_id, imported["catalogSha256"]
        )
        after_plan = self.service.reuse_plan(
            capture_id, imported["catalogSha256"]
        )
        self.assertNotEqual(
            before["knowledgeContentSha256"], after["knowledgeContentSha256"]
        )
        self.assertFalse(after_plan["derivedCache"]["hit"])
        self.assertEqual(
            after_plan["matches"][0]["workspaceCandidates"][0]["knowledgeId"],
            record_a["knowledgeId"],
        )

        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["knowledge"]["workspaces"]), 2)
        activations = manifest["activeKnowledge"]["workspaces"]
        self.assertGreater(
            activations[record_a["knowledgeId"]],
            activations[record_b["knowledgeId"]],
        )
        self.assertEqual(
            manifest["activeHeads"]["workspaces"][
                repeated["head"]["headKey"]
            ]["knowledgeId"],
            record_a["knowledgeId"],
        )
        manifest["knowledge"]["workspaces"].reverse()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        reordered = self.service.reuse_plan(
            capture_id, imported["catalogSha256"]
        )
        self.assertEqual(
            reordered["matches"][0]["workspaceCandidates"][0]["knowledgeId"],
            record_a["knowledgeId"],
        )

    def test_workspace_head_cas_rejects_a_stale_concurrent_publisher(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "workspace-head-cas", _dxbc(), resource_id=312
        )
        self.service.import_capture(catalog)
        status_a = self._workspace_status(
            "workspace-head-cas-a", capture_id, shader, "// A\n"
        )
        status_b = self._workspace_status(
            "workspace-head-cas-b", capture_id, shader, "// B\n"
        )
        status_c = self._workspace_status(
            "workspace-head-cas-c", capture_id, shader, "// C\n"
        )
        published_a = self.service.register_workspace(status_a)
        published_b = self.service.register_workspace(
            status_b, expected_head=published_a["head"]
        )
        self.assertEqual(published_b["head"]["generation"], 2)

        manifest_path = self.project / "project.json"
        before_repeat = manifest_path.read_bytes()
        repeated_b = self.service.register_workspace(status_b)
        self.assertTrue(repeated_b["idempotent"])
        self.assertFalse(repeated_b["headChanged"])
        self.assertEqual(repeated_b["head"]["generation"], 2)
        self.assertEqual(manifest_path.read_bytes(), before_repeat)

        with self.assertRaisesRegex(
            ReplayProjectError, "compare-and-swap conflict"
        ):
            self.service.register_workspace(
                status_c, expected_head=published_a["head"]
            )
        published_c = self.service.register_workspace(
            status_c, expected_head=published_b["head"]
        )
        self.assertEqual(published_c["head"]["generation"], 3)

    def test_workspace_head_key_uses_canonical_api_encoding_stable_identity(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "workspace-head-stable", _dxbc(), resource_id=313
        )
        self.service.import_capture(catalog)
        published = self.service.register_workspace(
            self._workspace_status(
                "workspace-head-stable-source",
                capture_id,
                shader,
                "// stable identity\n",
            )
        )
        manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        record = published["knowledge"]
        origin_capture = next(
            item
            for item in manifest["captures"]
            if item["captureId"] == capture_id
        )
        origin_shader = next(
            item
            for item in origin_capture["shaders"]
            if item["resourceId"] == shader["shaderResourceId"]
        )
        stable_identity = {
            "api": projects_module._api_family(origin_capture["api"]),
            "encoding": projects_module._shader_encoding(
                origin_shader["encoding"]
            ),
            "rawSha256": record["rawSha256"],
            "stage": record["stage"],
            "entryPoint": record["entryPoint"],
            "abiHash": record["baselineAbiSha256"],
            "layoutHash": record["layoutHash"],
        }
        inventory_record = {
            **{
                key: record[key]
                for key in (
                    "stage",
                    "entryPoint",
                    "rawSha256",
                    "codeSha256",
                    "layoutHash",
                    "interfaceHash",
                    "interfaceComplete",
                    "baselineAbiSha256",
                    "targetProfile",
                )
            },
            "inventoryOrigin": {
                "schema": "renderdoc-ultra-project-shader-inventory-origin/v1",
                "schemaVersion": 1,
                **stable_identity,
                "stableKeySha256": projects_module._stable_shader_identity_sha256(
                    stable_identity
                ),
            },
        }
        self.assertEqual(
            published["head"]["headKey"],
            self.service._workspace_head_key(inventory_record),
        )
        other_api = copy.deepcopy(inventory_record)
        other_api_identity = {
            **stable_identity,
            "api": "d3d12",
        }
        other_api["inventoryOrigin"].update(
            {
                "api": "d3d12",
                "stableKeySha256": projects_module._stable_shader_identity_sha256(
                    other_api_identity
                ),
            }
        )
        self.assertNotEqual(
            published["head"]["headKey"],
            self.service._workspace_head_key(other_api),
        )
        invalid_pair = copy.deepcopy(inventory_record)
        invalid_identity = {**stable_identity, "api": "vulkan"}
        invalid_pair["inventoryOrigin"].update(
            {
                "api": "vulkan",
                "stableKeySha256": projects_module._stable_shader_identity_sha256(
                    invalid_identity
                ),
            }
        )
        with self.assertRaisesRegex(
            ReplayProjectError, "API/encoding pair is unsupported"
        ):
            self.service._workspace_head_key(invalid_pair)

    def test_alias_material_and_hlsl_heads_are_layered_and_alias_is_current_only(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "layered-heads", _dxbc(), resource_id=314
        )
        imported = self.service.import_capture(catalog)
        workspace = self.service.register_workspace(
            self._workspace_status(
                "layered-heads-workspace",
                capture_id,
                shader,
                "// layered HLSL\n",
            )
        )

        def alias_document(alias: str) -> dict:
            return {
                "schema": ALIAS_SCHEMA,
                "schemaVersion": ALIAS_VERSION,
                "verifiedOnly": True,
                "captureId": capture_id,
                "aliases": [
                    {
                        "verified": True,
                        "captureId": capture_id,
                        "shaderResourceId": shader["shaderResourceId"],
                        "stage": shader["stage"],
                        "entryPoint": shader["entryPoint"],
                        "rawSha256": shader["rawSha256"],
                        "codeSha256": shader["codeSha256"],
                        "bindingKind": "constantBufferVariable",
                        "registerSpace": 0,
                        "bindNumber": 0,
                        "byteOffset": 0,
                        "canonicalPath": "cb0._child0",
                        "alias": alias,
                    }
                ],
            }

        alias_a_path = self.root / "layered-alias-a.json"
        alias_b_path = self.root / "layered-alias-b.json"
        alias_a_path.write_text(
            json.dumps(alias_document("BaseColor")), encoding="utf-8"
        )
        alias_b_path.write_text(
            json.dumps(alias_document("Tint")), encoding="utf-8"
        )
        alias_a = self.service.register_alias_table(alias_a_path)
        alias_b = self.service.register_alias_table(alias_b_path)
        self.assertEqual(alias_b["head"]["generation"], 2)

        parameter_id = "7" * 32
        session = {
            "schema": "renderdoc-ultra-material-session/v1",
            "captureId": capture_id,
            "captureSessionId": "8" * 32,
            "shaderResourceId": shader["shaderResourceId"],
            "stage": shader["stage"],
            "constantBlockPath": "cb0",
            "layoutSha256": "9" * 64,
            "parameters": [
                {
                    "parameterId": parameter_id,
                    "path": "cb0.BaseColor",
                    "baseType": "Float",
                    "components": 4,
                    "value": [1.0, 0.5, 0.25, 1.0],
                }
            ],
        }
        material_a = self.service.save_material_knowledge(
            session, [parameter_id]
        )
        session["parameters"][0]["value"] = [0.0, 0.5, 1.0, 1.0]
        material_b = self.service.save_material_knowledge(
            session, [parameter_id], expected_head=material_a["head"]
        )

        head_keys = {
            workspace["head"]["headKey"],
            alias_b["head"]["headKey"],
            material_b["head"]["headKey"],
        }
        self.assertEqual(len(head_keys), 3)
        plan = self.service.reuse_plan(
            capture_id, imported["catalogSha256"]
        )
        match = plan["matches"][0]
        self.assertEqual(
            [item["knowledgeId"] for item in match["aliasCandidates"]],
            [alias_b["knowledge"]["knowledgeId"]],
        )
        self.assertEqual(
            match["materialCandidates"][0]["knowledgeId"],
            material_b["knowledge"]["knowledgeId"],
        )
        with self.assertRaisesRegex(
            ReplayProjectError, "without expected_head"
        ):
            self.service.register_alias_table(alias_a_path)

    def test_restore_binding_is_capture_scoped_and_hashes_sources_once(self) -> None:
        shared_source = _dxbc(b"scoped shared program", b"source reflection")
        shared_target = _dxbc(b"scoped shared program", b"target reflection")
        unrelated_blob = _dxbc(b"unrelated program", b"other reflection")
        source_catalog, source_shader, source_capture = self._catalog(
            "restore-scope-source", shared_source, resource_id=601
        )
        target_catalog, unused_target_shader, target_capture = self._catalog(
            "restore-scope-target", shared_target, resource_id=602
        )
        unrelated_catalog, unrelated_shader, unrelated_capture = self._catalog(
            "restore-scope-unrelated", unrelated_blob, resource_id=603
        )
        self.service.import_capture(source_catalog)
        target_import = self.service.import_capture(target_catalog)
        self.service.import_capture(unrelated_catalog)

        relevant_workspace = self.service.register_workspace(
            self._workspace_status(
                "restore-scope-workspace",
                source_capture,
                source_shader,
                "float4 main() : SV_Target { return 1; }\n",
            )
        )["knowledge"]
        relevant_parameter_id = "1" * 32
        relevant_material = self.service.save_material_knowledge(
            {
                "schema": "renderdoc-ultra-material-session/v1",
                "captureId": source_capture,
                "captureSessionId": "2" * 32,
                "shaderResourceId": 601,
                "stage": "fragment",
                "constantBlockPath": "cb0",
                "layoutSha256": "3" * 64,
                "parameters": [
                    {
                        "parameterId": relevant_parameter_id,
                        "path": "cb0.BaseColor",
                        "baseType": "Float",
                        "components": 4,
                        "value": [1.0, 0.5, 0.25, 1.0],
                    }
                ],
            },
            [relevant_parameter_id],
        )["knowledge"]

        before = self.service.restore_binding_state(
            target_capture, target_import["catalogSha256"]
        )
        self.assertRegex(before["knowledgeContentSha256"], r"^[0-9a-f]{64}$")
        relevant_source = (
            self.project / relevant_workspace["sourcePath"]
        ).resolve()
        with mock.patch.object(
            projects_module,
            "_file_bytes",
            wraps=projects_module._file_bytes,
        ) as file_reads:
            snapshot = self.service.restore_knowledge_snapshot(
                target_capture, target_import["catalogSha256"]
            )
        source_reads = [
            call
            for call in file_reads.call_args_list
            if call.args and Path(call.args[0]).resolve() == relevant_source
        ]
        self.assertEqual(len(source_reads), 1)
        self.assertEqual(
            set(snapshot["workspaces"]), {relevant_workspace["knowledgeId"]}
        )
        self.assertEqual(
            set(snapshot["materials"]), {relevant_material["knowledgeId"]}
        )

        original_source = relevant_source.read_bytes()
        try:
            relevant_source.write_bytes(original_source + b"// tampered\n")
            with self.assertRaisesRegex(
                ReplayProjectError, "workspace source hash mismatch"
            ):
                self.service.restore_knowledge_snapshot(
                    target_capture, target_import["catalogSha256"]
                )
        finally:
            relevant_source.write_bytes(original_source)

        manifest_path = self.project / "project.json"
        original_manifest = manifest_path.read_bytes()
        tampered_manifest = json.loads(original_manifest.decode("utf-8"))
        tampered_material = next(
            item
            for item in tampered_manifest["knowledge"]["materials"]
            if item["knowledgeId"] == relevant_material["knowledgeId"]
        )
        tampered_material["parameters"][0]["value"][0] = 0.0
        try:
            manifest_path.write_text(json.dumps(tampered_manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                ReplayProjectError, "material knowledgeId does not match"
            ):
                self.service.restore_knowledge_snapshot(
                    target_capture, target_import["catalogSha256"]
                )
        finally:
            manifest_path.write_bytes(original_manifest)

        unrelated_workspace = self.service.register_workspace(
            self._workspace_status(
                "restore-scope-unrelated-workspace",
                unrelated_capture,
                unrelated_shader,
                "float4 main() : SV_Target { return 0; }\n",
            )
        )["knowledge"]
        unrelated_parameter_id = "4" * 32
        unrelated_material = self.service.save_material_knowledge(
            {
                "schema": "renderdoc-ultra-material-session/v1",
                "captureId": unrelated_capture,
                "captureSessionId": "5" * 32,
                "shaderResourceId": 603,
                "stage": "fragment",
                "constantBlockPath": "cb0",
                "layoutSha256": "6" * 64,
                "parameters": [
                    {
                        "parameterId": unrelated_parameter_id,
                        "path": "cb0.BaseColor",
                        "baseType": "Float",
                        "components": 4,
                        "value": [0.0, 0.0, 0.0, 1.0],
                    }
                ],
            },
            [unrelated_parameter_id],
        )["knowledge"]

        after = self.service.restore_knowledge_snapshot(
            target_capture, target_import["catalogSha256"]
        )
        self.assertEqual(
            after["knowledgeContentSha256"], before["knowledgeContentSha256"]
        )
        self.assertNotIn(unrelated_workspace["knowledgeId"], after["workspaces"])
        self.assertNotIn(unrelated_material["knowledgeId"], after["materials"])

        self.service.register_workspace(
            self._workspace_status(
                "restore-scope-new-relevant-workspace",
                source_capture,
                source_shader,
                "float4 main() : SV_Target { return 0.5; }\n",
            )
        )
        changed = self.service.restore_binding_state(
            target_capture, target_import["catalogSha256"]
        )
        self.assertNotEqual(
            changed["knowledgeContentSha256"], before["knowledgeContentSha256"]
        )

    def test_reuse_plan_selects_latest_material_head_per_cbuffer(self) -> None:
        catalog, shader, capture_id = self._catalog(
            "material-head", _dxbc(), resource_id=351
        )
        imported = self.service.import_capture(catalog)
        parameter_a = "a" * 32
        parameter_b = "b" * 32
        session = {
            "schema": "renderdoc-ultra-material-session/v1",
            "captureId": capture_id,
            "captureSessionId": "c" * 32,
            "shaderResourceId": shader["shaderResourceId"],
            "stage": shader["stage"],
            "constantBlockPath": "cb0",
            "layoutSha256": "d" * 64,
            "parameters": [
                {
                    "parameterId": parameter_a,
                    "path": "cb0.A",
                    "baseType": "Float",
                    "components": 1,
                    "value": 1.0,
                },
                {
                    "parameterId": parameter_b,
                    "path": "cb0.B",
                    "baseType": "Float",
                    "components": 1,
                    "value": 2.0,
                },
            ],
        }
        first = self.service.save_material_knowledge(session, [parameter_a])
        # Simulate a new Host/session whose in-memory dirty-leaf accumulator
        # knows only B. The project must merge durable head A under its lock.
        latest = self.service.save_material_knowledge(session, [parameter_b])
        self.assertEqual(
            [item["parameterId"] for item in latest["knowledge"]["parameters"]],
            [parameter_a, parameter_b],
        )

        plan = self.service.reuse_plan(capture_id, imported["catalogSha256"])
        candidates = plan["matches"][0]["materialCandidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0]["knowledgeId"], latest["knowledge"]["knowledgeId"]
        )
        self.assertTrue(candidates[0]["activeCandidate"])
        self.assertEqual(candidates[0]["supersededCandidateCount"], 1)
        self.assertEqual(candidates[0]["constantBlockPath"], "cb0")
        self.assertTrue(plan["policy"]["appendOnlyKnowledgeHistoryPreserved"])
        self.assertTrue(plan["policy"]["newestValidActiveHeadOnly"])
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["knowledge"]["materials"]), 2)
        self.assertEqual(
            manifest["knowledge"]["materials"][0]["knowledgeId"],
            first["knowledge"]["knowledgeId"],
        )

        before_revert = self.service.restore_binding_state(
            capture_id, imported["catalogSha256"]
        )
        session["parameters"][0]["value"] = 3.0
        changed = self.service.save_material_knowledge(session, [parameter_a])
        self.assertEqual(len(changed["knowledge"]["parameters"]), 2)
        session["parameters"][0]["value"] = 1.0
        with self.assertRaisesRegex(
            ReplayProjectError, "without expected_head"
        ):
            self.service.save_material_knowledge(session, [parameter_a])
        reverted = self.service.save_material_knowledge(
            session, [parameter_a], expected_head=changed["head"]
        )
        self.assertFalse(reverted["idempotent"])
        self.assertTrue(reverted["headChanged"])
        self.assertEqual(
            reverted["knowledge"]["knowledgeId"], latest["knowledge"]["knowledgeId"]
        )
        after_revert = self.service.restore_binding_state(
            capture_id, imported["catalogSha256"]
        )
        self.assertNotEqual(
            before_revert["knowledgeContentSha256"],
            after_revert["knowledgeContentSha256"],
        )
        reverted_plan = self.service.reuse_plan(
            capture_id, imported["catalogSha256"]
        )
        self.assertEqual(
            reverted_plan["matches"][0]["materialCandidates"][0]["knowledgeId"],
            latest["knowledge"]["knowledgeId"],
        )
        final_manifest = json.loads(
            (self.project / "project.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(final_manifest["knowledge"]["materials"]), 3)
        activations = final_manifest["activeKnowledge"]["materials"]
        self.assertGreater(
            activations[latest["knowledge"]["knowledgeId"]],
            activations[changed["knowledge"]["knowledgeId"]],
        )
        self.assertEqual(
            final_manifest["activeHeads"]["materials"][
                reverted["head"]["headKey"]
            ]["knowledgeId"],
            latest["knowledge"]["knowledgeId"],
        )

    def test_resealed_cache_cannot_bypass_interface_layout_match_gate(self) -> None:
        blob_a = _dxbc(b"same gated program", b"frame-a reflection")
        blob_b = _dxbc(b"same gated program", b"frame-b reflection")
        catalog_a, shader_a, capture_a = self._catalog(
            "gate-frame-a", blob_a, resource_id=401
        )
        catalog_b, unused_shader_b, capture_b = self._catalog(
            "gate-frame-b", blob_b, resource_id=402
        )
        self.service.import_capture(catalog_a)
        self.service.register_workspace(
            self._workspace_status(
                "gate-workspace-a", capture_a, shader_a, "// gated source\n"
            )
        )
        first = self.service.import_capture(catalog_b)
        self.assertEqual(
            first["matches"][0]["workspaceCandidates"][0]["matchMode"],
            "code-interface-layout-exact",
        )
        cache_path = Path(first["derivedCache"]["path"])
        cache_value = json.loads(cache_path.read_text(encoding="utf-8"))
        cache_value["mappings"][0]["workspaces"][0]["matchMode"] = "raw-exact"
        unsigned = {
            key: value for key, value in cache_value.items() if key != "sealSha256"
        }
        cache_value["sealSha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        cache_path.write_text(json.dumps(cache_value), encoding="utf-8")

        with mock.patch.object(
            self.service,
            "_build_reuse_mappings",
            wraps=self.service._build_reuse_mappings,
        ) as rebuilt:
            recovered = self.service.reuse_plan(capture_b, first["catalogSha256"])
        self.assertEqual(rebuilt.call_count, 1)
        self.assertFalse(recovered["derivedCache"]["hit"])
        self.assertEqual(
            recovered["matches"][0]["workspaceCandidates"][0]["matchMode"],
            "code-interface-layout-exact",
        )
        self.assertTrue(recovered["policy"]["liveAbiRevalidationRequiredBeforeShaderApply"])
        self.assertFalse(recovered["policy"]["autoApplyAuthorized"])

    def test_same_capture_id_tracks_catalog_revisions_and_selects_latest(self) -> None:
        catalog, unused_shader, capture_id = self._catalog(
            "same-capture-revisions", _dxbc(), resource_id=3
        )
        first = self.service.import_capture(catalog)
        self.assertFalse(first["catalogRevisionKnown"])

        changed = json.loads(catalog.read_text(encoding="utf-8"))
        changed["exportDiagnostics"] = {"reflectionRevision": 2}
        catalog.write_text(json.dumps(changed), encoding="utf-8")
        second = self.service.import_capture(catalog)

        self.assertEqual(second["captureId"], capture_id)
        self.assertFalse(second["idempotent"])
        self.assertFalse(second["catalogRevisionKnown"])
        self.assertNotEqual(first["catalogSha256"], second["catalogSha256"])
        status = self.service.status()
        self.assertEqual(status["captureCount"], 1)
        self.assertEqual(status["captureRevisionCount"], 2)
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        revisions = manifest["captures"][0]["catalogRevisions"]
        self.assertEqual(len(revisions), 2)
        self.assertEqual(
            {item["catalogSha256"] for item in revisions},
            {first["catalogSha256"], second["catalogSha256"]},
        )

        repeated = self.service.import_capture(catalog)
        self.assertTrue(repeated["idempotent"])
        self.assertTrue(repeated["catalogRevisionKnown"])
        self.assertEqual(self.service.status()["captureRevisionCount"], 2)

    def test_legacy_single_catalog_manifest_remains_readable_and_idempotent(self) -> None:
        catalog, unused_shader, unused_capture = self._catalog(
            "legacy-single-catalog", _dxbc(), resource_id=4
        )
        self.service.import_capture(catalog)
        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["captures"][0].pop("catalogRevisions")
        manifest["captures"][0].pop("activeCatalogSha256")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        self.assertEqual(self.service.status()["captureRevisionCount"], 1)
        repeated = self.service.import_capture(catalog)
        self.assertTrue(repeated["idempotent"])
        self.assertTrue(repeated["catalogRevisionKnown"])
        self.assertEqual(self.service.status()["captureRevisionCount"], 1)

    def test_revision_history_is_owned_exact_and_historical_reimport_keeps_head(self) -> None:
        catalog_a, shader_a, capture_id = self._catalog(
            "revision-owned-a", _dxbc(), resource_id=5
        )
        catalog_b = self._catalog_revision(
            catalog_a,
            "revision-owned-b",
            field_base_type="UInt",
            indent=2,
        )
        catalog_b_equivalent = self._catalog_revision(
            catalog_a,
            "revision-owned-b-equivalent",
            field_base_type="UInt",
            indent=4,
        )

        first = self.service.import_capture(catalog_a)
        second = self.service.import_capture(catalog_b)
        self.assertNotEqual(first["catalogSha256"], second["catalogSha256"])
        self.assertTrue(second["catalogRevisionAdded"])
        self.assertTrue(second["activeRevisionChanged"])

        equivalent = self.service.import_capture(catalog_b_equivalent)
        self.assertTrue(equivalent["idempotent"])
        self.assertFalse(equivalent["catalogRevisionAdded"])
        self.assertEqual(equivalent["catalogSha256"], second["catalogSha256"])
        self.assertEqual(self.service.status()["captureRevisionCount"], 2)

        historical = self.service.import_capture(catalog_a)
        self.assertTrue(historical["idempotent"])
        self.assertEqual(historical["catalogSha256"], first["catalogSha256"])
        self.assertEqual(
            historical["activeCatalogSha256"], second["catalogSha256"]
        )
        self.assertFalse(historical["activeRevisionChanged"])
        self.assertEqual(
            self.service.reuse_plan(capture_id, first["catalogSha256"])[
                "catalogSha256"
            ],
            first["catalogSha256"],
        )

        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        capture = manifest["captures"][0]
        self.assertEqual(capture["captureId"], capture_id)
        self.assertEqual(capture["catalogSha256"], second["catalogSha256"])
        self.assertEqual(capture["activeCatalogSha256"], second["catalogSha256"])
        self.assertEqual(len(capture["catalogRevisions"]), 2)
        owned_paths = [
            self.project / item["catalogPath"]
            for item in capture["catalogRevisions"]
        ]
        self.assertTrue(all(path.is_file() for path in owned_paths))
        self.assertTrue(all(path.is_relative_to(self.project) for path in owned_paths))

        # External import roots are origins, not retained authority.
        catalog_a.write_text("{}", encoding="utf-8")
        catalog_b.unlink()
        catalog_b_equivalent.unlink()
        self.assertEqual(self.service.status()["captureRevisionCount"], 2)
        self.assertEqual(shader_a["rawSha256"], capture["shaders"][0]["rawSha256"])

    def test_project_owned_catalog_revision_hash_tamper_fails_closed(self) -> None:
        catalog, unused_shader, unused_capture = self._catalog(
            "revision-catalog-tamper", _dxbc(), resource_id=51
        )
        self.service.import_capture(catalog)
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        revision = manifest["captures"][0]["catalogRevisions"][0]
        owned_catalog = self.project / revision["catalogPath"]
        owned_catalog.write_bytes(owned_catalog.read_bytes() + b" \n")

        with self.assertRaisesRegex(ReplayProjectError, "catalog hash mismatch"):
            self.service.status()

    def test_active_catalog_revision_mirror_tamper_fails_closed(self) -> None:
        catalog, unused_shader, unused_capture = self._catalog(
            "revision-mirror-tamper", _dxbc(), resource_id=52
        )
        self.service.import_capture(catalog)
        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["captures"][0]["importedAt"] = "2000-01-01T00:00:00Z"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ReplayProjectError, "mirror is inconsistent"):
            self.service.status()

    def test_workspace_registration_uses_exact_catalog_revision_not_head(self) -> None:
        catalog_a, shader_a, capture_id = self._catalog(
            "workspace-revision-a", _dxbc(), resource_id=6
        )
        first = self.service.import_capture(catalog_a)
        catalog_b = self._catalog_revision(
            catalog_a,
            "workspace-revision-b",
            field_base_type="UInt",
        )
        second = self.service.import_capture(catalog_b)
        self.assertNotEqual(first["catalogSha256"], second["catalogSha256"])

        status = self._workspace_status(
            "workspace-revision-source",
            capture_id,
            shader_a,
            "float4 main() : SV_Target { return 1; }\n",
        )
        status["capture"].update(
            {
                "catalogPath": str(catalog_a),
                "capturePath": str(self.root / "workspace-revision-a" / "workspace-revision-a.rdc"),
                "api": "D3D11",
            }
        )
        registered = self.service.register_workspace(status)
        self.assertEqual(
            registered["knowledge"]["originCatalogSha256"],
            first["catalogSha256"],
        )
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        revisions = manifest["captures"][0]["catalogRevisions"]
        revision_a = next(
            item for item in revisions if item["catalogSha256"] == first["catalogSha256"]
        )
        revision_b = next(
            item for item in revisions if item["catalogSha256"] == second["catalogSha256"]
        )
        self.assertEqual(
            registered["knowledge"]["layoutHash"],
            revision_a["shaders"][0]["layoutHash"],
        )
        self.assertNotEqual(
            revision_a["shaders"][0]["layoutHash"],
            revision_b["shaders"][0]["layoutHash"],
        )

        ambiguous = self._workspace_status(
            "workspace-revision-ambiguous",
            capture_id,
            shader_a,
            "float4 main() : SV_Target { return 0; }\n",
        )
        with self.assertRaisesRegex(ReplayProjectError, "revision is ambiguous"):
            self.service.register_workspace(ambiguous)

    def test_internal_workspace_artifact_is_hash_verified(self) -> None:
        blob = _dxbc()
        catalog, shader, capture_id = self._catalog(
            "frame-a", blob, resource_id=11
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "workspace-a", capture_id, shader, "// trusted seed\n"
        )
        registered = self.service.register_workspace(status)
        source = self.project / registered["knowledge"]["sourcePath"]
        source.write_text("// tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ReplayProjectError, "hash mismatch"):
            self.service.status()

    def test_project_rejects_capture_from_another_api_scope(self) -> None:
        blob = _dxbc()
        catalog, unused_shader, unused_capture = self._catalog(
            "frame-vulkan", blob, resource_id=12
        )
        value = json.loads(catalog.read_text(encoding="utf-8"))
        value["api"] = "Vulkan"
        value["captureId"] = capture_identity(value, value["shaders"])
        catalog.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ReplayProjectError, "API conflicts"):
            self.service.import_capture(catalog)

    def test_project_rejects_instruction_assembly_as_hlsl_knowledge(self) -> None:
        blob = _dxbc()
        catalog, shader, capture_id = self._catalog(
            "frame-assembly", blob, resource_id=13
        )
        self.service.import_capture(catalog)
        status = self._workspace_status(
            "workspace-assembly", capture_id, shader, "dcl_globalFlags refactoringAllowed\n"
        )
        status["source"]["format"] = "dxbc-asm"
        Path(status["source"]["path"]).rename(
            Path(status["source"]["path"]).with_name("shader.dxbc.asm")
        )
        status["source"]["path"] = str(
            Path(status["source"]["path"]).with_name("shader.dxbc.asm")
        )
        with self.assertRaisesRegex(ReplayProjectError, "HLSL"):
            self.service.register_workspace(status)

    def test_project_rejects_seeding_into_instruction_assembly_workspace(self) -> None:
        blob = _dxbc()
        catalog, shader, capture_id = self._catalog(
            "frame-hlsl", blob, resource_id=14
        )
        self.service.import_capture(catalog)
        registered = self.service.register_workspace(
            self._workspace_status(
                "workspace-hlsl", capture_id, shader, "float4 main() : SV_Target { return 1; }\n"
            )
        )
        destination = self._workspace_status(
            "workspace-assembly-destination",
            capture_id,
            shader,
            "dcl_globalFlags refactoringAllowed\n",
        )
        destination["source"]["format"] = "dxbc-asm"
        with self.assertRaisesRegex(ReplayProjectError, "HLSL"):
            self.service.seed_workspace(
                registered["knowledge"]["knowledgeId"], destination
            )


class ReplayProjectApiReuseCompatibilityTests(unittest.TestCase):
    """Derived API aliases do not rewrite immutable v1 project identities."""

    def setUp(self) -> None:
        # Compose the established fixture rather than inheriting its test
        # methods, which would silently rerun the entire project suite.
        self.fixture = ReplayProjectTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.project = self.fixture.root / "api-reuse-project"
        ReplayProjectService.create(self.project, "API reuse", game_id="example-game",
                                    build_id="build-1", platform="windows-x64", api_family="d3d12")
        self.service = ReplayProjectService(self.project)

    def _register_enum_catalog(self):
        catalog, shader, _ = self.fixture._catalog(
            "enum-origin", _dxbc(b"legacy enum catalog"), resource_id=853)
        value = json.loads(catalog.read_text(encoding="utf-8"))
        value["api"] = "GraphicsAPI.D3D12"
        value["captureId"] = capture_identity(value, value["shaders"])
        catalog.write_text(json.dumps(value), encoding="utf-8")
        self.service.import_capture(catalog)
        source = "float4 main() : SV_Target { return 0.75; }\n"
        origin = self.fixture._workspace_status(
            "enum-source", value["captureId"], shader, source)
        origin["capture"]["api"] = "GraphicsAPI.D3D12"
        published = self.service.register_workspace(origin)
        destination = self.fixture._workspace_status(
            "canonical-destination", value["captureId"], shader, "unused temporary source")
        Path(destination["source"]["path"]).unlink()
        destination["source"]["present"] = False
        destination["capture"]["api"] = "d3d12"
        destination["shader"]["resourceId"] = 860
        return published, destination, source

    def _inventory(self, knowledge, **overrides):
        return self.fixture._shader_inventory(**{
            "resource_id": 860, "raw_sha256": knowledge["rawSha256"],
            "layout_hash": knowledge["layoutHash"],
            "interface_hash": knowledge["interfaceHash"], "api": "d3d12",
            **overrides,
        })

    def test_known_enum_aliases_are_canonical_only_for_derived_reuse(self):
        for enum, canonical in (("D3D11", "d3d11"), ("D3D12", "d3d12"),
                                ("OpenGL", "opengl"), ("Vulkan", "vulkan")):
            for value in ("GraphicsAPI." + enum, "graphicsapi" + enum.lower(), canonical):
                with self.subTest(value=value):
                    self.assertEqual(projects_module._reuse_api_family(value), canonical)
            with self.subTest(legacy=enum):
                self.assertEqual(projects_module._api_family("GraphicsAPI." + enum),
                                 "graphicsapi" + enum.lower())
        self.assertEqual(projects_module._api_family("GraphicsAPI.D3D12"), "graphicsapid3d12")

    def test_unknown_enum_prefixes_types_and_punctuation_are_not_known_apis(self):
        for value in ("Vendor.GraphicsAPI.D3D12", "typeAPI.D3D12", "OtherAPI.OpenGL",
                      "GraphicsAPI.Metal", "GraphicsAPI.D3D13", "GraphicsAPI!!!D3D12",
                      "GraphicsAPI/D3D12", "GraphicsAPI.D3D12Extra"):
            with self.subTest(value=value):
                family = projects_module._reuse_api_family(value)
                self.assertEqual(family, projects_module._api_family(value))
                self.assertNotIn(family, {"d3d11", "d3d12", "opengl", "vulkan", "metal"})

    def test_legacy_enum_catalog_reuses_current_api_without_rewriting_old_head_or_manifest(self):
        published, destination, source = self._register_enum_catalog()
        knowledge = published["knowledge"]
        manifest_before = self.service.manifest_path.read_bytes()
        manifest = json.loads(manifest_before)
        physical = self.service._head_shader_identity(knowledge, baseline_abi=True, manifest=manifest)
        self.assertEqual(physical["api"], "graphicsapid3d12")
        self.assertEqual(self.service._workspace_head_key(knowledge, manifest), published["head"]["headKey"])

        # Reopening reads the actual persisted v1 head; no re-registration or
        # manifest rewrite is needed to match the current canonical inventory.
        reopened = ReplayProjectService(self.project)
        inventory = self._inventory(knowledge)
        snapshot = reopened.reuse_snapshot_for_shader_inventory(inventory)
        self.assertEqual(snapshot["reusePlan"]["matchedShaderCount"], 1)
        match, = snapshot["reusePlan"]["matches"]
        candidate, = match["workspaceCandidates"]
        self.assertEqual(match["shader"]["resourceId"], 860)
        self.assertEqual(candidate["knowledgeId"], knowledge["knowledgeId"])
        self.assertTrue(candidate["activeCandidate"])
        record = snapshot["knowledge"]["workspaces"][knowledge["knowledgeId"]]
        self.assertEqual(record["physicalIdentity"]["api"], "d3d12")
        self.assertEqual(record["physicalIdentity"]["stableKeySha256"],
                         inventory["shaders"][0]["stableKeySha256"])
        self.assertEqual(Path(record["source"]["artifactPath"]).read_text(encoding="utf-8"), source)
        bundle = reopened.prepare_workspace_source(knowledge["knowledgeId"], destination)
        self.assertEqual(bundle["sourceText"].encode("utf-8"),
                         Path(record["source"]["artifactPath"]).read_bytes())
        self.assertTrue(bundle["requiresCompile"])
        self.assertTrue(bundle["requiresLiveAbiRevalidation"])
        self.assertFalse(bundle["applied"])
        self.assertFalse(Path(destination["source"]["path"]).exists())
        self.assertEqual(reopened.manifest_path.read_bytes(), manifest_before)
        self.assertEqual(reopened._workspace_head_key(knowledge, json.loads(manifest_before)),
                         published["head"]["headKey"])

    def test_enum_compatibility_does_not_relax_different_api_or_encoding(self):
        published, destination, _source = self._register_enum_catalog()
        knowledge = published["knowledge"]
        before = self.service.manifest_path.read_bytes()
        for changes in ({"api": "d3d11"}, {"encoding": "DXIL", "protocol_encoding": "dxil"}):
            with self.subTest(inventory=changes):
                snapshot = self.service.reuse_snapshot_for_shader_inventory(
                    self._inventory(knowledge, **changes))
                self.assertFalse(any(match["workspaceCandidates"]
                                     for match in snapshot["reusePlan"]["matches"]))
                self.assertEqual(snapshot["knowledge"]["workspaces"], {})
                self.assertEqual(snapshot["binding"]["knowledgeIds"]["workspaces"], [])
        for field, value in (("api", "d3d11"), ("encoding", "DXIL")):
            with self.subTest(destination=field):
                changed = copy.deepcopy(destination)
                if field == "api":
                    changed["capture"]["api"] = value
                else:
                    changed["baseline"]["kind"] = value
                with self.assertRaisesRegex(ReplayProjectError, "API/encoding"):
                    self.service.prepare_workspace_source(knowledge["knowledgeId"], changed)
        self.assertEqual(self.service.manifest_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()


def test_a_lock_left_by_a_dead_owner_is_recovered_and_a_read_never_needs_the_lock(tmp_path):
    """A Host killed mid-operation must not brick the project until a file is deleted by hand."""
    import inspect
    import json
    from unittest import mock

    import pytest

    from src import active_projects as selection_locks
    from src import projects

    root = tmp_path / "replay-project"
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".project-operation.lock"
    owner_pid = 424242
    value = {
        "schema": selection_locks.EXCLUSIVE_PROJECT_LOCK_SCHEMA,
        "schemaVersion": selection_locks.EXCLUSIVE_PROJECT_LOCK_VERSION,
        "token": "a" * 32,
        "ownerPid": owner_pid,
        "processStartIdentity": "windows-filetime:0000000000001234",
        "acquiredAt": "2026-08-28T00:00:00Z",
    }
    value["lockSha256"] = selection_locks._document_hash(value, "lockSha256")
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    lock_path.write_bytes(raw)
    real_probe = selection_locks._process_lock_owner_state

    def probe(pid):
        return ("dead", "") if pid == owner_pid else real_probe(pid)

    with mock.patch.object(selection_locks, "_process_lock_owner_state", side_effect=probe):
        with projects._project_lock(root):
            assert lock_path.is_file()
            assert lock_path.read_bytes() != raw
    assert not lock_path.exists()
    assert list(root.glob(".project-operation.lock.recovered-*")) == []
    # A legacy token recorded no owner: still a manual matter for mutations...
    lock_path.write_text("a" * 32, encoding="ascii")
    with pytest.raises(projects.ReplayProjectError, match="stale lock"):
        with projects._project_lock(root):
            pass
    # ...but a status read does not create or require any disk lock, even on
    # read-only media. Check behavior rather than an implementation string.
    service = projects.ReplayProjectService(root)
    fake_manifest = {"projectId": "b"*32, "name": "retained", "scope": {}, "captures": [],
                     "knowledge": {"workspaces": [], "materials": [], "aliases": []}}
    with mock.patch.object(service, "_load", return_value=fake_manifest), \
            mock.patch.object(projects, "_project_lock", side_effect=PermissionError("read-only")) as disk_lock:
        assert service.status()["projectId"] == "b"*32
        disk_lock.assert_not_called()
    assert lock_path.read_text(encoding="ascii") == "a"*32
