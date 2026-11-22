"""Discovery is atomic and immediately readable, without durable user-data writes."""
import json
from types import SimpleNamespace
import pytest

from test_qrenderdoc_ai_background_bridge_contract import _load_bridge


def test_discovery_skips_fsync_and_keeps_capture_identity(tmp_path, monkeypatch):
    bridge = _load_bridge()
    path = tmp_path / "ipc" / "instance.json"
    bridge.INSTANCE_FILE = str(path)
    bridge._ctx = SimpleNamespace(IsCaptureLoaded=lambda: True,
                                  GetCaptureFilename=lambda: "D:/白银/1.rdc")
    bridge._capture_session_id = "current-capture-session"
    calls = []
    monkeypatch.delenv("RENDERDOC_UI_DURABLE_DISCOVERY_MANIFEST", raising=False)
    monkeypatch.setattr(bridge.os, "fsync", lambda fd: calls.append(fd))
    bridge._publish_instance_manifest(activity=True)
    value = json.loads(path.read_text(encoding="utf-8"))
    assert not calls
    assert value["schema"] == "replaylab-bridge-instance/v1"
    assert value["captureLoaded"] is True
    assert value["captureSessionId"] == "current-capture-session"
    assert value["capturePath"] == "D:/白银/1.rdc"
    assert value["lastActivityUnixMs"] > 0
    assert list(path.parent.iterdir()) == [path]

    # The diagnostic control changes only discovery durability, not its identity.
    monkeypatch.setenv("RENDERDOC_UI_DURABLE_DISCOVERY_MANIFEST", "1")
    bridge._publish_instance_manifest(stopped=True)
    assert len(calls) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["stopped"] is True


def test_normal_json_writes_remain_durable_and_failure_preserves_old_file(tmp_path, monkeypatch):
    bridge = _load_bridge()
    path = tmp_path / "user-data.json"
    calls = []
    monkeypatch.setattr(bridge.os, "fsync", lambda fd: calls.append(fd))
    bridge._atomic_write_json(str(path), {"revision": 1})
    assert len(calls) == 1

    original_replace = bridge.os.replace
    def check_replace(source, target):
        # Readers retain the previous complete value before the atomic rename.
        assert json.loads(path.read_text(encoding="utf-8")) == {"revision": 1}
        assert json.loads(open(source, encoding="utf-8").read()) == {"revision": 2}
        return original_replace(source, target)
    monkeypatch.setattr(bridge.os, "replace", check_replace)
    bridge._atomic_write_json(str(path), {"revision": 2}, durable=False)
    assert len(calls) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == {"revision": 2}
    assert list(tmp_path.iterdir()) == [path]

    def failed_replace(source, target):
        raise OSError("injected atomic replacement failure")
    monkeypatch.setattr(bridge.os, "replace", failed_replace)
    with pytest.raises(OSError, match="injected atomic replacement failure"):
        bridge._atomic_write_json(str(path), {"revision": 3})
    assert len(calls) == 2
    assert json.loads(path.read_text(encoding="utf-8")) == {"revision": 2}
    assert list(tmp_path.iterdir()) == [path]
