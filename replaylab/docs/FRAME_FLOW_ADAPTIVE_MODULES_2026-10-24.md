# Capture-driven frame explanations

The dialogue audit's event IDs identify observations in that capture. They are
not mandatory investigation targets. A search of production Host, bridge,
Agent, UI and bundled skills found no branches tied to those historical IDs.
The actual rigidity was in generic quality rules.

## Changes

- Explanation skill v18 chooses functional modules from the current execution
  order, Shader evidence and resource relationships. Historical examples are
  neither required algorithms nor a fixed phase list.
- More than one key draw no longer automatically requires a diagram. Observed
  transfer of rendered results between targets asks for a small visual path;
  repeated draws into one target and parallel MRT outputs do not create a chain.
- Stages need not invent a named technique to satisfy an empty findings list.
  Claimed techniques and rendering calculations still require supporting evidence.
- Explained utility-only stages may omit the Shader algorithm tables. Captured
  action flags decide this, not an Agent-selected category. Mixed drawing or
  compute stages retain their calculation checks.
- Visual paths are checked against execution order and recorded resource
  relationships. Parallel MRTs cannot be presented as successive calculations.
  Text-only intermediate nodes do not bypass contradictions. A missing proof
  remains distinct from an established wrong connection.
- Related producer/composition events may lie outside the local stage when
  referenced by its algorithm record. The Host and UI share this rule. The main
  vertical timeline continues to follow actual execution order.
- The legend distinguishes stage execution arrows from module data-flow links.
  Sliders, animations and detail panels remain optional explanatory choices.

These changes improve the built-in AI workflow. They do not assert that an old
saved diagram has been newly analyzed or that resource bindings alone prove a
Shader's mathematical meaning. Reanalysis must gather or review current evidence
and update the module explanations before declaring the task complete.

## Verification

Regression coverage includes changed EIDs/resource IDs, same-target accumulation,
independent MRTs, static texture inputs, cross-stage relations, reverse edges,
branch/merge paths, missing evidence and utility-stage boundaries. Skill loading
and localization gates are also checked before deployment.

Delivery: Bread runtime **1.13.57**, explanation skill **v18**. The final Host,
schema, persistence and UI-model suite passed **350 tests**. Skill loading passed
**83 tests**; two symbolic-link cases were skipped because the Windows account
cannot create those links. The isolated native window passed **89 checks**.
Translation synchronization, complete catalog generation, source-literal lint
and native relink passed. The deployed bridge's 28 files were verified.

The packaged MCP and Internal executables were inspected for the final graph
and legacy-linear validation code. Every bundled explanation skill document
was byte-compared against the final source before deployment. Native UI evidence
is in `%TEMP%/bread-frame-flow-native-157/result.json`; deployment/state restoration
evidence is in `%TEMP%/bread-frame-flow-deploy-157/`.
