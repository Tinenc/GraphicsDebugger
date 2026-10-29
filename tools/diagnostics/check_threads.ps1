$p = Get-Process -Id 49388
$threads = $p.Threads | Where-Object { $_.TotalProcessorTime.TotalSeconds -gt 1 } | Sort-Object { $_.TotalProcessorTime.TotalSeconds } -Descending
foreach ($t in $threads) {
    Write-Output ("TID: " + $t.Id + " State: " + $t.ThreadState + " WaitReason: " + $t.WaitReason + " CPU: " + [math]::Round($t.TotalProcessorTime.TotalSeconds, 2))
}
