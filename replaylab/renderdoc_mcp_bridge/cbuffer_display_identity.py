"""Bounded, read-only reuse of already observed CBuffer display identities.

No reflection fetch, event selection, buffer edit or replay is performed here.
The Bridge validates its live owner before consulting this process-local cache.
"""
from __future__ import print_function

import re


_observations = {}
_MAX_OBSERVATIONS = 256


def _key(identity):
    return (identity.get("captureSessionId"), str(identity.get("shaderResourceId") or ""),
            identity.get("rawSha256"), identity.get("stage"), identity.get("entryPoint", ""))


def remember(identity, capture_token, mutation_epoch):
    original = identity.get("rawSha256")
    display = identity.get("activeDisplayShaderRawSha256")
    replacement = identity.get("activeReplacementShaderRawSha256", "")
    if (not isinstance(original, str) or not re.fullmatch(r"[0-9a-f]{64}", original)
            or not isinstance(display, str) or display not in (original, replacement)
            or not isinstance(replacement, str) or replacement and not re.fullmatch(r"[0-9a-f]{64}", replacement)):
        return
    _observations[_key(identity)] = {
        "captureToken": capture_token, "mutationEpoch": mutation_epoch,
        "originalRawSha256": original, "activeDisplayShaderRawSha256": display,
        "activeReplacementShaderRawSha256": replacement,
    }
    while len(_observations) > _MAX_OBSERVATIONS:
        del _observations[next(iter(_observations))]


def observe(identity, capture_token, mutation_epoch, replacement_sha256):
    result = {"schema": "bread-cbuffer-display-identity/v1", "status": "refresh-required",
              "originalRawSha256": identity.get("rawSha256", ""),
              "captureSessionId": identity.get("captureSessionId", ""),
              "stage": identity.get("stage", ""), "entryPoint": identity.get("entryPoint", ""),
              "replayMutation": False, "replayRead": False,
              "refreshTool": "publish_cbuffer_aliases"}
    cached = _observations.get(_key(identity))
    if (cached is None or cached["captureToken"] != capture_token or
            cached["mutationEpoch"] != mutation_epoch or
            cached["activeReplacementShaderRawSha256"] != replacement_sha256):
        return result
    return dict(result, status="current", mutationEpoch=mutation_epoch,
                activeDisplayShaderRawSha256=cached["activeDisplayShaderRawSha256"],
                activeReplacementShaderRawSha256=replacement_sha256)
