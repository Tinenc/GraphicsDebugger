"""Small task-scoped stdio filter in front of ReplayLabMCP.

The filter exposes the task allow-list, enforces its effect ceiling, and checks
arguments against an optional frozen native-UI target. Local self-review uses
the shared task ledger even when no capture Host is running.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import stat
import sys
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .agent_diagnostics import public_error, redact
from .errors import ConfigurationError, PolicyError
from .agent_report import TaskReportStore, TASK_ID_ENV, TASK_EVENT_ROOT_ENV, TASK_REVIEW_TOOL_SCHEMA, TASK_REVIEW_TOOL_DESCRIPTION
from .task_review import TASK_REVIEW_TOOL, work_identity
from .policy import (
    AGENT_LOCAL_TOOLS,
    CORE_DIRECT_TOOLS,
    EFFECTFUL_TOOLS,
    READ_ONLY_TOOLS,
    TOOL_EFFECTS,
    WORKFLOW_EFFECTS,
    ordered_tool_names,
    tool_admissible_at_effect_ceiling,
    tool_call_effects,
    tool_effects,
)
from .process_env import host_process_environment, MCP_HOST_PROVIDER_ENV
from .skill_gateway import (
    DIRECTORY_TOOL,
    DIRECTORY_TOOL_DESCRIPTION,
    INVOKE_TOOL,
    LOAD_SKILL_TOOL,
    SkillGateway,
    compact_direct_tool_description,
    directory_tool_schema,
    encode as encode_capability_payload,
    invoke_tool_schema,
    load_validated_skill_catalog,
    load_tool_schema,
)
from .tool_scope import ToolCallScope
from .tool_output import (OwnedOutputStore, OWNED_OUTPUT_READER,
                          guard_mcp_call_result, mcp_result_failed, owned_output_reader_schema)
from .owned_workspace import AGENT_WORKSPACE_ROOT_ENV, OwnedWorkspace


_INSTANCE_ENV = "REPLAYLAB_INSTANCE_ID"
_REGISTRY_ENV = "RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY"
_TASK_ENV_NAMES = (
    "REPLAYLAB_TASK_ID",
    "REPLAYLAB_TASK_EVENT_ROOT",
    "REPLAYLAB_CONVERSATION_ID",
    AGENT_WORKSPACE_ROOT_ENV,
)
_MAX_JSON_LIST_ITEMS = 1_000_000
_MAX_ERROR_DETAIL_CHARS = 4096


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replaylab-agent-mcp-filter",
        description="Expose one task-scoped subset of Bread MCP tools",
    )
    parser.add_argument("--host", type=Path)
    parser.add_argument("--host-arg", action="append", default=[])
    parser.add_argument("--host-args-env", default="")
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("--allow-env", default="")
    parser.add_argument("--effect", action="append", default=[])
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--registry-path", default="")
    parser.add_argument("--scope-json", default="")
    # Accept old route arguments without letting a stored call budget govern AI work.
    parser.add_argument("--max-tool-calls", default="")
    parser.add_argument("--full-host-catalog", action="store_true")
    # Intent recognition is the Agent's job: it receives the Skill directory and
    # loads what the request actually needs.
    parser.add_argument("--skill-root", action="append", default=[])
    parser.add_argument("--disabled-skill", action="append", default=[])
    parser.add_argument("--mcp-server-json", default="")
    parser.add_argument("--mcp-server-file", type=Path)
    parser.add_argument("--task-input-file", type=Path)
    return parser


def _expand_route_file(argv: Sequence[str]) -> list[str]:
    values = list(argv)
    if "--route-file" not in values:
        return values
    if len(values) != 2 or values[0] != "--route-file":
        raise ConfigurationError("The task MCP route file must be the only route argument")
    path = Path(values[1])
    if not path.is_absolute():
        raise ConfigurationError("The task MCP route must be an absolute regular file")
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400 or details.st_size > 8 * 1024 * 1024:
        raise ConfigurationError("The task MCP route file is invalid")
    with path.open("rb") as stream:
        payload = json.loads(stream.read(8 * 1024 * 1024 + 1).decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != "bread-mcp-route/v1":
        raise ConfigurationError("Unsupported task MCP route schema")
    arguments = payload.get("arguments")
    environment = payload.get("environment")
    if not isinstance(arguments, list) or not all(isinstance(arg, str) and "\0" not in arg and arg != "--route-file" for arg in arguments):
        raise ConfigurationError("Invalid task MCP route arguments")
    if not isinstance(environment, dict) or any(not isinstance(key, str) or not isinstance(value, str) or "\0" in value or
        key not in MCP_HOST_PROVIDER_ENV | set(_TASK_ENV_NAMES) | {_INSTANCE_ENV} for key, value in environment.items()):
        raise ConfigurationError("Invalid task MCP route environment")
    os.environ.update(environment)
    return arguments


def _exception_detail(error: BaseException) -> str:
    """Expose bounded leaf causes hidden by AnyIO task-group wrappers."""

    pending: list[BaseException] = [error]
    details: list[str] = []
    while pending and len(details) < 8:
        current = pending.pop()
        nested = getattr(current, "exceptions", ())
        if isinstance(nested, tuple) and nested:
            pending.extend(reversed(nested))
            continue
        detail = (str(current).strip() or type(current).__name__).replace("\0", "�")
        if detail not in details:
            details.append(detail[:1024])
    rendered = " | ".join(details) or type(error).__name__
    return rendered[:_MAX_ERROR_DETAIL_CHARS]


@dataclass(slots=True)
class _CallAdmission:
    """One atomically admitted Host operation held until forwarding completes."""

    name: str
    active_effect: bool
    released: bool = False


class _ProxyCallCoordinator:
    """Enforce effect scope while serializing physical mutations and counting calls."""

    def __init__(
        self,
        *,
        effects: frozenset[str],
        scope: ToolCallScope,
    ) -> None:
        self._condition = asyncio.Condition()
        self._effects = effects
        self._scope = scope
        self._calls = 0
        self._active_effect_calls = 0

    @property
    def calls(self) -> int:
        return self._calls

    async def admit(self, name: str, arguments: dict[str, Any]) -> _CallAdmission:
        actual_effects = tool_call_effects(name, arguments)
        active_effect = bool(actual_effects)
        effect_claimed = False
        async with self._condition:
            try:
                if active_effect:
                    await self._condition.wait_for(
                        lambda: self._active_effect_calls == 0
                    )
                    self._active_effect_calls = 1
                    effect_claimed = True
                if not actual_effects.issubset(self._effects):
                    raise PolicyError(
                        f"tool '{name}' exceeds the active connection effect ceiling"
                    )
                self._scope.validate(name, arguments)
                self._calls += 1
                return _CallAdmission(
                    name=name,
                    active_effect=active_effect,
                )
            except BaseException:
                if effect_claimed:
                    self._active_effect_calls = 0
                self._condition.notify_all()
                raise

    async def finish(self, admission: _CallAdmission) -> None:
        async with self._condition:
            if admission.released:
                return
            admission.released = True
            if admission.active_effect:
                self._active_effect_calls -= 1
            self._condition.notify_all()


def _json_string_list(raw: str, label: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{label} must contain a JSON string array") from exc
    if (
        not isinstance(value, list)
        or len(value) > _MAX_JSON_LIST_ITEMS
        or any(
            not isinstance(item, str)
            or not item.strip()
            or "\0" in item
            or len(item) > 8192
            for item in value
        )
    ):
        raise ConfigurationError(f"{label} must contain a bounded JSON string array")
    return tuple(item.strip() for item in value)


def _selected_values(
    direct: Sequence[str], environment_name: str, label: str
) -> frozenset[str]:
    values = {str(item).strip() for item in direct if str(item).strip()}
    if environment_name:
        raw = os.environ.get(environment_name, "")
        if raw:
            values.update(_json_string_list(raw, environment_name))
    if not values:
        raise ConfigurationError(f"provide an explicit {label}")
    return frozenset(values)


def _host_arguments(direct: Sequence[str], environment_name: str) -> tuple[str, ...]:
    if environment_name and os.environ.get(environment_name, ""):
        return _json_string_list(os.environ[environment_name], environment_name)
    values = tuple(str(item) for item in direct) or ("stdio",)
    if len(values) > 32 or any(not item or "\0" in item or len(item) > 8192 for item in values):
        raise ConfigurationError("Bread MCP Host arguments are invalid")
    return values


def _validate_configuration(args: argparse.Namespace) -> tuple[
    Path | None,
    tuple[str, ...],
    frozenset[str],
    frozenset[str],
    ToolCallScope,
    dict[str, str],
]:
    host = args.host.expanduser() if args.host is not None else None
    if host is not None:
        if not host.is_absolute() or not host.is_file() or host.is_symlink():
            raise ConfigurationError("Bread MCP Host must be an absolute regular file")
        host = host.resolve()
    allowed = _selected_values(args.allow, args.allow_env, "tool allow-list")
    effects = frozenset(str(item).strip() for item in args.effect if str(item).strip())
    unknown = effects - WORKFLOW_EFFECTS
    if unknown:
        raise ConfigurationError(
            "unknown Bread effect class(es): " + ", ".join(sorted(unknown))
        )
    if "replay" in effects and "workspace" not in effects:
        raise ConfigurationError("the replay effect requires workspace")
    excessive = {
        name
        for name in allowed
        if not tool_admissible_at_effect_ceiling(name, effects)
    }
    if excessive:
        raise ConfigurationError(
            "tool allow-list exceeds the effect ceiling: "
            + ", ".join(sorted(excessive))
        )
    scope = ToolCallScope.from_json(str(args.scope_json or ""))
    owned_root = os.environ.get(AGENT_WORKSPACE_ROOT_ENV, "")
    if owned_root:
        OwnedWorkspace(Path(owned_root))
    if host is None:
        if allowed - AGENT_LOCAL_TOOLS - {OWNED_OUTPUT_READER}:
            raise ConfigurationError("capture-free proxy may only grant its local gateway tools")
        return None, (), allowed, effects, scope, host_process_environment({
            name: os.environ[name] for name in _TASK_ENV_NAMES if os.environ.get(name)
        })
    instance_id = str(args.instance_id or os.environ.get(_INSTANCE_ENV, "")).strip()
    registry_raw = str(args.registry_path or os.environ.get(_REGISTRY_ENV, "")).strip()
    if not instance_id or "\0" in instance_id or len(instance_id) > 512:
        raise ConfigurationError("a valid pinned qrenderdoc instance id is required")
    registry = Path(registry_raw).expanduser()
    if (
        not registry_raw
        or not registry.is_absolute()
        or registry.exists()
        or registry.is_symlink()
    ):
        raise ConfigurationError(
            "a fresh absolute live-edit registry path is required"
        )
    # EID selection remains flexible inside a capture, but this Host process
    # must not follow a later UI capture switch while an old task is finishing.
    # Forward the admitted task scope rather than inheriting ambient scope.
    extra = {_INSTANCE_ENV: instance_id, _REGISTRY_ENV: str(registry),
             "REPLAYLAB_TOOL_SCOPE": scope.to_json()}
    for name in _TASK_ENV_NAMES:
        value = os.environ.get(name, "")
        if value:
            extra[name] = value
    return (
        host,
        _host_arguments(args.host_arg, args.host_args_env),
        allowed,
        effects,
        scope,
        host_process_environment(extra),
    )


def _configured_mcp_servers(args: argparse.Namespace) -> list[dict[str, Any]]:
    raw = str(args.mcp_server_json or "").strip()
    if args.mcp_server_file is not None:
        path = args.mcp_server_file.expanduser()
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ConfigurationError("MCP runtime configuration must be an absolute regular file")
        with path.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ConfigurationError("MCP runtime configuration exceeds its size limit")
        raw = data.decode("utf-8", errors="strict")
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except ValueError as error:
        raise ConfigurationError("MCP runtime configuration must be a JSON array") from error
    if not isinstance(decoded, list):
        raise ConfigurationError("MCP runtime configuration must be a JSON array")
    return [dict(item) for item in decoded if isinstance(item, dict)][:32]


def _skill_gateway(
    args: argparse.Namespace,
    *,
    allowed: frozenset[str],
    effects: frozenset[str],
) -> SkillGateway | None:
    """Build the capability directory from the Skill roots this task was given."""

    roots = tuple(
        Path(str(item)).expanduser()
        for item in args.skill_root
        if str(item or "").strip()
    )
    if args.full_host_catalog:
        # API-mode keeps the exact Host catalog privately so its own Agent
        # gateway can expose the same compact direct/long-tail surface.
        return None
    servers = _configured_mcp_servers(args)
    catalog = load_validated_skill_catalog(
        roots, disabled_skills=args.disabled_skill or ()
    )
    on_skill_read = None
    task_id = os.environ.get("REPLAYLAB_TASK_ID", "")
    event_root = os.environ.get("REPLAYLAB_TASK_EVENT_ROOT", "")
    if task_id and event_root:
        from .events import TaskEventWriter

        writer = TaskEventWriter(event_root, task_id)
        on_skill_read = lambda receipt: writer.emit(
            "running", tool=LOAD_SKILL_TOOL, summary=receipt["skillId"],
            data={"phase": "skill-read", "status": "completed",
                  "skillReadReceipt": receipt, "hostVerified": False,
                  "verification": "localRead"},
        )
    return SkillGateway(
        catalog,
        tool_names=sorted(allowed - AGENT_LOCAL_TOOLS),
        effects=sorted(effects),
        mcp_servers=servers,
        asset_tools=_registered_asset_tools(),
        on_skill_read=on_skill_read,
    )


def _registered_asset_tools() -> tuple[dict[str, Any], ...]:
    """The extractors the user registered, read from the file Settings writes.

    Named in the directory so the Agent can see that an unpacker for this game
    exists at all. What it can actually do lives in its proved operations, which
    the Host serves; this only says who is there.
    """

    try:
        from . import agent_tool_providers

        tools, _diagnostics = agent_tool_providers.load_tools()
    except Exception:
        # A broken tools file must not take the capability directory down with it.
        return ()
    return tuple(
        tool for tool in tools
        if agent_tool_providers.tool_runtime_status(tool)[0]
    )


def _compatible_host_grant(
    registered: Sequence[str], requested: frozenset[str],
) -> tuple[frozenset[str], tuple[dict[str, str], ...]]:
    """Retain individually declared tools when Host and Agent versions differ.

    Source/package checks still validate the complete manifest. At runtime a
    missing optional operation or an unknown newer operation only affects that
    operation; no unknown or conflicting effect declaration becomes callable.
    """

    observed = frozenset(registered) - AGENT_LOCAL_TOOLS
    declared_read_only = READ_ONLY_TOOLS - EFFECTFUL_TOOLS - frozenset(TOOL_EFFECTS)
    declared_effectful = frozenset(
        name for name, effects in TOOL_EFFECTS.items()
        if name in EFFECTFUL_TOOLS and name not in READ_ONLY_TOOLS
        and effects and effects.issubset(WORKFLOW_EFFECTS)
    )
    known = declared_read_only | declared_effectful
    desired = requested - AGENT_LOCAL_TOOLS
    granted = observed & desired & known
    diagnostics = tuple(
        {"name": name[:96], "reason": reason}
        for names, reason in (
            (sorted(desired - observed), "not-present-in-host"),
            (sorted(observed - known), "no-coherent-effect-declaration"),
        )
        for name in names
    )
    return frozenset(granted), diagnostics


@asynccontextmanager
async def _proxy_connections(
    stack: AsyncExitStack, host: Path | None, host_args: Sequence[str],
    host_env: dict[str, str], external_servers: Sequence[dict[str, Any]],
):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from .external_mcp import ExternalMCPPool

    upstream = None
    if host is not None:
        streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
            command=str(host), args=list(host_args), env=host_env,
        )))
        upstream = await stack.enter_async_context(ClientSession(*streams))
        await upstream.initialize()
    external = ExternalMCPPool()
    await external.attach(external_servers, stack)
    yield upstream, external


async def _reviewed_external_call(external: Any, name: str, arguments: dict[str, Any], review_store: TaskReportStore) -> Any:
    try:
        result = await external.call(name, arguments)
    except Exception as error:
        review_store.record_task_activity(name, work_identity({
            "arguments": arguments, "error": public_error(error),
        }))
        raise
    dumped = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    review_store.record_task_activity(name, work_identity({"arguments": arguments, "result": dumped}))
    return result


class _FrameFlowCalls:
    """Task-local retry budget and actual Host roundtrip events."""

    tools = frozenset({"validate_frame_render_flow", "publish_frame_render_flow"})

    def __init__(self, host_env: dict[str, str]):
        self.failures: OrderedDict[str, tuple[str, int, str]] = OrderedDict()
        self.writer = None
        task_id = host_env.get(TASK_ID_ENV) or os.environ.get(TASK_ID_ENV)
        event_root = host_env.get(TASK_EVENT_ROOT_ENV) or os.environ.get(TASK_EVENT_ROOT_ENV)
        if task_id and event_root:
            from .events import TaskEventWriter
            with suppress(ValueError, OSError):
                self.writer = TaskEventWriter(event_root, task_id)

    def _emit(self, kind, name, activity_id, **data):
        if self.writer is not None:
            with suppress(Exception):
                self.writer.emit(kind, tool=name, data={"activityId": activity_id, **data})

    @staticmethod
    def _payload(result):
        payload = getattr(result, "structuredContent", None)
        if not isinstance(payload, dict):
            payload = getattr(next(iter(result.content or ()), None), "text", "")
        if isinstance(payload, dict) and "result" in payload:
            payload = payload["result"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                return {}
        return payload if isinstance(payload, dict) else {}

    async def call(self, upstream, name, arguments):
        flow = str(arguments.get("flow_id") or "")[:80]
        last = self.failures.get(flow)
        if name == "publish_frame_render_flow" and last and last[1] >= 3:
            raise PolicyError("Publication paused after three matching validation failures. "
                              "Correct the draft with validate_frame_render_flow before publishing again. "
                              "If this cannot be resolved, report the remaining rule to the user. " + last[2])

        activity_id = "frame-flow-host-" + uuid.uuid4().hex
        started = time.monotonic()
        self._emit("tool-started", name, activity_id)
        status, detail = "failed", ""
        try:
            result = await upstream.call_tool(name, arguments)
            payload = self._payload(result)
            failed = mcp_result_failed(result) or payload.get("valid") is False
            status = "failed" if failed else "completed"
            issues = payload.get("issues")
            if failed and isinstance(issues, list) and issues and isinstance(issues[0], dict):
                first = issues[0]
                detail = redact("%s: %s" % (first.get("path", ""), first.get("message", "")))[:400]
            elif failed and isinstance(payload.get("error"), str):
                detail = redact(payload["error"])[:400]
            if name == "publish_frame_render_flow" and payload.get("type") == "FrameFlowValidationError":
                signature = work_identity(issues)
                self.failures[flow] = (signature, last[1] + 1 if last and last[0] == signature else 1, detail)
                self.failures.move_to_end(flow)
                if len(self.failures) > 16:
                    self.failures.popitem(last=False)
            elif status == "completed":
                self.failures.pop(flow, None)
            return result
        finally:
            self._emit("tool-completed", name, activity_id, status=status,
                       elapsedMs=round((time.monotonic() - started) * 1000),
                       publicDetail=detail)


async def serve_filtered_proxy(
    host: Path | None,
    host_args: Sequence[str],
    allowed_tools: frozenset[str],
    *,
    effects: frozenset[str],
    scope: ToolCallScope,
    host_env: dict[str, str],
    gateway: SkillGateway | None = None,
    external_servers: Sequence[dict[str, Any]] = (),
    task_input: str | None = None,
) -> None:
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.server import NotificationOptions, Server
        from mcp.server.models import InitializationOptions
        from mcp.server.stdio import stdio_server
        from mcp.types import ToolAnnotations
        from mcp.types import Tool as _Tool
    except ImportError as exc:
        raise ConfigurationError(
            "the Bread MCP filter requires the MCP 1.x runtime"
        ) from exc

    async with AsyncExitStack() as stack:
        output_store = OwnedOutputStore()
        stack.callback(output_store.close)
        if task_input is not None:
            output_store.admit_task_input(task_input)
        async with _proxy_connections(stack, host, host_args, host_env, external_servers) as (upstream, external):
            listing = await upstream.list_tools() if upstream is not None else None
            upstream_by_name = {
                str(tool.name): tool for tool in list(getattr(listing, "tools", ()) or ())
            }
            # The capability directory, Skill loader and lazy dispatcher are served
            # here, not by the Host. Keep them out of the upstream completeness check.
            local_tools = (AGENT_LOCAL_TOOLS if gateway is not None else frozenset()) | {TASK_REVIEW_TOOL}
            review_store = TaskReportStore(
                str(host_env.get(TASK_ID_ENV) or os.environ.get(TASK_ID_ENV) or ""),
                host_env.get(TASK_EVENT_ROOT_ENV) or os.environ.get(TASK_EVENT_ROOT_ENV) or None,
            )
            host_tools, availability = _compatible_host_grant(
                tuple(upstream_by_name), frozenset(allowed_tools) - (
                    {OWNED_OUTPUT_READER} if OWNED_OUTPUT_READER not in upstream_by_name else set()
                )
            )
            host_reader_granted = OWNED_OUTPUT_READER in host_tools
            allowed_tools = host_tools | local_tools
            allowed_tools = allowed_tools | frozenset(external.tools)
            # Retained text belongs to this run even when no capture/Host exists.
            allowed_tools = allowed_tools | {OWNED_OUTPUT_READER}
            if not host_reader_granted:
                upstream_by_name[OWNED_OUTPUT_READER] = _Tool(
                    name=OWNED_OUTPUT_READER,
                    description="Page this run's exact retained task input or tool result by path and UTF-8 byte offset. Other files require a granted Host reader.",
                    inputSchema=owned_output_reader_schema(),
                )
            if availability:
                logging.getLogger(__name__).warning(
                    "Bread Host capability differences; remaining tools stay available: %s",
                    json.dumps(availability[:64], ensure_ascii=False),
                )
            if gateway is not None:
                gateway.retain_host_tools(host_tools, diagnostics=availability)
                gateway.register_external_tools({
                    name: tool.contract(name) for name, tool in external.tools.items()
                }, failed_servers=external.failed_servers)
                gateway.register_owned_output_reader(
                    upstream_by_name[OWNED_OUTPUT_READER].description or "",
                    upstream_by_name[OWNED_OUTPUT_READER].inputSchema,
                )
            task_tools = {}
            task_tools[TASK_REVIEW_TOOL] = _Tool(
                name=TASK_REVIEW_TOOL, description=TASK_REVIEW_TOOL_DESCRIPTION, inputSchema=TASK_REVIEW_TOOL_SCHEMA,
                annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
            )
            for name in host_tools:
                tool = upstream_by_name[name]
                declared_effects = tool_effects(name)
                read_only = not declared_effects
                existing = getattr(tool, "annotations", None)
                task_tools[name] = tool.model_copy(
                    update={
                        "annotations": ToolAnnotations(
                            title=getattr(existing, "title", None),
                            readOnlyHint=read_only,
                            destructiveHint=not read_only,
                            idempotentHint=read_only,
                            openWorldHint=False,
                        )
                    }
                )
            for name, tool in external.tools.items():
                task_tools[name] = tool.spec.model_copy(update={"name": name})
            if OWNED_OUTPUT_READER not in task_tools:
                task_tools[OWNED_OUTPUT_READER] = upstream_by_name[OWNED_OUTPUT_READER].model_copy(update={
                    "annotations": ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                   idempotentHint=True, openWorldHint=False),
                })
            if gateway is not None:
                from mcp.types import Tool as _Tool

                # The gateway was built before the upstream answered, so this is the
                # first moment the real descriptions exist. Without them, expanding a
                # family would list bare names.
                gateway.register_tools({
                    name: {
                        "description": str(getattr(tool, "description", "") or ""),
                        "inputSchema": (
                            getattr(tool, "inputSchema", None)
                            or getattr(tool, "input_schema", None)
                            or {"type": "object", "additionalProperties": True}
                        ),
                    }
                    for name, tool in upstream_by_name.items()
                })

                task_tools[DIRECTORY_TOOL] = _Tool(
                    name=DIRECTORY_TOOL,
                    description=DIRECTORY_TOOL_DESCRIPTION,
                    inputSchema=directory_tool_schema(),
                    annotations=ToolAnnotations(
                        title="Bread capabilities", readOnlyHint=True, destructiveHint=False,
                        idempotentHint=True, openWorldHint=False),
                )
                task_tools[LOAD_SKILL_TOOL] = _Tool(
                    name=LOAD_SKILL_TOOL,
                    description=(
                        "Load the matching Bread domain Skill before substantive domain work "
                        "unless already loaded. Use its task guidance and acceptance criteria; "
                        "the Agent chooses tool order and verifies the outcome."
                    ),
                    inputSchema=load_tool_schema(),
                    annotations=ToolAnnotations(
                        title="Load Bread Skill", readOnlyHint=True, destructiveHint=False,
                        idempotentHint=True, openWorldHint=False),
                )
                task_tools[INVOKE_TOOL] = _Tool(
                    name=INVOKE_TOOL,
                    description=(
                        "Execute a granted Bread Host tool outside the directly listed core "
                        "tools. Search or inspect its schema with list_bread_capabilities "
                        "when needed. The proxy validates arguments and applies the exact "
                        "Host effect grant and target scope."
                    ),
                    inputSchema=invoke_tool_schema(),
                    annotations=ToolAnnotations(
                        title="Invoke Bread Host tool", readOnlyHint=False,
                        destructiveHint=True, idempotentHint=False, openWorldHint=False),
                )

            # Common capture and Shader operations are ordinary typed MCP tools.
            # Less common operations remain available through the directory;
            # no catalog lookup is a prerequisite for an already-known name.
            # Routes without a Skill gateway retain the exact Host catalog for
            # diagnostics/backward compatibility.
            direct_tools = (
                set(CORE_DIRECT_TOOLS) & set(task_tools)
                if gateway is not None else set(task_tools)
            )
            direct_tools.add(OWNED_OUTPUT_READER)
            if gateway is not None:
                for direct_name in direct_tools:
                    gateway.tool(direct_name)
                    direct_tool = task_tools[direct_name]
                    task_tools[direct_name] = direct_tool.model_copy(update={
                        "description": compact_direct_tool_description(
                            str(getattr(direct_tool, "description", "") or "")
                        ),
                    })
            visible_tools = (
                ({name for name in AGENT_LOCAL_TOOLS if name in task_tools}
                 | direct_tools)
                if gateway is not None
                else set(task_tools)
            )

            server = Server(
                "Bread Task Filter",
                version=__version__,
                instructions="Only tools authorized by this Bread connection are exposed.",
            )
            call_coordinator = _ProxyCallCoordinator(
                effects=effects,
                scope=scope,
            )

            frame_flow_calls = _FrameFlowCalls(host_env)

            @server.list_tools()
            async def list_tools() -> list[Any]:
                return [task_tools[name] for name in ordered_tool_names(visible_tools)]

            # The Host validates operation arguments and physical effects.
            @server.call_tool(validate_input=False)
            async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
                if name not in allowed_tools:
                    raise PolicyError(f"tool '{name}' is outside the active task grant")
                if not isinstance(arguments, dict):
                    if name == TASK_REVIEW_TOOL:
                        review_store.invalidate_task_review("Bread MCP tool arguments must be an object")
                    raise PolicyError("Bread MCP tool arguments must be an object")
                if name == TASK_REVIEW_TOOL:
                    from mcp.types import CallToolResult, TextContent
                    try:
                        payload = review_store.record_task_review(**arguments)
                    except Exception as error:
                        # Missing/extra Python arguments fail before the Store's
                        # receipt validator runs. They must revoke an earlier pass.
                        review_store.invalidate_task_review(public_error(error))
                        raise
                    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))], isError=False)
                if (
                    gateway is not None
                    and name not in local_tools
                    and name not in direct_tools
                ):
                    raise PolicyError(
                        "This Host operation is outside the direct core tools; call it "
                        "through invoke_bread_tool with its exact granted name"
                    )
                if name in local_tools - {INVOKE_TOOL} and gateway is not None:
                    # Directory and Skill loading touch no capture state.
                    from mcp.types import CallToolResult, TextContent

                    if name == DIRECTORY_TOOL:
                        wanted = str(arguments.get("family", "") or "").strip()
                        search = str(arguments.get("query", "") or "").strip()
                        exact = str(arguments.get("tool", "") or "").strip()
                        selected = sum(bool(value) for value in (wanted, search, exact))
                        if selected > 1:
                            payload = {
                                "schema": "replaylab-capability-directory/v1",
                                "error": "family, query and tool are mutually exclusive",
                            }
                        elif exact:
                            payload = gateway.tool(exact, direct_tools=direct_tools)
                        elif search:
                            payload = gateway.search(search, direct_tools=direct_tools)
                        elif wanted:
                            payload = gateway.family(wanted, direct_tools=direct_tools)
                        else:
                            payload = gateway.directory(direct_tools=direct_tools)
                    else:
                        payload = gateway.load(
                            str(arguments.get("skill_id", "")),
                            str(arguments.get("section", "") or ""),
                            arguments.get("offset", 0),
                        )
                    encoded = encode_capability_payload(payload)
                    return CallToolResult(
                        content=[TextContent(type="text", text=encoded)],
                        structuredContent={"result": encoded},
                        isError=False,
                    )
                if name == INVOKE_TOOL and gateway is not None:
                    selected_name = str(arguments.get("name", "") or "").strip()
                    selected_arguments = arguments.get("arguments")
                    if (
                        not selected_name
                        or selected_name in AGENT_LOCAL_TOOLS
                        or selected_name not in allowed_tools
                    ):
                        raise PolicyError(
                            "invoke_bread_tool requires an exact granted Host tool name "
                            "listed in the Bread capability directory"
                        )
                    if not isinstance(selected_arguments, dict):
                        raise PolicyError("invoke_bread_tool.arguments must be an object")
                    argument_errors = gateway.tool_argument_errors(
                        selected_name, selected_arguments
                    )
                    if argument_errors:
                        raise PolicyError(
                            "invalid arguments for %s: %s. Inspect the tool's "
                            "inputSchema if needed; this call was not sent to the Host"
                            % (selected_name, "; ".join(argument_errors))
                        )
                    # From here on the selected Host tool -- not the dispatcher --
                    # owns effect, scope, accounting and evidence identity.
                    name = selected_name
                    arguments = dict(selected_arguments)
                elif gateway is not None and name in direct_tools:
                    argument_errors = gateway.tool_argument_errors(name, arguments)
                    if argument_errors:
                        raise PolicyError(
                            "invalid arguments for %s: %s" %
                            (name, "; ".join(argument_errors))
                        )
                if name == OWNED_OUTPUT_READER:
                    page = output_store.read(arguments)
                    if page is not None:
                        from mcp.types import CallToolResult, TextContent
                        review_store.record_task_activity(name, work_identity({"arguments": arguments, "result": page}))
                        text = json.dumps(page, ensure_ascii=False, separators=(",", ":"))
                        return CallToolResult(content=[TextContent(type="text", text=text)],
                                              isError=False)
                    if not host_reader_granted:
                        raise PolicyError("this path was not produced by this run and no Host file reader is granted")
                if name in external.tools:
                    # An enabled external server supplies its own contract.
                    # This path never claims a first-party Host effect receipt.
                    result = await _reviewed_external_call(external, name, arguments, review_store)
                    return guard_mcp_call_result(result, name, store=output_store, authority="user-configured-mcp-server")
                # Serialize mutations; reads and status calls can proceed while
                # a long Host operation runs.
                admission = await call_coordinator.admit(name, arguments)
                finished = False
                try:
                    if name in frame_flow_calls.tools:
                        result = await frame_flow_calls.call(upstream, name, arguments)
                    else:
                        result = await upstream.call_tool(name, arguments)
                    await call_coordinator.finish(admission)
                    finished = True
                    return guard_mcp_call_result(result, name, store=output_store)
                finally:
                    if not finished:
                        await call_coordinator.finish(admission)

            options = InitializationOptions(
                server_name="replaylab-task-filter",
                server_version=__version__,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
                instructions="Only tools authorized by this Bread connection are exposed.",
            )
            async with stdio_server() as downstream:
                await server.run(*downstream, options)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (AttributeError, OSError, ValueError):
                pass
    logging.basicConfig(level=logging.WARNING)
    try:
        args = _parser().parse_args(_expand_route_file(sys.argv[1:] if argv is None else argv))
        host, host_args, allowed, effects, scope, host_env = _validate_configuration(args)
        gateway = _skill_gateway(args, allowed=allowed, effects=effects)
        from .task_input import load_task_input
        task_input = load_task_input(args.task_input_file) if args.task_input_file is not None else None
        asyncio.run(
            serve_filtered_proxy(
                host,
                host_args,
                allowed,
                effects=effects,
                scope=scope,
                host_env=host_env,
                gateway=gateway,
                external_servers=_configured_mcp_servers(args),
                task_input=task_input,
            )
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.getLogger(__name__).error(
            "Bread MCP filter failed: %s", _exception_detail(exc)
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "serve_filtered_proxy"]
