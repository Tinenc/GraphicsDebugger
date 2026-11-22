"""Provider profiles and fast active-profile switching."""

from __future__ import annotations

import json
import ipaddress
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .catalog import ProviderPreset, get_preset
from .credential_store import normalize_api_credential
from .errors import AuthenticationError, ConfigurationError
from .process_env import MCP_HOST_PROVIDER_ENV, RESERVED_CREDENTIAL_ENV

PROFILE_SCHEMA = "replaylab-ai-profiles/v2"
LEGACY_PROFILE_SCHEMAS = frozenset({"replaylab-ai-profiles/v1"})
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SECRET_COMMAND_TERMINALS = frozenset(
    {
        "apikey",
        "auth",
        "authentication",
        "authorization",
        "bearer",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "header",
        "headers",
        "key",
        "password",
        "passwd",
        "pat",
        "secret",
        "token",
    }
)
_SECRET_VALUE_SUFFIXES = frozenset({"literal", "raw", "value"})
_ALLOWED_OPTION_KEYS = frozenset(
    {
        "api",
        "api_version",
        "allow_unsandboxed_agent",
        "document_input",
        "image_input",
        "location",
        "max_output_tokens",
        "native_shell",
        "project",
        "reasoning_effort",
        "context_fallback_window_tokens",
        "context_recent_turns",
        "context_reserved_output_tokens",
        "context_target_percent",
        "context_trigger_percent",
        "context_window_tokens",
        "stream_thinking",
        "subscription_auth_flow",
        "temperature",
        "thinking_budget_tokens",
        "timeout_seconds",
    }
)


def azure_endpoint_uses_v1(endpoint: str) -> bool:
    parsed = urlsplit(endpoint)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()
    return host.endswith(".models.ai.azure.com") or path.endswith("/openai/v1")


def _is_loopback_host(hostname: str) -> bool:
    host = hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_endpoint(endpoint: str) -> bool:
    """Return whether an absolute endpoint is bound to the local machine.

    Callers use this to keep credential-bearing local API requests away from
    environment-configured HTTP proxies.  Profile validation remains the
    authority for rejecting malformed URLs.
    """

    return _is_loopback_host(urlsplit(endpoint).hostname or "")


def _command_argument_may_contain_secret(argument: str) -> bool:
    """Reject common raw-secret carriers before an Agent argv is persisted.

    Custom ACP commands are intentionally argv arrays rather than shell text,
    but arbitrary vendors use many credential flag spellings.  Inspect option
    name components instead of maintaining a short exact-name blacklist.  A
    POSIX absolute path such as ``/token`` is not treated as a Windows option;
    slash options are inspected only when they contain ``:`` or ``=``.
    """

    stripped = argument.strip()
    lowered = stripped.lower()
    if stripped == "-H":
        return True
    if lowered.startswith(
        ("authorization:", "proxy-authorization:", "x-api-key:", "cookie:", "bearer ")
    ):
        return True

    name = ""
    if stripped.startswith("--") and len(stripped) > 2:
        name = stripped[2:]
    elif stripped.startswith("-") and len(stripped) > 1:
        name = stripped[1:]
    elif stripped.startswith("/") and any(delimiter in stripped for delimiter in (":", "=")):
        name = stripped[1:]
    elif "=" in stripped:
        candidate = stripped.split("=", 1)[0]
        if _ENV_RE.fullmatch(candidate):
            name = candidate
    if not name:
        return False
    name = re.split(r"[=:]", name, maxsplit=1)[0]
    # Split both separator-based and camelCase/PascalCase vendor spellings.
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "-", name)
    components = [part.lower() for part in re.split(r"[-_.]+", name) if part]
    if not components:
        return False
    if components[-1] in _SECRET_COMMAND_TERMINALS:
        return True
    # ``--token-budget`` and ``--header-size`` are ordinary tuning flags, but
    # an explicit raw/value/literal suffix still carries a persisted secret.
    return components[-1] in _SECRET_VALUE_SUFFIXES and any(
        part in _SECRET_COMMAND_TERMINALS for part in components[:-1]
    )


def normalize_openai_base_url(endpoint: str) -> str:
    """Remove only a known terminal OpenAI API operation from a pasted URL."""

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
    for suffix in ("/chat/completions", "/responses", "/models"):
        if lowered.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def default_config_path() -> Path:
    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    root = Path(appdata) if appdata else Path.home() / ".config"
    return root / "ReplayLab" / "ai" / "profiles.json"


@dataclass(slots=True)
class Profile:
    id: str
    preset: str
    model: str = ""
    models: list[str] = field(default_factory=list)
    default_model: str = ""
    title: str = ""
    base_url: str = ""
    api_key_env: str = ""
    command: list[str] = field(default_factory=list)
    agent_env: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    probed_capabilities: list[str] | None = None
    # Exact, user/provider-verified context windows keyed by complete model id.
    # This formal field keeps the legacy scalar ``options.context_window_tokens``
    # compatible while allowing one profile to enable models with different
    # windows without applying one model's limit to every selection.
    model_context_windows: dict[str, int] = field(default_factory=dict)

    def validate(self) -> None:
        string_fields = {
            "id": self.id,
            "preset": self.preset,
            "model": self.model,
            "default_model": self.default_model,
            "title": self.title,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
        }
        if any(not isinstance(value, str) for value in string_fields.values()):
            raise ConfigurationError("profile identity, model, URL and credential fields must be strings")
        if len(self.title) > 128 or any(value in self.title for value in ("\0", "\r", "\n")):
            raise ConfigurationError(
                "profile title must be at most 128 characters without NUL or newlines"
            )
        if len(self.base_url) > 4096 or "\0" in self.base_url:
            raise ConfigurationError("base_url must be at most 4096 characters without NUL")
        if not isinstance(self.models, list) or any(
            not isinstance(value, str) for value in self.models
        ):
            raise ConfigurationError("profile models must be an array of strings")
        if len(self.models) > 200:
            raise ConfigurationError("profile models may contain at most 200 entries")
        cleaned_models: list[str] = []
        for value in self.models:
            model_id = value.strip()
            if (
                not model_id
                or len(model_id) > 512
                or any(character in model_id for character in ("\0", "\r", "\n"))
            ):
                raise ConfigurationError(
                    "profile model ids must be non-empty single-line values of at most 512 characters"
                )
            if model_id not in cleaned_models:
                cleaned_models.append(model_id)
        self.models = cleaned_models
        self.model = self.model.strip()
        self.default_model = self.default_model.strip()
        if any(
            len(value) > 512
            or any(character in value for character in ("\0", "\r", "\n"))
            for value in (self.model, self.default_model)
        ):
            raise ConfigurationError(
                "profile model ids must be single-line values of at most 512 characters"
            )
        # v1 stored one model only.  Promote it to the v2 picker fields in memory so
        # old profiles remain immediately usable and are migrated on the next save.
        if not self.models and self.model:
            self.models = [self.model]
        if not self.default_model:
            self.default_model = self.model or (self.models[0] if self.models else "")
        if not self.model:
            self.model = self.default_model
        if self.default_model and self.default_model not in self.models:
            raise ConfigurationError("default_model must be present in profile models")
        if self.model and self.models and self.model not in self.models:
            raise ConfigurationError("model must be present in profile models")
        if not isinstance(self.command, list) or any(
            not isinstance(value, str) for value in self.command
        ):
            raise ConfigurationError("profile command must be an argv array of strings")
        if not isinstance(self.agent_env, list) or any(
            not isinstance(value, str) for value in self.agent_env
        ):
            raise ConfigurationError("agent_env must be an array of environment variable names")
        if not isinstance(self.options, dict) or any(
            not isinstance(key, str) for key in self.options
        ):
            raise ConfigurationError("profile options must be an object with string keys")
        if self.probed_capabilities is not None and (
            not isinstance(self.probed_capabilities, list)
            or any(not isinstance(value, str) for value in self.probed_capabilities)
        ):
            raise ConfigurationError("probed_capabilities must be null or an array of strings")
        if not isinstance(self.model_context_windows, dict):
            raise ConfigurationError("model_context_windows must be an object")
        if len(self.model_context_windows) > 200:
            raise ConfigurationError(
                "model_context_windows may contain at most 200 entries"
            )
        cleaned_context_windows: dict[str, int] = {}
        for raw_model_id, raw_window in self.model_context_windows.items():
            if not isinstance(raw_model_id, str):
                raise ConfigurationError(
                    "model_context_windows keys must be model-id strings"
                )
            model_id = raw_model_id.strip()
            if (
                not model_id
                or len(model_id) > 512
                or any(character in model_id for character in ("\0", "\r", "\n"))
            ):
                raise ConfigurationError(
                    "model_context_windows keys must be non-empty single-line model ids "
                    "of at most 512 characters"
                )
            if (
                not isinstance(raw_window, int)
                or isinstance(raw_window, bool)
                or not 4096 <= raw_window <= 4_000_000
            ):
                raise ConfigurationError(
                    "model_context_windows values must be integers between 4096 and 4000000"
                )
            if model_id in cleaned_context_windows:
                raise ConfigurationError(
                    "model_context_windows model ids must be unique after trimming"
                )
            cleaned_context_windows[model_id] = raw_window
        self.model_context_windows = cleaned_context_windows
        if not _ID_RE.fullmatch(self.id):
            raise ConfigurationError(
                "profile id must contain only lowercase letters, numbers, '.', '_' or '-'"
            )
        preset = self.provider
        if preset.kind == "model" and not self.model:
            raise ConfigurationError(f"profile '{self.id}' requires a model name")
        if preset.kind == "model" and preset.auth == "api-key" and not self.key_env:
            raise ConfigurationError(f"profile '{self.id}' requires an API-key environment variable")
        if preset.protocol == "azure-openai":
            if not self.endpoint:
                raise ConfigurationError("Azure OpenAI requires base_url/azure_endpoint")
            api_version = str(self.options.get("api_version", "")).strip()
            if azure_endpoint_uses_v1(self.endpoint) and api_version:
                raise ConfigurationError(
                    "Azure OpenAI v1/Foundry endpoints must omit options.api_version"
                )
            if not azure_endpoint_uses_v1(self.endpoint) and not api_version:
                raise ConfigurationError("Azure OpenAI requires options.api_version")
        if preset.kind == "agent" and not self.argv:
            raise ConfigurationError(f"agent profile '{self.id}' requires a command")
        if self.key_env and not _ENV_RE.fullmatch(self.key_env):
            raise ConfigurationError("api_key_env must be a valid environment variable name")
        if self.key_env.upper() in RESERVED_CREDENTIAL_ENV:
            raise ConfigurationError(
                "api_key_env must not reuse a system, execution, trust, or Bread control "
                "environment variable"
            )
        if len(self.agent_env) != len(set(self.agent_env)):
            raise ConfigurationError("agent_env names must be unique")
        if any(not _ENV_RE.fullmatch(name) for name in self.agent_env):
            raise ConfigurationError("agent_env entries must be valid environment variable names")
        if any(name.upper() in MCP_HOST_PROVIDER_ENV for name in self.agent_env):
            raise ConfigurationError(
                "agent_env must not expose Bread MCP provider configuration"
            )
        if preset.kind != "agent" and self.agent_env:
            raise ConfigurationError("agent_env is only valid for external Agent profiles")
        if self.endpoint:
            try:
                endpoint = urlsplit(self.endpoint)
                hostname = endpoint.hostname
                endpoint.port
            except ValueError as exc:
                raise ConfigurationError("base_url is invalid") from exc
            if endpoint.scheme not in {"http", "https"} or not hostname:
                raise ConfigurationError("base_url must be an absolute http:// or https:// URL")
            if endpoint.scheme == "http" and not _is_loopback_host(hostname):
                raise ConfigurationError(
                    "remote base_url must use https://; plaintext http:// is allowed only on loopback"
                )
            if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise ConfigurationError(
                    "base_url must not contain credentials, query parameters, or fragments"
                )
        profile_argv = self.argv
        trusted_catalog_argv = (
            bool(preset.command)
            and len(profile_argv) == len(preset.command)
            and tuple(profile_argv[1:]) == preset.command[1:]
        )
        for index, argument in enumerate(profile_argv):
            # Empty non-executable argv entries are valid and are required by a
            # small number of audited vendor flags (for example CodeBuddy's
            # documented ``--tools ""``). Custom commands keep the previous
            # non-empty rule; only the exact built-in catalog argv may use one.
            if "\0" in argument or (
                not argument and (index == 0 or not trusted_catalog_argv)
            ):
                raise ConfigurationError("external Agent command contains an invalid argument")
            if _command_argument_may_contain_secret(argument):
                raise ConfigurationError(
                    "external Agent secrets must use its managed login or environment variables, "
                    "not persisted command arguments"
                )
        unknown_options = sorted(set(self.options) - _ALLOWED_OPTION_KEYS)
        if unknown_options:
            raise ConfigurationError(
                "unknown or unsafe profile options: " + ", ".join(unknown_options)
            )
        if "allow_unsandboxed_agent" in self.options and preset.protocol != "acp":
            raise ConfigurationError(
                "allow_unsandboxed_agent is valid only for ACP Agent profiles"
            )
        if "subscription_auth_flow" in self.options and preset.kind != "agent":
            raise ConfigurationError(
                "subscription_auth_flow is valid only for Agent subscription profiles"
            )
        for key, value in self.options.items():
            if isinstance(value, (dict, list, tuple, set)):
                raise ConfigurationError(f"profile option '{key}' must be a scalar value")
        self._validate_option_values()

    def _validate_option_values(self) -> None:
        boolean_options = {
            "native_shell",
            "allow_unsandboxed_agent",
            "document_input",
            "image_input",
            "stream_thinking",
        }
        for key in boolean_options & self.options.keys():
            if not isinstance(self.options[key], bool):
                raise ConfigurationError(f"profile option '{key}' must be a boolean")
        for key in {
            "api",
            "api_version",
            "location",
            "project",
            "subscription_auth_flow",
        } & self.options.keys():
            if not isinstance(self.options[key], str):
                raise ConfigurationError(f"profile option '{key}' must be a string")
        if "api" in self.options and self.options["api"].strip().lower() not in {
            "chat",
            "responses",
        }:
            raise ConfigurationError("profile option 'api' must be 'chat' or 'responses'")
        if "subscription_auth_flow" in self.options and self.options[
            "subscription_auth_flow"
        ] not in {"browser", "device"}:
            raise ConfigurationError(
                "profile option 'subscription_auth_flow' must be 'browser' or 'device'"
            )
        for key, minimum, maximum in (
            ("max_output_tokens", 1, 1_000_000),
            ("context_fallback_window_tokens", 4096, 4_000_000),
            ("context_recent_turns", 0, 32),
            ("context_reserved_output_tokens", 256, 1_000_000),
            ("context_target_percent", 20, 70),
            ("context_trigger_percent", 50, 100),
            ("context_window_tokens", 4096, 4_000_000),
            ("thinking_budget_tokens", 0, 128_000),
        ):
            if key in self.options and (
                not isinstance(self.options[key], int)
                or isinstance(self.options[key], bool)
                or not minimum <= self.options[key] <= maximum
            ):
                raise ConfigurationError(
                    f"profile option '{key}' must be an integer between {minimum} and {maximum}"
                )
        target = int(self.options.get("context_target_percent", 25))
        trigger = int(self.options.get("context_trigger_percent", 100))
        if target >= trigger:
            raise ConfigurationError(
                "profile option 'context_target_percent' must be smaller than "
                "'context_trigger_percent'"
            )
        for key, minimum, maximum in (
            ("temperature", 0.0, 2.0),
            ("timeout_seconds", 1.0, 7200.0),
        ):
            if key in self.options:
                value = self.options[key]
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(float(value))
                    or not minimum <= float(value) <= maximum
                ):
                    raise ConfigurationError(
                        f"profile option '{key}' must be finite and between {minimum} and {maximum}"
                    )
        if "reasoning_effort" in self.options and self.options["reasoning_effort"] not in {
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        }:
            raise ConfigurationError(
                "profile option 'reasoning_effort' must be minimal, low, medium, high or xhigh"
            )

    @property
    def provider(self) -> ProviderPreset:
        try:
            return get_preset(self.preset)
        except KeyError as exc:
            raise ConfigurationError(str(exc)) from exc

    @property
    def endpoint(self) -> str:
        endpoint = (self.base_url or self.provider.base_url).rstrip("/")
        if endpoint and self.provider.protocol in {"openai-chat", "openai-responses"}:
            return normalize_openai_base_url(endpoint)
        return endpoint

    @property
    def key_env(self) -> str:
        return self.api_key_env or self.provider.api_key_env

    @property
    def argv(self) -> list[str]:
        return list(self.command or self.provider.command)

    @property
    def capabilities(self) -> frozenset[str]:
        if self.probed_capabilities is not None:
            capabilities = set(self.probed_capabilities)
        else:
            capabilities = set(self.provider.declared_capabilities)
        return frozenset(capabilities)

    def resolve_api_key(self, environ: dict[str, str] | None = None) -> str:
        if self.provider.auth in {"none", "adc", "managed"}:
            return ""
        env = os.environ if environ is None else environ
        value = env.get(self.key_env, "").strip()
        if not value and environ is None:
            # Environment variables remain a useful explicit override for CI and
            # automation.  Interactive qrenderdoc profiles may instead bind the
            # same stable profile id to the OS credential vault.  Explicit test
            # mappings never fall through to a developer's real credential store.
            from .credential_store import default_credential_store

            value = (default_credential_store().read(self.id) or "").strip()
        if not value:
            raise AuthenticationError(
                f"profile '{self.id}' needs environment variable {self.key_env}; "
                "or a saved OS credential; raw secrets are never stored in profiles.json"
            )
        return normalize_api_credential(value)

    def resolve_model(self, override: str = "") -> str:
        """Resolve a per-run model without mutating the saved profile.

        A non-empty ``models`` list is the explicit picker allow-list.  Keeping
        this check in the profile layer lets qrenderdoc, API runs, Codex and ACP
        apply exactly the same model-selection rule.
        """

        candidate = str(override or "").strip() or self.default_model or self.model
        if not candidate and self.provider.kind == "model":
            raise ConfigurationError(f"profile '{self.id}' requires a model name")
        if override and self.models and candidate not in self.models:
            raise ConfigurationError(
                f"model '{candidate}' is not enabled for profile '{self.id}'"
            )
        return candidate

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Profile":
        known = {item.name for item in cls.__dataclass_fields__.values()}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ConfigurationError(f"unknown profile fields: {', '.join(unknown)}")
        try:
            profile = cls(**value)
        except TypeError as exc:
            raise ConfigurationError(f"invalid profile field types: {exc}") from exc
        profile.validate()
        return profile


class ProfileStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_config_path()
        self.active_profile = ""
        self.profiles: dict[str, Profile] = {}
        self._baseline: dict[str, dict] = {}
        self._baseline_active = ""

    def load(self) -> "ProfileStore":
        if not self.path.exists():
            return self
        try:
            from .configuration_io import open_shared_read
            with open_shared_read(self.path) as stream:
                payload = json.loads(stream.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"cannot read profile store {self.path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema") not in {
            PROFILE_SCHEMA,
            *LEGACY_PROFILE_SCHEMAS,
        }:
            raise ConfigurationError(f"unsupported profile schema in {self.path}")
        raw_profiles = payload.get("profiles", [])
        if not isinstance(raw_profiles, list):
            raise ConfigurationError("profiles must be an array")
        profiles = [Profile.from_dict(item) for item in raw_profiles if isinstance(item, dict)]
        if len(profiles) != len(raw_profiles):
            raise ConfigurationError("every profile must be an object")
        self.profiles = {profile.id: profile for profile in profiles}
        if len(self.profiles) != len(profiles):
            raise ConfigurationError("profile ids must be unique")
        self.active_profile = str(payload.get("activeProfile", ""))
        if self.active_profile and self.active_profile not in self.profiles:
            raise ConfigurationError("activeProfile does not exist")
        self._baseline = {key: asdict(value) for key, value in self.profiles.items()}
        self._baseline_active = self.active_profile
        return self

    def save(self) -> None:
        from .configuration_io import file_transaction
        with file_transaction(self.path):
            self._save_transaction()

    def _save_transaction(self) -> None:
        # Merge only this writer's edits. A second application may have added
        # or removed an unrelated connection since this UI loaded its catalog.
        if self.path.exists():
            current = ProfileStore(self.path).load()
            merged = dict(current.profiles)
            for key in self._baseline.keys() - self.profiles.keys():
                merged.pop(key, None)
            for key, profile in self.profiles.items():
                if key not in self._baseline or asdict(profile) != self._baseline[key]:
                    merged[key] = profile
            if self.active_profile == self._baseline_active:
                self.active_profile = current.active_profile
            self.profiles = merged
            if self.active_profile not in merged:
                self.active_profile = sorted(merged)[0] if merged else ""
        for profile in self.profiles.values():
            profile.validate()
        if self.active_profile and self.active_profile not in self.profiles:
            raise ConfigurationError("activeProfile does not exist")
        payload = {
            "schema": PROFILE_SCHEMA,
            "activeProfile": self.active_profile,
            "profiles": [asdict(self.profiles[key]) for key in sorted(self.profiles)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix="profiles-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            import time
            for attempt in range(6):
                try:
                    os.replace(temporary, self.path)
                    break
                except OSError as error:
                    if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 5:
                        raise
                    time.sleep(.01 * 2**attempt)
        finally:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass
        self._baseline = {key: asdict(value) for key, value in self.profiles.items()}
        self._baseline_active = self.active_profile

    def add(self, profile: Profile, *, select: bool = False) -> None:
        profile.validate()
        self.profiles[profile.id] = profile
        if select or not self.active_profile:
            self.active_profile = profile.id

    def select(self, profile_id: str) -> Profile:
        try:
            profile = self.profiles[profile_id]
        except KeyError as exc:
            raise ConfigurationError(f"unknown profile: {profile_id}") from exc
        self.active_profile = profile_id
        return profile

    def active(self) -> Profile:
        if not self.active_profile:
            raise ConfigurationError("no active AI profile; add or select one first")
        return self.select(self.active_profile)


__all__ = [
    "LEGACY_PROFILE_SCHEMAS",
    "PROFILE_SCHEMA",
    "Profile",
    "ProfileStore",
    "azure_endpoint_uses_v1",
    "default_config_path",
    "is_loopback_endpoint",
    "normalize_openai_base_url",
]
