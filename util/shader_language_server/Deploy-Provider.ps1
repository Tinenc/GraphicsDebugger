[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ApplicationDirectory,
    [string]$ArchivePath = "",
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot "provider.json") -Raw | ConvertFrom-Json
$python = Join-Path $repoRoot "replaylab\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $pythonCommand = Get-Command python.exe -ErrorAction Stop
    $python = $pythonCommand.Source
}
$script = Join-Path $PSScriptRoot "deploy_provider.py"
$application = [System.IO.Path]::GetFullPath($ApplicationDirectory)
# Verifying an already installed bundle avoids rewriting a running language server.
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $python $script --application $application --verify-only 2>$null
$verified = $LASTEXITCODE -eq 0
$ErrorActionPreference = $previousErrorAction
if ($verified) { exit 0 }
if ($VerifyOnly) { throw "The packaged Shader language server or its licenses are missing or changed." }
if (-not $ArchivePath) { $ArchivePath = $env:BREAD_SHADER_LANGUAGE_SERVER_ARCHIVE }
if (-not $ArchivePath) {
    $ArchivePath = Join-Path $env:LOCALAPPDATA "Bread\provider-cache\slang-$($manifest.version)-$($manifest.platform).zip"
}
if (-not (Test-Path -LiteralPath $ArchivePath)) {
    throw "Pinned Shader language-server archive is unavailable. Download $($manifest.archive_url), then run Deploy-Provider.ps1 -ApplicationDirectory `"$application`" -ArchivePath <archive>. Builds never download dependencies silently."
}
& $python $script --archive $ArchivePath --application $application
if ($LASTEXITCODE -ne 0) { throw "Shader language-server deployment failed (exit $LASTEXITCODE)." }
& $python $script --application $application --verify-only
if ($LASTEXITCODE -ne 0) { throw "Shader language-server verification failed (exit $LASTEXITCODE)." }
