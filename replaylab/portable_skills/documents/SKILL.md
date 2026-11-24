---
name: documents
description: Create, inspect, edit and render local DOCX documents with the bundled Bread tools.
metadata:
  replaylab-version: 1
---

# Bread documents

Use bread_document for basic create/inspect. For styled editing, use bread_execute_code with language=python and python-docx. Original document rendering and OOXML helper scripts are shipped under the documents plugin resources; obtain the bundle paths from bread_portable_capabilities. The standard distribution omits LibreOffice. For previews, use the bundled artifact-tool/HTML browser rendering when it supports the requested format, or a configured rendering MCP. Inspect actual outputs; do not claim Office conversion was performed. Write UTF-8; retain paragraph structure, tables, fonts and links. Do not request Codex workspace-dependency or Spaces services for local files.

The basic document tool remains available when script execution is disabled. If an advanced tool is absent, inspect the current capability directory and the local execution setting; never claim a file or service works from its skill text alone.
