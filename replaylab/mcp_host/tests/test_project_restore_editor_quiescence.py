import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src import internal_tool_service, project_restore
from src.ipc_client import bind_direct_tool_call


def test_restore_pauses_only_its_exact_editor_and_preserves_durable_consent(monkeypatch, tmp_path):
    current = {'instanceId': 'gui-one', 'captureSessionId': 'a' * 32}
    foreign = {'instanceId': 'gui-two', 'captureSessionId': 'b' * 32}
    def job(name, workspace, binding):
        return SimpleNamespace(live_edit_id=name, workspace=workspace,
            session=SimpleNamespace(binding=binding), is_terminal=lambda: False, stop=Mock())
    own = job('own', tmp_path, current)
    other_gui = job('other', tmp_path, foreign)
    other_workspace = job('unrelated', tmp_path / 'another', current)
    key = os.path.normcase(str(tmp_path.resolve()))
    registry = Mock()
    manager = SimpleNamespace(_lifecycle_lock=threading.RLock(), _lock=threading.RLock(),
        _jobs={x.live_edit_id: x for x in (own, other_gui, other_workspace)},
        _active_by_workspace={key: own.live_edit_id}, _registry=registry)
    monkeypatch.setattr(internal_tool_service, '_SHADER_LIVE_EDIT_MANAGER', manager)
    with bind_direct_tool_call(current['instanceId'], current['captureSessionId']):
        assert project_restore._suspend_workspace_watchers(SimpleNamespace(), tmp_path) is None
    own.stop.assert_called_once()
    other_gui.stop.assert_not_called()
    other_workspace.stop.assert_not_called()
    assert key not in manager._active_by_workspace
    assert registry.mock_calls == []
