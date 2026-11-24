"""Isolated file-I/O benchmark, never opens or restores a user capture."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'replaylab/mcp_host'))
from src import file_content_cache
from src.shader_workspace.status_snapshot import _seal

with tempfile.TemporaryDirectory(prefix='bread-restore-cache-benchmark-') as scratch:
    scratch=Path(scratch);os.environ['BREAD_FILE_DIGEST_CACHE_ROOT']=str(scratch/'cache')
    path=scratch/'shared-compiler.dll'
    with path.open('wb') as stream:
        for _ in range(64):stream.write(b'Bread123'*131072)
    repeat=16;start=time.perf_counter()
    for _ in range(repeat):
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    original=digest.hexdigest();baseline=(time.perf_counter()-start)*1000
    before=file_content_cache.statistics();start=time.perf_counter()
    for _ in range(repeat):assert _seal(path)[2]==original
    accelerated=(time.perf_counter()-start)*1000;after=file_content_cache.statistics()
    with file_content_cache._lock:file_content_cache._cache.clear()
    start=time.perf_counter();assert _seal(path)[2]==original;disk_reuse=(time.perf_counter()-start)*1000
    result={'scope':'isolated 64 MiB shared file, 16 repeated validation seals; not actual RDC restore timing',
        'baselineMs':round(baseline,3),'acceleratedMs':round(accelerated,3),'newMemoryColdDiskReuseMs':round(disk_reuse,3),
        'digestReads':after['hashReads']-before['hashReads'],'memoryHits':after['memoryHits']-before['memoryHits'],
        'userCaptureOpened':False,'userProjectWritten':False}
out=ROOT/'output/bread-dual-view-restore-20261101/restore-cache-benchmark.json';out.parent.mkdir(parents=True,exist_ok=True)
out.write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
