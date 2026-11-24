# Reanalyze a frame and resolve disputed dependencies

Use for an explicit request to redraw, regenerate or analyze the frame again,
especially when a previous attempt merely republished old conclusions or stopped
at a suspected broken chain. A request only to change styling, export or restore
the existing diagram does not require this semantic reassessment.

## Start a new assessment, keep useful evidence

Bind the request to the current capture and requested scope, and start reanalysis
with `prepare_frame_render_flow(reanalyze=True)`. This starts an empty authoring
draft and returns raw chronological candidates; it preserves the old saved
diagram without feeding its semantic nodes into the new draft. Read the full
requested action outline and build a new working explanation from the evidence.
A successful preparation call establishes
the working context; it does not mean that the algorithms were reconsidered.

Use current action order and resource relationships to assess the frame again.
The old diagram is a comparison baseline, not the initial answer to approve.
Use section="baseline" only for comparison after independently identifying the
frame's functions and relationships. Do not copy its stages into proposals and
rewrite their investigation text to satisfy a freshness check.
Reconsider the important functions, stage boundaries and resource handoffs
throughout the requested scope, including previously accepted groups. The old
gap list is neither the scope of this work nor a prescribed stopping point.
Look inside broad groups where computation, geometry, state or resource use
changes, and rebuild the explanatory grouping from those observations. Shared
analysis for equivalent draws is useful; investigating every draw with every
tool is not required. Do not preserve or alter a module merely to match an old
layout, the user's example, a technique list or a desired stage count.

Verified source, action records, resource observations and image receipts can
remain cached. Check their identities and applicability, then reconsider what
they imply for this request. Actually reread the relevant source and bindings,
view representative images and investigate geometry where it distinguishes
competing explanations. Old descriptions of those observations are conclusions,
not substitutes for examining the evidence. Select evidence according to the
question; there is no required tool count, runtime or fixed list of techniques.
Reusing those raw observations is different from
accepting their previous interpretation. An unchanged conclusion is valid when
this assessment supports it; changing names or deleting useful graphs just to
make the result look new is not progress. Refresh the evidence affected by a
capture, source, binding or replay-content change; do not recapture unchanged
pixels or recreate Shader workspaces merely to demonstrate activity.

Keep a compact review record of the current assessment: which important
explanations or relationships changed, which were retained and why, and which
specific questions remain open. Write the new stages' investigation.evidence with
the findings and reasoning from this reassessment, including why an unchanged
conclusion remains supported. No special wording, extra field or fixed evidence
count is required. Do not use a new binding receipt, renamed stage or changed
caption as a substitute. Keep this in investigation/review records rather than
a second user-facing form or a fixed per-node checklist. This record
should distinguish a checked, unchanged interpretation from one carried over
without review. Resolve the useful open questions while constructing the new
proposal, rather than leaving semantic investigation to the final validator.

## A disputed edge is a question, not a terminal result

First identify what conflicts: an actual API/Shader observation, a coarse usage
candidate, a Host check, a stale observation, or a failed read. Phrase the
question narrowly: which contents reach which consumer, and which observation
would distinguish the competing explanations? A suspected overwrite or absent
usage candidate is not by itself proof of a broken dependency.

Use the versioned tracing rules in
[algorithm-investigation.md](algorithm-investigation.md). Depending on the doubt:

- Confirm both endpoints' capture/session, EID in execution order, resource,
  view, mip, slice/layer, sample and relevant channel or region. A later use of
  the same ResourceId may refer to different contents; an alternate view need
  not be a different underlying resource. Resolve aliases only where actual
  resource/view evidence supports them.
- Test whether the proposed producer actually modifies the required contents.
  A bound output or marker is only a candidate. Examine the relevant draw or
  dispatch workload, stores, write masks, blend/load behavior and coverage as
  needed. An unknown indirect workload is not zero work. A draw that updates
  part of an atlas or preserves destination channels does not erase every
  earlier contribution. Check the recorded range of a clear/discard and the
  source, destination and region of a copy/resolve before declaring replacement.
- Walk backward from the consumer's actual Shader read or fixed-function use,
  and forward from the candidate producer's useful result. If they do not meet,
  investigate the intervening transformations or alternate writers that could
  explain the mismatch. Follow copies, resolves, repacking and composition
  through their real resources rather than requiring one unchanged ResourceId.
  A destination blend can consume prior contents without a sampled SRV. Avoid
  cycles by retaining visited resource-version/question pairs and shared results.
- Use another evidence route when it can answer the same question: retained
  exact bindings and readable source/IR, recorded API arguments, resource-use
  history, a targeted live read or before/after image, or associated unpacked
  material/Shader data. Names and visual resemblance narrow the search; actual
  operations and uses establish the relationship. An asset association cannot
  prove captured execution or replace unavailable dynamic facts.

These are discriminating observations, not an ordered tool checklist. Expand
the unresolved boundary while useful candidates remain; continue independent
modules in parallel with that reasoning. A failed dynamic read should not
restart already-supported work or invalidate saved facts. Follow the existing
recovery guidance for an unhealthy replay controller; do not retry different
wrappers around the same failing state indefinitely or reopen the capture
without the supported recovery conditions.

Conclude an edge only as far as the evidence supports: confirmed handoff,
corrected route/version, disproved candidate, evidenced external/history
boundary, or a specific unresolved observation. If a Host rejection conflicts
with exact evidence, retain both and investigate the mismatch using the supported
correction or recovery path. Do not remove a real dependency, invent another
one, or weaken its meaning to make validation succeed. A real tool limitation
may leave a useful partial result, but a first read error or unexplored usage
candidate does not establish that limitation.

## Publish the assessment actually performed

Apply [render-path-review.md](render-path-review.md) to the revised explanation,
including previously accepted stages; old completion receipts do not review new claims.

Build concise stages and image graphs from the supported functions and resource
flow. Keep a real cross-target transformation or composition visible when it
helps explain the module; omit unrelated and duplicate images. Reuse unchanged
media where valid. A missing optional illustration does not justify deleting a
supported calculation, and a surviving old picture does not prove a newly
claimed computation. See [module-discovery.md](module-discovery.md) for choosing
representative visual, mesh, state and unpacked-asset observations.

Review both semantic coverage of the user's request and the Host's structural
findings. Republishing an unchanged baseline or exhausting its old error list
does not establish that the requested reassessment happened. Likewise, a fresh
assessment may legitimately retain the same boundaries if the evidence supports
them. A Host reanalysis check can detect missing updated evidence records; its
success is not proof that the recorded reasoning is correct. Complete only when
the important claims and requested coverage are
supported; keep material unresolved questions explicit. Preserve the useful
draft and explain a remaining limit once, in plain language, with the next
action that would resolve it. Keep retry history and technical ledgers in logs;
the final reply states what was reassessed, what changed and any material gap.
