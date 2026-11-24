"""Read exact CBuffer display lineage without compiling, replaying or ABI checks.

Original and replacement resources are immutable during one capture session.
Their identities do not require selecting the workspace's original Draw.  This
module reads the already loaded panel reflection where available, and otherwise
reads the exact native resource.  The latter is a read, never a replacement.
"""
from __future__ import print_function

import hashlib
import re

from .cbuffer_alias_identity import reflection_alias_blocks
from .cbuffer_display_identity import observe, remember


def _sha(bridge, reflection):
    raw = bridge._shader_raw_bytes(reflection) if reflection is not None else b""
    return hashlib.sha256(raw).hexdigest() if raw else ""


def _hex(value, name, size):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % size, value):
        raise ValueError("%s must be a lowercase hexadecimal identity" % name)
    return value


def _debug_session(bridge, session, rid):
    manager = getattr(bridge, "_shader_visual_debug_manager", None)
    value = getattr(manager, "_session", None)
    facts = (value or {}).get("facts") or {}
    if (facts.get("scope") != "shader-resource" or facts.get("capture") != session
            or str(facts.get("shaderId")) != str(rid)):
        return None
    if value.get("recoveryRequired") or value.get("status") not in ("active", "comparing"):
        raise RuntimeError("CBuffer display preview ownership needs recovery")
    return value


def _binding(bridge, params):
    session = _hex(params.get("expected_capture_session_id"), "capture session", 32)
    original_sha = _hex(params.get("expected_original_sha256"), "original Shader SHA", 64)
    if session != bridge._capture_session_id or not bridge._request_capture_loaded():
        raise RuntimeError("Capture session changed before CBuffer display lookup")
    token = bridge._current_capture_token()
    if token is None:
        raise RuntimeError("Capture session changed before CBuffer display lookup")
    rid = int(params.get("expected_original_resource_id") or 0)
    if rid <= 0:
        raise ValueError("CBuffer display lookup needs an original Shader resource")
    stage_name = bridge._normalise_stage_name(params.get("stage"))
    stage = dict(bridge._available_shader_stages([stage_name])).get(stage_name)
    if stage is None:
        raise ValueError("unsupported CBuffer Shader stage")
    entry = str(params.get("entry_point") or "")
    owner = bridge._shared_shader_replacements.get(rid)
    if owner is not None and any((
            owner.get("captureSessionId") != session, owner.get("captureToken") != token,
            owner.get("expectedOriginalResourceId") != rid,
            owner.get("expectedOriginalSha256") != original_sha,
            owner.get("stageName") != stage_name, owner.get("entryPoint") != entry)):
        raise RuntimeError("CBuffer display owner belongs to a different Shader or capture session")
    if any(row.get("expectedOriginalResourceId") == rid for row in bridge._event_shader_overrides.values()):
        raise RuntimeError("CBuffer display lookup has an event-scoped Shader owner")
    replacement_sha = str((owner or {}).get("candidateRawSha256") or "")
    if owner is not None:
        _hex(replacement_sha, "replacement Shader SHA", 64)
    candidate = params.get("expected_candidate_sha256")
    if candidate is not None and candidate != replacement_sha:
        raise RuntimeError("CBuffer display replacement candidate changed")
    capture_id = str((owner or {}).get("captureId") or
                     (getattr(bridge, "_symbol_capture_snapshot", {}) or {}).get("captureId") or "")
    expected_capture = params.get("expected_capture_id")
    if expected_capture:
        _hex(expected_capture, "capture identity", 64)
        if capture_id and expected_capture != capture_id:
            raise RuntimeError("CBuffer display owner belongs to a different capture")
    original = (owner or {}).get("originalResource")
    if original is None:
        original = bridge._invoke_on_ui_sync(lambda: bridge._context_resource_ids().get(rid))
    if original is None:
        raise RuntimeError("CBuffer display original Shader is unavailable")
    bridge._require_shared_shader_ui_owner(original, owner)
    debug = _debug_session(bridge, session, rid)
    return {"captureSessionId": session, "captureId": capture_id, "captureToken": token,
            "captureIdentityBasis": "capture-owner" if capture_id else "session-original-bytes",
            "shaderResourceId": rid, "rawSha256": original_sha, "stage": stage_name,
            "entryPoint": entry, "stageEnum": stage, "original": original, "owner": owner,
            "debug": debug, "activeReplacementShaderRawSha256": replacement_sha}


def _unchanged(bridge, binding):
    if (bridge._capture_session_id != binding["captureSessionId"]
            or bridge._current_capture_token() != binding["captureToken"]
            or bridge._shared_shader_replacements.get(binding["shaderResourceId"]) is not binding["owner"]
            or _debug_session(bridge, binding["captureSessionId"], binding["shaderResourceId"]) is not binding["debug"]):
        raise RuntimeError("CBuffer display owner changed during lookup")
    bridge._require_shared_shader_ui_owner(binding["original"], binding["owner"])


def _target(bridge, binding):
    debug = binding["debug"]
    if debug and debug["status"] == "active":
        return debug["cached"]["target"]
    return (binding["owner"] or {}).get("targetResource")


def _native_owner(bridge, ctrl, binding):
    getter = getattr(ctrl, "GetTargetShaderReplacement", None)
    if not callable(getter):
        raise RuntimeError("CBuffer display requires native Shader replacement ownership queries")
    current = getter(binding["original"])
    bridge._require_healthy_replay(ctrl)
    if bridge._resource_id_int(current) != bridge._resource_id_int(_target(bridge, binding)):
        raise RuntimeError("CBuffer panel Shader reflection owner changed outside Bread")


def _reflection_snapshot(bridge, reflection, include_blocks=True):
    """Copy primitive data while the native reflection's owner is still alive."""
    if reflection is None:
        return None
    value = {"rawSha256": _sha(bridge, reflection),
             "entryPoint": str(getattr(reflection, "entryPoint", "") or "")}
    if include_blocks:
        value["blocks"] = reflection_alias_blocks(reflection, bridge._shader_constant_fields,
                                                  bridge._constant_field_annotations_are_complete)
    return value


def _ui_snapshot(bridge, binding, include_blocks=True):
    def read():
        pipeline = bridge._ctx.CurPipelineState()
        if pipeline is None:
            return None
        selected = bridge._resource_id_int(pipeline.GetShader(binding["stageEnum"]))
        if selected not in (binding["shaderResourceId"], bridge._resource_id_int(_target(bridge, binding))):
            return None
        reported = pipeline.GetShaderEntryPoint(binding["stageEnum"])
        entry = str(getattr(reported, "name", reported) or "")
        if binding["entryPoint"] and entry and entry != binding["entryPoint"]:
            return None
        # PublishedPipelineState owns this borrowed reflection and destroys it
        # when the UI publishes another EID. No native pointer may escape this
        # callback or be handed to the later replay-thread transaction.
        return _reflection_snapshot(bridge, pipeline.GetShaderReflection(binding["stageEnum"]),
                                    include_blocks)
    return bridge._invoke_on_ui_sync(read)


def _display(binding, snapshot):
    sha = (snapshot or {}).get("rawSha256") or ""
    allowed = {binding["rawSha256"], binding["activeReplacementShaderRawSha256"]}
    debug = binding["debug"]
    ephemeral = bool(debug and debug["status"] == "active")
    if ephemeral:
        debug_sha = debug["cached"].get("rawSha256") or ""
        if not debug_sha:
            raise RuntimeError("CBuffer display preview reflection is unavailable")
        allowed.add(debug_sha)
    if not sha or sha not in allowed:
        raise RuntimeError("CBuffer panel Shader reflection belongs to a different Shader")
    return {"activeDisplayShaderRawSha256": sha, "activeDisplayEphemeral": ephemeral,
            "activeReplacementShaderRawSha256": binding["activeReplacementShaderRawSha256"]}


def _public(binding):
    return {key: binding[key] for key in ("captureId", "captureSessionId", "captureIdentityBasis",
            "shaderResourceId", "rawSha256", "stage", "entryPoint")}


def read_alias_identity(bridge, params):
    binding = _binding(bridge, params)
    snapshot = _ui_snapshot(bridge, binding)

    def run(ctrl):
        if (bridge._capture_session_id != binding["captureSessionId"] or
                bridge._current_capture_token() != binding["captureToken"]):
            raise RuntimeError("CBuffer display capture changed before reflection read")
        _native_owner(bridge, ctrl, binding)
        original_reflection = (binding["owner"] or {}).get("capturedOriginalReflection")
        debug = binding["debug"]
        if original_reflection is None and debug:
            original_reflection = debug["facts"].get("baselineReflection")
        original = _reflection_snapshot(bridge, original_reflection)
        if original is None and (snapshot or {}).get("rawSha256") == binding["rawSha256"]:
            original = snapshot
        if original is None:
            entries = list(ctrl.GetShaderEntryPoints(binding["original"]) or [])
            entry = bridge._exact_shader_entry(entries, binding["entryPoint"], binding["stage"])
            if entry is None:
                raise RuntimeError("CBuffer original Shader entry point/stage changed")
            original = _reflection_snapshot(bridge, ctrl.GetShader(bridge.rd.ResourceId(), binding["original"], entry))
        if (not original or original["rawSha256"] != binding["rawSha256"]
                or (original["entryPoint"] or binding["entryPoint"]) != binding["entryPoint"]):
            raise RuntimeError("CBuffer retained captured-original Shader bytes or entry point changed")
        active = snapshot
        if active is None:
            if debug and debug["status"] == "active":
                active = _reflection_snapshot(bridge, debug["cached"].get("reflection"))
            elif binding["owner"] is None:
                active = original
            else:
                target = _target(bridge, binding)
                entries = list(ctrl.GetShaderEntryPoints(target) or [])
                entry = bridge._exact_shader_entry(entries, binding["entryPoint"], binding["stage"])
                if entry is None and bridge._shader_api_token(ctrl) in ("d3d11", "d3d12"):
                    # DXBC reflection can expose the synthetic `main` name
                    # instead of the source function. The exact candidate
                    # bytes and native owner remain checked below.
                    matches = [value for value in entries
                               if bridge._known_stage_name(value) == binding["stage"]]
                    entry = matches[0] if len(matches) == 1 else None
                if entry is None:
                    raise RuntimeError("CBuffer replacement Shader entry point/stage changed")
                active = _reflection_snapshot(bridge, ctrl.GetShader(bridge.rd.ResourceId(), target, entry))
        display = _display(binding, active)
        result = dict(_public(binding), schema="replaylab-cbuffer-alias-identity/v1",
                      eventId=int(params.get("event_id") or 0), blocks=original["blocks"],
                      eventIdentityVerified=False, replayMutation=False, replayRead=True,
                      physicalAbiValidated=False, **display)
        if display["activeDisplayShaderRawSha256"] != binding["rawSha256"]:
            result["activeDisplayBlocks"] = active["blocks"]
        _native_owner(bridge, ctrl, binding)
        return result

    result = bridge._replay(run)
    _unchanged(bridge, binding)
    # Debug targets are session-local. Never let a durable publication or a
    # later stop/compare consume their identity as the persistent replacement.
    if not result["activeDisplayEphemeral"]:
        remember(result, binding["captureToken"], bridge._shared_shader_mutation_epoch)
    return result


def read_display_identity(bridge, params):
    binding = _binding(bridge, params)
    snapshot = _ui_snapshot(bridge, binding, include_blocks=False)
    fresh = dict(_public(binding), **_display(binding, snapshot)) if snapshot is not None else None
    _unchanged(bridge, binding)
    debug = binding["debug"]
    ephemeral = bool(debug and debug["status"] == "active")
    if fresh is not None and not ephemeral:
        remember(fresh, binding["captureToken"], bridge._shared_shader_mutation_epoch)
    result = observe(binding, binding["captureToken"], bridge._shared_shader_mutation_epoch,
                     binding["activeReplacementShaderRawSha256"])
    result.update(captureId=binding["captureId"], captureIdentityBasis=binding["captureIdentityBasis"],
                  activeDisplayEphemeral=ephemeral)
    if ephemeral:
        # There is no replay/native query in this freshness endpoint. Only the
        # loaded matching UI snapshot can provide a current ephemeral display.
        result.pop("activeDisplayShaderRawSha256", None)
        result.update(status="refresh-required")
        if fresh is not None:
            result.update(fresh, status="current")
    return result
