[CmdletBinding()]
param(
    [ValidateSet("x64", "Win32")]
    [string]$Platform = "x64",

    [ValidateSet("Release", "Development")]
    [string]$Configuration = "Release",

    [string]$OutputDirectory = (Join-Path $PSScriptRoot "..\..\dist\stable"),

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$buildDir = Join-Path $repo (Join-Path $Platform $Configuration)
$required = @(
    "qTinecmaTool.exe",
    "TinecmaToolcmd.exe",
    "TinecmaTool.dll",
    "TinecmaTool.json"
)
foreach ($name in $required) {
    $path = Join-Path $buildDir $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required build output is missing: $path"
    }
}

$forbiddenOutputPattern = '^(?:q?TinecmaTools(?:cmd|shim(?:32|64)?|ui)?|renderdoc(?:cmd|shim(?:32|64)?|ui)?)\.(?:dll|exe|json|lib|exp|ilk|pdb)$'
$forbiddenOutputs = @(Get-ChildItem -LiteralPath $buildDir -File | Where-Object {
    $_.Name -match $forbiddenOutputPattern
})
if ($forbiddenOutputs.Count -gt 0) {
    $paths = ($forbiddenOutputs | ForEach-Object FullName) -join [Environment]::NewLine
    throw "Legacy or mixed-brand build outputs were found. Run clean_and_rebuild.bat and rebuild before packaging:`n$paths"
}

$versionText = (& (Join-Path $buildDir "TinecmaToolcmd.exe") version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read the TinecmaTool version from $buildDir"
}
$version = if ($versionText -match "v([0-9]+(?:\.[0-9]+)+)") { $Matches[1] } else { "unknown" }
$commit = (git -C $repo rev-parse HEAD).Trim()
$shortCommit = $commit.Substring(0, [Math]::Min(12, $commit.Length))
$arch = if ($Platform -eq "x64") { "win-x64" } else { "win-x86" }
$packageName = "TinecmaTool-$version-$arch-$shortCommit"

$outputDir = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$stage = Join-Path $outputDir $packageName
$zip = Join-Path $outputDir "$packageName.zip"
$zipHash = "$zip.sha256"

if ((Test-Path -LiteralPath $stage) -or (Test-Path -LiteralPath $zip)) {
    if (-not $Force) {
        throw "Package output already exists. Re-run with -Force to replace: $packageName"
    }
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
    if (Test-Path -LiteralPath $zipHash) { Remove-Item -LiteralPath $zipHash -Force }
}

New-Item -ItemType Directory -Path $stage | Out-Null
$excludedDirectories = @("obj", "pymodules")
$excludedExtensions = @(".exp", ".ilk", ".iobj", ".ipdb", ".lib", ".pdb")
Get-ChildItem -LiteralPath $buildDir -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($buildDir.Length).TrimStart('\')
    $parts = $relative -split '[\\/]'
    @($parts | Where-Object { $excludedDirectories -contains $_ }).Count -eq 0 -and
        $excludedExtensions -notcontains $_.Extension.ToLowerInvariant()
} | ForEach-Object {
    $relative = $_.FullName.Substring($buildDir.Length).TrimStart('\')
    $destination = Join-Path $stage $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $destination
}

Copy-Item -LiteralPath (Join-Path $repo "LICENSE.md") -Destination $stage
Copy-Item -LiteralPath (Join-Path $repo "docs\DF_AUTHORIZED_CAPTURE_RESEARCH.md") -Destination $stage

$manifestFiles = @(Get-ChildItem -LiteralPath $stage -Recurse -File | Sort-Object FullName | ForEach-Object {
    [ordered]@{
        path = $_.FullName.Substring($stage.Length).TrimStart('\').Replace('\', '/')
        bytes = $_.Length
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
})
$manifest = [ordered]@{
    schemaVersion = 1
    product = "TinecmaTool"
    version = $version
    platform = $arch
    configuration = $Configuration
    commit = $commit
    createdAt = (Get-Date).ToUniversalTime().ToString("o")
    files = $manifestFiles
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $stage "manifest.json") -Encoding UTF8

Compress-Archive -LiteralPath $stage -DestinationPath $zip -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $([IO.Path]::GetFileName($zip))" | Set-Content -LiteralPath $zipHash -Encoding ASCII

Write-Host "Package: $zip"
Write-Host "SHA256: $hash"
