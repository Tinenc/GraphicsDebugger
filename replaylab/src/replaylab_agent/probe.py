"""Explicit, no-generation connection probes for model profiles."""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

from .credential_store import normalize_api_credential
from .errors import AuthenticationError, ConfigurationError, ConnectorError
from .process_env import (
    agent_command_argv,
    external_agent_environment,
    resolve_agent_executable,
)
from .profiles import Profile, is_loopback_endpoint


@dataclass(slots=True)
class ProbeReport:
    profile: str
    provider: str
    kind: str
    protocol: str
    status: str
    detail: str
    models: list[str] = field(default_factory=list)
    selected_model_available: bool | None = None
    warnings: list[str] = field(default_factory=list)
    endpoint: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        value = asdict(self)
        value["selectedModelAvailable"] = value.pop("selected_model_available")
        return value


@dataclass(frozen=True, slots=True)
class _EndpointCandidate:
    base_url: str
    models_url: str


_OPENAI_TERMINAL_SUFFIXES = ("/chat/completions", "/responses", "/models")
_MAX_DISCOVERY_ATTEMPTS = 2
_MAX_MODEL_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_DIAGNOSTIC_ENDPOINT_CHARS = 1024


def _replace_path(parsed: SplitResult, path: str) -> str:
    return urlunsplit((parsed.scheme, parsed.netloc, path or "", "", ""))


def _openai_endpoint_candidates(endpoint: str) -> list[_EndpointCandidate]:
    """Build at most two same-origin model-list candidates from user input.

    Users commonly paste a service root, a ``/v1`` base, or a complete
    ``models``, ``chat/completions`` or ``responses`` URL.  We only remove a
    known terminal API path and toggle one terminal ``v1`` segment.  We never
    follow redirects, alter scheme/authority, inspect response bodies for more
    URLs, or guess arbitrary vendor paths.
    """

    parsed = urlsplit(endpoint.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("base_url must be an absolute http:// or https:// URL")
    try:
        parsed.port
    except ValueError as exc:
        raise ConfigurationError("base_url contains an invalid port") from exc
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(
            "base_url must not contain credentials, query parameters, or fragments"
        )
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    for suffix in _OPENAI_TERMINAL_SUFFIXES:
        if lowered.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break

    paths = [path]
    if path.lower().endswith("/v1"):
        alternate = path[:-3].rstrip("/")
    else:
        alternate = (path + "/v1") if path else "/v1"
    if alternate != path:
        paths.append(alternate)

    candidates: list[_EndpointCandidate] = []
    origin = (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port)
    for candidate_path in paths[:_MAX_DISCOVERY_ATTEMPTS]:
        base_url = _replace_path(parsed, candidate_path).rstrip("/")
        models_url = base_url + "/models"
        candidate = urlsplit(models_url)
        candidate_origin = (
            candidate.scheme.lower(),
            (candidate.hostname or "").lower(),
            candidate.port,
        )
        if candidate_origin != origin or (
            parsed.scheme.lower() == "https" and candidate.scheme.lower() != "https"
        ):
            raise ConfigurationError("model discovery candidate changed endpoint origin or scheme")
        candidates.append(_EndpointCandidate(base_url, models_url))
    return candidates


def _attempt_diagnostics(attempts: list[dict[str, Any]]) -> str:
    """Format bounded, credential-free discovery attempts for user diagnostics.

    Attempt records deliberately contain only normalized same-origin endpoints,
    HTTP statuses and exception *type names*. Request headers, response bodies
    and API keys are never retained. Collapse control/whitespace characters and
    cap each endpoint so an invalid or hostile peer cannot create an unbounded
    error line.
    """

    details: list[str] = []
    for index, attempt in enumerate(attempts[:_MAX_DISCOVERY_ATTEMPTS], start=1):
        endpoint = " ".join(str(attempt.get("endpoint", "")).split())
        if len(endpoint) > _MAX_DIAGNOSTIC_ENDPOINT_CHARS:
            endpoint = endpoint[: _MAX_DIAGNOSTIC_ENDPOINT_CHARS - 1] + "…"
        status = str(attempt.get("status", "unknown"))
        error = " ".join(str(attempt.get("error", "")).split())
        detail = f"{index}) {endpoint or '<unknown endpoint>'} -> {status}"
        if error:
            detail += f" ({error})"
        details.append(detail)
    return "; ".join(details)


async def probe_profile(profile: Profile, *, timeout: float = 15.0) -> ProbeReport:
    """Check auth and model discovery without making a generation request.

    This function only runs when explicitly invoked by the user. It never
    prints or persists credentials and intentionally does not claim tool,
    schema, vision, or streaming support from a provider name alone.
    """

    profile.validate()
    if timeout <= 0 or timeout > 120:
        raise ConfigurationError("probe timeout must be between 0 and 120 seconds")
    provider = profile.provider
    if provider.kind == "agent":
        if (
            provider.protocol == "acp"
            and profile.options.get("allow_unsandboxed_agent") is not True
        ):
            raise ConfigurationError(
                "probing an ACP CLI launches an unsandboxed OS process; set "
                "allow_unsandboxed_agent=true only for a vendor Agent you trust"
            )
        argv = agent_command_argv(profile)
        command = argv[0] if argv else ""
        resolved = resolve_agent_executable(profile) if command else ""
        if not resolved:
            raise ConnectorError(f"external Agent command was not found: {command}")
        await _probe_agent_launch(profile, [resolved, *argv[1:]], timeout)
        return ProbeReport(
            profile.id,
            provider.title,
            provider.kind,
            provider.protocol,
            "launchable",
            f"process launch succeeded: {resolved}; authentication remains owned by that Agent",
        )
    if provider.protocol in {"azure-openai", "google-cloud"}:
        # Azure deployment enumeration uses a separate management-plane auth
        # contract, while Vertex model discovery relies on ADC and project IAM.
        # Constructing these providers is the safe no-generation validation.
        from .connectors.langchain_models import build_chat_model

        build_chat_model(profile)
        return ProbeReport(
            profile.id,
            provider.title,
            provider.kind,
            provider.protocol,
            "configured",
            "provider adapter constructed; use the official cloud console/IAM to verify deployment access",
            selected_model_available=None,
        )

    key = profile.resolve_api_key()
    endpoint, models, attempts = await discover_models(
        profile,
        timeout=timeout,
        api_key=key,
    )
    selected = profile.model in models if models else None
    warnings: list[str] = []
    if not models:
        warnings.append("the endpoint responded but returned no recognizable model ids")
    elif selected is False:
        warnings.append("the configured model was not present in the discovery response")
    return ProbeReport(
        profile.id,
        provider.title,
        provider.kind,
        provider.protocol,
        "connected",
        f"authenticated model discovery returned {len(models)} model(s)",
        models=models[:200],
        selected_model_available=selected,
        warnings=warnings,
        endpoint=endpoint,
        attempts=attempts,
    )


async def discover_models(
    profile: Profile,
    *,
    timeout: float = 15.0,
    api_key: str | None = None,
    trust_env: bool = True,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Discover selectable model ids without generation or redirect following.

    ``api_key`` exists for the qrenderdoc setup wizard, which passes a
    session-only secret over stdin.  ``None`` means resolve the configured
    environment variable; an explicit empty string is valid only for an
    unauthenticated local preset.
    """

    profile.validate()
    if timeout <= 0 or timeout > 120:
        raise ConfigurationError("probe timeout must be between 0 and 120 seconds")
    if profile.provider.kind != "model":
        raise ConfigurationError("model discovery requires a model API profile")
    if api_key is None:
        api_key = profile.resolve_api_key()

    requests = _model_list_requests(profile, api_key)
    # HTTPX honours HTTP_PROXY/HTTPS_PROXY by default.  That is useful for
    # remote HTTPS services, but unsafe for a credential-bearing loopback API:
    # a missing NO_PROXY entry would otherwise send the complete Authorization
    # header to the proxy.  An explicit True can never override this boundary;
    # False remains available to disable proxies for a remote endpoint too.
    client_trust_env = bool(trust_env) and not is_loopback_endpoint(profile.endpoint)
    attempts: list[dict[str, Any]] = []
    last_error = "the endpoint may not implement model listing"
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=client_trust_env
        ) as client:
            for base_url, url, headers, params in requests:
                try:
                    async with client.stream(
                        "GET", url, headers=headers, params=params
                    ) as response:
                        attempts.append(
                            {
                                "endpoint": base_url,
                                "status": response.status_code,
                            }
                        )
                        if response.status_code in {401, 403}:
                            # Authentication applies to the origin, not an API-version
                            # suffix. Stop immediately rather than sending the same
                            # credential to more guessed paths.
                            attempts[-1]["error"] = "authentication-rejected"
                            raise AuthenticationError(
                                f"{profile.provider.title} rejected the configured credential "
                                f"(HTTP {response.status_code}); bounded attempts: "
                                f"{_attempt_diagnostics(attempts)}"
                            )
                        if 300 <= response.status_code < 400:
                            last_error = "redirect refused"
                            attempts[-1]["error"] = "redirect-refused"
                            continue
                        if response.status_code >= 400:
                            last_error = f"HTTP {response.status_code}"
                            attempts[-1]["error"] = last_error
                            continue
                        content_length = response.headers.get("content-length", "")
                        if (
                            content_length.isdigit()
                            and int(content_length) > _MAX_MODEL_RESPONSE_BYTES
                        ):
                            attempts[-1]["error"] = "response-too-large"
                            raise ConnectorError(
                                f"{profile.provider.title} model discovery response "
                                f"exceeded 4 MiB; bounded attempts: "
                                f"{_attempt_diagnostics(attempts)}"
                            )
                        encoded = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(encoded) + len(chunk) > _MAX_MODEL_RESPONSE_BYTES:
                                attempts[-1]["error"] = "response-too-large"
                                raise ConnectorError(
                                    f"{profile.provider.title} model discovery response "
                                    f"exceeded 4 MiB; bounded attempts: "
                                    f"{_attempt_diagnostics(attempts)}"
                                )
                            encoded.extend(chunk)
                        try:
                            payload = json.loads(bytes(encoded))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            last_error = "non-JSON response"
                            attempts[-1]["error"] = "non-json-response"
                            continue
                        models = _parse_models(profile.provider.protocol, payload)[:200]
                        return base_url, models, attempts
                except (AuthenticationError, ConnectorError):
                    raise
                except httpx.HTTPError as exc:
                    if attempts and attempts[-1].get("endpoint") == base_url:
                        attempts[-1]["error"] = type(exc).__name__
                    else:
                        attempts.append(
                            {
                                "endpoint": base_url,
                                "status": "network-error",
                                "error": type(exc).__name__,
                            }
                        )
                    last_error = f"network error ({type(exc).__name__})"
                    continue
    except (AuthenticationError, ConnectorError):
        raise
    except httpx.HTTPError as exc:
        # Defensive fallback for a transport implementation that raises while
        # entering/exiting the bounded client rather than during get().
        raise ConnectorError(
            f"connection probe could not reach {profile.endpoint}: {type(exc).__name__}"
        ) from exc
    raise ConnectorError(
        f"{profile.provider.title} model discovery failed after {len(attempts)} "
        f"bounded same-origin attempt(s): {last_error}; attempts: "
        f"{_attempt_diagnostics(attempts) or '<none>'}"
    )


def _model_list_requests(
    profile: Profile, key: str
) -> list[tuple[str, str, dict[str, str], dict[str, str]]]:
    endpoint = profile.endpoint.rstrip("/")
    protocol = profile.provider.protocol
    key = normalize_api_credential(key, allow_empty=True)
    if protocol in {"openai-chat", "openai-responses"}:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return [
            (candidate.base_url, candidate.models_url, headers, {})
            for candidate in _openai_endpoint_candidates(endpoint)
        ]
    if protocol == "anthropic-messages":
        # MiniMax's Anthropic-compatible base already ends in /anthropic;
        # Anthropic itself uses /v1/models.  These are preset-owned paths, not
        # guesses derived from a server response.
        base_url = endpoint
        return [
            (
                base_url,
                base_url + "/v1/models",
                {"x-api-key": key, "anthropic-version": "2023-06-01"},
                {},
            )
        ]
    if protocol == "google-genai":
        return [
            (
                endpoint,
                endpoint + "/v1beta/models",
                {"x-goog-api-key": key},
                {},
            )
        ]
    raise ConfigurationError(f"no no-generation probe for protocol: {protocol}")


def _parse_models(protocol: str, payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    if protocol == "google-genai":
        raw = payload.get("models")
    else:
        raw = payload.get("data")
        if not isinstance(raw, list) and protocol in {"openai-chat", "openai-responses"}:
            raw = payload.get("models")
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        if isinstance(item, str):
            value = item
            if value and value not in result:
                result.append(value)
            continue
        if not isinstance(item, dict):
            continue
        value = item.get("name") if protocol == "google-genai" else item.get("id")
        if not isinstance(value, str) or not value:
            continue
        if protocol == "google-genai" and value.startswith("models/"):
            value = value.removeprefix("models/")
        result.append(value)
    return sorted(set(result))


async def _probe_agent_launch(
    profile: Profile, resolved_argv: list[str], timeout: float
) -> None:
    """Start and stop the configured server without creating a session or prompt."""

    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *resolved_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=external_agent_environment(profile),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # App-server/ACP processes normally stay alive waiting for protocol input.
        # A quick non-zero exit is a useful authentication/configuration failure;
        # remaining alive proves only that the configured command is spawnable.
        await asyncio.sleep(min(0.2, timeout / 4.0))
        if process.returncode not in {None, 0}:
            raise ConnectorError(
                f"external Agent process exited during launch (code {process.returncode})"
            )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise ConnectorError(f"external Agent process could not be launched: {exc}") from exc
    finally:
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
                with contextlib.suppress(Exception):
                    await process.stdin.wait_closed()
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                # Once killed, always reap the process. In particular, do not
                # leave Windows Proactor transports for event-loop shutdown.
                await process.wait()


__all__ = ["ProbeReport", "discover_models", "probe_profile"]
