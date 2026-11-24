"""Authenticated loopback save client for ReplayLab's Shader Workspace.

This module runs inside qrenderdoc's embedded Python 3.6. It never writes a
shader source directly: every save is sent to the private internal Host, which
binds or resumes the appropriate live-edit/watcher job. The compact Windows
runtime has no ``_socket``, so it uses a fixed no-proxy WinHTTP request. Other
hosts lazily load a direct loopback HTTP client. Neither transport follows
redirects or performs automatic authentication.
"""

from __future__ import absolute_import, print_function

import ctypes
import hashlib
import json
import os
import re
import tempfile
import threading
import time


SAVE_PATH = "/replaylab/shader-source/save"
SAVE_SCHEMA = "replaylab-shader-source-save/v1"
CHECK_PATH = "/replaylab/shader-source/check"
CHECK_SCHEMA = "replaylab-shader-source-check/v1"
MAX_CHECK_DIAGNOSTICS = 128
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_SOURCE_BYTES + 64 * 1024
MAX_RESPONSE_BYTES = 64 * 1024

# The internal Host advertises its route families here.  New families (for
# example "asset-index", 2026-09) are additive: a client requires the ones it
# uses and ignores the rest, so a newer Host never fails an older client.
REQUIRED_INTERNAL_CAPABILITIES = (
    "project-service", "project-restore", "shader-source-save", "shader-live-edit")


def _has_required_capabilities(value):
    if not isinstance(value, list) or len(value) > 32:
        return False
    if any(not isinstance(item, str) or len(item) > 64 for item in value):
        return False
    return all(item in value for item in REQUIRED_INTERNAL_CAPABILITIES)

MAX_STATE_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 1024
# Minimum Host package the client was written against. A newer patch or
# minor on the same major line stays compatible while protocol stays 1.
EXPECTED_HOST_VERSION = "1.13.121"
INTERNAL_HOST_STATE_PROTOCOL_VERSION = 1
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,128}$")
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_HOST_SEMVER = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")


def _parse_host_semver(value):
    if not isinstance(value, str):
        return None
    match = _HOST_SEMVER.fullmatch(value.strip())
    if match is None:
        return None
    parts = tuple(int(item) for item in match.groups())
    if any(part < 0 or part > 999999 for part in parts):
        return None
    return parts


def _host_runtime_version_compatible(received, expected):
    """True when a live Host is the same or a newer compatible package."""

    received_parts = _parse_host_semver(received)
    expected_parts = _parse_host_semver(expected)
    if received_parts is None or expected_parts is None:
        return False
    if received_parts[0] != expected_parts[0]:
        return False
    return received_parts >= expected_parts


class ReplayLabShaderSaveError(RuntimeError):
    pass


class ReplayLabShaderSaveConflict(ReplayLabShaderSaveError):
    pass


class ReplayLabShaderSaveUncertain(ReplayLabShaderSaveError):
    """The caller deadline elapsed after a request may have reached the Host."""

    pass


_ACTIVE_REQUEST_LOCK = threading.Lock()
_ACTIVE_REQUEST_THREAD = None


def _winhttp_request_blocking(method, path, port, headers, body,
                              timeout_seconds):
    """Perform one synchronous WinHTTP request on its owning worker thread.

    WinHTTP's synchronous receive timeout is not a wall-clock deadline when a
    peer withholds or trickles data. The public wrapper below therefore owns
    the caller deadline, while this function and only this worker thread own
    and close every HINTERNET handle. A timed-out peer can retain at most one
    daemon worker; later requests fail closed until that worker returns.
    """

    if os.name != "nt":
        return _portable_loopback_request(method, path, port, headers, body,
                                          timeout_seconds)
    winhttp = ctypes.WinDLL("winhttp", use_last_error=True)
    void_pointer = ctypes.c_void_p
    dword = ctypes.c_uint32

    winhttp.WinHttpOpen.argtypes = (
        ctypes.c_wchar_p, dword, ctypes.c_wchar_p, ctypes.c_wchar_p, dword)
    winhttp.WinHttpOpen.restype = void_pointer
    winhttp.WinHttpConnect.argtypes = (
        void_pointer, ctypes.c_wchar_p, ctypes.c_uint16, dword)
    winhttp.WinHttpConnect.restype = void_pointer
    winhttp.WinHttpOpenRequest.argtypes = (
        void_pointer, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, void_pointer, dword)
    winhttp.WinHttpOpenRequest.restype = void_pointer
    winhttp.WinHttpSetTimeouts.argtypes = (
        void_pointer, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
    winhttp.WinHttpSetTimeouts.restype = ctypes.c_int
    winhttp.WinHttpSetOption.argtypes = (
        void_pointer, dword, void_pointer, dword)
    winhttp.WinHttpSetOption.restype = ctypes.c_int
    winhttp.WinHttpSendRequest.argtypes = (
        void_pointer, ctypes.c_wchar_p, dword, void_pointer, dword, dword,
        ctypes.c_size_t)
    winhttp.WinHttpSendRequest.restype = ctypes.c_int
    winhttp.WinHttpReceiveResponse.argtypes = (void_pointer, void_pointer)
    winhttp.WinHttpReceiveResponse.restype = ctypes.c_int
    winhttp.WinHttpQueryHeaders.argtypes = (
        void_pointer, dword, ctypes.c_wchar_p, void_pointer,
        ctypes.POINTER(dword), ctypes.POINTER(dword))
    winhttp.WinHttpQueryHeaders.restype = ctypes.c_int
    winhttp.WinHttpReadData.argtypes = (
        void_pointer, void_pointer, dword, ctypes.POINTER(dword))
    winhttp.WinHttpReadData.restype = ctypes.c_int
    winhttp.WinHttpCloseHandle.argtypes = (void_pointer,)
    winhttp.WinHttpCloseHandle.restype = ctypes.c_int

    session = connection = request = None
    try:
        session = winhttp.WinHttpOpen(
            "RenderDocUltra-ReplayLab/1", 1, None, None, 0)  # NO_PROXY
        if not session:
            raise ctypes.WinError(ctypes.get_last_error())
        timeout_ms = max(100, min(120000, int(float(timeout_seconds) * 1000)))
        if not winhttp.WinHttpSetTimeouts(
                session, timeout_ms, timeout_ms, timeout_ms, timeout_ms):
            raise ctypes.WinError(ctypes.get_last_error())
        connection = winhttp.WinHttpConnect(
            session, "127.0.0.1", int(port), 0)
        if not connection:
            raise ctypes.WinError(ctypes.get_last_error())
        request = winhttp.WinHttpOpenRequest(
            connection, method, path, None, None, None, 0)
        if not request:
            raise ctypes.WinError(ctypes.get_last_error())
        # Never let a stale/reused loopback port redirect the bearer or trigger
        # ambient Negotiate/NTLM credentials. User-supplied Bearer headers are
        # preserved when WinHTTP automatic authentication is disabled.
        disabled_features = dword(
            0x2 | 0x4)  # WINHTTP_DISABLE_REDIRECTS | WINHTTP_DISABLE_AUTHENTICATION
        if not winhttp.WinHttpSetOption(
                request, 63, ctypes.byref(disabled_features),
                ctypes.sizeof(disabled_features)):
            raise ctypes.WinError(ctypes.get_last_error())
        auto_logon_policy = dword(2)  # WINHTTP_AUTOLOGON_SECURITY_LEVEL_HIGH
        if not winhttp.WinHttpSetOption(
                request, 77, ctypes.byref(auto_logon_policy),
                ctypes.sizeof(auto_logon_policy)):
            raise ctypes.WinError(ctypes.get_last_error())
        header_text = headers or None
        body_buffer = ctypes.create_string_buffer(body) if body else None
        body_pointer = (ctypes.cast(body_buffer, void_pointer)
                        if body_buffer is not None else None)
        if not winhttp.WinHttpSendRequest(
                request, header_text, len(headers or ""), body_pointer,
                len(body), len(body), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if not winhttp.WinHttpReceiveResponse(request, None):
            raise ctypes.WinError(ctypes.get_last_error())

        status = dword()
        status_size = dword(ctypes.sizeof(status))
        status_index = dword()
        if not winhttp.WinHttpQueryHeaders(
                request, 19 | 0x20000000, None, ctypes.byref(status),
                ctypes.byref(status_size), ctypes.byref(status_index)):
            raise ctypes.WinError(ctypes.get_last_error())
        chunks = []
        total = 0
        while True:
            buffer = ctypes.create_string_buffer(16 * 1024)
            received = dword()
            if not winhttp.WinHttpReadData(
                    request, buffer, len(buffer), ctypes.byref(received)):
                raise ctypes.WinError(ctypes.get_last_error())
            if received.value == 0:
                break
            total += int(received.value)
            if total > MAX_RESPONSE_BYTES:
                raise ReplayLabShaderSaveError(
                    "Bread Host returned an invalid response size")
            chunks.append(buffer.raw[:received.value])
        return int(status.value), b"".join(chunks)
    except ReplayLabShaderSaveError:
        raise
    except (OSError, ValueError) as exc:
        raise ReplayLabShaderSaveError(
            "Bread Host connection failed: %s" % str(exc))
    finally:
        for handle in (request, connection, session):
            if handle:
                try:
                    winhttp.WinHttpCloseHandle(handle)
                except Exception:
                    pass


def _portable_loopback_request(method, path, port, headers, body, timeout_seconds):
    """Unix transport with the same fixed origin and response bound as WinHTTP.

    http.client connects directly and does not consult proxy variables, follow
    redirects or perform automatic authentication. The outer worker owns the
    wall-clock deadline; this worker alone owns and closes its socket.
    """
    import http.client
    if (method not in ("GET", "POST") or not isinstance(path, str) or
            not path.startswith("/replaylab/") and path != "/health" or
            "\r" in path or "\n" in path or not 0 < int(port) < 65536):
        raise ReplayLabShaderSaveError("Invalid Bread Host request")
    request_headers = {}
    for line in str(headers or "").split("\r\n"):
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator or name.lower() not in ("authorization", "content-type", "accept"):
            raise ReplayLabShaderSaveError("Invalid Bread Host request headers")
        request_headers[name] = value.strip()
    connection = http.client.HTTPConnection("127.0.0.1", int(port),
                                             timeout=float(timeout_seconds))
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ReplayLabShaderSaveError("Bread Host returned an invalid response size")
        return int(response.status), raw
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ReplayLabShaderSaveError("Bread Host connection failed: %s" % str(exc))
    finally:
        connection.close()


def _bounded_winhttp_request(method, path, port, headers, body,
                             timeout_seconds):
    """Return by a strict wall-clock deadline without cross-thread handle close."""

    global _ACTIVE_REQUEST_THREAD
    timeout = max(0.1, min(120.0, float(timeout_seconds)))
    finished = threading.Event()
    outcome = {}

    def run_request():
        global _ACTIVE_REQUEST_THREAD
        try:
            outcome["value"] = _winhttp_request_blocking(
                method, path, port, headers, body, timeout)
        except Exception as exc:
            outcome["error"] = exc
        finally:
            with _ACTIVE_REQUEST_LOCK:
                if _ACTIVE_REQUEST_THREAD is threading.current_thread():
                    _ACTIVE_REQUEST_THREAD = None
            finished.set()

    worker = threading.Thread(
        target=run_request, name="ReplayLabShaderSaveHTTP")
    worker.daemon = True
    with _ACTIVE_REQUEST_LOCK:
        # A worker reserves the slot before Thread.start(). Treat even that
        # short pre-start state as occupied so concurrent saves cannot both
        # pass the guard and create two indefinitely pending WinHTTP requests.
        if _ACTIVE_REQUEST_THREAD is not None:
            raise ReplayLabShaderSaveError(
                "A previous Bread Host request is still pending")
        _ACTIVE_REQUEST_THREAD = worker
    try:
        worker.start()
    except Exception:
        with _ACTIVE_REQUEST_LOCK:
            if _ACTIVE_REQUEST_THREAD is worker:
                _ACTIVE_REQUEST_THREAD = None
        raise
    if not finished.wait(timeout):
        raise ReplayLabShaderSaveUncertain(
            "Bread Host request timed out")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _winhttp_health(port, timeout_seconds):
    return _bounded_winhttp_request(
        "GET", "/health", port, "Accept: application/json\r\n", b"",
        timeout_seconds)


def _winhttp_post(port, token, body, timeout_seconds):
    """POST directly to loopback through WinHTTP without Python's _socket."""

    headers = (
        "Authorization: Bearer %s\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        "Accept: application/json\r\n" % token)
    return _bounded_winhttp_request(
        "POST", SAVE_PATH, port, headers, body, timeout_seconds)


LIVE_EDIT_PATH = "/replaylab/shader-live-edit"
LIVE_EDIT_SCHEMA = "replaylab-internal-shader-live-edit/v1"


def _winhttp_post_live_edit(port, token, body, timeout_seconds):
    """POST one live-edit operation (apply / restore) to the internal Host."""

    headers = (
        "Authorization: Bearer %s\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        "Accept: application/json\r\n" % token)
    return _bounded_winhttp_request(
        "POST", LIVE_EDIT_PATH, port, headers, body, timeout_seconds)


def _winhttp_post_check(port, token, body, timeout_seconds):
    """POST an unsaved-buffer compile-check to the internal Host."""

    headers = (
        "Authorization: Bearer %s\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        "Accept: application/json\r\n" % token)
    return _bounded_winhttp_request(
        "POST", CHECK_PATH, port, headers, body, timeout_seconds)


def runtime_root(environment=None):
    env = os.environ if environment is None else environment
    configured = str(
        env.get("REPLAYLAB_INTERNAL_RUNTIME_DIR", "") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    local = str(env.get("LOCALAPPDATA", "") or "").strip()
    if local:
        return os.path.abspath(os.path.join(
            os.path.expanduser(local), "ReplayLab", "internal-runtime"))
    getuid = getattr(os, "getuid", None)
    suffix = str(getuid() if callable(getuid) else os.getpid())
    return os.path.abspath(os.path.join(
        tempfile.gettempdir(), "ReplayLabInternal-" + suffix))


def _is_reparse(path):
    if os.path.islink(path):
        return True
    if os.name != "nt":
        return False
    try:
        get_attributes = ctypes.windll.kernel32.GetFileAttributesW
        get_attributes.argtypes = (ctypes.c_wchar_p,)
        get_attributes.restype = ctypes.c_uint32
        attributes = int(get_attributes(str(path)))
        return attributes != 0xFFFFFFFF and bool(attributes & 0x400)
    except Exception:
        return True


def _read_bounded(path, maximum, label):
    value = os.path.abspath(str(path))
    if _is_reparse(value) or not os.path.isfile(value):
        raise ReplayLabShaderSaveError("%s is unavailable" % label)
    try:
        size = int(os.path.getsize(value))
        if size <= 0 or size > maximum:
            raise ReplayLabShaderSaveError("%s is invalid" % label)
        with open(value, "rb") as stream:
            raw = stream.read(maximum + 1)
    except ReplayLabShaderSaveError:
        raise
    except OSError as exc:
        raise ReplayLabShaderSaveError(
            "%s could not be read: %s" % (label, str(exc)))
    if len(raw) != size or len(raw) > maximum:
        raise ReplayLabShaderSaveError("%s changed while it was read" % label)
    return raw


def _strict_json(payload, label):
    def reject_duplicate(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("non-finite number: %s" % value)

    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ReplayLabShaderSaveError(
            "%s returned invalid JSON: %s" % (label, str(exc)))
    if not isinstance(value, dict):
        raise ReplayLabShaderSaveError("%s returned a non-object" % label)
    return value


def _process_alive(pid):
    try:
        value = int(pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if value <= 0:
        return False
    if value == os.getpid():
        return True
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (
            ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(
            0x1000 | 0x00100000, False, value)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_uint32()
            return bool(kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:  # Portable process-liveness probe; no signal is delivered.
        os.kill(value, 0)
        return True
    except OSError:
        return False


class ReplayLabShaderSaveClient(object):
    def __init__(self, runtime_directory=None, timeout_seconds=5.0,
                 startup_timeout_seconds=2.0, transport=None,
                 health_transport=None, sleep=None, monotonic=None,
                 check_transport=None):
        self.runtime_directory = os.path.abspath(
            runtime_directory or runtime_root())
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.startup_timeout_seconds = max(
            0.1, float(startup_timeout_seconds))
        self.transport = transport or _winhttp_post
        self.check_transport = check_transport or _winhttp_post_check
        self.health_transport = health_transport or _winhttp_health
        self.sleep = sleep or time.sleep
        self.monotonic = monotonic or time.monotonic

    @staticmethod
    def request_pending():
        """Report whether the one bounded WinHTTP worker still owns its slot."""

        with _ACTIVE_REQUEST_LOCK:
            return _ACTIVE_REQUEST_THREAD is not None

    def _credentials(self):
        state_path = os.path.join(
            self.runtime_directory, "internal-host.json")
        token_path = os.path.join(
            self.runtime_directory, "internal-host.token")
        deadline = self.monotonic() + self.startup_timeout_seconds
        last_error = None
        while self.monotonic() < deadline:
            try:
                state = _strict_json(
                    _read_bounded(state_path, MAX_STATE_BYTES,
                                  "Bread internal Host state"),
                    "Bread internal Host state")
                if (state.get("schema") != "replaylab-internal-host/v1" or
                        state.get("service") != "ReplayLabInternal" or
                        state.get("host") != "127.0.0.1"):
                    raise ReplayLabShaderSaveError(
                        "Bread internal Host state identity is invalid")
                try:
                    port = int(state.get("port"))
                    heartbeat = int(state.get("heartbeatUnixMs"))
                    pid = int(state.get("pid"))
                except (TypeError, ValueError, OverflowError):
                    raise ReplayLabShaderSaveError(
                        "Bread Host state is invalid")
                now_ms = int(time.time() * 1000)
                heartbeat_age = now_ms - heartbeat
                version = str(state.get("version", "") or "").strip()
                endpoint = str(state.get("endpoint", "") or "").strip()
                protocol_version = state.get("hostStateProtocolVersion")
                host_instance_id = state.get("hostInstanceId")
                if (port < 1024 or port > 65535 or pid <= 0 or heartbeat <= 0 or
                        heartbeat_age < -2000 or heartbeat_age > 5000 or
                        not _host_runtime_version_compatible(
                            version, EXPECTED_HOST_VERSION) or
                        endpoint != (
                            "http://127.0.0.1:%d/replaylab/internal" % port) or
                        isinstance(protocol_version, bool) or
                        not isinstance(protocol_version, int) or
                        protocol_version != INTERNAL_HOST_STATE_PROTOCOL_VERSION or
                        not isinstance(host_instance_id, str) or
                        ID_PATTERN.fullmatch(host_instance_id) is None or
                        not _process_alive(pid)):
                    raise ReplayLabShaderSaveError(
                        "Bread internal Host state is stale")
                published_token = str(state.get("tokenFile", "") or "").strip()
                if (published_token and
                        os.path.normcase(os.path.abspath(published_token)) !=
                        os.path.normcase(token_path)):
                    raise ReplayLabShaderSaveError(
                        "Bread internal Host token path is inconsistent")
                token = _read_bounded(
                    token_path, MAX_TOKEN_BYTES,
                    "Bread internal Host token").decode("ascii").strip()
                if TOKEN_PATTERN.fullmatch(token) is None:
                    raise ReplayLabShaderSaveError(
                        "Bread internal Host token is invalid")
                return (port, token, pid, version, protocol_version,
                        host_instance_id)
            except (ReplayLabShaderSaveError, UnicodeDecodeError) as exc:
                last_error = exc
                self.sleep(0.05)
        raise ReplayLabShaderSaveError(
            "Bread internal Host is unavailable: %s" %
            str(last_error or "internal Host state is unavailable"))

    def _verify_health(self, port, pid, version, protocol_version,
                       host_instance_id):
        try:
            status, body = self.health_transport(
                port, min(2.0, self.timeout_seconds))
        except ReplayLabShaderSaveError:
            raise
        except Exception as exc:
            raise ReplayLabShaderSaveError(
                "Bread internal Host health check failed: %s" % str(exc))
        if not body or len(body) > MAX_RESPONSE_BYTES:
            raise ReplayLabShaderSaveError(
                "Bread internal Host health response is invalid")
        value = _strict_json(body, "Bread internal Host health check")
        response_pid = value.get("pid")
        response_protocol_version = value.get("hostStateProtocolVersion")
        response_host_instance_id = value.get("hostInstanceId")
        if (status != 200 or value.get("service") != "ReplayLabInternal" or
                value.get("transport") != "internal-http" or
                not _host_runtime_version_compatible(
                    value.get("version"), EXPECTED_HOST_VERSION) or
                value.get("version") != version or
                not _has_required_capabilities(value.get("capabilities")) or
                isinstance(response_protocol_version, bool) or
                not isinstance(response_protocol_version, int) or
                response_protocol_version != protocol_version or
                not isinstance(response_host_instance_id, str) or
                ID_PATTERN.fullmatch(response_host_instance_id) is None or
                response_host_instance_id != host_instance_id or
                isinstance(response_pid, bool) or
                not isinstance(response_pid, int) or response_pid != pid):
            raise ReplayLabShaderSaveError(
                "Bread internal Host health identity is invalid")

    def save(self, live_edit_id, authorization_id, workspace_id,
             expected_raw_sha256, text):
        values = (live_edit_id, authorization_id, workspace_id)
        if any(not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None
               for value in values):
            raise ReplayLabShaderSaveError(
                "Shader save identity is invalid")
        if (not isinstance(expected_raw_sha256, str) or
                SHA256_PATTERN.fullmatch(expected_raw_sha256) is None):
            raise ReplayLabShaderSaveError(
                "Shader save expected hash is invalid")
        if not isinstance(text, str) or not text or "\x00" in text:
            raise ReplayLabShaderSaveError("Shader save source is invalid")
        try:
            source_bytes = text.encode("utf-8")
            source_size = len(source_bytes)
        except UnicodeEncodeError as exc:
            raise ReplayLabShaderSaveError(
                "Shader save source is not UTF-8") from exc
        if source_size > MAX_SOURCE_BYTES:
            raise ReplayLabShaderSaveError(
                "Shader save source exceeds its size limit")

        payload = {
            "liveEditId": live_edit_id,
            "authorizationId": authorization_id,
            "workspaceId": workspace_id,
            "expectedRawSha256": expected_raw_sha256,
            "text": text,
        }
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ReplayLabShaderSaveError(
                "Shader save request exceeds its size limit")

        source_hash = hashlib.sha256(source_bytes).hexdigest()
        (port, token, pid, version, protocol_version,
         host_instance_id) = self._credentials()
        self._verify_health(
            port, pid, version, protocol_version, host_instance_id)
        try:
            status, response_body = self.transport(
                port, token, body, self.timeout_seconds)
        except ReplayLabShaderSaveError:
            raise
        except Exception as exc:
            raise ReplayLabShaderSaveError(
                "Bread Host connection failed: %s" % str(exc))
        if not response_body or len(response_body) > MAX_RESPONSE_BYTES:
            raise ReplayLabShaderSaveError(
                "Bread Host returned an invalid response size")
        value = _strict_json(response_body, "Bread Host")
        if status != 200:
            message = str(value.get("error", "Shader save was rejected") or "")[:4096]
            if status == 409 and value.get("conflict") is True:
                raise ReplayLabShaderSaveConflict(message)
            raise ReplayLabShaderSaveError(
                "Bread Host rejected the Shader save (HTTP %d): %s" %
                (status, message))

        expected_fields = {
            "schema", "schemaVersion", "liveEditId", "authorizationId",
            "workspaceId", "watchId", "savedRawSha256", "size",
            "modifiedNs", "changed", "compileScheduled",
        }
        schema_version = value.get("schemaVersion")
        saved_hash = str(value.get("savedRawSha256", "") or "")
        changed = value.get("changed")
        if (set(value) != expected_fields or value.get("schema") != SAVE_SCHEMA or
                isinstance(schema_version, bool) or schema_version != 1 or
                value.get("liveEditId") != live_edit_id or
                value.get("authorizationId") != authorization_id or
                value.get("workspaceId") != workspace_id or
                ID_PATTERN.fullmatch(str(value.get("watchId", "") or "")) is None or
                SHA256_PATTERN.fullmatch(saved_hash) is None or
                saved_hash != source_hash or
                not isinstance(changed, bool) or
                changed != (source_hash != expected_raw_sha256) or
                not isinstance(value.get("compileScheduled"), bool)):
            raise ReplayLabShaderSaveError(
                "Bread Host returned an invalid Shader save result")
        size = value.get("size")
        modified_ns = value.get("modifiedNs")
        if (isinstance(size, bool) or not isinstance(size, int) or
                size != source_size or isinstance(modified_ns, bool) or
                not isinstance(modified_ns, int) or modified_ns <= 0 or
                modified_ns > 0x7FFFFFFFFFFFFFFF):
            raise ReplayLabShaderSaveError(
                "Bread Host returned invalid Shader save metadata")
        return {
            "rawSha256": value["savedRawSha256"],
            "size": size,
            "modifiedNs": modified_ns,
            "changed": value["changed"],
            "compileScheduled": value["compileScheduled"],
            "watchId": value["watchId"],
        }

    def live_edit_operation(self, operation, parameters, binding, timeout_seconds=None):
        """Run a capture-bound source/edit operation on the Host.

        ``binding`` is the exact qrenderdoc instance / capture session / capture
        path the Host checks before touching replay state.
        """

        if operation not in ("apply", "restore", "save-source", "convert-hlsl", "compiler-contract",
                             "open-source-document", "save-source-document",
                             "save-and-apply-source", "save-and-apply-source-document"):
            raise ReplayLabShaderSaveError("unsupported Shader live-edit operation")
        if not isinstance(parameters, dict) or not isinstance(binding, dict):
            raise ReplayLabShaderSaveError("Shader live-edit request is invalid")
        import uuid

        body = json.dumps(
            {"schema": LIVE_EDIT_SCHEMA, "requestId": uuid.uuid4().hex,
             "operation": operation, "parameters": parameters, "binding": binding},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ReplayLabShaderSaveError("Shader source request exceeds its size limit")
        (port, token, pid, version, protocol_version,
         host_instance_id) = self._credentials()
        self._verify_health(
            port, pid, version, protocol_version, host_instance_id)
        try:
            status, response_body = _winhttp_post_live_edit(
                port, token, body,
                float(timeout_seconds) if timeout_seconds else max(120.0, self.timeout_seconds))
        except ReplayLabShaderSaveError:
            raise
        except Exception as exc:
            raise ReplayLabShaderSaveError(
                "Bread Host connection failed: %s" % str(exc))
        if not response_body or len(response_body) > MAX_RESPONSE_BYTES:
            raise ReplayLabShaderSaveError(
                "Bread Host returned an invalid response size")
        value = _strict_json(response_body, "Shader live-edit response")
        if status != 200:
            error = value.get("error") if isinstance(value, dict) else None
            if status == 409 and isinstance(value, dict) and value.get("conflict") is True:
                raise ReplayLabShaderSaveConflict(str(error or "Shader source changed")[:2048])
            raise ReplayLabShaderSaveError(
                str(error or "Shader live-edit operation failed (HTTP %s)" % status)[:2048])
        result = value.get("result") if isinstance(value, dict) else None
        if not isinstance(result, dict):
            raise ReplayLabShaderSaveError("Shader live-edit response is invalid")
        return result

    def save_source(self, workspace_path, source_workspace_id, expected_raw_sha256,
                    text, binding):
        result = self.live_edit_operation("save-source", {
            "workspace_path": workspace_path, "source_workspace_id": source_workspace_id,
            "expected_raw_sha256": expected_raw_sha256, "text": text}, binding)
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if (result.get("rawSha256") != expected or result.get("sourceOnly") is not True
                or result.get("compileScheduled") is not False
                or result.get("sourceWorkspaceId") != source_workspace_id
                or not isinstance(result.get("sourcePath"), str)
                or not isinstance(result.get("modifiedNs"), int)):
            raise ReplayLabShaderSaveError("Bread Host returned an invalid source-only save result")
        return result

    def save_and_apply_source(self, workspace_path, source_workspace_id,
                              expected_raw_sha256, text, binding):
        result = self.live_edit_operation("save-and-apply-source", {
            "workspace_path": workspace_path, "source_workspace_id": source_workspace_id,
            "expected_raw_sha256": expected_raw_sha256, "text": text}, binding)
        return self._validate_editor_save(result, source_workspace_id, text)

    def save_and_apply_source_document(self, workspace_path, source_workspace_id,
                                       document_id, expected_raw_sha256,
                                       expected_source_sha256, text, binding):
        result = self.live_edit_operation("save-and-apply-source-document", {
            "workspace_path": workspace_path, "source_workspace_id": source_workspace_id,
            "document_id": document_id, "expected_raw_sha256": expected_raw_sha256,
            "expected_source_sha256": expected_source_sha256, "text": text}, binding)
        if (result.get("schema") != "bread-shader-source-document/v1" or
                result.get("sourceDocumentId") != document_id):
            raise ReplayLabShaderSaveError("Bread Host returned an invalid include save result")
        return self._validate_editor_save(result, source_workspace_id, text)

    @staticmethod
    def _validate_editor_save(result, source_workspace_id, text):
        # Saving and scheduling are independent facts: a compiler/session failure must
        # not make an already persisted editor buffer appear unsaved or lose its CAS.
        if (result.get("rawSha256") != hashlib.sha256(text.encode("utf-8")).hexdigest() or
                result.get("sourceWorkspaceId") != source_workspace_id or
                result.get("sourceOnly") is not False or
                not isinstance(result.get("compileScheduled"), bool) or
                not isinstance(result.get("sourcePath"), str) or
                not isinstance(result.get("modifiedNs"), int) or
                (result.get("autoApplyError") is not None and
                 not isinstance(result["autoApplyError"], str))):
            raise ReplayLabShaderSaveError("Bread Host returned an invalid editor save result")
        return result

    def open_source_document(self, workspace_path, source_workspace_id, requested_path, binding):
        result = self.live_edit_operation("open-source-document", {
            "workspace_path": workspace_path, "source_workspace_id": source_workspace_id,
            "requested_path": requested_path}, binding)
        if (result.get("schema") != "bread-shader-source-document/v1" or
                result.get("sourceWorkspaceId") != source_workspace_id or
                result.get("sourceOnly") is not True or result.get("compileScheduled") is not False or
                not isinstance(result.get("sourceDocumentId"), str) or
                re.fullmatch(r"[0-9a-f]{64}", result["sourceDocumentId"]) is None):
            raise ReplayLabShaderSaveError("Bread Host returned an invalid include document")
        return result

    def save_source_document(self, workspace_path, source_workspace_id, document_id,
                             expected_raw_sha256, expected_source_sha256, text, binding):
        result = self.live_edit_operation("save-source-document", {
            "workspace_path": workspace_path, "source_workspace_id": source_workspace_id,
            "document_id": document_id, "expected_raw_sha256": expected_raw_sha256,
            "expected_source_sha256": expected_source_sha256, "text": text}, binding)
        if (result.get("schema") != "bread-shader-source-document/v1" or
                result.get("sourceWorkspaceId") != source_workspace_id or
                result.get("sourceDocumentId") != document_id or
                result.get("rawSha256") != hashlib.sha256(text.encode("utf-8")).hexdigest() or
                result.get("sourceOnly") is not True or result.get("compileScheduled") is not False):
            raise ReplayLabShaderSaveError("Bread Host returned an invalid include save result")
        return result

    def check(self, live_edit_id, authorization_id, workspace_id, text):
        """Compile an unsaved buffer for live diagnostics; never publishes.

        Returns ``{"ok": bool|None, "diagnostics": [...]}``. ``ok`` is None
        when the buffer could not be checked live (route-plan workspace or an
        unavailable compiler); the caller then leaves existing squiggles as
        they are instead of clearing or inventing them.
        """

        values = (live_edit_id, authorization_id, workspace_id)
        if any(not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None
               for value in values):
            raise ReplayLabShaderSaveError("Shader check identity is invalid")
        if not isinstance(text, str) or not text or "\x00" in text:
            raise ReplayLabShaderSaveError("Shader check source is invalid")
        try:
            source_size = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ReplayLabShaderSaveError(
                "Shader check source is not UTF-8") from exc
        if source_size > MAX_SOURCE_BYTES:
            raise ReplayLabShaderSaveError(
                "Shader check source exceeds its size limit")

        payload = {
            "liveEditId": live_edit_id,
            "authorizationId": authorization_id,
            "workspaceId": workspace_id,
            "text": text,
        }
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ReplayLabShaderSaveError(
                "Shader check request exceeds its size limit")

        (port, token, pid, version, protocol_version,
         host_instance_id) = self._credentials()
        self._verify_health(
            port, pid, version, protocol_version, host_instance_id)
        try:
            status, response_body = self.check_transport(
                port, token, body, self.timeout_seconds)
        except ReplayLabShaderSaveError:
            raise
        except Exception as exc:
            raise ReplayLabShaderSaveError(
                "Bread Host connection failed: %s" % str(exc))
        if not response_body or len(response_body) > MAX_RESPONSE_BYTES:
            raise ReplayLabShaderSaveError(
                "Bread Host returned an invalid response size")
        value = _strict_json(response_body, "Bread Host")
        if status != 200:
            message = str(value.get("error", "Shader check was rejected") or "")[:4096]
            raise ReplayLabShaderSaveError(
                "Bread Host rejected the Shader check (HTTP %d): %s" %
                (status, message))
        expected_fields = {
            "schema", "schemaVersion", "liveEditId", "authorizationId",
            "workspaceId", "ok", "diagnostics", "reason",
        }
        schema_version = value.get("schemaVersion")
        ok = value.get("ok")
        raw_diagnostics = value.get("diagnostics")
        if (set(value) != expected_fields or value.get("schema") != CHECK_SCHEMA or
                isinstance(schema_version, bool) or schema_version != 1 or
                value.get("liveEditId") != live_edit_id or
                value.get("authorizationId") != authorization_id or
                value.get("workspaceId") != workspace_id or
                (ok is not None and not isinstance(ok, bool)) or
                not isinstance(raw_diagnostics, list) or
                not isinstance(value.get("reason"), str)):
            raise ReplayLabShaderSaveError(
                "Bread Host returned an invalid Shader check result")
        diagnostics = []
        for item in raw_diagnostics[:MAX_CHECK_DIAGNOSTICS]:
            if not isinstance(item, dict):
                continue
            line = item.get("line")
            column = item.get("column")
            severity = item.get("severity")
            message = item.get("message")
            if (isinstance(line, bool) or not isinstance(line, int) or line < 0 or
                    isinstance(column, bool) or not isinstance(column, int) or
                    column < 0 or
                    severity not in ("error", "warning", "information", "hint") or
                    not isinstance(message, str)):
                raise ReplayLabShaderSaveError(
                    "Bread Host returned an invalid Shader check diagnostic")
            diagnostics.append({
                "line": line,
                "column": column,
                "severity": severity,
                "text": message,
            })
        return {"ok": ok, "diagnostics": diagnostics,
                "reason": str(value.get("reason", "") or "")[:512]}


__all__ = [
    "ReplayLabShaderSaveClient",
    "ReplayLabShaderSaveConflict",
    "ReplayLabShaderSaveError",
    "ReplayLabShaderSaveUncertain",
]
