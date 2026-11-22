"""Capture-only projects must switch without an optional asset index."""
import json
from unittest import mock

import pytest

from src import server
from src.active_projects import ActiveReplayProjectService


def test_public_activation_switches_game_and_api_without_unpacked_assets(tmp_path, monkeypatch):
    service = ActiveReplayProjectService(tmp_path / "config/active.json", tmp_path / "projects")
    projects = [service.create_project(name="Game " + api, game_id="game-" + api,
                build_id="test", api_family=api) for api in ("d3d11", "d3d12", "opengl", "vulkan")]
    monkeypatch.setattr(server, "_active_project_service", lambda: service)
    refresh = mock.Mock(side_effect=AssertionError("capture-only project must not index assets"))
    monkeypatch.setattr(server, "_ensure_asset_activation_content_refresh", refresh)
    for target in projects + projects[::-1]:
        result = json.loads(server.activate_active_replay_project(target["projectRoot"]))
        assert "error" not in result, result
        assert result["assetCatalogRefresh"] is None
        assert result["project"]["workspaceId"] == target["workspaceId"]
        current = service.status()
        assert current["workspaceId"] == target["workspaceId"]
        assert current["scope"] == target["scope"]
        assert current["scopeConfigured"] is True and current["assetRootConfigured"] is False
    refresh.assert_not_called()


def test_activation_keeps_configured_unavailable_asset_root_diagnostic(tmp_path, monkeypatch):
    service = ActiveReplayProjectService(tmp_path / "config/active.json", tmp_path / "projects")
    assets = tmp_path / "assets"
    assets.mkdir()
    target = service.create_project(name="Assets", game_id="with-assets", build_id="test", asset_root=assets)
    service.create_project(name="Other", game_id="other", build_id="test")
    assets.rmdir()
    monkeypatch.setattr(server, "_active_project_service", lambda: service)
    # Use the actual asset boundary: an explicitly configured missing path
    # remains a diagnostic, and the already committed selection is observable.
    result = json.loads(server.activate_active_replay_project(target["projectRoot"]))
    assert "configured active-project asset root is unavailable" in result["error"]
    assert service.status()["workspaceId"] == target["workspaceId"]
