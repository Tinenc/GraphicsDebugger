from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

from src.asset_catalog import AssetCatalogError, AssetCatalogService
from src import asset_catalog
from src.shader_asset_retrieval import retrieve, texture_identity_evidence, value_evidence
from src.symbols.imaging import fingerprint
from tests.test_texture_identity import gradient_rgba, png_bytes
from tests.test_texture_identity import texture_package, texture_uexp


def _partial(catalog):
    # A committed checkpoint during the first scan has the same tables but no
    # final-scan counters. Existing evidence must remain queryable.
    with sqlite3.connect(catalog.database_path) as db:
        db.execute("DELETE FROM metadata WHERE key IN ('valueIndex','thumbnailIndex')")
        db.execute("UPDATE metadata SET value='false' WHERE key='complete'")


def _fixture(tmp_path):
    root = tmp_path / "assets"
    (root / "textures").mkdir(parents=True)
    (root / "materials").mkdir()
    owner = root / "textures" / "cloth.png"
    rgba = gradient_rgba(32, 32)
    owner.write_bytes(png_bytes(rgba, 32, 32))
    (root / "materials" / "cloth.mtl").write_text("newmtl Cloth\nmap_Kd ../textures/cloth.png\n")
    numeric = b"MATL\x00\x01Sheen\x00" + struct.pack("<ff", 12.354754, 0.7318211)
    (root / "materials" / "coat.matbin").write_bytes(numeric)
    catalog = AssetCatalogService(tmp_path / "catalog.sqlite3")
    catalog.scan(root, build_id="test", depth="headers")
    _partial(catalog)
    return root, owner, catalog, fingerprint(rgba, 32, 32)


def test_partial_catalog_searches_committed_pixels_values_and_reverse_references(tmp_path):
    root, owner, catalog, fp = _fixture(tmp_path)
    pixels = catalog.search_thumbnails(fp["dhash"], mean_color=fp["meanColor"])
    assert pixels["indexCoverage"] == "partial"
    assert pixels["indexedThumbnails"] == 1
    assert pixels["candidates"][0]["relativePath"] == "textures/cloth.png"
    assert catalog.search_values([12.354754])["candidates"]
    assert catalog.coverage()["thumbnailRows"] == 1
    evidence = texture_identity_evidence(catalog, [{**fp, "available": True, "resourceId": 44}])
    # MTL uses its own relation name; direct original-asset edges still survive.
    asset = pixels["candidates"][0]
    assert catalog.referrers(asset["assetId"])["referrers"][0]["relativePath"] == "materials/cloth.mtl"
    assert evidence["perTexture"][0]["matches"]


def test_missing_numeric_index_is_reported_unavailable_not_zero_match():
    catalog = mock.Mock()
    catalog.search_values.side_effect = RuntimeError("index unavailable")
    diagnostics = {}
    assert value_evidence(catalog, [{"bind": 0, "space": 0, "lane": 7, "value": 12.354754}],
                          diagnostics=diagnostics) == {}
    assert diagnostics["status"] == "unavailable"
    assert diagnostics["absenceProven"] is False
    assert "requiredAction" not in diagnostics


def test_cooked_mip_and_reader_preview_do_not_double_count_one_texture():
    fp = fingerprint(gradient_rgba(32, 32), 32, 32)
    catalog = mock.Mock()
    catalog.search_thumbnails.return_value = {"indexedThumbnails": 2, "candidates": [
        {"relativePath": "textures/coat.uexp", "distance": 1, "verdict": "same-image"},
        {"relativePath": "textures/coat.uasset", "distance": 2, "verdict": "same-image"}]}
    catalog.query.return_value = {"assets": [{"relativePath": "textures/coat.uasset", "assetId": "owner"}]}
    catalog.referrers.return_value = {"referrers": [{"relativePath": "materials/coat.uasset"}]}
    result = texture_identity_evidence(catalog, [{**fp, "available": True, "resourceId": 12}])
    assert result["byMaterial"]["materials/coat.uasset"]["sameImage"] == 1
    assert result["byMaterial"]["materials/coat.uasset"]["physicalIdentityVerified"] is False
    assert "strong perceptual matches" in result["byMaterial"]["materials/coat.uasset"]["summary"]
    assert "exact" not in result["byMaterial"]["materials/coat.uasset"]["summary"]
    assert len(result["perTexture"][0]["matches"]) == 1


def _index(root, owner, catalog, tmp_path, **overrides):
    payload = root / "textures" / "cloth.ubulk"
    if not payload.exists():
        payload.write_bytes(b"original compressed texture payload")
    preview = tmp_path / "reader-preview.png"
    preview.write_bytes(owner.read_bytes())
    inputs = [{"relativePath": p.relative_to(root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
              for p in (owner, payload)]
    kwargs = dict(expected_preview_sha256=hashlib.sha256(preview.read_bytes()).hexdigest(),
                  expected_source_sha256=inputs[0]["sha256"],
                  provenance={"operationId": "fixture-reader", "receiptSha256": "f" * 64,
                              "sourceFiles": inputs, "colorSpace": "reader-unspecified"})
    kwargs.update(overrides)
    with mock.patch.object(catalog, "_detect_engine", side_effect=AssertionError("must not scan the tree")):
        result = catalog.index_texture_preview(root, owner.relative_to(root).as_posix(), preview, **kwargs)
    return result, preview, payload


def test_verified_reader_preview_is_searchable_without_full_scan_and_preserves_edges(tmp_path):
    root, owner, catalog, fp = _fixture(tmp_path)
    result, preview, payload = _index(root, owner, catalog, tmp_path)
    assert result["indexed"] is True
    assert result["physicalIdentityVerified"] is False
    assert catalog.status()["complete"] is False
    search = catalog.search_thumbnails(fp["dhash"])
    assert search["candidates"][0]["evidenceKind"] == "decoded-reader-preview"
    assert catalog.referrers(result["assetId"])["referrers"]
    assert catalog.verify()["verified"] is True


def test_selected_large_reader_preview_is_admitted_without_expanding_directory_budget(tmp_path):
    root, owner, catalog, unused = _fixture(tmp_path)
    # Just over the former 1M-pixel ceiling; readers commonly return 2K/4K.
    width, height = 1025, 1024
    rgba = bytes((80, 120, 160, 255)) * width * height
    data = png_bytes(rgba, width, height)
    assert asset_catalog._image_fingerprint(data, ".png")["available"] is False
    owner.write_bytes(data)
    catalog.hydrate(root, [owner.relative_to(root).as_posix()])
    result, preview, payload = _index(root, owner, catalog, tmp_path)
    assert result["indexed"] is True
    stored = catalog.query(relative_paths=[owner.relative_to(root).as_posix()])["assets"][0]
    fp = stored["metadata"]["textureFingerprint"]
    assert (fp["sizeX"], fp["sizeY"]) == (width, height)
    assert fp["previewProvenance"]["previewSha256"] == hashlib.sha256(data).hexdigest()


def test_reader_preview_byte_bound_is_checked_before_read(tmp_path, monkeypatch):
    preview = tmp_path / "too-big.png"
    preview.write_bytes(b"PNG-over-limit")
    monkeypatch.setattr(asset_catalog, "MAX_READER_PREVIEW_BYTES", 4)
    with pytest.raises(AssetCatalogError, match="byte bound"):
        asset_catalog._preview_bytes(preview, hashlib.sha256(preview.read_bytes()).hexdigest())


@pytest.mark.parametrize("replace_build", [False, True])
def test_preview_transaction_uses_current_catalog_version(tmp_path, replace_build):
    root, owner, catalog, fp = _fixture(tmp_path)
    opened, resume = threading.Event(), threading.Event()
    original_connect = asset_catalog._database_connection

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        if not kwargs.get("read_only") and threading.current_thread().name.startswith("preview"):
            opened.set()
            if not resume.wait(10):
                connection.close()
                raise RuntimeError("concurrent catalog writer did not finish")
        return connection

    with mock.patch.object(asset_catalog, "_database_connection", side_effect=connect), \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview") as pool:
        future = pool.submit(_index, root, owner, catalog, tmp_path)
        try:
            assert opened.wait(10)
            if replace_build:
                with sqlite3.connect(catalog.database_path) as db:
                    db.execute("UPDATE metadata SET value='new-build' WHERE key='buildId'")
            else:
                # A real concurrent hydration updates an unrelated row and its
                # manifest aggregate after preview admission, before its write.
                material = root / "materials" / "cloth.mtl"
                material.write_text("newmtl Updated\nmap_Kd ../textures/cloth.png\n")
                catalog.hydrate(root, ["materials/cloth.mtl"])
        finally:
            resume.set()
        if replace_build:
            with pytest.raises(AssetCatalogError, match="catalog changed"):
                future.result(timeout=10)
            assert catalog.status()["buildId"] == "new-build"
            assert "previewProvenance" not in catalog.query(
                relative_paths=["textures/cloth.png"])["assets"][0]["metadata"]["textureFingerprint"]
        else:
            assert future.result(timeout=10)[0]["indexed"] is True
            assert catalog.verify()["verified"] is True
            assert catalog.query(relative_paths=["materials/cloth.mtl"])["assets"][0]["contentSha256"] == \
                hashlib.sha256(material.read_bytes()).hexdigest()


@pytest.mark.parametrize("changed", ["owner", "payload", "preview"])
def test_preview_reuse_rechecks_current_bytes_even_same_size_and_mtime(tmp_path, changed):
    root, owner, catalog, fp = _fixture(tmp_path)
    result, preview, payload = _index(root, owner, catalog, tmp_path)
    path = {"owner": owner, "payload": payload, "preview": preview}[changed]
    stat = path.stat()
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(raw)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    result = catalog.search_thumbnails(fp["dhash"])
    assert result["candidates"] == []
    assert result["unavailableEvidence"]
    assert result["absenceProven"] is False


def test_reader_preview_requires_bound_owner_and_receipt(tmp_path):
    root, owner, catalog, fp = _fixture(tmp_path)
    with pytest.raises(AssetCatalogError, match="receipt"):
        _index(root, owner, catalog, tmp_path, provenance={})
    with pytest.raises(AssetCatalogError, match="bind the original"):
        _index(root, owner, catalog, tmp_path, expected_source_sha256="0" * 64)


def test_reader_preview_rejects_another_root_even_if_owner_bytes_match(tmp_path):
    root, owner, catalog, fp = _fixture(tmp_path)
    foreign = tmp_path / "foreign"
    (foreign / "textures").mkdir(parents=True)
    other = foreign / "textures" / owner.name
    other.write_bytes(owner.read_bytes())
    with pytest.raises(AssetCatalogError, match="another configured root"):
        _index(foreign, other, catalog, tmp_path)


def test_exact_path_lookup_avoids_basename_candidate_limit(tmp_path):
    root, owner, catalog, fp = _fixture(tmp_path)
    for i in range(25):
        folder = root / ("copy%02d" % i)
        folder.mkdir()
        (folder / owner.name).write_bytes(owner.read_bytes())
    catalog.scan(root, build_id="test", depth="headers")
    result = catalog.query(relative_paths=["textures/cloth.png"], limit=1)
    assert result["assets"][0]["relativePath"] == "textures/cloth.png"
    assert not result["truncated"]


def test_empty_partial_texture_index_is_not_negative_asset_evidence(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    catalog = AssetCatalogService(tmp_path / "catalog.sqlite3")
    catalog.scan(root, build_id="empty")
    _partial(catalog)
    fp = fingerprint(gradient_rgba(32, 32), 32, 32)
    result = texture_identity_evidence(catalog, [{**fp, "available": True, "resourceId": 44}])
    entry = result["perTexture"][0]
    assert entry["status"] == "unavailable"
    assert entry["absenceProven"] is False
    assert "requiredAction" not in entry


def test_selected_scope_hydrates_texture_payloads_without_tree_scan(tmp_path):
    root = tmp_path / "assets"
    selected = root / "Game" / "Content" / "Textures"
    selected.mkdir(parents=True)
    (root / "Game" / "Game.uproject").write_text("{}")
    (selected / "Tex.uasset").write_bytes(texture_package("T_Crow_Body_D"))
    rgba = gradient_rgba(32, 32)
    (selected / "Tex.uexp").write_bytes(texture_uexp(rgba, 32, 32))
    catalog = AssetCatalogService(tmp_path / "catalog.sqlite3")
    catalog.scan(root, build_id="test", depth="names")
    _partial(catalog)
    with mock.patch.object(catalog, "_detect_engine", side_effect=AssertionError("must not rescan")):
        receipt = catalog.hydrate_scope(root, ["Game/Content/Textures"], max_files=8)
    assert receipt["textureExports"] == 1
    assert receipt["textureEvidence"][0]["available"] is True
    assert "Game/Content/Textures/Tex.uexp" in receipt["hydrated"]
    assert catalog.search_thumbnails(fingerprint(rgba, 32, 32)["dhash"])["candidates"]
