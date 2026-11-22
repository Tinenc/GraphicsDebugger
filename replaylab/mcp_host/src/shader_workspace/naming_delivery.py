"""Cheap source-bound CBuffer presentation delivery facts, without replay reads."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from .outline import _strip_comments
from .errors import ManifestValidationError
from ..shader_semantic_delivery import publication_state


def source_context(workspace, source: bytes, source_path: str = "") -> dict:
    """Read only the already-compiled include closure, never discover files.

    Keep the main source SHA independent from the dependency digest. A changed
    header invalidates naming even when the editor's main file is unchanged.
    """
    result = {"declarationSource": source, "sourceDependenciesDigest": "",
              "complete": True, "documentCount": 1}
    if not re.search(r"(?m)^\s*#\s*include\b", _strip_comments(source.decode("utf-8-sig", errors="replace"))):
        return result
    try:
        from ..shader_organization_review import _read_bytes, canonical_source_path
        from .compile_source_unit import validate_snapshot
        from .source_documents import capture_source_unit, _key, _read
        root = Path(workspace).resolve()
        canonical = source_path or canonical_source_path(str(root))
        main, saved = _read_bytes(root, canonical, 4 * 1024 * 1024)
        if saved != source:
            raise ValueError("Naming source differs from the saved compilation unit")
        _, raw = _read_bytes(root, "manifest.json", 16 * 1024 * 1024)
        manifest = json.loads(raw)
        candidate = manifest.get("latestCompiled") or {}
        proof = candidate.get("sourceDependencies")
        if not isinstance(proof, Mapping) or not proof.get("documents"):
            raise ValueError("The current Shader has no validated include snapshot")
        validate_snapshot(root, main, source, proof)
        allowed = {_key(item["sourcePath"]) for item in proof["documents"]}

        def read_known(path):
            if _key(path) not in allowed:
                raise ValueError("Shader include is not in the compiled source snapshot")
            return _read(path)

        unit = capture_source_unit(main, source_override=source, document_reader=read_known)
        if unit["digest"] != proof["digest"]:
            raise ValueError("Shader include bytes changed after the validated compilation")
        return {**result, "declarationSource": b"\n".join(item["raw"] for item in unit["documents"]),
                "sourceDependenciesDigest": unit["digest"], "documentCount": len(unit["documents"])}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ManifestValidationError) as error:
        return {**result, "complete": False, "diagnostic": str(error)[:400]}


def semantic_delivery(source: bytes, receipt: Mapping | None = None,
                      candidate_sha256: str = "", *, context: Mapping | None = None,
                      display_identity: Mapping | None = None,
                      original_shader_sha256: str = "") -> dict:
    source_sha = hashlib.sha256(source).hexdigest()
    context = context or {"declarationSource": source, "sourceDependenciesDigest": "", "complete":
                          not bool(re.search(br"(?im)^\s*#\s*include\b", source))}
    clean = _strip_comments(context["declarationSource"].decode("utf-8-sig", errors="replace"))
    has_blocks = bool(re.search(
        r"\b(?:cbuffer|tbuffer)\s+\w+\s*(?::[^{};]+)?\{|"
        r"\bConstantBuffer\s*<|\buniform\s+\w+\s*\{", clean))
    receipt = receipt if isinstance(receipt, Mapping) else {}
    required = has_blocks or bool(receipt) or not context["complete"]
    same_source = receipt.get("semanticSourceSha256") == source_sha
    state = publication_state(receipt, source_sha, candidate_sha256,
        display_identity=display_identity, original_shader_sha256=original_shader_sha256)
    dependency_digest = context["sourceDependenciesDigest"]
    if not context["complete"]:
        state = {"status": "pending", "reason": "naming-source-dependencies-unavailable"}
    elif receipt.get("sourceDependenciesDigest", "") != dependency_digest:
        state = {"status": "pending", "reason": "publication-source-dependencies-stale"}
    complete = bool(required and state["status"] == "complete")
    return {
        "schema": "bread-cbuffer-semantic-delivery/v1",
        "required": required,
        "sourceSha256": source_sha,
        "sourceDependenciesDigest": dependency_digest,
        **({"diagnostic": context["diagnostic"]} if context.get("diagnostic") else {}),
        "candidateRawSha256": candidate_sha256,
        "status": "complete" if complete else "pending" if required else "not-applicable",
        "reason": ("current-source-publication" if complete else
                   "no-cbuffer-declarations" if not required else
                   "publication-source-stale" if receipt and not same_source
                   else state.get("reason", "semantic-panel-publication-required")),
        **({"bindingCoverage": state["bindingCoverage"]} if "bindingCoverage" in state else {}),
        **{key: state[key] for key in ("activeDisplayShaderRawSha256", "displayIdentityStatus",
                                      "displayOnly", "shaderExecutionModified") if key in state},
        "receiptRelativePath": "ai/cbuffer-name-publication.json",
        "tool": "publish_cbuffer_aliases",
        "recompileRequired": not context["complete"],
        "nextAction": ("none" if complete or not required else
                       "repair-or-save-include-closure-and-compile-current-source" if not context["complete"] else
                       "refresh-display-identity-or-republish-semantic-fields" if
                       state.get("displayIdentityStatus") == "refresh-required" else
                       "reconcile-and-publish-semantic-fields-without-recompiling"),
    }
