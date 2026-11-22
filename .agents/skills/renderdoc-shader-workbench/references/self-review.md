# Agent-owned Shader organization review

This review is for requested organization/refactoring, not an obligation to edit
an explain-only Shader. Judge the actual whole requested program, including the
entry and all requested material/lighting branches. A complete file transfer,
successful compilation, smaller line count or more helpers does not demonstrate
that the Agent has inspected or improved the source.
For a new reorganization request, base this assessment on a fresh complete read
of the current program. Reconsider the existing helpers' responsibilities and
their callers before reusing old satisfied decisions. Unchanged good code may
remain unchanged; a fresh assessment does not require rewriting every character.

## What to assess

| Check | Evidence that matters | Unresolved gaps |
| --- | --- | --- |
| `readability` | Trace named inputs/resources through branches to outputs; explain operations and known spaces, units and component meaning. Cite representative functions or paths and cover remaining requested branches. | Anonymous scalar chains with no explained role, comments replacing code organization, or a tidy entry calling an untouched opaque implementation. |
| `unity_style_structure` | Meaningful internal geometry/surface/material/light groups; corresponding channel arithmetic expressed together as vectors; recognized math idioms used where they match the calculation. Cite actual combined calculations and any specific retained scalar forms. | Named but still repetitive XYZ/RGB chains; numbered struct members used as storage bins; blindly adding Unity stages, macros, runtime headers or ShaderLab to a replay Shader. |
| `function_calls` | Directory-like responsibility hierarchy in the actual calls: entry coordinates major operations, each operation owns its sub-operations and concrete algorithms. Trace every requested branch and business-function body, checking each caller's immediate responsibilities and each callee's narrower inputs/results. Shared helpers may have multiple callers; simple computations need no artificial depth. | A main function enumerating every leaf operation despite coherent higher-level groups; merely wrapping a giant body, one-line forwarding wrappers, diagram-only nesting, huge scalar output lists, or unpacking structs back into the original scalar forest. |
| `redundant_temporaries` | Remove aliases/copies that add no meaning; combine corresponding component operations and safe one-use chains; identify specific intermediates retained for meaning, reuse or a concrete semantic constraint. | Cosmetic renames without simplifying use; broad precision claims excusing entire arithmetic regions; aggressive inlining that changes required precision/order or branch/derivative behavior, duplicates samples/side effects, or obscures a meaningful intermediate. |

These are semantic criteria, not thresholds for counts, function length or naming
patterns. Unknown original names do not block readable neutral descriptions of
known operations. Review each remaining anonymous cluster: keep necessary
intermediates with an understandable operation/branch role, simplify needless
copies, and follow unresolved dependencies using available HLSL and baseline IR.
Missing original author meaning does not require recovering names before doing
this work. Preserve the captured entry ABI, resources and layout. Unity
Shader style guides internal organization; the saved source must remain suitable
for its actual captured API and source language.

Use the vector, math-expression and call-hierarchy examples in
[source-operations.md](source-operations.md#express-calculations-and-their-hierarchy)
while editing and during this same overall review. Inspect bodies beyond a tidy
entry and recognizable names: `ProbeDeltaX/Y/Z` can still encode the same redundant
scalar pattern as numbered variables. If related channel arithmetic or a flat
set of leaf helpers remains reasonably simplifiable within the request, record
that scope as `unmet` and repair it before `complete`. Do not add per-expression
tool reviews or compile runs. Keep concrete exceptions in existing evidence or
`retained_intermediates`; an additional report or schema is unnecessary.

Apply [algorithm-design.md](algorithm-design.md) within these same four checks.
Review the directory and function diagram at the same time: concise titles name
the meaningful calculations established from this draw's code and useful
mesh/texture/state context. Verify initial visual/state hypotheses against the
source. Revisit broad groups that hide independently explainable major work and
expose useful intermediate results or distinct contributions where appropriate.
Supporting taps, indices and factors belong to their owner; do not assume that
every coordinate or color transformation is merely a small helper. Apply this
judgment to the directory and the relevant level of the function flow graph.
Merge repeated appearances of one calculation without conflating
distinct branches. Confirm each source anchor still resolves to the described
calculation and major effects are covered; a list of function comments is not this
overview. Look inside coarse entry operations for independently useful calculations;
review the full data flow before choosing granularity. Supply the reviewed
`semantic_overview` with this existing review call, including its independent
analysis evidence and calculation-to-scope ownership from
[semantic-overview.md](semantic-overview.md). A temporary source-derived directory
or old sidecar cannot satisfy this part of a new organization. Do not manufacture
extra stages to reach a count, or split every helper into a stage.
Keep navigation metadata and provenance banners out of the Shader source.
For `function_calls`, cite a representative shared algorithm and its actual
callers, or explain why similar-looking paths have different contracts. For
`redundant_temporaries`, check repeated expensive pure calculations with the
same unchanged inputs as well as aliases; a shared function called twice does
not itself establish result reuse. For `readability`, trace a nontrivial helper
and branch using its comments: purpose, relevant input/result spaces or ranges,
and the reason for non-obvious steps should agree with the implementation.
Check that its opening comment states the purpose in plain language, with a short
algorithm/flow outline when needed. Detailed explanations must be placed at the
corresponding statements or blocks; a long technical header is not a substitute.
Unexplained formulas, stale comments, invented algorithm identities and line-by-line
syntax narration are gaps even when names are descriptive. Use source-specific
evidence, not a comment count, duplication percentage or a new review round.

## Edit, self-review, repair

The acceptance criteria also guide the initial organization. Before editing,
use the complete source and the user's requirements to plan responsibility
boundaries, opportunities for vector/algebra simplification and reuse, and the
comments needed to follow each calculation. Implement that full design before
the overall review; do not perform a rename-only pass and wait for the review
to discover that the actual requested work was never attempted.

Within the existing `readability` evidence, account for naming at both ends of
organization: identify which asset evidence was reused or investigated, which
source names it supports, and how the final consumers clarify or correct CBuffer
roles. A remembered material with unresolved locations does not mean panel names
were applied. Follow the post-organization reconciliation in
[asset-naming.md](asset-naming.md#reconcile-names-after-organization); unresolved
original names may use explicit inferred roles tied to known physical lanes.
If the task requests CBuffer panel organization, the whole-task review also cites
its publication receipt and actual semantic-field counts, including preserved
user fields and skipped rows. Those counts establish write coverage, not useful
names. Review the distinct roles and the actual display scope under
[the naming-quality guidance](resource-naming-delivery.md#names-that-can-be-distinguished-in-the-panel):
short recognizable labels, necessary distinctions and indices, collision handling
and preserved user names. Repeated owner prefixes or a mechanically rewritten
path do not demonstrate a new semantic interpretation. A source-quality review alone cannot satisfy that
display requirement. Repair concrete missed writes without repeating compilation
or rescanning unchanged assets; keep unsupported meanings visibly unknown.

### Completion decisions per computation scope

For broad organization, assess every scope against the following decisions,
including unchanged bodies and alternate branches. Put concise source-specific
outcomes in its existing `evidence`, `retained_intermediates` and `remaining_work`;
these are not new schema fields, tool calls or per-function reports. Related
functions can share evidence when it actually accounts for their differences.

| Decision | What must be established before this scope is satisfied |
| --- | --- |
| Function-body simplification | Inspect remaining expressions, including interleaved channel declarations, internal-axis names and long nested assignments. Combine corresponding channels; remove aliases and output-copy chains; update the same logical value where useful; express dense formulas as named contributions. Inspect reusable subexpressions, common factors and matching intrinsics, justifying any arithmetic reassociation against the actual numerical contract. Name the resulting calculation; if a candidate stays scalar or expanded, identify the specific semantic or readability reason. |
| Responsibility and call design | Reconsider the original boundaries: split mixed responsibilities, combine fragments that belong together, and implement progressively more detailed responsibilities in actual calls, like a directory hierarchy. Trace every branch's callers and callees with their inputs/results; a representative chain can summarize the evidence but cannot replace checks of siblings, fallback paths or existing helpers. Preserve shared algorithms instead of copying them under each parent. A renamed main, flat helper list, diagram-only group or wrapper around unchanged opaque code does not meet this decision. |
| Shared algorithm implementation | Compare related functions and branch variants across this scope and its dependencies. Replace equivalent multi-step copies at their actual call sites with one helper or common core, parameterizing meaningful differences. Identify the shared implementation and callers, or the concrete contract differences that justify separate code. |
| Shared computed results | Trace repeated pure work with the same inputs in the same valid execution scope. Pass an existing result to its consumers when appropriate; repeated calls to a shared helper alone do not establish this. Identify a reused result and consumers, or explain why observed inputs, evaluation conditions or effects differ. |

Where a decision does not apply, briefly explain why from the actual scope (for
example, an unchanged ABI-only declaration group performs no calculations).
Do not fabricate reuse or extra layers for a simple computation. A genuinely
clear existing body may remain unchanged; explain its current design rather than
treating unchanged bytes as either automatic failure or automatic acceptance.

Before `complete`, look for a counterexample to each decision in remaining source,
especially a large unchanged helper, repeated sibling variants, or an entry that
still owns unrelated detailed calculations. Trace the candidate and resolve it.
Check every business function's current plain-language summary, every complicated
function's algorithm/flow outline and its non-obvious internal steps, and the
meaningful alternate/fallback paths. One well-commented helper does not establish
coverage of the rest. Validate that caller-to-callee responsibilities become more
specific where needed, without recursive or empty wrapper layers.
If a feasible requested improvement remains, use `unmet` / `needs_work` and repair
it in the same task. Do not defer it as optional future polish, justify it with
compile success, or mark it satisfied merely because another example was fixed.
A concrete unresolvable limitation means `blocked` with preserved partial work;
it does not turn that scope into a successful exception.

Keep the user's requested readability, comments and behavioral constraints from
the four checks above. Completion means those requirements and all applicable
decisions are met, not that every function changed or that no alternative design
could ever be proposed. Reuse prior assessment for unaffected code and stop once
the requested standard is met.

### Batch repairs in the same working copy

After a coherent full-scope edit seems ready, trace the resulting source with the
four checks above. Keep concrete findings and repairs in working memory. Repair
feasible findings together, then review your changed definitions, downstream uses
and callers. Combine those observations with valid earlier review of unchanged
paths; a new source hash does not mean every unchanged line must be fetched again.
Repeat as needed for the requested quality, not for a fixed number of rounds.
Reuse the asset-naming receipt, partial material semantic context and valid
mappings from the initial preparation. Assess names and comments against both
the observed calculation and that context; unresolved parameter locations do
not require discarding useful material meaning or declaring inferred names exact.
This is part of the existing source review, not another matching/validation pass;
do not repeat reader discovery or asset searches during each source review. If
source edits affect a mapped declaration, check that declaration's name, binding
and uses. Only changed or contradicted mapping evidence calls for targeted repair.

Begin/write receipts include `sourceReviewObservations` tied to the current
source hash: function-body changes, corresponding-channel candidates/naming
families, repeated bodies, long assignments, adjacent comment locations and
actual calls. Use them to look for counterexamples to your completion claim. Inspect
the indicated bodies and consumers, including unchanged ones; count reductions,
renames and a successful save do not demonstrate that the remaining program is
organized. A zero consecutive-channel count does not cover interleaved declarations,
names such as projectedXFromY, struct fields or arbitrary algebra. A missing
adjacent comment can also be an ABI adapter covered by a section comment; inspect
the actual definition and relocate stale/misattached business-function summaries.
The inline examples are a bounded preview. `sourceReviewObservations.catalog`
links one complete JSON file by `relativePath`; read it with
`read_shader_workspace_file(mode=complete)`. Use the initial catalog while
planning the first whole-source edit, then the current catalog after the batch
import for final assessment. It contains all matches of the stated lexical
patterns, even those omitted from the preview, plus full function/call facts.
Its assignment section explicitly selects only eight complexity examples;
neither that selection nor the pattern detectors cover arbitrary algorithms.
Inspect all actual bodies and branches as well. Reuse valid source knowledge;
do not read or compile separately for each candidate.

In the existing `scope_review`, include `observation_dispositions` for **every**
current catalog `candidateId`, mapped to scopes covering its functions. A
candidate still exists, so it cannot be marked resolved. Repair feasible gaps
in the same working copy. If retaining a form, supply its specific `evidence`,
the concrete simpler `alternative` considered, and a `source_anchor` containing
exact current source lines (`start_line`, `end_line`, `text`) for that calculation.
Explain why the alternative changes the actual behavior or makes this code less
clear. A signature, unrelated example or generic precision statement cannot
stand in for the relevant code. For example, distinguish a required ordering
from a same-order vector form that remains possible. For meaningful locals,
identify their distinct result or later consumers instead of defending every
temporary together. A reason for one location does not excuse its neighbors.

`needs_work` keeps the owning scope unmet and means continue repairs. `complete`
requires all catalog entries addressed; an empty catalog uses an empty list but
still requires whole-source review. New source bytes invalidate earlier IDs and
anchors. The Host checks exact source and coverage, not the truth of a semantic
justification; do not treat a syntactically accepted record as proof of quality.

If the existing source already meets the request, assess the whole requested
scope under all four criteria before concluding that no changes are needed.
Keep the source unchanged and explain that conclusion with `no_change_reason`;
do not introduce cosmetic edits merely to get a different hash. This branch
requires the same source-specific evidence and cannot bypass an incomplete
review or leave known feasible gaps hidden behind "already satisfied".

Use the whole-file proposal route for broad changes. Do the review and related
repairs in the working copy, import the current bytes, and record the assessment
for the imported hash. A useful source assessment may record `needs_work`, but it means
continue those repairs in this task. Do not end the task with a feasible to-do list
or repeatedly record the same unresolved result in place of progress.

`complete` here assesses source quality, not compilation or replay effects.
After importing and recording that complete source review, final finish can
save, compile, validate and apply as requested. A `source-review-pending` response
requests continued edits and source review in the same draft. Do not mark gaps
complete to pass it. Intermediate compilation/validation is only for an explicit
user request; `publication_mode="checkpoint"` keeps that session editable.
Plain source saves remain available without a complete review. After a physical
failure, repair in the same working copy, reassess affected source and retry the
required checks. Whole-task `record_task_review` follows actual delivery; it
does not gate the earlier source review. Explain-only and source-only work keep
their narrower effect scope.

## Record the current assessment

Call `record_shader_organization_review` with:

- `workspace_path`, `expected_source_sha256` from the actual current bytes and
  preferably its explicit `organization_id`. An active organization selects its
  proposal; a closed organization selects saved Source. Omitting the ID uses the
  current organization tracked for this workspace in this task's ledger, not
  necessarily saved Source. External MCP use without that task ledger must pass
  the ID. A local file still awaiting import does not establish that the workspace
  has those bytes.
- `checks`: non-empty evidence text for each of `readability`,
  `unity_style_structure`, `function_calls` and `redundant_temporaries`.
  Explain what you inspected and concluded; avoid four interchangeable “passed”
  claims or copying the criteria as if they were observations.
- `scope_review`: the structured whole-source coverage below. It accompanies
  the same assessment call; it is not another review round or compilation gate.
- `verdict`: `needs_work` for remaining feasible source repairs, `complete` only
  when the full requested source quality is met, or `blocked` when a concrete
  limitation prevents progress.
- `findings` and `next_actions`: concrete remaining issues and feasible repairs;
  `complete` requires empty lists and an empty `blocker`. For `blocked`, supply `blocker` identifying
  the unavailable capability/evidence/permission or specific unresolved failure,
  along with attempted recovery. Available baseline DXIL, current HLSL and a
  useful independent reference must be considered before claiming missing source
  evidence. Say which dependency or operation still cannot be resolved; “no debug
  source”, anonymous-variable counts, complex phi nodes or lack of original names
  alone are not blockers. See [reconstruction.md](reconstruction.md). Task size or
  time spent alone is not a blocker.
- `no_change_reason`: when claiming `complete` without a source change in this
  task, explain why the existing source already satisfies the full request,
  backed by all four check evidence texts. The receipt calls this `noChangeReason`.
  In the final answer explicitly state that this task did not modify source and
  that the Agent's self-review found the current version sufficient; do not
  present old changes as a newly completed rewrite.

### Whole-source scope coverage

Use [scope-review.md](scope-review.md) for the structured `scope_review` fields,
full function/branch coverage and candidate dispositions. Keep the scope map
from the initial whole-file read and update affected outcomes with the draft.
