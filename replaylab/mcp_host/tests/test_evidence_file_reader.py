"""Reading back what the Host wrote, in any format, without becoming a file reader.

`read_evidence_file` exists because a saved path is not evidence on a route that
carries tool results as text: the model gets a file name and, left to itself, will
describe a picture it never saw. These tests pin three things — the contents really
come back in the most specific shape that fits (text in pieces, a raster image as an
image, everything else as an embedded resource with its real media type), no format is
refused merely for being unfamiliar, and nothing outside the directories ReplayLab owns
or was pointed at can be read at all.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from src import server


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    """A real, minimal PNG, so the reader is exercised on a file and not a stub."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class EvidenceFileReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name).resolve()
        self._roots = mock.patch.object(server, "_evidence_roots",
                                        return_value=[self.root])
        self._roots.start()
        self.addCleanup(self._roots.stop)

    def _read(self, path: Path, **kwargs) -> list:
        return server.read_evidence_file(path=str(path), **kwargs)

    @staticmethod
    def _summary(blocks: list) -> dict:
        return json.loads(blocks[0])

    def test_text_comes_back_in_pieces_that_join_up(self) -> None:
        target = self.root / "notes.txt"
        target.write_text("0123456789abcdef", encoding="utf-8")

        head = self._read(target, max_bytes=10)
        summary = self._summary(head)
        self.assertEqual(summary["kind"], "text")
        self.assertTrue(summary["truncated"])
        self.assertEqual(head[1], "0123456789")

        tail = self._read(target, max_bytes=10, offset=summary["returnedBytes"])
        self.assertFalse(self._summary(tail)["truncated"])
        self.assertEqual(head[1] + tail[1], "0123456789abcdef")

    def test_text_search_locates_instructions_without_paging_entire_disassembly(self) -> None:
        target = self.root / "shader.ir.txt"
        lines = ["// 雨滴\n"] + ["%d: sample(texture0) // 中文\n" % i for i in range(35)]
        target.write_bytes("".join(lines).encode("utf-8"))
        found, offset = [], 0
        with mock.patch.object(server.ipc, "call", side_effect=AssertionError("search must not replay")):
            while True:
                summary = self._summary(self._read(target, text_query="sample", offset=offset))
                self.assertLessEqual(len(summary["matches"]), 16)
                found.extend(summary["matches"])
                offset = summary["continuation"]["nextOffset"]
                if not summary["truncated"]:
                    break
        self.assertEqual([match["line"] for match in found], list(range(2, 37)))
        for match in found:
            block = self._read(target, offset=match["offset"], max_bytes=60)
            self.assertTrue(block[1].startswith(lines[match["line"] - 1]))

    def test_search_literal_does_not_act_as_regex_and_respects_unicode_boundaries(self) -> None:
        target = self.root / "shader.txt"
        target.write_bytes("雨滴\nabc.*def\nabc123def\n".encode("utf-8"))
        result = self._summary(self._read(target, text_query=".*"))
        self.assertEqual([row["line"] for row in result["matches"]], [2])
        self.assertIn("inside a UTF-8", self._summary(self._read(target, text_query="abc", offset=1))["error"])

    def test_search_large_single_line_returns_precise_offsets_and_all_occurrences(self) -> None:
        target = self.root / "native-response.json"
        raw = ("{\"shader\":\"" + "雨" * 70000 + (" dx.op.sample 雨滴" * 35) + "\"}").encode("utf-8")
        target.write_bytes(raw)
        matches, cursor = [], 0
        while True:
            result = self._summary(self._read(target, text_query="dx.op.sample", offset=cursor))
            self.assertLess(len(json.dumps(result).encode("utf-8")), 24 * 1024)
            matches.extend(result["matches"])
            cursor = result["continuation"]["nextOffset"]
            if not result["truncated"]:
                break
        self.assertEqual(len(matches), 35)
        for match in matches:
            self.assertGreater(match["offset"], 200000)
            self.assertTrue(raw[match["matchOffset"]:].startswith(b"dx.op.sample"))
            self.assertIn("dx.op.sample", self._read(target, offset=match["offset"], max_bytes=512)[1])

    def test_shader_representation_receipt_is_readable_without_an_active_project(self) -> None:
        representation_root = self.root / "shader-representations"
        representation_root.mkdir()
        target = representation_root / "shader.ir.txt"
        target.write_text("native instructions", encoding="utf-8")
        self._roots.stop()
        try:
            with mock.patch.object(server, "_SHADER_REPRESENTATIONS_ROOT", representation_root), \
                 mock.patch.object(server, "_active_asset_catalog_context", side_effect=RuntimeError("no project")), \
                 mock.patch.object(server, "_active_project_service", side_effect=RuntimeError("no project")):
                blocks = self._read(target)
                self.assertEqual(blocks[1], "native instructions")
                with mock.patch.object(server, "_event_texture_evidence_redirected",
                                       side_effect=lambda path: Path(path) == representation_root):
                    self.assertNotIn(representation_root, server._evidence_roots())
        finally:
            self._roots.start()

    def test_an_image_comes_back_as_an_image_not_as_a_path(self) -> None:
        target = self.root / "rt.png"
        payload = _png_bytes()
        target.write_bytes(payload)

        blocks = self._read(target)
        self.assertEqual(self._summary(blocks)["kind"], "image")
        self.assertEqual(len(blocks), 2)
        content = blocks[1].to_image_content()
        self.assertEqual(content.mimeType, "image/png")
        self.assertEqual(base64.b64decode(content.data), payload)

    def test_utf8_text_chunks_preserve_names_at_byte_boundaries(self) -> None:
        target = self.root / "material.json"
        original = json.dumps({"parameter": "雨滴颜色🟦", "value": 0.5}, ensure_ascii=False)
        target.write_text(original, encoding="utf-8")
        offset = 0
        parts = []
        while True:
            blocks = self._read(target, max_bytes=5, offset=offset)
            summary = self._summary(blocks)
            self.assertGreater(summary["returnedBytes"], 0)
            self.assertLessEqual(summary["returnedBytes"], 5)
            self.assertEqual(summary["continuation"]["nextOffset"],
                             offset + summary["returnedBytes"])
            parts.append(blocks[1])
            offset = summary["continuation"]["nextOffset"]
            if not summary["truncated"]:
                break
        self.assertEqual("".join(parts), original)
        self.assertEqual(offset, len(original.encode("utf-8")))

    def test_default_and_max_text_pages_are_bounded_and_explicit(self) -> None:
        target = self.root / "large.txt"
        target.write_text("x" * 50000, encoding="utf-8")

        default = self._read(target)
        default_summary = self._summary(default)
        self.assertEqual(default_summary["requestedMaxBytes"], 16 * 1024)
        self.assertEqual(default_summary["effectiveMaxBytes"], 16 * 1024)
        self.assertLess(
            len(json.dumps(default, ensure_ascii=True, default=str).encode("utf-8")),
            24 * 1024,
        )

        maximum = self._read(target, max_bytes=20 * 1024)
        maximum_summary = self._summary(maximum)
        self.assertEqual(maximum_summary["requestedMaxBytes"], 20 * 1024)
        self.assertLessEqual(maximum_summary["effectiveMaxBytes"], 20 * 1024)
        self.assertLess(
            len(json.dumps(maximum, ensure_ascii=True, default=str).encode("utf-8")),
            24 * 1024,
        )

        rejected = self._summary(self._read(target, max_bytes=20 * 1024 + 1))
        self.assertIn("must not exceed 20480", rejected["error"])

    def test_utf8_pages_reconstruct_exact_bytes_and_each_fit_vendor_cap(self) -> None:
        target = self.root / "unicode.jsonl"
        payload = (("雨滴颜色🟦/Material_半透明\n" * 900)
                   + "tail-with-ascii").encode("utf-8")
        target.write_bytes(payload)

        offset = 0
        rebuilt = bytearray()
        digest = hashlib.sha256(payload).hexdigest()
        while True:
            blocks = self._read(target, max_bytes=20 * 1024, offset=offset)
            summary = self._summary(blocks)
            encoded = json.dumps(blocks, ensure_ascii=True, default=str).encode("utf-8")
            self.assertLess(len(encoded), 24 * 1024)
            self.assertEqual(summary["sha256"], digest)
            self.assertEqual(summary["offset"], offset)
            part = blocks[1].encode("utf-8")
            self.assertEqual(len(part), summary["returnedBytes"])
            rebuilt.extend(part)
            offset = summary["continuation"]["nextOffset"]
            if not summary["truncated"]:
                break

        self.assertEqual(bytes(rebuilt), payload)
        self.assertEqual(offset, len(payload))

    def test_offset_inside_utf8_character_is_rejected(self) -> None:
        target = self.root / "unicode.txt"
        target.write_text("雨滴", encoding="utf-8")
        summary = self._summary(self._read(target, offset=1))
        self.assertIn("inside a UTF-8 character", summary["error"])

    def test_oversized_host_result_spills_exact_json_and_is_recoverable(self) -> None:
        output_root = self.root / "replaylab-tool-output"
        payload = {
            "schema": "example-large-result/v1",
            "workspaceId": "a" * 32,
            "status": "done",
            "rows": [
                {"index": index, "name": "材质_雨滴_%d" % index, "value": "x" * 64}
                for index in range(900)
            ],
        }
        expected = server._ok(payload).encode("utf-8")
        with mock.patch.object(server, "_asset_tool_output_root",
                               return_value=output_root), \
             mock.patch.object(server, "MAX_AGENT_RESULT_BYTES", 4096):
            receipt_text = server._ok_compact(payload)
        self.assertLess(len(receipt_text.encode("utf-8")), 4096)
        receipt = json.loads(receipt_text)
        pointer = receipt["completeContent"]
        self.assertEqual(pointer["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertFalse(receipt["_compacted"]["lossy"])

        offset = 0
        restored = bytearray()
        while True:
            blocks = self._read(Path(pointer["path"]), max_bytes=20 * 1024,
                                offset=offset)
            summary = self._summary(blocks)
            self.assertLess(
                len(json.dumps(blocks, ensure_ascii=True, default=str).encode("utf-8")),
                24 * 1024,
            )
            restored.extend(blocks[1].encode("utf-8"))
            offset = summary["continuation"]["nextOffset"]
            if not summary["truncated"]:
                break
        self.assertEqual(bytes(restored), expected)
        self.assertEqual(json.loads(bytes(restored)), payload)

    def test_large_shader_begin_keeps_draft_identity_in_preview(self) -> None:
        payload = {
            "schema": "renderdoc-ai-shader-organization-begin/v1",
            "workspacePath": str(self.root / "shader"),
            "organizationId": "a" * 32,
            "state": "drafting",
            "sourceSha256": "b" * 64,
            "semanticContext": {"notes": "x" * 300_000},
            "otherLargeFields": {str(index): "y" * 1000 for index in range(120)},
        }
        with mock.patch.object(server, "_asset_tool_output_root",
                               return_value=self.root / "shader-tool-output"), \
             mock.patch.object(server, "MAX_AGENT_RESULT_BYTES", 4096):
            preview = json.loads(server._ok_compact(payload))
        self.assertEqual(preview["state"], "drafting")
        self.assertEqual(preview["sourceSha256"], "b" * 64)
        self.assertEqual(
            json.loads(Path(preview["completeContent"]["path"]).read_text(encoding="utf-8")),
            payload,
        )

    def test_result_spill_retention_is_file_count_bounded(self) -> None:
        output_root = self.root / "replaylab-tool-output"
        with mock.patch.object(server, "_asset_tool_output_root",
                               return_value=output_root), \
             mock.patch.object(server, "MAX_AGENT_RESULT_BYTES", 100), \
             mock.patch.object(server, "_RESULT_SPILL_MAX_FILES", 2):
            for index in range(3):
                receipt = json.loads(server._ok_compact({
                    "operationId": "%032x" % index,
                    "payload": ("row-%d-" % index) * 100,
                }))
                self.assertTrue(Path(receipt["completeContent"]["path"]).is_file())
        spill_root = output_root / server._RESULT_SPILL_PREFIX
        self.assertLessEqual(len(list(spill_root.glob("*.json"))), 2)

    def test_result_spill_boundary_is_24k_utf8_bytes_not_characters(self) -> None:
        output_root = self.root / "replaylab-tool-output"
        cap = 24 * 1024
        overhead = len(server._ok({"value": ""}).encode("utf-8"))
        inline_payload = {"value": "x" * (cap - overhead)}
        overflow_payload = {"value": "x" * (cap - overhead + 1)}
        self.assertEqual(len(server._ok(inline_payload).encode("utf-8")), cap)
        self.assertEqual(len(server._ok(overflow_payload).encode("utf-8")), cap + 1)
        with mock.patch.object(server, "_asset_tool_output_root",
                               return_value=output_root):
            self.assertEqual(server._ok_compact(inline_payload),
                             server._ok(inline_payload))
            overflow = json.loads(server._ok_compact(overflow_payload))
            unicode_spill = json.loads(server._ok_compact({"value": "雨" * 5000}))
        self.assertEqual(overflow["_compacted"]["originalByteSize"], cap + 1)
        self.assertIn("completeContent", unicode_spill)
        self.assertLess(len(json.dumps(unicode_spill).encode("utf-8")), cap)

    def test_tiny_utf8_budget_is_actionable_instead_of_stalling(self) -> None:
        target = self.root / "material.txt"
        target.write_text("雨滴", encoding="utf-8")
        summary = self._summary(self._read(target, max_bytes=1))
        self.assertIn("use at least 4", summary["error"])

    def test_an_oversized_file_says_what_to_do_instead_of_raising_the_budget(self) -> None:
        target = self.root / "huge.png"
        target.write_bytes(_png_bytes(64, 64))

        blocks = self._read(target, max_bytes=8)
        summary = self._summary(blocks)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(summary["kind"], "image")
        self.assertIn("over max_bytes", summary["inlineContent"])
        # A smaller version is offered as an option, not imposed as the only one.
        self.assertIn("Raise max_bytes", summary["inlineContent"])

    def test_the_contents_can_be_left_out_when_only_the_facts_are_wanted(self) -> None:
        target = self.root / "rt.png"
        target.write_bytes(_png_bytes())

        blocks = self._read(target, inline=False)
        self.assertEqual(len(blocks), 1)
        summary = self._summary(blocks)
        self.assertEqual(summary["kind"], "image")
        self.assertIn("not requested", summary["inlineContent"])

    def test_an_unfamiliar_format_is_carried_as_a_resource_not_refused(self) -> None:
        """The point of not having a whitelist: a .pak is still handed over."""

        target = self.root / "mesh.pak"
        target.write_bytes(b"\x00\x01\x02\x03")

        blocks = self._read(target)
        summary = self._summary(blocks)
        self.assertEqual(summary["kind"], "binary")
        self.assertEqual(summary["byteSize"], 4)
        self.assertIn("sha256", summary)
        self.assertEqual(len(blocks), 2)
        resource = blocks[1].resource
        self.assertEqual(base64.b64decode(resource.blob), b"\x00\x01\x02\x03")
        self.assertEqual(resource.mimeType, "application/octet-stream")

    def test_a_pdf_comes_back_as_a_document_with_its_real_media_type(self) -> None:
        target = self.root / "spec.pdf"
        target.write_bytes(b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n")

        blocks = self._read(target)
        summary = self._summary(blocks)
        self.assertEqual(summary["mediaType"], "application/pdf")
        self.assertEqual(summary["kind"], "binary")
        self.assertEqual(blocks[1].resource.mimeType, "application/pdf")

    def test_the_bytes_decide_the_type_not_the_name(self) -> None:
        """A PNG called .dat is a PNG; a shader with no known suffix is still text."""

        mislabelled = self.root / "dump.dat"
        mislabelled.write_bytes(_png_bytes())
        self.assertEqual(self._summary(self._read(mislabelled))["mediaType"], "image/png")

        source = self.root / "lighting.usf"
        source.write_text("float3 N = normalize(vIn.Normal);\n", encoding="utf-8")
        self.assertEqual(self._summary(self._read(source))["kind"], "text")

        chinese = self.root / "notes.md"
        chinese.write_text("丝袜渲染，第二次高光\n", encoding="utf-8")
        blocks = self._read(chinese)
        self.assertEqual(self._summary(blocks)["kind"], "text")
        self.assertIn("丝袜", blocks[1])

    def test_a_file_outside_the_host_s_own_output_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            outsider = Path(elsewhere) / "secrets.txt"
            outsider.write_text("not ours", encoding="utf-8")
            blocks = self._read(outsider)
        self.assertEqual(len(blocks), 1)
        self.assertIn("outside the Host's evidence directories",
                      json.loads(blocks[0])["error"])

    def test_a_relative_path_and_a_directory_are_both_refused(self) -> None:
        self.assertIn("absolute", json.loads(
            server.read_evidence_file(path="notes.txt")[0])["error"])
        self.assertIn("not a regular file", json.loads(
            server.read_evidence_file(path=str(self.root))[0])["error"])

    def test_the_roots_are_what_replaylab_owns_or_was_pointed_at(self) -> None:
        # The project's Shader workspace root is read from the project status and has
        # to exist to be offered, the same as every other root here.
        (self.root / "ws").mkdir(parents=True, exist_ok=True)
        self._roots.stop()
        try:
            with mock.patch.object(server, "_event_texture_evidence_root",
                                   return_value=str(self.root / "evidence")), \
                 mock.patch.object(server, "_active_asset_catalog_context",
                                   return_value=(None, None, str(self.root / "unpacked"),
                                                 str(self.root / "project" / "catalog.db"))), \
                 mock.patch.object(
                     server, "_active_project_service",
                     return_value=type("S", (), {"status": staticmethod(
                         lambda: {"paths": {"shaderWorkspaces": str(self.root / "ws")}})})()):
                roots = {path.name for path in server._evidence_roots()}
        finally:
            self._roots.start()
        self.assertIn("evidence", roots)
        self.assertIn("ws", roots)
        self.assertIn("replaylab-texture-images", roots)
        self.assertIn("replaylab-tool-output", roots)
        self.assertIn("project", roots)
        # The user chose the unpacked-asset root for this project; a texture there is
        # evidence in exactly the way a render target is.
        self.assertIn("unpacked", roots)

    def test_a_missing_project_simply_offers_fewer_roots(self) -> None:
        self._roots.stop()
        try:
            with mock.patch.object(server, "_event_texture_evidence_root",
                                   side_effect=RuntimeError("no capture")), \
                 mock.patch.object(server, "_SHADER_REPRESENTATIONS_ROOT", self.root / "absent-representations"), \
                 mock.patch.object(server, "_active_asset_catalog_context",
                                   side_effect=RuntimeError("no project")), \
                 mock.patch.object(server, "get_active_shader_workspace_path",
                                   return_value="{}"):
                roots = [path.name for path in server._evidence_roots()]
        finally:
            self._roots.start()
        self.assertEqual(roots, ["replaylab-texture-images", "replaylab-tool-output"])

    def test_registered_tool_stdout_can_be_read_past_its_preview(self) -> None:
        output_root = self.root / "replaylab-tool-output"
        run = output_root / "run-material"
        run.mkdir(parents=True)
        target = run / "stdout.txt"
        document = json.dumps({"parameters": ["Trail Size"] * 8000})
        target.write_text(document, encoding="utf-8")
        outsider = self.root / "unrelated.json"
        outsider.write_text("{}", encoding="utf-8")
        self._roots.stop()
        try:
            with mock.patch.object(server.tempfile, "gettempdir", return_value=str(self.root)), \
                 mock.patch.object(server, "_event_texture_evidence_root", side_effect=RuntimeError("no capture")), \
                 mock.patch.object(server, "_active_asset_catalog_context", side_effect=RuntimeError("no project")), \
                 mock.patch.object(server, "_active_project_service", side_effect=RuntimeError("no project")):
                pages = []
                offset = 0
                while True:
                    page = self._read(target, offset=offset, max_bytes=20 * 1024)
                    pages.append(page)
                    receipt = self._summary(page)
                    offset = receipt["continuation"]["nextOffset"]
                    if not receipt["truncated"]:
                        break
                denied = self._summary(self._read(outsider))
        finally:
            self._roots.start()
        self.assertGreater(len(pages), 2)
        self.assertFalse(self._summary(pages[-1])["truncated"])
        self.assertEqual("".join(page[1] for page in pages), document)
        self.assertTrue(all(self._summary(page)["sha256"] == receipt["sha256"]
                            for page in pages))
        self.assertIn("outside the Host's evidence directories", denied["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class EvidenceRootsTests(unittest.TestCase):
    def test_shader_debug_tail_is_readable_by_owned_artifact_offset_without_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "ReplayLab" / "shader-debug-traces"
            root.mkdir(parents=True)
            target = root / "trace.jsonl"
            header = b'{"record":"header"}\n'
            tail = b'{"record":"step","step":3482,"value":[0.0,0.9573]}\n'
            target.write_bytes(header + tail)
            with mock.patch.dict(server.os.environ, {"LOCALAPPDATA": temporary}), \
                 mock.patch.object(server, "_event_texture_evidence_root", side_effect=RuntimeError("none")), \
                 mock.patch.object(server, "_active_asset_catalog_context", side_effect=RuntimeError("none")), \
                 mock.patch.object(server, "_active_project_service", side_effect=RuntimeError("none")), \
                 mock.patch.object(server, "_call", side_effect=AssertionError("No replay for trace paging")):
                self.assertIn(root.resolve(), server._evidence_roots())
                response = server.read_evidence_file(str(target), offset=len(header), max_bytes=4096)
                self.assertEqual(response[1], tail.decode("utf-8"))
                self.assertEqual(json.loads(response[0])["kind"], "text")
                unrelated = Path(temporary) / "not-evidence.txt"
                unrelated.write_text("private")
                self.assertIn("outside", json.loads(server.read_evidence_file(str(unrelated))[0])["error"])

    """The roots the reader offers must be the ones the docstring promises.

    Both project roots were resolved by calls that raised TypeError on every
    invocation -- a keyword-only argument omitted, and a tool called without its
    event id -- and a bare `except Exception` turned each into "no project". The
    whole shared-Shader-library flow depends on an organized source being readable,
    so it could never be published or reused.
    """

    def test_the_active_project_and_its_workspace_root_are_offered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "project" / "assets" / "catalog.sqlite3"
            assets = root / "unpacked"
            workspaces = root / "project" / "workspaces" / "shaders"
            for path in (database.parent, assets, workspaces):
                path.mkdir(parents=True, exist_ok=True)
            status = {"paths": {"shaderWorkspaces": str(workspaces)}}

            with mock.patch.object(
                server, "_active_asset_catalog_context",
                return_value=(object(), status, str(assets), str(database)),
            ) as context, mock.patch.object(
                server, "_active_project_service",
                return_value=type("S", (), {"status": staticmethod(lambda: status)})(),
            ):
                roots = [Path(item) for item in server._evidence_roots()]

            # Called the way the signature actually requires, not positionally.
            context.assert_called_once_with(require_catalog=False)
            resolved = {path.resolve() for path in roots}
            self.assertIn(database.parent.resolve(), resolved)
            self.assertIn(assets.resolve(), resolved)
            self.assertIn(workspaces.resolve(), resolved)

    def test_without_a_project_the_host_owned_roots_still_work(self) -> None:
        with mock.patch.object(
            server, "_active_asset_catalog_context", side_effect=RuntimeError("no project")
        ), mock.patch.object(
            server, "_active_project_service", side_effect=RuntimeError("no project")
        ):
            self.assertTrue(server._evidence_roots())


class DrawIntentPathTests(unittest.TestCase):
    def test_intents_live_beside_the_project_database(self) -> None:
        """Otherwise every project shares one file and answers with another game's intents."""

        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "assets" / "catalog.sqlite3"
            database.parent.mkdir(parents=True)
            with mock.patch.object(
                server, "_active_asset_catalog_context",
                return_value=(object(), {}, "", str(database)),
            ) as context:
                path = server._draw_intents_path()
            context.assert_called_once_with(require_catalog=False)
            self.assertEqual(path.parent, database.parent)

    def test_without_a_project_it_falls_back_to_the_user_file(self) -> None:
        from src.draw_intent import default_intents_path

        with mock.patch.object(
            server, "_active_asset_catalog_context", side_effect=RuntimeError("no project")
        ):
            self.assertEqual(server._draw_intents_path(), default_intents_path())

    def test_a_project_without_an_asset_root_still_keeps_its_own_intents(self) -> None:
        """Otherwise the file silently flips between the project and the shared
        user-level one as the asset root is configured, unplugged, or removed."""

        with tempfile.TemporaryDirectory() as temporary:
            shared = Path(temporary) / "project" / "shared" / "dictionaries"
            shared.mkdir(parents=True)
            status = {"projectRoot": str(Path(temporary) / "project"),
                      "paths": {"sharedDictionaries": str(shared)}}
            with mock.patch.object(
                server, "_active_asset_catalog_context",
                side_effect=RuntimeError("active project has no unpacked asset root"),
            ), mock.patch.object(
                server, "_active_project_service",
                return_value=type("S", (), {"status": staticmethod(lambda: status)})(),
            ):
                path = server._draw_intents_path()
            self.assertEqual(path.parent, (shared / "assets").resolve())
