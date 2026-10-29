$path = "C:\Users\Administrator\Desktop\COD_RDC_cod_frame0.rdc"
try {
    $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    Write-Output ("Opened successfully! Length: " + $stream.Length)
    $buf = New-Object byte[] 64
    $read = $stream.Read($buf, 0, 64)
    Write-Output ("Read bytes: " + $read)
    $hex = [System.BitConverter]::ToString($buf, 0, 32)
    Write-Output ("Hex: " + $hex)
    $stream.Close()
} catch {
    Write-Output ("Error: " + $_.Exception.Message)
}
