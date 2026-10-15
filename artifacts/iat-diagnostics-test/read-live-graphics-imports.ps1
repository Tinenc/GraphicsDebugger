param(
  [Parameter(Mandatory=$true)][int]$TargetPid,
  [Parameter(Mandatory=$true)][string]$OutputPath
)
$ErrorActionPreference = 'Stop'
$report = [ordered]@{ pid=$TargetPid; time=(Get-Date -Format o); mode='read-only'; error=$null }
$report.administrator = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
try {
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class GraphicsImportReader {
  [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool ReadProcessMemory(IntPtr process, IntPtr address, byte[] data, UIntPtr size, out UIntPtr read);
  [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern uint K32GetMappedFileNameW(IntPtr process, IntPtr address, System.Text.StringBuilder name, uint size);
}
'@
  $targetProcess = Get-Process -Id $TargetPid
  if($targetProcess.ProcessName -ne 'DeltaForceClient-Win64-Shipping') { throw 'PID is not the expected DF executable' }
  $report.processStart = $targetProcess.StartTime.ToString('o')
  $readProbe = [GraphicsImportReader]::OpenProcess(0x410, $false, $TargetPid)
  $report.openProcessError = if($readProbe -eq [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::GetLastWin32Error() } else { 0 }
  if($readProbe -eq [IntPtr]::Zero) { throw "OpenProcess query/read denied: $($report.openProcessError)" }
  [void][GraphicsImportReader]::CloseHandle($readProbe)
  $loadedModules = $targetProcess.Modules
  $moduleList = @($loadedModules | Where-Object { $null -ne $_ } | ForEach-Object {
    [pscustomobject]@{name=$_.ModuleName; path=$_.FileName; base=[uint64]$_.BaseAddress.ToInt64(); size=[uint64]$_.ModuleMemorySize}
  })
  $report.modules = @($moduleList | Where-Object { $_.name -match 'DeltaForceClient|d3d12|dxgi|TinecmaTool|sl\.interposer' })
  $processHandle = [GraphicsImportReader]::OpenProcess(0x410, $false, $TargetPid)
  if($processHandle -eq [IntPtr]::Zero) { throw "OpenProcess failed: $([Runtime.InteropServices.Marshal]::GetLastWin32Error())" }
  try {
    function Read-TargetBytes([uint64]$Address, [int]$Count) {
      if($Count -le 0 -or $Count -gt 65536) { throw 'Invalid read size' }
      $buffer = New-Object byte[] $Count
      $actual = [UIntPtr]::Zero
      if(-not [GraphicsImportReader]::ReadProcessMemory($processHandle,[IntPtr]::new([long]$Address),$buffer,[UIntPtr]::new([uint64]$Count),[ref]$actual)) {
        throw ('ReadProcessMemory failed: {0}, address=0x{1:x}' -f [Runtime.InteropServices.Marshal]::GetLastWin32Error(),$Address)
      }
      if($actual.ToUInt64() -ne $Count) { throw 'Incomplete read' }
      return ,$buffer
    }
    function Find-TargetOwner([uint64]$Address) {
      $owner = $moduleList | Where-Object { $Address -ge $_.base -and $Address-$_.base -lt $_.size } | Select-Object -First 1
      if($owner) { return ('{0}+0x{1:x}' -f $owner.path,($Address-$owner.base)) }
      $mappedName = New-Object Text.StringBuilder 1024
      if([GraphicsImportReader]::K32GetMappedFileNameW($processHandle,[IntPtr]::new([long]$Address),$mappedName,1024) -gt 0) { return $mappedName.ToString() }
      return 'not in enumerated module ranges'
    }
    if($moduleList.Count -eq 0) {
      $report.moduleEnumeration = 'Unavailable; reading main image at base 0x140000000 recorded by the capture log'
      $mainHeader = Read-TargetBytes 0x140000000 4096
      $mainNt = [BitConverter]::ToInt32($mainHeader,60)
      if([BitConverter]::ToUInt16($mainHeader,0) -ne 0x5a4d -or $mainNt -lt 0 -or $mainNt+264 -gt 4096) { throw 'Main image header invalid' }
      $moduleList = @([pscustomobject]@{name='DeltaForceClient-Win64-Shipping.exe'; path='main image (capture-log base)'; base=[uint64]0x140000000; size=[uint64][BitConverter]::ToUInt32($mainHeader,$mainNt+24+56)})
    }
    $report.images = @(
      foreach($image in ($moduleList | Where-Object { $_.name -match '^DeltaForceClient' })) {
        try {
          $header = Read-TargetBytes $image.base 4096
          if([BitConverter]::ToUInt16($header,0) -ne 0x5a4d) { throw 'Not a PE image' }
          $ntOffset = [BitConverter]::ToInt32($header,60)
          if($ntOffset -lt 0 -or $ntOffset+264 -gt $header.Length) { throw 'PE header outside initial page' }
          if([BitConverter]::ToUInt32($header,$ntOffset) -ne 0x4550 -or [BitConverter]::ToUInt16($header,$ntOffset+24) -ne 0x20b) { throw 'Not PE32+' }
          function Read-ImageBytes([uint64]$Rva,[int]$Count) {
            if($Rva -ge $image.size -or [uint64]$Count -gt $image.size-$Rva) { throw 'RVA outside image' }
            Read-TargetBytes ($image.base+$Rva) $Count
          }
          function Read-ImageString([uint32]$Rva) {
            $stringBytes = Read-ImageBytes $Rva ([int][Math]::Min(256,$image.size-$Rva))
            $end = [Array]::IndexOf($stringBytes,[byte]0)
            if($end -lt 0) { throw 'Unterminated import string' }
            [Text.Encoding]::ASCII.GetString($stringBytes,0,$end)
          }
          $directoryOffset=$ntOffset+24+112
          $importRva=[BitConverter]::ToUInt32($header,$directoryOffset+8)
          $importSize=[BitConverter]::ToUInt32($header,$directoryOffset+12)
          $delayRva=[BitConverter]::ToUInt32($header,$directoryOffset+104)
          $delaySize=[BitConverter]::ToUInt32($header,$directoryOffset+108)
          $imageReport=[ordered]@{name=$image.name; base=('0x{0:x}' -f $image.base); importRva=('0x{0:x}' -f $importRva); importSize=$importSize; delayRva=('0x{0:x}' -f $delayRva); delaySize=$delaySize; imports=@(); delayGraphics=@()}
          if($importRva -ne 0) {
            for($descriptorIndex=0;$descriptorIndex -lt [Math]::Min(64,[Math]::Floor($importSize/20));$descriptorIndex++) {
              $descriptor=Read-ImageBytes ($importRva+20*$descriptorIndex) 20
              $nameRva=[BitConverter]::ToUInt32($descriptor,12)
              $firstThunk=[BitConverter]::ToUInt32($descriptor,16)
              if($nameRva -eq 0 -and $firstThunk -eq 0) { break }
              $imageReport.imports += [pscustomobject]@{dll=(Read-ImageString $nameRva); originalFirstThunk=('0x{0:x}' -f [BitConverter]::ToUInt32($descriptor,0)); firstThunk=('0x{0:x}' -f $firstThunk)}
            }
          }
          if($delayRva -ne 0) {
            for($descriptorIndex=0;$descriptorIndex -lt [Math]::Min(64,[Math]::Floor($delaySize/32));$descriptorIndex++) {
              $descriptor=Read-ImageBytes ($delayRva+32*$descriptorIndex) 32
              $nameRva=[BitConverter]::ToUInt32($descriptor,4)
              if($nameRva -eq 0) { break }
              if(([BitConverter]::ToUInt32($descriptor,0) -band 1) -eq 0) { throw 'Unsupported VA delay descriptor' }
              $dllName=Read-ImageString $nameRva
              if($dllName -notmatch '^(d3d12|dxgi)\.dll$') { continue }
              $iatRva=[BitConverter]::ToUInt32($descriptor,12)
              $intRva=[BitConverter]::ToUInt32($descriptor,16)
              for($slot=0;$slot -lt 256;$slot++) {
                $lookup=[BitConverter]::ToUInt64((Read-ImageBytes ($intRva+8*$slot) 8),0)
                if($lookup -eq 0) { break }
                $name=if(($lookup -shr 63) -ne 0) { 'ordinal:{0}' -f ($lookup -band 65535) } else { Read-ImageString ([uint32]$lookup+2) }
                $value=[BitConverter]::ToUInt64((Read-ImageBytes ($iatRva+8*$slot) 8),0)
                $imageReport.delayGraphics += [pscustomobject]@{dll=$dllName; function=$name; slotAddress=('0x{0:x}' -f ($image.base+$iatRva+8*$slot)); target=('0x{0:x}' -f $value); owner=(Find-TargetOwner $value)}
              }
            }
          }
          [pscustomobject]$imageReport
        } catch { [pscustomobject]@{name=$image.name; error=$_.Exception.Message} }
      }
    )
  } finally { [void][GraphicsImportReader]::CloseHandle($processHandle) }
} catch { $report.error=$_.Exception.Message }
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
