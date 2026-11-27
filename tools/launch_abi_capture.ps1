param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$gui = Join-Path $root 'x64\BreadDevelopment\qTinecmaTool.exe'
$x86Helper = Join-Path $root 'x64\BreadDevelopment\x86\TinecmaToolcmd.exe'
$script = Join-Path $PSScriptRoot 'launch_abi_ui.py'
$wegame = 'F:\WeGame\wegame.exe'
foreach ($file in @($gui, $x86Helper, $script, $wegame)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { Write-Host "File not found: $file"; exit 1 }
}
if (Get-Process -Name 'wegame' -ErrorAction SilentlyContinue) {
    Write-Host 'WeGame is already running. Exit WeGame completely, then run this script again.'
    exit 2
}
$env:DISABLE_VULKAN_TINECMATOOL_CAPTURE_1_44 = '1'
$env:DISABLE_VULKAN_TINECMATOOL_CAPTURE_1_46 = '1'
Start-Process -FilePath $gui -WorkingDirectory (Split-Path -Parent $gui) -ArgumentList @('--ui-script', ('"' + $script + '"')) | Out-Null
Write-Host 'TinecmaTool (BreadDevelopment) is starting. It will launch F:\WeGame\wegame.exe from its Launch page.'
Write-Host 'Log in and launch Arena Breakout Infinite (2001688) from WeGame.'
Write-Host 'GameLoader.exe inline CreateProcess hooks will propagate capture into UAGame.exe.'
