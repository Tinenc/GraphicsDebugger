"""Bound large textual tool results without losing their evidence.

Vendor clients may spill a large MCP result to a sandbox file that their own file
tool is then forbidden to read. Bread instead retains the exact UTF-8 payload in
its already-authorized evidence directory and gives the model a small pointer it
can page with ``read_evidence_file``. Media blocks remain on their existing path.
"""

from __future__ import annotations

import copy
import codecs
import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping


MAX_INLINE_TOOL_OUTPUT_BYTES = 24 * 1024
DEFAULT_EVIDENCE_PAGE_BYTES = 16 * 1024
MAX_POINTER_BYTES = 2 * 1024
MAX_COMPLETE_SHADER_FILE_BYTES = 4 * 1024 * 1024
MAX_WORKING_FILE_METADATA_BYTES = 16 * 1024
TOOL_OUTPUT_PREFIX = "bread-agent-result-v1-"
TOOL_OUTPUT_TTL_SECONDS = 24 * 60 * 60
TOOL_OUTPUT_MAX_FILES = 64
TOOL_OUTPUT_MAX_TOTAL_BYTES = 128 * 1024 * 1024
OWNED_OUTPUT_READER = "read_evidence_file"


def owned_output_reader_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {
        "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0, "default": 0},
        "max_bytes": {"type": "integer", "minimum": 1, "maximum": 20480},
        "inline": {"type": "boolean", "default": True},
        "text_query": {"type": "string", "maxLength": 256, "default": ""},
    }, "required": ["path"], "additionalProperties": False}


class OwnedOutputStore:
    """Read only exact files retained by this run, never a directory grant."""

    def __init__(self) -> None:
        self.root = _tool_output_root() / ("run-" + uuid.uuid4().hex)
        self._files: dict[str, dict[str, Any]] = {}
        self._aliases: dict[str, str] = {}

    def admit_task_input(self, text: str) -> None:
        from .task_input import TASK_INPUT_URI
        # Admission occurs once before tools start; keep original user text in
        # a private run directory on POSIX as well as the per-user Windows temp.
        self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
        data = text.encode("utf-8")
        path = self.write(data, hashlib.sha256(data).hexdigest(), is_json=False,
                          tool=OWNED_OUTPUT_READER, authority="task-user-input")
        self._aliases[TASK_INPUT_URI] = str(path)

    def write(self, data: bytes, digest: str, *, is_json: bool,
              tool: str, authority: str) -> Path:
        target = _write_guard_output(data, digest, is_json=is_json, root=self.root)
        self._files[str(target)] = {"sha256": digest, "byteSize": len(data),
                                   "tool": str(tool), "authority": authority}
        return target

    def close(self) -> None:
        """Discard only this run's retained transport files after it ends."""
        for value in self._files:
            try:
                Path(value).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            self.root.rmdir()
        except OSError:
            pass
        self._files.clear()
        self._aliases.clear()

    def owns(self, path: str) -> bool:
        return isinstance(path, str) and str(Path(path).absolute()) in self._files

    def read(self, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        """Return a local page, or None for an unrelated Host-owned path."""
        value = arguments.get("path")
        if isinstance(value, str) and value in self._aliases:
            value = self._aliases[value]
        if not isinstance(value, str) or not value or "\0" in value:
            return None
        target = Path(value).absolute()
        record = self._files.get(str(target))
        if record is None:
            # Neither another run's output nor a forged filename gets authority
            # through the Host fallback. A pointer is data, not a filesystem grant.
            try:
                if target.is_relative_to(_tool_output_root().absolute()) or target.resolve().is_relative_to(_tool_output_root().resolve()):
                    raise ValueError("tool result path was not produced by this run")
            except OSError:
                pass
            return None
        for item in (target, *target.parents):
            if item.is_symlink() or item.is_junction():
                raise ValueError("retained tool result must not use a symlink or junction")
        if target.resolve(strict=True) != target or not target.is_file():
            raise ValueError("retained tool result path changed")
        data = target.read_bytes()
        if len(data) != record["byteSize"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("retained tool result content no longer matches its SHA-256")
        offset = arguments.get("offset", 0)
        budget = arguments.get("max_bytes", DEFAULT_EVIDENCE_PAGE_BYTES)
        if budget is None:
            budget = DEFAULT_EVIDENCE_PAGE_BYTES
        inline = arguments.get("inline", True)
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= len(data):
            raise ValueError("offset must be a byte cursor within the retained result")
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 20480:
            raise ValueError("max_bytes must be between 1 and 20480; page with nextOffset")
        if not isinstance(inline, bool):
            raise ValueError("inline must be a boolean")
        if offset < len(data) and 0x80 <= data[offset] <= 0xBF:
            raise ValueError("offset is inside a UTF-8 character; use nextOffset")
        page = {"schema": "replaylab-owned-tool-output/v1", "path": str(target),
                **record, "hostVerified": False, "verification": "localRead",
                "offset": offset, "requestedMaxBytes": budget, "inline": inline}
        if not inline:
            return page
        query = arguments.get("text_query", "")
        if query:
            if not isinstance(query, str) or len(query) > 256 or "\n" in query:
                raise ValueError("text_query must be a single-line string of at most 256 characters")
            # Retained JSON is often one long physical line. Locate individual
            # occurrences, not just its line start, so the caller can read the
            # requested Shader instructions without paging the whole response.
            needle, cursor, matches = query.encode("utf-8"), offset, []
            while len(matches) < 16:
                position = data.find(needle, cursor)
                if position < 0:
                    cursor = len(data)
                    break
                context_start = max(data.rfind(b"\n", 0, position) + 1, position - 80)
                while context_start < position and 0x80 <= data[context_start] <= 0xBF:
                    context_start += 1
                preview = data[context_start:context_start + 512].decode("utf-8", errors="ignore")
                matches.append({"offset": context_start, "matchOffset": position,
                                "preview": preview})
                cursor = position + len(needle)
            return {**page, "textQuery": query, "matches": matches,
                    "truncated": cursor < len(data), "nextOffset": cursor,
                    "continuation": {"nextOffset": cursor, "text_query": query},
                    "replayRequired": False}
        chunk = data[offset:offset + budget]
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        content = decoder.decode(chunk, final=offset + len(chunk) == len(data))
        if not content and chunk:
            raise ValueError("max_bytes is too small for the next UTF-8 character; use at least 4")

        def result(text: str) -> dict[str, Any]:
            consumed = len(text.encode("utf-8"))
            return {**page, "content": text, "returnedBytes": consumed,
                    "truncated": offset + consumed < len(data), "nextOffset": offset + consumed}

        # Escaped JSON and non-ASCII text must stay below the inline transport
        # budget too; the receipt always advances by bytes actually delivered.
        low, high = 0, len(content)
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(result(content[:middle]), ensure_ascii=False).encode("utf-8")) <= MAX_INLINE_TOOL_OUTPUT_BYTES:
                low = middle
            else:
                high = middle - 1
        if content and low == 0:
            raise ValueError("retained result receipt cannot fit the inline transport budget")
        return result(content[:low])

_PROJECTION_FIELDS = ("operationId", "status", "applied", "accepted")

# Host receipts may retain route prose for compatibility with older clients.
# The live Agent boundary carries facts, errors, acceptance criteria and named
# capabilities, but never lets those legacy fields choose the Agent's plan.
_DIRECTIVE_FIELDS = frozenset({
    "allowedActions",
    "autonomousNext",
    "firstSteps",
    "nextRead",
    "nextStep",
    "nextTool",
    "recommendedActions",
    "requiredAction",
    "requiredFirstStep",
    "reviewBatchGuidance",
    "skipAssetStage",
    "sourceRepairTool",
    "stopBroadAssetScan",
    "strategy",
})
_PLANNER_CONTAINERS = frozenset({
    "autonomousNext",
    "direction",
    "directions",
    "guidance",
    "nextRead",
    "nextStep",
    "planner",
    "recommendation",
    "recommendations",
    "recommendedAction",
    "recommendedActions",
    "reviewBatchGuidance",
    "strategy",
    "verdict",
})
_PLANNER_SIBLING_FIELDS = frozenset({
    "direction",
    "example",
    "firstSteps",
    "nextStep",
    "nextTool",
    "recommendedActions",
    "requiredAction",
    "requiredFirstStep",
    "reviewBatchGuidance",
    "strategy",
})
_GRAPH_CONTEXTS = frozenset({
    "algorithmGraph",
    "callGraph",
    "controlFlow",
    "dataFlow",
    "edges",
    "equivalence",
    "equivalenceGraph",
    "flowGraph",
    "flow_graph",
    "graph",
    "nodes",
    "stages",
})
_FACT_CONTEXTS = frozenset({
    "acceptance",
    "availableCapabilities",
    "capabilities",
    "error",
    "errors",
    "evidence",
    "facts",
    "requiredChecks",
    "unmetAcceptance",
})


def sanitize_host_planner_data(value: Any, *, _path: tuple[str, ...] = ()) -> Any:
    """Recursively remove only known Host planner/directive fields.

    ``next`` is intentionally contextual: flow-graph adjacency lists and fields
    such as ``nextOffset`` are evidence, not instructions.  Likewise ``tool`` is
    retained in provenance and errors and removed from planner containers.
    This is defense in depth; first-party Host tools
    should emit neutral facts/acceptance/capability fields themselves.
    """

    if isinstance(value, Mapping):
        keys = {str(key) for key in value}
        planner_context = any(part in _PLANNER_CONTAINERS for part in _path)
        fact_context = any(part in _FACT_CONTEXTS for part in _path)
        has_planner_sibling = bool(keys & _PLANNER_SIBLING_FIELDS)
        cleaned: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _DIRECTIVE_FIELDS:
                continue
            if key == "next":
                graph_adjacency = isinstance(item, (list, tuple)) and (
                    any(part in _GRAPH_CONTEXTS for part in _path)
                    or (
                        "id" in keys
                        and bool(keys & {
                            "function", "label", "reads", "sourceRange", "writes"
                        })
                    )
                )
                if not graph_adjacency:
                    continue
            if key == "direction" and (
                planner_context
                or has_planner_sibling
                or bool(keys & {"code", "message", "severity", "target", "example"})
            ):
                continue
            if (
                key in {"tool", "operation", "decodeTool"}
                and planner_context
                and not fact_context
            ):
                continue
            if key in {"result", "contentText"} and isinstance(item, str):
                cleaned[key] = sanitize_host_tool_output_text(item)
                continue
            cleaned[key] = sanitize_host_planner_data(
                item, _path=(*_path, key)
            )
        return cleaned
    if isinstance(value, list):
        return [sanitize_host_planner_data(item, _path=_path) for item in value]
    if isinstance(value, tuple):
        return [sanitize_host_planner_data(item, _path=_path) for item in value]
    return value


def sanitize_host_tool_output_text(text: str) -> str:
    """Sanitize a complete JSON Host result while leaving ordinary text exact."""

    if not isinstance(text, str):
        text = str(text)
    prefix = "ERROR: " if text.startswith("ERROR: ") else ""
    candidate = text[len(prefix):] if prefix else text
    try:
        decoded = json.loads(candidate)
    except (TypeError, ValueError):
        return text
    sanitized = sanitize_host_planner_data(decoded)
    if sanitized == decoded:
        return text
    return prefix + json.dumps(
        sanitized, ensure_ascii=False, separators=(",", ":"), default=str
    )


def _tool_output_root() -> Path:
    return Path(tempfile.gettempdir()) / "replaylab-tool-output"


def _json_payload(text: str) -> Any:
    candidate = text
    if candidate.startswith("ERROR: "):
        candidate = candidate[len("ERROR: "):]
    try:
        return json.loads(candidate)
    except (TypeError, ValueError):
        return None


def _find_projection(value: Any, field: str, *, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if isinstance(value, Mapping):
        if field in value:
            candidate = value[field]
            if field in {"applied", "accepted"}:
                return candidate if isinstance(candidate, bool) else None
            if isinstance(candidate, (str, int)) and not isinstance(candidate, bool):
                return candidate
        for nested in value.values():
            candidate = _find_projection(nested, field, depth=depth + 1)
            if candidate is not None:
                return candidate
    elif isinstance(value, list):
        for nested in value[:64]:
            candidate = _find_projection(nested, field, depth=depth + 1)
            if candidate is not None:
                return candidate
    return None


def _operation_projection(value: Any, field: str, *, depth: int = 0) -> Any:
    """Project an outcome from the current receipt, never its sample/history rows."""
    if depth > 3 or not isinstance(value, Mapping):
        return None
    if field in value:
        candidate = value[field]
        if field in {"applied", "accepted"}:
            return candidate if type(candidate) is bool else None
        return candidate if isinstance(candidate, str) and len(candidate) <= 256 else None
    # First-party adapters use result for a current operation envelope. A
    # mutation describes that same in-flight/terminal job, unlike arbitrary
    # historical evidence that may contain an unrelated successful operation.
    for key in ("result", "mutation"):
        nested = value.get(key)
        if key == "result" and isinstance(nested, str) and set(value) == {"result"}:
            nested = _json_payload(nested)
        candidate = _operation_projection(nested, field, depth=depth + 1)
        if candidate is not None:
            return candidate
    return None


def _cleanup_guard_outputs(root: Path, *, preserve: Path | None = None) -> None:
    """Bound only files owned by this guard; never touch asset-tool output."""

    try:
        entries = [
            item
            for item in root.glob(TOOL_OUTPUT_PREFIX + "*")
            if item.is_file() and not item.is_symlink()
        ]
    except OSError:
        return
    now = time.time()
    retained: list[tuple[Path, int, float]] = []
    for item in entries:
        try:
            stat = item.stat()
        except OSError:
            continue
        if item != preserve and now - stat.st_mtime > TOOL_OUTPUT_TTL_SECONDS:
            try:
                item.unlink()
            except OSError:
                pass
            continue
        retained.append((item, int(stat.st_size), float(stat.st_mtime)))
    retained.sort(
        key=lambda entry: (entry[0] == preserve, entry[2]),
        reverse=True,
    )
    total = 0
    kept = 0
    for item, size, _modified in retained:
        within_limit = (
            kept < TOOL_OUTPUT_MAX_FILES
            and total + size <= TOOL_OUTPUT_MAX_TOTAL_BYTES
        )
        if item == preserve or within_limit:
            total += size
            kept += 1
            continue
        try:
            item.unlink()
        except OSError:
            pass


def _write_guard_output(data: bytes, digest: str, *, is_json: bool, root: Path | None = None) -> Path:
    root = _tool_output_root() if root is None else root
    root.mkdir(parents=True, exist_ok=True)
    suffix = ".json" if is_json else ".txt"
    target = root / (TOOL_OUTPUT_PREFIX + digest + suffix)
    if target.is_file() and not target.is_symlink():
        try:
            if target.stat().st_size == len(data) and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                try:
                    os.utime(target, None)
                except OSError:
                    pass
                _cleanup_guard_outputs(root, preserve=target)
                return target.resolve()
        except OSError:
            pass
    temporary = root / (
        TOOL_OUTPUT_PREFIX + digest + "." + str(os.getpid()) + "." + uuid.uuid4().hex + ".tmp"
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    _cleanup_guard_outputs(root, preserve=target)
    return target.resolve()


def is_complete_shader_file_output(tool: str, text: str, *,
                                   authority: str = "first-party-host") -> bool:
    """Recognize the explicit, bounded whole-source first-party transport.

    A tool cannot bypass output bounds by declaring itself trusted. Only this
    known tool/schema and an exact full-file byte/hash receipt qualify. This
    predicate is also used at the API framework's automatic spill boundary.
    """
    if authority != "first-party-host" or tool != "read_shader_workspace_file":
        return False
    if not isinstance(text, str) or len(text) > MAX_COMPLETE_SHADER_FILE_BYTES * 6 + MAX_WORKING_FILE_METADATA_BYTES:
        return False
    value = _json_payload(text)
    if not isinstance(value, Mapping) or text.startswith("ERROR: "):
        return False
    if (value.get("schema") != "renderdoc-ai-shader-workspace-file/v1"
            or value.get("mode") != "complete" or value.get("truncated") is not False
            or value.get("applied") is not False or type(value.get("offset")) is not int
            or value["offset"] != 0):
        return False
    source = value.get("text")
    if not isinstance(source, str) or "\0" in source:
        return False
    try:
        raw = source.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    if len(raw) > MAX_COMPLETE_SHADER_FILE_BYTES:
        return False
    if any(type(value.get(key)) is not int or value[key] != expected for key, expected in (
            ("sizeBytes", len(raw)), ("chars", len(source)), ("totalChars", len(source)))):
        return False
    if value.get("sha256") != hashlib.sha256(raw).hexdigest():
        return False
    coverage = value.get("coverage")
    if not isinstance(coverage, Mapping) or dict(coverage) != {
            "byteOffset": 0, "returnedBytes": len(raw), "complete": True}:
        return False
    if type(coverage["byteOffset"]) is not int or type(coverage["returnedBytes"]) is not int or coverage["complete"] is not True:
        return False
    metadata = {key: item for key, item in value.items() if key != "text"}
    return len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) <= MAX_WORKING_FILE_METADATA_BYTES


def _working_file_delivery(value: Any) -> dict[str, Any] | None:
    """Keep an exact working-copy handoff, never source text, beside a spill."""
    if not isinstance(value, Mapping) or value.get("workingCopy") is not True or value.get("replayAuthority") is not False:
        return None
    if type(value.get("sizeBytes")) is not int or not 0 <= value["sizeBytes"] <= MAX_COMPLETE_SHADER_FILE_BYTES:
        return None
    string_keys = ("path", "relativePath", "virtualPath", "sha256", "sourceWorkspacePath",
                   "sourceRelativePath", "sourceSha256", "organizationId", "expectedProposalSha256")
    if any(not isinstance(value.get(key), str) or not value[key] for key in
           ("path", "relativePath", "virtualPath", "sha256")):
        return None
    if any(not isinstance(value[key], str) or len(value[key]) > 4096 for key in string_keys if key in value):
        return None
    result = {key: value[key] for key in (*string_keys, "sizeBytes", "workingCopy", "replayAuthority") if key in value}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_WORKING_FILE_METADATA_BYTES:
        return None
    return result


def _naming_delivery_summary(value: Any, *, budget: int = 4096) -> dict[str, Any] | None:
    """Never hide a pending acceptance obligation merely because its evidence is large."""
    if not isinstance(value, Mapping):
        return None
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= budget:
        return dict(value)
    scalars = ("required", "status", "sourceSha256", "cbufferStatus", "cbufferRequired", "complete",
               "tool", "phase", "nextAction", "reason", "schema",
               "publishedSourceSha256", "candidateRawSha256", "activeDisplayShaderRawSha256",
               "receiptRelativePath", "configured", "assetRootConfigured", "sourceNamesApplied",
               "panelAliasesApplied", "bindingCount", "reviewedBindingCount", "unresolvedLaneCount",
               "missingLaneCount", "unresolvedCount", "missingCount",
               "textureCount", "reviewedTextureCount", "matchedTextureCount", "publishedTextureCount",
               "usedAssetCount", "applied", "reused")
    result = {key: value[key] for key in scalars
              if isinstance(value.get(key), (str, int, bool)) and len(str(value[key])) <= 512}
    result["detailsOmitted"] = True
    # Keep complete all-binding counters/status; examples only locate remaining work. Full
    # evidence remains available at this tool result's exact read_evidence_file pointer.
    for key in ("bindingCoverage", "resourceNamingDelivery", "textureCoverage", "assetUsage"):
        child = _naming_delivery_summary(value.get(key), budget=1024)
        if child is not None:
            result[key] = child
    actions = value.get("nextActions")
    if isinstance(actions, list):
        result["nextActions"] = []
        for action in actions[:4]:
            if isinstance(action, Mapping):
                compact = {key: action[key] for key in ("tool", "phase", "purpose")
                           if isinstance(action.get(key), str) and len(action[key]) <= 256}
                arguments = action.get("arguments")
                if isinstance(arguments, Mapping) and len(json.dumps(arguments).encode("utf-8")) <= 256:
                    compact["arguments"] = dict(arguments)
                result["nextActions"].append(compact)
            elif isinstance(action, str) and len(action) <= 256:
                result["nextActions"].append(action)
    for key in ("counts", "acceptance"):
        child = value.get(key)
        if isinstance(child, Mapping):
            result[key] = {str(name): item for name, item in child.items()
                           if isinstance(item, (int, bool)) and len(str(name)) <= 96}
    for key in ("pendingBindings", "pendingTextures"):
        rows = value.get(key)
        if isinstance(rows, list):
            result[key] = [
                {name: row[name] for name in ("bind", "space", "binding", "registerSpace",
                    "resourceId", "status", "missingLaneCount")
                 if isinstance(row.get(name), (str, int, bool)) and len(str(row[name])) <= 128}
                for row in rows[:8] if isinstance(row, Mapping)]
            result[key + "Count"] = len(rows)
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= budget:
        return result
    # A large nested receipt must not make the entire obligation disappear from
    # the outer transport. Fit formal acceptance facts first, then evidence hints.
    containers = ("resourceNamingDelivery", "bindingCoverage", "textureCoverage", "assetUsage")
    priority = ("required", "status", "sourceSha256", "complete", "cbufferStatus", "cbufferRequired",
                "tool", "phase", "nextAction", "bindingCount", "reviewedBindingCount", "unresolvedCount",
                "missingCount", "missingLaneCount", "unresolvedLaneCount", "sourceNamesApplied",
                "panelAliasesApplied", "assetRootConfigured", *containers, "reason", "receiptRelativePath",
                "nextActions")
    bounded: dict[str, Any] = {"detailsOmitted": True}
    for key in dict.fromkeys((*priority, *result)):
        if key not in result or key == "detailsOmitted":
            continue
        child = result[key]
        if isinstance(child, Mapping) and key in containers:
            child = _naming_delivery_summary(child, budget=max(256, budget // 4))
        bounded[key] = child
        if len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) > budget:
            bounded.pop(key)
    return bounded


def _shader_validation_delivery(value: Mapping[str, Any]) -> dict[str, Any]:
    """Retain bounded failure facts when the full comparison must be paged."""
    result: dict[str, Any] = {}
    delivery = value.get("delivery")
    delivery = delivery if isinstance(delivery, Mapping) else {}
    for field, budget in (("numericRecovery", 4096), ("diagnostic", 2048), ("delivery", 2048)):
        item = value.get(field, delivery.get(field))
        if isinstance(item, Mapping) and len(json.dumps(item, ensure_ascii=False).encode("utf-8")) <= budget:
            result[field] = dict(item)
    semantic = _naming_delivery_summary(value.get("cbufferSemanticDelivery", delivery.get("cbufferSemanticDelivery")))
    if semantic is not None:
        result["cbufferSemanticDelivery"] = semantic
    observation = value.get("observation", delivery.get("observation"))
    if not isinstance(observation, Mapping):
        return result
    retained: dict[str, Any] = {
        key: observation[key] for key in
        ("captureId", "captureSessionId", "eventId", "accepted", "semanticEquivalenceVerified", "sampleCount",
         "diagnosticCode", "candidateDifferenceConclusive", "nextAction",
         "replayMethod", "pairedReplayUnavailable")
        if isinstance(observation.get(key), (str, bool, int))
        and len(str(observation[key])) <= (1024 if key == "pairedReplayUnavailable" else 128)}

    def metrics(source: Any) -> dict[str, Any]:
        if not isinstance(source, Mapping) or not isinstance(source.get("outputs"), list):
            return {}
        outputs = []
        for index, row in enumerate(source["outputs"][:4]):
            if not isinstance(row, Mapping):
                continue
            item = {"outputIndex": index}
            for key in ("accepted", "byteExact", "failedComponents", "comparedComponents",
                        "maximumAbsoluteError", "baselineSha256", "candidateSha256",
                        "mismatchBounds", "worstMismatch"):
                if key in row and len(json.dumps(row[key], ensure_ascii=False).encode("utf-8")) <= 1024:
                    item[key] = row[key]
            outputs.append(item)
        # This is explicitly a diagnostic projection, never complete coverage.
        return {"outputs": outputs, "outputCount": len(source["outputs"]), "partial": True}

    retained["metrics"] = metrics(observation.get("metrics"))
    stability = observation.get("baselineStability")
    if isinstance(stability, Mapping):
        retained["baselineStability"] = {
            key: stability[key] for key in ("checked", "byteExact", "withinTolerance", "stable",
                                            "accepted", "reason", "error")
            if isinstance(stability.get(key), (str, bool)) and len(str(stability[key])) <= 1024}
        if "outputs" in stability:
            retained["baselineStability"].update(metrics(stability))
        elif "metrics" in stability:
            retained["baselineStability"]["metrics"] = metrics(stability["metrics"])
    if len(json.dumps(retained, ensure_ascii=False).encode("utf-8")) <= 8192:
        result["observation"] = retained
    return result


def _cbuffer_publication_delivery(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep display outcomes visible when per-field evidence spills to disk.

    Ordinary row aliases may succeed while semantic fields or their active
    replacement mirror fail. An `applied` flag alone hides that distinction.
    This projection reports receipt facts, not a new completion judgment.
    """
    if value.get("schema") != "replaylab-cbuffer-alias-publication/v1":
        return {}
    result: dict[str, Any] = {"schema": value["schema"]}
    for key in ("applied", "partial", "idempotentReplay"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key in ("operationId", "requestSha256", "semanticSourceSha256", "activeDisplayShaderRawSha256",
                "activeReplacementShaderRawSha256"):
        if isinstance(value.get(key), str) and len(value[key]) <= 128:
            result[key] = value[key]
    counts = value.get("counts")
    if isinstance(counts, Mapping):
        result["counts"] = {key: counts[key] for key in (
            "requestedRows", "writtenRows", "keptUserRows", "rejectedAliases",
            "skippedRows", "unprocessedRows", "requestedBlockAliases", "writtenBlockAliases",
            "keptUserBlockAliases", "requestedSemanticFields", "writtenSemanticFields",
            "keptUserSemanticFields", "requestedDisplaySemanticFields", "writtenDisplaySemanticFields",
            "keptUserDisplaySemanticFields") if type(counts.get(key)) is int and 0 <= counts[key] <= 1_000_000}
    acceptance = value.get("acceptance")
    if isinstance(acceptance, Mapping):
        result["acceptance"] = {key: acceptance[key] for key in (
            "noUnprocessedRows", "noSkippedRows", "requestedBlockAliasesAccountedFor",
            "requestedSemanticFieldsAccountedFor", "currentDisplaySemanticFieldsAccountedFor")
            if type(acceptance.get(key)) is bool}
    coverage = value.get("bindingCoverage")
    if isinstance(coverage, Mapping):
        result["bindingCoverage"] = {key: coverage[key] for key in (
            "schema", "complete", "bindingCount", "reviewedBindingCount", "unresolvedLaneCount")
            if isinstance(coverage.get(key), (str, int, bool))}
        pending = coverage.get("pendingBindings")
        if isinstance(pending, list):
            result["bindingCoverage"]["pendingBindings"] = [
                {key: row[key] for key in ("bind", "space", "missingLaneCount")
                 if type(row.get(key)) is int}
                for row in pending[:128] if isinstance(row, Mapping)]
    binding = value.get("physicalBinding")
    if isinstance(binding, Mapping):
        result["physicalBinding"] = {key: binding[key] for key in (
            "workspaceId", "captureId", "captureSessionId", "eventId", "stage", "entryPoint",
            "shaderResourceId", "shaderRawSha256", "bindingSha256")
            if isinstance(binding.get(key), (str, int)) and len(str(binding[key])) <= 128}
    for key in ("published", "semanticMirrors"):
        rows = value.get(key)
        if not isinstance(rows, list):
            continue
        summaries = []
        for row in rows[:8]:
            if not isinstance(row, Mapping):
                continue
            summaries.append({field: row[field] for field in (
                "bind", "space", "semanticViewWritten", "semanticFieldsWritten", "semanticFieldsKeptUser",
                "semanticBindingBasis")
                if ((isinstance(row.get(field), (bool, int)) and len(str(row[field])) <= 16)
                    or (field == "semanticBindingBasis" and isinstance(row.get(field), str)
                        and len(row[field]) <= 64))})
        result[key] = {"blockCount": len(rows), "blocks": summaries,
                       "detailsOmitted": len(rows) > len(summaries)}
    for diagnostic_field in ("semanticMirrorSkipped", "semanticOriginalUnavailable"):
        skipped = value.get(diagnostic_field)
        if not isinstance(skipped, list):
            continue
        examples = []
        for row in skipped[:4]:
            if not isinstance(row, Mapping):
                continue
            examples.append({key: str(row[key])[:256] if key in ("name", "reason", "basis") else row[key]
                             for key in ("bind", "space", "name", "reason", "basis")
                             if isinstance(row.get(key), (str, int)) and
                             (key in ("name", "reason", "basis") or len(str(row[key])) <= 16)})
        result[diagnostic_field] = {"count": len(skipped), "examples": examples,
                                    "detailsOmitted": bool(skipped)}
    return {"cbufferPublication": result}


def _operation_failure_delivery(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the current failed operation's diagnosis visible beside large evidence.

    A traceback or reflection dump must not hide whether a write finished or
    whether its exact operation ID can be retried. These are receipt facts, not
    permission to retry. Never search history or nested evidence for an outcome.
    """
    if not _tool_payload_failed(value):
        return {}

    def scalar_fields(source: Mapping[str, Any], names: tuple[str, ...],
                      *, text_limit: int = 1024) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in names:
            item = source.get(key)
            if isinstance(item, str) and item:
                result[key] = item[:text_limit]
                if len(item) > text_limit:
                    result["detailsOmitted"] = True
            elif type(item) in (bool, int):
                result[key] = item
        return result

    failure = scalar_fields(value, (
        "ok", "applied", "status", "error", "errorCode", "errorType", "reason",
        "operationId", "requestSha256", "terminal", "sameOperationRetryAllowed",
    ))
    mutation = value.get("mutation")
    if isinstance(mutation, Mapping):
        failure["mutation"] = scalar_fields(mutation, (
            "operationId", "requestSha256", "tool", "status", "phase", "terminal",
            "applied", "idempotentReplay", "sameOperationRetryAllowed", "error",
        ), text_limit=512)
    diagnostic = value.get("diagnostic")
    if isinstance(diagnostic, Mapping):
        failure["diagnostic"] = scalar_fields(diagnostic, (
            "code", "message", "reason", "kind", "retryable", "recoverable",
            "requiredBytes", "maximumBytes", "actualBytes", "limitBytes",
        ), text_limit=512)
    return {"operationFailure": failure}


def guard_tool_output_text(tool: str, text: str, *, store: OwnedOutputStore | None = None,
                           authority: str = "first-party-host", failed: bool = False,
                           allow_complete_shader_inline: bool = True) -> str:
    """Return text unchanged when small, otherwise a bounded evidence pointer."""

    if authority == "first-party-host":
        text = sanitize_host_tool_output_text(text)
    data = text.encode("utf-8")
    if len(data) <= MAX_INLINE_TOOL_OUTPUT_BYTES:
        return text
    if allow_complete_shader_inline and is_complete_shader_file_output(tool, text, authority=authority):
        return text
    digest = hashlib.sha256(data).hexdigest()
    is_error = failed or text.startswith("ERROR: ")
    decoded = _json_payload(text)
    if authority == "first-party-host" and _tool_payload_failed(decoded):
        is_error = True
    target = (store.write(data, digest, is_json=decoded is not None, tool=tool, authority=authority)
              if store is not None else _write_guard_output(data, digest, is_json=decoded is not None))
    envelope: dict[str, Any] = {
        "schema": "replaylab-tool-output-pointer/v1",
        "tool": str(tool or "")[:128],
        "inline": False,
        "isError": is_error,
        "doNotRetry": True,
        "doNotRetryScope": "result-retrieval-only",
        "byteSize": len(data),
        "sha256": digest,
        "authority": authority,
        "path": str(target),
        "readWith": "read_evidence_file",
        "readArguments": {
            "path": str(target),
            "offset": 0,
            "max_bytes": DEFAULT_EVIDENCE_PAGE_BYTES,
        },
        "readInstructions": (
            (
                ("The Host call" if authority == "first-party-host" else "The configured MCP call") + " returned an oversized error. "
                if is_error
                else ("The Host operation" if authority == "first-party-host" else "The configured MCP operation") + " returned an oversized result. "
            )
            + "The call already ran once; do not retry it for more text. "
            "Load read_evidence_file, read this exact path in bounded pages, and advance "
            "offset by each receipt's returnedBytes until truncated=false. "
            "This restriction does not prevent supported operation recovery after diagnosing and fixing the failure."
        ),
    }
    if is_error:
        # This remains visible even when a compact receipt replaces the paging
        # prose below. Output paging must never become a blanket recovery ban.
        envelope["operationRecovery"] = (
            "Supported recovery remains available; retry authority comes from the operation receipt, not this output pointer."
        )
    for field in _PROJECTION_FIELDS:
        projected = _operation_projection(decoded, field)
        if projected is not None:
            envelope[field] = projected
    # Preserve cheap delivery/source choices beside the exact paged result.
    # Otherwise a large reuse catalog hides both the selected path and every
    # existing organized source, causing needless discovery or re-decompilation.
    pointer_limit = MAX_POINTER_BYTES
    if isinstance(decoded, Mapping):
        delivery: dict[str, Any] = {}
        if authority == "first-party-host":
            delivery.update(_operation_failure_delivery(decoded))
            if delivery:
                pointer_limit = MAX_INLINE_TOOL_OUTPUT_BYTES - 1024
        if authority == "first-party-host" and tool == "publish_cbuffer_aliases":
            delivery.update(_cbuffer_publication_delivery(decoded))
            if delivery:
                pointer_limit = MAX_INLINE_TOOL_OUTPUT_BYTES - 1024
        if authority == "first-party-host" and tool in {
                "finish_shader_workspace_organization", "validate_shader_workspace_delivery",
                "diagnose_shader_organization_candidate"}:
            delivery.update(_shader_validation_delivery(decoded))
            if delivery:
                pointer_limit = MAX_INLINE_TOOL_OUTPUT_BYTES - 1024
        if authority == "first-party-host" and tool in {
                "get_shader_workspace_status", "compile_shader_workspace",
                "apply_shader_workspace_shared_resource", "record_shader_resource_naming_review",
                "record_shader_organization_review", "update_shader_semantic_overview"}:
            observed = decoded.get("result", decoded)
            semantic = observed.get("cbufferSemanticDelivery") if isinstance(observed, Mapping) else None
            semantic = _naming_delivery_summary(semantic)
            if semantic is not None:
                delivery["cbufferSemanticDelivery"] = semantic
                pointer_limit = MAX_INLINE_TOOL_OUTPUT_BYTES - 1024
        if authority == "first-party-host" and tool in {"debug_pixel", "debug_vertex"}:
            trace_artifact = decoded.get("traceArtifact")
            if (isinstance(trace_artifact, Mapping)
                    and len(json.dumps(trace_artifact, ensure_ascii=False).encode("utf-8")) <= 6144):
                delivery["traceArtifact"] = dict(trace_artifact)
                pointer_limit = MAX_INLINE_TOOL_OUTPUT_BYTES - 1024
        if authority == "first-party-host" and tool in {
                "read_shader_workspace_file", "begin_shader_workspace_organization"}:
            agent_file = _working_file_delivery(decoded.get("agentFile"))
            if agent_file is not None:
                # The complete working file is already available. Do not hide
                # its import/CAS identity behind pages of the source payload.
                delivery["agentFile"] = agent_file
                pointer_limit = MAX_INLINE_TOOL_OUTPUT_BYTES - 1024
            semantic_delivery = _naming_delivery_summary(decoded.get("cbufferSemanticDelivery"))
            if semantic_delivery is not None:
                delivery["cbufferSemanticDelivery"] = semantic_delivery
                pointer_limit = MAX_INLINE_TOOL_OUTPUT_BYTES - 1024
        for field in ("workspacePath", "workspaceId", "organizationId", "sourceSha256"):
            projected = _find_projection(decoded, field)
            if isinstance(projected, str) and len(projected) <= 1024:
                delivery[field] = projected
        if authority == "first-party-host" and tool in {
                "propose_shader_names_from_asset", "submit_shader_asset_resolution",
                "retrieve_shader_asset_candidates", "begin_shader_workspace_organization"}:
            # A partial numeric match still carries useful material knowledge.
            # Keep its reuse/authority boundaries and saved dictionary location
            # visible when a large candidate catalog spills to disk. Never
            # truncate the dictionary into an apparently complete parameter set.
            for field, budget in (("materialSemanticContextSummary", 4 * 1024),
                                  ("materialSemanticContext", 6 * 1024)):
                value = decoded.get(field)
                if isinstance(value, Mapping) and len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= budget:
                    delivery[field] = dict(value)
                    pointer_limit = MAX_INLINE_TOOL_OUTPUT_BYTES - 1024
            context_path = decoded.get("materialSemanticContextPath")
            if isinstance(context_path, str) and 0 < len(context_path) <= 4096:
                delivery["materialSemanticContextPath"] = context_path
                pointer_limit = MAX_INLINE_TOOL_OUTPUT_BYTES - 1024
        if authority == "first-party-host" and tool in {
                "begin_shader_workspace_organization", "write_shader_workspace_organization_file"}:
            # Keep the current draft's compact review facts beside the write
            # receipt. Hiding them in a spill recreates the extra paging loop
            # and lets a successful save obscure unfinished source work.
            for field in ("sha256", "proposalSourceSha256", "previousProposalSha256"):
                value = decoded.get(field)
                if isinstance(value, str) and len(value) == 64:
                    delivery[field] = value
            edits_applied = decoded.get("textEditsApplied")
            if type(edits_applied) is int and edits_applied >= 0:
                delivery["textEditsApplied"] = edits_applied
            for field in ("sourceReviewObservations", "sourceReviewScope"):
                value = decoded.get(field)
                if isinstance(value, Mapping) and len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= 8 * 1024:
                    delivery[field] = dict(value)
                    pointer_limit = MAX_INLINE_TOOL_OUTPUT_BYTES - 1024
            editing = decoded.get("proposalEditing")
            if isinstance(editing, Mapping) and len(json.dumps(editing, ensure_ascii=False).encode("utf-8")) <= 2 * 1024:
                delivery["proposalEditing"] = dict(editing)
                pointer_limit = MAX_INLINE_TOOL_OUTPUT_BYTES - 1024
            preparation = _naming_delivery_summary(decoded.get("assetNamingPreparation"))
            if tool == "begin_shader_workspace_organization" and preparation is not None:
                delivery["assetNamingPreparation"] = preparation
                pointer_limit = MAX_INLINE_TOOL_OUTPUT_BYTES - 1024
        alternatives = decoded.get("existingSourceWorkspaces")
        if isinstance(alternatives, list):
            delivery["existingSourceWorkspaces"] = [
                {key: entry[key] for key in ("workspacePath", "workspaceId", "sourceSha256")
                 if isinstance(entry.get(key), str) and len(entry[key]) <= 1024}
                for entry in alternatives[:4] if isinstance(entry, Mapping)
            ]
        preview = decoded.get("preview")
        if isinstance(preview, Mapping):
            delivery["preview"] = {key: preview[key] for key in
                ("shown", "readOnly", "replayApplied", "workspaceId", "sourceWorkspaceId", "sourceSha256")
                if isinstance(preview.get(key), (str, bool)) and len(str(preview[key])) <= 1024}
        if delivery:
            envelope.pop("readInstructions", None)
            receipt: dict[str, Any] = {}
            envelope["receipt"] = receipt
            envelope["receiptIsPartial"] = True
            # Source observations must not lose their budget to long working
            # file provenance paths. They already carry the exact source hash.
            prioritized = {key: delivery[key] for key in
                           ("operationFailure", "numericRecovery", "diagnostic", "cbufferSemanticDelivery", "observation", "delivery", "traceArtifact", "cbufferPublication",
                            "sourceReviewObservations", "proposalEditing", "assetNamingPreparation",
                            "materialSemanticContextSummary", "materialSemanticContextPath", "agentFile")
                           if key in delivery}
            prioritized.update(delivery)
            for field, value in prioritized.items():
                if isinstance(value, list):
                    receipt[field] = []
                    for entry in value:
                        receipt[field].append(entry)
                        if len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) > pointer_limit:
                            receipt[field].pop()
                            break
                else:
                    receipt[field] = value
                    if len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) > pointer_limit:
                        if field == "agentFile" and isinstance(value, Mapping):
                            # Preserve all usable paths and import CAS. Origin
                            # provenance remains available in the paged payload.
                            receipt[field] = {key: item for key, item in value.items() if key not in {
                                "sourceWorkspacePath", "sourceRelativePath", "sourceSha256"}}
                            if len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) <= pointer_limit:
                                continue
                        receipt.pop(field)
                        envelope.setdefault("omittedReceiptFields", []).append(field)
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > pointer_limit:
        # Absolute Windows paths are the only unbounded pointer component. The
        # normal envelope is far below 2 KiB; fail closed if an exotic temp path
        # would make the cure large enough to spill again.
        envelope.pop("readInstructions", None)
        envelope["instruction"] = "Page this path with read_evidence_file; do not rerun the operation merely to retrieve its result."
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return encoded


def _structured_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping) and isinstance(value.get("result"), str):
        if len(value) == 1:
            return str(value["result"])
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)


def _tool_payload_failed(value: Any) -> bool:
    """Inspect only the current receipt, never nested evidence or history."""

    if isinstance(value, str):
        if value.startswith("ERROR: "):
            return True
        value = _json_payload(value)
    return isinstance(value, Mapping) and (
        value.get("ok") is False or value.get("isError") is True
        or bool(value.get("error"))
    )


def mcp_result_failed(result: Any, *, business_outcome: bool = True) -> bool:
    """Recognize protocol errors and explicit first-party receipt failures.

    A failed operation is still a completed tool invocation: callers retain its
    diagnostic result for the Agent to repair and continue. This helper neither
    raises nor retries. Only MCP's content/structuredContent envelopes and the
    FastMCP string-return wrapper are unwrapped; domain result/history trees are
    deliberately not traversed.
    """

    def field(value: Any, name: str, fallback: str = "") -> Any:
        if isinstance(value, Mapping):
            return value.get(name, value.get(fallback) if fallback else None)
        return getattr(value, name, getattr(value, fallback, None) if fallback else None)

    if field(result, "isError", "is_error") is True:
        return True
    if not business_outcome:
        return False
    if _tool_payload_failed(result):
        return True
    structured = field(result, "structuredContent", "structured_content")
    if isinstance(structured, Mapping) and set(structured) == {"result"} and isinstance(structured["result"], str):
        structured = structured["result"]
    if _tool_payload_failed(structured):
        return True
    content = field(result, "content")
    if isinstance(content, (list, tuple)):
        # A multi-part result can contain historical/error examples. Only an
        # unambiguous single JSON receipt supplies a business outcome.
        text = [field(block, "text") for block in content if field(block, "type") == "text"]
        if len(text) == 1 and _tool_payload_failed(text[0]):
            return True
    return False


def mcp_result_transport_text(result: Any) -> str:
    """One deduplicated textual representation for an MCP result transport."""

    content_parts = []
    for block in list(getattr(result, "content", ()) or ()):
        text = getattr(block, "text", None)
        if isinstance(text, str):
            content_parts.append(text)
        elif getattr(block, "type", None) == "resource":
            # Embedded text resources are evidence too; binary blobs go only
            # through collect_media/build_tool_result, never into token text.
            resource = getattr(block, "resource", None)
            resource_text = getattr(resource, "text", None)
            if isinstance(resource_text, str):
                dump = getattr(block, "model_dump", None)
                content_parts.append(_structured_text(dump(by_alias=True) if callable(dump) else {
                    "type": "resource", "resource": {key: value for key in
                        ("uri", "text", "mimeType", "_meta")
                        if (value := getattr(resource, key, None)) is not None},
                    **{key: value for key in ("annotations", "_meta")
                       if (value := getattr(block, key, None)) is not None}}))
            elif getattr(resource, "blob", None) is not None:
                # A text-only provider still needs the URI and metadata to
                # locate evidence or request a compatible preview. Do not
                # stringify a potentially huge binary attachment to get them.
                dump = getattr(block, "model_dump", None)
                metadata = dump(by_alias=True, exclude={"resource": {"blob"}}) if callable(dump) else {
                    "type": "resource", "resource": {key: value for key in ("uri", "mimeType", "_meta")
                        if (value := getattr(resource, key, None)) is not None},
                    **{key: value for key in ("annotations", "_meta")
                       if (value := getattr(block, key, None)) is not None}}
                content_parts.append(_structured_text({**metadata, "binaryContentTransport": "separate-media-path"}))
        elif getattr(block, "type", None) == "resource_link":
            dump = getattr(block, "model_dump", None)
            content_parts.append(_structured_text(dump(by_alias=True) if callable(dump) else {
                key: value for key in ("type", "uri", "name", "title", "description", "mimeType",
                                       "size", "icons", "annotations", "_meta")
                if (value := getattr(block, key, None)) is not None}))
        elif getattr(block, "type", None) in {"image", "audio"}:
            metadata = {key: value for key in ("annotations", "meta", "_meta")
                        if (value := getattr(block, key, None)) is not None}
            if metadata:
                dump = getattr(block, "model_dump", None)
                if callable(dump):
                    metadata = dump(by_alias=True, exclude={"data"})
                content_parts.append(_structured_text(metadata))
    content_text = "\n".join(content_parts)
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if (
        content_text
        and isinstance(structured, Mapping)
        and structured.get("result") == content_text
    ):
        extras = {key: value for key, value in structured.items() if key != "result"}
        if not extras:
            return content_text
        return json.dumps(
            {"contentText": content_text, "structuredContent": extras},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    structured_text = _structured_text(structured)
    if not content_text:
        return structured_text
    if not structured_text or structured_text == content_text:
        return content_text
    return json.dumps(
        {
            "contentText": content_text,
            "structuredContent": structured,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _copy_model(value: Any, updates: Mapping[str, Any]) -> Any:
    copier = getattr(value, "model_copy", None)
    if callable(copier):
        return copier(update=dict(updates))
    copier = getattr(value, "copy", None)
    if callable(copier):
        try:
            return copier(update=dict(updates))
        except TypeError:
            pass
    try:
        cloned = copy.copy(value)
        for key, item in updates.items():
            setattr(cloned, key, item)
        return cloned
    except (AttributeError, TypeError):
        return value


def _sanitize_mcp_call_result(result: Any) -> Any:
    """Preserve an MCP result's shape while filtering live planner fields."""

    changed = False
    content = []
    for block in list(getattr(result, "content", ()) or ()):
        original = getattr(block, "text", None)
        if isinstance(original, str):
            sanitized = sanitize_host_tool_output_text(original)
            if sanitized != original:
                block = _copy_model(block, {"text": sanitized})
                changed = True
        content.append(block)

    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    sanitized_structured = sanitize_host_planner_data(structured)
    updates: dict[str, Any] = {}
    if changed:
        updates["content"] = content
    if sanitized_structured != structured:
        updates["structuredContent"] = sanitized_structured
    return _copy_model(result, updates) if updates else result


def guard_mcp_call_result(result: Any, tool: str, *, store: OwnedOutputStore | None = None,
                          authority: str = "first-party-host") -> Any:
    """Replace oversized MCP text/JSON with one pointer while preserving media."""

    if authority == "first-party-host":
        result = _sanitize_mcp_call_result(result)
        if mcp_result_failed(result) and getattr(result, "isError", None) is not True:
            result = _copy_model(result, {"isError": True})
    transport_text = mcp_result_transport_text(result)
    guarded = guard_tool_output_text(tool, transport_text, store=store, authority=authority,
                                    failed=mcp_result_failed(result, business_outcome=authority == "first-party-host"))
    if guarded == transport_text:
        return result
    try:
        from mcp.types import TextContent
    except ImportError:  # pragma: no cover - the proxy cannot run without MCP
        return result
    media = [
        block
        for block in list(getattr(result, "content", ()) or ())
        if not isinstance(getattr(block, "text", None), str)
        and not isinstance(getattr(getattr(block, "resource", None), "text", None), str)
        and getattr(block, "type", None) != "resource_link"
    ]
    updates = {
        "content": [TextContent(type="text", text=guarded), *media],
        "structuredContent": {"result": guarded},
    }
    return _copy_model(result, updates)


__all__ = [
    "OwnedOutputStore",
    "OWNED_OUTPUT_READER",
    "owned_output_reader_schema",
    "MAX_INLINE_TOOL_OUTPUT_BYTES",
    "DEFAULT_EVIDENCE_PAGE_BYTES",
    "MAX_POINTER_BYTES",
    "TOOL_OUTPUT_MAX_FILES",
    "TOOL_OUTPUT_MAX_TOTAL_BYTES",
    "TOOL_OUTPUT_PREFIX",
    "TOOL_OUTPUT_TTL_SECONDS",
    "guard_mcp_call_result",
    "guard_tool_output_text",
    "mcp_result_transport_text",
    "mcp_result_failed",
    "sanitize_host_planner_data",
    "sanitize_host_tool_output_text",
]
