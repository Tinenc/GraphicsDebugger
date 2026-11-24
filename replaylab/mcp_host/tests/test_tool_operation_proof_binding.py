"""A recorded invocation belongs to the program content that was actually proved."""
import hashlib
from pathlib import Path
from unittest import mock

import pytest

from src import tool_runner


def operation(program, digest):
    return {"id": "reader", "connection": {"kind": "cli", "command": str(program)}, "interface": {
        "operations": [{"name": "read_material", "argv": ["{program}", "{asset}"],
                        "inputSchema": {"type": "object", "properties": {"asset": {"type": "string"}},
                                        "required": ["asset"]},
                        "proved": {"sample": "samples/mat.asset", "programSha256": digest}}]}}


def test_catalog_preserves_proof_and_changed_program_is_refused_before_process_creation(tmp_path):
    program = tmp_path / "reader.exe"
    program.write_bytes(b"original")
    digest = hashlib.sha256(program.read_bytes()).hexdigest()
    recorded = tool_runner.declared_operations([operation(program, digest)], include_builtins=False)[0]
    assert recorded["proved"] is True
    assert recorded["proofStatus"] == "program-bound"
    assert recorded["proof"]["programSha256"] == digest
    program.write_bytes(b"upgraded")
    output = tmp_path / "outputs"
    with mock.patch.object(tool_runner.subprocess, "run") as run:
        with pytest.raises(tool_runner.ToolRunError, match="changed since"):
            tool_runner.run_operation(recorded, {"asset": "mat.asset"}, program=str(program), output_root=output)
    run.assert_not_called()
    assert not output.exists()


def test_current_proof_runs_headlessly_and_reports_the_hash_checked_before_launch(tmp_path):
    program = tmp_path / "reader.exe"
    program.write_bytes(b"current")
    digest = hashlib.sha256(program.read_bytes()).hexdigest()
    recorded = tool_runner.declared_operations([operation(program, digest)], include_builtins=False)[0]
    completed = mock.Mock(returncode=0, stdout=b'{}', stderr=b'')
    with mock.patch.object(tool_runner.subprocess, "run", return_value=completed) as run:
        receipt = tool_runner.run_operation(recorded, {"asset": "mat.asset"}, program=str(program),
                                            output_root=tmp_path / "outputs")
    assert receipt["succeeded"] is True
    assert receipt["programSha256"] == digest
    assert run.call_args.kwargs["stdin"] == tool_runner.subprocess.DEVNULL
    assert run.call_args.kwargs["shell"] is False


def test_explicit_unproved_probe_remains_callable_but_does_not_mint_proof(tmp_path):
    program = tmp_path / "reader.exe"
    program.write_bytes(b"probe")
    raw = operation(program, "")["interface"]["operations"][0]
    raw["proved"] = None
    completed = mock.Mock(returncode=0, stdout=b'{}', stderr=b'')
    with mock.patch.object(tool_runner.subprocess, "run", return_value=completed):
        receipt = tool_runner.run_operation(raw, {"asset": "mat.asset"}, program=str(program), output_root=tmp_path)
    assert receipt["succeeded"] is True
    assert raw["proved"] is None
