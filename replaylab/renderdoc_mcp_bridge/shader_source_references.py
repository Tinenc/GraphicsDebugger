"""Bounded local editor revisions referenced by chat, never embedded in chat.

Only explicit UI attachments register snapshots. Reading one neither selects an
event nor reads a path, saves a document, compiles, or modifies replay. Retained
old-capture revisions are historical source evidence, not current replay facts.
Python 3.6 compatible for the embedded interpreter.
"""

from collections import OrderedDict
import copy
import hashlib
import json
import threading


class SourceReferences(object):
    def __init__(self, max_bytes=64 * 1024 * 1024, max_entries=128):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    def retain(self, context, facts, source_text):
        """Keep one exact referenced revision; return metadata, never source text."""
        source_sha = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        workspace_id = str(context["shader"]["workspaceFileId"])
        session_id = str(context.get("capture", {}).get("captureSessionId") or "")
        if not session_id or source_sha != facts.get("sourceSha256"):
            raise ValueError("Source reference requires its exact capture session and source SHA-256")
        size = len(source_text.encode("utf-8"))
        if size > self.max_bytes:
            raise ValueError("Source revision exceeds the editor reference cache capacity")
        key = (workspace_id, session_id, source_sha)
        source_context = copy.deepcopy(context)
        source_context.get("origin", {}).pop("range", None)
        source_context.get("capture", {}).pop("eventId", None)
        source_context.get("capture", {}).pop("selectedEventId", None)
        entry = {"text": source_text, "sizeBytes": size, "context": source_context,
                 "sourcePath": str(facts.get("sourcePath") or ""),
                 "workspacePath": str(facts.get("workspacePath") or ""),
                 "savedSourceSha256": str(facts.get("savedSourceSha256") or ""),
                 "unsavedChanges": bool(facts.get("unsavedChanges"))}
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= previous["sizeBytes"]
            self._entries[key] = entry
            self._bytes += size
            while self._bytes > self.max_bytes or len(self._entries) > self.max_entries:
                unused, removed = self._entries.popitem(last=False)
                self._bytes -= removed["sizeBytes"]
        return {"tool": "read_shader_workspace_source", "arguments": {
            "workspace_id": workspace_id, "capture_session_id": session_id,
            "source_sha256": source_sha}}

    def read(self, workspace_id, capture_session_id, source_sha256,
             start_line=1, end_line=0, offset=0, limit=12000,
             current_capture_session=""):
        """Page exact source text within a line range using Unicode character offsets."""
        if any(type(value) is not int for value in (start_line, end_line, offset, limit)):
            raise ValueError("Source range and paging arguments must be integers")
        if start_line < 1 or end_line < 0 or offset < 0 or not 1 <= limit <= 24000:
            raise ValueError("Invalid source range or paging limit (1-24000 characters)")
        key = (str(workspace_id), str(capture_session_id), str(source_sha256))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                raise ValueError("Referenced editor revision is unavailable or expired. Do not substitute "
                                 "a newer buffer or saved file. Reattach the intended revision; a managed "
                                 "saved file is equivalent only after verifying the exact source SHA-256.")
            self._entries.move_to_end(key)
            entry = dict(entry)
        lines = entry["text"].splitlines(True)
        if entry["text"].endswith(("\n", "\r")):
            lines.append("")  # The editor exposes the empty line after a trailing newline.
        line_count = max(1, len(lines))
        last = end_line or line_count
        if not 1 <= start_line <= last <= line_count:
            raise ValueError("Requested lines are outside the referenced source revision")
        window = "".join(lines[start_line - 1:last])
        if offset > len(window):
            raise ValueError("Offset is outside the requested source range")
        chunk = window[offset:offset + limit]
        next_offset = offset + len(chunk) if offset + len(chunk) < len(window) else None
        result = {"schema": "bread-shader-source-reference-read/v1", "workspaceId": key[0],
                "captureSessionId": key[1], "sourceSha256": key[2],
                "context": copy.deepcopy(entry["context"]),
                "savedSourceSha256": entry["savedSourceSha256"],
                "unsavedChanges": entry["unsavedChanges"], "sizeBytes": entry["sizeBytes"],
                "currentCaptureMatches": key[1] == str(current_capture_session),
                "evidenceKind": "retained-editor-source", "liveReplayEvidence": False,
                "totalLines": line_count, "startLine": start_line, "endLine": last,
                "totalCharsInRange": len(window), "offset": offset, "nextOffset": next_offset,
                "text": chunk, "applied": False}
        # The common Agent tool boundary is 24 KiB, and Host serializes Unicode
        # with JSON escapes. Fit the actual wire page (including metadata) so
        # every provider gets the text directly, even for huge Unicode lines.
        low, high = 0, len(chunk)
        while low < high:
            middle = (low + high + 1) // 2
            result["text"] = chunk[:middle]
            if len(json.dumps(result, ensure_ascii=True).encode("utf-8")) <= 22 * 1024:
                low = middle
            else:
                high = middle - 1
        result["text"] = chunk[:low]
        result["nextOffset"] = offset + low if offset + low < len(window) else None
        if chunk and not low:
            raise ValueError("Source reference metadata exceeds the page transport budget")
        return result


REFERENCES = SourceReferences()
