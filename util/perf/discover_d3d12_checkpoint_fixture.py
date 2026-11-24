"""Discover real D3D12 fixture EIDs and bindings, then emit checkpoint-profiler arguments.

Running main opens the capture and uses the GPU. Importing its pure evidence helpers does not
load RenderDoc. Discovery never claims shader mutation or checkpoint reuse passed: the separate
profiler must prove native full-output equivalence and actual hit counters.
"""
from __future__ import print_function

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import traceback


ALIAS_TEXTURE_NAMES = ("CheckpointAliasTarget", "CheckpointAliasWitness")


def select_alias_fixture(actions, groups, mode):
    if len(groups) != 4:
        raise ValueError("Alias fixture requires exactly four completed submissions")
    markers = [row for row in actions if row["name"].startswith("Checkpoint alias ")]
    expected = {"Checkpoint alias fixture " + mode}
    for region in (2, 3):
        expected.update(("Checkpoint alias barrier region " + str(region),
                         "Checkpoint alias full-clear region " + str(region)))
        if mode == "read":
            expected.add("Checkpoint alias read-before-clear region " + str(region))
    if len(markers) != len(expected) or {row["name"] for row in markers} != expected:
        raise ValueError("Actual alias action markers do not match the requested fixture mode")

    def marker(rows, name):
        matches = [row for row in rows if row["name"] == name]
        if (len(matches) != 1 or not matches[0].get("marker") or
                not (matches[0].get("exactAPI") or {}).get("chunkName", "").endswith("SetMarker")):
            raise ValueError("Alias fixture marker lacks exact SetMarker action evidence: " + name)
        return matches[0]

    def next_action(rows, mark, kind):
        index = rows.index(mark) + 1
        if index >= len(rows) or not rows[index][kind]:
            raise ValueError("Alias fixture marker is not followed by its expected " + kind)
        return rows[index]

    selected, read_draws = [], []
    for region, group in enumerate(groups):
        rows = sorted([row for row in actions
                       if group["begin"]["eventId"] < row["eventId"] < group["end"]["eventId"]],
                      key=lambda row: row["eventId"])
        count = 2 if mode == "read" and region >= 2 else 1
        if len(group["draws"]) != count:
            raise ValueError("Alias fixture has an unexpected draw count in submission " + str(region))
        main = next_action(rows, marker(rows, "Checkpoint region " + str(region)), "draw")
        if main != group["draws"][-1]:
            raise ValueError("Selected main draw must be the last draw in its submission")
        selected.append(main)
        if region == 0:
            mode_marker = marker(rows, "Checkpoint alias fixture " + mode)
            if mode_marker["eventId"] >= main["eventId"]:
                raise ValueError("Fixture mode marker must precede the first main draw")
        if region >= 2:
            alias = marker(rows, "Checkpoint alias barrier region " + str(region))
            clear_marker = marker(rows, "Checkpoint alias full-clear region " + str(region))
            clear = next_action(rows, clear_marker, "clear")
            if not (clear.get("exactAPI") or {}).get("chunkName", "").endswith("ClearRenderTargetView"):
                raise ValueError("Alias full clear lacks exact ClearRenderTargetView evidence")
            if not alias["eventId"] < clear_marker["eventId"] < clear["eventId"] < main["eventId"]:
                raise ValueError("Alias and complete clear must precede the main draw in order")
            if mode == "read":
                read = next_action(rows, marker(rows, "Checkpoint alias read-before-clear region " + str(region)), "draw")
                if read != group["draws"][0] or not alias["eventId"] < read["eventId"] < clear_marker["eventId"]:
                    raise ValueError("Witness draw must read before the complete clear")
                read_draws.append(read["eventId"])
    return selected, {"mode": mode, "mainDraws": [row["eventId"] for row in selected],
                      "readDraws": read_draws, "extraTextureNames": list(ALIAS_TEXTURE_NAMES)}


def select_fixture_events(actions, allow_single_submit=False, checkpoint_alias_fixture="off"):
    """Select exact draw EIDs inside evidenced, completed single-list submission actions."""
    if checkpoint_alias_fixture not in ("off", "clear", "read", "split"):
        raise ValueError("Unknown checkpoint alias fixture mode")
    if checkpoint_alias_fixture != "off" and allow_single_submit:
        raise ValueError("Alias fixture cannot use the single-submit fallback")
    groups, current, draws, clears = [], None, [], []
    boundary_name = re.compile(r"^=> ExecuteCommandLists\((\d+)\)\[(\d+)\]: (Reset|Close)\(")
    for row in sorted(actions, key=lambda value: value["eventId"]):
        if row["commandBoundary"]:
            match = boundary_name.match(row["name"])
            if not match or match.group(1, 2) != ("1", "0"):
                raise ValueError("Fixture requires evidenced one-list submissions, not guessed boundaries")
            if row["beginPass"]:
                if current is not None or match.group(3) != "Reset":
                    raise ValueError("Overlapping or ambiguous command-buffer boundary")
                current = {"begin": row, "end": None, "draws": []}
            elif row["endPass"]:
                if current is None or match.group(3) != "Close":
                    raise ValueError("Close boundary has no matching Reset")
                current["end"] = row
                groups.append(current)
                current = None
        if row["draw"]:
            if not row.get("exactAPI") or not row["exactAPI"]["chunkName"].endswith("DrawInstanced"):
                raise ValueError("Draw lacks exact DrawInstanced structured evidence")
            if current is None:
                raise ValueError("Draw is outside an evidenced submission")
            current["draws"].append(row)
            draws.append(row)
        if row["clear"] and row.get("exactAPI"):
            clears.append(row)
    if current is not None:
        raise ValueError("Unclosed command buffer in action evidence")
    draw_groups = [group for group in groups if group["draws"]]
    alias_evidence = None
    if checkpoint_alias_fixture != "off":
        selected, alias_evidence = select_alias_fixture(actions, groups, checkpoint_alias_fixture)
        ready = True
    elif len(draw_groups) >= 2:
        if any(len(group["draws"]) != 1 for group in draw_groups) or len(draw_groups) > 8:
            raise ValueError("Expected one bounded visible draw per regression submission")
        selected = [group["draws"][0] for group in draw_groups]
        ready = True
    elif allow_single_submit and len(draws) == 1 and clears:
        preceding = [row for row in clears if row["eventId"] < draws[0]["eventId"]]
        if not preceding:
            raise ValueError("Single-submit triangle has no evidenced preceding clear")
        selected, ready = [preceding[-1], draws[0]], False
    else:
        raise ValueError("At least two completed draw submissions are needed; Simple_Triangle alone is insufficient")
    return {"selectedEvents": [row["eventId"] for row in selected],
            "selectedActions": selected, "submissionGroups": groups,
            "readyForCheckpointMutation": ready,
            "aliasFixtureEvidence": alias_evidence,
            "selectionMeaning": "actual draw EIDs in complete single-list submissions" if ready else
                                "actual clear and draw only; no checkpoint reuse claim"}


def collect_actions(controller, rd):
    structured = controller.GetStructuredFile()
    rows = []

    def walk(actions):
        for action in actions:
            if len(rows) >= 4096:
                raise ValueError("Fixture discovery is bounded to 4096 actions")
            exact = []
            for event in action.events:
                if int(event.eventId) == int(action.eventId):
                    chunk_index = int(event.chunkIndex)
                    if not 0 <= chunk_index < len(structured.chunks):
                        raise ValueError("API event has an invalid structured chunk index")
                    chunk = structured.chunks[chunk_index]
                    exact.append({"eventId": int(event.eventId), "chunkIndex": chunk_index,
                                  "chunkId": int(chunk.metadata.chunkID), "chunkName": str(chunk.name),
                                  "fileOffset": int(event.fileOffset)})
            if len(exact) > 1:
                raise ValueError("Ambiguous exact API event in fixture action")
            rows.append({"eventId": int(action.eventId), "name": str(action.GetName(structured)),
                         "flags": str(action.flags), "commandBoundary": bool(action.flags & rd.ActionFlags.CommandBufferBoundary),
                         "beginPass": bool(action.flags & rd.ActionFlags.BeginPass),
                         "endPass": bool(action.flags & rd.ActionFlags.EndPass),
                         "draw": bool(action.flags & rd.ActionFlags.Drawcall),
                         "clear": bool(action.flags & rd.ActionFlags.Clear),
                         "marker": bool(action.flags & rd.ActionFlags.SetMarker),
                         "outputs": [str(value) for value in action.outputs],
                         "exactAPI": exact[0] if exact else None})
            walk(action.children)
    walk(controller.GetRootActions())
    # Queue Execute has synthetic Reset/Close action delimiters. Keep the actual serialised
    # Queue Execute chunks independently, rather than treating a synthetic EID as an API draw.
    submits = [{"chunkIndex": index, "chunkId": int(chunk.metadata.chunkID), "chunkName": str(chunk.name)}
               for index, chunk in enumerate(structured.chunks)
               if str(chunk.name).endswith("ExecuteCommandLists")]
    return rows, submits


def binding_issues(witnesses, selected_draw_eids):
    selected = [row for row in witnesses if row["eventId"] in selected_draw_eids]
    errors = []
    if len(selected) != len(selected_draw_eids):
        errors.append("missing selected draw binding witness")
    for row in selected:
        if not row["pixelShader"] or len(row["colorTargets"]) != 1 or row["colorTargets"][0]["slot"] != 0:
            errors.append("draw must bind one RT0 and a pixel shader")
        output = row.get("shaderOutputSignature", [])
        if (len(output) != 1 or output[0]["systemValue"] != "ColorOutput" or
                output[0]["regIndex"] != 0 or output[0]["compCount"] != 4 or output[0]["varType"] != "Float"):
            errors.append("pixel shader must reflect one float4 SV_Target0 output")
    if len({row["pixelShader"] for row in selected}) != 1:
        errors.append("selected submissions must share the actual captured pixel shader")
    if len({row["colorTargets"][0]["resource"] for row in selected if len(row["colorTargets"]) == 1}) != 1:
        errors.append("selected submissions must preserve the same actual output resource")
    return errors


def capture_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-single-submit", action="store_true")
    parser.add_argument("--checkpoint-alias-fixture", choices=("off", "clear", "read", "split"), default="off")
    args = parser.parse_args()
    capture_path, output = Path(args.capture).resolve(), Path(args.output).resolve()
    if not capture_path.is_file():
        parser.error("Capture does not exist")
    if output.exists():
        parser.error("Choose a new output manifest; existing evidence is not overwritten")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema": "renderdoc-d3d12-checkpoint-fixture/v1", "ok": False,
              "capture": str(capture_path), "captureBytes": capture_path.stat().st_size,
              "captureSha256": capture_hash(capture_path), "bindings": [], "issues": [],
              "validationMeaning": "discovery only; checkpoint hits, pixels and shader invalidation require profiler execution"}
    controller, capture_file, initialised = None, None, False
    started = time.monotonic()

    def publish():
        report["elapsedMs"] = round((time.monotonic() - started) * 1000, 3)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    try:
        import renderdoc as rd
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        initialised = True
        rd.SetDebugLogFile(str(output.with_suffix(".rdoc.log")))
        capture_file = rd.OpenCaptureFile()
        status = capture_file.OpenFile(str(capture_path), "", None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError("Capture OpenFile failed: " + str(status))
        report["captureDriver"] = capture_file.DriverName()
        if report["captureDriver"] != "D3D12":
            raise ValueError("Actual capture driver is not D3D12")
        publish()
        status, controller = capture_file.OpenCapture(rd.ReplayOptions(), None)
        if status != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(status))
        report["initialCheckpointStatus"] = json.loads(str(controller.GetReplayCheckpointStatus()))
        controller.ConfigureReplayCheckpoints(False, 0, 0)
        actions, submits = collect_actions(controller, rd)
        report.update(actions=actions, executeCommandListsChunks=submits)
        report.update(select_fixture_events(actions, args.allow_single_submit, args.checkpoint_alias_fixture))
        if report["readyForCheckpointMutation"] and len(submits) < len(report["selectedEvents"]):
            raise ValueError("Not enough actual serialised queue submissions for selected groups")
        publish()
        for event in report["selectedEvents"]:
            controller.SetFrameEvent(event, True)
            pipe = controller.GetPipelineState()
            shader = pipe.GetShader(rd.ShaderStage.Pixel)
            reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
            row = {"eventId": event, "pixelShader": str(shader) if shader != rd.ResourceId.Null() else None,
                   "colorTargets": [], "shaderOutputSignature": [], "topology": str(pipe.GetPrimitiveTopology())}
            for slot, target in enumerate(pipe.GetOutputTargets()):
                if target.resource != rd.ResourceId.Null():
                    row["colorTargets"].append({"slot": slot, "resource": str(target.resource),
                                                "format": target.format.Name(), "mip": int(target.firstMip),
                                                "slice": int(target.firstSlice)})
            if reflection:
                for sig in reflection.outputSignature:
                    row["shaderOutputSignature"].append({"systemValue": str(sig.systemValue).rsplit(".", 1)[-1],
                                                        "regIndex": int(sig.regIndex), "compCount": int(sig.compCount),
                                                        "varType": str(sig.varType).rsplit(".", 1)[-1]})
            report["bindings"].append(row)
            publish()
        draws = {row["eventId"] for row in report["selectedActions"] if row["draw"]}
        report["issues"].extend(binding_issues(report["bindings"], draws))
        report["profileArguments"] = ["--capture", str(capture_path), "--events",
                                       ",".join(str(value) for value in report["selectedEvents"]),
                                       "--min-hits", "1" if report["readyForCheckpointMutation"] else "0",
                                       "--max-seconds", "300"]
        if report["readyForCheckpointMutation"]:
            report["profileArguments"].append("--validate-mutations")
        if args.checkpoint_alias_fixture != "off":
            for name in ALIAS_TEXTURE_NAMES:
                report["profileArguments"].extend(("--extra-texture", name))
            if args.checkpoint_alias_fixture == "clear":
                report["profileArguments"].extend(("--min-full-clear-restores", "2"))
        report["ok"] = not report["issues"]
    except Exception as error:
        report["issues"].append(str(error))
        report["traceback"] = traceback.format_exc()
    finally:
        cleanup = [("controller", lambda: controller.Shutdown() if controller else None),
                   ("captureFile", lambda: capture_file.Shutdown() if capture_file else None),
                   ("runtime", lambda: rd.ShutdownReplay() if initialised else None)]
        for name, operation in cleanup:
            try:
                operation()
            except Exception as error:
                report.setdefault("cleanupErrors", []).append({"operation": name, "error": str(error)})
                report["ok"] = False
        publish()
    print(json.dumps({"ok": report["ok"], "output": str(output),
                      "selectedEvents": report.get("selectedEvents"), "issues": report["issues"]}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
