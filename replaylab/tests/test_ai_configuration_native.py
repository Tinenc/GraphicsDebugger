"""Execute the actual native configuration code without any AI Host process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

from replaylab_agent.catalog import PRESETS
from replaylab_agent.conversation import SUPPORTED_CONVERSATION_SCHEMAS
from replaylab_agent.profiles import Profile, ProfileStore

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def native_executable(tmp_path_factory):
    if sys.platform != "win32":
        pytest.skip("Windows native configuration build")
    directory = tmp_path_factory.mktemp("bread-native-settings")
    vswhere = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft Visual Studio/Installer/vswhere.exe"
    installation = subprocess.check_output([str(vswhere), "-latest", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], text=True).strip()
    if not installation:
        pytest.skip("Visual Studio C++ toolchain is unavailable")
    devcmd = Path(installation) / "Common7/Tools/VsDevCmd.bat"
    script = directory / "compiler-env.cmd"
    script.write_text(f'@echo off\ncall "{devcmd}" -no_logo -arch=x64 >nul\nif errorlevel 1 exit /b 1\nset\n', encoding="utf-8")
    values = subprocess.check_output(["cmd", "/d", "/u", "/c", str(script)], encoding="utf-16-le")
    environment = {key.upper(): value for key, value in os.environ.items()}
    environment.update({key.upper(): value for key, value in (line.split("=", 1) for line in values.splitlines() if "=" in line and not line.startswith("="))})
    environment["VSLANG"] = "1033"
    qt = ROOT / "qrenderdoc/3rdparty/qt"
    executable = directory / "native-settings.exe"
    args = [shutil.which("cl.exe", path=environment["PATH"]), "/nologo", "/EHsc", "/std:c++14", "/MD", "/utf-8", "/Zi",
            "/DQT_NO_CAST_FROM_ASCII", "/DQT_NO_CAST_TO_ASCII", "/DNOMINMAX",
            "/I" + str(ROOT / "qrenderdoc"), "/I" + str(qt / "include"),
            "/I" + str(qt / "include/QtCore"), "/I" + str(qt / "include/QtNetwork"),
            str(ROOT / "qrenderdoc/Windows/AI/AIConfiguration.cpp"),
            str(ROOT / "replaylab/tests/fixtures/ai_configuration_native.cpp"),
            "/Fe:" + str(executable), "/link", "/LIBPATH:" + str(qt / "x64/lib"),
            "Qt5Core.lib", "Qt5Network.lib", "advapi32.lib"]
    if os.environ.get("BREAD_SETTINGS_ASAN") == "1":
        args.insert(1, "/fsanitize=address")
    compiled = subprocess.run(args, env=environment, cwd=directory, encoding="utf-8", errors="replace", capture_output=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    environment["PATH"] = str(qt / "x64/bin") + os.pathsep + environment["PATH"]
    return executable, environment


@pytest.fixture
def native(native_executable, tmp_path):
    executable, environment = native_executable
    environment = {**environment, "APPDATA": str(tmp_path / "中文用户-Ω/roaming"), "LOCALAPPDATA": str(tmp_path / "local"),
                   "REPLAYLAB_AI_CLI": str(tmp_path / "missing-ai-host.exe")}

    def call(*args, key="", catalog=False):
        request = {"catalog": True} if catalog else {"args": list(args), "input": key}
        result = subprocess.run([str(executable)], input=json.dumps(request) + "\n", encoding="utf-8", capture_output=True, env=environment, timeout=10)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    call.path = tmp_path / "中文用户-Ω/roaming/ReplayLab/ai/profiles.json"
    call.local = tmp_path / "local"
    return call


def add(native, id="test-local", preset="ollama", model="model-a", *extra):
    return native("profile", "add", id, "--preset", preset, "--title", "离线连接", "--allowed-model", model, "--default-model", model, *extra, "--json")


def test_embedded_catalog_and_input_schemas_match_backend(native):
    rows = native(catalog=True)["catalog"]
    assert [row["id"] for row in rows] == [preset.id for preset in PRESETS]
    for row, preset in zip(rows, PRESETS):
        assert row["title"] == preset.title
        assert row["baseUrl"] == preset.base_url
        assert row["command"] == list(preset.command)
        assert row["authenticationUi"] == preset.authentication_ui
        assert row["supportsModelDiscovery"] == preset.supports_model_discovery
    assert add(native)["exitCode"] == 0
    row = json.loads(native("profile", "list", "--json")["output"])[0]
    assert row["runInputSchemas"] == list(SUPPORTED_CONVERSATION_SCHEMAS)


def test_offline_add_edit_and_default_are_readable_by_the_backend(native):
    assert add(native)["exitCode"] == 0
    assert add(native, "second")["exitCode"] == 0
    result = native("profile", "update", "test-local", "--preset", "ollama", "--title", "改名", "--allowed-model", "model-b", "--default-model", "model-b", "--json")
    assert result["exitCode"] == 0, result
    assert native("profile", "select", "second", "--json")["exitCode"] == 0
    store = ProfileStore(native.path).load()
    assert store.active_profile == "second"
    assert store.profiles["test-local"].title == "改名"
    assert store.profiles["test-local"].models == ["model-b"]


def test_legacy_profile_and_unrelated_metadata_survive_edit(native):
    native.path.parent.mkdir(parents=True)
    profile = {"id": "old", "preset": "ollama", "model": "legacy", "options": {"temperature": 0.25}}
    native.path.write_text(json.dumps({"schema": "replaylab-ai-profiles/v1", "activeProfile": "old", "profiles": [profile], "extension": {"keep": True}}))
    rows = json.loads(native("profile", "list", "--json")["output"])
    assert rows[0]["models"] == ["legacy"]
    assert rows[0]["defaultModel"] == "legacy"
    assert add(native, "other")["exitCode"] == 0
    payload = json.loads(native.path.read_text())
    assert payload["profiles"][0] == profile
    assert payload["extension"] == {"keep": True}
    assert native("profile", "update", "old", "--title", "旧连接", "--json")["exitCode"] == 0
    restored = ProfileStore(native.path).load().profiles["old"]
    assert restored.model == "legacy"
    assert restored.options == {"temperature": 0.25}


def test_backend_and_gui_writers_merge_unrelated_connections(native):
    assert add(native)["exitCode"] == 0
    stale = ProfileStore(native.path).load()
    assert add(native, "second")["exitCode"] == 0
    stale.add(Profile(id="third", preset="ollama", model="m"))
    stale.save()
    assert set(ProfileStore(native.path).load().profiles) == {"test-local", "second", "third"}
    assert native("profile", "update", "second", "--title", "new", "--json")["exitCode"] == 0
    assert set(ProfileStore(native.path).load().profiles) == {"test-local", "second", "third"}


def test_gui_honours_backend_file_transaction(native):
    from replaylab_agent.configuration_io import file_transaction
    assert add(native)["exitCode"] == 0
    before = native.path.read_bytes()
    with file_transaction(native.path):
        result = add(native, "blocked")
        assert result["exitCode"] != 0
        assert native.path.read_bytes() == before
    assert add(native, "after")["exitCode"] == 0


@pytest.mark.parametrize("content", ["broken", '{"schema":"future","profiles":[]}', '{"schema":"replaylab-ai-profiles/v2","profiles":[{"id":"bad/../id"}]}'])
def test_damaged_or_newer_configuration_is_preserved(native, content):
    native.path.parent.mkdir(parents=True)
    native.path.write_text(content)
    assert add(native)["exitCode"] != 0
    assert native.path.read_text() == content


@pytest.mark.parametrize("url", ["http://remote.example/v1", "https://key:secret@example.org", "https://example.org?api_key=secret", "garbage"])
def test_invalid_endpoint_cannot_be_saved(native, url):
    result = add(native, "invalid", "openai", "gpt-test", "--base-url", url)
    assert result["exitCode"] != 0
    assert not native.path.exists()
    assert "secret" not in result["diagnostic"]


def test_os_credential_round_trip_is_compatible_with_backend(native):
    from replaylab_agent.credential_store import WindowsCredentialStore
    id = "native-test-" + uuid.uuid4().hex
    assert add(native, id, "openai")["exitCode"] == 0
    try:
        assert native("profile", "credential", "store", "--profile", id, "--json", key="Bearer test-only-key")["exitCode"] == 0
        vault = WindowsCredentialStore()
        assert vault.read(id) == "test-only-key"
        assert "test-only-key" not in native.path.read_text()
        assert native("profile", "credential", "delete", "--profile", id, "--json")["exitCode"] == 0
        assert vault.read(id) is None
    finally:
        WindowsCredentialStore().delete(id)


def test_subscription_can_be_saved_without_starting_login(native):
    result = native("profile", "add", "offline-subscription", "--preset", "codex", "--json")
    assert result["exitCode"] == 0, result
    profile = ProfileStore(native.path).load().profiles["offline-subscription"]
    assert profile.preset == "codex"


def test_changed_provider_clears_old_secret_and_local_state(native):
    id = "native-test-" + uuid.uuid4().hex
    assert native("profile", "add", id, "--preset", "codex", "--json")["exitCode"] == 0
    state = native.local / "ReplayLab/codex" / id
    state.mkdir(parents=True)
    (state / "test-only-state").write_text("private")
    result = native("profile", "update", id, "--replace-provider", "--preset", "ollama", "--allowed-model", "m", "--default-model", "m", "--json")
    assert result["exitCode"] == 0, result
    assert not state.exists()
    assert ProfileStore(native.path).load().profiles[id].preset == "ollama"


def test_generated_catalog_is_checked_by_release_gate():
    result = subprocess.run([sys.executable, str(ROOT / "util/ai/generate_provider_catalog.py"), "--check"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("preset", PRESETS, ids=lambda preset: preset.id)
def test_every_embedded_provider_can_be_saved_for_the_canonical_backend(native, preset):
    arguments = ["profile", "add", "provider-test", "--preset", preset.id]
    if preset.kind == "model":
        arguments += ["--allowed-model", "test-model", "--default-model", "test-model"]
    if preset.id == "azure-openai":
        arguments += ["--base-url", "https://example.openai.azure.com/openai/v1"]
    if preset.id == "custom-acp":
        arguments += ["--command-json", '["test-agent"]', "--option", "allow_unsandboxed_agent=true"]
    result = native(*arguments, "--json")
    assert result["exitCode"] == 0, result
    profile = ProfileStore(native.path).load().profiles["provider-test"]
    assert profile.preset == preset.id
    rows = json.loads(native("profile", "list", "--json")["output"])
    assert rows[0]["endpoint"] == profile.endpoint
    assert rows[0]["apiKeyEnv"] == profile.key_env
    assert rows[0]["command"] == profile.argv


@pytest.mark.parametrize("flag", ["--api-key", "--apiKey", "--accessToken", "--header-value"])
def test_custom_command_credentials_cannot_enter_profile_json(native, flag):
    result = native("profile", "add", "bad-command", "--preset", "custom-acp", "--command-json", json.dumps(["agent", flag, "secret-must-not-persist"]), "--json")
    assert result["exitCode"] != 0
    assert not native.path.exists()


def test_removing_a_connection_is_local_and_keeps_other_profiles(native):
    assert add(native)["exitCode"] == 0
    assert add(native, "other")["exitCode"] == 0
    result = native("profile", "remove", "test-local", "--json")
    assert result["exitCode"] == 0, result
    store = ProfileStore(native.path).load()
    assert list(store.profiles) == ["other"]
    assert store.active_profile == "other"
