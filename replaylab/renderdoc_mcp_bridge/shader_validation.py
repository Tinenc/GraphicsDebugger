"""Bounded whole-output observations for shader organization (Python 3.6).

This is captured-input evidence, never a proof for unobserved inputs. The
comparison executes in one replay callback and restores the previous mapping
before returning; it does not publish a replacement or update UI ownership.
"""
import hashlib
import math
import struct
import time


TOLERANCES = {"floatAbsolute": 0.0001, "floatRelative": 0.001,
              "halfFloatAbsolute": 1.0 / 1024.0,
              "depthAbsolute": 0.000001, "integerExact": True,
              "normalizedQuantizationSteps": 1}
OUTPUT_LIMIT = 32 * 1024 * 1024
TOTAL_LIMIT = 64 * 1024 * 1024
MAX_MISMATCH_SAMPLES = 32
BASELINE_REPEAT_START_LIMIT_SECONDS = 45.0


def _enum(value):
    raw = "" if value is None else value
    name = getattr(raw, "name", None)
    return str(name if name is not None else raw).strip().rsplit(".", 1)[-1].lower()


def _unsigned_float(bits, mantissa_bits):
    """Decode the unsigned 5-bit-exponent floats used by R11G11B10."""
    exponent, mantissa = bits >> mantissa_bits, bits & ((1 << mantissa_bits) - 1)
    if exponent == 31:
        return float("nan") if mantissa else float("inf")
    if exponent == 0:
        return math.ldexp(float(mantissa), -14 - mantissa_bits)
    return math.ldexp(float((1 << mantissa_bits) + mantissa), exponent - 15 - mantissa_bits)


_UNSIGNED_FLOAT11 = tuple(_unsigned_float(bits, 6) for bits in range(2048))
_UNSIGNED_FLOAT10 = tuple(_unsigned_float(bits, 5) for bits in range(1024))


def _packed_components(raw, format_type):
    for (word,) in struct.iter_unpack("<I", raw):
        if format_type == "r11g11b10":
            yield _UNSIGNED_FLOAT11[word & 2047]
            yield _UNSIGNED_FLOAT11[(word >> 11) & 2047]
            yield _UNSIGNED_FLOAT10[word >> 22]
        else:
            yield word & 1023
            yield (word >> 10) & 1023
            yield (word >> 20) & 1023
            yield word >> 30


def compare_bytes(before, after, descriptor):
    """Compare every stored component, without diluting errors in an average."""
    if not before or len(before) != len(after):
        raise ValueError("output bytes are empty or changed size")
    exact = before == after
    kind = _enum(descriptor.get("componentType"))
    width = descriptor.get("componentByteWidth", 0)
    count = descriptor.get("componentCount", 0)
    result = {"byteExact": exact, "byteSize": len(before),
              "baselineSha256": hashlib.sha256(before).hexdigest(),
              "candidateSha256": hashlib.sha256(after).hexdigest(),
              "maximumAbsoluteError": 0.0, "failedComponents": 0,
              "comparedComponents": 0, "accepted": exact,
              "baselineNonFiniteComponents": 0,
              "mismatchSamples": [], "mismatchSamplesTruncated": False,
              "mismatchBounds": None, "worstMismatch": None}
    if descriptor["kind"] in ("storage-buffer", "storage-image"):
        result.update(comparedComponents=len(before), comparisonKind="byte-exact-storage")
        if not exact:
            for offset, (left, right) in enumerate(zip(before, after)):
                if left != right:
                    result["failedComponents"] += 1
                    result["maximumAbsoluteError"] = max(result["maximumAbsoluteError"], abs(left - right))
                    if len(result["mismatchSamples"]) < MAX_MISMATCH_SAMPLES:
                        result["mismatchSamples"].append({"byteOffset": offset,
                            "baselineValue": left, "candidateValue": right})
            result["mismatchSamplesTruncated"] = result["failedComponents"] > MAX_MISMATCH_SAMPLES
        return result
    formats = {"float": {2: "e", 4: "f"}, "depth": {4: "f"},
               "uint": {1: "B", 2: "H", 4: "I"},
               "sint": {1: "b", 2: "h", 4: "i"},
               "unorm": {1: "B", 2: "H"},
               "unormsrgb": {1: "B", 2: "H"},
               "snorm": {1: "b", 2: "h"}}
    fmt = formats.get(kind, {}).get(width)
    format_type = _enum(descriptor["formatType"])
    regular = format_type == "regular" and count in (1, 2, 3, 4)
    packed_float = format_type == "r11g11b10" and kind == "float" and count == 3
    packed_normalized = format_type == "r10g10b10a2" and kind in ("unorm", "unormsrgb") and count == 4
    packed_integer = format_type == "r10g10b10a2" and kind == "uint" and count == 4
    packed_format = packed_float or packed_normalized or packed_integer
    if (not regular or not fmt) and not packed_format:
        if exact:
            # Exact storage equality is still useful for packed formats, but
            # do not pretend those bytes were decoded into numeric components.
            result["comparedComponents"] = len(before)
            result["comparisonKind"] = "byte-exact"
            return result
        raise ValueError("changed packed/compressed output has no numeric comparator")
    pixel_bytes = 4 if packed_format else count * width
    if len(before) != descriptor["width"] * descriptor["height"] * pixel_bytes:
        raise ValueError("output format or row layout is unsupported")
    result["comparisonKind"] = "numeric-packed-components" if packed_format else "numeric-components"
    normalized = kind in ("unorm", "unormsrgb", "snorm")
    scale = (1023.0 if packed_normalized else
             float((1 << (width * 8 - (1 if kind == "snorm" else 0))) - 1) if normalized else 1.0)
    depth = descriptor["kind"] == "depth" or kind == "depth"
    absolute = TOLERANCES["depthAbsolute"] if depth else TOLERANCES["floatAbsolute"]
    if not depth and kind == "float" and width == 2:
        # One binary16 storage quantum near unit magnitude. Cancellation and
        # different legal round-trip instruction order can otherwise reject
        # visually negligible half-target changes under the float32 floor.
        absolute = TOLERANCES["halfFloatAbsolute"]
    relative = 0.0 if depth else TOLERANCES["floatRelative"]
    if normalized and not depth:
        # Color quantization must not relax the absolute depth limit.
        absolute = 1.0 / scale
        relative = 0.0
    if kind in ("uint", "sint"):
        absolute = relative = 0.0
    if exact and kind not in ("float", "depth"):
        result["comparedComponents"] = descriptor["width"] * descriptor["height"] * count
        return result
    left_values = (_packed_components(before, format_type) if packed_format else
                   (item[0] for item in struct.iter_unpack("<" + fmt, before)))
    right_values = (_packed_components(after, format_type) if packed_format else
                    (item[0] for item in struct.iter_unpack("<" + fmt, after)))
    for component_index, (left, right) in enumerate(zip(left_values, right_values)):
        if packed_normalized:
            # RGB has ten bits and alpha has two. Apply the existing normalized
            # one-storage-step rule independently, never a shared RGB scale.
            scale = 3.0 if component_index % 4 == 3 else 1023.0
            if not depth:
                absolute = 1.0 / scale
        a, b = left / scale, right / scale
        if kind == "snorm":
            a, b = max(-1.0, a), max(-1.0, b)
        result["comparedComponents"] += 1
        baseline_finite = math.isfinite(a)
        finite = baseline_finite and math.isfinite(b)
        if not baseline_finite:
            result["baselineNonFiniteComponents"] += 1
        error = abs(a - b) if finite else None
        allowed = absolute + relative * max(abs(a), abs(b)) if finite else None
        if finite:
            result["maximumAbsoluteError"] = max(result["maximumAbsoluteError"], error)
        if not finite or error > allowed + 1e-15:
            result["failedComponents"] += 1
            pixel_index = component_index // count
            x, y = pixel_index % descriptor["width"], pixel_index // descriptor["width"]
            worst = result["worstMismatch"]
            new_worst = worst is None or (error is not None and worst["absoluteError"] is not None
                                          and error > worst["absoluteError"])
            keep_sample = len(result["mismatchSamples"]) < MAX_MISMATCH_SAMPLES
            if keep_sample or new_worst:
                sample = {"x": x, "y": y, "component": component_index % count,
                          "componentIndex": component_index,
                          "baselineValue": a if math.isfinite(a) else str(a),
                          "candidateValue": b if math.isfinite(b) else str(b),
                          "absoluteError": error, "allowedAbsoluteError": allowed,
                          "reason": "tolerance-exceeded" if finite else "non-finite"}
                if keep_sample:
                    result["mismatchSamples"].append(sample)
                if new_worst:
                    result["worstMismatch"] = sample
            if not keep_sample:
                result["mismatchSamplesTruncated"] = True
            bounds = result["mismatchBounds"]
            if bounds is None:
                result["mismatchBounds"] = {"minX": x, "minY": y, "maxX": x, "maxY": y}
            else:
                bounds.update(minX=min(bounds["minX"], x), minY=min(bounds["minY"], y),
                              maxX=max(bounds["maxX"], x), maxY=max(bounds["maxY"], y))
    result["accepted"] = result["failedComponents"] == 0
    return result


def _swapchain_texture(bridge, texture):
    flag = getattr(getattr(bridge.rd, 'TextureCategory', None), 'SwapBuffer', 0)
    return bool(int(getattr(texture, 'creationFlags', 0)) & int(flag))


def _outputs(bridge, ctrl, require_color=True, allow_empty=False, color_index=None,
             targets_override=None, present=False):
    rd = bridge.rd
    pipeline = ctrl.GetPipelineState()
    textures = {bridge._resource_id_int(t.resourceId): t for t in ctrl.GetTextures()}
    outputs, total, materialized_total = [], 0, 0
    targets = [("color", i, t) for i, t in enumerate(pipeline.GetOutputTargets() or [])]
    targets.append(("depth", 0, pipeline.GetDepthTarget()))
    if targets_override is not None:targets=targets_override
    if color_index is not None:
        targets=[row for row in targets if row[0]=='color' and row[1]==color_index]
    for kind, index, target in targets:
        rid = bridge._resource_id_int(getattr(target, "resource", None))
        if not rid:
            continue
        texture = textures.get(rid)
        if texture is None or int(texture.msSamp) != 1 or int(texture.depth) != 1:
            raise ValueError("numeric observation requires single-sample 2D outputs")
        size = int(texture.byteSize)
        if size <= 0 or size > OUTPUT_LIMIT or total + size > TOTAL_LIMIT:
            raise ValueError("output exceeds the bounded numeric observation budget")
        total += size
        mip, array_slice = int(target.firstMip), int(target.firstSlice)
        array_size = int(getattr(texture, "arraysize", 1))
        num_slices = getattr(target, "numSlices", None)
        if (
            mip < 0 or mip >= int(getattr(texture, "mips", 1))
            or array_slice < 0 or array_slice >= array_size
            or (num_slices is None and array_size != 1)
            or (num_slices is not None and int(num_slices) != 1)
        ):
            raise ValueError("layered output observation is unsupported")
        # D3D resources can be typeless while the bound RTV/DSV supplies the
        # actual component interpretation. Readback contains that view's raw
        # storage; no shader or image conversion is requested here.
        view_format = getattr(target, "format", None)
        fmt = (view_format if view_format is not None
               and _enum(view_format.type) != "undefined"
               and _enum(view_format.compType) != "typeless"
               else texture.format)
        descriptor = {"kind": kind, "index": index, "resourceId": rid,
                      "mip": mip, "slice": array_slice,
                      "width": max(1, int(texture.width) >> mip),
                      "height": max(1, int(texture.height) >> mip),
                      "format": bridge._fmt_str(fmt), "formatType": _enum(fmt.type),
                      "componentType": _enum(fmt.compType),
                      "componentByteWidth": int(fmt.compByteWidth),
                      "componentCount": int(fmt.compCount)}
        if present or (color_index is not None and _swapchain_texture(bridge, texture)):
            descriptor['presentationTarget'] = True
        if callable(getattr(fmt,"BGRAOrder",None)) and fmt.BGRAOrder():descriptor['bgraOrder']=True
        raw = bytes(ctrl.GetTextureData(target.resource, rd.Subresource(mip, array_slice, 0)))
        materialized_total += len(raw)
        if not raw or len(raw) > OUTPUT_LIMIT or materialized_total > TOTAL_LIMIT:
            raise ValueError("materialized output exceeds observation budget")
        outputs.append((descriptor, raw))
    if (not outputs and not allow_empty) or (require_color and not any(d["kind"] == "color" for d, unused in outputs)):
        raise ValueError("numeric observation requires at least one color output")
    return outputs


def _storage_module():
    from . import shader_storage_validation
    return shader_storage_validation


def _storage_proof(bridge, reflection):
    return _storage_module().readonly_storage_proof(bridge, reflection)


def _vertex_module():
    from . import shader_vertex_validation
    return shader_vertex_validation


def _observed_outputs(bridge, ctrl, storage_stage=None, observation_kind="fragment", event_id=0):
    # Dispatches must never compare an old graphics pipeline's render targets.
    outputs = [] if observation_kind == "compute" else _outputs(
        bridge, ctrl, require_color=(observation_kind == "fragment"), allow_empty=(observation_kind == "vertex"))
    if observation_kind == "vertex":
        outputs += _vertex_module().outputs(bridge, ctrl, event_id,
            OUTPUT_LIMIT, TOTAL_LIMIT - sum(len(raw) for unused, raw in outputs))
    if storage_stage is not None:
        for stage, reflected_resources in storage_stage:
            observed = _storage_module().storage_outputs(bridge, ctrl, stage,
                OUTPUT_LIMIT, TOTAL_LIMIT - sum(len(raw) for unused, raw in outputs), reflected_resources)
            for desc, raw in observed:
                # Several stages may write the same resource. Compare it once.
                if not any(existing == desc for existing, unused in outputs):
                    outputs.append((desc, raw))
    if not outputs:
        raise ValueError("selected action has no observable output resources")
    return outputs


def _storage_stages(bridge, ctrl, stage_name, stage, selected_reflection):
    reflections = [(stage, selected_reflection)]
    if stage_name not in ("fragment", "compute"):
        reader = getattr(ctrl.GetPipelineState(), "GetShaderReflection", None)
        if not callable(reader):
            raise ValueError("raster-stage observation requires downstream Shader reflection")
        for name, candidate_stage in bridge._available_shader_stages(
                ["vertex", "hull", "domain", "geometry", "task", "mesh", "fragment"]):
            if name == stage_name:
                continue
            reflection = reader(candidate_stage)
            if reflection is not None:
                reflections.append((candidate_stage, reflection))
    proofs, writable = [], []
    for candidate_stage, reflection in reflections:
        proof = _storage_proof(bridge, reflection)
        if proof.get("uncovered"):
            raise ValueError(proof["uncovered"])
        proofs.append(proof)
        if not proof["complete"]:
            writable.append((candidate_stage, list(reflection.readWriteResources)))
    return proofs, writable


def _baseline_repeat_permission(bridge, started):
    """The optional diagnostic must not extend a slow or cancelled operation."""
    if time.monotonic() - started >= BASELINE_REPEAT_START_LIMIT_SECONDS:
        return "diagnostic-budget-exhausted"
    tracker_reader = getattr(bridge, "_request_event_tracker", None)
    tracker = tracker_reader() if callable(tracker_reader) else None
    dispatch_reader = getattr(bridge, "_request_dispatch_context", None)
    dispatch = dispatch_reader() if callable(dispatch_reader) else None
    if not isinstance(dispatch, dict):
        dispatch = tracker.get("dispatchContext") if isinstance(tracker, dict) else None
    if isinstance(dispatch, dict):
        for name in ("cancelEvent", "stopEvent", "captureInvalidatedEvent"):
            event = dispatch.get(name)
            if event is not None and event.is_set():
                return "request-cancelled-or-preempted"
    pending = getattr(bridge, "_foreground_replay_pending", None)
    generation = getattr(bridge, "_foreground_replay_generation", None)
    try:
        if callable(pending) and pending():
            return "foreground-replay-pending"
        expected = tracker.get("foregroundReplayGeneration") if isinstance(tracker, dict) else None
        current = generation() if callable(generation) else None
        if expected is not None and current is not None and expected != current:
            return "foreground-replay-generation-changed"
    except Exception:
        # An optional diagnostic must not assume an unreadable queue is idle.
        return "foreground-replay-state-unavailable"
    return ""


def _observe_event(bridge, ctrl, event_id):
    """Materialize analysis outputs without refreshing viewers between A/B legs."""
    replay = getattr(ctrl, "ReplayFrameEventForAnalysis", None)
    if callable(replay):
        # This native path bypasses replay checkpoints and does one Full replay
        # when there are no event overrides. SetFrameEvent also refreshes every
        # ReplayOutput and fetches shader feedback between replay phases.
        # Those presentation/analysis passes are unnecessary for raw readback.
        replay(event_id)
    else:
        bridge._set_frame_event(ctrl, event_id, True)


def _switch_target(bridge, ctrl, api, original, current, target):
    # Checked replacement methods also replay and refresh viewer outputs.
    # In particular, removing an already absent replacement must be a no-op
    # here, not another full replay of the same original Shader.
    if bridge._resource_id_int(current) == bridge._resource_id_int(target):
        return
    if target is None:
        bridge._remove_shared_shader_resource(ctrl, api, original)
    else:
        bridge._replace_shared_shader_resource(ctrl, api, original, target)


def _repeat_baseline(bridge, ctrl, api, original, target, event_id, baseline, metrics,
                     current_target, storage_stage=None, observation_kind="fragment"):
    _switch_target(bridge, ctrl, api, original, current_target, target)
    ctrl.ClearReplayCache()
    _observe_event(bridge, ctrl, event_id)
    repeated = _observed_outputs(bridge, ctrl, storage_stage, observation_kind, event_id)
    return _baseline_stability(baseline, repeated, metrics)


def _baseline_stability(baseline, repeated, metrics):
    if [d for d, unused in repeated] != [d for d, unused in baseline]:
        raise ValueError("baseline repeat output attachment identity changed")
    repeated_metrics = []
    for output_index, ((desc, initial), (unused, again)) in enumerate(zip(baseline, repeated)):
        if initial == again:
            nonfinite = metrics[output_index]["baselineNonFiniteComponents"]
            item = {"byteExact": True, "accepted": nonfinite == 0,
                    "baselineNonFiniteComponents": nonfinite,
                    "baselineSha256": hashlib.sha256(initial).hexdigest(),
                    "candidateSha256": hashlib.sha256(again).hexdigest()}
        else:
            item = compare_bytes(initial, again, desc)
        item["output"] = desc
        repeated_metrics.append(item)
    return {"checked": True,
            "byteExact": all(m["byteExact"] for m in repeated_metrics),
            "withinTolerance": all(m["accepted"] for m in repeated_metrics),
            "outputs": repeated_metrics}


def _paired_observations(bridge, ctrl, event_id, original, baseline, candidate):
    """Read bounded A/B/A snapshots from one native replay prefix, when admitted."""
    compare = getattr(ctrl, "ComparePixelShaderPair", None)
    if not callable(compare):
        return None, ""
    pair = compare(event_id, original, baseline if baseline is not None else original,
                   candidate if candidate is not None else original)
    if int(pair.eventId) != event_id:
        raise RuntimeError("paired Shader observation returned a different event")
    if not pair.supported:
        return None, str(pair.reason)[:1000]
    if not pair.completed:
        raise RuntimeError("paired Shader observation failed: %s" % str(pair.reason)[:1000])
    snapshots = pair.outputs
    if not snapshots or len(snapshots) > 9:
        raise ValueError("paired Shader observation returned invalid attachments")
    observations, totals, seen = [[], [], []], [0, 0, 0], set()
    for snapshot in snapshots:
        kind, index = "depth" if snapshot.depth else "color", int(snapshot.index)
        key = (kind, index)
        rid = bridge._resource_id_int(snapshot.resourceId)
        width, height = int(snapshot.width), int(snapshot.height)
        sub = snapshot.subresource
        if (key in seen or index < 0 or index > 7 or not rid
                or width <= 0 or height <= 0
                or int(sub.mip) != 0 or int(sub.slice) != 0 or int(sub.sample) != 0):
            raise ValueError("paired Shader observation returned invalid attachment identity")
        seen.add(key)
        fmt = snapshot.format
        desc = {"kind": kind, "index": index, "resourceId": rid,
                "mip": 0, "slice": 0, "width": width, "height": height,
                "format": bridge._fmt_str(fmt), "formatType": _enum(fmt.type),
                "componentType": _enum(fmt.compType),
                "componentByteWidth": int(fmt.compByteWidth),
                "componentCount": int(fmt.compCount)}
        if callable(getattr(fmt, "BGRAOrder", None)) and fmt.BGRAOrder():
            desc["bgraOrder"] = True
        for leg, field in enumerate(("baseline", "candidate", "baselineRepeat")):
            value = getattr(snapshot, field)
            if not value or len(value) > OUTPUT_LIMIT or totals[leg] + len(value) > TOTAL_LIMIT:
                raise ValueError("paired output exceeds the bounded numeric observation budget")
            totals[leg] += len(value)
            observations[leg].append((desc, bytes(value)))
    if not any(desc["kind"] == "color" for desc, unused in observations[0]):
        raise ValueError("paired numeric observation requires at least one color output")
    return observations, ""


def _effect_roi(observation, roi, flip_y=False):
    """Crop stored bytes, never display-normalized pixels, for a stated region."""
    result=[]
    for desc,raw in observation:
        width,height=desc['width'],desc['height']
        pixel_size=len(raw)//(width*height)
        if pixel_size*width*height!=len(raw):raise ValueError('Effect ROI requires a tightly packed color output')
        left,top,right,bottom=roi
        if flip_y:top,bottom=1-bottom,1-top
        x0,y0=int(left*width),int(top*height)
        x1,y1=min(width,max(x0+1,int(math.ceil(right*width)))),min(height,max(y0+1,int(math.ceil(bottom*height))))
        cropped=b''.join(raw[(y*width+x0)*pixel_size:(y*width+x1)*pixel_size] for y in range(y0,y1))
        info=dict(desc,width=x1-x0,height=y1-y0,roiPixels=[x0,y0,x1,y1])
        result.append((info,cropped))
    return result


def _captured_present(bridge, action):
    # Drivers also mark synthetic CaptureEnd actions as Present. Those may
    # reference the last backbuffer without recording an actual presentation.
    return (int(action.flags) & int(bridge.rd.ActionFlags.Present)
            and getattr(action, "customName", "") != "End of Capture")


def resolve_effect_presentation(bridge, params):
    """Resolve the actual presentation target without changing the replay event."""
    capture_id = bridge._require_capture_identity(params.get("expected_capture_id"))
    session = params.get("expected_capture_session_id")
    if not session or session != bridge._capture_session_id:
        raise ValueError("Capture session changed before resolving the final output")
    anchor = params["source_event_id"]
    requested = params.get("observation_event_id", 0)

    def resolve(ctrl):
        pending = list(ctrl.GetRootActions())
        candidates = []
        count = 0
        while pending:
            action = pending.pop()
            if count % 256 == 0:
                bridge._background_replay_checkpoint()
            count += 1
            pending.extend(getattr(action, "children", None) or [])
            event_id = int(action.eventId)
            if event_id >= anchor and _captured_present(bridge, action):
                resource_id = bridge._resource_id_int(getattr(action, "copyDestination", None))
                if resource_id:
                    candidates.append(dict(eventId=event_id, resourceId=str(resource_id)))
        candidates.sort(key=lambda candidate: candidate["eventId"])
        target = next((candidate for candidate in candidates if candidate["eventId"] == requested), None)
        if target is None and len(candidates) == 1:
            target = candidates[0]
        return dict(target=target, candidates=candidates[:32], candidateCount=len(candidates))

    result = bridge._replay(resolve)
    if session != bridge._capture_session_id:
        raise ValueError("Capture session changed while resolving the final output")
    return dict(result, captureId=capture_id, captureSessionId=session, sourceEventId=anchor)


def _effect_outputs(bridge,ctrl,event_id,options):
    """Present's output is its actual destination, never an inherited RTV."""
    root_actions=getattr(ctrl,'GetRootActions',None)
    action=(bridge._find_action_by_event(root_actions(),event_id)
            if callable(root_actions) else None)
    if callable(root_actions)and action is None:
        raise ValueError('Effect output event is not an actual captured action')
    if action is not None:
        flags=int(action.flags)
        if flags & int(bridge.rd.ActionFlags.Present):
            if int(options.get('attachmentIndex',0))!=0:raise ValueError('Present has one destination image at index 0')
            resource=getattr(action,'copyDestination',None)
            if not bridge._resource_id_int(resource):raise ValueError('Present has no observed destination texture')
            class Target(object):pass
            target=Target();target.resource=resource
            sub=getattr(action,'copyDestinationSubresource',None)
            target.firstMip=int(getattr(sub,'mip',0));target.firstSlice=int(getattr(sub,'slice',0))
            target.numSlices=1;target.format=None
            outputs = _outputs(bridge, ctrl, targets_override=[('color', 0, target)], present=True)
            if _captured_present(bridge, action):
                outputs[0][0]["capturedPresent"] = True
            return outputs
        if not(flags&int(bridge.rd.ActionFlags.Drawcall)):
            raise ValueError('Choose a real graphics draw or Present as the effect output event; compute/utility inherited RTVs are not output evidence')
    requested=int(options.get('attachmentIndex',0))
    available=[i for i,t in enumerate(ctrl.GetPipelineState().GetOutputTargets())
               if bridge._resource_id_int(t.resource)]
    if requested not in available:
        raise ValueError('Effect output EID %d has color target indices %s; requested attachmentIndex %d'%
                         (event_id,available,requested))
    return _outputs(bridge,ctrl,color_index=requested)


def _effect_preview(bridge,ctrl,options,observation,leg):
    return _snapshot_preview(options,observation,leg,source='full-replay-snapshot')


def _display_transfer(descriptor,requested):
    if requested not in ('auto','identity','linear-to-srgb'):
        raise ValueError('Unknown effect display transfer')
    if _enum(descriptor['componentType'])=='unormsrgb':return 'identity'
    return ('linear-to-srgb' if _enum(descriptor['componentType'])=='float' else 'identity') if requested=='auto' else requested


def _observe_effect_event(bridge,ctrl,event_id):
    roots=getattr(ctrl,'GetRootActions',None)
    action=bridge._find_action_by_event(roots(),event_id) if callable(roots) else None
    if action is not None and int(action.flags)&int(bridge.rd.ActionFlags.Present):
        # Present has an explicit destination, so no cached pipeline binding is
        # needed. Bypass scrub checkpoints and viewer-side redraws for each leg.
        _observe_event(bridge,ctrl,event_id)
    else:
        bridge._set_frame_event(ctrl,event_id,True)


def _snapshot_rgb(descriptor, raw, black=0., white=1., transfer='auto'):
    """Display immutable stored output bytes; never read the currently replayed texture.

    Preserve the same range and display transfer on both legs. The numeric A/B/A
    comparison still covers the full original precision and resolution.
    """
    width, height = descriptor['width'], descriptor['height']
    count, size = descriptor['componentCount'], descriptor['componentByteWidth']
    kind, packed = _enum(descriptor['componentType']), _enum(descriptor['formatType'])
    transfer=_display_transfer(descriptor,transfer)
    formats = {'float': {2:'e',4:'f'}, 'unorm': {1:'B',2:'H'},
               'unormsrgb': {1:'B',2:'H'}, 'snorm': {1:'b',2:'h'},
               'uint': {1:'B',2:'H',4:'I'}, 'sint': {1:'b',2:'h',4:'i'}}
    if not 1 <= count <= 4 or not 0 < width * height <= 16000000 or not white > black:
        raise ValueError('Invalid paired snapshot display dimensions or range')
    if packed == 'regular' and size in formats.get(kind, {}):
        pixel_bytes = size * count
        fmt = '<' + formats[kind][size] * count
        def pixel(offset):
            values = struct.unpack_from(fmt, raw, offset)
            if kind in ('unorm', 'unormsrgb'):
                return tuple(v / float((1 << (size * 8))-1) for v in values)
            if kind == 'snorm':
                return tuple(max(-1.,v / float((1 << (size * 8-1))-1)) for v in values)
            return values
    elif packed == 'r11g11b10' and kind == 'float':
        pixel_bytes = 4
        def pixel(offset):
            word = struct.unpack_from('<I',raw,offset)[0]
            return (_UNSIGNED_FLOAT11[word & 2047], _UNSIGNED_FLOAT11[(word >> 11) & 2047],
                    _UNSIGNED_FLOAT10[word >> 22])
    elif packed == 'r10g10b10a2' and kind in ('unorm','unormsrgb','uint'):
        pixel_bytes = 4
        def pixel(offset):
            word = struct.unpack_from('<I',raw,offset)[0]
            values = (word & 1023,(word >> 10) & 1023,(word >> 20) & 1023,word >> 30)
            return tuple(v / float(3 if i == 3 else 1023) for i,v in enumerate(values)) if kind != 'uint' else values
    else:
        raise ValueError('Paired snapshot PNG conversion does not support '+descriptor['format'])
    if len(raw) != width * height * pixel_bytes:
        raise ValueError('Paired snapshot byte extent does not match its format')
    scale = min(1.,1600. / max(width,height))
    w,h = max(1,int(width*scale)),max(1,int(height*scale))
    rgb = bytearray(w*h*3)
    for y in range(h):
        for x in range(w):
            values = pixel((min(height-1,int(y/scale))*width+min(width-1,int(x/scale)))*pixel_bytes)
            values = values[:3] if len(values)>1 else values*3
            if descriptor.get('bgraOrder'):values=tuple(reversed(values))
            for c in range(3):
                v = values[c] if c < len(values) else 0.
                if not math.isfinite(v):v=0.
                v=max(0.,min(1.,(v-black)/(white-black)))
                if transfer=='linear-to-srgb':v=12.92*v if v<=.0031308 else 1.055*v**(1./2.4)-.055
                rgb[(y*w+x)*3+c] = int(v*255+.5)
    return w,h,bytes(rgb)


def _snapshot_preview(options, observation, leg, source='same-input-paired-snapshot'):
    import os,zlib,binascii
    folder=options.get('artifactDir')
    if not folder:return None
    desc,raw=observation[0]
    transfer=_display_transfer(desc,options.get('displayTransfer','auto'))
    w,h,rgb=_snapshot_rgb(desc,raw,options.get('blackPoint',0.),options.get('whitePoint',1.),transfer)
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',binascii.crc32(kind+data)&0xffffffff)
    scanlines=b''.join(b'\x00'+rgb[y*w*3:(y+1)*w*3]for y in range(h))
    png=(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,2,0,0,0))
         +chunk(b'sRGB',b'\x00')+chunk(b'IDAT',zlib.compress(scanlines))+chunk(b'IEND',b''))
    path=os.path.join(folder,leg+'.png')
    with open(path,'xb')as stream:stream.write(png)
    return dict(path=path,sha256=hashlib.sha256(png).hexdigest(),resourceId=str(desc['resourceId']),
                width=w,height=h,eventId=options['eventId'],source=source,displayTransfer=transfer,
                presentationTarget=desc.get('presentationTarget', False),
                capturedPresent=desc.get('capturedPresent', False))


def compare_candidates(bridge, params):
    """Execute one isolated, restored A/B observation using exact native blobs."""
    started = time.monotonic()
    rd = bridge.rd
    capture_id = bridge._require_capture_identity(params.get("expected_capture_id"))
    session = params.get("expected_capture_session_id")
    if not session or session != bridge._capture_session_id:
        raise ValueError("capture session changed before shader observation")
    stage_name = bridge._normalise_stage_name(params.get("stage"))
    if stage_name not in ("vertex", "hull", "domain", "geometry", "fragment", "compute", "task", "mesh"):
        raise ValueError("selected stage requires a native ray-pipeline observation route; captured data remains exportable")
    events = bridge._shader_override_events(params)
    if len(events) != 1:
        raise ValueError("numeric observation requires one exact event")
    event = events[0]
    effect=params.get('effect_observation')
    observation_event=int(effect['eventId']) if effect else event['eventId']
    if effect:
        import os,re
        if stage_name not in ('vertex','fragment') or observation_event<event['eventId']:
            raise ValueError('Effect experiments need a vertex/fragment Shader and a same-or-later output event')
        roi=effect.get('roi',[0.,0.,1.,1.])
        if (len(roi)!=4 or any(not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=1 for v in roi)
                or roi[0]>=roi[2] or roi[1]>=roi[3]):raise ValueError('Invalid normalized effect ROI')
        folder=effect.get('artifactDir')
        if folder and (not os.path.isabs(folder) or not re.match(r'^effect-[0-9a-f]{32}$',os.path.basename(folder))
                or not os.path.isdir(folder) or os.listdir(folder) or bridge._event_texture_evidence_is_redirected(folder)):
            raise ValueError('Effect previews require a new, ordinary Host-owned artifact directory')
    rid = event["expectedOriginalResourceId"]
    if any(r.get("expectedOriginalResourceId") == rid for r in bridge._event_shader_overrides.values()):
        raise ValueError("event overrides must be resolved before numeric observation")
    existing = bridge._shared_shader_replacements.get(rid)
    original = existing.get("originalResource") if existing else bridge._invoke_on_ui_sync(lambda: bridge._context_resource_ids().get(rid))
    if original is None:
        raise ValueError("captured shader is unavailable")
    bridge._require_shared_shader_ui_owner(original, existing)
    entry = str(params.get("entry_point", ""))
    stages = dict(bridge._available_shader_stages([stage_name]))
    if stage_name not in stages:
        raise ValueError("selected Shader stage is unavailable in this replay runtime")
    stage = stages[stage_name]
    encoding_name = str(params.get("encoding", "")).lower()
    encoding_attr = {"dxil": "DXIL", "dxbc": "DXBC", "spirv": "SPIRV",
                     "opengl-spirv": "OpenGLSPIRV", "glsl": "GLSL"}.get(encoding_name)
    if not encoding_attr or not entry:
        raise ValueError("native candidate encoding and entry point are required")
    encoding = getattr(rd.ShaderEncoding, encoding_attr)
    candidates = []
    for key in ("baseline", "candidate"):
        item = params[key]
        unused, blob, digest = bridge._read_shader_candidate({
            "candidate_path": item["path"], "expected_sha256": item["rawSha256"],
            "encoding": encoding_name})
        candidates.append((blob, digest))
    visible_event = bridge._request_visible_event_id(0)

    def run(ctrl):
        targets, mutated, restored = [], False, False
        peers = []
        geometry_scope = None
        paired_reason = ""
        current_target = existing["targetResource"] if existing else None
        api = bridge._shader_api_token(ctrl)
        try:
            if encoding_name == "glsl" and (api != "opengl" or entry != "main"):
                raise ValueError("GLSL numeric observation requires OpenGL main")
            live = bridge._validate_live_shader_event(ctrl, event, stage, stage_name, entry,
                require_d3d11_state=(api == "d3d11"), owned_shared_replacement=existing)
            storage_proofs, storage_stages = _storage_stages(
                bridge, ctrl, stage_name, stage, live["reflection"])
            related = {}
            if effect and stage_name == 'vertex':
                geometry_scope = bridge._vertex_geometry_scope(ctrl, event['eventId'], observation_event, stage)
                if not geometry_scope['complete']:
                    raise ValueError('Vertex dependency evidence is incomplete')
                supplied = effect.get('relatedShaders') or []
                related = {int(item['resourceId']): item for item in supplied}
                if len(related) != len(supplied) or len(related) > 16:
                    raise ValueError('Related Shader replacements must be unique and bounded')
                expected = set()
                seen = {rid}
                for match in geometry_scope['matches']:
                    peer_id = match['shaderResourceId']
                    bridge._select_event_for_read(ctrl, match['eventId'])
                    pipe = ctrl.GetPipelineState()
                    peer_reflection = pipe.GetShaderReflection(stage)
                    if not peer_id or not peer_reflection:
                        raise ValueError('A related draw has no verifiable vertex Shader')
                    proofs, writable = _storage_stages(bridge, ctrl, stage_name, stage, peer_reflection)
                    if writable or not all(proof['complete'] for proof in proofs):
                        raise ValueError('A related vertex draw uses writable Shader storage')
                    if peer_id in seen:
                        continue
                    seen.add(peer_id)
                    peer_resource = pipe.GetShader(stage)
                    digest = match['shaderRawSha256']
                    peer_entry = match['entryPoint']
                    shared_code = digest == event['expectedOriginalSha256'] and peer_entry == entry
                    request = related.get(peer_id)
                    if not shared_code:
                        expected.add(peer_id)
                        if not request or not any(m['eventId'] == request['eventId'] and
                            m['shaderResourceId'] == peer_id for m in geometry_scope['matches']):
                            raise ValueError('Related draw %d requires its own coordinated vertex edit' % match['eventId'])
                    if any(record.get('expectedOriginalResourceId') == peer_id
                           for record in bridge._event_shader_overrides.values()):
                        raise ValueError('A related draw has a separate event Shader override')
                    owned = bridge._shared_shader_replacements.get(peer_id)
                    if owned:
                        raise ValueError('A related Shader has an active replacement. Resolve its live edit before the trial.')
                    bridge._require_shared_shader_ui_owner(peer_resource, None)
                    peer_event = dict(eventId=match['eventId'], expectedOriginalResourceId=peer_id,
                                      expectedOriginalSha256=digest)
                    checked = bridge._validate_live_shader_event(ctrl, peer_event, stage, stage_name,
                        peer_entry, require_d3d11_state=(api == 'd3d11'), event_already_selected=True)
                    peer = dict(resource=peer_resource, live=checked, entry=peer_entry, originalHash=digest,
                                targets=[], sharedCode=shared_code, request=request, current=None, touched=False)
                    peers.append(peer)
                if set(related) != expected or len(peers) > 16:
                    raise ValueError('Coordinated Shader edits do not match the detected geometry scope')
            # Reserve enough orphan capacity for the whole A/B build before
            # allocating either target. Both releases can fail independently.
            bridge._require_shader_target_build_capacity(sum(
                1 for unused_blob, digest in candidates
                if digest != event.get("expectedOriginalSha256")) + 2 * len(related))
            for blob, digest in candidates:
                # Native DXIL creation may deduplicate an exact original to
                # its captured ResourceId. Observe that baseline directly;
                # never replace a shader with itself or free the original.
                if digest == event.get("expectedOriginalSha256"):
                    targets.append(None)
                    continue
                target, messages = bridge._build_target_shader(
                    ctrl, entry, encoding, blob,
                    rd.ShaderCompileFlags(), stage)
                if not bridge._resource_id_int(target):
                    raise RuntimeError("BuildTargetShader failed: %s" % messages)
                targets.append(target)
                target_entry = bridge._exact_shader_entry(list(ctrl.GetShaderEntryPoints(target)), entry, stage_name)
                if target_entry is None:
                    raise ValueError("candidate entry is ambiguous")
                reflection = ctrl.GetShader(rd.ResourceId(), target, target_entry)
                if not reflection or hashlib.sha256(bridge._shader_raw_bytes(reflection)).hexdigest() != digest:
                    raise ValueError("built candidate identity changed")
                if encoding_name == "glsl" and str(getattr(reflection, "encoding", "")).lower().replace("shaderencoding.", "") != "glsl":
                    raise ValueError("built GLSL candidate encoding changed")
                storage_proofs.append(_storage_proof(bridge, reflection))
                if storage_proofs[-1].get("uncovered"):
                    raise ValueError(storage_proofs[-1]["uncovered"])
                if encoding_name == "dxil":
                    compatible = bridge._dxil_ambiguous_interface_compatible(ctrl, live["pipelineResource"], live["reflection"], reflection,
                        baseline_access=live.get("dxilAccessSignature"), baseline_layout_evidence=live.get("dxilLayoutEvidence"))
                else:
                    compatible = bridge._physical_shader_interfaces_compatible(live["reflection"], reflection)
                if not compatible:
                    raise ValueError("numeric observation cannot bypass a physical ABI mismatch")
            for peer in peers:
                if peer['sharedCode']:
                    if not bridge._physical_shader_interfaces_compatible(live['reflection'], peer['live']['reflection']):
                        raise ValueError('Identical related Shader bytes have a different physical interface')
                    peer['targets'] = targets
                    continue
                request = peer['request']
                peer_encoding_name = request['encoding']
                peer_encoding_attr = {"dxil": "DXIL", "dxbc": "DXBC", "spirv": "SPIRV",
                                      "opengl-spirv": "OpenGLSPIRV", "glsl": "GLSL"}.get(peer_encoding_name)
                if not peer_encoding_attr:
                    raise ValueError('Unsupported related Shader encoding')
                for key in ('baseline', 'candidate'):
                    item = request[key]
                    unused, blob, digest = bridge._read_shader_candidate(dict(
                        candidate_path=item['path'], expected_sha256=item['rawSha256'], encoding=peer_encoding_name))
                    if digest == peer['originalHash']:
                        peer['targets'].append(None)
                        continue
                    target, message = bridge._build_target_shader(ctrl, peer['entry'],
                        getattr(rd.ShaderEncoding, peer_encoding_attr), blob, rd.ShaderCompileFlags(), stage)
                    if not bridge._resource_id_int(target):
                        raise ValueError('Related Shader build failed: %s' % message)
                    peer['targets'].append(target)
                    target_entry = bridge._exact_shader_entry(list(ctrl.GetShaderEntryPoints(target)), peer['entry'], stage_name)
                    reflection = ctrl.GetShader(rd.ResourceId(), target, target_entry) if target_entry else None
                    if not reflection or hashlib.sha256(bridge._shader_raw_bytes(reflection)).hexdigest() != digest:
                        raise ValueError('Related Shader candidate identity changed')
                    proof = _storage_proof(bridge, reflection)
                    if proof.get('uncovered') or not proof['complete']:
                        raise ValueError('Related Shader candidate uses writable or unknown storage')
                    baseline = peer['live']
                    if peer_encoding_name == 'dxil':
                        compatible = bridge._dxil_ambiguous_interface_compatible(
                            ctrl, baseline['pipelineResource'], baseline['reflection'], reflection,
                            baseline_access=baseline.get('dxilAccessSignature'),
                            baseline_layout_evidence=baseline.get('dxilLayoutEvidence'))
                    else:
                        compatible = bridge._physical_shader_interfaces_compatible(baseline['reflection'], reflection)
                    if not compatible:
                        raise ValueError('Related Shader candidate has a physical ABI mismatch')

            def switch_peers(leg):
                for peer in peers:
                    previous, current = peer['current'], peer['targets'][leg]
                    peer['current'] = current
                    peer['touched'] |= previous is not None or current is not None
                    _switch_target(bridge, ctrl, api, peer['resource'], previous, current)
            needs_storage = not all(proof["complete"] for proof in storage_proofs)
            if effect and needs_storage:
                raise ValueError('Isolated effect experiments cannot mutate writable Shader storage')
            # Candidate ABI checks above retain the same storage binding table.
            if needs_storage and not any(s == stage for s, unused in storage_stages):
                storage_stages.append((stage, list(live["reflection"].readWriteResources)))
            storage_stage = storage_stages if needs_storage else None
            paired, paired_reason = None, ""
            # A native paired draw retains the same upstream inputs and restores
            # RT/DS contents before each A/B/A leg. It is useful precisely where
            # replaying the entire frame independently produces a moving baseline.
            # Either side may be a captured original or an earlier compiled draft.
            paired_effect = bool(effect and observation_event == event['eventId'] and not peers)
            if ((not effect or paired_effect) and stage_name == "fragment" and not needs_storage
                    and callable(getattr(ctrl, "ComparePixelShaderPair", None))):
                mutated = True
                _switch_target(bridge, ctrl, api, original, current_target, None)
                current_target = None
                ctrl.ClearReplayCache()
                paired, paired_reason = _paired_observations(
                    bridge, ctrl, event["eventId"], original, targets[0], targets[1])
                if paired is not None and paired_effect:
                    index = int(effect.get('attachmentIndex',0))
                    paired = [[(desc,raw)for desc,raw in leg if desc['kind']=='color' and desc['index']==index]
                              for leg in paired]
                    if any(len(leg)!=1 for leg in paired):
                        raise ValueError('Requested color attachment is absent from the paired snapshots')
                    swapchain_ids = {bridge._resource_id_int(texture.resourceId) for texture in ctrl.GetTextures()
                                     if _swapchain_texture(bridge, texture)}
                    for leg in paired:
                        if leg[0][0]['resourceId'] in swapchain_ids:
                            leg[0][0]['presentationTarget'] = True
            observations = paired[:2] if paired is not None else []
            effect_images=[]
            if paired is not None and effect and effect.get('artifactDir'):
                effect_images=[_snapshot_preview(effect,leg,name)
                               for leg,name in zip(observations,('before','after'))]
            if paired is None:
                for leg, target in enumerate(targets):
                    mutated = True
                    _switch_target(bridge, ctrl, api, original, current_target, target)
                    current_target = target
                    switch_peers(leg)
                    ctrl.ClearReplayCache()
                    if effect:
                        # Later passes require their own published pipeline/RT
                        # binding. Analysis-only replay retains the anchor cache.
                        _observe_effect_event(bridge,ctrl,observation_event)
                    else:
                        _observe_event(bridge, ctrl, observation_event)
                    observed=(_effect_outputs(bridge,ctrl,observation_event,effect) if effect else
                              _observed_outputs(bridge, ctrl, storage_stage, stage_name, event["eventId"]))
                    observations.append(observed)
                    if effect:effect_images.append(_effect_preview(bridge,ctrl,effect,observed,'before' if len(observations)==1 else 'after'))
            if len(observations[0]) != len(observations[1]):
                raise ValueError("output attachment count changed")
            metrics = []
            for (left_desc, left), (right_desc, right) in zip(*observations):
                if left_desc != right_desc:
                    changed = ["%s: %r -> %r" % (key, left_desc.get(key), right_desc.get(key))
                               for key in sorted(set(left_desc) | set(right_desc))
                               if left_desc.get(key) != right_desc.get(key)]
                    raise ValueError("output attachment identity changed (" + "; ".join(changed) + ")")
                item = compare_bytes(left, right, left_desc)
                item["output"] = left_desc
                metrics.append(item)
            if session != bridge._capture_session_id:
                raise RuntimeError("capture session changed during shader observation")
            result = {"captureId": capture_id, "captureSessionId": session,
                      "stage": stage_name, "observationScope": ("dispatch-storage" if stage_name == "compute" else "raster-outputs"),
                      "eventId": event["eventId"], "accepted": all(m["accepted"] for m in metrics),
                      "metrics": {"outputs": metrics, "coverage": "all-stored-output-data"},
                      "tolerances": dict(TOLERANCES),
                      "sampleCount": sum(m["comparedComponents"] for m in metrics),
                      "replayMethod": ("same-input-paired-draw" if paired is not None else
                          "analysis-full" if callable(getattr(
                          ctrl, "ReplayFrameEventForAnalysis", None)) else "frame-event"),
                      "semanticEquivalenceVerified": False}
            result["storageValidation"] = {
                "mode": "full-resource-bytes" if needs_storage else "read-only-or-absent",
                "readOnlyProofs": storage_proofs,
                "outputCount": sum(1 for desc, unused in observations[0]
                                   if desc["kind"].startswith("storage-")),
            }
            if paired_reason:
                result["pairedReplayUnavailable"] = paired_reason
            if effect:
                region_left=_effect_roi(observations[0],effect.get('roi',[0.,0.,1.,1.]),effect.get('flipY',False))
                region_right=_effect_roi(observations[1],effect.get('roi',[0.,0.,1.,1.]),effect.get('flipY',False))
                result['effectObservation']={'eventId':observation_event,'images':effect_images,
                    'roi':effect.get('roi',[0.,0.,1.,1.]),'metrics':[
                        dict(compare_bytes(a,b,desc),output=desc) for (desc,a),(_,b) in zip(region_left,region_right)],
                    'replacementScope':('single-draw-shader' if paired is not None else
                                        'related-shader-resources' if peers else 'shared-shader-resource'),
                    'coordinatedShaderCount':1 + len(peers), 'sourceEventId':event['eventId']}
            stability = None
            effect_stability = None
            if paired is not None:
                stability = _baseline_stability(observations[0], paired[2], metrics)
                if effect:
                    effect_stability=_baseline_stability(region_left,
                        _effect_roi(paired[2],effect.get('roi',[0.,0.,1.,1.]),effect.get('flipY',False)),
                        result['effectObservation']['metrics'])
                result["baselineStability"] = stability
                # A matching candidate cannot hide a nonrepeatable baseline.
                result["accepted"] = result["accepted"] and stability["withinTolerance"]
            requires_stability = bool(effect) or needs_storage or stage_name != "fragment"
            if not result["accepted"] or requires_stability:
                skip_reason = _baseline_repeat_permission(bridge, started) if stability is None else ""
                if stability is None:
                    stability = {"checked": False, "reason": skip_reason}
                if paired is None and not skip_reason:
                    # One failed-only A/B/A observation distinguishes an unstable
                    # baseline from a repeatable source difference. Release B's
                    # bytes before reading A again to retain the existing memory
                    # bound. This never turns the rejected A/B into acceptance.
                    observations[1] = []
                    right = None  # Drop the comparison loop's last B readback too.
                    try:
                        if effect:
                            _switch_target(bridge,ctrl,api,original,current_target,targets[0]);current_target=targets[0]
                            switch_peers(0)
                            ctrl.ClearReplayCache();_observe_effect_event(bridge,ctrl,observation_event)
                            repeated=_effect_outputs(bridge,ctrl,observation_event,effect)
                            stability=_baseline_stability(observations[0],repeated,metrics)
                            effect_stability=_baseline_stability(region_left,
                                _effect_roi(repeated,effect.get('roi',[0.,0.,1.,1.]),effect.get('flipY',False)),
                                result['effectObservation']['metrics'])
                        else:
                            stability = _repeat_baseline(bridge, ctrl, api, original, targets[0],
                                event["eventId"], observations[0], metrics, current_target,
                                storage_stage=storage_stage, observation_kind=stage_name)
                        current_target = targets[0]
                    except Exception as error:
                        # A/B already produced usable failure coordinates. Keep
                        # them even if this optional third read is unavailable.
                        # The outer finally still makes failed restoration fatal.
                        stability = {"checked": False, "reason": "baseline-repeat-failed",
                                     "errorType": type(error).__name__, "error": str(error)[:1000]}
                    if session != bridge._capture_session_id:
                        raise RuntimeError("capture session changed during baseline repeat")
                result["baselineStability"] = stability
                if requires_stability:
                    result["accepted"] = (result["accepted"] and stability.get("checked", False)
                                          and stability.get("withinTolerance", False))
                    if not stability.get("checked"):
                        result.update(diagnosticCode="storage-baseline-repeat-unavailable" if needs_storage else "raster-baseline-repeat-unavailable",
                            reason="Output comparison requires a repeatable full replay baseline.",
                            nextAction="retry-observation-with-available-replay-budget")
                if stability.get("checked") and not stability.get("withinTolerance"):
                    result.update({
                        "diagnosticCode": "baseline-replay-unstable",
                        "candidateDifferenceConclusive": False,
                        "reason": "Original Shader replay also exceeds the fixed comparison tolerance; "
                                  "the observed difference cannot yet be attributed to the candidate.",
                        "nextAction": "stabilize-replay-and-localize-difference",
                    })
        except ValueError as error:
            result = {"captureId": capture_id, "captureSessionId": session,
                      "eventId": event["eventId"], "accepted": False,
                      "reason": str(error), "sampleCount": 0,
                      "metrics": {"outputs": [], "coverage": "unsupported"},
                      "tolerances": dict(TOLERANCES),
                      "semanticEquivalenceVerified": False}
            if paired_reason:
                result["pairedReplayUnavailable"] = paired_reason
        finally:
            try:
                if mutated:
                    failures = []
                    for peer in reversed(peers):
                        if not peer['touched']:
                            continue
                        try:
                            bridge._remove_shared_shader_resource(ctrl, api, peer['resource'])
                        except Exception as error:
                            failures.append(str(error))
                    try:
                        if existing:
                            bridge._replace_shared_shader_resource(ctrl, api, original, existing["targetResource"])
                        else:
                            bridge._remove_shared_shader_resource(ctrl, api, original)
                    except Exception as error:
                        failures.append(str(error))
                    ctrl.ClearReplayCache()
                    if failures:
                        raise RuntimeError('Could not restore every Shader replacement: ' + '; '.join(failures))
                bridge._set_frame_event(ctrl, visible_event, True, allow_background_preemption=False)
                restored = True
            finally:
                released = set()
                peer_targets = [target for peer in peers for target in peer['targets']]
                for target in targets + peer_targets:
                    target_id = bridge._resource_id_int(target)
                    if not target_id or target_id in released:
                        continue
                    released.add(target_id)
                    if restored:
                        try:
                            ctrl.FreeTargetResource(target)
                        except Exception:
                            bridge._register_orphan_shader_target(target)
                    else:
                        # Restoration failed, so a live replacement may still
                        # bind this target; freeing it now could crash replay.
                        # Hand it to the Host-owned orphan sweep, which frees it
                        # once the binding clears instead of leaking it for the
                        # capture's lifetime.
                        bridge._register_orphan_shader_target(target)
        result["replayRestored"] = True
        if geometry_scope is not None:
            result['geometryScope'] = geometry_scope
        if effect and result.get('effectObservation'):
            stability=result.get('baselineStability')or{}
            if effect.get('roi',[0.,0.,1.,1.])!=[0.,0.,1.,1.] and effect_stability is not None:
                stability=effect_stability
                result['effectObservation']['stabilityScope']='requested-region'
                result['effectObservation']['baselineStability']=stability
            result['effectObservation']['conclusive']=bool(stability.get('checked') and stability.get('withinTolerance'))
        result["durationMilliseconds"] = round((time.monotonic() - started) * 1000)
        return result

    return bridge._replay(run)
