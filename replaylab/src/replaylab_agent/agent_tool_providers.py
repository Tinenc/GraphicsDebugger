"""External tools ReplayLab can call, and the interfaces it discovered for them.

Settings edits this list and the runtime attaches what it holds; the Host reads the
same file when it needs a tool to read an asset. Both sides validate identically —
``replaylab/tests/test_tool_provider_contract.py`` holds them to that — because a
tool the panel accepts and the Host drops is a tool the user believes in and the AI
never calls.

One idea, not two. An extractor like uex is a program that speaks MCP *and* has a
command line; registering it twice — once as an "MCP server" for the Agent, once as
an "asset reader" for the Host — was two mental models for one thing, and the second
one made us invent a private protocol nobody else implements.

A tool here has three parts:

* a **connection**: ``mcp-stdio``, ``mcp-http`` or ``cli``. That is the only thing a
  person is asked for — a path, or a URL.
* **roles** it may be used for. ``asset-reader`` is the one the Host calls itself;
  ``general`` means its tools are offered to the Agent during a task.
* an **interface**, which is *discovered and written back here*, never hand-written.
  For MCP that is ``tools/list`` — a typed, self-describing answer, which is why MCP
  is the preferred connection. For a plain command line it is the invocation that was
  observed to work against a real asset, cached so the next task starts from the answer.

An operation that has not been proved is not an operation: a shape somebody guessed
reports "unknown" for every asset and nobody ever learns why.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .bundled_paths import expand_tool_path, fold_tool_path

SCHEMA = "replaylab-tools/v1"
MAX_TOOLS = 32
MAX_OPERATIONS = 16
MAX_ARGS = 64
MAX_ENV = 32
MAX_HEADERS = 16
MAX_TEXT = 512

CONNECTION_KINDS = ("mcp-stdio", "mcp-http", "cli")
ROLES = ("asset-reader", "general")
# Argv slots the Host fills itself: the program, the run's output directory, the
# file an asset-reader is describing, and the uex facts derived from the active
# project's unpacked root (see mcp_host tool_runner.HOST_SLOTS). None is a
# declared parameter, and none is ever supplied by a caller.
HOST_SUPPLIED_SLOTS = frozenset(("program", "outputDir", "path", "assetRoot",
                                 "uexConfig", "uexProfile", "uexPath"))

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")


class ToolConfigError(ValueError):
    """A registration that cannot be trusted to run."""


def default_tools_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    # Never the shared temp directory: on Linux/macOS that put the task event
    # stream, the log and the tool settings under a world-readable /tmp that any
    # local user could pre-plant and that vanished on reboot. XDG data home is the
    # per-user equivalent of LOCALAPPDATA, which the rest of the product uses.
    root = Path(local).expanduser() if local else Path.home() / ".local" / "share"
    return root / "ReplayLab" / "config" / "tools.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any, label: str, *, limit: int = MAX_TEXT, required: bool = False) -> str:
    text = str(value or "").strip()
    if "\0" in text or "\n" in text or "\r" in text:
        raise ToolConfigError(f"{label} must be a single line")
    if len(text) > limit:
        raise ToolConfigError(f"{label} is longer than {limit} characters")
    if required and not text:
        raise ToolConfigError(f"{label} is required")
    return text


def slug(value: str, fallback: str = "tool") -> str:
    """A stable id from a display name, so nobody is asked to invent one."""

    text = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower()).strip("-.")
    return (text or fallback)[:64]


def validate_connection(raw: Mapping[str, Any]) -> dict[str, Any]:
    kind = _text(raw.get("kind"), "connection kind", limit=16, required=True).lower()
    if kind not in CONNECTION_KINDS:
        raise ToolConfigError("connection kind must be one of: " + ", ".join(CONNECTION_KINDS))
    connection: dict[str, Any] = {"kind": kind}
    if kind == "mcp-http":
        url = _text(raw.get("url"), "server URL", limit=1024, required=True)
        if not url.lower().startswith(("https://", "http://")):
            raise ToolConfigError("server URL must start with https:// or http://")
        # Plain HTTP would put a bearer token on the wire in clear text; loopback is
        # the one place that cannot leave the machine.
        if url.lower().startswith("http://"):
            host = url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
            if host not in {"localhost", "127.0.0.1", "[::1]", "::1"}:
                raise ToolConfigError("http:// is only allowed for localhost; use https://")
        connection["url"] = url
        connection["bearerEnv"] = _text(raw.get("bearerEnv"), "bearer token variable", limit=128)
        if connection["bearerEnv"] and not _ENV_NAME_RE.fullmatch(connection["bearerEnv"]):
            raise ToolConfigError("bearer token variable is not a valid environment name")
        headers: dict[str, str] = {}
        raw_headers = raw.get("headers") or {}
        if not isinstance(raw_headers, Mapping):
            raise ToolConfigError("headers must be an object of name/value pairs")
        for name, value in list(raw_headers.items())[:MAX_HEADERS]:
            key = str(name or "").strip()
            if not _HEADER_NAME_RE.fullmatch(key):
                raise ToolConfigError(f"invalid header name: {key[:64]}")
            headers[key] = _text(value, f"header {key}", limit=1024)
        connection["headers"] = headers
        connection["headerEnv"] = {}
        raw_header_env = raw.get("headerEnv") or {}
        if isinstance(raw_header_env, Mapping):
            for name, value in list(raw_header_env.items())[:MAX_HEADERS]:
                key = str(name or "").strip()
                variable = str(value or "").strip()
                if not _HEADER_NAME_RE.fullmatch(key) or not _ENV_NAME_RE.fullmatch(variable):
                    raise ToolConfigError(f"invalid header-from-environment entry: {key[:64]}")
                connection["headerEnv"][key] = variable
        return connection

    connection["command"] = _text(raw.get("command"), "command", limit=1024, required=True)
    arguments = raw.get("args") or []
    if not isinstance(arguments, (list, tuple)) or len(arguments) > MAX_ARGS:
        raise ToolConfigError("args must be a short list of strings")
    connection["args"] = [_text(item, "argument", limit=1024) for item in arguments]
    environment = raw.get("env") or {}
    if not isinstance(environment, Mapping):
        raise ToolConfigError("env must be an object of NAME=value pairs")
    if len(environment) > MAX_ENV:
        raise ToolConfigError(f"at most {MAX_ENV} environment values are supported")
    resolved: dict[str, str] = {}
    for name, value in environment.items():
        key = str(name or "").strip()
        if not _ENV_NAME_RE.fullmatch(key):
            raise ToolConfigError(f"invalid environment name: {key[:64]}")
        resolved[key] = _text(value, f"environment value {key}", limit=4096)
    connection["env"] = resolved
    return connection


MAX_SCHEMA_PROPERTIES = 24
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]{0,63})\}")
RESULT_KINDS = ("stdout-text", "files-in-output-dir", "exit-code")


def validate_input_schema(raw: Any) -> dict[str, Any]:
    """A JSON Schema object describing what one operation takes.

    Deliberately the same shape MCP uses for ``inputSchema``: an Agent that can call an
    MCP tool can call this without learning a second convention, and the Host can
    synthesise it into a real tool. Only the subset that survives being turned into
    command-line arguments is accepted -- an object of flat, typed properties -- because
    anything richer would have to be flattened by guesswork at call time.
    """

    if raw is None:
        return {"type": "object", "properties": {}, "required": []}
    if not isinstance(raw, Mapping) or raw.get("type", "object") != "object":
        raise ToolConfigError("inputSchema must be a JSON Schema object")
    properties = raw.get("properties") or {}
    if not isinstance(properties, Mapping) or len(properties) > MAX_SCHEMA_PROPERTIES:
        raise ToolConfigError(
            f"inputSchema must declare at most {MAX_SCHEMA_PROPERTIES} properties")
    resolved: dict[str, Any] = {}
    for name, definition in properties.items():
        key = str(name or "").strip()
        if not _ENV_NAME_RE.fullmatch(key):
            raise ToolConfigError(f"invalid parameter name: {key[:64]}")
        if not isinstance(definition, Mapping):
            raise ToolConfigError(f"parameter {key} must be an object")
        kind = str(definition.get("type", "string") or "string")
        if kind not in {"string", "integer", "number", "boolean"}:
            raise ToolConfigError(
                f"parameter {key} must be a string, integer, number or boolean")
        entry: dict[str, Any] = {"type": kind}
        if definition.get("description"):
            entry["description"] = _text(definition["description"],
                                         f"parameter {key} description")
        choices = definition.get("enum")
        if choices is not None:
            if not isinstance(choices, (list, tuple)) or not 1 <= len(choices) <= 32:
                raise ToolConfigError(f"parameter {key} enum must be a short list")
            entry["enum"] = [_text(item, f"parameter {key} choice", limit=256)
                             for item in choices]
        if "default" in definition and not isinstance(definition["default"], (dict, list)):
            entry["default"] = definition["default"]
        resolved[key] = entry
    required = raw.get("required") or []
    if not isinstance(required, (list, tuple)) or len(required) > MAX_SCHEMA_PROPERTIES:
        raise ToolConfigError("inputSchema required must be a short list of names")
    for name in required:
        if str(name) not in resolved:
            raise ToolConfigError(f"required parameter {str(name)[:64]} is not declared")
    return {"type": "object", "properties": resolved,
            "required": [str(name) for name in required]}


def validate_result(raw: Any) -> dict[str, Any]:
    """How to tell whether one invocation worked, and where its output went."""

    record = raw if isinstance(raw, Mapping) else {}
    kind = _text(record.get("kind") or "stdout-text", "result kind", limit=32).lower()
    if kind not in RESULT_KINDS:
        raise ToolConfigError("result kind must be one of: " + ", ".join(RESULT_KINDS))
    success = record.get("successWhen") if isinstance(record.get("successWhen"), Mapping) else {}
    exit_code = success.get("exitCode", 0)
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ToolConfigError("successWhen.exitCode must be an integer")
    minimum = success.get("minFiles", 0)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise ToolConfigError("successWhen.minFiles must be a non-negative integer")
    return {"kind": kind,
            "successWhen": {"exitCode": int(exit_code), "minFiles": int(minimum)}}


def operation_placeholders(argv: Any) -> list[str]:
    """Which ``{name}`` slots an argv template expects to be filled."""

    names: list[str] = []
    for entry in list(argv or []):
        for name in _PLACEHOLDER_RE.findall(str(entry)):
            if name not in names:
                names.append(name)
    return names


def validate_operation(raw: Mapping[str, Any]) -> dict[str, Any]:
    """One thing a tool was observed to do."""

    role = _text(raw.get("role") or "asset-reader", "operation role", limit=32).lower()
    if role not in ROLES:
        raise ToolConfigError("operation role must be one of: " + ", ".join(ROLES))
    kind = _text(raw.get("kind"), "operation kind", limit=16, required=True).lower()
    if kind not in {"mcp", "cli"}:
        raise ToolConfigError("operation kind must be 'mcp' or 'cli'")
    operation: dict[str, Any] = {"role": role, "kind": kind}
    if kind == "mcp":
        operation["name"] = _text(raw.get("name"), "MCP tool name", limit=128, required=True)
        operation["pathArgument"] = _text(raw.get("pathArgument") or "path",
                                          "path argument name", limit=64)
    else:
        argv = raw.get("argv") or []
        if not isinstance(argv, (list, tuple)) or not argv or len(argv) > MAX_ARGS:
            raise ToolConfigError("argv must be a short non-empty list")
        operation["argv"] = [_text(item, "argv entry", limit=1024) for item in argv]
        timeout = raw.get("timeoutSeconds", 20)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or not 1 <= float(timeout) <= 300:
            raise ToolConfigError("timeoutSeconds must be between 1 and 300")
        operation["timeoutSeconds"] = float(timeout)
    patterns = raw.get("patterns") or []
    if not isinstance(patterns, (list, tuple)) or len(patterns) > 32:
        raise ToolConfigError("patterns must be a short list of globs")
    operation["patterns"] = [_text(item, "pattern", limit=256) for item in patterns]
    # Optional routing metadata. Missing fields retain legacy probing behavior;
    # returned evidence still has to satisfy the requested capability.
    for field, maximum in (("capabilities", 8), ("observedCapabilities", 8), ("engines", 8)):
        values = raw.get(field) or []
        if not isinstance(values, (list, tuple)) or len(values) > maximum:
            raise ToolConfigError(f"{field} must be a short list")
        operation[field] = list(dict.fromkeys(
            _text(item, field, limit=32, required=True).lower() for item in values))
    if any(item not in {"classify", "properties", "references", "extract"}
           for item in operation["capabilities"] + operation["observedCapabilities"]):
        raise ToolConfigError("unknown asset reader capability")
    # The MCP half of the declaration: a name, a sentence, and a typed parameter list.
    # This is what an Agent reads, and it is identical in shape to an MCP tool, so no
    # second calling convention has to be learned or invented.
    # A name is how an Agent calls this, so every operation gets one. An operation
    # that predates the named catalog -- a plain asset-reader recipe, addressed by
    # role -- is still a real recipe, so it is given a derived name rather than
    # refused: refusing would silently drop a tool the user already registered.
    operation["name"] = operation.get("name") or _text(
        raw.get("name"), "operation name", limit=128) or _derived_operation_name(
        role, operation.get("argv") or [])
    if not _ID_RE.fullmatch(str(operation["name"]).lower()) and kind == "cli":
        raise ToolConfigError(
            "a command-line operation name must be a short identifier, so it can be "
            "offered as a tool the Agent calls by name")
    operation["description"] = _text(raw.get("description"), "operation description",
                                     limit=1024)
    operation["inputSchema"] = validate_input_schema(raw.get("inputSchema"))
    operation["result"] = validate_result(raw.get("result"))
    if kind == "cli":
        # Every slot the template will substitute has to be a declared parameter, or
        # the operation could only be called by someone who already knew the secret.
        declared = set(operation["inputSchema"]["properties"])
        missing = [name for name in operation_placeholders(operation["argv"])
                   if name not in declared and name not in HOST_SUPPLIED_SLOTS]
        if missing:
            raise ToolConfigError(
                "argv uses undeclared parameters: " + ", ".join(sorted(missing)))
    failures = raw.get("knownFailures") or []
    if not isinstance(failures, (list, tuple)) or len(failures) > 32:
        raise ToolConfigError("knownFailures must be a short list")
    # Recording what does not work matters as much as recording what does: without it
    # the same dead end is rediscovered on every asset that hits it.
    operation["knownFailures"] = [
        {"case": _text((item or {}).get("case"), "failure case", limit=256),
         "symptom": _text((item or {}).get("symptom"), "failure symptom", limit=512),
         "at": _text((item or {}).get("at") or _now(), "failure time", limit=64)}
        for item in failures if isinstance(item, Mapping)
        and str((item or {}).get("case") or "").strip()]
    proved = raw.get("proved")
    # The whole point of the interface block: an operation carries the evidence that it
    # was watched working, or it is not offered.
    operation["proved"] = {
        "sample": _text((proved or {}).get("sample"), "proof sample", limit=1024),
        "at": _text((proved or {}).get("at") or _now(), "proof time", limit=64),
        # The program as it was when this was proved. An updated extractor often
        # changes its arguments, so a recipe whose program has moved on is stale
        # rather than wrong, and says so instead of failing mysteriously.
        "programSha256": _text((proved or {}).get("programSha256"),
                               "proof program digest", limit=64),
        "sampleSha256": _text((proved or {}).get("sampleSha256"),
                              "proof sample digest", limit=64),
        "producedFiles": int((proved or {}).get("producedFiles") or 0),
    } if isinstance(proved, Mapping) and str((proved or {}).get("sample") or "").strip() else None
    return operation



def _derived_operation_name(role: str, argv: Sequence[Any]) -> str:
    """A stable identifier for an operation that was recorded without one.

    Derived from the role and the invocation itself, so the same recipe keeps the
    same name across restarts and two different recipes of one role never collide.
    """

    digest = hashlib.sha256(
        "\u0000".join(str(item) for item in argv).encode("utf-8")).hexdigest()[:8]
    return "%s_%s" % (str(role or "operation").replace("-", "_"), digest)


def validate_tool(raw: Mapping[str, Any]) -> dict[str, Any]:
    identifier = _text(raw.get("id"), "tool id", limit=64, required=True).lower()
    if not _ID_RE.fullmatch(identifier):
        raise ToolConfigError("tool id must be lower-case letters, digits, '.', '-' or '_'")
    if identifier == "replaylab":
        raise ToolConfigError("'replaylab' is the built-in server id and cannot be redefined")
    connection = raw.get("connection")
    if not isinstance(connection, Mapping):
        raise ToolConfigError("a tool needs a connection")
    roles = raw.get("roles") or ["general"]
    if not isinstance(roles, (list, tuple)):
        raise ToolConfigError("roles must be a list")
    resolved_roles = []
    for role in roles:
        name = _text(role, "role", limit=32).lower()
        if name not in ROLES:
            raise ToolConfigError("role must be one of: " + ", ".join(ROLES))
        if name not in resolved_roles:
            resolved_roles.append(name)
    interface = raw.get("interface") or {}
    if not isinstance(interface, Mapping):
        raise ToolConfigError("interface must be an object")
    operations = []
    for item in list(interface.get("operations") or [])[:MAX_OPERATIONS]:
        if isinstance(item, Mapping):
            operations.append(validate_operation(item))
    builtin_kind = _text(raw.get("builtinKind"), "built-in kind", limit=32).lower()
    engines = raw.get("engines") or []
    if not isinstance(engines, (list, tuple)) or len(engines) > 8:
        raise ToolConfigError("engines must be a short list")
    engines = list(dict.fromkeys(_text(item, "engine", limit=32, required=True).lower()
                                for item in engines))
    raw_options = raw.get("builtinOptions") or {}
    builtin_options: dict[str, Any] = {}
    if isinstance(raw_options, Mapping):
        for key, value in list(raw_options.items())[:16]:
            name = str(key or "").strip()
            if _ENV_NAME_RE.fullmatch(name) and isinstance(value, (str, bool)) \
                    and not (isinstance(value, str) and ("\n" in value or "\r" in value)):
                builtin_options[name] = value if isinstance(value, bool) else value.strip()[:1024]
    return {
        "id": identifier,
        "title": _text(raw.get("title"), "tool name", limit=128) or identifier,
        "description": _text(raw.get("description"), "description", limit=MAX_TEXT),
        "enabled": bool(raw.get("enabled", True)),
        "roles": resolved_roles or ["general"],
        "connection": validate_connection(connection),
        # A shipped recipe remembers which built-in it is and the answers the
        # dialog collected, so Settings can reopen it and re-derive the interface.
        "builtinKind": builtin_kind,
        "engines": engines,
        "builtinOptions": builtin_options,
        # "bundled": created from the tools that travel with the program; the
        # AgentHost keeps such entries in step with the bundle and never lets
        # them displace a program the user registered by hand.
        "source": "bundled" if str(raw.get("source", "")).strip() == "bundled" else "registered",
        "interface": {
            "source": _text(interface.get("source"), "interface source", limit=64),
            "discoveredAt": _text(interface.get("discoveredAt"), "discovery time", limit=64),
            "toolCount": max(0, int(interface.get("toolCount") or 0)),
            "operations": operations,
            # Help text and an operation summary are multi-line by nature; the record
            # keeps them as one readable line rather than refusing them.
            "note": _text(" ".join(str(interface.get("note") or "").split()),
                          "interface note", limit=MAX_TEXT),
        },
    }


def map_tool_paths(tool: Mapping[str, Any], convert) -> dict[str, Any]:
    """Apply ``convert`` to every path-like field of one registration (copy)."""

    result = json.loads(json.dumps(tool))
    connection = result.get("connection") or {}
    if isinstance(connection.get("command"), str):
        connection["command"] = convert(connection["command"])
    if isinstance(connection.get("args"), list):
        connection["args"] = [convert(item) if isinstance(item, str) else item for item in connection["args"]]
    options = result.get("builtinOptions") or {}
    for key, value in list(options.items()):
        if isinstance(value, str):
            options[key] = convert(value)
    for operation in (result.get("interface") or {}).get("operations") or []:
        if isinstance(operation.get("argv"), list):
            operation["argv"] = [convert(item) if isinstance(item, str) else item for item in operation["argv"]]
    return result


def load_tools(path: Path | str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Every registered tool, valid or not reported; a broken entry never raises."""

    source = Path(path) if path is not None else default_tools_path()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError) as error:
        return [], [f"tool configuration unreadable: {error}"[:400]]
    if not isinstance(payload, Mapping):
        return [], ["tool configuration is not an object"]
    tools: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    seen: set[str] = set()
    for raw in list(payload.get("tools") or [])[:MAX_TOOLS]:
        if not isinstance(raw, Mapping):
            continue
        try:
            tool = validate_tool(raw)
        except ToolConfigError as error:
            diagnostics.append(f"tool dropped: {error}"[:400])
            continue
        if tool["id"] in seen:
            diagnostics.append(f"duplicate tool id dropped: {tool['id']}")
            continue
        seen.add(tool["id"])
        tools.append(map_tool_paths(tool, expand_tool_path))
    return tools, diagnostics[:32]


def save_tools(tools: list[Mapping[str, Any]], path: Path | str | None = None) -> Path:
    target = Path(path) if path is not None else default_tools_path()
    validated = [map_tool_paths(validate_tool(tool), fold_tool_path) for tool in tools][:MAX_TOOLS]
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps({"schema": SCHEMA, "tools": validated}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(temporary, target)
    return target



def connection_identity(connection: Mapping[str, Any]) -> tuple[Any, ...]:
    """What makes this *the same program or endpoint* as before.

    A discovered interface describes what a program can do, so it survives a change
    of credentials or headers and is thrown away only when the thing being talked to
    is different. Comparing whole connection objects instead made re-saving a tool
    from Settings -- which rewrites every field, including ones the dialog does not
    show -- look like a new program and silently delete the recipes.
    """

    kind = str(connection.get("kind", "") or "")
    if kind.startswith("mcp-http"):
        return (kind, str(connection.get("url", "") or ""))
    return (kind, str(connection.get("command", "") or ""),
            tuple(str(item) for item in connection.get("args") or ()))


def upsert_tool(raw: Mapping[str, Any], path: Path | str | None = None) -> dict[str, Any]:
    """Add or replace a tool, keeping any interface already discovered for it."""

    tool = validate_tool(raw)
    tools, _ = load_tools(path)
    previous = next((item for item in tools if item["id"] == tool["id"]), None)
    if previous and not tool["interface"]["operations"]:
        # Re-registering the same connection must not throw away what we learned; a
        # changed connection must, because the interface belonged to the old one.
        if connection_identity(previous["connection"]) == connection_identity(
                tool["connection"]):
            tool["interface"] = previous["interface"]
    rest = [item for item in tools if item["id"] != tool["id"]]
    if len(rest) >= MAX_TOOLS:
        raise ToolConfigError(f"at most {MAX_TOOLS} tools can be registered")
    save_tools(rest + [tool], path)
    return tool


def remove_tool(tool_id: str, path: Path | str | None = None) -> bool:
    identifier = str(tool_id or "").strip().lower()
    tools, _ = load_tools(path)
    rest = [item for item in tools if item["id"] != identifier]
    if len(rest) == len(tools):
        return False
    save_tools(rest, path)
    return True


def set_tool_enabled(tool_id: str, enabled: bool, path: Path | str | None = None) -> bool:
    identifier = str(tool_id or "").strip().lower()
    tools, _ = load_tools(path)
    found = False
    for tool in tools:
        if tool["id"] == identifier:
            tool["enabled"] = bool(enabled)
            found = True
    if found:
        save_tools(tools, path)
    return found


def record_interface(tool_id: str, interface: Mapping[str, Any],
                     path: Path | str | None = None) -> dict[str, Any] | None:
    """Write back what discovery found. This is how a tool stops needing configuration."""

    identifier = str(tool_id or "").strip().lower()
    tools, _ = load_tools(path)
    updated: dict[str, Any] | None = None
    for tool in tools:
        if tool["id"] != identifier:
            continue
        tool["interface"] = dict(interface)
        # A tool that proved it can read an asset holds that role from now on.
        if any(op.get("role") == "asset-reader" and op.get("proved")
               for op in interface.get("operations") or []):
            if "asset-reader" not in tool["roles"]:
                tool["roles"].append("asset-reader")
        updated = tool
    if updated is not None:
        save_tools(tools, path)
    return updated


def asset_reader_operation(tool: Mapping[str, Any]) -> dict[str, Any] | None:
    """The proved way to ask this tool what a file is, if it has one."""

    if not tool.get("enabled", True) or "asset-reader" not in (tool.get("roles") or []):
        return None
    for operation in tool.get("interface", {}).get("operations") or []:
        if operation.get("role") == "asset-reader" and operation.get("proved"):
            return operation
    return None


def general_mcp_tools(tools: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The tools whose MCP servers are attached to a task."""

    attached = []
    for tool in tools:
        available, unused_reason = tool_runtime_status(tool)
        if not available:
            continue
        if "general" not in (tool.get("roles") or []):
            continue
        if str(tool.get("connection", {}).get("kind", "")).startswith("mcp-"):
            attached.append(dict(tool))
    return attached


def tool_runtime_status(tool: Mapping[str, Any]) -> tuple[bool, str]:
    """Return live local availability without mutating the saved registration.

    Optional programs remain visible in Settings, but are omitted from the
    Agent's callable catalog until their executable exists. Remote MCP
    reachability is checked by its bounded initialize handshake.
    """

    if not tool.get("enabled", True):
        return False, "disabled"
    connection = tool.get("connection") or {}
    kind = str(connection.get("kind", "") or "")
    if kind == "mcp-http":
        return True, ""
    command = str(connection.get("command", "") or "").strip()
    if not command:
        return False, "local tool command is empty"
    resolved = shutil.which(command)
    candidate = Path(resolved) if resolved else Path(command).expanduser()
    if candidate.is_file():
        return True, ""
    return False, "local tool executable is unavailable: %s" % command[:300]


def runtime_servers(tools: list[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """The MCP servers a task attaches, in the shape both engines already expect."""

    servers: list[dict[str, Any]] = []
    for tool in general_mcp_tools(tools):
        connection = tool.get("connection") or {}
        kind = str(connection.get("kind", ""))
        server: dict[str, Any] = {
            "id": tool["id"],
            "title": tool.get("title") or tool["id"],
            "enabled": True,
            "note": tool.get("description", ""),
        }
        if kind == "mcp-http":
            server["transport"] = "http"
            server["url"] = connection.get("url", "")
            server["bearerEnv"] = connection.get("bearerEnv", "")
            server["headers"] = dict(connection.get("headers") or {})
            server["headerEnv"] = dict(connection.get("headerEnv") or {})
        else:
            server["transport"] = "stdio"
            server["command"] = connection.get("command", "")
            server["args"] = list(connection.get("args") or [])
            server["env"] = dict(connection.get("env") or {})
        servers.append(server)
    return tuple(servers)


def migrate_legacy(readers_path: Path | str | None = None,
                   agent_tools_path: Path | str | None = None,
                   path: Path | str | None = None) -> dict[str, Any]:
    """Fold the old asset-reader registry and MCP server list into one tool list.

    Runs once: if ``tools.json`` already holds an entry for an id, the old record is
    left alone. Nothing is deleted from the legacy files — a rollback should not cost
    the user their configuration.
    """

    target = Path(path) if path is not None else default_tools_path()
    tools, _ = load_tools(target)
    known = {tool["id"] for tool in tools}
    imported: list[str] = []

    try:
        from .agent_asset_readers import default_readers_path, load_readers as _load_readers

        def load_external_readers(source):
            return _load_readers(source)[0]

        source = Path(readers_path) if readers_path is not None else default_readers_path()
        for reader in load_external_readers(source):
            identifier = str(reader.get("id", "")).lower()
            if not identifier or identifier in known:
                continue
            operation = {
                "role": "asset-reader", "kind": "cli",
                "argv": list(reader.get("args") or ["{path}"]),
                "patterns": list(reader.get("patterns") or []),
                "timeoutSeconds": float(reader.get("timeoutSeconds", 20) or 20),
                # Carried over unproved on purpose: the old registry never proved
                # anything, so the first task re-discovers before relying on it.
                "proved": None,
            }
            tools.append(validate_tool({
                "id": identifier,
                "title": reader.get("title") or identifier,
                "description": reader.get("note") or "",
                "enabled": bool(reader.get("enabled", True)),
                "roles": ["asset-reader"],
                "connection": {"kind": "cli", "command": reader.get("command", "")},
                "interface": {"source": "migrated/asset-readers", "operations": [operation],
                              "note": "carried over from the old registry; not yet proved"},
            }))
            known.add(identifier)
            imported.append(identifier)
    except Exception:  # noqa: BLE001 - migration never blocks startup
        pass

    try:
        from .agent_tools_config import default_tools_config_path as default_agent_tools_path

        source = Path(agent_tools_path) if agent_tools_path is not None \
            else default_agent_tools_path()
        payload = json.loads(source.read_text(encoding="utf-8"))
        for server in list((payload or {}).get("mcpServers") or [])[:MAX_TOOLS]:
            if not isinstance(server, Mapping):
                continue
            identifier = str(server.get("id", "")).lower()
            if not identifier or identifier in known:
                continue
            tools.append(validate_tool({
                "id": identifier,
                "title": server.get("title") or identifier,
                "description": server.get("note") or "",
                "enabled": bool(server.get("enabled", True)),
                "roles": ["general"],
                "connection": {"kind": "mcp-stdio", "command": server.get("command", ""),
                               "args": server.get("args") or [], "env": server.get("env") or {}},
            }))
            known.add(identifier)
            imported.append(identifier)
    except Exception:  # noqa: BLE001
        pass

    if imported:
        save_tools(tools, target)
    return {"imported": imported, "path": str(target), "tools": [tool["id"] for tool in tools]}


__all__ = [
    "CONNECTION_KINDS",
    "MAX_TOOLS",
    "ROLES",
    "SCHEMA",
    "ToolConfigError",
    "asset_reader_operation",
    "default_tools_path",
    "general_mcp_tools",
    "load_tools",
    "migrate_legacy",
    "record_interface",
    "remove_tool",
    "runtime_servers",
    "tool_runtime_status",
    "save_tools",
    "set_tool_enabled",
    "slug",
    "upsert_tool",
    "validate_connection",
    "validate_operation",
    "validate_tool",
]
