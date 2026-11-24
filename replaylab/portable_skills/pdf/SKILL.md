---
name: pdf
description: Create, inspect, extract and render PDF documents using bundled local libraries.
metadata:
  replaylab-version: 1
---

# Bread pdf

Use bread_document for basic create/inspect. Advanced work uses bundled pypdf, pdfplumber, reportlab and pypdfium2 through bread_execute_code. Use pypdfium2 for page-image verification. Embed a suitable font or use ReportLab UnicodeCIDFont for Chinese text. Extracted text alone does not establish correct visual layout.

The basic document tool remains available when script execution is disabled. If an advanced tool is absent, inspect the current capability directory and the local execution setting; never claim a file or service works from its skill text alone.
