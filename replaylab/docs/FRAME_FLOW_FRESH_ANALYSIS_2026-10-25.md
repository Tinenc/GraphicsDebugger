# Fresh frame analysis, capabilities and task cleanup

## Observed run

Task `7493e2d240cd45f2a38cfba64730ddbe` ran approximately 20:55–21:11 through
Codex app-server. Its Host receipts contain one reanalysis preparation, 29 stage
inspection calls, 45 cached diagram reads and three publication attempts. The
native trace also contains image-view operations. It did some new investigation,
but no resource-usage tracing, Shader representation, mesh or unpacked-asset
investigation appears in this run. It mostly revised the existing 23-stage
partition. Elapsed time alone cannot establish whether analysis was sufficient.

The first publication succeeded. Two subsequent attempts failed on attachment
identity, yet the task could still complete using the earlier publication's
quality receipt. A later failed correction was not tracked as unfinished work.

## Reanalysis behavior

An explicit reanalysis now clears the in-memory authoring document and presents
raw chronological candidates. Valid raw observations remain reusable. The old
publication stays saved/displayed, and is available separately through the
`baseline` section for comparison after an independent explanation has been
constructed. It does not seed the new editable proposals. Refreshing the action
outline cannot reinsert that old saved document into an active reassessment or
overwrite a newer working draft.
The task ledger also restores this empty-draft boundary after a Host process
restart, so loading the still-saved baseline cannot silently resume old nodes.

Skill v27 and the shared provider prompt require actually examining relevant
raw evidence, rebuilding the explanation across the requested scope and then
reviewing it. Old investigation text is not a substitute for reading source,
viewing relevant images or discriminating geometry/state evidence. This does not
require every tool on every draw, a minimum runtime, different stage counts or a
prescribed list of rendering techniques. Matching conclusions remain valid when
independently supported. Structural freshness checks do not certify semantic
correctness.

Starting reanalysis invalidates task completion until a new publication. A later
failed publication also invalidates completion; reading old successful quality
does not clear it. A valid retry resolves the failure. A typo in a flow identity
is recorded as a failed attempt rather than an imaginary diagram that must be
published. Ordinary cached preparation after a successful new publication does
not restart analysis.

## Texture display

The automatic illustration policy retains 0–1 for readable images, including
readable HDR/depth content, binary masks, sparse bright masks and isolated
outliers. It adapts the complete selected subresource's effective distribution
only when useful content is substantially invisible, clipped or lacks visible
contrast. Explicit ranges remain explicit; comparisons share a common mapping.
Float-bit decoded previews follow the same policy. No GPU resource is modified.

## Deleted conversations

Deletion first persists an identifier-only cleanup request, then cancels owned
tasks, removes the conversation and forgets monitor windows/tails. The background
cleanup removes proven-owned JSONL, task reports, locks, ACP stderr and disposable
workspaces. Workspace-owner and existing deletion records are checked alongside
report ownership, including when the report itself no longer exists.

Deletion markers prevent delayed task/report writers from recreating conversation
contents. Pending requests survive immediate application shutdown and are retried
on startup; conversation restoration filters these deleted IDs. The tombstones
contain IDs, not chat text. Captures, projects and authored outputs are retained;
known controller launch files are removed even from a retained output workspace.
Shared vendor/account logs are not owned by one conversation and are not erased.
Codex tasks already use ephemeral threads with history persistence disabled.

## Connection capabilities

The shared Agent tools configuration now supports `inheritDesktopCapabilities`
and `nativeShellEnabled`. Both are enabled for this user's requested setup, while
preserving the existing server and connection settings. Codex app-server, ACP
and API routes consume the same admitted local Skill and MCP catalog. Codex also
receives the user's effective native capability settings through the protocol;
secrets in server definitions are not placed on the process command line. Each
connection keeps its own existing login.

Explicit disabled integrations, tool filters and approval restrictions remain
effective. A Skill being installed is not evidence of a running browser or
computer service. Available services are determined by their actual handshake
and tool inventory; desktop-only services without a usable endpoint are not
reported as connected. Bread Host aliases remain routed through the current
task's capture binding.

API command execution and ACP terminal callbacks now use bounded local process
lifetimes, cancellation and output limits. This is local execution under the
user's permissions, not a directory sandbox. A requested sandbox or approval
requirement that this route cannot enforce remains unavailable with a reason.
Optional external MCP startup failures are isolated from Bread's main tools.
Each connection owns its MCP task group separately: a transport failure cannot
cancel the main analysis task or skip later healthy services. Real HTTP timeout
and HTTP 503 fixtures surrounded by healthy stdio services cover this boundary,
including successful calls and process cleanup. Actual user cancellation still
propagates. Tool calls use the configured timeout (60 seconds by default).

## Verification boundary

Regression fixtures exercise fresh drafts, preserved baselines, repeated
preparation, refresh/restoration, failed-publication recovery and display-range
decisions. No paid Agent run or user diagram regeneration is part of this change.
The user will test the revised analysis through Bread AI.

No-turn native Codex verification passed `thread/start`, the integration gate,
MCP inventory and installed-App queries with the user's effective permissions.
The shared API/ACP MCP pool also completed actual initialize/list-tools handshakes
in about 4.2 seconds: auggie, documentation, UEX, Node and computer/browser tools
connected; an obsolete executable path and two inactive services were isolated.
No model turn, browser action, business tool operation or real conversation
deletion was used for verification.
