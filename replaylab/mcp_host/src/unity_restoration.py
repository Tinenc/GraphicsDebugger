"""Durable, evidence-bound Unity restoration and repair iterations.

The Agent authors the adaptation. This module owns identity, coverage, freshness
and acceptance: an import, a self-assessment, or two arbitrary PNGs cannot finish
a restoration. All observations are supplied by Host tool implementations, not
through a model-facing 'record success' endpoint.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Any, Mapping
import uuid

SCHEMA = "bread-unity-restoration/v1"
REVIEW_AREAS = ("geometry_and_coordinates", "material_and_constants", "lighting_and_indirect",
                "render_states_and_passes", "scene_and_dependencies", "readable_source",
                "core_algorithm", "visible_effect")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_JSON = 32 * 1024 * 1024


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def file_digest(path: str | Path, limit: int = 512 * 1024 * 1024) -> str:
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Restoration evidence is missing, relative or too large")
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("Restoration evidence path is redirected")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _json(path: Path, limit: int = _MAX_JSON) -> dict:
    file_digest(path, limit)
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Restoration metadata must be an object")
    return value


def _external_dependency(row: Mapping[str, Any]) -> Path:
    """A Unity-resolved package file is evidence, never an arbitrary project write."""
    package = row.get("package") or {}
    name = package.get("name", "")
    if (not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]+", name)
            or not package.get("version") or not package.get("source")):
        raise ValueError("External dependency lacks its actual Unity package identity")
    prefix = "Packages/" + name + "/"
    asset = str(row.get("assetPath", ""))
    if not asset.startswith(prefix) or any(p in {"", ".", ".."} for p in asset[len(prefix):].split("/")) or "\\" in asset:
        raise ValueError("External dependency is not an exact package asset")
    root, path = Path(str(package.get("resolvedPath", ""))), Path(str(row.get("path", "")))
    if (not root.is_absolute() or not path.is_absolute() or not path.resolve().is_relative_to(root.resolve())
            or path.resolve() != (root / asset[len(prefix):]).resolve()):
        raise ValueError("External dependency differs from its Unity-resolved package path")
    return path


def _image(receipt: Mapping[str, Any]) -> dict:
    image = receipt.get("image") or ((receipt.get("artifact") or {}).get("output")) or {}
    path, sha = image.get("path"), image.get("sha256")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha) or file_digest(str(path), 64 * 1024 * 1024) != sha:
        raise ValueError("Rendered/reference image is missing or changed")
    from PIL import Image
    with Image.open(str(path)) as opened:
        if opened.format != "PNG":
            raise ValueError("Restoration display evidence must be PNG")
        width, height = opened.size
    return {"path": str(path), "sha256": sha, "width": width, "height": height}


def package_bundle(package_path: str) -> tuple[dict, dict]:
    """Read the exact bundle named by the already validated package manifest."""
    root = Path(package_path).resolve(strict=True)
    manifest = _json(root / "package-manifest.json")
    matches = []
    for row in manifest.get("files", []):
        relative = row.get("path", "")
        if (relative.startswith("source/") and relative.endswith(".json")
                and row.get("sha256") == manifest.get("captureBundleSha256")):
            candidate = root / relative
            if not candidate.resolve().is_relative_to(root) or file_digest(candidate) != row["sha256"]:
                raise ValueError("Captured source bundle changed")
            matches.append(_json(candidate))
    if len(matches) != 1:
        raise ValueError("Package must name exactly one captured source bundle")
    if manifest.get("captureSource"):
        matches[0]["captureSource"] = manifest["captureSource"]
    return matches[0], _json(root / "transfer-spec.json")


def _acceptance(value: Mapping[str, Any] | None) -> dict:
    value = dict(value or {})
    if set(value) - {"mode", "fidelity", "channels", "absoluteTolerance", "maxChangedPixelFraction"}:
        raise ValueError("Unknown restoration acceptance setting")
    mode = value.get("mode", "strict-pixel" if any(key in value for key in ("absoluteTolerance", "maxChangedPixelFraction")) else "visual-fidelity")
    if mode not in {"strict-pixel", "visual-fidelity"}:
        raise ValueError("Acceptance mode must be strict-pixel or visual-fidelity")
    result = {"mode": mode, "channels": value.get("channels", "RGB"),
              "absoluteTolerance": value.get("absoluteTolerance", 1 / 255),
              "maxChangedPixelFraction": value.get("maxChangedPixelFraction", 0.0)}
    if mode == "visual-fidelity":
        from .unity_visual_validation import normalize_fidelity_policy
        result["fidelity"] = normalize_fidelity_policy(value.get("fidelity"))
    elif value.get("fidelity") is not None:
        raise ValueError("Fidelity settings require visual-fidelity mode")
    if result["channels"] not in {"RGB", "RGBA"}:
        raise ValueError("Comparison channels must be RGB or RGBA")
    for key in ("absoluteTolerance", "maxChangedPixelFraction"):
        number = result[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 <= number <= 1:
            raise ValueError("Comparison tolerances must be finite values from 0 to 1")
    return result


def reference_selection(value: Mapping[str, Any]) -> dict:
    """Explicit output identity and shared display mapping; never auto-normalize."""
    if not isinstance(value, Mapping):
        raise ValueError("Provide the captured output/pass to compare")
    allowed = {"event_id", "attachment_kind", "attachment_index", "stage", "channel", "mip", "slice", "sample",
               "range_mode", "black_point", "white_point", "value_decode", "flip_y"}
    if set(value) - allowed:
        raise ValueError("Comparison references cannot crop, resize or select a filesystem path")
    result = {"attachment_kind": "rtv", "attachment_index": 0, "stage": "fragment", "channel": "rgba",
              "mip": 0, "slice": 0, "sample": 0, "range_mode": "native", "value_decode": "native", "flip_y": False,
              **dict(value)}
    if result["range_mode"] not in {"native", "explicit"}:
        raise ValueError("Use a fixed shared display range for restoration comparisons")
    if result["attachment_kind"] not in {"rtv", "dsv", "uav", "srv", "present"}:
        raise ValueError("Unknown comparison attachment kind")
    if type(result.get("event_id")) is not int or result["event_id"] <= 0:
        raise ValueError("Comparison reference requires an exact event_id")
    return result


def display_mapping(reference: Mapping[str, Any]) -> dict:
    return {key: reference[key] for key in ("channel", "range_mode", "black_point", "white_point", "value_decode", "flip_y")
            if key in reference}


def target_output_selection(value: Mapping[str, Any] | None, reference: Mapping[str, Any]) -> dict:
    if value is None:
        if reference["attachment_kind"] not in {"rtv", "present"} or reference["attachment_index"] != 0:
            raise ValueError("An intermediate/MRT/depth output needs an explicit matching Unity target_output")
        return {"kind": "camera-color"}
    if not isinstance(value, Mapping) or set(value) - {"kind", "name"}:
        raise ValueError("target_output must select camera-color or a named global-texture")
    if value.get("kind") == "camera-color" and not value.get("name"):
        return {"kind": "camera-color"}
    if value.get("kind") == "global-texture" and isinstance(value.get("name"), str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,255}", value["name"]):
        return dict(value)
    raise ValueError("Invalid Unity target_output")


class RestorationStore:
    def __init__(self, root: str | Path | None = None):
        data = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        self.root = Path(root or os.environ.get("BREAD_UNITY_RESTORATION_ROOT") or data / "ReplayLab/unity/restorations").absolute()
        self._lock = threading.RLock()

    def _path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
            raise ValueError("Invalid restoration ID")
        path = self.root / task_id / "task.json"
        for part in (path, *path.parents):
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise ValueError("Restoration task path is redirected")
        return path

    @contextmanager
    def transaction(self, task_id: str):
        from .unity_integration import _file_lock
        path = self._path(task_id)
        with self._lock, _file_lock(path.with_suffix(".lock"), threading.Event(), timeout=30):
            state = _json(path)
            yield state
            self._save(state)

    def _save(self, state: dict) -> None:
        from .shader_workspace.io import atomic_write_json
        path = self._path(state["restorationId"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if len(json.dumps(state, ensure_ascii=False).encode()) > _MAX_JSON:
            raise ValueError("Restoration ledger exceeds its size limit")
        atomic_write_json(path, state)

    def begin(self, capture: Mapping[str, Any], event_id: int, project_path: str,
              reference: Mapping[str, Any], acceptance: Mapping[str, Any] | None = None,
              target_output: Mapping[str, Any] | None = None) -> dict:
        from .unity_integration import _project
        project = _project(project_path)
        if type(event_id) is not int or event_id <= 0:
            raise ValueError("event_id must be positive")
        if not _ID.fullmatch(str(capture.get("captureSessionId", ""))):
            raise ValueError("A live capture session is required to begin restoration")
        reference = reference_selection(reference)
        if reference["event_id"] != event_id:
            raise ValueError("Reference must observe the selected EID; unrelated later outputs cannot certify this draw")
        from .capture_source import capture_source
        capture = {**capture, "sourceIdentity": capture_source(dict(capture))}
        state = {"schema": SCHEMA, "restorationId": uuid.uuid4().hex, "revision": 1,
            "capture": dict(capture), "eventId": event_id, "projectPath": str(project),
            "referenceSelection": reference, "targetOutput": target_output_selection(target_output, reference),
            "displayMapping": display_mapping(reference), "acceptance": _acceptance(acceptance),
            "sources": [], "binding": None, "reference": None, "inspection": None, "comparison": None,
            "portingReview": None, "iterations": [], "reconstructions": [], "recoveryAttempts": [], "status": "needs-work"}
        self._save(state)
        return self.status(state["restorationId"])

    @staticmethod
    def _invalidate(state: dict, reason: str) -> None:
        state["revision"] += 1
        state.update(status="needs-work", comparison=None, portingReview=None)
        state["lastChange"] = reason

    @staticmethod
    def _adopt_capture(state: dict, capture: dict) -> None:
        """A restoration records the selected RDC/EID, not a replay process lease.

        Legacy records only stored the path. Adopt that intent once and discard
        old observations; never claim that they validated the new session.
        """
        from .capture_source import capture_source, path_key, same_source
        old = state["capture"]
        before = old.get("sourceIdentity") or {"path": old.get("path", "")}
        after = capture.get("sourceIdentity") or capture_source(capture)
        session_changed = old.get("captureSessionId") != capture.get("captureSessionId")
        source_changed = False
        if before.get("contentSha256") or session_changed:
            if not same_source(before, after, legacy_path=True):
                # Saving resource names/annotations also changes RDC file bytes.
                # The same selected path is a continuation intent, not a claim
                # that old validation covers new contents. Report the change,
                # retain history, and let the Agent inspect current resources.
                if not (before.get("path") and after.get("path")
                        and path_key(before["path"]) == path_key(after["path"])):
                    raise ValueError("Export/reference is not this task's captured output: different RDC source")
                source_changed = True
                state.setdefault("diagnostics", []).append({"code": "capture-file-identity-changed",
                    "action": "Inspect current EID bindings; old validation is retained as historical evidence"})
        if session_changed or source_changed:
            state.setdefault("captureHistory", []).append({"capture": old,
                "reference": state.get("reference"), "revision": state["revision"],
                "reason": "capture-file-refreshed" if source_changed else
                    "same-rdc-reopened" if before.get("contentSha256") else "legacy-path-intent-adopted"})
            state["reference"] = None
            state["inspection"] = None
            RestorationStore._invalidate(state, "same-rdc-reopened")
        state["capture"] = {**old, **capture, "sourceIdentity": after}

    def observe_capture(self, task_id: str, capture: dict) -> dict:
        with self.transaction(task_id) as state:
            self._adopt_capture(state, capture)
        return self.status(task_id)

    def bind(self, task_id: str, export: Mapping[str, Any], prepared: Mapping[str, Any], sources: list[dict],
             capture: dict | None = None) -> dict:
        from .unity_shader_sources import bind_sources_to_bundle
        if export.get("status") != "finalized":
            raise ValueError("The resource export must be finalized before importing")
        request_path = Path(str(prepared["requestPath"]))
        # Import requests can contain manifested GPU buffer bytes. Keep the
        # bounded ledger small, while honoring the importer's request limit.
        from .unity_restore import MAX_FILE_BYTES
        request = _json(request_path, MAX_FILE_BYTES)
        bundle, spec = package_bundle(str(export["packagePath"]))
        # The native blobs/reflection remain in the validated export. Readable
        # source is optional reference data, not a required replay replacement.
        selected = bind_sources_to_bundle(sources, bundle, require_all=False)
        plan = (spec.get("unity") or {}).get("renderPlan")
        plan_review = None
        if plan is not None:
            from .unity_render_plan import validate_render_plan
            plan_review = validate_render_plan(plan)
        recorded = {(s.get("stage"), s.get("sourceSha256")) for s in (spec.get("shader") or {}).get("organizedSources", [])}
        if recorded != {(s["stage"], s["sourceSha256"]) for s in selected}:
            raise ValueError("Export does not contain the current organized Shader sources; finalize its transfer spec with shader.organizedSources")
        with self.transaction(task_id) as state:
            if bundle.get("eventId") != state["eventId"]:
                raise ValueError("Export belongs to a different EID")
            source = bundle.get("captureSource")
            if source:
                capture = {"path": source.get("path", ""), "api": bundle.get("api", ""),
                    "sourceIdentity": source, "captureSessionId": bundle.get("captureSessionId")}
            if capture is not None:
                if capture.get("captureSessionId") != bundle.get("captureSessionId"):
                    raise ValueError("Capture context does not identify the exported bundle")
                self._adopt_capture(state, capture)
            elif bundle.get("captureSessionId") != state["capture"]["captureSessionId"]:
                # Old packages predate portable source provenance. They remain
                # valid data snapshots after every originating replay session
                # has closed. Do not require a live lease to import saved files.
                # Keep the task's selected capture intent and expose the missing
                # provenance; current reference reads can refresh it separately.
                diagnostic = {"code": "legacy-export-source-context-unrecorded",
                    "exportCaptureSessionId": bundle.get("captureSessionId"),
                    "action": "Inspect the saved EID resources and current source as needed; import is available"}
                if diagnostic not in state.setdefault("diagnostics", []):
                    state["diagnostics"].append(diagnostic)
            if os.path.normcase(str(Path(request["projectPath"]).resolve())) != os.path.normcase(state["projectPath"]):
                raise ValueError("Import request belongs to another Unity project")
            state["sources"] = selected
            state["binding"] = {"operationId": export["operationId"], "packagePath": export["packagePath"],
                "requestPath": str(request_path), "requestFileSha256": file_digest(request_path),
                "requestSha256": request["requestSha256"], "packageManifestSha256": request["packageManifestSha256"],
                "requestGaps": request.get("gaps", []), "renderPlan": plan, "renderPlanReview": plan_review,
                "parameterCoverage": request.get("constantBufferCoverage", {}),
                "executionKind": request.get("executionKind", "draw"), "destination": request.get("destination", ""),
                "systemInterfaces": [row for row in request.get("runtimeBindings", [])
                    if row.get("kind") == "system" or row.get("classification") == "system"]}
            state["inspection"] = None
            state["reconstructions"] = []
            state.pop("effectiveRenderPlan", None)
            self._invalidate(state, "bound-current-export-and-organized-sources")
        return self.status(task_id)

    def reference(self, task_id: str, receipt: Mapping[str, Any]) -> dict:
        image = _image(receipt)
        with self.transaction(task_id) as state:
            if receipt.get("eventId") != state["referenceSelection"]["event_id"]:
                raise ValueError("Reference is not from this task's captured output")
            self._adopt_capture(state, dict(receipt.get("capture") or {}))
            record = {"image": image, "selection": state["referenceSelection"],
                      "manifest": receipt.get("manifest"), "artifact": receipt.get("artifact")}
            if state.get("reference") != record:
                state["reference"] = record
                self._invalidate(state, "captured-reference")
        return self.status(task_id)

    @staticmethod
    def _observation(state: dict, observation: Mapping[str, Any]) -> dict:
        binding = state.get("binding")
        if not binding:
            raise ValueError("Bind a finalized export before observing Unity")
        if observation.get("schema") != "bread-unity-capture-observation/v1":
            raise ValueError("The Editor did not return a capture observation")
        for key in ("requestSha256", "packageManifestSha256"):
            if observation.get(key) != binding[key]:
                raise ValueError("Unity observation belongs to a different import request")
        if not isinstance(observation.get("checks"), Mapping) or not observation.get("files"):
            raise ValueError("Unity observation omits actual checks or dependency files")
        root = Path(state["projectPath"]).resolve()
        if observation.get("projectPath") is not None and Path(str(observation["projectPath"])).resolve() != root:
            raise ValueError("Unity observation belongs to a different project")
        if observation.get("executionKind", "draw") != binding.get("executionKind", "draw"):
            raise ValueError("Unity observation executionKind differs from the bound import")
        if binding.get("executionKind") == "effect" and observation.get("geometryApplicable") is not False:
            raise ValueError("Effect observation must explicitly declare geometry inapplicable")
        for row in observation["files"]:
            path = Path(str(row.get("path", "")))
            if not path.is_absolute() or not path.resolve().is_relative_to(root):
                raise ValueError("Unity observation contains a file outside the selected project")
            if file_digest(path) != row.get("sha256"):
                raise ValueError("Unity dependency changed during inspection")
        for row in observation.get("externalDependencies", []):
            if file_digest(_external_dependency(row)) != row.get("sha256"):
                raise ValueError("Unity package dependency changed during inspection")
        result = dict(observation)
        if file_digest(binding["requestPath"]) != binding["requestFileSha256"]:
            raise ValueError("Immutable import request changed before parameter readback")
        from .unity_parameter_readback import verify_parameter_readback
        from .unity_restore import MAX_FILE_BYTES
        result["parameterReadbackGaps"] = verify_parameter_readback(
            _json(Path(binding["requestPath"]), MAX_FILE_BYTES), result)
        # A render timestamp/image path does not change the scene fingerprint.
        stable = {key: result.get(key) for key in ("requestSha256", "packageManifestSha256", "scenePath",
            "material", "runtimeBindings", "camera", "object", "lights", "components", "rendererFeatures", "requestedRenderState",
            "renderPipeline", "colorSpace", "targetGraphicsAPI", "unityVersion", "virtualDependencies",
            "checks", "gaps", "parameterReadbackGaps", "renderPlan", "executionKind", "applicability",
            "geometryApplicable", "effect")}
        stable["files"] = sorted(result["files"], key=lambda row: os.path.normcase(row["path"]))
        stable["externalDependencies"] = sorted(result.get("externalDependencies", []), key=lambda row: os.path.normcase(row["path"]))
        result["stateFingerprint"] = digest(stable)
        return result

    def inspect(self, task_id: str, observation: Mapping[str, Any]) -> dict:
        with self.transaction(task_id) as state:
            observed = self._observation(state, observation)
            if (state.get("inspection") or {}).get("stateFingerprint") != observed["stateFingerprint"]:
                self._invalidate(state, "unity-scene-or-validation-changed")
            state["inspection"] = observed
            state.pop("lastToolFailure", None)
        return self.status(task_id)

    def compare(self, task_id: str, observation: Mapping[str, Any]) -> dict:
        from .unity_visual_validation import compare_render_images
        with self.transaction(task_id) as state:
            observed = self._observation(state, observation)
            if observed.get("displayMapping") != state["displayMapping"] or observed.get("targetOutput") != state["targetOutput"]:
                raise ValueError("Unity rendered a different output or display mapping; fix the readback route before comparison")
            if not state.get("reference"):
                raise ValueError("Capture the task's exact reference before rendering Unity")
            if (state.get("inspection") or {}).get("stateFingerprint") != observed["stateFingerprint"]:
                self._invalidate(state, "unity-render-state-changed")
            state["inspection"] = observed
            reference, rendered = state["reference"]["image"], _image(observed)
            settings = state["acceptance"]
            comparison = compare_render_images(reference["path"], rendered["path"], reference["sha256"], rendered["sha256"],
                channels=settings["channels"], absolute_tolerance=settings["absoluteTolerance"],
                max_changed_pixel_fraction=settings["maxChangedPixelFraction"],
                mode=settings.get("mode", "strict-pixel"), fidelity=settings.get("fidelity"))
            comparison.update(stateFingerprint=observed["stateFingerprint"], revision=state["revision"],
                restorationId=task_id, capture=state["capture"], eventId=state["eventId"],
                referenceSelection=state["referenceSelection"], requestSha256=state["binding"]["requestSha256"])
            state["comparison"] = comparison
            state.pop("lastToolFailure", None)
            previous = state["iterations"][-1] if state["iterations"] else None
            iteration = {"stateFingerprint": observed["stateFingerprint"], "referenceSha256": reference["sha256"],
                "renderedSha256": rendered["sha256"], "changedPixelFraction": comparison["changedPixelFraction"],
                "meanAbsoluteError": comparison["meanAbsoluteError"], "withinTolerance": comparison["withinTolerance"]}
            iteration["progress"] = "first-observation" if previous is None else (
                "unchanged" if all(previous.get(k) == iteration[k] for k in ("stateFingerprint", "referenceSha256", "renderedSha256"))
                else "improved" if iteration["meanAbsoluteError"] < previous["meanAbsoluteError"] else "changed-not-improved")
            state["iterations"] = [*state["iterations"], iteration][-64:]
        return self.status(task_id)

    def review(self, task_id: str, state_fingerprint: str, assessments: Mapping[str, str],
               decisions: list[dict], limitations: list[str]) -> dict:
        """Record semantic decisions only after examining the current artifacts.

        Mechanical and image failures remain gaps even with a complete review.
        Each module records a port/Unity interface/frozen/approximation decision.
        """
        if not isinstance(assessments, Mapping) or set(assessments) != set(REVIEW_AREAS):
            raise ValueError("Review must cover source mapping, core algorithm, visible effect and all restoration areas")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in assessments.values()):
            raise ValueError("Review areas need concise implementation-specific evidence")
        if not isinstance(decisions, list) or not decisions or len(decisions) > 256:
            raise ValueError("Provide the reviewed computation/module mapping")
        for row in decisions:
            if (not isinstance(row, Mapping) or row.get("disposition") not in {"ported", "unity-native", "captured-data", "approximation"}
                    or any(not isinstance(row.get(k), str) or not row[k].strip() for k in ("module", "source", "target", "evidence"))):
                raise ValueError("Each module needs source, target, disposition and evidence")
        if (not isinstance(limitations, list) or len(limitations) > 64
                or any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in limitations)):
            raise ValueError("Invalid restoration limitations")
        if any(row["disposition"] == "approximation" for row in decisions) and not limitations:
            raise ValueError("Approximated modules need explicit limitations even when the selected image is within tolerance")
        with self.transaction(task_id) as state:
            if (state.get("inspection") or {}).get("stateFingerprint") != state_fingerprint:
                raise ValueError("Inspect current Unity artifacts before reviewing this revision")
            state["portingReview"] = {"stateFingerprint": state_fingerprint, "revision": state["revision"],
                "assessments": dict(assessments), "decisions": decisions, "limitations": limitations,
                "author": "agent", "hostSemanticApproval": False}
        return self.status(task_id)

    def use_visual_goal(self, task_id: str) -> dict:
        """Adopt the functional visual objective, without an iterative tolerance dial."""
        with self.transaction(task_id) as state:
            if state["acceptance"].get("mode", "strict-pixel") != "visual-fidelity":
                state["previousAcceptance"] = state["acceptance"]
                state["acceptance"] = _acceptance({"mode": "visual-fidelity"})
                self._invalidate(state, "objective-changed-to-core-algorithm-and-visible-effect")
        return self.status(task_id)

    def record_reconstruction(self, task_id: str, records: list[dict]) -> dict:
        from .unity_reconstruction import prepare_records
        if isinstance(records, list) and any(isinstance(row, Mapping) and row.get("retire") is True for row in records):
            if (not records or len(records) > 256 or any(not isinstance(row, Mapping) or row.get("retire") is not True
                    or not isinstance(row.get("id"), str) or not isinstance(row.get("reason"), str)
                    or not row["reason"].strip() or len(row["reason"]) > 4000 for row in records)):
                raise ValueError("Retire existing reconstructions separately, with their ids and repair reasons")
            ids = {row["id"] for row in records}
            existing = self.load(task_id)
            if len(ids) != len(records) or not ids <= {row["id"] for row in existing.get("reconstructions", [])}:
                raise ValueError("Retirement requires unique existing reconstruction ids")
            # Re-evaluate WITHOUT the proposed exceptions. A validated exception
            # otherwise hides precisely the source gap we must verify is gone.
            current = self.status(task_id, _exclude_reconstructions=ids)
            if not current["complete"]:
                raise ValueError("Retire only after the original gaps are repaired and current checks, render and review pass")
            with self.transaction(task_id) as state:
                if state["revision"] != current["revision"]:
                    raise ValueError("Restoration changed while retiring its reconstruction")
                state["reconstructionHistory"] = [*state.get("reconstructionHistory", []),
                    *[{"id": row["id"], "reason": row["reason"], "status": "retired-after-repair",
                       "stateFingerprint": current["inspection"]["stateFingerprint"], "revision": state["revision"]}
                      for row in records]][-256:]
                state["reconstructions"] = [row for row in state.get("reconstructions", []) if row["id"] not in ids]
            return self.status(task_id)
        current = self.status(task_id)
        prepared = prepare_records(current, records, file_digest)
        with self.transaction(task_id) as state:
            if state["revision"] != current["revision"]:
                raise ValueError("Restoration changed while preparing the inference; inspect it again")
            merged = {row["id"]: row for row in state.get("reconstructions", [])}
            for row in prepared:
                merged[row["id"]] = row
            if len(merged) > 256:
                raise ValueError("Too many reconstruction records")
            if list(merged.values()) != state.get("reconstructions", []):
                state["reconstructions"] = list(merged.values())
                self._invalidate(state, "reconstruction-implementation-recorded")
        return self.status(task_id)

    def adopt_observed_plan(self, task_id: str) -> dict:
        """Bind a real platform-adapted plan; validate it before replacing its checks."""
        from .unity_render_plan import validate_render_plan
        with self.transaction(task_id) as state:
            observed = state.get("inspection") or {}
            plan = observed.get("renderPlan")
            if not plan:
                raise ValueError("Inspect the implemented render plan in the target Editor first")
            for row in observed.get("files", []):
                if file_digest(row["path"]) != row["sha256"]:
                    raise ValueError("Implemented plan files changed; inspect again")
            for row in observed.get("externalDependencies", []):
                if file_digest(_external_dependency(row)) != row["sha256"]:
                    raise ValueError("Implemented plan package files changed; inspect again")
            review = validate_render_plan(plan)
            if review["status"] != "valid" or review["descriptorValidation"] != "checked":
                raise ValueError("Implemented plan still has invalid resources, dependencies or implementation descriptors")
            if (plan["target"]["pipeline"] != observed.get("renderPipeline")
                    or plan["target"]["editorVersion"] != observed.get("unityVersion")):
                raise ValueError("Implemented plan target differs from the observed pipeline or Editor version")
            root = Path(state["projectPath"]).resolve()
            observed_paths = {Path(row["path"]).resolve() for row in observed.get("files", [])}
            package_assets = {row.get("assetPath") for row in [*observed.get("files", []),
                *observed.get("externalDependencies", [])] if str(row.get("assetPath", "")).startswith("Packages/")}
            for asset in plan["implementationAssets"]:
                if asset.startswith("Packages/"):
                    if asset not in package_assets and (root / asset).resolve() not in observed_paths:
                        raise ValueError("Implemented plan package asset is absent from observed dependency files: " + asset)
                    continue
                path = (root / asset if asset.startswith("Assets/") else
                    root / state["binding"].get("destination", "") / asset).resolve()
                if not path.is_relative_to(root / "Assets") or path not in observed_paths:
                    raise ValueError("Implemented plan asset is absent from observed project dependency files: " + asset)
            if plan != (state.get("effectiveRenderPlan") or {}).get("plan", state["binding"].get("renderPlan")):
                state["effectiveRenderPlan"] = {"plan": plan, "review": review,
                    "requestSha256": state["binding"]["requestSha256"], "origin": "observed-platform-adaptation"}
                self._invalidate(state, "adopted-observed-platform-render-plan")
        return self.status(task_id)

    def record_recovery_attempt(self, task_id: str, route_id: str, context_fingerprint: str,
                                result: Mapping[str, Any], *, failed: bool = False) -> dict:
        """Only called by tool implementations after an actual operation."""
        with self.transaction(task_id) as state:
            row = {"routeId": route_id, "contextFingerprint": context_fingerprint, "failed": failed,
                "outcome": "failed" if failed else "authored" if result.get("changedFiles") else "no-improvement",
                "artifactHashes": [str(item.get("sha256")) for item in result.get("files", [])],
                "observationFingerprint": (state.get("inspection") or {}).get("stateFingerprint"),
                "diagnostic": str(result.get("error") or result.get("message") or "")[:2000]}
            if not state.get("recoveryAttempts") or state["recoveryAttempts"][-1] != row:
                state["recoveryAttempts"] = [*state.get("recoveryAttempts", []), row][-128:]
            if failed:
                state["lastToolFailure"] = {"code": "recovery-operation-failed", "routeId": route_id, "detail": row["diagnostic"]}
        return self.status(task_id)

    def record_failure(self, task_id: str, tool: str, error: Any) -> None:
        with self.transaction(task_id) as state:
            state["lastToolFailure"] = {"code": "unity-tool-failed", "tool": tool, "detail": str(error)[:4000]}

    def invalidate(self, task_id: str, reason: str) -> dict:
        with self.transaction(task_id) as state:
            self._invalidate(state, reason)
            state["inspection"] = None
        return self.status(task_id)

    def invalidate_review(self, task_id: str) -> dict:
        with self.transaction(task_id) as state:
            state["portingReview"] = None
            state["lastChange"] = "latest-semantic-review-failed"
        return self.status(task_id)

    def load(self, task_id: str) -> dict:
        state = _json(self._path(task_id))
        if state.get("schema") != SCHEMA or state.get("restorationId") != task_id:
            raise ValueError("Restoration task identity changed")
        return state

    def status(self, task_id: str, *, verify_files: bool = True, _exclude_reconstructions: set[str] | None = None) -> dict:
        from .unity_shader_sources import verify_source_files
        state = self.load(task_id)
        if _exclude_reconstructions:
            state["reconstructions"] = [row for row in state.get("reconstructions", []) if row["id"] not in _exclude_reconstructions]
        gaps = []

        def gap(code: str, tool: str, detail: Any = None) -> None:
            row = {"code": code, "tool": tool}
            if detail is not None:
                row["detail"] = detail
            gaps.append(row)

        if not state["binding"]:
            gap("export-and-organized-sources-required", "bind_unity_restoration")
        elif verify_files:
            for item in verify_source_files(state["sources"]):
                gap(item["code"], "bind_unity_restoration", item)
            try:
                binding = state["binding"]
                if file_digest(binding["requestPath"]) != binding["requestFileSha256"]:
                    raise ValueError("Import request changed")
                if file_digest(Path(binding["packagePath"]) / "package-manifest.json") != binding["packageManifestSha256"]:
                    raise ValueError("Export manifest changed")
            except (OSError, ValueError) as error:
                gap("restoration-input-changed", "bind_unity_restoration", str(error))
        binding = state.get("binding") or {}
        for item in binding.get("requestGaps", []):
            gap("export-configuration-gap", "bind_unity_restoration", item)
        coverage = binding.get("parameterCoverage", {})
        if binding and (not coverage or coverage.get("unmappedLaneCount") != 0):
            gap("cbuffer-coverage-incomplete", "bind_unity_restoration", coverage)
        effective = state.get("effectiveRenderPlan") or {}
        plan = effective.get("plan", binding.get("renderPlan"))
        plan_review = effective.get("review", binding.get("renderPlanReview")) or {}
        if (binding.get("executionKind") == "effect" and plan is None
                or plan is not None and (plan_review.get("status") != "valid"
                or plan_review.get("descriptorValidation") != "checked")):
            gap("render-plan-implementation-required", "bind_unity_restoration", plan_review)
        if not state["reference"]:
            gap("captured-reference-required", "render_unity_restoration")
        inspection = state.get("inspection") or {}
        if not inspection:
            gap("unity-inspection-required", "check_unity_restoration")
        else:
            checks = inspection.get("checks", {})
            if binding.get("executionKind") == "effect":
                effect = inspection.get("effect") or {}
                if (effect.get("kind") != "executable-effect" or effect.get("uploadsVerified") is not True
                        or effect.get("error") or not effect.get("passes")):
                    gap("effect-input-or-implementation-required", "check_unity_restoration", effect)
            for check in ("meshImport", "shaderCompile", "materialImport", "textureImport", "sceneIntegrity", "pipelineCompatible"):
                if checks.get(check) is not True:
                    gap("unity-check-failed", "check_unity_restoration", check)
            for item in inspection.get("gaps", []):
                gap("unity-configuration-gap", "check_unity_restoration", item)
            for item in inspection.get("parameterReadbackGaps", []):
                gap("unity-parameter-readback-gap", "check_unity_restoration", item)
            if plan is not None and inspection.get("renderPlan") != plan:
                gap("render-plan-changed", "adopt_unity_restoration_render_plan")
            if verify_files:
                for row in inspection.get("files", []):
                    try:
                        if file_digest(row["path"]) != row["sha256"]:
                            raise ValueError("changed")
                    except (OSError, ValueError):
                        gap("unity-artifact-changed", "check_unity_restoration", row["path"])
                for row in inspection.get("externalDependencies", []):
                    try:
                        if file_digest(_external_dependency(row)) != row.get("sha256"):
                            raise ValueError("changed")
                    except (OSError, ValueError):
                        gap("unity-package-dependency-changed", "check_unity_restoration", row.get("path"))
        comparison = state.get("comparison") or {}
        if not comparison or comparison.get("stateFingerprint") != inspection.get("stateFingerprint") or comparison.get("revision") != state["revision"]:
            gap("current-render-comparison-required", "render_unity_restoration")
        elif comparison.get("withinTolerance") is not True:
            gap("render-mismatch", "render_unity_restoration", {key: comparison.get(key) for key in ("meanAbsoluteError", "changedPixelFraction", "changedBounds")})
        if comparison:
            executed = inspection.get("passExecution", [])
            if binding.get("executionKind") == "effect":
                effect = inspection.get("effect") or {}
                execution = inspection.get("effectExecution") or {}
                render_id = inspection.get("renderExecutionId")
                camera = (inspection.get("renderCamera") or {}).get("name")
                expected_iterations = effect.get("warmupFrames", 0) + 1
                if (not render_id or not camera or execution.get("renderExecutionId") != render_id
                        or execution.get("executedIterations") != expected_iterations
                        or any(execution.get(key) != value for key, value in effect.items())):
                    gap("current-effect-execution-required", "render_unity_restoration")
                for effect_pass in effect.get("passes", []):
                    if not any(row.get("passId") == effect_pass.get("id") and row.get("renderId") == render_id
                            and row.get("cameraName") == camera and row.get("implementationAsset") == effect_pass.get("asset")
                            for row in executed):
                        gap("effect-pass-not-observed", "render_unity_restoration", effect_pass.get("id"))
            requirements = plan_review.get("requiredUnityEvidence", [])
            observed_order = []
            for required in requirements:
                matching = [row for row in executed if isinstance(row, Mapping) and row.get("passId") == required["pass"]
                    and bool(inspection.get("renderExecutionId")) and row.get("renderId") == inspection["renderExecutionId"]
                    and row.get("cameraName") == (inspection.get("renderCamera") or {}).get("name")
                    and row.get("implementationAsset") == required["asset"]
                    and row.get("reads") == required["reads"] and row.get("writes") == required["writes"]
                    and all(row.get(key) == required[key] for key in ("kind", "entryPoint", "insertionPoint") if key in required)]
                if not matching:
                    gap("render-plan-pass-not-observed", "render_unity_restoration", required["pass"])
                else:
                    observed_order.append(required["pass"])
                    descriptors = {row["version"]: row for row in (plan or {}).get("resources", [])}
                    for version in required["writes"]:
                        descriptor = descriptors.get(version)
                        if descriptor and not any(row.get("outputVersion") == version
                                and all(row.get(key) == descriptor[key] for key in ("width", "height", "format", "samples"))
                                for row in matching):
                            gap("render-plan-output-descriptor-mismatch", "render_unity_restoration", {"pass": required["pass"], "resource": version})
            execution_indexes: dict[str, list[int]] = {}
            for index, row in enumerate(executed):
                if isinstance(row, Mapping):
                    execution_indexes.setdefault(str(row.get("passId", "")), []).append(index)
            for name, dependencies in plan_review.get("dependencies", {}).items():
                if name in execution_indexes and any(dep in execution_indexes
                        and max(execution_indexes[dep]) >= min(execution_indexes[name]) for dep in dependencies):
                    gap("render-plan-execution-order-mismatch", "render_unity_restoration", name)
            if state["targetOutput"]["kind"] == "global-texture":
                output_evidence = inspection.get("targetOutputEvidence") or {}
                if (output_evidence.get("currentRender") is not True or output_evidence.get("producerPassId") not in observed_order
                        or output_evidence.get("renderId") != inspection.get("renderExecutionId")
                        or not any(row.get("passId") == output_evidence.get("producerPassId")
                            and row.get("outputVersion") == output_evidence.get("outputVersion")
                            and row.get("outputInstanceId") == output_evidence.get("outputInstanceId")
                            and row.get("renderId") == output_evidence.get("renderId") for row in executed)):
                    gap("current-output-producer-not-observed", "render_unity_restoration")
        if verify_files and comparison:
            for key in ("reference", "rendered"):
                try:
                    row = comparison[key]
                    if file_digest(row["path"]) != row["sha256"]:
                        raise ValueError("changed")
                except (OSError, ValueError, KeyError):
                    gap("comparison-image-changed", "render_unity_restoration", key)
        review = state.get("portingReview") or {}
        if not review or review.get("stateFingerprint") != inspection.get("stateFingerprint") or review.get("revision") != state["revision"]:
            gap("porting-review-required", "review_unity_restoration")
        elif state["acceptance"].get("mode") == "visual-fidelity" and any(not (review.get("assessments") or {}).get(key) for key in ("core_algorithm", "visible_effect")):
            gap("core-algorithm-and-effect-review-required", "review_unity_restoration")
        for interface in binding.get("systemInterfaces", []):
            field_id = (interface.get("sourceIdentity") or {}).get("fieldId")
            target = interface.get("name") or interface.get("target")
            if not field_id or not any(row.get("fieldId") == field_id and row.get("target") == target
                    and row.get("disposition") == "unity-native" for row in review.get("decisions", [])):
                gap("system-interface-review-required", "review_unity_restoration", {
                    "fieldId": field_id, "target": target, "capturedValue": interface.get("value"),
                    "requirement": "Verify this exact captured field's coordinate space and replacement Unity interface in the derived source"})
        from .unity_reconstruction import assess, gap_id, reconstructable
        gaps, reconstructions = assess(state, gaps, file_digest)
        for item in gaps:
            item.update(gapId=gap_id(item), reconstructable=reconstructable(item))
        state["resolvedReconstructions"] = [row for row in reconstructions if row["status"] == "validated-reconstruction"]
        state["reconstructionChecks"] = reconstructions
        state.update(status="complete" if not gaps else "needs-repair" if state["binding"] else "needs-work",
            complete=not gaps, gaps=gaps, nextTools=list(dict.fromkeys(g["tool"] for g in gaps)),
            workflowPolicy="agent-directed", diagnosticsOnly=True,
            acceptanceScope="Selected output under the recorded display mapping and Unity configuration; RGB8 preview comparison does not certify raw HDR values, original alpha or universal GPU equivalence",
            taskPath=str(self._path(task_id)))
        if gaps:
            from .unity_recovery import plan_recovery
            recovery = plan_recovery(state, state.get("targetCapabilities"), state.get("recoveryAttempts"))
            available = [row["id"] for row in recovery["routes"] if row.get("state") == "available"]
            attempts = sorted({digest({"contextFingerprint": row.get("contextFingerprint"),
                "routeId": row.get("routeId"), "artifactHashes": sorted(row.get("artifactHashes", [])),
                "observationFingerprint": row.get("observationFingerprint"), "outcome": row.get("outcome")})
                for row in state.get("recoveryAttempts", []) if row.get("artifactHashes") or row.get("observationFingerprint")})
            state["recoveryPlan"] = recovery
            state["recovery"] = {"contextFingerprint": recovery["contextFingerprint"],
                "suggestedRouteId": recovery.get("suggestedRouteId"), "availableRouteIds": available,
                "attemptFingerprint": digest(attempts)}
            state["nextTools"] = list(dict.fromkeys([*state["nextTools"], "plan_unity_restoration_recovery"]))
        return state
