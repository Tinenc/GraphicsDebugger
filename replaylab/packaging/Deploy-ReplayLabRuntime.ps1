[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ApplicationDirectory,

    [string]$RuntimeOutputDirectory = "",

    [switch]$AdoptExistingVerifiedRuntime,

    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$runtimeManifestName = "replaylab-runtime-manifest.json"
$runtimeManifestSchema = "replaylab-runtime-output/v1"
$deploymentMarkerName = ".replaylab-runtime-deployment.json"
$deploymentMarkerSchema = "replaylab-runtime-deployment/v1"
$runtimePayloadNames = @(
    "replaylab-ai.exe", "ReplayLabInternal.exe", "BreadMCP.exe",
    "ReplayLabShaderTools.exe")
$runtimeOwnedNames = @(
    "replaylab-ai.exe", "ReplayLabInternal.exe", "BreadMCP.exe",
    "ReplayLabShaderTools.exe",
    $runtimeManifestName)
$deploymentOwnedNames = @(
    "replaylab-ai.exe",
    "ReplayLabInternal.exe",
    "BreadMCP.exe",
    "ReplayLabShaderTools.exe",
    $runtimeManifestName,
    $deploymentMarkerName
)
$qrenderdocExecutableNames = @("qTinecmaTool.exe", "RenderDocBread.exe", "qrenderdoc.exe", "q_render_soco_doc.exe")

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

    $fullPath = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($fullPath)
    if ($fullPath.Equals($root, [StringComparison]::OrdinalIgnoreCase)) {
        return $fullPath
    }
    return $fullPath.TrimEnd(
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
        [Parameter(Mandatory = $true)][string]$Candidate
    )

    $base = Get-NormalizedPath $BasePath
    $candidatePath = Get-NormalizedPath $Candidate
    $prefix = $base + [IO.Path]::DirectorySeparatorChar
    return $candidatePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparseComponents {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Role
    )

    $fullPath = [IO.Path]::GetFullPath($Path)
    $filesystemRoot = [IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($filesystemRoot)) {
        throw "$Role must have a filesystem root: $fullPath"
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

function Assert-OrdinaryDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Role
    )

    $fullPath = Assert-NoReparseComponents $Path $Role
    $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Role must be an ordinary directory: $fullPath"
    }
    return $fullPath
}

function Assert-OrdinaryFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Role
    )

    $fullPath = Assert-NoReparseComponents $Path $Role
    $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
    if ($item.PSIsContainer -or $item -isnot [IO.FileInfo] -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Role must be an ordinary file: $fullPath"
    }
    return $fullPath
}

function Get-RequiredJsonProperty {
    param(
        [Parameter(Mandatory = $true)][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Role
    )

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        throw "$Role is missing required property '$Name'."
    }
    return $property.Value
}

function Assert-ExactFileNames {
    param(
        [Parameter(Mandatory = $true)][object[]]$Values,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$Role
    )

    $actual = @($Values)
    if ($actual.Count -ne $Expected.Count) {
        throw "$Role must contain exactly: $($Expected -join ', ')."
    }
    foreach ($value in $actual) {
        if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$value) -or
            [string]$value -cne [IO.Path]::GetFileName([string]$value)) {
            throw "$Role contains an unsafe filename: $value"
        }
    }
    foreach ($expectedName in $Expected) {
        $matches = @($actual | Where-Object { [string]$_ -ceq $expectedName })
        if ($matches.Count -ne 1) {
            throw "$Role must contain '$expectedName' exactly once with canonical casing."
        }
    }
}

function Read-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Role
    )

    $safePath = Assert-OrdinaryFile $Path $Role
    try {
        return Get-Content -LiteralPath $safePath -Raw -ErrorAction Stop | ConvertFrom-Json
    }
    catch {
        throw "$Role is not valid JSON: $safePath. $($_.Exception.Message)"
    }
}

function Get-ValidatedRuntimeOutput {
    param([Parameter(Mandatory = $true)][string]$Directory)

    $sourceRoot = Assert-OrdinaryDirectory $Directory "ReplayLab runtime output directory"
    $manifestPath = Join-Path $sourceRoot $runtimeManifestName
    $manifest = Read-JsonFile $manifestPath "ReplayLab runtime manifest"

    $schema = [string](Get-RequiredJsonProperty $manifest "schema" "ReplayLab runtime manifest")
    if ($schema -cne $runtimeManifestSchema) {
        throw "ReplayLab runtime manifest schema must be '$runtimeManifestSchema'; found '$schema'."
    }

    $versionValue = Get-RequiredJsonProperty $manifest "version" "ReplayLab runtime manifest"
    if ($versionValue -isnot [string] -or
        [string]::IsNullOrWhiteSpace([string]$versionValue) -or
        [string]$versionValue -notmatch '^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$') {
        throw "ReplayLab runtime manifest has an invalid version."
    }
    $version = [string]$versionValue

    if ($manifest.PSObject.Properties["layout"] -and $manifest.layout -eq "onedir") {
        throw "Shared portable Host files are staged by Build-RenderDocBread.ps1. Build without -PortableHosts for Development deployment."
    }
    $ownedFiles = @(Get-RequiredJsonProperty $manifest "ownedFiles" "ReplayLab runtime manifest")
    Assert-ExactFileNames $ownedFiles $runtimeOwnedNames "ReplayLab runtime manifest ownedFiles"

    $fileEntries = @(Get-RequiredJsonProperty $manifest "files" "ReplayLab runtime manifest")
    if ($fileEntries.Count -ne $runtimePayloadNames.Count) {
        throw "ReplayLab runtime manifest files must describe exactly the complete runtime executable set."
    }

    $records = @{}
    foreach ($entry in $fileEntries) {
        if ($null -eq $entry) {
            throw "ReplayLab runtime manifest contains an empty file record."
        }
        $nameValue = Get-RequiredJsonProperty $entry "path" "ReplayLab runtime file record"
        if ($nameValue -isnot [string]) {
            throw "ReplayLab runtime file record path must be a string."
        }
        $name = [string]$nameValue
        if (-not ($runtimePayloadNames -ccontains $name) -or
            $name -cne [IO.Path]::GetFileName($name) -or $records.ContainsKey($name)) {
            throw "ReplayLab runtime manifest contains an unexpected or duplicate file record: $name"
        }

        $sizeValue = Get-RequiredJsonProperty $entry "size" "ReplayLab runtime file record '$name'"
        $size = [int64]0
        if (-not [int64]::TryParse([string]$sizeValue, [ref]$size) -or $size -le 0) {
            throw "ReplayLab runtime manifest contains an invalid size for '$name'."
        }

        $shaValue = Get-RequiredJsonProperty $entry "sha256" "ReplayLab runtime file record '$name'"
        if ($shaValue -isnot [string] -or [string]$shaValue -notmatch '^[0-9A-Fa-f]{64}$') {
            throw "ReplayLab runtime manifest contains an invalid SHA-256 for '$name'."
        }
        $expectedHash = ([string]$shaValue).ToLowerInvariant()
        $sourcePath = Assert-OrdinaryFile (Join-Path $sourceRoot $name) `
            "ReplayLab runtime payload '$name'"
        $sourceItem = Get-Item -LiteralPath $sourcePath -Force
        if ([int64]$sourceItem.Length -ne $size) {
            throw "ReplayLab runtime payload '$name' size does not match its manifest."
        }
        $actualHash = Get-Sha256Hex $sourcePath
        if ($actualHash -cne $expectedHash) {
            throw "ReplayLab runtime payload '$name' SHA-256 does not match its manifest."
        }

        $records[$name] = [pscustomobject]@{
            Name = $name
            Path = $sourcePath
            Size = $size
            Sha256 = $expectedHash
        }
    }
    foreach ($requiredName in $runtimePayloadNames) {
        if (-not $records.ContainsKey($requiredName)) {
            throw "ReplayLab runtime manifest does not describe '$requiredName'."
        }
    }

    return [pscustomobject]@{
        Root = $sourceRoot
        ManifestPath = Assert-OrdinaryFile $manifestPath "ReplayLab runtime manifest"
        ManifestSha256 = Get-Sha256Hex $manifestPath
        Version = $version
        Records = $records
    }
}

function Assert-DeploymentMarker {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)

    $markerPath = Join-Path $TargetRoot $deploymentMarkerName
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw "Existing non-empty ReplayLab target is not owned by this deployment script: $TargetRoot"
    }
    $marker = Read-JsonFile $markerPath "ReplayLab runtime deployment marker"
    $schema = [string](Get-RequiredJsonProperty $marker "schema" `
        "ReplayLab runtime deployment marker")
    if ($schema -cne $deploymentMarkerSchema) {
        throw "ReplayLab runtime deployment marker has an unsupported schema: $markerPath"
    }
    $ownedFiles = @(Get-RequiredJsonProperty $marker "ownedFiles" `
        "ReplayLab runtime deployment marker")
    # Markers that still list ReplayLabMCP.exe are accepted once and rewritten
    # to BreadMCP.exe on the next successful deploy.
    $normalizedOwnedFiles = @($ownedFiles | ForEach-Object {
        if ([string]$_ -ceq "ReplayLabMCP.exe") { "BreadMCP.exe" } else { [string]$_ }
    })
    if ($normalizedOwnedFiles.Count -eq ($deploymentOwnedNames.Count - 1)) {
        # A marker written before the independent runner owns exactly the
        # original payload names. Only an absent new name may be added.
        $legacyNames = @($deploymentOwnedNames | Where-Object { $_ -cne "ReplayLabShaderTools.exe" })
        Assert-ExactFileNames $normalizedOwnedFiles $legacyNames `
            "legacy ReplayLab runtime deployment marker ownedFiles"
        if (Test-Path -LiteralPath (Join-Path $TargetRoot "ReplayLabShaderTools.exe")) {
            throw "Legacy deployment marker does not own the existing ReplayLabShaderTools.exe."
        }
    }
    else {
        Assert-ExactFileNames $normalizedOwnedFiles $deploymentOwnedNames `
            "ReplayLab runtime deployment marker ownedFiles"
    }

    $stateValue = Get-RequiredJsonProperty $marker "state" `
        "ReplayLab runtime deployment marker"
    if ($stateValue -isnot [string] -or
        @("complete", "incomplete") -cnotcontains [string]$stateValue) {
        throw "ReplayLab runtime deployment marker has an invalid state: $markerPath"
    }
    $manifestHashValue = Get-RequiredJsonProperty $marker "sourceManifestSha256" `
        "ReplayLab runtime deployment marker"
    if ($manifestHashValue -isnot [string] -or
        [string]$manifestHashValue -notmatch '^[0-9A-Fa-f]{64}$') {
        throw "ReplayLab runtime deployment marker has an invalid source manifest hash: $markerPath"
    }

    foreach ($ownedName in $deploymentOwnedNames) {
        $ownedPath = Join-Path $TargetRoot $ownedName
        if (Test-Path -LiteralPath $ownedPath) {
            Assert-OrdinaryFile $ownedPath "deployed ReplayLab-owned file '$ownedName'" | Out-Null
        }
    }
    return [string]$stateValue
}

function Get-ValidatedDeploymentTarget {
    param(
        [Parameter(Mandatory = $true)][string]$AppDirectory,
        [switch]$AllowVerifiedAdoption
    )

    $applicationRoot = Assert-OrdinaryDirectory $AppDirectory "qrenderdoc application directory"
    $filesystemRoot = [IO.Path]::GetPathRoot($applicationRoot)
    if (Test-SamePath $applicationRoot $filesystemRoot) {
        throw "The qrenderdoc application directory must not be a filesystem root: $applicationRoot"
    }
    $applicationExecutable = $null
    foreach ($executableName in $qrenderdocExecutableNames) {
        $candidate = Join-Path $applicationRoot $executableName
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $applicationExecutable = Assert-OrdinaryFile $candidate `
                "qrenderdoc application executable"
            break
        }
    }
    if ($null -eq $applicationExecutable) {
        throw ("The qrenderdoc application directory must contain one of these executables: " +
            ($qrenderdocExecutableNames -join ", ") + ". Directory: $applicationRoot")
    }

    $targetRoot = Get-NormalizedPath (Join-Path $applicationRoot "replaylab")
    if (-not (Test-PathWithin $applicationRoot $targetRoot)) {
        throw "ReplayLab runtime target escaped the qrenderdoc application directory: $targetRoot"
    }
    Assert-NoReparseComponents $targetRoot "ReplayLab runtime target" | Out-Null

    $isOwned = $false
    $needsAdoption = $false
    $markerState = $null
    if (Test-Path -LiteralPath $targetRoot) {
        $targetRoot = Assert-OrdinaryDirectory $targetRoot "ReplayLab runtime target"
        $children = @(Get-ChildItem -LiteralPath $targetRoot -Force)
        if ($children.Count -gt 0) {
            $markerPath = Join-Path $targetRoot $deploymentMarkerName
            if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
                $markerState = Assert-DeploymentMarker $targetRoot
                $isOwned = $true
            }
            elseif ($AllowVerifiedAdoption) {
                $needsAdoption = $true
            }
            else {
                throw ("Existing non-empty ReplayLab target is not owned by this deployment " +
                    "script: $targetRoot. Use -AdoptExistingVerifiedRuntime only when its " +
                    "canonical runtime files already match the verified build output.")
            }
        }
    }
    return [pscustomobject]@{
        ApplicationRoot = $applicationRoot
        ApplicationExecutable = $applicationExecutable
        TargetRoot = $targetRoot
        Exists = Test-Path -LiteralPath $targetRoot -PathType Container
        IsOwned = $isOwned
        NeedsAdoption = $needsAdoption
        MarkerState = $markerState
    }
}

function Assert-QRenderDocNotRunning {
    param([Parameter(Mandatory = $true)][string]$ApplicationDirectory)

    $applicationRoot = Get-NormalizedPath $ApplicationDirectory
    $processFilter = ($qrenderdocExecutableNames | ForEach-Object {
        "Name = '$($_)'"
    }) -join " OR "
    try {
        $candidates = @(Get-CimInstance -ClassName Win32_Process `
            -Filter $processFilter -ErrorAction Stop)
    }
    catch {
        throw ("Cannot verify that qrenderdoc is stopped before ReplayLab deployment. " +
            "No target files were changed. Details: $($_.Exception.Message)")
    }

    foreach ($candidate in $candidates) {
        $name = [string]$candidate.Name
        if (-not ($qrenderdocExecutableNames -icontains $name)) {
            continue
        }

        $executablePath = [string]$candidate.ExecutablePath
        if ([string]::IsNullOrWhiteSpace($executablePath)) {
            try {
                $executablePath = [string](Get-Process -Id ([int]$candidate.ProcessId) `
                    -ErrorAction Stop).Path
            }
            catch {
                # A same-named process in another security context cannot be
                # attributed to this application directory without a path.
                continue
            }
        }
        if ([string]::IsNullOrWhiteSpace($executablePath)) {
            continue
        }

        try {
            $processPath = Get-NormalizedPath $executablePath
            $processDirectory = Split-Path -Parent $processPath
        }
        catch {
            continue
        }
        if (Test-SamePath $processDirectory $applicationRoot) {
            throw ("Cannot deploy ReplayLab while qrenderdoc is running from the target " +
                "application directory. Close '$processPath' (PID $($candidate.ProcessId)) " +
                "and retry. No target files were changed.")
        }
    }
}

function Assert-ExistingRuntimeMatchesSource {
    param(
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][object]$Runtime
    )

    foreach ($name in $runtimePayloadNames) {
        $record = $Runtime.Records[$name]
        $targetPath = Assert-OrdinaryFile (Join-Path $TargetRoot $name) `
            "existing ReplayLab runtime payload '$name'"
        $targetItem = Get-Item -LiteralPath $targetPath -Force
        $targetHash = Get-Sha256Hex $targetPath
        if ([int64]$targetItem.Length -ne [int64]$record.Size -or
            $targetHash -cne [string]$record.Sha256) {
            throw ("Cannot adopt the existing ReplayLab target because '$name' does not " +
                "match the verified runtime output by size and SHA-256: $TargetRoot")
        }
    }

    $targetManifest = Assert-OrdinaryFile (Join-Path $TargetRoot $runtimeManifestName) `
        "existing ReplayLab runtime manifest"
    $targetManifestHash = Get-Sha256Hex $targetManifest
    if ($targetManifestHash -cne [string]$Runtime.ManifestSha256) {
        throw ("Cannot adopt the existing ReplayLab target because its runtime manifest " +
            "does not exactly match the verified build output: $TargetRoot")
    }
}

function Assert-TargetFileReplaceable {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    Assert-OrdinaryFile $Path "deployed ReplayLab-owned file '$Name'" | Out-Null
    $stream = $null
    try {
        $stream = [IO.File]::Open(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None)
    }
    catch {
        throw ("Cannot replace ReplayLab runtime file '$Name' because it is active, locked, " +
            "or not writable. Stop qrenderdoc and the ReplayLab sidecars, then retry. " +
            "No process was stopped. Details: $($_.Exception.Message)")
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Remove-OwnedStagingFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    try {
        Assert-OrdinaryFile $Path "ReplayLab deployment staging file" | Out-Null
        Remove-Item -LiteralPath $Path -Force
    }
    catch {
        Write-Warning "Could not remove ReplayLab deployment staging file '$Path': $($_.Exception.Message)"
    }
}

function Install-DeploymentMarker {
    param(
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][object]$Runtime,
        [Parameter(Mandatory = $true)]
        [ValidateSet("complete", "incomplete")]
        [string]$State,
        [Parameter(Mandatory = $true)][string]$SessionId
    )

    $marker = [ordered]@{
        schema = $deploymentMarkerSchema
        state = $State
        version = $Runtime.Version
        deployedUtc = [DateTime]::UtcNow.ToString("o")
        sourceManifestSha256 = $Runtime.ManifestSha256
        ownedFiles = $deploymentOwnedNames
    }
    $stagedMarker = Join-Path $TargetRoot (".$SessionId.$deploymentMarkerName.tmp")
    try {
        $marker | ConvertTo-Json -Depth 4 | Set-Content `
            -LiteralPath $stagedMarker -Encoding UTF8 -ErrorAction Stop
        $stagedMarker = Assert-OrdinaryFile $stagedMarker `
            "staged ReplayLab deployment marker"
        try {
            Move-Item -LiteralPath $stagedMarker `
                -Destination (Join-Path $TargetRoot $deploymentMarkerName) `
                -Force -ErrorAction Stop
        }
        catch {
            throw ("Cannot install the ReplayLab deployment marker. It may be locked or the " +
                "target may not be writable. No process was stopped. Details: " +
                $_.Exception.Message)
        }
    }
    finally {
        Remove-OwnedStagingFile $stagedMarker
    }
}

$packagingRoot = Get-NormalizedPath $PSScriptRoot
$replayLabRoot = Get-NormalizedPath (Split-Path -Parent $packagingRoot)
if ([string]::IsNullOrWhiteSpace($RuntimeOutputDirectory)) {
    $RuntimeOutputDirectory = Join-Path $replayLabRoot "output\replaylab"
}

$runtime = Get-ValidatedRuntimeOutput $RuntimeOutputDirectory
$target = Get-ValidatedDeploymentTarget $ApplicationDirectory `
    -AllowVerifiedAdoption:$AdoptExistingVerifiedRuntime
if ($target.NeedsAdoption) {
    Assert-ExistingRuntimeMatchesSource $target.TargetRoot $runtime
}

if ($ValidateOnly) {
    [pscustomobject]@{
        Status = "valid"
        Version = $runtime.Version
        RuntimeOutputDirectory = $runtime.Root
        ApplicationDirectory = $target.ApplicationRoot
        TargetDirectory = $target.TargetRoot
        AdoptionRequired = $target.NeedsAdoption
    }
    return
}

# Runtime sidecars can legitimately be stopped while their owning qrenderdoc
# window remains open. Replacing them in that interval strands the old bridge
# on stale Host state, so gate the whole mutating path on the exact executable
# directory rather than on process name alone.
Assert-QRenderDocNotRunning $target.ApplicationRoot

if ($target.NeedsAdoption) {
    $adoptionSessionId = [Guid]::NewGuid().ToString("N")
    Install-DeploymentMarker $target.TargetRoot $runtime "complete" $adoptionSessionId
    [pscustomobject]@{
        Status = "adopted"
        Version = $runtime.Version
        ApplicationDirectory = $target.ApplicationRoot
        TargetDirectory = $target.TargetRoot
    }
    return
}

foreach ($ownedName in $deploymentOwnedNames) {
    Assert-TargetFileReplaceable (Join-Path $target.TargetRoot $ownedName) $ownedName
}
# Drop the retired MCP executable name during BreadMCP migration.
Assert-TargetFileReplaceable (
    Join-Path $target.TargetRoot "ReplayLabMCP.exe") "ReplayLabMCP.exe"

if (-not $target.Exists) {
    New-Item -ItemType Directory -Path $target.TargetRoot -ErrorAction Stop | Out-Null
}
Assert-OrdinaryDirectory $target.TargetRoot "ReplayLab runtime target" | Out-Null

$sessionId = [Guid]::NewGuid().ToString("N")
$stagedFiles = @()
$moves = @()
try {
    foreach ($name in $runtimePayloadNames) {
        $record = $runtime.Records[$name]
        $stagedPath = Join-Path $target.TargetRoot (".$sessionId.$name.tmp")
        Copy-Item -LiteralPath $record.Path -Destination $stagedPath -ErrorAction Stop
        $stagedPath = Assert-OrdinaryFile $stagedPath "staged ReplayLab runtime payload '$name'"
        $stagedFiles += $stagedPath
        $stagedItem = Get-Item -LiteralPath $stagedPath -Force
        $stagedHash = Get-Sha256Hex $stagedPath
        if ([int64]$stagedItem.Length -ne [int64]$record.Size -or
            $stagedHash -cne [string]$record.Sha256) {
            throw "Staged ReplayLab runtime payload '$name' failed size or SHA-256 verification."
        }
        $moves += [pscustomobject]@{
            Name = $name
            StagedPath = $stagedPath
            DestinationPath = Join-Path $target.TargetRoot $name
        }
    }

    $stagedManifest = Join-Path $target.TargetRoot (".$sessionId.$runtimeManifestName.tmp")
    Copy-Item -LiteralPath $runtime.ManifestPath -Destination $stagedManifest -ErrorAction Stop
    $stagedManifest = Assert-OrdinaryFile $stagedManifest "staged ReplayLab runtime manifest"
    $stagedFiles += $stagedManifest
    $stagedManifestHash = Get-Sha256Hex $stagedManifest
    if ($stagedManifestHash -cne $runtime.ManifestSha256) {
        throw "Staged ReplayLab runtime manifest failed SHA-256 verification."
    }
    $moves += [pscustomobject]@{
        Name = $runtimeManifestName
        StagedPath = $stagedManifest
        DestinationPath = Join-Path $target.TargetRoot $runtimeManifestName
    }

    # A second preflight narrows the race between staging and committing files.
    foreach ($ownedName in $deploymentOwnedNames) {
        Assert-TargetFileReplaceable (Join-Path $target.TargetRoot $ownedName) $ownedName
    }
    Assert-TargetFileReplaceable (
        Join-Path $target.TargetRoot "ReplayLabMCP.exe") "ReplayLabMCP.exe"

    if (-not $target.IsOwned) {
        $allowedStagingPaths = @{}
        foreach ($stagedPath in $stagedFiles) {
            $allowedStagingPaths[(Get-NormalizedPath $stagedPath).ToLowerInvariant()] = $true
        }
        foreach ($child in @(Get-ChildItem -LiteralPath $target.TargetRoot -Force)) {
            $childPath = (Get-NormalizedPath $child.FullName).ToLowerInvariant()
            if (-not $allowedStagingPaths.ContainsKey($childPath)) {
                throw ("The initially empty ReplayLab target changed during staging and will " +
                    "not be adopted implicitly: $($child.FullName)")
            }
        }
    }

    # Once every payload is staged and verified, an incomplete marker makes any
    # mid-commit failure explicitly owned and safe to retry.
    Install-DeploymentMarker $target.TargetRoot $runtime "incomplete" $sessionId

    foreach ($move in $moves) {
        try {
            Move-Item -LiteralPath $move.StagedPath -Destination $move.DestinationPath `
                -Force -ErrorAction Stop
        }
        catch {
            throw ("Cannot replace ReplayLab runtime file '$($move.Name)'. It may have become " +
                "active or locked during deployment. Stop qrenderdoc and the ReplayLab sidecars, " +
                "then retry. No process was stopped. Details: $($_.Exception.Message)")
        }
    }

    foreach ($name in $runtimePayloadNames) {
        $record = $runtime.Records[$name]
        $deployedPath = Assert-OrdinaryFile (Join-Path $target.TargetRoot $name) `
            "deployed ReplayLab runtime payload '$name'"
        $deployedItem = Get-Item -LiteralPath $deployedPath -Force
        $deployedHash = Get-Sha256Hex $deployedPath
        if ([int64]$deployedItem.Length -ne [int64]$record.Size -or
            $deployedHash -cne [string]$record.Sha256) {
            throw "Deployed ReplayLab runtime payload '$name' failed final verification."
        }
    }
    $deployedManifestHash = Get-Sha256Hex (Join-Path $target.TargetRoot $runtimeManifestName)
    if ($deployedManifestHash -cne $runtime.ManifestSha256) {
        throw "Deployed ReplayLab runtime manifest failed final verification."
    }

    Install-DeploymentMarker $target.TargetRoot $runtime "complete" $sessionId

    $legacyMcpPath = Join-Path $target.TargetRoot "ReplayLabMCP.exe"
    if (Test-Path -LiteralPath $legacyMcpPath) {
        Assert-OrdinaryFile $legacyMcpPath "legacy ReplayLabMCP.exe" | Out-Null
        Remove-Item -LiteralPath $legacyMcpPath -Force -ErrorAction Stop
    }

    $finalMarkerState = Assert-DeploymentMarker $target.TargetRoot
    if ($finalMarkerState -cne "complete") {
        throw "ReplayLab runtime deployment marker did not reach the complete state."
    }

    [pscustomobject]@{
        Status = "deployed"
        Version = $runtime.Version
        ApplicationDirectory = $target.ApplicationRoot
        TargetDirectory = $target.TargetRoot
    }
}
finally {
    foreach ($stagedPath in $stagedFiles) {
        Remove-OwnedStagingFile $stagedPath
    }
}
