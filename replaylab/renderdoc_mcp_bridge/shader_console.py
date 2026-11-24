"""ReplayLab Console for qrenderdoc.

The console combines a fixed, append-only ReplayLab event journal with the
authorised shader watcher/live-edit audit trail.  Its trust roots are a fixed
per-user journal directory and the process-owned persistent shader
authorisation registry; paths found only in compiler text are never opened.
The module does not invoke a model, a shell, a compiler, or a replay mutation.

This file is intentionally Python 3.6 and PySide2 compatible because it is
imported by qrenderdoc's embedded interpreter.  The parsing/projection helpers
remain usable without Qt so they can be contract-tested independently.
"""

from __future__ import print_function

from contextlib import contextmanager
import copy
import ctypes
import hashlib
import json
import os
import queue
import re
import stat
import threading
import time
import uuid


try:
    from PySide2 import QtCore, QtGui, QtWidgets
    HAS_QT = True
except ImportError:  # pragma: no cover - ordinary unit tests intentionally lack Qt
    QtCore = None
    QtGui = None
    QtWidgets = None
    HAS_QT = False


REGISTRY_SCHEMA = "replaylab-shader-live-edit-registry/v1"
WORKSPACE_SCHEMA = "renderdoc-ultra-shader-workspace/v1"
WATCH_SCHEMA = "renderdoc-ultra-shader-watch/v1"
LIVE_EDIT_SCHEMA = "replaylab-shader-live-edit/v1"
ORGANIZATION_GATE_SCHEMA = "renderdoc-ai-shader-organization-gate/v1"
ORGANIZATION_POLICY_VERSION = "renderdoc-ai-organize-workspace/v2"
ORGANIZATION_PREVIEW_SCHEMA = "replaylab-shader-organization-preview/v1"
CONSOLE_SCHEMA = "replaylab-console/v1"
JOURNAL_EVENT_SCHEMA = "replaylab-console-event/v1"
ACTIVE_PROJECT_SELECTION_SCHEMA = "replaylab-active-project-selection/v1"
ACTIVE_PROJECT_WORKSPACE_SCHEMA = "replaylab-project-workspace/v1"
ACTIVE_PROJECT_REGISTRY_SCHEMA = "replaylab-project-registry/v1"
REPLAY_PROJECT_SCHEMA = "renderdoc-ultra-replay-project/v1"
PROJECT_SHADER_LIBRARY_SCHEMA = "replaylab-project-shader-library/v1"
COMPILED_CANDIDATE_SCHEMA = "renderdoc-ultra-project-compiled-candidate/v1"
SHADER_FLOW_GRAPH_SCHEMA = "replaylab-shader-flow-graph/v1"
SHADER_FLOW_GRAPH_VERSION = 1
SHARED_DICTIONARY_SCHEMA = "replaylab-shared-dictionary/v1"
SHARED_DICTIONARY_VERSION = 1

ACTIVE_PROJECT_STATE_ENV = "RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE"
ACTIVE_PROJECT_LAYOUT = {
    "metadataDirectory": ".replaylab",
    "workspaceManifest": ".replaylab/workspace.json",
    "replayProject": ".replaylab/replay-project",
    "captureCatalogs": "captures/catalogs",
    "shaderWorkspaces": "workspaces/shaders",
    "sharedDictionaries": "shared/dictionaries",
    "sharedExplanatory": "shared/dictionaries/explanatory",
    "sharedIndex": "shared/dictionaries/index.json",
}

MAX_REGISTRY_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_AUDIT_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_DIAGNOSTIC_LOG_BYTES = 1024 * 1024
MAX_AUTHORIZATIONS = 256
MAX_DRAFT_WORKSPACE_DISCOVERY = 256
MAX_EVENTS = 2048
MAX_CONSOLE_ENTRIES = 4096
MAX_TEXT = 16384
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_LINE_BYTES = 32 * 1024
MAX_JOURNAL_READ_FILES = 8
MAX_JOURNAL_READ_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_READ_BYTES_PER_FILE = 2 * 1024 * 1024
MAX_JOURNAL_SEGMENTS_PER_SESSION = 4
MAX_JOURNAL_RETENTION_FILES = 32
MAX_JOURNAL_RETENTION_BYTES = 32 * 1024 * 1024
MAX_JOURNAL_DIRECTORY_ENTRIES = 512
JOURNAL_RETENTION_CHECK_INTERVAL = 128
JOURNAL_RETENTION_MIN_INTERVAL_SECONDS = 60.0
MAX_PROJECT_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_PROJECT_KNOWLEDGE = 4096
MAX_PROJECT_CAPTURES = 256
MAX_PROJECT_ACTIVE_HEADS = 65536
MAX_PROJECT_EXPLANATORY_RECORDS = 4096
MAX_PROJECT_COMPILED_CANDIDATE_BYTES = 64 * 1024 * 1024
MAX_PROJECT_INVENTORY_OBSERVATIONS = 4096
MAX_SHADER_FLOW_GRAPH_BYTES = 1024 * 1024
MAX_SHADER_FLOW_GRAPH_NODES = 256
MAX_SHADER_FLOW_GRAPH_EDGES = 1024

CONSOLE_SEVERITIES = ("info", "warning", "error")
CONSOLE_MODULES = (
    "system", "bridge", "host", "project", "capture", "pass", "material",
    "shader", "replay", "toolchain",
)
CONSOLE_CONTEXT_FIELDS = (
    "operation", "captureId", "captureSessionId", "eventId", "resourceId",
    "stage", "passId", "projectId", "workspaceId", "jobId", "failureCode",
    "diagnostics",
)

TOKEN_RE = re.compile(r"^[0-9a-f]{32,64}$")
EXACT_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FLOW_GRAPH_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
HLSL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SOURCE_EXTENSION_RE = re.compile(
    r"\.(?:hlsl|hlsli|fx|fxh|glsl|vert|frag|comp|geom|tesc|tese)$",
    re.IGNORECASE,
)
HLSL_SOURCE_EXTENSION_RE = re.compile(r"\.(?:hlsl|hlsli|fx|fxh)$", re.IGNORECASE)
JOURNAL_FILE_RE = re.compile(
    r"^events\.(?P<role>[a-z][a-z0-9_-]{0,31})\."
    r"(?P<pid>[0-9]{1,10})\.(?P<session>[0-9a-f]{32})\."
    r"(?P<segment>[0-9]{4})\.jsonl$"
)

# DXC/D3DCompiler commonly use either ``file:line:column: error`` or
# ``file(line,column): error``.  The suffix-qualified file group prevents a
# Windows drive colon from being mistaken for the line separator.
COLON_DIAGNOSTIC_RE = re.compile(
    r"(?P<file>(?:[A-Za-z]:[\\/]|/)?[^\r\n]*?"
    r"\.(?:hlsl|hlsli|fx|fxh|glsl|vert|frag|comp|geom|tesc|tese))"
    r":(?P<line>[0-9]+)(?::(?P<column>[0-9]+))?\s*:\s*"
    r"(?P<severity>fatal\s+error|error|warning|note)\b"
    r"(?P<message>[^\r\n]*)",
    re.IGNORECASE,
)
PAREN_DIAGNOSTIC_RE = re.compile(
    r"(?P<file>(?:[A-Za-z]:[\\/]|/)?[^\r\n]*?"
    r"\.(?:hlsl|hlsli|fx|fxh|glsl|vert|frag|comp|geom|tesc|tese))"
    r"\((?P<line>[0-9]+)(?:\s*,\s*(?P<column>[0-9]+))?\)\s*:\s*"
    r"(?P<severity>fatal\s+error|error|warning|note)\b"
    r"(?P<message>[^\r\n]*)",
    re.IGNORECASE,
)
GLSL_DIAGNOSTIC_RE = re.compile(
    r"^[ \t]*(?P<severity>fatal\s+error|error|warning|note)\s*:\s*"
    r"(?P<file>[^\r\n]+?):(?P<line>[0-9]+):(?:(?P<column>[0-9]+):)?[ \t]*"
    r"(?P<message>[^\r\n]*)", re.IGNORECASE | re.MULTILINE,
)
MESA_DIAGNOSTIC_RE = re.compile(
    r"^[ \t]*(?P<file>[^\r\n]+?):(?P<line>[0-9]+)\((?P<column>[0-9]+)\)[ \t]*:[ \t]*"
    r"(?P<severity>fatal\s+error|error|warning|note)\b(?P<message>[^\r\n]*)",
    re.IGNORECASE | re.MULTILINE,
)

# These two fixed host diagnostics mean that an apply job lost its exact
# source/candidate binding because a newer save arrived.  They are expected
# latest-wins queue behaviour, not a compiler, ABI, or RenderDoc mutation
# failure.  Keep this list deliberately narrow so every other applyFailed
# remains a real error in the console.
SUPERSEDED_APPLY_FAILURE_FRAGMENTS = (
    "shader source changed after the candidate compile",
    "workspace no longer contains the expected saved candidate",
    "workspace no longer contains expected saved candidate",
)


# These ctypes definitions and WinAPI signatures must be process singletons.
# ctypes caches pointer types by the pointed-to Python class. Defining the
# structures inside every file read therefore retains a new POINTER type (and
# its structure class) for the lifetime of qrenderdoc.
class _WindowsFileTime(ctypes.Structure):
    _fields_ = (("low", ctypes.c_uint32), ("high", ctypes.c_uint32))


class _WindowsByHandleFileInformation(ctypes.Structure):
    _fields_ = (
        ("attributes", ctypes.c_uint32), ("created", _WindowsFileTime),
        ("accessed", _WindowsFileTime), ("written", _WindowsFileTime),
        ("volumeSerial", ctypes.c_uint32), ("sizeHigh", ctypes.c_uint32),
        ("sizeLow", ctypes.c_uint32), ("links", ctypes.c_uint32),
        ("fileIndexHigh", ctypes.c_uint32),
        ("fileIndexLow", ctypes.c_uint32),
    )


_WINDOWS_FILE_INFORMATION_POINTER = ctypes.POINTER(
    _WindowsByHandleFileInformation)
_WINDOWS_LONGLONG_POINTER = ctypes.POINTER(ctypes.c_longlong)
_WINDOWS_UINT32_POINTER = ctypes.POINTER(ctypes.c_uint32)
_WINDOWS_FILE_API = None
_WINDOWS_FILE_API_LOCK = threading.Lock()


class _WindowsFileApi(object):
    """One configured kernel32 file API surface for this Python process."""

    def __init__(self, kernel32):
        self.create_file = kernel32.CreateFileW
        self.create_file.argtypes = (
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
        self.create_file.restype = ctypes.c_void_p

        self.get_size = kernel32.GetFileSizeEx
        self.get_size.argtypes = (
            ctypes.c_void_p, _WINDOWS_LONGLONG_POINTER)
        self.get_size.restype = ctypes.c_int

        self.set_pointer = kernel32.SetFilePointerEx
        self.set_pointer.argtypes = (
            ctypes.c_void_p, ctypes.c_longlong,
            _WINDOWS_LONGLONG_POINTER, ctypes.c_uint32)
        self.set_pointer.restype = ctypes.c_int

        self.read_file = kernel32.ReadFile
        self.read_file.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            _WINDOWS_UINT32_POINTER, ctypes.c_void_p)
        self.read_file.restype = ctypes.c_int

        self.close_handle = kernel32.CloseHandle
        self.close_handle.argtypes = (ctypes.c_void_p,)
        self.close_handle.restype = ctypes.c_int

        self.get_information = kernel32.GetFileInformationByHandle
        self.get_information.argtypes = (
            ctypes.c_void_p, _WINDOWS_FILE_INFORMATION_POINTER)
        self.get_information.restype = ctypes.c_int

        self.get_final_path = kernel32.GetFinalPathNameByHandleW
        self.get_final_path.argtypes = (
            ctypes.c_void_p, ctypes.c_wchar_p,
            ctypes.c_uint32, ctypes.c_uint32)
        self.get_final_path.restype = ctypes.c_uint32


def _windows_file_api():
    """Return the lazily-created process-wide WinAPI binding."""
    if os.name != "nt":
        raise OSError("Windows file API is unavailable")
    global _WINDOWS_FILE_API
    if _WINDOWS_FILE_API is None:
        with _WINDOWS_FILE_API_LOCK:
            if _WINDOWS_FILE_API is None:
                _WINDOWS_FILE_API = _WindowsFileApi(
                    ctypes.WinDLL("kernel32", use_last_error=True))
    return _WINDOWS_FILE_API


class ShaderConsoleError(RuntimeError):
    pass


class ShaderConsoleConflictError(ShaderConsoleError):
    """The authorized source changed after the editor's expected hash."""

    pass


class ShaderConsoleReadCancelled(RuntimeError):
    """A background read lost its owner; never convert it into invalid data."""


_console_read_scope = threading.local()


@contextmanager
def console_read_batch():
    """Share display locators within one read; source bytes remain checked."""
    if getattr(_console_read_scope, "batch_depth", 0):
        yield
        return
    _console_read_scope.batch_depth = 1
    previous = getattr(_console_read_scope, "library_memo", None)
    _console_read_scope.library_memo = {}
    _console_read_scope.draft_memo = {}
    try:
        yield
    finally:
        _console_read_scope.library_memo = previous
        del _console_read_scope.draft_memo
        del _console_read_scope.batch_depth


@contextmanager
def known_source_locator(workspace_path):
    """An exact display hint, never an authority or a save/compile grant."""
    if not isinstance(workspace_path, str) or "\0" in workspace_path or len(workspace_path) > 8192:
        raise ShaderConsoleError("Shader source locator is invalid")
    previous = getattr(_console_read_scope, "draft_hint", None)
    _console_read_scope.draft_hint = os.path.abspath(workspace_path)
    try:
        yield
    finally:
        _console_read_scope.draft_hint = previous


def _observation_path_stamp(path):
    try:
        info = os.lstat(path)
        return (info.st_mode, info.st_size, getattr(info, "st_mtime_ns", info.st_mtime),
                getattr(info, "st_ctime_ns", info.st_ctime), info.st_ino,
                getattr(info, "st_file_attributes", 0))
    except OSError:
        return None


def _observe_library_path(path):
    dependencies = getattr(_console_read_scope, "library_dependencies", None)
    if dependencies is not None:
        path = os.path.abspath(str(path))
        stamp = _observation_path_stamp(path)
        previous = dependencies["paths"].setdefault(path, stamp)
        if previous != stamp:
            dependencies["changed"] = True


class _ObservedProjectLibraryCache(object):
    """One immutable display projection, never a source/save authority.

    A hit still safely reads and hashes every input, including content-addressed
    artifacts. Metadata is only an early rejection, not proof of unchanged bytes.
    This avoids re-parsing and re-validating the entire project for each console
    workspace and each journal update. Direct document/save readers remain strict.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.entry = None

    def _unchanged(self, dependencies):
        for path, stamp in dependencies["paths"].items():
            _check_console_read_cancelled()
            if _observation_path_stamp(path) != stamp:
                return False
        for path, record in dependencies["files"].items():
            maximum, field, digest = record
            raw = _read_file_bytes(path, maximum, field)
            if hashlib.sha256(raw).digest() != digest:
                return False
        return all(_observation_path_stamp(path) == stamp
                   for path, stamp in dependencies["paths"].items())

    def load(self, path):
        key = os.path.normcase(os.path.abspath(str(path)))
        while not self.lock.acquire(timeout=0.05):
            _check_console_read_cancelled()
        try:
            if self.entry is not None and self.entry[0] == key:
                try:
                    if self._unchanged(self.entry[1]):
                        return copy.deepcopy(self.entry[2])
                except ShaderConsoleError:
                    pass
            self.entry = None
            dependencies = {"paths": {}, "files": {}, "changed": False}
            previous = getattr(_console_read_scope, "library_dependencies", None)
            _console_read_scope.library_dependencies = dependencies
            try:
                result = _load_project_shader_library(path)
            finally:
                _console_read_scope.library_dependencies = previous
            if (not dependencies["changed"] and
                    len(dependencies["paths"]) <= 8192 and
                    self._unchanged(dependencies)):
                self.entry = (key, dependencies, copy.deepcopy(result))
            return result
        finally:
            self.lock.release()


_observed_project_library = _ObservedProjectLibraryCache()


def _project_library_for_console(path):
    memo = getattr(_console_read_scope, "library_memo", None)
    if memo is None:
        return _load_project_shader_library(path)
    key = os.path.normcase(os.path.abspath(str(path)))
    if key not in memo:
        memo[key] = _observed_project_library.load(path)
    return memo[key]


def _console_paths_signature(paths):
    return tuple((path, _observation_path_stamp(path)) for path in paths)


def _console_paths_affected(paths, changes):
    # Directory notifications include lock opens and unrelated sibling writes.
    # They prompt a stat scan, never force every Shader to be reparsed. Exact
    # file notifications still cover writes which preserve coarse timestamps.
    changed = set(os.path.normcase(os.path.abspath(path)) for path in changes
                  if not os.path.isdir(path))
    return any(os.path.normcase(os.path.abspath(path)) in changed for path in paths)


def _build_console_panel_snapshot(request):
    """Filesystem work only; no panel, capture controller or Qt wrappers."""
    with cancellable_console_reads(request["stop"]):
        before = _console_paths_signature(request["watchedPaths"])
        cached = request.get("cachedCatalog") or {}
        changes = request.get("changedPaths", [])
        full = request["force"] or request.get("fullObservationDue", False)
        if (not full and not _console_paths_affected(request["watchedPaths"], changes)
                and before == request["previousSignature"] and
                cached.get("registryPath") == request["registryPath"] and
                cached.get("captureBinding") == request["captureBinding"]):
            return {"unchanged": True}
        source = cached.get("snapshot")
        source_before = _console_paths_signature(
            (source or {}).get("watchedPaths", []))
        if (full or source is None or _console_paths_affected((source or {}).get("watchedPaths", []), changes) or
                cached.get("registryPath") != request["registryPath"] or
                cached.get("captureBinding") != request["captureBinding"] or
                cached.get("signature") != source_before):
            source = load_console_snapshot(
                request["registryPath"], capture_binding=request["captureBinding"],
                include_console_events=False)
            source_after = _console_paths_signature(source["watchedPaths"])
            source_signature = source_after if source_before == source_after else None
        else:
            source_signature = source_before
        # Journals change while AI and replay work runs. Updating messages must
        # not reread every Shader manifest or invalidate pending editor opens.
        generic = _load_generic_console_entries()
        snapshot = copy.deepcopy(source)
        snapshot["entries"] = snapshot["entries"] + generic["entries"]
        snapshot["entries"].sort(key=lambda item: (
            item.get("at", ""), item.get("unixMs", 0), item.get("streamKey", ""),
            item.get("sequence", 0)))
        snapshot["entries"] = snapshot["entries"][-MAX_CONSOLE_ENTRIES:]
        snapshot["errors"].extend(generic["errors"])
        snapshot["counts"] = console_severity_counts(snapshot["entries"])
        paths = list(dict.fromkeys(source["watchedPaths"] + generic["watchedPaths"]))
        snapshot["watchedPaths"] = paths
        after = _console_paths_signature(paths)
        # Never hide changes that arrived during a read by adopting them as the
        # new baseline. A newly discovered set gets one subsequent warm check.
        signature = after if before == after else None
        return {"snapshot": snapshot, "signature": signature, "cachedCatalog": {
            "snapshot": source, "signature": source_signature,
            "registryPath": request["registryPath"],
            "captureBinding": request["captureBinding"]}}


class _ConsoleSnapshotReader(object):
    """A single bounded worker; results are consumed by the GUI's timer."""

    def __init__(self):
        self.tasks = queue.Queue(1)
        self.results = queue.Queue(1)
        self.stop = threading.Event()
        self.catalog = None
        self.thread = threading.Thread(
            target=self._run, name="BreadConsoleSnapshot")
        self.thread.daemon = True
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                request = self.tasks.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with cancellable_console_reads(self.stop):
                    result = _build_console_panel_snapshot(dict(request, cachedCatalog=self.catalog))
                    if result.get("cachedCatalog") is not None:
                        self.catalog = result.pop("cachedCatalog")
            except ShaderConsoleReadCancelled:
                result = {"cancelled": True}
            except Exception as exc:
                result = {"error": _bounded(exc, 1024)}
            if not self.stop.is_set():
                self.results.put_nowait((request, result))

    def submit(self, request):
        self.tasks.put_nowait(request)

    def take(self):
        try:
            return self.results.get_nowait()
        except queue.Empty:
            return None

    def shutdown(self):
        self.stop.set()
        self.thread.join(5.0)
        if self.thread.is_alive():
            # i18n-ignore: shutdown diagnostic prevents unsafe Python finalization.
            raise ShaderConsoleError("Bread Console reader did not stop")


def _check_console_read_cancelled():
    for event in getattr(_console_read_scope, "events", ()):
        if event.is_set():
            # i18n-ignore: private worker cancellation, discarded before UI delivery.
            raise ShaderConsoleReadCancelled("Shader console read was cancelled")


@contextmanager
def cancellable_console_reads(stop_event):
    """Cancel bounded file/validation work without altering its trust checks.

    The scope is worker-local and nestable.  It cannot interrupt a single OS
    read, but stops before the next file, read chunk, or validation iteration.
    Save/Host operations do not enter this read-only cancellation scope.
    """
    previous = getattr(_console_read_scope, "events", ())
    _console_read_scope.events = previous + (stop_event,)
    try:
        _check_console_read_cancelled()
        yield
        _check_console_read_cancelled()
    finally:
        if previous:
            _console_read_scope.events = previous
        else:
            del _console_read_scope.events


def _canonical_sha256(value):
    """Match the Host registry's stable identity digest exactly."""
    _check_console_read_cancelled()
    try:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ShaderConsoleError(
            "live-edit authorization identity is not canonical JSON: %s" %
            _bounded(exc, 512))
    return hashlib.sha256(payload).hexdigest()


def default_registry_path(local_app_data=None):
    root = str(local_app_data or os.environ.get("LOCALAPPDATA", "") or "").strip()
    if not root:
        return ""
    return os.path.abspath(os.path.join(
        root, "ReplayLab", "config", "shader-live-edit-authorizations.v1.json"))


def default_active_project_selection_path(local_app_data=None, environment=None):
    """Return the process-owned active-project selection path used by the Host."""
    env = os.environ if environment is None else environment
    configured = str(env.get(ACTIVE_PROJECT_STATE_ENV, "") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    root = str(local_app_data or env.get("LOCALAPPDATA", "") or "").strip()
    if root:
        data_root = os.path.expanduser(root)
    else:
        xdg = str(env.get("XDG_DATA_HOME", "") or "").strip()
        data_root = (os.path.expanduser(xdg) if xdg else
                     os.path.join(os.path.expanduser("~"), ".local", "share"))
    return os.path.abspath(os.path.join(
        data_root, "ReplayLab", "config", "active-project.json"))


def default_console_journal_directory(local_app_data=None):
    """Return the one fixed per-user directory accepted by the console UI."""
    root = str(local_app_data or os.environ.get("LOCALAPPDATA", "") or "").strip()
    if not root:
        return ""
    directory = os.path.abspath(os.path.join(root, "ReplayLab", "console-events"))
    instance = str(os.environ.get("REPLAYLAB_INSTANCE_ID") or os.environ.get("BREAD_ROUTING_INSTANCE") or "")
    if re.fullmatch(r'[1-9][0-9]{0,19}-[0-9a-f]{32}', instance):
        directory = os.path.join(directory, 'instances', instance)
    return directory


def _bounded(value, maximum=MAX_TEXT):
    text = str(value or "")
    if "\x00" in text:
        text = text.replace("\x00", "")
    if len(text) > maximum:
        return text[:maximum] + "..."
    return text


def normalise_console_severity(value):
    """Collapse progress/success/note into Unity's ordinary message class."""
    severity = str(value or "info").strip().lower()
    if severity in ("warning", "warn"):
        return "warning"
    if severity in ("error", "fatal", "failure", "failed"):
        return "error"
    return "info"


def _console_module(value):
    module = str(value or "system").strip().lower()
    if module not in CONSOLE_MODULES:
        raise ShaderConsoleError("console module is not registered")
    return module


def _console_context(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ShaderConsoleError("console event context must be an object")
    clean = {}
    for key in CONSOLE_CONTEXT_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise ShaderConsoleError("console event context field is invalid")
        # _bounded appends an ellipsis after its content limit. Use 1021 so
        # every accepted context scalar, including non-authoritative failure
        # diagnostics, remains at or below the documented 1024-character cap.
        clean[key] = _bounded(item, 1021)
    return clean


def _utc_now_text(unix_ms=None):
    value = int(unix_ms if unix_ms is not None else time.time() * 1000)
    seconds = value // 1000
    milliseconds = value % 1000
    return "%s.%03dZ" % (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)), milliseconds)


_JOURNAL_LOCK = threading.RLock()
_JOURNAL_SESSION_ID = uuid.uuid4().hex
_JOURNAL_SEQUENCE = 0
_JOURNAL_SEGMENT = 0
_JOURNAL_RETENTION_COUNTER = 0
_JOURNAL_LAST_RETENTION_MONOTONIC = 0.0


def _journal_filename(directory, role, segment):
    return os.path.join(directory, "events.%s.%d.%s.%04d.jsonl" % (
        role, os.getpid(), _JOURNAL_SESSION_ID, segment))


def _fixed_journal_directory(directory):
    """Reject every spool path except ReplayLab's exact per-user directory."""
    expected = default_console_journal_directory()
    if not expected or os.path.normcase(os.path.abspath(str(directory))) != os.path.normcase(
            os.path.abspath(expected)):
        raise ShaderConsoleError("Bread console journal directory is not fixed")
    return os.path.abspath(expected)


def _regular_journal_file(path):
    """Accept one ordinary, single-link file without following redirections."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if (not stat.S_ISREG(info.st_mode) or os.path.islink(path) or
            bool(getattr(info, "st_file_attributes", 0) & 0x400)):
        return False
    # A hard link could make appending to the journal modify an unrelated file.
    # Fresh ReplayLab journal files always have exactly one directory entry.
    return int(getattr(info, "st_nlink", 1) or 1) == 1


def _journal_candidates(directory):
    """Enumerate only exact, bounded, non-linked ReplayLab journal segments."""
    root = _fixed_journal_directory(directory)
    if not os.path.isdir(root) or _is_reparse(root):
        return []
    values = []
    try:
        iterator = os.scandir(root)
    except OSError:
        return []
    try:
        for index, directory_entry in enumerate(iterator):
            if index >= MAX_JOURNAL_DIRECTORY_ENTRIES:
                break
            name = directory_entry.name
            match = JOURNAL_FILE_RE.fullmatch(name)
            if match is None:
                continue
            path = os.path.join(root, name)
            if not _regular_journal_file(path):
                continue
            try:
                info = os.stat(path)
            except OSError:
                continue
            size = int(info.st_size)
            if size < 0 or size > MAX_JOURNAL_BYTES:
                continue
            values.append({
                "mtime": float(info.st_mtime),
                "name": name,
                "path": path,
                "match": match,
                "size": size,
            })
    finally:
        iterator.close()
    return values


def _enforce_console_journal_retention(directory, protected_path=""):
    """Bound only the generic Console spool; shader/security audits are untouched."""
    root = _fixed_journal_directory(directory)
    protected = os.path.normcase(os.path.abspath(protected_path)) if protected_path else ""
    candidates = _journal_candidates(root)
    removals = set()

    own = [item for item in candidates if (
        item["match"].group("session") == _JOURNAL_SESSION_ID and
        int(item["match"].group("pid")) == os.getpid())]
    own.sort(key=lambda item: int(item["match"].group("segment")), reverse=True)
    for item in own[MAX_JOURNAL_SEGMENTS_PER_SESSION:]:
        removals.add(item["path"])

    retained = [item for item in candidates if item["path"] not in removals]
    retained.sort(key=lambda item: (item["mtime"], item["name"]), reverse=True)
    kept_bytes = 0
    for index, item in enumerate(retained):
        if (index >= MAX_JOURNAL_RETENTION_FILES or
                kept_bytes + item["size"] > MAX_JOURNAL_RETENTION_BYTES):
            removals.add(item["path"])
        else:
            kept_bytes += item["size"]

    removed = 0
    for path in sorted(removals):
        if protected and os.path.normcase(os.path.abspath(path)) == protected:
            continue
        # Revalidate immediately before deletion. Never delete an invalid name,
        # directory, symlink, reparse point, or hard link after a directory race.
        name = os.path.basename(path)
        if (JOURNAL_FILE_RE.fullmatch(name) is None or
                os.path.dirname(os.path.abspath(path)) != root or
                not _regular_journal_file(path)):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError:
            # A live producer may still own the segment. Retention is best effort
            # and a later bounded check will retry without disrupting publishing.
            pass
    return removed


def _maybe_enforce_console_journal_retention(directory, protected_path="",
                                             force=False):
    """Throttle reader-side retention while keeping a fixed safety bound."""
    global _JOURNAL_LAST_RETENTION_MONOTONIC
    now = time.monotonic()
    with _JOURNAL_LOCK:
        if (not force and _JOURNAL_LAST_RETENTION_MONOTONIC > 0 and
                now - _JOURNAL_LAST_RETENTION_MONOTONIC <
                JOURNAL_RETENTION_MIN_INTERVAL_SECONDS):
            return 0
        removed = _enforce_console_journal_retention(
            directory, protected_path=protected_path)
        _JOURNAL_LAST_RETENTION_MONOTONIC = now
        return removed


def _ensure_fixed_journal_directory(directory):
    directory = _fixed_journal_directory(directory)
    parent = os.path.dirname(directory)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    if os.path.exists(parent) and _is_reparse(parent):
        raise ShaderConsoleError("Bread console journal parent is redirected")
    if not os.path.isdir(directory):
        os.makedirs(directory)
    if _is_reparse(directory):
        raise ShaderConsoleError("Bread console journal directory is redirected")


def publish_console_event(module, severity, message, phase="", code="",
                          context=None, process_role="bridge"):
    """Append one trusted fixed-program event; no caller-controlled path exists.

    The API deliberately accepts only display scalars and a small context
    allow-list. It cannot carry a command, executable, argument list or source
    path. Each process owns its journal segment, so publishers never replace or
    delete another process's audit evidence.
    """
    global _JOURNAL_SEQUENCE, _JOURNAL_SEGMENT, _JOURNAL_RETENTION_COUNTER
    module_value = _console_module(module)
    supplied_severity = str(severity or "info").strip().lower()
    if supplied_severity not in (
            "info", "progress", "success", "note", "warning", "warn",
            "error", "fatal", "failure", "failed"):
        raise ShaderConsoleError("console event severity is invalid")
    severity_value = normalise_console_severity(severity)
    role_value = str(process_role or "bridge").strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", role_value) is None:
        raise ShaderConsoleError("console process role is invalid")
    message_value = _bounded(message)
    if not message_value:
        raise ShaderConsoleError("console event message is empty")
    phase_value = _bounded(phase or module_value, 128)
    code_value = _bounded(code, 256)
    context_value = _console_context(context)
    directory = default_console_journal_directory()
    with _JOURNAL_LOCK:
        _ensure_fixed_journal_directory(directory)
        _JOURNAL_SEQUENCE += 1
        unix_ms = int(time.time() * 1000)
        event = {
            "schema": JOURNAL_EVENT_SCHEMA,
            "schemaVersion": 1,
            "sessionId": _JOURNAL_SESSION_ID,
            "sequence": _JOURNAL_SEQUENCE,
            "unixMs": unix_ms,
            "at": _utc_now_text(unix_ms),
            "processRole": role_value,
            "processId": os.getpid(),
            "module": module_value,
            "severity": severity_value,
            "phase": phase_value,
            "code": code_value,
            "message": message_value,
            "context": context_value,
        }
        encoded = (json.dumps(
            event, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_JOURNAL_LINE_BYTES:
            raise ShaderConsoleError("console event exceeds its bounded size")
        path = _journal_filename(directory, role_value, _JOURNAL_SEGMENT)
        try:
            if os.path.exists(path) and not _regular_journal_file(path):
                raise ShaderConsoleError(
                    "console event journal is not an ordinary private file")
            if (os.path.isfile(path) and
                    os.path.getsize(path) + len(encoded) > MAX_JOURNAL_BYTES):
                _JOURNAL_SEGMENT += 1
                path = _journal_filename(directory, role_value, _JOURNAL_SEGMENT)
                if os.path.exists(path) and not _regular_journal_file(path):
                    raise ShaderConsoleError(
                        "console event journal segment is redirected")
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags, 0o600)
            try:
                offset = 0
                while offset < len(encoded):
                    written = os.write(descriptor, encoded[offset:])
                    if written <= 0:
                        raise OSError("console event journal made no write progress")
                    offset += written
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ShaderConsoleError(
                "could not append Bread console event: %s" % _bounded(exc, 512))
        _JOURNAL_RETENTION_COUNTER += 1
        if (_JOURNAL_RETENTION_COUNTER == 1 or
                _JOURNAL_RETENTION_COUNTER % JOURNAL_RETENTION_CHECK_INTERVAL == 0):
            _maybe_enforce_console_journal_retention(
                directory, protected_path=path, force=True)
    return dict(event)


def _positive_integer(value, default=0, maximum=0x7FFFFFFF):
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value < 0 or value > maximum:
        return default
    return value


def _is_reparse(path):
    """Reject symlinks and Windows reparse points before following them."""
    _observe_library_path(path)
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if os.path.islink(path):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _normal_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def _same_path(first, second):
    return _normal_path(first) == _normal_path(second)


def _within(root, path):
    try:
        root_value = _normal_path(root)
        path_value = _normal_path(path)
        return os.path.commonpath((root_value, path_value)) == root_value
    except (OSError, ValueError):
        return False


def _regular_file(path, maximum, field):
    _check_console_read_cancelled()
    value = os.path.abspath(str(path))
    if not os.path.isfile(value) or _is_reparse(value):
        raise ShaderConsoleError("%s is unavailable or redirected" % field)
    size = os.path.getsize(value)
    if size < 0 or size > maximum:
        raise ShaderConsoleError("%s exceeds its bounded size" % field)
    return value


def _regular_file_in_tree(root, path, maximum, field):
    """Accept a regular file only through ordinary directories under root."""
    _check_console_read_cancelled()
    root_value = _directory(root, field + " root")
    value = os.path.abspath(str(path))
    if not _within(root_value, value):
        raise ShaderConsoleError("%s escapes its fixed directory" % field)
    current = os.path.dirname(value)
    while not _same_path(current, root_value):
        if not _within(root_value, current) or _is_reparse(current):
            raise ShaderConsoleError("%s directory is redirected" % field)
        parent = os.path.dirname(current)
        if _same_path(parent, current):
            raise ShaderConsoleError("%s directory is invalid" % field)
        current = parent
    return _regular_file(value, maximum, field)


def _validate_windows_file_handle(api, handle, filename, field):
    """Bind a Win32 handle to the exact ordinary file named by the caller."""
    information = _WindowsByHandleFileInformation()
    if not api.get_information(handle, ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    if information.attributes & (0x400 | 0x10):  # REPARSE_POINT | DIRECTORY
        raise ShaderConsoleError("%s is unavailable or redirected" % field)

    capacity = 32768
    buffer = ctypes.create_unicode_buffer(capacity)
    length = int(api.get_final_path(handle, buffer, capacity, 0))
    if length <= 0 or length >= capacity:
        raise ctypes.WinError(ctypes.get_last_error())
    final_path = buffer.value
    if final_path.startswith("\\\\?\\UNC\\"):
        final_path = "\\\\" + final_path[8:]
    elif final_path.startswith("\\\\?\\"):
        final_path = final_path[4:]
    if (os.path.normcase(os.path.abspath(final_path)) !=
            os.path.normcase(os.path.abspath(filename))):
        raise ShaderConsoleError("%s is unavailable or redirected" % field)


def _read_file_bytes(path, maximum, field):
    _observe_library_path(path)
    raw = _read_file_bytes_uncached(path, maximum, field)
    dependencies = getattr(_console_read_scope, "library_dependencies", None)
    if dependencies is not None:
        filename = os.path.abspath(str(path))
        record = (maximum, field, hashlib.sha256(raw).digest())
        previous = dependencies["files"].setdefault(filename, record)
        if previous[2] != record[2]:
            dependencies["changed"] = True
        _observe_library_path(path)
    return raw


def _read_file_bytes_uncached(path, maximum, field):
    """Read a bounded file without blocking an atomic Windows replacement.

    CPython's ordinary Windows file open does not promise delete sharing. The
    live-edit writer publishes audits with ``os.replace``; a 100ms console
    reader must therefore opt into READ|WRITE|DELETE sharing explicitly.
    """
    filename = _regular_file(path, maximum, field)
    if os.name != "nt":
        try:
            with open(filename, "rb") as stream:
                raw = stream.read(maximum + 1)
        except OSError as exc:
            raise ShaderConsoleError(
                "%s is unavailable: %s" % (field, _bounded(exc, 512)))
        if len(raw) > maximum:
            raise ShaderConsoleError("%s exceeds its bounded size" % field)
        return raw

    handle = None
    close_handle = None
    try:
        api = _windows_file_api()
        close_handle = api.close_handle

        handle = api.create_file(
            filename, 0x80000000, 0x1 | 0x2 | 0x4, None, 3,
            0x08000000 | 0x00200000, None)
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, 0, invalid):
            raise ctypes.WinError(ctypes.get_last_error())
        _validate_windows_file_handle(api, handle, filename, field)
        size = ctypes.c_longlong()
        if not api.get_size(handle, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if size.value < 0 or size.value > maximum:
            raise ShaderConsoleError("%s exceeds its bounded size" % field)
        remaining = int(size.value)
        chunks = []
        while remaining > 0:
            _check_console_read_cancelled()
            request = min(remaining, 64 * 1024)
            buffer = ctypes.create_string_buffer(request)
            received = ctypes.c_uint32()
            if not api.read_file(
                    handle, buffer, request, ctypes.byref(received), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if received.value <= 0:
                break
            chunks.append(buffer.raw[:received.value])
            remaining -= received.value
        if remaining != 0:
            raise ShaderConsoleError("%s changed while it was read" % field)
        return b"".join(chunks)
    except ShaderConsoleError:
        raise
    except (OSError, ValueError) as exc:
        raise ShaderConsoleError(
            "%s is unavailable: %s" % (field, _bounded(exc, 512)))
    finally:
        if close_handle is not None and handle not in (
                None, 0, ctypes.c_void_p(-1).value):
            try:
                close_handle(handle)
            except Exception:
                pass


def _read_file_tail_bytes(path, maximum, tail_maximum, field):
    """Read only the newest bounded tail while preserving complete JSONL rows."""
    filename = _regular_file(path, maximum, field)
    tail_limit = max(1, min(int(tail_maximum), int(maximum)))

    def _complete_tail(raw, wanted_start):
        if wanted_start <= 0:
            return raw
        # The read starts one byte before the requested tail. If that byte is a
        # newline, the requested offset already starts on a record boundary;
        # otherwise discard the one partial record deterministically.
        if raw.startswith(b"\n"):
            return raw[1:]
        boundary = raw.find(b"\n")
        return raw[boundary + 1:] if boundary >= 0 else b""

    if os.name != "nt":
        try:
            with open(filename, "rb") as stream:
                size = int(os.fstat(stream.fileno()).st_size)
                if size < 0 or size > maximum:
                    raise ShaderConsoleError("%s exceeds its bounded size" % field)
                wanted_start = max(0, size - tail_limit)
                read_start = max(0, wanted_start - 1)
                stream.seek(read_start, os.SEEK_SET)
                raw = stream.read(size - read_start)
                return _complete_tail(raw, wanted_start)
        except ShaderConsoleError:
            raise
        except OSError as exc:
            raise ShaderConsoleError(
                "%s is unavailable: %s" % (field, _bounded(exc, 512)))

    handle = None
    close_handle = None
    try:
        api = _windows_file_api()
        close_handle = api.close_handle

        handle = api.create_file(
            filename, 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x08000000, None)
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, 0, invalid):
            raise ctypes.WinError(ctypes.get_last_error())
        size_value = ctypes.c_longlong()
        if not api.get_size(handle, ctypes.byref(size_value)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = int(size_value.value)
        if size < 0 or size > maximum:
            raise ShaderConsoleError("%s exceeds its bounded size" % field)
        wanted_start = max(0, size - tail_limit)
        read_start = max(0, wanted_start - 1)
        if read_start and not api.set_pointer(
                handle, read_start, None, 0):  # FILE_BEGIN
            raise ctypes.WinError(ctypes.get_last_error())
        remaining = size - read_start
        chunks = []
        while remaining > 0:
            _check_console_read_cancelled()
            request = min(remaining, 64 * 1024)
            buffer = ctypes.create_string_buffer(request)
            received = ctypes.c_uint32()
            if not api.read_file(
                    handle, buffer, request, ctypes.byref(received), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if received.value <= 0:
                break
            chunks.append(buffer.raw[:received.value])
            remaining -= received.value
        return _complete_tail(b"".join(chunks), wanted_start)
    except ShaderConsoleError:
        raise
    except (OSError, ValueError) as exc:
        raise ShaderConsoleError(
            "%s is unavailable: %s" % (field, _bounded(exc, 512)))
    finally:
        if close_handle is not None and handle not in (
                None, 0, ctypes.c_void_p(-1).value):
            try:
                close_handle(handle)
            except Exception:
                pass


def _directory(path, field):
    _check_console_read_cancelled()
    value = os.path.abspath(str(path))
    if not os.path.isdir(value) or _is_reparse(value):
        raise ShaderConsoleError("%s is unavailable or redirected" % field)
    return value


def _read_json(path, maximum, field):
    try:
        raw = _read_file_bytes(path, maximum, field)
        _check_console_read_cancelled()
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except ShaderConsoleError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ShaderConsoleError("%s is invalid: %s" % (field, _bounded(exc, 512)))
    if not isinstance(value, dict):
        raise ShaderConsoleError("%s must contain one JSON object" % field)
    return value


def _safe_relative(root, relative, field):
    if not isinstance(relative, str) or not relative or len(relative) > 4096:
        raise ShaderConsoleError("%s is invalid" % field)
    if os.path.isabs(relative):
        raise ShaderConsoleError("%s must be workspace-relative" % field)
    candidate = os.path.abspath(os.path.join(root, relative.replace("/", os.sep)))
    if not _within(root, candidate):
        raise ShaderConsoleError("%s escapes its workspace" % field)
    return candidate


def _project_canonical_sha256(value, omitted_field=""):
    _check_console_read_cancelled()
    material = dict(value) if isinstance(value, dict) else value
    if omitted_field and isinstance(material, dict):
        material.pop(omitted_field, None)
    try:
        payload = json.dumps(
            material, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ShaderConsoleError(
            "project document is not canonical JSON: %s" % _bounded(exc, 512))
    return hashlib.sha256(payload).hexdigest()


def _project_text(value, field, maximum=4096, allow_empty=False,
                  single_line=False):
    if not isinstance(value, str):
        raise ShaderConsoleError("%s must be text" % field)
    clean = value.strip()
    if ((not clean and not allow_empty) or len(clean) > maximum or
            "\x00" in clean or (single_line and ("\r" in clean or "\n" in clean))):
        raise ShaderConsoleError("%s has invalid content" % field)
    return clean


def _project_sha256(value, field, allow_empty=False):
    clean = str(value or "").strip().lower()
    if not clean and allow_empty:
        return ""
    if SHA256_RE.fullmatch(clean) is None:
        raise ShaderConsoleError("%s must be a lowercase SHA-256" % field)
    return clean


def _project_token(value, field):
    clean = str(value or "").strip().lower()
    if EXACT_TOKEN_RE.fullmatch(clean) is None:
        raise ShaderConsoleError("%s must be 32 lowercase hexadecimal characters" % field)
    return clean


def _project_positive(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ShaderConsoleError("%s must be a positive integer" % field)
    return value


def _project_nonnegative(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ShaderConsoleError("%s must be a non-negative integer" % field)
    return value


def _verify_project_seal(value, hash_field, field):
    digest = _project_sha256(value.get(hash_field), field + "." + hash_field)
    if digest != _project_canonical_sha256(value, hash_field):
        raise ShaderConsoleError("%s seal is invalid" % field)
    return digest


def _read_project_json(path, maximum, field, root=""):
    filename = os.path.abspath(str(path))
    if root:
        _regular_file_in_tree(root, filename, maximum, field)
    raw = _read_file_bytes(filename, maximum, field)
    _check_console_read_cancelled()
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ShaderConsoleError("%s is invalid: %s" % (field, _bounded(exc, 512)))
    if not isinstance(value, dict):
        raise ShaderConsoleError("%s must contain one JSON object" % field)
    return value, hashlib.sha256(raw).hexdigest()


def _project_relative_path(root, relative, field, expected=""):
    clean = _project_text(relative, field, 4096)
    if (clean != relative or "\\" in clean or clean.startswith("/") or
            re.match(r"^[A-Za-z]:", clean)):
        raise ShaderConsoleError("%s must be a canonical project-relative path" % field)
    parts = clean.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ShaderConsoleError("%s must be a canonical project-relative path" % field)
    if expected and clean != expected:
        raise ShaderConsoleError("%s is not content-addressed" % field)
    root_value = _directory(root, field + " root")
    candidate = os.path.abspath(os.path.join(root_value, *parts))
    if not _within(root_value, candidate):
        raise ShaderConsoleError("%s escapes its project" % field)
    current = root_value
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if not os.path.isdir(current) or _is_reparse(current):
            raise ShaderConsoleError("%s directory is unavailable or redirected" % field)
    return candidate


def _project_api_family(value, field, allow_empty=True):
    clean = _project_text(value, field, 64, allow_empty=allow_empty,
                          single_line=True)
    normal = re.sub(r"[^a-z0-9]+", "", clean.lower())
    normal = {
        "direct3d11": "d3d11", "directx11": "d3d11",
        "direct3d12": "d3d12", "directx12": "d3d12",
    }.get(normal, normal)
    if clean != normal:
        raise ShaderConsoleError("%s is not canonical" % field)
    return clean


def _project_identity_api_family(value):
    clean = re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())
    clean = {
        "direct3d11": "d3d11", "directx11": "d3d11",
        "direct3d12": "d3d12", "directx12": "d3d12",
        "opengl": "opengl", "vulkan": "vulkan", "metal": "metal",
    }.get(clean, clean)
    if not clean or len(clean) > 64:
        raise ShaderConsoleError("project Shader API family is invalid")
    return clean


def _project_shader_encoding(value):
    clean = re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())
    encoding = {
        "dxbc": "DXBC",
        "dxil": "DXIL",
        "spirv": "SPIR-V",
        "openglspirv": "OpenGL-SPIR-V",
        "glsl": "GLSL",
        "metallib": "METALLIB",
    }.get(clean)
    if encoding is None:
        raise ShaderConsoleError(
            "project Shader encoding is unsupported or not canonicalizable")
    return encoding


def _project_stable_shader_encoding(value):
    return {
        "DXBC": "dxbc",
        "DXIL": "dxil",
        "SPIR-V": "spirv",
        "OpenGL-SPIR-V": "opengl-spirv",
        "GLSL": "glsl",
        "METALLIB": "metallib",
    }[_project_shader_encoding(value)]


def _project_normalized_stage(value):
    clean = re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())
    stage = {
        "vs": "vertex", "vertex": "vertex",
        "hs": "hull", "hull": "hull", "tesscontrol": "hull",
        "ds": "domain", "domain": "domain", "tesseval": "domain",
        "gs": "geometry", "geometry": "geometry",
        "ps": "fragment", "pixel": "fragment", "fs": "fragment",
        "fragment": "fragment",
        "cs": "compute", "compute": "compute",
        "as": "task", "amplification": "task",
        "task": "task", "ms": "mesh", "mesh": "mesh",
        "raygen": "raygen", "raygeneration": "raygen",
        "intersection": "intersection",
        "anyhit": "anyhit", "closesthit": "closesthit", "miss": "miss",
        "callable": "callable",
    }.get(clean)
    if stage is None:
        raise ShaderConsoleError(
            "project Shader stage is unsupported or not canonicalizable")
    return stage


def _project_stable_shader_identity_sha256(identity):
    material = dict(identity)
    material["encoding"] = _project_stable_shader_encoding(
        identity.get("encoding"))
    return _project_canonical_sha256(material)


def _project_compiler_binding_identity(value):
    if (isinstance(value, dict) and
            value.get("schema") == "bread-native-shader-compiler-binding/v1"):
        # OpenGL compiles GLSL through the replay driver. Its evidence has no
        # external tool hops, but retains the same exact profile/route digest.
        fields = {
            "schema", "schemaVersion", "backend", "api", "sourceFormat",
            "outputFormat", "compilerContract", "targetProfile",
            "routePlanSha256", "executionAuthority", "evidenceOnly",
            "bindingSha256",
        }
        profile = value.get("targetProfile")
        if (set(value) != fields or value.get("schemaVersion") != 1 or
                value.get("backend") != "renderdoc-native" or
                value.get("api") != "opengl" or
                value.get("sourceFormat") != "glsl" or
                value.get("outputFormat") != "glsl" or
                value.get("compilerContract") != "bread-native-shader-compile/v1" or
                value.get("executionAuthority") is not False or
                value.get("evidenceOnly") is not True or
                not isinstance(profile, dict) or
                profile.get("apiEnvironment") != "opengl" or
                profile.get("containerFormat") not in {"glsl", "opengl-spirv"}):
            raise ShaderConsoleError("native compiler binding fields/policy are invalid")
        result = copy.deepcopy(value)
        claimed = result.pop("bindingSha256")
        _project_sha256(result["routePlanSha256"], "compilerBinding.routePlanSha256")
        if (_project_sha256(claimed, "compilerBinding.bindingSha256") !=
                _project_canonical_sha256(result)):
            raise ShaderConsoleError("project compiler binding hash mismatch")
        result["bindingSha256"] = claimed
        return result
    fields = {
        "schema", "schemaVersion", "targetProfile", "routePlanSha256",
        "hops", "executionAuthority", "evidenceOnly", "bindingSha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ShaderConsoleError("project compiler binding fields are invalid")
    if (value.get("schema") !=
            "renderdoc-ultra-shader-compiler-binding/v1" or
            value.get("schemaVersion") != 1 or
            value.get("executionAuthority") is not False or
            value.get("evidenceOnly") is not True or
            not isinstance(value.get("targetProfile"), dict)):
        raise ShaderConsoleError("project compiler binding policy is invalid")
    route_plan = value.get("routePlanSha256")
    if route_plan is not None:
        route_plan = _project_sha256(
            route_plan, "compilerBinding.routePlanSha256")
    raw_hops = value.get("hops")
    if (not isinstance(raw_hops, list) or not raw_hops or
            len(raw_hops) > 4):
        raise ShaderConsoleError("project compiler binding hops are invalid")
    hops = []
    for index, raw_hop in enumerate(raw_hops):
        label = "compilerBinding.hops[%d]" % index
        if not isinstance(raw_hop, dict) or set(raw_hop) != {
                "hop", "routeId", "inputFormat", "outputFormat", "toolId",
                "operation", "toolConfigDigest", "executableSha256",
                "fixedFileHashes", "argumentTemplate", "placeholders"}:
            raise ShaderConsoleError("%s fields are invalid" % label)
        fixed_hashes = raw_hop.get("fixedFileHashes")
        arguments = raw_hop.get("argumentTemplate")
        placeholders = raw_hop.get("placeholders")
        if (not isinstance(fixed_hashes, dict) or len(fixed_hashes) > 64 or
                not isinstance(arguments, list) or len(arguments) > 128 or
                not isinstance(placeholders, list) or
                len(placeholders) > 32):
            raise ShaderConsoleError("%s compiler metadata is invalid" % label)
        clean_fixed = {}
        for path, digest in fixed_hashes.items():
            clean_path = _project_text(
                path, label + ".fixedFilePath", 4096)
            if clean_path in clean_fixed:
                raise ShaderConsoleError(
                    "project compiler fixed file path is duplicated")
            clean_fixed[clean_path] = _project_sha256(
                digest, label + ".fixedFileSha256")
        clean_arguments = [
            _project_text(argument, label + ".argumentTemplate", 8192)
            for argument in arguments
        ]
        clean_placeholders = [
            _project_text(placeholder, label + ".placeholder", 128)
            for placeholder in placeholders
        ]
        if len(set(clean_placeholders)) != len(clean_placeholders):
            raise ShaderConsoleError(
                "project compiler placeholders are duplicated")
        hop = {
            "hop": _project_positive(raw_hop.get("hop"), label + ".hop"),
            "routeId": _project_text(
                raw_hop.get("routeId"), label + ".routeId", 256),
            "inputFormat": _project_text(
                raw_hop.get("inputFormat"), label + ".inputFormat", 64),
            "outputFormat": _project_text(
                raw_hop.get("outputFormat"), label + ".outputFormat", 64),
            "toolId": _project_text(
                raw_hop.get("toolId"), label + ".toolId", 128),
            "operation": _project_text(
                raw_hop.get("operation"), label + ".operation", 64),
            "toolConfigDigest": _project_sha256(
                raw_hop.get("toolConfigDigest"),
                label + ".toolConfigDigest"),
            "executableSha256": _project_sha256(
                raw_hop.get("executableSha256"),
                label + ".executableSha256"),
            "fixedFileHashes": clean_fixed,
            "argumentTemplate": clean_arguments,
            "placeholders": clean_placeholders,
        }
        if hop["hop"] != index + 1:
            raise ShaderConsoleError("project compiler hop order is invalid")
        hops.append(hop)
    result = {
        "schema": "renderdoc-ultra-shader-compiler-binding/v1",
        "schemaVersion": 1,
        "targetProfile": dict(value["targetProfile"]),
        "routePlanSha256": route_plan,
        "hops": hops,
        "executionAuthority": False,
        "evidenceOnly": True,
    }
    digest = _project_canonical_sha256(result)
    if (_project_sha256(value.get("bindingSha256"),
                        "compilerBinding.bindingSha256") != digest):
        raise ShaderConsoleError("project compiler binding hash mismatch")
    result["bindingSha256"] = digest
    if dict(value) != result:
        raise ShaderConsoleError("project compiler binding is not canonical")
    return result


def _project_assembly_disassembly_identity(value):
    if not isinstance(value, dict):
        raise ShaderConsoleError("project assembly/disassembly artifact is invalid")
    allowed = {
        "kind", "role", "format", "path", "artifactPath", "sha256",
        "byteSize", "routePlanSha256", "toolId", "toolConfigDigest",
        "executableSha256", "inputFormat", "inputRawSha256", "routeId",
        "operation", "physicalEvidence", "displayEvidenceOnly",
        "editableSource", "applyAuthorization",
    }
    if (set(value) - allowed or
            not {"kind", "role", "format", "sha256", "byteSize"}.issubset(
                set(value))):
        raise ShaderConsoleError(
            "project assembly/disassembly artifact fields are invalid")
    kind = value.get("kind")
    role = value.get("role")
    artifact_format = value.get("format")
    if role not in ("baseline", "candidate"):
        raise ShaderConsoleError(
            "project assembly/disassembly artifact policy is invalid")
    common = {
        "role": role,
        "sha256": _project_sha256(value.get("sha256"), "disassembly.sha256"),
        "byteSize": _project_positive(
            value.get("byteSize"), "disassembly.byteSize"),
        "routePlanSha256": _project_sha256(
            value.get("routePlanSha256"), "disassembly.routePlanSha256"),
        "toolId": _project_text(
            value.get("toolId"), "disassembly.toolId", 128),
        "toolConfigDigest": _project_sha256(
            value.get("toolConfigDigest"), "disassembly.toolConfigDigest"),
        "executableSha256": _project_sha256(
            value.get("executableSha256"), "disassembly.executableSha256"),
        "physicalEvidence": True,
        "editableSource": False,
        "applyAuthorization": False,
    }
    if kind == "dxil-disassembly":
        forbidden = {
            "inputFormat", "inputRawSha256", "routeId", "operation",
            "displayEvidenceOnly",
        }
        if (artifact_format != "dxil-asm" or
                value.get("physicalEvidence") is not True or
                value.get("editableSource") is not False or
                value.get("applyAuthorization") is not False or
                set(value) & forbidden):
            raise ShaderConsoleError(
                "project assembly/disassembly artifact policy is invalid")
        result = {"kind": "dxil-disassembly"}
        result.update(common)
        result["format"] = "dxil-asm"
        return result
    expected_inputs = {
        "dxbc-asm": "dxbc",
        "spirv-asm": "spirv",
        "opengl-spirv-asm": "opengl-spirv",
    }
    if (kind != "native-disassembly" or
            artifact_format not in expected_inputs or
            value.get("inputFormat") != expected_inputs.get(artifact_format) or
            value.get("operation") != "decompile" or
            value.get("physicalEvidence") is not True or
            value.get("displayEvidenceOnly") is not True or
            value.get("editableSource") is not False or
            value.get("applyAuthorization") is not False):
        raise ShaderConsoleError(
            "project assembly/disassembly artifact policy is invalid")
    result = {"kind": "native-disassembly"}
    result.update(common)
    result.update({
        "format": artifact_format,
        "inputFormat": expected_inputs[artifact_format],
        "inputRawSha256": _project_sha256(
            value.get("inputRawSha256"), "disassembly.inputRawSha256"),
        "routeId": _project_text(
            value.get("routeId"), "disassembly.routeId", 256),
        "operation": "decompile",
        "displayEvidenceOnly": True,
    })
    return result


def _project_assembly_disassembly_relative(identity):
    suffix = {
        "dxil-asm": ".dxil-asm",
        "dxbc-asm": ".dxbc-asm",
        "spirv-asm": ".spvasm",
        "opengl-spirv-asm": ".opengl.spvasm",
    }.get(identity.get("format"))
    if suffix is None:
        raise ShaderConsoleError(
            "project assembly/disassembly artifact format is invalid")
    return "knowledge/workspaces/disassembly/%s%s" % (
        identity["sha256"], suffix)


def _validate_project_disassembly_artifact(root, value):
    identity = _project_assembly_disassembly_identity(value)
    if "path" in value or "artifactPath" not in value:
        raise ShaderConsoleError(
            "project disassembly artifact provenance is invalid")
    expected = _project_assembly_disassembly_relative(identity)
    artifact_path = _project_relative_path(
        root, value.get("artifactPath"), "project disassembly artifact path",
        expected)
    canonical_record = dict(identity)
    canonical_record["artifactPath"] = expected
    if dict(value) != canonical_record:
        raise ShaderConsoleError(
            "project disassembly artifact is not canonical")
    _regular_file_in_tree(
        root, artifact_path, MAX_SOURCE_BYTES, "project disassembly artifact")
    raw = _read_file_bytes(
        artifact_path, MAX_SOURCE_BYTES, "project disassembly artifact")
    if (not raw or len(raw) != identity["byteSize"] or
            hashlib.sha256(raw).hexdigest() != identity["sha256"]):
        raise ShaderConsoleError(
            "project disassembly artifact size or hash mismatch")
    try:
        info = os.lstat(artifact_path)
    except OSError as exc:
        raise ShaderConsoleError(
            "project disassembly artifact metadata is unavailable: %s" %
            _bounded(exc, 512))
    if (not stat.S_ISREG(info.st_mode) or _is_reparse(artifact_path) or
            int(info.st_size) != len(raw)):
        raise ShaderConsoleError(
            "project disassembly artifact changed while it was read")
    return identity, artifact_path


def _project_inventory_origin_identity(value):
    fields = {
        "schema", "schemaVersion", "api", "encoding", "rawSha256",
        "stage", "entryPoint", "abiHash", "layoutHash", "stableKeySha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ShaderConsoleError("project inventory origin fields are invalid")
    if (value.get("schema") !=
            "renderdoc-ultra-project-shader-inventory-origin/v1" or
            value.get("schemaVersion") != 1):
        raise ShaderConsoleError("project inventory origin schema is invalid")
    identity = {
        "api": _project_identity_api_family(value.get("api")),
        "encoding": _project_shader_encoding(value.get("encoding")),
        "rawSha256": _project_sha256(
            value.get("rawSha256"), "inventoryOrigin.rawSha256"),
        "stage": _project_normalized_stage(value.get("stage")),
        "entryPoint": _project_text(
            value.get("entryPoint"), "inventoryOrigin.entryPoint", 512,
            allow_empty=True),
        "abiHash": _project_sha256(
            value.get("abiHash"), "inventoryOrigin.abiHash", True),
        "layoutHash": _project_sha256(
            value.get("layoutHash"), "inventoryOrigin.layoutHash", True),
    }
    stable = _project_stable_shader_identity_sha256(identity)
    if (_project_sha256(value.get("stableKeySha256"),
                        "inventoryOrigin.stableKeySha256") != stable):
        raise ShaderConsoleError("project inventory origin stable key mismatch")
    result = {
        "schema": "renderdoc-ultra-project-shader-inventory-origin/v1",
        "schemaVersion": 1,
    }
    result.update(identity)
    result["stableKeySha256"] = stable
    if dict(value) != result:
        raise ShaderConsoleError("project inventory origin is not canonical")
    return result


def _project_inventory_observation(value):
    fields = {
        "schema", "schemaVersion", "captureId", "inventorySha256",
        "stableKeySha256", "targetResourceId", "anchorEventId",
        "constantBlocksSha256", "workspaceManifestSha256", "sourceSha256",
        "compiledCandidateSha256", "applyAuthorization",
        "crossCaptureResourceIdReuse", "observedAt", "receiptSha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ShaderConsoleError(
            "project inventory observation fields are invalid")
    if (value.get("schema") !=
            "renderdoc-ultra-project-shader-inventory-observation/v1" or
            value.get("schemaVersion") != 1 or
            value.get("applyAuthorization") is not False or
            value.get("crossCaptureResourceIdReuse") is not False):
        raise ShaderConsoleError(
            "project inventory observation policy is invalid")
    anchor = value.get("anchorEventId")
    material = {
        "schema": "renderdoc-ultra-project-shader-inventory-observation/v1",
        "schemaVersion": 1,
        "captureId": _project_sha256(
            value.get("captureId"), "inventoryObservation.captureId"),
        "inventorySha256": _project_sha256(
            value.get("inventorySha256"),
            "inventoryObservation.inventorySha256"),
        "stableKeySha256": _project_sha256(
            value.get("stableKeySha256"),
            "inventoryObservation.stableKeySha256"),
        "targetResourceId": _project_positive(
            value.get("targetResourceId"),
            "inventoryObservation.targetResourceId"),
        "anchorEventId": (
            None if anchor is None else _project_positive(
                anchor, "inventoryObservation.anchorEventId")),
        "constantBlocksSha256": _project_sha256(
            value.get("constantBlocksSha256"),
            "inventoryObservation.constantBlocksSha256"),
        "workspaceManifestSha256": _project_sha256(
            value.get("workspaceManifestSha256"),
            "inventoryObservation.workspaceManifestSha256"),
        "sourceSha256": _project_sha256(
            value.get("sourceSha256"), "inventoryObservation.sourceSha256"),
        "compiledCandidateSha256": _project_sha256(
            value.get("compiledCandidateSha256"),
            "inventoryObservation.compiledCandidateSha256", True),
        "applyAuthorization": False,
        "crossCaptureResourceIdReuse": False,
    }
    observed_at = _project_text(
        value.get("observedAt"), "inventoryObservation.observedAt", 128)
    receipt = _project_canonical_sha256(material)
    if (_project_sha256(value.get("receiptSha256"),
                        "inventoryObservation.receiptSha256") != receipt):
        raise ShaderConsoleError("project inventory observation hash mismatch")
    result = dict(material)
    result.update({"observedAt": observed_at, "receiptSha256": receipt})
    if dict(value) != result:
        raise ShaderConsoleError(
            "project inventory observation is not canonical")
    return result


def _active_project_registry_path(selection_path):
    # Active selection is private to each application instance, while the
    # project library remains shared. Legacy installations colocated both.
    configured = str(os.environ.get("RENDERDOC_ULTRA_PROJECT_REGISTRY", "") or "").strip()
    return (os.path.abspath(os.path.expanduser(configured)) if configured else
            os.path.join(os.path.dirname(selection_path), "project-registry.json"))


def _validate_project_registry(selection_path, workspace_id, project_root):
    registry_path = _active_project_registry_path(selection_path)
    value, file_sha256 = _read_project_json(
        registry_path, MAX_REGISTRY_BYTES, "active project registry",
        os.path.dirname(registry_path))
    if set(value) != {
            "schema", "schemaVersion", "projects", "updatedAt",
            "registrySha256"}:
        raise ShaderConsoleError("active project registry fields are invalid")
    if (value.get("schema") != ACTIVE_PROJECT_REGISTRY_SCHEMA or
            value.get("schemaVersion") != 1):
        raise ShaderConsoleError("active project registry schema is invalid")
    registry_sha256 = _verify_project_seal(
        value, "registrySha256", "active project registry")
    projects = value.get("projects")
    if not isinstance(projects, list) or len(projects) > MAX_PROJECT_KNOWLEDGE:
        raise ShaderConsoleError("active project registry entries are invalid")
    matches = []
    roots = set()
    workspace_ids = set()
    for record in projects:
        if not isinstance(record, dict) or set(record) != {
                "workspaceId", "projectRoot", "name", "registeredAt"}:
            raise ShaderConsoleError("active project registry entry is invalid")
        token = _project_token(record.get("workspaceId"),
                               "registered workspaceId")
        root_text = _project_text(
            record.get("projectRoot"), "registered projectRoot", 4096)
        if not os.path.isabs(root_text):
            raise ShaderConsoleError("registered projectRoot must be absolute")
        root_key = os.path.normcase(os.path.abspath(root_text))
        if root_key in roots or token in workspace_ids:
            raise ShaderConsoleError("active project registry contains a duplicate")
        roots.add(root_key)
        workspace_ids.add(token)
        _project_text(record.get("name"), "registered project name", 256)
        _project_text(record.get("registeredAt"), "project registration time", 128)
        if (_same_path(root_text, project_root) and token == workspace_id):
            matches.append(record)
    if len(matches) != 1:
        raise ShaderConsoleError(
            "active project selection is not bound by the project registry")
    return {
        "path": registry_path,
        "fileSha256": file_sha256,
        "registrySha256": registry_sha256,
    }


def _validate_active_project_selection(selection_path):
    parent = _directory(os.path.dirname(selection_path),
                        "active project selection directory")
    value, file_sha256 = _read_project_json(
        selection_path, MAX_REGISTRY_BYTES, "active project selection", parent)
    if set(value) != {
            "schema", "schemaVersion", "workspaceId", "projectRoot",
            "selectedAt", "selectionSha256"}:
        raise ShaderConsoleError("active project selection fields are invalid")
    if (value.get("schema") != ACTIVE_PROJECT_SELECTION_SCHEMA or
            value.get("schemaVersion") != 1):
        raise ShaderConsoleError("active project selection schema is invalid")
    selection_sha256 = _verify_project_seal(
        value, "selectionSha256", "active project selection")
    workspace_id = _project_token(value.get("workspaceId"),
                                  "active project workspaceId")
    project_text = _project_text(value.get("projectRoot"), "active projectRoot", 4096)
    if not os.path.isabs(project_text):
        raise ShaderConsoleError("active projectRoot must be absolute")
    if _is_reparse(project_text):
        raise ShaderConsoleError("active projectRoot is redirected")
    project_root = _directory(project_text, "active projectRoot")
    if _same_path(project_root, os.path.abspath(os.path.join(project_root, os.pardir))):
        raise ShaderConsoleError("active projectRoot must not be a filesystem root")
    _project_text(value.get("selectedAt"), "active project selection time", 128)
    registry = _validate_project_registry(
        selection_path, workspace_id, project_root)
    return {
        "path": selection_path,
        "fileSha256": file_sha256,
        "selectionSha256": selection_sha256,
        "workspaceId": workspace_id,
        "projectRoot": project_root,
        "registry": registry,
    }


def _validate_active_project_workspace(binding):
    root = binding["projectRoot"]
    manifest_path = _project_relative_path(
        root, ACTIVE_PROJECT_LAYOUT["workspaceManifest"],
        "project workspace manifest path")
    value, file_sha256 = _read_project_json(
        manifest_path, MAX_REGISTRY_BYTES, "project workspace manifest", root)
    if set(value) != {
            "schema", "schemaVersion", "workspaceId", "name", "state",
            "scope", "assetRoot", "layout", "createdAt", "updatedAt",
            "configSha256"}:
        raise ShaderConsoleError("project workspace manifest fields are invalid")
    if (value.get("schema") != ACTIVE_PROJECT_WORKSPACE_SCHEMA or
            value.get("schemaVersion") != 1):
        raise ShaderConsoleError("project workspace manifest schema is invalid")
    config_sha256 = _verify_project_seal(
        value, "configSha256", "project workspace manifest")
    if _project_token(value.get("workspaceId"), "project workspaceId") != binding[
            "workspaceId"]:
        raise ShaderConsoleError(
            "active selection does not match the project workspace")
    name = _project_text(value.get("name"), "project workspace name", 256,
                         allow_empty=True, single_line=True)
    state = value.get("state")
    if state not in ("pending-scope", "bound"):
        raise ShaderConsoleError("project workspace state is invalid")
    scope = value.get("scope")
    if not isinstance(scope, dict) or set(scope) != {
            "gameId", "buildId", "platform", "apiFamily"}:
        raise ShaderConsoleError("project workspace scope is invalid")
    game = _project_text(scope.get("gameId"), "scope.gameId", 256,
                         allow_empty=True, single_line=True)
    build = _project_text(scope.get("buildId"), "scope.buildId", 256,
                          allow_empty=True, single_line=True)
    _project_text(scope.get("platform"), "scope.platform", 128,
                  allow_empty=True, single_line=True)
    _project_api_family(scope.get("apiFamily"), "scope.apiFamily", True)
    if state == "pending-scope" and any(scope.values()):
        raise ShaderConsoleError("pending project carries a partial scope")
    if state == "bound" and (not game or not build):
        raise ShaderConsoleError("bound project has incomplete scope")
    if value.get("layout") != ACTIVE_PROJECT_LAYOUT:
        raise ShaderConsoleError("project workspace layout is invalid")
    for key in (
            "metadataDirectory", "captureCatalogs", "shaderWorkspaces",
            "sharedDictionaries", "sharedExplanatory"):
        path = _project_relative_path(
            root, ACTIVE_PROJECT_LAYOUT[key], "project layout.%s" % key)
        _directory(path, "project layout.%s" % key)
    asset_root = _project_text(value.get("assetRoot"), "project assetRoot", 4096,
                               allow_empty=True)
    if asset_root and (not os.path.isabs(asset_root) or
                       _within(root, asset_root) or _same_path(root, asset_root)):
        raise ShaderConsoleError("project assetRoot has an unsafe boundary")
    _project_text(value.get("createdAt"), "project creation time", 128)
    _project_text(value.get("updatedAt"), "project update time", 128)
    return {
        "path": manifest_path,
        "fileSha256": file_sha256,
        "configSha256": config_sha256,
        "name": name,
        "state": state,
        "scope": dict(scope),
    }


def _project_manual_candidate_identity(value):
    # Saved manual edits carry source/binary provenance, not an ABI verdict.
    # Keep this projection in sync with Host project_manual_shader.compiled_identity.
    fields = {"schema", "schemaVersion", "rawSha256", "codeSha256", "byteSize", "kind",
              "sourceSha256", "sourceRevision", "targetProfile", "originBaselineAbiSha256",
              "validationScope", "semanticEquivalenceVerified", "manualApplyReceipt"}
    if (set(value) - {"artifactPath"} != fields or value.get("schema") != COMPILED_CANDIDATE_SCHEMA
            or value.get("schemaVersion") != 1
            or value.get("validationScope") != "manual-edit-unvalidated"
            or value.get("semanticEquivalenceVerified") is not False
            or not isinstance(value.get("targetProfile"), dict)):
        raise ShaderConsoleError("manual project candidate policy is invalid")
    receipt = value.get("manualApplyReceipt")
    receipt_fields = {"schema", "schemaVersion", "validationScope", "manualEdit", "replayAbiValidated",
                      "originalRawSha256", "candidateRawSha256", "sourceSha256", "sourceRevision",
                      "stage", "entryPoint", "targetProfile", "api", "encoding"}
    if (not isinstance(receipt, dict) or set(receipt) != receipt_fields
            or receipt.get("schema") != "bread-manual-shader-publication-receipt/v1"
            or receipt.get("schemaVersion") != 1
            or receipt.get("validationScope") != "manual-edit-unvalidated"
            or receipt.get("manualEdit") is not True or receipt.get("replayAbiValidated") is not False
            or not isinstance(receipt.get("targetProfile"), dict)):
        raise ShaderConsoleError("manual project candidate provenance is invalid")
    for key in ("originalRawSha256", "candidateRawSha256", "sourceSha256"):
        _project_sha256(receipt.get(key), "manualApplyReceipt.%s" % key)
    for key in ("stage", "api", "encoding"):
        _project_text(receipt.get(key), "manualApplyReceipt.%s" % key, 128, single_line=True)
    _project_text(receipt.get("entryPoint"), "manualApplyReceipt.entryPoint", 1024,
                  allow_empty=True, single_line=True)
    _project_positive(receipt.get("sourceRevision"), "manualApplyReceipt.sourceRevision")
    for key in ("rawSha256", "codeSha256", "sourceSha256", "originBaselineAbiSha256"):
        _project_sha256(value.get(key), "compiledCandidate.%s" % key)
    _project_positive(value.get("byteSize"), "compiledCandidate.byteSize")
    _project_positive(value.get("sourceRevision"), "compiledCandidate.sourceRevision")
    _project_text(value.get("kind"), "compiledCandidate.kind", 64, single_line=True)
    if (value["rawSha256"] != receipt["candidateRawSha256"]
            or value["sourceSha256"] != receipt["sourceSha256"]
            or value["sourceRevision"] != receipt["sourceRevision"]
            or value["targetProfile"] != receipt["targetProfile"]):
        raise ShaderConsoleError("manual project candidate provenance differs")
    return {key: value[key] for key in fields}


def _project_compiled_candidate_identity(value):
    if isinstance(value, dict) and value.get("validationScope") == "manual-edit-unvalidated":
        return _project_manual_candidate_identity(value)
    required = {
        "schema", "schemaVersion", "rawSha256", "codeSha256", "byteSize",
        "kind", "sourceSha256", "sourceRevision", "targetProfile",
        "originBaselineAbiSha256", "originCandidateAbiSha256",
        "originAbiReportSha256", "originRequestId", "validationScope",
        "semanticEquivalenceVerified",
    }
    if (not isinstance(value, dict) or
            not required.issubset(set(value)) or
            set(value) - required - {"artifactPath"}):
        raise ShaderConsoleError("project compiled candidate fields are invalid")
    if (value.get("schema") != COMPILED_CANDIDATE_SCHEMA or
            value.get("schemaVersion") != 1 or
            value.get("validationScope") != "replay-reflection-abi-compatible" or
            value.get("semanticEquivalenceVerified") is not False):
        raise ShaderConsoleError("project compiled candidate policy is invalid")
    profile = value.get("targetProfile")
    if not isinstance(profile, dict):
        raise ShaderConsoleError("project compiled candidate targetProfile is invalid")
    identity = {
        "schema": COMPILED_CANDIDATE_SCHEMA,
        "schemaVersion": 1,
        "rawSha256": _project_sha256(
            value.get("rawSha256"), "compiledCandidate.rawSha256"),
        "codeSha256": _project_sha256(
            value.get("codeSha256"), "compiledCandidate.codeSha256"),
        "byteSize": _project_positive(
            value.get("byteSize"), "compiledCandidate.byteSize"),
        "kind": _project_text(value.get("kind"), "compiledCandidate.kind", 64,
                              single_line=True),
        "sourceSha256": _project_sha256(
            value.get("sourceSha256"), "compiledCandidate.sourceSha256"),
        "sourceRevision": _project_positive(
            value.get("sourceRevision"), "compiledCandidate.sourceRevision"),
        "targetProfile": dict(profile),
        "originBaselineAbiSha256": _project_sha256(
            value.get("originBaselineAbiSha256"),
            "compiledCandidate.originBaselineAbiSha256"),
        "originCandidateAbiSha256": _project_sha256(
            value.get("originCandidateAbiSha256"),
            "compiledCandidate.originCandidateAbiSha256"),
        "originAbiReportSha256": _project_sha256(
            value.get("originAbiReportSha256"),
            "compiledCandidate.originAbiReportSha256"),
        "originRequestId": _project_text(
            value.get("originRequestId"), "compiledCandidate.originRequestId", 256),
        "validationScope": "replay-reflection-abi-compatible",
        "semanticEquivalenceVerified": False,
    }
    return identity


def _validate_project_compiled_candidate(root, value, record):
    identity = _project_compiled_candidate_identity(value)
    receipt = identity.get("manualApplyReceipt")
    if receipt is not None and any(receipt.get(key) != record.get(target) for key, target in (
            ("originalRawSha256", "rawSha256"), ("stage", "stage"), ("entryPoint", "entryPoint"))):
        raise ShaderConsoleError("manual project candidate original Shader differs")
    if "artifactPath" not in value:
        raise ShaderConsoleError(
            "project compiled candidate artifact provenance is invalid")
    if (identity["sourceSha256"] != record["sourceSha256"] or
            identity["targetProfile"] != record["targetProfile"] or
            identity["originBaselineAbiSha256"] != record["baselineAbiSha256"]):
        raise ShaderConsoleError("project compiled candidate binding is invalid")
    expected = "knowledge/workspaces/binaries/%s.bin" % identity["rawSha256"]
    artifact_path = _project_relative_path(
        root, value.get("artifactPath"), "project compiled candidate artifact path",
        expected)
    canonical_record = dict(identity)
    canonical_record["artifactPath"] = expected
    if dict(value) != canonical_record:
        raise ShaderConsoleError(
            "project compiled candidate is not canonical")
    _regular_file_in_tree(
        root, artifact_path, MAX_PROJECT_COMPILED_CANDIDATE_BYTES,
        "project compiled candidate artifact")
    raw = _read_file_bytes(
        artifact_path, MAX_PROJECT_COMPILED_CANDIDATE_BYTES,
        "project compiled candidate artifact")
    if (not raw or len(raw) != identity["byteSize"] or
            hashlib.sha256(raw).hexdigest() != identity["rawSha256"]):
        raise ShaderConsoleError(
            "project compiled candidate artifact size or hash mismatch")
    try:
        info = os.lstat(artifact_path)
    except OSError as exc:
        raise ShaderConsoleError(
            "project compiled candidate artifact metadata is unavailable: %s" %
            _bounded(exc, 512))
    if (not stat.S_ISREG(info.st_mode) or _is_reparse(artifact_path) or
            int(info.st_size) != len(raw)):
        raise ShaderConsoleError(
            "project compiled candidate artifact changed while it was read")
    return identity, artifact_path


def _project_workspace_source_format(value):
    language = value.get("sourceFormat", "hlsl")
    if language not in ("hlsl", "glsl"):
        raise ShaderConsoleError("project workspace source format must be HLSL or GLSL")
    return language


def _project_inventory_workspace_artifact_identity(value):
    if not isinstance(value, dict):
        raise ShaderConsoleError("project inventory workspace artifact is invalid")
    origin = _project_inventory_origin_identity(value.get("inventoryOrigin"))
    profile = value.get("targetProfile")
    if not isinstance(profile, dict):
        raise ShaderConsoleError(
            "project inventory workspace targetProfile is invalid")
    physical = {
        "stage": _project_normalized_stage(value.get("stage")),
        "entryPoint": _project_text(
            value.get("entryPoint"), "inventoryWorkspace.entryPoint", 512,
            allow_empty=True),
        "rawSha256": _project_sha256(
            value.get("rawSha256"), "inventoryWorkspace.rawSha256"),
        "codeSha256": _project_sha256(
            value.get("codeSha256"), "inventoryWorkspace.codeSha256", True),
        "layoutHash": _project_sha256(
            value.get("layoutHash"), "inventoryWorkspace.layoutHash", True),
        "interfaceHash": _project_sha256(
            value.get("interfaceHash"), "inventoryWorkspace.interfaceHash", True),
        "interfaceComplete": value.get("interfaceComplete") is True,
        "baselineAbiSha256": _project_sha256(
            value.get("baselineAbiSha256"),
            "inventoryWorkspace.baselineAbiSha256", True),
    }
    identity = {
        "schema": "renderdoc-ultra-project-shader-artifact-identity/v1",
        "schemaVersion": 1,
        "stableShader": origin,
        "physicalIdentity": physical,
        "targetProfile": dict(profile),
        "sourceSha256": _project_sha256(
            value.get("sourceSha256"), "inventoryWorkspace.sourceSha256"),
    }
    source_format = _project_workspace_source_format(value)
    if source_format != "hlsl":
        identity["sourceFormat"] = source_format
    if (physical["stage"] != origin["stage"] or
            physical["entryPoint"] != origin["entryPoint"] or
            physical["rawSha256"] != origin["rawSha256"] or
            physical["layoutHash"] != origin["layoutHash"] or
            physical["baselineAbiSha256"] != origin["abiHash"]):
        raise ShaderConsoleError(
            "project inventory workspace physical identity differs from its "
            "stable Shader")
    compiler = value.get("compilerBinding")
    if compiler is not None:
        clean_compiler = _project_compiler_binding_identity(compiler)
        if clean_compiler["targetProfile"] != identity["targetProfile"]:
            raise ShaderConsoleError(
                "project inventory compiler binding target profile differs")
        identity["compilerBinding"] = clean_compiler
    disassembly = value.get("assemblyDisassemblyArtifacts", [])
    if not isinstance(disassembly, list) or len(disassembly) > 2:
        raise ShaderConsoleError(
            "project inventory assembly/disassembly artifacts are invalid")
    if disassembly:
        clean_disassembly = [
            _project_assembly_disassembly_identity(item)
            for item in disassembly
        ]
        roles = [item["role"] for item in clean_disassembly]
        if len(set(roles)) != len(roles):
            raise ShaderConsoleError(
                "project inventory disassembly role is duplicated")
        if any(
                item["kind"] == "native-disassembly" and
                (item["role"] != "baseline" or
                 item["inputRawSha256"] != physical["rawSha256"])
                for item in clean_disassembly):
            raise ShaderConsoleError(
                "project inventory native disassembly physical binding mismatch")
        identity["assemblyDisassemblyArtifacts"] = clean_disassembly
    compiled = value.get("compiledCandidate")
    if compiled is not None:
        clean_compiled = _project_compiled_candidate_identity(compiled)
        if (clean_compiled["sourceSha256"] != identity["sourceSha256"] or
                clean_compiled["targetProfile"] != identity["targetProfile"] or
                clean_compiled["originBaselineAbiSha256"] !=
                physical["baselineAbiSha256"]):
            raise ShaderConsoleError(
                "project inventory compiled candidate binding mismatch")
        identity["compiledArtifact"] = {
            "schema": clean_compiled["schema"],
            "schemaVersion": clean_compiled["schemaVersion"],
            "rawSha256": clean_compiled["rawSha256"],
            "codeSha256": clean_compiled["codeSha256"],
            "byteSize": clean_compiled["byteSize"],
            "kind": clean_compiled["kind"],
            "sourceSha256": clean_compiled["sourceSha256"],
            "targetProfile": clean_compiled["targetProfile"],
            "originBaselineAbiSha256": clean_compiled[
                "originBaselineAbiSha256"],
            "validationScope": clean_compiled["validationScope"],
            "semanticEquivalenceVerified": clean_compiled[
                "semanticEquivalenceVerified"],
        }
        if clean_compiled["validationScope"] == "manual-edit-unvalidated":
            identity["compiledArtifact"]["manualApplyReceipt"] = clean_compiled["manualApplyReceipt"]
        else:
            identity["compiledArtifact"]["originCandidateAbiSha256"] = clean_compiled["originCandidateAbiSha256"]
    return identity


def _project_capture_revision(captures, capture_id, catalog_sha256):
    matches = [item for item in captures if (
        isinstance(item, dict) and item.get("captureId") == capture_id)]
    if len(matches) != 1:
        raise ShaderConsoleError("project workspace capture provenance is unavailable")
    capture = matches[0]
    revisions = capture.get("catalogRevisions")
    if revisions is None:
        if capture.get("catalogSha256") != catalog_sha256:
            raise ShaderConsoleError("project workspace catalog provenance is unavailable")
        revision = capture
    else:
        if not isinstance(revisions, list) or not revisions:
            raise ShaderConsoleError("project capture revisions are invalid")
        selected = [item for item in revisions if (
            isinstance(item, dict) and item.get("catalogSha256") == catalog_sha256)]
        if len(selected) != 1:
            raise ShaderConsoleError("project workspace catalog provenance is unavailable")
        revision = selected[0]
    shaders = revision.get("shaders")
    if not isinstance(shaders, list) or len(shaders) > 65536:
        raise ShaderConsoleError("project capture shaders are invalid")
    return shaders


def _validate_project_workspace_record(root, captures, record, activation_revision):
    required = {
        "workspaceId", "captureId", "originCatalogSha256", "stage",
        "entryPoint", "rawSha256", "codeSha256", "layoutHash",
        "interfaceHash", "interfaceComplete", "baselineAbiSha256",
        "targetProfile", "sourceSha256", "analysisSha256", "knowledgeId",
        "sourcePath", "sourceOriginPath", "workspacePath",
        "workspaceManifestSha256", "registeredAt",
    }
    optional = {
        "originResourceId", "compiledCandidate", "analysisPath",
        "analysisKind", "analysisPolicy", "compilerBinding",
        "assemblyDisassemblyArtifacts", "inventoryOrigin",
        "inventoryObservations", "publicationMode",
        "sourceFormat",
    }
    if (not isinstance(record, dict) or not required.issubset(set(record)) or
            set(record) - required - optional):
        raise ShaderConsoleError("project workspace knowledge fields are invalid")
    knowledge_id = _project_token(record.get("knowledgeId"),
                                  "project workspace knowledgeId")
    clean = {
        "workspaceId": _project_token(record.get("workspaceId"),
                                      "project source workspaceId"),
        "captureId": _project_sha256(record.get("captureId"),
                                     "project source captureId"),
        "originCatalogSha256": _project_sha256(
            record.get("originCatalogSha256"), "project source originCatalogSha256"),
        "stage": _project_text(record.get("stage"), "project source stage", 64,
                               single_line=True).lower(),
        "entryPoint": _project_text(
            record.get("entryPoint"), "project source entryPoint", 512,
            allow_empty=True, single_line=True),
        "rawSha256": _project_sha256(record.get("rawSha256"),
                                     "project source rawSha256"),
        "codeSha256": _project_sha256(record.get("codeSha256"),
                                      "project source codeSha256", True),
        "layoutHash": _project_sha256(record.get("layoutHash"),
                                      "project source layoutHash", True),
        "interfaceHash": _project_sha256(record.get("interfaceHash"),
                                         "project source interfaceHash", True),
        "baselineAbiSha256": _project_sha256(
            record.get("baselineAbiSha256"), "project source baselineAbiSha256", True),
        "sourceSha256": _project_sha256(record.get("sourceSha256"),
                                        "project source sourceSha256"),
        "analysisSha256": _project_sha256(record.get("analysisSha256"),
                                          "project source analysisSha256", True),
    }
    source_format = _project_workspace_source_format(record)
    if source_format != "hlsl":
        clean["sourceFormat"] = source_format
    origin_resource_id_persisted = "originResourceId" in record
    if origin_resource_id_persisted:
        clean["originResourceId"] = _project_positive(
            record.get("originResourceId"),
            "project source originResourceId")
    if not isinstance(record.get("interfaceComplete"), bool):
        raise ShaderConsoleError("project source interfaceComplete is invalid")
    clean["interfaceComplete"] = record["interfaceComplete"]
    profile = record.get("targetProfile")
    if not isinstance(profile, dict):
        raise ShaderConsoleError("project source targetProfile is invalid")
    clean["targetProfile"] = dict(profile)

    workspace_manifest_sha256 = _project_sha256(
        record.get("workspaceManifestSha256"),
        "project source workspaceManifestSha256")
    compiler_identity = None
    if "compilerBinding" in record:
        compiler_identity = _project_compiler_binding_identity(
            record.get("compilerBinding"))
        if compiler_identity["targetProfile"] != clean["targetProfile"]:
            raise ShaderConsoleError(
                "project compiler binding target profile differs")

    raw_disassembly = record.get("assemblyDisassemblyArtifacts", [])
    if (not isinstance(raw_disassembly, list) or len(raw_disassembly) > 2 or
            ("assemblyDisassemblyArtifacts" in record and
             not raw_disassembly)):
        raise ShaderConsoleError(
            "project assembly/disassembly artifacts are invalid")
    disassembly_identities = []
    disassembly_artifact_paths = []
    disassembly_roles = set()
    for artifact in raw_disassembly:
        artifact_identity, artifact_path = (
            _validate_project_disassembly_artifact(root, artifact))
        if artifact_identity["role"] in disassembly_roles:
            raise ShaderConsoleError("project disassembly role is duplicated")
        if (artifact_identity["kind"] == "native-disassembly" and
                (artifact_identity["role"] != "baseline" or
                 artifact_identity["inputRawSha256"] != clean["rawSha256"])):
            raise ShaderConsoleError(
                "project native disassembly physical binding mismatch")
        disassembly_roles.add(artifact_identity["role"])
        disassembly_identities.append(artifact_identity)
        disassembly_artifact_paths.append(artifact_path)

    compiled = record.get("compiledCandidate")
    compiled_identity = None
    compiled_artifact_path = ""
    if compiled is not None:
        compiled_identity, compiled_artifact_path = (
            _validate_project_compiled_candidate(root, compiled, clean))

    inventory_origin = None
    inventory_observations = []
    if "inventoryOrigin" in record:
        if origin_resource_id_persisted:
            raise ShaderConsoleError(
                "project inventory workspace must not claim originResourceId")
        inventory_origin = _project_inventory_origin_identity(
            record.get("inventoryOrigin"))
        if (inventory_origin["rawSha256"] != clean["rawSha256"] or
                inventory_origin["stage"] != clean["stage"] or
                inventory_origin["entryPoint"] != clean["entryPoint"] or
                inventory_origin["abiHash"] != clean["baselineAbiSha256"] or
                inventory_origin["layoutHash"] != clean["layoutHash"]):
            raise ShaderConsoleError(
                "project inventory origin differs from project knowledge")
        raw_observations = record.get("inventoryObservations")
        if (not isinstance(raw_observations, list) or
                not raw_observations or
                len(raw_observations) > MAX_PROJECT_INVENTORY_OBSERVATIONS):
            raise ShaderConsoleError(
                "project inventory observations are invalid or oversized")
        seen_receipts = set()
        expected_compiled_sha256 = (
            compiled_identity["rawSha256"]
            if compiled_identity is not None else "")
        for raw_observation in raw_observations:
            observation = _project_inventory_observation(raw_observation)
            if observation["receiptSha256"] in seen_receipts:
                raise ShaderConsoleError(
                    "project inventory observation receipt is duplicated")
            if (observation["stableKeySha256"] !=
                    inventory_origin["stableKeySha256"] or
                    observation["sourceSha256"] != clean["sourceSha256"] or
                    observation["compiledCandidateSha256"] !=
                    expected_compiled_sha256):
                raise ShaderConsoleError(
                    "project inventory observation differs from its artifact")
            seen_receipts.add(observation["receiptSha256"])
            inventory_observations.append(observation)
        first_observation = inventory_observations[0]
        if (first_observation["captureId"] != clean["captureId"] or
                first_observation["inventorySha256"] !=
                clean["originCatalogSha256"] or
                first_observation["workspaceManifestSha256"] !=
                workspace_manifest_sha256):
            raise ShaderConsoleError(
                "project first inventory observation differs from publication "
                "provenance")
    elif "inventoryObservations" in record:
        raise ShaderConsoleError(
            "project inventory observations have no inventory origin")

    publication_mode = record.get("publicationMode")
    if publication_mode is not None:
        if publication_mode != "source-only-draft" or compiled_identity is not None:
            raise ShaderConsoleError("project source-only publication mode is invalid")
        clean["publicationMode"] = publication_mode
    identity = dict(clean)
    if inventory_origin is not None:
        identity["inventoryOrigin"] = inventory_origin
    if compiler_identity is not None:
        identity["compilerBinding"] = compiler_identity
    if disassembly_identities:
        identity["assemblyDisassemblyArtifacts"] = disassembly_identities
    if compiled_identity is not None:
        identity["compiledCandidate"] = compiled_identity
    expected_identity = (
        _project_inventory_workspace_artifact_identity(identity)
        if inventory_origin is not None else identity)
    if _project_canonical_sha256(expected_identity)[:32] != knowledge_id:
        raise ShaderConsoleError("project workspace knowledgeId is invalid")
    expected_relative = "knowledge/workspaces/%s.%s" % (knowledge_id, source_format)
    source_path = _project_relative_path(
        root, record.get("sourcePath"), "project Shader source path", expected_relative)
    _regular_file_in_tree(root, source_path, MAX_SOURCE_BYTES, "project Shader source")
    raw = _read_file_bytes(source_path, MAX_SOURCE_BYTES, "project Shader source")
    if not raw or hashlib.sha256(raw).hexdigest() != clean["sourceSha256"]:
        raise ShaderConsoleError("project Shader source hash mismatch")
    try:
        source_text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ShaderConsoleError(
            "project Shader source is not valid UTF-8: %s" % _bounded(exc, 512))
    if "\x00" in source_text:
        raise ShaderConsoleError("project Shader source contains NUL characters")
    _project_text(record.get("sourceOriginPath"), "project source origin path", 4096)
    _project_text(record.get("workspacePath"), "project source workspace path", 4096)
    registered_at = _project_text(
        record.get("registeredAt"), "project source registration time", 128)
    if clean["analysisSha256"]:
        if {"analysisPath", "analysisKind", "analysisPolicy"} - set(record):
            raise ShaderConsoleError("project source analysis provenance is incomplete")
        expected_analysis = "knowledge/workspaces/%s.analysis.md" % clean[
            "analysisSha256"]
        analysis_path = _project_relative_path(
            root, record.get("analysisPath"), "project source analysis path",
            expected_analysis)
        if (record.get("analysisKind") != "ai-explanatory-text" or
                record.get("analysisPolicy") != {
                    "physicalEvidence": False, "applyAuthorization": False,
                    "reviewRequired": True}):
            raise ShaderConsoleError("project source analysis policy is invalid")
        _regular_file_in_tree(
            root, analysis_path, MAX_SOURCE_BYTES, "project source analysis")
        analysis_raw = _read_file_bytes(
            analysis_path, MAX_SOURCE_BYTES, "project source analysis")
        if hashlib.sha256(analysis_raw).hexdigest() != clean["analysisSha256"]:
            raise ShaderConsoleError("project source analysis hash mismatch")
    elif set(record) & {"analysisPath", "analysisKind", "analysisPolicy"}:
        raise ShaderConsoleError("project source has unbound analysis provenance")

    if inventory_origin is not None:
        selected_resource_id = inventory_observations[0]["targetResourceId"]
    else:
        shaders = _project_capture_revision(
            captures, clean["captureId"], clean["originCatalogSha256"])
        matches = []
        for shader in shaders:
            if (not isinstance(shader, dict) or
                    shader.get("stage") != clean["stage"] or
                    str(shader.get("entryPoint", "") or "") !=
                    clean["entryPoint"] or
                    shader.get("rawSha256") != clean["rawSha256"]):
                continue
            candidate_resource_id = _project_positive(
                shader.get("resourceId"), "project capture Shader resourceId")
            if (not origin_resource_id_persisted or
                    candidate_resource_id == clean["originResourceId"]):
                matches.append((shader, candidate_resource_id))
        if len(matches) != 1:
            raise ShaderConsoleError(
                "project source does not uniquely select one imported Shader")
        shader, selected_resource_id = matches[0]
        # Old native-format publications used the exact raw hash as their
        # optional code fingerprint. Match Host projects.py without accepting
        # arbitrary missing fingerprints: raw/stage/entry/resource already
        # selected this one imported Shader above.
        catalog_code_sha256 = str(shader.get("codeSha256", "") or "")
        legacy_raw_code_fallback = (
            not catalog_code_sha256 and clean["codeSha256"] == clean["rawSha256"])
        if ((catalog_code_sha256 != clean["codeSha256"] and not legacy_raw_code_fallback) or
                shader.get("layoutHash", "") != clean["layoutHash"] or
                shader.get("interfaceHash", "") != clean["interfaceHash"] or
                (shader.get("interfaceComplete") is True) !=
                clean["interfaceComplete"]):
            raise ShaderConsoleError(
                "project source physical identity is inconsistent")
    try:
        info = os.lstat(source_path)
    except OSError as exc:
        raise ShaderConsoleError(
            "project Shader source metadata is unavailable: %s" % _bounded(exc, 512))
    if (not stat.S_ISREG(info.st_mode) or _is_reparse(source_path) or
            int(info.st_size) != len(raw)):
        raise ShaderConsoleError("project Shader source changed while it was read")
    document_id = _project_canonical_sha256({
        "projectWorkspaceId": activation_revision["projectWorkspaceId"],
        "projectId": activation_revision["projectId"],
        "knowledgeId": knowledge_id,
    })[:32]
    return {
        "documentId": document_id,
        "documentKind": "project-library",
        "readOnly": True,
        "projectWorkspaceId": activation_revision["projectWorkspaceId"],
        "projectId": activation_revision["projectId"],
        "knowledgeId": knowledge_id,
        "activationRevision": activation_revision["activationRevision"],
        "sourcePath": source_path,
        "sourceRelativePath": expected_relative,
        "sourceFormat": source_format,
        "fileName": os.path.basename(source_path),
        "stage": clean["stage"],
        "entryPoint": clean["entryPoint"],
        "captureId": clean["captureId"],
        "originResourceId": selected_resource_id,
        "originResourceIdPersisted": origin_resource_id_persisted,
        "inventoryBacked": inventory_origin is not None,
        "inventoryOrigin": (
            dict(inventory_origin) if inventory_origin is not None else None),
        "inventoryObservationCount": len(inventory_observations),
        "rawShaderSha256": clean["rawSha256"],
        "sourceSha256": clean["sourceSha256"],
        "originWorkspaceId": clean["workspaceId"],
        "baselineAbiSha256": clean["baselineAbiSha256"],
        "targetProfile": clean["targetProfile"],
        "analysisSha256": clean["analysisSha256"],
        "compilerBinding": (
            dict(compiler_identity) if compiler_identity is not None else None),
        "assemblyDisassemblyArtifacts": [
            dict(item) for item in disassembly_identities],
        "assemblyDisassemblyArtifactPaths": disassembly_artifact_paths,
        "compiledCandidate": (
            dict(compiled_identity) if compiled_identity is not None else None),
        "compiledCandidateAvailable": compiled_identity is not None,
        "compiledCandidateValidationScope": (
            compiled_identity["validationScope"]
            if compiled_identity is not None else ""),
        "compiledCandidateArtifactPath": compiled_artifact_path,
        "registeredAt": registered_at,
        "size": len(raw),
        "modifiedNs": getattr(info, "st_mtime_ns", int(info.st_mtime * 1000000000)),
    }


def _validate_project_shader_flow_graphs_strict(binding, documents):
    """Load content-addressed project graphs and rebind navigation targets."""

    root = binding["projectRoot"]
    index_path = _project_relative_path(
        root, ACTIVE_PROJECT_LAYOUT["sharedIndex"],
        "shared dictionary index path",
        ACTIVE_PROJECT_LAYOUT["sharedIndex"])
    index, index_file_sha256 = _read_project_json(
        index_path, MAX_REGISTRY_BYTES, "shared dictionary index", root)
    if set(index) != {
            "schema", "schemaVersion", "workspaceId", "explanatory",
            "updatedAt", "indexSha256"}:
        raise ShaderConsoleError("shared dictionary index fields are invalid")
    if (index.get("schema") != SHARED_DICTIONARY_SCHEMA or
            index.get("schemaVersion") != SHARED_DICTIONARY_VERSION or
            _project_token(index.get("workspaceId"),
                           "shared dictionary workspaceId") !=
            binding["workspaceId"]):
        raise ShaderConsoleError("shared dictionary identity is invalid")
    _verify_project_seal(index, "indexSha256", "shared dictionary index")
    _project_text(index.get("updatedAt"), "shared dictionary update time", 128,
                  single_line=True)
    records = index.get("explanatory")
    if (not isinstance(records, list) or
            len(records) > MAX_PROJECT_EXPLANATORY_RECORDS):
        raise ShaderConsoleError(
            "shared explanatory index is invalid or oversized")

    documents_by_knowledge = dict(
        (str(document.get("knowledgeId", "")), document)
        for document in documents)
    selected = {}
    invalid = {}
    watched_paths = [index_path]
    identities = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {
                "knowledgeId", "kind", "artifactPath", "artifactSha256",
                "registeredAt", "policy"}:
            raise ShaderConsoleError("shared explanatory record is invalid")
        knowledge_id = _project_token(
            record.get("knowledgeId"), "shared explanatory knowledgeId")
        kind = _project_text(
            record.get("kind"), "shared explanatory kind", 64,
            single_line=True)
        if kind not in ("mermaid-flowchart", "shader-flow-graph"):
            raise ShaderConsoleError("unsupported shared explanatory kind")
        artifact_sha256 = _project_sha256(
            record.get("artifactSha256"),
            "shared explanatory artifactSha256")
        identity = (knowledge_id, kind, artifact_sha256)
        if identity in identities:
            raise ShaderConsoleError("shared explanatory record is duplicated")
        identities.add(identity)
        if record.get("policy") != {
                "explanatoryOnly": True,
                "physicalEvidence": False,
                "applyAuthorization": False,
                "aiProvisionalAutoApply": False}:
            raise ShaderConsoleError("shared explanatory policy is invalid")
        _project_text(
            record.get("registeredAt"),
            "shared explanatory registration time", 128,
            single_line=True)
        if kind != "shader-flow-graph":
            continue
        document = documents_by_knowledge.get(knowledge_id)
        if document is None:
            continue
        artifact_path = ""
        try:
            expected_relative = "%s/%s.flow-graph.json" % (
                ACTIVE_PROJECT_LAYOUT["sharedExplanatory"],
                artifact_sha256)
            artifact_path = _project_relative_path(
                root, record.get("artifactPath"),
                "shared explanatory artifact path", expected_relative)
            watched_paths.append(artifact_path)
            artifact_bytes = _read_file_bytes(
                artifact_path, MAX_SHADER_FLOW_GRAPH_BYTES,
                "shared explanatory artifact")
            if hashlib.sha256(artifact_bytes).hexdigest() != artifact_sha256:
                raise ShaderConsoleError(
                    "shared explanatory artifact hash mismatch")
            graph = json.loads(artifact_bytes.decode("utf-8", errors="strict"))
            graph = _validate_shader_flow_graph_value(
                graph,
                expected_source_sha256=str(
                    document.get("sourceSha256", "")),
                expected_stage=str(document.get("stage", "")),
                expected_entry_point=document.get("entryPoint"),
            )
        except (ShaderConsoleError, OSError, UnicodeError,
                ValueError, TypeError) as exc:
            selected.pop(knowledge_id, None)
            invalid[knowledge_id] = {
                "available": False,
                "status": "invalid",
                "artifactPath": artifact_path,
                "error": _bounded(exc, 1024),
            }
            continue

        # The published graph retains its original graphId/provenance, while
        # every click target is projected onto this exact immutable document.
        projected_nodes = []
        for original in graph["nodes"]:
            node = dict(original)
            location = dict(original["source"])
            location.update({
                "workspaceId": document["documentId"],
                "documentId": document["documentId"],
                "path": document["sourceRelativePath"],
            })
            node["source"] = location
            projected_nodes.append(node)
        projection = dict(graph)
        projection.update({
            "nodes": projected_nodes,
            "available": True,
            "status": "current",
            "stale": False,
            "artifactPath": artifact_path,
            "artifactSha256": artifact_sha256,
            "navigationDocumentId": document["documentId"],
        })
        selected[knowledge_id] = projection
        invalid.pop(knowledge_id, None)

    for document in documents:
        knowledge_id = str(document.get("knowledgeId", ""))
        graph = selected.get(knowledge_id)
        document["flowGraph"] = graph
        document["flowGraphStatus"] = (
            {
                "available": True,
                "status": "current",
                "artifactPath": str(graph.get("artifactPath", "")),
            }
            if graph is not None else
            invalid.get(knowledge_id, {
                "available": False,
                "status": "unavailable",
                "artifactPath": "",
            }))
    return {
        "indexPath": index_path,
        "indexFileSha256": index_file_sha256,
        "watchedPaths": watched_paths,
    }


def _validate_project_shader_flow_graphs(binding, documents):
    """Keep optional navigation metadata from denying reviewed HLSL access."""

    try:
        return _validate_project_shader_flow_graphs_strict(binding, documents)
    except (ShaderConsoleError, OSError) as exc:
        error = _bounded(exc, 1024)
        for document in documents:
            document["flowGraph"] = None
            document["flowGraphStatus"] = {
                "available": False,
                "status": "invalid",
                "artifactPath": "",
                "error": error,
            }
        # The fixed index path remains useful to the file watcher, but an
        # invalid index never becomes a consistency authority for HLSL.
        index_path = os.path.join(
            binding["projectRoot"],
            *ACTIVE_PROJECT_LAYOUT["sharedIndex"].split("/"))
        watched = []
        if (os.path.isfile(index_path) and not _is_reparse(index_path) and
                _within(binding["projectRoot"], index_path)):
            watched.append(index_path)
        return {
            "indexPath": "",
            "indexFileSha256": "",
            "watchedPaths": watched,
            "error": error,
        }


def _validate_replay_project(binding, workspace):
    project_root = _project_relative_path(
        binding["projectRoot"], ACTIVE_PROJECT_LAYOUT["replayProject"],
        "replay project root")
    project_root = _directory(project_root, "replay project root")
    manifest_path = os.path.join(project_root, "project.json")
    manifest, manifest_file_sha256 = _read_project_json(
        manifest_path, MAX_PROJECT_MANIFEST_BYTES, "replay project manifest",
        project_root)
    expected = {
        "schema", "schemaVersion", "projectId", "name", "scope",
        "createdAt", "updatedAt", "captures", "knowledge", "activeKnowledge",
        "activeHeads",
    }
    legacy_active_expected = set(expected) - {"activeHeads"}
    legacy_expected = legacy_active_expected - {"activeKnowledge"}
    if set(manifest) not in (
            expected, legacy_active_expected, legacy_expected):
        raise ShaderConsoleError("replay project manifest fields are invalid")
    if (manifest.get("schema") != REPLAY_PROJECT_SCHEMA or
            manifest.get("schemaVersion") != 1):
        raise ShaderConsoleError("replay project manifest schema is invalid")
    project_id = _project_token(manifest.get("projectId"), "replay projectId")
    project_name = _project_text(
        manifest.get("name"), "replay project name", 256,
        allow_empty=True, single_line=True)
    scope = manifest.get("scope")
    if not isinstance(scope, dict) or set(scope) != {
            "gameId", "buildId", "platform", "apiFamily"}:
        raise ShaderConsoleError("replay project scope is invalid")
    _project_text(scope.get("gameId"), "replay scope.gameId", 256,
                  single_line=True)
    _project_text(scope.get("buildId"), "replay scope.buildId", 256,
                  single_line=True)
    _project_text(scope.get("platform"), "replay scope.platform", 128,
                  allow_empty=True, single_line=True)
    _project_api_family(scope.get("apiFamily"), "replay scope.apiFamily", True)
    captures = manifest.get("captures")
    if not isinstance(captures, list) or len(captures) > MAX_PROJECT_CAPTURES:
        raise ShaderConsoleError("replay project captures are invalid")
    capture_ids = set()
    for capture in captures:
        _check_console_read_cancelled()
        if not isinstance(capture, dict):
            raise ShaderConsoleError("replay project capture record is invalid")
        capture_id = _project_sha256(
            capture.get("captureId"), "replay project captureId")
        if capture_id in capture_ids:
            raise ShaderConsoleError("replay project contains duplicate captures")
        capture_ids.add(capture_id)
    knowledge = manifest.get("knowledge")
    if not isinstance(knowledge, dict) or set(knowledge) != {
            "workspaces", "aliases", "materials"}:
        raise ShaderConsoleError("replay project knowledge index is invalid")
    for kind in ("workspaces", "aliases", "materials"):
        records = knowledge.get(kind)
        if not isinstance(records, list) or len(records) > MAX_PROJECT_KNOWLEDGE:
            raise ShaderConsoleError("replay project %s knowledge is invalid" % kind)
    workspace_ids = []
    for record in knowledge["workspaces"]:
        _check_console_read_cancelled()
        if not isinstance(record, dict):
            raise ShaderConsoleError("project workspace knowledge record is invalid")
        workspace_ids.append(_project_token(
            record.get("knowledgeId"), "project workspace knowledgeId"))
    if len(set(workspace_ids)) != len(workspace_ids):
        raise ShaderConsoleError("project workspace knowledgeId is duplicated")
    material_ids = []
    for record in knowledge["materials"]:
        _check_console_read_cancelled()
        if not isinstance(record, dict):
            raise ShaderConsoleError("project material knowledge record is invalid")
        material_ids.append(_project_token(
            record.get("knowledgeId"), "project material knowledgeId"))
    if len(set(material_ids)) != len(material_ids):
        raise ShaderConsoleError("project material knowledgeId is duplicated")
    alias_ids = []
    for record in knowledge["aliases"]:
        _check_console_read_cancelled()
        if not isinstance(record, dict):
            raise ShaderConsoleError("project alias knowledge record is invalid")
        alias_ids.append(_project_token(
            record.get("knowledgeId"), "project alias knowledgeId"))
    if len(set(alias_ids)) != len(alias_ids):
        raise ShaderConsoleError("project alias knowledgeId is duplicated")
    active = manifest.get("activeKnowledge")
    if active is None:
        active = {"revision": 0, "workspaces": {}, "materials": {}}
        for kind, ids in (("workspaces", workspace_ids),
                          ("materials", material_ids)):
            for token in ids:
                active["revision"] += 1
                active[kind][token] = active["revision"]
    if not isinstance(active, dict) or set(active) != {
            "revision", "workspaces", "materials"}:
        raise ShaderConsoleError("replay project activeKnowledge is invalid")
    revision = _project_nonnegative(active.get("revision"),
                                    "activeKnowledge.revision")
    serials = set()
    for kind, ids in (("workspaces", workspace_ids), ("materials", material_ids)):
        activations = active.get(kind)
        if not isinstance(activations, dict) or set(activations) != set(ids):
            raise ShaderConsoleError("replay project activeKnowledge is incomplete")
        for token, serial in activations.items():
            _project_token(token, "active knowledgeId")
            serial = _project_positive(serial, "active knowledge revision")
            if serial > revision or serial in serials:
                raise ShaderConsoleError("active knowledge revision is invalid")
            serials.add(serial)
    if revision != (max(serials) if serials else 0):
        raise ShaderConsoleError("active knowledge revision is inconsistent")
    active_heads = manifest.get("activeHeads")
    if active_heads is not None:
        if (not isinstance(active_heads, dict) or
                set(active_heads) != {"workspaces", "materials", "aliases"}):
            raise ShaderConsoleError("replay project activeHeads is invalid")
        known_ids = {
            "workspaces": set(workspace_ids),
            "materials": set(material_ids),
            "aliases": set(alias_ids),
        }
        for kind in ("workspaces", "materials", "aliases"):
            heads = active_heads.get(kind)
            if (not isinstance(heads, dict) or
                    len(heads) > (MAX_PROJECT_ACTIVE_HEADS
                                  if kind == "aliases"
                                  else MAX_PROJECT_KNOWLEDGE)):
                raise ShaderConsoleError(
                    "replay project activeHeads is invalid")
            if known_ids[kind] and not heads:
                raise ShaderConsoleError(
                    "replay project activeHeads is incomplete")
            for head_key, head in heads.items():
                _project_sha256(head_key, "project active head key")
                if (not isinstance(head, dict) or set(head) != {
                        "generation", "knowledgeId", "publishedAt"}):
                    raise ShaderConsoleError(
                        "replay project active head is invalid")
                _project_positive(
                    head.get("generation"), "project active head generation")
                knowledge_id = _project_token(
                    head.get("knowledgeId"),
                    "project active head knowledgeId")
                if knowledge_id not in known_ids[kind]:
                    raise ShaderConsoleError(
                        "replay project active head is absent")
                _project_text(
                    head.get("publishedAt"),
                    "project active head publishedAt", 128,
                    single_line=True)
    documents = []
    document_ids = set()
    for record in knowledge["workspaces"]:
        _check_console_read_cancelled()
        knowledge_id = record["knowledgeId"]
        document = _validate_project_workspace_record(
            project_root, captures, record, {
                "projectWorkspaceId": binding["workspaceId"],
                "projectId": project_id,
                "activationRevision": active["workspaces"][knowledge_id],
            })
        if document["documentId"] in document_ids:
            raise ShaderConsoleError("project Shader document identity is duplicated")
        document_ids.add(document["documentId"])
        document["isCurrentHead"] = (
            knowledge_id in {head["knowledgeId"] for head in active_heads["workspaces"].values()}
            if active_heads is not None else True)
        documents.append(document)
    documents.sort(key=lambda item: (
        item.get("activationRevision", 0), item.get("registeredAt", ""),
        item.get("knowledgeId", "")), reverse=True)
    graph_index = _validate_project_shader_flow_graphs(binding, documents)
    return {
        "projectRoot": project_root,
        "manifestPath": manifest_path,
        "manifestFileSha256": manifest_file_sha256,
        "projectId": project_id,
        "projectName": project_name,
        "documents": documents,
        "sharedIndexPath": graph_index["indexPath"],
        "sharedIndexFileSha256": graph_index["indexFileSha256"],
        "flowGraphWatchedPaths": graph_index["watchedPaths"],
    }


def _require_project_library_binding_unchanged(binding, workspace, replay):
    current_selection = _validate_active_project_selection(binding["path"])
    if any((
            current_selection["fileSha256"] != binding["fileSha256"],
            current_selection["selectionSha256"] != binding["selectionSha256"],
            current_selection["workspaceId"] != binding["workspaceId"],
            not _same_path(current_selection["projectRoot"], binding["projectRoot"]),
            current_selection["registry"]["fileSha256"] !=
            binding["registry"]["fileSha256"],
            current_selection["registry"]["registrySha256"] !=
            binding["registry"]["registrySha256"],
    )):
        raise ShaderConsoleError("active project changed while its Shader library was read")
    current_workspace = _validate_active_project_workspace(current_selection)
    if (current_workspace["fileSha256"] != workspace["fileSha256"] or
            current_workspace["configSha256"] != workspace["configSha256"]):
        raise ShaderConsoleError("project workspace changed while its Shader library was read")
    _unused, manifest_file_sha256 = _read_project_json(
        replay["manifestPath"], MAX_PROJECT_MANIFEST_BYTES,
        "replay project manifest", replay["projectRoot"])
    if manifest_file_sha256 != replay["manifestFileSha256"]:
        raise ShaderConsoleError("replay project changed while its Shader library was read")
    if replay.get("sharedIndexPath"):
        _unused, shared_index_file_sha256 = _read_project_json(
            replay["sharedIndexPath"], MAX_REGISTRY_BYTES,
            "shared dictionary index", binding["projectRoot"])
        if shared_index_file_sha256 != replay.get("sharedIndexFileSha256"):
            raise ShaderConsoleError(
                "shared dictionary changed while its Shader library was read")


def _load_project_shader_library(selection_path):
    binding = _validate_active_project_selection(selection_path)
    workspace = _validate_active_project_workspace(binding)
    result = {
        "schema": PROJECT_SHADER_LIBRARY_SCHEMA,
        "schemaVersion": 1,
        "selectionPath": binding["path"],
        "projectWorkspaceId": binding["workspaceId"],
        "projectRoot": binding["projectRoot"],
        "workspaceName": workspace["name"],
        "state": workspace["state"],
        "scope": workspace["scope"],
        "projectId": "",
        "projectName": "",
        "available": False,
        "documents": [],
        "errors": [],
        "watchedPaths": [
            # The selection can live in the hot IPC instance directory. Watch
            # its exact file (also polled for recreation), not IPC heartbeats.
            binding["path"],
            binding["registry"]["path"], workspace["path"],
            binding["projectRoot"],
        ],
    }
    if workspace["state"] != "bound":
        _require_project_library_binding_unchanged(binding, workspace, {
            "manifestPath": workspace["path"],
            "projectRoot": binding["projectRoot"],
            "manifestFileSha256": workspace["fileSha256"],
        })
        return result
    replay = _validate_replay_project(binding, workspace)
    _require_project_library_binding_unchanged(binding, workspace, replay)
    result.update({
        "projectId": replay["projectId"],
        "projectName": replay["projectName"],
        "available": True,
        "documents": replay["documents"],
    })
    result["watchedPaths"].extend([
        replay["projectRoot"], replay["manifestPath"],
        os.path.join(replay["projectRoot"], "knowledge", "workspaces"),
    ])
    result["watchedPaths"].extend(replay.get("flowGraphWatchedPaths", []))
    result["watchedPaths"].extend(
        item["sourcePath"] for item in replay["documents"])
    result["watchedPaths"].extend(
        item["compiledCandidateArtifactPath"]
        for item in replay["documents"]
        if item.get("compiledCandidateArtifactPath"))
    for document in replay["documents"]:
        result["watchedPaths"].extend(
            document.get("assemblyDisassemblyArtifactPaths", []))
    return result


def load_project_shader_library_snapshot(selection_path=None):
    """Project active-selection reader for immutable, shared HLSL knowledge."""
    path = os.path.abspath(str(
        selection_path or default_active_project_selection_path() or ""))
    result = {
        "schema": PROJECT_SHADER_LIBRARY_SCHEMA,
        "schemaVersion": 1,
        "selectionPath": path,
        "available": False,
        "projectWorkspaceId": "",
        "projectRoot": "",
        "workspaceName": "",
        "state": "",
        "scope": {},
        "projectId": "",
        "projectName": "",
        "documents": [],
        "errors": [],
        "watchedPaths": [path] if path else [],
    }
    if not path or not os.path.exists(path):
        parent = os.path.dirname(path) if path else ""
        if parent and os.path.isdir(parent) and not _is_reparse(parent):
            result["watchedPaths"].append(parent)
        return result
    try:
        result = _load_project_shader_library(path)
    except ShaderConsoleError as exc:
        result["errors"].append(_bounded(exc, 1024))
    unique = []
    known = set()
    for item in result.get("watchedPaths", []):
        key = _normal_path(item) if item else ""
        if key and key not in known and os.path.exists(item):
            known.add(key)
            unique.append(item)
    result["watchedPaths"] = unique
    return result


def load_project_shader_source_document(selection_path, project_workspace_id,
                                        project_id, knowledge_id,
                                        expected_source_sha256):
    """Read one immutable project HLSL document through the active selection."""
    path = os.path.abspath(str(
        selection_path or default_active_project_selection_path() or ""))
    requested_workspace = _project_token(
        project_workspace_id, "requested project workspaceId")
    requested_project = _project_token(project_id, "requested replay projectId")
    requested_knowledge = _project_token(
        knowledge_id, "requested project knowledgeId")
    expected_sha256 = _project_sha256(
        expected_source_sha256, "expected project sourceSha256")
    library = _load_project_shader_library(path)
    if (library.get("projectWorkspaceId") != requested_workspace or
            library.get("projectId") != requested_project):
        raise ShaderConsoleError("active project does not match the requested document")
    matches = [item for item in library.get("documents", []) if (
        item.get("knowledgeId") == requested_knowledge and
        item.get("sourceSha256") == expected_sha256)]
    if len(matches) != 1:
        raise ShaderConsoleError("project Shader document is unavailable or ambiguous")
    document = matches[0]
    raw = _read_file_bytes(
        document["sourcePath"], MAX_SOURCE_BYTES, "project HLSL source")
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    if not raw or raw_sha256 != expected_sha256:
        raise ShaderConsoleError("project HLSL source hash mismatch")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ShaderConsoleError(
            "project HLSL source is not valid UTF-8: %s" % _bounded(exc, 512))
    if "\x00" in text:
        raise ShaderConsoleError("project HLSL source contains NUL characters")
    # A second full protocol read after the source handle closes proves that
    # selection/config/manifest authority did not cross projects mid-open.
    confirmed = _load_project_shader_library(path)
    confirmed_matches = [item for item in confirmed.get("documents", []) if (
        item.get("knowledgeId") == requested_knowledge and
        item.get("sourceSha256") == raw_sha256)]
    if (confirmed.get("projectWorkspaceId") != requested_workspace or
            confirmed.get("projectId") != requested_project or
            len(confirmed_matches) != 1):
        raise ShaderConsoleError("active project changed while its HLSL was read")
    opened = dict(confirmed_matches[0])
    opened.update({
        "text": text,
        "rawSha256": raw_sha256,
        "size": len(raw),
    })
    return opened


def _validate_authorization(value):
    required_fields = {
        "authorizationId", "workspacePath", "workspaceIdentity",
        "workspaceIdentitySha256", "debounceMs", "authorizedAt",
        "updatedAt", "lastLiveEditId",
    }
    if not isinstance(value, dict) or set(value) != required_fields:
        raise ShaderConsoleError("live-edit authorization entry is invalid")
    authorization_id = str(value.get("authorizationId", "") or "").lower()
    live_edit_id = str(value.get("lastLiveEditId", "") or "").lower()
    if EXACT_TOKEN_RE.fullmatch(authorization_id) is None:
        raise ShaderConsoleError("live-edit authorization ID is invalid")
    if live_edit_id and EXACT_TOKEN_RE.fullmatch(live_edit_id) is None:
        raise ShaderConsoleError("live-edit session ID is invalid")
    debounce = value.get("debounceMs")
    if (isinstance(debounce, bool) or not isinstance(debounce, int) or
            debounce < 100 or debounce > 5000):
        raise ShaderConsoleError("live-edit authorization debounce is invalid")
    for field in ("authorizedAt", "updatedAt"):
        timestamp = value.get(field)
        if not isinstance(timestamp, str) or not timestamp or len(timestamp) > 128:
            raise ShaderConsoleError(
                "live-edit authorization timestamp is invalid")
    workspace_text = value.get("workspacePath")
    if not isinstance(workspace_text, str) or not os.path.isabs(workspace_text):
        raise ShaderConsoleError("authorized workspace path is invalid")
    workspace = _directory(workspace_text, "authorized workspace")
    identity = value.get("workspaceIdentity")
    identity_fields = {
        "workspaceId", "captureId", "capturePath", "shaderKey",
        "shaderRawSha256", "resourceId", "stage", "entryPoint",
    }
    if not isinstance(identity, dict) or set(identity) != identity_fields:
        raise ShaderConsoleError("authorized workspace identity is invalid")
    identity_sha256 = str(value.get("workspaceIdentitySha256", "") or "").lower()
    if (SHA256_RE.fullmatch(identity_sha256) is None or
            identity_sha256 != _canonical_sha256(identity)):
        raise ShaderConsoleError(
            "authorized workspace identity hash is invalid")
    workspace_id = str(identity.get("workspaceId", "") or "").lower()
    capture_id = str(identity.get("captureId", "") or "").lower()
    raw_sha256 = str(identity.get("shaderRawSha256", "") or "").lower()
    if EXACT_TOKEN_RE.fullmatch(workspace_id) is None:
        raise ShaderConsoleError("authorized workspace ID is invalid")
    if SHA256_RE.fullmatch(capture_id) is None or SHA256_RE.fullmatch(raw_sha256) is None:
        raise ShaderConsoleError("authorized capture/shader identity is invalid")
    capture_path = identity.get("capturePath")
    shader_key = identity.get("shaderKey")
    if (not isinstance(capture_path, str) or not capture_path or
            len(capture_path) > 32768 or not isinstance(shader_key, str) or
            not shader_key or len(shader_key) > 4096):
        raise ShaderConsoleError(
            "authorized capture/shader locator is invalid")
    resource_id = _positive_integer(identity.get("resourceId"), 0, 0x7FFFFFFFFFFFFFFF)
    if resource_id <= 0:
        raise ShaderConsoleError("authorized Shader ResourceId is invalid")
    stage = str(identity.get("stage", "") or "")
    entry_point = str(identity.get("entryPoint", "") or "")
    if (not stage or len(stage) > 64 or not entry_point or
            len(entry_point) > 512):
        raise ShaderConsoleError(
            "authorized Shader stage/entry point is invalid")
    return {
        "authorizationId": authorization_id,
        "lastLiveEditId": live_edit_id,
        "workspacePath": workspace,
        "workspaceId": workspace_id,
        "captureId": capture_id,
        "shaderRawSha256": raw_sha256,
        "stage": stage.lower(),
        "entryPoint": entry_point,
        "resourceId": resource_id,
        "capturePath": capture_path,
        "shaderKey": shader_key,
        "workspaceIdentitySha256": identity_sha256,
        "debounceMs": debounce,
        "authorizedAt": value["authorizedAt"],
        "updatedAt": value["updatedAt"],
    }


def _read_bounded_text(path, maximum, field):
    try:
        raw = _read_file_bytes(path, maximum, field)
        return raw.decode("utf-8", errors="replace")
    except ShaderConsoleError:
        raise
    except OSError as exc:
        raise ShaderConsoleError("%s is unavailable: %s" % (field, _bounded(exc, 512)))


def _manifest_compile_diagnostics(value, workspace, source_path):
    """Read both compiler streams through failed, source-bound job records."""
    history = value.get("history", [])
    if not isinstance(history, list) or len(history) > MAX_EVENTS:
        return {}
    jobs_root = os.path.join(workspace, "jobs")
    by_source_sha256 = {}
    for record in history:
        if not isinstance(record, dict) or record.get("status") != "failed":
            continue
        operation = str(record.get("operation", "") or "")
        if operation not in ("compile", "compile-route-chain"):
            continue
        hops = record.get("hops") if operation == "compile-route-chain" else [record]
        if not isinstance(hops, list):
            continue
        for hop in hops:
            if not isinstance(hop, dict) or hop.get("status") != "failed":
                continue
            if str(hop.get("operation", "") or "") != "compile":
                continue
            source_sha256 = str(hop.get("inputSha256", "") or "").lower()
            if SHA256_RE.fullmatch(source_sha256) is None:
                continue
            diagnostics = []
            seen = set()
            for stream in ("stderr", "stdout"):
                relative = hop.get(stream + "Path")
                if not isinstance(relative, str):
                    continue
                try:
                    path = _safe_relative(workspace, relative, "compiler log path")
                    if not _within(jobs_root, path):
                        continue
                    output = _read_bounded_text(path, MAX_DIAGNOSTIC_LOG_BYTES, "compiler log")
                    for diagnostic in parse_compiler_diagnostics(output, source_path, workspace):
                        key = _diagnostic_identity(diagnostic)
                        if key not in seen and len(diagnostics) < 128:
                            seen.add(key)
                            diagnostics.append(diagnostic)
                except ShaderConsoleError:
                    continue
            if diagnostics:
                by_source_sha256[source_sha256] = diagnostics
    return by_source_sha256


def _validate_manifest(workspace, authorization):
    manifest_path = os.path.join(workspace, "manifest.json")
    value = _read_json(manifest_path, MAX_MANIFEST_BYTES, "shader workspace manifest")
    if value.get("schema") != WORKSPACE_SCHEMA or value.get("schemaVersion") != 1:
        raise ShaderConsoleError("shader workspace manifest schema is invalid")
    if str(value.get("workspaceId", "")).lower() != authorization["workspaceId"]:
        raise ShaderConsoleError("authorized workspace ID no longer matches its manifest")
    capture = value.get("capture")
    shader = value.get("shader")
    source = value.get("source")
    if not isinstance(capture, dict) or not isinstance(shader, dict) or not isinstance(source, dict):
        raise ShaderConsoleError("shader workspace manifest identity is incomplete")
    expected = authorization
    comparisons = (
        (str(capture.get("captureId", "")).lower(), expected["captureId"]),
        (str(shader.get("rawSha256", "")).lower(), expected["shaderRawSha256"]),
        (str(shader.get("stage", "")).lower(), expected["stage"]),
        (str(shader.get("entryPoint", "")), expected["entryPoint"]),
        (_positive_integer(shader.get("resourceId"), 0, 0x7FFFFFFFFFFFFFFF),
         expected["resourceId"]),
    )
    if any(observed != declared for observed, declared in comparisons):
        raise ShaderConsoleError("authorized shader identity no longer matches its manifest")
    if (source.get("encoding") != "utf-8" or
            source.get("lane") != "executable-editable" or
            (type(source.get("compileEligible")) is not bool
             if authorization.get("viewOnlySource") else
             source.get("compileEligible") is not True)):
        raise ShaderConsoleError(
            "workspace source is not an executable editable UTF-8 representation")
    source_path = _safe_relative(workspace, source.get("path"), "workspace source path")
    source_root = os.path.join(workspace, "source")
    if not _within(source_root, source_path) or SOURCE_EXTENSION_RE.search(source_path) is None:
        raise ShaderConsoleError("workspace source is outside its dedicated source directory")
    source_path = _regular_file_in_tree(
        source_root, source_path, MAX_SOURCE_BYTES, "workspace shader source")
    diagnostics = _manifest_compile_diagnostics(value, workspace, source_path)
    return source_path, diagnostics, manifest_path


def _validate_organization_source_receipt(workspace, source_path, gate):
    """Check the Host's finalized source receipt used by the editor.

    This reader checks immutable source identity and the sealed session. The
    separate live-edit authorization and Host save path still govern effects.
    """
    verdict = gate.get("verdict")
    if (not isinstance(verdict, dict)
            or set(verdict) != {"schema", "accepted", "level", "reasons"}
            or verdict.get("schema") != "replaylab-review-verdict/v1"
            or verdict.get("accepted") is not True
            or verdict.get("level") != gate.get("equivalenceLevel")
            or verdict.get("reasons") != []
            or gate.get("finishedCurrentDraft") is not True
            or gate.get("publicationContract") != "validated-source"):
        raise ShaderConsoleError("shader organization source receipt is inconsistent")

    organization_id = gate["organizationId"]
    session_dir = _safe_relative(
        workspace, "ai/organization-sessions/" + organization_id,
        "shader organization session path")

    def session_json(name):
        path = os.path.join(session_dir, name)
        _regular_file_in_tree(workspace, path, MAX_MANIFEST_BYTES,
                              "shader organization session artifact")
        return _read_json(path, MAX_MANIFEST_BYTES, "shader organization session artifact")

    session = session_json("session.json")
    session_gate = session_json("gate.json")
    if (session_gate != gate
            or session.get("schema") != "renderdoc-ai-shader-organization-session/v1"
            or session.get("schemaVersion") != 1
            or session.get("policyVersion") != ORGANIZATION_POLICY_VERSION
            or session.get("state") != "completed"
            or not _same_path(session.get("workspacePath", ""), workspace)
            or not _same_path(_safe_relative(workspace, session.get("sourcePath", ""),
                                            "organization source path"), source_path)
            or session.get("completedAt") != gate.get("reviewedAt")):
        raise ShaderConsoleError("shader organization completed Host session is inconsistent")
    for key in (
            "organizationId", "workspaceId", "captureId", "stage", "entryPoint",
            "baselineSourceSha256", "proposalSourceSha256", "baselineCodeSha256",
            "organizedCodeSha256", "codeSha256Equal", "equivalenceLevel",
            "observation", "renameMap", "verdict", "decision",
            "organizedSourcePromoted", "finalBinding", "flowGraphId", "flowGraphSha256"):
        if session.get(key) != gate.get(key):
            if (key in ("flowGraphId", "flowGraphSha256")
                    and key not in session and gate.get(key) == (
                        "")):
                continue
            raise ShaderConsoleError("shader organization gate differs from its Host session")
    for key in ("finishedCurrentDraft", "publicationContract"):
        if session.get(key) != gate.get(key):
            raise ShaderConsoleError("shader organization finish differs from its Host session")

    source_format = _project_workspace_source_format(session)
    if ((source_format == "glsl" and not source_path.lower().endswith(".glsl")) or
            (source_format == "hlsl" and HLSL_SOURCE_EXTENSION_RE.search(source_path) is None)):
        raise ShaderConsoleError("shader organization source language is inconsistent")
    for name, key, digest_key in (
            ("baseline-source." + source_format, "baselineSourcePath", "baselineSourceSha256"),
            ("organized-proposal." + source_format, "proposalSourcePath", "proposalSourceSha256")):
        path = os.path.join(session_dir, name)
        declared = _safe_relative(workspace, session.get(key, ""), "organization artifact path")
        if not _same_path(declared, path):
            raise ShaderConsoleError("shader organization source artifact path is inconsistent")
        _regular_file_in_tree(workspace, path, MAX_SOURCE_BYTES, "organization source artifact")
        data = _read_file_bytes(path, MAX_SOURCE_BYTES, "organization source artifact")
        if hashlib.sha256(data).hexdigest() != gate[digest_key]:
            raise ShaderConsoleError("shader organization source artifact bytes changed")
        if key == "proposalSourcePath":
            proposal = gate.get(key)
            if not isinstance(proposal, str) or not proposal:
                raise ShaderConsoleError("shader organization proposal path is invalid")
            if not os.path.isabs(proposal):
                proposal = _safe_relative(workspace, proposal, "organization proposal path")
            if not _same_path(proposal, path):
                raise ShaderConsoleError("shader organization proposal path is inconsistent")

    level = gate["equivalenceLevel"]
    observation = gate.get("observation")
    renames = gate.get("renameMap")
    if (not isinstance(renames, dict) or len(renames) > 4096
            or any(not isinstance(old, str) or not isinstance(new, str)
                   or re.fullmatch(
                       r"(?:(?:cbuffer|resource):)?[A-Za-z_][A-Za-z0-9_]*"
                       r"(?:(?:\.|::)[A-Za-z_][A-Za-z0-9_]*)?", old
                   ) is None
                   or HLSL_IDENTIFIER_RE.fullmatch(new) is None
                   for old, new in renames.items())):
        raise ShaderConsoleError("shader organization rename evidence is invalid")
    if level in ("exact-code", "rename-only") and observation is not None:
        raise ShaderConsoleError("static organization proof contains inconsistent observation")
    if level == "observational-pass":
        if (not isinstance(observation, dict)
                or observation.get("schema") != "renderdoc-ai-shader-organization-observation/v1"
                or observation.get("scope") != "captured-event-output"
                or observation.get("accepted") is not True
                or observation.get("replayRestored") is not True
                or observation.get("semanticEquivalenceVerified") is not False
                or type(observation.get("sampleCount")) is not int
                or observation["sampleCount"] < 1
                or not isinstance(observation.get("metrics"), dict)
                or not isinstance(observation.get("tolerances"), dict)):
            raise ShaderConsoleError("shader organization Host output observation is invalid")
        for observation_key, gate_key in (
                ("baselineSourceSha256", "baselineSourceSha256"),
                ("candidateSourceSha256", "proposalSourceSha256"),
                ("baselineCandidateRawSha256", "baselineReviewCandidateRawSha256"),
                ("candidateRawSha256", "finalCandidateRawSha256")):
            value = observation.get(observation_key)
            if (not isinstance(value, str) or SHA256_RE.fullmatch(value) is None
                    or value != gate.get(gate_key)):
                raise ShaderConsoleError("shader organization observation candidate binding changed")


def _validate_project_source_program_binding(library, document, manifest, authorization):
    """Accept debug-container changes only through registered program identity.

    Imported source keeps its origin knowledge identity, while its receipt and
    live authorization belong to the destination capture. Resource IDs and raw
    container hashes therefore need not equal the origin's values. This grants
    editor visibility only; the live service still validates the new binary.
    """
    project_root = os.path.join(library["projectRoot"],
                               *ACTIVE_PROJECT_LAYOUT["replayProject"].split("/"))
    project, unused_sha = _read_project_json(
        os.path.join(project_root, "project.json"), MAX_PROJECT_MANIFEST_BYTES,
        "replay project manifest", project_root)
    if project.get("projectId") != library.get("projectId"):
        raise ShaderConsoleError("imported source belongs to another project")
    captures = project.get("captures") or []
    records = [item for item in (project.get("knowledge") or {}).get("workspaces", [])
               if isinstance(item, dict) and item.get("knowledgeId") == document["knowledgeId"]]
    if len(records) != 1:
        raise ShaderConsoleError("imported project knowledge is unavailable or ambiguous")
    origin = records[0]
    validated = _validate_project_workspace_record(project_root, captures, origin, {
        "projectWorkspaceId": library["projectWorkspaceId"],
        "projectId": library["projectId"],
        "activationRevision": document.get("activationRevision", 0),
    })
    if any(validated.get(key) != document.get(key) for key in (
            "knowledgeId", "rawShaderSha256", "sourceSha256", "baselineAbiSha256",
            "targetProfile", "stage", "entryPoint", "originWorkspaceId")):
        raise ShaderConsoleError("imported source differs from its project knowledge")
    capture = manifest.get("capture") or {}
    if capture.get("captureId") != authorization["captureId"]:
        raise ShaderConsoleError("imported project source capture changed")
    try:
        shaders = _project_capture_revision(captures, authorization["captureId"],
                                            capture.get("catalogSha256"))
    except ShaderConsoleError as exc:
        if str(exc) != "project workspace catalog provenance is unavailable":
            raise
        # Workspace hashes describe transport files; project revision hashes
        # describe normalized portable bytes. Resolve only the exact target
        # Shader's immutable evidence, including on its first source import.
        # Identical evidence in several revisions is harmless; conflicting
        # evidence remains ambiguous. Never guess the active revision.
        registered = next(item for item in captures if isinstance(item, dict) and
                          item.get("captureId") == authorization["captureId"])
        revisions = registered.get("catalogRevisions") or [registered]
        evidence = {}
        fields = ("resourceId", "rawSha256", "stage", "entryPoint", "codeSha256",
                  "interfaceHash", "interfaceComplete", "layoutHash", "encoding")
        for revision in revisions:
            if not isinstance(revision, dict):
                raise ShaderConsoleError("project capture revisions are invalid")
            rows = _project_capture_revision(
                captures, authorization["captureId"], revision.get("catalogSha256"))
            matches = [shader for shader in rows if isinstance(shader, dict) and
                       shader.get("resourceId") == authorization["resourceId"] and
                       shader.get("rawSha256") == authorization["shaderRawSha256"] and
                       shader.get("stage") == authorization["stage"] and
                       shader.get("entryPoint") == authorization["entryPoint"]]
            if len(matches) > 1:
                raise
            for shader in matches:
                projection = {key: shader.get(key) for key in fields}
                evidence[_canonical_sha256(projection)] = projection
        if len(evidence) != 1:
            raise
        shaders = list(evidence.values())
    targets = [item for item in shaders if isinstance(item, dict) and
               item.get("resourceId") == authorization["resourceId"] and
               item.get("rawSha256") == authorization["shaderRawSha256"] and
               item.get("stage") == authorization["stage"] and
               item.get("entryPoint") == authorization["entryPoint"]]
    if len(targets) != 1:
        raise ShaderConsoleError("imported project source target is unavailable or ambiguous")
    target = targets[0]
    if (origin.get("interfaceComplete") is not True or
            target.get("interfaceComplete") is not True or
            any(not origin.get(key) or origin.get(key) != target.get(key)
                for key in ("codeSha256", "interfaceHash")) or
            origin.get("layoutHash", "") != target.get("layoutHash", "") or
            (manifest.get("baseline") or {}).get("codeSha256") != target.get("codeSha256")):
        raise ShaderConsoleError("imported project source program differs from current Shader")
    origin_capture = next((item for item in captures if
                           item.get("captureId") == origin.get("captureId")), {})
    target_capture = next((item for item in captures if
                           item.get("captureId") == authorization["captureId"]), {})
    inventory_origin = origin.get("inventoryOrigin") or {}
    if inventory_origin:
        origin_api, origin_encoding = inventory_origin.get("api"), inventory_origin.get("encoding")
    else:
        origin_shaders = _project_capture_revision(
            captures, origin["captureId"], origin["originCatalogSha256"])
        origin_shader = next((item for item in origin_shaders if
                              item.get("resourceId") == validated["originResourceId"] and
                              item.get("rawSha256") == origin["rawSha256"] and
                              item.get("stage") == origin["stage"] and
                              item.get("entryPoint") == origin["entryPoint"]), {})
        origin_api, origin_encoding = origin_capture.get("api"), origin_shader.get("encoding")
    api = lambda value: _project_identity_api_family(str(value or "").split(".")[-1])
    encoding = lambda value: _project_shader_encoding(str(value or "").split(".")[-1])
    if (api(origin_api) != api(target_capture.get("api")) or
            api(capture.get("api")) != api(target_capture.get("api")) or
            encoding(origin_encoding) != encoding(target.get("encoding")) or
            encoding((manifest.get("baseline") or {}).get("kind")) != encoding(target.get("encoding"))):
        raise ShaderConsoleError("imported project source platform differs from current Shader")


def _validate_project_source_gate(workspace, authorization, source_path):
    """Resolve imported source provenance through the existing project reader.

    This permits editing a reused source; it never grants compilation or
    replacement. Those effects still use the current workspace's live service.
    The original project graph remains immutable and its navigation is only
    projected onto the new document in memory.
    """
    manifest_path = os.path.join(workspace, "manifest.json")
    manifest = _read_json(manifest_path, MAX_MANIFEST_BYTES, "shader workspace manifest")
    source = manifest.get("source") or {}
    receipt = source.get("projectSource")
    if not isinstance(receipt, dict):
        raise ShaderConsoleError("workspace has no imported project source")
    unsigned = dict(receipt)
    digest = unsigned.pop("receiptSha256", None)
    baseline = manifest.get("baseline") or {}
    if (receipt.get("schema") != "replaylab-verified-project-shader-source/v1" or
            digest != _canonical_sha256(unsigned) or
            manifest.get("workspaceId") != authorization["workspaceId"] or
            receipt.get("originalRawSha256") != authorization["shaderRawSha256"] or
            receipt.get("baselineAbiSha256") != baseline.get("abiSha256") or
            receipt.get("stage") != authorization["stage"] or
            receipt.get("entryPoint") != authorization["entryPoint"] or
            receipt.get("sourceSha256") != source.get("sha256") or
            receipt.get("sourceEntryPoint") != source.get("entryPoint") or
            receipt.get("targetProfile") != manifest.get("targetProfile") or
            receipt.get("applied") is not False or
            receipt.get("requiresCompile") is not True or
            receipt.get("requiresLiveAbiRevalidation") is not True or
            receipt.get("semanticEquivalenceVerified") is not False):
        raise ShaderConsoleError("imported project source binding changed")
    source_sha = _project_sha256(receipt.get("sourceSha256"), "imported project source hash")
    source_format = _project_workspace_source_format(receipt)
    if ((source_format == "glsl" and not source_path.lower().endswith(".glsl")) or
            (source_format == "hlsl" and HLSL_SOURCE_EXTENSION_RE.search(source_path) is None)):
        raise ShaderConsoleError("imported project source language changed")
    input_relative = "source-inputs/project-%s.%s" % (source_sha, source_format)
    input_path = _safe_relative(workspace, input_relative, "imported project input")
    if receipt.get("inputPath") != input_relative:
        raise ShaderConsoleError("imported project input path changed")
    _regular_file_in_tree(workspace, input_path, MAX_SOURCE_BYTES, "imported project input")
    if hashlib.sha256(_read_file_bytes(input_path, MAX_SOURCE_BYTES, "imported project input")).hexdigest() != source_sha:
        raise ShaderConsoleError("imported project input bytes changed")
    library = _project_library_for_console(default_active_project_selection_path())
    if (library.get("projectId") != receipt.get("projectId") or
            not _same_path(os.path.join(library["projectRoot"],
                                       *ACTIVE_PROJECT_LAYOUT["replayProject"].split("/")),
                           receipt.get("projectPath", ""))):
        raise ShaderConsoleError("imported source belongs to another project")
    managed_root = os.path.join(library["projectRoot"],
                               *ACTIVE_PROJECT_LAYOUT["shaderWorkspaces"].split("/"))
    if not _within(managed_root, workspace):
        raise ShaderConsoleError("imported source is outside the active project's Shader Workspace")
    documents = [item for item in library.get("documents", [])
                 if item.get("knowledgeId") == receipt.get("knowledgeId")]
    if len(documents) != 1:
        raise ShaderConsoleError("imported project knowledge is unavailable or ambiguous")
    document = documents[0]
    if _project_workspace_source_format(document) != source_format:
        raise ShaderConsoleError("imported source language differs from its project knowledge")
    for document_key, receipt_key in (
            ("sourceSha256", "sourceSha256"),
            ("stage", "stage"),
            ("entryPoint", "entryPoint"), ("targetProfile", "targetProfile"),
            ("originWorkspaceId", "originWorkspaceId"), ("analysisSha256", "analysisSha256")):
        if document.get(document_key) != receipt.get(receipt_key):
            raise ShaderConsoleError("imported source differs from its project knowledge")
    if document.get("rawShaderSha256") != receipt.get("originalRawSha256"):
        if document.get("baselineAbiSha256") != receipt.get("baselineAbiSha256"):
            raise ShaderConsoleError("imported source physical ABI differs from its project knowledge")
        _validate_project_source_program_binding(library, document, manifest, authorization)
    # For identical original bytes, the imported receipt above is bound to
    # THIS workspace's baseline. Old reflection/display hash versions from
    # another RDC cannot veto its editor. Compile/apply authority stays with
    # the current target's compiler and replay service.
    projection = None
    graph = document.get("flowGraph")
    if isinstance(graph, dict):
        nodes = []
        for original in graph["nodes"]:
            node = dict(original)
            location = dict(original["source"])
            location.update({"workspaceId": authorization["workspaceId"],
                             "documentId": authorization["workspaceId"],
                             "path": source["path"]})
            node["source"] = location
            nodes.append(node)
        projection = dict(graph)
        projection.update({"nodes": nodes, "navigationDocumentId": authorization["workspaceId"]})
    return {
        "kind": "verified-project-source", "path": manifest_path,
        "finalSourceSha256": source_sha,
        "projectFlowGraph": projection,
        "projectFlowGraphStatus": document.get("flowGraphStatus"),
        "watchedPaths": library.get("watchedPaths", []) + [input_path],
    }


def _validate_editable_source_gate(workspace, authorization, source_path):
    if authorization.get("viewOnlyOrganization") is True:
        return _validate_open_organization_source_preview(
            workspace, authorization, source_path)
    manifest = _read_json(os.path.join(workspace, "manifest.json"),
                          MAX_MANIFEST_BYTES, "shader workspace manifest")
    gate_path = _safe_relative(
        workspace, "ai/organize-code-gate.json", "shader organization gate path")
    gate = (_read_json(gate_path, MAX_MANIFEST_BYTES, "shader organization gate")
            if os.path.isfile(gate_path) else {})
    if gate.get("decision") == "editable-draft":
        return _validate_current_draft_gate(
            workspace, authorization, source_path, gate_path, gate)
    if isinstance((manifest.get("source") or {}).get("projectSource"), dict):
        return _validate_project_source_gate(workspace, authorization, source_path)
    return _validate_organization_gate(workspace, authorization, source_path)


def _validate_open_organization_source_preview(workspace, authorization, source_path):
    """Read a registered canonical source while its separate proposal is open.

    This memory-only locator is not a live-edit grant. Imported project bytes,
    the current organization pointer and its baseline must agree; proposal
    changes never authorize compilation or replace the source being displayed.
    """
    gate_path = os.path.join(workspace, "ai", "organize-code-gate.json")
    _regular_file_in_tree(workspace, gate_path, MAX_MANIFEST_BYTES,
                          "shader organization gate")
    gate = _read_json(gate_path, MAX_MANIFEST_BYTES, "shader organization gate")
    organization_id = str(gate.get("organizationId") or "")
    identity_keys = ("workspaceId", "captureId", "stage", "entryPoint")
    binding = gate.get("finalBinding")
    if (gate.get("schema") != ORGANIZATION_GATE_SCHEMA or
            gate.get("schemaVersion") != 1 or
            gate.get("policyVersion") != ORGANIZATION_POLICY_VERSION or
            gate.get("decision") not in ("drafting", "awaiting-proposal-review") or
            gate.get("requestedTransformation") != "organize-only" or
            gate.get("safeToStartLiveEdit") is not False or
            gate.get("organizedSourcePromoted") is not False or
            gate.get("replayApplied") is not False or
            not isinstance(binding, dict) or binding.get("complete") is not False or
            EXACT_TOKEN_RE.fullmatch(organization_id) is None or
            any(gate.get(key) != authorization[key] for key in identity_keys)):
        raise ShaderConsoleError("open organization source preview pointer is invalid")
    session_dir = _safe_relative(
        workspace, "ai/organization-sessions/" + organization_id,
        "open organization session")
    session_path = os.path.join(session_dir, "session.json")
    _regular_file_in_tree(workspace, session_path, MAX_MANIFEST_BYTES,
                          "open organization session")
    session = _read_json(session_path, MAX_MANIFEST_BYTES, "open organization session")
    relative_source = os.path.relpath(source_path, workspace).replace("\\", "/")
    program_identity = session.get("shaderProgramIdentity")
    if (session.get("schema") != "renderdoc-ai-shader-organization-session/v1" or
            session.get("schemaVersion") != 1 or
            session.get("policyVersion") != ORGANIZATION_POLICY_VERSION or
            session.get("state") not in ("drafting", "awaiting-proposal-review") or
            session.get("organizationId") != organization_id or
            not _same_path(session.get("workspacePath", ""), workspace) or
            session.get("sourcePath") != relative_source or
            any(session.get(key) != authorization[key] for key in identity_keys) or
            not isinstance(program_identity, dict) or
            program_identity.get("originalRawSha256") !=
            authorization["shaderRawSha256"]):
        raise ShaderConsoleError("open organization source preview session changed")
    project_gate = _validate_project_source_gate(workspace, authorization, source_path)
    source_sha = project_gate["finalSourceSha256"]
    if (binding.get("sourceSha256") != source_sha or
            session.get("baselineSourceSha256") != source_sha):
        raise ShaderConsoleError("open organization source preview baseline changed")
    baseline_path = _safe_relative(workspace, session.get("baselineSourcePath"),
                                   "open organization baseline")
    source_format = _project_workspace_source_format(session)
    if ((source_format == "glsl" and not source_path.lower().endswith(".glsl")) or
            (source_format == "hlsl" and HLSL_SOURCE_EXTENSION_RE.search(source_path) is None) or
            not _same_path(baseline_path, os.path.join(session_dir, "baseline-source." + source_format))):
        raise ShaderConsoleError("open organization baseline escaped its session")
    _regular_file_in_tree(workspace, baseline_path, MAX_SOURCE_BYTES,
                          "open organization baseline")
    for path in (baseline_path, source_path):
        if hashlib.sha256(_read_file_bytes(
                path, MAX_SOURCE_BYTES, "open organization canonical source")).hexdigest() != source_sha:
            raise ShaderConsoleError("open organization canonical source changed")
    result = dict(project_gate)
    result.update(kind="organization-source-preview", organizationId=organization_id,
                  safeToStartLiveEdit=False)
    result["watchedPaths"] = list(project_gate.get("watchedPaths", [])) + [
        gate_path, session_path, baseline_path]
    return result


def _validate_current_draft_gate(workspace, authorization, source_path,
                                 gate_path, gate):
    """Expose saved canonical HLSL for reading, without apply authorization."""
    organization_id = str(gate.get("organizationId", "") or "").lower()
    source_sha = str(gate.get("proposalSourceSha256", "") or "").lower()
    baseline_sha = str(gate.get("baselineSourceSha256", "") or "").lower()
    binding = gate.get("finalBinding")
    identity = (gate.get("workspaceId"), gate.get("captureId"),
                gate.get("stage"), gate.get("entryPoint"))
    expected = (authorization["workspaceId"], authorization["captureId"],
                authorization["stage"], authorization["entryPoint"])
    if (gate.get("schema") != ORGANIZATION_GATE_SCHEMA or
            gate.get("schemaVersion") != 1 or
            gate.get("policyVersion") != ORGANIZATION_POLICY_VERSION or
            gate.get("requestedTransformation") != "organize-only" or
            gate.get("decision") != "editable-draft" or
            gate.get("safeToStartLiveEdit") is not False or
            gate.get("replayApplied") is not False or
            gate.get("semanticEquivalenceVerified") is not False or
            not isinstance(binding, dict) or
            binding.get("complete") is not False or
            binding.get("sourceSha256") != source_sha or
            identity != expected or
            EXACT_TOKEN_RE.fullmatch(organization_id) is None or
            SHA256_RE.fullmatch(source_sha) is None or
            SHA256_RE.fullmatch(baseline_sha) is None):
        raise ShaderConsoleError("shader organization editable draft receipt is invalid")
    declared = gate.get("editableDraftSourcePath")
    if not isinstance(declared, str) or not _same_path(declared, source_path):
        raise ShaderConsoleError("shader organization editable draft points at another source")
    session_dir = _safe_relative(
        workspace, "ai/organization-sessions/" + organization_id,
        "shader organization draft session path")
    session_path = os.path.join(session_dir, "session.json")
    _regular_file_in_tree(workspace, session_path, MAX_MANIFEST_BYTES,
                          "shader organization draft session")
    session = _read_json(session_path, MAX_MANIFEST_BYTES,
                         "shader organization draft session")
    relative_source = os.path.relpath(source_path, workspace).replace("\\", "/")
    program_identity = session.get("shaderProgramIdentity") or {}
    if (session.get("schema") != "renderdoc-ai-shader-organization-session/v1" or
            session.get("schemaVersion") != 1 or
            session.get("policyVersion") != ORGANIZATION_POLICY_VERSION or
            session.get("state") not in ("drafting", "awaiting-proposal-review") or
            session.get("organizationId") != organization_id or
            session.get("workspaceId") != authorization["workspaceId"] or
            session.get("captureId") != authorization["captureId"] or
            session.get("stage") != authorization["stage"] or
            session.get("entryPoint") != authorization["entryPoint"] or
            session.get("sourcePath") != relative_source or
            session.get("syncedDraftSourceSha256") != source_sha or
            session.get("baselineSourceSha256") != baseline_sha or
            program_identity.get("originalRawSha256") != authorization["shaderRawSha256"]):
        raise ShaderConsoleError("shader organization editable draft session changed")
    for key, digest in (("baselineSourcePath", baseline_sha),
                        ("proposalSourcePath", source_sha)):
        relative = session.get(key)
        path = _safe_relative(workspace, relative,
                              "shader organization draft source path")
        if not _within(session_dir, path):
            raise ShaderConsoleError("shader organization draft source escapes its session")
        _regular_file_in_tree(workspace, path, MAX_SOURCE_BYTES,
                              "shader organization draft source")
        if hashlib.sha256(_read_file_bytes(
                path, MAX_SOURCE_BYTES, "shader organization draft source")).hexdigest() != digest:
            raise ShaderConsoleError("shader organization draft source changed")
    _regular_file_in_tree(workspace, source_path, MAX_SOURCE_BYTES,
                          "shader workspace current source")
    current_source_sha = hashlib.sha256(_read_file_bytes(
        source_path, MAX_SOURCE_BYTES, "shader workspace current source")).hexdigest()
    # This receipt proves which workspace/session owns the editable document;
    # it does not freeze future user edits. A failed compile or a later project
    # import can leave a valid older draft receipt behind. Read the current
    # canonical text and let the Host's normal compare-and-save / compile path
    # validate that revision, without granting the historical receipt apply rights.
    manifest = _read_json(os.path.join(workspace, "manifest.json"),
                          MAX_MANIFEST_BYTES, "shader workspace manifest")
    revision = manifest.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise ShaderConsoleError("shader workspace current Source revision is invalid")
    return {"kind": "editable-draft", "path": gate_path,
            "finalSourceSha256": current_source_sha, "finalRevision": revision,
            "draftReceiptSourceSha256": source_sha,
            "draftReceiptCurrent": current_source_sha == source_sha,
            "safeToStartLiveEdit": False}


def _validate_organization_gate(workspace, authorization, source_path):
    """Bind the editor source to a finalized Host compile/ABI receipt.

    Live-edit authorization separately governs compilation and replay apply.
    This receipt does not judge whether the Agent has finished explaining or
    reorganizing the shader to the user's satisfaction.
    """
    gate_path = _safe_relative(
        workspace, "ai/organize-code-gate.json", "shader organization gate path")
    _regular_file_in_tree(
        os.path.join(workspace, "ai"), gate_path, MAX_MANIFEST_BYTES,
        "shader organization gate")
    gate = _read_json(
        gate_path, MAX_MANIFEST_BYTES, "shader organization gate")
    if gate.get("schema") != ORGANIZATION_GATE_SCHEMA:
        raise ShaderConsoleError("shader organization gate schema is invalid")
    if (gate.get("schemaVersion") != 1 or
            gate.get("policyVersion") != ORGANIZATION_POLICY_VERSION or
            gate.get("requestedTransformation") != "organize-only" or
            gate.get("decision") != "accepted-executable-editable" or
            gate.get("finishedCurrentDraft") is not True or
            gate.get("publicationContract") != "validated-source" or
            gate.get("organizedSourcePromoted") is not True or
            gate.get("baselineCompiledAndAbiValidated") is not True or
            gate.get("organizedCompiledAndAbiValidated") is not True or
            gate.get("rawRecoveryCompiledAndAbiValidated") is not False or
            gate.get("semanticEquivalenceVerified") is not False or
            gate.get("safeToStartLiveEdit") is not True or
            gate.get("replayApplied") is not False):
        raise ShaderConsoleError(
            "shader organization gate did not promote editable HLSL")
    identity = (
        str(gate.get("workspaceId", "") or "").lower(),
        str(gate.get("captureId", "") or "").lower(),
        str(gate.get("stage", "") or "").lower(),
        str(gate.get("entryPoint", "") or ""),
    )
    expected = (
        authorization["workspaceId"], authorization["captureId"],
        authorization["stage"], authorization["entryPoint"])
    binding = gate.get("finalBinding")
    final_source_sha256 = str(gate.get("finalSourceSha256", "") or "").lower()
    binding_fields = {
        "complete", "sourceSha256", "revision", "candidateRawSha256",
        "codeSha256", "abiRequestId", "abiReportSha256",
    }
    organization_id = str(gate.get("organizationId", "") or "").lower()
    reviewed_at = gate.get("reviewedAt")
    baseline_source = str(gate.get("baselineSourceSha256", "") or "").lower()
    proposal_source = str(gate.get("proposalSourceSha256", "") or "").lower()
    baseline_code = str(gate.get("baselineCodeSha256", "") or "").lower()
    organized_code = str(gate.get("organizedCodeSha256", "") or "").lower()
    final_candidate = str(gate.get("finalCandidateRawSha256", "") or "").lower()
    final_code = str(gate.get("finalCodeSha256", "") or "").lower()
    final_abi_request = str(gate.get("finalAbiRequestId", "") or "").lower()
    final_abi_report = str(gate.get("finalAbiReportSha256", "") or "").lower()
    final_revision = gate.get("finalRevision")
    flow_graph_id = str(gate.get("flowGraphId", "") or "").lower()
    flow_graph_sha256 = str(gate.get("flowGraphSha256", "") or "").lower()
    flow_graph_artifact = gate.get("flowGraphArtifactPath", "")
    unbound_finish_graph_path = bool(
        not flow_graph_id and not flow_graph_sha256 and flow_graph_artifact)
    if unbound_finish_graph_path:
        # A finished draft can defer the explanatory graph. The Host records
        # its intended canonical location even when it published no graph
        # identity. This location grants no graph/source authority.
        if not isinstance(flow_graph_artifact, str):
            raise ShaderConsoleError(
                "shader organization gate flow graph binding is invalid")
        expected_graph_path = os.path.join(workspace, "ai", "flow-graph.json")
        declared_graph_path = (os.path.abspath(flow_graph_artifact)
                               if os.path.isabs(flow_graph_artifact) else
                               _safe_relative(workspace, flow_graph_artifact,
                                              "organized Shader flow graph path"))
        if (not _within(workspace, declared_graph_path) or
                not _same_path(declared_graph_path, expected_graph_path)):
            raise ShaderConsoleError(
                "shader organization gate flow graph binding is invalid")
    graph_binding_present = bool(
        flow_graph_id or flow_graph_sha256 or
        (flow_graph_artifact and not unbound_finish_graph_path))
    if (identity != expected or not isinstance(binding, dict) or
            set(binding) != binding_fields or
            binding.get("complete") is not True or
            EXACT_TOKEN_RE.fullmatch(organization_id) is None or
            not isinstance(reviewed_at, str) or not reviewed_at or
            len(reviewed_at) > 128 or
            SHA256_RE.fullmatch(final_source_sha256) is None or
            any(SHA256_RE.fullmatch(item) is None for item in (
                baseline_source, proposal_source, baseline_code,
                organized_code, final_candidate, final_code,
                final_abi_request, final_abi_report)) or
            isinstance(final_revision, bool) or
            not isinstance(final_revision, int) or final_revision <= 0 or
            str(binding.get("sourceSha256", "") or "").lower() !=
            final_source_sha256 or
            binding.get("revision") != final_revision or
            str(binding.get("candidateRawSha256", "") or "").lower() !=
            final_candidate or
            str(binding.get("codeSha256", "") or "").lower() != final_code or
            str(binding.get("abiRequestId", "") or "").lower() !=
            final_abi_request or
            str(binding.get("abiReportSha256", "") or "").lower() !=
            final_abi_report):
        raise ShaderConsoleError(
            "shader organization gate is not bound to this workspace")

    level = gate.get("equivalenceLevel")
    code_equal = baseline_code == organized_code
    if (gate.get("codeSha256Equal") is not code_equal
            or level not in ("exact-code", "rename-only", "observational-pass")
            or (level == "exact-code") is not code_equal
            or final_source_sha256 != proposal_source
            or final_code != organized_code):
        raise ShaderConsoleError("shader organization equivalence evidence is inconsistent")
    _validate_organization_source_receipt(workspace, source_path, gate)

    # Older reviewed workspaces legitimately have no structured graph.  A
    # newer gate must, however, bind all graph identity fields together; a
    # partial binding is never treated as optional metadata.
    if graph_binding_present:
        if (EXACT_TOKEN_RE.fullmatch(flow_graph_id) is None or
                SHA256_RE.fullmatch(flow_graph_sha256) is None or
                not isinstance(flow_graph_artifact, str) or
                not flow_graph_artifact):
            raise ShaderConsoleError(
                "shader organization gate flow graph binding is invalid")
        if os.path.isabs(flow_graph_artifact):
            declared_graph_path = os.path.abspath(flow_graph_artifact)
            if not _within(workspace, declared_graph_path):
                raise ShaderConsoleError(
                    "shader organization gate flow graph escapes its workspace")
        else:
            declared_graph_path = _safe_relative(
                workspace, flow_graph_artifact,
                "organized Shader flow graph path")
        expected_graph_path = os.path.join(workspace, "ai", "flow-graph.json")
        if not _same_path(declared_graph_path, expected_graph_path):
            raise ShaderConsoleError(
                "shader organization gate points at a different flow graph")

    declared_source = gate.get("executableEditableSourcePath")
    if not isinstance(declared_source, str) or not declared_source:
        raise ShaderConsoleError("shader organization gate source is missing")
    if os.path.isabs(declared_source):
        declared_path = os.path.abspath(declared_source)
        if not _within(workspace, declared_path):
            raise ShaderConsoleError(
                "shader organization gate source escapes its workspace")
    else:
        declared_path = _safe_relative(
            workspace, declared_source, "organized editable source path")
    if not _same_path(declared_path, source_path):
        raise ShaderConsoleError(
            "shader organization gate points at a different source")
    return {
        "path": gate_path,
        "finalSourceSha256": final_source_sha256,
        "finalRevision": final_revision,
        "finalCandidateRawSha256": final_candidate,
        "finalCodeSha256": final_code,
        "flowGraphId": flow_graph_id,
        "flowGraphSha256": flow_graph_sha256,
        "flowGraphArtifactPath": (
            declared_graph_path if graph_binding_present else ""),
    }


def _normalized_shader_flow_path(value, field):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ShaderConsoleError("%s is invalid" % field)
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (normalized.startswith("/") or
            re.match(r"^[A-Za-z]:", normalized) is not None or
            any(part in ("", ".", "..") for part in parts)):
        raise ShaderConsoleError("%s is not workspace-relative" % field)
    return normalized


def _validate_shader_flow_graph_value(
        graph, expected_workspace_id="", expected_source_sha256="",
        expected_source_revision=None, expected_source_path="",
        expected_stage="", expected_entry_point=None):
    """Strictly validate a navigation graph without granting file authority."""

    expected_fields = {
        "schema", "schemaVersion", "graphId", "workspaceId", "stage",
        "entryPoint", "sourcePath", "sourceSha256", "sourceRevision",
        "generatedAt", "origin", "nodes", "edges", "limits", "truncated",
        "policy",
    }
    if not isinstance(graph, dict):
        raise ShaderConsoleError("Shader flow graph fields are invalid")
    algorithm = graph.get("graphKind") == "algorithm-dataflow"
    if algorithm:
        expected_fields.update(("graphKind", "layoutDirection"))
    if set(graph) != expected_fields:
        raise ShaderConsoleError("Shader flow graph fields are invalid")
    if algorithm and graph.get("layoutDirection") != "TD":
        raise ShaderConsoleError("Shader algorithm graph must use top-down data flow")
    if (graph.get("schema") != SHADER_FLOW_GRAPH_SCHEMA or
            graph.get("schemaVersion") != SHADER_FLOW_GRAPH_VERSION):
        raise ShaderConsoleError("Shader flow graph schema is invalid")

    graph_id = str(graph.get("graphId", "") or "").lower()
    workspace_id = str(graph.get("workspaceId", "") or "").lower()
    source_sha256 = str(graph.get("sourceSha256", "") or "").lower()
    source_revision = graph.get("sourceRevision")
    generated_at = graph.get("generatedAt")
    if (EXACT_TOKEN_RE.fullmatch(graph_id) is None or
            EXACT_TOKEN_RE.fullmatch(workspace_id) is None or
            SHA256_RE.fullmatch(source_sha256) is None or
            isinstance(source_revision, bool) or
            not isinstance(source_revision, int) or source_revision < 0 or
            (source_revision == 0 and graph.get("origin") != "editable-draft") or
            not isinstance(graph.get("stage"), str) or
            not graph.get("stage") or len(graph.get("stage")) > 64 or
            not isinstance(graph.get("entryPoint"), str) or
            len(graph.get("entryPoint")) > 512 or
            graph.get("origin") not in (
                "organized-proposal", "recovered-baseline", "editable-draft") or
            not isinstance(generated_at, str) or not generated_at or
            len(generated_at) > 128 or
            any(ord(character) < 32 for character in generated_at)):
        raise ShaderConsoleError(
            "Shader flow graph identity or source binding is invalid")
    graph_source_path = _normalized_shader_flow_path(
        graph.get("sourcePath"), "Shader flow graph source path")
    if (expected_workspace_id and workspace_id != expected_workspace_id):
        raise ShaderConsoleError(
            "Shader flow graph belongs to a different workspace")
    if (expected_source_sha256 and
            source_sha256 != expected_source_sha256):
        raise ShaderConsoleError(
            "Shader flow graph belongs to different source bytes")
    if (expected_source_revision is not None and
            source_revision != expected_source_revision):
        raise ShaderConsoleError(
            "Shader flow graph belongs to a different source revision")
    if (expected_source_path and graph_source_path !=
            _normalized_shader_flow_path(
                expected_source_path, "expected Shader flow graph source path")):
        raise ShaderConsoleError(
            "Shader flow graph belongs to a different source path")
    if expected_stage and graph.get("stage") != expected_stage:
        raise ShaderConsoleError(
            "Shader flow graph belongs to a different Shader stage")
    if (expected_entry_point is not None and
            graph.get("entryPoint") != expected_entry_point):
        raise ShaderConsoleError(
            "Shader flow graph belongs to a different entry point")
    if graph.get("limits") != {
            "maxNodes": MAX_SHADER_FLOW_GRAPH_NODES,
            "maxEdges": MAX_SHADER_FLOW_GRAPH_EDGES}:
        raise ShaderConsoleError("Shader flow graph limits are invalid")
    expected_policy = {
            "navigationOnly": True,
            "physicalEvidence": False,
            "compileAuthorization": False,
            "applyAuthorization": False,
            "callEdgesDerivedFromFinalSource": True,
            "descriptionsMayBeAiAuthored": True}
    if algorithm:
        expected_policy.update({
            "callEdgesDerivedFromFinalSource": False,
            "computationRangesBoundToFinalSource": True,
            "dataEdgesAreLexicallyBound": True,
        })
    if graph.get("policy") != expected_policy:
        raise ShaderConsoleError("Shader flow graph policy is invalid")

    truncated = graph.get("truncated")
    if (not isinstance(truncated, dict) or set(truncated) != {
            "nodes", "edges", "omittedNodes", "omittedEdges"} or
            type(truncated.get("nodes")) is not bool or
            type(truncated.get("edges")) is not bool):
        raise ShaderConsoleError(
            "Shader flow graph truncation metadata is invalid")
    for field in ("omittedNodes", "omittedEdges"):
        count = truncated.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ShaderConsoleError(
                "Shader flow graph truncation metadata is invalid")

    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if (not isinstance(nodes, list) or
            len(nodes) > MAX_SHADER_FLOW_GRAPH_NODES or
            not isinstance(edges, list) or
            len(edges) > MAX_SHADER_FLOW_GRAPH_EDGES):
        raise ShaderConsoleError(
            "Shader flow graph nodes or edges are invalid or oversized")
    node_ids = set()
    for node in nodes:
        required_node_fields = {"id", "name", "label", "kind", "description", "source"}
        allowed_node_fields = set(required_node_fields)
        if algorithm:
            required_node_fields.update(("sourceRange", "reads", "writes", "inferred"))
            allowed_node_fields = set(required_node_fields)
        else:
            # v1 function-interface metadata is additive so legacy call graphs
            # remain readable by the embedded Python 3.6 bridge.
            allowed_node_fields.update(("signature", "inputs", "outputs"))
        if (not isinstance(node, dict) or
                not required_node_fields.issubset(node) or
                set(node) - allowed_node_fields):
            raise ShaderConsoleError("Shader flow graph node fields are invalid")
        node_id = str(node.get("id", "") or "")
        name = str(node.get("name", "") or "")
        label = node.get("label")
        description = node.get("description")
        location = node.get("source")
        if (FLOW_GRAPH_NODE_ID_RE.fullmatch(node_id) is None or
                HLSL_IDENTIFIER_RE.fullmatch(name) is None or
                len(name) > 256 or
                not isinstance(label, str) or not label.strip() or len(label) > 256 or
                any(ord(character) < 32 for character in label) or
                (not algorithm and label != name) or
                node.get("kind") not in (
                    ("computation",) if algorithm else ("entry-point", "function")) or
                node_id in node_ids or
                not isinstance(description, str) or not description or
                len(description) > 512 or
                any(ord(character) < 32 for character in description) or
                # Compatibility with the exact default description emitted by
                # older Hosts. Its prose semicolon is not implementation text.
                (not algorithm and description !=
                 "Shader entry point; the stage result is produced here." and (
                    any(marker in description for marker in (";", "{", "}", "```")) or
                    re.search(r"(?<![=!<>])=(?!=)|\b(?:return|discard)\s+", description))) or
                not isinstance(location, dict) or set(location) != {
                    "workspaceId", "documentId", "path", "line", "column"}):
            raise ShaderConsoleError("Shader flow graph node identity is invalid")
        node_ids.add(node_id)
        if not algorithm:
            if "signature" in node:
                signature = node.get("signature")
                if (not isinstance(signature, str) or not signature.strip() or
                        len(signature) > 512 or
                        any(ord(character) < 32 for character in signature) or
                        any(marker in signature for marker in (";", "{", "}", "```"))):
                    raise ShaderConsoleError(
                        "Shader flow graph function signature is invalid")
            for key in ("inputs", "outputs"):
                if key not in node:
                    continue
                values = node.get(key)
                if (not isinstance(values, list) or len(values) > 24 or
                        any(not isinstance(value, str) or
                            HLSL_IDENTIFIER_RE.fullmatch(value) is None or
                            len(value) > 128 for value in values) or
                        len(set(values)) != len(values)):
                    raise ShaderConsoleError(
                        "Shader flow graph function interface is invalid")
        if (location.get("workspaceId") != workspace_id or
                location.get("documentId") != workspace_id or
                _normalized_shader_flow_path(
                    location.get("path"),
                    "Shader flow graph node path") != graph_source_path):
            raise ShaderConsoleError(
                "Shader flow graph node source is outside this workspace")
        for field in ("line", "column"):
            position = location.get(field)
            if (isinstance(position, bool) or not isinstance(position, int) or
                    position <= 0 or position > 10000000):
                raise ShaderConsoleError(
                    "Shader flow graph node source location is invalid")
        if algorithm:
            span = node.get("sourceRange")
            if (not isinstance(span, dict) or set(span) != {"startLine", "endLine"}
                    or type(span.get("startLine")) is not int
                    or type(span.get("endLine")) is not int
                    or not 1 <= span["startLine"] <= location["line"] <= span["endLine"] <= 10000000
                    or node.get("inferred") is not True):
                raise ShaderConsoleError("Shader computation source range is invalid")
            for key in ("reads", "writes"):
                values = node.get(key)
                if (not isinstance(values, list) or len(values) > 32
                        or any(not isinstance(value, str) or not value.strip() or len(value) > 72
                               or any(ord(character) < 32 for character in value)
                               for value in values)):
                    raise ShaderConsoleError("Shader computation values are invalid")

    edge_ids = set()
    for edge in edges:
        edge_fields = {"source", "target", "kind"}
        if (not isinstance(edge, dict) or not edge_fields.issubset(edge)
                or set(edge) - edge_fields - ({"values"} if algorithm else set())):
            raise ShaderConsoleError("Shader flow graph edge fields are invalid")
        identity = (str(edge.get("source", "") or ""),
                    str(edge.get("target", "") or ""))
        if (edge.get("kind") not in (("data", "control") if algorithm else ("calls",)) or
                identity[0] not in node_ids or identity[1] not in node_ids or
                identity in edge_ids):
            raise ShaderConsoleError("Shader flow graph edge identity is invalid")
        edge_ids.add(identity)
        if "values" in edge:
            values = edge["values"]
            if (edge["kind"] != "data" or not isinstance(values, list)
                    or not 1 <= len(values) <= 32
                    or any(not isinstance(value, str) or not value.strip() or len(value) > 72
                           or any(ord(character) < 32 for character in value)
                           for value in values)):
                raise ShaderConsoleError("Shader data edge values are invalid")

    material = dict(graph)
    material.pop("graphId", None)
    material.pop("generatedAt", None)
    if _canonical_sha256(material)[:32] != graph_id:
        raise ShaderConsoleError(
            "Shader flow graph identity does not match its contents")
    return graph


def _validate_shader_flow_graph(workspace, authorization, source_path, gate):
    """Validate optional navigation metadata against current source identity."""

    graph_path = _safe_relative(
        workspace, "ai/flow-graph.json", "Shader flow graph path")
    _regular_file_in_tree(
        os.path.join(workspace, "ai"), graph_path,
        MAX_SHADER_FLOW_GRAPH_BYTES, "Shader flow graph")
    graph_bytes = _read_file_bytes(
        graph_path, MAX_SHADER_FLOW_GRAPH_BYTES, "Shader flow graph")
    try:
        graph = json.loads(graph_bytes.decode("utf-8", errors="strict"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ShaderConsoleError(
            "Shader flow graph is invalid: %s" % _bounded(exc, 512))
    if not isinstance(graph, dict):
        raise ShaderConsoleError(
            "Shader flow graph must contain one JSON object")
    expected_relative = os.path.relpath(
        source_path, workspace).replace("\\", "/")
    if expected_relative.startswith("../") or expected_relative == "..":
        raise ShaderConsoleError("Shader flow graph source escapes its workspace")
    graph = _validate_shader_flow_graph_value(
        graph,
        expected_workspace_id=authorization["workspaceId"],
        expected_source_sha256=gate["finalSourceSha256"],
        expected_source_revision=gate["finalRevision"],
        expected_source_path=expected_relative,
        expected_stage=authorization["stage"],
        expected_entry_point=authorization["entryPoint"],
    )
    return graph, graph_path


def _load_workspace_flow_graph(workspace, authorization, source_path, gate):
    """Load an optional graph without making it editor/apply authority."""

    if gate.get("kind") in ("verified-project-source", "organization-source-preview"):
        return gate.get("projectFlowGraph"), gate.get("projectFlowGraphStatus") or {
            "available": False, "status": "unavailable", "artifactPath": ""}
    graph_path = os.path.join(workspace, "ai", "flow-graph.json")
    if not os.path.exists(graph_path) and not os.path.islink(graph_path):
        return None, {
            "available": False, "status": "unavailable", "artifactPath": graph_path}
    try:
        graph, graph_path = _validate_shader_flow_graph(
            workspace, authorization, source_path, gate)
        projection = dict(graph)
        projection.update({
            "available": True,
            "status": "current",
            "stale": False,
            "artifactPath": graph_path,
        })
        return projection, {
            "available": True, "status": "current", "artifactPath": graph_path}
    except (ShaderConsoleError, OSError) as exc:
        return None, {
            "available": False,
            "status": "invalid",
            "artifactPath": graph_path,
            "error": _bounded(exc, 1024),
        }


def _draft_live_candidate_ready(workspace, gate, live, watch):
    """Use physical candidate and active replay facts, never a review verdict."""
    if (gate.get("kind") != "editable-draft" or
            not isinstance(live, dict) or live.get("state") != "running" or
            live.get("sessionState", "current") != "current" or
            not isinstance(watch, dict) or watch.get("state") != "running" or
            live.get("watchId") != watch.get("watchId") or
            live.get("activeRevision", 0) <= 0):
        return False
    source_sha = gate["finalSourceSha256"]
    candidate_revision = live.get("latestHandledRevision", 0) or live["activeRevision"]
    if (watch.get("observedSourceSha256") != source_sha or
            watch.get("lastCompiledSourceSha256") != source_sha or
            watch.get("latestCandidateRevision") != candidate_revision):
        return False
    try:
        manifest = _read_json(os.path.join(workspace, "manifest.json"),
                              MAX_MANIFEST_BYTES, "shader workspace manifest")
    except (ShaderConsoleError, OSError):
        return False
    candidate = manifest.get("latestCompiled")
    abi = candidate.get("abiGate") if isinstance(candidate, dict) else None
    current_candidate = bool(
        isinstance(candidate, dict) and
        candidate.get("sourceSha256") == source_sha and
        candidate.get("revision") == candidate_revision and
        manifest.get("revision") == candidate_revision and
        candidate.get("rawSha256") == live.get("activeCandidateRawSha256") and
        SHA256_RE.fullmatch(str(candidate.get("rawSha256") or "")) is not None)
    if not current_candidate:
        return False
    reused = live.get("reusedApplyEvidence")
    if (isinstance(reused, dict) and
            reused.get("revision") == candidate_revision and
            reused.get("candidateRawSha256") == candidate.get("rawSha256") and
            reused.get("evidenceReusedFromRevision") == live.get("activeRevision")):
        # The Host observed the exact executable already applied in this live
        # session. This is inherited ABI evidence, not a new reflection check.
        return True
    return bool(
        isinstance(abi, dict) and
        candidate.get("replayAbiValidated") is True and
        candidate.get("abiCompatible") is True and
        candidate.get("requiresReplayAbiValidation") is False and
        abi.get("status") == "compatible" and
        SHA256_RE.fullmatch(str(abi.get("requestId") or "")) is not None and
        SHA256_RE.fullmatch(str(abi.get("reportSha256") or "")) is not None
    )


def _authorized_workspace(registry_path, authorization_id, workspace_id,
                          capture_binding=None):
    """Resolve a durable live-edit identity or one bounded view-only draft.

    The editor never accepts a path selected from the tree as authority. Every
    open and save re-opens the fixed registry, validates its bounded schema,
    resolves the two opaque identities, and then revalidates the workspace
    manifest before touching source bytes.
    """
    path = os.path.abspath(str(registry_path or default_registry_path() or ""))
    requested_authorization = str(authorization_id or "").lower()
    requested_workspace = str(workspace_id or "").lower()
    if (TOKEN_RE.fullmatch(requested_authorization) is None or
            TOKEN_RE.fullmatch(requested_workspace) is None):
        raise ShaderConsoleError("shader editor workspace identity is invalid")
    entries = []
    registry_error = None
    if path and os.path.isfile(path):
        try:
            registry = _read_json(path, MAX_REGISTRY_BYTES, "live-edit authorization registry")
            if registry.get("schema") != REGISTRY_SCHEMA or registry.get("schemaVersion") != 1:
                raise ShaderConsoleError("live-edit authorization registry schema is invalid")
            entries = registry.get("entries")
            if not isinstance(entries, list) or len(entries) > MAX_AUTHORIZATIONS:
                raise ShaderConsoleError("live-edit authorization registry entries are invalid")
        except ShaderConsoleError as exc:
            registry_error = exc
            entries = []

    # Select by inert scalar IDs before resolving any workspace path. An old,
    # unrelated authorization whose workspace was deleted must not deny access
    # to the exact requested authorization; only the selected entry gains the
    # authority to touch disk and is then validated in full.
    matching_raw = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        raw_identity = raw.get("workspaceIdentity")
        raw_authorization = str(raw.get("authorizationId", "") or "").lower()
        raw_workspace = (str(raw_identity.get("workspaceId", "") or "").lower()
                         if isinstance(raw_identity, dict) else "")
        if (raw_authorization == requested_authorization and
                raw_workspace == requested_workspace):
            matching_raw.append(raw)
    if len(matching_raw) == 1:
        authorization = _validate_authorization(matching_raw[0])
    elif not matching_raw and capture_binding is not None:
        project_drafts, unused_paths = _managed_project_draft_authorizations(
            capture_binding)
        matches = [item for item in project_drafts
                   if item["authorizationId"] == requested_authorization
                   and item["workspaceId"] == requested_workspace]
        if len(matches) != 1:
            if registry_error is not None:
                raise registry_error
            raise ShaderConsoleError("shader editor workspace authorization is unavailable or ambiguous")
        authorization = matches[0]
    else:
        if registry_error is not None:
            raise registry_error
        raise ShaderConsoleError("shader editor workspace authorization is unavailable or ambiguous")
    source_path, compiler_diagnostics, manifest_path = _validate_manifest(
        authorization["workspacePath"], authorization)
    if SOURCE_EXTENSION_RE.search(source_path) is None:
        raise ShaderConsoleError("authorized workspace source language is unsupported")
    organization_gate = (_managed_source_gate(source_path)
                         if authorization.get("viewOnlySource") else
                         _validate_editable_source_gate(
                             authorization["workspacePath"], authorization, source_path))
    if (authorization.get("viewOnlyDraft") and
            organization_gate.get("kind") not in (
                "editable-draft", "organization-source-preview", "managed-source")):
        raise ShaderConsoleError("view-only draft no longer matches current source")
    return (authorization, source_path, compiler_diagnostics, manifest_path,
            organization_gate)


def _managed_source_gate(source_path):
    """Describe presentation bytes only; this is never a publication gate."""
    raw = _read_file_bytes(source_path, MAX_SOURCE_BYTES, "managed shader source")
    if not raw or b"\x00" in raw:
        raise ShaderConsoleError("managed shader source is empty or contains NUL")
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ShaderConsoleError("managed shader source is not valid UTF-8") from exc
    return {"kind": "managed-source", "organizationId": "",
            "finalSourceSha256": hashlib.sha256(raw).hexdigest()}


def normalize_capture_binding(value):
    """Normalize a current UI capture scope; absence never selects old data."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
            "instanceId", "captureSessionId", "capturePath"}:
        raise ShaderConsoleError("shader workspace capture binding is invalid")
    instance = value.get("instanceId")
    session = value.get("captureSessionId")
    path = value.get("capturePath")
    if (not isinstance(instance, str) or
            re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", instance) is None or
            instance in (".", "..") or not isinstance(session, str) or
            EXACT_TOKEN_RE.fullmatch(session) is None or
            not isinstance(path, str) or not path or len(path) > 32768 or
            "\x00" in path or not os.path.isabs(path)):
        raise ShaderConsoleError("shader workspace capture binding is invalid")
    return {"instanceId": instance, "captureSessionId": session,
            "capturePath": _normal_path(path)}


def _authorized_capture_live(authorization, capture_binding):
    binding = normalize_capture_binding(capture_binding)
    if binding is None or not _same_path(
            authorization["capturePath"], binding["capturePath"]):
        raise ShaderConsoleError("shader workspace does not belong to the current capture")
    live_id = authorization["lastLiveEditId"]
    if EXACT_TOKEN_RE.fullmatch(live_id) is None:
        raise ShaderConsoleError("shader workspace has no current live-edit session")
    workspace = authorization["workspacePath"]
    live_root = os.path.join(workspace, "live-edit")
    path = _regular_file_in_tree(
        live_root, os.path.join(live_root, live_id + ".json"),
        MAX_AUDIT_BYTES, "authorized shader live-edit audit")
    live = _validate_live(
        _read_json(path, MAX_AUDIT_BYTES, "authorized shader live-edit audit"),
        workspace, live_id)
    if live.get("captureBinding") != binding:
        raise ShaderConsoleError("shader workspace belongs to another capture session")
    return live


def _require_running_live_edit(authorization, source_path):
    """Require the exact authorized watcher/live pair to be running."""
    live_edit_id = str(authorization.get("lastLiveEditId", "") or "").lower()
    if EXACT_TOKEN_RE.fullmatch(live_edit_id) is None:
        raise ShaderConsoleError("authorized live-edit session is not running")
    workspace = authorization["workspacePath"]
    live_root = os.path.join(workspace, "live-edit")
    live_path = _regular_file_in_tree(
        live_root, os.path.join(live_root, live_edit_id + ".json"),
        MAX_AUDIT_BYTES, "authorized shader live-edit audit")
    live_value = _read_json(
        live_path, MAX_AUDIT_BYTES, "authorized shader live-edit audit")
    live = _validate_live(live_value, workspace, live_edit_id)
    if live.get("state") != "running":
        raise ShaderConsoleError("authorized live-edit session is not running")
    watch_id = str(live.get("watchId", "") or "").lower()
    watch_root = os.path.join(workspace, "watch")
    watch_path = _regular_file_in_tree(
        watch_root, os.path.join(watch_root, watch_id + ".json"),
        MAX_AUDIT_BYTES, "authorized shader watcher audit")
    watch_value = _read_json(
        watch_path, MAX_AUDIT_BYTES, "authorized shader watcher audit")
    watch = _validate_watch(watch_value, workspace, source_path, watch_id)
    if watch.get("state") != "running":
        raise ShaderConsoleError("authorized shader watcher is not running")
    return live, watch


def load_authorized_source_document(registry_path, authorization_id, workspace_id,
                                    capture_binding=None):
    """Read one HLSL document bound to this GUI capture, without granting writes."""
    binding = normalize_capture_binding(capture_binding)
    if binding is None:
        raise ShaderConsoleError("shader workspace requires a current capture")
    authorization, source_path, _diagnostics, _manifest_path, gate = _authorized_workspace(
        registry_path, authorization_id, workspace_id, binding)
    draft = gate.get("kind") == "editable-draft"
    managed_source = gate.get("kind") == "managed-source"
    readonly_preview = gate.get("kind") in ("organization-source-preview", "managed-source")
    if readonly_preview or draft:
        if not _same_path(authorization["capturePath"], binding["capturePath"]):
            raise ShaderConsoleError("shader workspace does not belong to the current capture")
        live = None
        try:
            candidate_live = _authorized_capture_live(authorization, binding)
            watch_id = candidate_live["watchId"]
            watch_path = _regular_file_in_tree(
                os.path.join(authorization["workspacePath"], "watch"),
                os.path.join(authorization["workspacePath"], "watch", watch_id + ".json"),
                MAX_AUDIT_BYTES, "authorized shader watcher audit")
            candidate_watch = _validate_watch(
                _read_json(watch_path, MAX_AUDIT_BYTES, "authorized shader watcher audit"),
                authorization["workspacePath"], source_path, watch_id)
            # Reading saved source does not require a successful compile or
            # replacement. Retain the current session identity even after a
            # compile/apply failure so its source can be opened and repaired.
            if candidate_live.get("watchId") == candidate_watch.get("watchId"):
                live = candidate_live
        except (ShaderConsoleError, OSError):
            pass
    else:
        live = _authorized_capture_live(authorization, binding)
    raw = _read_file_bytes(source_path, MAX_SOURCE_BYTES, "workspace shader source")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShaderConsoleError("workspace shader source is not valid UTF-8: %s" %
                                 _bounded(exc, 512))
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    if raw_sha256 != gate["finalSourceSha256"]:
        if draft or readonly_preview:
            raise ShaderConsoleError("shader organization editable source changed")
        _live, watch = _require_running_live_edit(authorization, source_path)
        # A restarted authorised watcher begins at the user's latest source,
        # which may already differ from the delivered asset. The running
        # Host-owned watch must observe these exact bytes; its initial hash
        # need not equal the original AI/project version on every restart.
        if watch.get("observedSourceSha256") != raw_sha256:
            raise ShaderConsoleError(
                "workspace shader source is not in the authorized organization/edit lineage")
    info = os.stat(source_path)
    if draft or readonly_preview:
        current_gate = (_managed_source_gate(source_path) if managed_source else
                        _validate_editable_source_gate(
                            authorization["workspacePath"], authorization, source_path))
        if (current_gate.get("kind") != gate.get("kind") or
                current_gate.get("organizationId") != gate.get("organizationId") or
                current_gate.get("finalSourceSha256") != raw_sha256):
            raise ShaderConsoleError("shader organization draft changed while its source was read")
        if live is not None:
            confirmed_live = _authorized_capture_live(authorization, binding)
            if (confirmed_live["liveEditId"] != live["liveEditId"] or
                    confirmed_live["watchId"] != live["watchId"]):
                raise ShaderConsoleError("shader workspace session changed while its source was read")
    else:
        confirmed_live = _authorized_capture_live(authorization, binding)
        if (confirmed_live["liveEditId"] != live["liveEditId"] or
                confirmed_live["watchId"] != live["watchId"]):
            raise ShaderConsoleError("shader workspace session changed while its source was read")
    return {
        "captureBinding": binding,
        "liveEditId": live["liveEditId"] if live else None,
        "watchId": live["watchId"] if live else None,
        "authorizationId": authorization["authorizationId"],
        "workspaceId": authorization["workspaceId"],
        "workspacePath": authorization["workspacePath"],
        "sourcePath": source_path,
        "fileName": os.path.basename(source_path),
        "stage": authorization["stage"],
        "entryPoint": authorization["entryPoint"],
        "resourceId": authorization["resourceId"],
        "text": text,
        "rawSha256": raw_sha256,
        "unvalidatedDraft": draft or managed_source,
        "readOnly": False,
        "sourceEditable": True,
        "size": len(raw),
        "modifiedNs": getattr(info, "st_mtime_ns", int(info.st_mtime * 1000000000)),
    }


def _load_saved_organization_preview(workspace, manifest, gate_path, gate,
                                     organization_id, source_sha256):
    """Bind a read-only preview to the Host's canonical source-save receipt."""
    source = manifest.get("source")
    capture = manifest["capture"]
    shader = manifest["shader"]
    if not isinstance(source, dict):
        raise ShaderConsoleError("organization draft source identity is invalid")
    route = manifest.get("routePlan")
    if route is not None and not isinstance(route, dict):
        raise ShaderConsoleError("organization draft source route is invalid")
    source_format = str((route or {}).get("editableFormat") or "hlsl").lower()
    source_path = _safe_relative(
        workspace, source.get("path"), "organization draft canonical source")
    source_root = os.path.join(workspace, "source")
    if (source_format not in ("hlsl", "glsl") or
            source.get("encoding") != "utf-8" or
            source.get("lane") != "executable-editable" or
            source.get("compileEligible") is not True or
            not _within(source_root, source_path) or
            os.path.basename(source_path) != "shader." + source_format):
        raise ShaderConsoleError("organization draft canonical source path is invalid")
    source_path = _regular_file_in_tree(
        source_root, source_path, MAX_SOURCE_BYTES,
        "organization draft canonical source")
    shader_raw_sha256 = str(shader.get("rawSha256") or "").lower()
    accepted = gate.get("decision") == "accepted-executable-editable"
    gate_source_sha256 = gate.get(
        "finalSourceSha256" if accepted else "proposalSourceSha256")
    if (SHA256_RE.fullmatch(shader_raw_sha256) is None or
            SHA256_RE.fullmatch(str(capture.get("captureId") or "")) is None or
            shader.get("stage") != manifest.get("stage") or
            shader.get("entryPoint") != manifest.get("entryPoint") or
            gate.get("organizationId") != organization_id or
            gate_source_sha256 != source_sha256):
        raise ShaderConsoleError("organization draft preview receipt is inconsistent")
    identity = {
        "workspaceId": str(manifest["workspaceId"]).lower(),
        "captureId": str(capture.get("captureId") or "").lower(),
        "stage": manifest.get("stage"),
        "entryPoint": manifest.get("entryPoint"),
        "shaderRawSha256": shader_raw_sha256,
    }
    def validate_source_receipt():
        if not accepted:
            return _validate_current_draft_gate(
                workspace, identity, source_path, gate_path, gate)
        final_binding = gate.get("finalBinding")
        if (gate.get("schema") != ORGANIZATION_GATE_SCHEMA or
                gate.get("schemaVersion") != 1 or
                gate.get("requestedTransformation") != "organize-only" or
                gate.get("organizedSourcePromoted") is not True or
                gate.get("replayApplied") is not False or
                gate.get("semanticEquivalenceVerified") is not False or
                not isinstance(final_binding, dict) or
                final_binding.get("complete") is not True or
                final_binding.get("sourceSha256") != source_sha256 or
                any(gate.get(key) != identity[key] for key in (
                    "workspaceId", "captureId", "stage", "entryPoint"))):
            raise ShaderConsoleError("organization finalized source receipt is inconsistent")
        return _validate_organization_source_receipt(workspace, source_path, gate)

    validate_source_receipt()
    raw = _read_file_bytes(
        source_path, MAX_SOURCE_BYTES, "organization draft canonical source")
    if not raw or hashlib.sha256(raw).hexdigest() != source_sha256:
        raise ShaderConsoleError("organization draft canonical source bytes changed")
    # Re-read the save metadata and its session/artifact bindings after reading
    # canonical bytes. A concurrent save must not turn an earlier receipt into
    # proof of a different draft, even when the workspace identity is unchanged.
    if (_read_json(os.path.join(workspace, "manifest.json"), MAX_MANIFEST_BYTES,
                   "shader workspace manifest") != manifest or
            _read_json(gate_path, MAX_MANIFEST_BYTES,
                       "organization draft save receipt") != gate):
        raise ShaderConsoleError("organization draft preview changed while it was read")
    validate_source_receipt()
    if hashlib.sha256(_read_file_bytes(
            source_path, MAX_SOURCE_BYTES,
            "organization draft canonical source")).hexdigest() != source_sha256:
        raise ShaderConsoleError("organization draft canonical source bytes changed")
    return source_path, raw, {
        "sourceFormat": source_format,
        "sourceByteSize": len(raw),
        "validationTier": "saved-source",
        "completedChecks": ["source-save"],
        "deferredFinalChecks": ["compile", "replay-abi", "equivalence"],
        "canonicalSourceUpdated": True,
    }


def load_related_shader_source_document(metadata, capture_binding=None, refresh=False):
    """Read the exact Host-authorized include working copy, never another Shader."""
    binding = normalize_capture_binding(capture_binding)
    if binding is None or metadata.get("schema") != "bread-shader-source-document/v1":
        raise ShaderConsoleError("Shader include document identity is invalid")
    workspace = _directory(metadata.get("workspacePath"), "Shader include workspace")
    document_id = str(metadata.get("sourceDocumentId") or "")
    if SHA256_RE.fullmatch(document_id) is None:
        raise ShaderConsoleError("Shader include document ID is invalid")
    receipt_path = _regular_file_in_tree(
        workspace, os.path.join(workspace, "ai", "source-documents", document_id + ".json"),
        MAX_MANIFEST_BYTES, "Shader include receipt")
    receipt = _read_json(receipt_path, MAX_MANIFEST_BYTES, "Shader include receipt")
    if (receipt.get("schema") != metadata.get("schema") or
            receipt.get("sourceDocumentId") != document_id or
            receipt.get("sourceWorkspaceId") != metadata.get("sourceWorkspaceId")):
        raise ShaderConsoleError("Shader include receipt identity changed")
    source_path = _safe_relative(workspace, receipt.get("sourceRelativePath"), "Shader include path")
    source_path = _regular_file_in_tree(os.path.join(workspace, "source"), source_path,
                                        MAX_SOURCE_BYTES, "Shader include source")
    if (_normal_path(source_path) != _normal_path(metadata.get("sourcePath", "")) or
            hashlib.sha256((receipt["sourceWorkspaceId"] + "\0" +
                            os.path.normcase(os.path.abspath(source_path))).encode("utf-8")).hexdigest() != document_id):
        raise ShaderConsoleError("Shader include path identity changed")
    manifest_path = _regular_file_in_tree(workspace, os.path.join(workspace, "manifest.json"),
                                          MAX_MANIFEST_BYTES, "Shader workspace manifest")
    manifest = _read_json(manifest_path, MAX_MANIFEST_BYTES, "Shader workspace manifest")
    if manifest.get("workspaceId") != receipt["sourceWorkspaceId"]:
        raise ShaderConsoleError("Shader include owner changed")
    root_path = _safe_relative(workspace, (manifest.get("source") or {}).get("path"), "Shader root source")
    root_path = _regular_file_in_tree(os.path.join(workspace, "source"), root_path,
                                      MAX_SOURCE_BYTES, "Shader root source")
    root_sha = hashlib.sha256(_read_file_bytes(root_path, MAX_SOURCE_BYTES, "Shader root source")).hexdigest()
    root = load_saved_shader_source_preview(workspace, root_sha, capture_binding=binding)
    if _normal_path(root_path) == _normal_path(source_path):
        raise ShaderConsoleError("Shader include cannot replace its root source")
    before = os.stat(source_path)
    raw = _read_file_bytes(source_path, MAX_SOURCE_BYTES, "Shader include source")
    after = os.stat(source_path)
    source_sha = hashlib.sha256(raw).hexdigest()
    if (b"\0" in raw or before.st_mtime_ns != after.st_mtime_ns or
            before.st_size != after.st_size or len(raw) != after.st_size or
            (not refresh and source_sha != metadata.get("rawSha256")) or
            _read_json(receipt_path, MAX_MANIFEST_BYTES, "Shader include receipt") != receipt):
        raise ShaderConsoleError("Shader include source changed while it was read")
    result = dict(metadata)
    result.update({key: root[key] for key in ("captureId", "capturePath", "stage", "entryPoint", "resourceId")})
    result.update(workspaceId=document_id, sourceWorkspaceId=receipt["sourceWorkspaceId"],
                  workspacePath=workspace, sourcePath=source_path, fileName=os.path.basename(source_path),
                  captureBinding=binding, documentKind="source-include", readOnly=False,
                  sourceEditable=True, sourceOnly=True, compileScheduled=False,
                  originPath=receipt["originPath"], sourceRelativePath=receipt["sourceRelativePath"],
                  rootSourcePath=root_path, rootSourceSha256=root_sha,
                  mutationPaths=receipt.get("mutationPaths", []), receiptPath=receipt_path,
                  text=raw.decode("utf-8"), rawSha256=source_sha, size=len(raw), modifiedNs=after.st_mtime_ns)
    return result


def reload_saved_shader_source_preview(previous, capture_binding=None):
    """Refresh a displayed saved-source buffer on the file worker, never write it."""
    workspace = _directory(previous.get("workspacePath"), "saved source workspace")
    manifest_path = _regular_file_in_tree(
        workspace, os.path.join(workspace, "manifest.json"),
        MAX_MANIFEST_BYTES, "shader workspace manifest")
    manifest = _read_json(manifest_path, MAX_MANIFEST_BYTES, "shader workspace manifest")
    if manifest.get("workspaceId") != previous.get("sourceWorkspaceId"):
        raise ShaderConsoleError("saved source workspace identity changed")
    source_path = _safe_relative(workspace, (manifest.get("source") or {}).get("path"),
                                 "saved canonical source")
    source = _regular_file_in_tree(
        os.path.join(workspace, "source"), source_path,
        MAX_SOURCE_BYTES, "saved canonical source")
    raw = _read_file_bytes(source, MAX_SOURCE_BYTES, "saved canonical source")
    opened = load_saved_shader_source_preview(
        workspace, hashlib.sha256(raw).hexdigest(), capture_binding=capture_binding)
    display_binding = previous.get("projectSourceBinding")
    if isinstance(display_binding, dict):
        # A source edit keeps its editor identity but is no longer the exact
        # restored project head. Never relabel changed bytes as that head.
        if opened["rawSha256"] == display_binding.get("sourceSha256"):
            opened = bind_project_shader_source_display(opened, display_binding)
        elif previous.get("captureBinding") == opened.get("captureBinding"):
            # The user edited this same source file. Retain its known physical
            # display location, but drop the old project-head/source claim.
            opened["sourceDisplayBinding"] = {key: display_binding[key] for key in (
                "captureId", "capturePath", "captureSessionId", "resourceId", "anchorEventId",
                "stage", "entryPoint", "originalRawSha256")}
    physical = opened.get("sourceDisplayBinding") or previous.get("sourceDisplayBinding")
    if (isinstance(physical, dict) and
            physical.get("captureSessionId") == (opened.get("captureBinding") or {}).get("captureSessionId") and
            _same_path(physical.get("capturePath", ""), (opened.get("captureBinding") or {}).get("capturePath", "")) and
            all(physical.get(left) == opened.get(right) for left, right in (
                ("originalRawSha256", "shaderRawSha256"), ("stage", "stage"), ("entryPoint", "entryPoint")))):
        opened.update({key: physical[key] for key in ("captureId", "capturePath", "resourceId")})
        opened["eventIds"] = [physical["anchorEventId"]]
        opened["sourceDisplayBinding"] = dict(physical)
    return opened


def load_saved_shader_source_preview(workspace_path, expected_source_sha256,
                                     capture_binding=None):
    """Read exact canonical Source without treating draft state as authority.

    This supplies presentation bytes only. Compile/organization receipts and
    live-edit authorization remain separate, and the source's capture need
    not be the capture whose UI currently displays the document. The receipt is
    immutable input; saving an editable working copy uses the Host's draft CAS route.
    """
    binding = normalize_capture_binding(capture_binding)
    if binding is None:
        raise ShaderConsoleError("saved source display requires a current capture")
    workspace = _directory(workspace_path, "saved source workspace")
    source_sha256 = str(expected_source_sha256 or "").lower()
    if SHA256_RE.fullmatch(source_sha256) is None:
        raise ShaderConsoleError("saved source hash is invalid")
    manifest_path = _regular_file_in_tree(
        workspace, os.path.join(workspace, "manifest.json"),
        MAX_MANIFEST_BYTES, "shader workspace manifest")

    def source_identity(manifest):
        capture = manifest.get("capture")
        shader = manifest.get("shader")
        source = manifest.get("source")
        if (manifest.get("schema") != WORKSPACE_SCHEMA or
                manifest.get("schemaVersion") != 1 or
                EXACT_TOKEN_RE.fullmatch(str(manifest.get("workspaceId", ""))) is None or
                not isinstance(capture, dict) or not isinstance(shader, dict) or
                not isinstance(source, dict) or source.get("encoding") != "utf-8" or
                SHA256_RE.fullmatch(str(capture.get("captureId") or "")) is None or
                SHA256_RE.fullmatch(str(shader.get("rawSha256") or "")) is None or
                shader.get("stage") != manifest.get("stage") or
                shader.get("entryPoint") != manifest.get("entryPoint")):
            raise ShaderConsoleError("saved source workspace identity is invalid")
        resource_id = _positive_integer(shader.get("resourceId"), 0, 0xFFFFFFFFFFFFFFFF)
        if not resource_id:
            raise ShaderConsoleError("saved source Shader identity is invalid")
        source_path = _safe_relative(workspace, source.get("path"), "saved source path")
        source_path = _regular_file_in_tree(
            os.path.join(workspace, "source"), source_path,
            MAX_SOURCE_BYTES, "saved canonical source")
        # Execution metadata, organization IDs and validation state can change
        # independently. Only presentation identity and actual bytes matter.
        return {
            "workspaceId": manifest["workspaceId"],
            "captureId": capture["captureId"],
            "capturePath": str(capture.get("capturePath") or ""),
            "stage": manifest.get("stage"),
            "entryPoint": manifest.get("entryPoint"),
            "shaderRawSha256": shader["rawSha256"],
            "resourceId": resource_id,
            "sourcePath": source_path,
        }

    manifest = _read_json(manifest_path, MAX_MANIFEST_BYTES, "shader workspace manifest")
    identity = source_identity(manifest)
    source_path = identity["sourcePath"]
    raw = _read_file_bytes(source_path, MAX_SOURCE_BYTES, "saved canonical source")
    if not raw or hashlib.sha256(raw).hexdigest() != source_sha256:
        raise ShaderConsoleError("saved canonical source bytes changed")
    if b"\x00" in raw:
        raise ShaderConsoleError("saved canonical source contains NUL")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShaderConsoleError("saved canonical source is not valid UTF-8: %s" % _bounded(exc, 512))
    if (source_identity(_read_json(manifest_path, MAX_MANIFEST_BYTES,
                                  "shader workspace manifest")) != identity or
            hashlib.sha256(_read_file_bytes(source_path, MAX_SOURCE_BYTES,
                                           "saved canonical source")).hexdigest() != source_sha256):
        raise ShaderConsoleError("saved canonical source changed while it was read")
    info = os.stat(source_path)
    preview_id = hashlib.sha256(
        ("saved-workspace-source\n" + identity["workspaceId"]).encode("utf-8")).hexdigest()
    result = {
        "captureBinding": binding,
        "workspaceId": preview_id,
        "sourceWorkspaceId": identity["workspaceId"],
        "workspacePath": workspace,
        "organizationId": "",
        "sourceKind": "saved-source",
        "documentKind": "organization-preview",
        "readOnly": True,
        "canonicalSourceUpdated": True,
        "replayApplied": False,
        "sourcePath": source_path,
        "fileName": os.path.basename(source_path),
        "rootSourcePath": source_path,
        "captureId": identity["captureId"],
        "capturePath": identity["capturePath"],
        "stage": identity["stage"],
        "entryPoint": identity["entryPoint"],
        "resourceId": identity["resourceId"],
        "shaderRawSha256": identity["shaderRawSha256"],
        "projectSourceIdentity": _project_source_document_identity(workspace, manifest, source_sha256),
        "validationTier": "saved-source",
        "completedChecks": ["source-read"],
        "deferredFinalChecks": ["compile", "replay-abi", "equivalence"],
        "text": text,
        "rawSha256": source_sha256,
        "size": len(raw),
        "modifiedNs": getattr(info, "st_mtime_ns", int(info.st_mtime * 1000000000)),
        "receiptPath": manifest_path,
    }
    result.update(_load_preview_navigation(workspace, manifest, source_sha256,
                                           preview_id, source_path))
    return result


def load_verified_organization_preview(workspace_path, expected_organization_id,
                                       expected_source_sha256,
                                       capture_binding=None):
    """Read a Host-saved or compile-verified draft as a read-only document.

    This path deliberately does not consult or mint live-edit authorization.
    The fixed save/preview receipt, exact source and workspace manifest must
    all describe the same currently open capture. A source-save receipt proves
    saved bytes only; it never implies compile, ABI or replacement success.
    """

    binding = normalize_capture_binding(capture_binding)
    if binding is None:
        raise ShaderConsoleError("organization preview requires a current capture")
    workspace = os.path.abspath(str(workspace_path or ""))
    if not workspace or not os.path.isdir(workspace) or _is_reparse(workspace):
        raise ShaderConsoleError("organization preview workspace is unavailable")
    organization_id = str(expected_organization_id or "").lower()
    source_sha256 = str(expected_source_sha256 or "").lower()
    if EXACT_TOKEN_RE.fullmatch(organization_id) is None:
        raise ShaderConsoleError("organization preview ID is invalid")
    if SHA256_RE.fullmatch(source_sha256) is None:
        raise ShaderConsoleError("organization preview source hash is invalid")

    manifest_path = _regular_file_in_tree(
        workspace, os.path.join(workspace, "manifest.json"),
        MAX_MANIFEST_BYTES, "shader workspace manifest")
    manifest = _read_json(
        manifest_path, MAX_MANIFEST_BYTES, "shader workspace manifest")
    capture = manifest.get("capture")
    shader = manifest.get("shader")
    if (manifest.get("schema") != WORKSPACE_SCHEMA or
            manifest.get("schemaVersion") != 1 or
            EXACT_TOKEN_RE.fullmatch(str(manifest.get("workspaceId", "")).lower()) is None or
            not isinstance(capture, dict) or not isinstance(shader, dict)):
        raise ShaderConsoleError("shader workspace manifest identity is invalid")

    if (_normal_path(str(binding.get("capturePath") or "")) !=
            _normal_path(str(capture.get("capturePath") or ""))):
        raise ShaderConsoleError("organization preview belongs to another capture")
    gate_path = os.path.join(workspace, "ai", "organize-code-gate.json")
    draft_gate = None
    if os.path.isfile(gate_path):
        try:
            gate_path = _regular_file_in_tree(
                workspace, gate_path, MAX_MANIFEST_BYTES,
                "organization draft save receipt")
            observed_gate = _read_json(
                gate_path, MAX_MANIFEST_BYTES, "organization draft save receipt")
        except ShaderConsoleError:
            # A compile preview has its own content-bound receipt. Unrelated
            # or broken save metadata must not veto that existing capability.
            observed_gate = {}
        gate_decision = observed_gate.get("decision")
        gate_source_sha256 = observed_gate.get(
            "finalSourceSha256" if gate_decision == "accepted-executable-editable"
            else "proposalSourceSha256")
        if (gate_decision in ("editable-draft", "accepted-executable-editable") and
                observed_gate.get("organizationId") == organization_id and
                gate_source_sha256 == source_sha256):
            draft_gate = observed_gate
    if draft_gate is not None:
        source_path, raw, receipt = _load_saved_organization_preview(
            workspace, manifest, gate_path, draft_gate,
            organization_id, source_sha256)
        receipt_path = gate_path
        completed = receipt["completedChecks"]
        deferred = receipt["deferredFinalChecks"]
    else:
        receipt_path, source_path, raw, receipt = _load_compiled_organization_preview(
            workspace, manifest, organization_id, source_sha256)
        completed = receipt["completedChecks"]
        deferred = receipt["deferredFinalChecks"]
    if b"\x00" in raw:
        raise ShaderConsoleError("organization preview source contains NUL")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShaderConsoleError(
            "organization preview source is not valid UTF-8: %s" %
            _bounded(exc, 512))
    # RenderDoc ResourceId values are unsigned 64-bit identities and commonly
    # exceed the generic 31-bit UI/count bound used by _positive_integer.
    resource_id = _positive_integer(
        shader.get("resourceId"), 0, 0x7FFFFFFFFFFFFFFF)
    if resource_id <= 0:
        raise ShaderConsoleError("organization preview Shader identity is invalid")
    info = os.stat(source_path)
    preview_id = hashlib.sha256(
        ("organization-preview\n" + str(manifest["workspaceId"]) + "\n" +
         organization_id).encode("utf-8")).hexdigest()
    root_source_path = source_path
    if isinstance(manifest.get("source"), dict) and manifest["source"].get("path"):
        root_source_path = _safe_relative(
            workspace, manifest["source"]["path"], "organization canonical source path")
        root_source_path = _regular_file_in_tree(
            os.path.join(workspace, "source"), root_source_path,
            MAX_SOURCE_BYTES, "organization canonical source")
    result = {
        "captureBinding": binding,
        "workspaceId": preview_id,
        "sourceWorkspaceId": str(manifest["workspaceId"]).lower(),
        "workspacePath": workspace,
        "organizationId": organization_id,
        "documentKind": "organization-preview",
        "readOnly": True,
        "canonicalSourceUpdated": receipt.get("canonicalSourceUpdated") is True,
        "replayApplied": False,
        "sourcePath": source_path,
        "rootSourcePath": root_source_path,
        "fileName": os.path.basename(source_path),
        "captureId": str(capture.get("captureId", "")).lower(),
        "capturePath": str(capture.get("capturePath") or ""),
        "stage": str(manifest.get("stage") or ""),
        "entryPoint": str(manifest.get("entryPoint") or ""),
        "resourceId": resource_id,
        "shaderRawSha256": str(shader.get("rawSha256") or ""),
        "projectSourceIdentity": _project_source_document_identity(workspace, manifest, source_sha256),
        "validationTier": str(receipt.get("validationTier") or ""),
        "completedChecks": list(completed),
        "deferredFinalChecks": list(deferred),
        "text": text,
        "rawSha256": source_sha256,
        "size": len(raw),
        "modifiedNs": getattr(info, "st_mtime_ns", int(info.st_mtime * 1000000000)),
        "receiptPath": receipt_path,
    }
    result.update(_load_preview_navigation(workspace, manifest, source_sha256,
                                           preview_id, source_path))
    return result


def bind_project_shader_source_display(opened, display_binding):
    """Bind an exact saved project source to its verified current-capture use.

    This changes only presentation metadata. The original file and workspace
    remain the source-only CAS target; no replay or edit lease is minted.
    """
    expected_fields = {"schema", "projectId", "knowledgeId", "sourceSha256", "captureId",
                       "capturePath", "captureSessionId", "resourceId", "anchorEventId",
                       "stage", "entryPoint", "originalRawSha256"}
    binding = opened.get("captureBinding") or {}
    if (not isinstance(display_binding, dict) or set(display_binding) not in (
            expected_fields, expected_fields | {"replaySource"}) or
            display_binding.get("schema") != "replaylab-project-shader-display-binding/v1" or
            display_binding.get("captureSessionId") != binding.get("captureSessionId") or
            not _same_path(display_binding.get("capturePath", ""), binding.get("capturePath", "")) or
            SHA256_RE.fullmatch(str(display_binding.get("captureId") or "")) is None or
            type(display_binding.get("resourceId")) is not int or
            not 0 < display_binding["resourceId"] <= 0xFFFFFFFFFFFFFFFF or
            type(display_binding.get("anchorEventId")) is not int or
            not 0 < display_binding["anchorEventId"] <= 0xFFFFFFFF or
            any(display_binding.get(left) != opened.get(right) for left, right in (
                ("sourceSha256", "rawSha256"), ("originalRawSha256", "shaderRawSha256"),
                ("stage", "stage"), ("entryPoint", "entryPoint")))):
        raise ShaderConsoleError("project source display binding changed")
    library = _project_library_for_console(default_active_project_selection_path())
    documents = [item for item in library.get("documents", [])
                 if item.get("knowledgeId") == display_binding.get("knowledgeId")]
    if (library.get("projectId") != display_binding.get("projectId") or len(documents) != 1 or
            not _within(os.path.join(library["projectRoot"], *ACTIVE_PROJECT_LAYOUT["shaderWorkspaces"].split("/")),
                        opened["workspacePath"]) or
            documents[0].get("isCurrentHead") is False or
            any(documents[0].get(left) != display_binding.get(right) for left, right in (
                ("sourceSha256", "sourceSha256"), ("rawShaderSha256", "originalRawSha256"),
                ("stage", "stage"), ("entryPoint", "entryPoint")))):
        raise ShaderConsoleError("project source display differs from the active project head")
    replay_source = display_binding.get("replaySource")
    if replay_source is not None:
        replay_documents = [item for item in library.get("documents", [])
                            if item.get("knowledgeId") == replay_source.get("knowledgeId")] if isinstance(replay_source, dict) else []
        if (not isinstance(replay_source, dict) or set(replay_source) != {"knowledgeId", "sourceSha256", "applied"} or
                replay_source.get("applied") is not True or len(replay_documents) != 1 or
                replay_documents[0].get("sourceSha256") != replay_source.get("sourceSha256") or
                any(replay_documents[0].get(left) != display_binding.get(right) for left, right in (
                    ("rawShaderSha256", "originalRawSha256"), ("stage", "stage"), ("entryPoint", "entryPoint")))):
            raise ShaderConsoleError("project source replay display binding changed")
    result = dict(opened)
    result["sourceCaptureId"] = opened["captureId"]
    result["sourceCapturePath"] = opened["capturePath"]
    result["sourceResourceId"] = opened["resourceId"]
    result.update({key: display_binding[key] for key in (
        "captureId", "capturePath", "resourceId", "stage", "entryPoint")})
    result["eventIds"] = [display_binding["anchorEventId"]]
    result["projectSourceBinding"] = dict(display_binding)
    result["projectSourceIdentity"] = {key: display_binding[key] for key in (
        "projectId", "knowledgeId", "originalRawSha256", "stage", "entryPoint", "sourceSha256")}
    return result


def _load_compiled_organization_preview(workspace, manifest, organization_id,
                                        source_sha256):
    """Retain the separate content-addressed compile-preview contract."""
    capture = manifest["capture"]
    receipt_path = _regular_file_in_tree(
        workspace, os.path.join(workspace, "ai", "organization-preview.json"),
        MAX_MANIFEST_BYTES, "organization preview receipt")
    receipt = _read_json(
        receipt_path, MAX_MANIFEST_BYTES, "organization preview receipt")
    source_format = str(receipt.get("sourceFormat") or "").lower()
    completed = receipt.get("completedChecks")
    deferred = receipt.get("deferredFinalChecks")
    if (receipt.get("schema") != ORGANIZATION_PREVIEW_SCHEMA or
            receipt.get("schemaVersion") != 1 or
            str(receipt.get("organizationId", "")).lower() != organization_id or
            str(receipt.get("workspaceId", "")).lower() !=
            str(manifest.get("workspaceId", "")).lower() or
            str(receipt.get("captureId", "")).lower() !=
            str(capture.get("captureId", "")).lower() or
            receipt.get("stage") != manifest.get("stage") or
            receipt.get("entryPoint") != manifest.get("entryPoint") or
            receipt.get("sourceSha256") != source_sha256 or
            source_format not in ("hlsl", "glsl") or
            type(receipt.get("sourceByteSize")) is not int or
            not 0 < receipt["sourceByteSize"] <= MAX_SOURCE_BYTES or
            not isinstance(completed, list) or completed != ["compile"] and
            completed != ["compile", "captured-output-fingerprint"] or
            not isinstance(deferred, list) or
            deferred != ["replay-abi", "equivalence"] or
            receipt.get("readOnly") is not True or
            receipt.get("promoted") is not False or
            receipt.get("canonicalSourceUpdated") is not False or
            receipt.get("replayAbiChecked") is not False or
            receipt.get("equivalenceChecked") is not False or
            receipt.get("replayApplied") is not False):
        raise ShaderConsoleError("organization preview receipt is inconsistent")

    preview_root = os.path.join(workspace, "ai", "organization-previews")
    source_path = _safe_relative(
        workspace, receipt.get("sourcePath"), "organization preview source")
    expected_name = source_sha256 + "." + source_format
    if (not _within(preview_root, source_path) or
            os.path.basename(source_path) != expected_name):
        raise ShaderConsoleError("organization preview source path is invalid")
    source_path = _regular_file_in_tree(
        preview_root, source_path, MAX_SOURCE_BYTES,
        "organization preview source")
    raw = _read_file_bytes(
        source_path, MAX_SOURCE_BYTES, "organization preview source")
    if (len(raw) != receipt["sourceByteSize"] or
            hashlib.sha256(raw).hexdigest() != source_sha256):
        raise ShaderConsoleError("organization preview source bytes changed")
    return receipt_path, source_path, raw, receipt


def save_authorized_source_document(registry_path, authorization_id, workspace_id,
                                    expected_raw_sha256, text):
    """Reject the retired embedded-process write path.

    Shader Workspace saves must cross the authenticated singleton Host and bind
    to its in-memory live-edit/watcher job. Keeping this fail-closed symbol
    makes older integrations receive an explicit error instead of silently
    falling back to an unaudited direct filesystem write.
    """
    del registry_path, authorization_id, workspace_id, expected_raw_sha256, text
    raise ShaderConsoleError(
        "direct shader source saves are disabled; use the running Bread Host")




def _latest_token_json(directory, preferred=""):
    if not os.path.isdir(directory) or _is_reparse(directory):
        return ""
    if preferred and TOKEN_RE.fullmatch(preferred):
        path = os.path.join(directory, preferred + ".json")
        if os.path.isfile(path) and not _is_reparse(path):
            return path
    candidates = []
    try:
        names = os.listdir(directory)
    except OSError:
        return ""
    if len(names) > MAX_AUTHORIZATIONS * 4:
        names = names[:MAX_AUTHORIZATIONS * 4]
    for name in names:
        stem, extension = os.path.splitext(name)
        if extension.lower() != ".json" or TOKEN_RE.fullmatch(stem) is None:
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or _is_reparse(path):
            continue
        try:
            candidates.append((os.path.getmtime(path), path))
        except OSError:
            continue
    return max(candidates)[1] if candidates else ""


def _validate_events(value, field):
    events = value.get("events", [])
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ShaderConsoleError("%s event list is invalid or too large" % field)
    clean = []
    previous = 0
    for raw in events:
        if not isinstance(raw, dict):
            raise ShaderConsoleError("%s contains a non-object event" % field)
        sequence = _positive_integer(raw.get("sequence"), 0)
        event_type = _bounded(raw.get("type", ""), 128)
        if sequence <= previous or not event_type:
            raise ShaderConsoleError("%s event identity/order is invalid" % field)
        previous = sequence
        # Preserve only scalar and fixed diagnostic display fields.  No command,
        # executable, or arguments from an audit become part of the UI model.
        event = {
            "sequence": sequence,
            "at": _bounded(raw.get("at", ""), 128),
            "type": event_type,
            "message": _bounded(raw.get("message", "")),
            "errorType": _bounded(raw.get("errorType", ""), 256),
            "revision": _positive_integer(raw.get("revision"), 0),
            "attempt": _positive_integer(raw.get("attempt"), 0),
            "replacementId": _bounded(raw.get("replacementId", ""), 128),
            "requestedSourceSha256": _bounded(
                raw.get("requestedSourceSha256", ""), 64).lower(),
        }
        diagnostic = raw.get("diagnostic")
        if isinstance(diagnostic, dict):
            event["diagnosticMessage"] = _bounded(diagnostic.get("message", ""))
            event["diagnosticCategory"] = _bounded(diagnostic.get("category", ""), 128)
            event["diagnosticPhase"] = _bounded(diagnostic.get("phase", ""), 128)
        event["diagnostics"] = []
        raw_diagnostics = raw.get("diagnostics")
        if isinstance(raw_diagnostics, list) and len(raw_diagnostics) <= 128:
            for item in raw_diagnostics:
                if not isinstance(item, dict):
                    continue
                severity = str(item.get("severity", "error") or "error").lower()
                if severity not in ("error", "warning", "note"):
                    severity = "error"
                line_value = item.get("line")
                column_value = item.get("column")
                event["diagnostics"].append({
                    "file": _bounded(item.get("file", ""), 4096),
                    "line": _positive_integer(line_value, 0, 100000000),
                    "column": _positive_integer(column_value, 0, 100000000),
                    "severity": severity,
                    "message": _bounded(item.get("message", ""), 4096),
                })
        clean.append(event)
    return clean


def _validate_watch(value, workspace, source_path, expected_watch_id=""):
    if value.get("schema") != WATCH_SCHEMA or value.get("schemaVersion") != 1:
        raise ShaderConsoleError("shader watcher audit schema is invalid")
    watch_id = str(value.get("watchId", "") or "").lower()
    if EXACT_TOKEN_RE.fullmatch(watch_id) is None or (
            expected_watch_id and watch_id != expected_watch_id):
        raise ShaderConsoleError("shader watcher audit identity is invalid")
    if not _same_path(value.get("workspacePath", ""), workspace):
        raise ShaderConsoleError("shader watcher audit changed workspace")
    if not _same_path(value.get("sourcePath", ""), source_path):
        raise ShaderConsoleError("shader watcher audit changed source file")
    initial_source_sha256 = str(
        value.get("initialSourceSha256", "") or "").lower()
    observed_source_sha256 = str(
        value.get("observedSourceSha256", "") or "").lower()
    if (SHA256_RE.fullmatch(initial_source_sha256) is None or
            SHA256_RE.fullmatch(observed_source_sha256) is None):
        raise ShaderConsoleError("shader watcher source lineage is invalid")
    return {
        "watchId": watch_id,
        "state": _bounded(value.get("state", ""), 32),
        "compileInProgress": value.get("compileInProgress") is True,
        "pending": value.get("pending") is True,
        "caughtUp": value.get("caughtUp") is True,
        "initialSourceSha256": initial_source_sha256,
        "observedSourceSha256": observed_source_sha256,
        "lastAttemptedSourceSha256": str(
            value.get("lastAttemptedSourceSha256", "") or "").lower(),
        "lastCompiledSourceSha256": str(
            value.get("lastCompiledSourceSha256", "") or "").lower(),
        "latestCandidateRevision": _positive_integer(
            value.get("latestCandidateRevision"), 0),
        "compileAttempts": _positive_integer(value.get("compileAttempts"), 0),
        "compileSuccesses": _positive_integer(value.get("compileSuccesses"), 0),
        "compileFailures": _positive_integer(value.get("compileFailures"), 0),
        "events": _validate_events(value, "shader watcher audit"),
    }


def _live_reused_apply_evidence(value):
    """Project only a complete same-executable reuse receipt from this audit."""
    active = value.get("activeReplacement")
    result = value.get("lastApply")
    if not isinstance(active, dict) or not isinstance(result, dict):
        return None
    binding = result.get("binding")
    validation = result.get("validation")
    replacement = result.get("replacement")
    if not all(isinstance(item, dict) for item in (binding, validation, replacement)):
        return None
    revision = _positive_integer(binding.get("revision"), 0)
    active_revision = _positive_integer(active.get("revision"), 0)
    raw_sha256 = active.get("candidateRawSha256")
    replacement_id = active.get("replacementId")
    if (result.get("schema") != "renderdoc-ultra-shader-shared-resource-apply-mcp/v1" or
            result.get("operation") != "reuse-active-shared-resource" or
            result.get("applied") is not True or result.get("reused") is not True or
            result.get("mutationPerformed") is not False or
            active_revision <= 0 or revision <= 0 or
            revision != value.get("latestHandledRevision") or
            SHA256_RE.fullmatch(str(raw_sha256 or "")) is None or
            EXACT_TOKEN_RE.fullmatch(str(replacement_id or "")) is None or
            binding.get("candidateRawSha256") != raw_sha256 or
            validation.get("replayAbiValidated") is not True or
            validation.get("performedForRevision") is not False or
            validation.get("evidenceReusedFromRevision") != active_revision or
            _positive_integer(active.get("originalResourceId"), 0, 0xFFFFFFFFFFFFFFFF) <= 0 or
            active.get("resourceScoped") is not True or
            active.get("allOriginalResourceUsesAffected") is not True or
            active.get("eventScoped") is not False or
            active.get("originalShaderPreserved") is not True or
            any(replacement.get(key) != active.get(key) for key in (
                "revision", "candidateRawSha256", "replacementId", "originalResourceId",
                "resourceScoped", "allOriginalResourceUsesAffected", "eventScoped",
                "originalShaderPreserved"))):
        return None
    return {"revision": revision, "candidateRawSha256": raw_sha256,
            "replacementId": replacement_id,
            "evidenceReusedFromRevision": active_revision,
            "performedForRevision": False}


def _validate_live(value, workspace, expected_live_id=""):
    if value.get("schema") != LIVE_EDIT_SCHEMA or value.get("schemaVersion") != 1:
        raise ShaderConsoleError("shader live-edit audit schema is invalid")
    live_id = str(value.get("liveEditId", "") or "").lower()
    watch_id = str(value.get("watchId", "") or "").lower()
    if (EXACT_TOKEN_RE.fullmatch(live_id) is None or
            EXACT_TOKEN_RE.fullmatch(watch_id) is None):
        raise ShaderConsoleError("shader live-edit audit identity is invalid")
    if expected_live_id and live_id != expected_live_id:
        raise ShaderConsoleError("shader live-edit audit is not the authorized session")
    if not _same_path(value.get("workspacePath", ""), workspace):
        raise ShaderConsoleError("shader live-edit audit changed workspace")
    active = value.get("activeReplacement")
    active_revision = 0
    if isinstance(active, dict):
        active_revision = _positive_integer(active.get("revision"), 0)
    return {
        "captureBinding": normalize_capture_binding(value.get("captureBinding")),
        "liveEditId": live_id,
        "watchId": watch_id,
        "state": _bounded(value.get("state", ""), 32),
        "sessionState": _bounded(value.get("sessionState", "current"), 32),
        "applyAttempts": _positive_integer(value.get("applyAttempts"), 0),
        "applySuccesses": _positive_integer(value.get("applySuccesses"), 0),
        "applyRejections": _positive_integer(value.get("applyRejections"), 0),
        "applyFailures": _positive_integer(value.get("applyFailures"), 0),
        "applyReuses": _positive_integer(value.get("applyReuses"), 0),
        "applyInProgress": isinstance(value.get("applyInProgress"), dict),
        "pendingApply": isinstance(value.get("pendingApply"), dict),
        "latestHandledRevision": _positive_integer(
            value.get("latestHandledRevision"), 0),
        "latestScheduledRevision": _positive_integer(
            value.get("latestScheduledRevision"), 0),
        "activeRevision": active_revision,
        "activeCandidateRawSha256": (active.get("candidateRawSha256")
                                     if isinstance(active, dict) else None),
        "reusedApplyEvidence": _live_reused_apply_evidence(value),
        "events": _validate_events(value, "shader live-edit audit"),
    }


def _authorised_diagnostic_source(reported, default_source, workspace):
    """Map compiler job copies back to the one authorised editable source."""
    reported = str(reported or "").strip().strip('"')
    if not reported:
        return default_source
    source_root = os.path.join(workspace, "source")
    jobs_root = os.path.join(workspace, "jobs")
    candidate = reported
    if not os.path.isabs(candidate):
        candidate = os.path.join(source_root, candidate)
    candidate = os.path.abspath(candidate)
    if _within(source_root, candidate) and os.path.isfile(candidate) and not _is_reparse(candidate):
        return candidate
    # A job's header or generated later-hop source has different line numbers.
    # Do not invent a location in the editable main document for these files.
    basename = os.path.basename(reported.replace("\\", "/")).lower()
    extension = os.path.splitext(default_source)[1].lower()
    if basename in {"input" + extension, os.path.basename(default_source).lower()}:
        return default_source
    if reported == "0" or basename == "source0" + extension:
        return default_source
    return ""


def parse_compiler_diagnostics(message, default_source_path, workspace_path=None):
    """Parse bounded target-compiler locations without trusting their paths.

    Every returned ``sourceFile`` is either the exact authorised source or a
    regular include under that workspace's dedicated ``source`` directory.
    """
    text = _bounded(message)
    source = os.path.abspath(str(default_source_path))
    workspace = os.path.abspath(str(workspace_path or os.path.dirname(os.path.dirname(source))))
    matches = []
    occupied = []
    for pattern in (GLSL_DIAGNOSTIC_RE, MESA_DIAGNOSTIC_RE,
                    PAREN_DIAGNOSTIC_RE, COLON_DIAGNOSTIC_RE):
        for match in pattern.finditer(text):
            span = match.span()
            if any(not (span[1] <= item[0] or span[0] >= item[1]) for item in occupied):
                continue
            occupied.append(span)
            line = _positive_integer(int(match.group("line")), 0, 100000000)
            column_text = match.group("column")
            column = _positive_integer(int(column_text), 0, 100000000) if column_text else 0
            severity = match.group("severity").lower().replace("fatal ", "")
            reported = match.group("file").strip()
            detail = match.group("message").strip()
            detail = re.sub(r"^\s*:\s*", "", detail)
            matches.append({
                "severity": severity,
                "message": _bounded(detail or ("compiler %s" % severity)),
                "sourceFile": _authorised_diagnostic_source(
                    reported, source, workspace),
                "reportedFile": _bounded(reported, 4096),
                "line": line,
                "column": column,
                "offset": span[0],
            })
    matches.sort(key=lambda item: item.pop("offset"))
    if not matches:
        # A global linking/target error still explains a failure. It has no
        # source location and must not create a misleading line-one marker.
        for line in text.splitlines():
            match = re.match(r"^\s*(?P<severity>fatal\s+error|error|warning)\s*:\s*(?P<message>.+)$", line, re.I)
            if match:
                matches.append({"severity": match.group("severity").lower().replace("fatal ", ""),
                    "message": _bounded(match.group("message")), "sourceFile": "", "reportedFile": "",
                    "line": 0, "column": 0})
                if len(matches) >= 128:
                    break
    return matches


def _entry(origin, event, severity, phase, text, source_path="", line=0,
           column=0, reported_file="", stream_key="", identity_suffix=""):
    sequence = event.get("sequence", 0)
    cursor_sequence = event.get("cursorSequence", sequence)
    stream_value = stream_key or origin
    identity = "%s:%d" % (stream_value, sequence)
    if identity_suffix:
        identity += ":" + str(identity_suffix)
    return {
        "id": identity,
        "origin": origin,
        "streamKey": stream_value,
        "sequence": sequence,
        "cursorSequence": cursor_sequence,
        "at": event.get("at", ""),
        "unixMs": _positive_integer(event.get("unixMs"), 0, 0x7FFFFFFFFFFFFFFF),
        "eventType": event.get("type", ""),
        "severity": normalise_console_severity(severity),
        "phase": phase,
        "text": _bounded(text),
        # A source is locatable only when an actual compiler diagnostic supplied
        # an explicit line. Session/ABI/persistence errors must never become the
        # misleading ``shader.hlsl:1:1`` fallback.
        "sourceFile": source_path if source_path and line > 0 else "",
        "reportedFile": reported_file,
        "line": line if line > 0 else 0,
        "column": column if line > 0 and column > 0 else 0,
        "module": "shader",
    }


def _diagnostic_identity(diagnostic, duplicate_index=0):
    """Return a stable content identity without borrowing audit sequences."""
    payload = json.dumps([
        normalise_console_severity(diagnostic.get("severity", "error")),
        str(diagnostic.get("reportedFile", "") or ""),
        _positive_integer(diagnostic.get("line"), 0, 100000000),
        _positive_integer(diagnostic.get("column"), 0, 100000000),
        str(diagnostic.get("message", "") or ""),
    ], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return "diagnostic:%s:%d" % (digest, int(duplicate_index))


def _is_superseded_apply_failure(event):
    """Return true only for the fixed stale-candidate race diagnostics."""
    if not isinstance(event, dict) or event.get("type") != "applyFailed":
        return False
    message = str(event.get("message", "") or "").lower()
    return any(fragment in message for fragment in SUPERSEDED_APPLY_FAILURE_FRAGMENTS)


def console_entries_from_audits(workspace, source_path, watch=None, live=None,
                                compiler_diagnostics=None):
    """Project fixed audit events into a bounded Unity-style console model."""
    entries = []
    workspace_key = _normal_path(workspace)
    watch_stream = "%s:watch:%s" % (
        workspace_key, (watch or {}).get("watchId", "default"))
    live_stream = "%s:live:%s" % (
        workspace_key, (live or {}).get("liveEditId", "default"))
    watch_labels = {
        "watchCreated": ("info", "监听", "Shader 源码监听已创建"),
        "watchStarted": ("success", "监听", "Shader 源码监听已启动"),
        "sourceChanged": ("info", "检测", "检测到 Shader 源码保存"),
        "compileStarted": ("progress", "编译", "正在编译 Shader"),
        "compileSucceeded": ("success", "编译", "Shader 编译成功，等待验证和应用"),
        "compileRetryScheduled": (
            "warning", "编译", "检测到瞬时文件占用，将自动重试本次保存"),
        "sourceChangedDuringCompile": (
            "warning", "检测", "编译期间检测到新的保存，将继续处理最新版本"),
        "observationFailed": ("error", "监听", "读取 Shader 源码失败"),
        "observationRecovered": ("success", "监听", "Shader 源码监听已恢复"),
        "stopRequested": ("warning", "监听", "Shader 源码监听正在停止"),
        "watchStopRequested": ("warning", "监听", "Shader 源码监听正在停止"),
        "watchStopped": ("warning", "监听", "Shader 源码监听已停止"),
        "watchFailed": ("error", "监听", "Shader 源码监听失败"),
    }
    if isinstance(watch, dict):
        for event in watch.get("events", []):
            event_type = event.get("type", "")
            if event_type == "compileFailed":
                message = event.get("message") or event.get("diagnosticMessage") or "Shader 编译失败"
                diagnostics = []
                for structured in event.get("diagnostics", []):
                    diagnostics.append({
                        "severity": structured["severity"],
                        "message": structured["message"] or message,
                        "sourceFile": _authorised_diagnostic_source(
                            structured.get("file", ""), source_path, workspace),
                        "reportedFile": structured.get("file", ""),
                        "line": structured.get("line", 0),
                        "column": structured.get("column", 0),
                    })
                if not diagnostics:
                    diagnostics = parse_compiler_diagnostics(
                        message, source_path, workspace)
                source_sha256 = event.get("requestedSourceSha256", "")
                if (not diagnostics and isinstance(compiler_diagnostics, dict)
                        and SHA256_RE.fullmatch(source_sha256 or "")):
                    diagnostics = compiler_diagnostics.get(source_sha256, [])
                if diagnostics:
                    diagnostic_occurrences = {}
                    for diagnostic in diagnostics:
                        cloned = dict(event)
                        cloned["cursorSequence"] = event.get("sequence", 0)
                        base_identity = _diagnostic_identity(diagnostic, 0)
                        duplicate_index = diagnostic_occurrences.get(base_identity, 0)
                        diagnostic_occurrences[base_identity] = duplicate_index + 1
                        entries.append(_entry(
                            "watch", cloned, diagnostic["severity"], "编译",
                            diagnostic["message"], diagnostic["sourceFile"],
                            diagnostic["line"], diagnostic["column"],
                            diagnostic["reportedFile"], watch_stream,
                            _diagnostic_identity(diagnostic, duplicate_index)))
                else:
                    entries.append(_entry(
                        "watch", event, "error", "编译",
                        "%s: %s" % (event.get("errorType") or "编译错误", message),
                        stream_key=watch_stream))
                continue
            label = watch_labels.get(event_type)
            if label:
                text = label[2]
                if event.get("attempt"):
                    text += "（第 %d 次）" % event["attempt"]
                if event.get("revision"):
                    text += " · revision %d" % event["revision"]
                if event.get("message") and event_type in ("observationFailed",):
                    text += "：" + event["message"]
                entries.append(_entry(
                    "watch", event, label[0], label[1], text,
                    stream_key=watch_stream))

    live_labels = {
        "liveEditCreated": ("info", "实时更新", "实时 Shader 更新会话已创建"),
        "liveEditStarted": ("success", "实时更新", "实时 Shader 更新已启动"),
        "compileRecovered": ("success", "编译", "Shader 编译已从上一次错误恢复"),
        "applyStarted": ("progress", "ABI / 应用", "正在验证 ABI 并应用 Shader"),
        "applyQueued": ("progress", "应用队列", "Shader 已进入实时应用队列"),
        "applySuperseded": (
            "warning", "应用队列", "较旧 Shader 已由更新的保存取代"),
        "applySucceeded": ("success", "ABI / 应用", "Shader 已验证并更新到 RenderDoc"),
        "applyReused": (
            "success", "ABI / 应用", "Shader 字节未变，复用已验证的实时替换"),
        "applyRejected": ("error", "ABI / 应用", "Shader 被 ABI / Replay 门禁拒绝"),
        "applyFailed": ("error", "ABI / 应用", "Shader 应用失败"),
        "liveEditFailed": ("error", "实时更新", "实时 Shader 更新会话失败"),
        "persistentAuthorizationRevokeFailed": (
            "error", "授权", "撤销实时编辑持久授权失败"),
        "workspaceWatchStopFailed": ("error", "监听", "停止源码监听失败"),
        "liveEditStopRequested": ("warning", "实时更新", "实时 Shader 更新正在停止"),
        "liveEditStopped": ("warning", "实时更新", "实时 Shader 更新已停止"),
    }
    if isinstance(live, dict):
        for event in live.get("events", []):
            event_type = event.get("type", "")
            # The watcher event contains richer compiler diagnostics; avoid a
            # duplicate red row when both audits are present.
            if event_type == "compileFailed" and isinstance(watch, dict):
                continue
            label = live_labels.get(event_type)
            if event_type == "compileFailed":
                label = ("error", "编译", "Shader 编译失败")
            elif _is_superseded_apply_failure(event):
                label = (
                    "warning", "应用队列",
                    "较旧 Shader 应用已由新的保存取代")
            if not label:
                continue
            text = label[2]
            if event.get("revision"):
                text += " · revision %d" % event["revision"]
            if event.get("message"):
                text += "：" + event["message"]
            entries.append(_entry(
                "live", event, label[0], label[1], text,
                stream_key=live_stream))

    entries.sort(key=lambda item: (
        item.get("at", ""), 0 if item.get("origin") == "watch" else 1,
        item.get("sequence", 0)))
    return entries[-MAX_CONSOLE_ENTRIES:]


def _validate_journal_event(raw, filename_match, previous_sequence):
    if not isinstance(raw, dict):
        raise ShaderConsoleError("console journal contains a non-object event")
    if raw.get("schema") != JOURNAL_EVENT_SCHEMA or raw.get("schemaVersion") != 1:
        raise ShaderConsoleError("console journal event schema is invalid")
    session_id = str(raw.get("sessionId", "") or "").lower()
    role = str(raw.get("processRole", "") or "").lower()
    process_id = _positive_integer(raw.get("processId"), 0, 0x7FFFFFFF)
    sequence = _positive_integer(raw.get("sequence"), 0, 0x7FFFFFFFFFFFFFFF)
    if (session_id != filename_match.group("session") or
            role != filename_match.group("role") or
            process_id != int(filename_match.group("pid")) or
            sequence <= previous_sequence):
        raise ShaderConsoleError("console journal event identity/order is invalid")
    unix_ms = _positive_integer(raw.get("unixMs"), 0, 0x7FFFFFFFFFFFFFFF)
    message = _bounded(raw.get("message", ""))
    severity = str(raw.get("severity", "") or "").lower()
    if not message or severity not in CONSOLE_SEVERITIES or unix_ms <= 0:
        raise ShaderConsoleError("console journal display fields are invalid")
    module = _console_module(raw.get("module", ""))
    context = _console_context(raw.get("context"))
    stream = "journal:%s:%d:%s" % (role, process_id, session_id)
    return {
        "id": "%s:%d" % (stream, sequence),
        "origin": "journal",
        "streamKey": stream,
        "sequence": sequence,
        "cursorSequence": sequence,
        "at": _bounded(raw.get("at", ""), 128) or _utc_now_text(unix_ms),
        "unixMs": unix_ms,
        "eventType": _bounded(raw.get("code", ""), 256) or "consoleEvent",
        "severity": severity,
        "phase": _bounded(raw.get("phase", ""), 128) or module,
        "text": message,
        "sourceFile": "",
        "reportedFile": "",
        "line": 0,
        "column": 0,
        "module": module,
        "context": context,
        "processRole": role,
        "processId": process_id,
    }


def _load_generic_console_entries():
    """Read the newest strict journals within one global byte/event budget."""
    result = {"entries": [], "errors": [], "watchedPaths": []}
    configured = default_console_journal_directory()
    if not configured:
        return result
    root = os.path.abspath(str(configured))
    if not os.path.isdir(root):
        return result
    if _is_reparse(root):
        result["errors"].append("Bread console journal directory is redirected")
        return result
    try:
        # The UI may be the only long-running reader while a host-only producer
        # appends events. Run the same generic-spool retention at most once per
        # minute; this never touches workspace, authorisation, or safety audits.
        _maybe_enforce_console_journal_retention(root)
        candidates = _journal_candidates(root)
        candidates.sort(
            key=lambda item: (item["mtime"], item["name"]), reverse=True)
        remaining_bytes = MAX_JOURNAL_READ_BYTES
        for item in candidates[:MAX_JOURNAL_READ_FILES]:
            if (remaining_bytes <= 0 or
                    len(result["entries"]) >= MAX_CONSOLE_ENTRIES):
                break
            name = item["name"]
            path = item["path"]
            match = item["match"]
            read_budget = min(
                MAX_JOURNAL_READ_BYTES_PER_FILE, remaining_bytes,
                max(item["size"], 1))
            try:
                raw = _read_file_tail_bytes(
                    path, MAX_JOURNAL_BYTES, read_budget,
                    "console event journal")
                remaining_bytes -= min(item["size"], read_budget)
                lines = raw.splitlines()
                remaining_events = MAX_CONSOLE_ENTRIES - len(result["entries"])
                if len(lines) > remaining_events:
                    lines = lines[-remaining_events:]
                previous = 0
                for index, line in enumerate(lines):
                    if not line.strip():
                        continue
                    if len(line) > MAX_JOURNAL_LINE_BYTES:
                        raise ShaderConsoleError("console journal line exceeds its bounded size")
                    try:
                        value = json.loads(line.decode("utf-8", errors="strict"))
                        event = _validate_journal_event(value, match, previous)
                    except (UnicodeError, ValueError, TypeError) as exc:
                        # A producer may be interrupted during its final append.
                        # Ignore only that incomplete tail; earlier evidence is
                        # immutable and remains visible.
                        if index == len(lines) - 1 and not raw.endswith(b"\n"):
                            break
                        raise ShaderConsoleError(
                            "console journal JSON is invalid: %s" % _bounded(exc, 512))
                    previous = event["sequence"]
                    result["entries"].append(event)
                result["watchedPaths"].append(path)
            except ShaderConsoleError as exc:
                result["errors"].append("%s: %s" % (name, _bounded(exc, 512)))
        result["entries"].sort(key=lambda item: (
            item.get("unixMs", 0), item.get("streamKey", ""),
            item.get("sequence", 0)))
        result["entries"] = result["entries"][-MAX_CONSOLE_ENTRIES:]
        result["watchedPaths"].insert(0, root)
    except (OSError, ShaderConsoleError) as exc:
        result["errors"].append(
            "Bread console journal is unavailable: %s" % _bounded(exc, 512))
    return result


def console_severity_counts(entries):
    counts = {"info": 0, "warning": 0, "error": 0}
    for entry in entries or []:
        severity = normalise_console_severity(
            entry.get("severity", "info") if isinstance(entry, dict) else "info")
        counts[severity] += 1
    return counts


def build_console_clear_cursors(entries):
    """Build a view-only high-water mark; original audit files stay intact."""
    cursors = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        stream = str(entry.get("streamKey", "") or "")
        sequence = _positive_integer(entry.get("cursorSequence"), 0,
                                     0x7FFFFFFFFFFFFFFF)
        if stream and sequence > cursors.get(stream, 0):
            cursors[stream] = sequence
    return cursors


def console_entries_after_clear(entries, cursors):
    cursor_values = cursors if isinstance(cursors, dict) else {}
    visible = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        stream = str(entry.get("streamKey", "") or "")
        sequence = _positive_integer(entry.get("cursorSequence"), 0,
                                     0x7FFFFFFFFFFFFFFF)
        if not stream or sequence > _positive_integer(
                cursor_values.get(stream), 0, 0x7FFFFFFFFFFFFFFF):
            visible.append(entry)
    return visible


def filter_console_entries(entries, enabled=None):
    values = enabled if isinstance(enabled, dict) else {
        "info": True, "warning": True, "error": True}
    return [entry for entry in entries or [] if values.get(
        normalise_console_severity(entry.get("severity", "info")), False)]


def _append_snapshot_error_entries(result):
    for message in result.get("errors", []):
        text_value = _bounded(message, 4096)
        digest = hashlib.sha256(text_value.encode("utf-8")).hexdigest()
        stream = "console-loader:%s" % digest
        result["entries"].append({
            "id": "%s:1" % stream,
            "origin": "console",
            "streamKey": stream,
            "sequence": 1,
            "cursorSequence": 1,
            "at": _utc_now_text(result.get("generatedUnixMs")),
            "unixMs": result.get("generatedUnixMs", 0),
            "eventType": "consoleSourceFailed",
            "severity": "error",
            "phase": "控制台",
            "text": text_value,
            "sourceFile": "",
            "reportedFile": "",
            "line": 0,
            "column": 0,
            "module": "system",
        })


def derive_console_status(watch=None, live=None):
    """Return the current headline/progress from the latest fixed audit state."""
    watch = watch if isinstance(watch, dict) else {}
    live = live if isinstance(live, dict) else {}
    if live.get("state") == "failed":
        return {"text": "实时更新会话失败", "phase": "错误", "percent": 100,
                "busy": False, "severity": "error"}
    if watch.get("compileInProgress"):
        return {"text": "正在编译 Shader", "phase": "编译", "percent": 45,
                "busy": True, "severity": "progress"}
    if watch.get("pending"):
        return {"text": "已检测到保存，等待源码稳定", "phase": "检测", "percent": 20,
                "busy": True, "severity": "progress"}
    combined = []
    for origin, audit in (("watch", watch), ("live", live)):
        for event in audit.get("events", []):
            combined.append((event.get("at", ""), origin, event))
    combined.sort(key=lambda item: item[0])
    latest = combined[-1][2] if combined else {}
    event_type = latest.get("type", "")
    if event_type == "applyStarted":
        return {"text": "正在验证 ABI 并应用 Shader", "phase": "ABI / 应用",
                "percent": 75, "busy": True, "severity": "progress"}
    if event_type in ("applyQueued", "applySuperseded"):
        return {"text": "Shader 已排队，等待实时应用", "phase": "应用队列",
                "percent": 68, "busy": True, "severity": "progress"}
    if _is_superseded_apply_failure(latest):
        revision = _positive_integer(latest.get("revision"), 0)
        return {"text": "较旧 Shader%s 已由新的保存取代" % (
                    " revision %d" % revision if revision else ""),
                "phase": "应用队列", "percent": 68, "busy": False,
                "severity": "warning"}
    if event_type in ("compileFailed", "applyRejected", "applyFailed", "liveEditFailed"):
        return {"text": latest.get("message") or "Shader 更新失败", "phase": "错误",
                "percent": 100, "busy": False, "severity": "error"}
    if event_type == "compileSucceeded":
        return {"text": "编译成功，等待 ABI 验证和应用", "phase": "编译",
                "percent": 65, "busy": True, "severity": "progress"}
    if event_type in ("applySucceeded", "applyReused") or live.get("activeRevision", 0) > 0:
        # A byte-identical save records applyReused for the new revision while
        # intentionally preserving the original active replacement object.
        # For a success event prefer that event, then the live manager's
        # handled revision.  For unrelated later events retain the actual
        # replacement revision so a failed handled revision is never labelled
        # as successfully active.
        if event_type in ("applySucceeded", "applyReused"):
            revision = _positive_integer(latest.get("revision"), 0)
            if not revision:
                revision = live.get("latestHandledRevision", 0)
            if not revision:
                revision = live.get("activeRevision", 0)
        else:
            revision = live.get("activeRevision", 0)
        return {"text": "Shader 已实时更新%s" % (
                    " · revision %d" % revision if revision else ""),
                "phase": "完成", "percent": 100, "busy": False,
                "severity": "success"}
    if live.get("state") == "running":
        return {"text": "实时监听中", "phase": "监听", "percent": 0,
                "busy": False, "severity": "success"}
    return {"text": "等待持久实时编辑会话", "phase": "空闲", "percent": 0,
            "busy": False, "severity": "info"}


def _project_source_document_identity(workspace, manifest, source_sha256):
    """Identify an unchanged imported project document for UI grouping only.

    Catalog IDs and replay ResourceIds are observations of that document. They
    are not its source identity. A locally edited copy never inherits this key.
    This projection grants no save, compile or replacement authority.
    """
    source = manifest.get("source") or {}
    receipt = source.get("projectSource")
    shader = manifest.get("shader") or {}
    if not isinstance(receipt, dict):
        return None
    unsigned = dict(receipt)
    receipt_sha = unsigned.pop("receiptSha256", None)
    if (receipt.get("schema") != "replaylab-verified-project-shader-source/v1" or
            receipt_sha != _canonical_sha256(unsigned) or
            EXACT_TOKEN_RE.fullmatch(str(receipt.get("projectId") or "")) is None or
            EXACT_TOKEN_RE.fullmatch(str(receipt.get("knowledgeId") or "")) is None or
            SHA256_RE.fullmatch(str(source_sha256 or "")) is None or
            source_sha256 != receipt.get("sourceSha256") or
            receipt.get("originalRawSha256") != shader.get("rawSha256") or
            receipt.get("stage") != manifest.get("stage") or
            receipt.get("entryPoint") != manifest.get("entryPoint")):
        return None
    project_path = str(receipt.get("projectPath") or "")
    if not os.path.isabs(project_path):
        return None
    project_root = os.path.dirname(os.path.dirname(project_path))
    if (not _same_path(project_path, os.path.join(project_root, *ACTIVE_PROJECT_LAYOUT["replayProject"].split("/"))) or
            not _within(os.path.join(project_root, *ACTIVE_PROJECT_LAYOUT["shaderWorkspaces"].split("/")), workspace)):
        return None
    return {key: receipt[key] for key in (
        "projectId", "knowledgeId", "originalRawSha256", "stage", "entryPoint", "sourceSha256")}


def _workspace_document_key(model):
    project = model.get("projectSourceIdentity")
    if isinstance(project, dict):
        return ("project",) + tuple(project.get(key) for key in (
            "projectId", "knowledgeId", "originalRawSha256", "stage", "entryPoint", "sourceSha256"))
    return ("physical", model["resourceId"], model["stage"], model["entryPoint"],
            model["shaderRawSha256"], model.get("sourceSha256", ""))


def logical_shader_document_id(model):
    """Stable display identity; physical workspace IDs still own edits/replay."""
    api = str(model.get("captureApi") or "").split(".")[-1].lower()
    raw = model.get("shaderRawSha256")
    stage, entry = model.get("stage"), model.get("entryPoint")
    if (not api or not isinstance(raw, str) or not SHA256_RE.fullmatch(raw) or
            not isinstance(stage, str) or not stage or not isinstance(entry, str)):
        return ""
    return "shader-" + _canonical_sha256({"api": api, "originalRawSha256": raw,
        "stage": stage, "entryPoint": entry})


def _current_project_source_head(model, library):
    """Match displayed bytes to the current head without rewriting import provenance."""
    origin = model.get("projectSourceIdentity") or model.get("projectSourceOrigin")
    if (not library.get("available") or not isinstance(origin, dict) or
            origin.get("projectId") != library.get("projectId") or
            any(origin.get(key) != model.get(field) for key, field in (
                ("originalRawSha256", "shaderRawSha256"), ("stage", "stage"),
                ("entryPoint", "entryPoint"))) or
            not _within(os.path.join(library.get("projectRoot", ""),
                *ACTIVE_PROJECT_LAYOUT["shaderWorkspaces"].split("/")),
                model.get("workspacePath", ""))):
        return None
    matches = [head for head in library.get("documents", [])
               if head.get("isCurrentHead") is True and
               all(head.get(key) == model.get(field) and model.get(field) not in (None, "")
                   for key, field in (("rawShaderSha256", "shaderRawSha256"),
                       ("stage", "stage"), ("entryPoint", "entryPoint"),
                       ("sourceSha256", "sourceSha256")))]
    return matches[0] if len(matches) == 1 else None


def _superseded_project_format_aliases(models, library, current_heads):
    """One published representation per original Shader, independent of apply.

    Project heads can be split by source language/compiler history. That is
    useful durable provenance, but not a reason to show two files for the same
    original Shader. This is presentation only: bytes and import receipts stay
    untouched, and the panel retains any alternate with unsaved local work.
    """
    if not library.get("available"):
        return {}
    groups = {}
    for model in models:
        identity = model.get("projectSourceIdentity") or model.get("projectSourceOrigin") or {}
        head = current_heads.get(model.get("workspaceId"))
        if (not head or identity.get("projectId") != library.get("projectId") or
                head.get("isCurrentHead") is not True or
                model.get("editorEligible") is not True or
                model.get("retainedWorkingCopy") or model.get("dirty") or model.get("manualDraft") or
                any(identity.get(key) != model.get(field) for key, field in (
                    ("originalRawSha256", "shaderRawSha256"), ("stage", "stage"),
                    ("entryPoint", "entryPoint"))) or
                any(head.get(key) != model.get(field) for key, field in (
                    ("rawShaderSha256", "shaderRawSha256"), ("stage", "stage"),
                    ("entryPoint", "entryPoint"), ("sourceSha256", "sourceSha256"))) or
                not model.get("captureApi") or not model.get("capturePath")):
            continue
        # An import receipt deliberately keeps its original source hash after
        # later edits. Only the exact current published bytes above associate
        # that newer display head; this never rewrites or promotes the receipt.
        logical_id = logical_shader_document_id(model)
        if not logical_id:
            continue
        key = (_normal_path(model["capturePath"]), logical_id)
        groups.setdefault(key, []).append((model, head))
    aliases = {}
    for values in groups.values():
        ranked = [(model, head) for model, head in values
                  if isinstance(head.get("activationRevision"), int) and
                  not isinstance(head.get("activationRevision"), bool)]
        if not ranked:
            continue
        generation = max(head["activationRevision"] for model, head in ranked)
        latest = [(model, head) for model, head in ranked if head["activationRevision"] == generation]
        if len({head.get("knowledgeId") for model, head in latest}) != 1:
            continue
        preferred = latest[0][0]
        for model, head in values:
            old_generation = head.get("activationRevision")
            if (model["workspaceId"] == preferred["workspaceId"] or
                    isinstance(old_generation, bool) or not isinstance(old_generation, int) or
                    old_generation >= generation or
                    model.get("sourceSha256") == preferred.get("sourceSha256")):
                continue
            aliases[model["workspaceId"]] = preferred["workspaceId"]
    return aliases


def _replay_source_document_aliases(models, binding, library):
    """Keep a restore implementation workspace out of the authored source tree.

    A restore may need immutable saved bytes while the user continues editing
    another file. Only an explicit, rechecked restore relation can join those
    documents. File dates, EIDs and equal filenames never select the winner.
    This is presentation metadata, not source or replay authorization.
    """
    aliases, watched = {}, []
    if not library.get("available") or not binding:
        return aliases, watched
    project = library.get("projectRoot", "")
    project_id = library.get("projectId")
    shader_root = os.path.join(project, *ACTIVE_PROJECT_LAYOUT["shaderWorkspaces"].split("/"))
    by_id, ambiguous = {}, set()
    for model in models:
        key = str(model.get("workspaceId") or "")
        if key in by_id:
            ambiguous.add(key)
        by_id[key] = model
    for key in ambiguous:
        by_id.pop(key, None)
    documents = {item.get("knowledgeId"): item for item in library.get("documents", [])
                 if item.get("isCurrentHead") is True}

    def same_original(left, right):
        return bool(left.get("editorEligible") is True and right.get("editorEligible") is True
            and _same_path(left.get("capturePath", ""), binding["capturePath"])
            and _same_path(right.get("capturePath", ""), binding["capturePath"])
            and all(left.get(key) == right.get(key) and left.get(key) not in (None, "")
                    for key in ("resourceId", "shaderRawSha256", "stage", "entryPoint"))
            and left.get("captureApi") == right.get("captureApi")
            and left.get("targetProfile") == right.get("targetProfile"))

    def admissible(replay, draft, knowledge_id, expected_source):
        imported = replay.get("projectSourceIdentity") or {}
        head = documents.get(knowledge_id) or {}
        return bool(replay is not draft and not replay.get("manualDraft") and same_original(replay, draft)
            and by_id.get(replay.get("workspaceId")) is replay
            and by_id.get(draft.get("workspaceId")) is draft
            and _within(shader_root, replay.get("workspacePath", ""))
            and _within(shader_root, draft.get("workspacePath", ""))
            and imported.get("projectId") == project_id
            and imported.get("knowledgeId") == knowledge_id
            and replay.get("sourceSha256") == expected_source == imported.get("sourceSha256")
            and head.get("sourceSha256") == expected_source
            and head.get("rawShaderSha256") == replay.get("shaderRawSha256")
            and head.get("stage") == replay.get("stage")
            and head.get("entryPoint") == replay.get("entryPoint"))

    # Read the current capture's exact derived snapshot. Its optional editor
    # hints remain usable across validation-policy upgrades, but cannot hide a
    # changed replay source or cross a project/capture/original-resource boundary.
    snapshot_root = os.path.join(project, *ACTIVE_PROJECT_LAYOUT["replayProject"].split("/"),
                                 "derived", "validated-rdc-restores")
    capture_key = hashlib.sha256(_normal_path(binding["capturePath"]).encode("utf-8")).hexdigest()
    snapshot_path = os.path.join(snapshot_root, capture_key + ".json")
    watched.extend((os.path.dirname(snapshot_root), snapshot_root, snapshot_path))
    try:
        snapshot, unused_digest = _read_project_json(snapshot_path, MAX_PROJECT_MANIFEST_BYTES,
            "verified restore source projection", project)
        unsigned = dict(snapshot)
        seal = unsigned.pop("sealSha256", None)
        if (snapshot.get("schema") != "replaylab-verified-rdc-restore/v1"
                or snapshot.get("schemaVersion") not in (1, 2)
                or seal != _canonical_sha256(unsigned)
                or snapshot.get("projectId") != project_id
                or snapshot.get("projectWorkspaceId") != library.get("projectWorkspaceId")
                or not _same_path(snapshot.get("capturePath", ""), binding["capturePath"])):
            raise ShaderConsoleError("restore source projection identity changed")
        rows = snapshot.get("shaders")
        if not isinstance(rows, list) or len(rows) > MAX_DRAFT_WORKSPACE_DISCOVERY:
            raise ShaderConsoleError("restore source projection is invalid")
        proposed = {}
        for record in rows:
            if not isinstance(record, dict):
                continue
            hint, owner = record.get("preservedLocalSource"), record.get("managedWorkspace")
            if not isinstance(hint, dict) or not isinstance(owner, dict):
                continue
            replay, draft = by_id.get(owner.get("workspaceId")), by_id.get(hint.get("workspaceId"))
            if replay is None or draft is None:
                continue
            identity = record.get("identity") or {}
            if (not isinstance(identity, dict)
                    or not admissible(replay, draft, record.get("knowledgeId"), record.get("sourceSha256"))
                    or any(identity.get(source) != replay.get(target) for source, target in (
                        ("resourceId", "resourceId"), ("rawSha256", "shaderRawSha256"),
                        ("stage", "stage"), ("entryPoint", "entryPoint")))):
                continue
            replay_path = _project_relative_path(project, owner.get("path"), "restore source owner")
            draft_path = _project_relative_path(project, hint.get("path"), "preserved source owner")
            if (not _same_path(replay_path, replay["workspacePath"])
                    or not _same_path(draft_path, draft["workspacePath"])):
                continue
            proposed.setdefault(replay["workspaceId"], set()).add(draft["workspaceId"])
        aliases.update((key, next(iter(values))) for key, values in proposed.items() if len(values) == 1)
    except (ShaderConsoleError, OSError, ValueError, TypeError, KeyError):
        pass

    # Before a first successful snapshot (or when its disposable cache is
    # missing), recognize only the exact generated replay-owner naming rule and
    # a verified unchanged project import. This also covers the legacy spelling.
    for replay in models:
        replay_id = replay.get("workspaceId")
        if replay_id in aliases:
            continue
        imported = replay.get("projectSourceIdentity") or {}
        knowledge_id = imported.get("knowledgeId")
        if EXACT_TOKEN_RE.fullmatch(str(knowledge_id or "")) is None:
            continue
        candidates = []
        for draft in models:
            if not admissible(replay, draft, knowledge_id, imported.get("sourceSha256")):
                continue
            draft_path = draft.get("workspacePath", "")
            if not _same_path(os.path.dirname(draft_path), os.path.dirname(replay["workspacePath"])):
                continue
            name = os.path.basename(draft_path)
            generated = _canonical_sha256({"domain": "replaylab-isolated-replay-workspace/v1",
                "originalWorkspace": name, "knowledgeId": knowledge_id})
            if os.path.basename(replay["workspacePath"]) in (generated, name + "-replay-" + knowledge_id):
                candidates.append(draft["workspaceId"])
        if len(candidates) == 1:
            aliases[replay_id] = candidates[0]
    # Ambiguous cycles or transitive replay copies cannot choose an authored
    # source. Keep them independently visible rather than silently discard work.
    aliases = {key: value for key, value in aliases.items() if value not in aliases}
    return aliases, watched


def _load_semantic_overview(workspace):
    """Optional navigation metadata; it never authorizes source or replay changes."""
    path = os.path.join(workspace, "ai", "semantic-overview.json")
    if not os.path.exists(path):
        return None
    try:
        if os.path.islink(path) or not _same_path(os.path.realpath(path), os.path.abspath(path)):
            return None
        value = _read_json(path, 1024 * 1024, "Shader semantic overview")
        if (not isinstance(value, dict) or value.get("version") != 1 or
                not isinstance(value.get("stages"), list) or
                len(value["stages"]) > 256 or
                re.fullmatch(r"[0-9a-f]{64}", str(value.get("sourceSha256") or "")) is None):
            return None
        return value
    except (ShaderConsoleError, ValueError, OSError):
        return None


def _load_preview_navigation(workspace, manifest, source_sha256, document_id, source_path):
    """Read navigation for the saved/proposal editor independently of apply gates.

    A source-only preview is also used when registration or replay is pending.
    Omitting these sidecars made that editor silently fall back to the entry
    function alone. Metadata never supplies compile/apply authorization. Old
    overview anchors remain useful after edits; the outline resolver reports
    their actual current/stale state without promoting the old review.
    """
    ai_path = os.path.join(workspace, "ai")
    graph_path = os.path.join(ai_path, "flow-graph.json")
    overview = _load_semantic_overview(workspace)
    if overview is not None and (
            overview.get("originalRawSha256") != (manifest.get("shader") or {}).get("rawSha256") or
            overview.get("entryPoint") != manifest.get("entryPoint")):
        overview = None
    result = {
        "semanticOverview": overview,
        "flowGraph": None,
        "flowGraphStatus": {"available": False, "status": "unavailable",
                            "artifactPath": graph_path},
        "watchedPaths": [ai_path, graph_path, os.path.join(ai_path, "semantic-overview.json")],
    }
    if not os.path.exists(graph_path) and not os.path.islink(graph_path):
        return result
    try:
        _regular_file_in_tree(workspace, graph_path, MAX_SHADER_FLOW_GRAPH_BYTES,
                              "Shader preview flow graph")
        graph = _validate_shader_flow_graph_value(
            _read_json(graph_path, MAX_SHADER_FLOW_GRAPH_BYTES, "Shader preview flow graph"),
            expected_workspace_id=manifest["workspaceId"],
            expected_source_sha256=source_sha256,
            expected_stage=manifest.get("stage", ""),
            expected_entry_point=manifest.get("entryPoint"))
        graph = copy.deepcopy(graph)
        relative_source = os.path.relpath(source_path, workspace).replace("\\", "/")
        for node in graph["nodes"]:
            node["source"].update(workspaceId=document_id, documentId=document_id,
                                  path=relative_source)
        graph.update(available=True, status="current", stale=False,
                     artifactPath=graph_path, navigationDocumentId=document_id)
        result["flowGraph"] = graph
        result["flowGraphStatus"] = {"available": True, "status": "current",
                                    "artifactPath": graph_path}
    except (ShaderConsoleError, OSError, ValueError, TypeError) as exc:
        result["flowGraphStatus"].update(status="invalid", error=_bounded(exc, 1024))
    return result


def _workspace_model(authorization, capture_binding=None):
    workspace = authorization["workspacePath"]
    live = None
    live_path = (
        "" if authorization.get("viewOnlyDraft") else
        _latest_token_json(
            os.path.join(workspace, "live-edit"), authorization["lastLiveEditId"])
    )
    if live_path:
        live_value = _read_json(live_path, MAX_AUDIT_BYTES, "shader live-edit audit")
        live = _validate_live(live_value, workspace, authorization["lastLiveEditId"])
    if (capture_binding is not None and not _same_path(
            authorization["capturePath"], capture_binding["capturePath"])):
        return None
    source_path, compiler_diagnostics, manifest_path = _validate_manifest(
        workspace, authorization)
    organization_gate_path = os.path.join(
        workspace, "ai", "organize-code-gate.json")
    flow_graph_path = os.path.join(workspace, "ai", "flow-graph.json")
    editor_eligible = False
    editor_eligibility = "non-hlsl-source"
    organization_gate = None
    if SOURCE_EXTENSION_RE.search(source_path) is not None:
        try:
            organization_gate = (_managed_source_gate(source_path)
                                 if authorization.get("viewOnlySource") else
                                 _validate_editable_source_gate(
                                     workspace, authorization, source_path))
            editor_eligible = True
            editor_eligibility = (
                "managed-source"
                if organization_gate.get("kind") == "managed-source"
                else
                "organization-source-preview"
                if organization_gate.get("kind") == "organization-source-preview"
                else "unvalidated-editable-draft"
                if organization_gate.get("kind") == "editable-draft"
                else "organized-executable-editable-hlsl"
            )
        except ShaderConsoleError:
            editor_eligibility = "organization-gate-unavailable"
    if capture_binding is not None and (
            organization_gate is None or
            organization_gate.get("kind") not in (
                "editable-draft", "organization-source-preview", "managed-source")) and (
            not authorization["lastLiveEditId"] or live is None or
            live.get("captureBinding") != capture_binding):
        return None
    if (capture_binding is not None and live is not None and
            live.get("captureBinding") != capture_binding):
        live = None
    flow_graph = None
    flow_graph_status = {
        "available": False,
        "status": "unavailable",
        "artifactPath": flow_graph_path,
    }

    if organization_gate is not None and organization_gate.get("kind") != "managed-source":
        flow_graph, flow_graph_status = _load_workspace_flow_graph(
            workspace, authorization, source_path, organization_gate)
    watch = None
    watch_id = live.get("watchId", "") if isinstance(live, dict) else ""
    watch_path = (
        "" if authorization.get("viewOnlyDraft") else
        _latest_token_json(os.path.join(workspace, "watch"), watch_id)
    )
    if watch_path:
        watch_value = _read_json(watch_path, MAX_AUDIT_BYTES, "shader watcher audit")
        watch = _validate_watch(watch_value, workspace, source_path, watch_id)
    entries = console_entries_from_audits(
        workspace, source_path, watch=watch, live=live,
        compiler_diagnostics=compiler_diagnostics)
    workspace_key = _normal_path(workspace)
    for entry in entries:
        entry["workspaceKey"] = workspace_key
    source_sha = hashlib.sha256(_read_file_bytes(source_path, MAX_SOURCE_BYTES, "managed Shader source")).hexdigest()
    manifest = _read_json(manifest_path, MAX_MANIFEST_BYTES, "shader workspace manifest")
    imported_source = (manifest.get("source") or {}).get("projectSource")
    imported_sha = imported_source.get("sourceSha256") if isinstance(imported_source, dict) else None
    # Audit history is useful diagnostics, but an orphan/stale watcher is not
    # the session used to read this source. Keep that identity explicit so a
    # capture reopen cannot strand editable drafts behind old watcher IDs.
    source_live = live
    if ((organization_gate or {}).get("kind") in (
            "editable-draft", "organization-source-preview", "managed-source") and
            (not isinstance(watch, dict) or not isinstance(live, dict) or
             live.get("watchId") != watch.get("watchId"))):
        source_live = None
    source_session = {
        "liveEditId": source_live.get("liveEditId") if source_live else None,
        "watchId": source_live.get("watchId") if source_live else None,
    }
    return {
        "authorizationId": authorization["authorizationId"],
        "workspaceId": authorization["workspaceId"],
        "workspacePath": workspace,
        "sourcePath": source_path,
        "sourceSha256": source_sha,
        "sourceSession": source_session,
        "projectSourceIdentity": _project_source_document_identity(workspace, manifest, source_sha),
        "projectSourceOrigin": _project_source_document_identity(
            workspace, manifest, imported_sha),
        "managedSourceLocator": authorization.get("viewOnlyDraft") is True,
        "manualDraft": (organization_gate or {}).get("kind") == "editable-draft",
        "activeOrganization": (authorization.get("activeOrganization") is True or
                               (organization_gate or {}).get("kind") in (
                                   "editable-draft", "organization-source-preview")),
        "captureId": authorization["captureId"],
        "captureApi": str((manifest.get("capture") or {}).get("api") or "").split(".")[-1].lower(),
        "targetProfile": copy.deepcopy(manifest.get("targetProfile")),
        "capturePath": authorization["capturePath"],
        "captureBinding": (
            live.get("captureBinding") if live is not None else
            capture_binding if (organization_gate or {}).get("kind") in (
                "editable-draft", "organization-source-preview", "managed-source")
            else None
        ),
        "shaderRawSha256": authorization["shaderRawSha256"],
        "stage": authorization["stage"],
        "entryPoint": authorization["entryPoint"],
        "resourceId": authorization["resourceId"],
        "updatedAt": authorization["updatedAt"],
        "authorizedAt": authorization["authorizedAt"],
        "editorEligible": editor_eligible,
        "editorEligibility": editor_eligibility,
        "readOnly": False,
        "sourceEditable": True,
        "flowGraph": flow_graph,
        "flowGraphStatus": flow_graph_status,
        "semanticOverview": _load_semantic_overview(workspace),
        # A live edit whose capture session expired cannot save: treat it as not ready
        # rather than presenting an editable document that fails on Ctrl+S.
        "autoApplyReady": bool(
            isinstance(live, dict) and live.get("state") == "running" and
            live.get("sessionState", "current") == "current" and
            isinstance(watch, dict) and watch.get("state") == "running" and
            ((organization_gate or {}).get("kind") != "editable-draft" or
             _draft_live_candidate_ready(workspace, organization_gate, live, watch))),
        "liveEditSessionState": (live.get("sessionState") if isinstance(live, dict) else None),
        "watch": watch,
        "liveEdit": live,
        "entries": entries,
        "status": derive_console_status(watch=watch, live=live),
        "watchedPaths": [path for path in (
            source_path, manifest_path, organization_gate_path,
            os.path.join(workspace, "ai", "semantic-overview.json"),
            os.path.dirname(organization_gate_path),
            flow_graph_path,
            live_path, watch_path, os.path.dirname(live_path) if live_path else "",
            os.path.dirname(watch_path) if watch_path else "") if path] +
            (organization_gate or {}).get("watchedPaths", []),
    }


def _managed_project_draft_authorizations(binding, diagnostics=None):
    memo = getattr(_console_read_scope, "draft_memo", None)
    if memo is None:
        return _managed_project_draft_authorizations_uncached(binding, diagnostics)
    key = (default_active_project_selection_path(), _canonical_sha256(binding),
           getattr(_console_read_scope, "draft_hint", None))
    if key not in memo:
        errors = []
        value = _managed_project_draft_authorizations_uncached(binding, errors)
        memo[key] = (value, errors)
    value, errors = memo[key]
    if diagnostics is not None:
        diagnostics.extend(errors)
    return copy.deepcopy(value)


def _managed_project_draft_authorizations_uncached(binding, diagnostics=None):
    """Discover editable text below the selected project's fixed Shader root.

    These records are in-memory UI locators. They are never written to the
    persistent live-edit authorization registry and cannot authorize a save or
    replay effect.
    """
    try:
        selection = _validate_active_project_selection(
            default_active_project_selection_path())
        _validate_active_project_workspace(selection)
        project_root = selection["projectRoot"]
        shader_root = _project_relative_path(
            project_root, ACTIVE_PROJECT_LAYOUT["shaderWorkspaces"],
            "managed Shader workspaces")
        if not os.path.isdir(shader_root) or _is_reparse(shader_root):
            return [], [shader_root]
        scan_roots = [shader_root, os.path.join(shader_root, "v2")]
        candidates = []
        watched = [selection["path"], shader_root]
        hint = getattr(_console_read_scope, "draft_hint", None)
        for scan_root in scan_roots:
            if not os.path.isdir(scan_root) or _is_reparse(scan_root):
                continue
            watched.append(scan_root)
            if hint:
                # Validate the known leaf under the selected managed root.
                # No directory search, arbitrary path or cross-project fallback.
                if (_same_path(os.path.dirname(hint), scan_root) and
                        not os.path.basename(hint).startswith(".") and
                        os.path.isdir(hint) and not _is_reparse(hint)):
                    candidates.append(hint)
                continue
            with os.scandir(scan_root) as entries:
                for entry in entries:
                    _check_console_read_cancelled()
                    if len(candidates) >= MAX_DRAFT_WORKSPACE_DISCOVERY:
                        break
                    if (entry.is_symlink() or not entry.is_dir(follow_symlinks=False)
                            or entry.name.startswith(".") or not os.path.isfile(
                                os.path.join(entry.path, "manifest.json"))):
                        continue
                    candidates.append(os.path.abspath(entry.path))
        result = []
        for workspace in candidates:
            _check_console_read_cancelled()
            manifest_path = os.path.join(workspace, "manifest.json")
            gate_path = os.path.join(workspace, "ai", "organize-code-gate.json")
            if not os.path.isfile(manifest_path):
                continue
            try:
                manifest = _read_json(manifest_path, MAX_MANIFEST_BYTES,
                                      "managed Shader workspace manifest")
                try:
                    gate = (_read_json(gate_path, MAX_MANIFEST_BYTES,
                                       "managed Shader draft gate")
                            if os.path.isfile(gate_path) else {})
                except (ShaderConsoleError, OSError, ValueError):
                    # Broken validation metadata cannot prohibit editing text.
                    # The source-only locator grants no compile/apply authority.
                    gate = {}
                capture = manifest.get("capture") or {}
                shader = manifest.get("shader") or {}
                source = manifest.get("source") or {}
                workspace_id = str(manifest.get("workspaceId") or "").lower()
                capture_id = str(capture.get("captureId") or "").lower()
                raw_sha = str(shader.get("rawSha256") or "").lower()
                stage = str(shader.get("stage") or manifest.get("stage") or "").lower()
                entry_point = str(shader.get("entryPoint") or manifest.get("entryPoint") or "")
                resource_id = shader.get("resourceId")
                if (manifest.get("schema") != WORKSPACE_SCHEMA or
                        manifest.get("schemaVersion") != 1 or
                        EXACT_TOKEN_RE.fullmatch(workspace_id) is None or
                        SHA256_RE.fullmatch(capture_id) is None or
                        SHA256_RE.fullmatch(raw_sha) is None or
                        not _same_path(capture.get("capturePath", ""), binding["capturePath"]) or
                        not stage or not entry_point or
                        type(resource_id) is not int or resource_id <= 0 or
                        source.get("encoding") != "utf-8" or
                        source.get("lane") != "executable-editable" or
                        type(source.get("compileEligible")) is not bool or
                        SOURCE_EXTENSION_RE.search(str(source.get("path") or "")) is None):
                    continue
                source_only = not (
                    source.get("lane") == "executable-editable" and
                    source.get("compileEligible") is True and
                    (gate.get("decision") == "editable-draft" or
                     (gate.get("decision") in ("drafting", "awaiting-proposal-review") and
                      isinstance(source.get("projectSource"), dict))))
                identity = {
                    "workspaceId": workspace_id,
                    "captureId": capture_id,
                    "capturePath": capture["capturePath"],
                    "shaderKey": "%s:%s:%s" % (stage, resource_id, entry_point),
                    "shaderRawSha256": raw_sha,
                    "stage": stage,
                    "entryPoint": entry_point,
                    "resourceId": resource_id,
                }
                authorization_id = hashlib.sha256(
                    ("view-only-draft\0" + workspace + "\0" + workspace_id).encode("utf-8")
                ).hexdigest()[:32]
                result.append({
                    "authorizationId": authorization_id,
                    "lastLiveEditId": "",
                    "workspacePath": workspace,
                    "workspaceId": workspace_id,
                    "captureId": capture_id,
                    "capturePath": capture["capturePath"],
                    "shaderRawSha256": raw_sha,
                    "stage": stage,
                    "entryPoint": entry_point,
                    "resourceId": resource_id,
                    "shaderKey": identity["shaderKey"],
                    "workspaceIdentitySha256": _canonical_sha256(identity),
                    "debounceMs": 100,
                    "authorizedAt": str(manifest.get("createdAt") or ""),
                    "updatedAt": str(manifest.get("updatedAt") or ""),
                    "viewOnlyDraft": True,
                    "viewOnlyOrganization": gate.get("decision") != "editable-draft",
                    "viewOnlySource": source_only,
                    "activeOrganization": gate.get("decision") in (
                        "editable-draft", "drafting", "awaiting-proposal-review"),
                })
                watched.extend((manifest_path, gate_path))
            except (ShaderConsoleError, OSError, ValueError, TypeError):
                continue
        return result, watched
    except (ShaderConsoleError, OSError, ValueError, TypeError) as error:
        selection_path = default_active_project_selection_path()
        if diagnostics is not None and os.path.exists(selection_path):
            diagnostics.append(_bounded(error, 1024))
        return [], [selection_path, _active_project_registry_path(selection_path)]

def load_console_snapshot(registry_path=None, capture_binding=None, include_console_events=True):
    """Reuse one verified project projection within this display-only batch."""
    with console_read_batch():
        return _load_console_snapshot(registry_path, capture_binding, include_console_events)


def _load_console_snapshot(registry_path=None, capture_binding=None, include_console_events=True):
    """Load the journal and sources belonging to one exact current capture."""
    _check_console_read_cancelled()
    path = os.path.abspath(str(registry_path or default_registry_path() or ""))
    selection_path = default_active_project_selection_path()
    project_registry_path = _active_project_registry_path(selection_path)
    generic = (_load_generic_console_entries() if include_console_events else
               {"entries": [], "errors": [], "watchedPaths": []})
    result = {
        "schema": CONSOLE_SCHEMA,
        "registryPath": path,
        "generatedUnixMs": int(time.time() * 1000),
        "workspaces": [],
        "alternateWorkspaces": [],
        "documentAliases": {},
        "entries": list(generic["entries"]),
        "errors": list(generic["errors"]),
        "sourceDiscoveryErrors": [],
        "watchedPaths": list(generic["watchedPaths"]) + [path, os.path.dirname(path),
            selection_path, project_registry_path, os.path.dirname(project_registry_path)],
        "captureBinding": None,
    }
    try:
        binding = normalize_capture_binding(capture_binding)
    except ShaderConsoleError as exc:
        binding = None
        result["errors"].append(_bounded(exc, 1024))
    result["captureBinding"] = binding
    if binding is None:
        result["counts"] = console_severity_counts(result["entries"])
        return result
    try:
        entries = []
        if path and os.path.isfile(path):
            try:
                registry = _read_json(path, MAX_REGISTRY_BYTES, "live-edit authorization registry")
                if registry.get("schema") != REGISTRY_SCHEMA or registry.get("schemaVersion") != 1:
                    raise ShaderConsoleError("live-edit authorization registry schema is invalid")
                entries = registry.get("entries")
                if not isinstance(entries, list) or len(entries) > MAX_AUTHORIZATIONS:
                    raise ShaderConsoleError("live-edit authorization registry entries are invalid")
            except ShaderConsoleError as exc:
                result["errors"].append(_bounded(exc, 1024))
                entries = []
        seen = set()
        registry_seen = set()
        for raw in entries:
            _check_console_read_cancelled()
            try:
                # Filter inert capture locators before touching unrelated old
                # workspaces. Deleted history must not contaminate this UI.
                identity = raw.get("workspaceIdentity") if isinstance(raw, dict) else None
                if (not isinstance(identity, dict) or not _same_path(
                        identity.get("capturePath", ""), binding["capturePath"])):
                    continue
                authorization = _validate_authorization(raw)
                key = _normal_path(authorization["workspacePath"])
                if key in registry_seen:
                    raise ShaderConsoleError("duplicate authorized shader workspace")
                registry_seen.add(key)
                model = _workspace_model(authorization, binding)
                if model is not None and model.get("captureBinding") == binding:
                    seen.add(key)
                    result["workspaces"].append(model)
            except ShaderConsoleError as exc:
                result["errors"].append(_bounded(exc, 1024))
        project_drafts, project_paths = _managed_project_draft_authorizations(binding, result["sourceDiscoveryErrors"])
        result["errors"].extend(result["sourceDiscoveryErrors"])
        result["watchedPaths"].extend(project_paths)
        for authorization in project_drafts:
            _check_console_read_cancelled()
            key = _normal_path(authorization["workspacePath"])
            if key in seen:
                continue
            try:
                model = _workspace_model(authorization, binding)
                if (model is not None and model.get("captureBinding") == binding
                        and model.get("editorEligibility") in (
                            "unvalidated-editable-draft", "organization-source-preview", "managed-source")):
                    seen.add(key)
                    result["workspaces"].append(model)
            except ShaderConsoleError as exc:
                result["errors"].append(_bounded(exc, 1024))
        result["workspaces"].sort(
            key=lambda item: (int((item.get("liveEdit") or {}).get("activeRevision", 0) or 0) > 0 and
                              (item.get("liveEdit") or {}).get("sessionState", "current") == "current",
                              item.get("autoApplyReady") is True,
                              item.get("authorizedAt", ""),
                              item.get("updatedAt", ""), item["workspacePath"]),
            reverse=True)
        # Imports of the exact same project source are one logical document,
        # even when historical catalogs stored different replay ResourceIds.
        # Retain every physical binding and alternate file for dirty buffers;
        # equal EIDs, names or executable bytes alone never merge documents.
        current = []
        document_keys = {}
        library = (_project_library_for_console(default_active_project_selection_path())
                   if any(item.get("projectSourceIdentity") or item.get("projectSourceOrigin")
                          for item in result["workspaces"]) else {})
        project_documents = {item.get("knowledgeId"): item for item in library.get("documents", [])}
        # A manual publication creates a new knowledge ID, even when an undo
        # restores exactly the imported source. The old import receipt remains
        # valid provenance; it must not hide the currently applied working copy.
        # These exact-source matches only affect presentation and deduplication.
        current_source_heads = {item["workspaceId"]: _current_project_source_head(item, library)
                                for item in result["workspaces"]}
        for model in result["workspaces"]:
            model["logicalShaderId"] = logical_shader_document_id(model)
        superseded_formats = _superseded_project_format_aliases(
            result["workspaces"], library, current_source_heads)
        replay_aliases, replay_paths = _replay_source_document_aliases(result["workspaces"], binding, library)
        result["watchedPaths"].extend(replay_paths)
        authored_sources = set(replay_aliases.values())
        # Preserved edited bytes remain the visible document even when their
        # import receipt names an older saved head. Prefer these targets before
        # exact-source grouping so aliases cannot point to another alternate.
        result["workspaces"].sort(key=lambda item: (
            item.get("manualDraft") is True, item["workspaceId"] in authored_sources), reverse=True)
        restored_originals = {}
        for item in result["workspaces"]:
            displayed_head = current_source_heads.get(item["workspaceId"]) or project_documents.get(
                (item.get("projectSourceIdentity") or {}).get("knowledgeId"), {})
            if ((item.get("projectSourceIdentity") or current_source_heads.get(item["workspaceId"])) and
                    item.get("autoApplyReady") is True and displayed_head.get("isCurrentHead") is True):
                restored_originals.setdefault((item["shaderRawSha256"], item["stage"], item["entryPoint"]), []).append(item)
        result["watchedPaths"].extend(library.get("watchedPaths", []))
        for model in result["workspaces"]:
            if model["workspaceId"] in superseded_formats and model["workspaceId"] not in authored_sources:
                model["historicalSource"] = True
                model["supersededCaptureSource"] = True
                model["supersededProjectRepresentation"] = True
                target = superseded_formats[model["workspaceId"]]
                result["documentAliases"][model["workspaceId"]] = replay_aliases.get(target, target)
                result["alternateWorkspaces"].append(model)
                continue
            if model["workspaceId"] in replay_aliases:
                model["internalReplaySource"] = True
                model["canonicalSourceWorkspaceId"] = replay_aliases[model["workspaceId"]]
                result["documentAliases"][model["workspaceId"]] = replay_aliases[model["workspaceId"]]
                result["alternateWorkspaces"].append(model)
                continue
            project_identity = model.get("projectSourceIdentity") or {}
            project_origin = model.get("projectSourceOrigin") or {}
            current_source_head = current_source_heads.get(model["workspaceId"])
            restored = restored_originals.get((model["shaderRawSha256"], model["stage"], model["entryPoint"]))
            if (model["workspaceId"] not in authored_sources and
                    not project_identity and model.get("managedSourceLocator") and
                    not model.get("autoApplyReady") and
                    model.get("editorEligibility") == "managed-source" and not model.get("activeOrganization") and restored and
                    model["resourceId"] not in {item["resourceId"] for item in restored}):
                # Pre-project decompiles have no import receipt. A disk locator
                # cannot make their old-session RID current. Preserve these
                # files separately once this original Shader has a restored head.
                model["historicalSource"] = True
                model["supersededCaptureSource"] = True
                restored_id = restored[0]["workspaceId"]
                result["documentAliases"][model["workspaceId"]] = replay_aliases.get(restored_id, restored_id)
                result["alternateWorkspaces"].append(model)
                continue
            if (model["workspaceId"] not in authored_sources and not project_identity and project_origin and
                    project_origin.get("projectId") == library.get("projectId") and
                    not model.get("activeOrganization") and
                    not model.get("autoApplyReady") and current_source_head is None):
                # Historical imports whose local bytes diverged remain recoverable
                # files. They are not current project heads, even if an old
                # catalog happened to reuse the current numeric ResourceId.
                model["historicalSource"] = True
                result["alternateWorkspaces"].append(model)
                continue
            project_document = current_source_head or (
                project_documents.get(project_identity.get("knowledgeId"))
                if project_identity.get("projectId") == library.get("projectId") else None)
            if (model["workspaceId"] not in authored_sources and
                    not model.get("activeOrganization") and
                    project_document and project_document.get("isCurrentHead") is False):
                model["historicalSource"] = True
                result["alternateWorkspaces"].append(model)
                continue
            key = _workspace_document_key(model)
            if current_source_head is not None:
                key = ("project", library["projectId"], current_source_head["knowledgeId"],
                       model["shaderRawSha256"], model["stage"], model["entryPoint"], model["sourceSha256"])
            if key not in document_keys:
                document_keys[key] = model
                current.append(model)
            else:
                preferred = document_keys[key]
                result["documentAliases"][model["workspaceId"]] = preferred["workspaceId"]
                result["alternateWorkspaces"].append(model)
                preferred.setdefault("relatedWorkspaceBindings", []).append({
                    field: model.get(field) for field in (
                        "workspaceId", "workspacePath", "captureId", "capturePath", "resourceId",
                        "shaderRawSha256", "stage", "entryPoint", "sourcePath")})
        result["workspaces"] = current
        result["watchedPaths"] = (result["watchedPaths"] + list(generic["watchedPaths"]) +
                                  [path, os.path.dirname(path)] + project_paths)
        for workspace in result["workspaces"] + result["alternateWorkspaces"]:
            result["watchedPaths"].extend(workspace.get("watchedPaths", []))
            result["entries"].extend(workspace.get("entries", []))
    except ShaderConsoleError as exc:
        result["errors"].append(_bounded(exc, 1024))
    # De-duplicate paths while retaining order. They are used only as
    # QFileSystemWatcher inputs, never as executable inputs.
    unique = []
    known = set()
    for item in result["watchedPaths"]:
        key = _normal_path(item)
        # Missing metadata must remain in the stat signature so its creation
        # invalidates an empty/error snapshot. Qt watcher callers separately
        # select existing files/directories before installing native watches.
        if key not in known and item and os.path.isabs(item):
            known.add(key)
            unique.append(item)
    result["watchedPaths"] = unique
    _append_snapshot_error_entries(result)
    result["entries"].sort(key=lambda item: (
        item.get("at", ""), item.get("unixMs", 0), item.get("streamKey", ""),
        item.get("sequence", 0)))
    result["entries"] = result["entries"][-MAX_CONSOLE_ENTRIES:]
    result["counts"] = console_severity_counts(result["entries"])
    return result


def _console_entry_before(entry, unix_ms):
    timestamp = entry.get("unixMs", 0)
    if isinstance(timestamp, int) and timestamp > 0:
        return timestamp < unix_ms
    # Audit producers use UTC text rather than unixMs. Keep undated diagnostics
    # visible, and do not let the first asynchronous load hide newer events.
    at = str(entry.get("at", ""))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", at) is None:
        return False
    fraction = at[20:-1] if len(at) > 20 else ""
    normalized = at[:19] + "." + fraction.ljust(9, "0") + "Z"
    cutoff = _utc_now_text(unix_ms)
    return normalized < cutoff[:23] + "000000Z"


if HAS_QT:
    class SourceLocationDialog(QtWidgets.QDialog):
        """Read-only exact-line source view; never writes or launches a command."""

        def __init__(self, source_path, line, column, parent=None):
            super(SourceLocationDialog, self).__init__(parent)
            self.setWindowTitle("着色器源码")
            self.resize(1000, 720)
            layout = QtWidgets.QVBoxLayout(self)
            location = "%s:%d:%d" % (source_path, max(line, 1), max(column, 1))
            label = QtWidgets.QLabel(location, self)
            label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            layout.addWidget(label)
            self.editor = QtWidgets.QPlainTextEdit(self)
            self.editor.setReadOnly(True)
            self.editor.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
            font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
            self.editor.setFont(font)
            layout.addWidget(self.editor, 1)
            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.Close, parent=self)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)
            raw = _read_file_bytes(
                source_path, MAX_SOURCE_BYTES, "workspace shader source")
            self.editor.setPlainText(raw.decode("utf-8", errors="replace"))
            block = self.editor.document().findBlockByNumber(max(line, 1) - 1)
            cursor = QtGui.QTextCursor(block)
            if column > 1:
                cursor.movePosition(
                    QtGui.QTextCursor.Right, QtGui.QTextCursor.MoveAnchor,
                    max(column - 1, 0))
            self.editor.setTextCursor(cursor)
            self.editor.centerCursor()
            selection = QtWidgets.QTextEdit.ExtraSelection()
            selection.cursor = QtGui.QTextCursor(block)
            selection.cursor.clearSelection()
            selection.format.setProperty(
                QtGui.QTextFormat.FullWidthSelection, True)
            selection.format.setBackground(QtGui.QColor(120, 20, 20, 125))
            selection.format.setForeground(QtGui.QColor(255, 230, 230))
            self.editor.setExtraSelections([selection])


    class ReplayLabConsolePanel(object):
        """A dockable, continuously refreshed general ReplayLab console."""

        POLL_INTERVAL_MS = 100
        # The short timer consumes completed work; it must not rescan the
        # entire project on every tick. Native file notifications refresh
        # immediately, with a bounded fallback for missed/atomic notifications.
        FALLBACK_SCAN_SECONDS = 2.0
        FILESYSTEM_COALESCE_MS = 20

        def __init__(self, ctx, qrd_module, registry_path=None, closed_callback=None,
                     capture_binding_provider=None):
            self._opened_unix_ms = int(time.time() * 1000)
            self.capture_binding_provider = capture_binding_provider
            self.ctx = ctx
            self.qrd = qrd_module
            self.registry_path = registry_path or default_registry_path()
            self.closed_callback = closed_callback
            self.top = QtWidgets.QWidget(ctx.GetMainWindow().Widget())
            self.top.setObjectName("breadConsole")
            self.top.setProperty("breadPreserveOnLayoutRestore", True)
            self.top.setWindowTitle("控制台")
            self.top.destroyed.connect(self._closed)
            root = QtWidgets.QVBoxLayout(self.top)
            root.setContentsMargins(5, 3, 5, 3)
            root.setSpacing(3)
            # One header row, Unity-style: scope, severity toggles, status,
            # then the two actions. The message list gets the rest of the dock.
            header = QtWidgets.QHBoxLayout()
            header.setSpacing(4)
            self.workspace_combo = QtWidgets.QComboBox(self.top)
            self.workspace_combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            self.workspace_combo.setMinimumContentsLength(14)
            self.workspace_combo.currentIndexChanged.connect(self._render_selected)
            header.addWidget(self.workspace_combo, 2)
            self.info_filter = QtWidgets.QToolButton(self.top)
            self.warning_filter = QtWidgets.QToolButton(self.top)
            self.error_filter = QtWidgets.QToolButton(self.top)
            self._filter_buttons = {
                "info": self.info_filter,
                "warning": self.warning_filter,
                "error": self.error_filter,
            }
            for button in self._filter_buttons.values():
                button.setCheckable(True)
                button.setChecked(True)
                button.setAutoRaise(True)
                button.toggled.connect(self._render_selected)
                header.addWidget(button)
            self.status_label = QtWidgets.QLabel("等待消息", self.top)
            self.status_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            self.status_label.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
            header.addWidget(self.status_label, 3)
            self.progress = QtWidgets.QProgressBar(self.top)
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress.setTextVisible(False)
            self.progress.setFixedWidth(90)
            self.progress.setMaximumHeight(12)
            self.progress.setVisible(False)
            header.addWidget(self.progress)
            self.refresh_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Refresh"), self.top)
            self.refresh_button.clicked.connect(lambda _checked=False: self.refresh(True))
            header.addWidget(self.refresh_button)
            self.clear_button = QtWidgets.QPushButton("清空", self.top)
            self.clear_button.setToolTip(
                "只清理当前控制台视图；不会删除审计、授权或原始日志")
            self.clear_button.clicked.connect(self._clear_messages)
            header.addWidget(self.clear_button)
            root.addLayout(header)
            self.tree = QtWidgets.QTreeWidget(self.top)
            self.tree.setHeaderLabels(("时间", "模块", "阶段", "信息", "位置"))
            self.tree.setRootIsDecorated(False)
            self.tree.setAlternatingRowColors(True)
            self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            self.tree.itemSelectionChanged.connect(self._selection_changed)
            self.tree.itemDoubleClicked.connect(self._item_open)
            self.tree.header().setStretchLastSection(False)
            self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
            self.tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
            self.tree.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
            self.tree.header().setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
            self.tree.header().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
            self.detail = QtWidgets.QTextEdit(self.top)
            self.detail.setReadOnly(True)
            self.detail.setMinimumHeight(40)
            # Message list above, detail box below; the user drags the handle
            # to give either pane the room it needs (Unity-style console).
            self.splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical, self.top)
            self.splitter.setChildrenCollapsible(False)
            self.splitter.addWidget(self.tree)
            self.splitter.addWidget(self.detail)
            self.splitter.setStretchFactor(0, 4)
            self.splitter.setStretchFactor(1, 1)
            self.splitter.setSizes([320, 110])
            root.addWidget(self.splitter, 1)
            actions = QtWidgets.QHBoxLayout()
            self.open_button = QtWidgets.QPushButton("定位到源码", self.top)
            self.open_button.setEnabled(False)
            self.open_button.clicked.connect(self._open_selected)
            actions.addWidget(self.open_button)
            actions.addStretch(1)
            root.addLayout(actions)
            self.file_watcher = QtCore.QFileSystemWatcher(self.top)
            self.file_watcher.fileChanged.connect(self._filesystem_changed)
            self.file_watcher.directoryChanged.connect(self._filesystem_changed)
            self.change_timer = QtCore.QTimer(self.top)
            self.change_timer.setSingleShot(True)
            try:
                self.change_timer.setTimerType(QtCore.Qt.PreciseTimer)
            except Exception:
                pass
            self.change_timer.setInterval(self.FILESYSTEM_COALESCE_MS)
            self.change_timer.timeout.connect(self._flush_filesystem_changes)
            self.timer = QtCore.QTimer(self.top)
            try:
                self.timer.setTimerType(QtCore.Qt.PreciseTimer)
            except Exception:
                pass
            self.timer.setInterval(self.POLL_INTERVAL_MS)
            self.timer.timeout.connect(self.refresh)
            self.timer.start()
            self._docked = False
            self._alive = True
            self._snapshot = None
            self._selected_workspace_key = "__all__"
            self._clear_cursors = {}
            self._source_dialogs = []
            self._watched_paths = [self.registry_path]
            self._last_disk_signature = None
            self._visible_entries = []
            self._snapshot_reader = _ConsoleSnapshotReader()
            self._snapshot_request = None
            self._refresh_pending = False
            self._clear_initial_snapshot = True
            self.refresh(True)
            # Each qrenderdoc process should open onto an empty Console view.
            # Journals remain on disk; this only advances the same view watermark
            # as the Unity-style Clear button.
            self._clear_messages()

        def _closed(self, _object=None):
            if not self._alive:
                return
            self._alive = False
            if self._snapshot_request is not None:
                self._snapshot_request["stop"].set()
            try:
                self.timer.stop()
            except Exception:
                pass
            try:
                self.change_timer.stop()
            except Exception:
                pass
            try:
                self.file_watcher.removePaths(self.file_watcher.files())
                self.file_watcher.removePaths(self.file_watcher.directories())
            except Exception:
                pass
            self._snapshot_reader.shutdown()
            callback = self.closed_callback
            self.closed_callback = None
            self._source_dialogs = []
            self.top = None
            if callback:
                callback()

        # The console is a strip along the bottom of the main area, like a game
        # engine's: it takes this share of the height of whatever it docks under
        # and never becomes a full central tab unless nothing else is open.
        BOTTOM_SHARE = 0.22

        def _bottom_anchor(self):
            """The widget the console docks under: the central viewers, in order."""
            for has, get in (("HasTextureViewer", "GetTextureViewer"),
                             ("HasPipelineViewer", "GetPipelineViewer"),
                             ("HasEventBrowser", "GetEventBrowser")):
                try:
                    if getattr(self.ctx, has)():
                        widget = getattr(self.ctx, get)().Widget()
                        if widget is not None:
                            return widget
                except Exception:
                    continue
            return None

        def show(self):
            if not self._alive or self.top is None:
                raise ShaderConsoleError("Bread Console has already been closed")
            if not self._docked:
                anchor = self._bottom_anchor()
                docked = False
                if anchor is not None:
                    try:
                        self.ctx.AddDockWindow(self.top, self.qrd.DockReference.BottomOf,
                                               anchor, self.BOTTOM_SHARE)
                        docked = True
                    except Exception:
                        docked = False
                if not docked:
                    self.ctx.AddDockWindow(self.top, self.qrd.DockReference.MainToolArea,
                                           None, self.BOTTOM_SHARE)
                self._docked = True
            self.ctx.RaiseDockWindow(self.top)

        def shutdown(self):
            if not self._alive:
                self._snapshot_reader.shutdown()
                return
            try:
                self.timer.stop()
            except Exception:
                pass
            for dialog in list(self._source_dialogs):
                try:
                    dialog.close()
                except Exception:
                    pass
            self._source_dialogs = []
            widget = self.top
            self._closed()
            if widget is not None:
                try:
                    widget.close()
                    widget.deleteLater()
                except Exception:
                    pass

        def _filesystem_changed(self, _path):
            # Atomic audit replacement can remove a QFileSystemWatcher file
            # subscription. Coalesce a save's file+directory burst into one
            # short deferred refresh and then rebuild every subscription.
            self._filesystem_paths = getattr(self, "_filesystem_paths", set())
            self._filesystem_paths.add(str(_path))
            if not self.change_timer.isActive():
                self.change_timer.start(self.FILESYSTEM_COALESCE_MS)

        def _flush_filesystem_changes(self):
            # Stat signatures distinguish source changes from journal appends.
            # Only an explicit Refresh asks for a complete source revalidation.
            self._filesystem_dirty = True
            self.refresh(False)

        def _disk_signature(self):
            return _console_paths_signature(self._watched_paths)

        def _reset_watched_paths(self, paths):
            # Directory notifications cover saves and atomic renames. Watching
            # every immutable artifact separately multiplies Windows handles
            # and churns native watch threads on every console publication.
            directories = set()
            files = set()
            for path in paths:
                directory = path if os.path.isdir(path) else os.path.dirname(path)
                if directory and os.path.isdir(directory):
                    directories.add(directory)
                if os.path.basename(os.path.dirname(path)) == "source" and os.path.isfile(path):
                    files.add(path)
            current = set(self.file_watcher.files() + self.file_watcher.directories())
            desired = directories | files
            removed = sorted(current - desired)
            added = sorted(desired - current)
            if removed:
                self.file_watcher.removePaths(removed)
            if added:
                self.file_watcher.addPaths(added)

        def _current_capture_binding(self):
            try:
                value = self.capture_binding_provider() if callable(
                    self.capture_binding_provider) else None
                return normalize_capture_binding(value)
            except ShaderConsoleError:
                return None

        def capture_changed(self, binding=None):
            if not self._alive:
                return
            for dialog in list(self._source_dialogs):
                dialog.close()
            self._source_dialogs = []
            self._last_disk_signature = None
            if self._snapshot_request is not None:
                self._snapshot_request["stop"].set()
            self.refresh(True)

        def refresh(self, force=False):
            if not self._alive:
                return
            binding = self._current_capture_binding()
            self._refresh_pending = self._refresh_pending or bool(force)
            completed = self._snapshot_reader.take()
            if completed is not None:
                request, result = completed
                self._snapshot_request = None
                if (not request["stop"].is_set() and
                        request["captureBinding"] == binding):
                    if "snapshot" in result:
                        self._publish_snapshot(result["snapshot"], result["signature"])
                    elif result.get("error"):
                        self.detail.setPlainText(result["error"])
            if self._snapshot_request is not None:
                if self._snapshot_request["captureBinding"] != binding:
                    self._snapshot_request["stop"].set()
                    self._refresh_pending = True
                return
            now = time.monotonic()
            scope_changed = (self._snapshot or {}).get("captureBinding") != binding
            if (not self._refresh_pending and not scope_changed
                    and not getattr(self, "_filesystem_dirty", False)
                    and now < getattr(self, "_next_snapshot_poll", 0.0)):
                return
            self._filesystem_dirty = False
            self._next_snapshot_poll = now + self.FALLBACK_SCAN_SECONDS
            full_due = now >= getattr(self, "_next_full_snapshot", 0.0)
            if full_due:
                self._next_full_snapshot = now + 30.0
            request = {
                "registryPath": self.registry_path,
                "captureBinding": binding,
                "watchedPaths": list(self._watched_paths),
                "previousSignature": self._last_disk_signature,
                "force": self._refresh_pending or scope_changed,
                "changedPaths": list(getattr(self, "_filesystem_paths", set())),
                "fullObservationDue": full_due,
                "stop": threading.Event(),
            }
            self._filesystem_paths = set()
            self._refresh_pending = False
            self._snapshot_request = request
            self._snapshot_reader.submit(request)

        def _publish_snapshot(self, snapshot, signature):
            previous_key = self._selected_workspace_key
            self._snapshot = snapshot
            self.workspace_combo.blockSignals(True)
            self.workspace_combo.clear()
            selected_index = 0
            self.workspace_combo.addItem("全部消息", "__all__")
            for index, workspace in enumerate(snapshot["workspaces"]):
                key = _normal_path(workspace["workspacePath"])
                label = "%s %s · Shader %d · %s" % (
                    workspace["stage"], workspace["entryPoint"],
                    workspace["resourceId"], os.path.basename(workspace["workspacePath"]))
                self.workspace_combo.addItem(label, key)
                if key == previous_key:
                    selected_index = index + 1
            self.workspace_combo.setCurrentIndex(selected_index)
            self._selected_workspace_key = str(
                self.workspace_combo.itemData(selected_index) or "__all__")
            self.workspace_combo.blockSignals(False)
            self._watched_paths = snapshot.get("watchedPaths", []) or [self.registry_path]
            self._reset_watched_paths(self._watched_paths)
            self._last_disk_signature = signature
            if self._clear_initial_snapshot:
                self._clear_initial_snapshot = False
                self._clear_messages(before_unix_ms=self._opened_unix_ms)
            self._render_selected()

        def _selected_workspace(self):
            if not self._snapshot:
                return None
            index = self.workspace_combo.currentIndex()
            values = self._snapshot.get("workspaces", [])
            if index <= 0 or index - 1 >= len(values):
                return None
            return values[index - 1]

        def _scope_entries(self):
            if not self._snapshot:
                return []
            workspace = self._selected_workspace()
            if workspace is None:
                return self._snapshot.get("entries", [])
            return workspace.get("entries", [])

        def _clear_messages(self, _checked=False, before_unix_ms=None):
            if not self._snapshot:
                return
            # Unity-style Clear is deliberately a view watermark. Original
            # security audits, authorisations and append-only journals remain.
            all_entries = list(self._snapshot.get("entries", []))
            for workspace in self._snapshot.get("workspaces", []):
                all_entries.extend(workspace.get("entries", []))
            if before_unix_ms is not None:
                all_entries = [entry for entry in all_entries
                               if _console_entry_before(entry, before_unix_ms)]
            latest = build_console_clear_cursors(all_entries)
            for stream, sequence in latest.items():
                if sequence > self._clear_cursors.get(stream, 0):
                    self._clear_cursors[stream] = sequence
            self._render_selected()

        def _enabled_severities(self):
            return dict((severity, button.isChecked())
                        for severity, button in self._filter_buttons.items())

        def _set_filter_counts(self, counts):
            self.info_filter.setText("普通 (%d)" % counts.get("info", 0))
            self.warning_filter.setText("警告 (%d)" % counts.get("warning", 0))
            self.error_filter.setText("错误 (%d)" % counts.get("error", 0))
            self.warning_filter.setStyleSheet("color: #e6b84a;")
            self.error_filter.setStyleSheet("color: #ff5f5f;")

        def _render_selected(self, _index=None):
            if not self._alive:
                return
            workspace = self._selected_workspace()
            self.tree.clear()
            self.detail.clear()
            self.open_button.setEnabled(False)
            entries = console_entries_after_clear(
                self._scope_entries(), self._clear_cursors)
            counts = console_severity_counts(entries)
            self._set_filter_counts(counts)
            visible = filter_console_entries(entries, self._enabled_severities())
            if workspace is None:
                self._selected_workspace_key = "__all__"
                errors = (self._snapshot or {}).get("errors", [])
                if errors:
                    self.status_label.setText(
                        "控制台 · %s" % errors[0])
                    self.status_label.setStyleSheet("color: #ff6666;")
                else:
                    self.status_label.setText("共 %d 条" % len(entries))
                    self.status_label.setStyleSheet("")
                self.progress.setValue(0)
                self.progress.setVisible(False)
            else:
                self._selected_workspace_key = _normal_path(workspace["workspacePath"])
                status = workspace["status"]
                self.status_label.setText("%s · %s" % (status["phase"], status["text"]))
                color = {
                    "error": "#ff6666", "success": "#79d279",
                    "progress": "#66b3ff", "warning": "#ffcc66",
                }.get(status["severity"], "")
                self.status_label.setStyleSheet(
                    "color: %s; font-weight: 600;" % color if color else "")
                self.progress.setValue(status["percent"])
                self.progress.setVisible(0 < status["percent"] < 100)
            severity_colors = {
                "error": QtGui.QColor("#ff5f5f"),
                "warning": QtGui.QColor("#e6b84a"),
            }
            # Entries stay in Python. Handing the dict to Qt as item data made
            # PySide2 convert every value to a QVariant, and on Windows a Python int
            # above 2**31 (unixMs) fails that conversion with a bare OverflowError
            # -- which is what "the console does not open" looked like.
            self._visible_entries = list(visible)
            for index, entry in enumerate(visible):
                location = ""
                if entry.get("sourceFile") and entry.get("line"):
                    location = "%s:%d%s" % (
                        os.path.basename(entry["sourceFile"]), entry["line"],
                        ":%d" % entry["column"] if entry.get("column") else "")
                item = QtWidgets.QTreeWidgetItem((
                    entry.get("at", "").replace("T", " ").replace("Z", ""),
                    entry.get("module", "system"), entry.get("phase", ""),
                    entry.get("text", ""), location))
                item.setData(0, QtCore.Qt.UserRole, index)
                item.setToolTip(3, entry.get("text", ""))
                brush_color = severity_colors.get(entry.get("severity"))
                if brush_color is not None:
                    brush = QtGui.QBrush(brush_color)
                    for column in range(5):
                        item.setForeground(column, brush)
                self.tree.addTopLevelItem(item)
            if self.tree.topLevelItemCount():
                latest = self.tree.topLevelItem(self.tree.topLevelItemCount() - 1)
                self.tree.setCurrentItem(latest)
                self.tree.scrollToItem(latest)

        def _selection_changed(self):
            items = self.tree.selectedItems()
            if not items:
                self.open_button.setEnabled(False)
                self.detail.clear()
                return
            entry = self._entry_for_item(items[0])
            if not isinstance(entry, dict):
                return
            location = ""
            if entry.get("sourceFile") and entry.get("line"):
                location = "\n%s:%d:%d" % (
                    entry["sourceFile"], entry["line"],
                    entry.get("column") or 1)
            self.detail.setPlainText("[%s/%s/%s] %s%s" % (
                entry.get("severity", "info"), entry.get("module", "system"),
                entry.get("phase", ""), entry.get("text", ""), location))
            self.open_button.setEnabled(bool(
                entry.get("sourceFile") and entry.get("line")))

        def _entry_for_item(self, item):
            try:
                index = int(item.data(0, QtCore.Qt.UserRole))
            except (TypeError, ValueError):
                return None
            entries = getattr(self, "_visible_entries", [])
            return entries[index] if 0 <= index < len(entries) else None

        def _item_open(self, item, _column):
            self._open_location(self._entry_for_item(item))

        def _open_selected(self):
            items = self.tree.selectedItems()
            if items:
                self._open_location(self._entry_for_item(items[0]))

        def _workspace_for_entry(self, entry):
            key = str(entry.get("workspaceKey", "") or "")
            if not key:
                return None
            for workspace in (self._snapshot or {}).get("workspaces", []):
                if _normal_path(workspace.get("workspacePath", "")) == key:
                    return workspace
            return None

        def _open_location(self, entry):
            if not isinstance(entry, dict):
                return
            workspace = self._workspace_for_entry(entry)
            binding = self._current_capture_binding()
            if (not workspace or not entry.get("sourceFile") or
                    not entry.get("line") or binding is None or
                    workspace.get("captureBinding") != binding):
                return
            source_path = entry["sourceFile"]
            # Re-check the fixed authorization boundary at click time. Compiler
            # text can never redirect this button to another file.
            source_root = os.path.join(workspace["workspacePath"], "source")
            if (not _within(source_root, source_path) or not os.path.isfile(source_path)
                    or _is_reparse(source_path)):
                QtWidgets.QMessageBox.critical(
                    self.top, "控制台",
                    "源码路径已改变或离开授权工作区，已拒绝打开。")
                return
            try:
                _authorized_capture_live(dict(
                    workspace, lastLiveEditId=(workspace.get("liveEdit") or {}).get(
                        "liveEditId", "")), binding)
                dialog = SourceLocationDialog(
                    source_path, entry.get("line") or 1,
                    entry.get("column") or 1, self.top)
                if binding != self._current_capture_binding():
                    dialog.close()
                    dialog.deleteLater()
                    return
                dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
                dialog.destroyed.connect(
                    lambda _object=None, selected=dialog: self._forget_dialog(selected))
                self._source_dialogs.append(dialog)
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
            except Exception as exc:
                QtWidgets.QMessageBox.critical(
                    self.top, "控制台", _bounded(exc, 2048))

        def _forget_dialog(self, dialog):
            try:
                self._source_dialogs.remove(dialog)
            except ValueError:
                pass

    # Compatibility surface for older installed bridge registrations.
    ShaderConsolePanel = ReplayLabConsolePanel

else:
    class ReplayLabConsolePanel(object):  # pragma: no cover - defensive import surface
        def __init__(self, *args, **kwargs):
            raise ShaderConsoleError("PySide2 is unavailable for Bread Console")

    ShaderConsolePanel = ReplayLabConsolePanel
