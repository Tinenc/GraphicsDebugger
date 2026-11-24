# Audit C — ReplayLab AgentHost policy & contract layers

> Historical audit only. The WorkflowGuard and Agent contracts below were deleted; current `policy.py` is only an effect manifest.

Scope: `/home/claude/work/repo/replaylab/src/replaylab_agent/` plus `/home/claude/work/repo/.agents/skills/`.
Reference runs: `logs/a92176d8….summary.txt` (yield → "Host requested evidence
correction" → child failed → supervisor blocked → terminal `partial`) and
`logs/57c9176355….summary.txt` (two `route-unavailable` tool failures → yield →
child-review "Validated" → child failed → blocked → `partial`). Both were plain
"反编译这个shader / PS·MainPS" (decompile) requests.

Bottom line up front: the host stacks **four independent validation regimes**
(pre-call policy/effect gate, post-call receipt/target binding, child-output/v2
cross-refs, and a shader-route acceptance-criteria gate) on top of **two more**
non-code regimes (SKILL.md acceptance contracts + workflow-acceptance.md
requirement ledger). A bare "decompile" is silently upgraded to a
`compile-only` mission that *requires* `compiled-candidate` + `replay-abi-fresh`
receipts the read-only child can never produce, the child correctly `yields`
(exactly as the SKILL tells it to), and the host then treats that yield as an
unmet-criteria/evidence-correction loop that terminates in `partial`. The user's
"过多的验证" and "配置冲突" complaints are both accurate and both structural.

---

## 1. Every validation layer

### 1(a) Pre-execution gates on a tool call

All in `policy.py`. Entry point is `WorkflowGuard.check()` (`policy.py:3789`,
~600 LOC) called before dispatch, plus `ToolCallScope` (`policy.py:5871`) and the
route/effect envelope.

| # | Where | Checks | Fatal? | Actionable msg to LLM? |
|---|---|---|---|---|
| 1 | `tool_effects()` / `tool_call_effects()` `policy.py:286,383` | tool's declared effect set (workspace/project/ui/replay) | yes if unknown tool | no — internal |
| 2 | `WorkflowGuard.check()` `policy.py:3789` | requested-effect ceiling vs each tool's effects; effect not in ceiling → raise | fatal (`AuthorizationPolicyError`) | partial — effect name |
| 3 | effect-ceiling recheck at receipt time `_validate_shader_route_receipt` `runtime.py:22110-22124` | `effects_outside_ceiling` → `AuthorizationPolicyError` "exceeded its immutable effect ceiling" | fatal | yes (effect names) |
| 4 | `ToolCallScope.require_complete()` / `exact_target` `runtime.py:20657-20686` | frozen capture/EID/stage/resource/raw/entry must match before the call is trusted | fatal `PolicyError` | no — "conflicts with the frozen Host scope" is non-repairable |
| 5 | target *freezing* — `_DYNAMIC_CALL_ID` + scope pinned per turn `tool_service.py:1388`, `policy.py:5871` | binds one immutable target for the whole child session | n/a | n/a |
| 6 | budgets — continuation tool reservation `runtime.py:18008-18024` | `observed_calls > reserved_tools` → `EvidenceValidationError` "exceeded its Host continuation tool reservation" | fatal | partial |
| 7 | allowlist — `_authorized_tools()` / `_physically_available_tools()` `shader_routing.py:2360-2400` | tool must be in route groups ∩ live inventory ∩ effect ceiling | route "blocked" | telemetry only |
| 8 | mutation-uncertainty freeze `WorkflowGuard._mutation_allowed_while_uncertain` `policy.py:4290` | blocks new effectful calls while a prior mutation is unresolved | fatal, fail-closed | partial |

### 1(b) Post-execution gates on a tool result

`InternalToolService._invoke` in `tool_service.py`.

| # | Where | Checks | Fatal? | Actionable? |
|---|---|---|---|---|
| 1 | `_tool_result_error()` + `_bounded_model_tool_diagnostic` `tool_service.py:1571,642` | tool error envelope → typed diagnostic (category/phase/retryable/allowedActions) | raises `ToolExecutionDiagnosticError` | **yes** — structured diagnostic reaches child (see §7) |
| 2 | `canonical_source_result_sha256()` `tool_service.py:1578` | computes `result_sha256` over canonical JSON | — | — |
| 3 | `extract_shader_identity_receipt` / `_recovery_receipt` / `_asset_correlation_receipt` `tool_service.py:1584-1610`, `tool_receipts.py:603,746,969` | parse & bind the strict Host receipt schema | `ConfigurationError` | partial |
| 4 | `attach_host_evidence_receipt_to_result` `tool_service.py:1607` | injects reserved `replaylabHostEvidence` {toolCallId, resultSha256} the child must copy verbatim | — | — |
| 5 | `ToolEvidenceRecord(...)` audit row `tool_service.py:1627` | durable {tool_call_id, tool, arguments_sha256, result_sha256, outcome, receipts} | — | — |
| 6 | `inspect_shader` identity binding `tool_service.py:534-547` | event/stage/raw/entry/resource of the *result* vs the frozen target | `ConfigurationError` → codes `inspect-shader-*-mismatch` | partial (generic English) |
| 7 | `prevalidate_read_only_workflow_result` `tool_service.py:1644` | read-only tool must not carry effect claims | fatal | partial |

### 1(c) Gates on the child's final output (`replaylab.child-output/v2`)

Two stacked validators. First **schema/cross-ref** in `ChildOutput._validate_internal_references`
(`orchestration.py:1553-1707`), then **descriptor admission** in
`ChildDescriptor.validate_output` (`orchestration.py:2784`), then the
**shader-specific** `_validate_shader_identity_against_host` (`runtime.py:20573`)
and `_validate_shader_route_receipt` (`runtime.py:22077`).

Schema/cross-ref (all fatal `ValueError`, surfaced as receipt-repair):
- unique evidence/finding/criterion IDs `orchestration.py:1554-1562`
- every referenced evidence ref exists `:1564-1590`
- tool evidence declared in `completed_tools` `:1591-1601`
- completion receipt exactly matches its tool evidence `tool/tool_call_id/result_sha256` `:1607-1625`
- completion receipt must bind a **passed** criterion `:1626-1634`
- `status=="completed"` requires ≥1 finding `:1687`
- `status in {blocked,failed}` requires unknowns `:1704`
- `status=="yielded"` requires v2 contract **and** admitted evidence `:1694-1701`

`validate_output` (fatal `PolicyError`):
- `output.skill == descriptor.skill`, `task_id` match `:2808-2816`
- cited evidence ⊆ `admitted_evidence_refs`; completed_tools ⊆ `admitted_tools` `:2853-2865`
- coverage criterion IDs ⊆ `expected_criteria` else "covers unknown acceptance criteria" `:2882-2887` ← **vocabulary-conflict trap, see §6**
- `completed` → missing/`not-passed` criteria fatal `:2894-2910`
- `child_delivery_completion_blockers` for delivery goals `:2911-2926`

Shader identity binding (`runtime.py:20573`, fatal `EvidenceValidationError`):
- every `inspect_shader` evidence has exact {tool_call_id,result_sha256} audit match `:20609-20638`
- **must cite every completed inspect_shader** `:20645-20650`
- cannot merge two physical identities `:20652-20657`
- `scope.exact_target` vs receipt event/stage/resource/raw/entry `:20660-20686`
- every finding+evidence `target` event/stage/resource/raw/entry/**capture_session_id** vs receipt `:20713-20730`
- `snapshot_id` is **Host-owned**: the model's value is discarded and replaced (`:20585-20588`, `:20706` "Provider-authored snapshot metadata is deliberately excluded"). So snapshot equality is *enforced by substitution*, not by comparing the child's echo.

Shader route acceptance (`runtime.py:22077`, fatal):
- `details["shader_strategy"] == decision.as_model_payload()` `:22102` (re-attached by `_admit_shader_route_from_output`)
- each **passed** criterion needs an exact causal Host receipt whose gate matches (`gate_record_is_valid` `:22260-22320`) else "lacks an exact causal Host receipt" `:22350`
- causal ordering: e.g. `route-provenance` before `readable-representation` before `compiled-candidate` before `replay-abi-validation` `:22368-22420`, `shader_routing.py:2503-2517`
- `completed` → all criteria passed + all required effects have receipts `:22423-22470`
- unknown/back-dated criteria → `shader-criterion-unknown` `:22203`

status enum: `Literal["completed","yielded","blocked","failed"]` `orchestration.py:1527`.
coverage status enum: `Literal["passed","not-met","not-applicable"]` `orchestration.py:355`.

### 1(d) Supervisor / reviewer output

`adaptive_orchestration.py`. The main supervisor emits a discriminated union
`SupervisorDecision = FinishDecision | DispatchNextStageDecision | AskUserDecision
| BlockedDecision` (`:339`), strict `extra="forbid"`. The reviewer that runs
after a child stage must produce one of these. Failure modes:
- `dispatch_next_stage` plan re-validated against target/scope/effects at dispatch (`_dispatch` gate).
- `blocked` → `terminal_status="partial"` via `_finish.partial_result` `:5484-5490`.
- The user-facing text is produced by `fallback_partial_answer` `:10260+`, which maps the *internal* reviewer reason onto a fixed Chinese string. "structured supervisor review" → `主代理未能完成最终检查` `:10271`, "delivery" → `所需输出尚未全部生成或验证` `:10298`. **The concrete cause (evidence-correction rejection / route-unavailable) is discarded here.** That is why both runs end with the same opaque "主代理未能完成最终检查 … 该工具返回值或本地工具配置没有通过校验".

---

## 2. Where the acceptance criteria come from — and the "decompile must prove replay ABI" defect

**Both** code constants and skill text, and they are independently binding.

Code constants: `shader_routing.py:2162-2350`, `_CRITERIA_BY_OUTCOME` maps each
`ShaderRouteOutcome` to a `_CriterionSpec(criterion_id, gate, description, group_ids)`.
E.g.:

```python
"replay-abi-validation": (
    _CriterionSpec(
        "shader-replay-abi-fresh",
        "replay-abi",
        "Fresh replay validation proves the candidate's entry point, interfaces, "
        "resources, CBuffer layout, and container ABI against the capture.",
        ("candidate-abi-validation",),
    ),
),                                              # shader_routing.py:2236-2245
```

`shader-compiler-container-receipted` (`:2228`), `shader-recovery-frontier-bounded`
(`:2196`), `shader-route-hash-bound` (`:2188`), `shader-identity-bound` (`:2164`),
`shader-fidelity-lane-bound` (`:2220`) are all here. `criterion-1/2/3` do **not**
come from here — they are the generic fallback IDs of the base child descriptor
(see §6).

Which criteria are required is driven by the **mode → outcome** table, not by the
user's ask:

```python
_COMPILE_OUTCOMES = _READABLE_OUTCOMES | frozenset(
    {"executable-representation", "compiled-candidate", "replay-abi-validation"}
)                                               # shader_routing.py:1125-1131
_MODE_OUTCOMES = { ...,
    "compile-only": _COMPILE_OUTCOMES, ... }    # shader_routing.py:1886-1889
```

**The defect (question 2, "plain decompile forced to prove replay ABI freshness"):**
`infer_shader_route_request()` classifies a bare decompile into `compile-only`:

```python
decompile_requested = contains("decompile","reverse compile","反编译")  # :2740
...
else:
    # Every other capture-bound Shader task gets a usable deliverable by
    # default: reconstruct, organize, compile and validate the captured ABI.
    mode = "compile-only"
    required_outcomes.append("organized-source")   # shader_routing.py:2949-2956
```

`反编译` sets `decompile_requested=True` but **not** `source_only` (only "只要可读/
不要编译" do that, `:2721-2735`) and **not** `compile_requested` (the regex
`(?<!反)编译` deliberately excludes 反编译, `:2758`). So it falls through to the
`else` → `compile-only` → `_COMPILE_OUTCOMES` → the mission now *requires*
`shader-compiler-container-receipted` and `shader-replay-abi-fresh`. The
supervisor confirms this: in both logs it dispatches `shader_route.requiredOutcomes`
containing `compiled-candidate` and `replay-abi-validation` for a pure decompile.

This is reinforced by the skill text, so even a supervisor that picked
`readable-reconstruction` re-adds the compile lane. `renderdoc-shader-workbench/SKILL.md`
"Intent and default deliverables":

> **Readable reconstruction** is the default … **construct a usable compile
> environment, compile a candidate** when a registered lane exists, and
> **validate its interface/ABI**. Missing original HLSL starts reconstruction
> immediately; it is not a clarification.

and "Result and acceptance contract": "A compile claim requires its compiler
receipt. ABI validation requires an exact capture comparison." So the compile/ABI
obligation exists in **both** the code default *and* the skill default, applied to
**every** shader task that is not an explicit `explain`/`source-only`.

**How "not-met" is treated:** for a `yielded` child, `not-met` criteria are
*tolerated* by `_validate_shader_route_receipt` (the `missing_criteria` fatal check
is gated on `status=="completed"` `runtime.py:22423`). But the workflow never
accepts a `yielded` result as done — it is a handoff that must be re-dispatched
(workflow-acceptance.md: "Both `requires-followup` and `blocked` prevent a
full-completion claim"), and the supervisor's final acceptance gate requires the
latest result to be `ready`/`completed` with every required criterion `passed`
(`_validate_shader_route_receipt` `:22423-22436`; workflow-acceptance.md "Final
acceptance gate"). So a `not-met` outcome is **not** returned as a useful partial —
it forces another dispatch, and because the underlying outcome (replay-abi) can
never be reached read-only, the loop exhausts and terminates `partial`. In effect
`not-met` behaves as failure.

---

## 3. Child status enum and the treatment of "yielded"

Enum: `status: Literal["completed","yielded","blocked","failed"]`
(`orchestration.py:1527`; ChildOutput). Note `partial` is **not** a child status —
it exists only at the *workflow terminal* layer
(`terminal_status: Literal["completed","partial","needs-user-input","blocked"]`,
`adaptive_orchestration.py:1698`). So the child cannot itself return "partial but
useful"; it must claim `completed` (and pass every criterion) or `yielded`/`blocked`/
`failed`.

What the host does with `yielded`:
- Structurally it is meant to be a **non-terminal supervisor handoff**:
  `safe_incomplete = yielded or child_terminal_report` `runtime.py:18003`, and a
  yield may legitimately leave authorized effects unperformed `:18025`. A yielded
  result requires the adaptive main-supervisor wrapper and exactly one typed
  `assistance_request` `runtime.py:22516-22540`; the prompt tells the child to
  "return status=yielded, and provide exactly one typed [assistance request]"
  `runtime.py:23760`.
- **But** the yielded output still runs the full evidence/route validators
  (`runtime.py:23300-23345`), and if any *passed* criterion lacks a receipt, or the
  `shader_strategy`/target checks fail, it is thrown into the evidence-correction
  retry loop (`runtime.py:11498-11545`, phase `child-review`, message "Host
  requested evidence correction"). When the same non-retryable fingerprint recurs
  (`repeated_forbidden`, `runtime.py:11524-11533`) the child is failed
  ("该操作未通过安全检查"), the supervisor then reviews and returns `blocked` →
  terminal `partial`. This is exactly the a92 trace: yield → one correction →
  child failed → supervisor blocked.

The SKILL **instructs the child to yield in the very situations the host then
punishes.** `renderdoc-shader-workbench/SKILL.md` "Yield and terminal boundaries":

> Yield only at a safe boundary when a material user preference, scope change,
> authority expansion, irreversible tradeoff, or genuinely missing user-owned
> fact changes the goal. If exactly one safe action remains inside the current
> authority, take it.

and README: "Missing evidence or an optional tool failure … is not by itself a
terminal blocker." So when the child hits `route-unavailable` (57c9) or reaches the
workspace-confirmation boundary (a92), the skill's own rule set steers it to a
read-only `yielded` checkpoint with `not-met` compile/abi criteria — which is
faithful to the skill but fails the host's `ready`/receipt gate. The child was
doing what it was told; the two contracts disagree.

---

## 4. Evidence receipts — `tool_call_id` / `result_sha256`, and the id-format hypothesis

Verification path: `_validate_shader_identity_against_host` builds
`records_by_identity` keyed on `(tool_call_id, result_sha256.casefold())`
(`runtime.py:20596-20604`) and requires each cited `inspect_shader` evidence to hit
exactly one completed audit row with a `ShaderIdentityReceipt` (`:20618-20638`).
`_validate_shader_route_receipt.exact_record_index` does the same for every gate
(`runtime.py:22224-22246`). On mismatch → `EvidenceValidationError` "has no exact
Host identity receipt" / "lacks an exact causal Host receipt", routed into the
correction loop; recurrence → fail.

**The `exec-…` vs host-id hypothesis is NOT the cause — by construction the host
adopts Codex's id.** The Codex app-server passes its `callId` straight through:

```python
call_id = params["callId"]                      # codex_app_server.py:1384
...
value = await self.dynamic_tool_handler(tool, arguments, call_id)   # :1419
```

and the tool service uses that supplied id verbatim as the audit `tool_call_id`:

```python
supplied_call_id = _DYNAMIC_CALL_ID.get().strip()
tool_call_id = supplied_call_id[:256]
if not tool_call_id or <control chars>:
    tool_call_id = "internal-" + uuid.uuid4().hex   # tool_service.py:1388-1394
```

`call_dynamic_tool` sets `_DYNAMIC_CALL_ID` to Codex's `call_id`
(`tool_service.py:2653`). So the audit row's `tool_call_id`, the
`replaylabHostEvidence.toolCallId` echoed back, and the value the child copies are
all the same `exec-…` string. The logs confirm the match: the child cites
`"tool_call_id":"exec-58ff79c1-…","result_sha256":"ee3b76…"` and `inspect_shader`
completed successfully — so the a92 failure was **not** a receipt-id mismatch. An
id-format mismatch could only arise if Codex sent control characters or an
>256-char id (host then swaps in `internal-<uuid>` and the child's copied `exec-…`
would no longer match) — a real but narrow edge, not what these runs hit. The
"evidence correction" in a92 was driven by the acceptance-criteria/`shader_strategy`
route gate, not by receipt-id verification.

---

## 5. Skill selection — mechanism and over-build

Mechanism (`skill_selection.py`): the host freezes a `SkillOffer` — "Host-frozen,
canonically ordered, **semantically unranked** Skill catalog" (`:105`) of **1–4**
candidates (`candidates: … max_length=4` `:127`; `SKILL_SELECTION_LIMIT`), each with
`instructions_sha256`. It is content-addressed by `offer_sha256`
(`skill_offer_sha256`, `:182`) and may carry `routing_request_sha256` +
`required_initial_skill_id` for an explicit `@skill` binding (both-or-neither,
`:150-160`). The child returns a `SkillSelection`; the host issues a
`SkillAdmission` (`schema replaylab-child-skill-admission/v3`, `:307`) that
re-checks: offer digest agreement, candidate = child selection, admission number =
host state, replan marker consistency, explicit-binding equality
(`_validate_admission` `:331-357`). `verify_admitted_skill_instructions` re-hashes
the loaded body against `instructions_sha256` (`:475`). `skillReplan` marks a
mid-task switch to another *previously-unattempted* offered skill.

Over-built vs. what it does: the logs show it always resolves to the single obvious
skill on `skillSelectionNumber=1`, `skillReplan=false`, `requiredInitialSkillId=null`,
`routingRequestSha256=null` (a92 line 463; 57c9 line 319). The machinery
maintains, per task: `skillOfferSha256`, `skillAdmissionSha256`, an admission
counter, a canonical-order invariant, an unranked-frontier invariant, a
replan-consistency marker and a full v3 admission schema — to choose one of one
practical candidate. It is a general skill-marketplace admission protocol servicing
a deterministic 1-of-1 pick.

How much SKILL.md is injected: the **entire body plus its required references**,
essentially unbounded for these files. `skills.py` reads the whole file (cap
`_MAX_SKILL_FILE_BYTES` `:313`), takes `body.strip()` as `instructions` with a
128 KiB cap (`_MAX_SKILL_BODY_CHARS` `:352-353`), and **appends every reference
section** (`instructions += "\n\n" + "\n\n".join(reference_sections)` `:414`). So:
- `renderdoc-shader-workbench`: 14,295 chars (SKILL.md, no refs) injected whole.
- `renderdoc-explain-event`: 24,391 (SKILL.md) + 20,307 (references/workflow-acceptance.md)
  + 10,786 (references/visual-evidence.md) ≈ **55.5 KB** injected into the child prompt,
  on top of the descriptor's own `render_prompt_contract` (`orchestration.py:2921+`)
  which re-lists the acceptance criteria a *third* time.

Do the acceptance sections reintroduce a fixed route the README forbids? Yes, in
substance. README is explicit that a skill "must not prescribe a required tool
count or group, compiler/provider choice, fixed phase prefix, first tool, or retry
route." But `renderdoc-shader-workbench/SKILL.md` "Result and acceptance contract"
and "Intent and default deliverables" make reconstruction→organize→compile→ABI a
*mandatory outcome chain* ("construct a usable compile environment, compile a
candidate … validate its interface/ABI"), and `workflow-acceptance.md` encodes a
sticky, ordered requirement ledger ("An `explicit-user` or `evidence-triggered`
required item is sticky across Event stages", "Final acceptance gate"). Combined
with `_OUTCOME_PREREQUISITES` (`shader_routing.py:2503-2517`) and the causal-order
receipt check (`runtime.py:22368-22420`), the "no fixed route" promise is
contradicted: the outcomes are fixed, ordered, and each demands a specific
tool-class receipt (`gate_record_is_valid` maps `compiler-container`→
`compile_shader_workspace`, `replay-abi`→`validate_shader_workspace_candidate`,
`runtime.py:22298-22303`). The route is de-facto fixed even though tool *order*
inside a group is nominally free.

---

## 6. policy.py structure, overlaps, and legacy

6,531 lines, but only **two classes**:
- `WorkflowGuard` `policy.py:3043-5832` (~2,790 LOC). Dominated by two methods:
  `check()` `:3789` (~620 LOC, pre-call effect gate) and `record_result()` `:4411`
  (~1,390 LOC, post-call mutation/receipt bookkeeping). Other methods:
  `bind_event_visual_workspace` `:3151`, `_mutation_binding` `:3329`,
  `_remember_shader_apply` `:3672`, `_forget_shader_rollback` `:3754`,
  `validate_completion` `:5798`, `validate_yield_boundary` `:5817`.
- `ToolCallScope` `policy.py:5871-6531` (~660 LOC).

Everything else (~2,900 LOC, `policy.py:286-3042`) is a flat bank of ~45
module-level strict validators: `_valid_event_flow_artifact_receipt` `:825`,
`_valid_event_texture_evidence_receipt` `:958`, `_strict_shader_binding` `:1185`,
`_strict_prepared_shader_workspace_binding` `:1335`,
`_strict_recovery_candidate_workspace_binding` `:1488`, `_valid_shader_apply_receipt`
`:1746`, `_valid_shader_rollback_receipt` `:2127`, `_valid_material_update_receipt`
`:2834`, `_valid_material_rollback_receipt` `:2947`, etc.

Overlapping / duplicate policies:
- **Target identity is validated in at least four places** with near-identical
  event/stage/resource/raw/entry comparisons: (i) `inspect_shader` result vs frozen
  target in `tool_service.py:534-547`; (ii) `ToolCallScope.exact_target` vs receipt
  in `runtime.py:20660-20686`; (iii) every finding/evidence `target` vs receipt in
  `runtime.py:20713-20730`; (iv) `bound_identity` details vs receipt in
  `runtime.py:20705-20760`. Pixel work repeats the same pattern a fifth time
  (`_validate_pixel_output_against_host` `runtime.py:5xx…` at `runtime.py:19657-19690`).
- **Multiple effect ceilings** compared independently: `WorkflowGuard.check`
  (`policy.py:3789`), `shader_goal_required_effects` (`shader_routing.py:1932`),
  `_MODE_REQUIRED_EFFECTS` (`shader_routing.py:1918`), and the receipt-time recheck
  `effects_outside_ceiling` (`runtime.py:22110`). A `workspace`-only decompile is
  effect-checked at least three times.
- **Rollback receipt validation duplicated** across shader (`_valid_shader_rollback_receipt`
  `:2127`, `_valid_shared_shader_rollback` `:2065`, `_valid_shader_rollback_physical_item`
  `:2006`) and material (`_valid_material_rollback_receipt` `:2947`) with parallel logic.

Legacy:
- `specialist_coordination.py` is an **empty compatibility shim** — "This module
  intentionally contains no implementation; it preserves the former Python names"
  — aliasing every `Specialist*` name to the `child_coordination` `Child*` model.
  The "specialist" vocabulary is dead but still shipped.
- `ChildOutput.contract_version` still accepts `replaylab.specialist-output/v1` and
  `/v2` (`orchestration.py:1518-1523`); `workflow_checkpoints.py:65-66` maps both to
  `replaylab.child-output/v2`.
- `_admit_shader_route_from_output` keeps `preselection_tools` "legacy offer field;
  never an execution boundary" (`runtime.py:22109`), and `_acceptance_criteria`
  keeps `route_group_ids` "for source compatibility with callers that still build
  suggestion telemetry" (`shader_routing.py:2469-2475`).
- `policy.py:3977` rejects "legacy Shader tool IDs".

**The acceptance-vocabulary conflict (root of the "配置冲突" complaint).** There are
two criterion ID vocabularies:
1. Route-derived `shader-*` IDs — `shader_child_descriptor_for_route` sets
   `acceptance_criterion_ids = (criterion.criterion_id …)` from the route decision
   (`orchestration.py:3358-3364`).
2. Generic `criterion-N` IDs — the base `_SHADER_CHILD` descriptor declares 10
   free-text `acceptance_criteria` strings (`orchestration.py:3536-3548`) and
   `criterion_ids()` falls back to `criterion-1…criterion-10` when
   `acceptance_criterion_ids` is empty (`orchestration.py:2651-2657`).

In both logged runs the child emitted coverage rows for **both** families in the
same output — `shader-identity-bound`, `shader-route-hash-bound`, … **and**
`criterion-1/2/3`. That can only happen if the prompt presented both the route
criteria (via the `shader_route`/`workflow_contract`) and the descriptor's generic
"Acceptance criteria" section (via `render_prompt_contract` `orchestration.py:2946-2950`).
The host's own `validate_output` then rejects any coverage ID not in its expected
set ("child result covers unknown acceptance criteria" `orchestration.py:2884-2887`)
— so whichever vocabulary the host is checking against, the *other* half of the
child's coverage is unexpected. The main agent, the child prompt, and the host
validator are keyed to different criterion namespaces.

---

## 7. tool_service.py ~525 — generic "tool-configuration" message and lost reason

The table `_TOOL_FAILURE_SUMMARIES` (`tool_service.py:519-547`) maps stable codes to
fixed English; `"tool-configuration": "The tool result or local configuration could
not be validated."` is `:525`. The comment above it (`:513-518`) states the intent:
"Never copy an exception message into [task events]: bridge/tool diagnostics can
contain capture paths, provider credentials, or an entire rejected result."

Conversion (`_tool_failure_diagnostic` `:560-608`): a `ConfigurationError` from any
tool **other than** `inspect_shader` is collapsed to `code="tool-configuration"`:

```python
elif isinstance(exc, ConfigurationError):
    if name != "inspect_shader":
        code = "tool-configuration"
    else:
        message = _known_failure_message(exc)   # only inspect_shader keeps detail
        ...                                       # tool_service.py:580-604
```

`_known_failure_message` is explicitly "for internal classification; it is never
persisted" (`:552-558`). So the *persisted, user-visible* task event and the final
answer carry only the generic string — which is exactly the terminal text in both
runs ("该工具返回值或本地工具配置没有通过校验").

**But the underlying reason IS preserved for the LLM on a live tool failure** — just
not for the user. When the tool returns an error envelope, `_bounded_model_tool_diagnostic`
(`:642`) admits an allowlisted `{category, phase, retryable, allowedActions}` (e.g.
`route-unavailable` `:284-294`) and raises `ToolExecutionDiagnosticError` carrying it
(`tool_service.py:1553`). That structured diagnostic is:
- written into the durable `tool-completed` event as `diagnosticCategory/Phase/
  Retryable/AllowedActions` after re-validation (`tool_service.py:1689-1712`) — this
  is what the 57c9 log shows: `diagnosticCategory:"route-unavailable"`,
  `diagnosticPhase:"route-plan"`, `diagnosticAllowedActions:[preserve-evidence,
  inspect-native-ir, try-registered-alternate-route, …]`;
- surfaced to the child model via `_child_failure_diagnostic` → `payload["toolDiagnostic"]`
  in the replan clue (`runtime.py:5266-5272`, `7016-7051`).

So the flow is: **the child gets the actionable category/phase/allowedActions; the
user gets the generic sentence.** The failure in 57c9 is therefore not a lost-reason
problem at the model layer — the child correctly saw `route-unavailable`, obeyed the
SKILL's "a missing route … is not a terminal blocker" rule, preserved evidence and
yielded — and the *host* then rejected that yield at the acceptance gate and reduced
the whole thing to the opaque `tool-configuration` terminal string. The reason is
preserved where it cannot help (the LLM, which already yielded) and erased where the
user needs it (the final answer).

---

## Synthesis for the user's complaint

- "过多的验证": a single read-only decompile passes through ~8 pre-call checks,
  ~7 post-call receipt checks, ~15 child-output cross-ref checks, and a shader-route
  acceptance gate with causal ordering — plus two prose contracts (SKILL.md +
  workflow-acceptance.md) re-imposing the same outcomes. Target identity alone is
  checked in 4–5 places.
- "配置冲突": (a) intent classifier upgrades bare "反编译" to `compile-only`, forcing
  `compiled-candidate`/`replay-abi-fresh` the read-only child can't satisfy
  (`shader_routing.py:2949`); (b) the SKILL tells the child to `yield` at exactly the
  boundary the host acceptance gate treats as unmet/failed (`SKILL.md` "Yield and
  terminal boundaries" vs `runtime.py:22423`, `11524`); (c) two criterion vocabularies
  (`shader-*` vs `criterion-N`) reach the child at once and the validator rejects the
  non-matching half (`orchestration.py:2884`, `3358` vs `2651`/`3536`). The result is
  a guaranteed `partial` with the reason discarded (`tool_service.py:525`,
  `adaptive_orchestration.py:10271`).

Minimal high-leverage fixes: (1) make `infer_shader_route_request`/`SKILL.md`
default a *bare* decompile to `readable-reconstruction` (drop compile/abi from the
required set unless the user asks to compile/validate/replace); (2) let the
acceptance gate accept a `yielded` read-only checkpoint as a returnable partial when
its `not-met` criteria are all effectful outcomes outside the requested authority;
(3) unify the two criterion vocabularies so `validate_output`
(`orchestration.py:2884`) and the child prompt share one namespace; (4) surface the
admitted `diagnosticCategory`/`allowedActions` in the user-facing terminal text
instead of the generic `tool-configuration` sentence.
