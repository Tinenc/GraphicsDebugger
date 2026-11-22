"""ReplayLab project launcher UI and private ProjectService client.

The built-in qrenderdoc integration deliberately does not duplicate project
persistence. Every create/activate/add operation uses the shared,
qrenderdoc-owned ``ReplayLabInternal.exe`` high-level HTTP service whose
``ActiveReplayProjectService`` owns the sealed JSON schema and game/build
boundary. ``ReplayLabMCP.exe`` is reserved for explicit external clients.
This module is Python 3.6 compatible for qrenderdoc.

RDC files are registered as references rather than copied. Embedded capture
thumbnails are read through ``OpenCaptureFile().OpenFile().GetThumbnail()``.
That API is RenderDoc's cheap metadata-only capture handle: this module never
calls ``OpenCapture`` and therefore never creates a replay device or changes
qrenderdoc's current capture/event. Reads are lazy, UI-thread scheduled, and
bounded by an in-memory LRU; unavailable/malformed thumbnails keep a visible
placeholder.
"""

from __future__ import absolute_import, print_function

import json
import importlib.util
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from collections import OrderedDict

try:
    from . import project_watch as _project_watch
    from . import project_permissions as _project_permissions
except (ImportError, ValueError):
    _watch_spec = importlib.util.spec_from_file_location(
        "_bread_project_watch", os.path.join(os.path.dirname(os.path.abspath(__file__)), "project_watch.py"))
    _project_watch = importlib.util.module_from_spec(_watch_spec)
    _watch_spec.loader.exec_module(_project_watch)
    _permission_spec = importlib.util.spec_from_file_location(
        "_bread_project_permissions", os.path.join(os.path.dirname(os.path.abspath(__file__)), "project_permissions.py"))
    _project_permissions = importlib.util.module_from_spec(_permission_spec)
    _permission_spec.loader.exec_module(_project_permissions)


PROJECT_LIST_SCHEMA = "replaylab-project-list/v1"
PROJECT_WORKSPACE_SCHEMA = "replaylab-project-workspace/v1"
CAPTURE_LIBRARY_SCHEMA = "replaylab-project-capture-library/v1"
CAPTURE_CONTENT_IDENTITY_SCHEMA = "replaylab-rdc-content-identity/v1"
CAPTURE_PROJECT_MATCH_SCHEMA = "replaylab-capture-project-match/v1"
MAX_CAPTURE_ARGUMENTS = 4096
MAX_THUMBNAIL_EDGE = 256
MAX_THUMBNAIL_BYTES = 4 * 1024 * 1024
MAX_THUMBNAIL_CACHE_ENTRIES = 256
MAX_THUMBNAIL_CACHE_BYTES = 64 * 1024 * 1024
MAX_THUMBNAILS_PER_SELECTION = 128
MAX_RESTORE_PROGRESS_EVENTS = 256
MAX_RESTORE_PROGRESS_COUNT = 1000000000


TRANSLATION_CONTEXT = "ReplayLabProjects"


def _qt_format(translated, *arguments):
    """Substitute Qt-style positional tokens after a direct translate call."""
    values = tuple(str(value) for value in arguments)

    def _replace(match):
        index = int(match.group(1))
        return values[index - 1] if 0 < index <= len(values) else match.group(0)

    # A single regex pass never treats %1 text inside a project name or path as
    # another placeholder. This mirrors the non-recursive property required by
    # translated UI formatting while remaining compatible with Python 3.6.
    return re.sub(r"%([1-9][0-9]*)", _replace, str(translated))


try:
    from . import project_restore_client as _internal_client_protocol
except (ImportError, ValueError):
    _internal_client_spec = importlib.util.spec_from_file_location(
        "_replaylab_internal_service_client",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "project_restore_client.py"))
    if (_internal_client_spec is None or
            _internal_client_spec.loader is None):
        raise ImportError("Unable to load the Bread internal service client")
    _internal_client_protocol = importlib.util.module_from_spec(
        _internal_client_spec)
    _internal_client_spec.loader.exec_module(_internal_client_protocol)


class ProjectLauncherError(RuntimeError):
    pass


class ThumbnailReadError(ProjectLauncherError):
    pass


def _normalise_restore_progress(value):
    """Return one bounded display-only project restore progress record."""
    if not isinstance(value, dict):
        raise ProjectLauncherError("Bread restore progress must be an object")

    phase = str(value.get("phase", "") or "").strip()
    message = str(value.get("message", "") or "").strip()
    raw_item = value.get("item", "")
    if not isinstance(raw_item, str):
        raise ProjectLauncherError("Bread restore progress text is invalid")
    item = raw_item.strip()
    if (not phase and not message) or len(phase) > 128 or len(message) > 1024:
        raise ProjectLauncherError("Bread restore progress text is invalid")
    try:
        item_bytes = item.encode("utf-8")
    except UnicodeEncodeError:
        raise ProjectLauncherError("Bread restore progress text is invalid")
    if (len(item_bytes) > 1024 or "\x00" in phase or "\x00" in message or
            "\x00" in item):
        raise ProjectLauncherError("Bread restore progress text is invalid")

    current_value = value.get("current", 0)
    total_value = value.get("total", 0)
    if isinstance(current_value, bool) or isinstance(total_value, bool):
        raise ProjectLauncherError("Bread restore progress counts are invalid")
    try:
        current = int(current_value)
        total = int(total_value)
    except (TypeError, ValueError, OverflowError):
        raise ProjectLauncherError("Bread restore progress counts are invalid")
    if (current < 0 or total < 0 or
            current > MAX_RESTORE_PROGRESS_COUNT or
            total > MAX_RESTORE_PROGRESS_COUNT):
        raise ProjectLauncherError("Bread restore progress counts are invalid")
    if total:
        current = min(current, total)
    else:
        current = 0
    return {
        "phase": phase,
        "message": message,
        "current": current,
        "total": total,
        "item": item,
    }


def _restore_progress_text(progress):
    phase = str(progress.get("phase", "") or "")
    if phase == "restore-dispatch":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Scheduling project recovery")
    elif phase == "client-validate":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Validating the project recovery request")
    elif phase == "host-state":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Checking the project service state")
    elif phase == "host-health":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Verifying the project service identity")
    elif phase == "restore-submit":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Starting the project recovery job")
    elif phase == "restore-queued":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Project recovery is queued")
    elif phase == "restore-running":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT,
            "The project service is running project recovery")
    elif phase == "result-validate":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Validating the project recovery result")
    elif phase == "validate":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Validating active project and capture")
    elif phase == "index-read":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Reading RDC project association cache")
    elif phase == "index-required":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT,
            "A unique Shader resource index is required before project data can be restored")
    elif phase == "cache-anchor":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Validating cached Shader anchors")
    elif phase == "cache-resource":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Validating cached Shader resources")
    elif phase == "cache-hit":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Validating cached live Shader anchors")
    elif phase == "resource-inventory":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Indexing unique Shader resources")
    elif phase == "resource-match":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Matching exact project Shader artifacts")
    elif phase == "resource-anchor":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT,
            "Validating one representative EID per Shader")
    elif phase == "prepare":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Preparing the current capture project")
    elif phase == "prepare-export-start":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Starting full-capture Shader export")
    elif phase == "prepare-export-wait":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Waiting for full-capture Shader export")
    elif phase == "prepare-export-validate":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Validating the exported Shader catalog")
    elif phase == "prepare-project-import":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Importing the Shader catalog into the project")
    elif phase == "prepare-reuse-plan":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Matching project Shader and CBuffer knowledge")
    elif phase == "prepare-catalog-register":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Registering the active capture catalog")
    elif phase == "prepare-symbol-resolve":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Resolving project Shader and CBuffer aliases")
    elif phase == "prepare-complete":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT,
            "Current capture project preparation completed")
    elif phase == "shader-prepare":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Preparing shared Shaders")
    elif phase == "shader-compile":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Compiling shared Shaders")
    elif phase == "shader-candidate-load":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT,
            "Loading a verified project Shader candidate")
    elif phase == "shader-apply":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Applying project Shaders")
    elif phase == "export-shader-bindings":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Exporting Shader bindings")
    elif phase == "material-restore":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Restoring CBuffer data")
    elif phase == "alias-restore":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT,
            "Restoring verified Shader and CBuffer aliases")
    elif phase == "index-write":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Writing the RDC project association cache")
    elif phase == "complete":
        if progress.get("preservedLocalEdits", 0):
            if progress.get("sourceOnlyDrafts", 0):
                message = _qt_format(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Project restore completed: %1 Shaders applied; %2 source drafts and local edits remain unapplied"),
                    progress.get("appliedShaders", 0),
                    progress["preservedLocalEdits"] + progress["sourceOnlyDrafts"])
            else:
                message = _qt_format(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Project restore completed: %1 Shaders applied; %2 local edits were preserved and remain unapplied"),
                    progress.get("appliedShaders", 0), progress["preservedLocalEdits"])
        elif progress.get("sourceOnlyDrafts", 0):
            message = _qt_format(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "Project restore completed: %1 Shaders applied; %2 saved source drafts remain unapplied"),
                progress.get("appliedShaders", 0), progress["sourceOnlyDrafts"])
        else:
            message = QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "Project restore completed")
    elif phase == "partial" and progress.get("item") == "local-edit":
        message = _qt_format(QtCore.QCoreApplication.translate(
            "ReplayLabProjects",
            "Project restore incomplete: %1 Shaders applied; %2 local edits were preserved but their saved replay state was not restored"),
            progress.get("appliedShaders", 0), progress.get("unappliedLocalEdits", 0))
    elif phase == "failed":
        message = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Project restore failed")
    else:
        # Unknown Host text is diagnostic data. Preserve it verbatim instead of
        # pretending that an unversioned backend sentence is a stable UI label.
        message = progress.get("message") or phase or QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Restoring project data")
    total = int(progress.get("total", 0) or 0)
    if total > 0:
        message = _qt_format(
            QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "%1 (%2 / %3)"),
            message, int(progress.get("current", 0) or 0), total)
    item = str(progress.get("item", "") or "")
    if item and not (phase == "partial" and item == "local-edit"):
        return _qt_format(
            QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "%1 · Current item: %2"),
            message, item)
    return message


def _restore_status_text(progress):
    """Return the single-line status-bar text for one restore snapshot."""

    return _qt_format(
        QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Project recovery: %1"),
        _restore_progress_text(progress))


def _capture_open_error(value):
    """Interpret explicit failure results without breaking legacy None returns."""
    if value is False:
        return QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT,
            "RenderDoc reported that the capture was not opened")
    if isinstance(value, dict):
        error_id = str(value.get("errorId", "") or "")
        error = value.get("error")
        if isinstance(error, dict):
            error = error.get("message") or error.get("detail") or str(error)
        diagnostic = str(error or "")[:4096]
        if error_id == "renderdoc-open-incomplete":
            return QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT,
                "RenderDoc did not finish opening the selected RDC")
        if error_id:
            return (_qt_format(QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "%1: %2"), error_id, diagnostic)
                if diagnostic else error_id[:4096])
        if diagnostic:
            return diagnostic
    return ""


def _restore_action_required(value):
    return bool(
        isinstance(value, dict)
        and value.get("completed") is False
        and value.get("outcome") == "action-required"
        and value.get("reasonId") == "catalog-index-required"
    )


def _restore_result_error(value, worker_error=""):
    if worker_error:
        return str(worker_error)[:4096]
    if value is False:
        return QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT,
            "Project data was not restored")
    if isinstance(value, dict):
        if _restore_action_required(value):
            return ""
        if value.get("outcome") == "action-required":
            reason_id = str(value.get("reasonId", "") or "")[:256]
            return reason_id or QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "Project restore failed")
        error_id = str(value.get("errorId", "") or "")
        error = value.get("error")
        if isinstance(error, dict):
            error = error.get("message") or error.get("detail") or str(error)
        diagnostic = str(error or "")[:4096]
        if error_id == "capture-no-longer-active":
            return QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT,
                "The selected RDC is no longer the active RenderDoc capture")
        if error_id == "capture-changed-during-restore":
            return QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT,
                "The active capture changed while project data was being restored")
        if error_id == "project-restore-client-unavailable":
            return QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT,
                "This qrenderdoc build cannot restore project data after opening an RDC")
        if error_id == "host-runtime-incompatible":
            return QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT,
                "The running project service does not match this qrenderdoc build. "
                "Close qrenderdoc and Codex, deploy the matching runtime, then restart "
                "qrenderdoc.")
        if error_id:
            # Unknown future IDs are diagnostics. Do not mistake them for a
            # successful restore merely because this UI predates the mapping;
            # retain any diagnostic supplied alongside it.
            return (_qt_format(QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "%1: %2"), error_id, diagnostic)
                if diagnostic else error_id[:4096])
        if diagnostic:
            return diagnostic
        if value.get("error"):
            return QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "Project restore failed")
    return ""


def _text(value, label, maximum=4096, allow_empty=False):
    if not isinstance(value, str):
        raise ProjectLauncherError("%s must be text" % label)
    result = value.strip()
    if ((not result and not allow_empty) or len(result) > maximum or
            "\x00" in result):
        raise ProjectLauncherError("%s has invalid content" % label)
    return result


def _normalise_capture_list(values):
    result = []
    seen = set()
    for value in list(values or []):
        path = os.path.abspath(_text(value, "capture path"))
        if not path.lower().endswith(".rdc"):
            raise ProjectLauncherError("Capture references must use the .rdc extension")
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    if len(result) > MAX_CAPTURE_ARGUMENTS:
        raise ProjectLauncherError("Too many capture files were selected")
    return result


def _project_settings_payload(project_scoped, game_id, build_id, platform,
                              api_family, asset_root):
    """Keep the internal workspace scope out of user settings updates."""
    values = {"asset_root": str(asset_root or "").strip()}
    if project_scoped:
        return values
    values.update({
        "game_id": str(game_id or "").strip(),
        "build_id": str(build_id or "").strip(),
        "platform": str(platform or "").strip(),
        "api_family": str(api_family or "").strip(),
    })
    return values


def launcher_startup_allowed(arguments, capture_loaded=False,
                             capture_loading=False, ui_available=True):
    """Pure startup gate used before showing the optional launcher window."""
    if not ui_available or capture_loaded or capture_loading:
        return False
    values = [str(value or "") for value in list(arguments or [])]
    lowered = [value.lower() for value in values]
    blocked_switches = (
        "--unittest", "--help", "-h", "--version", "-v",
        "--targetcontrol", "--remoteaccess", "--replayhost", "--tempfile",
        "--python", "--script", "--py",
        "--ui-python", "--ui-script", "--ui-py", "--crash",
        "--install_vulkan_layer", "--updatefailed", "--updatedone_admin",
        "--updatedone",
    )
    switch_names = [value.split("=", 1)[0] for value in lowered]
    if any(value in blocked_switches for value in switch_names):
        return False
    for value in values:
        if value.lower().endswith(".rdc") or (
                value and not value.startswith("-") and os.path.isfile(value)):
            return False
    return True


def _validated_content_identity(value, allow_none=True):
    if value is None and allow_none:
        return None
    if not isinstance(value, dict):
        raise ProjectLauncherError("Bread capture content identity is invalid")
    if (value.get("schema") != CAPTURE_CONTENT_IDENTITY_SCHEMA or
            value.get("algorithm") != "sha256-full"):
        raise ProjectLauncherError("Bread capture content identity is invalid")
    digest = str(value.get("sha256", "") or "").lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ProjectLauncherError("Bread capture content digest is invalid")
    try:
        size = int(value.get("sizeBytes"))
        modified = int(value.get("modifiedTimeNs"))
    except (TypeError, ValueError, OverflowError):
        raise ProjectLauncherError("Bread capture content metadata is invalid")
    if size < 0 or modified < 0:
        raise ProjectLauncherError("Bread capture content metadata is invalid")
    return {
        "schema": CAPTURE_CONTENT_IDENTITY_SCHEMA,
        "algorithm": "sha256-full",
        "sha256": digest,
        "sizeBytes": size,
        "modifiedTimeNs": modified,
        "fileIdentity": _text(
            value.get("fileIdentity", ""), "capture file identity", 128),
    }


def unique_project_for_capture(projects, capture_path, content_identity=None):
    """Return one unambiguous registered-path match.

    ``content_identity`` remains accepted for bridge compatibility but is no
    longer computed or used for project association.
    """
    path = os.path.abspath(_text(capture_path, "capture path"))
    key = os.path.normcase(path)
    del content_identity
    matches = []
    seen = set()
    for project in list(projects or []):
        if not isinstance(project, dict):
            continue
        workspace_id = str(project.get("workspaceId", "") or "")
        if len(workspace_id) != 32:
            continue
        if workspace_id in seen:
            continue
        captures = project.get("captures")
        if not isinstance(captures, list):
            continue
        if any(
                os.path.normcase(os.path.abspath(str(record.get("path", "") or ""))) == key
                for record in captures if isinstance(record, dict)):
            seen.add(workspace_id)
            matches.append(project)
    return matches[0] if len(matches) == 1 else None


def replace_project_snapshot(projects, updated_project):
    """Replace one project result in-place logically, preserving UI-local active state."""
    if not isinstance(updated_project, dict):
        raise ProjectLauncherError("Bread updated project is invalid")
    workspace_id = str(updated_project.get("workspaceId", "") or "")
    if not workspace_id:
        raise ProjectLauncherError("Bread updated project identity is missing")
    result = list(projects or [])
    for row, current in enumerate(result):
        if (isinstance(current, dict) and
                str(current.get("workspaceId", "") or "") == workspace_id):
            replacement = dict(updated_project)
            replacement["active"] = bool(current.get("active", False))
            result[row] = replacement
            return result, row
    raise ProjectLauncherError(
        "Bread updated project is not present in the current project list")


def _thumbnail_result(state, reason="", data=b"", width=0, height=0,
                      reason_id="", reason_detail=""):
    return {
        "state": state,
        "reason": str(reason or "")[:1024],
        "reasonId": str(reason_id or "")[:128],
        "reasonDetail": str(reason_detail or "")[:1024],
        "data": bytes(data),
        "width": int(width),
        "height": int(height),
    }


def _thumbnail_reason_text(result):
    """Localize stable thumbnail outcomes while retaining unknown diagnostics."""
    reason_id = str(result.get("reasonId", "") or "")
    detail = str(result.get("reasonDetail", "") or "")
    legacy_reason = str(result.get("reason", "") or "")
    diagnostic = detail or legacy_reason
    if reason_id == "rdc-unavailable":
        return QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "RDC file is unavailable")
    if reason_id == "open-metadata-failed":
        return _qt_format(QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT,
            "RenderDoc could not open RDC metadata: %1"), diagnostic)
    if reason_id == "no-embedded-thumbnail":
        return QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "The RDC has no embedded thumbnail")
    if reason_id == "preview-limits-exceeded":
        return QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT,
            "Embedded thumbnail exceeds the bounded preview limits")
    if reason_id == "read-failed":
        return _qt_format(QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Embedded thumbnail read failed: %1"), diagnostic)
    if reason_id == "reader-invalid-data":
        return QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Thumbnail reader returned invalid data")
    if reason_id == "reader-invalid-state":
        return QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Thumbnail reader returned invalid state")
    if reason_id == "invalid-bytes":
        return QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Thumbnail bytes are invalid")
    if reason_id == "invalid-dimensions":
        return QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Thumbnail dimensions are invalid")
    if reason_id == "unsafe-payload":
        return QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Thumbnail payload exceeds safe limits")
    if reason_id == "rdc-changed":
        return QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT,
            "RDC file changed while its thumbnail was read")
    if reason_id:
        return (_qt_format(QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "%1: %2"), reason_id, diagnostic)
            if diagnostic else reason_id[:1024])
    # Third-party/unknown reader failures are diagnostics, not stable labels.
    return diagnostic or \
        QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Embedded RDC thumbnail is unavailable")


def _load_projects_with_access_repair(client, attempted):
    """Repair registered project ACL failures once, off the UI thread."""
    projects = client.list_projects()
    repaired = False
    if os.name != "nt":
        return projects
    for project in projects:
        if (not project.get("unavailable") or
                project.get("unavailableReasonId") != "project-access-denied"):
            continue
        key = (project["workspaceId"], os.path.normcase(os.path.abspath(project["projectRoot"])))
        if key in attempted:
            continue
        attempted.add(key)
        try:
            result = _project_permissions.repair(project["projectRoot"], project["workspaceId"], automatic=True)
            repaired = result.get("status") == "repaired" or repaired
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            project["automaticAccessRepairError"] = str(error)[:1024]
    return client.list_projects() if repaired else projects


def _unavailable_project_reason_text(project):
    """Localize a stable registry failure while preserving its diagnostic."""
    reason_id = str(project.get("unavailableReasonId", "") or "")
    detail = str(project.get("unavailableReasonDetail", "") or "") or \
        str(project.get("unavailableReason", "") or "")
    if reason_id == "project-access-denied":
        label = QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Project access was denied. Check folder permissions and file access by other programs.")
        return _qt_format(QtCore.QCoreApplication.translate("ReplayLabProjects", "%1: %2"), label, detail)
    if reason_id == "project-files-busy":
        label = QtCore.QCoreApplication.translate(
            "ReplayLabProjects", "Project files are in use. Retry after the other operation finishes.")
        return _qt_format(QtCore.QCoreApplication.translate("ReplayLabProjects", "%1: %2"), label, detail)
    if reason_id == "project-validation-failed":
        label = QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "Project files are unavailable or invalid")
        return (_qt_format(QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "%1: %2"), label, detail)
            if detail else label)
    if reason_id:
        return (_qt_format(QtCore.QCoreApplication.translate(
            TRANSLATION_CONTEXT, "%1: %2"), reason_id, detail)
            if detail else reason_id[:1024])
    return detail or QtCore.QCoreApplication.translate(
        TRANSLATION_CONTEXT, "Project files are unavailable or invalid")


class RenderDocThumbnailReader(object):
    """Read only the embedded RDC thumbnail on the constructing thread.

    ``ICaptureFile.OpenFile`` parses the container header/section table and the
    optional ExtendedThumbnail section. It is intentionally distinct from
    ``ICaptureFile.OpenCapture``, which is the operation that creates a replay
    controller/device. The latter is never called here.
    """

    def __init__(self, rd_module, max_edge=MAX_THUMBNAIL_EDGE):
        self.rd = rd_module
        self.max_edge = int(max_edge)
        if self.max_edge <= 0 or self.max_edge > 2048:
            raise ThumbnailReadError("thumbnail max edge is outside the fixed safe range")
        self.owner_thread_id = threading.current_thread().ident

    def __call__(self, path):
        if threading.current_thread().ident != self.owner_thread_id:
            raise ThumbnailReadError(
                "RenderDoc thumbnail metadata must be read on its owning UI thread")
        path = os.path.abspath(_text(path, "thumbnail capture path"))
        if (not path.lower().endswith(".rdc") or os.path.islink(path) or
                not os.path.isfile(path)):
            return _thumbnail_result(
                "missing", "RDC file is unavailable",
                reason_id="rdc-unavailable")

        capture = None
        try:
            capture = self.rd.OpenCaptureFile()
            if capture is None:
                raise ThumbnailReadError("RenderDoc could not allocate a capture-file handle")
            result = capture.OpenFile(path, "", None)
            if result != self.rd.ResultCode.Succeeded:
                return _thumbnail_result(
                    "error", "RenderDoc could not open RDC metadata: %s" % str(result),
                    reason_id="open-metadata-failed", reason_detail=str(result))
            thumbnail = capture.GetThumbnail(self.rd.FileType.PNG, self.max_edge)
            data = bytes(getattr(thumbnail, "data", b"") or b"")
            width = int(getattr(thumbnail, "width", 0) or 0)
            height = int(getattr(thumbnail, "height", 0) or 0)
            if not data or width <= 0 or height <= 0:
                return _thumbnail_result(
                    "no-thumbnail", "The RDC has no embedded thumbnail",
                    reason_id="no-embedded-thumbnail")
            if (len(data) > MAX_THUMBNAIL_BYTES or width > self.max_edge or
                    height > self.max_edge):
                return _thumbnail_result(
                    "error", "Embedded thumbnail exceeds the bounded preview limits",
                    reason_id="preview-limits-exceeded")
            return _thumbnail_result("ready", data=data, width=width, height=height)
        except ThumbnailReadError:
            raise
        except Exception as exc:
            return _thumbnail_result(
                "error", "Embedded thumbnail read failed: %s" % str(exc),
                reason_id="read-failed", reason_detail=str(exc))
        finally:
            if capture is not None:
                try:
                    capture.Shutdown()
                except Exception:
                    pass


class ThumbnailCache(object):
    """Bounded positive/negative LRU keyed by exact file metadata."""

    def __init__(self, maximum_entries=MAX_THUMBNAIL_CACHE_ENTRIES,
                 maximum_bytes=MAX_THUMBNAIL_CACHE_BYTES):
        self.maximum_entries = int(maximum_entries)
        self.maximum_bytes = int(maximum_bytes)
        if self.maximum_entries <= 0 or self.maximum_entries > 4096:
            raise ThumbnailReadError("thumbnail cache entry limit is invalid")
        if self.maximum_bytes <= 0 or self.maximum_bytes > 512 * 1024 * 1024:
            raise ThumbnailReadError("thumbnail cache byte limit is invalid")
        self._entries = OrderedDict()
        self._total_bytes = 0

    @staticmethod
    def _file_key(path):
        absolute = os.path.abspath(_text(path, "thumbnail capture path"))
        if (not absolute.lower().endswith(".rdc") or os.path.islink(absolute) or
                not os.path.isfile(absolute)):
            return absolute, None
        try:
            status = os.stat(absolute)
        except OSError:
            return absolute, None
        return absolute, (
            os.path.normcase(absolute), int(status.st_size),
            int(getattr(status, "st_mtime_ns", int(status.st_mtime * 1000000000))),
        )

    @staticmethod
    def _validate_loaded(value):
        if not isinstance(value, dict):
            return _thumbnail_result(
                "error", "Thumbnail reader returned invalid data",
                reason_id="reader-invalid-data")
        state = value.get("state")
        if state not in ("ready", "missing", "no-thumbnail", "error", "stale"):
            return _thumbnail_result(
                "error", "Thumbnail reader returned invalid state",
                reason_id="reader-invalid-state")
        data = value.get("data", b"")
        try:
            data = bytes(data)
        except Exception:
            return _thumbnail_result(
                "error", "Thumbnail bytes are invalid",
                reason_id="invalid-bytes")
        width = value.get("width", 0)
        height = value.get("height", 0)
        if isinstance(width, bool) or isinstance(height, bool):
            return _thumbnail_result(
                "error", "Thumbnail dimensions are invalid",
                reason_id="invalid-dimensions")
        try:
            width = int(width)
            height = int(height)
        except Exception:
            return _thumbnail_result(
                "error", "Thumbnail dimensions are invalid",
                reason_id="invalid-dimensions")
        if state == "ready":
            if (not data or len(data) > MAX_THUMBNAIL_BYTES or width <= 0 or
                    height <= 0 or width > 2048 or height > 2048):
                return _thumbnail_result(
                    "error", "Thumbnail payload exceeds safe limits",
                    reason_id="unsafe-payload")
        else:
            data = b""
            width = 0
            height = 0
        return _thumbnail_result(
            state, value.get("reason", ""), data, width, height,
            value.get("reasonId", ""), value.get("reasonDetail", ""))

    def _remove(self, key):
        value = self._entries.pop(key, None)
        if value is not None:
            self._total_bytes -= len(value.get("data", b""))

    def clear(self):
        self._entries.clear()
        self._total_bytes = 0

    def get_or_load(self, path, loader):
        absolute, key = self._file_key(path)
        if key is None:
            result = _thumbnail_result(
                "missing", "RDC file is unavailable",
                reason_id="rdc-unavailable")
            result["cacheHit"] = False
            return result
        # Invalidate any cached revision of this physical path before lookup.
        for existing in list(self._entries.keys()):
            if existing[0] == key[0] and existing != key:
                self._remove(existing)
        if key in self._entries:
            result = self._entries.pop(key)
            self._entries[key] = result
            returned = dict(result)
            returned["cacheHit"] = True
            return returned

        try:
            loaded = loader(absolute)
        except Exception as exc:
            loaded = _thumbnail_result(
                "error", str(exc), reason_id="read-failed",
                reason_detail=str(exc))
        result = self._validate_loaded(loaded)
        unused_absolute, after_key = self._file_key(absolute)
        if after_key != key:
            result = _thumbnail_result(
                "stale", "RDC file changed while its thumbnail was read",
                reason_id="rdc-changed")
        self._entries[key] = result
        self._total_bytes += len(result.get("data", b""))
        while (len(self._entries) > self.maximum_entries or
               self._total_bytes > self.maximum_bytes):
            oldest_key = next(iter(self._entries))
            self._remove(oldest_key)
        returned = dict(result)
        returned["cacheHit"] = False
        return returned


class AsyncCallbackRegistry(object):
    """UI-thread registry that makes late worker results harmless after close."""

    def __init__(self):
        self._callbacks = {}

    def register(self, owner_token, callback, token=None):
        owner = _text(owner_token, "asynchronous owner", 128)
        result_token = token or uuid.uuid4().hex
        self._callbacks[result_token] = (owner, callback)
        return result_token

    def take(self, token):
        return self._callbacks.pop(str(token), None)

    def cancel_owner(self, owner_token):
        owner = str(owner_token or "")
        for token, value in list(self._callbacks.items()):
            if value[0] == owner:
                self._callbacks.pop(token, None)

    def clear(self):
        self._callbacks.clear()

    def __len__(self):
        return len(self._callbacks)


def validate_project(value):
    if not isinstance(value, dict) or value.get("schema") != PROJECT_WORKSPACE_SCHEMA:
        raise ProjectLauncherError("Bread returned an invalid project document")
    workspace_id = _text(value.get("workspaceId", ""), "workspace ID", 32)
    if len(workspace_id) != 32 or any(c not in "0123456789abcdef" for c in workspace_id):
        raise ProjectLauncherError("Bread project workspace ID is invalid")
    root = os.path.abspath(_text(value.get("projectRoot", ""), "project root"))
    name = _text(value.get("name", ""), "project name", 256)
    state = value.get("state")
    if state not in ("pending-scope", "bound"):
        raise ProjectLauncherError("Bread project state is invalid")
    scope = value.get("scope")
    if not isinstance(scope, dict):
        raise ProjectLauncherError("Bread project scope is missing")
    capture_library = value.get("captureLibrary")
    if (not isinstance(capture_library, dict) or
            capture_library.get("schema") != CAPTURE_LIBRARY_SCHEMA or
            capture_library.get("schemaVersion") not in (1, 2) or
            capture_library.get("workspaceId") != workspace_id):
        raise ProjectLauncherError("Bread project capture library is invalid")
    captures = capture_library.get("captures")
    if not isinstance(captures, list) or len(captures) > MAX_CAPTURE_ARGUMENTS:
        raise ProjectLauncherError("Bread capture list is invalid")
    clean_captures = []
    for record in captures:
        if not isinstance(record, dict):
            raise ProjectLauncherError("Bread capture reference is invalid")
        path = os.path.abspath(_text(record.get("path", ""), "capture path"))
        if not path.lower().endswith(".rdc"):
            raise ProjectLauncherError("Bread capture reference is not an .rdc path")
        clean_captures.append({
            "captureRefId": _text(record.get("captureRefId", ""), "capture reference ID", 64),
            "path": path,
            "displayName": _text(record.get("displayName", ""), "capture display name", 512),
            "available": bool(record.get("available", False)),
            "sizeBytes": record.get("sizeBytes"),
            "modifiedTimeNs": record.get("modifiedTimeNs"),
            "thumbnail": dict(record.get("thumbnail") or {}),
            "contentIdentity": _validated_content_identity(
                record.get("contentIdentity")),
        })
    return {
        "workspaceId": workspace_id,
        "projectRoot": root,
        "name": name,
        "state": state,
        "scope": dict(scope),
        "assetRoot": value.get("assetRoot"),
        "active": bool(value.get("active", False)),
        "unavailable": False,
        "captures": clean_captures,
    }


def validate_unavailable_project(value):
    """Validate a stale registry record that the UI may safely unregister."""
    if not isinstance(value, dict):
        raise ProjectLauncherError("Bread unavailable project record is invalid")
    workspace_id = _text(value.get("workspaceId", ""), "workspace ID", 32)
    if len(workspace_id) != 32 or any(
            c not in "0123456789abcdef" for c in workspace_id):
        raise ProjectLauncherError("Bread unavailable workspace ID is invalid")
    legacy_reason = _text(
        value.get("reason", "") or "", "unavailable project reason", 1024, True)
    reason_detail = _text(
        value.get("reasonDetail", "") or legacy_reason,
        "unavailable project reason detail", 1024, True)
    reason_id = _text(
        value.get("reasonId", "") or "",
        "unavailable project reason ID", 128, True)
    return {
        "workspaceId": workspace_id,
        "projectRoot": os.path.abspath(
            _text(value.get("projectRoot", ""), "project root")),
        "name": _text(value.get("name", ""), "project name", 256),
        "state": "unavailable",
        "scope": {},
        "assetRoot": None,
        "active": False,
        "unavailable": True,
        # Keep the legacy key so older callers and diagnostics remain intact.
        "unavailableReason": legacy_reason or reason_detail,
        "unavailableReasonId": reason_id,
        "unavailableReasonDetail": reason_detail,
        "captures": [],
    }


def _internal_project_operation(arguments):
    """Convert the in-module call shape to one typed service request.

    The argument list is never executed. Keeping this adapter avoids a broad
    UI rewrite while deleting the per-operation process boundary. Unknown,
    duplicate, or malformed switches fail before any HTTP request.
    """

    values = list(arguments or [])
    if not values or not isinstance(values[0], str):
        raise ProjectLauncherError("Bread project operation is invalid")
    command = values.pop(0)
    definitions = {
        "project-list": ({}, set()),
        "project-create": ({
            "--name": "name", "--project-root": "projectRoot",
            "--asset-root": "assetRoot", "--game-id": "gameId",
            "--build-id": "buildId", "--platform": "platform",
            "--api-family": "apiFamily", "--capture": "captureFiles",
        }, {"--capture"}),
        "project-register": ({"--project-root": "projectRoot"}, set()),
        "project-unregister": ({"--project-root": "projectRoot"}, set()),
        "project-activate": ({"--project-root": "projectRoot"}, set()),
        "project-configure": ({
            "--project-root": "projectRoot", "--game-id": "gameId",
            "--build-id": "buildId", "--platform": "platform",
            "--api-family": "apiFamily", "--asset-root": "assetRoot",
            "--clear-asset-root": "clearAssetRoot",
        }, set()),
        "project-add-capture": ({
            "--project-root": "projectRoot", "--capture": "captureFiles",
        }, {"--capture"}),
        "project-remove-capture": ({
            "--project-root": "projectRoot",
            "--capture-ref-id": "captureRefIds",
        }, {"--capture-ref-id"}),
        "project-match-capture": ({
            "--capture": "capturePath",
            "--association-lease": "associationLease",
            "--association-token": "associationToken",
        }, set()),
    }
    selected = definitions.get(command)
    if selected is None:
        raise ProjectLauncherError("Bread project operation is not allowed")
    switches, repeated = selected
    parsed = {}
    index = 0
    while index < len(values):
        switch = values[index]
        if switch not in switches:
            raise ProjectLauncherError(
                "Bread project operation contains an invalid option")
        field = switches[switch]
        if switch == "--clear-asset-root":
            if field in parsed:
                raise ProjectLauncherError(
                    "Bread project operation contains a duplicate option")
            parsed[field] = True
            index += 1
            continue
        if index + 1 >= len(values):
            raise ProjectLauncherError(
                "Bread project operation option has no value")
        item = values[index + 1]
        if switch in repeated:
            parsed.setdefault(field, []).append(item)
        elif field in parsed:
            raise ProjectLauncherError(
                "Bread project operation contains a duplicate option")
        else:
            parsed[field] = item
        index += 2

    if command == "project-list":
        if parsed:
            raise ProjectLauncherError("Bread project list has invalid options")
        return "list-projects", {}
    if command == "project-create":
        if not {"name", "projectRoot"}.issubset(parsed):
            raise ProjectLauncherError("Bread project creation is incomplete")
        return "create-project", {
            "name": parsed["name"],
            "projectRoot": parsed["projectRoot"],
            "assetRoot": parsed.get("assetRoot", ""),
            "captureFiles": parsed.get("captureFiles", []),
            "gameId": parsed.get("gameId", ""),
            "buildId": parsed.get("buildId", ""),
            "platform": parsed.get("platform", ""),
            "apiFamily": parsed.get("apiFamily", ""),
        }
    if command in (
            "project-register", "project-unregister", "project-activate"):
        if set(parsed) != {"projectRoot"}:
            raise ProjectLauncherError("Bread project path is incomplete")
        operation = {
            "project-register": "register-project",
            "project-unregister": "unregister-project",
            "project-activate": "activate-project",
        }[command]
        return operation, {"projectRoot": parsed["projectRoot"]}
    if command == "project-configure":
        if "projectRoot" not in parsed:
            raise ProjectLauncherError("Bread project configuration is incomplete")
        if parsed.get("clearAssetRoot") and "assetRoot" in parsed:
            raise ProjectLauncherError(
                "Bread project configuration has conflicting asset roots")
        mode = "unchanged"
        asset_root = ""
        if parsed.get("clearAssetRoot"):
            mode = "clear"
        elif "assetRoot" in parsed:
            mode = "set"
            asset_root = parsed["assetRoot"]
        return "configure-project", {
            "projectRoot": parsed["projectRoot"],
            "gameId": parsed.get("gameId", ""),
            "buildId": parsed.get("buildId", ""),
            "platform": parsed.get("platform", ""),
            "apiFamily": parsed.get("apiFamily", ""),
            "assetRootMode": mode,
            "assetRoot": asset_root,
        }
    if command == "project-add-capture":
        if "projectRoot" not in parsed or not parsed.get("captureFiles"):
            raise ProjectLauncherError("Bread capture addition is incomplete")
        return "add-captures", {
            "projectRoot": parsed["projectRoot"],
            "captureFiles": parsed["captureFiles"],
        }
    if command == "project-remove-capture":
        if "projectRoot" not in parsed or not parsed.get("captureRefIds"):
            raise ProjectLauncherError("Bread capture removal is incomplete")
        return "remove-captures", {
            "projectRoot": parsed["projectRoot"],
            "captureRefIds": parsed["captureRefIds"],
        }
    if command == "project-match-capture":
        if "capturePath" not in parsed:
            raise ProjectLauncherError("Bread capture match is incomplete")
        return "match-capture", {
            "capturePath": parsed["capturePath"],
            "associationLease": parsed.get("associationLease", ""),
            "associationToken": parsed.get("associationToken", ""),
        }
    raise AssertionError("unreachable project operation")


class ReplayLabProjectClient(object):
    """Typed client for the shared private ProjectService."""

    def __init__(self, executable, timeout_seconds=90, runner=None):
        self.executable = os.path.abspath(_text(
            executable, "Bread internal host executable"))
        self.timeout_seconds = int(timeout_seconds)
        self._internal_client = None
        if runner is None:
            internal_executable = (
                _internal_client_protocol.resolve_internal_executable(
                    self.executable))
            if not internal_executable:
                raise ProjectLauncherError(
                    "Packaged Bread internal host (ReplayLabInternal.exe) is unavailable: %s" %
                    self.executable)
            self._internal_client = (
                _internal_client_protocol.ReplayLabInternalProjectClient(
                    internal_executable,
                    request_timeout_seconds=max(1, self.timeout_seconds)))
            self.runner = self._run_internal_request
        else:
            self.runner = runner

    def _run_internal_request(self, arguments):
        operation, parameters = _internal_project_operation(arguments)
        try:
            return self._internal_client.invoke(operation, parameters)
        except _internal_client_protocol.ReplayLabRestoreClientError as exc:
            raise ProjectLauncherError(str(exc))

    def _invoke(self, arguments):
        return self.runner(list(arguments))

    def list_projects(self):
        value = self._invoke(["project-list"])
        if value.get("schema") != PROJECT_LIST_SCHEMA:
            raise ProjectLauncherError("Bread returned an invalid project list")
        projects = value.get("projects")
        if not isinstance(projects, list):
            raise ProjectLauncherError("Bread project list is malformed")
        rejected = value.get("rejected", [])
        if not isinstance(rejected, list):
            raise ProjectLauncherError("Bread unavailable project list is malformed")
        if len(projects) + len(rejected) > MAX_CAPTURE_ARGUMENTS:
            raise ProjectLauncherError("Bread project list is oversized")
        return (
            [validate_project(project) for project in projects] +
            [validate_unavailable_project(project) for project in rejected]
        )

    def create_project(self, name, project_root, asset_root="", capture_files=None,
                       game_id="", build_id="", platform="", api_family=""):
        game_id = _text(game_id, "game ID", 256, True)
        build_id = _text(build_id, "build ID", 256, True)
        if bool(game_id) != bool(build_id):
            raise ProjectLauncherError(
                "Game ID and Build ID must either both be supplied or both be empty")
        arguments = [
            "project-create",
            "--name", _text(name, "project name", 256),
            "--project-root", os.path.abspath(
                _text(project_root, "project root")),
        ]
        if game_id:
            arguments.extend(["--game-id", game_id, "--build-id", build_id])
        if platform:
            arguments.extend(["--platform", _text(platform, "platform", 128)])
        if api_family:
            arguments.extend(["--api-family", _text(api_family, "API family", 64)])
        if asset_root:
            arguments.extend(["--asset-root", os.path.abspath(
                _text(asset_root, "asset root"))])
        for capture in _normalise_capture_list(capture_files):
            arguments.extend(["--capture", capture])
        return validate_project(self._invoke(arguments))

    def register_existing_project(self, project_root):
        return validate_project(self._invoke([
            "project-register", "--project-root",
            os.path.abspath(_text(project_root, "project root")),
        ]))

    def unregister_project(self, project_root):
        value = self._invoke([
            "project-unregister", "--project-root",
            os.path.abspath(_text(project_root, "project root")),
        ])
        if (not isinstance(value, dict) or
                value.get("schema") != "replaylab-project-unregister/v1" or
                value.get("removed") is not True or
                value.get("projectFilesDeleted") is not False):
            raise ProjectLauncherError(
                "Bread returned an invalid project removal result")
        return dict(value)

    def activate(self, project_root):
        return validate_project(self._invoke([
            "project-activate", "--project-root",
            os.path.abspath(_text(project_root, "project root")),
        ]))

    def configure_project(self, project_root, game_id="", build_id="",
                          platform="", api_family="", asset_root=None):
        game_id = _text(game_id, "game ID", 256, True)
        build_id = _text(build_id, "build ID", 256, True)
        if bool(game_id) != bool(build_id):
            raise ProjectLauncherError(
                "Game ID and Build ID must either both be supplied or both be empty")
        arguments = [
            "project-configure", "--project-root",
            os.path.abspath(_text(project_root, "project root")),
        ]
        if game_id:
            arguments.extend(["--game-id", game_id, "--build-id", build_id])
            if platform:
                arguments.extend(["--platform", _text(platform, "platform", 128)])
            if api_family:
                arguments.extend(["--api-family", _text(api_family, "API family", 64)])
        if asset_root is not None:
            asset_root = _text(asset_root, "asset root", 4096, True)
            if asset_root:
                arguments.extend(["--asset-root", os.path.abspath(asset_root)])
            else:
                arguments.append("--clear-asset-root")
        return validate_project(self._invoke(arguments))

    def add_captures(self, project_root, capture_files):
        captures = _normalise_capture_list(capture_files)
        if not captures:
            raise ProjectLauncherError("Select at least one RDC file")
        arguments = [
            "project-add-capture", "--project-root",
            os.path.abspath(_text(project_root, "project root")),
        ]
        for capture in captures:
            arguments.extend(["--capture", capture])
        return validate_project(self._invoke(arguments))

    def remove_captures(self, project_root, capture_ref_ids):
        references = []
        seen = set()
        for value in list(capture_ref_ids or []):
            capture_ref_id = str(value or "").strip().lower()
            if (len(capture_ref_id) != 64 or
                    any(c not in "0123456789abcdef" for c in capture_ref_id)):
                raise ProjectLauncherError("Bread capture reference ID is invalid")
            if capture_ref_id not in seen:
                seen.add(capture_ref_id)
                references.append(capture_ref_id)
        if not references:
            raise ProjectLauncherError("Select at least one RDC reference")
        arguments = [
            "project-remove-capture", "--project-root",
            os.path.abspath(_text(project_root, "project root")),
        ]
        for capture_ref_id in references:
            arguments.extend(["--capture-ref-id", capture_ref_id])
        return validate_project(self._invoke(arguments))

    def match_and_activate_capture(self, capture_path, association_lease="",
                                   association_token=""):
        capture = _normalise_capture_list([capture_path])
        if len(capture) != 1:
            raise ProjectLauncherError("Select exactly one RDC file")
        arguments = [
            "project-match-capture", "--capture", capture[0],
        ]
        if bool(association_lease) != bool(association_token):
            raise ProjectLauncherError(
                "Association lease path and token must both be supplied")
        if association_lease:
            arguments.extend([
                "--association-lease", os.path.abspath(_text(
                    association_lease, "association lease path")),
                "--association-token", _text(
                    association_token, "association lease token", 64),
            ])
        value = self._invoke(arguments)
        if (not isinstance(value, dict) or
                value.get("schema") != CAPTURE_PROJECT_MATCH_SCHEMA or
                not isinstance(value.get("matches"), list)):
            raise ProjectLauncherError(
                "Bread returned an invalid capture/project match")
        try:
            match_count = int(value.get("matchCount"))
        except (TypeError, ValueError, OverflowError):
            raise ProjectLauncherError(
                "Bread returned an invalid capture/project match count")
        if match_count != len(value["matches"]):
            raise ProjectLauncherError(
                "Bread capture/project match count is inconsistent")
        activated = bool(value.get("activated", False))
        project = value.get("project")
        if activated and not (match_count == 1 and isinstance(project, dict)):
            raise ProjectLauncherError(
                "Bread capture/project activation result is inconsistent")
        if project is not None:
            workspace_id = str(project.get("workspaceId", "") or "")
            if (len(workspace_id) != 32 or
                    any(c not in "0123456789abcdef" for c in workspace_id)):
                raise ProjectLauncherError(
                    "Bread matched project identity is invalid")
            project = dict(project)
            project["projectRoot"] = os.path.abspath(
                _text(project.get("projectRoot", ""), "project root"))
            project["name"] = _text(project.get("name", ""), "project name", 256)
        result = dict(value)
        result["project"] = project
        return result


try:
    from PySide2 import QtCore, QtGui, QtWidgets
    HAS_QT = True
except ImportError:
    class _TranslationFallback(object):
        @staticmethod
        def translate(unused_context, source):
            del unused_context
            return str(source)

    class _QtCoreFallback(object):
        QCoreApplication = _TranslationFallback

    QtCore = _QtCoreFallback()
    HAS_QT = False


if HAS_QT:
    class _ProjectOperation(QtCore.QRunnable):
        def __init__(self, executor, token, operation):
            QtCore.QRunnable.__init__(self)
            self.executor = executor
            self.token = token
            self.operation = operation
            self.setAutoDelete(True)

        def run(self):
            value = None
            error = ""
            try:
                value = self.operation()
            except Exception as exc:
                error = "%s: %s" % (type(exc).__name__, str(exc))
            # Do not emit a PySide signal carrying arbitrary Python objects from
            # a QThreadPool worker. qrenderdoc embeds an older PySide2 build
            # where that cross-thread marshal can outlive a wrapper and crash in
            # pyside2.dll. A plain Python Queue owns the result until a UI-thread
            # QTimer drains it below.
            self.executor.publish_result(self.token, value, error[:4096])


    class AsyncProjectExecutor(QtCore.QObject):
        """Bounded process worker whose callbacks always run on the UI thread."""

        def __init__(self, maximum_threads=2):
            QtCore.QObject.__init__(self)
            self.pool = QtCore.QThreadPool()
            self.pool.setMaxThreadCount(max(1, min(int(maximum_threads), 4)))
            self.registry = AsyncCallbackRegistry()
            self._results = queue.Queue()
            self._shutdown = False
            self._result_timer = QtCore.QTimer(self)
            self._result_timer.setInterval(25)
            self._result_timer.timeout.connect(self._drain_results)

        def publish_result(self, token, value, error):
            """Publish only to Python-owned storage from a worker thread."""
            if self._shutdown:
                return
            try:
                self._results.put_nowait((str(token), value, str(error or "")))
            except Exception:
                # qrenderdoc may be tearing down its embedded interpreter. The
                # operation has no authority until its UI-thread callback runs.
                pass

        def submit(self, owner_token, operation, callback):
            if self._shutdown:
                raise ProjectLauncherError(
                    "Bread project executor has been shut down")
            owner = _text(owner_token, "asynchronous owner", 128)
            token = self.registry.register(owner, callback)
            if not self._result_timer.isActive():
                self._result_timer.start()
            self.pool.start(_ProjectOperation(self, token, operation))
            return token

        def cancel_owner(self, owner_token):
            self.registry.cancel_owner(owner_token)
            if len(self.registry) == 0:
                self._result_timer.stop()

        def cancel_all(self):
            self.registry.clear()
            self._result_timer.stop()
            try:
                self.pool.clear()
            except Exception:
                pass

        def shutdown(self, timeout_ms=5000):
            """Synchronously quiesce every Qt/Python callback before Py_Finalize.

            Project operations are bounded requests to the shared internal
            service. The generation fence drops UI callbacks immediately;
            QThreadPool's bounded wait gives each Python QRunnable a finite
            chance to finish and release its reference to this QObject.
            """
            if not self._shutdown:
                self._shutdown = True
                self.registry.clear()
                try:
                    self._result_timer.stop()
                    self._result_timer.timeout.disconnect(self._drain_results)
                except Exception:
                    pass
                try:
                    self.pool.clear()
                except Exception:
                    pass
            try:
                finished = self.pool.waitForDone(max(1, int(timeout_ms)))
            except TypeError:
                # Fail closed on bindings that do not expose the bounded overload.
                raise ProjectLauncherError(
                    "The Qt thread pool does not support bounded shutdown")
            if finished is False or self.pool.activeThreadCount() > 0:
                raise ProjectLauncherError(
                    "Bread project workers did not stop within %d ms" %
                    max(1, int(timeout_ms)))
            while True:
                try:
                    self._results.get_nowait()
                except queue.Empty:
                    break
            return True

        @QtCore.Slot()
        def _drain_results(self):
            # Bound each timer turn so a burst of completed file operations
            # cannot monopolise qrenderdoc's UI event loop.
            if self._shutdown:
                return
            for unused_index in range(32):
                del unused_index
                try:
                    token, value, error = self._results.get_nowait()
                except queue.Empty:
                    if len(self.registry) == 0:
                        self._result_timer.stop()
                    return
                self._deliver(token, value, error)
            if len(self.registry) == 0:
                self._result_timer.stop()

        def _deliver(self, token, value, error):
            record = self.registry.take(token)
            if record is None:
                return
            callback = record[1]
            try:
                callback(value, str(error or ""))
            except Exception:
                # UI callbacks own their own user-facing diagnostics. Never let
                # a late result cross qrenderdoc's event-loop boundary.
                pass


    class ReplayLabRestoreStatusTask(QtCore.QObject):
        """Run post-load restore while publishing progress in the main status bar."""

        def __init__(self, executor, owner_token, restore_capture,
                     project_root, capture_path, completed, status_bar=None,
                     parent=None):
            QtCore.QObject.__init__(self, parent)
            self.executor = executor
            self.owner_token = owner_token
            self.restore_capture = restore_capture
            self.project_root = project_root
            self.capture_path = capture_path
            self.completed = completed
            self.status_bar = status_bar
            self.status_widget = None
            self.status_label = None
            self.progress_bar = None
            self._progress_events = queue.Queue(MAX_RESTORE_PROGRESS_EVENTS)
            self._started = False
            self._finished = False

            self._install_status_widget()

            self._progress_timer = QtCore.QTimer(self)
            self._progress_timer.setInterval(25)
            self._progress_timer.timeout.connect(self._drain_progress)
            self._progress_timer.start()

        def _install_status_widget(self):
            add_widget = getattr(self.status_bar, "addWidget", None)
            if not callable(add_widget):
                return
            try:
                widget = QtWidgets.QWidget(self.status_bar)
                widget.setObjectName("replayLabRestoreStatusWidget")
                layout = QtWidgets.QHBoxLayout(widget)
                layout.setContentsMargins(6, 0, 6, 0)
                layout.setSpacing(8)

                label = QtWidgets.QLabel(widget)
                label.setObjectName("replayLabRestoreStatusText")
                label.setTextFormat(QtCore.Qt.PlainText)
                label.setMinimumWidth(0)
                label.setSizePolicy(
                    QtWidgets.QSizePolicy.Ignored,
                    QtWidgets.QSizePolicy.Preferred)

                progress = QtWidgets.QProgressBar(widget)
                progress.setObjectName("replayLabRestoreStatusProgress")
                progress.setRange(0, 0)
                progress.setTextVisible(False)
                progress.setMinimumWidth(120)
                progress.setMaximumWidth(180)
                # Bar first, then the words.
                layout.addWidget(progress)
                layout.addWidget(label, 1)

                # A normal status-bar widget is deliberately used instead of
                # showMessage(). Qt temporarily hides normal widgets for menu
                # or other foreground messages, so user activity stays higher
                # priority and the background progress returns afterwards.
                add_widget(widget, 1)
                self.status_widget = widget
                self.status_label = label
                self.progress_bar = progress
            except Exception:
                self.status_widget = None
                self.status_label = None
                self.progress_bar = None

        def _remove_status_widget(self):
            widget = self.status_widget
            self.status_widget = None
            self.status_label = None
            self.progress_bar = None
            if widget is None:
                return
            try:
                remove_widget = getattr(self.status_bar, "removeWidget", None)
                if callable(remove_widget):
                    remove_widget(widget)
            except Exception:
                pass
            try:
                widget.hide()
                widget.deleteLater()
            except Exception:
                pass

        def _show_terminal_failure(self, error):
            """Keep a restore failure visible without blocking RenderDoc."""

            show_message = getattr(self.status_bar, "showMessage", None)
            if not callable(show_message):
                return
            failed = _restore_status_text({
                "phase": "failed",
                "message": "",
                "current": 0,
                "total": 0,
                "item": "",
            })
            diagnostic = str(error or "")[:4096]
            text = (_qt_format(QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "%1: %2"), failed, diagnostic)
                if diagnostic else failed)
            try:
                # A temporary QStatusBar message is non-modal. It also hides
                # the normal progress widget before that widget is removed,
                # leaving foreground menus and EID navigation fully usable.
                show_message(text, 30000)
            except Exception:
                pass

        def publish_progress(self, value):
            """Worker-thread callback: publish Python data only, never touch Qt."""
            if self._finished:
                return False
            try:
                progress = _normalise_restore_progress(value)
            except Exception:
                # Progress is advisory UI data and must never abort a verified
                # restore operation. Invalid records are simply not displayed.
                return False
            try:
                self._progress_events.put_nowait(progress)
            except queue.Full:
                # Keep the newest state under a burst of per-resource updates.
                try:
                    self._progress_events.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._progress_events.put_nowait(progress)
                except queue.Full:
                    return False
            return True

        def start(self):
            if self._started or self._finished:
                return False
            self._started = True
            self.publish_progress({
                "phase": "restore-dispatch",
                "message": "",
                "current": 0,
                "total": 0,
            })

            def _restore():
                return self.restore_capture(
                    self.project_root, self.capture_path, self.publish_progress)

            try:
                self.executor.submit(
                    self.owner_token, _restore, self._restore_completed)
            except Exception as exc:
                self._restore_completed(
                    None, "%s: %s" % (type(exc).__name__, str(exc)))
                return False
            return True

        @QtCore.Slot()
        def _drain_progress(self):
            latest = None
            for unused_index in range(MAX_RESTORE_PROGRESS_EVENTS):
                del unused_index
                try:
                    latest = self._progress_events.get_nowait()
                except queue.Empty:
                    break
            if latest is None:
                return
            total = latest["total"]
            if self.progress_bar is not None and total > 0:
                self.progress_bar.setRange(0, total)
                self.progress_bar.setValue(latest["current"])
            elif self.progress_bar is not None:
                self.progress_bar.setRange(0, 0)
            if self.status_label is not None:
                text = _restore_status_text(latest)
                self.status_label.setProperty(
                    "replaylabPhase", latest["phase"])
                self.status_label.setText(text)
                self.status_label.setToolTip(text)

        def _restore_completed(self, value, error):
            if self._finished:
                return
            self._drain_progress()
            self._finished = True
            self._progress_timer.stop()
            try:
                self._progress_timer.timeout.disconnect(self._drain_progress)
            except Exception:
                pass
            terminal_error = _restore_result_error(value, error)
            if terminal_error:
                self._show_terminal_failure(terminal_error)
            self._remove_status_widget()
            completed = self.completed
            self.completed = lambda unused_value, unused_error: None
            completed(value, terminal_error)
            self.deleteLater()

        def abandon(self):
            """Detach a still-running restore when its launcher is torn down."""
            if self._finished:
                return
            self._finished = True
            self._progress_timer.stop()
            try:
                self._progress_timer.timeout.disconnect(self._drain_progress)
            except Exception:
                pass
            self.completed = lambda unused_value, unused_error: None
            self._remove_status_widget()
            self.deleteLater()

    class NewProjectDialog(QtWidgets.QDialog):
        def __init__(self, parent=None):
            QtWidgets.QDialog.__init__(self, parent)
            self.setWindowTitle(QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "New Project"))
            self.resize(720, 520)
            self.capture_files = []
            root = QtWidgets.QVBoxLayout(self)
            explanation = QtWidgets.QLabel(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "The project folder stores the portable parsed data and shared "
                    "knowledge. RDC entries remain references to their current files and "
                    "are not copied."))
            explanation.setWordWrap(True)
            root.addWidget(explanation)
            form = QtWidgets.QFormLayout()
            self.name = QtWidgets.QLineEdit()
            self.project_root = QtWidgets.QLineEdit()
            project_row = QtWidgets.QHBoxLayout()
            project_row.addWidget(self.project_root)
            browse_project = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Browse…"))
            browse_project.clicked.connect(self._browse_project)
            project_row.addWidget(browse_project)
            self.asset_root = QtWidgets.QLineEdit()
            asset_row = QtWidgets.QHBoxLayout()
            asset_row.addWidget(self.asset_root)
            browse_asset = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Browse…"))
            browse_asset.clicked.connect(self._browse_asset)
            asset_row.addWidget(browse_asset)
            form.addRow(QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "Project name *"), self.name)
            form.addRow(QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "Project path *"), project_row)
            form.addRow(QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "Unpacked assets"), asset_row)
            root.addLayout(form)
            capture_label = QtWidgets.QLabel(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Initial RDC references (files remain in their original locations)"))
            root.addWidget(capture_label)
            self.capture_list = QtWidgets.QListWidget()
            self.capture_list.setSelectionMode(
                QtWidgets.QAbstractItemView.ExtendedSelection)
            root.addWidget(self.capture_list, 1)
            capture_buttons = QtWidgets.QHBoxLayout()
            add_captures = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Add RDC files…"))
            add_captures.clicked.connect(self._browse_captures)
            remove_captures = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Remove Selected"))
            remove_captures.clicked.connect(self._remove_selected_captures)
            capture_buttons.addWidget(add_captures)
            capture_buttons.addWidget(remove_captures)
            capture_buttons.addStretch(1)
            root.addLayout(capture_buttons)
            self.remove_shortcut = QtWidgets.QShortcut(
                QtGui.QKeySequence.Delete, self.capture_list)
            self.remove_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            self.remove_shortcut.activated.connect(self._remove_selected_captures)
            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
            buttons.accepted.connect(self._accept_checked)
            buttons.rejected.connect(self.reject)
            root.addWidget(buttons)

        @staticmethod
        def _project_folder_name(value):
            name = str(value or "").strip()
            for character in '<>:"/\\|?*':
                name = name.replace(character, "-")
            return name.rstrip(" .") or QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "Project")

        def _browse_project(self):
            current = self.project_root.text().strip()
            start = os.path.dirname(current) if current else ""
            parent = QtWidgets.QFileDialog.getExistingDirectory(
                self, QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Select location for the project folder"), start)
            if parent:
                self.project_root.setText(os.path.join(
                    parent, self._project_folder_name(self.name.text())))

        def _browse_asset(self):
            path = QtWidgets.QFileDialog.getExistingDirectory(
                self, QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Select unpacked asset directory"), self.asset_root.text())
            if path:
                self.asset_root.setText(path)

        def _browse_captures(self):
            paths, unused_filter = QtWidgets.QFileDialog.getOpenFileNames(
                self, QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Add RenderDoc captures"), "",
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "RenderDoc Captures (*.rdc)"))
            del unused_filter
            for path in paths:
                key = os.path.normcase(os.path.abspath(path))
                if key not in set(os.path.normcase(p) for p in self.capture_files):
                    self.capture_files.append(os.path.abspath(path))
                    item = QtWidgets.QListWidgetItem(os.path.basename(path))
                    item.setData(QtCore.Qt.UserRole, os.path.abspath(path))
                    item.setToolTip(os.path.abspath(path))
                    self.capture_list.addItem(item)

        def _remove_selected_captures(self):
            selected = list(self.capture_list.selectedItems())
            if not selected:
                return
            removed = {
                os.path.normcase(os.path.abspath(str(
                    item.data(QtCore.Qt.UserRole) or "")))
                for item in selected
            }
            self.capture_files = [
                path for path in self.capture_files
                if os.path.normcase(os.path.abspath(path)) not in removed
            ]
            for item in selected:
                row = self.capture_list.row(item)
                if row >= 0:
                    self.capture_list.takeItem(row)

        def _accept_checked(self):
            if not self.name.text().strip():
                QtWidgets.QMessageBox.warning(
                    self, QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Missing project identity"),
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Project name is required."))
                return
            project_root = self.project_root.text().strip()
            if not project_root or not os.path.isabs(project_root):
                QtWidgets.QMessageBox.warning(
                    self, QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Missing project path"),
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "Choose an absolute path for the new project folder."))
                return
            if os.path.exists(project_root):
                QtWidgets.QMessageBox.warning(
                    self, QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Project path already exists"),
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "Choose a path for a new project folder. Existing projects can be "
                        "added from the main project window."))
                return
            self.accept()

        def values(self):
            return {
                "name": self.name.text().strip(),
                "project_root": os.path.abspath(self.project_root.text().strip()),
                "asset_root": self.asset_root.text().strip(),
                "capture_files": list(self.capture_files),
            }


    class ProjectSettingsDialog(QtWidgets.QDialog):
        """Edit the mutable asset root and bind a pending project once."""

        def __init__(self, project, parent=None):
            QtWidgets.QDialog.__init__(self, parent)
            self.project = project
            self.bound = project.get("state") == "bound"
            scope = project.get("scope") or {}
            self.project_scoped = (
                scope.get("gameId") == "replaylab-project" and
                len(str(scope.get("buildId", "") or "")) == 32)
            self.setWindowTitle(QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "Project Settings"))
            self.resize(650, 360)
            root = QtWidgets.QVBoxLayout(self)
            title = QtWidgets.QLabel("%s\n%s" % (
                project.get("name", ""), project.get("projectRoot", "")))
            title.setWordWrap(True)
            root.addWidget(title)
            explanation = QtWidgets.QLabel(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "This project is its own portable sharing boundary; only the unpacked asset "
                    "path is machine-specific and editable.") if self.project_scoped else
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Game/build is an immutable physical-sharing boundary. A pending legacy "
                    "project may be bound once; a bound project can only update its unpacked "
                    "asset path."))
            explanation.setWordWrap(True)
            root.addWidget(explanation)
            form = QtWidgets.QFormLayout()
            self.game_id = QtWidgets.QLineEdit(scope.get("gameId", ""))
            self.build_id = QtWidgets.QLineEdit(scope.get("buildId", ""))
            self.platform = QtWidgets.QLineEdit(
                scope.get("platform", "") or "windows-x64")
            self.api_family = QtWidgets.QComboBox()
            self.api_family.addItems(["", "d3d11", "d3d12", "vulkan", "opengl"])
            api = scope.get("apiFamily", "")
            if api and self.api_family.findText(api) < 0:
                self.api_family.addItem(api)
            self.api_family.setCurrentText(api)
            for widget in (
                    self.game_id, self.build_id, self.platform, self.api_family):
                widget.setEnabled(not self.bound)
            self.asset_root = QtWidgets.QLineEdit(project.get("assetRoot") or "")
            asset_row = QtWidgets.QHBoxLayout()
            asset_row.addWidget(self.asset_root)
            browse = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Browse…"))
            browse.clicked.connect(self._browse_asset)
            asset_row.addWidget(browse)
            clear = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Clear"))
            clear.clicked.connect(self.asset_root.clear)
            asset_row.addWidget(clear)
            if not self.project_scoped:
                form.addRow(QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Game ID"), self.game_id)
                form.addRow(QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Build ID"), self.build_id)
                form.addRow(QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Platform"), self.platform)
                form.addRow(QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Graphics API"), self.api_family)
            form.addRow(QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "Unpacked assets"), asset_row)
            root.addLayout(form)
            state = QtWidgets.QLabel(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Scope: this project workspace") if self.project_scoped else
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Scope: bound and immutable") if self.bound else
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Scope: pending; cross-frame physical reuse is disabled"))
            root.addWidget(state)
            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
            buttons.accepted.connect(self._accept_checked)
            buttons.rejected.connect(self.reject)
            root.addWidget(buttons)

        def _browse_asset(self):
            path = QtWidgets.QFileDialog.getExistingDirectory(
                self, QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Select unpacked asset directory"), self.asset_root.text())
            if path:
                self.asset_root.setText(path)

        def _accept_checked(self):
            if self.project_scoped:
                self.accept()
                return
            game = self.game_id.text().strip()
            build = self.build_id.text().strip()
            if bool(game) != bool(build):
                QtWidgets.QMessageBox.warning(
                    self, QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Incomplete sharing boundary"),
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "Supply both Game ID and Build ID, or leave both empty to keep this "
                        "project pending."))
                return
            self.accept()

        def values(self):
            return _project_settings_payload(
                self.project_scoped,
                self.game_id.text(),
                self.build_id.text(),
                self.platform.text(),
                self.api_family.currentText(),
                self.asset_root.text(),
            )


    class ReplayLabProjectLauncher(QtWidgets.QDialog):
        def __init__(self, client, open_capture, parent=None, closed_callback=None,
                     thumbnail_reader=None, thumbnail_cache=None,
                     async_executor=None, restore_capture=None):
            QtWidgets.QDialog.__init__(self, parent)
            self.client = client
            self.open_capture = open_capture
            if restore_capture is not None and not callable(restore_capture):
                raise ProjectLauncherError("restore_capture must be callable")
            self.restore_capture = restore_capture
            self.closed_callback = closed_callback
            self.thumbnail_reader = thumbnail_reader
            self.thumbnail_cache = thumbnail_cache or ThumbnailCache()
            self.async_executor = async_executor or AsyncProjectExecutor()
            self._async_owner = "launcher-%s" % uuid.uuid4().hex
            self._operation_generation = 0
            self._busy = False
            self._restore_task = None
            self.projects = []
            self._closed_notified = False
            self._thumbnail_generation = 0
            self._thumbnail_queue = []
            self._thumbnail_loaded = 0
            self._thumbnail_failed = 0
            self.thumbnail_timer = QtCore.QTimer(self)
            self.thumbnail_timer.setSingleShot(True)
            self.thumbnail_timer.timeout.connect(self._process_thumbnail_queue)
            self.finished.connect(self._dialog_finished)
            self.setWindowTitle(QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "Projects"))
            self.setModal(False)
            self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
            self.resize(1080, 700)
            root = QtWidgets.QVBoxLayout(self)
            title = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "Project Workspace"))
            font = title.font()
            font.setPointSize(font.pointSize() + 4)
            font.setBold(True)
            title.setFont(font)
            root.addWidget(title)
            subtitle = QtWidgets.QLabel(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Select a project. Shader, CBuffer aliases, asset matches, analysis and "
                    "pass knowledge are shared only inside that portable project boundary."))
            subtitle.setWordWrap(True)
            root.addWidget(subtitle)
            splitter = QtWidgets.QSplitter()
            root.addWidget(splitter, 1)

            left = QtWidgets.QWidget()
            left_layout = QtWidgets.QVBoxLayout(left)
            left_layout.addWidget(QtWidgets.QLabel(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Projects")))
            self.project_list = QtWidgets.QListWidget()
            self.project_list.currentRowChanged.connect(self._project_selected)
            self.project_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.project_list.customContextMenuRequested.connect(
                self._show_project_context_menu)
            left_layout.addWidget(self.project_list, 1)
            project_buttons = QtWidgets.QGridLayout()
            self.create_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "New Project…"))
            self.create_button.clicked.connect(self._new_project)
            self.add_project_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Add Existing…"))
            self.add_project_button.clicked.connect(self._add_existing_project)
            self.settings_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Project Settings…"))
            self.settings_button.clicked.connect(self._project_settings)
            project_buttons.addWidget(self.create_button, 0, 0)
            project_buttons.addWidget(self.add_project_button, 0, 1)
            project_buttons.addWidget(self.settings_button, 1, 0, 1, 2)
            left_layout.addLayout(project_buttons)
            self.remove_project_shortcut = QtWidgets.QShortcut(
                QtGui.QKeySequence.Delete, self.project_list)
            self.remove_project_shortcut.setContext(
                QtCore.Qt.WidgetWithChildrenShortcut)
            self.remove_project_shortcut.activated.connect(
                self._remove_selected_project)
            splitter.addWidget(left)

            right = QtWidgets.QWidget()
            right_layout = QtWidgets.QVBoxLayout(right)
            self.project_info = QtWidgets.QLabel(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Select a project"))
            self.project_info.setWordWrap(True)
            right_layout.addWidget(self.project_info)
            self.capture_list = QtWidgets.QListWidget()
            self.capture_list.setSelectionMode(
                QtWidgets.QAbstractItemView.ExtendedSelection)
            self.capture_list.setViewMode(QtWidgets.QListView.IconMode)
            self.capture_list.setResizeMode(QtWidgets.QListView.Adjust)
            self.capture_list.setIconSize(QtCore.QSize(144, 84))
            self.capture_list.setGridSize(QtCore.QSize(220, 150))
            self.capture_list.itemDoubleClicked.connect(self._capture_double_clicked)
            self.capture_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.capture_list.customContextMenuRequested.connect(
                self._show_capture_context_menu)
            right_layout.addWidget(self.capture_list, 1)
            self.thumbnail_note = QtWidgets.QLabel(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Embedded RDC thumbnails load lazily from capture metadata; no frame "
                    "replay is run."))
            self.thumbnail_note.setWordWrap(True)
            right_layout.addWidget(self.thumbnail_note)
            capture_buttons = QtWidgets.QHBoxLayout()
            self.add_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Add RDC files…"))
            self.add_button.clicked.connect(self._add_captures)
            self.open_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Open Selected Frame"))
            self.open_button.clicked.connect(self._open_selected_capture)
            capture_buttons.addWidget(self.add_button)
            capture_buttons.addStretch(1)
            capture_buttons.addWidget(self.open_button)
            right_layout.addLayout(capture_buttons)
            self.remove_capture_shortcut = QtWidgets.QShortcut(
                QtGui.QKeySequence.Delete, self.capture_list)
            self.remove_capture_shortcut.setContext(
                QtCore.Qt.WidgetWithChildrenShortcut)
            self.remove_capture_shortcut.activated.connect(
                self._remove_selected_captures)
            splitter.addWidget(right)
            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 2)
            self.status = QtWidgets.QLabel()
            root.addWidget(self.status)
            self.operation_progress = QtWidgets.QProgressBar()
            self.operation_progress.setRange(0, 0)
            self.operation_progress.setTextVisible(False)
            self.operation_progress.hide()
            root.addWidget(self.operation_progress)
            self._watch_baseline = None
            self._watch_in_flight = False
            self._watch_pending = False
            self.metadata_watch_timer = QtCore.QTimer(self)
            self.metadata_watch_timer.setInterval(1500)
            self.metadata_watch_timer.timeout.connect(self._poll_project_metadata)
            self.metadata_watch_timer.start()
            self.refresh()

        def _poll_project_metadata(self):
            if (self._closed_notified or self._busy or self._watch_in_flight or
                    not self.isVisible() or QtWidgets.QApplication.activeModalWidget() not in (None, self)):
                return
            if self._watch_pending:
                self._watch_pending = False
                self.refresh()
                return
            paths = _project_watch.metadata_paths(self.projects, os.environ)
            self._watch_in_flight = True

            def observed(value, error):
                self._watch_in_flight = False
                if self._closed_notified or error:
                    return
                changed = self._watch_baseline is not None and value != self._watch_baseline
                first = self._watch_baseline is None
                self._watch_baseline = value
                # One initial resynchronization closes the opening-time race;
                # subsequent ticks refresh only when metadata actually changes.
                if changed or first:
                    self._watch_pending = True
                    if not self._busy and QtWidgets.QApplication.activeModalWidget() in (None, self):
                        self._watch_pending = False
                        self.refresh()
            try:
                self.async_executor.submit(self._async_owner, lambda: _project_watch.snapshot(paths), observed)
            except Exception:
                self._watch_in_flight = False

        @staticmethod
        def _placeholder_icon(available):
            pixmap = QtGui.QPixmap(144, 84)
            pixmap.fill(QtGui.QColor("#44515f" if available else "#683e45"))
            painter = QtGui.QPainter(pixmap)
            painter.setPen(QtGui.QColor("white"))
            font = painter.font()
            font.setBold(True)
            font.setPointSize(16)
            painter.setFont(font)
            painter.drawText(pixmap.rect(), QtCore.Qt.AlignCenter, "RDC")
            painter.end()
            return QtGui.QIcon(pixmap)

        @staticmethod
        def _size_text(value):
            if value is None:
                return QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "missing")
            size = float(value)
            for unit in ("B", "KiB", "MiB", "GiB"):
                if size < 1024.0 or unit == "GiB":
                    return "%.1f %s" % (size, unit)
                size /= 1024.0
            return "%d B" % int(value)

        def _error(self, title, error):
            self.status.setText(_qt_format(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Not completed: %1"), error))
            QtWidgets.QMessageBox.critical(self, title, str(error))

        def _set_busy(self, busy, message=""):
            self._busy = bool(busy)
            for widget in (
                    self.project_list, self.capture_list, self.create_button,
                    self.add_project_button, self.settings_button, self.add_button,
                    self.open_button):
                widget.setEnabled(not self._busy)
            self.operation_progress.setVisible(self._busy)
            if self._busy:
                self.remove_project_shortcut.setEnabled(False)
                self.remove_capture_shortcut.setEnabled(False)
            if not self._busy:
                self._sync_project_actions()
            if message:
                self.status.setText(message)

        def _sync_project_actions(self):
            if self._busy:
                return
            project = self._selected_project()
            has_project = project is not None
            usable = has_project and not project.get("unavailable", False)
            self.capture_list.setEnabled(usable)
            self.settings_button.setEnabled(usable)
            self.add_button.setEnabled(usable)
            self.open_button.setEnabled(usable)
            self.remove_project_shortcut.setEnabled(has_project)
            self.remove_capture_shortcut.setEnabled(usable)

        def _show_project_context_menu(self, position):
            item = self.project_list.itemAt(position)
            if item is not None:
                row = self.project_list.row(item)
                if row >= 0:
                    self.project_list.setCurrentRow(row)

            menu = QtWidgets.QMenu(self.project_list)
            refresh_action = menu.addAction(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Refresh Projects"))
            remove_action = menu.addAction(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Remove Project"))
            permissions_action = menu.addAction(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Open Folder Permissions…"))
            repair_action = menu.addAction(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Repair Project Access (UAC)…"))
            refresh_action.setEnabled(not self._busy)
            # A blank-area click must never act on the stale current project.
            remove_action.setEnabled(
                not self._busy and item is not None and
                self._selected_project() is not None)
            permissions_action.setEnabled(item is not None and self._selected_project() is not None)
            repair_action.setEnabled(os.name == "nt" and not self._busy and item is not None and self._selected_project() is not None)
            chosen = menu.exec_(
                self.project_list.viewport().mapToGlobal(position))
            if chosen is refresh_action:
                self.refresh()
            elif chosen is remove_action:
                self._remove_selected_project()
            elif chosen is repair_action:
                self._repair_selected_project_access()
            elif chosen is permissions_action:
                project = self._selected_project()
                if not project:
                    return
                try:
                    if os.name == "nt":
                        # Explicit user action only. Open Windows' own folder
                        # security dialog; never elevate the whole application
                        # or grant broad ACLs to a copied project automatically.
                        import ctypes
                        from ctypes import wintypes
                        class ShellExecuteInfo(ctypes.Structure):
                            _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                                ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                                ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                                ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                                ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                                ("hProcess", wintypes.HANDLE)]
                        request = ShellExecuteInfo()
                        request.cbSize = ctypes.sizeof(request); request.fMask = 0x0000000c
                        request.lpVerb = "properties"; request.lpFile = project["projectRoot"]; request.nShow = 1
                        shell = ctypes.WinDLL("shell32", use_last_error=True)
                        shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
                        if not shell.ShellExecuteExW(ctypes.byref(request)):
                            raise ctypes.WinError(ctypes.get_last_error())
                    else:
                        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(project["projectRoot"]))
                except OSError as error:
                    self.status.setText(_qt_format(QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Could not open folder permissions: %1"), str(error)))

        def _repair_selected_project_access(self):
            project = self._selected_project()
            if not project:
                return
            root, workspace_id = project["projectRoot"], project["workspaceId"]

            def repaired(result):
                status = result.get("status")
                if status == "repaired":
                    self.refresh(workspace_id)
                elif status == "cancelled":
                    self.status.setText(QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Project access repair was cancelled."))
                else:
                    self.status.setText(QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Permission repair is still running. Refresh Projects after it finishes."))
            self._run_async(_qt_format(QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, "Repairing access to %1. Windows may request administrator approval for this project only…"), root),
                lambda: _project_permissions.repair(root, workspace_id), repaired,
                QtCore.QCoreApplication.translate(TRANSLATION_CONTEXT, "Could not repair project access"))

        def _show_capture_context_menu(self, position):
            item = self.capture_list.itemAt(position)
            if item is not None:
                if not item.isSelected():
                    # Right-clicking outside the existing multi-selection targets
                    # only the clicked capture. Clicking inside it preserves all
                    # selected captures for the remove operation.
                    self.capture_list.clearSelection()
                    item.setSelected(True)
                self.capture_list.setCurrentItem(
                    item, QtCore.QItemSelectionModel.NoUpdate)

            project = self._selected_project()
            usable = bool(
                project is not None and
                not project.get("unavailable", False) and
                not self._busy)
            menu = QtWidgets.QMenu(self.capture_list)
            open_action = menu.addAction(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Open Selected Frame"))
            remove_action = menu.addAction(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Remove from Project"))
            # Blank-space context menus cannot operate on a stale selection.
            open_action.setEnabled(
                usable and item is not None and
                bool(item.data(QtCore.Qt.UserRole + 2)))
            remove_action.setEnabled(
                usable and item is not None and
                bool(self.capture_list.selectedItems()))
            chosen = menu.exec_(
                self.capture_list.viewport().mapToGlobal(position))
            if chosen is open_action:
                self._open_selected_capture()
            elif chosen is remove_action:
                self._remove_selected_captures()

        def _run_async(self, message, operation, success, error_title):
            if self._busy or self._closed_notified:
                return False
            self._operation_generation += 1
            generation = self._operation_generation
            self._set_busy(True, message)

            def _completed(value, error):
                if (self._closed_notified or
                        generation != self._operation_generation):
                    return
                self._set_busy(False)
                if error:
                    self._error(error_title, error)
                    return
                success(value)

            try:
                self.async_executor.submit(
                    self._async_owner, operation, _completed)
            except Exception as exc:
                self._set_busy(False)
                self._error(error_title, exc)
                return False
            return True

        def refresh(self, select_workspace_id=""):
            previous = select_workspace_id
            current = self._selected_project()
            if not previous and current:
                previous = current["workspaceId"]
            selected_captures = {item.data(QtCore.Qt.UserRole) for item in self.capture_list.selectedItems()}
            selection_owner = current["workspaceId"] if current else ""
            project_scroll = self.project_list.verticalScrollBar().value()
            capture_scroll = self.capture_list.verticalScrollBar().value()
            def _loaded(projects):
                self._apply_projects(projects, previous)
                self.project_list.verticalScrollBar().setValue(project_scroll)
                selected_project = self._selected_project()
                if selected_project and selected_project["workspaceId"] == selection_owner:
                    for index in range(self.capture_list.count()):
                        item = self.capture_list.item(index)
                        if item.data(QtCore.Qt.UserRole) in selected_captures:
                            item.setSelected(True)
                self.capture_list.verticalScrollBar().setValue(capture_scroll)

            return self._run_async(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Loading projects in the background…"),
                lambda: _load_projects_with_access_repair(self.client,
                    self.__dict__.setdefault("_automatic_access_repairs", set())),
                _loaded,
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Could not load projects"))

        def _apply_projects(self, projects, previous=""):
            self.projects = list(projects or [])
            self.project_list.clear()
            selected_row = -1
            for row, project in enumerate(self.projects):
                active = "● " if project["active"] else ""
                unavailable = "⚠ " if project.get("unavailable", False) else ""
                item = QtWidgets.QListWidgetItem(
                    "%s%s%s\n%s" %
                    (active, unavailable, project["name"], project["projectRoot"]))
                if project.get("unavailable", False):
                    item.setToolTip(_unavailable_project_reason_text(project))
                self.project_list.addItem(item)
                if project["workspaceId"] == previous or (
                        selected_row < 0 and project["active"]):
                    selected_row = row
            if selected_row < 0 and self.projects:
                selected_row = 0
            if selected_row >= 0:
                self.project_list.setCurrentRow(selected_row)
            else:
                self.project_info.setText(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "No projects yet. Create a project or add an existing project folder."))
                self.capture_list.clear()
            self._sync_project_actions()
            unavailable_count = sum(
                1 for project in self.projects if project.get("unavailable", False))
            unavailable_suffix = _qt_format(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, " · %1 unavailable"),
                unavailable_count) if unavailable_count else ""
            self.status.setText(_qt_format(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Loaded %1 project record(s)%2"),
                len(self.projects), unavailable_suffix))

        def _apply_updated_project(self, updated):
            self.projects, row = replace_project_snapshot(self.projects, updated)
            # Add/remove capture operations cannot rename or relocate a project,
            # so keep the existing list item and selected row. Rebuild only the
            # selected project's RDC view from the authoritative returned status.
            self.project_list.setCurrentRow(row)
            self._project_selected(row)

        def _selected_project(self):
            row = self.project_list.currentRow()
            return self.projects[row] if 0 <= row < len(self.projects) else None

        def _project_selected(self, row):
            self._cancel_thumbnail_queue()
            if row < 0 or row >= len(self.projects):
                self._sync_project_actions()
                return
            project = self.projects[row]
            if project.get("unavailable", False):
                reason = _unavailable_project_reason_text(project)
                self.project_info.setText(_qt_format(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "%1 (unavailable)\nProject: %2\nReason: %3\n\nYou can remove this "
                        "machine-local record; no project files are deleted."),
                    project["name"], project["projectRoot"], reason))
                self.capture_list.clear()
                self.thumbnail_note.setText(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "Project files are unavailable, so RDC references cannot be read."))
                self._sync_project_actions()
                return
            scope = project["scope"]
            if scope.get("gameId") == "replaylab-project":
                sharing_boundary = QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "this project workspace")
            elif scope.get("gameId") and scope.get("buildId"):
                sharing_boundary = "%s / %s" % (
                    scope.get("gameId"), scope.get("buildId"))
            else:
                sharing_boundary = QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "pending (legacy project)")
            active_suffix = QtCore.QCoreApplication.translate(
                TRANSLATION_CONTEXT, " (active)") if project["active"] else ""
            assets = project.get("assetRoot") or \
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "not configured")
            self.project_info.setText(_qt_format(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "%1%2\nSharing boundary: %3\nAssets: %4\nProject: %5"),
                project["name"], active_suffix, sharing_boundary,
                assets, project["projectRoot"]))
            self.capture_list.clear()
            queued = 0
            for capture in project["captures"]:
                text = "%s\n%s\n%s" % (
                    capture["displayName"], self._size_text(capture["sizeBytes"]),
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Available") if capture["available"] else
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "File missing"))
                item = QtWidgets.QListWidgetItem(
                    self._placeholder_icon(capture["available"]), text)
                item.setData(QtCore.Qt.UserRole, capture["path"])
                item.setData(QtCore.Qt.UserRole + 1, capture["captureRefId"])
                item.setData(QtCore.Qt.UserRole + 2, capture["available"])
                item.setToolTip(capture["path"])
                self.capture_list.addItem(item)
                if (capture["available"] and self.thumbnail_reader is not None and
                        queued < MAX_THUMBNAILS_PER_SELECTION):
                    self._thumbnail_queue.append((
                        self._thumbnail_generation, item, capture["path"]))
                    queued += 1
                elif capture["available"] and queued >= MAX_THUMBNAILS_PER_SELECTION:
                    item.setToolTip(_qt_format(
                        QtCore.QCoreApplication.translate(
                            TRANSLATION_CONTEXT,
                            "Thumbnail deferred by the per-selection preview limit (%1)."),
                        MAX_THUMBNAILS_PER_SELECTION))
            if queued:
                self.thumbnail_note.setText(_qt_format(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "Loading %1 embedded RDC thumbnail(s) from metadata only; no replay "
                        "is run…"), queued))
                self.thumbnail_timer.start(0)
            elif self.thumbnail_reader is None:
                self.thumbnail_note.setText(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "Embedded thumbnail reader is unavailable; RDC placeholders remain "
                        "visible."))
            else:
                self.thumbnail_note.setText(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "No available RDC thumbnails to load for this project."))
            self._sync_project_actions()

        def _cancel_thumbnail_queue(self):
            self.thumbnail_timer.stop()
            self._thumbnail_generation += 1
            self._thumbnail_queue = []
            self._thumbnail_loaded = 0
            self._thumbnail_failed = 0

        def _process_thumbnail_queue(self):
            generation = self._thumbnail_generation
            while self._thumbnail_queue:
                queued_generation, item, path = self._thumbnail_queue.pop(0)
                if queued_generation != generation:
                    continue
                result = self.thumbnail_cache.get_or_load(
                    path, self.thumbnail_reader)
                if (generation != self._thumbnail_generation or
                        item.listWidget() is not self.capture_list):
                    return
                if result["state"] == "ready":
                    pixmap = QtGui.QPixmap()
                    if pixmap.loadFromData(result["data"], "PNG") and not pixmap.isNull():
                        item.setIcon(QtGui.QIcon(pixmap))
                        cache_suffix = QtCore.QCoreApplication.translate(
                            TRANSLATION_CONTEXT,
                            " · cached") if result.get("cacheHit") else ""
                        item.setToolTip(_qt_format(
                            QtCore.QCoreApplication.translate(
                                TRANSLATION_CONTEXT,
                                "Embedded RDC thumbnail · %1x%2%3"),
                            result["width"], result["height"], cache_suffix))
                        self._thumbnail_loaded += 1
                    else:
                        item.setToolTip(QtCore.QCoreApplication.translate(
                            TRANSLATION_CONTEXT,
                            "Embedded RDC thumbnail could not be decoded by Qt"))
                        self._thumbnail_failed += 1
                else:
                    item.setToolTip(_thumbnail_reason_text(result))
                    self._thumbnail_failed += 1
                break
            if self._thumbnail_queue:
                # Keep all RenderDoc and QPixmap work on the UI thread while
                # yielding between files so a large project stays responsive.
                self.thumbnail_timer.start(0)
            else:
                self.thumbnail_note.setText(_qt_format(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "Embedded metadata thumbnails: %1 loaded, %2 unavailable · no replay "
                        "was run."),
                    self._thumbnail_loaded, self._thumbnail_failed))

        def _new_project(self):
            dialog = NewProjectDialog(self)
            if dialog.exec_() != QtWidgets.QDialog.Accepted:
                return
            values = dialog.values()

            def _created(project):
                self.refresh(project["workspaceId"])
                self.status.setText(_qt_format(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Created and activated %1"),
                    project["name"]))

            captures = values.get("capture_files") or []
            message = (_qt_format(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Creating the project and adding %1 RDC reference(s)…"),
                len(captures)) if captures else
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Creating the project in the background…"))
            self._run_async(
                message,
                lambda: self.client.create_project(**values),
                _created,
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Could not create project"))

        def _add_existing_project(self):
            path = QtWidgets.QFileDialog.getExistingDirectory(
                self, QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Select an existing project folder"), "")
            if not path:
                return

            def _registered(project):
                self.refresh(project["workspaceId"])
                self.status.setText(_qt_format(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Added existing project: %1"),
                    project["name"]))

            self._run_async(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Validating and adding the existing project…"),
                lambda: self.client.register_existing_project(path),
                _registered,
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Could not add existing project"))

        def _remove_selected_project(self):
            project = self._selected_project()
            if not project:
                return
            project_root = project["projectRoot"]
            answer = QtWidgets.QMessageBox.question(
                self,
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Remove project record"),
                _qt_format(QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Remove '%1' from this project list?\n\nThe project folder and every "
                    "local file will remain on disk."), project["name"]),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if answer != QtWidgets.QMessageBox.Yes:
                return

            def _removed(unused_result):
                del unused_result
                self.refresh()
                self.status.setText(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "Removed the project record; local project files were kept"))

            self._run_async(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Removing the project record…"),
                lambda: self.client.unregister_project(project_root),
                _removed,
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Could not remove project"))

        def _project_settings(self):
            project = self._selected_project()
            if not project or project.get("unavailable", False):
                return
            dialog = ProjectSettingsDialog(project, self)
            if dialog.exec_() != QtWidgets.QDialog.Accepted:
                return
            project_root = project["projectRoot"]
            values = dialog.values()

            def _configured(updated):
                self.refresh(updated["workspaceId"])
                self.status.setText(_qt_format(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Updated project settings: %1"),
                    updated["name"]))

            self._run_async(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Updating project settings in the background…"),
                lambda: self.client.configure_project(project_root, **values),
                _configured,
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Could not update project settings"))

        def _add_captures(self):
            project = self._selected_project()
            if not project or project.get("unavailable", False):
                return
            paths, unused_filter = QtWidgets.QFileDialog.getOpenFileNames(
                self, QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Add RenderDoc captures"), "",
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "RenderDoc Captures (*.rdc)"))
            del unused_filter
            if not paths:
                return
            project_root = project["projectRoot"]
            capture_paths = list(paths)

            def _added(updated):
                self._apply_updated_project(updated)
                self.status.setText(_qt_format(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "Added %1 capture reference(s)"), len(capture_paths)))

            self._run_async(
                _qt_format(QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Adding %1 RDC reference(s) in the background…"),
                    len(capture_paths)),
                lambda: self.client.add_captures(project_root, capture_paths),
                _added,
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Could not add captures"))

        def _remove_selected_captures(self):
            project = self._selected_project()
            selected = list(self.capture_list.selectedItems())
            if (not project or project.get("unavailable", False) or
                    not selected):
                return
            capture_ref_ids = [
                str(item.data(QtCore.Qt.UserRole + 1) or "")
                for item in selected
            ]
            answer = QtWidgets.QMessageBox.question(
                self,
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Remove RDC references"),
                _qt_format(QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Remove %1 selected RDC reference(s) from this project?\n\nThe local "
                    ".rdc files will not be deleted."), len(capture_ref_ids)),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if answer != QtWidgets.QMessageBox.Yes:
                return
            project_root = project["projectRoot"]

            def _removed(updated):
                self._apply_updated_project(updated)
                self.status.setText(_qt_format(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "Removed %1 RDC reference(s); local files were kept"),
                    len(capture_ref_ids)))

            self._run_async(
                _qt_format(QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Removing %1 RDC reference(s)…"), len(capture_ref_ids)),
                lambda: self.client.remove_captures(
                    project_root, capture_ref_ids),
                _removed,
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Could not remove RDC references"))

        def _capture_double_clicked(self, item):
            self._open_capture_path(str(item.data(QtCore.Qt.UserRole) or ""))

        def _open_selected_capture(self):
            item = self.capture_list.currentItem()
            if item:
                self._capture_double_clicked(item)

        def _open_capture_path(self, path):
            project = self._selected_project()
            if not project or not path:
                return
            if not os.path.isfile(path) or os.path.islink(path):
                self._error(
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Capture unavailable"),
                    QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT,
                        "The referenced RDC file is unavailable"))
                return
            project_root = project["projectRoot"]

            def _activated(unused_project):
                del unused_project
                # LoadCapture owns the real replay progress dialog. Hide this
                # launcher first so the native progress UI is not obscured, but
                # keep the launcher alive so an immediate Python/API failure can
                # restore the user's project selection and report the error.
                self._set_busy(True, QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Opening the selected RDC in RenderDoc…"))
                self.hide()
                try:
                    opened = self.open_capture(path)
                    open_error = _capture_open_error(opened)
                    if open_error:
                        raise ProjectLauncherError(open_error)
                except Exception as exc:
                    self.show()
                    self.raise_()
                    self.activateWindow()
                    self._set_busy(False)
                    self._error(QtCore.QCoreApplication.translate(
                        TRANSLATION_CONTEXT, "Could not open capture"), exc)
                    return
                if self.restore_capture is None:
                    self.close()
                    return
                self._start_capture_restore(project_root, path)

            self._run_async(
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT,
                    "Activating the project before opening this RDC…"),
                lambda: self.client.activate(project_root),
                _activated,
                QtCore.QCoreApplication.translate(
                    TRANSLATION_CONTEXT, "Could not open capture"))

        def _start_capture_restore(self, project_root, capture_path):
            parent = self.parentWidget() or self
            status_bar = getattr(parent, "statusBar", lambda: None)()
            if status_bar is None:
                # ICaptureContext.GetMainWindow().Widget() is intentionally
                # exposed to Python as a plain QWidget by qrenderdoc's SWIG
                # boundary, even though the native object is a QMainWindow.
                # Resolve the Designer-owned status bar by type/object name.
                find_child = getattr(parent, "findChild", None)
                if callable(find_child):
                    status_bar = find_child(
                        QtWidgets.QStatusBar, "statusBar")
            task = ReplayLabRestoreStatusTask(
                self.async_executor, self._async_owner, self.restore_capture,
                project_root, capture_path, self._capture_restore_finished,
                status_bar, self)
            self._restore_task = task
            task.start()

        def _capture_restore_finished(self, unused_value, error):
            del unused_value
            self._restore_task = None
            if error:
                # ReplayLabRestoreStatusTask already published the bounded
                # diagnostic in the main status bar. A background accelerator
                # failure must never put a modal window in front of the opened
                # capture or prevent EID navigation.
                print("[Bread] Background project restore failed: %s" % error)
            self.close()

        def abandon_capture_restore(self):
            """Remove status for a restore whose owning capture was replaced."""
            restore_task = self._restore_task
            if restore_task is None:
                return False
            self._restore_task = None
            try:
                restore_task.abandon()
            except Exception:
                pass
            try:
                self.async_executor.cancel_owner(self._async_owner)
            except Exception:
                pass
            self.close()
            return True

        def _dialog_finished(self, unused_result=0):
            del unused_result
            self.metadata_watch_timer.stop()
            self._cancel_thumbnail_queue()
            self._operation_generation += 1
            restore_task = self._restore_task
            self._restore_task = None
            if restore_task is not None:
                try:
                    restore_task.abandon()
                except Exception:
                    pass
            try:
                self.async_executor.cancel_owner(self._async_owner)
            except Exception:
                pass
            notify = not self._closed_notified
            self._closed_notified = True
            if self.closed_callback and notify:
                self.closed_callback()

        def closeEvent(self, event):
            self.metadata_watch_timer.stop()
            self._dialog_finished()
            QtWidgets.QDialog.closeEvent(self, event)


__all__ = [
    "PROJECT_LIST_SCHEMA",
    "PROJECT_WORKSPACE_SCHEMA",
    "CAPTURE_LIBRARY_SCHEMA",
    "CAPTURE_CONTENT_IDENTITY_SCHEMA",
    "CAPTURE_PROJECT_MATCH_SCHEMA",
    "ProjectLauncherError",
    "ThumbnailReadError",
    "ReplayLabProjectClient",
    "RenderDocThumbnailReader",
    "ThumbnailCache",
    "AsyncCallbackRegistry",
    "validate_project",
    "replace_project_snapshot",
    "launcher_startup_allowed",
    "unique_project_for_capture",
    "HAS_QT",
]

if HAS_QT:
    __all__.extend([
        "AsyncProjectExecutor", "ReplayLabRestoreStatusTask",
        "NewProjectDialog", "ProjectSettingsDialog", "ReplayLabProjectLauncher"])
