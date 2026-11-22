[CmdletBinding()]
param(
    [string]$PythonExecutable = "",
    [string]$OutputDirectory = "",
    [string]$WorkDirectory = "",
    [string]$TemporaryBuildRoot = "",
    [switch]$KeepBuildWork,
    [switch]$PortableHosts,
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$supportDirectoryName = "bread-host-support"
$runtimeManifestName = "replaylab-runtime-manifest.json"
$runtimeManifestSchema = "replaylab-runtime-output/v1"
$buildSessionMarkerName = ".replaylab-build-session.json"
$buildSessionSchema = "replaylab-pyinstaller-session/v1"
$ownedRuntimeFiles = @(
    "replaylab-ai.exe",
    "ReplayLabInternal.exe",
    "BreadMCP.exe",
    "ReplayLabShaderTools.exe",
    $runtimeManifestName
)

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    $stream = [IO.File]::Open(
        [IO.Path]::GetFullPath($LiteralPath),
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read)
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try {
            return [BitConverter]::ToString(
                $algorithm.ComputeHash($stream)).Replace("-", "").ToLowerInvariant()
        }
        finally {
            $algorithm.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [IO.Path]::GetFullPath($Path).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar)
}

function Test-SamePath {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )
    return (Get-NormalizedPath $Left).Equals(
        (Get-NormalizedPath $Right), [StringComparison]::OrdinalIgnoreCase)
}

function Test-PathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [switch]$AllowSame
    )
    $base = Get-NormalizedPath $BasePath
    $target = Get-NormalizedPath $TargetPath
    if ($AllowSame -and $target.Equals($base, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = $base + [IO.Path]::DirectorySeparatorChar
    return $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-PathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [Parameter(Mandatory = $true)][string]$Role
    )
    $target = Get-NormalizedPath $TargetPath
    if (-not (Test-PathWithin $BasePath $target)) {
        throw "$Role must be a child of the ReplayLab source root: $target"
    }
    return $target
}

function Assert-NoReparseComponents {
    param(
        [Parameter(Mandatory = $true)][string]$AbsolutePath,
        [Parameter(Mandatory = $true)][string]$Role
    )
    $fullPath = [IO.Path]::GetFullPath($AbsolutePath)
    $filesystemRoot = [IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($filesystemRoot)) {
        throw "$Role must have a filesystem root: $AbsolutePath"
    }
    $relative = $fullPath.Substring($filesystemRoot.Length)
    $parts = $relative.Split(
        [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar),
        [StringSplitOptions]::RemoveEmptyEntries)
    $current = $filesystemRoot
    foreach ($part in $parts) {
        $current = Join-Path $current $part
        if (-not (Test-Path -LiteralPath $current)) {
            continue
        }
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Role must not pass through a symlink, junction, or reparse point: $current"
        }
    }
    return $fullPath
}

function Assert-RegularFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Role
    )
    $fullPath = Assert-NoReparseComponents ([IO.Path]::GetFullPath($Path)) $Role
    $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
    if ($item.PSIsContainer -or $item -isnot [IO.FileInfo] -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Role must be an ordinary file: $fullPath"
    }
    return $fullPath
}

function Assert-GeneratedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$ReplayLabRoot,
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Role,
        [Parameter(Mandatory = $true)][string[]]$ProtectedRoots
    )
    $safePath = Assert-PathWithin $ReplayLabRoot $Candidate $Role
    Assert-NoReparseComponents $safePath $Role | Out-Null
    foreach ($protectedRoot in $ProtectedRoots) {
        if (Test-PathWithin $protectedRoot $safePath -AllowSame) {
            throw "$Role must not overlap source or packaging files: $safePath"
        }
    }
    if (Test-Path -LiteralPath $safePath) {
        $item = Get-Item -LiteralPath $safePath -Force -ErrorAction Stop
        if (-not $item.PSIsContainer) {
            throw "$Role must be a directory: $safePath"
        }
    }
    return $safePath
}

function Assert-OutputDirectorySafe {
    param(
        [Parameter(Mandatory = $true)][string]$OutputRoot,
        [Parameter(Mandatory = $true)][string[]]$OwnedFiles
    )
    if (-not (Test-Path -LiteralPath $OutputRoot)) {
        return
    }
    $children = @(Get-ChildItem -LiteralPath $OutputRoot -Force)
    if ($children.Count -eq 0) {
        return
    }

    $manifestPath = Join-Path $OutputRoot $runtimeManifestName
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Existing non-empty OutputDirectory is not a marked ReplayLab runtime output: $OutputRoot"
    }
    $manifestPath = Assert-RegularFile $manifestPath "ReplayLab runtime output marker"
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "Existing ReplayLab runtime output marker is invalid: $manifestPath"
    }
    if ([string]$manifest.schema -ne $runtimeManifestSchema) {
        throw "Existing ReplayLab runtime output marker has an unsupported schema: $manifestPath"
    }

    $allowed = @{}
    foreach ($name in $OwnedFiles) {
        $allowed[$name.ToLowerInvariant()] = $true
    }
    foreach ($nameValue in @($manifest.ownedFiles)) {
        $name = [string]$nameValue
        if ([string]::IsNullOrWhiteSpace($name) -or
            $name -ne [IO.Path]::GetFileName($name) -or
            -not $allowed.ContainsKey($name.ToLowerInvariant())) {
            throw "ReplayLab runtime output marker contains an unsafe owned file: $name"
        }
    }
    foreach ($ownedName in $OwnedFiles) {
        $ownedPath = Join-Path $OutputRoot $ownedName
        if (Test-Path -LiteralPath $ownedPath) {
            Assert-RegularFile $ownedPath "ReplayLab-owned runtime file '$ownedName'" | Out-Null
        }
    }
}

function Resolve-PythonExecutable {
    param([Parameter(Mandatory = $true)][string]$Candidate)
    $resolved = $Candidate
    if (-not [IO.Path]::IsPathRooted($resolved)) {
        $command = Get-Command $resolved -CommandType Application -ErrorAction Stop
        $resolved = $command.Source
    }
    return Assert-RegularFile ([IO.Path]::GetFullPath($resolved)) "PythonExecutable"
}

function Get-BuildProbe {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$AgentSource,
        [Parameter(Mandatory = $true)][string]$McpSource
    )
    $probeCode = @'
import importlib.util
import json
from pathlib import Path
import struct
import sys

sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
required = [
    'PyInstaller', 'mcp', 'uvicorn', 'starlette', 'httpx', 'pydantic',
    'yaml', 'langgraph', 'genai_prices', 'acp', 'deepagents', 'langchain',
    'langchain_core', 'langchain_openai', 'langchain_anthropic',
    'langchain_google_genai', 'langchain_google_vertexai',
    'src.server', 'src.ipc_client',
]
missing = [name for name in required if importlib.util.find_spec(name) is None]
from replaylab_agent.runtime_dependencies import verify_deepagents_version
deepagents_version = verify_deepagents_version(Path(sys.argv[1]).parent / 'pyproject.toml')
# API profiles always use deepagents, so every supported provider adapter is
# part of the frozen runtime rather than an opportunistic optional bundle.
deep_modules = [
    'deepagents', 'langchain', 'langchain_core', 'langchain_openai',
    'langchain_anthropic', 'langchain_google_genai',
    'langchain_google_vertexai',
]
pyinstaller_version = None
if 'PyInstaller' not in missing:
    import PyInstaller
    pyinstaller_version = PyInstaller.__version__
from replaylab_agent import __version__ as agent_version
from src.version import SERVICE_VERSION as mcp_version
print(json.dumps({
    'python': list(sys.version_info[:3]),
    'pointerBits': struct.calcsize('P') * 8,
    'pyinstaller': pyinstaller_version,
    'agentVersion': agent_version,
    'mcpVersion': mcp_version,
    'missing': missing,
    'deepModules': deep_modules,
    'deepagentsVersion': deepagents_version,
}))
'@
    $probeOutput = & $Python -c $probeCode $AgentSource $McpSource
    if ($LASTEXITCODE -ne 0) {
        throw "Python build-environment probe failed."
    }
    try {
        return (($probeOutput | Out-String) | ConvertFrom-Json)
    }
    catch {
        throw "Python build-environment probe returned invalid JSON."
    }
}

function Assert-BuildProbe {
    param(
        [Parameter(Mandatory = $true)][object]$Probe,
        [Parameter(Mandatory = $true)][string]$ReplayLabRoot,
        [Parameter(Mandatory = $true)][string]$McpRoot
    )
    $version = @($Probe.python)
    if ($version.Count -lt 2 -or [int]$version[0] -ne 3 -or [int]$version[1] -lt 12) {
        throw "ReplayLab runtime packaging requires Python 3.12 or newer."
    }
    if ([int]$Probe.pointerBits -ne 64) {
        throw "ReplayLab Windows runtime packaging requires a 64-bit Python interpreter."
    }
    $missing = @($Probe.missing)
    if ($missing.Count -gt 0) {
        $agentInstall = "${ReplayLabRoot}[all]"
        $mcpInstall = "${McpRoot}[bundle]"
        throw ("Runtime build dependencies are missing: {0}. Install them with: `"{1}`" -m pip install -e `"{2}`" -e `"{3}`"" -f `
            ($missing -join ", "), $PythonExecutable, $agentInstall, $mcpInstall)
    }
    $pyInstallerMajor = [int](([string]$Probe.pyinstaller).Split('.')[0])
    if ($pyInstallerMajor -ne 6) {
        throw "ReplayLab runtime packaging requires PyInstaller 6.x; found $($Probe.pyinstaller)."
    }
    if ([string]$Probe.agentVersion -ne [string]$Probe.mcpVersion) {
        throw "AgentHost and BreadMCP versions must match before packaging."
    }
}

function Invoke-PyInstallerBuild {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$EntryPoint,
        [Parameter(Mandatory = $true)][string]$ImportRoot,
        [Parameter(Mandatory = $true)][string]$DistPath,
        [Parameter(Mandatory = $true)][string]$BuildPath,
        [Parameter(Mandatory = $true)][string]$SpecPath,
        [string[]]$AdditionalImportRoots = @(),
        [string[]]$CollectAll = @(),
        [string[]]$CollectSubmodules = @(),
        [string[]]$HiddenImports = @(),
        [string[]]$AddData = @(),
        [string[]]$CopyMetadata = @(),
        [switch]$SharedSupport
    )
    $arguments = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--noupx",
        "--log-level", "WARN",
        "--name", $Name,
        "--paths", $ImportRoot,
        "--distpath", $DistPath,
        "--workpath", $BuildPath,
        "--specpath", $SpecPath
    )
    if ($SharedSupport) {
        $arguments += @("--onedir", "--contents-directory", $supportDirectoryName)
    } else {
        $arguments += "--onefile"
    }
    foreach ($root in $AdditionalImportRoots) {
        $arguments += @("--paths", $root)
    }
    foreach ($module in $CollectAll) {
        $arguments += @("--collect-all", $module)
    }
    foreach ($module in $CollectSubmodules) {
        $arguments += @("--collect-submodules", $module)
    }
    foreach ($module in $HiddenImports) {
        $arguments += @("--hidden-import", $module)
    }
    foreach ($entry in $AddData) {
        $arguments += @("--add-data", $entry)
    }
    foreach ($distribution in $CopyMetadata) {
        $arguments += @("--copy-metadata", $distribution)
    }
    $arguments += $EntryPoint

    $cache = Join-Path $WorkDirectory ("pyinstaller-cache\" + $Name)
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    Assert-NoReparseComponents $cache "PyInstaller cache" | Out-Null
    Start-Job -ArgumentList $Python, $Name, $cache, $arguments -ScriptBlock {
        param($Python, $Name, $Cache, $Arguments)
        # Each Host owns a persistent cache. A second build using the same work
        # root waits rather than concurrently writing PyInstaller's binary cache.
        $lock = $null
        $deadline = [DateTime]::UtcNow.AddMinutes(10)
        try {
            while ($null -eq $lock) {
                try {
                    $lock = [IO.File]::Open(($Cache + ".lock"), 'OpenOrCreate', 'ReadWrite', 'None')
                } catch [IO.IOException] {
                    if ([DateTime]::UtcNow -gt $deadline) { throw }
                    Start-Sleep -Milliseconds 200
                }
            }
            $env:PYINSTALLER_CONFIG_DIR = $Cache
            $timer = [Diagnostics.Stopwatch]::StartNew()
            # PowerShell 5 treats native stderr as ErrorRecords. Preserve the
            # diagnostics; the process exit code decides whether freezing failed.
            $ErrorActionPreference = 'Continue'
            & $Python @Arguments 2>&1 | ForEach-Object { "$_" }
            if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed while building $Name (exit $LASTEXITCODE)." }
            Write-Output ("PyInstaller {0}: {1:N1}s" -f $Name, $timer.Elapsed.TotalSeconds)
        } finally {
            if ($null -ne $lock) { $lock.Dispose() }
        }
    }
}

function Remove-ControlledBuildSession {
    param(
        [Parameter(Mandatory = $true)][string]$WorkRoot,
        [Parameter(Mandatory = $true)][string]$SessionRoot,
        [Parameter(Mandatory = $true)][string]$SessionId
    )
    try {
        $safeSession = Assert-PathWithin $WorkRoot $SessionRoot "PyInstaller session"
        Assert-NoReparseComponents $safeSession "PyInstaller session" | Out-Null
        $markerPath = Assert-RegularFile (Join-Path $safeSession $buildSessionMarkerName) `
            "PyInstaller session marker"
        $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
        if ([string]$marker.schema -ne $buildSessionSchema -or
            [string]$marker.sessionId -ne $SessionId) {
            throw "PyInstaller session marker does not authorise cleanup: $safeSession"
        }
        $reparseItems = @(Get-ChildItem -LiteralPath $safeSession -Recurse -Force |
            Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 })
        if ($reparseItems.Count -gt 0) {
            throw "PyInstaller session contains a reparse point; preserving it for review: $safeSession"
        }
        Remove-Item -LiteralPath $safeSession -Recurse -Force
    }
    catch {
        Write-Warning "Build work was preserved because safe cleanup could not be proven: $($_.Exception.Message)"
    }
}

$packagingRoot = Get-NormalizedPath $PSScriptRoot
$replayLabRoot = Get-NormalizedPath (Split-Path -Parent $packagingRoot)
$repositoryRoot = Get-NormalizedPath (Split-Path -Parent $replayLabRoot)
$skillRoot = Join-Path $repositoryRoot ".agents\skills"
$agentSource = Join-Path $replayLabRoot "src"
$mcpRoot = Join-Path $replayLabRoot "mcp_host"
$agentEntry = Join-Path $packagingRoot "agent_host_entry.py"
$internalEntry = Join-Path $packagingRoot "internal_host_entry.py"
$mcpEntry = Join-Path $packagingRoot "mcp_host_entry.py"
$shaderToolsEntry = Join-Path $packagingRoot "shader_tools_entry.py"
$hostRuntime = Join-Path $packagingRoot "host_runtime.py"
$shaderToolsBuilder = Join-Path $packagingRoot "build_shader_tools.py"
$frozenMcpRouteProbe = Join-Path $packagingRoot "probe_frozen_mcp_route.py"
$frozenHostLifetimeProbe = Join-Path $packagingRoot "tests\Test-FrozenHostLifetime.ps1"
$continuationDriverSource = Join-Path $mcpRoot "src\unpacked_asset_jobs.py"
$protectedRoots = @(
    $agentSource,
    $mcpRoot,
    (Join-Path $replayLabRoot "renderdoc_mcp_bridge"),
    $packagingRoot
)

# Opt-in isolated output on the system temporary drive for low-space checkouts.
# Cleanup still operates only on marker-owned build sessions below WorkDirectory.
$generatedRoot = $replayLabRoot
if (-not [string]::IsNullOrWhiteSpace($TemporaryBuildRoot)) {
    $temporaryRoot = Get-NormalizedPath ([IO.Path]::GetTempPath())
    $generatedRoot = Get-NormalizedPath $TemporaryBuildRoot
    if ((Split-Path -Parent $generatedRoot) -ne $temporaryRoot -or
        (Split-Path -Leaf $generatedRoot) -notlike 'bread-runtime-*') {
        throw 'TemporaryBuildRoot must be a named bread-runtime-* directory directly under system temp.'
    }
    Assert-NoReparseComponents $generatedRoot "TemporaryBuildRoot" | Out-Null
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = if ($PortableHosts) {
        Join-Path $replayLabRoot "output\portable-runtime"
    } else {
        Join-Path $replayLabRoot "output\replaylab"
    }
}
if ([string]::IsNullOrWhiteSpace($WorkDirectory)) {
    $WorkDirectory = Join-Path $replayLabRoot "output\.pyinstaller-work"
}
$OutputDirectory = Assert-GeneratedDirectory $generatedRoot $OutputDirectory `
    "OutputDirectory" $protectedRoots
$WorkDirectory = Assert-GeneratedDirectory $generatedRoot $WorkDirectory `
    "WorkDirectory" $protectedRoots
if ((Test-PathWithin $OutputDirectory $WorkDirectory -AllowSame) -or
    (Test-PathWithin $WorkDirectory $OutputDirectory -AllowSame)) {
    throw "OutputDirectory and WorkDirectory must not overlap."
}

foreach ($sourceFile in @(
    (Join-Path $replayLabRoot "pyproject.toml"),
    (Join-Path $mcpRoot "pyproject.toml"),
    (Join-Path $agentSource "replaylab_agent\cli.py"),
    (Join-Path $mcpRoot "src\internal_host.py"),
    (Join-Path $mcpRoot "src\internal_tool_service.py"),
    (Join-Path $mcpRoot "src\event_flow_artifacts.py"),
    (Join-Path $mcpRoot "src\event_flow_diagram.py"),
    (Join-Path $mcpRoot "src\frame_render_flow.py"),
    (Join-Path $mcpRoot "src\asset_catalog.py"),
    (Join-Path $mcpRoot "src\resource_export.py"),
    (Join-Path $mcpRoot "src\unity_integration.py"),
    (Join-Path $mcpRoot "src\unity_restore.py"),
    (Join-Path $replayLabRoot "unity\com.bread.unity-connection\package.json"),
    (Join-Path $replayLabRoot "unity\com.bread.unity-connection\Editor\BreadUnityConnection.cs"),
    (Join-Path $replayLabRoot "unity\com.bread.unity-connection\Editor\Bread.UnityConnection.Editor.asmdef"),
    (Join-Path $replayLabRoot "unity\com.bread.renderdoc\package.json"),
    (Join-Path $replayLabRoot "unity\com.bread.renderdoc\Editor\BreadCaptureImporter.cs"),
    (Join-Path $replayLabRoot "unity\com.bread.renderdoc\Editor\Bread.Capture.Editor.asmdef"),
    $continuationDriverSource,
    (Join-Path $mcpRoot "src\replaylab_host.py"),
    $agentEntry,
    $internalEntry,
    $mcpEntry,
    $shaderToolsEntry,
    $shaderToolsBuilder,
    $hostRuntime,
    $frozenMcpRouteProbe,
    $frozenHostLifetimeProbe
)) {
    Assert-RegularFile $sourceFile "ReplayLab runtime packaging source" | Out-Null
}
# Every Skill folder in the repository ships, not a list kept by hand: a Skill
# added under .agents/skills used to be validated by the tests, offered by the
# source tree, and silently absent from the frozen runtime (5 of 10 shipped).
$bundledSkillFolders = @(Get-ChildItem -LiteralPath $skillRoot -Directory |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "SKILL.md") -PathType Leaf } |
    Sort-Object -Property Name)
if ($bundledSkillFolders.Count -lt 1) {
    throw "No Skill folders with SKILL.md were found under $skillRoot."
}
if (-not ($bundledSkillFolders | Where-Object { $_.Name -eq "replaylab-core" })) {
    throw "replaylab-core (the shared acceptance contract) is missing under $skillRoot."
}
foreach ($skillFolder in $bundledSkillFolders) {
    Assert-RegularFile (Join-Path $skillFolder.FullName "SKILL.md") "ReplayLab Skill definition" | Out-Null
}
Write-Host ("ReplayLab: bundling " + $bundledSkillFolders.Count + " Skill folder(s): " +
    (($bundledSkillFolders | ForEach-Object { $_.Name }) -join ", "))
if (-not $PortableHosts) { Assert-OutputDirectorySafe $OutputDirectory $ownedRuntimeFiles }

if ($ValidateOnly) {
    [pscustomobject]@{
        Status = "valid"
        OutputDirectory = $OutputDirectory
        WorkDirectory = $WorkDirectory
        AgentEntry = $agentEntry
        InternalEntry = $internalEntry
        McpEntry = $mcpEntry
        ShaderToolsEntry = $shaderToolsEntry
    }
    return
}

if ($env:OS -ne "Windows_NT") {
    throw "ReplayLab runtime packaging currently produces Windows executables and must run on Windows."
}
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $PythonExecutable = Join-Path $replayLabRoot ".venv\Scripts\python.exe"
}
$PythonExecutable = Resolve-PythonExecutable $PythonExecutable
& $PythonExecutable (Join-Path $PSScriptRoot 'version_policy.py') --check
if ($LASTEXITCODE -ne 0) { throw 'Bread release metadata is stale; run version_policy.py --write.' }
$probe = Get-BuildProbe $PythonExecutable $agentSource $mcpRoot
Assert-BuildProbe $probe $replayLabRoot $mcpRoot
$sourceInputsSha256 = & $PythonExecutable (Join-Path $packagingRoot "runtime_inputs.py")
if ($LASTEXITCODE -ne 0 -or $sourceInputsSha256 -notmatch '^[0-9a-f]{64}$') { throw "Cannot identify runtime source inputs." }

$sessionId = [Guid]::NewGuid().ToString("N")
$sessionRoot = Join-Path $WorkDirectory ("session-" + $sessionId)
if (-not (Test-Path -LiteralPath $WorkDirectory)) {
    New-Item -ItemType Directory -Path $WorkDirectory | Out-Null
}
Assert-NoReparseComponents $WorkDirectory "WorkDirectory" | Out-Null
New-Item -ItemType Directory -Path $sessionRoot -Force:$false | Out-Null
Assert-NoReparseComponents $sessionRoot "PyInstaller session" | Out-Null
@{
    schema = $buildSessionSchema
    sessionId = $sessionId
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $sessionRoot $buildSessionMarkerName) `
    -Encoding UTF8

try {
    $agentDist = Join-Path $sessionRoot "agent\dist"
    $agentBuild = Join-Path $sessionRoot "agent\build"
    $agentSpec = Join-Path $sessionRoot "agent\spec"
    $mcpDist = Join-Path $sessionRoot "mcp\dist"
    $mcpBuild = Join-Path $sessionRoot "mcp\build"
    $mcpSpec = Join-Path $sessionRoot "mcp\spec"
    $internalDist = Join-Path $sessionRoot "internal\dist"
    $internalBuild = Join-Path $sessionRoot "internal\build"
    $internalSpec = Join-Path $sessionRoot "internal\spec"
    New-Item -ItemType Directory -Path `
        $agentDist, $agentBuild, $agentSpec, `
        $internalDist, $internalBuild, $internalSpec, `
        $mcpDist, $mcpBuild, $mcpSpec | Out-Null

    # The runner owns only its static adapter closure. Its builder hashes all
    # source/interpreter/freezer inputs and reuses the complete EXE unchanged
    # when unrelated Host/Agent/UI code is rebuilt.
    $shaderToolsWork = Join-Path $sessionRoot "shader-tools"
    $builtShaderTools = Join-Path $shaderToolsWork "result\ReplayLabShaderTools.exe"
    $shaderToolsCache = Join-Path $WorkDirectory "shader-tools-cache"
    $shaderToolsBuildOutput = & $PythonExecutable -I $shaderToolsBuilder `
        --source-root $mcpRoot --entry $shaderToolsEntry `
        --cache-root $shaderToolsCache --work-root $shaderToolsWork `
        --output $builtShaderTools
    if ($LASTEXITCODE -ne 0) {
        throw "Independent ReplayLabShaderTools build/cache verification failed."
    }
    $shaderToolsBuild = (($shaderToolsBuildOutput | Out-String) | ConvertFrom-Json)
    $builtShaderTools = Assert-RegularFile $builtShaderTools "built shader tool runner"
    if ([string]$shaderToolsBuild.schema -cne "replaylab-shader-tools-cache/v1" -or
        [string]$shaderToolsBuild.executableSha256 -cne (Get-Sha256Hex $builtShaderTools)) {
        throw "Independent shader tool builder returned inconsistent artifact evidence."
    }
    & $builtShaderTools --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged ReplayLabShaderTools.exe failed its --help smoke test."
    }
    foreach ($adapterCommand in @(
        "adapter-hlsl", "adapter-dxbc-assembly", "adapter-d3dcompile",
        "adapter-dxil-assemble", "adapter-spirv-cross", "adapter-renodx")) {
        & $builtShaderTools $adapterCommand --help | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "The independent shader adapter '$adapterCommand' failed its --help smoke test."
        }
    }
    foreach ($hostDist in @($agentDist, $mcpDist, $internalDist)) {
        if ($PortableHosts) { break }
        Copy-Item -LiteralPath $builtShaderTools `
            -Destination (Join-Path $hostDist "ReplayLabShaderTools.exe")
    }

    # Model API profiles always use deepagents, and subscription profiles use
    # ACP/Codex directly. These are product dependencies, not optional extras.
    # LangChain resolves provider adapters dynamically, so collect every
    # supported adapter and preserve its distribution metadata explicitly.
    $deepModules = @($probe.deepModules)
    $agentCollectAll = @("mcp", "genai_prices") + $deepModules
    # The bundled Unity reader (``replaylab-ai unpacker unitypy``) needs UnityPy
    # inside the frozen AgentHost. A release must not silently lose Unity support.
    & $PythonExecutable -c "import UnityPy" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $agentCollectAll += @("UnityPy")
        Write-Host "  UnityPy found in the build environment: bundling the embedded Unity reader"
    } else {
        throw "UnityPy is required for a complete Bread runtime. Install the pinned bundle dependencies before building."
    }
    $agentCopyMetadata = @(
        "genai-prices",
        "mcp",
        "agent-client-protocol",
        "deepagents",
        "langchain",
        "langchain-core",
        "langchain-openai",
        "langchain-anthropic",
        "langchain-google-genai",
        "langchain-google-vertexai",
        "langgraph",
        "langgraph-checkpoint",
        "langgraph-prebuilt",
        "pydantic"
    )
    Write-Host ("ReplayLab: bundling the required agent runtime (" + ($deepModules -join ", ") + ")")
    # One data entry per Skill folder (SKILL.md and its references travel
    # together); replaylab-core is among them and is asserted above.
    $agentSkillData = @($bundledSkillFolders | ForEach-Object {
        $_.FullName + ":.agents/skills/" + $_.Name
    })
    $mcpProviderData = @(
        ($continuationDriverSource + ":replaylab_helpers"),
        ((Join-Path $replayLabRoot "unity") + ":bread_unity"),
        ((Join-Path $mcpRoot "src/unreal_editor_script.py") + ":bread_unreal"),
        ((Join-Path $mcpRoot "src/unreal_live_editor.py") + ":bread_unreal")
    )
    $agentData = @($agentSkillData) + @($mcpProviderData)
    $agentData += ((Join-Path $replayLabRoot "pyproject.toml") + ":replaylab_agent")
    $freezeJobs = @()
    $freezeTimer = [Diagnostics.Stopwatch]::StartNew()
    try {
    $freezeJobs += Invoke-PyInstallerBuild $PythonExecutable "replaylab-ai" $agentEntry $agentSource `
        $agentDist $agentBuild $agentSpec `
        -AdditionalImportRoots @($mcpRoot) `
        -CollectAll $agentCollectAll `
        -CollectSubmodules @("replaylab_agent", "langgraph", "acp", "src") `
        -HiddenImports @(
            "replaylab_agent.mcp_proxy",
            "replaylab_agent.selftest",
            "anyio",
            "anyio.streams.text",
            "httpx",
            "mcp.types",
            "mcp.client.stdio",
            "mcp.shared.message",
            "deepagents",
            "langchain_core.tools"
        ) `
        -AddData @($agentData) `
        -CopyMetadata $agentCopyMetadata -SharedSupport:$PortableHosts

    $freezeJobs += Invoke-PyInstallerBuild $PythonExecutable "BreadMCP" $mcpEntry $mcpRoot `
        $mcpDist $mcpBuild $mcpSpec `
        -CollectAll @("mcp") `
        -CollectSubmodules @("src", "uvicorn", "starlette") `
        -AddData @($mcpProviderData) -SharedSupport:$PortableHosts

    $freezeJobs += Invoke-PyInstallerBuild $PythonExecutable "ReplayLabInternal" $internalEntry $mcpRoot `
        $internalDist $internalBuild $internalSpec `
        -CollectSubmodules @("src", "uvicorn", "starlette") `
        -AddData @($mcpProviderData) -SharedSupport:$PortableHosts

    $freezeJobs | Wait-Job | Out-Null
    foreach ($job in $freezeJobs) {
        Receive-Job $job -ErrorAction Continue | ForEach-Object { Write-Host $_ }
    }
    if (@($freezeJobs | Where-Object { $_.State -ne 'Completed' }).Count) {
        throw 'One or more parallel PyInstaller builds failed; no runtime was published.'
    }
    Write-Host ("Parallel Host freezing: {0:N1}s" -f $freezeTimer.Elapsed.TotalSeconds)
    } finally {
        foreach ($job in $freezeJobs) {
            if ($job.State -eq 'Running') { Stop-Job $job }
            Remove-Job $job -Force
        }
    }

    if ($PortableHosts) {
        $portableDist = Join-Path $sessionRoot "portable"
        New-Item -ItemType Directory -Path $portableDist | Out-Null
        $portableAgent = Join-Path $agentDist "replaylab-ai"
        $portableMcp = Join-Path $mcpDist "BreadMCP"
        $portableInternal = Join-Path $internalDist "ReplayLabInternal"
        & $PythonExecutable $hostRuntime merge (Join-Path $portableDist $supportDirectoryName) `
            $portableAgent $portableMcp $portableInternal
        if ($LASTEXITCODE -ne 0) { throw "Frozen Hosts could not share their support files safely." }
        foreach ($hostDir in @($portableAgent, $portableMcp, $portableInternal)) {
            $hostName = (Split-Path -Leaf $hostDir) + ".exe"
            Copy-Item -LiteralPath (Join-Path $hostDir $hostName) -Destination $portableDist
        }
        Copy-Item -LiteralPath $builtShaderTools -Destination $portableDist
        $agentDist = $mcpDist = $internalDist = $portableDist
    }

    $builtAgent = Assert-RegularFile (Join-Path $agentDist "replaylab-ai.exe") `
        "built AgentHost executable"
    $builtMcp = Assert-RegularFile (Join-Path $mcpDist "BreadMCP.exe") `
        "built MCP Host executable"
    $builtInternal = Assert-RegularFile `
        (Join-Path $internalDist "ReplayLabInternal.exe") `
        "built internal Host executable"

    # Verify the actual embedded bytecode independently of PyInstaller's cache.
    $agentPackage = Join-Path $agentSource "replaylab_agent"
    $mcpPackage = Join-Path $mcpRoot "src"
    & $PythonExecutable $hostRuntime verify-sources $builtAgent $agentEntry $agentPackage $mcpPackage
    if ($LASTEXITCODE -ne 0) { throw "Frozen AgentHost source verification failed." }
    & $PythonExecutable $hostRuntime verify-sources $builtMcp $mcpEntry $mcpPackage
    if ($LASTEXITCODE -ne 0) { throw "Frozen MCP Host source verification failed." }
    & $PythonExecutable $hostRuntime verify-sources $builtInternal $internalEntry $mcpPackage
    if ($LASTEXITCODE -ne 0) { throw "Frozen Internal Host source verification failed." }

    & $builtAgent --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged replaylab-ai.exe failed its --help smoke test."
    }
    & $builtAgent -I -m replaylab_agent.mcp_proxy --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged replaylab-ai.exe failed its frozen MCP proxy dispatch smoke test."
    }
    & $builtAgent -I -m replaylab_agent.selftest --external-mcp-import-smoke | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw ("The packaged replaylab-ai.exe failed its external MCP import smoke test " +
            "with diagnostic exit code $LASTEXITCODE.")
    }
    & $builtAgent -I -m replaylab_agent.selftest --acp-chat-smoke | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged replaylab-ai.exe failed its ACP streaming chat/extension smoke test."
    }
    & $builtMcp --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged BreadMCP.exe failed its --help smoke test."
    }
    & $PythonExecutable $frozenMcpRouteProbe --agent $builtAgent `
        --host $builtMcp --timeout 60 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged AgentHost -> MCP filter -> BreadMCP route failed its handshake/catalog smoke test."
    }
    & $builtInternal --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged ReplayLabInternal.exe failed its --help smoke test."
    }
    & $frozenHostLifetimeProbe -InternalHost $builtInternal -McpHost $builtMcp `
        -OutputDirectory (Join-Path $sessionRoot "detached-host-lifetime") | Out-Null

    $smokeProfilePath = Join-Path $sessionRoot "agent-smoke-profiles.json"
    $smokeProfileJson = [ordered]@{
        schema = "replaylab-ai-profiles/v2"
        activeProfile = "packaging-smoke"
        profiles = @(
            [ordered]@{
                id = "packaging-smoke"
                preset = "custom-acp"
                model = "gpt-5.6-sol"
                models = @("gpt-5.6-sol")
                default_model = "gpt-5.6-sol"
                command = @($builtAgent, "--help")
                options = [ordered]@{ allow_unsandboxed_agent = $true }
            }
            [ordered]@{
                id = "packaging-cursor-smoke"
                preset = "cursor-acp"
                model = "composer-2.5[fast=true]"
                models = @("composer-2.5[fast=true]")
                default_model = "composer-2.5[fast=true]"
                options = [ordered]@{ allow_unsandboxed_agent = $true }
            }
        )
    } | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText(
        $smokeProfilePath,
        $smokeProfileJson + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false))
    # An empty tools config: the smoke test counts the bundled Skills alone, not
    # whatever the build machine's user has in its own Skill folders.
    $smokeToolsPath = Join-Path (Split-Path -Parent $smokeProfilePath) "packaging-smoke-tools.json"
    [IO.File]::WriteAllText(
        $smokeToolsPath,
        '{"schema":"replaylab-agent-tools/v1","skillRoots":[],"disabledSkills":[],"mcpServers":[]}' +
            [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false))
    $doctorOutput = & $builtAgent --config $smokeProfilePath doctor `
        --profile "packaging-smoke" --mcp-host $builtMcp --tools-config $smokeToolsPath --json
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged replaylab-ai.exe failed its frozen catalog/Skill doctor smoke test."
    }
    try {
        $parsedDoctorChecks = (($doctorOutput | Out-String) | ConvertFrom-Json)
        $doctorChecks = @($parsedDoctorChecks.GetEnumerator())
    }
    catch {
        throw "The packaged replaylab-ai.exe doctor smoke test returned invalid JSON."
    }
    $skillCheck = @($doctorChecks | Where-Object { $_.name -eq "skills" })
    $expectedSkillDetail = "$($agentSkillData.Count) validated skill(s)"
    if ($skillCheck.Count -ne 1 -or [string]$skillCheck[0].status -ne "ok" -or
        [string]$skillCheck[0].detail -ne $expectedSkillDetail) {
        $observedSkillCheck = $skillCheck | ConvertTo-Json -Depth 4 -Compress
        throw ("The packaged replaylab-ai.exe did not load exactly all " +
            "$($agentSkillData.Count) bundled ReplayLab Skills. " +
            "Observed: $observedSkillCheck")
    }
    $contextCheck = @($doctorChecks | Where-Object { $_.name -eq "model-context" })
    if ($contextCheck.Count -ne 1 -or [string]$contextCheck[0].status -ne "ok" -or
        [string]$contextCheck[0].detail -notmatch '^1050000 tokens') {
        throw "The packaged replaylab-ai.exe could not resolve the verified GPT-5.6 Sol context window."
    }

    # Cursor's catalog command is immutable, so give doctor a real, disposable
    # executable under that audited name. The smoke does not launch it.
    $cursorAgentSmoke = Join-Path $sessionRoot "cursor-agent.exe"
    Copy-Item -LiteralPath $builtAgent -Destination $cursorAgentSmoke
    $originalProcessPath = [Environment]::GetEnvironmentVariable("PATH", "Process")
    try {
        $env:PATH = $sessionRoot + [IO.Path]::PathSeparator + $originalProcessPath
        $cursorDoctorOutput = & $builtAgent --config $smokeProfilePath doctor `
            --profile "packaging-cursor-smoke" --mcp-host $builtMcp --json
        $cursorDoctorExitCode = $LASTEXITCODE
    }
    finally {
        [Environment]::SetEnvironmentVariable("PATH", $originalProcessPath, "Process")
    }
    if ($cursorDoctorExitCode -ne 0) {
        throw "The packaged replaylab-ai.exe failed its frozen Cursor catalog doctor smoke test."
    }
    try {
        $parsedCursorDoctorChecks = (
            ($cursorDoctorOutput | Out-String) | ConvertFrom-Json)
        $cursorDoctorChecks = @($parsedCursorDoctorChecks.GetEnumerator())
    }
    catch {
        throw "The packaged replaylab-ai.exe Cursor doctor smoke test returned invalid JSON."
    }
    $cursorContextCheck = @(
        $cursorDoctorChecks | Where-Object { $_.name -eq "model-context" })
    if ($cursorContextCheck.Count -ne 1 -or
        [string]$cursorContextCheck[0].status -ne "ok" -or
        [string]$cursorContextCheck[0].detail -ne "200000 tokens (model-catalog)") {
        throw "The packaged replaylab-ai.exe could not resolve the verified Cursor Composer context window."
    }

    $finalSourceInputsSha256 = & $PythonExecutable (Join-Path $packagingRoot "runtime_inputs.py")
    if ($LASTEXITCODE -ne 0 -or $finalSourceInputsSha256 -cne $sourceInputsSha256) {
        throw "Runtime source inputs changed during freezing. Build again from a stable checkout."
    }
    $runtimeStage = $OutputDirectory
    if ($PortableHosts) {
        $runtimeStage = $portableDist
    } else {
        if (-not (Test-Path -LiteralPath $runtimeStage)) {
            New-Item -ItemType Directory -Path $runtimeStage | Out-Null
        }
        Assert-NoReparseComponents $runtimeStage "OutputDirectory" | Out-Null
        Assert-OutputDirectorySafe $runtimeStage $ownedRuntimeFiles

        Copy-Item -LiteralPath $builtAgent `
            -Destination (Join-Path $runtimeStage "replaylab-ai.exe") -Force
        Copy-Item -LiteralPath $builtMcp `
            -Destination (Join-Path $runtimeStage "BreadMCP.exe") -Force
        Copy-Item -LiteralPath $builtInternal `
            -Destination (Join-Path $runtimeStage "ReplayLabInternal.exe") -Force
        Copy-Item -LiteralPath $builtShaderTools `
            -Destination (Join-Path $runtimeStage "ReplayLabShaderTools.exe") -Force
    }

    $fileRecords = @()
    foreach ($name in @(
        "replaylab-ai.exe", "ReplayLabInternal.exe", "BreadMCP.exe",
        "ReplayLabShaderTools.exe")) {
        $path = Assert-RegularFile (Join-Path $runtimeStage $name) `
            "packaged ReplayLab runtime file '$name'"
        $file = Get-Item -LiteralPath $path
        $fileRecords += [ordered]@{
            path = $name
            size = [int64]$file.Length
            # Avoid a dependency on Get-FileHash: some MSBuild post-build
            # PowerShell hosts do not expose the Utility module.
            sha256 = Get-Sha256Hex $path
        }
    }
    $manifest = [ordered]@{
        schema = $runtimeManifestSchema
        version = [string]$probe.agentVersion
        generatedUtc = [DateTime]::UtcNow.ToString("o")
        sourceInputsSha256 = [string]$sourceInputsSha256
        python = (@($probe.python) -join ".")
        pyinstaller = [string]$probe.pyinstaller
        dependencies = @{ deepagents = [string]$probe.deepagentsVersion }
        shaderTools = $shaderToolsBuild
        ownedFiles = $ownedRuntimeFiles
        files = $fileRecords
    }
    if ($PortableHosts) {
        $support = & $PythonExecutable $hostRuntime records $portableDist
        if ($LASTEXITCODE -ne 0) { throw "Could not inventory frozen Host support files." }
        $manifest["layout"] = "onedir"
        $manifest["files"] += [object[]](ConvertFrom-Json -InputObject ($support -join "`n"))
    }
    $stagedManifest = Join-Path $sessionRoot $runtimeManifestName
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $stagedManifest -Encoding UTF8
    Copy-Item -LiteralPath $stagedManifest `
        -Destination (Join-Path $runtimeStage $runtimeManifestName) -Force
    if ($PortableHosts) {
        & $PythonExecutable $hostRuntime publish $portableDist $OutputDirectory
        if ($LASTEXITCODE -ne 0) { throw "Could not publish the verified portable runtime directory." }
    }

    Write-Host "ReplayLab runtime $($probe.agentVersion) created at $OutputDirectory"
}
finally {
    if ($KeepBuildWork) {
        Write-Host "PyInstaller work retained at $sessionRoot"
    }
    else {
        Remove-ControlledBuildSession $WorkDirectory $sessionRoot $sessionId
    }
}
