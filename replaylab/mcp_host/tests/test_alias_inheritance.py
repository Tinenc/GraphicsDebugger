"""CBuffer names survive reopening the capture.

The capture identity hashes RenderDoc's ResourceIds, which are reassigned on
every open, so the same .rdc reopened is a new ``capture_id`` and every alias the
user approved was stranded under the old one. The Shader is the same bytes at the
same stage and entry point, so that is the key the names travel on.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.symbols.store import SymbolStore


def _catalog(capture_id: str, resource_id: int, raw: str) -> dict:
    return {
        "captureId": capture_id,
        "shaders": [{
            "shaderResourceId": resource_id, "stage": "fragment",
            "entryPoint": "MainPS", "rawSha256": raw,
        }],
    }


class AliasInheritanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SymbolStore(Path(self.temporary.name) / "symbols.sqlite3")
        self.raw = "a" * 64

    def _alias(self, capture_id: str, resource_id: int, **overrides) -> dict:
        record = {
            "captureId": capture_id, "shaderResourceId": resource_id,
            "stage": "fragment", "entryPoint": "MainPS", "bindingKind": "cbuffer-member",
            "registerSpace": 0, "bindNumber": 1, "byteOffset": 16,
            "canonicalPath": "cb1.member_2", "alias": "SunDirection",
            "originalName": "member_2", "provenance": "ai", "confidence": 0.9,
            "verified": True, "metadata": {},
        }
        record.update(overrides)
        return record

    def test_reopening_the_capture_finds_the_aliases_again(self) -> None:
        self.store.save_capture_catalog("first.json", _catalog("cap-one", 853, self.raw))
        self.store.save_alias(self._alias("cap-one", 853))
        self.assertEqual(self.store.list_aliases("cap-two"), [])

        # RenderDoc reopened the file: new capture identity, new ResourceId, same bytes.
        self.store.save_capture_catalog("second.json", _catalog("cap-two", 12212, self.raw))
        inherited = self.store.list_aliases("cap-two")
        self.assertEqual([item["alias"] for item in inherited], ["SunDirection"])
        self.assertEqual(inherited[0]["shader_resource_id"], 12212)
        self.assertTrue(inherited[0]["verified"])
        self.assertEqual(inherited[0]["metadata"]["inheritedFrom"]["captureId"], "cap-one")

    def test_a_different_shader_inherits_nothing(self) -> None:
        self.store.save_capture_catalog("first.json", _catalog("cap-one", 853, self.raw))
        self.store.save_alias(self._alias("cap-one", 853))
        self.store.save_capture_catalog("other.json", _catalog("cap-three", 853, "b" * 64))
        self.assertEqual(self.store.list_aliases("cap-three"), [])

    def test_the_most_recent_capture_is_the_donor(self) -> None:
        self.store.save_capture_catalog("one.json", _catalog("cap-one", 1, self.raw))
        self.store.save_alias(self._alias("cap-one", 1, alias="OldName"))
        self.store.save_capture_catalog("two.json", _catalog("cap-two", 2, self.raw))
        # The user renamed it in the second session.
        self.store.save_alias(self._alias("cap-two", 2, alias="NewName"))
        self.store.save_capture_catalog("three.json", _catalog("cap-three", 3, self.raw))
        aliases = {item["alias"] for item in self.store.list_aliases("cap-three")}
        self.assertIn("NewName", aliases)


class InheritanceSurvivesTheMatcherTests(unittest.TestCase):
    """The store alone passing is not enough: the matcher and the export path run next."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "symbols.sqlite3"
        self.store = SymbolStore(self.database)
        import hashlib
        self.blob = b"DXBC-not-really-a-container-but-stable-bytes"
        self.raw = hashlib.sha256(self.blob).hexdigest()

    def _verified_program_alias(self, capture_id: str, resource_id: int) -> dict:
        return {
            "captureId": capture_id, "shaderResourceId": resource_id,
            "stage": "fragment", "entryPoint": "MainPS", "bindingKind": "shader",
            "canonicalPath": "", "alias": "M_Hair_Anisotropic", "originalName": "",
            "provenance": "program", "confidence": 0.98, "verified": True,
            "metadata": {"programVerified": True, "captureId": capture_id,
                         "captureRawSha256": self.raw, "stage": "fragment",
                         "entryPoint": "MainPS"},
        }

    def test_the_matcher_keeps_inherited_rows_it_cannot_prove_again(self) -> None:
        import json

        from src.symbols import matcher

        self.store.save_capture_catalog("first.json", _catalog("cap-one", 853, self.raw))
        self.store.save_alias(self._verified_program_alias("cap-one", 853))
        # A scan with no shaders in it: the matcher can prove nothing this time
        # (the asset folder moved, the provider is gone), which is exactly when the
        # remembered names are worth having.
        self.store.replace_asset_scan("scan-empty", str(self.root), "", {})
        # The matcher verifies rawSha256 against the bytes on disk.
        blob = self.root / "shader.bin"
        blob.write_bytes(self.blob)
        catalog = dict(_catalog("cap-two", 12212, self.raw),
                       schema=matcher.CATALOG_SCHEMA, schemaVersion=matcher.CATALOG_VERSION)
        catalog["shaders"][0].update({"rawPath": "shader.bin", "rawSize": len(self.blob)})
        # The matcher derives the identity itself; leave it to compute it.
        catalog.pop("captureId")
        catalog_path = self.root / "second.json"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

        result = matcher.match_capture_catalog(catalog_path, "scan-empty", self.database)
        capture_id = result["captureId"]
        self.assertNotEqual(capture_id, "cap-one")

        rows = self.store.list_aliases(capture_id, verified_only=True)
        self.assertEqual([item["alias"] for item in rows], ["M_Hair_Anisotropic"])
        self.assertEqual(rows[0]["metadata"]["captureId"], capture_id)
        self.assertEqual(rows[0]["metadata"]["inheritedFrom"]["captureId"], "cap-one")

    def test_the_export_path_accepts_inherited_rows(self) -> None:
        from src.symbols.service import SymbolService

        self.store.save_capture_catalog("first.json", _catalog("cap-one", 853, self.raw))
        self.store.save_alias(self._verified_program_alias("cap-one", 853))
        self.store.save_capture_catalog("second.json", _catalog("cap-two", 12212, self.raw))

        service = SymbolService(self.database)
        result = service.export_alias_table("cap-two", self.root / "aliases.json")
        self.assertEqual(result["aliasCount"], 1)
        self.assertEqual(result["shaderCount"], 1)

    def test_a_re_save_does_not_stack_older_names_next_to_newer_ones(self) -> None:
        self.store.save_capture_catalog("one.json", _catalog("cap-one", 1, self.raw))
        self.store.save_alias(self._verified_program_alias("cap-one", 1))
        self.store.save_capture_catalog("two.json", _catalog("cap-two", 2, self.raw))
        renamed = self._verified_program_alias("cap-two", 2)
        renamed["alias"] = "M_Hair_v2"
        self.store.clear_aliases("cap-two", 2, "fragment", "MainPS")
        self.store.save_alias(renamed)
        # Saving the same identity again (a rescan) must not bring the old name back.
        self.store.save_capture_catalog("two.json", _catalog("cap-two", 2, self.raw))
        self.assertEqual({item["alias"] for item in self.store.list_aliases("cap-two")},
                         {"M_Hair_v2"})
