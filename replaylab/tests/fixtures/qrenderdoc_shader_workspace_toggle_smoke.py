"""Runtime smoke test for qrenderdoc's embedded Shader Workspace toggles."""

from __future__ import print_function

import json
import os
import sys

from PySide2 import QtWidgets


def _snapshot(panel):
    return {
        "filesChecked": bool(panel.files_button.isChecked()),
        "filesHidden": bool(panel.file_tree.isHidden()),
        "flowChecked": bool(panel.flow_button.isChecked()),
        "flowHidden": bool(panel.flow_graph.isHidden()),
        "sizes": [int(value) for value in panel.main_splitter.sizes()],
    }


application = QtWidgets.QApplication.instance()
output_path = os.environ.get("REPLAYLAB_TOGGLE_SMOKE_OUTPUT", "")


class _SmokeRunner(object):
    def run(self):
        result = {"schema": "replaylab-shader-workspace-toggle-smoke/v1"}
        try:
            bridge = sys.modules.get("renderdoc_mcp_bridge")
            panel = getattr(bridge, "_shader_workspace_ui", None)
            if panel is None:
                raise RuntimeError("Shader Workspace panel is unavailable")

            before = _snapshot(panel)

            panel.files_button.click()
            application.processEvents()
            files_toggled = _snapshot(panel)

            panel.files_button.click()
            application.processEvents()
            files_restored = _snapshot(panel)

            panel.flow_button.click()
            application.processEvents()
            flow_toggled = _snapshot(panel)

            panel.flow_button.click()
            application.processEvents()
            flow_restored = _snapshot(panel)

            files_visible = (
                files_toggled if files_toggled["filesChecked"]
                else files_restored)
            flow_visible = (
                flow_toggled if flow_toggled["flowChecked"]
                else flow_restored)

            checks = {
                "filesCheckedChanged": (
                    files_toggled["filesChecked"] != before["filesChecked"]),
                "filesVisibilityChanged": (
                    files_toggled["filesHidden"] != before["filesHidden"]),
                "filesVisiblePaneUsable": (
                    files_visible["filesChecked"] and
                    not files_visible["filesHidden"] and
                    files_visible["sizes"][0] > 0),
                "filesRestored": (
                    files_restored["filesChecked"] == before["filesChecked"] and
                    files_restored["filesHidden"] == before["filesHidden"]),
                "flowCheckedChanged": (
                    flow_toggled["flowChecked"] != before["flowChecked"]),
                "flowVisibilityChanged": (
                    flow_toggled["flowHidden"] != before["flowHidden"]),
                "flowVisiblePaneUsable": (
                    flow_visible["flowChecked"] and
                    not flow_visible["flowHidden"] and
                    flow_visible["sizes"][2] > 0),
                "flowRestored": (
                    flow_restored["flowChecked"] == before["flowChecked"] and
                    flow_restored["flowHidden"] == before["flowHidden"]),
            }
            result.update({
                "before": before,
                "filesToggled": files_toggled,
                "filesRestored": files_restored,
                "flowToggled": flow_toggled,
                "flowRestored": flow_restored,
                "checks": checks,
                "ok": all(checks.values()),
            })
        except Exception as exc:
            result.update({"ok": False, "error": "%s: %s" % (
                type(exc).__name__, str(exc))})

        print("REPLAYLAB_TOGGLE_SMOKE=" + json.dumps(
            result, sort_keys=True, separators=(",", ":")))
        if output_path:
            with open(output_path, "w") as output:
                json.dump(result, output, sort_keys=True, separators=(",", ":"))


runner = _SmokeRunner()
bridge = sys.modules.get("renderdoc_mcp_bridge")
if bridge is not None:
    bridge._runtime_toggle_smoke_runner = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.run)
