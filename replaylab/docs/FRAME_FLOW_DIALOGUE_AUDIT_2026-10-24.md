# Frame diagram dialogue audit — 2026-10-24

This is development evidence, not text to display inside rendering-stage cards.
Task: `47af9e58efa14fb49fed6550e8847c16`.

## Scope and evidence

The requested operation was frame explanation/diagram publication. No live capture,
paid model run, or application launch was performed during this audit.
Subsequent implementation acceptance used the native application and the existing
capture; those checks are recorded separately below and did not rerun a paid Agent.

Local retained sources:

- `%LOCALAPPDATA%/ReplayLab/ai-tasks/47af9e58efa14fb49fed6550e8847c16.jsonl`
- The adjacent `47af9e58efa14fb49fed6550e8847c16.report-state.json`
- `%LOCALAPPDATA%/ReplayLab/console-events/events.host.48172.81cdc89d80f5499e86fbef01cd6817ce.0000.jsonl`
- The external decompiler `job.json`, `stderr.log`, and `stdout.log` referenced by those console failures, below the existing project's `workspaces/shaders/v2/*/jobs/`.
- Audit extracts: `%TEMP%/bread-frame-log-audit-47af9e/{stats.json,external-tool-errors.json,public-messages.txt}`. Public messages contain only message events, not private reasoning.

The JSONL's MCP activity events retain argument **names**, not argument values or
complete responses. The report retains only the most recent 128 detailed facts.
The native run-owned transport directory was already removed. These limits matter
when attributing an individual file-read failure.

## Counts and elapsed time

Wall time: **6,024.574 seconds (100 minutes 24.574 seconds)**, from local
2026-10-23 23:43:21.045 to 2026-10-24 01:23:45.619.

The task JSONL contains 6,946 events. Deduplicating terminal MCP activities by
`activityId` gives **1,179 MCP calls, 80 failed**. Host `toolStats` contains
**416 calls, 52 errors**. These are different layers, not contradictory totals:
the task proxy handles local pagination, skill loading, and capability routing.
An `invoke_bread_tool` call appears under its wrapper name in MCP activity and
under its actual tool name in Host statistics.

| Host operation | Calls | Errors | Sum of recorded duration |
| --- | ---: | ---: | ---: |
| `get_active_shader_workspace_path` | 36 | 0 | 379.310 s |
| `prepare_shader_workspace` | 60 | 28 | 263.548 s |
| `decompile_shader_workspace` | 21 | 15 | 110.047 s |
| `get_shader_workspace_representation` | 33 | 4 | 46.534 s |
| `read_shader_workspace_file` | 44 | 0 | 0.201 s |
| `get_current_shader_representation` | 34 | 0 | 93.579 s |
| `inspect_shader` | 35 | 0 | 42.189 s |
| `inspect_frame_render_stage` | 73 | 0 | 128.016 s |
| `inspect_frame_resource_usage` | 18 | 0 | 22.204 s |
| `capture_event_texture_evidence` | 17 | 0 | 55.875 s |
| `publish_frame_render_flow` | 10 | 4 | 36.673 s |

Total recorded Host tool duration is **1,192.501 s (19 min 52.501 s)**. Workspace
lookup, preparation, decompilation, and workspace representation alone account
for **799.439 s (13 min 19.439 s)** of that sum. This is tool execution time, not a
measurement of the corresponding wall-clock interval. The remaining wall time
includes model generation, task-proxy work and orchestration; this log cannot
reliably divide it among those causes.

MCP `read_evidence_file` was called **740 times**, with 27 failures. Host saw only
two of those calls, both successful. The proxy intercepts reads of its own retained
large tool results. Consequently, **the missing Host representation root cannot
be blamed for all 740 calls, or asserted to explain the 27 failures**. Their exact
argument paths and error payloads were not retained in the available activity log.

## Why Shader preparation was entered

The Agent explicitly called editable-workspace tools. It was not a hidden side
effect of `get_current_shader_representation`: that read-only representation tool
already succeeded 34 times and does not invoke `prepare_shader_workspace`, an
external decompiler, a compiler, or a replay replacement. Previously it did export
a temporary raw-binary catalog for the explanation receipt.

Two implementation gaps made the explanation path unnecessarily expensive:

1. `get_current_shader_representation` produced text below the Host-owned
   `RenderDocUltra/shader-representations` root, but `_evidence_roots()` did not
   authorize reading that root. This is a reproducible interface bug even without
   a project, distinct from the unproven historical path attribution above.
2. Native `reverse_shader` generated every disassembly target and embedded source;
   the publisher concatenated them into one file. Hardware ISA and portable IR
   duplicated investigation work. Reading a large spilled JSON line offered only
   sequential byte pagination, with no literal search/precise match locations.

The 28 preparation failures consist of one path outside the active project and
27 paths rejected because they were not the managed deterministic workspace path.
These failures are unrelated to shader numeric equivalence or GPU occupancy.

The 15 decompilation failures are independently supported by job stderr:

| Error class | Count |
| --- | ---: |
| RenoDX `!data_type.empty()` assertion | 4 |
| `Unexpected dynamic cbuffer offset` | 2 |
| `Unexpected goto` | 3 |
| Unknown resource type for `textureunorm_f32 2darray` | 2 |
| Adapter rejected missing baseline input-signature row | 2 |
| Invalid workspace baseline interface binding | 1 |
| Unsupported assignment `@dx.op.quadOp.i32` | 1 |

There are **43 Host console errors** across preparation and decompilation. An
additional UI-state error and other publication errors appear at other layers;
the screenshot's total is not a count of 44 unique shader compiler defects.
No explicit compile/apply tool appears in this task's recorded tool inventory.
Argument-name-only activities do not establish every decompile option value, so
the audit does not infer that no internal validation ever ran.

## Why the last dependency investigation was rejected

The final public completion review identifies the chain
`62768 -> 62778/62783 -> 62931` for resources `18993460/18993461` as incomplete.
Detailed participation evidence reported `indirectArgumentsResolved=false` and
`nonZeroWorkload=null` for 62778/62783. The old frame outline nevertheless treated
the native zero-initialized dispatch counts as proven zero work. Publication then
rejected those EIDs as ineffective actions. This is a contract contradiction;
unknown indirect workload is not proof of an empty dispatch.

Native `ActionDescription` initializes counts/dimensions to zero;
`ActionFlags.Indirect` is `0x080000`. The outline now preserves `hasWork=null` for
indirect calls unless resolved argument evidence is explicitly present. A proved
zero workload still returns `false`. This change does not invent the missing GPU
argument values or claim the algorithm has been understood.

## Implemented corrections and verification

- Explanation defaults to embedded source or the first usable portable native IR.
  Hardware-specific alternatives remain available with `text_mode="all"`.
- Raw-binary export is explicit (`include_raw_bytes=true`). Ordinary explanation
  never creates an editable workspace or a binary catalog.
- The first receipt includes a bounded 4 KiB text preview, exact section offsets,
  and a continuation path. Its Host-owned root is readable without an active project,
  with redirected-ancestor checks preserved.
- `read_evidence_file(text_query=...)` searches saved text. Both Host evidence and
  proxy-retained JSON report exact UTF-8 `matchOffset`, a readable context `offset`,
  and a next search offset. Multiple matches on a single long JSON line are retained;
  search does not replay the capture or bypass file ownership/hash checks.
- Editable preparation/decompile tool contracts explicitly direct explanation work
  to the read-only representation tool. No inferred-keyword permission gate was
  added, so explicit user-requested editing remains available.
- Indirect draw/dispatch/mesh placeholders retain unknown workload until proved.

Validation: the six test files below passed **212 tests and 13 subtests**, with one
platform-dependent skip, in 44.70 seconds. Additional assertions through a real
two-process MCP proxy and the API tool adapter then passed in a targeted **3-test**
run (4.21 seconds). The Bridge files also pass Python 3.6 AST parsing. An existing
organization test fixture was supplied with its required `workspacePath`; its
production editing behavior was not changed.

Tests cover source preference, native-target fallback, legacy all-target inspection,
absence of workspace/catalog calls in analysis, real artifact readback, UTF-8 search
past 200 KiB on a single JSON line, all 35 same-line matches across pages, retained
file tampering rejection, and unresolved/proved indirect counts. No paid model rerun or
end-to-end frame-analysis speed claim is made.

Relevant test files:

```
replaylab/mcp_host/tests/test_frame_render_flow_outline.py
replaylab/mcp_host/tests/test_evidence_file_reader.py
replaylab/mcp_host/tests/test_bridge_composite_readonly.py
replaylab/mcp_host/tests/test_shader_workspace_mcp.py
replaylab/tests/test_owned_tool_output.py
replaylab/tests/test_task_mcp_proxy.py
```

## Presentation, navigation and native integration

Bread 1.13.55 includes the following changes alongside the analysis-tool fixes:

- Stage cards show the plain-language summary and actual horizontal `processSteps`.
  The input/calculation/output/composition ledger and calculation-details panel
  are no longer rendered. The evidence model retains those records for review.
- Stage and texture titles retain link styling without the arrow suffix. Texture
  navigation selects the exact attachment in the current output/following view,
  including compute UAV descriptors and the Present destination. It does not open
  another locked texture tab.
- Channel details use a horizontal popup with close, outside-click and Escape
  dismissal. Missing component images are requested from the actual texture;
  format component counts determine which channels exist, so RGB does not gain
  fabricated Alpha. One group shares event selection/restoration. Bounded caches
  verify PNG hashes and expire on capture, Shader or CBuffer changes.
- Native tree items provide plain text through `AccessibleTextRole`. UI inspection
  reads that role before the rich display value, avoiding the Python conversion
  failure for `RichResourceTextPtr` while preserving visible resource links.

The Development application was rebuilt, its canonical Python integration and
frozen runtime deployed, and translation synchronization/completeness gates passed.
The frozen check matched 79 modules, 20 skill files and 27 bridge Python files to
source. The frozen MCP route probe returned 48 tools with 48 effect annotations.
The integrated regression selection passed 321 tests. A separate native popup
fixture passed 86 checks, including horizontal layout, dismissals and image export.

The final native capture fixture passed **27 checks in 63.64 seconds** against
`D:/Study/抓帧/白银之城/1.rdc`, using the deployed 1.13.55 application:

- The saved 23-stage/113-preview flow restored with the correct capture; five
  project Shaders restored as editable and applied, with no restore failures.
- Current-output navigation selected RTV EID 61043 / resource 18993397, UAV
  EID 60823 / resource 18993447, and Present EID 66327 / resource 440. Locked
  texture tab count stayed zero throughout.
- EID 62027 returned four actual R/G/B/A PNGs. Their signatures and SHA-256 hashes
  verified, all four appeared in the popup, and the original EID/session survived.
- The second channel request used the verified cache with zero backend capture
  calls. Fixture-observed times were 2.922 seconds initially and 0.187 seconds
  cached; these include timer sampling and are not general performance guarantees.
- A bounded API Inspector page read returned native Buffer and Render Target
  rows through the real UI-state tool without a rich-resource conversion error.
  This was a resource-row sample, not an exhaustive tree traversal.

Acceptance evidence: `%TEMP%/bread-frame-flow-user-profile-155/result.json`,
`verify.py`, and `real-rgba-popup.png`. Earlier fixture attempts exposed incorrect
Python wrapper/identity assumptions in the test script; the final fixture uses
the application's UI-state interface and passes. The isolated test process exited;
no other application instance was terminated for these checks.

These checks validate implementation and delivery. They do not turn the previous
partial diagram into a completed algorithm investigation or measure a fresh
end-to-end Agent run.
