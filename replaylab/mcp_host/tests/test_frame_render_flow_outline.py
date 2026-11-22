from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/frame_render_flow_outline.py"
spec = spec_from_file_location("frame_render_flow_outline", SOURCE)
outline = module_from_spec(spec)
spec.loader.exec_module(outline)


def action(eid, flags=2, children=(), outputs=()):
    return SimpleNamespace(eventId=eid, flags=flags, children=list(children),
                           outputs=list(outputs), depthOut=0, name="EID %d" % eid)


def collect(roots, **kwargs):
    return outline.collect_actions(roots, lambda value: value.name,
                                   lambda value: int(value or 0),
                                   {"draw": 2, "dispatch": 4, "copy": 8, "marker": 16}, **kwargs)


def test_outline_preserves_order_parent_and_64_bit_binding_without_pipeline_access():
    root = action(1, 16, [action(5, outputs=[0, 2**50]), action(9, 4)])
    rows, total = collect([root, action(12, 8)])
    assert total == 4
    assert [row["eventId"] for row in rows] == [1, 5, 9, 12]
    assert [row["parentEventId"] for row in rows] == [0, 1, 1, 0]
    assert [row["actionKind"] for row in rows] == ["marker", "draw", "dispatch", "copy"]
    assert rows[1]["outputs"] == [2**50]
    assert rows[0]["isAction"] is False


def test_deep_action_trees_do_not_recurse_or_hide_omitted_tail():
    root = action(1501)
    for eid in reversed(range(1, 1501)):
        root = action(eid, 16, [root])
    checkpoints = []
    rows, total = collect([root], maximum=20, checkpoint=lambda: checkpoints.append(True))
    assert len(rows) == 20
    assert total == 1501
    assert len(checkpoints) >= 5
    assert rows[-1]["parentEventId"] == 19


def test_copy_view_mapping_survives_native_outline_and_host_normalization():
    from src.frame_render_flow import validate_outline
    copy = action(31, 8)
    copy.copySource, copy.copyDestination = 90, 91
    copy.copySourceSubresource = SimpleNamespace(mip=2, slice=3, sample=0)
    copy.copyDestinationSubresource = SimpleNamespace(mip=1, slice=4, sample=0)
    rows, total = collect([copy])
    raw = {"schema": "bread-frame-render-outline/v1", "captureSessionId": "a" * 32,
           "actions": rows, "summary": {"complete": True, "totalActionCount": total, "omittedActionCount": 0}}
    # Use the public constant so the test focuses on native-to-Host evidence.
    from src.frame_render_flow import OUTLINE_SCHEMA
    raw["schema"] = OUTLINE_SCHEMA
    stored = validate_outline(raw)["actions"][0]
    assert stored["copySourceSubresource"] == {"mip": 2, "slice": 3, "sample": 0}
    assert stored["copyDestinationSubresource"] == {"mip": 1, "slice": 4, "sample": 0}
    raw["actions"][0]["copySourceSubresource"].pop("mip")
    with pytest.raises(ValueError):
        validate_outline(raw)


def test_pages_cover_complete_snapshot_and_distinguish_source_coverage_from_page():
    rows, total = collect([action(eid) for eid in range(1, 12)])
    snapshot = {"actions": rows, "captureSessionId": "a"*32,
                "summary": {"complete": True, "totalActionCount": total,
                            "storedActionCount": total, "omittedActionCount": 0}}
    collected = []
    offset = 0
    while True:
        page = outline.outline_page(snapshot, offset=offset, limit=3, cache_hit=offset > 0)
        assert page["summary"]["complete"]
        collected.extend(page["actions"])
        if not page["hasMore"]:
            break
        offset = page["nextOffset"]
    assert collected == rows
    assert "cacheHit" not in snapshot["summary"]
    assert page["nextOffset"] == 11


@pytest.mark.parametrize("offset,limit", [(True, 2), (-1, 2), (0, 0), (0, 5001), (0, 1.2)])
def test_page_rejects_invalid_limits(offset, limit):
    with pytest.raises(ValueError):
        outline.outline_page({"actions": [], "summary": {}}, offset, limit)


@pytest.mark.parametrize("instanced", [False, True])
@pytest.mark.parametrize("indices,instances", [(3, 1), (0, 1), (3, 0), (0, 0)])
def test_draw_work_uses_native_counts_even_when_display_name_says_otherwise(indices, instances, instanced):
    expected = indices > 0 and (not instanced or instances > 0)
    draw = action(5, 2 | (0x020000 if instanced else 0))
    draw.name = "Draw(0, 0)" if expected else "Draw(999, 999)"
    draw.numIndices, draw.numInstances = indices, instances
    rows, unused = collect([draw])
    assert rows[0]["hasWork"] is expected


@pytest.mark.parametrize("dimensions,expected", [((2, 1, 3), True), ((0, 1, 1), False), ((1, 0, 1), False), ((1, 1, 0), False)])
@pytest.mark.parametrize("mesh", [False, True])
def test_dispatch_and_mesh_work_uses_native_dimensions(dimensions, expected, mesh):
    draw = action(5, flags=32 if mesh else 4)
    draw.name = "Dispatch(<999,999,999>)" if not expected else "Dispatch(<0,0,0>)"
    draw.dispatchDimension = dimensions
    draw.numIndices, draw.numInstances = 0, 0  # Mesh work is not an indexed draw count.
    rows, unused = outline.collect_actions([draw], lambda value: value.name, resource_id=lambda value: int(value or 0),
        masks={"draw": 2 | 32, "mesh": 32, "dispatch": 4, "marker": 16})
    assert rows[0]["actionKind"] == ("draw" if mesh else "dispatch")
    assert rows[0]["hasWork"] is expected


@pytest.mark.parametrize("flags,name", [(2, "Draw(0,0)"), (4, "IndirectDispatch(<0,1,1>)")])
def test_unknown_native_work_fields_are_not_inferred_from_formatted_names(flags, name):
    draw = action(5, flags)
    draw.name = name
    rows, unused = collect([draw])
    assert rows[0]["hasWork"] is None


def test_compute_outline_does_not_publish_inherited_rtv_or_dsv_as_outputs():
    dispatch = action(5, flags=4, outputs=[101, 102])
    dispatch.depthOut = 201
    dispatch.dispatchDimension = (4, 2, 1)
    rows, unused = collect([dispatch])
    assert rows[0]["hasWork"] is True
    assert rows[0]["outputs"] == [] and rows[0]["depthOutput"] == 0


@pytest.mark.parametrize("work", [False, True])
def test_native_decoded_arguments_update_outline_without_using_placeholder_fields(work):
    item = action(62778, flags=2 | 0x080000)
    item.numIndices, item.numInstances = 0, 0
    proof = {"status": "resolved", "nonZeroWorkload": work, "eventId": 62778, "chunkIndex": 123}
    rows, _ = collect([item], indirect_workload=lambda action: proof)
    assert rows[0]["hasWork"] is work
    assert rows[0]["indirectWorkload"] == proof


@pytest.mark.parametrize("kind", ["draw", "dispatch", "mesh"])
@pytest.mark.parametrize("count", [0, 4])
def test_indirect_placeholder_counts_are_unknown_until_arguments_are_proved(kind, count):
    flag = {"draw": 2, "dispatch": 4, "mesh": 32}[kind]
    item = action(62778, flags=flag | 0x080000)
    item.dispatchDimension = (count, 1, 1)
    item.numIndices, item.numInstances = count, 1
    masks = {"draw": 2 | 32, "mesh": 32, "dispatch": 4, "indirect": 0x080000}
    rows, _ = outline.collect_actions([item], lambda value: value.name,
                                     lambda value: int(value or 0), masks)
    assert rows[0]["isAction"] is True
    assert rows[0]["hasWork"] is None
    item.indirectArgumentsResolved = True
    rows, _ = outline.collect_actions([item], lambda value: value.name,
                                     lambda value: int(value or 0), masks)
    assert rows[0]["hasWork"] is (count > 0)
