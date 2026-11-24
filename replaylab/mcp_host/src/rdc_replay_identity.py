"""Use the application's canonical Python-3.6-compatible RDC section reader."""
from functools import lru_cache
import importlib.util
from pathlib import Path
import sys
import threading

_lock = threading.RLock()


@lru_cache(maxsize=1)
def protocol():
    root = (Path(sys.executable).resolve().parent.parent / 'integrations/replaylab/python/renderdoc_mcp_bridge'
            if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2] / 'renderdoc_mcp_bridge')
    path = root / 'rdc_replay_identity.py'
    spec = importlib.util.spec_from_file_location('_bread_rdc_replay_identity', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest_file(path, checkpoint=None):
    """Reuse a stable observation only within this restore, never after a write."""
    from . import file_content_cache as cache
    path = Path(path)
    # A separate namespace prevents mixing a replay digest with whole-file SHA.
    cache_key = str(path) + '.bread-replay-content-v1'
    with _lock:
        before = cache.file_stamp(path)
        basic = path.stat()
        result = cache.get(cache_key, before) or cache._persistent(cache_key, before)
        if result is None:
            with path.open('rb') as stream:
                result = protocol().replay_digest(stream, checkpoint=checkpoint)
        after = path.stat()
        if (cache.file_stamp(path) != before or
                (basic.st_dev, basic.st_ino, basic.st_size, basic.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError('RDC changed during replay content hashing')
        cache.put(cache_key, before, result)
        cache._persistent(cache_key, before, result)
        return result
