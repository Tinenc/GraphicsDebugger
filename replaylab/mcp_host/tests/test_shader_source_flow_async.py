"""Current-source graph delivery must not need a saved AI artifact or GUI parsing."""
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import time
from types import SimpleNamespace
from unittest import mock

from test_shader_workspace_panel import MODULE_PATH, _load_without_qt


workspace = _load_without_qt()
SOURCE = "float Helper(float x) { return x*x; }\nfloat MainPS(float x) { return Helper(x); }\n"


def request(source=SOURCE, graph=None, entry="MainPS"):
    return {"sourceText": source, "graph": graph, "entryPoint": entry,
            "stop": threading.Event(), "sourceIdentity": {
                "workspaceId": "owned-workspace", "documentId": "owned-workspace",
                "path": "source/shader.hlsl"}}


def panel_methods():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf8"))
    names = {"_source_flow_key", "_source_hierarchy", "_request_source_flow",
             "_source_flow_ready", "_sync_flow_graph"}
    methods = [node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {}
    globals_ = dict(workspace.__dict__)
    # The production panel has Qt; this extracted-method harness deliberately
    # excludes it. Disable persistent navigation storage unless a test supplies
    # its own temporary root.
    globals_["QtCore"] = SimpleNamespace(QStandardPaths=SimpleNamespace(
        AppLocalDataLocation=0, writableLocation=lambda location: ""))
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(MODULE_PATH), "exec"),
         globals_, namespace)
    return type("ProductionFlowMethods", (), namespace)


class Executor:
    def __init__(self):
        self.jobs = []

    def submit(self, owner, operation, callback):
        self.jobs.append((operation, callback))

    def deliver(self, index=0):
        operation, callback = self.jobs[index]
        callback(operation(), "")


def panel():
    instance = panel_methods()()
    instance.binding = {"captureSessionId": "a"}
    model = {"workspaceId": "owned-workspace", "entryPoint": "MainPS", "flowGraph": None}
    editor = SimpleNamespace(text=SOURCE)
    editor.GetText = lambda: editor.text
    document = {"workspaceId": model["workspaceId"], "documentToken": 7,
                "sourcePath": "source/shader.hlsl", "available": True,
                "textRevision": 0, "captureBinding": instance.binding, "editor": editor}
    instance._documents = {model["workspaceId"]: document}
    instance.current = document
    instance._current_document = lambda: instance.current
    instance._document_model = lambda key: model
    instance._current_capture_binding = lambda: instance.binding
    instance._snapshot_generation = 1
    instance._snapshot_owner = "owner"
    instance._snapshot_executor = Executor()
    instance._flow_request = None
    instance._flow_graph_signature = None
    instance._alive = True
    instance._shutting_down = False
    instance.flow_graph = mock.Mock()
    instance._anchor_graph = mock.Mock(side_effect=lambda document, graph, hierarchy: graph)
    instance._prune_flow_anchors = mock.Mock()
    return instance, document


def test_missing_saved_graph_projects_real_current_calls_and_single_function():
    result = workspace._build_source_flow_projection(request())
    assert result["state"] == "available"
    normalized = workspace._shader_flow_graph.normalize_flow_graph(result["graph"])
    assert [node["label"] for node in normalized["nodes"]] == ["MainPS", "Helper"]
    assert len(normalized["edges"]) == 1
    assert normalized["sourceDerived"] and not normalized["generated"]
    leaf = workspace._build_source_flow_projection(request("float MainPS() { return 1; }"))
    assert [node["name"] for node in leaf["graph"]["nodes"]] == ["MainPS"]
    assert leaf["graph"]["edges"] == []


def test_saved_annotations_need_exact_current_source_and_cannot_override_entry():
    graph = {"entryPoint": "WrongEntry", "sourceSha256": "0" * 64,
             "nodes": [{"name": "Helper", "description": "Old helper behavior"}], "edges": []}
    stale = workspace._build_source_flow_projection(request(graph=graph))
    assert stale["state"] == "available"
    assert all(node.get("description") != "Old helper behavior" for node in stale["graph"]["nodes"])
    graph["sourceSha256"] = hashlib.sha256(SOURCE.encode()).hexdigest()
    current = workspace._build_source_flow_projection(request(graph=graph))
    assert current["state"] == "available"
    assert any(node.get("description") == "Old helper behavior" for node in current["graph"]["nodes"])


def test_exact_legacy_entry_prose_survives_matching_source_projection():
    description = "Shader entry point; the stage result is produced here."
    graph = {"entryPoint": "MainPS", "sourceSha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
             "nodes": [{"name": "MainPS", "description": description}], "edges": []}
    result = workspace._build_source_flow_projection(request(graph=graph))
    assert result["graph"]["nodes"][0]["description"] == description
    graph["nodes"][0]["description"] = "return 1;"
    rejected = workspace._build_source_flow_projection(request(graph=graph))
    assert rejected["graph"]["nodes"][0].get("description") != "return 1;"


def test_parse_error_and_missing_entry_have_distinct_states_and_cancel_is_inert():
    assert workspace._build_source_flow_projection(request("float MainPS( {"))["state"] == "invalid-source"
    assert workspace._build_source_flow_projection(request(entry="Absent"))["state"] == "entry-unavailable"
    cancelled = request()
    cancelled["stop"].set()
    assert workspace._build_source_flow_projection(cancelled) == {"cancelled": True}


def test_gui_only_submits_one_job_then_reuses_hierarchy_and_graph():
    instance, document = panel()
    with mock.patch.object(workspace._shader_flow_hierarchy, "SourceFlowHierarchy",
                           side_effect=AssertionError("GUI must not parse")):
        instance._sync_flow_graph()
        for _ in range(10):
            instance._sync_flow_graph()
    assert len(instance._snapshot_executor.jobs) == 1
    instance.flow_graph.clear_graph.assert_called_once_with(
        graph_key="owned-workspace:unavailable", state="building")
    instance._snapshot_executor.deliver()
    graph = instance.flow_graph.set_graph.call_args.args[0]
    assert len(graph["nodes"]) == 2
    assert instance._source_hierarchy(document) is document["hierarchy"]
    for _ in range(10):
        instance._sync_flow_graph()
    assert len(instance._snapshot_executor.jobs) == 1
    assert instance.flow_graph.set_graph.call_count == 1


def test_new_editor_revision_supersedes_pending_result_without_queue_growth():
    instance, document = panel()
    instance._sync_flow_graph()
    original = instance._flow_request
    document["textRevision"] = 1
    document["editor"].text = SOURCE.replace("Helper", "Renamed")
    for _ in range(20):
        instance._sync_flow_graph()
    assert len(instance._snapshot_executor.jobs) == 1 and original["stop"].is_set()
    instance._snapshot_executor.deliver()
    assert len(instance._snapshot_executor.jobs) == 2
    instance.flow_graph.set_graph.assert_not_called()
    instance._snapshot_executor.deliver(1)
    assert document["hierarchyRevision"] == (1, 0)
    assert document["hierarchy"].function_identity("Renamed")


def test_edit_marks_existing_scene_stale_without_reparsing_or_resetting_exploration():
    instance, document = panel()
    instance._sync_flow_graph()
    instance._snapshot_executor.deliver()
    document["textRevision"] = 1
    document["editor"].text = SOURCE.replace("x*x", "x+x")
    instance._sync_flow_graph()
    instance.flow_graph.mark_source_changed.assert_called_once()
    assert instance.flow_graph.set_graph.call_count == 1
    assert len(instance._snapshot_executor.jobs) == 1
    assert instance._source_hierarchy(document) is None


def test_metadata_only_update_refreshes_function_descriptions_without_an_edit():
    instance, document = panel()
    instance._sync_flow_graph()
    instance._snapshot_executor.deliver()
    model = instance._document_model(document["workspaceId"])
    model["semanticOverview"] = {"sourceSha256": document["hierarchy"].source_sha256,
        "stages": [{"id": "purpose", "title": "Evaluate material response", "anchors": [
            {"kind": "function", "functionName": "Helper", "functionSignature": "Helper ( float x )"}]}]}
    assert instance._source_hierarchy(document) is None
    instance._sync_flow_graph()
    instance._snapshot_executor.deliver(1)
    current = instance.flow_graph.set_graph.call_args.args[0]
    assert current["nodes"][1]["description"] == "Evaluate material response"
    assert instance._source_hierarchy(document) is document["hierarchy"]
    assert instance._flow_request is None
    assert len(instance._snapshot_executor.jobs) == 2


def test_capture_or_document_close_drops_late_source_result():
    for close in (False, True):
        instance, document = panel()
        instance._sync_flow_graph()
        if close:
            instance._alive = False
        else:
            instance._snapshot_generation += 1
            instance.binding = {"captureSessionId": "other"}
            document["available"] = False
        instance._snapshot_executor.deliver()
        assert "hierarchy" not in document
        instance.flow_graph.set_graph.assert_not_called()


def test_large_buffer_worker_does_not_block_calling_thread():
    instance, document = panel()
    document["editor"].text = SOURCE + ("// padding for restored Shader source\n" * 10000)
    entered, release = threading.Event(), threading.Event()
    original = workspace._shader_flow_hierarchy.SourceFlowHierarchy
    owner = threading.get_ident()
    def delayed(*args):
        assert threading.get_ident() != owner
        entered.set()
        assert release.wait(5)
        return original(*args)
    with ThreadPoolExecutor(max_workers=1) as pool:
        instance._sync_flow_graph()
        operation, callback = instance._snapshot_executor.jobs[0]
        with mock.patch.object(workspace._shader_flow_hierarchy, "SourceFlowHierarchy", side_effect=delayed):
            pending = pool.submit(operation)
            assert entered.wait(5)
            started = time.perf_counter()
            instance._sync_flow_graph()
            assert time.perf_counter() - started < 0.1
            release.set()
            result = pending.result(timeout=5)
        callback(result, "")
    assert len(instance.flow_graph.set_graph.call_args.args[0]["nodes"]) == 2


def outlined_panel():
    from src.shader_workspace.semantic_overview import prepare_overview
    from test_shader_flow_range_navigation import RangeEditor
    source = 'float MainPS(float x) {\n float y = x*2;\n return y;\n}\n'
    instance, document = panel()
    document['editor'] = RangeEditor()
    document['editor'].source = source
    overview = prepare_overview(source, [{'id': 'shade', 'title': 'Shade', 'anchors': [
        {'function': 'MainPS', 'startLine': 2, 'endLine': 2}]}], 'MainPS')
    instance._document_model(document['workspaceId'])['semanticOverview'] = overview
    return instance, document


def deliver_pending(instance, first=0):
    position = first
    while position < len(instance._snapshot_executor.jobs):
        instance._snapshot_executor.deliver(position)
        position += 1
    return position


def test_live_directory_range_uses_worker_projection_after_gui_native_mapping():
    instance, document = outlined_panel()
    instance._sync_flow_graph()
    count = deliver_pending(instance)
    assert count == 1
    assert document['stageOutline'][0]['id'] == 'semantic:shade'
    editor = document['editor']
    # Inserting before the old range must preserve its identity, even when a
    # new statement has the same variable and the original operator changes.
    editor.insert(editor.source.index('float y'), 'float other = 0.1;\n ')
    operator = editor.source.index(' = x*2') + 1
    editor.insert(operator, '+')
    document['textRevision'] += 1
    instance._request_source_flow(document)
    operation, callback = instance._snapshot_executor.jobs[count]
    parsed = operation()
    with mock.patch.object(workspace._shader_flow_hierarchy, 'SourceFlowHierarchy',
                           side_effect=AssertionError('GUI mapping must not parse')):
        callback(parsed, '')
    assert instance._flow_request is not None
    # The second worker projects the already parsed index without parsing again.
    with mock.patch.object(workspace._shader_flow_hierarchy, 'SourceFlowHierarchy',
                           side_effect=AssertionError('second projection must reuse index')):
        deliver_pending(instance, count + 1)
    assert document['stageOutline'][0]['id'] == 'semantic:shade'
    assert document['stageOutline'][0]['line'] == 3
    assert instance._document_model(document['workspaceId'])['semanticOverview']['stages'][0][
        'anchors'][0]['tokenSignature'] == 'float y = x * 2 ;'


def test_stale_second_directory_projection_cannot_publish_after_further_edit():
    instance, document = outlined_panel()
    document['editor'].insert(0, '// initial current source\n')
    instance._sync_flow_graph()
    instance._snapshot_executor.deliver(0)
    assert instance._flow_request.get('outlineAnchorsAdjusted')
    document['textRevision'] += 1
    document['editor'].insert(0, '// next edit\n')
    instance._snapshot_executor.deliver(1)
    instance.flow_graph.set_graph.assert_not_called()
    assert len(instance._snapshot_executor.jobs) == 3
    deliver_pending(instance, 2)
    assert document['hierarchyRevision'] == (1, 0)
    assert document['stageOutline'][0]['line'] == 4


def test_removing_directory_metadata_releases_all_owned_ranges():
    instance, document = outlined_panel()
    instance._sync_flow_graph()
    count = deliver_pending(instance)
    assert document['editor'].ranges
    instance._document_model(document['workspaceId'])['semanticOverview'] = None
    instance._sync_flow_graph()
    deliver_pending(instance, count)
    assert not document['editor'].ranges
    assert 'flowOutlineAnchors' not in document


def test_committed_checkpoint_survives_external_rename_and_worker_reopen(tmp_path):
    instance, document = outlined_panel()
    path = tmp_path / 'shader.hlsl'
    document['sourcePath'] = str(path)
    path.write_bytes(document['editor'].source.encode('utf8'))
    qt = instance._request_source_flow.__func__.__globals__['QtCore']
    qt.QStandardPaths.writableLocation = lambda _: str(tmp_path / 'cache')
    model = instance._document_model(document['workspaceId'])
    model['shaderRawSha256'] = 'a' * 64
    model['semanticOverview']['stages'][0]['description'] = 'Preserved annotation'
    identity = workspace._shader_debug_snapshots.snapshot_identity(document, model)
    store = workspace._shader_navigation_history.NavigationHistory(str(tmp_path / 'cache'), identity)
    instance._sync_flow_graph()
    count = deliver_pending(instance)
    original = model['semanticOverview']
    saved = store.read(original)
    assert saved is not None
    editor = document['editor']
    editor.insert(0, '// 中文说明\n')
    document['textRevision'] += 1
    instance._request_source_flow(document)
    count = deliver_pending(instance, count)
    assert store.read(original) == saved  # unsaved buffer cannot change the checkpoint
    assert document['stageOutline'][0]['line'] == 3

    # A successful save with no new text edit must trigger the checkpoint pass.
    path.write_bytes(editor.source.encode('utf8'))
    document['navigationSaveRevision'] = 1
    instance._sync_flow_graph()
    deliver_pending(instance, count)
    assert store.read(original)['stages'][0]['anchors'][0]['sourceRange']['startLine'] == 3

    external = editor.source.replace('float y', 'float light').replace('return y', 'return light').replace('x*2', 'x*3')
    path.write_bytes(external.encode('utf8'))
    load = request(external)
    load.update(semanticOverview=original, navigationStorage={'root': str(tmp_path / 'cache'), 'identity': identity})
    reopened = workspace._build_source_flow_projection(load)
    assert reopened['stageOutline'][0]['line'] == 3
    assert reopened['stageOutline'][0]['description'] == 'Preserved annotation'
    assert 'light' in reopened['navigationOverview']['stages'][0]['anchors'][0]['tokenSignature']
    assert original['stages'][0]['anchors'][0]['sourceRange']['startLine'] == 2


def test_navigation_read_and_write_stay_on_worker(tmp_path):
    from src.shader_workspace.semantic_overview import prepare_overview
    owner = threading.get_ident()
    source = SOURCE
    overview = prepare_overview(source, [{'id': 'main', 'title': 'Main', 'anchors': [{'function': 'MainPS'}]}], 'MainPS')
    task = request(source)
    task.update(semanticOverview=overview, navigationStorage={'root': str(tmp_path), 'identity': {}})
    def only_worker(*args):
        assert threading.get_ident() != owner
        return None
    store = SimpleNamespace(read=only_worker, write=only_worker)
    with mock.patch.object(workspace._shader_navigation_history, 'NavigationHistory', return_value=store):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(workspace._build_source_flow_projection, task).result(timeout=5)['state'] == 'available'
