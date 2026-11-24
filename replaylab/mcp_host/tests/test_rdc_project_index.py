from __future__ import annotations

import copy
import hashlib
import json
import unittest

from src.rdc_project_index import (
    MAX_EVENT_IDS_PER_SHADER,
    MAX_PROJECT_BINDINGS,
    RDC_PROJECT_INDEX_LEGACY_SCHEMA,
    RDC_PROJECT_INDEX_LEGACY_VERSION,
    RDC_PROJECT_INDEX_SCHEMA,
    RDC_PROJECT_INDEX_VERSION,
    RDC_PROJECT_INDEX_V2_SCHEMA,
    RDC_PROJECT_INDEX_V2_VERSION,
    RdcProjectIndexError,
    binding_knowledge_is_current,
    build_desired_project_head,
    build_project_binding,
    build_realized_apply_receipt,
    build_realized_apply_snapshot,
    encode,
    encode_index,
    new,
    new_index,
    normalise_project_binding,
    parse,
    parse_index,
    remove,
    remove_project_binding,
    referenced_heads_sha256,
    select,
    select_project_binding,
    upsert,
    upsert_project_binding,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _id(label: str) -> str:
    return _sha(label)[:32]


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _stable_key_sha256(identity: dict) -> str:
    fields = {
        key: identity[key]
        for key in (
            "api",
            "encoding",
            "rawSha256",
            "stage",
            "entryPoint",
            "abiHash",
            "layoutHash",
        )
    }
    encoded = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _shader(
    label: str = "shader",
    *,
    resource_id: int = 7,
    anchor_event_id: int | None = 3,
    workspace_path: str | None = None,
    api: str = "d3d11",
    encoding: str = "dxbc",
) -> dict:
    identity = {
        "api": api,
        "encoding": encoding,
        "resourceId": resource_id,
        "stage": "fragment",
        "entryPoint": "main",
        "rawSha256": _sha(label + ":captured-raw"),
        "abiHash": _sha(label + ":abi"),
        "layoutHash": _sha(label + ":layout"),
        "stableKeyComplete": True,
        "stableKeySha256": "",
        "anchorEventId": anchor_event_id,
    }
    identity["stableKeySha256"] = _stable_key_sha256(identity)
    knowledge_id = _id(label + ":knowledge")
    candidate_revision = 3
    candidate_raw_sha256 = _sha(label + ":candidate-raw")
    return {
        "identity": identity,
        "knowledgeId": knowledge_id,
        "managedWorkspace": {
            "workspaceId": _id(label + ":managed-workspace"),
            "path": workspace_path
            or "shader-workspaces/%s" % _id(label + ":managed-workspace"),
        },
        "candidateRevision": candidate_revision,
        "candidateRawSha256": candidate_raw_sha256,
        "sourceSha256": _sha(label + ":source"),
        "artifactRefs": {
            "projectArtifactId": knowledge_id,
            "revision": candidate_revision,
            "compiledSha256": candidate_raw_sha256,
            "compileConfigSha256": _sha(label + ":compile-config"),
            "representationsSha256": _sha(label + ":representations"),
            "aliasesSha256": _sha(label + ":aliases"),
        },
    }


def _legacy_shader(
    label: str = "shader",
    *,
    resource_id: int = 7,
    event_ids: list[int] | None = None,
) -> dict:
    return {
        "identity": {
            "resourceId": resource_id,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": _sha(label + ":captured-raw"),
            "codeSha256": _sha(label + ":captured-code"),
            "interfaceHash": _sha(label + ":interface"),
            "layoutHash": _sha(label + ":layout"),
            "interfaceComplete": True,
            "eventIds": [9, 3] if event_ids is None else event_ids,
        },
        "knowledgeId": _id(label + ":knowledge"),
        "managedWorkspace": {
            "workspaceId": _id(label + ":managed-workspace"),
            "path": "shader-workspaces/%s" % _id(label + ":managed-workspace"),
        },
        "candidateRevision": 3,
        "candidateRawSha256": _sha(label + ":candidate-raw"),
        "sourceSha256": _sha(label + ":source"),
    }


def _material(label: str = "material", *, shader_resource_id: int = 7) -> dict:
    return {
        "knowledgeId": _id(label + ":knowledge"),
        "shaderResourceId": shader_resource_id,
        "stage": "fragment",
        "blockIndex": 0,
        "path": "PerMaterial",
        "layoutSha256": _sha(label + ":layout"),
        "eventId": 9,
        "parameters": [
            {"parameterId": _id(label + ":roughness")},
            {
                "parameterId": _id(label + ":base-color"),
                "path": "BaseColor",
                "baseType": "Float",
                "components": 4,
                "arrayIndex": 0,
                "byteOffset": 0,
                "byteSize": 16,
            },
        ],
    }


def _desired_binding(
    label: str = "binding",
    *,
    project_id: str | None = None,
    workspace_id: str | None = None,
    knowledge_hash: str | None = None,
    with_records: bool = True,
) -> dict:
    return {
        "projectId": project_id or _id(label + ":project"),
        "workspaceId": workspace_id or _id(label + ":workspace"),
        "scopeSha256": _sha(label + ":scope"),
        "captureId": _sha(label + ":capture"),
        "catalogSha256": _sha(label + ":catalog"),
        "knowledgeContentSha256": knowledge_hash or _sha(label + ":knowledge-content"),
        "matcherPolicySha256": _sha(label + ":matcher-policy"),
        "shaders": [_shader(label)] if with_records else [],
        "materials": [_material(label)] if with_records else [],
    }


def _v3_binding(desired: dict, *, active_revision: int = 11) -> dict:
    head = build_desired_project_head(
        active_knowledge_revision=active_revision,
        knowledge_content_sha256=desired["knowledgeContentSha256"],
        knowledge_ids_sha256=_sha("fixture:knowledge-ids"),
        artifact_refs_sha256=_sha("fixture:artifact-refs"),
        referenced_heads_sha256=referenced_heads_sha256(
            desired["shaders"], desired["materials"]
        ),
    )
    snapshot = build_realized_apply_snapshot(head, [], coverage="none")
    return build_project_binding(desired, head, snapshot)


def _binding(
    label: str = "binding",
    *,
    project_id: str | None = None,
    workspace_id: str | None = None,
    knowledge_hash: str | None = None,
    with_records: bool = True,
) -> dict:
    return _v3_binding(
        _desired_binding(
            label,
            project_id=project_id,
            workspace_id=workspace_id,
            knowledge_hash=knowledge_hash,
            with_records=with_records,
        )
    )


def _refresh_v3_binding(binding: dict) -> dict:
    desired = {
        key: copy.deepcopy(binding[key])
        for key in (
            "projectId",
            "workspaceId",
            "scopeSha256",
            "captureId",
            "catalogSha256",
            "knowledgeContentSha256",
            "matcherPolicySha256",
            "shaders",
            "materials",
        )
    }
    active_revision = int(
        binding.get("desiredProjectHead", {}).get("activeKnowledgeRevision", 11)
    )
    return _v3_binding(desired, active_revision=active_revision)


def _applied_binding(label: str = "binding", *, abi_report: bool = True) -> dict:
    binding = _binding(label)
    shader = binding["shaders"][0]
    receipt = build_realized_apply_receipt(
        shader["identity"],
        shader["managedWorkspace"]["workspaceId"],
        shader["candidateRevision"],
        shader["candidateRawSha256"],
        shader["identity"]["abiHash"],
        abi_report_sha256=_sha(label + ":abi-report") if abi_report else None,
    )
    snapshot = build_realized_apply_snapshot(
        binding["desiredProjectHead"], [receipt], coverage="complete"
    )
    desired = {
        key: copy.deepcopy(binding[key])
        for key in (
            "projectId",
            "workspaceId",
            "scopeSha256",
            "captureId",
            "catalogSha256",
            "knowledgeContentSha256",
            "matcherPolicySha256",
            "shaders",
            "materials",
        )
    }
    return build_project_binding(
        desired, binding["desiredProjectHead"], snapshot
    )


def _legacy_binding(label: str = "binding") -> dict:
    binding = _desired_binding(label)
    binding["shaders"] = [_legacy_shader(label)]
    return binding


def _sealed_legacy_index(binding: dict) -> dict:
    clean = copy.deepcopy(binding)
    for shader in clean["shaders"]:
        shader["identity"]["eventIds"].sort()
    for material in clean["materials"]:
        material["parameters"].sort(key=lambda item: item["parameterId"])
    clean["shaders"].sort(
        key=lambda shader: (
            shader["identity"]["resourceId"],
            shader["identity"]["stage"],
            shader["identity"]["entryPoint"],
            shader["identity"]["rawSha256"],
            shader["knowledgeId"],
            shader["managedWorkspace"]["workspaceId"],
            shader["managedWorkspace"]["path"],
            shader["candidateRevision"],
        )
    )
    clean["materials"].sort(
        key=lambda material: (
            material["shaderResourceId"],
            material["stage"],
            material["eventId"],
            material["blockIndex"],
            material["path"],
            material["knowledgeId"],
        )
    )
    unsigned = {
        "schema": RDC_PROJECT_INDEX_LEGACY_SCHEMA,
        "schemaVersion": RDC_PROJECT_INDEX_LEGACY_VERSION,
        "bindings": [clean],
    }
    seal = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return {**unsigned, "sealSha256": seal}


def _reseal_v2_index(index: dict) -> dict:
    unsigned = copy.deepcopy(index)
    unsigned.pop("sealSha256", None)
    return {
        **unsigned,
        "sealSha256": hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
    }


def _sealed_v2_index(binding: dict | None = None) -> dict:
    current = upsert_project_binding(new_index(), binding or _binding())
    desired = {
        key: copy.deepcopy(current["bindings"][0][key])
        for key in (
            "projectId",
            "workspaceId",
            "scopeSha256",
            "captureId",
            "catalogSha256",
            "knowledgeContentSha256",
            "matcherPolicySha256",
            "shaders",
            "materials",
        )
    }
    return _reseal_v2_index(
        {
            "schema": RDC_PROJECT_INDEX_V2_SCHEMA,
            "schemaVersion": RDC_PROJECT_INDEX_V2_VERSION,
            "bindings": [desired],
            "sealSha256": "0" * 64,
        }
    )


def _sealed_v2_with_legacy_protocol_tokens(
    *, api: str = "D3D11", encoding: str = "DXBC"
) -> dict:
    current = upsert_project_binding(new_index(), _binding())
    desired = {
        key: copy.deepcopy(current["bindings"][0][key])
        for key in (
            "projectId",
            "workspaceId",
            "scopeSha256",
            "captureId",
            "catalogSha256",
            "knowledgeContentSha256",
            "matcherPolicySha256",
            "shaders",
            "materials",
        )
    }
    legacy = {
        "schema": RDC_PROJECT_INDEX_V2_SCHEMA,
        "schemaVersion": RDC_PROJECT_INDEX_V2_VERSION,
        "bindings": [desired],
    }
    identity = desired["shaders"][0]["identity"]
    identity["api"] = api
    identity["encoding"] = encoding
    identity["stableKeySha256"] = _stable_key_sha256(identity)
    legacy["sealSha256"] = "0" * 64
    return _reseal_v2_index(legacy)


class RdcProjectIndexTests(unittest.TestCase):
    def test_new_index_round_trips_as_canonical_json(self) -> None:
        index = new_index()
        payload = encode_index(index)

        self.assertEqual(parse_index(payload), index)
        self.assertEqual(payload, encode_index(parse_index(payload)))
        decoded = json.loads(payload.decode("utf-8"))
        self.assertEqual(decoded["schema"], RDC_PROJECT_INDEX_SCHEMA)
        self.assertEqual(decoded["schemaVersion"], RDC_PROJECT_INDEX_VERSION)
        self.assertEqual(decoded["bindings"], [])

        unsigned = dict(decoded)
        seal = unsigned.pop("sealSha256")
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(seal, hashlib.sha256(canonical).hexdigest())

    def test_complete_v3_head_and_realized_receipt_round_trip(self) -> None:
        binding = _applied_binding()
        index = upsert_project_binding(new_index(), binding)
        parsed = parse_index(encode_index(index))

        selected = parsed["bindings"][0]
        self.assertTrue(selected["desiredProjectHead"]["complete"])
        snapshot = selected["realizedApplySnapshot"]
        self.assertEqual(snapshot["coverage"], "complete")
        self.assertEqual(len(snapshot["receipts"]), 1)
        receipt = snapshot["receipts"][0]
        self.assertTrue(receipt["routingOnly"])
        self.assertTrue(receipt["freshTargetValidationRequired"])
        self.assertFalse(receipt["physicalApplyAuthority"])
        self.assertFalse(receipt["replacementHandleReusable"])
        self.assertRegex(receipt["abiReportSha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(normalise_project_binding(binding), selected)

    def test_active_revision_is_outer_sealed_but_not_semantic_head_identity(self) -> None:
        index = upsert_project_binding(new_index(), _binding())
        changed = copy.deepcopy(index)
        head = changed["bindings"][0]["desiredProjectHead"]
        semantic_head = head["headSha256"]
        head["activeKnowledgeRevision"] += 100
        changed = _reseal_v2_index(changed)

        parsed = parse_index(json.dumps(changed))
        self.assertEqual(
            parsed["bindings"][0]["desiredProjectHead"]["headSha256"],
            semantic_head,
        )
        self.assertEqual(
            parsed["bindings"][0]["desiredProjectHead"][
                "activeKnowledgeRevision"
            ],
            111,
        )

    def test_v1_payload_is_verified_then_migrated_and_resealed_as_v3(self) -> None:
        legacy = _sealed_legacy_index(_legacy_binding())
        old_seal = legacy["sealSha256"]

        migrated = parse_index(
            json.dumps(
                legacy,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

        self.assertEqual(migrated["schema"], RDC_PROJECT_INDEX_SCHEMA)
        self.assertEqual(migrated["schemaVersion"], RDC_PROJECT_INDEX_VERSION)
        self.assertNotEqual(migrated["sealSha256"], old_seal)
        identity = migrated["bindings"][0]["shaders"][0]["identity"]
        self.assertFalse(identity["stableKeyComplete"])
        self.assertEqual(identity["api"], "")
        self.assertEqual(identity["encoding"], "")
        self.assertEqual(
            identity["abiHash"],
            _legacy_shader("binding")["identity"]["interfaceHash"],
        )
        self.assertEqual(identity["anchorEventId"], 3)
        self.assertNotIn("eventIds", identity)
        self.assertEqual(identity["stableKeySha256"], _stable_key_sha256(identity))
        refs = migrated["bindings"][0]["shaders"][0]["artifactRefs"]
        self.assertEqual(refs["compileConfigSha256"], "")
        self.assertEqual(refs["representationsSha256"], "")
        self.assertEqual(refs["aliasesSha256"], "")
        head = migrated["bindings"][0]["desiredProjectHead"]
        self.assertFalse(head["complete"])
        self.assertEqual(head["activeKnowledgeRevision"], 0)
        self.assertEqual(head["knowledgeIdsSha256"], "")
        snapshot = migrated["bindings"][0]["realizedApplySnapshot"]
        self.assertEqual(snapshot["coverage"], "legacy-unknown")
        self.assertEqual(snapshot["receipts"], [])
        self.assertEqual(parse_index(encode_index(migrated)), migrated)

    def test_v2_payload_is_verified_then_migrated_without_applied_claim(self) -> None:
        legacy = _sealed_v2_index()
        old_seal = legacy["sealSha256"]

        migrated = parse_index(json.dumps(legacy))

        self.assertEqual(migrated["schemaVersion"], 3)
        self.assertNotEqual(migrated["sealSha256"], old_seal)
        binding = migrated["bindings"][0]
        self.assertFalse(binding["desiredProjectHead"]["complete"])
        self.assertEqual(
            binding["desiredProjectHead"]["knowledgeContentSha256"],
            binding["knowledgeContentSha256"],
        )
        self.assertEqual(
            binding["realizedApplySnapshot"]["coverage"], "legacy-unknown"
        )
        self.assertEqual(binding["realizedApplySnapshot"]["receipts"], [])

    def test_v1_tampering_is_rejected_before_migration(self) -> None:
        legacy = _sealed_legacy_index(_legacy_binding())
        legacy["bindings"][0]["shaders"][0]["identity"]["eventIds"] = [3]
        with self.assertRaisesRegex(RdcProjectIndexError, "sealSha256"):
            parse_index(json.dumps(legacy))

    def test_legacy_v2_protocol_spellings_are_verified_then_downgraded(self) -> None:
        legacy = _sealed_v2_with_legacy_protocol_tokens(
            api="GraphicsAPI.Direct3D11",
            encoding="ShaderEncoding.DirectXBytecode",
        )
        legacy_identity = legacy["bindings"][0]["shaders"][0]["identity"]
        legacy_key = legacy_identity["stableKeySha256"]
        legacy_seal = legacy["sealSha256"]

        migrated = parse_index(json.dumps(legacy))
        identity = migrated["bindings"][0]["shaders"][0]["identity"]

        self.assertEqual(identity["api"], "d3d11")
        self.assertEqual(identity["encoding"], "dxbc")
        self.assertFalse(identity["stableKeyComplete"])
        self.assertNotEqual(identity["stableKeySha256"], legacy_key)
        self.assertEqual(identity["stableKeySha256"], _stable_key_sha256(identity))
        self.assertNotEqual(migrated["sealSha256"], legacy_seal)
        self.assertEqual(parse_index(encode_index(migrated)), migrated)

    def test_legacy_v2_digest_and_outer_seal_are_verified_before_migration(self) -> None:
        bad_key = _sealed_v2_with_legacy_protocol_tokens()
        bad_key["bindings"][0]["shaders"][0]["identity"][
            "stableKeySha256"
        ] = _sha("forged legacy key")
        bad_key = _reseal_v2_index(bad_key)
        with self.assertRaisesRegex(RdcProjectIndexError, "stableKeySha256"):
            parse_index(json.dumps(bad_key))

        bad_seal = _sealed_v2_with_legacy_protocol_tokens()
        bad_seal["bindings"][0]["knowledgeContentSha256"] = _sha("tampered")
        with self.assertRaisesRegex(RdcProjectIndexError, "sealSha256"):
            parse_index(json.dumps(bad_seal))

    def test_new_v2_identities_require_protocol_tokens_and_supported_pairs(self) -> None:
        supported = (
            ("d3d11", "dxbc"),
            ("d3d12", "dxbc"),
            ("d3d12", "dxil"),
            ("opengl", "glsl"),
            ("opengl", "spirv"),
            ("opengl", "opengl-spirv"),
            ("vulkan", "spirv"),
        )
        for index, (api, encoding) in enumerate(supported):
            with self.subTest(api=api, encoding=encoding):
                binding = _binding("supported-%d" % index)
                binding["shaders"] = [
                    _shader("supported-%d" % index, api=api, encoding=encoding)
                ]
                binding = _refresh_v3_binding(binding)
                normalised = upsert_project_binding(new_index(), binding)
                identity = normalised["bindings"][0]["shaders"][0]["identity"]
                self.assertEqual((identity["api"], identity["encoding"]), (api, encoding))
                self.assertTrue(identity["stableKeyComplete"])

        wrong_encoding = _binding("wrong-encoding")
        wrong_encoding["shaders"] = [
            _shader("wrong-encoding", api="d3d11", encoding="spirv")
        ]
        with self.assertRaisesRegex(RdcProjectIndexError, "pair is unsupported"):
            upsert_project_binding(new_index(), wrong_encoding)

        display_encoding = _binding("display-encoding")
        display_encoding["shaders"] = [
            _shader("display-encoding", api="d3d11", encoding="DXBC")
        ]
        with self.assertRaisesRegex(RdcProjectIndexError, "lowercase protocol encoding"):
            upsert_project_binding(new_index(), display_encoding)

        display_api = _binding("display-api")
        display_api["shaders"] = [
            _shader("display-api", api="Direct3D11", encoding="dxbc")
        ]
        with self.assertRaisesRegex(RdcProjectIndexError, "lowercase protocol API"):
            upsert_project_binding(new_index(), display_api)

    def test_stable_key_excludes_capture_local_resource_and_anchor(self) -> None:
        first = _shader(resource_id=7, anchor_event_id=3)
        second = _shader(resource_id=99, anchor_event_id=600)
        self.assertEqual(
            first["identity"]["stableKeySha256"],
            second["identity"]["stableKeySha256"],
        )

        binding = _binding()
        binding["shaders"] = [first, second]
        binding["materials"] = []
        binding = _refresh_v3_binding(binding)
        selected = upsert_project_binding(new_index(), binding)["bindings"][0]
        self.assertEqual(len(selected["shaders"]), 2)
        self.assertEqual(
            {
                shader["identity"]["anchorEventId"]
                for shader in selected["shaders"]
            },
            {3, 600},
        )

    def test_stable_key_hash_and_complete_components_are_strict(self) -> None:
        wrong_hash = _binding()
        wrong_hash["shaders"][0]["identity"]["stableKeySha256"] = _sha("wrong")
        with self.assertRaisesRegex(RdcProjectIndexError, "stableKeySha256"):
            upsert_project_binding(new_index(), wrong_hash)

        missing_abi = _binding()
        identity = missing_abi["shaders"][0]["identity"]
        identity["abiHash"] = ""
        identity["stableKeySha256"] = _stable_key_sha256(identity)
        with self.assertRaisesRegex(RdcProjectIndexError, "stable-key component"):
            upsert_project_binding(new_index(), missing_abi)

        incomplete = _binding()
        identity = incomplete["shaders"][0]["identity"]
        identity["stableKeyComplete"] = False
        identity["abiHash"] = ""
        identity["layoutHash"] = ""
        identity["anchorEventId"] = None
        identity["stableKeySha256"] = _stable_key_sha256(identity)
        incomplete = _refresh_v3_binding(incomplete)
        selected = upsert_project_binding(new_index(), incomplete)["bindings"][0]
        self.assertFalse(selected["shaders"][0]["identity"]["stableKeyComplete"])
        self.assertIsNone(selected["shaders"][0]["identity"]["anchorEventId"])

    def test_shader_identity_has_at_most_one_optional_anchor_eid(self) -> None:
        legacy_shape_in_v2 = _binding()
        identity = legacy_shape_in_v2["shaders"][0]["identity"]
        identity["eventIds"] = [3, 9]
        with self.assertRaisesRegex(RdcProjectIndexError, "fields"):
            upsert_project_binding(new_index(), legacy_shape_in_v2)

        invalid_anchor = _binding()
        invalid_anchor["shaders"][0]["identity"]["anchorEventId"] = [3, 9]
        with self.assertRaisesRegex(RdcProjectIndexError, "anchorEventId"):
            upsert_project_binding(new_index(), invalid_anchor)

    def test_artifact_references_are_exact_and_cannot_carry_replay_handles(self) -> None:
        conflict = _binding()
        conflict["shaders"][0]["artifactRefs"]["revision"] += 1
        with self.assertRaisesRegex(RdcProjectIndexError, "conflicts"):
            upsert_project_binding(new_index(), conflict)

        malformed_hash = _binding()
        malformed_hash["shaders"][0]["artifactRefs"]["aliasesSha256"] = "ABC"
        with self.assertRaisesRegex(RdcProjectIndexError, "aliasesSha256"):
            upsert_project_binding(new_index(), malformed_hash)

        replay_handle = _binding()
        replay_handle["shaders"][0]["artifactRefs"]["replacementResourceId"] = 77
        with self.assertRaisesRegex(RdcProjectIndexError, "fields"):
            upsert_project_binding(new_index(), replay_handle)

        top_level_handle = _binding()
        top_level_handle["shaders"][0]["replacementHandle"] = "session-local"
        with self.assertRaisesRegex(RdcProjectIndexError, "fields"):
            upsert_project_binding(new_index(), top_level_handle)

    def test_v3_head_and_receipt_inner_hashes_reject_resealed_tampering(self) -> None:
        index = upsert_project_binding(new_index(), _applied_binding())

        changed_head = copy.deepcopy(index)
        changed_head["bindings"][0]["desiredProjectHead"][
            "knowledgeIdsSha256"
        ] = _sha("forged-knowledge-ids")
        changed_head = _reseal_v2_index(changed_head)
        with self.assertRaisesRegex(RdcProjectIndexError, "headSha256"):
            parse_index(json.dumps(changed_head))

        changed_receipt = copy.deepcopy(index)
        changed_receipt["bindings"][0]["realizedApplySnapshot"]["receipts"][0][
            "candidateRawSha256"
        ] = _sha("forged-candidate")
        changed_receipt = _reseal_v2_index(changed_receipt)
        with self.assertRaisesRegex(RdcProjectIndexError, "receiptSha256"):
            parse_index(json.dumps(changed_receipt))

    def test_receipts_reject_handles_unknown_fields_and_unsafe_flags(self) -> None:
        index = upsert_project_binding(new_index(), _applied_binding())
        forbidden = (
            "captureSessionId",
            "replacementId",
            "operationId",
            "targetResourceId",
        )
        for field in forbidden:
            with self.subTest(field=field):
                changed = copy.deepcopy(index)
                changed["bindings"][0]["realizedApplySnapshot"]["receipts"][0][
                    field
                ] = "1" * 32
                changed = _reseal_v2_index(changed)
                with self.assertRaisesRegex(RdcProjectIndexError, "fields"):
                    parse_index(json.dumps(changed))

        unsafe = copy.deepcopy(index)
        unsafe["bindings"][0]["realizedApplySnapshot"]["receipts"][0][
            "physicalApplyAuthority"
        ] = True
        unsafe = _reseal_v2_index(unsafe)
        with self.assertRaisesRegex(RdcProjectIndexError, "unsafe value"):
            parse_index(json.dumps(unsafe))

    def test_resealed_receipt_must_still_match_one_exact_desired_shader(self) -> None:
        index = upsert_project_binding(new_index(), _applied_binding())
        changed = copy.deepcopy(index)
        snapshot = changed["bindings"][0]["realizedApplySnapshot"]
        receipt = snapshot["receipts"][0]
        receipt["originalResourceId"] += 1
        unsigned_receipt = dict(receipt)
        unsigned_receipt.pop("receiptSha256")
        receipt["receiptSha256"] = _canonical_sha256(unsigned_receipt)
        snapshot["receiptsSha256"] = _canonical_sha256(snapshot["receipts"])
        changed = _reseal_v2_index(changed)

        with self.assertRaisesRegex(RdcProjectIndexError, "one exact desired Shader"):
            parse_index(json.dumps(changed))

    def test_unknown_v3_head_and_snapshot_fields_are_rejected(self) -> None:
        for container in ("desiredProjectHead", "realizedApplySnapshot"):
            with self.subTest(container=container):
                binding = _binding()
                binding[container]["unexpected"] = True
                with self.assertRaisesRegex(RdcProjectIndexError, "fields"):
                    normalise_project_binding(binding)

    def test_legacy_binding_input_remains_accepted_but_never_authoritative(self) -> None:
        index = upsert_project_binding(new_index(), _v3_binding(_legacy_binding()))
        shader = index["bindings"][0]["shaders"][0]
        self.assertFalse(shader["identity"]["stableKeyComplete"])
        self.assertEqual(shader["identity"]["anchorEventId"], 3)
        self.assertEqual(
            shader["artifactRefs"]["compiledSha256"],
            shader["candidateRawSha256"],
        )

    def test_short_public_apis_match_descriptive_apis(self) -> None:
        binding = _binding()
        index = upsert(new(), binding)
        self.assertEqual(encode(index), encode_index(index))
        self.assertEqual(parse(encode(index)), parse_index(encode_index(index)))
        self.assertEqual(
            select(index, binding["projectId"], binding["workspaceId"]),
            select_project_binding(
                index, binding["projectId"], binding["workspaceId"]
            ),
        )
        self.assertEqual(
            remove(index, binding["projectId"], binding["workspaceId"]),
            remove_project_binding(
                index, binding["projectId"], binding["workspaceId"]
            ),
        )

    def test_upsert_normalises_and_selects_complete_records(self) -> None:
        binding = _binding()
        original = copy.deepcopy(binding)
        index = upsert_project_binding(new_index(), binding)

        # Public mutation APIs do not mutate caller-owned objects.
        self.assertEqual(binding, original)
        selected = select_project_binding(
            index,
            binding["projectId"],
            binding["workspaceId"],
            knowledge_content_sha256=binding["knowledgeContentSha256"],
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        identity = selected["shaders"][0]["identity"]
        self.assertEqual(identity["anchorEventId"], 3)
        self.assertNotIn("eventIds", identity)
        self.assertTrue(identity["stableKeyComplete"])
        self.assertEqual(identity["stableKeySha256"], _stable_key_sha256(identity))
        self.assertEqual(
            [item["parameterId"] for item in selected["materials"][0]["parameters"]],
            sorted(item["parameterId"] for item in binding["materials"][0]["parameters"]),
        )
        self.assertEqual(
            selected["shaders"][0]["managedWorkspace"]["path"],
            binding["shaders"][0]["managedWorkspace"]["path"],
        )

        selected["captureId"] = _sha("mutated-copy")
        self.assertNotEqual(index["bindings"][0]["captureId"], selected["captureId"])

    def test_empty_binding_is_valid_and_can_be_populated_later(self) -> None:
        empty = _binding(with_records=False)
        index = upsert_project_binding(new_index(), empty)
        self.assertEqual(index["bindings"][0]["shaders"], [])
        self.assertEqual(index["bindings"][0]["materials"], [])

        populated = _binding(
            project_id=empty["projectId"], workspace_id=empty["workspaceId"]
        )
        index = upsert_project_binding(index, populated)
        self.assertEqual(len(index["bindings"]), 1)
        self.assertEqual(len(index["bindings"][0]["shaders"]), 1)

    def test_upsert_replaces_exact_key_and_sorts_distinct_bindings(self) -> None:
        first = _binding("first", project_id="f" * 32, workspace_id="f" * 32)
        replacement = copy.deepcopy(first)
        replacement["knowledgeContentSha256"] = _sha("replacement-knowledge")
        replacement = _refresh_v3_binding(replacement)
        second = _binding("second", project_id="0" * 32, workspace_id="1" * 32)

        index = upsert_project_binding(new_index(), first)
        index = upsert_project_binding(index, replacement)
        index = upsert_project_binding(index, second)

        self.assertEqual(len(index["bindings"]), 2)
        self.assertEqual(index["bindings"][0]["projectId"], "0" * 32)
        selected = select_project_binding(index, "f" * 32, "f" * 32)
        assert selected is not None
        self.assertEqual(
            selected["knowledgeContentSha256"], replacement["knowledgeContentSha256"]
        )

    def test_knowledge_hash_invalidates_selection(self) -> None:
        binding = _binding()
        index = upsert_project_binding(new_index(), binding)

        self.assertTrue(
            binding_knowledge_is_current(binding, binding["knowledgeContentSha256"])
        )
        self.assertFalse(binding_knowledge_is_current(binding, _sha("stale")))
        self.assertIsNone(
            select_project_binding(
                index,
                binding["projectId"],
                binding["workspaceId"],
                knowledge_content_sha256=_sha("stale"),
            )
        )

    def test_remove_one_workspace_or_all_project_bindings(self) -> None:
        project_id = _id("shared-project")
        one = _binding("one", project_id=project_id, workspace_id=_id("workspace-one"))
        two = _binding("two", project_id=project_id, workspace_id=_id("workspace-two"))
        other = _binding("other")
        index = new_index()
        for binding in (one, two, other):
            index = upsert_project_binding(index, binding)

        index = remove_project_binding(index, project_id, one["workspaceId"])
        self.assertIsNone(select_project_binding(index, project_id, one["workspaceId"]))
        self.assertIsNotNone(select_project_binding(index, project_id, two["workspaceId"]))

        index = remove_project_binding(index, project_id)
        self.assertIsNone(select_project_binding(index, project_id, two["workspaceId"]))
        self.assertIsNotNone(
            select_project_binding(index, other["projectId"], other["workspaceId"])
        )
        self.assertEqual(remove_project_binding(index, project_id), index)

    def test_duplicate_json_keys_and_non_finite_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(RdcProjectIndexError, "duplicate JSON key"):
            parse_index(b'{"schema":"one","schema":"two"}')
        with self.assertRaisesRegex(RdcProjectIndexError, "non-finite"):
            parse_index(b'{"schema":NaN}')

    def test_tampering_unsupported_version_and_extra_fields_are_rejected(self) -> None:
        binding = _binding()
        index = upsert_project_binding(new_index(), binding)
        tampered = copy.deepcopy(index)
        tampered["bindings"][0]["catalogSha256"] = _sha("tampered")
        with self.assertRaisesRegex(RdcProjectIndexError, "sealSha256"):
            encode_index(tampered)

        wrong_version = copy.deepcopy(index)
        wrong_version["schemaVersion"] = 99
        with self.assertRaisesRegex(RdcProjectIndexError, "schema/version"):
            encode_index(wrong_version)

        boolean_version = copy.deepcopy(index)
        boolean_version["schemaVersion"] = True
        with self.assertRaisesRegex(RdcProjectIndexError, "schema/version"):
            encode_index(boolean_version)

        extra = copy.deepcopy(binding)
        extra["unexpected"] = True
        with self.assertRaisesRegex(RdcProjectIndexError, "fields"):
            upsert_project_binding(new_index(), extra)

    def test_managed_workspace_path_must_be_safe_project_relative(self) -> None:
        unsafe_paths = (
            "/absolute/path",
            "C:/absolute/path",
            "../escape",
            "nested/../escape",
            "nested/./alias",
            "nested\\windows",
            "nested//empty",
            "nested/trailing/",
        )
        for unsafe in unsafe_paths:
            with self.subTest(path=unsafe):
                binding = _binding()
                binding["shaders"][0]["managedWorkspace"]["path"] = unsafe
                with self.assertRaisesRegex(RdcProjectIndexError, "project-relative|root"):
                    upsert_project_binding(new_index(), binding)

    def test_material_path_must_not_be_absolute(self) -> None:
        for unsafe in ("/PerMaterial", r"C:\\PerMaterial"):
            with self.subTest(path=unsafe):
                binding = _binding()
                binding["materials"][0]["path"] = unsafe
                with self.assertRaisesRegex(RdcProjectIndexError, "absolute"):
                    upsert_project_binding(new_index(), binding)

    def test_parameter_summary_supports_id_only_but_validates_typed_entries(self) -> None:
        # The fixture contains both a typed summary and an intentionally ID-only hint.
        index = upsert_project_binding(new_index(), _binding())
        parameters = index["bindings"][0]["materials"][0]["parameters"]
        self.assertEqual({len(item) for item in parameters}, {1, 7})

        invalid = _binding()
        typed = next(
            item for item in invalid["materials"][0]["parameters"] if len(item) > 1
        )
        typed["byteSize"] = 12
        with self.assertRaisesRegex(RdcProjectIndexError, "byteSize"):
            upsert_project_binding(new_index(), invalid)

    def test_candidate_revision_is_a_strict_positive_integer(self) -> None:
        for invalid_revision in (0, -1, True, "3", _sha("not-a-revision")):
            with self.subTest(revision=invalid_revision):
                binding = _binding()
                binding["shaders"][0]["candidateRevision"] = invalid_revision
                with self.assertRaisesRegex(RdcProjectIndexError, "candidateRevision"):
                    upsert_project_binding(new_index(), binding)

    def test_material_stage_is_canonical_and_bound_to_the_same_shader_stage(self) -> None:
        unknown = _binding()
        unknown["materials"][0]["stage"] = "not-a-renderdoc-stage"
        with self.assertRaisesRegex(RdcProjectIndexError, "RenderDoc stage"):
            upsert_project_binding(new_index(), unknown)

        wrong_stage = _binding()
        wrong_stage["materials"][0]["stage"] = "vertex"
        with self.assertRaisesRegex(RdcProjectIndexError, "Shader/stage absent"):
            upsert_project_binding(new_index(), wrong_stage)

    def test_duplicate_associations_and_unknown_material_shader_are_rejected(self) -> None:
        duplicate_shader = _binding()
        alternate = copy.deepcopy(duplicate_shader["shaders"][0])
        alternate["knowledgeId"] = _id("different-knowledge")
        alternate["artifactRefs"]["projectArtifactId"] = alternate["knowledgeId"]
        duplicate_shader["shaders"].append(alternate)
        with self.assertRaisesRegex(RdcProjectIndexError, "duplicate associations"):
            upsert_project_binding(new_index(), duplicate_shader)

        duplicate_material = _binding()
        alternate_material = copy.deepcopy(duplicate_material["materials"][0])
        alternate_material["knowledgeId"] = _id("different-material-knowledge")
        duplicate_material["materials"].append(alternate_material)
        with self.assertRaisesRegex(RdcProjectIndexError, "duplicate associations"):
            upsert_project_binding(new_index(), duplicate_material)

        absent_shader = _binding()
        absent_shader["materials"][0]["shaderResourceId"] = 999
        with self.assertRaisesRegex(RdcProjectIndexError, "Shader/stage absent"):
            upsert_project_binding(new_index(), absent_shader)

    def test_record_and_binding_limits_are_enforced(self) -> None:
        too_many_events = _legacy_binding()
        too_many_events["shaders"][0]["identity"]["eventIds"] = list(
            range(1, MAX_EVENT_IDS_PER_SHADER + 2)
        )
        with self.assertRaisesRegex(RdcProjectIndexError, "eventIds"):
            _v3_binding(too_many_events)

        index = new_index()
        for number in range(MAX_PROJECT_BINDINGS):
            binding = _binding(
                "binding-%d" % number,
                project_id="%032x" % number,
                workspace_id="%032x" % number,
                with_records=False,
            )
            index = upsert_project_binding(index, binding)
        with self.assertRaisesRegex(RdcProjectIndexError, "binding limit"):
            upsert_project_binding(
                index,
                _binding(
                    "overflow",
                    project_id="%032x" % MAX_PROJECT_BINDINGS,
                    workspace_id="%032x" % MAX_PROJECT_BINDINGS,
                    with_records=False,
                ),
            )


if __name__ == "__main__":
    unittest.main()
