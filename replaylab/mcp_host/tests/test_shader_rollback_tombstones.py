from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
import uuid
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"


class _ResourceId:
    def __init__(self, value=0):
        self.value = int(value)

    def __int__(self):
        return self.value


def _load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    fake_renderdoc.ResourceId = _ResourceId
    previous_renderdoc = sys.modules.get("renderdoc")
    previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_bridge_shader_rollback_%s" % uuid.uuid4().hex,
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


class _Context:
    def __init__(self):
        self.capture_path = "D:/captures/rollback.rdc"
        self.replacements = {}
        self.calls = []

    def IsCaptureLoaded(self):
        return True

    def GetCaptureFilename(self):
        return self.capture_path

    def CurEvent(self):
        return 5

    def IsResourceReplaced(self, original):
        return int(original) in self.replacements

    def GetResourceReplacement(self, original):
        return self.replacements.get(int(original), _ResourceId())

    def UnregisterReplacement(self, original):
        self.calls.append(("UnregisterReplacement", int(original)))
        self.replacements.pop(int(original), None)


class _Controller:
    def __init__(self):
        self.calls = []
        self.replacements = {}
        self.fail_free = set()

    def ClearEventShaderOverride(self, event_id, stage):
        self.calls.append(("ClearEventShaderOverride", int(event_id), stage))

    def SetFrameEvent(self, event_id, force):
        self.calls.append(("SetFrameEvent", int(event_id), bool(force)))

    def SetEventShaderOverride(self, event_id, stage, original, target):
        self.calls.append((
            "SetEventShaderOverride", int(event_id), stage,
            int(original), int(target),
        ))
        return ""

    def RemoveReplacement(self, original):
        self.calls.append(("RemoveReplacement", int(original)))
        self.replacements.pop(int(original), None)

    def FreeTargetResource(self, target):
        target_id = int(target)
        self.calls.append(("FreeTargetResource", target_id))
        if target_id in self.fail_free:
            raise RuntimeError("injected target free failure")


class ShaderRollbackTombstoneTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_bridge()
        self.context = _Context()
        self.controller = _Controller()
        self.module._ctx = self.context
        self.module._replay = lambda callback: callback(self.controller)
        self.capture_id = "c" * 64
        self.other_capture_id = "d" * 64
        self.capture_session_id = "7" * 32
        self.workspace_id = "b" * 32
        self.module._capture_session_id = self.capture_session_id
        capture_path = os.path.normcase(os.path.abspath(self.context.capture_path))
        self.module._known_catalog_capture_ids = {
            self.capture_id: capture_path,
            self.other_capture_id: capture_path,
        }
        self.module._symbol_capture_snapshot = {
            "captureId": self.capture_id,
            "capturePath": self.context.capture_path,
            "shaderKeys": set(),
            "usesByKey": {},
        }
        self.capture_token = self.module._current_capture_token()

    def _install_event_family(self, tokens, events, target_id, family_id):
        target = _ResourceId(target_id)
        records = {}
        for token, event_id in zip(tokens, events):
            records[token] = {
                "overrideId": token,
                "captureId": self.capture_id,
                "captureSessionId": self.capture_session_id,
                "captureToken": self.capture_token,
                "eventId": event_id,
                "stageName": "fragment",
                "stageObject": "Pixel",
                "entryPoint": "main",
                "expectedOriginalResourceId": 77,
                "expectedOriginalSha256": "9" * 64,
                "originalResource": _ResourceId(77),
                "candidateRawSha256": "a" * 64,
                "physicalAbiSha256": "8" * 64,
                "familyId": family_id,
                "targetResource": target,
                "targetResourceId": target_id,
            }
        self.module._event_shader_overrides.update(records)
        self.module._event_shader_targets[family_id] = {
            "targetResource": target,
            "targetResourceId": target_id,
            "overrideIds": set(tokens),
        }
        self.module._event_shader_override_capture_token = self.capture_token

    def _event_params(self, token):
        return {
            "override_id": token,
            "expected_capture_id": self.capture_id,
            "expected_capture_session_id": self.capture_session_id,
        }

    def _install_shared(self, token, original_id, target_id):
        original = _ResourceId(original_id)
        target = _ResourceId(target_id)
        self.context.replacements[original_id] = target
        self.controller.replacements[original_id] = target
        self.module._shared_shader_replacements[original_id] = {
            "replacementId": token,
            "captureId": self.capture_id,
            "captureSessionId": self.capture_session_id,
            "captureToken": self.capture_token,
            "workspaceId": self.workspace_id,
            "api": "d3d11",
            "encoding": "dxbc",
            "stageName": "fragment",
            "entryPoint": "main",
            "expectedOriginalResourceId": original_id,
            "expectedOriginalSha256": "9" * 64,
            "originalResource": original,
            "candidateRawSha256": "a" * 64,
            "physicalAbiSha256": "8" * 64,
            "familyId": "f" * 64,
            "targetResource": target,
            "targetResourceId": target_id,
        }
        self.module._shared_shader_replacement_capture_token = self.capture_token

    def _shared_params(self, token):
        return {
            "replacement_id": token,
            "expected_capture_id": self.capture_id,
            "expected_capture_session_id": self.capture_session_id,
            "workspace_id": self.workspace_id,
        }

    def test_single_and_family_retry_are_idempotent_and_bounded(self):
        single = "1" * 32
        self._install_event_family([single], [101], 91, "a" * 64)
        self.controller.fail_free.add(91)

        wrong_active_capture = self._event_params(single)
        wrong_active_capture["expected_capture_id"] = self.other_capture_id
        with self.assertRaisesRegex(RuntimeError, "different capture identity"):
            self.module._rollback_shader_candidate_override(wrong_active_capture)
        wrong_active_session = self._event_params(single)
        wrong_active_session["expected_capture_session_id"] = "8" * 32
        with self.assertRaisesRegex(RuntimeError, "Capture session changed"):
            self.module._rollback_shader_candidate_override(wrong_active_session)
        self.assertIn(single, self.module._event_shader_overrides)
        self.assertEqual(self.controller.calls, [])

        first = self.module._rollback_shader_candidate_override(
            self._event_params(single)
        )
        self.assertTrue(first["cleared"])
        self.assertFalse(first["targetFreed"])
        self.assertTrue(first["originalShaderPreserved"])
        self.assertFalse(first["alreadyRestored"])
        first_calls = list(self.controller.calls)

        wrong_capture = self._event_params(single)
        wrong_capture["expected_capture_id"] = self.other_capture_id
        with self.assertRaisesRegex(RuntimeError, "different capture identity"):
            self.module._rollback_shader_candidate_override(wrong_capture)
        wrong_session = self._event_params(single)
        wrong_session["expected_capture_session_id"] = "8" * 32
        with self.assertRaisesRegex(RuntimeError, "Capture session changed"):
            self.module._rollback_shader_candidate_override(wrong_session)
        wrong_token = self._event_params("0" * 32)
        with self.assertRaisesRegex(ValueError, "Unknown or expired"):
            self.module._rollback_shader_candidate_override(wrong_token)

        retried = self.module._rollback_shader_candidate_override(
            self._event_params(single)
        )
        self.assertTrue(retried["alreadyRestored"])
        self.assertTrue(retried["idempotentRollback"])
        self.assertFalse(retried["targetFreed"])
        self.assertEqual(retried["clearWarning"], first["clearWarning"])
        self.assertEqual(self.controller.calls, first_calls)

        family = ["2" * 32, "3" * 32]
        self._install_event_family(family, [201, 202], 92, "b" * 64)
        family_params = {
            "override_ids": family,
            "expected_capture_id": self.capture_id,
            "expected_capture_session_id": self.capture_session_id,
        }
        family_first = self.module._rollback_shader_candidate_overrides(
            family_params
        )
        family_calls = list(self.controller.calls)
        family_retry = self.module._rollback_shader_candidate_overrides(
            family_params
        )
        self.assertTrue(family_first["transactional"])
        self.assertFalse(family_first["alreadyRestored"])
        self.assertTrue(family_retry["alreadyRestored"])
        self.assertTrue(family_retry["idempotentRollback"])
        self.assertEqual(
            [item["targetFreed"] for item in family_retry["overrides"]],
            [item["targetFreed"] for item in family_first["overrides"]],
        )
        self.assertTrue(all(
            item["alreadyRestored"] for item in family_retry["overrides"]
        ))
        self.assertEqual(self.controller.calls, family_calls)

        self.module.MAX_EVENT_SHADER_ROLLBACK_TOMBSTONES = 1
        newest = "4" * 32
        self._install_event_family([newest], [301], 93, "e" * 64)
        self.module._rollback_shader_candidate_override(
            self._event_params(newest)
        )
        self.assertEqual(
            list(self.module._event_shader_rollback_tombstones), [newest]
        )
        with self.assertRaisesRegex(ValueError, "Unknown or expired"):
            self.module._rollback_shader_candidate_override(
                self._event_params(family[0])
            )

        self.module._reset_symbol_state(restore_names=False)
        self.assertEqual(self.module._event_shader_rollback_tombstones, {})
        self.assertEqual(self.module._event_shader_rollback_tombstone_order, [])

    def test_shared_retry_preserves_real_free_result_and_is_bounded(self):
        first_token = "5" * 32
        self._install_shared(first_token, 77, 99)
        self.controller.fail_free.add(99)

        wrong_active_capture = self._shared_params(first_token)
        wrong_active_capture["expected_capture_id"] = self.other_capture_id
        with self.assertRaisesRegex(RuntimeError, "different lifecycle or workspace"):
            self.module._rollback_shader_candidate_shared_resource(
                wrong_active_capture
            )
        wrong_active_session = self._shared_params(first_token)
        wrong_active_session["expected_capture_session_id"] = "8" * 32
        with self.assertRaisesRegex(RuntimeError, "Capture session changed"):
            self.module._rollback_shader_candidate_shared_resource(
                wrong_active_session
            )
        self.assertIn(77, self.module._shared_shader_replacements)
        self.assertEqual(self.controller.calls, [])
        self.assertEqual(self.context.calls, [])

        first = self.module._rollback_shader_candidate_shared_resource(
            self._shared_params(first_token)
        )
        self.assertTrue(first["cleared"])
        self.assertFalse(first["targetFreed"])
        self.assertTrue(first["originalShaderPreserved"])
        first_controller_calls = list(self.controller.calls)
        first_context_calls = list(self.context.calls)

        wrong_capture = self._shared_params(first_token)
        wrong_capture["expected_capture_id"] = self.other_capture_id
        with self.assertRaisesRegex(RuntimeError, "different workspace"):
            self.module._rollback_shader_candidate_shared_resource(wrong_capture)
        wrong_session = self._shared_params(first_token)
        wrong_session["expected_capture_session_id"] = "8" * 32
        with self.assertRaisesRegex(RuntimeError, "Capture session changed"):
            self.module._rollback_shader_candidate_shared_resource(wrong_session)
        wrong_workspace = self._shared_params(first_token)
        wrong_workspace["workspace_id"] = "e" * 32
        with self.assertRaisesRegex(RuntimeError, "different workspace"):
            self.module._rollback_shader_candidate_shared_resource(wrong_workspace)
        with self.assertRaisesRegex(ValueError, "Unknown or expired"):
            self.module._rollback_shader_candidate_shared_resource(
                self._shared_params("0" * 32)
            )

        retried = self.module._rollback_shader_candidate_shared_resource(
            self._shared_params(first_token)
        )
        self.assertTrue(retried["alreadyRestored"])
        self.assertTrue(retried["idempotentRollback"])
        self.assertFalse(retried["targetFreed"])
        self.assertEqual(retried["freeWarning"], first["freeWarning"])
        self.assertEqual(self.controller.calls, first_controller_calls)
        self.assertEqual(self.context.calls, first_context_calls)

        self.module.MAX_SHARED_SHADER_ROLLBACK_TOMBSTONES = 1
        newest = "6" * 32
        self._install_shared(newest, 78, 100)
        self.module._rollback_shader_candidate_shared_resource(
            self._shared_params(newest)
        )
        self.assertEqual(
            list(self.module._shared_shader_rollback_tombstones), [newest]
        )
        with self.assertRaisesRegex(ValueError, "Unknown or expired"):
            self.module._rollback_shader_candidate_shared_resource(
                self._shared_params(first_token)
            )
        newest_retry = self.module._rollback_shader_candidate_shared_resource(
            self._shared_params(newest)
        )
        self.assertTrue(newest_retry["alreadyRestored"])

        self.module._reset_symbol_state(restore_names=False)
        self.assertEqual(self.module._shared_shader_rollback_tombstones, {})
        self.assertEqual(self.module._shared_shader_rollback_tombstone_order, [])


if __name__ == "__main__":
    unittest.main()
