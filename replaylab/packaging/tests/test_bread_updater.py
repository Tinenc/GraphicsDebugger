import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from zipfile import ZipFile

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "qrenderdoc/Resources/ApplyBreadUpdate.ps1"
MANIFEST = "RenderDocBread.manifest.json"
EXE = "RenderDocBread.exe"
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Uses the Windows portable updater")


def manifest(version, files):
    return json.dumps({
        "schema": "renderdocbread-release/v1", "product": "RenderDocBread",
        "platform": "windows-x64", "profile": "standard", "breadVersion": version,
        "files": [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in files.items()],
    }).encode()


@pytest.fixture
def launcher(tmp_path):
    # A windowless executable confirms the updater really starts the intended install.
    code = tmp_path / "launcher.cs"
    exe = tmp_path / "launcher.exe"
    code.write_text(
        'class Launcher { static void Main(string[] args) {'
        'if (args.Length == 2 && args[0] == "--hold-file") {'
        'using (System.IO.File.Open(args[1], System.IO.FileMode.Open,'
        'System.IO.FileAccess.Read, System.IO.FileShare.Read)) {'
        'System.IO.File.WriteAllText("worker-ready.txt", "ready");'
        'System.Threading.Thread.Sleep(60000); } return; }'
        'System.IO.File.WriteAllLines("restart.txt", args); } }',
        encoding="ascii",
    )
    windows = Path(os.environ["SystemRoot"])
    csc = windows / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run([csc, "/nologo", "/target:winexe", f"/out:{exe}", code],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return exe.read_bytes()


@contextlib.contextmanager
def locked_file(path):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = create(str(path), 0x80000000, 1, None, 3, 0, None)  # Read sharing, no delete sharing.
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.parametrize("failure", [None, "hash", "collision", "locked", "traversal"])
def test_update_preserves_data_and_recovers_from_failure(tmp_path, launcher, failure):
    app = tmp_path / "\u4fbf\u643a\u7248 Bread"
    stage = app / ".bread-update-test"
    stage.mkdir(parents=True)
    shutil.copyfile(SCRIPT, stage / SCRIPT.name)
    (stage / SCRIPT.name).chmod(stat.S_IREAD)  # QFile::copy retains the Qt resource permissions.
    shared = {EXE: launcher, "RenderDocBread.portable": b"portable"}
    old = {**shared, "renderdoc.dll": b"old", "tools/obsolete.txt": b"obsolete"}
    new = {**shared, "renderdoc.dll": b"new", "tools/new.txt": b"replacement"}
    personal = {
        "RenderDoc.config": b"user layout and preferences",
        "projects/example.rdc": b"capture",
        "replaylab/chat/history.json": b"saved conversation",
    }
    if failure == "collision":
        personal["tools/new.txt"] = b"personal file with conflicting name"
    for relative, data in {**old, **personal}.items():
        path = app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    old_manifest = manifest("1.13.127", old)
    (app / MANIFEST).write_bytes(old_manifest)
    if failure == "traversal":
        new["../escape.txt"] = b"must never be extracted"

    new_manifest = manifest("1.13.128", new)
    archive = stage / "update.zip"
    with ZipFile(archive, "w") as z:
        for relative, data in {**new, MANIFEST: new_manifest}.items():
            z.writestr(f"RenderDocBread/{relative}", data)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest() if failure != "hash" else "0" * 64
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    lock = locked_file(app / "renderdoc.dll") if failure == "locked" else contextlib.nullcontext()
    with lock:
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
             stage / SCRIPT.name, "-ParentProcessId", str(os.getpid()), "-InstalledVersion",
             "1.13.127", "-TargetVersion", "1.13.128", "-Sha256", digest, "-NoProgressWindow"],
            cwd=app, capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW,
        )

    for relative, data in personal.items():
        assert (app / relative).read_bytes() == data
    assert not (app / "escape.txt").exists()
    assert not (app / ".bread-update.lock").exists()
    restart = app / "restart.txt"
    deadline = time.monotonic() + 5
    while not restart.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)

    log = stage / "update-error.txt"
    diagnostic = log.read_text(encoding="utf-8-sig") if log.is_file() else result.stderr
    if failure:
        assert result.returncode == 1, result.stderr
        expected = {"hash": "SHA-256", "collision": "would be overwritten",
                    "locked": "renderdoc.dll", "traversal": "Invalid package path"}
        assert expected[failure] in diagnostic, diagnostic
        assert restart.read_text(encoding="utf-8").splitlines() == ["--updatefailed", str(log)]
        assert (app / MANIFEST).read_bytes() == old_manifest
        for relative, data in old.items():
            assert (app / relative).read_bytes() == data
    else:
        assert result.returncode == 0, diagnostic
        assert restart.read_text(encoding="utf-8") == ""
        assert (app / MANIFEST).read_bytes() == new_manifest
        for relative, data in new.items():
            assert (app / relative).read_bytes() == data
        assert not (app / "tools/obsolete.txt").exists()
        assert not stage.exists()


def test_update_closes_only_workers_from_the_installation(tmp_path, launcher):
    app = tmp_path / 'portable'
    worker = 'replaylab/BreadMCP.exe'
    runtime = 'replaylab/bread-host-support/base_library.zip'
    common = {EXE: launcher, 'RenderDocBread.portable': b'portable', 'renderdoc.dll': b'dll', worker: launcher}
    old = {**common, runtime: b'old runtime'}
    new = {**common, runtime: b'new runtime'}
    stage = app / '.bread-update-workers'
    stage.mkdir(parents=True)
    shutil.copyfile(SCRIPT, stage / SCRIPT.name)
    for relative, data in {**old, MANIFEST: manifest('1.13.127', old)}.items():
        path = app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    archive = stage / 'update.zip'
    with ZipFile(archive, 'w') as z:
        for relative, data in {**new, MANIFEST: manifest('1.13.128', new)}.items():
            z.writestr(f'RenderDocBread/{relative}', data)

    other = tmp_path / 'other'
    (other / worker).parent.mkdir(parents=True)
    (other / worker).write_bytes(launcher)
    (other / 'data').write_bytes(b'other installation')
    processes = []
    try:
        for root, filename in ((app, app / runtime), (other, other / 'data')):
            processes.append(subprocess.Popen([root / worker, '--hold-file', filename], cwd=root))
        deadline = time.monotonic() + 5
        while not all((root / 'worker-ready.txt').exists() for root in (app, other)):
            assert time.monotonic() < deadline
            time.sleep(.05)
        shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run(
            [shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
             stage / SCRIPT.name, '-ParentProcessId', str(os.getpid()), '-InstalledVersion',
             '1.13.127', '-TargetVersion', '1.13.128', '-Sha256', hashlib.sha256(archive.read_bytes()).hexdigest(),
             '-NoProgressWindow'],
            cwd=app, capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        log = stage / 'update-error.txt'
        assert result.returncode == 0, log.read_text(encoding='utf-8-sig') if log.exists() else result.stderr
        assert processes[0].poll() is not None and processes[1].poll() is None
        assert (app / runtime).read_bytes() == new[runtime]
        assert (other / 'data').read_bytes() == b'other installation'
        assert not stage.exists()
        assert not (app / '.bread-update.lock').exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
