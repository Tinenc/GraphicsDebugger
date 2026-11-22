"""Bounded, credential-safe reports for external process startup failures."""
from __future__ import annotations

import codecs
import ctypes
from datetime import datetime, timezone
import json
import locale
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import uuid
from typing import Mapping, Sequence

from .agent_diagnostics import configure_logging, default_log_root, redact


def utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def _native_encodings() -> list[str]:
    if os.name != "nt":
        return []
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    return [f"cp{api.GetOEMCP()}", f"cp{api.GetACP()}"]


def decode_process_output(value: bytes) -> tuple[str, str]:
    """Protocol stdout stays UTF-8; native stderr may use OEM/ANSI or UTF-16."""
    if value.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return value.decode("utf-16", errors="replace"), "utf-16"
    if value.startswith(codecs.BOM_UTF8):
        return value.decode("utf-8-sig", errors="replace"), "utf-8-sig"
    if len(value) >= 4 and len(value) % 2 == 0:
        if value[1::2].count(0) >= len(value) // 4:
            return value.decode("utf-16-le", errors="replace"), "utf-16-le"
        if value[::2].count(0) >= len(value) // 4:
            return value.decode("utf-16-be", errors="replace"), "utf-16-be"
    try:
        return value.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    encodings = _native_encodings()
    encodings.append(locale.getpreferredencoding(False))
    replacements = [(value.decode("utf-8", errors="replace"), "utf-8-replacement")]
    for encoding in dict.fromkeys(encodings):
        if encoding.lower().replace("-", "") in {"utf8", "utf16"}:
            continue
        try:
            decoded = value.decode(encoding, errors="replace")
            replacements.append((decoded, encoding))
            if "\ufffd" not in decoded:
                return decoded, encoding
        except (LookupError, UnicodeDecodeError):
            continue
    return min(replacements, key=lambda item: item[0].count("\ufffd"))


def safe_text(value: str, environment: Mapping[str, str]) -> str:
    value = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", str(value))
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    for name, secret in environment.items():
        if re.search(r"key|token|secret|password|credential|authorization|cookie", name, re.I) and secret:
            value = value.replace(secret, "[REDACTED]")
    return redact(value)


def startup_snapshot(command: Sequence[str], cwd: Path, environment: Mapping[str, str],
                     *, original_command: Sequence[str] | None = None) -> dict:
    rendered = subprocess.list2cmdline(command)
    safe_paths = {"APPDATA", "LOCALAPPDATA", "USERPROFILE", "CODEX_HOME", "TEMP", "TMP", "COMSPEC", "SYSTEMROOT", "WINDIR"}
    variables = []
    for name, value in sorted(environment.items(), key=lambda item: item[0].upper())[:512]:
        item = {"name": name[:256], "characters": len(value), "utf16Units": utf16_units(value)}
        if name.upper() in safe_paths:
            item["value"] = safe_text(value, environment)[:2048]
        else:
            item["value"] = "[REDACTED]"
        variables.append(item)
    path_value = next((value for name, value in environment.items() if name.upper() == "PATH"), "")
    path_entries = path_value.split(os.pathsep)
    executable = Path(command[0]) if command else Path()
    return {"schema": "bread-process-startup/v1", "createdUtc": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(), "platform": platform.platform(), "architecture": platform.machine(),
            "pythonVersion": sys.version.split()[0], "frozenHost": bool(getattr(sys, "frozen", False)),
            "workingDirectory": safe_text(str(cwd), environment), "workingDirectoryExists": cwd.is_dir(),
            "executable": safe_text(str(executable), environment), "executableExists": executable.is_file(),
            "executableKind": executable.suffix.lower(), "argumentCount": len(command),
            "argumentUtf16Units": [utf16_units(arg) for arg in command],
            "commandLineUtf16Units": utf16_units(rendered) + 1,
            "originalCommandLineUtf16Units": utf16_units(subprocess.list2cmdline(original_command or command)) + 1,
            "windowsNativeCommandLimit": 32767, "windowsCmdLimit": 8191,
            "environmentUtf16Units": sum(utf16_units(name + "=" + value) + 1 for name, value in environment.items()) + 1,
            "environmentCount": len(environment), "environment": variables,
            "pathEntryCount": len(path_entries), "pathUtf16Units": utf16_units(path_value),
            "pathEntries": [safe_text(value, environment)[:2048] for value in path_entries[:32]],
            "preferredEncoding": locale.getpreferredencoding(False)}


def write_startup_report(snapshot: dict, *, environment: Mapping[str, str], stderr: bytes = b"",
                         exit_code: int | None = None, error: BaseException | None = None) -> str:
    """Never replace the original failure with a logging failure."""
    text, encoding = decode_process_output(stderr)
    report = dict(snapshot, exitCode=exit_code, stderrBytes=len(stderr), stderrEncoding=encoding,
                  stderr=safe_text(text, environment)[-16384:])
    if error is not None:
        report["exceptionType"] = type(error).__name__
        report["exception"] = safe_text(str(error), environment)[:4096]
        report["winerror"] = getattr(error, "winerror", None)
        report["errno"] = getattr(error, "errno", None)
    try:
        root = default_log_root()
        logical_root = root
        if os.name == "nt" and not str(root).startswith("\\\\?\\"):
            spelling = os.path.abspath(root)
            root = Path("\\\\?\\UNC\\" + spelling[2:] if spelling.startswith("\\\\") else "\\\\?\\" + spelling)
        root.mkdir(parents=True, exist_ok=True)
        path = root / ("provider-startup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}-{uuid.uuid4().hex[:8]}.json")
        with path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        # Rotate only application-owned startup reports, never other diagnostic files.
        for old in sorted(root.glob("provider-startup-*.json"), key=lambda item: item.name, reverse=True)[20:]:
            try:
                old.unlink()
            except OSError:
                pass
        configure_logging(root).error("External process startup failed; report=%s exit=%s encoding=%s commandUnits=%s stderr=%s",
                                  path, exit_code, encoding, snapshot.get("commandLineUtf16Units"), report["stderr"][-2048:])
        return str(logical_root / path.name)
    except Exception:
        return ""
