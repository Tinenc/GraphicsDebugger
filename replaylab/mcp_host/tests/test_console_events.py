import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from src import console_events, replaylab_host


class ConsoleEventTests(unittest.TestCase):
    def test_host_event_matches_fixed_bridge_jsonl_schema(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}, clear=False
        ):
            emitted = console_events.emit_console_event(
                "project",
                "warning",
                "工程证据不完整",
                phase="tool-failure",
                code="project.incomplete",
                context={"operation": "prepare_active_capture_project", "eventId": 12},
            )

            self.assertTrue(emitted)
            directory = Path(temporary) / "ReplayLab" / "console-events"
            journals = list(directory.glob("*.jsonl"))
            self.assertEqual(1, len(journals))
            self.assertRegex(
                journals[0].name,
                re.compile(r"^events\.host\.\d+\.[0-9a-f]{32}\.\d{4}\.jsonl$"),
            )
            event = json.loads(journals[0].read_text(encoding="utf-8"))
            self.assertEqual("replaylab-console-event/v1", event["schema"])
            self.assertEqual(1, event["schemaVersion"])
            self.assertEqual("host", event["processRole"])
            self.assertEqual("project", event["module"])
            self.assertEqual("warning", event["severity"])
            self.assertEqual("工程证据不完整", event["message"])
            self.assertEqual(
                {
                    "operation": "prepare_active_capture_project",
                    "eventId": "12",
                },
                event["context"],
            )
            self.assertNotIn("sourcePath", event)
            self.assertNotIn("command", event)

    def test_public_producer_rejects_unregistered_data_without_raising(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}, clear=False
        ):
            self.assertFalse(
                console_events.emit_console_event(
                    "untrusted-module", "error", "no journal should be created"
                )
            )
            self.assertFalse(
                console_events.emit_console_event(
                    "shader",
                    "error",
                    "no source attachment",
                    context={"sourcePath": r"C:\arbitrary\shader.hlsl"},
                )
            )
            self.assertFalse(
                console_events.emit_console_event(
                    "shader", "arbitrary-severity", "invalid severity"
                )
            )
            directory = Path(temporary) / "ReplayLab" / "console-events"
            self.assertFalse(directory.exists())

    def test_best_effort_failure_does_not_change_tool_result_or_error(self):
        def compile_shader_workspace(_workspace_path):
            return '{"ok":true}'

        wrapped = console_events.instrument_mcp_tool(compile_shader_workspace)
        with mock.patch.object(
            console_events,
            "_append_event",
            side_effect=PermissionError("simulated journal denial"),
        ):
            self.assertEqual('{"ok":true}', wrapped("ignored"))

        expected = RuntimeError("compiler contract failed")

        def prepare_shader_workspace(_workspace_path):
            raise expected

        wrapped_failure = console_events.instrument_mcp_tool(prepare_shader_workspace)
        with mock.patch.object(
            console_events,
            "_append_event",
            side_effect=PermissionError("simulated journal denial"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                wrapped_failure("ignored")
        self.assertIs(expected, caught.exception)

    def test_fixed_tool_classification_and_high_frequency_suppression(self):
        self.assertEqual(
            "toolchain",
            console_events.console_tool_policy("compile_shader_workspace").module,
        )
        self.assertEqual(
            "project",
            console_events.console_tool_policy("prepare_active_capture_project").module,
        )
        self.assertEqual(
            "pass",
            console_events.console_tool_policy("classify_current_frame_passes").module,
        )
        self.assertEqual(
            "material",
            console_events.console_tool_policy("update_material_parameter").module,
        )
        self.assertEqual(
            "replay",
            console_events.console_tool_policy("scan_asset_symbols").module,
        )
        for operation in (
            "ping",
            "get_current_renderdoc_context",
            "get_shader_workspace_status",
            "poll_shader_workspace_watch",
            "poll_shader_workspace_live_edit",
            "get_material_session_status",
        ):
            self.assertIsNone(console_events.console_tool_policy(operation))
        self.assertIsNone(console_events.console_tool_policy("unknown_future_tool"))

    def test_wrapper_reports_json_error_as_failure_and_keeps_payload(self):
        def compile_shader_workspace(_workspace_path):
            return '{"error":"DXC rejected line 17","type":"RuntimeError"}'

        events = []
        wrapped = console_events.instrument_mcp_tool(compile_shader_workspace)
        with mock.patch.object(
            console_events,
            "emit_console_event",
            side_effect=lambda *args, **kwargs: events.append((args, kwargs)) or True,
        ):
            result = wrapped("ignored")

        self.assertEqual(
            '{"error":"DXC rejected line 17","type":"RuntimeError"}', result
        )
        self.assertEqual(["info", "error"], [event[0][1] for event in events])
        self.assertEqual(["tool.started", "tool.failed"], [
            event[1]["code"] for event in events
        ])
        self.assertEqual(
            "compile_shader_workspace", events[1][1]["context"]["operation"]
        )

    def test_host_lifecycle_events_are_best_effort_and_balanced(self):
        events = []
        with mock.patch.object(
            replaylab_host, "_serve", return_value=0
        ), mock.patch.object(
            replaylab_host,
            "emit_console_event",
            side_effect=lambda *args, **kwargs: events.append((args, kwargs)) or False,
        ):
            self.assertEqual(0, replaylab_host.serve(Path("unused"), 49643))

        self.assertEqual(
            ["host.starting", "host.stopped"],
            [event[1]["code"] for event in events],
        )

        events.clear()
        expected = RuntimeError("startup failed")
        with mock.patch.object(
            replaylab_host, "_serve", side_effect=expected
        ), mock.patch.object(
            replaylab_host,
            "emit_console_event",
            side_effect=lambda *args, **kwargs: events.append((args, kwargs)) or False,
        ):
            with self.assertRaises(RuntimeError) as caught:
                replaylab_host.serve(Path("unused"), 49643)
        self.assertIs(expected, caught.exception)
        self.assertEqual(
            ["host.starting", "host.failed", "host.stopped"],
            [event[1]["code"] for event in events],
        )

    def test_retention_bounds_only_regular_protocol_journals_and_keeps_current(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}, clear=False
        ):
            directory = Path(temporary) / "ReplayLab" / "console-events"
            directory.mkdir(parents=True)
            for index in range(36):
                path = directory / (
                    "events.host.%d.%s.%04d.jsonl"
                    % (10_000 + index, ("%032x" % (index + 1)), 0)
                )
                path.write_bytes((b"x" * 96) + b"\n")
                os.utime(path, (index + 1, index + 1))
            unrelated = directory / "shader-live-edit-security-audit.json"
            unrelated.write_text("must remain", encoding="utf-8")

            with mock.patch.object(
                console_events, "_WRITES_SINCE_RETENTION", 128
            ):
                self.assertTrue(
                    console_events.emit_console_event(
                        "host", "info", "retention check"
                    )
                )

            journals = [
                path
                for path in directory.iterdir()
                if console_events._JOURNAL_FILE_PATTERN.fullmatch(path.name)
            ]
            self.assertLessEqual(
                len(journals), console_events.MAX_RETAINED_JOURNAL_FILES
            )
            current = directory / (
                "events.host.%d.%s.%04d.jsonl"
                % (os.getpid(), console_events._SESSION_ID, console_events._SEGMENT)
            )
            self.assertTrue(current.is_file())
            self.assertEqual("must remain", unrelated.read_text(encoding="utf-8"))

    def test_retention_also_enforces_total_byte_budget(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}, clear=False
        ):
            directory = Path(temporary) / "ReplayLab" / "console-events"
            directory.mkdir(parents=True)
            for index in range(8):
                path = directory / (
                    "events.bridge.%d.%s.%04d.jsonl"
                    % (20_000 + index, ("%032x" % (index + 1)), 0)
                )
                path.write_bytes(b"x" * 256)
                os.utime(path, (index + 1, index + 1))

            with mock.patch.object(
                console_events, "MAX_RETAINED_JOURNAL_BYTES", 900
            ), mock.patch.object(
                console_events, "_WRITES_SINCE_RETENTION", 128
            ):
                self.assertTrue(
                    console_events.emit_console_event(
                        "host", "info", "byte retention check"
                    )
                )
                total = sum(
                    path.stat().st_size
                    for path in directory.iterdir()
                    if console_events._JOURNAL_FILE_PATTERN.fullmatch(path.name)
                )
                self.assertLessEqual(total, 900)


if __name__ == "__main__":
    unittest.main()
