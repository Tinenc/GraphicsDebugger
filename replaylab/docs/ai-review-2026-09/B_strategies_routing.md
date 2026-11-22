# Audit B — Strategies & Route-Decision Components in ReplayLab AgentHost

> Historical audit only. The strategies and routing runtime named below were deleted; use `../../ARCHITECTURE.md` for the current implementation.

Scope: `/home/claude/work/repo/replaylab/src/replaylab_agent/` — orchestration.py,
adaptive_orchestration.py, planning.py, shader_routing.py, event_routing.py,
mention_routing.py, intent_contract.py, child_coordination.py,
specialist_coordination.py, skill_selection.py, and their `runtime.py` call sites.
Design intent: `docs/AI_ORCHESTRATION_REFERENCE_AUDIT.md`.

## TL;DR (matches the user's complaint)

The user says: *"host 一些旧的逻辑没有清理掉，他貌似会一直决定路线，导致任务失败"* — the Host
still keeps deciding the route and the layers conflict. This is accurate and
provable. The design doc says (lines 50, 88-89):

> "Host tool admission and policy ... Must not own ... Shader/Event route ranking,
> compiler/provider choice, a fixed phase prefix, or a prescribed tool sequence."
> "There is no technical route-admission checkpoint; an optional route-plan field
> is diagnostic/advisory telemetry only."

The code contradicts this in three load-bearing places:

1. **Two orchestration strategies run at once.** `adaptive-main-child-agents/langgraph-v4`
   (`AdaptiveSupervisorWorkflow`, adaptive_orchestration.py:4147) drives planning +
   supervisor decisions, while `bounded-supervisor-child-reviewer/v2`
   (`BoundedSupervisorChildReviewer`, orchestration.py:4472) is still the object that
   supplies child descriptors, parallelism, and review/synthesis. Both names appear
   in every failed log (planning events tagged v4, running/child/review events tagged
   v2).
2. **`shader_routing.py` is a hard admission gate, not telemetry.** `select_shader_route`
   (shader_routing.py:~2990) produces `status="blocked"` `ShaderRouteDecision`s and the
   offer builder propagates that block (shader_routing.py:3269); the child descriptor is
   *derived from* the route's acceptance criteria (orchestration.py:3338-3364), so the
   route mode literally decides what the child must prove.
3. **The Host rebuilds and re-injects the exact failing route on resume.**
   `_restored_shader_goal_proposal` (adaptive_orchestration.py:7507) rebuilds a plan with
   `shader_route=goal.shader_route` from a stored `ShaderGoalMemory`, discarding the
   supervisor's normalized goal and replacing the plan summary with the raw prompt +
   `"Clarification response: 我的选择：PS · MainPS"` (runtime.py:662).

The observable failure loop: supervisor emits `shader_route.mode` = `compile-only`
(8 outcomes) → route gate admits it and the child descriptor inherits compile/ABI
acceptance criteria → read-only child cannot prove them and **yields "awaiting route
confirmation"** → review rejects (PolicyError → "该操作未通过安全检查") → reviewer
re-dispatches with `readable-reconstruction` + à-la-carte outcomes that no single
`_RouteSpec` matches → gate returns `no-goal-compatible-shader-capability` →
supervisor decision budget exhausts → `terminalStatus: partial`.

---

## 1. Inventory of every planner / strategy / route-decision component

Each row: where invoked from `runtime.py`, its input, whether it can OVERRIDE the
LLM plan, and whether it is still needed.

| # | Component | File:line | Invoked from runtime.py | Input | Overrides LLM plan? | Still needed? |
|---|---|---|---|---|---|---|
| A | LLM supervisor (`pydantic_ai.Agent`) | runtime.py:12339 | 12339-12374 (initial), plus review DecisionMaker | provider prompt | — it *is* the model plan | Yes (the only intended planner) |
| B | `normalize_model_initial_proposal` | adaptive_orchestration.py:8151 | runtime.py:12375 | model output + catalog + request + context | **Yes** — rewrites/repairs the model proposal, sets `migrated_legacy_plan` | Partly (schema coercion); the rewriting is scope creep |
| C | `admit_initial_decision` | adaptive_orchestration.py:7540 | runtime.py:12438 | decision + request + context + matrix + `remembered_shader_goal` | **Yes, heavily** — replaces the model decision with Host-built proposals (see §4) | This is the main offender |
| D | `safe_fallback_plan` / `_legacy_safe_fallback_plan` | planning.py:977 / 736 | runtime.py:3222, 3503, 12411, 14991 | request text, catalog | **Yes** — builds a full plan incl. `shader_route`/`event_route` when the model fails or is "mismatched" | `safe_fallback_plan` yes; `_legacy_safe_fallback_plan` is dead duplicate |
| E | `AdaptiveSupervisorWorkflow` (v4) | adaptive_orchestration.py:4147 | runtime.py:11279, 12521, 13511 | initial decision, execute/decide closures | Runs the graph; its `_prepare_route_retry` node re-drives review with `host_routing_feedback` | Yes, but duplicates F |
| F | `BoundedSupervisorChildReviewer` (v2) | orchestration.py:4472 | `self.orchestration_strategy`, runtime.py:1673-1674, 5508, 5971, 10497 | plan + results | `review_and_synthesize` rejects/relabels child results; `child_descriptor` picks fixed roles | Redundant with E; the two split one job |
| G | Host-stored Shader goal resume (`_restored_shader_goal_proposal` + `ShaderGoalMemory`) | adaptive_orchestration.py:7507; runtime.py:8500 `_admitted_shader_goal` | runtime.py:12572, 15149 (checkpoint) → 12446 (resume) | stored objective/route/effects | **Yes** — synthesizes the whole proposal from memory incl. the old `shader_route` | This is the "keeps deciding the route" bug |
| H | `intent_contract` inference (`infer_task_intent_contract`, `_shader_intent`) | intent_contract.py:1399, 753 | planning.py:1003 (inside D) | request text | Feeds D's fallback route; a **second** classifier of the same intent | Duplicates I; consolidate |
| I | `shader_routing` route planner (`offer_shader_routes` / `select_shader_route` / `infer_shader_route_request`) | shader_routing.py:3245 / ~2990 / 2610 | runtime.py:7243 `_offer_shader_goal_with_safe_fallback`, called 12848/12876 in dispatch | `ShaderRouteRequest` (mode+outcomes) + effects + tools | Gate can **block** the whole stage; derives acceptance criteria | The gate should be deleted per doc |
| J | `event_routing` route planner (`select_event_route`) | event_routing.py:873 | runtime.py:12831 (dispatch) | `EventRouteRequest` + intent + effects + tools | Emits `EventRouteDecision` incl. `artifact_obligations` the child must satisfy | Same class of gate as I |
| K | `mention_routing` (`RoutingAuthority`, `resolve_registry_mentions`) | mention_routing.py:60, 790 | runtime.py registry-routing (12331, 12390) | `@agent/@skill/@mcp` + registry | Can force clarification / bind skill/agent/mcp | Legitimate but overlaps skill_selection |
| L | `skill_selection` (`create_skill_offer` / `admit_skill_selection`) | skill_selection.py:188, 383 | runtime.py:12967 `_create_child_skill_offer`, 12976 | skill catalog + candidates | Child selects; Host admits | Legitimate |

### Actual precedence order (initial turn)

```
current_request = _current_conversation_request(prompt)        # runtime.py:642,662
        (raw prompt + "Clarification response:\n" + reply)      # supervisor goal NOT used here
   │
   ├─ workflow_resume?  → workflow_resume.initial                (runtime.py:12310)
   ├─ bare "why?"       → InitialDirectAnswerDecision            (runtime.py:12312-12330)
   ├─ @mention needs clarify → _registry_mention_clarification   (runtime.py:12331, planner_fallback=True)
   └─ else LLM supervisor.run()                                  (runtime.py:12350)
           → normalize_model_initial_proposal (B, rewrite)       (runtime.py:12375)
           → on any error: safe_fallback_plan (D) or fallback_clarification (planner_fallback=True)
   │
   ▼
admit_initial_decision (C)   ← THE OVERRIDE POINT                (runtime.py:12438)
   ├─ _restored_shader_goal_proposal (G) if remembered goal+resume (adaptive:7729)
   ├─ _canonicalize_initial_shader_plan(ensure_shader_task=True)  (adaptive:7592)
   ├─ safe_fallback_plan resource-export swap                     (adaptive:7624-7641)
   ├─ _host_read_only_fallback_plan / legacy_proposal             (adaptive:7698,7754,7771,7785)
   └─ _host_unavailable_target_decision                           (adaptive:7699,7755,7802)
   │
   ▼
_admitted_shader_goal → checkpoint ShaderGoalMemory (G is stored) (runtime.py:12572)
   │
   ▼
AdaptiveSupervisorWorkflow(E).run(initial)                        (runtime.py:12521/graph)
   dispatch_children → per-shader-task: offer_shader_routes(I)    (runtime.py:12876)
                       per-event-task:  select_event_route(J)     (runtime.py:12831)
                       child_descriptor from strategy(F)/route(I) (orchestration:4487,3331)
   supervisor_review → DecisionMaker (LLM) → _route_after_review
                       {next | retry(_prepare_route_retry) | finish}
```

So a single user turn passes the intent through **five** separate deciders that can
each rewrite the route (B, C, D/H+I, G, and the dispatch-time gate I/J), and the
model's own normalized goal is dropped at the very first step.

---

## 2. `shader_routing.py` — the route-admission gate (contradicts the doc)

### What it validates on `shader_route`

- **`mode` enum** — `ShaderOperationMode` (shader_routing.py:98-101 etc.):
  `readable-reconstruction`, `organized-readable-reconstruction`, `compile-only`,
  `organized-compile-only`, `compile-replace`, `cbuffer-edit`, `alias-review`,
  `alias-apply`, `project-reuse`, `source-watch`, `full-lifecycle`, `quick-analysis`.
- **`required_outcomes` per mode** — `_MODE_OUTCOMES` (shader_routing.py:1886-1911)
  maps each mode to a frozenset. Request outcomes are **added on top**:
  `required = set(_MODE_OUTCOMES[request.mode]); required.update(request.required_outcomes)`
  (shader_routing.py:3031-3033).
- **`replacementScope`** — must equal the spec's scope or `replacement-scope-mismatch`
  (shader_routing.py:3057-3060); also validated in `ShaderRouteRequest._validate_request`
  (shader_routing.py:216-231).
- **`selectionBasis`** — free-text, non-validated.
- **effects** — `_MODE_REQUIRED_EFFECTS` (shader_routing.py:1918-1929) +
  `shader_goal_required_effects` (1932-1967) compared against the spec's effects.

### It IS a hard admission gate

The per-route loop (shader_routing.py:3037-3122) computes blocker codes:

```python
missing_outcomes = required_outcomes - spec.outcomes
if missing_outcomes:
    blockers.append("required-outcome-not-covered")
if (spec.outcomes - required_outcomes).intersection(_GOAL_EXPANDING_OUTCOMES):
    blockers.append("route-adds-unrequested-outcome")
...
if not spec.required_effects.issubset(effect_set):
    blockers.append("required-effect-not-authorized")
if not goal_effect_set.issubset(spec.required_effects):
    blockers.append("requested-effect-outcome-not-covered")
if not spec.required_effects.issubset(goal_effect_set):
    blockers.append("route-adds-unrequested-effect")
```

If `not compatible_specs:` the function returns a **`status="blocked"`**
`ShaderRouteDecision` with `blocker_codes=("no-goal-compatible-shader-capability",)`
(shader_routing.py:3124-3156). `offer_shader_routes` propagates that as a blocked
offer (shader_routing.py:3269-3284), and `shader_child_descriptor_for_route` then
raises: `"a blocked Shader mission cannot create a child contract"`
(orchestration.py:3336-3337). `_admit_shader_route_from_output` likewise raises
`EvidenceValidationError("blocked Shader goal envelope cannot admit a child result")`
(runtime.py:6584-6592).

The block is a **whole-stage failure**, not advisory telemetry — directly
contradicting doc lines 88-89 and 50. Note the self-contradiction: the same file
carries comments insisting "Route hints are telemetry only" (shader_routing.py:3375)
and "candidate tool gaps are replan signals" (3158-3162), yet the offer-level
`blocked` status and the derived acceptance criteria are authoritative.

### The `readable-reconstruction` + `compiled-candidate`/`replay-abi-validation` case

Outcome sets (shader_routing.py:1117-1142):

```
_READABLE_OUTCOMES = {capture-evidence, algorithm-analysis, route-provenance, readable-representation}
_COMPILE_OUTCOMES  = _READABLE_OUTCOMES ∪ {executable-representation, compiled-candidate, replay-abi-validation}
```

For `mode="readable-reconstruction"` (`_MODE_OUTCOMES` = `_READABLE_OUTCOMES`) with
`required_outcomes = [algorithm-analysis, compiled-candidate, replay-abi-validation]`:

- Expanded `required_outcomes` = `_READABLE_OUTCOMES ∪ {compiled-candidate, replay-abi-validation}`
  (note: `executable-representation` is **absent**).
- The **readable** spec: `missing = {compiled-candidate, replay-abi-validation}` →
  `required-outcome-not-covered` → rejected.
- The **compile-only** spec (`outcomes=_COMPILE_OUTCOMES`): covers everything, but
  `spec.outcomes - required = {executable-representation}` which is in
  `_GOAL_EXPANDING_OUTCOMES` (shader_routing.py:1150-1164) →
  `route-adds-unrequested-outcome` → rejected.
- **Every** other spec fails the same way.

Result: `no-goal-compatible-shader-capability`, whole stage blocked. This is not a
capability shortage — it is a **self-inconsistent outcome list that the exact-set
matcher can never satisfy.** Because a model authors `required_outcomes` à la carte
(as the reviewer does in log a92176, entry [40]/[52]), it routinely produces sets that
match no `_RouteSpec` exactly. `algorithm-analysis` itself is harmless (it is in every
readable set); the fatal element is any goal-expanding outcome that doesn't line up
with a single spec's outcome frozenset.

### Rejected combinations (summary)

Rejected whenever the expanded outcome set is not exactly `⊆` one spec's outcomes AND
that spec's extras are all non-goal-expanding:
- readable mode + any of {compiled-candidate, replay-abi-validation, executable-representation, replay-replacement, organized-source(*), …}
- compile mode without `executable-representation` when a compile spec would otherwise match (adds it as "unrequested")
- any mode whose `replacementScope` ≠ the spec scope
- any outcome whose required effect is outside `authorized_effects`

(*) `organized-source` is goal-expanding too (shader_routing.py:1152), so mixing it
into a non-organized mode also blocks.

### Does a rejected route fail the whole stage?

Yes. Blocked offer → descriptor construction raises PolicyError/EvidenceValidationError
→ child fails → `_route_after_review` → supervisor eventually returns
`BlockedDecision(... "supervisor-decision-budget-exhausted")` and the run terminates
`partial` (the exact terminal text in every failing summary). The doc's promise
"Optional evidence failure produces useful partial output" (line 469) is technically
met, but the *cause* is a Host route gate the doc says must not exist.

---

## 3. `intent_contract.py` — `replaylab-task-intent/v1` and double specification

`TaskIntentContract` (intent_contract.py:163-183): `schema_version` fixed to
`replaylab-task-intent/v1`, an `intent` (discriminated union on `kind`), and
`deliveries: tuple[DeliveryObligation, ...]`.

- **`intent.operation`** for shader is `ShaderOperation` (intent_contract.py:24-25):
  `readable-reconstruction`, `validated-reconstruction`, plus explain/diagnose/replace/
  cbuffer-edit/alias-*/project-reuse/source-watch/full-lifecycle.
- **`deliveries[].kind`** is `DeliveryKind` (intent_contract.py:58-65): `analysis`,
  `readable-source`, `validated-shader-candidate`, etc.

Strictness: `_shader_intent` (intent_contract.py:753-1207) is a keyword classifier that
maps request text to `operation` and then to `delivery_kinds`
(intent_contract.py:1162-1205). Critically, **"反编译"/decompile is escalated to
`validated-reconstruction`** in one branch (1130/1137/1145) — this is why the very
first supervisor plan in log a92176 emitted `intent.operation:"validated-reconstruction"`
with `deliveries:[readable-source, validated-shader-candidate, failure-diagnosis]`,
even though the SKILL.md says decompile is *readable-reconstruction by default*
(SKILL.md:36-45).

### Double specification of the same thing

`shader_route_request_from_intent` (shader_routing.py:2568-2609) is an explicit adapter
that maps intent operation → shader mode:

```python
mode_by_operation = {
  "explain": "quick-analysis", "diagnose": "quick-analysis",
  "readable-reconstruction": "readable-reconstruction",
  "validated-reconstruction": "compile-only",   # <-- decompile → compile+ABI
  "replace": "compile-replace", ... }
```

So the same user intent is encoded **twice** in every plan task
(`PlannedTask.intent_contract` and `PlannedTask.shader_route`, planning.py:137 + the
per-task `shader_route` field). `intent.operation="validated-reconstruction"` and
`shader_route.mode="compile-only"` are redundant restatements, and
`deliveries=[readable-source, validated-shader-candidate]` restates
`required_outcomes=[readable-representation, compiled-candidate, replay-abi-validation]`.
When the reviewer later downgrades to `readable-reconstruction`, it must keep BOTH the
`intent.operation` and the `shader_route.mode` consistent by hand — and in the logs it
does not always (attempt-1 intent op = validated-reconstruction with a compile-only
route; the correction flips route to readable but the outcome list still names
compile outcomes in the supervisor's re-dispatch, log a92176 [40]). This is precisely
the "mutual configuration conflict" the user reports.

---

## 4. The "Host-stored Shader goal" production & resume (why the goal is discarded)

### Production

After the initial proposal is admitted, `_admitted_shader_goal(proposal)`
(runtime.py:8500-8524) extracts the single shader task and stores a `ShaderGoalMemory`
that **includes the route**:

```python
return ShaderGoalMemory.create(
    objective=proposal.goal.objective,
    success_criteria=proposal.goal.success_criteria,
    task_summary=task.summary,
    effects=task.effects,
    shader_route=task.shader_route,        # <-- the route is memorized
    custom_agent_id=task.custom_agent_id,
    skill_candidates=task.skill_candidates,
)
```

It is checkpointed at runtime.py:12572-12573 (and 15149 for the external-planner path).

### Resume

On a terse follow-up (`帮我修正`, `继续`, or a clarification reply),
`_host_followup_state` (runtime.py:700-784) resolves the prior failed/partial outcome
and `_resumable_shader_goal` (runtime.py:820-830) returns the stored `ShaderGoalMemory`
only when `_is_shader_goal_resume_request` is true. `admit_initial_decision` then calls
`_restored_shader_goal_proposal` (adaptive_orchestration.py:7507-7537):

```python
plan = TaskPlan(request_summary=goal.objective, tasks=[PlannedTask(
    id="task-1", summary=goal.task_summary, skill="renderdoc-shader-workbench",
    ... shader_route=goal.shader_route)])          # re-injects the SAME failing route
...
return InitialSupervisorProposal(
    goal=GoalSpec(objective=goal.objective, success_criteria=list(goal.success_criteria)),
    stage_objective="Resume the exact Host-stored Shader goal",  # <-- verbatim log string
    plan=plan)
```

This is invoked at adaptive_orchestration.py:7729-7734, ahead of any use of the LLM's
fresh decision.

### Why the plan summary becomes raw prompt + "Clarification response: …"

`_current_conversation_request` (runtime.py:642-662):

```python
origin = str(getattr(response, "origin_request", "") or "")
if not origin or origin == current:
    return current
return origin + "\n\nClarification response:\n" + current
```

This concatenation is what becomes `current_request`, and it flows straight into
`goal`/`task.summary` in log a92176 entry [8]:
`goal="反编译这个shader\n\nClarification response:\n我的选择：PS · MainPS"`. The
supervisor's *normalized* goal (produced by the zero-tool planner, log entry [3]/[11])
is **discarded**: `admit_initial_decision` rebuilds the proposal from
`fallback_goal(request, context)` (adaptive_orchestration.py:7599, 7637) or from
`ShaderGoalMemory` (7729), neither of which reads the model's normalized objective. So
the normalization stage runs, emits telemetry, and its result is thrown away — pure
waste and a source of the mismatched goal text.

---

## 5. Legacy / duplicated / dead code (with LOC estimates)

| Item | File:line | Nature | Est. LOC | Verdict |
|---|---|---|---|---|
| **Two strategy classes** | orchestration.py:4472 (v2) + adaptive_orchestration.py:4147 (v4) | Both do supervisor+review; v2 is default `self.orchestration_strategy` but v4 runs the graph | ~150 (v2) + core of ~600 (v4 class) | Collapse to one; v2's `child_descriptor`/`review_and_synthesize` should move into the graph |
| **`specialist_coordination.py`** | whole file (68 lines) | Pure alias shim re-exporting `child_coordination` names as `Specialist*` | 68 | Dead compat; delete once wire readers migrated |
| **`Specialist*` aliases** | orchestration.py:4581-4603 (23 aliases) + 4641-4653 in `__all__` | `SpecialistInput=ChildInput`, `shader_specialist_descriptor_for_route`, `BoundedSupervisorSpecialistReviewer`, etc. | ~45 | Doc line 25-28 says `specialist*` are "not runtime identities"; these are dead |
| **`migrate_legacy_plan` / `migratedLegacyPlan`** | runtime.py:12375, 12602, 14946, 15181; `normalize_model_initial_proposal`/`parse_external_initial_proposal` | Legacy plan migration always reports `false` in every log | ~60 | Verify still exercised; likely dead flag |
| **`_legacy_safe_fallback_plan`** | planning.py:736-975 | Duplicate of `safe_fallback_plan` (977-1126); both build `shader_route`+`event_route` fallbacks | ~240 | One is dead — consolidate |
| **`_legacy_infer_workflow_effects`** | planning.py:1128-1690 | 560-line keyword classifier, "Conservative fallback when a structured planner is unavailable" | ~560 | Only reached when structured planner missing; mostly dead in the codex preset |
| **v1..v4 registry migrations** | extension_registry.py:43-47, 735-842, 1243, 1323-1333 | `LEGACY_ORCHESTRATION_SCHEMA[_V2..V4]`, `_migrate_retired_first_party_policy` | ~400 | Migration-only; keep parser, but it is a large surface |
| **`skillCapabilities` handling** | extension_registry.py:2640-2645, 2698, 2795; parser at 735-842 | v3/v4 records "strictly parsed then discarded" (doc line 79) | ~120 | Correctly discarded, but still parsed/tombstoned every load |
| **`routeOrder` / `shader-recovery-policy`** | learning_candidates.py:114-119, 353-375, 882-898, 1023-1045, 1131-1156, 1544; learning_scheduler.py:134, 2878 | Retired global route-order policy; doc lines 262-269 say "quarantined from runtime scheduling" | ~300 | Retained in SQLite "for a no-loss v1" — migration/audit only, but still materialized |
| **`requiredInitialSkillId` / `required_initial_skill_id`** | runtime.py:4711-4812, 4982, 5017, 5102, 5297, 12974, 15556; planning.py:450 | Forces a first skill; overlaps `@skill` mention binding | ~120 | Overlaps mention_routing; the "fixed first call" the doc forbids (line 85) |
| **"fixed phase prefix" residue** | adaptive_orchestration.py:3393 comment ("not require a fixed phase prefix"); EVENT_WORKFLOW_PHASE_ORDER (orchestration.py:4630) | Event workflow still has phase ordering | ~80 | Doc line 50 forbids "fixed phase prefix"; Event side still orders phases |
| **`event_routing` `artifact_obligations`** | event_routing.py:87-266, 325-352 | Event analogue of the shader gate; child must satisfy Host-authored obligations | ~1094 (file) | Same over-reach as shader gate |
| **`shader_routing` route specs & gate** | shader_routing.py whole (3502 lines) | `_ROUTES`, `_RouteSpec`, `_MODE_OUTCOMES`, tool groups, acceptance criteria | ~3500 | Per doc, the entire admission/ranking half should be advisory or deleted |

Rough total of clearly legacy/duplicated/retired surface: **~2,300+ LOC** of dead or
migration-only code, plus the ~3,500-line shader gate and ~1,100-line event gate whose
*authoritative* behavior the doc says should not exist.

---

## 6. Responsibility split: implemented vs documented

Documented layering (doc table, lines 44-52): Main owns intent/ledger/synthesis;
Child owns local plan→act→observe→validate→replan and *safe tool choice*; Custom Agent
template owns standards; Skill owns process knowledge; **Host owns only tool admission,
effects, budgets, identity, receipts — NOT route ranking, compiler/provider choice, a
fixed phase prefix, or a prescribed sequence.**

Implemented reality — places where two (or more) layers decide the same thing:

| Decision | Layer 1 (should own) | Layer 2+ (also decides) | Evidence |
|---|---|---|---|
| **Route / operation mode** | Child (SKILL.md:12-26, 84-95: "Host must not select a compiler, decompiler, provider, MCP route, recovery candidate, Skill order") | Supervisor emits `shader_route.mode`; `admit_initial_decision` rewrites it; `_restored_shader_goal_proposal` re-injects it; `select_shader_route` gate admits/blocks it | adaptive:7524,7592; shader_routing:3031-3156 |
| **Acceptance criteria** | Custom Agent template (doc line 48) | Derived from the route mode/outcomes by the Host | orchestration.py:3338-3364 (`shader_child_descriptor_for_route` copies `decision.acceptance_criteria`) |
| **Target / stage** | Child from evidence (SKILL.md:52-68) | Host `admit_initial_decision` picks `decisive_stage`, rebuilds cards from frozen bindings, or returns `_host_unavailable_target_decision` | adaptive:7643-7806 |
| **Skill selection** | Child selects from offer (doc lines 176-183) | `required_initial_skill_id` forces first skill; `mention_routing` binds `@skill`; `intent_contract` domain hint steers it | runtime.py:4711-4812; mention_routing.py:790; planning.py:1003 |
| **Intent classification** | one place | `intent_contract._shader_intent` AND `shader_routing.infer_shader_route_request` AND `planning._legacy_infer_workflow_effects` | intent_contract:753; shader_routing:2610; planning:1128 |
| **Deliverables/outcomes** | Skill outcome contract (SKILL.md:249-271) | `intent_contract.deliveries` AND `shader_route.required_outcomes` (double spec, §3) | intent_contract:1162-1205; shader_routing:3031 |
| **Strategy identity** | one | v2 name in child/review events, v4 name in planning events | runtime.py:10497 vs 12502/12603 |
| **Planning** | Main Agent (LLM) | LLM proposal → `normalize_model_initial_proposal` → `admit_initial_decision` → `safe_fallback_plan`, any of which can replace it | runtime.py:12375-12448 |

Net: the Host layer (admission/policy) has absorbed route ranking, mode selection,
target selection, acceptance-criteria authorship, and skill ordering — exactly the six
"Must not own" items in doc line 50 and the `Rejected` list (doc lines 401, 409-410).
The child's SKILL.md correctly tells it "Do not yield for compiler/decompiler/provider
choice, ordinary tool order, technical recovery, a missing route" (SKILL.md:230-236),
yet the runtime's route gate + derived acceptance criteria force exactly that yield,
which the reviewer then treats as a failure.

---

## Concrete remediation targets (ordered)

1. **Delete the shader route admission gate's authority.** Make
   `select_shader_route`/`offer_shader_routes` return only advisory candidate
   telemetry; never `status="blocked"`; never derive `ChildDescriptor.acceptance_criteria`
   from `decision.acceptance_criteria` (orchestration.py:3338-3364). Acceptance must come
   from the Custom Agent template / Skill outcome contract, not the route mode.
   (shader_routing.py:3124-3198, 3269-3284; orchestration.py:3336-3364;
   runtime.py:6584-6592).
2. **Stop re-injecting the stored route on resume.** `_restored_shader_goal_proposal`
   should restore *target identity + effect ceiling + objective* only, and let the child
   re-plan the route; drop `shader_route=goal.shader_route` (adaptive:7524) and the
   `ShaderGoalMemory.shader_route` field (runtime.py:8521).
3. **Collapse the two strategies.** Keep `AdaptiveSupervisorWorkflow` (v4) as the single
   strategy; fold `BoundedSupervisorChildReviewer.review_and_synthesize` /
   `child_descriptor` into it; remove the dual `strategy` labels
   (runtime.py:10497 vs 12502/12603) so logs/UI show one identity.
4. **Deduplicate intent.** Pick ONE classifier (`intent_contract`) and derive the route
   request from it via `shader_route_request_from_intent`; delete
   `infer_shader_route_request` text-matching and `planning._legacy_infer_workflow_effects`.
   Stop storing both `intent_contract` and `shader_route` on `PlannedTask`.
5. **Stop discarding the normalized goal.** `admit_initial_decision` should reuse the
   supervisor's normalized `GoalSpec` instead of `fallback_goal(request, context)`; keep
   the raw prompt only as provenance, not as the plan summary (runtime.py:642-662,
   adaptive:7599/7637).
6. **Remove dead shims:** `specialist_coordination.py`, the `Specialist*` aliases
   (orchestration.py:4581-4603), `_legacy_safe_fallback_plan` (planning.py:736-975),
   `required_initial_skill_id` (fold into `@skill` mention), and the retired
   `routeOrder`/`shader-recovery-policy` SQLite columns once a migration cut-over lands.

## Appendix — log corroboration

- **a92176** (`反编译这个shader` + clarification `PS·MainPS`): planner emits v4 plan with
  `goal` = raw prompt+clarification, `stageObjective="Resume the exact Host-stored Shader
  goal"` (entry [8]); child runs `inspect_shader` + `get_shader_toolchain_capabilities`,
  selects `organized-compile-only`, **yields** with 6 of 9 shader criteria `not-met`
  (`shader-compiler-container-receipted`, `shader-replay-abi-fresh`, …); review fails
  "该操作未通过安全检查" (PolicyError, runtime.py:24621-24623); reviewer re-dispatches
  `readable-reconstruction` twice ([40],[52]) — terminal `partial`.
- **526e79d3** (`帮我修正`): fallback planner (`plannerAttempts:2, fallback:true`),
  `stageObjective="Inspect the exact current EID 11329"`, skill routed to
  `renderdoc-explain-event`; child "rerouting locally" then fails safety check; blocked;
  `partial`. Shows the terse-imperative path (adaptive:7581-7590) inheriting a Host goal.
- Both logs carry `"strategy":"adaptive-main-child-agents/langgraph-v4"` on planning
  events and `"strategy":"bounded-supervisor-child-reviewer/v2"` on running/child/review
  events — the two-strategy split, on-screen.
