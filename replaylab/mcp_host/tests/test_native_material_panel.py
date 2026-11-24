from __future__ import annotations

import ast
import base64
import copy
import hashlib
import importlib.util
from pathlib import Path
import struct
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "material_panel.py"
CAPTURE_ID = "c" * 64
CAPTURE_SESSION_ID = "d" * 32


def _load_module():
    spec = importlib.util.spec_from_file_location("replaylab_native_material_panel", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


panel = _load_module()
UI_PATH = MODULE_PATH.with_name("project_material_panel.py")
_ui_spec = importlib.util.spec_from_file_location("replaylab_material_ui", UI_PATH)
panel_ui = importlib.util.module_from_spec(_ui_spec)
_ui_spec.loader.exec_module(panel_ui)


def _leaf(path: str, base_type: str, components: int, offset: int) -> dict:
    identity = f"{path}:{base_type}:{components}:{offset}".encode("utf-8")
    return {
        "leafId": hashlib.sha256(identity).hexdigest()[:32],
        "path": path,
        "displayName": "Real" + path.title(),
        "byteOffset": offset,
        "byteSize": components * 4,
        "baseType": base_type,
        "components": components,
        "editable": True,
    }


def _selection(**changes) -> dict:
    value = {
        "schema": panel.SELECTION_SCHEMA,
        "eventId": 41,
        "stage": "fragment",
        "blockIndex": 0,
        "arrayIndex": 0,
        "physicalSlot": 2,
        "registerSpace": 0,
        "blockName": "CapturedMaterial",
        "entryPoint": "main",
        "shaderResourceId": 101,
        "bufferResourceId": 202,
        "bufferByteOffset": 64,
        "bufferByteSize": 64,
        "fieldName": "roughness",
        "fieldByteOffset": 0,
        "fieldBaseType": "Float",
        "fieldComponents": 1,
    }
    value.update(changes)
    return value


class _Bridge:
    def __init__(self):
        self.current_event = 41
        self.capture_id = CAPTURE_ID
        self.capture_session_id = CAPTURE_SESSION_ID
        self.api = "GraphicsAPI.D3D11"
        self.local = True
        self.snapshot_calls = []
        self.apply_calls = []
        self.rollback_calls = []
        self.current_hash = ""
        self.revision = 0
        self.override_id = ""
        self.fields = [
            _leaf("roughness", "Float", 1, 0),
            _leaf("tint", "Float", 3, 4),
            _leaf("mode", "SInt", 1, 16),
            _leaf("flags", "UInt", 1, 20),
        ]
        self.raw = bytearray(64)
        struct.pack_into("<f", self.raw, 0, 0.5)
        struct.pack_into("<3f", self.raw, 4, 1.0, 0.25, 0.75)
        struct.pack_into("<i", self.raw, 16, -2)
        struct.pack_into("<I", self.raw, 20, 7)
        self.current_hash = hashlib.sha256(self.raw).hexdigest()

    def context(self):
        return {
            "capture": {
                "loaded": True,
                "captureId": self.capture_id,
                "captureSessionId": self.capture_session_id,
                "api": self.api,
                "local": self.local,
            },
            "events": {"currentEventId": self.current_event},
        }

    def snapshot(self, params):
        self.snapshot_calls.append(copy.deepcopy(params))
        return {
            "schema": panel.SNAPSHOT_SCHEMA,
            "snapshotId": "a" * 32,
            "captureId": self.capture_id,
            "captureSessionId": self.capture_session_id,
            "eventId": 41,
            "stage": "fragment",
            "blockIndex": 0,
            "constantBlockPath": "CapturedMaterial",
            "constantBlockDisplayName": "PerMaterial",
            "slot": 2,
            "shaderResourceId": 101,
            "bufferResourceId": 202,
            "byteOffset": 64,
            "byteSize": 64,
            "validByteSize": 64,
            "rawBase64": base64.b64encode(self.raw).decode("ascii"),
            "rawSha256": hashlib.sha256(self.raw).hexdigest(),
            "layoutSha256": "b" * 64,
            "fields": copy.deepcopy(self.fields),
        }

    def apply(self, params):
        self.apply_calls.append(copy.deepcopy(params))
        self.revision = params["new_revision"]
        self.current_hash = params["data_sha256"]
        self.override_id = self.override_id or "e" * 32
        return {
            "schema": panel.OVERRIDE_SCHEMA,
            "snapshotId": params["snapshot_id"],
            "overrideId": self.override_id,
            "revision": self.revision,
            "currentRawSha256": self.current_hash,
            "configured": True,
            "appliedToCurrentEvent": True,
        }

    def rollback(self, params):
        self.rollback_calls.append(copy.deepcopy(params))
        return {
            "schema": panel.OVERRIDE_SCHEMA,
            "snapshotId": params["snapshot_id"],
            "snapshotReleased": True,
            "cleared": bool(params.get("override_id")),
        }


class NativeSelectionTests(unittest.TestCase):
    def test_callback_is_strict_and_resource_ids_are_normalised(self):
        callback = {
            "schema": panel.SELECTION_SCHEMA,
            "eventId": 41,
            "stageName": "Pixel",
            "blockIndex": 0,
            "arrayIndex": 0,
            "physicalSlot": 2,
            "registerSpace": 0,
            "blockName": "CapturedMaterial",
            "entryPoint": "main",
            "shaderResourceId": "shader-wrapper",
            "bufferResourceId": "buffer-wrapper",
            "bufferByteOffset": 64,
            "bufferByteSize": 64,
            "fieldName": "roughness",
            "fieldByteOffset": 0,
            "fieldBaseType": "VarType.Float",
            "fieldComponents": 1,
        }
        resources = {"shader-wrapper": 101, "buffer-wrapper": 202}
        value = panel.native_selection_from_callback(
            callback, resources.get, lambda value: {"Pixel": "fragment"}.get(value, value))
        self.assertEqual(value, _selection())

        native_pairs = list(callback.items())
        self.assertEqual(panel.native_selection_from_callback(
            native_pairs, resources.get, lambda value: "fragment"), value)
        for malformed in (native_pairs + [("eventId", 99)], native_pairs + [("eventId",)],
                          native_pairs + [(3, "not a native string key")]):
            with self.assertRaises(panel.MaterialPanelError):
                panel.native_selection_from_callback(malformed, resources.get, lambda value: "fragment")

        callback["arrayIndex"] = 1
        self.assertEqual(panel.native_selection_from_callback(
            callback, resources.get, lambda value: "fragment")["arrayIndex"], 1)

    def test_python_36_parser_accepts_embedded_panel(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        ast.parse(source, filename=str(MODULE_PATH), feature_version=(3, 6))
        ast.parse(UI_PATH.read_text(encoding="utf-8"), feature_version=(3, 6))


class MaterialPanelControllerTests(unittest.TestCase):
    def test_shared_window_and_leaf_beyond_64k_preserve_neighbor_bytes(self):
        bridge = _Bridge()
        size, base, offset = 128 * 1024, 256 * 1024 + 4, 96 * 1024
        bridge.api = "GraphicsAPI.Vulkan"
        bridge.raw = bytearray(b"\x5a" * size)
        struct.pack_into("<f", bridge.raw, offset, 0.5)
        bridge.fields = [_leaf("roughness", "Float", 1, offset)]
        original_snapshot = bridge.snapshot

        def snapshot(params):
            result = original_snapshot(params)
            result.update(byteOffset=base, byteSize=size, validByteSize=size)
            return result

        model = panel.MaterialPanelController(snapshot, bridge.apply, bridge.rollback, bridge.context)
        state = model.expose(_selection(bufferByteOffset=base, bufferByteSize=size,
                                        fieldByteOffset=offset))
        model.update(state["sessionId"], state["parameters"][0]["parameterId"], 0.25)
        updated = base64.b64decode(bridge.apply_calls[-1]["data_base64"])
        self.assertEqual(updated[:offset], bytes(bridge.raw[:offset]))
        self.assertEqual(updated[offset:offset + 4], struct.pack("<f", 0.25))
        self.assertEqual(updated[offset + 4:], bytes(bridge.raw[offset + 4:]))
        self.assertEqual(state["parameters"][0]["bindingByteOffset"], base)
        self.assertEqual(state["parameters"][0]["byteOffset"], offset)
        with self.assertRaises(panel.MaterialPanelError):
            model.expose(_selection(bufferByteSize=panel.MAX_SHARED_CBUFFER_BYTES + 1))

    def setUp(self):
        self.bridge = _Bridge()
        self.controller = panel.MaterialPanelController(
            self.bridge.snapshot, self.bridge.apply, self.bridge.rollback, self.bridge.context)

    def test_expose_uses_live_snapshot_identity_and_displays_physical_scope(self):
        status = self.controller.expose(_selection())
        self.assertEqual(len(self.bridge.snapshot_calls), 1)
        self.assertEqual(
            self.bridge.snapshot_calls[0],
            {
                "event_id": 41,
                "block_index": 0,
                "array_index": 0,
                "stage": "fragment",
                "expected_capture_id": CAPTURE_ID,
            },
        )
        self.assertEqual(len(status["parameters"]), 1)
        parameter = status["parameters"][0]
        self.assertEqual(parameter["capturedName"], "roughness")
        self.assertEqual(parameter["displayName"], "RealRoughness")
        self.assertEqual(parameter["value"], [0.5])
        self.assertIn("EID 41", parameter["physicalIdentity"])
        self.assertIn("PS binding2 space0", parameter["physicalIdentity"])
        self.assertIn("field +0x0", parameter["physicalIdentity"])
        self.assertIn("buffer +0x40", parameter["physicalIdentity"])

    def test_valid_text_edit_builds_one_allow_list_patch_and_replays(self):
        status = self.controller.expose(_selection())
        parameter_id = status["parameters"][0]["parameterId"]
        updated = self.controller.update_text(status["sessionId"], parameter_id, "0.875")

        self.assertEqual(updated["revision"], 1)
        self.assertEqual(updated["parameters"][0]["value"], [0.875])
        self.assertEqual(len(self.bridge.apply_calls), 1)
        request = self.bridge.apply_calls[0]
        self.assertEqual(request["expected_revision"], 0)
        self.assertEqual(request["new_revision"], 1)
        self.assertEqual(request["patches"][0]["leafId"], parameter_id)
        rebuilt = base64.b64decode(request["data_base64"])
        self.assertEqual(len(rebuilt), 64)
        self.assertAlmostEqual(struct.unpack_from("<f", rebuilt, 0)[0], 0.875)
        self.assertEqual(rebuilt[4:], bytes(self.bridge.raw[4:]))

    def test_multiple_native_exposures_share_one_physical_snapshot(self):
        roughness = self.controller.expose(_selection())
        tint = self.controller.expose(
            _selection(fieldName="tint", fieldByteOffset=4,
                       fieldBaseType="Float", fieldComponents=3))
        self.assertEqual(roughness["sessionId"], tint["sessionId"])
        self.assertEqual(len(self.bridge.snapshot_calls), 1)
        self.assertEqual(len(tint["parameters"]), 2)

    def test_wrong_native_offset_releases_snapshot_without_applying(self):
        with self.assertRaisesRegex(panel.MaterialPanelError, "unique editable leaf"):
            self.controller.expose(_selection(fieldByteOffset=28))
        self.assertEqual(self.bridge.apply_calls, [])
        self.assertEqual(len(self.bridge.rollback_calls), 1)
        self.assertEqual(self.controller.list_status(), [])

    def test_type_guard_and_shared_edit_after_event_navigation(self):
        status = self.controller.expose(_selection())
        parameter_id = status["parameters"][0]["parameterId"]
        with self.assertRaises(panel.MaterialPanelError):
            self.controller.update_text(status["sessionId"], parameter_id, "nan")
        self.bridge.current_event = 42
        result = self.controller.update_text(status["sessionId"], parameter_id, "1")
        self.assertEqual(result["scope"], "shared-buffer-range")
        self.assertEqual(result["parameters"][0]["absoluteByteOffset"], 64)
        self.assertEqual(len(self.bridge.apply_calls), 1)

    def test_rollback_releases_snapshot_and_forgets_parameter(self):
        status = self.controller.expose(_selection())
        parameter_id = status["parameters"][0]["parameterId"]
        self.controller.update(status["sessionId"], parameter_id, 0.25)
        result = self.controller.rollback(status["sessionId"])
        self.assertTrue(result["snapshotReleased"])
        self.assertEqual(self.bridge.rollback_calls[-1]["override_id"], "e" * 32)
        self.assertEqual(self.controller.list_status(), [])

    def test_capture_identity_is_required_before_snapshot(self):
        self.bridge.capture_id = ""
        with self.assertRaises(panel.MaterialPanelError):
            self.controller.expose(_selection())
        self.assertEqual(self.bridge.snapshot_calls, [])

    def test_four_api_and_remote_scope_defer_native_capability_to_setter(self):
        for api in ("D3D11", "D3D12", "Vulkan", "OpenGL"):
            bridge = _Bridge()
            bridge.api = "GraphicsAPI." + api
            bridge.local = False
            controller = panel.MaterialPanelController(
                bridge.snapshot, bridge.apply, bridge.rollback, bridge.context)
            status = controller.expose(_selection())
            self.assertEqual(status["api"], api)
            self.assertEqual(status["scope"], "shared-buffer-range")

    def test_local_d3d12_material_session_is_supported(self):
        self.bridge.api = "GraphicsAPI.D3D12"
        status = self.controller.expose(_selection())
        self.assertEqual(status["api"], "D3D12")
        self.assertEqual(len(self.bridge.snapshot_calls), 1)

    def test_task_and_mesh_native_selections_are_accepted(self):
        for stage_name in ("Task", "Mesh"):
            callback = {
                "schema": panel.SELECTION_SCHEMA,
                "eventId": 41,
                "stageName": stage_name,
                "blockIndex": 0,
                "arrayIndex": 0,
                "physicalSlot": 70000,
                "registerSpace": 3,
                "blockName": "CapturedMaterial",
                "entryPoint": "main",
                "shaderResourceId": 101,
                "bufferResourceId": 202,
                "bufferByteOffset": 256,
                "bufferByteSize": 256,
                "fieldName": "roughness",
                "fieldByteOffset": 0,
                "fieldBaseType": "Float",
                "fieldComponents": 1,
            }
            selection = panel.native_selection_from_callback(
                callback, int, lambda value: value.lower())
            self.assertEqual(selection["stage"], stage_name.lower())
            self.assertEqual(selection["physicalSlot"], 70000)
            self.assertEqual(selection["registerSpace"], 3)

    def test_missing_api_fails_closed_before_snapshot(self):
        self.bridge.api = ""
        with self.assertRaisesRegex(panel.MaterialPanelError, "capture API is unavailable"):
            self.controller.expose(_selection())
        self.assertEqual(self.bridge.snapshot_calls, [])


class _LifecycleController:
    def __init__(self, trace):
        self.trace = trace
        self.rollback_calls = 0
        self.forget_calls = 0
        self.update_calls = 0
        self.active = 1

    def rollback_all(self):
        self.rollback_calls += 1
        self.trace.append("rollback")
        self.active = 0
        return []

    def forget_all(self):
        self.forget_calls += 1
        self.trace.append("forget")

    def list_status(self):
        return [{"sessionId": "a" * 32}] if self.active else []

    def submit_rollback(self, _session_id, complete, _owner):
        self.rollback_all()
        complete({}, None)

    def update_text(self, *_args):
        self.update_calls += 1
        self.trace.append("update")


class _MiniQt:
    def __init__(self, trace):
        self.trace = trace
        self.closed = None
        self.text = {}

    def CreateToplevelWidget(self, _title, callback):
        self.closed = callback
        return object()

    def CreateVerticalContainer(self):
        return object()

    def CreateLabel(self):
        return object()

    def CreateButton(self, _callback):
        return object()

    def AddWidget(self, _parent, _child):
        pass

    def SetWidgetText(self, widget, value):
        self.text[widget] = value

    def SetWidgetEnabled(self, _widget, _enabled):
        pass

    def GetWidgetText(self, widget):
        return self.text.get(widget, "")

    def CloseToplevelWidget(self, widget):
        self.trace.append("close")
        self.closed(None, widget, "")

    def ClearContainedWidgets(self, _widget):
        pass

    def DestroyWidget(self, _widget):
        self.trace.append("destroy")


class _LifecycleContext:
    def __init__(self, helper):
        self.helper = helper

    def Extensions(self):
        return type("Extensions", (), {"GetMiniQtHelper": lambda _self: self.helper})()


class MaterialPanelCloseLifecycleTests(unittest.TestCase):
    def _panel(self):
        trace = []
        helper = _MiniQt(trace)
        controller = _LifecycleController(trace)
        closed = []
        qrd = type(
            "QRD", (),
            {"DockReference": type("DockReference", (), {
                "MainToolArea": 0, "RightOf": 1})})
        qt_core = type("QtCore", (), {"QTimer": object,
            "QCoreApplication": type("Application", (), {
                "translate": staticmethod(lambda _context, source: source)})})
        value = panel_ui.MiniQtMaterialPanel(
            _LifecycleContext(helper), qrd, qt_core, controller,
            lambda: closed.append(True))
        return value, helper, controller, trace, closed

    def test_close_reopen_preserves_session_for_explicit_rollback(self):
        value, helper, controller, _trace, closed = self._panel()
        context = value.ctx
        qrd = value.qrd
        qt_core = value.qt_core
        top = value.top
        helper.closed(None, top, "")
        self.assertEqual(controller.rollback_calls, 0)
        self.assertEqual(controller.forget_calls, 0)
        self.assertEqual(closed, [True])

        value._apply_widget_text("a" * 32, "b" * 32, object())
        value._rollback_all_clicked(None, None, "")
        self.assertEqual(controller.update_calls, 0)
        self.assertEqual(controller.rollback_calls, 0)
        helper.closed(None, top, "")
        self.assertEqual(closed, [True])

        # Registration reuses the persistent controller when it creates the replacement dock.
        reopened = panel_ui.MiniQtMaterialPanel(
            context, qrd, qt_core, controller, None)
        self.assertEqual(len(reopened.controller.list_status()), 1)
        reopened._rollback_all_clicked(None, None, "")
        self.assertEqual(controller.rollback_calls, 1)
        self.assertEqual(controller.active, 0)

    def test_shutdown_rolls_back_before_widget_destruction(self):
        value, _helper, controller, trace, closed = self._panel()
        value.shutdown(rollback=True)
        self.assertEqual(controller.rollback_calls, 1)
        self.assertLess(trace.index("rollback"), trace.index("destroy"))
        self.assertEqual(closed, [True])
        self.assertIsNone(value.top)
        self.assertIsNone(value.mqt)

    def test_shutdown_cancels_owned_debounce_callback(self):
        class _Signal:
            def __init__(self):
                self.callback = None

            def connect(self, callback):
                self.callback = callback

            def disconnect(self):
                self.callback = None

        class _Timer:
            instances = []

            def __init__(self):
                self.timeout = _Signal()
                self.started = 0
                self.stopped = False
                self.deleted = False
                self.__class__.instances.append(self)

            def setSingleShot(self, _value):
                pass

            def start(self, delay):
                self.started = delay

            def stop(self):
                self.stopped = True

            def deleteLater(self):
                self.deleted = True

        value, _helper, _controller, _trace, _closed = self._panel()
        value.qt_core = type("QtCore", (), {"QTimer": _Timer})
        fired = []
        value._schedule_debounced(lambda: fired.append(True), 180)
        timer = _Timer.instances[-1]
        self.assertEqual(timer.started, 180)

        value.shutdown(rollback=False)
        self.assertTrue(timer.stopped)
        self.assertTrue(timer.deleted)
        self.assertIsNone(timer.timeout.callback)
        self.assertEqual(fired, [])


if __name__ == "__main__":
    unittest.main()
