"""The asset-reader registry as the Settings UI and the CLI see it.

An unpacked game tree is still engine data: ``.uasset`` says "Unreal package"
and nothing else, and a Unity ``.bundle`` says less than that. Knowing what a
file *is* needs a reader for that container. The Host ships parsers for the
formats it can read itself; everything else is a command the user registers
here, and ``asset-readers.json`` is where that registration lives.

The same file is read by the Host (``mcp_host/src/asset_readers.py``) and
written by the Agent from Settings, so the two validators must agree exactly;
``replaylab/tests/test_asset_reader_contract.py`` holds them to that.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "replaylab-asset-readers/v1"
MAX_READERS = 16
MAX_PATTERNS = 16
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def default_readers_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    # Never the shared temp directory: on Linux/macOS that put the task event
    # stream, the log and the tool settings under a world-readable /tmp that any
    # local user could pre-plant and that vanished on reboot. XDG data home is the
    # per-user equivalent of LOCALAPPDATA, which the rest of the product uses.
    root = Path(local).expanduser() if local else Path.home() / ".local" / "share"
    return root / "ReplayLab" / "config" / "asset-readers.json"


class AssetReaderError(ValueError):
    """A registration that cannot be trusted to run."""


def validate_reader(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one registration; nothing on disk is trusted as given."""

    identifier = str(raw.get("id", "") or "").strip().lower()
    if not _ID_RE.fullmatch(identifier):
        raise AssetReaderError("reader id must be lower-case letters, digits, '.', '-' or '_'")
    command = str(raw.get("command", "") or "").strip()
    if not command or "\x00" in command or len(command) > 1024:
        raise AssetReaderError("reader command is required")
    arguments = raw.get("args") or []
    if not isinstance(arguments, (list, tuple)) or len(arguments) > 32:
        raise AssetReaderError("reader args must be a short list")
    patterns = raw.get("patterns") or []
    if not isinstance(patterns, (list, tuple)) or len(patterns) > MAX_PATTERNS:
        raise AssetReaderError("reader patterns must be a short list of globs")
    magic = str(raw.get("magic", "") or "").strip()
    if magic:
        try:
            bytes.fromhex(magic)
        except ValueError as error:
            raise AssetReaderError("reader magic must be hexadecimal bytes") from error
        if len(magic) > 64:
            raise AssetReaderError("reader magic is too long")
    timeout = raw.get("timeoutSeconds", 20)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= float(timeout) <= 300:
        raise AssetReaderError("reader timeoutSeconds must be between 1 and 300")
    if not patterns and not magic:
        raise AssetReaderError(
            "a reader must declare patterns and/or magic so it is only asked about files it claims")
    probe_arguments = raw.get("probeArgs") or []
    if not isinstance(probe_arguments, (list, tuple)) or len(probe_arguments) > 32:
        raise AssetReaderError("reader probeArgs must be a short list")
    return {
        "id": identifier,
        "title": str(raw.get("title", "") or identifier)[:128],
        "engines": [str(item)[:32] for item in (raw.get("engines") or [])][:8],
        "command": command,
        "args": [str(item)[:1024] for item in arguments],
        "probeArgs": [str(item)[:1024] for item in probe_arguments],
        "patterns": [str(item)[:256] for item in patterns],
        "magic": magic.lower(),
        "timeoutSeconds": float(timeout),
        "enabled": bool(raw.get("enabled", True)),
        "note": str(raw.get("note", "") or "")[:512],
    }


def load_readers(path: Path | str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Every registered reader, enabled or not, plus what was dropped.

    Settings must show a disabled or broken row so the user can repair it; the
    Host's own loader keeps only the enabled, valid ones.
    """

    source = Path(path) if path is not None else default_readers_path()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError) as error:
        return [], [f"asset reader registry unreadable: {error}"[:400]]
    if not isinstance(payload, Mapping):
        return [], ["asset reader registry is not an object"]
    readers: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    seen: set[str] = set()
    for raw in list(payload.get("readers") or [])[:MAX_READERS]:
        if not isinstance(raw, Mapping):
            continue
        try:
            reader = validate_reader(raw)
        except AssetReaderError as error:
            diagnostics.append(f"reader dropped: {error}"[:400])
            continue
        if reader["id"] in seen:
            diagnostics.append(f"duplicate reader id dropped: {reader['id']}")
            continue
        seen.add(reader["id"])
        readers.append(reader)
    return readers, diagnostics[:32]


def save_readers(readers: list[Mapping[str, Any]], path: Path | str | None = None) -> Path:
    target = Path(path) if path is not None else default_readers_path()
    validated = [validate_reader(reader) for reader in readers][:MAX_READERS]
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps({"schema": SCHEMA, "readers": validated}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(temporary, target)
    return target


def upsert_reader(raw: Mapping[str, Any], path: Path | str | None = None) -> dict[str, Any]:
    reader = validate_reader(raw)
    readers, _ = load_readers(path)
    rest = [item for item in readers if item["id"] != reader["id"]]
    if len(rest) >= MAX_READERS:
        raise AssetReaderError(f"at most {MAX_READERS} readers can be registered")
    save_readers(rest + [reader], path)
    return reader


def remove_reader(reader_id: str, path: Path | str | None = None) -> bool:
    identifier = str(reader_id or "").strip().lower()
    readers, _ = load_readers(path)
    rest = [item for item in readers if item["id"] != identifier]
    if len(rest) == len(readers):
        return False
    save_readers(rest, path)
    return True


def set_reader_enabled(reader_id: str, enabled: bool, path: Path | str | None = None) -> bool:
    identifier = str(reader_id or "").strip().lower()
    readers, _ = load_readers(path)
    found = False
    for reader in readers:
        if reader["id"] == identifier:
            reader["enabled"] = bool(enabled)
            found = True
    if found:
        save_readers(readers, path)
    return found


# Starting points for the readers other people already maintain. ReplayLab does not
# reimplement engine formats; it detects which reader can read a tree and calls it.
# These mirror ``mcp_host/src/asset_readers.PRESETS`` — the contract test keeps the
# two lists identical, so edit both or neither.
PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "uex",
        "title": "uex (Unreal, CUE4Parse)",
        "engines": ["unreal"],
        "patterns": ["*.uasset", "*.umap", "*.uexp", "*.pak", "*.utoc", "*.ucas"],
        "command": "<path to>/uex.exe",
        "args": ["--config", "<path to>/profiles.json", "preview",
                 "--profile", "<profile name>", "{path}"],
        "probeArgs": ["--config", "<path to>/profiles.json", "preview",
                      "--profile", "<profile name>", "{path}"],
        "homepage": "https://github.com/Straw1997/uex",
        "note": ("headless CUE4Parse tool: `preview` prints one asset as JSON on stdout, which "
                 "is exactly this contract. It addresses assets by their virtual package path, "
                 "so point the profile at the pak/IoStore set rather than a loose folder. It "
                 "also speaks MCP (`uex.exe mcp`), which is the richer route: attach it as an "
                 "MCP server in Settings > AI and the Agent calls its tools directly."),
    },
    {
        "id": "assetstudio",
        "title": "AssetStudio / AssetStudioCLI (Unity)",
        "engines": ["unity"],
        "patterns": ["*.assets", "*.bundle", "*.unity3d", "*.resS", "*.ress"],
        "command": "<path to>/AssetStudioCLI.exe",
        "args": ["{path}"],
        "homepage": "https://github.com/Perfare/AssetStudio",
        "note": "Unity serialized files and asset bundles; the Host itself only classifies them",
    },
    {
        "id": "unitypy",
        "title": "UnityPy script (Unity)",
        "engines": ["unity"],
        "patterns": ["*.assets", "*.bundle", "*.unity3d"],
        "command": "python",
        "args": ["<path to>/unitypy_reader.py", "{path}"],
        "homepage": "https://github.com/K0lb3/UnityPy",
        "note": "a few lines of Python around UnityPy is usually enough to print the JSON contract",
    },
    {
        "id": "umodel",
        "title": "UE Viewer / umodel (Unreal, legacy)",
        "engines": ["unreal"],
        "patterns": ["*.upk", "*.u", "*.ukx", "*.utx", "*.usx", "*.uax", "*.xxx"],
        "command": "<path to>/umodel.exe",
        "args": ["-list", "{path}"],
        "homepage": "https://www.gildor.org/en/projects/umodel",
        "note": "older engine versions CUE4Parse-based tools may not mount",
    },
)


# Registering a reader should cost one path, not a form. These are the tools people
# actually have; recognising the executable is what turns "here is my extractor" into a
# working registration without asking the user how to drive it.
# What the Host can already read without anyone registering anything. Settings shows
# these so the coverage question ("do I even need a tool for this?") has an answer on
# screen, and the Agent reads the same list before asking the user for an extractor.
BUILTIN_READERS: tuple[dict[str, Any], ...] = (
    {
        "id": "unreal-package",
        "title": "Unreal cooked packages",
        "engines": ["unreal"],
        "patterns": ["*.uasset", "*.umap", "*.uexp"],
        "description": ("package header, name table, imports and exports: the declared class, "
                        "the object paths it references, and texture platform data"),
        "limits": "no pixel data, no vertex buffers, no material parameter values",
    },
    {
        "id": "unity-serialized",
        "title": "Unity serialized files and bundles",
        "engines": ["unity"],
        "patterns": ["*.assets", "*.bundle", "*.unity3d", "*.resS"],
        "description": "file header and object class ids: Texture2D, Mesh, Material, AnimationClip",
        "limits": "classification only; pixels, meshes and parameters need AssetStudio or UnityPy",
    },
    {
        "id": "standalone-containers",
        "title": "Standalone containers",
        "engines": [],
        "patterns": ["*.dds", "*.png", "*.jpg", "*.exr", "*.hdr", "*.fbx", "*.gltf", "*.glb"],
        "description": "recognised by their leading bytes, not their extension",
        "limits": "",
    },
)


# The same tools are often better attached as MCP servers than as one-file readers:
# a server keeps its index open across calls instead of re-mounting the pak per file.
MCP_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "uex-mcp",
        "title": "uex MCP server (Unreal)",
        "engines": ["unreal"],
        "command": "<path to>/uex.exe",
        "args": ["mcp"],
        "homepage": "https://github.com/Straw1997/uex",
        "note": ("discovery, asset preview, metric queries, relationship analysis and job "
                 "management over MCP; register it in Settings > AI under MCP servers"),
    },
)


# Registering a reader should cost one path, not a form. These are the tools people
# actually have; recognising the executable is what turns "here is my extractor" into a
# working registration without asking the user how to drive it.
KNOWN_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "match": ("uex", "uex-cli"),
        "preset": "uex",
        "engines": ["unreal"],
        "description": "Unreal packages through CUE4Parse: classes, references and parameters",
        # `preview` prints one asset as JSON, which is exactly the reader contract.
        "invocations": (["preview", "{path}"], ["preview", "--profile", "{profile}", "{path}"]),
        "needs": ("profile",),
    },
    {
        "match": ("assetstudiocli", "assetstudiomodcli", "assetstudio"),
        "preset": "assetstudio",
        "engines": ["unity"],
        "description": "Unity serialized files and asset bundles",
        "invocations": (["{path}", "--json"], ["{path}"]),
    },
    {
        "match": ("umodel", "umodel_64", "ueviewer"),
        "preset": "umodel",
        "engines": ["unreal"],
        "description": "older Unreal versions CUE4Parse-based tools may not mount",
        # Its list output is plain text, so it cannot satisfy the JSON reader
        # contract. The declared list/export operations remain callable instead.
        "invocations": (),
    },
    {
        "match": ("fmodel",),
        "preset": "",
        "engines": ["unreal"],
        "description": "FModel; prefer its CLI build or uex for headless use",
        "invocations": (["--json", "{path}"],),
    },
    {
        "match": ("quickbms",),
        "preset": "",
        "engines": [],
        "description": "script-driven extractor for many proprietary containers",
        "invocations": (["{path}"],),
    },
)

# Tried in order for an executable nobody recognises. A reader has one job — take a
# path, print a JSON object — so the shapes worth trying are few.
GENERIC_INVOCATIONS: tuple[tuple[str, ...], ...] = (
    ("{path}",),
    ("--json", "{path}"),
    ("preview", "{path}"),
    ("info", "{path}"),
    ("describe", "{path}"),
    ("dump", "--json", "{path}"),
    ("export", "--json", "{path}"),
)

# A bare invocation can enter a graphical tool's package chooser.
HELP_FLAGS: tuple[tuple[str, ...], ...] = (("--help",), ("-h",), ("help",), ("-help",))


def help_flags(command: str) -> tuple[tuple[str, ...], ...]:
    """Use known CLI help syntax; guessed flags can enter viewer mode."""

    stem = Path(str(command)).stem.lower()
    if stem in {"umodel", "umodel_64", "ueviewer"}:
        return (("-help",),)
    if stem in {"fmodel", "assetstudio"}:
        return ()
    return HELP_FLAGS

_SUBCOMMAND_RE = re.compile(r"^\s{2,}([a-z][a-z0-9-]{2,20})\s{2,}\S", re.MULTILINE)


def recognise_tool(command: str) -> dict[str, Any] | None:
    """Which known tool this executable is, by its file name."""

    stem = Path(str(command or "")).stem.strip().lower()
    if not stem:
        return None
    for tool in KNOWN_TOOLS:
        if stem in tool["match"]:
            return dict(tool)
    return None


def candidate_invocations(command: str, help_text: str = "") -> tuple[tuple[str, ...], ...]:
    """Argument shapes to try, most likely first.

    A recognised tool contributes its own; anything else contributes the subcommands
    its help text advertises, then the generic shapes.
    """

    candidates: list[tuple[str, ...]] = []
    tool = recognise_tool(command)
    if tool:
        # Known non-JSON tools must not fall through to guessed viewer commands.
        return tuple(tuple(invocation) for invocation in tool["invocations"])
    advertised = []
    if help_text:
        for name in _SUBCOMMAND_RE.findall(help_text[:20000]):
            if name in {"help", "version", "install", "update", "serve", "mcp"}:
                continue
            if name not in advertised:
                advertised.append(name)
    for name in advertised[:8]:
        candidates.append((name, "{path}"))
        candidates.append((name, "--json", "{path}"))
    for invocation in GENERIC_INVOCATIONS:
        candidates.append(invocation)
    unique: list[tuple[str, ...]] = []
    for invocation in candidates:
        if invocation not in unique:
            unique.append(invocation)
    return tuple(unique[:24])


def discovery_locations() -> tuple[Path, ...]:
    """Where an extractor is usually installed, plus everything on PATH."""

    locations: list[Path] = []
    for entry in str(os.environ.get("PATH", "")).split(os.pathsep):
        candidate = entry.strip().strip('"')
        if candidate:
            locations.append(Path(candidate))
    for variable in ("LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)", "USERPROFILE"):
        base = os.environ.get(variable)
        if not base:
            continue
        root = Path(base)
        locations.extend([root, root / "Tools", root / "tools"])
        if variable == "LOCALAPPDATA":
            # The drop folder Settings names for the built-in unpackers: one
            # sub-folder per tool (uex/, assetstudio/, umodel/). Unzip a release
            # flat into it; deeper layouts are not searched.
            unpackers = root / "ReplayLab" / "tools" / "unpackers"
            locations.append(unpackers)
            try:
                if unpackers.is_dir():
                    locations.extend(sorted(child for child in unpackers.iterdir() if child.is_dir())[:32])
            except OSError:
                pass
        if variable == "USERPROFILE":
            locations.extend([root / "Downloads", root / "Desktop", root / "source" / "tools"])
    unique: list[Path] = []
    for location in locations:
        if location not in unique:
            unique.append(location)
    return tuple(unique[:64])


def discover_installed_readers(extra_roots: list[Path | str] | None = None) -> list[dict[str, Any]]:
    """Extractors already on this machine, ready to be probed.

    A shallow look in the obvious places: an executable buried eight levels deep is
    not something to go hunting for, and a full-disk scan is not a setup step.
    """

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    names = {name: tool for tool in KNOWN_TOOLS for name in tool["match"]}
    suffixes = (".exe", "") if os.name == "nt" else ("", ".exe")
    locations = list(discovery_locations())
    for extra in extra_roots or []:
        locations.insert(0, Path(str(extra)))
    for location in locations:
        try:
            if not location.is_dir():
                continue
            entries = list(location.iterdir())[:2000]
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            stem = entry.stem.lower()
            if stem not in names or entry.suffix.lower() not in suffixes:
                continue
            resolved = str(entry)
            if resolved.lower() in seen:
                continue
            seen.add(resolved.lower())
            tool = names[stem]
            found.append({
                "command": resolved,
                "id": tool["preset"] or stem,
                "title": entry.stem,
                "engines": list(tool["engines"]),
                "description": tool["description"],
                "needs": list(tool.get("needs") or []),
            })
    return found[:16]


def registration_from_command(command: str, name: str = "",
                             description: str = "") -> dict[str, Any]:
    """Build a registration from an executable path alone.

    Settings asks for one thing — where the tool is. A tool we recognise contributes
    its own invocation; anything else is stored with the plain "path in, JSON out"
    shape and marked unverified, which is what tells the Agent to probe it before the
    project relies on it.
    """

    executable = str(command or "").strip().strip('"')
    if not executable:
        raise AssetReaderError("the path to the extractor is required")
    tool = recognise_tool(executable) or {}
    if tool and not tool["invocations"]:
        raise AssetReaderError(
            "this tool has no JSON asset-reader invocation; use its declared list/export "
            "operations in the tool catalog")
    identifier = (str(name or "").strip().lower()
                  or str(tool.get("preset") or "")
                  or Path(executable).stem.lower())
    identifier = re.sub(r"[^a-z0-9._-]", "-", identifier).strip("-")[:64] or "reader"
    invocation = list((tool.get("invocations") or [["{path}"]])[0])
    # A shape that still carries a placeholder we cannot fill is not a usable shape.
    if any("{profile}" in item for item in invocation):
        invocation = ["{path}"]
    patterns: list[str] = []
    for preset in PRESETS:
        if preset["id"] == tool.get("preset"):
            patterns = list(preset["patterns"])
            break
    return {
        "id": identifier,
        "title": str(name or "").strip() or Path(executable).stem,
        "engines": list(tool.get("engines") or []),
        "command": executable,
        "args": invocation,
        "patterns": patterns or ["*"],
        "timeoutSeconds": 20.0,
        "enabled": True,
        "note": (str(description or "").strip() or str(tool.get("description") or ""))[:512],
        "recognised": bool(tool),
    }


def reader_contract() -> dict[str, Any]:
    """What a registered reader is asked to do; shown in Settings and to the Agent."""

    return {
        "schema": SCHEMA,
        "invocation": ("the command is run with the asset's absolute path substituted for {path} "
                       "(or appended when no placeholder appears); it must print one JSON object "
                       "on stdout and exit 0"),
        "outputFields": {
            "kind": "texture | material | mesh | shader | animation | audio | level | other",
            "className": "the engine class, when the container declares one",
            "references": "[{relation, target}] object paths this asset points at",
            "metadata": "anything else worth citing: dimensions, format, counts, parameter values",
        },
        "presets": [dict(item) for item in PRESETS],
    }


__all__ = [
    "BUILTIN_READERS",
    "MCP_PRESETS",
    "KNOWN_TOOLS",
    "MAX_READERS",
    "PRESETS",
    "SCHEMA",
    "AssetReaderError",
    "default_readers_path",
    "discover_installed_readers",
    "discovery_locations",
    "recognise_tool",
    "load_readers",
    "reader_contract",
    "registration_from_command",
    "remove_reader",
    "save_readers",
    "set_reader_enabled",
    "upsert_reader",
    "validate_reader",
]
