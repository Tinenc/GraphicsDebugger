from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
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

import test_shader_organization as harness
from src.shader_workspace import aliases as cbuffer_aliases
from src.shader_workspace import io as workspace_io
from src.shader_workspace import workspace_mutation_transaction as transactions
from src.shader_workspace.organization import (
    ShaderOrganizationError,
    ShaderOrganizationManager,
)
from src.shader_workspace import organization as organization_module


@pytest.fixture
def organization_case():
    case = harness.ShaderOrganizationTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def _assert_snapshot(snapshot: dict[Path, bytes | None]) -> None:
    for path, expected in snapshot.items():
        assert (path.read_bytes() if path.exists() else None) == expected


def test_workspace_mutation_locks_are_per_workspace_bounded_and_reclaimed(
    tmp_path, monkeypatch
):
    store = tmp_path / "transaction-store"
    first = tmp_path / "workspace-first"
    second = tmp_path / "workspace-second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(transactions, "_LOCK_RETRY_SECONDS", 0.08)
    entered = threading.Event()
    release = threading.Event()

    def hold_first() -> None:
        with transactions._transaction_lock(store, first, "org-first"):
            entered.set()
            assert release.wait(2.0)

    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(hold_first)
        assert entered.wait(1.0)
        started = time.monotonic()
        with transactions._transaction_lock(store, second, "org-second"):
            pass
        assert time.monotonic() - started < 0.5
        with pytest.raises(RuntimeError, match="another thread"):
            with transactions._transaction_lock(store, first, "org-first"):
                pass
        release.set()
        owner.result(timeout=2.0)

    with pytest.raises(RuntimeError, match="body failure"):
        with transactions._transaction_lock(store, second, "org-second"):
            raise RuntimeError("body failure")
    assert transactions._MUTATION_LOCKS == {}


def _run_crashing_transaction(
    *,
    store_root: Path,
    workspace_root: Path,
    organization_id: str,
    targets: list[tuple[Path, bytes]],
    crash_target: Path,
) -> subprocess.CompletedProcess[str]:
    payload_dir = workspace_root.parent / ("transaction-payloads-" + organization_id)
    payload_dir.mkdir(exist_ok=False)
    arguments: list[str] = []
    for index, (target, content) in enumerate(targets):
        payload = payload_dir / ("payload-%d.bin" % index)
        payload.write_bytes(content)
        arguments.extend([str(target), str(payload)])
    script = textwrap.dedent(
        """
        import os
        from pathlib import Path
        import sys
        from src.shader_workspace import workspace_mutation_transaction as tx

        store_root = Path(sys.argv[1])
        workspace_root = Path(sys.argv[2])
        organization_id = sys.argv[3]
        crash_target = Path(sys.argv[4]).resolve()
        values = sys.argv[5:]
        targets = {}
        for index in range(0, len(values), 2):
            targets[Path(values[index])] = Path(values[index + 1]).read_bytes()
        original = tx.atomic_write_bytes
        def crash(path, content):
            if Path(path).resolve() == crash_target:
                os._exit(73)
            return original(Path(path), content)
        tx.atomic_write_bytes = crash
        tx.apply_transaction(
            store_root=store_root,
            workspace_root=workspace_root,
            organization_id=organization_id,
            tool="restructure_shader_workspace_source",
            operation_id="process-interruption",
            request_sha256="a" * 64,
            targets=targets,
            checkpoint_after_apply=lambda: None,
            checkpoint_after_rollback=lambda: None,
        )
        """
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(store_root),
            str(workspace_root),
            organization_id,
            str(crash_target),
            *arguments,
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )


def _run_atomic_replace_crash(
    *,
    store_root: Path,
    workspace_root: Path,
    organization_id: str,
    target: Path,
    mode: str,
) -> subprocess.CompletedProcess[str]:
    script = textwrap.dedent(
        """
        import os
        from pathlib import Path
        import sys
        from src.shader_workspace import io
        from src.shader_workspace import workspace_mutation_transaction as tx

        store = Path(sys.argv[1])
        workspace = Path(sys.argv[2])
        organization_id = sys.argv[3]
        target = Path(sys.argv[4])
        mode = sys.argv[5]
        original = io._replace_with_retry
        def crash(temporary, destination):
            destination = Path(destination)
            should_crash = (
                (mode == "first-ledger" and destination.name == "ledger.json")
                or (mode == "preparing" and destination.name == "after-0000.bin")
                or (mode == "applying" and destination.resolve() == target.resolve())
            )
            if should_crash:
                os._exit(74)
            return original(temporary, destination)
        io._replace_with_retry = crash
        tx.apply_transaction(
            store_root=store,
            workspace_root=workspace,
            organization_id=organization_id,
            tool="rename_shader_workspace_symbols",
            operation_id="atomic-temp-crash",
            request_sha256="f" * 64,
            targets={target: b"after"},
            checkpoint_after_apply=lambda: None,
            checkpoint_after_rollback=lambda: None,
        )
        """
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(store_root),
            str(workspace_root),
            organization_id,
            str(target),
            mode,
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )


def test_successful_transaction_cleans_host_journal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "host-transactions"
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_bytes(b"before-first")
    checkpoints: list[str] = []

    transactions.apply_transaction(
        store_root=store,
        workspace_root=workspace,
        organization_id="1" * 32,
        tool="rename_shader_workspace_symbols",
        operation_id="rename-success",
        request_sha256="2" * 64,
        targets={first: b"after-first", second: b"after-second"},
        checkpoint_after_apply=lambda: checkpoints.append("after"),
        checkpoint_after_rollback=lambda: checkpoints.append("before"),
    )

    assert first.read_bytes() == b"after-first"
    assert second.read_bytes() == b"after-second"
    assert checkpoints == ["after"]
    assert not transactions.transaction_directory(store, workspace, "1" * 32).exists()


def test_process_interruption_completes_exact_staged_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "host-transactions"
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_bytes(b"before-first")
    second.write_bytes(b"before-second")
    organization_id = "3" * 32

    crashed = _run_crashing_transaction(
        store_root=store,
        workspace_root=workspace,
        organization_id=organization_id,
        targets=[(first, b"after-first"), (second, b"after-second")],
        crash_target=second,
    )
    assert crashed.returncode == 73, crashed.stderr
    assert first.read_bytes() == b"after-first"
    assert second.read_bytes() == b"before-second"

    checkpoints: list[str] = []
    assert transactions.recover_transaction(
        store_root=store,
        workspace_root=workspace,
        organization_id=organization_id,
        checkpoint_after_apply=lambda: checkpoints.append("after"),
        checkpoint_after_rollback=lambda: checkpoints.append("before"),
    )
    assert first.read_bytes() == b"after-first"
    assert second.read_bytes() == b"after-second"
    assert checkpoints == ["after"]
    assert not transactions.transaction_directory(store, workspace, organization_id).exists()


def test_external_edit_during_interrupted_transaction_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "host-transactions"
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_bytes(b"before-first")
    second.write_bytes(b"before-second")
    organization_id = "4" * 32

    crashed = _run_crashing_transaction(
        store_root=store,
        workspace_root=workspace,
        organization_id=organization_id,
        targets=[(first, b"after-first"), (second, b"after-second")],
        crash_target=second,
    )
    assert crashed.returncode == 73, crashed.stderr
    second.write_bytes(b"external-user-edit")

    with pytest.raises(
        transactions.WorkspaceMutationRecoveryRequired,
        match="requires explicit recovery",
    ):
        transactions.recover_transaction(
            store_root=store,
            workspace_root=workspace,
            organization_id=organization_id,
            checkpoint_after_apply=lambda: None,
            checkpoint_after_rollback=lambda: None,
        )

    assert first.read_bytes() == b"after-first"
    assert second.read_bytes() == b"external-user-edit"
    directory = transactions.transaction_directory(store, workspace, organization_id)
    assert json.loads((directory / "ledger.json").read_text(encoding="utf-8"))[
        "phase"
    ] == "recovery-required"


def test_rename_io_failure_rolls_back_every_workspace_artifact(
    organization_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = organization_case
    opened = case._begin()
    proposal = Path(opened["proposalSourcePath"])
    session = Path(opened["sessionPath"])
    aliases = case.root / "ai" / "aliases.json"
    view = case.root / "ai" / "organized.hlsl"
    before = _snapshot([proposal, session, aliases, view])
    original_write = transactions.atomic_write_bytes
    failed = False

    def fail_proposal(path: Path, content: bytes) -> None:
        nonlocal failed
        if Path(path).resolve() == proposal.resolve() and not failed:
            failed = True
            raise OSError("injected proposal replace failure")
        original_write(Path(path), content)

    monkeypatch.setattr(transactions, "atomic_write_bytes", fail_proposal)
    with pytest.raises(OSError, match="injected proposal replace failure"):
        case.manager.rename(
            str(case.root),
            opened["organizationId"],
            {"MainPS": "EvaluateSurface"},
        )

    _assert_snapshot(before)
    assert not transactions.transaction_directory(
        case.manager._mutation_store, case.root, opened["organizationId"]
    ).exists()


def test_restructure_note_io_failure_rolls_back_session(
    organization_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = organization_case
    opened = case._begin()
    session = Path(opened["sessionPath"])
    before = session.read_bytes()
    original_write = transactions.atomic_write_bytes
    failed = False

    def fail_session(path: Path, content: bytes) -> None:
        nonlocal failed
        if Path(path).resolve() == session.resolve() and not failed:
            failed = True
            raise OSError("injected restructure session failure")
        original_write(Path(path), content)

    monkeypatch.setattr(transactions, "atomic_write_bytes", fail_session)
    with pytest.raises(OSError, match="injected restructure session failure"):
        case.manager.restructure(
            str(case.root),
            opened["organizationId"],
            "note",
            name="MainPS",
            comment="Describe the entry point without losing the prior draft.",
        )

    assert session.read_bytes() == before
    assert not transactions.transaction_directory(
        case.manager._mutation_store, case.root, opened["organizationId"]
    ).exists()


def test_rename_checkpoint_failure_rolls_back_source_without_touching_alias_store(
    organization_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = organization_case
    source = (
        b"cbuffer generatedCB : register(b0) { float generated; };\n"
        b"float4 MainPS() : SV_Target { return generated; }\n"
    )
    case.original = source
    case.service.source_path.write_bytes(source)
    case.service.decompiled_sha = hashlib.sha256(source).hexdigest()
    case.service.baseline_code_sha = case.service._code_sha(source)
    identity = {
        "schema": "replaylab-cbuffer-alias-identity/v1",
        "captureId": "2" * 64,
        "captureSessionId": "a" * 32,
        "eventId": 12,
        "stage": "fragment",
        "entryPoint": "MainPS",
        "shaderResourceId": 17,
        "rawSha256": "3" * 64,
        "blocks": [{
            "block": "OriginalCB",
            "bind": 0,
            "space": 0,
            "members": [{"name": "original", "offset": 0}],
            "leaves": [{"path": "original", "offset": 0, "byteSize": 4}],
            "memberMappingAvailable": True,
        }],
    }
    case.manager._cbuffer_alias_identity = lambda unused: copy.deepcopy(identity)
    alias_root = Path(case.temporary.name) / "external-cbuffer-aliases"
    monkeypatch.setenv("REPLAYLAB_ALIAS_ROOT", str(alias_root))
    opened = case._begin()
    workspace_paths = [
        Path(opened["proposalSourcePath"]),
        Path(opened["sessionPath"]),
        case.root / "ai" / "aliases.json",
        case.root / "ai" / "organized.hlsl",
    ]
    before = _snapshot(workspace_paths)
    key = cbuffer_aliases.layout_sha256(
        "OriginalCB", [{"name": "original", "offset": 0}]
    )
    live_alias = cbuffer_aliases.layout_alias_path(key, alias_root)
    assert not live_alias.exists()
    original_checkpoint = case.manager._remember_mutation_session
    attempts = 0

    def fail_first_checkpoint(session_dir: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected checkpoint failure after file commit")
        original_checkpoint(session_dir)

    monkeypatch.setattr(case.manager, "_remember_mutation_session", fail_first_checkpoint)
    with pytest.raises(OSError, match="injected checkpoint failure"):
        case.manager.rename(
            str(case.root),
            opened["organizationId"],
            {"generatedCB": "Material", "generated": "albedo"},
        )

    _assert_snapshot(before)
    assert attempts == 2
    assert not live_alias.exists()
    assert not transactions.transaction_directory(
        case.manager._mutation_store, case.root, opened["organizationId"]
    ).exists()


def test_begin_recovers_interrupted_restructure_before_session_validation(
    organization_case,
) -> None:
    case = organization_case
    opened = case._begin()
    organization_id = opened["organizationId"]
    session_path = Path(opened["sessionPath"])
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["functionNotes"] = {"MainPS": "Recovered after a process interruption."}
    session = case.manager._seal_session(session)
    session_after = (
        json.dumps(session, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    alias_view = case.root / "ai" / "aliases.json"
    alias_after = b'{"schema":"replaylab-shader-aliases/v1","recovered":true}\n'

    crashed = _run_crashing_transaction(
        store_root=case.manager._mutation_store,
        workspace_root=case.root,
        organization_id=organization_id,
        targets=[(session_path, session_after), (alias_view, alias_after)],
        crash_target=alias_view,
    )
    assert crashed.returncode == 73, crashed.stderr
    assert json.loads(session_path.read_text(encoding="utf-8"))["functionNotes"]
    assert not alias_view.exists()

    restarted = ShaderOrganizationManager(
        lambda: case.service, case.manager._validate_candidate
    )
    resumed = restarted.begin(str(case.root))
    assert resumed["resumed"] is True
    assert resumed["organizationId"] == organization_id
    assert resumed["progress"]["functionNotes"] == 1
    assert alias_view.read_bytes() == alias_after
    assert not transactions.transaction_directory(
        restarted._mutation_store, case.root, organization_id
    ).exists()


@pytest.mark.parametrize(
    ("mode", "expected_recovered", "expected_content"),
    [
        ("first-ledger", False, b"before"),
        ("preparing", True, b"before"),
        ("applying", True, b"after"),
    ],
)
def test_atomic_write_crash_windows_recover_without_orphaning_journal(
    tmp_path: Path,
    mode: str,
    expected_recovered: bool,
    expected_content: bytes,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "transactions"
    target = workspace / "target.txt"
    target.write_bytes(b"before")
    organization_id = "5" * 32

    crashed = _run_atomic_replace_crash(
        store_root=store,
        workspace_root=workspace,
        organization_id=organization_id,
        target=target,
        mode=mode,
    )
    assert crashed.returncode == 74, crashed.stderr
    directory = transactions.transaction_directory(store, workspace, organization_id)
    assert directory.is_dir()
    live_temps = list(workspace.glob(".target.txt.tmp-*"))

    assert transactions.recover_transaction(
        store_root=store,
        workspace_root=workspace,
        organization_id=organization_id,
        checkpoint_after_apply=lambda: None,
        checkpoint_after_rollback=lambda: None,
    ) is expected_recovered
    assert target.read_bytes() == expected_content
    assert not directory.exists()
    if mode == "applying":
        # A live-directory temporary is not authenticated by the journal and
        # must not be guessed/deleted.  A separate bounded TTL janitor may own it.
        assert live_temps
        assert all(path.exists() for path in live_temps)


def test_preledger_unknown_entry_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "transactions"
    organization_id = "6" * 32
    directory = transactions.transaction_directory(store, workspace, organization_id)
    directory.mkdir(parents=True)
    unknown = directory / "do-not-delete.txt"
    unknown.write_text("external evidence", encoding="utf-8")

    with pytest.raises(
        transactions.WorkspaceMutationRecoveryRequired,
        match="no recoverable ledger",
    ):
        transactions.recover_transaction(
            store_root=store,
            workspace_root=workspace,
            organization_id=organization_id,
            checkpoint_after_apply=lambda: None,
        )
    assert unknown.read_text(encoding="utf-8") == "external evidence"


def test_preledger_cleanup_entry_limit_is_bounded_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "transactions"
    organization_id = "f" * 32
    directory = transactions.transaction_directory(store, workspace, organization_id)
    directory.mkdir(parents=True)
    monkeypatch.setattr(transactions, "_MAX_CLEANUP_ENTRIES", 3)
    temps = []
    for index in range(4):
        path = directory / (".ledger.json.tmp-%032x" % index)
        path.write_bytes(b"temporary")
        temps.append(path)

    with pytest.raises(
        transactions.WorkspaceMutationRecoveryRequired,
        match="no recoverable ledger",
    ):
        transactions.recover_transaction(
            store_root=store,
            workspace_root=workspace,
            organization_id=organization_id,
            checkpoint_after_apply=lambda: None,
        )
    assert all(path.read_bytes() == b"temporary" for path in temps)


def test_transaction_target_growth_after_stat_is_hard_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"ok")
    original_open = Path.open
    grew = False

    def grow_before_read(path: Path, mode="r", *args, **kwargs):
        nonlocal grew
        if path == target and mode == "rb" and not grew:
            grew = True
            with original_open(path, "wb") as stream:
                stream.write(b"x" * 128)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(transactions, "MAX_TARGET_BYTES", 8)
    monkeypatch.setattr(Path, "open", grow_before_read)
    with pytest.raises(ValueError, match="exceeds its size limit"):
        transactions._read_target(target)
    assert grew is True


def test_json_growth_after_stat_is_hard_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "ledger.json"
    target.write_bytes(b"{}")
    from src import shared_file_io
    original_open = shared_file_io.open_shared_read
    grew = False

    def grow_before_read(path: Path, *args, **kwargs):
        nonlocal grew
        if path == target and not grew:
            grew = True
            with path.open("wb") as stream:
                stream.write(b'{"value":"' + b"x" * 128 + b'"}')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(shared_file_io, "open_shared_read", grow_before_read)
    with pytest.raises(ValueError, match="exceeds its 16-byte size limit"):
        workspace_io.read_json_object(target, 16, "test ledger")
    assert grew is True


def test_committed_cleanup_failure_never_rolls_back_after_images(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "transactions"
    target = workspace / "target.txt"
    target.write_bytes(b"before")
    organization_id = "7" * 32
    directory = transactions.transaction_directory(store, workspace, organization_id)

    def checkpoint() -> None:
        (directory / "unknown-owner.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(
        transactions.WorkspaceMutationRecoveryRequired,
        match="committed cleanup failed",
    ):
        transactions.apply_transaction(
            store_root=store,
            workspace_root=workspace,
            organization_id=organization_id,
            tool="rename_shader_workspace_symbols",
            operation_id="cleanup-failure",
            request_sha256="7" * 64,
            targets={target: b"after"},
            checkpoint_after_apply=checkpoint,
        )

    assert target.read_bytes() == b"after"
    assert (directory / "unknown-owner.txt").read_text(encoding="utf-8") == "keep"
    ledger = json.loads((directory / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["phase"] == "recovery-required"


def test_transient_committed_cleanup_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "transactions"
    target = workspace / "target.txt"
    target.write_bytes(b"before")
    organization_id = "e" * 32
    original_cleanup = transactions._cleanup

    monkeypatch.setattr(
        transactions,
        "_cleanup",
        lambda unused_directory, unused_rows: (_ for _ in ()).throw(
            PermissionError("temporary sharing violation")
        ),
    )
    with pytest.raises(
        transactions.WorkspaceMutationRecoveryRequired,
        match="will be retried",
    ):
        transactions.apply_transaction(
            store_root=store,
            workspace_root=workspace,
            organization_id=organization_id,
            tool="rename_shader_workspace_symbols",
            operation_id="transient-cleanup",
            request_sha256="e" * 64,
            targets={target: b"after"},
            checkpoint_after_apply=lambda: None,
        )
    assert target.read_bytes() == b"after"
    directory = transactions.transaction_directory(store, workspace, organization_id)
    assert json.loads((directory / "ledger.json").read_text(encoding="utf-8"))[
        "phase"
    ] == "committed"

    monkeypatch.setattr(transactions, "_cleanup", original_cleanup)
    assert transactions.recover_transaction(
        store_root=store,
        workspace_root=workspace,
        organization_id=organization_id,
        checkpoint_after_apply=lambda: None,
    )
    assert not directory.exists()


def test_junction_parent_is_rejected_before_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    redirected = workspace / "redirected"
    redirected.mkdir(parents=True)
    target = redirected / "target.txt"
    store = tmp_path / "transactions"
    original = transactions._is_redirected

    monkeypatch.setattr(
        transactions,
        "_is_redirected",
        lambda path: Path(path) == redirected or original(Path(path)),
    )
    with pytest.raises(ValueError, match="redirected"):
        transactions.apply_transaction(
            store_root=store,
            workspace_root=workspace,
            organization_id="8" * 32,
            tool="rename_shader_workspace_symbols",
            operation_id="junction-preflight",
            request_sha256="8" * 64,
            targets={target: b"after"},
            checkpoint_after_apply=lambda: None,
        )
    assert not target.exists()


def test_parent_redirect_swap_after_snapshot_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    parent = workspace / "parent"
    parent.mkdir(parents=True)
    target = parent / "target.txt"
    target.write_bytes(b"before")
    store = tmp_path / "transactions"
    organization_id = "9" * 32
    redirected = False
    original_redirected = transactions._is_redirected
    original_write_ledger = transactions._write_ledger

    def is_redirected(path: Path) -> bool:
        return (redirected and Path(path) == parent) or original_redirected(Path(path))

    def flip_after_applying(path: Path, ledger) -> None:
        nonlocal redirected
        original_write_ledger(path, ledger)
        if ledger.get("phase") == "applying":
            redirected = True

    monkeypatch.setattr(transactions, "_is_redirected", is_redirected)
    monkeypatch.setattr(transactions, "_write_ledger", flip_after_applying)
    with pytest.raises(transactions.WorkspaceMutationRecoveryRequired):
        transactions.apply_transaction(
            store_root=store,
            workspace_root=workspace,
            organization_id=organization_id,
            tool="rename_shader_workspace_symbols",
            operation_id="junction-swap",
            request_sha256="9" * 64,
            targets={target: b"after"},
            checkpoint_after_apply=lambda: None,
        )
    assert target.read_bytes() == b"before"
    directory = transactions.transaction_directory(store, workspace, organization_id)
    assert json.loads((directory / "ledger.json").read_text(encoding="utf-8"))[
        "phase"
    ] == "recovery-required"


def test_workspace_slot_blocks_second_organization_until_owner_recovers(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "transactions"
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_bytes(b"before-first")
    second.write_bytes(b"before-second")
    owner = "a" * 32
    other = "b" * 32

    crashed = _run_crashing_transaction(
        store_root=store,
        workspace_root=workspace,
        organization_id=owner,
        targets=[(first, b"after-first"), (second, b"after-second")],
        crash_target=second,
    )
    assert crashed.returncode == 73, crashed.stderr
    assert transactions.pending_transaction_owner(
        store_root=store, workspace_root=workspace
    ) == owner
    assert transactions.transaction_directory(
        store, workspace, owner
    ) == transactions.transaction_directory(store, workspace, other)

    with pytest.raises(
        transactions.WorkspaceMutationRecoveryRequired,
        match="previous workspace mutation",
    ):
        transactions.apply_transaction(
            store_root=store,
            workspace_root=workspace,
            organization_id=other,
            tool="restructure_shader_workspace_source",
            operation_id="other-organization",
            request_sha256="b" * 64,
            targets={workspace / "other.txt": b"other"},
            checkpoint_after_apply=lambda: None,
        )
    assert not (workspace / "other.txt").exists()

    assert transactions.recover_transaction(
        store_root=store,
        workspace_root=workspace,
        organization_id=owner,
        checkpoint_after_apply=lambda: None,
    )
    transactions.apply_transaction(
        store_root=store,
        workspace_root=workspace,
        organization_id=other,
        tool="restructure_shader_workspace_source",
        operation_id="other-organization",
        request_sha256="b" * 64,
        targets={workspace / "other.txt": b"other"},
        checkpoint_after_apply=lambda: None,
    )
    assert (workspace / "other.txt").read_bytes() == b"other"


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path spelling")
def test_extended_and_normal_workspace_paths_share_lock_and_recovery(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    extended = Path("\\\\?\\" + str(workspace))
    store = tmp_path / "transactions"
    target = workspace / "target.txt"
    target.write_bytes(b"before")
    organization_id = "c" * 32
    crashed = _run_crashing_transaction(
        store_root=store,
        workspace_root=workspace,
        organization_id=organization_id,
        targets=[(target, b"after"), (workspace / "later.txt", b"later")],
        crash_target=workspace / "later.txt",
    )
    assert crashed.returncode == 73, crashed.stderr
    assert transactions.transaction_directory(
        store, workspace, organization_id
    ) == transactions.transaction_directory(store, extended, organization_id)
    assert transactions.recover_transaction(
        store_root=store,
        workspace_root=extended,
        organization_id=organization_id,
        checkpoint_after_apply=lambda: None,
    )
    assert target.read_bytes() == b"after"


def test_organization_private_stage_never_nests_random_alias_lock_order(
    tmp_path: Path,
) -> None:
    alias_root = tmp_path / "z-live-aliases"
    user_root = tmp_path / "zz-user-aliases"
    alias_root.mkdir()
    user_root.mkdir()
    source = (
        "cbuffer generatedCB : register(b0) { float generated; };\n"
        "float4 MainPS() : SV_Target { return generated; }\n"
    )

    with pytest.raises(
        ShaderOrganizationError,
        match="explicit publish_cbuffer_aliases tool",
    ):
        ShaderOrganizationManager._stage_cbuffer_aliases(
            source,
            {"generatedCB": "Material", "generated": "albedo"},
            None,
            alias_root,
            "project",
            user_root,
        )
    assert list(alias_root.iterdir()) == []
    assert list(user_root.iterdir()) == []


def test_open_session_recovers_workspace_slot_owner_before_other_sessions(
    organization_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = organization_case
    opened = case._begin()
    owner = opened["organizationId"]
    other = "0" * 32
    assert other != owner and other < owner
    sessions_dir = case.root / "ai" / "organization-sessions"
    other_dir = sessions_dir / other
    other_dir.mkdir()
    (other_dir / "session.json").write_text("{}", encoding="utf-8")
    owner_session = json.loads(Path(opened["sessionPath"]).read_text(encoding="utf-8"))
    owner_session["createdAt"] = "2026-09-20T02:00:00Z"
    other_session = dict(owner_session)
    other_session["organizationId"] = other
    other_session["createdAt"] = "2026-09-20T01:00:00Z"
    other_session["workspacePath"] = str(case.root.resolve())

    first = case.root / "owner-first.txt"
    second = case.root / "owner-second.txt"
    first.write_bytes(b"before-first")
    second.write_bytes(b"before-second")
    crashed = _run_crashing_transaction(
        store_root=case.manager._mutation_store,
        workspace_root=case.root,
        organization_id=owner,
        targets=[(first, b"after-first"), (second, b"after-second")],
        crash_target=second,
    )
    assert crashed.returncode == 73, crashed.stderr

    recovery_calls: list[str] = []

    def recover(root: Path, organization_id: str, unused_session_dir: Path) -> None:
        recovery_calls.append(organization_id)
        transactions.recover_transaction(
            store_root=case.manager._mutation_store,
            workspace_root=root,
            organization_id=organization_id,
            checkpoint_after_apply=lambda: None,
            checkpoint_after_rollback=lambda: None,
        )

    def read_session(session_dir: Path, unused_description: str):
        return dict(owner_session if session_dir.name == owner else other_session)

    monkeypatch.setattr(case.manager, "_recover_pending_mutation", recover)
    monkeypatch.setattr(organization_module, "_read_session_file", read_session)
    monkeypatch.setattr(case.manager._checkpoints, "has_pending", lambda *args: False)

    resumed = case.manager.open_session(case.root)
    assert resumed is not None
    assert resumed["organizationId"] == owner
    assert recovery_calls == [owner]
    assert first.read_bytes() == b"after-first"
    assert second.read_bytes() == b"after-second"


def test_workspace_lock_serializes_different_organizations_across_processes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "transactions"
    ready = tmp_path / "holder-ready"
    attempted = tmp_path / "waiter-attempted"
    release = tmp_path / "release-holder"
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    script = textwrap.dedent(
        """
        from pathlib import Path
        import sys
        import time
        from src.shader_workspace import workspace_mutation_transaction as tx

        store, workspace, organization_id, target = map(Path, sys.argv[1:5])
        mode = sys.argv[5]
        signal = Path(sys.argv[6])
        release = Path(sys.argv[7])
        if mode == "waiter":
            signal.write_text("attempted", encoding="utf-8")
            checkpoint = lambda: None
        else:
            def checkpoint():
                signal.write_text("ready", encoding="utf-8")
                for unused in range(1000):
                    if release.exists():
                        return
                    time.sleep(0.01)
                raise RuntimeError("holder release timed out")
        tx.apply_transaction(
            store_root=store,
            workspace_root=workspace,
            organization_id=organization_id.name,
            tool="restructure_shader_workspace_source",
            operation_id=mode,
            request_sha256="d" * 64,
            targets={target: mode.encode("utf-8")},
            checkpoint_after_apply=checkpoint,
        )
        """
    )
    cwd = Path(__file__).resolve().parents[1]
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(store),
            str(workspace),
            "a" * 32,
            str(first),
            "holder",
            str(ready),
            str(release),
        ],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    waiter = None
    try:
        for unused in range(500):
            if ready.exists():
                break
            assert holder.poll() is None, holder.communicate()[1]
            time.sleep(0.01)
        assert ready.exists()
        waiter = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(store),
                str(workspace),
                "b" * 32,
                str(second),
                "waiter",
                str(attempted),
                str(release),
            ],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for unused in range(500):
            if attempted.exists():
                break
            assert waiter.poll() is None, waiter.communicate()[1]
            time.sleep(0.01)
        assert attempted.exists()
        time.sleep(0.1)
        assert waiter.poll() is None
        assert not second.exists()
        release.write_text("release", encoding="utf-8")
        holder_output, holder_error = holder.communicate(timeout=10)
        waiter_output, waiter_error = waiter.communicate(timeout=10)
        assert holder.returncode == 0, holder_output + holder_error
        assert waiter.returncode == 0, waiter_output + waiter_error
    finally:
        release.touch(exist_ok=True)
        for process in (holder, waiter):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    assert first.read_bytes() == b"holder"
    assert second.read_bytes() == b"waiter"
