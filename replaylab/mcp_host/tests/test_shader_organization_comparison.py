import hashlib
from pathlib import Path
from unittest import mock

import pytest

from src import server


def test_host_comparison_binds_both_immutable_candidates(tmp_path):
    candidates = []
    for number in (1, 2):
        path = tmp_path / (str(number) + ".dxil")
        path.write_bytes(bytes([number]))
        candidates.append({"revision": number, "candidateBlobPath": str(path),
                           "rawSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "sourceSha256": str(number) * 64})
    status = {"eventId": 142, "capture": {"captureId": "a" * 64},
              "shader": {"resourceId": 902}, "baseline": {"rawSha256": "b" * 64},
              "latestCompiled": candidates[1]}
    service = mock.Mock()
    service.status.return_value = status
    service.candidate_reflection_request.return_value = {
        "params": {"encoding": "dxil", "stage": "fragment", "entry_point": "main"}}
    observed = {"captureId": "a" * 64, "captureSessionId": "c" * 32,
                "eventId": 142, "accepted": True, "replayRestored": True}
    with mock.patch.object(server, "_shader_workspace_service", return_value=service), \
         mock.patch.object(server, "_ensure_workspace_capture_catalog_identity"), \
         mock.patch.object(server, "_shader_organization_anchor_event", return_value=142), \
         mock.patch.object(server.ipc, "call", side_effect=[{"captureSessionId": "c" * 32}, observed]) as call:
        result = server._shader_organization_compare_candidates(str(tmp_path), *candidates)
        params = call.call_args.args[1]
        assert params["baseline"]["path"] == str(Path(candidates[0]["candidateBlobPath"]).resolve())
        assert params["candidate"]["rawSha256"] == candidates[1]["rawSha256"]
        assert params["expected_original_resource_id"] == 902
        assert result["baselineSourceSha256"] != result["candidateSourceSha256"]
        assert result["scope"] == "captured-event-output"
    candidates[0]["rawSha256"] = "d" * 64
    with mock.patch.object(server, "_shader_workspace_service", return_value=service), \
         mock.patch.object(server, "_ensure_workspace_capture_catalog_identity"), \
         mock.patch.object(server.ipc, "call", return_value={"captureSessionId": "c" * 32}) as call:
        with pytest.raises(RuntimeError, match="candidate bytes changed"):
            server._shader_organization_compare_candidates(str(tmp_path), *candidates)
        assert call.call_count == 1
