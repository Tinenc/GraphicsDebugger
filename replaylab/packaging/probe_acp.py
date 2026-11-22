#!/usr/bin/env python3
"""Standalone ACP agent connectivity probe -- no ReplayLab, no account needed.

Launches an ACP agent command exactly the way ReplayLab does (preferring the
Windows PATHEXT launcher, e.g. ``acp-claude-code.cmd``, over an extensionless
npm POSIX shim), performs the *account-free* JSON-RPC ``initialize`` handshake,
and prints the exit code, stdout and, crucially, **stderr** -- which the in-app
"connection test" hides. Use it to tell three failure classes apart:

  * launcher/runtime crash   -> the process exits immediately with a stderr error
  * protocol/version mismatch-> the process stays alive but never answers initialize
  * handshake OK             -> initialize returns a result (auth is only needed later)

Because ``initialize`` happens before any model/auth step, this verifies the
whole launch + protocol plumbing for ANY ACP agent (acp-claude-code, gemini,
qwen, codex, ...) without a subscription or API key.

Usage (from the repo root, with the build venv or any Python 3.9+):
    replaylab\\.venv\\Scripts\\python.exe replaylab\\packaging\\probe_acp.py acp-claude-code
    python probe_acp.py gemini --experimental-acp
    python probe_acp.py --timeout 8 acp-claude-code
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def resolve_command(command: str) -> str:
    """Resolve a bare command, preferring real Windows launchers over a shim.

    Mirrors ReplayLab's fixed resolver: on Windows a bare name tries the PATHEXT
    launchers (.cmd/.exe/...) before the extensionless POSIX shim of the same
    name (which CreateProcess rejects with WinError 193).
    """

    expanded = Path(command)
    if expanded.is_absolute():
        return str(expanded) if expanded.is_file() else ""
    if os.sep in command or (os.altsep and os.altsep in command):
        return ""
    extensions = [""]
    if os.name == "nt" and not expanded.suffix:
        pathext = [
            item.lower()
            for item in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
            if item
        ]
        extensions = pathext + [""]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        base = Path(directory)
        if not base.is_absolute():
            continue
        for extension in extensions:
            candidate = base / f"{command}{extension}"
            if candidate.is_file():
                return str(candidate)
    return ""


def _pump(stream, sink: list[bytes]) -> None:
    try:
        for chunk in iter(lambda: stream.read(4096), b""):
            sink.append(chunk)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe an ACP agent's launch + initialize handshake.")
    parser.add_argument("command", help="ACP agent command, e.g. acp-claude-code")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="extra args passed to the agent")
    parser.add_argument("--timeout", type=float, default=6.0, help="seconds to wait for a reply (default 6)")
    parser.add_argument("--protocol-version", type=int, default=1, help="ACP protocolVersion to send (default 1)")
    ns = parser.parse_args(argv)

    resolved = resolve_command(ns.command)
    print(f"command        : {ns.command}")
    print(f"resolved to    : {resolved or '<NOT FOUND ON PATH>'}")
    if not resolved:
        print("\nRESULT: the command is not on PATH. Install it (npm install -g ...) or open a new terminal.")
        return 2
    argv_full = [resolved, *ns.args]
    print(f"launching      : {' '.join(argv_full)}")

    # ACP (Zed agent-client-protocol) is newline-delimited JSON-RPC over stdio.
    initialize = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": ns.protocol_version, "clientCapabilities": {}},
        }
    ) + "\n"

    try:
        proc = subprocess.Popen(
            argv_full,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(Path.cwd()),
        )
    except OSError as exc:
        print(f"\nRESULT: could not launch the process: {exc}")
        return 2

    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    t_out = threading.Thread(target=_pump, args=(proc.stdout, out_chunks), daemon=True)
    t_err = threading.Thread(target=_pump, args=(proc.stderr, err_chunks), daemon=True)
    t_out.start()
    t_err.start()

    try:
        if proc.stdin:
            proc.stdin.write(initialize.encode("utf-8"))
            proc.stdin.flush()
    except Exception as exc:
        print(f"(could not write initialize to stdin: {exc})")

    deadline = time.time() + ns.timeout
    while time.time() < deadline and proc.poll() is None:
        if any(b"\n" in c for c in out_chunks):
            break
        time.sleep(0.1)

    exited = proc.poll()
    if exited is None:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    t_out.join(timeout=1)
    t_err.join(timeout=1)

    stdout = b"".join(out_chunks).decode("utf-8", "replace").strip()
    stderr = b"".join(err_chunks).decode("utf-8", "replace").strip()
    code = proc.poll()

    print(f"exit code      : {code}")
    print("\n--- stdout (first reply / banner) ---")
    print(stdout[:4000] if stdout else "<none>")
    print("\n--- stderr (the real error, if any) ---")
    print(stderr[:4000] if stderr else "<none>")

    print("\nRESULT: ", end="")
    got_reply = bool(stdout) and '"id"' in stdout and ("result" in stdout or "error" in stdout)
    if got_reply and "error" not in stdout.split('"result"')[0]:
        print("OK -- the agent launched and answered ACP initialize. Plumbing is good; "
              "any remaining failure is account/auth at session time.")
        return 0
    if exited is not None and exited != 0 and not got_reply:
        print(f"the agent process exited (code {exited}) during startup -- see stderr above "
              "for the real cause (missing runtime, crash, wrong Node version, ...).")
        return 1
    if exited is None or not stdout:
        print("the agent launched but never answered initialize within the timeout -- "
              "likely an ACP protocol/version mismatch, or it is waiting on something. "
              "See stderr above.")
        return 1
    print("unexpected reply; inspect stdout/stderr above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
