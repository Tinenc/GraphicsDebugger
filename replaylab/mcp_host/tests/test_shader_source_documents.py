"""Include edits are isolated files and invalidate publication, without compiling."""
import hashlib
import base64
import json
from pathlib import Path
from unittest import mock

import pytest
from test_shader_host_ide_finish import work
from src.shader_workspace import source_documents as documents
from src.shader_workspace.errors import WorkspaceWatchConflictError


def _open(work, header):
    return documents.open_source_document(work.manager, str(work.root),
                                           work.service.workspace_id, str(header))


def _save(work, opened, text):
    return documents.save_source_document(
        work.manager, str(work.root), work.service.workspace_id, opened["sourceDocumentId"],
        opened["rawSha256"], opened["rootSourceSha256"], text)


def test_local_header_save_never_replaces_main_or_compiles(work):
    header = work.service.source_path.with_name("lighting.hlsli")
    header.write_text("float Light() { return 1; }", encoding="utf-8")
    main = b'#include "lighting.hlsli"\n' + work.original
    work.service.source_path.write_bytes(main)
    opened = _open(work, header)
    with mock.patch.object(work.service, "compile", side_effect=AssertionError("no compile")):
        saved = _save(work, opened, "float Light() { return 2; }")
    assert saved["sourceOnly"] and saved["compileScheduled"] is False
    assert saved["sourcePath"] == str(header)
    assert header.read_text() == "float Light() { return 2; }"
    assert work.service.source_path.read_bytes() == main
    gate = json.loads((work.root / "ai/organize-code-gate.json").read_text())
    assert gate["decision"] == "drafting"


def test_external_same_names_and_transitive_edges_get_distinct_working_copies(work, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(); two.mkdir()
    child = one / "math.hlsli"
    child.write_text("float Child() { return 1; }")
    first, second = one / "common.hlsli", two / "common.hlsli"
    first.write_text('#include "math.hlsli"\nfloat A() { return Child(); }')
    second.write_text("float B() { return 2; }")
    originals = {item: item.read_bytes() for item in (first, second, child)}
    work.service.source_path.write_text('#include "%s"\n#include "%s"\n%s' % (
        first.as_posix(), second.as_posix(), work.original.decode()))
    opened = _open(work, first)
    other = _open(work, second)
    assert opened["sourcePath"] != other["sourcePath"]
    assert Path(opened["sourcePath"]).is_relative_to(work.root / "source")
    assert opened["mutationPaths"] == [str(work.service.source_path)]
    copied = Path(opened["sourcePath"])
    copy_graph = documents.capture_source_unit(copied)
    copied_child = Path(copy_graph["documents"][1]["path"])
    child_opened = _open(work, copied_child)
    assert child_opened["sourcePath"] == str(copied_child)
    assert child_opened["originPath"] == str(child)
    replacement = copied.read_text().replace("return Child();", "return Child() * 2;")
    saved = _save(work, opened, replacement)
    graph = documents.capture_source_unit(work.service.source_path)
    assert len(graph["documents"]) == 4
    assert all(Path(item["path"]).is_relative_to(work.root / "source") for item in graph["documents"])
    assert all(item.read_bytes() == before for item, before in originals.items())
    assert saved["rootSourceSha256"] != opened["rootSourceSha256"]
    assert saved["mutationPaths"] == []
    assert work.service.source_path.read_text().startswith('#include "includes/')


@pytest.mark.parametrize("changed", ["main", "header", "proposal"])
def test_include_conflicts_preserve_newer_edits(work, changed):
    header = work.service.source_path.with_name("math.hlsli")
    header.write_text("float F() { return 1; }")
    work.service.source_path.write_text('#include "math.hlsli"\n' + work.original.decode())
    opened = _open(work, header)
    newer = b"// newer\n"
    if changed == "main":
        work.service.source_path.write_bytes(newer + work.service.source_path.read_bytes())
    elif changed == "header":
        header.write_bytes(newer + header.read_bytes())
    else:
        session = work.manager.begin(str(work.root))
        Path(session["proposalSourcePath"]).write_bytes(newer + work.service.source_path.read_bytes())
    previous = (work.service.source_path.read_bytes(), header.read_bytes())
    with pytest.raises(WorkspaceWatchConflictError):
        _save(work, opened, "float F() { return 3; }")
    assert (work.service.source_path.read_bytes(), header.read_bytes()) == previous


def test_unrelated_path_is_not_a_definition_or_an_editable_include(work, tmp_path):
    unrelated = tmp_path / "shader.hlsl"
    unrelated.write_text("float Other() { return 1; }")
    with pytest.raises(ValueError, match="explicit include"):
        _open(work, unrelated)


def test_frozen_graph_does_not_depend_on_current_header_files(tmp_path):
    main, header = tmp_path / "shader.hlsl", tmp_path / "math.hlsli"
    main.write_text('#define MATH <math.hlsli>\n#include MATH\n// #include "absent.hlsli"\n')
    header.write_text("float Math() { return 1; }")
    first = documents.capture_source_unit(main)
    frozen = {documents._key(item["path"]): item["raw"] for item in first["documents"]}
    header.unlink()
    repeated = documents.capture_source_unit(main, document_reader=lambda path: frozen[documents._key(path)])
    assert repeated["digest"] == first["digest"]


def test_disabled_missing_include_does_not_prevent_opening_or_saving_active_header(work):
    header = work.service.source_path.with_name("math.hlsli")
    header.write_text("float F() { return 1; }")
    work.service.source_path.write_text('#if 0\n#include "not-installed.hlsli"\n#else\n'
                                      '#include "math.hlsli"\n#endif\n' + work.original.decode())
    opened = _open(work, header)
    saved = _save(work, opened, "float F() { return 2; }")
    assert saved["sourceEditable"] and saved["compileScheduled"] is False
    assert len(documents.capture_source_unit(work.service.source_path)["documents"]) == 2


@pytest.mark.parametrize("directives", [
    '#define HEADER "one.hlsli"\n#undef HEADER\n',
    '#define HEADER "one.hlsli"\n#if CONFIG\n#define HEADER "two.hlsli"\n#endif\n',
])
def test_ambiguous_or_undefined_include_macro_is_never_guessed(tmp_path, directives):
    main = tmp_path / "shader.hlsl"
    main.write_text(directives + '#include HEADER\n')
    for name in ("one.hlsli", "two.hlsli"):
        (tmp_path / name).write_text("// available but not a proven include target")
    with pytest.raises(ValueError, match="cannot be resolved explicitly"):
        documents.capture_source_unit(main)


def test_repeated_transitive_macro_include_cannot_reuse_first_context(tmp_path):
    main = tmp_path / "shader.hlsl"
    main.write_text('#define HEADER "a.hlsli"\n#include "wrapper.hlsli"\n'
                    '#define HEADER "b.hlsli"\n#include "wrapper.hlsli"\n')
    (tmp_path / "wrapper.hlsli").write_text('#include "indirect.hlsli"\n')
    (tmp_path / "indirect.hlsli").write_text('#include HEADER\n')
    (tmp_path / "a.hlsli").write_text("float A;")
    (tmp_path / "b.hlsli").write_text("float B;")
    with pytest.raises(ValueError, match="context-dependent macro include"):
        documents.capture_source_unit(main)


def test_unknown_parent_branch_cannot_define_a_certain_macro_through_header(tmp_path):
    main = tmp_path / "shader.hlsl"
    main.write_text('#if EXTERNAL_CONFIG\n#include "defines.hlsli"\n#endif\n#include HEADER\n')
    (tmp_path / "defines.hlsli").write_text('#define HEADER "a.hlsli"\n')
    (tmp_path / "a.hlsli").write_text("float A;")
    with pytest.raises(ValueError, match="cannot be resolved explicitly"):
        documents.capture_source_unit(main)


def test_project_commit_guard_prevents_any_external_copy_write(work, tmp_path):
    header = tmp_path / "external.hlsli"
    header.write_text("float F() { return 1; }")
    work.service.source_path.write_text('#include "%s"\n' % header.as_posix() + work.original.decode())
    def rejected():
        raise RuntimeError("project changed")
    with pytest.raises(RuntimeError, match="project changed"):
        documents.open_source_document(work.manager, str(work.root), work.service.workspace_id,
                                        str(header), commit_guard=rejected)
    assert not (work.root / "source/includes").exists()


def test_include_save_can_clear_a_header_without_compiling(work):
    header = work.service.source_path.with_name("empty.hlsli")
    header.write_text("// remove this obsolete helper\n")
    work.service.source_path.write_text('#include "empty.hlsli"\n' + work.original.decode())
    opened = _open(work, header)
    saved = _save(work, opened, "")
    assert saved["size"] == 0 and header.read_bytes() == b""
    assert len(documents.capture_source_unit(work.service.source_path)["documents"]) == 2


@pytest.mark.parametrize("invalid", ['#include "missing.hlsli"\n', '#include UNRESOLVED\n'])
def test_incomplete_header_remains_editable_and_can_be_fixed(work, invalid):
    header = work.service.source_path.with_name("editable.hlsli")
    header.write_text("float F() { return 1; }")
    work.service.source_path.write_text('#include "editable.hlsli"\n' + work.original.decode())
    opened = _open(work, header)
    with mock.patch.object(work.service, "compile", side_effect=AssertionError("no compile")):
        saved = _save(work, opened, invalid)
        assert saved["compileScheduled"] is False and header.read_text() == invalid
        with pytest.raises(ValueError):
            documents.capture_source_unit(work.service.source_path)
        partial = documents.capture_source_unit(work.service.source_path, allow_incomplete=True)
        assert partial["complete"] is False and partial["digest"] == ""
        reopened = _open(work, header)
        fixed = _save(work, reopened, "float F() { return 2; }")
    assert fixed["sourceOnly"] and len(documents.capture_source_unit(work.service.source_path)["documents"]) == 2


@pytest.mark.parametrize("operation", ["open", "save"])
def test_interrupted_header_save_is_not_recovered_after_project_switch(work, operation):
    header = work.service.source_path.with_name("math.hlsli")
    before, after = b"float F() { return 1; }", b"float F() { return 2; }"
    header.write_bytes(before)
    work.service.source_path.write_text('#include "math.hlsli"\n' + work.original.decode())
    opened = _open(work, header)
    header.write_bytes(after)
    journal = work.root / "ai/source-document-save.json"
    journal.write_text(json.dumps({"schema": "bread-shader-source-document-save/v1", "files": [
        {"path": "source/math.hlsli", "before": base64.b64encode(before).decode(),
         "after": base64.b64encode(after).decode()}]}))
    def rejected():
        raise RuntimeError("project changed")
    with pytest.raises(RuntimeError, match="project changed"):
        if operation == "open":
            documents.open_source_document(work.manager, str(work.root), work.service.workspace_id,
                                           str(header), commit_guard=rejected)
        else:
            documents.save_source_document(work.manager, str(work.root), work.service.workspace_id,
                opened["sourceDocumentId"], opened["rawSha256"], opened["rootSourceSha256"],
                "float F() { return 3; }", commit_guard=rejected)
    assert header.read_bytes() == after and journal.exists()


def test_external_include_partial_save_rolls_back_all_changed_files(work, tmp_path):
    from src.shader_workspace import organization
    external = tmp_path / "math.hlsli"
    external.write_text("float F() { return 1; }")
    original_main = ('#include "%s"\n' % external.as_posix()).encode() + work.original
    work.service.source_path.write_bytes(original_main)
    opened = _open(work, external)
    copied = Path(opened["sourcePath"])
    before = copied.read_bytes()
    real = organization._source_compare_and_swap
    def fail_parent(path, expected, replacement, label):
        if path == work.service.source_path and replacement != original_main:
            return False
        return real(path, expected, replacement, label)
    with mock.patch.object(organization, "_source_compare_and_swap", side_effect=fail_parent):
        with pytest.raises(WorkspaceWatchConflictError):
            _save(work, opened, "float F() { return 2; }")
    assert copied.read_bytes() == before
    assert work.service.source_path.read_bytes() == original_main
    assert not (work.root / "ai/source-document-save.json").exists()


def test_include_console_reader_preserves_owner_and_refreshes_only_header(tmp_path):
    import os
    from test_shader_console import console, _saved_draft_preview_fixture
    fixture, unused_gate = _saved_draft_preview_fixture(tmp_path)
    header = fixture.source.with_name("math.hlsli")
    header.write_text("float Math() { return 1; }", encoding="utf-8")
    identity = hashlib.sha256((fixture.WORKSPACE_ID + "\0" +
        os.path.normcase(os.path.abspath(header))).encode()).hexdigest()
    receipt = dict(schema="bread-shader-source-document/v1", sourceDocumentId=identity,
        sourceWorkspaceId=fixture.WORKSPACE_ID, originPath=str(header),
        sourceRelativePath="source/math.hlsli", mutationPaths=[])
    path = fixture.workspace / "ai/source-documents" / (identity + ".json")
    path.parent.mkdir()
    path.write_text(json.dumps(receipt))
    metadata = dict(receipt, workspacePath=str(fixture.workspace), sourcePath=str(header),
        rawSha256=hashlib.sha256(header.read_bytes()).hexdigest())
    opened = console.load_related_shader_source_document(metadata, fixture.capture_binding)
    assert opened["sourceWorkspaceId"] == fixture.WORKSPACE_ID
    assert opened["workspaceId"] == identity and opened["readOnly"] is False
    header.write_text("", encoding="utf-8")
    refreshed = console.load_related_shader_source_document(opened, fixture.capture_binding, refresh=True)
    assert refreshed["text"] == "" and refreshed["sourcePath"] == str(header)
    with pytest.raises(console.ShaderConsoleError, match="changed"):
        console.load_related_shader_source_document(opened, fixture.capture_binding)
    with pytest.raises(console.ShaderConsoleError, match="path identity"):
        console.load_related_shader_source_document(dict(metadata, sourcePath=str(fixture.source)), fixture.capture_binding)


def test_include_client_routes_header_identity_and_rejects_foreign_receipt(tmp_path):
    from test_shader_editor_client import client_module
    client = client_module.ReplayLabShaderSaveClient()
    text = "float F() { return 1; }"
    result = dict(schema="bread-shader-source-document/v1", sourceWorkspaceId="a" * 32,
                  sourceDocumentId="b" * 64, sourceOnly=True, compileScheduled=False,
                  rawSha256=hashlib.sha256(text.encode()).hexdigest())
    binding = {"captureSessionId": "c" * 32}
    with mock.patch.object(client, "live_edit_operation", return_value=result) as call:
        assert client.open_source_document(str(tmp_path), "a" * 32, str(tmp_path / "f.hlsli"), binding) == result
        assert call.call_args.args[0] == "open-source-document"
        assert client.save_source_document(str(tmp_path), "a" * 32, "b" * 64, "d" * 64, "e" * 64,
                                            text, binding) == result
        assert call.call_args.args[0] == "save-source-document"
        assert call.call_args.args[1]["document_id"] == "b" * 64
        result["sourceWorkspaceId"] = "f" * 32
        with pytest.raises(client_module.ReplayLabShaderSaveError):
            client.save_source_document(str(tmp_path), "a" * 32, "b" * 64, "d" * 64, "e" * 64, text, binding)
