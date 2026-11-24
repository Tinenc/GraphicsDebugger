---
name: presentations
description: Create, edit, inspect and render local PPTX presentations with bundled tools.
metadata:
  replaylab-version: 1
---

# Bread presentations

Use bread_document for simple slides and inspection. Use bread_execute_code with python-pptx for detailed layout, images, tables and charts, or the bundled JavaScript artifact-tool. Use the bundled artifact-tool or an actual configured renderer for previews and inspect the generated images. LibreOffice is intentionally absent; do not call bread_render_office or claim PDF conversion succeeded without an available renderer. Original plugin layout helpers and references remain available in the application-owned plugin directory. Local PPTX work does not require Spaces, workspace-dependency or a Codex-specific canvas.

The basic document tool remains available when script execution is disabled. If an advanced tool is absent, inspect the current capability directory and the local execution setting; never claim a file or service works from its skill text alone.
