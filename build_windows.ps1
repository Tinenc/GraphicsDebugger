param(
    [ValidateSet('Development', 'Release')]
    [string]$Configuration = 'Development',
    [ValidateSet('x64', 'Win32')]
    [string]$Platform = 'x64'
)

$ErrorActionPreference = 'Stop'
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$msbuild = & $vswhere -latest -products '*' -requires Microsoft.Component.MSBuild -find 'MSBuild/**/Bin/MSBuild.exe' | Select-Object -First 1
if (!$msbuild) { throw 'Visual Studio MSBuild was not found.' }
Push-Location $PSScriptRoot
try {
    & $msbuild renderdoc.sln "/p:Configuration=$Configuration" "/p:Platform=$Platform" /m:4 /nr:false /nologo /v:minimal
    if ($LASTEXITCODE -ne 0) { throw "MSBuild failed ($LASTEXITCODE). Close the GUI if the linker reports a locked output." }
}
finally { Pop-Location }
