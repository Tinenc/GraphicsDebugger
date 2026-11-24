"""Installed bridge smoke against an existing capture; no AI or source writes.

Run with --ui-script. BREAD_MULTISTAGE_SMOKE_OUTPUT is a JSON evidence path.
The current capture/EID is restored; original-vs-original comparison exercises
native readback and A/B/A plumbing, not a claim about another Shader program.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback

from PySide2 import QtCore
import renderdoc as rd


def start():
    context = pyrenderdoc
    if not context.IsCaptureLoaded():
        QtCore.QTimer.singleShot(500, start)
        return

    def worker():
        from renderdoc_mcp_bridge import shader_validation, shader_vertex_validation, resource_export
        import renderdoc_mcp_bridge as bridge
        output = Path(os.environ["BREAD_MULTISTAGE_SMOKE_OUTPUT"])
        output.parent.mkdir(parents=True, exist_ok=True)
        result = {"schema": "bread-multistage-installed-smoke/v1", "version": bridge.BRIDGE_INTEGRATION_VERSION,
            "bridgePath": bridge.__file__, "checks": {}, "diagnostics": [], "comparisonKind": "original-versus-original"}
        original_event = bridge._invoke_on_ui_sync(lambda: int(context.CurEvent()))
        try:
            def vertex(ctrl):
                bridge._set_frame_event(ctrl, 1267, True)
                values = shader_vertex_validation.outputs(bridge, ctrl, 1267, shader_validation.OUTPUT_LIMIT, shader_validation.TOTAL_LIMIT)
                return {"channelCount": len(values), "bytes": sum(len(raw) for _, raw in values),
                    "kinds": sorted(set(desc["componentType"] for desc, _ in values)),
                    "allSelfComparisonsAccepted": all(shader_validation.compare_bytes(raw, raw, desc)["accepted"] for desc, raw in values)}
            result["vertex"] = bridge._replay(vertex)
            result["checks"]["vertexNativeChannels"] = result["vertex"]["allSelfComparisonsAccepted"]

            def candidates(ctrl):
                pending, dispatches = list(ctrl.GetRootActions()), []
                while pending:
                    action = pending.pop()
                    pending.extend(action.children)
                    if action.flags & rd.ActionFlags.Dispatch:
                        dispatches.append(int(action.eventId))
                return sorted(dispatches)
            for event_id in bridge._replay(candidates)[:12]:
                try:
                    def storage(ctrl):
                        bridge._set_frame_event(ctrl, event_id, True)
                        reflection = ctrl.GetPipelineState().GetShaderReflection(rd.ShaderStage.Compute)
                        if reflection is None or not reflection.readWriteResources:
                            raise ValueError("no writable Compute outputs")
                        proof = shader_validation._storage_proof(bridge, reflection)
                        if proof.get("uncovered"):
                            raise ValueError(proof["uncovered"])
                        values = shader_validation._storage_module().storage_outputs(bridge, ctrl, rd.ShaderStage.Compute,
                            shader_validation.OUTPUT_LIMIT, shader_validation.TOTAL_LIMIT, list(reflection.readWriteResources))
                        return {"eventId": event_id, "outputCount": len(values), "bytes": sum(len(raw) for _, raw in values)}
                    result["compute"] = bridge._replay(storage)
                    result["checks"]["computeNativeStorage"] = result["compute"]["outputCount"] > 0
                    break
                except Exception as error:
                    result["diagnostics"].append({"eventId": event_id, "error": str(error)})
            comparison_targets = [("vertex", 1267)] + ([("compute", result["compute"]["eventId"])] if result.get("compute") else [])
            catalog_receipt = bridge._export_shader_catalog({"output_dir": str(output.parent / "catalog"),
                "event_ids": sorted(set(event for _, event in comparison_targets)), "stages": [stage for stage, _ in comparison_targets]})
            (output.parent / "catalog-receipt.json").write_text(json.dumps(catalog_receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            for stage, event_id in comparison_targets:
                try:
                    def program(ctrl):
                        bridge._set_frame_event(ctrl, event_id, True)
                        shader_stage = dict(bridge._available_shader_stages([stage]))[stage]
                        reflection = ctrl.GetPipelineState().GetShaderReflection(shader_stage)
                        return {"resource": bridge._resource_id_int(ctrl.GetPipelineState().GetShader(shader_stage)),
                            "raw": bridge._shader_raw_bytes(reflection), "entry": str(reflection.entryPoint),
                            "encoding": bridge._shader_encoding_token(reflection.encoding)}
                    facts = bridge._replay(program)
                    digest = hashlib.sha256(facts["raw"]).hexdigest()
                    blob_path = output.with_name(stage + ".original.bin")
                    blob_path.write_bytes(facts["raw"])
                    capture_id = catalog_receipt["captureId"]
                    params = {"stage": stage, "event_id": event_id, "expected_capture_id": capture_id,
                        "expected_capture_session_id": bridge._capture_session_id,
                        "expected_original_resource_id": facts["resource"], "expected_original_sha256": digest,
                        "entry_point": facts["entry"], "encoding": facts["encoding"],
                        "baseline": {"path": str(blob_path), "rawSha256": digest},
                        "candidate": {"path": str(blob_path), "rawSha256": digest}}
                    observed = shader_validation.compare_candidates(bridge, params)
                    result[stage + "Comparison"] = observed
                    result["checks"][stage + "ABA"] = observed.get("accepted") is True and observed.get("replayRestored") is True
                except Exception:
                    result["diagnostics"].append({"stage": stage, "error": traceback.format_exc()})
        except Exception:
            result["diagnostics"].append({"error": traceback.format_exc()})
        finally:
            try:
                bridge._replay(lambda ctrl: bridge._set_frame_event(ctrl, original_event, True))
                result["checks"]["eventRestored"] = True
            except Exception:
                result["diagnostics"].append({"restoreError": traceback.format_exc()})
            result["ok"] = all(result["checks"].get(name) for name in
                ("vertexNativeChannels", "computeNativeStorage", "vertexABA", "computeABA", "eventRestored"))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    threading.Thread(target=worker, name="BreadMultistageReadbackSmoke", daemon=True).start()


pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(start)
