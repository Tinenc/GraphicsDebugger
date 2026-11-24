"""Controlled captured-Shader ablation; immutable receipts, no source publication."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from pathlib import Path

from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field

from .visual_replies import _json, _write


class ExactSourceEdit(BaseModel):
    model_config=ConfigDict(extra='forbid')
    old_text: str=Field(min_length=1,max_length=24000,description='Unique exact span from the current saved Shader source')
    new_text: str=Field(max_length=24000,description='Replacement calculation for this temporary experiment only')


class RelatedVertexEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workspace_path: str
    expected_source_sha256: str
    edits: list[ExactSourceEdit] = Field(min_length=1, max_length=16)


def edited_source(source: str, edits: list[dict[str,str]]) -> str:
    if not 1<=len(edits)<=16:raise ValueError('Use 1..16 exact, focused source edits')
    # Tool text and JSON clients may use LF while the canonical Windows file
    # uses CRLF. Only the temporary compile unit is normalized; saved bytes and
    # the source identity guard stay untouched.
    result=source.replace('\r\n','\n')
    for edit in edits:
        if set(edit)!={'old_text','new_text'} or not all(isinstance(v,str)for v in edit.values()):
            raise ValueError('Each edit needs only old_text and new_text')
        old,new=edit['old_text'].replace('\r\n','\n'),edit['new_text'].replace('\r\n','\n')
        if not old or old==new or len(old)+len(new)>24000 or result.count(old)!=1:
            raise ValueError('Effect edits must change one unique exact source span')
        result=result.replace(old,new,1)
    return result


def _display_pair(images,flip_y):
    if len(images)!=2 or any(not isinstance(item,dict)for item in images):
        raise ValueError('Native experiment did not save both images')
    result=[]
    for item in images:
        path=Path(item['path']);raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Experiment image changed')
        with Image.open(path) as image:
            image=image.convert('RGB')
            if flip_y:image=ImageOps.flip(image)
            image.thumbnail((1600,1600),Image.Resampling.LANCZOS)
            display=path.with_name(path.stem+'-display.png')
            import io
            buffer=io.BytesIO();image.save(buffer,format='PNG');data=buffer.getvalue();_write(display,data)
            result.append(dict(path=str(display),sha256=hashlib.sha256(data).hexdigest(),width=image.width,height=image.height))
    if result[0]['width']!=result[1]['width'] or result[0]['height']!=result[1]['height']:
        raise ValueError('Experiment image dimensions changed')
    return result


def model_previews(result):
    """Bounded context and ROI pictures, with unambiguous before/after labels.

    These are model previews only. The slider still verifies the unchanged,
    content-addressed display pair against the experiment receipt.
    """
    previews=[]
    region=result.get('roi') or [0.,0.,1.,1.]
    for index,item in enumerate(result.get('images',[])):
        path=Path(item['path'])
        with Image.open(path)as original:
            for view in ('context','region'):
                image=original.convert('RGB')
                if view=='region':
                    left,top,right,bottom=region
                    dx=max((right-left)*.25,16/image.width)
                    dy=max((bottom-top)*.25,16/image.height)
                    box=(int(max(0,left-dx)*image.width),int(max(0,top-dy)*image.height),
                         int(min(1,right+dx)*image.width),int(min(1,bottom+dy)*image.height))
                    image=image.crop(box)
                image.thumbnail((960,960),Image.Resampling.LANCZOS)
                preview=path.with_name(path.stem+'-model-'+view+'.png')
                import io
                buffer=io.BytesIO();image.save(buffer,format='PNG');_write(preview,buffer.getvalue())
                previews.append(dict(path=str(preview),leg='before'if index==0 else 'after',
                    view=view,sourcePath=str(path),sourceSha256=item['sha256'],
                    width=image.width,height=image.height))
    return previews


def baseline_control_scope(control,region):
    effect=control.get('effectObservation')or{}
    if not effect.get('conclusive'):return ''
    if control.get('accepted'):return 'full-output'
    metrics=effect.get('metrics')or[]
    # Local attribution may use its explicitly requested region. Keep the full
    # output's rejected metrics in the receipt, retain the same tolerances and
    # require the full repeat-stability check. This never validates other pixels.
    if region!=[0.,0.,1.,1.] and metrics and all(m.get('accepted')for m in metrics):
        return 'requested-region'
    return ''


def run(server,workspace_path,expected_source_sha256,edits,observation_event_id=0,
        attachment_index=0,roi=None,flip_y=False,label='',related_edits=None,display_transfer='auto',
        observation_scope='auto'):
    if display_transfer not in ('auto','identity','linear-to-srgb'):raise ValueError('Unknown effect display transfer')
    if observation_scope not in ('auto', 'appearance', 'stage'):
        raise ValueError('Unknown effect observation scope')
    final_output = (observation_scope == 'appearance'
                    or (observation_scope == 'auto' and observation_event_id == 0))
    if (type(observation_event_id) is not int or observation_event_id < 0
            or (not final_output and not observation_event_id) or not 0 <= attachment_index <= 63):
        raise ValueError('A stage observation requires a positive event and a bounded RTV index')
    service=server._shader_workspace_service()
    from .shader_workspace.errors import ManifestValidationError
    root=service._workspace_root(workspace_path)
    refreshed=False
    try:
        service._load_manifest(root)
    except ManifestValidationError as error:
        stale=str(error)
        if not any(message in stale for message in (
                'route catalog differs from the process-owned path','route capability changed after creation',
                'route plan tool registry path mismatch','tool operation changed after workspace creation')):
            raise
        # The established migration revalidates the old record, preserves exact
        # source/identity, archives provenance and discards stale execution proof.
        old=service._load_manifest(root,allow_stale_route_bindings=True,validate_latest_candidate=False)
        service.migrate_toolchain(workspace_path,workspace_path,
            expected_route_plan_sha256=old['routePlan']['planSha256'],
            expected_source_sha256=expected_source_sha256,in_place=True,
            host_instance_id=server.os.environ.get('REPLAYLAB_HOST_INSTANCE_ID'))
        refreshed=True
    status=service.status(workspace_path)
    if status.get('stage')not in ('vertex','fragment'):
        raise ValueError('Effect trials support vertex/fragment workspaces; compute requires a storage-aware debug/resource route')
    binding=server._ensure_workspace_capture_catalog_identity(workspace_path,status=status)
    session=server._strict_lower_hex(binding.get('captureSessionId'),32,'capture session')
    source_path=Path(status['source']['path']);source=source_path.read_bytes()
    if hashlib.sha256(source).hexdigest()!=expected_source_sha256:raise ValueError('Canonical Shader source changed')
    changed=edited_source(source.decode('utf-8-sig'),edits)
    region=roi if roi is not None else [0.,0.,1.,1.]
    if (len(region)!=4 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v)or not 0<=v<=1 for v in region)
            or region[0]>=region[2]or region[1]>=region[3]):raise ValueError('ROI must be normalized [left,top,right,bottom]')
    anchor=server._shader_organization_anchor_event(status)
    presentation = {}
    if final_output:
        capture_id = status['capture']['captureId']
        resolution = server.ipc.call('resolve_shader_effect_presentation', dict(
            expected_capture_id=capture_id, expected_capture_session_id=session,
            source_event_id=anchor, observation_event_id=observation_event_id))
        if (resolution.get('captureId') != capture_id or resolution.get('captureSessionId') != session
                or resolution.get('sourceEventId') != anchor):
            raise ValueError('Final output resolution does not match the Shader capture and source event')
        target = resolution.get('target')
        if target is None:
            count = resolution['candidateCount']
            return dict(ok=False, phase='presentation-selection', applied=False,
                        candidates=resolution['candidates'], candidateCount=count,
                        reason=('Resolve the matching Present and pass its exact event ID'
                                if count > 1 else 'No captured Present with a destination was found after the Shader event'))
        presentation = dict(requestedEventId=observation_event_id, eventId=target['eventId'],
                            resourceId=target['resourceId'],
                            retargeted=bool(observation_event_id and observation_event_id != target['eventId']))
        observation_event_id, attachment_index = target['eventId'], 0
        # The captured output decides display transfer; a stale override can double-encode
        # even when the requested EID already names the correct Present.
        display_transfer = 'auto'
    companions = []
    scope = None
    if status['stage'] == 'vertex':
        scope = server.ipc.call('inspect_vertex_effect_scope', dict(event_id=anchor,
                                observation_event_id=observation_event_id), timeout=120)
        if scope.get('captureSessionId') != session:
            raise ValueError('Vertex scope does not match the current capture session')
        if not scope.get('complete'):
            return dict(ok=False, phase='vertex-dependency-check', geometryScope=scope,
                        reason='Vertex dependency evidence is incomplete', applied=False)
        primary_id = int(status['shader']['resourceId'])
        primary_hash = status['baseline']['rawSha256']
        by_id = {}
        for match in scope['matches']:
            rid = match['shaderResourceId']
            if rid != primary_id and match['shaderRawSha256'] != primary_hash:
                by_id.setdefault(rid, []).append(match)
        remaining = set(by_id)
        for proposed in related_edits or []:
            path = proposed['workspace_path']
            peer = service.status(path)
            rid = int(peer['shader']['resourceId'])
            peer_anchor = server._shader_organization_anchor_event(peer)
            if (rid not in remaining or peer['stage'] != 'vertex' or
                    peer['capture']['captureId'] != status['capture']['captureId'] or
                    not any(x['eventId'] == peer_anchor and x['shaderRawSha256'] == peer['baseline']['rawSha256']
                            for x in by_id[rid])):
                raise ValueError('Each related edit must identify one verified companion draw and its original VS')
            server._ensure_workspace_capture_catalog_identity(path, status=peer)
            peer_path = Path(peer['source']['path'])
            peer_hash = proposed['expected_source_sha256']
            peer_source = peer_path.read_bytes()
            if hashlib.sha256(peer_source).hexdigest() != peer_hash:
                raise ValueError('Related Shader source changed')
            text = peer_source.decode('utf-8-sig')
            peer_control = service.compile_diagnostic_candidate(path, text, peer_hash, 'related effect control')
            peer_trial = service.compile_diagnostic_candidate(
                path, edited_source(text, proposed['edits']), peer_hash, 'related effect trial')
            if not peer_control.get('compiled') or not peer_trial.get('compiled'):
                return dict(ok=False, phase='related-compile', eventId=peer_anchor,
                            control=peer_control, trial=peer_trial, applied=False)
            companions.append(dict(resourceId=rid, eventId=peer_anchor, source=peer_path, sourceHash=peer_hash,
                                   control=peer_control, trial=peer_trial))
            remaining.remove(rid)
        if remaining:
            return dict(ok=False, phase='vertex-dependency-check', geometryScope=scope,
                        requiredShaderResourceIds=sorted(remaining), applied=False,
                        reason='Related depth/color draws use different vertex calculations. Supply their exact related_edits.')
    elif related_edits:
        raise ValueError('Related vertex edits require a vertex experiment')
    canonical=service.compile_diagnostic_candidate(workspace_path,source.decode('utf-8-sig'),expected_source_sha256,'effect control: '+label[:160])
    if not canonical.get('compiled'):return dict(ok=False,phase='control-compile',control=canonical,applied=False)
    trial=service.compile_diagnostic_candidate(workspace_path,changed,expected_source_sha256,'effect trial: '+label[:160])
    if not trial.get('compiled'):return dict(ok=False,phase='trial-compile',trial=trial,applied=False)
    options=dict(eventId=observation_event_id,attachmentIndex=attachment_index,roi=region,flipY=flip_y,
                 blackPoint=0.,whitePoint=1.,displayTransfer=display_transfer)
    def compare(compiled,baseline,artifact_dir=None):
        related = []
        for peer in companions:
            control = peer['control']
            candidate = peer['trial'] if artifact_dir else control
            baseline_peer = (dict(path=control['candidateBlobPath'], rawSha256=control['rawSha256'])
                             if artifact_dir else control['baseline'])
            related.append(dict(eventId=peer['eventId'], resourceId=peer['resourceId'],
                                encoding=candidate['comparisonParams']['encoding'], baseline=baseline_peer,
                                candidate=dict(path=candidate['candidateBlobPath'], rawSha256=candidate['rawSha256'])))
        params=dict(compiled['comparisonParams'])
        params.update(expected_capture_id=status['capture']['captureId'],expected_capture_session_id=session,
            event_id=anchor,expected_original_resource_id=status['shader']['resourceId'],
            expected_original_sha256=status['baseline']['rawSha256'],baseline=baseline,
            candidate={'path':compiled['candidateBlobPath'],'rawSha256':compiled['rawSha256']},
            effect_observation=dict(options,artifactDir=str(artifact_dir)if artifact_dir else None,
                                    relatedShaders=related))
        result=server.ipc.call('compare_shader_organization_candidates',params,timeout=300.)
        if (result.get('captureSessionId')!=session or result.get('eventId')!=anchor or result.get('replayRestored')is not True):
            raise ValueError('Effect observation did not confirm target identity and replay restoration')
        if presentation:
            evidence = result.get('effectObservation') or {}
            outputs = [metric.get('output', {}) for metric in evidence.get('metrics', [])]
            if (evidence.get('eventId') != observation_event_id or len(outputs) != 1
                    or outputs[0].get('capturedPresent') is not True
                    or str(outputs[0].get('resourceId')) != presentation['resourceId']):
                raise ValueError('Replay output did not match the resolved captured Present')
            if artifact_dir and evidence.get('images'):
                before, after = evidence['images']
                if (any(image.get('capturedPresent') is not True or image.get('eventId') != observation_event_id
                        or image.get('resourceId') != presentation['resourceId'] for image in (before, after))
                        or before.get('displayTransfer') != after.get('displayTransfer')):
                    raise ValueError('Exported images did not match the resolved captured Present and display mapping')
        return result
    control=compare(canonical,canonical['baseline'])
    control_scope=baseline_control_scope(control,region)
    if not control_scope:
        return dict(ok=False,phase='baseline-control',control=control,applied=False,sourcePreserved=True,
                    observationEventId=observation_event_id,attachmentIndex=attachment_index,
                    reason='Unchanged source does not yet reproduce a stable captured output; causal attribution is unavailable')
    evidence_root=server._event_texture_evidence_root()
    folder=evidence_root/'shader-effect-experiments'/('effect-'+uuid.uuid4().hex)
    server._secure_event_artifact_directory(folder,evidence_root)
    observation=compare(trial,{'path':canonical['candidateBlobPath'],'rawSha256':canonical['rawSha256']},folder)
    effect=observation.get('effectObservation')or{}
    if not effect.get('images'):return dict(ok=False,phase='trial-observation',observation=observation,applied=False)
    images=_display_pair(effect['images'],flip_y)
    metrics=effect['metrics'];conclusive=bool(effect.get('conclusive'))
    outcome=('inconclusive'if not conclusive else 'no-observed-change'if all(m['byteExact']for m in metrics)
             else 'small-change'if all(m['accepted']for m in metrics)else 'changed')
    if hashlib.sha256(source_path.read_bytes()).hexdigest()!=expected_source_sha256:raise ValueError('Canonical source changed during experiment')
    if any(hashlib.sha256(peer['source'].read_bytes()).hexdigest() != peer['sourceHash'] for peer in companions):
        raise ValueError('Related saved source changed during the experiment')
    replacement_scope = effect.get('replacementScope') or (
        'related-shader-resources' if effect.get('coordinatedShaderCount', 1) > 1 else 'shared-shader-resource')
    receipt=dict(schema='bread-shader-effect-experiment/v1',verified=conclusive,captureSessionId=session,
        captureId=status['capture']['captureId'],sourceEventId=anchor,observationEventId=observation_event_id,
        workspacePath=workspace_path,sourceSha256=expected_source_sha256,label=label[:200],roi=region,
        stage=status['stage'],
        sourceEdits=edits,images=images,baselineControl=control,baselineControlScope=control_scope,
        observation=observation,outcome=outcome,
        replacementScope=replacement_scope,geometryScope=scope,sourcePreserved=True,replayRestored=True,applied=False,
        toolchainRefreshed=refreshed,
        display=dict(blackPoint=0.,whitePoint=1.,flipY=flip_y,channels='rgb',transfer=display_transfer),
        **({'presentationSelection': presentation} if presentation else {}))
    data=_json(receipt);digest=hashlib.sha256(data).hexdigest();path=folder/(digest+'.json');_write(path,data)
    return dict(ok=conclusive,phase='completed',outcome=outcome,receiptPath=str(path),receiptSha256=digest,
        stage=status['stage'],
        images=images,roi=region,roiMetrics=metrics,baselineControlPassed=True,replayRestored=True,applied=False,
        sourcePreserved=True,replacementScope=replacement_scope,geometryScope=scope,observationEventId=observation_event_id,
        displayTransfer=effect['images'][0].get('displayTransfer',display_transfer),
        presentationTarget=effect['images'][0].get('presentationTarget',False),baselineControlScope=control_scope,
        **({'presentationSelection': presentation} if presentation else {}))


def _verified_effect_receipt(path: Path, capture: dict) -> dict:
    raw=path.read_bytes()
    if len(raw)>512*1024 or not re.fullmatch(r'[0-9a-f]{64}',path.stem) or hashlib.sha256(raw).hexdigest()!=path.stem:
        raise ValueError('Effect receipt is not an immutable content-addressed observation')
    receipt=json.loads(raw)
    if (receipt.get('schema')!='bread-shader-effect-experiment/v1' or receipt.get('verified')is not True
            or receipt.get('captureSessionId')!=capture.get('captureSessionId') or not receipt.get('replayRestored')
            or not receipt.get('sourcePreserved')):raise ValueError('Effect receipt is not conclusive for this capture')
    return receipt


def verified_comparison(path:Path,capture:dict,before:dict,after:dict)->dict:
    receipt = _verified_effect_receipt(path, capture)
    images=receipt.get('images',[])
    if before.get('presentationFlipApplied') or after.get('presentationFlipApplied'):
        raise ValueError('Use the experiment pair in its recorded orientation and the viewer Flip Y control')
    if len(images)!=2 or [before.get('sourceSha256'),after.get('sourceSha256')]!=[i.get('sha256')for i in images]:
        raise ValueError('Comparison images do not match the exact controlled experiment')
    return _effect_facts(path, receipt)


def _effect_facts(path: Path, receipt: dict) -> dict:
    observation = receipt['observation']['effectObservation']
    native_images = observation.get('images') or []
    basis = None
    if len(native_images) == 2 and all(image.get('presentationTarget') is True for image in native_images):
        first, second = native_images
        event_id = receipt['observationEventId']
        if (first['eventId'] != event_id or second['eventId'] != event_id
                or first['resourceId'] != second['resourceId']
                or first['displayTransfer'] != second['displayTransfer']):
            raise ValueError('Final output changed its display basis during the comparison')
        display = receipt['display']
        basis = dict(eventId=event_id, resourceId=first['resourceId'], transfer=first['displayTransfer'],
                     blackPoint=display['blackPoint'], whitePoint=display['whitePoint'], flipY=display['flipY'])
    captured_present = bool(basis) and all(image.get('capturedPresent') is True for image in native_images)
    return dict(verified=True,receiptSha256=path.stem,outcome=receipt['outcome'],roi=receipt['roi'],
                stage=receipt.get('stage','fragment'),
                sourceEventId=receipt.get('sourceEventId'),workspacePath=receipt.get('workspacePath'),
                observationEventId=receipt['observationEventId'],replacementScope=receipt['replacementScope'],
                metrics=observation['metrics'], capturedPresent=captured_present,
                **({'presentationBasis': basis} if basis else {}))


def verify_layer_exclusion(path: Path, capture: dict, event_id: int, reference: dict) -> str:
    receipt = _verified_effect_receipt(path, capture)
    facts = _effect_facts(path, receipt)
    roi = facts['roi']
    marked = reference['roi']
    if (facts['sourceEventId'] != event_id or not facts['capturedPresent'] or
            facts['presentationBasis'] != reference.get('presentationBasis') or
            not (roi[0] <= marked[0] and roi[1] <= marked[1] and roi[2] >= marked[2] and roi[3] >= marked[3]) or
            facts['outcome'] != 'no-observed-change' or not facts['metrics'] or
            not all(row.get('byteExact') is True for row in facts['metrics'])):
        raise ValueError(f'EID {event_id} cannot be excluded. Disable that layer and verify no change in the '
                         'marked region on the same final output. A different Shader or extra inputs alone '
                         'do not exclude a neighbouring draw.')
    return path.stem
