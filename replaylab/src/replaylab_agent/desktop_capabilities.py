"""Reuse explicitly enabled local integrations without copying account credentials.

Discovery is read-only. These settings are a runtime overlay, never written back
to the user's Codex configuration or Bread's saved server list. A plugin's name
alone is not proof that its desktop-only service is connected.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass
class DesktopCapabilities:
    home: Path
    skills: list[Path] = field(default_factory=list)
    servers: list[dict[str, Any]] = field(default_factory=list)
    plugins: dict[str, Path] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    server_status: list[dict[str, str]] = field(default_factory=list)
    disabled_skill_paths: list[Path] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        # Never include environment values, headers or authentication settings.
        return {"source": "user-enabled-local-integrations", "skillRoots": len(self.skills),
                "mcpServers": [item["id"] for item in self.servers],
                "plugins": sorted(self.plugins), "diagnostics": self.diagnostics,
                "serverStatus": self.server_status,
                "desktopPrivateServices": "available-only-if-their-configured-server-connects",
                "approvalPolicy": "preserve-provider-and-user-settings"}


def _version_key(path: Path) -> tuple:
    return tuple((1, int(item)) if item.isdigit() else (0, item)
                 for item in re.split(r"(\d+)", path.name))


def _effective_config(config: dict[str, Any]) -> dict[str, Any]:
    selected = config.get("profile")
    profile = (config.get("profiles") or {}).get(selected, {})
    def merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(base)
        for key, value in overlay.items():
            result[key] = merge(result[key], value) if isinstance(result.get(key), Mapping) and isinstance(value, Mapping) else value
        return result
    return merge(config, profile) if isinstance(profile, Mapping) else config


def is_bread_server_config(raw: Mapping[str, Any]) -> bool:
    if Path(str(raw.get("command", ""))).name.casefold() in {"breadmcp.exe", "breadmcp", "replaylabinternal.exe", "replaylabinternal"}:
        return True
    values = " ".join(str(value) for value in (raw.get("command", ""), *(raw.get("args") or ()))).casefold().replace("\\", "/")
    return any(token in values for token in ("replaylab_mcp", "replaylab_agent.mcp", "renderdoc_mcp", "replaylab-host", "replaylab-mcp", "bread-host", "bread-mcp"))


def discover_desktop_capabilities(home: Path | None = None) -> DesktopCapabilities:
    root = (home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")).expanduser().resolve()
    result = DesktopCapabilities(root)
    if (root / "skills").is_dir():
        result.skills.append(root / "skills")
    try:
        result.config = _effective_config(tomllib.loads((root / "config.toml").read_text(encoding="utf-8")))
    except FileNotFoundError:
        result.diagnostics.append("Codex user configuration is not present")
        return result
    except (OSError, ValueError):
        result.diagnostics.append("Codex user configuration could not be read")
        return result

    disabled_servers: set[str] = set()
    for setting in (result.config.get("skills") or {}).get("config", ()):
        if isinstance(setting, Mapping) and setting.get("enabled") is False and setting.get("path"):
            result.disabled_skill_paths.append((root / Path(setting["path"]).expanduser()).resolve())

    def server(identifier: str, raw: Any, base: Path, source: str) -> None:
        if not isinstance(raw, Mapping):
            return
        identifier = identifier.lower()
        if identifier in disabled_servers:
            return
        if raw.get("enabled", True) is False:
            disabled_servers.add(identifier)
            result.server_status.append({"name": identifier, "status": "disabled", "source": source})
            return
        if identifier in {"bread", "replaylab"} or is_bread_server_config(raw):
            result.diagnostics.append(f"Reserved Host server skipped: {identifier}")
            return
        if any(item["id"] == identifier for item in result.servers):
            return
        item = dict(raw)
        item.update(id=identifier, title=identifier, source=source,
                    transport="http" if raw.get("url") else "stdio")
        item["args"] = list(raw.get("args") or ())
        item["env"] = dict(raw.get("env") or {})
        for name in raw.get("env_vars") or ():
            if str(name) in os.environ:
                item["env"][str(name)] = os.environ[str(name)]
        if raw.get("cwd"):
            item["cwd"] = str((base / str(raw["cwd"])).resolve())
        for key in ("command", "url"):
            if key in item:
                item[key] = str(item[key]).replace("${CLAUDE_PLUGIN_ROOT}", str(base)).replace("${CODEX_PLUGIN_ROOT}", str(base))
        item["args"] = [str(arg).replace("${CLAUDE_PLUGIN_ROOT}", str(base)).replace("${CODEX_PLUGIN_ROOT}", str(base)) for arg in item["args"]]
        item["headers"] = dict(raw.get("http_headers") or {})
        item["headerEnv"] = dict(raw.get("env_http_headers") or {})
        item["bearerEnv"] = str(raw.get("bearer_token_env_var") or "")
        # The shared MCP bridge must not silently turn a Codex prompt/deny rule
        # into an approval. It carries those rules into its call boundary.
        item["approvalRules"] = {"default": raw.get("default_tools_approval_mode", "approve"),
                                 "tools": dict(raw.get("tools") or {})}
        result.servers.append(item)
        result.server_status.append({"name": identifier, "status": "configured-not-yet-connected", "source": source})

    for identifier, raw in (result.config.get("mcp_servers") or {}).items():
        server(str(identifier), raw, root, "codex-user-config")
    for identifier, settings in (result.config.get("plugins") or {}).items():
        if not isinstance(settings, Mapping) or settings.get("enabled") is not True or "@" not in identifier:
            continue
        name, marketplace = identifier.rsplit("@", 1)
        if any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in (name, marketplace)):
            continue
        cache = root / "plugins" / "cache" / marketplace / name
        versions = sorted((path for path in cache.glob("*") if path.is_dir()), key=_version_key, reverse=True)
        plugin = next((path for path in versions if (path / ".codex-plugin/plugin.json").is_file()), None)
        # Use the application-owned plugin snapshot when installed. Keep login
        # and permission configuration separate from executable/plugin assets.
        if home is None:
            from .portable_capabilities import bundle_root, manifest
            shipped = next((item for item in manifest().get("plugins", []) if item["id"] == identifier), None)
            if shipped:
                plugin = bundle_root() / shipped["path"]
        if plugin is None:
            result.diagnostics.append(f"Enabled plugin is not installed locally: {identifier}")
            continue
        try:
            manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
            result.plugins[identifier] = plugin
            skill_paths = manifest.get("skills", "./skills")
            for relative in ([skill_paths] if isinstance(skill_paths, str) else skill_paths or ()):
                candidate = (plugin / relative).resolve()
                if candidate.is_dir() and candidate.is_relative_to(plugin.resolve()):
                    result.skills.append(candidate)
            declared = manifest.get("mcpServers", "./.mcp.json")
            if isinstance(declared, str):
                candidate = (plugin / declared).resolve()
                if not candidate.is_relative_to(plugin.resolve()) or not candidate.is_file():
                    continue
                declared = json.loads(candidate.read_text(encoding="utf-8"))
            for name, raw in (declared.get("mcpServers", declared) if isinstance(declared, Mapping) else {}).items():
                server(str(name), raw, plugin, f"codex-plugin:{identifier}")
        except (OSError, ValueError, TypeError):
            result.diagnostics.append(f"Enabled plugin manifest could not be read: {identifier}")
    if home is None:
        from .portable_capabilities import manifest, provider_servers
        if manifest():
            # Host-private clients loaded from old Codex configuration contain
            # transient pipe/path values. Prefer bundled clients and live IPC.
            private = {"node_repl", "cua_repl", "codex_app", "codex-app", "cua-repl"}
            policies = {item["id"]: item["approvalRules"] for item in result.servers
                        if item["id"] in private and "approvalRules" in item}
            result.servers = [item for item in result.servers if item["id"] not in private]
            result.servers.extend(provider_servers(excluded_ids=disabled_servers, policies=policies))
    return result


def codex_runtime_config(capabilities: DesktopCapabilities, managed_home: Path, *,
                         declared_mcp_ids=(), disabled_skill_paths=()) -> dict[str, Any]:
    """Import settings/cached plugin code, while retaining this connection's login.

    Only capability/permission settings are copied; credentials, account state,
    conversations and memories remain in their original homes. MCP definitions
    stay native in Codex so its tool-specific approvals remain authoritative.
    """
    allowed = {"features", "apps", "tools", "web_search", "sandbox_mode", "approval_policy",
               "default_permissions", "permissions", "sandbox_workspace_write", "shell_environment_policy",
               "computer_use", "mcp_servers", "plugins", "marketplaces", "skills"}
    values = {key: value for key, value in capabilities.config.items() if key in allowed}
    # Bread's capture route is always owned by this task, regardless of an
    # unrelated user server with the same name.
    if "mcp_servers" in values:
        servers = values.pop("mcp_servers")
        for key, value in servers.items():
            if key.casefold() not in {"bread", "replaylab"} and not is_bread_server_config(value):
                values["mcp_servers." + key] = value
    native_mcp_keys = {"command", "args", "env", "env_vars", "cwd", "url", "http_headers", "env_http_headers",
                       "bearer_token_env_var", "startup_timeout_sec", "tool_timeout_sec", "enabled", "required",
                       "enabled_tools", "disabled_tools", "default_tools_approval_mode", "tools"}
    for server in capabilities.servers:
        if str(server.get("source", "")).startswith("codex-plugin:"):
            values["mcp_servers." + server["id"]] = {key: value for key, value in server.items() if key in native_mcp_keys}
    declared = {str(name).casefold() for name in declared_mcp_ids}
    for key in list(values):
        if key.startswith("mcp_servers.") and key[len("mcp_servers."):].casefold() in declared:
            values[key] = {**values[key], "enabled": False}
    if disabled_skill_paths:
        values["skills"] = json.loads(json.dumps(values.get("skills") or {}))
        values["skills"].setdefault("config", []).extend({"path": str(path), "enabled": False} for path in disabled_skill_paths)
    for identifier, source in capabilities.plugins.items():
        name, marketplace = identifier.rsplit("@", 1)
        destination = managed_home / "plugins/cache" / marketplace / name / source.name
        if destination.resolve() == source.resolve():
            continue
        if not destination.exists():
            # Installed plugin files are executable user-enabled integrations;
            # never follow a link into the user's account database or secrets.
            if any(path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
                   for path in source.rglob("*")):
                continue
            try:
                shutil.copytree(source, destination)
            except FileExistsError:
                # A concurrent Bread conversation may install this same cache.
                pass
    if "skills" in values:
        values["skills"] = json.loads(json.dumps(values["skills"]))
        for setting in values["skills"].get("config", ()):
            if not isinstance(setting, dict) or not setting.get("path"):
                continue
            original = Path(setting["path"]).expanduser().resolve()
            for identifier, source in capabilities.plugins.items():
                if original.is_relative_to(source.resolve()):
                    name, marketplace = identifier.rsplit("@", 1)
                    setting["path"] = str(managed_home / "plugins/cache" / marketplace / name / source.name / original.relative_to(source.resolve()))
                    break
    return values


def codex_runtime_overrides(capabilities: DesktopCapabilities, managed_home: Path) -> list[str]:
    # Only boolean feature switches may appear on the process command line.
    # MCP env/header values and other configuration travel over private stdio
    # in thread/start.config, never argv or a diagnostic.
    del managed_home
    return [f"features.{key}={str(value).lower()}" for key, value in (capabilities.config.get("features") or {}).items()
            if isinstance(value, bool) and re.fullmatch(r"[a-zA-Z0-9_]+", key)]


def capability_guidance(summary: Mapping[str, Any], *, native_provider: bool) -> str:
    return ("\n\n## Connection capabilities\n" + json.dumps(dict(summary), ensure_ascii=False)
            + "\nUser-enabled desktop skills and configured MCP integrations are available through the capability directory. "
            "Only a successful server/tool response establishes availability. Do not claim that a private browser/computer/app bridge is connected merely because its skill was found. "
            "Keep explicit tool deny/approval rules and the Bread capture scope. "
            + ("Provider-native tools use that provider's configured permissions; unavailable approval callbacks are declined, not auto-approved."
               if native_provider else "This API route has owned-workspace file tools and model-dependent image input; desktop browser/computer access comes from connected MCP tools. A skill document alone does not supply an execution engine."))


def native_execution_policy(enabled: bool, settings: Mapping[str, Any], profile_options: Mapping[str, Any]) -> tuple[bool, str]:
    if not enabled or profile_options.get("native_shell") is False:
        return False, "native-shell-disabled"
    approval = profile_options.get("approval_policy", settings.get("approval_policy"))
    if approval is not None and approval != "never":
        return False, "configured-provider-approval-requires-live-broker"
    sandbox = profile_options.get("sandbox_mode", settings.get("sandbox_mode"))
    if sandbox is not None and sandbox != "danger-full-access":
        return False, "configured-sandbox-cannot-be-enforced-by-local-shell"
    if settings.get("default_permissions") or profile_options.get("permissions"):
        return False, "named-permission-profile-requires-native-provider"
    return True, "explicit-user-enabled-local-execution"


__all__ = ["DesktopCapabilities", "discover_desktop_capabilities", "codex_runtime_overrides", "codex_runtime_config", "capability_guidance"]
