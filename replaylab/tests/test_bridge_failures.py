from __future__ import annotations

import json
import os
import time

import pytest

from replaylab_agent.bridge import BridgeClient
from replaylab_agent.errors import BridgeError


def _client_with_instance(tmp_path, monkeypatch) -> tuple[BridgeClient, object]:
    instance = tmp_path / "instances" / "中文实例"
    (instance / "requests").mkdir(parents=True)
    (instance / "responses").mkdir()
    (instance / "instance.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "stopped": False,
                "heartbeatUnixMs": int(time.time() * 1000),
                "lastActivityUnixMs": int(time.time() * 1000),
                "protocolVersion": 2,
                "bridgeVersion": "test",
                "instanceId": "中文实例",
                "captureLoaded": True,
                "capturePath": "D:/项目/捕获/测试帧.rdc",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("replaylab_agent.bridge._pid_is_live", lambda _pid: True)
    client = BridgeClient(tmp_path, timeout=0.5)
    return client, instance


def test_bridge_manifest_preserves_chinese_capture_path(tmp_path, monkeypatch) -> None:
    client, _instance = _client_with_instance(tmp_path, monkeypatch)

    instances = client.instances()

    assert len(instances) == 1
    assert instances[0].id == "中文实例"
    assert instances[0].capture_path == "D:/项目/捕获/测试帧.rdc"


@pytest.mark.parametrize(
    "response, message",
    [
        (b"\xff\xfe\n", "not valid UTF-8"),
        (b"{not-json}\n", "malformed JSON"),
        (b"[]\n", "non-object JSON"),
    ],
)
def test_bridge_malformed_response_fails_immediately(
    tmp_path, monkeypatch, response, message
) -> None:
    client, instance = _client_with_instance(tmp_path, monkeypatch)

    def respond(_request_path, payload):
        response_path = instance / "responses" / f"{payload['id']}.json"
        response_path.write_bytes(response)

    monkeypatch.setattr(client, "_atomic_json", respond)
    started = time.monotonic()

    with pytest.raises(BridgeError, match=message):
        client.call("get_current_renderdoc_context", instance_id="中文实例")

    assert time.monotonic() - started < 0.25
