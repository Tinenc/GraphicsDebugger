"""Preview session behavior without relying on a user's open capture."""
import copy
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


PATH = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "shader_visual_debug.py"
spec = importlib.util.spec_from_file_location("visual_debug_session_test_module", PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_display_snapshot_owns_nested_layout_without_copying_shader_reflection():
    def forbidden(*args):
        raise AssertionError("ShaderReflection construction is intentionally disabled")
    bridge = SimpleNamespace(rd=SimpleNamespace(ShaderReflection=forbidden,
                             ConstantBlock=copy.deepcopy),
                             _shader_raw_bytes=lambda reflection: reflection.rawBytes)
    source = SimpleNamespace(rawBytes=bytearray(b"original"),
                             constantBlocks=[SimpleNamespace(members=[1, 2])])
    owned = mod._display_reflection_snapshot(bridge, source)
    source.rawBytes[:] = b"replaced"
    source.constantBlocks[0].members.append(3)
    assert owned.rawBytes == b"original"
    assert owned.constantBlocks[0].members == [1, 2]


class Instrumenter:
    def __init__(self):
        self.analyses = 0

    def inspect(self, source, start, end, **kwargs):
        self.analyses += 1
        return {"choices": [{"path": "", "type": "float"}],
                "selection": {"byteStart": start, "byteEnd": end, "expression": "x"}}

    def instrument(self, plan, **options):
        return {"source": "generated " + options["mode"], "entryPoint": "main",
                "output": options["target"], "semantics": "observe-at-expression"}

    def prepare_preprocess(self, source, start, end):
        return {"source": source, "byteStart": start, "byteEnd": end}

    def finish_preprocess(self, source, prepared):
        return dict(prepared, source=source)


class Driver:
    def __init__(self):
        self.capture = "capture-a"
        self.event = 13
        self.current = "user-modified-shader"
        self.compiles = 0
        self.applies = []
        self.restores = []
        self.freed = []
        self.during_compile = None
        self.during_apply = None

    def inspect(self, params, previous=None):
        return {"capture": self.capture, "eventId": self.event, "shaderId": "100",
                "entryPoint": "main", "compileIdentity": [self.capture, "shader-a"],
                "restoreState": self.current,
                "targets": [{"index": 0, "name": "R32", "components": 1, "componentType": "float"},
                            {"index": 2, "name": "RGBA32", "components": 4, "componentType": "float"}]}

    def compile(self, generated, facts, params):
        self.compiles += 1
        if self.during_compile:
            self.during_compile()
        return {"target": "preview-%d" % self.compiles, "byteSize": 100}

    def preprocess(self, source, params, facts, snapshot=None):
        return source

    def apply(self, session, previous, is_current):
        if not is_current():
            raise mod.DebugError("superseded", "cancelled")
        self.current = session["cached"]["target"]
        self.applies.append(self.current)
        if self.during_apply:
            self.during_apply()

    def restore(self, session, require_owned):
        self.current = session["restoreState"]
        self.restores.append(self.current)

    def free(self, cached, capture):
        self.freed.append(cached["target"])


@pytest.fixture
def environment(tmp_path):
    source = "// 中文\nfloat x = 0.25;\n"
    start = source.encode().index(b"x")
    params = {"ownerId": "editor", "requestGeneration": 1, "source": source, "scope": "event",
              "sourcePath": str(tmp_path / "shader.hlsl"),
              "sourceSha256": hashlib.sha256(source.encode()).hexdigest(),
              "byteStart": start, "byteEnd": start + 1}
    driver, instrumenter = Driver(), Instrumenter()
    return mod.Manager(driver, instrumenter), driver, instrumenter, params


@pytest.mark.parametrize("existing", [False, True])
def test_failed_post_commit_rollback_pins_candidate_and_stop_recovers_original(environment, monkeypatch, existing):
    manager, driver, _, params = environment
    first = manager.operation("start", params) if existing else None
    def failed_rollback():
        error = mod.DebugError("restore-failed", "native rollback failed after apply")
        error.preview_committed = True
        raise error
    driver.during_apply = failed_rollback
    monkeypatch.setattr(mod, "MAX_CACHE_TARGETS", 0)
    result = manager.operation("start", dict(params, requestGeneration=2, mode="grayscale"))
    assert result["status"] == "error" and result["activePreview"]
    recovery = result["recoverySession"]
    assert recovery["status"] == "active" and recovery["recoveryRequired"]
    if first:
        assert recovery["sessionId"] == first["sessionId"]
    candidate = manager._session["cached"]["target"]
    assert candidate == driver.current and candidate not in driver.freed
    assert len(manager._cache) == (2 if existing else 1)
    driver.during_apply = None
    stopped = manager.operation("stop", dict(params, requestGeneration=3, sessionId=recovery["sessionId"]))
    assert stopped["status"] == "stopped"
    assert driver.current == "user-modified-shader" and candidate in driver.freed
    assert manager._session is None and not manager._cache


def test_failed_comparison_release_retains_active_candidate_for_stop(environment):
    manager, driver, _, params = environment
    first = manager.operation("start", params)
    common = dict(params, sessionId=first["sessionId"])
    assert manager.operation("compare", dict(common, requestGeneration=2, original=True))["status"] == "comparing"
    def failed_rollback():
        error = mod.DebugError("restore-failed", "comparison rollback failed")
        error.preview_committed = True
        raise error
    driver.during_apply = failed_rollback
    failed = manager.operation("compare", dict(common, requestGeneration=3, original=False))
    assert failed["activePreview"] and failed["recoverySession"]["status"] == "active"
    assert manager._session["status"] == "active"
    driver.during_apply = None
    assert manager.operation("stop", dict(common, requestGeneration=4))["status"] == "stopped"
    assert driver.current == "user-modified-shader"


def test_cancel_restores_preexisting_user_version_and_cache_reuses_target(environment):
    manager, driver, instrumenter, params = environment
    result = manager.operation("start", params)
    assert result["status"] == "active" and result["targetIndex"] == 2
    assert not result["cacheHit"]
    stopped = manager.operation("stop", dict(params, requestGeneration=2, sessionId=result["sessionId"]))
    assert stopped["status"] == "stopped"
    assert driver.current == "user-modified-shader"
    result = manager.operation("start", dict(params, requestGeneration=3))
    assert result["cacheHit"] and driver.compiles == 1 and instrumenter.analyses == 1


def test_float_preview_widens_selected_output_and_has_distinct_reusable_cache(environment):
    manager, driver, _, params = environment
    original_compile = driver.compile
    outputs = []
    def record_output(generated, facts, params):
        outputs.append(dict(generated["output"]))
        return original_compile(generated, facts, params)
    driver.compile = record_output
    params["targetIndex"] = 0
    regular = manager.operation("start", params)
    floated = manager.operation("start", dict(params, requestGeneration=2, independentFloatOutput=True))
    reused = manager.operation("start", dict(params, requestGeneration=3))
    assert regular["status"] == floated["status"] == reused["status"] == "active"
    assert outputs[0]["components"] == 1 and outputs[1]["components"] == 4
    assert outputs[1]["componentType"] == "float" and outputs[1]["index"] == 0
    assert floated["independentFloatOutput"] and not regular["independentFloatOutput"]
    assert reused["cacheHit"] and driver.compiles == 2


def test_switch_and_hold_comparison_keep_initial_restore_state(environment):
    manager, driver, _, params = environment
    first = manager.operation("start", params)
    second = manager.operation("start", dict(params, mode="normal", requestGeneration=2))
    assert first["sessionId"] == second["sessionId"]
    comparison = dict(params, sessionId=first["sessionId"], requestGeneration=3, original=True)
    assert manager.operation("compare", comparison)["status"] == "comparing"
    assert driver.current == "user-modified-shader"
    assert manager.operation("compare", dict(comparison, requestGeneration=4, original=False))["status"] == "active"
    assert driver.current == "preview-2"
    manager.operation("stop", dict(params, requestGeneration=5))
    assert driver.current == "user-modified-shader"


def test_cancel_during_compile_prevents_late_apply(environment):
    manager, driver, _, params = environment
    driver.during_compile = lambda: manager.invalidate(dict(params, requestGeneration=2))
    result = manager.operation("start", params)
    assert result["reason"] == "superseded"
    assert not driver.applies and driver.current == "user-modified-shader"
    assert manager.operation("stop", dict(params, requestGeneration=2))["status"] == "stopped"


def test_cancel_during_native_apply_restores_immediately_on_completion(environment):
    manager, driver, _, params = environment
    driver.during_apply = lambda: manager.invalidate(dict(params, requestGeneration=2))
    result = manager.operation("start", params)
    assert result["reason"] == "superseded"
    assert driver.current == "user-modified-shader" and len(driver.restores) == 1


def test_other_owner_cannot_stop_or_compare_session(environment):
    manager, driver, _, params = environment
    result = manager.operation("start", params)
    foreign = dict(params, ownerId="other", sessionId=result["sessionId"])
    assert manager.operation("stop", foreign)["reason"] == "session-changed"
    assert manager.operation("compare", foreign)["reason"] == "session-changed"
    assert driver.current == "preview-1"


def test_new_capture_never_reuses_old_native_ids(environment):
    manager, driver, _, params = environment
    manager.operation("start", params)
    driver.capture = "capture-b"
    driver.current = "capture-b-original"
    result = manager.operation("start", dict(params, requestGeneration=2))
    assert not result["cacheHit"] and driver.compiles == 2
    assert not driver.freed and not driver.restores
    manager.operation("stop", dict(params, requestGeneration=3))
    assert driver.current == "capture-b-original"


def test_stale_source_hash_and_utf8_split_are_rejected(environment):
    manager, _, _, params = environment
    assert manager.operation("start", dict(params, sourceSha256="0" * 64))["reason"] == "stale-source"
    with pytest.raises(UnicodeError):
        mod.source_snapshot(dict(params, byteStart=4, byteEnd=5))


def test_snapshot_preserves_graph_and_unsaved_include_byte_positions(tmp_path):
    root = tmp_path / "shader.hlsl"
    include = tmp_path / "calculation.hlsl"
    root.write_text('#include "calculation.hlsl"\nfloat4 main():SV_Target { return calc(); }')
    include.write_text("float calc(){return 0;}")
    edited = "float calc(){float value=2;return value;}"
    start = edited.index("value")
    snapshot = mod.source_snapshot(dict(source=edited, sourcePath=str(include), rootSourcePath=str(root),
                                        byteStart=start, byteEnd=start + 5))
    assert snapshot["source"] == edited
    assert snapshot["files"]["shader.hlsl"] == root.read_bytes().decode("utf-8")
    assert snapshot["files"]["calculation.hlsl"] == edited
    assert snapshot["rootName"] == "shader.hlsl"
    assert snapshot["editedName"] == "calculation.hlsl"
    assert snapshot["needsPreprocess"] is True
    assert snapshot["source"].encode()[snapshot["byteStart"]:snapshot["byteEnd"]] == b"value"
    assert include.read_text() == "float calc(){return 0;}"


def test_snapshot_retains_inactive_and_repeated_includes_for_real_preprocessor(tmp_path):
    root = tmp_path / "shader.hlsl"
    header = tmp_path / "header.hlsl"
    header.write_text("#pragma once\nfloat included=1;\n")
    source = '#if 0\n#include "header.hlsl"\n#include "missing.hlsl"\n#endif\n#include "header.hlsl"\nfloat x=0;'
    start = source.index("x=")
    snapshot = mod.source_snapshot(dict(source=source, sourcePath=str(root), byteStart=start, byteEnd=start + 1))
    assert snapshot["source"] == source
    assert snapshot["files"]["shader.hlsl"].count('#include "header.hlsl"') == 2
    assert snapshot["files"]["header.hlsl"] == header.read_bytes().decode("utf-8")
    missing = mod._path(str(tmp_path / "missing.hlsl"))
    assert snapshot["dependencies"][missing] == "missing"
    assert "missing.hlsl" not in snapshot["files"]
    (tmp_path / "missing.hlsl").write_text("float optional=2;")
    current = mod.source_snapshot(dict(source=source, sourcePath=str(root), byteStart=start, byteEnd=start + 1))
    assert current["unitSha256"] != snapshot["unitSha256"]


def test_snapshot_preserves_guarded_recursive_include_graph(tmp_path):
    root = tmp_path / "shader.hlsl"
    header = tmp_path / "header.hlsl"
    header.write_text('#ifndef HEADER\n#define HEADER\n#include "shader.hlsl"\n#endif\n')
    source = '#ifndef ROOT\n#define ROOT\n#include "header.hlsl"\nfloat x=1;\n#endif\n'
    start = source.index("x=")
    snapshot = mod.source_snapshot(dict(source=source, sourcePath=str(root), byteStart=start, byteEnd=start + 1))
    assert set(snapshot["files"]) == {"shader.hlsl", "header.hlsl"}
    assert snapshot["files"]["shader.hlsl"] == source
    assert snapshot["files"]["header.hlsl"] == header.read_bytes().decode("utf-8")


def test_snapshot_defers_literal_include_escape_without_reading_it(tmp_path):
    root = tmp_path / "owned" / "shader.hlsl"
    root.parent.mkdir()
    outside = tmp_path / "outside.hlsl"
    outside.write_text("float secret;")
    source = '#include "../outside.hlsl"\nfloat x=0;'
    start = source.index("x=")
    snapshot = mod.source_snapshot(dict(source=source, sourcePath=str(root), byteStart=start, byteEnd=start + 1))
    assert snapshot["blockedIncludes"] == {"shader.hlsl": ["../outside.hlsl"]}
    assert mod._path(str(outside)) not in snapshot["dependencies"]
    assert "secret" not in "".join(snapshot["files"].values())


def test_snapshot_ignores_includes_inside_comments(tmp_path):
    source = '/*\n#include "../outside.hlsl"\n*/\nfloat x=0;'
    start = source.index("x=")
    snapshot = mod.source_snapshot(dict(source=source, sourcePath=str(tmp_path / "shader.hlsl"), byteStart=start, byteEnd=start + 1))
    assert not snapshot["blockedIncludes"] and not snapshot["needsPreprocess"]


def test_include_change_during_compile_cannot_be_applied(environment, tmp_path):
    manager, driver, _, params = environment
    header = tmp_path / "header.hlsl"
    header.write_text("float dependency=1;\n")
    source = '#include "header.hlsl"\nfloat x=0;'
    start = source.index("x=")
    params = dict(params, source=source, sourceSha256="", byteStart=start, byteEnd=start+1)
    driver.during_compile = lambda: header.write_text("float dependency=2;\n")
    result = manager.operation("start", params)
    assert result["reason"] == "stale-source" and not driver.applies


def test_bounded_cache_does_not_free_active_target(environment, monkeypatch):
    manager, driver, _, params = environment
    monkeypatch.setattr(mod, "MAX_CACHE_TARGETS", 2)
    for index, mode in enumerate(("raw", "normal", "coverage", "grayscale")):
        result = manager.operation("start", dict(params, requestGeneration=index + 1, mode=mode))
        assert result["status"] == "active"
    assert len(manager._cache) == 2 and driver.freed == ["preview-1", "preview-2"]
    assert driver.current == "preview-4"


def test_interface_allows_only_selected_color_output_change():
    baseline = {"entryPoint": "main", "outputSignature": [{"regIndex": 0, "systemValue": "colout", "compType": "float", "compCount": 1}]}
    candidate = copy.deepcopy(baseline)
    candidate["outputSignature"][0]["systemValue"] = "coloroutput"
    baseline["outputSignature"][0]["systemValue"] = "coloroutput"
    candidate["outputSignature"][0]["compCount"] = 4
    bridge = SimpleNamespace(_interface_incompatibilities=lambda before, after: [] if before == after else ["unexpected"])
    output = {"index": 0, "components": 4, "componentType": "float"}
    mod._validate_interface(bridge, baseline, candidate, output)
    with pytest.raises(mod.DebugError, match="type"):
        mod._validate_interface(bridge, baseline, candidate, dict(output, componentType="uint"))


def test_checkerboard_allows_only_the_generated_fixed_function_fragment_position():
    baseline = {"entryPoint": "main", "inputSignature": [], "outputSignature": [
        {"regIndex": 0, "systemValue": "coloroutput", "compType": "float", "compCount": 4}]}
    candidate = copy.deepcopy(baseline)
    candidate["inputSignature"] = [{"systemValue": "position", "compType": "float", "compCount": 4}]
    output = {"index": 0, "components": 4, "componentType": "float"}
    bridge = SimpleNamespace(_interface_incompatibilities=lambda before, after: [] if before == after else ["new varying"])
    with pytest.raises(mod.DebugError):
        mod._validate_interface(bridge, baseline, candidate, output)
    mod._validate_interface(bridge, baseline, candidate, output, allow_fragment_position=True)
    candidate["inputSignature"][0]["systemValue"] = "undefined"
    with pytest.raises(mod.DebugError):
        mod._validate_interface(bridge, baseline, candidate, output, allow_fragment_position=True)


def test_changed_preprocessor_flags_invalidate_the_analysis_cache(environment):
    manager, driver, instrumenter, params = environment
    original_inspect = driver.inspect
    definitions = [["@cmdline", "/DVALUE=1"]]
    def inspect_with_flags(params, previous=None):
        facts = original_inspect(params, previous)
        facts["flagValues"] = copy.deepcopy(definitions)
        facts["flagsNeedPreprocess"] = True
        return facts
    driver.inspect = inspect_with_flags
    assert manager.operation("start", params)["status"] == "active"
    definitions[0][1] = "/DVALUE=2"
    assert manager.operation("start", dict(params, requestGeneration=2))["status"] == "active"
    assert instrumenter.analyses == 2 and driver.compiles == 2


def test_preprocessed_expanded_expression_keeps_original_utf8_range_on_cache_hit(environment):
    manager, driver, instrumenter, params = environment
    source = "// 中文\nfloat4 main() : SV_Target { return lerp(a, b, 0.5); }"
    raw = source.encode("utf-8")
    start = raw.index(b"lerp")
    end = start + len(b"lerp(a, b, 0.5)")
    original_inspect = driver.inspect
    def inspect_with_flags(params, previous=None):
        facts = original_inspect(params, previous)
        facts["flagsNeedPreprocess"] = True
        return facts
    driver.inspect = inspect_with_flags
    def prepare(source, begin, finish):
        return {"source": "float helper;\n" + source, "byteStart": begin + 14,
                "byteEnd": end + 14,
                "originalSelection": {"byteStart": start, "byteEnd": end}}
    instrumenter.prepare_preprocess = prepare
    params = dict(params, source=source, sourceSha256="", byteStart=start, byteEnd=start + 4)
    first = manager.operation("inspect", params)
    second = manager.operation("start", dict(params, requestGeneration=2))
    for result in (first, second):
        selection = result["selection"]
        assert (selection["byteStart"], selection["byteEnd"]) == (start, end)
        assert selection["expression"] == "lerp(a, b, 0.5)"
        assert selection["line"] == 2
    assert instrumenter.analyses == 1


def test_shutdown_restores_before_releasing_cached_targets(environment):
    manager, driver, _, params = environment
    assert manager.operation("start", params)["status"] == "active"
    def release(target, capture):
        assert driver.current == "user-modified-shader"
        driver.freed.append(target["target"])
    driver.free = release
    manager.shutdown()
    assert driver.freed == ["preview-1"] and not manager._cache
    assert manager.operation("start", dict(params, requestGeneration=3))["reason"] == "session-closing"


class FollowingDriver(Driver):
    def __init__(self):
        super().__init__()
        self.overrides = {13: "prior-a", 14: "prior-b", 15: "prior-c"}
        self.inspected = []
        self.inspect_failure = None
        self.restore_failure = None
        self.resource = "201"

    def inspect(self, params, previous=None):
        # A prior event's preview must never reach the new event inspection.
        assert not any(str(value).startswith("preview") for event, value in self.overrides.items()
                       if event != self.event)
        self.inspected.append(self.event)
        if self.inspect_failure:
            raise mod.DebugError(self.inspect_failure, "incompatible or failed")
        facts = super().inspect(params, previous)
        facts["restoreState"] = self.overrides[self.event]
        for target in facts["targets"]:
            target["resourceId"] = self.resource
        return facts

    def apply(self, session, previous, is_current):
        super().apply(session, previous, is_current)
        self.overrides[self.event] = self.current

    def restore(self, session, require_owned):
        if self.restore_failure:
            raise mod.DebugError(self.restore_failure, "restore transport failed")
        super().restore(session, require_owned)
        self.overrides[session["facts"]["eventId"]] = session["restoreState"]


def following(environment):
    manager, _, instrumenter, params = environment
    driver = FollowingDriver()
    manager.driver = driver
    return manager, driver, instrumenter, dict(params, eventId=13)


def test_follow_restores_each_draw_and_reuses_shader_on_another_attachment(environment):
    manager, driver, _, params = following(environment)
    first = manager.operation("start", params)
    driver.event, driver.resource = 14, "202"
    second = manager.operation("follow", dict(params, eventId=14, requestGeneration=2,
                                               sessionId=first["sessionId"]))
    assert second["status"] == "active" and second["cacheHit"]
    assert second["sessionId"] != first["sessionId"]
    assert driver.compiles == 1 and driver.overrides[13] == "prior-a"
    assert driver.overrides[14] == "preview-1"
    assert manager.operation("stop", dict(params, requestGeneration=3,
                                            sessionId=second["sessionId"]))["status"] == "stopped"
    assert driver.overrides == {13: "prior-a", 14: "prior-b", 15: "prior-c"}


@pytest.mark.parametrize("reason", ["not-draw", "shader-changed", "reflection-unavailable", "no-color-target"])
def test_follow_pauses_incompatible_event_then_resumes_intent(environment, reason):
    manager, driver, _, params = following(environment)
    manager.operation("start", params)
    driver.event, driver.inspect_failure = 15, reason
    paused = manager.operation("follow", dict(params, eventId=15, requestGeneration=2))
    assert paused["status"] == "paused" and paused["reason"] == reason
    assert paused["sessionId"] == "" and manager._session is None
    assert driver.overrides[13] == "prior-a"
    driver.event, driver.inspect_failure = 13, None
    resumed = manager.operation("follow", dict(params, requestGeneration=3))
    assert resumed["status"] == "active" and resumed["cacheHit"]


def test_follow_inspect_only_returns_choices_without_guessing_struct_member(environment):
    manager, driver, _, params = following(environment)
    manager.operation("start", params)
    driver.event = 14
    ready = manager.operation("follow", dict(params, eventId=14, requestGeneration=2, inspectOnly=True))
    assert ready["status"] == "ready" and ready["choices"]
    assert manager._session is None and len(driver.applies) == 1
    assert driver.overrides[13] == "prior-a" and driver.overrides[14] == "prior-b"


def test_follow_failed_restore_retains_ownership_and_never_inspects_new_draw(environment):
    manager, driver, _, params = following(environment)
    first = manager.operation("start", params)
    driver.event, driver.restore_failure = 14, "restore-failed"
    result = manager.operation("follow", dict(params, eventId=14, requestGeneration=2))
    assert result["status"] == "error" and result["activePreview"]
    assert result["sessionId"] == first["sessionId"]
    assert manager._session and driver.inspected == [13]
    driver.restore_failure = None
    assert manager.operation("stop", dict(params, requestGeneration=3))["status"] == "stopped"
    assert driver.overrides[13] == "prior-a"


def test_follow_restore_transport_error_is_not_masked_by_cache_eviction(environment, monkeypatch):
    manager, driver, _, params = following(environment)
    manager.operation("start", params)
    manager.operation("start", dict(params, requestGeneration=2, mode="normal"))
    driver.event, driver.restore_failure = 14, "restore-failed"
    def cannot_free(*unused):
        raise RuntimeError("peer disconnected during eviction")
    driver.free = cannot_free
    monkeypatch.setattr(mod, "MAX_CACHE_TARGETS", 0)
    result = manager.operation("follow", dict(params, eventId=14, requestGeneration=3))
    assert result["reason"] == "restore-failed" and result["activePreview"]
    assert result["recoverySession"]["recoveryRequired"] and len(manager._cache) == 2


def test_follow_superseded_during_restore_does_not_apply(environment):
    manager, driver, _, params = following(environment)
    manager.operation("start", params)
    original_restore = driver.restore
    def restore(session, require_owned):
        original_restore(session, require_owned)
        manager.invalidate(dict(params, requestGeneration=3))
    driver.restore = restore
    driver.event = 14
    result = manager.operation("follow", dict(params, eventId=14, requestGeneration=2))
    assert result["reason"] == "superseded" and manager._session is None
    assert len(driver.applies) == 1 and driver.overrides[13] == "prior-a"


def test_follow_does_not_steal_another_owner(environment):
    manager, driver, _, params = following(environment)
    first = manager.operation("start", params)
    driver.event = 14
    result = manager.operation("follow", dict(params, eventId=14, requestGeneration=2, ownerId="other"))
    assert result["reason"] == "session-changed"
    assert manager._session["sessionId"] == first["sessionId"] and not driver.restores


def test_rapid_follow_and_stop_accept_latest_generation_with_undrained_receipt(environment):
    manager, driver, _, params = following(environment)
    first = manager.operation("start", params)
    driver.event = 14
    second = manager.operation("follow", dict(params, eventId=14, requestGeneration=2,
                                               sessionId=first["sessionId"]))
    assert second["status"] == "active"
    # B's receipt has not reached Qt; both C and Stop still carry A's ID.
    driver.event = 15
    third = manager.operation("follow", dict(params, eventId=15, requestGeneration=3,
                                              sessionId=first["sessionId"]))
    assert third["status"] == "active" and driver.overrides[14] == "prior-b"
    stopped = manager.operation("stop", dict(params, requestGeneration=4, sessionId=first["sessionId"]))
    assert stopped["status"] == "stopped" and manager._session is None
    assert driver.overrides == {13: "prior-a", 14: "prior-b", 15: "prior-c"}


def test_return_keyword_and_expanded_snapshot_range_share_compiled_cache(environment):
    manager, driver, _, params = environment
    manager.instrumenter = mod._load_instrumenter()
    source = "float4 main():SV_Target { float x=0.25; return x; }"
    first = dict(params, source=source, sourceSha256="", targetIndex=0, byteStart=source.index("return"),
                 byteEnd=source.index("return") + 6)
    active = manager.operation("start", first)
    assert active["status"] == "active", active
    selection = active["selection"]
    second = manager.operation("start", dict(first, requestGeneration=2,
        byteStart=selection["byteStart"], byteEnd=selection["byteEnd"]))
    assert second["status"] == "active" and second["cacheHit"] and driver.compiles == 1


def test_default_preview_display_is_custom(environment):
    manager, driver, _, params = environment
    generated = []
    original = driver.compile
    def compile(source, facts, request):
        generated.append(source["source"])
        return original(source, facts, request)
    driver.compile = compile
    assert manager.operation("start", params)["status"] == "active"
    assert generated == ["generated custom"]


def test_analysis_model_shares_current_source_and_releases_old_versions(environment):
    manager, driver, _, params = environment
    manager.instrumenter = mod._load_instrumenter()
    source = "float4 main():SV_Target { float x=0.25; float y=x*2; return y; }"
    request = dict(params, source=source, sourceSha256="", targetIndex=0,
                   byteStart=source.index("x*2"), byteEnd=source.index("x*2") + 3)
    assert manager.operation("inspect", request)["status"] == "ready"
    model = manager._source_model["model"]
    second = dict(request, requestGeneration=2, byteStart=source.index("return y") + 7,
                  byteEnd=source.index("return y") + 8)
    assert manager.operation("inspect", second)["status"] == "ready"
    assert len(manager._plans) == 2
    assert all(plan["model"] is model for plan in manager._plans.values())
    changed = dict(request, requestGeneration=3, source=source.replace("0.25", "0.75"))
    assert manager.operation("inspect", changed)["status"] == "ready"
    assert len(manager._plans) == 1 and manager._source_model["model"] is not model
    prior_capture_model = manager._source_model["model"]
    driver.capture = "capture-b"
    assert manager.operation("inspect", dict(changed, requestGeneration=4))["status"] == "ready"
    assert len(manager._plans) == 1 and manager._source_model["model"] is not prior_capture_model
    manager.shutdown()
    assert not manager._plans and manager._source_model is None and manager._analysis_source is None

