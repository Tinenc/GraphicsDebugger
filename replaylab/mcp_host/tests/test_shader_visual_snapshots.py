"""Source-location safety and durable value-debug recipes, without Qt/replay."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


MODULE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "shader_visual_snapshots.py"
SPEC = importlib.util.spec_from_file_location("bread_visual_snapshots_test", MODULE)
snapshots = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshots)

IDENTITY = {"workspace": "D:/Projects/角色", "path": "D:/Projects/角色/rim.hlsl",
            "stage": "Pixel", "originalShader": "a" * 64}
SOURCE = """// Rim calculation
struct Surface { float3 normal; };
float3 helper(float3 base, float weight) {
    float3 before = base * weight;
    float3 rim = before + 0.25;
    return rim;
}
float4 main():SV_Target0 { return float4(helper(0.2,0.4),1); }
"""


def make(source=SOURCE, needle="rim;", name="轮廓光", identity=None, **kwargs):
    start = source.index(needle)
    expression = needle[:-1] if needle.endswith(";") else needle
    return snapshots.make_snapshot(source, {"byteStart": len(source[:start].encode("utf-8")),
        "byteEnd": len(source[:start + len(expression)].encode("utf-8"))},
        identity or IDENTITY, name=name, **kwargs)


def resolve(record, source=SOURCE, identity=None):
    return snapshots.resolve_snapshot(record, source, identity or IDENTITY)


def assert_resolves(record, source, expression="rim"):
    result = resolve(record, source)
    assert result["status"] == "resolved", result
    selection = result["selection"]
    assert source.encode("utf-8")[selection["byteStart"]:selection["byteEnd"]].decode("utf-8") == expression
    assert selection["expression"] == expression
    assert selection["line"] == source.encode("utf-8")[:selection["byteStart"]].count(b"\n") + 1
    return result


def test_line_insertions_comments_unicode_and_whitespace_do_not_move_recipe():
    record = make()
    source = "// 新增中文说明\n\n" + SOURCE.replace("return rim;", "// 中文\n    return /*observed*/ rim ;")
    assert_resolves(record, source)


def test_algorithm_before_expression_uses_current_source_location():
    record = make()
    source = SOURCE.replace("base * weight", "normalize(base) * (weight + 0.75)")
    source = source.replace("float3 rim = before + 0.25;", "float3 adjustment = base * 2.0;\n    float3 rim = adjustment + before;")
    result = assert_resolves(record, source)
    assert "source" not in result and "source" not in record
    assert result["selection"]["byteStart"] != record["anchor"]["selection"]["byteStart"]


def test_same_variable_in_another_function_does_not_capture_wrong_owner():
    record = make()
    source = "float3 unrelated(){float3 rim=0;return rim;}\n" + SOURCE
    result = assert_resolves(record, source)
    assert result["selection"]["byteStart"] > source.index("float3 helper")


def test_parameter_overload_signature_disambiguates_same_named_functions():
    source = "float helper(float x){return x;}\nfloat helper(int x){return float(x);}\nfloat4 main():SV_Target0{return helper(1);}"
    record = make(source, "x;", name="参数")
    assert_resolves(record, "// line\n" + source, "x")


@pytest.mark.parametrize("source,reason", [
    (SOURCE.replace("return rim;", "return before;"), "snapshot-expression-rewritten"),
    (SOURCE.replace("return rim;", "return 0;"), "snapshot-expression-rewritten"),
    (SOURCE.replace("return rim;", ""), "snapshot-expression-rewritten"),
])
def test_deletion_or_rewrite_never_falls_back_to_another_rim(source, reason):
    result = resolve(make(), source)
    assert result["status"] == "unresolved"
    assert "selection" not in result
    # Existing declaration contains the same word; it is still not the old read.
    assert result["reason"] in {reason, "snapshot-anchor-ambiguous"}


def test_copy_of_selected_statement_is_ambiguous_even_if_old_offset_still_matches():
    source = SOURCE.replace("return rim;", "if(weight>0.5) return rim;\n    return rim;")
    result = resolve(make(), source)
    assert result == {"status": "unresolved", "reason": "snapshot-anchor-ambiguous"}


def test_copy_of_same_function_is_ambiguous():
    helper = SOURCE[SOURCE.index("float3 helper"):SOURCE.index("float4 main")]
    result = resolve(make(), SOURCE + helper)
    assert result["reason"] == "snapshot-anchor-ambiguous"
    assert "selection" not in result


def test_existing_repeated_statement_requires_stable_context_after_edits():
    source = "float helper(float x){if(x>1){x=x+1;}if(x<0){x=x+1;}return x;}float4 main():SV_Target0{return helper(0);}"
    record = make(source, "x=x+1", name="第一次")
    changed = source.replace("float helper", "// header\nfloat helper")
    result = assert_resolves(record, changed, "x=x+1")
    assert result["selection"]["byteStart"] == changed.index("x=x+1")
    deleted = source.replace("if(x>1){x=x+1;}", "")
    assert resolve(record, deleted)["status"] == "unresolved"


def test_same_statement_previous_calculation_change_can_keep_unique_right_context():
    source = "float helper(float x){float r=x+sin(0.25)+cos(x)*2.0;return r;}float4 main():SV_Target0{return helper(0);}"
    record = make(source, "sin(0.25)")
    changed = source.replace("r=x+", "r=x*x+0.75+")
    assert_resolves(record, changed, "sin(0.25)")


@pytest.mark.parametrize("key,value", [("workspace", "D:/Other"), ("path", "D:/Other/rim.hlsl"),
    ("stage", "Vertex"), ("originalShader", "b" * 64)])
def test_identity_boundary_never_crosses_workspace_document_stage_or_shader(key, value):
    identity = dict(IDENTITY, **{key: value})
    assert resolve(make(), identity=identity) == {"status": "unresolved", "reason": "snapshot-identity-mismatch"}


def test_windows_identity_normalization_is_portable():
    identity = dict(IDENTITY, workspace="d:\\Projects\\角色", path="d:\\Projects\\角色\\rim.hlsl")
    assert resolve(make(), identity=identity)["status"] == "resolved"


def test_save_restart_chinese_rename_reorder_and_delete(tmp_path):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    first, second = make(name="轮廓光"), make(needle="before + 0.25", name="高光✨")
    store.add(first)
    store.add(second)
    store.rename(first["id"], "新的轮廓光 · 红通道")
    store.reorder([second["id"], first["id"]])
    restarted = snapshots.SnapshotStore(tmp_path, IDENTITY)
    records = restarted.list()
    assert [r["id"] for r in records] == [second["id"], first["id"]]
    assert records[1]["name"] == "新的轮廓光 · 红通道"
    assert "新的轮廓光" in Path(store.path).read_text(encoding="utf-8")
    assert_resolves(records[1], "// reopened\n" + SOURCE)
    restarted.remove(second["id"])
    assert [r["id"] for r in store.list()] == [first["id"]]
    store.delete(first["id"])
    assert restarted.list() == []


def test_recipe_preserves_member_hit_and_display_options_without_old_replay_data(tmp_path):
    options = {"mode": "range", "rangeMin": -5, "rangeMax": 7, "channel": 2,
               "targetIndex": 1, "iteration": 9, "independentFloatOutput": True}
    record = make(member=".normal", options=options)
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    store.add(record)
    restored = store.list()[0]
    result = resolve(restored)
    assert result["options"] == options
    assert result["member"] == ".normal"
    assert not {"eventId", "replacement", "textureId", "image", "compiledShader", "source"} & set(restored)


def test_read_only_operations_never_create_source_or_store_files(tmp_path):
    source_path = tmp_path / "user.hlsl"
    source_path.write_text(SOURCE, encoding="utf-8")
    before = source_path.read_bytes()
    store = snapshots.SnapshotStore(tmp_path / "state", IDENTITY)
    assert store.list() == []
    assert not (tmp_path / "state").exists()
    assert_resolves(make(), SOURCE)
    assert source_path.read_bytes() == before


def test_identity_stores_are_isolated_and_concurrent_objects_reload(tmp_path):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    another = snapshots.SnapshotStore(tmp_path, IDENTITY)
    store.add(make(name="one"))
    another.add(make(name="two"))
    assert len(store.list()) == 2
    other_identity = dict(IDENTITY, path="D:/Other.hlsl")
    other = snapshots.SnapshotStore(tmp_path, other_identity)
    assert other.path != store.path and other.list() == []


def test_failed_atomic_replace_preserves_existing_file_and_cleans_temporary(tmp_path, monkeypatch):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    record = make()
    store.add(record)
    before = Path(store.path).read_bytes()
    def fail_replace(*args):
        raise OSError("simulated interruption")
    monkeypatch.setattr(snapshots.os, "replace", fail_replace)
    with pytest.raises(snapshots.SnapshotError, match="snapshot-store-write-failed"):
        store.rename(record["id"], "应当失败")
    assert Path(store.path).read_bytes() == before
    assert not list(Path(store.path).parent.glob(".snapshot-*.tmp"))


def test_invalid_file_is_not_silently_overwritten(tmp_path):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    Path(store.path).parent.mkdir(parents=True)
    Path(store.path).write_text("{truncated", encoding="utf-8")
    with pytest.raises(snapshots.SnapshotError, match="snapshot-store-invalid"):
        store.add(make())
    assert Path(store.path).read_text(encoding="utf-8") == "{truncated"


def test_file_count_and_source_limits_are_enforced(tmp_path, monkeypatch):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    monkeypatch.setattr(snapshots, "MAX_SNAPSHOTS", 1)
    store.add(make())
    with pytest.raises(snapshots.SnapshotError, match="snapshot-size-limit"):
        store.add(make())
    monkeypatch.setattr(snapshots, "MAX_FILE_BYTES", 16)
    with pytest.raises(snapshots.SnapshotError, match="snapshot-size-limit"):
        store.list()
    monkeypatch.setattr(snapshots, "MAX_SOURCE_BYTES", 16)
    with pytest.raises(snapshots.SnapshotError, match="snapshot-invalid-source"):
        make()


@pytest.mark.parametrize("options", [{"eventId": 42}, {"rangeMin": float("nan")}, {"iteration": -1},
    {"channel": 4}, {"mode": "invented"}, {"independentFloatOutput": "false"}])
def test_unsupported_options_cannot_smuggle_stale_replay_state(options):
    with pytest.raises(snapshots.SnapshotError, match="snapshot-invalid-options"):
        make(options=options)


def test_invalid_order_or_identity_does_not_change_file(tmp_path):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    record = make()
    store.add(record)
    before = Path(store.path).read_bytes()
    with pytest.raises(snapshots.SnapshotError, match="snapshot-invalid-order"):
        store.reorder([])
    with pytest.raises(snapshots.SnapshotError, match="snapshot-identity-mismatch"):
        store.add(make(identity=dict(IDENTITY, path="D:/different.hlsl")))
    assert Path(store.path).read_bytes() == before


def test_corrupt_anchor_is_rejected_without_trying_old_line_number():
    record = make()
    record["anchor"]["endToken"] = 999999
    with pytest.raises(snapshots.SnapshotError, match="snapshot-invalid-record"):
        resolve(record)


def test_returned_records_are_detached_from_store(tmp_path):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    record = make()
    store.add(record)
    record["anchor"]["bodyTokens"][0] = "changed"
    fetched = store.list()
    fetched[0]["name"] = "changed"
    assert store.list()[0]["name"] == "轮廓光"


@pytest.mark.parametrize("field,value", [("source", SOURCE), ("textureId", "123"), ("eventId", 42)])
def test_add_rejects_shader_source_images_and_native_state_fields(tmp_path, field, value):
    record = make()
    record[field] = value
    with pytest.raises(snapshots.SnapshotError, match="snapshot-invalid-record"):
        snapshots.SnapshotStore(tmp_path, IDENTITY).add(record)
    assert not list(tmp_path.rglob("*.json"))


def test_store_update_options_and_member_does_not_rebind_anchor(tmp_path):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    record = make()
    store.add(record)
    updated = store.update(record["id"], member=".roughness", options={"mode": "coverage", "iteration": 3})
    assert updated["anchor"] == record["anchor"]
    assert updated["options"]["iteration"] == 3
    assert updated["member"] == ".roughness"
    with pytest.raises(snapshots.SnapshotError, match="snapshot-invalid-metadata"):
        store.update(record["id"], anchor={})


def test_unbalanced_latest_source_is_unresolved_without_a_position():
    result = resolve(make(), SOURCE.replace("return rim;", "return (rim;"))
    assert result == {"status": "unresolved", "reason": "snapshot-source-unparseable"}


def test_invalid_metadata_types_are_rejected_consistently(tmp_path):
    with pytest.raises(snapshots.SnapshotError, match="snapshot-invalid-options"):
        make(options={"mode": []})
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    store.add(make())
    with pytest.raises(snapshots.SnapshotError, match="snapshot-invalid-order"):
        store.reorder([{}])


def test_glsl_function_anchor_tracks_token_position_through_comments():
    source = "#version 450\nlayout(location=0) out vec4 color;vec3 lighting(vec3 n){return mix(n,vec3(1),0.25);}\nvoid main(){color=vec4(lighting(vec3(0)),1);}"
    record = make(source, "mix(n,vec3(1),0.25)")
    changed = source.replace("return mix", "// changed explanation\nreturn mix")
    assert_resolves(record, changed, "mix(n,vec3(1),0.25)")


def test_custom_display_is_default_and_survives_store_restart(tmp_path):
    record = make()
    assert record["options"]["mode"] == "custom"
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    store.add(record)
    restarted = snapshots.SnapshotStore(tmp_path, IDENTITY)
    assert resolve(restarted.list()[0])["options"]["mode"] == "custom"
    store.update(record["id"], options={"mode": "raw"})
    assert restarted.list()[0]["options"]["mode"] == "raw"


def test_module_syntax_remains_compatible_with_embedded_python36():
    import ast
    ast.parse(MODULE.read_text(encoding="utf-8"), feature_version=(3, 6))


def test_hlsl_constantbuffer_register_is_not_an_executable_function():
    source = "cbuffer Params : register(b0) {float3 rim;};float4 main():SV_Target0{return float4(rim,1);}"
    with pytest.raises(snapshots.SnapshotError, match="snapshot-anchor-unavailable"):
        make(source, "rim;")
    record = make(source, "rim,1")
    assert record["anchor"]["function"] == "main"


def test_two_processes_do_not_lose_concurrent_additions(tmp_path):
    worker = """import importlib.util,json,sys,time
spec=importlib.util.spec_from_file_location('snap',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
store=module.SnapshotStore(sys.argv[2],json.loads(sys.argv[3]))
save=store._save
def slow_save(records):
    time.sleep(0.15)
    save(records)
store._save=slow_save
with open(sys.argv[4],encoding='utf-8') as stream: record=json.load(stream)
store.add(record)
"""
    records = [make(name="first"), make(name="second")]
    paths = [tmp_path / "first.json", tmp_path / "second.json"]
    for path, record in zip(paths, records):
        path.write_text(json.dumps(record), encoding="utf-8")
    processes = [subprocess.Popen([sys.executable, "-c", worker, str(MODULE), str(tmp_path / "state"),
                                  json.dumps(IDENTITY), str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for path in paths]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    store = snapshots.SnapshotStore(tmp_path / "state", IDENTITY)
    assert {record["id"] for record in store.list()} == {record["id"] for record in records}
    assert Path(store.path + ".lock").read_bytes() == b"\0"


def test_process_exit_releases_lock_and_contention_is_bounded(tmp_path, monkeypatch):
    worker = """import importlib.util,json,sys,time
spec=importlib.util.spec_from_file_location('snap',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
store=module.SnapshotStore(sys.argv[2],json.loads(sys.argv[3]))
with store._file_lock():
    print('locked',flush=True)
    time.sleep(30)
"""
    process = subprocess.Popen([sys.executable, "-c", worker, str(MODULE), str(tmp_path), json.dumps(IDENTITY)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    try:
        assert process.stdout.readline().strip() == b"locked"
        monkeypatch.setattr(snapshots, "LOCK_TIMEOUT_SECONDS", 0.05)
        with pytest.raises(snapshots.SnapshotError, match="snapshot-store-busy"):
            store.add(make())
    finally:
        process.terminate()
        process.communicate(timeout=5)
    # The stale sidecar is harmless: the operating system released ownership.
    store.add(make())
    assert len(store.list()) == 1


@pytest.mark.parametrize("replacement", [
    "{float3 rim=0.75;return rim;}",
    "if(weight>0.5){float3 rim=0.75;return rim;}",
])
def test_deleted_read_is_not_rebound_to_identical_text_in_new_shadow_scope(replacement):
    record = make()
    changed = SOURCE.replace("return rim;", replacement)
    result = resolve(record, changed)
    assert result == {"status": "unresolved", "reason": "snapshot-expression-rewritten"}


def test_new_shadow_declaration_inside_existing_block_invalidates_old_read():
    source = "float helper(float x){if(x>0){return x;}return 0;}float4 main():SV_Target0{return helper(1);}"
    record = make(source, "x;")
    changed = source.replace("if(x>0){", "if(x>0){float x=0.75;")
    assert resolve(record, changed)["status"] == "unresolved"


def test_same_declaration_initializer_edit_keeps_value_identity():
    record = make()
    changed = SOURCE.replace("float3 rim = before + 0.25;", "float3 rim = normalize(before) * 2.0;")
    assert_resolves(record, changed)


def test_compound_shadow_declaration_cannot_steal_saved_parameter_read():
    source = "float helper(float x){if(x>0){return x;}return 0;}float4 main():SV_Target0{return helper(1);}"
    record = make(source, "x;")
    changed = source.replace("if(x>0){", "if(x>0){float other=1,x=0.75;")
    assert resolve(record, changed)["status"] == "unresolved"


def test_call_argument_is_not_mistaken_for_a_new_declaration():
    source = "float helper(float x){if(x>0){return x;}return 0;}float4 main():SV_Target0{return helper(1);}"
    record = make(source, "x;")
    changed = source.replace("if(x>0){", "if(x>0){float other=max(1.0,x);")
    assert_resolves(record, changed, "x")


def test_multiple_recipes_share_only_exact_source_lexing_and_recheck_new_source(monkeypatch):
    snapshots._LEX_CACHE.clear()
    calls = []
    original = snapshots._lex
    def counted(source):
        calls.append(source)
        return original(source)
    monkeypatch.setattr(snapshots, "_lex", counted)
    records = [make(), make(needle="before + 0.25"), make(needle="base * weight")]
    for record in records:
        assert resolve(record)["status"] == "resolved"
    assert calls == [SOURCE]
    changed = "// new source\n" + SOURCE
    for record in records:
        assert resolve(record, changed)["status"] == "resolved"
    assert calls == [SOURCE, changed]
    # Reusing lexical tables never reuses a resolution result.
    deleted = SOURCE.replace("return rim;", "return before;")
    assert resolve(records[0], deleted)["status"] == "unresolved"
    assert len(calls) == 3 and len(snapshots._LEX_CACHE) == 2
    resolve(records[0], SOURCE)
    assert len(calls) == 4


def test_large_token_tables_are_not_retained_in_source_cache(monkeypatch):
    snapshots._LEX_CACHE.clear()
    monkeypatch.setattr(snapshots, "MAX_CACHED_TOKENS", 1)
    make()
    assert not snapshots._LEX_CACHE


def test_struct_declaration_initializer_changes_keep_selected_numeric_member():
    source = "struct Surface{float3 color;};Surface makeSurface(float x){Surface s;s.color=x;return s;}" \
             "float4 main():SV_Target0{Surface surface=makeSurface(0.25);float3 light=surface.color*2;return float4(lerp(light,surface.color,0.5),1);}"
    start = source.index("surface=makeSurface")
    record = snapshots.make_snapshot(source, {"byteStart": start, "byteEnd": start + len("surface")},
                                     IDENTITY, member=".color")
    changed = "// 插入行\n\n" + source.replace("makeSurface(0.25)", "makeSurface(0.8)")
    result = assert_resolves(record, changed, "surface")
    assert result["member"] == ".color"
    assert result["selection"]["byteStart"] == len(changed[:changed.index("surface=makeSurface")].encode("utf-8"))


@pytest.mark.parametrize("declaration,replacement", [
    ("float value=0.25;", "float value=0.8;"),
    ("float before=0,value=0.25;", "float before=0,value=0.8;"),
])
def test_plain_and_compound_declaration_snapshots_follow_initializer_edits(declaration, replacement):
    source = "float4 main():SV_Target0{" + declaration + "return value+value;}"
    start = source.index("value=")
    record = snapshots.make_snapshot(source, {"byteStart": start, "byteEnd": start + 5}, IDENTITY)
    assert_resolves(record, "\n\n" + source.replace(declaration, replacement), "value")


@pytest.mark.parametrize("replacement", [
    "float value=0.8;float value=0.25;",  # copied original after an edited declaration
    "float value=0.8;float value=0.9;",   # neither initializer matches the old text
    "{float value=0.8;}",               # moved into another lexical scope
    "int value=1;",                      # different declared type
    "",                                 # a surviving read is not a declaration
])
def test_declaration_snapshot_rejects_duplicate_moved_deleted_or_different_type(replacement):
    source = "float4 main():SV_Target0{float value=0.25;return value+value;}"
    start = source.index("value=")
    record = snapshots.make_snapshot(source, {"byteStart": start, "byteEnd": start + 5}, IDENTITY)
    result = resolve(record, source.replace("float value=0.25;", replacement))
    assert result["status"] == "unresolved" and "selection" not in result


def test_same_name_shadow_does_not_steal_existing_declaration_snapshot():
    source = "float4 main():SV_Target0{float value=0.25;return value+value;}"
    start = source.index("value=")
    record = snapshots.make_snapshot(source, {"byteStart": start, "byteEnd": start + 5}, IDENTITY)
    changed = source.replace("float value=0.25;", "float value=0.8;{float value=0.75;}")
    result = assert_resolves(record, changed, "value")
    assert result["selection"]["byteStart"] == changed.index("value=0.8")


SURFACE_ASSIGNMENT = """struct Surface{float3 albedo;};
Surface SampleSurface(float4 baseSample,float factor){
 Surface result;
 result.albedo=baseSample.xyz*factor;
 return result;
}
float4 main():SV_Target0{return float4(SampleSurface(0.5,1).albedo,1);}
"""


def byte_selection(source, expression, occurrence=0):
    start = -1
    for _ in range(occurrence + 1):
        start = source.index(expression, start + 1)
    return {"byteStart": len(source[:start].encode("utf-8")),
            "byteEnd": len(source[:start + len(expression)].encode("utf-8"))}


def test_inserted_assignment_and_compound_operator_keep_original_write_site():
    record = make(SURFACE_ASSIGNMENT, "result.albedo")
    changed = SURFACE_ASSIGNMENT.replace("result.albedo=baseSample.xyz*factor;",
        "result.albedo=0.1;\n result.albedo+=baseSample.xyz*factor;")
    result = assert_resolves(record, changed, "result.albedo")
    assert result["selection"]["byteStart"] == changed.index("result.albedo+=")
    assert result["selection"]["byteStart"] != changed.index("result.albedo=0.1")


@pytest.mark.parametrize("operator", ["+=", "-=", "*=", "/="])
def test_statement_operator_change_with_repeated_member_reads_remains_unique(operator):
    source = SURFACE_ASSIGNMENT.replace("return result;", "result.albedo=result.albedo*2;return result;")
    record = make(source, "result.albedo")
    changed = source.replace("result.albedo=baseSample", "result.albedo" + operator + "baseSample")
    result = assert_resolves(record, changed, "result.albedo")
    assert result["selection"]["byteStart"] == changed.index("result.albedo" + operator + "baseSample")


def test_operator_fallback_rejects_copy_even_when_only_one_exact_old_statement_remains():
    record = make(SURFACE_ASSIGNMENT, "result.albedo")
    changed = SURFACE_ASSIGNMENT.replace("result.albedo=baseSample.xyz*factor;",
        "result.albedo+=baseSample.xyz*factor;result.albedo=baseSample.xyz*factor;")
    assert resolve(record, changed) == {"status": "unresolved", "reason": "snapshot-anchor-ambiguous"}


def test_authoritative_live_reanchor_preserves_original_among_identical_inserted_writes():
    record = make(SURFACE_ASSIGNMENT, "result.albedo", name="原赋值")
    changed = SURFACE_ASSIGNMENT.replace("result.albedo=baseSample.xyz*factor;",
        "result.albedo=baseSample.xyz*factor;\n result.albedo=baseSample.xyz*factor;")
    assert resolve(record, changed)["status"] == "unresolved"
    # The editor's edit mapping knows insertion was before the original range.
    mapped = byte_selection(changed, "result.albedo", 1)
    updated = snapshots.reanchor_snapshot(record, changed, mapped, IDENTITY)
    result = resolve(updated, changed)
    assert result["selection"]["byteStart"] == mapped["byteStart"]
    assert updated["id"] == record["id"] and updated["createdAt"] == record["createdAt"]
    assert updated["name"] == "原赋值" and updated["options"] == record["options"]


def test_known_rename_updates_expression_and_current_source_anchor(tmp_path):
    record = make(SURFACE_ASSIGNMENT, "result.albedo")
    changed = SURFACE_ASSIGNMENT.replace("albedo", "tint")
    assert resolve(record, changed)["status"] == "unresolved"
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    store.add(record)
    updated = store.reanchor(record["id"], changed, byte_selection(changed, "result.tint"))
    assert updated["expression"] == "result.tint"
    assert_resolves(snapshots.SnapshotStore(tmp_path, IDENTITY).list()[0], changed, "result.tint")


def test_known_cut_move_uses_editor_range_instead_of_matching_other_scope(tmp_path):
    record = make(SURFACE_ASSIGNMENT, "result.albedo")
    changed = SURFACE_ASSIGNMENT.replace("result.albedo=baseSample.xyz*factor;",
        "if(factor>0){result.albedo=baseSample.xyz*factor;}")
    assert resolve(record, changed)["status"] == "unresolved"
    moved = snapshots.reanchor_snapshot(record, changed, byte_selection(changed, "result.albedo"), IDENTITY)
    assert_resolves(moved, changed, "result.albedo")


def test_deleted_anchor_persists_without_rebinding_and_explicit_undo_revives_it(tmp_path):
    source = SOURCE
    record = make(source)
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    store.add(record)
    deleted = source.replace("return rim;", "return before;")
    store.update_anchors(deleted, {record["id"]: None})
    restarted = snapshots.SnapshotStore(tmp_path, IDENTITY)
    invalid = restarted.list()[0]
    assert resolve(invalid, source) == {"status": "unresolved", "reason": "snapshot-expression-deleted"}
    # Undo/redo is explicit editor history, not a search for surviving text.
    restored_range = byte_selection(source, "rim;")
    restored_range["byteEnd"] -= 1
    restored = restarted.reanchor(record["id"], source, restored_range)
    assert restored["id"] == record["id"]
    assert_resolves(restored, source)
    restarted.update_anchors(deleted, {record["id"]: None})
    assert resolve(restarted.list()[0], source)["status"] == "unresolved"


def test_bulk_reanchor_keeps_concurrent_metadata_and_order_with_one_atomic_save(tmp_path, monkeypatch):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    first, second = make(), make(needle="before + 0.25")
    store.add(first)
    store.add(second)
    other = snapshots.SnapshotStore(tmp_path, IDENTITY)
    other.rename(first["id"], "其他窗口的新名字")
    other.reorder([second["id"], first["id"]])
    saves = []
    original = store._save
    def counted(records):
        saves.append(1)
        return original(records)
    monkeypatch.setattr(store, "_save", counted)
    changed = "// inserted\n" + SOURCE
    first_selection = byte_selection(changed, "rim;")
    first_selection["byteEnd"] -= 1
    updated = store.update_anchors(changed, {first["id"]: first_selection,
        second["id"]: byte_selection(changed, "before + 0.25")})
    assert len(saves) == 1
    assert [r["id"] for r in updated] == [second["id"], first["id"]]
    assert updated[1]["name"] == "其他窗口的新名字"
    assert_resolves(updated[1], changed)


def test_incomplete_editor_source_does_not_overwrite_last_persisted_anchor(tmp_path):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    record = make()
    store.add(record)
    before = Path(store.path).read_bytes()
    changed = SOURCE.replace("return rim;", "return (rim;")
    selection = byte_selection(changed, "rim;")
    selection["byteEnd"] -= 1
    with pytest.raises(snapshots.SnapshotError, match="snapshot-anchor-unavailable"):
        store.reanchor(record["id"], changed, selection)
    assert Path(store.path).read_bytes() == before


@pytest.mark.parametrize("expression", ["rim;", "return rim", "rim +", "before + 0.25; return rim"])
def test_live_reanchor_does_not_persist_a_partial_or_cross_statement_expression(expression):
    record = make()
    if expression == "rim +":
        source = SOURCE.replace("return rim;", "return rim + 1;")
    elif expression == "before + 0.25; return rim":
        source = SOURCE.replace(";\n    return", "; return")
    else:
        source = SOURCE
    with pytest.raises(snapshots.SnapshotError, match="snapshot-invalid-selection"):
        snapshots.reanchor_snapshot(record, source, byte_selection(source, expression), IDENTITY)


def test_deletion_tombstone_can_be_saved_while_other_editor_expression_is_incomplete(tmp_path):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    record = make()
    store.add(record)
    incomplete = SOURCE.replace("return rim;", "return (before;")
    store.update_anchors(incomplete, {record["id"]: None})
    assert resolve(store.list()[0], SOURCE)["reason"] == "snapshot-expression-deleted"


def test_restore_saved_anchor_after_discard_clears_only_edit_deletion(tmp_path):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    checkpoint = make()
    store.add(checkpoint)
    store.update_anchors(SOURCE.replace("return rim;", "return (before;"), {checkpoint["id"]: None})
    restored = store.restore_anchors([checkpoint])[0]
    assert restored["anchor"] == checkpoint["anchor"]
    assert_resolves(restored, SOURCE)
    assert restored["createdAt"] == checkpoint["createdAt"]


def test_restore_saved_deletion_preserves_tombstone_even_if_text_exists(tmp_path):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    record = make()
    store.add(record)
    checkpoint = store.invalidate_anchor(record["id"])
    store.restore_anchors([record])
    assert_resolves(store.list()[0], SOURCE)
    restored = store.restore_anchors([checkpoint])[0]
    assert resolve(restored, SOURCE)["reason"] == "snapshot-expression-deleted"


def test_restore_anchor_preserves_concurrent_metadata_order_and_removal(tmp_path, monkeypatch):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    first, second, removed = make(), make(needle="before + 0.25"), make()
    for record in (first, second, removed):
        store.add(record)
    other = snapshots.SnapshotStore(tmp_path, IDENTITY)
    other.update(first["id"], name="并发改名", member=".x", options={"mode": "custom", "channel": 2})
    other.reorder([second["id"], removed["id"], first["id"]])
    other.remove(removed["id"])
    other.invalidate_anchor(first["id"])
    saves, original = [], store._save
    def counted(records):
        saves.append(1)
        return original(records)
    monkeypatch.setattr(store, "_save", counted)
    restored = store.restore_anchors([first, second, removed])
    assert len(saves) == 1
    assert [record["id"] for record in restored] == [second["id"], first["id"]]
    assert restored[1]["name"] == "并发改名" and restored[1]["member"] == ".x"
    assert restored[1]["options"]["channel"] == 2
    assert restored[1]["anchor"] == first["anchor"]


@pytest.mark.parametrize("kind", ["foreign", "invalid", "duplicate"])
def test_restore_anchor_rejects_entire_invalid_batch_without_writing(tmp_path, kind):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    record = make()
    store.add(record)
    store.invalidate_anchor(record["id"])
    bad = copy.deepcopy(record)
    if kind == "foreign":
        bad["identity"]["stage"] = "vertex"
    elif kind == "invalid":
        bad["anchor"]["startToken"] = -1
    batch = [record, bad]
    previous = Path(store.path).read_bytes()
    with pytest.raises(snapshots.SnapshotError):
        store.restore_anchors(batch)
    assert Path(store.path).read_bytes() == previous


def test_restore_empty_or_concurrently_removed_checkpoints_never_creates_snapshot(tmp_path):
    store = snapshots.SnapshotStore(tmp_path, IDENTITY)
    assert store.restore_anchors([]) == []
    assert not Path(store.path).exists()
    assert store.restore_anchors([make()]) == []
    assert not Path(store.path).exists()


def test_unique_function_rename_recovers_legacy_snapshot():
    assert_resolves(make(), SOURCE.replace("helper", "renamed"))
