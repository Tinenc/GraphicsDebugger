#Requires -Version 5.1
<# Run a replay script using the packaged Python 3.6 DLL, without starting qrenderdoc.
   The script uses the same renderdoc.pyd/DLL as the selected Development or Release build. #>
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$Script,
  [string[]]$ScriptArguments = @(),
  [string]$Configuration = 'Development'
)
$ErrorActionPreference = 'Stop'
$perfRepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$perfBin = (Resolve-Path (Join-Path $perfRepoRoot "x64/$Configuration")).Path
$perfScript = (Resolve-Path -LiteralPath $Script).Path
foreach($name in @('python36.dll','python36.zip','pymodules/renderdoc.pyd','renderdoc.dll')) {
  if(-not (Test-Path -LiteralPath (Join-Path $perfBin $name))) { throw "Missing bundled dependency: $name" }
}
# A successful import does not establish SWIG/native ABI compatibility. Reject clearly stale
# bindings before loading either DLL; public pipeline structs can otherwise corrupt native memory.
$perfBindings = Get-Item -LiteralPath (Join-Path $perfBin 'pymodules/renderdoc.pyd')
$perfCoreLibrary = Get-Item -LiteralPath (Join-Path $perfBin 'renderdoc.dll')
$perfNewestAPI = Get-ChildItem -LiteralPath (Join-Path $perfRepoRoot 'renderdoc/api/replay') -Filter '*.h' -File |
  Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if($perfNewestAPI -and $perfBindings.LastWriteTimeUtc -lt $perfNewestAPI.LastWriteTimeUtc) {
  throw ("Stale renderdoc.pyd: it predates public API header {0}. Build matching core and bindings before replay: " +
    "MSBuild qrenderdoc/Code/pyrenderdoc/pyrenderdoc_module.vcxproj /m:2 /t:Build /p:Configuration={1} " +
    "/p:Platform=x64 /p:ReplayOptimizeDevelopment=true /p:SolutionDir=`"{2}\`"") -f $perfNewestAPI.Name,$Configuration,$perfRepoRoot
}
if($perfBindings.LastWriteTimeUtc -lt $perfCoreLibrary.LastWriteTimeUtc) {
  Write-Warning 'renderdoc.dll is newer than renderdoc.pyd. Verify they were built against the same public API; timestamps alone cannot establish ABI compatibility.'
}
if(-not ('ReplayPerf.EmbeddedPython36' -as [type])) {
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace ReplayPerf {
  public static class EmbeddedPython36 {
    [DllImport("kernel32", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool SetDllDirectory(string directory);
    [DllImport("python36", CallingConvention=CallingConvention.Cdecl)]
    public static extern void Py_SetPythonHome(IntPtr path);
    [DllImport("python36", CallingConvention=CallingConvention.Cdecl)]
    public static extern void Py_SetPath(IntPtr path);
    [DllImport("python36", CallingConvention=CallingConvention.Cdecl)]
    public static extern void Py_Initialize();
    [DllImport("python36", CallingConvention=CallingConvention.Cdecl)]
    public static extern int PyRun_SimpleString(byte[] script);
    [DllImport("python36", CallingConvention=CallingConvention.Cdecl)]
    public static extern int Py_FinalizeEx();
  }
}
'@
}
$perfArgv = @($perfScript) + $ScriptArguments
# CPython 3.6's CRT can retain an older environment snapshot when embedded in an existing
# PowerShell process. Transfer current process values in memory before executing the script.
# Never print the payload: environment variables may contain credentials.
$perfEnvironment = @{}
foreach($entry in [Environment]::GetEnvironmentVariables('Process').GetEnumerator()) {
  $perfEnvironment[[string]$entry.Key] = [string]$entry.Value
}
$perfEnvironment['PATH'] = $perfBin + ';' + $perfEnvironment['PATH']
$perfPayload = @{argv=$perfArgv;script=$perfScript;environment=$perfEnvironment} | ConvertTo-Json -Compress
$perfEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($perfPayload))
$perfPythonCode = @"
import base64, json, os, runpy, sys
payload = json.loads(base64.b64decode('$perfEncoded').decode('utf-8'))
os.environ.update(payload['environment'])
sys.argv = payload['argv']
sys.path.insert(0, os.path.dirname(payload['script']))
try:
    runpy.run_path(payload['script'], run_name='__main__')
except SystemExit as error:
    if error.code not in (None, 0):
        raise RuntimeError('Replay script exited with code {}'.format(error.code))
"@
$perfHomePtr = [Runtime.InteropServices.Marshal]::StringToHGlobalUni($perfBin)
$perfSearchPath = (Join-Path $perfBin 'python36.zip') + ';' + $perfBin + ';' + (Join-Path $perfBin 'pymodules')
$perfPathPtr = [Runtime.InteropServices.Marshal]::StringToHGlobalUni($perfSearchPath)
$perfSavedPath = $env:PATH
$perfInitialised = $false
try {
  $env:PATH = $perfBin + ';' + $env:PATH
  if(-not [ReplayPerf.EmbeddedPython36]::SetDllDirectory($perfBin)) { throw 'SetDllDirectory failed' }
  [ReplayPerf.EmbeddedPython36]::Py_SetPythonHome($perfHomePtr)
  [ReplayPerf.EmbeddedPython36]::Py_SetPath($perfPathPtr)
  [ReplayPerf.EmbeddedPython36]::Py_Initialize()
  $perfInitialised = $true
  $perfExit = [ReplayPerf.EmbeddedPython36]::PyRun_SimpleString([Text.Encoding]::UTF8.GetBytes($perfPythonCode + [char]0))
  if($perfExit -ne 0) { throw "Replay script failed (PyRun_SimpleString returned $perfExit)" }
} finally {
  if($perfInitialised) { [void][ReplayPerf.EmbeddedPython36]::Py_FinalizeEx() }
  [void][ReplayPerf.EmbeddedPython36]::SetDllDirectory($null)
  $env:PATH = $perfSavedPath
  [Runtime.InteropServices.Marshal]::FreeHGlobal($perfPathPtr)
  [Runtime.InteropServices.Marshal]::FreeHGlobal($perfHomePtr)
}
