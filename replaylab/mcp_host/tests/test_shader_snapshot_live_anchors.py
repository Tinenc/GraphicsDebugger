"""UI anchor lifecycle against the real persistent store; native edits are checked separately."""
import importlib.util
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(BRIDGE / filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


backend = load("snapshot_live_backend", "shader_visual_snapshots.py")
ui = load("snapshot_live_ui", "project_shader_debug_snapshots.py")
SOURCE = "// 法线\nfloat helper(float x) { float rim = x; return rim; }\n"
IDENTITY = {"workspace": "workspace", "path": "/shader.hlsl", "stage": "Pixel",
            "originalShader": "a" * 64}


def selection(source, expression="rim", last=True):
    data = source.encode("utf-8")
    start = data.rindex(expression.encode("utf-8")) if last else data.index(expression.encode("utf-8"))
    return {"byteStart": start, "byteEnd": start + len(expression.encode("utf-8"))}


class Editor:
    """Supply explicit native identities, without duplicating Scintilla edit mapping."""
    def __init__(self, source):
        self.source = source
        self.ranges = {}
        self.next_handle = 0

    def GetText(self):
        return self.source

    def CreateRangeAnchor(self, start, end):
        self.next_handle += 1
        self.ranges[self.next_handle] = [start, end]
        return self.next_handle

    def GetRangeAnchorRange(self, handle):
        return self.ranges.get(handle, [])

    def RemoveRangeAnchor(self, handle):
        self.ranges.pop(handle, None)


@pytest.fixture
def state(tmp_path):
    record = backend.make_snapshot(SOURCE, selection(SOURCE), IDENTITY, name="轮廓光")
    store = backend.SnapshotStore(str(tmp_path), IDENTITY)
    store.add(record)
    document = {"editor": Editor(SOURCE), "baselineEditorText": SOURCE}
    live = ui.LiveSnapshotAnchors(backend, store, document, IDENTITY)
    return live, store, document, record


def edit(state, source, expression="rim", deleted=False):
    live, store, document, record = state
    document["editor"].source = source
    handle = live.handles[record["id"]]
    positions = selection(source, expression) if not deleted else None
    document["editor"].ranges[handle] = [] if positions is None else list(positions.values())
    live.flush()
    return store.list()[0]


def test_same_text_delete_paste_persists_actual_native_deletion(state):
    record = edit(state, SOURCE, deleted=True)
    assert record["anchor"]["invalidated"] == "snapshot-expression-deleted"
    assert state[0].resolve(record, SOURCE)["reason"] == "snapshot-expression-deleted"


def test_discard_unsaved_deletion_restores_saved_identity(state):
    live, store, document, original = state
    edit(state, SOURCE.replace("return rim;", "return 0;"), deleted=True)
    live.reset(SOURCE)
    assert backend.resolve_snapshot(store.list()[0], SOURCE, IDENTITY)["status"] == "resolved"


@pytest.mark.parametrize("same_bytes", [False, True])
def test_saved_deletion_never_revives_after_reload(state, same_bytes):
    live, store, document, original = state
    source = SOURCE if same_bytes else SOURCE.replace("return rim;", "return 0;")
    edit(state, source, deleted=True)
    document["pendingSave"] = {"saveToken": 1}
    live.submitted()
    document["baselineEditorText"] = source
    live.saved(source)
    document["pendingSave"] = None
    live.reset(source)
    assert store.list()[0]["anchor"]["invalidated"] == "snapshot-expression-deleted"
    assert backend.resolve_snapshot(store.list()[0], source, IDENTITY)["status"] == "unresolved"


def test_incomplete_reload_recovers_in_same_editor_when_syntax_is_repaired(state):
    live, store, document, original = state
    incomplete = SOURCE.replace("}", "")
    live.reset(incomplete)
    document["editor"] = Editor(incomplete)
    live = ui.LiveSnapshotAnchors(backend, store, document, IDENTITY)
    assert live.handles[original["id"]] is None
    assert live.resolve(original, incomplete)["reason"] != "snapshot-expression-deleted"
    assert not store.list()[0]["anchor"].get("invalidated")
    document["editor"].source = SOURCE
    live.flush()
    assert live.resolve(store.list()[0], SOURCE)["status"] == "resolved"
    assert isinstance(live.handles[original["id"]], int)


def test_deleted_live_identity_does_not_retry_and_capture_same_name(state):
    live, store, document, original = state
    edit(state, SOURCE.replace("return rim;", "return 0;"), deleted=True)
    document["editor"].source = SOURCE + "// source edited again\n"
    live.flush()
    assert live.resolve(store.list()[0], document["editor"].source)["status"] == "unresolved"
    assert document["editor"].next_handle == 1


def test_live_rename_preserves_metadata_and_updates_current_expression(state):
    source = SOURCE.replace("return rim;", "return glow;")
    record = edit(state, source, "glow")
    assert record["expression"] == "glow"
    assert record["name"] == "轮廓光"
    assert record["id"] == state[3]["id"]
    assert state[0].resolve(record, source)["selection"]["expression"] == "glow"


def test_save_completion_uses_submitted_identity_not_later_edits(state):
    live, store, document, original = state
    saved_source = SOURCE.replace("x;", "x * 2;")
    edit(state, saved_source)
    document["pendingSave"] = {"saveToken": 3}
    live.submitted()
    edit(state, saved_source.replace("return rim;", "return 0;"), deleted=True)
    document["baselineEditorText"] = saved_source
    live.saved(saved_source)
    document["pendingSave"] = None
    live.reset(saved_source)
    assert backend.resolve_snapshot(store.list()[0], saved_source, IDENTITY)["status"] == "resolved"


def test_same_source_save_tokens_do_not_reuse_old_identity_checkpoint(state):
    live, store, document, original = state
    document["pendingSave"] = {"saveToken": 1}
    live.submitted()
    live.saved(SOURCE)
    edit(state, SOURCE, deleted=True)
    document["pendingSave"] = {"saveToken": 2}
    live.submitted()
    live.saved(SOURCE)
    document["pendingSave"] = None
    live.reset(SOURCE)
    assert store.list()[0]["anchor"]["invalidated"] == "snapshot-expression-deleted"


def test_discard_preserves_concurrent_rename_and_does_not_recreate_removed_snapshot(state):
    live, store, document, original = state
    edit(state, SOURCE.replace("return rim;", "return 0;"), deleted=True)
    store.rename(original["id"], "改名后")
    live.reset(SOURCE)
    assert store.list()[0]["name"] == "改名后"
    live = ui.LiveSnapshotAnchors(backend, store, document, IDENTITY)
    store.remove(original["id"])
    live.reset(SOURCE)
    assert store.list() == []


def test_incomplete_edit_keeps_native_identity_and_recovers_after_repair(state):
    live, store, document, original = state
    edit(state, SOURCE.replace("}", ""))
    assert not store.list()[0]["anchor"].get("invalidated")
    record = edit(state, SOURCE)
    assert live.resolve(record, SOURCE)["status"] == "resolved"


def test_no_native_api_uses_strict_durable_resolution(tmp_path):
    store = backend.SnapshotStore(str(tmp_path), IDENTITY)
    record = backend.make_snapshot(SOURCE, selection(SOURCE), IDENTITY)
    store.add(record)
    editor = type("OldEditor", (), {"GetText": lambda self: SOURCE})()
    live = ui.LiveSnapshotAnchors(backend, store, {"editor": editor}, IDENTITY)
    live.flush()
    assert live.resolve(record, SOURCE)["status"] == "resolved"
