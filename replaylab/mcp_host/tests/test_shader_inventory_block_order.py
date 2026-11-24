"""Bridge, restore and project inventories share the same CBuffer hash order."""
import copy
import hashlib
from types import SimpleNamespace

import pytest

from src import project_restore, projects
from tests import test_rdc_project_bridge as bridge_tests


@pytest.mark.parametrize("target_only", [False, True])
def test_multiple_cbuffers_keep_bridge_aggregate_hash_through_project_projection(target_only):
    case = bridge_tests.ReplayLabRdcProjectBridgeTests(methodName="runTest")
    case.setUpClass()
    case.setUp()
    try:
        # Native DXIL CBuffer Zoo reflection declares b7 before b1/b2/b3,
        # plus a very large space. Reflection index order is not bind order.
        case.controller.reflection.constantBlocks = [
            bridge_tests._constant_block(name="consts", bind=7),
            bridge_tests._constant_block(name="rootconsts", bind=1),
            bridge_tests._constant_block(name="packed_consts", bind=2),
            bridge_tests._constant_block(name="array_consts", bind=3),
            bridge_tests._constant_block(name="hugespace", bind=0, space=999999999),
        ]

        def call(method, params, timeout=None):
            return getattr(case.bridge, "_" + method)(params)

        inventory = project_restore._inventory_shader_resources(
            SimpleNamespace(ipc=SimpleNamespace(call=call)), case.capture_path,
            case.bridge._capture_session_id, "d3d11",
            SimpleNamespace(emit=lambda *args, **kwargs: None),
            target_raw_sha256s=(
                [hashlib.sha256(b"captured-shader").hexdigest()] if target_only else None))
        projected = project_restore._project_shader_inventory(
            inventory, allow_target_lookup=target_only)
        normalised = projects._normalise_shader_inventory(projected)

        assert normalised["capture"] == projected["capture"]
        assert normalised["capture"]["inventorySha256"] == inventory["capture"]["inventorySha256"]
        assert [block["index"] for block in normalised["shaders"][0]["constantBlocks"]] == [1, 2, 3, 0, 4]
        reordered = copy.deepcopy(projected)
        reordered["shaders"][0]["constantBlocks"].reverse()
        assert projects._normalise_shader_inventory(reordered) == normalised

        # Validation still rejects actual content changes with the old digest.
        tampered = copy.deepcopy(projected)
        tampered["shaders"][0]["constantBlocks"][0]["byteSize"] += 16
        with pytest.raises(projects.ReplayProjectError, match="aggregate hash mismatch"):
            projects._normalise_shader_inventory(tampered)
    finally:
        case.tearDown()
