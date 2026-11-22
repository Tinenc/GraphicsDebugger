"""CBuffer alias store shared with the qrenderdoc panel (keyed by block layout)."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src.shader_workspace import aliases

SOURCE = """cbuffer cb0 : register(b0) {
  float4 v0;            // 0
  float3 v1;            // 16
  float v2;             // 28  (fits behind the float3)
  float2 v3;            // 32
  float4x4 m0;          // 48
  row_major float3x2 m1;// 112 (3 rows)
  float4 arr[3];        // 160
  uint flags : packoffset(c20.y);   // 324
  SomeStruct s;         // unknown from here on
  float after;          // -1
};
cbuffer Lights : register(b2, space1) { int count; float3 dir; };
"""
LIGHTS = [{"name": "count", "offset": 0}, {"name": "dir", "offset": 4}]


if os.name == "nt":
    class _WindowsOverlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", ctypes.c_uint32),
            ("OffsetHigh", ctypes.c_uint32),
            ("hEvent", ctypes.c_void_p),
        ]


@contextmanager
def _cpp_compatible_alias_lock(root: Path):
    """Hold the native lock used by ReplayLabCBufferAliases.cpp."""

    root.mkdir(parents=True, exist_ok=True)
    stream = (root / ".alias-publication.lock").open("a+b")
    if stream.seek(0, os.SEEK_END) == 0:
        stream.write(b"\0")
        stream.flush()
        os.fsync(stream.fileno())
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.POINTER(_WindowsOverlapped)
        kernel32.LockFileEx.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, pointer,
        ]
        kernel32.LockFileEx.restype = ctypes.c_int
        kernel32.UnlockFileEx.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, pointer,
        ]
        kernel32.UnlockFileEx.restype = ctypes.c_int
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
        position = _WindowsOverlapped()
        if not kernel32.LockFileEx(handle, 3, 0, 1, 0, ctypes.byref(position)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            position = _WindowsOverlapped()
            if not kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(position)):
                raise ctypes.WinError(ctypes.get_last_error())
            stream.close()
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()


class ParseTests(unittest.TestCase):
    def test_typed_constant_buffer_array_uses_struct_layout_and_qualified_rename(self) -> None:
        source = (
            "struct MaterialData { float4 _2; float _3; };\n"
            "ConstantBuffer<MaterialData> materials[2] : register(b4, space1);"
        )
        members = aliases.cbuffer_members(source)
        self.assertEqual([(item["block"], item["bind"], item["space"], item["name"], item["offset"])
                          for item in members],
                         [("materials", 4, 1, "_2", 0), ("materials", 4, 1, "_3", 16)])
        (publication,) = aliases.aliases_from_renames(
            source, {"materials._2": "surfaceTint", "materials._3": "roughness"})
        self.assertEqual([(item["path"], item["alias"]) for item in publication["entries"]],
                         [("_2", "surfaceTint"), ("_3", "roughness")])

    def test_offsets_follow_hlsl_packing_and_packoffset(self) -> None:
        members = aliases.cbuffer_members(SOURCE)
        by_name = {m["name"]: m for m in members}
        self.assertEqual([m["name"] for m in members], ["v0", "v1", "v2", "v3", "m0", "m1", "arr", "flags", "s", "after", "count", "dir"])
        self.assertEqual(by_name["v0"]["offset"], 0)
        self.assertEqual(by_name["v1"]["offset"], 16)
        self.assertEqual(by_name["v2"]["offset"], 28)
        self.assertEqual(by_name["v3"]["offset"], 32)
        self.assertEqual(by_name["m0"]["offset"], 48)
        self.assertEqual(by_name["m1"]["offset"], 112)
        self.assertEqual(by_name["arr"]["offset"], 160)
        self.assertFalse(by_name["arr"]["leaf"])
        self.assertEqual(by_name["flags"]["offset"], 324)
        self.assertEqual(by_name["s"]["offset"], -1)
        self.assertEqual(by_name["after"]["offset"], -1)
        self.assertEqual((by_name["v0"]["block"], by_name["v0"]["bind"], by_name["v0"]["space"]), ("cb0", 0, 0))
        self.assertEqual((by_name["dir"]["block"], by_name["dir"]["bind"], by_name["dir"]["space"], by_name["dir"]["offset"]), ("Lights", 2, 1, 4))

    def test_layout_signature_matches_the_panel_contract(self) -> None:
        # ReplayLabCBufferAliases::LayoutSignature builds exactly this text.
        self.assertEqual(aliases.layout_signature("Lights", LIGHTS), "Lights\ncount@0\ndir@4")
        self.assertEqual(aliases.layout_sha256("Lights", LIGHTS), hashlib.sha256(b"Lights\ncount@0\ndir@4").hexdigest())
        self.assertEqual(aliases.layout_signature("cb0", [{"name": "s", "offset": -1}]), "")
        self.assertNotEqual(aliases.layout_sha256("A", LIGHTS), aliases.layout_sha256("B", LIGHTS))

    def test_publications_from_rename_map_cover_only_complete_cbuffer_layouts(self) -> None:
        pubs = aliases.aliases_from_renames(SOURCE, {"v0": "tint", "cb0": "MaterialCB", "arr": "bones", "dir": "sunDir", "ghost": "x", "count": "count"})
        self.assertEqual([p["block"] for p in pubs], ["cb0", "Lights"])
        cb0, lights = pubs
        self.assertEqual(cb0["layoutSha256"], "")  # struct member -> layout incomplete
        self.assertEqual([(e["path"], e["alias"], e["offset"]) for e in cb0["entries"]], [("v0", "tint", 0), ("arr", "bones", -1)])
        self.assertEqual(cb0["blockAlias"], "MaterialCB")
        self.assertEqual(lights["layoutSha256"], aliases.layout_sha256("Lights", LIGHTS))
        self.assertEqual(lights["entries"], [{"path": "dir", "offset": 4, "alias": "sunDir", "byteSize": 12, "original": "dir"}])
        self.assertIsNone(lights["blockAlias"])

    def test_alias_validation(self) -> None:
        self.assertTrue(aliases.is_valid_alias("基础色 (albedo)"))
        for bad in ("", "  ", "a\nb", "x" * 65, " lead"):
            self.assertFalse(aliases.is_valid_alias(bad), repr(bad))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"REPLAYLAB_ALIAS_ROOT": self.tmp.name})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_ai_publish_never_overwrites_user_entries(self) -> None:
        user = aliases.publish_aliases(
            "Lights",
            LIGHTS,
            [{"path": "count", "offset": 0, "alias": "lightCount"}],
            source="user",
            block_alias="Lighting Constants",
        )
        self.assertEqual(user["written"], ["Lights.count"])
        self.assertTrue(user["blockAliasWritten"])
        ai = aliases.publish_aliases("Lights", LIGHTS, [
            {"path": "count", "alias": "aiCount"},
            {"path": "dir", "offset": 4, "alias": "sunDir"},
            {"path": "ghost", "alias": ""},
        ], block_alias="AI Lighting")
        self.assertEqual(ai["keptUser"], ["Lights.count"])
        self.assertTrue(ai["blockAliasKeptUser"])
        self.assertFalse(ai["blockAliasWritten"])
        self.assertEqual(ai["written"], ["Lights.dir"])
        self.assertEqual(ai["rejected"][0]["path"], "Lights.ghost")
        key = aliases.layout_sha256("Lights", LIGHTS)
        stored = aliases.read_aliases(key)
        self.assertEqual({e["path"]: (e["alias"], e["source"]) for e in stored}, {"count": ("lightCount", "user"), "dir": ("sunDir", "ai")})
        payload = json.loads(Path(ai["path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], aliases.SCHEMA)
        self.assertEqual((payload["layoutSha256"], payload["block"], payload["members"]), (key, "Lights", LIGHTS))
        self.assertEqual(
            aliases.read_block_alias(key),
            {"alias": "Lighting Constants", "source": "user", "updatedAt": payload["blockAlias"]["updatedAt"]},
        )
        self.assertEqual(Path(ai["path"]).name, key + ".json")
        # a later user rename replaces the AI entry
        again = aliases.publish_aliases("Lights", LIGHTS, [{"path": "dir", "alias": "sunDirection"}], source="user")
        self.assertEqual(again["written"], ["Lights.dir"])
        self.assertEqual([e["alias"] for e in aliases.read_aliases(key) if e["path"] == "dir"], ["sunDirection"])
        # the same layout in another shader / capture sees the same aliases
        evidence = aliases.evidence_for_source("cbuffer Lights : register(b7) { int count; float3 dir; };")
        self.assertEqual(evidence["blocks"][0]["user"], [{"path": "count", "alias": "lightCount"}, {"path": "dir", "alias": "sunDirection"}])
        self.assertEqual(evidence["blocks"][0]["ai"], [])
        self.assertEqual(evidence["blocks"][0]["blockAlias"]["alias"], "Lighting Constants")

    def test_cpp_native_lock_and_host_publish_preserve_concurrent_user_write(self) -> None:
        root = Path(self.tmp.name).resolve()
        key = aliases.layout_sha256("Lights", LIGHTS)
        path = aliases.layout_alias_path(key, root)

        # This first assertion is the protocol check: C++ holds LockFileEx/flock,
        # while the Python Host attempts msvcrt.locking/flock on its own handle.
        with _cpp_compatible_alias_lock(root):
            with mock.patch.object(aliases, "_ALIAS_LOCK_RETRY_SECONDS", 0.04):
                with self.assertRaisesRegex(RuntimeError, "owns the store lock"):
                    with aliases.alias_store_transaction(root):
                        self.fail("the Host entered a C++-owned alias transaction")

        started = threading.Event()
        finished = threading.Event()
        result: list[dict[str, object]] = []
        failures: list[BaseException] = []

        def host_publish() -> None:
            started.set()
            try:
                result.append(aliases.publish_aliases(
                    "Lights", LIGHTS,
                    [{"path": "count", "offset": 0, "alias": "aiCount"},
                     {"path": "dir", "offset": 4, "alias": "sunDirection"}],
                    source="ai", root=root,
                ))
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=host_publish, name="host-alias-publish")
        with _cpp_compatible_alias_lock(root):
            worker.start()
            self.assertTrue(started.wait(1.0))
            self.assertFalse(finished.wait(0.08))
            path.write_text(json.dumps({
                "schema": aliases.SCHEMA,
                "layoutSha256": key,
                "block": "Lights",
                "members": LIGHTS,
                "aliases": [{
                    "path": "count", "offset": 0, "alias": "userCount",
                    "source": "user", "updatedAt": "2026-09-20T00:00:00Z",
                }],
            }), encoding="utf-8")
        worker.join(2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(result[0]["keptUser"], ["Lights.count"])
        self.assertEqual(
            {item["path"]: (item["alias"], item["source"])
             for item in aliases.read_aliases(key)},
            {"count": ("userCount", "user"), "dir": ("sunDirection", "ai")},
        )

    def test_incomplete_layout_and_corrupt_store_are_reported_not_hidden(self) -> None:
        with self.assertRaises(ValueError):
            aliases.publish_aliases("cb0", [{"name": "s", "offset": -1}], [{"path": "s", "alias": "x"}])
        key = aliases.layout_sha256("Lights", LIGHTS)
        path = aliases.layout_alias_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            aliases.publish_aliases("Lights", LIGHTS, [{"path": "count", "alias": "x"}])
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")
        evidence = aliases.evidence_for_source(SOURCE)
        self.assertIn("layout incomplete", evidence["blocks"][0]["error"])
        self.assertIn("JSON", evidence["blocks"][1]["error"])

    def test_captured_binding_retargets_generated_names_to_original_panel_key(self) -> None:
        source = "cbuffer _41_43 : register(b0, space2) { float generated : packoffset(c1.y); };"
        members = [{"name": "original", "offset": 20}]
        identity = {"blocks": [{"block": "cbuffer0", "bind": 0, "space": 2,
                                "members": members, "memberMappingAvailable": True,
                                "leaves": [{"path": "original", "offset": 20, "byteSize": 4}]}]}
        user = aliases.publish_aliases("cbuffer0", members,
                                      [{"path": "original", "offset": 20, "alias": "userValue"}],
                                      source="user", block_alias="User Constants")
        publications = aliases.aliases_from_renames(
            source, {"_41_43": "MaterialCB", "generated": "inferredValue"}, identity)
        self.assertEqual(len(publications), 1)
        publication = publications[0]
        self.assertEqual(publication["block"], "cbuffer0")
        self.assertEqual(publication["layoutSha256"], user["layoutSha256"])
        self.assertEqual(publication["entries"], [{"path": "original", "offset": 20, "alias": "inferredValue", "byteSize": 4, "original": "generated"}])
        self.assertTrue(publication["capturedIdentityMatched"])
        result = aliases.publish_aliases(publication["block"], publication["members"],
                                        publication["entries"], block_alias=publication["blockAlias"])
        self.assertEqual(result["keptUser"], ["cbuffer0.original"])
        self.assertTrue(result["blockAliasKeptUser"])
        evidence = aliases.evidence_for_source(source, identity)["blocks"][0]
        self.assertEqual(evidence["user"], [{"path": "generated", "alias": "userValue"}])
        self.assertEqual(evidence["blockAlias"]["alias"], "User Constants")
        self.assertNotEqual(user["layoutSha256"], aliases.layout_sha256("_41_43", [{"name": "generated", "offset": 20}]))

    def test_opaque_or_different_width_fields_only_publish_captured_block_alias(self) -> None:
        source = "cbuffer generatedCB : register(b0) { float scalar : packoffset(c0); };"
        target = {"block": "cbuffer0", "bind": 0, "space": 0,
                  "members": [{"name": "unknown", "offset": 0}],
                  "memberMappingAvailable": False, "leaves": []}
        for complete, leaves in ((False, []), (True, [{"path": "vector", "offset": 0, "byteSize": 16}])):
            target.update(memberMappingAvailable=complete, leaves=leaves)
            result = aliases.aliases_from_renames(source, {"generatedCB": "Material", "scalar": "red"}, {"blocks": [target]})[0]
            self.assertEqual(result["entries"], [])
            self.assertEqual(result["unmapped"], ["scalar"])
            self.assertEqual(result["blockAlias"], "Material")
            self.assertEqual(result["layoutSha256"], aliases.layout_sha256("cbuffer0", target["members"]))

    def test_missing_or_ambiguous_capture_slot_cannot_publish_generated_layout(self) -> None:
        source = "cbuffer generatedCB : register(b0) { float scalar; };"
        target = {"block": "cbuffer0", "bind": 0, "space": 0,
                  "members": [], "leaves": [], "memberMappingAvailable": False}
        for blocks in ([], [target, target]):
            result = aliases.aliases_from_renames(source, {"generatedCB": "Material", "scalar": "red"}, {"blocks": blocks})[0]
            self.assertEqual(result["layoutSha256"], "")
            self.assertFalse(result["capturedIdentityMatched"])
            self.assertEqual(result["entries"], [])


if __name__ == "__main__":
    unittest.main()


class StrippedDxilPlaceholderTests(unittest.TestCase):
    """Physical placeholder rows must not spread material semantics across shaders."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"REPLAYLAB_ALIAS_ROOT": self.tmp.name,
                                              "REPLAYLAB_ALIAS_PROJECT_ROOT": self.tmp.name})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_shader_bound_renames_do_not_publish_to_unscoped_placeholder_rows(self) -> None:
        source = ("cbuffer cbuffer1 : register(b1) { float4 v0 : packoffset(c0); "
                  "float4 v1 : packoffset(c1); float4 v2 : packoffset(c2); float4 v3 : packoffset(c3); };")
        identity = {"blocks": [{
            "block": "cbuffer1", "bind": 1, "space": 0,
            "members": [{"name": "unknown", "offset": 0}],
            "memberMappingAvailable": True, "memberMappingKind": "placeholder",
            "leaves": [{"path": "unknown[%d]" % i, "offset": 16 * i, "byteSize": 16} for i in range(4)],
        }]}
        (publication,) = aliases.aliases_from_renames(
            source, {"cbuffer1": "MaterialCB", "v0": "tintAndAlpha", "v3": "sunDirection"}, identity)
        self.assertTrue(publication["capturedIdentityMatched"])
        self.assertEqual(publication["unmapped"], [])
        self.assertEqual([(e["path"], e["alias"], e["offset"]) for e in publication["entries"]],
                         [("unknown[0]", "tintAndAlpha", 0), ("unknown[3]", "sunDirection", 48)])
        # The layout key is the panel's: block name plus the reflected top-level
        # variables, however few of them there are.
        self.assertEqual(publication["layoutSha256"],
                         aliases.layout_sha256("cbuffer1", [{"name": "unknown", "offset": 0}]))
        report = aliases.publish_aliases(publication["block"], publication["members"],
                                         publication["entries"], source="ai",
                                         block_alias=publication["blockAlias"])
        self.assertEqual(report["written"], [])
        self.assertFalse(report["applied"])
        self.assertFalse(report["blockAliasWritten"])
        self.assertEqual(report["reasonId"], "unscoped-placeholder-layout")
        self.assertEqual(len(report["rejected"]), 3)
        self.assertTrue(all(item["reasonId"] == report["reasonId"] for item in report["rejected"]))
        self.assertFalse(Path(report["path"]).exists())
        stored = aliases.read_aliases(publication["layoutSha256"])
        self.assertEqual(stored, [])

    def test_scoped_placeholder_ai_names_are_visible_only_to_the_same_shader(self) -> None:
        members = [{"name": "unknown", "offset": 0}]
        key = aliases.layout_sha256("cbuffer5", members)
        scope_a = {
            "shaderRawSha256": "a" * 64,
            "shaderResourceId": "1000000000000000723",
            "stage": "fragment",
            "bind": 5,
            "space": 0,
            "layoutSha256": key,
        }
        scope_b = dict(scope_a, shaderRawSha256="b" * 64)
        report = aliases.publish_aliases(
            "cbuffer5",
            members,
            [{"path": "unknown[2]", "offset": 32, "alias": "roughnessMetallic"}],
            source="ai",
            block_alias="Material Parameters",
            shader_scope=scope_a,
        )
        self.assertTrue(report["applied"])
        self.assertEqual(report["shaderScope"], scope_a)
        self.assertEqual(aliases.read_aliases(key), [])
        self.assertIsNone(aliases.read_block_alias(key))
        self.assertEqual(
            [item["alias"] for item in aliases.read_aliases(key, shader_scope=scope_a)],
            ["roughnessMetallic"],
        )
        self.assertEqual(
            aliases.read_block_alias(key, shader_scope=scope_a)["alias"],
            "Material Parameters",
        )
        self.assertEqual(aliases.read_aliases(key, shader_scope=scope_b), [])
        self.assertIsNone(aliases.read_block_alias(key, shader_scope=scope_b))

        # A layout-wide user name has priority without deleting the scoped AI
        # history. Removing the user record later can reveal the exact scoped AI
        # name again.
        aliases.publish_aliases(
            "cbuffer5", members,
            [{"path": "unknown[2]", "offset": 32, "alias": "Manual Name"}],
            source="user",
        )
        visible = aliases.read_aliases(key, shader_scope=scope_a)
        self.assertEqual([(item["alias"], item["source"]) for item in visible],
                         [("Manual Name", "user")])
        payload = json.loads(aliases.layout_alias_path(key).read_text(encoding="utf-8"))
        self.assertEqual(
            sorted((item["alias"], item["source"]) for item in payload["aliases"]),
            [("Manual Name", "user"), ("roughnessMetallic", "ai")],
        )

    def test_placeholder_scope_rejects_wrong_layout_or_incomplete_identity(self) -> None:
        members = [{"name": "unknown", "offset": 0}]
        key = aliases.layout_sha256("cbuffer5", members)
        base = {
            "shaderRawSha256": "a" * 64,
            "stage": "fragment",
            "bind": 5,
            "space": 0,
            "layoutSha256": key,
        }
        for invalid in (
            dict(base, shaderRawSha256="short"),
            dict(base, shaderResourceId=77),
            dict(base, shaderResourceId="0"),
            dict(base, shaderResourceId="18446744073709551616"),
            dict(base, stage="not-a-stage"),
            dict(base, bind=-1),
            dict(base, layoutSha256="b" * 64),
        ):
            with self.assertRaises(ValueError):
                aliases.publish_aliases(
                    "cbuffer5", members,
                    [{"path": "unknown[0]", "offset": 0, "alias": "value"}],
                    shader_scope=invalid,
                )

    def test_legacy_placeholder_ai_rows_and_block_alias_are_hidden_without_rewriting(self) -> None:
        members = [{"name": "unknown", "offset": 0}] * 13
        key = aliases.layout_sha256("cbuffer5", members)
        path = aliases.layout_alias_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": aliases.SCHEMA, "block": "cbuffer5", "members": members,
                   "layoutSha256": key,
                   "aliases": [{"path": "unknown[1]", "offset": 16, "alias": "OtherMaterialTint", "source": "ai"},
                               {"path": "unknown[2]", "offset": 32, "alias": "User Choice", "source": "user"}],
                   "blockAlias": {"alias": "Other Material", "source": "ai"}}
        path.write_text(json.dumps(payload), encoding="utf-8")
        before = path.read_bytes()
        self.assertEqual([item["alias"] for item in aliases.read_aliases(key)], ["User Choice"])
        self.assertIsNone(aliases.read_block_alias(key))
        identity = {"blocks": [{"block": "cbuffer5", "bind": 5, "space": 0,
                                "members": members, "memberMappingAvailable": True,
                                "leaves": [{"path": "unknown[1]", "offset": 16, "byteSize": 16},
                                           {"path": "unknown[2]", "offset": 32, "byteSize": 16}]}]}
        source = "cbuffer generated : register(b5) { float4 v1 : packoffset(c1); float4 v2 : packoffset(c2); };"
        evidence = aliases.evidence_for_source(source, identity)["blocks"][0]
        self.assertEqual(evidence["ai"], [])
        self.assertEqual(evidence["user"], [{"path": "v2", "alias": "User Choice"}])
        self.assertNotIn("blockAlias", evidence)
        self.assertEqual(evidence["aiSuppressedReasonId"], "unscoped-placeholder-layout")
        self.assertEqual(path.read_bytes(), before)
        # Explicit user edits work, retaining the suppressed historical AI record.
        result = aliases.publish_aliases("cbuffer5", members, [
            {"path": "unknown[3]", "offset": 48, "alias": "User Third"}], "user", block_alias="User Block")
        self.assertTrue(result["applied"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["aliases"][0], payload["aliases"][0] | {"updatedAt": ""})
        self.assertEqual(aliases.read_block_alias(key)["alias"], "User Block")
        blocked = aliases.publish_aliases("cbuffer5", members, [
            {"path": "unknown[2]", "offset": 32, "alias": "AI Again"}], block_alias="AI Block")
        self.assertFalse(blocked["applied"])
        self.assertEqual(blocked["keptUser"], ["cbuffer5.unknown[2]"])
        self.assertTrue(blocked["blockAliasKeptUser"])

    def test_placeholder_predicate_does_not_treat_named_fields_as_unknown(self) -> None:
        for name in ("unknown", "UNKNOWN", " unknown[0] "):
            self.assertTrue(aliases.has_placeholder_members([{"name": name, "offset": 0}]), name)
        for name in ("v0", "parameters", "unknownColor", "known_unknown"):
            self.assertFalse(aliases.has_placeholder_members([{"name": name, "offset": 0}]), name)


class ProjectSharedStoreTests(unittest.TestCase):
    """With a project selected the project's share is the one alias store."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "project").mkdir()
        (root / "user").mkdir()
        self.env = mock.patch.dict(os.environ, {
            "REPLAYLAB_ALIAS_ROOT": str(root / "user"),
            "REPLAYLAB_ALIAS_PROJECT_ROOT": str(root / "project"),
        })
        self.env.start()
        self.members = [{"name": "parameters", "offset": 0}]

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _project_file(self, key: str) -> Path:
        return (Path(self.tmp.name) / "project" / "shared" / "dictionaries" / "cbuffer-aliases"
                / (key + ".json"))

    def test_the_project_share_is_the_record_and_user_names_beat_ai(self) -> None:
        report = aliases.publish_aliases("cbuffer1", self.members,
                                         [{"path": "unknown[0]", "offset": 0, "alias": "tint"}],
                                         source="ai", block_alias="MaterialCB")
        self.assertEqual(report["store"], "project")
        self.assertTrue(self._project_file(report["layoutSha256"]).is_file(), report["path"])
        # The user renames in the panel: that name wins over a later AI publish.
        aliases.publish_aliases("cbuffer1", self.members,
                                [{"path": "unknown[0]", "offset": 0, "alias": "手动名"}], source="user")
        later = aliases.publish_aliases("cbuffer1", self.members,
                                        [{"path": "unknown[0]", "offset": 0, "alias": "aiAgain"},
                                         {"path": "unknown[1]", "offset": 16, "alias": "second"}],
                                        source="ai")
        self.assertEqual(later["keptUser"], ["cbuffer1.unknown[0]"])
        self.assertEqual({item["path"]: (item["alias"], item["source"])
                          for item in aliases.read_aliases(report["layoutSha256"])},
                         {"unknown[0]": ("手动名", "user"), "unknown[1]": ("second", "ai")})
        # No alias data was written under the per-user root while a project is
        # selected. The retained coordination file is not alias content.
        self.assertEqual(
            [item for item in (Path(self.tmp.name) / "user").iterdir()
             if item.name != ".alias-publication.lock"],
            [],
        )

    def test_a_pre_project_user_file_is_adopted_once_then_ignored(self) -> None:
        project_root = os.environ.pop("REPLAYLAB_ALIAS_PROJECT_ROOT")
        before = aliases.publish_aliases("cbuffer1", self.members,
                                         [{"path": "unknown[1]", "offset": 16, "alias": "早先的名字"}],
                                         source="user")
        self.assertEqual(before["store"], "user")
        os.environ["REPLAYLAB_ALIAS_PROJECT_ROOT"] = project_root
        key = before["layoutSha256"]
        self.assertEqual([item["alias"] for item in aliases.read_aliases(key)], ["早先的名字"])
        self.assertTrue(self._project_file(key).is_file())
        # The per-user file is history now: a change there does not reach the project.
        aliases_user_file = Path(self.tmp.name) / "user" / (key + ".json")
        import json
        payload = json.loads(aliases_user_file.read_text(encoding="utf-8"))
        payload["aliases"][0]["alias"] = "后来改的"
        aliases_user_file.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual([item["alias"] for item in aliases.read_aliases(key)], ["早先的名字"])

    def test_without_a_project_the_user_store_is_the_root(self) -> None:
        os.environ.pop("REPLAYLAB_ALIAS_PROJECT_ROOT", None)
        self.assertEqual(aliases.store_root(), Path(self.tmp.name) / "user")
