## Readable implementation

Organize actual calculations from inputs/resources through branches to outputs.
Name observed roles and operations without claiming to recover original names.
Apply the requested simplification, hierarchy and comment requirements while
designing and writing the first full-scope edit; self-review verifies that work,
not a later opportunity to start it. For a broad reorganization request,
reconsider the existing function boundaries
as well as each body. Derive the target modules from data flow and responsibilities;
the current function list is coverage to inspect, not a design to preserve.
Retain a clear existing implementation when justified, but do not reduce a whole
program rewrite to naming, comments or a few easy extractions.
Organize the actual calls like a directory hierarchy: entry -> major operations
-> their sub-operations -> concrete algorithms, wherever the computation needs
those levels. Each caller coordinates its immediate responsibilities; opening a
callee reveals that responsibility's more detailed steps with clear inputs and
results. Implement this hierarchy in the first full-scope edit and inspect every
requested branch. Shared algorithms may have multiple callers; do not duplicate
them to force a strict tree. An entry listing unrelated leaf helpers, a renamed
monolith, a diagram without matching calls, or empty forwarding layers does not
satisfy the hierarchy requirement. This is layered function calling, not recursion
or function declarations nested inside another function. Group related geometry, material
or lighting values in internal structs; combine corresponding 2–4-channel arithmetic
into vectors and recognize suitable math idioms such as `dot`, `mul` and `lerp`.
Inspect common terms and applicable algebraic simplifications as well as naming;
choose reassociation or operand reordering only when the actual numerical and
evaluation contract supports it. Use a clearer order-preserving representation
for constrained calculations instead of leaving the surrounding scalar forest.
Trace interleaved channel calculations and expanded vector constructors too.
Keep meaningful stage values, reuse them when prior values are dead, and remove
alias/copy round trips. Split dense expressions into named contributions. See
source-operations.md for concrete vector and expression examples.
Choose the hierarchy and expressions from the actual algorithm, not a fixed
lighting template, call depth or shortest-line target. Unity
Shader style is a readability reference, not permission to change the captured
pipeline, bindings, precision or entry interface.

Follow [algorithm-design.md](references/algorithm-design.md) for reusable
algorithms, the independent calculation overview and explicit helper contracts.
The independent overview exposes meaningful rendering calculations in short, plain titles;
make independently explainable major calculations easy to find instead of merging
them under broad responsibilities. Keep small supporting calculations inside their
owner. Mesh, texture and draw-state evidence may suggest working hypotheses;
confirm or revise them from the actual source. Apply the same granularity to the
directory and function flow graph. Publish the overview separately from
source through the review's required `semantic_overview` field, with real source anchors
and independent whole-source analysis evidence. An entry-call list is provisional
navigation, not a completed directory. Follow [semantic-overview.md](references/semantic-overview.md)
for the review payload and metadata-only repair path; no technique or stage count is prescribed.
Do not add `@overview`/`@stage` metadata, provenance banners or a long directory
comment block to the Shader. Share equivalent code and reuse
computed results when their inputs and evaluation conditions permit.
Comments must explain intent, data conventions and branches without needing
the chat. Start each function with a plain-language purpose; outline a complex
algorithm briefly and place details beside the affected calculations. Distinguish
inferred meanings from observed operations.
Keep each summary immediately above its definition and update it with that body.

A forest of unexplained `Value###` scalars or scalar output parameters remains
a gap when it hides those roles; no variable, parameter or helper count decides
completion. Unknown original symbols do not prevent explaining known operations
or designing useful internal interfaces. Keep uncertain meanings neutral; name
observed operations, operands and branch results. Trace difficult phi/merge values
in existing source and baseline IR, preserve their assignments and evaluation
conditions, and organize them with the surrounding computation. A giant opaque
helper is still a gap, even after several easy functions were extracted.
Preserve required calculation order, control flow, derivatives and side effects
during refactoring. A specific order-sensitive operation may need its original
form; it does not justify retaining adjacent repetitive component arithmetic.
Use the user's requested comment language, otherwise the configured RenderDoc
UI language. For Simplified Chinese, write natural, concrete explanations of
purpose and flow rather than translating identifier names into a technical slogan.
Use plain comment text without Markdown italic markers or HTML formatting.
Say what the calculation does and why it matters; keep detailed numeric caveats
beside the affected code rather than repeating them in every function summary.

Choose source tools from [source-operations.md](references/source-operations.md),
including atomic helper/interface and caller changes. The configured-asset check
and targeted naming route above do not require finding every original name before
source organization can proceed.
