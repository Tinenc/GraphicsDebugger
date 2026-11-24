# ReplayLab AgentHost — Runtime gate & lifecycle audit

> Historical audit only. The runtime and gates named below were deleted; use `../../ARCHITECTURE.md` for the current implementation and do not restore code from this report.

Scope: `/home/claude/work/repo/replaylab/src/replaylab_agent/` (runtime.py, orchestration.py,
adaptive_orchestration.py, policy.py, planning.py, errors.py). Event logs in
`/home/claude/work/logs/*.summary.txt` + `/home/claude/work/logs/ai-tasks/*.jsonl`.

All five audited runs use the **codex / gpt-5.6-sol** external‑agent preset
(`profile.provider.kind == "agent"`), so they are dispatched to
`AgentRuntime._run_external_plan` (runtime.py:14774), selected at runtime.py:11329‑11339:

```python
elif profile.provider.kind == "agent" and capture_bound and not skill_name:
    if workflow_resume_state is None:
        result = await self._run_external_plan(profile, prompt, run_context, writer, ...)
```

`_run_model_plan` (12157) / `_run_model_skill` (13571) are the **model‑provider** twins and are
NOT exercised by these runs (they only fire for `provider.kind == "model"`). Their duplicated
"Failed and released" (13188/13335), "Host requested evidence correction" (11541), "Validating
the frozen target" (14497/14581) emitters are dead for the codex preset — see §3.

---

## TL;DR of the defect

For a simple capture request the Host runs **planner → (admit) → child dispatch → child‑skill‑selection
→ external child (`_run_external`) → in‑child frozen‑target/evidence review → back to execute()'s
correction/reroute loop → supervisor‑review (reviewer LLM) → terminal**. Every stage is fenced by
**deterministic Host validators** that raise `PolicyError` / `EvidenceValidationError`. Two design facts
combine into the user‑visible failure:

1. A `PolicyError` that is *terminal* (`AuthorizationPolicyError`, `SafetyPolicyError`,
   `HostLedgerCorruption`, `MutationAuditUnavailable`, or any `.terminal_policy_violation`
   attribute — runtime.py:`_terminal_policy_violation` 4372) **releases the child immediately**
   with no real retry. A non‑terminal `PolicyError` is *reclassified* into an
   `EvidenceValidationError` "observation" and retried — but the retry re‑enters the **same
   deterministic gate** and fails identically.
2. The real diagnostic is stripped to one generic sentence by `_exception_summary`
   (runtime.py:24595): `PolicyError → "该操作未通过安全检查。"` and
   `EvidenceValidationError → "子任务结果未通过验证。"`. The raw reason is **never persisted to disk**
   and **never fed back to the reviewer/child LLM** (only the generic sentence is), so neither the
   model nor the user can correct the actual problem.

The reviewer/child therefore keep re‑proposing plans that are re‑rejected by the same gate; after
the tiny decision budget is spent the run ends `terminalStatus:"partial"` /
"Supervisor decision: blocked" with the boiler‑plate "尚未得到可完整确认的结果".

---

## 1. The full pipeline and every deterministic gate

Pipeline for one codex request (`_run_external_plan`, 14774):

```
planner (zero-tool supervisor, external_initial_supervisor_prompt)     14908-14966
  → admit_initial_decision (adaptive_orchestration.py:7540)            15013
  → validate_plan_authority / _validate_initial_routing_plan           15025-15055
  → [terminal decision branch: direct_answer / ask_user / blocked]     15056-15123  (runs AdaptiveSupervisorWorkflow with unreachable execute/decide)
  → [proposal branch] _bind_stage_evidence_plan / checkpoint plan      15139-15193
  → execute_stage → execute(task)                                      15195 / 15266
       child-skill-selection: _select_external_child_skill            15558
       per-attempt loop (while attempt < max_total_attempts)          15698
         → _run_external(...)  (the actual child)                     15764 / 16545
              in-child review: "Validating the frozen target ..."     18038-18209
         except ChildReceiptValidationError → "Host is repairing ..." 15787
         except EvidenceValidationError → "Host requested evidence correction" / "Failed and released" 15821
         except ReplayLabAIError(incl PolicyError) → reroute/observation/raise 15925
       → ChildSession.accepted "Accepted and released"                16232-16273
  → execute_safely wraps execute(): "Failed and released ..."         16275-16331
  → supervisor_review = decide() (reviewer LLM)                       16381
       DispatchNextStage → _admit_task_routing                        16442-16450
  → AdaptiveSupervisorWorkflow._supervisor_review admit_dispatch      adaptive_orchestration.py:5025-5252
  → _finish → terminal_status                                          adaptive_orchestration.py:5477
```

### Gate table (deterministic validators that can raise / degrade a run)

| # | Gate | File:line | Checks | On failure |
|---|------|-----------|--------|-----------|
| 1 | planner task‑budget | runtime.py:14954 | `len(initial.plan.tasks) > max_total_tasks` | `PolicyError` → planner retry (≤2) then deterministic fallback plan |
| 2 | `validate_plan_authority` (initial) | planning.py:356; called 14958/15025 | task.effects ⊆ authorized_effects; shader_route required_outcomes → implied effects (`shader_mode_effects` 373‑384; outcome→effect map 393‑438) | `PolicyError "…outside the Host authority envelope"` / "Shader route outcomes require workflow effects…"; converted to `fallback_clarification`/`_explicit_agent_routing_clarification` (15040‑15055) |
| 3 | `_validate_initial_routing_plan` | runtime.py:14961/15029 | plan tasks match registry routing | `PolicyError` → clarification fallback |
| 4 | `admit_initial_decision` | adaptive_orchestration.py:7540 | rebuilds shader clarification from **Host frozen candidates** (`_bound_shader_candidates`), rejects model‑authored option sets, remembers/ restores Host shader goal | can convert ask_user→proposal (legacy_proposal), ask_user→`InitialBlockedDecision`, or proposal→clarification |
| 5 | `_run_external` planning‑turn / skill guards | runtime.py:16566‑16814 | read‑only template vs effects (16599); `plan_skill != skill_name` (16650); capture instance pinned (16691); continuation reservations (16814/16818) | `PolicyError`/`ConfigurationError` — **pre‑model**, raised before the child LLM runs |
| 6 | `_requested_workflow_effects` shader ceiling | runtime.py:18242 / 18258 / 18264 | planned effects ⊆ `shader_route.authorized_effects`; continuation effect closure | `AuthorizationPolicyError` (**terminal**) → immediate release |
| 7 | tool exec diagnostic | tool_service (surfaced as `ToolExecutionDiagnosticError`, errors.py:36) | a local tool returned a non‑retryable diagnostic (e.g. `route-unavailable`) | model‑visible diagnostic; child usually **yields** |
| 8 | `WorkflowGuard.validate_yield_boundary` | policy.py:5817 | mutation_uncertain on a yield | `EvidenceValidationError` (retryable) |
| 9 | `WorkflowGuard.validate_completion` | policy.py:5798 | requested effects completed; replay postcondition present | `EvidenceValidationError` (retryable) |
| 10 | `_validate_child_evidence_route` | runtime.py:5407 (called 18068) | child‑selected execution skill produced a valid evidence route | `EvidenceValidationError`/`RouteEvidenceUnavailable` |
| 11 | `_validate_workflow_sequence` | runtime.py (called 18076, only when **not** yielded) | tool ordering vs effects | `EvidenceValidationError` |
| 12 | `_validate_structured_child_output` | runtime.py:22463 (called 18126) | v2 receipt: frozen target/hashes, coverage, scope, snapshot id | `EvidenceValidationError` / `ChildReceiptValidationError` (retryable) |
| 13 | `_raise_if_recovered_effect_would_be_corrected` | runtime.py:6338 (18091/18161) | a recovered effect must not be "corrected" away | re‑raises as terminal if a mutation would be undone |
| 14 | execute() `except ReplayLabAIError` terminal‑policy | runtime.py:15934 | `isinstance(exc,PolicyError) and _terminal_policy_violation(exc)` | `raise` → execute_safely → **"Failed and released … 该操作未通过安全检查"** |
| 15 | execute() `except ReplayLabAIError` non‑terminal PolicyError | runtime.py:16079‑16136 | ordinary PolicyError → convert to `EvidenceValidationError` observation | retry same child; **emits no progress event**; at `local_attempt>=limit` raises observation (16129) |
| 16 | execute() generic recoverable | runtime.py:16137‑16174 | recoverable disposition + local budget | "Child is rerouting … locally" (16167) then, if not recoverable / budget spent, `raise` (16147) |
| 17 | execute() EvidenceValidationError frontier | runtime.py:15861‑15904 | `evidence_frontier_exhausted` | "Failed and released" (15895) |
| 18 | reviewer routing admission | runtime.py:16445 `_admit_task_routing` | reviewer DispatchNextStage plan vs registry routing | `ValueError`/`ReplayLabAIError` → reviewer retry (≤2) |
| 19 | `admit_dispatch` (supervisor_review) | adaptive_orchestration.py:4794 | `validate_plan` → `_canonicalize_initial_shader_plan` → `_bind_event_continuation_plan` → `validate_plan_authority` → `_validate_event_next_phase_plan` → `_validate_assistance_resolution_plan` → `validate_delegation_plan` → `_validate_intra_plan_generic_effect_operation_uniqueness` → stage/task budget | any `PolicyError` → caught 5165, one bounded retry, else **`BlockedDecision`** (5206/5243) → "Supervisor decision: blocked" |
| 20 | `_route_after_review` dedup | adaptive_orchestration.py:5269‑5437 | proposed task fingerprint vs prior **accepted‑completed** fingerprints; supervisor_resolutions keys | "retry" (route_retry) up to `MAX_SUPERVISOR_DECISIONS`, else finish |
| 21 | `_finish` terminal gates | adaptive_orchestration.py:5477‑5686 | assistance/event/delivery/goal reviews; `_unresolved_recoverable_operation_failures` (1027) | `partial_result` (5487) / `blocked_result` (5526) / `needs-user-input` (5515) |
| 22 | initial‑clarification checkpoint | runtime.py:5724‑5730 + adaptive_orchestration.py:1800 | needs‑user‑input report **must have `report.stages`** | no durable checkpoint built (returns None) / `from_report` raises "an initial clarification has no workflow state to checkpoint" |

Terminal `PolicyError` subclasses (release immediately, no retry) — errors.py:180‑213:
`AuthorizationPolicyError`, `SafetyPolicyError` (both set `terminal_policy_violation = True`),
`HostLedgerCorruption`, `MutationAuditUnavailable`. Everything else (`TaskPolicyViolation`, bare
`PolicyError`) is "non‑terminal" and gets reclassified into an `EvidenceValidationError` observation
(gate 15).

---

## 2. Run‑by‑run mechanism

### (a) a92176d8 — child returns v2 `yielded`, coverage "not‑met" → "Host requested evidence correction (1/4)" then instant "Failed and released" PolicyError

Sequence (jsonl [2395]‑[2397]): `child-review started "Validating…"` → `child-review retrying
"Host requested evidence correction" progress 1/4` → `child "Failed and released … 该操作未通过安全检查"`.

- This run **resumed a Host‑stored shader goal**: the plan event carries stageObjective
  `"Resume the exact Host-stored Shader goal"`, `fallback:false`, `migratedLegacyPlan:false`. That
  string is emitted only by `_restored_shader_goal_proposal` (adaptive_orchestration.py:7507‑7537),
  which **overwrites the LLM plan** with the Host‑stored `ShaderGoalMemory` route/effects.
- Attempt 1: the child returns a valid‑looking `replaylab.child-output/v2` with `status:"yielded"`
  and 6 shader coverage criteria `not-met` (recovery/readable/organized/fidelity/compiler/replay).
  Inside `_run_external`, `validate_yield_boundary` passes (read‑only, no mutation), but
  `_validate_structured_child_output` (18126) raises a **non‑terminal `EvidenceValidationError`**
  (required coverage unmet). Caught by execute()'s `except EvidenceValidationError` (15821); because
  the local frontier is not yet exhausted it takes the correction branch (15905‑15924) and emits
  **"Host requested evidence correction" with `progress_total = max_total_attempts = 4`** (15916).
  `attempt_prompt` is reset to `base_prompt` (15910) and the loop continues.
- Attempt 2: `_run_external` is called again with the retained `evidence_correction` state. It
  **raises a terminal PolicyError before the model produces any output** — the shader effect‑ceiling
  re‑admission (`_requested_workflow_effects` 18242 `AuthorizationPolicyError "Shader goal envelope
  does not contain the planned effect ceiling"`, or the evidence‑correction ceiling check
  16829‑16833 `AuthorizationPolicyError "evidence correction changed the immutable workflow effect
  ceiling"`). Because it is a **terminal** `PolicyError`, execute()'s `except ReplayLabAIError`
  hits gate 14 (15934): `raise` → `execute_safely` → **"Failed and released … 该操作未通过安全检查"**
  (16324).

**Why the correction retry never really happened:** the "correction" is a fresh `_run_external`
call whose *setup* (effect‑ceiling / continuation admission) is a deterministic terminal gate. The
retained goal route requires effects (workspace/replay for `compiled-candidate` /
`replay-abi-validation`) that the resumed authority ceiling does not contain, so attempt 2 dies in
admission before the model can correct anything. `progressTotal:4` is cosmetic — the loop can raise
terminally on the very next iteration.

**Exact raise sites for the "该操作未通过安全检查" text:** any of `AuthorizationPolicyError`
(errors.py:180) raised at runtime.py:16831, 18243, 18258, 18264; `PolicyError` at 16599/16650/16814/
16819. All map through `_exception_summary` (24621‑24623) to the single sentence. (The child‑output
JSON is genuine model output; the *release* reason is the Host PolicyError, not the child's text.)

### (b) 57c9 — child‑review "Validated the frozen target and evidence receipts" (success) then instant "Failed and released" PolicyError

jsonl [2455]‑[2457]: `Validating…` → **`Validated the frozen target and evidence receipts`
(completed)** → `Failed and released … 该操作未通过安全检查`.

- The child called `inspect_shader`✓, `get_shader_toolchain_capabilities`✓, then
  `plan_shader_toolchain_route` and `plan_shader_recovery` both **failed** with
  `ToolExecutionDiagnosticError` / `errorCode:"tool-configuration"` /
  `diagnosticCategory:"route-unavailable"` / `diagnosticRetryable:false` (gate 7). The child then
  yielded (`status:"yielded"`), all forward shader coverage `not-met`.
- `_run_external`'s in‑child review (18051‑18209) validates a **yield**: `validate_yield_boundary`
  (no mutation) ✓, `_validate_child_evidence_route` ✓, `_validate_structured_child_output` ✓ →
  emits **"Validated the frozen target and evidence receipts"** (18208). Note `_validate_workflow_sequence`
  is *skipped* for a yield (18075 `if not safe_incomplete`).
- The gate that fires *after* that successful validation is the **effect/authority ceiling**, not an
  evidence gate. The frozen‑target validation only proves identity + receipt integrity; it does not
  clear the shader effect ceiling. The subsequent terminal `PolicyError`/`AuthorizationPolicyError`
  (shader effect‑ceiling admission — `_requested_workflow_effects` 18242, or the accept‑side
  reconciliation of a yield whose required `compile-only` outcomes imply workspace/replay authority
  the request never granted) releases the task via gate 14. Because the released exception is a
  `PolicyError` (not `EvidenceValidationError`), the surfaced sentence is "该操作未通过安全检查"
  rather than "子任务结果未通过验证" — confirming it is an authority/policy gate, not the evidence
  validator, that fires post‑validation.

> Residual: the *exact* line of the post‑validation terminal raise for a yielded, route‑unavailable
> shader child is masked by redaction (the whole point of §4). The candidate set is small and all
> terminal: 18243 / 18258 / 18264 (`_requested_workflow_effects`), 16831 (evidence‑correction ceiling).
> The observable invariant — "successful frozen‑target validation is immediately followed by a
> terminal authority PolicyError" — is certain.

### (c) 526e79d3 ("帮我修正") — child‑skill‑selection done, "Child is rerouting … locally" (1/4), then "Failed and released" with **zero** model output / tool calls

Raw jsonl proves this is **pure deterministic gating**: the whole run is 1.34 s
(ts 1788247952447→953787); the four child‑review events fire in **~20 ms** (953434→953457) — no time
for any model call.

- Planner produced a **fallback** legacy plan (`fallback:true`, `plannerAttempts:2`,
  `migratedLegacyPlan:false`, skill `renderdoc-explain-event`, stageObjective "Inspect the exact
  current EID 11329"). "帮我修正" is a bare imperative → `admit_initial_decision` / `fallback_clarification`
  route it to a read‑only explain‑event inspection (adaptive_orchestration.py:6181‑6190).
- execute() enters the attempt loop; each `_run_external` (15764) **raises during setup, before the
  model runs**. The first three raises are *recoverable non‑policy* `ReplayLabAIError`s (e.g.
  `ConfigurationError` "task Skill … has no executable live tool on the selected Agent transport"
  16800, or an event‑route admission `PolicyError` at 15387) → gate 16 recoverable branch →
  **"Child is rerouting renderdoc-explain-event locally"** (16167), `progress 1/4 … 3/4`.
- Attempt 4 (`attempt == max_total_attempts == 4`) raises a **terminal** `PolicyError` → gate 14
  → `execute_safely` → **"Failed and released 帮我修正: 该操作未通过安全检查"**.

So the PolicyError is raised **before the child ever runs**, by a pre‑model setup gate in
`_run_external` (16566‑16814) / execute() setup (15363, 15387, 15426, 15536). The four "rerouting"
retries are the loop churning deterministic setup rejections at microsecond speed; there is no model
output because the model is never reached.

### (d) 9e4888 / 719fdf6a — supervisor answered `ask_user` with vertex/fragment options, run ended `partial`, **user never saw the question**

Both are stage‑less **initial** clarifications: planner → `Supervisor decision: ask_user` (jsonl
[296]/[5]) → `completed terminalStatus:"partial"` with the boiler‑plate
"尚未得到可完整确认的结果。请求尚未全部完成：仍有结果未通过验证。" and **no `clarification`
field** in the terminal event.

Admission path (`_run_external_plan` terminal branch 15056‑15123 → `AdaptiveSupervisorWorkflow.run`
→ `_admit_initial` 4454 → `_finish`/`clarification_result` 5490):

- The model **never owns the option set**. `admit_initial_decision` (adaptive_orchestration.py:7890)
  discards the model's ids unless `decision.is_host_grounded`; a genuine shader ambiguity is
  **rebuilt from Host frozen candidates** via `_host_shader_clarification` (7816 / 6214), keyed on
  `_bound_shader_candidates(context)` and `shader_read_only_attemptable`. `clarification_id` and the
  option ids come from the Host, not the model.
- **Contrast that proves the mechanism:** run **58ff4eab** (same prompt "反编译这个shader", also
  stage‑less initial ask_user) succeeded with `terminalStatus:"needs-user-input"`. Its *published*
  clarification (jsonl [221]) carries **Host** option ids `vertex`/`fragment` with rawSha256 evidence
  — i.e. `_host_shader_clarification` fired and produced a `bind_host_grounding()` decision (7105).
  In 9e4888/719fdf6a the Host rebuild did **not** take (the model ids `fragment-mainps`/`vertex-main`
  survive in the transcript and no Host‑grounded clarification is published) — the shader candidates
  were not host‑bindable/attemptable at plan time, so admission fell through to `fallback_clarification`
  (7906). `fallback_clarification` (6161) with an EID loaded returns a `legacy_proposal` inspection
  **proposal** (6189) or, when no exact candidate is constructible, an `InitialBlockedDecision`
  (6225).
- **Why the question is dropped and it degrades to silent partial:** an initial clarification has
  **no stages**, so it is **not resumable** —
  `_prepare_workflow_checkpoint` bails at the `not report.stages` guard (runtime.py:5724‑5730) and
  `WorkflowCheckpointState.from_report` raises "an initial clarification has no workflow state to
  checkpoint" (adaptive_orchestration.py:1800). A clarification that is not cleanly host‑grounded
  collapses through the `BlockedDecision` path in `_finish` (5668‑5685) →
  `_workflow_requires_hard_block` is False with no stages → **`return partial_result(decision.reason)`**
  (5685) → `terminal_status:"partial"` with `fallback_partial_answer` (10250). The *rejection reason*
  is discarded (the branch renders the generic partial), so the user sees neither the question nor
  *why* it was refused.

> Residual: 58ff4 vs 9e4888 are near‑identical inputs; the divergence is a runtime context
> difference (whether `context["shaders"]` bound candidates were present/attemptable) that is not in
> the redacted logs. The **gates responsible** (host‑grounding at 7816/7890/7906, the stage‑less
> checkpoint bar at 5728 + 1800, the Blocked→partial collapse at 5685) are certain; which one 9e4888
> hit specifically cannot be proven from the logs because the reason is redacted.

### (e) a92176d8 / 57c9 — reviewer returned `dispatch_next_stage` with a corrected plan, Host logged "Supervisor decision: blocked"

Both transcripts show **two** reviewer `dispatch_next_stage` messages, then `Supervisor decision:
blocked`. The conversion lives in `AdaptiveSupervisorWorkflow._supervisor_review`
(adaptive_orchestration.py):

```
5025  decision = await self.decide(review_state)
5027  if isinstance(decision, DispatchNextStageDecision):
5028      decision = admit_dispatch(decision)          # gate 19
...
5165  except PolicyError as exc:
5171      host_feedback = safe_policy_feedback(prefix, exc)   # <-- reason reduced to a category
5172      if decision_attempts < decision_attempt_limit: continue   # exactly ONE bounded retry
...
5243  decision = BlockedDecision(reason="The Host rejected two incompatible supervisor decisions.", …)
```

`decision_attempt_limit = min(2, remaining_decisions)` (5011), so the reviewer gets **one** replan;
both `admit_dispatch` calls raise → `BlockedDecision` (5243) → `_finish` → partial. `admit_dispatch`
(4794‑4854) runs, in order: `validate_plan` → `_canonicalize_initial_shader_plan` →
`_bind_event_continuation_plan` → **`validate_plan_authority`** → `_validate_event_next_phase_plan`
→ `_validate_assistance_resolution_plan` → `validate_delegation_plan` →
`_validate_intra_plan_generic_effect_operation_uniqueness` → stage/task budget.

**Which validation most likely failed for these payloads:** the shader **effect‑authority mapping**
in `validate_plan_authority` (planning.py:393‑438). The reviewer plans carry
`shader_route.required_outcomes` such as `compiled-candidate`, `executable-representation`,
`replay-abi-validation` with `effects:["workspace"]` only, and `intent_contract.operation =
"validated-reconstruction"`. The outcome→effect map (396‑425) forces `workspace` (and for
replay‑replacement‑class outcomes, `replay`); when the required effect is missing **but already
inside the authority envelope**, line 433 raises
`PolicyError("Shader route outcomes require workflow effects that the plan did not authorize;
reconstruction/build artifacts require workspace authority and live replacement also requires replay
authority")`. The reviewer itself wrote "上次两次尝试都在执行前的任务契约校验阶段被拒绝" — confirming
a pre‑execution *task‑contract* rejection, i.e. this authority/route gate (or its sibling
`_canonicalize_initial_shader_plan` 7217). `safe_policy_feedback` (4881) reduces even this to
`"…: outside the Host authority envelope"` / bare code, so the reviewer never learns the exact field.

> The intra‑plan uniqueness gate (2877) is *not* the cause here — it requires ≥2 tasks (2900) and
> these plans are single‑task. `_validate_assistance_resolution_plan` (3128) passes when the failed
> child left no open assistance request (3177 early return).

---

## 3. Two strategies in one run + legacy/dead paths

Every run's phase‑progress events are tagged `strategy:"bounded-supervisor-child-reviewer/v2"`
while the plan‑created event is tagged `strategy:"adaptive-main-child-agents/langgraph-v4"`. These
are **two class name constants for the same executing graph**, not two competing engines:

- `orchestration.py:4482  name = "bounded-supervisor-child-reviewer/v2"` — the legacy bounded
  orchestrator; its name string is what `AgentRuntime._emit_progress` stamps on every child/child‑
  review/supervisor‑review event (a constant), but the legacy single‑stage executor is retired.
- `adaptive_orchestration.py:4150  class AdaptiveSupervisorWorkflow: name =
  "adaptive-main-child-agents/langgraph-v4"` — the **actual** LangGraph state machine that runs
  (admit_initial → dispatch_children → supervisor_review → prepare_next_stage/route_retry → finish,
  built 4186‑4212). This is what every `_run_external_plan` plan/terminal event reports.

Interaction: `_run_external_plan` (external/codex) and `_run_model_plan` (model) are twin drivers.
Both build the child `execute()`/`decide()` closures and hand them to
`AdaptiveSupervisorWorkflow(...).run()` — so the *adaptive* graph rewrites/owns the plan while the
*bounded* name is only a UI/telemetry label on the wrapper's progress emits. There is no second
engine; the dual naming is a cosmetic seam.

**Host overrides the LLM's plan** in three places:
- `_restored_shader_goal_proposal` (adaptive_orchestration.py:7507) replaces the LLM plan with the
  Host‑stored `ShaderGoalMemory` and stamps stageObjective `"Resume the exact Host-stored Shader
  goal"` (a92176d8).
- `admit_initial_decision` (7540) rebuilds shader clarifications/plans from frozen candidates
  (`_host_shader_clarification`, `_canonicalize_initial_shader_plan`), discarding model option sets.
- `_bind_stage_evidence_plan` / `_validate_initial_routing_plan` (runtime.py:15139/15029) re‑bind
  evidence and routing onto the model plan.

**Legacy / dead / duplicated paths:**
- `migratedLegacyPlan` — set only by `parse_external_initial_proposal` (8143‑8144) /
  `normalize_model_initial_proposal` when a **pre‑v2 bare `TaskPlan`** is accepted and wrapped by
  `legacy_proposal` (6100). Docstring (8109): "accepting old wire syntax never restores the retired
  single‑stage execution path." It is diagnostic only — dead compatibility.
- `fallback:true` — deterministic `_safe_fallback_plan_for_routing` when the planner produced nothing
  admissible (14988‑15008); 526e79d3 is such a fallback.
- `specialist*` compatibility shims (runtime.py:24510‑24524): a whole block of read‑only aliases
  `_specialist_* = _child_*` (e.g. `_gather_specialists = _gather_children`,
  `_validate_structured_specialist_output = _validate_structured_child_output`) for pre‑"child"
  embedders; errors.py:161 `SpecialistTerminalReport = ChildTerminalReport`. `_sanitize_public_diagnostic_text`
  (24572‑24591) even rewrites the word "specialist"→"child Agent" in any leaked prose.
- Duplicated per‑strategy emitters in `_run_model_plan`/`_run_model_skill` (11541, 13188, 13335,
  14497‑14734) are **dead for the codex preset** — the identical strings for external runs come from
  `_run_external_plan`/`_run_external` (15806‑16324, 18038‑18209).

---

## 4. Error transparency — where the real reason dies

- **Redaction to one sentence:** `_exception_summary` (runtime.py:24595‑24651) maps the exception
  *type* to a fixed category and **returns only that**, never `str(exc)`:
  `EvidenceValidationError→"子任务结果未通过验证。"`, `PolicyError→"该操作未通过安全检查。"`,
  `BridgeError→"RenderDoc 证据读取失败。"`, else `"AI 任务在本地主机中失败。"`. For zh_CN it returns
  `category_zh` with an explicit comment (24642‑24646) that raw provider/Host diagnostics are kept
  "only on the propagated exception chain."
- **Secondary scrubber:** `_sanitize_public_diagnostic_text` (24527‑24592) strips tracebacks, paths,
  Python reprs, tokens; used for `public_detail`. `safe_policy_feedback` (adaptive_orchestration.py:4881)
  reduces a PolicyError to `code + ": outside the Host authority envelope"` etc.
- **Not persisted on disk anywhere.** The docstring at runtime.py:24531‑24534 is explicit: "The
  original exception remains attached … available to a debugger through `__cause__`/`__context__`.
  Task events, however, are durable user‑facing transcripts: they must not contain … tracebacks …".
  Confirmed: the task‑event jsonl (`*.jsonl`) stores only the redacted `summary`/`activitySummary`
  (see 526e79d3 dump — no raw reason field). The durable terminal delivery
  (`_durable_terminal_delivery` runtime.py:2052) writes only `terminalStatus` + `result.text[:500]`
  (already redacted). run_state (run_state.py:1824‑1874 `prepare_terminal`) stores `terminal_status`
  + the redacted result payload. There is **no** tool‑audit or sqlite column that keeps the raw
  PolicyError/validator message — it lives only in the in‑process exception chain and is GC'd.
- **Not fed back to the LLM on retry.** Two cases:
  - `EvidenceValidationError` (trusted internal validator) → the child *does* get the raw bounded
    detail: `reason = str(exc)[:1000]` fed into the correction `attempt_prompt` (runtime.py:14695,
    18107; "Host validation feedback:\n{reason}"). This is the one place a real reason survives.
  - `PolicyError` → **sanitized only**. The observation conversion (16084‑16103) feeds
    `_sanitize_public_diagnostic_text(exc)` or the fixed string "The proposed operation did not
    satisfy its current contract."; the reviewer prompt gets `retry = self._exception_summary(exc)`
    (16459) = the generic category. This is exactly why the reviewer wrote
    **"上次失败的精确原因无法确认"** — its `external_supervisor_review_prompt(... retry_reason=retry ...)`
    (16405‑16410) receives only the one‑sentence category plus the child sessions' redacted
    `reason = self._exception_summary(exc)[:1000]` (15868/16049/16151/16284). The exact failing field
    is structurally unavailable to the model that is being asked to correct it.

---

## 5. Ceilings / budgets applied to one simple request

| Budget | Value | Source |
|--------|-------|--------|
| `max_supervisor_stages` | default 5, clamp 1‑6 | runtime.py:14837 (`_bounded_option`) |
| `MAX_WORKFLOW_STAGES` (trusted ceiling) | 6 | adaptive_orchestration.py (used 4501/5281) |
| `max_total_tasks` | default 8, clamp 1‑12 | runtime.py:14846 |
| `MAX_WORKFLOW_TASKS` | 12 | adaptive_orchestration.py |
| `MAX_STAGE_TASKS` (tasks per stage) | small (plan field `max_length`) | adaptive_orchestration.py:710/planning.py:349 |
| `MAX_SUPERVISOR_DECISIONS` | **12** | adaptive_orchestration.py:1689 |
| planner `supervisor_iterations` | `min(2, role.max_inner_iterations)` | runtime.py:14878 |
| reviewer `reviewer_iterations` | `min(2, role.max_inner_iterations)` | runtime.py:16399 |
| supervisor‑review `decision_attempt_limit` | `min(2, remaining_decisions)` — **one** replan | adaptive_orchestration.py:5011 |
| child `max_attempts` (`_child_attempt_limit`) | provider/shader dependent (4 in these runs → `progressTotal:4`) | runtime.py:15526 |
| `max_total_attempts` | `min(8, max_attempts)` | runtime.py:15539 |
| `local_attempt_limit` | `max_total_attempts // selection_round_limit` | runtime.py:15579 |
| stage timeout | `_stage_timeout(profile,"child"/"planner")` | runtime.py:15573/16372 |
| LangGraph recursion | `max(25, max_stages*4+8)` | adaptive_orchestration.py:4297 |

**Budgets that make a single yield/correction loop impossible:**
- The child‑review advertises `progressTotal:4` ("Host requested evidence correction 1/4") but a
  *terminal* `PolicyError` on the very next `_run_external` setup (gate 14) short‑circuits the loop
  on iteration 2 — the 4‑step correction budget can never be spent (a92176d8, 57c9). The
  correction/reroute loop presupposes a *retryable* `EvidenceValidationError`, but the actual blocker
  is a *terminal* authority `PolicyError`, so the loop count is decorative.
- `decision_attempt_limit = min(2, …)` gives the reviewer exactly **one** replan; two `admit_dispatch`
  rejections ⇒ `BlockedDecision` (a92176d8, 57c9). A supervisor that needs to discover *which* of
  ~8 authority/route fields is wrong, while receiving only "该操作未通过安全检查", cannot converge in
  one attempt.
- An **initial** `ask_user` is structurally non‑resumable: `not report.stages` (5728) + `from_report`
  raise (1800) mean the question can only be published if `_finish`/`clarification_result` returns
  `needs-user-input` directly; any fall‑through to `BlockedDecision` collapses to
  `partial_result` (5685) with the question discarded — a 0‑budget path to a silent partial
  (9e4888, 719fdf6a).
- 526e79d3 shows the ceiling churning uselessly: 4 attempts consumed in ~20 ms, all deterministic
  pre‑model setup raises, model never invoked.

---

## Bottom line

The failures are not model failures — they are **deterministic Host admission gates** (authority
effect‑ceiling mapping in `validate_plan_authority`/`_requested_workflow_effects`, the terminal
`PolicyError` classification in `_terminal_policy_violation`, `admit_dispatch`, and the stage‑less
clarification checkpoint bar) firing on shader/decompile requests, combined with a redaction layer
(`_exception_summary`, `safe_policy_feedback`, `_sanitize_public_diagnostic_text`) that removes the
one piece of information (the exact failing field/effect) that the reviewer/child LLM and the user
would need to fix it. The raw reason exists only on the in‑process exception chain and is written
nowhere durable.
