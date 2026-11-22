# Source routes and replay effects

## Choose and preserve source

Select the route from the current capture API, original encoding, stage/entry,
available source and installed compiler capabilities, including target version,
extensions, bindings and layout options. Re-evaluate these facts when the RDC or
game changes; a previously successful D3D route does not establish a Vulkan route.
Use compatible native source and direct compilation when available. Rebuilding a
source ABI adapter is only a repair for a demonstrated interface mismatch, not
a universal prerequisite for decompilation, organization or replacement. Keep
physical interface validation distinct from reconstructing interface code: a
direct route still needs the actual bound program, matching interfaces and output
checks before a completed replay replacement can be reported.

HLSL is the common authoring language when the detected route can preserve the
captured interface. Supported Vulkan stages use SPIRV-Cross HLSL reconstruction
and the installed SPIR-V-capable compiler, retaining descriptor sets, bindings,
locations, buffer layouts and position conventions. Do not assume an installed
DXC executable includes its optional SPIR-V backend. Existing saved GLSL can use
`convert_shader_workspace_to_hlsl(source_workspace_path, expected_source_sha256)`
to create a separate validated HLSL workspace without rewriting the old source.
Read its compile and ABI receipts; output equivalence and actual application are
separate steps. Keep the saved working route when unsupported features prevent
conversion, and explain the specific missing route rather than reporting a
successful universal language migration. OpenGL native uniforms and coordinates
need their own verified conversion; a working Vulkan route does not prove them.

An OpenGL capture whose recorded encoding is GLSL already has native source.
The workspace seeds that exact captured source as `captured-native-source`;
`baseline.kind=GLSL` and `targetProfile.profile=glsl` identify this route. Read and
edit it directly, without decompiling it into another language or adding an ABI
wrapper. RenderDoc's current OpenGL driver compiles the saved GLSL, then fresh
reflection, output comparison and checked replacement validate the result.
The persistent candidate has `kind=GLSL`: its bytes and hash describe source,
not SPIR-V or machine code. Restoring it rebuilds the target and checks its actual
interface again. An OpenGL SPIR-V baseline can also use a native GLSL candidate
when the current source and captured interface are compatible.

On failure, distinguish compilation diagnostics, incomplete/stale reflection,
numeric differences and display metadata. Repair the failed layer and reuse
unchanged source/evidence. For missing baseline interface metadata, retry the
supported fresh reflection/validation route before changing valid source; native
struct arrays and platform-specific layout conventions are not evidence that the
program is incompatible. Choose an alternative registered compiler/decompiler
only when its inputs and target contract fit the diagnostic. Do not make a failed
route pass by disabling validation, switching API semantics or applying guessed
buffer layouts. Keep unsupported capabilities explicit after actual recovery.

When the native compiler fails, or its candidate has a reproducible numerical
mismatch, inspect the workspace's actual routes. If a compatible file-compiler
route is registered, `compile_shader_workspace(prefer_native=false)` can select
it for one diagnostic attempt on the same source.
Use the actual error and available target support to choose that attempt; do not
alternate unchanged routes indefinitely. It still needs the captured interface
and output checks before application. A route change neither requires inventing
an ABI wrapper nor relaxes those checks.
The captured-GLSL native route has no implied SPIR-V fallback: diagnose its
driver messages, exact source, entry point, includes and current capture first.
Preserve organized source while repairing that specific issue. An unresolved
include is not a standalone compiled source unit, and a saved source artifact
alone is not compile, ABI or output-equivalence evidence.

`get_active_shader_workspace_path(event_id, stage)` returns the target's managed
workspace without a full-frame scan. `prepare_shader_workspace(...,
admit_existing=true)` binds the captured Shader and reports current source,
format and available routes; it does not decompile by default. Inspect these
facts and choose between improving useful source and reconstructing from the
immutable captured bytes. Source length or missing original names alone is not
a reason to restart.

Keep original bytes, decompiler evidence and user source distinct. Preserve
useful edits when repairing managed metadata. A fresh decompile/recovery should
start as an independent proposal/workspace, then be compared and promoted when
appropriate, not overwrite the user's current source. `decompile_shader_workspace`
may reuse a pristine program-produced result; it refuses to overwrite edited
source and does not turn that source into fresh original evidence.
Its ordinary delivery validates the reconstructed source for the captured
target by default, without permanently applying it unless requested. Resolve
reported source/include/interface gaps and retry the affected final validation;
the delivery receipt distinguishes saved text, compilation, ABI, output
comparison and application. An explicit `validate_for_apply=false` is draft-only,
not evidence that a generated workspace Shader is ready for replacement.
After editing a reconstructed source to repair these gaps, use
`validate_shader_workspace_delivery` to validate the current bytes, with
`apply_shared_resource=true` when replacement is requested. It reuses unchanged
compile/ABI proof and does not decompile again or overwrite repaired source.
Its `reference_only=true` mode obtains a separate decompiled reference from the
workspace's captured baseline through the fixed current route while retaining
canonical source and organization drafts. Reuse a matching cached reference;
cache reuse is not a newly executed decompile. Read the returned reference path
with `read_shader_workspace_file`, inspect it before adoption, and import chosen
changes through the ordinary proposal writer. A reference is neither a saved
rewrite of canonical source nor compile/apply evidence. It is raw reconstruction:
no reverse-compile probe or source ABI repair has been performed. Preserve the
correct captured entry/resource interface when incorporating useful computation
into the proposal; do not blindly replace working interface declarations.
`try_shader_workspace_recovery_candidate` and
`get_shader_workspace_representation` expose supported alternatives.
Route diagnostics and `routeMemory` describe what happened, not an instruction
to repeat the same failed route. Check the returned source format: explanatory
IR or binary evidence is not an executable editable Shader.

## Recover evidence before declaring an organization blocked

No embedded HLSL, anonymous locals or a complex phi/branch merge is a reason to
look at available evidence, not proof that the remaining program cannot be
organized. Decide what is actually missing: original author names, a particular
control/data dependency, missing source bytes, or a failing tool capability.
Only the latter concrete gaps require recovery; do not invent original names or
claim reconstructed source is original source.

- Keep the complete current HLSL in the Agent working file. Use exact binding
  facts already obtained; if missing, `inspect_shader(event_id, stage)` returns
  `workspaceIdentity` for captured-original routing and `shader` for the effective
  Shader. During replacement those hashes may differ.
- `get_shader_workspace_status` exposes available `assemblyDisassemblyArtifacts`.
  For DXIL select `role=baseline` for the captured program and read its returned
  text path with `read_shader_workspace_file(mode="file"|"complete")`. Read the
  whole useful evidence once, then only relevant dependencies. A candidate
  disassembly describes the replacement, not the captured original. If a path is
  absolute, derive `relative_path` under the returned workspace root.
- If a baseline representation is unavailable, use its known tool contract;
  discover that specific contract only when it is missing from context.
  `get_shader_workspace_representation(workspace_path, event_id, stage)` can
  materialize identity-bound explanatory IR for a source-empty workspace, but
  prefers existing executable source when present. Do not repeatedly call it
  expecting DXIL while it keeps returning the same HLSL. For current-event
  evidence, `get_current_shader_representation` is available; check its reported
  identity before treating it as captured-baseline evidence.
- When current HLSL appears incomplete, damaged, or unsuitable for a useful
  structural refactor, `decompile_shader_workspace(reference_only=true)` can
  obtain an independent same-baseline reference without overwriting useful work.
  Compare relevant coverage and difficult control flow once. The same baseline
  and unchanged route normally produce the same structure; an identical or
  unhelpful reference means proceed from the better existing source, not restart
  decompilation. Revisit the route only for changed inputs/tools or a concrete
  diagnostic that makes another registered route relevant.
- `try_shader_workspace_recovery_candidate` tries one alternative from the
  returned `recoveryPlan.decompileCandidates` and `compileCandidates`, in a new
  sibling workspace under the project's Shader root. Use the returned plan,
  source-workspace, baseline and capture identities. Do not repeat an attempted
  pair or chain recovery workspaces; do not exhaust all candidates when evidence
  already suffices for organization. If an alternate representation does not
  improve the target path, preserve it as evidence and continue the source work.

With calculations available, organize them by demonstrated operations and data
flow even when their artistic meaning is uncertain. Keep exact branch
conditions, phi/merge assignments, evaluation order, resource accesses and
side effects. Introduce coherent inputs/results and neutral operation names;
retain a necessary branch-carried intermediate with an explanation instead of
guessing its semantic role or deleting it. Trace every requested path to outputs;
an untouched anonymous core hidden behind a newly named wrapper is incomplete.
See [source-operations.md](source-operations.md) for complete-file editing and
[self-review.md](self-review.md) for the actual quality assessment.

Before `blocked`, name the specific unavailable artifact, unsupported construct
or failing operation, its affected computation, and the recovery actually tried.
If the remaining uncertainty can be preserved in a well-structured implementation,
continue. Lack of original symbols or an all-input equivalence proof is not, by
itself, a blocker to a behavior-preserving structural refactor. Do not replace a
real unresolved control/data dependency with a guess or label unreadable output
complete merely because the available routes were tried.

## Repair managed state

`toolchainStatus="refresh-required"` denotes a stale route binding, not bad
source. `migrate_shader_workspace_toolchain` can refresh it while retaining
source; use its source/digest checks and supported lease recovery rather than
editing lock files manually. Internal session IDs and cached EID hints can be
rebound from the actual RDC/original Shader binding. A different EID can use the
same original Shader; differing bytes or real binding require target inspection.

`repair_shader_workspace_source_abi` performs hash-bound source repair. Compiler
and ABI diagnostics identify concrete signature, resource or layout mismatches.
Pending reflection is not a compatible verdict. Decide the source repair from
those facts, preserving supported calculations. This is recovery from actual
validation failure, not a prerequisite or periodic check during ordinary editing.

## Independent delivery effects

`finish_shader_workspace_organization` final delivery validates by default;
an explicit draft-only save or checkpoint does not certify a completed result.
`show_in_workspace=true` or `show_shader_workspace_source` shows saved source
without requiring compile proof or creating live-edit authority. A clean matching
canonical editor is reused with its current editability; otherwise an editable
working document preserves other documents and unsaved edits. Use returned `workspaceId`
and `readOnly` for `get_shader_workspace_ui_state`; it may differ from
`sourceWorkspaceId`. Showing a completed result needs no new draft or rewrite.

`register_active_project_workspace(source_only_draft=true)` registers current
source independently of compilation. Default registration requires a current
ABI-validated candidate. Neither a registered record nor editor eligibility
alone proves an open writable document. Requested writable Ctrl+S editing uses
authorized live edit with automatic compile/apply on changed saves; do not enable
that behavior for a source-only request merely to change preview editability.

## Replacement and reuse

For a missing application/compiler capability, use the shared
`replaylab-core` capability-recovery reference and `get_shader_repair_context`.
The installed route list is not an assertion that another adapter cannot work.

Missing reflection is a repairable evidence gap, not proof that compilation or
replacement is unsupported. Candidate validation automatically reflects the
immutable original when a legacy baseline is unavailable, retaining edited
source and compiled candidates. Stripped SM4/5 DXBC can prove CBuffer registers,
spaces and byte capacities from SHDR/SHEX declarations even without RDEF; those
blocks are explicitly opaque, not invented float4 source members. Keep exact
typed layout checks where original type annotations exist. Diagnose a concrete
binding/size/signature difference separately from missing annotations. Reuse a
successful current recovery; do not repeatedly export catalogs, recreate the
workspace or undo texture aliases to repair the same reflection gap. Valid UUID
or stable mutation-label operation IDs are normalized by shared-resource apply;
reuse the same caller ID after an uncertain response.

`compile_shader_workspace` creates a candidate.
`validate_shader_workspace_candidate` can diagnose it before apply.
`apply_shader_workspace_shared_resource` checks the current target and candidate
interface, reusing supported unchanged validation when available.
`finish_shader_workspace_organization(apply_shared_resource=true)` validates
the saved bytes and applies in one operation, including supported captured-output
comparison; disabling a validation flag cannot bypass that requirement.
Default final delivery performs this validation without applying unless requested.
Read source-save, validation, apply and
display sub-results independently; ABI compatibility does not prove behavior.
Final validation/apply requires a current complete source-quality self-review.
`publication_mode="checkpoint"` retains the same editable draft and baseline
without compilation, validation or apply; writing/importing already saves progress.
Do not repeat finalization for each helper/branch or use physical success as a
substitute for repairing remaining source-quality findings.

Project restore caches successful original/candidate pairs individually; an
unrelated failed item does not invalidate another proven pair. Reuse matching
source/candidate/profile proof in the same RDC instead of repeating compile or
ABI calls. A current original Shader binding check and actual replacement are
still needed. EID hints and management revisions alone neither establish nor
invalidate that physical match.

Shared-resource apply affects checked uses of the same original Shader, possibly
at multiple EIDs. A request confined to one event needs a supported matching
scope. Persist the final applied source/candidate and RDC association through
`register_active_project_shader_replacement` when an exact persisted association
is not already confirmed. Use its receipt; read back only missing or uncertain
state. Do not repeat registration or application for an unchanged confirmed pair.
Project source reuse is not, by itself, authorization to replace a current
Shader or enable future automatic apply.
