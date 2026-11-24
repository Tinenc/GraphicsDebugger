---
name: computer-use
description: Operate Windows applications through the provider-neutral Bread desktop tools.
metadata:
  replaylab-version: 1
---

# Bread computer-use

Prefer command-line, application APIs and structured MCP tools. When UI control is needed use bread_windows snapshot, screenshot, focus, click and type. Inspect the current screenshot before acting; window IDs, PIDs and physical pixel coordinates come from that snapshot. Include the intended window ID for clicks and text input so those operations can verify the target. A local execution preference is required for input tools. The generic adapter uses Windows APIs and does not depend on a Codex native pipe. Original proprietary plugin manuals are retained as references, but their private APIs require a successful dedicated service connection.

The basic document tool remains available when script execution is disabled. If an advanced tool is absent, inspect the current capability directory and the local execution setting; never claim a file or service works from its skill text alone.
