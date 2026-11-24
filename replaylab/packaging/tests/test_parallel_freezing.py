"""Exercise real PowerShell child jobs without running the expensive freezer."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell packaging")
def test_freezers_overlap_use_separate_caches_and_propagate_exit_codes(tmp_path):
    (tmp_path / "PyInstaller.py").write_text('''
import json, os, pathlib, sys, time
name = sys.argv[sys.argv.index("--name") + 1]
root = pathlib.Path(os.environ["FREEZE_TEST_ROOT"])
(root / (name + ".started")).touch()
end = time.monotonic() + 20
while len(list(root.glob("*.started"))) < 3:
    if time.monotonic() > end: raise RuntimeError("Freezers did not overlap")
    time.sleep(.05)
(root / (name + ".json")).write_text(json.dumps({"cache": os.environ["PYINSTALLER_CONFIG_DIR"], "args": sys.argv}))
sys.exit(7 if name == "bad" else 0)
''')
    script = tmp_path / "probe.ps1"
    script.write_text('''
param($Builder, $Python, $WorkDirectory)
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($Builder, [ref]$null, [ref]$null)
$ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -in @('Invoke-PyInstallerBuild','Assert-NoReparseComponents')}, $false) |
    ForEach-Object { Invoke-Expression $_.Extent.Text }
$jobs = @()
try {
    foreach ($name in @('first', 'second', 'bad')) {
        $jobs += Invoke-PyInstallerBuild $Python $name 'entry path.py' $WorkDirectory `
            $WorkDirectory $WorkDirectory $WorkDirectory -AddData @('data path:target')
    }
    $jobs | Wait-Job | Out-Null
    foreach ($job in $jobs) { Receive-Job $job -ErrorAction Continue | Out-Host }
    if ($jobs[0].State -ne 'Completed' -or $jobs[1].State -ne 'Completed' -or
        $jobs[2].State -ne 'Failed') { throw 'Incorrect exit-code propagation' }
} finally {
    $jobs | Stop-Job
    $jobs | Remove-Job -Force
}
''')
    env = dict(os.environ, PYTHONPATH=str(tmp_path), FREEZE_TEST_ROOT=str(tmp_path))
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(script), str(Path(__file__).parents[1] / "Build-ReplayLabRuntime.ps1"),
        sys.executable, str(tmp_path / "build work")], env=env, capture_output=True, text=True, timeout=65)
    assert result.returncode == 0, result.stdout + result.stderr
    records = [json.loads((tmp_path / (name + ".json")).read_text()) for name in ("first", "second", "bad")]
    assert len({r["cache"] for r in records}) == 3
    assert all("data path:target" in r["args"] and "entry path.py" in r["args"] for r in records)
