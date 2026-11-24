# Bread AI architecture

Bread AI works like a tool-using assistant: the user states an outcome, the
Agent chooses how to investigate and edit, and the Host supplies capture facts,
IDE operations and receipts for actual effects. A Host diagnostic describes one
operation; it does not assign the Agent's next step.

```text
qrenderdoc conversation / explicit Shader action
  -> replaylab-ai AgentHost
     -> Model API: deepagents main Agent, optional general sub-agent
     -> ACP / Codex: selected vendor's native Agent
  -> task-scoped Bread MCP route
     -> direct common tools; on-demand capability directory and tool invoker
  -> first-party MCP Host, Shader Workspace and project services
  -> qrenderdoc bridge and replay controller
```

The qrenderdoc Python bridge is bundled from `replaylab/renderdoc_mcp_bridge/`
at the fixed first-party application path. It is not discovered as a user
extension. Existing `ReplayLab` storage paths and protocol identifiers remain
for compatibility; the user-facing brand is Bread / Bread AI.

## Engine selection and model control

The chosen profile selects the engine. Model API profiles use `DeepAgentsEngine`
with the installed deepagents runtime. ACP and Codex app-server profiles use
`NativeAgentEngine` and the selected vendor's model loop. There is no secondary
Bread planner, shader stage state machine or fallback engine.

On the API path, the main Agent has common Bread tools directly and may use one
general sub-agent for an independent task. Both can inspect the capability
directory and load a Skill. The Host does not create or preload a sub-agent for
each Skill. On ACP/Codex paths, the vendor Agent sees the task-scoped Bread MCP
route and the same optional Skill directory/loader. A Skill explicitly selected
by the caller can be preloaded; an EID, Shader button or UI intent alone never
selects one. Neither path automatically chooses a Skill from request keywords.

Skills under `.agents/skills/<id>/SKILL.md` are optional instructions and
quality guidance. `replaylab-core` is listed like any other available Skill;
missing, disabled or invalid Skills do not block an unrelated conversation.
Loading a Skill does not enlarge the MCP tool or effect grant. The Agent can use
a different supported method when that better serves the request.

## Request context and target identity

qrenderdoc supplies a hash-bound snapshot of the loaded capture, current UI
EID, focused Shader, resources and other context. It is a fallback for locating
the request, not proof of a claim. A target named in the latest user message or
established in conversation takes precedence over UI selection. The Agent can
inspect other EIDs and their bound resources before choosing an edit.

Capture-specific edits identify a concrete current target. The task MCP filter
enforces the connection's tool allowlist, effect ceiling and applicable exact UI
event/pixel scope. Host tools check their own capture, resource, original Shader,
source version/CAS and effect preconditions. A conversation id, old draft path
or task sidecar does not make a different live Shader the same target.

## Tools and effects

`policy.py` declares first-party read-only tools and the `workspace`, `project`,
`ui` and `replay` effects of mutations. Common tools have direct schemas. Other
registered operations are discoverable through `list_bread_capabilities` and
callable through `invoke_bread_tool`; `load_bread_skill` reads optional Skill
instructions. The Host reports facts, errors and effect receipts, while the
Agent decides tool order and when it has enough evidence.

`OrchestratorConfig` contains transport/resource settings, not a work plan.
`main_timeout` defaults to 86,400 seconds (24 hours) and can be configured from
1 to 86,400 seconds in `%LOCALAPPDATA%\ReplayLab\config\agent-engine.json`.
The user may cancel sooner. `max_tool_calls` defaults to `0`, meaning no
per-task call limit; a nonzero value from 1 to 1,000,000 is an optional
connection-level limit. These settings apply to API and native routes alike.
The proxy serializes physical mutations and records admitted tool calls; it
does not cap shader review rounds or decide whether a task is finished.
Individual provider and MCP operations retain their own transport timeouts;
the 24-hour default is the whole Agent turn, not a promise for one tool call.

ACP/Codex use the vendor's own model loop. For capture-bound work, the vendor
is attached to the Bread MCP route; generic vendor shell, file mutation, Apps
and computer-use tools cannot widen that route's authority. External MCP
servers explicitly enabled in settings retain their own contracts. Provider
authentication remains with the selected vendor or API provider.

## Shader work and publication

The Agent can reuse compatible saved source, inspect the selected EID and
resources, rename Texture/CBuffer/source symbols, edit source regions and
organize functions in the order useful for the task. IDE rename and extraction
tools perform mechanical scope, syntax and version checks. The Host does not
require a whole-Shader compile, ABI proof or replay replacement after each
local draft edit. Readability findings are diagnostics, not a semantic quality
gate. The Agent remains responsible for meaningful names, code structure and
honest coverage of the requested scope.

`finish_shader_workspace_organization` saves the current proposal as editable
source by default, without compiling or applying it. The Agent may request
validation or replay apply explicitly. A compile receipt, project save, replay replacement
and editable Shader Workspace entry are different effects and must be described
separately. Publishing a candidate or replacing replay code requires exact
current source, compiler, interface/ABI and replay-target checks. A successful
physical check does not prove that the code was semantically reorganized.

Texture display names, CBuffer panel aliases and source identifiers are
different destinations. The Agent chooses names from capture evidence, code and
optionally unpacked assets; the Host validates bound resources, reflected byte
ranges/layouts, name safety, user-owned names and atomic writes. Old alias
records lacking `shaderResourceId` must not veto a valid new match. User-owned
source and names remain protected unless the user explicitly selects them for
replacement; Bread-managed stale drafts and aliases may be repaired or replaced
within the requested Shader scope.

## Results, telemetry and recovery

The main Agent's answer and actual tool receipts are the result. There is no
`report_task_result` form, `ReportGate`, mandatory report retry, Shader review
round budget or workflow-resume endpoint. Task sidecars retain bounded tool-use
facts and physical occupancy information for diagnostics and cleanup; they do
not approve prose or choose a next action.

Both engines finish when the model turn finishes, asks the user for a material
choice, is cancelled or reaches a real provider/runtime limit. The API path
streams model, tool and optional child events. ACP transport loss may reopen a
fresh session a bounded number of times using durable Host receipts; a
successful mutation is not blindly repeated. Unresolved tool errors stay in
the answer as limitations. A missing report or historical progress marker
cannot force more model turns or change the result to failed.

Task events are append-only `replaylab-ai-task-event/v1` JSONL under
`%LOCALAPPDATA%\ReplayLab\ai-tasks\`. qrenderdoc displays the Agent's answer,
clarification and tool progress; raw provider diagnostics go to local logs.
Each user turn owns one task file; automatic continuations remain in that same
file. These are not size-rotated conversation fragments. New queued records
include `data.conversationId` so multiple turn files can be grouped without
changing or moving existing conversations. Old v1 logs remain directly readable.
Provider token deltas are coalesced only across identical contiguous metadata,
up to 8192 characters or 100ms. Reset, item/channel/lifecycle changes, tools and
terminal events flush the pending batch. Final deliveries retain their fsync
and cross-process sequence/idempotency guarantees. An abrupt process kill may
lose at most the pending 100ms of display text; terminal acknowledgement never
precedes the flush. Readers append `text`, honor `reset` and distinguish stream,
agent, item and reasoning channel; a JSONL row is not a sentence or model token.

## Development checks

Run the Agent and MCP Host pytest suites separately because they contain
same-named test modules. The Agent suite covers API, ACP/Codex fixtures,
optional Skill loading, tool effects, target context and the real installed
deepagents graph. The frozen MCP route probe checks the compact tool catalog,
on-demand operation discovery and Skill loading. A live RDC/model test remains
a separate release check because it needs a capture, configured provider and
replay environment. Follow `AGENTS.md` / `CLAUDE.md` for i18n, packaging and
qrenderdoc build requirements; use `/m:2` on memory-constrained Windows hosts.
