# First-party, explicit project UI repair only. $BreadRepairRequest is supplied
# as base64 JSON by project_permissions.py, never evaluated as PowerShell code.
$ErrorActionPreference = 'Stop'
try {
    $root = [IO.Path]::GetFullPath([string]$BreadRepairRequest.projectRoot).TrimEnd('\')
    $sidText = [string]$BreadRepairRequest.userSid
    if ($sidText -notmatch '^S-1-\d+(?:-\d+)+$' -or
        $root -eq [IO.Path]::GetPathRoot($root).TrimEnd('\') -or
        $root -eq [Environment]::GetFolderPath('Windows') -or
        $root -eq [Environment]::GetFolderPath('UserProfile')) { exit 10 }
    function Assert-OrdinaryPath([string]$path) {
        $full = [IO.Path]::GetFullPath($path)
        if ($full -ne $root -and -not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Project repair escaped the selected root' }
        $current = $full
        while ($current -and $current -ne [IO.Path]::GetPathRoot($current)) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Project repair cannot traverse links' }
            $current = [IO.Path]::GetDirectoryName($current)
        }
    }
    Assert-OrdinaryPath $root
    $manifestPath = Join-Path $root '.replaylab\workspace.json'
    Assert-OrdinaryPath $manifestPath
    if ((Get-Item -LiteralPath $manifestPath).Length -gt 4194304) { exit 11 }
    $manifest = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($manifest.schema -ne 'replaylab-project-workspace/v1' -or
        $manifest.workspaceId -ne [string]$BreadRepairRequest.workspaceId) { exit 12 }
    $relativeRoots = @('.replaylab', 'workspaces\shaders', 'shared\dictionaries', 'captures\catalogs')
    $targets = New-Object 'System.Collections.Generic.List[string]'
    $targets.Add($root)
    # Grant parent traversal/create access, but recurse only into the declared
    # Bread-managed trees. Original RDCs, unpacked assets and Unity outputs are
    # not enumerated or modified.
    foreach ($relative in $relativeRoots) {
        $path = Join-Path $root $relative
        if (-not (Test-Path -LiteralPath $path)) { continue }
        Assert-OrdinaryPath $path
        $parent = [IO.Path]::GetDirectoryName($path)
        if ($parent -ne $root -and -not $targets.Contains($parent)) { $targets.Add($parent) }
        if (-not $targets.Contains($path)) { $targets.Add($path) }
        $pending = New-Object 'System.Collections.Generic.Queue[string]'
        $pending.Enqueue($path)
        while ($pending.Count -gt 0) {
            $directory = $pending.Dequeue()
            foreach ($entry in Get-ChildItem -LiteralPath $directory -Force) {
                if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { exit 13 }
                if (-not $entry.FullName.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { exit 13 }
                $targets.Add($entry.FullName)
                if ($entry.PSIsContainer) { $pending.Enqueue($entry.FullName) }
                if ($targets.Count -gt 100000) { exit 14 }
            }
        }
    }
    if ($BreadRepairRequest.inspectOnly -eq $true) { exit 0 }
    $sid = New-Object System.Security.Principal.SecurityIdentifier($sidText)
    foreach ($path in $targets) {
        Assert-OrdinaryPath $path
        $entry = Get-Item -LiteralPath $path -Force
        $acl = if ($entry.PSIsContainer) { [IO.Directory]::GetAccessControl($path) } else { [IO.File]::GetAccessControl($path) }
        $denials = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
            Where-Object { $_.IdentityReference.Value -eq $sidText -and $_.AccessControlType -eq 'Deny' })
        if ($denials.Count -gt 0) {
            if ($denials | Where-Object IsInherited) { $acl.SetAccessRuleProtection($true, $true) }
            foreach ($rule in @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) {
                if ($rule.IdentityReference.Value -eq $sidText -and $rule.AccessControlType -eq 'Deny') { $acl.RemoveAccessRuleSpecific($rule) }
            }
        }
        $inheritance = [Security.AccessControl.InheritanceFlags]::None
        if ($entry.PSIsContainer -and $path -ne $root -and
            @('workspaces','shared','captures') -notcontains $entry.Name) {
            $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
        }
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $sid, [Security.AccessControl.FileSystemRights]::Modify, $inheritance,
            [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
        if ($entry.PSIsContainer) { [IO.Directory]::SetAccessControl($path, $acl) } else { [IO.File]::SetAccessControl($path, $acl) }
        if (-not $entry.PSIsContainer -and $entry.Extension -in @('.json','.hlsl','.glsl','.md','.html','.txt','.yaml','.yml')) {
            if ($entry.Attributes -band [IO.FileAttributes]::ReadOnly) {
                $entry.Attributes = $entry.Attributes -band (-bnot [IO.FileAttributes]::ReadOnly)
            }
        }
    }
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    if ($_.Exception -is [UnauthorizedAccessException] -or
        $_.Exception -is [Security.SecurityException] -or
        $_.CategoryInfo.Category -eq [Management.Automation.ErrorCategory]::PermissionDenied) { exit 21 }
    exit 20
}
