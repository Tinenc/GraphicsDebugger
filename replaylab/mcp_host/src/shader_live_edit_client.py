"""Fixed authenticated loopback client for the singleton Shader editor owner."""
from __future__ import annotations

import json
from pathlib import Path
import uuid
import re
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .internal_tool_service import SHADER_LIVE_EDIT_OPERATION_SCHEMA

PATH = "/replaylab/shader-live-edit"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Bread internal Shader requests cannot redirect")


_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


def _read_response(request, timeout):
    try:
        response = _OPENER.open(request, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        status = response.status
    if len(payload) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Bread internal Shader response exceeds its limit")
    value = json.loads(payload.decode("utf-8", errors="strict"))
    if not isinstance(value, dict):
        raise RuntimeError("Bread internal Shader response is not an object")
    if status != 200:
        error = RuntimeError("Bread internal Shader operation failed: %s" % str(value.get("error", status))[:2048])
        if isinstance(value.get("startTiming"), dict):
            error.shader_start_timing = value["startTiming"]
        raise error
    return value


def _directory_for_binding(binding, root=None):
    from . import internal_host
    if root is not None:
        return Path(root).resolve()
    from .ipc_client import INSTANCE_ROOT
    instance = binding.get('instanceId') if isinstance(binding, dict) else None
    if not isinstance(instance, str) or re.fullmatch(r'[1-9][0-9]{0,19}-[0-9a-f]{32}', instance) is None:
        raise RuntimeError('Shader service needs its exact application instance')
    # The GUI's instance_environment owns this directory. A vendor MCP may
    # inherit a shared/default runtime environment; it must never use it to
    # discover another application's Shader owner.
    return Path(INSTANCE_ROOT) / instance / 'internal-runtime'


def _endpoint(directory):
    from . import internal_host
    state_path = internal_host._paths(directory)["state"]
    if not state_path.is_file() or state_path.is_symlink() or state_path.stat().st_size > 65536:
        raise RuntimeError("Bread internal Shader service is unavailable; open qrenderdoc first")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    port = state.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise RuntimeError("Bread internal Shader port is invalid")
    health = _read_response(Request("http://127.0.0.1:%d/health" % port, method="GET"), 5.0)
    generation = internal_host._validate_generation(directory, port, health)
    if "shader-live-edit" not in health.get("capabilities", []):
        raise RuntimeError("Bread internal Shader service needs the current runtime; restart qrenderdoc after deployment")
    token_path = internal_host._paths(directory)["token"]
    if token_path.is_symlink() or not token_path.is_file() or token_path.stat().st_size > 1024:
        raise RuntimeError("Bread internal Shader credential is unavailable")
    token = token_path.read_text(encoding="ascii").strip()
    if internal_host.TOKEN_PATTERN.fullmatch(token) is None:
        raise RuntimeError("Bread internal Shader credential is invalid")
    return port, generation, health, token


def request(operation, parameters, binding, *, root=None):
    from . import internal_host
    directory = _directory_for_binding(binding, root)
    try:
        port, generation, health, token = _endpoint(directory)
    except (OSError, RuntimeError, ValueError) as initial:
        if root is not None:
            raise
        # Recover only before dispatching the Shader operation. A failed or
        # timed-out mutation is never replayed merely because its reply is lost.
        from .ipc_client import IPCClient
        try:
            client = IPCClient(instance_id=binding['instanceId'], timeout=12)
            client.call('ensure_internal_shader_service',
                {'expected_capture_session_id': binding['captureSessionId']}, timeout=12)
            deadline = time.monotonic() + 20
            while True:
                try:
                    port, generation, health, token = _endpoint(directory)
                    break
                except (OSError, RuntimeError, ValueError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.15)
        except Exception as recovery:
            raise RuntimeError('Bread internal Shader service recovery failed for %s at %s: %s; initial: %s'
                % (binding['instanceId'], directory, recovery, initial)) from recovery
    request_id = uuid.uuid4().hex
    body = json.dumps({"schema": SHADER_LIVE_EDIT_OPERATION_SCHEMA,
                       "requestId": request_id, "operation": operation,
                       "parameters": parameters, "binding": binding},
                      ensure_ascii=True, allow_nan=False).encode("utf-8")
    value = _read_response(Request("http://127.0.0.1:%d%s" % (port, PATH), data=body,
                                  headers={"Authorization": "Bearer " + token,
                                           "Content-Type": "application/json"}, method="POST"), 120.0)
    internal_host._validate_generation(directory, port, health)
    if (value.get("schema") != SHADER_LIVE_EDIT_OPERATION_SCHEMA
            or value.get("requestId") != request_id
            or value.get("hostInstanceId") != generation
            or value.get("binding") != binding
            or not isinstance(value.get("result"), dict)):
        raise RuntimeError("Bread internal Shader response identity changed")
    return value["result"]
