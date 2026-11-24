"""ReplayLab AgentHost command line.

Only three top-level commands are intentionally exposed:

* ``run`` executes one task;
* ``profile`` owns every connection-management operation;
* ``doctor`` performs offline diagnostics.

Model API profiles always use deepagents. Subscription profiles always use
the provider's native Agent protocol. There is no legacy-engine selector.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import threading
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Sequence

from .agent_contracts import RunRequest
from .agent_diagnostics import configure_logging, log_exception
from .agent_registry import CatalogView
from .bridge import BridgeClient
from .catalog import PRESETS, get_preset
from .conversation import (
    NATIVE_PROMPT_WIRE_MAX_BYTES,
    SUPPORTED_CONVERSATION_SCHEMAS,
    parse_conversation_prompt,
)
from .deep_engine import DeepAgentsEngine
from .doctor import run_doctor
from .engine_config import load_engine_config
from .agent_asset_readers import (
    BUILTIN_READERS,
    MCP_PRESETS,
    PRESETS as READER_PRESETS,
    AssetReaderError,
    default_readers_path,
    discover_installed_readers,
    load_readers,
    reader_contract,
    registration_from_command,
    remove_reader,
    set_reader_enabled,
    upsert_reader,
)
from .builtin_unpackers import (
    BUILTIN_UNPACKERS,
    builtin_unpacker,
    builtin_unpacker_definition,
    builtin_unpacker_rows,
    uex_mcp_server_definition,
)
from .agent_tool_providers import (
    CONNECTION_KINDS,
    ROLES as TOOL_ROLES,
    ToolConfigError,
    default_tools_path as tool_providers_path,
    load_tools as load_tool_providers,
    migrate_legacy as migrate_tool_providers,
    remove_tool as remove_tool_provider,
    runtime_servers,
    set_tool_enabled as set_tool_provider_enabled,
    slug as tool_slug,
    tool_runtime_status,
    upsert_tool as upsert_tool_provider,
)
from .agent_tools_config import (
    AgentToolsConfig,
    describe_skills,
    mcp_server_runtime_status,
    servers_for_runtime,
    user_skill_overlay_root,
    validate_mcp_server,
)
from .errors import ConfigurationError, ReplayLabAIError
from .native_engine import NativeAgentEngine
from .profiles import Profile, ProfileStore
from .probe import discover_models
from .process_env import (
    remove_codebuddy_profile_home,
    remove_codex_profile_home,
    remove_cursor_profile_home,
    remove_managed_vendor_profile_home,
)
from .tool_scope import SUPPORTED_INTENT_KINDS
from .run_lifetime import capture_gui_owner, run_lifetime, run_until_cancelled

_NATIVE_START_GATE_ENV = "REPLAYLAB_START_GATED"
_NATIVE_START_GATE_RECORD = b"start\n"
_MAX_STDERR_BYTES = 64 * 1024
_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


def _validated_conversation_id(value: str) -> str:
    """Admit only the stable opaque ids shared with the Host sidecar contract."""

    candidate = str(value or "")
    if candidate and not _CONVERSATION_ID_RE.fullmatch(candidate):
        raise ValueError("conversation identity is invalid")
    return candidate


class _SafeArgumentParser(argparse.ArgumentParser):
    """Keep rejected command-line values out of the terminal transcript."""

    def error(self, message: str) -> None:
        del message
        self.print_usage(sys.stderr)
        self.exit(
            2,
            "replaylab-ai: error[invalid-command-line]: Invalid command line. "
            "Use --help to review the supported options.\n",
        )


def _add_profile_definition_arguments(
    parser: argparse.ArgumentParser, *, update: bool
) -> None:
    parser.add_argument("--preset", required=not update)
    parser.add_argument("--model", default=None if update else "")
    parser.add_argument("--allowed-model", action="append", default=None if update else [])
    parser.add_argument("--default-model", default=None if update else "")
    parser.add_argument(
        "--model-context-window",
        action="append",
        default=None if update else [],
        metavar="MODEL=TOKENS",
    )
    parser.add_argument("--title", default=None if update else "")
    parser.add_argument("--base-url", default=None if update else "")
    parser.add_argument("--api-key-env", default=None if update else "")
    parser.add_argument("--agent-env", action="append", default=None if update else [])
    parser.add_argument("--option", action="append", default=None if update else [])
    parser.add_argument("--capability", action="append", default=None if update else [])
    parser.add_argument("--select", action="store_true")
    parser.add_argument("--json", action="store_true")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--command", dest="agent_command", nargs="+", default=None if update else [])
    commands.add_argument("--command-json", default=None if update else "", metavar="JSON")
    if update:
        parser.add_argument("--replace-provider", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="replaylab-ai",
        description="Bread AgentHost",
    )
    parser.add_argument("--config", type=Path, help="override profiles.json")
    parser.add_argument("--engine-config", type=Path, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    cleanup = commands.add_parser("conversation-cleanup", help="remove one deleted chat's task files")
    cleanup_target = cleanup.add_mutually_exclusive_group(required=True)
    cleanup_target.add_argument("--conversation-id", type=_validated_conversation_id)
    cleanup_target.add_argument("--pending", action="store_true")
    cleanup.add_argument("--task-id", action="append", default=[])
    cleanup.add_argument("--json", action="store_true")

    run = commands.add_parser("run", help="run one chat or exact Skill request")
    run.add_argument("--profile", default="")
    run.add_argument("--model", default="")
    run.add_argument("--skill", default="")
    run.add_argument("--intent-kind", choices=SUPPORTED_INTENT_KINDS, default="")
    prompt = run.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-stdin", action="store_true")
    run.add_argument("--context-file", type=Path)
    run.add_argument("--live-context", action="store_true")
    run.add_argument("--instance-id", default="")
    run.add_argument("--task-id", default="")
    run.add_argument(
        "--conversation-id",
        default="",
        type=_validated_conversation_id,
        help=argparse.SUPPRESS,
    )
    run.add_argument("--cancel-file", type=Path)
    run.add_argument("--mcp-host", type=Path)
    # "@mcp:<id>" in the chat box: enable a configured server for this request only.
    run.add_argument("--enable-mcp", action="append", default=[])
    run.add_argument("--json", action="store_true")

    profile = commands.add_parser("profile", help="manage AI connections")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    presets = profile_commands.add_parser("presets")
    presets.add_argument("--json", action="store_true")
    listing = profile_commands.add_parser("list")
    listing.add_argument("--json", action="store_true")
    add = profile_commands.add_parser("add")
    add.add_argument("id")
    _add_profile_definition_arguments(add, update=False)
    update = profile_commands.add_parser("update")
    update.add_argument("id")
    _add_profile_definition_arguments(update, update=True)
    select = profile_commands.add_parser("select")
    select.add_argument("id")
    select.add_argument("--json", action="store_true")
    remove = profile_commands.add_parser("remove")
    remove.add_argument("id")
    remove.add_argument("--json", action="store_true")
    models = profile_commands.add_parser("models")
    models.add_argument("id")
    models.add_argument("--allowed-model", action="append", default=None)
    models.add_argument("--default-model")
    models.add_argument("--model-context-window", action="append", default=None)
    models.add_argument("--json", action="store_true")

    discover = profile_commands.add_parser("discover-models")
    source = discover.add_mutually_exclusive_group()
    source.add_argument("--profile", default="")
    source.add_argument("--preset", default="")
    discover.add_argument("--base-url", default="")
    discover.add_argument("--api-key-env", default="")
    discover.add_argument("--api-key-stdin", action="store_true")
    discover.add_argument("--timeout", type=float, default=15.0)
    discover.add_argument("--include-hidden", action="store_true")
    discover.add_argument("--json", action="store_true")

    test_model = profile_commands.add_parser("test-model")
    source = test_model.add_mutually_exclusive_group()
    source.add_argument("--profile", default="")
    source.add_argument("--preset", default="")
    test_model.add_argument("--model", default="")
    test_model.add_argument("--timeout", type=float, default=90.0)
    test_model.add_argument("--json", action="store_true")

    credential = profile_commands.add_parser("credential")
    credential_commands = credential.add_subparsers(dest="credential_command", required=True)
    for name in ("status", "delete"):
        operation = credential_commands.add_parser(name)
        operation.add_argument("--profile", default="")
        operation.add_argument("--json", action="store_true")
    store = credential_commands.add_parser("store")
    store.add_argument("--profile", default="")
    store.add_argument("--api-key-stdin", action="store_true", required=True)
    store.add_argument("--json", action="store_true")

    subscription = profile_commands.add_parser("subscription")
    subscription_commands = subscription.add_subparsers(
        dest="subscription_command", required=True
    )
    status = subscription_commands.add_parser("status")
    status.add_argument("--profile", required=True)
    status.add_argument("--json", action="store_true")
    connect = subscription_commands.add_parser("connect")
    connect.add_argument("--profile", required=True)
    connect.add_argument("--flow", choices=("browser", "device"), default="browser")
    connect.add_argument("--timeout", type=float, default=600.0)
    connect.add_argument("--start-gated", action="store_true", required=True)
    connect.add_argument("--json", action="store_true")
    test = subscription_commands.add_parser("test")
    test.add_argument("--profile", required=True)
    test.add_argument("--timeout", type=float, default=60.0)
    test.add_argument("--start-gated", action="store_true", required=True)
    test.add_argument("--json", action="store_true")

    unpacker = commands.add_parser(
        "unpacker", help="run a bundled unpacker embedded in this executable"
    )
    unpacker.add_argument("kind", choices=["unitypy"])
    unpacker.add_argument("arguments", nargs=argparse.REMAINDER)

    tools = commands.add_parser(
        "tools", help="manage the Skills and MCP servers offered to every AI connection"
    )
    tools.add_argument("--tools-config", type=Path)
    tools_commands = tools.add_subparsers(dest="tools_command", required=True)
    tools_commands.add_parser("list")
    skill_enable = tools_commands.add_parser("skill-enable")
    skill_enable.add_argument("skill_id")
    skill_disable = tools_commands.add_parser("skill-disable")
    skill_disable.add_argument("skill_id")
    add_root = tools_commands.add_parser("add-skill-root")
    add_root.add_argument("path", type=Path)
    remove_root = tools_commands.add_parser("remove-skill-root")
    remove_root.add_argument("path", type=Path)
    mcp_set = tools_commands.add_parser("mcp-set")
    mcp_set.add_argument("--json-stdin", action="store_true")
    mcp_set.add_argument("--server-json", default="")
    mcp_remove = tools_commands.add_parser("mcp-remove")
    mcp_remove.add_argument("server_id")
    mcp_enable = tools_commands.add_parser("mcp-enable")
    mcp_enable.add_argument("server_id")
    mcp_disable = tools_commands.add_parser("mcp-disable")
    mcp_disable.add_argument("server_id")
    mcp_test = tools_commands.add_parser("mcp-test")
    mcp_test.add_argument("server_id")
    mcp_test.add_argument("--timeout", type=float, default=25.0)
    skill_show = tools_commands.add_parser("skill-show")
    skill_show.add_argument("skill_id")
    skill_write = tools_commands.add_parser("skill-write")
    skill_write.add_argument("skill_id")
    skill_write.add_argument("--content-stdin", action="store_true")
    skill_write.add_argument("--content-file", type=Path)
    skill_new = tools_commands.add_parser("skill-new")
    skill_new.add_argument("skill_id")
    skill_new.add_argument("--title", default="")
    skill_remove = tools_commands.add_parser("skill-remove")
    skill_remove.add_argument("skill_id")
    tools_commands.add_parser("reader-list")
    tools_commands.add_parser("reader-discover")
    tools_commands.add_parser("tool-list")
    tool_add = tools_commands.add_parser("tool-add")
    tool_add.add_argument("--kind", choices=("mcp-stdio", "mcp-http", "cli"), default="cli")
    tool_add.add_argument("--name", default="")
    tool_add.add_argument("--description", default="")
    tool_add.add_argument("--command", default="")
    tool_add.add_argument("--arg", action="append", default=[])
    tool_add.add_argument("--url", default="")
    tool_add.add_argument("--bearer-env", default="")
    tool_add.add_argument("--header", action="append", default=[])
    tool_add.add_argument("--header-env", action="append", default=[])
    tool_add.add_argument("--env", action="append", default=[])
    tool_add.add_argument("--role", action="append", default=[])
    tool_add.add_argument("--id", default="")
    tool_set = tools_commands.add_parser("tool-set")
    tool_set.add_argument("--json-stdin", action="store_true")
    tool_set.add_argument("--tool-json", default="")
    tool_remove = tools_commands.add_parser("tool-remove")
    tool_remove.add_argument("tool_id")
    tool_enable = tools_commands.add_parser("tool-enable")
    tool_enable.add_argument("tool_id")
    tool_disable = tools_commands.add_parser("tool-disable")
    tool_disable.add_argument("tool_id")
    # Every tools sub-command gets --json from the loop below; adding it here too
    # would be a duplicate option and argparse refuses the whole parser.
    tools_commands.add_parser("tool-migrate")
    tool_detect = tools_commands.add_parser("tool-detect")
    tool_detect.add_argument("kind")
    tool_test = tools_commands.add_parser("tool-test")
    tool_test.add_argument("tool_id")
    tool_test.add_argument("--timeout", type=float, default=30.0)
    reader_add = tools_commands.add_parser("reader-add")
    reader_add.add_argument("command")
    reader_add.add_argument("--name", default="")
    reader_add.add_argument("--description", default="")
    reader_set = tools_commands.add_parser("reader-set")
    reader_set.add_argument("--json-stdin", action="store_true")
    reader_set.add_argument("--reader-json", default="")
    reader_remove = tools_commands.add_parser("reader-remove")
    reader_remove.add_argument("reader_id")
    reader_enable = tools_commands.add_parser("reader-enable")
    reader_enable.add_argument("reader_id")
    reader_disable = tools_commands.add_parser("reader-disable")
    reader_disable.add_argument("reader_id")
    for parser_name in tools_commands.choices.values():
        parser_name.add_argument("--json", action="store_true")

    doctor = commands.add_parser("doctor", help="run offline connection diagnostics")
    doctor.add_argument("--profile", default="")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--jsonl", action="store_true", help="Stream each diagnostic row as it completes")
    doctor.add_argument("--mcp-host", type=Path)
    doctor.add_argument("--execution-test", action="store_true", help="Run one model + no-effect MCP tool roundtrip")
    doctor.add_argument("--timeout", type=float, default=120.0)
    # Point the Skill/MCP configuration somewhere else -- a path that does not exist is
    # fine and means "no user configuration". The packaging smoke test uses this so it
    # measures the build it just produced rather than the developer's own machine.
    doctor.add_argument("--tools-config", type=Path)
    return parser


def default_skill_root() -> Path:
    if getattr(sys, "frozen", False):
        installed = Path(sys.executable).resolve().parent / "skills"
        if installed.is_dir():
            # Settings must receive persistent, application-owned paths, not
            # a one-file extraction directory deleted when this CLI exits.
            return installed
        bundle_root = getattr(sys, "_MEIPASS", "")
        if bundle_root:
            return Path(bundle_root) / ".agents" / "skills"
    return Path(__file__).resolve().parents[3] / ".agents" / "skills"


def _load_store(path: Path | None) -> ProfileStore:
    return ProfileStore(path).load()


def _tools_config(path: Path | None = None) -> AgentToolsConfig:
    return AgentToolsConfig.load(path)


def _catalog(tools: AgentToolsConfig | None = None) -> CatalogView:
    """Every route loads the same catalog: bundled Skills plus the user's folders."""

    config = tools if tools is not None else _tools_config()
    catalog = CatalogView.load(skill_roots=config.skill_root_list(default_skill_root()))
    return config.filter_catalog(catalog)


def _parse_options(values: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in values:
        if "=" not in item:
            raise ValueError("profile option must be KEY=VALUE")
        key, raw = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("profile option key is empty")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        if isinstance(value, (dict, list)):
            raise ValueError(f"profile option must be scalar: {key}")
        result[key] = value
    return result


def _parse_model_context_windows(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in values:
        if "=" not in item:
            raise ValueError("model context window must be MODEL=TOKENS")
        model, raw = (part.strip() for part in item.rsplit("=", 1))
        if not model:
            raise ValueError("model context window model id is empty")
        try:
            window = int(raw, 10)
        except ValueError as exc:
            raise ValueError("model context window tokens must be a decimal integer") from exc
        if model in result:
            raise ValueError("model context window model ids must be unique")
        result[model] = window
    return result


def _parse_agent_command(args: argparse.Namespace) -> list[str]:
    encoded = str(getattr(args, "command_json", "") or "")
    if not encoded:
        return list(getattr(args, "agent_command", None) or [])
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError("--command-json exceeds the 64 KiB safety limit")
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("--command-json must be valid JSON") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("--command-json must contain an array of strings")
    if len(value) > 256:
        raise ValueError("--command-json may contain at most 256 arguments")
    return value


def _machine_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8", errors="backslashreplace"
    )


def _print_machine_json(value: Any) -> None:
    encoded = _machine_json(value) + b"\n"
    binary = getattr(sys.stdout, "buffer", None)
    if binary is not None:
        binary.write(encoded)
        binary.flush()
    else:
        sys.stdout.write(encoded.decode("utf-8"))
        sys.stdout.flush()


def _selected_profile(store: ProfileStore, profile_id: str) -> Profile:
    if profile_id:
        try:
            return store.profiles[profile_id]
        except KeyError as exc:
            raise ValueError(f"unknown profile: {profile_id}") from exc
    return store.active()


def _print_presets(as_json: bool) -> int:
    rows = [
        {
            "id": item.id,
            "title": item.title,
            "kind": item.kind,
            "executionMode": "model-api" if item.kind == "model" else "agent-protocol",
            "loopOwner": "deepagents" if item.kind == "model" else "vendor",
            "configurationMode": item.configuration_mode,
            "protocol": item.protocol,
            "auth": item.auth,
            "authenticationUi": item.authentication_ui,
            "supportsModelDiscovery": item.supports_model_discovery,
            "supportsManagedLogin": item.subscription_login_mode != "none",
            "subscriptionLoginMode": item.subscription_login_mode,
            "interactiveTerminalRequired": item.subscription_login_mode == "terminal",
            "installHint": item.install_hint,
            "defaultModel": "",
            "baseUrl": item.base_url,
            "apiKeyEnv": item.api_key_env,
            "command": list(item.command),
            "note": item.note,
        }
        for item in PRESETS
    ]
    if as_json:
        _print_machine_json(rows)
    else:
        print(f"{'ID':<18} {'TYPE':<7} {'PROTOCOL':<21} TITLE")
        for row in rows:
            print(f"{row['id']:<18} {row['kind']:<7} {row['protocol']:<21} {row['title']}")
    return 0


def _profile_wire(profile: Profile, *, active: bool) -> dict[str, Any]:
    return {
        "id": profile.id,
        "title": profile.title or profile.id,
        "active": active,
        "preset": profile.preset,
        "kind": profile.provider.kind,
        "executionMode": "model-api" if profile.provider.kind == "model" else "agent-protocol",
        "loopOwner": "deepagents" if profile.provider.kind == "model" else "vendor",
        "protocol": profile.provider.protocol,
        "auth": profile.provider.auth,
        "model": profile.model,
        "models": list(profile.models),
        "defaultModel": profile.default_model,
        "modelContextWindows": dict(profile.model_context_windows),
        "baseUrl": profile.base_url,
        "endpoint": profile.endpoint,
        "apiKeyEnv": profile.key_env,
        "command": profile.argv,
        "agentEnv": list(profile.agent_env),
        "options": dict(profile.options),
        "capabilities": sorted(profile.capabilities),
        "runInputSchemas": list(SUPPORTED_CONVERSATION_SCHEMAS),
    }


def _remove_provider_state(profile: Profile) -> None:
    if profile.preset == "codex":
        remove_codex_profile_home(profile)
    elif profile.preset == "cursor-acp":
        remove_cursor_profile_home(profile)
    elif profile.preset == "codebuddy-acp":
        remove_codebuddy_profile_home(profile)
    elif profile.preset in {
        "copilot-acp",
        "gemini-acp",
        "qwen-acp",
        "kimi-acp",
        "claude-code-acp",
    }:
        remove_managed_vendor_profile_home(profile)


def _profile_command(
    args: argparse.Namespace,
    store: ProfileStore,
    credential_store: Any | None = None,
) -> int:
    command = args.profile_command
    if command == "list":
        rows = [
            _profile_wire(profile, active=profile.id == store.active_profile)
            for profile in sorted(store.profiles.values(), key=lambda item: item.id)
        ]
        if args.json:
            _print_machine_json(rows)
        elif not rows:
            print("No AI profiles configured.")
        else:
            print(f"{'ACTIVE':<7} {'ID':<22} {'TYPE':<7} {'PRESET':<18} MODEL")
            for row in rows:
                print(
                    f"{('*' if row['active'] else ''):<7} {row['id']:<22} "
                    f"{row['kind']:<7} {row['preset']:<18} {row['model']}"
                )
        return 0

    if command == "add":
        get_preset(args.preset)
        models = list(dict.fromkeys(args.allowed_model))
        legacy_model = str(args.model or "").strip()
        default_model = str(args.default_model or "").strip()
        for model in (legacy_model, default_model):
            if model and model not in models:
                models.append(model)
        selected = legacy_model or default_model or (models[0] if models else "")
        profile = Profile(
            id=args.id,
            preset=args.preset,
            model=selected,
            models=models,
            default_model=default_model or selected,
            title=args.title,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
            command=_parse_agent_command(args),
            agent_env=list(dict.fromkeys(args.agent_env)),
            options=_parse_options(args.option),
            probed_capabilities=list(dict.fromkeys(args.capability)) or None,
            model_context_windows=_parse_model_context_windows(args.model_context_window),
        )
        if profile.id in store.profiles:
            raise ValueError(f"profile already exists: {profile.id}")
        store.add(profile, select=args.select)
        store.save()
        if args.json:
            _print_machine_json(_profile_wire(profile, active=profile.id == store.active_profile))
        else:
            print(f"Added profile '{profile.id}' ({profile.provider.title}).")
        return 0

    if command == "select":
        selected = store.select(args.id)
        store.save()
        if args.json:
            _print_machine_json(_profile_wire(selected, active=True))
        else:
            print(f"Active AI profile: {selected.id}")
        return 0

    if command == "remove":
        if args.id not in store.profiles:
            raise ValueError(f"unknown profile: {args.id}")
        removed = store.profiles[args.id]
        # Removing a connection must not depend on deleting optional vendor
        # cache/login files, which may be held by another application instance.
        cleanup_warnings = []
        if (
            credential_store is not None
            and credential_store.supported
            and removed.provider.kind == "model"
            and removed.provider.auth == "api-key"
        ):
            try:
                credential_store.delete(removed.id)
            except (OSError, ConfigurationError) as error:
                cleanup_warnings.append(type(error).__name__ + ": saved credential retained")
        del store.profiles[removed.id]
        if store.active_profile == removed.id:
            store.active_profile = sorted(store.profiles)[0] if store.profiles else ""
        store.save()
        try:
            from .provider_lease import profile_lease
            with profile_lease(store.path, removed.id, cleanup=True):
                _remove_provider_state(removed)
        except (OSError, ConfigurationError) as error:
            cleanup_warnings.append(type(error).__name__ + ": local provider state retained")
        payload = {"removed": removed.id, "activeProfile": store.active_profile}
        if cleanup_warnings:
            payload["cleanupWarnings"] = cleanup_warnings
        if args.json:
            _print_machine_json(payload)
        else:
            print(f"Removed profile '{removed.id}'.")
        return 0

    if command not in {"update", "models"}:
        raise AssertionError(command)
    try:
        current = store.profiles[args.id]
    except KeyError as exc:
        raise ValueError(f"unknown profile: {args.id}") from exc
    raw = asdict(current)
    if command == "models":
        if (
            args.allowed_model is None
            and args.default_model is None
            and args.model_context_window is None
        ):
            raise ValueError(
                "profile models requires --allowed-model, --default-model or --model-context-window"
            )
        if args.allowed_model is not None:
            raw["models"] = list(dict.fromkeys(args.allowed_model))
        if args.default_model is not None:
            raw["default_model"] = args.default_model
            raw["model"] = args.default_model
        if args.model_context_window is not None:
            raw["model_context_windows"] = _parse_model_context_windows(
                args.model_context_window
            )
    else:
        replacing = bool(args.replace_provider)
        requested_preset = args.preset or current.preset
        if args.preset and args.preset != current.preset and not replacing:
            raise ValueError(
                "changing preset requires --replace-provider so old provider settings cannot leak"
            )
        if replacing:
            if not args.preset:
                raise ValueError("--replace-provider requires an explicit --preset")
            get_preset(requested_preset)
            raw = {
                "id": current.id,
                "preset": requested_preset,
                "model": "",
                "models": [],
                "default_model": "",
                "title": current.title,
                "base_url": "",
                "api_key_env": "",
                "command": [],
                "agent_env": [],
                "options": {},
                "probed_capabilities": None,
                "model_context_windows": {},
            }
        for argument, field_name in {
            "preset": "preset",
            "model": "model",
            "default_model": "default_model",
            "title": "title",
            "base_url": "base_url",
            "api_key_env": "api_key_env",
        }.items():
            value = getattr(args, argument, None)
            if value is not None:
                raw[field_name] = value
                if argument == "default_model":
                    raw["model"] = value
        if args.model is not None and args.default_model is None:
            raw["default_model"] = args.model
        if args.allowed_model is not None:
            raw["models"] = list(dict.fromkeys(args.allowed_model))
        if args.agent_env is not None:
            raw["agent_env"] = list(dict.fromkeys(args.agent_env))
        if args.option is not None:
            raw["options"] = {**raw["options"], **_parse_options(args.option)}
        if args.capability is not None:
            raw["probed_capabilities"] = list(dict.fromkeys(args.capability)) or None
        if args.model_context_window is not None:
            raw["model_context_windows"] = _parse_model_context_windows(
                args.model_context_window
            )
        if args.command_json is not None or args.agent_command is not None:
            raw["command"] = _parse_agent_command(args)
    updated = Profile.from_dict(raw)
    if updated.default_model and updated.default_model not in updated.models:
        raise ValueError("default model must also be supplied as --allowed-model")
    provider_changed = command == "update" and args.replace_provider and updated.preset != current.preset
    if (
        provider_changed
        and credential_store is not None
        and credential_store.supported
        and current.provider.kind == "model"
        and current.provider.auth == "api-key"
    ):
        credential_store.delete(current.id)
    if provider_changed:
        _remove_provider_state(current)
    store.profiles[updated.id] = updated
    if getattr(args, "select", False):
        store.active_profile = updated.id
    store.save()
    if args.json:
        _print_machine_json(_profile_wire(updated, active=updated.id == store.active_profile))
    else:
        print(f"Updated profile '{updated.id}'.")
    return 0


def _read_session_api_key() -> str:
    from .credential_store import normalize_api_credential

    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read(64 * 1024 + 1)
    encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(encoded) > 64 * 1024:
        raise ValueError("stdin API key exceeds the 64 KiB safety limit")
    try:
        value = encoded.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("stdin API key must be valid UTF-8") from exc
    if not value or any(character in value for character in ("\0", "\r", "\n")):
        raise ValueError("stdin API key must contain one non-empty line")
    return normalize_api_credential(value)


def _credential_command(
    args: argparse.Namespace,
    profiles: ProfileStore,
    credential_store: Any | None = None,
) -> int:
    from .credential_store import default_credential_store

    profile = _selected_profile(profiles, args.profile)
    backend = credential_store or default_credential_store()
    if args.credential_command == "status":
        stored = bool(backend.read(profile.id)) if backend.supported else False
        payload = {
            "profile": profile.id,
            "backend": backend.backend,
            "supported": bool(backend.supported),
            "stored": stored,
        }
    elif args.credential_command == "store":
        if profile.provider.kind != "model" or profile.provider.auth != "api-key":
            raise ValueError("only model API-key profiles can store an API credential")
        if not backend.supported:
            raise ValueError("no supported operating-system credential store is available")
        backend.write(profile.id, _read_session_api_key())
        payload = {
            "profile": profile.id,
            "backend": backend.backend,
            "supported": True,
            "stored": True,
        }
    elif args.credential_command == "delete":
        deleted = backend.delete(profile.id) if backend.supported else False
        payload = {
            "profile": profile.id,
            "backend": backend.backend,
            "supported": bool(backend.supported),
            "stored": False,
            "deleted": deleted,
        }
    else:
        raise AssertionError(args.credential_command)
    if args.json:
        _print_machine_json(payload)
    elif args.credential_command == "status":
        print("saved" if payload["stored"] else "not saved")
    elif args.credential_command == "store":
        print(f"Saved API credential for profile '{profile.id}'.")
    else:
        print(f"Removed saved API credential for profile '{profile.id}'.")
    return 0


def _discovery_profile(args: argparse.Namespace, store: ProfileStore) -> Profile:
    if args.profile:
        return _selected_profile(store, args.profile)
    if args.preset:
        preset = get_preset(args.preset)
        marker = "__model_discovery__" if preset.kind == "model" else ""
        return Profile(
            id="discovery",
            preset=preset.id,
            model=marker,
            models=[marker] if marker else [],
            default_model=marker,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
        )
    return store.active()


def _codex_connector(profile: Profile, *, timeout: float) -> Any:
    if profile.provider.protocol != "codex-app-server":
        raise ValueError("this operation is available only for a Codex app-server profile")
    from .connectors.codex_app_server import CodexAppServerConnector
    from .process_env import external_agent_environment, resolved_agent_argv

    return CodexAppServerConnector(
        resolved_agent_argv(profile),
        cwd=Path.cwd(),
        timeout=timeout,
        env=external_agent_environment(profile),
    )


async def _models_command(args: argparse.Namespace, store: ProfileStore) -> int:
    profile = _discovery_profile(args, store)
    preset = get_preset(profile.preset)
    if preset.selectable_models:
        models = list(preset.selectable_models)
        payload = {
            "profile": profile.id if args.profile else "",
            "preset": profile.preset,
            "kind": profile.provider.kind,
            "protocol": profile.provider.protocol,
            "endpoint": "",
            "models": models,
            "modelDetails": [{"id": value, "displayName": value} for value in models],
            "attempts": [],
        }
    elif profile.provider.protocol == "codex-app-server":
        if args.api_key_stdin:
            raise ValueError("Codex app-server owns authentication; do not pass an API key")
        async with _codex_connector(profile, timeout=args.timeout) as connector:
            details = await connector.list_models(include_hidden=args.include_hidden)
        models = []
        for item in details:
            value = item.get("id", item.get("model", ""))
            if isinstance(value, str) and value and value not in models:
                models.append(value)
        payload = {
            "profile": profile.id if args.profile else "",
            "preset": profile.preset,
            "kind": profile.provider.kind,
            "protocol": profile.provider.protocol,
            "endpoint": "",
            "models": models,
            "modelDetails": details,
            "attempts": [],
        }
    else:
        key = _read_session_api_key() if args.api_key_stdin else None
        endpoint, models, attempts = await discover_models(
            profile, timeout=args.timeout, api_key=key
        )
        payload = {
            "profile": profile.id if args.profile else "",
            "preset": profile.preset,
            "kind": profile.provider.kind,
            "protocol": profile.provider.protocol,
            "endpoint": endpoint,
            "models": models,
            "modelDetails": [{"id": value, "displayName": value} for value in models],
            "attempts": attempts,
        }
    if args.json:
        _print_machine_json(payload)
    else:
        print("Models: " + (", ".join(payload["models"]) or "<none>"))
    return 0


async def _test_model_command(args: argparse.Namespace, store: ProfileStore) -> int:
    """Send one bounded prompt through an ACP model selection."""

    from .connectors.acp import ACPConnector
    from .process_env import external_agent_environment, resolved_agent_argv

    profile = _discovery_profile(args, store)
    model = str(args.model or "").strip()
    shown = model or "(provider default)"
    payload: dict[str, Any] = {
        "ok": False,
        "model": shown,
        "message": "",
        "diagnostic": "",
    }
    if profile.provider.protocol != "acp":
        payload["message"] = "Model testing is available only for ACP connections."
        payload["diagnostic"] = f"protocol: {profile.provider.protocol}"
        if args.json:
            _print_machine_json(payload)
        else:
            print(payload["message"])
        return 2
    if profile.options.get("allow_unsandboxed_agent") is not True:
        payload["message"] = "The trusted ACP process is not enabled for this connection."
        if args.json:
            _print_machine_json(payload)
        else:
            print(payload["message"])
        return 2
    tested = replace(profile, model=model)
    try:
        connector = ACPConnector(
            resolved_agent_argv(tested),
            cwd=Path.cwd(),
            timeout=float(args.timeout),
            env=external_agent_environment(tested),
            authentication_method=tested.provider.acp_auth_method,
            provider_id=tested.preset,
        )
        outcome = await connector.test_model(model=model)
        payload["ok"] = True
        if outcome.get("modelUnverified"):
            # The agent named no model, so this proves it generates -- not that it
            # generated with the id under test. Claiming the latter is how a
            # plan-restricted model passes here and fails on the first real task.
            payload["message"] = (
                f"The agent responded, but it does not report which model it used, "
                f"so '{shown}' could not be confirmed. It is chosen in that agent's "
                f"own configuration.")
        elif outcome.get("probeModelDiffers"):
            payload["message"] = (
                f"The agent responded, using '{outcome.get('model')}'. This check does "
                f"not select a model, so '{shown}' is applied only on a real run; if "
                f"that id is wrong the run will say so.")
        else:
            payload["message"] = (
                f"Model '{outcome.get('model') or shown}' responded successfully.")
    except Exception as exc:  # user-requested connection diagnostic
        payload["message"] = f"Model test failed for '{shown}'."
        payload["diagnostic"] = str(exc)
    if args.json:
        _print_machine_json(payload)
    else:
        print(payload["message"] + ((" " + payload["diagnostic"]) if payload["diagnostic"] else ""))
    return 0 if payload["ok"] else 1


def _start_subscription_control_reader(
    loop: asyncio.AbstractEventLoop,
    start: asyncio.Event,
    cancel: asyncio.Event,
) -> None:
    """Consume the bounded qrenderdoc start/cancel control protocol."""

    def signal(event: asyncio.Event) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(event.set)

    def records(stream: Any):
        try:
            descriptor = int(stream.fileno())
        except (AttributeError, OSError, TypeError, ValueError):
            while True:
                yield stream.readline(4097)

        pending = bytearray()
        while True:
            newline = pending.find(b"\n")
            if newline >= 0:
                record = bytes(pending[: newline + 1])
                del pending[: newline + 1]
                yield record
                continue
            if len(pending) > 4096:
                yield bytes(pending)
                return
            chunk = os.read(descriptor, 4096)
            if not chunk:
                yield bytes(pending) if pending else b""
                return
            pending.extend(chunk)

    def read_control() -> None:
        try:
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            for raw in records(stream):
                if raw in {b"", ""}:
                    signal(cancel)
                    return
                value = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
                if len(value) > 4096 or not value.endswith(b"\n"):
                    signal(cancel)
                    return
                command = value.strip()
                if command == b"start" and not start.is_set():
                    signal(start)
                    continue
                if command == b"cancel":
                    signal(cancel)
                    return
                # Unknown commands and duplicate starts fail closed.
                signal(cancel)
                return
        except Exception:
            signal(cancel)

    threading.Thread(
        target=read_control,
        name="replaylab-subscription-control",
        daemon=True,
    ).start()


async def _wait_for_subscription_start_gate(
    start: asyncio.Event,
    cancel: asyncio.Event,
    *,
    timeout: float,
) -> None:
    start_wait = asyncio.create_task(start.wait())
    cancel_wait = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait(
            {start_wait, cancel_wait},
            timeout=min(timeout, 30.0),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_wait in done or not done:
            raise ValueError(
                "Subscription start gate closed before 'start'; no provider process was created."
            )
    finally:
        for task in (start_wait, cancel_wait):
            if not task.done():
                task.cancel()
        await asyncio.gather(start_wait, cancel_wait, return_exceptions=True)


async def _subscription_command(
    args: argparse.Namespace,
    store: ProfileStore,
    backend_factory: Any | None = None,
) -> int:
    """Run the qrenderdoc-contained subscription state machine."""

    from .connectors.subscription import (
        SubscriptionBackend,
        SubscriptionCancelled,
        detect_subscription,
    )

    profile = _selected_profile(store, args.profile)
    if args.subscription_command == "status":
        # Status is deliberately process-free; approval is required before a
        # provider process may be created.
        payload = detect_subscription(profile)
        if args.json:
            _print_machine_json(payload)
        else:
            print(payload["message"])
        return 0

    timeout = float(args.timeout)
    if timeout <= 0 or timeout > 3600:
        raise ValueError("subscription timeout must be between 0 and 3600 seconds")
    if not bool(getattr(args, "start_gated", False)):
        raise ValueError("subscription connect/test requires --start-gated")

    def emit(payload: dict[str, Any]) -> None:
        if args.json:
            _print_machine_json(payload)
        elif payload.get("message"):
            print(payload["message"], flush=True)

    backend = (backend_factory or SubscriptionBackend)(profile, timeout=timeout, emit=emit)
    start = asyncio.Event()
    cancel = asyncio.Event()
    _start_subscription_control_reader(asyncio.get_running_loop(), start, cancel)
    terminal_kind = (
        "subscriptionCompleted"
        if args.subscription_command == "connect"
        else "testCompleted"
    )
    backend.event(
        "waitingForStart",
        "waitingForStart",
        "Waiting for qrenderdoc process containment before starting the provider.",
        messageId="subscription.waiting-for-containment",
        messageArgs=[],
    )
    try:
        try:
            await _wait_for_subscription_start_gate(start, cancel, timeout=timeout)
        except ValueError as exc:
            backend.event(
                terminal_kind,
                "cancelled",
                str(exc),
                messageId="subscription.start-gate-closed",
                messageArgs=[],
                authenticated=False,
                testCompleted=False,
            )
            return 1

        if not backend.detection["installed"]:
            backend.event(
                terminal_kind,
                "missing",
                backend.detection["message"],
                messageId=backend.detection.get("messageId", ""),
                messageArgs=list(backend.detection.get("messageArgs", [])),
                installed=False,
                authenticated=False,
                installHint=backend.detection.get("installHint", ""),
                testCompleted=False,
            )
            return 2
        if cancel.is_set():
            backend.event(
                terminal_kind,
                "cancelled",
                "Subscription operation cancelled before provider startup.",
                messageId="subscription.cancelled-before-start",
                messageArgs=[],
                authenticated=False,
                testCompleted=False,
            )
            return 1

        if args.subscription_command == "connect":
            backend.event(
                "connectStarted",
                "connecting",
                "Provider sign-in started inside the qrenderdoc process job.",
                messageId="subscription.sign-in-started",
                messageArgs=[],
            )
            result = await backend.connect(args.flow, cancel)
            backend.event(
                "testCompleted",
                "ok" if result.authenticated else "failed",
                result.message,
                messageId=getattr(result, "message_id", ""),
                messageArgs=list(getattr(result, "message_args", ())),
                authenticated=result.authenticated,
                terminalOpened=result.terminal_opened,
                models=list(result.models),
                account=result.account,
                testCompleted=True,
            )
            backend.event(
                "subscriptionCompleted",
                "connected" if result.authenticated else "failed",
                result.message,
                messageId=getattr(result, "message_id", ""),
                messageArgs=list(getattr(result, "message_args", ())),
                authenticated=result.authenticated,
                terminalOpened=result.terminal_opened,
                models=list(result.models),
                account=result.account,
                testCompleted=True,
            )
        elif args.subscription_command == "test":
            backend.event(
                "testStarted",
                "testing",
                "Running a bounded no-generation provider connection test.",
                messageId="subscription.test-started",
                messageArgs=[],
            )
            result = await backend.test(cancel)
            backend.event(
                "testCompleted",
                "ok" if result.authenticated else "failed",
                result.message,
                messageId=getattr(result, "message_id", ""),
                messageArgs=list(getattr(result, "message_args", ())),
                authenticated=result.authenticated,
                terminalOpened=result.terminal_opened,
                models=list(result.models),
                account=result.account,
                testCompleted=True,
            )
        else:  # pragma: no cover - argparse constrains this value
            raise AssertionError(args.subscription_command)
        return 0 if result.authenticated else 1
    except SubscriptionCancelled as exc:
        backend.event(
            terminal_kind,
            "cancelled",
            str(exc),
            authenticated=False,
            testCompleted=False,
        )
        return 1
    except ReplayLabAIError as exc:
        backend.event(
            terminal_kind,
            "failed",
            str(exc),
            authenticated=False,
            testCompleted=args.subscription_command == "test",
        )
        return 1
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


def _validate_frozen_context(value: dict[str, Any]) -> None:
    if value.get("schema") != "renderdoc-ultra-current-context/v1":
        return
    supplied = value.get("snapshotId")
    if not isinstance(supplied, str) or len(supplied) != 64:
        raise ValueError("frozen context snapshot ID is invalid")
    body = dict(value)
    body.pop("snapshotId", None)
    encoded = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != supplied.lower():
        raise ValueError("frozen context snapshot content hash does not match")


def _read_context(args: argparse.Namespace) -> tuple[dict[str, Any], str, str]:
    if args.context_file and args.live_context:
        raise ValueError("choose either --context-file or --live-context")
    if args.context_file:
        if not args.instance_id:
            raise ValueError("--context-file requires --instance-id for capture-tool binding")
        value = json.loads(args.context_file.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("context file must contain one JSON object")
        _validate_frozen_context(value)
        instance = BridgeClient().select_instance(args.instance_id)
        if not instance.capture_loaded:
            raise ValueError("the selected qrenderdoc instance has no loaded capture")
        capture = value.get("capture", {})
        capture_path = str(capture.get("path", "")) if isinstance(capture, dict) else ""
        return value, instance.id, capture_path or instance.capture_path
    if args.live_context:
        client = BridgeClient()
        if args.instance_id:
            instance = client.select_instance(args.instance_id)
            if not instance.capture_loaded:
                raise ValueError("the selected qrenderdoc instance has no loaded capture")
        else:
            loaded = [item for item in client.instances() if item.capture_loaded]
            if len(loaded) != 1:
                raise ValueError(
                    "--live-context requires exactly one loaded qrenderdoc instance or an "
                    "explicit --instance-id"
                )
            instance = loaded[0]
        value = client.call("get_current_renderdoc_context", instance_id=instance.id)
        if not isinstance(value, dict):
            raise ValueError("qrenderdoc returned an invalid context object")
        return value, instance.id, instance.capture_path
    if args.instance_id:
        raise ValueError("--instance-id requires --context-file or --live-context")
    return {}, "", ""


def _read_prompt(args: argparse.Namespace) -> str:
    prompt = args.prompt
    if args.prompt_stdin:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        raw = stream.read(NATIVE_PROMPT_WIRE_MAX_BYTES + 1)
        encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        if len(encoded) > NATIVE_PROMPT_WIRE_MAX_BYTES:
            raise ValueError("stdin prompt exceeds the 16 MiB safety limit")
        try:
            prompt = encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("stdin prompt must be valid UTF-8") from exc
    if not prompt or not prompt.strip():
        raise ValueError("prompt cannot be empty")
    return prompt


async def _wait_for_run_cancellation(path: Path) -> None:
    while True:
        try:
            with path.open("rb") as signal:
                payload = signal.read(65)
        except (FileNotFoundError, OSError):
            await asyncio.sleep(0.1)
            continue
        if len(payload) <= 64 and payload.strip().lower() == b"cancel":
            return
        await asyncio.sleep(0.1)


async def _run_once(args: argparse.Namespace, store: ProfileStore) -> int:
    owner = None
    try:
        owner = capture_gui_owner(args)
    except OSError as error:
        # A failed access/query must not be interpreted as GUI process death.
        configure_logging().warning("GUI owner monitor unavailable: %s", error)
    with run_lifetime(owner):
        return await _run_once_owned(args, store)


async def _run_once_owned(args: argparse.Namespace, store: ProfileStore) -> int:
    log = configure_logging()
    profile = _selected_profile(store, args.profile)
    profile.validate()
    model = profile.resolve_model(args.model)
    context, instance_id, capture_path = _read_context(args)
    prompt = _read_prompt(args)
    envelope_conversation_id = _validated_conversation_id(
        parse_conversation_prompt(prompt).conversation_id
    )
    explicit_conversation_id = _validated_conversation_id(
        getattr(args, "conversation_id", "")
    )
    if (
        explicit_conversation_id
        and envelope_conversation_id
        and explicit_conversation_id != envelope_conversation_id
    ):
        raise ValueError(
            "conversation identity conflicts with the typed conversation envelope"
        )
    conversation_id = explicit_conversation_id or envelope_conversation_id
    intent: dict[str, Any] | None = None
    if args.intent_kind:
        if not context or not instance_id:
            raise ValueError("--intent-kind requires frozen context and --instance-id")
        intent = {"kind": args.intent_kind, "arguments": {}}

    _sync_bundled_unpackers_quietly()
    tools_config = _tools_config()
    catalog = _catalog(tools_config)
    for diagnostic in catalog.diagnostics:
        log.warning("catalog: %s", diagnostic)
    for diagnostic in tools_config.diagnostics:
        log.warning("tool configuration: %s", diagnostic)
    request = RunRequest(
        prompt=prompt,
        profile=profile,
        task_id=args.task_id,
        model=model,
        context=context,
        instance_id=instance_id,
        capture_path=capture_path,
        conversation_id=conversation_id,
        intent=intent,
        skill_name=args.skill,
        mcp_executable=args.mcp_host,
    )
    config = load_engine_config(args.engine_config)
    # Both engines see the same Skill roots and the same user MCP servers.
    config.skill_roots = tools_config.skill_root_list(default_skill_root())
    config.disabled_skills = tuple(sorted(set(tools_config.disabled_skills + tools_config.desktop_disabled_skill_ids)))
    config.inherit_desktop_capabilities = tools_config.inherit_desktop_capabilities
    config.native_shell_enabled = tools_config.native_shell_enabled
    config.declared_mcp_ids = tuple(server.id for server in tools_config.mcp_servers)
    config.disabled_skill_paths = tools_config.runtime_disabled_skill_paths
    requested = {str(item).strip().lower() for item in getattr(args, "enable_mcp", []) if str(item).strip()}
    selected = []
    for server in tools_config.mcp_servers:
        if not (server.enabled or server.id in requested):
            continue
        available, reason = mcp_server_runtime_status(
            server, respect_enabled=server.id not in requested
        )
        if not available:
            log.warning("MCP server %s is hidden for this task: %s", server.id, reason)
            continue
        selected.append(server)
    missing = requested - {server.id for server in tools_config.mcp_servers}
    for server_id in sorted(missing):
        log.warning("requested MCP server is not configured: %s", server_id)
    attached = list(servers_for_runtime(selected))
    known = {str(server.get("id", "")).lower() for server in attached}
    # Tools registered in the unified list are attached the same way; a legacy entry
    # with the same id wins so a half-finished migration cannot double-start a server.
    registered_tools, tool_diagnostics = load_tool_providers()
    for diagnostic in tool_diagnostics:
        log.warning("tool configuration: %s", diagnostic)
    for tool in registered_tools:
        available, reason = tool_runtime_status(tool)
        if tool.get("enabled", True) and not available:
            log.warning(
                "registered tool %s is hidden for this task: %s",
                tool.get("id"), reason,
            )
    for server in runtime_servers(registered_tools):
        if str(server.get("id", "")).lower() in known:
            continue
        if requested and server["id"] not in requested and not server.get("enabled", True):
            continue
        attached.append(server)
    if config.inherit_desktop_capabilities:
        from .desktop_capabilities import discover_desktop_capabilities
        desktop = discover_desktop_capabilities()
        config.desktop_capability_summary = desktop.summary()
        from .desktop_capabilities import native_execution_policy
        config.native_shell_enabled, execution_reason = native_execution_policy(
            config.native_shell_enabled, desktop.config, profile.options)
        config.desktop_capability_summary["nativeShell"] = {"enabled": config.native_shell_enabled, "reason": execution_reason}
        for diagnostic in desktop.diagnostics:
            log.warning("desktop integration: %s", diagnostic)
        # Codex loads these directly so provider-side approval rules remain in
        # force. API/ACP use the same optional services through Bread's bridge.
        if profile.preset != "codex" or profile.provider.kind != "agent":
            declared = {server.id for server in tools_config.mcp_servers}
            declared.update(str(server.get("id", "")).lower() for server in attached)
            attached.extend(server for server in desktop.servers if server["id"] not in declared)
    config.external_mcp_servers = tuple(attached)
    if not config.inherit_desktop_capabilities:
        from .desktop_capabilities import native_execution_policy
        config.native_shell_enabled, _ = native_execution_policy(config.native_shell_enabled, {}, profile.options)
    from .portable_capabilities import server_definition, provider_servers
    portable = server_definition(execution_enabled=config.native_shell_enabled)
    if portable and portable["id"] not in {s.get("id") for s in attached}:
        config.external_mcp_servers += (portable,)
    configured = {str(item.get("id")) for item in config.external_mcp_servers}
    excluded = {server.id for server in tools_config.mcp_servers}
    provider_policies = {}
    if config.inherit_desktop_capabilities:
        excluded.update(item["name"] for item in desktop.server_status if item.get("status") == "disabled")
        provider_policies = {item["id"]: item["approvalRules"] for item in desktop.servers if "approvalRules" in item}
    config.external_mcp_servers += tuple(item for item in provider_servers(excluded_ids=excluded, policies=provider_policies)
                                         if item["id"] not in configured)
    engine: Any
    if profile.provider.kind == "model":
        engine = DeepAgentsEngine(catalog=catalog, config=config)
    elif profile.provider.kind == "agent":
        engine = NativeAgentEngine(catalog=catalog, config=config)
    else:  # pragma: no cover - profile validation constrains this value
        raise ConfigurationError(f"unsupported profile kind '{profile.provider.kind}'")

    cancel_path = args.cancel_file
    try:
        result = await run_until_cancelled(
            engine.run(request),
            cancel_waiter=_wait_for_run_cancellation(cancel_path) if cancel_path is not None else None,
            log=log,
        )
    finally:
        if cancel_path is not None:
            with contextlib.suppress(OSError):
                cancel_path.unlink(missing_ok=True)
    if args.json:
        _print_machine_json(result.payload())
    else:
        print(result.text)
    return 1 if result.terminal_status == "failed" else 0


_SAFE_TERMINAL_FAILURES = {
    "invalid-request": "The request or local AI configuration is invalid.",
    "local-io-failure": "The local AI Host could not access a required resource.",
    "ai-operation-failed": "The AI operation could not be completed.",
    "internal-host-failure": "The local AI Host failed unexpectedly.",
}


def _print_utf8_error(value: str) -> None:
    encoded = str(value).encode("utf-8", errors="backslashreplace")
    if len(encoded) + 1 > _MAX_STDERR_BYTES:
        suffix = b"...\n"
        encoded = (
            encoded[: _MAX_STDERR_BYTES - len(suffix)]
            .decode("utf-8", errors="ignore")
            .encode("utf-8")
            + suffix
        )
    else:
        encoded += b"\n"
    binary = getattr(sys.stderr, "buffer", None)
    if binary is not None:
        binary.write(encoded)
        binary.flush()
    else:
        sys.stderr.write(encoded.decode("utf-8"))
        sys.stderr.flush()


def _terminal_error_category(exc: BaseException) -> str:
    if isinstance(exc, (KeyError, ValueError, json.JSONDecodeError, ConfigurationError)):
        return "invalid-request"
    if isinstance(exc, OSError):
        return "local-io-failure"
    if isinstance(exc, ReplayLabAIError):
        return "ai-operation-failed"
    return "internal-host-failure"


def _print_safe_terminal_failure(exc: BaseException) -> None:
    category = _terminal_error_category(exc)
    _print_utf8_error(
        f"replaylab-ai: error[{category}]: {_SAFE_TERMINAL_FAILURES[category]} "
        "Check RenderDoc's diagnostic log for technical details."
    )


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(AttributeError, OSError, ValueError):
                reconfigure(encoding="utf-8", errors="strict")


def _consume_native_start_gate(args: argparse.Namespace) -> None:
    setting = os.environ.pop(_NATIVE_START_GATE_ENV, None)
    if setting is None or bool(getattr(args, "start_gated", False)):
        return
    if setting != "1":
        raise ValueError("native start gate setting is invalid")
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    record = stream.readline(len(_NATIVE_START_GATE_RECORD) + 1)
    encoded = record.encode("utf-8") if isinstance(record, str) else bytes(record)
    if encoded != _NATIVE_START_GATE_RECORD:
        raise ValueError("native start gate closed before the exact start record")


def _doctor_command(args: argparse.Namespace, store: ProfileStore) -> int:
    from .doctor import DoctorCheck
    streaming = bool(getattr(args, "jsonl", False))
    next_index = 0
    def payload(check):
        return {"name": check.name, "status": check.status, "detail": check.detail,
                "messageId": check.message_id, "messageArgs": list(check.message_args)}
    def emit(check, *, pending=False):
        nonlocal next_index
        if streaming:
            print(json.dumps({"type": "check", "index": next_index, "check": payload(check)},
                             ensure_ascii=False, separators=(",", ":")), flush=True)
        if not pending:
            next_index += 1
    profile = _selected_profile(store, args.profile)
    tools = _tools_config(getattr(args, "tools_config", None))
    catalog = _catalog(tools)
    checks = list(run_doctor(profile, catalog, mcp_executable=args.mcp_host, progress_callback=emit))
    if getattr(args, "execution_test", False):
        from .connection_readiness import run_execution_check, check_portable_tools, check_host_tools
        from .portable_capabilities import manifest as optional_capabilities
        local_checks = [("bread-host-execution", lambda: check_host_tools(args.mcp_host))]
        if optional_capabilities() and optional_capabilities().get("features", {}).get("portableTools", True):
            local_checks.insert(0, ("portable-tool-execution", check_portable_tools))
        for name, operation in local_checks:
            emit(DoctorCheck(name, "running", "Checking connection", "connection.check-running"), pending=True)
            check = asyncio.run(operation())
            checks.append(check)
            emit(check)
        # Check independent local tools even when provider authentication is
        # missing. A known provider configuration error must not spend quota.
        if not any(check.status == "error" for check in checks
                   if check.name not in {"portable-tool-execution", "bread-host-execution", "model-context", "model-context-enabled"}):
            emit(DoctorCheck("model-tool-execution", "running", "Checking connection", "connection.check-running"), pending=True)
            for check in asyncio.run(run_execution_check(profile, timeout=args.timeout, tools_config=tools)):
                checks.append(check)
                emit(check)
    if streaming:
        print(json.dumps({"type": "complete", "checks": [payload(check) for check in checks]},
                         ensure_ascii=False, separators=(",", ":")), flush=True)
    elif args.json:
        _print_machine_json(
            [
                {"name": check.name, "status": check.status, "detail": check.detail,
                 "messageId": check.message_id, "messageArgs": list(check.message_args)}
                for check in checks
            ]
        )
    else:
        for check in checks:
            print(f"[{check.status.upper():7}] {check.name}: {check.detail}")
    return 1 if any(check.status == "error" for check in checks) else 0


# The loader's own rule (skills._NAME_RE): an id this accepts is one the loader
# admits, so "new Skill" cannot create a folder the next task quarantines.
_SKILL_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _pairs(entries: list[str], label: str) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for entry in entries:
        name, separator, value = str(entry).partition("=")
        if not separator or not name.strip():
            raise ConfigurationError(f"{label} must be given as NAME=value")
        resolved[name.strip()] = value.strip()
    return resolved


def _tool_from_arguments(args: Any) -> dict[str, Any]:
    """Build one tool registration from the command line Settings uses.

    Only the connection is taken here. The interface is discovered by the Host against
    real assets and written back, so there is nothing else for a person to fill in.
    """

    kind = str(getattr(args, "kind", "cli"))
    connection: dict[str, Any] = {"kind": kind}
    if kind == "mcp-http":
        connection["url"] = str(getattr(args, "url", "") or "").strip()
        connection["bearerEnv"] = str(getattr(args, "bearer_env", "") or "").strip()
        connection["headers"] = _pairs(list(getattr(args, "header", []) or []), "--header")
        connection["headerEnv"] = _pairs(list(getattr(args, "header_env", []) or []),
                                         "--header-env")
        display = str(getattr(args, "name", "") or "") or connection["url"]
    else:
        connection["command"] = str(getattr(args, "command", "") or "").strip()
        connection["args"] = [str(item) for item in (getattr(args, "arg", []) or [])]
        connection["env"] = _pairs(list(getattr(args, "env", []) or []), "--env")
        display = str(getattr(args, "name", "") or "") or Path(connection["command"]).stem
    roles = [str(role) for role in (getattr(args, "role", []) or [])]
    if not roles:
        # A command-line tool is registered because it reads assets; an MCP server is
        # registered because its tools are wanted during a task. Both can be widened.
        roles = ["asset-reader"] if kind == "cli" else ["general"]
    identifier = str(getattr(args, "id", "") or "") or tool_slug(display)
    definition = {
        "id": identifier,
        "title": display or identifier,
        "description": str(getattr(args, "description", "") or ""),
        "enabled": True,
        "roles": roles,
        "connection": connection,
    }
    try:
        return dict(definition)
    except ToolConfigError as error:  # pragma: no cover - defensive
        raise ConfigurationError(str(error)) from error


def _skill_row(config: AgentToolsConfig, skill_id: str) -> dict[str, Any]:
    """The catalog row for one Skill, or a clear error naming what was searched."""

    identifier = str(skill_id or "").strip()
    catalog = CatalogView.load(skill_roots=config.skill_root_list(default_skill_root()))
    for row in describe_skills(catalog, config, default_skill_root()):
        if row["id"] == identifier:
            return row
    raise ConfigurationError(f"unknown Skill id: {identifier or '(empty)'}")


def _skill_show(config: AgentToolsConfig, skill_id: str) -> dict[str, Any]:
    """Read one SKILL.md so Settings can show and edit exactly what the AI loads."""

    row = _skill_row(config, skill_id)
    document = Path(row["path"] or "")
    try:
        content = document.read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        raise ConfigurationError(f"cannot read {document}: {error}") from error
    overlay = user_skill_overlay_root(config.path)
    return {
        "schema": "replaylab-agent-skill-document/v1",
        "id": row["id"],
        "title": row["title"],
        "source": row["source"],
        "path": str(document),
        "content": content,
        # A bundled Skill is replaced by the next install, so an edit is written to
        # the user overlay instead and that copy wins from then on.
        "writePath": str(document) if row["source"] == "user" else str(
            overlay / row["id"] / "SKILL.md"),
        "copyOnWrite": row["source"] != "user",
        "overlayRoot": str(overlay),
    }


def _write_skill_document(config: AgentToolsConfig, row: dict[str, Any], content: str,
                          config_path: Path | None) -> dict[str, Any]:
    """Write SKILL.md, copying a bundled Skill into the user overlay first."""

    if not content.strip():
        raise ConfigurationError("a Skill document cannot be empty")
    if len(content.encode("utf-8")) > 512 * 1024:
        raise ConfigurationError("a Skill document must be under 512 KiB")
    overlay = user_skill_overlay_root(config.path or config_path)
    copy_on_write = row["source"] != "user"
    target = Path(row["path"]) if not copy_on_write else overlay / row["id"] / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, target)
    registered = False
    if copy_on_write and overlay not in config.skill_roots:
        config.add_skill_root(overlay)
        config.save(config_path)
        registered = True
    return {
        "id": row["id"],
        "path": str(target),
        "copiedToOverlay": copy_on_write,
        "overlayRoot": str(overlay),
        "registeredOverlayRoot": registered,
        "bytes": len(content.encode("utf-8")),
    }


def _skill_write(config: AgentToolsConfig, args: Any, config_path: Path | None) -> dict[str, Any]:
    if getattr(args, "content_stdin", False):
        content = sys.stdin.read()
    elif getattr(args, "content_file", None):
        try:
            content = Path(args.content_file).read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            raise ConfigurationError(f"cannot read {args.content_file}: {error}") from error
    else:
        raise ConfigurationError("pass --content-stdin or --content-file")
    return _write_skill_document(config, _skill_row(config, args.skill_id), content, config_path)


# Quoted: a purely numeric id ("2") is an integer to YAML when bare, and the
# loader then quarantines the Skill as "name must be a string".
_SKILL_TEMPLATE = """---
name: "{identifier}"
title: "{title}"
description: >-
  One sentence saying when Bread AI should load this Skill. The catalog line
  is what the model reads before choosing, so name the situation, not the tool.
version: 1
---

# {title}

## When this applies

Describe the request this Skill answers.

## Procedure

1. State the first step in terms of evidence to gather.
2. Name the Host tools to call and what their result must show.
3. Say what a finished answer contains.

## Do not

* Do not guess where the capture can be read.
"""


def _skill_new(config: AgentToolsConfig, args: Any, config_path: Path | None) -> dict[str, Any]:
    """Create an empty Skill in the user overlay, ready to edit in Settings."""

    identifier = str(args.skill_id or "").strip().lower()
    if not _SKILL_ID_RE.fullmatch(identifier) or len(identifier) > 96:
        raise ConfigurationError(
            "a Skill id must be lower-case words of letters and digits joined by "
            "single hyphens, for example 'ue-hair-passes'")
    overlay = user_skill_overlay_root(config.path or config_path)
    target = overlay / identifier / "SKILL.md"
    if target.exists():
        raise ConfigurationError(f"a Skill already exists at {target}")
    title = str(getattr(args, "title", "") or "").strip() or identifier.replace("-", " ").title()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_SKILL_TEMPLATE.format(identifier=identifier, title=title), encoding="utf-8")
    registered = False
    if overlay not in config.skill_roots:
        config.add_skill_root(overlay)
        config.save(config_path)
        registered = True
    return {
        "id": identifier,
        "path": str(target),
        "overlayRoot": str(overlay),
        "registeredOverlayRoot": registered,
    }


def _skill_remove(config: AgentToolsConfig, skill_id: str,
                  config_path: Path | None) -> dict[str, Any]:
    """Delete one Skill the user made, and only one.

    Removing a Skill folder used to be the only way out of Settings, which takes
    every Skill in that folder with it. This deletes exactly the Skill named, and
    refuses anything shipped with RenderDoc — the installer owns those.
    """

    row = _skill_row(config, skill_id)
    if row["source"] != "user":
        raise ConfigurationError(
            f"{row['id']} is installed with RenderDoc; disable it instead of deleting it")
    document = Path(row["path"] or "")
    folder = document.parent
    root = Path(row.get("root") or folder.parent)
    # Refuse anything that is not <registered root>/<skill>/SKILL.md: a Skill is a
    # folder we created, never a directory the user pointed us at.
    try:
        relative = folder.relative_to(root)
    except (ValueError, OSError) as error:
        raise ConfigurationError(f"{document} is not inside a registered Skill folder") from error
    if len(relative.parts) != 1 or document.name != "SKILL.md":
        raise ConfigurationError(f"refusing to delete an unexpected layout at {folder}")
    removed: list[str] = []
    try:
        for entry in sorted(folder.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
                removed.append(str(entry))
            elif entry.is_dir():
                entry.rmdir()
        folder.rmdir()
    except OSError as error:
        raise ConfigurationError(f"could not delete {folder}: {error}") from error
    # An overlay root with nothing left in it is clutter in the folder list.
    unregistered = False
    overlay = user_skill_overlay_root(config.path or config_path)
    if root == overlay and not any(root.glob("*/SKILL.md")):
        if config.remove_skill_root(root):
            config.save(config_path)
            unregistered = True
    return {
        "id": row["id"],
        "deleted": str(folder),
        "files": removed,
        "unregisteredRoot": str(root) if unregistered else "",
    }


def _tool_test(tool_id: str, timeout: float) -> dict[str, Any]:
    """Start one registered tool and report whether it actually answers.

    A configured tool is not a working tool, and the difference is worth one button.
    An MCP server is initialised and its operations counted; a command line is checked
    for existence and asked for its help. Nothing is written either way.
    """

    identifier = str(tool_id or "").strip().lower()
    registered, _ = load_tool_providers()
    tool = next((item for item in registered if item["id"] == identifier), None)
    if tool is None:
        raise ConfigurationError(f"unknown tool id: {identifier or '(empty)'}")
    connection = tool["connection"]
    kind = str(connection.get("kind", ""))
    bounded = max(1.0, min(float(timeout), 120.0))
    started = time.monotonic()
    if kind == "mcp-stdio":
        from .acp_session import _diagnose_local_mcp_route

        report = asyncio.run(_diagnose_local_mcp_route(
            str(connection.get("command", "")), connection.get("args", ()),
            connection.get("env", {}), cwd=Path.cwd(), timeout=bounded))
        return {"schema": "replaylab-agent-tool-test/v1", "tool": identifier,
                "connection": kind, "ok": bool(report.get("ok")),
                "operations": int(report.get("tools") or 0),
                "seconds": round(float(report.get("seconds") or 0.0), 2),
                "error": str(report.get("error") or "")}
    if kind == "mcp-http":
        report = asyncio.run(_diagnose_http_mcp_route(connection, timeout=bounded))
        return {"schema": "replaylab-agent-tool-test/v1", "tool": identifier,
                "connection": kind, "ok": bool(report.get("ok")),
                "operations": int(report.get("tools") or 0),
                "seconds": round(float(report.get("seconds") or 0.0), 2),
                "error": str(report.get("error") or "")}
    command = str(connection.get("command", ""))
    resolved = shutil.which(command) or command
    if not Path(resolved).is_file():
        return {"schema": "replaylab-agent-tool-test/v1", "tool": identifier, "connection": kind,
                "ok": False, "operations": 0, "seconds": 0.0,
                "error": f"no such program: {command}"}
    try:
        completed = subprocess.run([resolved, "--help"], capture_output=True,
                                   timeout=min(bounded, 20.0), check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = (completed.stdout + b"\n" + completed.stderr).decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError) as error:
        return {"schema": "replaylab-agent-tool-test/v1", "tool": identifier, "connection": kind,
                "ok": False, "operations": 0,
                "seconds": round(time.monotonic() - started, 2), "error": str(error)[:400]}
    return {"schema": "replaylab-agent-tool-test/v1", "tool": identifier, "connection": kind,
            "ok": bool(output), "operations": 0,
            "seconds": round(time.monotonic() - started, 2),
            "helpText": output[:2000],
            "error": "" if output else "the program produced no output"}


async def _diagnose_http_mcp_route(connection: Any, *, timeout: float) -> dict[str, Any]:
    """Initialise a streamable-HTTP MCP server once and count its operations."""

    import os
    import time as _time

    started = _time.monotonic()
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as error:
        return {"ok": False, "tools": 0, "seconds": 0.0,
                "error": f"this MCP runtime has no streamable HTTP client: {error}"}
    headers = {str(k): str(v) for k, v in (connection.get("headers") or {}).items()}
    for name, variable in (connection.get("headerEnv") or {}).items():
        value = os.environ.get(str(variable), "")
        if value:
            headers[str(name)] = value
    bearer = os.environ.get(str(connection.get("bearerEnv") or ""), "")
    if bearer:
        headers["Authorization"] = "Bearer %s" % bearer
    try:
        async with streamablehttp_client(str(connection.get("url", "")),
                                         headers=headers or None) as opened:
            async with ClientSession(*opened[:2]) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout)
                listing = await asyncio.wait_for(session.list_tools(), timeout=timeout)
    except BaseException as error:  # noqa: BLE001 - a failure to reach it is the answer
        return {"ok": False, "tools": 0, "seconds": _time.monotonic() - started,
                "error": _flatten_tool_error(error)}
    return {"ok": True, "tools": len(list(getattr(listing, "tools", ()) or ())),
            "seconds": _time.monotonic() - started, "error": ""}


def _flatten_tool_error(error: BaseException) -> str:
    nested = getattr(error, "exceptions", None)
    if nested:
        return "; ".join(_flatten_tool_error(item) for item in nested)
    text = str(error).strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def _mcp_test(config: AgentToolsConfig, server_id: str, timeout: float) -> dict[str, Any]:
    """Start one configured MCP server and report initialize + tools/list.

    A server that is configured is not a server that runs: this is the button
    that says so before a task depends on it.
    """

    identifier = str(server_id or "").strip().lower()
    server = next((item for item in config.mcp_servers if item.id == identifier), None)
    if server is None:
        raise ConfigurationError(f"unknown MCP server id: {identifier or '(empty)'}")
    if server.transport != "stdio" or not server.command:
        return {
            "server": identifier,
            "ok": False,
            "tools": 0,
            "error": "only stdio servers with a command can be tested from here",
        }
    from .acp_session import _diagnose_local_mcp_route

    report = asyncio.run(
        _diagnose_local_mcp_route(
            server.command,
            server.args,
            server.env,
            cwd=Path.cwd(),
            timeout=max(1.0, min(float(timeout), 120.0)),
        )
    )
    return {
        "schema": "replaylab-agent-mcp-test/v1",
        "server": identifier,
        "title": server.title or identifier,
        "ok": bool(report.get("ok")),
        "tools": int(report.get("tools") or 0),
        "seconds": round(float(report.get("seconds") or 0.0), 2),
        "error": str(report.get("error") or ""),
    }


def _sync_bundled_unpackers_quietly() -> dict[str, Any] | None:
    """Register the unpackers shipped with this installation; never fail a command for it."""

    try:
        from .builtin_unpackers import sync_bundled_unpackers

        return sync_bundled_unpackers()
    except Exception as error:  # noqa: BLE001 - bundled tools are a convenience
        import logging

        logging.getLogger("replaylab_agent").warning("bundled unpackers: %s", error)
        return None


def _unpacker_command(args: Any) -> int:
    """Run a reader embedded in this executable (``unpacker unitypy describe <file>``).

    The UnityPy reader ships as source inside the AgentHost so a deployed copy
    needs no separate Python: the bundled recipe points at this executable.
    """

    if args.kind == "unitypy":
        from .unitypy_reader_script import SCRIPT

        arguments = [item for item in list(args.arguments or []) if item != "--"]
        saved = sys.argv
        try:
            sys.argv = ["unitypy_reader.py", *arguments]
            code = compile(SCRIPT, "unitypy_reader.py", "exec")
            namespace: dict[str, Any] = {"__name__": "__main__"}
            try:
                exec(code, namespace)  # noqa: S102 - our own bundled reader source
            except SystemExit as exc:
                return int(exc.code or 0) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            return 0
        finally:
            sys.argv = saved
    raise AssertionError(args.kind)


def _tools_command(args: Any) -> int:
    """Read and edit the Skill/MCP menu that every AI connection is offered."""

    path = getattr(args, "tools_config", None)
    config = _tools_config(path)
    command = args.tools_command
    changed = False
    result: dict[str, Any]
    if command in {"tool-list", "tool-set", "tool-detect", "list"}:
        _sync_bundled_unpackers_quietly()
    if command == "list":
        catalog = CatalogView.load(skill_roots=config.skill_root_list(default_skill_root()))
        result = {
            "schema": "replaylab-agent-tools-view/v1",
            "path": str(config.path or ""),
            "skillRoots": [str(root) for root in config.skill_roots],
            "bundledSkillRoot": str(default_skill_root()),
            "skills": describe_skills(catalog, config, default_skill_root()),
            "mcpServers": [server.to_dict() for server in config.mcp_servers],
            "mcpPresets": [dict(item) for item in MCP_PRESETS],
            "diagnostics": list(config.diagnostics) + list(catalog.diagnostics),
            "inheritDesktopCapabilities": config.inherit_desktop_capabilities,
            "nativeShellEnabled": config.native_shell_enabled,
        }
        if config.inherit_desktop_capabilities:
            from .desktop_capabilities import discover_desktop_capabilities
            result["desktopCapabilities"] = discover_desktop_capabilities().summary()
    elif command in {"skill-enable", "skill-disable"}:
        config.set_skill_enabled(args.skill_id, command == "skill-enable")
        changed = True
        result = {"skill": args.skill_id, "enabled": command == "skill-enable"}
    elif command == "add-skill-root":
        config.add_skill_root(args.path)
        changed = True
        result = {"skillRoots": [str(root) for root in config.skill_roots]}
    elif command == "remove-skill-root":
        removed = config.remove_skill_root(args.path)
        changed = removed
        result = {"removed": removed, "skillRoots": [str(root) for root in config.skill_roots]}
    elif command == "mcp-set":
        raw = sys.stdin.read() if args.json_stdin else str(args.server_json or "")
        try:
            payload = json.loads(raw or "{}")
        except ValueError as error:
            raise ConfigurationError("MCP server definition must be JSON") from error
        if not isinstance(payload, dict):
            raise ConfigurationError("MCP server definition must be a JSON object")
        server = config.upsert_mcp_server(payload)
        changed = True
        result = {"server": server.to_dict()}
    elif command == "mcp-remove":
        removed = config.remove_mcp_server(args.server_id)
        changed = removed
        result = {"removed": removed, "server": args.server_id}
    elif command in {"mcp-enable", "mcp-disable"}:
        found = config.set_mcp_enabled(args.server_id, command == "mcp-enable")
        if not found:
            raise ConfigurationError(f"unknown MCP server id: {args.server_id}")
        changed = True
        result = {"server": args.server_id, "enabled": command == "mcp-enable"}
    elif command == "mcp-test":
        result = _mcp_test(config, args.server_id, float(getattr(args, "timeout", 25.0)))
    elif command == "skill-show":
        result = _skill_show(config, args.skill_id)
    elif command == "skill-write":
        result = _skill_write(config, args, path)
    elif command == "skill-new":
        result = _skill_new(config, args, path)
    elif command == "skill-remove":
        result = _skill_remove(config, args.skill_id, path)
    elif command == "reader-list":
        readers, diagnostics = load_readers()
        result = {
            "schema": "replaylab-agent-reader-view/v1",
            "path": str(default_readers_path()),
            "readers": readers,
            "builtins": [dict(item) for item in BUILTIN_READERS],
            "diagnostics": diagnostics,
            "contract": reader_contract(),
        }
    elif command == "reader-discover":
        found = discover_installed_readers()
        registered = {str(item.get("command", "")).lower() for item in load_readers()[0]}
        for item in found:
            item["alreadyRegistered"] = item["command"].lower() in registered
        result = {"schema": "replaylab-agent-reader-discovery/v1", "candidates": found}
    elif command == "reader-set":
        raw = sys.stdin.read() if args.json_stdin else str(args.reader_json or "")
        try:
            payload = json.loads(raw or "{}")
        except ValueError as error:
            raise ConfigurationError("asset reader definition must be JSON") from error
        if not isinstance(payload, dict):
            raise ConfigurationError("asset reader definition must be a JSON object")
        try:
            reader = upsert_reader(payload)
        except AssetReaderError as error:
            raise ConfigurationError(str(error)) from error
        result = {"reader": reader, "path": str(default_readers_path())}
    elif command == "tool-list":
        registered, diagnostics = load_tool_providers()
        result = {
            "schema": "replaylab-agent-tool-view/v1",
            "path": str(tool_providers_path()),
            "tools": registered,
            # Servers from agent-tools.json (what the AI registers through
            # update_bread_configuration) ride along so the Settings list that
            # is rebuilt from this one answer shows them with the same switch.
            "mcpServers": [server.to_dict() for server in config.mcp_servers],
            "diagnostics": diagnostics,
            "builtinReaders": [dict(item) for item in BUILTIN_READERS],
            # Every unpacker Bread AI ships a recipe for, configured or not: the
            # Settings list shows them all, and the dialog opens on the kind.
            "builtinUnpackers": builtin_unpacker_rows(registered),
            "presets": {"mcp": [dict(item) for item in MCP_PRESETS],
                        "commandLine": [dict(item) for item in READER_PRESETS]},
            "connectionKinds": list(CONNECTION_KINDS),
            "roles": list(TOOL_ROLES),
        }
    elif command == "tool-add":
        try:
            result = {"tool": upsert_tool_provider(_tool_from_arguments(args)),
                      "path": str(tool_providers_path())}
        except ToolConfigError as error:
            raise ConfigurationError(str(error)) from error
    elif command == "tool-set":
        raw = sys.stdin.read() if args.json_stdin else str(args.tool_json or "")
        try:
            payload = json.loads(raw or "{}")
        except ValueError as error:
            raise ConfigurationError("tool definition must be JSON") from error
        if not isinstance(payload, dict):
            raise ConfigurationError("tool definition must be a JSON object")
        builtin_kind = str(payload.get("builtinKind") or "").strip().lower()
        if builtin_kind:
            # A built-in unpacker: the dialog sends the program and its few options;
            # the whole interface (operations, formats, results) comes from the
            # shipped recipe, never from a probe or a hand-written form.
            options = payload.get("builtinOptions") or {}
            try:
                definition = builtin_unpacker_definition(
                    builtin_kind, str(payload.get("program") or
                                      payload.get("connection", {}).get("command") or ""),
                    options if isinstance(options, dict) else {},
                    enabled=bool(payload.get("enabled", True)),
                    offer_to_agent=bool(payload.get("offerToAgent", True)))
                tool = upsert_tool_provider(definition)
            except (ValueError, ToolConfigError) as error:
                raise ConfigurationError(str(error)) from error
            result = {"tool": tool, "path": str(tool_providers_path())}
            if builtin_kind == "uex" and definition["builtinOptions"].get("registerMcp"):
                server = config.upsert_mcp_server(uex_mcp_server_definition(
                    definition["connection"]["command"],
                    definition["builtinOptions"]["configPath"]))
                changed = True
                result["mcpServer"] = server.to_dict()
        else:
            try:
                result = {"tool": upsert_tool_provider(payload),
                          "path": str(tool_providers_path())}
            except ToolConfigError as error:
                raise ConfigurationError(str(error)) from error
    elif command == "tool-detect":
        spec = builtin_unpacker(args.kind)
        if spec is None:
            raise ConfigurationError("unknown built-in unpacker: %s" % args.kind)
        names = {name.lower() for name in spec["programNames"]}
        candidates = [item for item in discover_installed_readers()
                      if Path(str(item.get("command", ""))).stem.lower() in names]
        if spec["kind"] == "unitypy":
            # Any interpreter on PATH; the script is ours. Prefer one that already
            # has UnityPy importable, and say which ones do not.
            import shutil
            import subprocess
            for name in ("python", "python3", "py"):
                found = shutil.which(name)
                if not found or any(item.get("command") == found for item in candidates):
                    continue
                try:
                    probe = subprocess.run([found, "-c", "import UnityPy;print(UnityPy.__version__)"],
                                           capture_output=True, timeout=20, check=False,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    version = probe.stdout.decode("utf-8", "replace").strip() \
                        if probe.returncode == 0 else ""
                except (OSError, subprocess.SubprocessError):
                    version = ""
                candidates.append({"command": found, "id": "unitypy", "title": name,
                                   "engines": ["unity"], "unityPy": version,
                                   "description": ("UnityPy %s" % version) if version
                                   else "UnityPy is not installed: pip install UnityPy"})
        result = {"schema": "replaylab-agent-tool-detect/v1", "kind": spec["kind"],
                  "candidates": candidates[:8]}
    elif command == "tool-remove":
        result = {"removed": remove_tool_provider(args.tool_id), "tool": args.tool_id}
    elif command in {"tool-enable", "tool-disable"}:
        found = set_tool_provider_enabled(args.tool_id, command == "tool-enable")
        if not found:
            raise ConfigurationError(f"unknown tool id: {args.tool_id}")
        result = {"tool": args.tool_id, "enabled": command == "tool-enable"}
    elif command == "tool-migrate":
        result = migrate_tool_providers()
    elif command == "tool-test":
        result = _tool_test(args.tool_id, float(getattr(args, "timeout", 30.0)))
    elif command == "reader-add":
        try:
            reader = upsert_reader(
                registration_from_command(args.command, args.name, args.description))
        except AssetReaderError as error:
            raise ConfigurationError(str(error)) from error
        result = {"reader": reader, "path": str(default_readers_path())}
    elif command == "reader-remove":
        result = {"removed": remove_reader(args.reader_id), "reader": args.reader_id}
    elif command in {"reader-enable", "reader-disable"}:
        found = set_reader_enabled(args.reader_id, command == "reader-enable")
        if not found:
            raise ConfigurationError(f"unknown asset reader id: {args.reader_id}")
        result = {"reader": args.reader_id, "enabled": command == "reader-enable"}
    else:  # pragma: no cover - argparse constrains this value
        raise AssertionError(command)
    if changed:
        config.save(path)
    if getattr(args, "json", False):
        _print_machine_json(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _configure_utf8_stdio()
        args = _parser().parse_args(argv)
        _consume_native_start_gate(args)
        if args.command == "profile" and args.profile_command == "presets":
            return _print_presets(args.json)

        if args.command == "unpacker":
            return _unpacker_command(args)
        if args.command == "tools":
            return _tools_command(args)
        if args.command == "conversation-cleanup":
            from .conversation_cleanup import cleanup_conversation, cleanup_pending_conversations
            result = (cleanup_pending_conversations() if args.pending else
                      cleanup_conversation(args.conversation_id, args.task_id))
            _print_machine_json(result)
            return 0 if result["complete"] else 1

        store = _load_store(args.config)
        from .provider_lease import profile_lease
        lease_id = str(getattr(args, "profile", "") or "")
        if args.command == "profile" and args.profile_command in {"subscription", "discover-models", "test-model"}:
            lease_id = str(getattr(args, "id", "") or lease_id or store.active_profile)
        elif args.command in {"run", "doctor"}:
            lease_id = lease_id or store.active_profile
        else:
            lease_id = ""
        lease = profile_lease(store.path, lease_id) if lease_id else contextlib.nullcontext()
        with lease:
            if args.command == "profile":
                if args.profile_command == "credential":
                    return _credential_command(args, store)
                if args.profile_command == "subscription":
                    return asyncio.run(_subscription_command(args, store))
                if args.profile_command == "discover-models":
                    return asyncio.run(_models_command(args, store))
                if args.profile_command == "test-model":
                    return asyncio.run(_test_model_command(args, store))
                from .credential_store import default_credential_store

                return _profile_command(args, store, default_credential_store())
            if args.command == "doctor":
                return _doctor_command(args, store)
            if args.command == "run":
                return asyncio.run(_run_once(args, store))
            raise AssertionError(args.command)
    except SystemExit as exc:
        if type(exc.code) is int:
            return exc.code
        _print_safe_terminal_failure(exc)
        return 2
    except (KeyboardInterrupt, asyncio.CancelledError):
        with contextlib.suppress(Exception):
            _print_utf8_error("replaylab-ai: cancelled")
        return 130
    except Exception as exc:
        with contextlib.suppress(Exception):
            log_exception(configure_logging(), "command failed", exc)
        with contextlib.suppress(Exception):
            _print_safe_terminal_failure(exc)
        return 2
    except BaseException as exc:
        with contextlib.suppress(Exception):
            log_exception(configure_logging(), "command failed", exc)
        with contextlib.suppress(Exception):
            _print_safe_terminal_failure(exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
