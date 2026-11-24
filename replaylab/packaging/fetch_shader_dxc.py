"""Acquire pinned official DXC, then exercise DXIL and SPIR-V code generation.

The Windows SDK DXC advertises SPIR-V flags even when its backend is disabled.
Neither -help nor a version string constitutes a capability check. Installation
requires an explicit output directory; this script never locates or modifies a
running Bread application's compiler directory automatically.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile


MANIFEST = Path(__file__).with_name("shader_dxc_manifest.json")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _run(arguments, cwd):
    options = dict(cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, timeout=60)
    if os.name == "nt":
        options["creationflags"] = 0x08000000
    completed = subprocess.run([str(value) for value in arguments], **options)
    if completed.returncode:
        raise RuntimeError((completed.stdout + completed.stderr).decode("utf-8", errors="replace"))
    return completed.stdout.decode("utf-8", errors="replace")


def verify_compiler(executable):
    executable = Path(executable).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="bread-dxc-capability-") as scratch:
        root = Path(scratch)
        source = root / "probe.hlsl"
        source.write_text("cbuffer C:register(b0){float4 tint;};\n"
                          "float4 SourcePS():SV_Target0{return tint;}\n", encoding="utf-8")
        common = [executable, "-T", "ps_6_0", "-E", "SourcePS", source]
        dxil, spirv = root / "probe.dxil", root / "probe.spv"
        _run(common + ["-Fo", dxil], root)
        payload = dxil.read_bytes()
        if len(payload) < 32 or payload[:4] != b"DXBC" or payload[4:20] == bytes(16):
            raise RuntimeError("DXC failed to emit a validated, signed DXIL container")
        _run(common + ["-spirv", "-fspv-target-env=vulkan1.0", "-fspv-entrypoint-name=CapturedPS",
                       "-fvk-b-shift", "4", "0", "-Fo", spirv], root)
        payload = spirv.read_bytes()
        if len(payload) < 20 or len(payload) % 4 or payload[:4] != b"\x03\x02\x23\x07":
            raise RuntimeError("DXC failed to emit SPIR-V")
        words = struct.unpack("<%dI" % (len(payload) // 4), payload)
        entries, binding, index = [], False, 5
        while index < len(words):
            count, opcode = words[index] >> 16, words[index] & 65535
            if not count or index + count > len(words):
                raise RuntimeError("Invalid DXC SPIR-V instruction")
            if opcode == 15:
                entries.append(struct.pack("<%dI" % (count - 3), *words[index + 3:index + count]).split(b"\0", 1)[0])
            if opcode == 71 and count == 4 and words[index + 2:index + 4] == (33, 4):
                binding = True
            index += count
        if entries != [b"CapturedPS"] or not binding:
            raise RuntimeError("DXC failed exported entry point or Vulkan binding-shift verification")
        return {"dxilValidatedAndSigned": True, "spirv": True, "exportedEntryPoint": True,
                "bindingShift": True, "version": _run([executable, "--version"], root).strip()}


def acquire(output, platform_name, archive=None):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    selected = manifest["platforms"][platform_name]
    output = Path(output).absolute()
    if output.is_symlink() or output.exists() and any(output.iterdir()):
        raise RuntimeError("DXC destination must be a new or empty directory")
    with tempfile.TemporaryDirectory(prefix="bread-dxc-download-") as scratch:
        temporary = Path(scratch)
        if archive:
            package = Path(archive).resolve(strict=True)
        else:
            package = temporary / "archive"
            with urllib.request.urlopen(selected["url"], timeout=60) as response, package.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        if digest(package) != selected["sha256"]:
            raise RuntimeError("Official DXC archive SHA-256 differs from the reviewed release pin")
        stage = temporary / "stage"
        stage.mkdir()
        def required(name):
            return name in selected["files"] or ("/" not in name and
                any(token in name.lower() for token in ("license", "notice")))
        if zipfile.is_zipfile(str(package)):
            with zipfile.ZipFile(str(package)) as bundle:
                contents = {name.replace("\\", "/"): bundle.read(name) for name in bundle.namelist()
                            if not name.endswith("/") and required(name.replace("\\", "/"))}
        else:
            with tarfile.open(str(package), "r:gz") as bundle:
                # Read bytes directly, never extract archive-controlled paths,
                # links or devices. The small fixed mapping chooses outputs.
                contents = {member.name.lstrip("./"): bundle.extractfile(member).read()
                            for member in bundle.getmembers() if member.isfile() and required(member.name.lstrip("./"))}
        for source, destination in selected["files"].items():
            path = stage / destination
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents[source])
        licenses = {name: data for name, data in contents.items()
                    if "/" not in name and any(token in name.lower() for token in ("license", "notice"))}
        if not licenses:
            raise RuntimeError("Official DXC archive has no license/notice files")
        for name, data in licenses.items():
            (stage / name).write_bytes(data)
        executable = stage / selected["executable"]
        executable.chmod(0o755)
        capabilities = verify_compiler(executable)
        files = {path.relative_to(stage).as_posix(): digest(path) for path in stage.rglob("*") if path.is_file()}
        if selected.get("fileSha256") and files != selected["fileSha256"]:
            raise RuntimeError("DXC files differ from the validated official release")
        receipt = {"schema": "bread-dxc-installed/v1", "release": selected.get("release", manifest["release"]),
                   "source": selected.get("source", manifest["source"]), "platform": platform_name,
                   "archiveSha256": selected["sha256"], "files": files, "capabilities": capabilities}
        (stage / "bread-dxc-release.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        output.mkdir(parents=True, exist_ok=True)
        for item in stage.iterdir():
            if item.is_dir():
                shutil.copytree(str(item), str(output / item.name))
            else:
                shutil.copy2(str(item), str(output / item.name))
        return receipt


def verify_bundle(directory, platform_name, codegen=True):
    """Verify a shipped/cache directory against source-controlled file pins."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    selected = manifest["platforms"][platform_name]
    directory = Path(directory).absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("Pinned DXC bundle directory is missing or redirected")
    expected_files = set(selected["fileSha256"]) | {"bread-dxc-release.json"}
    actual_files = {path.relative_to(directory).as_posix() for path in directory.rglob("*")
                    if path.is_file() or path.is_symlink()}
    if actual_files != expected_files:
        raise RuntimeError("Pinned DXC bundle contains missing or unexpected files")
    for relative, expected in selected["fileSha256"].items():
        path = directory / relative
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise RuntimeError("Pinned DXC bundle file changed or is missing: " + relative)
    receipt = json.loads((directory / "bread-dxc-release.json").read_text(encoding="utf-8"))
    if (receipt.get("schema") != "bread-dxc-installed/v1" or
            receipt.get("release") != selected.get("release", manifest["release"]) or
            receipt.get("archiveSha256") != selected["sha256"] or
            receipt.get("platform") != platform_name or receipt.get("files") != selected["fileSha256"]):
        raise RuntimeError("Pinned DXC bundle receipt disagrees with the release manifest")
    if codegen:
        verify_compiler(directory / selected["executable"])
    return receipt


def ensure_cache(directory, platform_name, archive=None):
    directory = Path(directory).absolute()
    if directory.is_dir() and any(directory.iterdir()):
        return verify_bundle(directory, platform_name)
    return acquire(directory, platform_name, archive)


def stage_bundle(cache, destination, platform_name):
    """Copy the complete fixed bin/lib layout; never adopt a PATH compiler."""
    verify_bundle(cache, platform_name, codegen=False)
    destination = Path(destination).absolute()
    if destination.exists() and any(destination.iterdir()):
        return verify_bundle(destination, platform_name)
    if destination.is_symlink():
        raise RuntimeError("DXC staging destination is redirected")
    if destination.exists():
        # copytree requires a missing destination on supported Python 3.6.
        destination.rmdir()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(cache), str(destination))
    return verify_bundle(destination, platform_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory")
    parser.add_argument("--archive", help="Offline official release archive; SHA-256 remains mandatory")
    parser.add_argument("--platform", choices=("windows-x64", "linux-x64"),
                        default="windows-x64" if os.name == "nt" else "linux-x64")
    parser.add_argument("--verify-executable", help="Verify real code generation without installing anything")
    parser.add_argument("--verify-directory", help="Check pinned files, licenses and real code generation")
    parser.add_argument("--ensure-cache", help="Acquire the reviewed archive only when this cache is empty")
    parser.add_argument("--stage-directory", help="Stage a complete verified --ensure-cache bundle in this directory")
    args = parser.parse_args()
    if args.verify_executable:
        result = verify_compiler(args.verify_executable)
    elif args.verify_directory:
        result = verify_bundle(args.verify_directory, args.platform)
    elif args.ensure_cache:
        result = ensure_cache(args.ensure_cache, args.platform, args.archive)
        if args.stage_directory:
            result = stage_bundle(args.ensure_cache, args.stage_directory, args.platform)
    else:
        if args.stage_directory:
            parser.error("--stage-directory requires --ensure-cache")
        if not args.output_directory:
            parser.error("--output-directory is required for acquisition")
        result = acquire(args.output_directory, args.platform, args.archive)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
