# Shader toolchain routing

The toolchain layer selects hash-pinned external shader operations by detected
container, exact stage, requested editable language, and target graphics API.
It does not download binaries, search `PATH`, or accept commands from MCP/AI
requests.

Packaged adapters run in the independent `ReplayLabShaderTools.exe`, not in
the MCP/Agent/Internal Host. Its build cache binds the adapter import closure,
runner entry point, Python runtime and PyInstaller/build dependencies by their
actual bytes. An unrelated Host rebuild reuses the same verified runner bytes.
Every launch still checks the runner, selected compiler/decompiler, required
DLLs, fixed files and command arguments. Explicit operator-owned configurations
are not rewritten. Automatic configurations keep their installation scope when
upgrading from the historical MCP adapter; that real runner change invalidates
old compile evidence rather than pretending the executables are identical.

Saved source remains readable when a selected tool changes or the historical
decompiler disappears. Status reports `toolchainStatus="refresh-required"`
with exact source/route guards and no candidate authority. Use
`migrate_shader_workspace_toolchain(..., in_place=true)` with the same source
and target workspace path to preserve the document and user edits, archive old
provenance, and bind the current compile route. The original compile route IDs,
formats and target profile remain fixed; old compiled/ABI evidence is revoked.
Compile and validate for the current capture before replacing again. This
operation does not copy a workspace, run AI review, or apply a replacement.
The existing default copy migration remains available for an explicit archive
and successor workflow. Neither path rewrites a user's source bytes.

## Non-negotiable limit

No compiler or decompiler can guarantee recovery of the original HLSL/GLSL
from arbitrary optimized shader bytecode. Compilation normally destroys
comments, macros, source file boundaries, many names, high-level control-flow
choices, and sometimes API-specific type distinctions. A high-level result is
therefore labelled `reconstructed-source`.

A candidate is not eligible for application merely because it decompiled and
compiled. The required evidence chain is:

1. detect the exact input container and executable stage;
2. choose exactly one trusted route in each direction (ambiguity fails closed);
3. reconstruct/edit source;
4. compile back to the **original container format** using the exact target
   profile encoded in the captured executable token;
5. run configured format validators where available;
6. run RenderDoc reflection and require complete ABI equality;
7. optionally compare rendered output, while treating a visual match as extra
   evidence rather than a proof for all inputs.

The round-trip smoke report explicitly keeps
`semanticEquivalenceVerified=false` and `replayAbiValidated=false`.

## Responsibility boundary

The task supplies a Shader goal, immutable scope and effect ceiling; neither an
AI profile nor a Skill grants a compiler or replay mutation. The Host exposes a
bounded catalog and effect-free observation surface. The active Agent—either a
deepagents Skill child or a vendor-native single Agent—chooses an evidence-bound
high-level route and performs bounded technical recovery. Fixed toolchain code
resolves that choice only to registered, SHA-256-pinned operations and rejects
ambiguous, unavailable or identity-incompatible providers. Model route choice
therefore never becomes an arbitrary command or permission channel.

AgentHost representation contracts keep the workflow order format-aware. A
normal executable-editable route compiles only after exact representation
readback and does not require HLSL organization, so GLSL and validated native
DXBC/DXIL/Vulkan/OpenGL SPIR-V instruction text can reach their own assembler,
validator, and replay-ABI gates. Organized compile/replace is a separate
HLSL-only route. Project publication remains reviewed-HLSL-only. ESSL, MSL,
WGSL, and Slang stay recognition/evidence rows until a Host capability plan has
real forward/reverse providers and validators; their names never grant an
executable lane.

Read-only choices can finish immediately. Workspace, project/UI publication and
replay replacement are admitted only by their individual MCP tools after fresh
scope, identity, effect, hash and ABI checks. Route diagnostics never grant a
later mutation, and ordinary capability failures cannot be relabelled to escape
those tool boundaries. A material user choice is returned through the engine's
normal clarification contract rather than a toolchain-owned workflow checkpoint.

## Two deliberately separate lanes

- The **explanation lane** is one-way and must always remain useful even when
  no high-level reconstruction exists. FXC `/dumpbin`, DXC `-dumpbin`,
  `spirv-dis`, or RenderDoc's native disassembly/IR provide executable
  calculations for inspection and AI annotation. A one-way `dxbc-asm` or
  `dxil-asm` route remains read-only; text alone never authorizes mutation.
- The **candidate lane** starts only from reviewable source or instruction/IR
  assembly that has a real same-format compiler/assembler. When no editable
  format is explicitly requested, fixed program routing prefers HLSL/GLSL and
  falls back to a complete native-IR round trip only when the preferred
  high-level route is statically incomplete during planning, before asking AI
  to construct missing source. If a selected decompiler fails at runtime, the
  program executes the finite, hash-bound alternate route-pair frontier itself.
  Each opaque candidate index creates a fresh isolated workspace, preserves the
  failed workspace, and never accepts a tool path, command, argument vector, or
  environment from AI. Explicit candidate selection remains available for an
  audited resume, but is not required for normal recovery. If the fixed frontier
  is exhausted, a compiler-backed provisional reconstruction scaffold is
  published from native evidence; it remains non-executable until hash-bound
  promotion and still requires compilation and fresh replay ABI validation. The
  automatic native-IR set includes a hash-pinned 3DMigoto DXBC assembly round
  trip and the
  official DXC `IDxcAssembler` DXIL/LLVM-text-to-container path. Vulkan and
  OpenGL SPIR-V assembly are also eligible only when one deterministic,
  integrity-valid catalog capability explicitly identifies `spirv-val` for the
  captured semantic format. The workspace v2 route plan binds that validator's
  capability, operation digest, executable hash and fixed-file hashes. It then
  actually validates the captured baseline, the untouched disassemble/assemble
  probe, and every compiled candidate before publishing an executable-editable
  source or revision. A missing, ambiguous, changed or failing validator closes
  both automatic and explicitly requested native SPIR-V lanes. A legacy v1
  native-SPIR-V plan remains readable as historical evidence but cannot
  decompile-publish, promote, compile or import a candidate. These checks still
  need fresh, API-specific RenderDoc reflection/ABI and live identity gates
  where those backends exist. No route record, validator success or AI response
  can authorize apply by itself.

This means every successfully bound/reflected Shader can retain non-empty
evidence, but a recognized format token is not by itself analyzable or
explainable, and an explanatory listing is not automatically editable or
applicable.

The diagnostic capability matrix distinguishes mere forward/reverse edge
existence from automatic editability. `automaticRoundTripReady` and `editable`
are true only when each direction has one uniquely selectable best trusted
route (and, for native SPIR-V, one uniquely selectable strict validator).
Equal-score route choices are reported as ambiguous and fail closed.

The default resource-wide replay replacement supports local D3D11/native DXBC,
D3D12/native DXBC or DXIL, Vulkan/native SPIR-V, and OpenGL/native OpenGL SPIR-V
for conventional graphics/compute stages. D3D11 still rejects dynamic linkage.
D3D12, Vulkan, and OpenGL use checked transactions that prebuild every affected
derived pipeline/program before committing the mapping; any failure aborts the
replacement. D3D12 reuses each captured root signature. Vulkan additionally
requires one exact entry/stage in the replacement module and rejects incomplete
graphics-pipeline-library and Ray paths. OpenGL requires `GL_ARB_gl_spirv` and
rejects `glCreateShaderProgramv` ownership until a target-ID-preserving checked
transaction exists. Event/family Shader overrides remain D3D11-only.
The separate material-session path supports event-scoped local D3D11 constant-
buffer bindings and bounded local D3D12 CBV-backed Root/descriptor-table
bindings; it does not authorize root-signature changes, root constants, ray
state objects or indirect Root-CBV mutation.
Vulkan descriptor/push-constant/specialization and OpenGL program-link contracts
must match fresh reflection and backend validation. Ray/library
exports/shader-groups/SBT associations and remote replay remain unsupported;
format recognition or source compilation never widens that apply gate.

## Managed startup catalog stability

ReplayLab workspaces pin the exact bytes of both managed config files. Host
startup therefore stages a newly discovered registry/catalog pair before it
decides whether to replace the active pair. Launching the same packaged tools
from another portable or installed directory does not by itself rotate those
bytes: the Host loads both pairs through the normal strict validators, freshly
hashes every executable and fixed file, and compares a path-independent
operation identity. It preserves the old managed pair only when all executable
and dependency SHA-256 values and filenames, side-by-side directory
relationships, argument templates, placeholders, timeouts, operation sets,
routes, validators and capability metadata are identical. The generated status
uses `source=existing-managed-config-relocated-copy` and retains the old
`managedConfig` hashes, so an already authorised workspace keeps its exact
catalog binding.

This is deliberately not a general toolchain-rebinding shortcut. Every file
named by the old registry must still exist as a regular non-symlink file and
pass its original hash. If an old file is missing, any byte/name/layout/command
field changed, a route/provider was added or removed, or comparison is
ambiguous, startup performs the normal managed `startup-refresh`. Existing
workspaces then fail closed against the changed catalog. Use
`migrate_shader_workspace_toolchain` to create a revision-zero successor and,
for durable auto-apply consent, explicitly move the authorization with
`migrate_shader_workspace_live_edit_authorization`. ReplayLab never silently
rewrites a workspace manifest or treats same tool bytes as Shader semantic
equivalence.

## Current route matrix

This matrix describes routes the local provider graph can generate when their
tools are explicitly packaged or registered. A particular ReplayLab bundle may
contain only a subset. The generated `shader-toolchain-status.json` is the
authoritative inventory for that installation; an absent binary means the
corresponding route is unavailable, not downloaded on demand.

| Input | Representation/edit path | Compile-back path | Status and boundary |
| --- | --- | --- | --- |
| DXBC | FXC `/dumpbin` → DXBC assembly | none | Official, non-empty explanation lane; Microsoft supplies no general SM4/5 DXBC assembler |
| DXBC | 3DMigoto 1.3.16 `-d -V` → Flugan DXBC assembly | 3DMigoto `-a --copy-reflection <exact captured baseline>` → DXBC | Native instruction-edit fallback when the preferred HLSL lane is incomplete, or an explicit lane; adapter verifies the pinned tool and baseline hashes, refuses derived-output overwrite, and keeps replay ABI/semantic-equivalence gates separate |
| DXBC | HLSLDecompiler → HLSL | pinned `D3DCompiler_47` adapter (preferred) or FXC → DXBC | Community SM4/SM5 reconstruction; exact `SHDR/SHEX` profile; fresh D3D11 or checked D3D12 shared-resource ABI/apply gate still required |
| DXBC | compatible dxil-spirv build with upstream dxbc-spirv → SPIR-V → SPIRV-Cross HLSL | pinned `D3DCompiler_47` adapter (preferred) or FXC → DXBC | Optional community fallback; file name/hash registration is not feature proof, so validated execution and the untouched reverse-compile probe must succeed; DirectX→Vulkan descriptor semantics can change |
| DXIL | locally source-built RenoDX `decomp.exe` → HLSL | DXC → DXIL | Preferred SM6 reconstructed-source route when packaged. The adapter binds the exact captured entry/signature and rejects unresolved row packing. Names and source structure are not original; untouched compilation, full ABI, exact DXIL CBuffer access, and replay gates remain mandatory |
| DXIL | DXC `-dumpbin` → DXIL/LLVM text | official DXC `IDxcAssembler::AssembleToContainer` → DXIL | Automatic native-IR fallback when no DXIL→HLSL round trip exists and the assembler adapter is locally packaged; it does not recover HLSL, and every candidate still requires exact profile/container checks plus fresh RenderDoc ABI and driver acceptance |
| DXIL | dxil-spirv → SPIR-V → SPIRV-Cross HLSL | DXC → DXIL | Community translation fallback, not an algorithm-equivalence guarantee. Descriptor/resource/indexing semantics may change across the DirectX→Vulkan→HLSL bridge; exact `DxilProgramHeader.ProgramVersion`, untouched compilation, full ABI/access checks, and checked D3D12 all-affected-PSO rebuild are required |
| SPIR-V | SPIRV-Cross → GLSL | glslang → SPIR-V | Reconstructed Vulkan GLSL; exact entry/stage; explicit trusted Vulkan target environment required. glslang ray suffixes are registered for Vulkan Ray stages; this proves a source round trip only, not SBT/state-object replacement |
| SPIR-V | SPIRV-Cross → HLSL | DXC → SPIR-V | Listed diagnostically, but automatic round-trip fails closed because an exact HLSL target profile cannot be physically derived from SPIR-V |
| SPIR-V | `spirv-dis` → SPIR-V assembly | `spirv-as` → SPIR-V | IR editing rather than high-level recovery; automatic fallback additionally requires one pinned Vulkan-environment `spirv-val`, successful baseline and untouched round-trip probes, and validation of every candidate |
| OpenGLSPIRV | SPIRV-Cross without Vulkan semantics → GLSL | glslang `-G --target-env opengl` → OpenGLSPIRV | Capture encoding is required; binary magic alone cannot distinguish it from Vulkan SPIR-V |
| OpenGLSPIRV | `spirv-dis` → OpenGLSPIRVAsm | `spirv-as` → OpenGLSPIRV | Physically shared syntax, logically separate route; automatic fallback requires the separately bound `spirv-val --target-env opengl` capability and the same baseline/round-trip/candidate probes |
| HLSL | source | pinned `D3DCompiler_47` adapter/FXC for DXBC; DXC for DXIL | Native compile, but interface/ABI still depends on the fixed compiler policy and exact target profile |
| GLSL | source | glslang | Separate `-V` Vulkan and `-G` OpenGL SPIR-V candidates; never cross the environments implicitly |
| DXIL Ray/Library | DXC `-dumpbin` → DXIL/LLVM text | official DXC `IDxcAssembler::AssembleToContainer` → DXIL | Native-IR round trip may be editable when both pinned operations pass. The container profile is recovered exactly as `lib_6_x`; the selected export name/stage remains a separate capture/reflection identity. No automatic high-level HLSL recovery is claimed, and replay replacement remains blocked without complete export/state-object/SBT identity |

## Capability axes and non-routes

`diagnostic_matrix` reports separate capability axes; none is an alias for the
next one:

| Axis | Exact meaning |
| --- | --- |
| `recognized` | The router accepts the format identifier. This may be vocabulary or conservative text detection only. |
| `analyzable` | Text can be inspected, or an integrity-valid provider can consume the format. It is not proof of an algorithmic explanation. |
| `explanatory` | A ready forward route can emit review evidence. It may be one-way and non-executable. |
| `editable` | A reverse route and every strict static prerequisite exist. Runtime baseline/compiler/validator probes must still pass before the workspace publishes the editable lane. |
| `compilable` | A ready route can emit a binary artifact. This says nothing about external validation, replay ABI or semantic equivalence. |
| `externalValidatorReady` | A hash-pinned validator is configured and integrity-valid. Diagnostics do not execute it; workspace provenance records the executions separately. |
| `replayAbiValidated` | Always false in toolchain diagnostics. Only fresh capture-bound replay reflection can set this fact. |
| `replayReplaceable` | Always false in toolchain diagnostics. Replacement is a separate API/backend transaction. |
| `workspaceStageSupported` | The stage may participate in a registered source/IR round trip. This remains true for Ray/Library; actual editability still depends on unique ready routes and validators. |
| `replayStageSupported` | Whether this stage class is admitted to the ordinary replay replacement transaction. It is false for Ray/Library even when source/IR round trip is ready. |
| `replayUnsupportedReason` | The exact stage-level replay boundary, kept separate from route-level `unsupportedReason`. |

DXBC, DXIL, Vulkan SPIR-V and OpenGL SPIR-V have physical binary adapters.
HLSL and GLSL have generated compiler routes when the corresponding fixed tools
are installed. ESSL, MSL, WGSL and Slang are recognized format vocabulary (and
some have conservative text markers), but the generated catalog does not claim
end-to-end decompile/compile routes for them. A custom provider must declare and
pin an actual edge before any such row becomes ready. Ray stages can report a
real Vulkan GLSL/SPIR-V round trip, and Ray/Library DXIL can report a native-IR
round trip, when those exact providers are ready. Diagnostics still report
`replayStageSupported=false` and a separate replay reason because per-export,
state-object/shader-group/SBT replacement is not implemented.

DXC provides DXIL disassembly (`IDxcCompiler3::Disassemble` / `dxc -dumpbin`),
not a general DXIL-to-HLSL decompiler. ReplayLab can separately register the
community RenoDX SM6 decompiler and the `dxil-spirv -> SPIRV-Cross` bridge, but
keeps both labelled reconstructed/best-effort and independently gated. Mesa NIR is an internal optimizing IR
used inside Mesa compiler stacks, not a portable original-source recovery CLI,
so it is not advertised as an HLSL decompiler route.

## Exact compiler profile

The workspace and round-trip runner never substitute a fixed `ps_5_0`,
`vs_5_0`, or stage-based guess:

- DXBC reads `VersionToken` only from a complete tokenized program header in
  the single `SHDR`/`SHEX` executable chunk. The chunk must contain both the
  version and `Length` DWORDs, be DWORD-aligned, and have
  `Length * 4 == chunk size`; a plausible first DWORD in truncated or
  contradictory bytes is rejected. SM 4.0, 4.1, 5.0, and 5.1 are supported.
  A container with an `Aon9` part fails closed: its ordinary-looking `*_4_0`
  token does not unambiguously recover the original
  `*_4_0_level_9_x` compiler target.
- DXIL reads `DxilProgramHeader.ProgramVersion` only after validating the full
  `DxilProgramHeader` and `DxilBitcodeHeader`: the part is at least 24 bytes,
  `SizeInUint32` exactly equals the part size, the inner magic is `DXIL`, the
  non-empty bitcode range starts after its header and stays in bounds, and its
  padded end agrees with both other size declarations. Truncated, overlapping,
  overflow-like, extra whole-DWORD trailing data, and contradictory headers
  fail closed.
- Stage and profile are cross-checked against the capture stage. For a DXIL
  `Library` container, `ProgramType` proves only the physical `lib_6_x`
  profile; a selected RayGen/Miss/Hit/Callable export stage comes from the
  capture shader key and must agree again with fresh candidate reflection. The
  container is never relabelled as a single-entry program.
- DXIL library/ray kinds have an exact `lib_6_x` profile, but no automatic
  high-level HLSL recovery/compile route is registered for them. A separately
  registered DXIL native-IR round trip can edit and reassemble the complete
  module without inventing a source entry target, but still cannot authorize
  replay apply.

The exact value is stored in `manifest.json.targetProfile`, re-derived whenever
the workspace is loaded, and supplied to a compiler only through the read-only
`{target_profile}` placeholder. A caller cannot override a DirectX token with a
different profile, and a non-DirectX input cannot manufacture one from its
stage name.

The portable `D3DCompiler_47` route is compile-only. Its hash-pinned adapter
loads one explicitly registered absolute `d3dcompiler_47.dll` path with
`D3DCompile`, passes only `{input}`, `{output}`, `{entry_point}` and
`{target_profile}`, and fixes both compiler flag words to zero. It supplies no
macro table or include handler, so `#include` is intentionally unsupported and
fails closed. If FXC is also registered, the portable route has the unique
better priority; callers never choose between equal routes. This adapter does
not reconstruct HLSL and does not make FXC/D3DCompiler disassembly editable.
Every output still has to pass container/profile checks and the full replay ABI
gate before it can be applied.

SPIRV-Cross is never invoked directly by a generated registry. Its bundled,
hash-pinned adapter receives both the independently parsed physical SPIR-V
entry and the capture-visible entry, plus stage, target profile, and capture
API. The DirectX-to-SPIR-V producer is not given the capture-visible name as an
unverified physical selector. After translation, the route parses every
`OpEntryPoint`: an exact requested match is used, otherwise exactly one entry
for the captured stage may be selected; missing or multiple stage matches fail
closed. For HLSL, SPIRV-Cross then mechanically renames that physical entry to
the capture-visible name, supplies `--entry`/`--stage`, and derives
`--shader-model` from the exact profile (for example, `ps_6_7` becomes `67`).
Missing facts, a profile/stage contradiction, a non-DirectX HLSL target, or an
unrepresentable profile fail before SPIRV-Cross starts. This prevents its
upstream defaults (first module entry point and HLSL Shader Model 3.0) from
silently selecting the wrong program.

## Explicit SPIR-V environment

The SPIR-V binary header identifies a SPIR-V version, but it does not prove the
consumer API environment or Vulkan version/extension contract. A generic
capture API label such as `Vulkan` is also insufficient. Generated configs
therefore have no inferred/default validation environment:

- `--spirv-target-env` is mandatory when `glslang` or `spirv-val` is supplied;
- accepted generated Vulkan environments are currently `vulkan1.0`,
  `vulkan1.1`, `vulkan1.2`, and `vulkan1.3`;
- the exact literal is bound into glslang, DXC SPIR-V, and `spirv-val`
  operations (`spirv-val --target-env ...`), so the validator can never fall
  back to its generic Universal default;
- if the capture/tool owner cannot establish the intended environment, the
  SPIR-V compile/validation route must not be configured. No version is guessed
  from the module header.

This environment choice is validation context, not semantic-equivalence or
RenderDoc replay-ABI evidence.

RenderDoc also exposes distinct `ShaderEncoding::SPIRV` and
`ShaderEncoding::OpenGLSPIRV` values. The word stream itself is physically
indistinguishable, so an OpenGL route is generated only with the explicit
`--enable-opengl-spirv` operator choice and is selected only when capture
metadata says `GraphicsAPI.OpenGL` plus `OpenGLSPIRV`. The corresponding fixed
commands use SPIRV-Cross without `--vulkan-semantics`, glslang `-G
--target-env opengl`, and `spirv-val --target-env opengl`.

## Generate trusted local configs

Supply absolute paths to binaries installed or built by the local operator:

```powershell
python -m src.shader_workspace.toolchain_config `
  --tools-output D:\RenderDocUltra\shader-tools.v1.json `
  --routes-output D:\RenderDocUltra\shader-toolchains.v1.json `
  --d3dcompiler 'C:\Program Files (x86)\Windows Kits\10\Redist\D3D\x64\d3dcompiler_47.dll' `
  --fxc 'C:\Program Files (x86)\Windows Kits\10\bin\...\x64\fxc.exe' `
  --dxc D:\VulkanSDK\Bin\dxc.exe `
  --renodx-decompiler D:\ShaderTools\decomp.exe `
  --spirv-cross D:\VulkanSDK\Bin\spirv-cross.exe `
  --glslang D:\VulkanSDK\Bin\glslangValidator.exe `
  --spirv-dis D:\VulkanSDK\Bin\spirv-dis.exe `
  --spirv-as D:\VulkanSDK\Bin\spirv-as.exe `
  --spirv-val D:\VulkanSDK\Bin\spirv-val.exe `
  --spirv-target-env vulkan1.1 `
  --enable-opengl-spirv `
  --hlsl-decompiler D:\ShaderTools\CMD_Decompiler.exe `
  --dxil-spirv D:\ShaderTools\dxil-spirv.exe
```

The generator:

- performs no network access or binary discovery;
- hashes every executable;
- pins `d3dcompiler_47.dll` together with the fixed ctypes adapter host/script;
  this compile-only route needs neither `fxc.exe` nor a Windows SDK installation
  on the destination machine when the DLL is lawfully packaged;
- registers official FXC/DXC dump operations as explanation-only routes;
- automatically pins sibling `dxcompiler.dll` and `dxil.dll` when present;
- pins a locally supplied RenoDX `decomp.exe` together with its side-by-side
  `dxcompiler.dll` and the fixed entry/signature adapter;
- pins the HLSLDecompiler adapter and `CMD_Decompiler.exe` together;
- pins the SPIRV-Cross adapter and executable together; callers supply only
  physical workspace facts, never free-form SPIRV-Cross arguments;
- accepts repeated `--pin PROVIDER=ABSOLUTE_PATH` for additional DLL/data
  dependencies;
- binds every route to the exact operation digest and the route catalog to the
  exact tool-registry file hash;
- refuses to overwrite configs unless `--force` is explicit.

Dynamic dependencies loaded from system directories cannot all be proven by
this registry. Pin local companion DLLs and install tools only from a trusted
source.

Start the MCP server with both process-owner-controlled paths. MCP callers
cannot override either path:

```powershell
$env:RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG = 'D:\RenderDocUltra\shader-tools.v1.json'
$env:RENDERDOC_ULTRA_SHADER_TOOLCHAINS_CONFIG = 'D:\RenderDocUltra\shader-toolchains.v1.json'
python -m src.server
```

## Deterministic API

```python
from src.shader_workspace import ToolchainCatalog

catalog = ToolchainCatalog(tool_config, route_config)

selection = catalog.select_round_trip_for_file(
    captured_shader,
    stage="fragment",
    target_api="d3d11",
)

matrix = catalog.diagnostic_matrix("fragment")

report = catalog.round_trip_smoke(
    captured_shader,
    editable_format=selection["editableFormat"],
    output_directory=smoke_directory,
    stage="fragment",
    entry_point="main",
    target_api="d3d11",
)
```

`select_round_trip_for_file` returns `why`, full capabilities, source URLs,
licenses, caveats, exact target profile, and both route/tool ID sequences. If
two equally ranked route combinations exist, it rejects auto-selection. An
advanced trusted caller can inspect `matching_routes` and choose an explicit
route ID. Task-scoped Agents do not receive that unrestricted internal route-ID
surface; they can select only the high-level catalog candidate and opaque,
Host-listed recovery alternatives.

Unpacked-project Shader/material-like/CBuffer evidence may improve explanation,
names and provenance, but it never changes executable-route eligibility or
authorizes a compiler, validator, ABI claim or replay replacement.

The same operations are exposed through three bounded MCP tools:

- `get_shader_toolchain_capabilities(stage)` only re-hashes and reports the
  matrix;
- `plan_shader_toolchain_route(shader_path, stage, ...)` detects and selects a
  unique complete round trip without running tools;
- `run_shader_toolchain_round_trip_smoke(shader_path, output_directory, stage,
  editable_format, ...)` executes only the configured, digest-bound route. A
  route using the SPIRV-Cross adapter additionally requires the capture's
  target API; it has no executable, command, argument-list, config-path, or
  target-profile parameter.

## Primary sources

- [RenderDoc `ShaderEncoding`](https://github.com/baldurk/renderdoc/blob/v1.x/renderdoc/api/replay/replay_enums.h)
  defines distinct Vulkan `SPIRV`/`SPIRVAsm` and
  `OpenGLSPIRV`/`OpenGLSPIRVAsm` encodings and separate known-tool mappings.
- [Microsoft DirectXShaderCompiler](https://github.com/microsoft/DirectXShaderCompiler)
  documents HLSL→DXIL/SPIR-V compilation and the compiler/disassembler/
  validator APIs.
- [DXC `DxilContainer.h`](https://github.com/microsoft/DirectXShaderCompiler/blob/main/include/dxc/DxilContainer/DxilContainer.h)
  defines the DXIL program-version fields used for exact profile extraction.
- [Microsoft FXC documentation](https://learn.microsoft.com/windows/win32/direct3dtools/dx-graphics-tools-fxc)
  documents `/T`, `/E`, `/Fo`, `/Fc`, and `/dumpbin` for legacy HLSL
  compilation/disassembly.
- [Microsoft `D3DCompile`](https://learn.microsoft.com/windows/win32/api/d3dcompiler/nf-d3dcompiler-d3dcompile)
  defines the fixed in-process HLSL-to-bytecode call used by the portable
  adapter. ReplayLab passes null macro/include pointers and fixed zero flags;
  redistribution rights still depend on the DLL package actually supplied by
  the bundle builder.
- [Khronos SPIRV-Cross](https://github.com/KhronosGroup/SPIRV-Cross) describes
  SPIR-V reconstruction to HLSL/GLSL and explicitly notes incomplete obscure
  feature coverage. Its [`main.cpp`](https://github.com/KhronosGroup/SPIRV-Cross/blob/main/main.cpp)
  documents that entry selection otherwise defaults to the first module entry,
  implements `--entry`/`--stage`, and enables the emitted HLSL entry name after
  an explicit rename. Its
  [`spirv_hlsl.hpp`](https://github.com/KhronosGroup/SPIRV-Cross/blob/main/spirv_hlsl.hpp)
  options default to Shader Model 3.0 unless `--shader-model` is supplied.
- [Khronos glslang](https://github.com/KhronosGroup/glslang) is the reference
  GLSL front end/SPIR-V generator. Its HLSL front end is deprecated as of April
  2026, so generated configs do not use it for HLSL.
- [Khronos SPIRV-Tools](https://github.com/KhronosGroup/SPIRV-Tools) provides
  `spirv-dis`, `spirv-as`, and `spirv-val`; upstream states validator coverage
  is incomplete/one-sided. The upstream
  [`spirv-val` CLI source](https://github.com/KhronosGroup/SPIRV-Tools/blob/main/tools/val/val.cpp)
  defines `--target-env`; generated configs always supply it explicitly.
- [3DMigoto 1.3.16 `cmd_Decompiler` source](https://github.com/bo3b/3Dmigoto/blob/cbc4993/HLSLDecompiler/cmd_Decompiler/cmd_Decompiler.cpp)
  defines `.hlsl` for `-D`, `.asm` for `-d`, and `.shdr` for `-a`. The HLSL
  adapter uses `-D -V --stop-on-failure`. The assembly adapter uses
  `-d -V --stop-on-failure`, whose validation reassembles the untouched listing
  and compares DXBC sections, then uses `-a --copy-reflection` with a
  job-local snapshot whose SHA-256 equals the captured baseline. In 1.3.16 the
  post-`-a` validation branch is still a TODO, so the adapter deliberately does
  not claim `-V` validates edited assembly. Successful assembly is an
  executable candidate, not HLSL recovery or semantic-equivalence proof. The
  3DMigoto source is GPL-3.0; verify the license/provenance of the supplied
  binary.
- [dxil-spirv](https://github.com/HansKristian-Work/dxil-spirv) is an MIT
  community DirectX→SPIR-V translation project used by D3D translation stacks.
  Its [`dxil_spirv.cpp` CLI source](https://github.com/HansKristian-Work/dxil-spirv/blob/master/dxil_spirv.cpp)
  defines the configured input, `--output`, `--validate`, and `--entry`
  arguments. Current upstream builds integrate `dxbc-spirv`; the same upstream
  executable's
  [`dxil_spirv_c.cpp` parser](https://github.com/HansKristian-Work/dxil-spirv/blob/master/dxil_spirv_c.cpp)
  dispatches legacy DXBC blobs through `parseDXBCBinary`. This justifies a
  conditional DXBC→SPIR-V route for compatible builds; it does not prove that
  an arbitrary local executable with that name contains the integration. The
  generated registry pins its exact hash, execution uses validation, output is
  independently detected as SPIR-V, and editable HLSL is withheld unless the
  untouched reverse compile succeeds. None of those checks establish source or
  semantic equivalence, and this is not a Microsoft-supported recovery path.
- [Mesa NIR documentation](https://docs.mesa3d.org/nir/index.html) describes NIR
  as Mesa's internal optimizing compiler IR.
