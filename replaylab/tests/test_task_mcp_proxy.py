"""The task-scoped MCP proxy enforces deterministic authority boundaries."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
import sys
from pathlib import Path

import pytest

from replaylab_agent import mcp_proxy
from replaylab_agent.errors import ConfigurationError, PolicyError
from replaylab_agent.policy import (
    AGENT_LOCAL_TOOLS,
    CORE_DIRECT_TOOLS,
    EFFECTFUL_TOOLS,
    READ_ONLY_TOOLS,
    TOOL_EFFECTS,
    tool_call_effects,
    tool_admissible_at_effect_ceiling,
    tool_effects,
    validate_registered_tool_effect_manifest,
)
from replaylab_agent.skill_gateway import DIRECTORY_TOOL, INVOKE_TOOL
from replaylab_agent.tool_scope import ToolCallScope


@pytest.fixture
def regular_python(tmp_path):
    # Production resolves a regular BreadMCP launcher. On POSIX sys.executable
    # is often a symlink, so it cannot stand in for that admitted Host path.
    # Preserve a venv's interpreter identity through a regular test launcher.
    if sys.prefix == sys.base_prefix or os.name == "nt":
        return str(Path(sys.executable).resolve())
    launcher = tmp_path / "python-host"
    launcher.write_text("#!" + sys.executable + "\nimport os, sys\n"
                        "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n", encoding="utf-8")
    launcher.chmod(0o700)
    return str(launcher)


@contextmanager
def proxy_stderr(path):
    with path.open("w", encoding="utf-8") as stream:
        try:
            yield stream
        except BaseException:
            stream.flush()
            print(path.read_text(encoding="utf-8", errors="replace")[-6000:], file=sys.stderr)
            raise


def _args(host: Path, registry: Path, *extra: str):
    return mcp_proxy._parser().parse_args(
        [
            "--host",
            str(host),
            "--instance-id",
            "instance-1",
            "--registry-path",
            str(registry),
            *extra,
        ]
    )


def test_effect_manifest_is_declarative_not_a_workflow_state_machine() -> None:
    assert tool_effects("inspect_event") == frozenset()
    assert tool_effects("compile_shader_workspace") == frozenset({"workspace"})
    assert tool_effects("apply_shader_workspace_candidate") == frozenset({"replay"})
    assert tool_effects("get_active_replay_project_status") == frozenset()
    assert "get_active_shader_workspace_path" not in READ_ONLY_TOOLS
    assert "get_active_shader_workspace_path" in EFFECTFUL_TOOLS
    assert tool_effects("get_active_shader_workspace_path") == frozenset({"project", "replay"})
    assert tool_effects("start_shader_workspace_live_edit") == frozenset(
        {"workspace", "replay"}
    )
    assert tool_effects("publish_cbuffer_aliases") == frozenset(
        {"workspace", "project"}
    )
    assert tool_effects("rename_shader_workspace_symbols") == frozenset(
        {"workspace", "project"}
    )
    assert tool_effects("register_active_project_workspace") == frozenset(
        {"project"}
    )
    assert tool_call_effects(
        "classify_current_frame_passes",
        {"publish_to_ui": True, "persist_project": False},
    ) == frozenset({"ui"})


def test_source_save_does_not_require_replay_or_ui_effects() -> None:
    tool = "finish_shader_workspace_organization"
    assert tool_call_effects(tool, {}) == frozenset({"workspace", "replay", "project"})
    assert tool_call_effects(tool, {"validate_for_apply": None}) == frozenset({"workspace", "replay", "project"})
    assert tool_call_effects(tool, {"validate_for_apply": False}) == frozenset({"workspace"})
    assert tool_call_effects(tool, {"publication_mode": "checkpoint"}) == frozenset({"workspace"})
    assert tool_call_effects(tool, {"publication_mode": "checkpoint", "validate_for_apply": None}) == frozenset({"workspace"})
    assert tool_call_effects(tool, {"start_live_edit": True, "validate_for_apply": False}) == frozenset({"workspace", "replay", "project"})
    assert tool_admissible_at_effect_ceiling(tool, {"workspace"})
    assert tool_call_effects(tool, {"show_in_workspace": True}) == frozenset({"workspace", "ui", "replay", "project"})
    assert tool_call_effects(tool, {"show_in_workspace": True, "validate_for_apply": False}) == frozenset({"workspace", "ui"})
    assert tool_call_effects(tool, {"apply_shared_resource": True}) == frozenset({"workspace", "replay", "project"})
    assert tool_call_effects(tool, {"validate_for_apply": True, "show_in_workspace": True}) == frozenset({"workspace", "ui", "replay", "project"})
    for flag in ("apply_shared_resource", "start_live_edit", "validate_for_apply", "show_in_workspace"):
        with pytest.raises(PolicyError, match="JSON boolean"):
            tool_call_effects(tool, {flag: "true"})


def test_generated_delivery_effects_include_temporary_replay_validation() -> None:
    tool = "decompile_shader_workspace"
    assert tool in CORE_DIRECT_TOOLS
    assert tool_call_effects(tool, {}) == frozenset({"workspace", "replay"})
    assert tool_call_effects(tool, {"reference_only": True}) == frozenset({"workspace"})
    assert tool_call_effects(tool, {"validate_for_apply": False}) == frozenset({"workspace"})
    assert tool_call_effects(tool, {"validate_for_apply": False, "apply_shared_resource": True}) == frozenset({"workspace", "replay"})
    assert tool_admissible_at_effect_ceiling(tool, {"workspace"})
    assert "validate_shader_workspace_delivery" in CORE_DIRECT_TOOLS
    assert tool_effects("validate_shader_workspace_delivery") == frozenset({"workspace", "replay"})
    assert not tool_admissible_at_effect_ceiling("validate_shader_workspace_delivery", {"workspace"})
    for flag in ("reference_only", "validate_for_apply", "apply_shared_resource"):
        with pytest.raises(PolicyError, match="JSON boolean"):
            tool_call_effects(tool, {flag: "true"})


def test_workspace_ui_readback_is_direct_and_read_only() -> None:
    assert "get_shader_workspace_ui_state" in CORE_DIRECT_TOOLS
    assert tool_effects("get_shader_workspace_ui_state") == frozenset()


def test_effect_manifest_exactly_matches_the_live_first_party_host() -> None:
    from src import server

    registered = frozenset(
        kwargs.get("name") or function.__name__
        for function, _args, kwargs in server.mcp._registrations
    )

    # The task-local review also has a Host entry for direct integrations. It
    # records Agent telemetry only and grants no capture/workspace effects.
    assert registered & AGENT_LOCAL_TOOLS == {"record_task_review"}
    effect_registered = registered - {"record_task_review"}
    assert validate_registered_tool_effect_manifest(registered) == effect_registered
    assert READ_ONLY_TOOLS | EFFECTFUL_TOOLS == effect_registered
    assert frozenset(TOOL_EFFECTS) == EFFECTFUL_TOOLS
    with pytest.raises(PolicyError, match="not registered"):
        validate_registered_tool_effect_manifest(registered - {"inspect_event"})
    with pytest.raises(PolicyError, match="lack explicit effect declarations"):
        validate_registered_tool_effect_manifest(registered | {"new_host_tool"})


def test_proxy_diagnostic_unwraps_task_group_leaf_causes() -> None:
    error = ExceptionGroup(
        "task group",
        [ConfigurationError("Bread MCP Host is missing allowed tool: stale_tool")],
    )

    assert mcp_proxy._exception_detail(error) == (
        "Bread MCP Host is missing allowed tool: stale_tool"
    )


def test_runtime_host_drift_only_hides_affected_operations(monkeypatch) -> None:
    requested = frozenset({"inspect_event", "compile_shader_workspace", "new_host_tool"})
    granted, diagnostics = mcp_proxy._compatible_host_grant(
        ("inspect_event", "new_host_tool"), requested,
    )
    assert granted == frozenset({"inspect_event"})
    assert {item["name"]: item["reason"] for item in diagnostics} == {
        "compile_shader_workspace": "not-present-in-host",
        "new_host_tool": "no-coherent-effect-declaration",
    }
    # A conflicting local declaration also remains inaccessible; annotations
    # supplied by a newer Host never substitute for an audited effect class.
    monkeypatch.setattr(mcp_proxy, "READ_ONLY_TOOLS", READ_ONLY_TOOLS | {"compile_shader_workspace"})
    granted, diagnostics = mcp_proxy._compatible_host_grant(
        ("inspect_event", "compile_shader_workspace"), requested,
    )
    assert granted == frozenset({"inspect_event"})
    assert any(item["name"] == "compile_shader_workspace" and
               item["reason"] == "no-coherent-effect-declaration" for item in diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize("full_host_catalog", [False, True])
async def test_partial_host_starts_and_unknown_tools_never_reach_upstream(
    tmp_path: Path, full_host_catalog: bool, regular_python,
) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    fixture = Path(__file__).parent / "fixtures" / "fake_partial_mcp_host.py"
    args = ["-m", "replaylab_agent.mcp_proxy", "--host", regular_python,
            "--host-arg", str(fixture), "--instance-id", "partial-host-test",
            "--registry-path", str((tmp_path / "registry.json").resolve()),
            "--allow", "inspect_event", "--allow", "compile_shader_workspace",
            "--allow", "new_host_tool", "--allow", DIRECTORY_TOOL,
            "--allow", INVOKE_TOOL, "--effect", "workspace"]
    if full_host_catalog:
        args.append("--full-host-catalog")
    parameters = StdioServerParameters(command=sys.executable, args=args, env=dict(os.environ))
    with proxy_stderr(tmp_path / "partial-host.stderr.log") as errlog:
        async with stdio_client(parameters, errlog=errlog) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                listed = {tool.name for tool in (await session.list_tools()).tools}
                assert "inspect_event" in listed
                assert "compile_shader_workspace" not in listed
                assert "new_host_tool" not in listed
                first = await session.call_tool("inspect_event", {})
                assert json.loads(first.content[0].text)["calls"] == 1
                unknown = await session.call_tool("new_host_tool", {})
                assert unknown.isError
                if not full_host_catalog:
                    directory = await session.call_tool(DIRECTORY_TOOL, {})
                    payload = json.loads(directory.content[0].text)
                    assert payload["grantedToolCount"] == 2  # Host operation plus run-owned output pager
                    assert len(payload["unavailableHostTools"]) == 2
                    for name in ("new_host_tool", "compile_shader_workspace"):
                        rejected = await session.call_tool(INVOKE_TOOL, {"name": name, "arguments": {}})
                        assert rejected.isError
                second = await session.call_tool("inspect_event", {})
                assert json.loads(second.content[0].text)["calls"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("with_skill_root", [True, False])
async def test_proxy_exposes_core_tools_and_discovers_long_tail(
    tmp_path: Path, with_skill_root: bool, regular_python,
) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    allowed = READ_ONLY_TOOLS | EFFECTFUL_TOOLS
    environment = dict(os.environ)
    environment["REPLAYLAB_TEST_ALLOWED_TOOLS"] = json.dumps(sorted(allowed))
    environment["REPLAYLAB_TASK_ID"] = "proxy-tool-catalog"
    environment["REPLAYLAB_TASK_EVENT_ROOT"] = str(tmp_path / "events")
    registry = (tmp_path / "live-edit-registry.json").resolve()
    skill_args = ["--skill-root", str(_skill_root(tmp_path))] if with_skill_root else []
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "replaylab_agent.mcp_proxy",
            "--host",
            regular_python,
            "--host-arg=-m",
            "--host-arg=src.server",
            "--allow-env",
            "REPLAYLAB_TEST_ALLOWED_TOOLS",
            "--effect",
            "workspace",
            "--effect",
            "project",
            "--effect",
            "ui",
            "--effect",
            "replay",
            "--instance-id",
            "proxy-integration-test",
            "--registry-path",
            str(registry),
            "--max-tool-calls",
            "2",
            *skill_args,
        ],
        env=environment,
    )

    # MCP captures its default stderr at import time. Earlier capture tests may
    # have supplied a stream without fileno; a real subprocess needs a real file.
    with proxy_stderr(tmp_path / "mcp-stderr.log") as errlog:
        async with stdio_client(parameters, errlog=errlog) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                listing = await session.list_tools()
                listed = {tool.name: tool for tool in listing.tools}
                assert "inspect_event" in listed
                assert "event_id" in listed["inspect_event"].inputSchema["properties"]
                runtime = await session.call_tool("get_host_runtime_status", {})
                assert not runtime.isError, runtime.content
                assert "report_task_result" not in listed

                direct = await session.call_tool("ping", {})
                assert direct.isError
                assert "outside the direct core tools" in direct.content[0].text
                unloaded = await session.call_tool(
                    INVOKE_TOOL, {"name": "ping", "arguments": {}}
                )
                # Discovery is unnecessary for a granted tool. The call reaches
                # the Host, whose deliberately absent capture is a tool failure.
                assert unloaded.isError, unloaded.content
                assert "instance is not alive: proxy-integration-test" in unloaded.content[0].text
                exact = await session.call_tool(DIRECTORY_TOOL, {"tool": "ping"})
                contract = json.loads(exact.content[0].text)
                assert contract["tool"]["name"] == "ping"
                assert contract["tool"]["inputSchema"]["type"] == "object"
                assert contract["directlyAvailable"] is False
                assert contract["invokeWith"] == INVOKE_TOOL
                typed = await session.call_tool(DIRECTORY_TOOL, {"tool": "get_host_runtime_status"})
                typed_contract = json.loads(typed.content[0].text)
                assert typed_contract["directlyAvailable"] is True
                assert typed_contract["invokeWith"] == "get_host_runtime_status"
                assert typed_contract["tool"]["inputSchema"] == listed["get_host_runtime_status"].inputSchema
                matches = await session.call_tool(DIRECTORY_TOOL, {"query": "get_host_runtime_status"})
                assert json.loads(matches.content[0].text)["tools"][0]["directlyAvailable"] is True
                directory = await session.call_tool(DIRECTORY_TOOL, {})
                payload = json.loads(directory.content[0].text)
                assert "toolCallBudget" not in payload
                assert payload["directToolCount"] == len(CORE_DIRECT_TOOLS) + 1

    tools = {tool.name: tool for tool in listing.tools}
    assert set(tools) == AGENT_LOCAL_TOOLS | set(CORE_DIRECT_TOOLS) | {"read_evidence_file"}
    read_only = tools[DIRECTORY_TOOL].annotations
    assert read_only is not None
    assert read_only.readOnlyHint is True
    assert read_only.destructiveHint is False
    assert read_only.idempotentHint is True
    assert read_only.openWorldHint is False
    effectful = tools[INVOKE_TOOL].annotations
    assert effectful is not None
    assert effectful.readOnlyHint is False
    assert effectful.destructiveHint is True
    assert effectful.idempotentHint is False
    assert effectful.openWorldHint is False


def test_filter_requires_explicit_allow_list_and_fresh_registry(tmp_path: Path) -> None:
    host = (tmp_path / "BreadMCP.exe").resolve()
    host.write_bytes(b"host")
    registry = (tmp_path / "registry.json").resolve()

    with pytest.raises(ConfigurationError, match="explicit tool allow-list"):
        mcp_proxy._validate_configuration(_args(host, registry))

    registry.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="fresh absolute"):
        mcp_proxy._validate_configuration(
            _args(host, registry, "--allow", "inspect_event")
        )


def test_filter_rejects_allow_list_above_connection_effect_ceiling(tmp_path: Path) -> None:
    host = (tmp_path / "BreadMCP.exe").resolve()
    host.write_bytes(b"host")

    with pytest.raises(ConfigurationError, match="exceeds the effect ceiling"):
        mcp_proxy._validate_configuration(
            _args(
                host,
                (tmp_path / "registry.json").resolve(),
                "--allow",
                "prepare_shader_workspace",
            )
        )


def test_parameterized_effect_tool_is_granted_at_its_narrow_variants(
    tmp_path: Path,
) -> None:
    host = (tmp_path / "BreadMCP.exe").resolve()
    host.write_bytes(b"host")
    registry = (tmp_path / "registry.json").resolve()

    # Both the read-only and UI-only forms fit these route ceilings even though
    # the same tool also has a project-persisting form.
    mcp_proxy._validate_configuration(_args(
        host, registry, "--allow", "classify_current_frame_passes"
    ))
    mcp_proxy._validate_configuration(_args(
        host, registry, "--allow", "classify_current_frame_passes",
        "--effect", "ui",
    ))


@pytest.mark.asyncio
async def test_parameterized_effect_arguments_are_checked_at_dispatch() -> None:
    coordinator = mcp_proxy._ProxyCallCoordinator(
        effects=frozenset({"ui"}),
        scope=ToolCallScope(),
    )
    read_only = await coordinator.admit(
        "classify_current_frame_passes",
        {"publish_to_ui": False, "persist_project": False},
    )
    await coordinator.finish(read_only)
    ui_only = await coordinator.admit(
        "classify_current_frame_passes",
        {"publish_to_ui": True, "persist_project": False},
    )
    await coordinator.finish(ui_only)
    with pytest.raises(PolicyError, match="effect ceiling"):
        await coordinator.admit(
            "classify_current_frame_passes",
            {"publish_to_ui": False, "persist_project": True},
        )
    for coercible in ("true", 1):
        with pytest.raises(PolicyError, match="must be a JSON boolean"):
            await coordinator.admit(
                "classify_current_frame_passes",
                {"publish_to_ui": False, "persist_project": coercible},
            )


@pytest.mark.asyncio
async def test_status_admission_passes_a_running_long_effect_call() -> None:
    coordinator = mcp_proxy._ProxyCallCoordinator(
        effects=frozenset({"workspace"}),
        scope=ToolCallScope(),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def long_call() -> None:
        admission = await coordinator.admit("compile_shader_workspace", {})
        started.set()
        try:
            await release.wait()
        finally:
            await coordinator.finish(admission)

    task = asyncio.create_task(long_call())
    await started.wait()
    status = await asyncio.wait_for(
        coordinator.admit("get_host_runtime_status", {}), timeout=0.5
    )
    await coordinator.finish(status)
    assert not task.done()
    release.set()
    await task


@pytest.mark.asyncio
async def test_effect_mutations_share_one_lane() -> None:
    coordinator = mcp_proxy._ProxyCallCoordinator(
        effects=frozenset({"workspace"}),
        scope=ToolCallScope(),
    )
    active = 0
    max_active = 0

    async def mutation() -> None:
        nonlocal active, max_active
        admission = await coordinator.admit("compile_shader_workspace", {})
        try:
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1
        finally:
            await coordinator.finish(admission)

    await asyncio.gather(mutation(), mutation())
    assert max_active == 1


@pytest.mark.asyncio
async def test_cancelled_or_failed_effect_releases_the_next_mutation() -> None:
    coordinator = mcp_proxy._ProxyCallCoordinator(
        effects=frozenset({"workspace"}),
        scope=ToolCallScope(),
    )
    started = asyncio.Event()
    never = asyncio.Event()

    async def cancelled_mutation() -> None:
        admission = await coordinator.admit("compile_shader_workspace", {})
        started.set()
        try:
            await never.wait()
        finally:
            await coordinator.finish(admission)

    first = asyncio.create_task(cancelled_mutation())
    await started.wait()
    waiting = asyncio.create_task(
        coordinator.admit("prepare_shader_workspace", {})
    )
    await asyncio.sleep(0)
    assert not waiting.done()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = await asyncio.wait_for(waiting, timeout=0.5)
    await coordinator.finish(second)

    async def failed_mutation() -> None:
        admission = await coordinator.admit("compile_shader_workspace", {})
        try:
            raise RuntimeError("upstream failed")
        finally:
            await coordinator.finish(admission)

    with pytest.raises(RuntimeError, match="upstream failed"):
        await failed_mutation()
    after_failure = await asyncio.wait_for(
        coordinator.admit("prepare_shader_workspace", {}), timeout=0.5
    )
    await coordinator.finish(after_failure)


@pytest.mark.asyncio
async def test_same_client_session_can_poll_while_long_host_call_runs(
    tmp_path: Path, regular_python,
) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    environment = dict(os.environ)
    environment["REPLAYLAB_TASK_ID"] = "proxy-concurrent-status"
    environment["REPLAYLAB_TASK_EVENT_ROOT"] = str(tmp_path / "events")
    fixture_host = Path(__file__).parent / "fixtures" / "fake_concurrent_mcp_host.py"
    registry = (tmp_path / "live-edit-registry.json").resolve()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "replaylab_agent.mcp_proxy",
            "--host", regular_python,
            "--host-arg", str(fixture_host),
            "--allow", "compile_shader_workspace",
            "--allow", "get_host_runtime_status",
            "--effect", "workspace",
            "--instance-id", "proxy-concurrent-test",
            "--registry-path", str(registry),
            "--max-tool-calls", "4",
        ],
        env=environment,
    )
    with proxy_stderr(tmp_path / "mcp-concurrent-stderr.log") as errlog:
        async with stdio_client(parameters, errlog=errlog) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                long_call = asyncio.create_task(
                    session.call_tool("compile_shader_workspace", {})
                )
                await asyncio.sleep(0.15)
                status = await asyncio.wait_for(
                    session.call_tool("get_host_runtime_status", {}), timeout=0.8
                )
                assert not status.isError, status.content
                payload = json.loads(status.content[0].text)
                assert payload["toolStatus"] == "busy"
                assert not long_call.done()
                completed = await long_call
                assert not completed.isError, completed.content


@pytest.mark.asyncio
async def test_same_client_session_serializes_two_effect_mutations(
    tmp_path: Path, regular_python,
) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    environment = dict(os.environ)
    environment["REPLAYLAB_TASK_ID"] = "proxy-serialized-effects"
    environment["REPLAYLAB_TASK_EVENT_ROOT"] = str(tmp_path / "events")
    fixture_host = Path(__file__).parent / "fixtures" / "fake_concurrent_mcp_host.py"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "replaylab_agent.mcp_proxy",
            "--host", regular_python,
            "--host-arg", str(fixture_host),
            "--allow", "compile_shader_workspace",
            "--allow", "get_host_runtime_status",
            "--effect", "workspace",
            "--instance-id", "proxy-serialized-effects-test",
            "--registry-path", str((tmp_path / "registry.json").resolve()),
            "--max-tool-calls", "4",
        ],
        env=environment,
    )
    with proxy_stderr(tmp_path / "mcp-effects-stderr.log") as errlog:
        async with stdio_client(parameters, errlog=errlog) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                first, second = await asyncio.gather(
                    session.call_tool(
                        "compile_shader_workspace", {"delay_seconds": 0.2}
                    ),
                    session.call_tool(
                        "compile_shader_workspace", {"delay_seconds": 0.2}
                    ),
                )
                assert not first.isError and not second.isError
                assert json.loads(first.content[0].text)["maxActive"] == 1
                assert json.loads(second.content[0].text)["maxActive"] == 1
                status = await session.call_tool("get_host_runtime_status", {})
                status_payload = json.loads(status.content[0].text)
                assert status_payload["effectCalls"] == 2
                assert status_payload["maxActive"] == 1


@pytest.mark.asyncio
async def test_proxy_spills_one_large_effect_receipt_without_retrying_it(
    tmp_path: Path, regular_python,
) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from replaylab_agent.tool_output import MAX_POINTER_BYTES

    environment = dict(os.environ)
    environment["REPLAYLAB_TASK_ID"] = "proxy-large-receipt"
    environment["REPLAYLAB_TASK_EVENT_ROOT"] = str(tmp_path / "events")
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    environment["TEMP"] = str(temp_root)
    environment["TMP"] = str(temp_root)
    fixture_host = Path(__file__).parent / "fixtures" / "fake_concurrent_mcp_host.py"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "replaylab_agent.mcp_proxy",
            "--host", regular_python,
            "--host-arg", str(fixture_host),
            "--allow", "compile_shader_workspace",
            "--allow", "get_host_runtime_status",
            "--effect", "workspace",
            "--instance-id", "proxy-large-receipt-test",
            "--registry-path", str((tmp_path / "registry.json").resolve()),
            "--max-tool-calls", "4",
        ],
        env=environment,
    )
    with proxy_stderr(tmp_path / "mcp-large-stderr.log") as errlog:
        async with stdio_client(parameters, errlog=errlog) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.call_tool(
                    "compile_shader_workspace", {"large_receipt": True}
                )
                assert not result.isError, result.content
                pointer_text = result.content[0].text
                assert len(pointer_text.encode("utf-8")) < MAX_POINTER_BYTES
                pointer = json.loads(pointer_text)
                assert pointer["schema"] == "replaylab-tool-output-pointer/v1"
                assert pointer["operationId"] == "large-operation-1"
                assert pointer["status"] == "done"
                assert pointer["applied"] is True
                assert pointer["accepted"] is True
                assert pointer["inline"] is False and pointer["doNotRetry"] is True
                saved = Path(pointer["path"])
                payload = json.loads(saved.read_text(encoding="utf-8"))
                assert payload["invocationCount"] == 1
                assert len(payload["receipt"].encode("utf-8")) == 300_000
                assert result.structuredContent == {"result": pointer_text}

                located = await session.call_tool("read_evidence_file", {
                    "path": pointer["path"], "text_query": "receipt"})
                assert not located.isError, located.content
                search = json.loads(located.content[0].text)
                assert search["textQuery"] == "receipt" and search["matches"]
                assert search["replayRequired"] is False

                status = await session.call_tool("get_host_runtime_status", {})
                assert json.loads(status.content[0].text)["effectCalls"] == 1


@pytest.mark.asyncio
async def test_host_operation_counter_does_not_limit_calls() -> None:
    coordinator = mcp_proxy._ProxyCallCoordinator(
        effects=frozenset(),
        scope=ToolCallScope(),
    )
    operation = await coordinator.admit("ping", {})
    await coordinator.finish(operation)
    assert coordinator.calls == 1
    another = await coordinator.admit("ping", {})
    await coordinator.finish(another)
    assert coordinator.calls == 2


def test_replay_effect_requires_workspace_and_legacy_budget_is_ignored(tmp_path: Path) -> None:
    host = (tmp_path / "BreadMCP.exe").resolve()
    host.write_bytes(b"host")
    registry = (tmp_path / "registry.json").resolve()

    with pytest.raises(ConfigurationError, match="replay effect requires workspace"):
        mcp_proxy._validate_configuration(
            _args(host, registry, "--allow", "inspect_event", "--effect", "replay")
        )
    # Old callers may still pass the field; it no longer changes admission.
    mcp_proxy._validate_configuration(
        _args(
            host,
            registry,
            "--allow",
            "inspect_event",
            "--max-tool-calls",
            "-1",
        )
    )


def test_shader_catalog_bootstrap_cannot_enter_through_read_only_scope(tmp_path: Path) -> None:
    host = (tmp_path / "BreadMCP.exe").resolve()
    host.write_bytes(b"host")
    with pytest.raises(ConfigurationError, match="exceeds the effect ceiling"):
        mcp_proxy._validate_configuration(_args(
            host, (tmp_path / "registry.json").resolve(),
            "--allow", "get_active_shader_workspace_path",
        ))


def test_native_ui_scope_allows_a_later_explicit_tool_target() -> None:
    scope = ToolCallScope.from_request(
        {
            "capture": {"captureSessionId": "a" * 32, "path": "D:/capture.rdc"},
            "events": {"currentEventId": 11329},
        },
        {"kind": "explain-current-event", "arguments": {"eventId": 11329}},
    )

    scope.validate("inspect_event", {"event_id": 11329})
    # Reads and effectful calls may follow the user's newer EID. Concrete Host
    # operations still validate their own capture identity and write target.
    scope.validate("inspect_event", {"event_id": 7})
    scope.validate("get_event_resource_graph", {"event_id": 11328})
    scope.validate("apply_shader_workspace_candidate", {"event_id": 7})
    assert ToolCallScope.from_json(scope.to_json()) == scope


def test_proxy_forwards_its_capture_scope_instead_of_ambient_previous_task(tmp_path, monkeypatch):
    host = (tmp_path / "BreadMCP.exe").resolve()
    host.write_bytes(b"host")
    admitted = ToolCallScope(capture_session_id="a" * 32, capture_path="D:/game-a/frame.rdc")
    monkeypatch.setenv("REPLAYLAB_TOOL_SCOPE", ToolCallScope(
        capture_session_id="b" * 32, capture_path="D:/game-b/frame.rdc").to_json())
    *_, environment = mcp_proxy._validate_configuration(_args(
        host, (tmp_path / "registry.json").resolve(), "--allow", "inspect_event",
        "--scope-json", admitted.to_json()))
    assert ToolCallScope.from_json(environment["REPLAYLAB_TOOL_SCOPE"]) == admitted
    assert environment["REPLAYLAB_INSTANCE_ID"] == "instance-1"


def _reverse_shader_scope() -> ToolCallScope:
    return ToolCallScope.from_request(
        {
            "capture": {
                "captureSessionId": "a" * 32,
                "captureId": "b" * 64,
                "path": "D:/capture.rdc",
            },
            "events": {"currentEventId": 11402},
            "shaders": {
                "focused": {
                    "resourceId": "1000000000000000723",
                    "stage": "fragment",
                    "entryPoint": "MainPS",
                    "rawSha256": "c" * 64,
                }
            },
        },
        {"kind": "reverse-edit-selected-shader", "arguments": {"eventId": 11402}},
    )


def test_shader_action_allows_a_new_conversation_target_without_binding_state(tmp_path: Path):
    scope = _reverse_shader_scope()
    workspace = str((tmp_path / "managed-workspace").resolve())
    scope.validate("compile_shader_workspace", {"workspace_path": workspace})
    scope.validate("begin_shader_workspace_organization", {
        "workspace_path": workspace, "event_id": 99999, "stage": "PS",
    })








def test_explicit_event_does_not_inherit_an_unrelated_ui_shader() -> None:
    context = {
        "events": {"currentEventId": 100}, "focusStage": "VS",
        "shaders": {"focused": {
            "resourceId": "123", "stage": "VS", "entryPoint": "UiMain",
            "rawSha256": "a" * 64,
        }},
    }
    scope = ToolCallScope.from_request(context, {
        "kind": "reverse-edit-selected-shader", "arguments": {"eventId": 200},
    })
    assert scope.event_id == 200
    assert scope.resource_id is None
    assert scope.raw_sha256 == scope.stage == scope.entry_point == ""
    assert ToolCallScope.from_json(scope.to_json()) == scope






def test_freeform_conversation_eid_is_not_restricted_by_ui_snapshot() -> None:
    scope = ToolCallScope.from_request({
        "events": {"currentEventId": 100},
        "capture": {"captureSessionId": "a" * 32},
    }, None)
    scope.validate("begin_shader_workspace_organization", {"event_id": 200, "stage": "PS"})


def _skill_root(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "replaylab-core").mkdir(parents=True)
    (root / "replaylab-core" / "SKILL.md").write_text(
        "---\nname: replaylab-core\ndescription: Standing rules.\nmetadata:\n"
        "  replaylab-category: core\n  replaylab-always: \"true\"\n---\nCopy identifiers exactly.\n",
        encoding="utf-8")
    (root / "renderdoc-explain-event").mkdir(parents=True)
    (root / "renderdoc-explain-event" / "SKILL.md").write_text(
        "---\nname: renderdoc-explain-event\ndescription: Analyze one event.\nmetadata:\n"
        "  replaylab-category: event-pipeline\n---\nUse inspect_event and cite exact values.\n",
        encoding="utf-8")
    return root


def test_capability_directory_and_skill_loader_are_served_locally(tmp_path: Path) -> None:
    """Intent tools exist before any Host operation is called."""

    host = (tmp_path / "BreadMCP.exe").resolve()
    host.write_bytes(b"host")
    registry = (tmp_path / "registry.json").resolve()
    args = _args(host, registry, "--allow", "inspect_event",
                 "--skill-root", str(_skill_root(tmp_path)))
    gateway = mcp_proxy._skill_gateway(args, allowed=frozenset({"inspect_event"}),
                                       effects=frozenset())
    assert gateway is not None
    directory = gateway.directory()
    assert [item["id"] for item in directory["skills"]] == [
        "renderdoc-explain-event", "replaylab-core",
    ]
    assert directory["effectClasses"] == ["read-only"]
    assert "toolCallBudget" not in directory
    loaded = gateway.load("renderdoc-explain-event")
    assert "Use inspect_event and cite exact values." in loaded["instructions"]

    # A native route with no Skill root still has a compact tool directory.
    empty_gateway = mcp_proxy._skill_gateway(
        _args(host, registry, "--allow", "inspect_event"),
        allowed=frozenset({"inspect_event"}), effects=frozenset(),
    )
    assert empty_gateway is not None
    assert empty_gateway.directory()["skills"] == []
    # API mode requests the full private Host catalog explicitly.
    assert mcp_proxy._skill_gateway(
        _args(host, registry, "--allow", "inspect_event", "--full-host-catalog"),
        allowed=frozenset({"inspect_event"}), effects=frozenset(),
    ) is None


def test_skill_problem_does_not_block_a_task_scoped_host_route(tmp_path: Path) -> None:
    host = (tmp_path / "BreadMCP.exe").resolve()
    host.write_bytes(b"host")
    registry = (tmp_path / "registry.json").resolve()
    missing = _args(
        host, registry, "--allow", "inspect_event",
        "--skill-root", str(tmp_path / "missing"),
    )
    missing_gateway = mcp_proxy._skill_gateway(
        missing, allowed=frozenset({"inspect_event"}), effects=frozenset()
    )
    assert missing_gateway is not None
    assert missing_gateway.directory()["skills"] == []

    broken_root = tmp_path / "broken-skills"
    (broken_root / "not-core").mkdir(parents=True)
    (broken_root / "not-core" / "SKILL.md").write_text(
        "---\nname: not-core\ndescription: optional\n---\nNothing.\n",
        encoding="utf-8",
    )
    broken = _args(
        host, registry, "--allow", "inspect_event",
        "--skill-root", str(broken_root),
    )
    optional_gateway = mcp_proxy._skill_gateway(
        broken, allowed=frozenset({"inspect_event"}), effects=frozenset()
    )
    assert optional_gateway is not None
    assert [item["id"] for item in optional_gateway.directory()["skills"]] == ["not-core"]


def test_local_intent_tools_are_never_part_of_the_host_effect_manifest() -> None:
    from replaylab_agent.policy import AGENT_LOCAL_TOOLS

    assert not (AGENT_LOCAL_TOOLS & (READ_ONLY_TOOLS | EFFECTFUL_TOOLS | frozenset(TOOL_EFFECTS)))
    # A Host that does not register them still validates: they are filter-owned.
    validate_registered_tool_effect_manifest(
        (READ_ONLY_TOOLS | EFFECTFUL_TOOLS) | AGENT_LOCAL_TOOLS)
    for name in AGENT_LOCAL_TOOLS:
        assert tool_call_effects(name, {}) == frozenset()


@pytest.mark.asyncio
async def test_frame_flow_retry_limit_is_released_by_valid_preflight(tmp_path):
    from types import SimpleNamespace
    from replaylab_agent.errors import PolicyError
    from replaylab_agent.events import TaskEventWriter
    from replaylab_agent.mcp_proxy import _FrameFlowCalls

    class Host:
        async def call_tool(self, name, arguments):
            if name == "validate_frame_render_flow":
                payload = {"valid": arguments["corrected"]}
            else:
                payload = {"error": "validation failed", "type": "FrameFlowValidationError",
                           "issues": [{"path": "stages[0].category", "message": "Choose an allowed category"}]}
            return SimpleNamespace(structuredContent=payload, content=[], isError=False)

    calls = _FrameFlowCalls({})
    calls.writer = TaskEventWriter(tmp_path, "frame-flow")
    host = Host()
    publish = "publish_frame_render_flow"
    validate = "validate_frame_render_flow"
    args = {"flow_id": "flow-1"}
    for _ in range(3):
        await calls.call(host, publish, args)
    with pytest.raises(PolicyError, match=r"stages\[0\].category"):
        await calls.call(host, publish, args)
    await calls.call(host, validate, {**args, "corrected": False})
    with pytest.raises(PolicyError, match="Publication paused"):
        await calls.call(host, publish, args)
    await calls.call(host, validate, {**args, "corrected": True})
    await calls.call(host, publish, args)
    events = [json.loads(row) for row in calls.writer.path.read_text(encoding="utf-8").splitlines()]
    assert [event["kind"] for event in events] == ["tool-started", "tool-completed"] * 6
    assert events[1]["data"]["status"] == "failed"
    assert events[1]["data"]["publicDetail"] == "stages[0].category: Choose an allowed category"
    assert events[1]["data"]["elapsedMs"] >= 0
