"""One-shot, same-file recovery of a fatal native replay controller (Python 3.6).

This module does not open files in qrenderdoc itself. The bridge supplies UI-
and replay-thread hooks, while this transaction owns identity checks, retry
limits, health checks and the cleanup boundary. It never retries a write or
reapplies Shader replacements/CBuffer edits on the newly opened controller.

Required hooks (an object with these methods):
  snapshot() -> loaded/local/temporary/capturePath/captureSessionId/eventId/
                selectedEventId; extra fields such as ReplayOptions are retained.
  health() -> captureSessionId/healthy/code/diagnostic from GetFatalErrorStatus.
  preserve(snapshot) -> {preserved: True, ...opaque retained UI/draft state}.
  begin(snapshot, preserved) -> opaque guard token. Suppress only the expected
                same-file lifecycle callbacks, not explicit user cancellation.
  invalidate(snapshot, preserved) -> forget applied replacements/live edits and
                current-session cache authority, retaining source and saved media.
  reopen(snapshot) -> UI-thread LoadCapture of this exact file, without project
                auto-apply. Must publish a new captureSessionId even for this path.
  restore_event(snapshot) -> UI-thread SetEventID([], selectedEventId,eventId,True);
                check the same-file guard and reopened session inside that UI
                transaction, immediately before changing the selection.
  finish(snapshot, preserved, result, token) -> always clear guards in finally;
                keep drafts/saved diagrams and publish invalidated live status.
Optional cancelled() checks explicit cancellation or a real user capture switch.

No hook may infer that a saved image or compiled Shader is current merely because
the new controller is healthy. A successful result is capture-original only.
"""

from collections import OrderedDict
import os
import threading


def _path_key(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path or ""))))


def _file_stamp(path):
    stat = os.stat(path)
    if not os.path.isfile(path):
        raise ValueError("The capture is not a regular local file")
    return (stat.st_dev, stat.st_ino, stat.st_size,
            getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)))


def _capture_identity(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("loaded") is not True:
        raise ValueError("There is no loaded capture to recover")
    if snapshot.get("local") is not True or snapshot.get("temporary") is not False:
        raise ValueError("Automatic replay recovery requires a permanent local capture")
    path = snapshot.get("capturePath")
    session = snapshot.get("captureSessionId")
    if not path or not isinstance(session, str) or not session:
        raise ValueError("The loaded capture has no complete physical identity")
    return _path_key(path), session


def _checked_health(hooks, session):
    health = hooks.health()
    if (not isinstance(health, dict) or health.get("error") or
            type(health.get("healthy")) is not bool or
            health.get("captureSessionId") != session or not health.get("code")):
        raise ValueError("Native replay health is unavailable or belongs to another capture session")
    return health


def _cancelled(hooks):
    check = getattr(hooks, "cancelled", None)
    return bool(callable(check) and check())


class ReplayRecoveryCoordinator:
    """Own a bounded recovery ledger; construct once for the bridge instance."""

    def __init__(self):
        self._guard = threading.RLock()
        self._active = False
        self._attempts = OrderedDict()
        self._lineage = OrderedDict()

    @property
    def active(self):
        with self._guard:
            return self._active

    def recover(self, hooks, expected_session_id=""):
        result = {
            "schema": "bread-replay-recovery/v1", "attempted": False,
            "recovered": False, "status": "not-started", "preserved": False,
            "replacementsRestored": False, "liveEditsRestored": False,
            "currentEvidenceVerified": False, "invalidated": [],
        }

        def fail(code, error):
            result.update(status="failed", errorId=code, error=str(error)[:4096])
            return result

        # Claim the operation before calling any hook, which can pump the GUI
        # event loop. A nested recovery must never close the in-flight controller.
        with self._guard:
            if self._active:
                return fail("replay-recovery-busy", "A replay recovery is already running")
            self._active = True
        snapshot, preserved, token = None, None, None
        guard_started = False
        phase = "preflight"
        try:
            snapshot = dict(hooks.snapshot())
            path, session = _capture_identity(snapshot)
            if any(type(snapshot.get(key)) is not int or snapshot[key] < 0
                   for key in ("eventId", "selectedEventId")):
                raise ValueError("The original visible and selected events are unavailable")
            result["previousCaptureSessionId"] = session
            if expected_session_id and expected_session_id != session:
                return fail("replay-recovery-capture-changed", "The requested capture session is no longer active")
            if _cancelled(hooks):
                return fail("replay-recovery-cancelled", "Recovery was cancelled before changing replay")
            health = _checked_health(hooks, session)
            if health["healthy"]:
                result["status"] = "not-needed"
                return result
            result["fatalStatus"] = {key: health.get(key) for key in ("code", "diagnostic")}
            stamp = _file_stamp(path)
            # A failed replacement must not be retried in a loop after each new
            # session. Descendants retain the first failed controller's lineage.
            origin = self._lineage.get((path, session), session)
            failure_key = (path, origin, str(health["code"]))
            if failure_key in self._attempts:
                return fail("replay-recovery-retry-limit", "This native replay failure has already had one recovery attempt")
            self._attempts[failure_key] = True
            while len(self._attempts) > 64:
                self._attempts.popitem(last=False)
            phase = "preserve"
            preserved = hooks.preserve(snapshot)
            if not isinstance(preserved, dict) or preserved.get("preserved") is not True:
                raise ValueError("Drafts and saved project evidence were not confirmed preserved")
            result["preserved"] = True
            if _capture_identity(hooks.snapshot()) != (path, session) or _file_stamp(path) != stamp:
                return fail("replay-recovery-capture-changed", "The capture changed while recovery state was being preserved")
            if _cancelled(hooks):
                return fail("replay-recovery-cancelled", "Recovery was cancelled before reopening the capture")
            phase = "begin"
            guard_started = True
            token = hooks.begin(snapshot, preserved)
            if _cancelled(hooks) or _capture_identity(hooks.snapshot()) != (path, session):
                return fail("replay-recovery-capture-changed", "The active capture changed before live state could be invalidated")
            # This invalidates only live authority. Recovery is not allowed to
            # erase a dirty editor or claim old replacements are still applied.
            phase = "invalidate"
            hooks.invalidate(snapshot, preserved)
            result["invalidated"] = ["shader-replacements", "cbuffer-edits", "capture-session-caches"]
            if _cancelled(hooks):
                return fail("replay-recovery-cancelled", "Recovery was cancelled before reopening the capture")
            if _capture_identity(hooks.snapshot()) != (path, session) or _file_stamp(path) != stamp:
                return fail("replay-recovery-capture-changed", "The capture changed before replay could be rebuilt")
            phase = "reopen"
            result["attempted"] = True
            hooks.reopen(snapshot)
            current = dict(hooks.snapshot())
            reopened_path, new_session = _capture_identity(current)
            if reopened_path != path or _file_stamp(path) != stamp:
                return fail("replay-recovery-capture-changed", "The reopened capture does not match the original file")
            if new_session == session:
                raise ValueError("The rebuilt controller did not receive a new capture session")
            result["captureSessionId"] = new_session
            self._lineage[(path, new_session)] = origin
            while len(self._lineage) > 64:
                self._lineage.popitem(last=False)
            phase = "health-after-reopen"
            if not _checked_health(hooks, new_session)["healthy"]:
                raise RuntimeError("The rebuilt native replay controller is still in a fatal state")
            if _cancelled(hooks):
                return fail("replay-recovery-cancelled", "Recovery was cancelled after reopening the capture")
            if _capture_identity(hooks.snapshot()) != (path, new_session):
                return fail("replay-recovery-capture-changed", "The active capture changed before restoring event selection")
            phase = "restore-event"
            hooks.restore_event(snapshot)
            current = dict(hooks.snapshot())
            if _capture_identity(current) != (path, new_session) or _file_stamp(path) != stamp:
                return fail("replay-recovery-capture-changed", "The capture changed while restoring event selection")
            if any(current.get(key) != snapshot.get(key) for key in ("eventId", "selectedEventId")):
                raise ValueError("The original visible and selected events were not restored")
            if not _checked_health(hooks, new_session)["healthy"]:
                raise RuntimeError("Native replay became fatal while restoring event selection")
            if _cancelled(hooks):
                return fail("replay-recovery-cancelled", "Recovery was cancelled before completion")
            result.update(status="recovered", recovered=True, replayContent="capture-original",
                          eventId=current.get("eventId"), selectedEventId=current.get("selectedEventId"))
            return result
        except Exception as error:
            result["failedPhase"] = phase
            return fail("replay-recovery-failed", error)
        finally:
            try:
                if guard_started:
                    # Call even when begin raises after setting only some flags.
                    # finish itself must use finally to clear native/Python guards.
                    hooks.finish(snapshot, preserved, result, token)
            except Exception as error:
                result.update(recovered=False, failedPhase="finish")
                fail("replay-recovery-cleanup-failed", error)
            finally:
                with self._guard:
                    self._active = False
