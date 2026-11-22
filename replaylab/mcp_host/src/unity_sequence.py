"""Persist the requested EID scope; derive progress from actual restoration receipts."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from .capture_source import capture_source, path_key, same_source
from .shader_workspace.io import atomic_write_json
from .unity_restoration import RestorationStore, _json, _ID


def selected_actions(actions: dict, event_ids: list[int], ranges: list[list[int]]) -> list[dict]:
    if any(type(value) is not int or value <= 0 for value in event_ids):
        raise ValueError("event_ids requires positive EIDs")
    if any(not isinstance(pair, list) or len(pair) != 2 or
           any(type(value) is not int or value <= 0 for value in pair) or pair[0] > pair[1] for pair in ranges):
        raise ValueError("event_ranges requires inclusive [first, last] pairs")
    if not event_ids and not ranges:
        raise ValueError("Select EIDs or EID ranges")
    found = {}

    def visit(rows):
        for row in rows:
            eid = row.get("eventId")
            flags = row.get("flags", 0)
            executable = bool(flags & (2 | 4 | 8 | 0x4000)) if type(flags) is int else any(
                name in str(flags) for name in ("Drawcall", "Dispatch", "MeshDispatch", "DispatchRay"))
            if type(eid) is int and executable and (eid in event_ids or any(a <= eid <= b for a, b in ranges)):
                found[eid] = {key: row[key] for key in ("eventId", "name", "flags", "markerPath", "outputs") if key in row}
            visit(row.get("children") or [])
    visit(actions.get("actions") or [])
    missing = sorted(set(event_ids) - found.keys())
    if missing:
        raise ValueError("Selected EIDs are not draw/dispatch actions: " + str(missing))
    if not found:
        raise ValueError("No executable draw/dispatch in the selected scope")
    return [found[eid] for eid in sorted(found)]


class UnitySequenceStore:
    def __init__(self, store: RestorationStore):
        self.store = store
        self.root = store.root.parent / "sequences"

    def _path(self, sequence_id):
        if not isinstance(sequence_id, str) or not _ID.fullmatch(sequence_id):
            raise ValueError("Invalid Unity sequence ID")
        return self.root / (sequence_id + ".json")

    def create(self, capture: dict, project: str, actions: dict, event_ids: list, ranges: list) -> dict:
        from .unity_integration import _project
        plan = {"schema": "bread-unity-sequence/v1", "sequenceId": uuid.uuid4().hex,
            "capture": {**capture, "sourceIdentity": capture_source(capture)}, "projectPath": str(_project(project)),
            "eventIds": event_ids, "eventRanges": ranges, "actions": selected_actions(actions, event_ids, ranges)}
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path(plan["sequenceId"]), plan)
        return self.status(plan["sequenceId"])

    def status(self, sequence_id: str) -> dict:
        plan = _json(self._path(sequence_id))
        tasks = []
        for path in self.store.root.glob("*/task.json"):
            state = _json(path)
            before = (state.get("capture") or {}).get("sourceIdentity") or state.get("capture") or {}
            if path_key(state["projectPath"]) == path_key(plan["projectPath"]) and same_source(
                    before, plan["capture"]["sourceIdentity"], legacy_path=True):
                tasks.append(state)
        rows = []
        for action in plan["actions"]:
            matching = [s for s in tasks if s.get("eventId") == action["eventId"]]
            # Existing tasks are reused, including those created before sequences
            # existed. Prefer the most advanced actual evidence, never revision.
            states = [self.store.status(s["restorationId"], verify_files=False) for s in matching]
            state = max(states, key=lambda s: (bool(s.get("complete")), bool(s.get("comparison")),
                bool(s.get("inspection")), bool(s.get("binding"))), default={})
            annotation = plan.get("annotations", {}).get(str(action["eventId"]), {})
            rows.append({**action, "restorationId": state.get("restorationId"), "annotation": annotation,
                "complete": state.get("complete") is True,
                "phase": "complete" if state.get("complete") else "adjust-and-render" if state.get("binding") else "inspect-and-import",
                "gaps": [g["code"] for g in state.get("gaps", [])]})
        current = next((row for row in rows if not row["complete"] and
            row["annotation"].get("status") not in {"ready-for-next", "deferred"}), None)
        return {**plan, "actions": rows, "current": current, "complete": all(row["complete"] for row in rows),
            "workflowPolicy": "agent-directed", "orderIsAdvisory": True,
            "allStepsAnnotated": current is None,
            "completedCount": sum(row["complete"] for row in rows), "actionCount": len(rows),
            "nextTools": ["get_unity_restoration_step"] if current else [],
            "workflow": "For each EID in order: inspect its own bindings/render state; prepare sources in Bread where feasible; import/adapt in the shared Unity scene; compile, render and adjust; then continue. Preserve depth, blending, draw order and pass boundaries.",
            "postProcessing": "Inspect actual input/output resource uses, subresources and prior writers (including outside this scope). Translate the dependency chain into ordered URP RenderFeature passes with explicit reads/writes, intermediate targets, formats, sizes and load/store behavior. Event adjacency alone is not a dependency."}

    def annotate(self, sequence_id: str, event_id: int, status: str, note: str, evidence: list[str]) -> dict:
        from .unity_integration import _file_lock
        import threading
        path = self._path(sequence_id)
        if status not in {"pending", "working", "ready-for-next", "deferred"} or not isinstance(note, str) or len(note) > 8000:
            raise ValueError("Provide a progress status and a bounded note")
        if not isinstance(evidence, list) or len(evidence) > 64 or any(not isinstance(item, str) or len(item) > 4096 for item in evidence):
            raise ValueError("Evidence annotations must be bounded text references")
        with _file_lock(path.with_suffix(".lock"), threading.Event(), timeout=30):
            plan = _json(path)
            if event_id not in {row["eventId"] for row in plan["actions"]}:
                raise ValueError("EID is outside the requested sequence")
            plan.setdefault("annotations", {})[str(event_id)] = {
                "status": status, "note": note, "evidence": evidence, "assessmentBy": "agent",
                "automatedValidation": False}
            atomic_write_json(path, plan)
        return self.status(sequence_id)
