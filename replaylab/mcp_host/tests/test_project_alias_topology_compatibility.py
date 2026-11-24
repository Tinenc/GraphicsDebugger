"""Old/fresh catalog topology spelling must not weaken physical alias binding."""
import copy

import pytest

from src import project_alias_rebind as rebind
from src import projects
from src.symbols.scanner import interface_hash


def stable_key(target):
    return projects._stable_shader_identity_sha256({
        key: target[key] for key in (
            "api", "encoding", "rawSha256", "stage", "entryPoint", "layoutHash", "abiHash"
        )
    })


def pair(topology):
    source = {
        "_projectApi": "D3D11", "encoding": "DXBC", "rawSha256": "1" * 64,
        "stage": "fragment", "entryPoint": "main", "layoutHash": "2" * 64,
        "inputSignature": [], "outputSignature": [], "constantBuffers": [],
        "readOnlyResources": [], "readWriteResources": [], "samplers": [],
        "threadGroupSize": [], "dynamicLinkageInterfaceCount": 0,
        "outputTopology": topology, "interfaceComplete": True,
    }
    source["interfaceHash"] = interface_hash(source)
    fresh = dict(source, outputTopology="Unknown" if topology == "" else "")
    target = {
        "api": "d3d11", "encoding": "dxbc", "rawSha256": source["rawSha256"],
        "stage": "fragment", "entryPoint": "main", "layoutHash": source["layoutHash"],
        "abiHash": rebind._source_shader_abi_sha256(fresh),
        "interfaceHash": interface_hash(fresh), "interfaceComplete": True,
    }
    target["stableKeySha256"] = stable_key(target)
    return source, target


@pytest.mark.parametrize("topology", ["", "Unknown"])
def test_alias_old_and_fresh_topology_keep_original_evidence(topology):
    source, target = pair(topology)
    before = copy.deepcopy((source, target))
    rebind._ensure_inventory_shader_identity(source, target)
    assert (source, target) == before


@pytest.mark.parametrize("field,value", [
    ("api", "d3d12"), ("rawSha256", "3" * 64), ("entryPoint", "other"),
    ("stage", "vertex"), ("layoutHash", "4" * 64), ("abiHash", "5" * 64),
    ("interfaceHash", "6" * 64), ("interfaceComplete", False),
])
def test_alias_topology_fallback_still_rejects_changed_physical_facts(field, value):
    source, target = pair("")
    target[field] = value
    target["stableKeySha256"] = stable_key(target)
    with pytest.raises(projects.ReplayProjectError):
        rebind._ensure_inventory_shader_identity(source, target)


@pytest.mark.parametrize("topology", ["TriangleList", "PointList"])
def test_alias_does_not_normalize_real_topology(topology):
    source, target = pair("")
    source["outputTopology"] = topology
    source["interfaceHash"] = interface_hash(source)
    with pytest.raises(projects.ReplayProjectError):
        rebind._ensure_inventory_shader_identity(source, target)


def test_alias_requires_verified_source_interface_hash():
    source, target = pair("")
    source["interfaceHash"] = "7" * 64
    with pytest.raises(projects.ReplayProjectError):
        rebind._ensure_inventory_shader_identity(source, target)


def test_alias_requires_internally_consistent_target_key():
    source, target = pair("")
    target["stableKeySha256"] = "8" * 64
    with pytest.raises(projects.ReplayProjectError, match="internally inconsistent"):
        rebind._ensure_inventory_shader_identity(source, target)
