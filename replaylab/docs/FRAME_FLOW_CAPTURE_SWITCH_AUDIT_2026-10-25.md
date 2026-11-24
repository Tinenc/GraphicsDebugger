# Frame-flow capture switching and semantic coverage

## Observed task

The built-in task `dae15b1f4b6046969503242ed961eb3f` analyzed a different
capture, `6.rdc`, frame 6773. Its source outline contained 3,332 records and
1,925 effective actions in 544 candidate groups. The task paged through the
candidate groups; the missing explanation was not a truncated first page.

The final six authored ranges covered all effective actions numerically, but
all six investigations remained partial. One range covered 734 actions and
29 output combinations with principally one photographed event. The trace
contains nine successful stage inspections, 37 event inspections, 22 texture
captures, two Shader inspections of the same event, and one resource-use
trace. These counts describe the failure, not a required quota for future work.
Several materially different Shader/state clues were left unexplained.

Three publications wrote independent JSON artifacts but failed capture-linked
persistence. The final quality record still contained 32 findings, including
partial investigation, missing algorithm explanations, incomplete attachment
evidence and unverified bindings. Four failed stage reads requested EIDs that
were not valid outline actions; this was not a global replay failure. After
the first publication failure, delivery recovery displaced independent
semantic investigation that could still proceed.

## Actual persistence defect

The Host snapshot included `copySourceSubresource` and
`copyDestinationSubresource`; the bridge's canonical projection dropped both.
The actual failed outline had 148 source and 730 destination view records.
The same outline consequently produced different digests and failed the
save guard with `Frame diagram does not match its action outline`.

The bridge projection now matches Host fields and defaults. Existing capture,
snapshot, resource and recorded subresource checks remain. Older saved outlines
can acquire optional view fields they never recorded, using the freshly
collected outline of the matching capture. Changed recorded mip/slice/sample
coordinates remain a rejection. The failed outline now has the identical
Host/bridge snapshot digest `e190a394...`.

## UI lifecycle

Image-export status previously had neither expiration nor clearing on capture
and document changes. The panel now owns a six-second single-shot notice timer,
clears the notice and export receipt when its document/capture changes, and
binds asynchronous export callbacks to their originating generation. An old
encoder is canceled safely and cannot update a new capture's status. Switching
captures while the save dialog is open cannot export a different document into
the old selection. An exporter initialization failure restores usable controls.

## Discovery and explanation

`get_frame_render_flow(section="candidates")` now exposes original captured
grouping after publication and restore. Previously section="stages" switched
to the authored overview, making a broad early draft an inconvenient basis for
continued discovery. The new view is cached and paged without GPU replay.

Coverage additionally reports bounded uncovered execution ranges and exact
action-page offsets, with actionable quality findings for omitted actions and
truncated source outlines. Numeric coverage remains separate from semantic
understanding; an all-covering range does not prove its role was investigated.

Explain-event skill v25 routes whole-frame analysis through module discovery
before grouping. It distinguishes original candidates from authored stages,
requires further investigation when materially different contributions are
hidden inside a broad range, and keeps delivery failures separate from
independent semantic work. Names use supported graphics terminology with short
plain-language explanations. Appearance-only labels and Shader entry names
remain clues until the calculation, state and use support the interpretation.
There are no prescribed techniques, EIDs, stage counts or per-tool quotas.

## Validation and delivery

- Persistence, outline, frame store and channel suite: 168 passed, including
  A-to-B-to-A, same-capture reopen, stale session rejection, subresource
  tampering and old-format enrichment. The actual failed outline digest was
  also checked without modifying its artifact.
- Export lifecycle and panel suite: 135 passed, including 11 new lifecycle cases.
- Native hidden UI fixture: 108 checks passed in 17.05 seconds, including real
  notice expiration, full/split PNG export, capture switching during export and
  late callback isolation. Receipt:
  `%TEMP%/bread-frame-export-lifecycle-87244d81e5734e2b80dd9ad996759439/result.json`.
- Scope discovery, frame store, server and reanalysis suite: 132 passed.
- Final persistence/recovery/version subset: 94 passed.
- Skill delivery, tool contracts and agent autonomy suite: 125 passed.
- Translation sync, build and lint: 5,307 completed messages; no unfinished
  entries or new untranslated source literals.
- Actual failed document persistence in an isolated system-temp destination:
  all 3,332 actions, six stages and 28 image entries saved, 23 unique media files
  hash-verified, loaded and rebound to a new session successfully in 0.811 seconds.
  The original RDC, document, outline and capture sidecar pointer were unchanged.
  Receipt: `%TEMP%/bread-frame-flow-persistence-e2e-zb5ahe3f/receipt.json`.

Suites overlap and must not be summed as unique tests. No paid AI generation
or manual rewriting of the user's diagram was performed.

Version 1.13.63 was built and deployed after frozen-source/package checks:
257 source files remained unchanged during packaging; 20 embedded Python
modules and eight skill/reference files matched their sources. Canonical bridge
deployment verified 31 files. Installed executable hashes matched the build.
The native UI was relinked with `/m:2`, explicit `SolutionDir`,
`ReplayOptimizeDevelopment=true` and `SkipReplayLabRuntime=true`; it completed
with zero warnings/errors. Qt catalogs were already synchronized and compiled.

The original application was closed gracefully and restarted with its existing
`6.rdc`. Installed bridge 1.13.63 loaded frame 6773 in a new capture session and
restored the six-stage draft. A read-only native outline query returned all
3,332 source actions and confirmed exact Host/installed-bridge snapshot parity.
Receipt: `%TEMP%/bread-frame-flow-deploy-163/installed-verification.json`.
The draft's unresolved semantic status was preserved for the user's next
built-in AI test; no new analysis or completion claim was manufactured.
