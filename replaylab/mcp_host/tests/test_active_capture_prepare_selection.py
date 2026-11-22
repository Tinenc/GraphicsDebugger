"""Capture preparation never holds the project selection during replay calls."""
import json
from unittest import mock

import pytest
import test_active_project_mcp as harness


@pytest.fixture
def environment():
    harness.ActiveProjectMcpTests.setUpClass()
    case = harness.ActiveProjectMcpTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


@pytest.mark.parametrize("assets", [False, True])
def test_capture_prepare_releases_selection_for_replay_progress_and_asset_scan(environment, assets):
    case = environment
    if assets:
        case._bound_with_assets()
    else:
        case._json(case.server.ensure_active_replay_project("Example Game"))
    original_call = case.ipc.call
    original_scan = harness._FakeSymbolService.scan
    observed = []

    def available(kind):
        assert not case.service._selection_lock_path.exists(), kind
        observed.append(kind)

    def call(method, *args, **kwargs):
        available("ipc:" + method)
        return original_call(method, *args, **kwargs)

    def scan(instance, *args, **kwargs):
        available("asset-scan")
        return original_scan(instance, *args, **kwargs)

    with mock.patch.object(case.ipc, "call", side_effect=call), \
         mock.patch.object(case.server, "SymbolService", harness._FakeSymbolService), \
         mock.patch.object(harness._FakeSymbolService, "scan", scan):
        result = case._json(case.server._prepare_active_capture_project(
            progress_callback=lambda *_args, **_kwargs: available("progress"),
            control_checkpoint=lambda *_args: available("control")))
    assert result["stagedCatalog"]["captureId"] == case.ipc.capture_id
    assert "progress" in observed and "control" in observed
    if assets:
        assert "asset-scan" in observed and "ipc:load_symbol_aliases" in observed


def test_switch_during_asset_scan_prevents_alias_publication_and_overlay(environment):
    case = environment
    case._bound_with_assets()
    original_scan = harness._FakeSymbolService.scan
    alternate = case.root / "other-project"
    previous = case.service.status()
    other = case.service.create_project(
        project_root=str(alternate), name="Other Project", game_id="", build_id="")
    case.service.activate(previous["projectRoot"])

    def scan(instance, *args, **kwargs):
        assert not case.service._selection_lock_path.exists()
        case.service.activate(other["projectRoot"])
        return original_scan(instance, *args, **kwargs)

    with mock.patch.object(case.server, "SymbolService", harness._FakeSymbolService), \
         mock.patch.object(harness._FakeSymbolService, "scan", scan):
        result = json.loads(case.server.prepare_active_capture_project())
    assert "selection or configuration changed" in result["error"]
    assert not any(method == "load_symbol_aliases" for method, *_rest in case.ipc.calls)
    assert case.service.status()["projectRoot"] == other["projectRoot"]


def test_import_can_fill_initially_unspecified_platform_before_snapshot(environment):
    case = environment
    case._json(case.server.ensure_active_replay_project("Example Game"))
    case._json(case.server.configure_active_replay_project("example-game", "build-1"))
    assert case.service.status()["scope"]["apiFamily"] == ""
    result = case._json(case.server.prepare_active_capture_project())
    assert result["project"]["scope"]["apiFamily"] == "d3d11"
    assert result["project"]["scope"]["platform"] == "windows-x64"
