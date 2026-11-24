"""Getting a picture, a document or a sound to the model — or saying we cannot.

The ACP agents open a saved file themselves. `deep_engine` cannot: it renders every
tool result as text, so anything that is not text used to vanish without trace and the
model would describe a render target it had never seen. These tests pin the shapes that
actually survive each provider's stack — checked by reading langchain-core,
langchain-anthropic, langchain-openai and the two vendor SDKs — and pin that whatever
cannot be carried is named in words rather than dropped.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from replaylab_agent import tool_media
from replaylab_agent.tool_media import (
    build_tool_result, carries, collect_media, protocol_media,
)


class _Block:
    def __init__(self, **fields) -> None:
        self.__dict__.update(fields)


class _Result:
    def __init__(self, *content, is_error: bool = False) -> None:
        self.content = list(content)
        self.isError = is_error


def _image(data: bytes = b"\x89PNG-ish", media_type: str = "image/png") -> _Block:
    return _Block(type="image", data=base64.b64encode(data).decode("ascii"),
                  mimeType=media_type)


def _resource(data: bytes = b"%PDF-1.7", media_type: str = "application/pdf",
              uri: str = "file:///e/spec.pdf") -> _Block:
    return _Block(type="resource",
                  resource=_Block(blob=base64.b64encode(data).decode("ascii"),
                                  mimeType=media_type, uri=uri))


class CollectMediaTests(unittest.TestCase):
    def test_an_image_block_is_found_and_kept_as_base64(self) -> None:
        result = _Result(_Block(type="text", text="saved"), _image(b"1234"))
        media = collect_media(result)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["kind"], "image")
        self.assertEqual(base64.b64decode(media[0]["base64"]), b"1234")

    def test_an_embedded_resource_carries_its_media_type_and_name(self) -> None:
        media = collect_media(_Result(_resource()))
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["kind"], "document")
        self.assertEqual(media[0]["mediaType"], "application/pdf")
        self.assertEqual(media[0]["name"], "spec.pdf")

    def test_audio_and_video_are_collected_even_though_no_provider_takes_them(self) -> None:
        """Collected, then honestly refused: the model must hear about it either way."""

        media = collect_media(_Result(
            _Block(type="audio", data="AAAA", mimeType="audio/wav"),
            _resource(b"ftypmp42", "video/mp4", "file:///e/clip.mp4")))
        self.assertEqual([item["kind"] for item in media], ["audio", "video"])

    def test_raw_bytes_are_encoded_rather_than_stringified(self) -> None:
        media = collect_media(_Result(_Block(type="image", data=b"\x00\x01\x02",
                                             mimeType="image/png")))
        self.assertEqual(base64.b64decode(media[0]["base64"]), b"\x00\x01\x02")

    def test_a_text_resource_is_left_to_the_text_path(self) -> None:
        block = _Block(type="resource", resource=_Block(text="hello", mimeType="text/plain",
                                                        uri="file:///e/a.txt"))
        self.assertEqual(collect_media(_Result(block)), [])

    def test_a_result_with_nothing_but_text_yields_nothing(self) -> None:
        self.assertEqual(collect_media(_Result(_Block(type="text", text="hi"))), [])
        self.assertEqual(collect_media(object()), [])


class ProtocolCapabilityTests(unittest.TestCase):
    def test_the_table_matches_what_each_sdk_accepts_in_a_tool_result(self) -> None:
        # anthropic: ToolResultBlockParam.Content includes image and document;
        # Base64PDFSourceParam is application/pdf only.
        self.assertTrue(carries("anthropic-messages", "image", "image/png"))
        self.assertTrue(carries("anthropic-messages", "document", "application/pdf"))
        # openai responses: input_image / input_file survive; chat completions is text.
        self.assertTrue(carries("openai-responses", "image", "image/webp"))
        self.assertTrue(carries("openai-responses", "document", "application/pdf"))
        self.assertEqual(protocol_media("openai-chat"), {})

    def test_no_provider_here_claims_audio_or_video(self) -> None:
        for protocol in PROTOCOLS:
            self.assertFalse(carries(protocol, "audio", "audio/wav"))
            self.assertFalse(carries(protocol, "video", "video/mp4"))

    def test_an_unknown_protocol_carries_nothing_rather_than_guessing(self) -> None:
        self.assertEqual(protocol_media("some-new-vendor"), {})
        self.assertEqual(protocol_media(""), {})


PROTOCOLS = ("anthropic-messages", "openai-responses", "openai-chat")


class BuildToolResultTests(unittest.TestCase):
    def test_without_media_every_existing_route_is_unchanged(self) -> None:
        for protocol in PROTOCOLS:
            self.assertEqual(build_tool_result("plain text", [], protocol), "plain text")

    def test_anthropic_gets_the_nested_shape_the_api_actually_accepts(self) -> None:
        media = collect_media(_Result(_image(b"abcd")))
        blocks = build_tool_result("saved to rt.png", media, "anthropic-messages")
        self.assertEqual(blocks[0], {"type": "text", "text": "saved to rt.png"})
        # Not LangChain's own {"type": "image", "base64": ...}: nested tool_result
        # content is passed through unconverted, so it must already be Anthropic's.
        self.assertEqual(blocks[1]["source"],
                         {"type": "base64", "media_type": "image/png",
                          "data": base64.b64encode(b"abcd").decode("ascii")})

    def test_anthropic_takes_a_pdf_as_a_document_block(self) -> None:
        blocks = build_tool_result("saved", collect_media(_Result(_resource())),
                                   "anthropic-messages")
        self.assertEqual(blocks[1]["type"], "document")
        self.assertEqual(blocks[1]["source"]["media_type"], "application/pdf")
        self.assertEqual(blocks[1]["title"], "spec.pdf")

    def test_anthropic_takes_plain_text_as_a_text_source_not_base64(self) -> None:
        # Base64PDFSourceParam is PDF-only; a base64 text/plain document is a 400.
        media = collect_media(_Result(_resource(b"hello world", "text/plain", "file:///e/notes.txt")))
        blocks = build_tool_result("saved", media, "anthropic-messages")
        self.assertEqual(blocks[1]["source"],
                         {"type": "text", "media_type": "text/plain", "data": "hello world"})

    def test_openai_responses_gets_the_chat_style_shapes_that_survive_both_layers(self) -> None:
        media = collect_media(_Result(_image(b"abcd"), _resource()))
        blocks = build_tool_result("saved", media, "openai-responses")
        # image_url and file are in langchain-core's TOOL_MESSAGE_BLOCK_TYPES and are
        # converted to input_image / input_file by langchain-openai; input_image is not.
        self.assertEqual(blocks[1]["type"], "image_url")
        self.assertTrue(blocks[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(blocks[2]["type"], "file")
        self.assertEqual(blocks[2]["file"]["filename"], "spec.pdf")
        self.assertTrue(blocks[2]["file"]["file_data"].startswith(
            "data:application/pdf;base64,"))

    def test_a_route_that_cannot_show_it_says_so_instead_of_dropping_it(self) -> None:
        text = build_tool_result("saved to rt.png", collect_media(_Result(_image())),
                                 "openai-chat")
        self.assertIsInstance(text, str)
        self.assertTrue(text.startswith("saved to rt.png"))
        self.assertIn("1 attachment(s) were produced but not shown", text)
        self.assertIn("cannot carry image content", text)
        self.assertIn("Do not describe content you have not received", text)

    def test_an_unsupported_kind_names_the_kind_and_offers_a_way_round(self) -> None:
        media = collect_media(_Result(_resource(b"ftyp", "video/mp4", "file:///e/clip.mp4")))
        text = build_tool_result("saved", media, "anthropic-messages")
        self.assertIn("cannot carry video content", text)
        self.assertIn("convert it with a tool you have registered", text)

    def test_a_supported_kind_in_the_wrong_format_says_which_formats_work(self) -> None:
        media = collect_media(_Result(_image(b"x", "image/tiff")))
        text = build_tool_result("saved", media, "anthropic-messages")
        self.assertIn("takes image as", text)
        self.assertIn("image/png", text)

    def test_one_oversized_item_does_not_stop_the_rest(self) -> None:
        big = {"kind": "image", "mediaType": "image/png", "base64": "x",
               "byteSize": tool_media.MAX_INLINE_ITEM_BYTES + 1, "name": ""}
        small = collect_media(_Result(_image(b"ok")))[0]
        blocks = build_tool_result("two", [big, small], "anthropic-messages")
        self.assertEqual([block["type"] for block in blocks], ["text", "image"])
        self.assertIn("over the", blocks[0]["text"])

    def test_the_budget_is_bytes_not_a_count_so_small_pictures_all_arrive(self) -> None:
        media = collect_media(_Result(*[_image(bytes([index]) * 16) for index in range(6)]))
        blocks = build_tool_result("six", media, "anthropic-messages")
        self.assertEqual(sum(1 for block in blocks if block["type"] == "image"), 6)
        self.assertNotIn("not shown", blocks[0]["text"])

    def test_the_total_budget_still_stops_a_runaway_result(self) -> None:
        half = tool_media.MAX_INLINE_TOTAL_BYTES // 2 + 1
        media = [{"kind": "image", "mediaType": "image/png", "base64": "x",
                  "byteSize": half, "name": ""} for _ in range(3)]
        blocks = build_tool_result("three", media, "anthropic-messages")
        self.assertEqual(sum(1 for block in blocks if block["type"] == "image"), 1)
        self.assertIn("inline budget is spent", blocks[0]["text"])


class DeepEngineWiringTests(unittest.TestCase):
    """The conversion the engine uses, exercised through the engine's own helper."""

    def _payload(self, result, protocol):
        from replaylab_agent import deep_engine

        return deep_engine._mcp_result_payload(result, protocol)

    def test_text_only_results_stay_strings_on_every_route(self) -> None:
        result = _Result(_Block(type="text", text="{\"ok\": true}"))
        for protocol in PROTOCOLS:
            self.assertEqual(self._payload(result, protocol), '{"ok": true}')

    def test_an_image_reaches_the_model_on_the_routes_that_can_take_it(self) -> None:
        result = _Result(_Block(type="text", text="saved"), _image(b"pix"))
        for protocol in ("anthropic-messages", "openai-responses"):
            blocks = self._payload(result, protocol)
            self.assertEqual(len(blocks), 2, protocol)
            self.assertEqual(blocks[0]["type"], "text")

    def test_a_failed_call_is_still_reported_as_text(self) -> None:
        result = _Result(_Block(type="text", text="boom"), _image(), is_error=True)
        payload = self._payload(result, "anthropic-messages")
        self.assertIsInstance(payload, str)
        self.assertIn("boom", payload)

    def test_sdk_images_do_not_duplicate_base64_into_text_on_any_api_route(self) -> None:
        from mcp.types import CallToolResult, ImageContent, TextContent
        from replaylab_agent.deep_engine import _mcp_result_text

        encoded = base64.b64encode(b"image-data" * 120000).decode("ascii")
        for text in ("", "preview ready"):
            result = CallToolResult(content=[ImageContent(type="image", data=encoded, mimeType="image/png")]
                                   + ([TextContent(type="text", text=text)] if text else []))
            self.assertEqual(_mcp_result_text(result), text)
            for protocol in (*PROTOCOLS, "azure-openai", "google-genai", "google-cloud"):
                payload = self._payload(result, protocol)
                if isinstance(payload, list):
                    self.assertNotIn(encoded[:200], payload[0]["text"])
                    self.assertEqual(json.dumps(payload).count(encoded), 1)
                else:
                    self.assertNotIn(encoded[:200], payload)
                    self.assertLess(len(payload), 2000)

    def test_embedded_text_links_and_structured_receipts_survive_without_binary_text(self) -> None:
        from mcp.types import CallToolResult, EmbeddedResource, TextResourceContents, ResourceLink
        from replaylab_agent.deep_engine import _mcp_result_text

        result = CallToolResult(content=[
            EmbeddedResource(type="resource", resource=TextResourceContents(
                uri="file:///e/source.hlsl", text="float roughness = 0.5;")),
            ResourceLink(type="resource_link", uri="file:///e/rt.png", name="SceneColor",
                         size=1234, _meta={"channel": "RGB"}),
        ], structuredContent={"revision": 3})
        text = _mcp_result_text(result)
        self.assertIn("float roughness = 0.5;", text)
        self.assertIn("SceneColor", text)
        link = json.loads(json.loads(text)["contentText"].splitlines()[-1])
        self.assertEqual(link["size"], 1234)
        self.assertEqual(link["_meta"], {"channel": "RGB"})
        self.assertEqual(json.loads(text)["structuredContent"]["revision"], 3)

    def test_binary_evidence_keeps_full_uri_and_metadata_on_text_only_routes(self) -> None:
        from mcp.types import CallToolResult, EmbeddedResource, BlobResourceContents
        from replaylab_agent.deep_engine import _mcp_result_text

        encoded = base64.b64encode(b"vertices" * 1000).decode("ascii")
        result = CallToolResult(content=[EmbeddedResource(type="resource", resource=BlobResourceContents(
            uri="file:///capture/mesh/vertices.bin", mimeType="application/octet-stream",
            blob=encoded, _meta={"vertexCount": 1000}))])
        metadata = json.loads(_mcp_result_text(result))
        self.assertNotIn("blob", metadata["resource"])
        self.assertEqual(metadata["resource"]["_meta"], {"vertexCount": 1000})
        for protocol in (*PROTOCOLS, "google-genai", "google-cloud", "azure-openai"):
            payload = self._payload(result, protocol)
            text = json.dumps(payload) if isinstance(payload, list) else payload
            self.assertIn("file:///capture/mesh/vertices.bin", text)
            self.assertNotIn(encoded, text)

    def test_large_json_is_saved_once_and_media_still_reaches_the_model(self) -> None:
        from replaylab_agent import deep_engine
        from replaylab_agent.tool_output import MAX_POINTER_BYTES

        original = json.dumps({
            "schema": "test-large-receipt/v1",
            "operationId": "operation-1",
            "status": "done",
            "applied": True,
            "accepted": True,
            "receipt": "数据" * 50_000,
        }, ensure_ascii=False, separators=(",", ":"))
        result = _Result(_Block(type="text", text=original), _image(b"pix"))
        # The MCP SDK commonly repeats the same JSON in structuredContent. It
        # must still produce one exact evidence file, not two copies.
        result.structuredContent = {"result": original}
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "replaylab_agent.tool_output._tool_output_root",
            return_value=Path(directory) / "replaylab-tool-output",
        ):
            blocks = deep_engine._mcp_result_payload(
                result, "anthropic-messages", "compile_shader_workspace"
            )
            self.assertIsInstance(blocks, list)
            pointer_text = blocks[0]["text"]
            self.assertLess(len(pointer_text.encode("utf-8")), MAX_POINTER_BYTES)
            pointer = json.loads(pointer_text)
            self.assertFalse(pointer["inline"])
            self.assertTrue(pointer["doNotRetry"])
            self.assertEqual(pointer["tool"], "compile_shader_workspace")
            self.assertEqual(pointer["operationId"], "operation-1")
            self.assertEqual(pointer["status"], "done")
            self.assertTrue(pointer["applied"] and pointer["accepted"])
            self.assertEqual(Path(pointer["path"]).read_text(encoding="utf-8"), original)
            self.assertEqual(blocks[1]["type"], "image")

    def test_guard_retention_removes_only_its_own_old_or_excess_files(self) -> None:
        from replaylab_agent import tool_output

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "replaylab-tool-output"
            root.mkdir()
            unrelated = root / "asset-reader-output.bin"
            unrelated.write_bytes(b"asset")
            old = root / (tool_output.TOOL_OUTPUT_PREFIX + "old.txt")
            old.write_bytes(b"old")
            os.utime(old, (time.time() - 100, time.time() - 100))
            recent = []
            for index in range(3):
                path = root / (
                    tool_output.TOOL_OUTPUT_PREFIX + "recent-%d.txt" % index
                )
                path.write_bytes(b"recent")
                os.utime(path, (time.time() + index, time.time() + index))
                recent.append(path)
            with (
                mock.patch("replaylab_agent.tool_output._tool_output_root", return_value=root),
                mock.patch.object(tool_output, "TOOL_OUTPUT_TTL_SECONDS", 10),
                mock.patch.object(tool_output, "TOOL_OUTPUT_MAX_FILES", 2),
            ):
                pointer = json.loads(tool_output.guard_tool_output_text(
                    "inspect_event", "x" * (tool_output.MAX_INLINE_TOOL_OUTPUT_BYTES + 1)
                ))

            assert Path(pointer["path"]).is_file()
            assert unrelated.read_bytes() == b"asset"
            assert not old.exists()
            owned = list(root.glob(tool_output.TOOL_OUTPUT_PREFIX + "*"))
            assert len(owned) <= 2

    def test_large_reuse_catalog_retains_source_choices_and_exact_evidence(self) -> None:
        from replaylab_agent import tool_output
        receipt = {"workspacePath": "D:/project/old", "workspaceId": "a" * 32,
                   "existingSourceWorkspaces": [
                       {"workspacePath": "D:/project/organized", "workspaceId": "b" * 32,
                        "sourceSha256": "c" * 64}],
                   "projectReuse": "evidence" * 50000}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            tool_output, "_tool_output_root", return_value=Path(directory)
        ):
            result = tool_output.guard_tool_output_text("get_active_shader_workspace_path", json.dumps(receipt))
            pointer = json.loads(result)
            self.assertLessEqual(len(result.encode("utf-8")), tool_output.MAX_POINTER_BYTES)
            self.assertTrue(pointer["receiptIsPartial"])
            self.assertEqual(pointer["receipt"]["existingSourceWorkspaces"], receipt["existingSourceWorkspaces"])
            self.assertEqual(json.loads(Path(pointer["path"]).read_text()), receipt)

    def test_large_finish_result_retains_preview_visibility_and_scope(self) -> None:
        from replaylab_agent import tool_output
        preview = {"shown": True, "readOnly": True, "replayApplied": False,
                   "workspaceId": "a" * 64, "sourceWorkspaceId": "b" * 32,
                   "sourceSha256": "c" * 64}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            tool_output, "_tool_output_root", return_value=Path(directory)
        ):
            result = tool_output.guard_tool_output_text("finish_shader_workspace_organization", json.dumps({
                "preview": preview, "detail": "x" * 50000,
            }))
            pointer = json.loads(result)
            self.assertLessEqual(len(result.encode("utf-8")), tool_output.MAX_POINTER_BYTES)
            self.assertEqual(pointer["receipt"]["preview"], preview)

    def test_oversized_error_pointer_stays_visibly_failed(self) -> None:
        from replaylab_agent import deep_engine

        result = _Result(
            _Block(type="text", text="failure-detail-" * 5000),
            is_error=True,
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "replaylab_agent.tool_output._tool_output_root",
            return_value=Path(directory) / "replaylab-tool-output",
        ):
            pointer = json.loads(deep_engine._mcp_result_payload(
                result, "openai-chat", "compile_shader_workspace"
            ))
        self.assertTrue(pointer["isError"])
        self.assertTrue(pointer["doNotRetry"])
        self.assertIn("returned an oversized error", pointer["readInstructions"])
        self.assertLessEqual(pointer["readArguments"]["max_bytes"], 20 * 1024)


class HostPlannerSanitizerTests(unittest.TestCase):
    """Live Host results carry evidence and capabilities, never a plan."""

    @staticmethod
    def _host_payload() -> dict:
        return {
            "facts": {"catalogState": "ready", "count": 3},
            "error": {"code": "reader-missing", "message": "No proved reader"},
            "acceptance": {"contentHashCurrent": False},
            "unmetAcceptance": [{"code": "content-proof-missing"}],
            "requiredChecks": ["verify-content-sha256"],
            "availableCapabilities": ["search_unpacked_assets"],
            "requiredFirstStep": "host-required-first-token",
            "requiredAction": "host-required-action-token",
            "firstSteps": ["host-first-step-token"],
            "next": "host-next-token",
            "strategy": "host-strategy-token",
            "assetGate": {
                "facts": {"assetRootConfigured": True},
                "nextTool": "host-asset-next-tool",
            },
            "verdict": {
                "accepted": False,
                "direction": "host-direction-token",
                "next": [{"tool": "host-planner-list-tool"}],
                "allowedActions": [{
                    "action": "recover",
                    "tool": "poll_terminal_receipt",
                    "operation": "status",
                }],
                "reasons": [{
                    "code": "blocked",
                    "message": "Exact acceptance fact",
                    "direction": "host-reason-direction-token",
                }],
            },
            "flowGraph": {
                "nodes": [{"id": "a"}, {"id": "b"}],
                "edges": [{"id": "a", "next": ["b"]}],
            },
            "page": {"nextOffset": 64, "hasMore": True},
            "allowedActions": [{
                "action": "recover",
                "tool": "get_shader_workspace_mutation_status",
                "arguments": {"operation_id": "op-1"},
            }],
        }

    def test_recursive_sanitizer_removes_only_planner_fields(self) -> None:
        from replaylab_agent.tool_output import sanitize_host_planner_data

        clean = sanitize_host_planner_data(self._host_payload())
        encoded = json.dumps(clean, sort_keys=True)
        for token in (
            "host-required-first-token",
            "host-required-action-token",
            "host-first-step-token",
            "host-next-token",
            "host-strategy-token",
            "host-asset-next-tool",
            "host-direction-token",
            "host-reason-direction-token",
            "host-planner-list-tool",
        ):
            self.assertNotIn(token, encoded)

        self.assertEqual(clean["facts"]["catalogState"], "ready")
        self.assertEqual(clean["error"]["code"], "reader-missing")
        self.assertFalse(clean["acceptance"]["contentHashCurrent"])
        self.assertEqual(clean["unmetAcceptance"][0]["code"], "content-proof-missing")
        self.assertEqual(clean["requiredChecks"], ["verify-content-sha256"])
        self.assertEqual(clean["availableCapabilities"], ["search_unpacked_assets"])
        self.assertEqual(clean["verdict"]["reasons"][0]["message"],
                         "Exact acceptance fact")
        self.assertNotIn("allowedActions", clean["verdict"])
        # ``next`` is graph adjacency evidence here, and nextOffset is a cursor.
        self.assertEqual(clean["flowGraph"]["edges"][0]["next"], ["b"])
        self.assertEqual(clean["page"]["nextOffset"], 64)
        self.assertNotIn("allowedActions", clean)

    def test_live_mcp_result_sanitizes_text_and_structured_content(self) -> None:
        from replaylab_agent.tool_output import guard_mcp_call_result

        raw = json.dumps(self._host_payload(), separators=(",", ":"))
        result = _Result(_Block(type="text", text=raw), is_error=True)
        result.structuredContent = {"result": raw, "transportFact": "kept"}

        guarded = guard_mcp_call_result(result, "begin_shader_workspace_organization")
        text_payload = json.loads(guarded.content[0].text)
        structured_payload = json.loads(guarded.structuredContent["result"])
        for payload in (text_payload, structured_payload):
            self.assertNotIn("requiredFirstStep", payload)
            self.assertNotIn("requiredAction", payload)
            self.assertNotIn("firstSteps", payload)
            self.assertNotIn("next", payload)
            self.assertNotIn("allowedActions", payload)
            self.assertEqual(payload["facts"]["count"], 3)
            self.assertEqual(payload["flowGraph"]["edges"][0]["next"], ["b"])
            self.assertEqual(payload["page"]["nextOffset"], 64)
            self.assertEqual(payload["error"]["message"], "No proved reader")
        self.assertEqual(guarded.structuredContent["transportFact"], "kept")
        self.assertTrue(guarded.isError)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


def test_the_effective_protocol_follows_the_api_option() -> None:
    """`options.api` flips the surface the model is built on; the tool-result media
    has to follow, or a Responses image block lands in a text-only Chat tool message."""

    from replaylab_agent.deep_engine import _effective_protocol, _wants_native_input
    from replaylab_agent.profiles import Profile

    responses = Profile(id="o", preset="openai", model="gpt-4.1", api_key_env="K")
    assert _effective_protocol(responses) == "openai-responses"
    assert _wants_native_input(responses, "image_input", _effective_protocol(responses))

    flipped = Profile(id="o", preset="openai", model="gpt-4.1", api_key_env="K",
                      options={"api": "chat"})
    assert _effective_protocol(flipped) == "openai-chat"
    # ...and a pasted screenshot is not sent natively where it cannot arrive.
    assert not _wants_native_input(flipped, "image_input", _effective_protocol(flipped))

    # An explicit choice always wins over the connection default.
    off = Profile(id="a", preset="anthropic", model="m", api_key_env="K",
                  options={"image_input": False})
    assert not _wants_native_input(off, "image_input", _effective_protocol(off))
