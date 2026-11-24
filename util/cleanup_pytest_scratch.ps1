[CmdletBinding()]
# Default is audit-only. Add -Apply to delete validated pytest scratch,
# -IncludeCache for reproducible .pytest_cache directories, and
# -IncludeSystemTestTemp for this repository's explicitly named OS-temp runs.
param(
  [switch]$Apply,
  [switch]$IncludeCache,
  [switch]$IncludeSystemTestTemp
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path.TrimEnd("\")
$repositoryPrefix = $repositoryRoot + "\"
$scratchNamePattern = if($IncludeCache) {
  "^(?:\.pytest-|\.tmp-pytest-|\.test-tmp-|_pytest_|\.tmp-test-|\.pytest_cache$)"
} else {
  "^(?:\.pytest-|\.tmp-pytest-|\.test-tmp-|_pytest_|\.tmp-test-)"
}

$directories = @(
  Get-ChildItem -LiteralPath $repositoryRoot -Directory -Force -Recurse |
    Where-Object { $_.Name -match $scratchNamePattern } |
    Sort-Object FullName -Unique
)

$validated = [System.Collections.Generic.List[object]]::new()
$seen = [System.Collections.Generic.HashSet[string]]::new(
  [System.StringComparer]::OrdinalIgnoreCase
)
$totalBytes = [int64]0
$totalFiles = [int64]0
$systemTempValidated = [System.Collections.Generic.List[object]]::new()
$systemTempBytes = [int64]0
$systemTempFiles = [int64]0

foreach($directory in $directories)
{
  $fullPath = (Resolve-Path -LiteralPath $directory.FullName).Path.TrimEnd("\")
  if($fullPath -eq $repositoryRoot -or
     -not $fullPath.StartsWith($repositoryPrefix, [System.StringComparison]::OrdinalIgnoreCase))
  {
    throw "Refusing cleanup target outside the repository: $fullPath"
  }
  if((Split-Path -Leaf $fullPath) -notmatch $scratchNamePattern)
  {
    throw "Refusing cleanup target with an unexpected name: $fullPath"
  }
  if(-not $seen.Add($fullPath))
  {
    throw "Duplicate cleanup target: $fullPath"
  }
  if($directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
  {
    throw "Refusing reparse-point cleanup target: $fullPath"
  }

  $items = @(Get-ChildItem -LiteralPath $fullPath -Force -Recurse)
  $reparsePoints = @(
    $items | Where-Object {
      $_.Attributes -band [System.IO.FileAttributes]::ReparsePoint
    }
  )
  if($reparsePoints.Count -ne 0)
  {
    throw "Refusing cleanup target containing a reparse point: $fullPath"
  }

  $files = @($items | Where-Object { -not $_.PSIsContainer })
  $bytes = [int64]0
  if($files.Count -ne 0)
  {
    $bytes = [int64](($files | Measure-Object Length -Sum).Sum)
  }
  $totalBytes += $bytes
  $totalFiles += $files.Count
  $validated.Add([pscustomobject]@{
      FullPath = $fullPath
      RelativePath = $fullPath.Substring($repositoryPrefix.Length)
      Bytes = $bytes
      Files = $files.Count
      Modified = $directory.LastWriteTime
    })
}

$trackedPaths = @(
  & git -C $repositoryRoot ls-files |
    ForEach-Object {
      [System.IO.Path]::GetFullPath(
        (Join-Path $repositoryRoot ($_ -replace "/", "\"))
      )
    }
)
foreach($trackedPath in $trackedPaths)
{
  foreach($target in $validated)
  {
    if($trackedPath -eq $target.FullPath -or
       $trackedPath.StartsWith($target.FullPath + "\", [System.StringComparison]::OrdinalIgnoreCase))
    {
      throw "Refusing cleanup target containing tracked file: $($target.RelativePath)"
    }
  }
}

foreach($outer in $validated)
{
  foreach($inner in $validated)
  {
    if($outer.FullPath -ne $inner.FullPath -and
       $inner.FullPath.StartsWith($outer.FullPath + "\", [System.StringComparison]::OrdinalIgnoreCase))
    {
      throw "Refusing nested cleanup targets: $($outer.RelativePath) and $($inner.RelativePath)"
    }
  }
}

if($IncludeSystemTestTemp)
{
  $systemTempRoot = (Resolve-Path -LiteralPath ([IO.Path]::GetFullPath($env:TEMP))).Path.TrimEnd("\")
  $systemTempNamePattern = "^(?:renderdoc-ultra-pytest|bread-(?:focused|host-focused|agent-focused|quality-focused|final-host-regression|agent-regression|postfix-(?:mcp|agent)|skill-recheck|quality-recheck)-[A-Za-z0-9-]+)$"
  $systemCandidates = @(
    Get-ChildItem -LiteralPath $systemTempRoot -Directory -Force |
      Where-Object { $_.Name -match $systemTempNamePattern } |
      Sort-Object FullName -Unique
  )
  foreach($directory in $systemCandidates)
  {
    $fullPath = (Resolve-Path -LiteralPath $directory.FullName).Path.TrimEnd("\")
    if(-not (Split-Path -Parent $fullPath).Equals(
        $systemTempRoot, [System.StringComparison]::OrdinalIgnoreCase))
    {
      throw "Refusing system test-temp target outside the direct temp root: $fullPath"
    }
    if((Split-Path -Leaf $fullPath) -notmatch $systemTempNamePattern)
    {
      throw "Refusing system test-temp target with an unexpected name: $fullPath"
    }
    if($directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
    {
      throw "Refusing system test-temp reparse target: $fullPath"
    }
    $items = @(Get-ChildItem -LiteralPath $fullPath -Force -Recurse)
    if(@($items | Where-Object {
        $_.Attributes -band [System.IO.FileAttributes]::ReparsePoint
      }).Count -ne 0)
    {
      throw "Refusing system test-temp target containing a reparse point: $fullPath"
    }
    $files = @($items | Where-Object { -not $_.PSIsContainer })
    $bytes = [int64]0
    if($files.Count -ne 0)
    {
      $bytes = [int64](($files | Measure-Object Length -Sum).Sum)
    }
    $systemTempBytes += $bytes
    $systemTempFiles += $files.Count
    $systemTempValidated.Add([pscustomobject]@{
        FullPath = $fullPath
        Bytes = $bytes
        Files = $files.Count
        Modified = $directory.LastWriteTime
      })
  }
}

$summary = [ordered]@{
  repository = $repositoryRoot
  matchedDirectories = $validated.Count
  files = $totalFiles
  bytes = $totalBytes
  applied = [bool]$Apply
  includedStandardCache = [bool]$IncludeCache
  includedSystemTestTemp = [bool]$IncludeSystemTestTemp
  systemTestTempDirectories = $systemTempValidated.Count
  systemTestTempFiles = $systemTempFiles
  systemTestTempBytes = $systemTempBytes
  deletedSystemTestTempDirectories = 0
  deletedDirectories = 0
  remainingDirectories = $validated.Count
  remainingSystemTestTempDirectories = $systemTempValidated.Count
}

if($Apply)
{
  foreach($target in ($validated | Sort-Object { $_.FullPath.Length } -Descending))
  {
    Remove-Item -LiteralPath $target.FullPath -Recurse -Force
  }
  foreach($target in $systemTempValidated)
  {
    Remove-Item -LiteralPath $target.FullPath -Recurse -Force
  }

  $remaining = @(
    Get-ChildItem -LiteralPath $repositoryRoot -Directory -Force -Recurse -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -match $scratchNamePattern }
  )
  $summary.deletedDirectories = $validated.Count
  $summary.remainingDirectories = $remaining.Count
  $summary.deletedSystemTestTempDirectories = $systemTempValidated.Count
  if($IncludeSystemTestTemp)
  {
    $summary.remainingSystemTestTempDirectories = @(
      Get-ChildItem -LiteralPath $systemTempRoot -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match $systemTempNamePattern }
    ).Count
  }
  else
  {
    $summary.remainingSystemTestTempDirectories = 0
  }
  if($remaining.Count -ne 0)
  {
    throw "Cleanup finished with $($remaining.Count) matching directories still present."
  }
  if($summary.remainingSystemTestTempDirectories -ne 0)
  {
    throw "Cleanup finished with $($summary.remainingSystemTestTempDirectories) system test-temp directories still present."
  }
}

[pscustomobject]$summary
