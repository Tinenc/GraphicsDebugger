"""IDE-style workspace for reviewed, organised and shared ReplayLab HLSL.

The panel deliberately has no independent filesystem write authority. Its tree
and documents come from managed source records bound to the current capture.
Every explicit editor save goes to the authenticated singleton Host, which
persists the source and establishes or resumes compilation/replacement. Agent
draft writes remain separate and do not acquire that editor intent implicitly.
There is no direct-disk fallback when that Host is unavailable.

This module remains Python 3.6/PySide2 compatible for qrenderdoc's embedded
interpreter. It is named ``project_*.py`` so all user-visible text is extracted
into qrenderdoc's ordinary Qt translation catalog.
"""

from __future__ import print_function

import hashlib
import copy
import importlib.util
import json
import os
import queue
import re
import threading
import tempfile
import time
import uuid


try:
    from . import shader_console as _shader_console_protocol
    from . import shader_editor_client as _shader_save_client_protocol
    from . import project_shader_flow_graph as _shader_flow_graph
    from . import project_shader_flow_hierarchy as _shader_flow_hierarchy
    from . import project_shader_stage_outline as _shader_stage_outline
    from . import project_shader_outline_anchors as _shader_outline_anchors
    from . import shader_source_references as _shader_source_references
    from . import project_shader_visual_debug as _shader_visual_debug
    from . import project_shader_debug_snapshots as _shader_debug_snapshots
    from . import shader_navigation_history as _shader_navigation_history
except (ImportError, ValueError):
    _navigation_spec = importlib.util.spec_from_file_location('_bread_shader_navigation_history',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shader_navigation_history.py'))
    _shader_navigation_history = importlib.util.module_from_spec(_navigation_spec)
    _navigation_spec.loader.exec_module(_shader_navigation_history)
    _shader_console_spec = importlib.util.spec_from_file_location(
        "_replaylab_shader_workspace_console_protocol",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "shader_console.py"))
    if _shader_console_spec is None or _shader_console_spec.loader is None:
        raise ImportError("Unable to load the Bread Shader Console protocol")
    _shader_console_protocol = importlib.util.module_from_spec(_shader_console_spec)
    _shader_console_spec.loader.exec_module(_shader_console_protocol)
    _shader_client_spec = importlib.util.spec_from_file_location(
        "_replaylab_shader_workspace_save_client",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "shader_editor_client.py"))
    if _shader_client_spec is None or _shader_client_spec.loader is None:
        raise ImportError("Unable to load the Bread Shader save client")
    _shader_save_client_protocol = importlib.util.module_from_spec(
        _shader_client_spec)
    _shader_client_spec.loader.exec_module(_shader_save_client_protocol)
    _shader_flow_graph_spec = importlib.util.spec_from_file_location(
        "_replaylab_shader_workspace_flow_graph",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "project_shader_flow_graph.py"))
    if (_shader_flow_graph_spec is None or
            _shader_flow_graph_spec.loader is None):
        raise ImportError("Unable to load the Bread Shader flow graph")
    _shader_flow_graph = importlib.util.module_from_spec(
        _shader_flow_graph_spec)
    _shader_flow_graph_spec.loader.exec_module(_shader_flow_graph)
    _hierarchy_spec = importlib.util.spec_from_file_location(
        "_replaylab_shader_workspace_flow_hierarchy",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "project_shader_flow_hierarchy.py"))
    _shader_flow_hierarchy = importlib.util.module_from_spec(_hierarchy_spec)
    _hierarchy_spec.loader.exec_module(_shader_flow_hierarchy)
    _outline_spec = importlib.util.spec_from_file_location(
        "_replaylab_shader_stage_outline",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "project_shader_stage_outline.py"))
    _shader_stage_outline = importlib.util.module_from_spec(_outline_spec)
    _outline_spec.loader.exec_module(_shader_stage_outline)
    _outline_anchors_spec = importlib.util.spec_from_file_location(
        "_bread_shader_outline_anchors",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "project_shader_outline_anchors.py"))
    _shader_outline_anchors = importlib.util.module_from_spec(_outline_anchors_spec)
    _outline_anchors_spec.loader.exec_module(_shader_outline_anchors)
    _references_spec = importlib.util.spec_from_file_location(
        "_replaylab_shader_source_references",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "shader_source_references.py"))
    _shader_source_references = importlib.util.module_from_spec(_references_spec)
    _references_spec.loader.exec_module(_shader_source_references)
    _visual_debug_spec = importlib.util.spec_from_file_location(
        "_bread_shader_visual_debug", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "project_shader_visual_debug.py"))
    _shader_visual_debug = importlib.util.module_from_spec(_visual_debug_spec)
    _visual_debug_spec.loader.exec_module(_shader_visual_debug)
    _debug_snapshots_spec = importlib.util.spec_from_file_location(
        "_bread_shader_debug_snapshots", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "project_shader_debug_snapshots.py"))
    _shader_debug_snapshots = importlib.util.module_from_spec(_debug_snapshots_spec)
    _debug_snapshots_spec.loader.exec_module(_shader_debug_snapshots)


try:
    from PySide2 import QtCore, QtGui, QtWidgets
    HAS_QT = True
except ImportError:  # pragma: no cover - contract tests use the non-Qt surface
    QtCore = None
    QtGui = None
    QtWidgets = None
    HAS_QT = False


# qrenderdoc creates QApplication in C++. PySide's QWidget.style() parents its
# QStyle wrapper to a temporary application wrapper unless Python retains it.
# Keep that wrapper alive beyond individual panels; Qt still owns the native app.
_qt_application_keepalive = None


POLL_INTERVAL_MS = 2000
SNAPSHOT_ERROR_RETRY_MIN_SECONDS = 30.0
SNAPSHOT_ERROR_RETRY_MAX_SECONDS = 300.0
FILESYSTEM_COALESCE_MS = 40
SAVE_RESULT_POLL_MS = 30
# Live compile-check fires only after editing pauses for this long, so a
# continuous typist triggers at most one background compile per pause.
LIVE_CHECK_DEBOUNCE_MS = 600
# The instant local analysis (no compiler) runs shortly after each pause.
LOCAL_ANALYSIS_DEBOUNCE_MS = 150
SAVE_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 10.0
MAX_OPEN_DOCUMENTS = 32
MAX_ORGANIZATION_PREVIEWS = 8
SETTINGS_KEY = "ReplayLab/ShaderWorkspace/state-v1"
TOKEN_RE = re.compile(r"^[0-9a-f]{32,64}$")

STAGE_ABBREVIATIONS = {
    "vertex": "VS",
    "hull": "HS",
    "tess_control": "HS",
    "domain": "DS",
    "tess_eval": "DS",
    "geometry": "GS",
    "fragment": "PS",
    "pixel": "PS",
    "compute": "CS",
    "task": "TS",
    "amplification": "AS",
    "mesh": "MS",
}


def _selection_attachment(document, text, source_path, start_line, end_line,
                          current_event=0, selected_event=0, known_events=(),
                          selection_range=None, source_text=""):
    """Build bounded, snapshot-bound reference data without replay calls or disk reads."""
    workspace = document.get("workspace", {})
    binding = document.get("captureBinding") or {}
    workspace_id = str(document.get("workspaceId") or workspace.get("workspaceId") or "")
    source_name = os.path.basename(source_path)
    if not TOKEN_RE.fullmatch(workspace_id):
        workspace_id = hashlib.sha256(("shader-workspace\n" + source_path).encode("utf-8")).hexdigest()
    events = sorted(set(int(value) for value in known_events if int(value) > 0))
    event = current_event if current_event in events else (events[0] if events else 0)
    source_range = {"lineStart": int(start_line), "lineEnd": int(end_line)}
    if isinstance(selection_range, dict):
        for key in ("columnStart", "columnEnd"):
            if type(selection_range.get(key)) is int and selection_range[key] > 0:
                source_range[key] = selection_range[key]
    capture = {"eventId": event, "selectedEventId": int(selected_event)}
    for field, value in (("captureSessionId", binding.get("captureSessionId")),
                         ("captureId", workspace.get("captureId"))):
        if value:
            capture[field] = str(value)
    shader = dict((key, value) for key, value in {
        "stage": str(workspace.get("stage") or ""),
        "entryPoint": str(document.get("entryPoint") or workspace.get("entryPoint") or ""),
        # rawSha256 of an opened document is a SOURCE digest, not captured Shader bytecode.
        "rawSha256": str(workspace.get("shaderRawSha256") or ""),
        "workspaceFileId": workspace_id,
        "resourceId": str(workspace.get("resourceId") or ""),
    }.items() if value)
    context = {
        "schema": "replaylab-attachment-context/v1",
        "origin": {"kind": "shader-workspace", "panel": "shader-workspace",
                   "sourceId": workspace_id, "sourceName": source_name, "range": source_range},
        "capture": capture, "shader": shader,
    }
    if shader.get("resourceId"):
        context["relations"] = [{"id": "workspace-" + workspace_id,
                                 "kind": "workspace-copy-of",
                                 "targetId": "shader:" + shader["resourceId"], "usage": "source"}]
    # The attachment is only a locator. Even an explicit code selection does
    # not copy code into chat; the Agent reads the exact local revision on demand.
    source_sha = hashlib.sha256(source_text.encode("utf-8")).hexdigest() if source_text is not None else ""
    facts = {
        "schema": "bread-shader-source-reference/v1", "kind": "shader-selection",
        "workspaceId": workspace_id,
        "workspacePath": str(workspace.get("workspacePath") or ""),
        "sourcePath": str(source_path), "capturePath": str(binding.get("capturePath") or workspace.get("capturePath") or ""),
        "sourceSha256": source_sha,
        "savedSourceSha256": str(document.get("rawSha256") or ""),
        "unsavedChanges": bool(document.get("dirty")) or (
            source_text is not None and document.get("baselineEditorText") is not None
            and source_text != document.get("baselineEditorText")), "knownEventIds": events[:128],
        "selection": source_range,
        "sourceIncluded": False,
    }
    if source_text is not None:
        facts["readSource"] = {"tool": "read_shader_workspace_source", "arguments": {
            "workspace_id": workspace_id, "capture_session_id": str(binding.get("captureSessionId") or ""),
            "source_sha256": source_sha, "start_line": int(start_line), "end_line": int(end_line)}}
    else:
        # An unopened source may not have an authorized UI snapshot. Its path
        # remains a hint to the managed file reader, never direct read authority.
        root = facts["workspacePath"]
        try:
            relative = os.path.relpath(source_path, root).replace("\\", "/") if root else ""
        except ValueError:
            relative = ""
        if relative and relative != ".." and not relative.startswith("../"):
            facts["readSource"] = {"tool": "read_shader_workspace_file", "arguments": {
                "workspace_path": root, "relative_path": relative, "mode": "range"}}
        facts["sourceState"] = "saved-file-not-open"
    return context, facts


def _calculation_attachment(document, scope, source_text, current_event=0,
                            selected_event=0, known_events=()):
    """Capture a bounded calculation reference from the current editor snapshot."""
    lines = source_text.splitlines()
    spans = []
    for raw in (scope.get("spans") or [])[:64]:
        start, end = int(raw.get("startLine", 0)), int(raw.get("endLine", 0))
        if not 1 <= start <= end <= max(1, len(lines)):
            continue
        span = {key: raw[key] for key in ("functionId", "functionName", "startLine", "endLine")
                if key in raw}
        spans.append(span)
    if not spans:
        raise ValueError("Calculation has no current source range")
    first = spans[0]
    context, facts = _selection_attachment(document, "", document.get("sourcePath", ""),
        first["startLine"], first["endLine"], current_event, selected_event, known_events,
        source_text=source_text)
    # Descriptions and source locations are inert reference data. Only the Host can
    # resolve these identities; an attachment never authorizes a path or edit.
    facts.update(schema="bread-flow-selection/v1", kind=scope.get("kind", "shader-calculation"),
        calculation={key: str(scope.get(key) or "")[:1200] for key in
                     ("id", "title", "description", "provenance", "functionId", "functionName")},
        spans=spans, spanCount=len(scope.get("spans") or []),
        graph={key: str((scope.get("graph") or {}).get(key) or "")[:512]
               for key in ("graphId", "nodeId", "functionId", "sourceSha256")})
    return context, facts


def _retain_attachment_source(context, facts, source_text):
    """Keep referenced editor text locally, outside every chat/provider payload."""
    retained = _shader_source_references.REFERENCES.retain(context, facts, source_text)
    # Keep each attachment's range; the retained source itself allows arbitrary
    # context or whole-file reads using the same immutable SHA-256 revision.
    facts["readSource"]["arguments"].update(retained["arguments"])


def _normal_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path or ""))))


def _workspace_usage_key(workspace, binding):
    """A current-session usage lookup must never be keyed by RDC path alone."""
    if not isinstance(binding, dict):
        return None
    resource = workspace.get("resourceId")
    capture_path = str(binding.get("capturePath", "") or "")
    if (isinstance(resource, bool) or not isinstance(resource, int) or resource <= 0 or
            not capture_path or not binding.get("instanceId") or not binding.get("captureSessionId") or
            _normal_path(str(workspace.get("capturePath", "") or "")) != _normal_path(capture_path)):
        return None
    return (binding["instanceId"], binding["captureSessionId"], _normal_path(capture_path), resource)


def _workspace_usage_keys(workspace, binding):
    """One project document may replace several original resources in this capture."""
    keys = []
    for target in [workspace] + list(workspace.get("relatedWorkspaceBindings") or []):
        if not isinstance(target, dict):
            continue
        key = _workspace_usage_key(target, binding)
        if key is not None and key not in keys:
            keys.append(key)
    return tuple(keys)


def _workspace_usage_complete(workspace, binding, cache):
    keys = _workspace_usage_keys(workspace, binding)
    return bool(keys) and all(key in cache for key in keys)


def _workspace_live_event_ids(workspace, binding, cache, known_events=()):
    keys = _workspace_usage_keys(workspace, binding)
    if not keys or not any(key in cache for key in keys):
        return None
    # Keep representative catalog anchors until every original resource has
    # answered; then the frame-wide usage index is authoritative, including [] .
    events = set() if all(key in cache for key in keys) else set(known_events)
    for key in keys:
        events.update(cache.get(key, ()))
    return sorted(events)


def _workspace_tree_event_ids(workspace, event_ids):
    """Use numeric capture events for labels and ordering, never label text."""
    values = event_ids.get(str(workspace.get("workspaceId", "") or ""), ())
    return sorted(set(value for value in values
                      if isinstance(value, int) and not isinstance(value, bool) and value > 0))


def _workspace_tree_sort_key(workspace, event_ids):
    events = _workspace_tree_event_ids(workspace, event_ids)
    capture_path = str(workspace.get("capturePath", "") or "")
    working_copy = workspace.get("retainedWorkingCopy") is True
    capture_key = ("" if working_copy else _normal_path(capture_path) if capture_path else
                   str(workspace.get("captureId", "") or ""))
    return (working_copy, capture_key,
            not bool(events), events[0] if events else 0,
            str(workspace.get("stage", "") or "").lower(),
            str(workspace.get("entryPoint", "") or "").casefold(),
            str(workspace.get("sourcePath", "") or "").replace("\\", "/").casefold(),
            str(workspace.get("workspaceId", "") or ""))


def _document_alias_target(workspace_id, aliases):
    """Resolve one display alias without accepting cycles or broken chains."""
    original = str(workspace_id or "")
    current = original
    seen = set()
    while current in aliases:
        if current in seen:
            return original
        seen.add(current)
        target = aliases[current]
        if not isinstance(target, str) or not target:
            return original
        current = target
    return current


def _format_message(translated, *arguments):
    values = tuple(str(value) for value in arguments)

    def _replace(match):
        index = int(match.group(1))
        return values[index - 1] if 0 < index <= len(values) else match.group(0)

    return re.sub(r"%([1-9][0-9]*)", _replace, str(translated))


def _latest_event(events, names):
    selected = None
    accepted = set(names)
    for event in events or []:
        if isinstance(event, dict) and event.get("type") in accepted:
            if selected is None or int(event.get("sequence", 0)) > int(
                    selected.get("sequence", 0)):
                selected = event
    return selected


def _classify_commit_unknown(base_hash, submitted_hash, disk_hash):
    """Classify an observed disk hash after an uncertain Host save."""

    if disk_hash == submitted_hash:
        return "committed"
    if disk_hash == base_hash:
        return "pending"
    return "external"


def _document_read_identity(document):
    """UI-owned CAS identity, without copying or reading the native buffer."""
    if document is None:
        return None
    return (document.get("documentToken"), document.get("textRevision", 0),
            document.get("rawSha256"), document.get("sourcePath"),
            bool(document.get("saveInFlight")), bool(document.get("commitUnknown")),
            (document.get("pendingSave") or {}).get("saveToken"))


def _paths_signature(paths):
    values = []
    for path in paths:
        try:
            info = os.stat(path)
            values.append((path, getattr(info, "st_mtime_ns", int(
                info.st_mtime * 1000000000)), int(info.st_size), int(info.st_mode)))
        except OSError:
            values.append((path, -1, -1, -1))
    return tuple(values)


# Which capture events (EIDs) bind each workspace's shader. Resolved on the
# file worker from the workspace manifest's catalogPath -> catalog "uses"
# (each use = eventId + the ShaderKey fields), grouped by resourceId, which is
# unique per capture. The catalog can be large and rarely changes, so it is
# cached by (path, mtime, size) across the 2 s snapshot polls.
_EVENT_ID_CACHE = {}
_EVENT_ID_CACHE_LOCK = threading.Lock()
_MAX_EVENT_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_EVENT_CATALOG_BYTES = 64 * 1024 * 1024


def shader_target_workspace_ids(workspaces, request, capture_binding):
    """Match physical capture/stage/RID/entry identities, including shared-source bindings."""
    if not capture_binding or _normal_path(request.get("capturePath", "")) != _normal_path(
            capture_binding.get("capturePath", "")):
        return []
    aliases = {"pixel": "fragment", "ps": "fragment", "fs": "fragment", "vs": "vertex",
               "hs": "hull", "tcs": "hull", "ds": "domain", "tes": "domain",
               "gs": "geometry", "cs": "compute", "as": "task", "ms": "mesh"}
    def stage(value):
        text = str(value or "").lower()
        return aliases.get(text, text)
    result = []
    for key, model in workspaces.items():
        for binding in [model] + list(model.get("relatedWorkspaceBindings") or []):
            if not isinstance(binding, dict):
                continue
            if (_normal_path(binding.get("capturePath", "")) == _normal_path(request["capturePath"])
                    and str(binding.get("resourceId", "")) == str(request.get("resourceId", ""))
                    and stage(binding.get("stage")) == stage(request.get("stage"))
                    and str(binding.get("entryPoint", "")) == str(request.get("entryPoint", ""))):
                result.append(key)
                break
    return result


def _read_bounded_json(path, maximum):
    """Read one JSON object with a size cap; any problem yields None."""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        if size <= 0 or size > maximum:
            return None
        with open(path, "rb") as handle:
            raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            return None
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _catalog_event_ids_by_resource(catalog_path):
    """Map shader resourceId -> sorted event IDs for one capture catalog (cached)."""
    try:
        info = os.stat(catalog_path)
        stamp = (int(getattr(info, "st_mtime_ns", 0) or 0), int(info.st_size))
    except OSError:
        return {}
    with _EVENT_ID_CACHE_LOCK:
        cached = _EVENT_ID_CACHE.get(catalog_path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    catalog = _read_bounded_json(catalog_path, _MAX_EVENT_CATALOG_BYTES)
    by_resource = {}
    uses = catalog.get("uses") if catalog else None
    if isinstance(uses, list):
        for use in uses:
            if not isinstance(use, dict):
                continue
            resource = use.get("resourceId", use.get("shaderResourceId"))
            event = use.get("eventId")
            if (isinstance(resource, bool) or not isinstance(resource, int) or
                    isinstance(event, bool) or not isinstance(event, int) or
                    resource <= 0 or not 0 < event <= 0xFFFFFFFF):
                continue
            by_resource.setdefault(resource, set()).add(event)
    result = dict((key, sorted(value)) for key, value in by_resource.items())
    with _EVENT_ID_CACHE_LOCK:
        _EVENT_ID_CACHE[catalog_path] = (stamp, result)
        # Bound the cache to the catalogs still referenced recently.
        if len(_EVENT_ID_CACHE) > 32:
            for stale in list(_EVENT_ID_CACHE)[:-32]:
                _EVENT_ID_CACHE.pop(stale, None)
    return result


def _workspace_event_ids(workspaces):
    """Return {workspaceId: [eventId, ...]} for the eligible workspaces.

    Never raises: a workspace whose manifest or catalog cannot be read simply
    has no event list (the tree then shows none rather than a wrong one).
    """
    result = {}
    for document in workspaces:
        workspace_id = str(document.get("workspaceId", "") or "")
        for workspace in [document] + list(document.get("relatedWorkspaceBindings") or []):
            if not isinstance(workspace, dict):
                continue
            if (workspace is not document and
                    _normal_path(workspace.get("capturePath", "")) !=
                    _normal_path(document.get("capturePath", ""))):
                continue
            events = _workspace_catalog_event_ids(workspace)
            if workspace_id and events:
                result.setdefault(workspace_id, set()).update(events)
    return dict((key, sorted(events)) for key, events in result.items())


def _workspace_catalog_event_ids(workspace):
    """Resolve this physical binding, never an identically numbered RID in another RDC."""
    try:
        workspace_path = str(workspace.get("workspacePath", "") or "")
        if not workspace_path:
            return []
        manifest = _read_bounded_json(
            os.path.join(workspace_path, "manifest.json"), _MAX_EVENT_MANIFEST_BYTES)
        if not manifest:
            return []
        capture = manifest.get("capture")
        shader = manifest.get("shader")
        if isinstance(capture, dict):
            expected_path = str(workspace.get("capturePath") or "")
            manifest_path = str(capture.get("capturePath") or "")
            if expected_path and manifest_path and _normal_path(expected_path) != _normal_path(manifest_path):
                return []
        catalog_path = capture.get("catalogPath") if isinstance(capture, dict) else None
        resource = shader.get("resourceId") if isinstance(shader, dict) else None
        if resource is None:
            resource = workspace.get("resourceId")
        if (not isinstance(catalog_path, str) or not catalog_path or
                isinstance(resource, bool) or not isinstance(resource, int) or resource <= 0):
            return []
        return _catalog_event_ids_by_resource(catalog_path).get(resource, [])
    except (OSError, ValueError, TypeError):
        return []


def _build_workspace_snapshot(request):
    """Filesystem/protocol work only. Never capture a panel, editor or Qt object."""
    try:
        with _shader_console_protocol.cancellable_console_reads(request["stop"]), \
                _shader_console_protocol.console_read_batch():
            return _build_workspace_snapshot_files(request)
    except _shader_console_protocol.ShaderConsoleReadCancelled:
        return {"cancelled": True}


def _source_read_session(workspace):
    """Compare the source reader's session, not a historical diagnostic audit."""
    session = workspace.get("sourceSession")
    if isinstance(session, dict):
        return session
    # Older snapshots/test adapters predate the explicit read-session field.
    return {"liveEditId": (workspace.get("liveEdit") or {}).get("liveEditId"),
            "watchId": (workspace.get("watch") or {}).get("watchId")}


def _build_workspace_snapshot_files(request):
    """Build one cancellable batch on the executor's file worker."""
    stop = request["stop"]
    if stop.is_set():
        return {"cancelled": True}
    signature = _paths_signature(request["watchedPaths"])
    if (request.get("knownSources") and signature == request["previousSignature"]):
        sources, errors = {}, {}
        for workspace in request["knownSources"]:
            key = workspace['workspaceId']
            try:
                with _shader_console_protocol.known_source_locator(workspace['workspacePath']):
                    opened = _shader_console_protocol.load_authorized_source_document(
                        request['registryPath'], workspace['authorizationId'], key,
                        capture_binding=request['captureBinding'])
                if (opened.get('captureBinding') != request['captureBinding'] or
                        opened.get('workspaceId') != key or
                        opened.get('authorizationId') != workspace['authorizationId'] or
                        opened.get('sourcePath') != workspace['sourcePath'] or
                        opened.get('liveEditId') != _source_read_session(workspace).get('liveEditId') or
                        opened.get('watchId') != _source_read_session(workspace).get('watchId')):
                    raise ValueError('Shader workspace source session changed during refresh')
                sources[key] = opened
            except Exception as error:
                errors[key] = str(error)[:2048]
        return {'sourceOnly': True, 'sources': sources, 'sourceErrors': errors}
    if not request["force"] and signature == request["previousSignature"]:
        return {"unchanged": True}
    snapshot = _shader_console_protocol.load_console_snapshot(
        request["registryPath"], capture_binding=request["captureBinding"],
        include_console_events=False)
    if stop.is_set():
        return {"cancelled": True}
    aliases = snapshot.get("documentAliases") or {}
    requested_ids = list(dict.fromkeys(aliases.get(key, key) for key in request["documentIds"]))
    eligible = [item for item in (snapshot.get("workspaces", []) + [
        alternate for alternate in snapshot.get("alternateWorkspaces", [])
        if alternate.get("workspaceId") in requested_ids]) if item.get("editorEligible") is True]
    # Read requested tabs first. Loading every readable source on every
    # metadata refresh delays a single click behind unrelated large Shaders.
    priorities = dict((str(key), index) for index, key in enumerate(requested_ids))
    selected = sorted((item for item in eligible
                       if str(item["workspaceId"]) in priorities),
                      key=lambda item: priorities[str(item["workspaceId"])])
    if not selected and not requested_ids:
        selected = eligible[:1]
    sources = {}
    errors = {}
    for workspace in selected[:MAX_OPEN_DOCUMENTS]:
        if stop.is_set():
            return {"cancelled": True}
        key = str(workspace["workspaceId"])
        try:
            opened = _shader_console_protocol.load_authorized_source_document(
                request["registryPath"], workspace.get("authorizationId", ""), key,
                capture_binding=request["captureBinding"])
            if (opened.get("captureBinding") != request["captureBinding"] or
                    opened.get("workspaceId") != key or
                    opened.get("authorizationId") != workspace.get("authorizationId") or
                    opened.get("sourcePath") != workspace.get("sourcePath") or
                    opened.get("liveEditId") != _source_read_session(workspace).get("liveEditId") or
                    opened.get("watchId") != _source_read_session(workspace).get("watchId")):
                raise ValueError("Shader workspace source session changed during refresh")
            sources[key] = opened
        except Exception as exc:
            errors[key] = str(exc)[:2048]
    preview_paths = []
    for key, previous in request.get("sourcePreviews", {}).items():
        is_include = previous.get("documentKind") == "source-include"
        if ((not is_include and previous.get("canonicalSourceUpdated") is not True and
             previous.get("readCanonicalSource") is not True) or
                previous.get("captureBinding") != request["captureBinding"]):
            continue
        try:
            if is_include:
                opened = _shader_console_protocol.load_related_shader_source_document(
                    previous, capture_binding=request["captureBinding"], refresh=True)
                preview_paths.extend([opened["rootSourcePath"], opened["receiptPath"]])
            else:
                opened = _shader_console_protocol.reload_saved_shader_source_preview(
                    previous, capture_binding=request["captureBinding"])
            preview_paths.append(opened["sourcePath"])
            preview_paths.extend(opened.get("watchedPaths") or [])
            opened.update(workspaceId=key, organizationId=previous.get("organizationId", ""),
                          readOnly=False)
            sources[key] = opened
        except Exception as exc:
            errors[key] = str(exc)[:2048]
    paths = list(dict.fromkeys(path for path in (
        [request["registryPath"], request["projectSelectionPath"]] +
        list(snapshot.get("watchedPaths", [])) + preview_paths) if path))
    # Keep the signature taken before the source reads. Changes during a slow
    # read force the next poll to revalidate rather than becoming a new baseline.
    after = _paths_signature(paths)
    stale_sources = []
    by_path = dict((item[0], item) for item in after)
    for key, opened in list(sources.items()):
        info = by_path.get(opened["sourcePath"])
        if info is None:
            info = _paths_signature([opened["sourcePath"]])[0]
        if info[1:3] != (opened.get("modifiedNs"), opened.get("size")):
            stale_sources.append(key)
            del sources[key]
    watched = [path for path in paths if os.path.isfile(path) or os.path.isdir(path)]
    try:
        event_ids = _workspace_event_ids(eligible)
    except Exception:  # never let the EID decoration break the snapshot
        event_ids = {}
    # A persistent source/session error is still a valid observation of this
    # exact disk state. Retain its signature so the 2 s UI poll does not reread
    # every workspace forever. File changes invalidate it immediately; a
    # separate bounded retry handles transient OS/Host failures. A source that
    # changed during this read gets one immediate follow-up instead.
    stable_during_read = signature == _paths_signature(request["watchedPaths"])
    return {"snapshot": snapshot, "sources": sources, "sourceErrors": errors,
            "eventIds": event_ids,
            "watchedPaths": paths, "existingPaths": watched,
            "signature": after if not stale_sources and stable_during_read else None,
            "staleSources": stale_sources}


def _new_snapshot_executor():
    try:
        from .project_launcher import AsyncProjectExecutor
    except (ImportError, ValueError):
        spec = importlib.util.spec_from_file_location(
            "_replaylab_shader_workspace_project_executor", os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "project_launcher.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        AsyncProjectExecutor = module.AsyncProjectExecutor
    return AsyncProjectExecutor(1)


def _build_source_flow_projection(request):
    """Index a loaded buffer off-thread; only navigation checkpoints use disk IO.

    No Qt objects, compilation or replay calls. The optional checkpoint writer
    compares committed source bytes before replacing its disposable sidecar.
    """
    if request["stop"].is_set():
        return {"cancelled": True}
    try:
        hierarchy = request.get("parsedHierarchy")
        if hierarchy is None:
            hierarchy = _shader_flow_hierarchy.SourceFlowHierarchy(
                request["sourceText"], request["sourceIdentity"])
        overview = request.get("semanticOverview")
        original_overview = request.get('navigationOriginal', overview)
        navigation_store = None
        storage = request.get('navigationStorage')
        if isinstance(overview, dict) and isinstance(storage, dict) and storage.get('root'):
            try:
                navigation_store = _shader_navigation_history.NavigationHistory(storage['root'], storage['identity'])
                if not request.get('outlineAnchorsAdjusted') and not request.get('hasLiveOutlineAnchors'):
                    overview = navigation_store.read(original_overview) or overview
            except (ValueError, TypeError, OSError):
                navigation_store = None
        if isinstance(overview, dict):
            _shader_outline_anchors.prepare(hierarchy)
            overview = _shader_stage_outline.reanchor_semantic_overview(hierarchy, overview)
        if hasattr(hierarchy, "set_function_annotations"):
            hierarchy.set_function_annotations(overview)
    except ValueError as exc:
        return {"state": "invalid-source", "error": str(exc)[:1024]}
    if request["stop"].is_set():
        return {"cancelled": True}
    accepted = request.get("graph")
    graph_source = ((accepted.get("sourceSha256") or
                     (accepted.get("binding") or {}).get("sourceSha256"))
                    if isinstance(accepted, dict) else None)
    # Saved AI annotations describe only their exact source bytes. A stale or
    # missing artifact cannot lend descriptions/entry identity to edited code.
    if graph_source != hierarchy.source_sha256:
        accepted = {"entryPoint": request["entryPoint"], "nodes": [], "edges": []}
    entry = str(request["entryPoint"])
    if not hierarchy.function_identity(entry):
        return {"hierarchy": hierarchy, "state": "entry-unavailable"}
    accepted = dict(accepted, entryPoint=entry)
    projection = hierarchy.project_root(accepted, functions_only=True)
    projection["sourceDerived"] = True
    stages = _shader_stage_outline.build_stage_outline(hierarchy, entry, overview)
    if (navigation_store is not None and not request['stop'].is_set()
            and (request.get('outlineAnchorsAdjusted') or original_overview.get('sourceSha256') == hierarchy.source_sha256)):
        navigation_store.write(original_overview, overview, request['sourceText'])
    return {"hierarchy": hierarchy, "graph": projection, "state": "available",
            "navigationOverview": overview,
            "stageOutline": stages,
            "stageOutlineSourceSha256": hierarchy.source_sha256,
            "stageOutlineOverviewSha256": (hashlib.sha256(json.dumps(original_overview,
                sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()
                if isinstance(original_overview, dict) else ""),
            "stageOutlineState": _shader_stage_outline.outline_state(hierarchy, entry, overview, stages=stages)}


def _save_worker_loop(save_client, task_queue, result_queue, stop_event):
    """Run complete Host save calls without ever touching a Qt object."""

    while True:
        task = task_queue.get()
        if task is None:
            return
        if stop_event.is_set():
            continue
        pending_request = getattr(save_client, "request_pending", None)
        while callable(pending_request):
            try:
                if not pending_request():
                    break
            except Exception:
                break
            if stop_event.wait(0.05):
                return
        outcome = {"task": task}
        try:
            if task.get("manualApply") and task.get("sourceDocumentId"):
                outcome["result"] = save_client.save_and_apply_source_document(
                    task["workspacePath"], task["sourceWorkspaceId"], task["sourceDocumentId"],
                    task["baseHash"], task["rootSourceSha256"], task["sourceText"], task["captureBinding"])
            elif task.get("manualApply"):
                outcome["result"] = save_client.save_and_apply_source(
                    task["workspacePath"], task["sourceWorkspaceId"],
                    task["baseHash"], task["sourceText"], task["captureBinding"])
            elif task.get("sourceDocumentId"):
                outcome["result"] = save_client.save_source_document(
                    task["workspacePath"], task["sourceWorkspaceId"], task["sourceDocumentId"],
                    task["baseHash"], task["rootSourceSha256"], task["sourceText"], task["captureBinding"])
            elif task.get("sourceOnly"):
                outcome["result"] = save_client.save_source(
                    task["workspacePath"], task["sourceWorkspaceId"],
                    task["baseHash"], task["sourceText"], task["captureBinding"])
            else:
                outcome["result"] = save_client.save(
                    task["liveEditId"], task["authorizationId"],
                    task["workspaceId"], task["baseHash"], task["sourceText"])
            outcome["kind"] = "saved"
        except Exception as exc:
            if isinstance(
                    exc,
                    _shader_save_client_protocol.ReplayLabShaderSaveConflict):
                outcome["kind"] = "conflict"
            elif isinstance(
                    exc,
                    _shader_save_client_protocol.ReplayLabShaderSaveUncertain):
                outcome["kind"] = "unknown"
            else:
                outcome["kind"] = "error"
            outcome["error"] = str(exc)
        result_queue.put(outcome)


# ---------------------------------------------------------------------------
# Instant local HLSL analysis (IDE-style, no compiler round trip)
#
# Modern shader IDEs surface most mistakes immediately from a local pass over
# the buffer — a symbol/reference check — and only use the compiler as the
# slower ground truth. This pass targets the mistakes editing produces most
# often in organized decompiled HLSL (self-contained, macro-free): using an
# identifier that is declared nowhere in the file (a deleted declaration, a
# typo). It is deliberately scope-insensitive: a name declared anywhere counts
# as declared, which trades a few missed cases for essentially no false
# positives; the compile-check still catches the rest.
# ---------------------------------------------------------------------------
_HLSL_KEYWORDS = frozenset("""
if else for while do switch case default break continue return discard true false
in out inout const static uniform precise nointerpolation linear centroid noperspective
sample groupshared shared extern volatile row_major column_major snorm unorm
globallycoherent export inline struct class interface cbuffer tbuffer typedef enum
namespace register packoffset void this template operator sizeof typename
""".split())
_HLSL_TYPE_ROOTS = ("float", "int", "uint", "bool", "half", "double", "min16float",
                    "min10float", "min16int", "min12int", "min16uint", "int64_t",
                    "uint64_t", "int16_t", "uint16_t", "float16_t", "float32_t",
                    "float64_t", "dword")
_HLSL_OBJECT_TYPES = frozenset("""
matrix vector string Buffer RWBuffer StructuredBuffer RWStructuredBuffer
AppendStructuredBuffer ConsumeStructuredBuffer ByteAddressBuffer RWByteAddressBuffer
ConstantBuffer TextureBuffer Texture1D Texture1DArray Texture2D Texture2DArray
Texture2DMS Texture2DMSArray Texture3D TextureCube TextureCubeArray RWTexture1D
RWTexture1DArray RWTexture2D RWTexture2DArray RWTexture3D RasterizerOrderedBuffer
RasterizerOrderedTexture2D SamplerState SamplerComparisonState sampler sampler1D
sampler2D sampler3D samplerCUBE InputPatch OutputPatch TriangleStream LineStream
PointStream RaytracingAccelerationStructure RayDesc FeedbackTexture2D
""".split())
_HLSL_INTRINSICS = frozenset("""
abs acos all any asdouble asfloat asfloat16 asint asint16 asuint asuint16 atan atan2
ceil clamp clip cos cosh countbits cross D3DCOLORtoUBYTE4 ddx ddx_coarse ddx_fine ddy
ddy_coarse ddy_fine degrees determinant distance dot dot2add dot4add_i8packed
dot4add_u8packed dst exp exp2 f16tof32 f32tof16 faceforward firstbithigh firstbitlow
floor fma fmod frac frexp fwidth isfinite isinf isnan ldexp length lerp lit log log10
log2 mad max min modf msad4 mul noise normalize pow radians rcp reflect refract
reversebits round rsqrt saturate sign sin sincos sinh smoothstep sqrt step tan tanh
transpose trunc tex1D tex2D tex3D texCUBE tex2Dlod tex2Dbias tex2Dproj tex2Dgrad
GroupMemoryBarrier GroupMemoryBarrierWithGroupSync DeviceMemoryBarrier
DeviceMemoryBarrierWithGroupSync AllMemoryBarrier AllMemoryBarrierWithGroupSync
InterlockedAdd InterlockedAnd InterlockedOr InterlockedXor InterlockedMin InterlockedMax
InterlockedExchange InterlockedCompareExchange InterlockedCompareStore
WaveActiveSum WaveActiveAllTrue WaveActiveAnyTrue WaveActiveAllEqual WaveGetLaneIndex
WaveGetLaneCount WaveIsFirstLane WaveReadLaneAt WaveReadLaneFirst WaveActiveBallot
WaveActiveCountBits WaveActiveMin WaveActiveMax WaveActiveProduct WaveActiveBitAnd
WaveActiveBitOr WaveActiveBitXor WavePrefixSum WavePrefixProduct WavePrefixCountBits
WaveMatch WaveMultiPrefixSum QuadReadAcrossX QuadReadAcrossY QuadReadAcrossDiagonal
QuadReadLaneAt QuadAny QuadAll EvaluateAttributeAtSample EvaluateAttributeCentroid
EvaluateAttributeSnapped GetRenderTargetSampleCount GetRenderTargetSamplePosition
CheckAccessFullyMapped abort errorf printf NonUniformResourceIndex pack_s8 pack_u8
pack_clamp_s8 pack_clamp_u8 unpack_s8s16 unpack_s8s32 unpack_u8u16 unpack_u8u32
IsHelperLane select and or not TraceRay ReportHit IgnoreHit AcceptHitAndEndSearch
CallShader DispatchMesh SetMeshOutputCounts GetAttributeAtVertex
""".split())
_HLSL_DECL_MODIFIERS = frozenset("""
const static in out inout uniform precise nointerpolation linear centroid noperspective
sample groupshared shared extern volatile row_major column_major snorm unorm
globallycoherent export inline unsigned signed
""".split())
# Not preceded by an identifier/number character or "." — this drops number
# suffixes (0.5f, 1e5f, 0x1F) and member accesses (.xyz, .Sample) at once.
_HLSL_IDENT = re.compile(r"(?<![A-Za-z0-9_.])[A-Za-z_][A-Za-z0-9_]*")
_HLSL_TYPE_SHAPE = re.compile(r"^(?:%s)(?:[1-4](?:x[1-4])?)?$" % "|".join(_HLSL_TYPE_ROOTS))
_HLSL_MAX_LOCAL_DIAGNOSTICS = 60


def _hlsl_blank_noncode(text):
    """Replace comments, string/char literals and preprocessor lines with spaces.

    Lengths and newlines are preserved so every offset still maps to the
    original buffer. Returns (blanked_text, defined_macro_names).
    """
    out = list(text)
    n = len(text)
    defines = set()
    i = 0
    line_start = True
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = i
            while j < n and text[j] != "\n":
                out[j] = " "; j += 1
            i = j; continue
        if c == "/" and nxt == "*":
            j = i + 2
            while j < n and not (text[j] == "*" and j + 1 < n and text[j + 1] == "/"):
                if text[j] != "\n":
                    out[j] = " "
                j += 1
            end = min(n, j + 2)
            for k in range(i, end):
                if text[k] != "\n":
                    out[k] = " "
            i = end; line_start = False; continue
        if c in "\"'":
            quote = c; j = i + 1
            while j < n and text[j] != quote and text[j] != "\n":
                if text[j] == "\\":
                    j += 1
                j += 1
            end = min(n, j + 1)
            for k in range(i, end):
                if text[k] != "\n":
                    out[k] = " "
            i = end; line_start = False; continue
        if c == "#" and line_start:
            j = i
            # A preprocessor line may continue with a trailing backslash.
            while j < n:
                if text[j] == "\n" and not (j > 0 and text[j - 1] == "\\"):
                    break
                j += 1
            directive = text[i:j]
            m = re.match(r"#\s*define\s+([A-Za-z_]\w*)", directive)
            if m:
                defines.add(m.group(1))
            for k in range(i, j):
                if text[k] != "\n":
                    out[k] = " "
            i = j; continue
        if c == "\n":
            line_start = True
        elif not c.isspace():
            line_start = False
        i += 1
    return "".join(out), defines


def _hlsl_is_type_word(word, user_types):
    return (word in user_types or word in _HLSL_OBJECT_TYPES or
            _HLSL_TYPE_SHAPE.match(word) is not None or word == "void")


def hlsl_local_diagnostics(text):
    """Return instant diagnostics for identifiers used but declared nowhere."""
    if not isinstance(text, str) or not text.strip():
        return []
    code, defines = _hlsl_blank_noncode(text)
    tokens = [(m.group(0), m.start(), m.end()) for m in _HLSL_IDENT.finditer(code)]
    if not tokens:
        return []

    def prev_sig(pos):
        """Previous non-space character before offset pos, and its index."""
        k = pos - 1
        while k >= 0 and code[k].isspace():
            k -= 1
        return (code[k] if k >= 0 else ""), k

    def next_sig(pos):
        k = pos
        while k < len(code) and code[k].isspace():
            k += 1
        return (code[k] if k < len(code) else ""), k

    # Pass 1 — user type names: struct/class/interface/cbuffer X, typedef ... X;
    user_types = set()
    for index, (word, start, end) in enumerate(tokens):
        if word in ("struct", "class", "interface", "cbuffer", "tbuffer") and index + 1 < len(tokens):
            nxt = tokens[index + 1]
            if nxt[1] > end and next_sig(end)[1] == nxt[1]:
                user_types.add(nxt[0])
        elif word == "typedef":
            # typedef <type...> NAME ;  -> the identifier right before ';'
            semi = code.find(";", end)
            if semi > 0:
                seg = code[end:semi]
                names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", seg)
                if names:
                    user_types.add(names[-1])

    # Pass 2 — declared names: "<type> [modifiers] NAME" (+ "type<...> NAME"),
    # comma-continued declarations at the same nesting depth, and macros.
    declared = set(defines)
    depth_at = []  # paren depth per token
    depth = 0
    pos = 0
    for word, start, end in tokens:
        depth += code.count("(", pos, start) - code.count(")", pos, start)
        depth_at.append(depth)
        pos = start
    decl_depth = None  # paren depth of the active declaration statement, if any
    for index, (word, start, end) in enumerate(tokens):
        before, before_idx = prev_sig(start)
        after, after_idx = next_sig(end)
        # A declaration statement ends at ';' or '{' or when leaving its depth.
        if decl_depth is not None:
            between = code[tokens[index - 1][2]:start] if index else ""
            if ";" in between or "{" in between or "}" in between or depth_at[index] < decl_depth:
                decl_depth = None
        if word in _HLSL_DECL_MODIFIERS or word in ("register", "packoffset"):
            continue
        if _hlsl_is_type_word(word, user_types):
            # Find the declared name: skip template args <...> and modifiers.
            j = index + 1
            scan_pos = end
            nxt_char, nxt_idx = next_sig(end)
            if nxt_char == "<":
                close = code.find(">", nxt_idx)
                if close < 0:
                    continue
                scan_pos = close + 1
                while j < len(tokens) and tokens[j][1] < scan_pos:
                    j += 1
            while j < len(tokens) and tokens[j][0] in _HLSL_DECL_MODIFIERS:
                j += 1
            if j < len(tokens):
                name, nstart, nend = tokens[j]
                gap = code[scan_pos:nstart]
                if gap.strip() == "" and not _hlsl_is_type_word(name, user_types) and name not in _HLSL_KEYWORDS:
                    # "float4 foo" / "float4 Foo(" / "Texture2D<float4> tex"
                    # but not "float4(x)" (constructor) — the gap must be pure space
                    declared.add(name)
                    decl_depth = depth_at[j]
            continue
        if decl_depth is not None and before == "," and depth_at[index] == decl_depth:
            # "float a, b, c;" / "int x = 1, y = 2;"
            if after in ("=", ",", ";", "[", ":"):
                declared.add(word)
            continue

    # Pass 3 — uses that are declared nowhere.
    diagnostics = []
    reported = set()
    line_starts = [0]
    for m in re.finditer("\n", code):
        line_starts.append(m.end())
    import bisect
    for index, (word, start, end) in enumerate(tokens):
        if (word in declared or word in _HLSL_KEYWORDS or word in _HLSL_INTRINSICS or
                word in _HLSL_DECL_MODIFIERS or _hlsl_is_type_word(word, user_types)):
            continue
        before, before_idx = prev_sig(start)
        # Member access / method call / scope: .xyz  .Sample(  A::b
        if before == "." or (before == ":" and before_idx > 0 and code[before_idx - 1] == ":"):
            continue
        # Namespace qualifiers are not value expressions. This also covers
        # [[vk::location(...)]] / [[vk::binding(...)]] inside struct fields,
        # where the single-bracket statement-attribute check below does not
        # apply. Keep checking argument values and unqualified uses of "vk".
        after, after_idx = next_sig(end)
        if after == ":" and code[after_idx:after_idx + 2] == "::":
            continue
        # Semantic after ':' (param/return/field semantics) up to , ) ; { =
        if before == ":":
            continue
        # Inside register(...) / packoffset(...)
        open_paren = code.rfind("(", 0, start)
        if open_paren >= 0 and code.find(")", open_paren, start) < 0:
            head = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", code[max(0, open_paren - 24):open_paren])
            if head and head[-1] in ("register", "packoffset"):
                continue
        # Inside a statement-level [attribute]
        open_br = code.rfind("[", 0, start)
        if open_br >= 0 and code.find("]", open_br, start) < 0:
            b2, _ = prev_sig(open_br)
            if b2 in ("", ";", "{", "}", "]"):
                continue
        # Labels "case FOO:" are rare in HLSL; treat identifiers followed by ':'
        # at statement start as semantics-like and skip.
        if before in ("", ";", "{", "}") and next_sig(end)[0] == ":":
            continue
        line = bisect.bisect_right(line_starts, start)  # 1-based
        column = start - line_starts[line - 1] + 1
        key = (word, line)
        if key in reported:
            continue
        reported.add(key)
        diagnostics.append({
            "line": line, "column": column, "length": end - start,
            "severity": "error",
            "text": "Undefined identifier '%s'" % word,
            "source": "local",
        })
        if len(diagnostics) >= _HLSL_MAX_LOCAL_DIAGNOSTICS:
            break
    return diagnostics


def _analysis_worker_loop(task_queue, result_queue, stop_event):
    """Run the instant local HLSL analysis off the UI thread (latest wins)."""

    while True:
        task = task_queue.get()
        if task is None:
            return
        # Coalesce: only the newest buffer matters while the user types.
        while True:
            try:
                newer = task_queue.get_nowait()
            except queue.Empty:
                break
            if newer is None:
                return
            task = newer
        if stop_event.is_set():
            continue
        try:
            diagnostics = hlsl_local_diagnostics(task.get("sourceText", ""))
        except Exception:
            diagnostics = []
        result_queue.put({"task": task, "diagnostics": diagnostics})


def _clamp_diagnostics_to_text(diagnostics, text):
    """Pull end-of-line diagnostics back onto the last token of their line.

    Compilers report "expected ';'" just past the last token, i.e. on the line
    break, where an underline has no glyph to sit on. Like other IDEs, anchor
    such a diagnostic to the last token so the squiggle is visible. Uses the
    exact text that was compiled, so columns map 1:1.
    """
    if not diagnostics or not isinstance(text, str):
        return diagnostics
    lines = text.split("\n")
    result = []
    for item in diagnostics:
        entry = dict(item)
        try:
            line = int(entry.get("line", 0) or 0)
            column = int(entry.get("column", 0) or 0)
        except (TypeError, ValueError):
            result.append(entry)
            continue
        if 1 <= line <= len(lines):
            content = lines[line - 1].rstrip("\r")
            stripped = content.rstrip()
            if stripped and column > len(stripped):
                end = len(stripped)
                start = end
                while start > 0 and end - start < 40 and (stripped[start - 1].isalnum() or stripped[start - 1] in "_)](."):
                    start -= 1
                if start == end:
                    start = end - 1
                entry["column"] = start + 1
                entry["length"] = max(1, end - start)
        result.append(entry)
    return result


def _check_worker_loop(save_client, task_queue, result_queue, stop_event):
    """Run Host compile-checks (unsaved-buffer diagnostics) off the UI thread.

    A check never writes, publishes, or applies, so a failure is only ever
    reported as an error outcome and never fabricates editor squiggles.
    """

    while True:
        task = task_queue.get()
        if task is None:
            return
        if stop_event.is_set():
            continue
        pending_request = getattr(save_client, "request_pending", None)
        while callable(pending_request):
            try:
                if not pending_request():
                    break
            except Exception:
                break
            if stop_event.wait(0.05):
                return
        outcome = {"task": task}
        try:
            outcome["result"] = save_client.check(
                task["liveEditId"], task["authorizationId"],
                task["workspaceId"], task["sourceText"])
            outcome["kind"] = "checked"
        except Exception as exc:
            outcome["kind"] = "error"
            outcome["error"] = str(exc)
        result_queue.put(outcome)


def _join_save_worker(worker, timeout_seconds, request_pending):
    """Bound shutdown latency and prove that the Python worker has stopped."""

    if worker is None:
        return True
    if worker is threading.current_thread():
        raise RuntimeError(
            "Bread Shader Workspace save worker cannot join itself")
    timeout = max(0.0, float(timeout_seconds))
    worker.join(timeout)
    if not worker.is_alive():
        return True
    try:
        transport_pending = bool(request_pending())
    except Exception:
        transport_pending = True
    # i18n-ignore: developer-only shutdown diagnostic printed by the bridge.
    raise RuntimeError(
        "Bread Shader Workspace save worker did not stop within %.3f seconds "
        "(Host request pending: %s)" %
        (timeout, "yes" if transport_pending else "no"))


if HAS_QT:
    class _ShaderStageDelegate(QtWidgets.QStyledItemDelegate):
        """Quiet, palette-aware navigation rows with a clear active stage."""
        def sizeHint(self, option, index):
            return QtCore.QSize(220, max(52, option.fontMetrics.height() * 2 + 20))

        def paint(self, painter, option, index):
            stage = index.data(QtCore.Qt.UserRole + 1) or {}
            painter.save()
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            selected = bool(option.state & QtWidgets.QStyle.State_Selected)
            hovered = bool(option.state & QtWidgets.QStyle.State_MouseOver)
            rect = option.rect.adjusted(2, 2, -2, -2)
            if selected or hovered:
                fill = QtGui.QColor(option.palette.highlight().color() if selected
                                    else option.palette.alternateBase().color())
                fill.setAlpha(95 if selected else 130)
                painter.setPen(QtCore.Qt.NoPen)
                painter.setBrush(fill)
                painter.drawRoundedRect(rect, 5, 5)
            if selected:
                painter.fillRect(QtCore.QRect(rect.left(), rect.top() + 7, 3, rect.height() - 14),
                                 option.palette.highlight())
            color = option.palette.text().color()
            subdued = QtGui.QColor(color)
            subdued.setAlpha(165)
            painter.setPen(subdued)
            number = QtCore.QRect(rect.left() + 10, rect.top() + 8, 28, rect.height() - 16)
            painter.drawText(number, QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft,
                             str(int(stage.get('order', 0))).zfill(2))
            title_rect = rect.adjusted(42, 7, -8, -22)
            font = QtGui.QFont(option.font)
            font.setBold(selected)
            painter.setFont(font)
            painter.setPen(color)
            title = QtGui.QFontMetrics(font).elidedText(
                str(stage.get('title') or ''), QtCore.Qt.ElideRight, title_rect.width())
            painter.drawText(title_rect, QtCore.Qt.AlignVCenter, title)
            font.setBold(False)
            font.setPointSizeF(max(7.0, font.pointSizeF() - 1.0))
            painter.setFont(font)
            painter.setPen(subdued)
            detail = QtGui.QFontMetrics(font).elidedText(
                str(stage.get('functionName') or ''), QtCore.Qt.ElideRight, title_rect.width())
            painter.drawText(rect.adjusted(42, 27, -8, -5), QtCore.Qt.AlignVCenter, detail)
            painter.restore()

    class _WorkspaceRoot(QtWidgets.QWidget):
        def __init__(self, owner):
            super(_WorkspaceRoot, self).__init__()
            self.owner = owner

        @QtCore.Slot(result=bool)
        def checkAllowClose(self):
            # ToolWindowManager queries this exact Qt meta-method before it
            # removes and directly deletes a dock tab; that path does not send
            # QWidget.closeEvent.
            return self.owner is None or self.owner._confirm_panel_close()

        def closeEvent(self, event):
            if self.checkAllowClose():
                event.accept()
                super(_WorkspaceRoot, self).closeEvent(event)
            else:
                event.ignore()


    def _pin_icon(palette, pinned):
        pixmap = QtGui.QPixmap(16, 16)
        pixmap.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.translate(8.0, 7.5)
        color = palette.color(QtGui.QPalette.ButtonText)
        color.setAlpha(235 if pinned else 175)
        painter.setPen(QtGui.QPen(
            color, 1.8 if pinned else 1.5, QtCore.Qt.SolidLine,
            QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
        if pinned:
            fill = QtGui.QColor(color)
            fill.setAlpha(55)
            painter.setBrush(fill)
        else:
            painter.setBrush(QtCore.Qt.NoBrush)
        body = QtGui.QPainterPath()
        body.moveTo(-4.5, -5.0)
        body.lineTo(4.5, -5.0)
        body.lineTo(2.7, -1.7)
        body.lineTo(2.1, 1.0)
        body.lineTo(-2.1, 1.0)
        body.lineTo(-2.7, -1.7)
        body.closeSubpath()
        painter.drawPath(body)
        painter.drawLine(QtCore.QPointF(0.0, 1.0), QtCore.QPointF(0.0, 5.5))
        painter.drawPoint(QtCore.QPointF(0.0, 6.0))
        painter.end()
        return QtGui.QIcon(pixmap)


    class _DocumentTabBar(QtWidgets.QTabBar):
        pinRequested = QtCore.Signal(str)
        closeDocumentRequested = QtCore.Signal(str)
        closeOthersRequested = QtCore.Signal(str)

        def __init__(self, parent=None):
            super(_DocumentTabBar, self).__init__(parent)
            self.setDocumentMode(True)
            self.setMovable(True)
            self.setTabsClosable(True)
            self.setElideMode(QtCore.Qt.ElideMiddle)
            self.setExpanding(False)
            self.tabCloseRequested.connect(self._close_index)
            self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.customContextMenuRequested.connect(self._context_menu)

        def _close_index(self, index):
            workspace_id = str(self.tabData(index) or "")
            if workspace_id:
                self.closeDocumentRequested.emit(workspace_id)

        def configure_pin_button(self, index, workspace_id, pinned):
            button = QtWidgets.QToolButton(self)
            button.setAutoRaise(True)
            button.setFixedSize(20, 20)
            button.setIcon(_pin_icon(self.palette(), pinned))
            if pinned:
                button.setToolTip(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Unpin tab"))
            else:
                button.setToolTip(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Pin tab"))
            button.clicked.connect(
                lambda _checked=False, value=workspace_id: self.pinRequested.emit(value))
            self.setTabButton(index, QtWidgets.QTabBar.LeftSide, button)

        def _context_menu(self, point):
            index = self.tabAt(point)
            if index < 0:
                return
            workspace_id = str(self.tabData(index) or "")
            menu = QtWidgets.QMenu(self)
            close_action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Close tab"))
            others_action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Close other tabs"))
            menu.addSeparator()
            pin_button = self.tabButton(index, QtWidgets.QTabBar.LeftSide)
            pin_action = menu.addAction(
                pin_button.toolTip() if pin_button is not None else
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Pin tab"))
            selected = menu.exec_(self.mapToGlobal(point))
            if selected is close_action:
                self.closeDocumentRequested.emit(workspace_id)
            elif selected is others_action:
                self.closeOthersRequested.emit(workspace_id)
            elif selected is pin_action:
                self.pinRequested.emit(workspace_id)

        def mouseReleaseEvent(self, event):
            if event.button() == QtCore.Qt.MiddleButton:
                index = self.tabAt(event.pos())
                if index >= 0:
                    self._close_index(index)
                    event.accept()
                    return
            super(_DocumentTabBar, self).mouseReleaseEvent(event)


    class ReplayLabShaderWorkspacePanel(object):
        """Docked tree, multi-document HLSL editor and compile/apply status."""

        def __init__(self, ctx, qrd, closed_callback=None, registry_path=None,
                     save_client=None, project_selection_path=None,
                     capture_binding_provider=None, async_executor=None,
                     visual_debug_operation=None):
            global _qt_application_keepalive
            _qt_application_keepalive = QtWidgets.QApplication.instance()
            self.ctx = ctx
            self._capture_binding_provider = capture_binding_provider
            self._capture_binding = None
            self.qrd = qrd
            self.closed_callback = closed_callback
            self.registry_path = str(registry_path or "")
            self.project_selection_path = str(project_selection_path or "")
            self._save_client = (
                save_client or
                _shader_save_client_protocol.ReplayLabShaderSaveClient())
            self._save_tasks = queue.Queue()
            self._save_results = queue.Queue()
            self._save_stop = threading.Event()
            self._save_worker = None
            self._save_worker_stopped = False
            self._check_tasks = queue.Queue()
            self._check_results = queue.Queue()
            self._check_stop = threading.Event()
            self._check_worker = None
            self._check_worker_stopped = False
            self._next_check_token = 0
            # Latest live compile-check per workspace, keyed to the editor
            # text revision it describes so a newer edit never keeps a stale
            # squiggle pinned to code that has since moved.
            self._live_check = {}
            self._live_check_logged = set()
            self._analysis_tasks = queue.Queue()
            self._analysis_results = queue.Queue()
            self._analysis_stop = threading.Event()
            self._analysis_worker = None
            # {workspaceId: {textRevision, diagnostics}} from the instant local pass.
            self._local_analysis = {}
            # {workspaceId: [eventId, ...]} — which capture events bind each shader.
            self._workspace_event_ids = {}
            # Live frame-wide usage from RenderDoc (GetUsage) for the OPEN capture:
            # authoritative and complete (a replaced shader keeps its original id
            # in the frame record), whereas a one-event catalog only knows one EID.
            self._usage_results = queue.Queue()
            self._usage_cache = {}
            self._usage_pending = set()
            self._usage_generation = 0
            self._next_document_token = 0
            self._next_save_token = 0
            self._alive = True
            self._docked = False
            self._shutting_down = False
            self._native_editors_released = False
            self._snapshot = None
            self._snapshot_executor = async_executor or _new_snapshot_executor()
            self._owns_snapshot_executor = async_executor is None
            self._snapshot_owner = "shader-workspace-" + uuid.uuid4().hex
            self._snapshot_generation = 0
            self._snapshot_running = False
            self._snapshot_again = False
            self._snapshot_stop = threading.Event()
            self._source_documents = {}
            self._last_open_error = ""
            self._source_open_errors = {}
            # Compile-verified organization previews are exact-session,
            # read-only projections. They never enter the live-edit registry
            # and therefore survive registry snapshots through this separate
            # in-memory overlay only.
            self._organization_previews = {}
            # In-memory source copies read directly from the current capture.
            # They have no Host save/compile authority or organization review claim.
            self._capture_sources = {}
            self._source_request_states = {}
            self._pending_open = {}
            self._pending_reload = {}
            self._workspaces = {}
            self._document_aliases = {}
            self._documents = {}
            self._tree_items = {}
            self._watched_paths = [path for path in (
                self.registry_path, self.project_selection_path) if path]
            self._last_disk_signature = None
            self._snapshot_error_retry_at = 0.0
            self._snapshot_error_retry_delay = SNAPSHOT_ERROR_RETRY_MIN_SECONDS
            self._source_discovery_error = ""
            self._loading_documents = set()
            self._reordering_tabs = False
            self._restored = False
            self._tree_signature = None
            self._flow_graph_signature = None
            self._flow_request = None
            self._settings = QtCore.QSettings()
            state = self._load_state()
            self._shader_labels = dict(state.get("shaderLabels") or {})
            self._saved_tab_state = dict(state)
            self._restore_default_document = state.get("tabChoicesExplicit") is not True
            self._closed_document_ids = set()
            self._pinned = list(state.get("pinned", []))
            self._restore_open = list(state.get("open", []))
            self._restore_active = str(state.get("active", "") or "")
            # Keep the source navigator out of the way until it is requested;
            # the code canvas and per-Shader flow graph are the primary views.
            # Existing state files predate these fields and therefore receive
            # the requested first-run defaults (files hidden, graph visible).
            self._files_visible = state.get("filesVisible") is True
            self._flow_visible = state.get("flowVisible") is not False
            self._stages_visible = state.get("stagesVisible") is not False
            self._debug_snapshots_visible = state.get("debugSnapshotsVisible") is True
            self._stage_outline_signature = None
            self._stage_items = {}

            self.top = _WorkspaceRoot(self)
            self.top.setObjectName("replaylabShaderWorkspace")
            self.top.setProperty("breadPreserveOnLayoutRestore", True)
            self.top.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
            self.top.setWindowTitle(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader Workspace"))
            self.top.destroyed.connect(self._destroyed)
            self._icon_style = self.top.style()

            outer = QtWidgets.QVBoxLayout(self.top)
            outer.setContentsMargins(4, 4, 4, 4)
            header = QtWidgets.QHBoxLayout()
            self.files_button = QtWidgets.QToolButton(self.top)
            self.files_button.setObjectName(
                "replaylabShaderWorkspaceFilesButton")
            self.files_button.setCheckable(True)
            self.files_button.setChecked(self._files_visible)
            self.files_button.setToolButtonStyle(
                QtCore.Qt.ToolButtonTextBesideIcon)
            self.files_button.setIcon(self._icon_style.standardIcon(
                QtWidgets.QStyle.SP_DirIcon))
            self.files_button.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader files"))
            self.files_button.clicked[bool].connect(self._toggle_file_tree)
            header.addWidget(self.files_button)
            self.stages_button = QtWidgets.QToolButton(self.top)
            self.stages_button.setObjectName("replaylabShaderWorkspaceStagesButton")
            self.stages_button.setCheckable(True)
            self.stages_button.setChecked(self._stages_visible)
            self.stages_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
            self.stages_button.setIcon(self._icon_style.standardIcon(QtWidgets.QStyle.SP_FileDialogListView))
            self.stages_button.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Rendering stages"))
            self.stages_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Show or hide the Shader stage outline"))
            self.stages_button.clicked[bool].connect(self._toggle_stage_outline)
            header.addWidget(self.stages_button)
            self.snapshots_button = QtWidgets.QToolButton(self.top)
            self.snapshots_button.setObjectName("breadShaderWorkspaceSnapshotsButton")
            self.snapshots_button.setCheckable(True)
            self.snapshots_button.setChecked(self._debug_snapshots_visible)
            self.snapshots_button.setText(QtCore.QCoreApplication.translate("ReplayLabProjects", "Debug snapshots"))
            self.snapshots_button.setToolTip(QtCore.QCoreApplication.translate("ReplayLabProjects", "Show or hide debug snapshots"))
            self.snapshots_button.clicked[bool].connect(self._toggle_debug_snapshots)
            header.addWidget(self.snapshots_button)
            self.flow_button = QtWidgets.QToolButton(self.top)
            self.flow_button.setObjectName(
                "replaylabShaderWorkspaceFlowButton")
            self.flow_button.setCheckable(True)
            self.flow_button.setChecked(self._flow_visible)
            self.flow_button.setToolButtonStyle(
                QtCore.Qt.ToolButtonTextBesideIcon)
            self.flow_button.setIcon(self._icon_style.standardIcon(
                QtWidgets.QStyle.SP_FileDialogDetailedView))
            self.flow_button.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Flow graph"))
            self.flow_button.clicked[bool].connect(self._toggle_flow_graph)
            header.addWidget(self.flow_button)
            header.addStretch(1)
            outer.addLayout(header)
            title = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader Workspace"), self.top)
            title_font = title.font()
            title_font.setBold(True)
            title.setFont(title_font)
            outer.addWidget(title)
            self.subtitle = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "Validated Shader source · Ctrl+S saves and automatically compiles/replaces"),
                self.top)
            # ``mid`` becomes nearly indistinguishable from the window color
            # in several dark themes. Use the theme's readable text role for
            # this small explanatory line.
            self.subtitle.setStyleSheet("color: palette(text);")
            self.subtitle.setWordWrap(True)
            outer.addWidget(self.subtitle)
            actions = QtWidgets.QHBoxLayout()
            self.save_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save"), self.top)
            self.save_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save the current Shader (Ctrl+S)"))
            self.save_button.clicked.connect(self._save_current)
            actions.addWidget(self.save_button)
            self.save_all_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Save All"), self.top)
            self.save_all_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save all modified Shader tabs"))
            self.save_all_button.clicked.connect(self._save_all)
            actions.addWidget(self.save_all_button)
            self.refresh_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Refresh"), self.top)
            self.refresh_button.clicked.connect(lambda _checked=False: self.refresh(True))
            actions.addWidget(self.refresh_button)
            self.convert_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Convert to HLSL"), self.top)
            self.convert_button.setObjectName("replaylabShaderWorkspaceConvertHLSLButton")
            self.convert_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "Convert the saved GLSL into a separate editable HLSL workspace and validate it. "
                "The original source is preserved."))
            self.convert_button.clicked.connect(self._convert_current_to_hlsl)
            self.convert_button.setVisible(False)
            actions.addWidget(self.convert_button)
            # The switch between what the capture recorded and what this workspace
            # compiled. An unchanged source is applied as already compiled; only a
            # changed one is compiled first.
            self.replace_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Use Compiled HLSL"), self.top)
            self.replace_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "Switch the captured Shader between its original bytes and this "
                "workspace's compiled HLSL. An unchanged source is applied without "
                "recompiling."))
            self.replace_button.clicked.connect(self._toggle_replacement)
            actions.addWidget(self.replace_button)
            self._switch_results = queue.Queue()
            self.switch_result_timer = QtCore.QTimer(self.top)
            self.switch_result_timer.setInterval(SAVE_RESULT_POLL_MS)
            self.switch_result_timer.timeout.connect(self._drain_switch_results)
            self.switch_result_timer.start()
            actions.addStretch(1)
            outer.addLayout(actions)

            self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal, self.top)
            self.file_tree = QtWidgets.QTreeWidget(self.main_splitter)
            self.file_tree.setObjectName("replaylabShaderWorkspaceTree")
            self.file_tree.setHeaderLabels((QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader files"),))
            self.file_tree.setMinimumWidth(210)
            self.file_tree.setUniformRowHeights(True)
            self.file_tree.itemClicked.connect(self._tree_item_clicked)
            self.file_tree.itemActivated.connect(self._tree_item_clicked)
            self.file_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.file_tree.customContextMenuRequested.connect(self._tree_context_menu)
            self.main_splitter.addWidget(self.file_tree)

            self.stage_panel = QtWidgets.QWidget(self.main_splitter)
            self.stage_panel.setObjectName("replaylabShaderWorkspaceStagePanel")
            self.stage_panel.setMinimumWidth(170)
            self.stage_panel.setMaximumWidth(380)
            stage_layout = QtWidgets.QVBoxLayout(self.stage_panel)
            stage_layout.setContentsMargins(0, 0, 0, 0)
            stage_layout.setSpacing(6)
            self.stage_status = QtWidgets.QLabel(self.stage_panel)
            self.stage_status.setObjectName("replaylabShaderWorkspaceStageStatus")
            self.stage_status.setWordWrap(True)
            self.stage_status.setContentsMargins(8, 6, 8, 4)
            stage_layout.addWidget(self.stage_status)
            self.stage_outline = QtWidgets.QListWidget(self.stage_panel)
            self.stage_outline.setObjectName("replaylabShaderWorkspaceStageOutline")
            self.stage_outline.setAccessibleName(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Rendering stages"))
            self.stage_outline.setMinimumWidth(170)
            self.stage_outline.setMaximumWidth(380)
            self.stage_outline.setSpacing(3)
            self.stage_outline.setMouseTracking(True)
            self.stage_outline.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            self.stage_outline.setItemDelegate(_ShaderStageDelegate(self.stage_outline))
            self.stage_outline.itemClicked.connect(self._stage_activated)
            self.stage_outline.itemActivated.connect(self._stage_activated)
            self.stage_outline.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.stage_outline.customContextMenuRequested.connect(self._stage_context_menu)
            stage_layout.addWidget(self.stage_outline, 1)
            self.main_splitter.addWidget(self.stage_panel)

            self.debug_snapshots = _shader_debug_snapshots.ShaderDebugSnapshots(self, self.main_splitter)
            self.main_splitter.addWidget(self.debug_snapshots)

            right = QtWidgets.QWidget(self.main_splitter)
            right_layout = QtWidgets.QVBoxLayout(right)
            right_layout.setContentsMargins(0, 0, 0, 0)
            self.tab_bar = _DocumentTabBar(right)
            self.tab_bar.currentChanged.connect(self._tab_changed)
            self.tab_bar.tabMoved.connect(self._tab_moved)
            self.tab_bar.pinRequested.connect(self._toggle_pin)
            self.tab_bar.closeDocumentRequested.connect(self._close_document)
            self.tab_bar.closeOthersRequested.connect(self._close_other_documents)
            right_layout.addWidget(self.tab_bar)

            self.visual_debug = _shader_visual_debug.ShaderVisualDebugController(
                self, visual_debug_operation, right_layout)
            self.editor_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical, right)
            self.editor_stack = QtWidgets.QStackedWidget(self.editor_splitter)
            empty = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "No Shader source is open. Open or decompile a Shader with Bread AI to create an editable working copy. Unvalidated edits are saved without applying them to replay."),
                self.editor_stack)
            empty.setAlignment(QtCore.Qt.AlignCenter)
            empty.setWordWrap(True)
            empty.setMargin(24)
            self.empty_page = empty
            self.editor_stack.addWidget(empty)
            self.editor_splitter.addWidget(self.editor_stack)

            problems_box = QtWidgets.QWidget(self.editor_splitter)
            problems_layout = QtWidgets.QVBoxLayout(problems_box)
            problems_layout.setContentsMargins(0, 0, 0, 0)
            problems_title = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Problems"), problems_box)
            problems_title_font = problems_title.font()
            problems_title_font.setBold(True)
            problems_title.setFont(problems_title_font)
            problems_layout.addWidget(problems_title)
            self.problems = QtWidgets.QTreeWidget(problems_box)
            self.problems.setHeaderLabels((
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Line"),
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Message"),
            ))
            self.problems.setRootIsDecorated(False)
            self.problems.setAlternatingRowColors(True)
            self.problems.header().setSectionResizeMode(
                0, QtWidgets.QHeaderView.ResizeToContents)
            self.problems.header().setSectionResizeMode(
                1, QtWidgets.QHeaderView.Stretch)
            self.problems.itemActivated.connect(self._problem_activated)
            self.problems.itemDoubleClicked.connect(self._problem_activated)
            problems_layout.addWidget(self.problems)
            self.editor_splitter.addWidget(problems_box)
            self.editor_splitter.setStretchFactor(0, 4)
            self.editor_splitter.setStretchFactor(1, 1)
            self.editor_splitter.setSizes((680, 150))
            right_layout.addWidget(self.editor_splitter)
            self.main_splitter.addWidget(right)

            self.flow_graph = _shader_flow_graph.ShaderFlowGraphPanel(
                self.main_splitter, navigation_resolver=self._resolve_flow_history)
            self.flow_graph.setObjectName(
                "replaylabShaderWorkspaceFlowGraph")
            self.flow_graph.setMinimumWidth(300)
            self.flow_graph.nodeActivated.connect(
                self._flow_graph_node_activated)
            self.flow_graph.subgraphRequested.connect(
                self._flow_graph_subgraph_requested)
            self.flow_graph.contextRequested.connect(self._flow_graph_context_requested)
            self.main_splitter.addWidget(self.flow_graph)
            self.main_splitter.setStretchFactor(0, 0)
            self.main_splitter.setStretchFactor(1, 0)
            self.main_splitter.setStretchFactor(2, 0)
            self.main_splitter.setStretchFactor(3, 3)
            self.main_splitter.setStretchFactor(4, 2)
            self.main_splitter.setCollapsible(0, True)
            self.main_splitter.setCollapsible(1, True)
            self.main_splitter.setCollapsible(2, True)
            self.main_splitter.setCollapsible(3, False)
            self.main_splitter.setCollapsible(4, True)
            self.main_splitter.setSizes((250, 235, 260, 820, 480))
            self.file_tree.setVisible(self._files_visible)
            self.stage_panel.setVisible(self._stages_visible)
            self.debug_snapshots.setVisible(self._debug_snapshots_visible)
            self.flow_graph.setVisible(self._flow_visible)
            self._update_file_button_tooltip()
            self._update_flow_button_tooltip()
            outer.addWidget(self.main_splitter, 1)
            self.stage_cursor_timer = QtCore.QTimer(self.top)
            self.stage_cursor_timer.setInterval(150)
            self.stage_cursor_timer.timeout.connect(self._sync_stage_cursor)
            self.stage_cursor_timer.start()
            self.stage_rebuild_timer = QtCore.QTimer(self.top)
            self.stage_rebuild_timer.setSingleShot(True)
            self.stage_rebuild_timer.setInterval(300)
            self.stage_rebuild_timer.timeout.connect(self._refresh_stage_outline)

            status_row = QtWidgets.QHBoxLayout()
            self.status_label = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Waiting for Shader source workspaces"), self.top)
            self.status_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            status_row.addWidget(self.status_label, 1)
            self.reload_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Reload"), self.top)
            self.reload_button.setVisible(False)
            self.reload_button.clicked.connect(self._reload_current)
            status_row.addWidget(self.reload_button)
            outer.addLayout(status_row)

            self.file_watcher = QtCore.QFileSystemWatcher(self.top)
            self.file_watcher.fileChanged.connect(self._filesystem_changed)
            self.file_watcher.directoryChanged.connect(self._filesystem_changed)
            self.change_timer = QtCore.QTimer(self.top)
            self.change_timer.setSingleShot(True)
            self.change_timer.setInterval(FILESYSTEM_COALESCE_MS)
            self.change_timer.timeout.connect(lambda: self.refresh(True))
            self.poll_timer = QtCore.QTimer(self.top)
            self.poll_timer.setInterval(POLL_INTERVAL_MS)
            self.poll_timer.timeout.connect(self.refresh)
            self.poll_timer.start()
            self.save_result_timer = QtCore.QTimer(self.top)
            self.save_result_timer.setInterval(SAVE_RESULT_POLL_MS)
            self.save_result_timer.timeout.connect(self._drain_save_results)
            self._save_worker = threading.Thread(
                target=_save_worker_loop,
                args=(self._save_client, self._save_tasks, self._save_results,
                      self._save_stop),
                name="ReplayLabShaderWorkspaceSave")
            self._save_worker.daemon = True
            self._save_worker.start()
            self.check_result_timer = QtCore.QTimer(self.top)
            self.check_result_timer.setInterval(SAVE_RESULT_POLL_MS)
            self.check_result_timer.timeout.connect(self._drain_check_results)
            self.check_result_timer.start()
            self.check_debounce_timer = QtCore.QTimer(self.top)
            self.check_debounce_timer.setSingleShot(True)
            self.check_debounce_timer.setInterval(LIVE_CHECK_DEBOUNCE_MS)
            self.check_debounce_timer.timeout.connect(self._request_live_check)
            self._check_worker = threading.Thread(
                target=_check_worker_loop,
                args=(self._save_client, self._check_tasks, self._check_results,
                      self._check_stop),
                name="ReplayLabShaderWorkspaceCheck")
            self._check_worker.daemon = True
            self._check_worker.start()
            self.analysis_debounce_timer = QtCore.QTimer(self.top)
            self.analysis_debounce_timer.setSingleShot(True)
            self.analysis_debounce_timer.setInterval(LOCAL_ANALYSIS_DEBOUNCE_MS)
            self.analysis_debounce_timer.timeout.connect(self._request_local_analysis)
            self._analysis_worker = threading.Thread(
                target=_analysis_worker_loop,
                args=(self._analysis_tasks, self._analysis_results, self._analysis_stop),
                name="ReplayLabShaderWorkspaceAnalysis")
            self._analysis_worker.daemon = True
            self._analysis_worker.start()

            self.close_shortcut = QtWidgets.QShortcut(
                QtGui.QKeySequence.Close, self.top)
            self.close_shortcut.activated.connect(self._close_current)
            self.save_all_shortcut = QtWidgets.QShortcut(
                QtGui.QKeySequence("Ctrl+Shift+S"), self.top)
            self.save_all_shortcut.activated.connect(self._save_all)
            self.files_shortcut = QtWidgets.QShortcut(
                QtGui.QKeySequence("Ctrl+B"), self.top)
            self.files_shortcut.activated.connect(self.files_button.click)
            self.refresh(True)

        def _load_state(self):
            raw = self._settings.value(SETTINGS_KEY, "")
            try:
                value = json.loads(str(raw or ""))
            except (TypeError, ValueError):
                return {}
            if not isinstance(value, dict):
                return {}
            clean = {}
            for field in ("open", "pinned"):
                values = value.get(field, [])
                clean[field] = [
                    str(item).lower() for item in values
                    if isinstance(item, str) and TOKEN_RE.fullmatch(item.lower())
                ][:MAX_OPEN_DOCUMENTS]
            active = str(value.get("active", "") or "").lower()
            clean["active"] = active if TOKEN_RE.fullmatch(active) else ""
            clean["filesVisible"] = value.get("filesVisible") is True
            clean["flowVisible"] = value.get("flowVisible") is not False
            clean["stagesVisible"] = value.get("stagesVisible") is not False
            clean["debugSnapshotsVisible"] = value.get("debugSnapshotsVisible") is True
            clean["capturePath"] = str(value.get("capturePath", "") or "")
            clean["tabChoicesExplicit"] = value.get("tabChoicesExplicit") is True
            clean["shaderLabels"] = {str(key): str(label)[:160] for key, label in
                (value.get("shaderLabels") or {}).items() if isinstance(label, str) and TOKEN_RE.fullmatch(str(key))}
            return clean

        def _save_state(self, force=False):
            if self.tab_bar is None:
                return
            binding = self._capture_binding
            saved = getattr(self, "_saved_tab_state", {})
            if not force and not binding and not self.tab_bar.count():
                # Closing a capture hides its tabs before panel shutdown.
                # That lifecycle projection is not a user's close-all choice.
                return
            opened = [
                str(self.tab_bar.tabData(index) or "")
                for index in range(self.tab_bar.count())
            ]
            opened = list(dict.fromkeys(opened + self._restore_open))
            active = ""
            if self.tab_bar.currentIndex() >= 0:
                active = str(self.tab_bar.tabData(self.tab_bar.currentIndex()) or "")
            value = {
                "open": opened,
                "pinned": [item for item in self._pinned
                           if item in self._documents or item in self._restore_open],
                "active": active,
                "capturePath": (binding or {}).get("capturePath", saved.get("capturePath", "")),
                # Legacy empty lists may have been written after capture-close
                # clearing. Only new explicit choices suppress the default tab.
                "tabChoicesExplicit": bool(force or opened or (
                    saved.get("tabChoicesExplicit") is True and (not binding or
                    _normal_path(saved.get("capturePath", "")) == _normal_path(binding["capturePath"])))),
                "filesVisible": bool(self._files_visible),
                "flowVisible": bool(self._flow_visible),
                "stagesVisible": bool(self._stages_visible),
                "debugSnapshotsVisible": bool(self._debug_snapshots_visible),
                "shaderLabels": dict(self._shader_labels),
            }
            self._settings.setValue(
                SETTINGS_KEY, json.dumps(value, separators=(",", ":")))
            self._saved_tab_state = value

        def _update_file_button_tooltip(self):
            if self._files_visible:
                tooltip = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Hide Shader files (Ctrl+B)")
            else:
                tooltip = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Show Shader files (Ctrl+B)")
            self.files_button.setToolTip(tooltip)

        def _toggle_file_tree(self, visible):
            self._files_visible = bool(visible)
            self.file_tree.setVisible(self._files_visible)
            self.files_button.setChecked(self._files_visible)
            self._update_file_button_tooltip()
            if self._files_visible:
                sizes = self.main_splitter.sizes()
                if sizes and sizes[0] < 180:
                    sizes[0] = 250
                    self.main_splitter.setSizes(sizes)
            self._save_state()

        def _update_flow_button_tooltip(self):
            if self._flow_visible:
                tooltip = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Hide flow graph")
            else:
                tooltip = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Show flow graph")
            self.flow_button.setToolTip(tooltip)

        def _toggle_debug_snapshots(self, visible):
            self._debug_snapshots_visible = bool(visible)
            self.debug_snapshots.setVisible(self._debug_snapshots_visible)
            self.snapshots_button.setChecked(self._debug_snapshots_visible)
            if self._debug_snapshots_visible:
                sizes = self.main_splitter.sizes()
                if len(sizes) >= 5 and sizes[2] < 210:
                    sizes[2] = 260
                    self.main_splitter.setSizes(sizes)
                self.debug_snapshots.refresh(True)
            self._save_state()

        def _toggle_flow_graph(self, visible):
            self._flow_visible = bool(visible)
            self.flow_graph.setVisible(self._flow_visible)
            self.flow_button.setChecked(self._flow_visible)
            self._update_flow_button_tooltip()
            if self._flow_visible:
                sizes = self.main_splitter.sizes()
                if len(sizes) >= 5 and sizes[4] < 260:
                    sizes[4] = 420
                    self.main_splitter.setSizes(sizes)
            self._save_state()

        def _toggle_stage_outline(self, visible):
            self._stages_visible = bool(visible)
            self.stage_panel.setVisible(self._stages_visible)
            self.stages_button.setChecked(self._stages_visible)
            if self._stages_visible:
                sizes = self.main_splitter.sizes()
                if len(sizes) >= 4 and sizes[1] < 170:
                    sizes[1] = 235
                    self.main_splitter.setSizes(sizes)
                self._refresh_stage_outline()
                self._sync_stage_cursor()
            self._save_state()

        def _refresh_stage_outline(self):
            document = self._current_document()
            if (self._alive and not self._shutting_down and self._stages_visible
                    and document and document.get('available')):
                self._request_source_flow(document)

        def _sync_stage_cursor(self):
            if not self._alive or self._shutting_down or not self._stages_visible:
                return
            document = self._current_document()
            valid = (document and document.get('available')
                     and document.get('captureBinding') == self._current_capture_binding()
                     and document.get('sourceFlowKey') == self._source_flow_key(document))
            self._update_stage_outline_status(document, valid)
            signature = document.get('sourceFlowKey') if valid else None
            if signature != self._stage_outline_signature:
                self._stage_outline_signature = signature
                self.stage_outline.clear()
                self._stage_items = {}
                for stage in document.get('stageOutline', []) if valid else []:
                    item = QtWidgets.QListWidgetItem(stage['title'], self.stage_outline)
                    item.setData(QtCore.Qt.UserRole, stage['id'])
                    item.setData(QtCore.Qt.UserRole + 1, stage)
                    contexts = list(dict.fromkeys(context['expression']
                        for span in stage.get('spans', []) + stage.get('callSpans', [])
                        for context in span.get('contexts', []) if context.get('expression')))
                    item.setToolTip('\n'.join([stage.get('description') or stage['functionName']] + contexts))
                    self._stage_items[stage['id']] = item
            if not valid or not document.get('hierarchy'):
                return
            line = int(document['editor'].GetCursorLine())
            cursor_key = (signature, line, document.get('activeStage'))
            if document.get('stageCursorKey') == cursor_key:
                return
            selected = _shader_stage_outline.stage_for_line(
                document['hierarchy'], document.get('stageOutline', []), line,
                document.get('activeStage'))
            document['activeStage'] = selected
            document['stageCursorKey'] = (signature, line, selected)
            item = self._stage_items.get(selected)
            self.stage_outline.setCurrentItem(item)
            if item is not None:
                self.stage_outline.scrollToItem(item, QtWidgets.QAbstractItemView.EnsureVisible)

        def _update_stage_outline_status(self, document, valid):
            state = (document or {}).get('stageOutlineState') or {}
            if not valid:
                if (document and document.get('available') and
                        document.get('captureBinding') == self._current_capture_binding()):
                    text = QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Updating calculation outline…")
                else:
                    text = QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "No calculation outline")
            elif state.get('state') == 'reviewed' and state.get('complete') is True:
                text = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Calculation outline reviewed")
            elif state.get('state') == 'stale':
                text = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Source changed · review the calculation outline")
            elif state.get('source') in ('source-derived', 'legacy-annotation'):
                text = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Temporary navigation · independent calculation outline not generated")
            elif state.get('state') == 'provisional':
                text = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Calculation outline needs review")
            else:
                text = QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "No calculation outline")
            if self.stage_status.text() != text:
                self.stage_status.setText(text)
            self.stage_status.setProperty('breadOutlineState', state.get('state') if valid else 'pending')

        def _stage_activated(self, item):
            document = self._current_document()
            if (not document or not document.get('available')
                    or document.get('captureBinding') != self._current_capture_binding()
                    or document.get('sourceFlowKey') != self._source_flow_key(document)):
                return
            stage = next((row for row in document.get('stageOutline', [])
                          if row['id'] == item.data(QtCore.Qt.UserRole)), None)
            hierarchy = document.get('hierarchy')
            if stage is None or hierarchy is None:
                return
            span = _shader_stage_outline.navigation_span(
                hierarchy, stage, int(document['editor'].GetCursorLine()))
            function = hierarchy.functions.get(span['functionId']) if span else None
            if function is None:
                return
            self._toggle_flow_graph(True)
            self._open_function_graph(document, hierarchy, function['id'], direct=True)
            document['activeStage'] = stage['id']
            # Resolve from this accepted source revision. A line can contain
            # several declarations; column one could belong to another one.
            token = next((hierarchy.tokens[index] for index in range(
                function['declaration'], function['end'] + 1)
                if hierarchy.source.count('\n', 0, hierarchy.tokens[index].start()) + 1 >=
                span['startLine']), hierarchy.tokens[function['declaration']])
            line = hierarchy.source.count('\n', 0, token.start()) + 1
            position = token.start()
            if span.get('startColumn'):
                line = span['startLine']
                position = hierarchy.lines[line - 1] + span['startColumn'] - 1
            column = len(hierarchy.source[hierarchy.lines[line - 1]:position].expandtabs(4)) + 1
            document['editor'].GoToLine(line, column)
            self._sync_stage_cursor()

        def _stage_context_menu(self, position):
            item = self.stage_outline.itemAt(position)
            document = self._current_document()
            if item is None or not document or not document.get('available'):
                return
            stage_id = str(item.data(QtCore.Qt.UserRole) or '')
            token = document.get('documentToken')
            revision = document.get('sourceFlowKey')
            menu = QtWidgets.QMenu(self.stage_outline)
            action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Add to Bread AI chat"))
            action.triggered.connect(lambda unused=False: self._add_stage_to_chat(stage_id, token, revision))
            menu.exec_(self.stage_outline.viewport().mapToGlobal(position))

        def _add_stage_to_chat(self, stage_id, token, revision):
            document = self._current_document()
            if (not document or not document.get('available')
                    or document.get('documentToken') != token
                    or document.get('captureBinding') != self._current_capture_binding()
                    or revision != self._source_flow_key(document)
                    or document.get('sourceFlowKey') != revision):
                return
            stage = next((row for row in document.get('stageOutline', []) if row['id'] == stage_id), None)
            if stage is not None:
                self._add_calculation_to_chat(document, dict(stage, kind='shader-calculation'))

        def _add_calculation_to_chat(self, document, scope):
            if (not document.get('available') or
                    document.get('captureBinding') != self._current_capture_binding()):
                return
            source_text = str(document['editor'].GetText())
            context, facts = _calculation_attachment(document, scope, source_text,
                int(self.ctx.CurEvent()), int(self.ctx.CurSelectedEvent()),
                self._workspace_event_ids.get(document['workspaceId'], ()))
            _retain_attachment_source(context, facts, source_text)
            title = str(scope.get('title') or scope.get('functionName') or '')
            source = _format_message(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader Workspace — %1"), title)
            self.ctx.AddTextToAIAssistant(title, json.dumps(facts, ensure_ascii=False), source,
                json.dumps(context, ensure_ascii=False, separators=(",", ":")))

        def _flow_graph_context_requested(self, payload):
            document = self._current_document()
            if (not isinstance(payload, dict) or not document or not document.get('available')
                    or document.get('captureBinding') != self._current_capture_binding()):
                return
            node, graph = payload.get('node') or {}, payload.get('graph') or {}
            navigation = node.get('navigation') or {}
            anchor = document.get('flowAnchors', {}).get(str(navigation.get('anchorId') or ''))
            if anchor is None:
                self._flow_graph_target_unavailable()
                return
            hierarchy = self._source_hierarchy(document)
            function = self._bookmarked_function(document, hierarchy, anchor['functionMarker'])
            if function is None:
                self._flow_graph_target_unavailable()
                return
            group_id = anchor.get('helperGroupId')
            if group_id:
                try:
                    detail = hierarchy.function_graph(function['id'], functions_only=True,
                                                       helper_group_id=group_id)
                except ValueError:
                    self._flow_graph_target_unavailable()
                    return
                spans, seen = [], set()
                for member in detail.get('nodes', []):
                    if member.get('kind') != 'function-call':
                        continue
                    spans.append(dict(member['sourceRange'], functionId=function['id'],
                                      functionName=function['name']))
                    target = hierarchy.functions.get(member.get('functionId'))
                    if target and target['id'] not in seen:
                        seen.add(target['id'])
                        first, last = self._function_line_span(hierarchy, target)
                        spans.append({'functionId': target['id'], 'functionName': target['name'],
                                      'startLine': first, 'endLine': last})
                self._add_calculation_to_chat(document, {'id': group_id, 'kind': 'shader-helper-group',
                    'title': str(node.get('label') or function['name']), 'spans': spans,
                    'functionId': function['id'], 'functionName': function['name'],
                    'provenance': 'current-source', 'graph': {'graphId': detail.get('graphId') or detail.get('graphKey'),
                    'nodeId': node.get('id'), 'functionId': function['id'],
                    'sourceSha256': hierarchy.source_sha256}})
                return
            # A call node represents its current callee. Resolve it again from
            # the tracked call site so a renamed/replaced function cannot attach
            # an unrelated definition from a stale graph.
            call_span = None
            if anchor.get('expansionKind') == 'call':
                line = document['editor'].GetLineAnchorLine(anchor['marker'])
                current = hierarchy.function_graph(function['id'], functions_only=True)
                matches = [item for item in current.get('nodes', [])
                           if item.get('kind') == 'function-call'
                           and item.get('sourceRange', {}).get('startLine', 0) <= line
                           <= item.get('sourceRange', {}).get('endLine', -1)] if line else []
                if len(matches) != 1 or not matches[0].get('functionId'):
                    self._flow_graph_target_unavailable()
                    return
                call_span = dict(matches[0]['sourceRange'], functionId=function['id'], functionName=function['name'])
                function = hierarchy.functions.get(matches[0]['functionId'])
                if function is None:
                    self._flow_graph_target_unavailable()
                    return
            first, last = self._function_line_span(hierarchy, function)
            spans = [{'functionId': function['id'], 'functionName': function['name'],
                      'startLine': first, 'endLine': last}]
            if call_span:
                spans.append(call_span)
            elif node.get('kind') in ('flow-control', 'function-group', 'computation'):
                line = document['editor'].GetLineAnchorLine(anchor['marker'])
                current = hierarchy.function_graph(function['id'], functions_only=True)
                matches = [item for item in current.get('nodes', [])
                           if item.get('kind') not in ('function-summary', 'entry-point')
                           and item.get('sourceRange', {}).get('startLine', 0) <= line
                           <= item.get('sourceRange', {}).get('endLine', -1)] if line else []
                if len(matches) != 1:
                    self._flow_graph_target_unavailable()
                    return
                spans.insert(0, dict(matches[0]['sourceRange'], functionId=function['id'],
                                     functionName=function['name']))
            scope = {'id': function['id'], 'kind': 'shader-function', 'title': function['name'],
                     'functionId': function['id'], 'functionName': function['name'],
                     'description': hierarchy.leading_comment(function['declaration'] - 1),
                     'provenance': 'current-source', 'spans': spans,
                     'graph': {'graphId': graph.get('graphId') or graph.get('graphKey'), 'nodeId': node.get('id'),
                               'functionId': function['id'], 'sourceSha256': hierarchy.source_sha256}}
            self._add_calculation_to_chat(document, scope)

        def _add_editor_selection_to_chat(
                self, text, source_path, start_line, end_line,
                workspace_id=""):
            title = QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader selection")
            source_name = (
                os.path.basename(source_path) or
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Shader Workspace"))
            source = _format_message(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Shader Workspace — %1 (lines %2–%3)"),
                source_name, start_line, end_line)

            document = self._documents.get(str(workspace_id or ""))
            if document is None or document.get("captureBinding") != self._current_capture_binding():
                return
            editor = document.get("editor")
            editor_widget = document.get("editorWidget")
            # The native editor records columns alongside its legacy four-argument callback.
            source_range = editor_widget.property("breadSelectionRange") if editor_widget else None
            source_text = str(editor.GetText()) if editor else None
            if source_text is None:
                return
            context, facts = _selection_attachment(
                document, text, source_path, start_line, end_line,
                int(self.ctx.CurEvent()), int(self.ctx.CurSelectedEvent()),
                self._workspace_event_ids.get(workspace_id, ()), source_range,
                source_text)
            _retain_attachment_source(context, facts, source_text)
            # i18n-ignore: These are structured reference facts, not UI prose or instructions.
            text = json.dumps(facts, ensure_ascii=False, indent=2)
            self.ctx.AddTextToAIAssistant(
                title, text, source,
                json.dumps(context, ensure_ascii=False, separators=(",", ":")))

        def _request_local_analysis(self):
            """Queue the instant local analysis of the current buffer."""
            if not self._alive or self.top is None:
                return
            document = self._current_document()
            if document is None or not document.get("available") or not document.get("dirty"):
                return
            if os.path.splitext(str(document.get("sourcePath") or ""))[1].lower() not in (
                    ".hlsl", ".hlsli", ".fx", ".fxh"):
                # This local parser knows HLSL only. GLSL retains its native
                # language services; do not paint HLSL false positives on it.
                return
            try:
                source_text = document["editor"].GetText()
            except Exception:
                return
            self._analysis_tasks.put({
                "workspaceId": document.get("workspaceId"),
                "documentToken": document["documentToken"],
                "textRevision": int(document.get("textRevision", 0)),
                "sourceText": source_text,
            })

        def _drain_analysis_results(self):
            if not self._alive or self.top is None:
                return
            updated = False
            while True:
                try:
                    outcome = self._analysis_results.get_nowait()
                except queue.Empty:
                    break
                task = outcome.get("task") or {}
                workspace_id = str(task.get("workspaceId", "") or "")
                document = self._documents.get(workspace_id)
                if (document is None or document.get("documentToken") != task.get("documentToken") or
                        int(document.get("textRevision", 0)) != int(task.get("textRevision", 0))):
                    continue
                self._local_analysis[workspace_id] = {
                    "textRevision": int(task.get("textRevision", 0)),
                    "diagnostics": list(outcome.get("diagnostics") or []),
                }
                updated = True
            if updated and self._alive:
                self._refresh_current_view()

        def _request_live_check(self):
            """Queue a compile-check of the current unsaved buffer, if eligible."""
            if not self._alive or self.top is None:
                return
            if getattr(self, "visual_debug", None) is not None and (
                    self.visual_debug.request or self.visual_debug.busy):
                return
            document = self._current_document()
            if document is None or not document.get("available"):
                return
            if (not document.get("dirty") or document.get("conflict") or
                    document.get("saveInFlight") or document.get("commitUnknown")):
                return
            workspace_id = document.get("workspaceId")
            workspace = self._document_model(workspace_id)
            if not workspace or workspace.get("autoApplyReady") is not True:
                return
            live_edit = workspace.get("liveEdit") or {}
            live_edit_id = str(live_edit.get("liveEditId", "") or "")
            authorization_id = str(document.get("authorizationId", "") or "")
            if not live_edit_id or not authorization_id:
                return
            try:
                source_text = self._document_source_text(document)
            except Exception:
                return
            if not source_text:
                return
            self._next_check_token += 1
            self._check_tasks.put({
                "checkToken": self._next_check_token,
                "workspaceId": workspace_id,
                "documentToken": document["documentToken"],
                "liveEditId": live_edit_id,
                "authorizationId": authorization_id,
                "sourceText": source_text,
                "textRevision": int(document.get("textRevision", 0)),
            })

        def _log_live_check_once(self, message):
            """Print each distinct live-check failure once (developer console)."""
            text = str(message or "")[:512]
            if text in self._live_check_logged:
                return
            self._live_check_logged.add(text)
            # i18n-ignore: developer-only diagnostic printed by the bridge.
            print("[Bread] Shader Workspace %s" % text)

        def _request_live_usage(self, workspaces):
            """Ask RenderDoc which events bind each shader of the open capture.

            Runs GetUsage on the replay thread (cheap: a frame-record lookup),
            never touching Qt there; results come back through a queue drained
            on the UI timer. Cached per capture session and resource until the
            capture changes, so the 2 s snapshot poll does not re-query.
            """
            if not self._alive or self.top is None:
                return
            binding = self._current_capture_binding()
            if not binding or binding != self._capture_binding:
                return
            generation = self._usage_generation
            capture_key = _normal_path(str(binding.get('capturePath', '') or ''))
            if not capture_key:
                return
            wanted = []
            wanted_keys = set()
            changed = False
            for workspace in workspaces or []:
                workspace_id = str(workspace.get('workspaceId', '') or '')
                events = _workspace_live_event_ids(workspace, binding, self._usage_cache,
                    self._workspace_event_ids.get(workspace_id, ()))
                if events is not None and self._workspace_event_ids.get(workspace_id) != events:
                    self._workspace_event_ids[workspace_id] = events
                    changed = True
                for key in _workspace_usage_keys(workspace, binding):
                    if key in self._usage_cache or key in self._usage_pending or key in wanted_keys:
                        continue
                    wanted.append((key, workspace_id, key[3]))
                    wanted_keys.add(key)
            if changed:
                self._rerender_tree_keep_selection()
            if not wanted:
                return
            try:
                by_int = {}
                for resource in self.ctx.GetResources():
                    rid = getattr(resource, 'resourceId', None)
                    try:
                        by_int[int(rid)] = rid
                    except Exception:
                        continue
            except Exception:
                return
            results = self._usage_results
            for key, workspace_id, resource in wanted:
                rid = by_int.get(resource)
                if rid is None:
                    continue
                self._usage_pending.add(key)

                def _query(ctrl, key=key, workspace_id=workspace_id, rid=rid):
                    # The replay thread reads only captured/plain Python state;
                    # the UI drain checks the live provider before publishing.
                    if (not self._alive or generation != self._usage_generation or
                            binding != self._capture_binding):
                        return
                    events = []
                    try:
                        seen = set()
                        for use in ctrl.GetUsage(rid):
                            event = int(getattr(use, 'eventId', 0) or 0)
                            if 0 < event <= 0xFFFFFFFF and event not in seen:
                                seen.add(event)
                                events.append(event)
                        events.sort()
                    except Exception:
                        events = None
                    if (self._alive and generation == self._usage_generation and
                            binding == self._capture_binding):
                        results.put((generation, binding, key, workspace_id, events))

                try:
                    replay = self.ctx.Replay()
                    # Use a distinct tag for each lookup so ReplayManager does not coalesce them.
                    replay.AsyncInvoke(_query, 'replaylab-shader-usage-%s-%s-%s' %
                                       (key[1], generation, resource))
                except Exception:
                    self._usage_pending.discard(key)

        def _drain_usage_results(self):
            if not self._alive or self.top is None:
                return
            binding = self._current_capture_binding()
            changed = False
            while True:
                try:
                    generation, requested_binding, key, workspace_id, events = self._usage_results.get_nowait()
                except queue.Empty:
                    break
                if (generation != self._usage_generation or not binding or
                        requested_binding != binding or binding != self._capture_binding):
                    continue
                self._usage_pending.discard(key)
                targets = [(target_id, target) for target_id, target in self._workspaces.items()
                           if key in _workspace_usage_keys(target, binding)]
                if not targets:
                    continue
                if events is None:
                    continue  # query failed: keep whatever the catalog said
                self._usage_cache[key] = list(events)
                for target_id, target in targets:
                    combined = _workspace_live_event_ids(target, binding, self._usage_cache,
                        self._workspace_event_ids.get(target_id, ()))
                    if self._workspace_event_ids.get(target_id) != combined:
                        self._workspace_event_ids[target_id] = combined
                        changed = True
            if changed:
                self._rerender_tree_keep_selection()

        def _rerender_tree_keep_selection(self):
            current = self._current_workspace_id()
            self._render_tree(list(self._workspaces.values()))
            # Rebuilding the tree drops the selection; restore the open tab's item.
            self._tree_signature = None
            item = self._tree_items.get(current) if current else None
            if item is not None:
                self.file_tree.setCurrentItem(item)

        def _drain_check_results(self):
            """Apply live compile-check diagnostics that still match the buffer."""
            self._drain_usage_results()
            self._drain_analysis_results()
            if not self._alive or self.top is None:
                return
            updated = False
            while True:
                try:
                    outcome = self._check_results.get_nowait()
                except queue.Empty:
                    break
                if outcome.get("kind") != "checked":
                    self._log_live_check_once(
                        "live check error: %s" % str(outcome.get("error", "") or ""))
                    continue
                result = outcome.get("result") or {}
                if result.get("ok") is None:
                    self._log_live_check_once(
                        "live check unavailable: %s" % str(result.get("reason", "") or ""))
                    continue
                task = outcome.get("task") or {}
                workspace_id = str(task.get("workspaceId", "") or "")
                document = self._documents.get(workspace_id)
                if (document is None or document.get("documentToken") !=
                        task.get("documentToken")):
                    continue
                if int(document.get("textRevision", 0)) != int(
                        task.get("textRevision", 0)):
                    continue
                self._live_check[workspace_id] = {
                    "textRevision": int(task.get("textRevision", 0)),
                    "diagnostics": _clamp_diagnostics_to_text(
                        list(result.get("diagnostics") or []), task.get("sourceText", "")),
                    "ok": bool(result.get("ok")),
                }
                updated = True
            if updated and self._alive:
                self._refresh_current_view()

        def _current_problems(self, document, workspace):
            """Choose which diagnostics underline the open document.

            A saved, stable document shows the compiler's post-save
            diagnostics. While the buffer is dirty those saved locations no
            longer match the text, so a fresh live compile-check of the
            unsaved buffer is shown instead -- but only when it describes the
            current edit revision, so a stale squiggle is never pinned to
            code that has since moved.
            """
            if document.get("conflict"):
                return []
            if document.get("dirty"):
                revision = int(document.get("textRevision", 0))
                # The exact current compiler result is authoritative. Local
                # identifier heuristics cannot know compiler-defined macros or
                # every dialect extension and must not survive a successful
                # check until the next save.
                live = self._live_check.get(document.get("workspaceId"))
                if live is not None and live.get("textRevision") == revision:
                    return list(live.get("diagnostics") or [])
                local = self._local_analysis.get(document.get("workspaceId"))
                if local is not None and local.get("textRevision") == revision:
                    return list(local.get("diagnostics") or [])
                return []
            if document.get("saveInFlight"):
                return []
            return self._workspace_problems(
                workspace or {}, document.get("rawSha256", ""))

        def _stop_save_worker(
                self, timeout_seconds=SAVE_WORKER_SHUTDOWN_TIMEOUT_SECONDS):
            if not self._check_worker_stopped:
                self._check_worker_stopped = True
                self._check_stop.set()
                self._check_tasks.put(None)
                self._analysis_stop.set()
                self._analysis_tasks.put(None)
            check_worker = self._check_worker
            if (check_worker is not None and
                    check_worker is not threading.current_thread()):
                check_worker.join(min(2.0, max(0.0, float(timeout_seconds))))
                if not check_worker.is_alive():
                    self._check_worker = None
            if self._save_worker_stopped:
                worker = self._save_worker
            else:
                self._save_worker_stopped = True
                self._save_stop.set()
                self._save_tasks.put(None)
                worker = self._save_worker
            stopped = _join_save_worker(
                worker, timeout_seconds, self._save_transport_pending)
            if stopped:
                self._save_worker = None
            return stopped

        def _destroyed(self, _object=None):
            if not self._alive:
                return
            if getattr(self, "visual_debug", None) is not None:
                self.visual_debug.shutdown()
            self._alive = False
            self._cancel_snapshot_refresh()
            if not self._shutting_down:
                try:
                    self._stop_save_worker()
                except Exception as exc:
                    # i18n-ignore: developer-only shutdown diagnostic.
                    print("[Bread] Shader Workspace shutdown error: %s" %
                          str(exc))
            try:
                self.poll_timer.stop()
                self.change_timer.stop()
                self.save_result_timer.stop()
                if getattr(self, "check_result_timer", None) is not None:
                    self.check_result_timer.stop()
                if getattr(self, "check_debounce_timer", None) is not None:
                    self.check_debounce_timer.stop()
                if getattr(self, "analysis_debounce_timer", None) is not None:
                    self.analysis_debounce_timer.stop()
                for name in ('stage_cursor_timer', 'stage_rebuild_timer'):
                    timer = getattr(self, name, None)
                    if timer is not None:
                        timer.stop()
            except Exception:
                pass
            callback = self.closed_callback
            self.closed_callback = None
            self.top = None
            if callback:
                callback()

        def _release_native_editors(self):
            if getattr(self, "visual_debug", None) is not None:
                self.visual_debug.shutdown()
            # QWidget wrappers returned by Widget() are non-owning. Detach them from the stack,
            # release Python callbacks, then ask CaptureContext to destroy the native owner.
            for document in self._documents.values():
                editor = document.get("editor")
                editor_widget = document.get("editorWidget")
                if editor is None:
                    continue
                if getattr(self, "debug_snapshots", None) is not None:
                    self.debug_snapshots.release_document(document,
                        document.get("baselineEditorText") if document.get("dirty") else None)
                outline_anchors = document.pop("flowOutlineAnchors", None)
                if outline_anchors is not None:
                    outline_anchors.clear()
                if editor_widget is not None:
                    self.editor_stack.removeWidget(editor_widget)
                editor.ClearCallbacks()
                self.ctx.DestroyShaderCodeEditor(editor)
                document["editor"] = None
                document["editorWidget"] = None
            self._native_editors_released = True
            self._live_check.clear()
            self._local_analysis.clear()

        def _confirm_panel_close(self):
            if self._native_editors_released:
                return True
            if (not self._shutting_down and getattr(self, "visual_debug", None) is not None and
                    (self.visual_debug.request or self.visual_debug.session_id)):
                self.visual_debug.stop_then(lambda: self.top.close() if self.top is not None else None)
                return False
            if self._shutting_down:
                self._release_native_editors()
                return True
            for workspace_id, document in list(self._documents.items()):
                if document and document.get("dirty"):
                    if not self._confirm_close_document(document):
                        return False
            self._save_state()
            try:
                # ToolWindowManager can directly delete an accepted dock tab. Prove the Python
                # worker is quiescent before allowing that irreversible ownership transition.
                self._stop_save_worker()
                self._cancel_snapshot_refresh()
            except Exception as exc:
                # i18n-ignore: developer-only shutdown diagnostic; the translated status below is
                # the user-visible close refusal.
                print("[Bread] Shader Workspace close deferred: %s" % str(exc))
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "The Shader Workspace is still finishing a save. Wait for it to complete, then close the workspace again."))
                return False
            self._release_native_editors()
            return True

        def show(self):
            if not self._alive or self.top is None:
                raise RuntimeError("Bread Shader Workspace has already been closed")
            if not self._docked:
                self.ctx.AddDockWindow(
                    self.top, self.qrd.DockReference.MainToolArea, None, 0.72)
                self._docked = True
            self.ctx.RaiseDockWindow(self.top)

        def shutdown(self):
            if not self._alive:
                return
            self._shutting_down = True
            self._save_state()
            try:
                self._stop_save_worker()
                self._cancel_snapshot_refresh()
            except Exception:
                # Keep the live widget and its bridge-owned controller reachable so a later stop()
                # can retry after the bounded Host request finishes. Closing here would trigger
                # destroyed/closed_callback and irreversibly lose the only worker reference.
                self._shutting_down = False
                raise
            try:
                self.poll_timer.stop()
                self.change_timer.stop()
                self.save_result_timer.stop()
                if getattr(self, "check_result_timer", None) is not None:
                    self.check_result_timer.stop()
                if getattr(self, "check_debounce_timer", None) is not None:
                    self.check_debounce_timer.stop()
                if getattr(self, "analysis_debounce_timer", None) is not None:
                    self.analysis_debounce_timer.stop()
                for name in ('stage_cursor_timer', 'stage_rebuild_timer'):
                    timer = getattr(self, name, None)
                    if timer is not None:
                        timer.stop()
                subscriptions = (
                    self.file_watcher.files() + self.file_watcher.directories())
                if subscriptions:
                    self.file_watcher.removePaths(subscriptions)
            except Exception:
                pass
            widget = self.top
            if widget is not None:
                widget.close()
                widget.deleteLater()

        def _reset_watched_paths(self, paths):
            # Existence/classification is performed by the snapshot worker. Do
            # not repeatedly remove/re-add every subscription on audit writes.
            current = self.file_watcher.files() + self.file_watcher.directories()
            removed = list(set(current) - set(paths))
            added = list(set(paths) - set(current))
            if removed:
                self.file_watcher.removePaths(removed)
            if added:
                self.file_watcher.addPaths(added)

        def _filesystem_changed(self, _path):
            # Audit/heartbeat writes request a subsequent snapshot, but do not
            # invalidate every in-flight read: that would starve a busy shader.
            # Source replacement and authority changes invalidate publication.
            sensitive = {self.registry_path, self.project_selection_path}
            for workspace in self._workspaces.values():
                source_path = workspace.get("sourcePath", "")
                sensitive.update((source_path, os.path.dirname(source_path)))
            if _path in sensitive:
                self._snapshot_generation += 1
                self._source_documents = {}
            if not self.change_timer.isActive():
                self.change_timer.start(FILESYSTEM_COALESCE_MS)

        def _document_model(self, document_id):
            return self._workspaces.get(document_id)

        def _current_capture_binding(self):
            try:
                value = (self._capture_binding_provider()
                         if self._capture_binding_provider is not None else None)
                return _shader_console_protocol.normalize_capture_binding(value)
            except Exception:
                return None

        def _can_resume_document(self, document, workspace, binding):
            previous = document.get('captureBinding') or {}
            if previous == binding:
                return True
            if document.get('saveInFlight') or document.get('commitUnknown'):
                return False
            # Reopening the same capture may restore its draft only after a fresh Host
            # authorization matched this managed source. A different RDC never inherits it.
            return bool(binding and previous.get('capturePath') == binding.get('capturePath') and
                        _normal_path(document.get('sourcePath', '')) ==
                        _normal_path(workspace.get('sourcePath', '')))

        def capture_changed(self, binding=None):
            binding = _shader_console_protocol.normalize_capture_binding(binding)
            if binding == self._capture_binding:
                return
            if getattr(self, "visual_debug", None) is not None and (
                    self.visual_debug.request or self.visual_debug.session_id):
                self.visual_debug.stop()
            if self._capture_binding is not None:
                # Persist real choices while the old native tabs still exist.
                self._save_state()
                self._restore_open = list(self._saved_tab_state.get("open", []))
                self._restore_active = str(self._saved_tab_state.get("active", "") or "")
            saved_path = (getattr(self, "_saved_tab_state", {}) or {}).get("capturePath", "")
            if binding and saved_path and _normal_path(saved_path) != _normal_path(binding["capturePath"]):
                # Another RDC never inherits pending tab restoration. Retained
                # dirty documents remain protected by _can_resume_document.
                self._restore_open = []
                self._restore_active = ""
                self._restore_default_document = True
                self._closed_document_ids.clear()
            # Clean preview tabs can be retired. Edited drafts use the same unsaved-buffer
            # protection as live documents and must survive a capture change.
            for preview_id in list(self._organization_previews):
                self._discard_organization_preview(preview_id)
            self._snapshot_stop.set()
            if getattr(self, '_flow_request', None) is not None:
                self._flow_request['stop'].set()
            self._snapshot_generation += 1
            self._source_documents = {}
            self._pending_open = {}
            self._pending_reload = {}
            self._capture_binding = binding
            self._last_open_error = ""
            self._source_open_errors = {}
            self._usage_generation += 1
            self._usage_cache = {}
            self._usage_pending = set()
            self._workspace_event_ids = {}
            self._last_disk_signature = None
            self._snapshot_error_retry_at = 0.0
            self._snapshot_error_retry_delay = SNAPSHOT_ERROR_RETRY_MIN_SECONDS
            self._tree_signature = None
            self._flow_graph_signature = None
            self._restored = False
            self._workspaces = {}
            self._document_aliases = {}
            self._tree_items = {}
            self.file_tree.clear()
            # Keep unsaved buffers in memory, but exclude the previous capture from tabs,
            # graph, F12, completion and save actions. Do not discard a user's edits.
            self.tab_bar.blockSignals(True)
            try:
                while self.tab_bar.count():
                    self.tab_bar.removeTab(0)
                for document in self._documents.values():
                    document['available'] = False
                    document['saveQueued'] = False
                    document['editor'].SetReadOnly(True)
                    document['editor'].SetNavigationEnabled(False)
            finally:
                self.tab_bar.blockSignals(False)
            self.editor_stack.setCurrentWidget(self.empty_page)
            self.flow_graph.clear_graph(graph_key='shader-workspace-empty')
            self._refresh_current_view()

        def _cancel_snapshot_refresh(self):
            self._snapshot_generation += 1
            self._snapshot_stop.set()
            self._snapshot_running = False
            self._source_documents = {}
            self._snapshot_again = False
            if getattr(self, '_flow_request', None) is not None:
                self._flow_request["stop"].set()
                self._flow_request = None
            self._snapshot_executor.cancel_owner(self._snapshot_owner)
            if self._owns_snapshot_executor:
                self._snapshot_executor.shutdown(10000)

        def refresh(self, force=False, source_only=False):
            if not self._alive or self._shutting_down:
                return
            binding = self._current_capture_binding()
            if binding != self._capture_binding:
                self.capture_changed(binding)
                force = True
            if self._snapshot_running:
                self._snapshot_again = self._snapshot_again or bool(force)
                if source_only:
                    # A click takes priority over a read-only catalog scan.
                    # The same executor delivers cancellation before reading
                    # the exact authorized source; saves/replay are untouched.
                    self._snapshot_stop.set()
                return
            identities = dict((key, _document_read_identity(document))
                              for key, document in self._documents.items())
            retry_due = bool(
                self._snapshot_error_retry_at and
                time.monotonic() >= self._snapshot_error_retry_at)
            self._snapshot_stop = threading.Event()
            request = {
                "stop": self._snapshot_stop,
                "generation": self._snapshot_generation,
                "captureBinding": dict(binding) if binding else None,
                "registryPath": self.registry_path,
                "projectSelectionPath": self.project_selection_path,
                "watchedPaths": list(self._watched_paths),
                "previousSignature": self._last_disk_signature,
                "force": bool(force or retry_due or self._pending_open or self._pending_reload or any(
                    document.get("commitUnknown") for document in self._documents.values())),
                "documentIds": list(dict.fromkeys(
                    list(self._pending_open) + list(self._documents) + self._pinned +
                    self._restore_open)),
                "documentStates": identities,
                "sourcePreviews": dict((key, dict(value.get("opened") or {}))
                                       for key, value in self._organization_previews.items()),
            }
            if source_only:
                request['knownSources'] = [dict((name, copy.deepcopy(workspace.get(name))) for name in (
                    'workspaceId', 'workspacePath', 'authorizationId', 'sourcePath', 'sourceSession',
                    'liveEdit', 'watch')) for key in self._pending_open
                    for workspace in [self._workspaces.get(key) or {}]
                    if workspace.get('workspacePath') and workspace.get('authorizationId') and
                    workspace.get('documentKind') not in ('organization-preview', 'capture-source', 'source-include')]
            # A source-only save can commit after HTTP times out. Read its canonical
            # project file, never the immutable validation preview, to resolve that result.
            for key, opened in request["sourcePreviews"].items():
                document = self._documents.get(key) or {}
                if document.get("commitUnknown") and (document.get("pendingSave") or {}).get("sourceOnly"):
                    opened["readCanonicalSource"] = True
            self._snapshot_running = True
            self._snapshot_again = False
            # The operation captures only immutable request data. The shared
            # executor retains the UI callback until delivery or cancel_owner.
            self._snapshot_executor.submit(
                self._snapshot_owner, lambda: _build_workspace_snapshot(request),
                lambda result, error: self._snapshot_ready(request, result, error))

        def _snapshot_ready(self, request, result, error):
            self._snapshot_running = False
            if error or not result or result.get("cancelled"):
                self._target_refresh_error = True
            elif not result.get("unchanged"):
                self._target_refresh_error = False
            if not self._alive or self._shutting_down:
                return
            binding = self._current_capture_binding()
            if binding != self._capture_binding:
                self.capture_changed(binding)
            if (request["captureBinding"] != binding or
                    request["generation"] != self._snapshot_generation or
                    request["registryPath"] != self.registry_path or
                    request["projectSelectionPath"] != self.project_selection_path):
                self._snapshot_again = True
            elif not error and result and result.get('sourceOnly'):
                self._source_documents.update(result['sources'])
                for key in result['sources']:
                    if key in request['documentStates']:
                        self._source_request_states[key] = request['documentStates'][key]
                    else:
                        self._source_request_states.pop(key, None)
                self._source_open_errors = dict(result.get('sourceErrors') or {})
                for key in set(result['sources']) | set(self._source_open_errors):
                    options = self._pending_open.pop(key, None)
                    if options is None:
                        continue
                    if key in result['sources']:
                        self._open_workspace(key, **options)
                    elif options.get('select'):
                        self._last_open_error = self._source_open_errors[key]
                self._refresh_current_view()
            elif not error and result and not result.get("unchanged") and not result.get("cancelled"):
                self._source_documents = result["sources"]
                self._source_request_states = request["documentStates"]
                self._apply_workspace_snapshot(result, binding)
            elif error:
                self._source_discovery_error = str(error)[:1024]
                self._refresh_current_view()
            if self._snapshot_again:
                self._snapshot_again = False
                self.refresh(True, source_only=bool(self._pending_open))

        def _apply_workspace_snapshot(self, result, binding):
            snapshot = result["snapshot"]
            self._snapshot = snapshot
            self._source_open_errors = dict(result.get("sourceErrors") or {})
            discovery_errors = snapshot.get("sourceDiscoveryErrors") or []
            self._source_discovery_error = str(discovery_errors[0])[:1024] if discovery_errors else ""
            self._target_refresh_error = bool(discovery_errors)
            previous_ids = set(self._workspaces)
            eligible = [workspace for workspace in snapshot.get('workspaces', [])
                        if workspace.get('editorEligible') is True]
            for workspace in eligible:
                previous = self._workspaces.get(str(workspace["workspaceId"])) or {}
                display_binding = previous.get("projectSourceBinding")
                if (isinstance(display_binding, dict) and
                        display_binding.get("captureSessionId") == (binding or {}).get("captureSessionId") and
                        workspace.get("sourceSha256") == display_binding.get("sourceSha256") and
                        workspace.get("shaderRawSha256") == display_binding.get("originalRawSha256") and
                        workspace.get("resourceId") == display_binding.get("resourceId")):
                    workspace.update(projectSourceBinding=display_binding,
                                     projectSourceIdentity=previous.get("projectSourceIdentity"))
            canonical_workspaces = {str(item['workspaceId']): item for item in eligible}
            aliases = dict(snapshot.get("documentAliases") or {})
            for alternate in snapshot.get("alternateWorkspaces", []):
                key = str(alternate["workspaceId"])
                if (self._document_has_local_work(key) or
                        (alternate.get("supersededProjectRepresentation") and
                         ((self._workspaces.get(key) or {}).get("retainedWorkingCopy") or
                          ((self._documents.get(key) or {}).get("workspace") or {}).get("retainedWorkingCopy")) and
                         (self._documents.get(key) or {}).get("captureBinding") == binding) or
                        (alternate.get("historicalSource") and not alternate.get("supersededCaptureSource") and
                         not alternate.get("internalReplaySource") and
                         key in self._documents and
                         self._documents[key].get("captureBinding") == binding)):
                    retained = dict(alternate)
                    retained["retainedWorkingCopy"] = True
                    retained["autoApplyReady"] = False
                    eligible.append(retained)
                    aliases.pop(key, None)
            self._document_aliases = aliases
            for old_id, target_id in aliases.items():
                if old_id in self._shader_labels and target_id not in self._shader_labels:
                    self._shader_labels[target_id] = self._shader_labels[old_id]
            self._pinned = list(dict.fromkeys(aliases.get(key, key) for key in self._pinned))
            self._restore_open = list(dict.fromkeys(aliases.get(key, key) for key in self._restore_open))
            self._restore_active = aliases.get(self._restore_active, self._restore_active)
            # Keep the requested ID until _open_workspace can check whether
            # its queued coordinates describe the current canonical text.
            merged_previews = []
            for preview_id, preview in list(self._organization_previews.items()):
                workspace = preview.get("workspace") or {}
                opened = preview.get("opened") or {}
                refreshed = result.get("sources", {}).get(preview_id)
                if refreshed is not None:
                    opened = preview["opened"] = refreshed
                    workspace.update(rawSha256=opened["rawSha256"], sourcePath=opened["sourcePath"])
                    for field in ("projectSourceIdentity", "projectSourceBinding", "semanticOverview",
                                  "flowGraph", "flowGraphStatus", "watchedPaths"):
                        workspace[field] = opened.get(field)
                    if workspace.get("documentKind") == "source-include":
                        document = self._documents.get(preview_id) or {}
                        if not any(document.get(key) for key in (
                                "dirty", "saveInFlight", "commitUnknown", "pendingSave", "conflict")):
                            for key in ("rootSourceSha256", "rootSourcePath", "mutationPaths"):
                                workspace[key] = opened.get(key)
                if (opened.get("captureBinding") == binding and
                        workspace.get("documentKind") in ("organization-preview", "source-include")):
                    canonical_id = (self._aliased_source_preview_workspace(
                        opened, canonical_workspaces, aliases) or self._canonical_organization_preview_workspace(
                        opened, canonical_workspaces, allow_stale_saved_preview=True)
                        if workspace.get("documentKind") == "organization-preview" else "")
                    if (canonical_id and self._discard_organization_preview(preview_id)):
                        merged_previews.append(canonical_id)
                        continue
                    eligible.append(workspace)
                    if preview_id not in result.get("sourceErrors", {}):
                        self._source_documents[preview_id] = opened
            # A restored current project draft is the editor for that exact
            # captured Shader. The untouched initial decompile is an archive,
            # not another current file. Keep pending/dirty copies visible.
            for preview in self._organization_previews.values():
                opened = preview.get("opened") or {}
                replaced_ids = {str(item["workspaceId"]) for item in eligible
                                if self._preview_replaces_clean_capture_source(opened, item)}
                if replaced_ids:
                    preview_id = str(opened["workspaceId"])
                    eligible = [item for item in eligible if str(item["workspaceId"]) not in replaced_ids]
                    self._pinned = list(dict.fromkeys(preview_id if key in replaced_ids else key for key in self._pinned))
                    self._restore_open = list(dict.fromkeys(preview_id if key in replaced_ids else key for key in self._restore_open))
            for workspace in eligible:
                if str(workspace['workspaceId']) in result.get('sourceErrors', {}):
                    workspace['autoApplyReady'] = False
            for source_id, captured in getattr(self, "_capture_sources", {}).items():
                if captured["opened"].get("captureBinding") == binding:
                    eligible.append(captured["workspace"])
                    self._source_documents[source_id] = captured["opened"]
            represented = {str(item['workspaceId']) for item in eligible}
            for key, document in self._documents.items():
                # Discovery/compile failures cannot close an editor. Keep its
                # local text as a display-only working copy until the ordinary
                # source/Host readers reconnect it; it grants no replay effect.
                if (key in represented or aliases.get(key) in represented or
                        document.get('captureBinding') != binding or
                        (self._tab_index(key) < 0 and not self._document_has_local_work(key))):
                    continue
                retained = dict(document.get('workspace') or self._workspaces.get(key) or {})
                if retained.get('workspaceId') != key:
                    continue
                retained.update(retainedWorkingCopy=True, metadataUnavailable=True,
                                editorEligible=True, autoApplyReady=False)
                eligible.append(retained)
            self._workspaces = {str(workspace['workspaceId']): workspace for workspace in eligible}
            self._rebind_project_documents(binding, aliases)
            eligible = list(self._workspaces.values())
            event_ids = result.get("eventIds") or {}
            self._workspace_event_ids = dict(
                (str(key), list(value)) for key, value in event_ids.items()
                if isinstance(value, (list, tuple)))
            for preview_id, preview in self._organization_previews.items():
                source_workspace_id = str(
                    (preview.get("workspace") or {}).get("sourceWorkspaceId") or "")
                if source_workspace_id in self._workspace_event_ids:
                    self._workspace_event_ids[preview_id] = list(
                        self._workspace_event_ids[source_workspace_id])
                display_events = (preview.get("opened") or {}).get("eventIds")
                if display_events:
                    self._workspace_event_ids[preview_id] = list(display_events)
            # Disk catalogs can contain only the representative EID used to
            # create a workspace. Polling them must not overwrite the complete
            # current-session usage result or resurrect a disproved use.
            for workspace in eligible:
                workspace_id = str(workspace['workspaceId'])
                events = _workspace_live_event_ids(workspace, binding, self._usage_cache,
                    self._workspace_event_ids.get(workspace_id, ()))
                if events is not None:
                    self._workspace_event_ids[workspace_id] = events
            signature = self._workspace_tree_signature(eligible)
            if signature != self._tree_signature:
                self._tree_signature = signature
                self._render_tree(eligible)
            self._request_live_usage(eligible)
            self._watched_paths = result["watchedPaths"]
            self._reset_watched_paths(result["existingPaths"])
            self._last_disk_signature = result["signature"]
            if result.get("sourceErrors") or result.get("staleSources") or discovery_errors:
                self._snapshot_error_retry_at = (
                    time.monotonic() + self._snapshot_error_retry_delay)
                self._snapshot_error_retry_delay = min(
                    SNAPSHOT_ERROR_RETRY_MAX_SECONDS,
                    max(SNAPSHOT_ERROR_RETRY_MIN_SECONDS,
                        self._snapshot_error_retry_delay * 2.0))
            else:
                self._snapshot_error_retry_at = 0.0
                self._snapshot_error_retry_delay = SNAPSHOT_ERROR_RETRY_MIN_SECONDS
            for workspace_id, document in list(self._documents.items()):
                workspace = self._workspaces.get(workspace_id)
                if workspace is not None and workspace.get('metadataUnavailable'):
                    document['workspace'] = workspace
                    document['available'] = False
                    document['readOnly'] = False
                    document['editor'].SetReadOnly(False)
                    document['editor'].SetNavigationEnabled(False)
                    continue
                if workspace is None or not self._can_resume_document(document, workspace, binding):
                    document['available'] = False
                    document['editor'].SetReadOnly(True)
                    document['editor'].SetNavigationEnabled(False)
                    index = self._tab_index(workspace_id)
                    if index >= 0:
                        self.tab_bar.removeTab(index)
                    continue
                document['available'] = True
                document['readOnly'] = False
                document['workspace'] = workspace
                document['authorizationId'] = workspace.get('authorizationId', '')
                document['captureBinding'] = binding
                document['editor'].SetReadOnly(False)
                document['editor'].SetNavigationEnabled(True)
                self._refresh_document_from_disk(workspace_id)
            if binding is not None:
                # Saved sources can become readable in separate Host snapshots.
                # Keep each intent pending until it opens or the user closes it.
                restore_ids = list(dict.fromkeys(self._pinned + self._restore_open))
                for workspace_id in restore_ids[:MAX_OPEN_DOCUMENTS]:
                    if ((workspace_id in self._source_documents or
                         (workspace_id in self._documents and
                          (self._workspaces.get(workspace_id) or {}).get("retainedWorkingCopy"))) and
                            workspace_id not in self._closed_document_ids):
                        if (self._tab_index(workspace_id) >= 0 or
                                self._open_workspace(workspace_id, quiet=True, select=False)):
                            self._restore_open = [item for item in self._restore_open if item != workspace_id]
            if not self._restored and binding is not None:
                self._restored = bool(self._source_documents)
                readable = [item for item in eligible if str(item['workspaceId']) in self._source_documents]
                if not self.tab_bar.count() and readable and self._restore_default_document:
                    self._open_workspace(str(readable[0]['workspaceId']), quiet=True, select=False)
                index = self._tab_index(self._restore_active)
                index = index if index >= 0 else (0 if self.tab_bar.count() else -1)
                self.tab_bar.setCurrentIndex(index)
                self._tab_changed(index)
            else:
                added = [str(workspace['workspaceId']) for workspace in eligible
                         if str(workspace['workspaceId']) not in previous_ids and
                         str(workspace['workspaceId']) not in self._closed_document_ids and
                         str(workspace['workspaceId']) in self._source_documents]
                if added and (self.tab_bar.count() or self._restore_default_document):
                    self._open_workspace(added[0], quiet=True)
            pending_open = self._pending_open
            self._pending_open = {}
            for workspace_id, options in pending_open.items():
                canonical_id = self._canonical_workspace_id(workspace_id)
                if canonical_id in self._source_documents:
                    self._open_workspace(workspace_id, **options)
                elif canonical_id in self._source_open_errors:
                    self._last_open_error = self._source_open_errors[canonical_id]
                elif canonical_id in result.get("staleSources", []):
                    # Retry a concurrent write, not a permanently unreadable
                    # source that would keep forcing the whole snapshot.
                    self._pending_open[workspace_id] = options
                elif canonical_id in self._workspaces:
                    # A click can arrive after this background read captured
                    # documentIds. Its result never attempted that source;
                    # preserve the newer intent for the next bounded read.
                    self._pending_open[workspace_id] = options
                    self._snapshot_again = True
            for workspace_id in dict.fromkeys(merged_previews):
                self._open_workspace(workspace_id, quiet=True, select=True)
            pending_reload = self._pending_reload
            self._pending_reload = {}
            for workspace_id, identity in pending_reload.items():
                document = self._documents.get(workspace_id)
                opened = self._cached_source_document(workspace_id)
                if (document is not None and opened is not None and
                        identity == _document_read_identity(document)):
                    self._apply_loaded_document(document, opened)
            self._refresh_current_view()

        def _document_has_local_work(self, workspace_id):
            document = self._documents.get(workspace_id)
            if document is None:
                return False
            if any(document.get(key) for key in (
                    "dirty", "saveInFlight", "commitUnknown", "pendingSave", "conflict")):
                return True
            try:
                return str(document["editor"].GetText()) != document.get("baselineEditorText")
            except Exception:
                return True

        def _rebind_project_documents(self, binding, aliases=None):
            """Carry project text tabs across RDCs without carrying replay leases."""
            if not binding:
                return
            candidates = [(key, workspace, self._source_documents.get(key) or {})
                          for key, workspace in self._workspaces.items()
                          if workspace.get("projectSourceIdentity") and not workspace.get("retainedWorkingCopy")]
            for old_id, document in list(self._documents.items()):
                old_workspace = document.get("workspace") or {}
                identity = old_workspace.get("projectSourceIdentity")
                current = self._workspaces.get(old_id)
                if (old_workspace.get("retainedWorkingCopy") is True and
                        current is not None and not current.get("retainedWorkingCopy") and
                        current.get("captureBinding") == binding == document.get("captureBinding") and
                        current.get("sourcePath") and
                        _normal_path(current["sourcePath"]) == _normal_path(document.get("sourcePath", "")) and
                        all(current.get(field) == old_workspace.get(field) and
                            current.get(field) not in (None, "") for field in (
                                "shaderRawSha256", "stage", "entryPoint", "resourceId"))):
                    # This exact working copy is canonical again after a save
                    # or project-head refresh. Keep the fresh current-session
                    # audit; an old retained flag must not erase it forever.
                    # Only replace the model, never the buffer or CAS baseline.
                    document["workspace"] = current
                    continue
                target = (aliases or {}).get(old_id)
                if (target and target in self._workspaces and target in self._source_documents and
                        not self._document_has_local_work(old_id)):
                    self._retire_rebound_document(old_id, target)
                    continue
                if not isinstance(identity, dict) or not identity:
                    continue
                matches = [(key, workspace, source) for key, workspace, source in candidates
                           if key != old_id and workspace.get("projectSourceIdentity") == identity and
                           source.get("rawSha256") == document.get("rawSha256")]
                retained = old_workspace.get("retainedWorkingCopy") is True
                if (not matches and not retained) or (old_id in self._workspaces and not retained):
                    continue
                if not any(workspace["projectSourceIdentity"].get("projectId") == identity.get("projectId")
                           for unused, workspace, source in candidates):
                    continue
                if self._document_has_local_work(old_id) or retained:
                    # The existing managed file remains the CAS save target;
                    # text editing uses no old watch/compile/apply authority.
                    copy_workspace = dict(old_workspace, retainedWorkingCopy=True,
                                          autoApplyReady=False, liveEdit={}, watch={}, captureBinding=binding)
                    self._workspaces[old_id] = copy_workspace
                    document.update(workspace=copy_workspace, captureBinding=binding, available=True, readOnly=False)
                    document["editor"].SetReadOnly(False)
                    document["editor"].SetNavigationEnabled(True)
                    if old_id not in self._restore_open:
                        self._restore_open.append(old_id)
                    continue
                if not matches:
                    continue
                self._retire_rebound_document(old_id, matches[0][0])

        def _retire_rebound_document(self, old_id, target):
            was_pinned = old_id in self._pinned
            restore_ids = list(self._restore_open)
            active = self._restore_active
            default_document = self._restore_default_document
            if self._close_document(old_id, prompt=False):
                if was_pinned and target not in self._pinned:
                    self._pinned.append(target)
                self._restore_open = list(dict.fromkeys(
                    [target if key == old_id else key for key in restore_ids] + [target]))
                self._restore_active = target if active == old_id else active
                self._restore_default_document = default_document
                self._closed_document_ids.discard(target)

        def _cached_source_document(self, workspace_id):
            workspace = self._workspaces.get(workspace_id)
            opened = self._source_documents.get(workspace_id)
            if (workspace is not None and opened is not None and
                    workspace.get("documentKind") in ("organization-preview", "source-include", "capture-source")):
                if (self._capture_binding != self._current_capture_binding() or
                        opened.get("captureBinding") != self._capture_binding or
                        opened.get("documentKind") != workspace.get("documentKind") or
                        opened.get("organizationId") != workspace.get("organizationId") or
                        opened.get("rawSha256") != workspace.get("rawSha256") or
                        opened.get("sourcePath") != workspace.get("sourcePath")):
                    return None
                return opened
            if (workspace is None or opened is None or
                    self._capture_binding != self._current_capture_binding() or
                    opened.get("captureBinding") != self._capture_binding or
                    opened.get("authorizationId") != workspace.get("authorizationId") or
                    opened.get("liveEditId") != _source_read_session(workspace).get("liveEditId") or
                    opened.get("watchId") != _source_read_session(workspace).get("watchId")):
                return None
            document = self._documents.get(workspace_id)
            if document is None and workspace_id in self._source_request_states:
                return None
            if document is not None and self._source_request_states.get(workspace_id) != _document_read_identity(document):
                return None
            return opened

        def _canonical_organization_preview_workspace(self, opened, workspaces=None,
                                                      allow_stale_saved_preview=False):
            """Reuse only the same authorized saved source, never an unsaved buffer."""
            binding = self._capture_binding
            if (opened.get("canonicalSourceUpdated") is not True or not binding or
                    binding != self._current_capture_binding() or
                    opened.get("captureBinding") != binding):
                return ""
            canonical_id = str(opened.get("sourceWorkspaceId") or "")
            candidates = self._workspaces if workspaces is None else workspaces
            workspace = candidates.get(canonical_id)
            shared_project_source = False
            project_identity = opened.get("projectSourceIdentity")
            if not workspace and isinstance(project_identity, dict) and project_identity:
                for key, candidate in candidates.items():
                    candidate_source = self._source_documents.get(key) or {}
                    same_project_document = candidate.get("projectSourceIdentity") == project_identity
                    same_current_shader = bool(opened.get("projectSourceBinding") and
                        _normal_path(candidate.get("capturePath", "")) == _normal_path(opened.get("capturePath", "")) and
                        all(candidate.get(field) == opened.get(field) and opened.get(field) not in (None, "")
                            for field in ("resourceId", "shaderRawSha256", "stage", "entryPoint")))
                    if ((same_project_document or same_current_shader) and
                            candidate_source.get("rawSha256") == opened.get("rawSha256") and
                            candidate.get("shaderRawSha256") == opened.get("shaderRawSha256")):
                        canonical_id, workspace = key, candidate
                        shared_project_source = True
                        break
            source = self._source_documents.get(canonical_id)
            if (not workspace or not source or workspace.get("editorEligible") is not True or
                    workspace.get("documentKind") == "organization-preview" or
                    source.get("captureBinding") != binding or
                    source.get("workspaceId") != canonical_id or
                    source.get("authorizationId") != workspace.get("authorizationId") or
                    source.get("liveEditId") != _source_read_session(workspace).get("liveEditId") or
                    source.get("watchId") != _source_read_session(workspace).get("watchId") or
                    not str(opened.get("sourcePath") or "").strip() or
                    _normal_path(workspace.get("sourcePath", "")) != _normal_path(source.get("sourcePath", "")) or
                    (not shared_project_source and
                     _normal_path(source.get("sourcePath", "")) != _normal_path(opened.get("sourcePath", ""))) or
                    not re.fullmatch(r"[0-9a-f]{64}", str(source.get("rawSha256") or "")) or
                    (not allow_stale_saved_preview and source.get("rawSha256") != opened.get("rawSha256")) or
                    any(workspace.get(key) != opened.get(key) or opened.get(key) in (None, "")
                        for key in (("stage", "entryPoint") if shared_project_source else
                                    ("captureId", "stage", "entryPoint", "resourceId")))):
                return ""
            for document_id, expected_sha in ((canonical_id, source.get("rawSha256")),
                                             (str(opened.get("workspaceId") or ""), opened.get("rawSha256"))):
                document = self._documents.get(document_id)
                if document is not None:
                    if (document.get("available") is not True or document.get("captureBinding") != binding or
                            document.get("rawSha256") != expected_sha or
                            any(document.get(key) for key in (
                                "dirty", "saveInFlight", "commitUnknown", "pendingSave", "conflict"))):
                        return ""
                    try:
                        if str(document["editor"].GetText()) != document.get("baselineEditorText"):
                            return ""
                    except Exception:
                        return ""
            return canonical_id

        def _canonical_workspace_id(self, workspace_id):
            """Late UI callbacks follow current source aliases, never old replay files."""
            original = str(workspace_id or "")
            if (self._capture_binding != self._current_capture_binding() or
                    self._document_has_local_work(original)):
                return original
            target = _document_alias_target(original, getattr(self, "_document_aliases", {}))
            return target if target in self._workspaces else original

        def _aliased_source_preview_workspace(self, opened, workspaces=None, aliases=None):
            """Select current text without importing an obsolete preview's bytes/lease."""
            binding = self._capture_binding
            if (not binding or binding != self._current_capture_binding() or
                    opened.get("captureBinding") != binding or
                    opened.get("documentKind") != "organization-preview" or
                    opened.get("canonicalSourceUpdated") is not True or opened.get("organizationId") or
                    self._document_has_local_work(str(opened.get("workspaceId") or ""))):
                return ""
            original = str(opened.get("sourceWorkspaceId") or opened.get("workspaceId") or "")
            if self._document_has_local_work(original):
                return ""
            candidates = self._workspaces if workspaces is None else workspaces
            aliases = getattr(self, "_document_aliases", {}) if aliases is None else aliases
            target = _document_alias_target(original, aliases)
            workspace = candidates.get(target)
            if (target == original or not workspace or workspace.get("editorEligible") is not True or
                    workspace.get("documentKind") == "source-include" or
                    _normal_path(workspace.get("capturePath", "")) != _normal_path(binding["capturePath"]) or
                    _normal_path(opened.get("capturePath", "")) != _normal_path(binding["capturePath"]) or
                    any(workspace.get(field) != opened.get(field) or not opened.get(field)
                        for field in ("resourceId", "shaderRawSha256", "stage", "entryPoint")) or
                    any(workspace.get(field) != opened.get(field)
                        for field in ("api", "targetProfile")
                        if workspace.get(field) is not None and opened.get(field) is not None)):
                return ""
            return target

        def _organization_preview_display_state(self, workspace_id):
            document = self._documents.get(workspace_id) or {}
            workspace = self._workspaces.get(workspace_id) or {}
            editor = document.get("editor")
            state = {"shown": True, "workspaceId": workspace_id,
                    "documentKind": str(document.get("documentKind") or workspace.get("documentKind") or "live-edit"),
                    "readOnly": (bool(editor.IsReadOnly()) if editor is not None else
                                 bool(workspace.get("readOnly") is True)),
                    "canonicalWorkspaceReused": workspace.get("documentKind") != "organization-preview"}
            if workspace.get("projectSourceIdentity"):
                state["canonicalProjectSourceReused"] = workspace["projectSourceIdentity"]
            return state

        def _preview_replaces_clean_capture_source(self, opened, workspace):
            # Only Host-verified current-head display bindings can supersede
            # an untouched decompiled/or older imported source in this tree.
            # Saved files, physical bindings and every dirty buffer survive.
            return bool(opened.get("projectSourceBinding") and
                        opened.get("captureBinding") == self._capture_binding and
                        workspace.get("documentKind") != "source-include" and
                        workspace.get("workspaceId") != opened.get("workspaceId") and
                        not self._document_has_local_work(str(workspace.get("workspaceId") or "")) and
                        _normal_path(workspace.get("capturePath", "")) ==
                        _normal_path(opened.get("capturePath", "")) and
                        all(workspace.get(key) == opened.get(key) and opened.get(key) not in (None, "")
                            for key in ("shaderRawSha256", "stage", "entryPoint", "resourceId")))

        def _retire_superseded_project_sources(self, opened, target_id):
            """Replace only clean displays of this exact current Shader target."""
            if not opened.get("projectSourceBinding"):
                return
            for key, candidate in list(self._workspaces.items()):
                if key == target_id or not self._preview_replaces_clean_capture_source(opened, candidate):
                    continue
                pinned, restored = list(self._pinned), list(self._restore_open)
                if key in self._organization_previews:
                    if not self._discard_organization_preview(key):
                        continue
                else:
                    if key in self._documents and not self._close_document(key, prompt=False):
                        continue
                    for mapping in (self._workspaces, self._source_documents,
                                    self._workspace_event_ids, self._live_check, self._local_analysis):
                        mapping.pop(key, None)
                self._pinned = list(dict.fromkeys(target_id if item == key else item for item in pinned))
                self._restore_open = list(dict.fromkeys(target_id if item == key else item for item in restored))
                if self._restore_active == key:
                    self._restore_active = target_id
                self._pending_open.pop(key, None)
                self._pending_reload.pop(key, None)

        def _refresh_saved_organization_source(self, opened):
            """Publish newer saved bytes into the same clean, authorized editor."""
            canonical_id = self._canonical_organization_preview_workspace(
                opened, allow_stale_saved_preview=True)
            if not canonical_id:
                return
            source = self._source_documents[canonical_id]
            if (source.get("rawSha256") == opened.get("rawSha256") or
                    re.fullmatch(r"[0-9a-f]{64}", str(opened.get("rawSha256") or "")) is None or
                    not isinstance(opened.get("text"), str) or
                    not isinstance(opened.get("modifiedNs"), int) or
                    not isinstance(source.get("modifiedNs"), int) or
                    opened["modifiedNs"] <= source["modifiedNs"]):
                return
            # The bridge already verified these canonical file bytes. Retain
            # the existing authorization and edit-session identity; displaying
            # a saved result must neither grant live-edit rights nor create a
            # second Shader while its background source refresh catches up.
            refreshed = dict(source)
            for key in ("text", "rawSha256", "size", "modifiedNs"):
                if key in opened:
                    refreshed[key] = opened[key]
            self._source_documents[canonical_id] = refreshed
            document = self._documents.get(canonical_id)
            if document is not None:
                self._apply_loaded_document(document, refreshed)
                self._source_request_states[canonical_id] = _document_read_identity(document)
            # A file read already in flight may still contain the old bytes.
            # Invalidate that delivery and let the ordinary asynchronous poll
            # reconcile current Host metadata without a second tool request.
            self._snapshot_generation += 1
            self._last_disk_signature = None

        def open_capture_source(self, opened):
            """Open a capture-owned source copy without publishing a managed workspace."""
            binding = self._current_capture_binding()
            if not isinstance(opened, dict) or not binding or opened.get("captureBinding") != binding:
                return False
            source_id = str(opened.get("workspaceId") or "")
            if TOKEN_RE.fullmatch(source_id) is None or not isinstance(opened.get("text"), str):
                return False
            if binding != self._capture_binding:
                self.capture_changed(binding)
            path = str(opened.get("sourcePath") or "")
            source = dict(opened, documentKind="capture-source", readOnly=False,
                          fileName=os.path.basename(path), rawSha256=hashlib.sha256(opened["text"].encode("utf-8")).hexdigest(),
                          size=len(opened["text"].encode("utf-8")), modifiedNs=0)
            workspace = {key: source.get(key) for key in (
                "workspaceId", "sourcePath", "rootSourcePath", "includeRoot", "entryPoint", "stage", "resourceId",
                "shaderRawSha256", "fileName", "rawSha256", "targetProfile", "language")}
            workspace.update(documentKind="capture-source", sourceKind="captured-source",
                             editorEligible=True, sourceEditable=True, autoApplyReady=False,
                             readOnly=False, capturePath=binding.get("capturePath", ""),
                             sourceRelativePath=source["fileName"], liveEdit={}, watch={})
            self._capture_sources[source_id] = {"workspace": workspace, "opened": source}
            self._workspaces[source_id] = workspace
            self._source_documents[source_id] = source
            self._render_tree(list(self._workspaces.values()))
            return self._open_workspace(source_id, quiet=False, select=True)

        def show_organization_preview(self, opened):
            """Select the latest verified draft without replacing a live buffer."""
            is_include = isinstance(opened, dict) and opened.get("documentKind") == "source-include"
            if (not isinstance(opened, dict) or
                    opened.get("documentKind") not in ("organization-preview", "source-include") or
                    (not is_include and opened.get("readOnly") is not True) or
                    opened.get("captureBinding") != self._current_capture_binding() or
                    opened.get("captureBinding") != self._capture_binding):
                return False
            preview_id = str(opened.get("workspaceId") or "")
            if TOKEN_RE.fullmatch(preview_id) is None:
                return False
            if self._document_has_local_work(preview_id):
                return False
            if opened.get("projectSourceBinding"):
                # A previous file/library snapshot may still describe the old project head.
                # It must not resurrect that display after this verified publication.
                self._snapshot_generation += 1
                self._last_disk_signature = None
            source_workspace_id = str(opened.get("sourceWorkspaceId") or "")
            aliased_id = self._aliased_source_preview_workspace(opened) if not is_include else ""
            if aliased_id:
                if not self._open_workspace(aliased_id, quiet=True, select=True):
                    return False
                document = self._documents.get(aliased_id)
                workspace = self._workspaces.get(aliased_id) or {}
                if not document or document.get("available") is not True:
                    return False
                text = self._document_source_text(document)
                state = self._organization_preview_display_state(aliased_id)
                state.update(
                    redirectedFromWorkspaceId=source_workspace_id,
                    displayedSourceSha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    displayedSourceWorkspaceId=str(document.get("sourceWorkspaceId") or
                                                   workspace.get("sourceWorkspaceId") or aliased_id),
                    displayedSourcePath=str(document.get("sourcePath") or workspace.get("sourcePath") or ""),
                    canonicalProjectSourceIdentity=workspace.get("projectSourceIdentity"),
                    canonicalProjectSourceBinding=workspace.get("projectSourceBinding"))
                self._refresh_current_view()
                return state
            if not is_include:
                self._refresh_saved_organization_source(opened)
            canonical_id = self._canonical_organization_preview_workspace(opened) if not is_include else ""
            if canonical_id and self._open_workspace(canonical_id, quiet=True, select=True):
                for key, value in list(self._organization_previews.items()):
                    if self._canonical_organization_preview_workspace(
                            value.get("opened") or {}, allow_stale_saved_preview=True) == canonical_id:
                        self._discard_organization_preview(key)
                state = self._organization_preview_display_state(canonical_id)
                if opened.get("projectSourceIdentity"):
                    state["canonicalProjectSourceReused"] = opened["projectSourceIdentity"]
                    self._workspaces[canonical_id]["projectSourceIdentity"] = opened["projectSourceIdentity"]
                    self._workspaces[canonical_id]["projectSourceBinding"] = opened.get("projectSourceBinding")
                if opened.get("eventIds"):
                    self._workspace_event_ids[canonical_id] = list(opened["eventIds"])
                self._retire_superseded_project_sources(opened, canonical_id)
                self._render_tree(list(self._workspaces.values()))
                self._refresh_current_view()
                return state

            stale_ids = [
                key for key, value in self._organization_previews.items()
                if (key != preview_id and
                    str((value.get("workspace") or {}).get(
                        "sourceWorkspaceId") or "") == source_workspace_id and
                    (value.get("opened") or {}).get("captureBinding") == opened.get("captureBinding") and
                    ((value.get("opened") or {}).get("rawSha256") == opened.get("rawSha256") or
                     ((value.get("opened") or {}).get("canonicalSourceUpdated") is True and
                      opened.get("canonicalSourceUpdated") is True)) and
                    _normal_path((value.get("opened") or {}).get("sourcePath", "")) ==
                    _normal_path(opened.get("sourcePath", "")))
            ]
            for stale_id in stale_ids:
                self._discard_organization_preview(stale_id)
            workspace = {
                "workspaceId": preview_id,
                "sourceWorkspaceId": source_workspace_id,
                "workspacePath": str(opened.get("workspacePath") or ""),
                "captureId": str(opened.get("captureId") or ""),
                "capturePath": str(opened.get("capturePath") or ""),
                "stage": str(opened.get("stage") or ""),
                "entryPoint": str(opened.get("entryPoint") or ""),
                "resourceId": opened.get("resourceId"),
                "shaderRawSha256": opened.get("shaderRawSha256"),
                "projectSourceIdentity": opened.get("projectSourceIdentity"),
                "projectSourceBinding": opened.get("projectSourceBinding"),
                "semanticOverview": opened.get("semanticOverview"),
                "flowGraph": opened.get("flowGraph"),
                "flowGraphStatus": opened.get("flowGraphStatus"),
                "watchedPaths": list(opened.get("watchedPaths") or []),
                "sourcePath": str(opened.get("sourcePath") or ""),
                "rootSourcePath": str(opened.get("rootSourcePath") or ""),
                "sourceRelativePath": str(opened.get("sourceRelativePath") or ""),
                "fileName": str(opened.get("fileName") or ""),
                "rawSha256": str(opened.get("rawSha256") or ""),
                "organizationId": str(opened.get("organizationId") or ""),
                "sourceKind": str(opened.get("sourceKind") or "organization-preview"),
                "validationTier": str(opened.get("validationTier") or ""),
                "completedChecks": list(opened.get("completedChecks") or []),
                "deferredFinalChecks": list(
                    opened.get("deferredFinalChecks") or []),
                "documentKind": opened["documentKind"],
                "readOnly": False,
                "sourceEditable": True,
                "editorEligible": True,
                "autoApplyReady": False,
                "liveEdit": {},
                "watch": {},
            }
            if is_include:
                for key in ("sourceDocumentId", "rootSourcePath", "rootSourceSha256", "mutationPaths", "originPath"):
                    workspace[key] = opened.get(key)
            source = dict(opened)
            source["workspaceId"] = preview_id
            source["documentKind"] = opened["documentKind"]
            source["readOnly"] = False
            self._organization_previews[preview_id] = {
                "workspace": workspace, "opened": source}
            # Also observe metadata-only publications after this source view
            # opens, including when no canonical live-edit model is available.
            self._last_disk_signature = None
            if len(self._organization_previews) > MAX_ORGANIZATION_PREVIEWS:
                candidates = sorted(
                    (value.get("opened", {}).get("modifiedNs", 0), key)
                    for key, value in self._organization_previews.items()
                    if key != preview_id)
                while (len(self._organization_previews) >
                       MAX_ORGANIZATION_PREVIEWS and candidates):
                    self._discard_organization_preview(candidates.pop(0)[1])
            self._workspaces[preview_id] = workspace
            self._retire_superseded_project_sources(opened, preview_id)
            self._source_documents[preview_id] = source
            self._rebind_project_documents(self._capture_binding)
            if opened.get("eventIds"):
                self._workspace_event_ids[preview_id] = list(opened["eventIds"])
            document = self._documents.get(preview_id)
            if document is not None:
                document["workspace"] = workspace
                document["captureBinding"] = self._capture_binding
                document["sourcePath"] = source["sourcePath"]
                document["fileName"] = source["fileName"]
                document["entryPoint"] = source["entryPoint"]
                document["documentKind"] = opened["documentKind"]
                document["readOnly"] = False
                document["available"] = True
                document["editor"].SetFileName(source["sourcePath"])
                if hasattr(document["editor"], "SetIncludeBaseDirectory"):
                    document["editor"].SetIncludeBaseDirectory(
                        os.path.dirname(workspace.get("rootSourcePath", ""))
                        if opened["documentKind"] == "organization-preview" else "")
                document["editor"].SetReadOnly(False)
                self._apply_loaded_document(document, source)
            self._render_tree(list(self._workspaces.values()))
            if not self._open_workspace(preview_id, quiet=True, select=True):
                return False
            self._refresh_current_view()
            return self._organization_preview_display_state(preview_id)

        def _discard_organization_preview(self, preview_id):
            preview_id = str(preview_id or "")
            document = self._documents.get(preview_id)
            if document is not None and document.get("editor") is not None:
                try:
                    if str(document["editor"].GetText()) != document.get("baselineEditorText"):
                        return False
                except Exception:
                    return False
            if document is not None and not self._close_document(
                    preview_id, prompt=False):
                return False
            self._organization_previews.pop(preview_id, None)
            self._workspaces.pop(preview_id, None)
            self._source_documents.pop(preview_id, None)
            self._workspace_event_ids.pop(preview_id, None)
            self._live_check.pop(preview_id, None)
            self._local_analysis.pop(preview_id, None)
            return True

        def _workspace_tree_signature(self, workspaces):
            return tuple((workspace['workspaceId'], workspace.get('captureId', ''),
                          workspace.get('capturePath', ''), bool(workspace.get('retainedWorkingCopy')),
                          workspace.get('stage', ''), workspace.get('entryPoint', ''),
                          workspace.get('sourcePath', ''),
                          tuple(_workspace_tree_event_ids(workspace, self._workspace_event_ids)))
                         for workspace in sorted(workspaces, key=lambda item:
                             _workspace_tree_sort_key(item, self._workspace_event_ids)))

        def _render_tree(self, workspaces):
            # Preview publication and usage updates also render outside the
            # snapshot path. Cache the tree we actually display, not the last
            # Host snapshot: otherwise merging a preview back into that same
            # snapshot leaves a clickable leaf with no document behind it.
            self._tree_signature = self._workspace_tree_signature(workspaces)
            self.file_tree.clear()
            self._tree_items = {}
            captures = {}
            # Stage is part of each leaf rather than a group: stage groups can
            # interleave EIDs and hide the numerical order of the capture.
            for workspace in sorted(workspaces, key=lambda item:
                                    _workspace_tree_sort_key(item, self._workspace_event_ids)):
                capture_id = str(workspace.get("captureId", "") or "")
                capture_path = str(workspace.get("capturePath") or "")
                working_copy = workspace.get("retainedWorkingCopy") is True
                capture_key = ("working-copy" if working_copy else
                               _normal_path(capture_path) if capture_path else capture_id or "unknown")
                capture_item = captures.get(capture_key)
                if capture_item is None:
                    label = (QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Working copies") if working_copy else
                        _format_message(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Capture %1"),
                        os.path.basename(capture_path) if capture_path else capture_id[:12] if capture_id else
                        QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "Unknown")))
                    capture_item = QtWidgets.QTreeWidgetItem((label,))
                    capture_item.setToolTip(0, capture_path or capture_id)
                    captures[capture_key] = capture_item
                    self.file_tree.addTopLevelItem(capture_item)
                stage = str(workspace.get("stage", "") or "").lower()
                abbreviation = STAGE_ABBREVIATIONS.get(stage, stage.upper())
                file_name = os.path.basename(str(workspace.get("sourcePath", "") or ""))
                file_name = self._shader_labels.get(str(workspace.get("workspaceId", "")), file_name)
                entry_point = str(workspace.get("entryPoint", "") or "")
                workspace_id = str(workspace.get("workspaceId", "") or "")
                events = _workspace_tree_event_ids(workspace, self._workspace_event_ids)
                label = _format_message(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "%1 · %2 · %3"), abbreviation, entry_point, file_name)
                tooltip = str(workspace.get("sourcePath", "") or "")
                if events:
                    # Preview known uses here; the menu exposes every EID.
                    # i18n-ignore: separator between numeric EID identifiers, not prose.
                    shown = ", ".join(str(event) for event in events[:4])
                    if len(events) > 4:
                        shown += " +%d" % (len(events) - 4)
                    label = _format_message(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "EID %1 · %2 · %3 · %4"),
                        shown, abbreviation, entry_point, file_name)
                    complete = _workspace_usage_complete(
                        workspace, self._current_capture_binding(), self._usage_cache)
                    usage_text = (QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Used by %1 event(s): %2") if complete else
                        QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Known uses: %1 event(s): %2"))
                    tooltip += "\n" + _format_message(usage_text,
                        str(len(events)),
                        ", ".join(str(event) for event in events[:64]) +
                        (" …" if len(events) > 64 else ""))
                    tooltip += "\n" + QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Right-click to jump to an event")
                leaf = QtWidgets.QTreeWidgetItem((label,))
                leaf.setData(0, QtCore.Qt.UserRole, workspace_id)
                leaf.setToolTip(0, tooltip)
                capture_item.addChild(leaf)
                self._tree_items[workspace_id] = leaf
            self.file_tree.expandAll()

        def _tree_item_clicked(self, item, _column=0):
            workspace_id = str(item.data(0, QtCore.Qt.UserRole) or "")
            if workspace_id:
                self._open_workspace(workspace_id)

        def _tree_context_menu(self, position):
            """Right-click on a shader: list the events that bind it and jump."""
            item = self.file_tree.itemAt(position)
            if item is None:
                return
            workspace_id = str(item.data(0, QtCore.Qt.UserRole) or "")
            if not workspace_id:
                return
            workspace = self._workspaces.get(workspace_id) or {}
            events = list(self._workspace_event_ids.get(workspace_id, ()))
            menu = QtWidgets.QMenu(self.file_tree)
            open_action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Open Shader"))
            open_action.triggered.connect(
                lambda _checked=False, value=workspace_id: self._open_workspace(value))
            rename_action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Rename Shader alias..."))
            rename_action.triggered.connect(lambda _checked=False, value=workspace_id: self._rename_shader_alias(value))
            attach_action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Add this Shader to the chat"))
            attach_action.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "Attach the Shader's identity, events and source header to Bread AI, then ask "
                "for an explanation or a new organization"))
            attach_action.triggered.connect(
                lambda _checked=False, value=workspace_id: self._add_workspace_to_chat(value))
            if not events:
                info = menu.addAction(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "No event usage information is available for this Shader"))
                info.setEnabled(False)
            else:
                menu.addSeparator()
                # Jumping only makes sense inside the capture these EIDs belong
                # to; for another open capture the actions stay visible but off.
                binding = self._current_capture_binding() or {}
                same_capture = bool(binding) and (
                    _normal_path(str(workspace.get("capturePath", "") or "")) ==
                    _normal_path(str(binding.get("capturePath", "") or "")))
                complete = _workspace_usage_complete(workspace, binding, self._usage_cache)
                header_text = (QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Used by %1 event(s)") if complete else
                    QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Known uses: %1 event(s)"))
                header = menu.addAction(_format_message(header_text, str(len(events))))
                header.setEnabled(False)

                def add_event(target, event):
                    action = target.addAction(_format_message(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Jump to EID %1"), str(event)))
                    action.setEnabled(same_capture)
                    if not same_capture:
                        action.setToolTip(QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "Open the capture this Shader belongs to first"))
                    action.triggered.connect(
                        lambda _checked=False, value=int(event): self._jump_to_event(value))

                def populate(target, values):
                    if not target.actions():
                        for event in values:
                            add_event(target, event)

                if len(events) <= 48:
                    # A temporary submenu is unnecessary for a short list.
                    for event in events:
                        add_event(menu, event)
                else:
                    # Every use remains reachable; instantiate actions only
                    # for the page being opened, without querying the replay.
                    for offset in range(0, len(events), 48):
                        values = tuple(events[offset:offset + 48])
                        page = menu.addMenu(_format_message(QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "EIDs %1–%2"), str(values[0]), str(values[-1])))
                        page.aboutToShow.connect(
                            lambda target=page, items=values: populate(target, items))
            menu.exec_(self.file_tree.viewport().mapToGlobal(position))

        def _add_workspace_to_chat(self, workspace_id):
            """Attach only library Shader identity and a source revision locator to chat."""
            workspace = self._workspaces.get(workspace_id) or {}
            binding = self._current_capture_binding()
            if (not workspace or not self._alive or not binding or
                    binding != self._capture_binding):
                return
            source_path = str(workspace.get("sourcePath", "") or "")
            source_name = os.path.basename(source_path) or QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader Workspace")
            events = [int(value) for value in self._workspace_event_ids.get(workspace_id, ())]
            # Source access uses the same capture/authorization boundary as
            # opening the editor. Never re-open a projection's path from the UI.
            opened = self._cached_source_document(workspace_id)
            source_text = opened.get("text") if opened is not None else None
            document = self._documents.get(workspace_id)
            editor_available = bool(document is not None and document.get("available") and
                    document.get("captureBinding") == binding and
                    document.get("authorizationId") == workspace.get("authorizationId") and
                    _normal_path(document.get("sourcePath", "")) == _normal_path(source_path))
            if editor_available:
                source_text = str(document["editor"].GetText())
            source_available = isinstance(source_text, str)
            reference_document = dict(document) if editor_available else {}
            reference_document.update(workspaceId=workspace_id, workspace=workspace,
                                      sourcePath=source_path, captureBinding=binding)
            if not editor_available:
                reference_document["dirty"] = False
                reference_document["rawSha256"] = str((opened or {}).get("rawSha256") or
                                                      workspace.get("sourceSha256") or "")
            context, facts = _selection_attachment(reference_document, "", source_path,
                1, max(1, len(source_text.splitlines())) if source_available else 1,
                int(self.ctx.CurEvent()), int(self.ctx.CurSelectedEvent()), events,
                source_text=source_text if source_available else None)
            context["origin"].pop("range", None)
            facts.pop("selection", None)
            facts["kind"] = "shader-source"
            if source_available:
                _retain_attachment_source(context, facts, source_text)
            text = json.dumps(facts, ensure_ascii=False)
            title = QtCore.QCoreApplication.translate("ReplayLabProjects", "Shader from the project library")
            source = _format_message(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader library — %1"), source_name)
            try:
                self.ctx.AddTextToAIAssistant(
                    title, text, source,
                    json.dumps(context, ensure_ascii=False, separators=(",", ":")))
                if not source_available:
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Shader reference added. Bread AI can read the managed source when needed."))
            except Exception as exc:
                # i18n-ignore: developer-only diagnostic printed by the bridge.
                print("[Bread] Shader Workspace add-to-chat failed: %s" % str(exc))

        def _jump_to_event(self, event_id):
            """Select an event in RenderDoc (same as clicking it in the event browser)."""
            try:
                event = int(event_id)
            except (TypeError, ValueError):
                return
            if event <= 0 or not self._alive:
                return
            try:
                self.ctx.SetEventID([], event, event)
            except Exception as exc:
                # i18n-ignore: developer-only diagnostic printed by the bridge.
                print("[Bread] Shader Workspace jump to EID %d failed: %s" % (event, str(exc)))

        def _tab_workspace_ids(self):
            return [
                str(self.tab_bar.tabData(index) or "")
                for index in range(self.tab_bar.count())
            ]

        def _tab_index(self, workspace_id):
            for index in range(self.tab_bar.count()):
                if str(self.tab_bar.tabData(index) or "") == str(workspace_id or ""):
                    return index
            return -1

        def _rename_shader_alias(self, workspace_id):
            label, accepted = QtWidgets.QInputDialog.getText(self.top,
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Rename Shader alias"),
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Shader alias (display only; empty resets):"),
                QtWidgets.QLineEdit.Normal, self._shader_labels.get(workspace_id, ""))
            if not accepted:
                return
            label = str(label).strip()[:160]
            if label:
                self._shader_labels[workspace_id] = label
            else:
                self._shader_labels.pop(workspace_id, None)
            self._save_state()
            self._tree_signature = None
            self._render_tree(list(self._workspaces.values()))
            for index in range(self.tab_bar.count()):
                key = str(self.tab_bar.tabData(index) or "")
                if key in self._documents:
                    self.tab_bar.setTabText(index, self._tab_title(self._documents[key]))

        def _tab_title(self, document):
            title = "%s · %s" % (
                document.get("entryPoint", ""), self._shader_labels.get(str(document.get("workspaceId", "")), document.get("fileName", "")))
            return title + (" *" if document.get("dirty") else "")

        def _insert_tab(self, workspace_id, document):
            pinned = workspace_id in self._pinned
            insertion = self.tab_bar.count()
            if pinned:
                insertion = sum(
                    1 for item in self._tab_workspace_ids() if item in self._pinned)
            index = self.tab_bar.insertTab(insertion, self._tab_title(document))
            self.tab_bar.setTabData(index, workspace_id)
            self.tab_bar.setTabToolTip(index, document.get("sourcePath", ""))
            self.tab_bar.configure_pin_button(index, workspace_id, pinned)
            return index

        def _open_workspace(self, workspace_id, quiet=False, select=True, navigation=None, definition=None):
            requested_id = str(workspace_id or "")
            workspace_id = self._canonical_workspace_id(workspace_id)
            if select:
                self._last_open_error = ""
                # A slower earlier open must not steal focus from a later
                # click, including when that document was already cached.
                for key, options in getattr(self, '_pending_open', {}).items():
                    options["select"] = self._canonical_workspace_id(key) == workspace_id
            if workspace_id != requested_id:
                # Definitions can depend on the original file's include root
                # even when the two root files happen to contain equal text.
                definition = None
                previous = self._source_documents.get(requested_id) or {}
                current = self._source_documents.get(workspace_id) or {}
                document = self._documents.get(workspace_id)
                if (not previous.get("rawSha256") or
                        previous.get("rawSha256") != current.get("rawSha256") or
                        (document is not None and self._document_has_local_work(workspace_id))):
                    # A delayed request contains line/column coordinates for the
                    # retired file. Keep the latest editor's cursor instead of
                    # applying old offsets or include context to different text.
                    navigation = None
                    definition = None
            self._closed_document_ids.discard(workspace_id)
            index = self._tab_index(workspace_id)
            if index >= 0:
                document = self._documents.get(workspace_id)
                if (document and document.get("available") and document.get("readOnly") and
                        document.get("captureBinding") == self._current_capture_binding()):
                    document["readOnly"] = False
                    document["editor"].SetReadOnly(False)
                if select:
                    self.tab_bar.setCurrentIndex(index)
                if navigation and workspace_id in self._documents:
                    self._documents[workspace_id]["editor"].GoToLine(*navigation)
                if definition:
                    self._definition_requested(*definition, owner_workspace_id=workspace_id)
                return True
            workspace = self._workspaces.get(workspace_id)
            if workspace is None or self._capture_binding != self._current_capture_binding():
                return False
            document = self._documents.get(workspace_id)
            if document is not None:
                if not self._can_resume_document(document, workspace, self._capture_binding):
                    return False
                document['workspace'] = workspace
                document['available'] = True
                document['captureBinding'] = self._capture_binding
                document['authorizationId'] = workspace.get('authorizationId', '')
                document['documentKind'] = str(
                    workspace.get('documentKind') or 'live-edit')
                document['readOnly'] = False
                document['editor'].SetNavigationEnabled(True)
                self._refresh_document_from_disk(workspace_id)
                index = self._insert_tab(workspace_id, document)
                if select:
                    self.tab_bar.setCurrentIndex(index)
                    self._tab_changed(index)
                if navigation:
                    document["editor"].GoToLine(*navigation)
                if definition:
                    self._definition_requested(*definition, owner_workspace_id=workspace_id)
                return True
            if len(self._documents) >= MAX_OPEN_DOCUMENTS:
                closed = False
                for candidate in list(self._documents):
                    document = self._documents.get(candidate)
                    if (candidate not in self._pinned and document and
                            not document.get("dirty")):
                        closed = self._close_document(candidate, prompt=False)
                        if closed:
                            break
                if not closed:
                    if not quiet:
                        QtWidgets.QMessageBox.warning(
                            self.top,
                            QtCore.QCoreApplication.translate(
                                "ReplayLabProjects", "Could not open Shader"),
                            QtCore.QCoreApplication.translate(
                                "ReplayLabProjects",
                                "Close an unmodified tab before opening another Shader."))
                    return False
            binding = self._current_capture_binding()
            opened = self._cached_source_document(workspace_id)
            if opened is None:
                self._pending_open[workspace_id] = {
                    "quiet": quiet, "select": select, "navigation": navigation}
                if definition:
                    self._pending_open[workspace_id]["definition"] = definition
                self.refresh(True, source_only=True)
                return False

            editor = self.ctx.CreateShaderCodeEditor(self.editor_stack)
            if editor is None:
                if not quiet:
                    QtWidgets.QMessageBox.warning(
                        self.top,
                        QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "Could not open Shader"),
                        QtCore.QCoreApplication.translate(
                            "ReplayLabProjects",
                            "The native Shader editor is unavailable."))
                return False
            editor_widget = editor.Widget()
            editor_widget.setObjectName(
                "replaylabShaderEditor_%s" % workspace_id)
            source_path = (
                opened.get("sourcePath", "") or
                workspace.get("sourcePath", "") or
                workspace.get("sourceRelativePath", "") or
                opened.get("fileName", "") or
                workspace.get("fileName", ""))
            extension = os.path.splitext(str(source_path or ""))[1].lower()
            language = (
                "glsl" if extension in (".glsl", ".vert", ".frag", ".geom",
                                         ".tesc", ".tese", ".comp") else
                "slang" if extension == ".slang" else "hlsl")
            if workspace.get("language") in ("hlsl", "glsl", "slang"):
                language = workspace["language"]
            self._loading_documents.add(workspace_id)
            editor.SetFileName(source_path)
            if hasattr(editor, "SetIncludeBaseDirectory"):
                editor.SetIncludeBaseDirectory(
                    os.path.dirname(str(opened.get("rootSourcePath") or workspace.get("rootSourcePath") or ""))
                    if workspace.get("documentKind") == "organization-preview" else "")
            editor.SetLanguage(language)
            editor.SetText(opened.get("text", ""))
            editor.SetSavePoint()
            self._loading_documents.discard(workspace_id)
            editor.SetTextChangedCallback(
                lambda value=workspace_id: self._document_text_changed(value))
            editor.SetSaveCallback(
                lambda value=workspace_id: self._save_document(value))
            editor.SetAddToChatCallback(
                lambda text, source_path, start_line, end_line,
                value=workspace_id: self._add_editor_selection_to_chat(
                    text, source_path, start_line, end_line, value))
            editor.SetDefinitionCallback(
                lambda file_name, line, column, value=workspace_id:
                self._definition_requested(file_name, line, column, value))
            self._next_document_token += 1
            document_kind = str(workspace.get("documentKind") or "live-edit")
            read_only = False
            document = {
                "workspaceId": workspace_id,
                "documentKind": document_kind,
                "readOnly": read_only,
                "captureBinding": binding,
                "documentToken": self._next_document_token,
                "authorizationId": opened.get("authorizationId", ""),
                "workspace": workspace,
                "sourcePath": opened.get("sourcePath", ""),
                "fileName": opened.get("fileName", ""),
                "entryPoint": opened.get("entryPoint", ""),
                "editor": editor,
                "editorWidget": editor_widget,
                "rawSha256": opened.get("rawSha256", ""),
                "modifiedNs": opened.get("modifiedNs", 0),
                "size": opened.get("size", 0),
                "newline": self._newline_style(opened.get("text", "")),
                "baselineEditorText": editor.GetText(),
                "textRevision": 0,
                "dirty": False,
                "conflict": False,
                "saveInFlight": False,
                "commitUnknown": False,
                "pendingSave": None,
                "saveQueued": False,
                "closeAfterSave": False,
                "available": True,
            }
            self._documents[workspace_id] = document
            if getattr(self, "visual_debug", None) is not None:
                self.visual_debug.bind_editor(document)
            if getattr(self, "debug_snapshots", None) is not None:
                try:
                    self.debug_snapshots.bind_document(document, workspace)
                except Exception as error:
                    self.debug_snapshots._error(error)
            self.editor_stack.addWidget(editor_widget)
            index = self._insert_tab(workspace_id, document)
            editor.SetReadOnly(read_only)
            if select:
                self.tab_bar.setCurrentIndex(index)
                # insertTab makes the first tab current before tabData exists,
                # so its immediate currentChanged callback cannot resolve the
                # document. Activate explicitly even when the index stayed 0.
                self._tab_changed(index)
            self._save_state()
            if navigation:
                editor.GoToLine(*navigation)
            if definition:
                self._definition_requested(*definition, owner_workspace_id=workspace_id)
            return True

        def _document_text_changed(self, workspace_id):
            if workspace_id in self._loading_documents:
                return
            document = self._documents.get(workspace_id)
            if document is None:
                return
            if getattr(self, "visual_debug", None) is not None:
                self.visual_debug.source_changed(workspace_id)
            if getattr(self, "debug_snapshots", None) is not None:
                self.debug_snapshots.source_changed(document)
            document["textRevision"] = int(
                document.get("textRevision", 0)) + 1
            self._sync_document_dirty(document)
            # A live compile-check underlines semantic errors in the
            # unsaved buffer. Restart the debounce so the background
            # compile runs only once the typist pauses.
            if (self._alive and
                    getattr(self, "check_debounce_timer", None) is not None and
                    document is self._current_document()):
                self.check_debounce_timer.start()
                if getattr(self, "analysis_debounce_timer", None) is not None:
                    self.analysis_debounce_timer.start()
                if getattr(self, "stage_rebuild_timer", None) is not None:
                    self.stage_rebuild_timer.start()

        def _document_changed(self, workspace_id, _modified=None):
            if workspace_id in self._loading_documents:
                return
            document = self._documents.get(workspace_id)
            if document is None:
                return
            self._sync_document_dirty(document)

        def navigate_shader_target(self, request):
            binding = self._current_capture_binding()
            if (not self._alive or self._shutting_down or not binding or
                    _normal_path(request.get("capturePath", "")) != _normal_path(binding.get("capturePath", ""))):
                return {"accepted": False, "status": "unavailable"}
            if (request.get("captureSessionId") and
                    request["captureSessionId"] != binding.get("captureSessionId")):
                return {"accepted": False, "status": "unavailable"}
            # Restoration/publication already populated these current-session
            # bindings. An in-flight background refresh must not delay opening
            # a known document or turn a transient refresh failure into a popup.
            if self._capture_binding == binding:
                matches = shader_target_workspace_ids(self._workspaces, request, binding)
                current = self._current_workspace_id()
                if current in matches:
                    matches.remove(current)
                    matches.insert(0, current)
                for workspace_id in matches:
                    if self.navigate_source_link({"workspaceId": workspace_id}):
                        return {"accepted": True, "status": "opened", "captureSessionId": binding.get("captureSessionId", "")}
            if request.get("refresh") is True:
                self._target_refresh_error = False
                self.refresh(force=True)
                return {"accepted": False, "status": "pending", "captureSessionId": binding.get("captureSessionId", "")}
            if self._snapshot_running:
                return {"accepted": False, "status": "pending"}
            if getattr(self, '_source_discovery_error', ''):
                # A failed read is not an absent Shader. Do not launch another
                # organization task or hide this diagnostic behind loading.
                return {"accepted": False, "status": "unavailable",
                        "diagnostic": self._source_discovery_error}
            if (self._capture_binding != binding or not self._snapshot or
                    getattr(self, "_target_refresh_error", False)):
                self.refresh(force=False)
                return {"accepted": False, "status": "pending", "captureSessionId": binding.get("captureSessionId", "")}
            matches = shader_target_workspace_ids(self._workspaces, request, binding)
            current = self._current_workspace_id()
            if current in matches:
                matches.remove(current)
                matches.insert(0, current)
            for workspace_id in matches:
                if self.navigate_source_link({"workspaceId": workspace_id}):
                    return {"accepted": True, "status": "opened"}
            # An existing but unreadable workspace must not trigger another AI organization.
            return {"accepted": False, "status": "unavailable" if matches else "missing"}

        def _select_source_link(self, workspace_id, request, line, column):
            symbol = str(request.get("symbol") or "")
            select_line = bool(request.get("selectLine"))
            expected = str(request.get("sourceSha256") or "")
            if not (symbol or select_line) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                return

            binding = self._capture_binding
            deadline = time.monotonic() + 15

            def select():
                if (not self._alive or self._shutting_down or
                        binding != self._current_capture_binding()):
                    return
                identity = self._canonical_workspace_id(workspace_id)
                document = self._documents.get(identity)
                if document is None:
                    if identity in self._pending_open and time.monotonic() < deadline:
                        QtCore.QTimer.singleShot(100, select)
                    return
                if (self._current_workspace_id() != identity or not document.get("available") or
                        document.get("captureBinding") != binding or document.get("rawSha256") != expected):
                    return

                editor = document["editor"]
                text = editor.GetText()
                if text != document.get("baselineEditorText"):
                    return
                from .source_link_selection import selection_range
                span = selection_range(text, line, column, symbol, select_line)
                if span:
                    editor.SelectRange(*span)

            QtCore.QTimer.singleShot(0, select)

        def navigate_source_link(self, request):
            """Open a chat citation by exact workspace/file identity, never by basename or EID."""
            if (not self._alive or self._shutting_down or
                    self._capture_binding != self._current_capture_binding()):
                return False
            workspace_id = str(request.get("workspaceId") or "")
            path = str(request.get("path") or "")
            related = str(request.get("file") or "")
            try:
                line, column = int(request.get("line", 1)), int(request.get("column", 1))
            except (ValueError, TypeError):
                return False
            if not 0 < line <= 10000000 or not 0 < column <= 1000000:
                return False
            if any("\x00" in value or len(value) > 8192 for value in (workspace_id, path, related)):
                return False
            if path:
                if not os.path.isabs(path):
                    return False
                key = _normal_path(path)
                matches = [identity for identity, model in self._workspaces.items()
                           if _normal_path(model.get("sourcePath", "")) == key]
                if workspace_id:
                    if workspace_id not in matches:
                        return False
                elif matches:
                    # A saved preview and its live editor may share one exact source path.
                    # Prefer the selected document; hidden retired buffers are not candidates.
                    selected = self._current_workspace_id()
                    workspace_id = selected if selected in matches else matches[0]
                else:
                    return False
            model = self._workspaces.get(workspace_id)
            if not model:
                return False
            if related:
                # Relative targets stay in this source unit. A compiler/LSP-resolved include
                # still goes through open_source_document on the existing background worker.
                related = related.replace("\\", "/")
                if (os.path.isabs(related) or ":" in related or
                        any(part in ("", ".", "..") for part in related.split("/"))):
                    return False
                source_path = str(model.get("sourcePath") or "")
                if related not in (model.get("sourceRelativePath"), os.path.basename(source_path)):
                    source_root = model.get("workspacePath")
                    if not source_root:
                        return False
                    requested = os.path.join(source_root, *related.split("/"))
                    if not self._open_workspace(workspace_id, quiet=True, select=True):
                        if workspace_id not in self._pending_open:
                            return False
                        self._pending_open[workspace_id]["definition"] = (requested, line, column)
                    else:
                        self._definition_requested(requested, line, column, workspace_id)
                    self.show()
                    return True
            accepted = self._open_workspace(
                workspace_id, quiet=True, select=True, navigation=(line, column))
            if accepted or workspace_id in self._pending_open:
                self.show()
                self._select_source_link(workspace_id, request, line, column)
                return True
            return False

        def _definition_requested(self, file_name, line, column, owner_workspace_id=None):
            """Open only a definition already authorised by this workspace."""

            requested = str(file_name or "")
            requested_key = os.path.normcase(
                os.path.normpath(requested)).replace("\\", "/")
            owner = self._documents.get(owner_workspace_id) or {}
            owner_model = owner.get("workspace") or self._workspaces.get(owner_workspace_id) or {}
            root_id = str(owner_model.get("sourceWorkspaceId") or owner_workspace_id or "")

            def _same_unit(workspace_id, workspace):
                return (not owner_workspace_id or
                        str(workspace.get("sourceWorkspaceId") or workspace_id) == root_id)

            def _matches(candidate):
                candidate = str(candidate or "")
                if not candidate or not requested_key:
                    return False
                candidate_key = os.path.normcase(
                    os.path.normpath(candidate)).replace("\\", "/")
                if candidate_key == requested_key:
                    return True
                return False

            # Every workspace commonly has the same basename and relative source path.
            # Prefer the exact native target identity; never let a suffix match select the
            # first unrelated Shader tab. Relative names are interpreted only for that owner.
            ordered_ids = list(self._documents)
            if owner_workspace_id in ordered_ids:
                ordered_ids.remove(owner_workspace_id)
                ordered_ids.insert(0, owner_workspace_id)
            for workspace_id in ordered_ids:
                document = self._documents[workspace_id]
                if not document.get("available"):
                    continue
                workspace = document.get("workspace", {})
                if not _same_unit(workspace_id, workspace):
                    continue
                candidates = (
                    document.get("sourcePath", ""),
                    workspace.get("sourcePath", ""),
                )
                if workspace_id == owner_workspace_id and not os.path.isabs(requested):
                    candidates += (document.get("fileName", ""),
                                   workspace.get("sourceRelativePath", ""),
                                   workspace.get("fileName", ""))
                if any(_matches(candidate) for candidate in candidates):
                    index = self._tab_index(workspace_id)
                    if index >= 0:
                        self.tab_bar.setCurrentIndex(index)
                    document["editor"].GoToLine(
                        max(1, int(line or 1)), max(1, int(column or 1)))
                    return

            models = dict(self._workspaces)
            for workspace_id, workspace in models.items():
                if not _same_unit(workspace_id, workspace):
                    continue
                candidates = (
                    workspace.get("sourcePath", ""),
                )
                if workspace_id == owner_workspace_id and not os.path.isabs(requested):
                    candidates += (workspace.get("sourceRelativePath", ""),
                                   workspace.get("fileName", ""))
                if not any(_matches(candidate) for candidate in candidates):
                    continue
                if self._open_workspace(workspace_id):
                    document = self._documents.get(workspace_id)
                    if document:
                        document["editor"].GoToLine(
                            max(1, int(line or 1)), max(1, int(column or 1)))
                    return
                if workspace_id in self._pending_open:
                    self._pending_open[workspace_id]["navigation"] = (
                        max(1, int(line or 1)), max(1, int(column or 1)))
                    return

            if owner and owner.get("available") and os.path.isabs(requested):
                self._open_related_definition(owner_workspace_id, requested, line, column)
                return

            self.status_label.setText(_format_message(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Definition is outside the authorized Shader Workspace: %1"),
                os.path.basename(requested) or requested))

        def _open_related_definition(self, owner_id, requested, line, column):
            """Authorize and read an unopened include on a worker, never the UI thread."""
            owner = self._documents.get(owner_id)
            if owner is None or owner.get("captureBinding") != self._current_capture_binding():
                return
            model = self._workspaces.get(owner_id) or owner.get("workspace") or {}
            request = {"owner": owner_id, "documentToken": owner.get("documentToken"),
                       "generation": self._snapshot_generation,
                       "captureBinding": dict(owner["captureBinding"]),
                       "projectSelectionPath": self.project_selection_path,
                       "workspacePath": model.get("workspacePath"),
                       "sourceWorkspaceId": model.get("sourceWorkspaceId") or owner_id,
                       "path": requested, "line": max(1, int(line or 1)),
                       "column": max(1, int(column or 1))}

            def read():
                metadata = self._save_client.open_source_document(
                    request["workspacePath"], request["sourceWorkspaceId"], request["path"],
                    request["captureBinding"])
                return _shader_console_protocol.load_related_shader_source_document(
                    metadata, capture_binding=request["captureBinding"])

            def ready(opened, error):
                current = self._documents.get(owner_id)
                if (not self._alive or self._shutting_down or current is None or
                        not current.get("available") or request["generation"] != self._snapshot_generation or
                        current.get("documentToken") != request["documentToken"] or
                        request["captureBinding"] != self._current_capture_binding() or
                        request["projectSelectionPath"] != self.project_selection_path):
                    return
                if error or not opened:
                    self.status_label.setText(
                        QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "Could not open Shader") + ": " + str(error or ""))
                    return
                target_id = opened["workspaceId"]
                existing = self._documents.get(target_id)
                if existing is None or not any(existing.get(key) for key in (
                        "dirty", "saveInFlight", "commitUnknown", "pendingSave", "conflict")):
                    if not self.show_organization_preview(opened):
                        return
                target = self._documents.get(target_id)
                if target is not None and target.get("available"):
                    index = self._tab_index(target_id)
                    if index >= 0:
                        self.tab_bar.setCurrentIndex(index)
                    target["editor"].GoToLine(request["line"], request["column"])

            self._snapshot_executor.submit(self._snapshot_owner, read, ready)

        def _sync_document_dirty(self, document):
            # Read-only is a transient UI condition during capture/project
            # transitions. It must never erase the user's unsaved working copy.
            pending = document.get("pendingSave")
            if (pending is not None and
                    (document.get("saveInFlight") or
                     document.get("commitUnknown"))):
                baseline = pending.get("submittedEditorText", "")
            else:
                baseline = document.get("baselineEditorText", "")
            document["dirty"] = (
                document["editor"].GetText() != baseline)
            workspace_id = document["workspaceId"]
            self._update_tab(workspace_id)
            if self._current_workspace_id() == workspace_id:
                self._refresh_current_view()

        def _update_tab(self, workspace_id):
            index = self._tab_index(workspace_id)
            document = self._documents.get(workspace_id)
            if index >= 0 and document:
                self.tab_bar.setTabText(index, self._tab_title(document))
                self.tab_bar.configure_pin_button(
                    index, workspace_id, workspace_id in self._pinned)

        def _current_workspace_id(self):
            index = self.tab_bar.currentIndex()
            return str(self.tab_bar.tabData(index) or "") if index >= 0 else ""

        def _current_document(self):
            return self._documents.get(self._current_workspace_id())

        def _source_hierarchy(self, document):
            revision = (document.get('textRevision', 0), document.get('flowBufferReset', 0))
            if (document.get('hierarchyRevision') == revision and
                    document.get('sourceFlowKey') == self._source_flow_key(document)):
                return document.get('hierarchy')
            self._request_source_flow(document)
            return None

        def _source_flow_key(self, document):
            workspace = self._document_model(document['workspaceId']) or {}
            graph = workspace.get('flowGraph') or {}
            return (document['documentToken'], document.get('textRevision', 0),
                    document.get('flowBufferReset', 0), str(graph.get('graphId') or ''),
                    str(workspace.get('entryPoint') or ''),
                    json.dumps(workspace.get('semanticOverview'), sort_keys=True, ensure_ascii=False),
                    document.get('navigationSaveRevision', 0))

        def _request_source_flow(self, document):
            key = self._source_flow_key(document)
            if document.get('sourceFlowKey') == key:
                return
            if self._flow_request is not None:
                if self._flow_request['key'] != key:
                    self._flow_request['stop'].set()
                return
            workspace = self._document_model(document['workspaceId']) or {}
            request = {
                'key': key, 'workspaceId': document['workspaceId'],
                'generation': self._snapshot_generation,
                'captureBinding': document.get('captureBinding'),
                'sourceText': str(document['editor'].GetText()),
                'sourceIdentity': {'workspaceId': document['workspaceId'],
                                   'documentId': document['workspaceId'],
                                   'path': document['sourcePath'],
                                   'sourceRevision': str(key[1:3])},
                'entryPoint': str(workspace.get('entryPoint') or ''),
                'semanticOverview': copy.deepcopy(workspace.get('semanticOverview')),
                'hasLiveOutlineAnchors': document.get('flowOutlineAnchors') is not None,
                'navigationStorage': {
                    'root': QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppLocalDataLocation),
                    'identity': _shader_debug_snapshots.snapshot_identity(document, workspace)},
                'graph': workspace.get('flowGraph'), 'stop': threading.Event(),
            }
            self._flow_request = request
            self._snapshot_executor.submit(
                self._snapshot_owner, lambda: _build_source_flow_projection(request),
                lambda result, error: self._source_flow_ready(request, result, error))

        def _source_flow_ready(self, request, result, error):
            if self._flow_request is request:
                self._flow_request = None
            if not self._alive or self._shutting_down:
                return
            document = self._documents.get(request['workspaceId'])
            if (document is not None and document.get('available') and
                    not request['stop'].is_set() and
                    request['generation'] == self._snapshot_generation and
                    request['captureBinding'] == self._current_capture_binding() and
                    request['key'] == self._source_flow_key(document)):
                result = result or {}
                if not request.get('outlineAnchorsAdjusted'):
                    overview = request.get('semanticOverview')
                    tracker = document.get('flowOutlineAnchors')
                    if isinstance(overview, dict) and result.get('hierarchy') is not None:
                        if tracker is None:
                            tracker = _shader_outline_anchors.LiveOutlineAnchors(document['editor'])
                            document['flowOutlineAnchors'] = tracker
                        adjusted = tracker.update(result['hierarchy'], overview, result.get('navigationOverview'))
                        if adjusted != overview:
                            # Native byte ranges belong to the GUI thread. Parse
                            # once off-thread, map those ranges here, then project
                            # the adjusted outline off-thread using that same index.
                            followup = dict(request, semanticOverview=adjusted,
                                            navigationOriginal=overview,
                                            parsedHierarchy=result['hierarchy'],
                                            outlineAnchorsAdjusted=True)
                            self._flow_request = followup
                            self._snapshot_executor.submit(self._snapshot_owner,
                                lambda: _build_source_flow_projection(followup),
                                lambda projected, detail: self._source_flow_ready(followup, projected, detail))
                            return
                    elif overview is None and tracker is not None:
                        tracker.clear()
                        document.pop('flowOutlineAnchors', None)
                document['sourceFlowKey'] = request['key']
                document['sourceFlowGraph'] = result.get('graph')
                document['sourceFlowState'] = result.get('state', 'invalid-source')
                document['sourceFlowError'] = str(error or result.get('error') or '')[:1024]
                document['hierarchyRevision'] = request['key'][1:3]
                document['hierarchy'] = result.get('hierarchy')
                document['stageOutline'] = result.get('stageOutline', [])
                document['stageOutlineState'] = result.get('stageOutlineState', {})
                document['stageOutlineSourceSha256'] = result.get('stageOutlineSourceSha256', '')
                document['stageOutlineOverviewSha256'] = result.get('stageOutlineOverviewSha256', '')
            current = self._current_document()
            if current is not None and current.get('available'):
                self._sync_flow_graph(current)

        def _function_line_span(self, hierarchy, record):
            start = hierarchy.tokens[record['declaration']].start()
            end = hierarchy.tokens[record['end']].start()
            return (hierarchy.source.count('\n', 0, start) + 1,
                    hierarchy.source.count('\n', 0, end) + 1)

        def _flow_token_anchor(self, document, hierarchy, line, column):
            editor = document['editor']
            if not all(callable(getattr(editor, name, None)) for name in (
                    'CreateRangeAnchor', 'GetRangeAnchorRange', 'RemoveRangeAnchor')):
                return None
            if not 1 <= line <= len(hierarchy.lines):
                return None
            position = hierarchy.lines[line - 1] + max(0, column - 1)
            token = next((token for token in hierarchy.tokens
                          if token.end() > position), None)
            if token is None or '\n' in hierarchy.source[position:token.start()]:
                return None
            return editor.CreateRangeAnchor(
                len(hierarchy.source[:token.start()].encode('utf-8')),
                len(hierarchy.source[:token.end()].encode('utf-8')))

        def _flow_anchor_position(self, document, hierarchy, anchor):
            if hierarchy is None:
                return None
            if anchor.get('rangeMarker') is not None:
                span = document['editor'].GetRangeAnchorRange(anchor['rangeMarker'])
                if len(span) != 2:
                    return None
                data = hierarchy.source.encode('utf-8')
                if not 0 <= span[0] < span[1] <= len(data):
                    return None
                try:
                    prefix = data[:span[0]].decode('utf-8')
                except UnicodeDecodeError:
                    return None
                line = prefix.count('\n') + 1
                # Parser columns count Unicode characters; Scintilla GoToLine
                # expects visual columns, with the editor's four-column tabs.
                column = len(prefix.rsplit('\n', 1)[-1].expandtabs(4)) + 1
                return line, column, len(prefix)
            line = document['editor'].GetLineAnchorLine(anchor['marker'])
            return (line, anchor.get('column', 1), None) if line else None

        def _function_bookmark(self, document, hierarchy, function_id):
            record = hierarchy.functions.get(function_id)
            if record is None:
                return -1
            bookmarks = document.setdefault('flowFunctionBookmarks', {})
            ranges = document.setdefault('flowFunctionRanges', {})
            if (function_id not in bookmarks or
                    not document['editor'].GetLineAnchorLine(bookmarks[function_id]) or
                    (bookmarks[function_id] in ranges and not
                     document['editor'].GetRangeAnchorRange(ranges[bookmarks[function_id]]))):
                position = hierarchy.tokens[record['declaration']].start()
                line = hierarchy.source.count('\n', 0, position) + 1
                # A new projection may bind a recreated declaration. Old graph anchors keep
                # their own invalid handle and must never be redirected to this new marker.
                bookmarks[function_id] = document['editor'].CreateLineAnchor(line)
                handle = self._flow_token_anchor(document, hierarchy, line,
                    position - hierarchy.lines[line - 1] + 1)
                if handle is not None:
                    ranges[bookmarks[function_id]] = handle
            return bookmarks[function_id]

        def _bookmarked_function(self, document, hierarchy, marker):
            if hierarchy is None:
                return None
            handle = document.get('flowFunctionRanges', {}).get(marker)
            if handle is not None:
                span = document['editor'].GetRangeAnchorRange(handle)
                if len(span) != 2:
                    return None
                matches = [record for record in hierarchy.functions.values()
                           if record is not None and span[0] <= len(hierarchy.source[
                               :hierarchy.tokens[record['declaration']].start()].encode('utf-8')) < span[1]]
                return matches[0] if len(matches) == 1 else None
            line = document['editor'].GetLineAnchorLine(marker)
            if not line:
                return None
            matches = [record for record in hierarchy.functions.values() if record is not None and
                       hierarchy.source.count('\n', 0, hierarchy.tokens[record['declaration']].start()) + 1 == line]
            # A bookmark identifies the edited declaration even after a parameter/function rename.
            # Multiple declarations on one line are ambiguous; do not guess between them.
            return matches[0] if len(matches) == 1 else None

        def _prune_flow_anchors(self, document, graph=None, max_anchors=8192):
            def graph_anchors(value):
                if not isinstance(value, dict):
                    return {}
                value = value.get('flowGraph', value)
                return {str(node.get('source', {}).get('anchorId')): str(node.get('kind') or '')
                        for node in value.get('nodes', [])[:256]
                        if isinstance(node, dict) and isinstance(node.get('source'), dict)
                        and node['source'].get('anchorId')}

            anchors = document.setdefault('flowAnchors', {})
            bookmarks = document.setdefault('flowFunctionBookmarks', {})
            function_ranges = document.setdefault('flowFunctionRanges', {})
            marker_fields = ('marker', 'functionMarker', 'expandMarker', 'callOwnerMarker')
            owned = {anchor.get(field, -1) for anchor in anchors.values()
                     for field in marker_fields}
            owned.update(bookmarks.values())
            owned.update(function_ranges)
            owned_ranges = {anchor['rangeMarker'] for anchor in anchors.values()
                            if anchor.get('rangeMarker') is not None}
            owned_ranges.update(function_ranges.values())
            # The panel owns a bounded, inert history of these exact graph dictionaries.
            # Inspect only issued anchor IDs; neither source paths nor graph labels are authority.
            history = getattr(getattr(self.flow_graph, '_history', None), '_entries', ())
            current = graph_anchors(graph)
            retained = set(current)
            # Higher values survive the hard cap first. The top-level graph must
            # remain reachable; current function/call nodes come next, followed
            # by their Input/Output boundaries and then older history. Boundaries
            # still navigate while retained, but losing one under an exceptional
            # cap is less harmful than losing the function expansion itself.
            retention_priority = {
                key: (2 if kind in ('input', 'output') else 3)
                for key, kind in current.items()}
            for index, entry in enumerate(history):
                issued = graph_anchors(entry[0])
                retained.update(issued)
                if index == 0:
                    for key in issued:
                        retention_priority[key] = 4  # Keep the top-level algorithm reachable.
                else:
                    for key, kind in issued.items():
                        retention_priority.setdefault(
                            key, 0 if kind in ('input', 'output') else 1)
            for key in list(anchors):
                if key not in retained:
                    del anchors[key]
            # Unusually large histories must not make every native edit scan unlimited markers.
            # An expired history node reports unavailable; it is never rebound by an old line.
            excess = max(0, len(anchors) - max(1, int(max_anchors)))
            oldest = sorted(anchors, key=lambda key: (
                retention_priority.get(key, 0), anchors[key].get('sequence', 0)))
            for key in oldest[:excess]:
                del anchors[key]
            referenced = {anchor.get(field, -1) for anchor in anchors.values()
                          for field in marker_fields}
            for function_id, marker in list(bookmarks.items()):
                if marker not in referenced:
                    del bookmarks[function_id]
            referenced.update(bookmarks.values())
            for marker in list(function_ranges):
                if marker not in referenced:
                    del function_ranges[marker]
            referenced_ranges = {anchor['rangeMarker'] for anchor in anchors.values()
                                 if anchor.get('rangeMarker') is not None}
            referenced_ranges.update(function_ranges.values())
            for handle in owned_ranges - referenced_ranges:
                document['editor'].RemoveRangeAnchor(handle)
            for marker in owned - referenced:
                if marker >= 0:
                    document['editor'].RemoveLineAnchor(marker)

        def _anchor_graph(self, document, graph, hierarchy, prune=True):
            anchors = document.setdefault('flowAnchors', {})
            local_graph = graph.get('projectionKind') == 'current-source-function'
            for node in graph.get('nodes', [])[:256]:
                location = node.get('source')
                if not isinstance(location, dict):
                    continue
                # Graph locations are navigation metadata, never authority to open a path.
                location['documentId'] = document['workspaceId']
                location['workspaceId'] = document['workspaceId']
                symbol = str(location.get('symbol') or node.get('name') or '')
                key = '%s:%s:%s' % (document['documentToken'],
                                     graph.get('graphId', ''), node.get('id', ''))
                anchored = anchors.get(key)
                if anchored is not None:
                    # Reprojecting an accepted graph after changing tabs must preserve an
                    # edited/renamed declaration's identity. A deleted marker stays invalid.
                    record = self._bookmarked_function(
                        document, hierarchy, anchored['functionMarker'])
                    location['anchorId'] = key
                    if (local_graph and hierarchy and
                            graph.get('sourceSha256') == hierarchy.source_sha256 and
                            (record is None or self._flow_anchor_position(document, hierarchy, anchored) is None)):
                        # This is a freshly parsed local graph, possibly after undo/recreation
                        # produced the exact same text hash. Give it a new opaque identity;
                        # retained history still holds the old, invalid anchor.
                        replacement = hierarchy.functions.get(graph.get('functionId'))
                        if replacement is not None:
                            key += ':new:%s' % (document.get('flowAnchorSequence', 0) + 1)
                            anchored, record = None, replacement
                else:
                    function_id = (graph.get('functionId') if local_graph else
                                   hierarchy.function_identity(symbol)) if hierarchy else None
                    record = hierarchy.functions.get(function_id) if hierarchy else None
                if record is None:
                    node.pop('functionId', None)
                    if anchored is None:
                        location.pop('anchorId', None)
                    continue
                function_id = record['id']
                symbol = record['name']
                if anchored is None:
                    first, last = self._function_line_span(hierarchy, record)
                    line = int(location.get('line', first) or first)
                    # Only a graph derived from this buffer can supply an initial line.
                    # For an older project graph, relocate by the unique function identity.
                    if (graph.get('sourceSha256') != hierarchy.source_sha256 or
                            not first <= line <= last):
                        line = first
                    column = max(1, int(location.get('column', 1) or 1))
                    # A top-level pipeline stage navigates to where it is called
                    # in the entry body (its use), not into its definition. The
                    # functionMarker below still tracks the declaration, so
                    # double-click expansion and rename tracking are unaffected.
                    call_owner_id = None
                    if not local_graph:
                        declaration = hierarchy.tokens[record['declaration']].start()
                        line = hierarchy.source.count('\n', 0, declaration) + 1
                        column = declaration - hierarchy.lines[line - 1] + 1
                        call = hierarchy.call_site(symbol)
                        if call is not None and call.get('ownerFunctionId'):
                            line = call['line']
                            column = max(1, int(call.get('column', 1) or 1))
                            call_owner_id = call['ownerFunctionId']
                    marker = document['editor'].CreateLineAnchor(line)
                    document['flowAnchorSequence'] = document.get('flowAnchorSequence', 0) + 1
                    anchored = {'marker': marker, 'functionId': function_id,
                                'functionMarker': self._function_bookmark(document, hierarchy, function_id),
                                'expansionKind': ('call' if local_graph and
                                                  node.get('kind') == 'function-call'
                                                  else 'stage'),
                                'sequence': document['flowAnchorSequence'],
                                'symbol': symbol, 'column': column}
                    handle = self._flow_token_anchor(document, hierarchy, line, column)
                    if handle is not None:
                        anchored['rangeMarker'] = handle
                    if node.get('helperGroupId'):
                        anchored['helperGroupId'] = node['helperGroupId']
                    if call_owner_id:
                        anchored['callOwnerId'] = call_owner_id
                        anchored['callOwnerMarker'] = self._function_bookmark(
                            document, hierarchy, call_owner_id)
                        anchored['callSymbol'] = symbol
                    anchors[key] = anchored
                location['anchorId'] = key
                location['symbol'] = symbol
                location['functionSignature'] = record['signature'][:1024]
                if not local_graph:
                    node['functionId'] = function_id
            if prune:
                self._prune_flow_anchors(document, graph)
            return graph

        def _open_function_graph(self, document, hierarchy, function_id, direct=False,
                                 helper_group_id=None):
            graph = hierarchy.function_graph(function_id, functions_only=True,
                                             helper_group_id=helper_group_id)
            ancestors = None
            if direct:
                ancestors = []
                root = copy.deepcopy(document.get('sourceFlowGraph') or {})
                entry_id = root.get('functionId')
                path = hierarchy.function_path(entry_id, function_id)
                if path and root:
                    self._anchor_graph(document, root, hierarchy, prune=False)
                    ancestors.append((root, {'graph_key': '%s:%s' % (
                        document['workspaceId'], root.get('graphId', 'root'))}))
                    for parent_id in path[1:-1]:
                        parent = hierarchy.function_graph(parent_id, functions_only=True)
                        self._anchor_graph(document, parent, hierarchy, prune=False)
                        ancestors.append((parent, {'graph_key': '%s:%s' % (
                            document['workspaceId'], parent['graphId'])}))
                # No caller is a source-proven root, not an unrelated visited function.
                if function_id == entry_id:
                    ancestors = []
            self._anchor_graph(document, graph, hierarchy, prune=False)
            self.flow_graph.open_subgraph(graph, graph_key='%s:%s' % (
                document['workspaceId'], graph['graphId']), ancestors=ancestors)
            self._prune_flow_anchors(document, graph)
            return graph

        def _resolve_flow_history(self, graph, options):
            """Rebuild a parent from the current buffer and issued native bookmarks."""
            document = self._current_document()
            if (not document or not document.get('available') or
                    document.get('captureBinding') != self._current_capture_binding()):
                return None
            hierarchy = self._source_hierarchy(document)
            if hierarchy is None:
                self._flow_graph_target_unavailable()
                return None
            summary = next((node for node in graph.get('nodes', [])
                            if node.get('kind') == 'function-summary'), None)
            anchor = document.get('flowAnchors', {}).get(
                (summary or {}).get('source', {}).get('anchorId'))
            function = self._bookmarked_function(document, hierarchy,
                        anchor['functionMarker']) if anchor else None
            if function is None:
                self._flow_graph_target_unavailable()
                return None
            try:
                is_root = str(graph.get('graphId') or '').startswith('root:')
                refreshed = hierarchy.function_graph(function['id'], functions_only=True,
                    expandable_self=is_root, helper_group_id=graph.get('helperGroupId'))
                if is_root:
                    refreshed['graphId'] = 'root:' + refreshed['graphId']
                    refreshed['functionName'] = ''
                self._anchor_graph(document, refreshed, hierarchy, prune=False)
            except ValueError:
                self._flow_graph_target_unavailable()
                return None
            return refreshed, {'graph_key': '%s:%s' % (
                document['workspaceId'], refreshed['graphId'])}

        def _sync_flow_graph(self, document=None):
            document = document if document is not None else self._current_document()
            if document is None or not document.get('available'):
                signature = ('shader-workspace-empty',)
                if signature != self._flow_graph_signature:
                    self._flow_graph_signature = signature
                    self.flow_graph.clear_graph(graph_key='shader-workspace-empty')
                return
            workspace = self._document_model(document['workspaceId']) or {}
            graph = workspace.get('flowGraph')
            graph_key = '%s:%s' % (document['workspaceId'],
                                   str((graph or {}).get('graphId') or 'unavailable'))
            # Text edits move native bookmarks. Rebuilding the scene on every keystroke would
            # reset the user's function exploration and detach those bookmarks.
            # Metadata may change while the source bytes remain identical.
            # Refresh the descriptions/grouping without treating every typed
            # character as permission to discard the current exploration.
            key = self._source_flow_key(document)
            signature = (graph_key, document['documentToken'], document.get('flowBufferReset', 0),
                         key[4:])
            if signature == self._flow_graph_signature:
                if (not document.get('flowProjectionIncomplete') and
                        document.get('sourceFlowState') == 'available'):
                    if document.get('renderedFlowRevision') != (
                            document.get('textRevision', 0), document.get('flowBufferReset', 0)):
                        self.flow_graph.mark_source_changed()
                    return
            self._flow_graph_signature = signature
            if document.get('sourceFlowKey') != key:
                document['flowProjectionIncomplete'] = True
                self._request_source_flow(document)
                if document.get('flowPendingKey') != key:
                    document['flowPendingKey'] = key
                    self.flow_graph.clear_graph(graph_key=graph_key, state='building')
                return
            hierarchy = document.get('hierarchy')
            projection = document.get('sourceFlowGraph')
            document['flowProjectionIncomplete'] = False
            if not isinstance(projection, dict):
                self.flow_graph.clear_graph(
                    graph_key=graph_key, state=document.get('sourceFlowState', 'invalid-source'),
                    detail=document.get('sourceFlowError', ''))
                return
            self._anchor_graph(document, projection, hierarchy)
            self.flow_graph.set_graph(projection, graph_key=graph_key)
            document['renderedFlowRevision'] = key[1:3]
            self._prune_flow_anchors(document, projection)

        def _flow_graph_target_unavailable(self):
            if getattr(self, '_flow_request', None) is not None:
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    'ReplayLabProjects', 'Building call flow from the current source...'))
                return
            self.status_label.setText(QtCore.QCoreApplication.translate(
                'ReplayLabProjects',
                'The linked calculation was removed or is incomplete. Finish editing the function before navigating.'))

        def _flow_graph_node_activated(self, payload):
            if not isinstance(payload, dict):
                return
            document = self._current_document()
            if (document is None or not document.get('available') or
                    document.get('captureBinding') != self._current_capture_binding()):
                return
            anchor = document.get('flowAnchors', {}).get(str(payload.get('anchorId') or ''))
            if anchor is None:
                self._flow_graph_target_unavailable()
                return
            hierarchy = self._source_hierarchy(document)
            record = self._bookmarked_function(document, hierarchy, anchor['functionMarker'])
            position = self._flow_anchor_position(document, hierarchy, anchor)
            if record is None or position is None:
                self._flow_graph_target_unavailable()
                return
            line, column, offset = position
            call_owner_id = anchor.get('callOwnerId')
            if call_owner_id:
                # A pipeline-stage anchor points at the call site in the entry
                # body. Validate the anchor still sits inside that owner function
                # and still invokes the stage before navigating; otherwise report
                # the target unavailable instead of jumping to unrelated code.
                owner = (self._bookmarked_function(document, hierarchy, anchor['callOwnerMarker'])
                         if 'callOwnerMarker' in anchor else hierarchy.functions.get(call_owner_id))
                if owner is None:
                    self._flow_graph_target_unavailable()
                    return
                first, last = self._function_line_span(hierarchy, owner)
                if (not first <= line <= last or
                        (offset is not None and not
                         hierarchy.tokens[owner['start']].start() <= offset <
                         hierarchy.tokens[owner['end']].end()) or
                        not hierarchy.line_calls(line, record['name'])):
                    self._flow_graph_target_unavailable()
                    return
                if offset is not None:
                    token = next((i for i, token in enumerate(hierarchy.tokens)
                                  if token.start() == offset), -1)
                    if (token < 0 or hierarchy.words[token] != record['name'] or
                            hierarchy.words[token + 1:token + 2] != ['(']):
                        self._flow_graph_target_unavailable()
                        return
                else:
                    # Older native editors expose line anchors only. Resolve a
                    # unique current call on that line instead of a stale column.
                    calls = [token for i, token in enumerate(hierarchy.tokens)
                             if hierarchy.words[i] == record['name'] and
                             hierarchy.words[i + 1:i + 2] == ['('] and
                             hierarchy.source.count('\n', 0, token.start()) + 1 == line]
                    if len(calls) != 1:
                        self._flow_graph_target_unavailable()
                        return
                    column = len(hierarchy.source[hierarchy.lines[line - 1]:calls[0].start()].expandtabs(4)) + 1
                document['editor'].GoToLine(line, column)
                return
            first, last = self._function_line_span(hierarchy, record)
            if (not first <= line <= last or
                    (offset is not None and not
                     hierarchy.tokens[record['declaration']].start() <= offset <
                     hierarchy.tokens[record['end']].end())):
                self._flow_graph_target_unavailable()
                return
            document['editor'].GoToLine(line, column)

        def _flow_graph_subgraph_requested(self, payload):
            document = self._current_document()
            if (not isinstance(payload, dict) or document is None or
                    not document.get('available') or
                    document.get('captureBinding') != self._current_capture_binding()):
                return
            # Only nodes issued by this panel can request an expansion.
            anchor = document.get('flowAnchors', {}).get(str(payload.get('anchorId') or ''))
            if anchor is None:
                self._flow_graph_target_unavailable()
                return
            hierarchy = self._source_hierarchy(document)
            try:
                if hierarchy is None:
                    raise ValueError('Source is incomplete')
                owner = self._bookmarked_function(document, hierarchy, anchor['functionMarker'])
                if owner is None:
                    raise ValueError('Linked function was removed')
                function = owner
                if anchor['expansionKind'] == 'call':
                    position = self._flow_anchor_position(document, hierarchy, anchor)
                    line, column, offset = position if position is not None else (0, 0, None)
                    current_graph = hierarchy.function_graph(owner['id'], functions_only=True)
                    matches = [node for node in current_graph['nodes']
                               if node.get('kind') == 'function-call' and
                               node.get('sourceRange', {}).get('startLine', 0) <= line <=
                               node.get('sourceRange', {}).get('endLine', -1)] if line else []
                    if offset is not None:
                        matches = [node for node in matches if
                                   hierarchy.lines[node['source']['line'] - 1] +
                                   node['source'].get('column', 1) - 1 == offset]
                    if len(matches) != 1 or not matches[0].get('functionId'):
                        raise ValueError('Linked call was removed or is ambiguous')
                    function = hierarchy.functions.get(matches[0]['functionId'])
                    if function is None:
                        raise ValueError('Linked callee was removed')
                helper_group_id = anchor.get('helperGroupId')
                graph = self._open_function_graph(document, hierarchy, function['id'],
                    helper_group_id=helper_group_id)
            except ValueError:
                self._flow_graph_target_unavailable()
                return
            # Function expansion also locates its definition. The stage outline
            # can then follow the same source position, including shared helpers.
            declaration = hierarchy.tokens[function['declaration']].start()
            line = hierarchy.source.count('\n', 0, declaration) + 1
            column = len(hierarchy.source[hierarchy.lines[line - 1]:declaration].expandtabs(4)) + 1
            document['editor'].GoToLine(line, column)

        def _tab_changed(self, index):
            if index >= 0:
                workspace_id = str(self.tab_bar.tabData(index) or "")
                document = self._documents.get(workspace_id)
                if document:
                    self.editor_stack.setCurrentWidget(document["editorWidget"])
                    tree_item = self._tree_items.get(workspace_id)
                    if tree_item is not None:
                        self.file_tree.setCurrentItem(tree_item)
            else:
                self.editor_stack.setCurrentWidget(self.empty_page)
            self._update_replace_button()
            self._sync_flow_graph()
            self._save_state()
            self._refresh_current_view()
            if getattr(self, "debug_snapshots", None) is not None:
                self.debug_snapshots.refresh()

        # ---- original / compiled switch -------------------------------------
        def _update_conversion_button(self):
            document = self._current_document()
            glsl = bool(document and os.path.splitext(str(document.get('sourcePath') or ''))[1].lower()
                        in ('.glsl', '.vert', '.frag', '.geom', '.tesc', '.tese', '.comp'))
            self.convert_button.setVisible(glsl)
            self.convert_button.setEnabled(bool(glsl and document.get('available')
                and document.get('documentKind') not in ('source-include', 'capture-source')
                and not document.get('conversionInFlight')
                and not document.get('saveInFlight') and not document.get('commitUnknown')))

        def _convert_current_to_hlsl(self):
            document = self._current_document()
            if (not document or not document.get('available')
                    or document.get('conversionInFlight') or document.get('switchInFlight')
                    or document.get('documentKind') in ('source-include', 'capture-source')):
                return
            extension = os.path.splitext(str(document.get('sourcePath') or ''))[1].lower()
            if extension not in ('.glsl', '.vert', '.frag', '.geom', '.tesc', '.tese', '.comp'):
                return
            if (any(document.get(key) for key in ('dirty', 'saveInFlight', 'commitUnknown',
                                                  'pendingSave', 'conflict'))
                    or document['editor'].GetText() != document.get('baselineEditorText')):
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Save the current Shader and resolve its errors before converting to HLSL."))
                return
            binding = self._current_capture_binding()
            if not binding or document.get('captureBinding') != binding:
                return
            path = self._document_workspace_path(document['workspaceId'], document)
            if not path or not document.get('rawSha256'):
                return
            request = {'workspaceId': document['workspaceId'],
                       'documentToken': document.get('documentToken'),
                       'sourceSha256': document['rawSha256'], 'binding': dict(binding),
                       'projectSelectionPath': self.project_selection_path}
            parameters = {'source_workspace_path': path,
                          'expected_source_sha256': document['rawSha256']}
            document['conversionInFlight'] = request
            self._update_conversion_button()
            self.status_label.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Converting and validating HLSL…"))
            client = self._save_client

            def convert():
                return client.live_edit_operation('convert-hlsl', parameters, request['binding'])

            self._snapshot_executor.submit(self._snapshot_owner, convert,
                lambda value, error: self._hlsl_conversion_ready(request, value, error))

        def _hlsl_conversion_ready(self, request, value, error):
            if not self._alive or self._shutting_down:
                return
            document = self._documents.get(request['workspaceId'])
            if document is None or document.get('conversionInFlight') is not request:
                return
            document.pop('conversionInFlight', None)
            self._update_conversion_button()
            if (not document.get('available')
                    or document.get('documentToken') != request['documentToken']
                    or request['binding'] != self._current_capture_binding()
                    or request['sourceSha256'] != document.get('rawSha256')
                    or request['projectSelectionPath'] != self.project_selection_path):
                return
            if error:
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "HLSL conversion failed: %1").replace('%1', str(error)[:4096]))
                return
            if not isinstance(value, dict) or value.get('readyForEditing') is not True:
                validation = value.get('abiValidation') if isinstance(value, dict) else None
                detail = str(validation.get('error') or '')[:4096] if isinstance(validation, dict) else ''
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "HLSL conversion failed: %1").replace('%1', detail)
                    if detail else QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "HLSL conversion did not pass validation."))
                return
            self.status_label.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "HLSL conversion completed; the original source is preserved."))
            self.refresh(True)

        def _document_workspace_path(self, workspace_id, document):
            model = self._document_model(workspace_id) or {}
            return str(model.get("workspacePath") or (document or {}).get("workspacePath") or "")

        def _applied_replacement_id(self, workspace_id, document):
            if document and document.get("appliedReplacementId"):
                return str(document["appliedReplacementId"])
            model = self._document_model(workspace_id) or {}
            live = model.get("liveEdit") or {}
            last = live.get("lastApply") if isinstance(live, dict) else None
            replacement = (last or {}).get("replacement") if isinstance(last, dict) else None
            value = (replacement or {}).get("replacementId") if isinstance(replacement, dict) else None
            return str(value) if value else ""

        def _update_replace_button(self):
            button = getattr(self, "replace_button", None)
            if button is None:
                return
            workspace_id = self._current_workspace_id()
            document = self._documents.get(workspace_id)
            if (not document or document.get("switchInFlight") or
                    document.get("readOnly") or
                    document.get("documentKind") in ("organization-preview", "source-include", "capture-source")):
                if not document:
                    button.setEnabled(False)
                    button.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Use Compiled HLSL"))
                else:
                    button.setEnabled(False)
                    button.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Use Compiled HLSL"))
                return
            button.setEnabled(bool(self._document_workspace_path(workspace_id, document)))
            if self._applied_replacement_id(workspace_id, document):
                button.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Use Original Shader"))
            else:
                button.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Use Compiled HLSL"))

        def _toggle_replacement(self):
            if getattr(self, "visual_debug", None) is not None and (
                    self.visual_debug.request or self.visual_debug.session_id):
                self.visual_debug.stop_then(self._toggle_replacement)
                return
            workspace_id = self._current_workspace_id()
            document = self._documents.get(workspace_id)
            if (not document or document.get("switchInFlight") or
                    document.get("readOnly") or
                    document.get("documentKind") in ("organization-preview", "source-include", "capture-source")):
                return
            path = self._document_workspace_path(workspace_id, document)
            binding = self._current_capture_binding()
            if not path or not binding:
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "No capture is open for this Shader; the switch needs the live capture"))
                return
            applied = self._applied_replacement_id(workspace_id, document)
            operation = "restore" if applied else "apply"    # i18n-ignore: live-edit operation identifiers
            parameters = {"workspace_path": path}
            if applied:
                parameters["replacement_id"] = applied
            document["switchInFlight"] = True
            self.replace_button.setEnabled(False)
            self.replace_button.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Switching..."))
            self.status_label.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "Restoring the captured Shader...") if applied else
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Applying the compiled HLSL (compiling first only if the source changed)..."))
            client = self._save_client
            results = self._switch_results

            def _run():
                try:
                    value = client.live_edit_operation(operation, parameters, binding)
                    results.put((workspace_id, operation, value, None))
                except Exception as exc:    # noqa: BLE001 - reported on the UI thread
                    results.put((workspace_id, operation, None, "%s: %s" % (
                        type(exc).__name__, str(exc)[:600])))

            worker = threading.Thread(target=_run, name="ReplayLabShaderSwitch")
            worker.daemon = True
            worker.start()

        def _drain_switch_results(self):
            while True:
                try:
                    workspace_id, operation, value, error = self._switch_results.get_nowait()
                except queue.Empty:
                    return
                document = self._documents.get(workspace_id)
                if document is not None:
                    document["switchInFlight"] = False
                if error:
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Shader switch failed: %1").replace("%1", error))
                elif document is not None:
                    if operation == "apply":    # i18n-ignore: live-edit operation identifier
                        document["appliedReplacementId"] = str(value.get("replacementId") or "")
                        self.status_label.setText(
                            QtCore.QCoreApplication.translate(
                                "ReplayLabProjects",
                                "Compiled HLSL applied (revision %1, recompiled)")
                            if value.get("compiled") else
                            QtCore.QCoreApplication.translate(
                                "ReplayLabProjects",
                                "Compiled HLSL applied (revision %1, no recompile needed)"))
                        self.status_label.setText(self.status_label.text().replace(
                            "%1", str(value.get("revision", ""))))
                    else:
                        document["appliedReplacementId"] = ""
                        self.status_label.setText(QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "Captured Shader restored"))
                self._update_replace_button()

        def _tab_moved(self, _from, _to):
            if self._reordering_tabs:
                return
            # Pinned tabs stay as one stable group at the left edge.
            self._reordering_tabs = True
            try:
                ordered = self._tab_workspace_ids()
                desired = ([item for item in ordered if item in self._pinned] +
                           [item for item in ordered if item not in self._pinned])
                if ordered != desired:
                    current = self._current_workspace_id()
                    for destination, workspace_id in enumerate(desired):
                        source = self._tab_index(workspace_id)
                        if source != destination:
                            self.tab_bar.moveTab(source, destination)
                    self.tab_bar.setCurrentIndex(self._tab_index(current))
                pinned = set(self._pinned)
                self._pinned = [
                    item for item in self._tab_workspace_ids() if item in pinned]
            finally:
                self._reordering_tabs = False
            self._save_state()

        def _toggle_pin(self, workspace_id):
            workspace_id = str(workspace_id or "")
            if workspace_id not in self._documents:
                return
            if workspace_id in self._pinned:
                self._pinned.remove(workspace_id)
            else:
                self._pinned.append(workspace_id)
            current = self._current_workspace_id()
            document = self._documents[workspace_id]
            index = self._tab_index(workspace_id)
            self.tab_bar.removeTab(index)
            self._insert_tab(workspace_id, document)
            selected_index = self._tab_index(current)
            self.tab_bar.setCurrentIndex(selected_index)
            self._tab_changed(selected_index)
            self._save_state()

        def _confirm_close_document(self, document):
            if not document.get("dirty"):
                if (document.get("saveInFlight") or
                        document.get("commitUnknown")):
                    document["closeAfterSave"] = True
                    return False
                return True
            message = _format_message(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save changes to %1 before closing?"),
                document.get("fileName", ""))
            box = QtWidgets.QMessageBox(self.top)
            box.setIcon(QtWidgets.QMessageBox.Question)
            box.setWindowTitle(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Unsaved Shader"))
            box.setText(message)
            buttons = QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel
            if document.get('available'):
                buttons |= QtWidgets.QMessageBox.Save
            else:
                box.setText(_format_message(QtCore.QCoreApplication.translate(
                    'ReplayLabProjects',
                    'There are unsaved changes to %1 from another capture. Reopen that capture to save, or discard the changes.'),
                    document.get('fileName', '')))
            box.setStandardButtons(buttons)
            answer = box.exec_()
            if answer == QtWidgets.QMessageBox.Save:
                document["closeAfterSave"] = True
                if not self._save_document(document["workspaceId"]):
                    document["closeAfterSave"] = False
                return False
            return answer == QtWidgets.QMessageBox.Discard

        def _close_document(self, workspace_id, prompt=True):
            workspace_id = str(workspace_id or "")
            document = self._documents.get(workspace_id)
            if document is None:
                self._restore_open = [item for item in self._restore_open if item != workspace_id]
                self._closed_document_ids.add(workspace_id)
                if workspace_id in self._pinned:
                    self._pinned.remove(workspace_id)
                if self._restore_active == workspace_id:
                    self._restore_active = ""
                self._save_state(force=True)
                return True
            if not prompt and (document.get('dirty') or document.get('saveInFlight') or
                               document.get('commitUnknown')):
                return False
            if prompt and not self._confirm_close_document(document):
                return False
            if getattr(self, "visual_debug", None) is not None:
                self.visual_debug.source_changed(workspace_id)
            index = self._tab_index(workspace_id)
            if index >= 0:
                self.tab_bar.removeTab(index)
            editor = document["editor"]
            editor_widget = document["editorWidget"]
            if getattr(self, "debug_snapshots", None) is not None:
                self.debug_snapshots.release_document(document,
                    document.get("baselineEditorText") if document.get("dirty") else None)
            outline_anchors = document.pop("flowOutlineAnchors", None)
            if outline_anchors is not None:
                outline_anchors.clear()
            self.editor_stack.removeWidget(editor_widget)
            editor.ClearCallbacks()
            self.ctx.DestroyShaderCodeEditor(editor)
            document["editor"] = None
            document["editorWidget"] = None
            self._documents.pop(workspace_id, None)
            self._closed_document_ids.add(workspace_id)
            self._restore_open = [item for item in self._restore_open if item != workspace_id]
            if self._restore_active == workspace_id:
                self._restore_active = ""
            self._pending_open.pop(workspace_id, None)
            self._pending_reload.pop(workspace_id, None)
            self._source_documents.pop(workspace_id, None)
            getattr(self, "_capture_sources", {}).pop(workspace_id, None)
            if workspace_id in self._pinned:
                self._pinned.remove(workspace_id)
            if not self._documents:
                self.editor_stack.setCurrentWidget(self.empty_page)
            if not self.tab_bar.count():
                self._restore_default_document = False
                self._closed_document_ids.update(self._restore_open)
                self._restore_open = []
                self._restore_active = ""
            self._save_state(force=True)
            self._refresh_current_view()
            return True

        def _close_current(self):
            workspace_id = self._current_workspace_id()
            if workspace_id:
                self._close_document(workspace_id)

        def _close_other_documents(self, workspace_id):
            for candidate in list(self._tab_workspace_ids()):
                if candidate != workspace_id and not self._close_document(candidate):
                    return
            cancelled = [item for item in self._restore_open if item != workspace_id]
            self._closed_document_ids.update(cancelled)
            self._restore_open = [item for item in self._restore_open if item == workspace_id]
            self._save_state(force=True)

        def _save_current(self, _checked=False):
            workspace_id = self._current_workspace_id()
            if workspace_id:
                return self._save_document(workspace_id)
            return True

        def _save_all(self, _checked=False):
            for workspace_id in list(self._tab_workspace_ids()):
                document = self._documents.get(workspace_id)
                if (document and not document.get("readOnly") and
                        document.get("dirty")):
                    if not self._save_document(workspace_id):
                        self.tab_bar.setCurrentIndex(self._tab_index(workspace_id))
                        return False
            return True

        def _save_document(self, workspace_id):
            if getattr(self, "visual_debug", None) is not None and (
                    self.visual_debug.request or self.visual_debug.session_id):
                self.visual_debug.stop_then(lambda: self._save_document(workspace_id))
                return
            document = self._documents.get(workspace_id)
            if document is None:
                return False
            if document.get("documentKind") == "capture-source":
                return self._export_capture_source(document)
            if document.get("readOnly"):
                if document.get("documentKind") == "organization-preview":
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Last successful segment is shown; final ABI and equivalence checks are still pending."))
                else:
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Project HLSL is read-only; create a managed workspace to edit and replace it."))
                return False
            workspace = self._workspaces.get(workspace_id)
            if (workspace is None or not document.get("available") or
                    document.get("captureBinding") != self._current_capture_binding()):
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"),
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Workspace authorization is no longer available."))
                return False
            if document.get("conflict"):
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"),
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "The source changed on disk. Reload it before saving to avoid overwriting another edit."))
                return False
            if workspace.get("sourceDocumentId"):
                mutations = {_normal_path(path) for path in workspace.get("mutationPaths") or []}
                # Attaching an external include rewrites exact parent directives.
                # Never replace a parent's disk text while an editor holds newer bytes.
                for other in self._documents.values():
                    if _normal_path(other.get("sourcePath", "")) not in mutations:
                        continue
                    if (any(other.get(key) for key in (
                            "dirty", "saveInFlight", "commitUnknown", "pendingSave", "conflict")) or
                            str(other["editor"].GetText()) != other.get("baselineEditorText")):
                        QtWidgets.QMessageBox.warning(
                            self.top, QtCore.QCoreApplication.translate(
                                "ReplayLabProjects", "Could not save Shader"),
                            QtCore.QCoreApplication.translate(
                                "ReplayLabProjects",
                                "The source changed on disk. Reload it before saving to avoid overwriting another edit."))
                        return False
            if (document.get("saveInFlight") or
                    document.get("commitUnknown")):
                if document.get("dirty"):
                    document["saveQueued"] = True
                return True
            # An explicit save can also retry a previously saved source after a
            # compiler failure or a stopped Host session; unchanged bytes are valid.
            source_text = self._document_source_text(document)
            try:
                submitted_hash = hashlib.sha256(
                    source_text.encode("utf-8", errors="strict")).hexdigest()
            except UnicodeEncodeError as exc:
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"), str(exc))
                return False
            live_edit = workspace.get("liveEdit") or {}
            self._next_save_token += 1
            task = {
                "saveToken": self._next_save_token,
                "documentToken": document["documentToken"],
                "workspaceId": workspace_id,
                "liveEditId": str(live_edit.get("liveEditId", "") or ""),
                "authorizationId": document.get("authorizationId", ""),
                "baseHash": document.get("rawSha256", ""),
                "submittedHash": submitted_hash,
                "sourceText": source_text,
                "submittedEditorText": document["editor"].GetText(),
                "textRevision": int(document.get("textRevision", 0)),
                "manualApply": True,
                "sourceOnly": (workspace.get("autoApplyReady") is not True or
                               document.get("documentKind") == "organization-preview"),
                "workspacePath": str(workspace.get("workspacePath") or ""),
                "sourceWorkspaceId": str(workspace.get("sourceWorkspaceId") or workspace_id),
                "sourceDocumentId": str(workspace.get("sourceDocumentId") or ""),
                "rootSourceSha256": str(workspace.get("rootSourceSha256") or ""),
                "captureBinding": dict(document["captureBinding"]),
            }
            document["pendingSave"] = task
            if getattr(self, "debug_snapshots", None) is not None:
                self.debug_snapshots.submitted(document)
            document["saveInFlight"] = True
            document["commitUnknown"] = False
            document["saveQueued"] = False
            document["autoApplyError"] = ""
            # The submitted undo revision is the provisional Qt clean point.
            # A definite failure restores dirty state; a success (or observed
            # commit after timeout) leaves Undo anchored at the saved revision.
            document["editor"].SetSavePoint()
            self._sync_document_dirty(document)
            self._save_tasks.put(task)
            if not self.save_result_timer.isActive():
                self.save_result_timer.start()
            self._save_state()
            self._refresh_current_view()
            return True

        def _drain_save_results(self):
            if not self._alive or self.top is None:
                return
            refresh_unknown = False
            while True:
                try:
                    outcome = self._save_results.get_nowait()
                except queue.Empty:
                    break
                task = outcome.get("task") or {}
                workspace_id = str(task.get("workspaceId", "") or "")
                document = self._documents.get(workspace_id)
                if (document is None or
                        document.get("documentToken") !=
                        task.get("documentToken")):
                    continue
                pending = document.get("pendingSave") or {}
                if pending.get("saveToken") != task.get("saveToken"):
                    continue
                document["saveInFlight"] = False
                kind = outcome.get("kind")
                if kind == "saved":
                    result = outcome.get("result") or {}
                    if result.get("rawSha256") != task.get("submittedHash"):
                        self._finish_failed_save(
                            document, outcome.get("error", ""), False)
                    else:
                        self._promote_submitted_save(
                            document, result,
                            result.get("changed") is True, True)
                elif kind == "unknown":
                    document["commitUnknown"] = True
                    self._sync_document_dirty(document)
                    refresh_unknown = True
                else:
                    self._finish_failed_save(
                        document, outcome.get("error", ""),
                        kind == "conflict")
            if refresh_unknown and self._alive:
                self.refresh(True)
            if not any(
                    document.get("saveInFlight")
                    for document in self._documents.values()):
                self.save_result_timer.stop()

        def _finish_failed_save(self, document, message, conflict):
            document["saveInFlight"] = False
            document["commitUnknown"] = False
            document["pendingSave"] = None
            document["saveQueued"] = False
            document["closeAfterSave"] = False
            document["conflict"] = bool(conflict)
            self._sync_document_dirty(document)
            document["editor"].SetModified(bool(document.get("dirty")))
            if self._alive and self.top is not None:
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"),
                    str(message or ""))
            self._save_state()
            self._refresh_current_view()

        def _save_transport_pending(self):
            checker = getattr(self._save_client, "request_pending", None)
            if not callable(checker):
                return False
            try:
                return bool(checker())
            except Exception:
                return True

        def _resolve_uncommitted_save(self, document):
            workspace_id = document["workspaceId"]
            retry = bool(document.get("saveQueued"))
            close_after_save = bool(document.get("closeAfterSave"))
            document["saveInFlight"] = False
            document["commitUnknown"] = False
            document["pendingSave"] = None
            document["saveQueued"] = False
            document["conflict"] = False
            self._sync_document_dirty(document)
            document["editor"].SetModified(bool(document.get("dirty")))
            if document.get("dirty") and (retry or close_after_save):
                if not self._save_document(workspace_id):
                    document["closeAfterSave"] = False
            elif close_after_save and not document.get("dirty"):
                self._close_document(workspace_id, prompt=False)
            else:
                document["closeAfterSave"] = False
                self._refresh_current_view()

        def _promote_submitted_save(self, document, metadata, changed,
                                    continue_queued):
            task = document.get("pendingSave")
            if task is None:
                return
            workspace_id = document["workspaceId"]
            document["rawSha256"] = task["submittedHash"]
            document["baselineEditorText"] = task["submittedEditorText"]
            document["navigationSaveRevision"] = document.get("navigationSaveRevision", 0) + 1
            if getattr(self, "debug_snapshots", None) is not None:
                self.debug_snapshots.saved(document, task["submittedEditorText"])
            document["modifiedNs"] = metadata.get("modifiedNs", 0)
            document["size"] = metadata.get("size", 0)
            document["saveInFlight"] = False
            document["commitUnknown"] = False
            document["pendingSave"] = None
            document["conflict"] = False
            document["available"] = True
            if task.get("manualApply"):
                document["autoApplyError"] = str(metadata.get("autoApplyError") or "")
            if task.get("sourceOnly"):
                # Keep the same tab/buffer and its new source identity. The frozen validation
                # snapshot remains untouched; later AI work resumes this actual saved draft.
                model = self._workspaces.get(workspace_id, {})
                source = dict(self._source_documents.get(workspace_id) or {})
                for key in ("sourcePath", "organizationId", "rootSourceSha256", "rootSourcePath", "mutationPaths"):
                    if metadata.get(key):
                        document[key] = model[key] = source[key] = metadata[key]
                model.update(rawSha256=task["submittedHash"], sourceEditable=True)
                source.update(rawSha256=task["submittedHash"], text=task["sourceText"],
                              modifiedNs=document["modifiedNs"], size=document["size"],
                              canonicalSourceUpdated=not bool(task.get("sourceDocumentId")))
                if task.get("sourceDocumentId"):
                    # Empty mutationPaths acknowledges that parent redirection completed.
                    model["mutationPaths"] = source["mutationPaths"] = metadata.get("mutationPaths", [])
                    self._last_disk_signature = None
                self._source_documents[workspace_id] = source
                preview = self._organization_previews.get(workspace_id)
                if preview is not None:
                    preview.update(workspace=model, opened=source)
                document["editor"].SetFileName(document["sourcePath"])
                if hasattr(document["editor"], "SetIncludeBaseDirectory"):
                    document["editor"].SetIncludeBaseDirectory(
                        os.path.dirname(str(model.get("rootSourcePath") or ""))
                        if document.get("documentKind") == "organization-preview" else "")
            queued = bool(document.get("saveQueued"))
            document["saveQueued"] = False
            close_after_save = bool(document.get("closeAfterSave"))
            self._sync_document_dirty(document)
            if not document.get("dirty"):
                document["editor"].SetSavePoint()
            if self._current_workspace_id() == workspace_id:
                if task.get("manualApply") and document.get("autoApplyError"):
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Source saved; automatic compilation could not start."))
                elif task.get("manualApply") and metadata.get("compileScheduled"):
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Saved; waiting for compile and replacement…"))
                elif task.get("sourceOnly"):
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Source is editable; compile and apply checks are still pending."))
                elif changed:
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Saved; waiting for compile and replacement…"))
                else:
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "No changes to save"))
            self._save_state()
            if (continue_queued and queued and document.get("dirty") and
                    not document.get("conflict")):
                if not self._save_document(workspace_id):
                    document["closeAfterSave"] = False
            elif close_after_save and not document.get("dirty"):
                self._close_document(workspace_id, prompt=False)
            else:
                document["closeAfterSave"] = False
                self._refresh_current_view()
            if task.get("manualApply"):
                self.refresh(True)

        @staticmethod
        def _document_source_text(document):
            source_text = document["editor"].GetText()
            newline = document.get("newline", "\n")
            if newline != "\n":
                source_text = source_text.replace("\r\n", "\n").replace(
                    "\r", "\n").replace("\n", newline)
            return source_text

        @staticmethod
        def _newline_style(text):
            value = str(text or "")
            if "\r\n" in value:
                return "\r\n"
            if "\r" in value:
                return "\r"
            return "\n"

        def _export_capture_source(self, document):
            destination, _filter = QtWidgets.QFileDialog.getSaveFileName(
                self.top, QtCore.QCoreApplication.translate("ReplayLabProjects", "Save Shader source copy"),
                document.get("exportPath") or document.get("fileName", ""),
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Shader source (*.hlsl *.glsl *.frag *.hlsli);;All files (*)"))
            if not destination:
                return False
            temporary = None
            try:
                text = str(document["editor"].GetText())
                with tempfile.NamedTemporaryFile(mode="wb", delete=False,
                        dir=os.path.dirname(os.path.abspath(destination)), prefix=".bread-source-", suffix=".tmp") as handle:
                    temporary = handle.name
                    handle.write(text.encode("utf-8"))
                os.replace(temporary, destination)
                temporary = None
            except (OSError, UnicodeError) as error:
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Could not save the source copy: %1").replace("%1", str(error)))
                return False
            finally:
                if temporary:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
            document["exportPath"] = destination
            if getattr(self, "debug_snapshots", None) is not None:
                self.debug_snapshots.submitted(document)
            document["baselineEditorText"] = text
            document["navigationSaveRevision"] = document.get("navigationSaveRevision", 0) + 1
            if getattr(self, "debug_snapshots", None) is not None:
                self.debug_snapshots.saved(document, text)
            document["editor"].SetSavePoint()
            self._sync_document_dirty(document)
            self.status_label.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Source copy saved to %1; replay was not changed.").replace("%1", destination))
            return True

        def _refresh_document_from_disk(self, workspace_id):
            document = self._documents.get(workspace_id)
            workspace = self._document_model(workspace_id)
            if document is None or workspace is None:
                return
            if document.get("documentKind") == "capture-source":
                return
            binding = self._current_capture_binding()
            if binding != self._capture_binding:
                self.capture_changed(binding)
                return
            document["readOnly"] = False
            document["editor"].SetReadOnly(False)
            # The worker result is authoritative while the Host request still
            # has a known outcome. Filesystem notification can arrive first.
            if document.get("saveInFlight"):
                return
            commit_unknown = bool(
                document.get("commitUnknown") and
                document.get("pendingSave") is not None)
            opened = self._cached_source_document(workspace_id)
            if opened is None:
                return
            disk_hash = opened.get("rawSha256")
            if commit_unknown:
                task = document.get("pendingSave") or {}
                classification = _classify_commit_unknown(
                    task.get("baseHash"), task.get("submittedHash"), disk_hash)
                if classification == "committed":
                    self._promote_submitted_save(
                        document, opened,
                        task.get("submittedHash") != task.get("baseHash"), True)
                elif classification == "pending":
                    document["modifiedNs"] = opened.get("modifiedNs", 0)
                    document["size"] = opened.get("size", 0)
                    document["conflict"] = False
                    if self._save_transport_pending():
                        self._sync_document_dirty(document)
                    else:
                        self._resolve_uncommitted_save(document)
                else:
                    document["commitUnknown"] = False
                    document["pendingSave"] = None
                    document["saveQueued"] = False
                    document["closeAfterSave"] = False
                    document["conflict"] = True
                    self._sync_document_dirty(document)
                    document["editor"].SetModified(bool(document.get("dirty")))
                return
            if disk_hash == document.get("rawSha256"):
                document["modifiedNs"] = opened.get("modifiedNs", 0)
                document["size"] = opened.get("size", 0)
                return
            # Native edits may precede the queued Python dirty notification.
            # A file watcher result must not overwrite that intervening edit.
            self._sync_document_dirty(document)
            if document.get("dirty"):
                try:
                    editor_hash = hashlib.sha256(
                        self._document_source_text(document).encode(
                            "utf-8", errors="strict")).hexdigest()
                except UnicodeEncodeError:
                    editor_hash = ""
                if editor_hash == disk_hash:
                    document["rawSha256"] = disk_hash
                    document["baselineEditorText"] = (
                        document["editor"].GetText())
                    document["modifiedNs"] = opened.get("modifiedNs", 0)
                    document["size"] = opened.get("size", 0)
                    document["conflict"] = False
                    self._sync_document_dirty(document)
                    document["editor"].SetSavePoint()
                    self._save_state()
                    return
                document["conflict"] = True
                return
            self._apply_loaded_document(document, opened)

        def _apply_loaded_document(self, document, opened):
            workspace_id = document["workspaceId"]
            if getattr(self, "debug_snapshots", None) is not None:
                self.debug_snapshots.release_document(document, opened.get("text", ""))
            outline_anchors = document.pop("flowOutlineAnchors", None)
            if outline_anchors is not None:
                outline_anchors.clear()
            self._loading_documents.add(workspace_id)
            line = document["editor"].GetCursorLine()
            column = document["editor"].GetCursorColumn()
            document["editor"].SetText(opened.get("text", ""))
            document["flowAnchors"] = {}
            document["flowFunctionBookmarks"] = {}
            document["flowFunctionRanges"] = {}
            document["flowBufferReset"] = int(document.get("flowBufferReset", 0)) + 1
            self._loading_documents.discard(workspace_id)
            document["rawSha256"] = opened.get("rawSha256", "")
            document["modifiedNs"] = opened.get("modifiedNs", 0)
            document["size"] = opened.get("size", 0)
            document["newline"] = self._newline_style(opened.get("text", ""))
            document["baselineEditorText"] = document["editor"].GetText()
            document["dirty"] = False
            document["conflict"] = False
            document["saveInFlight"] = False
            document["commitUnknown"] = False
            document["pendingSave"] = None
            document["saveQueued"] = False
            document["closeAfterSave"] = False
            document["available"] = True
            # A newly loaded source has its own compile/apply status; an earlier
            # explicit-save startup error must not override that result.
            document["autoApplyError"] = ""
            document["editor"].SetSavePoint()
            document["editor"].GoToLine(line, column)
            if getattr(self, "debug_snapshots", None) is not None:
                try:
                    self.debug_snapshots.bind_document(document,
                        self._document_model(workspace_id) or document.get("workspace") or {})
                except Exception as error:
                    self.debug_snapshots._error(error)
            self._update_tab(workspace_id)

        def _reload_current(self, _checked=False):
            document = self._current_document()
            if document is None:
                return
            if document.get("dirty"):
                answer = QtWidgets.QMessageBox.question(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Reload Shader"),
                    _format_message(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Discard local changes and reload %1 from disk?"),
                        document.get("fileName", "")),
                    QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel,
                    QtWidgets.QMessageBox.Cancel)
                if answer != QtWidgets.QMessageBox.Discard:
                    return
            workspace = self._document_model(document["workspaceId"])
            if workspace is None:
                return
            self._pending_reload[document["workspaceId"]] = _document_read_identity(document)
            self._source_documents.pop(document["workspaceId"], None)
            self.refresh(True)

        def _workspace_problems(self, workspace, source_sha256=""):
            problems = []
            watch = workspace.get("watch") or {}
            events = watch.get("events", [])
            failed = _latest_event(events, ("compileFailed",))
            succeeded = _latest_event(events, ("compileSucceeded",))
            compile_failure_current = not (
                failed is None or (succeeded is not None and int(
                    succeeded.get("sequence", 0)) > int(failed.get("sequence", 0))))
            if compile_failure_current:
                failed_source = str(failed.get("requestedSourceSha256", "") or "")
                if not (failed_source and source_sha256 and failed_source != source_sha256):
                    sequence = int(failed.get("sequence", 0))
                    values = [
                        entry for entry in workspace.get("entries", [])
                        if (int(entry.get("cursorSequence", 0)) == sequence and
                            entry.get("severity") in ("error", "warning") and
                            int(entry.get("line", 0)) > 0)
                    ]
                    if values:
                        problems.extend(values)
                    else:
                        problems.append({
                            "severity": "error",
                            "line": 0,
                            "column": 0,
                            "text": failed.get("message") or
                            QtCore.QCoreApplication.translate(
                                "ReplayLabProjects", "Shader compilation failed"),
                        })
            # Compile can succeed while the live replacement still fails: the
            # ABI/live-replay apply gate rejected the candidate, or the apply hit
            # a transport/host error. The concrete reason is otherwise dropped and
            # only the generic "Replacement failed" status line shows. Surface the
            # real reason here (as a non-navigable line-0 diagnostic) so the user
            # can tell an ABI rejection (revert the edit) from a transient apply
            # error (just save again).
            live = workspace.get("liveEdit") or {}
            apply_failed = _latest_event(
                live.get("events", []), ("applyRejected", "applyFailed", "liveEditFailed"))
            apply_ok = _latest_event(
                live.get("events", []), ("applySucceeded", "applyReused"))
            if apply_failed is not None and (apply_ok is None or int(
                    apply_failed.get("sequence", 0)) > int(apply_ok.get("sequence", 0))):
                detail = str(apply_failed.get("reason")
                             or apply_failed.get("message") or "").strip()
                diagnostic = apply_failed.get("diagnostic")
                if isinstance(diagnostic, dict):
                    category = str(diagnostic.get("category", "") or "").strip()
                    if category and category not in detail:
                        detail = ("%s [%s]" % (detail, category)) if detail else category
                if not detail:
                    # Reuse the existing translated status label as a fallback so
                    # this adds no new untranslated UI string.
                    detail = QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Replacement failed; the last good replacement remains active")
                problems.append({
                    "severity": "error",
                    "line": 0,
                    "column": 0,
                    "text": detail,
                })
            return problems

        def _set_editor_diagnostics(self, editor, problems):
            diagnostics = []
            for problem in problems or []:
                line = int(problem.get("line", 0) or 0)
                if line <= 0:
                    continue
                diagnostic = self.qrd.ShaderEditorDiagnostic()
                diagnostic.line = line
                diagnostic.column = max(
                    1, int(problem.get("column", 0) or 1))
                diagnostic.length = max(
                    1, int(problem.get("length", 0) or 1))
                severity = str(problem.get("severity", "error") or "error")
                diagnostic.severity = (
                    self.qrd.ShaderEditorDiagnosticSeverity.Warning
                    if severity == "warning" else
                    self.qrd.ShaderEditorDiagnosticSeverity.Information
                    if severity == "information" else
                    self.qrd.ShaderEditorDiagnosticSeverity.Hint
                    if severity == "hint" else
                    self.qrd.ShaderEditorDiagnosticSeverity.Error)
                diagnostic.message = str(problem.get("text", "") or "")
                # i18n-ignore: stable diagnostic producer metadata, not a UI label.
                diagnostic.source = "Bread Host"
                diagnostics.append(diagnostic)
            editor.SetDiagnostics(diagnostics)

        def _problem_activated(self, item, _column=0):
            line = int(item.data(0, QtCore.Qt.UserRole) or 0)
            column = int(item.data(1, QtCore.Qt.UserRole) or 0)
            document = self._current_document()
            if document and line > 0:
                document["editor"].GoToLine(line, column)

        def _status_text(self, workspace):
            if workspace is None:
                if self._capture_binding is None:
                    return QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Open an RDC capture to load its matching project Shaders")
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Waiting for Shader source workspaces")
            live = workspace.get("liveEdit") or {}
            watch = workspace.get("watch") or {}
            if watch.get("compileInProgress") is True:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Compiling…")
            if live.get("pendingApply") is True or live.get("applyInProgress") is True:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Applying saved Shader…")
            if watch.get("pending") is True:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Waiting for source to stabilize…")
            watch_failed = _latest_event(watch.get("events", []), ("compileFailed",))
            watch_ok = _latest_event(watch.get("events", []), ("compileSucceeded",))
            if watch_failed is not None and (watch_ok is None or int(
                    watch_failed.get("sequence", 0)) > int(watch_ok.get("sequence", 0))):
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Compilation failed; the last good replacement remains active")
            apply_failed = _latest_event(
                live.get("events", []), ("applyRejected", "applyFailed", "liveEditFailed"))
            apply_ok = _latest_event(
                live.get("events", []), ("applySucceeded", "applyReused"))
            if apply_failed is not None and (apply_ok is None or int(
                    apply_failed.get("sequence", 0)) > int(apply_ok.get("sequence", 0))):
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Replacement failed; the last good replacement remains active")
            # Manual saves intentionally carry no ABI approval, so their
            # autoApplyReady flag can remain false after a successful apply.
            # Display observed completion independently of that authorization
            # flag, and only for the source/revision in the current session.
            source_sha = workspace.get("sourceSha256")
            compiled_revision = int(watch.get("latestCandidateRevision", 0) or 0)
            active_revision = int(live.get("activeRevision", 0) or 0)
            current_revision_applied = active_revision == compiled_revision or (
                apply_ok is not None and
                int(apply_ok.get("revision", 0) or 0) == compiled_revision and
                int(live.get("latestHandledRevision", 0) or 0) == compiled_revision)
            if (source_sha and compiled_revision > 0 and active_revision > 0 and
                    current_revision_applied and
                    live.get("state") == "running" and
                    live.get("sessionState", "current") == "current" and
                    watch.get("state") == "running" and
                    live.get("watchId") and live.get("watchId") == watch.get("watchId") and
                    watch.get("observedSourceSha256") == source_sha and
                    watch.get("lastCompiledSourceSha256") == source_sha):
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Compiled and applied")
            # Readiness may drop while a new saved source is compiling or after
            # it failed. Do not hide those actionable results behind a draft label.
            if workspace.get("autoApplyReady") is not True:
                replay_source = (workspace.get("projectSourceBinding") or {}).get("replaySource") or {}
                if replay_source.get("applied") is True:
                    return QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Editing the latest saved draft; replay uses the previous validated version.")
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Source is editable; compile and apply checks are still pending.")
            if int(live.get("activeRevision", 0) or 0) > 0:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Compiled and applied")
            return QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Watching saves")

        def _refresh_current_view(self):
            document = self._current_document()
            if hasattr(self, 'convert_button'):
                self._update_conversion_button()
            self._sync_flow_graph(document)
            if document is None:
                self.subtitle.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Validated Shader source · Ctrl+S saves and automatically compiles/replaces"))
                self.save_button.setEnabled(False)
                self.save_all_button.setEnabled(any(
                    item.get("available") and not item.get("readOnly") and item.get("dirty") and
                    not item.get("conflict") and
                    not item.get("saveInFlight") and
                    not item.get("commitUnknown")
                    for item in self._documents.values()))
                self.reload_button.setVisible(False)
                self.problems.clear()
                if getattr(self, '_last_open_error', '') or getattr(self, '_source_discovery_error', ''):
                    self.status_label.setText(_format_message(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not load saved Shader sources: %1"),
                        getattr(self, '_last_open_error', '') or self._source_discovery_error))
                else:
                    self.status_label.setText(self._status_text(None))
                return
            workspace = self._document_model(document["workspaceId"])
            self.subtitle.setText(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader source copy · Ctrl+S exports a file; Output to RT previews values")
                if document.get("documentKind") == "capture-source" else QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Editable Shader source · Ctrl+S saves, compiles and replaces"))
            self.save_button.setEnabled(bool(
                document.get("available") and workspace and
                not document.get("readOnly") and
                not document.get("conflict") and
                not document.get("saveInFlight") and
                not document.get("commitUnknown")))
            self.save_all_button.setEnabled(any(
                item.get("available") and not item.get("readOnly") and item.get("dirty") and
                not item.get("conflict") and
                not item.get("saveInFlight") and
                not item.get("commitUnknown")
                for item in self._documents.values()))
            self.reload_button.setVisible(bool(document.get("conflict")))
            if document.get("conflict"):
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "The source changed on disk. Reload it before saving to avoid overwriting another edit."))
            elif document.get("autoApplyError"):
                self.status_label.setText(_format_message(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Source saved; automatic compilation could not start: %1"),
                    document["autoApplyError"]))
            elif document.get('conversionInFlight'):
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Converting and validating HLSL…"))
            elif (document.get("saveInFlight") or
                  document.get("commitUnknown")):
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Waiting for source to stabilize…"))
            elif document.get("dirty"):
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Modified"))
            else:
                self.status_label.setText(self._status_text(workspace))

            # Compiler locations describe the saved revision. Keep its console
            # history, but do not underline a newer, unsaved document with it.
            problems = self._current_problems(document, workspace)
            if document.get("autoApplyError"):
                problems.append({"line": 0, "column": 0, "severity": "error",
                                 "text": document["autoApplyError"]})
            self.problems.clear()
            for problem in problems:
                line = int(problem.get("line", 0) or 0)
                text_value = str(problem.get("text", "") or "")
                item = QtWidgets.QTreeWidgetItem((str(line) if line else "", text_value))
                item.setData(0, QtCore.Qt.UserRole, line)
                item.setData(1, QtCore.Qt.UserRole, int(problem.get("column", 0) or 0))
                if problem.get("severity") == "error":
                    item.setForeground(1, QtGui.QBrush(QtGui.QColor("#e06c75")))
                self.problems.addTopLevelItem(item)
            self._set_editor_diagnostics(document["editor"], problems)
            if not problems:
                placeholder = QtWidgets.QTreeWidgetItem((
                    "", QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "No compilation problems")))
                placeholder.setFlags(placeholder.flags() & ~QtCore.Qt.ItemIsSelectable)
                self.problems.addTopLevelItem(placeholder)


else:
    class ReplayLabShaderWorkspacePanel(object):
        def __init__(self, _ctx, _qrd, closed_callback=None, registry_path=None,
                     save_client=None, project_selection_path=None,
                     capture_binding_provider=None):
            del closed_callback, registry_path, save_client, project_selection_path, capture_binding_provider
            raise RuntimeError("PySide2 Qt is required for Bread Shader Workspace")


__all__ = ["HAS_QT", "ReplayLabShaderWorkspacePanel"]
