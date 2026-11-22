"""Exact, task-scoped long user inputs readable even when native Read is denied."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .conversation import estimate_tokens

TASK_INPUT_URI = "bread-task-input://request"
_MAX_INPUT_BYTES = 32 * 1024 * 1024


def prepare_native_task_input(text: str, route: Any, *, profile: Any, model: str) -> tuple[str, Any]:
    window = profile.model_context_windows.get(model) or profile.options.get("context_window_tokens")
    threshold = min(12_000, max(1024, int(window) // 4)) if window else 12_000
    if route is None or estimate_tokens(text) <= threshold:
        return text, route
    data = text.encode("utf-8")
    if len(data) > _MAX_INPUT_BYTES:
        raise ValueError("Task input exceeds the retained input size bound")
    # Hidden controller input is not a user deliverable or a retention receipt.
    path = route.registry_path.parent / (".task-input-" + uuid.uuid4().hex + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"schema": "bread-task-input/v1", "text": text, "byteSize": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}, stream, ensure_ascii=False)
    projection = (
        "The exact original user input was retained because it is too large for one request. "
        "Read it through Bread read_evidence_file with path=" + TASK_INPUT_URI + ", offset and max_bytes; "
        "text_query can locate relevant regions. The response supplies byte offsets and a SHA-256. "
        "Page through the necessary regions before deciding the task scope; constraints may occur in "
        "the omitted middle. Native filesystem tools or shell access are not required. "
        "Keep findings and large deliverables in the authorized workspace instead of copying the whole "
        "input into subsequent prompts.\n\nPreview only:\n" + text[:1200] + "\n[... archived middle ...]\n" + text[-1200:]
    )
    return projection, replace(route, args=(*route.args, "--task-input-file", str(path)))


def load_task_input(path: Path) -> str:
    details = path.lstat()
    if (not path.is_absolute() or not stat.S_ISREG(details.st_mode)
            or getattr(details, "st_file_attributes", 0) & 0x400 or details.st_size > _MAX_INPUT_BYTES * 2):
        raise ValueError("Task input descriptor is not an admitted regular file")
    with path.open("rb") as stream:
        value = json.loads(stream.read(_MAX_INPUT_BYTES * 2 + 1).decode("utf-8"))
    if (not isinstance(value, dict) or value.get("schema") != "bread-task-input/v1"
            or not isinstance(value.get("text"), str)):
        raise ValueError("Invalid task input descriptor")
    text = value["text"]
    data = text.encode("utf-8")
    if (len(data) > _MAX_INPUT_BYTES or value.get("byteSize") != len(data)
            or value.get("sha256") != hashlib.sha256(data).hexdigest()):
        raise ValueError("Task input descriptor does not match its content receipt")
    return text


def native_task_reference(route: Any) -> str:
    if route is None or "--task-input-file" not in route.args:
        return ""
    return ("\n\nThe exact original task input remains available through Bread read_evidence_file "
            "at path=" + TASK_INPUT_URI + ". Recover needed constraints with bounded pages or text_query, "
            "rather than relying on the preview or a summary alone.")
