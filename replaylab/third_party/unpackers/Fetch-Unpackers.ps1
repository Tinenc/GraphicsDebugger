# Bread AI -- fetch the four bundled unpackers into the program.
#
#   uex (CUE4Parse, Unreal 4/5 incl. GAME_SilverPalace)   built from source with the .NET 10 SDK
#   AssetStudioModCLI (Unity 1.7 .. 6000.x, bundles/.ab)   GitHub release, net472 build
#   UnityPy (Unity, Python reader)                        pip, into replaylab\.venv (and the frozen AgentHost)
#   umodel / UE Viewer (Unreal 1-4, legacy)               gildor.org umodel_win32.zip
#
# Everything lands in the repository bundle  replaylab\third_party\unpackers\<kind>\  (the
# source of truth for packaging) and is mirrored into the deployed application
# <ApplicationDirectory>\replaylab\tools\unpackers\ so a copied app carries the tools.
# Bread AI registers whatever is in the bundle by itself (%BREAD_TOOLS% paths) -- no
# per-machine setup; the uex profile (game + paks folder) is the one thing to check.
# Every download prints its URL and SHA-256. Nothing is executed from a download
# except the tools themselves, once, to print their version.
#
# Reading an *unpacked* tree needs none of that: no paks, no AES keys, no profile to
# write. Bread AI generates the uex profile from the project's asset root (which
# directory to mount, which engine version), so these tools are only fetched here --
# nothing about a game is configured. -PaksDir is optional and only for the other
# job: mounting a game's original .pak/.utoc containers.
#
# Usage:  Fetch-Unpackers.cmd [-AssetStudioTag v0.19.0] [-GitHubProxyPrefix https://ghfast.top/]
#         Fetch-Unpackers.cmd -UexSource D:\Study\GitHub\uex        (build from your own checkout)
#         Fetch-Unpackers.cmd -UexExe D:\Study\GitHub\uex\publish\uex.exe   (use a build you already have)
#         Fetch-Unpackers.cmd -ProfileOnly -PaksDir "D:\Games\...\Content\Paks"  (pak mode, optional)
#                             [-Ask]

[CmdletBinding()]
param(
    [string]$PaksDir = "",
    [string]$ProfileName = "silverpalace",
    [string]$Game = "GAME_SilverPalace",
    # Pin a release instead of resolving "latest" (e.g. -AssetStudioTag v0.19.0).
    [string]$AssetStudioTag = "",
    # Prefix put in front of every github.com download URL when github.com is slow
    # or blocked from your network, e.g. -GitHubProxyPrefix "https://ghfast.top/"
    [string]$GitHubProxyPrefix = "",
    # Prompt before each step (the default is to run everything without asking).
    [switch]$Ask,
    # Where the built application lives; the bundle is mirrored into <it>\replaylab\tools.
    [string]$ApplicationDirectory = "",
    [switch]$SkipUex,
    [switch]$SkipAssetStudio,
    [switch]$SkipUnityPy,
    [switch]$SkipUmodel,
    # Only (re)write the optional pak-mode uex profile for an already built bundle; no downloads.
    [switch]$ProfileOnly,
    # Build uex from a checkout you already have instead of cloning (your own repo).
    [string]$UexSource = "",
    # Or skip building entirely and bundle a uex.exe you already built.
    [string]$UexExe = "",
    # Also fetch the Oodle/zlib native decompressors so the shared bundle needs no
    # network on someone else's machine.
    [switch]$SkipNatives
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$VenvPython = Join-Path $RepoRoot "replaylab\.venv\Scripts\python.exe"
$AgentCli = Join-Path $RepoRoot "replaylab\.venv\Scripts\replaylab-ai.exe"
$Drop = Join-Path $RepoRoot "replaylab\third_party\unpackers"
if (-not $ApplicationDirectory) { $ApplicationDirectory = Join-Path $RepoRoot "x64\Development" }
New-Item -ItemType Directory -Force -Path $Drop | Out-Null
$Manifest = @{ schema = "bread-bundled-unpackers/v1"; fetchedAt = (Get-Date).ToUniversalTime().ToString("o"); tools = @{} }

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Note($text) { Write-Host "    $text" }
function Warn($text) { Write-Host "    ! $text" -ForegroundColor Yellow }

function Confirm-Or-Skip($question) {
    if (-not $Ask) { Note $question; return $true }
    $answer = Read-Host "$question [Y/n]"
    return ($answer -eq "" -or $answer -match '^[Yy]')
}

function Download($url, $target, $referer = "") {
    $url = Proxied $url
    Note "GET $url"
    $headers = @{ "User-Agent" = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BreadAI-unpacker-installer" }
    if ($referer) { $headers["Referer"] = $referer }
    Invoke-WebRequest -Uri $url -OutFile $target -UseBasicParsing -Headers $headers
    $hash = (Get-FileHash -Algorithm SHA256 $target).Hash.ToLower()
    $size = (Get-Item $target).Length
    Note ("saved {0} ({1:N0} bytes) sha256={2}" -f (Split-Path $target -Leaf), $size, $hash)
    return $hash
}

function Unzip-Flat($zip, $destination) {
    if (Test-Path $destination) { Remove-Item -Recurse -Force $destination }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Expand-Archive -Path $zip -DestinationPath $destination -Force
    # A release zip that wraps everything in one folder is flattened so Find sees the exe.
    $children = Get-ChildItem $destination
    if ($children.Count -eq 1 -and $children[0].PSIsContainer) {
        Get-ChildItem $children[0].FullName | Move-Item -Destination $destination -Force
        Remove-Item -Recurse -Force $children[0].FullName
    }
}

function Proxied($url) {
    if ($GitHubProxyPrefix -and $url -like "https://github.com/*") { return $GitHubProxyPrefix.TrimEnd("/") + "/" + $url }
    return $url
}

function Latest-Release-Tag($repo) {
    # Four ways to learn the newest tag, none of them the rate-limited API:
    # the final URL after following /releases/latest, the Atom feed, the raw
    # redirect header, and finally the HTML of the releases page.
    $headers = @{ "User-Agent" = "BreadAI-unpacker-installer" }
    $latest = Proxied "https://github.com/$repo/releases/latest"
    try {
        $page = Invoke-WebRequest -Uri $latest -UseBasicParsing -Headers $headers -MaximumRedirection 5
        $final = ""
        if ($page.BaseResponse -and $page.BaseResponse.ResponseUri) { $final = $page.BaseResponse.ResponseUri.AbsoluteUri }
        if ($final -match '/releases/tag/([^/?#]+)') { return [uri]::UnescapeDataString($Matches[1]) }
        if ($page.Content -match '/' + [regex]::Escape($repo) + '/releases/tag/([^"/?#<]+)') { return [uri]::UnescapeDataString($Matches[1]) }
        Note "releases/latest answered $([int]$page.StatusCode) without a tag in the URL or page; trying the feed."
    } catch {
        Note "releases/latest failed: $($_.Exception.Message.Split([char]10)[0]); trying the feed."
    }
    try {
        $feed = Invoke-WebRequest -Uri (Proxied "https://github.com/$repo/releases.atom") -UseBasicParsing -Headers $headers
        if ($feed.Content -match '/releases/tag/([^"/?#<]+)') { return [uri]::UnescapeDataString($Matches[1]) }
    } catch {
        Note "releases.atom failed: $($_.Exception.Message.Split([char]10)[0])"
    }
    try {
        $request = [System.Net.HttpWebRequest]::Create("https://github.com/$repo/releases/latest")
        $request.AllowAutoRedirect = $false
        $request.UserAgent = "BreadAI-unpacker-installer"
        try { $response = $request.GetResponse() } catch [System.Net.WebException] { $response = $_.Exception.Response }
        if ($response) {
            $location = $response.Headers["Location"]
            $status = [int]$response.StatusCode
            $response.Close()
            if ($location -match '/releases/tag/([^/?#]+)') { return [uri]::UnescapeDataString($Matches[1]) }
            Note "redirect probe answered $status, Location='$location'"
        }
    } catch {
        Note "redirect probe failed: $($_.Exception.Message.Split([char]10)[0])"
    }
    throw "could not determine the latest release of $repo from this network; re-run with -AssetStudioTag <tag> (see https://github.com/$repo/releases) and/or -GitHubProxyPrefix <mirror>"
}

function Latest-Release-Asset($repo, $patterns) {
    # Try the GitHub API first (exact names and sizes), fall back to the redirect
    # + known file names when the API is rate-limited or offline.
    $token = $env:GITHUB_TOKEN
    $headers = @{ "User-Agent" = "BreadAI-unpacker-installer" }
    if ($token) { $headers["Authorization"] = "Bearer $token" }
    if ($AssetStudioTag -and $repo -eq "aelurum/AssetStudio") { $token = $null; $headers = $null }
    try {
        if (-not $headers) { throw "release pinned by parameter" }
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers $headers
        foreach ($pattern in $patterns) {
            $asset = $release.assets | Where-Object { $_.name -like $pattern } | Select-Object -First 1
            if ($asset) { return @{ tag = $release.tag_name; name = $asset.name; url = $asset.browser_download_url; size = $asset.size } }
        }
        throw "no asset matching $($patterns -join ', ') in $repo $($release.tag_name)"
    } catch {
        Note "GitHub API unavailable ($($_.Exception.Message.Split([char]10)[0])); resolving the release by redirect instead."
    }
    $tag = if ($AssetStudioTag -and $repo -eq "aelurum/AssetStudio") { $AssetStudioTag } else { Latest-Release-Tag $repo }
    foreach ($pattern in $patterns) {
        if ($pattern -match '\*') { continue }   # the fallback needs literal file names
        $url = "https://github.com/$repo/releases/download/$tag/$pattern"
        try {
            $head = [System.Net.HttpWebRequest]::Create((Proxied $url))
            $head.Method = "HEAD"; $head.UserAgent = "BreadAI-unpacker-installer"; $head.AllowAutoRedirect = $true
            $probe = $head.GetResponse(); $length = $probe.ContentLength; $probe.Close()
            return @{ tag = $tag; name = $pattern; url = $url; size = $length }
        } catch [System.Net.WebException] {
            $status = $null
            if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
            if ($status -eq 404) { continue }
            # A CDN that refuses HEAD still serves GET: keep the file, size unknown.
            return @{ tag = $tag; name = $pattern; url = $url; size = -1 }
        }
    }
    throw "none of the expected files ($($patterns -join ', ')) exist in $repo release $tag"
}

function Banner($exe, $flag) {
    # Native tools write their usage to stderr; with Stop semantics that would
    # be a terminating error, so this helper reads it leniently.
    $ErrorActionPreference = "Continue"
    try { return ((& $exe $flag 2>&1 | Select-Object -First 1) -join " ") } catch { return "(no banner)" }
}

function Record-Tool($kind, $program, $version, $source, $hash) {
    $Manifest.tools[$kind] = @{ program = $program; version = "$version"; source = "$source"; sha256 = "$hash"; fetchedAt = (Get-Date).ToUniversalTime().ToString("o") }
}

function Sync-Bundle() {
    # Bread AI registers whatever the bundle holds; asking it to list the tools
    # runs that synchronisation for the development runtime (replaylab\.venv).
    $ErrorActionPreference = "Continue"
    if (Test-Path $AgentCli) { $cmd = @($AgentCli) }
    elseif (Test-Path $VenvPython) { $cmd = @($VenvPython, "-X", "utf8", "-m", "replaylab_agent.cli") }
    else { Warn "Bread AI runtime not found (replaylab\.venv); the bundle will be registered the first time the AI settings open."; return }
    $exe = $cmd[0]; $argList = @()
    if ($cmd.Length -gt 1) { $argList += $cmd[1..($cmd.Length - 1)] }
    $argList += @("tools", "tool-list", "--json")
    $text = (& $exe @argList 2>&1 | Out-String)
    try {
        $view = $text | ConvertFrom-Json
        foreach ($row in $view.builtinUnpackers) { Note ("{0,-12} {1}" -f $row.kind, $(if ($row.configured) { "configured: " + $row.program } else { "not configured" })) }
    } catch { Note $text.Trim() }
}

function Deploy-Bundle() {
    $target = Join-Path $ApplicationDirectory "replaylab\tools\unpackers"
    if (-not (Test-Path (Join-Path $ApplicationDirectory "replaylab"))) {
        Warn "application directory has no replaylab\ runtime yet ($ApplicationDirectory); the bundle stays in the repository and is mirrored by REBUILD_HOST_AND_BRIDGE.cmd."
        return
    }
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "replaylab\packaging\Deploy-BundledTools.ps1") -ApplicationDirectory $ApplicationDirectory
}

function Find-PaksDir() {
    if ($PaksDir) { return $PaksDir }
    $libraries = @()
    $steam = @("${env:ProgramFiles(x86)}\Steam", "$env:ProgramFiles\Steam", "C:\Steam", "D:\Steam", "D:\SteamLibrary", "E:\SteamLibrary", "F:\SteamLibrary")
    foreach ($root in $steam) {
        $vdf = Join-Path $root "steamapps\libraryfolders.vdf"
        if (Test-Path $vdf) {
            $libraries += $root
            Select-String -Path $vdf -Pattern '"path"\s+"([^"]+)"' -AllMatches | ForEach-Object { $_.Matches } | ForEach-Object { $libraries += ($_.Groups[1].Value -replace '\\\\', '\') }
        }
    }
    $roots = @()
    foreach ($library in ($libraries | Select-Object -Unique)) {
        $common = Join-Path $library "steamapps\common"
        if (Test-Path $common) { $roots += @{ path = $common; depth = 4 } }
    }
    # Any fixed drive, three levels deep, folders named like the game: bounded, a minute at most.
    foreach ($drive in (Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Free -ne $null })) {
        $roots += @{ path = $drive.Root; depth = 3 }
    }
    foreach ($root in $roots) {
        $hits = Get-ChildItem -Path $root.path -Directory -Recurse -Depth $root.depth -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match "Silver|白银" } |
            ForEach-Object { Get-ChildItem -Path $_.FullName -Directory -Recurse -Depth 3 -Filter "Paks" -ErrorAction SilentlyContinue } |
            Where-Object { Get-ChildItem -Path (Join-Path $_.FullName "*") -Include *.pak,*.utoc -ErrorAction SilentlyContinue | Select-Object -First 1 }
        if ($hits) { return ($hits | Select-Object -First 1).FullName }
    }
    return ""
}

if ($ProfileOnly) { $SkipAssetStudio = $true; $SkipUnityPy = $true; $SkipUmodel = $true }
function Fetch-UexNatives($uexRoot) {
    # CUE4Parse downloads these on first use into .uex-cache next to the exe. Fetching
    # them here means the bundle works on a machine with no access to github.com.
    $cache = Join-Path $uexRoot ".uex-cache"
    New-Item -ItemType Directory -Force -Path $cache | Out-Null
    $oodle = Join-Path $cache "oodle-data-shared.dll"
    $zlib = Join-Path $cache "zlib-ng2.dll"
    if ((Test-Path $oodle) -and (Test-Path $zlib)) { Note "native decompressors already in .uex-cache"; return }
    try {
        if (-not (Test-Path $oodle)) {
            $zip = Join-Path $env:TEMP "oodle-clang-cl-x64-release.zip"
            Download "https://github.com/WorkingRobot/OodleUE/releases/download/2026-06-04-1357/clang-cl-x64-release.zip" $zip | Out-Null
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $archive = [IO.Compression.ZipFile]::OpenRead($zip)
            try {
                $entry = $archive.Entries | Where-Object { $_.FullName -like "*bin/oodle-data-shared.dll" } | Select-Object -First 1
                if ($entry) { [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $oodle, $true) }
            } finally { $archive.Dispose() }
        }
        if (-not (Test-Path $zlib)) {
            $gz = Join-Path $env:TEMP "zlib-ng2.dll.gz"
            Download "https://github.com/NotOfficer/Zlib-ng.NET/releases/download/1.0.0/zlib-ng2.dll.gz" $gz | Out-Null
            $input = [IO.File]::OpenRead($gz)
            try {
                $stream = New-Object IO.Compression.GZipStream($input, [IO.Compression.CompressionMode]::Decompress)
                $output = [IO.File]::Create($zlib)
                try { $stream.CopyTo($output) } finally { $output.Dispose(); $stream.Dispose() }
            } finally { $input.Dispose() }
        }
        Note "native decompressors in $cache (Oodle, zlib-ng)"
    } catch {
        Warn ("native decompressors could not be fetched ({0}); uex downloads them itself on first use." -f $_.Exception.Message.Split([char]10)[0])
    }
}

function Write-PakProfile($uexRoot) {
    # Optional: mounting a game's ORIGINAL .pak/.utoc, which does need a version and
    # AES keys. Nothing in the unpacked-tree path uses this file.
    $exe = Join-Path $uexRoot "uex.exe"
    $paks = Find-PaksDir
    if (-not $paks) { Warn "Paks folder not found automatically; re-run with -PaksDir <game>\Content\Paks." }
    $profilesPath = Join-Path $uexRoot "profiles.json"
    $existing = ""
    if (Test-Path $profilesPath) { $existing = Get-Content $profilesPath -Raw }
    if ($existing -match ('"' + [regex]::Escape($ProfileName) + '"\s*:')) {
        $document = $existing | ConvertFrom-Json
        $current = [string]$document.profiles.$ProfileName.paksDir
        $stale = (-not $current) -or ($current -like "<put*") -or (-not (Test-Path $current))
        if ($paks -and ($PaksDir -or $stale)) {
            $document.profiles.$ProfileName | Add-Member -NotePropertyName paksDir -NotePropertyValue $paks -Force
            ($document | ConvertTo-Json -Depth 8) | Set-Content -Path $profilesPath -Encoding UTF8
            Note "profiles.json: profile '$ProfileName' paksDir -> $paks"
        } elseif ($stale) {
            Warn "profiles.json: profile '$ProfileName' still has no valid paksDir ($current)."
        } else {
            Note "profiles.json already has profile '$ProfileName' (paksDir $current); left as it is."
        }
    } else {
        $paksValue = if ($paks) { $paks } else { "<put the game's Content\Paks folder here>" }
        $profileRecord = [ordered]@{
            game = $Game
            paksDir = $paksValue
            outputDir = (Join-Path $uexRoot "export")
            analysisDir = (Join-Path $uexRoot "analysis")
            keys = @()
        }
        $document = [ordered]@{ profiles = [ordered]@{ $ProfileName = $profileRecord } }
        ($document | ConvertTo-Json -Depth 6) | Set-Content -Path $profilesPath -Encoding UTF8
        Note "profiles: $profilesPath  (profile '$ProfileName', game $Game)"
    }
    if ($paks -and (Test-Path $exe)) {
        Note "checking the profile against the paks (uex discover)..."
        $ErrorActionPreference = "Continue"
        & $exe --config $profilesPath discover --profile $ProfileName 2>&1 | Select-Object -First 40 | ForEach-Object { Note $_ }
        $ErrorActionPreference = "Stop"
    }
}

Write-Host "Bread AI bundled unpackers" -ForegroundColor Green
Note "bundle:      $Drop"
Note "application: $ApplicationDirectory"

# ---------------------------------------------------------------- AssetStudioModCLI
if (-not $SkipAssetStudio) {
    Step "AssetStudioModCLI (Unity)"
    $asset = Latest-Release-Asset "aelurum/AssetStudio" @("AssetStudioModCLI_net472_win32_64.zip", "AssetStudioModCLI_net472*.zip", "AssetStudioModCLI_net9_win64.zip")
    Note ("latest release {0}: {1}{2}" -f $asset.tag, $asset.name, $(if ($asset.size -ge 0) { " ({0:N0} bytes)" -f $asset.size } else { "" }))
    if (Confirm-Or-Skip "Download and install it into $Drop\assetstudio?") {
        $zip = Join-Path $env:TEMP $asset.name
        $hash = Download $asset.url $zip
        Unzip-Flat $zip (Join-Path $Drop "assetstudio")
        $exe = Get-ChildItem (Join-Path $Drop "assetstudio") -Filter "AssetStudioModCLI.exe" -Recurse | Select-Object -First 1
        if (-not $exe) { throw "AssetStudioModCLI.exe not found after unzip" }
        Note ("banner: " + (Banner $exe.FullName "--help"))
        Record-Tool "assetstudio" $exe.FullName $asset.tag "github.com/aelurum/AssetStudio" $hash
    }
}

# ---------------------------------------------------------------- UnityPy
if (-not $SkipUnityPy) {
    Step "UnityPy (Unity, Python reader)"
    if (-not (Test-Path $VenvPython)) { throw "replaylab venv python not found: $VenvPython" }
    if (Confirm-Or-Skip "pip install --upgrade UnityPy into replaylab\.venv?") {
        & $VenvPython -m pip install --upgrade --quiet UnityPy
        if ($LASTEXITCODE -ne 0) { throw "pip install UnityPy failed" }
        $version = & $VenvPython -c "import UnityPy; print(UnityPy.__version__)"
        Note "UnityPy $version (the frozen AgentHost embeds it on the next REBUILD_HOST_AND_BRIDGE)"
        Record-Tool "unitypy" "embedded: replaylab-ai unpacker unitypy" $version "pypi.org/project/UnityPy" ""
    }
}

# ---------------------------------------------------------------- umodel
if (-not $SkipUmodel) {
    Step "umodel / UE Viewer (Unreal legacy)"
    $url = "https://www.gildor.org/down/47/umodel/umodel_win32.zip"
    if (Confirm-Or-Skip "Download $url into $Drop\umodel?") {
        try {
            $zip = Join-Path $env:TEMP "umodel_win32.zip"
            $hash = Download $url $zip "https://www.gildor.org/en/projects/umodel"
            Unzip-Flat $zip (Join-Path $Drop "umodel")
            $exe = Get-ChildItem (Join-Path $Drop "umodel") -Filter "umodel_64.exe" -Recurse | Select-Object -First 1
            if (-not $exe) { $exe = Get-ChildItem (Join-Path $Drop "umodel") -Filter "umodel.exe" -Recurse | Select-Object -First 1 }
            if (-not $exe) { throw "umodel.exe not found after unzip" }
            Note ("banner: " + (Banner $exe.FullName "-help"))
            Record-Tool "umodel" $exe.FullName "gildor.org (2023-07-07 build)" "www.gildor.org/en/projects/umodel" $hash
        } catch {
            Warn ("umodel could not be fetched automatically ({0}). It is the least needed of the four (UE1-UE4 legacy titles). " -f $_.Exception.Message.Split([char]10)[0])
            Warn "Download umodel_win32.zip by hand from https://www.gildor.org/en/projects/umodel#files and unzip it into $Drop\umodel -- Bread AI picks it up on its own."
        }
    }
}

# ---------------------------------------------------------------- uex (built from source)
if (-not $SkipUex) {
    Step "uex (CUE4Parse; Unreal 4/5, reads the unpacked tree directly)"
    $uexRoot = Join-Path $Drop "uex"
    # The source tree is a SIBLING of the output, never its child and never its parent.
    # Publishing into a directory that CONTAINS the project's own source makes MSBuild
    # treat every .cs under it as output and exclude it from the compilation -- that is
    # the "error CS5001: no static Main" this script used to produce.
    $srcRoot = Join-Path $Drop "_src\uex"
    New-Item -ItemType Directory -Force -Path $uexRoot | Out-Null

    $prebuilt = ""
    if ($UexExe) {
        if (-not (Test-Path $UexExe)) { throw "-UexExe not found: $UexExe" }
        $prebuilt = (Resolve-Path $UexExe).Path
    } elseif (-not $ProfileOnly -and -not (Test-Path (Join-Path $uexRoot "uex.exe"))) {
        # A uex the user already built (their own repo's documented `-o publish`).
        $guesses = @()
        if ($UexSource) { $guesses += (Join-Path $UexSource "publish\uex.exe") }
        $guesses += "$env:USERPROFILE\source\uex\publish\uex.exe"
        $guesses += (Join-Path (Split-Path $RepoRoot -Parent) "uex\publish\uex.exe")
        foreach ($guess in $guesses) {
            if ($guess -and (Test-Path $guess)) { $prebuilt = (Resolve-Path $guess).Path; break }
        }
        if ($prebuilt) { Note "found a uex build you already have: $prebuilt" }
    }

    if ($ProfileOnly -and (Test-Path (Join-Path $uexRoot "uex.exe"))) {
        Note "uex.exe already present; profile only."
        $revision = "existing build"
    } elseif ($prebuilt) {
        # Copy the whole publish folder: uex.exe alone cannot run (its runtime
        # assemblies and CUE4Parse live beside it).
        $from = Split-Path $prebuilt -Parent
        Note "copying the published build into the bundle ($from)"
        & robocopy $from $uexRoot /E /NFL /NDL /NJH /NJS /NP /XD ".uex-cache" | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "copying the uex build failed (robocopy $LASTEXITCODE)" }
        $revision = "prebuilt: $prebuilt"
    } else {
        $sdks = @()
        try { $sdks = (& dotnet --list-sdks 2>$null) | Where-Object { $_ -match '^10\.' } } catch { }
        if (-not $sdks) {
            Warn ".NET 10 SDK is required to build uex and is not installed."
            if (Confirm-Or-Skip "Install it now with winget (Microsoft.DotNet.SDK.10)?") {
                & winget install --id Microsoft.DotNet.SDK.10 -e --accept-source-agreements --accept-package-agreements
                $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [Environment]::GetEnvironmentVariable("PATH", "User")
                $sdks = (& dotnet --list-sdks 2>$null) | Where-Object { $_ -match '^10\.' }
            }
        }
        if (-not $sdks) {
            Warn "skipping uex: no .NET 10 SDK (https://dotnet.microsoft.com/download/dotnet/10.0). Re-run this script after installing it."
        } else {
            if ($UexSource) {
                if (-not (Test-Path (Join-Path $UexSource "src\Uex\Uex.csproj"))) {
                    throw "-UexSource is not a uex checkout (no src\Uex\Uex.csproj): $UexSource"
                }
                $build = (Resolve-Path $UexSource).Path
                $revision = "local: $build"
                Note "building from your checkout: $build"
            } else {
                New-Item -ItemType Directory -Force -Path (Split-Path $srcRoot -Parent) | Out-Null
                if (Get-Command git -ErrorAction SilentlyContinue) {
                    if (Test-Path (Join-Path $srcRoot ".git")) {
                        & git -C $srcRoot fetch --depth 1 origin main
                        & git -C $srcRoot reset --hard FETCH_HEAD
                    } else {
                        if (Test-Path $srcRoot) { Remove-Item -Recurse -Force $srcRoot }
                        & git clone --depth 1 (Proxied "https://github.com/Straw1997/uex") $srcRoot
                    }
                    if ($LASTEXITCODE -ne 0) { throw "cloning uex failed" }
                    $revision = (& git -C $srcRoot rev-parse --short HEAD)
                } else {
                    $zip = Join-Path $env:TEMP "uex-main.zip"
                    Download "https://github.com/Straw1997/uex/archive/refs/heads/main.zip" $zip | Out-Null
                    Unzip-Flat $zip $srcRoot
                    $revision = "main.zip"
                }
                $build = $srcRoot
                Note "source revision: $revision"
            }
            if (-not $UexSource) {
                # Keep Bread's explicit inline-material evidence command when a
                # managed source checkout is refreshed. Never patch a caller's
                # separate -UexSource checkout or silently publish without it.
                $materialEvidencePatch = Join-Path $PSScriptRoot "uex-material-shader-maps.patch"
                if (-not (Test-Path -LiteralPath $materialEvidencePatch)) {
                    throw "Missing bundled UEX material evidence source patch: $materialEvidencePatch"
                }
                if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
                    throw "Building bundled UEX material evidence requires Git to apply its source patch. Install Git or supply a verified -UexExe build."
                }
                & git -C $build apply --reverse --check $materialEvidencePatch 2>$null
                if ($LASTEXITCODE -ne 0) {
                    & git -C $build apply --check $materialEvidencePatch
                    if ($LASTEXITCODE -ne 0) {
                        throw "The UEX material evidence patch does not match this source revision; review the patch before publishing."
                    }
                    & git -C $build apply $materialEvidencePatch
                    if ($LASTEXITCODE -ne 0) { throw "Applying the UEX material evidence patch failed." }
                }
                $revision += "+bread-material-shader-maps-v1"
            }
            # Publish inside the source tree the way the project documents it, then
            # copy the result into the bundle: the output never contains the source.
            $stage = Join-Path $build "publish"
            Push-Location $build
            try {
                & dotnet publish src/Uex/Uex.csproj -c Release -o $stage -p:CUE4PARSE_SKIP_NATIVE=true
                if ($LASTEXITCODE -ne 0) {
                    throw "dotnet publish failed. Build it by hand in $build (dotnet publish src/Uex/Uex.csproj -c Release -o publish -p:CUE4PARSE_SKIP_NATIVE=true) and re-run with -UexExe $stage\uex.exe"
                }
            } finally { Pop-Location }
            if (-not (Test-Path (Join-Path $stage "uex.exe"))) { throw "uex.exe not found after publish in $stage" }
            & robocopy $stage $uexRoot /E /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "copying the uex build failed (robocopy $LASTEXITCODE)" }
        }
    }

    $exe = Join-Path $uexRoot "uex.exe"
    if (Test-Path $exe) {
        Note ("version: " + (Banner $exe "--version"))
        Record-Tool "uex" $exe $revision "github.com/Straw1997/uex (CUE4Parse)" ""
        if (-not $SkipNatives) { Fetch-UexNatives $uexRoot }
        # The optional half: a profiles.json for mounting a game's original paks.
        # Unpacked trees need none of this -- Bread AI writes its own profile.
        if ($PaksDir -or $ProfileOnly) { Write-PakProfile $uexRoot }
        else {
            Note "unpacked mode: Bread AI generates the uex profile from the project's asset root."
            Note "  (pak mode is optional: re-run with -PaksDir <game>\Content\Paks to add one)"
        }
    }
}

Step "Bundle manifest, deployment and registration"
($Manifest | ConvertTo-Json -Depth 6) | Set-Content -Path (Join-Path $Drop "manifest.json") -Encoding UTF8
Note "manifest: $(Join-Path $Drop 'manifest.json')"
Deploy-Bundle
Sync-Bundle
Step "Done"
Note "The bundle travels with the program: copy the application folder and the tools come along."
