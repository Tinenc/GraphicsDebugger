# Bread AI Skills

API and ACP modes expose the same domain Skills through their respective
transports. For capture, Shader, export or asset work, load the matching
`SKILL.md` to obtain its useful paths and acceptance criteria. General chat
does not need a domain Skill. The user's request sets the outcome; the Agent
chooses tools and checks. A Skill does not grant a tool effect. Source-bound
review records preserve the Agent's assessment; the Host checks their identity
and structure, not whether the source is semantically good.

- `replaylab-core`: shared capture-identity, tool-effect and reporting boundaries.
- `renderdoc-explain-event`: EID, pass and frame analysis.
- `renderdoc-shader-workbench`: captured Shader analysis, source work, names and replay replacement.
- `renderdoc-resource-export`: capture-bound resource packages for Unity or Blender.
- `renderdoc-unpacked-asset-intelligence`: project asset search, reports and selected UEX delivery.

Each `SKILL.md` is a short entry point. Load it with `load_bread_skill`;
`list_bread_capabilities` can locate Skills and tools when needed. Its
`references/` pages are available on demand for an operation that needs their
detail. They are not an automatic bundle or a required phase sequence. A
missing Skill or reference is a relevant diagnostic, not a reason to prevent
independent work or a general conversation.

Host tools provide capture observations, operation results and physical safety
checks. The Agent chooses semantic interpretations and source edits. A saved
draft, compiled candidate, validated candidate and applied replay replacement
are different effects; describe them using their actual receipts. Compare
requested deliverables with observed results, repair concrete gaps, and repeat
only the checks affected by a change. Reuse completed work and inspect uncertain
mutations before retrying.

For Shader organization, first edit the whole requested scope and self-review
the source for readability, structure, calls and redundant temporaries. Repair
feasible findings and review again, keeping a record for the current bytes.
Perform necessary compilation and replay checks after that source work; an
intermediate save or successful compile does not establish readable completion.
