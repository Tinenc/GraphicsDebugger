"""Bounded algorithm explanations tied to observed frame events and resources.

Bindings establish where a resource is available, not its mathematical meaning.
The latter remains an evidence-backed Agent explanation, never a Host proof.
"""
from __future__ import annotations

from collections.abc import Mapping
from bisect import bisect_left, bisect_right
from copy import deepcopy


def validate_algorithm(value, members, actions):
    if value is None:
        return None
    fields = {"inputs", "steps", "outputs", "integration"}
    if not isinstance(value, Mapping) or set(value) - fields:
        raise ValueError("Algorithm accepts inputs, steps, outputs and integration")

    def text(row, key, limit, required=True):
        result = row.get(key, "")
        if not isinstance(result, str) or len(result) > limit or any(
                ord(c) < 32 and c not in "\n\t" for c in result):
            raise ValueError("Algorithm %s must be bounded plain text" % key)
        if required and not result.strip():
            raise ValueError("Algorithm %s is required" % key)
        return result.strip()

    def integer(row, key, event=False, local=False):
        result = row.get(key)
        if isinstance(result, bool) or not isinstance(result, int) or not 0 < result <= 0xffffffffffffffff:
            raise ValueError("Algorithm %s must be a positive integer" % key)
        if event and (result not in actions or not actions[result].get("isAction", True)
                      or actions[result].get("hasWork") is False):
            raise ValueError("Algorithm %s must identify an effective frame action" % key)
        if local and result not in members:
            raise ValueError("Algorithm calculation events must belong to their stage")
        return result

    result = {}
    for key in ("inputs", "steps", "outputs", "integration"):
        rows = value.get(key, [])
        if not isinstance(rows, list) or len(rows) > 32:
            raise ValueError("Algorithm %s supports at most 32 key entries" % key)
        result[key] = []
        seen = set()
        for row in rows:
            allowed = ({"eventId", "resourceId", "role", "producerEventId", "evidence"} if key == "inputs" else
                       {"eventId", "title", "calculation", "evidence"} if key == "steps" else
                       {"eventId", "resourceId", "role"} if key == "outputs" else
                       {"sourceEventId", "resourceId", "mode", "eventId", "explanation", "evidence", "targetResourceId"})
            if not isinstance(row, Mapping) or set(row) - allowed:
                raise ValueError("Algorithm %s has unsupported fields" % key)
            clean = {"eventId": integer(row, "eventId", event=True, local=key != "integration")}
            if key == "steps":
                clean.update(title=text(row, "title", 160), calculation=text(row, "calculation", 1200),
                             evidence=text(row, "evidence", 2400, False))
            else:
                clean["resourceId"] = integer(row, "resourceId")
            if key in ("inputs", "outputs"):
                clean["role"] = text(row, "role", 240)
                identity = (clean["eventId"], clean["resourceId"])
                if identity in seen:
                    raise ValueError("Algorithm resource roles must be unique per event")
                seen.add(identity)
            if key == "inputs":
                clean["evidence"] = text(row, "evidence", 2400, False)
                if "producerEventId" in row:
                    producer = integer(row, "producerEventId", event=True)
                    if actions[producer]["executionIndex"] >= actions[clean["eventId"]]["executionIndex"]:
                        raise ValueError("Algorithm input producer must precede its consumer")
                    clean["producerEventId"] = producer
            if key == "integration":
                clean["sourceEventId"] = integer(row, "sourceEventId", event=True, local=True)
                mode = row.get("mode")
                if mode not in ("downstream", "in-place", "present", "non-image"):
                    raise ValueError("Algorithm integration mode is invalid")
                clean.update(mode=mode, explanation=text(row, "explanation", 1200),
                             evidence=text(row, "evidence", 2400, False))
                source_order = actions[clean["sourceEventId"]]["executionIndex"]
                target_order = actions[clean["eventId"]]["executionIndex"]
                if target_order < source_order or (mode == "downstream" and target_order == source_order):
                    raise ValueError("Algorithm integration must follow its producer")
                if mode == "in-place" and target_order != source_order:
                    raise ValueError("In-place integration must refer to the producing event")
                if "targetResourceId" in row:
                    clean["targetResourceId"] = integer(row, "targetResourceId")
            result[key].append(clean)
    order = [actions[row["eventId"]]["executionIndex"] for row in result["steps"]]
    if order != sorted(order):
        raise ValueError("Algorithm steps must follow frame execution order")
    return result


def referenced_events(algorithm):
    events = set()
    for rows in (algorithm or {}).values():
        for row in rows:
            events.update(row[key] for key in ("eventId", "sourceEventId", "producerEventId") if key in row)
    return events


def write_index(actions, observations):
    """Index possible writers once; bindings remain candidates, not full overwrites."""
    resources = {}
    for event, action in actions.items():
        observed = observations.get(str(event), {})
        decoded = observed.get("indirectWorkload", observed.get("details", {}).get("indirectWorkload", {}))
        decoded_zero = (decoded.get("status") == "resolved" and
                        decoded.get("source") in {"native-structured-indirect-arguments", "event-bound-indirect-arguments"} and
                        decoded.get("eventId") == event and decoded.get("nonZeroWorkload") is False)
        # A retained binding on a decoded zero-instance draw cannot overwrite a
        # preceding resource version. Unknown indirect work remains a candidate.
        if action.get("hasWork") is False or decoded_zero:
            continue
        if action.get("isAction") is False or action.get("flags", 0) & 0x0100:
            # Markers retain pipeline bindings; Present observes its backbuffer.
            # Neither changes those resource versions.
            continue
        flags = action.get("flags", 0)
        destination = action.get("copyDestination", 0)
        utility = bool(flags & (0x0001 | 0x0400 | 0x0800 | 0x1000)) and not bool(
            flags & (0x0002 | 0x0004 | 0x0008 | 0x4000))
        if utility and destination:
            # Native Clear/Discard/Copy/Resolve actions identify their actual
            # destination. outputs/depthOutput (and old pipeline inventories)
            # can instead contain the preceding draw's still-bound targets.
            # A clear of another resource must not break this version chain.
            # With no exact destination, keep candidates conservatively.
            ids = {destination}
        else:
            ids = set(action.get("outputs", [])) | {action.get("depthOutput", 0), destination}
            ids.update(item.get("resourceId", 0) for item in observed.get("expectedOutputs", []))
        for rid in ids - {0, None}:
            resources.setdefault(rid, set()).add((action["executionIndex"], event))
    return {rid: sorted(writers) for rid, writers in resources.items()}


def intervening_writers(index, actions, rid, producer, consumer):
    values = index.get(rid, [])
    first = bisect_right(values, (actions[producer]["executionIndex"], float("inf")))
    last = bisect_left(values, (actions[consumer]["executionIndex"], -1))
    return [event for _, event in values[first:last]]


def _observation(observations, event):
    value = observations.get(str(event), {})
    return value if isinstance(value, Mapping) else {}


def _indirect_proof(observed):
    value = observed.get("indirectWorkload", (observed.get("details") or {}).get("indirectWorkload", {}))
    return value if isinstance(value, Mapping) else {}


def _workload_verified(action, observed, event):
    if action.get("hasWork") is False:
        return False
    if action.get("hasWork") is None and action.get("flags", 0) & 0x080000:
        proof = _indirect_proof(observed)
        return (proof.get("status") == "resolved" and proof.get("eventId") == event and
                proof.get("source") in {"native-structured-indirect-arguments", "event-bound-indirect-arguments"} and
                proof.get("nonZeroWorkload") is True)
    return True


def _resource_bindings(actions, observations, event, rid, output=False):
    """Captured views, with utility operands overriding inherited pipeline state."""
    action, obs = actions[event], _observation(observations, event)
    flags = action.get("flags", 0)
    utility = bool(flags & (0x0001 | 0x0100 | 0x0400 | 0x0800 | 0x1000)) and not bool(
        flags & (0x0002 | 0x0004 | 0x0008 | 0x4000))
    operand = "copyDestination" if output or flags & 0x0100 else "copySource"
    if utility and action.get(operand):
        if action[operand] != rid:
            return []
        # A Present action's destination is also its observable final output.
        # write_index separately excludes Present from resource mutations.
        binding = {"resourceId": rid, "kind": "action-output" if output else "action-input"}
        # CopyResource and subresource copies share ActionFlags.Copy, and the
        # former carries a default (0,0,0) native Subresource. The outline alone
        # therefore cannot certify the transfer extent. Return the operand but
        # leave its range unknown until exact API/view evidence is available.
        return [binding]
    keys = ("expectedOutputs",) if output else ("textureInputs", "inputs")
    bindings = [item for key in keys for item in (obs.get(key) or []) if isinstance(item, Mapping)]
    if not output:
        bindings += [item for item in (obs.get("expectedOutputs") or [])
                     if isinstance(item, Mapping) and item.get("kind") == "uav"]
    return [item for item in bindings if item.get("resourceId") == rid]


def _view_interval(binding, axis):
    """Return a proven extent, not an assumed single mip for an old SRV base."""
    subresource = binding.get("subresource")
    if axis != "byte" and isinstance(subresource, Mapping):
        value = subresource.get(axis)
        if type(value) is int and 0 <= value < 0xffffffff:
            return value, value + 1
    start_key, count_key, fallback = {
        "mip": ("firstMip", "numMips", "mip"),
        "slice": ("firstSlice", "numSlices", "slice"),
        "byte": ("byteOffset", "byteSize", "byteOffset"),
    }[axis]
    start, count = binding.get(start_key, binding.get(fallback)), binding.get(count_key)
    if type(start) is int and start >= 0 and type(count) is int and count > 0:
        return start, start + count
    return None


def _views_disjoint(left, right):
    # A buffer inventory also contains placeholder mip/slice values; those
    # cannot partition a buffer. Missing resourceType is allowed for legacy
    # texture receipts, but an explicitly typed buffer only uses byte ranges.
    types = {left.get("resourceType"), right.get("resourceType")}
    axes = (("byte",) if "buffer" in types else
            ("mip", "slice") if "texture" in types else ("byte", "mip", "slice"))
    for axis in axes:
        a, b = _view_interval(left, axis), _view_interval(right, axis)
        if a is not None and b is not None and (a[1] <= b[0] or b[1] <= a[0]):
            return True
    return False


def _version_writers(index, actions, observations, rid, producer, consumer):
    """Ignore only writes proven disjoint from every view read by the consumer."""
    candidates = intervening_writers(index, actions, rid, producer, consumer)
    obs = _observation(observations, consumer)
    reads = _resource_bindings(actions, observations, consumer, rid)
    if not reads or obs.get("bindingErrors") or obs.get("bindingsComplete") is False:
        return candidates, []
    relevant, disjoint = [], []
    for event in candidates:
        observed = _observation(observations, event)
        writes = _resource_bindings(actions, observations, event, rid, True)
        excluded = (writes and not observed.get("bindingErrors") and observed.get("bindingsComplete") is not False
                    and all(_views_disjoint(write, read) for write in writes for read in reads))
        (disjoint if excluded else relevant).append(event)
    return relevant, disjoint


def _dependency_investigation(actions, observations, rid, producer, consumer, candidates=(),
                              disjoint=(), flow_id=None, purpose="resource-version"):
    """Bounded, reproducible recovery hints, never Agent-supplied proof overrides."""
    view_keys = ("resourceId", "kind", "index", "stage", "resourceType", "firstMip", "numMips",
                 "firstSlice", "numSlices", "mip", "slice", "byteOffset", "byteSize", "subresource")

    def event_facts(event, output):
        if event is None:
            return None
        action, obs = actions[event], _observation(observations, event)
        bindings = _resource_bindings(actions, observations, event, rid, output) if rid else []
        facts = {"eventId": event, "executionIndex": action["executionIndex"],
                 "actionFlags": action.get("flags", 0), "actionKind": action.get("actionKind"),
                 "hasWork": action.get("hasWork"), "bindingsObserved": bool(obs),
                 "bindingsComplete": obs.get("bindingsComplete"),
                 "bindingErrors": deepcopy((obs.get("bindingErrors") or [])[:4]),
                 "bindingCount": len(bindings), "bindingsTruncated": len(bindings) > 16,
                 "bindings": [{key: deepcopy(row[key]) for key in view_keys if key in row}
                              for row in bindings[:16]]}
        for key in ("copySource", "copyDestination", "copySourceSubresource", "copyDestinationSubresource"):
            if action.get(key):
                facts[key] = deepcopy(action[key])
        proof = _indirect_proof(obs)
        if proof:
            facts["indirectWorkload"] = {key: deepcopy(proof[key]) for key in (
                "status", "source", "eventId", "nonZeroWorkload", "argumentType", "workload") if key in proof}
        return facts

    candidates = sorted(set(candidates), key=lambda event: actions[event]["executionIndex"])
    evidence = {"schema": "bread-frame-dependency-investigation/v1", "purpose": purpose,
                "resourceId": rid, "candidateOnly": True,
                "producer": event_facts(producer, True), "consumer": event_facts(consumer, False),
                "writeCandidateCount": len(candidates), "writeCandidatesTruncated": len(candidates) > 12,
                "writeCandidates": [event_facts(event, True) for event in candidates[:12]],
                "disjointWriteCount": len(disjoint), "disjointWritesTruncated": len(disjoint) > 12,
                "disjointWrites": [event_facts(event, True) for event in disjoint[:12]]}
    actions_out = []

    def request(tool, arguments, reason, scoped=False):
        row = {"tool": tool, "arguments": arguments, "reason": reason}
        if scoped:
            if flow_id:
                arguments["flow_id"] = flow_id
            else:
                row["requiresContext"] = ["flow_id"]
        if row not in actions_out:
            actions_out.append(row)

    for event in dict.fromkeys([value for value in (producer, consumer, *candidates[:12]) if value is not None]):
        action, obs = actions[event], _observation(observations, event)
        if (not _workload_verified(action, obs, event) or
                action.get("flags", 0) & (0x0001 | 0x0400 | 0x0800 | 0x1000)):
            request("inspect_event_api_parameters", {"event_id": event}, "inspect-recorded-workload-or-utility-operands")
        request("inspect_frame_render_stage", {"event_id": event, "refresh": False},
                "reuse-or-obtain-event-bindings-and-view-ranges", True)
    if rid:
        for event, direction in ((consumer, "before"), (producer, "after")):
            if event is not None:
                request("inspect_frame_resource_usage", {"resource_id": rid, "anchor_event_id": event,
                        "direction": direction, "order": "nearest", "offset": 0, "limit": 50, "refresh": False},
                        "trace-actual-version-candidates", True)
    return {"dependencyEvidence": evidence, "recoveryActions": actions_out,
            "recoveryPolicy": {"reuseValidEvidence": True, "usageResultsAreCandidates": True,
                "followUsageNextOffset": True, "incompleteUsageDoesNotProveNoWriter": True,
                "resolution": "Inspect relevant views and operations; follow the actual updated producer or "
                    "explain evidenced partial writes/accumulation. Recurse through its inputs and confirm its downstream use. "
                    "An external-stage mutation cannot be added as a local calculation; trace that stage's result. "
                    "A failed read is a route failure, not proof of a broken dependency."}}


def relevant_observation_events(algorithm, actions, index):
    events = referenced_events(algorithm)
    if algorithm:
        for row in algorithm["inputs"]:
            if "producerEventId" in row:
                events.update(intervening_writers(index, actions, row["resourceId"], row["producerEventId"], row["eventId"]))
        for row in algorithm["integration"]:
            events.update(intervening_writers(index, actions, row["resourceId"], row["sourceEventId"], row["eventId"]))
    return events


def _explained_utility_stage(stage, actions):
    """A described copy/clear/resolve needs no invented Shader calculation table.

    Classify from captured members, never from the Agent's chosen category or
    title. A mixed stage still needs its real draw/dispatch calculation evidence.
    """
    members = stage.get("eventIds")
    if not isinstance(members, list) or not members:
        return False
    for event in members:
        action = actions.get(event)
        if (not isinstance(action, Mapping) or action.get("isAction") is not True or
                action.get("flags", 0) & (0x0002 | 0x0004 | 0x0008 | 0x4000) or
                action.get("actionKind") in ("draw", "dispatch")):
            return False
    investigation = stage.get("investigation")
    evidence = investigation.get("evidence") if isinstance(investigation, Mapping) else None
    summary = stage.get("summary")
    return (isinstance(investigation, Mapping) and investigation.get("status") == "investigated" and
            isinstance(evidence, str) and bool(evidence.strip()) and
            isinstance(summary, str) and bool(summary.strip()) and
            stage.get("confidence") in ("captured", "inferred"))


def algorithm_gaps(stage, actions, observations, writers=None, flow_id=None):
    result = []

    def gap(code, message, next_action, **details):
        entry = {"code": code, "stageId": stage["id"], "message": message, "nextAction": next_action}
        entry.update(details)
        if entry not in result:
            result.append(entry)

    algorithm = stage.get("algorithm")
    if algorithm is None:
        if _explained_utility_stage(stage, actions):
            return result
        gap("missing-stage-algorithm", "The stage does not explain its key calculations and resource path.",
            "Investigate the key events, then record algorithm inputs, steps, outputs and integration before completing this stage.")
        return result
    writers = writers if writers is not None else write_index(actions, observations)

    def observation(event):
        return _observation(observations, event)

    def investigation(rid, producer, consumer, candidates=(), disjoint=(), purpose="resource-version"):
        return _dependency_investigation(actions, observations, rid, producer, consumer,
                                         candidates, disjoint, flow_id, purpose)

    # Keep unknown indirect steps publishable so investigation can continue,
    # but a bound RTV/SRV and a written explanation do not establish execution.
    for event in sorted(referenced_events(algorithm)):
        if actions[event].get("hasWork") is None and actions[event].get("flags", 0) & 0x080000:
            observed = observation(event)
            decoded = observed.get("indirectWorkload", observed.get("details", {}).get("indirectWorkload", {}))
            if (decoded.get("status") != "resolved" or
                    decoded.get("source") not in {"native-structured-indirect-arguments", "event-bound-indirect-arguments"} or
                    decoded.get("eventId") != event or not isinstance(decoded.get("nonZeroWorkload"), bool)):
                gap("unverified-algorithm-binding", "The indirect workload at EID %d is not established as non-zero." % event,
                    "Read inspect_event_api_parameters for this exact child or its ExecuteIndirect parent. "
                    "Use the decoded argument evidence; if it proves zero work, remove this event from the contributing calculation path.",
                    **investigation(None, event, None, purpose="indirect-workload"))
            elif decoded["nonZeroWorkload"] is False:
                result.setdefault("nonContributingEvents", []).append(event)

    def bound(event, rid, output=False):
        # A failed read of another descriptor does not erase this exact known
        # binding. Completeness matters separately when excluding write scopes.
        return bool(_resource_bindings(actions, observations, event, rid, output))

    def check_binding(event, rid, output=False):
        if bound(event, rid, output):
            return True
        gap("unverified-algorithm-binding", "EID %d has no recorded %s binding for resource %d." %
            (event, "output" if output else "input", rid),
            "Inspect this exact event and correct the resource role; do not infer a producer or consumer from nearby EIDs.",
            **investigation(rid, event if output else None, None if output else event, purpose="binding"))
        return False

    steps = {row["eventId"] for row in algorithm["steps"]}
    if not steps or not algorithm["outputs"]:
        gap("incomplete-stage-algorithm", "The explanation has no key calculation or result.",
            "Explain the actual operation and the result it produces; a pass name or an output screenshot is insufficient.")
    for row in algorithm["steps"]:
        event = row["eventId"]
        if not row["evidence"]:
            gap("missing-algorithm-step-evidence", "EID %d lacks evidence for its calculation." % event,
                "Inspect the relevant Shader expression or copy/blend operation and record a concrete source or instruction anchor.")
        obs = observation(event)
        shader_action = bool(actions[event].get("flags", 0) & (0x0002 | 0x0004 | 0x0008 | 0x4000)) or actions[event].get("actionKind") in ("draw", "dispatch")
        if shader_action and not isinstance(obs.get("expectedOutputs"), list):
            gap("unverified-algorithm-binding", "The key calculation at EID %d has not been inspected." % event,
                "Inspect every declared key draw/dispatch before completing the calculation explanation.")
        if obs.get("bindingErrors") or obs.get("bindingsComplete") is False:
            gap("unverified-algorithm-binding", "EID %d has incomplete input/output inspection." % event,
                "Resolve the failed binding inspection before claiming the calculation path is complete.")
        reads = [item for item in obs.get("textureInputs", []) if item.get("resourceId")]
        if reads and not any(item["eventId"] == event for item in algorithm["inputs"]):
            gap("incomplete-stage-algorithm", "EID %d has resource inputs but no explanation of them." % event,
                "Identify the inputs needed to explain this calculation and trace their actual producers or temporal boundary.")
    for row in algorithm["inputs"]:
        check_binding(row["eventId"], row["resourceId"])
        if "producerEventId" in row:
            check_binding(row["producerEventId"], row["resourceId"], True)
            intermediate, disjoint = _version_writers(writers, actions, observations, row["resourceId"],
                                                       row["producerEventId"], row["eventId"])
            # An explanation can account for inspected partial writes, but it
            # cannot turn an uninspected/failed/unknown-work event into proof.
            inspected = all(bound(event, row["resourceId"], True) and
                            not observation(event).get("bindingErrors") and
                            observation(event).get("bindingsComplete") is not False and
                            _workload_verified(actions[event], observation(event), event) for event in intermediate)
            if intermediate and (not row["evidence"] or not inspected):
                gap("unverified-algorithm-binding", "Resource %d has intervening write candidates before EID %d: %s." %
                    (row["resourceId"], row["eventId"], ", ".join(map(str, intermediate[:12]))),
                    "Inspect relevant writes and views. Select the actual input version or explain, with evidence, which prior contributions survive partial writes or accumulation.",
                    **investigation(row["resourceId"], row["producerEventId"], row["eventId"], intermediate, disjoint))
        elif not row["evidence"]:
            gap("incomplete-stage-algorithm", "The source of resource %d at EID %d is unexplained." %
                (row["resourceId"], row["eventId"]),
                "Trace its producer, or document the evidenced external/history input boundary without inventing a current-frame writer.",
                **investigation(row["resourceId"], None, row["eventId"], purpose="input-source"))
    outputs = {(row["eventId"], row["resourceId"]) for row in algorithm["outputs"]}
    integrated = set()
    for event, rid in sorted(outputs):
        check_binding(event, rid, True)
        if event not in steps:
            gap("incomplete-stage-algorithm", "The calculation producing resource %d at EID %d is missing." % (rid, event),
                "Record the key producing event and explain how it computes this result.")
    for row in algorithm["integration"]:
        source = (row["sourceEventId"], row["resourceId"])
        if source not in outputs:
            raise ValueError("Algorithm integration must refer to a declared key output")
        event, rid, mode = row["eventId"], row["resourceId"], row["mode"]
        valid = bool(row["evidence"])
        intermediate, disjoint = _version_writers(writers, actions, observations, rid, row["sourceEventId"], event)
        if intermediate:
            # A general composition claim does not account for mutations of the
            # source. Key intervening writes must be investigated as steps too.
            investigated = {item["eventId"] for item in algorithm["steps"] if item["evidence"]}
            if not set(intermediate) <= investigated:
                valid = False
                gap("unverified-algorithm-binding", "Resource %d changes between its declared result and composition at EID %d." % (rid, event),
                    "Follow the changed resource version. Use the final relevant producer or include and inspect relevant local calculation steps; trace other stages through their actual results.",
                    **investigation(rid, row["sourceEventId"], event, intermediate, disjoint))
        if mode == "present":
            valid = valid and bool(actions[event].get("flags", 0) & 0x0100) and actions[event].get("copyDestination") == rid
        elif mode == "downstream":
            valid = check_binding(event, rid) and valid
            if "targetResourceId" in row:
                valid = check_binding(event, row["targetResourceId"], True) and valid
            else:
                valid = False
        elif mode == "non-image":
            valid = valid and any(item.get("resourceId") == rid and item.get("resourceType") == "buffer"
                                  for item in observation(row["sourceEventId"]).get("expectedOutputs", []))
            if event != row["sourceEventId"]:
                valid = check_binding(event, rid) and valid
        else:
            valid = check_binding(event, rid, True) and valid
        if valid:
            integrated.add(source)
    for event, rid in sorted(outputs - integrated):
        gap("missing-algorithm-integration", "The destination or composition of resource %d from EID %d is unverified." % (rid, event),
            "Find the actual consumer and its output. Explain the blend/composition; for in-place work or non-image data, record its evidenced role.",
            **investigation(rid, event, None, purpose="output-destination"))
    return result
