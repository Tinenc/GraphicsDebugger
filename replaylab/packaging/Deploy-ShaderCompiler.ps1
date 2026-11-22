# Stage visual debugging's private DXC bundle. Existing workspace save routes
# can pin replaylab/tools/dxc.exe and its adjacent DLL hashes; never replace them.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ApplicationDirectory,
    [string]$PythonExecutable = "python",
    [string]$CacheDirectory = "",
    [string]$Archive = "",
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"
$application = (Resolve-Path -LiteralPath $ApplicationDirectory).Path.TrimEnd('\')
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot "shader_dxc_manifest.json") -Raw | ConvertFrom-Json
$release = $manifest.release
$definition = $manifest.platforms.'windows-x64'
if (-not $CacheDirectory) {
    $CacheDirectory = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) "BreadCompilerCache\dxc\$release\windows-x64"
}
$cache = [System.IO.Path]::GetFullPath($CacheDirectory)
$bundle = Join-Path $application "replaylab\tools\dxc"
$target = Join-Path $bundle "bin"
$acquisition = Join-Path $PSScriptRoot "fetch_shader_dxc.py"
$receiptPath = Join-Path $cache "bread-dxc-release.json"
$binaries = @("dxc.exe", "dxcompiler.dll", "dxil.dll")
$licenses = Join-Path $bundle "licenses\$release"

if ($VerifyOnly) {
    foreach ($item in $definition.fileSha256.PSObject.Properties) {
        $directory = $(if ($item.Name -in $binaries) { $target } else { $licenses })
        $path = Join-Path $directory $item.Name
        if (-not (Test-Path -LiteralPath $path) -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.Value) {
            throw "Packaged DXC differs from the pinned official release: $($item.Name)."
        }
    }
    & $PythonExecutable $acquisition --verify-executable (Join-Path $target "dxc.exe")
    if ($LASTEXITCODE -ne 0) { throw "Packaged DXC cannot generate both validated DXIL and SPIR-V." }
    exit 0
}

if (-not (Test-Path -LiteralPath $receiptPath)) {
    $arguments = @($acquisition, "--output-directory", $cache, "--platform", "windows-x64")
    if ($Archive) { $arguments += @("--archive", (Resolve-Path -LiteralPath $Archive).Path) }
    & $PythonExecutable @arguments
    if ($LASTEXITCODE -ne 0) { throw "Pinned DXC acquisition or real code-generation verification failed." }
}

$receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
if ($receipt.schema -ne "bread-dxc-installed/v1" -or $receipt.release -ne $release -or
    $receipt.archiveSha256 -ne $definition.sha256 -or $receipt.platform -ne "windows-x64") {
    throw "DXC cache does not match the pinned official release. Use a new cache directory."
}
foreach ($item in $definition.fileSha256.PSObject.Properties) {
    if ($item.Name -match '[/\\]' -or $item.Name -in @('.', '..')) { throw "Invalid Windows DXC receipt filename." }
    $path = Join-Path $cache $item.Name
    if (-not (Test-Path -LiteralPath $path) -or
        (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.Value) {
        throw "DXC cache changed: $($item.Name). Use a new cache directory."
    }
}
$changed = @($binaries | Where-Object {
    $path = Join-Path $target $_
    -not (Test-Path -LiteralPath $path) -or
        (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $definition.fileSha256.$_
})
if ($changed.Count) {
    # Do not replace private compiler DLLs while this application or its sidecars
    # are active. No process is terminated by packaging/deployment.
    $active = @(Get-Process | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($application + '\', [StringComparison]::OrdinalIgnoreCase) }
        catch { $false }
    })
    if ($active.Count) { throw "Stop the application and its sidecars before staging its pinned DXC." }
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    foreach ($name in $binaries) {
        Copy-Item -LiteralPath (Join-Path $cache $name) -Destination (Join-Path $target $name) -Force
    }
}
New-Item -ItemType Directory -Path $licenses -Force | Out-Null
foreach ($item in $definition.fileSha256.PSObject.Properties) {
    if ($item.Name -notin $binaries) {
        Copy-Item -LiteralPath (Join-Path $cache $item.Name) -Destination (Join-Path $licenses $item.Name) -Force
    }
}
Copy-Item -LiteralPath $receiptPath -Destination (Join-Path $licenses "bread-dxc-release.json") -Force
Write-Host "Pinned DXC $release is staged at $target"
