# CBuffer semantic naming and display audit

## Observed task and live target

The latest recorded organization task was `8bb831a4d49b4066bbc97f842533555c`
(2026-10-20 16:44–17:03 local time). It made 113 MCP calls. It read the naming
Skill but did not call asset candidate retrieval, parameter-name proposal,
asset resolution submission, resource-name publication or CBuffer-name
publication. Reading the Skill and retained asset context did not apply names.

The workspace retained six texture content anchors, two Hair01 material
candidates and 72 material parameters. The record explicitly said
`shader-context-only`, `drawMaterialAssociationConfirmed=false`,
`sourceNamesApplied=false`, `panelAliasesApplied=false`. Its parameter locations
and Agent hypotheses were empty. This was useful material context, not a
completed physical parameter mapping or panel publication.

The live target was confirmed again before changes: `1.rdc`, EID 61408,
fragment `MainPS`, D3D12. The organized source SHA-256 was
`4e89716dfefb1670c64749a936564748a3727d4ba6e21dfe9416ab9844ed2f9f`;
its active compiled Shader was
`283ec003242ad2d01fe8b93567f759e4f1f0cc403a8de04e8cae0581d431e755`.
The configured unpacked root and retained index were available. No full
asset scan or Shader recompilation was needed for this display change.

## Causes and implementation

1. The workflow treated panel aliases as an optional separate effect and lacked
   an explicit post-organization pass over CBuffer consumers. Skills and Agent
   review guidance now separate asset-context reuse, whole-source organization,
   final semantic reconciliation and actual publication receipts.
2. Existing aliases renamed reflected rows. They could neither merge scalar
   rows nor separate unrelated parameters packed into a float4. The existing
   `publish_cbuffer_aliases` tool now accepts `semantic_fields_json` with exact
   component offsets, scalar type, provenance, interpretation status and source
   hash. The viewer projects already decoded values into semantic rows.
3. Publication used the original Shader layout, while the panel displayed the
   replacement Shader's different reflected layout. Publication also writes an
   explicitly identified active-layout view when a replacement is present;
   bindings and exact Shader bytes scope both representations. It does not
   search unrelated Shader files or infer identity from a reused ResourceId.
4. Live publication exposed an additional stripped-DXIL case: CB5 originally
   reported 13 duplicate `unknown` members at offset zero and placeholder uint
   rows. Its active validated replacement exposes 217 unambiguous float leaves.
   Treating the placeholder row types as recovered parameter types rejected a
   useful display view at offset zero. Such views use the identity-checked
   active reflection, while retaining the original Shader identity. Typed
   captures still cannot be reinterpreted arbitrarily.

The semantic view is display-only. It never repacks bytes, changes register
bindings, rewrites reflection, changes a scalar type or becomes a material-edit
target. Unknown/unassigned channels remain visible. User aliases retain
priority. A vector contains its actual channels: RGB plus an unrelated
roughness value is a float3 and a float, not a fabricated RGBA value.

The UI provides status/source columns, original channels and byte offsets in
tooltips, and a physical-view toggle. Inferred means inferred; it does not claim
to recover the game's original parameter symbol. Pure display publication does
not invalidate the compiled Shader or trigger another compile/ABI comparison.
The viewer watches the scoped publication file, debounces atomic replacements,
and rebuilds semantic rows from its cached decoded values. Refreshing names
does not request another frame replay or wait for the publisher's file lock.

## Current-target interpretation

A reviewed display plan for CB5 was published with 57 fields covering 90 physical lanes.
Sixteen use both retained material parameters and observed source consumers.
Examples include Normal Intensity, Wet Roughness, Kajiya colors, Drop
Tilling&Offset, and PowerOutlineColor. Remaining named fields describe observed
rim-light, independent-effect, dissolve and output-color operations. These
interpretations remain marked inferred because the retained material candidates
do not prove one exact current draw/material association.

Examples of meaningful grouping are `rimPrimaryDirection` at c51.xyz,
`powerOutlineColor` at c55.yzw and `outputColorOverride` at c61.xyz. c51.w,
c55.x and c61.w remain separate controls. The plan preserves all other physical
channels rather than assigning unsupported names.

The live publication reported 57 requested/written semantic fields and 57
requested/written current-display fields, zero skipped rows and `partial=false`.
The stripped original layout used the verified active-reflection path described
above. These are persisted-view counts, not a claim that every original material
parameter has been recovered. The Shader source and compiled binary did not change.

## Validation scope

Regression coverage exercises scalar merging, packed-vector splitting,
non-contiguous components, type/offset conflicts, user alias priority, Shader
scope isolation, persistence, atomic publication and active-layout mirroring.
The naming-preparation contract remains bounded and performs no replay read,
asset scan or compile. Agent tool-output summaries retain publication counts
and partial/mirror failures even when detailed evidence is spilled to disk.

The installed native tests passed 120 assertions in five CBuffer test cases.
The application reopened the project with four Shader replacements restored,
four editable workspaces and no restore warnings or failures. The frozen runtime
was checked against 47 Python modules, 15 Skill files and 20 Bridge modules.
The final installed 1.13.43 MCP replayed the publication operation idempotently
with all 57 current-display fields accounted for. Its live workspace receipt
confirmed the target source hash was unchanged and the document was editable.

This change improves naming delivery and display. It does not certify every
material name or automatically identify every proprietary engine's packed
parameters; unresolved roles remain explicit and can use later asset/data-flow
evidence without preventing completion of the Shader organization itself.
