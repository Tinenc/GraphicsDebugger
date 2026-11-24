"""Pre-render volume W slices in one capture-bound replay transaction (Python 3.6)."""
import math

from .frame_flow_channels import request_identity, mutation_identity, _preview


def capture_volume(bridge, parent, requests, snapshot, cancelled):
    identity = request_identity(parent)
    replay, session = snapshot["replay"], snapshot["session"]
    mapping = parent.get("displayMapping") or {}
    flip_y = mapping.get("flipY", False)
    if type(flip_y) is not bool:
        raise ValueError("Volume orientation must be a boolean")
    if not isinstance(requests, list) or not 1 <= len(requests) <= 160:
        raise ValueError("Volume capture requires one to 160 bounded slice requests")
    seen = set()
    for request in requests:
        z, channel = request.get("slice"), request.get("channel")
        if type(z) is not int or z < 0 or channel not in ("rgb", "r", "g", "b", "a"):
            raise ValueError("Volume capture request uses an invalid slice or channel")
        if (z, channel) in seen:
            raise ValueError("Volume capture request contains a duplicate slice channel")
        seen.add((z, channel))
        mode = request.get("rangeMode")
        if mode not in ("native", "explicit"):
            raise ValueError("Volume slices require one fixed display range")
        if mode == "explicit":
            low, high = request.get("blackPoint"), request.get("whitePoint")
            if (type(low) not in (int, float) or type(high) not in (int, float)
                    or not math.isfinite(low) or not math.isfinite(high) or low >= high):
                raise ValueError("Volume display range is invalid")
    ranges = {(request.get("rangeMode"), request.get("blackPoint"), request.get("whitePoint"))
              for request in requests}
    if len(ranges) != 1 or len({z for z, channel in seen}) > 32:
        raise ValueError("Volume capture requires at most 32 W slices on one display scale")

    def checkpoint():
        if (cancelled() or session != bridge._capture_session_id or
                snapshot["flowGeneration"] != bridge._frame_render_flow_generation or
                snapshot["mutationEpoch"] != mutation_identity(bridge) or
                bridge._replay_foreground_generation(replay) != snapshot["foregroundGeneration"]):
            raise RuntimeError("Volume preview request was superseded")

    checkpoint()
    receipts, failures, errors, metadata = [], [], [], {}

    def collect(ctrl):
        selected = False
        try:
            checkpoint()
            texture = next((item for item in ctrl.GetTextures() or []
                            if bridge._resource_id_int(item.resourceId) == identity["resource_id"]), None)
            dimension = int(getattr(texture, "dimension", 0))
            depth = int(getattr(texture, "depth", 0))
            count = int(getattr(getattr(texture, "format", None), "compCount", 0))
            if dimension != 3 or depth <= 0 or count not in (1, 2, 3, 4):
                raise ValueError("Volume capture requires a real three-dimensional texture")
            mip_depth = max(1, depth >> identity["mip"])
            channels = (["rgb"] if count >= 3 else []) + list("rgba"[:count])
            if any(z >= mip_depth or channel not in channels for z, channel in seen):
                raise ValueError("Volume slice or component is outside the actual mip")
            metadata.update(componentCount=count, mipDepth=mip_depth)
            selected = True
            bridge._set_frame_event(ctrl, identity["event_id"], True)
            for request in requests:
                checkpoint()
                channel, z = request["channel"], request["slice"]
                slice_identity = dict(identity, slice=z)
                params = dict(slice_identity)
                params.update(channel="rgba" if channel == "rgb" else channel,
                              range_mode=request["rangeMode"], black_point=request.get("blackPoint"),
                              white_point=request.get("whitePoint"), flip_y=flip_y, max_dimension=1024,
                              request_id=request.get("request_id"), artifact_dir=request.get("artifact_dir"),
                              expected_capture_session_id=session)
                try:
                    receipt = bridge._capture_event_texture_evidence(
                        params, _controller=ctrl, _original_event=snapshot["originalEvent"],
                        _expected_resource_id=identity["resource_id"])
                    if receipt.get("captureSessionId") != session:
                        raise ValueError("Volume preview receipt capture session changed")
                    # Verify byte hash, PNG format and every subresource identity
                    # before returning. Host independently verifies and admits it.
                    _preview(receipt, slice_identity, parent, {}, channel)
                    attachment = receipt["artifact"]["attachment"]
                    if (attachment.get("textureDimension") != 3 or attachment.get("mipDepth") != mip_depth
                            or receipt["artifact"]["display"].get("flipY", False) != flip_y):
                        raise ValueError("Volume preview receipt changed dimension or orientation")
                    metadata.update(viewSliceFirst=attachment["viewSliceFirst"],
                                    viewSliceCount=attachment["viewSliceCount"])
                    receipts.append({"slice": z, "channel": channel, "rangeMode": request["rangeMode"],
                                     "blackPoint": request.get("blackPoint"), "whitePoint": request.get("whitePoint"),
                                     "receipt": receipt})
                except Exception as error:
                    checkpoint()
                    failures.append({"slice": z, "channel": channel, "error": str(error)[:1200]})
                    if "bread-replay-unavailable" in str(error):
                        break
            checkpoint()
        except Exception as error:
            errors.append(error)
        finally:
            if selected:
                try:
                    bridge._set_frame_event(ctrl, snapshot["originalEvent"], True)
                except Exception as error:
                    errors.append(error)

    tagged = getattr(replay, "BlockInvokeTagged", None)
    if tagged is not None:
        tagged("ReplayLab.background.frame-flow-volume", collect)
    else:
        replay.BlockInvoke(collect)
    if errors:
        raise errors[-1]
    checkpoint()
    if not metadata:
        raise RuntimeError("Volume preview replay request did not complete")
    for item in receipts:
        item["receipt"]["restoration"]["restored"] = True
    return dict(metadata, schema="bread-frame-flow-volume-capture/v1", captureSessionId=session,
                originalEventId=snapshot["originalEvent"], replayRevision=snapshot["mutationEpoch"],
                receipts=receipts, errors=failures, complete=not failures and len(receipts) == len(requests))
