"""Run the real Shader visual GPU acceptance through a remote ReplayProxy.

Requires a separately started matching ``render_soco_doccmd remoteserver``.
Use --server-url (or BREAD_SHADER_VISUAL_REMOTE_URL) for hostname:port, and the
same input/output arguments or environment as qrenderdoc_shader_visual_debug_smoke.
The fixture copies each RDC to the server, opens it with IRemoteServer.OpenCapture,
and reuses every local pixel/isolation/restoration assertion. There is no local
replay fallback. Closing a test closes its capture and connection, not the server.
Compatible with qrenderdoc's Python 3.6 runtime.
"""
from __future__ import print_function

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import traceback

import renderdoc as rd


class _RemoteController:
    def __init__(self, capture, controller):
        self.capture = capture
        self.controller = controller

    def __getattr__(self, name):
        return getattr(self.controller, name)

    def Shutdown(self):
        if self.controller is not None:
            controller, self.controller = self.controller, None
            self.capture.connection.CloseCapture(controller)
            self.capture.controller = None
            self.capture.evidence["remoteCaptureClosed"] = True


class _RemoteCapture:
    def __init__(self, endpoint, evidence):
        self.endpoint = endpoint
        self.evidence = evidence
        self.connection = None
        self.controller = None
        self.remote_path = ""

    def OpenFile(self, filename, filetype, progress):
        if filetype:
            raise RuntimeError("Remote smoke accepts native RDC files only")
        # ShutdownConnection returns before the server's client thread has fully
        # retired. Retry only that bounded handover race between our captures.
        deadline = time.monotonic() + 5.0
        attempts = 0
        while True:
            attempts += 1
            status, self.connection = rd.CreateRemoteServerConnection(self.endpoint)
            if status != rd.ResultCode.NetworkRemoteBusy or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        self.evidence["connectionAttempts"] = attempts
        if status != rd.ResultCode.Succeeded or self.connection is None:
            raise RuntimeError("Remote server connection failed: " + str(status))
        self.evidence["remoteSupportedReplays"] = list(self.connection.RemoteSupportedReplays())
        self.evidence["localProxies"] = list(self.connection.LocalProxies())
        self.remote_path = self.connection.CopyCaptureToRemote(filename, progress)
        if not self.remote_path:
            raise RuntimeError("CopyCaptureToRemote returned an empty path")
        self.evidence["remoteCapture"] = self.remote_path
        # CopyCaptureToRemote already registers the temporary server copy for cleanup;
        # the user's source RDC is untouched.
        return rd.ResultCode.Succeeded

    def OpenCapture(self, options, progress):
        status, native = self.connection.OpenCapture(
            rd.RemoteServer.NoPreference, self.remote_path, options, progress)
        if status == rd.ResultCode.Succeeded and native is not None:
            self.controller = _RemoteController(self, native)
            self.evidence["remoteCaptureOpened"] = True
            return status, self.controller
        return status, None

    def Shutdown(self):
        if self.connection is not None:
            try:
                if self.controller is not None:
                    self.controller.Shutdown()
            finally:
                connection, self.connection = self.connection, None
                connection.ShutdownConnection()
                self.evidence["remoteConnectionClosed"] = True


class _RemoteModule:
    def __init__(self, endpoint, evidence):
        self.endpoint = endpoint
        self.evidence = evidence

    def __getattr__(self, name):
        return getattr(rd, name)

    def OpenCaptureFile(self):
        return _RemoteCapture(self.endpoint, self.evidence)


def _local_fixture():
    path = Path(__file__).resolve().with_name("qrenderdoc_shader_visual_debug_smoke.py")
    spec = importlib.util.spec_from_file_location("_bread_visual_remote_assertions", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=os.environ.get("BREAD_SHADER_VISUAL_REMOTE_URL"))
    parser.add_argument("--capture-results", default=os.environ.get("BREAD_SHADER_VISUAL_SMOKE_INPUT"))
    parser.add_argument("--capture", default=os.environ.get("BREAD_SHADER_VISUAL_SMOKE_CAPTURE"))
    parser.add_argument("--output", default=os.environ.get("BREAD_SHADER_VISUAL_SMOKE_OUTPUT"))
    args = parser.parse_args()
    if not args.server_url or not args.output or not (args.capture_results or args.capture):
        parser.error("Provide an explicit remote endpoint, capture input, and output report path")
    directory = Path.cwd()
    entries = [{"capture": args.capture, "ok": True}] if args.capture else []
    if args.capture_results:
        manifest = Path(args.capture_results).resolve()
        directory = manifest.parent
        entries += json.loads(manifest.read_text(encoding="utf-8"))["captures"]
    report = {"schema": "bread-shader-visual-remote-gpu-smoke/v1", "ok": False,
              "python": sys.version, "serverURL": args.server_url, "captures": []}
    fixture = _local_fixture()
    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        for entry in entries:
            evidence = {"transport": "IRemoteServer.OpenCapture/ReplayProxy",
                        "serverURL": args.server_url, "remoteCaptureOpened": False}
            # The adapter is private to this imported fixture, never the global rd module.
            fixture.rd = _RemoteModule(args.server_url, evidence)
            try:
                result = fixture.validate_capture(entry, directory)
            except Exception:
                result = {"capture": entry.get("capture"), "ok": False,
                          "error": traceback.format_exc()}
            result["remote"] = evidence
            result["ok"] = bool(result["ok"] and evidence.get("remoteCaptureOpened") and
                                evidence.get("remoteCaptureClosed") and
                                evidence.get("remoteConnectionClosed"))
            report["captures"].append(result)
            Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["ok"] = bool(report["captures"]) and all(item["ok"] for item in report["captures"])
    finally:
        rd.ShutdownReplay()
        Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("BREAD_SHADER_VISUAL_REMOTE_SMOKE=" + json.dumps({"ok": report["ok"], "output": args.output}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
