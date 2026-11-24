# Bread AI connection audit — 2026-10-14

Target runtime: **1.13.28**. This audit covers the configured Codex, Claude ACP,
Cursor ACP and OpenAI-compatible API connections, together with shared API/ACP
failure handling. It does not claim that external service availability can be
guaranteed or that every unconfigured vendor has been tested against a live account.

## Latest incident

The screenshot's `Selected model is at capacity` is a provider turn failure,
not a failure to establish the Bread tool connection.

Task logs under `%LOCALAPPDATA%/ReplayLab/ai-tasks/` show:

| Task | Observed sequence | Duration |
| --- | --- | --- |
| `e661b0fcc2f24d4c8b36cb5ce7168bc9` | Native handshake succeeded; opened organization session; obtained full shader source and outline; provider failed with capacity error. | 254.070 s |
| `d0fa1955b5614cd5b658dc2f392c11f2` | Native handshake succeeded; provider again failed with capacity error before substantive work. | 8.578 s |

The recovered `organized.hlsl` was 322,241 bytes and still matched the original
source hash beginning `57dab`. Recovery preserved a working copy; it did not
complete the requested rewrite. The recovery manifest appeared as failed in the
UI because its activity lacked an explicit successful terminal state. That
display defect is corrected independently of the failed AI task.

The screenshot selected `gpt-6-sol`. The saved Codex profile had subsequently
changed to `gpt-6-astra`, with Cursor selected as the active connection. This
audit preserved those user selections and credentials.

## Live checks

Account/catalog and ACP handshake checks were followed by an isolated minimal
generation request: “Connection health check only. Reply exactly OK. Do not call
tools, read files, or edit files.” No shader task was executed by these probes.

| Configured route | Login/catalog/handshake | Actual generation |
| --- | --- | --- |
| Codex / `gpt-6-astra` | Passed; model listed | `OK`, 3.891 s, using the same isolation construction as native tasks |
| Cursor ACP / `default[]` | Passed; model listed | Passed, `end_turn` with nonempty response, 20.079 s |
| Custom OpenAI-compatible API / `gpt-5.6-sol` | First catalog probe failed; subsequent catalog probe passed | `OK`, 4.875 s |
| Claude ACP / requested `claude-sonnet-5` | Login and ACP handshake passed; adapter exposes model aliases | Model selection accepted after compatibility fix; actual generation rejected by provider with HTTP 403 `Request not allowed`, 5.579 s |

The first API probe did not retain a detailed cause; no unsupported explanation
is assigned to that transient failure. Claude's 403 proves that generation is
currently denied, but does not identify whether the provider's account, model or
another access rule is responsible. Reconnecting or silently changing the model
would not establish that the underlying permission issue was repaired.

The first temporary Codex generation harness incorrectly reused the subscription
account/catalog connector factory without native task hardening, and was correctly
rejected by tool isolation. A corrected harness used `native_codex_argv`, the
managed profile environment and a fresh OS temporary working directory. A
no-generation preflight confirmed empty MCP/Apps state before the successful
generation request. Production native tasks already use this construction; the
guard was not relaxed and global plugins were not disabled.

Probe evidence is stored under the OS temporary directory as
`bread-connection-audit-20261014.json`, `bread-model-health-20261014.json`,
`bread-codex-isolation-health-20261014.json`,
`bread-codex-health-retest-20261014.json` and
`bread-claude-health-retest-20261014.json`. These are point-in-time checks, not
ongoing monitors.

## Implementation changes

### Codex recovery

- Preserve structured provider error codes through terminal turn failures.
- For an authoritative terminal capacity failure with no unfinished tool effects,
  resume at most twice, after 2 and 5 seconds, in the same thread/model/workspace.
- Continue unfinished work using existing context and tool results. Do not resend
  the original request, attachments or Skill on every attempt.
- Do not add retries when the provider already reports its own retry. Keep the
  original turn deadline and make backoff cancellable.
- Do not automatically retry authentication, quota, policy or configuration
  failures. Do not silently switch models or rewrite credentials.
- If cancellation arrives before a new turn ID is known, close the owned
  connector rather than pretending an interrupt for the previous turn cancelled it.

### ACP protocol and lifecycle

- Emit session metadata in the installed ACP SDK's correct shape rather than
  nesting it under `_meta.field_meta`.
- Preserve explicit model selection. For the installed Claude adapter 0.76.0,
  accept only a successfully selected, publicly advertised alias after the
  requested full model ID was submitted; retain requested and reported identities.
  No client-side family/version alias guessing is used.
- A model test succeeds only with `end_turn` and nonempty response. Refusal,
  cancellation, token-limit stop and empty responses are not reported as success.
- Make startup/session cleanup bounded; do not retry prompts after uncertain
  execution or conceal failed transport cleanup. Cancellation still cleans up
  when the vendor's cancel operation fails.
- Restrict authentication-required handling to the actual authentication RPC
  contract. Do not treat every generic `-32000` error as an invitation to log in.
- Preserve Claude's known structured RPC error kinds. A genuine vendor 403 is
  reported as permission denied, not generic connection failure or success.
  The fallback parser is restricted to the vendor RPC error wrapper; it never
  treats stderr, model prose or tool output as authoritative status.

### API adapters and shared diagnostics

- Apply a default 120-second request timeout and at most two SDK request retries,
  preserving explicitly configured timeouts.
- Do not restart an entire agent task or replay a partially consumed stream.
- Correct Azure v1 endpoint/deployment routing, Gemini thinking-budget mapping
  including zero, and explicit rejection of unsupported Vertex URL overrides.
- Classify authentication, permission, quota, rate limit, capacity, timeout,
  transport, context, model and configuration failures with stable localized IDs.
  Structured request errors take precedence over coincidental capacity wording.
- Preserve the original cause of the native task deadline error.
- Show a localized actionable reason while retaining bounded, redacted technical
  detail. Redaction now covers quoted JSON secrets and URL user information.
- Mark successful local draft recovery as completed while retaining the failed
  AI task status. A recovered file is not evidence of completed organization.

The Shader Skill's remaining checkpoint wording was also aligned with the actual
tool contract: a checkpoint only persists the draft. It cannot compile, validate,
apply or start live edit. Whole-source organization and review precede final
physical validation. The Skill version is 84.

## Verification and limits

Restart acceptance exposed a second consequence of the failed shader task:
all three saved shader replacements restored successfully, but the open
organization draft correctly refused automatic compile monitoring. Restore had
mistakenly counted that refusal as an editor failure and warned that the project
failed to restore. The follow-up fix treats that exact, verified workspace as
`deferred` source work without starting a watcher or modifying the draft. The
existing source preview is read-only; the Agent's working copy/proposal remains
editable through its organization tools. This does not create a new writable GUI
document contract or imply that the draft is reviewed and ready to apply.
The real workspace was in `drafting`, with saved source still matching its
verified project source. Read-only preview discovery now covers that exact case;
a stale authorization registry entry no longer hides the fallback preview.
Preview locators remain in-memory and grant no watcher, source-write or replay
authority. The read-only view also no longer advertises Ctrl+S automatic compile.
Live UI readback confirmed three unique workspace entries; the affected shader
was visible and selected with the exact saved hash, read-only and with no new
compile/apply. A subsequent acceptance check caught a separate summary bug:
the automatic-restore callback treated any item `reason` as fatal, including
the successful deferral reason. Summary diagnostics now select failure reasons
from failed items and retain the deferred count separately.

Tests exercise actual installed SDK request objects with local/fake transports,
including 401/403/400, quota versus 429, 503 recovery, timeouts, cancellation,
partial-stream failure, ACP subprocess cleanup, model aliases, and Codex capacity
recovery after completed versus unfinished tool effects. Native compilation,
translation gates and frozen-module verification accompany deployment.

Combined connection regression: **292 passed, 4 skipped, 4 subtests passed**
(16.01 s). The skips concern Windows symlink/permission-test availability.
Integration-version checks: **3 passed**. Translation synchronization, complete
catalog compilation and lint passed with 5,124 finished entries and no unfinished
entries. The native Development build completed with zero warnings/errors.
Restore/client/scheduling regression: **234 passed, 229 subtests passed**;
the final added capture-change/cancellation cases passed in a focused **3-test,
2-subtest** run. Workspace panel regression: **104 passed**, including live-to-read-only
status changes and save-button behavior. These targeted runs were not repeated
only to obtain a combined count.
Console/discovery regression: **100 passed**, including eight new file-backed
preview cases. A read-only projection of the actual affected workspace confirmed
its 322,241-byte `57dab` source, eligibility, no live/watch IDs and disabled
automatic apply without writing project data.
Automatic-restore callback regression: **22 passed**, exercising the actual
console/status/event callback with three applied, two live-edit enabled and one
legitimate read-only deferred item, plus genuine failure and invalid-receipt cases.
Runtime **1.13.28** was built and deployed to `x64/Development/replaylab`;
24 frozen modules were compared against current source, the output manifest
passed validation, and all 21 canonical bridge integration files were verified.
Application shutdown used the normal close path after checking that open shader
documents were clean and no AI task process was running. The same capture was
opened again after deployment.

Final installed-build restart acceptance (PID 45844) completed with **3/3 shader
replacements applied, 2 automatic editors enabled, 1 source-only editor deferred,
0 failed items and no console warnings**. The terminal event was `info/complete`.
Evidence: `%TEMP%/bread-1.13.28-final-restart-acceptance.json`. This verifies
restoration and the draft deferral contract, not completion of the shader rewrite.

The live Claude access denial remains external and unresolved. The successful
current Codex model probe does not prove that the screenshot's different model
has available capacity, or that either model will remain available later.

Unconfigured API vendors are covered by adapter/SDK tests, not live account
requests. One pre-existing boundary remains for local API servers: model SDKs
can inherit proxy environment settings while the lightweight HTTP catalog probe
does not. Correcting that across synchronous/asynchronous client caches requires
explicit per-task HTTP-client ownership; it was not bypassed through global
environment changes. The configured API endpoint tested here is remote HTTPS.

Protocol reference: the official [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server)
describes terminal turn errors and structured error information. Recovery tests
also use the schema generated by the locally installed Codex binary rather than
assuming the public enumeration is exhaustive.
