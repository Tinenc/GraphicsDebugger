"""Explicit model + MCP roundtrip. Provider text alone is never a success receipt."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import sys
import tempfile

from .agent_diagnostics import get_logger
from .doctor import DoctorCheck
from .profiles import Profile


@asynccontextmanager
async def _probe_workspace(prefix: str):
    """Finish process teardown before trying to remove a disposable probe directory."""
    temp = tempfile.mkdtemp(prefix=prefix)
    try:
        yield temp
    finally:
        for delay in (0, 0.05, 0.1, 0.2):
            if delay:
                await asyncio.sleep(delay)
            try:
                shutil.rmtree(temp)
                break
            except FileNotFoundError:
                break
            except OSError as error:
                if getattr(error, "winerror", None) not in (5, 32, 33) or delay == 0.2:
                    get_logger().warning(
                        "Could not clean up the %s probe workspace (errno=%s, winerror=%s)",
                        prefix, error.errno, getattr(error, "winerror", None))
                    break


def _route(receipt: Path):
    frozen = getattr(sys, "frozen", False)
    args = (["-I"] if frozen else []) + ["-m", "replaylab_agent.connection_probe"]
    env = {"BREAD_CONNECTION_PROBE_RECEIPT": str(receipt), "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    if not frozen:
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return {"id": "bread-check", "transport": "stdio", "command": sys.executable, "args": args, "env": env}


def _proved(receipt: Path, challenge: str) -> bool:
    try:
        result = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return result == {"challengeSha256": hashlib.sha256(challenge.encode()).hexdigest(), "tool": "bread_connection_probe"}


async def _model_probe(profile: Profile, server: dict, challenge: str, workspace: Path, timeout: float,
                       *, tools_config=None):
    prompt = ("This is an explicitly requested Bread connection check. Call the MCP tool bread_connection_probe "
              f"exactly once with challenge={challenge}. Then reply briefly with its result. "
              "Use the tool; do not simulate its output. No other work is requested.")
    if profile.provider.kind == "model":
        from langchain_core.messages import HumanMessage, ToolMessage
        from .connectors.langchain_models import build_chat_model
        from .external_mcp import ExternalMCPPool
        pool = ExternalMCPPool()
        async with AsyncExitStack() as stack:
            await pool.attach([server], stack)
            if not pool.tools:
                raise RuntimeError("connection-check-mcp-unavailable")
            name = next(iter(pool.tools))
            spec = {"name": name, "description": "Echo the connection-check challenge without modifying project data.",
                    "parameters": {"type": "object", "properties": {"challenge": {"type": "string"}},
                                   "required": ["challenge"]}}
            model = build_chat_model(profile, max_retries=0).bind_tools([spec])
            messages = [HumanMessage(content=prompt.replace("bread_connection_probe", name))]
            answer = await model.ainvoke(messages)
            messages.append(answer)
            calls = getattr(answer, "tool_calls", [])
            if len(calls) != 1 or calls[0].get("name") != name or calls[0].get("args") != {"challenge": challenge}:
                raise RuntimeError("model-did-not-call-connection-check-tool")
            result = await pool.call(name, {"challenge": challenge})
            if result.isError:
                raise RuntimeError("connection-check-tool-error")
            messages.append(ToolMessage(content=json.dumps([getattr(block, "text", "") for block in result.content],
                                                           ensure_ascii=False), tool_call_id=calls[0]["id"]))
            final = await model.ainvoke(messages)
            if not getattr(final, "content", None):
                raise RuntimeError("model-did-not-complete-after-tool")
        return

    from .process_env import external_agent_environment, resolved_agent_argv
    from . import native_vendor
    allowed = frozenset({"bread_connection_probe"})
    inherit = getattr(tools_config, "inherit_desktop_capabilities", False) is True
    env = external_agent_environment(profile, native_vendor.main_agent_env(profile, timeout), model=profile.model)
    native_vendor.prepare_workspace(profile, workspace, allowed, inherit_desktop_capabilities=inherit)
    if profile.provider.protocol == "acp":
        if profile.options.get("allow_unsandboxed_agent") is not True:
            raise RuntimeError("trusted-acp-process-not-enabled")
        from .acp_session import ACPMainAgentSession
        argv = native_vendor.runtime_argv(profile, resolved_agent_argv(profile), workspace, allowed,
                                          inherit_desktop_capabilities=inherit)
        async with ACPMainAgentSession(command=argv, cwd=workspace, env=env, allowed_tools=allowed,
                                       mcp_stdio_command=server["command"], mcp_stdio_args=server["args"],
                                       mcp_stdio_env=server["env"], timeout=timeout,
                                       authentication_method=profile.provider.acp_auth_method,
                                       provider_id=profile.preset, model=profile.model,
                                       session_meta=native_vendor.session_meta(
                                           profile, inherit_desktop_capabilities=inherit)) as session:
            result = await session.prompt(prompt)
            if not result.final_text:
                raise RuntimeError("model-did-not-complete-after-tool")
        return
    if profile.provider.protocol == "codex-app-server":
        from .agent_context import harden_codex_argv
        from .connectors.codex_app_server import CodexAppServerConnector
        from .native_mcp import NativeMCPRoute
        route = NativeMCPRoute(server["command"], tuple(server["args"]), server["env"], allowed, frozenset(), workspace)
        overrides = []
        options = {}
        if inherit:
            from .desktop_capabilities import discover_desktop_capabilities, codex_runtime_config, codex_runtime_overrides
            capabilities = discover_desktop_capabilities()
            home = Path(env['CODEX_HOME'])
            overrides = codex_runtime_overrides(capabilities, home)
            options = dict(inherit_desktop_capabilities=True, desktop_runtime_config=codex_runtime_config(
                capabilities, home,
                declared_mcp_ids=tuple(s.id for s in getattr(tools_config, 'mcp_servers', ())),
                disabled_skill_paths=getattr(tools_config, 'runtime_disabled_skill_paths', ())))
        command = harden_codex_argv(resolved_agent_argv(profile), inherit_desktop_capabilities=inherit,
                                    config_overrides=[*overrides, route.codex_config_override()])
        async with CodexAppServerConnector(command, cwd=workspace, env=env, timeout=timeout,
                                           expected_mcp_servers={"bread"}, **options) as connector:
            result = await connector.run(prompt, model=profile.model or None)
            if not result.text:
                raise RuntimeError("model-did-not-complete-after-tool")
        return
    raise RuntimeError("connection-check-protocol-not-supported")


async def run_execution_check(profile: Profile, *, timeout: float = 120.0, tools_config=None) -> list[DoctorCheck]:
    """Fresh temporary workspace, bounded model execution, one pure MCP tool."""
    async with _probe_workspace("bread-connection-") as temp:
        workspace = Path(temp).resolve()
        receipt = workspace / "probe-receipt.json"
        server = _route(receipt)
        challenge = secrets.token_hex(32)
        try:
            async with asyncio.timeout(max(1.0, min(timeout, 180.0))):
                options = {"tools_config": tools_config} if tools_config is not None else {}
                await _model_probe(profile, server, challenge, workspace, timeout, **options)
            if not _proved(receipt, challenge):
                raise RuntimeError("model-returned-without-mcp-execution-receipt")
        except Exception as error:
            # Provider exceptions can include credentials or response bodies.
            # Keep a bounded public stage/type; detailed provider diagnostics
            # continue through the existing redacted diagnostic machinery.
            from .agent_diagnostics import public_error
            return [DoctorCheck("model-tool-execution", "error", public_error(error),
                                "connection.execution-failed", (public_error(error),))]
    return [DoctorCheck("model-tool-execution", "ok", "Model generated, called the test MCP tool and completed; receipt verified.",
                        "connection.execution-ok")]


async def _check_portable_tools() -> DoctorCheck:
    from .portable_capabilities import server_definition
    from .external_mcp import ExternalMCPPool
    server = server_definition()
    if server is None:
        return DoctorCheck("portable-tool-execution", "error", "Portable tools are not installed.", "connection.portable-missing")
    try:
        async with _probe_workspace("bread-document-check-") as temp:
            async with AsyncExitStack() as stack:
                pool = ExternalMCPPool()
                await pool.attach([server], stack)
                tool = "bread-portable__bread_document"
                if tool not in pool.tools:
                    detail = pool.failed_servers.get("bread-portable", "document tool was not advertised")
                    raise RuntimeError("portable-document-startup-failed: " + detail)
                samples = {".docx": {"title": "Bread 检查", "paragraphs": ["文档测试"]},
                           ".xlsx": {"rows": [["检测", 42]]}, ".pptx": {"slides": [{"title": "测试", "body": "Bread"}]},
                           ".pdf": {"paragraphs": ["Bread 检查"]}}
                for suffix, content in samples.items():
                    path = Path(temp) / ("check" + suffix)
                    result = await pool.call(tool, {"operation": "create", "path": str(path), "content": content})
                    if not _portable_result_ok(result) or not path.is_file() or path.stat().st_size == 0:
                        raise RuntimeError("portable-document-create-failed:" + suffix)
                    result = await pool.call(tool, {"operation": "inspect", "path": str(path)})
                    if not _portable_result_ok(result):
                        raise RuntimeError("portable-document-inspect-failed:" + suffix)
        return DoctorCheck("portable-tool-execution", "ok", "DOCX, XLSX, PPTX and PDF tools executed successfully.",
                           "connection.portable-execution-ok")
    except Exception as error:
        from .agent_diagnostics import public_error
        return DoctorCheck("portable-tool-execution", "error", public_error(error),
                           "connection.execution-failed", (public_error(error),))


def _portable_result_ok(result) -> bool:
    value = result.structuredContent
    if not isinstance(value, dict):
        for block in result.content:
            try:
                candidate = json.loads(getattr(block, "text", ""))
            except ValueError:
                continue
            if isinstance(candidate, dict) and "exitCode" in candidate:
                value = candidate
                break
    return not result.isError and isinstance(value, dict) and value.get("exitCode") == 0 and not value.get("timedOut")


async def check_portable_tools() -> DoctorCheck:
    try:
        async with asyncio.timeout(150):
            return await _check_portable_tools()
    except TimeoutError:
        return DoctorCheck("portable-tool-execution", "error", "portable-tool-execution-timed-out",
                           "connection.execution-failed", ("portable-tool-execution-timed-out",))


async def check_host_tools(executable=None) -> DoctorCheck:
    """Check the actual frozen first-party Host, without touching a capture."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from .mcp_runtime import find_bread_mcp
    import os
    try:
        host = find_bread_mcp(executable)
        async with _probe_workspace("bread-host-check-") as temp:
            env = {"REPLAYLAB_RUNTIME_DIR": temp, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
            if os.environ.get("SystemRoot"):
                env["SystemRoot"] = os.environ["SystemRoot"]
            async with asyncio.timeout(25):
                with open(os.devnull, "w") as errors:
                    async with stdio_client(StdioServerParameters(command=str(host), args=["stdio"], env=env), errlog=errors) as streams:
                        async with ClientSession(*streams) as session:
                            await session.initialize()
                            listing = await session.list_tools()
                            if "get_host_runtime_status" not in {tool.name for tool in listing.tools}:
                                raise RuntimeError("bread-host-runtime-status-tool-missing")
                            result = await session.call_tool("get_host_runtime_status", {})
                            bodies = [json.loads(block.text) for block in result.content if getattr(block, "type", "") == "text"]
                            if result.isError or not bodies or any(isinstance(body, dict) and body.get("error") for body in bodies):
                                raise RuntimeError("bread-host-status-call-failed")
        return DoctorCheck("bread-host-execution", "ok", "First-party Host initialized and executed its read-only runtime status tool.",
                           "connection.host-execution-ok")
    except Exception as error:
        from .agent_diagnostics import public_error
        return DoctorCheck("bread-host-execution", "error", public_error(error),
                           "connection.execution-failed", (public_error(error),))
