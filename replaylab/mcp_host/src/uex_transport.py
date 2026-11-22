"""Bounded, serialized UEX JSONL transport for exact read-only previews.

The process owns one mounted profile. Switching its executable, configuration,
profile, mapping or previously read loose packages discards that mount. Nothing
here scans an asset tree or changes the public operation/receipt contract.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_TRACKED_PACKAGES = 128
IDLE_SECONDS = 300.0
READY = "uex serve ready - one JSON request per line"


class TransportError(RuntimeError):
    pass


def _checkpoint(deadline: float, cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise InterruptedError("UEX preview cancelled")
    if time.monotonic() >= deadline:
        raise TimeoutError("UEX preview timed out")


def _digest(path: Path, deadline: float | None = None,
            cancel: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if deadline is not None:
                _checkpoint(deadline, cancel)
            digest.update(block)
    return digest.hexdigest()


def _package_stamp(path: Path, deadline: float | None = None,
                   cancel: threading.Event | None = None) -> tuple:
    # ProviderManager caches package objects as well as the file inventory. A new
    # receipt must never bind a fresh source hash to pixels from an old UObject.
    result = []
    for suffix in (".uasset", ".uexp", ".ubulk", ".m.ubulk", ".uptnl"):
        candidate = path.with_suffix(suffix)
        result.append((suffix, _digest(candidate, deadline, cancel) if candidate.is_file() else None))
    return tuple(result)


def adapt(operation: Mapping[str, Any], argv: Sequence[str]) -> dict | None:
    """Only known built-in argv shapes have an equivalent JSONL operation."""
    if operation.get("toolId") != "builtin-uex" or len(argv) != 9:
        return None
    command = {"uex_preview_texture": "preview-texture", "uex_preview_asset": "preview"}.get(
        operation.get("name"))
    option = "--out" if command == "preview-texture" else "--max-bytes"
    if (not command or list(argv[1:2]) != ["--config"] or argv[3] != command
            or argv[4] != "--profile" or argv[6] != option):
        return None
    config = Path(argv[2]).resolve()
    raw_config = config.read_bytes()
    if len(raw_config) > 4 * 1024 * 1024:
        raise TransportError("UEX profile configuration exceeds the size limit")
    profile = json.loads(raw_config).get("profiles", {}).get(argv[5], {})
    root = Path(str(profile.get("paksDir") or "")).resolve()
    # Archive mounts/aliases need a different invalidation model. Retain their
    # existing CLI path; this fast path is for exact loose-package previews.
    asset = Path(argv[8].replace("\\", "/"))
    if not profile or asset.is_absolute() or asset.suffix.lower() != ".uasset":
        return None
    source = (root / asset).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        return None
    args: dict[str, Any] = {"asset": argv[8]}
    if command == "preview-texture":
        args["out"] = str(Path(argv[7]).resolve())
    else:
        try:
            maximum = int(argv[7])
        except ValueError:
            return None
        # Serve clamps this range; never silently change the CLI's semantics.
        if not 1000 <= maximum <= 200000:
            return None
        args["maxBytes"] = maximum
    mapping = Path(str(profile.get("usmap") or ""))
    return {"config": config, "configHash": hashlib.sha256(raw_config).hexdigest(),
            "profile": argv[5], "source": source, "args": args, "command": command,
            "mappingHash": _digest(mapping) if mapping.is_file() else None}


class Session:
    """One pipe reader per stream; only the pool's request holder writes stdin."""
    def __init__(self, argv: Sequence[str]):
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, shell=False, **options)
        from .shader_workspace.tools import _attach_windows_kill_job
        self.job = _attach_windows_kill_job(self.process)
        self.argv = list(argv)
        self.frames: queue.Queue = queue.Queue(maxsize=4)
        self.closed = threading.Event()
        self.stderr_lock = threading.Lock()
        self.stderr = bytearray()
        self.readers = [threading.Thread(target=self._read_stdout, daemon=True,
                                         name="bread-uex-stdout"),
                        threading.Thread(target=self._read_stderr, daemon=True,
                                         name="bread-uex-stderr")]
        for reader in self.readers:
            reader.start()

    def _deliver(self, value: Any) -> None:
        while not self.closed.is_set():
            try:
                self.frames.put(value, timeout=0.1)
                return
            except queue.Full:
                continue

    def _read_stdout(self) -> None:
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(MAX_FRAME_BYTES + 1)
                if not line:
                    raise TransportError("UEX reader exited before returning a response")
                if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                    raise TransportError("UEX reader returned an oversized or incomplete frame")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TransportError("UEX reader returned an invalid JSONL envelope")
                self._deliver(value)
        except (OSError, ValueError, TransportError) as error:
            self._deliver(TransportError(str(error)))

    def _read_stderr(self) -> None:
        try:
            while not self.closed.is_set():
                block = self.process.stderr.read1(8192)
                if not block:
                    return
                with self.stderr_lock:
                    self.stderr.extend(block)
                    del self.stderr[:-MAX_STDERR_BYTES]
        except (OSError, ValueError):
            pass

    def read(self, deadline: float, cancel: threading.Event | None) -> dict:
        while True:
            _checkpoint(deadline, cancel)
            if self.closed.is_set():
                raise TransportError("UEX reader was closed")
            try:
                value = self.frames.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if isinstance(value, Exception):
                raise value
            return value

    def ready(self, deadline: float, cancel: threading.Event | None) -> None:
        message = self.read(deadline, cancel)
        if message.get("ok") is not True or message.get("result") != READY or message.get("id"):
            raise TransportError("UEX reader did not return its JSONL ready handshake")

    def request(self, payload: dict, deadline: float, cancel: threading.Event | None) -> dict:
        _checkpoint(deadline, cancel)
        with self.stderr_lock:
            self.stderr.clear()
        self.process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()
        reply = self.read(deadline, cancel)
        if reply.get("id") != payload["id"] or not isinstance(reply.get("ok"), bool):
            raise TransportError("UEX reader response does not match the active request")
        return reply

    def diagnostics(self) -> bytes:
        with self.stderr_lock:
            return bytes(self.stderr)

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        if self.job:
            from .shader_workspace.tools import _close_windows_handle
            _close_windows_handle(self.job)
            self.job = None
        if self.process.poll() is None:
            try:
                self.process.kill()
            except OSError:
                # The child may exit between poll() and kill(). Still drain and
                # close its pipes rather than interrupting pool cleanup.
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        for reader in self.readers:
            reader.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except OSError:
                pass


class ReaderPool:
    """One bounded mount, shared across calls and evicted after five idle minutes."""
    def __init__(self, session_factory=Session, idle_seconds=IDLE_SECONDS):
        self.lock = threading.Lock()
        self.session_factory = session_factory
        self.session: Session | None = None
        self.identity: tuple | None = None
        self.packages: dict[Path, tuple] = {}
        self.last_use = time.monotonic()
        self.idle_seconds = idle_seconds
        self.stopped = threading.Event()
        self.reaper: threading.Thread | None = None
        self.binary_hashes: dict[Path, tuple] = {}

    def _runtime_identity(self, program: Path, digest: str) -> tuple:
        components = [(str(program), digest)]
        for name in ("uex.dll", "CUE4Parse.dll", "CUE4Parse-Conversion.dll"):
            path = program.parent / name
            if path.is_file():
                stat = path.stat()
                stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                known = self.binary_hashes.get(path)
                if not known or known[0] != stamp:
                    self.binary_hashes[path] = (stamp, _digest(path))
                components.append((str(path), self.binary_hashes[path][1]))
        return tuple(components)

    def _discard(self) -> None:
        if self.session:
            self.session.close()
        self.session = None
        self.identity = None
        self.packages.clear()

    def _reap(self) -> None:
        while not self.stopped.wait(min(30.0, max(0.05, self.idle_seconds))):
            if self.lock.acquire(blocking=False):
                try:
                    if time.monotonic() - self.last_use >= self.idle_seconds:
                        self._discard()
                finally:
                    self.lock.release()

    def close(self) -> None:
        self.stopped.set()
        # Requests check this event through their own deadline; shutdown is also
        # allowed to abort a live process without waiting for the mount to finish.
        if self.session:
            self.session.close()
        if self.lock.acquire(timeout=3):
            try:
                self._discard()
            finally:
                self.lock.release()

    def run(self, spec: dict, program: str, program_sha256: str, timeout: float,
            cancel: threading.Event | None = None) -> dict:
        deadline = time.monotonic() + timeout
        acquired = False
        metadata: dict[str, Any] = {"kind": "uex-jsonl", "reused": False}
        try:
            while not acquired:
                _checkpoint(deadline, cancel)
                acquired = self.lock.acquire(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            _checkpoint(deadline, cancel)
            if self.stopped.is_set():
                raise TransportError("UEX reader pool has shut down")
            if self.reaper is None:
                self.reaper = threading.Thread(target=self._reap, daemon=True, name="bread-uex-idle")
                self.reaper.start()
            identity = (self._runtime_identity(Path(program).resolve(), program_sha256),
                        str(spec["config"]), spec["configHash"], spec["profile"], spec["mappingHash"])
            reason = "cold-start"
            if self.session:
                reason = ("configuration-or-runtime-changed" if identity != self.identity
                          else "reader-exited" if self.session.process.poll() is not None
                          else "package-limit" if len(self.packages) >= MAX_TRACKED_PACKAGES
                          else "source-changed" if any(_package_stamp(p, deadline, cancel) != s
                                                       for p, s in self.packages.items())
                          else "")
                if reason:
                    self._discard()
            metadata["reused"] = self.session is not None
            if not self.session:
                self.session = self.session_factory([program, "--config", str(spec["config"]), "serve"])
                if self.stopped.is_set():
                    raise TransportError("UEX reader pool shut down during process startup")
                self.identity = identity
                self.session.ready(deadline, cancel)
                metadata["restartReason"] = reason
            self.packages[spec["source"]] = _package_stamp(spec["source"], deadline, cancel)
            request_id = uuid.uuid4().hex
            metadata.update({"processId": self.session.process.pid, "argv": self.session.argv,
                             "requestId": request_id, "profile": spec["profile"]})
            reply = self.session.request({"id": request_id, "cmd": spec["command"],
                "profile": spec["profile"], "args": spec["args"]}, deadline, cancel)
            result = reply.get("result") if reply["ok"] else reply.get("error")
            if not isinstance(result, str):
                raise TransportError("UEX preview response is not text")
            stderr = self.session.diagnostics()
            if reply["ok"]:
                # A package can change while the child is decoding. Refuse the
                # current result too, not just the next reuse. This also protects
                # property previews, which have no PNG indexing post-check.
                if any(_package_stamp(path, deadline, cancel) != stamp
                       for path, stamp in self.packages.items()):
                    raise TransportError("UEX source package changed during preview; retry with the current source")
                return {"exitCode": 0, "stdout": (result + "\n").encode("utf-8"),
                        "stderr": stderr, "timedOut": False, "transport": metadata}
            # A failed load may leave an incomplete cached UObject. Next operation
            # remounts instead of repeatedly consulting the failed object.
            self._discard()
            return {"exitCode": 1, "stdout": b"", "stderr": stderr + result.encode("utf-8"),
                    "timedOut": False, "transport": metadata}
        except (OSError, ValueError, TransportError) as error:
            stderr = self.session.diagnostics() if acquired and self.session else b""
            if acquired:
                self._discard()
            return {"exitCode": -1, "stdout": b"", "stderr": stderr + str(error).encode("utf-8"),
                    "timedOut": isinstance(error, TimeoutError),
                    "cancelled": isinstance(error, InterruptedError), "transport": metadata}
        finally:
            if acquired:
                self.last_use = time.monotonic()
                self.lock.release()


POOL = ReaderPool()
atexit.register(POOL.close)
