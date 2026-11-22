from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "project_pass_panel.py"
CAPTURE_A = "a" * 64
CAPTURE_B = "b" * 64


def _load_module():
    spec = importlib.util.spec_from_file_location("replaylab_pass_panel", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


panel_module = _load_module()


def _proposal():
    return {
        "authority": "AIProvisional",
        "verified": False,
        "applied": False,
        "reviewRequired": True,
        "physicalEvidence": False,
        "proposedCategory": "Post",
        "proposedSubcategory": "ToneMap",
        "displayLabel": "Candidate final tone mapping",
        "rationale": "Asset naming suggests a tone-mapping stage.",
    }


def _classification(capture_id=CAPTURE_A, *, proposal=False, indirect=False):
    event_id = 41
    category = "Post"
    subcategory = "ToneMap"
    group_kind = "indirect-container" if indirect else "marker"
    node_type = "container" if indirect else "markerGroup"
    group_label = "ExecuteIndirect(1)" if indirect else "PostProcess"
    item = {
        "eventId": event_id,
        "name": "ToneMap Draw",
        "markerPath": ["Frame", "PostProcess"],
        "category": category,
        "subcategory": subcategory,
        "confidence": "probable",
        "confidenceScore": 0.91,
        "authority": "FixedProgram",
        "semanticVerified": False,
        "evidence": [
            {
                "source": "markerPath",
                "code": "semantic-token",
                "detail": "markerPath matched tonemap",
                "weight": 0.84,
                "authority": "FixedProgram",
            }
        ],
        "alternatives": [],
        "actionKind": "graphics",
        "indirect": indirect,
        "indirectContainerName": group_label if indirect else "",
        "passFingerprint": "1" * 64,
        "shaders": [
            {
                "stage": "fragment",
                "resourceId": 101,
                "rawSha256": "2" * 64,
                "codeSha256": "",
                "name": "ToneMapPS",
            }
        ],
        "targets": {
            "renderTargets": [
                {"resourceId": 202, "name": "Backbuffer", "format": "RGBA8"}
            ],
            "depthTargets": [],
        },
        "stateSummary": {
            "blendEnabled": False,
            "depthTestEnabled": False,
            "depthWriteEnabled": False,
            "readResourceCount": 3,
            "writeResourceCount": 1,
            "fullscreen": True,
        },
        "actualWorkState": "output-affecting-candidate",
        "participation": {
            "issuedAction": True,
            "nonZeroWorkload": True,
            "outputAffectingCandidate": True,
            "observedContributor": False,
        },
        "limitations": ["Pass semantics are inferred."],
    }
    if proposal:
        item["semanticProposal"] = _proposal()
    return {
        "schema": panel_module.CLASSIFICATION_SCHEMA,
        "schemaVersion": panel_module.CLASSIFICATION_VERSION,
        "captureId": capture_id,
        "generatedAt": "2026-08-22T00:00:00Z",
        "items": [item],
        "containers": [],
        "tree": [
            {
                "id": "a" * 20,
                "nodeType": "category",
                "label": category,
                "count": 1,
                "children": [
                    {
                        "id": "b" * 20,
                        "nodeType": "subcategory",
                        "label": subcategory,
                        "count": 1,
                        "children": [
                            {
                                "id": "c" * 20,
                                "nodeType": node_type,
                                "groupKind": group_kind,
                                "label": group_label,
                                "count": 1,
                                "foldByDefault": indirect,
                                "children": [
                                    {
                                        "id": "d" * 20,
                                        "nodeType": "action",
                                        "eventId": event_id,
                                        "label": "ToneMap Draw",
                                        "confidence": "probable",
                                        "indirect": indirect,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        "omittedEvents": [],
        "summary": {
            "inputCount": 1,
            "actionCount": 1,
            "containerCount": 0,
            "markerContextCount": 0,
            "duplicateCount": 0,
            "categoryCounts": {category: 1},
            "sourceTruncated": False,
            "omittedActionCountLowerBound": 0,
            "truncationReasons": [],
            "effectiveMaxActions": 2500,
        },
        "policy": {
            "classificationAuthority": "FixedProgram",
            "aiCanChangeFixedTree": False,
            "aiProposalAuthority": "AIProvisional",
            "eventIdsAreCaptureLocal": True,
            "resourceIdsAreCaptureLocal": True,
            "projectReuseRequiresExactPassFingerprint": True,
            "executeIndirectParentsAreContainers": True,
            "concreteIndirectChildrenRemainVisible": True,
        },
        "provisionalOverlayCount": 1 if proposal else 0,
    }


class _Widget:
    def __init__(self, kind, callback=None):
        self.kind = kind
        self.callback = callback
        self.text = ""
        self.children = []
        self.destroyed = False
        self.enabled = True
        self.checked = False
        self.properties = {}

    def setProperty(self, name, value):
        self.properties[str(name)] = value
        return True

    def click(self):
        if self.callback:
            return self.callback(None, self, "")
        return None


class _MiniQt:
    def __init__(self):
        self.widgets = []
        self.closed = []

    def _widget(self, kind, callback=None):
        widget = _Widget(kind, callback)
        self.widgets.append(widget)
        return widget

    def CreateToplevelWidget(self, title, callback):
        widget = self._widget("top", callback)
        widget.text = title
        return widget

    def CreateVerticalContainer(self):
        return self._widget("vertical")

    def CreateHorizontalContainer(self):
        return self._widget("horizontal")

    def CreateLabel(self):
        return self._widget("label")

    def CreateButton(self, callback):
        return self._widget("button", callback)

    def CreateGroupBox(self, collapsible):
        widget = self._widget("group")
        widget.collapsible = collapsible
        return widget

    def AddWidget(self, parent, child):
        parent.children.append(child)

    def SetWidgetText(self, widget, text):
        widget.text = str(text)

    def SetWidgetChecked(self, widget, checked):
        widget.checked = bool(checked)

    def GetWidgetText(self, widget):
        return widget.text

    def ClearContainedWidgets(self, parent):
        parent.children = []

    def DestroyWidget(self, widget):
        widget.destroyed = True

    def CloseToplevelWidget(self, widget):
        self.closed.append(widget)


class _Extensions:
    def __init__(self, mini):
        self.mini = mini

    def GetMiniQtHelper(self):
        return self.mini


class _Browser:
    def __init__(self):
        self.widget = _Widget("event-browser")

    def Widget(self):
        return self.widget


class _Context:
    def __init__(self):
        self.mini = _MiniQt()
        self.extensions = _Extensions(self.mini)
        self.browser = _Browser()
        self.docks = []
        self.raises = []

    def Extensions(self):
        return self.extensions

    def HasEventBrowser(self):
        return True

    def GetEventBrowser(self):
        return self.browser

    def AddDockWindow(self, *arguments):
        self.docks.append(arguments)

    def RaiseDockWindow(self, widget):
        self.raises.append(widget)


class _DockReference:
    MainToolArea = "main"
    RightOf = "right"


class _Qrd:
    DockReference = _DockReference


class PassPanelValidationTests(unittest.TestCase):
    def test_module_remains_python_36_parseable(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        ast.parse(source, filename=str(MODULE_PATH), feature_version=(3, 6))

    def test_exact_classification_is_sanitised_to_display_only_fields(self):
        clean = panel_module.validate_classification(_classification())
        self.assertEqual(clean["captureId"], CAPTURE_A)
        self.assertEqual(clean["actionCount"], 1)
        self.assertEqual(clean["nodeCount"], 4)
        self.assertEqual(set(clean["items"][41]), {
            "eventId", "name", "category", "subcategory", "confidence",
            "actualWorkState", "participation", "semanticProposal"
        })
        self.assertNotIn("shaders", clean["items"][41])
        self.assertNotIn("targets", clean["items"][41])

    def test_forbidden_execution_file_and_offset_fields_fail_closed(self):
        for forbidden in ("command", "file", "byteOffset", "workspacePath", "args"):
            with self.subTest(forbidden=forbidden):
                value = _classification()
                value["items"][0]["debugMetadata"] = {forbidden: "untrusted"}
                with self.assertRaisesRegex(
                    panel_module.PassPanelError, "forbidden UI field"
                ):
                    panel_module.validate_classification(value)

    def test_tree_counts_identity_order_and_item_agreement_are_strict(self):
        cases = []
        wrong_count = _classification()
        wrong_count["tree"][0]["count"] = 2
        cases.append(wrong_count)
        duplicate_id = _classification()
        duplicate_id["tree"][0]["children"][0]["id"] = "a" * 20
        cases.append(duplicate_id)
        mismatch = _classification()
        mismatch["items"][0]["category"] = "UI"
        cases.append(mismatch)
        wrong_group = _classification()
        wrong_group["tree"][0]["children"][0]["children"][0]["groupKind"] = "command"
        cases.append(wrong_group)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(panel_module.PassPanelError):
                    panel_module.validate_classification(value)

    def test_provisional_overlay_cannot_claim_verified_or_change_count(self):
        value = _classification(proposal=True)
        clean = panel_module.validate_classification(value)
        self.assertEqual(clean["provisionalOverlayCount"], 1)
        self.assertEqual(
            clean["items"][41]["semanticProposal"]["authority"],
            "AIProvisional",
        )
        value["items"][0]["semanticProposal"]["verified"] = True
        with self.assertRaises(panel_module.PassPanelError):
            panel_module.validate_classification(value)

        wrong_count = _classification(proposal=True)
        wrong_count["provisionalOverlayCount"] = 0
        with self.assertRaises(panel_module.PassPanelError):
            panel_module.validate_classification(wrong_count)

    def test_actual_work_state_must_match_fixed_participation(self):
        value = _classification()
        value["items"][0]["actualWorkState"] = "observed-contributor"
        with self.assertRaisesRegex(
            panel_module.PassPanelError, "disagrees with participation"
        ):
            panel_module.validate_classification(value)

        missing = _classification()
        del missing["items"][0]["participation"]
        with self.assertRaisesRegex(
            panel_module.PassPanelError, "participation must be an object"
        ):
            panel_module.validate_classification(missing)

    def test_node_and_text_limits_are_enforced(self):
        with self.assertRaisesRegex(panel_module.PassPanelError, "node limit"):
            panel_module.validate_classification(_classification(), maximum_nodes=3)
        value = _classification()
        value["items"][0]["name"] = "x" * (panel_module.MAX_LABEL + 1)
        with self.assertRaises(panel_module.PassPanelError):
            panel_module.validate_classification(value)

    def test_dynamic_text_policy_allows_html_and_rationale_lf_but_rejects_controls(self):
        html = _classification(proposal=True)
        html["items"][0]["semanticProposal"]["displayLabel"] = "<b>literal</b>"
        html["items"][0]["semanticProposal"]["rationale"] = "line one\nline two"
        clean = panel_module.validate_classification(html)
        self.assertEqual(
            clean["items"][41]["semanticProposal"]["displayLabel"],
            "<b>literal</b>",
        )
        self.assertEqual(
            clean["items"][41]["semanticProposal"]["rationale"],
            "line one\nline two",
        )

        for payload in ("line one\nline two", "unsafe\u202elabel"):
            with self.subTest(payload=repr(payload)):
                value = _classification(proposal=True)
                value["items"][0]["semanticProposal"]["displayLabel"] = payload
                with self.assertRaises(panel_module.PassPanelError):
                    panel_module.validate_classification(value)


class MiniQtPassPanelTests(unittest.TestCase):
    def setUp(self):
        self.context = _Context()
        self.selected = []
        self.closed_count = 0

        def closed():
            self.closed_count += 1

        self.panel = panel_module.MiniQtPassPanel(
            self.context, _Qrd, self.selected.append, closed
        )

    def test_load_builds_collapsible_hierarchy_and_exact_eid_button(self):
        status = self.panel.load(_classification())
        self.assertEqual(status["actionCount"], 1)
        self.assertEqual(status["categoryCount"], 1)
        self.assertEqual(status["provisionalOverlayCount"], 0)
        groups = [widget for widget in self.context.mini.widgets if widget.kind == "group"]
        self.assertEqual([widget.text for widget in groups[-3:]], [
            "Post (1)", "Tone Map (1)", "PostProcess (1)"
        ])
        self.assertTrue(all(widget.collapsible for widget in groups[-3:]))
        button = self.panel._event_buttons[41]
        self.assertEqual(button.text, "EID 41 · ToneMap Draw")
        button.click()
        self.assertEqual(self.selected, [41])
        self.assertEqual(self.context.mini.GetWidgetText(self.panel.status_label),
                         "Selected EID 41.")
        labels = [
            widget for widget in self.context.mini.widgets
            if widget.kind == "label"
        ]
        self.assertTrue(labels)
        self.assertTrue(all(
            widget.properties.get("textFormat") == panel_module.QtCore.Qt.PlainText
            for widget in labels
        ))

    def test_truncated_source_warning_and_status_preserve_bounded_receipt(self):
        value = _classification()
        value["summary"].update({
            "sourceTruncated": True,
            "omittedActionCountLowerBound": 17,
            "truncationReasons": ["action-limit", "driver-diagnostic"],
            "effectiveMaxActions": 2500,
        })
        status = self.panel.load(value)
        self.assertTrue(status["sourceTruncated"])
        self.assertEqual(status["omittedActionCountLowerBound"], 17)
        self.assertEqual(
            status["truncationReasons"], ["action-limit", "driver-diagnostic"])
        self.assertEqual(status["effectiveMaxActions"], 2500)
        self.assertIn("at least 17 action(s) were omitted", self.panel.warning_label.text)
        self.assertIn("action limit", self.panel.warning_label.text)
        # Unknown backend diagnostics remain raw instead of becoming a
        # translation lookup key.
        self.assertIn("driver-diagnostic", self.panel.warning_label.text)

    def test_html_ai_label_is_plain_text(self):
        value = _classification(proposal=True)
        value["items"][0]["semanticProposal"]["displayLabel"] = "<b>literal</b>"
        self.panel.load(value)
        proposal = next(
            widget for widget in self.context.mini.widgets
            if widget.kind == "label" and "AIProvisional:" in widget.text
        )
        self.assertEqual(proposal.text, "AIProvisional: <b>literal</b>")
        self.assertEqual(
            proposal.properties.get("textFormat"),
            panel_module.QtCore.Qt.PlainText,
        )

    def test_indirect_and_ai_provisional_are_visually_separate(self):
        self.panel.load(_classification(proposal=True, indirect=True))
        texts = [widget.text for widget in self.context.mini.widgets]
        self.assertIn("ExecuteIndirect · ExecuteIndirect(1) (1)", texts)
        self.assertIn("Fixed: Probable", texts)
        self.assertIn("Work: Output-affecting candidate", texts)
        self.assertIn("AIProvisional: Candidate final tone mapping", texts)
        self.assertTrue(self.panel._event_buttons[41].text.startswith("Indirect EID 41"))
        groups = [widget for widget in self.context.mini.widgets if widget.kind == "group"]
        self.assertTrue(groups[-1].checked)

    def test_invalid_reload_does_not_destroy_valid_capture_view(self):
        self.panel.load(_classification())
        button = self.panel._event_buttons[41]
        invalid = _classification()
        invalid["items"][0]["unsafe"] = {"command": "erase"}
        with self.assertRaises(panel_module.PassPanelError):
            self.panel.load(invalid)
        button.click()
        self.assertEqual(self.selected, [41])
        self.assertEqual(self.panel._capture_id, CAPTURE_A)

    def test_capture_change_expires_stale_buttons_and_new_capture_loads(self):
        first = self.panel.load(_classification(CAPTURE_A))
        self.assertFalse(first["captureChanged"])
        stale_button = self.panel._event_buttons[41]

        second = self.panel.load(_classification(CAPTURE_B))
        self.assertTrue(second["captureChanged"])
        current_button = self.panel._event_buttons[41]
        stale_button.click()
        self.assertEqual(self.selected, [])
        current_button.click()
        self.assertEqual(self.selected, [41])

        self.panel.capture_changed()
        current_button.click()
        self.assertEqual(self.selected, [41])
        self.assertIsNone(self.panel._model)
        self.assertEqual(self.panel._capture_id, "")

    def test_show_docks_once_and_shutdown_expires_callbacks(self):
        self.panel.load(_classification())
        button = self.panel._event_buttons[41]
        self.assertTrue(self.panel.show())
        self.assertTrue(self.panel.show())
        self.assertEqual(len(self.context.docks), 1)
        self.assertEqual(len(self.context.raises), 2)
        self.assertEqual(self.context.docks[0][1], _DockReference.RightOf)
        top = self.panel.top
        self.panel.shutdown()
        self.assertEqual(self.context.mini.closed, [top])
        self.assertIsNone(self.panel.top)
        self.assertIsNone(self.panel.mqt)
        self.assertIsNone(self.panel.ctx)
        button.click()
        self.assertEqual(self.selected, [])
        self.panel.shutdown()
        self.assertEqual(self.context.mini.closed, [top])
        with self.assertRaises(panel_module.PassPanelError):
            self.panel.show()

    def test_undocked_shutdown_explicitly_destroys_orphan_widget(self):
        top = self.panel.top
        self.panel.shutdown()
        self.assertTrue(top.destroyed)
        self.assertEqual(self.context.mini.closed, [])
        self.assertIsNone(self.panel.top)
        self.assertEqual(self.closed_count, 1)

    def test_native_destroy_callback_never_touches_deleted_widgets(self):
        self.panel.load(_classification())
        stale_button = self.panel._event_buttons[41]
        native_destroy_callback = self.panel.top.callback

        def deleted_widget_access(*_args, **_kwargs):
            raise AssertionError("MiniQt access during QWidget::destroyed")

        # QWidget::destroyed is emitted while the native object hierarchy is
        # already being torn down. Model that contract by rejecting every UI
        # mutation for the close callback and any late queued callback.
        for name in (
            "ClearContainedWidgets", "DestroyWidget", "CreateLabel",
            "SetWidgetText", "AddWidget",
        ):
            setattr(self.context.mini, name, deleted_widget_access)

        native_destroy_callback(None, self.panel.top, "")
        self.assertTrue(self.panel._shutdown)
        self.assertIsNone(self.panel._model)
        self.assertEqual(self.panel._capture_id, "")
        self.assertEqual(self.panel._event_buttons, {})
        self.assertEqual(self.closed_count, 1)
        self.assertIsNone(self.panel.closed_callback)

        # Late/duplicate native callbacks remain idempotent and cannot touch
        # the deleted status label or content container.
        stale_button.click()
        self.assertEqual(self.selected, [])
        self.assertFalse(self.panel.capture_changed())
        native_destroy_callback(None, self.panel.top, "")
        self.assertEqual(self.closed_count, 1)
        self.panel.shutdown()

    def test_set_event_failure_is_reported_without_another_operation(self):
        context = _Context()
        calls = []

        def fail(event_id):
            calls.append(event_id)
            raise RuntimeError("selection unavailable")

        instance = panel_module.MiniQtPassPanel(context, _Qrd, fail)
        instance.load(_classification())
        instance._event_buttons[41].click()
        self.assertEqual(calls, [41])
        self.assertIn("Could not select EID 41", instance.status_label.text)


if __name__ == "__main__":
    unittest.main()
