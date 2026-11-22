"""Durable/idempotent CBuffer alias publication transactions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from src.shader_workspace import aliases
from src.shader_workspace import cbuffer_alias_publication as publication


def _roots(tmp_path: Path, monkeypatch):
    alias_root = tmp_path / "aliases"
    user_root = tmp_path / "user-aliases"
    workspace = tmp_path / "workspace"
    alias_root.mkdir()
    user_root.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(aliases, "project_store_root", lambda: alias_root)
    monkeypatch.setattr(aliases, "user_store_root", lambda: user_root)
    return alias_root, workspace


def _make_directory_redirect(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip("Windows junction creation is unavailable: " + completed.stderr)
    else:
        link.symlink_to(target, target_is_directory=True)


def _remove_directory_redirect(link: Path) -> None:
    if not link.exists() and not link.is_symlink():
        return
    if os.name == "nt":
        os.rmdir(str(link))
    else:
        link.unlink()


def _batch(block: str, old_name: str, new_name: str) -> dict:
    return {
        "block": block,
        "members": [{"name": old_name, "offset": 0}],
        "entries": [{"path": old_name, "offset": 0, "alias": new_name}],
        "blockAlias": None,
    }


def _result(rows):
    return {
        "schema": "test-cbuffer-publication/v1",
        "published": rows,
        "applied": any(item.get("applied") is True for item in rows),
    }


def test_ledger_bound_uses_the_exact_written_bytes(tmp_path):
    # Like a large field/coverage receipt: indentation previously exceeded the
    # reader's limit even when a compact serialization fitted it.
    value = {"items": [{"offset": i, "reviewed": True} for i in range(90000)]}
    assert len(json.dumps(value, indent=2).encode("utf-8")) > publication.MAX_LEDGER_BYTES
    path = tmp_path / "ledger.json"
    publication.write_ledger(path, value)
    assert path.stat().st_size <= publication.MAX_LEDGER_BYTES
    saved = publication.read_ledger(path)
    assert saved["items"] == value["items"]
    assert saved["schema"] == publication.LEDGER_SCHEMA


def test_oversized_ledger_does_not_replace_existing_receipt(tmp_path):
    path = tmp_path / "ledger.json"
    publication.write_ledger(path, {"phase": "committed", "operationId": "preserve-receipt"})
    before = path.read_bytes()
    with pytest.raises(ValueError, match="ledger exceeds"):
        publication.write_ledger(path, {"result": "x" * publication.MAX_LEDGER_BYTES})
    assert path.read_bytes() == before
    assert publication.read_ledger(path)["phase"] == "committed"


def _run_crashing_publication(
    *,
    alias_root: Path,
    user_root: Path,
    workspace: Path,
    operation_id: str,
    mode: str,
) -> subprocess.CompletedProcess[str]:
    script = textwrap.dedent(
        """
        import os
        from pathlib import Path
        import sys
        from src.shader_workspace import aliases
        from src.shader_workspace import cbuffer_alias_publication as publication
        from src.shader_workspace import io

        alias_root = Path(sys.argv[1])
        user_root = Path(sys.argv[2])
        workspace = Path(sys.argv[3])
        operation_id = sys.argv[4]
        mode = sys.argv[5]
        aliases.project_store_root = lambda: alias_root
        aliases.user_store_root = lambda: user_root
        batch = {
            "block": "Material",
            "members": [{"name": "value", "offset": 0}],
            "entries": [{"path": "value", "offset": 0, "alias": "newValue"}],
            "blockAlias": None,
        }
        if mode == "legacy-unbound-stage":
            transaction_dir, unused_ledger = publication.publication_paths(
                workspace, operation_id
            )
            with publication._publication_transaction_lock(transaction_dir):
                stage_root = transaction_dir / ("stage-" + "a" * 32)
                stage_root.mkdir()
                key = aliases.layout_sha256(batch["block"], batch["members"])
                live = aliases.layout_alias_path(key, alias_root)
                if live.exists():
                    original = live.read_bytes()
                    io.atomic_write_bytes(
                        transaction_dir / ("backup-000-" + key + ".json"),
                        original,
                    )
                    io.atomic_write_bytes(
                        aliases.layout_alias_path(key, stage_root), original
                    )
                aliases._publish_aliases_unlocked(
                    batch["block"], batch["members"], batch["entries"], "ai",
                    root=stage_root, store_kind="project"
                )
                os._exit(88)
        original_replace = io._replace_with_retry
        ledger_replaces = 0
        def crash_replace(temporary, destination):
            nonlocal_marker = None
            global ledger_replaces
            destination = Path(destination)
            if destination.name == "ledger.json":
                ledger_replaces += 1
            should_crash = (
                (mode == "first-ledger-temp" and destination.name == "ledger.json"
                 and ledger_replaces == 1)
                or (mode == "prepared-ledger-temp" and destination.name == "ledger.json"
                    and ledger_replaces == 2)
                or (mode == "backup-temp" and destination.name.startswith("backup-"))
                or (mode == "stage-temp" and destination.parent.name.startswith("stage-")
                    and destination.suffix == ".json")
            )
            if should_crash:
                os._exit(88)
            return original_replace(temporary, destination)
        io._replace_with_retry = crash_replace
        original_mkdir = Path.mkdir
        def crash_mkdir(path, *args, **kwargs):
            result = original_mkdir(path, *args, **kwargs)
            if mode == "stage-mkdir" and path.name.startswith("stage-"):
                os._exit(88)
            return result
        Path.mkdir = crash_mkdir
        def build_result(rows):
            if mode == "build-result":
                os._exit(88)
            return {
                "schema": "test-cbuffer-publication/v1",
                "published": rows,
                "applied": True,
            }
        publication.publish_transaction(
            workspace_root=workspace,
            operation_id=operation_id,
            request_sha256="1" * 64,
            caller_request_sha256="2" * 64,
            publications=[batch],
            build_result=build_result,
        )
        """
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(alias_root),
            str(user_root),
            str(workspace),
            operation_id,
            mode,
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )


def test_operation_paths_hash_windows_colons_and_resume_existing_legacy_ledger(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    operation_id = "shader:cbuffer:0001"
    directory, ledger = publication.publication_paths(workspace, operation_id)
    expected = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:32]

    assert directory.name == "operation-" + expected
    assert ":" not in directory.name
    assert ledger == directory / "ledger.json"

    legacy_id = "operation-legacy-1"
    legacy = workspace / "ai" / "cbuffer-alias-publications" / legacy_id
    publication.write_ledger(
        legacy / "ledger.json",
        {
            "operationId": legacy_id,
            "phase": "committed",
        },
    )

    resumed, resumed_ledger = publication.publication_paths(workspace, legacy_id)
    assert resumed == legacy
    assert resumed_ledger == legacy / "ledger.json"


def test_colon_operation_id_publishes_and_replays(tmp_path, monkeypatch):
    unused_alias_root, workspace = _roots(tmp_path, monkeypatch)
    batches = [_batch("Material", "value", "surfaceValue")]
    request_sha = hashlib.sha256(b"colon request").hexdigest()
    caller_request_sha = hashlib.sha256(b"colon caller request").hexdigest()

    first = publication.publish_transaction(
        workspace_root=workspace,
        operation_id="shader:cbuffer:0001",
        request_sha256=request_sha,
        caller_request_sha256=caller_request_sha,
        publications=batches,
        build_result=_result,
    )
    replay = publication.publish_transaction(
        workspace_root=workspace,
        operation_id="shader:cbuffer:0001",
        request_sha256=request_sha,
        caller_request_sha256=caller_request_sha,
        publications=batches,
        build_result=_result,
    )

    assert first["applied"] is True
    assert replay["idempotentReplay"] is True
    directory, ledger = publication.publication_paths(
        workspace, "shader:cbuffer:0001"
    )
    assert ":" not in directory.name
    assert publication.read_ledger(ledger)["operationId"] == "shader:cbuffer:0001"


def test_transaction_preserves_placeholder_shader_scope(tmp_path, monkeypatch):
    unused_alias_root, workspace = _roots(tmp_path, monkeypatch)
    members = [{"name": "unknown", "offset": 0}]
    layout = aliases.layout_sha256("cbuffer5", members)
    scope = {
        "shaderRawSha256": "a" * 64,
        "shaderResourceId": "1000000000000000723",
        "stage": "fragment",
        "bind": 5,
        "space": 0,
        "layoutSha256": layout,
    }
    batch = {
        "block": "cbuffer5",
        "members": members,
        "entries": [{"path": "unknown[1]", "offset": 16, "alias": "surfaceTint"}],
        "blockAlias": "Material Parameters",
        "shaderScope": scope,
    }
    result = publication.publish_transaction(
        workspace_root=workspace,
        operation_id="shader:cbuffer:scoped-placeholder",
        request_sha256=hashlib.sha256(b"scoped request").hexdigest(),
        caller_request_sha256=hashlib.sha256(b"scoped caller").hexdigest(),
        publications=[batch],
        build_result=_result,
    )
    assert result["applied"] is True
    assert aliases.read_aliases(layout) == []
    assert [item["alias"] for item in aliases.read_aliases(
        layout, shader_scope=scope
    )] == ["surfaceTint"]
    assert aliases.read_block_alias(layout, shader_scope=scope)["alias"] == (
        "Material Parameters"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path spelling")
@pytest.mark.parametrize("commit_extended", [False, True])
def test_equivalent_normal_and_extended_paths_replay_same_operation(
    tmp_path, monkeypatch, commit_extended
):
    alias_root = tmp_path / "aliases"
    user_root = tmp_path / "user-aliases"
    workspace = tmp_path / "workspace"
    for path in (alias_root, user_root, workspace):
        path.mkdir()

    def extended(path: Path) -> Path:
        return Path("\\\\?\\" + str(path.resolve()))

    normal_roots = (alias_root.resolve(), user_root.resolve(), workspace.resolve())
    extended_roots = tuple(extended(path) for path in normal_roots)
    first = extended_roots if commit_extended else normal_roots
    second = normal_roots if commit_extended else extended_roots
    monkeypatch.setattr(aliases, "project_store_root", lambda: first[0])
    monkeypatch.setattr(aliases, "user_store_root", lambda: first[1])
    batch = _batch("Material", "value", "surfaceValue")
    operation_id = "alternate-windows-path-spelling"
    publication.publish_transaction(
        workspace_root=first[2],
        operation_id=operation_id,
        request_sha256="9" * 64,
        caller_request_sha256="a" * 64,
        publications=[batch],
        build_result=_result,
    )

    monkeypatch.setattr(aliases, "project_store_root", lambda: second[0])
    monkeypatch.setattr(aliases, "user_store_root", lambda: second[1])
    fast = publication.replay_committed_receipt(
        workspace_root=second[2],
        operation_id=operation_id,
        caller_request_sha256="a" * 64,
    )
    full = publication.publish_transaction(
        workspace_root=second[2],
        operation_id=operation_id,
        request_sha256="9" * 64,
        caller_request_sha256="a" * 64,
        publications=[batch],
        build_result=_result,
    )
    assert fast["idempotentReplay"] is True
    assert full["idempotentReplay"] is True


@pytest.mark.parametrize(
    "mode",
    [
        "first-ledger-temp",
        "stage-mkdir",
        "backup-temp",
        "stage-temp",
        "prepared-ledger-temp",
        "build-result",
        "legacy-unbound-stage",
    ],
)
def test_process_kill_during_preparing_windows_recovers_without_orphan_stage(
    tmp_path, monkeypatch, mode
):
    alias_root, workspace = _roots(tmp_path, monkeypatch)
    user_root = tmp_path / "user-aliases"
    members = [{"name": "value", "offset": 0}]
    aliases.publish_aliases(
        "Material",
        members,
        [{"path": "value", "offset": 0, "alias": "oldValue"}],
        source="ai",
    )
    operation_id = "preparing-crash-" + mode
    crashed = _run_crashing_publication(
        alias_root=alias_root,
        user_root=user_root,
        workspace=workspace,
        operation_id=operation_id,
        mode=mode,
    )
    assert crashed.returncode == 88, crashed.stderr

    batch = _batch("Material", "value", "newValue")
    result = publication.publish_transaction(
        workspace_root=workspace,
        operation_id=operation_id,
        request_sha256="1" * 64,
        caller_request_sha256="2" * 64,
        publications=[batch],
        build_result=_result,
    )
    assert result["applied"] is True
    transaction_dir, unused_ledger = publication.publication_paths(
        workspace, operation_id
    )
    names = {path.name for path in transaction_dir.iterdir()}
    assert names <= {"ledger.json", ".operation.lock"}
    replay = publication.replay_committed_receipt(
        workspace_root=workspace,
        operation_id=operation_id,
        caller_request_sha256="2" * 64,
    )
    assert replay["idempotentReplay"] is True


def test_prepared_cleanup_then_new_intent_atomic_crash_recovers(
    tmp_path, monkeypatch
):
    alias_root, workspace = _roots(tmp_path, monkeypatch)
    user_root = tmp_path / "user-aliases"
    batch = _batch("Material", "value", "newValue")
    operation_id = "prepared-cleanup-new-intent-crash"

    with pytest.raises(RuntimeError, match="stop after prepare"):
        publication.publish_transaction(
            workspace_root=workspace,
            operation_id=operation_id,
            request_sha256="1" * 64,
            caller_request_sha256="2" * 64,
            publications=[batch],
            build_result=_result,
            before_apply=lambda: (_ for _ in ()).throw(
                RuntimeError("stop after prepare")
            ),
        )
    unused_directory, ledger_path = publication.publication_paths(
        workspace, operation_id
    )
    assert publication.read_ledger(ledger_path)["phase"] == "prepared"

    crashed = _run_crashing_publication(
        alias_root=alias_root,
        user_root=user_root,
        workspace=workspace,
        operation_id=operation_id,
        mode="first-ledger-temp",
    )
    assert crashed.returncode == 88, crashed.stderr

    result = publication.publish_transaction(
        workspace_root=workspace,
        operation_id=operation_id,
        request_sha256="1" * 64,
        caller_request_sha256="2" * 64,
        publications=[batch],
        build_result=_result,
    )
    assert result["applied"] is True
    transaction_dir, unused_ledger = publication.publication_paths(
        workspace, operation_id
    )
    assert {path.name for path in transaction_dir.iterdir()} <= {
        "ledger.json",
        ".operation.lock",
    }


def test_preledger_cleanup_fails_closed_if_stage_becomes_redirected(
    tmp_path, monkeypatch
):
    unused_alias_root, workspace = _roots(tmp_path, monkeypatch)
    batch = _batch("Material", "value", "newValue")
    key = aliases.layout_sha256(batch["block"], batch["members"])
    operation_id = "legacy-stage-redirect-swap"
    transaction_dir, unused_ledger = publication.publication_paths(
        workspace, operation_id
    )
    transaction_dir.mkdir(parents=True)
    stage_root = transaction_dir / ("stage-" + "b" * 32)
    stage_root.mkdir()
    (stage_root / (key + ".json")).write_bytes(b"legacy stage")
    saved_stage = transaction_dir / "saved-legacy-stage"
    external = tmp_path / "external-legacy-stage"
    external.mkdir()
    external_alias = external / (key + ".json")
    external_alias.write_bytes(b"external must survive")
    original_assert = publication._assert_directory_identity
    swapped = False

    def swap_after_first_check(path, expected, label):
        nonlocal swapped
        result = original_assert(path, expected, label)
        if label == "CBuffer alias legacy stage" and not swapped:
            swapped = True
            stage_root.rename(saved_stage)
            _make_directory_redirect(stage_root, external)
        return result

    monkeypatch.setattr(
        publication, "_assert_directory_identity", swap_after_first_check
    )
    try:
        with pytest.raises(RuntimeError, match="redirected|changed"):
            publication.publish_transaction(
                workspace_root=workspace,
                operation_id=operation_id,
                request_sha256="5" * 64,
                caller_request_sha256="6" * 64,
                publications=[batch],
                build_result=_result,
            )
        assert swapped is True
        assert external_alias.read_bytes() == b"external must survive"
    finally:
        _remove_directory_redirect(stage_root)


def test_committed_replay_cleans_authenticated_ledger_atomic_temp(
    tmp_path, monkeypatch
):
    unused_alias_root, workspace = _roots(tmp_path, monkeypatch)
    batch = _batch("Material", "value", "newValue")
    operation_id = "committed-ledger-temp"
    publication.publish_transaction(
        workspace_root=workspace,
        operation_id=operation_id,
        request_sha256="3" * 64,
        caller_request_sha256="4" * 64,
        publications=[batch],
        build_result=_result,
    )
    transaction_dir, ledger_path = publication.publication_paths(
        workspace, operation_id
    )
    ledger = publication.read_ledger(ledger_path)
    ledger["cleanupError"] = "prior transient cleanup"
    publication.write_ledger(ledger_path, ledger)
    temporary = transaction_dir / (".ledger.json.tmp-" + "a" * 32)
    temporary.write_bytes(b"interrupted atomic ledger update")

    replay = publication.replay_committed_receipt(
        workspace_root=workspace,
        operation_id=operation_id,
        caller_request_sha256="4" * 64,
    )
    assert replay["idempotentReplay"] is True
    assert not temporary.exists()
    assert "cleanupError" not in publication.read_ledger(ledger_path)


def test_committed_cleanup_fails_closed_if_stage_becomes_redirected(
    tmp_path, monkeypatch
):
    unused_alias_root, workspace = _roots(tmp_path, monkeypatch)
    batch = _batch("Material", "value", "newValue")
    operation_id = "committed-stage-redirect-swap"
    original_cleanup = publication._cleanup_committed_staging
    monkeypatch.setattr(
        publication,
        "_cleanup_committed_staging",
        lambda unused_directory, unused_ledger: (_ for _ in ()).throw(
            PermissionError("retain committed stage")
        ),
    )
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        publication.publish_transaction(
            workspace_root=workspace,
            operation_id=operation_id,
            request_sha256="7" * 64,
            caller_request_sha256="8" * 64,
            publications=[batch],
            build_result=_result,
        )
    monkeypatch.setattr(publication, "_cleanup_committed_staging", original_cleanup)
    transaction_dir, ledger_path = publication.publication_paths(
        workspace, operation_id
    )
    ledger = publication.read_ledger(ledger_path)
    stage_root = Path(ledger["stageRoot"])
    key = str(ledger["targets"][0]["layoutSha256"])
    saved_stage = transaction_dir / "saved-committed-stage"
    external = tmp_path / "external-committed-stage"
    external.mkdir()
    external_alias = external / (key + ".json")
    external_alias.write_bytes(b"committed external must survive")
    original_assert = publication._assert_directory_identity
    swapped = False

    def swap_after_first_check(path, expected, label):
        nonlocal swapped
        result = original_assert(path, expected, label)
        if label == "CBuffer alias stage" and not swapped:
            swapped = True
            stage_root.rename(saved_stage)
            _make_directory_redirect(stage_root, external)
        return result

    monkeypatch.setattr(
        publication, "_assert_directory_identity", swap_after_first_check
    )
    try:
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            publication.replay_committed_receipt(
                workspace_root=workspace,
                operation_id=operation_id,
                caller_request_sha256="8" * 64,
            )
        assert swapped is True
        assert external_alias.read_bytes() == b"committed external must survive"
    finally:
        _remove_directory_redirect(stage_root)


def test_operation_locks_are_per_operation_bounded_and_reclaimed(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(publication, "_LOCK_RETRY_SECONDS", 0.08)
    first = tmp_path / "operation-first"
    second = tmp_path / "operation-second"
    entered = threading.Event()
    release = threading.Event()

    def hold_first() -> None:
        with publication._publication_transaction_lock(first):
            entered.set()
            assert release.wait(2.0)

    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(hold_first)
        assert entered.wait(1.0)
        started = time.monotonic()
        with publication._publication_transaction_lock(second):
            pass
        assert time.monotonic() - started < 0.5
        with pytest.raises(RuntimeError, match="another thread"):
            with publication._publication_transaction_lock(first):
                pass
        release.set()
        owner.result(timeout=2.0)

    assert publication._OPERATION_LOCKS == {}


def test_committed_operation_replays_durable_receipt_without_republishing(
    tmp_path, monkeypatch
):
    unused_alias_root, workspace = _roots(tmp_path, monkeypatch)
    batches = [_batch("Material", "value", "surfaceValue")]
    request_sha = hashlib.sha256(b"same request").hexdigest()
    caller_request_sha = hashlib.sha256(b"same caller request").hexdigest()
    first = publication.publish_transaction(
        workspace_root=workspace,
        operation_id="operation-committed-1",
        request_sha256=request_sha,
        caller_request_sha256=caller_request_sha,
        publications=batches,
        build_result=_result,
    )
    assert first["applied"] is True
    durable = publication.replay_committed_receipt(
        workspace_root=workspace,
        operation_id="operation-committed-1",
        caller_request_sha256=caller_request_sha,
    )
    assert durable["idempotentReplay"] is True
    assert durable["published"] == first["published"]
    with pytest.raises(RuntimeError, match="different caller request"):
        publication.replay_committed_receipt(
            workspace_root=workspace,
            operation_id="operation-committed-1",
            caller_request_sha256=hashlib.sha256(b"different caller").hexdigest(),
        )

    def repeated_write(*args, **kwargs):
        raise AssertionError("committed operation must not publish again")

    monkeypatch.setattr(aliases, "publish_aliases", repeated_write)
    replay = publication.publish_transaction(
        workspace_root=workspace,
        operation_id="operation-committed-1",
        request_sha256=request_sha,
        caller_request_sha256=caller_request_sha,
        publications=batches,
        build_result=_result,
    )
    assert replay["idempotentReplay"] is True
    assert replay["published"] == first["published"]

    with pytest.raises(RuntimeError, match="different inputs"):
        publication.publish_transaction(
            workspace_root=workspace,
            operation_id="operation-committed-1",
            request_sha256=hashlib.sha256(b"different").hexdigest(),
            caller_request_sha256=caller_request_sha,
            publications=batches,
            build_result=_result,
        )


def test_unknown_ledger_phase_is_preserved_and_fails_closed(
    tmp_path, monkeypatch
):
    alias_root, workspace = _roots(tmp_path, monkeypatch)
    batch = _batch("Material", "value", "surfaceValue")
    request_sha = hashlib.sha256(b"unknown phase request").hexdigest()
    caller_request_sha = hashlib.sha256(b"unknown phase caller").hexdigest()
    operation_id = "operation-unknown-phase"
    publication.publish_transaction(
        workspace_root=workspace,
        operation_id=operation_id,
        request_sha256=request_sha,
        caller_request_sha256=caller_request_sha,
        publications=[batch],
        build_result=_result,
    )
    unused_directory, ledger_path = publication.publication_paths(
        workspace, operation_id
    )
    ledger = publication.read_ledger(ledger_path)
    ledger["phase"] = "unexpected-future-phase"
    publication.write_ledger(ledger_path, ledger)
    key = aliases.layout_sha256(batch["block"], batch["members"])
    live_path = aliases.layout_alias_path(key, alias_root)
    live_before = live_path.read_bytes()
    build_calls: list[list[dict]] = []

    def unexpected_build(rows):
        build_calls.append(rows)
        return _result(rows)

    with pytest.raises(RuntimeError, match="unrecognized phase"):
        publication.replay_committed_receipt(
            workspace_root=workspace,
            operation_id=operation_id,
            caller_request_sha256=caller_request_sha,
        )
    with pytest.raises(RuntimeError, match="unrecognized phase"):
        publication.publish_transaction(
            workspace_root=workspace,
            operation_id=operation_id,
            request_sha256=request_sha,
            caller_request_sha256=caller_request_sha,
            publications=[batch],
            build_result=unexpected_build,
        )

    assert build_calls == []
    assert live_path.read_bytes() == live_before
    assert publication.read_ledger(ledger_path)["phase"] == "unexpected-future-phase"


def test_second_layout_write_failure_restores_every_original_then_same_id_recovers(
    tmp_path, monkeypatch
):
    alias_root, workspace = _roots(tmp_path, monkeypatch)
    batches = [
        _batch("MaterialA", "valueA", "newA"),
        _batch("MaterialB", "valueB", "newB"),
    ]
    aliases.publish_aliases(
        "MaterialA",
        batches[0]["members"],
        [{"path": "valueA", "offset": 0, "alias": "oldA"}],
    )
    aliases.publish_aliases(
        "MaterialB",
        batches[1]["members"],
        [{"path": "valueB", "offset": 0, "alias": "oldB"}],
    )
    paths = [
        aliases.layout_alias_path(
            aliases.layout_sha256(batch["block"], batch["members"]), alias_root
        )
        for batch in batches
    ]
    before = [path.read_bytes() for path in paths]
    real_atomic_write = publication.atomic_write_bytes
    live_writes = 0

    def fail_second_live_write(path, value):
        nonlocal live_writes
        target = Path(path)
        if target.parent.resolve() == alias_root.resolve():
            live_writes += 1
            if live_writes == 2:
                raise OSError("injected second-layout failure")
        return real_atomic_write(target, value)

    monkeypatch.setattr(publication, "atomic_write_bytes", fail_second_live_write)
    request_sha = hashlib.sha256(b"two layouts").hexdigest()
    caller_request_sha = hashlib.sha256(b"two layout caller request").hexdigest()
    with pytest.raises(OSError, match="second-layout"):
        publication.publish_transaction(
            workspace_root=workspace,
            operation_id="operation-rollback-1",
            request_sha256=request_sha,
            caller_request_sha256=caller_request_sha,
            publications=batches,
            build_result=_result,
        )
    assert [path.read_bytes() for path in paths] == before
    unused_dir, ledger_path = publication.publication_paths(
        workspace, "operation-rollback-1"
    )
    assert publication.read_ledger(ledger_path)["phase"] == "failed-rolled-back"

    monkeypatch.setattr(publication, "atomic_write_bytes", real_atomic_write)
    recovered = publication.publish_transaction(
        workspace_root=workspace,
        operation_id="operation-rollback-1",
        request_sha256=request_sha,
        caller_request_sha256=caller_request_sha,
        publications=batches,
        build_result=_result,
    )
    assert recovered["applied"] is True
    assert publication.read_ledger(ledger_path)["phase"] == "committed"
    assert {item["alias"] for item in aliases.read_aliases(
        aliases.layout_sha256("MaterialA", batches[0]["members"])
    )} == {"newA"}


def test_parallel_host_publishes_merge_instead_of_losing_one_update(
    tmp_path, monkeypatch
):
    unused_alias_root, unused_workspace = _roots(tmp_path, monkeypatch)
    members = [{"name": "values", "offset": 0}]

    def write(index: int):
        return aliases.publish_aliases(
            "Material",
            members,
            [{"path": "values[%d]" % index, "offset": index * 16,
              "alias": "value%d" % index}],
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(32)))
    stored = aliases.read_aliases(aliases.layout_sha256("Material", members))
    assert {item["path"] for item in stored} == {
        "values[%d]" % index for index in range(32)
    }


def test_project_adoption_and_publish_share_one_lock(tmp_path, monkeypatch):
    alias_root, unused_workspace = _roots(tmp_path, monkeypatch)
    user_root = tmp_path / "user-aliases"
    members = [{"name": "value", "offset": 0}]
    key = aliases.layout_sha256("Material", members)
    aliases.publish_aliases(
        "Material",
        members,
        [{"path": "value", "offset": 0, "alias": "OldName"}],
        source="user",
        root=user_root,
    )
    project_file = aliases.layout_alias_path(key, alias_root)
    adoption_entered = threading.Event()
    release_adoption = threading.Event()
    original_write = aliases.atomic_write_bytes

    def pause_adoption(path, payload):
        decoded = json.loads(payload)
        if (
            Path(path) == project_file
            and decoded.get("adoptedFrom")
            and not adoption_entered.is_set()
        ):
            adoption_entered.set()
            assert release_adoption.wait(5.0)
        return original_write(path, payload)

    monkeypatch.setattr(aliases, "atomic_write_bytes", pause_adoption)
    with ThreadPoolExecutor(max_workers=2) as executor:
        reader = executor.submit(aliases.read_aliases, key)
        assert adoption_entered.wait(5.0)
        publisher = executor.submit(
            aliases.publish_aliases,
            "Material",
            members,
            [{"path": "value", "offset": 0, "alias": "NewName"}],
            "user",
        )
        time.sleep(0.05)
        assert not publisher.done()
        release_adoption.set()
        assert reader.result(timeout=5.0)[0]["alias"] == "OldName"
        assert publisher.result(timeout=5.0)["applied"] is True

    assert aliases.read_aliases(key)[0]["alias"] == "NewName"


def test_project_adoption_locks_user_source_before_copy(tmp_path, monkeypatch):
    alias_root, unused_workspace = _roots(tmp_path, monkeypatch)
    user_root = tmp_path / "user-aliases"
    members = [{"name": "value", "offset": 0}]
    key = aliases.layout_sha256("Material", members)
    aliases.publish_aliases(
        "Material",
        members,
        [{"path": "value", "offset": 0, "alias": "OldName"}],
        source="user",
        root=user_root,
    )
    write_entered = threading.Event()
    release_write = threading.Event()
    original_write = aliases.atomic_write_bytes

    def pause_user_write(path, payload):
        if Path(path).parent == user_root and not write_entered.is_set():
            write_entered.set()
            assert release_write.wait(5.0)
        return original_write(path, payload)

    monkeypatch.setattr(aliases, "atomic_write_bytes", pause_user_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        user_writer = executor.submit(
            aliases.publish_aliases,
            "Material",
            members,
            [{"path": "value", "offset": 0, "alias": "NewName"}],
            "user",
            root=user_root,
        )
        assert write_entered.wait(5.0)
        adopter = executor.submit(aliases.read_aliases, key)
        time.sleep(0.05)
        assert not adopter.done()
        release_write.set()
        assert user_writer.result(timeout=5.0)["applied"] is True
        assert adopter.result(timeout=5.0)[0]["alias"] == "NewName"

    assert aliases.layout_alias_path(key, alias_root).is_file()
    assert aliases.read_aliases(key)[0]["alias"] == "NewName"


def test_alias_store_nested_locks_are_root_specific_and_ordered(tmp_path):
    first = tmp_path / "a-store"
    second = tmp_path / "z-store"
    with aliases.alias_store_transaction(first):
        with aliases.alias_store_transaction(second):
            held = dict(getattr(aliases._ALIAS_LOCK_STATE, "held", {}))
            assert len(held) == 2
            with aliases.alias_store_transaction(first):
                nested = dict(getattr(aliases._ALIAS_LOCK_STATE, "held", {}))
                assert nested[os.path.normcase(str(first.resolve()))] == 2
    assert not dict(getattr(aliases._ALIAS_LOCK_STATE, "held", {}))

    with aliases.alias_store_transaction(second):
        with pytest.raises(RuntimeError, match="canonical order"):
            with aliases.alias_store_transaction(first):
                pass


def test_alias_store_locks_are_per_root_bounded_and_reclaimed(
    tmp_path, monkeypatch
):
    first = tmp_path / "alias-store-first"
    second = tmp_path / "alias-store-second"
    monkeypatch.setattr(aliases, "_ALIAS_LOCK_RETRY_SECONDS", 0.08)
    entered = threading.Event()
    release = threading.Event()

    def hold_first() -> None:
        with aliases.alias_store_transaction(first):
            entered.set()
            assert release.wait(2.0)

    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(hold_first)
        assert entered.wait(1.0)
        started = time.monotonic()
        with aliases.alias_store_transaction(second):
            pass
        assert time.monotonic() - started < 0.5
        with pytest.raises(RuntimeError, match="another thread"):
            with aliases.alias_store_transaction(first):
                pass
        release.set()
        owner.result(timeout=2.0)

    with pytest.raises(RuntimeError, match="body failure"):
        with aliases.alias_store_transaction(second):
            raise RuntimeError("body failure")
    assert aliases._ALIAS_LOCKS == {}


def test_private_stage_root_does_not_invert_live_alias_lock(tmp_path, monkeypatch):
    alias_root = tmp_path / "z-project-aliases"
    user_root = tmp_path / "zz-user-aliases"
    workspace = tmp_path / "a-workspace"
    alias_root.mkdir()
    user_root.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(aliases, "project_store_root", lambda: alias_root)
    monkeypatch.setattr(aliases, "user_store_root", lambda: user_root)
    batch = _batch("Material", "value", "surfaceValue")

    result = publication.publish_transaction(
        workspace_root=workspace,
        operation_id="inverse-stage-order",
        request_sha256=hashlib.sha256(b"inverse-stage").hexdigest(),
        caller_request_sha256=hashlib.sha256(b"inverse-stage-caller").hexdigest(),
        publications=[batch],
        build_result=_result,
    )

    assert result["applied"] is True
    assert aliases.read_aliases(
        aliases.layout_sha256("Material", batch["members"])
    )[0]["alias"] == "surfaceValue"


def test_committed_replay_retries_cleanup_after_post_commit_failure(
    tmp_path, monkeypatch
):
    unused_alias_root, workspace = _roots(tmp_path, monkeypatch)
    batches = [_batch("Material", "value", "surfaceValue")]
    request_sha = hashlib.sha256(b"cleanup retry").hexdigest()
    caller_request_sha = hashlib.sha256(b"cleanup retry caller").hexdigest()
    original_cleanup = publication._cleanup_committed_staging
    attempts = 0

    def fail_once(transaction_dir, ledger):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("injected post-commit sharing violation")
        return original_cleanup(transaction_dir, ledger)

    monkeypatch.setattr(publication, "_cleanup_committed_staging", fail_once)
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        publication.publish_transaction(
            workspace_root=workspace,
            operation_id="operation-cleanup-retry",
            request_sha256=request_sha,
            caller_request_sha256=caller_request_sha,
            publications=batches,
            build_result=_result,
        )

    directory, ledger_path = publication.publication_paths(
        workspace, "operation-cleanup-retry"
    )
    ledger = publication.read_ledger(ledger_path)
    assert ledger["phase"] == "committed"
    stage_root = Path(ledger["stageRoot"])
    assert stage_root.is_dir()

    replay = publication.publish_transaction(
        workspace_root=workspace,
        operation_id="operation-cleanup-retry",
        request_sha256=request_sha,
        caller_request_sha256=caller_request_sha,
        publications=batches,
        build_result=_result,
    )
    assert replay["idempotentReplay"] is True
    assert attempts == 2
    assert not stage_root.exists()
    assert not list(directory.glob("backup-*.json"))
    assert "cleanupError" not in publication.read_ledger(ledger_path)


def test_committed_cleanup_preserves_unknown_stage_entry_until_removed(
    tmp_path, monkeypatch
):
    unused_alias_root, workspace = _roots(tmp_path, monkeypatch)
    batches = [_batch("Material", "value", "surfaceValue")]
    request_sha = hashlib.sha256(b"unknown cleanup").hexdigest()
    caller_request_sha = hashlib.sha256(b"unknown cleanup caller").hexdigest()
    unknown: Path | None = None

    def inject_unknown() -> None:
        nonlocal unknown
        unused_directory, ledger_path = publication.publication_paths(
            workspace, "operation-unknown-cleanup"
        )
        ledger = publication.read_ledger(ledger_path)
        unknown = Path(ledger["stageRoot"]) / "foreign-owner.bin"
        unknown.write_bytes(b"preserve me")

    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        publication.publish_transaction(
            workspace_root=workspace,
            operation_id="operation-unknown-cleanup",
            request_sha256=request_sha,
            caller_request_sha256=caller_request_sha,
            publications=batches,
            build_result=_result,
            before_apply=inject_unknown,
        )
    assert unknown is not None
    assert unknown.read_bytes() == b"preserve me"

    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        publication.publish_transaction(
            workspace_root=workspace,
            operation_id="operation-unknown-cleanup",
            request_sha256=request_sha,
            caller_request_sha256=caller_request_sha,
            publications=batches,
            build_result=_result,
        )
    assert unknown.read_bytes() == b"preserve me"
    unknown.unlink()
    replay = publication.publish_transaction(
        workspace_root=workspace,
        operation_id="operation-unknown-cleanup",
        request_sha256=request_sha,
        caller_request_sha256=caller_request_sha,
        publications=batches,
        build_result=_result,
    )
    assert replay["idempotentReplay"] is True
