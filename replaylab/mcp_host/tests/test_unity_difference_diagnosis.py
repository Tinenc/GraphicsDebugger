import copy
import json
from pathlib import Path
import struct

import pytest

from src.unity_layered_validation import compare_layers
from src.unity_difference_diagnosis import localize_difference


def image(tmp_path,name,values,width=None):
    path=tmp_path/(name+".r32f");path.write_bytes(struct.pack("<"+"f"*len(values),*values))
    Path(str(path)+".json").write_text(json.dumps(dict(width=width or len(values),height=1,channels=1)))
    return dict(path=str(path))


def pair(tmp_path,name,left,right,**kwargs):
    return dict(name=name,category="post-process",reference=image(tmp_path,name+"-rdc",left),
                rendered=image(tmp_path,name+"-unity",right),referenceOrigin="rdc-original",**kwargs)


def test_small_spatial_difference_has_exact_bounds_even_with_large_equal_background(tmp_path):
    layer=pair(tmp_path,"shadow",[0]*64,[0]*40+[1]+[0]*23)
    metrics=compare_layers([layer])["layers"][0]["metrics"]
    assert metrics["differenceBounds"]==[40,0,1,1]
    assert metrics["changedPixelFraction"]==1/64 and metrics["pixelsOverTolerance"]==1


def test_equal_after_minus_before_does_not_hide_mismatched_upstream_inputs(tmp_path):
    effect=pair(tmp_path,"AO",[11],[21],referenceBefore=image(tmp_path,"before-r",[10]),renderedBefore=image(tmp_path,"before-u",[20]))
    full=pair(tmp_path,"final",[11],[21]);full["category"]="full-frame"
    baseline=pair(tmp_path,"baseline",[10],[20]);baseline["category"]="full-frame"
    report=compare_layers([effect,full,baseline])
    assert report["layers"][0]["metrics"]["maximumAbsoluteError"]==0
    assert report["layers"][0]["comparisonMode"]=="after-minus-before"
    result=localize_difference(report,isolation=[dict(effect="AO",fullOutput="final",bypassOutput="baseline",effectOutput="AO",
        sameInputs=True,sameEnabledRemainder=True,singleEffectIntervention=True,restored=True)])
    assert not result["isolation"][0]["observedConsistentRemainderAndEffectDifference"]


def test_matching_remainder_and_divergent_contribution_narrows_effect_without_claiming_cause(tmp_path):
    effect=pair(tmp_path,"AO",[11],[12],referenceBefore=image(tmp_path,"before-r",[10]),renderedBefore=image(tmp_path,"before-u",[10]))
    full=pair(tmp_path,"final",[11],[12]);full["category"]="full-frame"
    baseline=pair(tmp_path,"baseline",[10],[10]);baseline.update(category="full-frame",referenceOrigin="rdc-controlled-isolation")
    report=compare_layers([effect,full,baseline])
    isolation=dict(effect="AO",fullOutput="final",bypassOutput="baseline",effectOutput="AO",
        sameInputs=True,sameEnabledRemainder=True,singleEffectIntervention=True,restored=True)
    result=localize_difference(report,isolation=[isolation])
    observed=result["isolation"][0]
    assert observed["observedConsistentRemainderAndEffectDifference"] and observed["remainderScope"]=="full-frame-remainder"
    assert not observed["rootCauseProven"] and result["rereadImageBytes"] is False
    report["layers"][2]["referenceOrigin"]="retained-unity-kernel"
    assert not localize_difference(report,isolation=[isolation])["isolation"][0]["observedConsistentRemainderAndEffectDifference"]


def test_dependency_frontier_does_not_mistake_report_order_or_propagation_for_root_cause(tmp_path):
    report=compare_layers([pair(tmp_path,"final",[0],[1]),pair(tmp_path,"shadow",[0],[1],module="ShadowSample"),
                           pair(tmp_path,"depth",[0],[0]),pair(tmp_path,"otherShadow",[0],[1],module="ShadowSample")])
    graph=[dict(name="final",dependsOn=["shadow"]),dict(name="shadow",dependsOn=["depth"]),
           dict(name="otherShadow",dependsOn=["depth"])]
    result=localize_difference(report,graph)
    assert [row["name"] for row in result["divergenceFrontier"]]==["shadow","otherShadow"]
    assert result["downstreamDifferences"][0]["name"]=="final"
    assert result["sharedRepairCandidates"]=={"ShadowSample":["shadow","otherShadow"]}
    assert not result["rootCauseProven"]


def test_actual_binding_fields_are_filtered_toward_the_failing_consumer(tmp_path):
    report=compare_layers([pair(tmp_path,"shadow",[0],[1])])
    inputs=[dict(name="surface",kind="texture",reference=dict(mip=0,sampler="Repeat"),rendered=dict(mip=0,sampler="Repeat")),
        dict(name="shadowInput",kind="cbuffer",consumers=["shadow"],reference=dict(matrix=[1,0,0,1],bias=.001),
             rendered=dict(matrix=[0,1,0,1],bias=.001))]
    result=localize_difference(report,inputs=inputs)
    first=result["inputDifferences"][0]
    assert first["name"]=="shadowInput" and first["differenceCount"]==2
    assert first["differences"][0]["path"]=="shadowInput.matrix[0]"


def test_cycle_and_unrestored_bypass_remain_findings(tmp_path):
    report=compare_layers([pair(tmp_path,"A",[0],[1]),pair(tmp_path,"B",[0],[1])])
    result=localize_difference(report,[dict(name="A",dependsOn=["B"]),dict(name="B",dependsOn=["A"])],
        isolation=[dict(effect="AO")])
    assert {row["code"] for row in result["findings"]}>={"current-frame-dependency-cycle","diagnostic-bypass-restore-unproven","effect-attribution-unproven"}


def test_incomplete_before_pair_and_mismatching_layouts_are_not_silently_resized(tmp_path):
    layer=pair(tmp_path,"AO",[0],[1],referenceBefore=image(tmp_path,"before-r",[0]))
    with pytest.raises(ValueError,match="both referenceBefore"):
        compare_layers([layer])
    layer["renderedBefore"]=image(tmp_path,"before-u",[0,0])
    with pytest.raises(ValueError,match="identical explicit pixel"):
        compare_layers([layer])


def test_matching_small_roi_is_not_whole_frame_remainder_proof(tmp_path):
    full=pair(tmp_path,"final",[1,1],[1,2]);full["category"]="full-frame"
    baseline=pair(tmp_path,"remainder",[1,1],[1,2],roi=[0,0,1,1]);baseline["category"]="full-frame"
    effect=pair(tmp_path,"AO",[0,0],[0,1])
    result=localize_difference(compare_layers([full,baseline,effect]),isolation=[dict(effect="AO",
        fullOutput="final",bypassOutput="remainder",effectOutput="AO",sameInputs=True,
        sameEnabledRemainder=True,singleEffectIntervention=True,restored=True)])
    assert result["isolation"][0]["remainderScope"]=="local-boundary"


def test_localizer_reuses_reports_after_images_are_archived_elsewhere(tmp_path):
    report=compare_layers([pair(tmp_path,"AO",[0],[1])])
    for row in report["layers"]:
        for path in row["evidencePaths"]:Path(path).unlink()
    result=localize_difference(copy.deepcopy(report))
    assert result["divergenceFrontier"][0]["name"]=="AO"
    assert result["rereadImageBytes"] is False and result["diagnosticsOnly"]
