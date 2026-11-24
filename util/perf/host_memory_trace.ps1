# Rotate / start a fresh Bread Host memory-trace run.
# Auto-release after every MCP tool is Host-owned; Agents never call cleanup.
param(
    [ValidateSet("archive", "tail", "status")]
    [string]$Action = "archive"
)

$ErrorActionPreference = "Stop"
$logDir = Join-Path $env:LOCALAPPDATA "ReplayLab\logs"
$trace = Join-Path $logDir "host-memory-trace.jsonl"

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

switch ($Action) {
    "archive" {
        if (Test-Path $trace) {
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $dest = Join-Path $logDir ("host-memory-trace.{0}.jsonl" -f $stamp)
            Move-Item -LiteralPath $trace -Destination $dest -Force
            Write-Host "Archived previous trace to:"
            Write-Host $dest
        } else {
            Write-Host "No existing host-memory-trace.jsonl (fresh start)."
        }
        Write-Host ""
        Write-Host "New run will append to:"
        Write-Host $trace
        Write-Host ""
        Write-Host "Next: close qrenderdoc if needed, deploy integration, restart qrenderdoc,"
        Write-Host "then reproduce the AI task. Host writes begin/end/cache-release/post-release/poll rows automatically."
    }
    "tail" {
        if (-not (Test-Path $trace)) {
            Write-Host "Trace not created yet: $trace"
            exit 1
        }
        Get-Content -LiteralPath $trace -Tail 40 -Wait
    }
    "status" {
        Write-Host "Trace path: $trace"
        if (Test-Path $trace) {
            $info = Get-Item -LiteralPath $trace
            Write-Host ("Size: {0:N1} KB; LastWrite: {1}" -f ($info.Length / 1KB), $info.LastWriteTime)
            Write-Host "Last 5 kinds:"
            Get-Content -LiteralPath $trace -Tail 5 | ForEach-Object {
                try {
                    $row = $_ | ConvertFrom-Json
                    "{0}  tool={1}  kind={2}" -f $row.unixNs, $row.tool, $row.kind
                } catch {
                    $_
                }
            }
        } else {
            Write-Host "File does not exist yet (Host creates it on first tool/sample)."
        }
    }
}
