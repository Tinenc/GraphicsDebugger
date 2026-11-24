# Bread AI runtime architecture

The user states the outcome. The Agent chooses its investigation and edits.
The Host supplies capture facts, IDE operations and checks for actual side
effects. A Host error is a specific tool result, not a workflow instruction.

```text
qrenderdoc chat / explicit Shader action
    -> replaylab-ai AgentHost
       -> model API: DeepAgents main Agent and optional general sub-agents
       -> ACP/Codex: vendor-native Agent
    -> task-scoped Bread MCP route
       -> direct typed core tools or on-demand long-tail operations
    -> MCP Host, Shader Workspace and active-project services
    -> qrenderdoc first-party bridge and replay controller
```

The profile kind selects the engine; the engine does not decide the user's
task. Both paths receive the same task-scoped Host effect grant and use the
same workspace/capture services. API children may specialize by Skill; an
ACP/Codex provider owns its own model loop. Codex can use native commands and
file/image tools inside a verified task workspace. Native writes require a
verified workspace-write scope; otherwise the connection falls back to
read-only and tells the Agent to use the granted Bread tools. Writes to
canonical Shader, project state or replay still use the capture-bound MCP
route. ACP permits supported owned-workspace file operations; unrestricted
generic shell operations remain unavailable without a verifiable sandbox
contract. The bridge is
packaged from `replaylab/renderdoc_mcp_bridge/`, not discovered as a user
extension.

The UI snapshot carries the current capture and selected EID as fallback
context. An EID explicitly identified in conversation takes precedence. Reads
may inspect other events and bindings. A mutation must name a concrete current
target and pass the Host's capture, Shader, resource and source checks; a stale
UI focus or conversation ID is not evidence of the live target.

## Agent guidance and tools

Skills under `.agents/skills/` are optional domain guidance. The Agent can load
them through `load_bread_skill`; loading one does not spawn a specialist. They describe
useful evidence and the quality of a result, without prescribing a tool order,
line count, engine, asset-matching path or number of review rounds. The user
can disable `replaylab-core`. Missing or disabled Skills do not block a task or
change its effect grant. The Shader button supplies a target and task context;
the Agent chooses useful guidance and methods to satisfy the requested outcome.

`policy.py` defines a compact typed core tool set for event/Shader inspection,
bound resources, source reading/editing, naming, final validation and
application. Common tools are callable directly on both API and ACP routes.
Other registered Host operations remain discoverable through
`list_bread_capabilities` and callable through `invoke_bread_tool`; loading a
separate schema first is optional. This keeps legacy UI/project operations
available without putting their complete schemas into every model turn.

The route checks each operation's input schema, connection allowlist, effect
class (`workspace`, `project`, `ui`, `replay`) and current target. The model's
tool loop has no Host-imposed call budget. Skills and prompts cannot widen the
effect grant. The model decides which tool is useful and whether another
observation will change its answer.

## Shader editing and delivery

The Agent may start from capture evidence or reuse compatible project source.
It can choose reading windows and function boundaries based on the actual
syntax/data flow. IDE rename/extraction tools perform mechanical scope, CAS
and syntax checks. Draft edits are saved as draft progress; they need not pass
a whole-Shader compile/ABI check after every local change. A failed baseline
compile does not prevent creating and repairing the draft.

For broad organization, `begin_shader_workspace_organization(agent_file_name=...)`
can copy the entire current proposal into the Agent's owned directory. The Agent
edits and reviews that file, then imports it once through `source_file` with the
proposal CAS identity. `read_shader_workspace_file` supports explicit complete,
file-only and range delivery. Verified complete first-party source bypasses the
generic output eviction up to the existing 4 MiB file bound; ordinary outputs do
not. Coverage records distinguish copying a whole file from actually returning
all of its text to the model.

The Host reports readable-code findings such as anonymous locals and long
functions as diagnostics. They do not dictate the Agent's next action or make
an otherwise valid draft disappear. The Agent remains responsible for naming
from observed computation, reducing useless temporaries, covering the scope
the user requested and reporting any unfinished region honestly. A function
graph, when requested, comes from actual final calls and comments.

There is no per-edit stage gate, review-round budget or Host judgment that a function
is readable. By default, `finish_shader_workspace_organization` saves the exact
proposal as editable source without compiling or replacing replay code. The
Agent may request validation or apply explicitly; applying compiles the current
source and checks the captured ABI and live target. A save or validation receipt
does not prove that the user's semantic organization goal was completed. The
Agent must distinguish an unchanged source from work it actually reorganized.

Before publishing a compiled candidate or replacing replay code, the Host
checks the exact current source revision, compiler result, captured interface
and replay ABI. Project registration reads back the exact immutable knowledge
record and final source receipt. Applying
a replay replacement and starting live edit are distinct effects. A draft
save, project registration, compile, replay replacement and editable Shader
Workspace entry must be reported as separate facts. Undo uses the exact
current replacement/session identity, not the chat's old report sidecar.

Texture display names, CBuffer panel aliases and source identifiers are
separate destinations. The Agent chooses names from capture evidence, code and
optional unpacked assets. The Host checks actual current bindings, reflected
CBuffer byte ranges/layout, name safety, user-owned names and write CAS. A
same-platform cooked material layout can strengthen a semantic name; it is
not a universal prerequisite. Missing `shaderResourceId` in an old alias file
cannot by itself prevent a new current capture match. Semantic confidence and
verified bytecode identity are separate claims.

## Telemetry, recovery and compatibility

The old `report_task_result` and review-budget tools remain removed. Completion
review applies to every main-Agent task, including questions without a capture.
The Agent derives acceptance criteria from the current user request and relevant
Skill delivery requirements. `record_task_review` records each requirement,
`satisfied` / `unmet` / `blocked`, and concise evidence, plus an overall verdict,
remaining findings, next actions and any concrete blocker. Ordinary explanations
can use one criterion and their existing answer as evidence; they do not need
external investigation or an artifact solely to satisfy this protocol.

The task-local tool is available independently of capture services. The receipt
is bound to the task's current execution and observed work revision. Later tool
work makes an earlier assessment stale; repeated observations or changes to
review wording alone do not count as substantive progress. This is an Agent
assessment, not independent Host verification of arbitrary artifacts or claims.
The main Agent owns the overall review; child agents supply evidence and gaps.

At a normal end-turn boundary, missing or stale review and actionable unmet
criteria continue in the same Agent session. Real blockers or repeated endings
without progress produce a partial result with completed work and remaining
requirements. Pending tools, cancellation, transport failures and output limits
retain their existing termination handling. Review reuses collected evidence;
additional tools are justified by a concrete gap. Once the user's criteria are
met, optional polish is not a reason to keep the task running indefinitely.

Completion is followed by cleanup, so ordinary user output files must remain
available afterwards. A bounded scan of the owned working directories preserves
the original turn directory when non-Shader outputs are present; controller
bootstrap/configuration names are excluded from this decision. It does not copy
credentials or relocate the output. A retained-file receipt identifies the actual
local paths and does not imply Host/project import. Shader-only work continues
to use the existing source recovery archive. Failed or oversized scans retain
the source conservatively instead of deleting potentially useful work.

Shader
organization now has an explicit Agent self-review receipt, requested after the
Agent's coherent source edit. `record_shader_organization_review` binds the
Agent's assessment to the current proposal/saved source hash and records four
checks: readability, Unity-style internal structure, function calls and redundant
temporaries. The Host checks identity and record shape, never source quality.
Only organization intent or actual organization operations engage this specialized
source lifecycle. The general review cites this evidence without repeating the
four checks. Both reviews are joined into one continuation decision, so a mixed
task does not restart two separate workflows.

At an organization turn boundary, ACP, Codex and API retain their current session
and continue if the current review is absent, stale, reports repairs, or the
reviewed draft has not been saved. Source changes invalidate the old review.
Repeated end-turn without source/review progress stops with a partial outcome
and reason. A first fresh blocked review now gets a same-session recovery
reassessment: the Agent checks prior attempts and practical authorized alternatives,
preserves existing work, and either continues or explains the actual dependency.
Repeating blocked with the same work/source facts ends as partial; rewording the
blocker does not restart the cycle. The main
Agent still owns the edits, semantic checks and whether repairs are warranted;
the runtime does not run a second model, score source, or impose fixed review
rounds. A review receipt is separate from successful compilation or replay apply.
An already-satisfactory source can be accepted only with a full review and an
explicit `no_change_reason`; the final response then states that this turn did
not modify those source files.

Before deleting an owned turn directory, ordinary Shader source files are
retained with a hash/path recovery manifest. Configuration and hidden files are
excluded. Recovery errors and safety bounds preserve the original directory.
These local copies are not automatic workspace imports or replay authority.

Progress before the last tool is not a final answer. Empty conclusions,
transport interruption and explicit provider output truncation are reported
as incomplete outcomes. ACP continues explicit `max_tokens` and
`max_turn_requests` stops in the same session; API continues explicit provider
output limits using its current checkpoint when no graph nodes or tools are
pending. Neither path imposes a total output-continuation count while new work
facts or new answer text arrive. Three consecutive limited turns without either
stop as partial rather than spending indefinitely on repeated or empty output.
Tool counters, timestamps and copied fallback answers do not count as progress.
Both paths preserve partial text across empty retries until a new tool boundary.
Continuation guidance asks the Agent to split work and tool arguments into
bounded steps and save large deliverables incrementally. Completed tools are
not replayed, and configured output caps remain unchanged.
API also uses that checkpoint for Shader review
continuations and appends only the continuation message; it never resends the
original task or reruns completed tool calls. Stalled or unsafe-to-resume API
truncation remains an explicit partial outcome. Codex accepts an explicit
`final_answer` from a completed turn snapshot if the provider omitted the separate
item event; commentary and reasoning are never fallback answers. The
[November output audit](AI_OUTPUT_CHAIN_AUDIT_2026-11-11.md) records the coverage
and remaining live-provider limits. The [October execution audit](AI_EXECUTION_AUDIT_2026-10-12.md)
records the observed premature conclusion, permission limits and performance
fixes, including targeted GIL release around native replay, Shader inspection,
compilation, readback and replacement calls. Checked replacement/removal can
replay internally, so wrapping only the public `SetFrameEvent` entry is insufficient.

The [length recovery design](AI_LENGTH_RECOVERY_DESIGN_2026-11-11.md) records the
open-source comparison and input-side implementation. API model-window metadata
now drives the running compactor, with output reservation, conservative complete
request counting and a bounded summary input. The original task remains pinned
as quoted user data with an exact archive reference. Oversized initial text is
archived before the first call. Native ACP/Codex can page equivalent task-bound
input through `read_evidence_file` without gaining ambient filesystem access.
Explicit provider context errors recover inside the existing model call; ACP
uses `/compact` only when the current session advertised it. Maintenance text
never becomes an answer. Truncated API tool proposals are quarantined before
execution, including parsed partial JSON. Root checkpoints record them as
unexecuted and may continue with bounded calls; children return that fact to the
parent. Actual pending tools still prohibit unsafe continuation.

`decompile_shader_workspace(reference_only=true)` can reconstruct the immutable
captured baseline into an independent reference even when working source already
exists. It preserves canonical source, drafts and compiled candidates, and performs
no compile probe or replay. The returned path/hash describes raw reconstruction,
not an applied or ABI-validated replacement. A matching fixed-route/baseline result
is reused after checking output and tool integrity. The Agent reads that reference
only when it addresses a concrete gap; unchanged reconstruction is not a reason to
restart organization. Missing debug names do not prevent conservative structure,
function extraction and dataflow naming while preserving uncertain calculations.

Codex owned-directory writes use an explicit randomly named permission profile,
selected consistently for thread/start and turn/start. Legacy sandbox fields may
silently resolve to readOnly in current app-server builds. The effective response
must grant only the owned runtime workspace, disable network and exclude ambient
temporary-directory writes. Unavailable or unverified capability falls back to
readOnly with a timeline diagnostic; tool presence alone does not prove write access.

Keep user-authored source and aliases unless the user explicitly selected
them for replacement. Bread-managed stale drafts/aliases can be repaired or
replaced when their old lineage does not match the current captured Shader.
The immutable original Shader identity, current binding, physical byte
layout and source version remain the relevant compatibility anchors. Old
technical `ReplayLab` schema names, storage paths and Python/C++ identifiers
are retained where changing them would break existing projects; the
user-facing brand is Bread / Bread AI.

Capability varies by graphics API, source language, compiler and available
asset reader. The Host should return that capability and a concrete
diagnostic; the Agent should use the supported route and state any limit.
This architecture does not promise HLSL-level restructuring for every
GLSL/native-IR capture.

Run Agent and MCP Host tests separately (their suites include same-named test
modules), then the packaging route probe, i18n gates and the qrenderdoc x64
Development build. Use `/m:2` for qrenderdoc on memory-constrained Windows
hosts. A real RDC/model smoke test is a separate release check because it
requires an active capture, configured provider and replay environment.
