[CmdletBinding()]
param(
    [ValidateSet("x64", "x86", "Win32")]
    [string]$Platform = "x64",

    [ValidateSet("Release", "Development")]
    [string]$Configuration = "Development"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$platformDirectory = if ($Platform -eq "x86") { "Win32" } else { $Platform }
$buildDirectory = Join-Path $repo (Join-Path $platformDirectory $Configuration)

function Require-Text {
    param([string]$Path, [string]$Pattern, [string]$Description)

    $fullPath = Join-Path $repo $Path
    if (-not (Select-String -LiteralPath $fullPath -Pattern $Pattern -Quiet)) {
        throw "Build identity check failed: $Description ($fullPath)"
    }
}

Require-Text "renderdoc\api\replay\renderdoc_replay.h" 'TinecmaTool__replay__marker' "Replay marker export is missing"
Require-Text "renderdoc\os\win32\win32_libentry.cpp" 'LibraryHooks::Detect\("TinecmaTool__replay__marker"\)' "Replay marker detection does not match"
Require-Text "renderdocshim\renderdocshim.cpp" 'GetProcAddress\(mod, "INTERNAL_SetCaptureFile"\)' "Shim capture-file export does not match the core DLL"
Require-Text "renderdoc\replay\app_api.cpp" 'RENDERDOC_GetAPI' "Standard public API entry point is missing"
Require-Text "renderdoc\replay\app_api.cpp" 'TINECMATOOL_GetAPI' "Branded public API compatibility alias is missing"

if (-not (Test-Path -LiteralPath $buildDirectory -PathType Container)) {
    Write-Host "Source identity checks passed; build directory does not exist: $buildDirectory"
    exit 0
}

$requiredOutputs = @(
    "qTinecmaTool.exe",
    "TinecmaToolcmd.exe",
    "TinecmaTool.dll",
    "TinecmaTool.json"
)
foreach ($name in $requiredOutputs) {
    $path = Join-Path $buildDirectory $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required branded build output is missing: $path"
    }
}

$forbiddenOutputPattern = '^(?:q?TinecmaTools(?:cmd|shim(?:32|64)?|ui)?|renderdoc(?:cmd|shim(?:32|64)?|ui)?)\.(?:dll|exe|json|lib|exp|ilk|pdb)$'
$forbiddenOutputs = @(Get-ChildItem -LiteralPath $buildDirectory -File | Where-Object {
    $_.Name -match $forbiddenOutputPattern
})
if ($forbiddenOutputs.Count -gt 0) {
    $paths = ($forbiddenOutputs | ForEach-Object FullName) -join [Environment]::NewLine
    throw "Legacy or mixed-brand outputs are present:`n$paths"
}

$manifest = Get-Content -LiteralPath (Join-Path $buildDirectory "TinecmaTool.json") -Raw | ConvertFrom-Json
if ($manifest.layer.name -ne "VK_LAYER_TINECMATOOL_Capture") {
    throw "Unexpected Vulkan layer name: $($manifest.layer.name)"
}
if ($manifest.layer.library_path -notmatch 'TinecmaTool\.dll$') {
    throw "Unexpected Vulkan layer library path: $($manifest.layer.library_path)"
}

Write-Host "Build identity checks passed: $buildDirectory"
