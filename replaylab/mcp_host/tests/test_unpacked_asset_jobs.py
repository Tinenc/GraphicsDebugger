from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from src import unpacked_asset_jobs as jobs
from src.unpacked_asset_jobs import UnpackedAssetJobError, UnpackedAssetJobManager


OPERATION_ID = "a" * 32
CATALOG_SHA256 = "b" * 64
INVENTORY_EVIDENCE_SHA256 = "c" * 64
SPEC_ASSET_ID = "asset-v1-" + "d" * 64
INVENTORY_EVIDENCE_ASSET_ID = "asset-v1-" + "e" * 64


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, payload: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_bytes(payload)
    return path


def _write_inventory_database(
    path: Path,
    *,
    profile: str = "Demo",
    inventories: list[dict[str, object]] | None = None,
    assets: list[dict[str, object]] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = inventories or [
        {
            "id": 42,
            "confidence": "Strong",
            "truncated": False,
            "asset_count": 1,
            "relation_count": 0,
            "error_count": 0,
        }
    ]
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA user_version=2;
            CREATE TABLE inventories (
                id INTEGER PRIMARY KEY,
                profile TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                stored_at TEXT NOT NULL,
                analyzer_version TEXT NULL,
                metadata_json TEXT NULL,
                asset_count INTEGER NOT NULL,
                relation_count INTEGER NOT NULL,
                error_count INTEGER NOT NULL
            );
            CREATE TABLE assets (
                inventory_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                name TEXT NOT NULL,
                directory TEXT NOT NULL,
                class TEXT NOT NULL,
                kind TEXT NOT NULL,
                package_size INTEGER NOT NULL,
                estimated_memory_size INTEGER NULL,
                available INTEGER NOT NULL,
                error TEXT NULL,
                mesh_json TEXT NULL,
                texture_json TEXT NULL,
                material_json TEXT NULL,
                animation_json TEXT NULL,
                skeleton_json TEXT NULL,
                availability_json TEXT NULL,
                extra_json TEXT NULL,
                metrics_json TEXT NULL,
                PRIMARY KEY (inventory_id, path)
            );
            """
        )
        for row in rows:
            metadata = {
                "discoveryConfidence": row.get("confidence"),
                "truncated": str(row.get("truncated", False)).lower(),
            }
            connection.execute(
                """
                INSERT INTO inventories (
                    id, profile, generated_at, stored_at, analyzer_version,
                    metadata_json, asset_count, relation_count, error_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    row["id"],
                    profile,
                    "2026-08-29T01:02:03Z",
                    "2026-08-29T01:02:04Z",
                    "uex-test",
                    json.dumps(metadata),
                    row.get("asset_count", 0),
                    row.get("relation_count", 0),
                    row.get("error_count", 0),
                ),
            )
        for asset in assets or []:
            connection.execute(
                """
                INSERT INTO assets (
                    inventory_id, path, name, directory, class, kind,
                    package_size, estimated_memory_size, available, error,
                    mesh_json, material_json, metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    asset.get("inventory_id", 42),
                    asset["path"],
                    asset.get("name", "SK_Hero"),
                    asset.get("directory", "SilverPalace/Content/Hero"),
                    asset.get("class", "SkeletalMesh"),
                    asset.get("kind", "SkeletalMesh"),
                    asset.get("package_size", 1234),
                    asset.get("estimated_memory_size", 5678),
                    1 if asset.get("available", True) else 0,
                    asset.get("error"),
                    asset.get("mesh_json", "{\"huge\":\"secret-detail\"}"),
                    asset.get("material_json", "{\"huge\":\"secret-material\"}"),
                    json.dumps(asset.get("metrics", {"triangles.lod0": 12})),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _provider_environment(base: Path) -> dict[str, str]:
    providers = base / "providers"
    providers.mkdir(parents=True)
    analysis = providers / "analysis" / "Demo"
    _write_inventory_database(
        analysis / "inventory.db",
        inventories=[
            {
                "id": 42,
                "confidence": "Strong",
                "truncated": False,
                "asset_count": 2,
            }
        ],
        assets=[
            {"path": "Game/Hero/SK_Hero.SK_Hero"},
            {
                "path": "SilverPalace/Content/Hero/SK_Hero.SK_Hero",
                "directory": "SilverPalace/Content/Hero",
            },
        ],
    )
    profiles = _write(
        providers / "profiles.json",
        _json_bytes(
            {
                "profiles": {
                    "Demo": {
                        "game": "GAME_Demo",
                        "paksDir": "C:/private/game/paks",
                        "aesKey": "never-return-this-secret",
                        "analysisDir": str(analysis),
                    }
                }
            }
        ),
    )
    wrapper = _write(providers / "export_mesh_to_blender.py", "# fixed wrapper\n")
    _write(providers / "assemble_blender.py", "# fixed assembler\n")
    _write(providers / "validate_blend.py", "# fixed validator\n")
    addon = providers / "io_scene_ueformat"
    addon.mkdir()
    return {
        "PATH": "",
        jobs.UEX_ENV: sys.executable,
        jobs.UEX_PROFILES_ENV: str(profiles),
        jobs.BLENDER_ENV: sys.executable,
        jobs.UEX_BLENDER_WRAPPER_ENV: str(wrapper),
        jobs.UEFORMAT_ADDON_ENV: str(addon),
    }


def _valid_report_spec(name: str = "Generic Project Rules") -> dict[str, object]:
    return {
        "version": 1,
        "name": name,
        "characterPathRules": [
            {
                "name": "project-character-layout",
                "regex": (
                    r"^(?<category>[^/]+)/(?<character>[^/]+)/"
                    r"(?<variant>[^/]+)/(?<state>[^/]+)/(?<component>[^/]+)"
                ),
            }
        ],
        "scene": {"scopeGlobs": ["Project/Content/**"]},
    }


def _report_state(inventory_id: int = 42, profile: str = "Demo") -> dict[str, object]:
    return {
        "kind": "uex-asset-report",
        "request": {
            "profile": profile,
            "inventoryId": inventory_id,
            "inventoryConfidence": "Strong",
            "inventoryEvidenceAssetId": INVENTORY_EVIDENCE_ASSET_ID,
            "inventoryEvidenceSha256": INVENTORY_EVIDENCE_SHA256,
            "allowPartial": False,
            "specAssetId": SPEC_ASSET_ID,
            "catalogSha256": CATALOG_SHA256,
        },
    }


def _write_report_bundle(
    job_dir: Path,
    *,
    inventory_id: int = 42,
    manifest_inventory_id: int | None = None,
    report_inventory_id: int | None = None,
    html_inventory_id: int | None = None,
    report_profile: str = "Demo",
    manifest_profile: str = "Demo",
    manifest_schema_version: int = 1,
    manifest_kind: str = "uex-asset-distribution-report",
    report_schema_version: int = 1,
    artifact_mutator=None,
) -> dict[str, Path]:
    target = job_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    report_inventory = inventory_id if report_inventory_id is None else report_inventory_id
    html_inventory = inventory_id if html_inventory_id is None else html_inventory_id
    report = {
        "schemaVersion": report_schema_version,
        "profile": report_profile,
        "specName": "UnitTest",
        "inventoryId": report_inventory,
        "characterAssemblies": [],
        "sceneCategories": [],
        "warnings": [],
    }
    report_path = _write(target / "asset-report.json", _json_bytes(report))
    csv_path = _write(
        target / "asset-summary.csv",
        b"\xef\xbb\xbfRecordType,Category,AssemblyId\r\n",
    )
    html_path = _write(
        target / "asset-report.html",
        "<!doctype html><script>const report = "
        + json.dumps({"inventoryId": html_inventory, "profile": "Demo"})
        + ";</script>\n",
    )
    artifacts = []
    content_types = {
        report_path.name: "application/json; charset=utf-8",
        csv_path.name: "text/csv; charset=utf-8",
        html_path.name: "text/html; charset=utf-8",
    }
    for path in (report_path, csv_path, html_path):
        artifacts.append(
            {
                "fileName": path.name,
                "contentType": content_types[path.name],
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    if artifact_mutator is not None:
        artifact_mutator(artifacts)
    manifest = {
        "schemaVersion": manifest_schema_version,
        "kind": manifest_kind,
        "profile": manifest_profile,
        "specName": "UnitTest",
        "inventoryId": inventory_id if manifest_inventory_id is None else manifest_inventory_id,
        "reportSchemaVersion": report_schema_version,
        "artifacts": artifacts,
    }
    manifest_path = _write(
        target / "asset-report-manifest.json", _json_bytes(manifest)
    )
    return {
        "html": html_path,
        "json": report_path,
        "csv": csv_path,
        "manifest": manifest_path,
    }


def _blender_state(
    assets: list[str] | None = None,
    *,
    export_only: bool = False,
    prefer_uemodel: bool = True,
) -> dict[str, object]:
    canonical_assets = [
        jobs._normalize_export_asset_path(asset)
        for asset in (assets or ["/Game/Hero/SK_Hero_Body"])
    ]
    decision = {
        "inventoryId": 42,
        "profile": "Demo",
        "generatedAt": "2026-08-29T01:02:03Z",
        "storedAt": "2026-08-29T01:02:04Z",
        "assetCount": len(canonical_assets),
        "relationCount": 0,
        "errorCount": 0,
        "discoveryConfidence": "Strong",
        "strong": True,
        "committed": True,
        "complete": True,
        "truncated": False,
        "reportEligible": True,
        "status": "complete",
        "metadataRecognized": True,
    }
    decision["decisionSha256"] = hashlib.sha256(
        json.dumps(
            decision,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    inventory_assets = [
        jobs._inventory_asset_identity_receipt(
            inventory_path=asset + "." + asset.rsplit("/", 1)[-1],
            export_asset_path=asset,
            asset_class="SkeletalMesh",
            kind="SkeletalMesh",
        )
        for asset in canonical_assets
    ]
    return {
        "kind": "uex-blender-export",
        "request": {
            "profile": "Demo",
            "inventoryId": 42,
            "inventoryDecisionSha256": decision["decisionSha256"],
            "inventoryDecision": decision,
            "inventoryAssets": inventory_assets,
            "virtualAssets": canonical_assets,
            "lod0Only": False,
            "exportMorphTargets": True,
            "preferUeModel": prefer_uemodel,
            "exportOnly": export_only,
        },
    }


def _write_blender_bundle(
    job_dir: Path,
    state: dict[str, object],
    *,
    assembly_mutator=None,
    bundle_mutator=None,
    validation_mutator=None,
) -> dict[str, object]:
    request = state["request"]
    assert isinstance(request, dict)
    assets = list(request["virtualAssets"])
    export_only = bool(request["exportOnly"])
    prefer_uemodel = bool(request["preferUeModel"])
    target = job_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    blend = target / "UEX_Assembly.blend"
    assembly_path = blend.with_suffix(".assembly.json")
    export_root = target / "UEX_Assembly_uex_export"
    bundle_records: list[dict[str, object]] = []
    bundle_manifests: list[Path] = []
    for index, asset in enumerate(assets, start=1):
        safe_name = asset.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        safe_name = safe_name.split(".", 1)[0]
        bundle_dir = export_root / "bundles" / f"{index:02d}_{safe_name}"
        mesh_path = _write(bundle_dir / "meshes" / "lod0.glb", b"glTF-lod0")
        ue_model_path = _write(bundle_dir / "models" / "mesh.uemodel", b"uemodel")
        material_path = _write(bundle_dir / "materials" / "00_body.json", "{}\n")
        texture_path = _write(bundle_dir / "textures" / "base.png", b"png")
        _write(bundle_dir / "import_to_blender.py", "# bundle importer\n")
        bundle = {
            "schema": "uex.blender-bundle.v1",
            "sourceAsset": asset,
            "meshName": f"Part{index}",
            "assetType": "USkeletalMesh",
            "skeleton": "/Game/Hero/SKEL_Hero",
            "boneCount": 32,
            "sourceLodCount": 1,
            "morphTargetCount": 2,
            "meshes": [
                {
                    "lodIndex": 0,
                    "relativePath": mesh_path.relative_to(bundle_dir).as_posix(),
                    "bytes": mesh_path.stat().st_size,
                    "format": "glb",
                }
            ],
            "ueModel": ue_model_path.relative_to(bundle_dir).as_posix(),
            "materials": [
                {
                    "slotIndex": 0,
                    "slotName": "Body",
                    "materialName": "MI_Body",
                    "materialPath": "/Game/Hero/MI_Body",
                    "materialJson": material_path.relative_to(bundle_dir).as_posix(),
                    "rawMaterialJson": None,
                    "blendMode": "Opaque",
                    "shadingModel": "DefaultLit",
                    "baseColor": {
                        "relativePath": texture_path.relative_to(bundle_dir).as_posix(),
                        "confidence": "high",
                    },
                    "directTextureParameters": [],
                    "textureParameters": [],
                    "scalars": {},
                    "switches": {},
                    "colors": {},
                    "warnings": [],
                }
            ],
            "textures": [
                {
                    "textureName": "T_Base",
                    "sourcePath": "/Game/Hero/T_Base",
                    "relativePath": texture_path.relative_to(bundle_dir).as_posix(),
                    "importedWidth": 4,
                    "importedHeight": 4,
                    "decodedWidth": 4,
                    "decodedHeight": 4,
                    "firstAvailableMip": 0,
                    "mipBias": 0,
                    "error": None,
                }
            ],
            "warnings": [],
        }
        if bundle_mutator is not None:
            bundle_mutator(bundle, index, bundle_dir)
        manifest_path = _write(bundle_dir / "manifest.json", _json_bytes(bundle))
        bundle_manifests.append(manifest_path)
        bundle_records.append(
            {
                "asset": asset,
                "directory": str(bundle_dir.resolve()),
                "manifest": str(manifest_path.resolve()),
                "meshName": f"Part{index}",
                "sourceLodCount": 1,
                "exportedLodCount": 1,
                "boneCount": 32,
                "morphTargetCount": 2,
                "materialCount": 1,
                "boundBaseColorCount": 1,
                "textureCount": 1,
                "warningCount": 0,
                "hasUeModel": True,
            }
        )

    validation_path = blend.with_suffix(".validation.json")
    validation: dict[str, object] | None = None
    if not export_only:
        _write(blend, b"BLENDER")
        validation = {
            "schema": "uex.blender-validation.v1",
            "ok": True,
            "blendFile": str(blend.resolve()),
            "blenderVersion": "4.5.0",
            "checks": {
                "meshCount": len(assets),
                "armatureObjectCount": 1,
                "referencedArmatureCount": 1,
                "sharedArmature": "Hero_Rig",
                "boneCount": 32,
                "lodCollections": {"0": ["Hero_LOD0"]},
                "imageCount": 1,
                "missingImages": [],
                "meshes": [
                    {
                        "name": f"Part{index}",
                        "vertices": 3,
                        "vertexGroups": 2,
                        "weightedVertices": 3,
                        "armatureModifiers": 1,
                        "shapeKeys": 2,
                    }
                    for index in range(1, len(assets) + 1)
                ],
            },
            "errors": [],
            "warnings": [],
        }
        if validation_mutator is not None:
            validation_mutator(validation)
        _write(validation_path, _json_bytes(validation))

    assembly: dict[str, object] = {
        "schema": "uex.blender-assembly.v1",
        "status": "exported" if export_only else "complete",
        "profile": request["profile"],
        "sourceAssets": assets,
        "bundles": bundle_records,
        "geometrySource": (
            "not-assembled" if export_only else ("uemodel" if prefer_uemodel else "glb")
        ),
        "outputBlend": None if export_only else str(blend.resolve()),
        "outputSha256": None if export_only else _sha256(blend),
        "validationReport": None if export_only else str(validation_path.resolve()),
        "validationOk": None if export_only else True,
        "assemblyManifest": str(assembly_path.resolve()),
        "options": {
            "lod0Only": request["lod0Only"],
            "exportMorphTargets": request["exportMorphTargets"],
            "overwrite": False,
            "exportOnly": request["exportOnly"],
            "preferUeModel": request["preferUeModel"],
        },
    }
    if assembly_mutator is not None:
        assembly_mutator(assembly)
    _write(assembly_path, _json_bytes(assembly))
    return {
        "target": target,
        "blend": blend,
        "assembly": assembly_path,
        "validation": validation_path,
        "bundleManifests": bundle_manifests,
    }


def _write_partial_parent(
    manager: UnpackedAssetJobManager,
    root: Path,
    *,
    assets: list[str],
    missing_indices: tuple[int, ...],
    operation_id: str = OPERATION_ID,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    profile, decision, inventory_assets = manager._resolve_blender_inventory_selection(
        profile="Demo",
        inventory_id=42,
        expected_decision_sha256=manager.resolve_inventory_decision("Demo", 42)[
            "decisionSha256"
        ],
        assets=tuple(assets),
    )
    request: dict[str, object] = {
        "profile": profile,
        "inventoryId": 42,
        "inventoryDecisionSha256": decision["decisionSha256"],
        "inventoryDecision": decision,
        "inventoryAssets": inventory_assets,
        "virtualAssets": [item["exportAssetPath"] for item in inventory_assets],
        "lod0Only": False,
        "exportMorphTargets": True,
        "preferUeModel": False,
        "exportOnly": False,
    }
    job_dir = root / "jobs" / operation_id
    paths = _write_blender_bundle(job_dir, {"request": request})
    for index in missing_indices:
        shutil.rmtree(paths["bundleManifests"][index].parent)
    _write(job_dir / "stdout.log", "")
    _write(job_dir / "stderr.log", "synthetic provider failure\n")
    state: dict[str, object] = {
        "schema": jobs.JOB_SCHEMA,
        "schemaVersion": jobs.JOB_VERSION,
        "operationId": operation_id,
        "kind": "uex-blender-export",
        "requestSha256": hashlib.sha256(jobs._canonical_bytes(request)).hexdigest(),
        "request": request,
        "status": "running",
        "pid": 1234,
        "exitCode": None,
        "result": None,
        "error": "",
        "setupComplete": True,
        "createdUnixMs": 1,
        "updatedUnixMs": 1,
    }
    finished = manager._finish(operation_id, state, job_dir, 7)
    public = manager._public_state(finished, job_dir)
    return finished, public, paths


class UnpackedAssetJobProviderTests(unittest.TestCase):
    def test_unconfigured_providers_are_reported_and_jobs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = UnpackedAssetJobManager(Path(temporary) / "jobs", {"PATH": ""})
            capabilities = manager.capabilities()
            self.assertFalse(capabilities["routes"]["uexDecisionDiscovery"]["available"])
            self.assertFalse(capabilities["routes"]["assetReport"]["available"])
            self.assertFalse(capabilities["routes"]["blenderBundleOnly"]["available"])
            self.assertFalse(capabilities["routes"]["blenderAssembly"]["available"])
            self.assertFalse(
                capabilities["routes"]["blenderContinuation"]["available"]
            )
            self.assertTrue(
                capabilities["routes"]["blenderBundleOnly"][
                    "inventoryDecisionRequired"
                ]
            )
            self.assertTrue(
                capabilities["routes"]["blenderAssembly"][
                    "exactAssetMembershipRequired"
                ]
            )
            self.assertFalse(capabilities["providers"]["ueformatAddon"]["available"])
            self.assertEqual(capabilities["providers"]["ueformatAddon"]["sha256"], "")
            self.assertFalse(capabilities["policy"]["callerExecutablePathsAccepted"])
            self.assertFalse(capabilities["policy"]["callerOutputPathsAccepted"])
            self.assertFalse(capabilities["policy"]["shellExecution"])
            self.assertTrue(
                capabilities["reportSpecification"]["inlineManagedStagingAvailable"]
            )
            self.assertEqual(
                capabilities["reportSpecification"]["missingRulesStatus"], "yield"
            )
            decisions = manager.discover_uex_decisions()
            self.assertFalse(decisions["available"])
            self.assertEqual(decisions["status"], "unavailable")
            self.assertEqual(decisions["reasonCode"], "uex-profiles-unavailable")
            with self.assertRaises(UnpackedAssetJobError):
                manager.start_report(
                    operation_id=OPERATION_ID,
                    profile="Demo",
                    inventory_id=42,
                    inventory_evidence_asset_id=INVENTORY_EVIDENCE_ASSET_ID,
                    inventory_evidence_sha256=INVENTORY_EVIDENCE_SHA256,
                    spec_path=Path(temporary) / "spec.json",
                    spec_asset_id=SPEC_ASSET_ID,
                    catalog_sha256=CATALOG_SHA256,
                    allow_partial=False,
                )
            with self.assertRaises(UnpackedAssetJobError):
                manager.start_blender_export(
                    operation_id=OPERATION_ID,
                    profile="Demo",
                    inventory_id=42,
                    expected_inventory_decision_sha256="0" * 64,
                    virtual_assets=["/Game/Hero/SK_Hero"],
                    lod0_only=False,
                    export_morph_targets=True,
                    prefer_uemodel=False,
                    export_only=True,
                )

    def test_host_owned_inventory_decisions_are_quality_aware_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = _provider_environment(base)
            database = base / "providers" / "analysis" / "Demo" / "inventory.db"
            database.unlink()
            _write_inventory_database(
                database,
                inventories=[
                    {"id": 42, "confidence": "Strong", "truncated": False},
                    {"id": 43, "confidence": "Strong", "truncated": True},
                    {"id": 44, "confidence": "Moderate", "truncated": False},
                ],
                assets=[
                    {
                        "inventory_id": 42,
                        "path": "SilverPalace/Content/Hero/SK_Hero.SK_Hero",
                        "class": "SkeletalMesh",
                        "kind": "SkeletalMesh",
                        "metrics": {"triangles.lod0": 321, "vertices.lod0": 654},
                    }
                ],
            )
            manager = UnpackedAssetJobManager(base / "jobs", environment)

            decisions = manager.discover_uex_decisions(inventory_limit=2)
            self.assertTrue(decisions["available"])
            self.assertEqual(decisions["status"], "partial")
            self.assertTrue(decisions["coverage"]["inventoryEnumerationTruncated"])
            self.assertEqual([item["profile"] for item in decisions["profiles"]], ["Demo"])
            profile = decisions["profiles"][0]
            self.assertTrue(profile["truncated"])
            self.assertEqual(profile["inventoryCount"], 3)
            by_id = {item["inventoryId"]: item for item in profile["inventories"]}
            self.assertFalse(by_id[44]["strong"])
            self.assertFalse(by_id[44]["complete"])
            self.assertTrue(by_id[43]["strong"])
            self.assertTrue(by_id[43]["truncated"])
            self.assertFalse(by_id[43]["reportEligible"])
            complete = manager.resolve_inventory_decision("demo", 42)
            self.assertTrue(complete["complete"])
            self.assertFalse(complete["truncated"])
            self.assertRegex(complete["decisionSha256"], r"^[0-9a-f]{64}$")

            serialized = json.dumps(decisions)
            self.assertNotIn("never-return-this-secret", serialized)
            self.assertNotIn("private/game", serialized)
            self.assertNotIn("analysisDir", serialized)
            self.assertNotIn(str(database), serialized)

            query = manager.query_uex_inventory_assets(
                profile="Demo",
                inventory_id=42,
                kind="SkeletalMesh",
                limit=10,
            )
            self.assertEqual(query["status"], "ready")
            self.assertEqual(query["returned"], 1)
            item = query["items"][0]
            self.assertEqual(item["assetClass"], "SkeletalMesh")
            self.assertEqual(
                item["exportAssetPath"],
                "SilverPalace/Content/Hero/SK_Hero",
            )
            self.assertEqual(item["metrics"]["triangles.lod0"], 321.0)
            query_serialized = json.dumps(query)
            self.assertNotIn("secret-detail", query_serialized)
            self.assertNotIn("meshJson", query_serialized)
            self.assertNotIn("materialJson", query_serialized)

    def test_unsupported_inventory_schema_fails_closed_without_exposing_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = _provider_environment(base)
            database = base / "providers" / "analysis" / "Demo" / "inventory.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA user_version=1;")
                connection.commit()
            finally:
                connection.close()
            manager = UnpackedAssetJobManager(base / "jobs", environment)
            result = manager.discover_uex_decisions()
            self.assertTrue(result["available"])
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(
                result["profiles"][0]["reasonCode"], "inventory-store-unavailable"
            )
            self.assertNotIn(str(database), json.dumps(result))

    def test_inaccessible_path_alias_is_reported_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            inaccessible_alias = base / "WindowsApps" / "python.exe"
            manager = UnpackedAssetJobManager(base / "jobs", {"PATH": str(base)})
            with mock.patch.object(
                jobs.shutil, "which", return_value=str(inaccessible_alias)
            ), mock.patch.object(jobs.sys, "frozen", True, create=True):
                capabilities = manager.capabilities()
            self.assertFalse(capabilities["providers"]["uex"]["available"])
            self.assertFalse(capabilities["providers"]["blender"]["available"])
            self.assertFalse(capabilities["providers"]["uexPython"]["available"])
            self.assertFalse(capabilities["routes"]["assetReport"]["available"])

    def test_no_caller_provider_or_output_path_parameters_exist(self) -> None:
        report_parameters = inspect.signature(UnpackedAssetJobManager.start_report).parameters
        blender_parameters = inspect.signature(
            UnpackedAssetJobManager.start_blender_export
        ).parameters
        forbidden = {
            "uex",
            "uex_path",
            "profiles",
            "profiles_path",
            "wrapper",
            "blender",
            "blender_path",
            "ueformat_addon",
            "output",
            "output_path",
            "output_directory",
        }
        self.assertTrue(forbidden.isdisjoint(report_parameters))
        self.assertTrue(forbidden.isdisjoint(blender_parameters))

    def test_frozen_host_does_not_misreport_itself_as_wrapper_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = _provider_environment(base)
            with mock.patch.object(jobs.sys, "frozen", True, create=True):
                manager = UnpackedAssetJobManager(base / "jobs", environment)
                capabilities = manager.capabilities()
            self.assertFalse(capabilities["providers"]["uexPython"]["available"])
            self.assertFalse(capabilities["routes"]["blenderBundleOnly"]["available"])
            self.assertFalse(capabilities["routes"]["blenderAssembly"]["available"])

    def test_frozen_host_uses_hash_bound_bundled_continuation_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = _provider_environment(base)
            bundle_root = base / "pyinstaller-extraction"
            helper = _write(
                bundle_root.joinpath(*jobs._FROZEN_CONTINUATION_DRIVER_RELATIVE),
                "# fixed packaged continuation helper\n",
            )
            with mock.patch.object(
                jobs.sys, "frozen", True, create=True
            ), mock.patch.object(
                jobs.sys, "_MEIPASS", str(bundle_root), create=True
            ):
                manager = UnpackedAssetJobManager(base / "jobs", environment)
                paths = manager._provider_paths()
                capabilities = manager.capabilities()
            self.assertEqual(
                paths["blenderContinuationDriver"], helper.resolve(strict=True)
            )
            self.assertEqual(
                capabilities["providers"]["blenderContinuationDriver"],
                {
                    "available": True,
                    "sha256": _sha256(helper),
                    "name": "unpacked_asset_jobs.py",
                },
            )

    def test_frozen_host_prefers_explicit_continuation_driver(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = _provider_environment(base)
            configured = _write(base / "owner" / "continuation.py", "# owner\n")
            environment[jobs.UEX_BLENDER_CONTINUATION_DRIVER_ENV] = str(configured)
            bundle_root = base / "pyinstaller-extraction"
            _write(
                bundle_root.joinpath(*jobs._FROZEN_CONTINUATION_DRIVER_RELATIVE),
                "# bundled\n",
            )
            with mock.patch.object(
                jobs.sys, "frozen", True, create=True
            ), mock.patch.object(
                jobs.sys, "_MEIPASS", str(bundle_root), create=True
            ):
                manager = UnpackedAssetJobManager(base / "jobs", environment)
                driver = manager._provider_paths()["blenderContinuationDriver"]
            self.assertEqual(driver, configured.resolve(strict=True))

    def test_frozen_host_rejects_redirected_continuation_helper(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks are unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = _provider_environment(base)
            bundle_root = base / "pyinstaller-extraction"
            helper = bundle_root.joinpath(
                *jobs._FROZEN_CONTINUATION_DRIVER_RELATIVE
            )
            helper.parent.mkdir(parents=True)
            outside = _write(base / "outside-driver.py", "# outside\n")
            try:
                os.symlink(outside, helper)
            except OSError:
                self.skipTest("test process cannot create symbolic links")
            with mock.patch.object(
                jobs.sys, "frozen", True, create=True
            ), mock.patch.object(
                jobs.sys, "_MEIPASS", str(bundle_root), create=True
            ):
                manager = UnpackedAssetJobManager(base / "jobs", environment)
                driver = manager._provider_paths()["blenderContinuationDriver"]
            self.assertIsNone(driver)

    def test_invalid_explicit_driver_does_not_fall_back_to_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = _provider_environment(base)
            environment[jobs.UEX_BLENDER_CONTINUATION_DRIVER_ENV] = str(
                base / "owner" / "missing.py"
            )
            bundle_root = base / "pyinstaller-extraction"
            _write(
                bundle_root.joinpath(*jobs._FROZEN_CONTINUATION_DRIVER_RELATIVE),
                "# bundled\n",
            )
            with mock.patch.object(
                jobs.sys, "frozen", True, create=True
            ), mock.patch.object(
                jobs.sys, "_MEIPASS", str(bundle_root), create=True
            ):
                manager = UnpackedAssetJobManager(base / "jobs", environment)
                driver = manager._provider_paths()["blenderContinuationDriver"]
            self.assertIsNone(driver)

    def test_frozen_host_never_falls_back_to_imported_module_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = _provider_environment(base)
            executable_source = _write(base / "imported-module.py", "# module\n")
            with mock.patch.object(
                jobs.sys, "frozen", True, create=True
            ), mock.patch.object(
                jobs.sys, "_MEIPASS", str(base / "missing-extraction"), create=True
            ), mock.patch.object(jobs, "__file__", str(executable_source)):
                manager = UnpackedAssetJobManager(base / "jobs", environment)
                driver = manager._provider_paths()["blenderContinuationDriver"]
            self.assertIsNone(driver)

    def test_missing_continuation_driver_is_reported_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = _provider_environment(base)
            with mock.patch.object(
                jobs, "__file__", str(base / "missing-continuation-driver.py")
            ):
                manager = UnpackedAssetJobManager(base / "jobs", environment)
                capabilities = manager.capabilities()
            self.assertFalse(
                capabilities["providers"]["blenderContinuationDriver"][
                    "available"
                ]
            )
            self.assertFalse(
                capabilities["routes"]["blenderContinuation"]["available"]
            )

    def test_relative_job_root_and_redirected_provider_are_rejected(self) -> None:
        with self.assertRaises(UnpackedAssetJobError):
            UnpackedAssetJobManager.from_environment(
                {jobs.JOB_ROOT_ENV: "relative/jobs", "PATH": ""}
            )
        if not hasattr(os, "symlink"):
            return
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            provider = _write(base / "provider.exe", b"provider")
            redirected = base / "redirected.exe"
            try:
                os.symlink(provider, redirected)
            except OSError:
                self.skipTest("test process cannot create symbolic links")
            manager = UnpackedAssetJobManager(
                base / "jobs", {"PATH": "", jobs.UEX_ENV: str(redirected)}
            )
            self.assertFalse(manager.capabilities()["providers"]["uex"]["available"])


class UnpackedAssetJobStartTests(unittest.TestCase):
    def _start_report(self, manager: UnpackedAssetJobManager, spec: Path, **updates):
        values = {
            "operation_id": OPERATION_ID,
            "profile": "Demo",
            "inventory_id": 42,
            "inventory_evidence_asset_id": INVENTORY_EVIDENCE_ASSET_ID,
            "inventory_evidence_sha256": INVENTORY_EVIDENCE_SHA256,
            "spec_path": spec,
            "spec_asset_id": SPEC_ASSET_ID,
            "catalog_sha256": CATALOG_SHA256,
            "allow_partial": False,
        }
        values.update(updates)
        return manager.start_report(**values)

    def test_report_operation_is_idempotent_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = _provider_environment(base)
            manager = UnpackedAssetJobManager(base / "jobs", environment)
            spec = _write(base / "spec.json", _json_bytes(_valid_report_spec()))
            process = mock.Mock(pid=1234)
            process.poll.return_value = None
            with mock.patch.object(jobs.subprocess, "Popen", return_value=process) as popen:
                first = self._start_report(manager, spec)
                second = self._start_report(manager, spec)
                self.assertEqual(first["operationId"], OPERATION_ID)
                self.assertEqual(second["operationId"], OPERATION_ID)
                self.assertEqual(popen.call_count, 1)
                state = json.loads(
                    (base / "jobs" / "jobs" / OPERATION_ID / "job.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    state["request"]["inventoryEvidenceAssetId"],
                    INVENTORY_EVIDENCE_ASSET_ID,
                )
                self.assertEqual(
                    state["request"]["inventoryEvidenceSha256"],
                    INVENTORY_EVIDENCE_SHA256,
                )
                self.assertEqual(state["request"]["inventoryConfidence"], "Strong")
                self.assertRegex(
                    state["request"]["inventoryDecisionSha256"], r"^[0-9a-f]{64}$"
                )
                self.assertFalse(state["request"]["allowPartial"])
                self.assertEqual(state["request"]["specSource"], "catalog-managed-v1")
                self.assertEqual(
                    state["request"]["stagedInputs"][0]["relativePath"],
                    "input/report-spec.json",
                )
                staged = base / "jobs" / "jobs" / OPERATION_ID / "input" / "report-spec.json"
                self.assertEqual(
                    json.loads(staged.read_text(encoding="utf-8")),
                    jobs.normalize_asset_report_spec(_valid_report_spec()),
                )
                command = popen.call_args.args[0]
                self.assertIsInstance(command, list)
                self.assertEqual(command.count("asset-report"), 1)
                self.assertNotIn("--allow-partial", command)
                self.assertIn(str(staged), command)
                self.assertNotIn(str(spec.resolve()), command)
                self.assertFalse(popen.call_args.kwargs["shell"])
                with self.assertRaises(UnpackedAssetJobError):
                    self._start_report(manager, spec, inventory_id=43)
            running = manager._running.pop(OPERATION_ID)
            running.stdout.close()
            running.stderr.close()

    def test_report_rejects_non_strong_or_unbound_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "jobs", _provider_environment(base))
            spec = _write(base / "spec.json", _json_bytes(_valid_report_spec()))
            invalid = (
                {"operation_id": "A" * 32},
                {"inventory_id": 0},
                {"inventory_id": True},
                {"inventory_id": 43},
                {"inventory_evidence_asset_id": ""},
                {"inventory_evidence_asset_id": "forged"},
                {"inventory_evidence_sha256": "not-a-hash"},
                {"spec_asset_id": "forged"},
                {"catalog_sha256": "not-a-hash"},
                {"profile": "bad\nprofile"},
                {"allow_partial": True},
                {"allow_partial": "false"},
            )
            process = mock.Mock(pid=1234)
            process.poll.return_value = None
            accepted: list[dict[str, object]] = []
            with mock.patch.object(jobs.subprocess, "Popen", return_value=process):
                try:
                    for index, update in enumerate(invalid, start=1):
                        isolated = {"operation_id": f"{index:032x}"}
                        isolated.update(update)
                        try:
                            self._start_report(manager, spec, **isolated)
                        except (UnpackedAssetJobError, TypeError):
                            pass
                        else:
                            accepted.append(update)
                finally:
                    for operation_id, running in list(manager._running.items()):
                        running.stdout.close()
                        running.stderr.close()
                        manager._running.pop(operation_id, None)
            self.assertEqual(accepted, [], f"invalid report inputs accepted: {accepted!r}")

    def test_inline_report_spec_is_canonical_staged_hash_bound_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "jobs", _provider_environment(base))
            process = mock.Mock(pid=1234)
            process.poll.return_value = None
            spec = _valid_report_spec()
            with mock.patch.object(jobs.subprocess, "Popen", return_value=process) as popen:
                first = manager.start_report(
                    operation_id=OPERATION_ID,
                    profile="demo",
                    inventory_id=42,
                    report_spec=spec,
                    project_binding_sha256="1" * 64,
                )
                second = manager.start_report(
                    operation_id=OPERATION_ID,
                    profile="Demo",
                    inventory_id=42,
                    report_spec=json.loads(json.dumps(spec)),
                    project_binding_sha256="1" * 64,
                )
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(first["requestSha256"], second["requestSha256"])
                self.assertEqual(first["request"]["specSource"], "inline-managed-v1")
                self.assertEqual(first["request"]["inventoryEvidenceAssetId"], "")
                staged = base / "jobs" / "jobs" / OPERATION_ID / "input" / "report-spec.json"
                canonical = jobs.normalize_asset_report_spec(spec)
                self.assertEqual(staged.read_bytes(), jobs._canonical_bytes(canonical))
                self.assertEqual(
                    first["request"]["specSha256"], hashlib.sha256(staged.read_bytes()).hexdigest()
                )
                self.assertEqual(first["request"]["specBytes"], staged.stat().st_size)
                self.assertIn(str(staged), popen.call_args.args[0])

                changed = _valid_report_spec("Different Project Rules")
                with self.assertRaises(UnpackedAssetJobError):
                    manager.start_report(
                        operation_id=OPERATION_ID,
                        profile="Demo",
                        inventory_id=42,
                        report_spec=changed,
                        project_binding_sha256="1" * 64,
                    )
                staged.write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(UnpackedAssetJobError, "staged input"):
                    manager.start_report(
                        operation_id=OPERATION_ID,
                        profile="Demo",
                        inventory_id=42,
                        report_spec=spec,
                        project_binding_sha256="1" * 64,
                    )
            running = manager._running.pop(OPERATION_ID)
            running.stdout.close()
            running.stderr.close()

    def test_inline_report_spec_missing_project_rules_yields_before_job_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "jobs", _provider_environment(base))
            invalid_specs = (
                {},
                {"version": 1, "name": "No character rules", "characterPathRules": [], "scene": {"scopeGlobs": ["Project/**"]}},
                {"version": 1, "name": "No scene rules", "characterPathRules": _valid_report_spec()["characterPathRules"], "scene": {"scopeGlobs": []}},
                {**_valid_report_spec(), "unexpected": True},
                {**_valid_report_spec(), "scene": {"scopeGlobs": ["Project/**"], "percentile": float("nan")}},
            )
            for index, spec in enumerate(invalid_specs, start=1):
                operation_id = f"{index:032x}"
                with self.subTest(index=index), self.assertRaises(UnpackedAssetJobError):
                    manager.start_report(
                        operation_id=operation_id,
                        profile="Demo",
                        inventory_id=42,
                        report_spec=spec,
                    )
                self.assertFalse((base / "jobs" / "jobs" / operation_id).exists())

            with self.assertRaisesRegex(UnpackedAssetJobError, "yield"):
                manager.start_report(
                    operation_id="f" * 32,
                    profile="Demo",
                    inventory_id=42,
                )
            self.assertFalse((base / "jobs" / "jobs" / ("f" * 32)).exists())

    def test_blender_rejects_broad_or_invalid_asset_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "jobs", _provider_environment(base))
            decision = manager.resolve_inventory_decision("Demo", 42)
            base_values = {
                "operation_id": OPERATION_ID,
                "profile": "Demo",
                "inventory_id": 42,
                "expected_inventory_decision_sha256": decision["decisionSha256"],
                "virtual_assets": ["/Game/Hero/SK_Hero"],
                "lod0_only": False,
                "export_morph_targets": True,
                "prefer_uemodel": True,
                "export_only": False,
            }
            invalid = (
                {"operation_id": "short"},
                {"profile": "bad\rprofile"},
                {"inventory_id": 0},
                {"inventory_id": True},
                {"expected_inventory_decision_sha256": "0" * 64},
                {"expected_inventory_decision_sha256": "not-a-hash"},
                {"virtual_assets": []},
                {"virtual_assets": ["/Game/Hero/../Secret/SK_Secret"]},
                {"virtual_assets": ["C:/Game/Hero/SK_Hero"]},
                {"virtual_assets": ["Game/Hero/SK_Hero.OtherObject"]},
                {"virtual_assets": ["/Game/Hero/SK", "/game/hero/sk"]},
                {"virtual_assets": [f"/Game/Hero/SK_{index}" for index in range(65)]},
                {"lod0_only": 0},
                {"export_morph_targets": "yes"},
                {"prefer_uemodel": 1},
                {"export_only": None},
            )
            for update in invalid:
                values = dict(base_values)
                values.update(update)
                with self.subTest(update=update), self.assertRaises(UnpackedAssetJobError):
                    manager.start_blender_export(**values)
            self.assertFalse((base / "jobs" / "jobs" / OPERATION_ID).exists())

    def test_blender_normalizes_real_inventory_object_path_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "jobs", _provider_environment(base))
            decision = manager.resolve_inventory_decision("Demo", 42)
            process = mock.Mock(pid=1234)
            process.poll.return_value = None
            with mock.patch.object(jobs.subprocess, "Popen", return_value=process) as popen:
                result = manager.start_blender_export(
                    operation_id=OPERATION_ID,
                    profile="Demo",
                    inventory_id=42,
                    expected_inventory_decision_sha256=decision[
                        "decisionSha256"
                    ],
                    virtual_assets=[
                        "SilverPalace/Content/Hero/SK_Hero.SK_Hero"
                    ],
                    lod0_only=False,
                    export_morph_targets=True,
                    prefer_uemodel=False,
                    export_only=True,
                )
            self.assertEqual(
                result["request"]["virtualAssets"],
                ["SilverPalace/Content/Hero/SK_Hero"],
            )
            self.assertEqual(result["request"]["inventoryId"], 42)
            self.assertEqual(
                result["request"]["inventoryDecisionSha256"],
                decision["decisionSha256"],
            )
            self.assertEqual(
                result["request"]["inventoryAssets"][0]["inventoryPath"],
                "SilverPalace/Content/Hero/SK_Hero.SK_Hero",
            )
            command = popen.call_args.args[0]
            self.assertIn("SilverPalace/Content/Hero/SK_Hero", command)
            self.assertNotIn("SilverPalace/Content/Hero/SK_Hero.SK_Hero", command)
            running = manager._running.pop(OPERATION_ID)
            running.stdout.close()
            running.stderr.close()

    def test_blender_requires_exact_available_skeletalmesh_inventory_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = _provider_environment(base)
            database = base / "providers" / "analysis" / "Demo" / "inventory.db"
            database.unlink()
            _write_inventory_database(
                database,
                inventories=[
                    {
                        "id": 42,
                        "confidence": "Strong",
                        "truncated": False,
                        "asset_count": 8,
                    }
                ],
                assets=[
                    {"path": "Project/Hero/SK_Valid.SK_Valid"},
                    {
                        "path": "Project/Hero/SM_WrongKind.SM_WrongKind",
                        "class": "StaticMesh",
                        "kind": "StaticMesh",
                    },
                    {
                        "path": "Project/Hero/SK_WrongClass.SK_WrongClass",
                        "class": "StaticMesh",
                        "kind": "SkeletalMesh",
                    },
                    {
                        "path": "Project/Hero/SK_Unavailable.SK_Unavailable",
                        "available": False,
                    },
                    {
                        "path": "Project/Hero/SK_Error.SK_Error",
                        "error": "provider failed",
                    },
                    {"path": "Project/Hero/SK_Ambiguous"},
                    {"path": "Project/Hero/SK_Ambiguous.SK_Ambiguous"},
                ],
            )
            manager = UnpackedAssetJobManager(base / "jobs", environment)
            decision = manager.resolve_inventory_decision("Demo", 42)
            base_values = {
                "profile": "Demo",
                "inventory_id": 42,
                "expected_inventory_decision_sha256": decision["decisionSha256"],
                "lod0_only": False,
                "export_morph_targets": True,
                "prefer_uemodel": False,
                "export_only": True,
            }
            rejected = {
                "missing": "Project/Hero/SK_Missing",
                "wrong-kind": "Project/Hero/SM_WrongKind",
                "wrong-class": "Project/Hero/SK_WrongClass",
                "unavailable": "Project/Hero/SK_Unavailable",
                "provider-error": "Project/Hero/SK_Error",
                "ambiguous": "Project/Hero/SK_Ambiguous",
            }
            for index, (label, asset) in enumerate(rejected.items(), start=1):
                operation_id = f"{index:032x}"
                with self.subTest(label=label), self.assertRaises(
                    UnpackedAssetJobError
                ):
                    manager.start_blender_export(
                        operation_id=operation_id,
                        virtual_assets=[asset],
                        **base_values,
                    )
                self.assertFalse((base / "jobs" / "jobs" / operation_id).exists())

            process = mock.Mock(pid=1234)
            process.poll.return_value = None
            with mock.patch.object(jobs.subprocess, "Popen", return_value=process):
                accepted = manager.start_blender_export(
                    operation_id=OPERATION_ID,
                    virtual_assets=["/project/hero/sk_valid.sk_valid"],
                    **base_values,
                )
            self.assertEqual(
                accepted["request"]["virtualAssets"],
                ["Project/Hero/SK_Valid"],
            )
            self.assertEqual(
                accepted["request"]["inventoryAssets"][0]["kind"],
                "SkeletalMesh",
            )
            self.assertRegex(
                accepted["request"]["inventoryAssets"][0]["identitySha256"],
                r"^[0-9a-f]{64}$",
            )
            running = manager._running.pop(OPERATION_ID)
            running.stdout.close()
            running.stderr.close()

    def test_blender_rejects_a_stale_real_inventory_decision_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            environment = _provider_environment(base)
            manager = UnpackedAssetJobManager(base / "jobs", environment)
            stale = manager.resolve_inventory_decision("Demo", 42)
            database = base / "providers" / "analysis" / "Demo" / "inventory.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE inventories SET stored_at = ? WHERE id = ?;",
                    ("2026-08-29T02:03:04Z", 42),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(UnpackedAssetJobError, "decision changed"):
                manager.start_blender_export(
                    operation_id=OPERATION_ID,
                    profile="Demo",
                    inventory_id=42,
                    expected_inventory_decision_sha256=stale["decisionSha256"],
                    virtual_assets=["Game/Hero/SK_Hero"],
                    lod0_only=False,
                    export_morph_targets=True,
                    prefer_uemodel=False,
                    export_only=True,
                )
            self.assertFalse((base / "jobs" / "jobs" / OPERATION_ID).exists())


class UnpackedAssetJobContinuationTests(unittest.TestCase):
    assets = [
        "Game/Hero/SK_Hero",
        "SilverPalace/Content/Hero/SK_Hero",
    ]
    continuation_id = "b" * 32

    def test_continuation_derives_only_failed_assets_and_is_hash_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "jobs"
            manager = UnpackedAssetJobManager(root, _provider_environment(base))
            capabilities = manager.capabilities()
            self.assertTrue(
                capabilities["routes"]["blenderContinuation"]["available"]
            )
            self.assertTrue(
                capabilities["routes"]["blenderContinuation"][
                    "successfulBundlesReexported"
                ]
                is False
            )
            unused_state, parent, parent_paths = _write_partial_parent(
                manager,
                root,
                assets=self.assets,
                missing_indices=(1,),
            )
            process = mock.Mock(pid=4321)
            process.poll.return_value = None
            arguments = {
                "operation_id": self.continuation_id,
                "parent_operation_id": OPERATION_ID,
                "expected_parent_request_sha256": parent["requestSha256"],
                "expected_parent_result_sha256": parent["resultSha256"],
            }
            with mock.patch.object(
                jobs.subprocess, "Popen", return_value=process
            ) as popen:
                first = manager.start_blender_continuation(**arguments)
                second = manager.start_blender_continuation(**arguments)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(first["requestSha256"], second["requestSha256"])
            continuation = first["request"]["continuation"]
            self.assertEqual(continuation["retryIndices"], [1])
            self.assertEqual(continuation["retryAssets"], [self.assets[1]])
            self.assertEqual(
                [item["asset"] for item in continuation["retainedItems"]],
                [self.assets[0]],
            )
            self.assertNotIn("sourceRoot", continuation["retainedItems"][0])
            self.assertNotIn("artifacts", continuation["retainedItems"][0])
            self.assertRegex(
                continuation["retainedItems"][0]["artifactSetSha256"],
                r"^[0-9a-f]{64}$",
            )
            plan_path = (
                root
                / "jobs"
                / self.continuation_id
                / "input"
                / "continuation-plan.json"
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["retryAssets"], [self.assets[1]])
            self.assertEqual(
                [source["origin"] for source in plan["sources"]],
                ["retained", "retry"],
            )
            self.assertEqual(
                plan["sources"][0]["bundleManifest"]["sha256"],
                _sha256(parent_paths["bundleManifests"][0]),
            )
            command = popen.call_args.args[0]
            self.assertEqual(command[-2], "--run-blender-continuation")
            self.assertEqual(Path(command[-1]), plan_path)
            self.assertNotIn(str(parent_paths["bundleManifests"][0]), command)
            signature = inspect.signature(
                UnpackedAssetJobManager.start_blender_continuation
            ).parameters
            self.assertTrue(
                {
                    "path",
                    "manifest",
                    "accepted_items",
                    "failed_items",
                    "virtual_assets",
                }.isdisjoint(signature)
            )
            with self.assertRaisesRegex(UnpackedAssetJobError, "exact terminal partial"):
                manager.start_blender_continuation(
                    **{
                        **arguments,
                        "operation_id": "c" * 32,
                        "expected_parent_result_sha256": "0" * 64,
                    }
                )
            running = manager._running.pop(self.continuation_id)
            running.stdout.close()
            running.stderr.close()

    def test_continuation_rejects_tampered_retained_bundle_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "jobs"
            manager = UnpackedAssetJobManager(root, _provider_environment(base))
            unused_state, parent, parent_paths = _write_partial_parent(
                manager,
                root,
                assets=self.assets,
                missing_indices=(1,),
            )
            _write(parent_paths["bundleManifests"][0], b"{}\n")
            with self.assertRaisesRegex(UnpackedAssetJobError, "manifest hash changed"):
                manager.start_blender_continuation(
                    operation_id=self.continuation_id,
                    parent_operation_id=OPERATION_ID,
                    expected_parent_request_sha256=parent["requestSha256"],
                    expected_parent_result_sha256=parent["resultSha256"],
                )
            self.assertFalse((root / "jobs" / self.continuation_id).exists())

    def test_restart_adopts_combined_parent_and_retry_shared_blend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "jobs"
            manager = UnpackedAssetJobManager(root, _provider_environment(base))
            unused_state, parent, parent_paths = _write_partial_parent(
                manager,
                root,
                assets=self.assets,
                missing_indices=(1,),
            )
            process = mock.Mock(pid=4321)
            process.poll.return_value = None
            with mock.patch.object(jobs.subprocess, "Popen", return_value=process):
                manager.start_blender_continuation(
                    operation_id=self.continuation_id,
                    parent_operation_id=OPERATION_ID,
                    expected_parent_request_sha256=parent["requestSha256"],
                    expected_parent_result_sha256=parent["resultSha256"],
                )
            running = manager._running.pop(self.continuation_id)
            running.stdout.close()
            running.stderr.close()
            job_dir = root / "jobs" / self.continuation_id
            state = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
            request = state["request"]

            retry_source = base / "retry-source"
            retry_request = {
                **request,
                "virtualAssets": [self.assets[1]],
                "inventoryAssets": [request["inventoryAssets"][1]],
            }
            retry_paths = _write_blender_bundle(
                retry_source, {"request": retry_request}
            )
            retry_bundle = retry_paths["bundleManifests"][0].parent
            parent_manifest = parent_paths["bundleManifests"][0]
            calls: list[tuple[list[str], str]] = []

            def execute(command, label):
                command = list(command)
                calls.append((command, label))
                if label == "UEX continuation retry export":
                    retry_destination = (
                        job_dir
                        / "target"
                        / "retry"
                        / "Retry_Assembly_uex_export"
                        / "bundles"
                        / "01_SK_Hero"
                    )
                    retry_destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(retry_bundle, retry_destination)
                elif label == "Blender continuation assembly":
                    output = Path(command[command.index("--output") + 1])
                    _write(output, b"COMBINED-BLENDER")
                elif label == "Blender continuation validation":
                    report = Path(command[command.index("--report") + 1])
                    blend = job_dir / "target" / "UEX_Assembly.blend"
                    _write(
                        report,
                        _json_bytes(
                            {
                                "schema": "uex.blender-validation.v1",
                                "ok": True,
                                "blendFile": str(blend.resolve()),
                                "checks": {
                                    "meshCount": 2,
                                    "armatureObjectCount": 1,
                                    "referencedArmatureCount": 1,
                                    "sharedArmature": "Hero_Rig",
                                    "boneCount": 32,
                                    "lodCollections": {"0": ["Hero_LOD0"]},
                                    "missingImages": [],
                                    "meshes": [
                                        {
                                            "name": "Body",
                                            "vertices": 3,
                                            "weightedVertices": 3,
                                            "armatureModifiers": 1,
                                        },
                                        {
                                            "name": "Face",
                                            "vertices": 3,
                                            "weightedVertices": 3,
                                            "armatureModifiers": 1,
                                        },
                                    ],
                                },
                                "errors": [],
                            }
                        ),
                    )

            with mock.patch.object(
                jobs, "_run_continuation_command", side_effect=execute
            ):
                jobs._run_blender_continuation_plan(
                    job_dir / "input" / "continuation-plan.json"
                )
            labels = [label for unused_command, label in calls]
            self.assertEqual(
                labels,
                [
                    "UEX continuation retry export",
                    "Blender continuation assembly",
                    "Blender continuation validation",
                ],
            )
            retry_command = calls[0][0]
            self.assertEqual(
                [retry_command[index + 1] for index, value in enumerate(retry_command) if value == "--asset"],
                [self.assets[1]],
            )
            self.assertNotIn(self.assets[0], retry_command)
            assembly_command = calls[1][0]
            self.assertEqual(assembly_command.count("--manifest"), 2)

            recovered = manager.status(self.continuation_id)
            self.assertEqual(recovered["status"], "completed")
            self.assertTrue(recovered["result"]["complete"])
            self.assertTrue(recovered["result"]["recoveredAfterHostRestart"])
            self.assertTrue(recovered["result"]["assemblyReceipt"]["accepted"])
            self.assertEqual(
                recovered["result"]["aggregate"]["retainedBundleCount"], 1
            )
            self.assertEqual(
                recovered["result"]["aggregate"]["retriedBundleCount"], 1
            )
            self.assertEqual(
                [item["sourceOperationId"] for item in recovered["result"]["items"]],
                [OPERATION_ID, self.continuation_id],
            )
            self.assertEqual(
                recovered["result"]["validationReceipt"]["schema"],
                "uex.blender-validation.v1",
            )
            self.assertTrue(parent_manifest.is_file())

    def test_driver_skips_export_when_all_bundles_are_retained_and_reassembles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "jobs"
            manager = UnpackedAssetJobManager(root, _provider_environment(base))
            unused_state, parent, unused_paths = _write_partial_parent(
                manager,
                root,
                assets=self.assets,
                missing_indices=(),
            )
            process = mock.Mock(pid=4321)
            process.poll.return_value = None
            with mock.patch.object(jobs.subprocess, "Popen", return_value=process):
                started = manager.start_blender_continuation(
                    operation_id=self.continuation_id,
                    parent_operation_id=OPERATION_ID,
                    expected_parent_request_sha256=parent["requestSha256"],
                    expected_parent_result_sha256=parent["resultSha256"],
                )
            self.assertEqual(started["request"]["continuation"]["retryAssets"], [])
            running = manager._running.pop(self.continuation_id)
            running.stdout.close()
            running.stderr.close()
            job_dir = root / "jobs" / self.continuation_id
            plan_path = job_dir / "input" / "continuation-plan.json"
            calls: list[tuple[list[str], str]] = []

            def execute(command, label):
                command = list(command)
                calls.append((command, label))
                if label == "Blender continuation assembly":
                    output = Path(command[command.index("--output") + 1])
                    _write(output, b"REASSEMBLED")
                elif label == "Blender continuation validation":
                    report = Path(command[command.index("--report") + 1])
                    blend = job_dir / "target" / "UEX_Assembly.blend"
                    _write(
                        report,
                        _json_bytes(
                            {
                                "schema": "uex.blender-validation.v1",
                                "ok": True,
                                "blendFile": str(blend.resolve()),
                                "checks": {
                                    "meshCount": 2,
                                    "armatureObjectCount": 1,
                                    "referencedArmatureCount": 1,
                                    "sharedArmature": "Hero_Rig",
                                    "boneCount": 32,
                                    "lodCollections": {"0": ["Hero_LOD0"]},
                                    "missingImages": [],
                                    "meshes": [
                                        {
                                            "name": "Body",
                                            "vertices": 3,
                                            "weightedVertices": 3,
                                            "armatureModifiers": 1,
                                        }
                                    ],
                                },
                                "errors": [],
                            }
                        ),
                    )

            with mock.patch.object(
                jobs, "_run_continuation_command", side_effect=execute
            ):
                jobs._run_blender_continuation_plan(plan_path)
            labels = [label for unused_command, label in calls]
            self.assertEqual(
                labels,
                ["Blender continuation assembly", "Blender continuation validation"],
            )
            assembly_command = calls[0][0]
            self.assertEqual(assembly_command.count("--manifest"), 2)
            self.assertNotIn("UEX continuation retry export", labels)
            recovered = manager.status(self.continuation_id)
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(
                recovered["result"]["aggregate"]["retainedBundleCount"], 2
            )
            self.assertEqual(
                recovered["result"]["aggregate"]["retriedBundleCount"], 0
            )

    def test_restart_rejects_a_changed_host_staged_continuation_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "jobs"
            manager = UnpackedAssetJobManager(root, _provider_environment(base))
            unused_state, parent, unused_paths = _write_partial_parent(
                manager,
                root,
                assets=self.assets,
                missing_indices=(1,),
            )
            process = mock.Mock(pid=4321)
            process.poll.return_value = None
            with mock.patch.object(jobs.subprocess, "Popen", return_value=process):
                manager.start_blender_continuation(
                    operation_id=self.continuation_id,
                    parent_operation_id=OPERATION_ID,
                    expected_parent_request_sha256=parent["requestSha256"],
                    expected_parent_result_sha256=parent["resultSha256"],
                )
            running = manager._running.pop(self.continuation_id)
            running.stdout.close()
            running.stderr.close()
            plan = (
                root
                / "jobs"
                / self.continuation_id
                / "input"
                / "continuation-plan.json"
            )
            _write(plan, b"{}")
            recovered = manager.status(self.continuation_id)
            self.assertEqual(recovered["status"], "interrupted")
            self.assertIsNone(recovered["result"])
            self.assertIn("staged plan", recovered["error"])


class UnpackedAssetReportVerificationTests(unittest.TestCase):
    def test_completed_manifest_is_adopted_after_host_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "managed"
            job_dir = root / "jobs" / OPERATION_ID
            _write_report_bundle(job_dir)
            state = {
                "schema": jobs.JOB_SCHEMA,
                "schemaVersion": 1,
                "operationId": OPERATION_ID,
                "kind": "uex-asset-report",
                "requestSha256": "f" * 64,
                "request": _report_state()["request"],
                "status": "running",
                "pid": 1234,
                "exitCode": None,
                "result": None,
                "error": "",
                "createdUnixMs": 1,
                "updatedUnixMs": 1,
            }
            _write(job_dir / "job.json", _json_bytes(state))
            result = UnpackedAssetJobManager(root, {}).status(OPERATION_ID)

            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["result"]["recoveredAfterHostRestart"])

    def test_accepts_real_uex_four_file_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary) / "job"
            paths = _write_report_bundle(job_dir)
            manager = UnpackedAssetJobManager(Path(temporary) / "managed", {})
            result = manager._verify_report(_report_state(), job_dir)
            self.assertTrue(result["complete"])
            self.assertEqual(result["inventoryId"], 42)
            self.assertEqual(len(result["artifacts"]), 4)
            self.assertEqual(
                {artifact["path"] for artifact in result["artifacts"]},
                {path.relative_to(job_dir / "target").as_posix() for path in paths.values()},
            )
            self.assertEqual(
                result["completionManifest"]["path"], "asset-report-manifest.json"
            )

    def test_rejects_missing_or_unlisted_portable_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "managed", {})
            for missing in ("html", "json", "csv", "manifest"):
                job_dir = base / missing
                paths = _write_report_bundle(job_dir)
                paths[missing].unlink()
                with self.subTest(missing=missing), self.assertRaises(
                    UnpackedAssetJobError
                ):
                    manager._verify_report(_report_state(), job_dir)

    def test_manifest_binds_each_output_by_exact_name_size_and_hash(self) -> None:
        mutations = {
            "wrong-name": lambda artifacts: artifacts[0].update(
                {"fileName": "../asset-report.json"}
            ),
            "wrong-size": lambda artifacts: artifacts[0].update(
                {"bytes": artifacts[0]["bytes"] + 1}
            ),
            "wrong-hash": lambda artifacts: artifacts[0].update({"sha256": "0" * 64}),
            "duplicate": lambda artifacts: artifacts.__setitem__(1, dict(artifacts[0])),
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "managed", {})
            accepted: list[str] = []
            for label, mutation in mutations.items():
                job_dir = base / label
                _write_report_bundle(job_dir, artifact_mutator=mutation)
                try:
                    manager._verify_report(_report_state(), job_dir)
                except UnpackedAssetJobError:
                    pass
                else:
                    accepted.append(label)
            self.assertEqual(accepted, [], f"invalid manifest mutations accepted: {accepted}")

    def test_inventory_and_profile_must_match_structured_outputs(self) -> None:
        variants = {
            "manifest-inventory": {"manifest_inventory_id": 43},
            "report-inventory": {"report_inventory_id": 43},
            "html-inventory": {"html_inventory_id": 43},
            "manifest-profile": {"manifest_profile": "Other"},
            "report-profile": {"report_profile": "Other"},
            "manifest-schema": {"manifest_schema_version": 2},
            "manifest-kind": {"manifest_kind": "ordinary-html-report"},
            "report-schema": {"report_schema_version": 2},
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "managed", {})
            accepted: list[str] = []
            for label, updates in variants.items():
                job_dir = base / label
                _write_report_bundle(job_dir, **updates)
                try:
                    manager._verify_report(_report_state(), job_dir)
                except UnpackedAssetJobError:
                    pass
                else:
                    accepted.append(label)
            self.assertEqual(accepted, [], f"mismatched report metadata accepted: {accepted}")


class UnpackedAssetBlenderVerificationTests(unittest.TestCase):
    def test_accepts_complete_validated_uemodel_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary) / "job"
            state = _blender_state(
                ["/Game/Hero/SK_Hero_Body", "/Game/Hero/SK_Hero_Face"]
            )
            _write_blender_bundle(job_dir, state)
            manager = UnpackedAssetJobManager(Path(temporary) / "managed", {})
            result = manager._verify_blender(state, job_dir)
            self.assertTrue(result["complete"])
            self.assertFalse(result["exportOnly"])
            self.assertEqual(result["geometrySource"], "uemodel")
            self.assertEqual(result["bundleCount"], 2)
            self.assertFalse(result["partial"])
            self.assertEqual(result["aggregate"]["status"], "complete")
            self.assertEqual(result["inventoryId"], 42)
            self.assertEqual(
                result["inventoryDecisionSha256"],
                state["request"]["inventoryDecisionSha256"],
            )
            self.assertTrue(result["assemblyReceipt"]["accepted"])
            self.assertEqual(
                result["assemblyReceipt"]["inventoryDecisionSha256"],
                state["request"]["inventoryDecisionSha256"],
            )
            self.assertEqual(
                result["assemblyReceipt"]["sourceAssets"],
                state["request"]["virtualAssets"],
            )
            self.assertEqual(
                [item["status"] for item in result["items"]],
                ["completed", "completed"],
            )
            self.assertIsNotNone(result["blend"])
            self.assertIsNotNone(result["validation"])
            self.assertEqual(
                result["validationReceipt"],
                {
                    "schema": "uex.blender-validation.v1",
                    "ok": True,
                    "errors": 0,
                    "meshCount": 2,
                    "referencedArmatureCount": 1,
                    "boneCount": 32,
                    "sharedArmature": "Hero_Rig",
                    "lod0CollectionCount": 1,
                    "missingImageCount": 0,
                },
            )

    def test_accepts_bundle_only_export_without_claiming_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary) / "job"
            state = _blender_state(export_only=True)
            _write_blender_bundle(job_dir, state)
            manager = UnpackedAssetJobManager(Path(temporary) / "managed", {})
            result = manager._verify_blender(state, job_dir)
            self.assertTrue(result["complete"])
            self.assertTrue(result["exportOnly"])
            self.assertEqual(result["geometrySource"], "not-assembled")
            self.assertIsNone(result["blend"])
            self.assertIsNone(result["validation"])
            self.assertIsNone(result["validationReceipt"])
            self.assertTrue(result["assemblyReceipt"]["accepted"])

    def test_accepts_glb_only_when_the_request_explicitly_selected_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary) / "job"
            state = _blender_state(prefer_uemodel=False)
            _write_blender_bundle(job_dir, state)
            manager = UnpackedAssetJobManager(Path(temporary) / "managed", {})
            result = manager._verify_blender(state, job_dir)
            self.assertTrue(result["complete"])
            self.assertEqual(result["geometrySource"], "glb")

    def test_real_provider_package_source_identity_normalizes_without_false_mismatch(self) -> None:
        def provider_source(bundle, _index, _directory):
            bundle["sourceAsset"] = "Game/Hero/SK_Hero_Body.uasset"

        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary) / "job"
            state = _blender_state(["/Game/Hero/SK_Hero_Body"])
            _write_blender_bundle(job_dir, state, bundle_mutator=provider_source)
            result = UnpackedAssetJobManager(
                Path(temporary) / "managed", {}
            )._verify_blender(state, job_dir)
            self.assertTrue(result["complete"])
            self.assertEqual(result["items"][0]["asset"], "Game/Hero/SK_Hero_Body")

    def test_nonzero_multi_asset_job_preserves_valid_bundles_and_returns_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "managed"
            job_dir = root / "jobs" / OPERATION_ID
            state = _blender_state(
                ["Game/Hero/SK_Hero_Body", "Game/Hero/SK_Hero_Face"]
            )
            paths = _write_blender_bundle(job_dir, state)
            shutil.rmtree(paths["bundleManifests"][1].parent)
            _write(job_dir / "stdout.log", "")
            _write(job_dir / "stderr.log", "second asset failed\n")
            state.update(
                {
                    "schema": jobs.JOB_SCHEMA,
                    "schemaVersion": 1,
                    "operationId": OPERATION_ID,
                    "requestSha256": "f" * 64,
                    "status": "running",
                    "pid": 1234,
                    "exitCode": None,
                    "result": None,
                    "error": "",
                    "createdUnixMs": 1,
                    "updatedUnixMs": 1,
                }
            )
            manager = UnpackedAssetJobManager(root, {})
            finished = manager._finish(OPERATION_ID, state, job_dir, 7)

            self.assertEqual(finished["status"], "partial")
            self.assertEqual(finished["error"], "")
            self.assertFalse(finished["result"]["complete"])
            self.assertTrue(finished["result"]["partial"])
            self.assertIsNone(finished["result"]["validationReceipt"])
            self.assertEqual(
                [item["status"] for item in finished["result"]["items"]],
                ["completed", "failed"],
            )
            self.assertEqual(
                finished["result"]["aggregate"],
                {
                    "status": "partial",
                    "requestedCount": 2,
                    "completedBundleCount": 1,
                    "failedItemCount": 1,
                    "assemblyAccepted": False,
                    "inventoryId": 42,
                    "inventoryDecisionSha256": state["request"][
                        "inventoryDecisionSha256"
                    ],
                },
            )
            self.assertFalse(finished["result"]["assemblyReceipt"]["accepted"])
            self.assertEqual(
                finished["result"]["assemblyReceipt"][
                    "inventoryDecisionSha256"
                ],
                state["request"]["inventoryDecisionSha256"],
            )
            first_manifest = paths["bundleManifests"][0]
            self.assertTrue(first_manifest.is_file())
            self.assertIn(
                first_manifest.relative_to(paths["target"]).as_posix(),
                {item["path"] for item in finished["result"]["artifacts"]},
            )
            public = manager._public_state(finished, job_dir)
            self.assertEqual(public["error"], "")
            self.assertIn("provider exited", public["result"]["providerFailure"])
            self.assertTrue(public["policy"]["terminal"])
            self.assertTrue(public["policy"]["successfulItemsImmutable"])
            self.assertTrue(public["policy"]["retryOnlyFailedItemsWithNewOperation"])

    def test_zero_exit_assembly_failure_keeps_partial_top_level_error_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "managed"
            job_dir = root / "jobs" / OPERATION_ID
            state = _blender_state(
                ["Game/Hero/SK_Hero_Body", "Game/Hero/SK_Hero_Face"]
            )
            paths = _write_blender_bundle(job_dir, state)
            paths["assembly"].unlink()
            _write(job_dir / "stdout.log", "")
            _write(job_dir / "stderr.log", "")
            state.update(
                {
                    "schema": jobs.JOB_SCHEMA,
                    "schemaVersion": 1,
                    "operationId": OPERATION_ID,
                    "requestSha256": "f" * 64,
                    "status": "running",
                    "pid": 1234,
                    "exitCode": None,
                    "result": None,
                    "error": "",
                    "createdUnixMs": 1,
                    "updatedUnixMs": 1,
                }
            )
            manager = UnpackedAssetJobManager(root, {})
            finished = manager._finish(OPERATION_ID, state, job_dir, 0)
            public = manager._public_state(finished, job_dir)

            self.assertEqual(public["status"], "partial")
            self.assertEqual(public["error"], "")
            self.assertFalse(public["result"]["complete"])
            self.assertEqual(
                public["result"]["aggregate"]["completedBundleCount"], 2
            )
            self.assertFalse(public["result"]["assemblyReceipt"]["accepted"])
            self.assertIn(
                "assembly manifest", public["result"]["providerFailure"]
            )

    def test_restart_recovers_valid_bundle_subset_without_claiming_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "managed"
            job_dir = root / "jobs" / OPERATION_ID
            state = _blender_state(
                ["Game/Hero/SK_Hero_Body", "Game/Hero/SK_Hero_Face"]
            )
            paths = _write_blender_bundle(job_dir, state)
            shutil.rmtree(paths["bundleManifests"][1].parent)
            persisted = {
                "schema": jobs.JOB_SCHEMA,
                "schemaVersion": 1,
                "operationId": OPERATION_ID,
                "kind": state["kind"],
                "requestSha256": "f" * 64,
                "request": state["request"],
                "status": "running",
                "pid": 1234,
                "exitCode": None,
                "result": None,
                "error": "",
                "createdUnixMs": 1,
                "updatedUnixMs": 1,
            }
            _write(job_dir / "job.json", _json_bytes(persisted))
            _write(job_dir / "stdout.log", "")
            _write(job_dir / "stderr.log", "")
            recovered = UnpackedAssetJobManager(root, {}).status(OPERATION_ID)
            self.assertEqual(recovered["status"], "partial")
            self.assertEqual(recovered["error"], "")
            self.assertFalse(recovered["result"]["complete"])
            self.assertTrue(recovered["result"]["recoveredAfterHostRestart"])
            self.assertIn(
                "process ownership was lost",
                recovered["result"]["providerFailure"],
            )
            self.assertEqual(
                recovered["result"]["aggregate"]["completedBundleCount"], 1
            )

    def test_blender_verification_rejects_changed_inventory_binding(self) -> None:
        mutations = {
            "inventory-id": lambda request: request.update({"inventoryId": 43}),
            "decision-hash": lambda request: request.update(
                {"inventoryDecisionSha256": "0" * 64}
            ),
            "decision-content": lambda request: request["inventoryDecision"].update(
                {"assetCount": 999}
            ),
            "asset-path": lambda request: request["inventoryAssets"][0].update(
                {"exportAssetPath": "Game/Other/SK_Other"}
            ),
            "asset-kind": lambda request: request["inventoryAssets"][0].update(
                {"kind": "StaticMesh"}
            ),
            "asset-class": lambda request: request["inventoryAssets"][0].update(
                {"assetClass": "StaticMesh"}
            ),
            "asset-unavailable": lambda request: request["inventoryAssets"][0].update(
                {"available": False}
            ),
            "asset-identity-hash": lambda request: request["inventoryAssets"][0].update(
                {"identitySha256": "0" * 64}
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "managed", {})
            accepted: list[str] = []
            for label, mutation in mutations.items():
                job_dir = base / label
                state = _blender_state()
                _write_blender_bundle(job_dir, state)
                mutation(state["request"])
                try:
                    manager._verify_blender(state, job_dir)
                except UnpackedAssetJobError:
                    pass
                else:
                    accepted.append(label)
            self.assertEqual(
                accepted, [], f"changed Inventory bindings accepted: {accepted}"
            )

    def test_assembly_profile_assets_options_and_paths_are_request_bound(self) -> None:
        def wrong_output(assembly):
            assembly["outputBlend"] = str(Path(temporary) / "outside.blend")

        mutations = {
            "profile": lambda value: value.update({"profile": "Other"}),
            "asset-order": lambda value: value.update(
                {"sourceAssets": list(reversed(value["sourceAssets"]))}
            ),
            "lod-option": lambda value: value["options"].update({"lod0Only": True}),
            "morph-option": lambda value: value["options"].update(
                {"exportMorphTargets": False}
            ),
            "prefer-option": lambda value: value["options"].update(
                {"preferUeModel": False}
            ),
            "export-option": lambda value: value["options"].update({"exportOnly": True}),
            "output-path": wrong_output,
            "validation-path": lambda value: value.update(
                {"validationReport": str(Path(temporary) / "outside.validation.json")}
            ),
            "assembly-path": lambda value: value.update(
                {"assemblyManifest": str(Path(temporary) / "outside.assembly.json")}
            ),
            "bundle-record-asset": lambda value: value["bundles"][0].update(
                {"asset": "/Game/Other/SK_Other"}
            ),
            "bundle-directory": lambda value: value["bundles"][0].update(
                {"directory": str(Path(temporary) / "outside-bundle")}
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "managed", {})
            accepted: list[str] = []
            for label, mutation in mutations.items():
                job_dir = base / label
                state = _blender_state(
                    ["/Game/Hero/SK_Hero_Body", "/Game/Hero/SK_Hero_Face"]
                )
                _write_blender_bundle(job_dir, state, assembly_mutator=mutation)
                try:
                    manager._verify_blender(state, job_dir)
                except UnpackedAssetJobError:
                    pass
                else:
                    accepted.append(label)
            self.assertEqual(accepted, [], f"unbound assembly mutations accepted: {accepted}")

    def test_bundle_identity_type_and_files_are_strictly_validated(self) -> None:
        def mutate_source(bundle, _index, _directory):
            bundle["sourceAsset"] = "/Game/Other/SK_Other"

        def mutate_type(bundle, _index, _directory):
            bundle["assetType"] = "UStaticMesh"

        def remove_mesh(bundle, _index, _directory):
            bundle["meshes"] = []

        def traverse_mesh(bundle, _index, directory):
            _write(directory.parent / "escaped.glb", b"outside bundle")
            bundle["meshes"][0]["relativePath"] = "../escaped.glb"

        def wrong_mesh_size(bundle, _index, _directory):
            bundle["meshes"][0]["bytes"] += 1

        def missing_material(bundle, _index, _directory):
            bundle["materials"][0]["materialJson"] = "materials/missing.json"

        def absolute_texture(bundle, _index, directory):
            bundle["textures"][0]["relativePath"] = str(
                (directory / "textures" / "base.png").resolve()
            )

        def missing_importer(_bundle, _index, directory):
            (directory / "import_to_blender.py").unlink()

        def missing_uemodel(bundle, _index, _directory):
            bundle["ueModel"] = "models/missing.uemodel"

        def traverse_uemodel(bundle, _index, directory):
            _write(directory.parent / "escaped.uemodel", b"outside bundle")
            bundle["ueModel"] = "../escaped.uemodel"

        def missing_lod0(bundle, _index, _directory):
            bundle["meshes"][0]["lodIndex"] = 1

        def no_bones(bundle, _index, _directory):
            bundle["boneCount"] = 0

        mutations = {
            "source-asset": mutate_source,
            "asset-type": mutate_type,
            "empty-meshes": remove_mesh,
            "mesh-traversal": traverse_mesh,
            "mesh-size": wrong_mesh_size,
            "missing-material": missing_material,
            "absolute-texture": absolute_texture,
            "missing-importer": missing_importer,
            "missing-uemodel": missing_uemodel,
            "uemodel-traversal": traverse_uemodel,
            "missing-lod0": missing_lod0,
            "no-bones": no_bones,
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "managed", {})
            accepted: list[str] = []
            for label, mutation in mutations.items():
                job_dir = base / label
                state = _blender_state()
                _write_blender_bundle(job_dir, state, bundle_mutator=mutation)
                try:
                    manager._verify_blender(state, job_dir)
                except UnpackedAssetJobError:
                    pass
                else:
                    accepted.append(label)
            self.assertEqual(accepted, [], f"invalid bundle mutations accepted: {accepted}")

    def test_preferred_geometry_and_output_hash_are_not_silently_downgraded(self) -> None:
        mutations = {
            "glb-downgrade": lambda value: value.update({"geometrySource": "glb"}),
            "not-assembled": lambda value: value.update(
                {"geometrySource": "not-assembled"}
            ),
            "wrong-hash": lambda value: value.update({"outputSha256": "0" * 64}),
            "validation-not-ok": lambda value: value.update({"validationOk": False}),
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "managed", {})
            accepted: list[str] = []
            for label, mutation in mutations.items():
                job_dir = base / label
                state = _blender_state(prefer_uemodel=True)
                _write_blender_bundle(job_dir, state, assembly_mutator=mutation)
                try:
                    manager._verify_blender(state, job_dir)
                except UnpackedAssetJobError:
                    pass
                else:
                    accepted.append(label)
            self.assertEqual(accepted, [], f"invalid geometry claims accepted: {accepted}")

    def test_validation_report_binds_blend_and_rechecks_critical_invariants(self) -> None:
        def minimal(value):
            value.clear()
            value.update({"schema": "uex.blender-validation.v1", "ok": True})

        def wrong_blend(value):
            value["blendFile"] = str(Path(temporary) / "different.blend")

        def reported_errors(value):
            value["errors"] = ["missing LOD0"]

        def no_mesh(value):
            value["checks"]["meshCount"] = 0

        def no_shared_rig(value):
            value["checks"]["referencedArmatureCount"] = 0
            value["checks"]["sharedArmature"] = None

        def no_bones(value):
            value["checks"]["boneCount"] = 0

        def no_lod0(value):
            value["checks"]["lodCollections"] = {"1": ["Hero_LOD1"]}

        def missing_images(value):
            value["checks"]["missingImages"] = ["T_Missing"]

        def no_weights(value):
            value["checks"]["meshes"][0]["weightedVertices"] = 0

        def no_modifier(value):
            value["checks"]["meshes"][0]["armatureModifiers"] = 0

        mutations = {
            "minimal": minimal,
            "wrong-blend": wrong_blend,
            "reported-errors": reported_errors,
            "no-mesh": no_mesh,
            "no-shared-rig": no_shared_rig,
            "no-bones": no_bones,
            "no-lod0": no_lod0,
            "missing-images": missing_images,
            "no-weights": no_weights,
            "no-modifier": no_modifier,
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manager = UnpackedAssetJobManager(base / "managed", {})
            accepted: list[str] = []
            for label, mutation in mutations.items():
                job_dir = base / label
                state = _blender_state()
                _write_blender_bundle(job_dir, state, validation_mutator=mutation)
                try:
                    manager._verify_blender(state, job_dir)
                except UnpackedAssetJobError:
                    pass
                else:
                    accepted.append(label)
            self.assertEqual(accepted, [], f"invalid validation reports accepted: {accepted}")


if __name__ == "__main__":
    unittest.main()
