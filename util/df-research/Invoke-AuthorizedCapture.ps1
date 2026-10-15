[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetExecutable,

    [string[]]$TargetArguments = @(),

    [string]$ToolDirectory = (Join-Path $PSScriptRoot "..\..\x64\Release"),

    [string]$WorkingDirectory,

    [string]$OutputDirectory = (Join-Path $PSScriptRoot "..\..\captures\authorized"),

    [switch]$CaptureChildProcesses,

    [Parameter(Mandatory = $true)]
    [switch]$AcknowledgeAuthorization
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $AcknowledgeAuthorization) {
    throw "This launcher is limited to software you own or are explicitly authorised to test."
}

$target = [IO.Path]::GetFullPath($TargetExecutable)
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "Target executable not found: $target"
}

$toolDir = [IO.Path]::GetFullPath($ToolDirectory)
$cli = Join-Path $toolDir "TinecmaToolcmd.exe"
if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) {
    throw "TinecmaToolcmd.exe not found in: $toolDir"
}

if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
    $WorkingDirectory = Split-Path -Parent $target
}
$workingDir = [IO.Path]::GetFullPath($WorkingDirectory)
if (-not (Test-Path -LiteralPath $workingDir -PathType Container)) {
    throw "Working directory not found: $workingDir"
}

$outputDir = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

$startedAt = Get-Date
$stamp = $startedAt.ToString("yyyyMMdd-HHmmss")
$captureTemplate = Join-Path $outputDir "authorized-$stamp"
$receiptPath = Join-Path $outputDir "authorized-$stamp.json"

$captureArgs = @(
    "capture",
    "--wait-for-exit",
    "--working-dir", $workingDir,
    "--capture-file", $captureTemplate
)
if ($CaptureChildProcesses) {
    $captureArgs += "--opt-hook-children"
}
$captureArgs += $target
$captureArgs += $TargetArguments

Write-Host "Launching an authorised target with TinecmaTool."
Write-Host "Use the configured capture hotkey (normally F12 or Print Screen) while the target is rendering."
& $cli @captureArgs
$toolExitCode = $LASTEXITCODE

$captures = @(Get-ChildItem -LiteralPath $outputDir -Filter "*.rdc" -File |
    Where-Object { $_.LastWriteTime -ge $startedAt.AddSeconds(-2) } |
    Sort-Object LastWriteTime)

$receipt = [ordered]@{
    schemaVersion = 1
    startedAt = $startedAt.ToUniversalTime().ToString("o")
    completedAt = (Get-Date).ToUniversalTime().ToString("o")
    branch = (git -C (Join-Path $PSScriptRoot "..\..") branch --show-current 2>$null)
    commit = (git -C (Join-Path $PSScriptRoot "..\..") rev-parse HEAD 2>$null)
    tool = $cli
    target = $target
    captureChildProcesses = [bool]$CaptureChildProcesses
    toolExitCode = $toolExitCode
    captures = @($captures | ForEach-Object {
        [ordered]@{
            path = $_.FullName
            bytes = $_.Length
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
}
$receipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $receiptPath -Encoding UTF8

if ($toolExitCode -ne 0) {
    throw "TinecmaToolcmd exited with code $toolExitCode. Receipt: $receiptPath"
}
if ($captures.Count -eq 0) {
    Write-Warning "No .rdc was produced. Run again and press the capture hotkey while the authorised target renders. Receipt: $receiptPath"
    exit 2
}

Write-Host "Capture complete: $($captures[-1].FullName)"
Write-Host "Receipt: $receiptPath"
