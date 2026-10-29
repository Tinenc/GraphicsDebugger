param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$gui = Join-Path $root 'x64\Development\qTinecmaTool.exe'
$script = Join-Path $PSScriptRoot 'launch_df_ui.py'
$wegame = 'F:\WeGame\wegame.exe'
foreach ($file in @($gui, $script, $wegame)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { Write-Host "File not found: $file"; exit 1 }
}
if (Get-Process -Name 'wegame' -ErrorAction SilentlyContinue) {
    Write-Host 'WeGame is already running. Exit WeGame completely, then run this script again.'
    exit 2
}
$env:DISABLE_VULKAN_TINECMATOOL_CAPTURE_1_44 = '1'
Start-Process -FilePath $gui -WorkingDirectory (Split-Path -Parent $gui) -ArgumentList @('--ui-script', ('"' + $script + '"')) | Out-Null
Write-Host 'TinecmaTool is starting. It will launch F:\WeGame\wegame.exe from its Launch page.'
Write-Host 'Log in and launch DF from WeGame. Check the DF live capture page and verify a saved RDC.'
