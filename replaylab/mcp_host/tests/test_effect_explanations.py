import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from src import server
from src.effect_experiments import edited_source, verified_comparison
from src.effect_experiments import baseline_control_scope


def test_local_control_never_accepts_a_failed_target_region_or_unstable_repeat():
    control=dict(accepted=False,effectObservation=dict(conclusive=True,metrics=[dict(accepted=True)]))
    assert baseline_control_scope(control,[.4,.7,.6,.99])=='requested-region'
    assert not baseline_control_scope(control,[0.,0.,1.,1.])
    control['effectObservation']['metrics'][0]['accepted']=False
    assert not baseline_control_scope(control,[.4,.7,.6,.99])
    control['effectObservation'].update(conclusive=False,metrics=[dict(accepted=True)])
    assert not baseline_control_scope(control,[.4,.7,.6,.99])
from src.visual_replies import publish, _json
from test_shader_numeric_validation import FakeBridge, validation, descriptor, native_blob, packed


def test_paired_effect_exports_its_own_bytes_without_another_replay(tmp_path):
    from test_shader_numeric_validation import install_pair_result
    bridge=FakeBridge(existing=True)
    pair=install_pair_result(bridge,candidate=.25,repeat=.75)
    pair.outputs[0].baseline=packed('f',.75)
    folder=tmp_path/('effect-'+'a'*32);folder.mkdir()
    bridge._event_texture_evidence_is_redirected=lambda p:False
    bridge.params['effect_observation']=dict(eventId=50,attachmentIndex=0,roi=[0.,0.,1.,1.],artifactDir=str(folder))
    result=validation.compare_candidates(bridge,bridge.params)
    effect=result['effectObservation']
    assert effect['conclusive'] and result['replayRestored']
    assert result['replayMethod']=='same-input-paired-draw' and bridge.ctrl.observe_count==0
    assert effect['replacementScope']=='single-draw-shader'
    assert len(effect['images'])==2
    colors=[]
    for item in effect['images']:
        assert hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()==item['sha256']
        with Image.open(item['path'])as image:colors.append(image.getpixel((0,0)))
    assert colors==[(225,225,225),(137,137,137)]
    assert bridge.ctrl.mapping==999 and bridge.ctrl.event_id==17 and not bridge.ctrl.targets


def test_region_stability_is_distinct_from_unrelated_output_pixels():
    from test_shader_numeric_validation import install_pair_result
    bridge=FakeBridge(existing=True);pair=install_pair_result(bridge,candidate=.5,repeat=1.)
    item=pair.outputs[0];item.width=2
    item.baseline=packed('f',1.,1.);item.candidate=packed('f',.5,1.);item.baselineRepeat=packed('f',1.,2.)
    bridge.params['effect_observation']=dict(eventId=50,attachmentIndex=0,roi=[0.,0.,.5,1.])
    result=validation.compare_candidates(bridge,bridge.params)
    assert not result['accepted']and not result['baselineStability']['withinTolerance']
    assert result['effectObservation']['conclusive']and result['effectObservation']['stabilityScope']=='requested-region'
    bridge.params['effect_observation']['roi']=[0.,0.,1.,1.]
    result=validation.compare_candidates(bridge,bridge.params)
    assert not result['effectObservation']['conclusive']


def test_packed_snapshot_png_mapping_keeps_distinct_r11g11b10_channels():
    import struct
    desc=dict(width=2,height=1,componentCount=3,componentByteWidth=0,componentType='Float',
              formatType='R11G11B10',format='R11G11B10_FLOAT')
    raw=struct.pack('<II',15<<6,(15<<5)<<22)
    width,height,rgb=validation._snapshot_rgb(desc,raw)
    assert (width,height,rgb)==(2,1,bytes([255,0,0,0,0,255]))


def test_display_transfer_encodes_linear_once_and_preserves_display_storage():
    import struct
    desc=dict(width=1,height=1,componentCount=3,componentByteWidth=4,componentType='Float',
              formatType='Regular',format='RGB32_FLOAT')
    raw=struct.pack('<fff',.5,.0031308,1.)
    assert validation._snapshot_rgb(desc,raw)[2]==bytes([188,10,255])
    assert validation._snapshot_rgb(desc,raw,transfer='identity')[2]==bytes([128,1,255])
    stored=dict(desc,componentByteWidth=1,componentType='UNormSRGB')
    assert validation._snapshot_rgb(stored,bytes([128,64,255]),transfer='linear-to-srgb')[2]==bytes([128,64,255])
    final=dict(stored,componentType='UNorm')
    assert validation._snapshot_rgb(final,bytes([128,64,255]))[2]==bytes([128,64,255])


def test_local_explanation_cannot_touch_frame_publication(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('Local explanation reached global state')
    monkeypatch.setattr(server,'_frame_render_flow_record',forbidden)
    result=json.loads(server.publish_frame_render_flow('any',[],title='Local rim light'))
    assert 'publish_visual_reply' in result['error'] and 'No frame diagram was changed' in result['error']


def test_effect_native_uses_same_later_output_and_restores_prior_mapping():
    bridge=FakeBridge(existing=True)
    bridge.params['effect_observation']=dict(eventId=80,attachmentIndex=0,roi=[0.,0.,1.,1.])
    bridge.ctrl.output_values[202]=packed('f',2.)
    result=validation.compare_candidates(bridge,bridge.params)
    assert result.get('effectObservation',{}).get('conclusive') and result['replayRestored'],result
    assert result['effectObservation']['metrics'][0]['maximumAbsoluteError']==pytest.approx(1.)
    assert [c[1]for c in bridge.ctrl.calls if c[0]=='event'].count(80)==3
    assert bridge.ctrl.mapping==999 and bridge.ctrl.event_id==17 and not bridge.ctrl.targets


def test_vertex_effect_trial_has_same_restored_output_contract():
    bridge=FakeBridge(existing=True)
    bridge._vertex_geometry_scope=lambda *args:dict(complete=True,matches=[])
    bridge._available_shader_stages=lambda stages:[('vertex','vertex')]
    pipe=bridge.ctrl.GetPipelineState()
    pipe.GetShaderReflection=lambda stage:None
    bridge.ctrl.GetPipelineState=lambda:pipe
    bridge.params['stage']='vertex'
    bridge.params['effect_observation']=dict(eventId=80,attachmentIndex=0,roi=[0.,0.,1.,1.])
    result=validation.compare_candidates(bridge,bridge.params)
    assert result.get('effectObservation',{}).get('conclusive') and result['replayRestored'],result
    assert bridge.ctrl.mapping==999 and bridge.ctrl.event_id==17 and not bridge.ctrl.targets


def test_vertex_trial_switches_and_restores_a_duplicate_geometry_shader():
    from types import SimpleNamespace as NS

    bridge = FakeBridge(existing=True)
    bridge._available_shader_stages = lambda stages: [('vertex', 'vertex')]
    bridge.params['stage'] = 'vertex'
    bridge.params['effect_observation'] = dict(eventId=80, attachmentIndex=0, roi=[0., 0., 1., 1.])
    original_bytes = native_blob('captured')
    digest = hashlib.sha256(original_bytes).hexdigest()
    bridge.event['expectedOriginalSha256'] = digest
    bridge._vertex_geometry_scope = lambda *args: dict(complete=True, matches=[dict(
        eventId=43, relation='duplicated-geometry', shaderResourceId=8,
        shaderRawSha256=digest, entryPoint='MainPS', depthTarget=97, colorTargets=[])])
    bridge._select_event_for_read = lambda ctrl, event: setattr(ctrl, 'event_id', event)
    pipe = bridge.ctrl.GetPipelineState()
    pipe.GetShader = lambda stage: 8
    pipe.GetShaderReflection = lambda stage: NS(raw=original_bytes, readWriteResources=[])
    bridge.ctrl.GetPipelineState = lambda: pipe
    peer_mapping = {}
    events = []
    replace, remove = bridge._replace_shared_shader_resource, bridge._remove_shared_shader_resource
    read = bridge.ctrl.GetTextureData

    def apply_peer(ctrl, api, original, target):
        if original == 8:
            peer_mapping[original] = target
        else:
            replace(ctrl, api, original, target)

    def restore_peer(ctrl, api, original):
        if original == 8:
            peer_mapping.pop(original, None)
        else:
            remove(ctrl, api, original)

    def observe(resource, subresource):
        events.append((bridge.ctrl.mapping, peer_mapping.get(8)))
        return read(resource, subresource)

    bridge._replace_shared_shader_resource = apply_peer
    bridge._remove_shared_shader_resource = restore_peer
    bridge.ctrl.GetTextureData = observe
    result = validation.compare_candidates(bridge, bridge.params)
    assert result.get('effectObservation', {}).get('conclusive'), result
    assert result['effectObservation']['coordinatedShaderCount'] == 2
    assert len(events) == 3 and all(main == peer for main, peer in events)
    assert events[0] == events[2] and events[0] != events[1]
    assert not peer_mapping and bridge.ctrl.mapping == 999 and not bridge.ctrl.targets


def test_compute_effect_requires_a_separate_storage_observation_route():
    bridge=FakeBridge();bridge.params['stage']='compute'
    bridge.params['effect_observation']=dict(eventId=80,roi=[0.,0.,1.,1.])
    with pytest.raises(ValueError,match='vertex/fragment'):
        validation.compare_candidates(bridge,bridge.params)


def test_effect_failure_also_restores_prior_mapping():
    bridge=FakeBridge(existing=True);bridge.ctrl.observe_failure=2
    bridge.params['effect_observation']=dict(eventId=80,attachmentIndex=0,roi=[0.,0.,1.,1.])
    result=validation.compare_candidates(bridge,bridge.params)
    assert result['replayRestored'] and result['sampleCount']==0
    assert bridge.ctrl.mapping==999 and bridge.ctrl.event_id==17 and not bridge.ctrl.targets


def test_later_output_refreshes_binding_instead_of_using_analysis_anchor_cache():
    from copy import copy
    from types import SimpleNamespace as NS
    bridge=FakeBridge()
    later=copy(bridge.ctrl.texture);later.resourceId=88
    output=copy(bridge.ctrl.output);output.resource=88
    bridge.ctrl.GetTextures=lambda:[bridge.ctrl.texture,later]
    bridge.ctrl.GetPipelineState=lambda:NS(GetOutputTargets=lambda:[output if bridge.ctrl.event_id==80 else bridge.ctrl.output],
                                          GetDepthTarget=lambda:bridge.ctrl.depth)
    # This fast replay API intentionally leaves the public pipeline cache stale.
    bridge.ctrl.ReplayFrameEventForAnalysis=lambda event:bridge.ctrl.calls.append(('analysis-only',event))
    bridge.params['effect_observation']=dict(eventId=80,attachmentIndex=0,roi=[0.,0.,1.,1.])
    result=validation.compare_candidates(bridge,bridge.params)
    assert result['metrics']['outputs'][0]['output']['resourceId']==88
    assert not any(call[0]=='analysis-only'for call in bridge.ctrl.calls)


def test_effect_roi_crops_stored_pixels_and_matches_display_flip():
    desc=dict(descriptor('UInt',width=1,pixels=2),height=2)
    first=validation._effect_roi([(desc,bytes([1,2,3,4]))],[0.,0.,.5,.5])
    flipped=validation._effect_roi([(desc,bytes([1,2,3,4]))],[0.,0.,.5,.5],True)
    assert first[0][1]==bytes([1]) and flipped[0][1]==bytes([3])


def test_present_uses_actual_destination_and_rejects_inherited_compute_targets():
    from copy import copy
    from types import SimpleNamespace as NS
    bridge=FakeBridge();dest=copy(bridge.ctrl.texture);dest.resourceId=99
    bridge.ctrl.GetTextures=lambda:[bridge.ctrl.texture,dest]
    bridge.ctrl.GetRootActions=lambda:[NS(flags=256,copyDestination=99,copyDestinationSubresource=NS(mip=0,slice=0))]
    bridge._find_action_by_event=lambda actions,event:actions[0]
    bridge.rd.ActionFlags=NS(Present=256,Drawcall=2)
    result=validation._effect_outputs(bridge,bridge.ctrl,100,{'attachmentIndex':0})
    assert result[0][0]['resourceId']==99
    assert result[0][0]['presentationTarget'] is True
    assert result[0][0]['capturedPresent'] is True
    bridge.ctrl.GetRootActions=lambda:[NS(flags=256,copyDestination=99,customName="End of Capture")]
    assert not validation._effect_outputs(bridge,bridge.ctrl,100,{})[0][0].get('capturedPresent')
    bridge.rd.TextureCategory = NS(SwapBuffer=16)
    bridge.ctrl.GetRootActions=lambda:[NS(flags=2)]
    assert not validation._effect_outputs(bridge,bridge.ctrl,100,{})[0][0].get('presentationTarget')
    bridge.ctrl.texture.creationFlags = 16
    assert validation._effect_outputs(bridge,bridge.ctrl,100,{})[0][0]['presentationTarget'] is True
    bridge.ctrl.GetRootActions=lambda:[NS(flags=4)]
    with pytest.raises(ValueError,match='inherited RTVs'):validation._effect_outputs(bridge,bridge.ctrl,100,{})


def test_presentation_resolution_corrects_early_hint_and_requires_exact_choice_for_multiple_outputs():
    from types import SimpleNamespace as NS

    bridge = FakeBridge()
    bridge.rd.ActionFlags = NS(Present=256, Drawcall=2)
    bridge._background_replay_checkpoint = lambda: None
    first = NS(eventId=40, flags=256, copyDestination=7)
    early = NS(eventId=75, flags=2)
    present = NS(eventId=91, flags=256, copyDestination=41)
    marker = NS(eventId=70, flags=0, children=[early, present])
    capture_end = NS(eventId=130, flags=256, copyDestination=42, customName="End of Capture")
    actions = [first, marker, NS(eventId=96, flags=256, copyDestination=0), capture_end]
    bridge.ctrl.GetRootActions = lambda: actions
    params = dict(bridge.params, source_event_id=50, observation_event_id=75)

    assert validation.resolve_effect_presentation(bridge, params)["target"] == {"eventId": 91, "resourceId": "41"}
    actions.insert(-1, NS(eventId=98, flags=256, copyDestination=42))
    multiple = validation.resolve_effect_presentation(bridge, params)
    assert multiple["target"] is None
    assert multiple["candidates"] == [{"eventId": 91, "resourceId": "41"}, {"eventId": 98, "resourceId": "42"}]
    assert validation.resolve_effect_presentation(
        bridge, dict(params, observation_event_id=98))["target"] == multiple["candidates"][1]
    actions[:] = [first, capture_end]
    assert validation.resolve_effect_presentation(bridge, params)["target"] is None
    assert not bridge.ctrl.calls


@pytest.mark.parametrize("mismatch", [False, True])
def test_effect_appearance_replays_and_exports_the_corrected_present(tmp_path, monkeypatch, mismatch):
    from types import SimpleNamespace as NS
    from src.effect_experiments import run

    source = tmp_path / "shader.hlsl"
    source.write_bytes(b"return original;")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    capture_id, session = "a" * 64, "b" * 32
    status = dict(stage="fragment", source={"path": str(source)}, capture={"captureId": capture_id},
                  shader={"resourceId": 7}, baseline={"rawSha256": "c" * 64})
    calls = []

    def ipc_call(method, params, **kwargs):
        calls.append(method)
        if method == "resolve_shader_effect_presentation":
            assert params["observation_event_id"] == 75 and params["source_event_id"] == 50
            return dict(captureId=capture_id, captureSessionId=session, sourceEventId=50,
                        target=dict(eventId=91, resourceId="41"))
        assert method == "compare_shader_organization_candidates"
        options = params["effect_observation"]
        assert options["eventId"] == 91 and options["attachmentIndex"] == 0
        assert options["displayTransfer"] == "auto"
        images = []
        if options["artifactDir"]:
            for name, color in (("before", "red"), ("after", "blue")):
                path = Path(options["artifactDir"]) / (name + ".png")
                Image.new("RGB", (2, 2), color).save(path)
                images.append(dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                   resourceId="99" if mismatch and name == "after" else "41",
                                   eventId=91, displayTransfer="identity",
                                   presentationTarget=True, capturedPresent=True))
        return dict(eventId=50, captureSessionId=session, replayRestored=True, accepted=True,
                    effectObservation=dict(eventId=91, conclusive=True, images=images, metrics=[
                        dict(byteExact=False, accepted=False, output=dict(resourceId=41, capturedPresent=True))]))

    service = NS(_workspace_root=Path, _load_manifest=lambda _: None, status=lambda _: status,
                 compile_diagnostic_candidate=lambda *args: dict(compiled=True, baseline={}, comparisonParams={},
                                                                 candidateBlobPath="candidate", rawSha256=digest))
    monkeypatch.setattr(server, "ipc", NS(call=ipc_call))
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "_ensure_workspace_capture_catalog_identity",
                        lambda *a, **k: {"captureSessionId": session})
    monkeypatch.setattr(server, "_shader_organization_anchor_event", lambda _: 50)
    monkeypatch.setattr(server, "_EVENT_TEXTURE_EVIDENCE_ROOT", tmp_path / "evidence")

    def experiment():
        return run(server, str(tmp_path), digest, [dict(old_text="original", new_text="changed")],
                   75, attachment_index=2, observation_scope="appearance", display_transfer="linear-to-srgb")

    if mismatch:
        with pytest.raises(ValueError, match="Exported images did not match"):
            experiment()
        assert not list((tmp_path / "evidence").rglob("*.json"))
    else:
        result = experiment()
        assert result["ok"] and result["presentationSelection"] == dict(
            requestedEventId=75, eventId=91, resourceId="41", retargeted=True)
        receipt = json.loads(Path(result["receiptPath"]).read_bytes())
        assert receipt["presentationSelection"] == result["presentationSelection"]
        assert [image["eventId"] for image in receipt["observation"]["effectObservation"]["images"]] == [91, 91]
    assert calls == ["resolve_shader_effect_presentation", "compare_shader_organization_candidates",
                     "compare_shader_organization_candidates"]


def test_present_ablation_bypasses_checkpoints_and_viewer_replays():
    from types import SimpleNamespace as NS
    bridge=FakeBridge(existing=True)
    bridge.rd.ActionFlags=NS(Present=256,Drawcall=2)
    bridge.ctrl.GetRootActions=lambda:[NS(flags=256,copyDestination=41,copyDestinationSubresource=NS(mip=0,slice=0))]
    bridge._find_action_by_event=lambda actions,event:actions[0]
    bridge.ctrl.ReplayFrameEventForAnalysis=lambda event:bridge.ctrl.calls.append(('analysis',event))
    bridge.params['effect_observation']=dict(eventId=100,attachmentIndex=0,roi=[0.,0.,1.,1.])
    result=validation.compare_candidates(bridge,bridge.params)
    assert result['replayRestored'] and result['effectObservation']['conclusive']
    assert [call for call in bridge.ctrl.calls if call==('analysis',100)]==[('analysis',100)]*3


@pytest.mark.parametrize('edits',[
    [],[dict(old_text='missing',new_text='0')],[dict(old_text='a',new_text='a')],
    [dict(old_text='x',new_text='0',offset=4)]])
def test_effect_edits_reject_ambiguous_or_unbounded_trials(edits):
    with pytest.raises(ValueError):edited_source('a a',edits)


def test_temporary_edits_normalize_windows_newlines():
    assert edited_source('a\r\nb\r\n',[dict(old_text='a\nb',new_text='c\nd')])=='c\nd\n'


def test_model_schema_describes_nested_cards_and_exact_edit_keys():
    from mcp.server.fastmcp.tools.base import Tool
    effect=Tool.from_function(server.run_shader_effect_experiment,structured_output=False).parameters
    visual=Tool.from_function(server.publish_visual_reply).parameters
    assert set(effect['$defs']['ExactSourceEdit']['properties'])=={'old_text','new_text'}
    assert effect['properties']['observation_event_id']['default'] == 0
    assert effect['properties']['observation_scope']['default'] == 'auto'
    assert visual['$defs']['Card']['additionalProperties']is False
    assert 'attachmentId'not in visual['$defs']['ImageSource']['properties']


def test_experiment_comparison_rejects_stale_or_wrong_image_pair(tmp_path):
    receipt=dict(schema='bread-shader-effect-experiment/v1',verified=True,captureSessionId='session',
        replayRestored=True,sourcePreserved=True,images=[{'sha256':'a'*64},{'sha256':'b'*64}],
        outcome='changed',roi=[0.,0.,1.,1.],observationEventId=80,replacementScope='shared-shader-resource',
        observation={'effectObservation':{'metrics':[]}})
    raw=_json(receipt);path=tmp_path/(hashlib.sha256(raw).hexdigest()+'.json');path.write_bytes(raw)
    assert verified_comparison(path,{'captureSessionId':'session'},dict(sourceSha256='a'*64),dict(sourceSha256='b'*64))['verified']
    with pytest.raises(ValueError):verified_comparison(path,{'captureSessionId':'other'},dict(sourceSha256='a'*64),dict(sourceSha256='b'*64))
    with pytest.raises(ValueError):verified_comparison(path,{'captureSessionId':'session'},dict(sourceSha256='b'*64),dict(sourceSha256='a'*64))
    path.write_bytes(raw+b' ')
    with pytest.raises(ValueError):verified_comparison(path,{'captureSessionId':'session'},{},{})


def test_layer_comparisons_share_final_output_and_keep_intermediate_scope_explicit(tmp_path):
    before, after = tmp_path / 'before.png', tmp_path / 'after.png'
    Image.new('RGB', (4, 4), (30, 60, 90)).save(before)
    Image.new('RGB', (4, 4), (40, 60, 90)).save(after)
    images = [{'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in (before, after)]

    def final_receipt(event_id, transfer, captured_present=True):
        native = dict(eventId=event_id, resourceId='18', displayTransfer=transfer, presentationTarget=True)
        if captured_present:
            native['capturedPresent'] = True
        data = _json(dict(schema='bread-shader-effect-experiment/v1', verified=True, captureSessionId='session',
            replayRestored=True, sourcePreserved=True, images=images, outcome='changed', roi=[0., 0., 1., 1.],
            observationEventId=event_id, replacementScope='shared-shader-resource',
            sourceEventId=31, workspacePath=str(tmp_path / 'shader'), stage='fragment',
            display=dict(blackPoint=0., whitePoint=1., flipY=False),
            observation=dict(effectObservation=dict(metrics=[], images=[native, native]))))
        path = tmp_path / (hashlib.sha256(data).hexdigest() + '.json')
        path.write_bytes(data)
        return str(path)

    pair = dict(type='compare', image={'path': str(before)}, secondImage={'path': str(after)})
    anchor = dict(pair, title='First layer', experimentReceipt=final_receipt(90, 'identity'))
    stage = dict(pair, title='Intermediate RT', image={'eventId': 31}, secondImage={'eventId': 32})

    def publish_pair(other):
        return publish('Layers', [anchor, other], {'capture': {'captureSessionId': 'session'}},
                       capture_image=lambda s: {'path': str(before if s.eventId == 31 else after)},
                       capture_mesh=lambda _: None, authorize_file=Path, root=tmp_path / 'visuals')

    with pytest.raises(ValueError, match='Intermediate RT.*same final presentation output EID 90'):
        publish_pair(stage)
    assert publish_pair(dict(stage, comparisonScope='stage'))['cardCount'] == 2
    for eid, transfer in ((91, 'identity'), (90, 'linear-to-srgb')):
        with pytest.raises(ValueError, match='same final presentation output EID 90'):
            publish_pair(dict(pair, title='Second layer', experimentReceipt=final_receipt(eid, transfer)))
    with pytest.raises(ValueError, match="observation_scope='appearance'"):
        publish_pair(dict(pair, title='Unverified swapchain draw', comparisonScope='appearance',
                          experimentReceipt=final_receipt(90, 'identity', captured_present=False)))
    result = publish_pair(dict(pair, title='Second layer', comparisonScope='appearance',
                               experimentReceipt=final_receipt(90, 'identity')))
    card = json.loads(Path(result['path']).read_bytes())['cards'][1]
    assert card['experiment']['presentationBasis']['transfer'] == 'identity'
    with Image.open(tmp_path / 'visuals/blobs' / card['image']['file']) as saved:
        assert saved.getpixel((0, 0)) == (30, 60, 90, 255)

    # A marked effect can no longer bypass review by only publishing one
    # slider while linking to additional layers without any results or code.
    from src.task_report import TaskReportStore
    store = TaskReportStore('marked-layers', tmp_path / 'tasks')
    store.begin_task_review('a' * 64, marked_render_target=True)
    anchor['links'] = [dict(label='Another layer', url='rdoc://event/32'),
                       dict(label='Output', url='rdoc://event/90')]
    options = dict(capture_image=lambda _: None, capture_mesh=lambda _: None, authorize_file=Path,
                   root=tmp_path / 'visuals', read_code=lambda spec: dict(text='float alpha = mask;', workspacePath=spec.workspacePath))
    context = {'capture': {'captureSessionId': 'session'}}
    criteria = [dict(requirement='Explain the marked layers', status='satisfied', evidence='Compared the marked region')]

    def review(cards):
        publication = publish('Layers', cards, context, **options)
        store.register_reply_visual(publication['id'], publication['presentation'])
        return store.record_task_review('complete', criteria, [], [], visual_review='Reviewed the final bundle')

    with pytest.raises(ValueError, match=r'marked effect explanation.*"missingLayerViews":\[32\]'):
        review([anchor])
    diagram = dict(type='diagram', title='Composition', nodes=[dict(id='a', text='First layer', eventId=31),
        dict(id='b', text='Second layer', eventId=32, image={'path': str(after)})], edges=[['a', 'b']])
    with pytest.raises(ValueError, match='missing organized Shader code'):
        review([anchor, diagram])
    diagram['nodes'][0]['codeBlocks'] = [dict(workspacePath=str(tmp_path / 'shader'), expectedSourceSha256='b' * 64)]
    review([anchor, diagram])
    assert store.task_review_state()['fresh']


def test_marked_composite_audit_covers_unlinked_neighbours_and_bound_texture(tmp_path):
    from src.task_report import TaskReportStore
    from src.visual_replies import EffectDrawExclusion, neighbouring_composite_draws, publish, _json

    def draw(event, indices):
        return dict(eventId=event, numIndices=indices, numInstances=1, outputs=[5],
                    flags=12, markerPath=['Transparent'])

    actions = [draw(eid, indices) for eid, indices in [(10, 6), (20, 24), (30, 24), (40, 24), (50, 12)]]
    assert neighbouring_composite_draws(actions, 30) == [20, 40]
    before = tmp_path / 'before.png'
    Image.new('RGB', (8, 8), 'black').save(before)

    def pair(eid, color, baseline=before):
        after = tmp_path / f'after-{eid}.png'
        Image.new('RGB', (8, 8), color).save(after)
        paths = [baseline, after]
        workspace = str(tmp_path / f'shader-{eid}')
        images = [{'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]
        native = dict(eventId=90, resourceId='19', displayTransfer='identity',
                      presentationTarget=True, capturedPresent=True)
        payload = _json(dict(schema='bread-shader-effect-experiment/v1', verified=True,
            captureSessionId='session', replayRestored=True, sourcePreserved=True, images=images,
            outcome='changed', roi=[0., 0., 1., 1.], observationEventId=90, sourceEventId=eid,
            replacementScope='shared-shader-resource', workspacePath=workspace, stage='fragment',
            display=dict(blackPoint=0., whitePoint=1., flipY=False),
            observation=dict(effectObservation=dict(metrics=[], images=[native, native]))))
        receipt = tmp_path / (hashlib.sha256(payload).hexdigest()+'.json')
        receipt.write_bytes(payload)
        return dict(type='compare', title=f'Layer {eid}', comparisonScope='appearance',
                    image={'path': str(baseline)}, secondImage={'path': str(after)}, experimentReceipt=str(receipt))

    pairs = [pair(eid, color) for eid, color in ((30, 'blue'), (40, 'red'), (20, 'green'))]
    nodes = [dict(id=f'layer{eid}', text=f'Draw {eid}', eventId=eid, image={'eventId': eid},
                  codeBlocks=[dict(workspacePath=str(tmp_path / f'shader-{eid}'), expectedSourceSha256='b'*64)])
             for eid in (20, 30, 40)]
    graph = dict(type='diagram', title='Composite', nodes=[*nodes,
                 dict(id='final', text='Composite', image=dict(eventId=90, attachmentKind='present'))],
                 edges=[[node['id'], 'final'] for node in nodes])
    options = dict(root=tmp_path/'visuals', authorize_file=Path, capture_mesh=lambda _: None,
                   capture_image=lambda source, **_: dict(path=str(before), resourceId='280'),
                   read_code=lambda block: dict(text='float a = 1;', workspacePath=block.workspacePath),
                   effect_neighbours=lambda eid: neighbouring_composite_draws(actions, eid),
                   effect_inputs=lambda eid: ['280'] if eid == 20 else [])
    store = TaskReportStore('full-effect', tmp_path/'tasks')
    store.begin_task_review('a'*64, marked_render_target=True)

    def review(cards, visual_checks=None, reviewed_answer=None, effect_exclusions=None):
        result = publish('Effect', cards, {'capture': {'captureSessionId': 'session'}},
                         effect_exclusions=effect_exclusions, **options)
        store.register_reply_visual(result['id'], result['presentation'])
        return store.record_task_review('complete', [dict(requirement='All layers', status='satisfied',
            evidence='Inspected each contribution')], [], [], visual_review='Inspected complete bundle',
            visual_checks=visual_checks, reviewed_answer=reviewed_answer)

    with pytest.raises(ValueError, match=r'"missingFinalLayerComparisons":\[20,40\]'):
        review([pairs[0], graph])
    exclusion = EffectDrawExclusion(eventId=40, evidence='Different shader and additional sampled textures',
                                   experimentReceipt=pairs[1]['experimentReceipt'])
    with pytest.raises(ValueError, match='EID 40 cannot be excluded'):
        review([pairs[0], graph], effect_exclusions=[exclusion])
    with pytest.raises(ValueError, match=r'"missingInputTextures":\[\{"eventId":20,"resourceId":"280"\}\]'):
        review([*pairs, graph])
    nodes[0]['secondImage'] = dict(eventId=20, attachmentKind='srv')
    mismatched_baseline = tmp_path / 'different-baseline.png'
    Image.new('RGB', (8, 8), (2, 0, 0)).save(mismatched_baseline)
    with pytest.raises(ValueError, match=r'"combinedBaselineMismatch":true'):
        review([pairs[0], pairs[1], pair(20, 'green', mismatched_baseline), graph])
    with pytest.raises(ValueError, match='visual_checks'):
        review([*pairs, graph])
    checks = {key: 'Checked current evidence' for key in ('overview', 'contributions', 'display',
                                                       'flow', 'source', 'links', 'inactive')}
    answer = dict(overview='Three layers form the visible effect.',
                  steps=[dict(text='Describe the first layer.',events=[20]),
                         dict(text='Describe the second layer.',events=[30])],
                  combined='Each layer blends into the displayed result.')
    with pytest.raises(ValueError, match=r'omits these contributor EIDs: \[40\]'):
        review([*pairs, graph], checks, answer)
    answer['steps'].append(dict(text='Describe the third layer.',events=[40]))
    receipt = review([*pairs, graph], checks, answer)
    assert receipt['visualReview']['checks'] == checks
    assert '3. Describe the third layer.' in receipt['visualReview']['answer']
    assert store.task_review_state()['fresh']


def test_verified_calculation_diagram_cannot_complete_with_missing_shader_code(tmp_path):
    from src.task_report import TaskReportStore

    before, after = tmp_path / "before.png", tmp_path / "after.png"
    Image.new("RGB", (8, 8), "blue").save(before)
    Image.new("RGB", (8, 8), "green").save(after)
    workspaces = [tmp_path / "rain-a", tmp_path / "rain-b"]
    cards = []
    for eid, workspace in zip((21, 22), workspaces):
        receipt = dict(schema="bread-shader-effect-experiment/v1", verified=True,
                       captureSessionId="session", replayRestored=True, sourcePreserved=True,
                       images=[{"sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in (before, after)],
                       outcome="changed", roi=[0., 0., 1., 1.], observationEventId=80,
                       sourceEventId=eid, stage="fragment", workspacePath=str(workspace),
                       replacementScope="shared-shader-resource",
                       observation={"effectObservation": {"metrics": []}})
        raw = _json(receipt)
        path = tmp_path / (hashlib.sha256(raw).hexdigest() + ".json")
        path.write_bytes(raw)
        cards.append(dict(type="compare", title=f"Layer {eid}", image={"path": str(before)},
                          secondImage={"path": str(after)}, experimentReceipt=str(path)))
    cards.append(dict(type="diagram", title="Shader calculations", nodes=[
        dict(id="layers", text="Combine layers", calculation="surface = mix(a, b, weight);")
    ]))
    context = {"capture": {"captureSessionId": "session"}}
    options = dict(capture_image=lambda _: None, capture_mesh=lambda _: None,
                   authorize_file=Path, root=tmp_path / "visuals",
                   read_code=lambda block: dict(text="float layer = weight;", workspacePath=block.workspacePath))
    store = TaskReportStore("visual-code", tmp_path / "tasks")
    store.begin_task_review("a" * 64)
    criteria = [dict(requirement="Explain the rain layers", status="satisfied", evidence="Reviewed the result")]

    def publish_for_review():
        publication = publish("Effect", cards, context, **options)
        store.register_reply_visual(publication["id"], publication["presentation"])
        return publication["presentation"]["shaderCodeGaps"]

    assert [gap["eventId"] for gap in publish_for_review()] == [21, 22]
    with pytest.raises(ValueError, match="missing organized Shader code"):
        store.record_task_review("complete", criteria, [], [], visual_review="Images are visible")
    for index, workspace in enumerate(workspaces):
        cards[-1]["nodes"][0].setdefault("codeBlocks", []).append(
            dict(workspacePath=str(workspace), expectedSourceSha256="b" * 64, startLine=1, endLine=1))
        assert [gap["eventId"] for gap in publish_for_review()] == [21, 22][index + 1:]
    store.record_task_review("complete", criteria, [], [], visual_review="All tested shaders have excerpts")
    assert store.task_review_state()["fresh"]


def test_thumbnail_flow_and_saved_source_card_keep_evidence(tmp_path):
    image=tmp_path/'node.png';Image.new('RGB',(8,8),'blue').save(image)
    result=publish('Effect',[
        {'type':'diagram','title':'Flow','nodes':[{'id':'a','text':'Before','image':{'path':str(image)},'calculation':'x * rim'},
                                                 {'id':'b','text':'After','eventId':80}],
         'edges':[['a','b']],'edgeLabels':['Compositing']},
        {'type':'code','title':'Actual source','workspacePath':'managed','expectedSourceSha256':'a'*64,'startLine':3,'endLine':4}
    ],{},capture_image=lambda _:None,capture_mesh=lambda _:None,authorize_file=Path,
    read_code=lambda spec:dict(text='float rim = 0;\nreturn rim;',startLine=spec.startLine,url='rdoc://shader/id?line=3'),root=tmp_path/'visuals')
    manifest=json.loads(Path(result['path']).read_bytes())
    assert manifest['cards'][0]['nodes'][0]['image']['sha256']
    assert manifest['cards'][0]['edgeLabels']==['Compositing']
    assert manifest['cards'][1]['code']['startLine']==3


def test_new_tools_declare_effects_and_long_replay_scope():
    from replaylab_agent.policy import tool_call_effects
    from src.host_runtime import LONG_TOOLS,REPLAY_TOUCH_TOOLS
    assert tool_call_effects('run_shader_effect_experiment',{})==frozenset({'workspace','replay'})
    assert {'run_shader_effect_experiment','publish_visual_reply','get_cbuffer_parameter_link'}<=LONG_TOOLS&REPLAY_TOUCH_TOOLS


def test_effect_tool_delivers_both_pixels_and_readable_slider_sources(tmp_path,monkeypatch):
    from src import effect_experiments
    monkeypatch.setattr(server,'_EVENT_TEXTURE_EVIDENCE_ROOT',tmp_path)
    folder=server._event_texture_evidence_root()/'shader-effect-experiments'/'trial'
    server._secure_event_artifact_directory(folder,tmp_path)
    images=[]
    for name,color in [('before','white'),('after','blue')]:
        path=folder/(name+'-display.png');Image.new('RGB',(1600,900),color).save(path)
        images.append(dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        assert server._within_evidence_roots(str(path))==path
    result=dict(ok=True,images=images,roi=[.5,.2,.55,.3],receiptPath='receipt')
    monkeypatch.setattr(effect_experiments,'run',lambda *args,**kwargs:result)
    blocks=server.run_shader_effect_experiment('workspace','a'*64,[],2111)
    assert json.loads(blocks[0])['receiptPath']=='receipt'
    assert len([b for b in blocks if not isinstance(b,str)])==4
    previews=[json.loads(b)for b in blocks[1:]if isinstance(b,str)]
    assert [(p['leg'],p['view'])for p in previews]==[
        ('before','context'),('before','region'),('after','context'),('after','region')]
    assert all(max(p['width'],p['height'])<=960 and p['inlineImage']=='included in this result'for p in previews)
