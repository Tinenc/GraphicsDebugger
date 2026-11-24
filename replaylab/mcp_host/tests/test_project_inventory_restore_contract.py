"""The real project inventory artifact contract survives restore adaptation."""
import copy
from pathlib import Path

import pytest

from src import project_restore
from tests import test_projects as project_fixtures


@pytest.fixture
def published_contract():
    fixture = project_fixtures.ReplayProjectTests()
    fixture.setUp()
    try:
        _, head, inventory, _ = fixture._published_source_advance_fixture()
        snapshot = fixture.service.reuse_snapshot_for_shader_inventory(inventory)
        yield fixture, head, inventory, snapshot, fixture.published_head_status
    finally:
        fixture.tearDown()


def test_real_inventory_artifact_builds_exact_restore_record(published_contract):
    fixture, head, inventory, snapshot, status = published_contract
    artifact = snapshot["knowledge"]["workspaces"][head["knowledgeId"]]["compiledCandidate"]
    assert "sha256" in artifact and "rawSha256" not in artifact
    before = copy.deepcopy(snapshot)
    bound = project_restore._inventory_binding_state(snapshot, inventory)
    knowledge = bound["workspaces"][head["knowledgeId"]]
    identity = project_restore._normal_shader_identity(snapshot["reusePlan"]["matches"][0]["shader"])
    assert project_restore._knowledge_matches_shader(knowledge, identity, executable=True)
    assert knowledge["compiledCandidate"]["rawSha256"] == status["latestCompiled"]["rawSha256"]
    assert knowledge["compiledCandidate"]["sourceSha256"] == status["source"]["currentSha256"]
    assert knowledge["compiledCandidate"]["sourceRevision"] == status["revision"]
    assert knowledge["compiledCandidate"]["targetProfile"] == status["targetProfile"]
    record = project_restore._shader_record_from_status(
        fixture.root, identity, head["knowledgeId"], Path(status["workspacePath"]), status, knowledge)
    assert record["artifactRefs"]["compiledSha256"] == artifact["sha256"]
    assert record["artifactRefs"]["compileConfigSha256"] == project_restore._canonical_sha256(
        knowledge["compilerBinding"])
    assert record["sourceSha256"] == status["source"]["currentSha256"]
    assert snapshot == before  # Adaptation never rewrites the sealed contract.


@pytest.mark.parametrize("hash_fields", [
    {"sha256": "invalid"},
    {"sha256": None},
    {"rawSha256": "f" * 64},
])
def test_conflicting_or_invalid_artifact_hash_is_rejected(published_contract, hash_fields):
    fixture, head, inventory, snapshot, status = published_contract
    snapshot["knowledge"]["workspaces"][head["knowledgeId"]]["compiledCandidate"].update(hash_fields)
    with pytest.raises(RuntimeError, match="sha256|hash fields disagree"):
        project_restore._inventory_binding_state(snapshot, inventory)


def test_matching_legacy_raw_hash_is_preserved(published_contract):
    fixture, head, inventory, snapshot, status = published_contract
    compiled = snapshot["knowledge"]["workspaces"][head["knowledgeId"]]["compiledCandidate"]
    compiled["rawSha256"] = compiled.pop("sha256")
    bound = project_restore._inventory_binding_state(snapshot, inventory)
    assert bound["workspaces"][head["knowledgeId"]]["compiledCandidate"] == compiled
