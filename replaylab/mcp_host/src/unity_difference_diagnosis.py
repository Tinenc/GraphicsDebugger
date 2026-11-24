"""Reuse numerical reports to narrow a supplied dependency graph; no quality gate."""
import math


def _status(row):
    metrics=row.get("metrics") or {}
    keys=("comparedComponents","nonFiniteComponents","componentsOverTolerance")
    if any(type(metrics.get(key)) is not int or metrics[key]<0 for key in keys):return "unknown"
    if metrics["nonFiniteComponents"]:return "different"
    if metrics["comparedComponents"]==0:return "unknown"
    return "different" if metrics["componentsOverTolerance"] else "within-supplied-tolerance"


def _input_difference(item):
    absolute=item.get("absoluteTolerance",0);relative=item.get("relativeTolerance",0)
    if any(type(value) not in (float,int) or not math.isfinite(value) or value<0 for value in (absolute,relative)):
        raise ValueError("Input tolerances must be finite nonnegative numbers")
    differences=[];visited=0;different=0
    def walk(a,b,path,depth):
        nonlocal visited,different
        visited+=1
        if visited>16384 or depth>12:raise ValueError("Input comparison exceeds bounded structure; page the actual fields")
        if isinstance(a,dict) and isinstance(b,dict):
            for key in sorted(set(a)|set(b)):
                if key in a and key in b:walk(a[key],b[key],path+"."+str(key),depth+1)
                else:note(path+"."+str(key),"missing-binding-or-field")
        elif isinstance(a,list) and isinstance(b,list) and len(a)==len(b):
            for index,(left,right) in enumerate(zip(a,b)):walk(left,right,path+f"[{index}]",depth+1)
        elif type(a) in (float,int) and type(b) in (float,int):
            if not math.isfinite(a) or not math.isfinite(b):note(path,"non-finite-input")
            elif abs(a-b)>absolute+relative*abs(a):note(path,"numeric-difference")
        elif type(a)!=type(b) or a!=b:note(path,"layout-or-value-difference")
    def note(path,reason):
        nonlocal different
        different+=1
        if len(differences)<64:differences.append(dict(path=path,reason=reason))
    if "reference" not in item or "rendered" not in item:
        return dict(name=item["name"],kind=item.get("kind"),status="unknown",differences=[],differenceCount=0)
    walk(item["reference"],item["rendered"],item["name"],0)
    return dict(name=item["name"],kind=item.get("kind"),status="different" if different else "within-supplied-tolerance",
                differenceCount=different,differences=differences,truncated=different>len(differences),
                consumers=item.get("consumers",[]))


def localize_difference(comparison,pass_graph=None,inputs=None,isolation=None):
    if not isinstance(comparison,dict) or comparison.get("schema")!="bread-unity-layer-comparison/v1":
        raise ValueError("Reuse a compare_unity_effect_layers report")
    layers=comparison.get("layers",[])
    graph=pass_graph or [];inputs=inputs or [];isolation=isolation or []
    for rows,limit in ((layers,128),(graph,256),(inputs,128),(isolation,32)):
        if not isinstance(rows,list) or len(rows)>limit or any(not isinstance(row,dict) for row in rows):
            raise ValueError("Use bounded comparison/graph/input/isolation object lists")
    def names(rows):
        values=[row.get("name") for row in rows]
        if any(not isinstance(name,str) or not name or len(name)>256 for name in values) or len(set(values))!=len(values):
            raise ValueError("Give each observation a unique short name")
        return values
    names(layers);names(graph);names(inputs)
    observed={row["name"]:row for row in layers}
    states={name:_status(row) for name,row in observed.items()}
    dependencies={row["name"]:row.get("dependsOn",[]) for row in graph}
    for parents in dependencies.values():
        if not isinstance(parents,list) or len(parents)>128 or any(not isinstance(name,str) for name in parents):
            raise ValueError("dependsOn lists actual current-frame input observation names")
    findings=[];frontier=[];propagated=[];unknown=[]
    visited=set();visiting=set();cycles=set()
    def visit(name):
        if name in visiting:cycles.add(name);return
        if name in visited:return
        visiting.add(name)
        for parent in dependencies.get(name,[]):visit(parent)
        visiting.remove(name);visited.add(name)
    for name in dependencies:visit(name)
    cycle_affected=set(cycles)
    while True:
        previous=len(cycle_affected)
        cycle_affected.update(name for name,parents in dependencies.items() if any(parent in cycle_affected for parent in parents))
        if len(cycle_affected)==previous:break
    if cycles:findings.append(dict(code="current-frame-dependency-cycle",names=sorted(cycles),
        diagnostic="Separate previous-frame edges from the current-frame pass graph; no linear/binary-search assumption is valid here"))
    for name,row in observed.items():
        status=states[name]
        if status=="unknown":unknown.append(name);continue
        if status!="different":continue
        parents=dependencies.get(name,[])
        bad=[parent for parent in parents if states.get(parent)=="different"]
        missing=[parent for parent in parents if states.get(parent,"unknown")=="unknown"]
        detail=dict(name=name,module=row.get("module",""),differenceBounds=(row.get("metrics") or {}).get("differenceBounds"),
                    changedPixelFraction=(row.get("metrics") or {}).get("changedPixelFraction"),evidencePaths=row.get("evidencePaths",[]))
        if bad:
            propagated.append(dict(**detail,differingInputs=bad))
        else:
            controlled=row.get("identicalInputFixture") is True or (bool(parents) and not missing and name not in cycle_affected)
            frontier.append(dict(**detail,classification="isolated-input-boundary-candidate" if controlled else "input-or-root-boundary-unproven",
                                 missingInputs=missing))
    input_results=[_input_difference(item) for item in inputs]
    candidates={row["name"] for row in frontier}
    input_results.sort(key=lambda row:(row["status"]!="different",not bool(candidates.intersection(row.get("consumers",[])))))
    isolation_results=[]
    for item in isolation:
        effect=item.get("effect")
        if not isinstance(effect,str) or not effect:raise ValueError("Isolation observations name the actual effect")
        full=item.get("fullOutput");baseline=item.get("bypassOutput");effect_output=item.get("effectOutput")
        base=observed.get(baseline,{})
        controlled=(all(item.get(key) is True for key in ("sameInputs","sameEnabledRemainder","singleEffectIntervention")) and
                    observed.get(effect_output,{}).get("identicalInputFixture") is not False)
        origins={"rdc-original","rdc-controlled-isolation"}
        available=(states.get(full)=="different" and states.get(baseline)=="within-supplied-tolerance" and
                   states.get(effect_output)=="different" and controlled and
                   all(observed.get(name,{}).get("referenceOrigin") in origins for name in (full,baseline,effect_output)))
        shape=base.get("shape",[]);metrics=base.get("metrics",{})
        whole=(base.get("category")=="full-frame" and len(shape)==3 and
               all(type(value) is int and value>0 for value in shape) and
               metrics.get("comparedPixels")==shape[0]*shape[1] and
               (metrics.get("roi") is None or metrics["roi"]==[0,0,shape[0],shape[1]]))
        scope="full-frame-remainder" if available and whole else "local-boundary" if available else "unproven"
        if not available:findings.append(dict(code="effect-attribution-unproven",effect=effect,
            diagnostic="Need original/controlled-reference full output, a matching equivalent remainder, and a divergent isolated effect output under a single-effect intervention"))
        if item.get("restored") is not True:findings.append(dict(code="diagnostic-bypass-restore-unproven",effect=effect))
        isolation_results.append(dict(effect=effect,observedConsistentRemainderAndEffectDifference=available,
            remainderScope=scope,diagnosticStateRestored=item.get("restored") is True,
            rootCauseProven=False,comparisons=[full,baseline,effect_output],
            residualMetrics=observed.get(effect_output,{}).get("metrics",{}),
            finalOutputMetrics=observed.get(full,{}).get("metrics",{})))
    groups={}
    for row in frontier:
        if row["module"]:groups.setdefault(row["module"],[]).append(row["name"])
    return dict(schema="bread-unity-difference-localization/v1",diagnosticsOnly=True,acceptanceBy="agent",
        comparisonStates=states,divergenceFrontier=frontier,downstreamDifferences=propagated,unknownComparisons=unknown,
        inputDifferences=input_results,sharedRepairCandidates=groups,isolation=isolation_results,findings=findings,
        rereadImageBytes=False,rootCauseProven=False,
        nextAction="Inspect the supplied graph's earliest differing boundary and its actual bindings; compare the suspect's output/contribution under fixed inputs. Repair shared inputs/modules once and rerender only affected descendants, then both final views. Temporary bypass is a diagnostic experiment, not an omitted delivery effect.",
        scope="Numerical observations under supplied graph, units, tolerances and intervention annotations; no automatic causal, fidelity or completion certification. Bisection requires a proven linear persistent divergence; branching, temporal edges and cancellation require boundary checks.")
