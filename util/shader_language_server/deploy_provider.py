"""Deploy the pinned semantic provider from a verified official release archive.

No download or arbitrary archive extraction occurs during an application build.
Usage: python deploy_provider.py --archive <slang.zip> --application <x64/Development>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import zipfile


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def verify(application: Path, manifest: dict) -> dict:
    target = (application.resolve() / manifest["install_subdirectory"]).resolve()
    target.relative_to(application.resolve())
    receipt = json.loads((target / "provider.json").read_text(encoding="utf-8"))
    if any(receipt.get(key) != manifest[key] for key in ("provider", "version", "archive_sha256")):
        raise ValueError("Installed semantic provider does not match the pinned release")
    required = {item["destination"]: item["sha256"] for item in manifest["files"]}
    required.update(manifest.get("license_files", {}))
    if not manifest.get("license_files"):
        required.update({name: value for name, value in receipt["installed_files"].items()
                         if name in manifest["license_members"] or
                         any(name.startswith(prefix) for prefix in manifest["license_prefixes"])})
    for name, expected in required.items():
        path = (target / name).resolve()
        path.relative_to(target)
        if digest(path) != expected:
            raise ValueError("Installed semantic provider file checksum mismatch: " + name)
    return dict(provider=manifest["provider"], version=manifest["version"], path=str(target),
                verified=True, files=len(required))


def deploy(archive: Path, application: Path, manifest: dict) -> dict:
    if archive.stat().st_size != manifest["archive_size"] or digest(archive) != manifest["archive_sha256"]:
        raise ValueError("The shader language-server archive does not match the pinned release")
    target = (application.resolve() / manifest["install_subdirectory"]).resolve()
    target.relative_to(application.resolve())
    payloads = {}
    with zipfile.ZipFile(archive) as package:
        for item in manifest["files"]:
            data = package.read(item["member"])
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("The pinned provider executable has an invalid checksum")
            payloads[item["destination"]] = data
        for name in package.namelist():
            if name.endswith("/"):
                continue
            if name in manifest["license_members"] or any(name.startswith(prefix) for prefix in manifest["license_prefixes"]):
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or ":" in name or "\\" in name:
                    raise ValueError("Invalid license path in provider archive")
                payloads[name] = package.read(name)
    receipt = dict(manifest, installed_files={name: hashlib.sha256(data).hexdigest()
                                            for name, data in sorted(payloads.items())})
    payloads["provider.json"] = (json.dumps(receipt, indent=2) + "\n").encode("utf-8")
    target.mkdir(parents=True, exist_ok=True)
    for name, data in payloads.items():
        destination = (target / name).resolve()
        destination.relative_to(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".bread-provider-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        try:
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    for name, expected in receipt["installed_files"].items():
        if digest(target / name) != expected:
            raise RuntimeError("Installed shader language-server verification failed")
    return dict(provider=manifest["provider"], version=manifest["version"], path=str(target),
                files=len(receipt["installed_files"]), bytes=sum(map(len, payloads.values())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--application", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name("provider.json").read_text(encoding="utf-8"))
    if args.verify_only:
        print(json.dumps(verify(args.application, manifest), indent=2))
    elif args.archive:
        print(json.dumps(deploy(args.archive, args.application, manifest), indent=2))
    else:
        parser.error("--archive is required when deploying")
