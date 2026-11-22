"""Reference-only chat attachments and bounded reads of exact editor revisions."""
import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from test_shader_workspace_panel import _load_without_qt, MODULE_PATH


module = _load_without_qt()
References = module._shader_source_references.SourceReferences


def reference(source, session="c" * 32):
    document = {"workspaceId": "a" * 32, "rawSha256": "b" * 64, "dirty": True,
        "captureBinding": {"captureSessionId": session, "capturePath": "D:/capture.rdc"},
        "sourcePath": "D:/project/source/shader.hlsl",
        "workspace": {"workspacePath": "D:/project", "shaderRawSha256": "d" * 64,
                      "stage": "fragment", "entryPoint": "Main", "resourceId": 9223372036854775900}}
    context, facts = module._selection_attachment(document, source, document["sourcePath"],
        1, max(1, len(source.splitlines())), 7, 7, [7], source_text=source)
    return context, facts


def test_explicit_selection_contains_only_identity_and_can_read_unsaved_revision_after_edit():
    store = References()
    source = "float Main() {\r\n    return 0.123;\r\n}\r\n"
    context, facts = reference(source)
    args = store.retain(context, facts, source)["arguments"]
    assert not {"text", "selectedText", "contextText", "excerpts"} & set(facts)
    assert source not in json.dumps(facts)
    assert "return 0.123" not in json.dumps(facts)
    newer = source.replace("0.123", "0.456")
    new_context, new_facts = reference(newer)
    store.retain(new_context, new_facts, newer)
    page = store.read(**args, start_line=2, end_line=2, current_capture_session="f" * 32)
    assert page["text"] == "    return 0.123;\r\n"
    assert page["context"]["shader"]["resourceId"] == "9223372036854775900"
    assert "eventId" not in page["context"]["capture"]
    assert page["currentCaptureMatches"] is False
    assert page["liveReplayEvidence"] is False and page["applied"] is False
    assert page["unsavedChanges"] is True


def test_full_unicode_long_line_paging_survives_common_provider_output_budget():
    from replaylab_agent.tool_output import guard_tool_output_text, MAX_INLINE_TOOL_OUTPUT_BYTES
    source = "// " + "法线\\\"😀" * 14000 + "\nfloat Main() { return 1; }\n"
    context, facts = reference(source)
    store = References()
    args = store.retain(context, facts, source)["arguments"]
    chunks, offset = [], 0
    while True:
        page = store.read(**args, offset=offset, limit=24000)
        encoded = json.dumps(page, ensure_ascii=True)
        assert len(encoded.encode()) <= MAX_INLINE_TOOL_OUTPUT_BYTES
        delivered = guard_tool_output_text("read_shader_workspace_source", encoded,
                                          authority="first-party-host")
        assert json.loads(delivered)["text"] == page["text"]
        chunks.append(page["text"])
        if page["nextOffset"] is None:
            break
        assert page["nextOffset"] > offset
        offset = page["nextOffset"]
    assert len(chunks) > 2
    assert "".join(chunks) == source
    assert hashlib.sha256("".join(chunks).encode()).hexdigest() == facts["sourceSha256"]


def test_cache_expiry_or_wrong_session_never_returns_current_or_disk_text():
    store = References(max_entries=1)
    first, second = "float original;", "float newer;"
    args = store.retain(*reference(first), first)["arguments"]
    store.retain(*reference(second), second)
    with mock.patch("builtins.open", side_effect=AssertionError("no filesystem fallback")):
        with pytest.raises(ValueError, match="unavailable or expired"):
            store.read(**args)
        other = store.retain(*reference(second), second)["arguments"]
        other["capture_session_id"] = "f" * 32
        with pytest.raises(ValueError, match="unavailable or expired"):
            store.read(**other)


@pytest.mark.parametrize("kwargs", [dict(start_line=0), dict(end_line=200), dict(offset=-1),
    dict(offset=99999), dict(limit=24001), dict(start_line=True), dict(limit=0)])
def test_invalid_ranges_are_rejected_without_substitution(kwargs):
    store = References()
    args = store.retain(*reference("abc\nxyz"), "abc\nxyz")["arguments"]
    with pytest.raises(ValueError):
        store.read(**args, **kwargs)


def test_original_cached_context_is_not_mutated_by_caller_or_reader():
    store = References()
    context, facts = reference("float x;")
    args = store.retain(context, facts, "float x;")["arguments"]
    context["shader"]["resourceId"] = "wrong"
    page = store.read(**args)
    page["context"]["shader"]["resourceId"] = "also-wrong"
    assert store.read(**args)["context"]["shader"]["resourceId"] == "9223372036854775900"


def test_empty_final_editor_line_is_a_valid_reference():
    store = References()
    args = store.retain(*reference("float x;\n"), "float x;\n")["arguments"]
    result = store.read(**args, start_line=2, end_line=2)
    assert result["text"] == "" and result["nextOffset"] is None
    assert result["totalLines"] == 2


def test_selection_sees_current_editor_revision_before_dirty_signal_is_processed():
    document = {"workspaceId": "a" * 32, "rawSha256": "b" * 64, "dirty": False,
                "baselineEditorText": "float original;", "workspace": {},
                "captureBinding": {"captureSessionId": "c" * 32}}
    unused, facts = module._selection_attachment(document, "float changed;", "shader.hlsl", 1, 1,
                                                source_text="float changed;")
    assert facts["unsavedChanges"] is True
    assert facts["sourceSha256"] == hashlib.sha256(b"float changed;").hexdigest()


def test_host_reader_is_provider_neutral_read_only_and_forwards_exact_revision():
    from src import server
    from replaylab_agent.policy import CORE_DIRECT_TOOLS, READ_ONLY_TOOLS, tool_effects
    args = {"workspace_id": "a" * 32, "capture_session_id": "c" * 32,
            "source_sha256": "d" * 64, "start_line": 8, "end_line": 18, "offset": 3, "limit": 4000}
    with mock.patch.object(server.ipc, "call", return_value={"text": "abc", "applied": False}) as call:
        result = json.loads(server.read_shader_workspace_source(**args))
    call.assert_called_once_with("read_shader_workspace_source", args, timeout=30.0)
    assert result["text"] == "abc"
    assert "read_shader_workspace_source" in CORE_DIRECT_TOOLS
    assert "read_shader_workspace_source" in READ_ONLY_TOOLS
    assert not tool_effects("read_shader_workspace_source")


def test_host_rejects_invalid_source_reference_before_bridge_call():
    from src import server
    with mock.patch.object(server.ipc, "call") as call:
        result = json.loads(server.read_shader_workspace_source("../source.hlsl", "c" * 32, "d" * 64))
    assert "error" in result
    call.assert_not_called()


def test_embedded_reader_is_ui_marshaled_and_has_no_replay_or_file_access():
    path = MODULE_PATH.with_name("__init__.py")
    tree = ast.parse(path.read_text("utf8"), feature_version=(3, 6))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_read_shader_workspace_source")
    calls = [node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
             for node in ast.walk(function) if isinstance(node, ast.Call)
             and isinstance(node.func, (ast.Attribute, ast.Name))]
    assert "_invoke_on_ui_sync" in calls
    assert not {"_replay", "open", "SetEventID", "SetFrameEvent", "SetText"} & set(calls)
    ast.parse(Path(module._shader_source_references.__file__).read_text("utf8"), feature_version=(3, 6))


@pytest.mark.parametrize("selection_kind", ["code", "calculation"])
def test_real_attachment_callback_registers_revision_without_putting_code_in_chat(selection_kind):
    source = "float Main(){\nreturn 0.123;\n}"
    binding = {"captureSessionId": "c" * 32, "capturePath": "D:/capture.rdc"}
    document = {"workspaceId": "a" * 32, "rawSha256": "b" * 64, "dirty": True,
                "sourcePath": "D:/project/source/shader.hlsl", "captureBinding": binding,
                "workspace": {"stage": "fragment", "entryPoint": "Main", "workspacePath": "D:/project"},
                "editor": SimpleNamespace(GetText=lambda: source), "available": True}
    panel = SimpleNamespace(_documents={document["workspaceId"]: document},
        _workspace_event_ids={document["workspaceId"]: [7]}, _current_capture_binding=lambda: binding,
        ctx=SimpleNamespace(CurEvent=lambda: 7, CurSelectedEvent=lambda: 7, AddTextToAIAssistant=mock.Mock()))
    name = "_add_editor_selection_to_chat" if selection_kind == "code" else "_add_calculation_to_chat"
    tree = ast.parse(MODULE_PATH.read_text("utf8"))
    node = next(item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef) and item.name == name)
    scope = dict(vars(module), QtCore=SimpleNamespace(QCoreApplication=SimpleNamespace(
        translate=lambda context, text: text)))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(MODULE_PATH), "exec"), scope)
    store = References()
    with mock.patch.object(module._shader_source_references, "REFERENCES", store):
        if selection_kind == "code":
            scope[name](panel, "return 0.123;", document["sourcePath"], 2, 2, document["workspaceId"])
        else:
            scope[name](panel, document, {"title": "Response", "spans": [
                {"functionId": "main", "functionName": "Main", "startLine": 1, "endLine": 3}]})
        sent = panel.ctx.AddTextToAIAssistant.call_args.args
        facts = json.loads(sent[1])
        assert "return 0.123;" not in sent[1]
        assert "excerpts" not in facts and "selectedText" not in facts
        assert store.read(**facts["readSource"]["arguments"])["text"].strip().endswith(
            "return 0.123;" if selection_kind == "code" else "}")
