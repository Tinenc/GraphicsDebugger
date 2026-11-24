from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


REPLAYLAB_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPLAYLAB_ROOT / "renderdoc_mcp_bridge" / "__init__.py"


class _FakeSignal:
    def __init__(self) -> None:
        self.callbacks = []
        self.disconnect_count = 0

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def disconnect(self) -> None:
        self.disconnect_count += 1
        self.callbacks = []

    def emit(self) -> None:
        for callback in list(self.callbacks):
            callback()


class _FakeTimer:
    instances = []

    def __init__(self) -> None:
        self.timeout = _FakeSignal()
        self.single_shot = False
        self.started_ms = None
        self.stop_count = 0
        self.delete_count = 0
        self.__class__.instances.append(self)

    def setSingleShot(self, value) -> None:
        self.single_shot = bool(value)

    def start(self, milliseconds) -> None:
        self.started_ms = int(milliseconds)

    def stop(self) -> None:
        self.stop_count += 1

    def deleteLater(self) -> None:
        self.delete_count += 1


class _FakeApplication:
    def arguments(self):
        return ["qrenderdoc"]


class _FakeQCoreApplication:
    @staticmethod
    def instance():
        return _FakeApplication()

    @staticmethod
    def translate(_context, source):
        return source


class _FakeExtensions:
    def __init__(self) -> None:
        self.window_menus = []
        self.errors = []

    def RegisterWindowMenu(self, base, path, callback) -> None:
        self.window_menus.append((base, path, callback))

    def ErrorDialog(self, message, title) -> None:
        self.errors.append((message, title))


class _FakeMainWindow:
    def Widget(self):
        return self


class _FakeContext:
    def __init__(self) -> None:
        self.extensions = _FakeExtensions()
        self.main_window = _FakeMainWindow()
        self.capture_loaded = False
        self.capture_loading = False

    def Extensions(self):
        return self.extensions

    def GetMainWindow(self):
        return self.main_window

    def IsCaptureLoaded(self) -> bool:
        return self.capture_loaded

    def IsCaptureLoading(self) -> bool:
        return self.capture_loading


class _FakeThumbnailCache:
    def __init__(self) -> None:
        self.clear_count = 0

    def clear(self) -> None:
        self.clear_count += 1


class _FakeExecutor:
    def __init__(self, workers) -> None:
        self.workers = workers
        self.shutdown_count = 0

    def shutdown(self) -> bool:
        self.shutdown_count += 1
        return True


class _FakeLauncher:
    def __init__(self, *arguments) -> None:
        self.arguments = arguments
        self.visible = False
        self.show_count = 0
        self.raise_count = 0
        self.activate_count = 0
        self.close_count = 0
        self.abandon_count = 0

    def isVisible(self) -> bool:
        return self.visible

    def show(self) -> None:
        self.visible = True
        self.show_count += 1

    def raise_(self) -> None:
        self.raise_count += 1

    def activateWindow(self) -> None:
        self.activate_count += 1

    def close(self) -> None:
        self.visible = False
        self.close_count += 1

    def abandon_capture_restore(self) -> bool:
        self.abandon_count += 1
        return True


class ProjectLauncherRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module_name = "renderdoc_mcp_bridge_project_registration_contract"
        cls.previous_renderdoc = sys.modules.get("renderdoc")
        cls.previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
        sys.modules["renderdoc"] = types.ModuleType("renderdoc")
        spec = importlib.util.spec_from_file_location(
            cls.module_name,
            BRIDGE_PATH,
            submodule_search_locations=[str(BRIDGE_PATH.parent)],
        )
        assert spec is not None and spec.loader is not None
        cls.bridge = importlib.util.module_from_spec(spec)
        sys.modules[cls.module_name] = cls.bridge
        spec.loader.exec_module(cls.bridge)

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == cls.module_name or name.startswith(cls.module_name + "."):
                sys.modules.pop(name, None)
        if cls.previous_renderdoc is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = cls.previous_renderdoc
        if cls.previous_qrenderdoc is not None:
            sys.modules["qrenderdoc"] = cls.previous_qrenderdoc

    def setUp(self) -> None:
        _FakeTimer.instances = []
        self.context = _FakeContext()
        self.launchers = []
        self.startup_gate_calls = []
        self.cancel_count = 0

        def _launcher(*arguments):
            value = _FakeLauncher(*arguments)
            self.launchers.append(value)
            return value

        def _startup_allowed(arguments, loaded, loading, ui_available):
            self.startup_gate_calls.append(
                (list(arguments), loaded, loading, ui_available)
            )
            return bool(ui_available and not loaded and not loading)

        def _cancel_auto_activation():
            self.cancel_count += 1

        self.file_menu = object()
        self.bridge.QtCore = types.SimpleNamespace(
            QTimer=_FakeTimer,
            QCoreApplication=_FakeQCoreApplication,
        )
        self.bridge.qrd = types.SimpleNamespace(
            WindowMenu=types.SimpleNamespace(File=self.file_menu)
        )
        self.bridge.rd = object()
        self.bridge.HAS_UI = True
        self.bridge._ctx = self.context
        self.bridge._project_launcher_protocol = types.SimpleNamespace(
            HAS_QT=True,
            ThumbnailCache=_FakeThumbnailCache,
            RenderDocThumbnailReader=lambda _rd: object(),
            AsyncProjectExecutor=_FakeExecutor,
            ReplayLabProjectClient=lambda executable: ("client", executable),
            ReplayLabProjectLauncher=_launcher,
            launcher_startup_allowed=_startup_allowed,
        )
        self.bridge._project_restore_client_protocol = None
        self.bridge._project_launcher_ui = None
        self.bridge._project_launcher_callbacks = []
        self.bridge._project_thumbnail_cache = None
        self.bridge._project_thumbnail_reader = None
        self.bridge._project_async_executor = None
        self.bridge._project_startup_timer = None
        self.bridge._find_replaylab_internal_host = lambda: "ReplayLabInternal.exe"
        self.bridge._cancel_project_auto_activation = _cancel_auto_activation

    def test_file_menu_callback_is_retained_and_idle_timer_opens_launcher(self) -> None:
        self.bridge._register_project_launcher_ui(self.context)

        self.assertEqual(1, len(self.context.extensions.window_menus))
        base, path, menu_callback = self.context.extensions.window_menus[0]
        self.assertIs(self.file_menu, base)
        self.assertEqual(["Projects…"], path)
        self.assertEqual(2, len(self.bridge._project_launcher_callbacks))
        self.assertIs(menu_callback, self.bridge._project_launcher_callbacks[0])

        self.assertEqual(1, len(_FakeTimer.instances))
        timer = _FakeTimer.instances[0]
        self.assertTrue(timer.single_shot)
        self.assertEqual(350, timer.started_ms)
        self.assertIs(timer, self.bridge._project_startup_timer)
        self.assertEqual([], self.launchers)

        timer.timeout.emit()

        self.assertIsNone(self.bridge._project_startup_timer)
        self.assertEqual([([], False, False, True)], self.startup_gate_calls)
        self.assertEqual(1, len(self.launchers))
        self.assertIs(self.launchers[0], self.bridge._project_launcher_ui)
        self.assertEqual(1, self.launchers[0].show_count)
        self.assertEqual(1, self.launchers[0].raise_count)
        self.assertEqual(1, self.launchers[0].activate_count)
        self.assertEqual(1, timer.stop_count)
        self.assertEqual(1, timer.timeout.disconnect_count)
        self.assertEqual(1, timer.delete_count)

    def test_unregister_cancels_pending_startup_timer_and_drops_callbacks(self) -> None:
        self.bridge._register_project_launcher_ui(self.context)
        timer = self.bridge._project_startup_timer
        executor = self.bridge._project_async_executor
        cache = self.bridge._project_thumbnail_cache

        self.bridge._unregister_project_launcher_ui()

        self.assertEqual(1, self.cancel_count)
        self.assertIsNone(self.bridge._project_startup_timer)
        self.assertEqual([], self.bridge._project_launcher_callbacks)
        self.assertIsNone(self.bridge._project_async_executor)
        self.assertIsNone(self.bridge._project_thumbnail_cache)
        self.assertIsNone(self.bridge._project_thumbnail_reader)
        self.assertEqual(1, executor.shutdown_count)
        self.assertEqual(1, cache.clear_count)
        self.assertEqual(1, timer.stop_count)
        self.assertEqual(1, timer.timeout.disconnect_count)
        self.assertEqual(1, timer.delete_count)

        timer.timeout.emit()
        self.assertEqual([], self.launchers)

    def test_file_menu_reuses_a_hidden_launcher_with_an_active_restore(self) -> None:
        self.bridge._register_project_launcher_ui(self.context)
        menu_callback = self.context.extensions.window_menus[0][2]

        menu_callback()
        launcher = self.launchers[0]
        launcher.visible = False
        menu_callback()

        self.assertEqual(1, len(self.launchers))
        self.assertIs(launcher, self.bridge._project_launcher_ui)
        self.assertEqual(2, launcher.show_count)
        self.assertEqual(2, launcher.raise_count)
        self.assertEqual(2, launcher.activate_count)

    def test_capture_change_can_abandon_the_launchers_restore_status(self) -> None:
        self.bridge._register_project_launcher_ui(self.context)
        self.context.extensions.window_menus[0][2]()
        launcher = self.launchers[0]

        self.assertTrue(self.bridge._abandon_project_launcher_restore())
        self.assertEqual(1, launcher.abandon_count)


if __name__ == "__main__":
    unittest.main()
