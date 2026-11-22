"""CPU-only acceptance of existing alias discovery/checkpoint reports; never opens a capture."""
import argparse
import json
from pathlib import Path
import re
import sys

from discover_d3d12_checkpoint_fixture import ALIAS_TEXTURE_NAMES, select_fixture_events


def validate_alias_report(discovery, profile, mode):
    issues = []
    if mode not in ("clear", "read", "split"):
        raise ValueError("An explicit clear/read/split fixture mode is required")
    if discovery.get("schema") != "renderdoc-d3d12-checkpoint-fixture/v1" or not discovery.get("ok"):
        raise ValueError("Completed successful fixture discovery is required")
    selected = select_fixture_events(discovery["actions"], checkpoint_alias_fixture=mode)
    events = selected["selectedEvents"]
    if discovery.get("selectedEvents") != events:
        issues.append("Discovery main EIDs differ from actual action-marker evidence")
    if profile.get("schema") != "renderdoc-checkpoint-profile/v1" or not profile.get("measurementComplete"):
        raise ValueError("Completed checkpoint profile is required")
    if not profile.get("ok") or profile.get("issues") or profile.get("cleanupErrors"):
        issues.append("Checkpoint profile did not pass all replay and cleanup checks")
    if (Path(profile.get("capture", "")).resolve() != Path(discovery.get("capture", "")).resolve()
            or profile.get("captureBytes") != discovery.get("captureBytes")):
        issues.append("Discovery and profile identify different capture files")
    if profile.get("events") != events:
        issues.append("Profile does not cover exactly the four discovered main draws")
    requested = profile.get("configuration", {}).get("extra_texture", [])
    if not set(ALIAS_TEXTURE_NAMES).issubset(requested):
        issues.append("Both target and witness must be explicitly requested as extra textures")
    identities = {}
    for name in ALIAS_TEXTURE_NAMES:
        matches = [row for row in profile.get("extraTextureResources", []) if row.get("name") == name]
        if len(matches) != 1 or not matches[0].get("resource"):
            raise ValueError("Missing or ambiguous exact texture identity: " + name)
        identities[name] = matches[0]["resource"]
    if len(set(identities.values())) != 2:
        raise ValueError("Target and witness must be distinct actual resources")

    def pixels(visit):
        witness = visit.get("witness", {})
        if witness.get("eventId") != visit.get("eventId"):
            raise ValueError("Pixel witness belongs to a different EID")
        result = {}
        for name, resource in identities.items():
            declared = [row for row in witness.get("extraTextures", []) if row.get("name") == name]
            if (len(declared) != 1 or declared[0].get("resource") != resource or
                    any(declared[0].get(field) != 1 for field in ("mips", "slices", "samples")) or
                    declared[0].get("format") != "R8G8B8A8_UNORM"):
                raise ValueError("Missing full, unchanged RGBA8 texture descriptor: " + name)
            raw = [row for row in witness.get("pixels", []) if row.get("resource") == resource]
            if (len(raw) != 1 or any(raw[0].get(field) != 0 for field in ("mip", "slice", "sample")) or
                    raw[0].get("bytes") != 256 * 128 * 4 or
                    not re.fullmatch(r"[0-9a-f]{64}", raw[0].get("sha256", ""))):
                raise ValueError("Missing complete 256x128 native RGBA8 pixel hash: " + name)
            result[name] = (raw[0]["bytes"], raw[0]["sha256"])
        return result

    visits = profile.get("visits", [])
    if not visits:
        raise ValueError("No ordinary or forced pixel witnesses")
    covered = []
    for visit in visits:
        try:
            covered.append((visit, pixels(visit)))
        except ValueError as error:
            issues.append("{} EID {}: {}".format(visit.get("phase"), visit.get("eventId"), error))
    canonical = {}
    for event in events:
        references = [value for visit, value in covered
                      if visit.get("eventId") == event and visit.get("phase") == "canonical"
                      and visit.get("force") is True]
        if len(references) != 2 or references[0] != references[1]:
            issues.append("Missing or unstable double forced T/W reference at EID " + str(event))
            continue
        canonical[event] = references[0]
        ordinary = [value for visit, value in covered if visit.get("eventId") == event
                    and visit.get("force") is False and not visit.get("phase", "").startswith("mutation.")]
        if not ordinary or any(value != references[0] for value in ordinary):
            issues.append("Ordinary T/W pixels differ from forced references at EID " + str(event))
    if len(canonical) == 4:
        # A constant/black target cannot demonstrate that restoring its old contents matters.
        if len({canonical[event][ALIAS_TEXTURE_NAMES[0]] for event in events}) != 4:
            issues.append("The four target references do not demonstrate distinct rendered contents")
        if mode == "read":
            for source, destination in ((events[1], events[2]), (events[2], events[3])):
                if canonical[destination][ALIAS_TEXTURE_NAMES[1]] != canonical[source][ALIAS_TEXTURE_NAMES[0]]:
                    issues.append("Read-before-clear witness at EID {} does not preserve target EID {}".format(destination, source))

    restore_rows = []
    for sample in profile.get("samples", []):
        for record in sample.get("timingRecords", []):
            if record.get("api") == "D3D12Checkpoint" and record.get("op") == "restore":
                if sample.get("status") != "complete":
                    issues.append("A native checkpoint restore has no completed sample")
                restore_rows.append((sample, record))
    # Only actual restore records qualify; D3D12ClearProof approved/candidates are not copy savings.
    omissions = [(sample, row) for sample, row in restore_rows
                 if row.get("fullClearInitialCopiesDeferred", 0) > 0]
    first_alias = next(row["eventId"] for row in discovery["actions"]
                       if row["name"] == "Checkpoint alias barrier region 2")

    def matching_proofs(sample, restore):
        if not events[0] < restore.get("event", 0) < first_alias:
            return []
        return [row for row in sample.get("timingRecords", [])
                if row.get("api") == "D3D12ClearProof" and row.get("checkpoint") == restore["event"]
                and row.get("target") == restore.get("target") and row.get("eligibleResources", 0) > 0]

    ordinary_samples = {(visit.get("phase", "") + ".replay", visit.get("eventId")) for visit in visits
                        if visit.get("force") is False and not visit.get("phase", "").startswith("mutation.")}
    ordinary_omissions = [(sample, row) for sample, row in omissions
                          if (sample.get("phase"), sample.get("eventId")) in ordinary_samples
                          and sample.get("eventId") in events[2:]
                          and row.get("target") in (sample["eventId"], sample["eventId"] - 1)
                          and row.get("fullClearAllocationBytes", 0) >= 256 * 128 * 4
                          and any(proof.get("aliasBarriersPassed", 0) > 0 and proof.get("approved", 0) > 0
                                  for proof in matching_proofs(sample, row))]
    if mode == "clear" and len(ordinary_omissions) < 2:
        issues.append("Positive alias case requires at least two actual ordinary full-clear omissions")
    if mode in ("read", "split") and omissions:
        issues.append("Negative alias case incorrectly omitted resource contents before full clear")
    proofs = [proof for sample, restore in restore_rows for proof in matching_proofs(sample, restore)]
    if mode == "read" and not any(proof.get("aliasBarriersPassed", 0) > 0
                                   and proof.get("blockedCandidates", 0) > 0 for proof in proofs):
        issues.append("Read negative case lacks native alias passage and read-before-clear rejection")
    if mode == "split" and not any(proof.get("barrierStops", 0) > 0
                                    and proof.get("stopReason") == "splitOrUnknownBarrier" for proof in proofs):
        issues.append("Split negative case lacks the native mixed-barrier rejection")
    if not restore_rows or profile.get("observedHits", 0) < 1:
        issues.append("Actual checkpoint restore coverage is required in every alias variant")
    return {"schema": "renderdoc-d3d12-checkpoint-alias-acceptance/v1", "ok": not issues,
            "mode": mode, "events": events, "coveredVisits": len(covered),
            "nativeRestoreRecords": len(restore_rows), "fullClearRestoreRecords": len(omissions),
            "ordinaryFullClearRestoreRecords": len(ordinary_omissions), "issues": issues,
            "coverage": "Full native T/W hashes and real restore records; unused alias-buffer bytes are not validated"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--mode", choices=("clear", "read", "split"), required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        parser.error("Choose a new output path; existing evidence is not overwritten")
    try:
        report = validate_alias_report(json.loads(Path(args.discovery).read_text(encoding="utf-8")),
                                       json.loads(Path(args.profile).read_text(encoding="utf-8")), args.mode)
    except (KeyError, TypeError, ValueError) as error:
        report = {"ok": False, "mode": args.mode, "issues": [str(error)]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
