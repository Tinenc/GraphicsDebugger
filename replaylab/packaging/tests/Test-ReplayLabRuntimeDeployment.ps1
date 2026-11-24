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

function Assert-Throws {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][string]$Pattern,
        [Parameter(Mandatory = $true)][string]$Message
    )
    $caught = $null
    try {
        & $Action
    }
    catch {
        $caught = $_
    }
    if ($null -eq $caught) {
        throw $Message
    }
    if ([string]$caught.Exception.Message -notmatch $Pattern) {
        throw "$Message Unexpected error: $($caught.Exception.Message)"
    }
}

function Set-FakeRuntimeOutput {
    param([Parameter(Mandatory = $true)][string]$Directory)

    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    [IO.File]::WriteAllBytes(
        (Join-Path $Directory "replaylab-ai.exe"),
        [Text.Encoding]::UTF8.GetBytes("fake-agent-runtime"))
    [IO.File]::WriteAllBytes(
        (Join-Path $Directory "ReplayLabInternal.exe"),
        [Text.Encoding]::UTF8.GetBytes("fake-internal-runtime"))
    [IO.File]::WriteAllBytes(
        (Join-Path $Directory "BreadMCP.exe"),
        [Text.Encoding]::UTF8.GetBytes("fake-mcp-runtime"))
    [IO.File]::WriteAllBytes(
        (Join-Path $Directory "ReplayLabShaderTools.exe"),
        [Text.Encoding]::UTF8.GetBytes("fake-independent-shader-tools"))
    $records = @()
    foreach ($name in @(
        "replaylab-ai.exe", "ReplayLabInternal.exe", "BreadMCP.exe",
        "ReplayLabShaderTools.exe")) {
        $path = Join-Path $Directory $name
        $item = Get-Item -LiteralPath $path
        $records += [ordered]@{
            path = $name
            size = [int64]$item.Length
            sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    [ordered]@{
        schema = "replaylab-runtime-output/v1"
        version = "1.10.1-test"
        generatedUtc = [DateTime]::UtcNow.ToString("o")
        ownedFiles = @(
            "replaylab-ai.exe",
            "ReplayLabInternal.exe",
            "BreadMCP.exe",
            "ReplayLabShaderTools.exe",
            "replaylab-runtime-manifest.json"
        )
        files = $records
    } | ConvertTo-Json -Depth 5 | Set-Content `
        -LiteralPath (Join-Path $Directory "replaylab-runtime-manifest.json") -Encoding UTF8
}

$testsRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$packagingRoot = [IO.Path]::GetFullPath((Split-Path -Parent $testsRoot))
$deployScript = Join-Path $packagingRoot "Deploy-ReplayLabRuntime.ps1"

$tokens = $null
$parseErrors = $null
[Management.Automation.Language.Parser]::ParseFile(
    $deployScript, [ref]$tokens, [ref]$parseErrors) | Out-Null
if ($parseErrors.Count -gt 0) {
    throw ("Deployment script has PowerShell parser errors: " +
        (($parseErrors | ForEach-Object { $_.Message }) -join "; "))
}

$sourceText = Get-Content -LiteralPath $deployScript -Raw
Assert-True (-not $sourceText.Contains("Stop-Process")) `
    "Deployment script must not stop processes."
Assert-True (-not $sourceText.Contains("taskkill")) `
    "Deployment script must not invoke taskkill."
Assert-True (-not ($sourceText -match 'Remove-Item[^\r\n]*-Recurse')) `
    "Deployment script must not recursively remove target content."
Assert-True ($sourceText.Contains('Join-Path $applicationRoot "replaylab"')) `
    "Deployment target must be derived from the qrenderdoc application directory."

$testRoot = Join-Path ([IO.Path]::GetTempPath()) `
    ("replaylab-runtime-deploy-test-" + [Guid]::NewGuid().ToString("N"))
$resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
New-Item -ItemType Directory -Path $resolvedTestRoot | Out-Null

try {
    $runtimeRoot = Join-Path $resolvedTestRoot "runtime-output"
    Set-FakeRuntimeOutput $runtimeRoot

    $appRoot = Join-Path $resolvedTestRoot "application"
    New-Item -ItemType Directory -Path $appRoot | Out-Null
    Set-Content -LiteralPath (Join-Path $appRoot "q_render_soco_doc.exe") `
        -Value "fake-qrenderdoc"

    $validation = & $deployScript -ApplicationDirectory $appRoot `
        -RuntimeOutputDirectory $runtimeRoot -ValidateOnly
    Assert-True ($validation.Status -eq "valid") "ValidateOnly did not validate the layout."
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $appRoot "replaylab"))) `
        "ValidateOnly must not create the ReplayLab target."

    $liveApp = Join-Path $resolvedTestRoot "live-application"
    New-Item -ItemType Directory -Path $liveApp | Out-Null
    $liveExecutable = Join-Path $liveApp "q_render_soco_doc.exe"
    Copy-Item -LiteralPath (Join-Path ([Environment]::SystemDirectory) "ping.exe") `
        -Destination $liveExecutable
    $liveProcess = Start-Process -FilePath $liveExecutable `
        -ArgumentList @("-n", "120", "127.0.0.1") -WindowStyle Hidden -PassThru
    try {
        $processDeadline = [DateTime]::UtcNow.AddSeconds(5)
        $observedPath = ""
        while ([DateTime]::UtcNow -lt $processDeadline) {
            $observed = Get-CimInstance -ClassName Win32_Process `
                -Filter "ProcessId = $($liveProcess.Id)" -ErrorAction SilentlyContinue
            $observedPath = [string]$observed.ExecutablePath
            if (-not [string]::IsNullOrWhiteSpace($observedPath)) {
                break
            }
            Start-Sleep -Milliseconds 50
        }
        Assert-True (-not [string]::IsNullOrWhiteSpace($observedPath)) `
            "The controlled qrenderdoc-name process was not observable."
        Assert-True (([IO.Path]::GetFullPath($observedPath)) -eq `
            ([IO.Path]::GetFullPath($liveExecutable))) `
            "The controlled qrenderdoc-name process did not run from the test application."

        $liveValidation = & $deployScript -ApplicationDirectory $liveApp `
            -RuntimeOutputDirectory $runtimeRoot -ValidateOnly
        Assert-True ($liveValidation.Status -eq "valid") `
            "ValidateOnly must remain read-only when the target qrenderdoc is running."
        Assert-Throws {
            & $deployScript -ApplicationDirectory $liveApp `
                -RuntimeOutputDirectory $runtimeRoot | Out-Null
        } 'qrenderdoc is running from the target application directory|No target files were changed' `
            "Deployment must fail before mutation when the target qrenderdoc is running."
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $liveApp "replaylab"))) `
            "A running target qrenderdoc must be detected before creating the target."

        $otherApp = Join-Path $resolvedTestRoot "other-application"
        New-Item -ItemType Directory -Path $otherApp | Out-Null
        Set-Content -LiteralPath (Join-Path $otherApp "qrenderdoc.exe") `
            -Value "fake-qrenderdoc"
        $otherDeployment = & $deployScript -ApplicationDirectory $otherApp `
            -RuntimeOutputDirectory $runtimeRoot
        Assert-True ($otherDeployment.Status -eq "deployed") `
            "A same-named process from another directory must not block deployment."
    }
    finally {
        if (-not $liveProcess.HasExited) {
            Stop-Process -Id $liveProcess.Id -Force -ErrorAction SilentlyContinue
            $liveProcess.WaitForExit(5000) | Out-Null
        }
        $liveProcess.Dispose()
    }

    $deployment = & $deployScript -ApplicationDirectory $appRoot `
        -RuntimeOutputDirectory $runtimeRoot
    Assert-True ($deployment.Status -eq "deployed") "Deployment did not report success."
    $targetRoot = Join-Path $appRoot "replaylab"
    Assert-True (([IO.Path]::GetFullPath($deployment.TargetDirectory)) -eq
        ([IO.Path]::GetFullPath($targetRoot))) `
        "Deployment must target exactly the application's replaylab child directory."
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $appRoot "BreadMCP.exe"))) `
        "Deployment must not place runtime payloads directly in the application directory."
    foreach ($name in @(
        "replaylab-ai.exe",
        "ReplayLabInternal.exe",
        "BreadMCP.exe",
        "ReplayLabShaderTools.exe",
        "replaylab-runtime-manifest.json",
        ".replaylab-runtime-deployment.json"
    )) {
        Assert-True (Test-Path -LiteralPath (Join-Path $targetRoot $name) -PathType Leaf) `
            "Deployment did not create $name."
    }
    foreach ($name in @(
        "replaylab-ai.exe", "ReplayLabInternal.exe", "BreadMCP.exe",
        "ReplayLabShaderTools.exe")) {
        $sourceHash = (Get-FileHash -LiteralPath (Join-Path $runtimeRoot $name) `
            -Algorithm SHA256).Hash
        $targetHash = (Get-FileHash -LiteralPath (Join-Path $targetRoot $name) `
            -Algorithm SHA256).Hash
        Assert-True ($sourceHash -ceq $targetHash) "Deployed hash mismatch for $name."
    }

    $unrelatedPath = Join-Path $targetRoot "operator-notes.txt"
    Set-Content -LiteralPath $unrelatedPath -Value "preserve me"
    & $deployScript -ApplicationDirectory $appRoot `
        -RuntimeOutputDirectory $runtimeRoot | Out-Null
    Assert-True (Test-Path -LiteralPath $unrelatedPath -PathType Leaf) `
        "A marker-owned redeployment must preserve unrelated files."

    $markerPath = Join-Path $targetRoot ".replaylab-runtime-deployment.json"
    $modernMarker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    $legacyMarker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    $legacyMarker.ownedFiles = @($legacyMarker.ownedFiles | Where-Object {
        $_ -cne "ReplayLabShaderTools.exe"
    })
    $legacyMarker | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $markerPath -Encoding UTF8
    Assert-Throws {
        & $deployScript -ApplicationDirectory $appRoot `
            -RuntimeOutputDirectory $runtimeRoot -ValidateOnly | Out-Null
    } 'does not own the existing ReplayLabShaderTools.exe' `
        "An old deployment marker must not adopt an existing unowned shader runner."
    Remove-Item -LiteralPath (Join-Path $targetRoot "ReplayLabShaderTools.exe") -Force
    $oldManifestPath = Join-Path $targetRoot "replaylab-runtime-manifest.json"
    $oldManifest = Get-Content -LiteralPath $oldManifestPath -Raw | ConvertFrom-Json
    $oldManifest.ownedFiles = @($oldManifest.ownedFiles | Where-Object {
        $_ -cne "ReplayLabShaderTools.exe"
    })
    $oldManifest.files = @($oldManifest.files | Where-Object {
        $_.path -cne "ReplayLabShaderTools.exe"
    })
    $oldManifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $oldManifestPath -Encoding UTF8
    $legacyMarker.sourceManifestSha256 = (
        Get-FileHash -LiteralPath $oldManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $legacyMarker | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $markerPath -Encoding UTF8
    & $deployScript -ApplicationDirectory $appRoot `
        -RuntimeOutputDirectory $runtimeRoot | Out-Null
    $upgradedMarker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    Assert-True (@($upgradedMarker.ownedFiles) -ccontains "ReplayLabShaderTools.exe") `
        "A known three-executable deployment must upgrade its canonical ownership marker."
    Assert-True (Test-Path -LiteralPath (Join-Path $targetRoot "ReplayLabShaderTools.exe") -PathType Leaf) `
        "A known three-executable deployment must gain the verified independent shader runner."

    $interruptedMarker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    $interruptedMarker.state = "incomplete"
    $interruptedMarker | ConvertTo-Json -Depth 4 | Set-Content `
        -LiteralPath $markerPath -Encoding UTF8
    Remove-Item -LiteralPath (Join-Path $targetRoot "BreadMCP.exe") -Force
    & $deployScript -ApplicationDirectory $appRoot `
        -RuntimeOutputDirectory $runtimeRoot | Out-Null
    $recoveredMarker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
    Assert-True ([string]$recoveredMarker.state -ceq "complete") `
        "A retry must recover an incomplete deployment and complete its marker."
    Assert-True (Test-Path -LiteralPath (Join-Path $targetRoot "BreadMCP.exe") `
        -PathType Leaf) "A retry must restore a missing owned payload."

    $unmarkedApp = Join-Path $resolvedTestRoot "unmarked-application"
    $unmarkedTarget = Join-Path $unmarkedApp "replaylab"
    New-Item -ItemType Directory -Path $unmarkedTarget | Out-Null
    Set-Content -LiteralPath (Join-Path $unmarkedApp "qrenderdoc.exe") -Value "fake-qrenderdoc"
    $unknownPath = Join-Path $unmarkedTarget "unknown.bin"
    Set-Content -LiteralPath $unknownPath -Value "unknown"
    Assert-Throws {
        & $deployScript -ApplicationDirectory $unmarkedApp `
            -RuntimeOutputDirectory $runtimeRoot | Out-Null
    } 'not owned by this deployment script' `
        "A non-empty unmarked target must fail closed."
    Assert-True (Test-Path -LiteralPath $unknownPath -PathType Leaf) `
        "Rejected unmarked target content must be preserved."

    $adoptApp = Join-Path $resolvedTestRoot "adopt-application"
    $adoptTarget = Join-Path $adoptApp "replaylab"
    New-Item -ItemType Directory -Path $adoptTarget | Out-Null
    Set-Content -LiteralPath (Join-Path $adoptApp "qrenderdoc.exe") -Value "fake-qrenderdoc"
    foreach ($name in @(
        "replaylab-ai.exe",
        "ReplayLabInternal.exe",
        "BreadMCP.exe",
        "ReplayLabShaderTools.exe",
        "replaylab-runtime-manifest.json"
    )) {
        Copy-Item -LiteralPath (Join-Path $runtimeRoot $name) `
            -Destination (Join-Path $adoptTarget $name)
    }
    $adoptNote = Join-Path $adoptTarget "legacy-operator-note.txt"
    Set-Content -LiteralPath $adoptNote -Value "preserve during adoption"
    Assert-Throws {
        & $deployScript -ApplicationDirectory $adoptApp `
            -RuntimeOutputDirectory $runtimeRoot | Out-Null
    } 'not owned by this deployment script' `
        "Legacy runtime adoption must require an explicit switch."
    $adoption = & $deployScript -ApplicationDirectory $adoptApp `
        -RuntimeOutputDirectory $runtimeRoot -AdoptExistingVerifiedRuntime
    Assert-True ($adoption.Status -eq "adopted") `
        "A byte-identical legacy runtime should be explicitly adoptable."
    Assert-True (Test-Path -LiteralPath $adoptNote -PathType Leaf) `
        "Runtime adoption must preserve unrelated legacy files."
    $adoptMarker = Get-Content `
        -LiteralPath (Join-Path $adoptTarget ".replaylab-runtime-deployment.json") `
        -Raw | ConvertFrom-Json
    Assert-True ([string]$adoptMarker.state -ceq "complete") `
        "An adopted runtime must receive a complete deployment marker."

    $mismatchedAdoptApp = Join-Path $resolvedTestRoot "mismatched-adopt-application"
    $mismatchedAdoptTarget = Join-Path $mismatchedAdoptApp "replaylab"
    New-Item -ItemType Directory -Path $mismatchedAdoptTarget | Out-Null
    Set-Content -LiteralPath (Join-Path $mismatchedAdoptApp "qrenderdoc.exe") `
        -Value "fake-qrenderdoc"
    foreach ($name in @(
        "replaylab-ai.exe",
        "ReplayLabInternal.exe",
        "BreadMCP.exe",
        "ReplayLabShaderTools.exe",
        "replaylab-runtime-manifest.json"
    )) {
        Copy-Item -LiteralPath (Join-Path $runtimeRoot $name) `
            -Destination (Join-Path $mismatchedAdoptTarget $name)
    }
    Add-Content -LiteralPath (Join-Path $mismatchedAdoptTarget "BreadMCP.exe") `
        -Value "legacy mismatch"
    Assert-Throws {
        & $deployScript -ApplicationDirectory $mismatchedAdoptApp `
            -RuntimeOutputDirectory $runtimeRoot -AdoptExistingVerifiedRuntime | Out-Null
    } 'does not match the verified runtime output' `
        "Adoption must reject a legacy payload that differs from verified output."
    Assert-True (-not (Test-Path -LiteralPath `
        (Join-Path $mismatchedAdoptTarget ".replaylab-runtime-deployment.json"))) `
        "Rejected adoption must not write a deployment marker."

    $tamperedRoot = Join-Path $resolvedTestRoot "tampered-runtime"
    Set-FakeRuntimeOutput $tamperedRoot
    Add-Content -LiteralPath (Join-Path $tamperedRoot "BreadMCP.exe") -Value "tampered"
    $cleanApp = Join-Path $resolvedTestRoot "clean-application"
    New-Item -ItemType Directory -Path $cleanApp | Out-Null
    Set-Content -LiteralPath (Join-Path $cleanApp "qrenderdoc.exe") -Value "fake-qrenderdoc"
    Assert-Throws {
        & $deployScript -ApplicationDirectory $cleanApp `
            -RuntimeOutputDirectory $tamperedRoot | Out-Null
    } 'size does not match|SHA-256 does not match' `
        "A payload that differs from its manifest must be rejected."
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $cleanApp "replaylab"))) `
        "Source validation failure must happen before target creation."

    $tamperedShaderRoot = Join-Path $resolvedTestRoot "tampered-shader-runtime"
    Set-FakeRuntimeOutput $tamperedShaderRoot
    Add-Content -LiteralPath (Join-Path $tamperedShaderRoot "ReplayLabShaderTools.exe") -Value "tampered"
    Assert-Throws {
        & $deployScript -ApplicationDirectory $cleanApp `
            -RuntimeOutputDirectory $tamperedShaderRoot | Out-Null
    } 'size does not match|SHA-256 does not match' `
        "The fourth shader runner must be verified as strictly as the Host payloads."
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $cleanApp "replaylab"))) `
        "Shader runner verification failure must precede target creation."

    $unsafeNamesRoot = Join-Path $resolvedTestRoot "unsafe-owned-names-runtime"
    Set-FakeRuntimeOutput $unsafeNamesRoot
    $unsafeManifestPath = Join-Path $unsafeNamesRoot "replaylab-runtime-manifest.json"
    $unsafeManifest = Get-Content -LiteralPath $unsafeManifestPath -Raw | ConvertFrom-Json
    $unsafeManifest.ownedFiles[0] = "..\replaylab-ai.exe"
    $unsafeManifest | ConvertTo-Json -Depth 5 | Set-Content `
        -LiteralPath $unsafeManifestPath -Encoding UTF8
    Assert-Throws {
        & $deployScript -ApplicationDirectory $cleanApp `
            -RuntimeOutputDirectory $unsafeNamesRoot -ValidateOnly | Out-Null
    } 'unsafe filename|must contain' `
        "Runtime manifests with non-canonical owned filenames must be rejected."

    $linkRoot = Join-Path $resolvedTestRoot "runtime-output-link"
    $linkCreated = $false
    try {
        New-Item -ItemType SymbolicLink -Path $linkRoot -Target $runtimeRoot `
            -ErrorAction Stop | Out-Null
        $linkCreated = $true
        Assert-Throws {
            & $deployScript -ApplicationDirectory $cleanApp `
                -RuntimeOutputDirectory $linkRoot -ValidateOnly | Out-Null
        } 'reparse point|symlink|junction' `
            "A runtime output reached through a reparse point must be rejected."
    }
    catch {
        if ($linkCreated) {
            throw
        }
        Write-Host "Symbolic-link safety case skipped because this host cannot create test links."
    }
    finally {
        if ($linkCreated -and (Test-Path -LiteralPath $linkRoot)) {
            Remove-Item -LiteralPath $linkRoot -Force
        }
    }

    $lockedPath = Join-Path $targetRoot "BreadMCP.exe"
    $lock = [IO.File]::Open(
        $lockedPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read)
    try {
        Assert-Throws {
            & $deployScript -ApplicationDirectory $appRoot `
                -RuntimeOutputDirectory $runtimeRoot | Out-Null
        } 'active, locked|No process was stopped' `
            "An active or locked runtime must fail with a clear message."
    }
    finally {
        $lock.Dispose()
    }

    Write-Host "ReplayLab runtime deployment static and safety tests passed."
}
finally {
    $expectedPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($resolvedTestRoot.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolvedTestRoot).StartsWith("replaylab-runtime-deploy-test-")) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
