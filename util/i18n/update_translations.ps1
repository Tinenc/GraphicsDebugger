[CmdletBinding()]
param(
    [string]$QtBin,
    [switch]$Check,
    [switch]$KeepObsolete,
    [switch]$AllowExtractorWarnings
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "QtLinguistTools.ps1")

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "../..")).Path
$translationDirectory = Join-Path $repoRoot "qrenderdoc/Translations"
$catalog = Join-Path $translationDirectory "qrenderdoc_zh_CN.ts"
$linter = Join-Path $PSScriptRoot "lint_ts.py"
$glossary = Join-Path $translationDirectory "glossary.zh_CN.yaml"
$sourceManifestPath = Join-Path $PSScriptRoot "source_manifest.json"
$replayLabExtractor = Join-Path $PSScriptRoot "extract_replaylab_translations.py"
$replayLabCatalogSync = Join-Path $PSScriptRoot "sync_replaylab_catalog.py"
$replayLabTranslations = Join-Path $PSScriptRoot "replaylab_zh_CN.json"
$replayLabRegistry = Join-Path $translationDirectory "ReplayLabProjects.cpp"
$python = Resolve-PythonForI18n

# The native settings catalog is generated from the same provider definitions as AgentHost.
$catalogArguments = @((Join-Path $repoRoot 'util/ai/generate_provider_catalog.py'))
if ($Check) { $catalogArguments += '--check' }
& $python @catalogArguments
if ($LASTEXITCODE -ne 0) { throw 'The offline AI provider catalog is not synchronized.' }

$replayLabSourceRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $repoRoot "replaylab/renderdoc_mcp_bridge"))
$replayLabProjectLauncher = Join-Path $replayLabSourceRoot "project_launcher.py"
$replayLabModule = Join-Path $replayLabSourceRoot "__init__.py"
if ((Test-Path -LiteralPath $replayLabProjectLauncher -PathType Leaf) -and
    (Test-Path -LiteralPath $replayLabModule -PathType Leaf))
{
    # Project UI modules must use the project_*.py naming convention. Discover
    # them recursively so splitting a dialog into a new module cannot silently
    # bypass translation extraction. __init__.py is bounded by explicit project
    # UI scope markers because it also hosts unrelated integration panels.
    $replayLabSources = @($replayLabModule) + @(
        Get-ChildItem -LiteralPath $replayLabSourceRoot -Recurse -File -Filter "project_*.py" |
            Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' } |
            ForEach-Object { $_.FullName }
    )
    $replayLabSources = @($replayLabSources | Sort-Object -Unique)
    $replayLabExtractArguments = @(
        $replayLabExtractor,
        "--output", $replayLabRegistry
    )
    foreach ($replayLabSource in $replayLabSources)
    {
        $replayLabExtractArguments += @("--source", $replayLabSource)
    }
    if ($Check) { $replayLabExtractArguments += "--check" }
    & $python $replayLabExtractArguments
    if ($LASTEXITCODE -ne 0)
    {
        throw "ReplayLab Python UI translation extraction failed."
    }
}
else
{
    throw "Canonical ReplayLab built-in integration source is unavailable or incomplete: $replayLabSourceRoot"
}

if (-not (Test-Path -LiteralPath $sourceManifestPath))
{
    throw "i18n source manifest not found: $sourceManifestPath"
}
$sourceManifest = Get-Content -LiteralPath $sourceManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($sourceManifest.schema_version -ne 1)
{
    throw "Unsupported i18n source manifest schema in $sourceManifestPath"
}

$sourceRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot ([string]$sourceManifest.root)))
$repoPrefix = $repoRoot.TrimEnd([char[]]"\/") + [System.IO.Path]::DirectorySeparatorChar
if (-not $sourceRoot.StartsWith($repoPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
    -not (Test-Path -LiteralPath $sourceRoot -PathType Container))
{
    throw "i18n source manifest root must be an existing directory inside the repository: $sourceRoot"
}

$sourceExtensions = @($sourceManifest.extensions | ForEach-Object { ([string]$_).ToLowerInvariant() })
$excludedPrefixes = @($sourceManifest.excluded_prefixes | ForEach-Object {
    $prefix = ([string]$_).Replace('\', '/')
    if (-not $prefix.EndsWith('/')) { $prefix += '/' }
    $prefix
})
$includedPrefixes = @($sourceManifest.included_prefixes | ForEach-Object {
    $prefix = ([string]$_).Replace('\', '/')
    if (-not $prefix.EndsWith('/')) { $prefix += '/' }
    $prefix
})

function Test-I18nSourcePath
{
    param([System.IO.FileInfo]$File)

    if ($sourceExtensions -notcontains $File.Extension.ToLowerInvariant())
    {
        return $false
    }
    $relative = $File.FullName.Substring($repoRoot.Length).TrimStart([char[]]"\/").Replace('\', '/')
    $isExcluded = $false
    foreach ($prefix in $excludedPrefixes)
    {
        if ($relative.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase))
        {
            $isExcluded = $true
            break
        }
    }
    if (-not $isExcluded)
    {
        return $true
    }
    foreach ($prefix in $includedPrefixes)
    {
        if ($relative.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase))
        {
            return $true
        }
    }
    return $false
}

$sourceFiles = @(Get-ChildItem -LiteralPath $sourceRoot -Recurse -File |
    Where-Object { Test-I18nSourcePath -File $_ } |
    Sort-Object -Property FullName |
    # Qt 5 lupdate decodes response-file paths using the local code page.
    # Repository-relative paths keep a Unicode checkout root out of that file.
    ForEach-Object { $_.FullName.Substring($repoRoot.Length + 1).Replace('\', '/') })
if ($sourceFiles.Count -eq 0)
{
    throw "i18n source manifest did not discover any source files."
}

if (-not (Test-Path -LiteralPath $catalog))
{
    throw "Translation catalog not found: $catalog"
}

$lupdate = Resolve-QtLinguistTool -Name "lupdate" -QtBin $QtBin
Assert-Qt5LinguistTool -Path $lupdate

$temporaryCatalog = $null
$sourceList = $null
$outputCatalog = $catalog
if ($Check)
{
    # Keep the check file beside the real TS so lupdate emits the same repository-relative locations.
    $temporaryCatalog = Join-Path $translationDirectory (".qrenderdoc_zh_CN.check.{0}.ts" -f $PID)
    Copy-Item -LiteralPath $catalog -Destination $temporaryCatalog
    $outputCatalog = $temporaryCatalog
}

try
{
    $sourceList = [System.IO.Path]::GetTempFileName()
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($sourceList, [string[]]$sourceFiles, $utf8NoBom)

    $arguments = [System.Collections.Generic.List[string]]::new()
    $arguments.Add("@$sourceList")
    foreach ($argument in @(
        "-locations", "relative", "-no-ui-lines",
        "-source-language", "en", "-target-language", "zh_CN"
    ))
    {
        $arguments.Add($argument)
    }
    if (-not $KeepObsolete)
    {
        $arguments.Add("-no-obsolete")
    }
    $arguments.Add("-ts")
    $arguments.Add($outputCatalog)

    # Redirect stderr to a file so Windows PowerShell 5 does not wrap every native diagnostic
    # in a noisy ErrorRecord. The file is outside the repository and is always removed below.
    $extractorStderr = [System.IO.Path]::GetTempFileName()
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    Push-Location $repoRoot
    try
    {
        $extractorStdout = (& $lupdate $arguments 2> $extractorStderr | Out-String)
        $extractorExitCode = $LASTEXITCODE
        $extractorErrors = Get-Content -LiteralPath $extractorStderr -Raw -ErrorAction SilentlyContinue
        $extractorOutput = $extractorStdout + $extractorErrors
    }
    finally
    {
        Pop-Location
        $ErrorActionPreference = $previousErrorActionPreference
        Remove-Item -LiteralPath $extractorStderr -Force -ErrorAction SilentlyContinue
    }
    if ($extractorOutput.Trim())
    {
        Write-Host ($extractorOutput.TrimEnd())
    }
    if ($extractorExitCode -ne 0)
    {
        throw "lupdate failed with exit code $extractorExitCode."
    }

    # This diagnostic means the string was not added to the catalog at all, so it must not pass CI silently.
    if (-not $AllowExtractorWarnings -and $extractorOutput -match 'tr\(\) cannot be called without context')
    {
        throw 'lupdate found tr() calls without a translation context. Use a QObject class tr() or QCoreApplication::translate("StableContext", ...).'
    }

    $replayLabSyncArguments = @(
        $replayLabCatalogSync,
        $outputCatalog,
        "--translations", $replayLabTranslations
    )
    if ($Check) { $replayLabSyncArguments += "--check" }
    & $python $replayLabSyncArguments
    if ($LASTEXITCODE -ne 0)
    {
        throw "ReplayLab reviewed catalog synchronization failed."
    }

    $lintArguments = @($catalog, "--glossary", $glossary)
    if ($Check)
    {
        $lintArguments += @("--compare-catalog", $temporaryCatalog)
    }
    & $python $linter $lintArguments
    if ($LASTEXITCODE -ne 0)
    {
        throw "Translation lint failed."
    }

    if ($Check)
    {
        Write-Host "Translation catalog is synchronized with all tracked UI sources."
    }
    else
    {
        Write-Host "Updated $catalog"
    }
}
finally
{
    if ($sourceList -and (Test-Path -LiteralPath $sourceList))
    {
        Remove-Item -LiteralPath $sourceList -Force
    }
    if ($temporaryCatalog -and (Test-Path -LiteralPath $temporaryCatalog))
    {
        Remove-Item -LiteralPath $temporaryCatalog -Force
    }
}
