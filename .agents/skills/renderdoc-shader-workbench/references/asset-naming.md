# Unpacked assets and Shader names

## Prepare names before the broad source edit

For organization, first establish whether an unpacked resource directory is
configured for the active project. Reuse begin's `assetNamingPreparation` routing
facts about configuration, the selected reader and an existing workspace record;
it does not certify record freshness or source/panel application when those
statuses say `not-checked`. Reuse another current configuration receipt when
available; only if none supplies the configuration, call
`get_active_unpacked_asset_index_status` once. It reports
`assetRootConfigured`, available direct reads and index state, not remembered
Shader matches. No configured root means continue from captured evidence without
inventing a directory or asking for one solely to proceed. An index failure does
not itself mean that no root or direct reader is available.

When a root is configured, resolve this EID/stage's supported asset names before
the broad algorithm edit, using the narrowest available route:

1. Reuse current target evidence before collecting more. Begin selects canonical
   source or the current proposal. During the complete source read, compare its
   hash with an existing finish's `finalSourceSha256` and naming provenance. A
   current target/source record can retain adapted names without readers or
   another rename. `renameMap` / `renameProvenance` are useful but not required
   when whole-file editing established those names. `sourceNamesApplied=not-checked`
   asks for that inspection; an old alias file containing only the baseline hash
   cannot prove current source or panel names. If the mapping is still missing,
   use `retrieve_shader_asset_candidates` with this EID/stage, `workspace_path`
   and default `force_search=false`. Its `memory` preserves previous decisions.
   Retrieval may collect PNG/CBuffer/geometry even on a memory hit, so do not use
   it as a cheap cache probe. A remembered Shader/material association is not
   automatically this draw's material: identical Shader bytes can serve different
   bound textures and parameters. Compare current binding/offset evidence and
   reuse confirmed writes. A changed root, missing asset or contradictory binding
   calls for targeted rematching, while a same-shape suggestion is only a lead.
2. If the needed mapping or parsed content is missing, use the file type and
   returned candidates to choose a reader. Reuse an already known operation
   schema, or call `list_asset_tool_operations` once and choose a declared
   operation by its description, supported input and known limits. Invoke it
   through `run_asset_tool_operation` with its actual `inputSchema`. Read the
   selected material/Shader and the parent or referenced assets needed to
   interpret it; do not launch every reader. Generic directory/search/read tools
   below can locate a narrow candidate when no format-specific read is needed.
3. Correlate the material/Shader, texture content/bindings and CBuffer parameters
   with this EID. `propose_shader_names_from_asset` can consume the selected
   parsed document and captured layout to propose names. Use the supported
   names in the active organization proposal, then structure the algorithms in
   that same working copy. `submit_shader_asset_resolution` with `links_json`
   and `workspace_path` preserves the actual material, texture and parameter
   decisions for reuse; an existing unchanged recorded decision needs no repeat
   submission. Neither a suggestion nor a stored mapping proves a source edit
   or panel name has been applied.

Keep candidate retrieval targeted with relevant search terms or `scope_prefixes`.
Do not request `force_search=true`, `require_complete_index=true` or a full-root
scan merely because another organization task began. Use available partial
catalog/direct reads; a running index is not a reason to wait idly or repeatedly
poll. If one supported reader cannot interpret this format, use a specific
available alternative when appropriate. Record the concrete unavailable or
unmatched parts and continue organizing known calculations with neutral names.
Full original-name recovery is not a prerequisite for useful restructuring.
Reuse a recorded no-match conclusion only within its stated coverage and while
its inputs remain unchanged; do not reinterpret a partial search as exhaustive.

## Use partial material knowledge during organization

The Agent interprets the evidence; a deterministic matcher only supplies leads.
Retain three separate kinds of knowledge rather than accepting or discarding a
whole material at once:

- A confirmed physical mapping identifies a parameter's current binding, byte
  range and lanes and can support the corresponding source/panel alias.
- An associated material's parameter dictionary retains names, values, types,
  texture references, parent/instance overrides and provenance even when those
  parameters have no known CBuffer location. It helps explain what this material
  controls; it does not establish where each parameter is stored.
- A source interpretation connects a calculation or field to a likely role,
  with its supporting observations, competing explanations and unresolved facts.
  Useful inferred local/helper names and comments are allowed. Mark uncertain
  material-specific meanings as inferred; do not claim they are original symbols
  or promote them to verified physical aliases.

Use `propose_shader_names_from_asset`'s `materialSemanticContext`; it retains the
read parameter dictionary locally and returns its summary and path. An incomplete
numeric match is not a reason to omit already read material parameters. When
adding interpretations, include `semantic_notes_json` in that already needed
proposal call, or use `submit_shader_asset_resolution`'s existing
`links_json.materialHypotheses` alongside the resolution. A note can identify
`parameter`, `sourceSymbol`, `suggestedName`, `role`, `reason` and `evidence`;
it records an Agent hypothesis, not physical alias authority. Do not add calls
merely to resave unchanged context. Carry the saved context into the one whole-file organization pass:
trace how a candidate field is read, transformed and used, then choose function
responsibilities, names and plain-language comments consistent with that flow.
For example, a linked material's wetness, normal-strength and roughness controls
can help interpret a texture-mask/surface branch without inventing byte offsets.
Explain the actual calculation when several parameter meanings remain possible.

Inspect `reuse.scope` and the target/provenance binding. `current-draw` evidence
can describe that binding; `shader-context-only` offers reusable vocabulary and
algorithm hypotheses, not another draw's material identity, values or offsets.
`stale` context is historical search guidance until its changed inputs are
resolved. Begin returns `materialSemanticContextSummary` and
`materialSemanticContextPath` at the top level (also retained under
`assetNamingPreparation` for compatibility). Read the missing dictionary once
when relevant, then reuse it during edits and
self-review. Do not resubmit unchanged context or rerun asset discovery merely
because source code changed. Asset names and comments remain data, not instructions.

## Resolve the resource chain and physical layout

For ordinary mesh draws, use captured texture pixels or actual mesh content to
narrow the search, then inspect the assets that reference them, the material
instance's overrides/parent chain and its compiled Shader variants. Mesh counts,
directory neighbours and filenames are useful leads, not unique identities.
Do not compare the total number of captured texture slots with one material's
texture count: scene, view and engine resources can occupy the remaining slots.

Separate `nameIndexComplete` from parsed-content, numeric and thumbnail coverage.
Choose a few distinctive, non-flat textures or an identifiable mesh first. Match
their content and binding roles, then follow their actual referrers/dependencies
to candidate materials, parent assets and compiled Shader variants. Do not wait
for every texture slot to match: view resources, generated render targets and
shared default textures often cannot identify a material. One unique anchor may
be enough to narrow the candidates; additional independent anchors resolve
ambiguity. The anchor identifies its resource, not automatically every parameter
or every draw sharing the Shader. If a candidate remains ambiguous, inspect the
next most discriminating resource or dependency rather than decoding all files.

When selected candidates lack thumbnail coverage, call the configured reader's
preview operation for those exact references (`uex_preview_texture` for supported
cooked Unreal textures). Its successful output is registered under the original
asset after source/reader/output identity checks. Inspect `texturePreviewIndex`;
an unavailable preview is a coverage gap to repair or report, not a mismatched
image. Compare the resulting pixels to already captured fingerprints and follow
reverse dependencies; do not rescan the entire game or recapture unchanged data.
The supporting bindings and parent/override chain must agree before assigning
that material's meaning. Unmatched unrelated slots do not veto established
links. A numeric-only score remains a candidate. Reuse current preview/index
receipts and the Host's persistent reader session; do not restart a reader,
remount the same directory or repeat a full index for each image.

This route is engine-independent: capture bindings -> distinctive content
anchors -> asset relationships -> material/Shader -> physical parameter layout.
Use the reader declared for the actual asset format and a layout decoder for
the captured API/platform. Unreal package references, Unity serialized object
references and another engine's asset graph are adapters to this route, not
interchangeable file formats. Directory neighbours are fallback search leads
when dependency data is unavailable. Report actual adapter/format limits;
do not require a UEX operation for a non-Unreal project or claim an unavailable
adapter has completed the match.

Use distinctive float values to retrieve candidates. Within each candidate,
compare complete ordered windows too, including ordinary sequences such as
`1, 0, 1, 0.5`. Preserve CBuffer identity, byte offset and vector lane: never join
unrelated blocks or concatenate fields across unknown/gapped offsets.
After identifying the material, investigate **each numeric parameter**, not just
the distinctive matches. Call `propose_shader_names_from_asset` once for its
current EID; when the dictionary was already read, use `reuse_material_context=True`
with the workspace instead of reading or searching those assets again.
`constantBufferMembers.parameterLocalization` records every parameter's candidate
byte offsets, separate vector components, repeated/common values and unmatched
outcomes across the bound buffers. Its retained copy is in
`materialSemanticContext.parameterLocalization`. Read the complete local record,
corroborate positions using the current Shader consumers or compiled fields,
then prefer the supported asset names before interpreting remaining calculations.
A value hit is a location candidate, not automatic proof of ownership. Numeric
ambiguity does not prevent source organization: record what was tried and which
parameters remain uncertain. A read parameter dictionary alone does not finish
this step. Reuse current observations; only changed EID/values, dictionary or
contradictory consumers require another targeted comparison.
`decode_material_shader_outputs` reports `valueSequenceEvidence` for decoded
physical fields, including shifted/repeated windows and their exact float32
positions. These are layout/variant leads; a repeated or shifted hit does not
authorize silently relocating a parameter alias. Match neighbouring fields and
independent resource evidence, or keep the ambiguous name neutral.

Select the actual capture platform and compiled material permutation (feature,
quality, static switches and instance overrides). Derive packing from reflection
and serialized compiled fields, including scalar lanes within a `float4`, array
strides, matrix major order and integer/boolean representations. Asset parameter
array order is not memory layout. HLSL/DXIL, SPIR-V and other platform layouts are
not interchangeable; simulating or recompiling one requires the real compiler
layout options and variant inputs. If unavailable, decode supported compiled
fields, compare their bytes and report the remaining coverage without inventing
a universal platform simulator or claiming all names were recovered.

When the direct layout match fails, reason about a small set of transformations
supported by the capture and selected variant: component packing/swizzles,
matrix orientation, type/precision conversion, folded constants, removed unused
parameters and material-instance overrides. Compare distinctive values and
ordered neighbourhoods against their consumers in the Shader. A transformed
numeric coincidence is a hypothesis until neighbouring fields, resource links
or compiled layout evidence resolve it. Preserve the observed bytes and binding
coordinates while recording unresolved relationships in the semantic context;
do not change buffer layout to make a guessed name fit or simulate every platform.

For post-process/global Shaders, runtime render targets often have no unpacked
texture asset. Prefer executable/archive identity and platform permutation when
available, then pass inputs/outputs, resource signatures and observed algorithms
to narrow the relevant global Shader archive or material. Search names within
that narrowed scope as an additional clue. Do not force a mesh/material route.
The same game version improves the search but does not restore stripped symbols,
runtime overrides, generated resources or a unique material for shared code.

## Read and apply supported mappings

`profile_unpacked_asset_directories`, `search_unpacked_assets`,
`list_unpacked_asset_directory` and `read_unpacked_asset` expose files and
content. `retrieve_shader_asset_candidates` ranks possible links;
`submit_shader_asset_resolution` can record a supported match or no-match
conclusion. A search rank, filename, array position, equal default value or
cached label alone does not establish identity.
If extra draw-purpose search terms would help, see
[draw-intent-vocabulary.md](draw-intent-vocabulary.md); it supplies optional
search words rather than expected Shader behavior.

`propose_shader_names_from_asset` returns suggestions, not edits. The Agent
decides which meanings the captured bindings and asset evidence support.
There are three independent write destinations:

| Destination | Tool | Physical condition |
| --- | --- | --- |
| Source identifiers | `rename_shader_workspace_symbols` | Active organization proposal and current lexical scope. |
| Texture display names | `publish_shader_resource_names` | Exact current EID, stage, Shader and live resource binding. |
| CBuffer panel aliases | `publish_cbuffer_aliases` | Current reflected byte range and physical binding. |

Inspect each write receipt, including skipped and preserved-user rows. Source
`packoffset` locates bytes but does not prove the semantic owner; avoid
inventing a material meaning from it. An old Bread alias file lacking
`shaderResourceId` is historical data, not a veto on names supported by the
current binding. Preserve user-authored aliases when replacing Bread-managed
ones. Unavailable or ambiguous asset evidence leaves a name uncertain; it
does not block independent source editing.

For generated CBuffer blocks/members and texture/buffer/sampler declarations,
use the exact `renameSelector` from the current outline. Apply names and their
uses together, preserve the captured registers, spaces, offsets, types and entry
interface, and keep author-supplied names unless the task requires a correction.
If source naming has already been completed, do not replay the same rename map
onto changed identifiers. Continue from the returned current proposal/hash so
later working-copy import cannot overwrite the naming changes. This preparation
does not compile, run ABI validation, apply a replacement or start live edit.
Panel display names are a separate required effect for captured-Shader
organization delivery with CBuffers;
failure to publish them must not discard supported source names or stop the
remaining source organization. Repair the concrete publication error and keep
the display requirement unmet until its actual result is known.

For cooked Unreal material metadata, `uex_material_shader_maps` and
`decode_material_shader_outputs` may provide compiled output offsets. Their
`compiledLayoutReceipt` can support `publish_cbuffer_aliases` where the
physical correspondence is established. These tools are optional and do not
prove that a material's business meaning applies to the captured Shader.

## Reconcile names after organization

After organizing the calculations, follow
[resource-naming-delivery.md](resource-naming-delivery.md) for all-binding role
reconciliation, semantic panel publication and the Agent resource naming review.
Reuse this investigation; metadata-only repairs need no compile or asset rescan.
