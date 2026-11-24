"""Credential-free terminal diagnostics shared by API and native Agent routes.

Classification describes a failure; it never retries a turn or changes a model.
Connectors own recovery because only they know which effects already completed.
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping

from .errors import AuthenticationError, ConfigurationError, ContextOverflowError

_RETRYABLE = frozenset({"provider-capacity", "provider-rate-limit", "provider-timeout",
                        "provider-transport", "provider-unavailable"})
_CODES = _RETRYABLE | frozenset({"provider-quota", "provider-authentication",
    "provider-permission", "provider-model-unavailable", "provider-configuration",
    "provider-context", "provider-policy", "provider-turn-failed", "provider-failed"})
_QUOTA_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached",
    "credit_balance_exhausted", "usage_limit_exceeded", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "organization_usage_limit_exceeded"})


def provider_failure_metadata(error: BaseException) -> dict:
    """Retain structured codes through wrappers without publishing server bodies."""
    chain, seen = [], set()
    current = error
    while current is not None and id(current) not in seen and len(chain) < 6:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    code = ""
    for item in chain:
        explicit = getattr(item, "error_code", "")
        if isinstance(explicit, str) and explicit in _CODES:
            code = explicit
            break
    if not code:
        for item in reversed(chain):
            body = getattr(item, "body", None)
            response = getattr(item, "response", None)
            if body is None and response is not None:
                try:
                    if len(response.content) <= 65536:
                        body = response.json()
                except (ValueError, TypeError, AttributeError, RuntimeError):
                    pass
            if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
                body = body["error"]
            raw_code = str((body.get("code") if isinstance(body, Mapping) else None)
                           or getattr(item, "code", "") or "").lower()
            raw_type = str((body.get("type") if isinstance(body, Mapping) else None)
                           or getattr(item, "type", "") or "").lower()
            status = getattr(item, "status_code", None)
            if not isinstance(status, int) or isinstance(status, bool):
                status = getattr(response, "status_code", getattr(item, "code", None))
            if not isinstance(status, int) or isinstance(status, bool):
                status = None
            text = str(item)[:4000].lower()
            message = str(body.get("message") or "").lower() if isinstance(body, Mapping) else ""
            exhausted_pool = any(phrase in message for phrase in (
                "all available accounts exhausted", "no available accounts",
                "no available channels", "no available channel",
                "account pool exhausted", "upstream accounts exhausted"))
            if ({raw_code, raw_type} & _QUOTA_CODES or status == 402
                    or status in {None, 429} and "you have no credits remaining" in (message or text)):
                code = "provider-quota"
            elif isinstance(item, AuthenticationError) or status == 401 or raw_code in {"invalid_api_key", "authentication_error", "unauthorized"}:
                code = "provider-authentication"
            elif exhausted_pool and status in {None, 403, 429, 503}:
                # Compatible gateways sometimes report upstream capacity as HTTP 403.
                # Keep explicit key/quota errors above; this is not a reason to rewrite a
                # valid payload, rotate credentials or repeat an Agent/tool turn.
                code = "provider-capacity"
            elif status == 403:
                code = "provider-permission"
            elif raw_code in {"context_length_exceeded", "context_window_exceeded"} or isinstance(item, ContextOverflowError):
                code = "provider-context"
            elif raw_code in {"model_not_found", "model_not_available"}:
                code = "provider-model-unavailable"
            elif raw_code in {"server_overloaded", "server_is_overloaded"} or (
                    status in {None, 503} and raw_code in {"", "other"}
                    and "selected model is at capacity" in text):
                code = "provider-capacity"
            elif status == 429 or raw_code in {"rate_limit_exceeded", "slow_down"} or raw_type == "rate_limit_error":
                code = "provider-rate-limit"
            elif status == 408 or isinstance(item, (TimeoutError, asyncio.TimeoutError)) or type(item).__name__ in {"APITimeoutError", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"}:
                code = "provider-timeout"
            elif isinstance(item, ConnectionError) or type(item).__name__ in {"APIConnectionError", "ConnectError", "RemoteProtocolError", "ReadError", "WriteError", "NetworkError"}:
                code = "provider-transport"
            elif status == 409 or isinstance(status, int) and 500 <= status <= 599 or (
                    status is None and (
                        {raw_code, raw_type} & {"server_error", "internal_error", "internal_server_error", "service_unavailable", "service_unavailable_error"}
                        or type(item).__name__ == "APIError" and not raw_code and raw_type in {"", "error"}
                        and (message or text).strip().rstrip(".") == "an error occurred while processing the request"
                    )):
                code = "provider-unavailable"
            elif isinstance(item, ConfigurationError) or status in {400, 404, 422}:
                code = "provider-configuration"
            if code:
                break
    code = code or "provider-failed"
    message_code = "provider-failed" if code == "provider-turn-failed" else code
    result = {"errorCode": code, "errorMessageId": "task.error." + message_code,
              "errorMessageArgs": [], "retryable": code in _RETRYABLE}
    # Retryable describes the incident class, not an instruction to loop again.
    for item in chain:
        if getattr(item, "error_code", "") == code:
            result["retryable"] = getattr(item, "retryable", False) is True
            delay = getattr(item, "retry_after_seconds", None)
            if isinstance(delay, (int, float)) and not isinstance(delay, bool) and math.isfinite(delay) and 0 <= delay <= 86400:
                result["retryAfterSeconds"] = delay
            break
    return result
