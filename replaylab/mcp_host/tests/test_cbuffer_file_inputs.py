"""Owned data files preserve ordinary naming validation and retry identity."""
import asyncio
import hashlib
import json
import uuid
from unittest import mock

import pytest

from src import server
from src.agent_workspace_files import AGENT_WORKSPACE_ROOT_ENV
import test_material_naming_mcp as naming_tests


@pytest.fixture
def publication_case(monkeypatch):
    case = naming_tests.CBufferAliasPublicationMCPTests()
    case.setUp()
    root = case.workspace.parent / "agent"
    root.mkdir()
    monkeypatch.setenv(AGENT_WORKSPACE_ROOT_ENV, str(root))
    try:
        yield case, root
    finally:
        case.tearDown()
        case.doCleanups()


def write_json(root, name, value):
    path = root / name
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(raw)
    return str(path), hashlib.sha256(raw).hexdigest()


def publish(operation_id=None, **values):
    return json.loads(asyncio.run(server.publish_cbuffer_aliases(
        "test-workspace", "[]", operation_id=operation_id or uuid.uuid4().hex, **values)))


def field():
    return {"bind": 4, "space": 0, "name": "roughness", "type": "float",
            "components": [{"offset": 0}], "status": "inferred", "provenance": "shader",
            "evidence": "The read at offset 0 controls the reflection lobe width"}


def test_file_input_publishes_identically_and_binds_the_observed_file(publication_case):
    case, root = publication_case
    fields = [field()]
    reviews = [{"bind": 4, "space": 0, "unresolvedOffsets": list(range(4, 32, 4)),
                "evidence": "The remaining physical lanes have no established consumer role"}]
    path, digest = write_json(root, "fields.json", fields)
    reviews_path, reviews_sha = write_json(root, "reviews.json", reviews)
    operation = uuid.uuid4().hex
    result = publish(operation, semantic_fields_file=path, semantic_fields_file_sha256=digest,
                     binding_reviews_file=reviews_path, binding_reviews_file_sha256=reviews_sha)
    assert "error" not in result, result
    assert result["applied"] and result["bindingCoverage"]["complete"]
    assert result["published"][0]["semanticView"]["fields"][0]["name"] == "roughness"
    assert [item["sha256"] for item in result["inputFiles"]] == [digest, reviews_sha]
    assert all(item["replayAuthority"] is False for item in result["inputFiles"])
    saved = json.loads((case.workspace / "ai/cbuffer-name-publication.json").read_bytes())
    assert saved["inputFiles"] == result["inputFiles"]
    with mock.patch.object(server, "_shader_organization_cbuffer_alias_identity",
                           side_effect=AssertionError("A retry must not rebuild capture identity")):
        repeated = publish(operation, semantic_fields_json=json.dumps(fields), binding_reviews_json=json.dumps(reviews))
    assert repeated["idempotentReplay"] and repeated["requestSha256"] == result["requestSha256"]


def test_file_input_is_read_once_and_hash_change_is_rejected_before_capture(publication_case, monkeypatch):
    from src import agent_workspace_files
    case, root = publication_case
    path, digest = write_json(root, "fields.json", [field()])
    original = agent_workspace_files.read_agent_json_file
    reads = []
    def read(*args):
        reads.append(args)
        value = original(*args)
        # The publication must use the observed, hash-bound bytes once. A later
        # edit cannot silently replace that snapshot during normalization.
        (root / "fields.json").write_text("[]")
        return value
    monkeypatch.setattr(agent_workspace_files, "read_agent_json_file", read)
    result = publish(semantic_fields_file=path, semantic_fields_file_sha256=digest)
    assert "error" not in result and len(reads) == 1
    assert result["inputFiles"][0]["sha256"] == digest
    with mock.patch.object(server, "_alias_publication_workspace_status",
                           side_effect=AssertionError("Stale data must fail before capture work")):
        stale = publish(semantic_fields_file=path, semantic_fields_file_sha256=digest)
    assert "SHA256 does not match" in stale["error"]


@pytest.mark.parametrize("arguments, expected", [
    ({"semantic_fields_file": "fields.json", "semantic_fields_json": "[{}]"}, "mutually exclusive"),
    ({"semantic_fields_file_sha256": "a" * 64}, "requires semantic_fields_file"),
    ({"binding_reviews_file": "fields.json", "binding_reviews_json": "[{}]"}, "mutually exclusive"),
    ({"binding_reviews_file_sha256": "a" * 64}, "requires binding_reviews_file"),
])
def test_file_inline_choice_errors_do_not_touch_capture(publication_case, arguments, expected):
    with mock.patch.object(server, "_alias_publication_workspace_status",
                           side_effect=AssertionError("Invalid arguments must fail before capture work")):
        result = publish(**arguments)
    assert expected in result["error"]


def test_file_contents_still_require_valid_array_and_physical_lanes(publication_case):
    case, root = publication_case
    path, digest = write_json(root, "object.json", {"not": "an array"})
    result = publish(semantic_fields_file=path, semantic_fields_file_sha256=digest)
    assert "array" in result["error"]
    invalid = field()
    invalid["components"] = [{"offset": 8192}]
    path, digest = write_json(root, "bad-lane.json", [invalid])
    result = publish(semantic_fields_file=path, semantic_fields_file_sha256=digest)
    assert "error" in result and "reflected" in result["error"]
    assert not (case.workspace / "ai/cbuffer-name-publication.json").exists()


def test_naming_evidence_schema_exposes_reference_shape_and_corrective_errors():
    from pydantic import TypeAdapter
    schema = TypeAdapter(server.CBufferNamingEvidence).json_schema()
    assert schema["properties"]["schema"]["const"] == "bread-cbuffer-naming-evidence/v1"
    assert set(schema["required"]) == {"schema", "summary", "references"}
    reference = schema["$defs"]["CBufferNamingReference"]
    assert set(reference["required"]) == {"kind", "locator"}
    assert "shader-source" in reference["properties"]["kind"]["enum"]
    with pytest.raises(ValueError, match="bread-cbuffer-naming-evidence/v1"):
        server._cbuffer_naming_evidence({"summary": "Observed field", "references": []})
    with pytest.raises(ValueError, match="not a string"):
        server._cbuffer_naming_evidence({"schema": "bread-cbuffer-naming-evidence/v1",
                                       "summary": "Observed field", "references": ["source/shader.hlsl:123"]})
