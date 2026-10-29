$p1 = Get-Process -Id 49388
$t1 = @{}
foreach ($t in $p1.Threads) {
    $t1[$t.Id] = $t.TotalProcessorTime.TotalMilliseconds
}
Start-Sleep -Seconds 3
$p2 = Get-Process -Id 49388
$active = @()
foreach ($t in $p2.Threads) {
    $old = 0
    if ($t1.ContainsKey($t.Id)) { $old = $t1[$t.Id] }
    $diff = $t.TotalProcessorTime.TotalMilliseconds - $old
    if ($diff -gt 10) {
        $active += [PSCustomObject]@{
            TID = $t.Id
            DiffMs = [math]::Round($diff, 1)
            TotalSec = [math]::Round($t.TotalProcessorTime.TotalSeconds, 2)
            State = $t.ThreadState
            WaitReason = $t.WaitReason
        }
    }
}
$active | Sort-Object DiffMs -Descending | Format-Table
