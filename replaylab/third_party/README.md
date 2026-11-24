# Bundled third-party tools

Unity project integration uses [CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp)
under MIT, pinned to v10.3.0 / `aa5fc638d623f56178d50329d9ad8c541a57fe66`.
`mcp_host/src/unity_integration.py` records and verifies the source archive SHA-256
and uses the upstream `uv.lock` for its isolated server runtime. It is downloaded
on the first requested Unity connection, then reused from the per-user cache.
The first-party UPM helpers under `unity/` are included in the packaged runtime
as `bread_unity`; installing them does not require users to configure a separate
MCP server. The application installer itself does not need to open a Unity project.
An already running RenderDoc is never restarted by Unity setup.

`unpackers/` holds the four asset unpackers Bread AI ships with:

| kind | program | engine |
|---|---|---|
| `uex` | `uex.exe` (+ `profiles.json`) built from github.com/Straw1997/uex (CUE4Parse) | Unreal 4 / 5 |
| `assetstudio` | `AssetStudioModCLI.exe` from github.com/aelurum/AssetStudio releases | Unity 1.7 – 6000.x |
| `umodel` | `umodel_64.exe` / `umodel.exe` from gildor.org | Unreal 1 – 4 (legacy) |
| `unitypy` | embedded in the AgentHost (`replaylab-ai unpacker unitypy`) — no files here | Unity |

`unpackers\Fetch-Unpackers.cmd` downloads/builds them (each download prints its
URL and SHA-256, versions land in `unpackers\manifest.json`) and mirrors the
result into `x64\Development\replaylab\tools\unpackers\`.
After that one fetch, nothing here is a manual step. Building qrenderdoc mirrors
the bundle into the build output itself: `qrenderdoc_local.vcxproj` target
`DeployBundledUnpackers` (after `DeployReplayLabRuntime`, incremental against
`<out>\replaylab\tools\unpackers\deployed.json`) runs
`packaging\Deploy-BundledTools.ps1`. Everything downstream therefore carries the
tools without knowing about them: the official zip and the MSI are both made from
the build output (`make_package_win32.sh` copies `x64\Release` into `dist\Release64`,
and `heat` harvests `dist\Release64\replaylab` into the installer), and so is a
folder someone copies by hand. `REBUILD_HOST_AND_BRIDGE.cmd` runs the same deploy
because it refreshes the runtime without building qrenderdoc, and
`output\shader-workspace-20260906\PACKAGE_SHARE.cmd` only audits the built
application (tools present, no absolute path in any shipped file) and zips it.

At run time the tools are addressed as `%BREAD_TOOLS%\unpackers\<kind>\...`:
`%BREAD_TOOLS%` is `<app>\replaylab\tools` for the frozen runtime and
`replaylab\third_party` for a source checkout. The AgentHost registers what it
finds there on every start (`source: bundled`), never displacing a program the
user registered by hand, so a copied application folder works without setup.
There is nothing to configure per machine. An unpacked tree is loose files, so
uex mounts the *directory*: no paks, no AES keys, no `.usmap`. Bread AI writes
uex's profile itself (which directory to mount and which engine version) into
`%LOCALAPPDATA%\ReplayLab\tools\uex\profiles.json` the first time a uex recipe
runs, and pins the version once `uex discover` proves one.

`unpackers\uex\profiles.json` is only for the other, optional job -- mounting a
game's **original** `.pak`/`.utoc`, which does need a version and AES keys. Add
one with `Fetch-Unpackers.cmd -ProfileOnly -PaksDir "D:\...\Content\Paks"`
(no downloads), or in the built-in unpacker dialog in Settings › AI.

Building uex: the source tree lives in `unpackers\_src\uex` and the build is
published into `<source>\publish`, then copied here. The output must never be a
parent of the source -- MSBuild then excludes the project's own `.cs` files and
the build fails with `CS5001: no static Main`. `-UexSource <checkout>` builds
from a clone you already have; `-UexExe <...\publish\uex.exe>` skips building and
copies that publish folder.
