# Shader source operations

Choose tools for the current source and intended edit; they do not judge
readability or impose a tool sequence. Use exact decimal strings for 64-bit
ResourceId arguments, for example `"1000000000000000723"`.

Resolve current source by captured original bytes/API/stage/entry. Language,
catalog/toolchain and knowledge versions are not new logical Shader identities.
Preserve divergent or unsaved copies; follow the Host's canonical workspace.

For whole-Shader organization, obtain the complete source through the file route
below and work on it as one program. A new reorganization request requires a fresh
complete read and responsibility assessment of the current program; copying an
old review, touching a few helpers or reading only a diff does not satisfy it.
During that assessment, use the outline or bounded reads to locate
unfamiliar or changed paths. Keep reviewed unchanged calculations in working
memory; do not fetch the complete source repeatedly as many small range calls.
Begin/save also links a complete source-observation catalog in
`sourceReviewObservations.catalog.relativePath`. Read that JSON through the same
file tool for the initial design and final imported source; inline samples are
only a preview. It is a map of lexical candidates and calls, not another compiler
phase or proof that unlisted calculations are already organized.

| Need | Tool | Operation fact |
| --- | --- | --- |
| Read source or obtain a working file | `read_shader_workspace_file(mode=complete\|file\|range)` | Explicit complete text, a complete owned Agent-file copy, or a bounded range; coverage states what was delivered. |
| Find declarations and relationships | `get_shader_workspace_outline` | Lexical scopes, references and calls, not semantic meanings. |
| Rename an identifier | `rename_shader_workspace_symbols` | Scope-aware writes and skipped diagnostics; the Agent chooses meaning. |
| Refactor calculations/interfaces | `restructure_shader_workspace_source` | Mechanical extraction or bounded atomic rewrites with source anchors and concrete rejection diagnostics. |
| Start/resume a draft | `begin_shader_workspace_organization(agent_file_name=...)` | Session, baseline and proposal paths; an optional complete owned working copy avoids a separate checkout read. |
| Obtain a fresh reconstruction reference | `decompile_shader_workspace(reference_only=true)` | Uses captured baseline and fixed route; preserves current source/drafts and may reuse identical cached reference evidence. |
| Write proposed source | `write_shader_workspace_organization_file(kind=proposal)` | Import an owned `source_file`, apply exact `edits`, or replace `content`; proposal writes use the current hash to avoid stale overwrites. |
| Write explanation/diagram | `write_shader_workspace_organization_file` | `kind=analysis` or `kind=flowchart` writes the declared artifact path; after closure it binds the artifact to current saved Source without reopening acceptance. |
| Record Agent source self-review and calculation overview | `record_shader_organization_review` | Hash-bound source assessment including required independent `semantic_overview` and its review; see [semantic-overview.md](semantic-overview.md). No compilation or Host semantic scoring. |
| Inspect or repair only the calculation directory | `get_shader_semantic_overview_context`, `update_shader_semantic_overview` | Read current function locations/prior evidence, then publish independently analyzed stages. Preserve source, compile and replay results; do not treat the function catalog as a semantic directory. |
| Finish reviewed source and optionally apply/show it | `finish_shader_workspace_organization(show_in_workspace=true)` | Final validation requires a current complete source review. `apply_shared_resource=true` also applies and records the active project association. `publication_mode=checkpoint` only persists the proposal; no compile, validation or apply. |
| Show already-saved source | `show_shader_workspace_source(workspace_path)` | Shows existing source without saving, compiling or applying. |
| Compile saved source | `compile_shader_workspace` | Compiles saved source; an imported proposal is validated by its finish operation. |
| Register a source-only draft | `register_active_project_workspace(source_only_draft=true)` | Project source registration independent of compile/apply. |
| Read visible delivery | `get_shader_workspace_ui_state` | Workspace tree, actual editor identity and read-only state. |

Final finish publishes source, navigation and project association together.
Successful `replacementRegistration` needs no repeat. For publication failure,
use its exact registration recovery parameters without recompiling. The Host
completes legacy identity from verified original/workspace evidence. After a
capture restart use a current replacement receipt. Ordinary saves compile/apply
without repeating AI organization review or ABI validation.

Requested workspace display must pass the source-bound directory readback in
[semantic-overview.md](semantic-overview.md). Repair display without recompiling.

Rename/restructure may omit `operation_id`; the Host returns its `operationId`.
Rewrite ledger names are optional as well; return values identify the edit.
Retry the identical request with a known ID, or use
`get_shader_workspace_mutation_status` after a timeout, rather than starting a
duplicate mutation. Proposal edits still need an open draft and their source
guards; closed-session explanations carry separate source-binding evidence.

When native file editing is unavailable, `edits=[{old_text,new_text}, ...]` applies
ordinary text changes to the existing proposal with `expected_proposal_sha256`.
Choose exact unique anchors, including their surrounding context when needed.
The batch is ordered and atomic: every old text must match exactly once at its
step or none of the batch is saved. Change related helpers and callers together;
there is no per-helper compile or semantic rewrite gate. On an anchor/hash
conflict, read the affected current range and adjust the batch. Do not retransmit
the unchanged full program just to modify several functions. The receipt gives
the new hash and current review observations; all changes still receive the one
overall source review before final compilation and application.

## Internal interfaces

Mechanical extraction follows crossing variables and may emit scalar `out`
parameters. It does not design semantic data groups. Use an atomic rewrite for
related helper declarations, signatures and callers when an internal struct or
vector result makes the calculation easier to follow. For example, if the
source supports these roles:

```hlsl
struct SurfaceData { float3 normalWS; float3 albedo; float roughness; };
struct LightData { float3 directionWS; float3 color; };
struct LightingResult { float3 diffuse; float3 specular; };

LightingResult EvaluateLighting(SurfaceData surface, LightData light);
```

This is an interface illustration, not a required helper, algorithm or complete
Shader. Bundle values by role and compatible component meaning; keep unrelated
scalars separate. Preserve the captured entry signature, resources, CBuffer
layouts and ABI structs, adapting them to internal structs inside the Shader.
Read actual `availableCapabilities` and rejection details: bounded HLSL rewrite
accepts new or existing complete pure internal computation structs evolving
atomically with their helpers/callers, not arbitrary resource/ABI changes.
Larger changes can use the complete proposal-file writer; patch limits do not
choose the Agent's refactoring route.

For unnamed or branch-heavy code, infer interfaces from definitions, uses and
control-flow joins before assigning high-level material meanings. Keep phi
assignments under the same predecessor conditions and carry required values in
explicit results; do not hoist texture samples/derivatives across branches or
delete state merely because it looks repetitive. Use neutral operation names
when intent is unknown. Reuse already available baseline DXIL/IR or obtain an
independent reconstruction reference as described in
[reconstruction.md](reconstruction.md) when a dependency needs clarification.
Unknown author intent does not require leaving the entire calculation anonymous.

## Express calculations and their hierarchy

For organization requests, improve expressions as well as names. Corresponding
independent channel calculations with the same operands and operations usually
belong in a vector expression. Follow their consumers too, so vectors do not
immediately become another set of scalar aliases. Keep unlike channels, different
branch conditions or independent meanings separate.
This applies to two channels as well as RGB/XYZ. Check constructors as part of
the first body rewrite: `float3(s * v.x, s * v.y, s * v.z)` can express the same
component multiplications as `s * v` when `s` is a scalar and `v` a `float3`.
Keep the existing reciprocal-length calculation if required; this cleanup does
not depend on changing normalization or floating-point association.
Remove mechanical forwarding aliases, but retain readable named stages such as
diffuse, specular or rim contributions and reused results when the calculation
contains them; collapsing several meaningful stages into one huge expression is
not simplification. Judge readability from the computation, not a line-length limit.

For example, this remains repetitive after renaming:

```hlsl
float probeDeltaX = probeScale * indirectLight.x - baseLight.x;
float probeDeltaY = probeScale * indirectLight.y - baseLight.y;
float probeDeltaZ = probeScale * indirectLight.z - baseLight.z;
float mixedLightX = mixWeight * probeDeltaX + baseLight.x;
float mixedLightY = mixWeight * probeDeltaY + baseLight.y;
float mixedLightZ = mixWeight * probeDeltaZ + baseLight.z;
```

With corresponding `float3` inputs and consumers, express that calculation as:

```hlsl
float3 probeDelta = probeScale * indirectLight - baseLight;
float3 mixedLight = mixWeight * probeDelta + baseLight;
```

Two corresponding detail channels can similarly become:

```hlsl
float2 detailWeight = clamp(abs(detail.xy) * detailGain, 0.0f, 1.0f);
// 零值在原计算中选择正方向，因此这里保留这个边界行为。
float2 detailSign = float2(detail.x < 0.0f ? -1.0f : 1.0f,
                          detail.y < 0.0f ? -1.0f : 1.0f);
float2 shiftedDetail = detail.xy - detailSign * detailScale;
```

Here `detailScale` is the pair of corresponding axis scales, not two unrelated
parameters. Update downstream consumers to use these vectors. Do not replace
the sign selection blindly with `sign`, which gives a different result at zero.

This keeps the component operation structure. If the intermediate adds no meaning
or reuse, and interpolation fits the numeric contract, the same intent can be:

```hlsl
float3 mixedLight = lerp(baseLight, probeScale * indirectLight, mixWeight);
```

Prefer recognized expressions when their actual semantics match:

| Existing calculation | Useful expression | Detail to preserve |
| --- | --- | --- |
| Corresponding products summed as a projection or weighted value | `dot(a, b)` | Component set and any required accumulation order/precision. |
| A matrix transform expressed as row/column products | `mul(matrix, vector)` or `mul(vector, matrix)` | Observed row/column convention, component order and homogeneous coordinate. |
| An interpolation between two values | `lerp(a, b, t)` | Endpoint order and the original weight; do not add a clamp. |
| A threshold or a clamp to [0, 1] | `step(edge, value)` or `saturate(value)` | Strict versus inclusive comparison, operand types and relevant exceptional-value behavior. |

These are candidates for semantic simplification, not blind text replacements.
An intrinsic can affect floating-point ordering or contraction. When an actual
constraint requires an expanded form, retain that local form and name the
constraint; generic precision concerns do not justify leaving all channels
scalar. Assess these changes with the rest of the source and use the single
final compile/ABI/output validation, not a validation cycle per expression.

Design the call hierarchy around responsibility. For a shader that really has
these calculations, a useful structure might be:

```text
MainPS
  PrepareSurface
  PrepareLightingInputs
  EvaluateLighting
    EvaluateDirectLighting
      EvaluateDiffuse
      EvaluateSpecular
    EvaluateIndirectLighting
      BlendProbeLighting
  ComposeOutput
```

The entry expresses the major stages; each stage owns its implementation and
calls reusable lower-level calculations. A graph with all of these leaf functions
directly under `MainPS` usually means that responsibility has not been grouped.
Moving the same giant body into `EvaluateLighting` also leaves the problem intact.
Use actual data dependencies and branch ownership to choose boundaries; do not
invent diffuse/specular stages, force a depth, or add forwarding wrappers to make
a graph look nested. Inline trivial glue when a separate function adds no useful
contract, and share calculations only when their inputs, conditions and effects
are equivalent. This is source refactoring in the same working file; the diagram
should then follow the real final calls.

## Complete-file editing

When native or API file tools can read and write the owned working file under
the connection's effective permissions, prefer
`begin_shader_workspace_organization(agent_file_name="organized.hlsl")`
to obtain the session and complete current proposal in one call. Edit its
returned `agentFile` with the connection's file tools and import the result through
`write_shader_workspace_organization_file(source_file=..., kind=proposal)`.
Use the returned proposal hash as `expected_proposal_sha256` for that import.
This replaces repeated whole-file range reads and retransmission in write arguments.
Its `sourceReviewScope` supplies the initial source hash and function inventory.
Use that inventory with the whole-file read to keep a short scope map for all
calculation branches, including helper bodies and fallback paths; group related
functions rather than making a tool call or separate review for every function.
`read_shader_workspace_file(mode="file", agent_file_name=...)` also makes a complete
owned copy, returning metadata instead of duplicating all source in the response.
If file tools are unavailable, effectively read-only, or provide no runtime read
coverage, use Bread's source reader. During an organization its response is a
bounded inline page, including when `mode="complete"` was requested. Continue
with `mode="range", offset=<nextOffset>` until `nextOffset` is null; the offset is
characters, not bytes. Check actual coverage instead of the requested mode.
The whole supported file is up to 4 MiB. Keep the whole program in context and change the proposal with
ordered `edits` batches. Use `content` when replacing the whole program is useful.
Tool presence alone does not prove write access. Do not require an inaccessible
or unwritable local path from such a connection. `mode="range"`
remains useful for a targeted follow-up, not a compulsory organization workflow.
If native file access is denied, use supported Bread source tools. Exact draft
`edits` do not inherit the static rewrite tool's semantic or line-count limits;
bundle related declarations and callers in the same atomic text batch. The
scope-aware rename/extraction tools remain useful for their specialized operations.
Do not regenerate a several-hundred-KiB file for each rename or small compiler fix;
full-file transport is a fallback for changes that need it, not an editing loop.
Keep and reuse the working copy you just edited. Batch related changes and the
source self-review before one proposal import; do not reread or retransmit the
whole draft after each extraction. If another tool modifies the proposal, inspect
its returned change evidence or only the affected definitions and callers; obtain
a fresh full copy only when needed to resolve divergence or incomplete coverage.

Copying a file does not mean the Agent has read or reviewed it. Read/process the
actual program with available tools before assessing its complete requested scope.
The first proposal edit, import or source rename requires current-task read
coverage. A full read plus observed Host edits preserves this baseline for the
same task; it does not require rereading every unchanged function after each edit.
A new task/organization cannot inherit old read or review evidence. Record the
current complete assessment again before finalization, even if the bytes did
not need changes. Historical restoration and registration of already validated
work remain separate from a new organization request.
The copy grants no replay authority; final source/compile/apply checks still apply.
API filesystem tools use the returned `virtualPath`; the import tool accepts
the returned `relativePath`. Native file tools use the absolute `path`.
Coordinate parallel edits with independent copies or a reviewed merge; inspect
the combined definitions and callers before importing a shared working file.
Use current source anchors after edits. A static accepted rewrite still needs
the relevant compile/interface checks before replacement.

Perform source self-review and batch feasible corrections in that working file
before expensive finalization. After importing the reviewed bytes, record their
assessment using the import receipt's current hash. An earlier review describes
only the bytes it was bound to. See [self-review.md](self-review.md).

## Save and display

An operation receipt closes that source operation, not the Agent's requested
task. Review the saved path/interface gaps and keep repairing actionable ones.
Base a requested flowchart on final saved functions and calculations.
Complete the whole requested branch scope in the working copy, assess it, repair
findings together, then import and record the source-quality review. Use final
finish for the final candidate, not each intermediate helper. Writing/importing
the proposal already persists progress; use it like saving an IDE buffer.
Do not call finish/checkpoint, compile, ABI validation or replay apply to save
progress between extractions or naming batches. Complete the working copy,
review the whole requested scope and batch its repairs before final validation.
`publication_mode="checkpoint"` retains the organization ID and baseline while
persisting the proposal; it never compiles or validates baseline/proposal and
rejects validation, apply and live-edit flags.
Plain save needs neither compilation nor a review. A pending final review
response leaves the draft intact: address its findings instead of
reopening the session or submitting a false complete verdict.

Display returns the actual `workspaceId` and `readOnly`. Reusing a clean matching
canonical editor retains its editability; the editable source draft preserves
independent or unsaved documents. Display does not start live edit. Source-only
Ctrl+S saves retain pending validation; an already active live-edit document keeps
its compile/apply workflow. Read the returned save mode rather than assuming that
an editable buffer is an applied Shader.

Normal source editing requires no capture open/reload, replay replacement or
ownership recovery. The final apply's Host coordination handles an existing
replacement owner; do not preemptively search for owners or clear occupancy.
If final apply reports a concrete unresolved failure, use its specific recovery
diagnostic and preserve the reviewed draft. Reloading the RDC interrupts the
current capture-bound AI task and is not an ownership-recovery operation.

For a failed output comparison, use [numeric-recovery.md](numeric-recovery.md)
to localize the difference in the same draft before retrying finalization.

## Function execution overview

Keep the source's responsibility hierarchy: entry, major operations, sub-operations
and reusable algorithms. Inside a selected function, its overview follows the
execution of its main calls: one step connects to the next statement's step.
Retain conditional alternatives, loops and exits; do not connect exclusive
branches as if both always execute. Calls in a shared expression may be grouped
when their evaluation order is not established. Opening a module reveals its
own steps; navigating a step locates the corresponding source. Avoid showing
every temporary assignment or inventing extra wrapper functions for the diagram.
# Explanation links

For code explanations, show relevant excerpts in fenced blocks with the actual
language, such as `hlsl` or `glsl`, and link useful stages to their source locations:
`rdoc://shader/<workspaceId>?line=<one-based-line>&column=<one-based-column>&file=source/shader.hlsl`.
Use the workspace identity and location returned by source tools; encode the
relative path when needed. The `file` parameter is optional for the main source.
Link current saved code, not an unrelated Shader with the same function name;
describe a draft excerpt as a draft when it differs.

