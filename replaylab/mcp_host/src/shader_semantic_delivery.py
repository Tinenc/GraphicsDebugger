"""Source-bound CBuffer display delivery facts for a Shader organization.

This is a bounded local receipt read, not a semantic quality score or a replay
validation. Source review and display publication are independent effects.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping


RECEIPT_PATH = "ai/cbuffer-name-publication.json"
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
DISPLAY_IDENTITY_PATH = "ai/cbuffer-display-identity.json"


def save_display_identity(workspace: str, identity: Mapping[str, Any]) -> None:
    """Persist a Host/Bridge observation, never an Agent assertion or apply proof."""
    from .shader_workspace.io import atomic_write_json
    root = Path(workspace).resolve()
    path = root / DISPLAY_IDENTITY_PATH
    if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("CBuffer display identity path is redirected")
    fields = ("status", "originalRawSha256", "activeDisplayShaderRawSha256",
              "activeReplacementShaderRawSha256", "captureSessionId", "stage", "entryPoint",
              "mutationEpoch", "reason", "refreshTool")
    value = {key: identity[key] for key in fields if key in identity}
    value.update(schema="bread-cbuffer-display-identity/v1", displayOnly=True)
    if path.is_file():
        try:
            if path.stat().st_size <= 16384 and json.loads(path.read_text(encoding="utf-8")) == value:
                return
        except (OSError, ValueError):
            pass
    atomic_write_json(path, value)


def load_display_identity(workspace: str) -> dict[str, Any]:
    from .shader_organization_review import _read_bytes
    try:
        _, raw = _read_bytes(Path(workspace).resolve(), DISPLAY_IDENTITY_PATH, 16384)
        value = json.loads(raw)
        if isinstance(value, dict) and value.get("schema") == "bread-cbuffer-display-identity/v1":
            return value
    except (OSError, ValueError):
        pass
    return {"status": "refresh-required", "refreshTool": "publish_cbuffer_aliases"}


def delivery_state(workspace: str, source_sha256: str, contract: Mapping[str, Any], *, source_path: str = "") -> dict[str, Any]:
    if source_path:
        from .shader_organization_review import _read_bytes
        from .shader_workspace.naming_delivery import semantic_delivery, source_context
        from .shader_resource_naming_review import aggregate
        import hashlib
        root = Path(workspace).resolve()
        _, source = _read_bytes(root, source_path, 4 * 1024 * 1024)
        if hashlib.sha256(source).hexdigest() != source_sha256:
            raise ValueError("Shader source changed during naming delivery check")
        context = source_context(root, source, source_path)
        derived = semantic_delivery(source, context=context)
        effective = {**contract, "required": bool(contract.get("cbufferRequired", contract.get("required")) or derived["required"])}
        cbuffer = delivery_state(workspace, source_sha256, effective)
        if not context["complete"]:
            cbuffer.update(status="pending", required=True, reason="naming-source-dependencies-unavailable",
                           diagnostic=context.get("diagnostic", ""), recompileRequired=True,
                           nextAction="repair-or-save-include-closure-and-compile-current-source")
        elif cbuffer.get("required"):
            try:
                _, receipt_raw = _read_bytes(root, RECEIPT_PATH, MAX_RECEIPT_BYTES)
                receipt = json.loads(receipt_raw)
                if receipt.get("sourceDependenciesDigest", "") != context["sourceDependenciesDigest"]:
                    cbuffer.update(status="pending", reason="publication-source-dependencies-stale")
            except (OSError, ValueError, TypeError, AttributeError):
                pass  # The ordinary receipt validation already reports this failure.
        return aggregate(cbuffer, root, source, context)
    result: dict[str, Any] = {
        "required": contract.get("required") is True,
        "sourceSha256": source_sha256,
        "receiptRelativePath": RECEIPT_PATH,
        "status": "pending",
    }
    if not result["required"]:
        return {**result, "status": "not-applicable"}
    # Share the existing regular-file/containment boundary, and never follow a
    # receipt-supplied path or perform expensive capture discovery here.
    from .shader_organization_review import _read_bytes
    root = Path(workspace).resolve()
    candidate_sha256 = str(contract.get("candidateRawSha256") or "")
    original_sha256 = ""
    # Keep the latest compiled identity as diagnostic information. It is never
    # evidence of which executable or reflection the UI currently displays.
    manifest_path = root / "manifest.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        try:
            _, manifest_raw = _read_bytes(root, "manifest.json", 16 * 1024 * 1024)
            manifest = json.loads(manifest_raw)
            if not isinstance(manifest, Mapping):
                raise ValueError("Shader manifest is not an object")
            latest = manifest.get("latestCompiled")
            original_sha256 = str((manifest.get("baseline") or {}).get("rawSha256") or "")
            if isinstance(latest, Mapping) and latest.get("sourceSha256") == source_sha256:
                candidate = latest.get("rawSha256")
                candidate_sha256 = candidate if re.fullmatch(r"[0-9a-f]{64}", str(candidate or "")) else ""
        except (OSError, ValueError, TypeError):
            return {**result, "reason": "current-original-identity-unavailable"}
    try:
        _, raw = _read_bytes(root, RECEIPT_PATH, MAX_RECEIPT_BYTES)
        receipt = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return {**result, "reason": "publication-receipt-missing"}
    return {**result, **publication_state(receipt, source_sha256, candidate_sha256,
        display_identity=load_display_identity(workspace), original_shader_sha256=original_sha256)}


def replacement_candidate_sha256(receipt: Mapping[str, Any]) -> str:
    """Separate the checked replacement owner from the UI's reflected layout.

    New receipts explicitly record no owner as an empty string. Older receipts
    prove a replacement only when their displayed program differs from the
    captured original; an original reflection is never apply evidence.
    """
    if "activeReplacementShaderRawSha256" in receipt:
        value = receipt.get("activeReplacementShaderRawSha256")
    else:
        value = receipt.get("activeDisplayShaderRawSha256")
        original = str(receipt.get("shaderRawSha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", original) or value == original:
            return ""
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else ""


def publication_state(receipt: Mapping[str, Any] | None, source_sha256: str,
                      candidate_sha256: str = "", *,
                      display_identity: Mapping[str, Any] | None = None,
                      original_shader_sha256: str = "") -> dict[str, Any]:
    """Shared source/display receipt checks for tool delivery and continuation."""
    result: dict[str, Any] = {"status": "pending", "sourceSha256": source_sha256,
                            "displayOnly": True, "shaderExecutionModified": False}
    if re.fullmatch(r"[0-9a-f]{64}", candidate_sha256):
        result["candidateRawSha256"] = candidate_sha256
    if not isinstance(receipt, Mapping) or receipt.get("schema") != "replaylab-cbuffer-name-publication-receipt/v1":
        return {**result, "reason": "publication-receipt-missing"}
    published_sha = receipt.get("semanticSourceSha256")
    if not re.fullmatch(r"[0-9a-f]{64}", str(published_sha or "")) or published_sha != source_sha256:
        return {**result, "reason": "publication-source-stale",
                "publishedSourceSha256": str(published_sha or "")[:64]}
    original = str(receipt.get("shaderRawSha256") or "")
    display = str(receipt.get("activeDisplayShaderRawSha256") or "")
    replacement = replacement_candidate_sha256(receipt)
    if (not re.fullmatch(r"[0-9a-f]{64}", original) or
            not re.fullmatch(r"[0-9a-f]{64}", display) or display not in (original, replacement) or
            (original_shader_sha256 and original != original_shader_sha256)):
        return {**result, "reason": "publication-display-target-stale"}
    if "activeReplacementShaderRawSha256" in receipt and receipt.get("activeReplacementShaderRawSha256") not in ("", replacement):
        return {**result, "reason": "publication-display-target-stale"}
    # Compiling a candidate does not change the CBuffer panel. Only an actual
    # display observation can require a mirror for a different reflected layout.
    result.update(activeDisplayShaderRawSha256=display,
                  displayIdentityStatus="published" if display_identity is None else "current")
    if display_identity is not None:
        if display_identity.get("status") != "current":
            return {**result, "reason": "publication-display-identity-refresh-required",
                    "displayIdentityStatus": "refresh-required", "recompileRequired": False,
                    "nextAction": "refresh-display-identity-or-republish-semantic-fields",
                    "tool": "publish_cbuffer_aliases"}
        if (display_identity.get("originalRawSha256") != original or
                display_identity.get("activeDisplayShaderRawSha256") != display):
            return {**result, "reason": "publication-display-target-stale"}
        observed_owner = display_identity.get("activeReplacementShaderRawSha256", "")
        if display != original and (observed_owner != display or replacement != display):
            return {**result, "reason": "publication-display-target-stale"}
    counts = receipt.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}
    keys = ("requestedDisplaySemanticFields", "writtenDisplaySemanticFields", "keptUserDisplaySemanticFields")
    if any(type(counts.get(key)) is not int or not 0 <= counts[key] <= 1_000_000 for key in keys):
        return {**result, "reason": "display-outcome-unavailable"}
    result["counts"] = {key: counts[key] for key in keys}
    requested, written, kept = (counts[key] for key in keys)
    acceptance = receipt.get("acceptance")
    acceptance = acceptance if isinstance(acceptance, Mapping) else {}
    requested_source = counts.get("requestedSemanticFields")
    active_shader = str(receipt.get("activeDisplayShaderRawSha256") or "")
    original_is_displayed = (re.fullmatch(r"[0-9a-f]{64}", active_shader) is not None
                            and active_shader == receipt.get("shaderRawSha256"))
    from .shader_workspace.semantic_coverage import coverage_state
    coverage = coverage_state(receipt)
    reviewed_physical_only = (coverage["complete"] and requested_source == 0
                             and ((coverage.get("unresolvedLaneCount") or 0)
                                  + (coverage.get("reviewedUnusedLaneCount") or 0)) > 0)
    # A typed original needs no replacement mirror: its semantic fields are
    # counted by requestedSemanticFields. Zero display/mirror rows cannot be
    # accepted for a different active replacement, however.
    accounted = ((requested > 0 or original_is_displayed or reviewed_physical_only) and written + kept == requested
                 and type(requested_source) is int and (requested_source > 0 or reviewed_physical_only)
                 and acceptance.get("requestedSemanticFieldsAccountedFor") is True
                 and acceptance.get("currentDisplaySemanticFieldsAccountedFor") is True)
    if receipt.get("applied") is not True or receipt.get("partial") is not False or not accounted:
        return {**result, "reason": "display-publication-incomplete"}
    result["bindingCoverage"] = coverage
    if not coverage["complete"]:
        return {**result, "reason": coverage["reason"]}
    return {**result, "status": "complete", "reason": "current-source-display-published"}
