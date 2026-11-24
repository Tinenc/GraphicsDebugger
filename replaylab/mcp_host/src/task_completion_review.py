"""Agent task assessments bound to this execution and observed work receipts.

The Host validates shape and freshness only. It does not score answers, decide
whether evidence is sufficient, or scan/hash artifacts to prove completion.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

REVIEW_SCHEMA = "bread-task-review/v1"
STATE_SCHEMA = "bread-task-review-state/v1"
MAX_IDENTITIES = 4096
REVIEW_TOOLS = frozenset({"record_task_review", "record_shader_organization_review"})
VISUAL_CHECKS = ('overview', 'contributions', 'display', 'flow', 'source', 'links', 'inactive')
TASK_REVIEW_TOOL_DESCRIPTION = (
    "Record the main Agent's self-review against the current user's requirements, after the last meaningful operation. "
    "Use existing evidence; this needs no preparatory tool or new validation. For an ordinary answer one requirement "
    "and its explanation suffice. Each criterion has requirement, status (satisfied/unmet/blocked), and evidence strings. "
    "Use complete only when every requirement is satisfied and no findings/actions/blocker remain; use needs_work "
    "with findings and next_actions then continue, or blocked with the concrete reason you cannot proceed. "
    "The Host binds this assessment to the current task execution and work version but does not judge answer quality. "
    "Subagents provide evidence to the main Agent and must not record the whole-task verdict."
    " If only a real user decision remains, save concrete evidence, use blocked with decision_request "
    "(question/options/evidence), and present that question. This ends automatic recovery without claiming completion."
    " After publishing visuals, include visual_review: assess the assembled overview, images, graph relationships, "
    "stage results, source coverage, inline navigation and relevance against the actual question and loaded presentation guidance. "
    "A valid image pair or one source helper alone does not review the entire explanation. The publication inventory is factual; "
    "you decide which views are useful, repair feasible omissions, and explain that judgment in visual_review."
    " For a layered marked effect include visual_checks with a separate evidence note for overview, "
    "contributions, display, flow, source, links and inactive calculations. Supply reviewed_answer "
    "with a plain-language overview, ordered steps referencing every contributor EID, and how they combine. "
    "That is the standalone answer shown to the user."
)
TASK_REVIEW_TOOL_SCHEMA = {
    "type": "object", "properties": {
        "verdict": {"type": "string", "enum": ["needs_work", "complete", "blocked"]},
        "criteria": {"type": "array", "minItems": 1, "maxItems": 16, "items": {
            "type": "object", "properties": {
                "requirement": {"type": "string", "minLength": 1, "maxLength": 2000},
                "status": {"type": "string", "enum": ["satisfied", "unmet", "blocked"]},
                "evidence": {"type": "string", "minLength": 1, "maxLength": 4000},
            }, "required": ["requirement", "status", "evidence"], "additionalProperties": False,
        }},
        "findings": {"type": "array", "maxItems": 32, "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
        "next_actions": {"type": "array", "maxItems": 32, "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
        "blocker": {"type": "string", "maxLength": 4000, "default": ""},
        "visual_review": {"type":"string","maxLength":8000,"default":""},
        "visual_checks": {"type": "object", "properties": {
            key: {"type": "string", "minLength": 1, "maxLength": 1000} for key in VISUAL_CHECKS
        }, "required": list(VISUAL_CHECKS), "additionalProperties": False},
        "reviewed_answer": {"type": "object", "properties": {
            "overview": {"type": "string", "minLength": 16, "maxLength": 1000},
            "steps": {"type": "array", "minItems": 2, "maxItems": 12, "items": {
                "type": "object", "properties": {
                    "text": {"type": "string", "minLength": 8, "maxLength": 1200},
                    "events": {"type": "array", "maxItems": 12, "items": {"type": "integer", "minimum": 1}},
                }, "required": ["text", "events"], "additionalProperties": False,
            }},
            "combined": {"type": "string", "minLength": 12, "maxLength": 1200},
            "inactive": {"type": "string", "maxLength": 1200},
        }, "required": ["overview", "steps", "combined"], "additionalProperties": False},
        "decision_request": {"type": ["object","null"], "properties": {
            "question": {"type":"string","minLength":1,"maxLength":2000},
            "options": {"type":"array","minItems":2,"maxItems":3,"items":{"type":"string","minLength":1,"maxLength":512}},
            "evidence": {"type":"array","minItems":1,"maxItems":16,"items":{"type":"string","minLength":1,"maxLength":2000}},
        },"required":["question","options","evidence"],"additionalProperties":False},
    }, "required": ["verdict", "criteria", "findings", "next_actions"], "additionalProperties": False,
}
_VOLATILE = frozenset({"updatedunixns", "timestampunixns", "timestamp", "elapsedms", "durationms",
    "elapsed", "duration", "elapsedseconds", "durationseconds", "elapsedus", "durationus",
    "startedunixns", "completedunixns", "finishedunixns", "createdunixns", "latencyms", "walltimems",
    "createdat", "updatedat", "completedat", "startedat", "finishedat", "sampledat",
    "pollafterms", "requestid", "activityid", "operationid", "jobid", "generatedat"})


def _stable(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "<nested>"
    if isinstance(value, Mapping):
        return {str(key): _stable(item, depth + 1) for key, item in list(value.items())[:1024]
                if re.sub(r"[^a-z0-9]", "", str(key).lower()) not in _VOLATILE}
    if isinstance(value, (list, tuple)):
        return [_stable(item, depth + 1) for item in value[:1024]]
    if isinstance(value, str):
        return {"sha256": hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest(), "chars": len(value)}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(type(value).__name__)


def activity_identity(tool: str, result: Any, arguments: Mapping[str, Any] | None = None) -> str:
    """Digest observed facts without persisting source text, arguments or secrets."""
    if isinstance(result, Mapping) and result.get("schema") == "bread-unity-restoration/v1":
        from .unity_restoration_requirements import stable_activity
        result = stable_activity(result)
    value = {"tool": tool, "arguments": _stable(arguments or {}), "result": _stable(result)}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def work_fingerprint(identities: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(identities))).encode("ascii")).hexdigest()


def _reviewed_explanation(answer: Any, related_events: list[int]) -> str:
    def paragraph(value, minimum, maximum):
        return isinstance(value, str) and minimum <= len(value.strip()) <= maximum and '\n' not in value.strip()

    if (not isinstance(answer, Mapping) or set(answer) - {'overview', 'steps', 'combined', 'inactive'} or
            not paragraph(answer.get('overview'), 16, 1000) or
            not paragraph(answer.get('combined'), 12, 1200) or
            not paragraph(answer.get('inactive', ''), 0, 1200) or
            not isinstance(answer.get('steps'), list) or not 2 <= len(answer['steps']) <= 12):
        raise ValueError('Supply reviewed_answer with a standalone overview, ordered steps, and the combined result.')
    covered = set()
    lines = []
    for index, step in enumerate(answer['steps'], start=1):
        if (not isinstance(step, Mapping) or set(step) != {'text', 'events'} or
                not paragraph(step['text'], 8, 1200) or not isinstance(step['events'], list) or
                len(step['events']) > 12 or any(type(eid) is not int or eid <= 0 for eid in step['events'])):
            raise ValueError('Each reviewed answer step needs short prose and the EIDs it explains.')
        covered.update(step['events'])
        lines.append(f"{index}. {step['text'].strip()}")
    missing = sorted(set(related_events) - covered)
    if missing:
        raise ValueError(f'The reviewed answer omits these contributor EIDs: {missing}. Explain each layer in its steps.')
    parts = [answer['overview'].strip(), '\n'.join(lines), answer['combined'].strip(), answer.get('inactive', '').strip()]
    return '\n\n'.join(p for p in parts if p)


def make_review(state: Mapping[str, Any], verdict: str, criteria: list[dict[str, str]],
                findings: list[str], next_actions: list[str], blocker: str = "", decision_request: dict | None = None,
                visual_review: str = '', visual_checks: dict[str, str] | None = None,
                reviewed_answer: dict | None = None) -> dict[str, Any]:
    if verdict not in {"needs_work", "complete", "blocked"}:
        raise ValueError("verdict must be needs_work, complete or blocked")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 16:
        raise ValueError("criteria must contain 1-16 requirements from the current user request")
    for row in criteria:
        if not isinstance(row, Mapping) or set(row) != {"requirement", "status", "evidence"}:
            raise ValueError("Each criterion must contain requirement, status and evidence")
        if row["status"] not in {"satisfied", "unmet", "blocked"}:
            raise ValueError("criterion status must be satisfied, unmet or blocked")
        for key, limit in (("requirement", 2000), ("evidence", 4000)):
            if not isinstance(row[key], str) or not row[key].strip() or len(row[key]) > limit:
                raise ValueError("Each requirement and evidence must be nonempty bounded text")
    for values in (findings, next_actions):
        if not isinstance(values, list) or len(values) > 32 or any(
                not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("findings and next_actions must contain at most 32 nonempty short strings")
    if not isinstance(blocker, str) or len(blocker) > 4000:
        raise ValueError("blocker must be a short explanation")
    publication=state.get('visualPublication') or {}
    if not isinstance(visual_review,str) or len(visual_review)>8000:
        raise ValueError('visual_review must be a short presentation assessment')
    marked_effect = publication.get('markedEffect') if state.get('markedRenderTarget') else None
    diagram = any(card['type'] == 'diagram' for card in publication.get('cards', []))
    if verdict == 'complete' and marked_effect and (marked_effect['needsFinalReplay'] or
            marked_effect['needsDiagram'] or marked_effect['missingLayerViews'] or
            marked_effect.get('missingFinalLayerComparisons') or marked_effect.get('missingInputTextures') or
            marked_effect.get('combinedBaselineMismatch')):
        raise ValueError(
            'The marked effect explanation is incomplete. Use the verified captured Present for final comparisons, '
            'show each confirmed layer with a controlled final pair sharing the combined baseline, and connect its result and key bound '
            'textures to the final composite in the diagram. Inspect and justify exclusions of unrelated draws. '
            'Republish one complete bundle. '
            'Pending: ' + json.dumps(marked_effect, ensure_ascii=False, separators=(',', ':')))
    if verdict == "complete" and publication.get("shaderCodeGaps") and (diagram or marked_effect):
        gaps = json.dumps(publication["shaderCodeGaps"], ensure_ascii=False, separators=(",", ":"))
        raise ValueError(
            "Visual explanation is missing organized Shader code for tested contributors. "
            "Organize and validate these Shader workspaces, including CBuffer/resource naming, "
            "then republish the existing comparison and graph with source-linked code blocks. "
            "Pending: " + gaps)
    checks_required = verdict == 'complete' and marked_effect and 'missingFinalLayerComparisons' in marked_effect
    if checks_required or visual_checks is not None:
        if (not isinstance(visual_checks, Mapping) or set(visual_checks) != set(VISUAL_CHECKS) or
                any(not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000
                    for note in visual_checks.values())):
            raise ValueError('Review the complete visual answer with visual_checks for ' + ', '.join(VISUAL_CHECKS) +
                             '. Give a concrete evidence note per field. For inactive, say which relevant disabled '
                             'calculations were checked and how their state and illustration are supported.')
    answer_required = checks_required and 'combinedBaselineMismatch' in marked_effect
    rendered_answer = (_reviewed_explanation(reviewed_answer, (marked_effect or {}).get('relatedEvents', []))
                       if answer_required or reviewed_answer is not None else '')
    if verdict=='complete' and publication and not visual_review.strip():
        raise ValueError('Review the assembled visual explanation before completing, then include visual_review. '
            'Assess which graph relationships, computed results, stage code and useful inline links the question needs; '
            'remove irrelevant mesh/UV cards. The overview stays short and plain; detailed code follows the graph. '
            'Repair feasible omissions rather than reviewing only one image pair or helper. Published inventory: '+
            json.dumps(publication,ensure_ascii=False,separators=(',',':')))
    if verdict == "complete" and (any(row["status"] != "satisfied" for row in criteria)
                                   or findings or next_actions or blocker.strip()):
        raise ValueError("complete requires all criteria satisfied with no unresolved findings, actions or blocker")
    if verdict == "needs_work" and (not findings or not next_actions):
        raise ValueError("needs_work requires concrete findings and next_actions")
    if verdict == "blocked" and not blocker.strip():
        raise ValueError("blocked requires the actual reason work cannot continue")
    if decision_request is not None:
        if (verdict!="blocked" or not isinstance(decision_request,dict) or
                set(decision_request)!={"question","options","evidence"}):
            raise ValueError("A pending user decision requires blocked and question/options/evidence")
        question=decision_request["question"];options=decision_request["options"];evidence=decision_request["evidence"]
        if (not isinstance(question,str) or not question.strip() or len(question)>2000 or
                not isinstance(options,list) or not 2<=len(options)<=3 or
                not isinstance(evidence,list) or not 1<=len(evidence)<=16 or
                any(not isinstance(value,str) or not value.strip() or len(value)>512 for value in options) or
                any(not isinstance(value,str) or not value.strip() or len(value)>2000 for value in evidence)):
            raise ValueError("Supply a short user question, 2-3 choices and concrete saved evidence")
    return {"schema": REVIEW_SCHEMA, "taskId": state["taskId"], "scopeId": state["scopeId"],
            "requestFingerprint": state["requestFingerprint"], "workRevision": state["workRevision"],
            "workFingerprint": work_fingerprint(state.get("identities", [])), "verdict": verdict,
            "criteria": [dict(row) for row in criteria], "findings": list(findings),
            "nextActions": list(next_actions), "blocker": blocker,
            "author": "agent", "hostSemanticApproval": False,
            **({'visualReview':{'visualId':publication.get('id',''),'evidence':visual_review,
                                **({'checks': dict(visual_checks)} if visual_checks else {}),
                                **({'answerSpec': reviewed_answer, 'answer': rendered_answer} if rendered_answer else {})}}
               if visual_review.strip() else {}),
            **({"decisionRequest":json.loads(json.dumps(decision_request))} if decision_request is not None else {})}


def state_view(state: Mapping[str, Any]) -> dict[str, Any]:
    result = {"schema": STATE_SCHEMA, **{key: state[key] for key in
              ("taskId", "scopeId", "requestFingerprint", "workRevision")},
              "workFingerprint": work_fingerprint(state.get("identities", [])),
              "review": None, "fresh": False,
              "unityRestorations": [*state.get("unityRestorations", {}).values(),
                                    *state.get("unityFunctionalImports", {}).values()],
              "progressTrackingSaturated": state.get("progressTrackingSaturated") is True}
    if state.get('visualPublication'):result['visualPublication']=state['visualPublication']
    if state.get('markedRenderTarget'):result['markedRenderTarget']=True
    if isinstance(state.get("reviewError"), str):
        result["reviewError"] = state["reviewError"][:600]
    review = state.get("review")
    if isinstance(review, Mapping):
        try:
            reconstructed = make_review(state, review.get("verdict"), review.get("criteria"),
                review.get("findings"), review.get("nextActions"), review.get("blocker", ""), review.get("decisionRequest"),
                (review.get('visualReview') or {}).get('evidence',''),
                (review.get('visualReview') or {}).get('checks'),
                (review.get('visualReview') or {}).get('answerSpec'))
            identity = ("schema", "taskId", "scopeId", "requestFingerprint", "author", "hostSemanticApproval")
            if (all(review.get(key) == reconstructed[key] for key in identity) and
                    review.get('visualReview') == reconstructed.get('visualReview')):
                result["review"] = json.loads(json.dumps(review))
                result["fresh"] = (type(review.get("workRevision")) is int
                    and review["workRevision"] == state["workRevision"]
                    and review.get("workFingerprint") == result["workFingerprint"]
                    and (not state.get('visualPublication') or
                         (review.get('visualReview') or {}).get('visualId')==state['visualPublication'].get('id')))
        except (ValueError, KeyError, TypeError):
            pass
    return result
