"""Agent-owned shader self-review, joined to the provider's existing session.

The Host supplies exact source identity and records the Agent's assessment.
This module only checks that assessment's presence/freshness and detects a
stalled continuation. It never scores source quality or runs validation tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping


def semantic_delivery_progress(value: Any) -> str:
    """Count physical publication/coverage changes, never new prose or polling timestamps."""
    scalar_keys = {
        "required", "status", "cbufferStatus", "cbufferRequired", "sourceSha256", "candidateRawSha256",
        "publishedSourceSha256", "activeDisplayShaderRawSha256", "complete", "bind", "space",
        "binding", "registerSpace", "resourceId", "kind", "bindingCount", "reviewedBindingCount",
        "unresolvedLaneCount", "missingLaneCount", "unresolvedCount", "missingCount",
        "textureCount", "reviewedTextureCount",
        "matchedTextureCount", "publishedTextureCount", "usedAssetCount", "configured",
        "reused", "applied", "sourceNamesApplied", "panelAliasesApplied",
        "requestedSemanticFields", "writtenSemanticFields", "keptUserSemanticFields",
        "requestedDisplaySemanticFields", "writtenDisplaySemanticFields", "keptUserDisplaySemanticFields",
    }
    container_keys = {
        "bindingCoverage", "textureCoverage", "resourceNamingDelivery", "assetUsage", "counts",
        "pendingBindings", "pendingTextures", "bindings", "textures", "missingOffsets",
        "semanticOffsets", "unresolvedOffsets", "requiredOffsets",
    }

    def stable(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): stable(child) for key, child in item.items()
                    if key in scalar_keys | container_keys}
        if isinstance(item, (list, tuple)):
            children = [stable(child) for child in item]
            return sorted(children, key=lambda child: json.dumps(child, sort_keys=True, ensure_ascii=False))
        return item if item is None or isinstance(item, (str, bool, int)) else None

    return json.dumps(stable(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _semantic_delivery_pending(value: Mapping[str, Any], source_sha256: str) -> bool:
    if value.get("required") is not True:
        return False
    if value.get("status") != "complete" or value.get("sourceSha256") != source_sha256:
        return True
    # A successful material-block publication cannot override a pending sibling binding or
    # asset/texture reconciliation. This checks formal receipts, not semantic naming quality.
    coverage = value.get("bindingCoverage")
    if isinstance(coverage, Mapping) and coverage.get("complete") is False:
        return True
    resources = value.get("resourceNamingDelivery")
    return bool(isinstance(resources, Mapping) and resources.get("required") is True
                and (resources.get("status") != "complete"
                     or resources.get("sourceSha256") != source_sha256))


@dataclass(frozen=True)
class ShaderReviewDecision:
    action: str = "accept"
    reason: str = ""
    targets: tuple[dict[str, Any], ...] = ()

    def unchanged_note(self, language: str) -> str:
        if not any(target.get("reviewStatus") == "unchangedAccepted" for target in self.targets):
            return ""
        return ("本轮未修改部分或全部目标源码；Agent 自检判断其现有版本已满足整理标准。"
                if language == "zh_CN" else
                "Some or all target source files were unchanged this turn; the Agent's self-review judged their existing versions to meet the organization criteria.")

    def continuation_prompt(self) -> str:
        if self.targets and all(target.get("reviewStatus") == "overview-display-pending" for target in self.targets):
            return (
                "The source and independent calculation directory are reviewed, but the requested UI delivery "
                "has not displayed that exact reviewed directory. Reuse this workspace and saved source. Call "
                "show_shader_workspace_source, then get_shader_workspace_ui_state; its actual directory must "
                "be current and reviewed for the same source and overview. If the metadata is stale, repair it "
                "with update_shader_semantic_overview first. This is metadata/display recovery: do not rewrite "
                "source, compile, validate ABI, apply replay, create another workspace or repeat finish. Then "
                "review the whole request. Current delivery facts:\n"
                + json.dumps(self.targets, ensure_ascii=False, separators=(",", ":")))
        if self.targets and all(target.get("reviewStatus") == "overview-pending" for target in self.targets):
            return (
                "The source assessment is current, but the independent calculation directory is missing, "
                "stale or unreviewed. Continue this same task by repairing that metadata. Use "
                "get_shader_semantic_overview_context with the existing workspace and organization id. "
                "Analyze the complete current program, reusing valid source knowledge; trace the actual "
                "calculations inside coarse orchestration functions. Mesh, texture and draw-state evidence "
                "may suggest working hypotheses; confirm or revise them from actual expressions and "
                "resource consumers. Expose independently explainable major calculations and meaningful "
                "intermediate results hidden inside broad stages. Apply that granularity to the directory "
                "and the corresponding function-graph level, while mapping small supporting work to its "
                "owners. Do not merge distinct contributions just because they reach the same final color. "
                "Do not copy the entry-call "
                "list, expand every helper, or impose techniques/stage counts. Publish stages and "
                "their current-source granularity/data-flow/scope assessment using "
                "update_shader_semantic_overview. This preserves the existing source assessment and "
                "does not require a source rewrite, recompilation, ABI check, replay or a repeated finish. "
                "Any already separate pending source save or resource-name effect remains to be completed "
                "after the directory update. Then review the whole request and give a brief factual answer.\n\n"
                "Current source and directory delivery facts:\n"
                + json.dumps(self.targets, ensure_ascii=False, separators=(",", ":"))
            )
        if self.targets and all(target.get("reviewStatus") == "semantic-delivery-pending" for target in self.targets):
            return (
                "The source organization is reviewed and saved, but its required resource naming/panel "
                "delivery is still pending for the final source. Continue in this same task with only that "
                "remaining effect. Reuse the current source, asset context and prior semantic fields; reconcile "
                "names and component groups with the current consumers. If CBuffer publication remains pending, call publish_cbuffer_aliases "
                "with semantic_fields_json and semantic_source_sha256 equal to the current saved source hash. "
                "Preserve unknown lanes and user-authored names; known lanes can carry explicit inferred roles "
                "without recovering every original asset name. Submit the complete intended view for each "
                "affected block, including valid existing fields. Inspect current-display requested/written/"
                "preserved/skipped counts and repair a concrete publication error. A historical receipt or "
                "one named material CBuffer does not cover the other bindings. Inspect bindingCoverage and "
                "finish every active block; use get_shader_workspace_outline(section='cbuffer-semantics') "
                "for existing source aliases and exact lane candidates. Split unrelated controls, and "
                "record specifically unresolved offsets with evidence in binding_reviews_json instead of "
                "inventing names or treating an entire uninspected block as unknown. A Shader may use "
                "view, lighting and draw constants as well as material constants. "
                "If resourceNamingDelivery is pending, reuse assetNamingPreparation and existing asset receipts; "
                "resolve only missing targeted asset/texture evidence. Inspect every used texture binding, "
                "publish supported changed names with publish_shader_resource_names, then use "
                "record_shader_resource_naming_review with expected_source_sha256 and the current workspace. "
                "Its review must record assets {status,evidence,usage}, every texture {selector,status,evidence} "
                "(plus operation_id for published names), and reconciliation explaining the post-source naming "
                "assessment. Retained and specifically unresolved names are legitimate recorded outcomes; "
                "never invent asset matches or original names. Reuse complete CBuffer or texture delivery; "
                "do not republish already covered bindings just to fill the remaining review. "
                "Source identifiers alone do not establish current panel delivery. This is display metadata: "
                "do not rewrite source, repeat source review, compile, ABI-check, apply, reopen the capture or "
                "rescan assets merely to publish it. Do not call finish_shader_workspace_organization again "
                "for metadata-only work: its source draft may already be closed. After publication, update the whole-task review and final "
                "answer. If an actual external blocker remains after practical recovery, record the panel "
                "requirement as blocked rather than omitting it or claiming complete.\n\n"
                "Current source and display delivery facts:\n"
                + json.dumps(self.targets, ensure_ascii=False, separators=(",", ":"))
            )
        return (
            "Continue this same Shader organization task with an explicit Agent self-review. "
            "Your previous answer was not finalized because a current review is still missing "
            "or reports remaining work. Keep the existing session, edits, loaded Skills and tool "
            "results; do not replay completed operations. Read the current proposed/final source "
            "only where needed. Carry forward asset-naming preparation: use valid target-specific "
            "asset mappings and names already adapted to this source; if configured unpacked assets "
            "were skipped, finish targeted reader/name work in the same draft without a whole-tree "
            "rescan. Reuse partial materialSemanticContext too: associated parameter names and values "
            "can inform calculation roles and comments even when offsets are unresolved; keep those "
            "interpretations distinct from confirmed physical aliases and inspect its reuse scope. "
            "A mapping receipt alone is not proof that names were applied. Before the final source "
            "review, reconcile CBuffer names with the organized consumers in the same draft; use "
            "known physical lanes for explicit inferred roles when original material names remain "
            "unresolved. For captured-Shader organization with CBuffers, publish_cbuffer_aliases "
            "with semantic_fields_json after the unified validation and inspect written/preserved/"
            "skipped semantic-field counts. This display projection does not alter captured layout "
            "or require recompiling the same source. Carry the separate panel result into whole-task "
            "acceptance rather than treating source names or the material dictionary as proof of it. "
            "Reuse unchanged investigation and repair only concrete remaining naming/publication gaps. "
            "Apply the complete "
            "organization requirements while repairing, not as optional final polish. Read source "
            "only where needed, and assess overall readability, Unity-style logical structure "
            "while preserving the captured interface, function calls and data flow, and redundant "
            "intermediate variables. Inspect same-operation two-to-four-component chains and expanded scalar/vector constructors for vector expressions, "
            "recognized math for suitable intrinsics, and the actual call hierarchy for coherent "
            "subsystems and reusable calculations. Renamed XYZ scalars or a flat list of extracted "
            "helpers alone do not establish readable organization. Retain scalar steps or a flat "
            "helper where their actual semantics or responsibility justify it, not under a blanket "
            "precision claim. Follow the Skill's source-specific review guidance; do not impose "
            "fixed function counts, nesting depth or a lighting template. You decide whether the "
            "source meets the request. "
            "Use current source observations to inspect interleaved channel families, repeated "
            "function bodies, long assignments and missing adjacent function summaries too. "
            "A zero match count only describes that detector. Simplify same-role value lifetimes "
            "and decompose dense formulas into meaningful contributions; preserve local operation "
            "order with readable vector stages where needed instead of retaining entire scalar regions. "
            "For broad organization, reconsider existing function boundaries as well as bodies; "
            "derive responsibilities from the computation rather than preserve the old function list. "
            "For every requested computation scope, state the actual outcome or a specific retention "
            "reason for body simplification, responsibility/call design, shared algorithm implementations "
            "and shared computed results in the existing scope evidence. Inspect remaining expressions, "
            "unchanged helpers and related variants for counterexamples. A feasible gap keeps its scope "
            "unmet; improvements elsewhere do not compensate for it. Reusing a helper does not prove "
            "that repeated results are reused. Apply these decisions only to the requested scope and "
            "affected dependencies; do not expand a narrow edit or explanation into a whole-program rewrite. "
            "Check plain-language summaries for every business function, the major stages of complex "
            "functions, and detailed notes beside non-obvious algorithms and branches. Comments must "
            "describe the actual current implementation. Apply suitable math identities and intrinsic "
            "forms under the actual numerical contract. Actual calls must form a directory-like "
            "responsibility hierarchy: major operations own sub-operations and concrete algorithms. "
            "Trace every requested branch and business function; one representative chain cannot "
            "cover siblings or fallbacks. Shared helpers may have several callers. Do not add "
            "recursive calls, empty forwarding layers or artificial depth. "
            "Repair actionable gaps together in the same working copy, then review the changed "
            "source again. Draft writes/imports persist progress without compilation or replay. "
            "Complete the whole requested source and its self-review before compile, ABI checks "
            "and final apply. After that final validation fails, focused physical comparisons "
            "of changed diagnostic trials are authorized recovery, not ordinary intermediate "
            "publication. Follow the loaded numeric-recovery contract without marking an "
            "intentionally partial trial as complete or replacing the full intended proposal. "
            "Ordinary source editing must not open/reload the capture or recover replacement "
            "ownership; the Host coordinates ownership during final apply. Use already known "
            "tools and schemas directly, and discover only a specific missing contract. "
            "Record your actual assessment "
            "with record_shader_organization_review and the exact current source SHA-256. "
            "Include semantic_overview with the independently analyzed meaningful calculation stages "
            "and a current-source review of their granularity, data flow and scope ownership. "
            "Use mesh, texture and draw-state observations as working hypotheses, then check the actual "
            "expressions and consumers. Revisit broad directory and function-graph groups that hide "
            "independently explainable major calculations or useful intermediate results; expose those "
            "parts where they improve navigation, with small supporting operations inside their owner. "
            "The entry's direct-call list or every helper comment is not the completed directory. "
            "Choose useful calculation boundaries from the full program, without fixed techniques or counts. "
            "When only the directory is missing/stale, use get_shader_semantic_overview_context then "
            "update_shader_semantic_overview; retain valid source review and avoid recompiling or "
            "rewriting already completed source merely to repair navigation metadata. "
            "In that same scope_review, use observation_dispositions for every candidateId in the "
            "complete catalog linked by the latest write/import sourceReviewObservations.catalog.relativePath. "
            "Inline examples are a preview, not the scope. Each item needs candidate_id, scope_ids, "
            "retained or needs_work status and evidence. Retained also needs a concrete alternative "
            "and source_anchor {start_line,end_line,text} matching the relevant current calculation. "
            "Explain why the alternative cannot apply or is less clear here. A still-present candidate "
            "cannot be called resolved. Read the catalog once and batch repairs, never call or compile "
            "per candidate. An empty set still requires full-scope review. "
            "Include scope_review bound to the initial whole-source hash: the planned function/path "
            "groups and a matching assessment of each group's current functions, branches, retained "
            "intermediates and remaining work. Reuse the original scope plan and valid unchanged "
            "evidence; a clean entry cannot stand in for reviewing its helper bodies or fallback paths. "
            "Use needs_work for remaining repairs, complete only after the requested organization "
            "and all four checks are satisfied, or blocked with a concrete reason if you cannot "
            "proceed. An unchanged source or a passing compile alone is not evidence of newly "
            "completed organization. If all requested criteria already hold without edits, record "
            "complete with an explicit no_change_reason and report that this turn made no source changes. "
            "After recording a current complete review, finish/save the reviewed proposal and "
            "perform any requested final apply before answering. A complete review that is already "
            "fresh can be reused when saving; do not repeat review or compile just to save. "
            "If the existing source already satisfies the request without changes, report that "
            "honestly; do not make meaningless edits just to change its hash.\n\n"
            "Current Host source/review identities (Agent assessments are not Host quality verdicts):\n"
            + json.dumps(self.targets, ensure_ascii=False, separators=(",", ":"))
        )

    def incomplete_text(self, language: str) -> str:
        zh = language == "zh_CN"
        if self.reason == "blocked":
            text = "着色器整理尚未完成：Agent 自检记录了以下阻塞。" if zh else (
                "Shader organization is incomplete: the Agent's self-review records these blockers."
            )
        elif self.reason == "unfinished-tools":
            text = "着色器自检未完成：本轮仍有未结束的工具，无法安全续接；已有修改已保留。" if zh else (
                "Shader self-review is incomplete: tools were still unfinished, so the task could not "
                "be continued safely. Existing edits were preserved."
            )
        elif self.reason == "review-state-unavailable":
            text = "着色器自检记录暂时无法读取；无法确认当前源码已完成自检，已有修改已保留。" if zh else (
                "Shader self-review records could not be read, so a current review could not be confirmed. Existing edits were preserved."
            )
        else:
            text = "着色器整理尚未完成：提醒自检后，源码或自检状态仍未推进；已停止重复续接并保留已有修改。" if zh else (
                "Shader organization is incomplete: after a self-review reminder, the source or "
                "review state did not advance. Repeated continuation was stopped and existing edits were preserved."
            )
        details: list[str] = []
        for target in self.targets:
            review = target.get("review")
            review = review if isinstance(review, Mapping) else {}
            values = [target.get("diagnostic"), review.get("blocker"), *(review.get("findings") or []), *(review.get("nextActions") or [])]
            if target.get("reviewStatus") == "unchanged":
                values.insert(0, "尚未记录实际源码整理修改。" if zh else "No source organization changes were recorded.")
            elif target.get("reviewStatus") in {"missing", "stale"}:
                values.insert(0, "尚未提交当前源码版本的完整自检。" if zh else "A complete self-review for the current source is still missing.")
            elif target.get("reviewStatus") == "scope-incomplete":
                values.insert(0, "源码自检缺少完整函数与分支范围的结构化记录。" if zh else "Source self-review lacks structured coverage of its full function and branch scope.")
            elif target.get("reviewStatus") == "overview-pending":
                values.insert(0, "当前源码的独立计算目录尚未完成分析和复核；可单独补充，无需重新编译。" if zh else "The current source still needs an independently analyzed calculation directory; update its metadata without recompiling.")
            elif target.get("reviewStatus") == "overview-display-pending":
                values.insert(0, "已审核的计算目录尚未在目标着色器界面中确认显示；只需修复显示，无需重新编译。" if zh else "The reviewed calculation directory has not been verified in the target Shader editor; repair its display without recompiling.")
            elif target.get("reviewStatus") == "unsaved":
                values.insert(0, "已自检的草稿尚未保存为工作区源码。" if zh else "The reviewed proposal has not been saved as workspace source.")
            elif target.get("reviewStatus") == "semantic-delivery-pending":
                values.insert(0, "当前源码对应的资源名称、CBuffer 通道分组或解包资源使用复核尚未完成。" if zh else "Resource naming, CBuffer channel publication or unpacked-asset usage review is incomplete for the current source.")
            for value in values:
                if isinstance(value, str) and value.strip() and value.strip() not in details:
                    details.append(value.strip()[:600])
        if not self.targets and self.reason != "review-state-unavailable":
            details.append("尚未创建或整理目标着色器。" if zh else "The target Shader was not created or organized.")
        return text + ("\n\n" + "\n".join("- " + item for item in details[:8]) if details else "")


@dataclass
class ShaderReviewLoop:
    explicit: bool = False
    _last_pending: tuple[Any, ...] | None = field(default=None, init=False)

    @classmethod
    def for_intent(cls, intent: Mapping[str, Any] | None) -> "ShaderReviewLoop":
        return cls(explicit=isinstance(intent, Mapping) and intent.get("kind") == "reverse-edit-selected-shader")

    def decide(self, state: Mapping[str, Any], *, detect_stall: bool = True) -> ShaderReviewDecision:
        if state.get("error"):
            return ShaderReviewDecision("partial", "review-state-unavailable", ({"review": {"blocker": str(state["error"])[:800]}},))
        rows = state.get("targets")
        rows = rows if isinstance(rows, list) else []
        targets: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            transfer = row.get("unityTransfer") or {}
            if not self.explicit and transfer.get("diagnosticsOnly"):
                # A conflicting original-workspace draft is still useful
                # diagnostic evidence. It must not lock independent Unity work
                # or force publication of the original API's source/UI state.
                continue
            if (not self.explicit and transfer.get("referenceOnly") is True
                    and transfer.get("sourceSha256") == row.get("sourceSha256")
                    and transfer.get("restorationId") and not row.get("sourceConflict")):
                # The Host matched this exact saved draft to a bound Unity
                # restoration. Unity still owes compile/execution/visual review;
                # the incomplete source replay path must not restart that loop.
                continue
            # Opening an unchanged draft for inspection does not turn a plain
            # follow-up question into a new source-editing obligation. The
            # general task review still assesses the user's actual request.
            # Unknown/legacy provenance and failed reads remain conservative.
            if (not self.explicit and row.get("sourceWorkObserved") is False
                    and row.get("changed") is False and not row.get("review")
                    and not row.get("sourceConflict") and not row.get("diagnostic")
                    and len(str(row.get("sourceSha256") or "")) == 64):
                continue
            review = row.get("review")
            review = review if isinstance(review, Mapping) else {}
            verdict = review.get("verdict")
            fresh = row.get("fresh") is True
            changed = row.get("changed") is True
            saved = row.get("saved", True) is True
            if fresh and verdict == "blocked" and str(review.get("blocker") or "").strip():
                review_status = "blocked"
            elif not review:
                review_status = "missing"
            elif not fresh:
                review_status = "stale"
            elif verdict == "complete" and row.get("scopeCoverageComplete") is False:
                review_status = "scope-incomplete"
            elif verdict == "complete" and row.get("semanticOverviewComplete") is False:
                review_status = "overview-pending"
            elif verdict == "complete" and row.get("semanticOverviewDisplayComplete") is False:
                review_status = "overview-display-pending"
            elif verdict == "complete" and not saved:
                review_status = "unsaved"
            elif not changed:
                review_status = ("unchangedAccepted" if verdict == "complete"
                                 and str(review.get("noChangeReason") or "").strip() else "unchanged")
            else:
                review_status = str(verdict) if verdict in {"needs_work", "complete"} else "missing"
            semantic_delivery = row.get("cbufferSemanticDelivery")
            semantic_delivery = semantic_delivery if isinstance(semantic_delivery, Mapping) else {}
            if (review_status in {"complete", "unchangedAccepted"}
                    and _semantic_delivery_pending(semantic_delivery, str(row.get("sourceSha256") or ""))):
                review_status = "semantic-delivery-pending"
            target = {
                "workspacePath": str(row.get("workspacePath") or "")[:1024],
                "organizationId": str(row.get("organizationId") or "")[:128],
                "sourceKind": str(row.get("sourceKind") or "")[:32],
                "sourceSha256": str(row.get("sourceSha256") or "")[:64],
                "changed": changed,
                "saved": saved,
                "canonicalSourceSha256": str(row.get("canonicalSourceSha256") or "")[:64],
                "sourceConflict": row.get("sourceConflict") is True,
                "diagnostic": str(row.get("diagnostic") or "")[:800],
                "reviewStatus": review_status,
                "review": {
                    "verdict": str(verdict or ""),
                    "findings": _strings(review.get("findings")),
                    "nextActions": _strings(review.get("nextActions", review.get("next_actions"))),
                    "blocker": str(review.get("blocker") or "")[:1600],
                    "noChangeReason": str(review.get("noChangeReason") or "")[:1600],
                    "scopeGaps": _scope_gaps(review.get("scopeReview")),
                },
            }
            if semantic_delivery:
                target["cbufferSemanticDelivery"] = dict(semantic_delivery)
            if row.get("semanticOverviewDisplayRequired") is True:
                target["semanticOverviewDisplay"] = dict(row.get("semanticOverviewDisplay") or {})
            targets.append(target)
            if review_status == "blocked":
                blocked.append(target)
            elif review_status not in {"complete", "unchangedAccepted"}:
                pending.append(target)
        if not targets and not self.explicit:
            return ShaderReviewDecision()
        if not pending and targets:
            return (ShaderReviewDecision("partial", "blocked", tuple(blocked)) if blocked
                    else ShaderReviewDecision(targets=tuple(targets)))
        # A new note, timestamp, repeated read, or changed organization ID alone
        # cannot perpetually reset the no-progress guard. A source change or a
        # newly submitted assessment can. There is no fixed review-round limit.
        fingerprint = tuple(sorted(
            (target["workspacePath"], target["sourceSha256"], target["changed"], target["reviewStatus"],
             target["saved"], target["canonicalSourceSha256"], target["sourceConflict"],
             semantic_delivery_progress(target.get("cbufferSemanticDelivery")))
            for target in pending
        )) or (("unstarted-organization",),)
        if detect_stall and fingerprint == self._last_pending:
            return ShaderReviewDecision("partial", "no-progress", tuple(pending + blocked))
        if detect_stall:
            self._last_pending = fingerprint
        return ShaderReviewDecision("continue", "self-review-required", tuple(pending + blocked))


def _strings(value: Any) -> list[str]:
    return [item[:600] for item in value[:12] if isinstance(item, str)] if isinstance(value, list) else []


def _scope_gaps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping) or not isinstance(value.get("results"), list):
        return []
    return [{"id": str(row.get("id") or "")[:128], "status": str(row.get("status") or ""),
             "remainingWork": _strings(row.get("remaining_work"))}
            for row in value["results"] if isinstance(row, Mapping) and row.get("status") != "satisfied"]


def review_state(store: Any) -> dict[str, Any]:
    """One cheap ledger read at a provider turn boundary, never tool polling."""
    read = getattr(store, "shader_organization_review_state", None)
    try:
        value = read() if callable(read) else {}
    except Exception as error:
        from .agent_diagnostics import public_error
        return {"error": public_error(error)}
    return dict(value) if isinstance(value, Mapping) else {}


def emit_review_continuation(reporter: Any, decision: ShaderReviewDecision) -> None:
    reporter.writer.emit(
        "running", summary="Agent Shader self-review is continuing",
        data={"phase": "shader-self-review", "status": "running", "agentId": "main",
              "activityId": "main-shader-self-review", "activityKind": "agentReview",
              "activityTitleId": "task.shader-review.started", "activityTitleArgs": [],
              "reviewReason": decision.reason, "reviewTargets": list(decision.targets),
              "hostVerified": False, "verification": "agentAssessment"},
    )
