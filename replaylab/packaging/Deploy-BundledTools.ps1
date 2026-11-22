# Mirror the bundled third-party tools into a deployed application.
#
#   replaylab\third_party\unpackers\<kind>\   ->   <ApplicationDirectory>\replaylab\tools\unpackers\<kind>\
#
# The runtime resolves %BREAD_TOOLS% to <app>\replaylab\tools when frozen, so a
# deployed (or copied) application finds the unpackers next to itself. Source
# trees used for building (uex\src) are not shipped. Runs after
# Deploy-ReplayLabRuntime.ps1; safe to run again at any time.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ApplicationDirectory,
    [string]$BundleRoot = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $BundleRoot) { $BundleRoot = Join-Path $repoRoot "replaylab\third_party\unpackers" }
$runtime = Join-Path $ApplicationDirectory "replaylab"
if (-not (Test-Path -LiteralPath $runtime)) {
    throw "no deployed runtime at $runtime; run Deploy-ReplayLabRuntime.ps1 first"
}
if (-not (Test-Path -LiteralPath $BundleRoot)) {
    Write-Host "no bundle at $BundleRoot (run replaylab\third_party\unpackers\Fetch-Unpackers.cmd first); nothing to deploy"
    exit 0
}
# _src holds checkouts used to build; it is never shipped.
$kinds = Get-ChildItem -LiteralPath $BundleRoot -Directory |
    Where-Object { $_.Name -ne "_src" -and -not $_.Name.StartsWith(".") }
if (-not $kinds) {
    Write-Host "Unpacker deployment requires staged tool payloads at $BundleRoot."
    exit 0
}
$target = Join-Path $runtime "tools\unpackers"
New-Item -ItemType Directory -Force -Path $target | Out-Null
foreach ($kind in $kinds) {
    $destination = Join-Path $target $kind.Name
    $targetPrefix = [IO.Path]::GetFullPath($target).TrimEnd('\') + '\'
    if (-not ([IO.Path]::GetFullPath($destination)).StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        ((Get-Item -LiteralPath $kind.FullName).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Bundled tool must be an ordinary child directory: $destination"
    }
    $arguments = @($kind.FullName, $destination, "/MIR", "/NJH", "/NJS", "/NDL", "/NFL", "/NP", "/R:2", "/W:1", "/XD", "src", "obj", "bin", ".git", "/XF", "*.pdb")
    & robocopy @arguments | Out-Null
    # robocopy: 0-7 are success codes (files copied / extras removed), 8+ are failures.
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $($kind.Name) (exit $LASTEXITCODE)" }
    $count = (Get-ChildItem -LiteralPath $destination -Recurse -File | Measure-Object).Count
    Write-Host ("  {0,-12} {1,6} files -> {2}" -f $kind.Name, $count, $destination)
}
foreach ($file in @("manifest.json")) {
    $source = Join-Path $BundleRoot $file
    if (Test-Path -LiteralPath $source) {
        $manifest = Get-Content -LiteralPath $source -Raw | ConvertFrom-Json
        foreach ($tool in $manifest.tools.PSObject.Properties) {
            if ($tool.Value.program -match '^[A-Za-z]:[\\/]') {
                $tool.Value.program = '%BREAD_TOOLS%/unpackers/' + $tool.Name + '/' + [IO.Path]::GetFileName($tool.Value.program)
            }
        }
        $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $target $file) -Encoding UTF8
    }
}
# Bundles that no longer exist in the repository are removed from the application too.
foreach ($existing in Get-ChildItem -LiteralPath $target -Directory) {
    if ($kinds.Name -notcontains $existing.Name) {
        if (-not $existing.FullName.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Unsafe obsolete tool directory: $($existing.FullName)" }
        Remove-Item -LiteralPath $existing.FullName -Recurse -Force
        Write-Host ("  {0,-12} removed (no longer in the bundle)" -f $existing.Name)
    }
}
# The stamp is what makes this incremental: the build target compares the bundle's
# files against it and skips the whole step when nothing changed.
$deployed = [ordered]@{
    schema = "bread-bundled-unpackers-deployed/v1"
    deployedAt = (Get-Date).ToUniversalTime().ToString("o")
    bundleRoot = '%BREAD_TOOLS%/unpackers'
    kinds = @($kinds | ForEach-Object { $_.Name })
    fileCount = (Get-ChildItem -LiteralPath $target -Recurse -File | Measure-Object).Count
}
($deployed | ConvertTo-Json -Depth 4) | Set-Content -Path (Join-Path $target "deployed.json") -Encoding UTF8
Write-Host "bundled tools deployed to $target"
exit 0
