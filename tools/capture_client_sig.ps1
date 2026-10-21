# capture_client_sig.ps1
# Capture the "launch signature" of the running Client-Win64-Shipping.exe
# so we can tell WHAT kuro is actually checking (ancestry vs cmdline token vs env).
#
# How to use:
#   1. Start the game the NORMAL way: double-click C:\Wuthering Waves\launcher.exe
#      and let it reach the main lobby.
#   2. Run this script. It prints the Client-Win64 command line, parent chain,
#      and the whole launcher process tree.
#   3. Save the output (Tee-Object) and send it to me, or compare it yourself
#      against the RenderDoc-Launch instance.
#
#   powershell -ExecutionPolicy Bypass -File capture_client_sig.ps1 | Tee-Object client_sig.txt

$ErrorActionPreference = "SilentlyContinue"

Write-Host "=== 1. Find the running Client-Win64-Shipping.exe ==="
$cl = Get-CimInstance Win32_Process -Filter "Name='Client-Win64-Shipping.exe'"
if (-not $cl) {
    Write-Host "  Not found. Start the game via C:\Wuthering Waves\launcher.exe first, then re-run." -ForegroundColor Red
    exit 1
}

foreach ($p in $cl) {
    Write-Host ""
    Write-Host ("PID: {0}   PPID: {1}   started: {2}" -f $p.ProcessId, $p.ParentProcessId, $p.CreationDate) -ForegroundColor Cyan
    Write-Host "--- command line (if kuro uses a cmdline token, it is here) ---"
    Write-Host ("  " + $p.CommandLine)

    $pp = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $p.ParentProcessId)
    if ($pp) {
        Write-Host "--- parent process ---"
        Write-Host ("  PPID {0} = {1}" -f $p.ParentProcessId, $pp.Name)
        Write-Host ("  parent cmdline: " + $pp.CommandLine)
        $ppp = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $pp.ParentProcessId)
        if ($ppp) {
            Write-Host ("  grandparent {0} = {1}" -f $pp.ParentProcessId, $ppp.Name)
        }
    }
}

Write-Host ""
Write-Host "=== 2. Whole launcher / game process tree (running now) ==="
Get-CimInstance Win32_Process -Filter "Name LIKE '%Wuthering%' OR Name LIKE '%launcher%' OR Name LIKE '%Client-Win64%' OR Name LIKE '%KRLauncher%'" |
    ForEach-Object {
        $cmd = ($_.CommandLine -replace "\s+", " ")
        if ($cmd.Length -gt 160) { $cmd = $cmd.Substring(0,160) }
        "  PID={0,-7} PPID={1,-7} {2}  cmd={3}" -f $_.ProcessId, $_.ParentProcessId, $_.Name, $cmd
    }

Write-Host ""
Write-Host "=== 3. Compare the two launch paths ==="
Write-Host "   Normal (launcher)  vs  RenderDoc-Launch  instance of Client-Win64."
Write-Host "   The DIFFERENCE in cmdline / parent chain is the marker to spoof."
