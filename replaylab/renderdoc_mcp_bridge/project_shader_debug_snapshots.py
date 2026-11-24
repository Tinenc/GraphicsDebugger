"""Shader Workspace snapshot list; stores locations and settings, never pixels."""
from __future__ import absolute_import

import hashlib
import importlib.util
import os
import copy

try:
    from PySide2 import QtCore, QtWidgets
except ImportError:
    QtCore = QtWidgets = None


def snapshot_identity(document, workspace):
    path = os.path.normcase(os.path.realpath(str(document.get("sourcePath") or workspace.get("sourcePath") or "")))
    return {"workspace": str(workspace.get("workspacePath") or document.get("workspaceId") or ""),
            "path": path, "stage": str(workspace.get("stage") or "pixel"),
            "originalShader": str(workspace.get("shaderRawSha256") or workspace.get("resourceId") or "")}


def _backend():
    try:
        from . import shader_visual_snapshots
        return shader_visual_snapshots
    except ImportError:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shader_visual_snapshots.py")
        spec = importlib.util.spec_from_file_location("_bread_visual_snapshots", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def snapshot_error_message(error):
    code = str(getattr(error, "code", ""))
    if code in ("snapshot-invalid-identity", "snapshot-identity-mismatch"):
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "The current Shader identity is unavailable or does not match this snapshot.")
    if code in ("snapshot-anchor-unavailable", "snapshot-invalid-selection", "snapshot-invalid-source"):
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "This location cannot be saved. Select a value inside a function and try again.")
    if code == "snapshot-invalid-options":
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "The snapshot has unsupported display settings. Choose valid debug options and save it again.")
    if code == "snapshot-size-limit":
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "The snapshot size or count limit was reached. Remove unused snapshots or select a smaller expression.")
    if code == "snapshot-store-busy":
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "Another update is saving snapshots. Try again in a moment.")
    if code in ("snapshot-store-invalid", "snapshot-invalid-record", "snapshot-invalid-metadata", "snapshot-duplicate-id"):
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "The snapshot data is invalid. Existing snapshots were left unchanged.")
    if code == "snapshot-store-read-failed":
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "Could not read debug snapshots. See the diagnostic for details.")
    if code == "snapshot-store-write-failed":
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "Could not save debug snapshots. See the diagnostic for details.")
    if code in ("snapshot-not-found", "snapshot-invalid-order"):
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "The snapshot list changed. Reopen the list and try again.")
    return QtCore.QCoreApplication.translate("ReplayLabProjects", "Debug snapshot unavailable. See the diagnostic for details.")


def snapshot_location_message(reason):
    if reason == "snapshot-expression-deleted":
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "This expression was deleted. Undo the edit or select a new snapshot location.")
    if reason == "snapshot-anchor-ambiguous":
        return QtCore.QCoreApplication.translate("ReplayLabProjects", "More than one location matches this snapshot. Select the intended location to create a new snapshot.")
    return QtCore.QCoreApplication.translate("ReplayLabProjects", "This snapshot is temporarily unavailable in the current source. Its location will be checked again as you edit.")


class LiveSnapshotAnchors(object):
    """Editor positions are authoritative until an explicit buffer reload.

    A missing live range is a deletion, never a request to search for another
    occurrence. Durable token anchors are only used when opening a document.
    """
    def __init__(self, module, store, document, identity):
        self.module, self.store, self.document, self.identity = module, store, document, identity
        self.editor = document["editor"]
        self.handles = {}
        self.pending_reasons = {}
        self.records = {}
        self.baseline_source = str(document.get("baselineEditorText", self.editor.GetText()))
        self.baseline_records = {}
        self.source = str(self.editor.GetText())
        self.supported = all(callable(getattr(self.editor, name, None)) for name in
                             ("CreateRangeAnchor", "GetRangeAnchorRange", "RemoveRangeAnchor"))
        self.sync_records(store.list())
        self.baseline_records = copy.deepcopy(self.records)
        self.fingerprint = self._fingerprint()
        self.saved_versions = {}

    def _fingerprint(self):
        return tuple((key, tuple(self.editor.GetRangeAnchorRange(handle)) if handle is not None else ())
                     for key, handle in sorted(self.handles.items())) if self.supported else ()

    def sync_records(self, records):
        latest = {record["id"]: record for record in records}
        for snapshot_id in set(self.handles) - set(latest):
            handle = self.handles.pop(snapshot_id)
            self.pending_reasons.pop(snapshot_id, None)
            if handle is not None:
                self.editor.RemoveRangeAnchor(handle)
        source = str(self.editor.GetText())
        for snapshot_id, record in latest.items():
            # None means that this editor has never established an identity
            # (e.g. it opened while a function was temporarily incomplete).
            # Retry only those unresolved records after text changes. An int
            # whose native range is now empty is an actual deletion and must
            # never be rebound to another same-named occurrence.
            pending = (snapshot_id in self.handles and self.handles[snapshot_id] is None
                       and not record.get("anchor", {}).get("invalidated")
                       and source != self.source)
            if (snapshot_id not in self.handles or pending) and self.supported:
                resolved = self.module.resolve_snapshot(record, source, self.identity)
                handle = None
                if resolved.get("status") == "resolved":
                    selection = resolved["selection"]
                    candidate = self.editor.CreateRangeAnchor(selection["byteStart"], selection["byteEnd"])
                    # A stale native binding or a mocked editor is not a valid handle.
                    if isinstance(candidate, int) and candidate >= 0:
                        handle = candidate
                self.handles[snapshot_id] = handle
                self.pending_reasons[snapshot_id] = resolved.get("reason", "snapshot-anchor-unavailable")
        self.records = latest

    def selection(self, snapshot_id, source):
        handle = self.handles.get(snapshot_id)
        if handle is None:
            return None
        positions = list(self.editor.GetRangeAnchorRange(handle))
        data = source.encode("utf-8")
        if len(positions) != 2 or not 0 <= positions[0] < positions[1] <= len(data):
            return None
        start, end = positions
        return {"byteStart": start, "byteEnd": end,
                "line": data[:start].count(b"\n") + 1,
                "expression": data[start:end].decode("utf-8")}

    def resolve(self, record, source):
        if not self.supported:
            return self.module.resolve_snapshot(record, source, self.identity)
        selected = self.selection(record["id"], source)
        if selected is None:
            reason = (self.pending_reasons.get(record["id"], "snapshot-anchor-unavailable")
                      if self.handles.get(record["id"]) is None else "snapshot-expression-deleted")
            return {"status": "unresolved", "reason": reason}
        try:
            updated = self.module.reanchor_snapshot(record, source, selected, self.identity)
        except self.module.SnapshotError as exc:
            return {"status": "unresolved", "reason": exc.code}
        return {"status": "resolved", "selection": selected,
                "member": updated.get("member", ""), "options": updated.get("options", {})}

    def flush(self):
        source = str(self.editor.GetText())
        if source != self.source:
            self.sync_records(self.records.values())
        fingerprint = self._fingerprint()
        if (source == self.source and fingerprint == self.fingerprint) or not self.supported:
            return
        changes = {snapshot_id: self.selection(snapshot_id, source)
                   for snapshot_id in self.records if self.handles.get(snapshot_id) is not None}
        if changes:
            try:
                self.records = {record["id"]: record for record in self.store.update_anchors(source, changes)}
            except self.module.SnapshotError as exc:
                # A temporarily unfinished expression must not prevent an actual
                # deletion from being persisted. Keep its live handle for Undo.
                deleted = {key: value for key, value in changes.items() if value is None}
                if deleted:
                    self.records = {record["id"]: record for record in self.store.update_anchors(source, deleted)}
                if exc.code not in ("snapshot-invalid-selection", "snapshot-invalid-source",
                                    "snapshot-anchor-unavailable"):
                    raise
                return
        self.source = source
        self.fingerprint = fingerprint
        self.checkpoint()

    def submitted(self):
        self.flush()
        source = str(self.editor.GetText())
        self.saved_versions[self._save_key(source)] = copy.deepcopy(self.records)
        # At most one save is active and one queued; retain a little extra room
        # without keeping an unbounded edit history in Python.
        while len(self.saved_versions) > 4:
            del self.saved_versions[next(iter(self.saved_versions))]

    def _save_key(self, source):
        return (hashlib.sha256(source.encode("utf-8")).hexdigest(),
                (self.document.get("pendingSave") or {}).get("saveToken", 0))

    def saved(self, source):
        """A committed save is an identity checkpoint even when its bytes are unchanged."""
        source = str(source)
        saved = self.saved_versions.get(self._save_key(source))
        if saved is None and source == str(self.editor.GetText()):
            self.flush()
            saved = self.records
        if saved is not None:
            self.baseline_records = copy.deepcopy(saved)
            self.baseline_source = source
        else:
            self.checkpoint()

    def checkpoint(self):
        baseline = str(self.document.get("baselineEditorText", self.baseline_source))
        if baseline == self.baseline_source:
            return
        if baseline == str(self.editor.GetText()):
            self.baseline_records = copy.deepcopy(self.records)
            self.baseline_source = baseline
            return
        saved = self.saved_versions.get(self._save_key(baseline))
        if saved is not None:
            self.baseline_records = copy.deepcopy(saved)
            self.baseline_source = baseline
            return
        for snapshot_id, record in self.records.items():
            if record.get("anchor", {}).get("invalidated"):
                self.baseline_records[snapshot_id] = copy.deepcopy(record)
                continue
            resolved = self.module.resolve_snapshot(record, baseline, self.identity)
            if resolved.get("status") == "resolved":
                self.baseline_records[snapshot_id] = self.module.reanchor_snapshot(
                    record, baseline, resolved["selection"], self.identity)
        self.baseline_source = baseline

    def reset(self, source):
        """Discard edits/reload: restore checkpoints before native SetText clears IDs."""
        self.checkpoint()
        self.store.restore_anchors(list(self.baseline_records.values()))
        changes = {}
        for record in self.store.list():
            original = self.baseline_records.get(record["id"], record)
            resolved = self.module.resolve_snapshot(original, source, self.identity)
            if resolved.get("status") == "resolved":
                changes[record["id"]] = resolved["selection"]
        if changes:
            self.store.update_anchors(source, changes)
        self.close()

    def close(self):
        for handle in self.handles.values():
            if handle is not None:
                self.editor.RemoveRangeAnchor(handle)
        self.handles.clear()
        self.pending_reasons.clear()


if QtWidgets is not None:
    class ShaderDebugSnapshots(QtWidgets.QWidget):
        def __init__(self, panel, parent):
            super(ShaderDebugSnapshots, self).__init__(parent)
            self.panel = panel
            self.setObjectName("breadShaderDebugSnapshots")
            self.setMinimumWidth(210)
            self.setMaximumWidth(480)
            self._store = None
            self._identity = None
            self._signature = None
            self._rows = {}
            self._updating = False
            self._module = None
            self._live_documents = {}
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(4, 4, 4, 4)
            self.status = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Snapshots for the current Shader. Click to locate and debug; drag to reorder."), self)
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            self.list = QtWidgets.QListWidget(self)
            self.list.setObjectName("breadShaderDebugSnapshotList")
            self.list.setAccessibleName(QtCore.QCoreApplication.translate("ReplayLabProjects", "Debug snapshots"))
            self.list.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
            self.list.setDefaultDropAction(QtCore.Qt.MoveAction)
            self.list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            self.list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.list.customContextMenuRequested.connect(self.context_menu)
            self.list.itemClicked.connect(self.activate)
            self.list.itemActivated.connect(self.activate)
            # A native InternalMove drop emits rowsMoved before dropEvent has
            # unwound. Rebuild/renumber only after that operation is complete.
            # Parenting the single-shot timer also cancels it on panel close.
            self.reorder_timer = QtCore.QTimer(self)
            self.reorder_timer.setSingleShot(True)
            self.reorder_timer.timeout.connect(self.reordered)
            self.list.model().rowsMoved.connect(self._queue_reorder)
            layout.addWidget(self.list, 1)
            buttons = QtWidgets.QHBoxLayout()
            self.rename_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate("ReplayLabProjects", "Rename"), self)
            self.rename_button.clicked.connect(self.rename_current)
            buttons.addWidget(self.rename_button)
            self.delete_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate("ReplayLabProjects", "Delete"), self)
            self.delete_button.clicked.connect(self.delete_current)
            buttons.addWidget(self.delete_button)
            layout.addLayout(buttons)
            self.timer = QtCore.QTimer(self)
            self.timer.setInterval(300)
            self.timer.timeout.connect(self._tick)
            self.timer.start()

        def _current(self):
            document = self.panel._current_document()
            if not document or not document.get("available"):
                return None, None
            return document, self.panel._document_model(document.get("workspaceId")) or document.get("workspace") or {}

        def _open_store(self, document, workspace):
            identity = snapshot_identity(document, workspace)
            if not identity["originalShader"]:
                raise ValueError("Shader identity is unavailable")
            if self._module is None:
                self._module = _backend()
            if identity != self._identity:
                root = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppLocalDataLocation)
                if not root:
                    raise ValueError("Application data directory is unavailable")
                self._store = self._module.SnapshotStore(os.path.join(root, "shader_debug_snapshots"), identity)
                self._identity = identity
            return self._store

        def _error(self, error):
            self.status.setText(snapshot_error_message(error))
            self.status.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Diagnostic: %1").replace("%1", str(error)))

        def bind_document(self, document, workspace=None):
            key = document.get("documentToken", id(document))
            identity = snapshot_identity(document, workspace or document.get("workspace") or {})
            live = self._live_documents.get(key)
            if live is not None and (live.editor is not document.get("editor") or live.identity != identity):
                live.close()
                live = None
            if live is None:
                store = self._open_store(document, workspace or document.get("workspace") or {})
                live = LiveSnapshotAnchors(self._module, store, document, identity)
                self._live_documents[key] = live
            return live

        def source_changed(self, document):
            # Native tracking is synchronous. Persistence is debounced even when
            # the sidebar is hidden or another document becomes current.
            self._signature = None

        def submitted(self, document):
            live = self._live_documents.get(document.get("documentToken", id(document)))
            if live is not None:
                try:
                    live.submitted()
                except Exception as exc:
                    self._error(exc)

        def saved(self, document, source):
            live = self._live_documents.get(document.get("documentToken", id(document)))
            if live is not None:
                try:
                    live.saved(source)
                except Exception as exc:
                    self._error(exc)

        def release_document(self, document, reset_source=None):
            live = self._live_documents.pop(document.get("documentToken", id(document)), None)
            if live is not None:
                try:
                    if reset_source is not None:
                        live.reset(str(reset_source))
                    else:
                        live.flush()
                except Exception as exc:
                    self._error(exc)
                finally:
                    live.close()

        def _tick(self):
            for live in list(self._live_documents.values()):
                try:
                    live.flush()
                    live.checkpoint()
                except Exception as exc:
                    self._error(exc)
            self.refresh()

        def refresh(self, force=False):
            if not force and not self.isVisible():
                return
            document, workspace = self._current()
            if document is None:
                self.list.clear()
                self._rows = {}
                self._signature = None
                self.status.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Open a Shader to view its debug snapshots."))
                return
            source = str(document["editor"].GetText())
            signature = (tuple(sorted(snapshot_identity(document, workspace).items())),
                         hashlib.sha256(source.encode("utf-8")).hexdigest())
            if not force and signature == self._signature:
                return
            try:
                store = self._open_store(document, workspace)
                snapshots = store.list()
                live = self.bind_document(document, workspace)
                live.flush()
                snapshots = store.list()
                live.sync_records(snapshots)
                current = self.list.currentItem()
                selected_id = current.data(QtCore.Qt.UserRole) if current is not None else None
                self._updating = True
                self.list.clear()
                self._rows = {}
                for number, snapshot in enumerate(snapshots, 1):
                    resolved = live.resolve(snapshot, source)
                    selection = resolved.get("selection") or {}
                    expression = str(selection.get("expression") or snapshot.get("expression") or "") + str(snapshot.get("member") or "")
                    name = str(snapshot.get("name") or "")
                    value = name + " · " + expression if name else expression  # i18n-ignore: user-authored name and expression separated by punctuation.
                    if resolved.get("status") == "resolved":
                        label = QtCore.QCoreApplication.translate("ReplayLabProjects", "%1 · Line %2 · %3").replace(
                            "%1", str(number)).replace("%2", str(selection.get("line", ""))).replace("%3", value)
                    else:
                        label = QtCore.QCoreApplication.translate("ReplayLabProjects", "%1 · Unresolved · %2").replace(
                            "%1", str(number)).replace("%2", value)
                    item = QtWidgets.QListWidgetItem(label, self.list)
                    item.setData(QtCore.Qt.UserRole, snapshot["id"])
                    item.setToolTip(expression if resolved.get("status") == "resolved" else
                                    snapshot_location_message(resolved.get("reason")))
                    self._rows[snapshot["id"]] = (snapshot, resolved)
                    if snapshot["id"] == selected_id:
                        self.list.setCurrentItem(item)
                self._signature = signature
                self.status.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Snapshots for the current Shader. Click to locate and debug; drag to reorder.") if snapshots else QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "No debug snapshots. Start visual debugging, then press Ctrl+Shift+B."))
            except Exception as exc:
                self._error(exc)
            finally:
                self._updating = False

        def add_current(self):
            controller = self.panel.visual_debug
            request, selection = controller.request, controller.selection
            document, workspace = self._current()
            if (not request or not selection or document is None or
                    request.get("workspaceId") != document.get("workspaceId") or
                    not controller.state.accepts(request, document)):
                controller.label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Start visual debugging on the current source before saving a snapshot."))
                return
            try:
                store = self._open_store(document, workspace)
                snapshot = self._module.make_snapshot(str(document["editor"].GetText()), selection,
                    self._identity, member=str(request.get("member") or ""), options=controller._options({}))
                store.add(snapshot)
                self.bind_document(document, workspace).sync_records(store.list())
                self.panel._toggle_debug_snapshots(True)
                self.refresh(True)
                self.status.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Debug snapshot saved. It will use the latest source when opened."))
            except Exception as exc:
                self._error(exc)

        def activate(self, item):
            if item is None:
                return
            snapshot_id = item.data(QtCore.Qt.UserRole)
            row = self._rows.get(snapshot_id)
            document, workspace = self._current()
            if row is None or document is None:
                return
            try:
                # Always resolve the current buffer, including unsaved edits.
                live = self.bind_document(document, workspace)
                live.flush()
                resolved = live.resolve(row[0], str(document["editor"].GetText()))
                if resolved.get("status") != "resolved":
                    self.refresh(True)
                    self.status.setText(snapshot_location_message(resolved.get("reason")))
                    return
                selection = resolved["selection"]
                if callable(getattr(document["editor"], "SelectRange", None)):
                    document["editor"].SelectRange(selection["byteStart"], selection["byteEnd"])
                else:
                    document["editor"].GoToLine(int(selection["line"]), 1)
                self.panel.visual_debug.begin(document["workspaceId"], selection["byteStart"], selection["byteEnd"], snapshot=resolved)
            except Exception as exc:
                self._error(exc)

        def rename_current(self, *_unused):
            item = self.list.currentItem()
            row = self._rows.get(item.data(QtCore.Qt.UserRole)) if item is not None else None
            if row is None:
                return
            name, accepted = QtWidgets.QInputDialog.getText(self,
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Rename debug snapshot"),
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Snapshot name:"),
                QtWidgets.QLineEdit.Normal, str(row[0].get("name") or ""))
            if accepted:
                try:
                    self._store.rename(row[0]["id"], str(name))
                    self.refresh(True)
                except Exception as exc:
                    self._error(exc)

        def delete_current(self, *_unused):
            item = self.list.currentItem()
            if item is not None and self._store is not None:
                try:
                    self._store.remove(item.data(QtCore.Qt.UserRole))
                    self.refresh(True)
                except Exception as exc:
                    self._error(exc)

        def reordered(self, *_unused):
            if not self._updating and self._store is not None:
                try:
                    self._store.reorder([self.list.item(i).data(QtCore.Qt.UserRole) for i in range(self.list.count())])
                    self.refresh(True)
                except Exception as exc:
                    self._error(exc)

        def _queue_reorder(self, *_unused):
            if not self._updating:
                self.reorder_timer.start(0)

        def context_menu(self, position):
            item = self.list.itemAt(position)
            if item is None:
                return
            self.list.setCurrentItem(item)
            menu = QtWidgets.QMenu(self)
            menu.addAction(QtCore.QCoreApplication.translate("ReplayLabProjects", "Rename"), self.rename_current)
            menu.addAction(QtCore.QCoreApplication.translate("ReplayLabProjects", "Delete"), self.delete_current)
            menu.exec_(self.list.viewport().mapToGlobal(position))
