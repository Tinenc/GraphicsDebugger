"""User configuration for Skills and external MCP servers.

The Agent decides *which* Skill a request needs; this file decides what is on
the menu at all.  It is edited from qrenderdoc (Settings > AI) through the
AgentHost ``tools`` commands, and is read by both engines, so an ACP
subscription and an API connection see exactly the same catalog.

Nothing here grants Host effects. Disabling a Skill removes it from the
directory; adding an MCP server attaches the user's own server to the vendor
session next to the task-scoped ReplayLab route.
"""

from __future__ import annotations

import json
import ipaddress
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .bundled_paths import expand_tool_path, fold_tool_path

from .errors import ConfigurationError


SCHEMA = "replaylab-agent-tools/v1"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_SKILL_ROOTS = 16
MAX_MCP_SERVERS = 32
MAX_MCP_ARGS = 64
MAX_MCP_ENV = 32
MAX_TEXT = 512


def default_tools_config_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    # Never the shared temp directory: on Linux/macOS that put the task event
    # stream, the log and the tool settings under a world-readable /tmp that any
    # local user could pre-plant and that vanished on reboot. XDG data home is the
    # per-user equivalent of LOCALAPPDATA, which the rest of the product uses.
    root = Path(local).expanduser() if local else Path.home() / ".local" / "share"
    return root / "ReplayLab" / "config" / "agent-tools.json"


def user_skill_overlay_root(config_path: Path | str | None = None) -> Path:
    """Where an edited or newly written Skill is kept.

    A bundled Skill is replaced by the next install, so Settings never writes to
    it: the edit is copied here, this folder is registered as a Skill root, and
    the copy is what the catalog loads from then on.
    """

    base = Path(config_path) if config_path is not None else default_tools_config_path()
    return base.parent / "skills"


@dataclass(slots=True)
class MCPServerConfig:
    id: str
    title: str = ""
    enabled: bool = True
    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title or self.id,
            "enabled": bool(self.enabled),
            "transport": self.transport,
        }
        if self.transport == "stdio":
            payload["command"] = fold_tool_path(self.command)
            payload["args"] = [fold_tool_path(item) for item in self.args]
            payload["env"] = dict(self.env)
        else:
            payload["url"] = self.url
        if self.note:
            payload["note"] = self.note
        return payload


def _text(value: Any, label: str, *, limit: int = MAX_TEXT, required: bool = False) -> str:
    text = str(value or "").strip()
    if "\0" in text or "\n" in text or "\r" in text:
        raise ConfigurationError(f"{label} must be a single line")
    if len(text) > limit:
        raise ConfigurationError(f"{label} is longer than {limit} characters")
    if required and not text:
        raise ConfigurationError(f"{label} is required")
    return text


def validate_mcp_server(raw: Mapping[str, Any]) -> MCPServerConfig:
    """Validate one user-declared MCP server; never trust the file on disk."""

    identifier = _text(raw.get("id"), "MCP server id", limit=64, required=True).lower()
    if not _ID_RE.fullmatch(identifier):
        raise ConfigurationError(
            "MCP server id must be lower-case letters, digits, '.', '-' or '_'"
        )
    if identifier == "replaylab":
        raise ConfigurationError("'replaylab' is the built-in server id and cannot be redefined")
    transport = _text(raw.get("transport") or "stdio", "MCP transport", limit=16).lower()
    if transport not in {"stdio", "http"}:
        raise ConfigurationError("MCP transport must be 'stdio' or 'http'")
    server = MCPServerConfig(
        id=identifier,
        title=_text(raw.get("title"), "MCP server title", limit=128) or identifier,
        enabled=bool(raw.get("enabled", True)),
        transport=transport,
        note=_text(raw.get("note"), "MCP server note", limit=MAX_TEXT),
    )
    if transport == "stdio":
        server.command = expand_tool_path(_text(raw.get("command"), "MCP command", limit=1024, required=True))
        arguments = raw.get("args") or []
        if not isinstance(arguments, (list, tuple)):
            raise ConfigurationError("MCP args must be a list of strings")
        if len(arguments) > MAX_MCP_ARGS:
            raise ConfigurationError(f"an MCP server accepts at most {MAX_MCP_ARGS} arguments")
        server.args = tuple(expand_tool_path(_text(item, "MCP argument", limit=1024)) for item in arguments)
        environment = raw.get("env") or {}
        if not isinstance(environment, Mapping):
            raise ConfigurationError("MCP env must be an object of NAME=value pairs")
        if len(environment) > MAX_MCP_ENV:
            raise ConfigurationError(f"an MCP server accepts at most {MAX_MCP_ENV} environment values")
        env: dict[str, str] = {}
        for name, value in environment.items():
            key = str(name or "").strip()
            if not _ENV_NAME_RE.fullmatch(key):
                raise ConfigurationError(f"invalid MCP environment name: {key[:64]}")
            env[key] = _text(value, "MCP environment value", limit=4096)
        server.env = env
    else:
        url = _text(raw.get("url"), "MCP URL", limit=1024, required=True)
        try:
            endpoint = urlsplit(url)
            host = endpoint.hostname or ""
            port = endpoint.port
            loopback = host.lower() == "localhost"
            if not loopback:
                try:
                    loopback = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    pass
            if (not host or "\\" in url or any(char.isspace() for char in host)
                    or endpoint.username is not None or endpoint.password is not None
                    or endpoint.fragment or port is not None and not 1 <= port <= 65535):
                raise ValueError("invalid endpoint")
            if endpoint.scheme not in {"https", "http"} or endpoint.scheme == "http" and (not loopback or endpoint.query):
                raise ValueError("insecure endpoint")
        except ValueError:
            # Never echo a rejected URL: it may contain an embedded credential.
            raise ConfigurationError(
                "MCP requires HTTPS, or HTTP on a literal loopback address/localhost without a query; "
                "URL credentials and fragments are not allowed"
            ) from None
        server.url = url
    return server


@dataclass(slots=True)
class AgentToolsConfig:
    """Skill roots, disabled Skills and external MCP servers."""

    skill_roots: tuple[Path, ...] = ()
    disabled_skills: tuple[str, ...] = ()
    mcp_servers: tuple[MCPServerConfig, ...] = ()
    path: Path | None = None
    diagnostics: tuple[str, ...] = ()
    # Opt-in: reuse user-enabled desktop integrations, without merging secrets
    # or imported entries back into this saved configuration.
    inherit_desktop_capabilities: bool = False
    native_shell_enabled: bool = False
    desktop_disabled_skill_ids: tuple[str, ...] = ()
    runtime_disabled_skill_paths: tuple[str, ...] = ()

    # ------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: Path | str | None = None) -> "AgentToolsConfig":
        """Read the configuration; an unreadable or invalid file yields defaults.

        Configuration must never stop a task from running: a broken entry is
        dropped and reported in ``diagnostics``.
        """

        source = Path(path) if path is not None else default_tools_config_path()
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path=source)
        except (OSError, ValueError) as error:
            return cls(path=source, diagnostics=(f"tool configuration unreadable: {error}"[:400],))
        if not isinstance(payload, Mapping):
            return cls(path=source, diagnostics=("tool configuration is not an object",))
        diagnostics: list[str] = []
        roots: list[Path] = []
        for item in list(payload.get("skillRoots") or [])[:MAX_SKILL_ROOTS]:
            text = str(item or "").strip()
            if not text:
                continue
            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                diagnostics.append(f"skill root is not absolute: {text[:200]}")
                continue
            roots.append(candidate)
        disabled = tuple(
            sorted(
                {
                    str(item or "").strip()
                    for item in list(payload.get("disabledSkills") or [])[:128]
                    if str(item or "").strip()
                }
            )
        )
        servers: list[MCPServerConfig] = []
        seen: set[str] = set()
        for raw in list(payload.get("mcpServers") or [])[:MAX_MCP_SERVERS]:
            if not isinstance(raw, Mapping):
                continue
            try:
                server = validate_mcp_server(raw)
            except ConfigurationError as error:
                diagnostics.append(f"MCP server dropped: {error}"[:400])
                continue
            if server.id in seen:
                diagnostics.append(f"duplicate MCP server id dropped: {server.id}")
                continue
            seen.add(server.id)
            servers.append(server)
        return cls(
            skill_roots=tuple(roots),
            disabled_skills=disabled,
            mcp_servers=tuple(servers),
            path=source,
            diagnostics=tuple(diagnostics[:32]),
            inherit_desktop_capabilities=payload.get("inheritDesktopCapabilities") is True,
            native_shell_enabled=payload.get("nativeShellEnabled") is True,
        )

    # ------------------------------------------------------------ saving
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "skillRoots": [str(root) for root in self.skill_roots],
            "disabledSkills": list(self.disabled_skills),
            "mcpServers": [server.to_dict() for server in self.mcp_servers],
            "inheritDesktopCapabilities": self.inherit_desktop_capabilities,
            "nativeShellEnabled": self.native_shell_enabled,
        }

    def save(self, path: Path | str | None = None) -> Path:
        target = Path(path) if path is not None else (self.path or default_tools_config_path())
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, target)
        self.path = target
        return target

    # ----------------------------------------------------------- editing
    def add_skill_root(self, root: Path | str) -> None:
        candidate = Path(str(root)).expanduser()
        if not candidate.is_absolute():
            raise ConfigurationError("a Skill folder must be an absolute path")
        if not candidate.is_dir():
            raise ConfigurationError(f"no such Skill folder: {candidate}")
        if candidate in self.skill_roots:
            return
        if len(self.skill_roots) >= MAX_SKILL_ROOTS:
            raise ConfigurationError(f"at most {MAX_SKILL_ROOTS} Skill folders are supported")
        self.skill_roots = self.skill_roots + (candidate,)

    def remove_skill_root(self, root: Path | str) -> bool:
        candidate = Path(str(root)).expanduser()
        remaining = tuple(item for item in self.skill_roots if item != candidate)
        changed = len(remaining) != len(self.skill_roots)
        self.skill_roots = remaining
        return changed

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> None:
        identifier = _text(skill_id, "Skill id", limit=96, required=True)
        disabled = set(self.disabled_skills)
        if enabled:
            disabled.discard(identifier)
        else:
            disabled.add(identifier)
        self.disabled_skills = tuple(sorted(disabled))

    def upsert_mcp_server(self, raw: Mapping[str, Any]) -> MCPServerConfig:
        server = validate_mcp_server(raw)
        remaining = [item for item in self.mcp_servers if item.id != server.id]
        if len(remaining) >= MAX_MCP_SERVERS:
            raise ConfigurationError(f"at most {MAX_MCP_SERVERS} MCP servers are supported")
        remaining.append(server)
        self.mcp_servers = tuple(sorted(remaining, key=lambda item: item.id))
        return server

    def remove_mcp_server(self, server_id: str) -> bool:
        identifier = str(server_id or "").strip().lower()
        remaining = tuple(item for item in self.mcp_servers if item.id != identifier)
        changed = len(remaining) != len(self.mcp_servers)
        self.mcp_servers = remaining
        return changed

    def set_mcp_enabled(self, server_id: str, enabled: bool) -> bool:
        identifier = str(server_id or "").strip().lower()
        found = False
        for server in self.mcp_servers:
            if server.id == identifier:
                server.enabled = bool(enabled)
                found = True
        return found

    # ------------------------------------------------------------- views
    def enabled_mcp_servers(self) -> tuple[MCPServerConfig, ...]:
        return tuple(server for server in self.mcp_servers if server.enabled)

    def skill_root_list(self, bundled_root: Path | str) -> tuple[Path, ...]:
        """The bundled root first, then the user's, without duplicates."""

        from .portable_capabilities import skill_roots
        roots: list[Path] = [Path(bundled_root), *skill_roots()]
        for root in self.skill_roots:
            if root not in roots:
                roots.append(root)
        if self.inherit_desktop_capabilities and not skill_roots():
            from .desktop_capabilities import discover_desktop_capabilities
            for root in discover_desktop_capabilities().skills:
                if root not in roots:
                    roots.append(root)
        return tuple(roots)

    def is_skill_enabled(self, skill_id: str) -> bool:
        return str(skill_id or "").strip() not in set(self.disabled_skills)

    def filter_catalog(self, catalog: Any) -> Any:
        """Drop user-disabled Skills; a Skill never grants Host authority."""

        disabled = set(self.disabled_skills)
        if self.inherit_desktop_capabilities:
            from .desktop_capabilities import discover_desktop_capabilities
            paths = discover_desktop_capabilities().disabled_skill_paths
            inherited = set()
            for identifier, skill in catalog.skills.items():
                path = getattr(skill, "path", None)
                if path and any(Path(path).resolve() == item or Path(path).resolve().is_relative_to(item) for item in paths):
                    inherited.add(identifier)
            self.desktop_disabled_skill_ids = tuple(sorted(inherited))
            disabled.update(inherited)
        if not disabled:
            return catalog
        self.runtime_disabled_skill_paths = tuple(str(getattr(skill, "path", ""))
            for identifier, skill in catalog.skills.items() if identifier in disabled and getattr(skill, "path", None))
        for skill_id in list(catalog.skills):
            if skill_id in disabled:
                del catalog.skills[skill_id]
        return catalog


def _owning_root(document: Path, roots: tuple[Path, ...]) -> Path:
    """Which registered folder offers this Skill.

    A Skill lives in its own directory under a registered root, so the root is the
    grandparent of ``SKILL.md`` — but only the configuration knows which one is
    registered, and a nested layout would make that guess wrong.
    """

    for root in roots:
        try:
            document.relative_to(root)
        except (ValueError, OSError):
            continue
        return root
    return document.parent.parent


def describe_skills(catalog: Any, config: AgentToolsConfig, bundled_root: Path | str) -> list[dict[str, Any]]:
    """Directory rows for the Settings UI: every discovered Skill and its state."""

    bundled = Path(bundled_root).resolve()
    from .portable_capabilities import skill_roots
    from .bundled_paths import app_root
    built_in_roots = tuple(root.resolve() for root in (bundled, app_root() / "skills", *skill_roots()))
    rows: list[dict[str, Any]] = []
    for skill in sorted(catalog.skills.values(), key=lambda item: item.id):
        path = Path(getattr(skill, "path", "") or "").resolve()
        source = "bundled"
        if not any(path.is_relative_to(root) for root in built_in_roots):
            source = "user"
        # The registered root, not the Skill's own folder: Settings removes what the
        # configuration actually holds, and must never derive it from the document path.
        root = _owning_root(path, built_in_roots if source == "bundled" else config.skill_roots)
        rows.append(
            {
                "id": skill.id,
                "title": skill.title,
                "description": skill.description,
                "category": skill.category,
                "always": bool(getattr(skill, "always", False)),
                "enabled": config.is_skill_enabled(skill.id),
                "source": source,
                "path": str(path),
                "root": str(root),
                "removable": source == "user" and not bool(getattr(skill, "always", False)),
                "version": getattr(skill, "version", ""),
            }
        )
    return rows


def servers_for_directory(servers: Iterable[MCPServerConfig]) -> list[dict[str, Any]]:
    return [
        {
            "id": server.id,
            "title": server.title or server.id,
            "enabled": bool(server.enabled),
            "transport": server.transport,
            "note": server.note,
        }
        for server in servers
    ]


def servers_for_runtime(servers: Sequence[MCPServerConfig]) -> tuple[dict[str, Any], ...]:
    return tuple(server.to_dict() for server in servers)


def mcp_server_runtime_status(
    server: MCPServerConfig, *, respect_enabled: bool = True
) -> tuple[bool, str]:
    """Say whether an enabled server can be attached on this machine now.

    Keep unavailable optional programs in Settings so the user can repair them,
    but do not advertise a stdio server that cannot actually be started. HTTP
    reachability is established by the normal bounded MCP handshake.
    """

    if respect_enabled and not server.enabled:
        return False, "disabled"
    if server.transport != "stdio":
        return True, ""
    command = str(server.command or "").strip()
    if not command:
        return False, "stdio command is empty"
    resolved = shutil.which(command)
    candidate = Path(resolved) if resolved else Path(command).expanduser()
    if candidate.is_file():
        return True, ""
    return False, "stdio executable is unavailable: %s" % command[:300]


__all__ = [
    "AgentToolsConfig",
    "MCPServerConfig",
    "SCHEMA",
    "default_tools_config_path",
    "describe_skills",
    "servers_for_directory",
    "servers_for_runtime",
    "mcp_server_runtime_status",
    "user_skill_overlay_root",
    "validate_mcp_server",
]
