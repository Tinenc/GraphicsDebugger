from concurrent.futures import ThreadPoolExecutor
import hashlib
import ctypes
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
from src import file_content_cache as cache
from src.restore_file_preparation import artifact_statuses


def test_persistent_digest_reuses_across_process_memory_reset_and_detects_same_mtime_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAD_FILE_DIGEST_CACHE_ROOT",str(tmp_path/"cache"))
    p=tmp_path/"capture.rdc";p.write_bytes(b"first")
    start=cache.statistics()["hashReads"]
    assert cache.digest_file(p,persistent=True)==hashlib.sha256(b"first").hexdigest()
    with cache._lock:cache._cache.clear()
    assert cache.digest_file(p,persistent=True)==hashlib.sha256(b"first").hexdigest()
    reliable=cache.file_stamp(p) is not None
    assert cache.statistics()["hashReads"]-start==(1 if reliable else 2)
    old=p.stat();p.write_bytes(b"other");os.utime(p,ns=(old.st_atime_ns,old.st_mtime_ns))
    assert cache.digest_file(p,persistent=True)==hashlib.sha256(b"other").hexdigest()


def test_unknown_or_corrupt_disk_cache_is_disposable(tmp_path,monkeypatch):
    monkeypatch.setenv("BREAD_FILE_DIGEST_CACHE_ROOT",str(tmp_path/"cache"))
    p=tmp_path/"shader.bin";p.write_bytes(b"shader")
    target=cache._disk_path(p);target.parent.mkdir();target.write_text('{"schema":"old"}')
    assert cache.digest_file(p,persistent=True)==hashlib.sha256(b"shader").hexdigest()
    with cache._lock:cache._cache.clear()
    target.write_text("broken cache")
    assert cache.digest_file(p,persistent=True)==hashlib.sha256(b"shader").hexdigest()
    with cache._lock:cache._cache.clear()
    target.write_text("123")
    assert cache.digest_file(p,persistent=True)==hashlib.sha256(b"shader").hexdigest()


def test_concurrent_shared_compiler_hash_reads_once(tmp_path):
    p=tmp_path/"compiler.exe";p.write_bytes(b"tool"*1024*1024)
    start=cache.statistics()["hashReads"]
    with ThreadPoolExecutor(max_workers=4) as pool:values=list(pool.map(cache.digest_file,[p]*8))
    assert len(set(values))==1
    assert cache.statistics()["hashReads"]-start==(1 if cache.file_stamp(p) is not None else 8)


def test_no_reliable_file_change_identity_always_hashes(tmp_path,monkeypatch):
    monkeypatch.setattr(cache,"file_stamp",lambda path:None)
    p=tmp_path/"source";p.write_bytes(b"aaa")
    assert cache.digest_file(p,persistent=True)==hashlib.sha256(b"aaa").hexdigest()
    p.write_bytes(b"bbb")
    assert cache.digest_file(p,persistent=True)==hashlib.sha256(b"bbb").hexdigest()


@pytest.mark.skipif(os.name!="nt",reason="Windows FILE_BASIC_INFO contract")
def test_filesystem_with_zero_change_time_disables_cache(tmp_path,monkeypatch):
    unused,Basic=cache._windows_api()
    class Kernel:
        def CreateFileW(self,*args):return 77
        def CloseHandle(self,handle):pass
        def GetFileInformationByHandleEx(self,handle,kind,pointer,size):
            ctypes.cast(pointer,ctypes.POINTER(Basic)).contents.changed=0
            return 1
    monkeypatch.setattr(cache,"_windows_api",lambda:(Kernel(),Basic))
    path=tmp_path/"unsupported-filesystem";path.write_bytes(b"source")
    assert cache.file_stamp(path) is None


def test_file_preparation_is_bounded_ordered_and_cancellable():
    active=0;peak=0;guard=threading.Lock();checkpoints=[]
    def read(path):
        nonlocal active,peak
        with guard:active+=1;peak=max(peak,active)
        try:time.sleep(.005);return {"workspacePath":path}
        finally:
            with guard:active-=1
    result=artifact_statuses(lambda:SimpleNamespace(status=read),range(12),checkpoints.append)
    assert [row["workspacePath"] for row in result]==list(map(str,range(12)))
    assert peak==2 and len(checkpoints)==3
    def stop(phase):raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError,match="cancelled"):
        artifact_statuses(lambda:SimpleNamespace(status=read),range(12),stop)
