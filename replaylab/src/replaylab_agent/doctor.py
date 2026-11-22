"""Offline connection diagnostics for a ReplayLab AI profile."""

from __future__ import annotations

import importlib.util
import asyncio
import sys
from collections.abc import Mapping
from typing import Callable
from dataclasses import dataclass
from pathlib import Path

from .bridge import BridgeClient
from .conversation import resolve_context_spec
from .errors import UnknownModelContextError
from .connectors.claude_auth import check_claude_auth_status
from .mcp_runtime import find_replaylab_mcp
from .profiles import Profile
from .process_env import (
    agent_command_argv,
    codex_profile_home,
    is_packaged_windows_executable as _is_packaged_windows_executable,
    resolve_agent_executable,
    resolve_executable,
)
from .agent_registry import CatalogView


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
    message_id: str = ""
    message_args: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "ok"


def _command_path(command: str) -> str:
    return resolve_executable(command)


def _profile_context_models(profile: Profile) -> list[tuple[str, str]]:
    """Return every current/default/enabled selection exactly once."""

    candidates: list[tuple[str, str]] = []
    if profile.model:
        candidates.append(("current model", profile.model))
    if profile.default_model:
        candidates.append(("default model", profile.default_model))
    for index, model in enumerate(profile.models, start=1):
        candidates.append((f"enabled model {index}", model))
    if not candidates and profile.provider.kind == "agent":
        candidates.append(("provider default", ""))

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, model in candidates:
        if model in seen:
            continue
        seen.add(model)
        unique.append((label, model))
    return unique


def run_doctor(
    profile: Profile,
    catalog: CatalogView,
    *,
    bridge: BridgeClient | None = None,
    mcp_executable: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    include_external_mcp: bool = True,
    progress_callback: Callable[[DoctorCheck], None] | None = None,
) -> list[DoctorCheck]:
    """Run non-networked checks; no login or provider quota is consumed."""

    class CheckResults(list):
        def append(self, check):
            super().append(check)
            if progress_callback is not None:
                progress_callback(check)
    checks: list[DoctorCheck] = CheckResults()
    version_ok = sys.version_info >= (3, 12)
    checks.append(
        DoctorCheck(
            "python",
            "ok" if version_ok else "error",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )

    profile_valid = False
    try:
        profile.validate()
        profile_valid = True
        checks.append(DoctorCheck("profile", "ok", f"{profile.provider.title} / {profile.id}"))
    except Exception as exc:
        checks.append(DoctorCheck("profile", "error", str(exc)))

    if profile_valid:
        selections = _profile_context_models(profile)
        for index, (label, model) in enumerate(selections):
            check_name = "model-context" if index == 0 else "model-context-enabled"
            try:
                context = resolve_context_spec(profile, model=model)
                detail = f"{context.context_window} tokens ({context.source})"
                if index:
                    detail = f"{label}: {detail}"
                checks.append(DoctorCheck(check_name, "ok", detail))
            except UnknownModelContextError as exc:
                detail = (f"{label}: " if index else "") + str(exc)
                checks.append(DoctorCheck(check_name, "warning", detail,
                    "doctor.context-window-unknown", (model or "<provider default>",)))
            except Exception as exc:
                detail = str(exc)
                if index:
                    detail = f"{label}: {detail}"
                checks.append(DoctorCheck(check_name, "error", detail))

    if profile.provider.kind == "model":
        provider_module = {
            "openai-responses": "langchain_openai",
            "openai-chat": "langchain_openai",
            "anthropic-messages": "langchain_anthropic",
            "google-genai": "langchain_google_genai",
            "google-cloud": "langchain_google_vertexai",
            "azure-openai": "langchain_openai",
        }.get(profile.provider.protocol, "")
        required = ["deepagents", "langchain_core", "mcp"]
        if provider_module:
            required.append(provider_module)
        missing = [name for name in required if importlib.util.find_spec(name) is None]
        checks.append(
            DoctorCheck(
                "model-runtime",
                "ok" if not missing else "error",
                "deepagents and the selected LangChain provider are available"
                if not missing
                else "missing runtime package(s): " + ", ".join(missing),
            )
        )
        try:
            profile.resolve_api_key()
            key_detail = (
                "local/ADC authentication"
                if not profile.key_env
                else f"credential available via {profile.key_env} or the OS secure store"
            )
            checks.append(DoctorCheck("authentication", "ok", key_detail))
        except Exception as exc:
            checks.append(DoctorCheck("authentication", "error", str(exc)))
    else:
        argv = agent_command_argv(profile)
        command = argv[0] if argv else ""
        resolved = resolve_agent_executable(profile, environ=environ) if command else ""
        packaged = bool(resolved and _is_packaged_windows_executable(resolved))
        command_status = "error" if not resolved or packaged else "ok"
        command_detail = resolved or f"command not found: {command}"
        if packaged:
            command_detail = (
                f"{resolved} is a packaged WindowsApps executable and may reject child-process "
                "launch; install the vendor CLI or configure an explicit launchable executable"
            )
        checks.append(
            DoctorCheck(
                "agent-command",
                command_status,
                command_detail,
            )
        )
        if profile.provider.protocol == "codex-app-server":
            home = codex_profile_home(profile, environ=environ)
            checks.append(
                DoctorCheck(
                    "codex-internal-tools",
                    "ok",
                    "task-scoped Bread MCP with isolated Codex home; "
                    "no persistent MCP registration is injected into CODEX_HOME: "
                    + str(home),
                )
            )
        if profile.provider.protocol == "acp":
            trusted = profile.options.get("allow_unsandboxed_agent") is True
            checks.append(
                DoctorCheck(
                    "acp-process-trust",
                    "ok" if trusted else "error",
                    "unsandboxed vendor process explicitly trusted"
                    if trusted
                    else "set allow_unsandboxed_agent=true only for a trusted official CLI",
                )
            )
            installed = importlib.util.find_spec("acp") is not None
            checks.append(
                DoctorCheck(
                    "acp-runtime",
                    "ok" if installed else "error",
                    "agent-client-protocol available"
                    if installed
                    else "install replaylab-agenthost",
                )
            )
            if profile.preset == "claude-code-acp":
                if profile_valid and trusted and command_status == "ok":
                    try:
                        authentication = asyncio.run(check_claude_auth_status(
                            profile, environ=environ, timeout=5.0))
                        authenticated = (
                            authentication.logged_in is True
                            and authentication.state == "authenticated")
                        checks.append(DoctorCheck(
                            "claude-authentication", "ok" if authenticated else "error",
                            authentication.detail, authentication.message_id,
                            authentication.message_args))
                    except Exception:
                        # CLI output and exception messages may contain account data.
                        # Only the shared bounded status code is exposed to the UI.
                        checks.append(DoctorCheck(
                            "claude-authentication", "error",
                            "Claude sign-in status could not be verified: process-failed",
                            "subscription.claude-auth-status-failed", ("process-failed",)))
                else:
                    checks.append(DoctorCheck(
                        "claude-authentication", "warning",
                        "Claude sign-in status was not checked because the profile or trusted CLI is unavailable.",
                        "claude-auth.skipped"))

    if include_external_mcp:
        try:
            host = find_replaylab_mcp(mcp_executable)
            checks.append(DoctorCheck("bread-mcp", "ok", str(host)))
        except Exception as exc:
            checks.append(DoctorCheck("bread-mcp", "error", str(exc)))

    checks.append(
        DoctorCheck(
            "skills",
            "ok" if catalog.skills else "error",
            f"{len(catalog.skills)} validated skill(s)",
        )
    )
    from .portable_capabilities import public_status
    portable = public_status()
    if portable["present"]:
        checks.append(DoctorCheck("portable-capabilities", "ok" if portable["ready"] else "warning",
                              str(portable["plugins"]) + " bundled plugins; missing: " + ", ".join(portable["missing"]),
                              "connection.portable-ok" if portable["ready"] else "connection.portable-missing",
                                  (str(portable["plugins"]),)))
    quarantine_summary = getattr(catalog, "quarantine_summary", None)
    if callable(quarantine_summary):
        summary = quarantine_summary()
        if summary.quarantine_count:
            checks.append(
                DoctorCheck(
                    "skills-quarantine",
                    "warning",
                    summary.doctor_detail(),
                )
            )

    client = bridge or BridgeClient(timeout=1.0)
    try:
        instances = client.instances()
        checks.append(
            DoctorCheck(
                "qrenderdoc",
                "ok" if instances else "warning",
                f"{len(instances)} live Bread bridge instance(s)"
                if instances
                else "no live qrenderdoc bridge; start qrenderdoc and open a capture",
            )
        )
    except Exception as exc:
        checks.append(DoctorCheck("qrenderdoc", "warning", str(exc)))
    return checks


__all__ = ["DoctorCheck", "run_doctor"]
