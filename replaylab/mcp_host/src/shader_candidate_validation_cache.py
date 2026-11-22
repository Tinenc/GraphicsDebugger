"""Process-local reuse of an exact successful candidate reflection.

This is not an ABI authority or a persistent restore receipt. Entries are created
only after the Host validated a real replay reflection. Callers must revalidate
workspace files and obtain the current Bridge capture session before reuse; the
final Bridge transaction still checks the actual target and replacement owner.
"""
from __future__ import annotations

import copy
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping


def _sha(value: Any, length: int = 64) -> bool:
    return (isinstance(value, str) and len(value) == length
            and all(c in "0123456789abcdef" for c in value))


def _binding(status: Mapping[str, Any], request: Mapping[str, Any]) -> dict | None:
    source = status.get("source") or {}
    latest = status.get("latestCompiled") or {}
    gate = latest.get("abiGate") or {}
    compiler = status.get("compilerBinding") or {}
    capture = status.get("capture") or {}
    baseline = status.get("baseline") or {}
    shader = status.get("shader") or {}
    binding = request.get("binding") or {}
    if not all(isinstance(x, Mapping) for x in
               (source, latest, gate, compiler, capture, baseline, shader, binding)):
        return None
    # Missing older metadata is a cache miss, never an inferred proof. status()
    # has already verified actual include bytes, compiler executable/config,
    # immutable original/candidate blobs and the sealed ABI report.
    if (status.get("sourceDependenciesCurrent") is not True
            or status.get("abiCompatible") is not True
            or status.get("replayAbiValidated") is not True
            or status.get("abiValidationStatus") != "compatible"
            or latest.get("validationScope") != "replay-reflection-abi-compatible"
            or latest.get("replayAbiValidated") is not True
            or latest.get("abiCompatible") is not True
            or latest.get("requiresReplayAbiValidation") is not False
            or latest.get("semanticEquivalenceVerified") is not False
            or gate.get("status") != "compatible"
            or source.get("currentSha256") != latest.get("sourceSha256")
            or status.get("revision") != latest.get("revision")
            or gate.get("requestId") != request.get("requestId")):
        return None
    for value in (source.get("currentSha256"), source.get("dependenciesDigest"),
                  compiler.get("bindingSha256"), baseline.get("rawSha256"),
                  baseline.get("abiSha256"), latest.get("rawSha256"),
                  gate.get("requestId"), gate.get("reportSha256"),
                  gate.get("baselineAbiSha256"), gate.get("candidateAbiSha256"),
                  capture.get("captureId"), capture.get("catalogSha256")):
        if not _sha(value):
            return None
    if not _sha(status.get("workspaceId"), 32):
        return None
    expected = {"workspaceId": status["workspaceId"],
                "captureId": capture["captureId"], "revision": status["revision"],
                "candidateRawSha256": latest["rawSha256"],
                "baselineAbiSha256": baseline["abiSha256"],
                "stage": status.get("stage"), "entryPoint": status.get("entryPoint")}
    if any(binding.get(k) != v for k, v in expected.items()):
        return None
    return copy.deepcopy({
        "workspaceId": status["workspaceId"], "capture": dict(capture),
        "shader": dict(shader), "baseline": dict(baseline),
        "revision": status["revision"], "stage": status.get("stage"),
        "entryPoint": status.get("entryPoint"), "targetProfile": status.get("targetProfile"),
        "source": {key: source.get(key) for key in
                   ("path", "format", "currentSha256", "dependenciesDigest", "dependencyDocumentCount")},
        "compilerBinding": dict(compiler), "routePlan": status.get("routePlan"),
        "latestCompiled": dict(latest), "request": dict(request),
    })


class ShaderCandidateValidationCache:
    def __init__(self, limit: int = 32):
        self._limit = limit
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normcase(str(Path(path).expanduser().resolve()))

    def contains(self, path: str) -> bool:
        with self._lock:
            return self._key(path) in self._entries

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def remember(self, path: str, status: Mapping[str, Any], request: Mapping[str, Any],
                 validated: Mapping[str, Any]) -> None:
        binding = _binding(status, request)
        session = validated.get("captureSessionId")
        if (binding is None or not _sha(session, 32)
                or validated.get("compatible") is not True
                or validated.get("replayAbiValidated") is not True
                or validated.get("requiresReplayAbiValidation") is not False
                or validated.get("revision") != status.get("revision")
                or validated.get("requestId") != request.get("requestId")
                or validated.get("abiReportSha256") != status["latestCompiled"]["abiGate"]["reportSha256"]):
            return
        key = self._key(path)
        with self._lock:
            self._entries[key] = {"binding": binding, "session": session,
                                  "validation": copy.deepcopy(dict(validated))}
            self._entries.move_to_end(key)
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)

    def lookup(self, path: str, status: Mapping[str, Any], request: Mapping[str, Any],
               session: str) -> dict | None:
        binding = _binding(status, request)
        if binding is None or not _sha(session, 32):
            return None
        key = self._key(path)
        with self._lock:
            entry = self._entries.get(key)
            if not entry or entry["session"] != session or entry["binding"] != binding:
                return None
            self._entries.move_to_end(key)
            result = copy.deepcopy(entry["validation"])
        result.update(reusedCurrentSessionValidation=True,
                      freshPreliminaryReplayReflectionPerformed=False)
        return result
