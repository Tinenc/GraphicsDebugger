"""Small bounded and atomic filesystem helpers used by shader workspaces."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping


# Windows editors, antivirus scanners and indexers can briefly keep a JSON
# destination (or its just-written temporary) open without FILE_SHARE_DELETE.
# Retrying only these well-known sharing/access failures keeps atomic writes
# robust without hiding a real ACL, redirection or disk failure.
_WINDOWS_TRANSIENT_FILE_ERRORS = frozenset({5, 32, 33})
_ATOMIC_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)


def canonical_workspace_identity(path: Path | str) -> str:
    """Return one request/lock identity for equivalent workspace spellings.

    In particular, Win32 ``C:\\...`` and ``\\\\?\\C:\\...`` spellings name the
    same workspace.  Request digests and process-local target locks must not
    treat them as separate mutation authorities.
    """

    text = str(Path(path).expanduser().resolve())
    if os.name == "nt":
        text = text.replace("/", "\\")
        if text.upper().startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[8:]
        elif text.startswith("\\\\?\\"):
            text = text[4:]
    return os.path.normcase(os.path.normpath(text))


def legacy_workspace_path_spellings(path: Path | str) -> tuple[str, ...]:
    """Enumerate bounded old ``workspacePath`` spellings for hash migration.

    Older durable receipts hashed the display path directly.  Callers may use
    these candidates only alongside a new canonical digest, allowing an exact
    existing receipt to replay without making arbitrary request hashes valid.
    """

    raw = str(path)
    resolved = str(Path(path).expanduser().resolve())
    values = [raw, resolved]
    if os.name == "nt":
        for value in tuple(values):
            value = value.replace("/", "\\")
            if value.upper().startswith("\\\\?\\UNC\\"):
                normal = "\\\\" + value[8:]
            elif value.startswith("\\\\?\\"):
                normal = value[4:]
            else:
                normal = value
            values.append(normal)
            if normal.startswith("\\\\"):
                values.append("\\\\?\\UNC\\" + normal[2:])
            elif _is_windows_drive_absolute(normal):
                values.append("\\\\?\\" + normal)
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


def _is_windows_drive_absolute(path: str) -> bool:
    """Small dependency-free check for an absolute Windows drive path."""

    return (
        len(path) >= 3
        and path[0].isalpha()
        and path[1] == ":"
        and path[2] in {"\\", "/"}
    )


def workspace_bound_request_hashes(
    request: Mapping[str, Any], workspace_path: Path | str
) -> tuple[str, tuple[str, ...]]:
    """Hash a request with canonical workspace identity plus old-path aliases."""

    canonical = dict(request)
    canonical.pop("workspacePath", None)
    canonical["workspaceIdentity"] = canonical_workspace_identity(workspace_path)

    def digest(value: Mapping[str, Any]) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    primary = digest(canonical)
    compatible: list[str] = []
    legacy_base = dict(request)
    legacy_base.pop("workspaceIdentity", None)
    for spelling in legacy_workspace_path_spellings(workspace_path):
        legacy = dict(legacy_base)
        legacy["workspacePath"] = spelling
        candidate = digest(legacy)
        if candidate != primary and candidate not in compatible:
            compatible.append(candidate)
    return primary, tuple(compatible)


def _is_transient_windows_file_error(error: BaseException) -> bool:
    if not isinstance(error, OSError):
        return False
    winerror = getattr(error, "winerror", None)
    if winerror in _WINDOWS_TRANSIENT_FILE_ERRORS:
        return True
    return os.name == "nt" and winerror is None and getattr(error, "errno", None) in {
        errno.EACCES,
        errno.EBUSY,
    }


def _replace_with_retry(temporary: Path, path: Path) -> None:
    for attempt in range(len(_ATOMIC_RETRY_DELAYS) + 1):
        try:
            os.replace(temporary, path)
            return
        except OSError as exc:
            if (
                not _is_transient_windows_file_error(exc)
                or attempt >= len(_ATOMIC_RETRY_DELAYS)
            ):
                raise
            time.sleep(_ATOMIC_RETRY_DELAYS[attempt])


def _cleanup_temporary_with_retry(temporary: Path) -> None:
    """Best-effort bounded cleanup which never masks the write exception."""

    for attempt in range(len(_ATOMIC_RETRY_DELAYS) + 1):
        try:
            temporary.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if (
                not _is_transient_windows_file_error(exc)
                or attempt >= len(_ATOMIC_RETRY_DELAYS)
            ):
                return
            time.sleep(_ATOMIC_RETRY_DELAYS[attempt])


def is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def atomic_write_bytes(path: Path, value: bytes, *, compact_temporary: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Project artifacts can opt out of duplicating a near-limit destination
    # name in staging (NAME_MAX / Win32 MAX_PATH). Transaction journals retain
    # their target-bound naming contract for crash recovery. In either case
    # staging stays beside its destination for same-filesystem atomic replace.
    name = "project" if compact_temporary else path.name
    temporary = path.parent / (".%s.tmp-%s" % (name, uuid.uuid4().hex))
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_with_retry(temporary, path)
    finally:
        _cleanup_temporary_with_retry(temporary)


def atomic_write_json(path: Path, value: Any, *, compact_temporary: bool = False) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, payload, compact_temporary=compact_temporary)


def apply_exact_text_edits(source: str, edits: list[dict[str, str]], *, maximum_bytes: int) -> str:
    """Build a draft in memory from ordered, uniquely matching text replacements.

    This is a text operation, not a Shader refactoring or validation policy.
    The caller owns the source hash check and publishes only the final result.
    """

    if not isinstance(edits, list) or not edits:
        raise ValueError("edits must be a non-empty list of old_text/new_text objects")
    result = source
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, dict) or set(edit) != {"old_text", "new_text"}:
            raise ValueError("edit %d must contain exactly old_text and new_text" % index)
        old, new = edit["old_text"], edit["new_text"]
        if not isinstance(old, str) or not old or "\x00" in old:
            raise ValueError("edit %d old_text must be non-empty NUL-free text" % index)
        if not isinstance(new, str) or "\x00" in new:
            raise ValueError("edit %d new_text must be NUL-free text" % index)
        start = result.find(old)
        if start < 0:
            raise ValueError("edit %d old_text did not match the current proposal exactly" % index)
        if result.find(old, start + 1) >= 0:
            raise ValueError("edit %d old_text matched more than once; include unique surrounding text" % index)
        result = result[:start] + new + result[start + len(old):]
        if len(result.encode("utf-8")) > maximum_bytes:
            raise ValueError("edit %d exceeds the proposal size limit (%d bytes)" % (index, maximum_bytes))
    return result


def read_json_object(path: Path, maximum_bytes: int, description: str) -> dict:
    for attempt in range(len(_ATOMIC_RETRY_DELAYS) + 1):
        try:
            return _read_json_object_once(path, maximum_bytes, description)
        except OSError as error:
            if (os.name != "nt" or getattr(error, "winerror", None) not in _WINDOWS_TRANSIENT_FILE_ERRORS
                    or attempt == len(_ATOMIC_RETRY_DELAYS)):
                raise
            time.sleep(_ATOMIC_RETRY_DELAYS[attempt])
    raise AssertionError("bounded JSON read retry exhausted")


def _read_json_object_once(path: Path, maximum_bytes: int, description: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("%s is not a regular file: %s" % (description, path))
    before = path.stat()
    if before.st_size <= 0 or before.st_size > maximum_bytes:
        raise ValueError(
            "%s size must be between 1 and %d bytes: %s"
            % (description, maximum_bytes, path)
        )
    try:
        from ..shared_file_io import open_shared_read
        with open_shared_read(path) as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read(maximum_bytes + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > maximum_bytes:
            raise ValueError(
                "%s exceeds its %d-byte size limit: %s"
                % (description, maximum_bytes, path)
            )
        if (
            len(raw) != before.st_size
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise ValueError("%s changed while it was read: %s" % (description, path))
        text = raw.decode("utf-8-sig", errors="strict")
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("%s is not valid UTF-8 JSON: %s" % (description, exc)) from exc
    if not isinstance(value, dict):
        raise ValueError("%s must contain one JSON object" % description)
    return value
