# Bread runtime packaging

Before packaging, read [RELEASE_REQUIREMENTS.md](RELEASE_REQUIREMENTS.md) and the
[sealed dependency and redistribution inventory](../dependencies/README.md).
These are standing release requirements, also linked from the root `AGENTS.md` and
`CLAUDE.md`.

Start packaging only after the user explicitly authorizes it for the current
task. Keep work under development uncommitted on its local branch. Obtain
explicit authorization before committing, pushing, opening a PR, merging,
deleting a branch, tagging, uploading or publishing.
The user may authorize several named steps together.

For authorized GitHub delivery, follow the
[branch and release completion workflow](../../CLAUDE.md#branch-and-release-completion)
and [release completion requirements](RELEASE_REQUIREMENTS.md#8-github-主分支合并与分支清理).
Verify the `origin` repository and its actual default branch. Verify inclusion
before any authorized branch cleanup. Use an explicit
`--repo Straw1997/renderdocBread` for `gh` commands.
Preserve published tags/artifacts and unrelated work. Documentation-only follow-ups
do not require rebuilding an existing release.

## RenderDocBread portable Windows release

Double-click `PACKAGE_RENDERDOCBREAD.cmd` in the repository root, or run
`./replaylab/packaging/Build-RenderDocBread.ps1`. The standard package writes
`dist/RenderDocBread-<version>-win-x64.zip` and its `.sha256` file using the frozen
Host version. Other package profiles use timestamped names. The executable is
`RenderDocBread.exe`. The archive has one enclosing `RenderDocBread` directory.
Publish an update using the `autoUpdate` values in the package configuration and
the [automatic update release contract](RELEASE_REQUIREMENTS.md#自动更新发布约定).

Release identity follows `versioning` in `RenderDocBread.package.json`:
`<integrated RenderDoc major>.<integrated RenderDoc minor>.<extensionRevision>`.
The first two components come from `renderdoc/api/replay/version.h`, never the
latest online release. Keep the extension revision increasing across upstream
upgrades. After changing that one configured revision or integrating upstream,
run `python replaylab/packaging/version_policy.py --write`; it updates the native
Bread version header and all four Python version declarations. `--check` verifies
the generated sources without writing, and both build entry points run that gate
before compiling. Packaging does not increment or silently repair version metadata.
The title, About, CLI version and Windows EXE resources share that header, while
the core's upstream API/ABI version remains separate. Full and compact packages
check actual GUI and Host versions and record upstream/revision provenance in
the release manifest. A stale native EXE is rejected, including with skip-build
options. Existing `1.13.xxx` updater versions remain numerically comparable with
the new `1.46.xxx` series; do not rename historical tags or release archives.

`RenderDocBread.package.json` is the checked-in release configuration. Paths in this
configuration are relative to the repository or application; release manifests contain
relative paths and hashes. Assembly uses an allowlist and a private OS temporary directory.
It never deploys to the running application's directory or stops any process. Native build
output is isolated under `output/RenderDocBread-native`; native project references and
matching Python bindings are built from source, including after a clean checkout.
The packaging and Development entry points enable optimized replay drivers by
default. `-DisableReplayOptimization` produces an explicit unoptimized control;
record this choice in performance comparisons. Native builds must use a single
consistent source path: mixing a junction-based `SolutionDir` with projects
opened through their original path can defeat MSVC precompiled-header identity.

Build prerequisites: VS2022 C++/VC143 redistributable files, Windows SDK, 64-bit CPython
3.12, and x64 .NET SDK **10.0.302** (apphost pack **10.0.10**). The ordinary Git checkout includes all sealed archive
parts. `Restore-BuildEnvironment.ps1` assembles them offline and checks the lock
file before use. Qt Linguist, Shader tools, the
language service, Unity provider, UEX source and locked Python/NuGet dependencies are
restored from sealed repository inputs. UEX is published with `-r win-x64 --self-contained
true` so the recipient needs no .NET SDK or modern .NET runtime installation. Use
`-UexSource` for another checkout or `-UexPublishDirectory` for an already self-contained
publish. Package assembly does not use a developer's account or fetch dependencies.

Optional parameters include `-BuildInputRoot`, `-ApplicationDirectory`, `-Destination`,
`-PythonExecutable`, `-QtBin`, `-CapabilitiesDirectory`, `-UexSource`,
`-UexPublishDirectory`, `-DotNetExecutable`, and `-AuditOnly`. `-SkipNativeBuild` and `-SkipRuntimeBuild` explicitly reuse previous builds;
omit them for a release of the current source. No force switch can bypass the release audit.
`New-BreadPackage.ps1` remains a compatibility entry point for packaging previous builds.

For a low-space checkout, `Build-ReplayLabRuntime.ps1` accepts
`-TemporaryBuildRoot` naming a `bread-runtime-*` directory directly under the system
temporary directory; explicitly place both output and work directories below it.
The default still stays inside the checkout, and cleanup remains limited to marked
build sessions. Local patch deployment reuses an existing capability directory only
after its sealed files and dependency inventory exactly match the selected bundle.

The default standard edition keeps the complete public skill inventory, document SDKs
(python-docx, openpyxl, python-pptx, pypdf), Python/Node runtimes, desktop adapters,
provider clients, Shader tools, unpackers and Unity MCP. It excludes Office and bundled
Chromium. Optional browser operations use an installed system browser or a configured
browser MCP; ordinary RDC and document operations require neither.
`prepare_standard_capabilities.py` derives this inventory from a verified complete
application-owned snapshot, with licenses and relative paths retained. Private
provider clients still require their actual live service; login alone does not
create a desktop service. No credentials are packaged. Configured MCP tools are
searchable by capability without an @ mention. The smaller RDC and core profiles
remain explicit build choices, not automatic fallbacks for missing dependencies.

For an update with unchanged native dependencies, build the runtime, then use
`build_compact_release.py --base <verified-release.zip> --runtime <runtime-output>
--gui <built-RenderDocBread.exe> --output <new-release.zip> --receipt <receipt.json>`.
Omitting `--capabilities` uses the configured standard snapshot. The script rewrites a fresh archive,
verifies every byte and the current frozen-source fingerprint, and never leaves
unreachable superseded payloads in the ZIP. Extract into a private OS temporary
directory and run the normal audit/compiler/relocation smoke checks before
delivery. Native or dependency changes use the full build/audit/smoke path.
The old append-based finalizer is not suitable for shrinking a release.

The package includes the canonical Python bridge, every current repository skill folder and its references,
frozen AI/MCP/internal/Shader hosts, Shader compilers and navigation server, UnityPy,
the pinned Coplay Unity MCP server with a private relocatable CPython 3.12 runtime,
Unity Editor connection/import packages, UEX, AssetStudio, umodel, Qt/Python and app-local
VC runtime DLLs. Optional x86 injection/Android artifacts are included when present in the
source application; this x64 package does not claim those targets are built or tested.
External Unity Editor/Blender installations, GPU drivers, games, third-party subscription
CLIs and account login are not included. Model API connections work through the bundled
host after the recipient supplies their own connection. Private connection files, tokens,
chat/project data and caches are excluded.

UEX's three pinned Oodle/zlib/Detex native codec DLLs are explicit dependencies.
Their upstream code expects the legacy `.uex-cache` location, so the assembler
copies exactly those three sealed files there; it does not copy an existing
cache directory. The audit rejects other cache contents or changed DLLs.
UEX also receives its own `vcruntime140.dll` for its native DLL search path.
Packaging tests actual native loading and a native zlib decompression roundtrip.

`prepare_unity_bundle.py` verifies the pinned provider archive and frozen dependency lock,
creates a build-only environment below `replaylab/output/unity-provider-cache`, and copies
its runtime into `replaylab/tools/unity-mcp`. Its relative `python312._pth` disables Python
registry, environment and machine-specific `.pth` discovery. Installed Unity projects use
that co-located interpreter through Bread's existing project-scoped MCP route; no recipient
Python/uv download is needed. The ordinary development install retains its on-demand setup.

The marker `RenderDocBread.portable` selects the bundled AI host and dedicated UI settings
under `%LOCALAPPDATA%/RenderDocBread/ui`, avoiding another RenderDoc UI profile or a source
venv/PATH host. Existing Bread data/connection paths under `%LOCALAPPDATA%/ReplayLab` remain
compatible. It does not register file associations, install a Vulkan layer, enable global
hooks or change the registry during extraction/startup. Vulkan capture may still require an
explicit capture-layer registration; replay needs no registration. The build-side checks
cover relocated startup with a restricted PATH, frozen imports, exact runtime hashes,
DXC capabilities, navigation-server integrity and path/privacy scans. Windows 10 22H2 and
Windows 11 x64 are the release targets; real GPU replay/capture and clean-machine testing
on both systems remain separate validation, not a claim made by the packaging script.

The release smoke also opens the real main window without a pre-set
`RENDERDOC_CONFIG_DIR`, using isolated ASCII, Chinese and supplementary-Unicode
user directories. It verifies both `UI.config` and the core's `renderdoc.conf`
under the automatic portable profile, plus preservation of an explicit Unicode
override. Run this gate separately with
`python replaylab/packaging/verify_portable_profile.py --application <portable-directory>`.
The native core resolves the same portable default during DLL initialization,
before GUI startup can set an environment override. The test uses a private
desktop and an OS job to close its own GUI/services and remove its temporary profiles.

Bread is the display brand. Existing `ReplayLab` executable names, protocol identifiers,
build commands, and storage paths below remain unchanged for compatibility.

AI connection configuration is an in-process native operation. The provider catalog is
compiled into the GUI from `replaylab/src/replaylab_agent/catalog.py` by
`util/ai/generate_provider_catalog.py`; translation synchronization regenerates it and
the release translation gate checks that it is current. Do not add a startup-time
AgentHost or network dependency for this catalog. The GUI shares the existing
`ReplayLab/ai/profiles.json` format and byte-range writer lock with AgentHost, writes
atomically, and uses the same Windows Credential Manager targets. Network model
discovery, subscription authentication, diagnostics and task execution remain in
AgentHost and cannot gate opening or saving the configuration form.

Shader visual previews require DXC with both DXIL and SPIR-V code generation.
Windows SDK builds can list `-spirv` in their help while omitting that backend.
`shader_dxc_manifest.json` pins Microsoft's official release archives and their
verified SHA-256 digests. Acquire into a new staging directory with:

```powershell
python replaylab/packaging/fetch_shader_dxc.py --output-directory C:\Temp\bread-dxc
```

Use `--archive` for an already downloaded official archive. Acquisition verifies
the archive hash, preserves licenses, and compiles real signed DXIL and SPIR-V
programs, including an exported entry-point name and binding shift. The release
package audit repeats these capability checks against its actual `dxc.exe`.
No command above modifies an existing application. After verification, stage
the Windows executable and both DLLs together under `replaylab/tools/dxc/bin` while the
application is stopped. Linux acquisition uses `--platform linux-x64`, preserves
the upstream `bin`/`lib` loader layout, and can be used through `PATH`; its tools
are executed in place. Unsupported hosts can supply compatible system tools on
`PATH`, subject to the same runtime compiler/dependency identity checks.
The Linux pin uses v1.8.2407 for Ubuntu 22.04 compatibility; the newer Windows
pin is v1.9.2609. Its corresponding Linux release requires GLIBC 2.38 and is
therefore not used for the older supported Linux host.

`New-BreadPackage.ps1` invokes `Deploy-ShaderCompiler.ps1` before its audit. This
acquires the pinned release into `%LOCALAPPDATA%/BreadCompilerCache`, verifies
its files, and stages only `dxc.exe`, `dxcompiler.dll`, `dxil.dll` in that private
`dxc/bin` directory, with licenses and the release receipt under
`replaylab/tools/dxc/licenses/<release>`. Visual debugging prefers this bundle
over a flat `replaylab/tools/dxc.exe`. Existing saved workspace compiler paths,
their adjacent DLLs, and pinned toolchain hashes remain unchanged. Both deployment
verification and the release package audit check the private bundle. An active
application or sidecar prevents compiler replacement. Use
an explicit deployment command for an existing Windows x86-64 application:

```powershell
./replaylab/packaging/Deploy-ShaderCompiler.ps1 `
    -ApplicationDirectory C:\Bread `
    -Archive C:\Downloads\dxc_2026_09_29.zip `
    -CacheDirectory C:\CompilerCache\dxc
```

`-Archive` supplies the pinned official archive offline; omit it to download
when the cache is empty. An existing verified `-CacheDirectory` also works
offline. `-VerifyOnly` checks the staged package without changing it. The
ordinary C++ build does not download or replace compilers.

For Linux release builds, opt in explicitly at configure time:

```sh
cmake -S . -B /absolute/build -DBREAD_BUNDLE_SHADER_COMPILERS=ON \
  -DCMAKE_INSTALL_PREFIX=/absolute/dist
cmake --build /absolute/build --target bundle-bread-shader-compilers
cmake --install /absolute/build --component bread-shader-compilers
```

The normal application build/install also includes that target/component when
enabled. The compiler target can run independently without building native
code. For an offline configure, supply these four optional parameters:

```sh
cmake -S . -B /absolute/build -DBREAD_BUNDLE_SHADER_COMPILERS=ON \
  -DBREAD_DXC_ARCHIVE=/absolute/linux_dxc_2024_07_31.x86_64.tar.gz \
  -DBREAD_GLSLANG_ARCHIVE=/absolute/glslang-tools_11.8.0+1.3.204.0-1_amd64.deb \
  -DBREAD_DXC_CACHE_DIRECTORY=/absolute/cache/dxc \
  -DBREAD_GLSLANG_CACHE_DIRECTORY=/absolute/cache/glslang
```

The archive parameters seed empty caches; existing verified caches work
without archives or network access. Cache parameters otherwise default to
XDG/user caches. `BREAD_BUNDLE_DXC` remains a compatibility alias that enables
both tools. Configure does not download anything. The explicit target verifies
every cached file, tests actual code generation, and stages both complete
bundles at `<build>/bin/replaylab/tools/` for build-tree launches. Installation keeps
`share/renderdoc/replaylab/tools/dxc/bin/dxc` and its adjacent `../lib` libraries,
plus `share/renderdoc/replaylab/tools/glslang/bin/glslangValidator` and all
licenses. A relocated package needs no workstation `PATH` compiler.
`make_package_linux.sh` verifies both installed bundles and refuses to release
an incomplete package. Developer builds keep bundling disabled by default and
do not acquire tools silently.

`shader_dxc_manifest.json` and `shader_glslang_manifest.json` are the version,
source URL and archive/file SHA-256 authorities; deployment preserves their
licenses and verification receipts. At runtime, preview cache identities also
include the selected compiler and its resolved dependencies, so a changed
executable or library invalidates cached compilation results.

The GLSL tool is pinned to Ubuntu jammy's official `glslang-tools` package
`11.8.0+1.3.204.0-1`; the archive and individual files are SHA-256 checked.
Only the compiler and full copyright/license notice are extracted. Acquisition
uses `dpkg-deb` to read its zstd payload; staging an existing verified cache and
running an installed application do not require the package manager. Its
glslang/SPIRV compiler and optimizer code is statically linked. The host must
provide GLIBC 2.34 or newer and GLIBCXX 3.4.29 or newer, including the usual
`libstdc++`, `libm`, `libc` and transitive `libgcc_s` runtime libraries. No system
compiler or library is copied into the package. Capability checks run with an
empty `PATH` and exercise macro/include preprocessing, optimized Vulkan GLSL
and OpenGL SPIR-V, including exported entry-point preservation.

The bundled tools currently target Linux x86-64 hosts and are tested on Ubuntu
22.04. Other host CPU architectures and cross builds are rejected explicitly;
Android remote replay uses the host's compiler bundle, not an Android compiler
executable. These compiler checks do not claim Linux/Android GPU replay coverage.

`Build-ReplayLabRuntime.ps1` produces four Python 3.12 executables.

The portable build entry point passes `-PortableHosts` and stages the three
Agent, Internal and MCP launchers beside one verified `bread-host-support/`
directory under `replaylab/output/portable-runtime`. Their application-relative
names are unchanged. The release manifest pins the support files as well as
the four executables. Shared files must match byte for byte, except equivalent
standard library ZIPs whose member contents match. Embedded first-party code
is verified against the current checkout before a runtime is staged.

For Development deployment, invoke the freezer without `-PortableHosts` to
produce the existing onefile runtime under `replaylab/output/replaylab`.
Portable Host files are installed together by the release assembler and kept
separate from the Development deployment input.

The four entry points are:

- `replaylab-ai.exe` — qrenderdoc's AgentHost with deepagents model-API execution,
  vendor-native ACP/Codex subscription execution, profile/credential handling,
  bundled RenderDoc Skills, and task-event projection
- `ReplayLabInternal.exe` — qrenderdoc-owned, bearer-protected loopback service
  exposing only bounded ProjectService and project-restore HTTP operations
- `BreadMCP.exe` — first-party MCP Host used through a filtered task-level
  stdio route by deepagents children and vendor-native ACP/Codex tasks, and by
  explicit external Codex/IDE/automation clients.
- `ReplayLabShaderTools.exe` — independent fixed shader adapters, reused unchanged
  when their build inputs are unchanged.

The AgentHost and MCP onefile builds both embed the audited
`src/unpacked_asset_jobs.py` source as the fixed
`replaylab_helpers/unpacked_asset_jobs.py` data helper. This is intentionally a
source data file: an independently configured Python provider must be able to
execute the Host-managed Blender continuation driver after PyInstaller extracts
either onefile runtime. This covers AgentHost-managed continuations and stdio
MCP execution. Runtime resolution is confined to the
absolute `_MEIPASS` root and the selected helper is SHA-256-bound into
continuation receipts.

The default output is `replaylab/output/replaylab/`. The qrenderdoc build owns
deployment of the embedded Python bridge, so this runtime builder does not
create an extension manifest or copy a plugin directory.

The three Hosts freeze concurrently, with separate persistent
`<WorkDirectory>/pyinstaller-cache/<host>` caches. Per-host OS locks serialize
another build using the same cache. All jobs must succeed before smoke tests
or output publication; logs include individual and wall-clock freeze times.
The independent Shader runner keeps its content-addressed cache and checks.

For development-only runtime builds, prepare one 64-bit Python environment with
both projects' build dependencies as below. Release packaging uses the sealed,
hash-locked environment from `Restore-BuildEnvironment.ps1` instead of resolving
these editable-install dependencies anew.

```powershell
$python = "D:\path\to\python.exe"
& $python -m pip install -e ".\replaylab[all]" -e ".\replaylab\mcp_host[bundle]"
./replaylab/packaging/Build-ReplayLabRuntime.ps1 -PythonExecutable $python
```

The build probe and frozen self-test require the installed DeepAgents version
to match the exact pin in `replaylab/pyproject.toml`. The AgentHost embeds that
dependency manifest, and each successful output records the verified framework
version alongside the runtime files' SHA-256 hashes.
The frozen AgentHost also runs `-I -m replaylab_agent.selftest --acp-chat-smoke`:
an account-free ACP handshake, model probe and two-turn streaming chat against an
independent local stdio peer. CodeBuddy extension notifications and unsupported
requests are interleaved with real SDK messages; a failed check stops packaging.
This does not test a vendor's subscription account, model availability or quota.
The Windows build also starts each frozen Host through its real `ensure` command
in an isolated runtime directory. After the launcher exits, its detached service
must retain its own readable Python archive and stop normally. Long-lived frozen
children use `PYINSTALLER_RESET_ENVIRONMENT=1` so the launcher's onefile cleanup
cannot delete files needed by later imports, HTTP requests or editor saves.
Rebuilds replace only the five marker-owned files; an unmarked,
non-empty output directory is rejected and unrelated entries are never
recursively deleted.

Deploy a verified runtime into an existing qrenderdoc application layout with
the explicit deployment command:

```powershell
./replaylab/packaging/Deploy-ReplayLabRuntime.ps1 `
    -ApplicationDirectory ./x64/Development
```

The Visual Studio project exposes the same explicit step after building the runtime:

```powershell
$solutionDir = (Resolve-Path .).Path.TrimEnd('\') + '\'
MSBuild qrenderdoc/qrenderdoc_local.vcxproj /m:2 /t:DeployReplayLabRuntime `
    /p:Configuration=Development /p:Platform=x64 "/p:SolutionDir=$solutionDir"
```

CMake builds expose `deploy-replaylab-runtime`. Neither target is part of the ordinary incremental
`Build`; deployment must happen while qrenderdoc and both sidecars are stopped.

The command reads `replaylab/output/replaylab/` by default and always targets
only `<ApplicationDirectory>/replaylab/`. The application directory must contain an
recognized application executable (`RenderDocBread.exe`, with legacy executable
names accepted for compatibility). The source manifest schema, version, exact
owned filenames, sizes, and SHA-256 hashes are validated before any target
mutation; source and target paths may not contain reparse points.

An empty target is marked on first deployment. A non-empty target without that
deployment marker is rejected. To adopt a legacy manual deployment, use
`-AdoptExistingVerifiedRuntime`; adoption succeeds only when both canonical
sidecars, AgentHost, and the build manifest already match the verified runtime output
byte-for-byte (size and SHA-256). Other files are preserved. A mismatched legacy
directory still fails closed and must be reviewed manually.

Later deployments replace only the four runtime executables, build manifest, and
deployment marker. Any unrelated entries are preserved and are never
recursively removed. The marker enters an `incomplete` state only after every
new file has been staged and verified, so an interrupted commit is explicitly
owned and retryable; it changes to `complete` after final hash verification. If
a runtime file is active or locked, deployment fails with a stop-and-retry
message and does not terminate any process. Runtime deployment remains
deliberately separate from the C++ build so rebuilding qrenderdoc cannot
overwrite a sidecar serving an active analysis session. The private and MCP
hosts use different state files, bearer tokens, ports, protocols, and routes;
neither lifecycle command can adopt or release the other's owners.

`ReplayLabShaderTools.exe` is an independent fixed adapter runner. It contains
only the statically resolved adapter import closure, without importing the MCP
Host, server, or workspace package initialization. Its six `adapter-*` commands
preserve the existing fixed argument contracts. The builder smoke-tests every
command with `--help` before using the packaged Hosts.

The runner cache lives at `<WorkDirectory>/shader-tools-cache/`. Its key hashes
the entry point, adapter sources and transitive local imports, the independent
build implementation, Python executable/DLLs/stdlib/site startup scripts, and
the installed PyInstaller implementation, bootloader, dependencies and hook
providers. Host/Agent/report source changes do not enter that key. Cache hits
verify the complete runner's size and SHA-256 and copy exactly those bytes;
actual input changes rebuild it. Tampered or incomplete cache records fail
closed. A bounded process lock prevents concurrent cache generation, and input
hashes are checked again before committing a newly built result.

The runtime manifest includes all four executables. A deployment marker with
the exact former three-executable ownership list can add an absent shader
runner during an ordinary verified deployment. An existing unowned runner is
rejected. Compiler/adapter binaries and fixed arguments remain hash-bound by
the workspace tool registry; this split does not relax tool integrity checks.

The fast checks do not run PyInstaller:

```powershell
./replaylab/packaging/tests/Test-ReplayLabPackaging.ps1
./replaylab/packaging/tests/Test-ReplayLabRuntimeDeployment.ps1
```

### Portable plugin dependencies

`prepare_capabilities_bundle.py` imports an explicitly selected desktop plugin
code tree and its dependency tree into an application-owned snapshot. It never
imports login state, account configuration, captures, conversations or live MCP
pipe settings. `--provider-runtime` includes dedicated clients; they activate
only with a live advertised provider service. Bread's document, browser and
Windows adapters use a shared MCP interface on all connection types.

Build public releases from a clean checkout of the release commit. To ship a new
Bread workflow, commit its complete directory under `.agents/skills`; runtime
freezing and portable assembly discover it automatically. The skill loader validates
the name, frontmatter and declared references. Category is optional, and instructions
should describe how to verify results in the language of the workflow. Register new
MCP tools through the existing Host entry points and declare any packaged dependencies
in the release inputs.

Keep personal skills in the local user configuration outside the checkout.
`--custom-skill NAME` selects a desktop skill for a private snapshot, and
`--bread-skills` preserves local overrides for private use. Neither option
approves content for a public release. Public verification accepts the existing
approved compatibility routes and vendor system skills under `custom-skills` and
rejects user override snapshots. It verifies physical and logical inventories,
including the contents of nested capsules. Required Node/Python SDKs belong in
their dedicated runtime components, where their dependencies are preserved.
Claude task workspaces copy instructions/references/scripts with Windows extended
paths, prune dependency/cache directories before scanning, and reject retained
links or reparse points.

Package audits and capability verification share process-local parallel SHA-256
results keyed by file identity, size, modification time and change time (NTFS
ChangeTime on Windows). Changed files are rehashed after smoke tests; final ZIP
members still receive independent byte verification. Both full and compact ZIP
writers store compressed formats and the four known large frozen Hosts verbatim.
Files sampled within the two-second timestamp window are rehashed until a
settled sample is available, covering rapid writes that share a clock tick.
Expanded-tree receipts likewise require settled file identities before reuse.

Dependency expansion uses 20-hex directory names for new generations and discovers
existing full-hash directories without moving them. A sibling atomic receipt binds
the full archive hash, locked inventory and every file's metadata; unchanged normal
restores avoid rereading contents. Source archives are always hash-verified.
`restore_dependencies.py --full-verify` and release capability selection hash the
complete expanded inventory. `--verify-only` also fully checks any selected trees
already present under `--output`. Receipts never replace release verification.

The standard package requires a verified capabilities snapshot. The default
entry point restores/selects it from sealed inputs; `--capabilities` (PowerShell:
`-CapabilitiesDirectory`) explicitly selects another verified snapshot.
An existing sealed snapshot can be rebuilt/verified without the original desktop
installation. Keep original license/notice files and plugin license metadata.
The pinned LibreOffice administrative image is 26.8.2, SHA-256
`ff4b911403b67ee9e6249f820f9782c44bb32c94fdaa45b0da53a8e419e5307a`.
The optional extended profile can pair Chromium with Playwright. The standard
profile keeps the SDK and uses an installed browser only when explicitly needed.

Settings connection checks use `doctor --execution-test`: one actual model
turn/tool roundtrip and application-owned document smoke files in the OS temp
directory. The test endpoint cannot edit captures/projects or Unity. Offline
`doctor` remains available to automation without consuming model quota.

Retained Python, Node, native, provider and custom-skill runtimes are shipped
in five `bundles/*.zip` LZMA capsules. The outer ZIP stores those capsules
without recompressing them; all dependency files and licenses remain bundled.
Plugin roots are shortened to `p/<plugin-id>` through manifest-relative paths.
This keeps deep npm trees out of Explorer's initial extraction path and reduces
the initial extracted footprint. It does not reduce the size of the full set of
expanded runtimes: disk usage grows as tools are first used, and the compressed
copies remain available for offline repair.

First use expands only the required component in the application's own tools
directory. Use a writable portable installation directory. An OS-owned lock
coordinates concurrent requests; SHA-256 checks and a private staging directory
precede atomic publication. Unchanged components reuse their ready marker on
later calls. Failed expansion preserves the old runtime, and Windows expansion
uses extended-length paths. Full older dependency snapshots remain supported.

For a GUI/host update that preserves the native core, injection libraries and
already sealed dependency inventory,
`build_compact_release.py` creates a fresh archive from a verified base, a
compressed capabilities snapshot, the current frozen runtime and newly built
GUI. It rejects changed dependency content, verifies every released file and
removes superseded ZIP payloads. Its output is a complete portable distribution.

Connection diagnostics use `doctor --jsonl --execution-test`: each completed
check is streamed immediately, with running rows before slow execution probes.
Closing the native diagnostics dialog cancels its owned process tree. Local
tool health, credential checks and real model/MCP execution remain distinct;
a provider-side 403/502 is not reported as a successful connection.
## Clean checkout and dependency inputs

The Windows x64 package can restore its third-party inputs from
`replaylab/dependencies/inputs.lock.json` and the ordinary Git binary parts in
`replaylab/dependencies/archives`. Each part is at most 48 MiB, below GitHub's
regular-file warning threshold. Clone normally; no LFS setup or separate Release
download is needed. `Restore-BuildEnvironment.ps1` automatically reconstructs
missing ZIPs, checks every part and archive SHA-256 plus the exact inventory,
then restores the locked offline environment. Reconstructed ZIPs and expanded
inputs stay ignored; the source parts are tracked and unchanged.
To assemble and verify inputs without creating an environment, run
`python replaylab/packaging/restore_dependencies.py --verify-only`.
When deliberately updating sealed inputs, update the locked archive inventory,
run `python replaylab/packaging/seal_dependency_parts.py`, and commit the new
parts together with `inputs.lock.json`. Public Releases contain only the complete
user application archive and its checksum, never separate build-input archives.
See [the dependency and redistribution checklist](../dependencies/README.md)
and [machine inventory](../dependencies/inventory.json).

Install 64-bit CPython 3.12, Visual Studio 2022 with the C++ workload and
Windows SDK, and x64 .NET SDK 10.0.302. Restore with:

```powershell
./replaylab/packaging/Restore-BuildEnvironment.ps1
./replaylab/packaging/Build-RenderDocBread.ps1
```

Restoration verifies the archived inputs and creates a separate environment
from local hash-locked wheels. It does not use an existing `.venv`, Codex
plugin installation, account or `x64/Development` application. The normal
package entry runs restoration itself, builds project references and matching
Python bindings, and stages compilers, language service, Unity provider and
the complete standard skill/runtime set. UEX restores from the local NuGet
feed with checked-in lock files and publishes self-contained. Offline UEX
restore disables the network vulnerability lookup; it does not certify the
dependencies free of advisories. The inventory checklist records known
upstream issues separately.

`-PythonExecutable` selects the CPython 3.12 bootstrap interpreter;
`-BuildInputRoot` selects disposable expanded inputs and environment storage.
They are build options, never recipient-machine dependencies. The canonical
archives remain read-only. Keep at least 20 GB free for a native build and
staging, in addition to Git and sealed-archive storage. Native artifacts still use
`output/RenderDocBread-native`; use a separate checkout on a larger volume
when the source volume is full.

`-SkipNativeBuild` requires an explicit read-only `-ApplicationDirectory`.
It is an incremental packaging option, not the clean-build path. No command
above deploys to or terminates a running application.

## Windows build compatibility

Build filesystem operations use explicit Windows extended-length paths for dependency
extraction, inventory verification, source copying and staging cleanup. The build does
not require changing LongPathsEnabled or the user registry. Enumeration errors stop
restoration and preserve the existing generation instead of treating unreadable data
as missing files. Temporary cleanup retries read-only or busy files; if cleanup still
fails, it reports the exact retained directory without hiding the original build result.

UEX uses the checked-in dotnet-sdk.global.json to select SDK 10.0.302 and its apphost
10.0.10, matching the sealed runtime packages. Newer feature bands alone are not an
offline replacement. Install this x64 SDK alongside existing SDKs, or pass
-DotNetExecutable to an existing private installation. Missing prerequisites are reported
before native compilation. An explicitly supplied -UexPublishDirectory needs no UEX SDK
build. UEX source inputs are copied to an owned build directory before pinning SDK
settings; user checkouts and sealed input generations are preserved.
