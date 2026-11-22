"""One Agent-owned completion review for every task, including plain answers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .agent_diagnostics import public_error
from .shader_review import (ShaderReviewDecision, ShaderReviewLoop, review_state as shader_review_state,
                            semantic_delivery_progress)
from .policy import AGENT_LOCAL_TOOLS, EFFECTFUL_TOOLS, READ_ONLY_TOOLS

TASK_REVIEW_TOOL = "record_task_review"
_DYNAMIC_FIELDS = frozenset({"timestamp", "updatedat", "createdat", "updatedunixns", "elapsed", "elapsedseconds",
                           "elapsedms", "duration", "durationms", "durationseconds", "round", "roundindex",
                           "activityid", "callid", "toolcallid", "requestid", "progress", "percent", "walltimeseconds",
                           "timestampunixns", "completedat", "startedat", "pollafterms", "operationid", "jobid", "generatedat"})


def is_task_review_tool(name: str) -> bool:
    return name.casefold() in {TASK_REVIEW_TOOL, "bread." + TASK_REVIEW_TOOL, "replaylab." + TASK_REVIEW_TOOL,
                              "mcp__bread__" + TASK_REVIEW_TOOL, "mcp__replaylab__" + TASK_REVIEW_TOOL}


def native_bread_tool(name: str) -> bool:
    key = name.casefold()
    for prefix in ("bread.", "replaylab.", "mcp__bread__", "mcp__replaylab__"):
        if key.startswith(prefix):
            return True  # The task proxy records external calls on this route too.
    return name in AGENT_LOCAL_TOOLS | EFFECTFUL_TOOLS | READ_ONLY_TOOLS


def work_identity(value: Any) -> str:
    def stable(item: Any) -> Any:
        if callable(getattr(item, "model_dump", None)):
            return stable(item.model_dump(mode="json"))
        if isinstance(item, Mapping):
            return {str(key): stable(child) for key, child in item.items()
                    if re.sub(r"[^a-z0-9]", "", str(key).casefold()) not in _DYNAMIC_FIELDS}
        if isinstance(item, (list, tuple)):
            return [stable(child) for child in item]
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
            except (ValueError, TypeError):
                return item
            return stable(parsed) if isinstance(parsed, (dict, list)) else item
        if item is None or isinstance(item, (int, float, bool)):
            return item
        return str(item)
    raw = json.dumps(stable(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _brief_review_detail(value: Any, limit: int = 320) -> str:
    """One public diagnostic; the complete review remains in the task record."""
    text = " ".join(str(value or "").split())
    sentence = re.split(r"(?<=[。！？])\s*|(?<=[.!?])\s+", text, maxsplit=1)[0]
    if len(sentence) <= limit:
        return sentence
    fragment = sentence[:limit]
    boundary = max(fragment.rfind(";"), fragment.rfind("；"), fragment.rfind(" "))
    if boundary >= limit // 2:
        fragment = fragment[:boundary]
    return fragment.rstrip() + "…"


@dataclass
class TaskActivityTracker:
    store: Any
    error: str = ""

    def record(self, tool: str, identity: str, occurrence: str) -> None:
        if is_task_review_tool(tool):
            return
        try:
            self.store.record_task_activity(tool, identity, occurrence_id=occurrence)
        except Exception as error:
            self.error = public_error(error)

    def native_event(self, event: Mapping[str, Any]) -> None:
        if event.get("activityKind") not in {"toolCall", "mcpToolCall", "commandExecution", "fileChange", "webSearch", "imageView"}:
            return
        if event.get("status") not in {"completed", "failed", "cancelled", "declined"} and event.get("lifecycle") != "completed":
            return
        tool = str(event.get("activityTarget") or event.get("activityKind") or "tool")
        if native_bread_tool(tool) and event.get("status") not in {"failed", "cancelled", "declined"}:
            return
        if event.get("status") == "completed" and event.get("verification") == "localFileObserved":
            observer = getattr(self.store, "record_shader_source_read", None)
            if callable(observer):
                try:
                    observer(event.get("workingFileObservation"))
                except Exception as error:
                    self.error = public_error(error)
        facts = {"tool": tool, "kind": event.get("toolKind"), "status": event.get("status"),
                 "workingFileObservation": event.get("workingFileObservation")}
        identity = event.get("workIdentity")
        if isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity):
            facts["workIdentity"] = identity
        self.record(tool, work_identity(facts), "native:" + str(event.get("activityId") or work_identity(facts)))


@dataclass(frozen=True)
class TaskReviewDecision:
    action: str = "accept"
    reason: str = ""
    state: Mapping[str, Any] = field(default_factory=dict)
    shader: ShaderReviewDecision = field(default_factory=ShaderReviewDecision)
    recovery_route: Mapping[str, Any] = field(default_factory=dict)

    def unchanged_note(self, language: str) -> str:
        return self.shader.unchanged_note(language)

    def reviewed_explanation(self) -> str:
        if self.action != 'accept' or self.state.get('fresh') is not True:
            return ''
        return str(((self.state.get('review') or {}).get('visualReview') or {}).get('answer') or '')

    def continuation_prompt(self) -> str:
        recovery = self.reason == "blocker-recovery-review"
        prompt = ((
            "Reassess the reported blocker in this same session before ending the task. "
            "Use the relevant loaded Skill, prior tool results and already available capabilities to distinguish "
            "an untried recovery path from an actual external dependency. Review what was attempted and its "
            "specific outcome. If a recorded next_action or another authorized, practical alternative is still "
            "untried, carry it out and continue the requested work; do not merely restate the blocker or list "
            "possible actions. Reuse completed work and preserve existing edits and deliverables. "
            "Do not rerun failed operations unchanged, repeat successful validation, or perform broad discovery "
            "just to prolong the task. If work truly requires missing user input, credentials, or permission, "
            "do not bypass that boundary: use the available user-input mechanism when appropriate, or record "
            "blocked with the precise dependency and attempted recovery evidence. If no feasible authorized "
            "alternative remains, accurately retain blocked. Never change the verdict to complete merely to "
            "pass this continuation check.\n\n"
        ) if recovery else "") + (
            "Continue the same task with the required Agent completion self-review. Keep the current session, "
            "tool results, Skills and edits; do not replay completed operations or repeat unchanged checks. "
            "Decide whether the original user request is fully satisfied using relevant acceptance criteria. "
            "For a plain answer, one criterion and evidence from your answer is enough; no new investigation, "
            "artifact, tool discovery or external call is required just to review. Repair feasible gaps yourself, "
            "then call record_task_review with criteria [{requirement,status:satisfied|unmet|blocked,evidence}], "
            "verdict complete|needs_work|blocked, findings, next_actions, and a concrete blocker if blocked. "
            "Use complete only when every requirement is satisfied. This is your assessment, not a Host score. "
            "Call the review tool after substantive work and any required Shader review/save; then provide the "
            "full standalone final answer to the original user request, even if only verification changed. "
            "Do not answer with a progress report or a reference to an earlier draft. "
            "A text-only answer can be corrected directly and marked complete without inventing tool work.\n\n"
            "Current completion review: " + json.dumps(dict(self.state), ensure_ascii=False, separators=(",", ":"))
        )
        if self.shader.action == "continue" or self.shader.reason == "blocked":
            prompt += "\n\nShader-specific checks remain in this same continuation:\n" + self.shader.continuation_prompt()
        if self.state.get('visualPublication'):
            publication = self.state['visualPublication']
            marked_effect = publication.get('markedEffect') if self.state.get('markedRenderTarget') else None
            prompt += ("\n\nReview the actual published visual explanation against the loaded presentation rules. "
                "Decide whether its important calculations and relationships need a graph, and whether each useful "
                "computation has its result image and relevant source. Repair feasible gaps in the same task. "
                "Keep short expressions in selected node details; source-linked code belongs to later code pages. "
                "Keep the opening overview plain and brief, with useful inline links; put detailed equations/code "
                "in the graph/code pages rather than display-math blocks in the overview. Omit mesh/UV views that "
                "do not help the question. Record that assessment in visual_review after the final publication. "
                "This inventory is an observation, not a requirement that every question use every view: "+
                json.dumps(publication,ensure_ascii=False,separators=(',',':')))
            if marked_effect:
                prompt += ("\n\nFor the marked effect, fix any markedEffect gaps. Check adjacent candidate draws "
                    "against their actual shaders and captured state. For each relevant layer, show a controlled pair at "
                    "the verified captured Present with observation_scope='appearance'. Connect the computed results "
                    "and key input textures to the final composite. Use the same output, display mapping and all-layers baseline. "
                    "Exclude candidates only using a restored experiment showing no change in the marked region. "
                    "Review the full draft and visuals with visual_checks: overview, contributions, display, flow, "
                    "source, links, inactive. Explain relevant disabled calculations with the captured value and a "
                    "labelled comparison or result where supported. Submit reviewed_answer with overview, steps "
                    "[{text,events}], combined, and optional inactive. Mention every related layer in the steps. "
                    "That standalone explanation is shown in chat instead of a status-only reply.")
            if publication.get('shaderCodeGaps') and (marked_effect or
                    any(card['type'] == 'diagram' for card in publication.get('cards', []))):
                prompt += (
                    "\n\nThe listed shaderCodeGaps are required deliverables for these tested effects. "
                    "Use the Shader workbench to organize and review each listed workspace, name the CBuffers "
                    "and resources, and finish with validation and no permanent replay apply. "
                    "Reuse the saved observations and comparison images. Republish the full explanation with "
                    "organized source excerpts for the missing Shader workspaces before recording complete.")
        advisory_unity = [row for row in self.state.get("unityRestorations", []) if row.get("diagnosticsOnly")]
        if advisory_unity:
            prompt += ("\n\nUnity observations are advisory. Choose the next useful action from actual artifacts and the user's goal; "
                "direct capture/project tools and alternate rendering paths remain available. Do not repeat unchanged checks "
                "or require formal binding/UI publication to proceed. Record real execution/images and missing checks honestly.\n"
                + json.dumps(advisory_unity, ensure_ascii=False, separators=(",", ":")))
        unity = [row for row in self.state.get("unityRestorations", []) if not row.get("complete") and not row.get("diagnosticsOnly")]
        if unity:
            prompt += ("\n\nFunctional Unity restoration still has observed gaps. Continue the SAME restoration ID: "
                "get_unity_restoration_status, inspect and repair the specific failed source/parameter/pass/scene checks, "
                "then render_unity_restoration and assess the current artifacts with review_unity_restoration. "
                "Use check_unity_restoration(repair_configuration=true) for configuration drift; edit the derived "
                "Shader or feature through project tools for an actual algorithm error, then inspect/render again. "
                "Reuse unchanged resources, saved Shader references and available merged CBuffer names. "
                "Prefer Bread decompilation, organization, compile/ABI and captured-output validation before the port, "
                "reusing existing proof; permanent replay apply is not a prerequisite. If application/tool limitations "
                "prevent completion, preserve the best saved draft and passed checks, record exactly which checks are "
                "missing or failed and continue the remaining organization, compilation and verification in Unity. "
                "Do not relabel known compiler/numeric defects as missing checks or successful validation. "
                "If no export is bound, finalize and bind it before diagnosing images or choosing compute/CPU rewrites. A missing tool, "
                "unsupported pipeline/API, dispatch-only EID or absent historical input is a reason to call "
                "plan_unity_restoration_recovery and execute another implementation route. Use apply_unity_recovery_assets "
                "to author real Shader/ComputeShader/C# or generated input files even if the MCP provider lacks a file tool. "
                "Infer justified history/layout/inputs or use Unity-native interfaces, record their exact sourceGapIds "
                "and implementation/execution evidence with record_unity_reconstruction, then check/render again. "
                "Keep captured facts separate from inferred or generated inputs. Preserve the core algorithm and "
                "verify the intended effect is visible; normal destination-renderer differences may pass the task's "
                "visual-fidelity objective. Do not restart successful imports, widen acceptance just to pass, replace "
                "the requested algorithm with an unrelated stock Shader, or mark an import as a finished port. "
                "The current review covers geometry_and_coordinates, material_and_constants, lighting_and_indirect, "
                "render_states_and_passes, scene_and_dependencies, readable_source, core_algorithm and visible_effect. "
                "An unchanged failure needs another diagnosed repair, not repeat polling. Wrong project/capture identity, "
                "failed compilation and absent execution/output still require actual correction.\n"
                + json.dumps(unity, ensure_ascii=False, separators=(",", ":")))
        if self.recovery_route:
            prompt += ("\n\nA practical Unity recovery route remains untried. Execute this route in the current task "
                "before ending with a capability blocker. Read its concrete plan with plan_unity_restoration_recovery, "
                "author and run its implementation, and collect fresh observations. A route suggestion alone is not "
                "progress or execution evidence: " + json.dumps(dict(self.recovery_route), ensure_ascii=False, separators=(",", ":")))
        return prompt

    def incomplete_text(self, language: str, candidate: str = "") -> str:
        zh = language == "zh_CN"
        reasons = {
            "unfinished-tools": ("仍有操作未结束。", "Some operations are still running."),
            "review-state-unavailable": ("暂时无法确认检查结果。", "The review result could not be confirmed."),
            "blocked": ("", ""),
            "blocked-after-recovery-review": ("", ""),
            "awaiting-user-decision": ("已保存当前成果，等待你的选择。", "Current results are saved; awaiting your decision."),
            "no-progress": ("重复尝试没有取得新进展，已停止本轮处理。", "Repeated attempts made no new progress; this run has stopped."),
        }
        pair = reasons.get(self.reason, reasons["no-progress"])
        explanation = pair[0 if zh else 1]
        text = ("任务尚未完成" if zh else "The task is incomplete") + (
            ("：" if zh else ": ") + explanation if explanation else "。" if zh else ".")
        review = self.state.get("review")
        review = review if isinstance(review, Mapping) else {}
        paragraphs = [text]
        answer = candidate.strip()
        if answer:
            # The Agent already produced the user-facing explanation. Do not
            # prepend another entire evidence ledger or append a second report.
            paragraphs.append(answer)
        else:
            completed, remaining = [], []
            for row in review.get("criteria", []):
                if not isinstance(row, Mapping):
                    continue
                destination = completed if row.get("status") == "satisfied" else remaining
                if len(destination) < 2:
                    value = row.get("evidence") if destination is completed else row.get("requirement")
                    brief = _brief_review_detail(value or row.get("requirement"))
                    if brief and brief not in destination:
                        destination.append(brief)
            if completed:
                paragraphs.append(("已完成：" if zh else "Completed: ") + " / ".join(completed))
            if remaining:
                paragraphs.append(("剩余工作：" if zh else "Remaining: ") + " / ".join(remaining))

        blocker = review.get("blocker") or self.state.get("error") or self.state.get("reviewError")
        if not blocker and self.shader.action == "partial":
            for target in self.shader.targets:
                source_review = target.get("review")
                source_review = source_review if isinstance(source_review, Mapping) else {}
                blocker = source_review.get("blocker") or target.get("diagnostic")
                if blocker:
                    break
            if not blocker:
                blocker = self.shader.incomplete_text(language).split("\n\n", 1)[-1]
        brief_blocker = _brief_review_detail(blocker)
        if brief_blocker and brief_blocker.casefold() not in "\n".join(paragraphs).casefold():
            paragraphs.append(("原因：" if zh else "Reason: ") + brief_blocker)
        if not review and not answer and not brief_blocker:
            paragraphs.append("尚未记录本次任务的完成检查。" if zh else "No completion review was recorded for this task.")
        if not answer:
            actions = review.get("nextActions", review.get("next_actions", []))
            if actions:
                paragraphs.append(("后续需要：" if zh else "Next step: ") + _brief_review_detail(actions[0]))
        decision=review.get("decisionRequest")
        if self.reason=="awaiting-user-decision" and isinstance(decision,Mapping):
            question=str(decision.get("question") or "").strip()
            if question and question.casefold() not in "\n".join(paragraphs).casefold():
                paragraphs.append(question+"\n\n"+"\n".join("- "+str(option) for option in decision.get("options",[])))
        return "\n\n".join(paragraphs)


@dataclass
class TaskReviewLoop:
    shader_loop: ShaderReviewLoop
    _work_facts: Any = field(default=None, init=False)
    _seen_pending: set[Any] = field(default_factory=set, init=False)
    _issued_unity_routes: set[tuple[str, str, str]] = field(default_factory=set, init=False)

    def _next_unity_route(self, state: Mapping[str, Any]) -> dict:
        for row in state.get("unityRestorations", []):
            if row.get("complete") or row.get("diagnosticsOnly"):
                continue
            recovery = row.get("recovery") or {}
            if not isinstance(recovery, Mapping) or not recovery.get("contextFingerprint"):
                continue
            names = recovery.get("availableRouteIds", [])
            if not isinstance(names, (list, tuple)):
                continue
            suggested = recovery.get("suggestedRouteId")
            ordered = ([suggested] if suggested in names else []) + [name for name in names if name != suggested]
            for name in ordered[:32]:
                if not isinstance(name, str) or not name:
                    continue
                key = (str(row.get("restorationId", "")), str(recovery["contextFingerprint"]), name)
                if key not in self._issued_unity_routes:
                    self._issued_unity_routes.add(key)
                    return {"restorationId": key[0], "contextFingerprint": key[1], "routeId": name}
        return {}

    @classmethod
    def for_intent(cls, intent: Mapping[str, Any] | None) -> "TaskReviewLoop":
        return cls(ShaderReviewLoop.for_intent(intent))

    def decide(self, state: Mapping[str, Any], shader_state: Mapping[str, Any]) -> TaskReviewDecision:
        shader = self.shader_loop.decide(shader_state, detect_stall=False)
        if state.get("error"):
            return TaskReviewDecision("partial", "review-state-unavailable", state, shader)
        review = state.get("review")
        review = review if isinstance(review, Mapping) else {}
        verdict = review.get("verdict")
        status = str(verdict) if state.get("fresh") is True and verdict in {"complete", "needs_work", "blocked"} else ("stale" if review else "missing")
        decision=review.get("decisionRequest")
        if status=="blocked" and isinstance(decision,Mapping) and decision.get("question") and decision.get("evidence"):
            # Input requested by the Agent is not another recovery opportunity.
            # Do not resume Shader/Unity route discovery behind that question.
            return TaskReviewDecision("partial", "awaiting-user-decision", state, shader)
        if shader.action == "partial" and shader.reason != "blocked":
            return TaskReviewDecision("partial", shader.reason, state, shader)
        blocked = status == "blocked" or shader.reason == "blocked"
        pending_unity = tuple((row.get("restorationId"), row.get("stateFingerprint"),
                              tuple(row.get("gapCodes", [])), (row.get("recovery") or {}).get("attemptFingerprint"))
                             for row in state.get("unityRestorations", []) if not row.get("complete") and not row.get("diagnosticsOnly"))
        if status == "complete" and shader.action == "accept" and not pending_unity:
            return TaskReviewDecision(state=state, shader=shader)
        facts = (state.get("workFingerprint"), pending_unity, tuple(
            (row.get("workspacePath"), row.get("sourceSha256"), row.get("saved"), row.get("canonicalSourceSha256"), row.get("sourceConflict"),
             semantic_delivery_progress(row.get("cbufferSemanticDelivery")))
            for row in shader.targets))
        signature = (status, tuple(row.get("reviewStatus") for row in shader.targets))
        if facts != self._work_facts:
            self._work_facts = facts
            self._seen_pending.clear()
        if signature in self._seen_pending:
            route = self._next_unity_route(state)
            if route:
                return TaskReviewDecision("continue", "unity-recovery-route", state, shader, route)
            return TaskReviewDecision("partial", "blocked-after-recovery-review" if blocked else "no-progress", state, shader)
        self._seen_pending.add(signature)
        return TaskReviewDecision("continue", "blocker-recovery-review" if blocked else "self-review-required", state, shader)


def completion_review_state(store: Any, tracker: TaskActivityTracker) -> dict[str, Any]:
    try:
        state = store.task_review_state()
        if not isinstance(state, Mapping) or not state.get("scopeId"):
            raise ValueError("Task review scope is missing or malformed")
        return {**dict(state), **({"error": tracker.error} if tracker.error else {})}
    except Exception as error:
        return {"error": public_error(error)}


def emit_task_review(reporter: Any, decision: TaskReviewDecision) -> None:
    reporter.writer.emit("running", summary="Agent completion self-review is continuing", data={
        "phase": "task-self-review", "status": "running", "agentId": "main", "activityId": "main-task-self-review",
        "activityKind": "agentReview", "activityTitleId": "task.self-review.started", "activityTitleArgs": [],
        "reviewReason": decision.reason, "hostVerified": False, "verification": "agentAssessment",
    })
