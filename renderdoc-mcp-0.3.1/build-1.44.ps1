# build-1.44.ps1 - build renderdoc-mcp against a locally-built RenderDoc 1.44 tree.
#
# Why this script exists:
#   * The released renderdoc-mcp v0.3.1 zip bundles a RenderDoc v1.43 runtime.
#     RenderDoc changed struct layout between 1.43 and 1.44 (EventUsage lost its
#     `view` member), so you MUST rebuild against the 1.44 headers to match the
#     1.44 renderdoc.dll you are running against. Never mix.
#   * The VS / MSBuild generator fails inside the WorkBuddy agent session because
#     the inherited environment block contains BOTH `Path` and `PATH`, which makes
#     .NET's ProcessStartInfo throw. The NMake Makefiles generator avoids MSBuild
#     entirely, so this script uses NMake and sets the toolchain env by hand.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File build-1.44.ps1
#   powershell -ExecutionPolicy Bypass -File build-1.44.ps1 -Clean
#
# Run it from a NORMAL terminal (not the agent sandbox) for real work.

param(
    [string] $RenderDocDir = 'E:\GraphicsDebugger',
    [string] $BuildDir     = '',
    [string] $Config       = 'Release',
    [switch] $Clean
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
if (-not $BuildDir) { $BuildDir = Join-Path $repo 'build-144' }

# ---------------------------------------------------------------- 1. checks
if (-not (Test-Path (Join-Path $RenderDocDir 'renderdoc\api\replay\renderdoc_replay.h'))) {
    throw "RENDERDOC_DIR does not look like a RenderDoc source tree: $RenderDocDir"
}

$versionHeader = Join-Path $RenderDocDir 'renderdoc\api\replay\version.h'
$verLine = Select-String -Path $versionHeader -Pattern 'RENDERDOC_VERSION_MINOR\s+(\d+)' |
           Select-Object -First 1
$minor = if ($verLine) { $verLine.Matches[0].Groups[1].Value } else { '?' }
Write-Host "RenderDoc source : $RenderDocDir (v1.$minor)"

$rdLib = Join-Path $RenderDocDir 'x64\Development\renderdoc.lib'
if (-not (Test-Path $rdLib)) {
    $rdLib = Join-Path $RenderDocDir 'x64\Release\renderdoc.lib'
}
if (-not (Test-Path $rdLib)) {
    throw "renderdoc.lib not found. Build RenderDoc (x64) first."
}
Write-Host "renderdoc.lib    : $rdLib"

# ------------------------------------------------------- 2. MSVC toolchain
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vsPath = $null
if (Test-Path $vswhere) {
    $vsPath = & $vswhere -latest -products * `
                -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
                -property installationPath 2>$null | Select-Object -First 1
}
if (-not $vsPath) { throw 'Visual Studio with the C++ workload was not found.' }

$msvcRoot = Join-Path $vsPath 'VC\Tools\MSVC'
$msvc = Get-ChildItem $msvcRoot -Directory |
        Sort-Object { [version]($_.Name -replace '_', '.') } -Descending |
        Select-Object -First 1
if (-not $msvc) { throw "No MSVC toolset under $msvcRoot" }

$sdkRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10'
$sdkVer = Get-ChildItem (Join-Path $sdkRoot 'Include') -Directory |
          Sort-Object Name -Descending | Select-Object -First 1
if (-not $sdkVer) { throw "No Windows 10/11 SDK under $sdkRoot\Include" }

Write-Host "MSVC             : $($msvc.FullName)"
Write-Host "Windows SDK      : $($sdkVer.Name)"

# ------------------------------------------------------------- 3. build env
# Note: assign via $env: so this only affects this process and its children.
$env:Path  = "$($msvc.FullName)\bin\Hostx64\x64;$sdkRoot\bin\$($sdkVer.Name)\x64;$env:Path"
$env:INCLUDE = @(
    "$($msvc.FullName)\include",
    "$sdkRoot\Include\$($sdkVer.Name)\ucrt",
    "$sdkRoot\Include\$($sdkVer.Name)\um",
    "$sdkRoot\Include\$($sdkVer.Name)\shared",
    "$sdkRoot\Include\$($sdkVer.Name)\winrt",
    "$sdkRoot\Include\$($sdkVer.Name)\cppwinrt"
) -join ';'
$env:LIB = @(
    "$($msvc.FullName)\lib\x64",
    "$sdkRoot\Lib\$($sdkVer.Name)\ucrt\x64",
    "$sdkRoot\Lib\$($sdkVer.Name)\um\x64"
) -join ';'
$env:VCToolsInstallDir = "$($msvc.FullName)\"

$rdBuildDir = Split-Path -Parent (Split-Path -Parent $rdLib)   # ...\x64

# -------------------------------------------------------------- 4. cmake
if ($Clean -and (Test-Path $BuildDir)) {
    Write-Host "Removing $BuildDir"
    Remove-Item -Recurse -Force $BuildDir
}

Write-Host "`n=== configure ==="
& cmake -S $repo -B $BuildDir -G 'NMake Makefiles' `
        "-DCMAKE_BUILD_TYPE=$Config" `
        "-DRENDERDOC_DIR=$RenderDocDir" `
        "-DRENDERDOC_BUILD_DIR=$rdBuildDir"
if ($LASTEXITCODE -ne 0) { throw "cmake configure failed ($LASTEXITCODE)" }

Write-Host "`n=== build ==="
& cmake --build $BuildDir
if ($LASTEXITCODE -ne 0) { throw "cmake build failed ($LASTEXITCODE)" }

# ------------------------------------------------------------- 5. verify
Write-Host "`n=== verify ==="
$expected = @('renderdoc-mcp.exe', 'renderdoc-cli.exe', 'renderdoc.dll', 'renderdoc.json')
$missing = @()
foreach ($f in $expected) {
    $p = Join-Path $BuildDir $f
    if (Test-Path $p) {
        Write-Host ("  OK    {0,-20} {1,12:N0} bytes" -f $f, (Get-Item $p).Length)
    } else {
        Write-Host ("  MISS  {0}" -f $f)
        $missing += $f
    }
}
if ($missing.Count -gt 0) {
    Write-Warning "Missing outputs: $($missing -join ', ')"
}

# renderdoc.dll must match the tree it was linked against
$srcDll = Join-Path $rdBuildDir 'Development\renderdoc.dll'
if (-not (Test-Path $srcDll)) { $srcDll = Join-Path $rdBuildDir 'Release\renderdoc.dll' }
if (Test-Path $srcDll) {
    $a = (Get-FileHash $srcDll -Algorithm SHA256).Hash
    $b = (Get-FileHash (Join-Path $BuildDir 'renderdoc.dll') -Algorithm SHA256).Hash
    if ($a -eq $b) { Write-Host '  OK    renderdoc.dll matches the source tree build' }
    else           { Write-Warning 'renderdoc.dll in the output dir differs from the source tree build' }
}

Write-Host @"

Done. Output dir: $BuildDir

Next steps
  1. Point your MCP client at:
       $BuildDir\renderdoc-mcp.exe
  2. Offline .rdc analysis works as-is. LIVE capture does NOT:
     src/core/capture.cpp hardcodes ENABLE_VULKAN_RENDERDOC_CAPTURE and copies
     renderdoc.json (layer VK_LAYER_RENDERDOC_Capture). If this machine disables
     that layer name via VK_LOADER_LAYERS_DISABLE and uses a rebranded layer
     (TinecmaTool) instead, capture_frame / 'renderdoc-cli capture' will silently
     capture nothing. Patch the env name + manifest, or use a non-rebranded build.
  3. Known-cosmetic: every RenderDoc process may exit with 0xC0000409 inside an
     agent sandbox (faulting module is the sandbox's injected DLL). The official
     RenderDoc 1.43 renderdoccmd.exe shows the same behaviour, so it is not a
     1.44 incompatibility. Run from a normal shell for clean CI.
"@
