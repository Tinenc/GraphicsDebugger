from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from src import server


def _status(project, error="catalog is unavailable"):
    service = mock.Mock()
    if isinstance(project, Exception):
        service.status.side_effect = project
    else:
        service.status.return_value = project
    with mock.patch.object(server, "_active_asset_catalog_context", side_effect=RuntimeError(error)), \
            mock.patch.object(server, "_active_project_service", return_value=service):
        return json.loads(server.get_active_unpacked_asset_index_status())


def _assert_availability_not_a_route(result):
    assert "requiredAction" not in result
    assert "optionalIndexAction" not in result
    assert "next" not in result


@pytest.mark.parametrize("error", ["catalog missing", "no unpacked asset root in a stale catalog message"])
def test_available_configured_root_can_be_read_despite_arbitrary_index_error(tmp_path, error):
    result = _status({"assetRoot": str(tmp_path), "assetRootConfigured": True,
                      "assetRootAvailable": True}, error)
    assert result["assetRootConfigured"] is True
    assert result["directAssetReadsAvailable"] is True
    _assert_availability_not_a_route(result)
    assert result["reason"] == error
    assert not result["ready"] and not result["applied"]


def test_unconfigured_root_does_not_claim_direct_reads_for_database_error():
    result = _status({"assetRoot": None, "assetRootConfigured": False, "assetRootAvailable": False},
                     "database is locked")
    assert result["assetRootConfigured"] is False
    assert result["directAssetReadsAvailable"] is False
    _assert_availability_not_a_route(result)


@pytest.mark.parametrize("advertised_available", [False, True])
def test_missing_configured_directory_is_not_an_available_direct_route(tmp_path, advertised_available):
    result = _status({"assetRoot": str(tmp_path / "missing"), "assetRootConfigured": True,
                      "assetRootAvailable": advertised_available})
    assert result["assetRootConfigured"] is True
    assert result["directAssetReadsAvailable"] is False
    _assert_availability_not_a_route(result)


def test_configured_file_is_not_an_asset_directory(tmp_path):
    path = tmp_path / "not-a-directory"
    path.write_text("data")
    result = _status({"assetRoot": str(path), "assetRootConfigured": True, "assetRootAvailable": True})
    assert result["assetRootConfigured"] is True
    assert result["directAssetReadsAvailable"] is False


def test_project_configuration_failure_reports_unknown_not_invented_availability():
    result = _status(RuntimeError("active project changed"))
    assert result["assetRootConfigured"] is None
    assert result["directAssetReadsAvailable"] is False
    _assert_availability_not_a_route(result)
    assert result["configurationError"] == "active project changed"


def test_directory_check_error_does_not_reopen_direct_read_route(tmp_path):
    with mock.patch.object(Path, "is_dir", side_effect=PermissionError("directory access denied")):
        result = _status({"assetRoot": str(tmp_path), "assetRootConfigured": True, "assetRootAvailable": True})
    assert result["assetRootConfigured"] is True
    assert result["directAssetReadsAvailable"] is False
    assert "access denied" in result["configurationError"]


def test_redirected_directory_is_not_advertised_for_direct_reads(tmp_path):
    with mock.patch.object(Path, "is_symlink", return_value=True):
        result = _status({"assetRoot": str(tmp_path), "assetRootConfigured": True, "assetRootAvailable": True})
    assert result["directAssetReadsAvailable"] is False
