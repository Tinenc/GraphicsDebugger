[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path (Split-Path -Parent $PSScriptRoot) `
    "deploy_replaylab_integration.ps1"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $scriptPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0)
{
    throw "deploy_replaylab_integration.ps1 does not parse: $($parseErrors[0].Message)"
}

$source = Get-Content -LiteralPath $scriptPath -Raw
$requiredFiles = @(
    "__init__.py",
    "ai_context.py",
    "cbuffer_alias_identity.py",
    "dxil_access_signature.py",
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
    if ($source -notmatch [regex]::Escape(('"' + $requiredFile + '"')))
    {
        throw "Deployment script does not fail closed on required source: $requiredFile"
    }
}
if ($source -notmatch 'Get-ChildItem[^\r\n]+-Recurse' -or
    $source -notmatch '\$segments -contains "__pycache__"' -or
    $source -notmatch 'Join-Path \$target \$sourceFile\.RelativePath' -or
    $source -notmatch 'Remove-ReplayLabStaleTargetEntries \$target \$sourceFiles' -or
    $source -notmatch 'Get-FileHash')
{
    throw "Deployment script does not recursively copy and verify safe relative paths"
}
if ($source -notmatch 'integrations/replaylab/python/renderdoc_mcp_bridge' -or
    $source -match 'extension\.json' -or
    $source -match 'qrenderdoc/extensions' -or
    $source -match '\$env:APPDATA')
{
    throw "Deployment script does not enforce the fixed built-in ReplayLab integration layout"
}

foreach ($name in @(
        "Get-ReplayLabRelativePath",
        "Assert-ReplayLabChildPath",
        "Assert-ReplayLabNoReparseComponents",
        "Get-ReplayLabIntegrationSourceFiles",
        "Remove-ReplayLabStaleTargetEntries",
        "Assert-ReplayLabIntegrationTargetStopped"))
{
    $definition = @($ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true))
    if ($definition.Count -ne 1)
    {
        throw "Expected exactly one $name definition in the deployment script"
    }
    . ([scriptblock]::Create($definition[0].Extent.Text))
}

$testRoot = Join-Path ([IO.Path]::GetFullPath([IO.Path]::GetTempPath())) (
    "replaylab-deploy-test-" + [guid]::NewGuid().ToString("N"))
try
{
    $applicationRoot = Join-Path $testRoot "Development"
    $integrationTarget = Join-Path $applicationRoot `
        "integrations\replaylab\python\renderdoc_mcp_bridge"
    function Get-Process {
        param([string]$ErrorAction)
        return $script:deploymentProcesses
    }
    try
    {
        $script:deploymentProcesses = @([pscustomobject]@{
            ProcessName = "qrenderdoc"; Id = 11
            Path = (Join-Path $testRoot "Foreign\qrenderdoc.exe")
        })
        Assert-ReplayLabIntegrationTargetStopped $integrationTarget
        foreach ($processName in @("qrenderdoc", "q_render_soco_doc"))
        {
            $script:deploymentProcesses = @([pscustomobject]@{
                ProcessName = $processName; Id = 12
                Path = (Join-Path $applicationRoot ($processName + ".exe")).ToUpperInvariant()
            })
            $message = ""
            try { Assert-ReplayLabIntegrationTargetStopped $integrationTarget }
            catch { $message = $_.Exception.Message }
            if ($message -notmatch "running from the target application directory")
            {
                throw "A running target application was not rejected: $message"
            }
        }
        foreach ($unknownPath in @("", "relative\qrenderdoc.exe"))
        {
            $script:deploymentProcesses = @([pscustomobject]@{
                ProcessName = "qrenderdoc"; Id = 13; Path = $unknownPath
            })
            $message = ""
            try { Assert-ReplayLabIntegrationTargetStopped $integrationTarget }
            catch { $message = $_.Exception.Message }
            if ($message -notmatch "Cannot determine the application directory")
            {
                throw "An unknown process path did not fail closed: $message"
            }
        }
        if (Test-Path -LiteralPath $applicationRoot)
        {
            throw "Process checks must not create or change the deployment target."
        }
    }
    finally
    {
        Remove-Item Function:\Get-Process
        Remove-Variable deploymentProcesses -Scope Script
    }

    $sourceRoot = Join-Path $testRoot "source"
    $nestedFile = Join-Path $sourceRoot "project_tools\nested_panel.py"
    $cacheFile = Join-Path $sourceRoot "project_tools\__pycache__\nested_panel.py"
    New-Item -ItemType Directory -Force -Path `
        ([IO.Path]::GetDirectoryName($nestedFile)), `
        ([IO.Path]::GetDirectoryName($cacheFile)) | Out-Null
    [IO.File]::WriteAllText((Join-Path $sourceRoot "integration.json"), "{}")
    [IO.File]::WriteAllText((Join-Path $sourceRoot "extension.json"), "{}")
    [IO.File]::WriteAllText((Join-Path $sourceRoot "__init__.py"), "# fixture")
    [IO.File]::WriteAllText($nestedFile, "# nested fixture")
    [IO.File]::WriteAllText($cacheFile, "# cache fixture")
    [IO.File]::WriteAllText(
        (Join-Path $sourceRoot "project_tools\ignored.json"), "{}")

    $relativePaths = @(
        Get-ReplayLabIntegrationSourceFiles $sourceRoot |
            ForEach-Object { $_.RelativePath.Replace('\', '/') })
    $expected = @("__init__.py", "integration.json", "project_tools/nested_panel.py")
    if (($relativePaths -join '|') -ne ($expected -join '|'))
    {
        throw "Recursive deployment discovery returned an unsafe or incomplete set: $($relativePaths -join '|')"
    }

    $mirrorTarget = Join-Path $testRoot "mirror-target"
    $keptTarget = Join-Path $mirrorTarget "project_tools\nested_panel.py"
    $legacyTarget = Join-Path $mirrorTarget "pass_panel.py"
    $staleNestedTarget = Join-Path $mirrorTarget "obsolete\stale.py"
    New-Item -ItemType Directory -Force -Path `
        ([IO.Path]::GetDirectoryName($keptTarget)), `
        ([IO.Path]::GetDirectoryName($staleNestedTarget)) | Out-Null
    [IO.File]::WriteAllText($keptTarget, "# existing canonical target")
    [IO.File]::WriteAllText($legacyTarget, "# removed legacy module")
    [IO.File]::WriteAllText($staleNestedTarget, "# stale nested module")
    $fixtureSourceFiles = @(Get-ReplayLabIntegrationSourceFiles $sourceRoot)
    Remove-ReplayLabStaleTargetEntries $mirrorTarget $fixtureSourceFiles
    if (-not (Test-Path -LiteralPath $keptTarget -PathType Leaf) -or
        (Test-Path -LiteralPath $legacyTarget) -or
        (Test-Path -LiteralPath $staleNestedTarget) -or
        (Test-Path -LiteralPath (Join-Path $mirrorTarget "obsolete")))
    {
        throw "Deployment mirror cleanup did not preserve canonical files and remove stale entries"
    }

    $outside = Join-Path $testRoot "outside.py"
    [IO.File]::WriteAllText($outside, "# outside")
    $message = ""
    try
    {
        Assert-ReplayLabChildPath $sourceRoot $outside | Out-Null
    }
    catch
    {
        $message = $_.Exception.Message
    }
    if ($message -notmatch "outside the ReplayLab integration root")
    {
        throw "Sibling path did not fail the child-path boundary check: $message"
    }

    # Every canonical module is a fail-closed deployment prerequisite. This
    # catches a source file that exists in a developer checkout but was omitted
    # from Git and is therefore absent in a clean build checkout.
    foreach ($missingFile in $requiredFiles)
    {
        $fixtureRoot = Join-Path $testRoot (
            "missing-" + [IO.Path]::GetFileNameWithoutExtension($missingFile))
        $fixtureSource = Join-Path $fixtureRoot "renderdoc_mcp_bridge"
        New-Item -ItemType Directory -Force -Path $fixtureSource | Out-Null
        foreach ($presentFile in $requiredFiles)
        {
            if ($presentFile -ne $missingFile)
            {
                [IO.File]::WriteAllText(
                    (Join-Path $fixtureSource $presentFile), "# fixture")
            }
        }
        $missingMessage = ""
        try
        {
            & $scriptPath `
                -SourceRoot $fixtureSource `
                -TargetRoot (Join-Path $fixtureRoot "deployed") | Out-Null
        }
        catch
        {
            $missingMessage = $_.Exception.Message
        }
        if ($missingMessage -notmatch "Required ReplayLab integration source is missing" -or
            $missingMessage -notmatch [regex]::Escape($missingFile))
        {
            throw "Missing canonical source '$missingFile' did not fail closed: $missingMessage"
        }
    }
}
finally
{
    if (Test-Path -LiteralPath $testRoot)
    {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}

Write-Host "ReplayLab built-in integration deployment source checks passed."
