#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)][int]$ParentProcessId,
    [Parameter(Mandatory = $true)][version]$InstalledVersion,
    [Parameter(Mandatory = $true)][version]$TargetVersion,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{64}$')][string]$Sha256,
    [switch]$NoProgressWindow
)

$ErrorActionPreference = 'Stop'
$stage = [IO.Path]::GetFullPath($PSScriptRoot)
$app = [IO.Path]::GetDirectoryName($stage)
$restartDirectory = (Get-Location).ProviderPath
$exe = Join-Path $app 'RenderDocBread.exe'
$archivePath = Join-Path $stage 'update.zip'
$payload = Join-Path $stage 'payload'
$backup = Join-Path $stage 'backup'
$log = Join-Path $stage 'update-error.txt'
$manifestName = 'RenderDocBread.manifest.json'
$zipPrefix = 'RenderDocBread/'
$progress = $null
$progressPercent = 0
$progressLabels = @{
    title = 'Installing RenderDocBread update'
    waiting = 'Waiting for RenderDocBread to close...'
    verify = 'Verifying the downloaded package...'
    extract = 'Extracting and verifying update files...'
    close = 'Closing background processes...'
    check = 'Checking files in the current installation...'
    replace = 'Installing the new version...'
    restart = 'Opening the updated application...'
    clean = 'Cleaning up temporary update files...'
    rollback = 'Restoring the previous installation...'
    restore = 'Opening the previous version...'
    failed = 'The update could not complete. See the installer log at %1.'
}

function New-UpdateProgress {
    # PowerShell performs the installation while the window owns a separate message loop.
    Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Threading;
using System.Windows.Forms;

public sealed class BreadUpdateProgress : IDisposable
{
    private readonly Thread thread;
    private readonly ManualResetEvent shown = new ManualResetEvent(false);
    private readonly object sync = new object();
    private Form window;
    private string status;
    private int progress;
    private volatile bool closing;

    public BreadUpdateProgress(string title, string initial)
    {
        status = initial;
        thread = new Thread(() => Run(title));
        thread.IsBackground = true;
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        if (!shown.WaitOne(5000) || window == null)
        {
            Dispose();
            throw new InvalidOperationException("The installer progress window could not open.");
        }
    }

    private void Run(string title)
    {
        try
        {
            Application.EnableVisualStyles();
            using (var form = new Form())
            using (var timer = new System.Windows.Forms.Timer())
            {
                form.Text = title;
                form.StartPosition = FormStartPosition.CenterScreen;
                form.FormBorderStyle = FormBorderStyle.FixedDialog;
                form.MaximizeBox = false;
                form.MinimizeBox = false;
                form.ControlBox = false;
                form.AutoScaleMode = AutoScaleMode.Dpi;
                form.AutoScaleDimensions = new SizeF(96, 96);
                form.ClientSize = new Size(460, 106);

                var label = new Label { Dock = DockStyle.Top, Height = 27, Text = status };
                var bar = new ProgressBar { Dock = DockStyle.Top, Height = 17, Maximum = 100 };
                var value = new Label { Dock = DockStyle.Top, Height = 24, Text = "0%",
                                        TextAlign = ContentAlignment.MiddleRight };
                form.Padding = new Padding(16);
                form.Controls.AddRange(new Control[] { value, bar, label });
                timer.Interval = 80;
                timer.Tick += delegate
                {
                    lock (sync)
                    {
                        label.Text = status;
                        bar.AccessibleName = status;
                        bar.Value = progress;
                        value.Text = progress + "%";
                    }
                };
                form.FormClosing += (sender, args) =>
                    args.Cancel = !closing && args.CloseReason == CloseReason.UserClosing;
                form.Shown += delegate { window = form; shown.Set(); };
                if (closing) return;
                timer.Start();
                Application.Run(form);
            }
        }
        catch (Exception)
        {
            // Presentation must never interrupt the installation or rollback.
        }
        finally { shown.Set(); }
    }

    public void Report(string text, int percent)
    {
        lock (sync) { status = text; progress = percent; }
    }

    public void Dispose()
    {
        closing = true;
        try
        {
            if (window != null && window.IsHandleCreated)
                window.BeginInvoke((Action)window.Close);
        }
        catch (InvalidOperationException) { }
        if (thread.Join(2000)) shown.Dispose();
    }
}
'@
    return [BreadUpdateProgress]::new($progressLabels.title, $progressLabels.waiting)
}

function Show-UpdateProgress([string]$phase, [int]$percent) {
    $script:progressPercent = $percent
    if ($progress) { $progress.Report($progressLabels[$phase], $percent) }
}

function Show-UpdateFailure {
    if ($progress) {
        [void][Windows.Forms.MessageBox]::Show($progressLabels.failed.Replace('%1', $log), $progressLabels.title,
            [Windows.Forms.MessageBoxButtons]::OK, [Windows.Forms.MessageBoxIcon]::Error)
    }
}

function Wait-ApplicationWindow([Diagnostics.Process]$process) {
    # Installer-only tests use a windowless launcher. Interactive updates wait for the real window.
    if ($NoProgressWindow) { return }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    do {
        if ($process.WaitForExit(150)) { throw "RenderDocBread exited during restart ($($process.ExitCode))." }
        $process.Refresh()
        if ($process.MainWindowHandle -ne [IntPtr]::Zero) { return }
    } while ($timer.Elapsed.TotalSeconds -lt 90)
    throw 'RenderDocBread did not open a window before the restart timeout.'
}

function Assert-RegularPath([string]$path) {
    try {
        $attributes = [IO.File]::GetAttributes($path)
    } catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] {
        return
    }
    if ($attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Redirected update path: $path"
    }
}

function Resolve-PackagePath([string]$root, [string]$relative) {
    # Block traversal, alternate streams, Windows device names, and path aliases.
    foreach ($part in $relative.Split('/')) {
        if ($part -match '(^$|[\\:\x00-\x1f*?"<>|]|[. ]$|^ |^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$))') {
            throw "Invalid package path: $relative"
        }
        $root = [IO.Path]::Combine($root, $part)
        Assert-RegularPath $root
    }
    return $root
}

function Read-Manifest([string]$path, [version]$version) {
    $manifest = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -cne 'renderdocbread-release/v1' -or
        $manifest.product -cne 'RenderDocBread' -or $manifest.platform -cne 'windows-x64' -or
        $manifest.profile -cne 'standard' -or $manifest.breadVersion -cne $version.ToString()) {
        throw "Unexpected package manifest: $path"
    }
    $files = [Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($record in $manifest.files) {
        if (!$record.path -or $record.path -eq $manifestName -or $record.sha256 -notmatch '^[a-f0-9]{64}$' -or
            $record.size -lt 0) {
            throw "Invalid package manifest record: $($record.path)"
        }
        $files.Add([string]$record.path, $record)
    }
    foreach ($required in @('RenderDocBread.exe', 'RenderDocBread.portable', 'renderdoc.dll')) {
        if (!$files.ContainsKey($required)) { throw "Missing package file: $required" }
    }
    return ,$files
}

function Get-StreamHash([IO.Stream]$stream) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '')
    } finally {
        $hasher.Dispose()
    }
}

function Test-PackageFile([string]$path, $record) {
    $file = [IO.FileInfo]::new($path)
    if (!$file.Exists -or $file.Length -ne $record.size) { return $false }
    $stream = $file.OpenRead()
    try {
        return (Get-StreamHash $stream) -eq $record.sha256
    } finally {
        $stream.Dispose()
    }
}

function Get-InstallProcesses {
    $prefix = $app + [IO.Path]::DirectorySeparatorChar
    $session = [Diagnostics.Process]::GetCurrentProcess().SessionId
    foreach ($process in Get-Process) {
        try {
            $image = $process.Path
            if (!$image -and $process.ProcessName -in @('RenderDocBread', 'BreadMCP',
                'ReplayLabInternal', 'replaylab-ai', 'ReplayLabShaderTools')) {
                $image = [BreadUpdateProcess]::ImagePath($process.Id)
            }
            if ($image -and $image.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                @{ Process = $process; Image = $image; SameSession = ($process.SessionId -eq $session) }
            }
        } catch [InvalidOperationException] {
            # The process exited during enumeration.
        }
    }
}

function Close-InstallProcesses {
    # Only close the exact GUI and stop known, package-owned sidecars in this session.
    $workers = @('replaylab\BreadMCP.exe', 'replaylab\ReplayLabInternal.exe',
                 'replaylab\replaylab-ai.exe', 'replaylab\ReplayLabShaderTools.exe')
    $running = @(Get-InstallProcesses)
    foreach ($entry in $running | Where-Object { $_.Image -eq $exe }) {
        $process = $entry.Process
        if ($process.HasExited) { continue }
        if ($entry.SameSession -and $process.CloseMainWindow() -and $process.WaitForExit(30000)) {
            continue
        }
        if (!$process.HasExited) { throw "Close this RenderDocBread window to install the update: $($entry.Image) (PID $($process.Id))" }
    }
    foreach ($entry in Get-InstallProcesses) {
        $process = $entry.Process
        $relative = $entry.Image.Substring($app.Length + 1)
        if ($process.HasExited) { continue }
        if (!$entry.SameSession -or $relative -notin $workers -or !$old.ContainsKey($relative.Replace('\', '/'))) {
            throw "Close the process using this installation before updating: $($entry.Image) (PID $($process.Id))"
        }
        try {
            $process.Kill()
            if (!$process.WaitForExit(10000)) { throw 'The process did not exit.' }
        } catch {
            if (!$process.HasExited) { throw "Could not stop $($entry.Image) (PID $($process.Id)): $_" }
        }
    }
}

function Move-UpdatePath([string]$from, [string]$to) {
    for ($attempt = 0; ; $attempt++) {
        try {
            [IO.File]::Move($from, $to)
            return
        } catch {
            $reason = $_.Exception.GetBaseException()
            if ($attempt -ge 5 -or ($reason.HResult -band 0xffff) -notin 5, 32, 33) {
                throw "Could not move '$from' to '$to': $($reason.Message)"
            }
            Start-Sleep -Milliseconds 300
        }
    }
}

$journal = [Collections.Generic.List[object]]::new()
function Move-UpdateFile([string]$from, [string]$to) {
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($to))
    Move-UpdatePath $from $to
    $journal.Add(@{ From = $from; To = $to })
}

function Remove-UpdateDirectory([string]$path) {
    Assert-RegularPath $path
    for ($attempt = 0; [IO.Directory]::Exists($path); $attempt++) {
        try {
            Remove-Item -LiteralPath $path -Recurse -Force
            return
        } catch {
            if ($attempt -ge 5) { throw }
            Start-Sleep -Milliseconds 300
        }
    }
}

function Expand-Update {
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    Show-UpdateProgress 'verify' 5
    $stream = [IO.File]::Open($archivePath, 'Open', 'Read', 'Read')
    try {
        if ((Get-StreamHash $stream) -ne $Sha256) {
            throw 'The downloaded ZIP does not match the GitHub release SHA-256.'
        }
        $stream.Position = 0
        Show-UpdateProgress 'extract' 15
        $zip = [IO.Compression.ZipArchive]::new($stream, 'Read')
        try {
            [void][IO.Directory]::CreateDirectory($payload)
            $path = Join-Path $payload $manifestName
            $entry = $zip.GetEntry($zipPrefix + $manifestName)
            if (!$entry) { throw 'The release ZIP is missing the package manifest.' }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $path)
            $files = Read-Manifest $path $TargetVersion
            $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
            foreach ($entry in $zip.Entries) {
                if (!$entry.FullName.StartsWith($zipPrefix, [StringComparison]::Ordinal)) {
                    throw "Unexpected ZIP entry: $($entry.FullName)"
                }
                $relative = $entry.FullName.Substring($zipPrefix.Length)
                if (!$seen.Add($relative)) { throw "Duplicate ZIP entry: $relative" }
                if ($relative -ceq $manifestName) { continue }
                if (!$files.ContainsKey($relative) -or $entry.Length -ne $files[$relative].size) {
                    throw "Unlisted or invalid ZIP entry: $relative"
                }
                $destination = Resolve-PackagePath $payload $relative
                [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $destination)
                if (!(Test-PackageFile $destination $files[$relative])) {
                    throw "Extracted file failed verification: $relative"
                }
                if ($progress -and $seen.Count % 64 -eq 0) {
                    Show-UpdateProgress 'extract' (15 + [int](40 * $seen.Count / $zip.Entries.Count))
                }
            }
            if ($seen.Count -ne $files.Count + 1) { throw 'The release ZIP is incomplete.' }
            return ,$files
        } finally {
            $zip.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

$lock = $null
$failed = $false
$rollbackFailed = $false
$validStage = $false
$ownsInstall = $false
try {
    if ([IO.Path]::GetFileName($stage) -notmatch '^\.bread-update-[a-z0-9]+$') {
        throw 'Invalid update staging folder.'
    }
    Assert-RegularPath $stage
    Assert-RegularPath $app
    $validStage = $true
    Assert-RegularPath $exe

    # Hold the startup gate through validation, rollback and failure cleanup.
    $lockPath = Resolve-PackagePath $app '.bread-update.lock'
    $lock = [IO.FileStream]::new($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None, 1,
        [IO.FileOptions]::DeleteOnClose)
    $ownsInstall = $true
    if (!$NoProgressWindow) {
        try {
            $labels = Join-Path $stage 'progress.json'
            Assert-RegularPath $labels
            if ([IO.File]::Exists($labels)) {
                $translated = Get-Content -LiteralPath $labels -Raw -Encoding UTF8 | ConvertFrom-Json
                foreach ($label in @($progressLabels.Keys)) {
                    if ($translated.$label -is [string] -and $translated.$label) {
                        $progressLabels[$label] = $translated.$label
                    }
                }
            }
            $progress = New-UpdateProgress
        } catch {
            Write-Warning "Could not show installer progress: $_"
        }
    }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class BreadUpdateProcess {
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool QueryFullProcessImageName(IntPtr process, int flags, StringBuilder path, ref int length);
    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    public static string ImagePath(int pid) {
        IntPtr process = OpenProcess(0x1000, false, pid); // PROCESS_QUERY_LIMITED_INFORMATION
        if (process == IntPtr.Zero) return null;
        try {
            int length = 32768;
            var path = new StringBuilder(length);
            return QueryFullProcessImageName(process, 0, path, ref length) ? path.ToString() : null;
        } finally { CloseHandle(process); }
    }
}
'@
    $parent = Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue
    if ($parent -and [BreadUpdateProcess]::ImagePath($ParentProcessId) -eq $exe -and !$parent.WaitForExit(60000)) {
        throw 'RenderDocBread did not exit before the update timeout.'
    }
    $oldManifest = Resolve-PackagePath $app $manifestName
    $old = Read-Manifest $oldManifest $InstalledVersion
    if ($TargetVersion -le $InstalledVersion) { throw 'The update must be a newer release.' }
    $new = Expand-Update
    Show-UpdateProgress 'close' 55
    Close-InstallProcesses

    # Only replace files that still match the installed package. Leave other files
    # untouched, including local edits to files removed from the newer release.
    $replace = [Collections.Generic.List[string]]::new()
    Show-UpdateProgress 'check' 60
    $scanned = 0
    $totalChecks = $new.Count + $old.Count
    foreach ($relative in $new.Keys) {
        $scanned++
        if ($progress -and $scanned % 64 -eq 0) {
            Show-UpdateProgress 'check' (60 + [int](15 * $scanned / $totalChecks))
        }
        $target = Resolve-PackagePath $app $relative
        if (Test-Path -LiteralPath $target) {
            if (!$old.ContainsKey($relative)) { throw "An existing file would be overwritten: $target" }
            if (Test-PackageFile $target $new[$relative]) { continue }
            if (!(Test-PackageFile $target $old[$relative])) {
                throw "An existing file would be overwritten: $target"
            }
        }
        $replace.Add($relative)
    }
    $remove = [Collections.Generic.List[string]]::new()
    foreach ($relative in $old.Keys) {
        $scanned++
        if ($progress -and $scanned % 64 -eq 0) {
            Show-UpdateProgress 'check' (60 + [int](15 * $scanned / $totalChecks))
        }
        if ($new.ContainsKey($relative)) { continue }
        $target = Resolve-PackagePath $app $relative
        if (Test-PackageFile $target $old[$relative]) { $remove.Add($relative) }
    }

    # A legacy executable could be opened during extraction or hash checks.
    Show-UpdateProgress 'close' 75
    Close-InstallProcesses
    # Record the new version only after the entire payload has been installed.
    $replace.Add($manifestName)
    $totalMoves = $remove.Count + $replace.Count
    $moved = 0
    Show-UpdateProgress 'replace' 75
    foreach ($relative in $remove) {
        Move-UpdateFile (Join-Path $app $relative) (Join-Path $backup $relative)
        $moved++
        if ($progress -and $moved % 32 -eq 0) { Show-UpdateProgress 'replace' (75 + [int](20 * $moved / $totalMoves)) }
    }
    foreach ($relative in $replace) {
        $target = Join-Path $app $relative
        if ([IO.File]::Exists($target)) { Move-UpdateFile $target (Join-Path $backup $relative) }
        Move-UpdateFile (Join-Path $payload $relative) $target
        $moved++
        if ($progress -and $moved % 32 -eq 0) { Show-UpdateProgress 'replace' (75 + [int](20 * $moved / $totalMoves)) }
    }
} catch {
    $failed = $true
    $errors = @($_)
    Show-UpdateProgress 'rollback' $progressPercent
    for ($i = $journal.Count - 1; $i -ge 0; $i--) {
        try {
            Move-UpdatePath $journal[$i].To $journal[$i].From
        } catch {
            $rollbackFailed = $true
            $errors += $_
        }
    }
    if ($validStage) {
        $errors | Out-String | Set-Content -LiteralPath $log -Encoding UTF8 -ErrorAction Continue
    }
    if ($validStage -and !$rollbackFailed) {
        try {
            foreach ($directory in @($payload, $backup)) {
                Remove-UpdateDirectory $directory
            }
            [IO.File]::Delete($archivePath)
        } catch {
            $_ | Out-String | Add-Content -LiteralPath $log -Encoding UTF8 -ErrorAction Continue
        }
    }
} finally {
    if ($lock) { $lock.Dispose() }
}

# Keep the backup when rollback was blocked. In every other case remove the
# transient payload and archive. The error log stays available to the user.
try {
    if (!$ownsInstall) { exit 1 }
    if ($rollbackFailed) {
        Show-UpdateFailure
        exit 1
    }
    if (![IO.Directory]::Exists($restartDirectory)) { $restartDirectory = $app }
    if (!$failed) {
        Show-UpdateProgress 'restart' 96
        $newApp = Start-Process -FilePath $exe -WorkingDirectory $restartDirectory -PassThru
        Wait-ApplicationWindow $newApp
        Show-UpdateProgress 'clean' 99
        # Qt preserves the read-only resource permissions when it copies this script.
        Remove-UpdateDirectory $stage
        exit 0
    }
    if (!(Get-InstallProcesses | Where-Object { $_.Image -eq $exe })) {
        Show-UpdateProgress 'restore' 96
        $previousApp = Start-Process -FilePath $exe -WorkingDirectory $restartDirectory -PassThru `
            -ArgumentList @('--updatefailed', ('"{0}"' -f $log))
        Wait-ApplicationWindow $previousApp
    }
} catch {
    $_ | Out-String | Add-Content -LiteralPath $log -Encoding UTF8 -ErrorAction Continue
    $failed = $true
    Show-UpdateFailure
} finally {
    if ($progress) { $progress.Dispose() }
}
if ($failed) { exit 1 }
