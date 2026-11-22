from __future__ import annotations

import subprocess
import sys

from src.shader_workspace.source_observation import (
    source_observation_available,
    source_observation_transaction,
)


def test_observation_pause_is_cross_process_and_leftover_file_is_not_a_lock(tmp_path):
    code = """
import sys
from src.shader_workspace.source_observation import source_observation_available
print(source_observation_available(sys.argv[1]), flush=True)
"""
    with source_observation_transaction(tmp_path):
        # Same-thread nesting is needed for status callbacks inside a transaction.
        assert source_observation_available(tmp_path)
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
                                capture_output=True, text=True, check=True, timeout=15)
        assert result.stdout.strip() == "False"
    assert (tmp_path / "watch" / ".source-observation.lock").exists()
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
                            capture_output=True, text=True, check=True, timeout=15)
    assert result.stdout.strip() == "True"


def test_process_exit_releases_observation_without_manual_recovery(tmp_path):
    code = """
import os, sys
from src.shader_workspace.source_observation import source_observation_transaction
with source_observation_transaction(sys.argv[1]):
    os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True, timeout=15)
    assert source_observation_available(tmp_path)
