"""Project-scoped Unreal Editor jobs and Epic's installed MCP transport."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
from threading import Lock
import time
from uuid import uuid4

from .resource_export import _atomic_write_bytes, _atomic_write_json
from .shared_file_io import file_transaction
from .unity_restore import _file, _not_redirected, _sha
from .unreal_recipe import LIGHT_CLASSES, LIGHT_SETTINGS, MATTE_INPUTS, NATIVE_OPERATORS, Recipe, SCENE_INPUTS


PLUGIN_NAMES = ("PythonScriptPlugin", "EditorScriptingUtilities")
GLTF_IMPORTERS = ("Interchange", "GLTFImporter")
MCP_PLUGINS = ("ModelContextProtocol", "AllToolsets")
TOOLSETS = {"ActorTools", "AssetTools", "BlueprintTools", "LevelTools", "MaterialTools",
            "MaterialFunctionTools", "MaterialInstanceTools", "SceneTools", "TextureTools"}
BREAD_EDITOR_TOOLSET = "BreadEditorTools"
EDITOR_PYTHON_ENV = "BREAD_UNREAL_EDITOR_PYTHON"
WINDOWS_HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def project_descriptor(project_path):
    project = Path(project_path).absolute()
    _not_redirected(project)
    if project.is_dir():
        matches = list(project.glob("*.uproject"))
        if len(matches) != 1:
            raise ValueError("Select an Unreal project directory with one .uproject")
        project = matches[0]
    _not_redirected(project)
    if project.suffix.lower() != ".uproject" or not project.is_file():
        raise ValueError("Select an existing .uproject")
    return project.resolve()


def _launcher_installs():
    path = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
    path /= "Epic/UnrealEngineLauncher/LauncherInstalled.dat"
    if path.is_file():
        for item in json.loads(path.read_text(encoding="utf-8-sig")).get("InstallationList", []):
            if re.fullmatch(r"UE_\d+\.\d+", item.get("AppName", "")):
                yield item["AppName"][3:], Path(item["InstallLocation"])
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Epic Games\Unreal Engine\Builds") as key:
                for i in range(winreg.QueryInfoKey(key)[1]):
                    name, value, _ = winreg.EnumValue(key, i)
                    if isinstance(value, str):
                        yield name, Path(value)
        except FileNotFoundError:
            pass


def inspect_project(project_path, editor_path=None):
    project = project_descriptor(project_path)
    descriptor = json.loads(project.read_text(encoding="utf-8-sig"))
    association = str(descriptor.get("EngineAssociation", ""))
    if editor_path:
        binary = Path(editor_path).absolute()
        if binary.name.lower() != "unrealeditor-cmd.exe":
            raise ValueError("Select the installed UnrealEditor-Cmd.exe")
    else:
        binary = next((root / "Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
                       for key, root in _launcher_installs() if key.casefold() == association.casefold()), None)
    result = {"project": str(project), "engineAssociation": association,
              "status": "missing-editor", "nativeMcpAvailable": False, "renderCapabilities": "unobserved"}
    if binary is None or not binary.is_file():
        return result
    engine = binary.parents[2]
    version = json.loads(_file(engine, "Build/Build.version").read_text(encoding="utf-8-sig"))
    major, minor, patch = (int(version[k]) for k in ("MajorVersion", "MinorVersion", "PatchVersion"))
    if re.fullmatch(r"\d+\.\d+", association) and association != f"{major}.{minor}":
        raise ValueError("Editor major/minor does not match the project association")
    plugins = {p.stem for p in (engine / "Plugins").rglob("*.uplugin")}
    enabled = {p["Name"] for p in descriptor.get("Plugins", []) if p.get("Enabled")}
    available_importers = [name for name in GLTF_IMPORTERS if name in plugins]
    importer = next((name for name in available_importers if name in enabled),
                    next(iter(available_importers), None))
    native = major >= 5 and set(MCP_PLUGINS) <= plugins
    batch = major >= 5 and set(PLUGIN_NAMES) <= plugins and importer is not None
    return {**result, "status": "available" if batch or native else "unsupported-editor",
            "editor": str(binary), "editorVersion": f"{major}.{minor}.{patch}",
            "nativeMcpAvailable": native, "batchRenderAvailable": batch,
            "gltfImporterPlugin": importer, "enabledPlugins": sorted(enabled)}


def _enable_plugins(project, names):
    original = project.read_bytes()
    descriptor = json.loads(original.decode("utf-8-sig"))
    entries = descriptor.setdefault("Plugins", [])
    by_name = {p["Name"].casefold(): p for p in entries}
    for name in names:
        if name.casefold() in by_name:
            by_name[name.casefold()]["Enabled"] = True
        else:
            entries.append({"Name": name, "Enabled": True})
    if descriptor == json.loads(original.decode("utf-8-sig")):
        return original, original
    _atomic_write_json(project, descriptor)
    return original, project.read_bytes()


def _restore_descriptor(project, original, configured):
    if original != configured and project.read_bytes() == configured:
        _atomic_write_bytes(project, original)


def _project_editor_running(project):
    command = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
               "Get-CimInstance Win32_Process -Filter "
               "\"Name = 'UnrealEditor.exe' OR Name = 'UnrealEditor-Cmd.exe'\" | "
               "ForEach-Object { $_.CommandLine }")
    lines = subprocess.check_output(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                    timeout=15, **WINDOWS_HIDDEN).decode("utf-8", errors="replace")
    for line in lines.splitlines():
        for argument in re.findall(r'"[^"]*"|[^\s"]+', line):
            candidate = Path(argument.strip('"'))
            if candidate.suffix.casefold() == ".uproject":
                if candidate.is_absolute() and candidate.resolve() == project:
                    return True
                if not candidate.is_absolute() and candidate.name.casefold() == project.name.casefold():
                    return True
    return False


def _owned_listener(port, process):
    if process.poll() is not None:
        raise ValueError("Managed Unreal Editor exited")
    rows = subprocess.check_output(["netstat.exe", "-ano", "-p", "tcp"], timeout=10,
                                   **WINDOWS_HIDDEN).decode("ascii", errors="replace").splitlines()
    for row in rows:
        fields = row.split()
        if len(fields) >= 5 and fields[3] == "LISTENING" and fields[1].endswith(f":{port}"):
            if fields[1] != f"127.0.0.1:{port}" or fields[4] != str(process.pid):
                raise ValueError("Unreal MCP listener is not the managed loopback endpoint")
            return True
    return False


async def _mcp_request(port, tool=None, arguments=None):
    import anyio
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    def loopback_client(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                                 trust_env=False, follow_redirects=False)

    with anyio.fail_after(90):
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp",
                                         httpx_client_factory=loopback_client) as (read, write, _):
            async with ClientSession(read, write) as client:
                hello = await client.initialize()
                if hello.serverInfo.name != "unreal-mcp":
                    raise ValueError("Endpoint does not identify itself as Epic Unreal MCP")
                if tool:
                    result = await client.call_tool(tool, arguments or {})
                else:
                    result = await client.list_tools()
                return result.model_dump(mode="json", exclude_none=True)


def _native_arguments(tool, arguments):
    if tool == "list_toolsets" and not arguments:
        return {}
    if tool not in {"describe_toolset", "call_tool"}:
        raise ValueError("Discover tools through list_toolsets and describe_toolset")
    name = arguments.get("toolset_name", "")
    if not isinstance(name, str) or name.rsplit(".", 1)[-1] not in TOOLSETS:
        raise ValueError("Select a project asset, material, Blueprint, scene or actor toolset")
    if tool == "call_tool":
        action = arguments.get("tool_name")
        if not isinstance(action, str) or not re.fullmatch(r"[A-Za-z_]\w{0,95}", action):
            raise ValueError("Provide the exact advertised tool name")
        if re.search(r"(^|_)(delete|remove_asset|execute|python|script|shell|command|live_coding)(_|$)", action, re.I):
            raise ValueError("Use scoped editing tools for Unreal restoration")
        if not isinstance(arguments.get("arguments", {}), dict):
            raise ValueError("Native tool arguments must be an object")
    return arguments


def _editor_script():
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "bread_unreal/unreal_editor_script.py"
    return Path(__file__).with_name("unreal_editor_script.py")


def _stage_package(package, staging, texture_specs):
    manifest = json.loads(_file(package, "package-manifest.json").read_text(encoding="utf-8"))
    known = {entry["path"]: entry for entry in manifest["files"]}

    def copy(relative, target):
        source = _file(package, relative)
        record = known.get(relative)
        if not record or source.stat().st_size != record["size"]:
            raise ValueError("Artifact is not in the finalized export: " + relative)
        shutil.copyfile(source, target)
        if _sha(target) != record["sha256"]:
            raise ValueError("Artifact changed while preparing the Unreal import: " + relative)
        return target

    gltf = copy("asset.gltf", staging / "asset.gltf")
    copy("asset.bin", staging / "asset.bin")
    doc = json.loads(gltf.read_text(encoding="utf-8"))
    if (len(doc.get("meshes", [])) != 1 or len(doc.get("buffers", [])) != 1
            or doc["buffers"][0].get("uri") != "asset.bin"):
        raise ValueError("This Unreal import requires the canonical captured static mesh")
    gaps = []
    for primitive in doc["meshes"][0]["primitives"]:
        if primitive.get("mode", 4) != 4:
            raise ValueError("The Unreal static mesh route requires triangle primitives")
        primitive.pop("material", None)
        for attribute in primitive["attributes"]:
            if attribute.startswith("_"):
                gaps.append({"kind": "custom-vertex-channel", "channel": attribute})
    for field in ("materials", "images", "textures", "samplers"):
        doc.pop(field, None)
    _atomic_write_json(gltf, doc)
    textures = []
    for texture in texture_specs:
        suffix = Path(texture.artifact).suffix
        path = copy(texture.artifact, staging / ("texture_" + texture.id + suffix))
        textures.append({"id": texture.id, "path": str(path), "sRGB": texture.sRGB,
                         "float": suffix in {".exr", ".hdr"},
                         "sampler": {"filter": texture.filter, "address_x": texture.addressX,
                                     "address_y": texture.addressY}})
        gaps.append({"kind": "texture-view", "texture": texture.id,
                     "reason": "UE imports an image derivative without the captured mip chain"})
    return gltf, textures, gaps, _sha(package / "package-manifest.json")


def restoration_receipt(project, job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Invalid Unreal restoration job id")
    directory = project.parent / "Saved/Bread/Restorations" / job_id
    receipt = json.loads(_file(directory, "receipt.json").read_text(encoding="utf-8"))
    if (receipt.get("status") != "rendered" or receipt.get("jobId") != job_id
            or Path(receipt["project"]) != project):
        raise ValueError("No completed, matching Unreal render receipt")
    if not re.fullmatch(r"/Game/BreadImports/R_[a-f0-9]{12}_[a-f0-9]{12}", receipt.get("assetRoot", "")):
        raise ValueError("Invalid restored asset namespace")
    return receipt, directory


def _checked_destination(project, asset_root):
    content = project.parent / "Content" / "BreadImports"
    destination = content / asset_root.rsplit("/", 1)[-1]
    _not_redirected(destination)
    if not destination.resolve().is_relative_to(content.resolve()):
        raise ValueError("Unreal assets escape the expected project folder")
    return destination


def _saved_assets(project, destination):
    files = sorted(destination.rglob("*.uasset")) + sorted(destination.rglob("*.umap"))
    return {p.relative_to(project.parent).as_posix(): _sha(_file(project.parent, p.relative_to(project.parent).as_posix()))
            for p in files}


class UnrealService:
    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bread-unreal")
        self._lock = Lock()
        self._jobs = {}
        self._active = {}
        self._native = {}

    def start(self, project_path, editor_path, operation):
        info = inspect_project(project_path, editor_path)
        if info["status"] != "available":
            return info
        project = Path(info["project"])
        with self._lock:
            if project in self._active:
                raise ValueError("An Unreal job already owns this project")
            job_id = uuid4().hex
            state = {"status": "queued", "jobId": job_id, **{k: v for k, v in info.items() if k != "status"}}
            self._jobs[job_id] = state
            self._active[project] = job_id
            self._pool.submit(self._execute, project, state, operation)
            return dict(state)

    def _execute(self, project, state, operation):
        with self._lock:
            state["status"] = "running"
        try:
            lock = project.parent / "Saved/Bread/EditorIntegration"
            _not_redirected(lock.with_suffix(".update.lock"))
            with file_transaction(lock, timeout=60):
                result = operation(project, dict(state))
        except Exception as error:
            result = {"status": "failed", "diagnostic": str(error)}
        with self._lock:
            state.update(result)
            self._active.pop(project, None)

    def status(self, job_id):
        with self._lock:
            if job_id not in self._jobs:
                raise ValueError("Unknown Unreal job")
            return dict(self._jobs[job_id])

    def probe(self, project_path, editor_path):
        return self.start(project_path, editor_path, self._probe)

    def _probe(self, project, info):
        if not info.get("batchRenderAvailable"):
            raise ValueError("The selected editor is missing required project scripting or glTF import plugins")
        if _project_editor_running(project):
            raise ValueError("Close the target editor before launching a capability probe")
        with tempfile.TemporaryDirectory(prefix="BreadUnrealProbe-") as temp:
            directory = Path(temp)
            request = {"nonce": uuid4().hex, "project": str(project), "engine": info["editorVersion"],
                       "probe": True, "lightClasses": LIGHT_CLASSES}
            log = directory / "editor.log"
            try:
                receipt = self._editor_job(project, info, request, directory, log)
            except ValueError as error:
                if log.is_file():
                    with log.open(encoding="utf-8", errors="replace") as stream:
                        from collections import deque
                        tail = "".join(deque(stream, maxlen=20))
                    raise ValueError("Unreal capability probe failed. " + tail) from error
                raise
            return receipt

    def prepare(self, project_path, editor_path, interactive=False):
        return self.start(project_path, editor_path,
                          lambda project, info: self._connect(project, info, interactive))

    def _connect(self, project, info, interactive=False):
        from . import unreal_live_connection
        session = unreal_live_connection.discover(project, Path(info["editor"]))
        if session is None and interactive and not info["nativeMcpAvailable"]:
            if os.environ.get(EDITOR_PYTHON_ENV) != "1":
                raise ValueError("This Bread task has not been granted local Editor script execution")
            session = unreal_live_connection.launch(project, info)
        if session:
            return {"status": "connected", "provider": "bread-editor-python",
                    "transport": "interactive-editor", "editorPid": session["pid"],
                    "toolsets": [self._batch_toolset()], "nextTool": "call_unreal_project_tool"}

        if not info["nativeMcpAvailable"]:
            probed = self._probe(project, info)
            if probed["status"] != "probed":
                return probed
            return {**probed, "status": "connected", "provider": "bread-editor-python",
                    "transport": "background-editor", "toolsets": [self._batch_toolset()],
                    "nextTool": "call_unreal_project_tool"}
        if project in self._native:
            port, process, _ = self._native[project]
            if process.poll() is None and _owned_listener(port, process):
                return {"status": "connected", "provider": "epic-unreal-mcp"}
            self._native.pop(project)
        if _project_editor_running(project):
            raise ValueError("The project is open in an unmanaged Unreal Editor")
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = candidate.getsockname()[1]
        original, configured = _enable_plugins(project, MCP_PLUGINS)
        process = None
        try:
            process = subprocess.Popen([info["editor"], str(project), "-unattended", "-NoSplash",
                                        "-RenderOffscreen", "-NoSound", "-ModelContextProtocolStartServer",
                                        f"-ModelContextProtocolPort={port}",
                                        "-ini:Engine:[HTTPServer.Listeners]:DefaultBindAddress=127.0.0.1"],
                                       cwd=project.parent, **WINDOWS_HIDDEN)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline and not _owned_listener(port, process):
                time.sleep(1)
            if time.monotonic() >= deadline:
                raise ValueError("Unreal MCP did not start before the deadline")
            tools = asyncio.run(_mcp_request(port))
            names = {item["name"] for item in tools.get("tools", [])}
            if not {"list_toolsets", "describe_toolset", "call_tool"} <= names:
                raise ValueError("The selected Editor did not advertise the expected Epic MCP discovery tools")
            self._native[project] = (port, process, Lock())
            return {"status": "connected", "provider": "epic-unreal-mcp", "tools": sorted(names)}
        except BaseException:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=30)
            _restore_descriptor(project, original, configured)
            raise

    @staticmethod
    def _batch_toolset():
        tools = {}
        if os.environ.get(EDITOR_PYTHON_ENV) == "1":
            tools["run_python"] = {
                "description": "Run Python on the Unreal Editor thread for the selected project. "
                               "Set result to a small JSON value. Explicitly save edited assets and maps.",
                "inputSchema": {"type": "object", "additionalProperties": False, "required": ["code"],
                                "properties": {"code": {"type": "string", "maxLength": 120000}}},
            }
        return {"toolset_name": BREAD_EDITOR_TOOLSET, "tools": tools}

    def _batch_call(self, project, info, tool, arguments):
        toolset = self._batch_toolset()
        if tool == "list_toolsets" and not arguments:
            return {"status": "available", "provider": "bread-editor-python", "toolsets": [toolset]}
        if arguments.get("toolset_name") != BREAD_EDITOR_TOOLSET:
            raise ValueError("Discover the installed provider with list_toolsets")
        if tool == "describe_toolset" and set(arguments) == {"toolset_name"}:
            return toolset
        if tool != "call_tool" or arguments.get("tool_name") != "run_python":
            raise ValueError("Discover the exact Bread Editor tool with describe_toolset")
        if "run_python" not in toolset["tools"]:
            raise ValueError("This Bread task has not been granted local Editor script execution")
        params = arguments.get("arguments")
        code = params.get("code") if isinstance(params, dict) and set(params) == {"code"} else None
        if not isinstance(code, str) or not code.strip() or "\x00" in code or len(code.encode("utf-8")) > 120000:
            raise ValueError("Provide nonempty Editor Python source of at most 120000 bytes")
        return self.start(str(project), info["editor"], lambda target, job: self._run_editor_python(target, job, code))

    def _run_editor_python(self, project, info, code):
        from . import unreal_live_connection
        session = unreal_live_connection.discover(project, Path(info["editor"]))
        if session:
            return unreal_live_connection.execute(project, session, info["jobId"], code)
        if _project_editor_running(project):
            raise ValueError("The selected project is already open in an unmanaged Unreal Editor")
        directory = project.parent / "Saved/Bread/EditorMCP" / info["jobId"]
        _not_redirected(directory)
        directory.mkdir(parents=True, exist_ok=False)
        log = directory / "editor.log"
        with tempfile.TemporaryDirectory(prefix="BreadUnrealMcp-") as temp:
            request = {"nonce": uuid4().hex, "project": str(project), "engine": info["editorVersion"],
                       "editorPython": code}
            try:
                receipt = self._editor_job(project, info, request, Path(temp), log)
            except Exception as error:
                return {"status": "failed", "diagnostic": str(error), "logPath": str(log)}
        return {**receipt, "provider": "bread-editor-python", "logPath": str(log)}

    def call(self, project_path, tool, arguments, editor_path=None):
        project = project_descriptor(project_path)
        entry = self._native.get(project)
        if entry is None:
            info = inspect_project(str(project), editor_path)
            if not info["nativeMcpAvailable"] and info.get("batchRenderAvailable"):
                return self._batch_call(project, info, tool, arguments)
            raise ValueError("Start the managed Unreal MCP connection first")
        args = _native_arguments(tool, arguments)
        port, process, serial = entry
        with serial:
            if not _owned_listener(port, process):
                raise ValueError("The managed Unreal MCP endpoint is unavailable")
            return asyncio.run(_mcp_request(port, tool, args))

    def import_package(self, project_path, editor_path, manager, operation_id, recipe, replace_job_id=None):
        parsed = Recipe.model_validate(recipe)
        return self.start(project_path, editor_path,
                          lambda project, info: self._import(project, info, manager, operation_id, parsed,
                                                             replace_job_id))

    def _import(self, project, info, manager, operation_id, recipe, replace_job_id):
        operation_ids = dict.fromkeys([operation_id, *(mesh.operationId for mesh in recipe.additionalMeshes)])
        if any(texture.operationId and texture.operationId not in operation_ids for texture in recipe.textures):
            raise ValueError("A texture source must be one of the restoration's captured Draws")
        packages = {}
        sources = {}
        identities = set()
        for source_id in operation_ids:
            export = manager.status(source_id)
            if export.get("status") != "finalized" or not manager.validate(source_id).get("valid"):
                raise ValueError("Finalize and validate the captured Draw before Unreal import: " + source_id)
            request = export.get("request") or {}
            capture = export.get("capture") or {}
            source = {key: capture.get(key) or request.get(key)
                      for key in ("captureId", "eventId", "captureSessionId")}
            fingerprint = (request.get("captureSource") or {}).get("contentSha256")
            identity = ("file", fingerprint) if fingerprint else (
                "session", source["captureSessionId"] or source["captureId"])
            identities.add(identity)
            sources[source_id] = source
            packages[source_id] = Path(export["packagePath"])
        if len(packages) > 1 and (len(identities) != 1 or not next(iter(identities))[1]):
            raise ValueError("All Unreal Draw packages must identify the same captured frame")
        return self.render(project, info, packages, operation_id, recipe, replace_job_id,
                           sources=sources)

    def observe(self, project_path, editor_path, base_job_id):
        project = project_descriptor(project_path)
        base, directory = restoration_receipt(project, base_job_id)
        recipe = Recipe.model_validate_json(_file(directory, "recipe.json").read_bytes())
        return self.start(project_path, editor_path,
                          lambda project, info: self.render(project, info, None, base["operationId"],
                                                            recipe, observe=base))

    def preflight(self, project_path, editor_path, material_paths, include_level=False):
        if (not isinstance(material_paths, list) or len(material_paths) > 512
                or any(not isinstance(p, str) or not re.fullmatch(r"/(Game|Engine|[A-Za-z0-9_]+)/[A-Za-z0-9_/.]+", p)
                       or ".." in p for p in material_paths)
                or (not material_paths and not include_level)):
            raise ValueError("Select consuming material/instance asset paths or include_level")
        def operation(project, info):
            from . import unreal_live_connection
            session = unreal_live_connection.discover(project, Path(info["editor"]))
            log = (Path(session["directory"]) / "editor.log" if session else
                   project.parent / "Saved/Bread/EditorMCP" / info["jobId"] / "editor.log")
            code = ("import runpy\n"
                    f"helper = runpy.run_path({str(_editor_script())!r})\n"
                    f"paths = {material_paths!r}\n"
                    "materials = [unreal.load_asset(p) for p in paths]\n"
                    "if any(m is None for m in materials): raise ValueError('Selected material asset is missing')\n"
                    "world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()\n"
                    f"result = helper['preflight_materials'](materials, world, {str(log)!r}, {include_level!r})\n")
            result = self._run_editor_python(project, info, code)
            if result.get("status") == "executed" and not (result.get("result") or {}).get("passed"):
                result["status"] = "material-preflight-blocked"
            return result
        return self.start(project_path, editor_path, operation)

    def render(self, project, info, packages, operation_id, recipe, replace_job_id=None,
               observe=None, sources=None):
        if not info.get("batchRenderAvailable"):
            raise ValueError("The selected editor does not support Bread's batch rendering route")
        if _project_editor_running(project):
            raise ValueError("Close the target project's Unreal Editor before batch rendering to protect unsaved work")
        evidence = project.parent / "Saved" / "Bread" / "Restorations" / info["jobId"]
        _not_redirected(evidence)
        recipe_json = recipe.model_dump(mode="json", exclude_none=True)
        if not recipe.additionalMeshes:
            recipe_json.pop("additionalMeshes")
        recipe_sha = hashlib.sha256(json.dumps(recipe_json, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        asset_root = "/Game/BreadImports/R_" + operation_id[:12] + "_" + recipe_sha[:12]
        if observe:
            asset_root = observe["assetRoot"]
        replaced = None
        if replace_job_id:
            replaced, _ = restoration_receipt(project, replace_job_id)
            previous_sources = replaced.get("sourcePackages", {replaced["operationId"]: {}})
            if replaced["operationId"] != operation_id or previous_sources.keys() != packages.keys():
                raise ValueError("A replacement must refer to the same captured operations")
            asset_root = replaced["assetRoot"]
        destination = _checked_destination(project, asset_root)
        previous_hashes = _saved_assets(project, destination) if observe or replaced else {}
        if replaced and (not previous_hashes or previous_hashes != replaced["savedAssetHashes"]):
            raise ValueError("Previous Unreal assets have been edited; keep their authored changes")
        if not observe and not replaced and destination.exists():
            raise ValueError("This exact restoration already has assets. Reopen its level or render it again")
        evidence.mkdir(parents=True, exist_ok=False)
        log = evidence / "editor.log"
        backup = evidence / "previous-assets"
        if replaced:
            destination.replace(backup)
        try:
            with tempfile.TemporaryDirectory(prefix="BreadUnreal-") as temp:
                staging = Path(temp)
                request = {"nonce": uuid4().hex, "project": str(project), "engine": info["editorVersion"],
                           "assetRoot": asset_root, "evidence": str(evidence), "recipe": recipe_json,
                           "nativeOperators": NATIVE_OPERATORS, "sceneInputs": SCENE_INPUTS,
                           "matteInputs": MATTE_INPUTS, "lightClasses": LIGHT_CLASSES,
                           "lightSettings": LIGHT_SETTINGS}
                if observe:
                    request["observe"] = {key: observe[key] for key in ("level", "materials")}
                    gaps, package_sha = observe["gaps"], observe["packageSha256"]
                    source_packages = observe.get("sourcePackages")
                else:
                    meshes = {}
                    textures = []
                    gaps = []
                    source_packages = {}
                    for source_id, package in packages.items():
                        part = staging / source_id
                        part.mkdir()
                        texture_specs = [texture for texture in recipe.textures
                                         if (texture.operationId or operation_id) == source_id]
                        mesh, imported_textures, import_gaps, digest = _stage_package(package, part, texture_specs)
                        meshes[source_id] = str(mesh)
                        textures.extend(imported_textures)
                        gaps.extend({**gap, "operationId": source_id} for gap in import_gaps)
                        source_packages[source_id] = {**sources[source_id], "packageSha256": digest}
                    package_sha = source_packages[operation_id]["packageSha256"]
                    request.update({"mesh": meshes[operation_id], "textures": textures,
                                    "additionalMeshes": {item.id: meshes[item.operationId]
                                                         for item in recipe.additionalMeshes}})
                receipt = self._editor_job(project, info, request, staging, log)
                if receipt["status"] == "rendered":
                    saved = _saved_assets(project, destination)
                    if not any(key.endswith(".umap") for key in saved) or (observe and saved != previous_hashes):
                        raise ValueError("The expected saved level is missing or changed during observation")
                    receipt["savedAssetHashes"] = saved
                    config_paths = ("Config/DefaultEngine.ini", "Config/DefaultScalability.ini")
                    receipt["renderConfigHashes"] = {name: _sha(_file(project.parent, name))
                                                      if (project.parent / name).exists() else None
                                                      for name in config_paths}
                    for image in receipt["images"]:
                        path = Path(image["path"])
                        if path.parent != evidence or _sha(path) != image["sha256"]:
                            raise ValueError("Unreal RT receipt disagrees with the saved artifact")
                    with log.open(encoding="utf-8", errors="replace") as stream:
                        diagnostics = [line.strip() for line in stream if "Failed to compile Material" in line
                                       and asset_root in line]
                    if diagnostics:
                        receipt["status"] = "shader-errors"
                        receipt["shaderDiagnostics"] = diagnostics[:40]
            source_capture = observe.get("sourceCapture", {}) if observe else sources[operation_id]
            if source_packages is not None:
                receipt["sourcePackages"] = source_packages
            receipt.update({"operationId": operation_id, "project": str(project), "jobId": info["jobId"],
                            "sourceCapture": source_capture,
                            "assetRoot": asset_root, "recipeSha256": recipe_sha, "packageSha256": package_sha,
                            "logPath": str(log), "gaps": gaps, "engine": info["editorVersion"]})
            _atomic_write_json(evidence / "recipe.json", recipe_json)
            _atomic_write_json(evidence / "receipt.json", receipt)
        except Exception as error:
            receipt = {"status": "failed", "diagnostic": str(error), "logPath": str(log)}
        if receipt["status"] != "rendered" and not observe:
            if destination.exists():
                destination.replace(evidence / "failed-assets")
            if backup.exists():
                backup.replace(destination)
        return receipt

    @staticmethod
    def _editor_job(project, info, request, staging, log):
        req_path = staging / "request.json"
        _atomic_write_json(req_path, request)
        original, configured = _enable_plugins(project, (*PLUGIN_NAMES, info["gltfImporterPlugin"]))
        try:
            script = _editor_script()
            result = subprocess.run([info["editor"], str(project), "-unattended", "-NoSplash",
                                     "-RenderOffscreen", "-NoSound", f"-abslog={log}",
                                     f"-ExecutePythonScript={script}"], cwd=project.parent,
                                    env={**os.environ, "BREAD_UNREAL_REQUEST": str(req_path)},
                                    timeout=1200, **WINDOWS_HIDDEN)
        finally:
            _restore_descriptor(project, original, configured)
        receipt_path = staging / "receipt.json"
        if not receipt_path.is_file():
            raise ValueError(f"Editor produced no receipt (exit {result.returncode}). See {log}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.pop("nonce", "") != request["nonce"]:
            raise ValueError("Unreal Editor receipt identity mismatch")
        if result.returncode and receipt["status"] in {"rendered", "probed", "executed"}:
            receipt["status"] = "editor-error"
        return receipt


_SERVICE = UnrealService()
