"""LangChain chat-model construction for the deepagents engine.

This is the sole model-adapter layer. Profiles keep the same endpoint,
credential and option names
(``api``, ``api_version``, ``temperature``, ``timeout_seconds``,
``reasoning_effort``, ``thinking_budget``, ``stream_thinking``).
"""

from __future__ import annotations

import importlib
from typing import Any, Mapping

from ..errors import ConfigurationError, ConnectorError
from ..profiles import Profile, azure_endpoint_uses_v1

_INSTALL_HINT = 'pip install "replaylab-agenthost"'
_LOCAL_API_KEY_PLACEHOLDER = "replaylab-local-no-key"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0
DEFAULT_OPENAI_STREAM_IDLE_SECONDS = 600.0
DEFAULT_REQUEST_MAX_RETRIES = 2


def _string_option(profile: Profile, name: str, default: str) -> str:
    value = profile.options.get(name, default)
    if not isinstance(value, str):
        raise ConfigurationError(
            f"profile '{profile.id}' options.{name} must be a string"
        )
    return value.strip()


def _require_endpoint(profile: Profile) -> str:
    endpoint = profile.endpoint
    if not endpoint:
        raise ConfigurationError(
            f"profile '{profile.id}' requires base_url for protocol "
            f"'{profile.provider.protocol}'"
        )
    return endpoint


def _load(module: str, profile: Profile) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ConnectorError(
            f"profile '{profile.id}' needs the LangChain package providing '{module}': {_INSTALL_HINT}"
        ) from exc


def _common_kwargs(profile: Profile, max_retries: int | None = None, *, openai_stream=False) -> dict[str, Any]:
    # Bound one provider request, not the graph/Agent invocation. In particular
    # never replay an Agent after it has already executed a Host tool.
    kwargs: dict[str, Any] = {
        "max_retries": DEFAULT_REQUEST_MAX_RETRIES if max_retries is None else max_retries,
        "timeout": float(profile.options.get("timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS)),
    }
    if openai_stream:
        if "timeout_seconds" in profile.options:
            kwargs["stream_chunk_timeout"] = kwargs["timeout"]
        else:
            from httpx import Timeout

            # SSE keepalives can keep the socket active during long reasoning.
            # Bound both parsed-content silence and a fully silent transport.
            kwargs["stream_chunk_timeout"] = DEFAULT_OPENAI_STREAM_IDLE_SECONDS
            kwargs["timeout"] = Timeout(DEFAULT_REQUEST_TIMEOUT_SECONDS,
                                        read=DEFAULT_OPENAI_STREAM_IDLE_SECONDS)
    if "temperature" in profile.options:
        kwargs["temperature"] = float(profile.options["temperature"])
    return kwargs


def _max_output_tokens(profile: Profile) -> int:
    value = profile.options.get("max_output_tokens", 0)
    return int(value) if value else 0


def _thinking_budget(profile: Profile) -> int:
    from ..native_vendor import thinking_budget

    return int(thinking_budget(profile))


def build_chat_model(
    profile: Profile,
    *,
    model: str = "",
    environ: Mapping[str, str] | None = None,
    max_retries: int | None = None,
) -> Any:
    """A LangChain ``BaseChatModel`` for *profile* (no request is made)."""

    profile.validate()
    if max_retries is not None and (type(max_retries) is not int or not 0 <= max_retries <= 10):
        raise ConfigurationError("max_retries must be an integer from 0 to 10")
    provider = profile.provider
    if provider.kind != "model":
        raise ConfigurationError(
            f"profile '{profile.id}' is an external Agent connector ({provider.protocol}); "
            "the deepagents engine only drives model API connections"
        )
    environment = None if environ is None else dict(environ)
    model_name = profile.resolve_model(model)
    protocol = provider.protocol
    stream_thinking = profile.options.get("stream_thinking")
    budget = 0 if stream_thinking is False else _thinking_budget(profile)
    # Whether the user asked for reasoning at all, as opposed to the default budget
    # merely existing. Reasoning parameters are rejected outright by models that do
    # not support them, so on surfaces where the common models do not (Responses
    # with gpt-4.1 / gpt-4o) they have to be opt-in rather than default-on.
    thinking_requested = (
        stream_thinking is True
        or "thinking_budget_tokens" in profile.options
        or profile.options.get("reasoning_effort") is not None
    )

    if protocol in {"openai-responses", "openai-chat"}:
        endpoint = _require_endpoint(profile)
        api_key = profile.resolve_api_key(environment)
        if provider.auth == "none":
            api_key = _LOCAL_API_KEY_PLACEHOLDER
        module = _load("langchain_openai", profile)
        default_surface = "responses" if protocol == "openai-responses" else "chat"
        from urllib.parse import urlsplit
        entered_path = urlsplit(profile.base_url or "").path.rstrip("/").lower()
        if entered_path.endswith("/chat/completions"):
            default_surface = "chat"
        elif entered_path.endswith("/responses"):
            default_surface = "responses"
        surface = _string_option(profile, "api", default_surface).lower()
        if surface not in {"chat", "responses"}:
            raise ConfigurationError(f"profile '{profile.id}' options.api must be 'chat' or 'responses'")
        kwargs = _common_kwargs(profile, max_retries, openai_stream=True)
        kwargs.update({"model": model_name, "api_key": api_key, "base_url": endpoint})
        if _max_output_tokens(profile):
            kwargs["max_tokens"] = _max_output_tokens(profile)
        effort = profile.options.get("reasoning_effort")
        if surface == "responses":
            kwargs["use_responses_api"] = True
            # Only when asked: `reasoning` is accepted by reasoning models alone, and
            # sending it by default made every turn fail on gpt-4.1 / gpt-4o with
            # "Unsupported parameter: 'reasoning'" -- the most common models on the
            # OpenAI preset. `stream_thinking=true` or a budget turns it on.
            # The effort rides in the same `reasoning` object: on the Responses
            # surface a separate `reasoning_effort` next to `reasoning` is forwarded
            # as an unknown argument by some langchain-openai versions.
            reasoning: dict[str, str] = {}
            if budget > 0 and thinking_requested:
                reasoning["summary"] = "detailed"
            if effort is not None:
                reasoning["effort"] = str(effort)
            if reasoning:
                kwargs["reasoning"] = reasoning
        elif effort is not None:
            kwargs["reasoning_effort"] = str(effort)
        from .openai_compatibility import compatible_chat_model
        return compatible_chat_model(module.ChatOpenAI, kwargs)

    if protocol == "anthropic-messages":
        endpoint = _require_endpoint(profile)
        api_key = profile.resolve_api_key(environment)
        module = _load("langchain_anthropic", profile)
        kwargs = _common_kwargs(profile, max_retries)
        kwargs.update({"model": model_name, "api_key": api_key, "base_url": endpoint})
        if _max_output_tokens(profile):
            kwargs["max_tokens"] = _max_output_tokens(profile)
        # The stock Anthropic preset thinks by default; a custom Anthropic-protocol
        # profile (a proxy, Bedrock-style gateway) thinks when asked, by the same
        # switches the Responses branch honours.
        if budget > 0 and (thinking_requested or profile.preset == "anthropic"):
            thinking_tokens = max(1024, budget)
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_tokens}
            kwargs.pop("temperature", None)  # Anthropic refuses temperature with thinking
            # Anthropic requires max_tokens > budget_tokens. A user-set max_output_tokens
            # below the budget used to survive `setdefault` untouched, and every request
            # was then rejected; raise it to the floor instead of leaving it.
            floor = max(4096, thinking_tokens + 2048)
            kwargs["max_tokens"] = max(int(kwargs.get("max_tokens") or 0), floor)
        return module.ChatAnthropic(**kwargs)

    if protocol == "google-genai":
        # An endpoint override is accepted by the profile and honoured by model
        # discovery, so a user who points this at a gateway sees it work in the
        # wizard. Silently ignoring it here would send their gateway's key to Google
        # instead -- a credential to a provider they did not choose. Refuse before
        # anything else, so the message is this one and not "install a package".
        # `base_url`, not `endpoint`: the latter resolves to the preset's own default
        # (https://generativelanguage.googleapis.com), so testing it would refuse
        # every stock Gemini profile. Only a user override is a problem here.
        if profile.base_url.strip():
            raise ConfigurationError(
                "profile '%s' sets a base URL, which the Gemini protocol does not "
                "send: the request would go to Google with this key. Use the "
                "OpenAI-compatible protocol for a gateway, or clear the base URL."
                % profile.id)
        api_key = profile.resolve_api_key(environment)
        module = _load("langchain_google_genai", profile)
        kwargs = _common_kwargs(profile, max_retries)
        kwargs.update({"model": model_name, "google_api_key": api_key})
        if _max_output_tokens(profile):
            kwargs["max_output_tokens"] = _max_output_tokens(profile)
        if "thinking_budget_tokens" in profile.options or stream_thinking is not None:
            kwargs["thinking_budget"] = budget
        if stream_thinking is not None or budget > 0:
            kwargs["include_thoughts"] = budget > 0
        return module.ChatGoogleGenerativeAI(**kwargs)

    if protocol == "google-cloud":
        if profile.base_url.strip():
            raise ConfigurationError(
                "profile '%s' sets a base URL, which the Vertex protocol does not "
                "send. Use the OpenAI-compatible protocol for a gateway, or clear the base URL."
                % profile.id)
        module = _load("langchain_google_vertexai", profile)
        kwargs = _common_kwargs(profile, max_retries)
        kwargs["model_name"] = model_name
        project = _string_option(profile, "project", "")
        location = _string_option(profile, "location", "")
        if project:
            kwargs["project"] = project
        if location:
            kwargs["location"] = location
        if _max_output_tokens(profile):
            kwargs["max_output_tokens"] = _max_output_tokens(profile)
        return module.ChatVertexAI(**kwargs)

    if protocol == "azure-openai":
        endpoint = _require_endpoint(profile)
        api_key = profile.resolve_api_key(environment)
        module = _load("langchain_openai", profile)
        kwargs = _common_kwargs(profile, max_retries, openai_stream=True)
        if azure_endpoint_uses_v1(endpoint):
            # The v1 endpoint already contains its API route and uses the
            # ordinary OpenAI contract. AzureChatOpenAI would require the
            # intentionally absent api_version and insert a deployment route.
            kwargs.update({"model": model_name, "base_url": endpoint, "api_key": api_key})
            if _max_output_tokens(profile):
                kwargs["max_tokens"] = _max_output_tokens(profile)
            effort = profile.options.get("reasoning_effort")
            if effort is not None:
                kwargs["reasoning_effort"] = str(effort)
            from .openai_compatibility import compatible_chat_model
            return compatible_chat_model(module.ChatOpenAI,kwargs)
        kwargs.update(
            {
                "azure_deployment": model_name,
                "azure_endpoint": endpoint,
                "api_key": api_key,
                "api_version": _string_option(profile, "api_version", "") or None,
            }
        )
        if _max_output_tokens(profile):
            kwargs["max_tokens"] = _max_output_tokens(profile)
        effort = profile.options.get("reasoning_effort")
        if effort is not None:
            kwargs["reasoning_effort"] = str(effort)
        from .openai_compatibility import compatible_chat_model
        return compatible_chat_model(module.AzureChatOpenAI,kwargs)

    raise ConfigurationError(
        f"profile '{profile.id}' uses model protocol '{protocol}', which the deepagents engine does not support yet"
    )


__all__ = ["build_chat_model"]
