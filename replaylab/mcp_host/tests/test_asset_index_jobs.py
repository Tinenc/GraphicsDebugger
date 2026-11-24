"""Persistent on-demand asset indexing: job table, internal route contract, MCP fallback."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from src import asset_index_jobs
from src.asset_catalog import AssetCatalogError
from src.internal_tool_service import (
    ASSET_INDEX_OPERATION_SCHEMA,
    InternalToolService,
    parse_asset_index_operation,
)


class AssetIndexJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "assets"
        self.root.mkdir()
        for index in range(5):
            (self.root / ("t%d.txt" % index)).write_text("value %d" % index, encoding="utf-8")
        self.database = Path(self.tmp.name) / "managed" / "symbols.sqlite3"
        self.database.parent.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()
        asset_index_jobs._JOBS.clear()

    def test_ensure_runs_once_and_status_reports_completion(self) -> None:
        first = asset_index_jobs.ensure_index_job(self.database, self.root, "b1", owner="test")
        self.assertEqual(first["status"], "running")
        joined = asset_index_jobs.ensure_index_job(self.database, self.root, "b1", owner="test")
        self.assertEqual(joined["status"], "running")
        final = asset_index_jobs.wait_for_index_job(self.database, 30.0)
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["result"]["assetCount"], 5)
        self.assertTrue(final["result"]["complete"])
        self.assertEqual(final["owner"], "test")

    def test_names_only_job_never_builds_a_full_content_manifest(self) -> None:
        with mock.patch.object(
            asset_index_jobs.AssetCatalogService,
            "refresh_content_manifest",
            side_effect=AssertionError("names-only indexing must not hash the whole tree"),
        ):
            first = asset_index_jobs.ensure_index_job(
                self.database, self.root, "b1", depth="names", owner="test"
            )
            self.assertEqual(first["status"], "running")
            final = asset_index_jobs.wait_for_index_job(self.database, 30.0)
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["depth"], "names")
        self.assertEqual(
            final["result"]["contentManifest"],
            {
                "ready": False,
                "deferred": True,
                "reasonCode": "names-only-index",
            },
        )

    def test_storage_preparation_failure_is_synchronous_and_no_job_is_advertised(self) -> None:
        with mock.patch.object(
            asset_index_jobs.AssetCatalogService,
            "prepare_root",
            side_effect=AssetCatalogError("catalog database is locked"),
        ):
            with self.assertRaisesRegex(AssetCatalogError, "locked"):
                asset_index_jobs.ensure_index_job(
                    self.database, self.root, "b1", depth="names", owner="test"
                )
        self.assertIsNone(asset_index_jobs.index_job_status(self.database))

    def test_operation_contract_rejects_paths_and_unknown_fields(self) -> None:
        good = json.dumps({"schema": ASSET_INDEX_OPERATION_SCHEMA, "requestId": uuid.uuid4().hex,
                           "operation": "ensure", "depth": "headers"}).encode()
        self.assertEqual(parse_asset_index_operation(good)["operation"], "ensure")
        with self.assertRaises(ValueError):
            parse_asset_index_operation(json.dumps({"schema": ASSET_INDEX_OPERATION_SCHEMA, "requestId": uuid.uuid4().hex,
                                                    "operation": "ensure", "depth": "headers", "assetRoot": "C:/x"}).encode())
        with self.assertRaises(ValueError):
            parse_asset_index_operation(json.dumps({"schema": ASSET_INDEX_OPERATION_SCHEMA, "requestId": uuid.uuid4().hex,
                                                    "operation": "delete", "depth": "headers"}).encode())

    def test_internal_service_resolves_the_project_itself(self) -> None:
        service = InternalToolService(project=object(), restore_jobs=object(), shader_manager=object())
        status = {"scope": {"buildId": "b9"}}
        with mock.patch("src.server._active_asset_catalog_context",
                        return_value=(object(), status, self.root, self.database)) as context:
            code, value = service.asset_index_operation(json.dumps({
                "schema": ASSET_INDEX_OPERATION_SCHEMA, "requestId": uuid.uuid4().hex,
                "operation": "ensure", "depth": "headers"}).encode())
            self.assertEqual(code, 200)
            self.assertEqual(value["result"]["buildId"], "b9")
            self.assertIn(value["result"]["job"]["status"], {"running", "done"})
            context.assert_called_with(require_catalog=False, create_dictionary=True)
            asset_index_jobs.wait_for_index_job(self.database, 30.0)
            code, value = service.asset_index_operation(json.dumps({
                "schema": ASSET_INDEX_OPERATION_SCHEMA, "requestId": uuid.uuid4().hex,
                "operation": "status", "depth": "headers"}).encode())
            self.assertEqual(code, 200)
            self.assertEqual(value["result"]["job"]["status"], "done")
            self.assertTrue(value["result"]["catalog"]["complete"])

    def test_mcp_falls_back_to_a_local_job_without_the_internal_service(self) -> None:
        from src import asset_index_client, server

        with mock.patch.object(asset_index_client, "request",
                               side_effect=asset_index_client.InternalAssetIndexUnavailable("not running")):
            job = server._asset_index_job(self.database, self.root, "b2")
            self.assertEqual(job["owner"], "task-process")
            self.assertEqual(job["fallbackReason"], "not running")
            final = asset_index_jobs.wait_for_index_job(self.database, 30.0)
            self.assertEqual(final["status"], "done")
            self.assertEqual(server._asset_index_status(self.database)["status"], "done")


if __name__ == "__main__":
    unittest.main()
