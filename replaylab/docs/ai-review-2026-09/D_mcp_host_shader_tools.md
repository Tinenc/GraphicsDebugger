# Audit D — MCP host shader tools: why `plan_shader_toolchain_route` / `plan_shader_recovery` returned `route-unavailable`

> Historical incident analysis. Its Agent-runtime references are obsolete; only independently verified MCP tool evidence remains useful.

Failure under audit: user asked "反编译这个shader" (decompile a D3D12 DXIL `ps_6_6` pixel shader,
EID 11329, entry `MainPS`, no embedded source). Child ran `inspect_shader` → `get_shader_toolchain_capabilities`
(both OK, catalog advertised integrity-verified routes `community-renodx-dxil-hlsl`,
`dxc-hlsl-dxil-exact-profile`, `dxc-dxil-native-disassembly`), then `plan_shader_toolchain_route(...)`
and `plan_shader_recovery(...)` both FAILED with `ToolExecutionDiagnosticError` / errorCode
`tool-configuration` / diagnosticCategory `route-unavailable`, **identical** operationFingerprint
`e7998398…`. Log: `/home/claude/work/logs/57c9176355fb41b99a9696bfbd265689.summary.txt`.

Bottom line up front: **the two plan tools require `shader_path` to be an already-exported captured-shader
*binary file on disk*. Nothing the child had run produces such a file.** The first thing both tools do is
open that path; the open failed (`shader path must be a regular non-symlink file`), which the deterministic
classifier maps to `route-unavailable`. The identical operationFingerprint on both calls (it is the SHA-256
of the arguments) proves both calls carried the same bad `shader_path`. The real reason string is then
**deliberately stripped** at the agent boundary and replaced with the generic "The tool result or local
configuration could not be validated." The capability catalog is file-independent, so it kept advertising the
routes as ready — a genuine advertise-vs-plan inconsistency, but *not* one caused by manifest staleness.

---

## 1. Schema of the two tools, meaning of `shader_path`, and every `route-unavailable` condition

### Exact schema (server.py)

Both are thin wrappers over `ToolchainCatalog`:

`plan_shader_toolchain_route` — `server.py:4483`

```python
@mcp.tool()
def plan_shader_toolchain_route(
    shader_path: str,
    stage: str,
    editable_format: str = "",
    capture_encoding: str = "",
    target_api: str = "",
    allow_reconstructed: bool = True,
) -> str:
    ...
    result = _shader_toolchain_catalog().select_round_trip_for_file(
        shader_path, stage,
        editable_format=editable_format or None,
        capture_encoding=capture_encoding or None,
        target_api=target_api or None,
        allow_reconstructed=allow_reconstructed,
    )
    ...
    except Exception as error:
        return _shader_err(error, "route-plan")
```

`plan_shader_recovery` — `server.py:4516` — identical arg list, calls
`_shader_toolchain_catalog().plan_recovery_for_file(...)`, phase `"recovery-plan"`. The catalog itself is
rebuilt fresh per call from process-owner fixed paths (`server.py:678 _shader_toolchain_catalog`).

### What `shader_path` means

It is a filesystem path to the **raw captured shader binary** (DXBC/DXIL/SPIR-V container bytes). Both
`select_round_trip_for_file` and `plan_recovery_for_file` open it as their *first* step via `_read_shader_file`
before any routing runs:

`toolchains.py:2041` (`select_round_trip_for_file`)
```python
unused_shader, data = _read_shader_file(shader_path)
detection = detect_shader_format(data, capture_encoding)
```
`toolchains.py:2083` (`plan_recovery_for_file`) — same `_read_shader_file(shader_path)` first line.

`_read_shader_file` (`toolchains.py:570`):
```python
def _read_shader_file(path):
    source_path = Path(path).expanduser()
    if source_path.is_symlink() or not source_path.is_file():
        raise ToolchainRoutingError("shader path must be a regular non-symlink file")
    ...
    if size <= 0 or size > MAX_SHADER_BYTES:
        raise ToolchainRoutingError("shader file must contain 1..%d bytes" % MAX_SHADER_BYTES)
    ...
    if len(data) != size:
        raise ToolchainRoutingError("shader file changed while it was being read")
```

**There is no host-side resolution of an `rdoc://` URI, a ResourceId, an EID, or a workspace-relative name.**
The string is passed to `Path(...)` verbatim. The plan tools give the agent **no way to obtain the file** —
neither `inspect_shader` (`server.py:2521`, just proxies the bridge and returns reflection/disasm JSON) nor
`get_shader_toolchain_capabilities` (`server.py:4470`, takes only `stage`) writes a shader binary to a path
the model is told about. The only tools that materialize such bytes on disk are the workspace tools
(`prepare_shader_workspace` exports a catalog and copies the baseline; `get_current_shader_representation`
can export raw bytes below the representation root) — none of which the child had called.

### Every condition that yields `route-unavailable`

`route-unavailable` is produced by the deterministic classifier for **any `ToolchainRoutingError` whose
message does not match the two special buckets** (`diagnostics.py:178`):

```python
elif isinstance(error, ToolchainRoutingError):
    if _contains(message, r"unsupported shader format", r"unsupported target api"):
        category = "unsupported-format-or-api"                     # NOT route-unavailable
    elif _contains(message, r"ambiguous", r"equal.*route", r"choose an explicit"):
        category = "route-ambiguous"                               # NOT route-unavailable
    else:
        category = "route-unavailable"
        actions = ["preserve-evidence","inspect-native-ir","try-registered-alternate-route",
                   "construct-source-if-compile-route-exists","install-or-register-format-provider"]
        ai_may_author_source = True
```

Those five `actions` are exactly the `diagnosticAllowedActions` in the failed log record — confirming the
error was a plain `ToolchainRoutingError`. Every `ToolchainRoutingError` raise that reaches this `else`
branch therefore surfaces as `route-unavailable`. The complete list (all in `toolchains.py`, plus
`profiles.py`), grouped:

**A. File / input problems (raised before any routing) — reached first:**
- `toolchains.py:573` `"shader path must be a regular non-symlink file"` (path missing, a dir, or a symlink)
- `toolchains.py:577` `"shader file must contain 1..N bytes"` (empty / oversize)
- `toolchains.py:582` `"shader file changed while it was being read"`
- `toolchains.py:2044 / 2086` `"captured shader format could not be detected"`
- `toolchains.py:541` `"shader format hint %s contradicts detected %s evidence"` (capture_encoding vs bytes)

**B. Format/API contradictions and derivation gaps:**
- `select_round_trip` / `plan_recovery`: `"automatic round-trip planning requires a captured binary shader
  format"` (`:1858/:1378`); `"capture encoding %s contradicts detected container %s"` (`:1865/:1386`);
  `_require_format_for_api` mismatch; `"SPIR-V target API/environment cannot be derived from the module"`
  (`:1871/:1393`); `"auto-route editable format must be textual"` (`:1899`); `"recovery editable format must
  be textual"` (`:1403`)
- `select_round_trip_for_file:2063` `"the automatic route needs a compiler target profile that cannot be
  derived from this shader format"`
- `select_round_trip:1986` `"the selected route requires the physical target API; it will not be inferred…"`
- profile extraction errors (`profiles.py`, e.g. `:122` `"DXIL program version does not declare Shader
  Model 6+"`, malformed-container variants) — all `ToolchainRoutingError`.

**C. No integrity-valid route exists (the "advertised-but-unavailable" case):**
- `find_path:1229` `"no integrity-valid %s -> %s route supports stage %s…"` — raised when
  `select_round_trip` can find no round trip (`:1976 "no complete automatic shader round trip is available"`).
  This fires when the on-disk tool binaries fail their fresh SHA-256 integrity probe
  (`tools.py:229 _integrity_error`).

**D. Ambiguity → NOT route-unavailable** (`route-ambiguous`): `find_path:1253`
`"auto-route is ambiguous … choose an explicit route ID"`; `strict_spirv_validator:1127`.

**E. Unsupported format/API → NOT route-unavailable** (`unsupported-format-or-api`):
`normalize_format:194`, `_normalize_target_api:291`.

### Most likely condition for this run

Given `capture_encoding=DXIL`, `editable_format=HLSL`, `target_api=D3D12`, `allow_reconstructed=true`,
`stage=fragment`, **and** the capability probe having just reported renodx + dxc routes as integrity-verified
(so bucket **C** — missing/tampered binaries — is excluded, and the two DXC/renodx routes make bucket **D**
ambiguity unlikely and bucket **B** profile/API derivation succeeds for a real DXIL file with `ps_6_6`):

**The dominant cause is bucket A — `shader_path` did not point to a real exported shader file**
(`toolchains.py:573`, `"shader path must be a regular non-symlink file"`). Supporting evidence:
- Both plan tools open the same path *first*, before format/route logic; a bad path short-circuits every
  other condition regardless of the other five arguments.
- The child had no tool that produced an on-disk shader binary, so whatever it passed (an `rdoc://` URI, the
  ResourceId `1000000000000000860`, or a guessed workspace path) does not exist as a file.
- `operationFingerprint = arguments_sha256` (see §4). Both calls got the **identical** fingerprint
  `e7998398…`, which means both carried byte-identical arguments and failed the identical way — consistent
  with the same non-existent `shader_path` in both, not with two different route-topology failures.

`ps_6_6` itself is **not** rejected: `profiles.extract_target_profile` accepts any DXIL `major >= 6`
(`profiles.py:121-122`); there is no verified-profile allowlist in the plan path. The `ps_6_0`-only entries
in the verification JSON are documentation of what was smoke-tested, not a runtime gate (see §5).

---

## 2. Required call ordering, where it is (not) enforced, and whether it is documented

There **is** a real data-dependency ordering, but it is **not** the sequence the child attempted, and it is
**not** stated to the agent anywhere as the entry path.

The intended flow is a single high-level tool that internally does export → select-route → create-workspace
→ first decompile:

- `prepare_shader_workspace(event_id, stage, workspace_path, …)` (`server.py:4874`) — "Export, uniquely
  select, create, and represent one event/stage shader." It calls the bridge `export_shader_catalog` for
  exactly that EID+stage (`server.py:4985`), verifies a single physical use, creates the workspace, and
  "starts one registered reconstruction attempt immediately whenever one exists." It takes **`event_id`/
  `stage`, not a `shader_path`.**
- `decompile_shader_workspace(workspace_path)` (`server.py:5279`) — reuses/produces the source from an
  existing workspace. Also takes a **workspace path, not a captured-binary path.**

So the route planning that the child tried to do by hand is done *inside* `prepare_shader_workspace`. The
`plan_shader_toolchain_route` / `plan_shader_recovery` / `run_shader_toolchain_round_trip_smoke` trio are
lower-level file helpers that presuppose an already-exported binary; they are effectively an advanced
offline-file surface, not the decompile entry point.

**Ordering enforcement:** dependencies are enforced structurally by argument types, not by a documented
state machine — `decompile_shader_workspace`/`compile_shader_workspace`/`validate_shader_workspace_candidate`
all take a `workspace_path` that must already exist (created by `prepare_shader_workspace`/
`create_shader_workspace`); ABI/organization tools re-check capture identity and source hashes at call time
(see the `capture-or-shader-identity-conflict` / organization branches in `diagnostics.py:57-137, 292-311`).
The plan tools enforce only "the file you named must exist and be a shader" — they cannot know a workspace
"should" have been created first.

**Documentation to the agent:** none of this ordering, and in particular the fact that `shader_path` must be
a pre-exported file, is stated in the tool descriptions or the skill.
- `plan_shader_toolchain_route`'s docstring (`server.py:4492`) says only "captured binary -> editable text ->
  same binary" and calls the input "content … detected". `docs/SHADER_TOOLCHAINS.md:414` likewise:
  "`plan_shader_toolchain_route(shader_path, stage, …)` detects and selects a unique complete round trip
  without running tools" — with no statement of where `shader_path` comes from.
- `SKILL.md` (`/home/claude/work/repo/.agents/skills/renderdoc-shader-workbench/SKILL.md`) is deliberately
  *anti*-procedural. It tells the agent the stage map is "a map, not a checklist" (line 98), that the Host
  "must not select a compiler, decompiler, provider, MCP route, recovery candidate, Skill order, or ordinary
  tool order" (lines 20-24), and that "A route ID is diagnostic provenance, not an immutable permission
  token" (lines 86-89). It never names `prepare_shader_workspace` as the entry point, never says the plan
  tools need an exported file, and explicitly discourages treating any ordering as mandatory. The child
  faithfully followed the skill's "plan the route first" mental model — and the skill pointed it at the one
  tool family that cannot start from a bound EID.

This is the core usability defect: the skill forbids the Host from choosing tool order, the docs describe the
plan tools abstractly, and the only tool that turns a bound EID into a decompile (`prepare_shader_workspace`)
is never surfaced as the thing to call for "decompile this shader."

---

## 3. Shader tool count and the subset actually needed for "decompile DXIL → readable HLSL"

`server.py` registers **136** `@mcp.tool()` functions total; **~52** are shader/workspace/material/toolchain/
organization/live-edit tools. The shader-relevant registrations include:

`inspect_shader`, `get_current_shader_representation`, `get_shader_toolchain_capabilities`,
`plan_shader_toolchain_route`, `plan_shader_recovery`, `run_shader_toolchain_round_trip_smoke`,
`create_shader_workspace`, `try_shader_workspace_recovery_candidate`, `prepare_shader_workspace`,
`decompile_shader_workspace`, `get_shader_workspace_representation`, `promote_shader_workspace_explanation`,
`compile_shader_workspace`, `migrate_shader_workspace_toolchain`, `get_shader_workspace_status`,
`begin_shader_workspace_organization`, `review_shader_workspace_organization`,
`validate_shader_workspace_candidate`, `apply_shader_workspace_candidate`,
`list_shader_workspace_family_uses`, `apply_shader_workspace_candidate_family`,
`apply_shader_workspace_shared_resource`, `register_active_project_shader_replacement`,
`rollback_shader_workspace_shared_resource`, `rollback_shader_workspace_override`,
`rollback_shader_workspace_override_family`, `start/poll/stop_shader_workspace_watch`,
`start/poll/list/migrate/stop_shader_workspace_live_edit`, plus the material-session family,
`query_active_shader_asset_evidence`, `get_active_shader_workspace_path`, `seed_active_shader_workspace`,
`restore_current_shader`, `seed_shader_workspace_from_replay_project`, etc.

**There is no single high-level `decompile_shader` / `reconstruct_shader` tool** that takes an EID and returns
readable HLSL. The closest thing is the two-call minimum:

1. `prepare_shader_workspace(event_id, stage, workspace_path)` — exports, selects the route internally,
   creates the workspace, and runs the first reconstruction attempt.
2. `decompile_shader_workspace(workspace_path)` — publishes the readable source path.

Optionally `inspect_shader` first for identity, and `get_shader_workspace_representation` /
`get_shader_workspace_status` to read the result. So a *plain* "decompile to readable HLSL" is achievable in
**2 calls** — **if** the agent knows to start at `prepare_shader_workspace`. The failure mode here is that
the abstract skill steered the child into the low-level `get_shader_toolchain_capabilities` →
`plan_shader_toolchain_route` → `plan_shader_recovery` file-planning path, which cannot bootstrap from an EID
and needs a file it was never given a way to create. If instead the agent tries to satisfy the full
"validated reconstruction" contract (organize, compile, ABI-validate) it must orchestrate roughly
`prepare_ → decompile_ → begin_organization → review_organization → validate_candidate (compile+ABI)` — 6-10
low-level calls, each with its own identity/hash re-checks.

---

## 4. How the real reason is converted to the generic message; is the human-readable reason preserved?

The host **keeps** the real reason; the agent boundary **strips** it.

**Host side (reason present):** `_shader_err` (`server.py:177`) returns a JSON envelope that still contains
the raw text:
```python
return json.dumps({
    "error": str(error),                                   # e.g. "shader path must be a regular non-symlink file"
    "type": type(error).__name__,
    "diagnostic": classify_shader_failure(error, phase=phase),  # diagnostic.message is the bounded raw text
})
```
`classify_shader_failure` (`shader_workspace/diagnostics.py:140`) builds `{category, phase, exceptionType,
message, retryable, allowedActions, aiPolicy}` and puts the bounded raw text in `message`
(`diagnostics.py:460-478`).

**Agent side (reason dropped):** `replaylab_agent/tool_service.py`:
- `_bounded_model_tool_diagnostic` (`:642`) re-reads the host `diagnostic` but **retains only**
  `category, phase, retryable, allowedActions` — the `message` field is intentionally discarded
  (`:667-675`). If anything fails admission it returns `_untrusted_model_tool_diagnostic` with
  `category:"unknown", allowedActions:[]`.
- The tool error is raised as `ToolExecutionDiagnosticError` (`:1553`) whose *string* is a template that
  contains only category/phase/retryable/actions (never the host `message`), with
  `model_diagnostic=diagnostic` and `operation_fingerprint=arguments_sha256` (`:1559`).
- At the durable boundary (`:1680`) it maps to the model-visible record:
  ```python
  error_code, error_summary = _tool_failure_diagnostic(name, exc)
  data = {"status":"failed","errorType":…,"errorCode":error_code,"errorSummary":error_summary, …}
  ```
  `_tool_failure_diagnostic` (`:560`) → for a `ConfigurationError`/generic tool failure returns code
  `"tool-configuration"`, and `_TOOL_FAILURE_SUMMARIES["tool-configuration"] = "The tool result or local
  configuration could not be validated."` (`:525`). It then re-admits the diagnostic via
  `_revalidate_model_tool_diagnostic` and attaches `diagnosticCategory/Phase/Retryable/AllowedActions` and
  `diagnosticOperationFingerprint` (`:1694-1712`). This record is exactly the log line at event [616].

The stripping is **by design**. The comment at `tool_service.py:515-519` states it explicitly:
> "Task events are durable and user-visible. Never copy an exception message into them: bridge/tool
> diagnostics can contain capture paths, provider credentials, or an entire rejected result. `errorCode` is
> the stable localization/diagnostic key and the English text is only a bounded fallback…"

`operationFingerprint`: `arguments_sha256 = evidence_digest(arguments)` (`tool_service.py:1395`) — the SHA-256
of the tool arguments. Same args → same fingerprint, which is why `plan_shader_toolchain_route` and
`plan_shader_recovery` produced the **identical** `e7998398…` (both got the same six argument values,
including the same bad `shader_path`).

**Is the raw reason kept anywhere visible?** No — not anywhere the LLM or user sees:
- The model/user record carries only `errorSummary` + category/phase/actions/fingerprint. The concrete
  `"shader path must be a regular non-symlink file"` never appears.
- `tool-audit.jsonl` (written by `mcp_proxy._append_audit`, `mcp_proxy.py:1211`; path built at
  `runtime.py:17362` and `:18654`) stores an `error` field, but on the failure path it is
  `type(exc).__name__ + ": " + str(exc)` truncated to 2048 (`mcp_proxy.py:1120-1128`, `:727-736`). Since the
  exception raised is the already-sanitized `ToolExecutionDiagnosticError`, `str(exc)` is the category/phase
  template — **still not** the host's `message`. The host `_shader_err` envelope with the real text is
  consumed and reduced at the tool_service boundary before the audit line is written, so the audit file
  captures the category and allowed actions but not the underlying "why the file could not be opened".

Net: the one fact that would have let the child self-correct — "the path you passed is not a file; you must
export the shader into a workspace first" — is discarded at every layer the model or user can read.

---

## 5. Verification-manifest gating — could a stale manifest make every route "unavailable" while capabilities still advertise them?

**Route availability is gated on the loaded tool *registry* (the generated config JSON), not on the docs
verification manifest, and — crucially — capability advertisement and route planning use the identical
integrity gate, so they cannot disagree on integrity.**

- Integrity is a fresh, per-call re-hash of the on-disk binaries against the SHA-256 recorded in the loaded
  registry: `ToolRegistry.operation_integrity` (`tools.py:710`) → `_integrity_error` (`tools.py:229`):
  ```python
  actual = _file_sha256(operation.executable)
  if not hmac.compare_digest(actual, operation.executable_sha256):
      return "executable SHA-256 changed after tool registry load"
  # …same for each fixed_file_hashes entry…
  ```
  `"available": error is None` (`tools.py:722`). The catalog also hashes the registry file itself at load
  (`toolchains.py:1001 actual_registry_hash = _sha256_file(self.registry.path)`).
- Both surfaces consume the **same** `_integrity()` map:
  - route planning: `matching_routes`/`find_path`/`candidate_paths` filter on
    `integrity[(tool_id, operation)]["available"]` (`toolchains.py:1171, 1211, 1318`).
  - capability probe `diagnostic_matrix` filters on the **same** key (`toolchains.py:2200, 2208`) and, when a
    route is configured but not integrity-available, labels it `tool-integrity-unavailable`, *not*
    `route-ready` (`toolchains.py:1503-1531`).
  So a stale/mismatched registry would make the routes show as `tool-integrity-unavailable` in **both**
  `get_shader_toolchain_capabilities` **and** the plan tools — a *consistent* degradation, and the plan tools
  would then raise `find_path`'s `"no integrity-valid … route"` (bucket C), still `route-unavailable`. That
  is **not** this run: the child reported the capability matrix listed the renodx/dxc routes as
  integrity-verified, i.e. integrity passed.
- `docs/SHADER_TOOLCHAIN_VERIFICATION.windows-x64.json` is a provenance/round-trip **record** (schema
  `renderdoc-ultra-shader-toolchain-verification/v1`), pinning tool commits/hashes and listing
  `verifiedRoundTrips` (all `ps_6_0`, plus a `renodx` `knownObservedFailure` note). It is **not** loaded by
  `ToolchainCatalog`/`ToolRegistry` as the availability gate — the registry config (produced by
  `toolchain_config.py`, which hashes real files at generation time) is. So a stale docs manifest cannot by
  itself flip routes to unavailable.

**However, there is a genuine advertise-vs-plan inconsistency here — just not a hash one.**
`get_shader_toolchain_capabilities(stage)` is **file-independent**: it enumerates format-edge readiness for a
stage and never touches a shader file. `plan_shader_toolchain_route(shader_path, …)` is **file-dependent**:
it must open `shader_path`, detect its format, derive a target profile, and require a *unique* best route in
each direction. The catalog can therefore truthfully advertise "renodx/dxc DXIL↔HLSL routes are ready for
fragment" while the plan call fails `route-unavailable` because the *file* it was handed does not exist (or,
in other scenarios, because the file's format/API/profile does not line up or the best route is not unique).
That mismatch — a file-independent "yes, routes exist" next to a file-dependent "route unavailable" — is
exactly what made the failure look contradictory to the child, and it is the design seam worth fixing
(e.g., have `plan_*` distinguish "input file invalid/absent" from "no route topology" instead of collapsing
both into `route-unavailable`, or have the capability probe note that a shader file must first be exported).

---

## 6. Complexity estimate

- **`server.py`:** 14,091 lines total; **136** registered MCP tools. **~52** are shader/workspace/material/
  toolchain/organization/live-edit tools. The contiguous "EXTERNAL SHADER TOOLCHAINS / WORKSPACE" block runs
  from `server.py:4467` to ~`8000` (≈3,500 LOC), with additional shader/project tools in the
  `10299–14019` range (active-project, workspace-path, seed/restore, replay-project seeding, resource
  export). Rough total server.py LOC devoted to shader/workspace tooling: **~4,500–5,000**. Supporting host
  modules add much more: `shader_workspace/workspace.py` (363 KB), `toolchains.py` (119 KB),
  `toolchain_config.py` (51 KB), `tools.py` (35 KB), `diagnostics.py`, `profiles.py`, `errors.py`, plus
  `shader_live_edit.py` (98 KB). The agent side adds `shader_routing.py` (127 KB) and the tool_service /
  mcp_proxy validation stack.

- **Validation steps per single tool call** (`tool_service.py` dispatch, ~`:1380–1743`), in order:
  1. tool-call-id sanitation; `arguments_sha256 = evidence_digest(arguments)` (fingerprint) — `:1390-1395`
  2. durable run-state session: `prepare_tool_call` with idempotency key + operation digest; replay if a
     prior identical call `completed`; block if a prior effectful call `dispatched` without a verified result
     — `:1396-1441`
  3. emit `tool-started` telemetry — `:1446-1455`
  4. `_tool_transaction` acquires the per-workspace/replay-lane lock — `:1481`
  5. `workflow_guard.check(name, arguments)` re-check under the lock (frozen effect ceiling / prior-failure
     freeze) — `:1489-1490`
  6. recovery-ledger capacity check for effectful calls — `:1491-1499`
  7. `_verify_live_scope` — re-reads `get_current_renderdoc_context` and validates the frozen capture-session
     identity — `:1501, 1745-1753`
  8. `mark_tool_dispatched` durable transition + audit `started` — `:1502-1514`
  9. `_execute` the actual host/bridge call — `:1517`
  10. `_tool_result_error` → on error, `_bounded_model_tool_diagnostic` + raise sanitized
     `ToolExecutionDiagnosticError` — `:1520-1561`
  11. second `_verify_live_scope` post-dispatch — `:1562`
  12. `validate_scoped_tool_result(name, arguments, result, scope)` — `:1563`
  13. `extract_event_artifact_receipt`, `extract_shader_asset_correlation_receipt`,
     `extract_shader_identity_receipt` — `:1566-1576`
  14. `canonical_source_result_sha256(result)` (result identity hash) — `:1578`
  15. `extract_shader_recovery_receipt` (binds recovery evidence to prior records) — `:1584`
  16. `extract_host_actual_work_ledger` — `:1598`
  17. `attach_host_evidence_receipt_to_result` (result_sha256 + ledger) — `:1606`
  18. durable `complete_tool_call` commit; on commit failure raise `MutationAuditUnavailable` rather than
     invite a retry — `:1613-…`
  19. append `completed`/`failed` line to `tool-audit.jsonl` (proxy path, `mcp_proxy._append_audit`).

  So roughly **15-19 discrete validation/receipt steps wrap every host tool call** — lease/lock, durable
  idempotency + reconcile, two live-capture identity re-verifications, effect-ceiling guard, result-schema
  and scope validation, four receipt extractions, a canonical result hash, an actual-work ledger, and a
  durable audit commit. The host tool body itself then adds its own layer (e.g. fresh SHA-256 integrity
  re-hash of every pinned binary, catalog registry-hash check, capture-identity/source-hash re-checks).

---

## Key file:line index

- `server.py:4483` `plan_shader_toolchain_route`; `:4516` `plan_shader_recovery`; `:4470`
  `get_shader_toolchain_capabilities`; `:4874` `prepare_shader_workspace`; `:5279`
  `decompile_shader_workspace`; `:177` `_shader_err`; `:678` `_shader_toolchain_catalog`.
- `shader_workspace/toolchains.py:570` `_read_shader_file` (`:573` the failing message); `:2031`
  `select_round_trip_for_file`; `:2071` `plan_recovery_for_file`; `:1836` `select_round_trip`; `:1176`
  `find_path` (`:1229` no-route, `:1253` ambiguous); `:2119` `diagnostic_matrix`; `:1503-1531`
  route-ready/tool-integrity-unavailable/route-unavailable categories.
- `shader_workspace/diagnostics.py:178-198` `ToolchainRoutingError` → `route-unavailable`.
- `shader_workspace/tools.py:229` `_integrity_error`; `:710` `operation_integrity`.
- `shader_workspace/profiles.py:94` `extract_target_profile` (`:121` SM6+ check; no ps_6_6 rejection).
- `replaylab_agent/tool_service.py:525` generic summary; `:560` `_tool_failure_diagnostic`; `:642`
  `_bounded_model_tool_diagnostic` (drops `message`); `:1553` raise sanitized error; `:1680-1712` model-visible
  failure record; `:1395` `arguments_sha256` (= operationFingerprint).
- `replaylab_agent/mcp_proxy.py:1211` `_append_audit`; `:1120-1128` failure audit `error` = sanitized string.
- `runtime.py:17362` and `:18654` `tool-audit.jsonl` path.
- `docs/SHADER_TOOLCHAIN_VERIFICATION.windows-x64.json` (ps_6_0-only round trips; provenance record, not a
  runtime gate). `docs/SHADER_TOOLCHAINS.md:410-420` tool descriptions.
- `.agents/skills/renderdoc-shader-workbench/SKILL.md:20-24, 86-98` "Host must not select route", "map not a
  checklist".
