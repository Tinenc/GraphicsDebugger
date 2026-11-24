from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
import uuid


REPOSITORY = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"


def _load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    fake_renderdoc.ShaderStage = types.SimpleNamespace(
        Vertex="vs",
        Hull="hs",
        Domain="ds",
        Geometry="gs",
        Fragment="ps",
        Compute="cs",
        Task="as",
        Mesh="ms",
    )
    previous_renderdoc = sys.modules.get("renderdoc")
    previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_bridge_catalog_rebind_%s" % uuid.uuid4().hex,
            BRIDGE_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_renderdoc is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous_renderdoc
        if previous_qrenderdoc is not None:
            sys.modules["qrenderdoc"] = previous_qrenderdoc


def _catalog(capture_path):
    raw_bytes = b"fixed captured shader bytes"
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    shader_key = "77:fragment:main:%s" % raw_sha256
    shader_identity = {
        "shaderKey": shader_key,
        "shaderResourceId": 77,
        "resourceId": 77,
        "stage": "fragment",
        "entryPoint": "main",
        "rawSha256": raw_sha256,
    }
    catalog = {
        "schema": "renderdoc-ultra-capture-symbols/v1",
        "schemaVersion": 1,
        "capturePath": capture_path,
        "api": "GraphicsAPI.D3D11",
        "capture": {
            "path": capture_path,
            "api": "GraphicsAPI.D3D11",
            "renderer": "D3D11",
            "frameNumber": 1,
        },
        "selection": {
            "mode": "explicit",
            "eventIds": [321, 322],
            "stages": ["fragment"],
        },
        "shaders": [dict(shader_identity, rawPath="blobs/%s.bin" % raw_sha256)],
        "uses": [dict(shader_identity, eventId=321)],
        "errors": [],
        "counts": {"events": 2, "shaders": 1, "uses": 1, "errors": 0},
    }
    return catalog, {raw_sha256: raw_bytes}


def _change_shader_identity(catalog):
    changed = copy.deepcopy(catalog)
    raw_sha256 = changed["shaders"][0]["rawSha256"]
    shader_key = "77:fragment:alternate:%s" % raw_sha256
    for record in (changed["shaders"][0], changed["uses"][0]):
        record["entryPoint"] = "alternate"
        record["shaderKey"] = shader_key
    return changed


class _AliasSwitchContext:
    def __init__(self, capture_path):
        self.capture_path = str(capture_path)
        self.names = {
            12: (True, "BreadManaged"),
            13: (True, "UserTakeover"),
        }
        self.captured_names = {12: "CapturedTwelve", 13: "CapturedThirteen"}
        self.set_calls = []
        self.fail_resource = None

    def IsCaptureLoaded(self):
        return True

    def CurEvent(self):
        return 9

    def GetCaptureFilename(self):
        return self.capture_path

    def GetResources(self):
        return [
            types.SimpleNamespace(resourceId=resource_id)
            for resource_id in sorted(self.names)
        ]

    def HasResourceCustomName(self, resource_id):
        return self.names[int(resource_id)][0]

    def GetResourceNameUnsuffixed(self, resource_id):
        return self.names[int(resource_id)][1]

    def SetResourceCustomName(self, resource_id, name):
        resource_id = int(resource_id)
        self.set_calls.append((resource_id, str(name)))
        if resource_id == self.fail_resource:
            raise RuntimeError("injected catalog-switch rollback failure")
        if name:
            self.names[resource_id] = (True, str(name))
        else:
            self.names[resource_id] = (
                False, self.captured_names[resource_id]
            )


class BridgeCatalogRebindTests(unittest.TestCase):
    def _configure(self, module, catalog, blobs):
        capture_path = catalog["capturePath"]
        module._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: 9,
            GetCaptureFilename=lambda: capture_path,
        )
        module._replay = lambda unused_callback: (
            copy.deepcopy(catalog), dict(blobs))

    def _seed_state(self, module):
        old_capture_id = "a" * 64
        old_capture_path = os.path.normcase(os.path.abspath("D:/captures/old.rdc"))
        module._symbol_capture_snapshot = {
            "captureId": old_capture_id,
            "capturePath": "D:/captures/old.rdc",
            "shaderKeys": {(12, "vertex", "main", "b" * 64)},
            "usesByKey": {(12, "vertex", "main", "b" * 64): [7]},
        }
        module._known_catalog_capture_ids = {old_capture_id: old_capture_path}
        module._known_catalog_identity_pairs = {(old_capture_id, "c" * 64)}
        module._symbol_alias_overlay = {
            "schema": module.SYMBOL_ALIAS_SCHEMA,
            "schemaVersion": module.SYMBOL_ALIAS_VERSION,
            "captureId": old_capture_id,
            "sourcePath": "D:/aliases.json",
            "confirmedOnly": True,
            "allAliases": [{"displayName": "old alias"}],
            "aliases": [{"displayName": "old alias"}],
        }
        module._symbol_resource_renames = {
            12: {"name": "CapturedName", "hadCustomName": True}
        }
        module._symbol_rename_capture_token = ("loaded", old_capture_path)
        return {
            "snapshot": copy.deepcopy(module._symbol_capture_snapshot),
            "known": copy.deepcopy(module._known_catalog_capture_ids),
            "pairs": copy.deepcopy(module._known_catalog_identity_pairs),
            "overlay": copy.deepcopy(module._symbol_alias_overlay),
            "renames": copy.deepcopy(module._symbol_resource_renames),
            "token": module._symbol_rename_capture_token,
        }

    def _assert_state_unchanged(self, module, original):
        self.assertEqual(module._symbol_capture_snapshot, original["snapshot"])
        self.assertEqual(module._known_catalog_capture_ids, original["known"])
        self.assertEqual(module._known_catalog_identity_pairs, original["pairs"])
        self.assertEqual(module._symbol_alias_overlay, original["overlay"])
        self.assertEqual(module._symbol_resource_renames, original["renames"])
        self.assertEqual(module._symbol_rename_capture_token, original["token"])

    def _seed_committed_alias_switch(self, module, context):
        old_capture_id = "a" * 64
        old_snapshot = {
            "captureId": old_capture_id,
            "capturePath": context.capture_path,
            "shaderKeys": {(12, "vertex", "main", "b" * 64)},
            "usesByKey": {(12, "vertex", "main", "b" * 64): [7]},
        }
        capture_token = (
            "loaded",
            os.path.normcase(os.path.abspath(context.capture_path)),
        )
        original_overlay = module._new_symbol_alias_overlay()
        committed_overlay = {
            "schema": module.SYMBOL_ALIAS_SCHEMA,
            "schemaVersion": module.SYMBOL_ALIAS_VERSION,
            "captureId": old_capture_id,
            "sourcePath": "D:/aliases-old.json",
            "confirmedOnly": True,
            "allAliases": [{"alias": "BreadManaged"}],
            "aliases": [{"alias": "BreadManaged"}],
        }
        resource_renames = {
            12: {
                "hadCustomName": True,
                "name": "CapturedTwelve",
                "managedName": "BreadManaged",
                "bindingKind": "shader",
            },
            13: {
                "hadCustomName": True,
                "name": "CapturedThirteen",
                "managedName": "BreadManagedThirteen",
                "bindingKind": "shader",
            },
        }
        ledger = {
            "schema": "renderdoc-ultra-alias-recovery-ledger/v1",
            "transactionId": "d" * 32,
            "captureId": old_capture_id,
            "captureSessionId": "e" * 32,
            "captureToken": capture_token,
            "state": "committed",
            "manifestSha256": "f" * 64,
            "originalOverlay": original_overlay,
            "originalResourceRenames": {},
            "originalRenameCaptureToken": None,
            "resources": {
                12: {
                    "rid": 12,
                    "hadCustomName": True,
                    "name": "CapturedTwelve",
                },
                13: {
                    "rid": 13,
                    "hadCustomName": True,
                    "name": "CapturedThirteen",
                },
            },
            "managedNames": {
                12: {"hadCustomName": True, "name": "BreadManaged"},
                13: {
                    "hadCustomName": True,
                    "name": "BreadManagedThirteen",
                },
            },
        }
        module._capture_session_id = "e" * 32
        module._symbol_capture_snapshot = old_snapshot
        module._symbol_alias_overlay = committed_overlay
        module._symbol_resource_renames = resource_renames
        module._symbol_rename_capture_token = capture_token
        module._symbol_alias_recovery_ledger = ledger
        module._symbol_alias_last_rollback_receipt = None
        module._known_catalog_capture_ids = {
            old_capture_id: capture_token[1]
        }
        module._known_catalog_identity_pairs = set()
        module._known_catalog_stable_pairs = set()
        return old_snapshot, ledger

    def test_expected_mismatches_never_publish_or_write_catalog_identity(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            base_catalog, blobs = _catalog("D:/captures/current.rdc")
            expected_capture_id = module._catalog_capture_id(base_catalog)
            expected_evidence = module._catalog_identity_evidence_sha256(base_catalog)

            use_changed = copy.deepcopy(base_catalog)
            use_changed["uses"][0]["eventId"] = 322
            selection_changed = copy.deepcopy(base_catalog)
            selection_changed["selection"]["eventIds"] = [322, 321]
            cases = (
                (
                    "capture",
                    base_catalog,
                    "f" * 64,
                    expected_evidence,
                    "expected_capture_id",
                ),
                (
                    "use",
                    use_changed,
                    expected_capture_id,
                    expected_evidence,
                    "selection/Shader/use evidence changed",
                ),
                (
                    "selection",
                    selection_changed,
                    expected_capture_id,
                    expected_evidence,
                    "selection/Shader/use evidence changed",
                ),
                (
                    "shader",
                    _change_shader_identity(base_catalog),
                    expected_capture_id,
                    expected_evidence,
                    "expected_capture_id",
                ),
            )
            for name, live_catalog, wanted_capture_id, wanted_evidence, error in cases:
                with self.subTest(name=name):
                    original = self._seed_state(module)
                    self._configure(module, live_catalog, blobs)
                    output_dir = Path(temporary) / name / "export"
                    with self.assertRaisesRegex(RuntimeError, error):
                        module._export_shader_catalog({
                            "output_dir": str(output_dir),
                            "event_ids": [321, 322],
                            "stages": ["fragment"],
                            "expected_capture_id": wanted_capture_id,
                            "expected_identity_evidence_sha256": wanted_evidence,
                        })
                    self._assert_state_unchanged(module, original)
                    self.assertNotIn(
                        wanted_capture_id, module._known_catalog_capture_ids)
                    self.assertNotIn(
                        (wanted_capture_id, wanted_evidence),
                        module._known_catalog_identity_pairs,
                    )
                    with self.assertRaisesRegex(
                            RuntimeError, "no bridge catalog identity"):
                        module._require_capture_identity(wanted_capture_id)
                    self.assertFalse(output_dir.exists())

    def test_matching_expected_identity_is_published_before_files_are_returned(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            catalog, blobs = _catalog("D:/captures/current.rdc")
            capture_id = module._catalog_capture_id(catalog)
            evidence_sha256 = module._catalog_identity_evidence_sha256(catalog)
            self._configure(module, catalog, blobs)
            original = self._seed_state(module)
            output_dir = Path(temporary) / "expected"

            result = module._export_shader_catalog({
                "output_dir": str(output_dir),
                "event_ids": [321, 322],
                "stages": ["fragment"],
                "expected_capture_id": capture_id,
                "expected_identity_evidence_sha256": evidence_sha256,
            })

            self.assertEqual(result["captureId"], capture_id)
            self.assertEqual(result["identityEvidenceSha256"], evidence_sha256)
            self.assertIs(result["expectedIdentityValidatedBeforeCache"], True)
            self.assertIn(capture_id, module._known_catalog_capture_ids)
            self.assertIn(
                (capture_id, evidence_sha256),
                module._known_catalog_identity_pairs,
            )
            # Restart rebinding is identity-only. It must not switch the active
            # symbol catalog or discard aliases selected by the user.
            self.assertEqual(
                module._symbol_capture_snapshot, original["snapshot"])
            self.assertEqual(module._symbol_alias_overlay, original["overlay"])
            self.assertEqual(module._symbol_resource_renames, original["renames"])
            self.assertTrue((output_dir / "shader_catalog.v1.json").is_file())
            self.assertEqual(len(list((output_dir / "blobs").glob("*.bin"))), 1)

    def test_matching_expected_stable_identity_can_activate_symbol_catalog(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            catalog, blobs = _catalog("D:/captures/current.rdc")
            capture_id = module._catalog_capture_id(catalog)
            stable_capture_id = module._catalog_stable_capture_id(catalog)
            stable_evidence_sha256 = (
                module._catalog_stable_identity_evidence_sha256(catalog)
            )
            self._configure(module, catalog, blobs)
            module._symbol_capture_snapshot = {
                "captureId": "a" * 64,
                "capturePath": "D:/captures/current.rdc",
                "shaderKeys": set(),
                "usesByKey": {},
            }
            module._known_catalog_capture_ids = {}
            module._known_catalog_identity_pairs = set()
            module._known_catalog_stable_pairs = set()
            module._symbol_alias_overlay = module._new_symbol_alias_overlay()
            module._symbol_resource_renames = {}
            module._symbol_rename_capture_token = None
            module._symbol_alias_recovery_ledger = None
            output_dir = Path(temporary) / "active-expected"

            result = module._export_shader_catalog({
                "output_dir": str(output_dir),
                "event_ids": [321, 322],
                "stages": ["fragment"],
                "expected_stable_capture_id": stable_capture_id,
                "expected_stable_identity_evidence_sha256": (
                    stable_evidence_sha256
                ),
                "activate_catalog_if_valid": True,
            })

            self.assertIs(result["expectedIdentityValidatedBeforeCache"], True)
            self.assertIs(result["activeCatalogPublished"], True)
            self.assertEqual(module._symbol_capture_snapshot["captureId"], capture_id)
            self.assertEqual(
                module._symbol_capture_snapshot["usesByKey"][
                    (77, "fragment", "main", catalog["shaders"][0]["rawSha256"])
                ],
                [321],
            )
            self.assertIn(capture_id, module._known_catalog_capture_ids)
            self.assertIn(
                (stable_capture_id, stable_evidence_sha256),
                module._known_catalog_stable_pairs,
            )

    def test_expected_activation_recovers_old_aliases_then_switches_catalog(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            catalog, blobs = _catalog("D:/captures/current.rdc")
            capture_id = module._catalog_capture_id(catalog)
            stable_capture_id = module._catalog_stable_capture_id(catalog)
            stable_evidence_sha256 = (
                module._catalog_stable_identity_evidence_sha256(catalog)
            )
            context = _AliasSwitchContext(catalog["capturePath"])
            module._ctx = context
            module._replay = lambda unused_callback: (
                copy.deepcopy(catalog), dict(blobs)
            )
            unused_old_snapshot, unused_ledger = (
                self._seed_committed_alias_switch(module, context)
            )

            result = module._export_shader_catalog({
                "output_dir": str(Path(temporary) / "alias-switch"),
                "event_ids": [321, 322],
                "stages": ["fragment"],
                "expected_stable_capture_id": stable_capture_id,
                "expected_stable_identity_evidence_sha256": (
                    stable_evidence_sha256
                ),
                "activate_catalog_if_valid": True,
            })

            self.assertIs(result["activeCatalogPublished"], True)
            self.assertEqual(module._symbol_capture_snapshot["captureId"], capture_id)
            self.assertIsNone(module._symbol_alias_recovery_ledger)
            self.assertEqual(module._symbol_alias_overlay["aliases"], [])
            self.assertEqual(module._symbol_resource_renames, {})
            self.assertEqual(context.names[12], (True, "CapturedTwelve"))
            # A custom name that no longer equals Bread's managed value belongs
            # to the user and survives the automatic catalog switch.
            self.assertEqual(context.names[13], (True, "UserTakeover"))
            self.assertIn((12, "CapturedTwelve"), context.set_calls)
            self.assertNotIn((13, "CapturedThirteen"), context.set_calls)

    def test_expected_activation_keeps_old_catalog_when_alias_recovery_fails(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            catalog, blobs = _catalog("D:/captures/current.rdc")
            stable_capture_id = module._catalog_stable_capture_id(catalog)
            stable_evidence_sha256 = (
                module._catalog_stable_identity_evidence_sha256(catalog)
            )
            context = _AliasSwitchContext(catalog["capturePath"])
            context.fail_resource = 12
            module._ctx = context
            module._replay = lambda unused_callback: (
                copy.deepcopy(catalog), dict(blobs)
            )
            old_snapshot, ledger = self._seed_committed_alias_switch(
                module, context
            )

            with self.assertRaisesRegex(RuntimeError, "recovery remains required"):
                module._export_shader_catalog({
                    "output_dir": str(Path(temporary) / "alias-switch-fails"),
                    "event_ids": [321, 322],
                    "stages": ["fragment"],
                    "expected_stable_capture_id": stable_capture_id,
                    "expected_stable_identity_evidence_sha256": (
                        stable_evidence_sha256
                    ),
                    "activate_catalog_if_valid": True,
                })

            self.assertIs(module._symbol_capture_snapshot, old_snapshot)
            self.assertIs(module._symbol_alias_recovery_ledger, ledger)
            self.assertEqual(
                module._symbol_alias_overlay["captureId"], old_snapshot["captureId"]
            )

    def test_unvalidated_catalog_switch_still_requires_explicit_alias_recovery(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            catalog, blobs = _catalog("D:/captures/current.rdc")
            context = _AliasSwitchContext(catalog["capturePath"])
            module._ctx = context
            module._replay = lambda unused_callback: (
                copy.deepcopy(catalog), dict(blobs)
            )
            old_snapshot, ledger = self._seed_committed_alias_switch(
                module, context
            )

            with self.assertRaisesRegex(
                RuntimeError, "Rollback the capture-bound symbol aliases"
            ):
                module._export_shader_catalog({
                    "output_dir": str(Path(temporary) / "ordinary-switch"),
                    "event_ids": [321, 322],
                    "stages": ["fragment"],
                })

            self.assertIs(module._symbol_capture_snapshot, old_snapshot)
            self.assertIs(module._symbol_alias_recovery_ledger, ledger)
            self.assertEqual(context.names[12], (True, "BreadManaged"))

    def test_active_catalog_request_still_fails_before_publication_on_mismatch(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            expected_catalog, blobs = _catalog("D:/captures/current.rdc")
            live_catalog = _change_shader_identity(expected_catalog)
            self._configure(module, live_catalog, blobs)
            original = self._seed_state(module)

            with self.assertRaisesRegex(RuntimeError, "stable_capture_id"):
                module._export_shader_catalog({
                    "output_dir": str(Path(temporary) / "mismatch-active"),
                    "event_ids": [321, 322],
                    "stages": ["fragment"],
                    "expected_stable_capture_id": (
                        module._catalog_stable_capture_id(expected_catalog)
                    ),
                    "expected_stable_identity_evidence_sha256": (
                        module._catalog_stable_identity_evidence_sha256(
                            expected_catalog
                        )
                    ),
                    "activate_catalog_if_valid": True,
                })

            self._assert_state_unchanged(module, original)

    def test_alias_shader_revalidation_uses_retained_original_for_owned_replacement(self):
        module = _load_bridge()
        resource_id = 77
        event_id = 321
        original_bytes = b"captured original shader"
        candidate_bytes = b"active Bread replacement"
        original_sha256 = hashlib.sha256(original_bytes).hexdigest()
        candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        key = (resource_id, "fragment", "main", original_sha256)
        entry = types.SimpleNamespace(name="main")
        original_reflection = types.SimpleNamespace(
            entryPoint="main", rawBytes=original_bytes)
        active_reflection = types.SimpleNamespace(
            entryPoint="main", rawBytes=candidate_bytes)
        capture_token = ("loaded", "D:/captures/current.rdc")

        class Pipeline:
            def GetShader(self, unused_stage):
                return resource_id

        class Controller:
            def __init__(self):
                self.pipeline = Pipeline()

            def GetPipelineState(self):
                return self.pipeline

            def GetShaderEntryPoints(self, shader_id):
                self.assert_shader(shader_id)
                return [entry]

            def GetShader(self, unused_pipeline, shader_id, selected_entry):
                self.assert_shader(shader_id)
                self.assertIs(selected_entry, entry)
                return active_reflection

            def assert_shader(self, shader_id):
                if module._resource_id_int(shader_id) != resource_id:
                    raise AssertionError("unexpected Shader resource")

            def assertIs(self, left, right):
                if left is not right:
                    raise AssertionError("unexpected entry point")

        controller = Controller()
        module._symbol_capture_snapshot = {
            "captureId": "a" * 64,
            "capturePath": capture_token[1],
            "shaderKeys": {key},
            "usesByKey": {key: [event_id]},
        }
        module._capture_session_id = "b" * 32
        module._active_capture_token = capture_token
        module._shared_shader_replacement_capture_token = capture_token
        module._shared_shader_replacements = {
            resource_id: {
                "captureSessionId": module._capture_session_id,
                "captureToken": capture_token,
                "api": "d3d12",
                "originalResource": resource_id,
                "expectedOriginalResourceId": resource_id,
                "expectedOriginalSha256": original_sha256,
                "stageName": "fragment",
                "entryPoint": "main",
                "candidateRawSha256": candidate_sha256,
                "capturedOriginalReflection": original_reflection,
            }
        }
        module._available_shader_stages = lambda unused=None: [("fragment", "ps")]
        module._request_visible_event_id = lambda unused=0: 9
        module._set_frame_event = lambda unused_ctrl, unused_event, unused_force: None
        module._get_pipeline_obj = lambda unused_pipeline, unused_ctrl: 901
        module._current_capture_token = lambda: capture_token
        module._shader_api_token = lambda unused_ctrl: "d3d12"
        module._replay = lambda callback: callback(controller)

        verified, failures = module._revalidate_symbol_shader_keys({key})

        self.assertEqual(verified, {key})
        self.assertEqual(failures, {})

    def test_alias_shader_revalidation_rejects_foreign_owner_and_target(self):
        module = _load_bridge()
        resource_id = 77
        event_id = 321
        original_bytes = b"captured original shader"
        candidate_bytes = b"active Bread replacement"
        original_sha256 = hashlib.sha256(original_bytes).hexdigest()
        candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        key = (resource_id, "fragment", "main", original_sha256)
        entry = types.SimpleNamespace(name="main")
        original_reflection = types.SimpleNamespace(
            entryPoint="main", rawBytes=original_bytes)
        active_reflection = types.SimpleNamespace(
            entryPoint="main", rawBytes=candidate_bytes)
        capture_token = ("loaded", "D:/captures/current.rdc")

        class Pipeline:
            def GetShader(self, unused_stage):
                return resource_id

        class Controller:
            def GetPipelineState(self):
                return Pipeline()

            def GetShaderEntryPoints(self, unused_shader_id):
                return [entry]

            def GetShader(self, unused_pipeline, unused_shader_id, unused_entry):
                return active_reflection

        module._symbol_capture_snapshot = {
            "captureId": "a" * 64,
            "capturePath": capture_token[1],
            "shaderKeys": {key},
            "usesByKey": {key: [event_id]},
        }
        module._capture_session_id = "b" * 32
        module._active_capture_token = capture_token
        module._shared_shader_replacement_capture_token = capture_token
        owner = {
            "captureSessionId": module._capture_session_id,
            "captureToken": capture_token,
            "api": "d3d12",
            "originalResource": resource_id,
            "expectedOriginalResourceId": resource_id,
            "expectedOriginalSha256": original_sha256,
            "stageName": "fragment",
            "entryPoint": "main",
            "candidateRawSha256": candidate_sha256,
            "capturedOriginalReflection": original_reflection,
        }
        module._shared_shader_replacements = {resource_id: owner}
        module._available_shader_stages = lambda unused=None: [("fragment", "ps")]
        module._request_visible_event_id = lambda unused=0: 9
        module._set_frame_event = lambda unused_ctrl, unused_event, unused_force: None
        module._get_pipeline_obj = lambda unused_pipeline, unused_ctrl: 901
        module._current_capture_token = lambda: capture_token
        module._shader_api_token = lambda unused_ctrl: "d3d12"
        module._replay = lambda callback: callback(Controller())

        owner["captureSessionId"] = "c" * 32
        verified, failures = module._revalidate_symbol_shader_keys({key})
        self.assertEqual(verified, set())
        self.assertIn("another capture", failures[key])

        owner["captureSessionId"] = module._capture_session_id
        active_reflection.rawBytes = b"replacement changed outside Bread"
        verified, failures = module._revalidate_symbol_shader_keys({key})
        self.assertEqual(verified, set())
        self.assertIn("changed outside Bread", failures[key])

    def test_export_without_expected_identity_remains_backward_compatible(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            catalog, blobs = _catalog("D:/captures/current.rdc")
            capture_id = module._catalog_capture_id(catalog)
            self._configure(module, catalog, blobs)
            module._symbol_capture_snapshot = {
                "captureId": "",
                "capturePath": "",
                "shaderKeys": set(),
                "usesByKey": {},
            }
            module._known_catalog_capture_ids = {}
            module._known_catalog_identity_pairs = set()
            output_dir = Path(temporary) / "ordinary"

            result = module._export_shader_catalog({
                "output_dir": str(output_dir),
                "event_ids": [321, 322],
                "stages": ["fragment"],
            })

            self.assertEqual(result["captureId"], capture_id)
            self.assertEqual(
                result["identityEvidenceSha256"],
                module._catalog_identity_evidence_sha256(catalog),
            )
            self.assertNotIn("expectedIdentityValidatedBeforeCache", result)
            self.assertIn(capture_id, module._known_catalog_capture_ids)
            self.assertIn(
                (capture_id, result["identityEvidenceSha256"]),
                module._known_catalog_identity_pairs,
            )
            self.assertTrue((output_dir / "shader_catalog.v1.json").is_file())

    def test_full_export_reuses_reflection_but_keeps_per_event_uses_and_bindings(self):
        module = _load_bridge()
        capture_path = "D:/captures/repeated-shader.rdc"
        shader_resource_id = 77
        pipeline_resource_id = 901
        entry = types.SimpleNamespace(name="main")
        block = types.SimpleNamespace(
            name="PerDraw",
            fixedBindNumber=2,
            fixedBindSetOrSpace=0,
            bindArraySize=1,
            byteSize=64,
            bufferBacked=True,
            inlineDataBytes=False,
            compileConstants=False,
            variables=[],
        )
        reflection = types.SimpleNamespace(
            entryPoint="main",
            encoding="dxbc",
            rawBytes=b"one exact shader used by two events",
            constantBlocks=[block],
            inputSignature=[],
            outputSignature=[],
            readOnlyResources=[],
            readWriteResources=[],
            samplers=[],
            interfaces=[],
        )

        class Pipeline:
            def __init__(self, controller):
                self.controller = controller
                self.constant_block_calls = []

            def GetShader(self, unused_stage):
                return shader_resource_id

            def GetShaderEntryPoint(self, unused_stage):
                return "main"

            def GetShaderPipelineObject(self):
                return pipeline_resource_id

            def GetConstantBlock(self, stage, block_index, array_index):
                self.constant_block_calls.append(
                    (self.controller.event_id, stage, block_index, array_index))
                return types.SimpleNamespace(descriptor=types.SimpleNamespace(
                    resource=1000 + self.controller.event_id,
                    byteOffset=self.controller.event_id * 16,
                    byteSize=64,
                ))

        class Controller:
            def __init__(self):
                self.event_id = 0
                self.entry_point_calls = 0
                self.reflection_calls = 0
                self.pipeline = Pipeline(self)

            def GetRootActions(self):
                raise AssertionError(
                    "explicit representative-EID export must not scan actions"
                )

            def GetAPIProperties(self):
                return types.SimpleNamespace(
                    pipelineType="GraphicsAPI.D3D12", localRenderer="D3D12")

            def GetFrameInfo(self):
                return types.SimpleNamespace(frameNumber=1)

            def SetFrameEvent(self, event_id, unused_force):
                self.event_id = event_id

            def GetPipelineState(self):
                return self.pipeline

            def GetShaderEntryPoints(self, shader_id):
                self.assert_shader(shader_id)
                self.entry_point_calls += 1
                return [entry]

            def GetShader(self, pipeline_id, shader_id, selected_entry):
                if pipeline_id != pipeline_resource_id:
                    raise AssertionError("unexpected pipeline")
                self.assert_shader(shader_id)
                if selected_entry is not entry:
                    raise AssertionError("unexpected entry")
                self.reflection_calls += 1
                return reflection

            @staticmethod
            def assert_shader(shader_id):
                if shader_id != shader_resource_id:
                    raise AssertionError("unexpected shader")

        controller = Controller()
        module._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: 9,
            GetCaptureFilename=lambda: capture_path,
        )
        module._replay = lambda callback: callback(controller)
        module._known_catalog_capture_ids = {}
        module._known_catalog_identity_pairs = set()
        module._symbol_capture_snapshot = {
            "captureId": "",
            "capturePath": "",
            "shaderKeys": set(),
            "usesByKey": {},
        }

        original_layout = module._constant_block_layout
        layout_calls = []

        def counting_layout(constant_block):
            layout_calls.append(constant_block)
            return original_layout(constant_block)

        module._constant_block_layout = counting_layout
        with tempfile.TemporaryDirectory() as temporary:
            progress_root = Path(temporary) / "instance"
            progress_root.mkdir()
            module.IPC_DIR = str(progress_root)
            module.INSTANCE_ID = "123-%s" % ("d" * 32)
            module._capture_session_id = "e" * 32
            output_dir = Path(temporary) / "cached-reflection"
            module._export_shader_catalog({
                "output_dir": str(output_dir),
                "event_ids": [101, 202],
                "stages": ["fragment"],
                "progress_token": "f" * 32,
                "progress_capture_session_id": "e" * 32,
            })
            catalog = json.loads(
                (output_dir / "shader_catalog.v1.json").read_text(
                    encoding="utf-8"))
            progress = json.loads(
                (progress_root / (
                    "shader-catalog-progress-%s.json" % ("f" * 32)
                )).read_text(encoding="utf-8")
            )

        self.assertEqual(controller.entry_point_calls, 1)
        self.assertEqual(controller.reflection_calls, 1)
        self.assertEqual(layout_calls, [block])
        self.assertEqual(
            [call[0] for call in controller.pipeline.constant_block_calls],
            [101, 202],
        )
        self.assertEqual(len(catalog["shaders"]), 1)
        self.assertEqual([use["eventId"] for use in catalog["uses"]], [101, 202])
        self.assertEqual(
            [use["constantBlocks"][0]["binding"]["resourceId"]
             for use in catalog["uses"]],
            [1101, 1202],
        )
        self.assertEqual(progress["operationToken"], "f" * 32)
        self.assertEqual(progress["captureSessionId"], "e" * 32)
        self.assertEqual(progress["instanceId"], "123-%s" % ("d" * 32))
        self.assertEqual(progress["eventId"], 202)
        self.assertEqual((progress["current"], progress["total"]), (2, 2))

    def test_progress_binding_rejects_invalid_token_or_capture_session(self):
        module = _load_bridge()
        module._capture_session_id = "a" * 32
        with self.assertRaisesRegex(ValueError, "progress_token"):
            module._shader_catalog_progress_binding(
                {
                    "progress_token": "../escape",
                    "progress_capture_session_id": "a" * 32,
                }
            )
        with self.assertRaisesRegex(RuntimeError, "Capture session changed"):
            module._shader_catalog_progress_binding(
                {
                    "progress_token": "b" * 32,
                    "progress_capture_session_id": "c" * 32,
                }
            )

    def test_ordinary_all_draw_export_allows_no_events_or_shaders(self):
        module = _load_bridge()
        with tempfile.TemporaryDirectory() as temporary:
            capture_path = "D:/captures/empty.rdc"
            catalog = {
                "schema": "renderdoc-ultra-capture-symbols/v1",
                "schemaVersion": 1,
                "capturePath": capture_path,
                "api": "GraphicsAPI.D3D11",
                "capture": {
                    "path": capture_path,
                    "api": "GraphicsAPI.D3D11",
                    "renderer": "D3D11",
                    "frameNumber": 1,
                },
                "selection": {
                    "mode": "all_draw_dispatch",
                    "eventIds": [],
                    "stages": ["fragment"],
                },
                "shaders": [],
                "uses": [],
                "errors": [],
                "counts": {
                    "events": 0,
                    "shaderUses": 0,
                    "uniqueShaders": 0,
                    "uniqueBlobs": 0,
                    "errors": 0,
                },
            }
            capture_id = module._catalog_capture_id(catalog)
            evidence_sha256 = module._catalog_identity_evidence_sha256(catalog)
            self._configure(module, catalog, {})
            module._symbol_capture_snapshot = {
                "captureId": "",
                "capturePath": "",
                "shaderKeys": set(),
                "usesByKey": {},
            }
            module._known_catalog_capture_ids = {}
            module._known_catalog_identity_pairs = set()
            output_dir = Path(temporary) / "empty"

            result = module._export_shader_catalog({
                "output_dir": str(output_dir),
                "stages": ["fragment"],
            })

            self.assertEqual(result["captureId"], capture_id)
            self.assertEqual(result["identityEvidenceSha256"], evidence_sha256)
            self.assertEqual(result["counts"]["uniqueShaders"], 0)
            self.assertIn(
                (capture_id, evidence_sha256),
                module._known_catalog_identity_pairs,
            )
            self.assertEqual(module._symbol_capture_snapshot["shaderKeys"], set())
            self.assertTrue((output_dir / "shader_catalog.v1.json").is_file())

    def test_cache_capacity_failure_is_transactional(self):
        module = _load_bridge()
        catalog, unused_blobs = _catalog("D:/captures/current.rdc")
        catalog["captureId"] = module._catalog_capture_id(catalog)
        original = self._seed_state(module)
        module._known_catalog_capture_ids = {
            "%064x" % index: "D:/captures/known.rdc"
            for index in range(module.MAX_KNOWN_CATALOG_CAPTURE_IDS)
        }
        original["known"] = copy.deepcopy(module._known_catalog_capture_ids)

        with self.assertRaisesRegex(RuntimeError, "Too many catalog identities"):
            module._cache_symbol_catalog(catalog)

        self._assert_state_unchanged(module, original)

    def test_exact_pair_capacity_failure_is_transactional_and_identity_only(self):
        module = _load_bridge()
        catalog, unused_blobs = _catalog("D:/captures/current.rdc")
        capture_id = module._catalog_capture_id(catalog)
        catalog["captureId"] = capture_id
        original = self._seed_state(module)
        module._known_catalog_capture_ids = {
            capture_id: os.path.normcase(os.path.abspath(catalog["capturePath"]))
        }
        module._known_catalog_identity_pairs = {
            (capture_id, "%064x" % index)
            for index in range(module.MAX_KNOWN_CATALOG_IDENTITY_PAIRS)
        }
        original["known"] = copy.deepcopy(module._known_catalog_capture_ids)
        original["pairs"] = copy.deepcopy(module._known_catalog_identity_pairs)

        with self.assertRaisesRegex(RuntimeError, "Too many exact catalog selections"):
            module._cache_catalog_identity_pair(catalog)

        self._assert_state_unchanged(module, original)

    def test_capture_status_exposes_bounded_exact_identity_pairs(self):
        module = _load_bridge()
        capture_path = "D:/captures/current.rdc"
        capture_id = "d" * 64
        evidence_sha256 = "e" * 64
        module._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            GetCaptureFilename=lambda: capture_path,
        )
        module._capture_session_id = "7" * 32
        module._known_catalog_capture_ids = {
            capture_id: os.path.normcase(os.path.abspath(capture_path))
        }
        module._known_catalog_identity_pairs = {
            (capture_id, evidence_sha256)
        }
        module._symbol_capture_snapshot = {
            "captureId": "",
            "capturePath": "",
            "shaderKeys": set(),
            "usesByKey": {},
        }
        controller = types.SimpleNamespace(
            GetAPIProperties=lambda: types.SimpleNamespace(
                pipelineType="GraphicsAPI.D3D11", localRenderer="D3D11"
            ),
            GetFrameInfo=lambda: types.SimpleNamespace(frameNumber=1),
        )
        module._replay = lambda callback: callback(controller)

        status = module._get_capture_status({})

        self.assertEqual(status["knownCatalogCaptureIds"], [capture_id])
        self.assertEqual(
            status["knownCatalogIdentityPairs"],
            [{
                "captureId": capture_id,
                "identityEvidenceSha256": evidence_sha256,
            }],
        )


if __name__ == "__main__":
    unittest.main()


class StableCaptureIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = _load_bridge()

    """A ResourceId is not an identity, and a workspace must survive one changing.

    RenderDoc reassigns ResourceIds every time a capture is opened. The identity a
    workspace used to be gated on hashed one in, so every stored workspace stopped
    reattaching after a restart -- observed in the field as three catalogs of the same
    .rdc, identical in path, API, stage, entry point and Shader bytes, differing only
    in 1000000000000000853 / 1000000000000000860 / 1000000000000012212.
    """

    @staticmethod
    def _catalog(resource_id):
        shader_key = "%d:fragment:MainPS:%s" % (resource_id, "3c" * 32)
        return {
            "schema": "renderdoc-ultra-capture-symbols/v1",
            "schemaVersion": 1,
            "capturePath": r"D:\captures\6.rdc",
            "api": "GraphicsAPI.D3D12",
            "selection": {"mode": "explicit", "eventIds": [11329],
                          "stages": ["fragment"]},
            "shaders": [{
                "shaderResourceId": resource_id, "resourceId": resource_id,
                "stage": "fragment", "entryPoint": "MainPS", "entry": "MainPS",
                "rawSha256": "3c" * 32, "shaderKey": shader_key,
            }],
            "uses": [{
                "eventId": 11329, "shaderResourceId": resource_id,
                "resourceId": resource_id, "stage": "fragment",
                "entryPoint": "MainPS", "rawSha256": "3c" * 32,
                "shaderKey": shader_key,
            }],
        }

    def test_the_volatile_capture_id_does_change_with_the_resource_id(self) -> None:
        """Kept as-is: it is honest provenance for one replay session."""

        first = self.bridge._catalog_capture_id(self._catalog(853))
        second = self.bridge._catalog_capture_id(self._catalog(860))
        self.assertNotEqual(first, second)

    def test_the_stable_identity_ignores_the_resource_id(self) -> None:
        for other in (860, 12212, 1):
            self.assertEqual(
                self.bridge._catalog_stable_capture_id(self._catalog(853)),
                self.bridge._catalog_stable_capture_id(self._catalog(other)),
            )

    def test_the_stable_identity_still_separates_different_shaders(self) -> None:
        changed = self._catalog(853)
        changed["shaders"][0]["rawSha256"] = "ab" * 32
        self.assertNotEqual(
            self.bridge._catalog_stable_capture_id(self._catalog(853)),
            self.bridge._catalog_stable_capture_id(changed),
        )
        renamed = self._catalog(853)
        renamed["shaders"][0]["entryPoint"] = "MainPS2"
        self.assertNotEqual(
            self.bridge._catalog_stable_capture_id(self._catalog(853)),
            self.bridge._catalog_stable_capture_id(renamed),
        )
        # The path is deliberately *not* part of it: the same file opened from the
        # recent list with other casing, moved a folder up, or a second capture of
        # the same build are the same Shaders. Whether a stored workspace may
        # reattach is decided by the use-evidence pair, not by this hash alone.
        moved = self._catalog(853)
        moved["capturePath"] = r"D:\captures\7.rdc"
        self.assertEqual(
            self.bridge._catalog_stable_capture_id(self._catalog(853)),
            self.bridge._catalog_stable_capture_id(moved),
        )

    def test_the_use_evidence_loses_its_resource_ids_too(self) -> None:
        first = self.bridge._catalog_stable_identity_evidence_sha256(self._catalog(853))
        second = self.bridge._catalog_stable_identity_evidence_sha256(self._catalog(12212))
        self.assertEqual(first, second)
        # ...but not the event it was used at, which is what the selection means.
        moved_event = self._catalog(853)
        moved_event["selection"]["eventIds"] = [11330]
        moved_event["uses"][0]["eventId"] = 11330
        self.assertNotEqual(
            first, self.bridge._catalog_stable_identity_evidence_sha256(moved_event))

    def test_a_shader_key_keeps_everything_except_the_resource_id(self) -> None:
        self.assertEqual(
            self.bridge._stable_shader_key("1000000000000000853:fragment:MainPS:abc"),
            "fragment:MainPS:abc",
        )
        # Nothing to strip is not an error; it is simply returned unchanged.
        self.assertEqual(self.bridge._stable_shader_key("fragment"), "fragment")
        self.assertEqual(self.bridge._stable_shader_key(""), "")
