# Independent Shader calculation directory

For an organization request, read the complete current source and understand its
calculation paths before authoring the directory. Mesh shape/attributes, texture
appearance and channels, material names and draw state can first suggest working
hypotheses about what is being rendered. Check those hypotheses against actual
resource reads, expressions, branches and consumers; revise them when the code
disagrees. Visual resemblance or a familiar resource name does not establish an
algorithm. Titles should expose the useful
rendering calculations a reader would want to find. The entry's orchestration
calls may be too coarse; a list of helper names or comments may be too fragmented.
Choose boundaries from actual expressions and dependencies. Several calculations
may live inside one function, and one calculation may span several functions.
Keep small supporting operations inside their owning stage. No fixed technique,
EID, stage count or required function shape applies.

Favor a little more useful calculation detail when a broad stage contains several
major operations that can each be explained independently. Look for a coherent
purpose and a meaningful intermediate result: what does this part establish or
change, and which later calculation uses it? Distinct contributions, differently
controlled responses or substantial transformations can deserve separate entries
even when they live in one function or share inputs. These are judgment cues, not
mandatory categories or a rule to split every expression. A reader should be able
to find the substantial calculation without first opening a generic umbrella
stage and searching its body.

Keep address arithmetic, individual samples, factors and forwarding helpers with
their owner when they only implement that one purpose. Judge by role and context:
a coordinate or color transformation may itself be an important calculation,
while a small conversion used inside it may not. Preserve the actual order and
branch ownership when refining a stage. Repeated appearances of one calculation
can share an entry; distinct calculations should not be merged just because they
have similar names or contribute to the same final color.

Use these same decisions for the function flow graph at the appropriate level.
Its useful nodes describe actual calculation modules, with supporting steps
inside them. The directory remains independent navigation and need not copy graph
nodes or function boundaries one-for-one. Refining the overview does not require
source edits solely to manufacture extra graph nodes.

Publish this alongside the source review using `semantic_overview`:

```json
{
  "stages": [{
    "id": "stable-calculation-id",
    "title": "Short calculation title",
    "description": "Brief explanation of the actual purpose.",
    "anchors": [{"function": "ActualFunction", "startLine": 20, "endLine": 31}]
  }],
  "review": {
    "source_sha256": "the exact current proposal or saved source hash",
    "granularity_evidence": "Identify independently useful calculations exposed in the directory, explain any broad grouping retained, and say which supporting steps stay with their owners.",
    "data_flow_evidence": "Explain the actual order, meaningful branches and shared results that determine these stages.",
    "scope_mappings": [{
      "scope_id": "an existing scope_review result id",
      "stage_ids": ["stable-calculation-id"],
      "disposition": "represented",
      "evidence": "Explain how this reviewed calculation scope is discoverable through those stages."
    }],
    "remaining_work": []
  }
}
```

The example is a schema, not a stage template. Map every source-review scope to
the applicable stages. `represented` covers meaningful calculations;
`supporting` assigns subordinate work to its owner, without creating extra rows.
`inactive` may have no stages when the source is genuinely inactive/interface-only;
explain that source-specific reason. Standalone directory repair without an
organization requires `source_functions` in each mapping, covering the current
source functions. A scope may map to several stages and a stage to several scopes.
Supply several anchors for a coherent calculation across functions. Omit line
ranges only when the whole function is a useful anchor. `helperSignatures` can
associate supporting functions without turning them into extra stages.

Review in both directions: each stage opens the intended calculation, and the
meaningful computations in the source can be found without entering a generic
catch-all. Check order, branch ownership, duplicates, titles and omissions after
edits. Revisit broad rows: do they hide independently explainable major work that
would make navigation more useful if separately exposed? Also revisit very small
rows that only repeat their owner's purpose. Record the concrete split/grouping
decisions in the existing granularity evidence, not an extra report or review
round. This evidence is the Agent's assessment; the Host verifies current-source
identity, usable anchors and coverage shape, not semantic correctness. Repair
remaining gaps before a complete assessment; do not use boilerplate evidence to
claim that a coarse fallback has been analyzed.

For an otherwise finished Shader with a missing, coarse or stale directory, use
`get_shader_semantic_overview_context(workspace_path, organization_id?)`. Read the
current source and retain still-valid analysis. Publish the independently revised
metadata using `update_shader_semantic_overview` with the current source hash and
the payload above. Pass the organization id to attach it to a current source
review. This path neither changes Shader code nor compiles/applies it. A stale
source review remains a separate issue; it is not upgraded by editing navigation.

Store metadata outside source. Do not add `@overview`/`@stage` markers, a generated
header directory, provenance or validation banners. Old annotations and coarse
function navigation remain usable for locating code but are not evidence that
the requested independent directory is complete.

Requested workspace display is part of delivery. Read back the actual editor's
directory state, parsed source hash, semantic overview digest and ordered stage
IDs. They must match the current reviewed source. A pending projection may
finish asynchronously; missing, stale or provisional navigation requires
repair, even if the Shader compiled and rendered correctly. Use the returned
metadata/display recovery action and read back the same logical Shader, without
recompiling unchanged code. Clean superseded source versions belong to that
same Shader item; protected unsaved copies must not appear as a second canonical
result. Choose independent rendering calculations, not a fixed directory count.
