from __future__ import annotations

import argparse
import hashlib
import json
from types import SimpleNamespace

import pytest


def _canonical_native_context() -> dict[str, object]:
    context: dict[str, object] = {
        "schema": "renderdoc-ultra-current-context/v1",
        "schemaVersion": 1,
        "capture": {
            "loaded": True,
            "captureSessionId": "a" * 32,
            "path": "C:/captures/冻结上下文.rdc",
        },
        "events": {
            "currentEventId": 142,
            "selectedEventId": 140,
        },
    }
    encoded = json.dumps(
        context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    context["snapshotId"] = hashlib.sha256(encoded).hexdigest()
    return context


def test_context_file_requires_and_returns_exact_instance(monkeypatch, tmp_path) -> None:
    import replaylab_agent.cli as cli

    context_file = tmp_path / "context.json"
    context_file.write_text(
        json.dumps({"capture": {"path": "C:/captures/frozen.rdc"}}),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        context_file=context_file,
        live_context=False,
        instance_id="",
    )
    with pytest.raises(ValueError, match="requires --instance-id"):
        cli._read_context(args)

    class FakeBridge:
        def select_instance(self, instance_id):
            assert instance_id == "qrd-1"
            return SimpleNamespace(
                id="qrd-1",
                capture_loaded=True,
                capture_path="C:/captures/frozen.rdc",
            )

    monkeypatch.setattr(cli, "BridgeClient", FakeBridge)
    args.instance_id = "qrd-1"
    context, instance_id, capture_path = cli._read_context(args)
    assert context["capture"]["path"] == capture_path
    assert instance_id == "qrd-1"


def test_context_file_accepts_canonical_native_snapshot(monkeypatch, tmp_path) -> None:
    import replaylab_agent.cli as cli

    frozen_context = _canonical_native_context()
    context_file = tmp_path / "native-context.json"
    context_file.write_text(
        json.dumps(frozen_context, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    class FakeBridge:
        def select_instance(self, instance_id):
            assert instance_id == "qrd-native"
            return SimpleNamespace(
                id="qrd-native",
                capture_loaded=True,
                capture_path="C:/captures/fallback.rdc",
            )

    monkeypatch.setattr(cli, "BridgeClient", FakeBridge)
    args = argparse.Namespace(
        context_file=context_file,
        live_context=False,
        instance_id="qrd-native",
    )

    context, instance_id, capture_path = cli._read_context(args)

    assert context == frozen_context
    assert instance_id == "qrd-native"
    assert capture_path == "C:/captures/冻结上下文.rdc"


def test_context_file_rejects_tampered_native_snapshot(monkeypatch, tmp_path) -> None:
    import replaylab_agent.cli as cli

    frozen_context = _canonical_native_context()
    frozen_context["events"]["currentEventId"] = 143
    context_file = tmp_path / "tampered-native-context.json"
    context_file.write_text(
        json.dumps(frozen_context, ensure_ascii=False),
        encoding="utf-8",
    )

    class BridgeMustNotBeCalled:
        def __init__(self):
            raise AssertionError("tampered snapshots must be rejected before bridge access")

    monkeypatch.setattr(cli, "BridgeClient", BridgeMustNotBeCalled)
    args = argparse.Namespace(
        context_file=context_file,
        live_context=False,
        instance_id="qrd-native",
    )

    with pytest.raises(ValueError, match="snapshot content hash does not match"):
        cli._read_context(args)
