"""Agent-directed restoration handoff observations, not an execution gate."""
import hashlib
from pathlib import Path


def _delivery_observations(delivery):
    """Observe files without deleting anything or treating annotations as proof."""
    findings = []
    project_value = delivery.get("projectPath")
    project = Path(project_value) if isinstance(project_value, str) and project_value else None
    if project is None or not project.is_absolute() or not (project / "Assets").is_dir():
        return [{"code": "delivery-project-not-observed"}], {"sceneReopenObserved": False,
            "artifactCleanupObserved": False, "remainingTemporaryArtifacts": [], "missingFinalAssets": []}

    def project_path(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Delivery paths must be nonempty project-relative or absolute paths")
        path = Path(value)
        return (path if path.is_absolute() else project / path).resolve()

    scene = delivery.get("sceneDelivery") or {}
    if not isinstance(scene, dict):
        raise ValueError("sceneDelivery must be an observation object")
    scene_path = project_path(scene["scenePath"]) if scene.get("scenePath") else None
    scene_file = bool(scene_path and scene_path.is_relative_to((project / "Assets").resolve())
        and scene_path.suffix.lower() == ".unity" and scene_path.is_file())
    if not scene_file:
        findings.append({"code": "saved-scene-not-observed"})
    scene_hash = None
    if scene_file:
        digest = hashlib.sha256()
        with scene_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        scene_hash = digest.hexdigest()
    scene_current = bool(scene_hash and scene.get("sceneSha256") == scene_hash)
    if scene_file and not scene_current:
        findings.append({"code": "scene-reopen-content-not-current"})
    reopen = (scene.get("saved") is True and scene.get("reopenedFromDisk") is True
        and scene.get("requiresSetupCommand") is False and type(scene.get("missingScripts")) is int
        and scene["missingScripts"] == 0 and scene.get("missingReferences") == []
        and scene.get("compileErrors") == [] and scene.get("finalOutputsReviewed") is True)
    proof = scene.get("evidencePaths")
    evidence = (isinstance(proof, list) and 0 < len(proof) <= 64
        and all(isinstance(path, str) and Path(path).is_absolute() and Path(path).is_file() for path in proof))
    if not (reopen and evidence and scene_current):
        findings.append({"code": "scene-reopen-without-setup-not-observed",
            "diagnostic": "Save serialized scene/assets, remove construction and validation helpers, reopen the .unity normally, and inspect current final Scene/Game output without a setup command."})

    inventory = delivery.get("artifactInventory")
    if inventory is None:
        inventory = []
    if not isinstance(inventory, list) or len(inventory) > 4096 or any(not isinstance(row, dict) for row in inventory):
        raise ValueError("Supply at most 4096 generated artifact observations")
    reviewed = delivery.get("artifactInventoryReviewed") is True and "artifactInventory" in delivery
    if not reviewed:
        findings.append({"code": "generated-artifact-inventory-not-reviewed"})
    inventory_finding_count = len(findings)
    remaining, missing, runtime, seen = [], [], [], set()
    for row in inventory:
        path = project_path(row.get("path"))
        if path in seen:
            raise ValueError("Each generated artifact needs one unambiguous role")
        seen.add(path)
        role = row.get("role")
        if role not in ("temporary", "final", "runtime"):
            raise ValueError("Artifact role must be temporary, final or runtime")
        if role == "temporary":
            residue = [path]
            if path.is_relative_to((project / "Assets").resolve()):
                residue.append(Path(str(path) + ".meta"))
            remaining.extend(str(item) for item in residue if item.exists())
            if row.get("requiredByFinalAssets") is True:
                findings.append({"code": "temporary-artifact-required-by-final-assets", "path": str(path)})
        else:
            if not path.is_file():
                missing.append(str(path))
            if role == "runtime":
                if not path.is_relative_to(project.resolve()):
                    findings.append({"code": "runtime-asset-outside-project", "path": str(path)})
                runtime.append(str(path))
                if not isinstance(row.get("reason"), str) or not row["reason"].strip():
                    findings.append({"code": "runtime-asset-purpose-missing", "path": str(path)})
    if remaining:
        findings.append({"code": "temporary-artifacts-remain", "paths": remaining})
    if missing:
        findings.append({"code": "final-assets-missing", "paths": missing})
    return findings, {"sceneReopenObserved": bool(scene_file and reopen and evidence and scene_current),
        "sceneSha256": scene_hash,
        "artifactCleanupObserved": bool(reviewed and len(findings) == inventory_finding_count),
        "remainingTemporaryArtifacts": remaining, "missingFinalAssets": missing, "retainedRuntimeAssets": runtime,
        "scope": "File presence is observed; reopen/render, ownership and dependency annotations are supplied by the Agent. This read-only report neither deletes files nor certifies visual fidelity."}


def review_handoff(effects, comparisons, attempts, delivery, no_progress_budget=3, diagnosis=None, checkpoint_budget=12):
    if not isinstance(effects,list) or len(effects)>128 or not isinstance(comparisons,list) or len(comparisons)>128:
        raise ValueError("Supply at most 128 effect and comparison observations")
    if not isinstance(attempts,list) or len(attempts)>256 or not isinstance(delivery,dict):
        raise ValueError("Supply bounded attempt observations and a delivery object")
    if any(not isinstance(row,dict) for row in comparisons+attempts):
        raise ValueError("Comparison and attempt observations must be objects")
    if type(no_progress_budget) is not int or not 1<=no_progress_budget<=16:
        raise ValueError("Use a finite advisory no-progress budget of 1..16 distinct attempts")
    if diagnosis is not None and not isinstance(diagnosis,dict):
        raise ValueError("Reuse the optional difference-localization report")
    if type(checkpoint_budget) is not int or not 1<=checkpoint_budget<=128:
        raise ValueError("Use a finite advisory checkpoint_budget of 1..128 distinct attempts")
    findings=[]; unresolved=[]; tradeoffs=[]; stalled=[]; checkpoints=[]; unresolved_limits=[]; remaining_routes={}
    isolated={row.get("effect") for row in (diagnosis or {}).get("isolation",[])
        if (diagnosis or {}).get("schema")=="bread-unity-difference-localization/v1" and
        row.get("observedConsistentRemainderAndEffectDifference") is True and
        row.get("remainderScope")=="full-frame-remainder" and row.get("diagnosticStateRestored") is True}
    names=[row.get("name") for row in effects if isinstance(row,dict)]
    if len(names)!=len(effects) or any(not isinstance(n,str) or not n.strip() for n in names) or len(set(names))!=len(names):
        raise ValueError("Each effect needs a unique meaningful name")
    active={row["name"] for row in effects if row.get("state") not in ("disabled","unresolved")}
    unavailable={row["name"] for row in effects if row.get("state") in ("disabled","unresolved")}
    for row in effects:
        name=row["name"];state=row.get("state")
        if state not in ("matched","approximation","accepted-difference","disabled","unresolved"):
            raise ValueError("Effect state must be matched, approximation, accepted-difference, disabled or unresolved")
        if state=="unresolved": unresolved.append(name)
        if state!="matched" and not row.get("explanation"):
            findings.append({"code":"effect-limit-unexplained","effect":name})
        if row.get("dependencyKind")=="history" and row.get("runtimeHistoryInvestigated") is not True:
            findings.append({"code":"runtime-history-route-not-investigated","effect":name})
        if state in ("disabled","approximation","accepted-difference") and row.get("userDecision",{}).get("accepted") is not True:
            tradeoffs.append(name)
        if state=="disabled":
            findings.append(dict(code="delivery-effect-disabled",effect=name,
                diagnostic="Temporary bypass is an attribution experiment, not the requested restoration outcome; retain legacy decision data without suggesting omission"))
        if row.get("diagnosticBypassUsed") is True and row.get("diagnosticBypassRestored") is not True:
            findings.append(dict(code="diagnostic-bypass-not-restored",effect=name))
        if state in ("unresolved","approximation","accepted-difference") and name not in isolated:
            findings.append(dict(code="residual-effect-attribution-unproven",effect=name,
                diagnostic="Before attributing residual error to this effect, compare its isolated output and the equivalent remainder under a controlled single-effect intervention"))
        assessment=row.get("resolutionAssessment") or {}
        routes=assessment.get("remainingFeasibleRoutes",[])
        if not isinstance(routes,list) or any(not isinstance(route,str) or not route.strip() for route in routes):
            raise ValueError("remainingFeasibleRoutes lists concrete executable recovery alternatives")
        if routes:remaining_routes[name]=routes
        paths=assessment.get("evidencePaths") or []
        real=bool(paths) and all(isinstance(path,str) and Path(path).is_absolute() and Path(path).is_file() for path in paths)
        if (not routes and assessment.get("reason") and real and
                ((assessment.get("status")=="exhausted-supported-recovery" and assessment.get("attemptedAlternatives")) or
                 assessment.get("status")=="external-user-input-required")):
            unresolved_limits.append(name)
        observed=set(); repeated=False; repeated_since_progress=False; trailing=set()
        for attempt in attempts:
            if row.get("budgetWindowId") and attempt.get("budgetWindowId")!=row["budgetWindowId"]:continue
            if attempt.get("effect")==name and attempt.get("strategy") and attempt.get("inputFingerprint"):
                key=(str(attempt["strategy"]),str(attempt["inputFingerprint"]))
                if attempt.get("materialProgress") is True:
                    trailing.clear();repeated_since_progress=False
                else:
                    if key in observed: repeated=True;repeated_since_progress=True
                    trailing.add(key)
                observed.add(key)
        # A repaired hypothesis/input may be tried once again. Merely logging,
        # reviewing or repeating an identical strategy does not create progress.
        if repeated:
            findings.append({"code":"unchanged-attempt-repeated","effect":name})
        if len(trailing)>=no_progress_budget or repeated_since_progress:
            stalled.append(name)
            findings.append({"code":"effect-no-material-progress","effect":name,"distinctAttempts":len(observed)})
        if len(observed)>=checkpoint_budget:
            checkpoints.append(name)
            findings.append(dict(code="effect-checkpoint-budget-reached",effect=name,distinctAttempts=len(observed),budget=checkpoint_budget))
    baseline=False
    for comparison in comparisons:
        paths=comparison.get("evidencePaths") or []
        real=bool(paths) and all(isinstance(path,str) and Path(path).is_absolute() and Path(path).is_file() for path in paths)
        if (comparison.get("referenceOrigin") in {"rdc-original","rdc-controlled-isolation"} and comparison.get("matched") is True and
            comparison.get("sameInputs") is True and set(comparison.get("referenceEffects",[]))==active and
            set(comparison.get("renderedEffects",[]))==active and set(comparison.get("bypassedEffects",comparison.get("disabledEffects",[])))==unavailable and real):
            baseline=True
    if unavailable and not baseline:
        findings.append({"code":"remaining-effects-matched-baseline-missing","disabledEffects":sorted(unavailable),
                         "diagnostic":"Compare matching enabled effect sets / pre-effect inputs; disabling only Unity while comparing full RDC is not a valid isolated baseline"})
    for item in ("dualViewFinalOutputs","resizeAndTemporalChecks","assetLayoutReviewed","temporaryDiagnosticsRemoved"):
        if delivery.get(item) is not True:
            findings.append({"code":"handoff-observation-missing","observation":item})
    delivery_findings, delivery_observations = _delivery_observations(delivery)
    findings.extend(delivery_findings)
    # Counters trigger internal replanning, never a user budget/permission gate.
    # The Agent supplies saved evidence that supported recovery really cannot
    # proceed; missing tool/unknown history or its own unapproved approximation
    # alone is not such a limit.
    decision=bool((set(unresolved)|set(tradeoffs)).intersection(unresolved_limits))
    independent=delivery.get("remainingIndependentWorkComplete") is True
    affected=set(tradeoffs)|set(unresolved)
    options=(["Continue targeted repairs with a new hypothesis/budget",
              "Accept the quantified residual difference with the effect retained"] if affected and affected<=isolated else
             ["Provide the missing evidence/access or choose a new recovery route",
              "Preserve the measured unresolved checkpoint for a later decision"])
    return {"schema":"bread-unity-handoff-review/v1","diagnosticsOnly":True,"acceptanceBy":"agent",
        "unresolvedEffects":unresolved,"unapprovedTradeoffs":tradeoffs,"noProgressEffects":stalled,
        "checkpointEffects":checkpoints,
        "reassessmentSuggested":bool(stalled or checkpoints),"unresolvedRecoveryLimits":unresolved_limits,
        "remainingFeasibleRoutes":remaining_routes,"deliveryObservations":delivery_observations,
        "matchedRemainingEffectsBaselineObserved":baseline,"findings":findings,"userDecisionSuggested":decision,
        "isolatedDifferenceEffects":sorted(isolated),"temporaryBypassIsDeliveryOption":False,
        "nextAction":"save-checkpoint-and-ask-user" if decision and independent else
            "continue-independent-work" if decision else "execute-remaining-feasible-recovery" if remaining_routes else
            "reassess-and-continue-repairs" if stalled or checkpoints else "repair-concrete-findings",
        "suggestedOptions":options if decision else [],
        "scope":"Input-control, effect-match and user-decision fields are Agent annotations; this report does not verify consent or grant acceptance. Missing history alone is not proof that dynamic reconstruction is impossible."}
