# Frame-flow resource dependencies

This change updates Bread AI and the diagram renderer. The user's capture
diagram is not reanalyzed, republished or edited by this development task.
The user will test the complete analysis through the built-in AI.

## Cause

The previous graph check treated a resource snapshot as reachable from itself.
Repeating the same EID/output under different arithmetic labels could therefore
produce a visible multi-step graph without another draw. A second problem was
that resource IDs alone did not distinguish disjoint mip/slice views.

## Analysis and presentation

- Explanation Skill v20 starts from a meaningful module result, follows the
  inputs actually used by its Shader back to contributing producers, and
  verifies forward consumption/composition before overwrite. It follows actual
  execution order, view ranges and partial writes, with history/import boundaries
  and reusable investigation caches. Bindings and the nearest writer are
  candidates, not independent proof of the computation.
- `inspect_frame_resource_usage(order="nearest")` pages backwards before a
  consumer or forwards after a producer using the cached usage index. Its default
  execution order is unchanged. Neither a partial page nor a resource use is
  promoted to complete provenance.
- Native stage inventories retain descriptor mip/slice ranges. Older texture
  inventories refresh once on demand. Persisted output inventories retain the
  same range metadata.
- A displayed module graph needs a supported transfer between distinct events,
  matching resource views and representative media participating in the path.
  Repeated snapshots, single-draw arithmetic and parallel outputs remain useful
  as independent result images, without invented arrows. Real forks, merges,
  different versions of one texture and before/after comparisons remain valid.
- Old documents receive the presentation safeguards when displayed. Authored
  graphs and evidence remain intact. Publishing returns compact per-stage
  show/omit decisions and reasons to the AI; these display choices do not create
  a new analysis-completion blocker.
- Machine semantic tags are hidden from captions. Human descriptions and
  channel explanations remain visible.

No production rule names a particular EID, resource or rendering technique.

## Verification

Deployed runtime **1.13.59**, explanation Skill **v20**:

- 659 frame/Host/bridge/UI-model tests and six subtests passed.
- 82 Skill/task-review/integration-manifest tests passed.
- The deployed native UI fixture passed 100 checks, including independent MRT
  results, retained genuine process media, channel popups and full/split export.
- Translation synchronization, complete catalog build, source-literal lint,
  Python compatibility and the native `/m:2` relink passed.
- MCP/Internal packaged modules were checked for the final code; all bundled
  explanation/core Skill Markdown files were byte-compared with source.
- Bread restarted with the original capture, EID and selected texture restored.
  All 364 files in the capture's diagram companion bundle retained their hashes.
  No frame-diagram analysis or publication tool was invoked against the capture.

Two outdated task-review assertions were aligned with the already-established
concise-answer contract, while retaining verification of full internal review
evidence. The native positive-flow fixture was corrected to use distinct
intermediate snapshots instead of the duplicated snapshots this change rejects.

Verification and deployment receipts are recorded under the operating-system
temporary directories `bread-frame-flow-deploy-159` and
`bread-frame-flow-native-159`.
