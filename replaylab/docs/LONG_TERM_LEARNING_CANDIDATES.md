# Long-term learning candidates

ReplayLab records possible improvements without letting one task rewrite the
filesystem Skills, MCP tool implementation, profile configuration, or active
Agent engine. The candidate ledger is an evidence system, not prompt memory and
not an online self-modifying runtime. It does not restore the removed Custom
Agent or orchestration-registry architecture.

## Trust and storage boundaries

- `LearningCandidateStore` keeps state, evidence indexes, hash-chained
  transitions, and promotion operation state in a WAL/FULL SQLite ledger.
- Candidate content, validation plans, completed outcome receipts, exact task
  ledger receipts, registry snapshots, and promotion/rollback receipts use the
  existing `RunStateStore` content-addressed artifact store.
- Candidate content and external observations are untrusted data. They have
  strict schemas, size limits, scalar/path restrictions, and content hashes.
  They are never rendered into the system/developer prompt or treated as a
  higher-priority instruction.
- The candidate identity is based on immutable content, target, risk, scope,
  failure fingerprint, and supersession identity. The registry version is
  recorded as evidence metadata, not included in that identity, so validation
  can intentionally span registry versions.

The state machine is linear and has no model-selected shortcut:

```text
observed -> reproduced -> shadow -> validated -> promotion-ready -> promoted
    |            |           |          |                |
    +------------+-----------+----------+--------------> rejected
                                                             
promoted -> rolled-back
```

Even low-risk knowledge requires independent observations, reproductions,
successful shadow runs, validation, tasks, and Provider/toolchain diversity.
Capture/Shader-scoped candidates also require distinct stable capture identities
derived from the capture ID or raw Shader identity. Ephemeral process/session
UUIDs never manufacture diversity. Repeated evidence from one task/run is
set-deduplicated. Failed observations and reproductions do not count as
successful Provider, capture, or registry diversity, and any unresolved
host-verified shadow failure blocks validation. Provider and registry diversity
thresholds are capped by the distinct routes that are actually configured, so a
single-provider deployment cannot wait forever for a nonexistent second route.

`skill-draft`, `tool-config-draft`, and `executable-draft` have enforced minimum
risk levels. Executable content cannot be labelled as low-risk knowledge.

## Compact success fast path

An ordinary completed task does not start or advance the learning state
machine. `LearningShadowScheduler` stores only a bounded structured success
rollup: stable request/physical-scope/authority IDs, a series fingerprint, the
latest exact receipt fingerprint, result and evidence digests/counts, and an
aggregate success count. Request text, response prose, context bodies, plans,
workflows, reasoning and raw tool results are excluded. A task ID can increment
one series only once within the bounded de-duplication window, and at most 4096
series are retained.

The series fingerprint deliberately excludes the result digest and UI snapshot
ID so repeated successes for the same Host request identity and physical target
aggregate even when model wording or a UI projection changes. The exact receipt
fingerprint still covers the complete compact summary. The Host-provided request
digest is used so attachment identity is not lost.

Prompt-sized request/context artifacts are created only after a failure-derived
candidate has met its reproduction threshold, matches the later successful
task, and is admitted by a trusted validation runner. Failed, partial and
blocked outcomes never enter the success rollup; they go only through failure
candidate intake. Likewise, a completed Shader route trace is retained only
after two independent typed failures for the same target/route can make that
later success actionable.

Conversation memory follows the same projection rule without weakening exact
replay. A completed outcome projects one short structured receipt and one
bounded natural-language conclusion into later prompts; duplicate key-result
and answer-summary copies are removed, including from legacy summaries. Full
terminal prose and reusable read-only execution receipts remain in prompt-free,
content-addressed stores. Exact cache hits therefore still avoid a model/tool
rerun, and a same-scope meta follow-up can retrieve the full prior answer by its
result digest.

## Proof that the candidate was exercised

A normal successful task cannot be relabelled as candidate evidence after it
finishes. Before a shadow or validation run is created, a scheduler must call
`prepare_validation_run_binding`. That freezes:

- candidate ID and exact candidate-content SHA;
- shadow/validation mode and a content-addressed validation plan;
- task, request, acceptance-contract, Provider/toolchain and registry digests;
- capture session, snapshot, EID, stage, resource and raw Shader SHA when
  applicable.

The binding digest is the immutable `RunStateStore.binding_sha256` used when
the run is created. The same full binding is copied into the durable terminal
payload. Evidence admission then verifies the frozen binding, physical target,
completed outcome, exact task ID/run, Provider/toolchain, reusable
`TaskExecutionLedger` receipt, and result digest. A cloned outcome with another
capture identity or an unrelated successful task therefore cannot add a count.

Production validation uses a separate scheduler and a trusted runner registry.
The candidate's strict `replaylab-learning-validation-procedure/v1` document
names only a code-admitted runner/version, stable check IDs, modes, task kinds,
bounded timeout and inert JSON configuration. It cannot grant a tool or effect,
is never appended to a prompt, and becomes executable only after the selected
runner admits the exact procedure digest.

`LearningShadowScheduler` persists compact success rollups, opportunities,
jobs, attempts, validated promotion handoffs, and promotion attempts in a
WAL/FULL SQLite ledger. A later ordinary task supplies an exact request, target,
Provider and registry opportunity plus only its structured success digests and
counts; its prose and full result are not candidate evidence. The scheduler
creates a different task ID, freezes the validation binding before dispatch,
and admits evidence only from the dedicated terminal run and exact
execution-ledger receipt. Jobs are content-deduplicated and use cross-process
leases, expired-lease recovery, global concurrency limits,
per-tick/per-job/per-candidate rolling budgets, bounded exponential backoff and
deterministic jitter. If a process stops after terminal commit, the next
scheduler reconciles that committed payload without rerunning the executor.

AgentHost notes opportunities only after the ordinary run's durable terminal
commit. The qrenderdoc intent path performs one bounded maintenance page
(by default, at most one validation job, one promotion handoff, and one health
rollback) after the visible terminal event and queue acknowledgement. Idle
worker polls run the same bounded page even when there is no new user intent, so
a crash-recovered or backoff-delayed job does not depend on another request to
make progress. All scheduler exceptions remain bounded diagnostics and cannot
rewrite the user task. A
scheduler without an installed trusted executor cannot run validation. The
generic lane rejects effectful opportunities; reconstruction validation needs
a separately reviewed runner that proves an isolated temporary workspace
instead of borrowing the user's live workspace/replay authority. The built-in
runner accepts only exact allowlisted managed Skill procedures and the one
code-owned `shader-recovery-hints` v2 procedure. The latter is parsed as inert
JSON and is never executed as code, used to order tools, used to choose a
compiler/decompiler, or used to cap attempts/retries. Its shadow runner validates
typed production observations: an exact failed capability, a later successful
Agent-chosen route, and accepted route, compile, and ABI receipts for the same
physical Shader target. A schema-only simulation cannot count. After formal
promotion, a new Agent may receive matching hints as optional soft context with
explicit `mayIgnore`, `mayCombine`, `mayReverse`, and
`maySelectUnlistedRoute` semantics. Candidate JSON cannot select or manufacture
an executor, tool, effect, route, or authority.

The runtime may derive this one code-owned hint candidate automatically only
after at least two independent tasks report the same typed route failure and a
later independent task proves an Agent-selected success with the required
receipts. Candidate indexes and frozen Host route catalogs are not interpreted;
targets without a complete physical Shader identity remain knowledge
observations. The hint records a failure fingerprint, hashed target identity,
stage/conditions, successful route suggestion, confidence, evidence digests and
content version, but never a global route order.
Observed/reproduced counts may follow the exact supersession lineage for the
same failure fingerprint, but shadow and validation evidence always bind the
candidate's exact immutable content SHA.

After a candidate becomes `validated`, the scheduler writes a deterministic
outbox handoff bound to the candidate/content SHA and the exact host-verified
validation task. Every maintenance tick also scans allowlisted `validated`,
`promotion-ready`, and `promoted` candidates and idempotently backfills a
missing handoff. This closes the crash window between the candidate transition
and outbox insertion. The handoff has stable, different review/apply task IDs,
its own lease, rolling attempt budget and backoff. It never contains the user
prompt or candidate body.

When a trusted `ProductionPromotionService` is injected, due handoffs call
`promote_validated(candidate_id, review_task_id=..., apply_task_id=...)`
automatically. A missing service leaves the handoff queued. An exception leaves
it pending for bounded retry and never changes the visible user result.
`promotion-ready`/uncertain operations require the promotion service's exact
reconcile/resume path; the scheduler never guesses that an external effect
succeeded. A committed `promoted` candidate is recognized after restart
without applying it again. Thus scheduling is autonomous, while actual
activation remains inside the separate allowlisted, reversible CAS backend.

Promotion does not end observation. For managed Shader recovery hints,
the scheduler compares later typed failures against the promotion time and the
same fingerprint. Two independent post-promotion task failures create a durable
rollback handoff. The exact active promotion operation is then rolled back
through the same leased, receipt-bound service; an uncertain rollback is
reconciled rather than blindly repeated.

## Pre-AgentHost observation inbox

Failures can occur before AgentHost starts. The Python 3.6-compatible bridge
module `renderdoc_mcp_bridge/learning_observation_inbox.py` writes only a
`replaylab-learning-observation/v1` fact envelope to:

```text
%LOCALAPPDATA%/ReplayLab/learning-observation-inbox/incoming
```

`write_learning_observation(...)` uses a content-derived observation ID and an
atomic replace. On the next AgentHost startup, bounded intake atomically claims
each file under a unique process claim name, validates its filename/schema/
hash/size, stores valid observations, archives them, and quarantines malformed
content. Another process winning the same claim is not reported as corruption;
abandoned claims become recoverable only after a stale lease. External facts
are `external-observed` and contribute no verified validation threshold. The
bridge never edits a Skill or registry.

## Promotion and rollback

`prepare_promotion` binds the exact expected target SHA/version and proposed
new SHA/version. Preparation moves the candidate and creates the operation in
one SQLite transaction. `ManagedOverlayPromotionBackend` is the first-party
production implementation. Its allowlist is built in code and currently
contains the four RenderDoc procedure Skills plus the non-executable,
non-authoritative `shader-recovery-hints` advisory configuration. The retired
`shader-recovery-policy`/`routeOrder` schema is audit-only migration data and is
not a production target. Arbitrary target IDs, filesystem
paths, commands, executables, MCP grants, tool grants, effects, and executable
candidates are rejected.

A `ReversiblePromotionBackend` must implement an external atomic compare-and-
swap, return an exact apply receipt with an opaque rollback token, support a
read-only target probe, and restore the exact previous SHA/version. Local
promotion state and candidate state transition commit in one transaction after
the external receipt validates. A crash or exception after an external call is
recorded as `apply-uncertain` or `rollback-uncertain`; it is never automatically
retried. `reconcile_promotion` probes the exact target and can only return to a
safe pre-operation state or finalize a receipt-bound applied/rolled-back state.
Any other target remains unknown and fail-closed.

The generic ledger additionally requires an independent promotion-review task
and a different apply task for an executable candidate. The first-party
managed backend is narrower and rejects executable candidates entirely; a
future executable backend would need a separate signed release/build channel
and cannot hot-modify the process that generated or validated it.

## Managed next-run activation

Formal upgrades never edit a packaged `.agents/skills` directory, executable,
MCP registration, or the running registry. The backend writes only beneath the
fixed per-user `managed-learning-overlays` root:

- candidate JSON is written to a content-addressed immutable version object;
- the exact prior activation pointer is retained as an immutable backup;
- an operation journal durably binds the target, CAS versions/digests and
  rollback capability before the pointer changes;
- a staged file is flushed and atomically replaces the small activation
  pointer under a cross-process target lock;
- apply and rollback receipts are immutable and integrity-bound.

Every path is derived from a code-allowlisted safe ID, checked for containment,
and rejected if any managed component is a symlink or Windows reparse point.
Concurrent promotions serialize on the target and the loser fails its exact
SHA/version CAS. Startup reconciliation inspects the journal, immutable version
and pointer; it does not repeat an uncertain external effect. A managed overlay
also has to prove a complete ancestry chain back to the current packaged
baseline. Replacing the packaged baseline invalidates and quarantines a stale
overlay instead of silently layering older learned advice over new code.

The filesystem pointer alone is not activation authority. A later AgentHost
loads it only when the learning SQLite ledger also reports the same operation
as `applied`, contains its apply receipt, and binds the exact target/version/
digest. Until both commits verify, the base registry remains active. Running
tasks keep their frozen snapshot; a verified Skill or recovery-hint update is
visible only to a newly constructed runtime and can be rolled back to the exact
previous pointer. Recovery hints are loaded into a separate advisory map;
`tool_configs` remains empty so the Host cannot reinterpret learning as runtime
route or retry policy.

`ProductionPromotionConfig` is Host-owned constructor configuration, not a
candidate field and not model-editable. Its production defaults are
`enabled=True` and `reconcile_on_start=True`. The storage location defaults to
`%LOCALAPPDATA%/ReplayLab/managed-learning-overlays` (or the platform-local
application-data fallback); an embedder may inject an absolute test/deployment
root. Relative roots are rejected. No environment variable or candidate can
replace the code-defined target allowlist.

The bootstrap call belongs after `RunStateStore` and `LearningCandidateStore`
construction but before a new runtime projects `_registry_skills`:

```python
boot = bootstrap_production_promotion(
    base_snapshot,
    store=learning_candidates,
    run_state=run_state,
    root=learning_promotion_root,
)
# boot.snapshot is the only registry snapshot projected into this new run.
```

The public production surface is exported from
`replaylab_agent.learning_promotion.__all__`; it includes the bootstrap,
configuration/result records, backend/service, strict candidate builders and
the two candidate wire-schema constants.
