"""Real child pipes exercise reuse, failure recovery and request cancellation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

import pytest

from src import tool_runner, uex_transport as transport


READER = r'''
import json, os, sys, time
from pathlib import Path
print(json.dumps({"ok": True, "result": "uex serve ready - one JSON request per line"}), flush=True)
count = 0
for line in sys.stdin:
    request = json.loads(line)
    mode = request["args"]["asset"]
    count += 1
    if request["args"].get("started"):
        Path(request["args"]["started"]).write_text("started")
    if mode == "hang": time.sleep(30)
    if mode == "slow": time.sleep(.5)
    if request["args"].get("mutateSource"):
        Path(request["args"]["mutateSource"]).write_bytes(b"changed during decoding")
    if mode == "crash": os._exit(9)
    if mode == "flood":
        sys.stderr.buffer.write(b"diagnostic" * 100000)
        sys.stderr.flush()
    if mode == "oversize":
        print("x" * (17 * 1024 * 1024), flush=True)
        continue
    if request["args"].get("out"):
        Path(request["args"]["out"]).write_bytes(b"PNG fixture")
    print(json.dumps({"id": "wrong" if mode == "badid" else request["id"],
        "ok": mode != "error", "error": "reader rejected asset",
        "result": json.dumps({"pid": os.getpid(), "count": count})}), flush=True)
'''


@pytest.fixture
def rig(tmp_path):
    script = tmp_path / "reader.py"
    script.write_text(READER)
    source = tmp_path / "Texture.uasset"
    source.write_bytes(b"package")
    config = tmp_path / "profiles.json"
    config.write_text(json.dumps({"profiles": {"test": {"paksDir": str(tmp_path)}}}))
    pool = transport.ReaderPool(lambda argv: transport.Session([sys.executable, "-u", str(script)]))
    spec = {"config": config, "configHash": "config1", "profile": "test", "mappingHash": None,
            "source": source, "command": "preview", "args": {"asset": "normal"}}
    yield pool, spec, tmp_path
    pool.close()


def run(pool, spec, mode="normal", timeout=3, cancel=None, program_hash="exe1"):
    selected = dict(spec, args=dict(spec["args"], asset=mode))
    return pool.run(selected, sys.executable, program_hash, timeout, cancel)


def value(reply):
    assert reply["exitCode"] == 0, reply
    return json.loads(reply["stdout"])


def wait_started(path):
    deadline = time.monotonic() + 3
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert path.exists()


def test_two_calls_reuse_one_process_and_keep_receipts_independent(rig):
    pool, spec, _ = rig
    first = run(pool, spec)
    second = run(pool, spec)
    assert value(first)["pid"] == value(second)["pid"]
    assert value(second)["count"] == 2
    assert first["transport"]["reused"] is False
    assert second["transport"]["reused"] is True
    assert first["transport"]["requestId"] != second["transport"]["requestId"]


@pytest.mark.parametrize("change", ["config", "profile", "mapping", "exe", "source", "companion"])
def test_changed_identity_or_package_never_reuses_cached_object(rig, change):
    pool, spec, _ = rig
    old = value(run(pool, spec))["pid"]
    digest = "exe1"
    if change == "config": spec["configHash"] = "config2"
    if change == "profile": spec["profile"] = "other"
    if change == "mapping": spec["mappingHash"] = "mapping2"
    if change == "exe": digest = "exe2"
    if change == "source": spec["source"].write_bytes(b"new same path package")
    if change == "companion": spec["source"].with_suffix(".ubulk").write_bytes(b"new payload")
    reply = run(pool, spec, program_hash=digest)
    assert value(reply)["pid"] != old
    assert reply["transport"]["reused"] is False


@pytest.mark.parametrize("failure", ["crash", "badid", "oversize", "error"])
def test_failed_or_corrupt_reader_is_reaped_then_next_call_recovers(rig, failure):
    pool, spec, _ = rig
    run(pool, spec)
    process = pool.session.process
    failed = run(pool, spec, failure)
    assert failed["exitCode"] != 0
    assert process.poll() is not None
    assert pool.session is None
    assert value(run(pool, spec))["pid"] != process.pid


def test_stderr_flood_is_drained_and_bounded(rig):
    pool, spec, _ = rig
    reply = run(pool, spec, "flood")
    value(reply)
    assert len(reply["stderr"]) <= transport.MAX_STDERR_BYTES


def test_active_cancel_kills_only_active_reader_and_next_call_recovers(rig):
    pool, spec, root = rig
    started = root / "started"
    spec["args"]["started"] = str(started)
    cancel = threading.Event()
    with ThreadPoolExecutor() as executor:
        future = executor.submit(run, pool, spec, "hang", 4, cancel)
        wait_started(started)
        process = pool.session.process
        cancel.set()
        reply = future.result(timeout=3)
    assert reply["cancelled"] is True
    assert process.poll() is not None
    value(run(pool, spec))


@pytest.mark.parametrize("queued_action", ["cancel", "timeout"])
def test_queued_cancel_or_timeout_does_not_kill_another_request(rig, queued_action):
    pool, spec, root = rig
    started = root / "started"
    spec["args"]["started"] = str(started)
    cancel = threading.Event()
    with ThreadPoolExecutor() as executor:
        active = executor.submit(run, pool, spec, "slow")
        wait_started(started)
        process = pool.session.process
        if queued_action == "cancel": cancel.set()
        queued = run(pool, spec, timeout=.05, cancel=cancel)
        assert queued["exitCode"] == -1
        assert process.poll() is None
        completed = active.result(timeout=3)
        value(completed)
        assert completed["transport"]["processId"] == process.pid
    next_reply = run(pool, spec)
    value(next_reply)
    assert next_reply["transport"]["processId"] == process.pid


def test_timeout_reaps_and_idle_cleanup_releases_mount(rig):
    pool, spec, _ = rig
    reply = run(pool, spec, "hang", timeout=.15)
    assert reply["timedOut"] is True
    assert pool.session is None
    pool.idle_seconds = .05
    # Wake interval is chosen when reaper first waits; call the expiry path via
    # an independent pool to exercise actual idle cleanup without long sleeps.
    fresh = transport.ReaderPool(pool.session_factory, idle_seconds=.05)
    try:
        value(run(fresh, spec))
        process = fresh.session.process
        deadline = time.monotonic() + 2
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert process.poll() is not None
    finally:
        fresh.close()


def operation():
    return {"toolId": "builtin-uex", "name": "uex_preview_texture",
            "argv": ["--config", "{uexConfig}", "preview-texture", "--profile", "{uexProfile}",
                     "--out", "{outputDir}/preview.png", "{asset}"],
            "inputSchema": {"type": "object", "properties": {"asset": {"type": "string"}},
                            "required": ["asset"]},
            "result": {"successWhen": {"exitCode": 0, "minFiles": 1}}}


def test_tool_runner_preserves_stdout_artifact_and_logical_argv(rig, monkeypatch):
    pool, spec, root = rig
    monkeypatch.setattr(transport, "POOL", pool)
    receipts = [tool_runner.run_operation(operation(), {"asset": "Texture.uasset"},
                 program=sys.executable, output_root=root / "output",
                 host_slots={"uexConfig": str(spec["config"]), "uexProfile": "test"}) for _ in range(2)]
    for receipt in receipts:
        assert receipt["succeeded"] is True
        assert receipt["producedFileCount"] == 1
        assert receipt["argv"][-1] == "Texture.uasset"
        assert receipt["transport"]["kind"] == "uex-jsonl"
        stdout = Path(receipt["stdoutPath"]).read_bytes()
        assert hashlib.sha256(stdout).hexdigest() == receipt["stdoutSha256"]
    assert receipts[1]["transport"]["reused"] is True
    assert receipts[0]["outputDirectory"] != receipts[1]["outputDirectory"]


def test_async_cancellation_reaps_worker_without_blocking_event_loop(rig, monkeypatch):
    pool, spec, root = rig
    monkeypatch.setattr(transport, "POOL", pool)
    started = root / "started"
    spec["args"] = {"asset": "hang", "started": str(started)}
    monkeypatch.setattr(transport, "adapt", lambda *args: spec)
    async def scenario():
        task = asyncio.create_task(tool_runner.run_operation_async(operation(), {"asset": "Texture.uasset"},
            program=sys.executable, output_root=root / "output",
            host_slots={"uexConfig": str(spec["config"]), "uexProfile": "test"}))
        ticks = 0
        while not started.exists() and ticks < 300:
            await asyncio.sleep(.01)
            ticks += 1
        assert started.exists()
        process = pool.session.process
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.poll() is not None
        assert ticks > 0
    asyncio.run(scenario())


def test_unknown_recipes_and_options_keep_cli_path(rig):
    _, spec, root = rig
    argv = [sys.executable, "--config", str(spec["config"]), "preview-texture", "--profile",
            "test", "--out", str(root / "preview.png"), "Texture.uasset"]
    assert transport.adapt(operation(), argv) is not None
    assert transport.adapt(dict(operation(), toolId="user-reader"), argv) is None
    assert transport.adapt(operation(), argv + ["--extra"]) is None
    assert transport.adapt(operation(), argv[:-1] + ["../other.uasset"]) is None


def test_change_to_previously_read_package_invalidates_different_next_asset(rig):
    pool, spec, root = rig
    first = run(pool, spec)
    other = root / "Other.uasset"
    other.write_bytes(b"other")
    other_spec = dict(spec, source=other)
    assert run(pool, other_spec)["transport"]["reused"] is True
    spec["source"].with_suffix(".m.ubulk").write_bytes(b"changed first package")
    result = run(pool, other_spec)
    assert result["transport"]["processId"] != first["transport"]["processId"]
    assert result["transport"]["restartReason"] == "source-changed"


def test_runtime_dll_change_invalidates_even_when_launcher_hash_unchanged(rig):
    pool, spec, root = rig
    program = root / "uex.exe"
    program.write_bytes(b"fixed apphost")
    component = root / "uex.dll"
    component.write_bytes(b"assembly one")
    first = pool.run(spec, str(program), "fixed-host-sha", 3)
    component.write_bytes(b"assembly two changed")
    second = pool.run(spec, str(program), "fixed-host-sha", 3)
    assert first["transport"]["processId"] != second["transport"]["processId"]
    assert second["transport"]["restartReason"] == "configuration-or-runtime-changed"


def test_real_config_and_usmap_bytes_bind_adapter_identity(rig):
    _, spec, root = rig
    mapping = root / "game.usmap"
    mapping.write_bytes(b"mapping one")
    config = json.loads(spec["config"].read_text())
    config["profiles"]["test"]["usmap"] = str(mapping)
    spec["config"].write_text(json.dumps(config))
    argv = [sys.executable, "--config", str(spec["config"]), "preview-texture", "--profile",
            "test", "--out", str(root / "preview.png"), "Texture.uasset"]
    first = transport.adapt(operation(), argv)
    mapping.write_bytes(b"mapping two")
    second = transport.adapt(operation(), argv)
    assert first["mappingHash"] != second["mappingHash"]
    config["profiles"]["test"]["game"] = "different game version"
    spec["config"].write_text(json.dumps(config))
    assert transport.adapt(operation(), argv)["configHash"] != first["configHash"]


def test_pool_shutdown_interrupts_mount_and_reaps_process(rig):
    pool, spec, root = rig
    started = root / "started"
    spec["args"]["started"] = str(started)
    with ThreadPoolExecutor() as executor:
        active = executor.submit(run, pool, spec, "hang", 30)
        wait_started(started)
        process = pool.session.process
        pool.close()
        assert active.result(timeout=3)["exitCode"] != 0
        assert process.poll() is not None
        assert pool.session is None


def test_changed_source_during_read_rejects_current_result_and_discards_cache(rig):
    pool, spec, _ = rig
    spec["args"]["mutateSource"] = str(spec["source"])
    result = run(pool, spec)
    assert result["exitCode"] == -1
    assert b"changed during preview" in result["stderr"]
    assert pool.session is None
    spec["args"].pop("mutateSource")
    assert value(run(pool, spec))["count"] == 1


def test_shutdown_during_child_creation_never_leaks_late_process(rig):
    original, spec, _ = rig
    starting = threading.Event()
    processes = []
    def delayed_factory(argv):
        starting.set()
        time.sleep(.15)
        session = original.session_factory(argv)
        processes.append(session.process)
        return session
    pool = transport.ReaderPool(delayed_factory)
    with ThreadPoolExecutor() as executor:
        task = executor.submit(run, pool, spec)
        assert starting.wait(2)
        pool.close()
        assert task.result(timeout=3)["exitCode"] == -1
    assert processes and processes[0].poll() is not None
    assert pool.session is None
