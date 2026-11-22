[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-True {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

$testsRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$packagingRoot = [IO.Path]::GetFullPath((Split-Path -Parent $testsRoot))
$replayLabRoot = [IO.Path]::GetFullPath((Split-Path -Parent $packagingRoot))
$buildScript = Join-Path $packagingRoot "Build-ReplayLabRuntime.ps1"
$claudeSetupScript = Join-Path $packagingRoot "setup_claude_code_acp.ps1"

$tokens = $null
$parseErrors = $null
[Management.Automation.Language.Parser]::ParseFile(
    $buildScript, [ref]$tokens, [ref]$parseErrors) | Out-Null
if ($parseErrors.Count -gt 0) {
    throw ("Build script has PowerShell parser errors: " +
        (($parseErrors | ForEach-Object { $_.Message }) -join "; "))
}

$tokens = $null
$parseErrors = $null
[Management.Automation.Language.Parser]::ParseFile(
    $claudeSetupScript, [ref]$tokens, [ref]$parseErrors) | Out-Null
if ($parseErrors.Count -gt 0) {
    throw ("Claude ACP setup script has PowerShell parser errors: " +
        (($parseErrors | ForEach-Object { $_.Message }) -join "; "))
}

foreach ($entryName in @(
    "agent_host_entry.py", "internal_host_entry.py", "mcp_host_entry.py",
    "probe_frozen_mcp_route.py", "shader_tools_entry.py", "build_shader_tools.py")) {
    $entryPath = Join-Path $packagingRoot $entryName
    Assert-True (Test-Path -LiteralPath $entryPath -PathType Leaf) "Missing entry point: $entryName"
}

$source = Get-Content -LiteralPath $buildScript -Raw
$claudeSetupSource = Get-Content -LiteralPath $claudeSetupScript -Raw
$agentEntrySource = Get-Content -LiteralPath `
    (Join-Path $packagingRoot "agent_host_entry.py") -Raw
$projectFile = Get-Content -LiteralPath `
    (Join-Path (Split-Path -Parent $replayLabRoot) "qrenderdoc\qrenderdoc_local.vcxproj") -Raw
Assert-True ($claudeSetupSource.Contains("isolated CLAUDE_CONFIG_DIR") -and
    $claudeSetupSource.Contains("Save and Connect")) `
    "Claude ACP setup must direct login through ReplayLab's profile-isolated connection flow."
Assert-True (-not $claudeSetupSource.Contains(
        ([Environment]::NewLine + "    claude setup-token"))) `
    "Claude ACP dependency setup must not write credentials to the user's global Claude profile."
Assert-True (-not $claudeSetupSource.Contains(
        ([Environment]::NewLine + "    claude auth login"))) `
    "Claude ACP dependency setup must keep interactive login in the selected connection."
Assert-True ($source.Contains('$PythonExecutable "replaylab-ai"')) `
    "Build script must name the AgentHost executable replaylab-ai."
Assert-True ($source.Contains('$PythonExecutable "BreadMCP"')) `
    "Build script must name the MCP executable BreadMCP."
Assert-True ($source.Contains('$PythonExecutable "ReplayLabInternal"')) `
    "Build script must name the private executable ReplayLabInternal."
Assert-True ($source.Contains('$PythonExecutable -I $shaderToolsBuilder') -and
    $source.Contains('"ReplayLabShaderTools.exe"') -and
    $source.Contains('--cache-root $shaderToolsCache')) `
    "Build must cache and package the independent shader runner."
Assert-True ($source.Contains('& $builtShaderTools $adapterCommand --help') -and
    $source.IndexOf('foreach ($hostDist in') -lt
    $source.IndexOf('& $builtMcp --help')) `
    "All adapters must import successfully and the runner must be beside every Host before smoke tests."
$legacyManifestToken = "extension" + ".json"
Assert-True (-not $source.Contains($legacyManifestToken)) `
    "Runtime packaging must not copy or generate a Python extension manifest."
Assert-True ($source.Contains(
        '& $builtAgent -I -m replaylab_agent.mcp_proxy --help')) `
    "Build script must smoke-test the frozen AgentHost MCP proxy dispatch."
Assert-True ($source.Contains(
        '& $builtAgent -I -m replaylab_agent.selftest --external-mcp-import-smoke')) `
    "Build script must smoke-test the frozen external MCP client imports."
Assert-True ($source.Contains(
        '& $PythonExecutable $frozenMcpRouteProbe --agent $builtAgent')) `
    "Build script must handshake through the frozen AgentHost filter and BreadMCP."
Assert-True ($source.Contains(
        '& $frozenHostLifetimeProbe -InternalHost $builtInternal -McpHost $builtMcp')) `
    "Both frozen Hosts must retain their extracted runtime after ensure exits."
Assert-True ($source.Contains('"deepagents"') -and
    $source.Contains('"agent-client-protocol"') -and
    $source.Contains('"langchain-google-vertexai"') -and
    $source.Contains('-CopyMetadata $agentCopyMetadata')) `
    "AgentHost packaging must copy required agent, MCP, and provider metadata."
Assert-True (-not $source.Contains('"pydantic_ai"') -and
    -not $source.Contains('"pydantic-ai-slim"') -and
    -not $source.Contains('"fastmcp"') -and
    -not $source.Contains('"fastmcp-slim"')) `
    "AgentHost packaging must not retain removed pydantic-ai or FastMCP runtime paths."
Assert-True (-not $source.Contains('LiteLLM') -and -not $source.Contains('litellm')) `
    "Runtime packaging must not expose the unused LiteLLM build option."
Assert-True ($source.Contains('deepagents_version = verify_deepagents_version(') -and
    $source.Contains('(Join-Path $replayLabRoot "pyproject.toml") + ":replaylab_agent"') -and
    $source.Contains('dependencies = @{ deepagents = [string]$probe.deepagentsVersion }')) `
    "Build and frozen self-test must verify the canonical framework pin and record its version."
Assert-True ($source.Contains(
        '$agentData = @($agentSkillData) + @($mcpProviderData)')) `
    "AgentHost data must combine canonical Skills with the fixed MCP provider helper."
Assert-True ($source.Contains('-AddData @($agentData)')) `
    "AgentHost packaging must embed canonical Skills and the continuation helper."
Assert-True ($source.Contains(
        '$continuationDriverSource + ":replaylab_helpers"')) `
    "MCP packaging must embed the fixed UEX Blender continuation source helper."
Assert-True ($source.Contains(
        '$bundledSkillFolders = @(Get-ChildItem -LiteralPath $skillRoot -Directory') -and
    $source.Contains('Where-Object { $_.Name -eq "replaylab-core" }') -and
    $source.Contains('$agentSkillData = @($bundledSkillFolders | ForEach-Object')) `
    "Runtime packaging must discover, validate, and bundle the standing ReplayLab Skill set before freezing."
Assert-True ($projectFile.Contains(
        '$(SolutionDir).agents\skills\renderdoc-shader-workbench\**\*') -and
    $projectFile.Contains('$(SolutionDir).agents\skills\replaylab-core\**\*') -and
    $projectFile.Contains('$(SolutionDir)replaylab\pyproject.toml') -and
    $projectFile.Contains('$(SolutionDir)replaylab\mcp_host\pyproject.toml')) `
    "qrenderdoc incremental runtime inputs must include bundled Skills and dependency manifests."
Assert-True ($source.Contains('-AddData @($mcpProviderData)')) `
    "BreadMCP packaging must add its fixed provider helper data."
Assert-True ($source.Contains(
        '(Join-Path $mcpRoot "src\resource_export.py")')) `
    "ValidateOnly must require the resource export Host source."
Assert-True ($source.Contains('failed its frozen catalog/Skill doctor smoke test')) `
    "Build script must smoke-test model metadata and bundled Skills in the frozen AgentHost."
Assert-True ($source.Contains('preset = "cursor-acp"')) `
    "Frozen doctor smoke must cover the Cursor ACP preset."
Assert-True ($source.Contains('model = "composer-2.5[fast=true]"')) `
    "Frozen doctor smoke must cover Cursor's bracketed Composer model id."
Assert-True (-not $source.Contains('context_window_tokens')) `
    "Frozen Cursor doctor smoke must resolve context without a profile override."
Assert-True ($source.Contains(
        '--profile "packaging-cursor-smoke" --mcp-host $builtMcp --json')) `
    "Build script must run frozen doctor against the Cursor smoke profile."
Assert-True ($source.Contains(
        '[string]$cursorContextCheck[0].detail -ne "200000 tokens (model-catalog)"')) `
    "Frozen Cursor doctor smoke must require the verified 200000-token model catalog entry."
Assert-True ($source.Contains('[Text.UTF8Encoding]::new($false)')) `
    "Frozen doctor smoke profiles must be written as UTF-8 without a Windows PowerShell BOM."
Assert-True ($source.Contains('$parsedDoctorChecks.GetEnumerator()')) `
    "Frozen doctor smoke must enumerate a root JSON array under Windows PowerShell 5.1."
Assert-True ($source.Contains('$parsedCursorDoctorChecks.GetEnumerator()')) `
    "Frozen Cursor doctor smoke must enumerate a root JSON array under Windows PowerShell 5.1."
Assert-True ($agentEntrySource.Contains(
        '["-m", "replaylab_agent.mcp_proxy"]')) `
    "AgentHost entry point must dispatch the task-filtered MCP proxy."
Assert-True ($agentEntrySource.Contains('BreadMCP.exe')) `
    "Frozen AgentHost must resolve its co-packaged BreadMCP executable."

$encodingProbe = Join-Path ([IO.Path]::GetTempPath()) `
    ("replaylab-profile-encoding-" + [Guid]::NewGuid().ToString("N") + ".json")
try {
    [IO.File]::WriteAllText(
        $encodingProbe,
        '{"schema":"replaylab-ai-profiles/v2"}' + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false))
    $probeBytes = [IO.File]::ReadAllBytes($encodingProbe)
    $hasUtf8Bom = $probeBytes.Length -ge 3 -and
        $probeBytes[0] -eq 0xEF -and $probeBytes[1] -eq 0xBB -and $probeBytes[2] -eq 0xBF
    Assert-True (-not $hasUtf8Bom) `
        "Frozen doctor smoke profiles must remain BOM-free on this PowerShell host."
}
finally {
    Remove-Item -LiteralPath $encodingProbe -Force -ErrorAction SilentlyContinue
}

$validationOutput = Join-Path $replayLabRoot "output\packaging-validation-only"
$validationWork = Join-Path $replayLabRoot "output\.packaging-validation-work"
$result = & $buildScript -ValidateOnly -OutputDirectory $validationOutput `
    -WorkDirectory $validationWork
Assert-True ($result.Status -eq "valid") "ValidateOnly did not report a valid layout."
Assert-True (-not (Test-Path -LiteralPath $validationOutput)) `
    "ValidateOnly must not create the output directory."
Assert-True (-not (Test-Path -LiteralPath $validationWork)) `
    "ValidateOnly must not create the work directory."

$unsafeRejected = $false
try {
    & $buildScript -ValidateOnly -OutputDirectory $replayLabRoot `
        -WorkDirectory $validationWork | Out-Null
}
catch {
    $unsafeRejected = $true
}
Assert-True $unsafeRejected "ValidateOnly must reject the ReplayLab source root as output."

$scratchRoot = Join-Path ([IO.Path]::GetTempPath()) ("bread-runtime-test-" + [Guid]::NewGuid().ToString("N"))
$result = & $buildScript -ValidateOnly -TemporaryBuildRoot $scratchRoot `
    -OutputDirectory (Join-Path $scratchRoot "runtime") -WorkDirectory (Join-Path $scratchRoot "work")
Assert-True ($result.Status -eq "valid") "Named temporary output should be accepted."
Assert-True (-not (Test-Path -LiteralPath $scratchRoot)) "Validation must not create temporary outputs."
$unsafeRejected = $false
try {
    & $buildScript -ValidateOnly -TemporaryBuildRoot ([IO.Path]::GetTempPath()) `
        -OutputDirectory $validationOutput -WorkDirectory $validationWork | Out-Null
}
catch { $unsafeRejected = $true }
Assert-True $unsafeRejected "The system temporary root itself must never be a generated build root."

Write-Host "ReplayLab packaging static and parameter-safety tests passed."
