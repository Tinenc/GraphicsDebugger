"""Session-bound, paged frame outline and AI-authored presentation model.

The outline uses cached capture actions, not a replay of every draw.  Stage
names are explanatory annotations; EIDs, order, coverage and image provenance
remain owned by the Host.
"""
from __future__ import annotations

from collections import OrderedDict
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import re
import secrets
import threading
from typing import Any, Callable, Mapping

from .frame_flow_algorithm import algorithm_gaps, referenced_events, relevant_observation_events, validate_algorithm, write_index
from .frame_flow_presentation import channel_groups, process_graph, process_graph_gaps
from .frame_flow_visual_review import comparison_compatibility, prepare_visuals


OUTLINE_SCHEMA = "bread-frame-render-flow-outline/v1"
DOCUMENT_SCHEMA = "bread-frame-render-flow/v1"
MAX_ACTIONS = 100_000
MAX_STAGES = 256
MAX_IMAGES = 512
MAX_STAGE_IMAGES = 64
STAGE_CATEGORIES = frozenset(("depth", "shadow", "geometry", "lighting", "transparency",
                             "postprocess", "upscale", "ui", "compute", "copy", "present", "other"))
TECHNIQUE_STATUSES = frozenset(("recognized", "inferred", "unresolved", "not-observed"))
IMAGE_ANNOTATION_FIELDS = {"group", "label", "semantic", "channelMeaning", "channelGroups", "evidence", "parentImageIndex", "presentation"}
STAGE_ANNOTATION_FIELDS = {"category", "techniques", "investigation", "comparisons", "animations", "processSteps", "processGraph", "outputOmissions", "algorithm"}
_ACTION_MASK = 0x0001 | 0x0002 | 0x0004 | 0x0008 | 0x0100 | 0x0400 | 0x0800 | 0x1000 | 0x4000 | 0x8000
# renderdoc/api/replay/replay_enums.h, ActionFlags.  ExecuteIndirect and
# command-buffer nodes describe containers, not a semantic rendering pass.
_PASS_MARKER_MASK = 0x0020 | 0x0040 | 0x2000
_STRUCTURAL_CONTAINER_MASK = 0x0010 | 0x0200 | 0x1000000
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UNRESOLVED_PURPOSE = re.compile(
    r"未知功能|算法未知|具体(?:用途|功能|算法).{0,8}(?:未确认|未知|不明)|"
    r"(?:用途|功能|算法)(?:尚)?未(?:确认|确定)|"
    r"(?:unknown|unidentified|undetermined)\s+(?:purpose|function|algorithm)|"
    r"(?:purpose|function|algorithm)\s+(?:is\s+)?(?:unknown|unconfirmed)", re.I)
_UNRESOLVED_TEXTURE = re.compile(
    r"physical-attachment|未逐项(?:确认|还原)|(?:通道|分量|业务含义).{0,12}(?:未知|未确认|未还原|不明)|"
    r"(?:unknown|unconfirmed|undetermined)\s+(?:channel|component|meaning)|"
    r"(?:channel|component)\s+(?:meaning\s+)?(?:is\s+)?(?:unknown|unconfirmed)", re.I)
_IDENTIFIER_TITLE = re.compile(r"^(?:(?:EID|RID|ResourceId)\s*\d|(?:RTV|DSV|SRV|UAV)\s*\d|\d{5,}\b|(?:Stage|阶段)\s*\d*$)", re.I)


def _process_steps(value: Any, image_count: int, comparison_count: int, animation_count: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or (value and not 2 <= len(value) <= 12):
        raise ValueError("Process steps require 2 to 12 ordered visual steps")
    result = []
    counts = {"imageIndex": image_count, "comparisonIndex": comparison_count, "animationIndex": animation_count}
    for row in value:
        if not isinstance(row, Mapping) or set(row) - ({"label", "description"} | set(counts)):
            raise ValueError("Process steps accept label, description and one media reference")
        references = set(row) & set(counts)
        if len(references) != 1:
            raise ValueError("Each process step must reference exactly one image, comparison or animation")
        key = next(iter(references))
        if counts[key] == 0:
            raise ValueError("Process step references missing media")
        result.append({"label": _text(row.get("label", ""), "process label", 160, required=True),
                       "description": _text(row.get("description", ""), "process description", 600),
                       key: _integer(row[key], key, 0, counts[key] - 1)})
    return result


def _output_omissions(value: Any, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the inventory complete without requiring an unrelated image wall."""
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("Output omissions must be a bounded list of observed attachments")
    result, used, issues = [], {}, []
    for row_index, row in enumerate(value):
        with _check(issues, "outputOmissions[%d]" % row_index):
            fields = {"eventId", "resourceId", "kind", "index", "stage", "mip", "slice", "sample", "reason", "evidence"}
            if not isinstance(row, Mapping) or set(row) - fields:
                raise ValueError("Output omissions require an observed attachment, reason and evidence")
            rid = _integer(row.get("resourceId"), "omitted resourceId", 1, 0xFFFFFFFFFFFFFFFF)
            kind = _text(row.get("kind", ""), "omitted kind", 16, required=True)
            index = _integer(row.get("index"), "omitted binding index", 0, 65535)
            selected = [i for i, item in enumerate(attachments)
                        if (item.get("resourceId"), item.get("kind"), item.get("index")) == (rid, kind, index)
                        and all(item.get(key, 0 if key != "stage" else "") == row[key]
                                for key in ("eventId", "stage", "mip", "slice", "sample") if key in row)]
            if len(selected) != 1 or selected[0] in used:
                if not selected:
                    detail = "no observed match; verify the key output EID and binding"
                elif len(selected) > 1:
                    detail = "%d observed matches; specify eventId/stage/mip/slice/sample" % len(selected)
                else:
                    detail = "duplicates outputOmissions[%d]" % used[selected[0]]
                raise ValueError("Output omission must identify one unique Host-observed attachment"
                                 " (resourceId=%d, index=%d): %s" % (rid, index, detail))
            attachment = attachments[selected[0]]
            clean = {key: attachment[key] for key in ("eventId", "resourceId", "kind", "index", "stage", "mip", "slice", "sample") if key in attachment}
            clean.update(reason=_text(row.get("reason", ""), "omission reason", 600, required=True),
                         evidence=_text(row.get("evidence", ""), "omission evidence", 1200, required=True))
            result.append(clean)
            used[selected[0]] = row_index
    if issues:
        raise FrameFlowValidationError(issues)
    return result


def _integer(value: Any, field: str, low: int = 0, high: int = 0xFFFFFFFF) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("%s must be an integer between %d and %d" % (field, low, high))
    return value


def _text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("%s must be plain text of at most %d characters" % (field, limit))
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError("%s contains control characters" % field)
    result = value.strip()
    if required and not result:
        raise ValueError("%s is required" % field)
    return result


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def editable_stage(stage: Mapping[str, Any]) -> dict[str, Any]:
    """Return exactly the accepted publication input, without Host-derived fields."""
    fields = {"id", "title", "summary", "confidence", "startEventId", "endEventId", "representativeEventId"}
    proposal = {key: deepcopy(stage[key]) for key in fields | STAGE_ANNOTATION_FIELDS if key in stage}
    proposal["images"] = [{key: deepcopy(image[key]) for key in
                           {"manifestPath", "title", "description"} | IMAGE_ANNOTATION_FIELDS if key in image}
                          for image in stage.get("images", [])]
    proposal["comparisons"] = [{key: deepcopy(row[key]) for key in
                                ("leftImageIndex", "rightImageIndex", "label", "description", "relationship") if key in row}
                               for row in stage.get("comparisons", [])]
    proposal["animations"] = [{key: deepcopy(row[key]) for key in
                               ("label", "description", "imageIndices", "frameDurationMs") if key in row}
                              for row in stage.get("animations", [])]
    return proposal


def _analysis_evidence(stage: Mapping[str, Any]) -> set[str]:
    # Track the reasoning record, not presentation or a refreshed binding list.
    # This is a review-progress check, never proof that the reasoning is correct.
    investigation = stage.get("investigation") or {}
    algorithm = stage.get("algorithm") or {}
    evidence = {_digest(investigation["evidence"])} if investigation.get("evidence", "").strip() else set()
    for key, rows in algorithm.items():
        evidence.update(_digest({key: {field: value for field, value in row.items() if field != "title"}})
                        for row in rows if row.get("evidence", "").strip())
    evidence.update(_digest(row["evidence"]) for row in stage.get("techniques", [])
                    if row.get("evidence", "").strip())
    return evidence


def validate_outline(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema") != OUTLINE_SCHEMA:
        raise ValueError("Frame outline schema is invalid")
    session = value.get("captureSessionId")
    capture = value.get("captureId", "")
    if not isinstance(session, str) or not _HEX32.fullmatch(session):
        raise ValueError("Frame outline capture session is invalid")
    if not isinstance(capture, str) or (capture and not _HEX64.fullmatch(capture)):
        raise ValueError("Frame outline capture identity is invalid")
    raw = value.get("actions")
    if not isinstance(raw, list) or len(raw) > MAX_ACTIONS:
        raise ValueError("Frame outline action count exceeds the supported bound")
    actions, seen_events, seen_order = [], set(), set()
    for row in raw:
        if not isinstance(row, Mapping):
            raise ValueError("Frame outline action must be an object")
        event = _integer(row.get("eventId"), "eventId", 1)
        order = _integer(row.get("executionIndex"), "executionIndex")
        if event in seen_events or order in seen_order:
            raise ValueError("Frame outline EIDs and execution indices must be unique")
        seen_events.add(event)
        seen_order.add(order)
        outputs = row.get("outputs", [])
        if not isinstance(outputs, list) or len(outputs) > 32:
            raise ValueError("Frame outline output attachment list is invalid")
        clean = {
            "eventId": event, "executionIndex": order,
            "parentEventId": _integer(row.get("parentEventId", 0), "parentEventId"),
            "name": _text(row.get("name", ""), "action name", 512),
            "flags": _integer(row.get("flags", 0), "flags"),
            "outputs": [_integer(item, "output resource ID", high=0xFFFFFFFFFFFFFFFF) for item in outputs],
        }
        for key in ("depthOutput", "copySource", "copyDestination"):
            clean[key] = _integer(row.get(key, 0), key, high=0xFFFFFFFFFFFFFFFF)
        for key in ("copySourceSubresource", "copyDestinationSubresource"):
            if key in row:
                if not isinstance(row[key], Mapping):
                    raise ValueError("Copy subresource must be an object")
                clean[key] = {field: _integer(row[key].get(field), "copy subresource " + field)
                              for field in ("mip", "slice", "sample")}
        if "isAction" in row:
            if not isinstance(row["isAction"], bool):
                raise ValueError("Frame outline isAction must be a boolean")
            clean["isAction"] = row["isAction"]
        if "actionKind" in row:
            clean["actionKind"] = _text(row["actionKind"], "actionKind", 32)
        if "hasWork" in row:
            if row["hasWork"] is not None and not isinstance(row["hasWork"], bool):
                raise ValueError("Frame outline hasWork must be boolean or null")
            clean["hasWork"] = row["hasWork"]
        actions.append(clean)
    actions.sort(key=lambda row: row["executionIndex"])
    by_eid = {row["eventId"]: row for row in actions}
    for row in actions:
        parent = row["parentEventId"]
        if parent and (parent not in by_eid or by_eid[parent]["executionIndex"] >= row["executionIndex"]):
            raise ValueError("Frame outline parent must precede its child")
    summary = value.get("summary")
    if not isinstance(summary, Mapping) or not isinstance(summary.get("complete"), bool):
        raise ValueError("Frame outline must report source completeness")
    total = _integer(summary.get("totalActionCount"), "totalActionCount", len(actions))
    omitted = _integer(summary.get("omittedActionCount", 0), "omittedActionCount")
    if total != len(actions) + omitted or summary["complete"] != (omitted == 0):
        raise ValueError("Frame outline completeness contradicts its action counts")
    return {
        "schema": OUTLINE_SCHEMA, "captureSessionId": session, "captureId": capture,
        "capturePath": _text(value.get("capturePath", ""), "capturePath", 32768),
        "api": _text(value.get("api", ""), "api", 64), "actions": actions,
        "frameNumber": _integer(value.get("frameNumber", 0), "frameNumber"),
        "summary": {"complete": summary["complete"], "totalActionCount": total,
                    "omittedActionCount": omitted},
    }


def _gpu_actions(outline: Mapping[str, Any]) -> list[dict[str, Any]]:
    parents = {row["parentEventId"] for row in outline["actions"] if row["parentEventId"]}
    return [row for row in outline["actions"]
            if row.get("isAction", bool(row["flags"] & _ACTION_MASK))
            and row["eventId"] not in parents]


def candidate_stages(outline: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Fold contiguous marker/attachment regions without dropping any GPU action."""
    # The validated tree is in execution order, so a parent has already been
    # visited. Resolve pass ancestry once rather than walking thousands of
    # per-draw ExecuteIndirect wrappers for each candidate.
    pass_ancestor: dict[int, dict[str, Any] | None] = {}
    for row in outline["actions"]:
        semantic_marker = (row["flags"] & _PASS_MARKER_MASK
                           and not row["flags"] & _STRUCTURAL_CONTAINER_MASK)
        pass_ancestor[row["eventId"]] = row if semantic_marker else pass_ancestor.get(row["parentEventId"])

    def action_kind(action: Mapping[str, Any]) -> str:
        native_kind = action.get("actionKind")
        if native_kind and native_kind not in ("marker", "other"):
            return str(native_kind)
        for mask, kind in ((0x0002 | 0x0008, "draw"), (0x0004 | 0x4000, "dispatch"),
                           (0x0400, "copy"), (0x0800, "resolve"), (0x0001, "clear"),
                           (0x0100, "present"), (0x1000, "generate-mips"), (0x8000, "build-acceleration")):
            if action["flags"] & mask:
                return kind
        return "other"

    groups: list[dict[str, Any]] = []
    previous = None
    for action in _gpu_actions(outline):
        parent = pass_ancestor.get(action["parentEventId"])
        pass_id = parent["eventId"] if parent else 0
        kind = action_kind(action)
        key = (pass_id, tuple(action["outputs"]), action["depthOutput"], kind)
        if not groups or key != previous:
            groups.append({"id": "stage-%d" % (len(groups) + 1),
                           "title": (parent or action)["name"][:160] or "EID %d" % action["eventId"],
                           "summary": "", "confidence": "captured",
                           "startEventId": action["eventId"], "endEventId": action["eventId"],
                           "representativeEventId": action["eventId"], "actionCount": 0,
                           "parentEventId": pass_id, "outputs": list(action["outputs"]),
                           "depthOutput": action["depthOutput"], "actionKind": kind})
        group = groups[-1]
        group["endEventId"] = action["eventId"]
        group["representativeEventId"] = action["eventId"]
        group["actionCount"] += 1
        previous = key
    return groups


def _techniques(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("Each stage supports at most 32 technique findings")
    result = []
    for row in value:
        if not isinstance(row, Mapping) or set(row) - {"name", "status", "evidence", "summary"}:
            raise ValueError("Technique findings accept name, status, evidence and summary")
        status = row.get("status", "unresolved")
        if not isinstance(status, str) or status not in TECHNIQUE_STATUSES:
            raise ValueError("Technique status must be recognized, inferred, unresolved or not-observed")
        result.append({"name": _text(row.get("name", ""), "technique name", 160, required=True),
                       "status": status, "evidence": _text(row.get("evidence", ""), "technique evidence", 1200),
                       "summary": _text(row.get("summary", ""), "technique summary", 1200)})
    return result


def _investigation(value: Any) -> dict[str, str]:
    allowed = {"status", "evidence", "unresolvedReason", "nextAction"}
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ValueError("Investigation accepts status, evidence, unresolvedReason and nextAction")
    status = value.get("status", "not-investigated")
    if status not in ("investigated", "partial", "not-investigated"):
        raise ValueError("Investigation status must be investigated, partial or not-investigated")
    return {"status": status, **{key: _text(value.get(key, ""), "investigation " + key, 1200)
                                 for key in ("evidence", "unresolvedReason", "nextAction")}}


def _comparisons(value: Any, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    image_count = len(images)
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError("Each stage supports at most 8 image comparisons")
    result = []
    for row in value:
        if not isinstance(row, Mapping) or set(row) - {"leftImageIndex", "rightImageIndex", "label", "description", "relationship"}:
            raise ValueError("Comparisons reference leftImageIndex/rightImageIndex and optional explanatory text")
        left = _integer(row.get("leftImageIndex"), "leftImageIndex", 0, max(0, image_count - 1))
        right = _integer(row.get("rightImageIndex"), "rightImageIndex", 0, max(0, image_count - 1))
        if image_count < 2 or left == right:
            raise ValueError("Comparisons require two different images from the same stage")
        left_image, right_image = images[left], images[right]
        reasons = comparison_compatibility(left_image, right_image)
        result.append({"leftImageIndex": left, "rightImageIndex": right,
                       "label": _text(row.get("label", ""), "comparison label", 160),
                       "description": _text(row.get("description", ""), "comparison description", 1200),
                       "relationship": _text(row.get("relationship", ""), "comparison relationship", 160),
                       "visualComparable": not reasons, "comparisonWarnings": reasons,
                       "differentResource": left_image.get("resourceId") != right_image.get("resourceId"),
                       "numericDifferenceEvidence": False})
    return result


def _animations(value: Any, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError("Each stage supports at most 8 animations")
    result = []
    for row in value:
        if not isinstance(row, Mapping) or set(row) - {"label", "description", "imageIndices", "frameDurationMs"}:
            raise ValueError("Animations accept label, description, imageIndices and frameDurationMs")
        indices = row.get("imageIndices")
        if not isinstance(indices, list) or not 2 <= len(indices) <= 16 or not images:
            raise ValueError("Animations require 2 to 16 verified image indices")
        indices = [_integer(index, "animation image index", 0, len(images) - 1) for index in indices]
        if len(set(indices)) != len(indices):
            raise ValueError("Animation image indices must be unique")
        frames = [images[index] for index in indices]
        if len({(frame.get("width"), frame.get("height")) for frame in frames}) != 1:
            raise ValueError("Animation frames must have identical dimensions")
        mappings = [{key: frame.get("displayMapping", {}).get(key)
                     for key in ("rangeMode", "blackPoint", "whitePoint", "depthInterpretation")}
                    for frame in frames]
        if any(mapping != mappings[0] for mapping in mappings[1:]) or len({frame.get("channel") for frame in frames}) != 1:
            raise ValueError("Animation frames must use the same channels and display range")
        if len({frame.get("displayMapping", {}).get("valueDecode") or "native" for frame in frames}) != 1:
            raise ValueError("Animation frames must use the same value decoding")
        result.append({"label": _text(row.get("label", ""), "animation label", 160, required=True),
                       "description": _text(row.get("description", ""), "animation description", 1200, required=True),
                       "imageIndices": indices,
                       "frameDurationMs": _integer(row.get("frameDurationMs", 400), "frameDurationMs", 100, 5000)})
    return result


def _channel_parents(images: list[dict[str, Any]]) -> None:
    for index, image in enumerate(images):
        if "parentImageIndex" not in image:
            continue
        parent_index = _integer(image["parentImageIndex"], "parentImageIndex", 0, len(images) - 1)
        parent = images[parent_index]
        if index == parent_index or image["group"] != "channels" or parent["group"] not in ("outputs", "inputs"):
            raise ValueError("Channel parent must reference a separate output or input image")
        keys = ("eventId", "resourceId")
        mapping_keys = ("mip", "slice", "sample", "flipY")
        if (any(image.get(key) != parent.get(key) for key in keys) or
                (image.get("displayMapping", {}).get("valueDecode") or "native") !=
                (parent.get("displayMapping", {}).get("valueDecode") or "native") or
                any(image.get("displayMapping", {}).get(key, 0) != parent.get("displayMapping", {}).get(key, 0)
                    for key in mapping_keys)):
            raise ValueError("Channel parent must reference the same event, texture and subresource")


def _draw_dispatch(row: Mapping[str, Any]) -> bool:
    return row.get("actionKind") in ("draw", "dispatch") or bool(row["flags"] & (0x0002 | 0x0004 | 0x0008 | 0x4000))


def navigation_action(member_rows: list[dict[str, Any]], observations=None) -> tuple[dict[str, Any], str]:
    """Prefer the final effective graphics draw, then compute, then utility.

    Unknown workload remains unknown. Never infer indirect arguments from an
    action's display name, and do not treat an explicit zero dispatch as work.
    """
    def effective(row):
        obs = (observations or {}).get(str(row["eventId"]), {})
        proof = obs.get("indirectWorkload", (obs.get("details") or {}).get("indirectWorkload", {}))
        return row.get("hasWork") is not False and not (
            proof.get("source") in {"native-structured-indirect-arguments", "event-bound-indirect-arguments"} and
            proof.get("status") == "resolved" and proof.get("eventId") == row["eventId"] and
            proof.get("nonZeroWorkload") is False)
    active = [row for row in member_rows if effective(row)]
    graphics = [row for row in active if row.get("actionKind") == "draw" or row["flags"] & (0x0002 | 0x0008)]
    if graphics:
        return graphics[-1], "last-draw"
    compute = [row for row in active if row.get("actionKind") == "dispatch" or row["flags"] & (0x0004 | 0x4000)]
    if compute:
        return compute[-1], "last-dispatch"
    utility = [row for row in active if not _draw_dispatch(row)]
    if utility:
        return utility[-1], "last-utility"
    return member_rows[-1], "no-effective-work"


def _has_rendered_texture_transfer(stage: Mapping[str, Any], observations: Mapping[str, Any]) -> bool:
    """Choose a visual explanation from observed transfers, never draw count.

    Independent MRTs and repeated accumulation into one target are not a chain.
    A static material texture has no declared current-frame producer. Bindings
    only establish a possible transfer; algorithm_gaps checks its explanation.
    """
    def outputs(event: int) -> set[int]:
        return {item.get("resourceId") for item in observations.get(str(event), {}).get("expectedOutputs", [])
                if item.get("resourceId") and item.get("resourceType") != "buffer"
                and (item.get("kind") in ("rtv", "dsv") or
                     str(item.get("resourceType", "")).lower().startswith("texture"))}

    def consumes(event: int, resource: int) -> bool:
        observation = observations.get(str(event), {})
        return any(item.get("resourceId") == resource for key in ("textureInputs", "inputs")
                   for item in observation.get(key, []))

    algorithm = stage.get("algorithm") or {}
    for row in algorithm.get("inputs", []):
        producer, event, resource = row.get("producerEventId"), row["eventId"], row["resourceId"]
        if (producer and resource in outputs(producer) and consumes(event, resource)
                and outputs(event) - {resource}):
            return True
    for row in algorithm.get("integration", []):
        resource, target = row["resourceId"], row.get("targetResourceId")
        if (row.get("mode") == "downstream" and target and target != resource
                and resource in outputs(row["sourceEventId"])
                and consumes(row["eventId"], resource) and target in outputs(row["eventId"])):
            return True
    return False


def _stage_quality(stage: Mapping[str, Any], last_shader_action: Mapping[str, Any] | None,
                   observations: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Publish useful drafts while making missing investigation actionable."""
    gaps = []
    def gap(code: str, message: str, next_action: str) -> None:
        gaps.append({"code": code, "stageId": stage["id"], "message": message, "nextAction": next_action})
    if _IDENTIFIER_TITLE.search(stage["title"]) or re.search(r"(?:绘制|渲染)(?:了)?(?:大量|很多)(?:的)?几何", stage["title"]):
        gap("nonsemantic-stage-title", "The stage title names an ID or draw volume instead of its rendering function.",
            "Name the rendering function supported by this capture's evidence; do not copy a fixed list of stage names.")
    if not stage["summary"]:
        gap("missing-stage-explanation", "The stage has no plain-language explanation of its rendering function.",
            "Briefly explain what this module does and how it contributes to the frame; use only details needed to understand it.")
    if _UNRESOLVED_PURPOSE.search(stage["title"] + " " + stage["summary"]):
        gap("unresolved-stage-explanation", "The explanation still contains an unresolved function or algorithm.",
            "Read the relevant Shader code and input/output evidence; replace the placeholder with supported calculations, or retain a specific unresolved finding and continue investigation.")
    # A graph is a presentation choice, not proof of algorithm completeness.
    # Keep checking declared resource paths below; do not demand a redundant
    # text-only graph when the explanation and representative images suffice.
    for index, image in enumerate(stage["images"]):
        description = image["description"]
        if not image["title"] or _IDENTIFIER_TITLE.search(image["title"]):
            gap("nonsemantic-texture-title", "Image %d needs a meaningful texture name instead of an EID or attachment ID." % index,
                "Use its investigated role as the title, such as BaseColor, normal, scene depth or Bloom blur; leave IDs in metadata.")
        if _UNRESOLVED_TEXTURE.search(" ".join(image.get(key, "") for key in ("title", "description", "semantic", "channelMeaning"))):
            gap("unresolved-texture-meaning", "Image %d still has unresolved or placeholder channel meanings." % index,
                "Trace its writer and consuming EIDs, inspect the relevant Shader calculations, and explain the actual contents and channel groups.")
        if not image["channelMeaning"]:
            gap("missing-texture-channel-meaning", "Image %d does not explain the channels used by this calculation." % index,
                "Record meaningful channel groups, such as RGB color and A coverage, supported by producer or consumer Shader evidence.")
        if (image["semantic"] or image["channelMeaning"]) and not image["evidence"]:
            gap("missing-image-semantic-evidence", "Image %d has semantic labels without supporting evidence." % index,
                "Explain how Shader calculations, resource usage or asset names support the image/channel interpretation.")
        metadata_only = re.fullmatch(
            r"(?:DXGI_FORMAT_|VK_FORMAT_)?(?:[RGBAD]\d+)+(?:_[A-Z0-9_]+)?"
            r"(?:[,，;；\s]+\d+\s*[×x]\s*\d+)?[.。]?", description)
        if not description or "undefined" in description or metadata_only:
            gap("missing-texture-explanation", "Image %d has no useful explanation of what it stores and where it is used." % index,
                "Add a plain-language texture purpose and downstream use; keep format and dimensions in metadata.")
    present_event = stage["outputCoverage"].get("presentEventId")
    if present_event and not any(image.get("attachmentKind") == "present" and image.get("eventId") == present_event
                                 and image.get("channel") == "rgba" and image.get("group") == "outputs"
                                 and image.get("resourceId") == stage["outputCoverage"]["presentResourceId"]
                                 for image in stage["images"]):
        gap("missing-final-present-preview", "The frame's final presented image has not been captured.",
            "Capture the exact Present event with attachment_kind=present; an independent UI layer is not the final composited frame.")
    if last_shader_action is None:
        return gaps
    if stage["outputCoverage"]["source"] != "stage-inspection":
        gap("missing-stage-inspection", "The stage has no Host-recorded pipeline and resource inventory.",
            "Call inspect_frame_render_stage for this stage, reuse its Shader/output evidence, then update the investigation.")
    elif stage["outputCoverage"]["inventoryComplete"] is False:
        gap("stage-inventory-incomplete", "Some stage resource bindings could not be inspected.",
            "Read outputCoverage.bindingErrors, repair or retry the failed binding inspection, and retain a partial finding if unavailable.")
    previews_unavailable = (stage["outputCoverage"]["source"] == "stage-inspection"
                            and stage["outputCoverage"]["nonImageOutputs"]
                            and not stage["outputCoverage"]["expectedResourceIds"])
    if not stage["images"] and not previews_unavailable:
        gap("missing-stage-preview", "This rendering stage has no captured preview.",
            "Capture the final draw/dispatch outputs, or explain why no image can be obtained.")
    if stage["outputCoverage"]["missingResourceIds"] or stage["outputCoverage"]["missingAttachments"]:
        gap("missing-output-attachments", "Some known color/depth attachments have no full-channel preview.",
            "Capture outputs needed to explain this stage, or record exact outputOmissions with relevance evidence for auxiliary bindings.")
    investigation = stage["investigation"]
    if investigation["status"] == "not-investigated":
        gap("stage-not-investigated", "The stage has not recorded an investigation.",
            "Inspect representative pipeline state, Shader and relevant resources; record the evidence and conclusion.")
    elif investigation["status"] == "partial":
        gap("stage-investigation-partial", investigation["unresolvedReason"] or "The stage investigation is incomplete.",
            investigation["nextAction"] or "Investigate the remaining question using a different relevant evidence source.")
    elif not investigation["evidence"]:
        gap("missing-investigation-evidence", "The investigated status has no supporting evidence.",
            "Record the concrete EIDs, Shader observations or resource comparisons supporting the stage explanation.")
    if stage["confidence"] == "unknown":
        gap("unresolved-stage-purpose", investigation["unresolvedReason"] or "The rendering purpose remains unresolved.",
            investigation["nextAction"] or "Inspect the stage Shader and compare its inputs and outputs; retain uncertainty if evidence remains unavailable.")
    # A useful functional explanation need not claim a named technique. Check
    # the techniques actually claimed, rather than requiring a checklist entry.
    for finding in stage["techniques"]:
        if finding["status"] == "unresolved":
            gap("unresolved-technique", "Technique remains unresolved: " + finding["name"],
                investigation["nextAction"] or "Inspect the relevant Shader calculation or input/output comparison and update this finding.")
        elif not finding["evidence"]:
            gap("missing-technique-evidence", "Technique finding lacks evidence: " + finding["name"],
                "Record the concrete observations and the scope of this finding; names and colors alone are insufficient.")
    return gaps


class FrameFlowValidationError(ValueError):
    """Bounded, indexed issues from the cached outline and observed attachments."""

    def __init__(self, issues):
        self.issues = issues[:64]
        self.issue_count = len(issues)
        super().__init__("%d frame diagram validation issue(s): %s" % (
            self.issue_count, "; ".join("%s: %s" % (issue["path"], issue["message"]) for issue in self.issues)))


@contextmanager
def _check(issues, path):
    try:
        yield
    except FrameFlowValidationError as error:
        issues.extend({"path": path + "." + row["path"], "message": row["message"]}
                      for row in error.issues)
    except ValueError as error:
        issues.append({"path": path, "message": str(error)[:400]})


def _stage_outputs(algorithm, navigation_event, by_eid, observations):
    # The final navigable draw is often only a copy/cleanup. Review the
    # actual key calculations, keeping navigation independent of evidence.
    output_events = {row["eventId"] for row in (algorithm or {}).get("outputs", [])} or {navigation_event}
    output_events = sorted(output_events, key=lambda event: by_eid[event]["executionIndex"])
    observed_outputs, binding_errors, observed_event_count = [], [], 0
    inventory_complete = True
    for event in output_events:
        observation = (observations or {}).get(str(event), {})
        event_outputs = observation.get("expectedOutputs") if isinstance(observation, Mapping) else None
        if not isinstance(event_outputs, list):
            inventory_complete = False
            continue
        observed_event_count += 1
        errors = deepcopy(observation.get("bindingErrors", []))
        binding_errors.extend(errors)
        inventory_complete = inventory_complete and observation.get("bindingsComplete", True) is not False and not errors
        for item in event_outputs:
            item = deepcopy(item)
            if len(output_events) > 1 or event != navigation_event:
                item["eventId"] = event
            observed_outputs.append(item)
    if not observed_event_count:
        inventory_complete = None
    expected_outputs = sorted({value for event in output_events for value in
                               by_eid[event].get("outputs", []) + [by_eid[event].get("depthOutput", 0)] if value})
    non_image_outputs = []
    expected_attachments = []
    if observed_event_count:
        expected_attachments = [deepcopy(item) for item in observed_outputs
            if item.get("captureSupported") is not False and item.get("resourceType", "texture") != "buffer"]
        expected_outputs = sorted({item["resourceId"] for item in expected_attachments})
        non_image_outputs = [deepcopy(item) for item in observed_outputs
            if item.get("captureSupported") is False or item.get("resourceType") == "buffer"]
    return {"expectedResourceIds": expected_outputs, "expectedAttachments": expected_attachments,
            "keyOutputEventIds": output_events, "nonImageOutputs": non_image_outputs,
            "inventoryComplete": inventory_complete, "bindingErrors": binding_errors,
            "source": "stage-inspection" if observed_event_count else "action-outline"}


def _validate_stages(stages, by_eid, gpu, gpu_order, observations, *, title, summary):
    if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGES:
        raise ValueError("Provide between 1 and %d chronological stages; combine adjacent stages if needed" % MAX_STAGES)
    issues, validated, ids = [], [], set()
    previous_end = -1
    image_count = 0
    with _check(issues, "title"):
        _text(title, "diagram title", 160)
    with _check(issues, "summary"):
        _text(summary, "diagram summary", 2400)
    for index, stage in enumerate(stages):
        path = "stages[%d]" % index
        if not isinstance(stage, Mapping):
            issues.append({"path": path, "message": "Each frame stage must be an object"})
            continue
        allowed = {"id", "title", "summary", "confidence", "startEventId", "endEventId",
                   "representativeEventId", "images"} | STAGE_ANNOTATION_FIELDS
        with _check(issues, path):
            if set(stage) - allowed:
                raise ValueError("Unsupported stage fields: %s" % ", ".join(sorted(set(stage) - allowed)))
        with _check(issues, path + ".id"):
            stage_id = _text(stage.get("id", "stage-%d" % (index + 1)), "stage id", 80, required=True)
            if not re.fullmatch(r"[a-zA-Z0-9_.-]+", stage_id) or stage_id in ids:
                raise ValueError("Stage IDs must be unique plain identifiers")
            ids.add(stage_id)
        with _check(issues, path + ".title"):
            stage_title = _text(stage.get("title", ""), "stage title", 160, required=True)
        with _check(issues, path + ".summary"):
            stage_summary = _text(stage.get("summary", ""), "stage summary", 1200)
        confidence = stage.get("confidence", "inferred")
        with _check(issues, path + ".confidence"):
            if confidence not in ("captured", "inferred", "unknown"):
                raise ValueError("Stage confidence must be captured, inferred or unknown")
        category = stage.get("category", "other")
        with _check(issues, path + ".category"):
            if not isinstance(category, str) or category not in STAGE_CATEGORIES:
                raise ValueError("Stage category must be one of: " + ", ".join(sorted(STAGE_CATEGORIES)))
        with _check(issues, path + ".techniques"):
            techniques = _techniques(stage.get("techniques", []))
        with _check(issues, path + ".investigation"):
            investigation = _investigation(stage.get("investigation", {}))
        with _check(issues, path + ".images"):
            images = stage.get("images", [])
            if not isinstance(images, list) or len(images) > MAX_STAGE_IMAGES:
                raise ValueError("Each stage supports at most %d evidence images" % MAX_STAGE_IMAGES)
            image_count += len(images)
            if image_count > MAX_IMAGES:
                raise ValueError("Frame diagram supports at most %d evidence images" % MAX_IMAGES)
        member_rows = []
        with _check(issues, path + ".bounds"):
            first = _integer(stage.get("startEventId"), "startEventId", 1)
            last = _integer(stage.get("endEventId"), "endEventId", 1)
            if first not in by_eid or last not in by_eid:
                raise ValueError("Stage bounds must be actual EIDs in this frame")
            begin, end = by_eid[first]["executionIndex"], by_eid[last]["executionIndex"]
            if end < begin or begin <= previous_end:
                raise ValueError("Frame stages must be chronological, non-overlapping ranges")
            previous_end = end
            member_rows = gpu[bisect_left(gpu_order, begin):bisect_right(gpu_order, end)]
            if not member_rows:
                raise ValueError("A stage must contain a draw, dispatch, copy, clear, resolve or present action")
        if not member_rows:
            continue
        members = [row["eventId"] for row in member_rows]
        with _check(issues, path + ".representativeEventId"):
            representative = _integer(stage.get("representativeEventId", members[-1]), "representativeEventId", 1)
            if representative not in members:
                raise ValueError("Representative EID must be a GPU action inside its stage")
        navigation_row, navigation_status = navigation_action(member_rows, observations)
        algorithm = None
        with _check(issues, path + ".algorithm"):
            algorithm = validate_algorithm(stage.get("algorithm"), set(members), by_eid)
        # If the submitted algorithm is invalid, its key outputs are unknown.
        if algorithm is None and issues and issues[-1]["path"] == path + ".algorithm":
            continue
        outputs = _stage_outputs(algorithm, navigation_row["eventId"], by_eid, observations)
        omissions = []
        with _check(issues, path):
            omissions = _output_omissions(stage.get("outputOmissions", []), outputs["expectedAttachments"])
        if issues:
            continue
        header = {"id": stage_id, "title": stage_title, "summary": stage_summary, "confidence": confidence,
                  "category": category, "techniques": techniques, "investigation": investigation,
                  "startEventId": first, "endEventId": last, "eventIds": members, "algorithm": algorithm}
        validated.append((header, member_rows, end, navigation_row, navigation_status, outputs, omissions))
    if issues:
        raise FrameFlowValidationError(issues)
    return validated


def validate_proposals(outline, stages, *, title="", summary="", observations=None):
    """Validate bounded author inputs against cached evidence, with no image resolution or replay."""
    by_eid = {row["eventId"]: row for row in outline["actions"]}
    gpu = _gpu_actions(outline)
    _validate_stages(stages, by_eid, gpu, [row["executionIndex"] for row in gpu],
                     observations or {}, title=title, summary=summary)


def build_document(outline: Mapping[str, Any], flow_id: str, snapshot_id: str,
                   stages: list[dict[str, Any]], *, title: str = "", summary: str = "",
                   image_resolver: Callable[[Mapping[str, Any], set[int]], dict[str, Any]] | None = None,
                   observations: Mapping[str, Any] | None = None) -> dict[str, Any]:
    rows = outline["actions"]
    by_eid = {row["eventId"]: row for row in rows}
    gpu = _gpu_actions(outline)
    gpu_order = [row["executionIndex"] for row in gpu]
    clean, covered = [], set()
    validated = _validate_stages(stages, by_eid, gpu, gpu_order, observations or {},
                                 title=title, summary=summary)
    writers = write_index(by_eid, observations or {})
    for stage, validation in zip(stages, validated):
        header, member_rows, end, navigation_row, navigation_status, outputs, omissions = validation
        members = header["eventIds"]
        algorithm = header["algorithm"]
        navigation_event = navigation_row["eventId"]
        last_shader_action = navigation_row if navigation_status in ("last-draw", "last-dispatch") else None
        output_events = outputs["keyOutputEventIds"]
        expected_attachments = outputs["expectedAttachments"]
        expected_outputs = outputs["expectedResourceIds"]
        images = stage.get("images", [])
        resolved = []
        for image in images:
            if not isinstance(image, Mapping) or set(image) - ({"manifestPath", "manifest_path", "title", "description"} | IMAGE_ANNOTATION_FIELDS):
                raise ValueError("Images accept only a managed manifestPath, title and description")
            if image_resolver is None:
                raise ValueError("An evidence image resolver is required")
            evidence_members = (set(row["eventId"] for row in gpu[:bisect_right(gpu_order, end)])
                                if image.get("group") == "inputs" else set(members))
            result = image_resolver(image, evidence_members)
            result["title"] = _text(image.get("title", result.get("title", "")), "image title", 160)
            result["description"] = _text(image.get("description", ""), "image description", 1200)
            group = image.get("group", "outputs")
            if group not in ("outputs", "channels", "inputs", "animation-frames"):
                raise ValueError("Image group must be outputs, channels, inputs or animation-frames")
            result["group"] = group
            presentation = image.get("presentation", "primary")
            if presentation not in ("primary", "detail"):
                raise ValueError("Image presentation must be primary or detail")
            result["presentation"] = presentation
            result.pop("parentImageIndex", None)
            if "parentImageIndex" in image:
                result["parentImageIndex"] = image["parentImageIndex"]
            for key in ("label", "semantic", "channelMeaning", "evidence"):
                result[key] = _text(image.get(key, ""), "image " + key, 1200 if key == "evidence" else 160)
            if "channelGroups" in image:
                result["channelGroups"] = channel_groups(image["channelGroups"], result.get("componentCount", 0))
            resolved.append(result)
        _channel_parents(resolved)
        represented_outputs = sorted({image["resourceId"] for image in resolved
            if image.get("resourceId") and image["group"] == "outputs"
            and image.get("eventId") in output_events and image.get("channel") in ("rgba", "depth")})
        missing_outputs = sorted(set(expected_outputs) - set(represented_outputs))
        def attachment_has_preview(attachment: Mapping[str, Any]) -> bool:
            for image in resolved:
                if (image["group"] != "outputs" or image.get("eventId") != attachment.get("eventId", navigation_event)
                        or image.get("channel") not in ("rgba", "depth")
                        or image.get("resourceId") != attachment["resourceId"]
                        or image.get("attachmentKind") != attachment["kind"]
                        or image.get("attachmentIndex") != attachment["index"]):
                    continue
                mapping = image.get("displayMapping", {})
                expected_stage = attachment.get("stage") or (
                    "fragment" if attachment["kind"] in ("rtv", "dsv") else "")
                image_stage = image.get("attachmentStage") or (
                    "fragment" if image.get("attachmentKind") in ("rtv", "dsv") else "")
                if expected_stage != image_stage:
                    continue
                if all(key not in attachment or mapping.get(key) == attachment[key]
                       for key in ("mip", "slice", "sample")):
                    return True
            return False
        def omitted(attachment: Mapping[str, Any]) -> bool:
            return any(all(attachment.get(key) == row.get(key) for key in
                           ("eventId", "resourceId", "kind", "index", "stage", "mip", "slice", "sample")) for row in omissions)
        missing_attachments = [item for item in expected_attachments
                               if not attachment_has_preview(item) and not omitted(item)]
        if expected_attachments:
            missing_outputs = sorted({item["resourceId"] for item in missing_attachments})
        comparisons = _comparisons(stage.get("comparisons", []), resolved)
        animations = _animations(stage.get("animations", []), resolved)
        presents = [row for row in member_rows if row["flags"] & 0x0100 and row.get("copyDestination")]
        clean_stage = {**header, "representativeEventId": navigation_event, "images": resolved,
                      "navigationEventId": navigation_event, "navigationPolicy": "last-effective-draw-then-dispatch-or-utility",
                      "navigationStatus": navigation_status, "navigationHasWork": navigation_row.get("hasWork"),
                      "navigationActionKind": navigation_row.get("actionKind") or (
                          "draw" if navigation_row["flags"] & (0x0002 | 0x0008) else
                          "dispatch" if navigation_row["flags"] & (0x0004 | 0x4000) else "utility"),
                      "comparisons": comparisons, "animations": animations,
                      "processSteps": _process_steps(stage.get("processSteps", []), len(resolved), len(comparisons), len(animations)),
                      "processGraph": process_graph(stage.get("processGraph"), resolved, comparisons, animations, set(members),
                                                    allowed_events=set(members) | referenced_events(algorithm)),
                      "outputOmissions": omissions,
                      "outputCoverage": {**outputs, "expectedResourceIds": expected_outputs,
                                         "representedResourceIds": represented_outputs,
                                         "missingResourceIds": missing_outputs, "missingAttachments": missing_attachments,
                                         "omittedAttachments": omissions,
                                         "presentEventId": presents[-1]["eventId"] if presents else 0,
                                         "presentResourceId": presents[-1]["copyDestination"] if presents else 0,
                                         "status": "missing" if missing_outputs or missing_attachments else "complete"
                                         if expected_outputs or outputs["source"] == "stage-inspection" else "not-known-from-outline"}}
        clean_stage["visualReview"] = prepare_visuals(clean_stage, by_eid, observations or {})
        clean_stage["qualityGaps"] = _stage_quality(clean_stage, last_shader_action, observations or {})
        clean_stage["qualityGaps"].extend(algorithm_gaps(clean_stage, by_eid, observations or {}, writers, flow_id=flow_id))
        clean_stage["qualityGaps"].extend(process_graph_gaps(clean_stage, by_eid, observations or {}))
        animated = {index for animation in clean_stage["animations"] for index in animation["imageIndices"]}
        if any(image["group"] == "animation-frames" and index not in animated for index, image in enumerate(resolved)):
            raise ValueError("Every animation-frames image must be referenced by an animation")
        clean.append(clean_stage)
        covered.update(members)
    source = outline["summary"]
    quality_gaps = [gap for stage in clean for gap in stage["qualityGaps"]]
    # Keep omissions actionable even when every submitted card passes its own
    # checks. The timeline is ordered by execution, never by numeric EID gaps.
    # These are investigation ranges, not proposed semantic stage boundaries.
    uncovered_ranges = []
    pending_range = None
    action_offsets = {row["eventId"]: offset for offset, row in enumerate(rows)}
    for row in gpu:
        if row["eventId"] in covered:
            pending_range = None
            continue
        if pending_range is None:
            pending_range = {"startEventId": row["eventId"], "endEventId": row["eventId"],
                             "actionCount": 0, "actionOffset": action_offsets[row["eventId"]]}
            uncovered_ranges.append(pending_range)
        pending_range["endEventId"] = row["eventId"]
        pending_range["actionCount"] += 1
    if uncovered_ranges:
        quality_gaps.append({
            "code": "unrepresented-frame-actions", "stageId": "",
            "message": "Some frame actions are not represented in the diagram.",
            "nextAction": "Read the uncovered action ranges and investigate their purposes; extend or split stages only where the evidence supports a shared function. A publication failure does not narrow the requested frame scope.",
            "omittedActionCount": len(gpu) - len(covered),
            "uncoveredRangeCount": len(uncovered_ranges),
            "uncoveredRanges": uncovered_ranges[:64],
            "rangesTruncated": len(uncovered_ranges) > 64,
            "recoveryActions": [{"tool": "get_frame_render_flow", "arguments": {
                "flow_id": flow_id, "section": "actions", "offset": row["actionOffset"], "limit": 200}}
                for row in uncovered_ranges[:8]],
        })
    if not source["complete"]:
        quality_gaps.append({
            "code": "incomplete-action-outline", "stageId": "",
            "message": "The frame action outline is incomplete.",
            "nextAction": "Inspect source completeness and the collection limit before claiming whole-frame coverage; a last page of retained actions does not establish that omitted source actions were analyzed.",
            "sourceOmittedActionCount": source["omittedActionCount"],
        })
    used_observations = {}
    used_events = {stage["navigationEventId"] for stage in clean}
    for stage in clean:
        used_events.update(relevant_observation_events(stage["algorithm"], by_eid, writers))
    indirect_proofs = {}
    for event in by_eid:
        observation = (observations or {}).get(str(event))
        if not isinstance(observation, Mapping):
            continue
        details = observation.get("details")
        decoded = observation.get("indirectWorkload", (details if isinstance(details, Mapping) else {}).get("indirectWorkload"))
        if (not isinstance(decoded, Mapping) or decoded.get("eventId") != event or
                decoded.get("source") not in {"native-structured-indirect-arguments", "event-bound-indirect-arguments"}):
            continue
        # Preserve the small native receipt, not the full stage/API inspection.
        # These values must survive a save/reload even when the outline still
        # has unknown work and the receipt came from a later investigation.
        indirect_proofs[event] = {field: deepcopy(decoded[field]) for field in (
            "status", "source", "eventId", "chunkIndex", "commandIndex", "argumentIndex",
            "argumentType", "nonZeroWorkload") if field in decoded}
        if decoded.get("source") == "event-bound-indirect-arguments":
            receipt = decoded.get("bufferEvidence") or {}
            indirect_proofs[event]["bufferEvidence"] = {field: deepcopy(receipt[field]) for field in (
                "resourceId", "eventId", "captureSessionId", "offset", "length", "sha256", "capturePhase",
                "replayEventId", "restoredEventId") if field in receipt}
        workload = decoded.get("workload")
        if isinstance(workload, Mapping):
            indirect_proofs[event]["workload"] = {field: deepcopy(workload[field]) for field in (
                "vertexCount", "indexCount", "instanceCount", "groupCountX", "groupCountY", "groupCountZ")
                if field in workload}
        if decoded.get("status") == "resolved" and decoded.get("nonZeroWorkload") is False:
            # Zero-work events are deliberately absent from the writer index.
            # Retain that negative proof too, or reload invents a possible write.
            used_events.add(event)
    for event in sorted(used_events, key=lambda event: by_eid[event]["executionIndex"]):
        key = str(event)
        observation = (observations or {}).get(key)
        if (isinstance(observation, Mapping) and
                (isinstance(observation.get("expectedOutputs"), list) or event in indirect_proofs)):
            used_observations[key] = {"captureSessionId": outline["captureSessionId"],
                                      "evidence": _text(observation.get("evidence", ""), "observation evidence", 2400)}
            for field in ("inventoryVersion", "expectedOutputs", "bindingsComplete", "bindingErrors", "textureInputs", "inputs", "shaders",
                          "sourceCaptureSessionId", "evidenceFreshness", "currentReplayVerified"):
                if field in observation:
                    used_observations[key][field] = deepcopy(observation[field])
            if event in indirect_proofs:
                used_observations[key]["indirectWorkload"] = indirect_proofs[event]
    return {"schema": DOCUMENT_SCHEMA, "schemaVersion": 2, "flowId": flow_id, "snapshotId": snapshot_id,
            **{key: outline[key] for key in ("captureSessionId", "captureId", "capturePath", "api")},
            "frameNumber": outline.get("frameNumber", 0),
            "title": _text(title, "diagram title", 160), "summary": _text(summary, "diagram summary", 2400),
            "coverage": {"totalActionCount": len(gpu), "coveredActionCount": len(covered),
                         "omittedActionCount": len(gpu) - len(covered),
                         "uncoveredRangeCount": len(uncovered_ranges),
                         "uncoveredRanges": uncovered_ranges[:64],
                         "rangesTruncated": len(uncovered_ranges) > 64,
                         "complete": source["complete"] and len(covered) == len(gpu),
                         "sourceComplete": source["complete"], "totalSourceActionCount": source["totalActionCount"],
                         "sourceOmittedActionCount": source["omittedActionCount"],
                         "notes": "Stage arrows show execution order. Thumbnails are captured attachments; semantic labels may be inferred."},
            "quality": {"analysisComplete": not quality_gaps and source["complete"] and len(covered) == len(gpu),
                        "gapCount": len(quality_gaps), "gaps": quality_gaps,
                        "investigatedStageCount": sum(stage["investigation"]["status"] == "investigated" for stage in clean),
                        "publicationAllowed": True, "completenessIsNotProof": True},
            "observations": used_observations,
            "stages": clean}


class FrameRenderFlowStore:
    """Small bounded Host cache; pagination and publication never recollect a frame."""
    def __init__(self, capacity: int = 4):
        self._records: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self._drafts: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
        self.capacity = capacity

    def prepare(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        outline = validate_outline(raw)
        snapshot = _digest(outline)
        flow_id = "flow-" + snapshot[:32]
        with self._lock:
            record = self._records.get(flow_id)
            if record is None:
                record = {"flowId": flow_id, "snapshotId": snapshot, "outline": outline,
                          "candidates": candidate_stages(outline), "document": None, "observations": {},
                          "observationRevision": 0, "observationRevisions": {}}
                self._records[flow_id] = record
            self._records.move_to_end(flow_id)
            while len(self._records) > self.capacity:
                evicted, _ = self._records.popitem(last=False)
                self._discard_drafts(evicted)
            return deepcopy(record)

    def get(self, session: str, flow_id: str = "", *, copy_record: bool = True) -> dict[str, Any]:
        with self._lock:
            if not flow_id:
                flow_id = next((key for key, value in reversed(self._records.items())
                                if value["outline"]["captureSessionId"] == session), "")
            record = self._records.get(flow_id)
            if record is None:
                raise ValueError("No cached frame diagram; call prepare_frame_render_flow first")
            if record["outline"]["captureSessionId"] != session:
                raise ValueError("Capture session changed; prepare the current frame again")
            # Host readers never mutate the outline. Avoid copying 100k action
            # dictionaries just to return one 50-stage page. Public callers
            # retain the isolated-copy default.
            return deepcopy(record) if copy_record else dict(record)

    def _discard_drafts(self, flow_id: str) -> None:
        for draft_id, entry in list(self._drafts.items()):
            if entry[0] == flow_id:
                del self._drafts[draft_id]

    def save_draft(self, flow_id: str, proposal: dict[str, Any], reanalysis_revision: int) -> str:
        # A receipt is a capability for one immutable, in-memory revision. It
        # contains no capture data and is unusable under a different flow.
        if len(json.dumps(proposal, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("Frame diagram draft exceeds the 2 MiB proposal budget")
        with self._lock:
            record = self._records.get(flow_id)
            if record is None or record.get("reanalysis", {}).get("revision", 0) != reanalysis_revision:
                raise ValueError("The prepared frame changed; validate proposals again against the current draft")
            draft_id = secrets.token_hex(16)
            self._drafts[draft_id] = (flow_id, deepcopy(proposal))
            while len(self._drafts) > 8:
                self._drafts.popitem(last=False)
            return draft_id

    def get_draft(self, flow_id: str, draft_id: str) -> dict[str, Any]:
        with self._lock:
            entry = self._drafts.get(draft_id)
            if entry is None or entry[0] != flow_id or flow_id not in self._records:
                raise ValueError("No draft revision for this prepared frame; submit proposals to preflight again")
            self._drafts.move_to_end(draft_id)
            return deepcopy(entry[1])

    def publish(self, flow_id: str, document: dict[str, Any]) -> None:
        with self._lock:
            record = self._records.get(flow_id)
            if record is None or record["snapshotId"] != document["snapshotId"]:
                raise ValueError("Frame outline changed before publication")
            record["document"] = deepcopy(document)

    def begin_reanalysis(self, session: str, flow_id: str) -> dict[str, Any]:
        """Start an independent draft, retaining raw evidence and a comparison baseline."""
        with self._lock:
            self.get(session, flow_id, copy_record=False)
            record = self._records[flow_id]
            self._discard_drafts(flow_id)
            baseline = record.get("document") or record.get("baselineDocument")
            stages = (baseline or {}).get("stages", [])
            record["baselineDocument"] = deepcopy(baseline)
            # Keep the published window/sidecar intact. Only the authoring
            # session resets: old semantic nodes must not seed a fresh analysis.
            record["document"] = None
            record["reanalysis"] = {
                "revision": record.get("reanalysis", {}).get("revision", 0) + 1,
                "baselineObservationRevision": record.get("observationRevision", 0),
                "baselineEvidence": sorted(set().union(*(_analysis_evidence(row) for row in stages))),
            }
            return self.get(session, flow_id)

    @staticmethod
    def reanalysis_status(record: Mapping[str, Any], document: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        review = record.get("reanalysis")
        if not review:
            return None
        document = (record.get("document") if document is None else document) or {}
        stages = document.get("stages", [])
        fresh = {int(event) for event, revision in record.get("observationRevisions", {}).items()
                 if revision > review["baselineObservationRevision"]
                 and record.get("observations", {}).get(event, {}).get("currentReplayVerified") is not False
                 and record.get("observations", {}).get(event, {}).get("evidenceFreshness") != "previously-captured"}
        pending, updated = [], []
        baseline_evidence = set(review["baselineEvidence"])
        for stage in stages:
            # Renumbering/splitting old nodes and re-reading their bindings do
            # not establish that their purpose and dependencies were reconsidered.
            # An unchanged conclusion can retain its public caption and record
            # the renewed evaluation in investigation.evidence using valid caches.
            changed = bool(_analysis_evidence(stage) - baseline_evidence)
            (updated if changed else pending).append(stage["id"])
        return {"requested": True, "revision": review["revision"], "updatedStageIds": updated,
                "pendingStageIds": pending, "newlyInspectedEventIds": sorted(fresh),
                "draftStarted": bool(stages), "baselineAvailable": bool(record.get("baselineDocument")),
                "reviewComplete": bool(stages) and not pending, "semanticCorrectnessProven": False}

    @staticmethod
    def annotate_reanalysis(record: Mapping[str, Any], document: dict[str, Any]) -> None:
        status = FrameRenderFlowStore.reanalysis_status(record, document)
        if status is None:
            return
        document["reanalysis"] = status
        document["quality"]["gaps"] = [gap for gap in document["quality"]["gaps"]
                                      if gap.get("code") != "stage-review-not-refreshed"]
        for identity in status["pendingStageIds"]:
            gap = {"code": "stage-review-not-refreshed", "stageId": identity,
                   "message": "The requested new analysis has no updated reasoning record for this stage; caption edits and binding reads alone do not reanalyse it.",
                   "nextAction": "Reassess this module's purpose, boundaries and resource dependencies from available evidence. Update its algorithm or investigation.evidence with the findings, including why an unchanged conclusion is retained. Reuse valid source/images; refresh only missing or stale observations."}
            document["quality"]["gaps"].append(gap)
        document["quality"]["gapCount"] = len(document["quality"]["gaps"])
        document["quality"]["analysisComplete"] = bool(document["quality"]["analysisComplete"] and status["reviewComplete"])

    def record_observation(self, session: str, event_id: int, observation: Mapping[str, Any], flow_id: str = "") -> None:
        """Attach a Host-owned exact-EID pipeline inventory to the cached frame."""
        event_id = _integer(event_id, "observation event ID", 1)
        if not isinstance(observation, Mapping):
            raise ValueError("Stage observation must be an object")
        if observation.get("captureSessionId", session) != session:
            raise ValueError("Stage observation belongs to another capture session")
        if "bindingsComplete" in observation and not isinstance(observation["bindingsComplete"], bool):
            raise ValueError("Stage observation bindingsComplete must be boolean")
        errors = observation.get("bindingErrors", [])
        if not isinstance(errors, list) or len(errors) > 128:
            raise ValueError("Stage observation binding errors must be a bounded list")
        clean_errors = []
        for error in errors:
            if not isinstance(error, Mapping):
                raise ValueError("Stage observation binding error must be an object")
            clean_errors.append({key: _text(error.get(key, ""), "binding error " + key, 512)
                                 for key in ("kind", "stage", "error")})
        outputs = observation.get("expectedOutputs")
        if not isinstance(outputs, list) or len(outputs) > 128:
            raise ValueError("Stage observation must include up to 128 expected output attachments")
        clean_outputs = []
        for output in outputs:
            if not isinstance(output, Mapping) or output.get("kind") not in ("rtv", "dsv", "uav", "copy-destination"):
                raise ValueError("Stage output inventory must contain RTV/DSV/UAV or action buffer descriptors")
            if output["kind"] == "copy-destination" and (
                    output.get("resourceType") != "buffer" or output.get("captureSupported") is not False):
                raise ValueError("Action buffer output must identify a non-image buffer")
            item = {"resourceId": _integer(output.get("resourceId"), "output resource ID", 1, 0xFFFFFFFFFFFFFFFF),
                    "kind": output["kind"], "index": _integer(output.get("index", 0), "output index", 0, 65535)}
            for key in ("mip", "slice", "sample", "firstMip", "firstSlice"):
                if key in output:
                    item[key] = _integer(output[key], key)
            for key in ("byteOffset", "byteSize"):
                if key in output:
                    item[key] = _integer(output[key], key, high=0xFFFFFFFFFFFFFFFF)
            for key in ("numMips", "numSlices"):
                if key in output:
                    item[key] = None if output[key] is None else _integer(output[key], key)
            for key in ("stage", "format", "captureReason", "description"):
                if key in output:
                    item[key] = _text(output[key], "output " + key, 512)
            if "resourceType" in output:
                if output["resourceType"] not in ("texture", "buffer"):
                    raise ValueError("Output resourceType must be texture or buffer")
                item["resourceType"] = output["resourceType"]
            if "captureSupported" in output:
                if not isinstance(output["captureSupported"], bool):
                    raise ValueError("Output captureSupported must be boolean")
                item["captureSupported"] = output["captureSupported"]
            clean_outputs.append(item)
        with self._lock:
            record = self.get(session, flow_id, copy_record=False)
            if event_id not in {row["eventId"] for row in record["outline"]["actions"]}:
                raise ValueError("Stage observation EID does not exist in the frame")
            action = next(row for row in record["outline"]["actions"] if row["eventId"] == event_id)
            details = observation.get("details", {})
            # A failed native controller returns an empty PipeState without a
            # Python exception. Such a receipt is not a new resource inventory.
            requires_shader = bool(action.get("flags", 0) & (0x2 | 0x4 | 0x8 | 0x4000))
            empty_pipeline = (requires_shader and isinstance(details, Mapping)
                              and details.get("shaders") == {} and not clean_outputs
                              and not observation.get("textureInputs") and not details.get("inputs")
                              and not details.get("outputs"))
            if observation.get("pipelineUnavailable") or empty_pipeline:
                raise ValueError("bread-pipeline-unavailable: the exact action returned an empty pipeline; prior evidence was retained")
            for output in clean_outputs:
                if (output["kind"] == "copy-destination" and
                        action.get("copyDestination") != output["resourceId"]):
                    raise ValueError("Action buffer output must match the exact action destination")
            cleaned = deepcopy(dict(observation))
            cleaned["expectedOutputs"] = clean_outputs
            if "bindingErrors" in observation:
                cleaned["bindingErrors"] = clean_errors
            self._records[record["flowId"]]["observations"][str(event_id)] = cleaned
            stored = self._records[record["flowId"]]
            stored["observationRevision"] = stored.get("observationRevision", 0) + 1
            stored.setdefault("observationRevisions", {})[str(event_id)] = stored["observationRevision"]
            stored.get("observationReadFailures", {}).pop(str(event_id), None)

    def record_read_failure(self, session: str, event_id: int, error: str, flow_id: str) -> None:
        """Track an unsuccessful refresh separately from last usable evidence."""
        with self._lock:
            self.get(session, flow_id, copy_record=False)
            record = self._records[flow_id]
            if event_id not in {row["eventId"] for row in record["outline"]["actions"]}:
                return
            message = str(error)[:1600]
            record.setdefault("observationReadFailures", {})[str(event_id)] = message
            if "bread-replay-unavailable" in message:
                record["replayUnavailable"] = {"eventId": event_id, "error": message}

    def mark_replay_unavailable(self, session: str, error: str, flow_id: str) -> None:
        """A batch may report fatal health without identifying its first failing EID."""
        with self._lock:
            self.get(session, flow_id, copy_record=False)
            self._records[flow_id]["replayUnavailable"] = {"error": str(error)[:1600]}

    @staticmethod
    def page(record: Mapping[str, Any], *, offset: int = 0, limit: int = 50, section: str = "stages") -> dict[str, Any]:
        _integer(offset, "offset", 0, MAX_ACTIONS)
        _integer(limit, "limit", 1, 200)
        if section not in ("stages", "candidates", "actions", "proposals", "quality", "baseline"):
            raise ValueError("section must be stages, candidates, actions, proposals, quality or baseline")
        outline, document = record["outline"], record["document"]
        if document and record.get("reanalysis"):
            # The prior publication stays intact, but its old success/gap list
            # must not be presented as acceptance of the newly requested review.
            document = dict(document, quality=deepcopy(document["quality"]))
            FrameRenderFlowStore.annotate_reanalysis(record, document)
        values = ((record.get("baselineDocument") or {}).get("stages", []) if section == "baseline" else
                  outline["actions"] if section == "actions" else
                  record["candidates"] if section == "candidates" else
                  document.get("quality", {}).get("gaps", []) if section == "quality" and document else
                  [] if section == "quality" else
                  [] if section == "proposals" and record.get("reanalysis") and not document else
                  [editable_stage(stage) for stage in (document["stages"] if document else record["candidates"])] if section == "proposals" else
                  document["stages"] if document else record["candidates"])
        end = min(len(values), offset + limit)
        page = deepcopy(values[offset:end])
        if (section == "stages" and document) or section == "baseline":
            # Full membership is preserved in the saved document; do not send
            # a 100k-EID array to the Agent just to describe one stage.
            for stage in page:
                stage["actionCount"] = len(stage.pop("eventIds"))
        return {"schema": "bread-frame-render-flow-page/v1", "flowId": record["flowId"],
                "snapshotId": record["snapshotId"],
                **{key: outline[key] for key in ("captureSessionId", "captureId", "capturePath", "api")},
                "frameNumber": outline.get("frameNumber", 0),
                "published": bool(document), "section": section, "offset": offset, "limit": limit,
                "editableSection": "proposals", "qualitySection": "quality",
                "reanalysisRequested": bool(record.get("reanalysis")),
                "reanalysis": FrameRenderFlowStore.reanalysis_status(record),
                "baselineAvailable": bool(record.get("baselineDocument")),
                "sourceCandidateCount": len(record["candidates"]),
                "total": len(values), "hasMore": end < len(values), "nextOffset": end if end < len(values) else None,
                "items": page, "source": deepcopy(outline["summary"]),
                "coverage": deepcopy(document["coverage"]) if document else None,
                "quality": {key: deepcopy(value) for key, value in document.get("quality", {}).items()
                            if key != "gaps"} if document else None,
                "observations": deepcopy(record.get("observations", {})) if section == "actions" else {},
                "title": document["title"] if document else "", "summary": document["summary"] if document else ""}
