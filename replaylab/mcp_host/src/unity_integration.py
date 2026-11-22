"""Project-scoped, on-demand Unity MCP installation and connection.

Coplay's MIT-licensed MCP is pinned independently of Bread's Python runtime. Nothing
is installed, started or stopped at import time. Preparing a project is a background
job; failure to find an Editor is actionable state, never a successful connection.
Only installer subprocesses are terminated on cancellation, never a user's Editor.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from typing import Any, Mapping

PROVIDER = "CoplayDev/unity-mcp"
VERSION = "10.3.0"
COMMIT = "aa5fc638d623f56178d50329d9ad8c541a57fe66"
ARCHIVE_URL = f"https://codeload.github.com/{PROVIDER}/zip/{COMMIT}"
ARCHIVE_SHA256 = "d2353d0604e250184ee5c6fbb14b49d381a555f129977b09f80779b8c8df9b96"
PACKAGE = "com.coplaydev.unity-mcp"
PROVIDER_ADAPTER_REVISION = "bread-background-scene-capture-v1"
AUTO_RECOVERY_GRACE_SECONDS = 2.0
UNITY_PREFLIGHT_TIMEOUT_SECONDS = 10.0
UV_VERSION = "0.9.5"
# The official wheel contains the same standalone executable and avoids an
# installer script or a dependency on pip/Python in packaged Bread builds.
UV_WINDOWS_URL = "https://files.pythonhosted.org/packages/af/14/0f07d0b2e561548b4e3006208480a5fce8cdaae5247d85efbfb56e8e596b/uv-0.9.5-py3-none-win_amd64.whl"
UV_WINDOWS_SHA256 = "48a12390421f91af8a8993cf15c38297c0bb121936046286e287975b2fbf1789"
# Version-pinned PyPI wheels: https://pypi.org/pypi/uv/0.9.5/json . Each wheel
# contains a standalone binary; frozen Bread needs neither pip nor system Python.
UV_WHEELS = {
    ("win32", "x86_64"): (UV_WINDOWS_URL, UV_WINDOWS_SHA256),
    ("win32", "arm64"): ("https://files.pythonhosted.org/packages/c7/33/14244c0641c2340653ae934e5c82750543fcddbcd260bdc2353a33b6148f/uv-0.9.5-py3-none-win_arm64.whl", "c966e3a4fe4de3b0a6279d0a835c79f9cddbb3693f52d140910cbbed177c5742"),
    ("darwin", "x86_64"): ("https://files.pythonhosted.org/packages/4e/6c/3508d67f80aac0ddb5806680a6735ff6cb5a14e9b697e5ae145b01050880/uv-0.9.5-py3-none-macosx_10_12_x86_64.whl", "922cd784cce36bbdc7754b590d28c276698c85791c18cd4c6a7e917db4480440"),
    ("darwin", "arm64"): ("https://files.pythonhosted.org/packages/b2/26/bd6438cf6d84a6b0b608bcbe9f353d8e424f8fe3b1b73a768984a76bf80b/uv-0.9.5-py3-none-macosx_11_0_arm64.whl", "8603bb902e578463c50c3ddd4ee376ba4172ccdf4979787f8948747d1bb0e18b"),
    ("linux", "x86_64"): ("https://files.pythonhosted.org/packages/9b/83/a0bdf4abf86ede79b427778fe27e2b4a022c98a7a8ea1745dcd6c6561f17/uv-0.9.5-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", "6507bbbcd788553ec4ad5a96fa19364dc0f58b023e31d79868773559a83ec181"),
    ("linux", "arm64"): ("https://files.pythonhosted.org/packages/48/8a/a990d9a39094d4d47bd11edff17573247f3791c33a19626e92c995498e68/uv-0.9.5-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64.whl", "48a3542835d37882ff57d1ff91b757085525d98756712fa61cf9941d3dda8ebf"),
}
SCHEMA = "bread-unity-integration/v1"
_VERSION_RE = re.compile(r"\b(?:20\d{2}|600\d)\.\d+\.\d+[abfp]\d+(?:c\d+)?\b")
_TERMINAL = frozenset(("completed", "partial", "failed", "cancelled", "action-required"))


class UnityIntegrationError(RuntimeError):
    def __init__(self, code: str, detail: str = "", **data: Any) -> None:
        super().__init__(detail or code)
        self.code, self.data = code, data


def _json(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise UnityIntegrationError("invalid-json-object", str(path))
    return result


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _project(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_dir() or not (candidate / "Assets").is_dir():
        raise UnityIntegrationError("invalid-unity-project", str(candidate))
    if not (candidate / "ProjectSettings/ProjectVersion.txt").is_file():
        raise UnityIntegrationError("unity-project-version-missing", str(candidate))
    if not (candidate / "Packages/manifest.json").is_file():
        raise UnityIntegrationError("unity-package-manifest-missing", str(candidate))
    return candidate


def _version(project: Path) -> str:
    match = re.search(r"^m_EditorVersion:\s*(\S+)", (project / "ProjectSettings/ProjectVersion.txt").read_text(encoding="utf-8-sig"), re.M)
    if not match or not _VERSION_RE.fullmatch(match.group(1)):
        raise UnityIntegrationError("invalid-unity-editor-version", str(project))
    return match.group(1)


def discover_editors() -> list[dict[str, str]]:
    """Read Hub metadata and standard installation roots, without starting Unity."""
    found: dict[str, dict[str, str]] = {}

    def add(path: Any, version: str = "") -> None:
        if not isinstance(path, str) or not path:
            return
        candidate = Path(path).expanduser()
        if candidate.is_dir():
            relative_paths = (("Unity.exe", "Editor/Unity.exe") if sys.platform == "win32" else
                              ("Contents/MacOS/Unity", "Unity.app/Contents/MacOS/Unity", "Unity") if sys.platform == "darwin" else
                              ("Unity", "Editor/Unity"))
            candidate = next((candidate / suffix for suffix in relative_paths if (candidate / suffix).is_file()), candidate / relative_paths[-1])
        if candidate.is_file():
            match = _VERSION_RE.search(str(candidate))
            actual_version = version or (match.group(0) if match else "")
            if actual_version:
                found[_key(candidate)] = {"path": str(candidate.resolve()), "version": actual_version}

    hub_roots = [Path(os.environ.get("APPDATA", "")) / "UnityHub", Path.home() / "Library/Application Support/UnityHub", Path.home() / ".config/UnityHub"]
    install_roots = [Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Unity/Hub/Editor", Path("/Applications/Unity/Hub/Editor"), Path.home() / "Unity/Hub/Editor",
                     Path.home() / "Applications/Unity/Hub/Editor", Path("/opt/unityhub/Editor"), Path("/opt/unity/Editor")]
    install_roots.extend(Path(value).expanduser() for value in os.environ.get("BREAD_UNITY_EDITOR_ROOTS", "").split(os.pathsep) if value)
    for root in hub_roots:
        for name in ("editors-v2.json", "editors.json"):
            try:
                value = json.loads((root / name).read_text(encoding="utf-8-sig"))
                rows = value.get("data", []) if isinstance(value, dict) else value
                if isinstance(rows, dict):
                    rows = list(rows.values())
                for item in rows if isinstance(rows, list) else []:
                    if not isinstance(item, dict):
                        continue
                    locations = item.get("location", item.get("path", []))
                    for location in locations if isinstance(locations, list) else [locations]:
                        add(location, str(item.get("version", "")))
            except (OSError, ValueError, TypeError):
                pass
        try:
            secondary = json.loads((root / "secondaryInstallPath.json").read_text(encoding="utf-8-sig"))
            if isinstance(secondary, str):
                install_roots.append(Path(secondary))
        except (OSError, ValueError):
            pass
    for root in install_roots:
        if root.is_dir():
            add(str(root))
            for child in root.iterdir():
                if child.is_dir():
                    add(str(child))
    return sorted(found.values(), key=lambda item: (item["version"], item["path"]))


def _select_editor(version: str, explicit: str | None) -> str | None:
    editors = discover_editors()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise UnityIntegrationError("unity-editor-not-found", str(path))
        matching = next((item for item in editors if _key(item["path"]) == _key(path)), None)
        actual = matching["version"] if matching else ""
        if not actual:
            # -version exits without opening a project. Never guess from a caller's
            # filename or silently upgrade a user's existing Unity project.
            result = subprocess.run([str(path), "-version"], capture_output=True, text=True, timeout=20, **_hidden())
            match = _VERSION_RE.search(result.stdout + result.stderr)
            actual = match.group(0) if match else ""
        if actual != version:
            raise UnityIntegrationError("unity-editor-version-mismatch", str(path), expected=version, actual=actual)
        return str(path)
    return next((item["path"] for item in editors if item["version"] == version), None)


def _hidden() -> dict[str, Any]:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != ".bread-package.json" and path.suffix != ".meta":
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _download(url: str, target: Path, sha256: str, cancel: threading.Event) -> None:
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == sha256:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name(target.name + "." + uuid.uuid4().hex + ".part")
    try:
        digest = hashlib.sha256()
        request = urllib.request.Request(url, headers={"User-Agent": "Bread-Unity-Integration"})
        with urllib.request.urlopen(request, timeout=30) as response, pending.open("wb") as output:
            while True:
                if cancel.is_set():
                    raise UnityIntegrationError("cancelled")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != sha256:
            raise UnityIntegrationError("download-checksum-mismatch", url, expected=sha256, actual=digest.hexdigest())
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)


def _extract_zip(archive: Path, output: Path) -> None:
    with zipfile.ZipFile(archive) as source:
        for item in source.infolist():
            relative = Path(item.filename.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts or ":" in item.filename or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise UnityIntegrationError("unsafe-package-archive", item.filename)
            if not (output / relative).resolve().is_relative_to(output.resolve()):
                raise UnityIntegrationError("unsafe-package-archive", item.filename)
        source.extractall(output)


@contextmanager
def _file_lock(path: Path, cancel: threading.Event, timeout: float = 900):
    """Serialize project/cache mutations across Bread Host processes as well."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            if cancel.is_set():
                raise UnityIntegrationError("cancelled")
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise UnityIntegrationError("unity-project-operation-busy", str(path))
                cancel.wait(0.2)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class _Job:
    id: str
    project: str
    status: str = "queued"
    stage: str = "queued"
    result: dict[str, Any] = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event)
    reuse_count: int = 0
    start_editor_requested: bool = True
    reconnect_requested: bool = False
    automatic_recovery: bool = False


class UnityIntegrationService:
    def __init__(self, cache_root: str | Path | None = None, package_root: str | Path | None = None) -> None:
        data = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or
                    (Path.home() / "Library/Application Support" if sys.platform == "darwin" else Path.home() / ".local/share"))
        self.cache_root = Path(cache_root or data / "ReplayLab/unity").expanduser().resolve()
        self.package_root = Path(package_root) if package_root else self._default_package_root()
        self._guard = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._active: dict[str, str] = {}
        self._async_locks: dict[str, asyncio.Lock] = {}
        self._instances: dict[str, dict[str, Any]] = {}
        self._tool_jobs: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._external_connections: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _default_package_root() -> Path:
        override = os.environ.get("BREAD_UNITY_PACKAGES_ROOT")
        if override:
            return Path(override).expanduser()
        if getattr(sys, "frozen", False):
            bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / "bread_unity"
            if bundled.is_dir():
                return bundled
            return Path(sys.executable).resolve().parent / "integrations/unity"
        return Path(__file__).resolve().parents[2] / "unity"

    def inspect_project(self, project_path: str, editor_path: str | None = None) -> dict[str, Any]:
        try:
            project = _project(project_path)
            version = _version(project)
            packages = _json(project / "Packages/manifest.json").get("dependencies", {})
            editor = _select_editor(version, editor_path)
            minimum = tuple(int(x) for x in version.split(".")[:2]) >= (2021, 3)
            state = "ready-to-prepare" if editor and minimum else "unity-editor-required" if not editor else "unity-version-unsupported"
            external = self._external_connections.get(_key(project))
            cached = bool(external) or self._known_connection(project)
            if external:
                state = "existing-mcp-available"
            return {"schema": SCHEMA, "status": state, "projectPath": str(project), "unityVersion": version,
                    "editorPath": editor, "urpVersion": packages.get("com.unity.render-pipelines.universal"),
                    # An installed package is not the active graphics/quality/camera pipeline.
                    "renderPipeline": "unverified",
                    "declaredRenderPipelines": {name: packages[package] for name, package in (
                        ("urp", "com.unity.render-pipelines.universal"),
                        ("hdrp", "com.unity.render-pipelines.high-definition")) if package in packages},
                    "renderingInspectionTool": "bread_inspect_rendering",
                    "managedImporter": {"pipeline": "preserve-existing", "scope": "manifested-draw-and-pass-implementation",
                        "multiPassRoute": "project-tools", "visualValidation": "task-bound-inspect-render-repair"},
                    "provider": {"repository": PROVIDER, "version": VERSION, "commit": COMMIT},
                    "installedEditors": discover_editors(), "connected": None if cached else False,
                    "connectionCached": cached, "existingMcp": {"connectionId": external["descriptor"]["id"],
                        "capabilities": sorted(external["capabilities"])} if external else None,
                    "connectionState": "cached-unverified" if cached else "not-verified",
                    "nextAction": {"code": "verify-existing-mcp", "tool": "call_unity_project_tool",
                        "toolName": external["identityTool"]} if external else None if editor and minimum else
                        {"code": state, "requiredVersion": version, "hubUri": f"unityhub://{version}"}}
        except (UnityIntegrationError, OSError, ValueError, subprocess.SubprocessError) as error:
            return self._error(error)

    @staticmethod
    def _error(error: Exception) -> dict[str, Any]:
        return {"schema": SCHEMA, "status": "failed", "code": getattr(error, "code", "unity-integration-error"),
                "detail": str(error), "errorType": type(error).__name__, **getattr(error, "data", {})}

    def prepare_project(self, project_path: str, editor_path: str | None = None, start_editor: bool = True,
                        timeout_seconds: float = 240, reconnect: bool = False,
                        automatic_recovery: bool = False) -> dict[str, Any]:
        """Return immediately; repeated preparations of one project share one job."""
        try:
            project = _project(project_path)
        except (UnityIntegrationError, OSError) as error:
            return self._error(error)
        key = _key(project)
        with self._guard:
            previous = self._jobs.get(self._active.get(key, ""))
            if previous and previous.status not in _TERMINAL:
                previous.reuse_count += 1
                previous.start_editor_requested = previous.start_editor_requested or start_editor
                return {**self.job_status(previous.id), "reused": True}
            if automatic_recovery and previous and previous.automatic_recovery and previous.status in _TERMINAL:
                # Preserve the diagnostic instead of starting another identical
                # installer on every tool call. Explicit preparation after an AI
                # repair remains available; this is not a project/import lock.
                return {**self.job_status(previous.id), "reused": True}
            job = _Job(uuid.uuid4().hex, str(project), start_editor_requested=start_editor,
                       reconnect_requested=reconnect, automatic_recovery=automatic_recovery)
            self._jobs[job.id] = job
            self._active[key] = job.id
            threading.Thread(target=self._prepare_worker, args=(job, editor_path, start_editor, max(1, min(float(timeout_seconds), 900))), daemon=True).start()
            return self.job_status(job.id)

    def job_status(self, job_id: str) -> dict[str, Any]:
        with self._guard:
            job = self._jobs.get(job_id)
            if not job:
                return {"schema": SCHEMA, "status": "failed", "code": "unity-job-not-found", "jobId": job_id}
            return {"schema": SCHEMA, "jobId": job.id, "projectPath": job.project, "status": job.status,
                    "stage": job.stage, "cancelRequested": job.cancel.is_set(), "result": dict(job.result)}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._guard:
            job = self._jobs.get(job_id)
            if job and job.status not in _TERMINAL:
                job.cancel.set()
            return self.job_status(job_id)

    async def start_import(self, project_path: str, request_path: str,
                           editor_path: str | None = None) -> dict[str, Any]:
        """Prepare and import using the same job/status API and Host event loop.

        The caller creates the immutable request below Library/Bread. A repeated
        request while running reuses the task; different requests serialize through
        the project lock. Cancelling never closes Unity or kills an in-flight import.
        """
        try:
            project = _project(project_path)
            request = Path(request_path).expanduser().resolve()
            if not request.is_file() or not request.is_relative_to((project / "Library/Bread").resolve()):
                raise UnityIntegrationError("unity-import-request-outside-project", str(request))
            request_digest = hashlib.sha256(request.read_bytes()).hexdigest()
            key = _key(project) + ":import:" + request_digest
            with self._guard:
                existing = self._jobs.get(self._tool_jobs.get(key, ""))
                if existing and existing.status not in _TERMINAL:
                    return {**self.job_status(existing.id), "reused": True}
                job = _Job(uuid.uuid4().hex, str(project))
                self._jobs[job.id] = job
                self._tool_jobs[key] = job.id
            task = asyncio.create_task(self._import_worker(job, request, request_digest, editor_path))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return self.job_status(job.id)
        except (OSError, ValueError, UnityIntegrationError) as error:
            return self._error(error)

    async def _import_worker(self, job: _Job, request: Path, digest: str, editor_path: str | None) -> None:
        preparation_id = ""
        owns_preparation = False
        try:
            self._stage(job, "prepare-unity")
            external = self._external_connections.get(_key(job.project))
            preparation = ({"status": "completed", "result": {"connected": True, "provider": "existing-mcp"}}
                           if external else self.prepare_project(job.project, editor_path))
            preparation_id = str(preparation.get("jobId", ""))
            owns_preparation = bool(preparation_id) and not preparation.get("reused", False)
            with self._guard:
                job.result = {"preparationJobId": preparation_id}
            while preparation.get("status") not in _TERMINAL:
                self._stage(job, "prepare-unity:" + str(preparation.get("stage", "queued")))
                await asyncio.sleep(0.25)
                preparation = self.job_status(preparation["jobId"])
            if preparation.get("status") != "completed" or not preparation.get("result", {}).get("connected"):
                with self._guard:
                    job.status = "action-required" if preparation.get("status") == "completed" else preparation.get("status", "failed")
                    job.result = preparation
                return
            self._stage(job, "import-capture")
            if hashlib.sha256(request.read_bytes()).hexdigest() != digest:
                raise UnityIntegrationError("unity-import-request-changed", str(request))
            expected = _json(request)
            policy = expected.get("pipelinePolicy") or {}
            if policy.get("allowCreateUrp") is True:
                if str(policy.get("targetPipeline") or "urp").lower() != "urp":
                    raise UnityIntegrationError("unity-pipeline-policy-conflict", "Creating URP requires an explicit URP target.")
                self._stage(job, "prepare-urp")
                urp = await self._poll_tool(job, "bread_prepare_urp", {"allow_pipeline_creation": True}, timeout=300)
                if urp.get("success") is False or self._tool_data(urp).get("status") != "ready":
                    with self._guard:
                        job.status, job.result = "action-required", urp
                    return
            self._stage(job, "import-capture")
            result = await self.call_tool(job.project, "bread_import_capture", {"request_path": str(request), "request_file_sha256": digest, "action": "import"})
            with self._guard:
                job.result = {"requestPath": str(request), "lastObservedResult": result}
            if (self._tool_data(result).get("status") in ("pending", "queued", "running")
                    or result.get("operationOutcome") == "unknown" and result.get("safeToRetry") is False):
                result = await self._poll_tool(job, "bread_import_capture", {"request_path": str(request), "request_file_sha256": digest, "action": "status"}, timeout=300, observe_started=True)
            elif self._retryable_connection_result(result):
                result = await self._poll_tool(job, "bread_import_capture", {"request_path": str(request), "request_file_sha256": digest, "action": "import"}, timeout=300, observe_started=True)
            # Generated Editor scripts can reload the domain between copying
            # implementation files and creating their assets. Resume only the
            # importer's explicit, same-request receipt; never restart extraction.
            deadline = time.monotonic() + 300
            while (self._tool_data(result).get("retrySameRequest") is True
                   and self._tool_data(result).get("code") == "implementation_compile_pending"):
                if job.cancel.is_set():
                    break
                if time.monotonic() >= deadline:
                    raise UnityIntegrationError("unity-operation-timeout", "implementation-compile", lastObservedResult=result)
                self._stage(job, "await-implementation-compile")
                await asyncio.sleep(1)
                if hashlib.sha256(request.read_bytes()).hexdigest() != digest:
                    raise UnityIntegrationError("unity-import-request-changed", str(request))
                result = await self._poll_tool(job, "bread_import_capture",
                    {"request_path": str(request), "request_file_sha256": digest, "action": "import"},
                    timeout=max(1, deadline - time.monotonic()), observe_started=True)
            with self._guard:
                # A cancel request during the Editor call is reported together with
                # its real outcome: do not conceal assets that were already written.
                data = self._tool_data(result)
                status = data.get("status")
                checks = data.get("checks")
                expected = _json(request)
                identity_matches = (bool(re.fullmatch(r"[a-f0-9]{64}", str(expected.get("requestSha256", ""))))
                    and bool(re.fullmatch(r"[a-f0-9]{64}", str(expected.get("packageManifestSha256", ""))))
                    and data.get("requestSha256") == expected.get("requestSha256")
                    and data.get("packageManifestSha256") == expected.get("packageManifestSha256")
                    and data.get("requestFileSha256") == digest)
                required_checks = ("meshImport", "shaderCompile", "materialImport", "textureImport", "sceneIntegrity", "pipelineCompatible")
                verified_import = (status == "imported" and identity_matches and isinstance(checks, Mapping)
                    and all(checks.get(name) is True for name in required_checks))
                job.status = "completed" if verified_import and result.get("success") is not False else "partial" if status == "partial" else "action-required" if status == "needs_project_decision" else "failed"
                if status in ("imported", "partial") and not identity_matches:
                    job.status = "failed"
                    result = {**result, "code": "unity-import-receipt-mismatch"}
                if status not in ("imported", "partial", "needs_project_decision", "failed"):
                    result = {**result, "code": "unity-import-incomplete", "reportedStatus": status}
                job.stage, job.result = "import-returned", result
        except Exception as error:
            with self._guard:
                preparation_job = self._jobs.get(preparation_id)
                if job.cancel.is_set() and owns_preparation and preparation_job and preparation_job.reuse_count == 0:
                    preparation_job.cancel.set()
                job.status = "cancelled" if job.cancel.is_set() else "failed"
                job.result = {**job.result, **self._error(error), "requestPath": str(request),
                              "preparationJobId": preparation_id}

    @staticmethod
    def _tool_data(result: Mapping[str, Any]) -> dict[str, Any]:
        data = result.get("data")
        return dict(data) if isinstance(data, Mapping) else dict(result)

    @staticmethod
    def _retryable_connection_result(result: Mapping[str, Any]) -> bool:
        if (result.get("connectionRecoveryNeeded") is False
                or result.get("code") in ("unity-connection-recovery-failed", "unity-operation-outcome-unknown")):
            return False
        return (result.get("code") in ("unity-project-not-connected", "unity-editor-busy", "unity-connection-recovering")
                or result.get("errorType") in ("McpToolError", "ConnectionError", "TimeoutError", "ConnectionResetError"))

    async def _poll_tool(self, job: _Job, name: str, arguments: Mapping[str, Any], timeout: float,
                         observe_started: bool = False) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if job.cancel.is_set() and not observe_started:
                raise UnityIntegrationError("cancelled")
            result = await self.call_tool(job.project, name, arguments)
            if result.get("operationOutcome") == "unknown" and result.get("safeToRetry") is False:
                if name == "bread_import_capture":
                    # Observe the existing request/receipt instead of issuing the
                    # import a second time after losing its reply.
                    arguments = {**arguments, "action": "status"}
                    await asyncio.sleep(1)
                    continue
                return result
            if observe_started:
                with self._guard:
                    job.result = {"requestPath": arguments.get("request_path"), "lastObservedResult": result}
            data = self._tool_data(result)
            if data.get("status") not in ("pending", "queued", "running") and not self._retryable_connection_result(data):
                return result
            await asyncio.sleep(1)
        raise UnityIntegrationError("unity-operation-timeout", name)

    def _stage(self, job: _Job, stage: str) -> None:
        if job.cancel.is_set():
            raise UnityIntegrationError("cancelled")
        with self._guard:
            job.status, job.stage = "running", stage

    def _prepare_worker(self, job: _Job, editor_path: str | None, start_editor: bool, timeout: float) -> None:
        try:
            with _file_lock(self._lock_path(Path(job.project)), job.cancel):
                # A live handshake proves identity, not that this application's
                # current importer is installed. Reuse only intact current Bread
                # packages; stale managed packages follow the normal safe upgrade.
                project = Path(job.project)
                if job.reconnect_requested:
                    self._instances.pop(_key(project), None)
                    settings = project / "Library/Bread/unity-connection.json"
                    if settings.is_file():
                        config = _json(settings)
                        if config.get("enabled") and config.get("nonce"):
                            # Keep project identity stable; a separate request
                            # wakes the Editor bridge even when it retained its nonce.
                            _write_json(settings, {**config, "reconnectRequest": uuid.uuid4().hex})
                executable = self._runtime_executable()
                if executable.is_file():
                    self._stage(job, "verify-existing-connection")
                    connection = self._probe(project)
                    package_diagnostic = None
                    try:
                        packages_current = bool(connection and self._bread_packages_current(project))
                        provider_marker = project / "Packages" / PACKAGE / ".bread-package.json"
                        if packages_current:
                            packages_current = (provider_marker.is_file() and
                                _json(provider_marker).get("breadAdapterRevision") == PROVIDER_ADAPTER_REVISION)
                    except UnityIntegrationError as error:
                        if not connection or error.code not in ("unity-package-conflict", "unity-package-modified"):
                            raise
                        # A live identified Editor remains usable with customized
                        # packages. Preserve those files and use its actual tools.
                        packages_current = False
                        package_diagnostic = {"code": error.code, "detail": str(error),
                            "diagnosticsOnly": True, "retainedInstalledPackages": True,
                            "bundledSdkInstalled": False}
                    if (connection and (packages_current or package_diagnostic)
                            and (project / "Packages" / PACKAGE / ".bread-package.json").is_file()
                            and (package_diagnostic or connection.get("packageRevision") == self._package_revision(project))):
                        with self._guard:
                            self._instances[_key(project)] = dict(connection)
                            job.status, job.stage = "completed", "connected"
                            job.result = {"schema": SCHEMA, "status": "connected", "connected": True,
                                "projectPath": str(project), "unityVersion": _version(project), "instance": connection,
                                "reused": True, "editorStarted": False, "runtimePath": str(executable),
                                "mcpConnection": self._connection(project, executable, connection)}
                            if package_diagnostic:
                                job.result["packageDiagnostics"] = package_diagnostic
                        return
                self._stage(job, "inspect-project")
                inspected = self.inspect_project(job.project, editor_path)
                if inspected["status"] != "ready-to-prepare":
                    with self._guard:
                        job.status = "failed" if inspected["status"] == "failed" else "action-required"
                        job.result = inspected
                    return
                self._stage(job, "prepare-runtime")
                with _file_lock(self.cache_root / "runtime.lock", job.cancel):
                    source = self._source(job)
                    executable = self._runtime(source, job)
                project = Path(job.project)
                self._stage(job, "install-packages")
                self._install_package(project, source / "MCPForUnity")
                for name in ("com.bread.unity-connection", "com.bread.renderdoc"):
                    package = self.package_root / name
                    if not package.is_dir():
                        raise UnityIntegrationError("bundled-unity-package-missing", str(package))
                    self._install_package(project, package)
                settings = project / "Library/Bread/unity-connection.json"
                config = _json(settings) if settings.is_file() else {}
                revision = self._package_revision(project)
                nonce = config.get("nonce") if config.get("packageRevision") == revision else None
                config.update({"enabled": True, "nonce": nonce or uuid.uuid4().hex, "providerCommit": COMMIT,
                               "packageRevision": revision})
                _write_json(settings, config)
                self._stage(job, "connect-editor")
                connection = self._probe(project)
                started = False
                if not connection and job.start_editor_requested and not self._project_locked(project):
                    self._start_editor(project, str(inspected["editorPath"]))
                    started = True
                elif not connection and not job.start_editor_requested:
                    with self._guard:
                        job.status, job.stage = "completed", "packages-ready"
                        job.result = {**inspected, "status": "prepared", "connected": False, "runtimePath": str(executable)}
                    return
                deadline = time.monotonic() + timeout
                while not connection and time.monotonic() < deadline:
                    self._stage(job, "await-editor-connection")
                    job.cancel.wait(1)
                    connection = self._probe(project)
                if not connection:
                    raise UnityIntegrationError("unity-connection-not-ready", "Editor compilation, package resolution, licensing or project errors may require attention.",
                        projectPath=str(project), logPath=str(project / "Library/Bread/editor.log"), editorStarted=started)
                with self._guard:
                    self._instances[_key(project)] = dict(connection)
                    job.status, job.stage = "completed", "connected"
                    job.result = {**inspected, "status": "connected", "connected": True, "instance": connection,
                                  "runtimePath": str(executable), "editorStarted": started,
                                  "mcpConnection": self._connection(project, executable, connection)}
        except Exception as error:
            with self._guard:
                job.status = "cancelled" if job.cancel.is_set() or getattr(error, "code", "") == "cancelled" else "action-required" if getattr(error, "code", "") in ("unity-connection-not-ready", "unity-package-conflict", "unity-package-modified", "unity-runtime-unavailable") else "failed"
                job.result = self._error(error)

    def _lock_path(self, project: Path) -> Path:
        return self.cache_root / "locks" / (hashlib.sha256(_key(project).encode()).hexdigest() + ".lock")

    def _source(self, job: _Job) -> Path:
        bundled = self._bundled_provider()
        if bundled is not None:
            return bundled
        archive = self.cache_root / "downloads" / f"unity-mcp-{COMMIT}.zip"
        _download(ARCHIVE_URL, archive, ARCHIVE_SHA256, job.cancel)
        target = self.cache_root / "sources" / COMMIT
        if not (target / "Server/uv.lock").is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="bread-unity-source-", dir=target.parent) as scratch:
                stage = Path(scratch)
                _extract_zip(archive, stage)
                unpacked = stage / f"unity-mcp-{COMMIT}"
                if _json(unpacked / "MCPForUnity/package.json").get("version") != VERSION:
                    raise UnityIntegrationError("unity-provider-version-mismatch")
                if target.exists():
                    raise UnityIntegrationError("unity-provider-cache-incomplete", str(target))
                os.replace(unpacked, target)
        return target

    def _runtime(self, source: Path, job: _Job) -> Path:
        bundled = self._bundled_provider()
        if bundled is not None:
            return bundled / "runtime/python.exe"
        root = self.cache_root / "runtime" / COMMIT
        executable = self._runtime_executable()
        marker = root / ".bread-runtime.json"
        if executable.is_file() and marker.is_file() and _json(marker).get("commit") == COMMIT:
            return executable
        uv = shutil.which("uv")
        if not uv:
            architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
            wheel = UV_WHEELS.get((sys.platform, architecture))
            if not wheel:
                raise UnityIntegrationError("unity-runtime-unavailable", "Install uv for this platform or configure an existing Unity MCP.", platform=sys.platform, architecture=architecture)
            archive = self.cache_root / "downloads" / wheel[0].rsplit("/", 1)[-1]
            _download(wheel[0], archive, wheel[1], job.cancel)
            tools = self.cache_root / "tools" / f"uv-{UV_VERSION}-{sys.platform}-{architecture}"
            binary = tools / (f"uv-{UV_VERSION}.data/scripts/uv.exe" if sys.platform == "win32" else f"uv-{UV_VERSION}.data/scripts/uv")
            if not binary.is_file():
                tools.mkdir(parents=True, exist_ok=True)
                _extract_zip(archive, tools)
            if not binary.is_file():
                raise UnityIntegrationError("unity-runtime-unavailable", "The verified uv wheel does not contain its expected executable.")
            if sys.platform != "win32":
                binary.chmod(binary.stat().st_mode | 0o700)
            uv = str(binary)
        environment = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(root), UV_CACHE_DIR=str(self.cache_root / "uv-cache"),
                           UV_PYTHON_INSTALL_DIR=str(self.cache_root / "python"), UV_PYTHON_DOWNLOADS="automatic")
        self._run([uv, "sync", "--frozen", "--no-dev", "--no-editable", "--python", "3.12", "--project", str(source / "Server")],
                  environment, job, self.cache_root / "logs/runtime-install.log", 900)
        if not executable.is_file():
            raise UnityIntegrationError("unity-runtime-install-incomplete", str(executable))
        _write_json(marker, {"commit": COMMIT, "version": VERSION, "archiveSha256": ARCHIVE_SHA256})
        return executable

    def _runtime_executable(self) -> Path:
        bundled = self._bundled_provider()
        if bundled is not None:
            return bundled / "runtime/python.exe"
        return self.cache_root / "runtime" / COMMIT / ("Scripts/mcp-for-unity.exe" if sys.platform == "win32" else "bin/mcp-for-unity")

    @staticmethod
    def _bundled_provider() -> Path | None:
        if not getattr(sys, "frozen", False):
            return None
        root = Path(sys.executable).resolve().parent / "tools/unity-mcp"
        marker = root / "bread-unity-provider.json"
        if not marker.is_file():
            if (root.parents[2] / "RenderDocBread.portable").is_file():
                raise UnityIntegrationError("bundled-unity-provider-missing", str(root))
            # Old development installations retain their explicit on-demand setup path.
            return None
        value = _json(marker)
        if (value.get("commit") != COMMIT or value.get("archiveSha256") != ARCHIVE_SHA256 or
                not (root / "MCPForUnity/package.json").is_file() or
                not (root / "runtime/python.exe").is_file()):
            raise UnityIntegrationError("bundled-unity-provider-invalid", str(root))
        return root

    @staticmethod
    def _run(argv: list[str], environment: dict[str, str], job: _Job, log: Path, timeout: float) -> None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as output:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output, env=environment, **_hidden())
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if job.cancel.wait(0.2) or time.monotonic() >= deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    raise UnityIntegrationError("cancelled" if job.cancel.is_set() else "unity-install-timeout", str(log))
            if process.returncode:
                raise UnityIntegrationError("unity-runtime-install-failed", str(log), exitCode=process.returncode)

    @staticmethod
    def _package_state(project: Path, source: Path) -> tuple[dict, Path, str, bool]:
        """Validate ownership/content without mutating an installed package."""
        metadata = _json(source / "package.json")
        name = str(metadata.get("name", ""))
        if not re.fullmatch(r"com\.[a-z0-9.-]+", name):
            raise UnityIntegrationError("invalid-unity-package-name", name)
        destination = project / "Packages" / name
        if not destination.resolve().is_relative_to((project / "Packages").resolve()):
            raise UnityIntegrationError("unity-package-path-escape", str(destination))
        dependency = _json(project / "Packages/manifest.json").get("dependencies", {}).get(name)
        marker = destination / ".bread-package.json"
        installed = _json(marker) if marker.is_file() else {}
        managed_previous_version = installed.get("version") if installed.get("name") == name else None
        managed_versions = installed.get("previousManagedVersions", []) if installed.get("name") == name else []
        if not isinstance(managed_versions, list):
            managed_versions = []
        if dependency and dependency not in (metadata["version"], managed_previous_version, *managed_versions, f"https://github.com/{PROVIDER}.git?path=/MCPForUnity#{COMMIT}", f"https://github.com/{PROVIDER}.git?path=/MCPForUnity#v{VERSION}"):
            raise UnityIntegrationError("unity-package-conflict", name, existing=dependency)
        digest = _tree_digest(source)
        marker = destination / ".bread-package.json"
        if destination.exists():
            if not marker.is_file():
                if _tree_digest(destination) != digest:
                    raise UnityIntegrationError("unity-package-conflict", str(destination))
            else:
                old = _json(marker)
                if _tree_digest(destination) != old.get("contentSha256"):
                    raise UnityIntegrationError("unity-package-modified", str(destination))
                adapter_current = name != PACKAGE or old.get("breadAdapterRevision") == PROVIDER_ADAPTER_REVISION
                if old.get("sourceSha256") == digest and adapter_current and (name == PACKAGE or old.get("contentSha256") == digest):
                    return metadata, destination, digest, True
        return metadata, destination, digest, False

    def _bread_packages_current(self, project: Path) -> bool:
        current = True
        for name in ("com.bread.unity-connection", "com.bread.renderdoc"):
            source = self.package_root / name
            if not source.is_dir():
                raise UnityIntegrationError("bundled-unity-package-missing", str(source))
            metadata, destination, digest, matches = self._package_state(project, source)
            if metadata.get("name") != name:
                raise UnityIntegrationError("invalid-unity-package-name", str(source))
            # These first-party packages are copied without provider notices or
            # other generated content. The actual installed files and both
            # provenance hashes must match today's bundled source. Keep checking
            # both packages even after a stale result to detect user edits before
            # starting any upgrade or download.
            marker = destination / ".bread-package.json"
            current = bool(matches and marker.is_file() and
                           _json(marker).get("contentSha256") == digest) and current
        return current

    @staticmethod
    def _package_revision(project: Path) -> str:
        revisions = [_json(project / "Packages" / name / ".bread-package.json").get("contentSha256")
                     for name in (PACKAGE, "com.bread.unity-connection", "com.bread.renderdoc")]
        return hashlib.sha256(json.dumps(revisions).encode()).hexdigest()

    @staticmethod
    def _install_package(project: Path, source: Path) -> None:
        metadata, destination, digest, current = UnityIntegrationService._package_state(project, source)
        if current:
            return
        name = metadata["name"]
        # Complete the copy outside Packages so Unity never compiles half a package.
        staging_root = project / "Library/Bread/package-staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="install-", dir=staging_root) as scratch:
            staging = Path(scratch) / name
            shutil.copytree(source, staging)
            # Existing MonoScript/asset GUIDs are project data. Dropping these
            # when upgrading an embedded package breaks saved scene references.
            if destination.is_dir():
                for previous_meta in destination.rglob("*.meta"):
                    relative = previous_meta.relative_to(destination)
                    asset = staging / relative.with_suffix("")
                    if (asset.exists() and not previous_meta.is_symlink() and
                            previous_meta.resolve().is_relative_to(destination.resolve())):
                        shutil.copyfile(previous_meta, staging / relative)
            provenance: dict[str, Any] = {"name": name, "version": metadata["version"], "sourceSha256": digest}
            previous_marker = destination / ".bread-package.json"
            previous = _json(previous_marker) if previous_marker.is_file() else {}
            if previous.get("name") == name:
                versions = previous.get("previousManagedVersions", [])
                versions = versions if isinstance(versions, list) else []
                provenance["previousManagedVersions"] = sorted({value for value in
                    [*versions, previous.get("version")] if isinstance(value, str)})[-32:]
            if name == PACKAGE:
                # Coplay's Scene screenshot helper focuses Unity before every
                # capture. Repaint the existing view without stealing focus.
                # Adapt only our staged managed copy; preserve source archives
                # and reject user-edited installations through _package_state.
                helper = staging / "Editor/Helpers/EditorWindowScreenshotUtility.cs"
                if helper.is_file():
                    text = helper.read_text(encoding="utf-8-sig")
                    text = text.replace("sceneView.Focus();",
                        "EditorApplication.QueuePlayerLoopUpdate(); // Bread: repaint without activating Unity.")
                    helper.write_text(text, encoding="utf-8")
                scene_tools = staging / "Editor/Tools/ManageScene.cs"
                if scene_tools.is_file():
                    text = scene_tools.read_text(encoding="utf-8-sig")
                    begin = text.find("        private static void EnsureGameView()")
                    end = text.find("        private static void ScheduleAssetImportWhenFileExists(", begin)
                    if begin >= 0 and end > begin:
                        text = text[:begin] + '''        private static void EnsureGameView()
        {
            // Bread: screenshots repaint existing views, without opening or
            // activating an EditorWindow. Prefer camera-target readback when
            // no matching view exists; do not move the user's desktop focus.
            var type = Type.GetType("UnityEditor.GameView,UnityEditor");
            if (type != null)
                foreach (var item in UnityEngine.Resources.FindObjectsOfTypeAll(type))
                    if (item is EditorWindow window) window.Repaint();
            SceneView.RepaintAll();
            EditorApplication.QueuePlayerLoopUpdate();
        }

''' + text[end:]
                        scene_tools.write_text(text, encoding="utf-8")
                license_path = source.parent / "LICENSE"
                if not license_path.is_file():
                    raise UnityIntegrationError("unity-provider-license-missing", str(license_path))
                shutil.copyfile(license_path, staging / "LICENSE.CoplayDev.txt")
                provenance.update({"repository": f"https://github.com/{PROVIDER}", "commit": COMMIT,
                                   "archiveSha256": ARCHIVE_SHA256, "breadAdapterRevision": PROVIDER_ADAPTER_REVISION})
                (staging / "NOTICE.Bread.txt").write_text(
                    f"MCP for Unity {VERSION}\nhttps://github.com/{PROVIDER}\nCommit: {COMMIT}\n"
                    "Distributed with its MIT license by Bread.\n"
                    "Bread adaptation: Scene/Game screenshots repaint existing views without opening or focusing windows.\n", encoding="utf-8")
            provenance["contentSha256"] = _tree_digest(staging)
            _write_json(staging / ".bread-package.json", provenance)
            backup = Path(scratch) / "previous"
            if destination.exists():
                os.replace(destination, backup)
            try:
                os.replace(staging, destination)
            except Exception:
                if backup.exists():
                    os.replace(backup, destination)
                raise

    @staticmethod
    def _project_locked(project: Path) -> bool:
        path = project / "Temp/UnityLockfile"
        if not path.exists():
            return False
        # Unity holds this file without sharing on Windows; do not launch another
        # editor when the existing owner has not completed compilation yet.
        try:
            with path.open("r+b"):
                pass
            return os.name != "nt"  # conservative on Unix; lockfile may be stale
        except OSError:
            return True

    @staticmethod
    def _start_editor(project: Path, executable: str) -> None:
        log = project / "Library/Bread/editor.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        # Open the normal Editor so users can inspect and edit the same project
        # that Bread controls. The project-local opt-in starts MCP in either mode.
        # Keep a graphics device: URP comparison renders cannot use -nographics.
        argv = [executable, "-projectPath", str(project), "-logFile", str(log)]
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 4  # SW_SHOWNOACTIVATE: visible graphical Editor, no focus request.
            options["startupinfo"] = startup
        elif sys.platform == "darwin" and ".app/Contents/MacOS/" in executable:
            bundle = executable.split(".app/Contents/MacOS/", 1)[0] + ".app"
            argv = ["/usr/bin/open", "-g", "-a", bundle, "--args", *argv[1:]]
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **options)

    def _probe(self, project: Path) -> dict[str, Any] | None:
        # A fresh, previously verified connection must keep its persistent TCP
        # session. A competing identity socket would disconnect the AI's client.
        # Actual tool calls still verify identity through that shared MCP session.
        if self._known_connection(project):
            return dict(self._instances[_key(project)])
        try:
            config = _json(project / "Library/Bread/unity-connection.json")
            if not config.get("enabled") or not config.get("nonce"):
                return None
        except (OSError, ValueError, UnityIntegrationError):
            return None
        directory = Path(os.environ.get("UNITY_MCP_STATUS_DIR") or Path.home() / ".unity-mcp")
        for status_file in directory.glob("unity-mcp-status-*.json"):
            try:
                status = _json(status_file)
                path = Path(status.get("project_path", ""))
                if path.name.lower() == "assets":
                    path = path.parent
                if _key(path) != _key(project) or status.get("reloading"):
                    continue
                port = int(status.get("unity_port", 0))
                if not 1 <= port <= 65535:
                    continue
                identity = self._bridge_identity(port)
                data = identity.get("data") or {}
                if not identity.get("success") or _key(data.get("projectPath", "")) != _key(project) or data.get("nonce") != config["nonce"] or data.get("unityVersion") != _version(project) or data.get("packageRevision") != config.get("packageRevision"):
                    continue
                if data.get("isCompiling") or data.get("isUpdating"):
                    continue
                if config.get("reconnectRequest") and data.get("reconnectRequest") != config["reconnectRequest"]:
                    # The old live bridge can answer before the Editor handles
                    # the restart request. Do not resume a write into that gap.
                    continue
                project_hash = status_file.stem.removeprefix("unity-mcp-status-")
                if not re.fullmatch(r"[a-f0-9]{8}", project_hash):
                    continue
                return {"id": f"{status.get('project_name', project.name)}@{project_hash}", "hash": project_hash,
                        "port": port, **data}
            except (OSError, ValueError, UnityIntegrationError):
                continue
        return None

    def _known_connection(self, project: Path) -> bool:
        """Inspect cached identity without opening a competing Coplay TCP socket."""
        known = self._instances.get(_key(project))
        if not known:
            return False
        try:
            config = _json(project / "Library/Bread/unity-connection.json")
            directory = Path(os.environ.get("UNITY_MCP_STATUS_DIR") or Path.home() / ".unity-mcp")
            status_path = directory / ("unity-mcp-status-" + str(known.get("hash", "")) + ".json")
            status = _json(status_path)
            return bool(config.get("enabled") and config.get("nonce") == known.get("nonce")
                        and config.get("packageRevision") == known.get("packageRevision")
                        and (not config.get("reconnectRequest") or config["reconnectRequest"] == known.get("reconnectRequest"))
                        and (not known.get("port") or status.get("unity_port") == known["port"])
                        and (not status.get("project_path") or _key(Path(status["project_path"]).parent if Path(status["project_path"]).name.lower() == "assets" else status["project_path"]) == _key(project))
                        and not status.get("reloading") and time.time() - status_path.stat().st_mtime < 60)
        except (OSError, ValueError, UnityIntegrationError):
            return False

    @staticmethod
    def _bridge_identity(port: int) -> dict[str, Any]:
        """One bounded read-only handshake before starting the official MCP server.

        Coplay v10.3.0 StdioBridgeHost uses an ASCII greeting and unsigned 64-bit
        big-endian frame length. All actual user tools go through the MCP client.
        """
        with socket.create_connection(("127.0.0.1", port), timeout=1) as connection:
            connection.settimeout(2)

            def exact(length: int) -> bytes:
                value = bytearray()
                while len(value) < length:
                    chunk = connection.recv(length - len(value))
                    if not chunk:
                        raise UnityIntegrationError("unity-connection-closed")
                    value.extend(chunk)
                return bytes(value)

            greeting = bytearray()
            while len(greeting) < 512 and not greeting.endswith(b"\n"):
                greeting.extend(exact(1))
            if b"WELCOME UNITY-MCP 1 FRAMING=1" not in greeting:
                raise UnityIntegrationError("unity-bridge-handshake-mismatch")
            request = json.dumps({"type": "bread_connection_identity", "params": {}}).encode()
            connection.sendall(struct.pack(">Q", len(request)) + request)
            length = struct.unpack(">Q", exact(8))[0]
            if length > 1024 * 1024:
                raise UnityIntegrationError("unity-identity-response-too-large")
            result = json.loads(exact(length))
            if not isinstance(result, dict):
                raise UnityIntegrationError("unity-invalid-identity-response")
            # TCP wraps tool return values in {status, result}.
            if result.get("status") == "success" and isinstance(result.get("result"), dict):
                result = result["result"]
            return result

    @staticmethod
    def _connection(project: Path, executable: Path, instance: Mapping[str, Any]) -> dict[str, Any]:
        prefix = (["-I", str(executable.parent / "bread_unity_entry.py")]
                  if (executable.parent / "bread_unity_entry.py").is_file() else [])
        return {"id": "bread-unity-" + hashlib.sha256(_key(project).encode()).hexdigest()[:16],
                "connection": {"kind": "mcp-stdio", "command": str(executable),
                    "args": prefix + ["--transport", "stdio", "--default-instance", str(instance["hash"]), "--project-scoped-tools"],
                    "env": {"UNITY_MCP_DISABLE_TELEMETRY": "true", "DISABLE_TELEMETRY": "true",
                            "FASTMCP_CHECK_FOR_UPDATES": "off", "FASTMCP_SHOW_SERVER_BANNER": "false",
                            "UNITY_MCP_LOG_DIR": str(project / "Library/Bread/mcp-logs")}}}

    async def connect_existing_mcp(self, project_path: str, connection: Mapping[str, Any],
                                   identity_tool: str = "bread_connection_identity",
                                   identity_arguments: Mapping[str, Any] | None = None,
                                   required_tools: list[str] | None = None) -> dict[str, Any]:
        """Verify a supplied MCP endpoint without installing or launching Unity.

        Provider adapters must expose a read-only identity tool returning the
        actual ``projectPath`` and ``unityVersion`` (directly or inside ``data``).
        Tool names come from tools/list; custom Bread names may additionally be
        advertised by that verified identity under ``capabilities``. No claim is
        made that arbitrary MCP servers implement Bread's managed importer.
        """
        from .mcp_tool_client import shared_client, validate_http_endpoint
        descriptor: dict[str, Any] | None = None
        try:
            project = _project(project_path)
            key = _key(project)
            value = dict(connection)
            kind = value.get("kind")
            if kind == "mcp-http":
                validate_http_endpoint(str(value.get("url", "")))
            elif kind == "mcp-stdio":
                command = str(value.get("command", ""))
                executable = shutil.which(command) or (command if Path(command).is_file() else "")
                if not executable or not isinstance(value.get("args", []), list):
                    raise UnityIntegrationError("unity-mcp-command-invalid")
                if not all(isinstance(item, str) and "\0" not in item for item in value.get("args", [])):
                    raise UnityIntegrationError("unity-mcp-command-invalid")
                value["command"] = str(Path(executable).resolve())
            else:
                raise UnityIntegrationError("unity-mcp-transport-unsupported", "Use MCP stdio or streamable HTTP; provider-internal WebSocket endpoints are not MCP endpoints.")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", identity_tool):
                raise UnityIntegrationError("unity-identity-tool-invalid")
            descriptor = {"id": "bread-unity-external-" + hashlib.sha256(key.encode()).hexdigest()[:16], "connection": value}
            async with self._async_locks.setdefault(key, asyncio.Lock()):
                client = shared_client()
                listed = await client.list_tools(descriptor)
                names = {str(item.get("name", "")) for item in listed}
                custom_identity = identity_tool.startswith("bread_") and identity_tool not in names and "execute_custom_tool" in names
                if identity_tool not in names and not custom_identity:
                    raise UnityIntegrationError("unity-identity-tool-unavailable", identity_tool)
                adapter = {"descriptor": descriptor, "identityTool": identity_tool,
                    "identityArguments": dict(identity_arguments or {}), "customIdentity": custom_identity,
                    "tools": names, "capabilities": set(), "projectPath": str(project)}
                identity = await self._verify_external(project, adapter, client)
                capabilities = identity.get("capabilities") or []
                if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
                    raise UnityIntegrationError("unity-capability-handshake-invalid")
                adapter["capabilities"] = names | set(capabilities)
                missing = sorted(set(required_tools or ()) - adapter["capabilities"])
                if missing:
                    raise UnityIntegrationError("unity-required-tools-unavailable", missingTools=missing)
                rendering: dict[str, Any] = {}
                if "bread_inspect_rendering" in adapter["capabilities"]:
                    rendering = self._tool_data(await self._external_tool(client, adapter, "bread_inspect_rendering", {}))
                    if rendering.get("projectPath") and _key(rendering["projectPath"]) != key:
                        raise UnityIntegrationError("unity-project-identity-mismatch")
                self._external_connections[key] = adapter
                return {"schema": SCHEMA, "status": "connected", "connected": True,
                    "provider": "existing-mcp", "connectionId": descriptor["id"], "transport": kind,
                    "projectPath": str(project), "unityVersion": identity["unityVersion"],
                    "capabilities": sorted(adapter["capabilities"]), "rendering": rendering,
                    "renderPipeline": rendering.get("renderPipeline", identity.get("renderPipeline", "unverified")),
                    "identityVerified": True, "editorStarted": False, "packagesModified": False,
                    "managedImportAvailable": "bread_import_capture" in adapter["capabilities"]}
        except Exception as error:
            self._external_connections.pop(_key(project_path), None)
            if descriptor:
                await shared_client().close(descriptor["id"])
            # Transport exceptions may embed bearer-bearing URLs or headers.
            return {"schema": SCHEMA, "status": "failed", "connected": False,
                "code": getattr(error, "code", "unity-mcp-handshake-failed"),
                "detail": str(error) if isinstance(error, UnityIntegrationError) else type(error).__name__,
                **getattr(error, "data", {})}

    async def _verify_external(self, project: Path, adapter: Mapping[str, Any], client: Any) -> dict[str, Any]:
        name, arguments = adapter["identityTool"], adapter["identityArguments"]
        if adapter["customIdentity"]:
            result = await client.call(adapter["descriptor"], "execute_custom_tool", {"tool_name": name, "parameters": arguments})
        else:
            result = await client.call(adapter["descriptor"], name, arguments)
        data = self._tool_data(result)
        if (result.get("success") is False or not data.get("projectPath")
                or _key(data["projectPath"]) != _key(project) or data.get("unityVersion") != _version(project)):
            raise UnityIntegrationError("unity-project-identity-mismatch")
        if data.get("isCompiling") or data.get("isUpdating"):
            raise UnityIntegrationError("unity-editor-busy")
        return data

    @staticmethod
    async def _external_tool(client: Any, adapter: Mapping[str, Any], name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name in adapter["tools"]:
            return await client.call(adapter["descriptor"], name, arguments)
        if name.startswith("bread_") and name in adapter["capabilities"] and "execute_custom_tool" in adapter["tools"]:
            return await client.call(adapter["descriptor"], "execute_custom_tool", {"tool_name": name, "parameters": dict(arguments)})
        raise UnityIntegrationError("unity-tool-unavailable", name)

    async def call_tool(self, project_path: str, tool_name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Recover connection prerequisites without replaying an uncertain mutation."""
        from .mcp_tool_client import shared_client
        with self._guard:
            preparing = self._jobs.get(self._active.get(_key(project_path), ""))
            if preparing and preparing.status not in _TERMINAL:
                preparing.reuse_count += 1
                # The installer owns the project while packages/Editor reload.
                # Report its progress rather than making another call wait on
                # the same project's file lock (or start another installer).
                return {"schema": SCHEMA, "status": "pending", "code": "unity-connection-recovering",
                        "connectionRecoveryNeeded": True, "operationOutcome": "not-started", "safeToRetry": True,
                        "jobId": preparing.id, "recovery": self.job_status(preparing.id),
                        "nextAction": {"code": "poll-unity-connection", "tool": "get_unity_integration_job"}}
        result = await self._call_connected_tool(project_path, tool_name, arguments)
        if not result.get("connectionRecoveryNeeded"):
            self._observe_healthy_connection(project_path)
            return result
        project = _project(project_path)
        key = _key(project)
        external = self._external_connections.get(key)
        connection_id = external["descriptor"]["id"] if external else "bread-unity-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        await shared_client().close(connection_id)
        uncertain = result.get("operationOutcome") == "unknown"
        can_retry = not uncertain or self._read_only_tool(tool_name, arguments or {})
        if external:
            try:
                await asyncio.wait_for(self._verify_external(project, external, shared_client()), UNITY_PREFLIGHT_TIMEOUT_SECONDS)
                if can_retry:
                    return await self._resume_connected_tool(project_path, tool_name, arguments, {"provider": "existing-mcp"})
                return {**result, "connectionRestored": True, "safeToRetry": False,
                        "nextAction": {"code": "inspect-operation-outcome", "tool": "call_unity_project_tool"}}
            except Exception as error:
                return {**result, "recovery": {"status": "failed", "code": "unity-external-reconnect-failed", "errorType": type(error).__name__},
                        "nextAction": {"code": "repair-existing-mcp", "tool": "connect_unity_project_mcp"}}
        preparation = self.prepare_project(str(project), reconnect=True, automatic_recovery=True)
        owns_preparation = not preparation.get("reused")
        # Short reloads can recover within this call. Long installs retain one
        # observable background job rather than tying up an MCP request indefinitely.
        deadline = time.monotonic() + AUTO_RECOVERY_GRACE_SECONDS
        try:
            while preparation.get("status") not in _TERMINAL and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                preparation = self.job_status(preparation["jobId"])
        except asyncio.CancelledError:
            if preparation.get("jobId") and owns_preparation:
                with self._guard:
                    active = self._jobs.get(preparation["jobId"])
                    if active is None or active.reuse_count == 0:
                        self.cancel_job(preparation["jobId"])
            raise
        connected = preparation.get("status") == "completed" and preparation.get("result", {}).get("connected")
        if connected and can_retry:
            return await self._resume_connected_tool(project_path, tool_name, arguments, preparation)
        pending = preparation.get("status") not in _TERMINAL
        return {**result, "status": "pending" if pending else "failed",
                "code": "unity-connection-recovering" if pending else "unity-operation-outcome-unknown" if connected else "unity-connection-recovery-failed",
                "operationOutcome": "unknown" if uncertain else "not-started", "safeToRetry": can_retry,
                "jobId": preparation.get("jobId"), "recovery": preparation, "connectionRestored": bool(connected),
                "nextAction": {"code": "inspect-operation-outcome" if connected and uncertain else "poll-unity-connection" if pending else "inspect-connection-diagnostics",
                               "tool": "call_unity_project_tool" if connected else "get_unity_integration_job"}}

    def _observe_healthy_connection(self, project_path: str) -> None:
        with self._guard:
            previous = self._jobs.get(self._active.get(_key(project_path), ""))
            if previous and previous.status == "completed":
                previous.automatic_recovery = False

    async def _resume_connected_tool(self, project_path: str, name: str, arguments: Mapping[str, Any] | None,
                                     preparation: Mapping[str, Any]) -> dict[str, Any]:
        result = await self._call_connected_tool(project_path, name, arguments)
        if result.get("connectionRecoveryNeeded"):
            return {**result, "code": "unity-connection-recovery-failed", "recovery": dict(preparation),
                    "jobId": preparation.get("jobId"),
                    "safeToRetry": result.get("operationOutcome") != "unknown" or self._read_only_tool(name, arguments or {}),
                    "nextAction": {"code": "inspect-connection-diagnostics", "tool": "connect_unity_project_mcp" if preparation.get("provider") == "existing-mcp" else "get_unity_integration_job"}}
        self._observe_healthy_connection(project_path)
        return result

    @staticmethod
    def _read_only_tool(name: str, arguments: Mapping[str, Any]) -> bool:
        return (name == "bread_connection_identity" or name.startswith(("bread_inspect_", "bread_get_"))
                or name == "read_console" or name in ("manage_material", "manage_asset", "manage_gameobject", "manage_scene")
                and arguments.get("action") in ("inspect", "get", "get_properties", "list", "get_hierarchy")
                or name == "bread_import_capture" and arguments.get("action") == "status")

    async def _call_connected_tool(self, project_path: str, tool_name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Call Coplay through the existing MCP session pool, with exact project identity."""
        from .mcp_tool_client import shared_client
        dispatched = False
        try:
            project = _project(project_path)
            lock = self._async_locks.setdefault(_key(project), asyncio.Lock())
            async with lock:
                # Serialize even across multiple Bread processes. The file lock is
                # acquired off the Host event loop and held through the tool call.
                lock_cancel = threading.Event()
                file_lock = _file_lock(self._lock_path(project), lock_cancel, timeout=60)
                acquiring = asyncio.create_task(asyncio.to_thread(file_lock.__enter__))
                try:
                    await asyncio.shield(acquiring)
                except asyncio.CancelledError:
                    lock_cancel.set()
                    try:
                        await asyncio.shield(acquiring)
                    except Exception:
                        pass
                    else:
                        await asyncio.to_thread(file_lock.__exit__, None, None, None)
                    raise
                try:
                    external = self._external_connections.get(_key(project))
                    if external:
                        client = shared_client()
                        await asyncio.wait_for(self._verify_external(project, external, client), UNITY_PREFLIGHT_TIMEOUT_SECONDS)
                        scoped_arguments = dict(arguments or {})
                        commands = scoped_arguments.get("commands", []) if tool_name == "batch_execute" else []
                        if (tool_name == "set_active_instance" or any(name in scoped_arguments for name in ("unity_instance", "unityInstance"))
                                or any(isinstance(command, Mapping) and (command.get("tool") == "set_active_instance"
                                    or any(name in (command.get("params") or {}) for name in ("unity_instance", "unityInstance"))) for command in commands)):
                            raise UnityIntegrationError("unity-instance-override-rejected")
                        dispatched = True
                        result = await self._external_tool(client, external, tool_name, scoped_arguments)
                        # A provider may switch its active project during an
                        # operation; do not certify that result as project-bound.
                        await asyncio.wait_for(self._verify_external(project, external, client), UNITY_PREFLIGHT_TIMEOUT_SECONDS)
                        return result
                    instance = self._instances.get(_key(project))
                    if not instance:
                        instance = await asyncio.to_thread(self._probe, project)
                    if not instance:
                        raise UnityIntegrationError("unity-project-not-connected", str(project))
                    scoped_arguments = dict(arguments or {})
                    for routing_key in ("unity_instance", "unityInstance"):
                        override = scoped_arguments.pop(routing_key, None)
                        if override is not None and override not in (instance["id"], instance["hash"]):
                            raise UnityIntegrationError("unity-instance-override-rejected", str(override))
                    if tool_name == "set_active_instance" and scoped_arguments.get("instance") not in (instance["id"], instance["hash"]):
                        raise UnityIntegrationError("unity-instance-override-rejected")
                    if tool_name == "batch_execute":
                        for command in scoped_arguments.get("commands", []):
                            if not isinstance(command, Mapping):
                                continue
                            nested = command.get("params") or {}
                            if command.get("tool") == "set_active_instance" or any(key in nested for key in ("unity_instance", "unityInstance")):
                                raise UnityIntegrationError("unity-instance-override-rejected")
                    executable = self._runtime_executable()
                    if not executable.is_file():
                        raise UnityIntegrationError("unity-runtime-not-prepared", str(executable))
                    descriptor = self._connection(project, executable, instance)
                    client = shared_client()
                    selected = await asyncio.wait_for(client.call(descriptor, "set_active_instance", {"instance": instance["id"]}), UNITY_PREFLIGHT_TIMEOUT_SECONDS)
                    if selected.get("success") is False:
                        raise UnityIntegrationError("unity-instance-selection-failed", json.dumps(selected))
                    # Verify through the persistent official MCP connection. Opening
                    # a second raw TCP socket for every tool would force Coplay to
                    # reconnect and rediscover its tools repeatedly.
                    identity = await asyncio.wait_for(client.call(descriptor, "execute_custom_tool", {"tool_name": "bread_connection_identity", "parameters": {}}), UNITY_PREFLIGHT_TIMEOUT_SECONDS)
                    data = self._tool_data(identity)
                    if identity.get("success") is False and not data.get("projectPath"):
                        raise UnityIntegrationError("unity-connection-not-ready")
                    if data.get("isCompiling") or data.get("isUpdating"):
                        raise UnityIntegrationError("unity-editor-busy")
                    config = _json(project / "Library/Bread/unity-connection.json")
                    if config.get("reconnectRequest") and data.get("reconnectRequest") != config["reconnectRequest"]:
                        raise UnityIntegrationError("unity-connection-not-ready")
                    if identity.get("success") is False or _key(data.get("projectPath", "")) != _key(project) or data.get("nonce") != config.get("nonce") or data.get("unityVersion") != _version(project) or data.get("packageRevision") != config.get("packageRevision"):
                        raise UnityIntegrationError("unity-project-identity-mismatch", str(project))
                    self._instances[_key(project)] = dict(instance)
                    if tool_name == "bread_connection_identity":
                        return identity
                    if tool_name.startswith("bread_"):
                        dispatched = True
                        return await client.call(descriptor, "execute_custom_tool", {"tool_name": tool_name, "parameters": scoped_arguments})
                    dispatched = True
                    return await client.call(descriptor, tool_name, scoped_arguments)
                finally:
                    await asyncio.to_thread(file_lock.__exit__, None, None, None)
        except Exception as error:
            recovery = (getattr(error, "transport_failure", False) or isinstance(error, (ConnectionError, TimeoutError))
                        or getattr(error, "code", "") in {"unity-project-not-connected", "unity-runtime-not-prepared", "unity-instance-selection-failed", "unity-connection-not-ready", "unity-editor-busy"})
            identity_changed = getattr(error, "code", "") == "unity-project-identity-mismatch"
            if recovery or identity_changed:
                self._instances.pop(_key(project_path), None)
            external = self._external_connections.get(_key(project_path))
            if external and identity_changed:
                await shared_client().close(external["descriptor"]["id"])
                self._external_connections.pop(_key(project_path), None)
            failed = self._error(error) if isinstance(error, UnityIntegrationError) or not external else {
                "schema": SCHEMA, "status": "failed", "code": "unity-mcp-call-failed", "detail": type(error).__name__, "errorType": type(error).__name__}
            return {**failed, "connectionRecoveryNeeded": bool(recovery),
                    "operationOutcome": "unknown" if dispatched and getattr(error, "request_sent", None) is not False else "not-started"}


_SERVICE: UnityIntegrationService | None = None


def shared_service() -> UnityIntegrationService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = UnityIntegrationService()
    return _SERVICE
