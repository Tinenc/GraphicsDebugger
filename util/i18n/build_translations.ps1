[CmdletBinding()]
param(
    [string]$QtBin,
    [string]$OutputDirectory,
    [switch]$RequireComplete,
    [switch]$SkipQtCatalog
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "QtLinguistTools.ps1")

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "../..")).Path
$translationDirectory = Join-Path $repoRoot "qrenderdoc/Translations"
$catalog = Join-Path $translationDirectory "qrenderdoc_zh_CN.ts"
$glossary = Join-Path $translationDirectory "glossary.zh_CN.yaml"
$linter = Join-Path $PSScriptRoot "lint_ts.py"
if (-not $OutputDirectory)
{
    $OutputDirectory = $translationDirectory
}

$lrelease = Resolve-QtLinguistTool -Name "lrelease" -QtBin $QtBin
Assert-Qt5LinguistTool -Path $lrelease
$python = Resolve-PythonForI18n

$lintArguments = @($catalog, "--glossary", $glossary)
if ($RequireComplete)
{
    $lintArguments += "--require-complete"
}
& $python $linter $lintArguments
if ($LASTEXITCODE -ne 0)
{
    throw "Translation lint failed; no QM file was produced."
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$outputCatalog = Join-Path $OutputDirectory "qrenderdoc_zh_CN.qm"
& $lrelease $catalog -qm $outputCatalog -compress -nounfinished -removeidentical
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $outputCatalog))
{
    throw "lrelease failed to create $outputCatalog."
}
Write-Host "Built $outputCatalog"

if (-not $SkipQtCatalog)
{
    $qtPrefix = Split-Path -Parent (Split-Path -Parent $lrelease)
    $qtCatalog = Join-Path $qtPrefix "translations/qt_zh_CN.qm"
    if (Test-Path -LiteralPath $qtCatalog)
    {
        $qtCatalogOutput = Join-Path $OutputDirectory "qt_zh_CN.qm"
        Copy-Item -LiteralPath $qtCatalog -Destination $qtCatalogOutput -Force
        Write-Host "Copied Qt standard-widget translation to $qtCatalogOutput"
    }
    else
    {
        Write-Warning "Qt standard translation qt_zh_CN.qm was not found under $qtPrefix; standard Qt dialogs may remain English."
    }
}
