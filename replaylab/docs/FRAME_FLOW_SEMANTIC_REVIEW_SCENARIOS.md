# Frame-flow semantic review scenarios

Use this compact matrix when changing the frame-analysis Skill or reviewing a
completed diagram. It evaluates whether the explanation follows the supplied
evidence, not whether particular words, techniques, EIDs or stage counts appear.
The observations are hypothetical and deliberately independent of a game/API.

For each case, read the current `renderdoc-explain-event` Skill and its
`references/render-path-review.md`, then assess the draft using only the stated
observations. Record the concrete finding, the next useful observation or edit,
and whether the changed explanation needs another review. Do not open a capture,
call a paid Agent, or regenerate a user's diagram to run this document review.

| Case | Available observations and draft | Expected review decision |
| --- | --- | --- |
| Forward-only scene | Geometry Shaders evaluate material and illumination and write scene color; later operations only transform that image. No separate object branch or terrain system is evidenced. | Accept a supported forward-path explanation. Do not require a GBuffer, deferred lighting, character, terrain or other absent module. |
| Mixed object paths | One geometry group stores surface properties consumed by a later lighting calculation. A different group evaluates material and lighting while drawing into an independent target, which is subsequently blended into scene color. The draft calls the latter group “extra color.” | Explain the distinct shading paths and their actual integration. Investigate object identity only if it materially changes the explanation; do not assume a character from timing alone. Repair the vague visible label and recheck the overall composition. |
| Important finding hidden internally | Source and resource observations establish deferred lighting, and the internal algorithm record names it. The visible card only says “local color updates.” | Bring the supported role into the visible title and concise explanation. Reuse the existing source/binding evidence; a new GPU read is unnecessary merely to revise presentation. |
| Bound resources without understood purpose | A broad postprocessing range has several materially different Shader operations and target changes. The draft only states that an SRV is sampled and a smaller RTV is written. Action coverage and publication checks pass. | Keep the semantic finding open. Read the relevant calculations and consumers to distinguish the functions; then group by actual purpose. Complete coverage and successful publication do not explain the algorithm. |
| Small but distinct contribution | Before/after thumbnails look almost identical, but geometry/state/source evidence establishes a localized rendering branch whose result is composed later. | Retain the meaningful branch rather than merging it solely because thumbnails look similar. Use a matched comparison or useful crop when it clarifies the contribution; do not require a fixed image quota. |
| Dynamic read unavailable | A current dynamic query fails, while valid capture-bound source and exact binding observations still explain a module. One separate claim requires a dynamic fact that is unavailable. | Continue the supported static explanation and other actionable modules. Use a genuinely different evidence/recovery route for the remaining claim; isolate a real limitation without guessing or repeating the same failed state. |
| Unchanged but reassessed conclusion | A redraw request leads to a fresh assessment of scope, boundaries and key dependencies. The prior interpretation remains supported by valid observations. | Retain that interpretation with concise renewed reasoning. Do not rename stages, change their count or recapture unchanged images to manufacture novelty. |
| Capture or game changed | The new capture reuses some numeric IDs and familiar resource names, but its Shader identities, bindings and execution order differ from the previous capture. | Establish the new capture identity and derive its own interpretation. Do not reuse old EID roles, object categories, saved-completion receipts or diagram boundaries merely because names/IDs match. |
| Graph without real handoffs | Several cards use the same final texture under different operation labels. No observation establishes that one node's output is consumed by another. | Remove the invented chain or investigate the actual intermediates. Keep a texture graph only for supported transformations, branches or merges; parallel MRTs and repeated screenshots are not a dependency chain. |

## Review record

For each changed Skill revision, record which cases were examined and any
substantive ambiguity found. A satisfactory result explains why the proposed
decision follows from the observations and does not add missing facts. If a
finding changes the draft, reassess that correction and its affected context.

These cases are a reasoning aid, not a runtime gate, technique checklist or
keyword scorer. Offline unit tests can verify capture isolation, receipt
freshness, reference loading and data-flow contracts. They cannot prove that a
future model will correctly identify every rendering function or produce a
complete semantic explanation; that requires reviewing its actual evidence and
delivered diagram.
