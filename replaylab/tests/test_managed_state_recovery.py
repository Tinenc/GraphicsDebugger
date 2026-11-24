from pathlib import Path
import json
import os
import pytest
from replaylab_agent import managed_state as state
from replaylab_agent.provider_lease import profile_lease
from replaylab_agent.profiles import Profile, ProfileStore


def test_denied_managed_home_recovers_without_copying_login(tmp_path, monkeypatch):
    env = {"LOCALAPPDATA": str(tmp_path), "REPLAYLAB_MANAGED_CODEX_HOME": "1"}
    primary = tmp_path / "ReplayLab/codex/connection-test"
    primary.mkdir(parents=True)
    (primary / "auth.json").write_text("untouched-token")
    probe = state._probe
    def denied(path):
        if path == primary:
            raise PermissionError("access denied")
        probe(path)
    monkeypatch.setattr(state, "_probe", denied)
    selected = state.prepare_codex_home(primary, env)
    assert selected != primary
    assert state.selected_codex_home(primary, env) == selected
    assert not (selected / "auth.json").exists()
    assert (primary / "auth.json").read_text() == "untouched-token"
    assert not list(selected.glob(".bread-home-*.pending"))


def test_custom_home_never_redirects(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_probe", lambda path: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(PermissionError):
        state.prepare_codex_home(tmp_path / "custom", {"LOCALAPPDATA": str(tmp_path), "REPLAYLAB_MANAGED_CODEX_HOME": "1"})


def test_two_stale_connection_catalogs_merge_only_their_own_changes(tmp_path):
    path = tmp_path / "profiles.json"
    seed = ProfileStore(path); seed.add(Profile("first", "ollama", model="first")); seed.save()
    a = ProfileStore(path).load(); b = ProfileStore(path).load()
    a.add(Profile("second", "ollama", model="second")); a.save()
    b.add(Profile("third", "ollama", model="third")); b.save()
    assert set(ProfileStore(path).load().profiles) == {"first", "second", "third"}
    a.profiles.pop("first"); a.active_profile = "second"; a.save()
    b.save()  # An unchanged stale catalog must not resurrect a removed profile.
    assert set(ProfileStore(path).load().profiles) == {"second", "third"}


def test_concurrent_provider_readers_prevent_destructive_cleanup(tmp_path):
    config = tmp_path / "profiles.json"
    with profile_lease(config, "connection-test"):
        with profile_lease(config, "connection-test"):
            with pytest.raises(OSError):
                with profile_lease(config, "connection-test", cleanup=True):
                    pytest.fail("an active login directory cannot be deleted")
    with profile_lease(config, "connection-test", cleanup=True):
        pass
