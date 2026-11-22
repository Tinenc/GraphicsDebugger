"""High-level orchestration for deterministic and reviewed symbol matching."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .evidence import collect_asset_evidence
from .matcher import (
    ALIAS_SCHEMA,
    ALIAS_VERSION,
    match_capture_catalog,
    validate_candidate,
)
from .providers import run_provider
from .scanner import (
    ingest_trusted_provider_result,
    normalize_stage,
    scan_asset_root,
)
from .store import SymbolStore


def default_workspace(asset_root: str | Path) -> Path:
    root = Path(asset_root).expanduser().resolve()
    key = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:16]
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".renderdoc-ultra"
    return base / "RenderDocUltra" / "symbols" / key


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-%d" % os.getpid())
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(str(temporary), str(path))


def _evidence_id(
    capture_id: str,
    shader_resource_id: int,
    stage: str,
    entry_point: str,
    asset_shader_id: int,
    evidence: dict,
) -> str:
    payload = {
        "captureId": capture_id,
        "shaderResourceId": int(shader_resource_id),
        "stage": stage,
        "entryPoint": entry_point,
        "assetShaderId": int(asset_shader_id),
        "evidence": evidence,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "evidence-v1-" + hashlib.sha256(encoded).hexdigest()


class SymbolService:
    """Facade used by MCP tools.

    Model output never enters the database through a raw SQL or arbitrary-code
    path.  A model can only select a candidate already emitted by the matcher;
    this class re-runs the deterministic validator before storing that choice.
    """

    def __init__(
        self, database_path: str | Path, *, read_only: bool = False
    ):
        self.database_path = Path(database_path).expanduser().resolve()
        self.read_only = bool(read_only)
        self.store = SymbolStore(self.database_path, read_only=self.read_only)

    @classmethod
    def for_asset_root(
        cls, asset_root: str | Path, database_path: str | Path | None = None
    ) -> "SymbolService":
        path = (
            Path(database_path).expanduser().resolve()
            if database_path
            else default_workspace(asset_root) / "symbols-v3.sqlite3"
        )
        return cls(path)

    def scan(
        self,
        asset_root: str | Path,
        build_id: str = "",
        max_files: int = 200000,
        max_file_size_mb: int = 2048,
        provider_id: str = "",
    ) -> dict:
        selected_provider = provider_id.strip()
        if selected_provider:
            normalized_build = build_id.strip() or "unknown-build"
            result = run_provider(
                selected_provider,
                asset_root,
                build_id=normalized_build,
            )
            return ingest_trusted_provider_result(
                result,
                asset_root,
                self.database_path,
                build_id=normalized_build,
                max_files=max_files,
                max_file_size_mb=max_file_size_mb,
            )
        return scan_asset_root(
            asset_root,
            self.database_path,
            build_id=build_id,
            max_files=max_files,
            max_file_size_mb=max_file_size_mb,
        )

    def match(self, catalog_path: str | Path, scan_id: str) -> dict:
        return match_capture_catalog(catalog_path, scan_id, self.database_path)

    def run(
        self,
        asset_root: str | Path,
        catalog_path: str | Path,
        build_id: str = "",
        max_files: int = 200000,
        max_file_size_mb: int = 2048,
        provider_id: str = "",
    ) -> dict:
        scan = self.scan(
            asset_root,
            build_id=build_id,
            max_files=max_files,
            max_file_size_mb=max_file_size_mb,
            provider_id=provider_id,
        )
        match = self.match(catalog_path, scan["scanId"])
        return {
            "schema": "renderdoc-ultra-symbol-run/v1",
            "databasePath": str(self.database_path),
            "scan": scan,
            "match": match,
        }

    def list_matches(self, capture_id: str) -> dict:
        matches = self.store.list_matches(capture_id)
        return {
            "captureId": capture_id,
            "databasePath": str(self.database_path),
            "matches": matches,
        }

    def unresolved(self, capture_id: str) -> dict:
        catalog_row = self.store.get_capture_catalog(capture_id)
        if catalog_row is None:
            raise ValueError("Unknown capture: %s" % capture_id)
        groups: dict[tuple[int, str, str], list[dict]] = {}
        for match in self.store.list_matches(capture_id):
            key = (
                int(match["shader_resource_id"]),
                match["stage"],
                match.get("entry_point", ""),
            )
            groups.setdefault(key, []).append(match)

        unresolved: list[dict] = []
        for shader in catalog_row["metadata"].get("shaders", []):
            key = (
                int(shader["shaderResourceId"]),
                shader.get("stage", ""),
                shader.get("entryPoint", ""),
            )
            candidates = groups.get(key, [])
            selected = next((item for item in candidates if item["selected"]), None)
            has_untrusted_symbol_candidate = any(
                isinstance(item.get("assetMetadata"), dict)
                and item["assetMetadata"].get("_symbolMetadataTrusted") is not True
                and bool(
                    item["assetMetadata"].get("constantBuffers")
                    or item["assetMetadata"].get("constantBlocks")
                    or item["assetMetadata"].get("resources")
                    or item["assetMetadata"].get("samplers")
                    or item["assetMetadata"].get("name")
                )
                for item in candidates
            )
            selected_has_trusted_symbols = bool(
                selected
                and selected.get("assetMetadata", {}).get("_symbolMetadataTrusted")
                is True
            )
            if selected and selected["status"] in (
                "ExactBlob",
                "ExecutableAndLayout",
                "AIProgramVerified",
                "ReusedSymbolRule",
            ) and (selected_has_trusted_symbols or not has_untrusted_symbol_candidate):
                continue
            unresolved.append(
                {
                    "itemId": "%d:%s:%s" % key,
                    "shaderResourceId": key[0],
                    "stage": key[1],
                    "entryPoint": key[2],
                    "rawSha256": shader.get("rawSha256", ""),
                    "codeSha256": shader.get("codeSha256", ""),
                    "layoutHash": shader.get("layoutHash", ""),
                    "uses": shader.get("uses", []),
                    "constantBuffers": shader.get("constantBuffers", []),
                    "identityProgramVerified": bool(
                        selected
                        and selected["status"]
                        in ("ExactBlob", "ExecutableAndLayout", "ReusedSymbolRule")
                    ),
                    "needsSymbolMetadataReview": bool(
                        selected and has_untrusted_symbol_candidate
                    ),
                    "candidates": [
                        {
                            "candidateId": item["asset_shader_id"],
                            "assetPath": item["relative_path"],
                            "displayName": item["display_name"],
                            "score": item["score"],
                            "status": item["status"],
                            "evidence": item["evidence"],
                            "assetMetadata": item["assetMetadata"],
                        }
                        for item in candidates
                    ],
                }
            )
        return {
            "schema": "renderdoc-ultra-ai-cases/v1",
            "captureId": capture_id,
            "count": len(unresolved),
            "cases": unresolved,
            "instructions": {
                "allowed": "Select only a listed candidateId and cite its evidence.",
                "forbidden": "Do not invent registers, offsets, hashes, or candidate IDs.",
            },
        }

    def evidence(
        self,
        capture_id: str,
        shader_resource_id: int,
        stage: str,
        entry_point: str,
        candidate_id: int | None = None,
        top_k: int = 10,
    ) -> dict:
        stage = normalize_stage(stage)
        entry_point = str(entry_point or "")
        capture = self.store.get_capture_shader(
            capture_id, shader_resource_id, stage, entry_point
        )
        if capture is None:
            raise ValueError("Unknown captured shader/stage")
        matches = [
            item
            for item in self.store.list_matches(capture_id)
            if int(item["shader_resource_id"]) == int(shader_resource_id)
            and item["stage"] == stage
            and item.get("entry_point", "") == entry_point
        ]
        if candidate_id is not None:
            matches = [
                item
                for item in matches
                if int(item["asset_shader_id"]) == int(candidate_id)
            ]
            if not matches:
                raise ValueError("candidate_id is not a candidate for this captured shader")
        else:
            matches = matches[: max(1, min(int(top_k), 50))]

        candidates: list[dict] = []
        for match in matches:
            asset = self.store.get_asset_shader(match["asset_shader_id"])
            if asset is None:
                continue
            candidates.append(
                {
                    "evidenceId": _evidence_id(
                        capture_id,
                        shader_resource_id,
                        stage,
                        entry_point,
                        asset["id"],
                        match["evidence"],
                    ),
                    "match": {
                        "candidateId": int(asset["id"]),
                        "status": match["status"],
                        "score": match["score"],
                        "selected": match["selected"],
                        "evidence": match["evidence"],
                    },
                    "asset": collect_asset_evidence(self.store, asset),
                }
            )
        return {
            "schema": "renderdoc-ultra-match-evidence/v1",
            "captureId": capture_id,
            "shaderResourceId": int(shader_resource_id),
            "stage": stage,
            "entryPoint": entry_point,
            "captureShader": capture["metadata"],
            "candidates": candidates,
            "rules": {
                "boundedFileWindowBytes": 512 * 1024,
                "maySelectOnlyReturnedCandidateId": True,
                "physicalLayoutOwnedByValidator": True,
            },
        }

    def submit_decision(
        self,
        capture_id: str,
        scan_id: str,
        shader_resource_id: int,
        stage: str,
        entry_point: str,
        asset_shader_id: int,
        confidence: float,
        rationale: str,
        evidence_id: str,
    ) -> dict:
        stage = normalize_stage(stage)
        entry_point = str(entry_point or "")
        candidates = [
            item
            for item in self.store.list_matches(capture_id)
            if int(item["shader_resource_id"]) == int(shader_resource_id)
            and item["stage"] == stage
            and item.get("entry_point", "") == entry_point
        ]
        proposed = next(
            (
                item
                for item in candidates
                if int(item["asset_shader_id"]) == int(asset_shader_id)
            ),
            None,
        )
        if proposed is None:
            raise ValueError(
                "asset_shader_id is not a candidate emitted for this captured shader"
            )
        expected_evidence_id = _evidence_id(
            capture_id,
            shader_resource_id,
            stage,
            entry_point,
            asset_shader_id,
            proposed["evidence"],
        )
        if evidence_id != expected_evidence_id:
            raise ValueError(
                "evidence_id must come from get_symbol_match_evidence for this candidate"
            )
        deterministic = next(
            (
                item
                for item in candidates
                if item["selected"]
                and item["source"] == "program"
                and item["status"] in ("ExactBlob", "ExecutableAndLayout")
            ),
            None,
        )
        if deterministic and int(deterministic["asset_shader_id"]) == int(asset_shader_id):
            raise ValueError("The selected asset is already the program-verified identity")

        capture_row = self.store.get_capture_shader(
            capture_id, shader_resource_id, stage, entry_point
        )
        asset = self.store.get_asset_shader(asset_shader_id)
        if capture_row is None or asset is None:
            raise ValueError("Capture shader or asset shader no longer exists")
        if asset["scan_id"] != scan_id:
            raise ValueError("Candidate does not belong to the supplied asset scan")

        validation = validate_candidate(capture_row["metadata"], asset)
        if not validation["accepted"]:
            raise ValueError(validation["reason"])
        if deterministic and not (
            validation["status"] == "ExactBlob"
            and validation["evidence"].get("assetRawHashVerified")
        ):
            raise ValueError(
                "AI cannot replace a program-verified identity with a different shader"
            )
        confidence = max(0.0, min(float(confidence), 1.0))
        # MCP/model input is never an authority boundary. Even when physical
        # compatibility passes, semantic ownership chosen by AI stays review-
        # only until a future non-MCP RenderDoc UI approval flow exists.
        status = "AIProvisional"
        confidence = min(confidence, 0.94)

        evidence = {
            "evidenceId": evidence_id,
            "candidateId": int(asset_shader_id),
            "validator": validation,
            "modelRationale": rationale,
            "userConfirmed": False,
            "authority": "mcp-model-proposal",
        }
        self.store.set_ai_decision(
            capture_id,
            shader_resource_id,
            stage,
            entry_point,
            asset_shader_id,
            status,
            confidence,
            rationale,
            evidence,
        )

        return {
            "captureId": capture_id,
            "shaderResourceId": int(shader_resource_id),
            "stage": stage,
            "entryPoint": entry_point,
            "assetShaderId": int(asset_shader_id),
            "status": status,
            "proposalAuthority": "AIProvisional",
            "programVerified": False,
            "activated": False,
            "committed": False,
            "aliasCount": 0,
            "requiresTrustedUIApproval": True,
            "validation": validation,
        }

    def export_alias_table(
        self,
        capture_id: str,
        output_path: str | Path,
        verified_only: bool = True,
        scan_id: str = "",
        catalog_sha256: str = "",
    ) -> dict:
        if not verified_only:
            raise ValueError(
                "Unverified aliases are review evidence and cannot be exported for application"
            )
        catalog = self.store.get_capture_catalog(capture_id)
        if catalog is None:
            raise ValueError("Unknown capture: %s" % capture_id)
        revision_sha256 = str(catalog_sha256 or "").strip().lower()
        if revision_sha256 and (
            len(revision_sha256) != 64
            or any(character not in "0123456789abcdef" for character in revision_sha256)
        ):
            raise ValueError("catalog_sha256 must be a lowercase SHA-256")
        capture_shaders = {
            (
                int(shader.get("shaderResourceId", 0)),
                shader.get("stage", ""),
                shader.get("entryPoint", ""),
            ): shader
            for shader in catalog["metadata"].get("shaders", [])
        }
        rows = self.store.list_aliases(capture_id, verified_only=True)
        aliases: list[dict] = []
        grouped: dict[tuple[int, str, str], dict] = {}
        for row in rows:
            metadata = row["metadata"]
            key = (
                int(row["shader_resource_id"]),
                row["stage"],
                row.get("entry_point", ""),
            )
            capture_shader = capture_shaders.get(key)
            if capture_shader is None:
                continue
            raw_sha256 = str(metadata.get("captureRawSha256", "") or "")
            code_sha256 = str(metadata.get("captureCodeSha256", "") or "")
            if not (
                metadata.get("programVerified") is True
                and row["provenance"] in ("program", "rule:program")
                and raw_sha256
                and raw_sha256 == capture_shader.get("rawSha256", "")
                and metadata.get("captureId") == capture_id
                and metadata.get("stage") == key[1]
                and metadata.get("entryPoint", "") == key[2]
            ):
                continue
            alias = {
                "captureId": capture_id,
                "shaderResourceId": key[0],
                "stage": key[1],
                "entryPoint": key[2],
                "rawSha256": raw_sha256,
                "codeSha256": code_sha256,
                "bindingKind": row["binding_kind"],
                "registerSpace": int(row["register_space"]),
                "bindNumber": int(row["bind_number"]),
                "byteOffset": int(row["byte_offset"]),
                "canonicalPath": row["canonical_path"],
                "alias": row["alias"],
                "originalName": row["original_name"],
                "provenance": row["provenance"],
                "confidence": row["confidence"],
                "verified": bool(row["verified"]),
                "metadata": metadata,
            }
            aliases.append(alias)
            group = grouped.setdefault(
                key,
                {
                    "shaderResourceId": key[0],
                    "stage": key[1],
                    "entryPoint": key[2],
                    "rawSha256": raw_sha256,
                    "codeSha256": code_sha256,
                    "aliases": [],
                },
            )
            group["aliases"].append(alias)
            if alias["bindingKind"] == "shader":
                group["shaderAlias"] = alias["alias"]
                group["verified"] = alias["verified"]

        value = {
            "schema": ALIAS_SCHEMA,
            "schemaVersion": ALIAS_VERSION,
            "captureId": capture_id,
            "catalogSha256": revision_sha256,
            "captureCatalog": catalog["catalog_path"],
            "verifiedOnly": True,
            "aliases": aliases,
            "shaders": list(grouped.values()),
            "symbolRules": [
                rule
                for rule in (self.store.list_symbol_rules(scan_id) if scan_id else [])
                if rule.get("program_verified")
            ],
        }
        path = Path(output_path).expanduser().resolve()
        _atomic_json(path, value)
        return {
            "schema": ALIAS_SCHEMA,
            "schemaVersion": ALIAS_VERSION,
            "captureId": capture_id,
            "catalogSha256": revision_sha256,
            "path": str(path),
            "aliasCount": len(aliases),
            "shaderCount": len(grouped),
            "verifiedOnly": True,
        }
