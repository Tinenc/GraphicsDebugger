"""Recover missing reflection from immutable captured bytes, never from a draft."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .abi import (build_candidate_reflection_request, parse_shader_abi,
                  validate_candidate_reflection_response)
from .catalog import ABI_STATUS_CAPTURED_COMPLETE, MAX_CATALOG_BYTES, load_selected_shader
from .errors import AbiValidationError
from .io import atomic_write_json, read_json_object
from ..symbols.scanner import interface_hash, interface_is_complete, layout_hash


def reflection_request(service, root, manifest, interface_sha256=None):
    baseline, capture = manifest["baseline"], manifest["capture"]
    # Only the params go to RenderDoc. The temporary hash binding is replaced by
    # the validated reflected ABI below; this request grants no apply authority.
    return build_candidate_reflection_request(
        workspace_id=manifest["workspaceId"], capture_id=capture["captureId"],
        api=capture["api"], candidate_path=service._artifact(root, baseline["path"]),
        candidate_sha256=baseline["rawSha256"], candidate_byte_size=baseline["byteSize"],
        candidate_kind=baseline["kind"], revision=max(1, manifest["revision"]),
        stage=manifest["stage"], entry_point=manifest["entryPoint"],
        baseline_abi_sha256=interface_sha256 or baseline["rawSha256"],
        target_profile=manifest["targetProfile"]["profile"],
    )


def recover(service, root, manifest, request, response):
    if manifest["baseline"].get("abiStatus") != "unavailable":
        raise AbiValidationError("baseline reflection recovery requires an unavailable baseline")
    if reflection_request(service, root, manifest) != request:
        raise AbiValidationError("workspace changed during baseline reflection recovery")
    if response.get("captureId") != manifest["capture"]["captureId"]:
        raise AbiValidationError("baseline reflection was not bound to the capture")
    abi = parse_shader_abi(response.get("compiledInterface"),
                           expected_stage=manifest["stage"],
                           expected_entry_point=manifest["entryPoint"])
    verified_request = reflection_request(service, root, manifest, abi.sha256)
    # Reuse all native proof checks: exact file/raw/reflected hashes, API/encoding,
    # entry, byte count, successful target build/reflection and target release.
    validate_candidate_reflection_response(verified_request, response, abi)
    capture = manifest["capture"]
    previous_catalog = Path(capture["catalogPath"])
    if hashlib.sha256(previous_catalog.read_bytes()).hexdigest() != capture["catalogSha256"]:
        raise AbiValidationError("captured catalog changed before baseline reflection recovery")
    catalog = read_json_object(previous_catalog, MAX_CATALOG_BYTES, "captured Shader catalog")
    if hashlib.sha256(previous_catalog.read_bytes()).hexdigest() != capture["catalogSha256"]:
        raise AbiValidationError("captured catalog changed during baseline reflection recovery")
    raw_interface = deepcopy(response["compiledInterface"])
    selected = [s for s in catalog["shaders"]
                if s.get("shaderKey") == manifest["shader"]["shaderKey"]]
    if len(selected) != 1:
        raise AbiValidationError("baseline reflection did not select one catalog Shader")
    interface_keys = ("interfaceComplete", "inputSignature", "outputSignature",
                      "readOnlyResources", "readWriteResources", "samplers",
                      "constantBlocks", "dynamicLinkageInterfaceCount",
                      "outputTopology", "threadGroupSize")
    for key in interface_keys:
        selected[0][key] = deepcopy(raw_interface[key])
    selected[0]["constantBuffers"] = deepcopy(raw_interface["constantBlocks"])
    selected[0]["layoutHash"] = layout_hash(selected[0])
    selected[0]["interfaceHash"] = interface_hash(selected[0])
    if not interface_is_complete(selected[0]):
        raise AbiValidationError("recovered interface is incomplete for the project catalog")
    for use in catalog.get("uses", []):
        if use.get("shaderKey") != manifest["shader"]["shaderKey"]:
            continue
        bindings = {(b.get("space"), b.get("bind")): b.get("binding")
                    for b in use.get("constantBlocks", [])}
        use["constantBlocks"] = deepcopy(raw_interface["constantBlocks"])
        for block in use["constantBlocks"]:
            binding = bindings.get((block.get("space"), block.get("bind")))
            if binding is not None:
                block["binding"] = binding
    # A sibling revision preserves relative immutable blob paths. Never replace
    # the old catalog: older receipts continue referring to their original bytes.
    catalog_digest = hashlib.sha256(json.dumps(catalog, sort_keys=True, ensure_ascii=False,
                                             separators=(",", ":")).encode("utf-8")).hexdigest()
    recovered_catalog = previous_catalog.with_name("catalog-baseline-%s.json" % catalog_digest)
    atomic_write_json(recovered_catalog, catalog)
    verified = load_selected_shader(recovered_catalog, manifest["shader"]["shaderKey"])
    if (verified.blob != service._artifact(root, manifest["baseline"]["path"]).read_bytes()
            or verified.capture_id != capture["captureId"]
            or verified.api != capture["api"] or verified.abi is None
            or verified.abi.sha256 != abi.sha256):
        raise AbiValidationError("recovered baseline catalog identity changed")
    receipt = {
        "schema": "bread-shader-baseline-reflection-recovery/v1",
        "workspaceId": manifest["workspaceId"], "revision": manifest["revision"],
        "baselineRawSha256": manifest["baseline"]["rawSha256"],
        "previousCatalogPath": str(previous_catalog),
        "previousCatalogSha256": capture["catalogSha256"],
        "catalogPath": str(recovered_catalog),
        "catalogSha256": hashlib.sha256(recovered_catalog.read_bytes()).hexdigest(),
        "baselineAbiSha256": abi.sha256,
        "sourcePreserved": True, "candidateCompiled": False, "candidateApplied": False,
    }
    capture.update(catalogPath=str(recovered_catalog), catalogSha256=receipt["catalogSha256"])
    manifest["baseline"].update(abiStatus=ABI_STATUS_CAPTURED_COMPLETE,
                                abiSha256=abi.sha256, compiledInterface=abi.to_dict())
    manifest["lastGood"] = deepcopy(manifest["baseline"])
    candidate = manifest.get("latestCompiled")
    if candidate:
        candidate["abiGate"] = {"status": "pending-replay-reflection",
                                "baselineAbiSha256": abi.sha256}
        candidate.update(validationScope="container-structure-only", requiresReplayAbiValidation=True,
                         replayAbiValidated=False, abiCompatible=False,
                         semanticEquivalenceVerified=False)
        service._validate_latest_candidate(root, manifest, manifest["revision"], abi)
    manifest.pop("lastAbiAttempt", None)
    manifest["baselineInterfaceRecovery"] = receipt
    service._append_history(root, manifest, {
        "operation": "baseline-reflection-recovery", "revision": manifest["revision"],
        "status": "source-and-candidate-preserved", "recovery": receipt,
    })
    return receipt
