"""Create persistent demo captures through the local RenderDoc target-control API.

Run with matching standalone Python, run_bundled_replay_python.ps1, or qrenderdoc --python.
No desktop automation or replay is performed. Demo windows may be created by the APIs.
"""
from __future__ import print_function

import argparse
import json
import ntpath
import os
from pathlib import Path
import signal
import sys
import time
import traceback


def renderdoc_module():
    # Keep process-identity and child-environment tests CPU-only without a native binding import.
    import renderdoc
    return renderdoc


def windows_image_path(path):
    path = str(path)
    if path.startswith("\\\\?\\"):
        path = path[4:]
    return ntpath.normcase(ntpath.normpath(path))


class WindowsProcessAPI:
    """Small typed Win32 surface; all identity and termination operations use one handle."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        declarations = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "GetProcessId": ([wintypes.HANDLE], wintypes.DWORD),
            "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "GetSystemTimeAsFileTime": ([ctypes.POINTER(wintypes.FILETIME)], None),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def ticks(value):
        return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

    def now(self):
        value = self.wintypes.FILETIME()
        self.kernel.GetSystemTimeAsFileTime(self.ctypes.byref(value))
        return self.ticks(value)

    def require(self, result):
        if not result:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return result

    def open(self, pid):
        # QUERY_LIMITED_INFORMATION | TERMINATE | SYNCHRONIZE. No inherited handle.
        return self.require(self.kernel.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, pid))

    def identity(self, handle):
        pid = self.require(self.kernel.GetProcessId(handle))
        capacity = self.wintypes.DWORD(32768)
        image = self.ctypes.create_unicode_buffer(capacity.value)
        self.require(self.kernel.QueryFullProcessImageNameW(handle, 0, image, self.ctypes.byref(capacity)))
        times = [self.wintypes.FILETIME() for _ in range(4)]
        self.require(self.kernel.GetProcessTimes(handle, *[self.ctypes.byref(value) for value in times]))
        return pid, image.value, self.ticks(times[0])

    def wait(self, handle, milliseconds):
        result = self.kernel.WaitForSingleObject(handle, milliseconds)
        if result == 0:
            return True
        if result == 258:
            return False
        raise self.ctypes.WinError(self.ctypes.get_last_error())

    def terminate(self, handle):
        self.require(self.kernel.TerminateProcess(handle, 1))

    def exit_code(self, handle):
        code = self.wintypes.DWORD()
        self.require(self.kernel.GetExitCodeProcess(handle, self.ctypes.byref(code)))
        return code.value

    def close(self, handle):
        self.require(self.kernel.CloseHandle(handle))


class OwnedWindowsProcess:
    def __init__(self, api, pid, executable, earliest, latest):
        self.api, self.handle = api, None
        if pid <= 0 or earliest > latest:
            raise RuntimeError("Invalid target PID or launch time window")
        handle = api.open(pid)
        try:
            observed_pid, image, created = api.identity(handle)
            if observed_pid != pid or windows_image_path(image) != windows_image_path(executable):
                raise RuntimeError("Target-control process identity does not match the requested demo executable")
            if not earliest <= created <= latest:
                raise RuntimeError("Target-control process creation time is outside this ExecuteAndInject window")
            self.receipt = {"mode": "retained-windows-process-handle", "verified": True,
                            "pid": observed_pid, "imagePath": image, "createdFileTime100ns": created,
                            "launchEarliestFileTime100ns": earliest, "launchLatestFileTime100ns": latest}
            self.handle = handle
        except Exception:
            api.close(handle)
            raise

    def cleanup(self):
        if self.handle is None:
            raise RuntimeError("Owned process handle already closed")
        handle, self.handle = self.handle, None
        try:
            if self.api.wait(handle, 0):
                action = "alreadyExited"
            else:
                # If exit raced the first wait, an unsuccessful terminate is accepted only when
                # that same retained handle is now signalled. Never reopen a possibly reused PID.
                try:
                    self.api.terminate(handle)
                except OSError:
                    if not self.api.wait(handle, 0):
                        raise
                action = "terminatedOwnedHandle"
            if not self.api.wait(handle, 5000):
                raise RuntimeError("Owned demo did not exit within the cleanup wait")
            self.receipt.update(cleanupAction=action, exitVerified=True, exitCode=self.api.exit_code(handle))
            return self.receipt
        finally:
            self.api.close(handle)


def validate_live_forward_fixture(enabled, apis, demo):
    if enabled and (list(apis) != ["D3D12"] or demo != "D3D12_RenderTarget_Binds"):
        raise ValueError("--live-forward-fixture requires --apis D3D12 --d3d12-demo D3D12_RenderTarget_Binds")


def validate_clear_fixture(enabled, apis, demo):
    if enabled and (list(apis) != ["D3D12"] or demo != "D3D12_Checkpoint_Triangle"):
        raise ValueError("--checkpoint-clear-fixture requires --apis D3D12 --d3d12-demo D3D12_Checkpoint_Triangle")


def validate_alias_fixture(mode, apis, demo, clear_fixture=False):
    if mode not in ("off", "clear", "read", "split"):
        raise ValueError("Unknown checkpoint alias fixture mode")
    if mode != "off":
        if list(apis) != ["D3D12"] or demo != "D3D12_Checkpoint_Triangle":
            raise ValueError("--checkpoint-alias-fixture requires --apis D3D12 "
                             "--d3d12-demo D3D12_Checkpoint_Triangle")
        if clear_fixture:
            raise ValueError("--checkpoint-alias-fixture and --checkpoint-clear-fixture are mutually exclusive")


def alias_fixture_environment(rd, mode):
    modification = rd.EnvironmentModification()
    modification.name = "RENDERDOC_CHECKPOINT_ALIAS_FIXTURE"
    # Disable inherited variants unless the current invocation explicitly selects one.
    modification.value = mode
    modification.mod, modification.sep = rd.EnvMod.Set, rd.EnvSep.NoSep
    return modification


def fixture_environment(rd, enabled):
    modification = rd.EnvironmentModification()
    modification.name = "RENDERDOC_LIVE_FORWARD_FIXTURE"
    # Explicitly clear inherited fixture state when not requested.
    modification.value = "1" if enabled else "0"
    modification.mod, modification.sep = rd.EnvMod.Set, rd.EnvSep.NoSep
    return modification


def prepare_vulkan_layer(source_path, library_path, output):
    """Select one implicit capture layer for this child process only.

    This fork injects system_load.dll, but its Vulkan objects are built from the same
    static library as renderdoc.dll and still use VK_LAYER_RENDERDOC_Capture.
    Loading a second DLL's layer would connect Vulkan to a different capturer.
    """
    source_path, library_path = Path(source_path).resolve(), Path(library_path).resolve()
    manifest = json.loads(source_path.read_text(encoding="utf-8-sig"))
    layer = manifest["layer"]
    if layer["name"] != "VK_LAYER_RENDERDOC_Capture":
        raise RuntimeError("Vulkan manifest must describe the compiled RenderDoc layer name")
    if not library_path.is_file():
        raise RuntimeError("Injected Vulkan layer DLL is missing: " + str(library_path))
    if os.name == "nt":
        import ctypes
        if ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError("Run this smoke helper without elevation: Vulkan ignores child layer paths when elevated")
    layer["library_path"] = str(library_path)
    implicit_dir, explicit_dir = output / "vulkan-implicit", output / "vulkan-explicit-empty"
    implicit_dir.mkdir()
    explicit_dir.mkdir()
    # Keep an implicit layer's required disable switch, isolated from replay-host switches.
    disable_name = "DISABLE_EID_API_SMOKE_LAYER_" + output.name.replace("-", "_")
    layer["disable_environment"] = {disable_name: "1"}
    generated_path = implicit_dir / "renderdoc-smoke.json"
    generated_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    child_environment = {
        "VK_IMPLICIT_LAYER_PATH": str(implicit_dir),
        "VK_LAYER_PATH": str(explicit_dir),
        "VK_INSTANCE_LAYERS": "",
        "VK_LOADER_DEBUG": "error,warn,layer",
    }
    child_environment.update(layer["enable_environment"])
    return child_environment, {
        "sourceManifest": str(source_path), "generatedManifest": str(generated_path),
        "library": str(library_path), "layerName": layer["name"],
        "implementationVersion": layer["implementation_version"],
        "mode": "child-only-implicit-layer", "environment": child_environment,
    }


def capture_one(api, demo, output, timeout, frame, compiler_dir, layer_manifest, layer_library,
                d3d12_demo="D3D12_Simple_Triangle", d3d11_demo="D3D11_Simple_Triangle",
                gl_demo="GL_Simple_Triangle", vk_demo="VK_Simple_Triangle", live_forward_fixture=False,
                portable_forward_fixture=False, checkpoint_clear_fixture=False,
                checkpoint_alias_fixture="off", vk_buffer_delta="static"):
    rd = renderdoc_module()
    tests = {"D3D11": d3d11_demo, "OpenGL": gl_demo, "OpenGLES": gl_demo,
             "Vulkan": vk_demo, "D3D12": d3d12_demo}
    expected = {"D3D11": "D3D11", "OpenGL": "OpenGL", "OpenGLES": "OpenGLES",
                "Vulkan": "Vulkan", "D3D12": "D3D12"}
    entry = {"api": api, "demo": tests[api], "ok": False}
    control = None
    process_id = 0
    owned_process = None
    started = time.monotonic()
    try:
        validate_live_forward_fixture(live_forward_fixture, [api], tests[api])
        validate_clear_fixture(checkpoint_clear_fixture, [api], tests[api])
        validate_alias_fixture(checkpoint_alias_fixture, [api], tests[api], checkpoint_clear_fixture)
        log_path = output / (tests[api] + ".log")
        command = '{} --width 400 --height 300 --log "{}"'.format(tests[api], log_path)
        envmods = [fixture_environment(rd, live_forward_fixture)]
        portable = rd.EnvironmentModification()
        portable.name = "RENDERDOC_PORTABLE_FORWARD_FIXTURE"
        portable.value = "1" if portable_forward_fixture else "0"
        portable.mod, portable.sep = rd.EnvMod.Set, rd.EnvSep.NoSep
        envmods.append(portable)
        clear_fixture = rd.EnvironmentModification()
        clear_fixture.name = "RENDERDOC_CHECKPOINT_CLEAR_FIXTURE"
        clear_fixture.value = "1" if checkpoint_clear_fixture else "0"
        clear_fixture.mod, clear_fixture.sep = rd.EnvMod.Set, rd.EnvSep.NoSep
        envmods.append(clear_fixture)
        alias_fixture = alias_fixture_environment(rd, checkpoint_alias_fixture)
        envmods.append(alias_fixture)
        entry["childEnvironment"] = {"RENDERDOC_LIVE_FORWARD_FIXTURE": "1" if live_forward_fixture else "0"}
        entry["liveForwardFixture"] = bool(live_forward_fixture)
        entry["childEnvironment"][portable.name] = portable.value
        entry["portableForwardFixture"] = bool(portable_forward_fixture)
        entry["childEnvironment"][clear_fixture.name] = clear_fixture.value
        entry["checkpointClearFixture"] = bool(checkpoint_clear_fixture)
        entry["childEnvironment"][alias_fixture.name] = alias_fixture.value
        entry["checkpointAliasFixture"] = checkpoint_alias_fixture
        for name, enabled in (("RENDERDOC_VK_BUFFER_DELTA_DYNAMIC", vk_buffer_delta in ("dynamic", "dynamic-stages")),
                              ("RENDERDOC_VK_BUFFER_DELTA_SEPARATE", vk_buffer_delta == "separate"),
                              ("RENDERDOC_VK_BUFFER_DELTA_STAGES", vk_buffer_delta in ("stages", "dynamic-stages"))):
            modification = rd.EnvironmentModification()
            modification.name, modification.value = name, "1" if enabled else "0"
            modification.mod, modification.sep = rd.EnvMod.Set, rd.EnvSep.NoSep
            envmods.append(modification)
            entry["childEnvironment"][name] = modification.value
        if api == "OpenGLES":
            modification = rd.EnvironmentModification()
            modification.name, modification.value = "RENDERDOC_SHARED_BUFFER_GLES", "1"
            modification.mod, modification.sep = rd.EnvMod.Set, rd.EnvSep.NoSep
            envmods.append(modification)
        if api == "Vulkan":
            layer_environment, layer_info = prepare_vulkan_layer(layer_manifest, layer_library, output)
            entry["childVulkanLayer"] = layer_info
            for name, value in sorted(layer_environment.items()):
                modification = rd.EnvironmentModification()
                modification.name, modification.value = name, value
                modification.mod, modification.sep = rd.EnvMod.Set, rd.EnvSep.NoSep
                envmods.append(modification)
        if compiler_dir:
            modification = rd.EnvironmentModification()
            modification.name, modification.value = "PATH", compiler_dir
            modification.mod, modification.sep = rd.EnvMod.Prepend, rd.EnvSep.Platform
            envmods.append(modification)
            entry["childCompilerDirectory"] = compiler_dir
        process_api = WindowsProcessAPI() if os.name == "nt" else None
        launch_before = process_api.now() if process_api else None
        result = rd.ExecuteAndInject(str(demo), str(demo.parent), command, envmods,
                                     str(output / tests[api]), rd.CaptureOptions(), False)
        launch_after = process_api.now() if process_api else None
        if process_api:
            entry["launchWindow"] = {"earliestFileTime100ns": launch_before,
                                     "latestFileTime100ns": launch_after}
        entry["injectResult"] = str(result.result)
        if result.result != rd.ResultCode.Succeeded:
            raise RuntimeError("ExecuteAndInject failed: " + str(result.result))
        control = rd.CreateTargetControl("localhost", result.ident, "eid-api-smoke", True)
        if control is None:
            raise RuntimeError("Target-control connection was not created")
        process_id = control.GetPID()
        entry["pid"] = process_id
        if process_api:
            owned_process = OwnedWindowsProcess(process_api, process_id, str(demo), launch_before, launch_after)
            entry["ownedProcess"] = owned_process.receipt
        control.QueueCapture(frame, 1)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = control.ReceiveMessage(None)
            if message.type == rd.TargetControlMessageType.NewCapture:
                captured = message.newCapture
                path = Path(captured.path).resolve()
                if not captured.local or output not in path.parents:
                    raise RuntimeError("Capture was not saved under the requested local output")
                if not path.is_file() or path.stat().st_size == 0:
                    raise RuntimeError("NewCapture reported a missing or empty capture")
                capture_file = rd.OpenCaptureFile()
                try:
                    status = capture_file.OpenFile(str(path), "", None)
                    if status != rd.ResultCode.Succeeded:
                        raise RuntimeError("Capture OpenFile failed: " + str(status))
                    driver = capture_file.DriverName()
                finally:
                    capture_file.Shutdown()
                entry.update({"capture": str(path), "driver": driver,
                              "reportedAPI": captured.api, "frameNumber": captured.frameNumber,
                              "bytes": path.stat().st_size})
                if driver != expected[api]:
                    raise RuntimeError("Captured driver {} does not match {}".format(driver, expected[api]))
                entry["ok"] = True
                return entry
            if message.type == rd.TargetControlMessageType.Disconnected or not control.Connected():
                raise RuntimeError("Demo disconnected before a capture; inspect " + str(log_path))
        raise RuntimeError("Capture timed out; inspect " + str(log_path))
    except Exception as error:
        entry.update({"error": str(error), "traceback": traceback.format_exc()})
        return entry
    finally:
        if control is not None:
            try:
                control.Shutdown()
            except Exception as error:
                entry.setdefault("cleanupErrors", []).append("TargetControl shutdown: " + str(error))
                entry["ok"] = False
        if owned_process is not None:
            try:
                entry["ownedProcess"] = owned_process.cleanup()
            except Exception as error:
                entry.setdefault("cleanupErrors", []).append("Owned process cleanup: " + str(error))
                entry["ok"] = False
        elif os.name == "nt" and process_id > 0:
            entry["processCleanup"] = "refused-unverified-windows-process"
        elif os.name != "nt" and process_id > 0:
            # Preserve the pre-existing POSIX behavior. The retained-handle ownership guarantee
            # above is Windows-specific; never fall back to this PID operation on Windows.
            try:
                os.kill(process_id, signal.SIGTERM)
                entry["processCleanup"] = "posix-target-control-pid-signal"
            except ProcessLookupError:
                entry["processCleanup"] = "posix-already-exited"
            except OSError as error:
                entry.setdefault("cleanupErrors", []).append("POSIX cleanup: " + str(error))
                entry["ok"] = False
        entry["elapsedMs"] = round((time.monotonic() - started) * 1000, 3)


def main():
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demos", default=os.environ.get("RENDERDOC_API_SMOKE_DEMOS", str(repo / "util/test/demos_x64.exe")))
    parser.add_argument("--output", default=os.environ.get("RENDERDOC_API_SMOKE_OUTPUT", str(repo / "output/eid-api-smoke")))
    parser.add_argument("--apis", default=os.environ.get("RENDERDOC_API_SMOKE_APIS", "D3D11,OpenGL,Vulkan"))
    parser.add_argument("--d3d12-demo", choices=("D3D12_Simple_Triangle", "D3D12_Checkpoint_Triangle", "D3D12_Checkpoint_Query", "D3D12_Shared_Buffer", "D3D12_CBuffer_Zoo", "D3D12_RenderTarget_Binds", "D3D12_UAV_Declarations", "D3D12_Sparse_Initial_Residency", "D3D12_Checkpoint_Buffer_Alias", "D3D12_Checkpoint_Typed_Alias", "D3D12_Buffer_Delta_Ranges", "D3D12_Buffer_CPU_Dirty_Ranges"),
                        default="D3D12_Simple_Triangle")
    parser.add_argument("--live-forward-fixture", action="store_true",
                        help="Enable per-frame RT clears in the D3D12_RenderTarget_Binds proof fixture")
    parser.add_argument("--portable-forward-fixture", action="store_true",
                        help="Request the four-region single-pass VK_Shared_Buffer proof fixture")
    parser.add_argument("--checkpoint-clear-fixture", action="store_true",
                        help="Fully clear each closed submission in D3D12_Checkpoint_Triangle")
    parser.add_argument("--checkpoint-alias-fixture", choices=("off", "clear", "read", "split"),
                        default="off", help="Committed RT clear after an unrelated alias barrier; "
                        "read and split select conservative negative cases")
    parser.add_argument("--d3d11-demo", choices=("D3D11_Simple_Triangle", "D3D11_Shared_Buffer", "D3D11_CBuffer_Zoo", "D3D11_Buffer_Delta", "D3D11_Buffer_Delta_Ranges", "D3D11_Checkpoint_Stages"), default="D3D11_Simple_Triangle")
    parser.add_argument("--gl-demo", choices=("GL_Simple_Triangle", "GL_Shared_Buffer", "GL_CBuffer_Zoo", "GL_Buffer_Delta", "GL_Buffer_Delta_Ranges", "GLES_Buffer_Delta", "GLES_Buffer_Delta_Ranges", "GL_Checkpoint_Texture_Versions", "GLES_Checkpoint_Texture_Versions"), default="GL_Simple_Triangle")
    parser.add_argument("--vk-demo", choices=("VK_Simple_Triangle", "VK_Shared_Buffer", "VK_CBuffer_Zoo", "VK_Buffer_Delta"), default="VK_Simple_Triangle")
    parser.add_argument("--vk-buffer-delta", choices=("static", "dynamic", "separate", "stages", "dynamic-stages"), default="static")
    parser.add_argument("--compiler-dir", default=os.environ.get("RENDERDOC_API_SMOKE_COMPILER_DIR", ""))
    parser.add_argument("--vulkan-layer-manifest", default=os.environ.get(
        "RENDERDOC_API_SMOKE_VULKAN_LAYER_MANIFEST", str(repo / "x64/Development/renderdoc.json")))
    parser.add_argument("--vulkan-layer-library", default=os.environ.get(
        "RENDERDOC_API_SMOKE_VULKAN_LAYER_LIBRARY", str(repo / "x64/Development/system_load.dll")))
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--frame", type=int, default=5)
    embedded_qt = "pyrenderdoc" in globals()
    args = parser.parse_args([] if embedded_qt else None)
    apis = args.apis.split(",")
    if any(api not in ("D3D11", "D3D12", "OpenGL", "OpenGLES", "Vulkan") for api in apis):
        parser.error("--apis accepts D3D11,D3D12,OpenGL,OpenGLES,Vulkan")
    if "OpenGLES" in apis and args.gl_demo not in ("GL_Shared_Buffer", "GLES_Buffer_Delta", "GLES_Buffer_Delta_Ranges", "GLES_Checkpoint_Texture_Versions"):
        parser.error("OpenGLES requires a demo that explicitly requests an ES context")
    if args.portable_forward_fixture and (apis != ["Vulkan"] or args.vk_demo != "VK_Shared_Buffer"):
        parser.error("--portable-forward-fixture requires --apis Vulkan --vk-demo VK_Shared_Buffer")
    try:
        validate_live_forward_fixture(args.live_forward_fixture, apis, args.d3d12_demo)
        validate_clear_fixture(args.checkpoint_clear_fixture, apis, args.d3d12_demo)
        validate_alias_fixture(args.checkpoint_alias_fixture, apis, args.d3d12_demo,
                               args.checkpoint_clear_fixture)
    except ValueError as error:
        parser.error(str(error))
    if args.frame < 1 or args.timeout <= 0:
        parser.error("Frame and timeout must be positive")
    demo = Path(args.demos).resolve()
    if not demo.is_file():
        parser.error("Build the demo executable first: " + str(demo))
    compiler_dir = str(Path(args.compiler_dir).resolve()) if args.compiler_dir else ""
    if compiler_dir and not any((Path(compiler_dir) / name).is_file()
                                for name in ("glslc.exe", "glslangValidator.exe")):
        parser.error("Compiler directory needs glslc.exe or glslangValidator.exe")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Preserve every run and capture; unlike the full test harness, nothing is deleted.
    run_output = output / time.strftime("%Y%m%d-%H%M%S")
    run_output.mkdir(exist_ok=False)
    result = {"schema": "renderdoc-api-smoke-capture/v1", "ok": False,
              "python": sys.version, "demo": str(demo), "liveForwardFixture": args.live_forward_fixture,
              "captures": []}
    rd = renderdoc_module()
    if not embedded_qt:
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        for api in apis:
            result["captures"].append(capture_one(api, demo, run_output, args.timeout, args.frame,
                                                compiler_dir, args.vulkan_layer_manifest,
                                                args.vulkan_layer_library, args.d3d12_demo,
                                                args.d3d11_demo, args.gl_demo, args.vk_demo,
                                                args.live_forward_fixture, args.portable_forward_fixture,
                                                args.checkpoint_clear_fixture,
                                                args.checkpoint_alias_fixture, args.vk_buffer_delta))
            (run_output / "capture-results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        result["ok"] = all(entry["ok"] for entry in result["captures"])
    finally:
        if not embedded_qt:
            rd.ShutdownReplay()
    (run_output / "capture-results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    status = main()
    if "pyrenderdoc" not in globals():
        sys.exit(status)
