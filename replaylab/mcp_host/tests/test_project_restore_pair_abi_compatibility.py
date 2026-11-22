"""Pair apply may retain old report hashes while signing canonical topology."""
import copy
import json
from pathlib import Path

import pytest

from src import project_restore
from src.shader_workspace.abi import parse_shader_abi
from tests import test_project_restore as restore_fixtures
from tests.test_project_restore_source_admission import PLATFORMS
from tests.test_shader_abi import complete_interface


@pytest.fixture(params=PLATFORMS, ids=[row[0] + "-" + row[2] for row in PLATFORMS])
def pair_case(request):
    owner = restore_fixtures.ProjectRestoreTests()
    owner.setUp()
    try:
        api, capture_api, encoding, unused_language = request.param
        def make(legacy_first=True):
            current = complete_interface("fragment")
            legacy = copy.deepcopy(current)
            legacy["outputTopology"] = ""
            baseline, actual = (legacy, current) if legacy_first else (current, legacy)
            old_hash, live_hash = parse_shader_abi(baseline).sha256, parse_shader_abi(actual).sha256
            status = copy.deepcopy(owner.workspace_status)
            status["capture"]["api"] = capture_api
            status["baseline"].update(abiSha256=old_hash, compiledInterface=baseline, kind=encoding)
            status["latestCompiled"]["kind"] = encoding
            host = restore_fixtures._Host(owner.active, status, owner.capture_path, owner.session_id,
                         None, owner.identity_evidence)
            host.candidate_abi_sha256 = live_hash
            workspace = Path(status["workspacePath"])
            wrapped = json.loads(host.apply_shader_workspace_shared_resource(
                str(workspace), status["revision"], owner.candidate_sha, "7" * 32,
                expected_anchor_event_id=9))
            fresh = encoding == "GLSL"
            wrapped["binding"].update(api=api, encoding=encoding.lower())
            wrapped["validation"].update(
                baselineAbiSha256=live_hash, validationScope="verified-shader-pair",
                reusedValidation=True, captureSessionId=owner.session_id,
                freshPhysicalValidationPerformed=fresh)
            replacement = wrapped["replacement"]
            replacement.update(api=api, encoding=encoding.lower(), physicalAbiSha256=live_hash,
                               abiValidationMode="verified-shader-pair", targetBuiltOnce=True,
                               freshPhysicalAbiValidationPerformed=fresh)
            payload = {key: replacement[key] for key in (
                "originalRawSha256", "candidateRawSha256", "physicalAbiSha256",
                "api", "stage", "entryPoint", "encoding")}
            payload.update(schema="replaylab-validated-shader-restore/v1", captureSha256="8" * 64)
            replacement["restoreValidationReceipt"] = {"payload": payload, "signature": "9" * 64}
            def validate(value):
                return project_restore._validate_workspace_apply_response(
                    host, workspace, status, value, operation_id="7" * 32,
                    expected_revision=status["revision"], expected_candidate_raw_sha256=owner.candidate_sha,
                    expected_anchor_event_id=9, capture_session_id=owner.session_id)
            return status, host, wrapped, validate
        yield make
    finally:
        owner.tearDown()


@pytest.mark.parametrize("legacy_first", [True, False])
def test_pair_receipt_and_current_physical_digest_keep_immutable_report_hash(pair_case, legacy_first):
    status, host, wrapped, validate = pair_case(legacy_first)
    before = copy.deepcopy((status, wrapped, host.workspace_service._status))
    assert validate(wrapped) is wrapped
    assert (status, wrapped, host.workspace_service._status) == before
    assert wrapped["binding"]["baselineAbiSha256"] != wrapped["validation"]["baselineAbiSha256"]


@pytest.mark.parametrize("field,value", [
    ("validation.baselineAbiSha256", "1" * 64),
    ("binding.baselineAbiSha256", "1" * 64),
    ("replacement.physicalAbiSha256", "1" * 64),
    ("replacement.restoreValidationReceipt.payload.physicalAbiSha256", "1" * 64),
    ("replacement.restoreValidationReceipt.payload.originalRawSha256", "1" * 64),
    ("replacement.restoreValidationReceipt.payload.candidateRawSha256", "1" * 64),
    ("replacement.originalRawSha256", "1" * 64),
    ("replacement.abiReportSha256", "1" * 64),
])
def test_pair_alias_does_not_waive_any_bound_digest(pair_case, field, value):
    unused_status, unused_host, wrapped, validate = pair_case()
    target = wrapped
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    with pytest.raises(RuntimeError, match="invalid live ABI"):
        validate(wrapped)


def test_pair_alias_requires_exact_agreement_between_validation_replacement_and_receipt(pair_case):
    status, unused_host, wrapped, validate = pair_case()
    # Both digests are valid aliases, but a response cannot mix which one the
    # signed proof actually authenticates.
    wrapped["replacement"]["restoreValidationReceipt"]["payload"]["physicalAbiSha256"] = status["baseline"]["abiSha256"]
    with pytest.raises(RuntimeError, match="invalid live ABI"):
        validate(wrapped)


def test_ordinary_report_still_requires_exact_historical_baseline(pair_case):
    unused_status, unused_host, wrapped, validate = pair_case()
    wrapped["validation"].pop("validationScope")
    with pytest.raises(RuntimeError, match="invalid live ABI"):
        validate(wrapped)
