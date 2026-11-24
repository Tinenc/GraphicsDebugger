"""Large material JSON survives inline/transport compaction with content evidence."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

from src import tool_runner


def operation(argv, *, min_files=0):
    return {"name": "read_material", "argv": argv,
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "result": {"successWhen": {"exitCode": 0, "minFiles": min_files}}}


def test_real_json_larger_than_inline_limit_is_saved_with_exact_bytes_and_digest(tmp_path):
    script = tmp_path / "reader.py"
    script.write_text("import json, sys\nsys.stdout.buffer.write(json.dumps({'padding':'x'*70000,'scalars':{'TailParameter':0.625}}).encode('utf-8'))\n", encoding="utf-8")
    receipt = tool_runner.run_operation(operation(["{program}", str(script)]), {},
                                        program=sys.executable, output_root=tmp_path / "outputs")
    assert receipt["succeeded"] and receipt["stdoutTruncated"]
    full = Path(receipt["stdoutPath"]).read_bytes()
    assert json.loads(full)["scalars"] == {"TailParameter": 0.625}
    assert receipt["stdoutByteSize"] == receipt["stdoutFileByteSize"] == len(full)
    assert receipt["stdoutSha256"] == receipt["stdoutFileSha256"] == hashlib.sha256(full).hexdigest()
    assert not receipt["stdoutFileTruncated"]
    assert receipt["producedFileCount"] == 0


def test_even_small_json_has_a_file_for_transport_compaction_without_faking_export_success(tmp_path):
    raw = json.dumps({"value": "材质" * 1000}).encode("utf-8")
    done = subprocess.CompletedProcess([], 0, stdout=raw, stderr=b"")
    with mock.patch.object(tool_runner.subprocess, "run", return_value=done):
        receipt = tool_runner.run_operation(operation(["{program}"], min_files=1), {},
                                            program=sys.executable, output_root=tmp_path)
    assert Path(receipt["stdoutPath"]).read_bytes() == raw
    assert not receipt["stdoutTruncated"]
    assert not receipt["succeeded"]
    assert receipt["producedFileCount"] == 0


def test_oversize_stdout_file_is_explicitly_incomplete_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_runner, "MAX_OUTPUT_ARTIFACT_BYTES", 128)
    raw = b"a" * 200
    done = subprocess.CompletedProcess([], 0, stdout=raw, stderr=b"")
    with mock.patch.object(tool_runner.subprocess, "run", return_value=done):
        receipt = tool_runner.run_operation(operation(["{program}"]), {}, program=sys.executable, output_root=tmp_path)
    assert receipt["stdoutFileTruncated"]
    assert receipt["stdoutByteSize"] == 200
    assert receipt["stdoutFileByteSize"] == 128
    assert Path(receipt["stdoutPath"]).read_bytes() == raw[:128]
    assert receipt["stdoutSha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["stdoutFileSha256"] == hashlib.sha256(raw[:128]).hexdigest()


def test_timeout_preserves_observed_partial_stdout_without_claiming_success(tmp_path):
    with mock.patch.object(tool_runner.subprocess, "run", side_effect=subprocess.TimeoutExpired("reader", 1, output=b'{"partial":')):
        receipt = tool_runner.run_operation(operation(["{program}"]), {}, program=sys.executable, output_root=tmp_path)
    assert receipt["timedOut"] and not receipt["succeeded"]
    assert Path(receipt["stdoutPath"]).read_bytes() == b'{"partial":'
