"""Pixels survive the exact native-agent MCP proxy and installed API serializers.

No model requests are made. Tests run two real stdio processes (Host and task
proxy) and inspect the packets the Codex/ACP client receives, then serialize the
same result through the API libraries used by Bread.
"""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sys

from PIL import Image
import pytest

from replaylab_agent.deep_engine import _mcp_result_payload
from replaylab_agent.skill_gateway import INVOKE_TOOL


def _image_bytes():
    buffer = BytesIO()
    Image.new("RGB", (32, 16), (35, 125, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("via_dispatcher", [False, True])
async def test_real_stdio_proxy_preserves_host_images_and_large_receipt_guard(tmp_path, via_dispatcher):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_openai.chat_models.base import _construct_responses_api_input
    from langchain_anthropic.chat_models import _format_messages

    raw = _image_bytes()
    source = tmp_path / "rt.png"
    source.write_bytes(raw)
    env = dict(os.environ)
    env["REPLAYLAB_TASK_EVENT_ROOT"] = str(tmp_path / "events")
    env["LOCALAPPDATA"] = str(tmp_path / "local")
    env["XDG_DATA_HOME"] = str(tmp_path / "local")
    env["PYTHONPATH"] = os.pathsep.join([
        str(Path(__file__).resolve().parents[1] / "src"),
        str(Path(__file__).resolve().parents[1] / "mcp_host"),
        env.get("PYTHONPATH", ""),
    ])
    fixture = Path(__file__).parent / "fixtures" / "media_mcp_host.py"
    args = ["-m", "replaylab_agent.mcp_proxy", "--host", sys.executable,
            "--host-arg", str(fixture), "--host-arg", str(tmp_path),
            "--instance-id", "media-transport-test",
            "--registry-path", str(tmp_path / "registry.json"),
            "--effect", "workspace",
            "--allow", "read_evidence_file", "--allow", "capture_event_texture_evidence",
            "--allow", INVOKE_TOOL]
    if not via_dispatcher:
        args.append("--full-host-catalog")
    parameters = StdioServerParameters(command=sys.executable, args=args, env=env)
    with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as streams:
            async with ClientSession(*streams) as session:
                try:
                    await session.initialize()
                except Exception:
                    pytest.fail((tmp_path / "mcp-stderr.log").read_text(encoding="utf-8"))
                async def call(name, arguments):
                    return await session.call_tool(
                        INVOKE_TOOL if via_dispatcher else name,
                        {"name": name, "arguments": arguments} if via_dispatcher else arguments)
                for event_id in (41, 42):
                    result = await call("capture_event_texture_evidence",
                                        {"event_id": event_id, "attachment_kind": "rtv"})
                    assert not result.isError, result.content
                    images = [block for block in result.content if block.type == "image"]
                    assert len(images) == 1
                    image_bytes = base64.b64decode(images[0].data)
                    pixels = Image.open(BytesIO(image_bytes))
                    assert pixels.size == (32, 16)
                    assert pixels.getpixel((0, 0)) == (35, 125, 200)
                    # An oversized textual receipt can be paged; its picture must
                    # remain an actual image block, not base64 inside text/JSON.
                    text = "\n".join(block.text for block in result.content if block.type == "text")
                    assert images[0].data not in text
                    if event_id == 41:
                        assert json.loads(text)["modelImage"]["sha256"] == hashlib.sha256(image_bytes).hexdigest()
                    else:
                        assert len(text.encode("utf-8")) < 24 * 1024

                    for protocol in ("openai-responses", "anthropic-messages"):
                        blocks = _mcp_result_payload(result, protocol, "capture_event_texture_evidence")
                        messages = [AIMessage(content="", tool_calls=[{
                            "name": "capture_event_texture_evidence", "args": {}, "id": "call_image"}]),
                            ToolMessage(content=blocks, tool_call_id="call_image")]
                        if protocol == "openai-responses":
                            wire = _construct_responses_api_input(messages)
                            image = wire[-1]["output"][1]
                            assert image["type"] == "input_image"
                            delivered = image["image_url"].split(",", 1)[1]
                        else:
                            _, wire = _format_messages(messages, model=None)
                            image = wire[-1]["content"][0]["content"][1]
                            assert image["type"] == "image"
                            delivered = image["source"]["data"]
                        assert base64.b64decode(delivered) == image_bytes
                    for protocol in ("openai-chat", "azure-openai", "google-genai", "google-cloud"):
                        text_only = _mcp_result_payload(result, protocol, "capture_event_texture_evidence")
                        assert isinstance(text_only, str)
                        assert "not shown" in text_only
                        assert "Do not describe content you have not received" in text_only

                read = await call("read_evidence_file", {"path": str(source), "inline": True})
                assert not read.isError
                image = next(block for block in read.content if block.type == "image")
                assert base64.b64decode(image.data) == raw
                metadata = await call("capture_event_texture_evidence", {
                    "event_id": 41, "attachment_kind": "rtv", "inline": False})
                assert [block.type for block in metadata.content] == ["text"]
                assert json.loads(metadata.content[0].text)["modelImage"]["included"] is False
