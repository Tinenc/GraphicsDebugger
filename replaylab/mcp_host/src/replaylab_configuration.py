"""The configuration Settings > AI edits, readable and writable by the Agent.

One file per concern, both owned by the user:

* ``agent-tools.json`` — Skill folders, per-Skill enablement, external MCP servers.
  The AgentHost owns its schema; this module reads and edits the same document so a
  change made by the Agent and a change made in the panel are the same change.
* ``asset-readers.json`` — extractors registered for unpacked asset formats.

Nothing here grants a capture effect. Every Skill, including ``replaylab-core``,
is optional guidance that the user can enable or disable.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .asset_readers import (
    MAX_READERS,
    default_readers_path,
    load_external_readers,
    save_external_readers,
    validate_reader,
)


AGENT_TOOLS_SCHEMA = "replaylab-agent-tools/v1"
MAX_SKILL_ROOTS = 16
MAX_MCP_SERVERS = 32
# Same shapes the agent loader (agent_tools_config) accepts.
_MCP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MCP_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_MCP_ARGS = 64
MAX_MCP_ENV = 32


class _McpEntryError(ValueError):
    """One MCP field the agent loader would drop the whole server for."""


def _single_line(value: Any, label: str, limit: int) -> str:
    text = str(value or "").strip()
    if "\0" in text or "\n" in text or "\r" in text:
        raise _McpEntryError("%s must be a single line" % label)
    if len(text) > limit:
        raise _McpEntryError("%s is longer than %d characters" % (label, limit))
    return text
ALWAYS_ENABLED_SKILLS = frozenset()
# The loader's own limits (replaylab_agent.skills): a document the host accepts
# must be one the loader admits, or the AI is told it installed something it did not.
MAX_SKILL_BYTES = 256 * 1024
MAX_SKILL_BODY_CHARS = 128 * 1024
MAX_SKILL_METADATA_ENTRIES = 64
MAX_SKILL_METADATA_KEY_CHARS = 128
MAX_SKILL_METADATA_VALUE_CHARS = 16 * 1024
MAX_SKILL_CATEGORY_CHARS = 96
MAX_SKILL_VERSION = 1_000_000
_SKILL_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def user_skill_overlay_root(tools_path: Path | str | None = None) -> Path:
    """Where a Skill written from a task is kept.

    Never the installed package: that folder is replaced by the next update, and a
    Skill the user asked for should outlive it.
    """

    base = Path(tools_path) if tools_path is not None else default_agent_tools_path()
    return base.parent / "skills"


def default_agent_tools_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    # Per-user data home, never the shared temp directory: the agent, the bridge
    # and qrenderdoc resolve this file with the same rule.
    root = Path(local).expanduser() if local else Path.home() / ".local" / "share"
    return root / "ReplayLab" / "config" / "agent-tools.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _discovered_skills(roots: list[str]) -> list[dict[str, Any]]:
    """Every SKILL.md under the configured roots, with its declared identity."""

    skills: list[dict[str, Any]] = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            manifest = entry / "SKILL.md"
            if not entry.is_dir() or not manifest.is_file():
                continue
            name = entry.name
            description = ""
            version = ""
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")[:8192]
            except OSError:
                text = ""
            for line in text.splitlines()[:24]:
                stripped = line.strip()
                if stripped.startswith("name:"):
                    name = stripped[5:].strip() or name
                elif stripped.startswith("description:"):
                    description = stripped[12:].strip()
                elif stripped.startswith("replaylab-version:"):
                    version = stripped[18:].strip().strip('"')
            skills.append({
                "id": name,
                "description": description[:400],
                "version": version,
                "path": str(manifest),
                "root": str(base),
            })
    return skills


def read_agent_tools(path: Path | str | None = None, bundled_root: Path | str | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else default_agent_tools_path()
    payload = _load(target)
    roots = [str(item) for item in (payload.get("skillRoots") or []) if str(item).strip()]
    if bundled_root:
        roots = [str(bundled_root)] + [item for item in roots if item != str(bundled_root)]
    disabled = sorted({str(item) for item in (payload.get("disabledSkills") or []) if str(item).strip()})
    discovered = _discovered_skills(roots)
    for skill in discovered:
        skill["enabled"] = skill["id"] not in disabled
        skill["always"] = skill["id"] in ALWAYS_ENABLED_SKILLS
    return {
        "path": str(target),
        "skillRoots": roots,
        "disabledSkills": disabled,
        "skills": discovered,
        "mcpServers": [dict(item) for item in (payload.get("mcpServers") or []) if isinstance(item, Mapping)],
    }



def skill_document_problem(identifier: str, content: str) -> str:
    """Why the runtime loader would refuse this Skill, or "" if it would not.

    The same rules ``replaylab_agent.skills`` applies at load time: YAML front
    matter, ``name`` equal to the folder, a non-empty ``description`` under 1024
    characters, scalar metadata within the loader's counts and sizes, a valid
    ``replaylab-category`` and version numbers, a body under 128 Ki characters. Writing a document that fails them used to report
    "wrote Skill x, enabled Skill x" -- and the next task quarantined it with a
    content-free code, so the user saw nothing and the AI believed it had installed
    something.
    """

    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return ("Skill %s must start with YAML front matter: a line '---', then "
                "'name: %s' and 'description: ...', then a closing '---'"
                % (identifier, identifier))
    try:
        import yaml
    except ImportError:
        return "Skill %s cannot be checked: PyYAML is not installed in this host" % identifier
    try:
        _unused, header, body = text.split("---", 2)
        value = yaml.safe_load(header) or {}
    except ValueError:
        return "Skill %s front matter is not closed with a second '---'" % identifier
    except Exception as error:  # noqa: BLE001 - yaml error types vary
        return "Skill %s front matter is not valid YAML: %s" % (identifier, str(error)[:120])
    if not isinstance(value, dict):
        return "Skill %s front matter must be a mapping" % identifier
    name = value.get("name", "")
    if name != identifier:
        return ("Skill %s front matter must say 'name: %s' (it says %r); the loader "
                "requires the name to equal the folder" % (identifier, identifier, name))
    description = value.get("description", "")
    if not isinstance(description, str) or not description.strip():
        return "Skill %s needs a non-empty 'description' in its front matter" % identifier
    if len(description.strip()) > 1024:
        return "Skill %s description must be at most 1024 characters" % identifier
    metadata = value.get("metadata", {}) or {}
    if not isinstance(metadata, dict) or any(
            not isinstance(key, str) or not isinstance(item, (str, int, float, bool))
            for key, item in metadata.items()):
        return "Skill %s metadata must be a mapping of scalar values" % identifier
    if len(metadata) > MAX_SKILL_METADATA_ENTRIES:
        return "Skill %s metadata may hold at most %d entries" % (
            identifier, MAX_SKILL_METADATA_ENTRIES)
    for key, item in metadata.items():
        if not key or len(key) > MAX_SKILL_METADATA_KEY_CHARS:
            return "Skill %s has an empty or oversized metadata key" % identifier
        if len(str(item)) > MAX_SKILL_METADATA_VALUE_CHARS:
            return "Skill %s metadata value for %r is too long" % (identifier, key)
    category = str(metadata.get("replaylab-category", "other"))
    if len(category) > MAX_SKILL_CATEGORY_CHARS or not _SKILL_ID_RE.fullmatch(category):
        return ("Skill %s metadata replaylab-category must be lower-case words joined by "
                "hyphens (it says %r)" % (identifier, category[:64]))
    for key in ("replaylab-version", "replaylab-registry-version"):
        if key in metadata:
            try:
                number = int(str(metadata[key]))
            except ValueError:
                number = 0
            if not 1 <= number <= MAX_SKILL_VERSION:
                return "Skill %s metadata %s must be a positive integer" % (identifier, key)
    if "replaylab-registry-virtual" in metadata:
        return ("Skill %s must not declare replaylab-registry-virtual; the loader sets "
                "that marker itself" % identifier)
    if not body.strip():
        return "Skill %s has front matter but no procedure below it" % identifier
    if len(body.strip()) > MAX_SKILL_BODY_CHARS:
        return "Skill %s body exceeds %d characters" % (identifier, MAX_SKILL_BODY_CHARS)
    return ""


def _mcp_server_entry(identifier: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Build the on-disk entry, refusing exactly what the agent loader would drop."""

    transport = _single_line(raw.get("transport", ""), "transport", 16).lower()
    url = _single_line(raw.get("url", ""), "url", 1024)
    command = _single_line(raw.get("command", ""), "command", 1024)
    if not transport:
        transport = "http" if url and not command else "stdio"
    entry: dict[str, Any] = {
        "id": identifier,
        "title": _single_line(raw.get("title", ""), "title", 128) or identifier,
        "enabled": bool(raw.get("enabled", True)),
        "transport": transport,
        "note": _single_line(raw.get("note", ""), "note", 512),
    }
    if transport == "http":
        # The same rule Settings applies: a URL-reached server, TLS only, so a
        # bearer token never crosses the wire in clear text.
        if not url.lower().startswith("https://"):
            raise _McpEntryError("an HTTP server needs a url starting with https://")
        entry["url"] = url
        return entry
    if transport != "stdio":
        raise _McpEntryError("transport must be 'stdio' or 'http'")
    if not command:
        raise _McpEntryError("a stdio server needs a command")
    arguments = raw.get("args") or []
    if not isinstance(arguments, (list, tuple)):
        raise _McpEntryError("args must be a list of strings")
    if len(arguments) > MAX_MCP_ARGS:
        raise _McpEntryError("at most %d args are accepted" % MAX_MCP_ARGS)
    environment = raw.get("env") or {}
    if not isinstance(environment, Mapping):
        raise _McpEntryError("env must be an object of NAME=value pairs")
    if len(environment) > MAX_MCP_ENV:
        raise _McpEntryError("at most %d env values are accepted" % MAX_MCP_ENV)
    env: dict[str, str] = {}
    for name, value in environment.items():
        key = str(name or "").strip()
        if not _MCP_ENV_NAME_RE.fullmatch(key):
            raise _McpEntryError("invalid env name %r" % key[:64])
        env[key] = _single_line(value, "env value %s" % key, 4096)
    entry["command"] = command
    entry["args"] = [_single_line(item, "arg", 1024) for item in arguments]
    entry["env"] = env
    return entry


def apply_configuration_change(
    *,
    enable_skills: list[str] = (),
    disable_skills: list[str] = (),
    add_skill_roots: list[str] = (),
    remove_skill_roots: list[str] = (),
    upsert_mcp_servers: list[Mapping[str, Any]] = (),
    remove_mcp_servers: list[str] = (),
    upsert_asset_readers: list[Mapping[str, Any]] = (),
    remove_asset_readers: list[str] = (),
    upsert_tools: list[Mapping[str, Any]] = (),
    remove_tools: list[str] = (),
    write_skills: list[Mapping[str, Any]] = (),
    tools_path: Path | str | None = None,
    readers_path: Path | str | None = None,
    providers_path: Path | str | None = None,
) -> dict[str, Any]:
    """Apply an additive change and report exactly what moved."""

    target = Path(tools_path) if tools_path is not None else default_agent_tools_path()
    payload = _load(target)
    payload.setdefault("schema", AGENT_TOOLS_SCHEMA)
    roots = [str(item) for item in (payload.get("skillRoots") or []) if str(item).strip()]
    disabled = {str(item) for item in (payload.get("disabledSkills") or []) if str(item).strip()}
    servers = [dict(item) for item in (payload.get("mcpServers") or []) if isinstance(item, Mapping)]
    changes: list[str] = []
    rejected: list[str] = []

    for skill in enable_skills:
        identifier = str(skill or "").strip()
        if identifier and identifier in disabled:
            disabled.discard(identifier)
            changes.append("enabled Skill %s" % identifier)
    for skill in disable_skills:
        identifier = str(skill or "").strip()
        if not identifier:
            continue
        if identifier not in disabled:
            disabled.add(identifier)
            changes.append("disabled Skill %s" % identifier)

    for root in add_skill_roots:
        candidate = Path(str(root)).expanduser()
        if not candidate.is_absolute():
            rejected.append("Skill folder must be an absolute path: %s" % root)
            continue
        if not candidate.is_dir():
            rejected.append("no such Skill folder: %s" % candidate)
            continue
        if str(candidate) in roots:
            continue
        if len(roots) >= MAX_SKILL_ROOTS:
            rejected.append("at most %d Skill folders are supported" % MAX_SKILL_ROOTS)
            break
        roots.append(str(candidate))
        changes.append("added Skill folder %s" % candidate)
    for root in remove_skill_roots:
        candidate = str(Path(str(root)).expanduser())
        if candidate in roots:
            roots = [item for item in roots if item != candidate]
            changes.append("removed Skill folder %s" % candidate)

    for raw in upsert_mcp_servers:
        if not isinstance(raw, Mapping):
            continue
        identifier = str(raw.get("id", "") or "").strip().lower()
        if not identifier:
            rejected.append("an MCP server needs an id")
            continue
        # Mirror the agent loader's rules here so a bad entry is refused with a
        # reason now instead of being silently dropped at the next load.
        if not _MCP_ID_RE.fullmatch(identifier):
            rejected.append(
                "MCP server id must be lower-case letters, digits, '.', '-' or '_': %s"
                % identifier[:64])
            continue
        if identifier == "replaylab":
            rejected.append("'replaylab' is the built-in server id and cannot be redefined")
            continue
        existing = next((item for item in servers
                         if str(item.get("id", "")).lower() == identifier), None)
        # An entry that only carries {id, enabled} is a toggle: Settings can switch
        # a server off in place, and the AI could not -- it had to re-supply the
        # whole connection to change one flag.
        if existing is not None and set(raw.keys()) <= {"id", "enabled"} and "enabled" in raw:
            entry = dict(existing)
            entry["enabled"] = bool(raw.get("enabled", True))
            servers = [item for item in servers if str(item.get("id", "")).lower() != identifier]
            servers.append(entry)
            changes.append("%s MCP server %s" % (
                "enabled" if entry["enabled"] else "disabled", identifier))
            continue
        try:
            entry = _mcp_server_entry(identifier, raw)
        except _McpEntryError as error:
            rejected.append("MCP server %s: %s" % (identifier, error))
            continue
        servers = [item for item in servers if str(item.get("id", "")).lower() != identifier]
        if len(servers) >= MAX_MCP_SERVERS:
            rejected.append("at most %d MCP servers are supported" % MAX_MCP_SERVERS)
            break
        servers.append(entry)
        changes.append("configured MCP server %s" % identifier)
    for server in remove_mcp_servers:
        identifier = str(server or "").strip().lower()
        remaining = [item for item in servers if str(item.get("id", "")).lower() != identifier]
        if len(remaining) != len(servers):
            servers = remaining
            changes.append("removed MCP server %s" % identifier)

    payload["skillRoots"] = roots
    payload["disabledSkills"] = sorted(disabled)
    payload["mcpServers"] = servers
    if changes:
        _save(target, payload)

    readers_file = Path(readers_path) if readers_path is not None else default_readers_path()
    readers = [dict(item) for item in load_external_readers(readers_file)]
    reader_changes = False
    for raw in upsert_asset_readers:
        if not isinstance(raw, Mapping):
            continue
        try:
            reader = validate_reader(raw)
        except Exception as error:  # noqa: BLE001 - report, never crash the task
            rejected.append("asset reader rejected: %s" % str(error)[:200])
            continue
        readers = [item for item in readers if item.get("id") != reader["id"]]
        if len(readers) >= MAX_READERS:
            rejected.append("at most %d asset readers are supported" % MAX_READERS)
            break
        readers.append(reader)
        reader_changes = True
        changes.append("registered asset reader %s" % reader["id"])
    for reader_id in remove_asset_readers:
        identifier = str(reader_id or "").strip().lower()
        remaining = [item for item in readers if item.get("id") != identifier]
        if len(remaining) != len(readers):
            readers = remaining
            reader_changes = True
            changes.append("removed asset reader %s" % identifier)
    if reader_changes:
        save_external_readers(readers, readers_file)

    # External tools: MCP servers and command-line extractors are one list.
    provider_ids: list[str] = []
    try:
        from .tool_providers import (
            ToolConfigError,
            default_tools_path,
            load_tools,
            remove_tool,
            upsert_tool,
        )

        providers_file = Path(providers_path) if providers_path is not None \
            else default_tools_path()
        for raw in upsert_tools:
            if not isinstance(raw, Mapping):
                continue
            try:
                tool = upsert_tool(raw, providers_file)
            except ToolConfigError as error:
                rejected.append("tool rejected: %s" % str(error)[:200])
                continue
            changes.append("registered tool %s" % tool["id"])
        for tool_id in remove_tools:
            if remove_tool(str(tool_id or ""), providers_file):
                changes.append("removed tool %s" % str(tool_id))
        provider_ids = [item["id"] for item in load_tools(providers_file)[0]]
    except Exception as error:  # noqa: BLE001 - configuration never crashes a task
        if upsert_tools or remove_tools:
            rejected.append("tool list unavailable: %s" % str(error)[:200])

    written: list[str] = []
    overlay = user_skill_overlay_root(target)
    for raw in write_skills:
        if not isinstance(raw, Mapping):
            continue
        identifier = str(raw.get("id", "") or "").strip().lower()
        content = str(raw.get("content", "") or "")
        if not _SKILL_ID_RE.fullmatch(identifier) or len(identifier) > MAX_SKILL_CATEGORY_CHARS:
            rejected.append(
                "a Skill id must be lower-case words of letters and digits joined by "
                "single hyphens (for example 'ue-hair-passes'), got %r" % identifier[:64])
            continue
        if not content.strip():
            rejected.append("Skill %s has no content" % identifier)
            continue
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_SKILL_BYTES:
            rejected.append("Skill %s is larger than %d bytes" % (identifier, MAX_SKILL_BYTES))
            continue
        problem = skill_document_problem(identifier, content)
        if problem:
            # Refuse now, with the reason, rather than write a document the loader
            # will quarantine silently at the next task.
            rejected.append(problem)
            continue
        document = overlay / identifier / "SKILL.md"
        if document.is_file() and not bool(raw.get("replace", False)):
            rejected.append(
                "Skill %s already exists; pass replace to overwrite %s" % (identifier, document))
            continue
        try:
            document.parent.mkdir(parents=True, exist_ok=True)
            temporary = document.with_name(document.name + ".tmp")
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, document)
        except OSError as error:
            rejected.append("could not write Skill %s: %s" % (identifier, str(error)[:200]))
            continue
        written.append(str(document))
        changes.append("wrote Skill %s to %s" % (identifier, document))
        if str(overlay) not in roots:
            if len(roots) >= MAX_SKILL_ROOTS:
                # The loader reads the first MAX_SKILL_ROOTS folders only; put the
                # user's own folder first rather than past the end of the list.
                roots.insert(0, str(overlay))
                dropped = roots.pop()
                changes.append("added Skill folder %s ahead of %s (the loader reads at most %d)"
                               % (overlay, dropped, MAX_SKILL_ROOTS))
            else:
                roots.append(str(overlay))
                changes.append("added Skill folder %s" % overlay)
        # A Skill the user asked for is a Skill they want offered.
        if identifier in disabled:
            disabled.discard(identifier)
            changes.append("enabled Skill %s" % identifier)
    if written:
        payload["skillRoots"] = roots
        payload["disabledSkills"] = sorted(disabled)
        _save(target, payload)

    return {
        "changed": changes,
        "rejected": rejected,
        "skillRoots": roots,
        "disabledSkills": sorted(disabled),
        "mcpServers": [item.get("id") for item in servers],
        "assetReaders": [item.get("id") for item in readers],
        "tools": provider_ids,
        "writtenSkills": written,
        "files": {"agentTools": str(target), "assetReaders": str(readers_file),
                  "skillOverlay": str(overlay)},
    }


__all__ = [
    "ALWAYS_ENABLED_SKILLS",
    "apply_configuration_change",
    "default_agent_tools_path",
    "read_agent_tools",
    "user_skill_overlay_root",
]
