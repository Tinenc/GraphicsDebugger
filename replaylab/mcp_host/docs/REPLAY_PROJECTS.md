# Multi-capture replay projects

A replay project is a bounded portable index. Launcher-created projects use
their immutable workspace ID as a private project sharing boundary; lower-level
MCP-created projects may instead bind an explicit game build. It lets a later
capture discover reusable shader source, verified display aliases and selected
material values without reusing capture-local replay authority. Each project
also keeps a capture library whose RDC entries are references, not owned copies.

## qrenderdoc project home

An ordinary idle Bread/qrenderdoc launch opens `Bread Projects`. The
same home is available later from `File -> Bread Projects…`. It supports
creating a project at a user-selected path, adding an existing portable project,
forgetting a local registry entry without deleting its directory, configuring
an unpacked-asset root, adding/removing RDC references without deleting the RDC,
and opening a frame from its bounded thumbnail list. Direct RDC/file-association launches and replay-host,
target-control, Python, remote, crash-handler, and other special startup modes
continue directly to their requested operation.

Launcher projects are bound automatically to their own workspace, so the New
Project dialog needs only a name, project path, optional unpacked-asset path and
initial RDC references. Capture catalogs, Shader interfaces/layouts and fresh
ABI validation remain the physical compatibility gates inside that project.
Legacy pending projects remain readable and can still be bound once from
Project Settings. The lower-level `create_replay_project` MCP operation still
requires an explicit game/build boundary.

All project-home host calls and capture association run on a bounded background
pool. The Qt UI is updated only on its own thread, and owner/generation guards
discard results after the dialog closes or a different capture is loaded.
Opening a project card activates that project before qrenderdoc opens the RDC.
Opening an RDC directly performs a metadata-only background association check
and changes the active project only if exactly one registered project contains
that normalized absolute path.

Capture-library additions record the canonical path, display name and stable
path-derived reference ID without reading the full RDC. Embedded thumbnails are
then read lazily from container metadata, so adding multi-gigabyte captures does
not wait for an unrelated full-file hash. Older version-2 content identities
remain readable but are no longer used for project association. Zero or
multiple path matches leave the current project alone.

### Automatic restore acceleration

Automatic restore runs on the Host worker and its progress window is non-modal.
An RDC that already has an exact `project-reuse-index` association validates
only the Shader resources named by that association; it does not build the
whole unique-resource inventory first. The bounded verifier re-reads those
target ResourceIds in the current capture session and checks raw bytes, the
seven-field stable key, reflection/layout/ABI and CBuffer block hashes before
alias restoration or Shader apply. An unchanged association is not written
back to the RDC again. Only a version-3 association with a complete desired
head and complete realized-receipt coverage can enter this exact path. Valid
version-1/version-2 associations are migrated as incomplete routing hints and
fall back to the inventory path until a successful restore refreshes them.

When the association is missing or stale, the first complete unique-resource
scan publishes a disposable project-side cache at
`derived/shader-resource-inventories/<capture-path-sha256>.json`. The sidecar is
bound to the project scope, normalized RDC path, file observation, inventory
algorithm and matching policy; it uses canonical JSON, a content seal, bounded
size and atomic replacement. A later open uses it only to select the small set
of resources that can match the project's per-identity `activeHeads` index,
then runs the same fresh target-session verifier and re-plans from the verified
subset. The legacy `activeKnowledge` serial remains audit/order metadata.
Publishing newer reviewed HLSL therefore takes effect lazily for the same or
another already inventoried RDC without batch-rewriting historical
RDCs. When an older RDC is opened and restored successfully, it refreshes its
own association to the current head.

The sidecar never stores replay handles, replacement IDs, session authority or
raw bytecode, and cached ResourceIds/EIDs never cross capture boundaries. A
missing, stale, malformed, over-budget or mismatching cache falls back to a
fresh complete inventory. Replay work intentionally remains one bounded job:
multiple restoration threads would still serialize through RenderDoc's replay
controller and would add state contention without reducing GPU replay time.

Project knowledge is the one-way authority for automatic restore. Portable
Shader, CBuffer-layout, alias and material records are append-only and selected
through a per-identity active head. Each head has a monotonic generation and a
content hash; project publication uses compare-and-swap against the generation
that the editor or restore transaction originally read. Re-observing the
already-current artifact is genuinely idempotent and does not advance the head.
An older RDC can therefore contribute a new, hash-bound observation, but it
cannot reactivate historical HLSL, aliases or material data over a newer head.
An intentional revert is a separate explicit publication against the current
head generation.

Workspace head keys v2 identify the captured original by API, encoding, raw
bytecode hash, stage and entry point. Editable language, compiler/reflection
versions and display metadata do not create parallel active versions. Legacy
v1 lane indexes are validated with their original key algorithm, then merged
using the activation order of their explicitly selected heads. Historical
publications and their paths remain readable; only one source is current.
Changing editable language binds a matching compiler route before importing
the current source. It never feeds HLSL to a GLSL route or overwrites a draft.
Legacy navigation sidecars keep their original review/source hash and reanchor
in the editor; a source edit does not fabricate a new semantic approval.
An automatically resumed editor can precede a restore reservation. Restore
retires only that exact workspace/GUI/session writer, preserving durable consent
and its native replacement, then enables editing after committing. Other GUI
captures remain independent. Manual-source restore uses the newly inventoried
target EID, rather than reusing the first EID in an older workspace catalog.
Resource replacement covers every use of each original RID; UI usage lists
combine all bound RIDs for one logical source within the current capture.

The RDC association separates the desired project head from a realized-apply
snapshot. Its semantic head hashes the exact referenced knowledge IDs,
content-addressed artifact references, and Shader/material head projections
used by that capture. The legacy project-wide `activeKnowledgeRevision` is
outer-sealed audit metadata and is deliberately excluded from the semantic
`headSha256`, so an unrelated publication cannot invalidate this RDC. A
realized receipt records which desired artifact was successfully validated and
either applied or adopted from an exact live replacement in the same capture
session. It is routing evidence only: it never contains reusable capture
session, replacement, operation or rollback authority. A new ReplayController
must always create a fresh replacement after the bounded identity/ABI check;
within the same capture session, an exact live replacement can instead be
adopted without another mutation. In both cases the fast path avoids global
matching and compilation.

Timestamps such as `registeredAt`, `observedAt` and `appliedAt` are audit and UI
metadata only. They never select a head, invalidate an otherwise identical
binding or decide whether an RDC may update the project. This avoids clock skew,
copied capture timestamps and backup restores turning stale data into authority.

CBuffer persistence is deliberately split. Stable block layout and verified
display aliases are keyed by Shader identity plus block path/layout/ABI hashes.
Event-scoped numeric values remain explicit material-session data and require a
fresh EID, binding, type and layout validation; opening an RDC never applies old
numeric bytes merely because a layout identifier matches.

The machine-local sealed project registry stores portable roots outside the
default projects home. Its first load migrates existing valid default projects
once; after that, it is authoritative, so forgetting a record does not make the
unchanged directory reappear. Project creation first constructs and validates a complete hidden staging tree,
then atomically publishes it and commits the active-project selection under the
same lock. A failed selection commit restores the prior selection and moves any
published tree to a hidden quarantine before cleanup, so it cannot appear as a
usable half-project. Settings and pinned active-project operations share the
same selection lock and preserve snapshot semantics.

### Upgrade compatibility and local drafts

Bread must keep previously supported project records readable when upgrading.
Application/package versions, UI translations, comments and formatting are not
physical Shader validation policy. They must not invalidate otherwise matching
restore receipts. A change to actual ABI rules, validation algorithms or the
native replay implementation can require fresh validation; it must fall back to
the saved project source/candidate and current captured original instead of
treating a stale acceleration cache as a failed project. Successful validation
publishes current derived receipts for subsequent fast opens. Old authority
hashes and historical records are not rewritten to claim newer validation.

The captured original remains the match anchor: verify its bytes, API, stage,
encoding, entry point and applicable target profile before applying a saved
replacement. Reuse a matching compiled candidate when possible. GLSL targets
rebuilt by an OpenGL/GLES driver still require physical reflection validation;
this successful inline validation is compatible with the cached restore route.

An edited local source and the last successfully saved replay version are
independent. Restore the saved version through a separate replay workspace,
retain the current draft and its editor/snapshot identity, and do not start a
watcher that silently applies the draft. An explicit save in that original
editor can take over the restored replacement. Draft display hints are
revalidated, optional metadata; their absence cannot veto a successful replay
restore. Preserve those hints when migrating legacy caches and when writing a
new snapshot so the second open retains the same draft.
Generated replay workspace names have the same fixed length as ordinary
workspace names, so isolating a draft does not grow compiler input paths past
Windows tool limits. Existing verified replay workspaces remain readable.

Changes to these contracts must keep the upgrade regression suite passing on
Windows and Linux. From `replaylab/mcp_host`, run:

```text
python -m pytest tests/test_shader_physical_validation_policy.py tests/test_project_restore_upgrade_local_draft.py tests/test_project_restore_local_draft.py tests/test_verified_restore_host.py
```

The tests cover package/UI updates, real policy changes, legacy snapshot
migration, a subsequent cache hit, unchanged candidate bytes and preservation
of drafts in Unicode paths. Corrupted records, missing saved artifacts and a
different captured Shader remain genuine diagnostic conditions.

## Safety model

- Launcher projects use a unique private workspace scope. Explicit lower-level
  projects still use `game_id` and `build_id`. Optional `platform` and
  `api_family` further narrow either boundary; the first imported catalog pins
  the API when omitted.
- A later catalog with a conflicting declared game/build/platform/API is
  rejected. Catalogs that do not carry game/build metadata remain bound to the
  explicit project scope supplied by the user; the capture path is never used
  to guess a build.
- Raw matches use physically read shader bytes. A code-hash match is accepted
  only when stage, entry point, complete interface hash and CBuffer layout hash
  also match.
- Old workspace manifests, ABI reports, replay handles, replacement IDs,
  material sessions and capture-session IDs are never reused as authority. A
  content-addressed compiled candidate may skip recompilation only when its
  source, compiler binding, target profile and bytes still match the active
  project head; the target capture/session is then freshly rebound and its
  replay ABI is validated before apply.
- Seeding writes only `source/shader.hlsl` in a freshly validated, unedited
  destination workspace. It does not compile or apply anything. Normal compile,
  fresh replay reflection/ABI validation and live capture checks remain required.
- Alias and material entries are candidates. Material values require a new live
  material session and exact block-layout validation before use.
- `captureId` identifies a capture-path/API/shader-byte family. It is not a
  hash of the complete reflection and `uses` payload. Each normalized catalog
  payload therefore has its own `catalogSha256` revision. Workspaces and
  knowledge records bind both values; if a capture has multiple revisions and
  that provenance is missing, registration fails closed as ambiguous.
- If the source workspace contains fixed `ai/analysis.md`, it is copied as
  size-limited UTF-8, content-addressed explanatory text. The reuse plan labels
  it `physicalEvidence: false` and `applyAuthorization: false`; it can guide a
  review but cannot establish a register, offset, ABI fact or permission.

## MCP workflow

1. Create a project with
   `create_replay_project(project_path, game_id, build_id, name, platform, api_family)`.
2. Export the current frame's strict shader catalog with
   `export_capture_symbol_catalog`, then call
   `import_replay_project_capture(project_path, catalog_path)`.
3. For executable HLSL, complete and accept the transactional organisation
   review, compile the promoted source, and freshly validate that current
   candidate against the replay ABI. Only then call
   `register_replay_project_workspace`. Registration rechecks the HMAC-sealed
   organisation gate, current source/revision/candidate hashes, and exact ABI
   request/report before publishing the HLSL and compiled artifact into the
   project library; source-only, provisional, stale, or explanation-only
   workspaces fail closed. If available, register a verified alias export with
   `register_replay_project_alias_table`. Save only explicitly selected live
   parameters with `save_replay_project_material_knowledge`.
4. For another frame of the same build, export/import its catalog. Import returns
   the same result as `get_replay_project_reuse_plan`: matches grouped per live
   shader, with their exact match mode and remaining validation requirements.
5. Create and decompile a new workspace for that frame. For a chosen source
   candidate, call `seed_shader_workspace_from_replay_project`. Review the diff,
   compile, call `validate_shader_workspace_candidate`, and only then use an
   explicitly authorized apply tool.

In AgentHost, status and reuse-plan reads are available by default inside
the configured workspace root. Every create/import/register/save/seed operation
is effectful and requires an exact same-turn `/allow` tool name. Shader apply
still requires its separate same-turn validation and authorization.

## Stored data and limits

The atomic `project.json` manifest indexes at most 256 captures, 65,536 shaders
and 4,096 catalog revisions or records of each knowledge kind. A newly seen
revision is normalized, copied into the project-owned immutable path
`captures/catalogs/<captureId>/revisions/<catalogSha256>/catalog.json`, and its
adjacent shader blobs are copied by content address. The external import path
is no longer authoritative after import. Re-importing a known historical
revision is idempotent and does not move the active/head revision backward.
Every load verifies each owned catalog and artifact hash, the active-revision
pointer, and its duplicated head fields.

HLSL seeds are limited to 16 MiB, analysis text to 1 MiB, and alias artifacts
to 64 MiB. Operations use fail-closed project/workspace lock files; a stale
lock is never guessed safe. Version-1 single-catalog projects remain readable;
their first later import migrates the legacy catalog into owned immutable
storage before adding another revision.

The project home can explicitly open a selected capture, and a directly opened
capture can activate one exact unique-path project match. It does not
create workspaces for every shader match, apply aliases to RenderDoc, create
material sessions, choose among multiple candidates, or authorize Shader Apply.
Those remain explicit program/AI/user orchestration steps.
