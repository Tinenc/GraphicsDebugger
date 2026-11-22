"""Display freshness must not depend on compiling or replaying a candidate."""
import ast
import hashlib
import importlib.util
from pathlib import Path
import re
import sys
import types
from types import SimpleNamespace

import pytest


BRIDGE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge"
ORIGINAL = hashlib.sha256(b"original").hexdigest()
REPLACEMENT = hashlib.sha256(b"replacement").hexdigest()


def cache_module():
    spec = importlib.util.spec_from_file_location("test_cbuffer_display_cache", BRIDGE / "cbuffer_display_identity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def identity(**extra):
    return dict(captureSessionId="a" * 32, shaderResourceId=17, rawSha256=ORIGINAL,
                stage="fragment", entryPoint="main", **extra)


@pytest.mark.parametrize("replacement", ["", REPLACEMENT])
def test_cached_observation_only_survives_same_capture_owner_and_mutation_epoch(replacement):
    cache = cache_module()
    value = identity(activeDisplayShaderRawSha256=replacement or ORIGINAL,
                     activeReplacementShaderRawSha256=replacement)
    cache.remember(value, "capture-a", 5)
    current = cache.observe(identity(), "capture-a", 5, replacement)
    assert current["status"] == "current"
    assert current["activeDisplayShaderRawSha256"] == (replacement or ORIGINAL)
    assert current["replayRead"] is current["replayMutation"] is False
    for token, epoch, owner in [("capture-b", 5, replacement), ("capture-a", 6, replacement),
                                ("capture-a", 5, "c" * 64)]:
        stale = cache.observe(identity(), token, epoch, owner)
        assert stale["status"] == "refresh-required"
        assert stale["refreshTool"] == "publish_cbuffer_aliases"
    changed_entry = identity()
    changed_entry["entryPoint"] = "other"
    assert cache.observe(changed_entry, "capture-a", 5, replacement)["status"] == "refresh-required"


def test_cache_is_bounded_and_rejects_unproven_display_owner():
    cache = cache_module()
    cache.remember(identity(activeDisplayShaderRawSha256=REPLACEMENT,
                            activeReplacementShaderRawSha256=""), "capture-a", 1)
    assert cache.observe(identity(), "capture-a", 1, "")["status"] == "refresh-required"
    for index in range(300):
        value = identity(activeDisplayShaderRawSha256=ORIGINAL, activeReplacementShaderRawSha256="")
        value["shaderResourceId"] = index
        cache.remember(value, "capture-a", 1)
    assert len(cache._observations) == 256


def bridge_handler(monkeypatch, *, replacement=False, api="Vulkan"):
    from test_cbuffer_alias_identity import _wrapper
    namespace, params, unused_restored, unused_checks = _wrapper(monkeypatch, owned=replacement)
    record = namespace["_record"]
    record.update(captureSessionId="a" * 32, captureToken="token-a", expectedOriginalSha256=ORIGINAL,
                  candidateRawSha256=REPLACEMENT, entryPoint="main")
    namespace["_capture_session_id"] = "a" * 32
    namespace["_current_capture_token"] = lambda: "token-a"
    original = namespace["_original"]
    original.rawBytes, original.entryPoint = b"original", "main"
    candidate = namespace["_controller"].candidate
    candidate.rawBytes, candidate.entryPoint = b"replacement", "main"
    pipeline = namespace["_pipeline"]
    pipeline.raw, pipeline.entry, pipeline.api = (b"replacement" if replacement else b"original"), "main", api
    pipeline.selected = 27 if replacement else 17
    pipeline.GetShaderReflection = lambda stage: SimpleNamespace(rawBytes=pipeline.raw)
    params.update(expected_capture_session_id="a" * 32, expected_original_sha256=ORIGINAL, entry_point="main")
    def forbidden(*args, **kwargs):
        raise AssertionError("Display freshness must never perform replay, change selection, or write resources")
    namespace["_replay"] = forbidden
    cache = sys.modules[namespace["__name__"] + ".cbuffer_display_identity"]
    return namespace, params, pipeline, cache


@pytest.mark.parametrize("api", ["D3D11", "D3D12", "Vulkan", "OpenGL"])
@pytest.mark.parametrize("replacement", [False, True])
def test_bridge_reads_loaded_ui_snapshot_across_apis_without_any_replay(monkeypatch, api, replacement):
    namespace, params, pipeline, cache = bridge_handler(monkeypatch, replacement=replacement, api=api)
    result = namespace["_get_shader_cbuffer_display_identity"](params)
    assert result["status"] == "current"
    assert result["activeDisplayShaderRawSha256"] == (REPLACEMENT if replacement else ORIGINAL)
    # Merely compiling another candidate is not even an input to this path.
    namespace["latestCompiled"] = {"rawSha256": "c" * 64}
    pipeline.selected = 99
    assert namespace["_get_shader_cbuffer_display_identity"](params)["status"] == "current"
    namespace["_shared_shader_mutation_epoch"] += 1
    assert namespace["_get_shader_cbuffer_display_identity"](params)["status"] == "refresh-required"
    # Selecting a matching live display recovers without recompiling or replay.
    pipeline.selected = 17
    assert namespace["_get_shader_cbuffer_display_identity"](params)["status"] == "current"


def test_true_reflection_conflict_is_not_treated_as_a_valid_original(monkeypatch):
    namespace, params, pipeline, _ = bridge_handler(monkeypatch)
    pipeline.raw = b"foreign"
    with pytest.raises(RuntimeError, match="different Shader"):
        namespace["_get_shader_cbuffer_display_identity"](params)


def test_different_entry_point_cannot_refresh_the_requested_layout(monkeypatch):
    namespace, params, pipeline, _ = bridge_handler(monkeypatch)
    pipeline.entry = "differentEntry"
    assert namespace["_get_shader_cbuffer_display_identity"](params)["status"] == "refresh-required"


def test_owner_from_another_capture_token_is_rejected(monkeypatch):
    namespace, params, _, _ = bridge_handler(monkeypatch, replacement=True)
    namespace["_shared_shader_replacements"][17]["captureToken"] = "token-old"
    with pytest.raises(RuntimeError, match="different Shader"):
        namespace["_get_shader_cbuffer_display_identity"](params)
