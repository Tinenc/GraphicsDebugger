"""Large naming revisions remain atomic and readable without dropping history."""

import copy
import hashlib
import json

import pytest

from src.shader_workspace import aliases, alias_field_storage as storage
from src.shader_workspace import cbuffer_alias_publication as publication
from src.shader_workspace.io import atomic_write_json


def fields(count=5379):
    return [{"name": "lights[%d].intensity" % i, "type": "float",
             "components": [{"offset": i * 4}], "status": "inferred",
             "provenance": "shader", "evidence": ("source/shader.glsl:120 uses this light's intensity. " * 4).strip(),
             "description": "控制这盏灯对表面的照明强度。" * 7} for i in range(count)]


def setup(tmp_path, monkeypatch):
    root = tmp_path / "aliases"
    root.mkdir()
    monkeypatch.setattr(aliases, "project_store_root", lambda: root)
    monkeypatch.setattr(aliases, "user_store_root", lambda: tmp_path / "user")
    members = [{"name": "lights", "offset": 0}]
    key = aliases.layout_sha256("Lights", members)
    scope = {"shaderRawSha256": "a" * 64, "stage": "fragment", "bind": 3, "space": 0,
             "layoutSha256": key}
    return root, members, key, scope


def test_repeated_large_usage_observations_are_deduplicated_without_losing_history(tmp_path, monkeypatch):
    root, members, key, scope = setup(tmp_path, monkeypatch)
    review = {'schema': 'bread-cbuffer-usage-review/v1',
        'evidence': 'Source consumers were inspected for all instance lanes.',
        'unusedComponents': [{'offset': n * 4, 'type': 'float'} for n in range(10000)]}
    old = [{'shaderScope': dict(scope, shaderRawSha256=('%064x' % n)), 'sourceSha256': 'c' * 64,
            'source': 'ai', 'fields': [], 'usageReview': review} for n in range(1, 12)]
    path = root / (key + '.json')
    payload = {'schema': aliases.SCHEMA, 'layoutSha256': key, 'block': 'Lights',
               'members': members, 'aliases': [], 'semanticViews': old}
    path.write_bytes(storage.json_bytes(payload))
    assert path.stat().st_size < aliases.MAX_FILE_BYTES
    aliases.publish_aliases('Lights', members, [], shader_scope=scope, semantic_fields=[],
        semantic_source_sha256='d' * 64, semantic_usage_review=review)
    packed = json.loads(path.read_bytes())
    assert len(packed['semanticViews']) == 12 and path.stat().st_size < 200000
    chunks = packed['semanticViews'][0]['usageReview']['unusedComponentChunks']
    assert all(view['usageReview']['unusedComponentChunks'] == chunks for view in packed['semanticViews'])
    assert aliases.read_semantic_view(key, shader_scope=scope)['usageReview'] == review
    from src.shader_workspace.alias_field_gc import _manifest_pages
    assert {page['sha256'] for page in chunks} <= _manifest_pages(packed)
    target = tmp_path / 'copied'
    storage.copy_pages(packed, root, target)
    assert storage.read_usage_review(packed['semanticViews'][0]['usageReview'], target) == review


def test_missing_usage_page_preserves_references_during_unrelated_user_alias_write(tmp_path, monkeypatch):
    root, members, key, scope = setup(tmp_path, monkeypatch)
    review = {'schema': 'bread-cbuffer-usage-review/v1', 'evidence': 'Confirmed unused lanes.',
              'unusedComponents': [{'offset': n * 4, 'type': 'float'} for n in range(64)]}
    aliases.publish_aliases('Lights', members, [], shader_scope=scope, semantic_fields=[],
        semantic_source_sha256='d' * 64, semantic_usage_review=review)
    path = root / (key + '.json')
    manifest = json.loads(path.read_bytes())
    chunk = manifest['semanticViews'][0]['usageReview']['unusedComponentChunks'][0]
    page_bytes = storage._path(root, chunk['sha256']).read_bytes()
    storage._path(root, chunk['sha256']).unlink()
    aliases.publish_aliases('Lights', members, [{'path': 'lights', 'offset': 0, 'alias': 'user label'}], 'user')
    assert json.loads(path.read_bytes())['semanticViews'] == manifest['semanticViews']
    with pytest.raises((ValueError, FileNotFoundError)):
        aliases.read_semantic_view(key, shader_scope=scope)
    storage._path(root, chunk['sha256']).write_bytes(page_bytes)
    assert aliases.read_semantic_view(key, shader_scope=scope)['usageReview'] == review


def test_large_legacy_history_migrates_losslessly_and_receipt_replays(tmp_path, monkeypatch):
    root, members, key, scope = setup(tmp_path, monkeypatch)
    values = fields()
    old_view = {"shaderScope": scope, "sourceSha256": "c" * 64, "source": "user",
                "updatedAt": "2026-10-26", "fields": values}
    legacy = {"schema": aliases.SCHEMA, "layoutSha256": key, "block": "Lights",
              "members": members, "aliases": [{"path": "lights", "offset": 0,
                   "alias": "User lights", "source": "user"}], "semanticViews": [old_view]}
    path = root / (key + ".json")
    atomic_write_json(path, legacy)
    assert path.stat().st_size > aliases.MAX_FILE_BYTES
    assert len(storage.json_bytes(legacy)) < aliases.MAX_FILE_BYTES
    updated_scope = dict(scope, shaderRawSha256="b" * 64)
    batch = {"block": "Lights", "members": members, "entries": [],
             "shaderScope": updated_scope, "semanticFields": values,
             "semanticSourceSha256": "d" * 64}
    args = dict(workspace_root=tmp_path / "workspace", operation_id="large-lights-migration",
                request_sha256="1" * 64, caller_request_sha256="2" * 64,
                publications=[batch], build_result=lambda rows: {"published": rows})
    result = publication.publish_transaction(**args)
    manifest = json.loads(path.read_bytes())
    assert path.stat().st_size < 20 * 1024
    assert len(manifest["semanticViews"]) == 2
    assert manifest["aliases"][0]["alias"] == "User lights"
    for target in (scope, updated_scope):
        assert aliases.read_semantic_view(key, shader_scope=target)["fields"] == values
    # Reused fields have identical pages even across bytecode versions/user ownership.
    assert manifest["semanticViews"][0]["fieldChunks"] == manifest["semanticViews"][1]["fieldChunks"]
    assert len(storage.json_bytes(result)) < 20 * 1024
    snapshot = {p.name: p.read_bytes() for p in (root / storage.DIRECTORY).iterdir()}
    replay = publication.publish_transaction(**args)
    assert replay["idempotentReplay"]
    assert replay["published"] == result["published"]
    assert snapshot == {p.name: p.read_bytes() for p in (root / storage.DIRECTORY).iterdir()}


def test_failed_manifest_commit_rolls_back_but_preserves_shared_pages_then_retries(tmp_path, monkeypatch):
    root, members, key, scope = setup(tmp_path, monkeypatch)
    values = fields(300)
    aliases.publish_aliases("Lights", members, [], shader_scope=scope, semantic_fields=values)
    path = root / (key + ".json")
    original = path.read_bytes()
    newer = copy.deepcopy(values)
    newer[0]["description"] = "Updated explanation"
    batch = {"block": "Lights", "members": members, "entries": [],
             "shaderScope": dict(scope, shaderRawSha256="b" * 64), "semanticFields": newer}
    args = dict(workspace_root=tmp_path / "workspace", operation_id="retry-large-lights",
                request_sha256="1" * 64, caller_request_sha256="2" * 64,
                publications=[batch], build_result=lambda rows: {"published": rows})
    write = publication.atomic_write_bytes
    failed = []
    def fail_once(target, raw):
        if target == path and not failed:
            failed.append(True)
            raise OSError("simulated interrupted manifest commit")
        return write(target, raw)
    monkeypatch.setattr(publication, "atomic_write_bytes", fail_once)
    with pytest.raises(OSError, match="simulated"):
        publication.publish_transaction(**args)
    assert path.read_bytes() == original
    assert aliases.read_semantic_view(key, shader_scope=scope)["fields"] == values
    publication.publish_transaction(**args)
    assert aliases.read_semantic_view(key, shader_scope=batch["shaderScope"])["fields"] == newer


@pytest.mark.parametrize("missing", [False, True])
def test_corrupt_or_missing_page_never_drops_existing_view_on_publish(tmp_path, monkeypatch, missing):
    root, members, key, scope = setup(tmp_path, monkeypatch)
    values = fields(300)
    aliases.publish_aliases("Lights", members, [], shader_scope=scope, semantic_fields=values)
    path = root / (key + ".json")
    original = path.read_bytes()
    descriptor = json.loads(original)["semanticViews"][0]["fieldChunks"][0]
    page = root / storage.DIRECTORY / (descriptor["sha256"] + ".json")
    if missing:
        page.unlink()
    else:
        page.write_bytes(page.read_bytes().replace(b"intensity", b"Intensity", 1))
    with pytest.raises((ValueError, FileNotFoundError)):
        aliases.publish_aliases("Lights", members, [], shader_scope=scope, semantic_fields=values)
    assert path.read_bytes() == original


def test_project_adoption_copies_verified_pages_before_manifest(tmp_path, monkeypatch):
    root, members, key, scope = setup(tmp_path, monkeypatch)
    user = tmp_path / "user"
    values = fields(300)
    aliases.publish_aliases("Lights", members, [], shader_scope=scope, semantic_fields=values, root=user)
    assert not (root / (key + ".json")).exists()
    assert aliases.read_semantic_view(key, shader_scope=scope)["fields"] == values
    assert (root / storage.DIRECTORY).is_dir()
    assert len(list((root / storage.DIRECTORY).iterdir())) == len(list((user / storage.DIRECTORY).iterdir()))


def test_exact_scope_reads_do_not_expand_other_history(tmp_path, monkeypatch):
    root, members, key, scope = setup(tmp_path, monkeypatch)
    values = fields(300)
    aliases.publish_aliases("Lights", members, [], shader_scope=scope, semantic_fields=values)
    newer = dict(scope, shaderRawSha256="b" * 64)
    changed = copy.deepcopy(values)
    changed[0]["description"] = "A later candidate"
    aliases.publish_aliases("Lights", members, [], shader_scope=newer, semantic_fields=changed)
    read = storage.read_page
    read_digests = []
    def observe(root, descriptor):
        read_digests.append(descriptor["sha256"])
        return read(root, descriptor)
    monkeypatch.setattr(storage, "read_page", observe)
    assert aliases.read_semantic_view(key, shader_scope=newer)["fields"] == changed
    manifest = json.loads((root / (key + ".json")).read_bytes())
    selected = manifest["semanticViews"][-1]
    assert read_digests == [row["sha256"] for row in selected["fieldChunks"]]


def test_pre_upgrade_oversized_failed_stage_is_cleaned_for_same_operation_retry(tmp_path, monkeypatch):
    root, members, key, scope = setup(tmp_path, monkeypatch)
    batch = {"block": "Lights", "members": members, "entries": [],
             "shaderScope": scope, "semanticFields": fields(300)}
    fail = [True]
    def result(rows):
        if fail[0]:
            raise OSError("stopped before commit")
        return {"published": rows}
    args = dict(workspace_root=tmp_path / "workspace", operation_id="oversized-stage-retry",
                request_sha256="1" * 64, caller_request_sha256="2" * 64,
                publications=[batch], build_result=result)
    with pytest.raises(OSError, match="before commit"):
        publication.publish_transaction(**args)
    directory, ledger_path = publication.publication_paths(args["workspace_root"], args["operation_id"])
    ledger = publication.read_ledger(ledger_path)
    stage = __import__('pathlib').Path(ledger["stageRoot"]) / (key + ".json")
    stage.write_bytes(stage.read_bytes() + b" " * aliases.MAX_FILE_BYTES)
    assert stage.stat().st_size > aliases.MAX_FILE_BYTES
    fail[0] = False
    assert publication.publish_transaction(**args)["published"][0]["semanticFieldsWritten"] == 300
    assert not stage.exists()


def test_valid_hash_with_invalid_user_fields_cannot_be_overwritten(tmp_path, monkeypatch):
    root, members, key, scope = setup(tmp_path, monkeypatch)
    values = fields(300)
    aliases.publish_aliases("Lights", members, [], "user", shader_scope=scope, semantic_fields=values)
    path = root / (key + ".json")
    manifest = json.loads(path.read_bytes())
    invalid = copy.deepcopy(values)
    invalid[0]["type"] = "invalid-type"
    manifest["semanticViews"][0] = storage.store_view(dict(manifest["semanticViews"][0], fields=invalid), root)
    path.write_bytes(storage.json_bytes(manifest))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="scalar or vector"):
        aliases.publish_aliases("Lights", members, [], shader_scope=scope, semantic_fields=values)
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", [
    {"fieldCount": 1}, {"fields": []},
    {"fieldChunks": [{"sha256": "../escape", "count": 1, "sizeBytes": 5}]},
])
def test_page_reference_bounds_and_identity_are_checked(tmp_path, change):
    view = storage.store_view({"fields": fields(100)}, tmp_path)
    view.update(change)
    with pytest.raises(ValueError):
        storage.read_fields(view, tmp_path)
