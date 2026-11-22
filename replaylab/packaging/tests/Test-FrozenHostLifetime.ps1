[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InternalHost,
    [Parameter(Mandatory = $true)][string]$McpHost,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# This probe reads the loaded modules of the frozen Host it starts, and the Hosts
# are 64-bit. A 32-bit process cannot do that: EnumProcessModules against a 64-bit
# target returns only the main image, so Process.Modules reports exactly one entry
# no matter how long you wait -- a failure that looks like a broken build and is
# nothing of the sort. Visual Studio's default MSBuild\Current\Bin\MSBuild.exe is
# 32-bit, and the powershell.exe it launches is redirected by WOW64 to the 32-bit
# copy, so this is the ordinary case rather than an exotic one. Re-launch through
# SysNative (the alias that reaches the real System32 from a 32-bit process) and
# report that run as ours.
if (-not [Environment]::Is64BitProcess -and [Environment]::Is64BitOperatingSystem) {
    $nativeShell = Join-Path $env:WINDIR 'SysNative\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $nativeShell)) {
        throw ('This probe must inspect 64-bit Hosts but is running as a 32-bit process, and ' +
            '64-bit PowerShell was not found at ' + $nativeShell + '. Build with the 64-bit ' +
            'MSBuild (MSBuild\Current\Bin\amd64\MSBuild.exe).')
    }
    & $nativeShell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath `
        -InternalHost $InternalHost -McpHost $McpHost -OutputDirectory $OutputDirectory
    exit $LASTEXITCODE
}

if (Test-Path -LiteralPath $OutputDirectory) { throw 'Use a fresh lifetime probe directory.' }
$probeRoot = [IO.Path]::GetFullPath($OutputDirectory)
$null = New-Item -ItemType Directory -Path $probeRoot
$savedEnvironment = @{}
foreach ($key in @('LOCALAPPDATA', 'REPLAYLAB_INSTANCE_ID', 'RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY')) {
    $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
$records = @()
try {
    foreach ($entry in @(
        @{name='internal'; exe=$InternalHost; state='internal-host.json'},
        @{name='mcp'; exe=$McpHost; state='host.json'})) {
        $directory = Join-Path $probeRoot $entry.name
        $null = New-Item -ItemType Directory -Path $directory
        $runtime = Join-Path $directory 'runtime'
        [Environment]::SetEnvironmentVariable('LOCALAPPDATA', (Join-Path $directory 'local'), 'Process')
        [Environment]::SetEnvironmentVariable('REPLAYLAB_INSTANCE_ID', 'packaging-detached-lifetime', 'Process')
        [Environment]::SetEnvironmentVariable('RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY', (Join-Path $directory 'live-edit.json'), 'Process')
        $executable = (Resolve-Path -LiteralPath $entry.exe).Path
        $child = $null
        $launcher = $null
        $checksComplete = $false
        $record = [ordered]@{host=$entry.name; executable=$executable; launcherExited=$false; resourcesPresent=$false; childExited=$false; passed=$false}
        try {
            $launcher = Start-Process -FilePath $executable -ArgumentList @(
                'ensure', '--runtime-dir', ('"' + $runtime + '"'), '--owner-pid', [string]$PID
            ) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $directory 'ensure.json') -RedirectStandardError (Join-Path $directory 'ensure-stderr.log')
            # Cache the launcher's handle immediately, exactly as done for $child
            # below. Without a cached handle, a launcher that exits quickly reads
            # back ExitCode as $null, and '$null -ne 0' would misreport a
            # successful ensure as 'Frozen ensure failed.'
            $null = $launcher.Handle
            if (-not $launcher.WaitForExit(60000)) { throw 'Frozen ensure did not exit within 60 seconds.' }
            if ($null -eq $launcher.ExitCode) { throw 'Frozen ensure exit code could not be read (handle lost).' }
            $record.launcherExited = $true
            $record.launcherExitCode = $launcher.ExitCode
            if ($launcher.ExitCode -ne 0) { throw 'Frozen ensure failed.' }
            $state = Get-Content -LiteralPath (Join-Path $runtime $entry.state) -Raw | ConvertFrom-Json
            $child = Get-Process -Id ([int]$state.pid) -ErrorAction Stop
            $null = $child.Handle
            $record.childPid = $child.Id
            # Inspect the running child's loaded Python DLL, not an archive in
            # the executable. Its actual extraction directory must survive the
            # ensure bootloader's exit, or later codec/import requests fail.
            # A single sample right after 'ensure' returns can catch the Host
            # with nothing but its own image mapped: a process that has only just
            # been created has not finished mapping its DLLs, and a onefile bundle
            # unpacks itself before Python is loaded at all. What this probe is
            # actually asserting is a *steady state* -- the extraction directory
            # outlives the launcher -- so wait for the runtime to appear instead
            # of reading the module list once and calling it an answer.
            #
            # Process.Modules also throws when the target has exited, cannot be
            # opened, or sits across a bitness boundary; PowerShell turns that into
            # $null, which then pipes one $null into Where-Object and fails with
            # "property ModuleName not found" -- a message about nothing. Each
            # condition below says what actually happened.
            $python = @()
            $loadedModules = @()
            $modulesError = ''
            $deadline = [DateTime]::UtcNow.AddSeconds(60)
            while ([DateTime]::UtcNow -lt $deadline) {
                if ($child.HasExited) {
                    throw ('The detached Host exited before it finished starting (exit code ' +
                        [string]$child.ExitCode + '). A ReplayLab process left over from an ' +
                        'earlier session is the usual cause: it holds the frozen executable ' +
                        'open, so the freshly built one could not replace it. Close ReplayLab ' +
                        'and every ReplayLab*.exe / replaylab-ai.exe still running, then build ' +
                        'again.')
                }
                try {
                    $child.Refresh()
                    $loadedModules = @($child.Modules)
                    $modulesError = ''
                } catch {
                    $loadedModules = @()
                    $modulesError = $_.Exception.Message
                }
                $python = @($loadedModules |
                    Where-Object { $null -ne $_ -and $_.ModuleName -match '^python[0-9]{2,}\.dll$' })
                if ($python.Count -ge 1) { break }
                Start-Sleep -Milliseconds 250
            }
            if ($python.Count -ne 1) {
                $detail = ' Saw ' + [string]$loadedModules.Count +
                    ' loaded module(s) after waiting 60 seconds.'
                if ($modulesError) {
                    $detail = ' The module list could not be read: ' + $modulesError
                }
                throw ('Expected one loaded Python runtime in the detached Host; found ' +
                    [string]$python.Count + '.' + $detail)
            }
            $bundle = Split-Path -Parent $python[0].FileName
            $record.bundlePath = $bundle
            $library = Join-Path $bundle 'base_library.zip'
            $record.resourcesPresent = Test-Path -LiteralPath $library -PathType Leaf
            if (-not $record.resourcesPresent) { throw 'Detached Host lost base_library.zip after ensure exited.' }
            # Windows may retain the already-open ZIP/DLLs while deleting an
            # extension that has not yet been imported. IDNA also needs this
            # lazy Unicode database; checking the ZIP alone misses that case.
            $record.unicodeDatabasePresent = Test-Path -LiteralPath (Join-Path $bundle 'unicodedata.pyd') -PathType Leaf
            if (-not $record.unicodeDatabasePresent) { throw 'Detached Host lost unicodedata.pyd after ensure exited.' }
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $archive = [IO.Compression.ZipFile]::OpenRead($library)
            try {
                if ($null -eq $archive.GetEntry('encodings/idna.pyc')) { throw 'Detached Host encoding archive lacks IDNA.' }
                $record.idnaPresent = $true
            } finally { $archive.Dispose() }
            $checksComplete = $true
        } finally {
            # This unique probe directory is the only service we own. Request
            # its normal shutdown even if the missing-resource assertion fails.
            if (-not (Test-Path -LiteralPath $runtime)) {
                $null = New-Item -ItemType Directory -Path $runtime
            }
            [IO.File]::WriteAllText((Join-Path $runtime 'shutdown.request'), 'packaging lifetime probe complete')
            if ($null -eq $child) {
                $statePath = Join-Path $runtime $entry.state
                if (Test-Path -LiteralPath $statePath) {
                    $ownedState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
                    $child = Get-Process -Id ([int]$ownedState.pid) -ErrorAction SilentlyContinue
                    if ($null -ne $child) { $null = $child.Handle }
                }
            }
            if ($null -ne $child) {
                $record.childExited = $child.WaitForExit(20000)
                if ($record.childExited) { $record.childExitCode = $child.ExitCode }
            }
            if ($null -ne $child -and -not $record.childExited) {
                # This probe started this Host itself, in its own private runtime
                # directory and its own LOCALAPPDATA, so nothing else owns it.
                # Leaving it alive is worse than killing it: it keeps the freshly
                # frozen executable open, and the *next* build then fails somewhere
                # unrelated with an access-denied that points at nothing. A Host
                # that ignored a shutdown request is already a failure this probe
                # reports; it should not also become tomorrow's mystery.
                $record.childKilled = $true
                try { $child.Kill() } catch { $record.childKillError = $_.Exception.Message }
                $null = $child.WaitForExit(10000)
            }
            if ($null -ne $launcher -and -not $launcher.HasExited) {
                $record.launcherExited = $launcher.WaitForExit(10000)
            }
            $record.passed = $checksComplete -and $record.childExited -and $record.childExitCode -eq 0
            $records += $record
            $records | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $probeRoot 'result.json') -Encoding utf8
        }
        if (-not $record.passed) { throw 'Detached Host did not shut down normally with exit code zero.' }
    }
} finally {
    foreach ($key in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], 'Process')
    }
}
Write-Output 'Both frozen Hosts retained their runtime after ensure exited and shut down normally.'
