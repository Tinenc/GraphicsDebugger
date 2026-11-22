[CmdletBinding()]
param([string]$Capture = '')
$app = Join-Path $PSScriptRoot 'x64\BreadDevelopment'
$executable = Join-Path $app 'qTinecmaTool.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw 'Run build_bread.ps1 first. See docs/Bread_DF_工具使用说明书.md.' }
$launch = @{FilePath=$executable; WorkingDirectory=$app}
if ($Capture) {
    $path = (Resolve-Path -LiteralPath $Capture -ErrorAction Stop).Path
    $launch.ArgumentList = '"'+$path+'"'
}
Start-Process @launch
