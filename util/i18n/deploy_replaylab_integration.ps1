[CmdletBinding()]
param(
    [ValidateSet("Development", "Release")]
    [string]$Configuration = "Development",
    [string]$SourceRoot = "",
    [string]$TargetRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-ReplayLabDeploymentSha256 {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    # Windows PowerShell launched by an embedded host can inherit a PSModulePath
    # without Microsoft.PowerShell.Utility. Deployment must not depend on its
    # Get-FileHash cmdlet being auto-loadable.
    $stream = [IO.File]::OpenRead([IO.Path]::GetFullPath($LiteralPath))
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '') }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}

function Get-ReplayLabRelativePath {
    param([string]$BasePath, [string]$TargetPath)
    # Uri.MakeRelativeUri is available in Windows PowerShell 5 and avoids the
    # PowerShell 7-only Path.GetRelativePath API.
    $baseFull = [IO.Path]::GetFullPath($BasePath).TrimEnd('\') + '\'
    $targetFull = [IO.Path]::GetFullPath($TargetPath)
    $baseUri = New-Object Uri($baseFull)
    $targetUri = New-Object Uri($targetFull)
    return [Uri]::UnescapeDataString(
        $baseUri.MakeRelativeUri($targetUri).ToString()).Replace('/', '\')
}

function Assert-ReplayLabChildPath {
    param([string]$BasePath, [string]$TargetPath)
    $baseFull = [IO.Path]::GetFullPath($BasePath).TrimEnd('\') + '\'
    $targetFull = [IO.Path]::GetFullPath($TargetPath)
    if (-not $targetFull.StartsWith($baseFull, [StringComparison]::OrdinalIgnoreCase))
    {
        throw "Refusing to use a path outside the ReplayLab integration root: $targetFull"
    }
    return $targetFull
}

function Assert-ReplayLabNoReparseComponents {
    param(
        [Parameter(Mandatory = $true)][string]$AbsolutePath,
        [Parameter(Mandatory = $true)][string]$Role
    )
    $fullPath = [IO.Path]::GetFullPath($AbsolutePath)
    $root = [IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($root))
    {
        throw "$Role must have a filesystem root: $AbsolutePath"
    }
    $components = $fullPath.Substring($root.Length).Split(
        [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar),
        [StringSplitOptions]::RemoveEmptyEntries)
    $current = $root
    foreach ($componentName in $components)
    {
        $current = Join-Path $current $componentName
        if (-not (Test-Path -LiteralPath $current))
        {
            continue
        }
        $component = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($component.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        {
            throw "$Role must not pass through a symlink, junction, or other reparse point: $current"
        }
    }
    return $fullPath
}

function Get-ReplayLabIntegrationSourceFiles {
    param([Parameter(Mandatory = $true)][string]$Root)
    $safeRoot = Assert-ReplayLabNoReparseComponents $Root "ReplayLab integration source"
    $seen = @{}
    $files = @()
    foreach ($sourceFile in Get-ChildItem -LiteralPath $safeRoot -File -Recurse -Force)
    {
        $relativePath = Get-ReplayLabRelativePath $safeRoot $sourceFile.FullName
        $segments = @($relativePath.Split(
            [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar),
            [StringSplitOptions]::RemoveEmptyEntries))
        if ($segments -contains "__pycache__")
        {
            continue
        }
        $isPython = $sourceFile.Extension -eq ".py"
        $isRootMetadata = $relativePath -in @("integration.json", "project_permissions.ps1")
        if (-not ($isPython -or $isRootMetadata))
        {
            continue
        }
        if ([IO.Path]::IsPathRooted($relativePath) -or
            @($segments | Where-Object { $_ -eq "." -or $_ -eq ".." }).Count -gt 0)
        {
            throw "Unsafe ReplayLab integration source path: $relativePath"
        }
        $safeSource = Assert-ReplayLabChildPath $safeRoot $sourceFile.FullName
        Assert-ReplayLabNoReparseComponents $safeSource (
            "ReplayLab integration source '$relativePath'") | Out-Null
        $key = $relativePath.ToLowerInvariant()
        if ($seen.ContainsKey($key))
        {
            throw "Duplicate ReplayLab integration source path: $relativePath"
        }
        $seen[$key] = $true
        $files += [pscustomobject]@{
            SourcePath = $safeSource
            RelativePath = $relativePath
        }
    }
    return @($files | Sort-Object RelativePath)
}

function Remove-ReplayLabStaleTargetEntries {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][array]$SourceFiles
    )
    $safeRoot = Assert-ReplayLabNoReparseComponents $Root (
        "ReplayLab integration deployment target")
    $expected = @{}
    foreach ($sourceFile in $SourceFiles)
    {
        $expected[$sourceFile.RelativePath.ToLowerInvariant()] = $true
    }

    foreach ($targetFile in Get-ChildItem -LiteralPath $safeRoot -File -Recurse -Force)
    {
        $relativePath = Get-ReplayLabRelativePath $safeRoot $targetFile.FullName
        if ($expected.ContainsKey($relativePath.ToLowerInvariant()))
        {
            continue
        }
        $stalePath = Assert-ReplayLabChildPath $safeRoot $targetFile.FullName
        Assert-ReplayLabNoReparseComponents $stalePath (
            "stale ReplayLab integration file '$relativePath'") | Out-Null
        Remove-Item -LiteralPath $stalePath -Force
    }

    $targetDirectories = @(Get-ChildItem -LiteralPath $safeRoot -Directory -Recurse -Force |
        Sort-Object { $_.FullName.Length } -Descending)
    foreach ($targetDirectory in $targetDirectories)
    {
        $relativePath = Get-ReplayLabRelativePath $safeRoot $targetDirectory.FullName
        $safeDirectory = Assert-ReplayLabChildPath $safeRoot $targetDirectory.FullName
        Assert-ReplayLabNoReparseComponents $safeDirectory (
            "ReplayLab integration deployment directory '$relativePath'") | Out-Null
        if (@(Get-ChildItem -LiteralPath $safeDirectory -Force).Count -eq 0)
        {
            Remove-Item -LiteralPath $safeDirectory -Force
        }
    }
}

function Assert-ReplayLabIntegrationTargetStopped {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)

    $target = [IO.Path]::GetFullPath($TargetRoot).TrimEnd('\', '/')
    $suffix = '\integrations\replaylab\python\renderdoc_mcp_bridge'
    if (-not $target.EndsWith($suffix, [StringComparison]::OrdinalIgnoreCase))
    {
        throw "Cannot determine the qrenderdoc application directory from the fixed integration target: $target"
    }
    $applicationRoot = $target.Substring(0, $target.Length - $suffix.Length)
    Assert-ReplayLabNoReparseComponents $target "ReplayLab integration deployment target" | Out-Null
    $running = @(Get-Process -ErrorAction Stop | Where-Object {
        $_.ProcessName -in @("qTinecmaTool", "RenderDocBread", "qrenderdoc", "q_render_soco_doc")
    })
    foreach ($candidate in $running)
    {
        try
        {
            $executablePath = [string]$candidate.Path
            if ([string]::IsNullOrWhiteSpace($executablePath) -or
                -not [IO.Path]::IsPathRooted($executablePath))
            {
                throw "The process executable path is unavailable."
            }
            $processDirectory = [IO.Path]::GetDirectoryName(
                [IO.Path]::GetFullPath($executablePath)).TrimEnd('\', '/')
        }
        catch
        {
            throw "Cannot determine the application directory of running qrenderdoc PID $($candidate.Id). No target files were changed."
        }
        if ($processDirectory.Equals($applicationRoot, [StringComparison]::OrdinalIgnoreCase))
        {
            throw "Close qrenderdoc running from the target application directory before deploying its built-in ReplayLab integration. PID: $($candidate.Id); path: $executablePath. No target files were changed."
        }
    }
}

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "../..")).Path
if ([string]::IsNullOrWhiteSpace($SourceRoot))
{
    $SourceRoot = Join-Path $repositoryRoot "replaylab/renderdoc_mcp_bridge"
}
$SourceRoot = [System.IO.Path]::GetFullPath($SourceRoot)
if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container))
{
    throw "Canonical ReplayLab built-in integration source is unavailable: $SourceRoot"
}

$requiredFiles = @(
    "__init__.py",
    "ai_context.py",
    "cbuffer_alias_identity.py",
    "dxil_access_signature.py",
    "dxbc_cbuffer_layout.py",
    "project_watch.py",
    "project_permissions.py",
    "project_permissions.ps1",
    "material_panel.py",
    "project_pass_panel.py",
    "project_launcher.py",
    "project_restore_client.py",
    "project_shader_flow_graph.py",
    "project_shader_workspace.py",
    "shader_console.py",
    "shader_editor_client.py",
    "shader_validation.py",
    "integration.json"
)
foreach ($requiredFile in $requiredFiles)
{
    $requiredPath = Join-Path $SourceRoot $requiredFile
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf))
    {
        throw "Required ReplayLab integration source is missing: $requiredPath"
    }
}

$sourceFiles = @(Get-ReplayLabIntegrationSourceFiles $SourceRoot)
if ($sourceFiles.Count -lt $requiredFiles.Count)
{
    throw "ReplayLab integration source file discovery returned an incomplete set."
}

if ([string]::IsNullOrWhiteSpace($TargetRoot))
{
    $TargetRoot = Join-Path $repositoryRoot (
        "x64/{0}/integrations/replaylab/python/renderdoc_mcp_bridge" -f $Configuration)
}
$target = [System.IO.Path]::GetFullPath($TargetRoot)
$sourceBoundary = $SourceRoot.TrimEnd('\')
$targetBoundary = $target.TrimEnd('\')
$sourcePrefix = $sourceBoundary + '\'
$targetPrefix = $targetBoundary + '\'
if ($sourceBoundary.Equals($targetBoundary, [StringComparison]::OrdinalIgnoreCase) -or
    $sourceBoundary.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase) -or
    $targetBoundary.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase))
{
    throw "ReplayLab integration source and deployment target must be distinct, non-nested directories."
}

Assert-ReplayLabIntegrationTargetStopped $target
New-Item -ItemType Directory -Path $target -Force | Out-Null
Assert-ReplayLabNoReparseComponents $target "ReplayLab integration deployment target" | Out-Null
Remove-ReplayLabStaleTargetEntries $target $sourceFiles
foreach ($sourceFile in $sourceFiles)
{
    $deployed = Assert-ReplayLabChildPath $target (
        Join-Path $target $sourceFile.RelativePath)
    $parentDirectory = [IO.Path]::GetDirectoryName($deployed)
    if (-not $parentDirectory.Equals(
            [IO.Path]::GetFullPath($target).TrimEnd('\'),
            [StringComparison]::OrdinalIgnoreCase))
    {
        $parentDirectory = Assert-ReplayLabChildPath $target $parentDirectory
    }
    New-Item -ItemType Directory -Path $parentDirectory -Force | Out-Null
    Assert-ReplayLabNoReparseComponents $parentDirectory (
        "ReplayLab integration deployment parent '$($sourceFile.RelativePath)'") | Out-Null
    Copy-Item -LiteralPath $sourceFile.SourcePath -Destination $deployed -Force
}
foreach ($sourceFile in $sourceFiles)
{
    $deployed = Assert-ReplayLabChildPath $target (
        Join-Path $target $sourceFile.RelativePath)
    $sourceHash = Get-ReplayLabDeploymentSha256 -LiteralPath $sourceFile.SourcePath
    $targetHash = Get-ReplayLabDeploymentSha256 -LiteralPath $deployed
    if ($sourceHash -ne $targetHash)
    {
        throw "ReplayLab integration deployment hash mismatch: $deployed"
    }
}
Write-Host "Deployed and verified $($sourceFiles.Count) ReplayLab built-in integration files: $target"
