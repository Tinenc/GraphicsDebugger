"""Offline tests for the opt-in, bounded real-provider validation harness."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from replaylab_agent.connectors.langchain_models import build_chat_model
from replaylab_agent.errors import ConfigurationError
from replaylab_agent.profiles import Profile

_SPEC = importlib.util.spec_from_file_location(
    "validate_vendor_matrix", Path(__file__).resolve().parents[1] / "packaging" / "validate_vendor_matrix.py")
matrix = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(matrix)


def test_call_budget_rejects_before_real_langchain_model_generation():
    budget = matrix.ModelCallBudget(2)
    model = FakeMessagesListChatModel(responses=[AIMessage(content="one"), AIMessage(content="two"),
                                               AIMessage(content="must not run")], callbacks=[budget])
    assert model.invoke("hello").content == "one"
    assert model.invoke("hello").content == "two"
    with pytest.raises(ConfigurationError, match="budget exhausted"):
        model.invoke("hello")
    assert model.i == 2
    assert budget.calls == 2


def test_model_constructor_retry_override_and_token_limit_reach_sdk():
    profile = Profile(id="local", preset="ollama", model="fixed-model", options={"max_output_tokens": 1200})
    with mock.patch("replaylab_agent.connectors.langchain_models._load") as load:
        build_chat_model(profile, max_retries=0)
    kwargs = load.return_value.ChatOpenAI.call_args.kwargs
    assert kwargs["max_retries"] == 0
    assert kwargs["max_tokens"] == 1200
    assert kwargs["model"] == "fixed-model"
    assert profile.options == {"max_output_tokens": 1200}


def test_required_thinking_helper_failure_does_not_silently_disable_thinking():
    profile = Profile(id="local", preset="ollama", model="fixed-model")
    with mock.patch("replaylab_agent.native_vendor.thinking_budget", side_effect=ConfigurationError("bad thinking")):
        with pytest.raises(ConfigurationError, match="bad thinking"):
            build_chat_model(profile)


@pytest.mark.parametrize("value", [-1, True, 0.5, 11])
def test_invalid_retry_override_fails_before_sdk(value):
    with pytest.raises(ConfigurationError, match="max_retries"):
        build_chat_model(Profile(id="local", preset="ollama", model="model"), max_retries=value)


def test_inventory_never_serializes_endpoint_or_key():
    profile = Profile(id="local", preset="custom-openai", model="exact", base_url="https://private.invalid")
    with mock.patch.object(Profile, "resolve_api_key", return_value="private-secret-value"):
        value = matrix.inventory(SimpleNamespace(profiles={"local": profile}))
    encoded = json.dumps(value)
    assert "private" not in encoded
    assert value[0]["credentialAvailable"] is True
    assert set(value[0]) == {"id", "kind", "preset", "model", "credentialAvailable", "runtimeInstalled"}


def test_non_frozen_context_rejected_before_bridge(tmp_path):
    path = tmp_path / "context.json"
    path.write_text("{}", encoding="utf-8")
    with mock.patch.object(matrix, "_read_context") as read:
        with pytest.raises(ConfigurationError, match="frozen"):
            matrix.read_bound_context(argparse.Namespace(context_file=path))
    read.assert_not_called()


def test_diagnostics_redact_nested_strings_without_breaking_json():
    value = {"error": "Bearer secret-value", "details": [{"endpoint": "https://private.invalid/abc?a=2"}]}
    safe = matrix.safe_value(value)
    assert "secret-value" not in json.dumps(safe)
    assert "private.invalid" not in json.dumps(safe)
    assert safe["details"][0]["endpoint"] == "<url omitted>"
