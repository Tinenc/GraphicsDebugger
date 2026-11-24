"""Best-effort, process-owned events for the ReplayLab Console.

The qrenderdoc bridge consumes a small JSONL protocol from the fixed
``%LOCALAPPDATA%\\ReplayLab\\console-events`` directory.  This producer is
deliberately display-only: callers cannot choose a journal path, attach a
source file, or pass commands/arguments through an event.  A failure to emit
telemetry is always ignored by the public API and therefore cannot change an
MCP tool's result or error semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Callable, Mapping, TypeVar, cast
import uuid


JOURNAL_EVENT_SCHEMA = "replaylab-console-event/v1"
JOURNAL_SCHEMA_VERSION = 1
PROCESS_ROLE = "host"

MAX_MESSAGE_CHARS = 16_384
MAX_PHASE_CHARS = 128
MAX_CODE_CHARS = 256
MAX_CONTEXT_VALUE_CHARS = 1_024
MAX_JOURNAL_LINE_BYTES = 32 * 1_024
MAX_JOURNAL_BYTES = 4 * 1_024 * 1_024
MAX_RETAINED_JOURNAL_FILES = 32
MAX_RETAINED_JOURNAL_BYTES = 32 * 1_024 * 1_024
RETENTION_INTERVAL_WRITES = 128

CONSOLE_SEVERITIES = frozenset(("info", "warning", "error"))
CONSOLE_MODULES = frozenset(
    (
        "system",
        "bridge",
        "host",
        "project",
        "capture",
        "pass",
        "material",
        "shader",
        "replay",
        "toolchain",
    )
)
CONSOLE_CONTEXT_FIELDS = frozenset(
    (
        "operation",
        "captureId",
        "captureSessionId",
        "eventId",
        "resourceId",
        "stage",
        "passId",
        "projectId",
        "workspaceId",
    )
)

_SESSION_ID = uuid.uuid4().hex
_SEQUENCE = 0
_SEGMENT = 0
_WRITES_SINCE_RETENTION = RETENTION_INTERVAL_WRITES
_JOURNAL_LOCK = threading.RLock()
_JOURNAL_FILE_PATTERN = re.compile(
    r"^events\.(?P<role>[a-z][a-z0-9_-]{0,31})\."
    r"(?P<pid>[0-9]{1,10})\.(?P<session>[0-9a-f]{32})\."
    r"(?P<segment>[0-9]{4})\.jsonl$"
)


@dataclass(frozen=True)
class ToolConsolePolicy:
    """Fixed display classification for one registered MCP operation."""

    module: str
    label: str


def _policies(
    module: str, labels: Mapping[str, str]
) -> dict[str, ToolConsolePolicy]:
    return {
        name: ToolConsolePolicy(module=module, label=label)
        for name, label in labels.items()
    }


# Only operations in this fixed catalog generate generic Console traffic.
# Read-only getters, list/status calls and watcher polling are intentionally
# absent so a background monitor cannot flood the user's Console.
TOOL_CONSOLE_POLICIES: dict[str, ToolConsolePolicy] = {}
TOOL_CONSOLE_POLICIES.update(
    _policies(
        "capture",
        {
            "open_capture": "打开捕获",
            "launch_renderdoc": "启动 RenderDoc",
            "export_capture_symbol_catalog": "导出捕获符号目录",
        },
    )
)
TOOL_CONSOLE_POLICIES.update(
    _policies(
        "project",
        {
            "ensure_active_replay_project": "准备活动工程",
            "configure_active_replay_project": "配置活动工程",
            "configure_active_project_asset_root": "配置工程资产目录",
            "activate_active_replay_project": "激活工程",
            "prepare_active_capture_project": "准备当前捕获工程",
            "register_active_project_workspace": "登记 Shader 工作区",
            "seed_active_shader_workspace": "复用工程 Shader 知识",
            "create_replay_project": "创建工程",
            "import_replay_project_capture": "导入工程捕获",
            "register_replay_project_workspace": "登记工程工作区",
            "register_replay_project_alias_table": "登记工程别名表",
            "save_replay_project_material_knowledge": "保存材质知识",
            "seed_shader_workspace_from_replay_project": "从工程生成 Shader 工作区",
        },
    )
)
TOOL_CONSOLE_POLICIES.update(
    _policies(
        "pass",
        {
            "classify_current_frame_passes": "整理当前帧 Pass",
            "apply_current_frame_pass_ai_proposals": "应用 Pass 语义建议",
        },
    )
)
TOOL_CONSOLE_POLICIES.update(
    _policies(
        "material",
        {
            "prepare_material_session": "准备材质调试会话",
            "update_material_parameter": "更新材质参数",
            "rollback_material_session": "回滚材质调试会话",
            "clear_all_material_sessions": "清理材质调试会话",
        },
    )
)
TOOL_CONSOLE_POLICIES.update(
    _policies(
        "replay",
        {
            "scan_asset_symbols": "扫描资产符号",
            "inspect_asset_shader_recovery": "检查资产 Shader 恢复证据",
            "match_capture_assets": "匹配捕获资产",
            "resolve_capture_symbols": "解析捕获符号",
            "submit_symbol_match": "提交符号匹配",
            "export_symbol_alias_table": "导出符号别名表",
            "apply_symbol_alias_table": "应用符号别名表",
        },
    )
)
TOOL_CONSOLE_POLICIES.update(
    _policies(
        "toolchain",
        {
            "get_shader_toolchain_capabilities": "读取 Shader 工具链能力",
            "plan_shader_toolchain_route": "规划 Shader 工具链路线",
            "plan_shader_recovery": "规划 Shader 恢复路线",
            "run_shader_toolchain_round_trip_smoke": "验证 Shader 工具链",
            "try_shader_workspace_recovery_candidate": "尝试 Shader 恢复路线",
            "prepare_shader_workspace": "反编译并准备 Shader 工作区",
            "decompile_shader_workspace": "反编译 Shader",
            "compile_shader_workspace": "编译 Shader",
            "migrate_shader_workspace_toolchain": "迁移 Shader 工具链",
            "validate_shader_workspace_candidate": "验证 Shader ABI",
        },
    )
)
TOOL_CONSOLE_POLICIES.update(
    _policies(
        "shader",
        {
            "create_shader_workspace": "创建 Shader 工作区",
            "promote_shader_workspace_explanation": "发布 Shader 可读说明",
            "begin_shader_workspace_organization": "开始整理 Shader 源码",
            "apply_shader_workspace_candidate": "应用 Shader 候选",
            "apply_shader_workspace_candidate_family": "应用 Shader 家族候选",
            "apply_shader_workspace_shared_resource": "替换共享 Shader 资源",
            "rollback_shader_workspace_shared_resource": "回滚共享 Shader 资源",
            "rollback_shader_workspace_override": "回滚 Shader 覆盖",
            "rollback_shader_workspace_override_family": "回滚 Shader 家族覆盖",
            "start_shader_workspace_watch": "启动 Shader 编译监听",
            "stop_shader_workspace_watch": "停止 Shader 编译监听",
            "start_shader_workspace_live_edit": "启动 Shader 实时更新",
            "migrate_shader_workspace_live_edit_authorization": "迁移实时更新授权",
            "stop_shader_workspace_live_edit": "停止 Shader 实时更新",
            "restore_current_shader": "恢复原始 Shader",
        },
    )
)

HIGH_FREQUENCY_SUPPRESSED_TOOLS = frozenset(
    (
        "ping",
        "get_capture_status",
        "get_current_renderdoc_context",
        "get_shader_workspace_status",
        "list_material_sessions",
        "get_material_session_status",
        "poll_shader_workspace_watch",
        "poll_shader_workspace_live_edit",
        "list_shader_workspace_live_edits",
        "get_active_replay_project_status",
        "get_active_capture_catalog_binding_status",
        "get_replay_project_status",
    )
)


def console_tool_policy(operation: str) -> ToolConsolePolicy | None:
    """Return only a fixed, compile-time policy; unknown tools stay quiet."""

    if operation in HIGH_FREQUENCY_SUPPRESSED_TOOLS:
        return None
    return TOOL_CONSOLE_POLICIES.get(operation)


def _bounded(value: Any, maximum: int) -> str:
    text = str(value or "").replace("\x00", "")
    if len(text) > maximum:
        return text[:maximum] + "..."
    return text


def _normalise_severity(value: str) -> str:
    severity = str(value or "info").strip().lower()
    if severity in ("warning", "warn"):
        return "warning"
    if severity in ("error", "fatal", "failure", "failed"):
        return "error"
    return "info"


def _default_journal_directory() -> Path:
    # LOCALAPPDATA is process-owner configuration.  There is intentionally no
    # function argument or MCP input that can redirect this journal.  Off Windows
    # the same layout lives under the XDG data root: refusing there would drop the
    # diagnostic journal on every non-Windows Host, silently, because this
    # producer is best-effort by design.
    root = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    if root:
        fixed_root = Path(os.path.abspath(os.path.expanduser(root)))
    else:
        xdg = str(os.environ.get("XDG_DATA_HOME", "") or "").strip()
        fixed_root = (Path(xdg).expanduser() if xdg
                      else Path.home() / ".local" / "share")
    directory = fixed_root / "ReplayLab" / "console-events"
    instance = str(os.environ.get("REPLAYLAB_INSTANCE_ID") or os.environ.get("BREAD_ROUTING_INSTANCE") or "")
    try:
        from .ipc_client import _CALL_BINDING
        bound = _CALL_BINDING.get()
        if bound is not None:
            instance = bound[0]
    except (ImportError, AttributeError):
        pass
    if re.fullmatch(r'[1-9][0-9]{0,19}-[0-9a-f]{32}', instance):
        directory = directory / 'instances' / instance
    return directory


def _is_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return True
    if path.is_symlink():
        return True
    attributes = int(getattr(details, "st_file_attributes", 0) or 0)
    return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _ensure_journal_directory(directory: Path) -> None:
    parent = directory.parent
    parent.mkdir(parents=True, exist_ok=True)
    if not parent.is_dir() or _is_reparse(parent):
        raise RuntimeError("Bread console journal parent is redirected")
    directory.mkdir(exist_ok=True)
    if not directory.is_dir() or _is_reparse(directory):
        raise RuntimeError("Bread console journal directory is redirected")


def _context(value: Mapping[str, Any] | None) -> dict[str, str]:
    if value is None:
        return {}
    clean: dict[str, str] = {}
    for key, item in value.items():
        if key not in CONSOLE_CONTEXT_FIELDS:
            raise ValueError("console event context field is not registered")
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise ValueError("console event context value is invalid")
        clean[key] = _bounded(item, MAX_CONTEXT_VALUE_CHARS)
    return clean


def _utc_now(unix_ms: int) -> str:
    seconds, milliseconds = divmod(unix_ms, 1000)
    return "%s.%03dZ" % (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)),
        milliseconds,
    )


def _journal_path(directory: Path, segment: int) -> Path:
    filename = "events.%s.%d.%s.%04d.jsonl" % (
        PROCESS_ROLE,
        os.getpid(),
        _SESSION_ID,
        segment,
    )
    return directory / filename


def _validate_existing_journal(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    try:
        details = path.lstat()
    except OSError as error:
        raise RuntimeError("Bread console journal is unavailable") from error
    if (
        _is_reparse(path)
        or not stat.S_ISREG(details.st_mode)
        or int(getattr(details, "st_nlink", 1) or 1) != 1
    ):
        raise RuntimeError("Bread console journal is redirected")


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(first))) == os.path.normcase(
        os.path.abspath(str(second))
    )


def _retention_candidates(directory: Path) -> list[tuple[int, str, int, Path]]:
    """Return only ordinary, non-redirected protocol journals in fixed root."""

    candidates: list[tuple[int, str, int, Path]] = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return candidates
    for entry in entries:
        if _JOURNAL_FILE_PATTERN.fullmatch(entry.name) is None:
            continue
        path = directory / entry.name
        try:
            details = entry.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(details.st_mode)
                or int(getattr(details, "st_nlink", 1) or 1) != 1
                or _is_reparse(path)
            ):
                continue
            size = int(details.st_size)
            if size < 0:
                continue
            modified_ns = int(
                getattr(details, "st_mtime_ns", int(details.st_mtime * 1_000_000_000))
            )
        except OSError:
            continue
        candidates.append((modified_ns, entry.name, size, path))
    return candidates


def _prune_console_journals(directory: Path, current_path: Path) -> None:
    """Bound generic journals without touching links or non-protocol audits."""

    candidates = _retention_candidates(directory)
    total_bytes = sum(item[2] for item in candidates)
    remaining_files = len(candidates)
    if (
        remaining_files <= MAX_RETAINED_JOURNAL_FILES
        and total_bytes <= MAX_RETAINED_JOURNAL_BYTES
    ):
        return
    # Oldest evidence is discarded first. The journal being appended by this
    # process is always retained, even under a concurrent timestamp anomaly.
    candidates.sort(key=lambda item: (item[0], item[1]))
    for _modified_ns, _name, size, path in candidates:
        if (
            remaining_files <= MAX_RETAINED_JOURNAL_FILES
            and total_bytes <= MAX_RETAINED_JOURNAL_BYTES
        ):
            break
        if _same_path(path, current_path):
            continue
        try:
            details = path.lstat()
            if (
                not stat.S_ISREG(details.st_mode)
                or int(getattr(details, "st_nlink", 1) or 1) != 1
                or _is_reparse(path)
            ):
                continue
            path.unlink()
        except OSError:
            # Another producer may have the file open. Retention is telemetry
            # maintenance and cannot be allowed to fail the event append.
            continue
        remaining_files -= 1
        total_bytes = max(0, total_bytes - size)


def _maybe_prune_console_journals(directory: Path, current_path: Path) -> None:
    global _WRITES_SINCE_RETENTION
    _WRITES_SINCE_RETENTION += 1
    if _WRITES_SINCE_RETENTION < RETENTION_INTERVAL_WRITES:
        return
    _WRITES_SINCE_RETENTION = 0
    _prune_console_journals(directory, current_path)


def _append_event(
    module: str,
    severity: str,
    message: str,
    *,
    phase: str = "",
    code: str = "",
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one validated event; callers should use ``emit_console_event``."""

    global _SEQUENCE, _SEGMENT
    module_value = str(module or "").strip().lower()
    if module_value not in CONSOLE_MODULES:
        raise ValueError("console event module is not registered")
    supplied_severity = str(severity or "").strip().lower()
    if supplied_severity not in CONSOLE_SEVERITIES:
        raise ValueError("console event severity is invalid")
    severity_value = _normalise_severity(supplied_severity)
    if severity_value not in CONSOLE_SEVERITIES:
        raise ValueError("console event severity is invalid")
    message_value = _bounded(message, MAX_MESSAGE_CHARS)
    if not message_value:
        raise ValueError("console event message is empty")
    phase_value = _bounded(phase or module_value, MAX_PHASE_CHARS)
    code_value = _bounded(code, MAX_CODE_CHARS)
    context_value = _context(context)
    directory = _default_journal_directory()

    with _JOURNAL_LOCK:
        _ensure_journal_directory(directory)
        _SEQUENCE += 1
        unix_ms = int(time.time() * 1000)
        event: dict[str, Any] = {
            "schema": JOURNAL_EVENT_SCHEMA,
            "schemaVersion": JOURNAL_SCHEMA_VERSION,
            "sessionId": _SESSION_ID,
            "sequence": _SEQUENCE,
            "unixMs": unix_ms,
            "at": _utc_now(unix_ms),
            "processRole": PROCESS_ROLE,
            "processId": os.getpid(),
            "module": module_value,
            "severity": severity_value,
            "phase": phase_value,
            "code": code_value,
            "message": message_value,
            "context": context_value,
        }
        encoded = (
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_JOURNAL_LINE_BYTES:
            raise ValueError("console event exceeds its bounded line size")

        path = _journal_path(directory, _SEGMENT)
        _validate_existing_journal(path)
        if path.is_file() and path.stat().st_size + len(encoded) > MAX_JOURNAL_BYTES:
            _SEGMENT += 1
            path = _journal_path(directory, _SEGMENT)
            _validate_existing_journal(path)
        flags = (
            os.O_APPEND
            | os.O_CREAT
            | os.O_WRONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags, 0o600)
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("console event journal made no write progress")
                offset += written
        finally:
            os.close(descriptor)
        _maybe_prune_console_journals(directory, path)
        return event


def emit_console_event(
    module: str,
    severity: str,
    message: str,
    *,
    phase: str = "",
    code: str = "",
    context: Mapping[str, Any] | None = None,
) -> bool:
    """Best-effort public producer; telemetry can never fail an operation."""

    try:
        _append_event(
            module,
            severity,
            message,
            phase=phase,
            code=code,
            context=context,
        )
        return True
    except Exception:
        return False


def _result_error(result: Any) -> str:
    if not isinstance(result, str) or len(result) > 1024 * 1024:
        return ""
    try:
        value = json.loads(result)
    except (TypeError, ValueError):
        return ""
    if not isinstance(value, dict) or not value.get("error"):
        return ""
    return _bounded(value.get("error"), 2_048)


_Function = TypeVar("_Function", bound=Callable[..., Any])


def instrument_mcp_tool(function: _Function) -> _Function:
    """Wrap a registered meaningful tool with fixed start/result events.

    Preserve async tools as coroutines so FastMCP awaits them. Sync wrappers
    around async tools return raw coroutine objects and break Agent calls.
    """

    import inspect

    policy = console_tool_policy(function.__name__)
    if policy is None:
        return function

    def _emit_start() -> dict[str, str]:
        event_context = {"operation": function.__name__}
        emit_console_event(
            policy.module,
            "info",
            "开始：%s" % policy.label,
            phase="tool-start",
            code="tool.started",
            context=event_context,
        )
        return event_context

    def _emit_result(event_context: dict[str, str], result: Any) -> None:
        result_error = _result_error(result)
        if result_error:
            emit_console_event(
                policy.module,
                "error",
                "失败：%s：%s" % (policy.label, result_error),
                phase="tool-failure",
                code="tool.failed",
                context=event_context,
            )
        else:
            emit_console_event(
                policy.module,
                "info",
                "完成：%s" % policy.label,
                phase="tool-success",
                code="tool.succeeded",
                context=event_context,
            )

    def _emit_exception(event_context: dict[str, str], exc: BaseException) -> None:
        emit_console_event(
            policy.module,
            "error",
            "失败：%s：%s" % (policy.label, _bounded(exc, 2_048)),
            phase="tool-failure",
            code="tool.failed",
            context=event_context,
        )

    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
            event_context = _emit_start()
            try:
                result = await function(*args, **kwargs)
            except Exception as exc:
                _emit_exception(event_context, exc)
                raise
            _emit_result(event_context, result)
            return result

        return cast(_Function, async_wrapped)

    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        event_context = _emit_start()
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            _emit_exception(event_context, exc)
            raise
        _emit_result(event_context, result)
        return result

    return cast(_Function, wrapped)


__all__ = [
    "CONSOLE_CONTEXT_FIELDS",
    "CONSOLE_MODULES",
    "HIGH_FREQUENCY_SUPPRESSED_TOOLS",
    "JOURNAL_EVENT_SCHEMA",
    "ToolConsolePolicy",
    "console_tool_policy",
    "emit_console_event",
    "instrument_mcp_tool",
]
