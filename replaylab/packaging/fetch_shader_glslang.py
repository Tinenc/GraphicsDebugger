"""Acquire and verify Bread's pinned Linux GLSL compiler, without installing a package.

Only the reviewed Ubuntu archive's compiler and copyright file are extracted.
The glslang/SPIRV compiler libraries are statically linked. The declared Linux
C/C++ runtime baseline remains a host requirement, not a copied workstation lib.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import urllib.request


MANIFEST = Path(__file__).with_name("shader_glslang_manifest.json")
RECEIPT = "bread-glslang-release.json"


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _run(arguments, directory):
    environment = dict(os.environ)
    # Prove that code generation/optimisation is self-contained: no external
    # spirv-opt/spirv-as or other workstation compiler can be launched via PATH.
    environment["PATH"] = ""
    result = subprocess.run([str(value) for value in arguments], cwd=str(directory),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=60, env=environment)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr).decode("utf-8", errors="replace"))
    return result.stdout.decode("utf-8", errors="replace")


def _entries(raw):
    if len(raw) < 20 or len(raw) % 4 or raw[:4] != b"\x03\x02\x23\x07":
        raise RuntimeError("glslang did not emit a SPIR-V module")
    words = struct.unpack("<%dI" % (len(raw) // 4), raw)
    names, index = [], 5
    while index < len(words):
        count, opcode = words[index] >> 16, words[index] & 65535
        if not count or index + count > len(words):
            raise RuntimeError("glslang emitted invalid SPIR-V instructions")
        if opcode == 15:
            names.append(struct.pack("<%dI" % (count - 3), *words[index + 3:index + count])
                         .split(b"\0", 1)[0].decode("utf-8"))
        index += count
    return names


def verify_compiler(executable):
    executable = Path(executable).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="bread-glslang-capability-") as scratch:
        root = Path(scratch)
        (root / "values.glsl").write_text("#define BREAD_SCALE 2.0\n", encoding="utf-8")
        source = root / "probe.frag"
        source.write_text('#version 450\n#extension GL_GOOGLE_include_directive : require\n'
                          '#include "values.glsl"\n#ifndef BREAD_VALUE\n#error missing macro\n#endif\n'
                          '#if defined(VULKAN) && VULKAN != 100\n#error wrong Vulkan macro\n#endif\n'
                          '#if defined(GL_SPIRV) && GL_SPIRV != 100\n#error wrong GL macro\n#endif\n'
                          'layout(location=0) out vec4 color;\n'
                          'void main(){color=vec4(BREAD_VALUE*BREAD_SCALE,0,0,1);}\n', encoding="utf-8")
        common = [executable, "-S", "frag", "-DBREAD_VALUE=0.25", "-I" + str(root)]
        preprocessed = _run(common + ["-E", "-DVULKAN=100", source], root)
        if "0.25*2.0" not in "".join(preprocessed.split()) or "#include" in preprocessed:
            raise RuntimeError("glslang failed actual macro/include preprocessing")
        for flag, environment, name in (("-V", "vulkan1.0", "vulkan.spv"),
                                         ("-G", "opengl", "opengl.spv")):
            output = root / name
            _run(common + [flag, "--target-env", environment, "-Os", "--source-entrypoint", "main",
                           "-e", "CapturedPS", "-o", output, source], root)
            if _entries(output.read_bytes()) != ["CapturedPS"]:
                raise RuntimeError("glslang failed exported entry-point verification")
        return {"macroPreprocess": True, "includePreprocess": True, "vulkanGLSL": True,
                "openGLSPIRV": True, "exportedEntryPoint": True,
                "optimizedWithoutExternalTools": True,
                "version": _run([executable, "--version"], root).strip()}


def _manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def acquire(output, archive=None):
    manifest = _manifest()
    output = Path(output).absolute()
    if output.is_symlink() or output.exists() and any(output.iterdir()):
        raise RuntimeError("glslang destination must be a new or empty directory")
    with tempfile.TemporaryDirectory(prefix="bread-glslang-download-") as scratch:
        temporary = Path(scratch)
        if archive:
            package = Path(archive).resolve(strict=True)
        else:
            package = temporary / "package.deb"
            with urllib.request.urlopen(manifest["url"], timeout=60) as response, package.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        if digest(package) != manifest["sha256"]:
            raise RuntimeError("Official glslang archive SHA-256 differs from the reviewed pin")
        unpacker = shutil.which("dpkg-deb")
        if not unpacker:
            raise RuntimeError("Acquiring the pinned Ubuntu glslang archive requires dpkg-deb; a verified cache can be staged without it")
        # dpkg-deb supports the archive's zstd payload on Ubuntu 22.04. Read a
        # tar stream, never extract archive paths, symlinks or devices to disk.
        result = subprocess.run([unpacker, "--fsys-tarfile", str(package)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=60)
        if result.returncode or len(result.stdout) > 64 * 1024 * 1024:
            raise RuntimeError("Reading the pinned glslang archive failed: " + result.stderr.decode("utf-8", errors="replace"))
        stage = temporary / "stage"
        stage.mkdir()
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as bundle:
            for item in bundle.getmembers():
                name = item.name[2:] if item.name.startswith("./") else item.name
                if item.isfile() and name in manifest["files"]:
                    target = stage / manifest["files"][name]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(bundle.extractfile(item).read())
        (stage / manifest["executable"]).chmod(0o755)
        actual = {path.relative_to(stage).as_posix(): digest(path) for path in stage.rglob("*") if path.is_file()}
        if actual != manifest["fileSha256"]:
            raise RuntimeError("glslang files differ from the reviewed archive")
        receipt = {"schema": "bread-glslang-installed/v1", "platform": manifest["platform"],
                   "release": manifest["release"], "source": manifest["source"],
                   "archiveSha256": manifest["sha256"], "files": actual,
                   "runtimeRequirements": manifest["runtimeRequirements"],
                   "capabilities": verify_compiler(stage / manifest["executable"])}
        (stage / RECEIPT).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        if output.exists():
            output.rmdir()  # Only the empty directory accepted above.
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(stage), str(output))
        return receipt


def verify_bundle(directory, codegen=True):
    manifest = _manifest()
    directory = Path(directory).absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("Pinned glslang bundle is missing or redirected")
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*")
              if path.is_file() or path.is_symlink()}
    if actual != set(manifest["fileSha256"]) | {RECEIPT}:
        raise RuntimeError("Pinned glslang bundle contains missing or unexpected files")
    for relative, expected in manifest["fileSha256"].items():
        path = directory / relative
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise RuntimeError("Pinned glslang bundle file changed or is missing: " + relative)
    receipt = json.loads((directory / RECEIPT).read_text(encoding="utf-8"))
    if (receipt.get("schema") != "bread-glslang-installed/v1" or
            receipt.get("release") != manifest["release"] or receipt.get("archiveSha256") != manifest["sha256"] or
            receipt.get("platform") != manifest["platform"] or receipt.get("files") != manifest["fileSha256"]):
        raise RuntimeError("Pinned glslang receipt disagrees with the release manifest")
    if codegen:
        verify_compiler(directory / manifest["executable"])
    return receipt


def ensure_cache(directory, archive=None):
    directory = Path(directory).absolute()
    if directory.is_dir() and any(directory.iterdir()):
        return verify_bundle(directory)
    return acquire(directory, archive)


def stage_bundle(cache, destination):
    verify_bundle(cache, codegen=False)
    destination = Path(destination).absolute()
    if destination.is_symlink():
        raise RuntimeError("glslang staging destination is redirected")
    if destination.exists() and any(destination.iterdir()):
        return verify_bundle(destination)
    if destination.exists():
        destination.rmdir()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(cache), str(destination))
    return verify_bundle(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory")
    parser.add_argument("--archive", help="Offline pinned Ubuntu glslang-tools deb")
    parser.add_argument("--verify-directory")
    parser.add_argument("--ensure-cache")
    parser.add_argument("--stage-directory")
    args = parser.parse_args()
    if args.verify_directory:
        result = verify_bundle(args.verify_directory)
    elif args.ensure_cache:
        result = ensure_cache(args.ensure_cache, args.archive)
        if args.stage_directory:
            result = stage_bundle(args.ensure_cache, args.stage_directory)
    else:
        if not args.output_directory or args.stage_directory:
            parser.error("Use --output-directory to acquire, or --ensure-cache with --stage-directory")
        result = acquire(args.output_directory, args.archive)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
