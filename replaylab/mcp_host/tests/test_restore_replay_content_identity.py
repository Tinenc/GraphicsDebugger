import importlib.util
import io
from pathlib import Path
import struct

import pytest

from src.rdc_replay_identity import protocol, digest_file


def section(kind=1, data=b'frame resources and commands', name=b'FrameCapture'):
    return struct.pack('<4sIQQQII', b'\0'*4, kind, len(data), len(data), 1, 0, len(name)+1) + name+b'\0'+data


def capture(*sections):
    return struct.pack('<QII16s',0x434f4452,0x102,32,b'fixture') + b''.join(sections)


def digest(data):
    return protocol().replay_digest(io.BytesIO(data))


def test_only_editor_annotations_can_change_without_losing_replay_identity():
    original = capture(section())
    for kind in (3,4,5):
        assert digest(original) == digest(original+section(kind,b'new annotation',b'Editor'))
    assert digest(original) == digest(original+section(0,b'project receipts',b'replaylab/cache/project-reuse-index'))
    for kind in (0,2,9,10,11,12):
        assert digest(original) != digest(original+section(kind,b'potential replay input',b'Other'))
    assert digest(original) != digest(capture(section(data=b'changed frame commands')))


def test_ascii_annotations_and_compressed_payloads_are_bounded_without_decompression():
    original = capture(section(data=b'compressed opaque capture payload'))
    annotation = b'A\n4\n4\n1\nNotes\ntext'
    assert digest(original+annotation) == digest(original)
    changed = bytearray(original)
    # A replay section's flags are part of its identity, even if payload bytes match.
    struct.pack_into('<I',changed,32+32,2)
    assert digest(changed) != digest(original)


@pytest.mark.parametrize('bad', [b'not an rdc', capture(), capture(section())[:-1],
    capture(section(),section()), capture(section())+b'?',
    capture(section())+struct.pack('<4sIQQQII',b'\0'*4,4,1<<60,1<<60,1,0,2)+b'n\0'])
def test_malformed_containers_never_acquire_replay_identity(bad):
    with pytest.raises(ValueError): digest(bad)


def test_host_cache_observes_annotation_and_replay_changes(tmp_path,monkeypatch):
    monkeypatch.setenv('BREAD_FILE_DIGEST_CACHE_ROOT',str(tmp_path/'cache'))
    path=tmp_path/'capture.rdc'; path.write_bytes(capture(section()))
    first=digest_file(path)
    path.write_bytes(capture(section(),section(3,b'note',b'Bookmarks')))
    assert digest_file(path)==first
    path.write_bytes(capture(section(data=b'new replay')))
    assert digest_file(path)!=first


def test_embedded_capture_cache_uses_same_scope_and_rechecks_file_changes(tmp_path):
    path=Path(__file__).resolve().parents[2]/'renderdoc_mcp_bridge/shader_apply_receipts.py'
    spec=importlib.util.spec_from_file_location('_bread_receipts_content_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    cache=module.CaptureDigestCache(); source=tmp_path/'frame.rdc'
    source.write_bytes(capture(section())); first=cache.replay_digest(str(source),'a'*32)
    assert first==digest_file(source)
    source.write_bytes(capture(section(),section(5,b'name',b'ResourceRenames')))
    assert cache.replay_digest(str(source),'a'*32)==first
    source.write_bytes(capture(section(data=b'changed GPU commands')))
    assert cache.replay_digest(str(source),'a'*32)!=first


def test_scope_upgrade_requires_authenticated_original_whole_file_bytes(tmp_path):
    path=Path(__file__).resolve().parents[2]/'renderdoc_mcp_bridge/shader_apply_receipts.py'
    spec=importlib.util.spec_from_file_location('_bread_receipts_upgrade_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    source=tmp_path/'frame.rdc'; source.write_bytes(capture(section()))
    store=module.ReceiptStore(str(tmp_path/'keys'))
    full=module.CaptureDigestCache().digest(str(source),'old')
    payload={'schema':module.SCHEMA,'captureSha256':full,'originalRawSha256':'a'*64}
    old=store.seal(payload)
    upgraded=module.upgrade_receipts_replay_scope(store,[old],str(source))[0]
    new=store.verify(upgraded)
    assert new.pop('captureReplaySha256') == digest_file(source)
    assert new.pop('captureReplayIdentitySchema') == 'bread-rdc-replay-content/v1'
    assert new == payload and store.verify(old) == payload
    source.write_bytes(capture(section(),section(3,b'note',b'Bookmarks')))
    with pytest.raises(ValueError,match='Original RDC bytes'):
        module.upgrade_receipts_replay_scope(store,[old],str(source))
    tampered={'payload':dict(payload,originalRawSha256='b'*64),'signature':old['signature']}
    with pytest.raises(ValueError,match='signature'):
        module.upgrade_receipts_replay_scope(store,[tampered],str(source))
