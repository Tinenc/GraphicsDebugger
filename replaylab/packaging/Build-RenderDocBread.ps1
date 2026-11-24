#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ApplicationDirectory = '',
    [string]$Destination = '',
    [string]$PythonExecutable = '',
    [string]$UexSource = '',
    [string]$UexPublishDirectory = '',
    [string]$CapabilitiesDirectory = '',
    [string]$QtBin = '',
    [string]$BuildInputRoot = '',
    [string]$DotNetExecutable = '',
    [switch]$SkipNativeBuild,
    [switch]$SkipRuntimeBuild,
    [switch]$AuditOnly,
    [switch]$DisableReplayOptimization
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'RenderDocBread.package.json') -Raw | ConvertFrom-Json
# Reject stale generated versions before dependency restoration or any build.
if (-not $PythonExecutable) { $PythonExecutable = (Get-Command python.exe -ErrorAction Stop).Source }
& $PythonExecutable (Join-Path $PSScriptRoot 'version_policy.py') --check
if ($LASTEXITCODE -ne 0) { throw 'Synchronize the Bread release identity with version_policy.py --write before building.' }
if (-not $Destination) { $Destination = Join-Path $repoRoot $config.destination }
if (-not $BuildInputRoot) { $BuildInputRoot = Join-Path $repoRoot 'replaylab\output\build-inputs' }
# Fail before the expensive native build if the sealed offline UEX apphost cannot be restored.
if (-not $UexPublishDirectory) {
    if (-not $DotNetExecutable) {
        $dotnetCommand = Get-Command dotnet.exe -ErrorAction SilentlyContinue
        if ($dotnetCommand) { $DotNetExecutable = $dotnetCommand.Source }
    }
    if (-not $DotNetExecutable) { throw 'Install the x64 .NET SDK 10.0.302, or supply -DotNetExecutable / -UexPublishDirectory.' }
    $sdkListing = & $DotNetExecutable --list-sdks
    $pinnedSdk = @($sdkListing | Where-Object { $_ -match '^10\.0\.302\s+\[(.+)\]$' })
    if ($LASTEXITCODE -ne 0 -or $pinnedSdk.Count -eq 0) {
        throw 'The offline UEX build requires x64 .NET SDK 10.0.302 (apphost 10.0.10). Install that version; a newer feature band alone cannot use the sealed offline feed. Private installs can use -DotNetExecutable.'
    }
    $sdkRoot = [regex]::Match($pinnedSdk[0], '\[(.+)\]').Groups[1].Value
    $apphostPack = Join-Path (Split-Path -Parent $sdkRoot) 'packs\Microsoft.NETCore.App.Host.win-x64\10.0.10'
    if (-not (Test-Path -LiteralPath $apphostPack -PathType Container)) { throw "The pinned .NET apphost pack 10.0.10 is missing: $apphostPack. Repair .NET SDK 10.0.302." }
}
# Restore locked third-party inputs before looking for a pre-existing application.
# Checked-in archive parts restore all sealed inputs offline after an ordinary clone.
$restoreArguments = @('-OutputRoot', $BuildInputRoot)
if ($PythonExecutable) { $restoreArguments += @('-PythonExecutable', $PythonExecutable) }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'Restore-BuildEnvironment.ps1') @restoreArguments
if ($LASTEXITCODE -ne 0) { throw 'Restoring the build inputs/environment failed.' }
$PythonExecutable = Join-Path $BuildInputRoot 'environment\Scripts\python.exe'
$paths = Get-Content -LiteralPath (Join-Path $BuildInputRoot 'paths.json') -Raw | ConvertFrom-Json
$env:PATH = (Split-Path -Parent $PythonExecutable) + ';' + $env:PATH
if (-not $QtBin) { $QtBin = $paths.'qt-linguist' }
if (-not $CapabilitiesDirectory) { $CapabilitiesDirectory = $paths.capabilities }
if ($CapabilitiesDirectory) {
    & $PythonExecutable (Join-Path $PSScriptRoot 'prepare_capabilities_bundle.py') --output $CapabilitiesDirectory --public
    if ($LASTEXITCODE -ne 0) { throw 'The explicitly selected capabilities snapshot failed verification.' }
}
function Invoke-CheckedScript([string]$Script, [string[]]$Arguments) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Script @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Script failed (exit $LASTEXITCODE)" }
}

# The default is a source build. An explicit installed ApplicationDirectory remains
# a read-only compatibility input for incremental packaging, never a prerequisite.
$nativeOutput = Join-Path $repoRoot 'output\RenderDocBread-native'
$gui = Join-Path $nativeOutput 'RenderDocBread.exe'
if ($ApplicationDirectory) { $ApplicationDirectory = (Resolve-Path -LiteralPath $ApplicationDirectory).Path }
elseif ($SkipNativeBuild) { throw '-SkipNativeBuild requires an explicit -ApplicationDirectory.' }
else { $ApplicationDirectory = $nativeOutput }
New-Item -ItemType Directory -Force -Path $nativeOutput | Out-Null
foreach ($inputDirectory in @($paths.'application-tools', $paths.'shader-language-server')) {
    & $PythonExecutable (Join-Path $PSScriptRoot 'build_fs.py') copy --source $inputDirectory --destination $nativeOutput
    if ($LASTEXITCODE -ne 0) { throw "Copying sealed native tool input failed: $inputDirectory" }
}
if (-not $SkipNativeBuild) {
    # Shipping Development diagnostics does not require unoptimized replay.
    $replayOptimization = if ($DisableReplayOptimization) { 'false' } else { 'true' }
    Invoke-CheckedScript (Join-Path $repoRoot 'util\i18n\update_translations.ps1') @('-QtBin', $QtBin, '-Check')
    Invoke-CheckedScript (Join-Path $repoRoot 'util\i18n\build_translations.ps1') @('-QtBin', $QtBin, '-RequireComplete')
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $msbuild = & $vswhere -latest -requires Microsoft.Component.MSBuild -find 'MSBuild\**\Bin\amd64\MSBuild.exe' | Select-Object -First 1
    if (-not $msbuild) { throw 'MSBuild with the C++ workload is required.' }
    foreach ($project in @('renderdoc\renderdoc.vcxproj', 'renderdoc\renderdoc_no_export.vcxproj',
                            'renderdoccmd\renderdoccmd.vcxproj', 'renderdocshim\renderdocshim.vcxproj',
                            'qrenderdoc\qrenderdoc_local.vcxproj')) {
        & $msbuild (Join-Path $repoRoot $project) /m:2 /t:Build `
            /p:Configuration=Development /p:Platform=x64 "/p:ReplayOptimizeDevelopment=$replayOptimization" "/p:SolutionDir=$repoRoot\" `
            "/p:OutDir=$nativeOutput\" /p:SkipReplayLabRuntime=true /p:BuildProjectReferences=true
        if ($LASTEXITCODE -ne 0) { throw "Native build failed: $project. No running application was replaced." }
    }
    # The API bindings must come from the current core headers, not an old deployed PYD.
    New-Item -ItemType Directory -Force -Path (Join-Path $nativeOutput 'pymodules') | Out-Null
    foreach ($binding in @(@('pyrenderdoc_module.vcxproj', 'renderdoc.pyd'), @('qrenderdoc_module.vcxproj', 'qrenderdoc.pyd'))) {
        & $msbuild (Join-Path $repoRoot ('qrenderdoc\Code\pyrenderdoc\' + $binding[0])) /m:2 /t:Build `
            /p:Configuration=Development /p:Platform=x64 "/p:ReplayOptimizeDevelopment=$replayOptimization" "/p:SolutionDir=$repoRoot\" `
            "/p:OutDir=$nativeOutput\" /p:BuildProjectReferences=false
        if ($LASTEXITCODE -ne 0) { throw "Building the matching Python bindings failed: $($binding[0])" }
        Copy-Item -LiteralPath (Join-Path $nativeOutput $binding[1]) -Destination (Join-Path $nativeOutput ('pymodules\' + $binding[1])) -Force
    }
} else {
    $gui = Join-Path $ApplicationDirectory 'RenderDocBread.exe'
}
if (-not (Test-Path -LiteralPath $gui)) { throw "Build RenderDocBread.exe first: $gui" }
if (-not $SkipRuntimeBuild) {
    Invoke-CheckedScript (Join-Path $PSScriptRoot 'Build-ReplayLabRuntime.ps1') @('-PythonExecutable', $PythonExecutable, '-PortableHosts')
}
$runtime = Join-Path $repoRoot 'replaylab\output\portable-runtime'
$unityBundle = $paths.'unity-mcp'
& $PythonExecutable -c 'import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from prepare_unity_bundle import verify;verify(Path(sys.argv[2]))' $PSScriptRoot $unityBundle
if ($LASTEXITCODE -ne 0) { throw 'Preparing the bundled Unity MCP runtime failed.' }

# UEX's ordinary publish requires .NET 10 on the recipient. Publish the same patched source
# self-contained so the zip does not depend on any installed dotnet/registry/PATH runtime.
if (-not $UexPublishDirectory) {
    $uexInputSource = if ($UexSource) { $UexSource } else { $paths.'uex-source' }
    # Always build an owned copy, preserving a caller's source and global.json too.
    $UexSource = Join-Path $repoRoot 'output\RenderDocBread-tools\uex-source'
    if ([IO.Path]::GetFullPath($uexInputSource) -ne [IO.Path]::GetFullPath($UexSource)) {
        New-Item -ItemType Directory -Force -Path $UexSource | Out-Null
        & $PythonExecutable (Join-Path $PSScriptRoot 'build_fs.py') copy --source $uexInputSource --destination $UexSource
        if ($LASTEXITCODE -ne 0) { throw 'Copying the sealed UEX source failed.' }
    }
        $project = Join-Path $UexSource 'src\Uex\Uex.csproj'
        if (-not (Test-Path -LiteralPath $project)) {
            throw 'A self-contained UEX publish is required. Supply -UexPublishDirectory, or -UexSource pointing to the patched UEX checkout.'
        }
        $UexPublishDirectory = Join-Path $repoRoot 'output\RenderDocBread-tools\uex-win-x64'
        $lockRoot = Join-Path $repoRoot 'replaylab\dependencies\uex-locks'
        & $PythonExecutable (Join-Path $PSScriptRoot 'build_fs.py') copy --source $lockRoot --destination $UexSource
        if ($LASTEXITCODE -ne 0) { throw 'Copying the UEX dependency locks failed.' }
        # Resolve the SDK from an application-owned working directory, not a caller's global.json.
        $sdkSelection = Join-Path $repoRoot 'output\RenderDocBread-tools\dotnet-sdk'
        New-Item -ItemType Directory -Force -Path $sdkSelection | Out-Null
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'dotnet-sdk.global.json') -Destination (Join-Path $sdkSelection 'global.json') -Force
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'dotnet-sdk.global.json') -Destination (Join-Path $UexSource 'global.json') -Force
        Push-Location $sdkSelection
        try {
        $selectedSdk = & $DotNetExecutable --version
        if ($LASTEXITCODE -ne 0 -or $selectedSdk -ne '10.0.302') { throw 'The pinned .NET SDK 10.0.302 could not be selected.' }
        $nugetPackages = Join-Path $repoRoot 'output\RenderDocBread-tools\nuget-packages'
        & $DotNetExecutable restore $project -r win-x64 --locked-mode --source $paths.'nuget-feed' --packages $nugetPackages `
            -p:CUE4PARSE_SKIP_NATIVE=true -p:SelfContained=true -p:RuntimeFrameworkVersion=10.0.10 -p:NuGetAudit=false
        if ($LASTEXITCODE -ne 0) { throw 'Offline restore of the locked UEX dependencies failed.' }
        & $DotNetExecutable publish $project -c Release -r win-x64 --self-contained true -o $UexPublishDirectory `
            --no-restore -p:CUE4PARSE_SKIP_NATIVE=true -p:PublishSingleFile=false -p:PublishTrimmed=false `
            -p:RuntimeFrameworkVersion=10.0.10 -p:NuGetAudit=false
        if ($LASTEXITCODE -ne 0) { throw 'Self-contained UEX publish failed.' }
        } finally { Pop-Location }
        foreach ($notice in @(@('LICENSE', 'LICENSE.uex.txt'), @('deps\CUE4Parse\LICENSE', 'LICENSE.CUE4Parse.txt'),
                               @('deps\CUE4Parse\NOTICE', 'NOTICE.CUE4Parse.txt'))) {
            Copy-Item -LiteralPath (Join-Path $UexSource $notice[0]) -Destination (Join-Path $UexPublishDirectory $notice[1]) -Force
        }
}
$vsRoot = Join-Path ${env:ProgramFiles} 'Microsoft Visual Studio'
$crt = Get-ChildItem -LiteralPath $vsRoot -Filter 'Microsoft.VC143.CRT' -Directory -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Parent.Name -eq 'x64' } | Sort-Object FullName -Descending | Select-Object -First 1
if (-not $crt) { throw 'The redistributable x64 VC143 CRT was not found in Visual Studio.' }
$arguments = @((Join-Path $PSScriptRoot 'package_portable.py'), '--application', $ApplicationDirectory,
    '--gui', $gui, '--runtime', $runtime, '--uex', $UexPublishDirectory, '--crt', $crt.FullName,
    '--destination', $Destination, '--unity-mcp', $unityBundle)
if ($CapabilitiesDirectory) { $arguments += @('--capabilities', $CapabilitiesDirectory) }
if ($AuditOnly) { $arguments += '--audit-only' }
& $PythonExecutable @arguments
if ($LASTEXITCODE -ne 0) { throw 'Portable package validation failed; no shareable archive was approved.' }
