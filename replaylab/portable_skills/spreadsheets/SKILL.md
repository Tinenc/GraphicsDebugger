---
name: spreadsheets
description: Create, edit and inspect XLSX spreadsheets with bundled openpyxl, XlsxWriter and artifact-tool.
metadata:
  replaylab-version: 1
---

# Bread spreadsheets

Use bread_document for basic create/inspect. For formulas, styles, charts and detailed edits, use bread_execute_code with bundled openpyxl or XlsxWriter; the JavaScript artifact-tool is available through breadRequire('@oai/artifact-tool'). openpyxl writes formulas but does not calculate them. Recalculate through the bundled artifact-tool or a configured calculation MCP and inspect results before claiming numerical validation. LibreOffice is intentionally absent. Keep original formula cells when editing. Original plugin reference files remain in the bundle.

The basic document tool remains available when script execution is disabled. If an advanced tool is absent, inspect the current capability directory and the local execution setting; never claim a file or service works from its skill text alone.
