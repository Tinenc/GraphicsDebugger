#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$PythonExecutable,
    [Parameter(Mandatory=$true)][string]$DotNetExecutable,
    [string]$ApplicationDirectory = '',
    [switch]$SkipNativeBuild,
    [switch]$SkipRuntimeBuild
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
if (-not $ApplicationDirectory) { $ApplicationDirectory = Join-Path $repo 'x64\BreadDevelopment' }
$app = [IO.Path]::GetFullPath($ApplicationDirectory)
$packaging = Join-Path $repo 'replaylab\packaging'
$inputsRoot = Join-Path $repo 'replaylab\output\build-inputs'
function Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}
function Script([string]$Path, [string[]]$Arguments) {
    Checked 'powershell.exe' (@('-NoProfile','-ExecutionPolicy','Bypass','-File',$Path) + $Arguments)
}
function CopyTree([string]$Source, [string]$Destination) {
    Checked $script:buildPython @((Join-Path $packaging 'build_fs.py'),'copy','--source',$Source,'--destination',$Destination)
}
# This entry point creates a local Development installation. It does not package,
# publish, change Git history, kill processes, or replace a running installation.
$active = @(Get-Process | Where-Object {
    try { $_.Path -and $_.Path.StartsWith($app.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase) }
    catch { $false }
})
if ($active.Count) { throw "Close programs using the requested output directory first: $app" }
$sdk = @(& $DotNetExecutable --list-sdks | Where-Object { $_ -match '^10\.0\.302\s' })
if ($LASTEXITCODE -ne 0 -or -not $sdk.Count) { throw 'UEX requires the locked .NET SDK 10.0.302.' }
$oldPath = $env:PATH
$oldPython = $env:BREAD_BUILD_PYTHON
Push-Location $repo
try {
    Script (Join-Path $packaging 'Restore-BuildEnvironment.ps1') @('-PythonExecutable',$PythonExecutable,'-OutputRoot',$inputsRoot)
    $script:buildPython = Join-Path $inputsRoot 'environment\Scripts\python.exe'
    $inputs = Get-Content -LiteralPath (Join-Path $inputsRoot 'paths.json') -Raw | ConvertFrom-Json
    $env:BREAD_BUILD_PYTHON = $script:buildPython
    $env:PATH = (Split-Path -Parent $script:buildPython)+';'+$oldPath
    New-Item -ItemType Directory -Force -Path $app | Out-Null
    CopyTree $inputs.'application-tools' $app
    CopyTree $inputs.'shader-language-server' $app
    if (-not $SkipNativeBuild) {
        Script (Join-Path $repo 'util\i18n\update_translations.ps1') @('-QtBin',$inputs.'qt-linguist','-Check')
        Script (Join-Path $repo 'util\i18n\build_translations.ps1') @('-QtBin',$inputs.'qt-linguist','-RequireComplete')
        $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
        $msbuild = & $vswhere -latest -requires Microsoft.Component.MSBuild -find 'MSBuild\**\Bin\amd64\MSBuild.exe' | Select-Object -First 1
        if (-not $msbuild) { throw 'Visual Studio 2022 C++ build tools are required.' }
        foreach ($project in @('renderdoc\renderdoc.vcxproj','renderdoccmd\renderdoccmd.vcxproj',
            'renderdocshim\renderdocshim.vcxproj','qrenderdoc\qrenderdoc_local.vcxproj',
            'qrenderdoc\Code\pyrenderdoc\pyrenderdoc_module.vcxproj',
            'qrenderdoc\Code\pyrenderdoc\qrenderdoc_module.vcxproj')) {
            Checked $msbuild @((Join-Path $repo $project),'/m:2','/t:Build','/nr:false','/nologo','/v:minimal',
                '/p:Configuration=Development','/p:Platform=x64','/p:ReplayOptimizeDevelopment=true',
                "/p:SolutionDir=$repo\", "/p:OutDir=$app\",'/p:SkipReplayLabRuntime=true')
        }
        New-Item -ItemType Directory -Force -Path (Join-Path $app 'pymodules') | Out-Null
        foreach ($binding in @('renderdoc.pyd','qrenderdoc.pyd')) {
            Copy-Item -LiteralPath (Join-Path $app $binding) -Destination (Join-Path $app ('pymodules\'+$binding)) -Force
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $app 'qTinecmaTool.exe'))) { throw 'qTinecmaTool.exe was not built.' }
    if (-not $SkipRuntimeBuild) {
        Script (Join-Path $packaging 'Build-ReplayLabRuntime.ps1') @('-PythonExecutable',$script:buildPython)
    }
    # Native post-build can stage tools before the runtime deployer has claimed
    # its directory. Preserve those generated files in a unique sibling backup.
    $runtime = Join-Path $app 'replaylab'
    $marker = Join-Path $runtime '.replaylab-runtime-deployment.json'
    $oldRuntime = ''
    if ((Test-Path -LiteralPath $runtime) -and -not (Test-Path -LiteralPath $marker)) {
        $oldRuntime = Join-Path $app ('replaylab-before-deploy-'+[Guid]::NewGuid().ToString('N'))
        if (-not ([IO.Path]::GetFullPath($runtime)).StartsWith($app.TrimEnd('\')+'\') -or
            -not ([IO.Path]::GetFullPath($oldRuntime)).StartsWith($app.TrimEnd('\')+'\')) { throw 'Invalid runtime paths' }
        Move-Item -LiteralPath $runtime -Destination $oldRuntime
    }
    Script (Join-Path $packaging 'Deploy-ReplayLabRuntime.ps1') @('-ApplicationDirectory',$app)
    CopyTree $inputs.'application-tools' $app
    CopyTree $inputs.'shader-language-server' $app
    Script (Join-Path $repo 'util\i18n\deploy_replaylab_integration.ps1') @('-TargetRoot',(Join-Path $app 'integrations\replaylab\python\renderdoc_mcp_bridge'))
    CopyTree (Join-Path $repo '.agents\skills') (Join-Path $runtime 'skills')
    CopyTree (Join-Path $repo 'replaylab\portable_skills') (Join-Path $runtime 'portable-skills')
    CopyTree $inputs.'unity-mcp' (Join-Path $runtime 'tools\unity-mcp')
    Checked $script:buildPython @((Join-Path $packaging 'prepare_standard_capabilities.py'),
        '--source',$inputs.capabilities,'--output',(Join-Path $runtime 'tools\capabilities'))
    $uexSource = Join-Path $repo 'output\DF-Bread-tools\uex-source'
    $uexPublish = Join-Path $repo 'output\DF-Bread-tools\uex-win-x64'
    CopyTree $inputs.'uex-source' $uexSource
    CopyTree (Join-Path $repo 'replaylab\dependencies\uex-locks') $uexSource
    Copy-Item -LiteralPath (Join-Path $packaging 'dotnet-sdk.global.json') -Destination (Join-Path $uexSource 'global.json') -Force
    $project = Join-Path $uexSource 'src\Uex\Uex.csproj'
    Push-Location $uexSource
    try {
        Checked $DotNetExecutable @('restore',$project,'-r','win-x64','--locked-mode','--source',$inputs.'nuget-feed',
            '--packages',(Join-Path $repo 'output\DF-Bread-tools\nuget-packages'),
            '-p:CUE4PARSE_SKIP_NATIVE=true','-p:SelfContained=true','-p:RuntimeFrameworkVersion=10.0.10','-p:NuGetAudit=false')
        Checked $DotNetExecutable @('publish',$project,'-c','Release','-r','win-x64','--self-contained','true',
            '-o',$uexPublish,'--no-restore','-p:CUE4PARSE_SKIP_NATIVE=true','-p:PublishSingleFile=false',
            '-p:PublishTrimmed=false','-p:RuntimeFrameworkVersion=10.0.10','-p:NuGetAudit=false')
    } finally { Pop-Location }
    $uexTarget = Join-Path $runtime 'tools\unpackers\uex'
    CopyTree $uexPublish $uexTarget
    CopyTree $inputs.'uex-native' (Join-Path $uexTarget '.uex-cache')
    foreach ($notice in @(@('LICENSE','LICENSE.uex.txt'),@('deps\CUE4Parse\LICENSE','LICENSE.CUE4Parse.txt'),@('deps\CUE4Parse\NOTICE','NOTICE.CUE4Parse.txt'))) {
        Copy-Item -LiteralPath (Join-Path $uexSource $notice[0]) -Destination (Join-Path $uexTarget $notice[1]) -Force
    }
    Checked $script:buildPython @((Join-Path $packaging 'verify_uex_natives.py'),(Join-Path $uexTarget '.uex-cache'))
    Checked (Join-Path $uexTarget 'uex.exe') @('--version')
    Script (Join-Path $packaging 'Deploy-ReplayLabRuntime.ps1') @('-ApplicationDirectory',$app,'-ValidateOnly')
    Write-Host "DF + Bread Development is ready: $app\qTinecmaTool.exe"
} finally {
    Pop-Location
    $env:PATH = $oldPath
    if ($null -eq $oldPython) { Remove-Item Env:BREAD_BUILD_PYTHON -ErrorAction SilentlyContinue }
    else { $env:BREAD_BUILD_PYTHON = $oldPython }
}
