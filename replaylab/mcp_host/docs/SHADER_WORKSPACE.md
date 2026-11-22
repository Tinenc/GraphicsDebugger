# External Shader Workspace

Interactive save, unsaved-buffer compiler checks and workspace value previews
use the same bound compiler recipe. The recipe is projected from the existing
manifest and current pinned tool registry; it does not migrate source, paths or
history. When a registered compile route exists, all three paths use it instead
of independently preferring a replay-native or private compiler. Captured native
GLSL workspaces without an external route keep their native compiler. Temporary
preview source and output paths differ, while language/profile, entry contract,
macros, target environment, layouts, position conventions and process environment
come from that recipe. Route/argument/tool changes invalidate the preview cache;
unchanged targets and manual-save bytecode caches remain reusable. AI organization
validation stays separate from this interactive compilation policy.

For the source-bound symbol table, edit latency and validation tiers, see
[Shader refactoring latency and symbol ownership](SHADER_REFACTOR_ACCELERATION.md).

The external Shader workspace is Bread's deterministic foundation for
turning one captured Shader into a reviewable edit/compile experiment. It is
format- and API-driven rather than Unreal-, Unity-, or engine-driven: fixed
code reads the captured API, executable bytes, stage, entry point and physical
target profile, then selects only a locally registered, hash-pinned round-trip
route.

The result is not promised to be the game's original source. Only the
`executable-editable` lane can be compiled or save-watched. A compile proves
only that the route emitted one structurally valid executable of the captured
format/profile. A fresh replay reflection proves only ABI compatibility. Live
replacement is a further, separate capability for local D3D11/native DXBC,
D3D12/native DXBC or DXIL, Vulkan/native SPIR-V, and OpenGL/native OpenGL SPIR-V
on conventional graphics/compute stages. Ray/Library replacement remains a
separate fail-closed boundary.

## End-to-end user workflow

The workspace UI identifies a logical Shader by its captured original bytes,
API, stage and entry point. Source language, catalog/toolchain revisions and
project knowledge versions are history of that Shader, not additional tree
entries. Capture-local replay bindings still retain their own capture and
ResourceId checks. Reorganization resolves the existing canonical source first;
it does not start from decompilation merely because an import receipt or catalog
revision is older. Clean superseded tabs follow the canonical source. Unsaved
or externally modified working copies remain protected.

The normal "reverse this draw's Shader, make it readable, then apply my saves"
workflow is:

1. Resolve the current capture, EventId and stage. `prepare_shader_workspace`
   exports the exact Shader bytes and reflection, identifies the capture API,
   detects DXBC, DXIL, Vulkan SPIR-V or OpenGL SPIR-V from physical evidence,
   and derives the stage/entry/profile. It does not guess a profile from a UI
   label.
2. Inspect the returned bound workspace, representation lane, and hash-bound
   route inventory. Preparation does not run a route probe or decompiler by
   default. The Agent may call `decompile_shader_workspace` for the bound route,
   `try_shader_workspace_recovery_candidate` for a selected registered pair in a
   sibling workspace, or `get_shader_workspace_representation` for RenderDoc
   disassembly. `attempt_reconstruction=true` explicitly requests the former
   one-shot preparation behavior. A selected recovery pair receives an exact
   source/target, capture/raw identity and route-bound receipt; a failed attempt
   leaves the original workspace intact. Compiler-capable explanatory source
   remains a draft until explicitly promoted, compiled and ABI-validated.
   Read legacy metadata using its original schema. Missing modern identity
   fields are completed from matching captured bytes, reflection and verified
   workspace evidence; a schema upgrade alone does not invalidate saved work.
   Preserve project names, paths, source edits and immutable revisions. A real
   incompatible toolchain change can use `migrate_shader_workspace_toolchain`
   to create a new workspace with the exact current source bytes and the same
   route shape. Old revisions and candidates remain available in the archive.
3. If an unpacked `assetRoot` is configured and the project catalog has already
   been prepared, `query_active_shader_asset_evidence(event_id, stage, top_k)`
   reads only the active project's hash-bound full catalog and managed symbol
   database. It correlates the exact captured Shader with bounded source/binary,
   reflection and CBuffer evidence without accepting a path, scanning, compiling,
   submitting a match or applying an alias. Names/source previews help the AI
   explain the program, but only program-verified blob/code/interface/layout
   matches may seed physical aliases or cross-capture knowledge. `AIProvisional`
   evidence remains review-only.
4. For executable source, `begin_shader_workspace_organization` opens an
   editable draft from the current source. The Agent chooses the edits and
   tools. The final `finish_shader_workspace_organization` binds the exact
   draft and its source review, then compiles and validates the candidate once.
   A checkpoint/source-only finish does not authorize replay replacement.
   Prefer available compilable native source or a directly successful fixed
   tool route; bytecode alone does not imply ABI repair. Reuse the current
   compile/ABI evidence and repair only an actual failure. Register the
   validated source separately only for an explicit low-level/recovery route.
   For the normal requested replacement, finish with `apply_shared_resource=true` and the
   requested workspace display/live-edit options. Finish applies the validated
   candidate, publishes the reviewed source and navigation, and records the
   active project's replacement association in one operation. Its
   `replacementRegistration` receipt is the result: do not repeat registration
   or validation after a successful receipt. Read back the canonical editable
   source and durable live session. A short-lived Agent-owned watcher is not
   sufficient. If only project publication fails, retain the applied candidate
   and use the returned registration recovery parameters without recompiling
   or reorganizing unchanged source.
   When display is requested, final readback also checks the actual calculation
   directory: the current source, reviewed semantic overview digest and ordered
   stage IDs must agree with the editor projection. Pending generation is
   distinguished from a missing, provisional or stale directory. A display
   mismatch leaves source/replay results intact and requires display/metadata
   recovery; it does not require another compilation or ABI pass. Directory
   quality follows the reviewed rendering calculations, never a fixed row count.
   Registration verifies the workspace's immutable catalog against the live
   Shader/event uses and instance/session/path/API. The UI symbol catalog ID
   may be empty or belong to a different export selection of the same RDC;
   it is not substituted for this exact workspace evidence. If the active
   project has not imported that catalog revision, registration imports the
   existing, verified workspace catalog under the source transaction after
   both publication gates agree. It does not export a new full frame or infer
   permission from an empty UI ID.
5. For the normal editable/save/compile/replace request, start the live session
   above; explain-only and explicit export-only requests skip apply/watch.
   A stable source save is debounced,
   compiled to an immutable revision, rebound to the current capture/session
   and original Shader, and applied through the checked native replacement
   path for the capture API. Ordinary editor saves do not repeat the AI
   organization's ABI review. Strict AI publication reuses a successful
   in-process validation only for the exact source/revision/candidate, tool
   route, original Shader and current capture session; stale or incomplete
   receipts take the normal validation path. A newer pending save
   prevents an older revision from being applied. Ray/Library workspaces may
   still compile source/IR, but cannot start auto-apply while their complete
   export/state-object/shader-group/SBT transaction is unavailable.
   The explicit authorisation is stored with the exact workspace/capture/shader
   identity and is resumed by the singleton Host after a Host restart. Use
   `list_shader_workspace_live_edits` to inspect active, authorised and failed
   resume records; an explicit stop revokes the durable authorisation.
   The qrender Bridge intentionally forgets capture-session-local catalog IDs
   on restart. The watcher may resume before a capture is ready. When a strict
   AI publication later needs candidate validation, fixed Host code derives a canonical evidence hash
   from the immutable, `catalogSha256`-bound workspace catalog and checks the
   exact `(captureId, evidenceSha256)` pair. If that pair is absent, the Host replays the catalog's
   original `selection` into a temporary process-owned export and requires the
   same selection mode/EventIds/stages, complete ShaderKey set,
   `(EventId, ShaderKey)` use tuples, selected Shader bytes, capture path/API,
   and unchanged capture session. The Bridge compares both expected hashes
   before publishing any identity. A successful restart rebind records only the
   bounded exact identity pair and capture-path binding; it does not switch the
   active symbol snapshot or clear aliases. A mismatch leaves all prior Bridge
   identity/snapshot/alias state unchanged. This lazy preflight creates no workspace,
   compiles or applies nothing by itself, and is not an exception retry: a
   failed saved revision remains handled and a newer save is still required.
   A byte-identical ReplayLab copy launched from another directory does not
   rotate the managed catalog while every old pinned file remains present and
   integrity-valid; startup proves command/capability relocation equivalence
   and preserves the exact old config bytes. Missing old files or any real
   executable, argument, dependency, route or validator change still refreshes
   the catalog and takes the fail-closed migration path.
   A real route-catalog/tool-registry change deliberately makes the old
   workspace fail closed, so its listener is not resumed against different
   executables. After `migrate_shader_workspace_toolchain` creates the exact
   successor, use `migrate_shader_workspace_live_edit_authorization` with the
   preserved source SHA-256. The Host moves (never copies) the same
   authorization only when the fixed migration record directly links both
   workspace IDs, both current source files still match, and capture path/ID,
   original Shader ResourceId/raw hash, stage and entry point are unchanged.
   Repeat this explicit operation for each edge of a multi-version migration;
   no directory scan or guessed successor can inherit consent.

   `start_shader_workspace_live_edit` is an explicit persistent auto-apply
   authorization path. The user's editable/save/compile/replace request permits
   this delivery step within the task's effect ceiling; choosing an AI
   connection or Skill alone does not grant it. Explain-only and export-only
   requests do not start it. Compile-only monitoring has its own bounded watch
   tool; replay Apply retains its compile/validate/apply transaction and rollback
   evidence.
6. A compile, ABI, capture or apply failure leaves the previous
   `lastGoodReplacement` active. Stop the session or use its rollback metadata
   to restore the captured original. Stopping monitoring does not silently
   discard an already active replacement unless rollback was explicitly
   requested.

If no executable round trip exists, ReplayLab publishes non-empty
`explanatory-ir` or `binary-only` evidence and stops before watch/apply. This is
the extension point for another local provider; it is not a claim that every
engine cache, vendor ISA or assembly language is already supported.

## Visual debugging in Bread Shader Workspace

With a capture open and a draw selected, open its compilable pixel/fragment
source in Shader Workspace. The toolbar operates on the open working copy;
the temporary Source import menu has been removed.
The debug toolbar stays above the source and wraps on narrow panels. Place the caret on
a value, a function call's name, or `return`, or select a complete value expression,
and press **Ctrl+E** (or **Output to
RT**). The expression and its source line are marked while the preview is
active. **Ctrl+Shift+E** or **Restore** restores the state from
before the session, including an existing draw override. **Hold to compare**
temporarily restores that state and returns to the preview on release.

The default destination is the first bound four-component color target, or
the first bound color target if none has four components. The target selector
can override that choice. **Custom**, the default display mode, automatically
converts scalar and vector values to the target's numeric type and width:
a scalar is `(x, x, x, 1)` on a four-channel
target, a two-component value is `(x, y, 0, 1)`, and a three-component value is
`(x, y, z, 1)`; a four-component value is unchanged. A narrower target receives the corresponding leading
components. Integer targets retain integer output types. The target format
still controls quantization, clamping and sRGB conversion.

Clicking a function name observes that call's result; clicking `return` observes
its returned expression. Reads inside helper functions, call arguments, nested
arithmetic and conditional expressions can be selected independently. Probes
stay at the selected expression, so they do not move a call outside its branch
or evaluate the selected call again. The occurrence control also applies to a
helper called more than once by the same fragment.

Structs and nested structs, including structure-valued function results, open
a member picker even when only one displayable member is available. Matrix rows (HLSL) or columns
(GLSL) and fixed-size array elements are selectable. Large or unsized arrays
require selecting an indexed element in source. Numeric scalar/vector types,
including Boolean and integer values, are supported; an opaque texture,
sampler or resource handle must first be sampled/read into a numeric value.
Statements without a value (such as a bare `return;`), resource handles and
expressions whose type cannot be determined have specific diagnostics. An
`out`/`inout` argument must retain writable storage; select its value after the
call instead. The compiler and parser reject unsupported syntax or interface
layouts with a diagnostic, without applying a partially validated candidate.

The optional **Independent RGBA32F preview** uses a private floating-point
texture at the selected output slot, retaining negative and HDR values. Its
output location remains the same; color and depth/stencil attachments are
cloned so this draw cannot overwrite the captured attachments through those
bindings. The captured attachments remain at their state immediately before
the draw until comparison or stop replays the original draw. The private
texture is shown directly in Texture Viewer and is not added to the capture's
resource list. FP32 conversion has normal 32-bit floating-point precision;
large integer and double values are not represented losslessly. Unsupported
attachment topologies or unavailable FP32/MSAA
capabilities fail explicitly. Original-RT mode remains available.

Additional controls select all components or R/G/B/A individually. A selected
component displays as `(v, v, v, 1)` before any optional range/normal mapping;
unavailable components are disabled. Grayscale uses Rec.709 RGB luminance for
vectors and directly displays a scalar. Other modes provide
normal-vector mapping from `[-1, 1]`, a custom display range, execution
coverage, and the occurrence to observe in a loop or repeated function call.
Fragments that never reach the selected occurrence are marked. Texture
Viewer restores its prior follow/tab/subresource state on exit unless the
user has manually navigated elsewhere.

This is an **observation preview**, not a GPU execution breakpoint. The
selected read is observed at that expression; an assignment target is
observed after its assignment, with side-effecting accesses evaluated only
once. Later calculations and resource writes may still run to preserve
derivatives. A later `discard`/`clip` is suppressed after observation; a kill
before observation is honored. The panel displays this distinction. Debugging
is limited to the selected draw's pixel/fragment stage; it does not create a
global shader replacement or modify the saved source/workspace revision.

First use preprocesses/instruments and compiles a temporary source snapshot,
then validates the emitted interface and installs the draw override. Repeated
previews reuse bounded source plans, compiled shaders, pipelines and scratch
textures. Cache keys include source/include bytes, compiler/library identity,
compile flags, capture/shader identity, output format and display options.
Cache hits avoid recompilation and repeated ABI validation; capture, source,
ownership and remote-health checks still run. A cold new expression or new
display option can compile, and replay time remains capture-dependent.
Receipts report cache hits and high-resolution compile/replay timings.

**Debug snapshot** or **Ctrl+Shift+B** saves the selected expression, structure
member and display settings. **Debug snapshots** toggles a persistent left
sidebar alongside Shader files and Rendering stages. Entries show an ordinal,
current line and expression; they support Unicode names, drag ordering and
deletion. Clicking an entry resolves its function/token anchor against the
current editor buffer, navigates there, and creates a fresh preview. It never
stores rendered pixels or compiled shaders in the snapshot. Inserting lines
or changing earlier calculations preserves a uniquely identified location;
deleted/duplicated/ambiguous locations are marked unresolved and never guessed.
An explicitly selected RT or member that disappears is reported instead of
silently changing the requested output. Recipes are stored atomically in local
application data, keyed by workspace/source/stage/original Shader identity,
with cross-process locking. Temporary imported documents have temporary
identities; persistent workspaces retain snapshots across application restarts.

Work is asynchronous, and an older compile cannot overwrite a newer request
or resurrect a cancelled session. Switching EID retains the debug intent:
the old Draw's override is restored before inspecting the new event. A Draw
using the same Shader and compatible output receives a fresh preview with the
compiled cache reused where possible. Other Shaders, non-Draw events or absent
outputs pause it; returning to a compatible Draw resumes it. Only the current
Draw owns the preview override. Restore clears this follow intent. Source edits,
capture changes and panel closure restore an owned session. A lost remote connection is reported
as unconfirmed recovery, rather than successful restoration. Reconnect or
reopen the capture to recover when the replay peer itself is unavailable.

The native paths cover D3D11, D3D12, OpenGL, OpenGL ES and Vulkan. Linux uses
the same GL/Vulkan replay and Qt editor paths. Android uses host-side
preprocessing/compilation and the remote replay protocol; it needs no compiler
or Python UI on the device. The remote peer must have the matching override
capability. Packaging verifies pinned compiler binaries and dependencies;
Linux packages enable `BREAD_BUNDLE_SHADER_COMPILERS` to include both DXC and
glslang. Windows GPU and remote-loopback checks, Linux native build/editor and
compiler tests are recorded under
`output/shader-visual-debug-acceptance-20261028/`. Linux GPU and Android device
validation require hardware and have not been performed in this environment.

## Trust boundary

MCP calls may provide only:

- capture catalog/workspace paths;
- an exact catalog `shaderKey` or an event/stage pair;
- either no tool IDs (the recommended automatic route) or both registered
  legacy single-hop decompiler/compiler tool IDs;
- a save-watcher debounce in the fixed 100-5000 ms range, or an opaque watcher
  ID returned by the server;
- a positive catalog-backed event ID for event-scoped apply, or an opaque
  override ID returned by a successful apply for rollback.

They cannot provide a registry path, executable, command, argument template, or
environment. The registry path is fixed when the MCP server process starts:

1. `RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG`, when set by the process owner;
2. otherwise `%LOCALAPPDATA%\RenderDocUltra\shader-tools.v1.json` on Windows;
3. otherwise `$XDG_CONFIG_HOME/RenderDocUltra/shader-tools.v1.json`, falling
   back to `~/.config/RenderDocUltra/shader-tools.v1.json`.

The registry is a trust decision by the local user. Hash verification proves
which bytes will run; it does not prove that those bytes are safe. Review each
tool before registering it.

The packaged ReplayLab host's automatic discovery is deliberately narrower
than an explicit process-owner registry. It accepts FXC/DXC only from
`ReplayLab/tools` or a versioned Windows SDK `bin` directory. The portable
compile-only D3DCompiler adapter accepts `d3dcompiler_47.dll` **only** from
`ReplayLab/tools`; it never discovers a workstation/System32 copy. Community/
SPIR-V tools likewise require fixed names below `ReplayLab/tools`. The host
never searches or executes arbitrary `PATH` entries, rejects a discovered file
whose final path is a symbolic link, never downloads tools, and hashes every
accepted executable/DLL (plus known sibling DLLs) into the generated registry.
The D3DCompiler route exposes no macros, include handler or caller-controlled
flags, cannot recover HLSL, and remains subject to the same exact-profile and
full replay ABI gate as FXC output.
Legacy Shader Model 4/5 DXBC translation through `dxil-spirv` additionally
requires a compatible build containing upstream `dxbc-spirv` integration. A
file name or registration hash identifies bytes but does not prove that feature:
the pinned executable must succeed with validation enabled, emit detected
SPIR-V, and pass the untouched HLSL reverse-compile probe before source is
published as executable-editable.

## Registry schema

The following is an illustrative registry. Replace every path and 64-character
digest with the SHA-256 of the exact local file. The values shown below are
deliberately placeholders and will fail validation.

```json
{
  "schema": "renderdoc-ultra-shader-tools/v1",
  "schemaVersion": 1,
  "tools": {
    "dxbc-decompiler": {
      "executable": "D:/RenderDocUltra/Tools/DxbcDecompiler.exe",
      "executableSha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "fixedFileHashes": {
        "D:/RenderDocUltra/Tools/dxbc-rules.json": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      },
      "decompile": {
        "args": [
          "--input", "{input}",
          "--output", "{output}",
          "--rules", "D:/RenderDocUltra/Tools/dxbc-rules.json",
          "--stage", "{stage}",
          "--entry", "{entry_point}"
        ],
        "timeoutSeconds": 120
      }
    },
    "hlsl-compiler-wrapper": {
      "executable": "D:/RenderDocUltra/Tools/HlslCompileWrapper.exe",
      "executableSha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "fixedFileHashes": {},
      "compile": {
        "args": [
          "--input", "{input}",
          "--output", "{output}",
          "--baseline", "{baseline}",
          "--stage", "{stage}",
          "--entry", "{entry_point}",
          "--target-profile", "{target_profile}"
        ],
        "timeoutSeconds": 120
      }
    }
  }
}
```

The workspace reads the exact shader model/profile from a structurally
validated captured SHDR/SHEX tokenized-program header or DXIL program header.
For SHDR/SHEX, the declared DWORD length must exactly equal the aligned chunk
size; a first version token alone is not accepted. A workspace compiler must
consume the read-only `{target_profile}` value; fixed or stage-guessed profiles
are rejected.

PowerShell can calculate a digest without executing the target:

```powershell
(Get-FileHash -Algorithm SHA256 -LiteralPath 'D:\RenderDocUltra\Tools\DxbcDecompiler.exe').Hash.ToLowerInvariant()
```

Registry enforcement includes:

- absolute, regular, non-symlink executables and fixed files;
- required `executableSha256` and `fixedFileHashes` fields;
- hashes checked while loading and immediately before/after every run;
- every literal absolute file argument pinned in `fixedFileHashes`;
- no command shell, `shell=True`, Python inline code, or module launch;
- a minimal child environment and no MCP-supplied environment values;
- 1–600 second registered timeouts, best-effort process-tree termination,
  bounded logs, and bounded shader/source/output sizes;
- a cross-process fail-closed workspace operation lock;
- immutable first decompile: an existing route-bound source is never silently
  overwritten (`source/shader.hlsl`/`shader.glsl` for configured high-level
  routes, or the fixed DXBC/DXIL/SPIR-V instruction filename for an explicitly
  selected or deterministically planned instruction lane).

On Windows, the child is attached to a kill-on-close Job Object immediately
after `Popen`; this leaves a very small pre-attachment race and is not an OS
sandbox guarantee. POSIX uses a separate process group.

Available argument placeholders are fixed: `{input}`, `{output}`, `{baseline}`,
`{job_dir}`, `{workspace_dir}`, `{stage}`, `{entry_point}`,
`{captured_entry_point}`, `{revision}`, `{capture_id}`, `{raw_sha256}`,
`{target_profile}`, and `{target_api}`.

For format detection, deterministic auto-routing, official/community adapter
generation, SPIR-V validation, and round-trip diagnostics, see
[`SHADER_TOOLCHAINS.md`](SHADER_TOOLCHAINS.md).

## Bound multi-hop route chains

The normal workspace path now selects a complete round trip from the
process-owner-controlled `ToolchainCatalog`. For example, a configured DXIL
capture may bind:

```text
DXIL -> SPIR-V -> HLSL -> SPIR-V -> DXIL
```

`manifest.json.routePlan` records the exact route-catalog path and SHA-256, the
exact tool-registry SHA-256, and every forward/reverse hop's full capability
record and immutable operation identity. The plan also has a canonical
`planSha256`. Reloading a workspace reopens the process-owned catalog and
rejects changed catalog bytes, registry bytes, route metadata, operation
digests, executable/fixed-file hashes, discontinuous formats, or a changed
plan.

Every hop runs in a separate `jobs/*` directory. Its input is copied from the
previous hop, and the input/output format evidence and SHA-256 values are
recorded. A failed decompile hop leaves its job/log evidence but never publishes
the fixed source path. Before routed text is first published as
`executable-editable`, the untouched HLSL, GLSL or instruction assembly is
compiled through the exact bound reverse chain. The probe must produce the same
physical executable format and exact captured target profile/API environment;
otherwise only the failed job evidence is retained. Its hashes/jobs are stored
in `source.baselineCompile`, with
`replayAbiValidated=false` and `semanticEquivalenceVerified=false`.

Normal edited compilation then runs the same bound reverse chain and publishes
a candidate only when those physical checks pass. Neither the baseline probe
nor a later successful compile proves semantic or replay ABI equivalence.

If catalog or registry bytes legitimately change after a workspace was created,
ordinary status/compile/watch operations continue to fail closed. Upgrade with
`migrate_shader_workspace_toolchain`, supplying the exact old route-plan and
current-source SHA-256 values returned by fresh status. Migration never edits
the old workspace in place, never imports an old candidate or ABI report, and
always records `semanticEquivalenceVerified=false` in the new workspace.

The legacy pair of direct tool IDs remains supported for existing callers. It
is intentionally separate from route mode; a request cannot mix direct IDs
with a route plan, and it can never submit an executable, arguments, registry,
or catalog path.

### Current format and replay scope

| Captured executable | Editable round trip when its pinned providers pass | AI organisation | Fresh replay ABI / live replacement |
|---|---|---|---|
| D3D11 DXBC | Reconstructed HLSL to native DXBC; an explicitly registered DXBC instruction-assembly route may also be used | HLSL only | Supported for native DXBC. Default apply preserves one captured Shader ResourceId across all uses; event/family overrides remain D3D11-only |
| D3D12 DXBC | Reconstructed HLSL or registered instruction text back to native DXBC | HLSL only | Supported through the checked D3D12 shared-resource transaction |
| D3D12 DXIL | Direct community DXIL-to-HLSL reconstruction or a configured DXIL -> SPIR-V -> HLSL bridge, then exact-profile DXC output; registered DXIL/LLVM assembly may be assembled separately | HLSL only | Supported for native DXIL through the checked D3D12 shared-resource transaction |
| Vulkan SPIR-V | A configured SPIRV-Cross -> GLSL and glslang -> SPIR-V route, or automatic direct SPIR-V assembly only after a unique bound Vulkan `spirv-val` passes the baseline and untouched round-trip probes | No; edit GLSL/assembly directly | Supported for conventional graphics/compute/task/mesh only when the module has one exact entry/stage and checked rebuilding of every affected pipeline succeeds; incomplete graphics-pipeline-library and Ray paths fail closed |
| OpenGL SPIR-V | The analogous OpenGL GLSL route or validator-gated native assembly, kept distinct from Vulkan despite identical binary magic | No; edit GLSL/assembly directly | Supported for legacy graphics/compute with `GL_ARB_gl_spirv` and checked relink/revalidation of every affected program/pipeline; `glCreateShaderProgramv` ownership fails closed |
| Vulkan Ray SPIR-V | SPIRV-Cross -> GLSL plus glslang ray-stage compile, or validator-gated SPIR-V assembly | No; edit GLSL/assembly directly | Source/IR round trip can be ready; replay Apply remains blocked until exact export, shader-group, pipeline/state-object, ABI and SBT records are one rollback-capable transaction |
| DXIL Ray/Library | DXC native DXIL/LLVM text plus official `IDxcAssembler` when both pinned operations pass | No; native IR is not HLSL | Native-IR round trip can be ready with exact `lib_6_x` container identity and a separately bound export name/stage; automatic high-level HLSL recovery and replay Apply remain blocked without complete export/state-object associations |
| ESSL, MSL, WGSL, Slang, other engine caches, Metal libraries and vendor ISA | Recognized vocabulary or evidence-only until a fixed provider declares real input/output formats, stage/API rules, hashes, licence and validator | No implicit HLSL conversion | Not implemented; recognition/detection is not an end-to-end route |

The toolchain diagnostic axes are deliberately non-cumulative: recognized,
analyzable, explanatory, editable, compilable, externally validated,
replay-ABI-validated and replay-replaceable are separate facts. A row can be
recognized and explanatory while remaining non-editable; compilation can pass
while external validation and replay ABI remain false. Toolchain diagnostics
never execute a validator, validate replay ABI or authorize replacement. Ray
and Library rows may now be editable/compilable when a real source/IR round trip
is ready, while `replayStageSupported=false` and `replayUnsupportedReason`
retain the exact state-object/shader-group/SBT/export boundary.

"Supported" in the middle column means that a configured local route can be
executed and its untouched reverse-compile probe can pass. It does not promise
source fidelity or replay mutation. Community HLSL reconstruction can preserve
many calculations while losing names, macros, function boundaries and compiler
intent such as precision propagation. SPIR-V's API environment cannot be
inferred from the binary magic alone; the capture API and registered validator
remain authoritative.

## MCP workflow

### Prepare directly from RenderDoc

`prepare_shader_workspace` asks the bridge to export exactly one event and
stage. It then requires exactly one matching `uses` record, verifies its shader
key and physical blob, and creates the workspace. The default call does not
run a decompiler, RenderDoc reverse disassembly, or provisional reconstruction.

```text
prepare_shader_workspace(
  event_id=481,
  stage="fragment",
  workspace_path="D:/ShaderLabs/frame-481-ps"
)
```

Leaving both tool IDs and `editable_format` empty lets static planning select
HLSL/GLSL or an available native-IR lane; the selected plan and other registered
capabilities are returned as facts. Runtime failure never silently changes the
requested editable format. The Agent explicitly chooses a bound decompile,
an alternate registered pair, or RenderDoc evidence. The optional
`attempt_reconstruction=true` argument runs one selected decompile and publishes
the native representation and provisional scaffold on failure. SPIR-V assembly
is a native-IR option only when its direct disassembler, direct assembler and
exactly one fixed `spirv-val`
capability are integrity-valid. Selection alone is not publication: the
workspace must run the bound validator on the captured baseline and the
untouched disassemble/assemble output before writing the editable source, then
validate the baseline and output again for every edited candidate compile. Any
missing, ambiguous, changed or failing validator leaves the source/revision
unpublished. `create_shader_workspace` also accepts a pair of registered legacy
tool IDs for a catalog already exported by another operation.

An explicit `editable_format="hlsl"` keeps the HLSL recovery/AI-construction
lane and never silently changes language. Explicit `dxbc-asm` or `dxil-asm`
forces the corresponding instruction lane. Explicit `spirv-asm` and
`opengl-spirv-asm` do **not** bypass the validator gate: every native SPIR-V
source or compile lane requires the same unique, integrity-valid `spirv-val`
binding. Native-IR output is stored as `source/shader.dxbc.asm` or
`source/shader.dxil.ll`; the AI may review it but the HLSL organiser is
disabled. SPIR-V native output uses `source/shader.spvasm` or
`source/shader.opengl.spvasm`.

New routed workspaces use route-plan v2. In addition to every route hop, its
mandatory native-SPIR-V validator binding contains the validator capability,
operation/config digest, executable SHA-256 and fixed-file hashes. Successful
validator jobs retain the exact input artifact path, input byte size and hash,
physical format detection, job-local copied input, stdout/stderr paths and
hashes, exit status and complete immutable binding. `status`, compile and
candidate validation re-read those files and hashes; changing a validator input
or log invalidates the provenance. Route-plan v1 remains readable for status
and historical evidence, but a native SPIR-V v1 plan cannot publish editable
source, promote source, compile, or import a compiled candidate. It has no
validator binding and is never upgraded in place or treated as evidence that
the strict SPIR-V gate ran; a new v2 workspace must be created instead. Deleting
`validators` from v2 and recomputing `planSha256` is rejected.

If the event/stage resolves to zero or multiple shaders, preparation stops. It
does not guess.

The active-capture staging layer separates the shader-set family identity from
the exact catalog identity. `captureId` covers the capture path, API, and raw
shader identities; `catalogSha256` covers the normalized full catalog including
reflection and uses. Distinct catalog revisions for the same capture are stored
under `revisions/<catalogSha256>/catalog.json`, never overwritten. Managed
workspace paths use `v2/<workspaceBindingSha256>`, where the binding hashes the
full capture ID, catalog revision, route binding, stage, Shader resource ID, and
raw Shader hash as one canonical tuple. The separately returned
`routeBindingSha256` hashes the exact route catalog, tool registry, and
automatic-format policy. Thus every identity change selects another workspace
without concatenating several full hashes into a path that Windows tools cannot
open. `manifest.json` stores
`capture.catalogSha256`. The server hashes the selected catalog before and after
shader selection and rejects symlinks, size drift, active-binding drift, or a
mismatched manifest. Use
`get_active_capture_catalog_binding_status` to inspect this exact binding.

Legacy workspaces without `capture.catalogSha256` can still be read when their
older full catalog, shader blob, and ABI checks remain valid. They do not gain
permission to cross-bind a newer catalog revision, and they are not eligible for
automatic live catalog rebinding. Explicitly migrate or rebuild such a workspace
to establish the immutable catalog hash first.

### Create from an existing catalog

Use `create_shader_workspace` when `export_capture_symbol_catalog` has already
produced a catalog and you know its exact `shaderKey`. Then call
`decompile_shader_workspace` once.

### Use an unpacked asset tree as evidence

`configure_active_project_asset_root` stores one absolute, non-symlink
`assetRoot`; `prepare_active_capture_project` then runs bounded symbol and
recovery scans. The scanner recognises common HLSL/HLSLI/FX/CG/USF/USH, GLSL,
WGSL, Slang, MSL, ShaderLab/ShaderGraph and SPIR-V text, plus embedded
DXBC/DXIL/SPIR-V/Metal library candidates. It limits file count, per-file and
total bytes, candidate count and source-preview bytes, and never executes a
script/compiler from the asset tree.

After preparation, prefer
`query_active_shader_asset_evidence(event_id, stage, top_k)` for capture-bound
analysis. The query accepts no filesystem or database path, opens only
`shared/dictionaries/assets/symbols-v3.sqlite3` in SQLite read-only/query-only
mode, verifies the current active-project/full-catalog binding and freezes the
capture/session/EID/stage/resource/entry/raw-hash/catalog identity. Missing
configuration, catalog or database returns `ready=false` with a factual
`reasonCode`. The Agent can continue from capture evidence or prepare the active
project when asset correlation is useful and the required project/UI effects are
available. The response separates `observed` relations
from `inferred` hints and persists no new relation by itself.
`candidateCount=0` proves only that the bounded query found no match in the
frozen catalog/database snapshot; it neither proves nor disproves a concrete
Shader/asset relationship.

The current symbol schema has Shader, capture, match, alias and CBuffer evidence,
but no typed material entity or authoritative material-to-Shader/include graph.
Material-like path or metadata tokens are therefore only bounded inferred hints;
`materialEvidence.observedEntities` remains empty. Future material providers must emit a
trusted, versioned, hash-bound schema before those edges can be treated as
observed project knowledge.

Evidence levels deliberately remain distinct:

- paths, names, includes and source text are semantic hints;
- an interface/layout match is a physical compatibility clue, not an algorithm
  match;
- executable `codeSha256` equality is stronger than ABI equality;
- complete raw-container SHA-256 equality is identity evidence, but still does
  not authorize replay mutation.

Only fixed matcher output marked program-verified may supply a physical
register, space, byte offset, type/layout or reusable alias. AI may choose among
emitted candidates and rename local HLSL identifiers, but cannot manufacture a
layout. `AIProvisional`, source previews, `analysis.md` and Mermaid are never
Apply authority. Cross-capture reuse is disabled until the active project has
an explicit immutable game/build/platform/API scope, and every reused seed must
pass fresh compile, ABI and live-capture checks in the new capture.

### Edit and compile

Open the `source.path` reported by `get_shader_workspace_status` in Visual
Studio, Rider, or another editor. This is normally
`<workspace>/source/shader.hlsl` for DirectX HLSL or
`<workspace>/source/shader.glsl` for a configured SPIR-V high-level route. A
planned or explicitly requested instruction lane uses
`<workspace>/source/shader.dxbc.asm`, `<workspace>/source/shader.dxil.ll`,
`<workspace>/source/shader.spvasm`, or
`<workspace>/source/shader.opengl.spvasm`.
After saving, call:

```text
compile_shader_workspace(workspace_path="D:/ShaderLabs/frame-481-ps")
```

Each successful compile creates an immutable revision under `revisions/` and
records tool hashes, source/output hashes, command metadata, bounded logs, and a
history entry. Before replay reflection, the response reports:

```json
{
  "candidateState": {
    "present": true,
    "requiresReplayAbiValidation": true,
    "replayAbiValidated": false,
    "applied": false
  },
  "renderDocReplacement": {
    "attempted": false,
    "applied": false,
    "supportedByTheseTools": true,
    "supportedByThisOperation": false,
    "supportedCaptureScope": {
      "apis": ["D3D11", "D3D12", "Vulkan", "OpenGL"],
      "containerKinds": ["DXBC", "DXIL", "SPIRV", "OPENGL-SPIRV"],
      "combinations": [
        {"api": "D3D11", "containerKinds": ["DXBC"]},
        {"api": "D3D12", "containerKinds": ["DXBC", "DXIL"]},
        {"api": "Vulkan", "containerKinds": ["SPIRV"]},
        {"api": "OpenGL", "containerKinds": ["OPENGL-SPIRV"]}
      ]
    },
    "candidateScopeStatus": "not-evaluated",
    "applyTool": "apply_shader_workspace_shared_resource"
  }
}
```

`supportedByTheseTools` is retained as a host-capability flag. It is not a
claim about the current candidate. `candidateScopeStatus` can become
`supported-after-fresh-gates` only for D3D11/native DXBC,
D3D12/native DXBC or DXIL, Vulkan/native SPIR-V, or OpenGL/native OpenGL SPIR-V;
other combinations are `unsupported`, and unresolved status is `not-evaluated`.
Fresh replay ABI, live-event, stage, and backend gates still run during apply.

### AI source drafts and final publication

An `executable-editable` Shader Workspace can open a draft from its current
source:

```text
begin_shader_workspace_organization(
  workspace_path="D:/ShaderLabs/frame-481-ps",
  event_id=481
)
```

`begin` takes the shared source-transaction lease, preserves the byte-for-byte
decompiler output when available, and snapshots the current editable source.
It does not compile or run replay ABI validation. When an EventId is provided,
the semantic context verifies that the selected EID uses this exact captured
Shader and records the current capture/session identity and
`reconstructionFidelity`. Bound textures and pipeline state are optional hints:
the default does not fetch them, so the AI can request those tools only when
useful. Set `include_live_binding_evidence=true` to fetch both during `begin`;
each result reports `available`, `unavailable`, or `notFetched` in
`liveBindingEvidence.evidenceStatus`. A failed or malformed hint does not block
draft creation; a changed capture/session or mismatched Shader identity does.
The call returns
paths under one draft session:

```text
organized-proposal.hlsl   # editable draft; this SHA is checked at finalization
analysis.md               # optional free-form explanation
flowchart.mmd              # optional diagram artifact
module-descriptions.json  # optional function descriptions
semantic-context.json     # captured evidence, not publication authority
```

The Agent may use source-bound rename, bounded rewrite, extraction and ordinary
draft editing as needed. Mechanical edits do static and compare-and-swap checks
without compiling the entire Shader after every step. `#include`, `#line` and
other source-expanding or diagnostic-remapping directives are rejected in this
transaction. The capture's physical resource and CBuffer layout remains the
authority for final replay ABI validation. When the current draft is ready:

```text
finish_shader_workspace_organization(
  workspace_path="D:/ShaderLabs/frame-481-ps",
  organization_id="<begin result>",
  apply_shared_resource=false,
  generate_flow_graph=false
)
```

The default `finish` obtains the same source lease used by watchers, rereads
the exact source and proposal hashes, and uses compare-and-swap to make the
proposal the current editable Shader Workspace source. It records an
`editable-draft` receipt, without compiling, validating ABI or replacing replay
code. The optional `generate_flow_graph=true` builds navigation metadata from
those saved source calls and comments; failure to make a graph does not undo
the saved source. A later compile/ABI/apply uses the current source and fresh
candidate identity.

The Shader Workspace can show this current HLSL draft read-only before any
live-edit session exists. For a managed active project it discovers only
capture-matching workspaces below that project's fixed `workspaces/shaders`
directory; this view does not create a live-edit authorization. After a
current-source candidate passes replay ABI validation and an authorized live
edit is running, the editor can use its normal save/watch path.

The Agent may call `compile_shader_workspace`,
`validate_shader_workspace_candidate`, and
`apply_shader_workspace_shared_resource` directly when replay replacement is
requested. That route enforces the physical current-source and ABI checks
without requiring the optional organization equivalence receipt.

For an explicitly requested strict validation, `validate_for_apply=true`
compiles and ABI-validates the baseline and exact proposal, then checks final
equivalence before issuing its publication receipt. This optional check uses
the cheapest sufficient evidence:

- `exact-code`: candidate executable code bytes match the reconstructed baseline.
- `rename-only`: the fixed token checker proves only declared names, comments
  and whitespace changed. DXIL names may change its code hash.
- `observational-pass`: for other AI restructuring, the Host temporarily runs
  both candidates from the same captured event inputs, measures output with
  fixed absolute/relative floating-point tolerances, stricter depth tolerances
  and exact integer comparison, then restores replay state. This is bounded
  output evidence, never proof for all inputs.

The Agent chooses draft size and decomposition. The Host does not impose review
rounds or compile each segment. Unsupported sampling is not a pass. Current
numerical support is bounded; read the returned scope, counts, tolerances and
unsupported diagnostics rather than extrapolating to other outputs or stages.

Vertex observations include post-VS channels for every admitted instance/view,
with reflected scalar types and API alignment, plus raster outputs. Hull,
domain, geometry, task and mesh changes use raster/downstream-storage output
observations. Compute observes its bound storage buffers/images and counters,
never stale graphics attachments. Non-fragment full-replay observations require
a repeatable A/B/A baseline; the fragment paired-draw optimization remains
fragment-specific. All routes retain bounded coverage and rollback. Native ray
pipeline/library replacement still requires complete pipeline/state-object/SBT
support and is not authorized by an offline compile or source review.

For a Unity port these are preferred upstream checks, reusing existing validated
source or preparing it in Bread first. Permanent replay replacement is not
required. Concrete application/tool limitations permit transferring an exact
retained proposal with partial evidence and missing/failed checks, then continuing
organization/compilation/debugging in Unity. The read tool prefers current proof
and preserves reference-only status when proof is incomplete. Source-integrity,
target compilation, bindings, execution and visual checks still apply. Proof for
captured HLSL does not certify changed Unity engine interfaces or scene inputs.

The comparator reads complete bound color/depth output data in one replay
callback, using the RTV/DSV view format when the underlying texture is typeless.
It supports single-sample, single-layer 2D views with a 32 MiB per-resource and
64 MiB total budget. Enum names are normalized, including the valid
`ResourceFormatType.Regular = 0`; the bound output `Descriptor.numSlices`
determines the layer count. Unsupported outputs reject rather than silently
reducing the measured region.

Float tolerances are absolute `1e-4` plus relative `1e-3`; half-float color has
an absolute floor of `1/1024`, depth uses absolute `1e-6`, normalized color
allows one storage quantization step, and integers are exact. Every component
must pass; NaN/Inf are rejected and an average cannot hide isolated failures.
For `comparisonKind=numeric-components`, `comparedComponents` counts decoded
values. An unsupported packed format can pass only by identical storage bytes:
`comparisonKind=byte-exact` then counts bytes in the same legacy field. A
changed packed format is unsupported. Top-level `sampleCount` sums these
counts and must not be described as a pixel count or uniformly numeric samples.

An unsupported observation returns `accepted=false`, `sampleCount=0` and a
reason only after the callback confirms restoration of the previous replay
mapping. A restoration failure remains an RPC error; it cannot report
`replayRestored=true`. If `BuildTargetShader` deduplicates the baseline bytes to
the captured original ResourceId, observation runs the original pipeline
directly instead of trying to replace a Shader with itself.

Strict publication checks the external Host receipt, source bindings, declared rename
proof or complete captured-output observation. Editable JSON alone grants no
authority. Neither source nor executable hash equality proves universal Shader
equivalence; hashes identify the exact files and candidates checked. The final
gate binds workspace/capture/stage/entry, source SHA-256, revision, candidate
raw/code SHA-256 and fresh ABI request/report hashes. The completed session and
supporting evidence are committed before `ai/organize-code-gate.json` can say
`safeToStartLiveEdit=true`.

The graph remains independent navigation metadata. It is bound to workspace
ID, source SHA-256, stage and entry point; an uncompiled editable draft has no
compiler revision. The Shader Workspace marks the graph stale after later
source edits. It never authorizes compile, ABI or Apply.

The strict source/candidate transaction may optionally apply the exact validated
candidate and may register the workspace with the active project. Registration
requires the gate's `finalRevision` to equal the manifest's current `latest`
revision; an intervening editor change or compile therefore blocks publication.
Apply and registration revalidate sealed hashes and ABI evidence independently.

When optional strict validation fails, the draft remains available and the baseline
source is restored without overwriting newer editor bytes. The Agent may repair
the same draft and retry. Missing or unfinished analysis and diagram documents
do not block source finalization. The Host does not enforce readability quotas,
review rounds or partial-stage publication.

A compiler/ABI/concurrency failure restores the baseline only when it can do
so without overwriting newer editor bytes. Temporary numerical comparison
requires the `workspace + replay` effects and must restore replay state; it
does not authorize permanent replacement. Registration opens the editor and
requires `project + ui`. Final validation leaves
`semanticEquivalenceVerified=false`: none proves that all possible inputs
match the captured executable or recovers the original source.

`rename_shader_workspace_symbols` changes source tokens only. It may read an
existing user panel name as optional naming evidence, but it never writes the
external CBuffer alias store and no alias/session receipt can satisfy source
finalization. Panel display names are a separate explicit `publish_cbuffer_aliases`
operation. Semantic rows or block names on that route require a same-platform
`compiledLayoutReceipt`; unknown/overlapping fields remain unmapped and user
aliases keep priority. A failed or unavailable panel publication does not
invalidate a valid source rename or block source organization. Display aliases
do not change original reflection or resource bindings.

Ordinary user editing is a separate operation: new source and compiled code
hashes are expected. A save compares the editor's last observed hash with the
current disk bytes to avoid overwriting a newer edit, then compiles the new
source into a new revision. Apply requires that revision's current source,
candidate, capture identity and fresh ABI evidence; it does not require the
edited executable to equal the untouched reconstruction. An old source
validation or ABI receipt cannot authorize different edited bytes.

After a successful user edit is actually applied, explicit replacement
registration has its own current-source/candidate/ABI readback path for project
synchronization. It does not require the old
AI organization verdict or treat the user's new computation as a rejected
readability change. It excludes stale analysis and flow-chart artifacts.
Ordinary temporary saves do not automatically overwrite the project's shared
head. The validated HLSL and any optional explanatory artifacts are saved by
the workspace registration path.
Matching shaders in another RDC may reuse project HLSL; each target capture
still needs a new exact binding and compatible current candidate before apply.

The September 5 recovery of `6.rdc`, EID 11329, `fragment/MainPS` passed fresh
replay ABI and a captured-original versus recompiled-candidate observation.
The 1920x1080 `R16G16B16A16_FLOAT` target had 8,294,400 numeric components,
maximum absolute error `6.103515625e-05`, and zero failed components. The
`D32S8` target's 16,588,800 stored bytes were identical. Replay was restored;
the observation took about 37.4 seconds and does not prove all-input semantic
equivalence. The later organized source used the `exact-code` fast path and
its canonical HLSL, analysis and generated flow graph were registered in the
active project.

The existing external alias store accepted six original-layout block aliases:
`cbuffer0..5` map to `GlobalsCB`, `ViewCB`, `ToonMainLightCB`,
`CartoonBasePassCB`, `MaterialCollection0CB`, and `MaterialCB`. Each returned
`capturedIdentityMatched=true` and `blockAliasWritten=true`; ambiguous members
were not mapped. These are AI display aliases with user precedence, not changes
to reflection or a claim that stripped member names were recovered. Store
write evidence does not establish the original CBuffer viewer's visual state.

An independent process then used the deployed editor's actual
`/replaylab/shader-source/save` client to change output Alpha from `0.0f` to
`0.125f`. Both the edit and restoration returned HTTP 200, scheduled compile,
passed apply at revisions 3 and 4, and restored the original source bytes.
The native workspace readback independently showed `documentKind=live-edit`,
the canonical source path and `readOnly=false`; the project-library document
remained a separate read-only entry. The UI hash difference was traced to
mixed line endings: the original has 3,190 CRLF endings and 35 lone LF endings;
the actual `_document_source_text` save projection makes them all CRLF. Its
233,304-byte result exactly matches both recorded UI hashes, with every line's
content unchanged. `ShaderCodeEditor::GetText()` reads the full UTF-8 buffer;
there is no fixed-length truncation or unresolved content mismatch. The raw
save/restore evidence remains the real HTTP receipts and canonical file-byte
readback. The separate `ui-editable-normalization.json` records the matching
projection. Evidence is under repository
`output/shader-workflow-20260905/`, particularly `final-recovery-observation.json`,
`register_active_project_workspace.json`, `rename_shader_workspace_symbols.json`,
`live-save-summary.json`, the two `live-save-*-http-response.json` files and
`ui-editable.json`.

Final Development deployment and editable delivery are complete.
`final-deployment.json` verifies all three frozen executables against build
outputs and five changed Bridge modules against repository source; the Bridge
deployment updated 16 files. The deployment guard checks processes belonging
to the target application directory, so an unrelated original RenderDoc under
`Program Files` and its loaded capture remain unaffected. The restarted
Development application loaded `6.rdc` and delivered the final workspace at
`D:\Study\抓帧\ReplayLab Project\workspaces\shaders\v2\recovery-11329-20260905-ready`.

`final-delivery-check.json` records workspace ID
`64939d93b3f6454e9e11c21277b91909`, revision 2, fresh ABI validation,
`applied=true`, and `liveState=running`. The canonical document is editable and
its text matches the actual newline projection; the project's saved HLSL
matches the canonical raw bytes. The flow graph was regenerated and published
for this revision (`flowGraphCurrent=true`), with the analysis and generated
Mermaid paths read back. A separate caller's unchanged-source save succeeded
on the final internal Host with `changed=false/compileScheduled=false`.
The live session remains running for the user. This completes this capture's
recovery, organization, compile/apply, project publication and editing flow.
The earlier `...-final` functional-edit/restore evidence is kept separate from
this `...-ready` delivery. An additional restart-resume test of the new ready
session and actual reuse in a different RDC were not performed in this run;
cross-capture apply still requires the existing exact rebind and fresh ABI.

## Deterministic replay ABI gate

The workspace now contains the fail-closed Host side of the first replay ABI
gate. A capture catalog produced by the current bridge contains a complete
compiled interface and `outputTopology`. Workspace creation canonicalises and
hashes that interface. A catalog that claims `interfaceComplete=true` but omits
or malforms any required physical field is rejected. Older/minimal catalogs may
still be decompiled and compiled, but their candidates are permanently marked
`blocked-baseline-interface-unavailable` and cannot enter this gate.

For the current candidate, `candidate_reflection_request()` constructs the only
accepted bridge request. Its method is `reflect_shader_candidate`; its parameters
bind the exact candidate path, raw SHA-256, stage, entry point, API, and native
`dxbc` or `dxil` encoding. D3D11 accepts DXBC only; D3D12 accepts DXBC or DXIL.
The bridge must perform this replay-thread sequence:

1. read and hash the exact candidate bytes;
2. call `BuildTargetShader` with the exact accepted native encoding;
3. call `GetShader(ResourceId.Null(), targetId, entryPoint)`;
4. serialise the complete resulting `ShaderReflection`;
5. call `FreeTargetResource(targetId)` in a `finally` path.

`validate_candidate_reflection()` rejects the response unless both raw hashes,
byte size, stage, entry point, encoding, build status, reflection status and
temporary-resource cleanup all match. It then compares the baseline and
candidate physical interfaces, not their interface hashes alone:

- stage and entry point;
- complete input/output signatures, including semantics, registers, component
  types/counts, streams, channel masks and per-primitive rate;
- every SRV/UAV register/space, array size, descriptor/texture kind, access
  flags and recursively described element type, plus every sampler binding;
- every constant-block register/space, size and storage kind;
- every recursively nested constant field's relative offset, byte size, scalar
  type, rows/columns/elements, array/matrix stride, row-major/type flags,
  pointer-type identity and bitfield packing;
- compute thread-group dimensions, reflected output topology and the dynamic
  linkage interface count.

Display names (`cb0`, variable names and resource names) are intentionally not
ABI facts. Declaration/list order is also ignored after each item is bound to
its physical register or offset. Missing, ambiguous, duplicate, type-coerced or
internally inconsistent reflection data is rejected rather than guessed.

Stripped DXIL needs one narrower distinction. RenderDoc can still show a
CBuffer's register/space and byte size while its reconstructed member names,
grouping or source types are placeholder or contradictory. ReplayLab marks
that source-member description `ambiguous`; this does **not** mean the CBuffer
or its bytes are missing. For D3D12/DXIL only, the fixed direct DXIL
disassembler independently extracts each physical CBuffer load tuple
(`space`, register, byte offset, byte width and return scalar type) from both
the captured baseline and candidate. An ambiguous block is accepted only when
those complete access signatures and the binding/size envelope match exactly;
all non-CBuffer ABI facts remain strict. Missing evidence, a dynamic/unparsed
access, or any tuple mismatch still fails closed. Toolchain migration never
copies old access evidence: the migrated workspace regenerates it with the
current hash-pinned disassembler before compilation/ABI validation.

Consequently, readable source may rename a CBuffer or field, but it may not
change `register`/`space`, `packoffset`, byte size, scalar representation,
matrix/array stride or any physical access that the Shader performs.

An exact match writes an immutable `revisions/<n>/abi-report.json`, changes only
that candidate to `replay-reflection-abi-compatible`, and reports
`replayAbiValidated=true`. The report is hash-bound in the manifest and is fully
reparsed and recomputed on every later status/compile operation. ABI mismatch
returns bounded field-level differences and persists an immutable rejection
report under that revision. `get_shader_workspace_status` returns
`abiValidationStatus="incompatible"` and `lastAbiAttempt` with the comparison
and report binding. The candidate remains unauthorized; a fresh rejection
also revokes any older cached compatible result for those same bytes. Its
internal `abiGate` remains pending, which is an authorization state and must
not override the persisted diagnostic. Editing the source changes the
diagnostic to `source-changed`; a newly compiled revision does not inherit
the old failure. Repeated identical differences retain the first report.
Revalidating an already-compatible revision still requires a fresh bridge
reflection but idempotently retains the first audit report.

`get_shader_workspace_status` reports algorithm-reconstruction evidence
separately as `reconstructionFidelity`:

```json
{
  "scope": "captured-baseline-vs-untouched-route-round-trip",
  "available": true,
  "baselineCompileVerified": true,
  "capturedCodeSha256": "...",
  "reconstructedCodeSha256": "...",
  "codeSha256Equal": false,
  "currentSourceMatchesUntouchedReconstruction": true,
  "currentCandidateMatchesUntouchedReconstruction": true,
  "currentCandidateReplayAbiCompatible": true,
  "classification": "abi-compatible-executable-code-mismatch",
  "semanticEquivalenceVerified": false
}
```

Its classifications are `not-measured`, `executable-code-mismatch`,
`abi-compatible-executable-code-mismatch`, or
`exact-executable-code-match`. The comparison uses executable code chunks, not
the whole container hash, and refers only to the untouched route round trip.
Even `exact-executable-code-match` deliberately leaves
`semanticEquivalenceVerified=false`; no general proof is inferred for undefined
inputs or external resource behaviour. Conversely, ABI compatibility only says
the candidate can be called with the same signatures, bindings and layouts—it
does not say its instructions calculate the same values.

The ABI gate itself does not install a replacement, does not change `lastGood`,
and does not by itself authorize an apply. It accepts native DXBC for D3D11,
native DXBC/DXIL for D3D12, native SPIR-V for Vulkan, and native OpenGL SPIR-V
for OpenGL. D3D11 dynamic-linkage interfaces, Ray/Library stages and remote
replay remain fail-closed. D3D12/Vulkan/OpenGL shared-resource apply adds the
checked all-affected-derived-pipeline transaction described below; this does
not authorize root-signature, descriptor or CBuffer mutation.

## Default shared-resource apply and rollback

After validation, the normal editor flow calls:

```text
apply_shader_workspace_shared_resource(
  workspace_path="D:/ShaderLabs/frame-481-ps"
)
```

The Host repeats workspace, capture, candidate, ABI and live Shader-identity
checks. One generated target replaces the original captured Shader ResourceId
for every use, so draws remain shared instead of receiving per-event copies.
Distinct original ResourceIds are never merged merely because their bytes are
equal, and a foreign/stock Editor replacement is never silently taken over.

For D3D11, the target must be native DXBC. For D3D12, it may be native DXBC or
DXIL. Vulkan requires native SPIR-V and OpenGL requires native OpenGL SPIR-V.
D3D12, Vulkan, and OpenGL use checked transactions: before committing the
replacement mapping, RenderDoc builds and validates every affected
graphics/compute PSO, Vulkan pipeline/shader object, or OpenGL program/program
pipeline. If any derived object fails, no new mapping is committed and the new
target is released. D3D12 reuses, but does not modify or synthesize, each
captured root signature. Vulkan multi-entry modules, incomplete graphics
pipeline libraries and Ray stages fail closed. OpenGL requires
`GL_ARB_gl_spirv` and rejects `glCreateShaderProgramv` targets whose resource
identity cannot yet be replaced transactionally.

The successful response contains a `replacementId`. Roll it back with
`rollback_shader_workspace_shared_resource`; rollback removes the replay and UI
mapping and frees only the generated target. The captured original Shader is
retained throughout. Event/family overrides described next are a separate,
D3D11-only isolation tool, not the default shared edit scope.

The low-level shared-resource apply has replay effect only. Final organization
finish coordinates that apply with project persistence when requested, using
the exact successful revision, candidate SHA-256 and `replacementId`. It returns
the project-effect receipt together with the apply result. The separate
`register_active_project_shader_replacement` endpoint remains available for
metadata-only recovery and explicit low-level workflows. An inactive/read-only
project fails publication without changing the already reported replay apply
result. Registration does not compile or revalidate an unchanged candidate.

## Event-scoped D3D11 apply and rollback

First validate without mutating replay state:

```text
validate_shader_workspace_candidate(
  workspace_path="D:/ShaderLabs/frame-481-ps"
)
```

An exact ABI match creates (or idempotently rechecks) the immutable audit
report. To display the edited shader for the catalog-backed draw/dispatch:

```text
apply_shader_workspace_candidate(
  workspace_path="D:/ShaderLabs/frame-481-ps",
  event_id=481
)
```

Apply never trusts a caller-provided `validated=true` value or only a previous
report. In the same MCP call it obtains a fresh replay reflection, runs the
complete ABI comparison, reloads the workspace/catalog/candidate provenance,
and requires that the request binding is unchanged. The event must be an exact
use of the workspace shader in the trusted catalog.

The bridge then performs a second live gate on the replay thread:

1. require the workspace catalog identity and its exact selection/Shader/use
   evidence hash to be known as one pair in the current physical
   capture session and require the capture path/API to match; after a Bridge
   restart, the Host may re-establish only that exact identity through the
   fixed lazy preflight described above;
2. call `SetFrameEvent(eventId, True)` before inspecting or configuring the
   override;
3. verify D3D11, event, stage, original shader ResourceId, entry point and raw
   DXBC SHA-256 against the captured baseline;
4. reject reflected dynamic-linkage interfaces or bound class instances;
5. re-read/hash the exact revision candidate, build a DXBC target shader, and
   verify that target reflection still contains those exact bytes and the same
   complete physical ABI;
6. call `SetEventShaderOverride`, which applies only around that event's
   `OnlyDraw` replay and restores the captured shader immediately afterwards.

The target ResourceId remains owned by the bridge while the override is live.
It is not installed with global `ReplaceResource`, so action discovery,
pipeline inspection, capture loading, and unrelated events continue to use the
captured shader. Multiple workspaces exported from different event/stage
subsets of the same physical capture session can coexist even though their
catalog `captureId` values differ.

When several events used the same captured Shader ResourceId, apply them as one
family transaction instead of looping the single-event tool:

```text
apply_shader_workspace_candidate_family(
  workspace_path="D:/ShaderLabs/frame-481-ps"
)
```

With the default `scope="all-exact-uses"`, the Host discovers every matching
EventId from the trusted catalog. `list_shader_workspace_family_uses` exposes
the same result with `offset`/`limit` pagination. A family larger than 256 is
never silently truncated: automatic apply fails and requires an explicit
reviewed subset (`scope="explicit", event_ids=[...]`).

The Bridge preserves the capture's logical sharing identity. A family key is
bound to the capture session, original ResourceId, stage, entry point, baseline
raw hash, candidate bytes and Bridge-computed physical ABI. ABI report/request
IDs remain per-override audit evidence and do not split an identical target.
It builds one target and holds an override-reference set for all family events.
Equal DXBC bytes in
two different captured ResourceIds are **not** silently deduplicated. If any
event validation or setter fails, all changes made by that batch are cleared
and its earlier live overrides are restored. Repeating the exact apply is
idempotent and does not build another target.

A successful response contains an opaque `overrideId`. Roll it back with:

```text
rollback_shader_workspace_override(
  workspace_path="D:/ShaderLabs/frame-481-ps",
  override_id="<returned 32-character ID>"
)
```

Rollback calls `ClearEventShaderOverride` and then `FreeTargetResource`.
For a shared family, use
`rollback_shader_workspace_override_family(workspace_path, override_ids)`;
the target is freed only when its final event reference is removed.
Build/configuration failures clear the batch and free only its new target.
Applying a new candidate to an event/stage releases the old target only after
no other event references it. `FreeTargetResource` is never called for the
captured/original Shader ResourceId, which remains immutable and available for
every rollback.
Capture close/switch invalidates all capture-local IDs and bookkeeping. These
runtime overrides are process/session-local and deliberately do not promote
workspace `lastGood` or survive a RenderDoc restart. One capture session holds
at most 256 simultaneous event/stage overrides and 4096 known subset-catalog
identities; both limits fail closed without evicting a live override.

Use `get_shader_workspace_status` to validate hashes and inspect the baseline,
current source hash, whether the source changed after decompile, latest compiled
candidate, revision, `lastGood`, history count, and operation-lock presence.
Status is read-only and never removes the operation lock.

### Compile automatically after an IDE save

After preparation, Visual Studio, Rider, and other editors can use the same
fixed source path with a process-local watcher:

```text
start_shader_workspace_watch(
  workspace_path="D:/ShaderLabs/frame-481-ps",
  debounce_ms=100
)
```

The returned `watchId` is used only with:

```text
poll_shader_workspace_watch(watch_id="<returned 32-character ID>")
stop_shader_workspace_watch(watch_id="<returned 32-character ID>")
```

The watcher observes exactly the manifest-validated source returned by status:
for example `<workspace>/source/shader.hlsl`, `shader.glsl`,
`shader.dxbc.asm`, `shader.dxil.ll`, `shader.spvasm`, or
`shader.opengl.spvasm`, according to the bound route. The source
directory and file may not be symlinks or redirected outside the workspace. It
records the existing content hash as its initial baseline, so starting a
watcher does not immediately compile. A different UTF-8 content hash must then
remain stable for the configured debounce before the existing workspace-bound
compiler is called.

Compiles are serialized. If the file changes while a compile is running, that
candidate is kept only as an unapplied historical revision and the watcher
queues the newest stable content. `caughtUp=true` means the newest observed
hash equals the newest compiled source hash and no compile is pending. It does
not mean that RenderDoc accepted or applied the candidate.

IDE atomic-save patterns are supported: replacing the tracked source with a complete
temporary file may produce a transient missing/changed-file observation, but no
compile starts until one valid UTF-8 content hash is stable for the debounce
window. A successful watcher record binds both `latestCandidateRevision` and
`latestCandidateRawSha256`; malformed compiler results are treated as failures.
If the current file differs from either the latest candidate or, before the
first candidate exists, the immutable decompiler-source hash when watching
starts, one debounced catch-up compile is scheduled. This covers an editor save
made between workspace preparation and watcher startup instead of requiring a
second save merely to wake the watcher.
If a newer save arrives while an older compile is running, automatic apply waits
for `caughtUp=true` and uses only the newest successful stable revision.

Every watcher writes a bounded audit snapshot to
`<workspace>/watch/<watchId>.json`. It includes source hashes, attempts,
successful candidate revisions, errors, and at most 128 recent lifecycle
events. Failed compilation of one hash is not retried continuously; another
content change or an explicit manual compile is required. Compiler failures
that launched a workspace job also remain in the normal workspace manifest and
job logs. A failure never advances the immutable revision or overwrites the
previous successful candidate. In live-edit auto-apply mode it also leaves the
previously active RenderDoc replacement unchanged.

The compile-only watcher above never validates or applies. Use the distinct,
explicitly authorised live-edit session for save-to-replay:

```text
start_shader_workspace_live_edit(
  workspace_path="D:/ShaderLabs/frame-481-ps",
  debounce_ms=100
)
poll_shader_workspace_live_edit(live_edit_id="<returned ID>")
list_shader_workspace_live_edits()
migrate_shader_workspace_live_edit_authorization(
  source_workspace_path="D:/ShaderLabs/frame-481-ps-old",
  workspace_path="D:/ShaderLabs/frame-481-ps",
  expected_source_sha256="<exact migrated source SHA-256>"
)
stop_shader_workspace_live_edit(live_edit_id="<returned ID>")
```

Authorization migration does not start a watcher in the current Host process.
Call `start_shader_workspace_live_edit` on the returned target to begin
immediately, or allow the next singleton-Host startup to resume it. Any source
change on either side, non-direct/forged migration edge, physical Shader change,
active watcher, or missing old authorization rejects the move and preserves the
old registry entry.

The live monitor accepts only a post-start candidate for which the watcher
reports `compileInProgress=false`, `pending=false`, `caughtUp=true`, and
`lastCompiledSourceSha256 == observedSourceSha256`. It passes that exact source
SHA-256, revision and candidate raw SHA-256 through fresh ABI validation and
the shared-resource apply transaction. The Host rechecks the binding just
before mutation. Therefore a save arriving after revision N but before N+1
finishes cannot cause N to be applied against newer source.

This save-to-replay path covers conventional D3D11/D3D12, Vulkan, and OpenGL
graphics/compute stages admitted by their backend gates. Ray/Library saves can
still produce immutable compiled revisions, but are rejected before replay
mutation until the required export/state-object/shader-group/SBT transaction is
implemented.

On success, status records `activeReplacement`, `lastGoodReplacement` and
rollback evidence. On compile failure, ABI mismatch, capture/session change or
apply failure, that save is rejected and the previous last-good replacement
(or captured original when none exists) remains active. Failed bytes are not
repeatedly retried; save different content or compile explicitly. The
singleton-owned live-edit session started by `start_shader_workspace_live_edit`
uses this same binding and gates rather than a weaker path. A capture-session
switch stops the old monitor without applying stale work.

Safety and lifetime limits are fixed in the host implementation:

- at most eight active watchers and 32 stopped/failed jobs retained in memory;
- bounded source size, event history, error text, polling frequency, and stop
  join time;
- one active watcher per canonical workspace;
- one versioned, token-owned `.workspace-watch.lock` lease per workspace across
  MCP server processes; normal starts never steal it. During durable Host
  resume, or a fixed-toolchain migration that must precede that resume, a
  different, well-formed previous singleton-Host generation may be archived
  after transaction-residue checks. Migration keeps the replacement lease for
  its whole transaction, so there is no unlock/relock recovery window. The Host
  generation is supplied only by the process environment and is not an MCP
  request parameter. Legacy token-only locks and malformed leases always
  require explicit audit;
- the same fail-closed cross-process operation lock and trusted compiler
  binding used by manual compilation;
- daemon worker threads that are signalled and given one bounded join window on
  their owning Host's shutdown;
- stopping never cancels an already running external compiler halfway through;
  it lets that attempt finish but schedules no later attempt.

Compile-only watcher IDs are process-local. A live-edit start is different: it
creates a durable, exact-identity authorisation owned by the singleton Host and
the Host resumes it after restart. Explicit stop revokes that authorisation;
capture/shader/source identity drift or unsafe lease residue makes resume fail
closed. JSON audit files and immutable revisions remain on disk. This
implementation uses bounded content polling rather than an editor plugin or
native filesystem notification API, so no IDE is granted a separate command
execution channel.

All five public live-edit operations (start/list/poll/stop/migrate) now forward
through the private, purpose-built `/replaylab/shader-live-edit` endpoint to
the same `ReplayLabInternal` manager that handles editor source saves. Ordinary
Agent/MCP processes no longer resume or own persistent live-edit sessions.
This fixes a split-process failure where start succeeded in an Agent process
but a later editor save reached an internal manager that did not know its ID.
The endpoint has a closed operation schema, authenticated fixed loopback
transport, Host identity/generation checks, and exact caller instance/session/
capture-path binding. Apply worker threads explicitly re-enter the stored
binding rather than assuming the HTTP thread's ContextVar is inherited.

Startup identity discovery reads the workspace and exact live instance
manifest; it makes no synchronous qrenderdoc RPC while qrenderdoc may itself
be waiting for internal-Host health. Fresh ABI/apply still verifies the live
capture before mutation. Starting this singleton session and saving its
canonical source use one manager and one workspace lease, even when the
requesting Agent exits between the two operations.

Resume itself neither requires qrender to have finished loading the capture
nor replays the last compiled revision. Catalog rebinding is lazy at the fresh
ABI/apply preflight for a newly compiled save. If the capture is missing,
different, or its exact selection/Shader/use evidence cannot be reproduced,
that save fails closed and is not automatically retried; the durable watcher
remains available for a later new save. The compare-before-publish operation is
performed inside the single-threaded Bridge request loop: an expected mismatch
cannot enter either the legacy capture-ID allow-list or the exact-pair list, and
ordinary user catalog exports remain the only path that replaces the symbol
snapshot/alias context.

### Source transaction, crash and platform boundary

Organisation review and save watchers share the same token-owned
`.workspace-watch.lock` source-transaction lease. They cannot run concurrently
against one workspace. The organiser also uses source compare-and-swap with a
durable `.workspace-source-transaction.backup` and journal. On Windows it opens
the source with share-read only and locks the byte range, preventing ordinary
write/truncate/delete/rename operations through the compare/write window. On
POSIX it uses `flock`; that coordinates participating writers, while an
unmanaged rename cannot be prevented. A post-write inode/path check detects
that race and fails closed instead of claiming promotion.

Normal starts never treat a lock as stale merely because its owner process
vanished. The narrow persistent-resume/toolchain-migration exception above is
limited to a different verified singleton-Host generation and is forbidden
when a source transaction journal/backup exists. Legacy/malformed residue remains blocked:
inspect the recorded source path and hashes, restore the backup when necessary,
and only then remove the exact residue manually. Never delete a broad workspace
tree as recovery. An unfinished organisation session
is HMAC-sealed with a process-owned key and intentionally cannot be reviewed
after a server restart; start a new session after resolving any transaction
residue. Windows process-tree timeout uses a kill-on-close Job Object but has a
small pre-attachment race; POSIX uses a process group. Neither mechanism is an
OS sandbox.

## Current boundaries

### Representation lanes: never open an unexplained blank editor

Shader understanding and executable editing are separate trust lanes:

- `executable-editable` is the only compile/watch lane. It uses the route-bound
  fixed path, such as `source/shader.hlsl`, `source/shader.glsl`, or one of the
  instruction-assembly paths above. Assembly is executable instruction
  text—not recovered high-level source—and still requires its captured-format
  checks and any available validator. AI organisation remains HLSL-only;
  GLSL/assembly may be edited directly in an IDE. Only candidates inside the
  server-advertised checked combinations—D3D11/DXBC, D3D12/DXBC or DXIL,
  Vulkan/SPIR-V, and OpenGL/OpenGL-SPIR-V—can then enter fresh replay-reflection
  ABI validation and Apply, still subject to their stage and backend gates.
- `explanatory-ir` stores RenderDoc-produced native disassembly, embedded
  source, or reviewed pseudocode below `explanation/`. AI may rename, comment,
  and reorganize this evidence for human understanding, but it may never send
  this lane directly to compile, save-watch, or replay apply.
- `binary-only` is the final non-empty fallback. It preserves the captured raw
  SHA-256 and diagnostics. The current-event API also exports and re-hashes the
  complete Shader executable when it is at most 64 MiB; larger blobs are
  explicitly labelled hash-only instead of pretending the bytes were saved.

`prepare_shader_workspace` now returns `result.representation` and retains the
same object under `result.decompiled` for older clients. Use
`get_shader_workspace_representation` to retry or materialize the workspace
representation. When no complete registered round trip exists, use:

```text
get_current_shader_representation(event_id=481, stage="fragment")
```

That tool does not use the executable workspace/catalog loader. It can persist
RenderDoc reflection/disassembly for Vulkan, OpenGL, and other reflected APIs
below the process-owned representation root. Every result reports
`compileEligible=false`, `applyEligible=false`, `abiCompatible=false`, and
`semanticEquivalenceVerified=false`.

To turn reviewed pseudocode into a new experiment, write exactly
`explanation/promoted.hlsl`, record its SHA-256 and the representation SHA-256,
then call `promote_shader_workspace_explanation` with
`acknowledge_semantic_equivalence_unverified=true`. Promotion creates an
`executable-editable` source but does not compile or apply it. Same-profile
compilation, exact replay ABI validation, and separate apply authorization are
still mandatory. ABI equality proves only the callable/binding contract, never
equivalence of the changed calculations.

Implemented now:

- strict capture-catalog selection and baseline hashing;
- every status/decompile/compile operation re-opens the original catalog and
  revalidates capture ID/path/API, full ShaderKey, baseline bytes and raw/code
  fingerprints; missing or rewritten provenance fails closed;
- safe external decompile/edit/compile workspace lifecycle;
- host-owned tool registry and task-scoped MCP service;
- read-only active-project Shader asset correlation with exact catalog/database
  binding, observed-versus-inferred relations, and non-mutating `ready=false`
  fallback;
- format/API/stage-aware DXBC, DXIL and Vulkan/OpenGL SPIR-V route planning,
  finite hash-bound route-pair recovery and non-empty evidence fallback;
- proposal-only AI HLSL organisation with authoritative baseline/proposal
  compile, fresh ABI checks, exact reconstructed-code gate and source CAS;
- bounded VS/Rider-compatible save watching with latest-content convergence;
- persistent live-edit monitoring that applies only the newest caught-up,
  freshly validated save and preserves the previous last-good replacement on
  failure;
- restart-safe lazy Bridge catalog rebinding from the immutable workspace
  selection, with hash-bound exact `(captureId, evidenceSha256)` fast paths,
  identity-only Bridge publication, full Shader/use-set and capture-session
  validation, and no stale-revision retry;
- safe toolchain migration into a fresh revision-zero workspace when pinned
  catalog/registry bytes are intentionally upgraded;
- capture-bound output fingerprints for exact MRT/depth hashes plus a bounded
  deterministic pixel grid; this is observational comparison evidence, not a
  semantic proof or automatic Apply authorisation;
- complete captured-interface persistence plus replay-reflected D3D11/DXBC,
  D3D12/DXBC|DXIL, Vulkan/SPIR-V and OpenGL/OpenGL-SPIR-V ABI comparison with
  immutable validation reports;
- default sharing-preserving D3D11/DXBC plus checked D3D12/DXBC|DXIL,
  Vulkan/SPIR-V and OpenGL/OpenGL-SPIR-V resource replacement, including
  transactional rebuild of every affected derived graphics/compute object;
- capture-bound, event-scoped D3D11/DXBC Shader apply with target ownership,
  immediate replay refresh, supersede cleanup and explicit rollback;
- durable revisions, logs, locks, and status reporting.

Not implemented by these tools:

- a native Visual Studio/Rider plugin or OS filesystem-notification backend;
- a proof that AI restructuring, ABI-compatible output or community-decompiled
  HLSL is semantically identical to the captured program;
- Metal/proprietary-cache providers, Ray/Library state-object/SBT replacement,
  remote replay, or universal vendor-ISA assembly;
- semantic sliders/colour pickers or a new custom CBuffer binding. Event-scoped
  shadow-CBuffer/material sessions are implemented separately for local D3D11
  and bounded local D3D12 Root/descriptor-table CBVs. D3D12 root constants,
  ray state objects and `ExecuteIndirect` Root CBVs remain excluded.

The scoped apply layer does not promote `lastGood`; it is a reversible replay
experiment, not a durable assertion that the edited shader is correct.
