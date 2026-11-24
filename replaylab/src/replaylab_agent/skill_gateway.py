"""Capability directory and on-demand Skill loading for every AI connection.

Intent recognition belongs to the Agent, not to a keyword table in the Host.
Every route (vendor-native ACP, Codex, and the API/deepagents route) is handed
the same small local tool surface:

``list_bread_capabilities``
    The catalog of project Skills, the granted Host tool families, and the
    external MCP servers the user configured, with no capture side effects.
    ``family=<name>`` expands one family; ``query=<text>`` searches every
    granted tool name and description when a needed tool seems missing.

``load_bread_skill``
    The task guidance and acceptance criteria of the domain Skill selected by
    the Agent, loaded before substantive domain work.

``invoke_bread_tool``
    The dispatcher for less common granted Host operations. The selected
    operation's schema and real effect policy still apply server-side; a
    separate contract-loading call is optional.

``record_task_review``
    Main-Agent-only completion assessment, also available without a capture or
    domain Skill. Delegated Agents return evidence to the main Agent.

The Agent selects relevant guidance and the supported tool route. General
conversation needs no domain Skill or capture operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .agent_registry import CatalogView, SkillInfo
from .errors import ConfigurationError
from .policy import (
    AGENT_LOCAL_TOOLS,
    tool_effect_variants,
    tool_effects,
    tool_has_parameterized_effects,
)


DIRECTORY_TOOL = "list_bread_capabilities"
LOAD_SKILL_TOOL = "load_bread_skill"
INVOKE_TOOL = "invoke_bread_tool"
DIRECTORY_TOOL_DESCRIPTION = (
    "Discover a missing Bread capability or inspect an unfamiliar tool's arguments. "
    "Use tools already present in your tool list directly with their supplied schemas; "
    "discovery is not a prerequisite. Reuse a contract already in your current context. "
    "Use query or family to locate an unknown operation, tool for its exact contract, "
    "then call it; this directory does not execute work or report task progress. "
    "With no arguments, list Skills, granted Host tool families and configured MCP servers."
)
assert AGENT_LOCAL_TOOLS == frozenset(
    {DIRECTORY_TOOL, LOAD_SKILL_TOOL, INVOKE_TOOL, "record_task_review"}
)

DIRECTORY_SCHEMA = "replaylab-capability-directory/v1"
SKILL_SCHEMA = "replaylab-skill/v1"

MAX_SKILL_INSTRUCTION_CHARS = 160_000
# One tool result has to stay small enough that the client keeps it in the
# conversation. A vendor client that spills a large result to a file (Cursor does)
# and then cannot read that file back from its own sandbox turns a loaded Skill
# into "permission denied" -- which is exactly what happened: the procedure was
# delivered and never seen. So a long Skill is handed over in sections: the part
# that comes before the first heading plus a list of the sections, and each
# section on request. A single large section is itself cursor-paged; otherwise
# the section lookup merely moves the same spill/denied-read failure one call
# later.
MAX_SKILL_INLINE_BYTES = 24_000
# Leave room for the bounded successful-read receipt appended after paging.
_SKILL_TEXT_INLINE_BYTES = MAX_SKILL_INLINE_BYTES - 1024
# Compatibility name for callers that imported the first implementation. The
# enforced unit is bytes, not Python characters.
MAX_SKILL_INLINE_CHARS = MAX_SKILL_INLINE_BYTES
MAX_SKILL_REFERENCE_BYTES = 64 * 1024
_SECTION_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)


def compact_direct_tool_description(description: str, *, limit: int = 640) -> str:
    """Keep the purpose in a direct tool list; exact details stay discoverable."""

    first = re.split(r"\n\s*\n|\n\s*Args\s*:", str(description or ""), maxsplit=1)[0]
    return " ".join(first.split())[:limit].rstrip()


def load_validated_skill_catalog(
    skill_roots: Iterable[Path | str],
    *,
    disabled_skills: Iterable[str] = (),
) -> CatalogView:
    """Load available Skills for a lazy route; report unavailable roots.

    Native clients decide their allow-list before the stdio proxy starts, so
    all routes use the same catalog view. A missing optional root is a
    diagnostic, not a reason to block an unrelated conversation.
    """

    roots = tuple(
        Path(str(item)).expanduser()
        for item in skill_roots
        if str(item or "").strip()
    )
    invalid = []
    valid_roots = []
    for root in roots:
        try:
            valid = root.is_dir()
        except OSError:
            valid = False
        if not valid:
            invalid.append(str(root))
        else:
            valid_roots.append(root)
    try:
        catalog = CatalogView.load(skill_roots=valid_roots)
        catalog.diagnostics.extend(
            f"Skill root unavailable: {root}" for root in invalid[:8]
        )
        disabled = {
            str(item).strip()
            for item in disabled_skills
            if str(item or "").strip()
        }
        for skill_id in list(catalog.skills):
            if skill_id in disabled:
                del catalog.skills[skill_id]
        return catalog
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(
            "Bread Skill catalog could not be loaded from the configured roots"
        ) from exc


def split_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """``(preamble, [(heading, body)])`` for a markdown Skill."""

    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return text, []
    preamble = text[: matches[0].start()]
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(2).strip(), text[match.start(): end].rstrip()))
    return preamble, sections


def _section_key(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.strip().lower()).strip("-")[:64]


def _instruction_page(
    base: Mapping[str, Any],
    text: str,
    offset: int,
    *,
    next_call: Any,
) -> dict[str, Any]:
    """Fit one lossless text page inside the client-safe encoded result bound."""

    def candidate(end: int) -> dict[str, Any]:
        result = dict(base)
        result.update({
            "instructions": text[offset:end],
            "offset": offset,
            "nextOffset": end,
            "hasMore": end < len(text),
        })
        if end < len(text):
            result["next"] = next_call(end)
        return result

    whole = candidate(len(text))
    if len(json.dumps(whole, ensure_ascii=False).encode("utf-8")) <= _SKILL_TEXT_INLINE_BYTES:
        return whole

    low = offset
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(candidate(middle), ensure_ascii=False).encode("utf-8")) <= _SKILL_TEXT_INLINE_BYTES:
            low = middle
        else:
            high = middle - 1
    if low <= offset:
        # Skill metadata is expected to be tiny compared with the text. Fail
        # explicitly rather than returning an oversized payload that a client
        # spills to an unreadable file.
        return {
            "schema": SKILL_SCHEMA,
            "id": str(base.get("id", "")),
            "error": "Skill page metadata exceeds the inline result limit",
            "offset": offset,
            "hasMore": True,
        }
    return candidate(low)

# Families exist so the directory stays readable: 140+ granted tool ids would
# cost more context than the Skills they belong to. Order matters; the first
# matching family wins.
_TOOL_FAMILIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "unity-restoration",
        "Connect Unity, reuse organized Shader/CBuffer inputs, import, inspect, repair and compare the actual render.",
        ("unity",),
    ),
    (
        "task-review",
        "Record the Agent's evidence-based assessment of the whole requested outcome.",
        ("record_task_review",),
    ),
    (
        "asset-readers",
        "Run a proved engine-specific asset reader and retain its content-bound Host receipt.",
        ("uex_material_shader_maps",),
    ),
    (
        "capture-naming",
        "Publish or roll back user-visible texture, CBuffer and symbol names for the current capture.",
        ("resource_names", "symbol_alias", "cbuffer_alias"),
    ),
    (
        "shader-workspace",
        "Reconstruct, organize, compile, ABI-check, edit and replace one capture-bound shader.",
        ("shader_workspace", "shader_source", "decompile", "organization", "live_edit", "restructure",
         "record_shader_resource_naming_review", "shader_semantic_overview"),
    ),
    (
        "unpacked-assets",
        "Index, search and resolve the project's unpacked game assets, and remember the resolution.",
        ("asset", "unpacked", "symbol_match", "dictionary"),
    ),
    (
        "shader-inspection",
        "Read a shader's bytecode, disassembly, bindings, algorithm graph and flow.",
        ("shader", "dxil", "dxbc", "spirv", "cbuffer", "constant_buffer"),
    ),
    (
        "capture-analysis",
        "Events, draws, passes, pipeline state, pixel history, frame flow and counters.",
        ("event", "draw", "frame", "pass", "pixel", "pipeline", "counter", "lighting", "mesh"),
    ),
    (
        "resources",
        "Textures, buffers, resource metadata and validated exports.",
        ("texture", "buffer", "resource", "export"),
    ),
    (
        "materials",
        "Material sessions, shared CBuffer aliases and material publication.",
        ("material",),
    ),
    (
        "project",
        "Active project state, capture status, RenderDoc context and task reporting.",
        ("project", "capture", "context", "report", "ping"),
    ),
)

# A vendor initially sees only these family summaries. Keep every expanded
# family deliberately small: large 30-40 tool buckets caused models to keep
# reopening the catalog because the useful operation was lost in a wall of
# near-identical names. Eight leaves room for the family description and effect
# metadata while staying below the API guidance of fewer than ten functions per
# namespace. New Host tools are still safe: an overflowing family is split into
# deterministic numbered parts by ``group_tools`` instead of growing without
# bound.
MAX_TOOLS_PER_FAMILY = 8


def _family_of(name: str) -> str:
    lowered = str(name or "").lower()
    for family, _summary, needles in _TOOL_FAMILIES:
        if any(needle in lowered for needle in needles):
            return family
    return "other"


def group_tools(tool_names: Iterable[str]) -> dict[str, list[str]]:
    """Which family each granted tool belongs to, families in directory order."""

    grouped: dict[str, list[str]] = {}
    for name in sorted({str(item or "").strip()
                        for item in tool_names if str(item or "").strip()}):
        grouped.setdefault(_family_of(name), []).append(name)
    order = [family for family, _summary, _needles in _TOOL_FAMILIES] + ["other"]
    bounded: dict[str, list[str]] = {}
    for family in order:
        names = grouped.get(family) or []
        if len(names) <= MAX_TOOLS_PER_FAMILY:
            if names:
                bounded[family] = names
            continue
        for offset in range(0, len(names), MAX_TOOLS_PER_FAMILY):
            part = offset // MAX_TOOLS_PER_FAMILY + 1
            bounded["%s-%d" % (family, part)] = names[
                offset: offset + MAX_TOOLS_PER_FAMILY
            ]
    return bounded


def _family_summary(family: str) -> str:
    summaries = {name: summary for name, summary, _ in _TOOL_FAMILIES}
    if family in summaries:
        return summaries[family]
    base, separator, part = family.rpartition("-")
    if separator and part.isdigit() and base in summaries:
        return "%s Part %s of this namespace." % (summaries[base], part)
    return "Tools that belong to no other family."


def _tool_effect_labels(name: str) -> list[str]:
    effects = sorted(tool_effects(name))
    return effects or ["read-only"]


def _tool_effect_metadata(name: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {"effects": _tool_effect_labels(name)}
    if tool_has_parameterized_effects(name):
        metadata["effectsDependOnArguments"] = True
        metadata["effectVariants"] = [
            sorted(variant) or ["read-only"]
            for variant in tool_effect_variants(name)
        ]
        if name == "finish_shader_workspace_organization":
            metadata["baseEffects"] = ["workspace"]
            metadata["effectArguments"] = {
                "apply_shared_resource / applySharedResource": {
                    "effect": "replay", "default": False,
                },
                "validate_for_apply / validateForApply": {
                    "effect": "replay", "default": None,
                    "condition": "Null or omitted validates final delivery; checkpoint only saves the proposal.",
                },
                "show_in_workspace / showInWorkspace": {
                    "effect": "ui", "default": False,
                },
                "start_live_edit / startLiveEdit": {
                    "effect": "replay", "default": False,
                    "independentEffect": True,
                    "condition": "Requires final validation; live editing starts only after apply_shared_resource applied the replacement.",
                },
            }
        elif name == "decompile_shader_workspace":
            metadata["baseEffects"] = ["workspace"]
            metadata["effectArguments"] = {
                "validate_for_apply / validateForApply": {
                    "effect": "replay", "default": True,
                    "condition": "Reference-only reconstruction never validates or applies.",
                },
                "reference_only / referenceOnly": {"default": False},
                "apply_shared_resource / applySharedResource": {"effect": "replay", "default": False},
            }
        else:
            metadata["effectArguments"] = {
                "publish_to_ui / publishToUi": {"effect": "ui", "default": True},
                "persist_project / persistProject": {
                    "effect": "project",
                    "default": False,
                },
            }
    return metadata


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _argument_schema_errors(
    schema: Mapping[str, Any], value: Any, *, path: str = "arguments", depth: int = 0
) -> list[str]:
    """Small fail-fast validator for a deferred Host tool contract.

    The upstream MCP Host remains the full JSON-Schema authority. This catches
    the expensive and common mistakes before a nested invocation is started:
    missing required fields, misspelled extra fields and basic JSON type/enum
    mismatches. Recursion is deliberately bounded so an untrusted third-party
    schema cannot turn catalog validation into unbounded work.
    """

    if depth > 8 or not isinstance(schema, Mapping):
        return []
    expected = schema.get("type")
    expected_types = (
        [expected] if isinstance(expected, str)
        else [item for item in (expected or []) if isinstance(item, str)]
    )
    if expected_types and not any(_matches_json_type(value, item) for item in expected_types):
        labels = ["a JSON boolean" if item == "boolean" else item for item in expected_types]
        return ["%s must be %s" % (path, " or ".join(labels))]
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return ["%s must be one of %s" % (path, enum[:12])]
    if not isinstance(value, Mapping):
        return []

    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    errors: list[str] = []
    for name in schema.get("required") or []:
        if isinstance(name, str) and name not in value:
            errors.append("missing required parameter %r" % name)
    if schema.get("additionalProperties") is False:
        for name in value:
            if name not in properties:
                errors.append("unexpected parameter %r" % str(name))
    for name, child in properties.items():
        if name in value and isinstance(child, Mapping):
            errors.extend(_argument_schema_errors(
                child, value[name], path="%s.%s" % (path, name), depth=depth + 1
            ))
        if len(errors) >= 16:
            break
    return errors[:16]


def tool_families(tool_names: Iterable[str]) -> list[dict[str, Any]]:
    """Group granted Host tools into a compact, readable directory section.

    Each family lists **every** granted tool name. Descriptions stay behind
    ``family=<name>`` / ``query=<text>`` so the directory itself stays a
    names-first catalog instead of a truncated guess list.
    """

    families: list[dict[str, Any]] = []
    for family, names in group_tools(tool_names).items():
        families.append(
            {
                "family": family,
                "summary": _family_summary(family),
                "toolCount": len(names),
                "toolNames": names,
                "examples": names[:8],
                "effects": sorted(
                    {effect for tool in names for effect in _tool_effect_labels(tool)}
                ),
            }
        )
    return families


def _skill_entry(skill: SkillInfo, *, loaded: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": skill.id,
        "title": skill.title,
        "description": skill.description,
        "category": skill.category,
        "loaded": loaded,
    }
    if getattr(skill, "when", ""):
        entry["whenToUse"] = skill.when
    if getattr(skill, "version", ""):
        entry["version"] = skill.version
    return entry


class SkillGateway:
    """Directory + loader shared by the task MCP proxy and the API route."""

    def __init__(
        self,
        catalog: CatalogView,
        *,
        tool_names: Sequence[str] = (),
        effects: Sequence[str] = (),
        mcp_servers: Sequence[Mapping[str, Any]] = (),
        user_tool_names: Sequence[str] = (),
        tool_descriptions: Mapping[str, str] | None = None,
        tool_schemas: Mapping[str, Mapping[str, Any]] | None = None,
        asset_tools: Sequence[Mapping[str, Any]] = (),
        load_mechanism: str = LOAD_SKILL_TOOL,
        on_skill_read: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._catalog = catalog
        self._tool_names = tuple(sorted({str(name) for name in tool_names if str(name or "").strip()}))
        self._effects = tuple(sorted({str(item) for item in effects if str(item or "").strip()}))
        self._mcp_servers = tuple(dict(server) for server in mcp_servers)
        self._user_tool_names = tuple(sorted({str(name) for name in user_tool_names if str(name or "").strip()}))
        self._external_tools: dict[str, dict[str, Any]] = {}
        self._tool_descriptions = {
            str(name): str(text or "") for name, text in (tool_descriptions or {}).items()}
        self._tool_schemas = {
            str(name): dict(schema)
            for name, schema in (tool_schemas or {}).items()
            if isinstance(schema, Mapping)
        }
        self._asset_tools = tuple(dict(item) for item in asset_tools)
        self._load_mechanism = load_mechanism or LOAD_SKILL_TOOL
        self._loaded: list[str] = []
        self._skill_read_facts: list[dict[str, Any]] = []
        self._on_skill_read = on_skill_read
        self._loaded_tool_contracts: set[str] = set()
        self._tool_availability_diagnostics: tuple[dict[str, str], ...] = ()

    # ------------------------------------------------------------- state
    @property
    def loaded_skill_ids(self) -> tuple[str, ...]:
        return tuple(self._loaded)

    def tool_contract_loaded(self, name: str) -> bool:
        """Whether the Agent explicitly expanded this exact Host contract."""

        return str(name or "").strip() in self._loaded_tool_contracts

    def tool_argument_errors(
        self, name: str, arguments: Mapping[str, Any]
    ) -> tuple[str, ...]:
        """Preflight one loaded nested call against its deferred schema."""

        wanted = str(name or "").strip()
        schema = self._tool_schemas.get(wanted)
        if not isinstance(schema, Mapping):
            return ()
        return tuple(_argument_schema_errors(schema, arguments))

    def describe_tools(self, descriptions: Mapping[str, str]) -> None:
        """Fill in the granted tools' own descriptions once they are known.

        The task proxy builds this gateway before it has spoken to the Host, so the
        descriptions arrive one step later than the names.
        """

        for name, text in (descriptions or {}).items():
            key = str(name or "").strip()
            if key:
                self._tool_descriptions[key] = str(text or "")

    def register_tools(self, tools: Mapping[str, Mapping[str, Any]]) -> None:
        """Attach the exact Host descriptions and schemas discovered by tools/list.

        Names are fixed by the task grant.  Metadata is accepted only for one of
        those names, so a stale or foreign tools/list response can never make the
        directory advertise a callable that the proxy cannot dispatch.
        """

        granted = set(self._tool_names)
        for raw_name, raw in (tools or {}).items():
            name = str(raw_name or "").strip()
            if name not in granted or not isinstance(raw, Mapping):
                continue
            self._tool_descriptions[name] = str(raw.get("description") or "")
            schema = raw.get("inputSchema", raw.get("input_schema"))
            if isinstance(schema, Mapping):
                self._tool_schemas[name] = dict(schema)

    def register_external_tools(
        self, tools: Mapping[str, Mapping[str, Any]], *, failed_servers: Mapping[str, str] | None = None,
    ) -> None:
        """Publish actual attached external contracts separately from Host grants."""

        for name, contract in tools.items():
            if name in self._tool_names:
                continue
            declared = {key: contract[key] for key in
                        ("name", "family", "server", "authority", "description", "inputSchema")
                        if key in contract}
            declared.update(name=name, family="external-mcp", authority="user-configured-mcp-server")
            self._external_tools[name] = declared
            schema = declared.get("inputSchema")
            if isinstance(schema, Mapping):
                self._tool_schemas[name] = dict(schema)
        self._user_tool_names = tuple(sorted(set(self._user_tool_names) | set(self._external_tools)))
        if failed_servers:
            self._mcp_servers = tuple(
                dict(server, enabled=False, note=failed_servers[str(server.get("id", ""))])
                if str(server.get("id", "")) in failed_servers else server
                for server in self._mcp_servers
            )

    def register_owned_output_reader(self, description: str, schema: Mapping[str, Any]) -> None:
        """Expose the narrow run-owned pager under the existing evidence reader."""
        name = "read_evidence_file"
        self._tool_names = tuple(sorted(set(self._tool_names) | {name}))
        self._tool_descriptions[name] = str(description)
        self._tool_schemas[name] = dict(schema)

    def retain_host_tools(
        self, names: Iterable[str], *, diagnostics: Sequence[Mapping[str, str]] = (),
    ) -> None:
        """Reflect the actual per-tool runtime grant without choosing a workflow."""

        available = frozenset(str(name) for name in names)
        self._tool_names = tuple(name for name in self._tool_names if name in available)
        self._loaded_tool_contracts.intersection_update(available)
        self._tool_availability_diagnostics = tuple(
            {"name": str(item.get("name", ""))[:96],
             "reason": str(item.get("reason", ""))[:96]}
            for item in diagnostics[:64]
        )

    def mark_loaded(self, skill_id: str) -> None:
        skill_id = str(skill_id or "").strip()
        if skill_id and skill_id not in self._loaded:
            self._loaded.append(skill_id)

    # --------------------------------------------------------- directory
    def directory(self, *, direct_tools: Iterable[str] = ()) -> dict[str, Any]:
        skills = [
            _skill_entry(skill, loaded=skill.id in self._loaded)
            for skill in sorted(self._catalog.skills.values(), key=lambda item: item.id)
        ]
        payload: dict[str, Any] = {
            "schema": DIRECTORY_SCHEMA,
            "skillUse": (
                "Load the matching domain Skill before substantive capture, Shader, export "
                "or asset work unless its complete instructions are already in your own "
                "current context. loadedSkills and loaded flags describe shared delivery "
                "history, not what this actor has received; reload absent or incomplete "
                "instructions. Use its task guidance and acceptance "
                "criteria, then choose supported tools and self-review the requested outcome. "
                "The main Agent uses record_task_review when available to assess the whole "
                "request; reuse specialized reviews and existing evidence, repair substantive "
                "gaps and stop when the requested outcome is met. Accepting a review "
                "record does not mean the Host judged its semantic accuracy. "
                "General conversation needs no Skill. The attached capture is context, "
                "not a request to analyse it."
            ),
            "expandWith": (
                "Use operations already in your tool list directly with their supplied schemas. "
                "%s() lists other toolNames; family=<name> or query=<text> finds a missing "
                "operation, and tool=<name> returns an unfamiliar schema. "
                "Call other granted tools through %s. Known arguments need no lookup; "
                "reuse contracts already in your current context."
                % (DIRECTORY_TOOL, INVOKE_TOOL)
            ),
            "directToolCount": len(set(direct_tools) & set(self._tool_names)),
            "skills": skills,
            "loadedSkills": list(self._loaded),
            "skillReadFacts": [dict(item) for item in self._skill_read_facts[-8:]],
            "loadWith": self._load_mechanism,
            "toolFamilies": tool_families(self._tool_names),
            "grantedToolCount": len(self._tool_names),
            "effectClasses": list(self._effects) or ["read-only"],
            "hostEffectGrant": {
                "allowed": list(self._effects) or ["read-only"],
                "meaning": (
                    "The Host enforces these side-effect classes on the selected nested "
                    "tool. They do not grant shell, file, web or delegation tools from the vendor."
                ),
            },
            "vendorGenericToolPolicy": {
                "grantedByHostEffects": False,
                "availability": "provider-dependent",
                "reason": (
                    "Native provider tools follow that provider's verified runtime permissions, "
                    "separately from this Bread MCP grant. Codex may expose read-only native "
                    "access within verified workspace roots; capture, Shader and project "
                    "changes use the scoped Bread Host tools."
                ),
                "independentOfHostEffectGrant": True,
            },
            "invokeWith": INVOKE_TOOL,
            "loadedToolContracts": sorted(self._loaded_tool_contracts),
        }
        if self._user_tool_names:
            payload["userMcpTools"] = list(self._user_tool_names[:64])
            payload["toolFamilies"].append({
                "family": "external-mcp", "summary": "Attached user-configured MCP operations",
                "toolCount": len(self._user_tool_names), "toolNames": list(self._user_tool_names),
                "authority": "user-configured-mcp-server",
            })
        if self._tool_availability_diagnostics:
            payload["unavailableHostTools"] = list(self._tool_availability_diagnostics)
        if self._asset_tools:
            # Extractors the user registered. What each one can actually do was
            # discovered by running it, so this names them and says where the
            # proved invocations are, rather than restating a guess here.
            payload["assetTools"] = [
                {
                    "id": str(tool.get("id", "")),
                    "title": str(tool.get("title", "") or tool.get("id", "")),
                    "purpose": str(tool.get("description", "") or ""),
                    "enabled": bool(tool.get("enabled", True)),
                    "provedOperations": len([
                        item for item in (tool.get("interface") or {}).get("operations") or []
                        if item.get("proved")]),
                }
                for tool in self._asset_tools[:32]
            ]
            payload["assetToolsAreCalledBy"] = (
                "list_asset_tool_operations returns each proved invocation as a named "
                "operation with a typed inputSchema; run_asset_tool_operation calls one. "
                "An extractor with no proved operation has not been worked out yet -- "
                "discover_tool_interface probes it."
            )
        if self._mcp_servers:
            payload["mcpServers"] = [
                {
                    "id": str(server.get("id", "")),
                    "title": str(server.get("title", "") or server.get("id", "")),
                    "enabled": bool(server.get("enabled", True)),
                    "transport": str(server.get("transport", "stdio")),
                    "note": str(server.get("note", "") or ""),
                }
                for server in self._mcp_servers
            ]
        quarantine = self._catalog.quarantine_summary()
        quarantined = sorted(
            {
                str(getattr(item, "skill_id", "") or "")
                for item in getattr(quarantine, "diagnostics", ()) or ()
                if str(getattr(item, "skill_id", "") or "")
            }
        )
        if quarantined:
            payload["unavailableSkills"] = quarantined
        return payload

    def family(self, name: str, *, direct_tools: Iterable[str] = ()) -> dict[str, Any]:
        """Every granted tool in one family, with its own description.

        The directory groups tools so it stays readable; this is how the grouping
        stops being a wall. An Agent that suspects a tool exists can see the whole
        family rather than inferring it from eight examples.
        """

        wanted = str(name or "").strip().lower()
        direct = frozenset(direct_tools)
        if wanted == "external-mcp" and self._external_tools:
            return {
                "schema": DIRECTORY_SCHEMA, "family": wanted,
                "toolCount": len(self._external_tools),
                "tools": [{key: value for key, value in contract.items() if key != "inputSchema"}
                          for contract in self._external_tools.values()],
                "invokeWith": INVOKE_TOOL,
            }
        grouped = group_tools(self._tool_names)
        if wanted not in grouped:
            return {
                "schema": DIRECTORY_SCHEMA,
                "error": "no granted tool family is called %r" % wanted[:64],
                "families": list(grouped),
                "hint": "call %s with no arguments for the directory" % DIRECTORY_TOOL,
            }
        return {
            "schema": DIRECTORY_SCHEMA,
            "family": wanted,
            "summary": _family_summary(wanted),
            "toolCount": len(grouped[wanted]),
            "tools": [
                {
                    "name": tool,
                    "description": self._tool_descriptions.get(tool, "")[:240],
                    "contractLoaded": tool in self._loaded_tool_contracts,
                    "directlyAvailable": tool in direct,
                    **_tool_effect_metadata(tool),
                }
                for tool in grouped[wanted]
            ],
            "note": (
                "Call directlyAvailable operations by their own tool name using the schema "
                "already in your tool list. For other granted operations use %s. "
                "Inspect tool=<exact-name> only when its arguments are unfamiliar; "
                "reuse a contract already in your current context."
                % INVOKE_TOOL
            ),
            "detailsWith": "%s(tool=<exact-name>)" % DIRECTORY_TOOL,
            "searchWith": (
                "%s(query=<text>) searches every granted tool name and description "
                "when the family name is unknown" % DIRECTORY_TOOL
            ),
        }

    def search(self, query: str, *, direct_tools: Iterable[str] = ()) -> dict[str, Any]:
        """Search granted tool names and descriptions by free text."""

        needle = str(query or "").strip().lower()
        direct = frozenset(direct_tools)
        if not needle:
            return {
                "schema": DIRECTORY_SCHEMA,
                "error": "query must be a non-empty search string",
                "hint": (
                    "call %s() for the directory, %s(family=<name>) for one family, "
                    "or %s(query=<text>) to search tools"
                    % (DIRECTORY_TOOL, DIRECTORY_TOOL, DIRECTORY_TOOL)
                ),
                "families": list(group_tools(self._tool_names)),
            }
        tokens = [part for part in needle.replace("_", " ").split() if part]
        matches: list[dict[str, Any]] = []
        for name in sorted(set(self._tool_names) | set(self._external_tools)):
            external = self._external_tools.get(name)
            description = str((external or {}).get("description", self._tool_descriptions.get(name, "")) or "")
            hay = ("%s %s" % (name, description)).lower()
            if needle in hay or (tokens and all(part in hay for part in tokens)):
                matches.append({
                    "name": name,
                    "description": description[:240],
                    "family": "external-mcp" if external else _family_of(name),
                    "contractLoaded": name in self._loaded_tool_contracts,
                    "directlyAvailable": name in direct,
                    **({"server": external.get("server", ""), "authority": "user-configured-mcp-server"}
                       if external else _tool_effect_metadata(name)),
                })
            if len(matches) >= 64:
                break
        return {
            "schema": DIRECTORY_SCHEMA,
            "query": needle[:128],
            "matchCount": len(matches),
            "tools": matches,
            "grantedToolCount": len(self._tool_names),
            "next": (
                "Call directlyAvailable results directly with their supplied schemas. "
                "Use %s for other granted results. Inspect the chosen result with "
                "%s(tool=<exact-name>) only if its arguments are unfamiliar and absent "
                "from your current context. If nothing matched, try a shorter token or "
                "%s(family=...) from the directory."
                % (INVOKE_TOOL, DIRECTORY_TOOL, DIRECTORY_TOOL)
            ),
        }

    def tool(self, name: str, *, direct_tools: Iterable[str] = ()) -> dict[str, Any]:
        """Return one exact callable's full contract, and nothing else."""

        wanted = str(name or "").strip()
        if not wanted or wanted not in set(self._tool_names) | set(self._external_tools):
            return {
                "schema": DIRECTORY_SCHEMA,
                "error": "no granted Host or attached configured MCP tool is called %r" % wanted[:96],
                "hint": (
                    "Use %s(query=<text>) or %s(family=<name>) and copy an exact name."
                    % (DIRECTORY_TOOL, DIRECTORY_TOOL)
                ),
            }
        already_loaded = wanted in self._loaded_tool_contracts
        self._loaded_tool_contracts.add(wanted)
        direct = wanted in frozenset(direct_tools)
        return {
            "schema": DIRECTORY_SCHEMA,
            "alreadyLoaded": already_loaded,
            "directlyAvailable": direct,
            "tool": dict(self._external_tools[wanted]) if wanted in self._external_tools else {
                "name": wanted,
                "family": _family_of(wanted),
                "description": self._tool_descriptions.get(wanted, ""),
                "inputSchema": self._tool_schemas.get(
                    wanted, {"type": "object", "additionalProperties": True}
                ),
                **_tool_effect_metadata(wanted),
            },
            "invokeWith": wanted if direct else INVOKE_TOOL,
            "invokeArguments": "object matching tool.inputSchema" if direct else {
                "name": wanted,
                "arguments": "object matching tool.inputSchema",
            },
            "note": (
                "The Host supplies facts, operations and acceptance receipts. It does not "
                "choose whether or when this tool is useful; that decision belongs to the Agent. "
                "Use the supplied invokeWith route. A directly available tool already supplied "
                "its schema in your tool list. Reuse this contract while it remains in your "
                "current context; do not query it again before each call. alreadyLoaded "
                "describes shared delivery history, not another actor's current context."
            ),
        }

    # ------------------------------------------------------------- load
    def load(self, skill_id: str, section: str = "", offset: int = 0) -> dict[str, Any]:
        """Return guidance and record only text actually delivered successfully."""

        payload = self._load_payload(skill_id, section, offset)
        instructions = payload.get("instructions")
        if "error" not in payload and isinstance(instructions, str) and instructions:
            requested = str(payload["id"])
            skill = self._catalog.skills[requested]
            self.mark_loaded(requested)
            receipt = {
                "skillId": requested,
                "version": str(getattr(skill, "version", "") or "")[:32],
                "section": str(payload.get("section") or "entry")[:96],
                "offset": int(payload.get("offset", 0)),
                "deliveredChars": len(instructions),
                "instructionsSha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
                "entryComplete": not bool(section or offset or payload.get("sectioned")
                                           or payload.get("hasMore") or payload.get("truncated")),
            }
            payload["readReceipt"] = receipt
            self._skill_read_facts.append(receipt)
            self._skill_read_facts = self._skill_read_facts[-32:]
            if self._on_skill_read is not None:
                try:
                    self._on_skill_read(dict(receipt))
                except Exception:
                    # Observability never governs whether guidance is available.
                    pass
        payload["loadedSkills"] = list(self._loaded)
        return payload

    def _load_payload(self, skill_id: str, section: str = "", offset: int = 0) -> dict[str, Any]:
        requested = str(skill_id or "").strip()
        if not requested:
            return {
                "schema": SKILL_SCHEMA,
                "error": "skill_id is required",
                "availableSkills": [skill.id for skill in self._catalog.skills.values()],
            }
        skill = self._catalog.skills.get(requested)
        if skill is None:
            return {
                "schema": SKILL_SCHEMA,
                "error": "unknown Skill id: %s" % requested[:96],
                "availableSkills": [item.id for item in self._catalog.skills.values()],
                "hint": "call %s to see the directory" % DIRECTORY_TOOL,
            }
        already = requested in self._loaded
        instructions = skill.instructions.strip()
        truncated = False
        if len(instructions) > MAX_SKILL_INSTRUCTION_CHARS:
            instructions = instructions[:MAX_SKILL_INSTRUCTION_CHARS]
            truncated = True
        payload: dict[str, Any] = {
            "schema": SKILL_SCHEMA,
            "id": skill.id,
            "title": skill.title,
            "description": skill.description,
            "category": skill.category,
            "alreadyLoaded": already,
            "loadedSkills": list(self._loaded),
        }
        preamble, sections = split_sections(instructions)
        wanted_section = str(section or "").strip()
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            payload["error"] = "offset must be a non-negative integer"
            return payload
        if wanted_section.startswith("references/"):
            try:
                reference = _reference_path(skill.path, wanted_section)
                with reference.open("rb") as stream:
                    opened = os.fstat(stream.fileno())
                    checked = _reference_path(skill.path, wanted_section).stat()
                    if (not stat.S_ISREG(opened.st_mode)
                            or (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino)):
                        raise ValueError("Skill reference changed while it was opened")
                    raw = stream.read(MAX_SKILL_REFERENCE_BYTES + 1)
                if len(raw) > MAX_SKILL_REFERENCE_BYTES:
                    raise ValueError("Skill reference exceeds 64 KiB")
                body = raw.decode("utf-8", errors="strict")
            except (OSError, RuntimeError, ValueError) as error:
                payload["error"] = "Skill reference is unavailable: %s" % str(error)[:512]
                return payload
            payload["section"] = wanted_section
            payload["sectionChars"] = len(body)
            payload["reference"] = True
            if offset > len(body):
                payload["error"] = "offset is past the end of reference %r" % wanted_section[:96]
                return payload
            return _instruction_page(
                payload, body, offset,
                next_call=lambda next_offset: (
                    "%s(skill_id=%r, section=%r, offset=%d)"
                    % (self._load_mechanism, skill.id, wanted_section, next_offset)
                ),
            )
        references = _reference_files(skill.path)
        if references:
            payload["references"] = references
            payload["referenceLoads"] = [
                {"tool": self._load_mechanism,
                 "arguments": {"skill_id": skill.id, "section": name}}
                for name in references
            ]
            payload["referenceNote"] = "References and text pages are available on demand."
        if wanted_section:
            key = _section_key(wanted_section)
            match = next(((heading, body) for heading, body in sections
                          if _section_key(heading) == key or heading.strip() == wanted_section), None)
            if match is None:
                payload["error"] = "this Skill has no section %r" % wanted_section[:96]
                payload["sections"] = [
                    {"section": heading, "chars": len(body)} for heading, body in sections]
                return payload
            heading, body = match
            if offset > len(body):
                payload["error"] = "offset is past the end of section %r" % heading[:96]
                payload["section"] = heading
                payload["sectionChars"] = len(body)
                return payload
            payload["section"] = heading
            payload["sectionChars"] = len(body)
            return _instruction_page(
                payload,
                body,
                offset,
                next_call=lambda next_offset: (
                    "%s(skill_id=%r, section=%r, offset=%d)"
                    % (self._load_mechanism, skill.id, heading, next_offset)
                ),
            )
        fits_inline = len(encode({**payload, "instructions": instructions}).encode("utf-8")) <= _SKILL_TEXT_INLINE_BYTES
        if not sections and (not fits_inline or offset):
            if offset > len(instructions):
                payload["error"] = "offset is past the end of this Skill"
                payload["instructionChars"] = len(instructions)
                return payload
            payload["instructionChars"] = len(instructions)
            return _instruction_page(
                payload,
                instructions,
                offset,
                next_call=lambda next_offset: (
                    "%s(skill_id=%r, offset=%d)"
                    % (self._load_mechanism, skill.id, next_offset)
                ),
            )
        if offset:
            payload["error"] = "offset requires a section returned by a previous Skill load"
            return payload
        if not fits_inline and sections:
            # Head of the procedure now, the rest by name. Everything is still here:
            # nothing is dropped, and the section list says exactly what is left.
            head = [preamble.rstrip()]
            delivered: list[str] = []
            payload["sectioned"] = True
            payload["deliveredSections"] = delivered
            payload["sections"] = [
                {"section": heading, "chars": len(body), "delivered": False}
                for heading, body in sections]
            payload["next"] = (
                "This Skill is available in sections. Load any relevant section with "
                "%s(skill_id=%r, section=\"<section>\"); nextOffset pages long sections."
                % (self._load_mechanism, skill.id))
            for heading, body in sections:
                candidate_head = [*head, body]
                candidate_delivered = [*delivered, heading]
                candidate_payload = {
                    **payload,
                    "instructions": "\n\n".join(
                        part for part in candidate_head if part
                    ),
                    "deliveredSections": candidate_delivered,
                    "sections": [
                        {
                            "section": candidate_heading,
                            "chars": len(candidate_body),
                            "delivered": candidate_heading in candidate_delivered,
                        }
                        for candidate_heading, candidate_body in sections
                    ],
                }
                if len(json.dumps(candidate_payload, ensure_ascii=False).encode("utf-8")) > _SKILL_TEXT_INLINE_BYTES:
                    break
                head.append(body)
                delivered.append(heading)
            payload["instructions"] = "\n\n".join(part for part in head if part)
            payload["sections"] = [
                {"section": heading, "chars": len(body), "delivered": heading in delivered}
                for heading, body in sections]
        else:
            payload["instructions"] = instructions
        if truncated:
            payload["truncated"] = True
        return payload


def _reference_path(skill_path: Path | None, section: str) -> Path:
    """One local Markdown reference; neither links nor path aliases are reads."""

    parts = section.split("/")
    if (len(parts) != 2 or parts[0] != "references" or not parts[1]
            or any(character in section for character in ("\\", ":", "\x00"))
            or parts[1] in (".", "..") or Path(parts[1]).suffix.lower() != ".md"):
        raise ValueError("section must be references/<file>.md without path traversal")
    if skill_path is None:
        raise ValueError("Skill has no local reference directory")
    skill_file = Path(skill_path).absolute()
    root = skill_file.parent
    reference = root / "references" / parts[1]
    for item in (skill_file, reference, *reference.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError("Skill reference must not use symlinks or junctions")
    resolved = reference.resolve(strict=True)
    if resolved.parent != (root / "references").resolve(strict=True):
        raise ValueError("Skill reference is outside its reference directory")
    if not resolved.is_relative_to(root.resolve(strict=True)) or not resolved.is_file():
        raise ValueError("Skill reference must be a regular file inside its Skill")
    if resolved.stat().st_size > MAX_SKILL_REFERENCE_BYTES:
        raise ValueError("Skill reference exceeds 64 KiB")
    return resolved


def _reference_files(path: Path | None) -> list[str]:
    if path is None:
        return []
    directory = Path(path).parent / "references"
    try:
        if directory.is_symlink() or directory.is_junction() or not directory.is_dir():
            return []
        references = []
        for item in directory.iterdir():
            name = item.relative_to(directory.parent).as_posix()
            try:
                _reference_path(path, name)
            except (OSError, RuntimeError, ValueError):
                continue
            references.append(name)
        return sorted(references)[:32]
    except (OSError, RuntimeError, ValueError):
        return []


def directory_tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "family": {
                "type": "string",
                "description": (
                    "Optional. A tool family name from the directory: returns every "
                    "tool in it with its own description instead of the summary."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Optional. Free-text search across every granted tool name and "
                    "description (for example 'organization write' or 'asset resolve'). "
                    "Use when family is unknown or a needed tool seems missing."
                ),
            },
            "tool": {
                "type": "string",
                "description": (
                    "Optional exact tool name returned by the directory, family or query. "
                    "Returns only that tool's full description and inputSchema."
                ),
            },
        },
        "additionalProperties": False,
    }


def load_tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": "Exact Skill id from list_bread_capabilities.",
            },
            "section": {
                "type": "string",
                "description": (
                    "Optional. A section heading or references/<file>.md from a previous "
                    "load of this Skill. Returns that section or reference's text; a large "
                    "result returns a lossless cursor page."
                ),
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": (
                    "Character cursor from a previous section page's nextOffset. "
                    "Use only with the exact same skill_id and section until hasMore=false."
                ),
            },
        },
        "required": ["skill_id"],
        "additionalProperties": False,
    }


def invoke_tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Exact granted Host tool name. Use list_bread_capabilities "
                    "to discover an unfamiliar name or schema."
                ),
            },
            "arguments": {
                "type": "object",
                "description": "Arguments matching the Host tool's inputSchema.",
            },
        },
        "required": ["name", "arguments"],
        "additionalProperties": False,
    }


def encode(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "compact_direct_tool_description",
    "AGENT_LOCAL_TOOLS",
    "DIRECTORY_SCHEMA",
    "DIRECTORY_TOOL_DESCRIPTION",
    "DIRECTORY_TOOL",
    "INVOKE_TOOL",
    "LOAD_SKILL_TOOL",
    "MAX_SKILL_INLINE_CHARS",
    "MAX_SKILL_INLINE_BYTES",
    "MAX_TOOLS_PER_FAMILY",
    "split_sections",
    "SKILL_SCHEMA",
    "SkillGateway",
    "directory_tool_schema",
    "encode",
    "load_tool_schema",
    "invoke_tool_schema",
    "load_validated_skill_catalog",
    "group_tools",
    "tool_families",
]
