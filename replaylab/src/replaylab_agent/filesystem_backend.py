"""Keep the SDK file contract while decoding ripgrep's JSON as UTF-8.

The pinned SDK streams JSON with the Windows locale encoding. Retain its path
parser, file fallback, caps and process reaper; only this subprocess is UTF-8.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import tempfile
import threading

from deepagents.backends.filesystem import FilesystemBackend, _resolve_ripgrep_path, DEFAULT_GREP_TIMEOUT


class Utf8GrepMixin:
    def _ripgrep_search(self, pattern: str, base_full: Path, include_glob: str | None,
                        max_count: int | None = None):
        executable=_resolve_ripgrep_path()
        if executable is None:return None,False
        command=[executable,'--json','-F']
        if max_count is not None:command+=['-m',str(max_count+1)]
        if include_glob:command+=['--glob',include_glob]
        directory=base_full.is_dir()
        command+=['--',pattern,'.'if directory else str(base_full)]
        logger=logging.getLogger(__name__)
        with tempfile.TemporaryFile() as errors:
            try:
                process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=errors,
                    text=True,encoding='utf-8',errors='strict',cwd=str(base_full)if directory else None,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt'else 0)
            except (OSError,ValueError):
                return None,False
            expired=threading.Event()
            def expire():
                expired.set()
                try:process.kill()
                except OSError:pass
            timer=threading.Timer(DEFAULT_GREP_TIMEOUT,expire);timer.daemon=True;timer.start()
            results={};total=0;truncated=False;failed=False
            try:
                assert process.stdout is not None
                resolved=base_full.resolve()
                for line in process.stdout:
                    match=self._parse_rg_match(line,base_full,resolved)
                    if match is None:continue
                    if max_count is not None and total>=max_count:
                        truncated=True;process.terminate();break
                    path,number,text=match
                    results.setdefault(path,[]).append((number,text));total+=1
            except (UnicodeError,OSError):
                failed=True
                try:process.terminate()
                except OSError:pass
            finally:
                timer.cancel();self._reap_ripgrep(process)
            if failed:return None,False
            if expired.is_set():return (results,True)if results else(None,False)
            if truncated:return results,True
            if process.returncode not in (0,1):
                errors.seek(0)
                logger.debug('ripgrep failed; using SDK file search: %s',errors.read(2048).decode('utf-8','replace'))
                return None,False
            return results,False


class Utf8FilesystemBackend(Utf8GrepMixin,FilesystemBackend):
    pass
