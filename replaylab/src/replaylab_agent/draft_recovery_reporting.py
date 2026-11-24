"""Expose retained task outputs and local Shader draft recovery accurately."""

from __future__ import annotations

from typing import Any, Mapping


def retained_task_outputs(receipt: Any) -> bool:
    return (isinstance(receipt, Mapping)
            and receipt.get("schema") == "replaylab-owned-work-output-retention/v1"
            and receipt.get("sourceRetained") is True)


def recovery_note(receipt: Any, language: str) -> str:
    if not isinstance(receipt, Mapping):
        return ""
    path = str(receipt.get("manifestPath") or receipt.get("sourceDirectory") or "")
    if not path:
        return ""
    if retained_task_outputs(receipt):
        # Successful retention is recorded by emit_draft_recovery and the task
        # inspector; it is not part of the substantive user answer.
        return ""
    if language == "zh_CN":
        return "本地着色器草稿恢复位置：[查看恢复记录](<" + path + ">)。该副本尚不能代表已导入工作区源码或完成回放替换。"
    return ("Local Shader draft recovery: [open recovery record](<" + path
            + ">). This local copy does not establish a Host source import or replay replacement.")


def emit_draft_recovery(reporter: Any, receipt: Any) -> None:
    if not isinstance(receipt, Mapping):
        return
    retained = retained_task_outputs(receipt)
    phase = "task-output-retention" if retained else "shader-draft-recovery"
    reporter.writer.emit("running", summary="Local task outputs retained" if retained else "Local Shader draft recovery recorded", data={
        "phase": phase, "activityId": phase, "activityKind": "localFile",
        "status": "completed" if retained or receipt.get("status") == "recovered" else "failed",
        "lifecycle": "completed" if retained or receipt.get("status") == "recovered" else "failed",
        "activityTitleId": "task.detail.public-summary", "activityTitleArgs": [str(receipt.get("manifestPath") or receipt.get("sourceDirectory") or "")],
        "recovery": dict(receipt), "hostVerified": False, "verification": "localFileObserved",
    })
