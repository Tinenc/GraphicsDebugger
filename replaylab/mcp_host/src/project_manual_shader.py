"""Source/binary provenance for successful manual edits, never ABI authority."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

SCOPE = "manual-edit-unvalidated"
RECEIPT_SCHEMA = "bread-manual-shader-publication-receipt/v1"


def _sha(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("manual Shader provenance has an invalid hash")
    return value


def receipt_identity(value: Any) -> dict[str, Any]:
    fields = {"schema", "schemaVersion", "validationScope", "manualEdit", "replayAbiValidated",
              "originalRawSha256", "candidateRawSha256", "sourceSha256", "sourceRevision",
              "stage", "entryPoint", "targetProfile", "api", "encoding"}
    if (not isinstance(value, Mapping) or set(value) != fields
            or value.get("schema") != RECEIPT_SCHEMA or value.get("schemaVersion") != 1
            or value.get("validationScope") != SCOPE or value.get("manualEdit") is not True
            or value.get("replayAbiValidated") is not False
            or type(value.get("sourceRevision")) is not int or value["sourceRevision"] <= 0
            or not isinstance(value.get("targetProfile"), Mapping)
            or any(not isinstance(value.get(k), str) or not value[k] for k in ("stage", "api", "encoding"))
            or not isinstance(value.get("entryPoint"), str)):
        raise ValueError("manual Shader publication provenance is invalid")
    for key in ("originalRawSha256", "candidateRawSha256", "sourceSha256"):
        _sha(value[key])
    return copy.deepcopy(dict(value))


def from_apply(result: Mapping[str, Any], status: Mapping[str, Any]) -> dict[str, Any]:
    validation, binding = result.get("validation") or {}, result.get("binding") or {}
    replacement, latest = result.get("replacement") or {}, status.get("latestCompiled") or {}
    source = status.get("source") or {}
    if (result.get("applied") is not True or result.get("scope") != "captured-resource"
            or validation.get("manualEdit") is not True or validation.get("replayAbiValidated") is not False
            or validation.get("validationScope") != SCOPE
            or replacement.get("abiValidationMode") != "manual-fast"
            or replacement.get("originalShaderPreserved") is not True
            or replacement.get("resourceScoped") is not True or replacement.get("eventScoped") is not False
            or binding.get("revision") != status.get("revision")
            or binding.get("candidateRawSha256") != latest.get("rawSha256")
            or binding.get("sourceSha256") != source.get("currentSha256")
            or replacement.get("candidateRawSha256") != latest.get("rawSha256")
            or replacement.get("originalRawSha256") != status.get("baseline", {}).get("rawSha256")
            or replacement.get("originalResourceId") != status.get("shader", {}).get("resourceId")
            or replacement.get("stage") != status.get("stage")
            or replacement.get("entryPoint", "") != status.get("entryPoint", "")):
        raise ValueError("manual Shader publication differs from successful apply")
    return receipt_identity({"schema": RECEIPT_SCHEMA, "schemaVersion": 1,
        "validationScope": SCOPE, "manualEdit": True, "replayAbiValidated": False,
        "originalRawSha256": replacement["originalRawSha256"],
        "candidateRawSha256": latest["rawSha256"], "sourceSha256": source["currentSha256"],
        "sourceRevision": status["revision"], "stage": status["stage"],
        "entryPoint": status.get("entryPoint", ""), "targetProfile": status["targetProfile"],
        "api": replacement["api"], "encoding": replacement["encoding"]})


def compiled_identity(value: Mapping[str, Any], schema: str, version: int) -> dict[str, Any]:
    fields = {"schema", "schemaVersion", "rawSha256", "codeSha256", "byteSize", "kind",
              "sourceSha256", "sourceRevision", "targetProfile", "originBaselineAbiSha256",
              "validationScope", "semanticEquivalenceVerified", "manualApplyReceipt"}
    if (set(value) - {"artifactPath"} != fields or value.get("schema") != schema
            or value.get("schemaVersion") != version or value.get("validationScope") != SCOPE
            or value.get("semanticEquivalenceVerified") is not False
            or type(value.get("byteSize")) is not int or value["byteSize"] <= 0
            or not isinstance(value.get("kind"), str) or not value["kind"]):
        raise ValueError("manual project Shader candidate is invalid")
    receipt = receipt_identity(value["manualApplyReceipt"])
    for key in ("rawSha256", "codeSha256", "sourceSha256", "originBaselineAbiSha256"):
        _sha(value[key])
    if (value["rawSha256"] != receipt["candidateRawSha256"]
            or value["sourceSha256"] != receipt["sourceSha256"]
            or value["sourceRevision"] != receipt["sourceRevision"]
            or value["targetProfile"] != receipt["targetProfile"]):
        raise ValueError("manual project Shader candidate provenance differs")
    return {key: copy.deepcopy(value[key]) for key in fields}


def candidate_from_status(status: Mapping[str, Any], *, source_bytes: bytes,
                          workspace_root: Path, manifest_bytes: bytes,
                          baseline: Mapping[str, Any], schema: str, version: int):
    receipt = receipt_identity(status["manualApplyReceipt"])
    latest = status.get("latestCompiled") or {}
    manifest = json.loads(manifest_bytes.decode("utf8"))
    persisted = manifest.get("latestCompiled") or {}
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    if (receipt["originalRawSha256"] != baseline.get("rawSha256")
            or receipt["stage"] != status.get("stage")
            or receipt["entryPoint"] != status.get("entryPoint", "")
            or receipt["targetProfile"] != status.get("targetProfile")
            or receipt["sourceRevision"] != status.get("revision")
            or receipt["sourceSha256"] != source_hash
            or receipt["sourceSha256"] != latest.get("sourceSha256")
            or receipt["candidateRawSha256"] != latest.get("rawSha256")
            or latest.get("revision") != status.get("revision")
            or manifest.get("workspaceId") != status.get("workspaceId")
            or manifest.get("revision") != status.get("revision")
            or any(persisted.get(k) != latest.get(k) for k in
                   ("revision", "blobPath", "sourcePath", "sourceSha256", "rawSha256", "kind"))):
        raise ValueError("manual publication source/candidate identity changed")
    def artifact(relative, maximum):
        path = workspace_root / str(relative or "")
        if path.is_symlink() or not path.resolve().is_relative_to(workspace_root) or path.stat().st_size > maximum:
            raise ValueError("manual publication artifact is redirected or oversized")
        return path.read_bytes()
    if artifact(latest["sourcePath"], 64 * 1024 * 1024) != source_bytes:
        raise ValueError("manual publication compiled source changed")
    binary = artifact(latest["blobPath"], 512 * 1024 * 1024)
    if hashlib.sha256(binary).hexdigest() != receipt["candidateRawSha256"] or len(binary) != latest.get("byteSize"):
        raise ValueError("manual publication candidate bytes changed")
    identity = compiled_identity({"schema": schema, "schemaVersion": version,
        "rawSha256": receipt["candidateRawSha256"], "codeSha256": latest["codeSha256"],
        "byteSize": len(binary), "kind": latest["kind"], "sourceSha256": source_hash,
        "sourceRevision": status["revision"], "targetProfile": status["targetProfile"],
        "originBaselineAbiSha256": baseline["abiSha256"], "validationScope": SCOPE,
        "semanticEquivalenceVerified": False, "manualApplyReceipt": receipt}, schema, version)
    return identity, binary
