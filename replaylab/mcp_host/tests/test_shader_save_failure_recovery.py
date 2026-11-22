"""Exercise saved-source recovery with real watcher/live-edit workers.

Only compilation and replay are fixtures. Source CAS, explicit-save scheduling,
audit publication, compiler diagnostics and live candidate admission are real.
"""
import copy
import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from src.shader_live_edit import ShaderLiveEditManager, ShaderLiveEditSession, _LiveEditJob
from src.shader_workspace import ShaderWorkspaceWatchManager
from src.shader_workspace.watcher import _WatchJob
from test_shader_live_edit import _compatible_result, _wait_until


def sha(data):
    return hashlib.sha256(data).hexdigest()


class CompilerBoundary:
    def __init__(self, root, api):
        self.root, self.api = root, api
        self.source = root / "source" / ("shader.glsl" if api in {"opengl", "vulkan"} else "shader.hlsl")
        self.source.parent.mkdir()
        self.source.write_bytes(self.text("0.1"))
        self.original = sha(self.source.read_bytes())
        self.latest = None
        self.attempts = []
        self.header = None

    def text(self, value):
        if self.api in {"opengl", "vulkan"}:
            return ("#version 450\nlayout(location=0) out vec4 color;\n"
                    "void main() { color = vec4(%s); }\n" % value).encode()
        return ("float4 main() : SV_Target\n{\n    return %s;\n}\n" % value).encode()

    def status(self, root):
        assert Path(root) == self.root
        return {"workspacePath": str(self.root), "capture": {"api": self.api},
                "source": {"present": True, "path": str(self.source),
                           "currentSha256": sha(self.source.read_bytes()),
                           "decompiledSha256": self.original, "lane": "executable-editable"},
                "representation": {"lane": "executable-editable", "compileEligible": True},
                "latestCompiled": copy.deepcopy(self.latest)}

    def compile(self, root):
        assert Path(root) == self.root
        source = self.source.read_bytes()
        unit = source + (self.header.read_bytes() if self.header else b"")
        self.attempts.append(sha(unit))
        if b"BROKEN" in unit:
            diagnostics = {
                "d3d11": "shader.hlsl(3,4): error X3000: unrecognized identifier 'BROKEN'",
                "d3d12": "shader.hlsl:3:4: error: use of undeclared identifier 'BROKEN'",
                "opengl": "0(3) : error C1503: undefined variable BROKEN",
                "vulkan": "ERROR: shader.glsl:3: 'BROKEN' : undeclared identifier",
            }
            raise RuntimeError(diagnostics[self.api])
        self.latest = {"revision": len(self.attempts), "sourceSha256": sha(source),
                       "rawSha256": sha(self.api.encode() + b":" + unit)}
        return copy.deepcopy(self.latest)


@pytest.mark.parametrize("api", ["d3d11", "d3d12", "opengl", "vulkan"])
def test_saved_compile_error_correction_applies_only_latest_source(tmp_path, api):
    compiler = CompilerBoundary(tmp_path, api)
    watches = ShaderWorkspaceWatchManager(lambda: compiler)
    binding = {"instanceId": "save-test", "captureSessionId": "b" * 32,
               "capturePath": str(tmp_path / "frame.rdc")}
    identity = {"workspaceId": "a" * 32, "captureId": "c" * 64,
                "capturePath": binding["capturePath"], "resourceId": 77,
                "stage": "fragment", "entryPoint": "main", "shaderRawSha256": "d" * 64,
                "shaderKey": "77:fragment:main:" + "d" * 64}
    applied = []

    def apply(root, revision, raw):
        assert compiler.latest["revision"] == revision
        assert compiler.latest["rawSha256"] == raw
        assert compiler.latest["sourceSha256"] == sha(compiler.source.read_bytes())
        applied.append((revision, raw, compiler.source.read_bytes()))
        return _compatible_result(tmp_path, revision, raw)

    manager = ShaderLiveEditManager(watches, apply, poll_seconds=.005, join_seconds=.5,
        registry_path=tmp_path / "registry.json", workspace_identity=lambda root: identity,
        bind_session=lambda root: ShaderLiveEditSession(binding, lambda: "current", apply))

    def request():
        return manager.compile_saved_source(str(tmp_path), identity["workspaceId"],
            sha(compiler.source.read_bytes()), capture_binding=binding)

    try:
        first = request()
        live_id, watch_id = first["liveEditId"], first["watchId"]
        _wait_until(lambda: manager.poll(live_id)["applySuccesses"] == 1, timeout=5)
        previous = manager.poll(live_id)["activeReplacement"]
        invalid = compiler.text("BROKEN")
        watches.save_source(watch_id, sha(compiler.source.read_bytes()), invalid.decode())
        request()
        _wait_until(lambda: manager.poll(live_id)["lastCompileError"] is not None, timeout=5)
        failed = manager.poll(live_id)
        assert failed["state"] == "running"
        assert failed["activeReplacement"] == previous
        assert failed["lastCompileError"]["diagnostics"][0]["line"] == 3
        assert "BROKEN" in failed["lastCompileError"]["message"]
        assert len(applied) == 1

        corrected = compiler.text("0.8")
        watches.save_source(watch_id, sha(invalid), corrected.decode())
        request()
        _wait_until(lambda: manager.poll(live_id)["applySuccesses"] == 2, timeout=5)
        recovered = manager.poll(live_id)
        assert recovered["lastCompileError"] is None
        assert recovered["activeReplacement"]["revision"] > previous["revision"]
        assert applied[-1][2] == corrected
        assert applied[0][1] != applied[-1][1]
        assert compiler.source.read_bytes() == corrected
        assert all(b"BROKEN" not in item[2] for item in applied)
        assert "compileRecovered" in [event["type"] for event in recovered["events"]]
    finally:
        manager.shutdown()
        watches.shutdown()


def manual_jobs(tmp_path):
    compiler = CompilerBoundary(tmp_path, "d3d12")
    (tmp_path / "watch").mkdir()
    watch = _WatchJob("a" * 32, tmp_path, compiler.source, 100, compiler,
        compiler.original, None, None, None, None, None, tmp_path / "watch/audit.json",
        mock.Mock(path=tmp_path / "watch/lease", owned=True), lambda job: None)
    watch._state = "running"
    live = _LiveEditJob(live_edit_id="b" * 32, watch_snapshot=watch.snapshot(),
        watch_manager=mock.Mock(), apply_candidate=mock.Mock(), on_finished=lambda job: None,
        poll_seconds=.005, join_seconds=.5, session_unavailable_retry_seconds=.1)
    return compiler, watch, live


def compile_saved(compiler, watch, text):
    watch.save_source(sha(compiler.source.read_bytes()), text.decode())
    watch.request_saved_source_compile(sha(text))
    watch._stable_since -= 1
    watch._tick()


def test_include_failure_does_not_mark_old_same_root_candidate_caught_up(tmp_path):
    compiler, watch, live = manual_jobs(tmp_path)
    root_source = compiler.source.read_bytes()
    compiler.header = compiler.source.parent / "color.hlsli"
    compiler.header.write_bytes(b"float brightness = 0.1;")
    compile_saved(compiler, watch, root_source)
    good = watch.snapshot()
    assert good["caughtUp"] is True
    live._observe_watch(good)

    compiler.header.write_bytes(b"float brightness = BROKEN;")
    compile_saved(compiler, watch, root_source)
    failed = watch.snapshot()
    assert failed["lastCompiledSourceSha256"] == failed["observedSourceSha256"]
    assert failed["latestCandidateRevision"] == good["latestCandidateRevision"]
    assert failed["lastError"] is not None
    assert failed["caughtUp"] is False

    compiler.header.write_bytes(b"float brightness = 0.8;")
    compile_saved(compiler, watch, root_source)
    fixed = watch.snapshot()
    assert fixed["lastError"] is None and fixed["caughtUp"] is True
    assert fixed["latestCandidateRevision"] > good["latestCandidateRevision"]
    assert fixed["latestCandidateRawSha256"] != good["latestCandidateRawSha256"]


@pytest.mark.parametrize("last_outcome", ["success", "failure"])
def test_one_poll_keeps_actual_latest_compile_outcome(tmp_path, last_outcome):
    compiler, watch, live = manual_jobs(tmp_path)
    states = ("0.3", "BROKEN") if last_outcome == "failure" else ("BROKEN", "0.3")
    for value in states:
        compile_saved(compiler, watch, compiler.text(value))
    # Both counters advanced since the coordinator's previous observation.
    snapshot = watch.snapshot()
    assert snapshot["compileSuccesses"] == snapshot["compileFailures"] == 1
    live._observe_watch(snapshot)
    audit = json.loads(live.audit_path.read_text(encoding="utf8"))
    if last_outcome == "failure":
        assert audit["lastCompileError"] is not None
        assert "BROKEN" in audit["lastCompileError"]["message"]
        assert "compileRecovered" not in [event["type"] for event in audit["events"]]
    else:
        assert audit["lastCompileError"] is None
        assert "compileRecovered" in [event["type"] for event in audit["events"]]


def test_adopted_candidate_clears_previous_error_in_live_observation_and_resume(tmp_path):
    compiler, watch, live = manual_jobs(tmp_path)
    source = compiler.text("0.4")
    with mock.patch.object(compiler, "compile", side_effect=RuntimeError("compiler temporarily unavailable")):
        compile_saved(compiler, watch, source)
    live._observe_watch(watch.snapshot())
    assert live.snapshot()["lastCompileError"] is not None
    # An independent fixed compiler publishes a candidate for the exact saved
    # bytes after the failure. Adoption is a successful compile outcome too.
    (tmp_path / "manifest.json").write_text(json.dumps({
        "source": {"path": "source/shader.hlsl"}, "latestCompiled": {
            "revision": 5, "sourceSha256": sha(source), "rawSha256": "e" * 64}}), encoding="utf8")
    watch._tick()
    snapshot = watch.snapshot()
    assert snapshot["compileSuccesses"] == 1
    assert snapshot["events"][-1]["type"] == "compileAdopted"
    live._observe_watch(snapshot)
    assert live.snapshot()["lastCompileError"] is None
    resumed = _LiveEditJob(live_edit_id="c" * 32, watch_snapshot=snapshot,
        watch_manager=mock.Mock(), apply_candidate=mock.Mock(), on_finished=lambda job: None,
        poll_seconds=.005, join_seconds=.5, session_unavailable_retry_seconds=.1)
    assert resumed.snapshot()["lastCompileError"] is None
