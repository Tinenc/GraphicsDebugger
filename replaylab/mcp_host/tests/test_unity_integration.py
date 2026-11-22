from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import threading
import time
import zipfile
from unittest import mock

import pytest

from src import unity_integration as unity


def test_portable_provider_uses_relative_bundle_without_runtime_install(tmp_path, monkeypatch):
    root = tmp_path / "relocated 中文" / "replaylab"
    executable = root / "BreadMCP.exe"
    bundle = root / "tools/unity-mcp"
    write(bundle / "bread-unity-provider.json", {"commit": unity.COMMIT, "archiveSha256": unity.ARCHIVE_SHA256})
    write(bundle / "MCPForUnity/package.json", {"version": unity.VERSION})
    write(bundle / "runtime/python.exe", "test interpreter")
    write(bundle / "runtime/bread_unity_entry.py", "from main import main\nmain()\n")
    monkeypatch.setattr(unity.sys, "frozen", True, raising=False)
    monkeypatch.setattr(unity.sys, "executable", str(executable))
    service = unity.UnityIntegrationService(cache_root=tmp_path / "empty-cache")
    job = unity._Job("test", "")
    with mock.patch.object(unity, "_download", side_effect=AssertionError("must stay offline")):
        assert service._source(job) == bundle
        assert service._runtime(bundle, job) == bundle / "runtime/python.exe"
        assert service._runtime_executable() == bundle / "runtime/python.exe"
    descriptor = service._connection(tmp_path / "UnityProject", service._runtime_executable(), {"hash": "project-instance"})
    assert descriptor["connection"]["args"][:2] == ["-I", str(bundle / "runtime/bread_unity_entry.py")]
    assert not (tmp_path / "empty-cache").exists()


def test_portable_provider_does_not_download_when_its_bundle_is_missing(tmp_path, monkeypatch):
    write(tmp_path / "RenderDocBread.portable", "portable")
    monkeypatch.setattr(unity.sys, "frozen", True, raising=False)
    monkeypatch.setattr(unity.sys, "executable", str(tmp_path / "replaylab/BreadMCP.exe"))
    with pytest.raises(unity.UnityIntegrationError) as error:
        unity.UnityIntegrationService._bundled_provider()
    assert error.value.code == "bundled-unity-provider-missing"


def write(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "Unity project"
    (root / "Assets").mkdir(parents=True)
    write(root / "ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.0.55f1\n")
    write(root / "Packages/manifest.json", {"dependencies": {"com.unity.render-pipelines.universal": "17.0.4"}})
    return root


@pytest.fixture
def service(tmp_path):
    return unity.UnityIntegrationService(tmp_path / "cache")


def wait_job(service, result):
    deadline = time.monotonic() + 4
    while result["status"] not in unity._TERMINAL and time.monotonic() < deadline:
        time.sleep(0.01)
        result = service.job_status(result["jobId"])
    assert result["status"] in unity._TERMINAL
    return result


def test_missing_editor_is_actionable_and_does_not_install(project, service):
    with mock.patch.object(unity, "discover_editors", return_value=[]), mock.patch.object(service, "_source") as source:
        inspected = service.inspect_project(str(project))
        assert inspected["status"] == "unity-editor-required"
        assert inspected["connected"] is False
        assert inspected["nextAction"]["requiredVersion"] == "6000.0.55f1"
        result = wait_job(service, service.prepare_project(str(project)))
        assert result["status"] == "action-required"
        source.assert_not_called()
    assert not (project / "Library").exists()


def test_exact_editor_version_not_latest_or_project_upgrade(project, service, tmp_path):
    editor = write(tmp_path / "6000.0.56f1/Editor/Unity.exe", "fake")
    with mock.patch.object(unity, "discover_editors", return_value=[{"path": str(editor), "version": "6000.0.56f1"}]):
        assert service.inspect_project(str(project))["status"] == "unity-editor-required"
        result = service.inspect_project(str(project), str(editor))
        assert result["code"] == "unity-editor-version-mismatch"
    assert "6000.0.55f1" in (project / "ProjectSettings/ProjectVersion.txt").read_text()


def test_hub_v2_editor_metadata(tmp_path, monkeypatch):
    editor = write(tmp_path / "custom/Unity.exe", "fake")
    write(tmp_path / "appdata/UnityHub/editors-v2.json", {"schema_version": "2", "data": [{"version": "6000.0.55f1", "location": [str(editor)]}]})
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert {"path": str(editor.resolve()), "version": "6000.0.55f1"} in unity.discover_editors()


def test_package_install_idempotent_preserves_modifications(project, service, tmp_path):
    source = tmp_path / "source"
    write(source / "package.json", {"name": "com.bread.test", "version": "0.1.0"})
    write(source / "Editor/Tool.cs", "original")
    service._install_package(project, source)
    destination = project / "Packages/com.bread.test"
    stamp = (destination / "Editor/Tool.cs").stat().st_mtime_ns
    service._install_package(project, source)
    assert (destination / "Editor/Tool.cs").stat().st_mtime_ns == stamp
    write(destination / "Editor/Tool.cs", "user change")
    with pytest.raises(unity.UnityIntegrationError, match="com.bread.test") as error:
        service._install_package(project, source)
    assert error.value.code == "unity-package-modified"
    assert (destination / "Editor/Tool.cs").read_text() == "user change"


def test_package_install_rejects_conflicting_existing_dependency(project, service, tmp_path):
    source = tmp_path / "source"
    write(source / "package.json", {"name": unity.PACKAGE, "version": unity.VERSION})
    write(project / "Packages/manifest.json", {"dependencies": {unity.PACKAGE: "https://example.test/other.git"}})
    with pytest.raises(unity.UnityIntegrationError) as error:
        service._install_package(project, source)
    assert error.value.code == "unity-package-conflict"
    assert not (project / "Packages" / unity.PACKAGE).exists()


def test_managed_package_update_preserves_unrelated_files(project, service, tmp_path):
    source = tmp_path / "source"
    write(source / "package.json", {"name": "com.bread.test", "version": "0.1.0"})
    write(source / "Editor/Tool.cs", "one")
    keep = write(project / "Assets/User.asset", "keep")
    service._install_package(project, source)
    write(source / "Editor/Tool.cs", "two")
    service._install_package(project, source)
    assert (project / "Packages/com.bread.test/Editor/Tool.cs").read_text() == "two"
    assert keep.read_text() == "keep"


def test_archive_traversal_rejected(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.txt", "bad")
    with pytest.raises(unity.UnityIntegrationError) as error:
        unity._extract_zip(archive, tmp_path / "out")
    assert error.value.code == "unsafe-package-archive"
    assert not (tmp_path / "escape.txt").exists()


def test_checksum_mismatch_never_promotes_download(tmp_path):
    import io
    target = tmp_path / "download.zip"
    with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"wrong")):
        with pytest.raises(unity.UnityIntegrationError) as error:
            unity._download("https://example.test/source", target, hashlib.sha256(b"right").hexdigest(), threading.Event())
    assert error.value.code == "download-checksum-mismatch"
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_prepare_job_deduplicates_and_cancellation_never_starts_editor(project, service):
    entered = threading.Event()

    def stalled_source(job):
        entered.set()
        job.cancel.wait(3)
        raise unity.UnityIntegrationError("cancelled")

    with mock.patch.object(service, "inspect_project", return_value={"status": "ready-to-prepare", "editorPath": "fake"}), mock.patch.object(service, "_source", side_effect=stalled_source), mock.patch.object(service, "_start_editor") as start:
        one = service.prepare_project(str(project))
        assert entered.wait(1)
        two = service.prepare_project(str(project))
        assert one["jobId"] == two["jobId"]
        assert two["reused"]
        service.cancel_job(one["jobId"])
        assert wait_job(service, one)["status"] == "cancelled"
        start.assert_not_called()


def test_probe_requires_exact_path_nonce_and_version(project, service, tmp_path, monkeypatch):
    status_dir = tmp_path / "status"
    monkeypatch.setenv("UNITY_MCP_STATUS_DIR", str(status_dir))
    write(project / "Library/Bread/unity-connection.json", {"enabled": True, "nonce": "expected"})
    write(status_dir / "unity-mcp-status-1234abcd.json", {"project_path": str(project / "Assets"), "project_name": project.name, "unity_port": 12345})
    response = {"success": True, "data": {"projectPath": str(project), "nonce": "wrong", "unityVersion": "6000.0.55f1"}}
    with mock.patch.object(service, "_bridge_identity", return_value=response):
        assert service._probe(project) is None
        response["data"]["nonce"] = "expected"
        assert service._probe(project)["hash"] == "1234abcd"
        response["data"]["projectPath"] = str(tmp_path / "same name elsewhere")
        assert service._probe(project) is None


def test_official_framing_handshake_and_wrapped_response():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    commands = []

    def serve():
        with listener, listener.accept()[0] as client:
            client.sendall(b"WELCOME UNITY-MCP 1 FRAMING=1\n")
            header = client.recv(8)
            commands.append(json.loads(client.recv(struct.unpack(">Q", header)[0])))
            result = json.dumps({"status": "success", "result": {"success": True, "data": {"nonce": "ok"}}}).encode()
            client.sendall(struct.pack(">Q", len(result)) + result)

    thread = threading.Thread(target=serve)
    thread.start()
    result = unity.UnityIntegrationService._bridge_identity(port)
    thread.join(timeout=2)
    assert result["data"]["nonce"] == "ok"
    assert commands == [{"type": "bread_connection_identity", "params": {}}]


def test_call_routes_to_exact_instance_and_reuses_shared_client(project, service):
    executable = service.cache_root / "runtime" / unity.COMMIT / ("Scripts/mcp-for-unity.exe" if os.name == "nt" else "bin/mcp-for-unity")
    write(executable, "fake")
    write(project / "Library/Bread/unity-connection.json", {"enabled": True, "nonce": "expected"})
    client = mock.Mock()
    client.call = mock.AsyncMock(side_effect=[{"success": True}, {"success": True, "data": {"projectPath": str(project), "nonce": "expected", "unityVersion": "6000.0.55f1"}}, {"success": True, "data": {"status": "imported"}}])
    instance = {"hash": "1234abcd", "id": "Exact@1234abcd"}
    with mock.patch.object(service, "_probe", return_value=instance), mock.patch("src.mcp_tool_client.shared_client", return_value=client):
        result = asyncio.run(service.call_tool(str(project), "bread_import_capture", {"request_path": "request.json"}))
    assert result["data"]["status"] == "imported"
    calls = client.call.call_args_list
    assert calls[0].args[1:] == ("set_active_instance", {"instance": "Exact@1234abcd"})
    assert calls[2].args[1:] == ("execute_custom_tool", {"tool_name": "bread_import_capture", "parameters": {"request_path": "request.json"}})
    assert "1234abcd" in calls[0].args[0]["connection"]["args"]


def test_import_pending_is_polled_and_partial_is_not_success(project, service):
    identity = {"requestSha256": "a" * 64, "packageManifestSha256": "b" * 64}
    request = write(project / "Library/Bread/request.json", identity)
    responses = [{"success": True, "data": {"status": "pending"}}, {"success": True, "data": {**identity, "status": "partial", "requestFileSha256": hashlib.sha256(request.read_bytes()).hexdigest(), "gaps": ["shader-not-validated"]}}]

    async def run():
        with mock.patch.object(service, "prepare_project", return_value={"status": "completed", "result": {"connected": True}}), mock.patch.object(service, "call_tool", new=mock.AsyncMock(side_effect=responses)) as call:
            result = await service.start_import(str(project), str(request))
            await asyncio.gather(*service._tasks)
            assert service.job_status(result["jobId"])["status"] == "partial"
            assert call.call_args_list[-1].args[2]["action"] == "status"
    asyncio.run(run())


def test_import_request_cannot_escape_project(project, service, tmp_path):
    request = write(tmp_path / "elsewhere.json", {})
    result = asyncio.run(service.start_import(str(project), str(request)))
    assert result["code"] == "unity-import-request-outside-project"


@pytest.mark.parametrize("receipt,expected", [
    ({"status": "interrupted"}, "failed"),
    ({"status": "not_started"}, "failed"),
    ({"status": "imported"}, "failed"),
    ({"status": "imported", "checks": {}}, "failed"),
    ({"status": "imported", "checks": {"shaderCompilation": "passed"}}, "failed"),
    ({"status": "imported", "checks": {"shaderCompile": False}}, "failed"),
    ({"status": "imported", "checks": {key: True for key in ("meshImport", "shaderCompile", "materialImport", "textureImport", "sceneIntegrity", "pipelineCompatible")}}, "completed"),
])
def test_import_requires_final_receipt_checks(project, service, receipt, expected):
    identity = {"requestSha256": "a" * 64, "packageManifestSha256": "b" * 64}
    request = write(project / "Library/Bread/Requests/request.json", identity)
    receipt.update(identity)
    receipt["requestFileSha256"] = hashlib.sha256(request.read_bytes()).hexdigest()

    async def run():
        replies = [{"success": True, "data": receipt}]
        with mock.patch.object(service, "prepare_project", return_value={"status": "completed", "result": {"connected": True}}), mock.patch.object(service, "call_tool", new=mock.AsyncMock(side_effect=replies)):
            result = await service.start_import(str(project), str(request))
            await asyncio.gather(*service._tasks)
            assert service.job_status(result["jobId"])["status"] == expected
    asyncio.run(run())


def test_coplay_license_and_pinned_provenance_are_installed(project, service, tmp_path):
    source = tmp_path / "provider/MCPForUnity"
    write(source / "package.json", {"name": unity.PACKAGE, "version": unity.VERSION})
    write(source.parent / "LICENSE", "MIT License\nCopyright CoplayDev\n")
    service._install_package(project, source)
    destination = project / "Packages" / unity.PACKAGE
    assert (destination / "LICENSE.CoplayDev.txt").read_text().startswith("MIT License")
    provenance = json.loads((destination / ".bread-package.json").read_text())
    assert provenance["commit"] == unity.COMMIT
    assert provenance["contentSha256"] == unity._tree_digest(destination)
    service._install_package(project, source)
    assert not (source / "LICENSE.CoplayDev.txt").exists()


def test_inspection_never_interrupts_a_persistent_mcp_socket(project, service):
    with mock.patch.object(unity, "discover_editors", return_value=[]), mock.patch.object(service, "_probe") as probe:
        service.inspect_project(str(project))
        probe.assert_not_called()


def test_prepared_without_connection_never_completes_import(project, service):
    request = write(project / "Library/Bread/Requests/request.json", {})

    async def run():
        with mock.patch.object(service, "prepare_project", return_value={"status": "completed", "result": {"connected": False}}), mock.patch.object(service, "call_tool", new=mock.AsyncMock()) as call:
            result = await service.start_import(str(project), str(request))
            await asyncio.gather(*service._tasks)
            assert service.job_status(result["jobId"])["status"] == "action-required"
            call.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("tool,arguments", [
    ("manage_asset", {"unity_instance": "Other@abcdabcd"}),
    ("manage_asset", {"unityInstance": "Other@abcdabcd"}),
    ("set_active_instance", {"instance": "Other@abcdabcd"}),
    ("batch_execute", {"commands": [{"tool": "manage_asset", "params": {"unity_instance": "Other@abcdabcd"}}]}),
])
def test_per_call_instance_overrides_cannot_escape_project(project, service, tool, arguments):
    with mock.patch.object(service, "_probe", return_value={"hash": "1234abcd", "id": "Exact@1234abcd"}), mock.patch("src.mcp_tool_client.shared_client") as client:
        result = asyncio.run(service.call_tool(str(project), tool, arguments))
        assert result["code"] == "unity-instance-override-rejected"
        client.assert_not_called()


def test_cancel_after_import_started_observes_real_receipt(project, service):
    job = unity._Job("cancelled-but-import-inflight", str(project))
    job.cancel.set()
    result = {"success": True, "data": {"status": "partial"}}
    with mock.patch.object(service, "call_tool", new=mock.AsyncMock(return_value=result)) as call:
        observed = asyncio.run(service._poll_tool(job, "bread_import_capture", {"action": "status"}, 1, observe_started=True))
        assert observed == result
        call.assert_called_once()
        assert job.result["lastObservedResult"] == result


def test_cached_connection_is_not_reported_as_live_inspection(project, service):
    with mock.patch.object(unity, "discover_editors", return_value=[]), mock.patch.object(service, "_known_connection", return_value=True):
        result = service.inspect_project(str(project))
        assert result["connected"] is None
        assert result["connectionState"] == "cached-unverified"


def test_installed_pipeline_package_does_not_claim_it_is_active(project, service):
    with mock.patch.object(unity, "discover_editors", return_value=[]):
        result = service.inspect_project(str(project))
    assert result["renderPipeline"] == "unverified"
    assert result["declaredRenderPipelines"] == {"urp": "17.0.4"}
    assert result["renderingInspectionTool"] == "bread_inspect_rendering"
    assert result["managedImporter"]["scope"] == "manifested-draw-and-pass-implementation"
    assert result["managedImporter"]["pipeline"] == "preserve-existing"


def test_cancel_import_cancels_its_unshared_preparation(project, service):
    entered = threading.Event()
    request = write(project / "Library/Bread/request.json", {})

    def source(job):
        entered.set()
        assert job.cancel.wait(3)
        raise unity.UnityIntegrationError("cancelled")

    async def run():
        with mock.patch.object(service, "inspect_project", return_value={"status": "ready-to-prepare", "editorPath": "fake"}), mock.patch.object(service, "_source", side_effect=source), mock.patch.object(service, "_start_editor") as start:
            result = await service.start_import(str(project), str(request))
            while not entered.is_set():
                await asyncio.sleep(0.01)
            service.cancel_job(result["jobId"])
            await asyncio.gather(*service._tasks)
            completed = service.job_status(result["jobId"])
            assert completed["status"] == "cancelled"
            setup = service.job_status(completed["result"]["preparationJobId"])
            assert wait_job(service, setup)["status"] == "cancelled"
            start.assert_not_called()
    asyncio.run(run())


def test_prepare_without_launch_is_upgraded_by_import_requirement(project, service):
    entered = threading.Event()

    def source(job):
        entered.set()
        job.cancel.wait(3)
        raise unity.UnityIntegrationError("cancelled")

    with mock.patch.object(service, "inspect_project", return_value={"status": "ready-to-prepare", "editorPath": "fake"}), mock.patch.object(service, "_source", side_effect=source):
        first = service.prepare_project(str(project), start_editor=False)
        assert entered.wait(1)
        second = service.prepare_project(str(project), start_editor=True)
        assert second["jobId"] == first["jobId"]
        assert service._jobs[first["jobId"]].start_editor_requested is True
        service.cancel_job(first["jobId"])
        assert wait_job(service, first)["status"] == "cancelled"


def test_live_bread_connection_is_reused_before_editor_discovery_or_install(project, service, tmp_path):
    identity = managed_bread_packages(project, service, tmp_path)
    with mock.patch.object(service, "_probe", return_value=identity), mock.patch.object(service, "inspect_project") as inspect, mock.patch.object(service, "_source") as source, mock.patch.object(service, "_install_package") as install:
        result = wait_job(service, service.prepare_project(str(project)))
    assert result["status"] == "completed" and result["result"]["reused"]
    assert result["result"]["editorStarted"] is False
    inspect.assert_not_called()
    source.assert_not_called()
    install.assert_not_called()


def test_cached_live_connection_preserves_the_ai_transport(project, service, tmp_path, monkeypatch):
    identity = managed_bread_packages(project, service, tmp_path)
    service._instances[unity._key(project)] = identity
    status_dir = tmp_path / "status"
    monkeypatch.setenv("UNITY_MCP_STATUS_DIR", str(status_dir))
    write(status_dir / "unity-mcp-status-1234abcd.json", {"reloading": False})
    settings = project / "Library/Bread/unity-connection.json"
    original_settings = settings.read_bytes()
    with mock.patch.object(service, "_bridge_identity") as handshake, \
            mock.patch.object(service, "_start_editor") as start, \
            mock.patch.object(service, "_install_package") as install:
        result = wait_job(service, service.prepare_project(str(project)))
    assert result["status"] == "completed" and result["result"]["reused"]
    assert result["result"]["instance"] == identity
    assert settings.read_bytes() == original_settings
    handshake.assert_not_called()
    start.assert_not_called()
    install.assert_not_called()


@pytest.mark.parametrize("locked", [True, False])
def test_prepare_connects_open_editor_or_launches_one_with_mcp_enabled(project, service, tmp_path, locked):
    identity = managed_bread_packages(project, service, tmp_path)
    with mock.patch.object(service, "_probe", side_effect=[None, None, identity]), \
            mock.patch.object(service, "inspect_project", return_value={"status": "ready-to-prepare", "editorPath": "Unity.exe"}), \
            mock.patch.object(service, "_source", return_value=tmp_path / "provider-source"), \
            mock.patch.object(service, "_runtime", return_value=service._runtime_executable()), \
            mock.patch.object(service, "_project_locked", return_value=locked), \
            mock.patch.object(service, "_start_editor") as start:
        result = wait_job(service, service.prepare_project(str(project)))
    assert result["status"] == "completed" and result["result"]["connected"]
    assert result["result"]["editorStarted"] is not locked
    assert start.call_count == (0 if locked else 1)
    config = json.loads((project / "Library/Bread/unity-connection.json").read_text())
    assert config["enabled"] is True and config["nonce"] == identity["nonce"]


def test_started_editor_is_visible_and_retains_graphics(project):
    with mock.patch.object(unity.subprocess, "Popen") as popen:
        unity.UnityIntegrationService._start_editor(project, "Unity.exe")
    assert popen.call_args.args[0] == [
        "Unity.exe", "-projectPath", str(project), "-logFile", str(project / "Library/Bread/editor.log")]
    assert "creationflags" not in popen.call_args.kwargs
    if unity.sys.platform == "win32":
        startup = popen.call_args.kwargs["startupinfo"]
        assert startup.dwFlags & unity.subprocess.STARTF_USESHOWWINDOW
        assert startup.wShowWindow == 4  # visible, without activation
    else:
        assert "startupinfo" not in popen.call_args.kwargs


def test_provider_scene_capture_adapter_is_managed_idempotent_and_preserves_user_edits(project, service, tmp_path):
    source = tmp_path / "provider/MCPForUnity"
    relative = "Editor/Helpers/EditorWindowScreenshotUtility.cs"
    original = "void Capture() { sceneView.Focus(); sceneView.Repaint(); }"
    write(source / "package.json", {"name": unity.PACKAGE, "version": unity.VERSION})
    write(source / relative, original)
    write(source / "Editor/Tools/ManageScene.cs", '''class ManageScene {
        private static void EnsureGameView() { EditorApplication.ExecuteMenuItem("Window/General/Game"); }
        private static void ScheduleAssetImportWhenFileExists(string path) { KeepImport(path); }
}''')
    write(source.parent / "LICENSE", "MIT test license")
    service._install_package(project, source)
    target = project / "Packages" / unity.PACKAGE
    installed = (target / relative).read_text(encoding="utf-8")
    assert "sceneView.Focus();" not in installed
    assert "sceneView.Repaint();" in installed
    assert (source / relative).read_text(encoding="utf-8") == original
    scene_tools = (target / "Editor/Tools/ManageScene.cs").read_text(encoding="utf-8")
    assert 'ExecuteMenuItem("Window/General/Game")' not in scene_tools
    assert "FindObjectsOfTypeAll(type)" in scene_tools
    assert "KeepImport(path)" in scene_tools
    marker = json.loads((target / ".bread-package.json").read_text())
    assert marker["breadAdapterRevision"] == unity.PROVIDER_ADAPTER_REVISION
    assert service._package_state(project, source)[3] is True
    # A prior managed copy upgrades without losing MonoScript GUIDs.
    marker.pop("breadAdapterRevision")
    write(target / ".bread-package.json", marker)
    write(target / (relative + ".meta"), "guid: retained-guid")
    assert service._package_state(project, source)[3] is False
    service._install_package(project, source)
    assert (target / (relative + ".meta")).read_text() == "guid: retained-guid"
    write(target / relative, "user source")
    with pytest.raises(unity.UnityIntegrationError) as error:
        service._install_package(project, source)
    assert error.value.code == "unity-package-modified"
    assert (target / relative).read_text() == "user source"


def managed_bread_packages(project, service, tmp_path):
    service.package_root = tmp_path / "bundled-packages"
    for name in ("com.bread.unity-connection", "com.bread.renderdoc"):
        source = service.package_root / name
        write(source / "package.json", {"name": name, "version": "0.1.0"})
        write(source / "Editor/Tool.cs", "original " + name)
        service._install_package(project, source)
    provider = tmp_path / "provider-source"
    write(provider / "MCPForUnity/package.json", {"name": unity.PACKAGE, "version": unity.VERSION})
    write(provider / "MCPForUnity/Editor/Provider.cs", "pinned provider")
    write(provider / "LICENSE", "provider test license")
    service._install_package(project, provider / "MCPForUnity")
    revision = service._package_revision(project)
    write(service._runtime_executable(), "existing runtime")
    write(project / "Library/Bread/unity-connection.json", {
        "enabled": True, "nonce": "old-nonce", "packageRevision": revision, "providerCommit": unity.COMMIT})
    return {"hash": "1234abcd", "id": "Existing@1234abcd", "projectPath": str(project),
            "packageRevision": revision, "nonce": "old-nonce"}


@pytest.mark.parametrize("name", ["com.bread.unity-connection", "com.bread.renderdoc", "loaded-revision"])
def test_connected_old_bread_package_upgrades_before_claiming_reuse(project, service, tmp_path, name):
    identity = managed_bread_packages(project, service, tmp_path)
    if name == "loaded-revision":
        identity["packageRevision"] = "stale-loaded-revision"
        write(project / "Library/Bread/unity-connection.json", {
            "enabled": True, "nonce": "old-nonce", "packageRevision": identity["packageRevision"], "providerCommit": unity.COMMIT})
    old_config = (project / "Library/Bread/unity-connection.json").read_bytes()
    if name != "loaded-revision":
        source_file = service.package_root / name / "Editor/Tool.cs"
        write(source_file, "new bundled implementation")
    provider = tmp_path / "provider-source"
    write(provider / "MCPForUnity/package.json", {"name": unity.PACKAGE, "version": unity.VERSION})
    write(provider / "MCPForUnity/Editor/Provider.cs", "pinned provider")
    write(provider / "LICENSE", "provider test license")
    untouched = write(project / "Assets/User.asset", "user asset")
    def live_identity(project):
        return {**identity, **json.loads((project / "Library/Bread/unity-connection.json").read_text())}

    with mock.patch.object(service, "_probe", side_effect=live_identity), \
            mock.patch.object(service, "inspect_project", return_value={"status": "ready-to-prepare", "editorPath": "existing editor"}), \
            mock.patch.object(service, "_source", return_value=provider) as source, \
            mock.patch.object(service, "_runtime", return_value=service._runtime_executable()), \
            mock.patch.object(service, "_start_editor") as start:
        result = wait_job(service, service.prepare_project(str(project)))
    assert result["status"] == "completed", result
    assert not result["result"].get("reused")
    if name != "loaded-revision":
        assert (project / "Packages" / name / "Editor/Tool.cs").read_text() == "new bundled implementation"
    config = json.loads((project / "Library/Bread/unity-connection.json").read_text())
    assert config["nonce"] != "old-nonce" and config["packageRevision"] != identity["packageRevision"]
    assert result["result"]["instance"]["packageRevision"] == config["packageRevision"]
    assert (project / "Library/Bread/unity-connection.json").read_bytes() != old_config
    assert service._bread_packages_current(project)
    assert untouched.read_text() == "user asset"
    source.assert_called_once()
    start.assert_not_called()


@pytest.mark.parametrize("name", ["com.bread.unity-connection", "com.bread.renderdoc"])
def test_connected_modified_package_is_preserved_before_upgrade_or_download(project, service, tmp_path, name):
    identity = managed_bread_packages(project, service, tmp_path)
    # A stale first package must not short-circuit validation of a modified second.
    write(service.package_root / "com.bread.unity-connection/Editor/Tool.cs", "new bundled implementation")
    changed = write(project / "Packages" / name / "Editor/Tool.cs", "user custom implementation")
    original_config = (project / "Library/Bread/unity-connection.json").read_bytes()
    with mock.patch.object(service, "_probe", return_value=identity), \
            mock.patch.object(service, "inspect_project") as inspect, \
            mock.patch.object(service, "_source") as download, \
            mock.patch.object(service, "_install_package") as install:
        result = wait_job(service, service.prepare_project(str(project)))
    assert result["status"] == "completed"
    assert result["result"]["connected"] is True
    assert result["result"]["packageDiagnostics"]["code"] == "unity-package-modified"
    assert result["result"]["packageDiagnostics"]["retainedInstalledPackages"] is True
    assert changed.read_text() == "user custom implementation"
    assert (project / "Library/Bread/unity-connection.json").read_bytes() == original_config
    inspect.assert_not_called()
    download.assert_not_called()
    install.assert_not_called()


def test_managed_sdk_upgrade_preserves_script_and_folder_guids(project, tmp_path):
    source = tmp_path / "sdk"
    write(source / "package.json", {"name": "com.bread.renderdoc", "version": "0.2.0"})
    write(source / "Runtime/Binding.cs", "// original script")
    unity.UnityIntegrationService._install_package(project, source)
    installed = project / "Packages/com.bread.renderdoc"
    write(installed / "Runtime.meta", "guid: preserved-folder-guid")
    write(installed / "Runtime/Binding.cs.meta", "guid: preserved-script-guid")
    write(installed / "Deleted.cs.meta", "guid: orphan-is-not-a-current-asset")
    write(project / "Packages/manifest.json", {"dependencies": {"com.bread.renderdoc": "0.2.0"}})
    write(source / "package.json", {"name": "com.bread.renderdoc", "version": "0.3.0"})
    write(source / "Runtime/Binding.cs", "// upgraded script")
    unity.UnityIntegrationService._install_package(project, source)
    assert (installed / "Runtime/Binding.cs").read_text() == "// upgraded script"
    assert (installed / "Runtime.meta").read_text() == "guid: preserved-folder-guid"
    assert (installed / "Runtime/Binding.cs.meta").read_text() == "guid: preserved-script-guid"
    assert not (installed / "Deleted.cs.meta").exists()
    assert unity.UnityIntegrationService._package_state(project, source)[3] is True


def test_connected_stale_bread_never_overwrites_a_different_provider_version(project, service, tmp_path):
    identity = managed_bread_packages(project, service, tmp_path)
    installed = project / "Packages/com.bread.renderdoc/Editor/Tool.cs"
    original = installed.read_bytes()
    write(service.package_root / "com.bread.renderdoc/Editor/Tool.cs", "new bundled implementation")
    manifest = write(project / "Packages/manifest.json", {"dependencies": {unity.PACKAGE: "user-chosen-version"}})
    provider = tmp_path / "provider-source"
    write(provider / "MCPForUnity/package.json", {"name": unity.PACKAGE, "version": unity.VERSION})
    original_manifest = manifest.read_bytes()
    with mock.patch.object(service, "_probe", return_value=identity), \
            mock.patch.object(service, "inspect_project", return_value={"status": "ready-to-prepare", "editorPath": "existing editor"}), \
            mock.patch.object(service, "_source", return_value=provider), \
            mock.patch.object(service, "_runtime", return_value=service._runtime_executable()), \
            mock.patch.object(service, "_start_editor") as start:
        result = wait_job(service, service.prepare_project(str(project)))
    assert result["status"] == "action-required"
    assert result["result"]["code"] == "unity-package-conflict"
    assert manifest.read_bytes() == original_manifest
    assert installed.read_bytes() == original
    start.assert_not_called()


@pytest.mark.parametrize("allow_create", [False, True])
def test_import_only_prepares_urp_for_explicit_pipeline_creation(project, service, allow_create):
    request = write(project / "Library/Bread/request.json", {"pipelinePolicy": {"allowCreateUrp": allow_create}})
    replies = ([{"success": True, "data": {"status": "ready"}}] if allow_create else []) + [{"success": True, "data": {"status": "failed"}}]

    async def run():
        with mock.patch.object(service, "prepare_project", return_value={"status": "completed", "result": {"connected": True}}), mock.patch.object(service, "call_tool", new=mock.AsyncMock(side_effect=replies)) as call:
            await service.start_import(str(project), str(request))
            await asyncio.gather(*service._tasks)
            actual = [item.args[1] for item in call.call_args_list]
            assert ("bread_prepare_urp" in actual) is allow_create
            if allow_create:
                assert call.call_args_list[0].args[2] == {"allow_pipeline_creation": True}
    asyncio.run(run())


def test_domain_reload_resumes_only_the_immutable_import_request(project, service):
    identity = {"requestSha256": "a" * 64, "packageManifestSha256": "b" * 64}
    request = write(project / "Library/Bread/request.json", identity)
    completed = {**identity, "status": "imported", "requestFileSha256": hashlib.sha256(request.read_bytes()).hexdigest(),
        "checks": {key: True for key in ("meshImport", "shaderCompile", "materialImport", "textureImport", "sceneIntegrity", "pipelineCompatible")}}
    replies = [{"status": "interrupted", "code": "implementation_compile_pending", "retrySameRequest": True},
               {"status": "failed", "code": "unity-project-not-connected"}, completed]

    async def run():
        with mock.patch.object(service, "prepare_project", return_value={"status": "completed", "result": {"connected": True}}), mock.patch.object(service, "call_tool", new=mock.AsyncMock(side_effect=replies)) as call, mock.patch.object(unity.asyncio, "sleep", new=mock.AsyncMock()):
            result = await service.start_import(str(project), str(request))
            await asyncio.gather(*service._tasks)
            assert service.job_status(result["jobId"])["status"] == "completed"
            assert {item.args[2]["request_path"] for item in call.call_args_list} == {str(request)}
            assert all(item.args[1] == "bread_import_capture" and item.args[2]["action"] == "import" for item in call.call_args_list)
    asyncio.run(run())


def test_existing_mcp_checks_tools_and_live_project_without_preparation(project, service):
    client = mock.Mock(list_tools=mock.AsyncMock(return_value=[{"name": "project_identity"}, {"name": "manage_material"}]), close=mock.AsyncMock())
    identity = {"success": True, "data": {"projectPath": str(project), "unityVersion": "6000.0.55f1", "renderPipeline": "hdrp"}}
    client.call = mock.AsyncMock(side_effect=[identity, identity, {"success": True}, identity])

    async def run():
        with mock.patch("src.mcp_tool_client.shared_client", return_value=client), mock.patch.object(service, "prepare_project") as prepare:
            result = await service.connect_existing_mcp(str(project), {"kind": "mcp-http", "url": "http://127.0.0.1:8080/mcp"}, "project_identity", required_tools=["manage_material"])
            assert result["connected"] and result["renderPipeline"] == "hdrp"
            assert result["packagesModified"] is False and result["managedImportAvailable"] is False
            assert (await service.call_tool(str(project), "manage_material", {"action": "inspect"}))["success"]
            prepare.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("fault,expected", [("wrong-project", "unity-project-identity-mismatch"), ("missing-tool", "unity-required-tools-unavailable")])
def test_existing_mcp_cannot_claim_missing_project_or_capabilities(project, service, fault, expected):
    client = mock.Mock(list_tools=mock.AsyncMock(return_value=[{"name": "identity"}]), close=mock.AsyncMock())
    client.call = mock.AsyncMock(return_value={"projectPath": str(project / "Other") if fault == "wrong-project" else str(project), "unityVersion": "6000.0.55f1"})

    async def run():
        with mock.patch("src.mcp_tool_client.shared_client", return_value=client):
            result = await service.connect_existing_mcp(str(project), {"kind": "mcp-http", "url": "https://example.test/mcp"}, "identity", required_tools=["manage_material"])
            assert result["code"] == expected
            assert not service._external_connections
            client.close.assert_awaited_once()
    asyncio.run(run())


def test_existing_mcp_project_switch_is_detected_before_the_next_mutation(project, service):
    client = mock.Mock(list_tools=mock.AsyncMock(return_value=[{"name": "identity"}, {"name": "manage_material"}]), close=mock.AsyncMock())
    client.call = mock.AsyncMock(side_effect=[{"projectPath": str(project), "unityVersion": "6000.0.55f1"},
        {"projectPath": str(project / "Other"), "unityVersion": "6000.0.55f1"}])

    async def run():
        with mock.patch("src.mcp_tool_client.shared_client", return_value=client):
            assert (await service.connect_existing_mcp(str(project), {"kind": "mcp-http", "url": "http://localhost:8080/mcp"}, "identity"))["connected"]
            result = await service.call_tool(str(project), "manage_material", {})
            assert result["code"] == "unity-project-identity-mismatch"
            assert [item.args[1] for item in client.call.call_args_list] == ["identity", "identity"]
    asyncio.run(run())


@pytest.mark.parametrize("platform_name,machine", [("win32", "AMD64"), ("win32", "ARM64"), ("darwin", "x86_64"), ("darwin", "arm64"), ("linux", "x86_64"), ("linux", "aarch64")])
def test_frozen_runtime_bootstrap_has_pinned_binaries_for_supported_platforms(project, service, tmp_path, platform_name, machine):
    downloads = []

    def extract(archive, output):
        suffix = "uv.exe" if platform_name == "win32" else "uv"
        write(output / f"uv-{unity.UV_VERSION}.data/scripts/{suffix}", "verified binary")

    def run(*args):
        write(service._runtime_executable(), "runtime")

    with mock.patch.object(unity.sys, "platform", platform_name), mock.patch.object(unity.platform, "machine", return_value=machine), mock.patch.object(unity.shutil, "which", return_value=None), mock.patch.object(unity, "_download", side_effect=lambda url, target, sha, cancel: downloads.append((url, sha))), mock.patch.object(unity, "_extract_zip", side_effect=extract), mock.patch.object(service, "_run", side_effect=run):
        result = service._runtime(tmp_path / "source", unity._Job("runtime", str(project)))
        assert result.is_file()
        assert len(downloads) == 1 and len(downloads[0][1]) == 64
        assert downloads[0][0].startswith("https://files.pythonhosted.org/")


@pytest.mark.parametrize("platform_name,relative", [("win32", "Editor/Unity.exe"), ("darwin", "Unity.app/Contents/MacOS/Unity"), ("linux", "Editor/Unity")])
def test_custom_editor_roots_cover_all_host_platforms(tmp_path, monkeypatch, platform_name, relative):
    root = tmp_path / "editors"
    editor = write(root / "6000.0.55f1" / relative, "editor")
    monkeypatch.setenv("BREAD_UNITY_EDITOR_ROOTS", str(root))
    with mock.patch.object(unity.sys, "platform", platform_name):
        assert {"path": str(editor.resolve()), "version": "6000.0.55f1"} in unity.discover_editors()
