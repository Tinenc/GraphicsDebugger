[CmdletBinding()]
param(
    [string]$ApplicationDirectory = "",
    [string]$BundleDirectory = "",
    [string]$Version = "1.13.93",
    [switch]$Restart
)
$ErrorActionPreference = 'Stop'
$repository = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $ApplicationDirectory) { $ApplicationDirectory = Join-Path $repository 'x64\Development' }
$ApplicationDirectory = [IO.Path]::GetFullPath($ApplicationDirectory)
$bundle = if ($BundleDirectory) { [IO.Path]::GetFullPath($BundleDirectory) } else { Join-Path $repository 'replaylab\output\dual-view-update-1.13.93' }
$manifestPath = Join-Path $bundle 'update-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) { throw "The verified update bundle has not been prepared: $manifestPath" }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.schema -ne 'bread-staged-update/v1' -or $manifest.version -ne $Version) { throw 'Unexpected update bundle schema/version.' }
$running = @(Get-Process RenderDocBread,q_render_soco_doc,qrenderdoc -ErrorAction SilentlyContinue | Where-Object { $_.Path -and [IO.Path]::GetDirectoryName($_.Path) -eq $ApplicationDirectory })
if ($running.Count) { throw 'Bread is still open. Save task progress, stop its active request normally, and exit Bread before installing. This installer does not stop tasks or kill the application.' }
foreach ($file in $manifest.files) {
    $path = [IO.Path]::GetFullPath((Join-Path $bundle $file.path))
    if (-not $path.StartsWith($bundle.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Update path escaped its bundle.' }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256) { throw "Update file differs from verification: $path" }
}
& (Join-Path $repository 'replaylab\packaging\Deploy-ReplayLabRuntime.ps1') -ApplicationDirectory $ApplicationDirectory -RuntimeOutputDirectory (Join-Path $bundle 'runtime')
& (Join-Path $repository 'util\i18n\deploy_replaylab_integration.ps1') -SourceRoot (Join-Path $bundle 'bridge') -TargetRoot (Join-Path $ApplicationDirectory 'integrations\replaylab\python\renderdoc_mcp_bridge')
Write-Output "Bread $Version installed. No Unity project, capture or conversation was changed."
if ($Restart) {
    $executable = @('RenderDocBread.exe','q_render_soco_doc.exe','qrenderdoc.exe') | ForEach-Object { Join-Path $ApplicationDirectory $_ } | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $executable) { throw 'No application executable exists in the requested installation directory.' }
    Start-Process -FilePath $executable -WindowStyle Hidden | Out-Null
}
