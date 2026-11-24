# Compatibility entry point. Assembly always happens in a private staging directory.
[CmdletBinding()]
param([string]$ApplicationDirectory = '', [string]$Destination = '', [switch]$AuditOnly,
      [switch]$Force, [switch]$SkipDeploy)
$ErrorActionPreference = 'Stop'
if ($Force) { throw 'Incomplete releases cannot bypass the portability audit.' }
$arguments = @('-SkipNativeBuild', '-SkipRuntimeBuild')
if ($ApplicationDirectory) { $arguments += @('-ApplicationDirectory', $ApplicationDirectory) }
if ($Destination) { $arguments += @('-Destination', $Destination) }
if ($AuditOnly) { $arguments += '-AuditOnly' }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'Build-RenderDocBread.ps1') @arguments
exit $LASTEXITCODE
