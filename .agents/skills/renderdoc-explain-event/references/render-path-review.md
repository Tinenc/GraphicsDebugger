# Understand and review the rendering path

Use this reasoning during module discovery and again on the actual final diagram.
It is not a required technique list, tool sequence, EID set or stage count. Derive
the relevant questions from this capture, including when the game or API changes.
Do not copy another frame's object categories, algorithms or pass order.

## Find where surfaces become lit color

Follow how geometry/material data reaches the final scene. Determine where surface
properties are evaluated and where illumination is applied. Describe the actual representation and the operations that consume it. Derive
technical names only after examining those calculations and their integration.
Do not force a rendering-path label from target count, geometry, names or a
familiar appearance. Different operations can coexist in the same capture.

Follow the distinct object/material branches actually present. Ask what geometry
they draw, where their surface color and lighting are evaluated, what controls
visibility, and how the contribution reaches scene color. Scene, character and
terrain are possible subjects, not required nodes. A frame with only forward-lit
geometry and no character or terrain needs none of those invented branches.
Conversely, an object shaded by a different path must not disappear inside a
generic scene-color range merely because the destination is shared.

Use the smallest observation that resolves the meaningful doubt. Mesh shape,
instances, vertex deformation, linked material/Shader assets and visible changes
can identify the subject; depth/stencil, culling, blending and write masks explain
its participation. For example, an expanded silhouette together with the relevant
vertex offset and face/depth state can distinguish an outline from a repeated
material draw. Neither a dark contour nor reversed culling alone proves it.
Use the available asset index when material or geometry identity is ambiguous,
following distinctive resources and dependencies rather than a guessed game path.
Match observations to the captured permutation and platform.

## Explain contributions rather than API actions

Trace important lighting inputs to what produces and uses them. Distinguish a
visibility factor from illumination, material color from already lit color, and
retained history from current-frame work by their actual calculation and use.
If separate direct, indirect or shadow contributions are evidenced, explain how
they influence the relevant surfaces and where they combine. If they are not,
do not require named subsystems to appear. An engine label is a lead, not proof
that its usual implementation or every advertised feature ran in this capture.

Similarly, follow scene-color transformations to the presented image. Identify
what each meaningful operation changes and why its intermediates are needed;
"writes a color target" proves an output binding, not the filter or rendering
purpose. Investigate materially different Shader/state/resource transitions
inside a broad postprocessing or extra-color range. Combine repeated operations
with one purpose; separate functions whose computations or contributions differ.
Terrain, tiled data, special geometry or independent effect layers warrant their
own explanation only when their observed processing is significant to this frame.

For a multi-draw function, trace its result backward to real producers and forward
to integration. Use a compact texture graph for meaningful cross-target handoffs,
branches and merges. Single-draw arithmetic, unrelated MRT outputs or repeated
copies of the same final image do not establish such a graph. A small but important
composition change can use a matched before/after divider. Select representative
media to explain the operation, not to decorate every edge with a duplicate.

Keep compact observations in the existing investigation/evidence records: what
the binding/operation proves, the supported functional interpretation, and any
consequential open question. Binding correctness and semantic understanding are
different claims. Reuse matching observations and read-only source/IR. Prepare
managed source or a temporary compiled variant when it helps distinguish the
remaining hypotheses; choose the method from the question and evidence.

## Review the explanation the user will actually see

After constructing or revising the diagram, read its displayed titles, summaries,
selected images and texture graphs as a complete explanation. Compare that story
against the execution outline and the investigated important resource paths.
Reverse-trace the presented scene through its major contributions, then check the
forward order. This can reveal an omitted branch or a broad label hiding several
different functions; it is not a request to query every pixel or run every tool.

Look for important contributions that the current explanation cannot account for,
not missing technique names. Check whether their sources and integration are
explained, whether broad stages hide different calculations, and whether the
observed branches contradict the proposed rendering path. Follow those gaps back
to evidence and revise the explanation. When several roles are integrated into
one draw, explain them within that stage instead of inventing separate nodes.
Use a concise overview to orient the reader to the observed pipeline and its
important exceptions; leave the investigation notes and discarded hypotheses out
of the main diagram. An unobserved example technology needs no placeholder node.

The reader should be able to locate supported conclusions about where material
becomes lit color, which distinct subjects take different paths, and how important
contributions join the scene. If the investigation has established deferred
lighting but the main card only says "extra color", bring the supported role into
the visible title and concise explanation. A correct algorithm buried in internal
records does not repair an unclear diagram. Name specific operations when known;
do not claim a more specific technique than the calculations support.

Judge unclear stages by whether further available evidence can distinguish their
purpose. A generic label, a changed thumbnail or a Shader entry name is not enough
when the core calculation remains unexamined. Conversely, absence of an example
technique or an exact asset name is not a defect in an otherwise explained frame.
Do not invent missing modules, force a forward/deferred classification for every
utility operation, or turn this guidance into a keyword score or fixed checklist.

Record concrete findings with the existing task review: the affected conclusion
or visible stage, evidence supporting or contradicting it, and the smallest useful
follow-up. Coverage, publication receipts and gapCount=0 do not close semantic
findings. Investigate feasible omissions, repair grouping/names/media and review
the changed explanation plus its place in the overall path again. There is no
fixed number of rounds. Reuse valid unchanged evidence; revalidate changed claims
and resource relationships rather than merely recording a new satisfied verdict.

An actual evidence boundary may remain, but a first read error or an unexamined
alternative is not that boundary. Continue other actionable modules while using
supported recovery/alternate routes. Never guess to make an all-clear report.
Keep unresolved limitations in the investigation/review and summarize material ones
once. The final reply should briefly say what was understood or improved, whether
the result was saved, and any real remaining limitation; omit protocol fields,
tool counts and retry history from normal user-facing completion text.
