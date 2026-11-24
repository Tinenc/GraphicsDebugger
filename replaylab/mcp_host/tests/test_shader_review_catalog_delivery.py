"""A compact tool receipt must not silently narrow full-source review."""
import hashlib
import json

import pytest

from src.shader_organization_review import source_snapshot
from src.source_review_observations import review_candidates


def make_source(tmp_path):
    folder = tmp_path / "ai" / "organization-sessions" / ("b" * 32)
    folder.mkdir(parents=True)
    source = "\n".join(
        f"// Scale this direction.\nfloat3 Scale{i}(float3 v, float s) {{\n"
        "    return float3(s * v.x, s * v.y, s * v.z);\n}\n"
        for i in range(24)
    ).encode()
    (tmp_path / "shader.hlsl").write_bytes(source)
    (folder / "session.json").write_text(json.dumps({
        "organizationId": "b" * 32, "state": "drafting",
        "sourcePath": "shader.hlsl", "proposalSourcePath": "shader.hlsl",
        "baselineSourcePath": "shader.hlsl",
        "baselineSourceSha256": hashlib.sha256(source).hexdigest(),
    }), encoding="utf-8")
    return source_snapshot(str(tmp_path), "b" * 32)


def test_begin_save_delivery_keeps_all_matches_in_one_readable_file(tmp_path):
    from src.server import _shader_source_review_delivery
    snapshot = make_source(tmp_path)
    before = (tmp_path / "shader.hlsl").read_bytes()
    receipt = _shader_source_review_delivery(snapshot)
    pointer = receipt["catalog"]
    target = tmp_path / pointer["relativePath"]
    raw = target.read_bytes()
    catalog = json.loads(raw)
    assert pointer["bytes"] == len(raw)
    assert catalog["sourceSha256"] == snapshot["sourceSha256"]
    assert len(catalog["expandedVectorProducts"]["examples"]) == 24
    assert len(receipt["expandedVectorProducts"]["examples"]) < 24
    assert len(review_candidates(catalog)) > len(review_candidates(receipt))
    assert (tmp_path / "shader.hlsl").read_bytes() == before
    assert len(json.dumps(receipt).encode()) < 8 * 1024
    modified = target.stat().st_mtime_ns
    assert _shader_source_review_delivery(snapshot) == receipt
    assert target.stat().st_mtime_ns == modified


def test_catalog_generation_rejects_stale_source_without_writing_evidence(tmp_path):
    from src.server import _shader_source_review_delivery
    snapshot = make_source(tmp_path)
    (tmp_path / "shader.hlsl").write_text("// concurrent edit", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        _shader_source_review_delivery(snapshot)
    assert not list(tmp_path.rglob("source-review-catalog.json"))


def test_catalog_file_is_not_an_authority_for_its_next_refresh(tmp_path):
    from src.server import _shader_source_review_delivery
    snapshot = make_source(tmp_path)
    receipt = _shader_source_review_delivery(snapshot)
    target = tmp_path / receipt["catalog"]["relativePath"]
    target.write_text('{"candidateCount":0,"semanticAssessment":true}', encoding="utf-8")
    _shader_source_review_delivery(snapshot)
    assert len(json.loads(target.read_bytes())["expandedVectorProducts"]["examples"]) == 24
