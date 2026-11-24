"""Store locked build archives as ordinary Git files smaller than 50 MiB."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from restore_dependencies import DEPENDENCIES, child, load_lock, sha, verify_archive


def seal(inputs: Path, part_size: int = 48 * 1024 * 1024) -> dict:
    if not 0 < part_size < 50 * 1024 * 1024:
        raise ValueError("Archive parts must be smaller than 50 MiB")
    lock = load_lock(inputs)
    for record in lock["archives"]:
        archive = verify_archive(inputs, record)
        parts = []
        with archive.open("rb") as stream:
            while data := stream.read(part_size):
                relative = f"archives/{record['id']}/{record['sha256'][:16]}.{len(parts):03}.part"
                target = child(inputs, relative)
                digest = hashlib.sha256(data).hexdigest()
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if target.is_symlink() or target.is_junction() or sha(target) != digest:
                        raise ValueError(f"Existing dependency part differs: {relative}")
                else:
                    with target.open("xb") as output:
                        output.write(data)
                parts.append({"path": relative, "size": len(data), "sha256": digest})
        record["parts"] = parts
        record.pop("downloadUrl", None)
        print(f"Sealed {record['id']}: {len(parts)} parts", flush=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=inputs, suffix=".tmp", delete=False) as stream:
            pending = Path(stream.name)
            stream.write(json.dumps(lock, ensure_ascii=False, indent=2) + "\n")
        os.replace(pending, inputs / "inputs.lock.json")
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return lock


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, default=DEPENDENCIES)
    args = parser.parse_args()
    seal(args.inputs)
