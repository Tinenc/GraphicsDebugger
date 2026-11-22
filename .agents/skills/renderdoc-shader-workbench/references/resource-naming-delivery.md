# Resource naming delivery

Use after the whole-source organization pass; reuse asset evidence prepared with
[asset-naming.md](asset-naming.md).

## Names that can be distinguished in the panel

Keep parameter and resource identifiers in English by default, independently of the UI/chat
language. Prefer meaningful existing source/asset names; use the user's language for
descriptions and evidence. Use another naming language only when explicitly requested, and
always preserve user-authored labels. Do not translate an existing English Shader identifier
into a Chinese display alias just because the conversation is Chinese.

Publication counts alone do not prove delivery: inspect the actual CBuffer panel for the
current replacement, including after project recovery. Saved names on captured reflection
must also appear on the compatible compiled reflection. A missing display mirror is a tool
delivery defect to repair; do not reanalyse or rename thousands of already-known fields.

Choose a concise display name for the observed role, then only the qualifiers
needed to distinguish it from the other visible parameters or textures. Keep
known coordinate spaces, units or processing stages when they change the meaning.
Move explanations and provenance into `description`/evidence/status rather than
repeating a block name, asset directory, `inferred` or a type label in every name.
A description should explain that parameter's use; one generic provenance sentence
copied onto every field does not explain the fields. Texture names should say
what the texture represents or its role in this calculation, not repeat the same
material prefix followed by a resource number. Preserve exact matched asset
names as provenance even when a shorter display name is appropriate.

Distinguish the **physical identity**, **source expression** and **display label**.
Bind/space, byte offsets, scalar types, array indices and reflected paths must
remain traceable and unchanged. A valid source access such as an array element's
member is not redundant code. In a flat panel, a long shared `array[index].` prefix
can hide the useful role at the end of every row: choose a role-first label with
the index and the smallest necessary owner qualifier when that remains unambiguous.
In an actual grouped view, a visible parent can provide that owner context; do not
assume a parent exists or omit identity from a flat list. Nested arrays still need
the indices that distinguish their elements. Do not blindly strip everything
before a dot, drop indices, truncate names or rename user-authored labels.
The CBuffer panel can group proven array-role labels under one role, displaying
element numbers on its children and retaining full physical paths for search/editing.
Publish the full unambiguous label; do not remove its index in the stored mapping
to simulate that presentation. A unique visible element may use a shorter panel
label when there is no collision; this does not make the physical array a singleton
or remove its stored index. User labels and unproven array labels remain intact.

Compare proposed short names within their real display scope before publication.
When two different values would become indistinguishable, add the smallest
meaningful qualifier (owner, space, stage or usage), rather than restoring a long
common prefix to every row or appending arbitrary counters. Keep existing user
choices. Truly homogeneous array elements may share one role with distinct
indices; do not invent different meanings merely to avoid repetition. If the
consumer establishes only a packed or mode-dependent control, describe that
known role briefly and put the encoding/branch explanation in its description.

Review distinct roles and name patterns before expanding arrays, then check the
published panel/list for ambiguity and clipping using the available display
state. Reuse an established element mapping where its consumers justify it; a
thousand indexed copies do not supply a thousand semantic discoveries. Uniform
prefix replacement, dot-path conversion and successful write counts alone do not
establish naming quality. Record the useful shortening, required distinctions
and remaining uncertainty in the existing `reconciliation` evidence. Correct
supported deficiencies in the same task; do not force cosmetic edits to good names.

## Reconcile names after organization

Use the completed working source to revisit CBuffer roles before recording its
final source review. Trace each remaining physical field through its consumers:
where it enters a coordinate, normal, color, roughness, lighting weight or branch
calculation, and whether its components represent one quantity or unrelated
parameters. Refine earlier names when the actual data flow contradicts them.
Retain exact recovered asset labels as provenance alongside clearer source names.
This pass uses the already read material dictionary and source; only investigate
new contradictory or missing evidence, not every asset again.

Inventory **all** used CBuffer and texture bindings. A mesh material's matched
block is only part of that inventory: view, scene-lighting, draw and generated
resource controls still need their own consumer-based review. Reuse names
already present in source macros, structs and helpers. The existing outline's
`section="cbuffer-semantics"` returns each source block's member coordinates and
direct alias/vector candidates in one local read. Candidates are not automatic
groups: a `float4` containing direction.xyz and shadow.w becomes a direction
float3 plus a shadow scalar; literal padding is never a physical channel. Use
actual consumers and the already read asset dictionary to decide meaningful
groups and inferred names. Preserve explicit user choices.

For arrays of structs, preserve the reflected element stride and nested member
offsets. Reuse one understood element's role where its consumers establish the
same role across the array; do not spend tokens re-deriving thousands of identical
entries. Generate the explicit indexed display fields in the owned working file
from that reviewed mapping, then submit them through the normal naming tool's
`semantic_fields_file` and `semantic_fields_file_sha256` arguments. Use the file's
actual SHA256; leave `semantic_fields_json` at `[]`. Do not print or embed a large
generated array in a tool call: that duplicates data into the provider's message
stream and can exceed its message size limit. `binding_reviews_file` and
`binding_reviews_file_sha256` provide the same route for large coverage reviews.
These UTF-8 JSON files contain the same arrays as the inline inputs, stay in the
current controller-owned Agent working directory and are limited to 16 MiB each.
The Host checks the supplied hash and reads each file once, then performs all
ordinary schema, physical-lane and publication checks. File input grants no
semantic authority. Reuse the same operation ID after a timeout; identical file
and inline contents have the same publication identity.
Large published views use checked field pages selected by the current Shader
manifest. Publishing a complete block also asks the Host to retire obsolete AI
names for that captured Shader and binding, including old compiled layouts.
Current project heads, active replacements, recovery versions and user names stay
protected; historical receipts alone do not keep old mappings active. The Host
reclaims unreferenced pages after commit and protects interrupted transactions.
Do not delete store files yourself or shorten evidence to work around capacity.
Receipts include cleanup counts and may contain page references instead of every
field; read the current semantic view for details. After a timeout reuse the same
operation ID. Storage migration, cleanup and rollback use the same Host path for
every AI connection mode.
The mapping must still identify the exact array element and member; a display
label can be shorter under the rules above. Never flatten an array by guessing
a stride, label all lanes as color, or omit other buffers
because one large instance block dominates the field count. Split publication
by complete buffer blocks if necessary; replacing a block's view requires its
whole desired field set, not successive chunks that overwrite earlier fields.

Apply source renames and their uses in the same proposal with
`rename_shader_workspace_symbols` or the complete owned working-copy edit. Keep
registers, offsets, scalar types, resource bindings and entry ABI unchanged. Name
uncertain fields after observed operations; do not present an inferred role as an
original recovered symbol. A material match need not resolve every parameter:
known offsets plus clear consumers can support a useful inferred display field.
An ambiguous value with no established consumers remains physically named.

Prepare the semantic fields during this pass as part of captured-Shader
organization, then call `publish_cbuffer_aliases` after the unified
compile/ABI/apply result, or after saving for an explicitly source-only task.
An ordinary short follow-up organization request carries this delivery forward;
renaming source alone does not complete
it. Begin/finish return `cbufferSemanticDelivery` separately from the source
validation result. If it remains pending, finish only that metadata effect in
the same task. Reuse and reconcile existing semantic fields; a receipt from
another source hash or replacement hash is stale, even if its counts succeeded.
Do not call finish or compile again merely to refresh names/channel groups.
Source-only interpretations still describe that saved source, not an applied
replacement. Use its `semantic_fields_json` argument with the complete desired
set for each affected block and `semantic_source_sha256` for that saved source.
`rows_json="[]"` permits semantic fields alone; ordinary row aliases may be
included in the same publication. Reuse an operation ID after a timeout, as the
tool contract requires. For example:

```json
[
  {
    "bind": 5,
    "space": 0,
    "name": "baseColor",
    "type": "float4",
    "components": [{"offset": 832}, {"offset": 836}, {"offset": 840}, {"offset": 844}],
    "status": "inferred",
    "provenance": "shader",
    "evidence": "source/shader.hlsl:123 uses these four lanes as the color/alpha multiplier",
    "description": "材质的基础颜色和透明度；由整理后的乘法与输出用途推断。"
  }
]
```

The offsets above illustrate the schema, not a mapping for another capture.
Each field identifies `bind`, `space`, a `name`, `type` (`float`, `int` or `uint`
with an optional 2/3/4 suffix), ordered physical `components`, `status`
(`confirmed` or `inferred`), `provenance` (`asset`, `shader` or `combined`) and
specific `evidence`; add a plain-language `description` where useful. The status
expresses the Agent's evidence judgment, not a Host certification of semantics.
Each component's offset is in the actual block; components may be non-contiguous
or reordered when supported by the consumers. Never fabricate absent channels,
overlap fields or reinterpret a captured scalar type merely to make a vector.
Stripped original reflection may expose only overlapping `unknown` storage rows.
It cannot establish typed lanes. The Host can instead bind the display to the
current validated, applied candidate's exact typed reflection. Reuse that result;
do not demand recovered original names or force float aliases onto real uint
fields. Keep the original unresolved layout unchanged. Read
`semanticBindingBasis="verified-active-reflection"`, `semanticOriginalUnavailable`
and the current-display outcome rather than assuming both layouts were named.
Original unavailability is not a failed display when the active typed view was
written completely. This changes metadata, not the validated Shader bytes.

Group baseColor's actual RGBA lanes as one float4 row; keep roughness as one
scalar row even when packed beside other parameters in a captured float4. Three
color lanes plus an unrelated roughness lane form a float3 and a float, not RGBA.
This is a display projection only: no buffer repacking, reflection rewrite,
byte changes, new HLSL declaration or compilation is needed for panel grouping.
Unassigned channels retain their physical display and unknown meaning. Keep
distinct user-defined fields; inspect preserved-user/skipped receipts rather
than overwriting their names.

The publication replaces the AI semantic view for each mentioned block and
Shader scope, so submit the whole intended view for an affected block in one
batch. Unmentioned blocks are retained; a row-only call or empty field list does
not clear semantic views. Read `requestedSemanticFields`, `writtenSemanticFields`,
`keptUserSemanticFields` and per-block `semanticViewWritten`/`semanticFieldsWritten`.
Also inspect `requestedDisplaySemanticFields`, `writtenDisplaySemanticFields`,
`keptUserDisplaySemanticFields`, `currentDisplaySemanticFieldsAccountedFor` and
`semanticMirrorSkipped` for the currently displayed replacement.
An `applied` flag can refer only to ordinary aliases and does not prove these
fields were displayed. A physical-offset/type error calls for repairing the
affected field from current reflection, not dropping the full Shader or repeating
the asset scan. Record the publication result separately in whole-task acceptance.

Read `bindingCoverage`: it accounts for every current reflected block, not merely
the requested rows. Submit all supported groups together; untouched same-source,
same-candidate blocks retain their accounted state. For individual lanes whose
roles remain uncertain after inspecting their consumers, use
`binding_reviews_json=[{"bind":1,"space":0,"unresolvedOffsets":[16,20],"evidence":"specific consumer facts and remaining ambiguity"}]`.
They remain physically named and count as reviewed unknowns, not recovered
names. Do not use this escape for uninspected buffers, padding aliases or an
available source role. New source/candidate bytes invalidate old coverage.
No extra compile or replay is needed to repair these presentation facts.

Distinguish unknown roles from reflected storage proven unused by the current
program. The same binding review may include `unusedOffsets` and `unusedEvidence`
for the latter; these offsets must be disjoint from named and unresolved lanes.
Explain the inspected consumers, branches and indexing that establish non-use.
No literal access, an inactive current branch, or a generic whole-buffer claim
does not prove a dynamically indexed member unused. Revisit every remaining
unnamed consumer family after the source rewrite; use a supported operation-role
name even when its original asset label is unavailable. Leave genuine ambiguity
explicit instead of inventing names or treating a global unknown summary as
finished analysis. Review coverage `complete` is distinct from `namingComplete`:
report named, proven-unused and unresolved outcomes separately. Neither field
counts nor complete coverage proves the names' semantic quality.

Unused means no consumer in this current Shader entry and variant, not an empty
value, invalid parameter or unused storage across the game. Reflected declarations
and unconsumed vector lanes remain real, editable bytes; do not call them padding
unless the physical layout actually identifies an alignment gap. Unknown meaning
does not prove non-use. Preserve the physical field when no role is supported,
and describe confirmed non-use separately from unresolved roles. The Host carries
the source-bound, typed `unusedOffsets` review into the panel's current alias view;
do not create semantic names such as `unusedColor` to simulate this status or
erase a value. A later source/candidate revision needs its own review. In the final
answer, distinguish inferred names, reviewed unused lanes and remaining unknowns
briefly instead of claiming every reflected byte has a recovered meaning.

The optional `naming_evidence` argument preserves the Agent's provenance claim.
Its object shape is exactly:

```json
{
  "schema": "bread-cbuffer-naming-evidence/v1",
  "summary": "What the inspected source or matched asset establishes",
  "references": [
    {"kind": "shader-source", "locator": "source/shader.hlsl:123"}
  ]
}
```

Use 1–16 reference objects, not strings. `kind` is `capture`, `shader-source`,
`unpacked-asset`, `user-description` or `analysis`; `locator` identifies actual
evidence in 1–512 characters. An optional `sha256` is the evidence file's real
lowercase SHA256. Keep `summary` within 2000 characters. Omit this optional
argument when unused; never invent a material match or hash to satisfy it.

Publish supported texture names through `publish_shader_resource_names` as part
of the same delivery, even for a short request to organize the Shader. Exact
asset matches, observed role names and runtime-generated resources are distinct
outcomes. Keep unknown resources neutral; do not require every texture to match
an asset before publishing the established names. The final answer briefly says
which material/texture assets matched, which names were used, which roles were
inferred and which bindings remain uncertain, rather than listing tool history.

## Record the Agent's naming decisions

Keep one source-bound resource naming review alongside the source self-review.
It makes skipped investigation and missing display publication visible to the
task loop; the record does not infer meanings or certify the Agent's conclusions.
Use the optional `resource_naming_review` in `record_shader_organization_review`
for decisions already made during the whole-source pass. If publication changes
those facts after validation, update them with
`record_shader_resource_naming_review` for the current source hash; this is a
local metadata write and needs no repeated source review or compile.

The `review` object records:

- `assets`: `status` (`not-configured`, `matched`, `reused`, `partial`, `no-match`
  or `reader-unavailable`), concrete `evidence` and `usage`. State which associated
  assets or retained parameter dictionary actually informed the names. A
  `matched` or `reused` result also supplies `evidence_paths` pointing to existing
  workspace evidence, normally `["ai/asset-resolution.json"]`. Reuse the actual
  retained record; changed evidence must be reconsidered, not recertified from
  an old description. These files support traceability, not Host semantic approval.
  A configured directory cannot be called `not-configured`; a partial search cannot
  be described as an exhaustive no-match result. Reuse valid evidence without a
  fresh scan; missing reader support leaves that part explicitly unresolved.
  When an associated numeric parameter dictionary exists, also include
  `parameter_localization` with `status: "reviewed"` and `evidence` explaining
  which offsets were corroborated, which remain ambiguous and how they informed names.
  This refers to the current per-parameter observation saved by
  `propose_shader_names_from_asset`; reuse its current report. If the captured
  values/layout cannot be read, use `status: "unavailable"` with the concrete
  failure and continue supported source work. Unknown/unused/overridden parameters
  are valid investigated outcomes; do not fabricate an offset to complete a row.
- `textures`: one disposition for each current texture selector, with `selector`,
  `status` (`published`, `retained` or `unresolved`) and `evidence`. A published
  name includes its successful publication `operation_id`. Retain a current,
  appropriate existing name when justified; unresolved resources keep neutral
  names. A successful write for one binding does not account for other bindings.
- `reconciliation`: how the finished calculations informed the final CBuffer
  roles, channel groups and texture names, including remaining uncertain roles.
  Include the actual naming-quality decisions: which shared label text was
  unnecessary, which qualifiers/indices remained necessary, and how short-name
  collisions and user names were handled. Do not substitute publication counts
  or a statement that every field received a name for this judgment.

Use the returned inventory/selectors and tool schema, not selectors copied from
another Shader. CBuffer panel publication is accounted separately by
`bindingCoverage`; this review does not substitute for its actual write receipt.
Repair only missing effects reported by the current delivery. Do not issue one
review per field, make artificial name changes, or compile merely to refresh a
review. The final answer summarizes actual asset use and remaining uncertainty;
it must not equate a source-only validation receipt with the whole task being done.
