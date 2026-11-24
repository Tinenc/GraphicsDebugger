"""Durable task tool receipts shared with the first-party MCP Host.

The Agent owns the completion assessment. This adapter exposes the Host's
tool-fact ledger and structured self-review receipts for freshness checks,
transport recovery and diagnostics; the Host does not score answer quality.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The source checkout has two sibling ``src`` packages. Import the MCP Host's
# canonical ledger before Python can resolve ``src`` from replaylab/src.
mcp_root = Path(__file__).resolve().parents[2] / "mcp_host"
if (mcp_root / "src" / "task_report.py").is_file():
    mcp_root_text = str(mcp_root)
    if not sys.path or sys.path[0] != mcp_root_text:
        sys.path.insert(0, mcp_root_text)

from src.task_report import (  # type: ignore[import-not-found]  # noqa: E402
    TASK_EVENT_ROOT_ENV,
    TASK_ID_ENV,
    TaskReportStore,
    read_task_report_sidecars,
)
from src.task_completion_review import (  # type: ignore[import-not-found]  # noqa: E402
    TASK_REVIEW_TOOL_DESCRIPTION,
    TASK_REVIEW_TOOL_SCHEMA,
)

__all__ = [
    "TASK_EVENT_ROOT_ENV",
    "TASK_ID_ENV",
    "TaskReportStore",
    "read_task_report_sidecars",
    "TASK_REVIEW_TOOL_DESCRIPTION",
    "TASK_REVIEW_TOOL_SCHEMA",
]
