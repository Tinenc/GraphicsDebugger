---
name: excel-live-control
description: Inspect and edit a user-selected live Excel workbook on Windows through bundled COM libraries, or work on XLSX files with the portable document tools.
metadata:
  replaylab-version: 1
---

# Live Excel through Bread

Use the shared `bread_execute_code` Python runtime, which includes pywin32 and
comtypes. Attach with `win32com.client.GetActiveObject("Excel.Application")`;
inspect workbook names, full paths and worksheet names before selecting the
user's requested workbook. Do not assume the active workbook is the requested
one, create another Excel process, or close another workbook.

Use Excel Range/Worksheet/Workbook COM APIs for the requested inspection or edit.
Recalculate with Excel when formulas change and inspect representative calculated
values and error cells. Save only the selected document when the task authorizes
saving. Use `bread_windows` screenshots when visual inspection is needed.

Microsoft Excel is the target application and must be running for live control;
the Python/COM client libraries travel with Bread. If the target is an XLSX file
rather than a live workbook, use the local spreadsheet skill and bundled tools.
Script execution follows the existing local execution setting. Dedicated cloud
document connectors are optional separate connections, not a prerequisite for
local Windows workbooks.
