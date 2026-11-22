"""Physical evidence from the November external task logs, without live IPC."""
import copy
import pytest

from src.pass_classification import classify_passes
from src.shader_workspace import AbiValidationError, compare_shader_abi, parse_shader_abi
from tests.test_shader_abi import complete_interface
from src import projects
from src.symbols.scanner import interface_is_complete


@pytest.mark.parametrize("api,instanced", [("D3D11", False), ("D3D12", True),
                                         ("Vulkan", True), ("OpenGL", False)])
@pytest.mark.parametrize("indices,instances", [(83982, 0), (83982, 1), (0, 1)])
@pytest.mark.parametrize("symbolic", [False, True])
def test_draw_participation_uses_instanced_flag_not_display_name(api, instanced, indices, instances, symbolic):
    flags = {"flagNames": ["Drawcall"] + (["Instanced"] if instanced else [])} if symbolic else {
        "flags": 2 | (0x020000 if instanced else 0)}
    result = classify_passes([dict(eventId=882, name="localized opaque name", numIndices=indices,
                                   numInstances=instances, **flags)],
                             pipeline_states={882: {"colorWriteTargets": [{"resourceId": 10}]}})
    row = result["items"][0]
    expected = indices > 0 and (not instanced or instances > 0)
    assert row["participation"]["nonZeroWorkload"] is expected, api
    assert row["participation"]["observedContributor"] is False
    assert row["workload"]["instanceCount"] == (instances if instanced else 1)


@pytest.mark.parametrize("mask", [0, 3, 12, 15])
def test_dxbc_output_never_written_mask_roundtrips_without_changing_physical_abi(mask):
    value = complete_interface("vertex")
    value["outputSignature"][0].update(compCount=2, regChannelMask=3, channelUsedMask=mask)
    original = copy.deepcopy(value)
    baseline = parse_shader_abi(value)
    candidate = parse_shader_abi(baseline.to_dict())
    assert baseline.outputs[0].used_mask == mask
    assert baseline.sha256 == candidate.sha256
    changed_usage = copy.deepcopy(value)
    changed_usage["outputSignature"][0]["channelUsedMask"] = 0
    assert compare_shader_abi(baseline, parse_shader_abi(changed_usage)).compatible
    changed_register = copy.deepcopy(value)
    changed_register["outputSignature"][0].update(regChannelMask=12)
    assert not compare_shader_abi(baseline, parse_shader_abi(changed_register)).compatible
    assert value == original


def test_input_mask_validation_and_output_component_count_still_fail_closed():
    value = complete_interface()
    value["inputSignature"][0]["channelUsedMask"] = 12
    with pytest.raises(AbiValidationError, match="outside its register mask"):
        parse_shader_abi(value)
    value = complete_interface()
    value["outputSignature"][0].update(regChannelMask=3, compCount=4, channelUsedMask=12)
    with pytest.raises(AbiValidationError, match="component count"):
        parse_shader_abi(value)


@pytest.mark.parametrize("mask", [0, 3, 12, 15])
def test_symbol_catalog_and_workspace_agree_on_dxbc_output_masks(mask):
    value = complete_interface("vertex")
    value["constantBuffers"] = value["constantBlocks"]
    value["outputSignature"][0].update(compCount=2, regChannelMask=3, channelUsedMask=mask)
    assert interface_is_complete(value)
    assert parse_shader_abi(value).outputs[0].used_mask == mask
    value["inputSignature"][0]["channelUsedMask"] = 12
    assert not interface_is_complete(value)


def test_project_artifact_near_component_limit_uses_short_atomic_staging(tmp_path):
    target = tmp_path / ("a" * 230 + ".json")
    projects.atomic_write_json(target, {"saved": "中文项目"})
    assert projects.read_json_object(target, 1024, "project") == {"saved": "中文项目"}
    projects.atomic_write_bytes(target, b"second version")
    assert target.read_bytes() == b"second version"
    assert list(tmp_path.iterdir()) == [target]
