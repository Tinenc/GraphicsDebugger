from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"


def _load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    fake_renderdoc.ActionFlags = types.SimpleNamespace(
        Clear=0x0001,
        Drawcall=0x0002,
        Dispatch=0x0004,
        MeshDispatch=0x0008,
        Present=0x0100,
        Copy=0x0400,
        Resolve=0x0800,
        GenMips=0x1000,
        DispatchRay=0x4000,
        BuildAccStruct=0x8000,
    )
    previous = sys.modules.get("renderdoc")
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_bridge_event_filters", BRIDGE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous


class _Browser:
    def __init__(self):
        self.registered = []
        self.unregistered = []
        self.empty_regions = []
        self.filter_text = []
        self.current = ""

    def RegisterEventFilterFunction(
        self, name, description, callback, parser, completer
    ):
        self.registered.append(
            (name, description, callback, parser, completer)
        )
        return True

    def UnregisterEventFilterFunction(self, name):
        self.unregistered.append(name)
        return True

    def SetEmptyRegionsVisible(self, visible):
        self.empty_regions.append(bool(visible))

    def SetCurrentFilterText(self, text):
        self.current = text
        self.filter_text.append(text)

    def GetCurrentFilterText(self):
        return self.current


class _Extensions:
    def __init__(self):
        self.items = []

    def RegisterWindowMenu(self, base, path, callback):
        self.items.append(("window", base, tuple(path), callback))

    def RegisterPanelMenu(self, base, path, callback):
        self.items.append(("panel", base, tuple(path), callback))


class _Context:
    def __init__(self):
        self.browser = _Browser()
        self.extensions = _Extensions()
        self.shown = 0

    def GetEventBrowser(self):
        return self.browser

    def Extensions(self):
        return self.extensions

    def ShowEventBrowser(self):
        self.shown += 1


class EventBrowserFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = _load_bridge()

    def setUp(self):
        self.bridge._event_filter_registered = False
        self.bridge._event_filter_ui_callbacks = []
        self.bridge.qrd = types.SimpleNamespace(
            WindowMenu=types.SimpleNamespace(Tools="tools"),
            PanelMenu=types.SimpleNamespace(EventBrowser="events"),
        )

    def _action(self, event_id, flags):
        return types.SimpleNamespace(eventId=event_id, flags=flags)

    def test_draw_filter_matches_only_exact_draw_and_dispatch_action_rows(self):
        match = self.bridge._event_filter_matches
        for flag in (0x0002, 0x0004, 0x0008, 0x4000):
            self.assertTrue(match(None, "rdultra", "draw", 10, None,
                                  self._action(10, flag), ""))

        self.assertFalse(match(None, "rdultra", "draw", 9, None,
                               self._action(10, 0x0002), "state event"))
        self.assertFalse(match(None, "rdultra", "draw", 10, None,
                               self._action(10, 0x0400), "copy"))
        self.assertFalse(match(None, "rdultra", "draw", 10, None, None, ""))

    def test_output_filter_includes_direct_gpu_output_actions(self):
        match = self.bridge._event_filter_matches
        output_flags = (
            0x0001, 0x0002, 0x0004, 0x0008, 0x0100,
            0x0400, 0x0800, 0x1000, 0x4000, 0x8000,
        )
        for flag in output_flags:
            self.assertTrue(match(None, "rdultra", "output", 20, None,
                                  self._action(20, flag), ""))
        self.assertFalse(match(None, "rdultra", "output", 20, None,
                               self._action(20, 0x0040), "marker"))

    def test_parser_is_fail_closed_and_all_matches_every_event(self):
        self.assertEqual(
            self.bridge._event_filter_parse(None, "rdultra", "draw/dispatch"),
            "",
        )
        self.assertNotEqual(
            self.bridge._event_filter_parse(None, "rdultra", "unknown"), ""
        )
        self.assertTrue(
            self.bridge._event_filter_matches(
                None, "rdultra", "all", 1, None, None, "capture start"
            )
        )

    def test_ui_registration_applies_presets_without_replay_calls(self):
        ctx = _Context()
        self.bridge._register_event_browser_filter_ui(ctx)

        self.assertEqual(len(ctx.browser.registered), 1)
        self.assertEqual(ctx.browser.registered[0][0], "rdultra")
        self.assertEqual(len(ctx.extensions.items), 6)

        by_path = {item[2]: item[3] for item in ctx.extensions.items}
        by_path[("Bread AI", "Event Browser Filter", "Draw / Dispatch Only")](
            ctx, None
        )
        self.assertEqual(ctx.browser.empty_regions[-1], False)
        self.assertEqual(ctx.browser.filter_text[-1], "$rdultra(draw)")

        by_path[("Bread AI", "Event Browser Filter", "All")](ctx, None)
        self.assertEqual(ctx.browser.empty_regions[-1], True)
        self.assertEqual(ctx.browser.filter_text[-1], "")
        self.assertEqual(ctx.shown, 2)

    def test_unregister_removes_custom_function_and_invalid_filter_text(self):
        ctx = _Context()
        self.bridge._register_event_browser_filter_ui(ctx)
        ctx.browser.current = "$rdultra(output)"

        self.bridge._unregister_event_browser_filter_ui(ctx)

        self.assertEqual(ctx.browser.unregistered, ["rdultra"])
        self.assertEqual(ctx.browser.current, "")
        self.assertFalse(self.bridge._event_filter_registered)
        self.assertEqual(self.bridge._event_filter_ui_callbacks, [])


if __name__ == "__main__":
    unittest.main()
