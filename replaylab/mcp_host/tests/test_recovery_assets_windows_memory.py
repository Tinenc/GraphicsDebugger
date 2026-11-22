"""Windows secure asset reads keep ctypes metadata process-bounded."""

from __future__ import annotations

import ctypes
import os
import threading

import pytest

from src.symbols import recovery_assets


@pytest.mark.skipif(os.name != "nt", reason="Windows ctypes pointer-cache regression")
def test_secure_reader_reuses_winapi_and_pointer_types_under_load(tmp_path):
    payload = b"bounded recovery asset\n"
    (tmp_path / "asset.bin").write_bytes(payload)

    first = recovery_assets.read_recovery_regular_file(
        tmp_path, "asset.bin", len(payload)
    )
    assert first == (payload, "", len(payload))
    api = recovery_assets._windows_secure_file_api()
    assert api is not None
    pointer_cache = getattr(ctypes, "_pointer_type_cache")
    pointer_types_before = set(pointer_cache)

    # This loop previously created fresh Structure classes on every read;
    # ctypes retained POINTER variants for each class forever.
    for _ in range(512):
        assert recovery_assets.read_recovery_regular_file(
            tmp_path, "asset.bin", len(payload)
        ) == (payload, "", len(payload))

    singleton_ids: list[int] = []
    barrier = threading.Barrier(9)

    def get_api() -> None:
        barrier.wait()
        singleton_ids.append(id(recovery_assets._windows_secure_file_api()))

    threads = [threading.Thread(target=get_api) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert set(singleton_ids) == {id(api)}
    assert set(pointer_cache) == pointer_types_before

