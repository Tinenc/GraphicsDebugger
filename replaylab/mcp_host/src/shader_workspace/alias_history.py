"""Transaction-local pruning of obsolete AI views across reflected layouts.

Renaming reflected members can change a layout file's key even when the captured
Shader and binding remain the same. These helpers discover and stage only those
old manifests; the publication transaction owns commit, rollback and page GC.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import aliases, alias_field_storage
from .io import atomic_write_bytes

MAX_HISTORY_LAYOUTS = 4096


def _scope_key(scope: Any) -> tuple | None:
    if not isinstance(scope, Mapping):
        return None
    return tuple(scope.get(field) for field in (
        "shaderRawSha256", "stage", "bind", "space", "layoutSha256"))


def _read_manifest(path: Path) -> dict[str, Any]:
    if alias_field_storage._redirected(path) or alias_field_storage._redirected(path.parent):
        raise ValueError("CBuffer history manifest is redirected")
    payload = aliases._read_alias_payload(path)
    members = payload.get("members")
    if (payload.get("schema") != aliases.SCHEMA or payload.get("layoutSha256") != path.stem
            or not isinstance(members, list)
            or not all(isinstance(member, Mapping) for member in members)
            or aliases.layout_sha256(str(payload.get("block") or ""), members) != path.stem):
        raise ValueError("CBuffer history manifest does not match its layout identity")
    for field in ("semanticViews", "aliases", "blockAliases"):
        records = payload.get(field, [])
        if not isinstance(records, list) or not all(isinstance(record, Mapping) for record in records):
            raise ValueError("CBuffer history records must be object arrays")
    for view in payload.get("semanticViews", []):
        # Validate references without loading unchanged user/history fields.
        # Raw records are preserved on write, including unknown extension data.
        aliases.normalise_shader_scope(view.get("shaderScope"), expected_layout_sha256=path.stem)
        if "fieldChunks" in view:
            alias_field_storage.page_descriptors(view)
        if isinstance(view.get("usageReview"), Mapping):
            alias_field_storage.usage_page_descriptors(view["usageReview"])
    return payload


def _prune_payload(payload: Mapping[str, Any], policies: list[Mapping[str, Any]]) -> tuple[dict, dict]:
    views = list(payload.get("semanticViews", []))
    removed = []
    for policy in policies:
        retention = policy["semanticRetention"]
        views, obsolete = aliases.prune_semantic_history(
            views, shader_scope=policy["shaderScope"],
            captured_shader_sha256=policy["semanticCapturedShaderSha256"],
            protected_shader_sha256=retention["protectedShaderRawSha256"],
            keep_previous=retention.get("keepPrevious", 0))
        removed.extend(obsolete)
    remaining_scopes = {_scope_key(view.get("shaderScope")) for view in views}
    obsolete_scopes = {_scope_key(view.get("shaderScope")) for view in removed} - remaining_scopes

    def keep(record):
        return record.get("source") != "ai" or _scope_key(record.get("shaderScope")) not in obsolete_scopes

    result = dict(payload)
    entries = list(payload.get("aliases", []))
    blocks = list(payload.get("blockAliases", []))
    result["aliases"] = [row for row in entries if keep(row)]
    if "blockAliases" in payload:
        result["blockAliases"] = [row for row in blocks if keep(row)]
    if "semanticViews" in payload:
        result["semanticViews"] = views
    return result, {"removedViews": len(removed),
                    "removedAliases": len(entries) - len(result["aliases"]),
                    "removedBlockAliases": len(blocks) - len(result.get("blockAliases", [])),
                    "remainingViews": len(views)}


def discover_history_publications(
    root: Path, publications: list[Mapping[str, Any]], *,
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return old-layout cleanup targets while the caller holds the alias lock.

    Only authoritative zero-rollback policies participate. A conservative caller
    retaining one unpinned predecessor remains confined to its current layout,
    avoiding an accidental predecessor per historical layout.
    """
    from .semantic_usage import clean_view_fields

    root = Path(root)
    policies, targeted = [], set()
    for publication in publications:
        targeted.add(aliases.layout_sha256(str(publication.get("block") or ""),
                                           list(publication.get("members") or [])))
        retention = publication.get("semanticRetention")
        if not isinstance(retention, Mapping) or retention.get("keepPrevious", 0) != 0:
            continue
        if not publication.get("semanticFields") and "semanticUsageReview" not in publication:
            continue
        clean_view_fields(publication.get("semanticFields") or [], publication.get("semanticUsageReview"))
        policy = {"shaderScope": aliases.normalise_shader_scope(publication["shaderScope"]),
                  "semanticCapturedShaderSha256": publication.get("semanticCapturedShaderSha256", ""),
                  "semanticRetention": dict(retention)}
        # Validate the trusted retention list before discovering any mutation.
        aliases.prune_semantic_history(
            [], shader_scope=policy["shaderScope"],
            captured_shader_sha256=policy["semanticCapturedShaderSha256"],
            protected_shader_sha256=retention.get("protectedShaderRawSha256"), keep_previous=0)
        policies.append(policy)
    if not policies or not root.is_dir():
        return []
    if alias_field_storage._redirected(root):
        raise ValueError("CBuffer history root is redirected")

    def deferred(path: Path, reason: str) -> None:
        if diagnostics is not None:
            diagnostics.append({"layoutSha256": path.stem, "reason": reason[:400]})

    # Every view published together is current, including captured and active
    # reflection mirrors. A policy for one must not remove another's names.
    published_hashes = {policy["shaderScope"]["shaderRawSha256"] for policy in policies}
    for policy in policies:
        policy["semanticRetention"] = {
            "protectedShaderRawSha256": sorted(set(policy["semanticRetention"]["protectedShaderRawSha256"])
                                               | published_hashes), "keepPrevious": 0}
    paths = []
    for path in root.glob("*.json"):
        if aliases._SHA256_RE.fullmatch(path.stem) and path.stem not in targeted:
            paths.append(path)
        if len(paths) > MAX_HISTORY_LAYOUTS:
            # Bounded maintenance must not turn a valid naming publication into
            # a failure. No metadata outside the bounded scan is changed.
            deferred(root, "CBuffer history layout scan limit reached")
            return []
    results = []
    for path in sorted(paths):
        adjusted = [{**policy, "shaderScope": {**policy["shaderScope"], "layoutSha256": path.stem}}
                    for policy in policies]
        try:
            if alias_field_storage._redirected(path):
                raise ValueError("CBuffer history manifest is redirected")
            payload = aliases._read_alias_payload(path)
            raw_views = payload.get("semanticViews", [])
            if not isinstance(raw_views, list):
                raise ValueError("CBuffer history semantic views must be an array")
            # A corrupt unrelated layout must not prevent a fresh naming
            # publication. Check lineage before validating a cleanup target;
            # uncertain data is left unchanged and reported for maintenance.
            candidates = [view for view in raw_views if isinstance(view, Mapping)]
            has_obsolete = any(aliases.prune_semantic_history(
                candidates, shader_scope=policy["shaderScope"],
                captured_shader_sha256=policy["semanticCapturedShaderSha256"],
                protected_shader_sha256=policy["semanticRetention"]["protectedShaderRawSha256"],
                keep_previous=0)[1] for policy in adjusted)
            if not has_obsolete:
                continue
            payload = _read_manifest(path)
            unused_payload, cleanup = _prune_payload(payload, adjusted)
        except (OSError, ValueError, TypeError) as exc:
            deferred(path, str(exc))
            continue
        if not cleanup["removedViews"]:
            continue
        results.append({"historyOnly": True, "layoutSha256": path.stem,
                        "block": payload["block"], "members": payload["members"],
                        "historyPolicies": adjusted})
    return results


def prune_history_file(path: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Write a cleanup-only staged manifest; never insert fields or delete pages."""
    path = Path(path)
    if plan.get("historyOnly") is not True or plan.get("layoutSha256") != path.stem:
        raise ValueError("CBuffer history plan does not match its staged layout")
    policies = plan.get("historyPolicies")
    if not isinstance(policies, list) or not policies:
        raise ValueError("CBuffer history plan needs validated retention policies")
    payload = _read_manifest(path)
    for policy in policies:
        aliases.normalise_shader_scope(policy["shaderScope"], expected_layout_sha256=path.stem)
    updated, cleanup = _prune_payload(payload, policies)
    changed = bool(cleanup["removedViews"])
    if changed:
        updated["updatedAt"] = aliases._now()
        serialized = alias_field_storage.json_bytes(updated)
        if len(serialized) > aliases.MAX_FILE_BYTES:
            raise ValueError("CBuffer alias manifest exceeds its bounded size")
        atomic_write_bytes(path, serialized)
    return {"layoutSha256": path.stem, "path": str(path), "block": payload["block"],
            "historyOnly": True, "semanticHistoryCleanup": cleanup, "applied": changed}
