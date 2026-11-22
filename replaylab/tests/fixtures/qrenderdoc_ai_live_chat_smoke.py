"""Send one real Bread AI chat from qrenderdoc's composer.

Run with qrenderdoc's ``--ui-python`` option and a capture filename. Set
``BREAD_AI_UI_SMOKE_OUTPUT`` to a system-temp JSON path and optionally set
``BREAD_AI_UI_SMOKE_PROMPT``. For captures with automatic project restoration,
``BREAD_AI_UI_SMOKE_RESTORE_RESULT`` can name that capture's restore-result JSON;
the fixture waits for a finished result bound to this qrenderdoc process before
submitting. Set ``BREAD_AI_UI_SMOKE_PROFILE_ID`` and
``BREAD_AI_UI_SMOKE_NEW_CONVERSATION=1`` to exercise a chosen connection in a
fresh conversation. The marker proves UI submission only; inspect the
new AgentHost task JSONL for tool calls and the terminal result separately.
"""

from __future__ import print_function

import json
import os
import time

from PySide2 import QtCore, QtGui, QtWidgets


class _LiveChatSmoke(object):
    def __init__(self):
        self.app = QtWidgets.QApplication.instance()
        self.output = os.environ.get("BREAD_AI_UI_SMOKE_OUTPUT", "")
        self.prompt = os.environ.get(
            "BREAD_AI_UI_SMOKE_PROMPT",
            "At EID 11384, inspect the bound pixel shader entry point and one "
            "texture. Report observed facts only; do not modify anything.",
        )
        self.restore_result = os.environ.get("BREAD_AI_UI_SMOKE_RESTORE_RESULT", "")
        self.profile_id = os.environ.get("BREAD_AI_UI_SMOKE_PROFILE_ID", "")
        self.new_conversation = os.environ.get("BREAD_AI_UI_SMOKE_NEW_CONVERSATION") == "1"
        self.deadline = time.monotonic() + float(
            os.environ.get("BREAD_AI_UI_SMOKE_TIMEOUT_SECONDS", "180")
        )
        self.phase = "capture"
        self.result = {"schema": "bread-ai-live-chat-smoke/v1", "sent": False}
        self.timer = QtCore.QTimer(self.app)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.tick)

    def finish(self, error=None):
        self.timer.stop()
        self.result["phase"] = self.phase
        if error is not None:
            self.result["error"] = "%s: %s" % (type(error).__name__, error)
            self.result["plainEditors"] = [
                {
                    "placeholder": widget.placeholderText()[:100],
                    "tooltip": widget.toolTip()[:140],
                    "enabled": widget.isEnabled(),
                }
                for widget in self.app.allWidgets()
                if isinstance(widget, QtWidgets.QPlainTextEdit)
            ][:10]
            self.result["skillWidgets"] = [
                {
                    "class": widget.metaObject().className(),
                    "pythonType": type(widget).__name__,
                    "tooltip": widget.toolTip()[:140],
                    "enabled": widget.isEnabled(),
                }
                for widget in self.app.allWidgets()
                if "@skill:" in widget.toolTip()
            ][:10]
        encoded = json.dumps(self.result, sort_keys=True, separators=(",", ":"))
        print("BREAD_AI_UI_SMOKE=" + encoded)
        if self.output:
            with open(self.output, "w") as stream:
                stream.write(encoded)

    def tick(self):
        try:
            if time.monotonic() > self.deadline:
                raise RuntimeError("capture, assistant dock, or composer did not become ready")
            if self.phase == "capture":
                if not pyrenderdoc.IsCaptureLoaded():
                    return
                if self.restore_result:
                    try:
                        with open(self.restore_result, "r") as stream:
                            restore = json.load(stream)
                    except (IOError, ValueError):
                        return
                    if restore.get("status") != "finished":
                        return
                    bindings = [
                        item.get("captureBinding", {}).get("instanceId", "")
                        for item in restore.get("editing", {}).get("items", [])
                    ]
                    if not any(value.startswith(str(os.getpid()) + "-") for value in bindings):
                        return
                    self.result["restoreReady"] = True
                self.phase = "action"
            if self.phase == "action":
                for widget in self.app.topLevelWidgets():
                    action = widget.findChild(QtWidgets.QAction, "action_AI_Assistant")
                    if action is not None:
                        self.main_window = widget
                        self.assistant_action = action
                        break
                if not hasattr(self, "assistant_action"):
                    return
                self.assistant_action.trigger()
                self.phase = "conversation"
                return
            if self.phase == "conversation":
                if self.new_conversation:
                    buttons = self.main_window.findChildren(QtWidgets.QPushButton)
                    button = next((item for item in buttons
                                   if item.accessibleName() in ("New conversation", "新建对话")), None)
                    if button is None:
                        return
                    button.click()
                    self.result["newConversation"] = True
                self.phase = "profile"
                return
            if self.phase == "profile":
                if self.profile_id:
                    combo = next((item for item in self.main_window.findChildren(QtWidgets.QComboBox)
                                  if any(str(item.itemData(index)) == self.profile_id
                                         for index in range(item.count()))), None)
                    if combo is None:
                        return
                    index = next(index for index in range(combo.count())
                                 if str(combo.itemData(index)) == self.profile_id)
                    combo.setCurrentIndex(index)
                    self.result["profileId"] = self.profile_id
                self.phase = "composer"
                return
            editors = [
                widget
                for widget in self.app.allWidgets()
                if "@skill:" in widget.toolTip()
            ]
            if not editors or not editors[0].isEnabled():
                return
            # AIPromptEdit is a C++ subclass without Q_OBJECT, so PySide's
            # allWidgets() exposes it as QWidget. Dispatch normal key events to
            # its actual C++ control without taking ownership of that widget.
            editor = editors[0]
            self.result["editorClass"] = editor.metaObject().className()
            text_event = QtGui.QKeyEvent(
                QtCore.QEvent.KeyPress, QtCore.Qt.Key_unknown,
                QtCore.Qt.NoModifier, self.prompt,
            )
            QtWidgets.QApplication.sendEvent(editor, text_event)
            if editor.property("plainText") != self.prompt:
                raise RuntimeError("composer did not receive the prompt")
            event = QtGui.QKeyEvent(
                QtCore.QEvent.KeyPress, QtCore.Qt.Key_Return, QtCore.Qt.NoModifier
            )
            QtWidgets.QApplication.sendEvent(editor, event)
            if editor.property("plainText"):
                raise RuntimeError("composer did not clear after submission")
            self.result["sent"] = True
            self.result["captureLoaded"] = True
            self.finish()
        except Exception as error:
            self.finish(error)


def _start():
    runner = _LiveChatSmoke()
    # The Python script's globals may be released after startup. Hold the
    # timer/callback on qrenderdoc's long-lived QApplication until it fires.
    runner.app._bread_ai_live_chat_smoke = runner
    runner.timer.start()


pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(_start)
