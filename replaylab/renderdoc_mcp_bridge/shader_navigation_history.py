"""Disposable, source-bound navigation checkpoints; never review/replay authority.

Only committed editor bytes are checkpointed. This is separate from immutable
Agent-authored annotations, preserves deletion markers, and uses the same OS
locking/atomic-write discipline as debug snapshots. Runs on the flow worker.
"""
import copy
import hashlib
import importlib.util
import json
import os
import tempfile

try:
    from . import shader_visual_snapshots as _snapshots
except (ImportError, ValueError):
    _spec = importlib.util.spec_from_file_location('_bread_navigation_snapshot_io',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shader_visual_snapshots.py'))
    _snapshots = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_snapshots)

MAX_BYTES = 512 * 1024
SCHEMA = 'bread-shader-navigation-checkpoint/v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _metadata(overview):
    value = copy.deepcopy(overview)
    value.pop('navigationBodies', None)
    value.pop('navigationSourceSha256', None)
    for row in value.get('stages', []):
        row['anchors'] = [anchor.get('kind') for anchor in row.get('anchors', [])]
        row['helperSignatures'] = len(row.get('helperSignatures', []))
    return value


class NavigationHistory(object):
    def __init__(self, root, identity):
        self.io = _snapshots.SnapshotStore(os.path.join(root, 'shader_navigation'), identity)

    def read(self, original):
        try:
            with open(self.io.path, 'rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None
            record = json.loads(raw.decode('utf-8'))
            overview = record['overview']
            if (record.get('schema') != SCHEMA or record.get('identity') != self.io.identity
                    or record.get('head') != digest(original) or record.get('seal') != digest(overview)
                    or not isinstance(overview, dict) or _metadata(overview) != _metadata(original)):
                return None
            # A saved native deletion has an empty signature and stays deleted.
            # Temporary inability to bind is different: retain its old source
            # evidence and retry when reopening the next committed revision.
            for stage in overview.get('stages', []):
                for anchor in stage.get('anchors', []):
                    anchor.pop('navigationUnresolved', None)
            return overview
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def write(self, original, projected, source):
        """No-op on dirty/discarded buffers, contention, corruption or stale heads."""
        temporary = None
        try:
            if not isinstance(projected, dict) or _metadata(original) != _metadata(projected):
                return False
            data = json.dumps({'schema': SCHEMA, 'identity': self.io.identity,
                'head': digest(original), 'seal': digest(projected), 'overview': projected},
                ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
            if len(data) > MAX_BYTES:
                return False
            with self.io._lock, self.io._file_lock():
                # Check inside the lock: another window may have saved a newer
                # source while this worker was parsing the old buffer.
                with open(self.io.identity['path'], 'rb') as stream:
                    current = stream.read(_snapshots.MAX_SOURCE_BYTES + 1)
                if current.decode('utf-8-sig').replace('\r\n', '\n') != source.replace('\r\n', '\n'):
                    return False
                try:
                    with open(self.io.path, 'rb') as stream:
                        if stream.read(MAX_BYTES + 1) == data:
                            return True
                except FileNotFoundError:
                    pass
                with tempfile.NamedTemporaryFile(mode='wb', dir=os.path.dirname(self.io.path),
                                                  prefix='.navigation-', suffix='.tmp', delete=False) as stream:
                    temporary = stream.name
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.io.path)
                temporary = None
            return True
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
