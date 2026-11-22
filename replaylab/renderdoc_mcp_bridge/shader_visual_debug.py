"""Ephemeral Shader value previews (embedded Python 3.6 compatible).

The default resource override follows the selected Shader through every Draw.
It never publishes workspace source or frees another owner's replacement.
All native objects are accessed on the replay thread through ReplayDriver.
"""
import collections
import copy
import hashlib
import importlib.util
import json
import os
import posixpath
import re
import threading
import time
import uuid
import tempfile
from types import SimpleNamespace


SCHEMA = "bread-shader-visual-debug/v1"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_CACHE_TARGETS = 24
MAX_PLANS = 12
MAX_ANALYSIS_TOKENS = 100000
UNSET_TARGET = 0xffffffff
RESOURCE_SCOPE = "shader-resource"
EVENT_SCOPE = "event"


def _display_reflection_snapshot(bridge, reflection):
    """Own only display data; newer bindings intentionally forbid reflection copies.

    ConstantBlock is a value type whose copy owns its nested member arrays.
    Never retain wrappers borrowed from a Shader that can be replaced/freed.
    """
    return SimpleNamespace(rawBytes=bytes(bridge._shader_raw_bytes(reflection)),
                           constantBlocks=[bridge.rd.ConstantBlock(block)
                                           for block in reflection.constantBlocks])


class DebugError(RuntimeError):
    def __init__(self, reason, message):
        super(DebugError, self).__init__(message)
        self.reason = reason


def _scope(params):
    scope = str(params.get("scope") or RESOURCE_SCOPE)
    if scope not in (RESOURCE_SCOPE, EVENT_SCOPE):
        raise DebugError("invalid-scope", "Unknown Shader preview scope")
    return scope


def _digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _path(value):
    return os.path.normcase(os.path.realpath(os.path.abspath(value)))


def _source_bytes(source):
    if not isinstance(source, str) or not source or "\0" in source:
        raise DebugError("invalid-source", "Shader source must be non-empty UTF-8 text")
    raw = source.encode("utf-8", errors="strict")
    if len(raw) > MAX_SOURCE_BYTES:
        raise DebugError("invalid-source", "Shader source exceeds the preview size limit")
    return raw


def source_snapshot(params):
    """Freeze the edited file and its local include graph without changing disk.

    This is a graph of immutable files, never a textual include expander. The
    real compiler processes conditional directives, guards, pragma once and
    repeated includes in an isolated mirror. Missing literal includes are
    recorded too: an inactive branch may legitimately name a nonexistent file,
    and creating that file later must invalidate the graph's cache identity.
    """
    edited = _source_bytes(params.get("source"))
    declared_hash = str(params.get("sourceSha256") or "")
    if declared_hash and _digest(edited) != declared_hash:
        raise DebugError("stale-source", "Editor source no longer matches its selection")
    start = int(params.get("byteStart", 0))
    end = int(params.get("byteEnd", start))
    if start < 0 or end < start or end > len(edited):
        raise DebugError("invalid-selection", "The selected UTF-8 byte range is invalid")
    # Validate boundaries, not just lengths: Scintilla reports UTF-8 bytes.
    edited[:start].decode("utf-8", errors="strict")
    edited[:end].decode("utf-8", errors="strict")
    edited_path = _path(params.get("sourcePath") or "shader.hlsl")
    root_path = _path(params.get("rootSourcePath") or edited_path)
    boundary = _path(params.get("includeRoot")) if params.get("includeRoot") else os.path.dirname(root_path)
    dependencies, files, visited, blocked_includes = {}, {}, set(), {}
    size = [0]
    needs_preprocess = [root_path != edited_path]
    directive = re.compile(br'^\s*#\s*include\s*(["<])([^"<>\r\n]+)[">]\s*(?://[^\r\n]*)?(?:\r?\n)?$')
    include_prefix = re.compile(br'^\s*#\s*include\b')

    def relative_name(filename):
        try:
            contained = os.path.commonpath([boundary, filename]) == boundary
        except ValueError:
            contained = False
        if not contained:
            raise DebugError("include-outside-unit", "Shader include is outside its compilation unit: " + filename)
        return os.path.relpath(filename, boundary).replace(os.sep, "/")

    def gather(filename, depth=0, required=False):
        filename = _path(filename)
        name = relative_name(filename)
        if filename in visited:
            return
        if depth >= 64 or len(visited) >= 4096:
            raise DebugError("include-limit", "Shader include graph exceeds its depth or file limit")
        # A cycle is a graph edge, not a preprocessing decision. Include guards
        # may make it valid; an actual unguarded cycle is diagnosed by the compiler.
        visited.add(filename)
        try:
            if filename == edited_path:
                raw = edited
            else:
                with open(filename, "rb") as handle:
                    raw = handle.read(MAX_SOURCE_BYTES + 1)
            text = raw.decode("utf-8", errors="strict")
        except FileNotFoundError as error:
            if required:
                raise DebugError("include-unavailable", str(error))
            dependencies[filename] = "missing"
            return
        except (OSError, UnicodeError) as error:
            raise DebugError("include-unavailable", str(error))
        if len(raw) > MAX_SOURCE_BYTES or b"\0" in raw:
            raise DebugError("invalid-source", "Shader include exceeds its size limit or contains NUL")
        size[0] += len(raw)
        if size[0] > MAX_SOURCE_BYTES:
            raise DebugError("invalid-source", "Shader compilation graph exceeds the preview size limit")
        dependencies[filename] = _digest(raw)
        files[name] = text
        cursor = 0
        # A directive inside a block comment is not an include. Keep byte
        # offsets/newlines intact while hiding comments for directive discovery.
        def hide_comment(match):
            value = match.group(0)
            return re.sub(br'[^\r\n]', b' ', value) if value.startswith((b'//', b'/*')) else value
        scan = re.sub(br'//[^\r\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"', hide_comment, raw)
        for line in scan.splitlines(True):
            if include_prefix.match(line):
                needs_preprocess[0] = True
                if filename == edited_path and cursor <= start < cursor + len(line):
                    raise DebugError("invalid-selection", "Select a value expression, not an include directive")
            match = directive.match(line)
            if match:
                include_name = match.group(2).decode("utf-8")
                absolute = (os.path.isabs(include_name) or include_name.startswith(("/", "\\")) or
                            re.match(r"^[A-Za-z]:[\\/]", include_name))
                # Captured Windows sources can use backslashes when replayed
                # on a Unix host. Mirror their relative directory identity.
                portable_name = include_name.replace("\\", os.sep).replace("/", os.sep)
                include_path = _path(os.path.join(os.path.dirname(filename), portable_name))
                try:
                    inside = not absolute and os.path.commonpath([boundary, include_path]) == boundary
                except ValueError:
                    inside = False
                if inside:
                    gather(include_path, depth + 1)
                else:
                    # Never read outside the chosen source unit. The compiler
                    # decides whether this directive is active, using a missing
                    # in-mirror filename in place of the forbidden path.
                    blocked_includes.setdefault(name, []).append(include_name)
            cursor += len(line)

    root_name, edited_name = relative_name(root_path), relative_name(edited_path)
    gather(root_path, required=True)
    # Supply the editor overlay even when it is not a literal graph edge.
    # The isolated compiler rejects active computed includes: their transitive
    # dependencies cannot be frozen by this source snapshot.
    gather(edited_path, required=True)
    graph_identity = {"rootName": root_name, "editedName": edited_name,
                      "dependencies": dependencies}
    return {"source": edited.decode("utf-8"), "byteStart": start,
            "byteEnd": end, "dependencies": dependencies, "files": files,
            "rootName": root_name, "editedName": edited_name, "blockedIncludes": blocked_includes,
            "needsPreprocess": needs_preprocess[0],
            "unitSha256": _digest(graph_identity), "sourceSha256": _digest(edited),
            "originalByteStart": start, "originalByteEnd": end,
            "editedPath": edited_path, "rootPath": root_path}


def _load_instrumenter():
    path = os.path.join(os.path.dirname(__file__), "shader_visual_instrument.py")
    spec = importlib.util.spec_from_file_location("_bread_shader_visual_instrument", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _public_selection(plan, snapshot):
    selection = dict(plan.get("selection") or {})
    displacement = snapshot["byteStart"] - snapshot["originalByteStart"]
    selection["byteStart"] = int(selection.get("byteStart", snapshot["byteStart"])) - displacement
    selection["byteEnd"] = int(selection.get("byteEnd", snapshot["byteEnd"])) - displacement
    return selection


class Manager(object):
    """Latest-request-wins sessions with bounded replay-local target ownership."""
    def __init__(self, driver, instrumenter=None):
        self.driver = driver
        self.instrumenter = instrumenter
        self._lock = threading.RLock()
        self._generation_lock = threading.Lock()
        self._generations = {}
        self._session = None
        self._cache = collections.OrderedDict()
        self._plans = collections.OrderedDict()
        self._analysis_source = None
        self._source_model = None
        self._anchors = collections.OrderedDict()
        self._capture = None
        self._source_directories = []
        self._closing = False

    def invalidate(self, params):
        owner = str(params.get("ownerId") or "workspace")
        generation = int(params.get("requestGeneration", 0))
        with self._generation_lock:
            self._generations[owner] = max(generation, self._generations.get(owner, -1))
        return {"schema": SCHEMA, "status": "invalidated"}

    def _current(self, owner, generation):
        with self._generation_lock:
            return self._generations.get(owner, -1) == generation

    def _require_current(self, owner, generation):
        if not self._current(owner, generation):
            raise DebugError("superseded", "A newer preview request superseded this result")

    def operation(self, operation, params):
        params = dict(params or {})
        owner = str(params.get("ownerId") or "workspace")
        generation = int(params.get("requestGeneration", 0))
        self.invalidate(params)
        if operation == "invalidate":
            return {"schema": SCHEMA, "status": "invalidated"}
        try:
            with self._lock:
                if self._closing:
                    raise DebugError("session-closing", "Shader preview is shutting down")
                self._require_current(owner, generation)
                if operation == "stop":
                    return self._stop(owner, params)
                if operation == "compare":
                    return self._compare(owner, params, generation)
                if operation == "status":
                    return self._receipt(self._session)
                if operation == "capture-source":
                    source = self.driver.capture_source(params)
                    if "files" in source:
                        directory = tempfile.TemporaryDirectory(prefix="bread-shader-source-")
                        self._source_directories.append(directory)
                        files = source.pop("files")
                        for name, contents in files.items():
                            destination = os.path.join(directory.name, name)
                            os.makedirs(os.path.dirname(destination), exist_ok=True)
                            with open(destination, "w", encoding="utf-8", newline="") as handle:
                                handle.write(contents)
                        source["sourcePath"] = source["rootSourcePath"] = os.path.join(directory.name, source.pop("rootName"))
                        source["includeRoot"] = directory.name
                    source["workspaceId"] = uuid.uuid4().hex
                    source.update(status="ready", schema=SCHEMA, captureBinding=dict(params.get("captureBinding") or {}))
                    return source
                if operation not in ("inspect", "start", "follow"):
                    raise DebugError("invalid-operation", "Unknown Shader preview operation")
                try:
                    if operation == "follow":
                        return self._follow(params, owner, generation)
                    return self._prepare(operation, params, owner, generation)
                finally:
                    self._evict()
        except DebugError as error:
            result = {"schema": SCHEMA, "status": "error", "reason": error.reason,
                      "message": str(error)}
            recovery = getattr(error, "recovery_session", None)
            if operation in ("follow", "stop"):
                result["restored"] = self._session is None
                if recovery is None and self._session and self._session["ownerId"] == owner:
                    recovery = self._receipt(self._session)
            if recovery:
                result.update(recoverySession=recovery, activePreview=True,
                              sessionId=recovery["sessionId"], selection=recovery["selection"],
                              sourceSha256=recovery["sourceSha256"])
            return result
        except Exception as error:
            code = getattr(error, "code", "")
            result = {"schema": SCHEMA, "status": "error", "reason": "unsupported-expression" if code else "preview-failed",
                      "instrumentationReason": str(code), "message": str(error)}
            if operation in ("follow", "stop"):
                result["restored"] = self._session is None
                if self._session and self._session["ownerId"] == owner:
                    recovery = self._receipt(self._session)
                    result.update(recoverySession=recovery, activePreview=True,
                                  sessionId=recovery["sessionId"], selection=recovery["selection"],
                                  sourceSha256=recovery["sourceSha256"])
            return result

    def shutdown(self):
        """Hard lifetime gate: do not finalize Python while a replay worker lives."""
        self._closing = True
        with self._generation_lock:
            for owner in self._generations:
                self._generations[owner] += 1
        if not self._lock.acquire(timeout=5.0):
            raise RuntimeError("Shader preview worker is still stopping; keep its replay context alive")
        try:
            if self._session:
                self.driver.restore(self._session, require_owned=True)
                self._session = None
            for key in list(self._cache):
                self.driver.free(self._cache[key], self._capture)
                del self._cache[key]
            self._plans.clear()
            self._source_model = None
            self._analysis_source = None
            self._anchors.clear()
            for directory in self._source_directories:
                directory.cleanup()
            self._source_directories = []
        finally:
            self._lock.release()

    def _release_session(self):
        session = self._session
        if session is None:
            return
        try:
            self.driver.restore(session, require_owned=True)
        except Exception as error:
            # Keep the target pinned and expose its ownership even when the
            # remote restore response is lost. The UI must still offer Stop.
            session["recoveryRequired"] = True
            if not isinstance(error, DebugError):
                error = DebugError("restore-failed", str(error))
            error.recovery_session = self._receipt(session)
            raise error
        self._session = None

    def _follow(self, params, owner, generation):
        existing = self._session
        # The current owner/generation is authoritative. UI receipts can lag
        # behind rapid EID changes, so a previous event's session ID is advisory.
        if existing and existing["ownerId"] != owner:
            raise DebugError("session-changed", "This preview session belongs to a different request")
        if existing and existing["facts"].get("scope") == RESOURCE_SCOPE:
            # Selecting another EID only changes the observation point. The
            # replay resource replacement already covers its complete prefix.
            self.driver.follow(existing, params)
            self._require_current(owner, generation)
            return self._receipt(existing)
        try:
            return self._prepare("inspect" if params.get("inspectOnly") else "start",
                                 params, owner, generation)
        except DebugError as error:
            # An incompatible event suspends intent, but may never hide a
            # failed restore or discard a still-owned native override.
            if self._session is None and error.reason in (
                    "not-draw", "shader-changed", "reflection-unavailable", "no-color-target"):
                return {"schema": SCHEMA, "status": "paused", "reason": error.reason,
                        "sessionId": "", "message": str(error)}
            if self._session is not None and self._session["ownerId"] == owner:
                error.recovery_session = self._receipt(self._session)
            raise

    def _prepare(self, operation, params, owner, generation):
        # Python 3.6 on Windows uses a coarse tick clock for monotonic(); use
        # the performance counter so cache/replay timings do not round to 0 ms.
        began = time.perf_counter()
        scope = _scope(params)
        if scope == RESOURCE_SCOPE and params.get("independentFloatOutput"):
            raise DebugError("independent-preview-unsupported",
                "Independent float output does not propagate to later Draws; use the captured render target")
        existing = self._session
        requested_capture = (params.get("captureBinding") or {}).get("captureSessionId")
        if existing and (not requested_capture or requested_capture == existing["facts"]["capture"]):
            if (owner != existing["ownerId"] or scope != existing["facts"].get("scope", EVENT_SCOPE) or
                    (scope == EVENT_SCOPE and int(params.get("eventId", existing["facts"]["eventId"])) != existing["facts"]["eventId"]) or
                    (params.get("shaderId") and str(params["shaderId"]) != existing["facts"]["shaderId"])):
                # Do this before inspecting/replaying the new event: leaving an
                # old override in the replay prefix can contaminate its inputs,
                # and restoring afterwards would stale its mutation generation.
                self._release_session()
                self._require_current(owner, generation)
        # Inspection may show a member chooser while the user navigates away.
        # Its immutable anchor facts remain usable only while native prior
        # ownership still matches; no second event selection is required.
        anchor_key = _digest([owner, params.get("captureBinding"), params.get("eventId"),
            params.get("shaderId"), params.get("stage"), params.get("entryPoint"),
            params.get("expectedOriginalSha256"), params.get("targetProfile"),
            params.get("language"), params.get("source")])
        previous = self._session
        if previous is None and scope == RESOURCE_SCOPE and anchor_key in self._anchors:
            anchor = self._anchors[anchor_key]
            previous = {"facts": anchor, "status": "comparing", "restoreState": anchor["restoreState"]}
        inspect_started = time.perf_counter()
        facts = self.driver.inspect(params, previous=previous)
        inspect_ms = (time.perf_counter() - inspect_started) * 1000
        source_started = time.perf_counter()
        facts["scope"] = scope
        if facts["capture"] != self._capture:
            # Old native IDs belong to a destroyed controller. Never pass them
            # to the new controller, even if their decimal values happen to match.
            self._cache.clear()
            self._session = None
            self._capture = facts["capture"]
            self._anchors.clear()
        snapshot = source_snapshot(params)
        if self.instrumenter is None:
            self.instrumenter = _load_instrumenter()
        language = params.get("language", "hlsl")
        analysis_source = (facts["capture"], snapshot["unitSha256"], language)
        if analysis_source != self._analysis_source:
            # Retain compiled target binaries under their separate byte budget, but discard
            # old source/model graphs immediately. Different selections share one current model.
            self._plans.clear()
            self._source_model = None
            self._analysis_source = analysis_source
        needs_preprocess = getattr(self.instrumenter, "needs_preprocess", lambda source: False)
        preprocess_required = bool(snapshot.get("needsPreprocess") or facts.get("flagsNeedPreprocess") or
                                   needs_preprocess(snapshot["source"]))
        prepare_source = getattr(self.instrumenter, "prepare_source", None)
        if self._source_model is None and callable(prepare_source) and not preprocess_required:
            self._source_model = prepare_source(snapshot["source"], language=language)
        normalizer = getattr(self.instrumenter, "normalize_selection", None)
        normalized = (normalizer(snapshot["source"], snapshot["byteStart"], snapshot["byteEnd"],
                                 **({"prepared": self._source_model} if self._source_model else {}))
                      if callable(normalizer) else snapshot)
        plan_key = _digest([snapshot["unitSha256"], normalized["byteStart"], normalized["byteEnd"],
                            params.get("language", "hlsl"), facts["entryPoint"],
                            facts.get("api"), facts.get("profile"), facts.get("flagValues", []),
                            facts["compileIdentity"].get("preprocessor") if isinstance(facts["compileIdentity"], dict) else None,
                            facts["compileIdentity"].get("compiler") if isinstance(facts["compileIdentity"], dict) else None])
        plan = self._plans.get(plan_key)
        preprocessed = False
        original_selection = None
        if plan is None:
            source, start, end = snapshot["source"], snapshot["byteStart"], snapshot["byteEnd"]
            if preprocess_required:
                prepared = self.instrumenter.prepare_preprocess(source, start, end)
                text = self.driver.preprocess(prepared["source"], params, facts, snapshot)
                mapped = self.instrumenter.finish_preprocess(text, prepared)
                source, start, end = mapped["source"], mapped["byteStart"], mapped["byteEnd"]
                original_selection = mapped.get("originalSelection") or prepared.get("originalSelection")
                preprocessed = True
            plan = self.instrumenter.inspect(source, start, end, language=params.get("language", "hlsl"),
                entry_point=facts["entryPoint"],
                **({"prepared": self._source_model} if self._source_model else {}))
            plan["_preprocessed"] = preprocessed
            if original_selection is not None:
                plan["_originalSelection"] = dict(original_selection)
            self._plans[plan_key] = plan
            def retained_tokens():
                models = {id(p["model"]): p["model"] for p in self._plans.values() if "model" in p}
                return sum(len(model.get("tokens", [])) for model in models.values())
            while len(self._plans) > MAX_PLANS or (len(self._plans) > 1 and retained_tokens() > MAX_ANALYSIS_TOKENS):
                self._plans.popitem(last=False)
        else:
            self._plans.move_to_end(plan_key)
        self._require_current(owner, generation)
        selection = _public_selection(plan, snapshot)
        if plan.get("_preprocessed"):
            # The instrumenter can expand a clicked function name or return
            # keyword before preprocessing. Keep that original-file range for
            # editor marks; compiler-expanded offsets belong to another unit.
            original_selection = plan.get("_originalSelection") or {}
            selection.update(byteStart=int(original_selection.get("byteStart", snapshot["originalByteStart"])),
                             byteEnd=int(original_selection.get("byteEnd", snapshot["originalByteEnd"])))
            selection["expression"] = params["source"].encode("utf-8")[selection["byteStart"]:selection["byteEnd"]].decode("utf-8")
        selection["line"] = params["source"].encode("utf-8")[:selection["byteStart"]].count(b"\n") + 1
        if operation == "inspect":
            if scope == RESOURCE_SCOPE:
                self._anchors[anchor_key] = facts
                self._anchors.move_to_end(anchor_key)
                while len(self._anchors) > MAX_PLANS:
                    self._anchors.popitem(last=False)
            return {"schema": SCHEMA, "status": "ready", "choices": plan.get("choices", []),
                    "scope": scope, "resourceScoped": scope == RESOURCE_SCOPE,
                    "persistentAcrossEvents": scope == RESOURCE_SCOPE, "eventScoped": scope == EVENT_SCOPE,
                    "anchorEventId": facts["eventId"], "shaderId": facts["shaderId"],
                    "targets": facts["targets"], "selection": selection,
                    "sourceSha256": snapshot["sourceSha256"],
                    "semantics": "observe-at-expression",
                    "timings": {"inspectReplayAndContractMs": inspect_ms,
                                "sourcePreparationMs": (time.perf_counter() - source_started) * 1000,
                                "totalMs": (time.perf_counter() - began) * 1000}}
        requested = int(params.get("targetIndex", -1))
        targets = facts["targets"]
        target = next((t for t in targets if t["index"] == requested), None)
        if requested < 0:
            target = next((t for t in targets if t["components"] == 4), None) or (targets[0] if targets else None)
        if target is None:
            raise DebugError("no-color-target", "The selected Draw has no matching writable color target")
        independent = bool(params.get("independentFloatOutput", False))
        target = dict(target)
        if independent:
            # Preserve the selected location, but generate a floating point
            # output for the private RGBA32F attachment, regardless of the
            # captured target's integer/normalised channel representation.
            target.update(components=4, componentType="float", independentFloatOutput=True)
        options = {"member": str(params.get("member") or ""), "target": target,
                   "mode": str(params.get("mode") or "custom"),
                   "range_min": float(params.get("rangeMin", 0)),
                   "range_max": float(params.get("rangeMax", 1)),
                   "iteration": int(params.get("iteration", 0))}
        if "channel" in params:
            options["channel"] = int(params["channel"])
        cache_options = dict(options, target={k: v for k, v in target.items() if k != "resourceId"})
        key = _digest([facts["compileIdentity"], plan_key, cache_options, scope])
        cached = self._cache.get(key)
        cache_hit = cached is not None
        source_ms = (time.perf_counter() - source_started) * 1000
        compiled_at = time.perf_counter()
        if cached is None:
            generated = self.instrumenter.instrument(plan, **options)
            cached = self.driver.compile(generated, facts, params)
            cached["key"] = key
            cached["generated"] = {"semantics": generated.get("semantics", "observe-at-expression")}
            self._cache[key] = cached
        self._cache.move_to_end(key)
        compile_ms = (time.perf_counter() - compiled_at) * 1000
        self._require_current(owner, generation)
        # Include dependencies can change while the compiler was running.
        if source_snapshot(params)["unitSha256"] != snapshot["unitSha256"]:
            raise DebugError("stale-source", "Shader include dependencies changed during preview compilation")
        existing = self._session
        identity = (owner, facts["capture"], scope,
                    facts["eventId"] if scope == EVENT_SCOPE else None, facts["shaderId"])
        if existing and existing["identity"] != identity:
            self._release_session()
            existing = None
        restore_state = existing["restoreState"] if existing else facts["restoreState"]
        session = {"sessionId": existing["sessionId"] if existing else uuid.uuid4().hex,
                   "ownerId": owner, "identity": identity, "facts": facts,
                   "restoreState": restore_state, "cached": cached, "target": target,
                   "selection": selection, "sourceSha256": snapshot["sourceSha256"],
                   "status": "active", "cacheHit": cache_hit}
        session["independentFloatOutput"] = independent
        applied_at = time.perf_counter()
        try:
            self.driver.apply(session, previous=existing,
                              is_current=lambda: self._current(owner, generation))
        except DebugError as error:
            if getattr(error, "preview_committed", False):
                # Native installation succeeded, but querying the private
                # texture and rolling back both failed. Keep ownership and
                # pin the candidate so Stop can retry the exact original state.
                session["recoveryRequired"] = True
                session["recoveryRollbackState"] = getattr(error, "rollback_state", None)
                if existing:
                    session["recoveryRollbackCacheKey"] = existing["cached"]["key"]
                self._session = session
                error.recovery_session = self._receipt(session)
            raise
        self._session = session
        if not self._current(owner, generation):
            # A cancel during native replay must not let a late completion
            # resurrect the preview after its stop response.
            self.driver.restore(session, require_owned=True)
            self._session = None
            raise DebugError("superseded", "The preview was cancelled while replaying")
        session["timings"] = {"compileMs": compile_ms,
                              "inspectReplayAndContractMs": inspect_ms,
                              "sourcePreparationMs": source_ms,
                              "applyReplayMs": (time.perf_counter() - applied_at) * 1000,
                              "totalMs": (time.perf_counter() - began) * 1000}
        self._evict()
        return self._receipt(session)

    def _evict(self):
        if self._session and self._session.get("recoveryRequired"):
            # A disconnected peer cannot confirm FreeTargetResource either.
            # Preserve the recovery receipt instead of letting best-effort
            # eviction replace it with a second transport exception.
            return
        pinned = {self._session["cached"]["key"]} if self._session else set()
        if self._session and self._session.get("recoveryRollbackCacheKey"):
            pinned.add(self._session["recoveryRollbackCacheKey"])
        total = sum(item.get("byteSize", 0) for item in self._cache.values())
        for key in list(self._cache):
            if len(self._cache) <= MAX_CACHE_TARGETS and total <= MAX_CACHE_BYTES:
                break
            if key in pinned:
                continue
            item = self._cache[key]
            self.driver.free(item, self._capture)
            total -= item.get("byteSize", 0)
            del self._cache[key]

    def _stop(self, owner, params):
        session = self._session
        if session is None:
            return {"schema": SCHEMA, "status": "stopped"}
        # A stop with the newest generation cancels this controller's intent,
        # including a follow that committed before its UI receipt was drained.
        if session["ownerId"] != owner:
            raise DebugError("session-changed", "This preview session belongs to a different request")
        self._release_session()
        self._evict()
        return {"schema": SCHEMA, "status": "stopped", "sessionId": session["sessionId"]}

    def _compare(self, owner, params, generation):
        session = self._session
        if not session or owner != session["ownerId"] or params.get("sessionId") != session["sessionId"]:
            raise DebugError("session-changed", "The preview session has expired")
        original = bool(params.get("original"))
        if original and session["status"] != "comparing":
            self.driver.restore(session, require_owned=True)
            session["status"] = "comparing"
            session["recoveryRequired"] = False
        elif not original and session["status"] == "comparing":
            try:
                self.driver.apply(session, previous=session,
                                  is_current=lambda: self._current(owner, generation))
            except DebugError as error:
                if getattr(error, "preview_committed", False):
                    session["status"] = "active"
                    session["recoveryRequired"] = True
                    session["recoveryRollbackState"] = getattr(error, "rollback_state", None)
                    error.recovery_session = self._receipt(session)
                raise
            session["status"] = "active"
            session["recoveryRequired"] = False
            if not self._current(owner, generation):
                self.driver.restore(session, require_owned=True)
                session["status"] = "comparing"
                raise DebugError("superseded", "The comparison request was superseded")
        return self._receipt(session)

    @staticmethod
    def _receipt(session):
        if not session:
            return {"schema": SCHEMA, "status": "stopped"}
        return {"schema": SCHEMA, "status": session["status"],
                "scope": session["facts"].get("scope", EVENT_SCOPE),
                "resourceScoped": session["facts"].get("scope") == RESOURCE_SCOPE,
                "persistentAcrossEvents": session["facts"].get("scope") == RESOURCE_SCOPE,
                "eventScoped": session["facts"].get("scope", EVENT_SCOPE) == EVENT_SCOPE,
                "anchorEventId": session["facts"]["eventId"],
                "shaderId": session["facts"]["shaderId"],
                "sessionId": session["sessionId"], "selection": session["selection"],
                "sourceSha256": session["sourceSha256"],
                "targetIndex": session["target"]["index"], "cacheHit": session["cacheHit"],
                "independentFloatOutput": session.get("independentFloatOutput", False),
                "cbufferDisplayNamesAvailable": bool(session.get("cbufferDisplayNamesAvailable", False)),
                "previewTexture": session.get("previewTexture"),
                "recoveryRequired": bool(session.get("recoveryRequired", False)),
                "semantics": session["cached"]["generated"]["semantics"],
                "timings": session.get("timings", {})}


class ReplayDriver(object):
    """RenderDoc adapter. Mutations always run as one replay-thread transaction."""
    def __init__(self, bridge):
        self.bridge = bridge
        path = os.path.join(os.path.dirname(__file__), "shader_visual_compile.py")
        spec = importlib.util.spec_from_file_location("_bread_shader_visual_compile", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.compiler = module.Compiler()
        self._has_preprocessor_options = module.has_preprocessor_options
        bound_path = os.path.join(os.path.dirname(__file__), "shader_bound_compile.py")
        bound_spec = importlib.util.spec_from_file_location("_bread_shader_bound_compile", bound_path)
        bound_module = importlib.util.module_from_spec(bound_spec)
        bound_spec.loader.exec_module(bound_module)
        self.bound_compiler = bound_module.BoundCompiler(self.compiler)
        self._mirror_source = module._mirror_source

    def _capture(self):
        return str(self.bridge._capture_session_id or "")

    def _check_capture(self, params):
        binding = params.get("captureBinding") or {}
        if (not self._capture() or binding.get("captureSessionId") != self._capture() or
                not self.bridge._active_capture_token):
            raise DebugError("capture-changed", "The capture session changed")
        token = self.bridge._active_capture_token
        if _path(binding.get("capturePath") or ".") != _path(str(token[1])):
            raise DebugError("capture-changed", "The capture path changed")

    def capture_source(self, params):
        self._check_capture(params)
        bridge, rd = self.bridge, self.bridge.rd
        capture = self._capture()
        def run(ctrl):
            if capture != self._capture() or int(ctrl.GetCurrentEventID()) != int(params.get("eventId", 0)):
                raise DebugError("event-changed", "The current Draw changed")
            pipeline = ctrl.GetPipelineState()
            reflection = pipeline.GetShaderReflection(rd.ShaderStage.Pixel)
            if reflection is None:
                raise DebugError("reflection-unavailable", "The current Draw has no fragment Shader source")
            info = reflection.debugInfo
            captured_files = [(str(f.filename), str(f.contents)) for f in info.files]
            encoding = str(getattr(info, "encoding", "")).lower()
            api = bridge._shader_api_token(ctrl)
            source_entry = str(params.get("entryPoint") or getattr(info, "entrySourceName", "") or reflection.entryPoint or "main")
            index = int(getattr(info, "editBaseFile", -1))
            if not 0 <= index < len(captured_files):
                index = int(getattr(getattr(info, "entryLocation", None), "fileIndex", 0))
            if not 0 <= index < len(captured_files):
                index = 0
            source_extension = os.path.splitext(captured_files[index][0])[1].lower() if captured_files else ""
            if "hlsl" in encoding or api in ("d3d11", "d3d12"):
                language = "hlsl"
            elif "glsl" in encoding:
                language = "glsl"
            else:
                language = "hlsl" if source_extension in (".hlsl", ".hlsli", ".fx", ".fxh", ".usf", ".ush", ".cg", ".cginc") else "glsl"
            source_file = str(params.get("sourceFile") or "")
            if source_file:
                if not os.path.isabs(source_file):
                    raise DebugError("invalid-source", "The selected source file must have an absolute path")
                source_file = _path(source_file)
                with open(source_file, "rb") as handle:
                    raw_source = handle.read(MAX_SOURCE_BYTES + 1)
                text = raw_source.decode("utf-8", errors="strict")
                _source_bytes(text)
                extension = os.path.splitext(source_file)[1].lower()
                if extension in (".hlsl", ".hlsli", ".fx", ".fxh", ".usf", ".ush", ".cg", ".cginc"):
                    language = "hlsl"
                elif extension in (".glsl", ".frag", ".vert", ".fs", ".vs"):
                    language = "glsl"
                return {"sourcePath": source_file, "rootSourcePath": source_file,
                        "includeRoot": os.path.dirname(source_file), "text": text,
                        "language": language, "stage": "fragment", "sourceOrigin": "selected-file",
                        "entryPoint": source_entry,
                        "resourceId": str(bridge._resource_id_int(pipeline.GetShader(rd.ShaderStage.Pixel))),
                        "shaderRawSha256": _digest(bridge._shader_raw_bytes(reflection))}
            if not captured_files and "glsl" in str(reflection.encoding).lower():
                captured_files = [("shader.frag", bridge._shader_raw_bytes(reflection).decode("utf-8"))]
            if not captured_files:
                raise DebugError("source-unavailable", "No high-level source is embedded in this capture; open its Shader source working copy")
            # Captured filenames are provenance, not filesystem authority. Make
            # local owned names and redirect only includes that name a captured
            # file exactly or have one unambiguous basename match.
            mapping, files = {}, {}
            for number, (name, text) in enumerate(captured_files):
                normalized = posixpath.normpath(name.replace("\\", "/"))
                leaf = normalized.rsplit("/", 1)[-1] or ("shader.hlsl" if language == "hlsl" else "shader.frag")
                leaf = re.sub(r"[^A-Za-z0-9_.-]", "_", leaf)
                if normalized in mapping:
                    raise DebugError("source-unavailable", "Captured source has ambiguous duplicate filenames: " + name)
                mapping[normalized] = "%d_%s" % (number, leaf)
            for name, text in captured_files:
                normalized = posixpath.normpath(name.replace("\\", "/"))
                def include(match):
                    requested = match.group(1).replace("\\", "/")
                    resolved = mapping.get(posixpath.normpath(requested))
                    if resolved is None:
                        resolved = mapping.get(posixpath.normpath(posixpath.join(posixpath.dirname(normalized), requested)))
                    if resolved is None:
                        candidates = [value for key, value in mapping.items() if key.rsplit("/", 1)[-1] == requested.rsplit("/", 1)[-1]]
                        resolved = candidates[0] if len(candidates) == 1 else None
                    return '#include "%s"' % resolved if resolved else match.group(0)
                files[mapping[normalized]] = re.sub(r'(?m)^\s*#\s*include\s*["<]([^">\r\n]+)[">]', include, text)
            root = mapping[posixpath.normpath(captured_files[index][0].replace("\\", "/"))]
            return {"files": files, "rootName": root, "text": files[root], "language": language,
                    "stage": "fragment", "entryPoint": source_entry,
                    "resourceId": str(bridge._resource_id_int(pipeline.GetShader(rd.ShaderStage.Pixel))),
                    "shaderRawSha256": _digest(bridge._shader_raw_bytes(reflection))}
        return bridge._replay(run)

    @staticmethod
    def _state(value):
        # Copy ResourceId values, not a borrowed view into a controller container.
        return {"active": bool(value.active), "eventId": int(value.eventId),
                "stage": value.stage, "expectedOriginal": value.expectedOriginal,
                "replacement": value.replacement, "outputTarget": int(value.outputTarget),
                "independentFloatOutput": bool(getattr(value, "independentFloatOutput", False))}

    def _resource_state(self, ctrl, shader):
        if not hasattr(ctrl, "GetTargetShaderReplacement"):
            raise DebugError("runtime-outdated", "Restart Bread with the persistent Shader preview runtime")
        target = ctrl.GetTargetShaderReplacement(shader)
        self.bridge._require_healthy_replay(ctrl)
        return {"active": bool(self.bridge._resource_id_int(target)),
                "expectedOriginal": shader, "replacement": target}

    @staticmethod
    def _same_resource_state(left, right):
        return bool(left is not None and right is not None and left["active"] == right["active"] and
                    (not left["active"] or left["replacement"] == right["replacement"]))

    def follow(self, session, params):
        """Observe resource ownership without replaying, compiling or selecting an EID."""
        self._check_capture(params)
        def run(ctrl):
            if self._capture() != session["facts"]["capture"]:
                raise DebugError("capture-changed", "The capture session changed")
            self.bridge._require_healthy_replay(ctrl)
            if not self._owned(ctrl, session):
                raise DebugError("replacement-changed", "Another operation changed the Shader replacement")
        try:
            self.bridge._replay(run)
        except DebugError as error:
            if error.reason in ("replacement-changed", "capture-changed"):
                self._clear_cbuffer_display_source(session)
            raise

    def _check_original_sha(self, params, capture, shader_id, baseline_sha, baseline_entry):
        expected_sha = str(params.get("expectedOriginalSha256") or "").lower()
        if expected_sha and baseline_sha != expected_sha:
            owner = getattr(self.bridge, "_shared_shader_replacements", {}).get(shader_id) or {}
            if (owner.get("expectedOriginalSha256") != expected_sha or
                    owner.get("candidateRawSha256") != baseline_sha or
                    owner.get("captureSessionId") != capture or
                    str(owner.get("entryPoint") or "") != baseline_entry):
                raise DebugError("shader-changed", "The workspace source belongs to different captured Shader bytes")

    def inspect(self, params, previous=None):
        self._check_capture(params)
        scope = _scope(params)
        if scope == RESOURCE_SCOPE and params.get("independentFloatOutput"):
            raise DebugError("independent-preview-unsupported",
                "Independent float output does not propagate to later Draws; use the captured render target")
        bridge, rd = self.bridge, self.bridge.rd
        session = self._capture()
        previous_facts = previous.get("facts") if previous else None
        if (scope == RESOURCE_SCOPE and previous_facts and
                previous_facts.get("scope") == RESOURCE_SCOPE and previous_facts["capture"] == session and
                str(params.get("shaderId") or previous_facts["shaderId"]) == previous_facts["shaderId"]):
            # The editor still targets its original Shader even when the user
            # is viewing an unrelated Draw, dispatch, marker or present event.
            # Reuse captured anchor facts, never replacement reflection.
            self.follow(previous, params)
            if bridge._normalise_stage_name(params.get("stage") or "fragment") != "fragment":
                raise DebugError("stage-unsupported", "Color output preview requires a PS or FS Shader")
            self._check_original_sha(params, session, int(previous_facts["shaderId"]),
                previous_facts["compileIdentity"]["baselineSha256"],
                previous_facts.get("baselineEntryPoint", previous_facts["entryPoint"]))
            return dict(previous_facts)
        def read_facts(ctrl):
            if session != self._capture():
                raise DebugError("capture-changed", "The capture session changed")
            event = int(ctrl.GetCurrentEventID())
            if scope == EVENT_SCOPE and int(params.get("eventId", event)) != event:
                raise DebugError("event-changed", "Select the Draw before starting its preview")
            if getattr(self, "_draw_events_capture", None) != session:
                # Actions are immutable for a capture. Copy only scalar EIDs;
                # do not retain native objects across controller lifetimes.
                draw_events, pending = set(), list(ctrl.GetRootActions())
                draw_flags = rd.ActionFlags.Drawcall | rd.ActionFlags.MeshDispatch
                while pending:
                    action = pending.pop()
                    children = list(action.children)
                    pending.extend(children)
                    if not children and action.flags & draw_flags:
                        draw_events.add(int(action.eventId))
                self._draw_events = draw_events
                self._draw_events_capture = session
            if event not in self._draw_events:
                raise DebugError("not-draw", "Color output preview requires a Draw event")
            stage_name = bridge._normalise_stage_name(params.get("stage") or "fragment")
            if stage_name != "fragment":
                raise DebugError("stage-unsupported", "Color output preview requires a PS or FS Shader")
            stage = rd.ShaderStage.Pixel
            pipeline = ctrl.GetPipelineState()
            shader = pipeline.GetShader(stage)
            shader_id = bridge._resource_id_int(shader)
            if not shader_id or shader_id != int(params.get("shaderId") or shader_id):
                raise DebugError("shader-changed", "The current Draw binds a different Shader")
            if scope == RESOURCE_SCOPE and not hasattr(ctrl, "GetTargetShaderReplacement"):
                raise DebugError("runtime-outdated", "Restart Bread with the persistent Shader preview runtime")
            if scope == EVENT_SCOPE and not hasattr(ctrl, "GetEventShaderOverride"):
                raise DebugError("runtime-outdated", "Restart Bread with the current-draw preview runtime")
            previous_facts = previous.get("facts") if previous else None
            same_session = bool(previous_facts and
                previous_facts["capture"] == session and previous_facts["eventId"] == event and
                previous_facts["shaderId"] == str(shader_id))
            if same_session and not self._owned(ctrl, previous):
                raise DebugError("replacement-changed", "Another operation changed the event Shader override")
            # Some APIs return replacement reflection while the event preview
            # is active. Its wrapper output/entry must never become the next
            # preview's captured baseline. Reuse facts only after native
            # ownership and current capture/event/shader have been verified.
            baseline = None if same_session else pipeline.GetShaderReflection(stage)
            if baseline is None and not same_session:
                raise DebugError("reflection-unavailable", "The bound Shader reflection is unavailable")
            baseline_entry = (previous_facts.get("baselineEntryPoint", previous_facts["entryPoint"])
                              if same_session else str(baseline.entryPoint))
            entry = str(params.get("entryPoint") or
                        (previous_facts["entryPoint"] if same_session else baseline.entryPoint) or "main")
            api = bridge._shader_api_token(ctrl)
            raw = b"" if same_session else bridge._shader_raw_bytes(baseline)
            baseline_sha = previous_facts["compileIdentity"]["baselineSha256"] if same_session else _digest(raw)
            self._check_original_sha(params, session, shader_id, baseline_sha, baseline_entry)
            textures = {bridge._resource_id_int(t.resourceId): t for t in ctrl.GetTextures()}
            targets = []
            descriptors = list(pipeline.GetOutputTargets() or [])
            if api == "vulkan":
                # Generic PipeState appends resolve attachments, which cannot
                # be addressed by a fragment output location.
                color_count = len(ctrl.GetVulkanPipelineState().currentPass.renderpass.colorAttachments)
                descriptors = descriptors[:color_count]
            for index, descriptor in enumerate(descriptors):
                texture_id = bridge._resource_id_int(getattr(descriptor, "resource", None))
                texture = textures.get(texture_id)
                if not texture:
                    continue
                fmt = getattr(descriptor, "format", None)
                if not fmt or not int(getattr(fmt, "compCount", 0)):
                    fmt = texture.format
                count = int(fmt.compCount)
                if not 1 <= count <= 4:
                    continue
                comp = str(fmt.compType).lower()
                comp = "uint" if comp.endswith("uint") else "sint" if comp.endswith("sint") else "float"
                targets.append({"index": index, "name": str(fmt.Name()), "components": count,
                                "componentType": comp, "resourceId": str(texture_id),
                                "samples": int(texture.msSamp), "format": str(fmt.Name())})
                if same_session:
                    old_target = next((t for t in previous_facts["targets"] if t["index"] == index), {})
                    if "outputName" in old_target:
                        targets[-1]["outputName"] = old_target["outputName"]
                else:
                    for output in getattr(baseline, "outputSignature", []):
                        if int(getattr(output, "regIndex", -1)) == index:
                            targets[-1]["outputName"] = str(getattr(output, "varName", ""))
            if not targets:
                raise DebugError("no-color-target", "The current Draw has no color attachment")
            if same_session:
                flag_values = list(previous_facts["flagValues"])
            else:
                flags = getattr(getattr(baseline, "debugInfo", None), "compileFlags", rd.ShaderCompileFlags())
                flag_values = [(str(f.name), str(f.value)) for f in getattr(flags, "flags", [])]
            profile = params.get("targetProfile") or ""
            if isinstance(profile, dict):
                profile = profile.get("profile", "")
            if not profile and same_session:
                profile = previous_facts["profile"]
            if not profile and api in ("d3d11", "d3d12"):
                command = " ".join(v for n, v in flag_values if n == "@cmdline")
                match = re.search(r'\bps_[4-6]_[0-9]+(?:_level_9_[13])?\b', command)
                if match:
                    profile = match.group(0)
                else:
                    # Preserve the captured model instead of silently converting
                    # a DXIL Shader to the legacy D3D compiler route.
                    profile = _profile_from_container(raw)
            identity = {"capture": session, "api": api, "shaderId": str(shader_id),
                        "entryPoint": entry, "profile": profile, "flags": flag_values,
                        "baselineSha256": baseline_sha,
                        "targets": [{k: v for k, v in t.items() if k != "resourceId"} for t in targets]}
            spirv_version = (previous_facts["spirvVersion"] if same_session else
                             int.from_bytes(raw[4:8], "little") if raw[:4] == b"\x03\x02\x23\x07" and len(raw) >= 8 else 0x10000)
            return {"capture": session, "eventId": event, "shaderId": str(shader_id), "scope": scope,
                    "shader": shader, "stage": stage, "api": api, "entryPoint": entry,
                    "profile": profile, "flagValues": flag_values, "targets": targets,
                    "baselineEntryPoint": baseline_entry,
                    # Own this value: the replay/backend may release its
                    # borrowed reflection after capture close or replacement.
                    "baselineReflection": (previous_facts.get("baselineReflection") if same_session else
                                           _display_reflection_snapshot(bridge, baseline)),
                    "targetEncoding": (previous_facts.get("targetEncoding", "") if same_session else str(baseline.encoding)),
                    "spirvVersion": spirv_version,
                    "mutationGeneration": int(ctrl.GetShaderReplacementGeneration(event)),
                    "compileIdentity": identity,
                    "baselineInterface": (previous_facts["baselineInterface"] if same_session else
                        bridge._physical_shader_interface(bridge._compiled_shader_interface(baseline))),
                    "restoreState": (self._resource_state(ctrl, shader) if scope == RESOURCE_SCOPE else
                                     self._state(ctrl.GetEventShaderOverride(event, stage)))}
        def run(ctrl):
            if scope == EVENT_SCOPE:
                return read_facts(ctrl)
            original_event = int(ctrl.GetCurrentEventID())
            expected_shader = int(params.get("shaderId") or 0)
            try:
                return read_facts(ctrl)
            except DebugError as first_error:
                if first_error.reason not in ("not-draw", "shader-changed", "reflection-unavailable", "no-color-target"):
                    raise
                if (not expected_shader or (first_error.reason == "shader-changed" and
                        bridge._resource_id_int(ctrl.GetPipelineState().GetShader(rd.ShaderStage.Pixel)) == expected_shader)):
                    raise  # A real source/original mismatch cannot be repaired by another EID.
                last_error = first_error
            # A workspace can be debugged while an unrelated Shader or a
            # non-Draw is selected. Discover an actual use of the requested
            # resource, inspect there, and restore the replay cursor before
            # compiling/applying. The qrenderdoc selected EID never changes.
            shader = bridge._resolve_resource_id(ctrl, expected_shader)
            usages = list(ctrl.GetUsage(shader) or [])
            candidates = [int(params.get("eventId") or 0)]
            candidates.extend(int(usage.eventId) for usage in usages)
            visited = {original_event}
            try:
                for event in candidates:
                    if event in visited or event not in getattr(self, "_draw_events", set()):
                        continue
                    visited.add(event)
                    if session != self._capture():
                        raise DebugError("capture-changed", "The capture session changed")
                    ctrl.SetFrameEvent(event, True)
                    bridge._require_healthy_replay(ctrl)
                    try:
                        return read_facts(ctrl)
                    except DebugError as error:
                        if error.reason not in ("not-draw", "shader-changed", "reflection-unavailable", "no-color-target"):
                            raise
                        if (error.reason == "shader-changed" and
                                bridge._resource_id_int(ctrl.GetPipelineState().GetShader(rd.ShaderStage.Pixel)) == expected_shader):
                            raise
                        last_error = error
                raise last_error
            finally:
                if session == self._capture() and int(ctrl.GetCurrentEventID()) != original_event:
                    bridge._require_healthy_replay(ctrl)
                    ctrl.SetFrameEvent(original_event, True)
                    bridge._require_healthy_replay(ctrl)
        facts = bridge._replay(run)
        if params.get("workspacePath") and params.get("expectedOriginalSha256"):
            client = bridge._shader_workspace_ui._save_client
            recipe = client.live_edit_operation("compiler-contract", {
                "workspace_path": params["workspacePath"],
                "source_workspace_id": params.get("sourceWorkspaceId") or params["workspaceId"],
                "expected_raw_sha256": params["expectedOriginalSha256"]}, params["captureBinding"])
            recipe_api = str((recipe.get("values") or {}).get("target_api") or "").lower().replace("graphicsapi.", "")
            recipe_api = {"direct3d11":"d3d11", "direct3d12":"d3d12", "gl":"opengl", "vk":"vulkan"}.get(recipe_api,recipe_api)
            if (recipe.get("stage") != "fragment" or recipe_api != facts["api"] or
                    recipe.get("workspaceId") != (params.get("sourceWorkspaceId") or params["workspaceId"]) or
                    recipe.get("entryPoint") != facts["entryPoint"] or
                    recipe.get("originalRawSha256") != params["expectedOriginalSha256"] or
                    recipe.get("language") != str(params.get("language") or "hlsl")):
                raise DebugError("compiler-contract-mismatch", "Workspace compiler language or stage changed")
            facts["workspaceCompiler"] = recipe
            facts["compileIdentity"]["compiler"] = self.bound_compiler.identity(recipe)
            facts["flagsNeedPreprocess"] = self.bound_compiler.needs_preprocess(recipe)
            if recipe["backend"] == "replay-native" and re.search(r"(?m)^\s*#",str(params.get("source") or "")):
                # A native GLSL program remains driver-compiled. If its source
                # needs lexical preprocessing, that helper's binary/library
                # identity must also invalidate cached instrumentation plans.
                facts["compileIdentity"]["preprocessor"] = self.compiler.identity(
                    str(params.get("language") or "hlsl"), facts["api"], facts["profile"])
            facts["flagValues"] = list(recipe["flags"])
            facts["compileIdentity"]["flags"] = list(recipe["flags"])
        else:
            facts["compileIdentity"]["compiler"] = self.compiler.identity(
                str(params.get("language") or "hlsl"), facts["api"], facts["profile"])
            facts["flagsNeedPreprocess"] = self._has_preprocessor_options(facts["flagValues"])
        return facts

    def preprocess(self, source, params, facts, snapshot=None):
        try:
            recipe = facts.get("workspaceCompiler")
            if recipe and recipe["backend"] == "bound-route":
                return self.bound_compiler.preprocess(recipe, source, facts["entryPoint"], snapshot, self._mirror_source)
            profile = ("opengl-spirv" if "openglspirv" in str(facts.get("targetEncoding", "")).lower()
                       else facts["profile"])
            return self.compiler.preprocess(source, str(params.get("language") or "hlsl"),
                                            facts["entryPoint"], profile, facts["api"], snapshot,
                                            flags=facts["flagValues"])
        except RuntimeError as error:
            raise DebugError("preprocess-failed", str(error))

    def compile(self, generated, facts, params):
        bridge, rd = self.bridge, self.bridge.rd
        raw = _source_bytes(generated["source"])
        language = str(params.get("language") or "hlsl")
        binary = None
        recipe = facts.get("workspaceCompiler")
        if recipe and recipe["backend"] == "bound-route":
            try:
                binary = self.bound_compiler.compile(recipe, generated["source"], generated["entryPoint"])
            except RuntimeError as error:
                raise DebugError("compile-failed", str(error))
        opengl_spirv = (language == "glsl" and facts["api"] == "opengl" and
                        "openglspirv" in str(facts.get("targetEncoding", "")).lower())
        if not recipe and (opengl_spirv or (language == "hlsl" and (facts["api"] == "vulkan" or
                (facts["api"] == "d3d12" and str(facts["profile"]).startswith("ps_6_"))))):
            try:
                binary = self.compiler.compile(generated["source"], language, facts["api"],
                    generated["entryPoint"], facts["profile"], facts.get("spirvVersion", 0x10000),
                    flags=facts["flagValues"], exported_entry=facts.get("baselineEntryPoint", ""),
                    target_encoding="OpenGLSPIRV" if opengl_spirv else None)
            except RuntimeError as error:
                raise DebugError("compile-failed", str(error))
        def run(ctrl):
            if self._capture() != facts["capture"]:
                raise DebugError("capture-changed", "Capture changed before preview compilation")
            encoding = getattr(rd.ShaderEncoding, binary["encoding"] if binary else
                               {"hlsl": "HLSL", "glsl": "GLSL"}.get(language, "Unknown"), None)
            if encoding is None or encoding not in ctrl.GetTargetShaderEncodings():
                raise DebugError("compiler-unavailable", "The replay backend cannot compile this Shader source language")
            flags = rd.ShaderCompileFlags()
            values = list(facts["flagValues"])
            if language == "hlsl" and facts["api"] in ("d3d11", "d3d12"):
                if not re.fullmatch(r"ps_[4-6]_[0-9]+(?:_level_9_[13])?", facts["profile"]):
                    raise DebugError("profile-unavailable", "The captured pixel Shader profile is unavailable")
                # Native DXBC flag decoding consumes packing/precision and
                # optimisation switches too. Replacing the whole command line
                # changes matrix semantics even though resource bindings match.
                command = " ".join(v for n, v in values if n == "@cmdline")
                command = re.sub(r'(?<!\S)[/-]T\s+\S+', "", command)
                command = command.strip() + " /T " + facts["profile"]
                values = [(n, v) for n, v in values if n != "@cmdline"] + [("@cmdline", command.strip())]
            flag_objects = []
            for name, value in values:
                flag = rd.ShaderCompileFlag()
                flag.name, flag.value = name, value
                flag_objects.append(flag)
            flags.flags = flag_objects
            target, messages = bridge._build_target_shader(ctrl, generated["entryPoint"], encoding,
                                                           binary["bytes"] if binary else raw, flags, facts["stage"])
            if not bridge._resource_id_int(target):
                raise DebugError("compile-failed", str(messages or "Preview compilation failed"))
            try:
                entries = list(ctrl.GetShaderEntryPoints(target) or [])
                entry = bridge._exact_shader_entry(entries,
                                                   generated["entryPoint"], "fragment")
                if entry is None and facts["api"] in ("d3d11", "d3d12"):
                    # DXBC exposes a synthetic main entry even when its source
                    # function was named PSMain. It is a single-stage program.
                    pixel_entries = [item for item in entries if item.stage == facts["stage"]]
                    entry = pixel_entries[0] if len(pixel_entries) == 1 else None
                if entry is None and binary:
                    entry = bridge._exact_shader_entry(entries, facts.get("baselineEntryPoint", ""), "fragment")
                if entry is None:
                    raise DebugError("interface-mismatch", "The compiled preview entry point is missing")
                reflection = ctrl.GetShader(rd.ResourceId(), target, entry)
                if reflection is None:
                    raise DebugError("interface-mismatch", "The compiled preview reflection is unavailable")
                candidate = bridge._physical_shader_interface(bridge._compiled_shader_interface(reflection))
                _validate_interface(bridge, facts["baselineInterface"], candidate, generated["output"],
                    allow_fragment_position="unreached-output-checkerboard" in generated.get("warnings", []))
                return {"target": target, "byteSize": len(raw) + len(bridge._shader_raw_bytes(reflection)),
                        "messages": str(messages or ""), "capture": facts["capture"],
                        "reflection": _display_reflection_snapshot(bridge, reflection),
                        "rawSha256": _digest(bridge._shader_raw_bytes(reflection))}
            except Exception:
                ctrl.FreeTargetResource(target)
                raise
        return bridge._replay(run)

    def _owned(self, ctrl, session):
        facts = session["facts"]
        if facts.get("scope") == RESOURCE_SCOPE:
            state = self._resource_state(ctrl, facts["shader"])
            expected = (session["restoreState"] if session["status"] == "comparing" else
                        {"active": True, "replacement": session["cached"]["target"]})
            return self._same_resource_state(state, expected)
        state = self._state(ctrl.GetEventShaderOverride(facts["eventId"], facts["stage"]))
        expected = session["restoreState"] if session["status"] == "comparing" else {
            "active": True, "expectedOriginal": facts["shader"],
            "replacement": session["cached"]["target"], "outputTarget": session["target"]["index"],
            "independentFloatOutput": session.get("independentFloatOutput", False)}
        if state["active"] != expected["active"]:
            return False
        return not state["active"] or all(state[k] == expected[k] for k in
                                         ("expectedOriginal", "replacement", "outputTarget", "independentFloatOutput"))

    def _set_resource_state(self, ctrl, shader, state):
        if state["active"]:
            error = ctrl.ReplaceTargetShaderChecked(shader, state["replacement"])
        else:
            error = ctrl.RemoveTargetShaderReplacementChecked(shader)
        if error:
            raise DebugError("restore-failed", str(error))
        self.bridge._require_healthy_replay(ctrl)

    def _clear_cbuffer_display_source(self, session):
        """Remove this session's temporary names without touching Shader state."""
        bridge = self.bridge
        clear = getattr(getattr(bridge, "_ctx", None), "UnregisterCBufferDebugDisplaySource", None)
        if callable(clear):
            try:
                bridge._invoke_on_ui_sync(lambda: clear(session["sessionId"]))
            except Exception:
                # This is a display-only aid. A name-refresh failure must not
                # convert a successful GPU restore into a false apply failure.
                pass
        session["cbufferDisplayNamesAvailable"] = False

    def _cbuffer_display_source(self, session):
        """Copy retained original data while still executing on the replay thread."""
        bridge, facts = self.bridge, session["facts"]
        source = facts.get("baselineReflection")
        owner = getattr(bridge, "_shared_shader_replacements", {}).get(int(facts["shaderId"]))
        if (owner and owner.get("captureSessionId") == facts["capture"]
                and owner.get("originalResource") == facts["shader"]):
            captured = owner.get("capturedOriginalReflection")
            raw = bridge._shader_raw_bytes(captured) if captured is not None else b""
            if raw and _digest(raw) == owner.get("expectedOriginalSha256"):
                source = _display_reflection_snapshot(bridge, captured)
        return source

    def _register_cbuffer_display_source(self, session, mutation_generation, source):
        """Copy exact original/debug layouts into a capture-local UI registry."""
        bridge, facts = self.bridge, session["facts"]
        register = getattr(getattr(bridge, "_ctx", None), "RegisterCBufferDebugDisplaySource", None)
        session["cbufferDisplayNamesAvailable"] = False
        if not callable(register) or self._capture() != facts["capture"]:
            return
        active = session["cached"].get("reflection")
        source_raw = bridge._shader_raw_bytes(source) if source is not None else b""
        active_raw = bridge._shader_raw_bytes(active) if active is not None else b""
        if not source_raw or not active_raw:
            return
        def publish():
            if self._capture() != facts["capture"]:
                return False
            return bool(register(session["sessionId"], facts["shader"], session["cached"]["target"],
                _digest(source_raw), _digest(active_raw), facts["stage"], int(mutation_generation),
                list(source.constantBlocks), list(active.constantBlocks)))
        try:
            session["cbufferDisplayNamesAvailable"] = bridge._invoke_on_ui_sync(publish)
            if not session["cbufferDisplayNamesAvailable"]:
                self._clear_cbuffer_display_source(session)
        except Exception:
            self._clear_cbuffer_display_source(session)

    def _apply_resource(self, session, previous, is_current):
        bridge, facts = self.bridge, session["facts"]
        if session.get("independentFloatOutput"):
            raise DebugError("independent-preview-unsupported",
                "Independent float output does not propagate to later Draws; use the captured render target")
        def run(ctrl):
            if self._capture() != facts["capture"]:
                raise DebugError("capture-changed", "Capture changed before applying the preview")
            bridge._require_healthy_replay(ctrl)
            if not is_current():
                raise DebugError("superseded", "The preview request was superseded")
            before = self._resource_state(ctrl, facts["shader"])
            if ((previous and not self._owned(ctrl, previous)) or
                    (not previous and not self._same_resource_state(before, session["restoreState"]))):
                raise DebugError("replacement-changed", "Another operation changed the Shader replacement")
            # ReplaceResource's checked native path invalidates the entire
            # replay prefix and refreshes the currently selected EID once. Do
            # not register this temporary target in qrenderdoc's persisted map.
            error = ctrl.ReplaceTargetShaderChecked(facts["shader"], session["cached"]["target"])
            if error:
                try:
                    bridge._require_healthy_replay(ctrl)
                except Exception as health_error:
                    failure = DebugError("restore-failed",
                        "Shader preview acceptance could not be confirmed: " + str(error) +
                        "; replay diagnostic: " + str(health_error))
                    failure.preview_committed = True
                    failure.rollback_state = before
                    raise failure
                raise DebugError("apply-failed", str(error))
            try:
                bridge._require_healthy_replay(ctrl)
                if not self._owned(ctrl, dict(session, status="active")):
                    raise DebugError("replacement-changed", "Another operation changed the committed preview")
            except Exception as apply_error:
                try:
                    bridge._require_healthy_replay(ctrl)
                    owned = self._owned(ctrl, dict(session, status="active"))
                    if owned:
                        self._set_resource_state(ctrl, facts["shader"], before)
                except Exception as rollback_error:
                    error = DebugError("restore-failed",
                        "Preview was applied but its previous state could not be restored: " +
                        str(rollback_error) + "; replay diagnostic: " + str(apply_error))
                    error.preview_committed = True
                    error.rollback_state = before
                    raise error
                if not owned:
                    raise DebugError("replacement-changed",
                        "Another operation changed the committed preview; its state was preserved")
                raise
            try:
                display_source = self._cbuffer_display_source(session)
            except Exception:
                display_source = None  # Display-only copying cannot undo a successful GPU apply.
            return int(ctrl.GetShaderReplacementGeneration(0)), display_source
        try:
            generation, display_source = bridge._replay(run)
        except DebugError as error:
            if error.reason in ("replacement-changed", "capture-changed"):
                self._clear_cbuffer_display_source(previous or session)
            raise
        self._register_cbuffer_display_source(session, generation, display_source)
        self._notify()

    def _restore_resource(self, session, require_owned=True):
        bridge, facts = self.bridge, session["facts"]
        if self._capture() != facts["capture"]:
            self._clear_cbuffer_display_source(session)
            return  # The old controller owns destruction of all its native IDs.
        def run(ctrl):
            if self._capture() != facts["capture"]:
                return
            bridge._require_healthy_replay(ctrl)
            current = self._resource_state(ctrl, facts["shader"])
            if require_owned and not self._owned(ctrl, session):
                if session.get("recoveryRequired") and self._same_resource_state(current, session["restoreState"]):
                    return  # A prior restore committed but its reply was lost.
                if not (session.get("recoveryRequired") and
                        self._same_resource_state(current, session.get("recoveryRollbackState"))):
                    raise DebugError("replacement-changed",
                        "Another operation owns the Shader replacement; its state was preserved")
            if session["status"] == "comparing":
                return
            session["recoveryRequired"] = True
            self._set_resource_state(ctrl, facts["shader"], session["restoreState"])
        try:
            bridge._replay(run)
        except DebugError as error:
            if error.reason in ("replacement-changed", "capture-changed"):
                self._clear_cbuffer_display_source(session)
            raise
        self._clear_cbuffer_display_source(session)
        self._notify()

    def apply(self, session, previous, is_current):
        if session["facts"].get("scope") == RESOURCE_SCOPE:
            return self._apply_resource(session, previous, is_current)
        bridge = self.bridge
        facts = session["facts"]
        def run(ctrl):
            if self._capture() != facts["capture"]:
                raise DebugError("capture-changed", "Capture changed before applying the preview")
            bridge._require_healthy_replay(ctrl)
            if not is_current():
                raise DebugError("superseded", "The preview request was superseded")
            if int(ctrl.GetCurrentEventID()) != facts["eventId"]:
                raise DebugError("event-changed", "The current Draw changed during preview compilation")
            if (not previous and int(ctrl.GetShaderReplacementGeneration(facts["eventId"])) !=
                    facts["mutationGeneration"]):
                raise DebugError("replacement-changed", "Shader replacements changed during preview compilation")
            if previous and not self._owned(ctrl, previous):
                raise DebugError("replacement-changed", "Another operation changed the event Shader override")
            if not previous:
                current = self._state(ctrl.GetEventShaderOverride(facts["eventId"], facts["stage"]))
                baseline = session["restoreState"]
                if any(current[k] != baseline[k] for k in ("active", "expectedOriginal", "replacement", "outputTarget", "independentFloatOutput")):
                    raise DebugError("replacement-changed", "The event Shader override changed during compilation")
            before = self._state(ctrl.GetEventShaderOverride(facts["eventId"], facts["stage"]))
            error = ctrl.SetEventShaderOverride(facts["eventId"], facts["stage"], facts["shader"],
                                                session["cached"]["target"], session["target"]["index"],
                                                session.get("independentFloatOutput", False))
            if error:
                try:
                    bridge._require_healthy_replay(ctrl)
                except Exception as health_error:
                    # A failed reply can follow remote registration or replay.
                    # Even an unchanged local state cannot prove that the peer
                    # rejected the candidate; retain it until recovery/close.
                    failure = DebugError("restore-failed",
                        "Shader preview acceptance could not be confirmed: " + str(error) +
                        "; replay diagnostic: " + str(health_error))
                    failure.preview_committed = True
                    failure.rollback_state = before
                    raise failure
                raise DebugError("apply-failed", str(error))
            try:
                # A remote void operation may only publish its failure through
                # replay health. A local typed state snapshot is not evidence
                # that the remote device accepted or replayed the mutation.
                bridge._require_healthy_replay(ctrl)
                if session.get("independentFloatOutput"):
                    preview = ctrl.GetEventShaderPreviewTexture(facts["eventId"], facts["stage"])
                    bridge._require_healthy_replay(ctrl)
                    fmt = getattr(preview, "format", None)
                    if (not bridge._resource_id_int(preview.resourceId) or
                            int(getattr(preview, "width", 0)) <= 0 or
                            int(getattr(preview, "height", 0)) <= 0 or
                            int(getattr(fmt, "compCount", 0)) != 4 or
                            int(getattr(fmt, "compByteWidth", 0)) != 4 or
                            str(getattr(fmt, "compType", "")).lower().split(".")[-1] != "float"):
                        raise DebugError("preview-unavailable", "The replay backend did not expose its float preview texture")
                    session["previewTexture"] = preview
            except Exception as preview_error:
                # The native mutation is already committed. Restore it here
                # when health permits; otherwise retain ownership for recovery.
                try:
                    bridge._require_healthy_replay(ctrl)
                    # Compare may still label the session "comparing"
                    # until apply returns; native state is now the candidate.
                    owned = self._owned(ctrl, dict(session, status="active"))
                    if owned:
                        if before["active"]:
                            restore_error = ctrl.SetEventShaderOverride(facts["eventId"], facts["stage"],
                                before["expectedOriginal"], before["replacement"], before["outputTarget"],
                                before["independentFloatOutput"])
                            if restore_error:
                                raise RuntimeError(str(restore_error))
                        else:
                            ctrl.ClearEventShaderOverride(facts["eventId"], facts["stage"])
                        bridge._require_healthy_replay(ctrl)
                except Exception as rollback_error:
                    error = DebugError("restore-failed",
                        "Preview was applied but its previous state could not be restored: " +
                        str(rollback_error) + "; preview diagnostic: " + str(preview_error))
                    error.preview_committed = True
                    error.rollback_state = before
                    raise error
                if not owned:
                    raise DebugError("replacement-changed",
                        "Another operation changed the committed preview; its state was preserved")
                raise
        bridge._replay(run)
        self._notify()

    def restore(self, session, require_owned=True):
        if session["facts"].get("scope") == RESOURCE_SCOPE:
            return self._restore_resource(session, require_owned)
        bridge = self.bridge
        facts = session["facts"]
        if self._capture() != facts["capture"]:
            return  # The old controller destroyed its own temporary resources.
        def run(ctrl):
            if self._capture() != facts["capture"]:
                return
            bridge._require_healthy_replay(ctrl)
            if require_owned and not self._owned(ctrl, session):
                current_state = self._state(ctrl.GetEventShaderOverride(facts["eventId"], facts["stage"]))
                def matches(expected):
                    return (expected is not None and current_state["active"] == expected["active"] and
                        (not current_state["active"] or all(current_state[key] == expected[key] for key in
                            ("expectedOriginal", "replacement", "outputTarget", "independentFloatOutput"))))
                if session.get("recoveryRequired") and matches(session["restoreState"]):
                    return  # Rollback committed, but its response was lost.
                if not (session.get("recoveryRequired") and matches(session.get("recoveryRollbackState"))):
                    raise DebugError("replacement-changed", "Another operation owns this event override; its state was preserved")
            if session["status"] == "comparing":
                return
            state = session["restoreState"]
            current = int(ctrl.GetCurrentEventID())
            # If a void clear or event replay fails remotely after updating the
            # local bookkeeping, a later healthy Stop must reconcile that state.
            session["recoveryRequired"] = True
            try:
                if state["active"]:
                    if current != facts["eventId"]:
                        ctrl.SetFrameEvent(facts["eventId"], True)
                        bridge._require_healthy_replay(ctrl)
                    error = ctrl.SetEventShaderOverride(facts["eventId"], facts["stage"],
                        state["expectedOriginal"], state["replacement"], state["outputTarget"],
                        state.get("independentFloatOutput", False))
                    if error:
                        raise DebugError("restore-failed", str(error))
                else:
                    ctrl.ClearEventShaderOverride(facts["eventId"], facts["stage"])
                bridge._require_healthy_replay(ctrl)
            finally:
                if int(ctrl.GetCurrentEventID()) != current:
                    bridge._require_healthy_replay(ctrl)
                    ctrl.SetFrameEvent(current, True)
                    bridge._require_healthy_replay(ctrl)
        bridge._replay(run)
        self._notify()

    def _notify(self):
        # A viewer refresh is not the mutation commit. A closing UI must never
        # turn a successfully installed override into an unowned failed request.
        try:
            self.bridge._notify_shader_replay_changed()
        except Exception:
            pass

    def free(self, cached, capture):
        if capture == self._capture():
            def run(ctrl):
                if capture != self._capture():
                    return
                self.bridge._require_healthy_replay(ctrl)
                ctrl.FreeTargetResource(cached["target"])
                self.bridge._require_healthy_replay(ctrl)
            self.bridge._replay(run)


def _profile_from_container(raw):
    import struct
    if raw[:4] != b"DXBC" or len(raw) < 32:
        return ""
    count = struct.unpack_from("<I", raw, 28)[0]
    if count > 128 or 32 + count * 4 > len(raw):
        return ""
    for i in range(count):
        offset = struct.unpack_from("<I", raw, 32 + i * 4)[0]
        if offset + 12 > len(raw) or raw[offset:offset + 4] not in (b"SHEX", b"SHDR", b"DXIL"):
            continue
        token = struct.unpack_from("<I", raw, offset + 8)[0]
        major, minor, stage = (token >> 4) & 15, token & 15, token >> 16
        if stage == 0 and 4 <= major <= 6:
            return "ps_%d_%d" % (major, minor)
    return ""


def _validate_interface(bridge, baseline, candidate, output, allow_fragment_position=False):
    """Allow only the chosen color output to change its width/numeric type."""
    baseline, candidate = copy.deepcopy(baseline), copy.deepcopy(candidate)
    slot = int(output["index"])
    def selected(item):
        system = str(item.get("systemValue", "")).lower()
        semantic = str(item.get("semanticName", "")).lower()
        return (int(item.get("regIndex", -1)) == slot and
                ("color" in system or semantic in ("sv_target", "color") or system in ("undefined", "none", "")))
    outputs = [item for item in candidate.get("outputSignature", []) if selected(item)]
    if len(outputs) != 1:
        raise DebugError("interface-mismatch", "The preview must declare exactly one output at the chosen target location")
    item = outputs[0]
    actual = str(item.get("compType", "")).lower()
    required = output["componentType"]
    if required == "sint":
        required = "int"
    if actual not in ({"int", "sint"} if required == "int" else {required}):
        raise DebugError("interface-mismatch", "Preview output type does not match the target attachment")
    if int(item.get("compCount", 0)) < int(output["components"]):
        raise DebugError("interface-mismatch", "Preview output does not cover every target channel")
    baseline["outputSignature"] = [x for x in baseline.get("outputSignature", []) if not selected(x)]
    candidate["outputSignature"] = [x for x in candidate.get("outputSignature", []) if not selected(x)]
    if allow_fragment_position:
        # GLSL's generated unreached-pixel checker reads gl_FragCoord. This
        # fixed-function input needs no vertex output or descriptor binding,
        # even if the original fragment program did not use it. Permit only
        # this generated float4 builtin, never a new user varying.
        candidate["inputSignature"] = [item for item in candidate.get("inputSignature", [])
            if not (item not in baseline.get("inputSignature", []) and
                    item.get("systemValue") == "position" and item.get("compType") == "float" and
                    int(item.get("compCount", 0)) == 4 and not item.get("perPrimitiveRate", False))]
    # Wrapper names are implementation details; retain the same stage/linkage.
    candidate["entryPoint"] = baseline["entryPoint"]
    differences = bridge._interface_incompatibilities(baseline, candidate)
    if differences:
        raise DebugError("interface-mismatch", "Preview changed captured bindings or stage interfaces: " + ", ".join(differences))
