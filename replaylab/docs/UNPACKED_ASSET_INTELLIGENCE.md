# Unpacked asset intelligence

Bread keeps unpacked-project knowledge separate from capture reconstruction.
The main Agent partitions mixed work. An ordinary child Agent loads
`renderdoc-unpacked-asset-intelligence` to inventory, relate, report, and supply
exact source assets; other ordinary children load the Event, Shader, or Resource
Export Skill for capture-bound work.

## Reader selection for a draw

Read operations now route by asset root, detected/configured engine and requested
capability (`classify`, `properties`, `references`). A built-in file header can
prove classification but cannot satisfy a request for material parameters. Deep
reads default to `properties`; actual returned fields must prove that capability.
The project's verified choice is preferred for that exact root/engine/capability.
An engine mismatch is rejected, registered export operations are never invoked
as implicit readers, and at most two distinct read invocations are attempted.
Equivalent operations are deduplicated. Read-only results and concurrent requests
are shared with file/configuration invalidation; material analysis uses this same
route instead of issuing a second independent UEX read.

No installed reader, missing resource directory or insufficient fields returns a
nonblocking capture-only fallback. The Agent can infer Shader names from bindings
and calculations, marks them as inferred and continues the requested work. It does
not need to verify every reader or scan the whole game before organizing a Shader.
UnityPy/AssetStudio, UEX/CUE4Parse and registered custom readers remain providers
behind this route. AssetRipper is not bundled by this change; its project and
reference reconstruction can be added as a capability-specific provider without
introducing another competing per-EID scan.

## Workflow boundary

1. Bind the active project, game/build scope, and configured unpacked-asset root.
2. Reuse a complete compatible `asset-catalog-v1.sqlite3`, or run one bounded
   incremental/full scan under the project effect.
3. Query typed identities and evidence-bearing relations; re-hash one selected
   asset before delivery. A returned absolute path is evidence for a trusted
   consumer, not execution authority.
4. Return a small immutable result digest to the main Agent. It schedules any
   dependent Event/Shader/Resource Export stage; children do not call each other
   or widen scope themselves.
5. For UEX reports or Blender delivery, inspect registered provider
   capabilities and the Host-owned UEX decision receipt first. Start one
   idempotent Host-managed job, poll its operation ID, and distinguish a fully
   validated completion from an explicit partial result.

Batch membership must be explicit and bounded. The main Agent partitions
capture draws into exact child operations. UEX Blender accepts up to 64 unique
exact virtual SkeletalMesh paths in one logical assembly job. Completed child
artifacts remain valid after a sibling failure; only known-terminal failed or
unstarted children may be retried.

## Host-owned UEX decisions

`get_unpacked_asset_provider_decisions` reads only the process-owner configured
profiles registration and committed UEX `inventory.db` schema v2. It returns
profile **names** plus bounded Inventory identities, counts, `Strong`,
`committed`, `complete`, `truncated`, report eligibility, and a canonical
decision hash. It never returns the profiles file, `analysisDir`, pak roots,
AES values, arbitrary metadata, or a caller-selected database path. Missing
configuration, an unsupported schema, an unreadable store, absent quality
metadata, a non-Strong Inventory, and a truncated Inventory remain distinct
unavailable/partial receipts.

The audited UEX CLI exposes `inventories` and `query-assets`, but its Inventory
list omits the quality metadata needed for the Strong/truncated gate. ReplayLab
therefore reads the already committed database in SQLite read-only/query-only
mode and selects fixed columns. It does not mount or rescan paks during
decision discovery.

`query_unpacked_asset_inventory_assets` queries one exact Inventory with fixed
pagination and optional exact kind/text filters. Results contain only bounded
identity, size, availability, and numeric metric fields; large `mesh_json`,
`material_json`, texture/detail JSON, and provider errors are omitted. Real UEX
object identities such as `SilverPalace/Content/.../SK_X.SK_X` receive a
validated `exportAssetPath` of `SilverPalace/Content/.../SK_X`. Only a
same-name object suffix is removed. Leading `/` is optional at Blender-job
input; drive paths, traversal, controls, and ambiguous object suffixes are
rejected.

## Persistent catalog

The catalog is stored in the active project's shared `assets` dictionary. It
records a per-occurrence asset identity, normalized relative path, content
SHA-256, kind/format, parser coverage, typed metadata, and bounded relationship
edges. Incremental scans reuse a prior hash only when path, size, and timestamp
still agree; delivery always re-hashes the file.

Built-in parsers cover glTF/GLB, OBJ/MTL, common Shader includes, Unity GUIDs,
PNG/DDS/KTX2/JPEG texture headers and color-space evidence, plus
`uex.blender-bundle.v1`, assembly, validation, and report manifests. Unknown or
provider-only formats remain explicit rather than receiving guessed semantics.
Relations distinguish deterministic verified links from unresolved or
heuristic candidates. Basename and directory proximity are search hints only.

The current implementation atomically publishes one active catalog snapshot.
Result receipts remain hash-bound, but historical catalog snapshots are not yet
queryable through MCP; a changed catalog requires a fresh query/resolution.

## UEX asset reports

`start_unpacked_asset_report` accepts no source, executable, profiles database,
or output path. Its primary inputs are a profile name, positive concrete
Inventory ID, and a bounded structured version-1 project grouping
specification. The Host rejects unknown fields, missing character/scene rules,
invalid scalar types, unbounded strings/lists/regex timeouts, non-finite
numbers, and incoherent animation bins before it creates an operation. It
never substitutes game-specific example rules when the project rules are not
known; the result is an explicit yield/decision requirement.

The canonical JSON is written to the fixed managed
`input/report-spec.json` inside the operation, and its exact size/SHA-256 is
part of the idempotent request hash. UEX receives only that staged path. The
older catalog form remains compatible: the catalog JSON is re-hashed and
parsed, then the same canonical form is staged, so the provider never consumes
the mutable source path. Inline specs need an active project game/build scope
but do not require an otherwise unrelated unpacked-file catalog.

Legacy catalog Inventory evidence is supplemental: before dispatch the Host
independently resolves the selected profile/Inventory from the registered UEX
database and requires its authoritative decision to be `Strong`,
`complete=true`, and `truncated=false`. Inline specs require no caller-uploaded
Inventory evidence. A project JSON file cannot forge that authorization.

The provider runs the fixed `uex asset-report` route. Success requires
`asset-report.html`, `asset-report.json`, `asset-summary.csv`, and
`asset-report-manifest.json`. The manifest and renderer-independent JSON must
bind the same profile and Inventory; the self-contained HTML must embed the
same identity; and the manifest must bind the other three exact names, byte
lengths, and SHA-256 hashes. Missing cooked values remain missing, never zero.
The loaded Skill contract also fixes Role/Role+Weapon grouping, distinct-Skeleton
bone aggregation, exact-Skeleton animation association, deduplicated texture
resolution counts, and nearest-rank P90.

Capabilities explicitly report inline managed staging and legacy catalog
compatibility. An Agent using this Skill may construct a project-specific
structured spec from verified project rules, but must ask the user when those
rules are absent or ambiguous. There is no implicit Silver Palace or other
sample-project policy.

## UEX Blender delivery

`start_unpacked_asset_blender_export` requires a concrete Inventory ID and the
exact `decisionSha256` returned by Host-owned discovery in addition to exact
cooked virtual asset paths. Before creating an operation, the Host reopens that
committed Inventory read-only and proves every normalized path resolves to one
unique, available row whose `kind` is `SkeletalMesh` and whose class is
`SkeletalMesh`/`USkeletalMesh`, with no provider error. A stale decision hash,
missing row, ambiguous package/object identity, unavailable asset, or wrong
class fails before provider dispatch; callers cannot export an unproven search
result. The canonical Inventory identities and decision receipt are included
in the request hash.

Every selected asset must produce a
`uex.blender-bundle.v1` whose `sourceAsset` is unchanged and whose type is
`USkeletalMesh`. The Host validates LOD0, mesh byte counts and safe relative
paths, optional `.uemodel`, importer, material/raw-material JSON, base-color and
parameter textures, Skeleton/bones, and bundle identity.

`.uemodel` is preferred only when the registered UEFormat add-on is available.
GLB is an explicit requested fallback, not a silent equivalent. A final
`.blend` additionally requires request-bound profile/options/assets, coherent
geometry route, output hash, `uex.blender-assembly.v1`, and a passing
`uex.blender-validation.v1` proving meshes, skin weights, armature modifiers,
shared armature, bones, LOD0, and no missing images or reported errors.
Completed assembly receipts retain the validation artifact and also project a
bounded `validationReceipt` with its schema/pass flag and critical aggregate
mesh/armature/bone/LOD0/image counts. Bundle-only and partial jobs set that
receipt to null rather than implying Blender validation ran.

The Host's persisted assembly receipt couples the assembly-manifest artifact
hash, exact source set, per-asset Inventory identity hashes, Inventory ID, and
decision SHA-256. This is the Inventory provenance boundary even though the
external UEX wrapper's native assembly schema has no Inventory fields.

Every selected asset has its own receipt with exact asset identity, terminal
state, validated bundle manifest, artifacts, and hashes. If the UEX wrapper
exits after exporting only part of a multi-asset set, or Blender
assembly/validation fails, ReplayLab independently validates and preserves each
successful bundle. The job becomes terminal `partial`, `complete=false`, lists
the failed items, and records that assembly was not accepted. Host restart can
recover the same verified subset. A partial result never becomes `completed`;
a validated partial keeps the top-level `error` empty so the report layer can
publish its immutable accepted artifacts, while the sanitized provider/assembly
failure remains in `result.providerFailure` and aggregate completion remains
false. A failed result with no accepted artifact still uses the top-level error.
A continuation is started only through
`continue_unpacked_asset_blender_export(operation_id, parent_operation_id,
expected_parent_request_sha256, expected_parent_result_sha256)`. The caller
cannot submit assets, manifests, old paths, or an accepted/failed partition.
The Host loads the exact terminal `partial` parent, recomputes both receipt
bindings, re-hashes every retained bundle artifact, and derives only the
failed/open indices for the fixed wrapper's `--export-only` retry. A fresh
operation request and its atomically staged internal plan bind the parent
operation/request/result hashes, each retained artifact hash, the Inventory
decision and per-asset identities, and all fixed provider hashes.

When the parent requested a final `.blend`, the Host then invokes the fixed
assembler with retained parent manifests plus newly exported manifests in the
original asset order and runs the fixed Blender validator. This also handles
an assembly-only continuation where every bundle was already accepted: no UEX
asset export is repeated. The final receipt identifies each item's owning
operation, reports retained/retried counts, and accepts the assembly only after
the same shared-armature, skinning, LOD0, image, and output-hash gates. On Host
restart, an exact completed plan/output is revalidated and adopted; otherwise
the operation becomes explicit partial/interrupted and no uncertain effect is
automatically replayed. A changed parent artifact, staged plan, request, or
result hash fails closed.

## Provider configuration

Provider locations are process-owner configuration and are returned only as
availability/name/hash fingerprints. The model cannot override them:

| Environment variable | Provider |
|---|---|
| `RENDERDOC_ULTRA_UNPACKED_ASSET_JOB_ROOT` | Managed job root |
| `RENDERDOC_ULTRA_UEX` | UEX executable |
| `RENDERDOC_ULTRA_UEX_PROFILES` | UEX profiles database |
| `RENDERDOC_ULTRA_UEX_BLENDER_WRAPPER` | Audited Blender export wrapper |
| `RENDERDOC_ULTRA_UEX_BLENDER_CONTINUATION_DRIVER` | Optional process-owner absolute continuation driver override; an invalid explicit value fails closed |
| `RENDERDOC_ULTRA_UEX_PYTHON` | Python runtime for the wrapper |
| `RENDERDOC_ULTRA_BLENDER` | Blender executable |
| `RENDERDOC_ULTRA_UEFORMAT_ADDON` | UEFormat add-on file/directory |

In a source Python process, the current interpreter is an eligible fallback and
the audited `unpacked_asset_jobs.py` module is the continuation driver fallback.
In a frozen/PyInstaller Host, the Host executable is never launched as a Python
interpreter. `Build-ReplayLabRuntime.ps1` embeds that audited module into both
the AgentHost and MCP onefile builds as the fixed
`replaylab_helpers/unpacked_asset_jobs.py` data helper; the Host resolves it
only below PyInstaller's absolute process-owned extraction root and rejects
missing, redirected, or escaping paths. A valid explicit process-owner driver
configuration takes precedence, while an invalid explicit value does not
silently select another provider. The selected driver is SHA-256-bound in the
capability, request, and continuation-plan receipts. A separately configured
Python provider remains required to execute either driver. Profiles and
secrets are never returned in logs or model-visible state.

The canonical Skill contract is
`.agents/skills/renderdoc-unpacked-asset-intelligence/`. It incorporates the
portable-report semantics from `uex-export-asset-report` and the exact bundle,
assembly, rig, LOD, material, and validation contract from
`uex-export-blender`.
