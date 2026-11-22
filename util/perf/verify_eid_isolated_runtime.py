"""Verify the local native/Python identities and config isolation without opening a capture."""
from __future__ import print_function

import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys


def digest(path):
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def module_path(name):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
    kernel.GetModuleFileNameW.restype = ctypes.c_uint
    handle = kernel.GetModuleHandleW(name)
    if name is not None and not handle:
        raise RuntimeError("Expected loaded module missing: " + name)
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetModuleFileNameW(handle, buffer, len(buffer))
    if not length or length >= len(buffer):
        raise RuntimeError("Cannot identify loaded module")
    return Path(buffer.value).resolve()


def main():
    root = Path(__file__).resolve().parents[2]
    output = (Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else
              root / "output" / "eid-perf-20260908" / "runtime-isolation.json")
    output.relative_to(root)
    config = Path(os.environ["RENDERDOC_CONFIG_DIR"]).resolve()
    config.relative_to(root)
    global_config = Path(os.environ["APPDATA"]) / "renderdoc" / "renderdoc.conf"
    global_before = digest(global_config)
    import renderdoc as rd
    paths = {"host": module_path(None), "core": module_path("renderdoc.dll"),
             "python": module_path("python36.dll"), "binding": Path(rd.__file__).resolve()}
    for path in paths.values():
        path.relative_to(root)
    local_config = config / "renderdoc.conf"
    if not local_config.is_file():
        raise RuntimeError("Native initialization did not create the isolated configuration")
    if global_before != digest(global_config):
        raise RuntimeError("Global RenderDoc configuration changed")
    report = {
        "ok": True, "captureOpened": False, "gpuReplayRun": False,
        "pythonVersion": sys.version, "version": rd.GetVersionString(),
        "commit": rd.GetCommitHash(), "config": str(local_config),
        "globalConfigUnchanged": True, "globalConfigSha256": global_before,
        "paths": {key: str(value) for key, value in paths.items()},
        "sha256": {key: digest(value) for key, value in paths.items()},
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "report": str(output), "gpuReplayRun": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
