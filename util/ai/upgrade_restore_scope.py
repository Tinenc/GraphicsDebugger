"""Derive replay-content cache keys from authenticated whole-RDC proofs.

No project source, Shader, RDC, compiler result or validation claim is changed.
The original derived snapshot is retained before replacing this accelerator.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import uuid


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()


def upgrade(snapshot_path, backup, receipt_path):
    snapshot_path=Path(snapshot_path).resolve(strict=True)
    root=Path(__file__).resolve().parents[2]
    module_path=root/'replaylab/renderdoc_mcp_bridge/shader_apply_receipts.py'
    spec=importlib.util.spec_from_file_location('_bread_scope_upgrade',module_path)
    protocol=importlib.util.module_from_spec(spec);spec.loader.exec_module(protocol)
    original=snapshot_path.read_bytes()
    snapshot=json.loads(original)
    seal=snapshot.pop('sealSha256')
    if canonical_sha(snapshot)!=seal or snapshot.get('schema')!='replaylab-verified-rdc-restore/v1':
        raise ValueError('Derived restore snapshot is invalid')
    capture=Path(snapshot['capturePath']).resolve(strict=True)
    records=snapshot['shaders']
    proofs=[record['replacement']['restoreValidationReceipt'] for record in records]
    if any(proof['payload']['captureSha256']!=snapshot['captureSha256'] for proof in proofs):
        raise ValueError('Snapshot whole-file identity is inconsistent')
    upgraded=protocol.upgrade_receipts_replay_scope(protocol.ReceiptStore(),proofs,str(capture))
    for record,proof in zip(records,upgraded):
        record['replacement']['restoreValidationReceipt']=proof
    snapshot.update(captureReplaySha256=upgraded[0]['payload']['captureReplaySha256'],
        captureReplayIdentitySchema='bread-rdc-replay-content/v1')
    snapshot['sealSha256']=canonical_sha(snapshot)
    backup=Path(backup).resolve()
    backup.parent.mkdir(parents=True,exist_ok=True)
    if backup.exists(): raise ValueError('Use a new backup path')
    backup.write_bytes(original)
    if snapshot_path.read_bytes()!=original: raise ValueError('Snapshot changed during scope upgrade')
    pending=snapshot_path.with_name(snapshot_path.name+'.scope-'+uuid.uuid4().hex)
    try:
        pending.write_text(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False),encoding='utf-8')
        os.replace(pending,snapshot_path)
    finally:
        pending.unlink(missing_ok=True)
    result={'upgraded':True,'shaders':len(records),'snapshot':str(snapshot_path),'backup':str(backup),
        'captureFullSha256':snapshot['captureSha256'],'captureReplaySha256':snapshot['captureReplaySha256'],
        'unchangedValidationClaims':True,'oldReceiptsPreserved':True}
    Path(receipt_path).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('snapshot','backup','receipt'): parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    upgrade(args.snapshot,args.backup,args.receipt)
