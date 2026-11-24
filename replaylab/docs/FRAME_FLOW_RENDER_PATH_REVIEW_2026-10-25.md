# Frame rendering-path review and task isolation

## Audit of the latest run

Task `ca9920726fa74b6d9f8d579191ba1229` ran from 16:08 to 17:15 using
`codex-app-server`, model `gpt-6-sol`. It inspected 167 stages, 46 resource-use
records and nine Shader representations, and captured 44 texture images. Its
23 stages covered 1,925 effective actions. This was substantial new analysis,
not the earlier six-stage persistence failure or simple republication.

The remaining problem was semantic review. Its final criteria checked coverage,
publication receipts and viewed images/IR, but did not assess whether the visible
diagram explained the shading paths and distinct object contributions. The
internal algorithm for stage 8 already recorded deferred lighting, while its
visible title described local occlusion and lighting injection. Stages 16 and
18/19 used broad additional-color/postprocessing labels without establishing
their specific calculations. No mesh-shape or associated unpacked-material
inspection appeared in this run. Those tools are alternatives when needed,
not quotas or requirements for every stage.

## Guidance change

Explain-event skill v26 introduces `references/render-path-review.md`, used
during discovery and final review. It reasons from where material and lighting
are evaluated, allows forward/deferred/hybrid paths, and follows the distinct
object/material branches actually observed. It checks that important supported
conclusions reach the displayed titles, summaries and media, rather than remaining
only in internal algorithm records. Resource writes and coverage are not proof
of understanding a filter or rendering purpose.

Findings lead to targeted investigation, revised grouping/names/media and another
review of the affected explanation and overall path. Existing task-review scope,
work revision and fingerprint mechanisms remain; publishing a changed result
invalidates an earlier review. No technique-name scorer, preset game/EID list,
stage count, new mandatory JSON form or fixed review-round count was introduced.
Valid unchanged evidence is reused. The user will exercise the built-in AI;
this change does not manually regenerate their diagram.

## Capture isolation repair

Task prompts now expose capture/session identities and prioritize same-capture
history. Project assets may be shared when their association is verified; EIDs,
ResourceIds and captured evidence are not carried blindly into a new game.

The task MCP proxy previously parsed its scope without forwarding it to the Host
process. The scope now reaches IPCClient, including direct/nested calls that do
not go through the server convenience wrapper. A model-supplied parameter cannot
override that pin. Context reads and manual UI switches cannot adopt another
capture. A successful requested-open receipt with matching path or replay-recovery
receipt with the exact previous/new session lineage can advance the task binding.
Independent in-process jobs retain their own binding. Ordinary UI capture changes
still cancel the old task; a new task receives the new capture's context.

Saved diagram restoration also verifies the returned outline/document session
before cache adoption and checks that the capture has not changed during the read.
Matching file paths alone cannot admit a stale same-file-reopen result.

## Connection capabilities

Recent tasks use the official Codex app-server connector, not ACP. Both routes
can drive an Agent tool loop with Bread's exposed tools, but protocol choice does
not automatically grant the desktop application's personal plugins, browser,
network or arbitrary computer-control environment. The current Codex route has
task-workspace files/shell/images, Bread skills and MCP tools, including explicit
application UI operations. ACP also depends on the chosen provider's negotiated
image and terminal capabilities. No provider permission expansion was needed.

## Regression review scenarios

These are review cases, not a required module checklist for generated diagrams.
They deliberately prescribe no game, EID, stage count or required technique.

| Observations and draft | Expected review decision |
|---|---|
| Geometry draws evaluate material and lighting; no separate object path is observed. | Explain the supported path; do not invent characters, terrain or a deferred stage. |
| Stored surface attributes feed a lighting consumer; another visible object shades directly and later joins scene color. | Explain both paths and their integration, rather than hiding one inside extra color. |
| Internal evidence establishes deferred lighting but the main title remains generic. | Repair the displayed explanation using existing evidence; another GPU read is unnecessary. |
| A postprocessing range has verified SRV/RTV bindings but only says it writes color. | Inspect the relevant calculation and consumers; complete coverage cannot close the semantic finding. |
| A live read fails but valid capture-bound source, bindings and images resolve the question. | Use that evidence route without claiming a fresh replay observation or blocking unrelated modules. |
| A reassessed conclusion and its supporting evidence remain correct. | Keep them; do not force renamed stages, new images or different grouping to demonstrate activity. |
| A marker/thumbnail suggests an effect but the actual computation contradicts it. | Investigate the contradiction and follow the supported operation; do not preserve the guessed name. |

Offline tests can verify scope isolation, transport, review freshness, tool
contracts and complete skill delivery. They cannot prove every future model's
semantic judgment. A built-in AI run should be evaluated against its actual
capture evidence and final visible explanation using these decision principles.

## Validation

- IPC and frame-server checks: 71 passed, including 15 new transport-scope cases
  and two saved-diagram lifecycle cases.
- Proxy, native connector and prompt checks: 147 passed.
- Shader workflow and replay-recovery neighbors: 232 passed.
- Persistence, resource-cache, export and recovery checks: 83 passed.
- Real file IPC, CLI context and persistence checks: 34 passed.
- Skill/gateway/tool-contract suite passed; both entry and every reference are
  delivered completely within the inline limits, including the new review guide.
- Additional review-freshness, persistence, reanalysis and scope-discovery suite:
  66 passed. The amended capture-target prompt and standards subset: 85 passed.

These suites overlap and must not be summed as unique tests. No paid model run
was made and no user capture or authored diagram was regenerated.

## Installed result

Runtime 1.13.64 was built, checked against 258 source files, and deployed after
an incremental qrenderdoc build with `/m:2`, explicit `SolutionDir`,
`ReplayOptimizeDevelopment=true` and `SkipReplayLabRuntime=true`. Compiled code
from 23 packaged modules and all nine explain-event/core skill files matched
the final sources. The 31 canonical bridge files and all four runtime binaries
were verified in the application installation before restart.

The restarted application reports bridge 1.13.64 and reopened the original
`6.rdc`, frame 6773, with a new capture session. A read-only call through the
installed BreadMCP executable, carrying that explicit task scope, retrieved
the current capture and restored 23-stage diagram successfully. It did not
generate or rewrite the diagram. Build/deployment receipts are under
`%TEMP%/bread-frame-flow-deploy-164/`; the final runtime build log is
`%TEMP%/bread-runtime-build-1.13.64-final.log`.
