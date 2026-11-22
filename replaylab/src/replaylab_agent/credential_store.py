"""OS-backed storage for long-lived model API credentials.

Profile JSON contains only a stable profile id and an environment-variable
name.  On Windows the secret itself is stored as a Generic Credential owned by
the current user.  There is deliberately no plaintext fallback on platforms
without a supported secure store.
"""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from typing import Protocol, runtime_checkable

from .errors import ReplayLabAIError


class CredentialStoreError(ReplayLabAIError):
    """The operating-system credential store rejected an operation."""


@runtime_checkable
class CredentialStore(Protocol):
    backend: str
    supported: bool

    def read(self, profile_id: str) -> str | None: ...

    def write(self, profile_id: str, secret: str) -> None: ...

    def delete(self, profile_id: str) -> bool: ...


_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TARGET_PREFIX = "ReplayLab/AI/API/v1/"
_MAX_SECRET_BYTES = 5 * 512
_BEARER_PREFIX_RE = re.compile(r"^bearer[ \t]+", re.IGNORECASE)


def normalize_api_credential(secret: str, *, allow_empty: bool = False) -> str:
    """Normalize a pasted API credential without accepting header injection.

    Provider setup dialogs commonly receive either the token itself or a copied
    ``Bearer <token>`` value.  Store and send only the token.  Newlines and NUL
    remain invalid so the normalized value is always safe to place in exactly
    one SDK/header credential field.
    """

    if not isinstance(secret, str):
        raise CredentialStoreError("API credential must be text")
    value = secret.strip()
    if value.casefold() == "bearer":
        raise CredentialStoreError("API credential must be non-empty single-line text")
    if _BEARER_PREFIX_RE.match(value):
        value = _BEARER_PREFIX_RE.sub("", value, count=1).strip()
        if not value:
            raise CredentialStoreError("API credential must be non-empty single-line text")
    if not value:
        if allow_empty:
            return ""
        raise CredentialStoreError("API credential must be non-empty single-line text")
    if any(character in value for character in ("\0", "\r", "\n")):
        raise CredentialStoreError("API credential must be non-empty single-line text")
    return value


def _target(profile_id: str) -> str:
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise CredentialStoreError("credential profile id is invalid")
    return _TARGET_PREFIX + profile_id


class UnsupportedCredentialStore:
    backend = "unavailable"
    supported = False

    def read(self, profile_id: str) -> str | None:
        _target(profile_id)
        return None

    def write(self, profile_id: str, secret: str) -> None:
        del secret
        _target(profile_id)
        raise CredentialStoreError(
            "no supported operating-system credential store is available"
        )

    def delete(self, profile_id: str) -> bool:
        _target(profile_id)
        return False


if os.name == "nt":
    _LPBYTE = ctypes.POINTER(ctypes.c_ubyte)

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", _LPBYTE),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", wintypes.LPVOID),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


class WindowsCredentialStore:
    """Windows Credential Manager Generic Credential adapter."""

    backend = "windows-credential-manager"
    supported = os.name == "nt"

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialStoreError("Windows Credential Manager is unavailable")
        self._api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._api.CredWriteW.restype = wintypes.BOOL
        self._api.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._api.CredReadW.restype = wintypes.BOOL
        self._api.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._api.CredDeleteW.restype = wintypes.BOOL
        self._api.CredFree.argtypes = [wintypes.LPVOID]
        self._api.CredFree.restype = None

    @staticmethod
    def _error(operation: str) -> CredentialStoreError:
        code = ctypes.get_last_error()
        detail = ctypes.FormatError(code).strip() if code else "unknown Windows error"
        return CredentialStoreError(f"Windows Credential Manager {operation} failed: {detail}")

    def read(self, profile_id: str) -> str | None:
        target = _target(profile_id)
        pointer = ctypes.POINTER(_CREDENTIALW)()
        ctypes.set_last_error(0)
        if not self._api.CredReadW(
            target,
            self._CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            if ctypes.get_last_error() == self._ERROR_NOT_FOUND:
                return None
            raise self._error("read")
        try:
            credential = pointer.contents
            size = int(credential.CredentialBlobSize)
            if size <= 0:
                return None
            raw = ctypes.string_at(credential.CredentialBlob, size)
            try:
                value = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CredentialStoreError(
                    "saved API credential is not valid UTF-8; replace it in Settings -> AI"
                ) from exc
            return value or None
        finally:
            self._api.CredFree(pointer)

    def write(self, profile_id: str, secret: str) -> None:
        target = _target(profile_id)
        value = normalize_api_credential(secret)
        encoded = bytearray(value.encode("utf-8"))
        if len(encoded) > _MAX_SECRET_BYTES:
            for index in range(len(encoded)):
                encoded[index] = 0
            raise CredentialStoreError(
                f"API credential exceeds the Windows {_MAX_SECRET_BYTES}-byte limit"
            )
        blob = (ctypes.c_ubyte * len(encoded)).from_buffer(encoded)
        credential = _CREDENTIALW()
        credential.Type = self._CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.Comment = "Bread qrenderdoc model API credential"
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(blob, _LPBYTE)
        credential.Persist = self._CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = f"Bread profile {profile_id}"
        try:
            ctypes.set_last_error(0)
            if not self._api.CredWriteW(ctypes.byref(credential), 0):
                raise self._error("write")
        finally:
            for index in range(len(encoded)):
                encoded[index] = 0

    def delete(self, profile_id: str) -> bool:
        target = _target(profile_id)
        ctypes.set_last_error(0)
        if self._api.CredDeleteW(target, self._CRED_TYPE_GENERIC, 0):
            return True
        if ctypes.get_last_error() == self._ERROR_NOT_FOUND:
            return False
        raise self._error("delete")


def default_credential_store() -> CredentialStore:
    if os.name == "nt":
        return WindowsCredentialStore()
    return UnsupportedCredentialStore()


__all__ = [
    "CredentialStore",
    "CredentialStoreError",
    "UnsupportedCredentialStore",
    "WindowsCredentialStore",
    "default_credential_store",
    "normalize_api_credential",
]
