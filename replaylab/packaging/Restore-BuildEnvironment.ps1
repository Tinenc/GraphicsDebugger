#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$PythonExecutable = '',
    [string]$OutputRoot = '',
    [switch]$VerifyOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $OutputRoot) { $OutputRoot = Join-Path $repoRoot 'replaylab\output\build-inputs' }
if (-not $PythonExecutable) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) { $PythonExecutable = & $launcher.Source -3.12 -c 'import sys;print(sys.executable)' }
    if (-not $PythonExecutable) {
        $command = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($command) { $PythonExecutable = $command.Source }
    }
}
if (-not $PythonExecutable) { throw 'Install 64-bit CPython 3.12 or supply -PythonExecutable.' }
& $PythonExecutable -c 'import sys;assert sys.version_info[:2]==(3,12) and sys.maxsize>2**32, "64-bit CPython 3.12 required"'
if ($LASTEXITCODE -ne 0) { throw 'The selected build interpreter is not 64-bit CPython 3.12.' }
$arguments = @((Join-Path $PSScriptRoot 'restore_dependencies.py'), '--output', (Join-Path $OutputRoot 'expanded'))
if ($VerifyOnly) { $arguments += '--verify-only' }
else { $arguments += @('--receipt', (Join-Path $OutputRoot 'paths.json')) }
& $PythonExecutable @arguments
if ($LASTEXITCODE -ne 0) { throw 'Dependency input restoration failed. Check the complete source checkout and the preceding diagnostic.' }
if ($VerifyOnly) { return }
$paths = Get-Content -LiteralPath (Join-Path $OutputRoot 'paths.json') -Raw | ConvertFrom-Json
& $PythonExecutable (Join-Path $PSScriptRoot 'bootstrap_build.py') --output (Join-Path $OutputRoot 'environment') --wheels $paths.'python-wheels'
if ($LASTEXITCODE -ne 0) { throw 'Creating the locked offline build environment failed.' }
Write-Host "Build Python: $(Join-Path $OutputRoot 'environment\Scripts\python.exe')"
