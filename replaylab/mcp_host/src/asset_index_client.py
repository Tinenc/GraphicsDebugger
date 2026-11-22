"""Loopback client that asks qrenderdoc's ReplayLabInternal to run the asset scan."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid
from urllib.request import Request

from .internal_tool_service import ASSET_INDEX_OPERATION_SCHEMA
from .shader_live_edit_client import _read_response

PATH = "/replaylab/asset-index"
CAPABILITY = "asset-index"


class InternalAssetIndexUnavailable(RuntimeError):
    """The internal service is not running or predates the asset-index route."""


def is_current_process_owner(*, root=None) -> bool:
    """Whether this process owns the exact published Internal generation.

    Restore holds the active-project selection lock on its worker thread.
    Calling our own HTTP endpoint would send project status to a different
    thread that waits for that same lock until the HTTP caller times out.
    """

    from . import internal_host

    directory = internal_host.runtime_root() if root is None else Path(root).resolve()
    state_path = internal_host._paths(directory)["state"]
    try:
        if state_path.is_symlink() or not state_path.is_file() or state_path.stat().st_size > 65536:
            return False
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    generation = os.environ.get("REPLAYLAB_HOST_INSTANCE_ID", "")
    return bool(
        isinstance(state, dict)
        and state.get("schema") == "replaylab-internal-host/v1"
        and type(state.get("pid")) is int
        and state["pid"] == os.getpid()
        and internal_host.INSTANCE_PATTERN.fullmatch(generation)
        and state.get("hostInstanceId") == generation
    )


def request(operation: str, *, depth: str = "headers", root=None, timeout: float = 30.0) -> dict:
    from . import internal_host

    directory = internal_host.runtime_root() if root is None else Path(root).resolve()
    state_path = internal_host._paths(directory)["state"]
    if not state_path.is_file() or state_path.is_symlink() or state_path.stat().st_size > 65536:
        raise InternalAssetIndexUnavailable("Bread internal service is not running")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    port = state.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise InternalAssetIndexUnavailable("Bread internal service port is invalid")
    try:
        health = _read_response(Request("http://127.0.0.1:%d/health" % port, method="GET"), 3.0)
    except Exception as exc:  # noqa: BLE001
        raise InternalAssetIndexUnavailable("Bread internal service is unreachable: %s" % str(exc)[:200]) from exc
    generation = internal_host._validate_generation(directory, port, health)
    if CAPABILITY not in health.get("capabilities", []):
        raise InternalAssetIndexUnavailable(
            "Bread internal service predates persistent asset indexing; restart qrenderdoc after deployment")
    token_path = internal_host._paths(directory)["token"]
    if token_path.is_symlink() or not token_path.is_file() or token_path.stat().st_size > 1024:
        raise InternalAssetIndexUnavailable("Bread internal credential is unavailable")
    token = token_path.read_text(encoding="ascii").strip()
    if internal_host.TOKEN_PATTERN.fullmatch(token) is None:
        raise InternalAssetIndexUnavailable("Bread internal credential is invalid")
    request_id = uuid.uuid4().hex
    body = json.dumps({"schema": ASSET_INDEX_OPERATION_SCHEMA, "requestId": request_id,
                       "operation": operation, "depth": depth}, ensure_ascii=True).encode("utf-8")
    value = _read_response(Request("http://127.0.0.1:%d%s" % (port, PATH), data=body,
                                   headers={"Authorization": "Bearer " + token,
                                            "Content-Type": "application/json"}, method="POST"), timeout)
    if (value.get("schema") != ASSET_INDEX_OPERATION_SCHEMA or value.get("requestId") != request_id
            or value.get("hostInstanceId") != generation or not isinstance(value.get("result"), dict)):
        raise RuntimeError("Bread internal asset-index response identity changed")
    return value["result"]


__all__ = ["CAPABILITY", "InternalAssetIndexUnavailable", "is_current_process_owner", "request"]
