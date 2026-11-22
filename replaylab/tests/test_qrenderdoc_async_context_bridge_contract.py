from __future__ import annotations

from pathlib import Path
from typing import Optional


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_ROOT = REPOSITORY_ROOT / "qrenderdoc" / "Code" / "ReplayLab"
BRIDGE_SOURCE = (
    REPOSITORY_ROOT / "replaylab" / "renderdoc_mcp_bridge" / "__init__.py"
)


def _source(name: str) -> str:
    return (INTEGRATION_ROOT / name).read_text(encoding="utf-8")


def _section(source: str, start: str, end: Optional[str] = None) -> str:
    begin = source.index(start)
    return source[begin:] if end is None else source[begin : source.index(end, begin)]


def test_native_context_freeze_bridge_is_pollable_and_keeps_legacy_entry_point() -> None:
    header = _source("ReplayLabIntegration.h")
    source = _source("ReplayLabIntegration.cpp")

    assert "CurrentContextJSON(QString *error = NULL) const;" in header
    assert "CurrentContextJSON(const QString &focusStage, QString *error) const;" in header
    for method in (
        "BeginCurrentContextFreezeJSON",
        "PollCurrentContextFreezeJSON",
        "AbandonCurrentContextFreezeJSON",
        "ValidateCurrentContextFreezeResultPath",
    ):
        assert method in header
        assert "ReplayLabIntegration::" + method in source

    begin = _section(
        source,
        "QString ReplayLabIntegration::BeginCurrentContextFreezeJSON(",
        "QString ReplayLabIntegration::PollCurrentContextFreezeJSON(",
    )
    poll = _section(
        source,
        "QString ReplayLabIntegration::PollCurrentContextFreezeJSON(",
        "QString ReplayLabIntegration::AbandonCurrentContextFreezeJSON(",
    )
    abandon = _section(
        source,
        "QString ReplayLabIntegration::AbandonCurrentContextFreezeJSON(",
    )

    assert '"begin_native_context_freeze_json"' in begin
    assert "qint64 expectedCurrentEventId" in begin
    assert "qint64 expectedSelectedEventId" in begin
    assert "expectedCurrentEventId > 0 ? QString::number(expectedCurrentEventId)" in begin
    assert "expectedSelectedEventId >= 0 ? QString::number(expectedSelectedEventId)" in begin
    assert "expectedCaptureSessionId" in begin
    assert '"poll_native_context_freeze_json"' in poll
    assert '"abandon_native_context_freeze_json"' in abandon
    for entry_point in (begin, poll, abandon):
        assert "InvokeInternalIntegration" in entry_point
        assert "BlockInvoke" not in entry_point

    # The real-time UI path must stay entirely outside the embedded Python GIL after Begin.
    assert "polls the returned result file with QFile" in header
    assert "must not be" in header and "used for real-time UI polling" in header
    assert "replaylab-current-context-freeze-job/v1" in header


def test_native_context_result_path_is_bound_to_this_qrenderdoc_instance() -> None:
    source = _source("ReplayLabIntegration.cpp")
    validator = _section(
        source,
        "QString ReplayLabIntegration::ValidateCurrentContextFreezeResultPath(",
    )

    assert 'qgetenv("LOCALAPPDATA")' in validator
    assert 'lit("ReplayLab/ipc-v2/instances")' in validator
    assert "QCoreApplication::applicationPid()" in validator
    assert 'lit("native-context-jobs/%1.json").arg(jobId)' in validator
    assert "isLowerHex32(jobId)" in validator
    assert "isLowerHex32(instanceNonce)" in validator
    assert "!jobDirectoryInfo.isSymLink()" in validator
    assert "!instanceDirectoryInfo.isSymLink()" in validator
    assert "!suppliedInfo.isSymLink()" in validator


def test_submit_time_context_freeze_uses_the_foreground_fifo_replay_lane() -> None:
    source = BRIDGE_SOURCE.read_text(encoding="utf-8")
    declaration = _section(
        source,
        "CURRENT_CONTEXT_FREEZE_REPLAY_TAG_PREFIX = (",
        "MAX_EVENT_SHADER_OVERRIDES",
    )
    begin = _section(
        source,
        "def _begin_current_context_freeze(",
        "def _poll_current_context_freeze(",
    )

    assert '"ReplayLab.current-context-freeze."' in declaration
    assert "ReplayLab.background." not in declaration
    assert "CURRENT_CONTEXT_FREEZE_REPLAY_TAG_PREFIX + job_id" in begin
