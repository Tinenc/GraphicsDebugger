from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from src.symbols.providers import (
    PROVENANCE,
    ProviderIntegrityError,
    ProviderOutputError,
    ProviderRegistryError,
    ProviderTimeoutError,
    UnknownProviderError,
    load_registry,
    run_provider,
    verify_attestation,
)


FAKE_PROVIDER = r'''from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--asset-root", required=True)
parser.add_argument("--manifest", required=True)
parser.add_argument("--build", required=True)
parser.add_argument("--provider-id", required=True)
parser.add_argument("--provider-version", required=True)
parser.add_argument("--mode", required=True)
args = parser.parse_args()

if args.mode == "timeout":
    time.sleep(5.0)
if args.mode == "child-timeout":
    marker = Path(args.asset_root) / "provider-child-survived.txt"
    child_code = (
        "from pathlib import Path; import time; time.sleep(0.8); "
        "Path(%r).write_text('alive', encoding='utf-8')" % str(marker)
    )
    subprocess.Popen([sys.executable, "-c", child_code])
    time.sleep(5.0)

manifest_path = Path(args.manifest)
blob = b"provider-owned-compiled-shader"
blob_path = manifest_path.parent / "shader.bin"
blob_path.write_bytes(blob)
relative_blob = "../outside.bin" if args.mode == "escape" else "shader.bin"
manifest = {
    "schema": "renderdoc-ultra-symbols/v1",
    "schemaVersion": 1,
    "buildId": args.build,
    "provider": {
        "providerId": args.provider_id,
        "version": args.provider_version,
    },
    "metadata": {
        "inheritedSecret": os.environ.get("RENDERDOC_ULTRA_TEST_SECRET"),
    },
    "shaders": [
        {
            "path": "Game/Shaders/Hero.shader",
            "name": "Hero/Forward",
            "stage": "pixel",
            "entryPoint": "main",
            "blobPath": relative_blob,
            "rawSha256": hashlib.sha256(blob).hexdigest(),
        }
    ],
}
if args.mode == "self-trust":
    manifest["shaders"][0]["trusted"] = True
manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
print("provider-ok")
'''


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TrustedProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.assets = self.base / "assets"
        self.config = self.base / "config"
        self.tools = self.base / "tools"
        self.assets.mkdir()
        self.config.mkdir()
        self.tools.mkdir()
        self.script = self.tools / "fake_provider.py"
        self.script.write_text(FAKE_PROVIDER, encoding="utf-8")
        self.registry_path = self.config / "trusted_providers.v1.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _entry(
        self,
        provider_id: str,
        mode: str,
        *,
        timeout: float = 5.0,
        executable: Path | None = None,
        executable_hash: str | None = None,
        include_script_hash: bool = True,
    ) -> dict:
        selected_executable = executable or Path(sys.executable)
        selected_hash = executable_hash or file_hash(selected_executable)
        fixed_files = (
            {str(self.script): file_hash(self.script)} if include_script_hash else {}
        )
        return {
            "providerId": provider_id,
            "version": "1.2.3",
            "executable": str(selected_executable),
            "sha256": selected_hash,
            "argsTemplate": [
                str(self.script),
                "--asset-root",
                "{assetRoot}",
                "--manifest",
                "{manifestPath}",
                "--build",
                "{buildId}",
                "--provider-id",
                provider_id,
                "--provider-version",
                "1.2.3",
                "--mode",
                mode,
            ],
            "supportedSchemas": ["renderdoc-ultra-symbols/v1"],
            "buildPatterns": ["game-*"],
            "timeoutSeconds": timeout,
            "fixedFileHashes": fixed_files,
        }

    def _write_registry(self, entries: list[dict]) -> None:
        self.registry_path.write_text(
            json.dumps(
                {
                    "schema": "renderdoc-ultra-provider-registry/v1",
                    "schemaVersion": 1,
                    "providers": entries,
                }
            ),
            encoding="utf-8",
        )

    def _run(self, provider_id: str, *, build_id: str = "game-42"):
        with patch(
            "src.symbols.providers.default_registry_path",
            return_value=self.registry_path,
        ):
            return run_provider(provider_id, self.assets, build_id=build_id)

    def test_runs_only_registered_fixed_command_and_attests_in_memory(self) -> None:
        self._write_registry([self._entry("fixture", "normal")])
        registry = load_registry(self.registry_path)
        self.assertIn("fixture", registry.providers)
        previous = os.environ.get("RENDERDOC_ULTRA_TEST_SECRET")
        os.environ["RENDERDOC_ULTRA_TEST_SECRET"] = "must-not-leak"
        try:
            result = self._run("fixture")
        finally:
            if previous is None:
                os.environ.pop("RENDERDOC_ULTRA_TEST_SECRET", None)
            else:
                os.environ["RENDERDOC_ULTRA_TEST_SECRET"] = previous

        self.assertTrue(verify_attestation(result))
        self.assertEqual(result.attestation.provider_id, "fixture")
        self.assertEqual(result.attestation.provider_version, "1.2.3")
        self.assertEqual(result.attestation.provenance, PROVENANCE)
        self.assertEqual(
            result.attestation.provider_binary_sha256,
            file_hash(Path(sys.executable)),
        )
        self.assertEqual(result.manifest["buildId"], "game-42")
        self.assertIsNone(result.manifest["metadata"]["inheritedSecret"])
        self.assertEqual(len(result.artifacts), 1)
        self.assertEqual(result.artifacts[0].relative_path, "shader.bin")
        self.assertEqual(result.stdout.strip(), "provider-ok")

        # The independent output directory is gone, while the bounded artifact
        # remains available as attested in-memory bytes.
        self.assertEqual(result.artifacts[0].data, b"provider-owned-compiled-shader")

    def test_rejects_unknown_provider_id(self) -> None:
        self._write_registry([self._entry("fixture", "normal")])
        registry = load_registry(self.registry_path)
        self.assertIn("fixture", registry.providers)
        with self.assertRaises(UnknownProviderError):
            self._run("not-registered")

    def test_rejects_tampered_executable_before_launch(self) -> None:
        fake_executable = self.tools / "provider.exe"
        fake_executable.write_bytes(b"registered executable bytes")
        registered_hash = file_hash(fake_executable)
        self._write_registry(
            [
                self._entry(
                    "tampered",
                    "normal",
                    executable=fake_executable,
                    executable_hash=registered_hash,
                )
            ]
        )
        registry = load_registry(self.registry_path)
        self.assertIn("tampered", registry.providers)
        fake_executable.write_bytes(b"modified executable bytes")
        with self.assertRaises(ProviderIntegrityError):
            self._run("tampered")

    def test_rejects_provider_output_path_escape(self) -> None:
        self._write_registry([self._entry("escape", "escape")])
        registry = load_registry(self.registry_path)
        self.assertIn("escape", registry.providers)
        with self.assertRaises(ProviderOutputError):
            self._run("escape")

    def test_rejects_provider_self_declared_trust(self) -> None:
        self._write_registry([self._entry("self-trust", "self-trust")])
        registry = load_registry(self.registry_path)
        self.assertIn("self-trust", registry.providers)
        with self.assertRaises(ProviderOutputError):
            self._run("self-trust")

    def test_kills_provider_after_registered_timeout(self) -> None:
        self._write_registry([self._entry("slow", "timeout", timeout=0.1)])
        registry = load_registry(self.registry_path)
        self.assertIn("slow", registry.providers)
        started = time.monotonic()
        with self.assertRaises(ProviderTimeoutError):
            self._run("slow")
        self.assertLess(time.monotonic() - started, 3.0)

    def test_timeout_terminates_provider_process_tree(self) -> None:
        self._write_registry(
            [self._entry("child-slow", "child-timeout", timeout=0.1)]
        )
        marker = self.assets / "provider-child-survived.txt"
        with self.assertRaises(ProviderTimeoutError):
            self._run("child-slow")
        time.sleep(1.1)
        self.assertFalse(marker.exists())

    def test_shell_and_batch_executables_are_rejected(self) -> None:
        for filename in ("cmd.exe", "powershell.exe", "sh", "unsafe.cmd"):
            with self.subTest(filename=filename):
                executable = self.tools / filename
                executable.write_bytes(b"not a trusted provider host")
                self._write_registry(
                    [
                        self._entry(
                            "unsafe-shell",
                            "normal",
                            executable=executable,
                            executable_hash=file_hash(executable),
                        )
                    ]
                )
                with self.assertRaisesRegex(
                    ProviderRegistryError, "command shell"
                ):
                    load_registry(self.registry_path)

    def test_interpreter_inline_module_and_pre_script_placeholders_are_rejected(self) -> None:
        for unsafe_argument in ("-cprint('unsafe')", "-msuspicious_module"):
            with self.subTest(argument=unsafe_argument):
                entry = self._entry("unsafe-inline", "normal")
                entry["argsTemplate"] = [unsafe_argument, str(self.script)]
                self._write_registry([entry])
                with self.assertRaisesRegex(
                    ProviderRegistryError, "pinned script file"
                ):
                    load_registry(self.registry_path)

        entry = self._entry("unsafe-placeholder", "normal")
        entry["argsTemplate"] = ["{buildId}", str(self.script)]
        self._write_registry([entry])
        with self.assertRaisesRegex(
            ProviderRegistryError, "placeholders must occur after"
        ):
            load_registry(self.registry_path)

    def test_timeout_must_be_finite(self) -> None:
        entry = self._entry("infinite-timeout", "normal")
        self._write_registry([entry])
        payload = self.registry_path.read_text(encoding="utf-8")
        payload = payload.replace('"timeoutSeconds": 5.0', '"timeoutSeconds": 1e999')
        self.registry_path.write_text(payload, encoding="utf-8")
        with self.assertRaisesRegex(ProviderRegistryError, "timeoutSeconds"):
            load_registry(self.registry_path)

    def test_compound_literal_file_argument_must_be_hash_pinned(self) -> None:
        rules = self.tools / "provider-rules.json"
        rules.write_text("{}", encoding="utf-8")
        entry = self._entry("unpinned-rules", "normal")
        entry["argsTemplate"].append("--rules=%s" % rules)
        self._write_registry([entry])
        with self.assertRaisesRegex(ProviderRegistryError, "must be pinned"):
            load_registry(self.registry_path)


if __name__ == "__main__":
    unittest.main()
