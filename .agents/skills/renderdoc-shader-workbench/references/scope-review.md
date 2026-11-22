# Whole-source scope coverage

Keep the working scope map made from the initial full file and the request.
`begin.sourceReviewScope` reports `baselineSourceSha256`, `initialFunctions`
and `currentFunctions`. The initial identity remains the organization baseline
when resuming an existing draft. A new request to reorganize starts a fresh
whole-current-source assessment even when this draft or its old review is reused.
Read all bodies and branches and reconsider responsibilities; do not carry over
old satisfied decisions without that inspection. Map its calculations to the
current result. Within the same assessment, unchanged reviewed helpers need no
edits or repeated reads. A plan can group
related functions, including all unchanged ABI/load helpers in one group.

Supply this object as `scope_review` (the saved receipt uses `scopeReview`):

```json
{
  "baseline_source_sha256": "<begin.sourceReviewScope.baselineSourceSha256>",
  "plan": [{"id": "lighting", "requirement": "Organize all requested lighting paths", "functions": ["<initial function names>"]}],
  "results": [{
    "id": "lighting",
    "status": "satisfied",
    "source_functions": ["<current functions implementing those paths>"],
    "evidence": "<body simplification; responsibility/call design; shared algorithms; shared results: actual functions/consumers and outcomes or specific retention reasons>",
    "branches": [{"path": "<material variant or fallback path>", "status": "satisfied", "evidence": "<actual path and result inspected>"}],
    "retained_intermediates": "<specific values kept for a meaningful result or actual precision/control-flow requirement; say none when none remain>",
    "remaining_work": []
  }],
  "observation_dispositions": [{
    "candidate_id": "<current full catalog candidateId>",
    "scope_ids": ["lighting"],
    "status": "retained",
    "evidence": "<why this alternative changes the actual contract or is less clear, including affected consumers>",
    "alternative": "<specific simpler expression, shared result or helper arrangement considered>",
    "source_anchor": {"start_line": 10, "end_line": 12, "text": "<exact current lines of the related calculation>"}
  }]
}
```

Use `satisfied`, `unmet` or `blocked` for each scope and each of its branches.
The plan covers all initial function names; the results cover all current
functions, including newly extracted bodies. Each planned ID has one result.
For a straight-line helper, one straight-line path is sufficient. Inspect every
requested material mode, alternate path and fallback within its owning scope;
do not replace them with a statement that only the entry was traced. Grouped
functions and overloads still require reviewing their actual bodies.

An unmet/blocked scope keeps concrete `remaining_work`. A satisfied scope cannot
contain unfinished branches or repairs; `complete` requires every scope and
branch satisfied. Existing progress maps can supply this structure. Do not drop
a difficult scope or replace its original requirement when updating the result.
Four global evidence paragraphs without this coverage cannot admit a new
complete assessment. Older receipts remain readable but need the missing scope
assessment before final publication; reuse their valid observations.

Do not justify an entire anonymous scalar body with "preserves precision".
Identify the particular operation/order that needs intermediate values, retain
those meaningful values, and still simplify adjacent aliases, repeated component
calculations and untouched feasible branches. A new wrapper around the same
opaque body leaves that scope `unmet`. No naming pattern, variable count or
function length alone determines completion, and recording the scope map does
not establish that the semantic conclusions are correct.

Likewise, evidence such as “extracted lighting functions” is insufficient when
those functions are only a flat mechanical partition. Explain how their callers,
inputs and results represent the computation. Do not manufacture call depth by
adding pass-through wrappers, or force unrelated shaders into a lighting design.

The Host verifies byte identity and record structure, not semantic quality. A
successful recording is not a compilation, replay replacement or independent
expert approval. Do not mark unresolved work complete just to satisfy a lifecycle
check. Later source changes invalidate the old assessment; review the affected
result and record its current hash. A final answer reports the source self-review
outcome separately from the saved/compiled/applied effects and any real limitation.
Even source-only delivery requires saving the reviewed proposal as workspace
Source before ending; the review itself does not perform that save.
