from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest

from replaylab_agent.cli import _credential_command, _parser, _profile_command
from replaylab_agent.credential_store import (
    CredentialStoreError,
    normalize_api_credential,
)
from replaylab_agent.errors import AuthenticationError
from replaylab_agent.profiles import Profile, ProfileStore


class FakeCredentialStore:
    backend = "fake-secure-store"
    supported = True

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def read(self, profile_id: str) -> str | None:
        return self.values.get(profile_id)

    def write(self, profile_id: str, secret: str) -> None:
        self.values[profile_id] = secret

    def delete(self, profile_id: str) -> bool:
        return self.values.pop(profile_id, None) is not None


def _profiles(tmp_path) -> ProfileStore:
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(Profile("api-main", "openai", "model-a"))
    return store


def test_credential_cli_uses_stdin_and_never_serialises_secret(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    profiles = _profiles(tmp_path)
    credentials = FakeCredentialStore()
    emitted: list[dict] = []
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(b"sk-not-written-to-output")),
    )
    parser = _parser()

    store_args = parser.parse_args(
        [
            "profile",
            "credential",
            "store",
            "--profile",
            "api-main",
            "--api-key-stdin",
            "--json",
        ]
    )
    assert _credential_command(store_args, profiles, credentials) == 0
    assert credentials.values == {"api-main": "sk-not-written-to-output"}
    assert emitted[-1] == {
        "profile": "api-main",
        "backend": "fake-secure-store",
        "supported": True,
        "stored": True,
    }
    assert "sk-not-written-to-output" not in repr(emitted)

    status_args = parser.parse_args(
        ["profile", "credential", "status", "--profile", "api-main", "--json"]
    )
    assert _credential_command(status_args, profiles, credentials) == 0
    assert emitted[-1]["stored"] is True

    delete_args = parser.parse_args(
        ["profile", "credential", "delete", "--profile", "api-main", "--json"]
    )
    assert _credential_command(delete_args, profiles, credentials) == 0
    assert emitted[-1]["deleted"] is True
    assert credentials.values == {}


def test_credential_cli_normalizes_pasted_bearer_before_persistence(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    profiles = _profiles(tmp_path)
    credentials = FakeCredentialStore()
    monkeypatch.setattr(cli, "_print_machine_json", lambda value: None)
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(b"  Bearer\t saved-token  \n")),
    )
    args = _parser().parse_args(
        [
            "profile",
            "credential",
            "store",
            "--profile",
            "api-main",
            "--api-key-stdin",
            "--json",
        ]
    )

    assert _credential_command(args, profiles, credentials) == 0
    assert credentials.values == {"api-main": "saved-token"}


@pytest.mark.parametrize(
    "entered, expected",
    [
        ("raw-token", "raw-token"),
        (" Bearer raw-token ", "raw-token"),
        ("bEaReR\t raw-token", "raw-token"),
        ("", ""),
    ],
)
def test_api_credential_normalization(entered, expected) -> None:
    assert normalize_api_credential(entered, allow_empty=not entered) == expected


@pytest.mark.parametrize(
    "entered, allow_empty",
    [
        ("Bearer ", False),
        ("Bearer ", True),
        ("Bearer\nsecret", False),
        ("raw\rsecret", False),
        ("raw\0secret", False),
    ],
)
def test_api_credential_normalization_rejects_empty_or_injected_values(
    entered, allow_empty
) -> None:
    with pytest.raises(CredentialStoreError, match="single-line"):
        normalize_api_credential(entered, allow_empty=allow_empty)


def test_profile_resolution_prefers_environment_then_secure_store(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.credential_store as credential_store

    profile = _profiles(tmp_path).active()
    credentials = FakeCredentialStore()
    credentials.values[profile.id] = "Bearer stored-key"
    monkeypatch.setattr(
        credential_store, "default_credential_store", lambda: credentials
    )

    assert profile.resolve_api_key() == "stored-key"
    monkeypatch.setenv(profile.key_env, "Bearer environment-key")
    assert profile.resolve_api_key() == "environment-key"
    with pytest.raises(AuthenticationError, match="environment variable"):
        profile.resolve_api_key({})


def test_profile_remove_deletes_stale_credential(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    monkeypatch.setattr(cli, "_print_machine_json", lambda value: None)
    parser = _parser()
    credentials = FakeCredentialStore()

    remove_store = _profiles(tmp_path / "remove")
    credentials.values["api-main"] = "remove-me"
    remove_args = parser.parse_args(
        ["profile", "remove", "api-main", "--json"]
    )
    assert _profile_command(remove_args, remove_store, credentials) == 0
    assert "api-main" not in credentials.values


def test_same_provider_atomic_rebuild_preserves_saved_credential(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    monkeypatch.setattr(cli, "_print_machine_json", lambda value: None)
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(
        Profile(
            "azure-main",
            "azure-openai",
            model="deployment-a",
            models=["deployment-a"],
            default_model="deployment-a",
            base_url="https://example.openai.azure.com",
            options={"api_version": "2025-04-01-preview"},
        )
    )
    credentials = FakeCredentialStore()
    credentials.values["azure-main"] = "keep-for-same-provider"
    args = _parser().parse_args(
        [
            "profile",
            "update",
            "azure-main",
            "--replace-provider",
            "--preset",
            "azure-openai",
            "--base-url",
            "https://example.services.ai.azure.com/openai/v1",
            "--allowed-model",
            "deployment-a",
            "--default-model",
            "deployment-a",
            "--json",
        ]
    )
    assert _profile_command(args, store, credentials) == 0
    assert credentials.values["azure-main"] == "keep-for-same-provider"


def test_provider_replacement_deletes_stale_credential(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    monkeypatch.setattr(cli, "_print_machine_json", lambda value: None)
    parser = _parser()
    credentials = FakeCredentialStore()

    replace_store = _profiles(tmp_path / "replace")
    credentials.values["api-main"] = "old-provider-key"
    replace_args = parser.parse_args(
        [
            "profile",
            "update",
            "api-main",
            "--replace-provider",
            "--preset",
            "ollama",
            "--allowed-model",
            "model-a",
            "--default-model",
            "model-a",
            "--json",
        ]
    )
    assert _profile_command(replace_args, replace_store, credentials) == 0
    assert "api-main" not in credentials.values


def test_store_parser_requires_explicit_stdin_flag() -> None:
    parser = _parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["profile", "credential", "store", "--profile", "api-main"]
        )
