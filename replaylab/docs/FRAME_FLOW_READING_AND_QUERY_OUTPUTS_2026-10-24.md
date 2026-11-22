# Frame-flow reading experience and query-output audit

## Observed run

Task `6d727741baff4d8aba910e1bae4b06fb` ran from 16:29:44 to 17:20:53.
It did perform fresh work: 33 stage inspections, seven read-only Shader
representation requests, and a saved 24-stage diagram covering 8250/8250
effective actions. The publication contained 14 module graphs, 266 preview
records and 76 prepared channel views. Preview records include detail variants;
they are not 266 distinct rendering techniques.

Seven publication attempts were rejected: three ambiguous output omissions,
one disconnected graph, and three images outside the declared capture/stage/
attachment scope. The Agent corrected those publication errors. Three nearby
API-event reads were also rejected before it read the correct action. The
final saved diagram had one remaining quality gap.

That gap was a Host inventory defect. A native query resolve had an exact
destination buffer and native API parameters, and the Agent recorded a
non-image terminal result. However, stage inspection only enumerated Shader
bindings and omitted the action's buffer destination. Consequently, the
non-image validation could never accept the already investigated result.
The event/resource numbers from this incident remain audit evidence; no
production rule depends on them.

## Fixes

- Utility actions enumerate exact buffer operands through the native buffer
  catalog, excluding inherited Shader bindings. Unknown resources are not
  automatically classified as buffers. Publication preserves and checks that
  the output matches the exact action destination. The mathematical/composition
  validation is unchanged.
- Legacy utility inventories are refreshed once when inspected. The refreshed
  receipt is cached, avoiding repeated use of the incomplete old receipt.
- The final response no longer has the entire internal self-review ledger
  prepended. It retains the actual answer, unfinished status and a concise
  blocker. Full review evidence remains in task logs.
- A theme-aware, resizable stage directory follows execution order. Clicking
  scrolls to the module without changing EID; reading/scrolling highlights the
  current module. Image export includes the graph, not the directory.
- Channel popups use short component-prefixed titles with the complete
  explanation below. Legacy entirely text-only subgraphs can be omitted while
  their evidence and stage previews remain available.
- Existing unique event-matched media can illustrate module nodes. Every
  automatic association must preserve a valid resource path, including parallel
  MRT semantics. Declared, compatible before/after views of the same resource
  can use a divider for subtle changes. The small-preview comparison is cached,
  uses no GPU replay, and is never numerical-equivalence evidence. Comparing
  different resources still requires an explicit relationship.
- Explanation skill v19 describes these adaptive choices without fixed EIDs,
  technique lists or an obligatory graph for every stage.

## Verification

Runtime **1.13.58**. Frame/Host/bridge/UI-model regressions: **586 passed** and
six subtests. Task/Skill/Agent regressions: **173 passed** and four subtests.
The final deployed native UI fixture passed **98 checks**, including directory
navigation, scroll synchronization, channel captions, themes and full/split
PNG exports. Translation extraction, synchronization, complete catalog build,
source-literal lint and native relink passed.

Packaged MCP/Internal modules were inspected for final inventory and media
logic. Every Markdown file of the bundled explanation and shared core Skills
was byte-compared with source. Deployment verified all 28 canonical bridge
files. Runtime and UI evidence is under the operating-system temporary folders
`bread-frame-flow-deploy-158` and `bread-frame-flow-native-158-final`.

The installed MCP was then exercised against the restored real capture. It
automatically refreshed the legacy utility inventory, recorded the exact native
buffer output, and republished the existing analysis through the normal public
tools. The 24-stage/266-preview diagram now has zero validation gaps,
`analysisComplete=true`, `savedWithCapture=true` and `window.published=true`.
This is verification of the recorded analysis and repaired evidence path, not
a claim of newly reverse-engineering every Shader again. The original selected
EID and texture were preserved. The receipt is `revalidation.json` in the
deployment evidence directory.
