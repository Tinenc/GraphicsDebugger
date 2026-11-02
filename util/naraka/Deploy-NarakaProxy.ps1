<#
.SYNOPSIS
    Deploy or remove the Naraka proxy layer (dxgi.dll / d3d11.dll / d3d12.dll)
    in the game's program directory.

.DESCRIPTION
    The proxies are the only capture route left for Naraka: injection into
    LauncherGame.exe is pointless (it exits), injection into StartGame.exe blocks
    Neac, and patching the game's own imports from inside is undone by VMProtect.
    Becoming one of the game's imports avoids all three.

    Nothing about this is irreversible, and nothing here touches the parts of the
    install the game validates:
      * the original tempdx12.txt / ExtraVars.unityargs are never written
      * no executable is patched, renamed, or re-signed
      * only three DLLs are added, and removal restores the exact prior state
        (a manifest with SHA-256 is written before any change)

    Dry-run is the default. Nothing is copied without -Apply.

.PARAMETER GameDir
    The game's program directory. Default F:\Naraka\program.

.PARAMETER Apply
    Actually copy the files. Without this the script only reports what it would do.

.PARAMETER Undeploy
    Remove the proxies and restore anything they displaced.

.EXAMPLE
    # what would happen, with no side effects
    .\Deploy-NarakaProxy.ps1

.EXAMPLE
    # deploy
    .\Deploy-NarakaProxy.ps1 -Apply

.EXAMPLE
    # remove everything again
    .\Deploy-NarakaProxy.ps1 -Undeploy -Apply
#>

[CmdletBinding()]
param(
    [string]$GameDir = 'F:\Naraka\program',
    [switch]$Apply,
    [switch]$Undeploy,
    [switch]$ForceProtected
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ProxyDir = Join-Path $RepoRoot 'tools\proxy'
$Core     = Join-Path $RepoRoot 'x64\Development\TinecmaTool.dll'
$Manifest = Join-Path $GameDir '.naraka_proxy_manifest.json'
$ProxyNames = @('dxgi.dll', 'd3d11.dll', 'd3d12.dll')
$WorkDir = Join-Path $env:LOCALAPPDATA 'Temp\Naraka\proxy'

function Write-Step($msg) { Write-Host "  $msg" }
function Write-Ok($msg)   { Write-Host "  [ok]   $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "  [warn] $msg" -ForegroundColor Yellow }
function Write-Bad($msg)  { Write-Host "  [FAIL] $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "=== Naraka proxy layer: $($(if ($Undeploy) { 'undeploy' } else { 'deploy' })) ===" -ForegroundColor Cyan
Write-Host "  game dir : $GameDir"
Write-Host "  mode     : $(if ($Apply) { 'APPLY' } else { 'DRY RUN (add -Apply to act)' })"
Write-Host ""

# ---------------------------------------------------------------- preflight --
Write-Host "[1/4] Preflight"

# Measured on 2026-11-02, and the reason this route is blocked: with the three
# DLLs in the game directory the game launched and rendered (wrapped D3D12 AND
# D3D11 devices, "Graphics first present" logged), but the protection raised a
# warning on entering the game and tore the graphics devices down seconds later.
# The files in the install directory are visible to the anti-cheat; the capture
# machinery itself was working.
#
# Deploying this is therefore opt-in behind a switch rather than something a
# stray run can do. See docs/NARAKA_代理层实测_2026-11-02.md.
if (-not $Undeploy -and -not $ForceProtected) {
    Write-Host ""
    Write-Host "  REFUSING TO DEPLOY" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Adding dxgi/d3d11/d3d12 to the game directory is detected by the"
    Write-Host "  anti-cheat: on 2026-11-02 it warned on entering the game and then"
    Write-Host "  shut the graphics devices down. The proxies worked; being visible"
    Write-Host "  in the install directory is what failed."
    Write-Host ""
    Write-Host "  The retained D3D12 factory bridge lives inside the core and needs"
    Write-Host "  no files in the game directory. Use that."
    Write-Host ""
    Write-Host "  To deploy anyway (accepting the warning and the ban risk):"
    Write-Host "    .\Deploy-NarakaProxy.ps1 -ForceProtected -Apply"
    Write-Host ""
    exit 2
}

if (-not (Test-Path $GameDir)) { Write-Bad "game dir not found: $GameDir"; exit 1 }
$GameExe = Join-Path $GameDir 'NarakaBladepoint.exe'
if (-not (Test-Path $GameExe)) { Write-Bad "NarakaBladepoint.exe not in $GameDir"; exit 1 }
Write-Ok "game install present"

$running = @()
foreach ($n in @('NarakaBladepoint', 'StartGame', 'LootHoarder', 'LauncherGame')) {
    $p = Get-Process -Name $n -ErrorAction SilentlyContinue
    if ($p) { $running += $n }
}
if ($running.Count -gt 0) {
    Write-Bad "game processes are running: $($running -join ', ')"
    Write-Bad "close the game and its launcher before changing files underneath it"
    exit 1
}
Write-Ok "no game process running"

# The anti-cheat services are only observed. Stopping or reconfiguring them is
# out of scope and would itself be a modification of the protected environment.
$ace = Get-Service -Name 'ACE-*','Neac*' -ErrorAction SilentlyContinue
if ($ace) {
    Write-Step "anti-cheat services present: $(($ace | ForEach-Object { "$($_.Name)=$($_.StartType)" }) -join ', ')"
}

if (-not $Undeploy) {
    if (-not (Test-Path $Core)) { Write-Bad "core not built: $Core"; exit 1 }
    Write-Ok "core present: $Core"

    $blob = [System.IO.File]::ReadAllBytes($Core)
    $text = [System.Text.Encoding]::ASCII.GetString($blob)
    $hasBridge = $text.Contains('TINECMATOOL_D3D11CreateDevice') -and
                 $text.Contains('TINECMATOOL_D3D12CreateDevice')
    if (-not $hasBridge) {
        Write-Bad "TinecmaTool.dll has no proxy bridge exports."
        Write-Bad "Rebuild the core first:"
        Write-Bad "  msbuild renderdoc.sln /m /p:Configuration=Development /p:Platform=x64"
        Write-Bad "Deploying without it is harmless (the proxy refuses to load the core)"
        Write-Bad "but captures nothing, so there is no reason to install files for it."
        exit 1
    }
    Write-Ok "core exports the proxy bridge"

    foreach ($n in $ProxyNames) {
        $p = Join-Path $ProxyDir $n
        if (-not (Test-Path $p)) { Write-Bad "proxy not built: $p"; exit 1 }
    }
    Write-Ok "all three proxies built"
}

# ------------------------------------------------------------------ current --
Write-Host ""
Write-Host "[2/4] Current state of the game directory"

$existing = @{}
$ourHashes = @{}
foreach ($n in $ProxyNames) {
    $mp = Join-Path $ProxyDir $n
    if (Test-Path $mp) { $ourHashes[$n] = (Get-FileHash $mp -Algorithm SHA256).Hash }
}

foreach ($n in $ProxyNames) {
    $p = Join-Path $GameDir $n
    if (Test-Path $p) {
        $h = (Get-FileHash $p -Algorithm SHA256).Hash
        $existing[$n] = $h
        $isOurs = ($ourHashes.ContainsKey($n) -and $ourHashes[$n] -eq $h)
        Write-Step "$n present ($(if ($isOurs) { 'ours - already installed' } else { 'NOT ours - will be backed up' }))"
    } else {
        Write-Step "$n absent"
    }
}

if ($Undeploy) {
    # ------------------------------------------------------------- undeploy --
    Write-Host ""
    Write-Host "[3/4] Removing proxies"

    if (-not (Test-Path $Manifest)) {
        Write-Warn2 "no manifest found - only removing files that match our build"
    }

    foreach ($n in $ProxyNames) {
        $p = Join-Path $GameDir $n
        if (-not (Test-Path $p)) { continue }
        $mp = Join-Path $ProxyDir $n
        $ours = $false
        if (Test-Path $mp) {
            $ours = ((Get-FileHash $p -Algorithm SHA256).Hash -eq (Get-FileHash $mp -Algorithm SHA256).Hash)
        }
        if ($ours) {
            if ($Apply) { Remove-Item $p -Force; Write-Ok "removed $n" }
            else        { Write-Step "would remove $n" }
        } else {
            Write-Warn2 "$n is not our file - leaving it alone"
        }
    }

    if (Test-Path $Manifest) {
        $m = Get-Content $Manifest -Raw | ConvertFrom-Json
        foreach ($b in $m.backups) {
            $dst = Join-Path $GameDir $b.name
            # A manifest written by the earlier, buggy version may list our own
            # binaries as "originals". Restoring those would reinstall the proxy.
            if ($ourHashes.ContainsKey($b.name) -and $ourHashes[$b.name] -eq $b.sha256) {
                Write-Warn2 "$($b.name) in the manifest is our own build - not restoring it"
                continue
            }
            if ($Apply) {
                Copy-Item $b.stash $dst -Force
                Write-Ok "restored $($b.name) from backup"
            } else {
                Write-Step "would restore $($b.name) from $($b.stash)"
            }
        }
        if ($Apply) { Remove-Item $Manifest -Force }
    }

    $ini = Join-Path $GameDir 'naraka_proxy.ini'
    if (Test-Path $ini) {
        if ($Apply) { Remove-Item $ini -Force; Write-Ok "removed naraka_proxy.ini" }
        else        { Write-Step "would remove naraka_proxy.ini" }
    }

    Write-Host ""
    Write-Host "=== Undeploy complete ===" -ForegroundColor Cyan
    exit 0
}

# ------------------------------------------------------------------- deploy --
Write-Host ""
Write-Host "[3/4] Installing proxies"

$backupDir = Join-Path $WorkDir 'backup'
$records = @()

if ($Apply) {
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

    # Stash anything we are about to displace before touching it - but only
    # genuinely foreign files. An earlier version backed up every file present,
    # including the proxies it had installed itself, so an undeploy "restored"
    # our own DLLs back into the game directory. The unity of this check matters
    # more than it looks: restoring our files is the opposite of uninstalling.
    foreach ($k in $existing.Keys) {
        $src = Join-Path $GameDir $k
        if ($ourHashes.ContainsKey($k) -and $ourHashes[$k] -eq $existing[$k]) {
            Write-Step "$k is our build - nothing to back up"
            continue
        }
        $stash = Join-Path $backupDir $k
        Copy-Item $src $stash -Force
        $records += [pscustomobject]@{ name = $k; stash = $stash; sha256 = $existing[$k] }
        Write-Ok "backed up $k -> $stash"
    }

    foreach ($n in $ProxyNames) {
        Copy-Item (Join-Path $ProxyDir $n) (Join-Path $GameDir $n) -Force
        Write-Ok "installed $n"
    }

    $iniText = @"
[naraka]
; renderdoc | pix | both | off
mode=renderdoc
target_process=narakabladepoint.exe
rdoc_dll=$Core
capture_dir=$WorkDir\captures
log_dir=$WorkDir
"@
    Set-Content -Path (Join-Path $GameDir 'naraka_proxy.ini') -Value $iniText -Encoding ASCII
    Write-Ok "wrote naraka_proxy.ini"

    $manifestObj = [pscustomobject]@{
        created  = (Get-Date).ToString('s')
        gameDir  = $GameDir
        proxies  = $ProxyNames
        backups  = $records
    }
    $manifestObj | ConvertTo-Json -Depth 5 | Set-Content -Path $Manifest -Encoding UTF8
    Write-Ok "manifest: $Manifest"
} else {
    foreach ($n in $ProxyNames) { Write-Step "would install $n" }
    Write-Step "would write naraka_proxy.ini"
    Write-Step "would write manifest $Manifest"
}

# ---------------------------------------------------------------- how to use --
Write-Host ""
Write-Host "[4/4] How to capture"
Write-Host @"
  1. Start the game normally through its own launcher.
     The proxies load by themselves - nothing is injected, no process is patched.
  2. Wait until you are in game and rendering (not the loading screen).
  3. Press F12, or create the trigger file:
       New-Item "$WorkDir\captures\trigger_capture_narakabladepoint"
  4. The .rdc lands in:
       $WorkDir\captures
  5. Read the proxy's account of what happened:
       $WorkDir\naraka_proxy.log

  Undo everything with:
       .\Deploy-NarakaProxy.ps1 -Undeploy -Apply
"@
Write-Host ""
if (-not $Apply) {
    Write-Host "DRY RUN - nothing was changed. Re-run with -Apply to install." -ForegroundColor Yellow
}
