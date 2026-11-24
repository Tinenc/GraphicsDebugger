"""Long native inputs survive the real MCP route without ambient file access."""
import json
import os
from pathlib import Path
import sys

import pytest

from replaylab_agent.native_mcp import NativeMCPRoute
from replaylab_agent.profiles import Profile
from replaylab_agent.task_input import (TASK_INPUT_URI, load_task_input, native_task_reference,
                                        prepare_native_task_input)
from replaylab_agent.tool_output import OwnedOutputStore


def route(tmp_path):
    return NativeMCPRoute(sys.executable, ("-m", "replaylab_agent.mcp_proxy", "--allow", "read_evidence_file"),
                          {}, frozenset({"read_evidence_file"}), frozenset(), tmp_path / "registry.json")


def test_exact_native_input_is_bound_to_private_descriptor_without_expanding_grants(tmp_path):
    original = "Find TARGET in this original input\n" + "data\n" * 20000 + "TARGET=法线\n" + "more data\n" * 20000
    previous = route(tmp_path)
    projection, admitted = prepare_native_task_input(original, previous,
        profile=Profile(id="native", preset="custom-acp", command=[sys.executable]), model="")
    assert len(projection) < 4000 and "TARGET=法线" not in projection
    assert TASK_INPUT_URI in projection and "read_evidence_file" in projection
    assert admitted.allowed_tools == previous.allowed_tools and admitted.effects == previous.effects
    assert admitted.env == previous.env
    assert original not in " ".join(admitted.args)
    descriptor = Path(admitted.args[-1])
    assert load_task_input(descriptor) == original
    assert TASK_INPUT_URI in native_task_reference(admitted)
    value = json.loads(descriptor.read_text(encoding="utf-8"))
    value["text"] += " altered"
    descriptor.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="content receipt"):
        load_task_input(descriptor)


def test_task_input_alias_is_readable_only_in_its_admitted_run():
    owner, other = OwnedOutputStore(), OwnedOutputStore()
    try:
        owner.admit_task_input("Unicode 法线 input")
        page = owner.read({"path": TASK_INPUT_URI, "max_bytes": 100})
        assert page["content"] == "Unicode 法线 input"
        assert other.read({"path": TASK_INPUT_URI}) is None
        assert page["authority"] == "task-user-input"
    finally:
        owner.close()
        other.close()


def test_private_native_input_is_not_presented_as_a_delivered_output(tmp_path):
    from replaylab_agent.agent_context import ProviderContext
    profile = Profile(id="native", preset="custom-acp", command=[sys.executable])
    context = ProviderContext(profile=profile, model="", instance_id="", context={}, intent=None,
                              task_id="private-input", workspace_base=tmp_path / "work")
    _, turn_root = context.new_turn_workspace()
    _, admitted = prepare_native_task_input("Input data\n" * 20000, route(turn_root), profile=profile, model="")
    assert load_task_input(Path(admitted.args[-1])).startswith("Input data")
    assert ProviderContext.cleanup_turn_workspace(turn_root) is None
    assert not turn_root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatcher", [False, True])
async def test_real_stdio_task_input_paging_retains_middle_without_host_or_native_read(tmp_path, dispatcher):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    original = "begin\n" + "unchanged input\n" * 5000 + "TARGET=法线纹理\n" + "tail data\n" * 5000
    projection, admitted = prepare_native_task_input(original, route(tmp_path),
        profile=Profile(id="native", preset="custom-acp", command=[sys.executable]), model="")
    assert "TARGET=法线纹理" not in projection
    args = list(admitted.args)
    if not dispatcher:
        args.append("--full-host-catalog")
    else:
        args += ["--allow", "invoke_bread_tool"]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join([
        str(Path(__file__).resolve().parents[1] / "src"),
        str(Path(__file__).resolve().parents[1] / "mcp_host"),
    ])
    environment["REPLAYLAB_TASK_ID"] = "native-input-paging"
    environment["REPLAYLAB_TASK_EVENT_ROOT"] = str(tmp_path / "events")
    params = StdioServerParameters(command=sys.executable, args=args, env=environment)
    with (tmp_path / "stderr.log").open("w", encoding="utf-8") as errors:
        async with stdio_client(params, errlog=errors) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                arguments = {"path": TASK_INPUT_URI, "offset": 0, "max_bytes": 1024, "text_query": "TARGET="}
                result = await session.call_tool("invoke_bread_tool" if dispatcher else "read_evidence_file",
                    {"name": "read_evidence_file", "arguments": arguments} if dispatcher else arguments)
                assert not result.isError, result.content
                text = "\n".join(block.text for block in result.content if block.type == "text")
                assert "TARGET=法线纹理" in text
                assert "task-user-input" in text
                assert "sha256" in text
