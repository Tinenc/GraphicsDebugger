Set-StrictMode -Version Latest

function Resolve-QtLinguistTool
{
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$QtBin
    )

    $executable = if ($env:OS -eq "Windows_NT") { "$Name.exe" } else { $Name }
    $candidates = [System.Collections.Generic.List[string]]::new()

    if ($QtBin)
    {
        $candidates.Add((Join-Path $QtBin $executable))
    }

    foreach ($environmentRoot in @($env:QTDIR, $env:QT_ROOT_DIR))
    {
        if ($environmentRoot)
        {
            $candidates.Add((Join-Path (Join-Path $environmentRoot "bin") $executable))
        }
    }

    $onPath = Get-Command $executable -ErrorAction SilentlyContinue
    if ($onPath)
    {
        $candidates.Add($onPath.Source)
    }

    foreach ($qmakeName in @("qmake", "qmake-qt5"))
    {
        $qmake = Get-Command $qmakeName -ErrorAction SilentlyContinue
        if ($qmake)
        {
            $queriedBin = (& $qmake.Source -query QT_INSTALL_BINS 2>$null | Select-Object -First 1)
            if ($queriedBin)
            {
                $candidates.Add((Join-Path $queriedBin $executable))
            }
        }
    }

    if ($env:OS -eq "Windows_NT")
    {
        $knownQtRoot = "C:\Qt"
        if (Test-Path -LiteralPath $knownQtRoot)
        {
            $knownTools = Get-ChildItem -Path $knownQtRoot -Filter $executable -File -Recurse -ErrorAction SilentlyContinue |
                Where-Object { $_.DirectoryName -match '[\\/]msvc[^\\/]*[\\/]bin$' } |
                Sort-Object FullName -Descending
            foreach ($knownTool in $knownTools)
            {
                $candidates.Add($knownTool.FullName)
            }
        }
    }

    foreach ($candidate in $candidates | Select-Object -Unique)
    {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf))
        {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw "Could not locate Qt 5 $Name. Pass -QtBin, set QTDIR, or add the Qt bin directory to PATH."
}

function Assert-Qt5LinguistTool
{
    param([Parameter(Mandatory = $true)][string]$Path)

    $versionOutput = (& $Path -version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $versionOutput -notmatch '(\d+)\.(\d+)(?:\.(\d+))?')
    {
        throw "Could not determine the version of $Path. Output: $versionOutput"
    }

    $patchVersion = 0
    if ($Matches[3])
    {
        $patchVersion = [int]$Matches[3]
    }
    $version = [version]::new([int]$Matches[1], [int]$Matches[2], $patchVersion)
    if ($version.Major -ne 5 -or $version -lt [version]"5.6")
    {
        throw "QRenderDoc translations require Qt 5.6 or newer in the Qt 5 series; found $version at $Path."
    }

    Write-Host "Using $Path ($versionOutput)"
}

function Resolve-PythonForI18n
{
    if ($env:BREAD_BUILD_PYTHON)
    {
        if (-not (Test-Path -LiteralPath $env:BREAD_BUILD_PYTHON -PathType Leaf))
        {
            throw "BREAD_BUILD_PYTHON does not identify an installed interpreter."
        }
        return $env:BREAD_BUILD_PYTHON
    }
    foreach ($name in @("python", "python3"))
    {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command)
        {
            return $command.Source
        }
    }
    throw "Python 3 is required to run util/i18n/lint_ts.py."
}
