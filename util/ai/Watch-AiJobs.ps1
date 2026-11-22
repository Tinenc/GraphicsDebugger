<#
.SYNOPSIS
    Run a fixed set of build and test jobs on request, so a remote assistant can
    compile and test on this machine without a person driving each run.

.DESCRIPTION
    This is deliberately not a remote shell. It watches one folder for a request
    file whose entire content is a job *name*, looks that name up in the table
    below, and runs the command this script already contains. Nothing in a
    request file is ever executed, passed to a shell, or used to build a command
    line, so the worst a bad request can do is run one of the jobs listed by
    -List, which is exactly what you could run yourself from the repository.

    Requests arrive as <REPO>\output\ai-jobs\<anything>.job
    Results appear  as <REPO>\output\ai-jobs\<same name>.log  (UTF-8)
                   and <REPO>\output\ai-jobs\<same name>.exit (the exit code)
    The request is then renamed to .job.done so it never runs twice.

    Stop it with Ctrl+C, or by creating <REPO>\output\ai-jobs\stop.flag.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File util\ai\Watch-AiJobs.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File util\ai\Watch-AiJobs.ps1 -List
#>
[CmdletBinding()]
param(
    [string] $Repo = '',
    [double] $PollSeconds = 3,
    [switch] $List
)

$ErrorActionPreference = 'Stop'

if(-not $Repo)
{
    # $PSScriptRoot is not populated while param() defaults are evaluated in
    # Windows PowerShell 5.1, so the repository root is resolved here instead.
    $here = $PSScriptRoot
    if(-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $Repo = Split-Path -Parent (Split-Path -Parent $here)
}
if(-not (Test-Path -LiteralPath (Join-Path $Repo 'renderdoc.sln')))
{
    throw "Repo '$Repo' does not look like the renderdocUltra checkout; pass -Repo."
}

$scripts = Join-Path $Repo 'output\shader-workspace-20260906'
$venv    = Join-Path $Repo 'replaylab\.venv\Scripts\python.exe'

# The whole allowlist. A job name that is not a key here is refused.
$Jobs = [ordered]@{
    'build-qrenderdoc' = @{
        Description = 'Rebuild the qrenderdoc UI (C++, Development x64) and redeploy the bridge.'
        NeedsClosed = $true
        # <nul so the script's trailing `pause` returns instead of waiting forever.
        Run = { & cmd.exe /c ('call "{0}" <nul' -f (Join-Path $scripts 'REBUILD_QRENDERDOC.cmd')) }
    }
    'build-host-bridge' = @{
        Description = 'Rebuild the ReplayLab Host and bridge only (no C++).'
        NeedsClosed = $false
        Run = { & cmd.exe /c ('call "{0}" <nul' -f (Join-Path $scripts 'REBUILD_HOST_AND_BRIDGE.cmd')) }
    }
    'i18n' = @{
        Description = 'Regenerate the zh_CN catalog and .qm without building anything else.'
        NeedsClosed = $false
        Run = {
            & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Repo 'util\i18n\update_translations.ps1')
            & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Repo 'util\i18n\build_translations.ps1')
        }
    }
    'test-host' = @{
        Description = 'Run the ReplayLabMCP (Host) test suite.'
        NeedsClosed = $false
        Run = {
            Push-Location (Join-Path $Repo 'replaylab\mcp_host')
            try { & $venv -X utf8 -m pytest -q --tb=short -p no:cacheprovider --basetemp=$env:TEMP\pt-host }
            finally { Pop-Location }
        }
    }
    'test-agent' = @{
        Description = 'Run the AgentHost test suite.'
        NeedsClosed = $false
        Run = {
            Push-Location (Join-Path $Repo 'replaylab')
            try { & $venv -X utf8 -m pytest -q --tb=short -p no:cacheprovider --basetemp=$env:TEMP\pt-agent }
            finally { Pop-Location }
        }
    }
}

if($List)
{
    $Jobs.GetEnumerator() | ForEach-Object {
        '{0,-18} {1}' -f $_.Key, $_.Value.Description
    }
    return
}

$inbox = Join-Path $Repo 'output\ai-jobs'
New-Item -ItemType Directory -Force -Path $inbox | Out-Null
$stop = Join-Path $inbox 'stop.flag'
Remove-Item -LiteralPath $stop -Force -ErrorAction SilentlyContinue

Write-Host "Watching $inbox"
Write-Host ("Jobs: " + ($Jobs.Keys -join ', '))
Write-Host "Ctrl+C, or create stop.flag in that folder, to stop."

function Invoke-AiJob
{
    param([System.IO.FileInfo] $Request)

    $name = [System.IO.Path]::GetFileNameWithoutExtension($Request.Name)
    $log  = Join-Path $Request.DirectoryName ($name + '.log')
    $code = Join-Path $Request.DirectoryName ($name + '.exit')
    $wanted = (Get-Content -LiteralPath $Request.FullName -Raw -ErrorAction SilentlyContinue)
    $wanted = ($wanted -replace '\s', '')

    if(-not $Jobs.Contains($wanted))
    {
        $text = "refused: '$wanted' is not a job this watcher knows. Known: " + ($Jobs.Keys -join ', ')
        Set-Content -LiteralPath $log -Value $text -Encoding UTF8
        Set-Content -LiteralPath $code -Value '2' -Encoding UTF8
        Write-Host "  $name -> $text"
        return
    }

    $job = $Jobs[$wanted]
    if($job.NeedsClosed -and (Get-Process -Name 'q_render_soco_doc' -ErrorAction SilentlyContinue))
    {
        $text = 'refused: close qrenderdoc first, then send this request again.'
        Set-Content -LiteralPath $log -Value $text -Encoding UTF8
        Set-Content -LiteralPath $code -Value '3' -Encoding UTF8
        Write-Host "  $name -> $text"
        return
    }

    Write-Host ("  {0} -> running {1} ..." -f $name, $wanted) -NoNewline
    $started = Get-Date
    # A build writes warnings to stderr; with 2>&1 those arrive as error records,
    # which must not abort the watcher. The job's own exit code is the verdict.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    try     { $output = & $job.Run 2>&1 | Out-String }
    catch   { $output = "watcher caught: $($_.Exception.Message)"; $global:LASTEXITCODE = 1 }
    finally { $ErrorActionPreference = $previous }
    $exit = $global:LASTEXITCODE
    if($null -eq $exit) { $exit = 0 }
    $header = "job: $wanted`r`nstarted: $started`r`nfinished: $(Get-Date)`r`nexit: $exit`r`n" + ('-' * 60) + "`r`n"
    Set-Content -LiteralPath $log -Value ($header + $output) -Encoding UTF8
    Set-Content -LiteralPath $code -Value $exit -Encoding UTF8
    Write-Host (" exit={0} ({1:n0}s)" -f $exit, ((Get-Date) - $started).TotalSeconds)
}

while(-not (Test-Path -LiteralPath $stop))
{
    foreach($request in @(Get-ChildItem -LiteralPath $inbox -Filter '*.job' -File -ErrorAction SilentlyContinue | Sort-Object CreationTime))
    {
        try     { Invoke-AiJob -Request $request }
        catch   { Write-Host ("  {0} -> watcher error: {1}" -f $request.Name, $_.Exception.Message) }
        finally { Move-Item -LiteralPath $request.FullName -Destination ($request.FullName + '.done') -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Seconds $PollSeconds
}

Write-Host 'Stopped.'
