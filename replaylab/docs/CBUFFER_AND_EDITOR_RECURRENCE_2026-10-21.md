# CBuffer publication and editor diagnostics recurrence

The recorded task `915d3eeb45a24239bf1f2b4fe9338a1d` ran from 11:49:08 to
12:17:25 local time. It made 67 MCP calls, including 14 capability queries,
one asset retrieval, two reader operations, four proposal writes, one source
review and one final validation. One UEX reader failed to return complete UTF-8
JSON; two logging layers recorded that same failure. The final source and
validation were saved, but the task made zero `publish_cbuffer_aliases` calls.
Its review explicitly excluded panel naming because the latest prompt did not
repeat the request.

The previous panel publication described source `4e89716d...` and active Shader
`283ec003...`. The new source is
`a4430c84c5c84d8f18e93d7187155ee72dbf6437282450aa1641f9e89534e068`,
with a different validated candidate and reflected layout. Applying the old
display interpretation automatically would incorrectly claim that it had been
reviewed against the new calculations. The exact-Shader UI guard was correct.

## Changes

- Shader organization now includes CBuffer presentation by default. Begin and
  finish return a separate source/candidate-bound `cbufferSemanticDelivery`
  obligation. An old or missing naming receipt cannot complete that obligation.
- The Agent continues its existing session when only this obligation remains.
  It reconciles and publishes display metadata without re-running source review,
  compilation or replay validation. This applies to API and subscription flows.
- Publication receipts retain source hash, active Shader hash, field counts and
  acceptance results. User semantic fields preserved by precedence count as
  accounted for; they do not cause an endless publication loop.
- Previous names remain bounded review hints for the same captured Shader;
  they are never silently accepted as the current display interpretation.
- The current CB5 plan was reviewed against all declared float lanes: 152
  properties covering 216 lanes, including 41 vector groups and 16 asset-assisted
  properties. Unrelated controls remain scalar. All names are marked inferred;
  this is not a claim to recover exact original game symbols or material identity.

## False editor errors

The bundled Slang provider advertises incremental text synchronization. The
client sent whole new text without a replacement range. Slang inserted that
text into the previous document, accumulating duplicate structs and functions.
The old protocol reproduced exactly 997 errors and 3 warnings, including the
ambiguous struct names at the screenshot's lines 2782–2785. The source on disk
was valid.

The client now respects the negotiated synchronization mode and replaces the
full previous document using UTF-16 positions. A narrow Slang Unicode-at-EOF
compatibility path reopens the semantic document without altering user text.
The actual 3,007-line Shader passed repeated grow/shrink/reload updates with
zero errors. An intentional unknown identifier still produced an error and
repairing it cleared the diagnostic. Existing semantic navigation/reference/
highlighting probes passed all 16 checks.

The changes ship in Bread integration 1.13.44 with Shader Workbench Skill 102.

## Installed acceptance

The final build completed successfully. Native editor/CBuffer tests passed 175
assertions across 13 cases. Frozen artifacts matched 53 Python modules, all 15
relevant Skill files and all 20 Bridge modules. The reopened project restored
four Shader replacements and four editable workspaces with no restore warnings.

The installed MCP published all 152 requested semantic fields against the exact
current candidate `4e4049bf5bf5605e85c30771e7ae067ce32f8c761fb50727658b38ec2076bd8b`,
with `partial=false` and no skipped fields. An application startup verification
script opened the actual CB5 viewer at EID 61408 and observed the original
physical rows before publication, then all 152 named semantic rows after the
file-watch refresh (153 total rows including the remaining physical row).
Both open Shader editor diagnostic controls reported zero errors and warnings.
The source hash remained unchanged; the source/candidate-bound delivery checker
now returns `complete`. Display publication did not recompile the Shader.

## Follow-up: native window placement and direct parameter editing

The above ad-hoc verification script called `Widget().show()` on a CBuffer
viewer without `CaptureContext.AddDockWindow`. It left an unmanaged child at the
main window's upper-left corner. The semantic rows were correctly in the native
BufferViewer, but the visible placement was a test harness defect. The fixture
paths now use the same transient docking entry as Pipeline State, and that
verification process was closed normally before deploying the follow-up build.

Integration 1.13.45 additionally carries naming freshness through status,
compile and direct apply results. Completion reads the current manifest's
candidate identity, so a compile after finish cannot be certified by an older
display receipt. Parameter-only changes do not invalidate source interpretation.
Direct CBuffer editing and its native acceptance are described in
`CBUFFER_DIRECT_EDIT_RUNTIME_2026-10-21.md`.
