from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import io
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import shutil
import struct
import sys
from types import ModuleType, SimpleNamespace

import pytest

from src.frame_render_flow import FrameRenderFlowStore, build_document, validate_outline
from test_frame_render_flow import outline, stage


SOURCE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/frame_flow_persistence.py"
spec = spec_from_file_location("frame_flow_persistence", SOURCE)
storage = module_from_spec(spec)
spec.loader.exec_module(storage)


@pytest.mark.parametrize("size", [37, 1024 * 1024 + 91])
def test_bounded_preview_read_does_not_allocate_the_safety_ceiling(monkeypatch, size):
    payload = b"x" * size
    requests = []

    class Reader(io.BytesIO):
        def read(self, count=-1):
            requests.append(count)
            return super().read(count)

    source = SimpleNamespace(is_file=lambda: True,
                             stat=lambda: SimpleNamespace(st_size=size),
                             open=lambda mode: Reader(payload))
    monkeypatch.setattr(storage, "_ordinary", lambda path: source)
    assert storage._bounded("preview", storage.MAX_OUTLINE) == payload
    assert requests and 0 < max(requests) <= 1024 * 1024


def test_bounded_read_still_rejects_a_file_growing_past_its_limit(monkeypatch):
    maximum = 1024 * 1024 + 9
    source = SimpleNamespace(is_file=lambda: True,
                             stat=lambda: SimpleNamespace(st_size=1),
                             open=lambda mode: io.BytesIO(b"x" * (maximum + 1)))
    monkeypatch.setattr(storage, "_ordinary", lambda path: source)
    with pytest.raises(ValueError, match="changed size"):
        storage._bounded("growing", maximum)


def write_rdc_container(path, frame=b"immutable frame payload", metadata=b"old edited shaders", thumbnail=b""):
    """Native 1.2 container layout; the synthetic frame body is not replayed."""
    previous_mtime = path.stat().st_mtime_ns if path.exists() else 0
    def section(kind, name, payload):
        name = name.encode("utf-8") + b"\0"
        return struct.pack("<4sIQQQII", b"\0" * 4, kind, len(payload), len(payload), 1, 0, len(name)) + name + payload
    header = (struct.pack("<HHI", 1 if thumbnail else 0, 1 if thumbnail else 0, len(thumbnail)) + thumbnail +
              struct.pack("<QIB", 100, 4, 7) + b"Vulkan\0" + struct.pack("<Qd", 1, 1.0))
    data = struct.pack("<QII16s", 0x434f4452, 0x102, 32 + len(header), b"test") + header
    path.write_bytes(data + section(1, "FrameCapture", frame) + section(9, "EditedShaders", metadata))
    # Guarantee a distinct stat key even on low-resolution Windows volumes.
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, max(previous_mtime, info.st_mtime_ns) + 1000000000))


@pytest.fixture
def bundle(tmp_path):
    capture = tmp_path / "captures" / "frame.rdc"
    capture.parent.mkdir()
    capture.write_bytes(b"captured contents" * 100)
    data = outline(3)
    data["capturePath"] = str(capture)
    data = validate_outline(data)
    record = FrameRenderFlowStore().prepare(data)
    document = build_document(data, record["flowId"], record["snapshotId"], [stage(1, 7)],
                              title="Frame", summary="Visible surfaces are shaded, then composed.")
    png = tmp_path / "preview.png"
    png.write_bytes(b"pretend already verified PNG receipt")
    sha = hashlib.sha256(png.read_bytes()).hexdigest()
    document["stages"][0]["images"] = [{"path": str(png), "sha256": sha, "manifestPath": "old-manifest.json",
                                        "eventId": 7, "resourceId": 9}]
    root = tmp_path / "local-artifacts"
    return capture, document, data, root


def test_bundle_is_portable_and_rebinds_only_after_file_and_action_identity_checks(bundle, tmp_path):
    capture, document, data, root = bundle
    receipt = storage.save(capture, document, data, root)
    assert receipt["saved"] and receipt["portable"] and receipt["mediaCount"] == 1
    assert capture.read_bytes() == b"captured contents" * 100
    moved = tmp_path / "moved" / "frame.rdc"
    moved.parent.mkdir()
    shutil.copy2(capture, moved)
    shutil.copytree(storage.sidecar_directory(capture).parent, storage.sidecar_directory(moved).parent)
    Path(document["stages"][0]["images"][0]["path"]).unlink()
    restored = storage.load(moved, root)
    current = deepcopy(data)
    current.update(capturePath=str(moved), captureSessionId="b" * 32, captureId="c" * 64)
    rebound = storage.rebind(restored, current)
    record = FrameRenderFlowStore().prepare(current)
    assert rebound["document"]["flowId"] == record["flowId"]
    assert rebound["document"]["captureSessionId"] == "b" * 32
    assert Path(rebound["document"]["stages"][0]["images"][0]["path"]).is_file()
    current["actions"][1]["outputs"] = [1234]
    with pytest.raises(ValueError, match="action tree"):
        storage.rebind(restored, current)


def test_restore_prior_revision_preserves_newer_document_and_validates_media(bundle):
    capture,document,outline_data,root=bundle
    old=storage.save(capture,document,outline_data,root)
    folder=Path(old['directory']);original_pointer=json.loads((folder/'current.json').read_text())
    changed=deepcopy(document);changed['title']='Local explanation accidentally published'
    storage.save(capture,changed,outline_data,root)
    newer=json.loads((folder/'current.json').read_text())
    restored=storage.restore_revision(capture,root,original_pointer['documentSha256'])
    assert restored['document']['title']=='Frame'
    assert (folder/(newer['documentSha256']+'.json')).is_file()
    assert list(folder.glob('*.pointer.json'))
    before=(folder/'current.json').read_bytes()
    media=next((folder/'media').glob('*'));media.write_bytes(b'corrupt')
    with pytest.raises(ValueError):storage.restore_revision(capture,root,newer['documentSha256'])
    assert (folder/'current.json').read_bytes()==before


def test_replaced_same_name_capture_is_not_restored_even_with_same_length(bundle):
    capture, document, data, root = bundle
    storage.save(capture, document, data, root)
    original = capture.stat()
    capture.write_bytes(b"replacement data" * 100)
    os.utime(capture, ns=(original.st_atime_ns, original.st_mtime_ns))
    with pytest.raises(ValueError, match="capture file identity"):
        storage.load(capture, root)


def test_native_editable_sections_and_header_thumbnail_do_not_change_frame_identity(bundle):
    capture, document, data, root = bundle
    write_rdc_container(capture)
    first = storage.capture_fingerprint(capture)
    assert first["method"] == "rdc-frame-section-sha256-and-action-outline/v2"
    assert first["frameCaptureSha256"] == hashlib.sha256(b"immutable frame payload").hexdigest()
    storage.save(capture, document, data, root)
    write_rdc_container(capture, metadata=b"replacement shaders" * 100, thumbnail=b"replacement thumbnail")
    assert storage.capture_fingerprint(capture) == first
    assert storage.load(capture, root)["document"]["title"] == document["title"]
    write_rdc_container(capture, metadata=b"short")
    assert storage.load(capture, root)["captureFingerprint"] == first


def test_any_frame_payload_change_invalidates_native_bundle_even_with_identical_outline(bundle):
    capture, document, data, root = bundle
    frame = bytearray(b"x" * (2 * 1024 * 1024 + 7))
    write_rdc_container(capture, frame=frame)
    storage.save(capture, document, data, root)
    frame[100003] = ord("y")
    write_rdc_container(capture, frame=frame)
    with pytest.raises(ValueError, match="capture file identity"):
        storage.load(capture, root)


def test_strictly_matching_legacy_native_bundle_upgrades_before_metadata_edit(bundle):
    capture, document, data, root = bundle
    write_rdc_container(capture)
    storage.save(capture, document, data, root)
    pointer_path = storage.sidecar_directory(capture) / "current.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["capture"] = storage._legacy_capture_fingerprint(capture)
    pointer_path.write_bytes(storage.encoded(pointer))
    restored = storage.load(capture, root)
    assert restored["captureFingerprint"]["method"] == "rdc-frame-section-sha256-and-action-outline/v2"
    assert json.loads(pointer_path.read_text())["capture"] == restored["captureFingerprint"]
    write_rdc_container(capture, metadata=b"later native SaveEdits" * 50)
    assert storage.load(capture, root)["document"]["title"] == document["title"]


def test_changed_legacy_bundle_is_not_adopted_only_because_its_outline_matches(bundle):
    capture, document, data, root = bundle
    write_rdc_container(capture)
    storage.save(capture, document, data, root)
    pointer_path = storage.sidecar_directory(capture) / "current.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["capture"] = storage._legacy_capture_fingerprint(capture)
    before = storage.encoded(pointer)
    pointer_path.write_bytes(before)
    write_rdc_container(capture, metadata=b"unobserved edit")
    with pytest.raises(ValueError, match="capture file identity"):
        storage.load(capture, root)
    assert pointer_path.read_bytes() == before


def test_frame_identity_is_cached_for_unchanged_stat_and_rechecked_after_metadata_write(bundle, monkeypatch):
    capture, document, data, root = bundle
    write_rdc_container(capture)
    storage._FRAME_FINGERPRINT_CACHE.clear()
    calls, original = [], Path.open
    def opened(path, *args, **kwargs):
        if path == capture:
            calls.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    first = storage.capture_fingerprint(capture)
    first["frameCaptureSha256"] = "caller-must-not-poison-cache"
    second = storage.capture_fingerprint(capture)
    assert len(calls) == 1 and second["frameCaptureSha256"] != first["frameCaptureSha256"]
    previous_mtime = capture.stat().st_mtime_ns
    write_rdc_container(capture, metadata=b"new edited shaders")
    assert capture.stat().st_mtime_ns != previous_mtime
    calls.clear()
    assert storage.capture_fingerprint(capture) == second and len(calls) == 1


@pytest.mark.parametrize("position,payload", [(8, b"\xff\xff\xff\xff"), (12, b"\xff\xff\xff\xff"),
                                             (76, b"A"), (80, b"\x09\0\0\0")])
def test_malformed_native_frame_header_does_not_fall_back_to_weak_file_sampling(bundle, position, payload):
    capture, document, data, root = bundle
    write_rdc_container(capture)
    content = bytearray(capture.read_bytes())
    content[position:position + len(payload)] = payload
    capture.write_bytes(content)
    with pytest.raises(ValueError):
        storage.capture_fingerprint(capture)


def test_volume_slices_are_portable_verified_assets_not_lost_on_reopen(bundle, tmp_path):
    capture, document, data, root = bundle
    slice_file = tmp_path / "volume-z3.png"
    slice_file.write_bytes(b"verified volume slice content")
    digest = hashlib.sha256(slice_file.read_bytes()).hexdigest()
    document["stages"][0]["images"][0]["volumePreview"] = {
        "depth": 8, "mip": 0, "selectedSlice": 3,
        "slices": [{"slice": 3, "channel": "r", "path": str(slice_file),
                    "sha256": digest, "width": 4, "height": 4}]}
    receipt = storage.save(capture, document, data, root)
    assert receipt["mediaCount"] == 2
    slice_file.unlink()
    restored = storage.load(capture, root)
    preview = restored["document"]["stages"][0]["images"][0]["volumePreview"]
    assert preview["selectedSlice"] == 3
    assert Path(preview["slices"][0]["path"]).read_bytes() == b"verified volume slice content"


def test_volume_changed_slice_cannot_be_saved_with_stale_receipt(bundle, tmp_path):
    capture, document, data, root = bundle
    slice_file = tmp_path / "volume-z3.png"
    slice_file.write_bytes(b"changed slice")
    document["stages"][0]["images"][0]["volumePreview"] = {"slices": [
        {"path": str(slice_file), "sha256": "a" * 64}]}
    with pytest.raises(ValueError, match="changed before persistence"):
        storage.save(capture, document, data, root)


def test_more_than_1024_distinct_pngs_including_volume_children_survive_reopen(bundle, tmp_path):
    from PIL import Image
    capture, document, data, root = bundle
    parent = document["stages"][0]["images"][0]
    base_path = Path(parent["path"])
    Image.new("RGB", (1, 1), (255, 255, 255)).save(base_path)
    parent["sha256"] = hashlib.sha256(base_path.read_bytes()).hexdigest()
    sources = [base_path]
    # Eight volumes, each with 32 Z slices and four physical channels, plus
    # the shared main preview: 1025 distinct PNG assets, all within UI bounds.
    for volume_index in range(8):
        volume_parent = deepcopy(parent)
        slices = []
        for index in range(128):
            identity = volume_index * 128 + index
            path = tmp_path / ("volume-%d.png" % identity)
            Image.new("RGB", (1, 1), (identity & 255, identity >> 8, 37)).save(path)
            slices.append({"slice": index // 4, "channel": "rgba"[index % 4],
                           "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "width": 1, "height": 1})
            sources.append(path)
        volume_parent["volumePreview"] = {"depth": 32, "mip": 0, "selectedSlice": 16, "slices": slices}
        document["stages"][0]["images"].append(volume_parent)
    receipt = storage.save(capture, document, data, root)
    assert receipt["mediaCount"] == 1025
    for path in sources:
        path.unlink()
    restored = storage.load(capture, root)
    current = deepcopy(data)
    current["captureSessionId"] = "b" * 32
    rebound = storage.rebind(restored, current)["document"]
    assert rebound["captureSessionId"] == "b" * 32
    assets = list(storage._asset_fields(rebound))
    assert len({owner[key] for owner, key, _, _ in assets}) == 1025
    for owner, key, hash_key, _ in assets:
        assert hashlib.sha256(Path(owner[key]).read_bytes()).hexdigest() == owner[hash_key]


@pytest.mark.parametrize("budget", ["MAX_MEDIA_ENTRIES", "MAX_POINTER"])
def test_same_inventory_limits_apply_before_committing_save_and_during_load(bundle, tmp_path, monkeypatch, budget):
    capture, document, data, root = bundle
    storage.save(capture, document, data, root)
    pointer = storage.sidecar_directory(capture) / "current.json"
    before = pointer.read_bytes()
    limit = 0 if budget == "MAX_MEDIA_ENTRIES" else len(before) - 1
    monkeypatch.setattr(storage, budget, limit)
    with pytest.raises(ValueError, match="budget"):
        storage.save(capture, document, data, root)
    assert pointer.read_bytes() == before
    with pytest.raises(ValueError):
        storage.load(capture, root)


def test_timestamp_change_rejects_changes_outside_fingerprint_samples(bundle):
    capture, document, data, root = bundle
    storage.save(capture, document, data, root)
    info = capture.stat()
    os.utime(capture, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
    with pytest.raises(ValueError, match="capture file identity"):
        storage.load(capture, root)


def test_indirect_runtime_names_do_not_change_capture_identity_and_navigation_is_refreshed(bundle):
    capture, document, data, root = bundle
    storage.save(capture, document, data, root)
    loaded = storage.load(capture, root)
    current = deepcopy(data)
    current["captureSessionId"] = "b" * 32
    current["actions"][-1].update(name="[0] arg0: IndirectDraw(<0, 1>)", hasWork=False)
    current["actions"][-2].update(name="[0] arg0: IndirectDraw(<4432, 1>)", hasWork=True)
    restored = storage.rebind(loaded, current)
    assert restored["document"]["stages"][0]["navigationEventId"] == current["actions"][-2]["eventId"]
    assert restored["document"]["stages"][0]["navigationHasWork"] is True
    current["actions"][-1]["flags"] = 4
    with pytest.raises(ValueError, match="action tree"):
        storage.rebind(loaded, current)


def test_corrupt_media_fails_without_publishing_a_new_session(bundle):
    capture, document, data, root = bundle
    storage.save(capture, document, data, root)
    next(storage.sidecar_directory(capture).glob("media/*.png")).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="media content"):
        storage.load(capture, root)


def test_bundle_paths_cannot_escape_its_directory(bundle):
    capture, document, data, root = bundle
    storage.save(capture, document, data, root)
    folder = storage.sidecar_directory(capture)
    pointer = json.loads((folder / "current.json").read_text())
    payload = json.loads((folder / (pointer["documentSha256"] + ".json")).read_text())
    payload["stages"][0]["images"][0]["path"] = "../outside.png"
    encoded = storage.encoded(payload)
    sha = hashlib.sha256(encoded).hexdigest()
    (folder / (sha + ".json")).write_bytes(encoded)
    pointer["documentSha256"] = sha
    (folder / "current.json").write_bytes(storage.encoded(pointer))
    with pytest.raises(ValueError, match="escapes"):
        storage.load(capture, root)


def test_media_copy_failure_leaves_the_last_complete_pointer_unchanged(bundle):
    capture, document, data, root = bundle
    storage.save(capture, document, data, root)
    pointer = storage.sidecar_directory(capture) / "current.json"
    before = pointer.read_bytes()
    changed = deepcopy(document)
    changed["title"] = "A new title"
    changed["stages"][0]["images"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="content changed"):
        storage.save(capture, changed, data, root)
    assert pointer.read_bytes() == before
    assert storage.load(capture, root)["document"]["title"] == document["title"]


def test_unwritable_rdc_directory_uses_a_capture_scoped_local_fallback(bundle, monkeypatch):
    capture, document, data, root = bundle
    original = storage._save_at
    def save_at(folder, *args):
        if folder == storage.sidecar_directory(capture):
            raise PermissionError("Read-only capture drive")
        return original(folder, *args)
    monkeypatch.setattr(storage, "_save_at", save_at)
    receipt = storage.save(capture, document, data, root)
    assert receipt["saved"] and not receipt["portable"]
    assert "Read-only" in receipt["sidecarError"]
    assert storage.load(capture, root)["document"]["title"] == document["title"]


def test_repeated_media_is_verified_once_and_uses_unique_byte_budget(bundle, monkeypatch):
    capture, document, data, root = bundle
    document["stages"][0]["images"] *= 10
    size = Path(document["stages"][0]["images"][0]["path"]).stat().st_size
    monkeypatch.setattr(storage, "MAX_BUNDLE", size + 1)
    receipt = storage.save(capture, document, data, root)
    assert receipt["mediaCount"] == 1
    calls, original = [], storage._bounded
    def bounded(path, limit):
        if Path(path).suffix == ".png":
            calls.append(path)
        return original(path, limit)
    monkeypatch.setattr(storage, "_bounded", bounded)
    restored = storage.load(capture, root)
    assert len(restored["document"]["stages"][0]["images"]) == 10 and len(calls) == 1


def test_animation_gif_and_channel_parent_metadata_survive_portable_storage(bundle, tmp_path):
    capture, document, data, root = bundle
    gif = tmp_path / "comparison.gif"
    gif.write_bytes(b"GIF89a receipt")
    item = document["stages"][0]
    item["images"].append(dict(item["images"][0], group="channels", parentImageIndex=0))
    item["animations"] = [{"label": "Lighting accumulates", "description": "Each pass adds its contribution.",
                            "imageIndices": [0, 1], "frameDurationMs": 400, "gifPath": str(gif),
                            "gifSha256": hashlib.sha256(gif.read_bytes()).hexdigest()}]
    receipt = storage.save(capture, document, data, root)
    assert receipt["mediaCount"] == 2
    restored = storage.load(capture, root)["document"]["stages"][0]
    assert restored["images"][1]["parentImageIndex"] == 0
    assert Path(restored["animations"][0]["gifPath"]).read_bytes() == b"GIF89a receipt"


def test_fingerprint_reads_bounded_bytes_from_a_large_capture(bundle, monkeypatch):
    capture, document, data, root = bundle
    with capture.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024)
    read_sizes, original = [], Path.open
    class Reader:
        def __init__(self, inner): self.inner = inner
        def __enter__(self): return self
        def __exit__(self, *args): self.inner.close()
        def seek(self, offset): return self.inner.seek(offset)
        def read(self, count):
            read_sizes.append(count)
            return self.inner.read(count)
    monkeypatch.setattr(Path, "open", lambda path, *args, **kwargs: Reader(original(path, *args, **kwargs)))
    result = storage.capture_fingerprint(capture)
    assert result["byteSize"] == 64 * 1024 * 1024
    assert sum(read_sizes) <= storage.SAMPLE_BYTES * storage.SAMPLE_COUNT


def test_legacy_migration_prefers_user_complete_diagram_to_newer_partial_probe(bundle):
    capture, document, data, root = bundle
    folder = root / "frame-render-flow" / data["captureSessionId"]
    folder.mkdir(parents=True)
    (folder / (storage.digest(data) + ".outline.json")).write_bytes(storage.encoded(data))
    for count, complete in enumerate((True, False)):
        candidate = deepcopy(document)
        candidate["coverage"]["complete"] = complete
        payload = storage.encoded(candidate)
        path = folder / (hashlib.sha256(payload).hexdigest() + ".json")
        path.write_bytes(payload)
        os.utime(path, ns=(capture.stat().st_mtime_ns, capture.stat().st_mtime_ns + (count + 1) * 1000000000))
    candidates = storage.legacy_candidates(capture, root)
    assert len(candidates) == 2 and candidates[0]["document"]["coverage"]["complete"]
    current = deepcopy(data)
    current["captureSessionId"] = "b" * 32
    assert storage.rebind(candidates[0], current)["document"]["captureSessionId"] == "b" * 32


def test_bridge_uses_python36_compatible_syntax_and_host_outline_contract(bundle):
    ast.parse(SOURCE.read_text(encoding="utf-8"), feature_version=(3, 6))
    assert storage.canonical_outline(bundle[2]) == validate_outline(bundle[2])


def _copy_outline(data):
    data = deepcopy(data)
    data["actions"][-1].update(
        actionKind="copy", isAction=True, hasWork=True, flags=0x1000,
        copySource=17, copyDestination=19,
        copySourceSubresource={"mip": 2, "slice": 3, "sample": 0},
        copyDestinationSubresource={"mip": 1, "slice": 4, "sample": 0})
    return data


def test_bridge_and_host_snapshot_include_copy_view_coordinates_and_defaults(bundle):
    data = _copy_outline(bundle[2])
    data["actions"][0] = {"eventId": 1, "executionIndex": 0}
    data["actions"][-1]["copySourceSubresource"]["diagnostic"] = "not a snapshot field"
    data["actions"][-1]["name"] = "  CopySubresource  "
    data["actions"][-1]["actionKind"] = " copy "
    expected = validate_outline(data)
    clean = storage.canonical_outline(data)
    assert clean == expected
    assert storage.digest(clean) == FrameRenderFlowStore().prepare(data)["snapshotId"]
    clean["actions"][-1]["copySourceSubresource"]["mip"] = 9
    assert data["actions"][-1]["copySourceSubresource"]["mip"] == 2


def test_switch_capture_a_b_a_and_reopen_keep_distinct_copy_snapshots(bundle, tmp_path):
    capture_a, unused_document, data, root = bundle
    capture_b = tmp_path / "captures" / "different.rdc"
    capture_b.write_bytes(b"different frame contents")
    store = FrameRenderFlowStore()
    records, saved = {}, {}
    for path, session, destination, title in (
            (capture_a, "a" * 32, 19, "First frame"),
            (capture_b, "b" * 32, 51, "Second frame")):
        current = _copy_outline(data)
        current.update(capturePath=str(path), captureSessionId=session)
        current["actions"][-1]["copyDestination"] = destination
        record = store.prepare(current)
        document = build_document(record["outline"], record["flowId"], record["snapshotId"],
                                  [stage(1, 7)], title=title)
        store.publish(record["flowId"], document)
        assert storage.save(path, document, record["outline"], root)["saved"]
        records[path] = record
        saved[path] = storage.load(path, root)
    assert records[capture_a]["snapshotId"] != records[capture_b]["snapshotId"]
    with pytest.raises(ValueError, match="Capture session changed"):
        store.get("b" * 32, records[capture_a]["flowId"])
    with pytest.raises(ValueError, match="action tree"):
        storage.rebind(saved[capture_a], records[capture_b]["outline"])

    # A -> B -> A, then close/reopen A again: each controller has a new
    # session but admits only A's recorded content and copy subresources.
    for session in ("c" * 32, "d" * 32):
        current = deepcopy(records[capture_a]["outline"])
        current["captureSessionId"] = session
        restored = storage.rebind(storage.load(capture_a, root), current)
        record = store.prepare(current)
        assert restored["document"]["title"] == "First frame"
        assert restored["document"]["snapshotId"] == record["snapshotId"]
        assert restored["document"]["flowId"] == record["flowId"]
        assert restored["outline"]["actions"][-1]["copyDestinationSubresource"]["slice"] == 4
        assert storage.save(capture_a, restored["document"], restored["outline"], root)["saved"]
    assert storage.load(capture_b, root)["document"]["title"] == "Second frame"


@pytest.mark.parametrize("field", ["mip", "slice", "sample"])
def test_recorded_copy_subresource_change_cannot_rebind(bundle, field):
    capture, unused_document, data, root = bundle
    record = FrameRenderFlowStore().prepare(_copy_outline(data))
    document = build_document(record["outline"], record["flowId"], record["snapshotId"], [stage(1, 7)])
    storage.save(capture, document, record["outline"], root)
    current = deepcopy(record["outline"])
    current["captureSessionId"] = "b" * 32
    current["actions"][-1]["copySourceSubresource"][field] += 1
    with pytest.raises(ValueError, match="action tree"):
        storage.rebind(storage.load(capture, root), current)


def test_legacy_outline_can_acquire_copy_coordinates_without_ignoring_recorded_identity(bundle):
    capture, unused_document, data, root = bundle
    current = validate_outline(_copy_outline(data))
    old = deepcopy(current)
    old["actions"][-1].pop("copyDestinationSubresource")
    record = FrameRenderFlowStore().prepare(old)
    document = build_document(old, record["flowId"], record["snapshotId"], [stage(1, 7)])
    storage.save(capture, document, old, root)
    current["captureSessionId"] = "b" * 32
    loaded = storage.load(capture, root)
    restored = storage.rebind(loaded, current)
    assert restored["outline"]["actions"][-1]["copyDestinationSubresource"] == {
        "mip": 1, "slice": 4, "sample": 0}
    assert restored["document"]["snapshotId"] == FrameRenderFlowStore().prepare(current)["snapshotId"]
    current["actions"][-1]["copySourceSubresource"]["mip"] += 1
    with pytest.raises(ValueError, match="action tree"):
        storage.rebind(loaded, current)
    current["actions"][-1]["copySourceSubresource"]["mip"] -= 1
    current["actions"][-1]["copyDestination"] += 1
    with pytest.raises(ValueError, match="action tree"):
        storage.rebind(loaded, current)


def _restore_scheduler(monkeypatch, bundle):
    capture, document, data, root = bundle
    storage.save(capture, document, data, root)
    loaded = storage.load(capture, root)
    current = deepcopy(data)
    current["captureSessionId"] = "b" * 32
    calls, jobs = [], []
    package = ModuleType("_frame_flow_lifecycle_test")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, package.__name__ + ".frame_flow_persistence", storage)
    executor = SimpleNamespace(cancel_owner=lambda owner: calls.append(("cancel", owner)),
                               submit=lambda owner, worker, callback: jobs.append((worker, callback)))
    replay = SimpleNamespace(BlockInvoke=lambda callback: (calls.append(("outline",)), callback(None)))
    panel = SimpleNamespace(set_document=lambda value: calls.append(("document", value)),
                            show=lambda: calls.append(("show",)))
    ctx = SimpleNamespace(IsCaptureLoaded=lambda: True, Replay=lambda: replay)
    namespace = {"__package__": package.__name__, "HAS_UI": True, "_ctx": ctx,
        "_capture_session_id": "b" * 32, "_frame_render_flow_generation": 1,
        "_frame_render_flow_saved": None, "_frame_render_flow_outline_cache": None,
        "_capture_identity_on_ui": lambda: {"path": str(capture), "local": True},
        "_ensure_project_async_executor": lambda: executor, "_frame_flow_artifact_root": lambda: root,
        "_collect_frame_render_flow_outline": lambda ctrl, identity, session: current,
        "_ensure_frame_render_flow_ui": lambda: panel,
        "_publish_console_event": lambda *args, **kwargs: calls.append(("error", args)),
        "QtCore": SimpleNamespace(QCoreApplication=SimpleNamespace(translate=lambda context, text: text))}
    source = SOURCE.with_name("__init__.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "_schedule_frame_render_flow_restore"]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace, calls, jobs


def test_capture_loaded_only_queues_work_and_restores_window_after_verification(bundle, monkeypatch):
    namespace, calls, jobs = _restore_scheduler(monkeypatch, bundle)
    namespace["_schedule_frame_render_flow_restore"]()
    assert calls == [("cancel", "frame-flow-restore")]
    assert namespace["_frame_render_flow_saved"] is None
    worker, callback = jobs[0]
    result = worker()
    assert calls[-1] == ("outline",) and not any(call[0] == "show" for call in calls)
    callback(result, "")
    assert calls[-1] == ("show",)
    assert namespace["_frame_render_flow_saved"]["document"]["captureSessionId"] == "b" * 32


@pytest.mark.parametrize("change", ["capture", "publication", "closed"])
def test_late_restore_cannot_reopen_old_capture_or_overwrite_new_publication(bundle, monkeypatch, change):
    namespace, calls, jobs = _restore_scheduler(monkeypatch, bundle)
    namespace["_schedule_frame_render_flow_restore"]()
    worker, callback = jobs[0]
    result = worker()
    if change == "capture":
        namespace["_capture_session_id"] = "c" * 32
    elif change == "publication":
        namespace["_frame_render_flow_generation"] += 1
    else:
        namespace["_ctx"] = None
    callback(result, "")
    assert namespace["_frame_render_flow_saved"] is None
    assert not any(call[0] == "show" for call in calls)
