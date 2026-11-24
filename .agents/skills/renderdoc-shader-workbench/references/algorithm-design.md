# Reusable calculations and explanatory comments

Use this guidance for requested source organization. Improve the actual algorithm
representation and its explanation in the same working copy; this is not another
tool phase, a library migration or permission to change the captured computation.
Keep the language, ABI and numerical requirements described in the main Skill.
Use these criteria from the initial data-flow design through the full-scope edit.
Do not preserve a poor existing partition and postpone real restructuring,
mathematical simplification or comments until the final self-review.

## Choose the right reusable unit

Before choosing semantic module names, understand what this draw actually does.
Reuse the selected EID's available image, bound texture/mesh and pipeline evidence;
inspect useful missing views when they distinguish competing interpretations.
A face-like texture, a particular mesh region or a blend/depth/cull setup provides
a hypothesis, not proof of a lighting algorithm. Trace that hypothesis through
the Shader consumers and matched material/Shader assets. A full-screen or compute
pass needs resource dependencies rather than an invented mesh role. Keep this
targeted; do not export a whole draw or reconstruct a whole frame merely to name
a local calculation. Missing artistic meaning need not block describing known
operations neutrally.

For full reorganization, read each computation block and trace its inputs and
consumers before preserving or adding helpers. In the existing scope map, connect
the old block to its intended responsibility and result, the branch that owns it,
and any shared algorithm or result it should use. This exposes a large helper
that still mixes unrelated work and a tiny helper that extracted only the last
line of a duplicated algorithm. Reassign work accordingly; existing function
names and boundaries are clues, not the proposed design.

Read each caller as a table of contents, like opening a directory: its calls name
the meaningful parts of that operation. Opening each callee reveals the next
useful level of detail. For a lighting Shader containing these computations,
an illustrative responsibility hierarchy is:

```text
MainPS
  PrepareSurface
    DecodeMaterialTextures
    BuildSurfaceNormal
  EvaluateLighting
    EvaluateDirectLighting
      EvaluateDiffuse
      EvaluateSpecular
    EvaluateIndirectLighting
      EvaluateProbeLighting
      EvaluateEnvironmentReflection
  ComposeOutput
```

Derive the actual functions from the source. An unlit, compute or simple Shader
needs its own smaller design; this does not impose a fixed depth or lighting
template. Each parent must own and combine its children's useful results.
Shared mathematical helpers may be called from several branches, so the real
call graph can share nodes rather than being a strict tree. Preserve that reuse.
If a caller still enumerates unrelated leaf calculations, or a callee contains
several separable responsibilities, reorganize the actual definitions and calls.
A diagram-only grouping or one-line forwarding layer does not implement the
required architecture. Simplify the bodies while establishing these boundaries;
distributing unchanged scalar chains under a tree is incomplete organization.

For postprocessing, first identify the distinct transformations in this program
and the useful intermediate results, then subdivide each transformation only
where its calculations justify a further level. A pass that only reconstructs
a value and filters it needs no material/lighting tree. Conversely, a helper
that mixes coordinate setup, unrelated light contributions and final composition
usually hides several responsibilities. Mesh/texture/material evidence can
suggest those roles; confirm them in the calculations before choosing boundaries.
Neither splitting by line ranges nor bundling adjacent statements solely to
reduce the entry function implements semantic modules.

Write a separate overview of the Shader's meaningful rendering calculations during
the same full-source edit. Follow [semantic-overview.md](semantic-overview.md) for
granularity, source anchors, independent review evidence and publication. Describe
actual visual work in execution order: an entry-call list can hide independently
useful calculations, while a helper/comment list can fragment one purpose. Expose
major calculations with useful intermediate results or distinct contributions;
keep small supporting operations inside their owner. Judge transformations by
their actual role rather than automatically treating them as minor work. Do not
choose a rendering checklist, stage count or fixed function hierarchy.

Apply this granularity to each useful level of the function flow graph too. The
directory remains independent navigation: one function may contain several stages,
and one stage may span several functions. Preserve meaningful branch ownership
and shared helpers. Use concise titles for the purpose or useful result; keep
implementation details beside the code. Function definitions still need their own
brief purpose comments and nested functional organization.

Publish `semantic_overview` with `record_shader_organization_review`, bound to the
current source. If only the directory needs repair, use
`update_shader_semantic_overview` without recompiling or modifying organized code.
Move useful legacy `@overview`/`@stage` annotations into this metadata during the
next authorized source organization. Do not add generated header directories,
EID/provenance banners or validation chatter to the Shader.

During whole-source review, check both directions: each directory item leads to
its real calculation, and significant effects remain discoverable. Repair missing
major operations, repeated utility entries, verbose or outdated titles, wrong
order and dead anchors. A working function graph or valid old source review does
not replace this independent assessment. See the linked reference for the review
payload; copying function comments or graph nodes does not establish completion.

- Use a matching HLSL intrinsic directly for standard math. A new `MyLerp` or
  `MyDot` forwarding function usually adds no useful contract. A domain helper
  can be worthwhile when it expresses a meaningful operation with its own inputs,
  conventions or several steps, rather than merely hiding the intrinsic name.
- Find repeated multi-step calculations by their operations and data flow, not
  only matching text or function bodies. Include renamed, inlined and sibling
  branch copies; check whether an existing helper already implements them.
  Replace the equivalent instances together. Choose the whole useful algorithm
  boundary: extracting a final hue formula still leaves duplicated channel
  ordering and range calculations; moving only a final normalization can leave
  the same direction-adjustment sequence repeated at every caller. Differences
  in spaces, clamping, evaluation order or resources still need comparison.
- Parameterize real differences such as a scale, exponent or input vector. Keep
  different clamp placement, order-sensitive arithmetic, sampling behavior and
  branch ownership explicit. Factor a common core plus small variant-specific
  composition where that is clearer; avoid a universal helper with many unrelated
  switches or a different function for every renamed copy of the same algorithm.
- A coherent, nontrivial algorithm can deserve a helper even with one caller.
  Conversely, inline trivial glue that adds no meaningful boundary. Neither
  occurrence count nor function size decides extraction by itself.
- Give helpers narrow, explicit inputs and results. Name known spaces and roles
  consistently (for example normalWS versus normalTS). Prefer a value or cohesive
  result struct to a long list of scalar outputs. Group data by meaning, not into
  a bag containing every live temporary. Prefer pure calculation helpers with
  visible dependencies; make resource reads, derivatives or other effects clear
  when they belong in a helper. Preserve required entry/resource declarations.
- Keep helpers in the existing source unless the workspace supports and the task
  benefits from additional files. Reuse does not require new Unity headers,
  ShaderLab, macros, templates or a new include hierarchy.

## Code reuse and result reuse are different

One shared definition removes duplicated implementation. Trace repeated calls
and resource loads too: a common path may already compute a result that each
material branch computes again, or a channel expression may fetch the same record
once per component. When the inputs and evaluation conditions permit reuse,
compute the named result once in its existing owning scope and pass it to the
consumers. Check unchanged inputs, resource/index identity and possible writes;
matching names or formulas alone do not establish those facts. A read-only record
can be loaded once and used as a vector without rearranging its arithmetic.
This is local data-flow reuse, not persistent caching across pixels, draws or
frames, and source-level deduplication alone does not prove lower GPU cost.

For example, after confirming the helper is pure and both calls have identical
inputs and execution conditions, prefer:

```hlsl
float3 decodedNormal = DecodeSurfaceNormal(encodedNormal, normalScale);
float diffuseWeight = EvaluateDiffuseWeight(decodedNormal, lightDirection);
float specularWeight = EvaluateSpecularWeight(decodedNormal, halfDirection);
```

to decoding the same value separately inside both weight calculations. Do not
move an implicit-LOD texture sample or derivative across a branch to obtain this
shape, or move a calculation before a branch that previously excluded it. Keep
distinct results when their inputs, precision or evaluation conditions differ.
Do not introduce new epsilon guards, clamps, normalization or approximations just
to make a helper appear robust; preserve the original behavior and document its
actual assumptions.

## Expressions, state and layout

Choose the readable mathematical representation before laying out lines. Trace
related channels and their consumers across intervening statements: a matrix
expanded as all X steps, then all Y steps can still become vector operations.
For order-sensitive arithmetic, first simplify representation without changing
the per-component operation tree. `precise` does not require scalar variables.
For example, with the original four coefficient vectors and accumulation order:

```hlsl
// 先求齐次位置，再除以 W，得到片元位置。
precise float4 projected = fragment.x * coefficientsX;
projected = fragment.y * coefficientsY + projected;
projected = fragment.z * coefficientsZ + projected;
projected = (1.0f * translation) + projected;
precise float3 position = projected.xyz / projected.w;
```

Preserve the existing coefficient layout and carry the vector through internal
interfaces. Use `mul` when its matrix convention and numerical contract fit.
Likewise, factoring common terms or replacing blend expressions with `lerp`
can improve clarity, but real-number identities do not automatically preserve
floating-point association, contraction, exceptional values or branch behavior.
Inspect shared factors, repeated subexpressions, vector reductions and standard
blend, range and transform patterns explicitly. Prefer the applicable intrinsic
or one reusable mathematical operation when it states the computation clearly;
do not create a wrapper for each arithmetic operator. Associativity, commutativity
and distributivity are candidates to justify from the concrete operands and
required result, not blanket permission to reorder every floating-point formula.
When order must remain, combine channels and remove needless copies while keeping
that order; specify the local exception rather than rejecting all simplification.
Choose the appropriate form as part of the edit and validate the final candidate
once; a local ordering constraint is not a reason to abandon adjacent cleanup.
Do not combine "normalize each layer, then sum" into "sum, then normalize".

Treat locals by meaning and lifetime rather than by their number:

- Update a color, direction, position or accumulated result through its stages
  when it retains the same role and the prior value has no remaining consumer.
  Preserve the written operand order: `value = contribution + value` need not
  become `value += contribution` in an order-sensitive calculation.
- Trace declarations to their uses, including aliases hidden across helpers or
  in a large shared struct. Remove names that only relay an unchanged input,
  nested branch result or final field. Keep named weights, coverage, light
  contributions and other useful algorithm results even when used once; naming
  their role can make the formula easier to understand. Do not replace a useful
  stage with a dense expression merely to lower the temporary count.
- Keep distinct names for values with different roles, values reused later,
  branch-join state and useful intermediate algorithm results. Do not recycle
  one `temp` between coverage, roughness and color, or claim fewer source locals
  necessarily reduce GPU registers.
- Initialize a coherent result and assign its members in the owning branches
  instead of creating a parallel set of output locals solely to copy them at
  return. Avoid moving resource reads or derivative work across those branches.
- Break a large formula into its actual contributions (for example base light,
  directional modulation and a surface layer), combine corresponding RGB work,
  then compose those named values in the required order. A new name for every
  multiplication is as unhelpful as a deeply nested eleven-line assignment.

Format by meaning: one statement per line, spaces around binary operators,
consistent block indentation, and blank lines between algorithm stages. For a
long call, place coherent arguments on aligned continuation lines. For a long
formula, expose useful contributions before wrapping; do not densely hard-wrap
the same expression or hide it unchanged in another helper. Aim for comfortably
readable editor width (often 80–120 columns), not a line-count acceptance gate.
Remove parentheses only when precedence and the required evaluation tree remain
clear. Name important coefficients; shorten numeric spellings only when the
target precision has the identical value, rather than rounding by appearance.

## Explain the program at useful levels

Use the configured UI language for explanations unless the user requests another.
Provide enough source comments for someone reading the file without the chat:

Lead with plain, concrete language: say what the function does to its inputs and
what useful result it produces. For a complex or long function, follow that short
summary with a brief outline of the core algorithm and processing stages. Keep
the opening readable without first deciphering formulas or specialist vocabulary.
Place the opening summary directly above the function definition, not above a
preceding struct or a different helper. Every business calculation needs a useful
summary; small ABI adapters may share an accurate nearby section explanation.
Cover existing helpers and alternate branches too, not only functions created
this turn. For a complicated function, the short opening outline and detailed
internal notes serve different purposes and both are needed. Follow the user's
chosen language; Chinese comments should use everyday wording before technical
detail, with formulas and terminology explained where they become relevant.
Put detailed explanations inside the function beside the code they explain.
Describe the important statements or semantic blocks, not every assignment.

Use familiar verbs such as adjust, blend, choose and convert. Name the actual
value or result instead of vague phrases such as "process the data". Explain an
unfamiliar term briefly when it is needed; do not stack algorithm names as a
substitute for explaining their purpose. Formulas and precise technical terms
can support the explanation next to a difficult calculation. They should not
replace the plain-language summary or become a long glossary in every header.

| Location | Explain what a reader needs |
| --- | --- |
| File/entry overview | The main data path, major calculation stages and meaningful material/output branches. Keep a short orientation, not a transcript of tool calls. |
| Function opening | First, briefly say what this function does and what its result is for. For complex or long functions, add a short explanation of the core idea and stage order. Include input/result conventions only where needed to understand the call; do not put all implementation detail here. |
| Statements and algorithm stages inside the body | Beside the relevant code, explain what this step changes, why it is needed and how its result is used next. Clarify non-obvious formulas, known coordinate/color spaces, units, ranges or resource/derivative requirements where they matter. Comment semantic blocks together instead of narrating every assignment. |
| Branches and joins | What the observed selector changes, why variants or fallbacks differ, and which values are selected or carried through the join. |
| Non-obvious constants and order | The observed role of a coefficient, threshold, channel packing, clamp or denominator; why a particular ordering or evaluation scope must remain. Give a derivation when supported and useful. |

Keep known facts separate from inferred meaning. If a coefficient's artistic or
physical purpose is unknown, explain the observed scaling/mixing operation instead
of inventing its provenance. Use a recognized algorithm name only when the actual
formula supports it; a plausible highlight is not automatically a named BRDF.
Use a brief inference note where that distinction matters, not an uncertainty
disclaimer on every line. Missing original names do not prevent documenting the
known data flow or completing supported refactoring.

For the earlier vector blend example, this Simplified Chinese version illustrates
the separation between a short function summary and nearby implementation notes:

```hlsl
// 调整间接光的强度，再把它与当前光照混合。
float3 BlendIndirectLight(float3 currentLight, float3 indirectLight,
                          float intensity, float weight)
{
    // 按 intensity 缩放各颜色分量，得到准备混入的间接光。
    float3 targetLight = intensity * indirectLight;

    // 求出目标光照与当前光照相差多少，后面只加入这部分差值。
    float3 lightDelta = targetLight - currentLight;

    // weight 为 0 时保留当前光照，为 1 时得到目标光照。
    // 原式允许权重小于 0 或大于 1，结果也可能落在这两种光照之外。
    return weight * lightDelta + currentLight;
}
```

The comment explains a decision and the expression's behavior. By comparison,
`// Multiply and subtract` merely narrates syntax. Do not assert that these colors
are linear, that a vector is normalized or that a weight lies in [0, 1] unless the
source/evidence establishes it. Comments should not bury the code in a repeated
paragraph per assignment or conceal an otherwise unstructured calculation.

Name important constants and decoded parameters when their role is established,
while preserving original values and the external CBuffer/resource layout. Retain
a concise mapping to captured fields where it helps traceability. Do not round
constants, rename fixed ABI fields or add a safety clamp as a documentation edit.

After consolidating helpers, update their callers, names and comments together.
Check the explanation against the body: a path that still blends wet material
must not be described as dry, and a shared helper must not retain a one-variant
summary. Notes about preserving captured order explain constraints, not what
the algorithm does; provide the purpose as well. Confirm the real call graph
follows the resulting source. Reuse the existing whole-source review and final
validation; do not add comment quotas, a separate report, or a compile/check
cycle per extracted algorithm.
