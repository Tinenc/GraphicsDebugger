# Localize a numeric mismatch

Keep the immutable session baseline and latest full proposal. Read the failed
comparison's `observation.metrics.outputs[].mismatchSamples`, `worstMismatch`
and `mismatchBounds`: they give pixel coordinates, component, both values and
allowed error. Use `observation.baselineStability` when present to check whether
the repeated original output is stable. A fluctuating baseline is a diagnostic
problem to resolve; neither widening tolerance nor accepting a failed comparison
is a repair. Missing samples from an older receipt call for one comparison with
the current diagnostics, not a broad capability search or capture reload.

Trace affected outputs back to changed calculations. Prioritize changes to
precision/contraction, operation order, normalization, division and zero handling,
derivatives/sampling and conditional execution. Use targeted source/IR evidence;
do not undo unrelated readable naming, comments or function boundaries.
If the culprit is not apparent, partition the actual changed calculation groups
and compare a trial retaining roughly half of those changes against the fixed
baseline. Keep each trial compilable by moving definitions, signatures, shared
data and callers together. A failing subset narrows the next trial; a passing
subset directs attention to its complement. Interacting edits may require
testing complements or a smaller group, so do not assume monotonic failure.
Keep a short hash-to-result ledger and reuse an unchanged trial's receipt.

Use `diagnose_shader_organization_candidate(workspace_path, source_text,
expected_source_sha256, label)` for those diagnostic variants. Supply the full
compilable trial text; `expected_source_sha256` is the current saved canonical
source hash, not the trial hash. It retains an isolated source/blob/receipt under
the same workspace, checks the captured interface and compares to the original
with fixed tolerances, then restores replay. It does not alter the canonical
source, organization proposal, published revision, review or live-edit state.
An intentionally partial localization trial needs no `complete` source review.
Do not save it as canonical source or send it through the final-delivery gate.
`ok=true` means the diagnostic operation ran; inspect `accepted`, `observation`
and baseline stability to learn whether the trial matched.

When `baselineStability.withinTolerance=false` or
`diagnosticCode=baseline-replay-unstable`, the observed candidate difference is
not yet conclusive. Investigate the repeatability of the original replay using
the supplied diagnostic, rather than identifying a source bug from a moving
baseline or assuming all candidate error is noise. A new informative trial or
stable observation can advance recovery; unchanged retries cannot.
`replayMethod="same-input-paired-draw"` identifies a paired observation with
shared draw inputs. If unavailable, `pairedReplayUnavailable` explains why the
fallback replay path was used. Baseline instability on that path does not make
the candidate successful; retain the unresolved comparison and its diagnostic.

Only run a new physical comparison for a changed, useful trial, after source and
compile/interface preconditions. These diagnostics are recovery after the one
whole-draft final validation, not per-function validation during normal editing.
Restore the full intended proposal with the localized repair, reassess affected
source/callers, then finalize that exact source. Preserve trials separately from
the canonical delivered source. Continue while a concrete localization or repair
step is available. If repeatable progress is impossible because the baseline is
unstable, required observations are unavailable or an external limit stops the
Agent, save the draft and report the specific remaining difference and attempts;
do not substitute save-only success for the requested applied result.

For a failed pixel trace, inspect a returned `traceArtifact` before treating the
inline preview limit as missing debugger support. Read the saved trace through
`read_evidence_file` and its returned offsets; use the artifact's completion and
truncation facts to distinguish a short preview from an incomplete execution.
