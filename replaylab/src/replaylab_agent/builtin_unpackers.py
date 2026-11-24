"""Unpackers Bread AI ships pre-configured: program, interface and formats.

A person supplies one thing -- where the program is -- and every invocation is
already written down: what each tool reads, how it is called for the operations
the AI needs (list, preview, search, export), and how to tell whether a call
worked. Nothing here is guessed at run time.

The recipes follow each tool's published command line:

* uex (Straw1997/uex, CUE4Parse): ``uex --config profiles.json
  {list|search|preview|export|scan} --profile <name>``; ``uex --config profiles.json mcp`` is the
  same tool as an MCP server (pak/IoStore mounting, previews, inventories).
* AssetStudioModCLI (aelurum/AssetStudio): ``AssetStudioModCLI <file|folder> -m
  {info|export|dump|exportRaw} -t <types> -o <dir> -g <grouping>``; exports
  Texture2D, Sprite, TextAsset, MonoBehaviour, Font, Shader, AudioClip, Mesh.
* UnityPy (K0lb3/UnityPy): a bundled Python script (``unitypy_reader.py``) prints
  one JSON object per file (the Host's reader contract) and exports shader text,
  material parameters, textures and text assets.
* UE Viewer / umodel (gildor.org): ``umodel -list|-export [-out=dir]
  [-game=tag] [-aes=key] <package>`` for UE1-UE4 packages the CUE4Parse route
  cannot mount.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

UNREAL_COOKED_PATTERNS = ["*.uasset", "*.umap", "*.uexp", "*.ubulk", "*.uptnl",
                          "*.pak", "*.utoc", "*.ucas"]
# umodel's own formats only. A cooked UE4/5 tree belongs to uex and to the Host's
# own package parser: routing a .uasset here made umodel fall back to its graphical
# package chooser -- a modal window in the middle of an automated read.
UNREAL_LEGACY_PATTERNS = ["*.upk", "*.u", "*.ukx", "*.utx", "*.usx", "*.uax", "*.xxx"]
UNITY_PATTERNS = ["*.assets", "*.resS", "*.resource", "*.bundle", "*.unity3d", "*.ab",
                  "*.unitybundle", "sharedassets*", "level*", "globalgamemanagers*",
                  "resources.assets", "*.split0", "*.apk"]

_HEX_KEY_RE = re.compile(r"^(0x)?[0-9A-Fa-f]{64}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_GAME_TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _builtin_proof(kind: str) -> dict[str, Any]:
    # A shipped recipe is offered without a per-machine probe: it follows the tool's
    # documented command line. The first real call records its own result next to
    # this, and a changed program is noticed the same way as for any other tool.
    return {"sample": "built-in recipe: %s" % kind, "at": _now(),
            "programSha256": "", "sampleSha256": "", "producedFiles": 0}


def _operation(*, name: str, role: str, argv: list[str], description: str,
               patterns: list[str] | None = None, kind: str = "cli",
               properties: Mapping[str, Mapping[str, Any]] | None = None,
               required: list[str] | None = None, result: str = "stdout-text",
               min_files: int = 0, timeout: float = 120.0,
               builtin_kind: str = "") -> dict[str, Any]:
    return {
        "role": role, "kind": kind, "name": name, "argv": list(argv),
        "description": description, "patterns": list(patterns or []),
        "timeoutSeconds": float(timeout),
        "inputSchema": {"type": "object",
                        "properties": {key: dict(value) for key, value in
                                       (properties or {}).items()},
                        "required": list(required or [])},
        "result": {"kind": result, "successWhen": {"exitCode": 0, "minFiles": min_files}},
        "proved": _builtin_proof(builtin_kind or name),
    }


# ---------------------------------------------------------------------------
# The catalog Settings shows and the dialog builds from. ``options`` are the
# only questions the dialog asks besides the program path.
BUILTIN_UNPACKERS: tuple[dict[str, Any], ...] = (
    {
        "kind": "uex",
        "title": "uex (Unreal, CUE4Parse)",
        "engines": ["unreal"],
        "programNames": ["uex", "uex-cli"],
        "programHint": "uex.exe",
        "homepage": "https://github.com/Straw1997/uex",
        "patterns": UNREAL_COOKED_PATTERNS,
        "reads": "cooked packages (.uasset/.umap/.uexp) and pak / IoStore (.pak, .utoc/.ucas) "
                 "containers: StaticMesh, SkeletalMesh, Texture, Material, MaterialInstance "
                 "and 20+ UObject classes, references and parameters. The project's unpacked "
                 "tree is mounted directly (no paks, no AES keys): the Host generates the uex "
                 "profile from the project's asset root",
        "options": [
            {"key": "configPath", "title": "profiles.json (pak mode only)", "kind": "file",
             "required": False,
             "hint": "leave empty to read the project's unpacked tree directly; set only to "
                     "mount original .pak/.utoc containers with your own game version, AES "
                     "keys and .usmap"},
            {"key": "profile", "title": "Profile name (pak mode only)", "kind": "text",
             "required": False,
             "hint": "the profile in that profiles.json; empty in unpacked mode"},
            {"key": "registerMcp", "title": "Also attach uex as an MCP server",
             "kind": "bool", "default": True,
             "hint": "uex mcp: the same tool over MCP -- previews, search, inventories and "
                     "jobs are then called directly during a task"},
        ],
    },
    {
        "kind": "assetstudio",
        "title": "AssetStudioModCLI (Unity)",
        "engines": ["unity"],
        "programNames": ["assetstudiomodcli", "assetstudiocli", "assetstudio"],
        "programHint": "AssetStudioModCLI.exe",
        "homepage": "https://github.com/aelurum/AssetStudio",
        "patterns": UNITY_PATTERNS,
        "reads": "Unity serialized files, asset bundles (.bundle/.unity3d/.ab), APKs and "
                 "level files: Texture2D, Sprite, TextAsset, MonoBehaviour, Font, Shader, "
                 "AudioClip, VideoClip, Mesh, Animator",
        "options": [
            {"key": "unityVersion", "title": "Unity version (optional)", "kind": "text",
             "required": False,
             "hint": "only for stripped bundles that do not carry their engine version"},
        ],
    },
    {
        "kind": "unitypy",
        "title": "UnityPy script (Unity)",
        "engines": ["unity"],
        "programNames": ["python", "python3", "py"],
        "programHint": "python.exe (with `pip install UnityPy`)",
        "homepage": "https://github.com/K0lb3/UnityPy",
        "patterns": UNITY_PATTERNS,
        "reads": "Unity serialized files and bundles through UnityPy: object inventory as "
                 "JSON (the Host's reader contract), shader text, material parameters, "
                 "textures and text assets",
        "options": [],
    },
    {
        "kind": "umodel",
        "title": "UE Viewer / umodel (Unreal, legacy)",
        "engines": ["unreal"],
        "programNames": ["umodel", "umodel_64", "ueviewer"],
        "programHint": "umodel_64.exe",
        "homepage": "https://www.gildor.org/en/projects/umodel",
        "patterns": UNREAL_LEGACY_PATTERNS,
        "reads": "Unreal Engine 1-3 era packages (.upk/.u/.ukx/.utx/.usx/.uax/.xxx): "
                 "meshes, skeletal meshes, animations and textures from 300+ games, "
                 "including versions CUE4Parse-based tools do not mount. It is never used "
                 "for cooked UE4/5 packages (uex reads those): given one it cannot open it "
                 "shows a graphical package chooser instead of failing",
        "options": [
            {"key": "gameTag", "title": "Game tag (optional)", "kind": "text",
             "required": False, "hint": "umodel -game=<tag>; see `umodel -taglist`"},
            {"key": "aesKey", "title": "AES key (optional)", "kind": "text",
             "required": False, "hint": "umodel -aes=<key> for encrypted paks"},
        ],
    },
)

BUILTIN_UNPACKER_KINDS = tuple(item["kind"] for item in BUILTIN_UNPACKERS)


def builtin_unpacker(kind: str) -> dict[str, Any] | None:
    for item in BUILTIN_UNPACKERS:
        if item["kind"] == str(kind or "").strip().lower():
            return dict(item)
    return None


def builtin_tool_id(kind: str) -> str:
    return "builtin-%s" % kind


def _clean_options(kind: str, options: Mapping[str, Any] | None) -> dict[str, Any]:
    spec = builtin_unpacker(kind)
    if spec is None:
        raise ValueError("unknown built-in unpacker: %s" % kind)
    values: dict[str, Any] = {}
    for option in spec["options"]:
        key = option["key"]
        raw = (options or {}).get(key, option.get("default"))
        if option["kind"] == "bool":
            values[key] = bool(raw)
            continue
        text = str(raw or "").strip()
        if "\0" in text or "\n" in text or "\r" in text:
            raise ValueError("%s must be a single line" % option["title"])
        if option.get("required") and not text:
            raise ValueError("%s is required" % option["title"])
        if option["kind"] == "file" and text and text.startswith(TOOLS_ROOT_TOKEN):
            text = str(Path(expand_tool_path(text)))
        values[key] = text
    if kind == "unitypy" and (options or {}).get("embedded"):
        values["embedded"] = True
    if kind == "uex":
        # Both or neither: a profiles.json names the paks and a profile names the
        # entry in it. Half of that pair would silently fall back to the unpacked
        # tree and read something other than what the user set up.
        if values.get("configPath") and not values.get("profile"):
            raise ValueError("Profile name is required when a profiles.json is given "
                             "(leave both empty to read the project's unpacked tree)")
        if values.get("profile") and not values.get("configPath"):
            raise ValueError("profiles.json is required when a profile name is given "
                             "(leave both empty to read the project's unpacked tree)")
    if values.get("profile") and not _PROFILE_RE.fullmatch(values["profile"]):
        raise ValueError("profile name may use letters, digits, '.', '_' and '-' only")
    if values.get("gameTag") and not _GAME_TAG_RE.fullmatch(values["gameTag"]):
        raise ValueError("game tag may use letters, digits, '.', '_' and '-' only")
    if values.get("aesKey") and not _HEX_KEY_RE.fullmatch(values["aesKey"]):
        raise ValueError("AES key must be 64 hexadecimal digits (optionally 0x-prefixed)")
    return values


def unitypy_script_path() -> Path:
    """Where the bundled UnityPy reader is materialised for the interpreter to run."""

    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    root = Path(local).expanduser() if local else Path.home() / ".local" / "share"
    return root / "ReplayLab" / "tools" / "unitypy_reader.py"


def materialise_unitypy_script(target: Path | None = None) -> Path:
    from .unitypy_reader_script import SCRIPT

    path = Path(target) if target is not None else unitypy_script_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current != SCRIPT:
        path.write_text(SCRIPT, encoding="utf-8", newline="\n")
    return path


# Slots the Host fills for uex from the active project's unpacked root: the
# generated profiles.json, the profile named after the mounted directory, and a
# file's path relative to that directory (the key CUE4Parse gives a loose file).
UEX_HOST_SLOTS = ("uexConfig", "uexProfile", "uexPath", "assetRoot")


def _uex_operations(cfg: str = "", profile: str = "") -> list[dict[str, Any]]:
    """uex recipes. Without a hand-written profile they run in *unpacked mode*: the
    Host mounts the project's already-extracted tree through a profile it generates
    (directory + engine version, no keys); with ``cfg``/``profile`` they mount the
    paks that profile describes."""

    unpacked = not (cfg and profile)
    config_value = "{uexConfig}" if unpacked else cfg
    profile_value = "{uexProfile}" if unpacked else profile
    reader_target = "{uexPath}" if unpacked else "{path}"
    reader_patterns = ["*.uasset", "*.umap"] if unpacked else UNREAL_COOKED_PATTERNS

    def argv(subcommand: str, *rest: str) -> list[str]:
        """``uex --config <file> <subcommand> --profile <name> ...``

        Only ``--config`` is global in uex; ``--profile`` is declared by each
        subcommand, so it has to come *after* the subcommand name. Putting it
        before -- which is what the first version of these recipes did -- makes
        every single uex call fail with an unrecognised-option error.
        """

        return ["--config", config_value, subcommand, "--profile", profile_value, *rest]

    return [
        _operation(name="uex_preview", role="asset-reader", builtin_kind="uex",
                   argv=argv("preview", reader_target), patterns=reader_patterns,
                   description="One cooked asset as JSON: class, properties, references, "
                               "texture platform data (uex preview)"),
        _operation(name="uex_list", role="general", builtin_kind="uex",
                   argv=argv("list", "{directory}"),
                   description="List children of a virtual directory of the mounted tree "
                               "(uex list). Paths are relative to the mount root, e.g. "
                               "SilverPalace/Content/ArtRes",
                   properties={"directory": {"type": "string",
                                             "description": "virtual path, e.g. <Game>/Content/"}},
                   required=["directory"]),
        _operation(name="uex_search", role="general", builtin_kind="uex",
                   argv=argv("search", "--limit", "{limit}", "{query}"),
                   description="Find assets by name across the mounted tree (uex search)",
                   properties={"query": {"type": "string",
                                         "description": "name or path fragment"},
                               "limit": {"type": "string", "default": "100",
                                         "description": "maximum matches to print"}},
                   required=["query"]),
        _operation(name="uex_preview_asset", role="general", builtin_kind="uex",
                   argv=argv("preview", "--max-bytes", "{maxBytes}", "{asset}"),
                   description="Preview one asset by virtual path as JSON: class, every "
                               "serialized property, material parameters and references "
                               "(uex preview)",
                   properties={"asset": {"type": "string",
                                         "description": "virtual asset path"},
                               "maxBytes": {"type": "string", "default": "131072",
                                            "description": "cap on the JSON printed"}},
                   required=["asset"]),
        _operation(name="uex_material_shader_maps", role="general", builtin_kind="uex",
                   argv=argv("material-shader-maps", "--max-bytes", "8388608", "{asset}"),
                   description="Read compiled ShaderMap/uniform/preshader evidence for one "
                               "matched Unreal Material/MaterialInstance loose package using "
                               "the profile's concrete game version; no directory scan or game "
                               "compilation. When an instance has no inline variants, Bread "
                               "follows its observed Parent chain only inside the configured "
                               "complete asset root and returns materialChain, shaderMapOwner, "
                               "materialOverrideChain and chainDiagnostics. JSON is capped at "
                               "8 MiB; read the owner receipt stdoutPath "
                               "when inline stdout is truncated. Check succeeded, status, "
                               "readShaderMaps, coverage and diagnostics before interpreting "
                               "empty results. Compare platform/feature/quality variants and "
                               "trace typed preshader output offsets/components: parameter "
                               "array order and DefaultValueOffset are not GPU offsets. "
                               "Semantic names do not prove captured bytecode identity. "
                               "Normal preview may omit compiled resources when reading is disabled.",
                   properties={"asset": {"type": "string",
                                         "description": "one exact matched Unreal Material or "
                                                        "MaterialInstance .uasset path inside "
                                                        "the profile's unpacked root; no prefix, "
                                                        "directory, wildcard or archive"}},
                   required=["asset"]),
        _operation(name="uex_export", role="general", builtin_kind="uex",
                   argv=argv("export", "--only", "{only}"),
                   description="Export a virtual path prefix as FModel-style JSON/PNG. The "
                               "files land in the profile's own output directory (uex export "
                               "takes no --out); the run prints what it wrote",
                   properties={"only": {"type": "string",
                                        "description": "virtual path prefix to export"}},
                   required=["only"], timeout=300.0),
        _operation(name="uex_preview_texture", role="general", builtin_kind="uex",
                   argv=argv("preview-texture", "--out", "{outputDir}/preview.png", "{asset}"),
                   description="Decode one texture asset to PNG (uex preview-texture)",
                   properties={"asset": {"type": "string",
                                         "description": "virtual texture asset path"}},
                   required=["asset"], result="files-in-output-dir", min_files=1,
                   timeout=180.0),
        _operation(name="uex_extract_raw", role="general", builtin_kind="uex",
                   argv=argv("extract-raw", "--only", "{only}", "--out", "{outputDir}",
                             "--max-files", "{maxFiles}"),
                   description="Extract decrypted/decompressed .uasset/.uexp/.ubulk files for a "
                               "virtual path so the Host's own Unreal parsers can read them "
                               "(uex extract-raw)",
                   properties={"only": {"type": "string",
                                        "description": "virtual path or prefix to extract"},
                               "maxFiles": {"type": "string", "default": "2000",
                                            "description": "cap on files (0 = unlimited)"}},
                   required=["only"], result="files-in-output-dir", min_files=1,
                   timeout=300.0),
        _operation(name="uex_export_blender", role="general", builtin_kind="uex",
                   argv=argv("export-blender", "--out", "{outputDir}", "{asset}"),
                   description="Export a Static/SkeletalMesh with its LODs, materials and "
                               "textures as a Blender bundle (glTF + material JSON + PNG); the "
                               "bundle manifest is indexed by the asset catalog (uex export-blender)",
                   properties={"asset": {"type": "string",
                                         "description": "virtual mesh asset path"}},
                   required=["asset"], result="files-in-output-dir", min_files=1,
                   timeout=300.0),
        _operation(name="uex_relations", role="general", builtin_kind="uex",
                   argv=argv("relations", "--source", "{source}"),
                   description="Dependency edges of one asset: which materials, textures, "
                               "meshes it references or is referenced by (uex relations; "
                               "needs a prior uex scan)",
                   properties={"source": {"type": "string",
                                          "description": "object path, e.g. /Game/Chars/Hero.Hero"}},
                   required=["source"]),
        _operation(name="uex_discover", role="general", builtin_kind="uex",
                   argv=argv("discover"),
                   description="Validate the profile: probe UE version candidates, mappings and "
                               "AES keys against the mounted tree and report the confidence; in "
                               "unpacked mode a strong result pins the engine version for later "
                               "runs (uex discover)",
                   timeout=300.0),
    ]


def _assetstudio_operations(unity_version: str) -> list[dict[str, Any]]:
    extra = ["--unity-version", unity_version] if unity_version else []
    return [
        _operation(name="assetstudio_info", role="general", builtin_kind="assetstudio",
                   argv=["{path}", "-m", "info"] + extra,
                   description="Asset counts by type for one Unity file or folder "
                               "(AssetStudioModCLI -m info)",
                   properties={"path": {"type": "string",
                                        "description": "asset file, bundle or folder"}},
                   required=["path"]),
        _operation(name="assetstudio_export", role="general", builtin_kind="assetstudio",
                   argv=["{path}", "-m", "export", "-t", "{types}", "-g", "type",
                         "-o", "{outputDir}"] + extra,
                   description="Export shader text, material/MonoBehaviour dumps, "
                               "textures, sprites, text assets or meshes from one Unity "
                               "file or folder (AssetStudioModCLI -m export)",
                   properties={"path": {"type": "string",
                                        "description": "asset file, bundle or folder"},
                               "types": {"type": "string",
                                         "description": "comma list: shader, monoBehaviour, "
                                                        "tex2d, sprite, textAsset, mesh, "
                                                        "animator, audio, font, movieTexture, "
                                                        "videoClip, all",
                                         "default": "shader,textAsset,monoBehaviour,tex2d"}},
                   required=["path"], result="files-in-output-dir", min_files=1,
                   timeout=300.0),
        _operation(name="assetstudio_dump", role="general", builtin_kind="assetstudio",
                   argv=["{path}", "-m", "dump", "-t", "{types}", "-g", "type",
                         "-o", "{outputDir}"] + extra,
                   description="Dump serialized fields (materials, MonoBehaviours, "
                               "shader metadata) as text (AssetStudioModCLI -m dump)",
                   properties={"path": {"type": "string",
                                        "description": "asset file, bundle or folder"},
                               "types": {"type": "string", "default": "all"}},
                   required=["path"], result="files-in-output-dir", min_files=1,
                   timeout=300.0),
        _operation(name="assetstudio_filter_export", role="general",
                   builtin_kind="assetstudio",
                   argv=["{path}", "-m", "export", "-t", "{types}", "-g", "type",
                         "--filter-by-name", "{name}", "-o", "{outputDir}"] + extra,
                   description="Export only assets whose name contains a text "
                               "(AssetStudioModCLI --filter-by-name)",
                   properties={"path": {"type": "string"},
                               "name": {"type": "string",
                                        "description": "name fragment to match"},
                               "types": {"type": "string", "default": "all"}},
                   required=["path", "name"], result="files-in-output-dir", min_files=1,
                   timeout=300.0),
        _operation(name="assetstudio_container_export", role="general",
                   builtin_kind="assetstudio",
                   argv=["{path}", "-m", "export", "-t", "{types}", "-g", "type",
                         "--filter-by-container", "{container}", "-o", "{outputDir}"] + extra,
                   description="Export assets by container path (Assets/Characters/Hat/...) "
                               "from a bundle or folder (AssetStudioModCLI --filter-by-container)",
                   properties={"path": {"type": "string"},
                               "container": {"type": "string",
                                             "description": "container path fragment"},
                               "types": {"type": "string", "default": "all"}},
                   required=["path", "container"], result="files-in-output-dir", min_files=1,
                   timeout=300.0),
    ]


def _unitypy_operations(script: str, prefix: list[str] | None = None) -> list[dict[str, Any]]:
    # ``prefix`` replaces the script argument when the reader runs embedded in
    # the AgentHost executable (``replaylab-ai unpacker unitypy ...``).
    lead = list(prefix) if prefix is not None else [script]
    return [
        _operation(name="unitypy_describe", role="asset-reader", builtin_kind="unitypy",
                   argv=lead + ["describe", "{path}"], patterns=UNITY_PATTERNS,
                   description="One Unity file as JSON: object inventory by class with "
                               "names, containers and path ids (UnityPy)", timeout=60.0),
        _operation(name="unitypy_list", role="general", builtin_kind="unitypy",
                   argv=lead + ["describe", "{path}", "--limit", "{limit}"],
                   description="Inventory of one Unity file or bundle as JSON (UnityPy)",
                   properties={"path": {"type": "string"},
                               "limit": {"type": "integer", "default": 500}},
                   required=["path"]),
        _operation(name="unitypy_export", role="general", builtin_kind="unitypy",
                   argv=lead + ["export", "{path}", "--out", "{outputDir}",
                         "--types", "{types}", "--name", "{name}"],
                   description="Export shader text (.shader), material parameters "
                               "(.json), textures (.png) and text assets from one Unity "
                               "file or bundle (UnityPy)",
                   properties={"path": {"type": "string"},
                               "types": {"type": "string",
                                         "description": "comma list: Shader, Material, "
                                                        "Texture2D, TextAsset, "
                                                        "MonoBehaviour, Mesh",
                                         "default": "Shader,Material,TextAsset"},
                               "name": {"type": "string",
                                        "description": "only objects whose name contains "
                                                       "this (empty = all)",
                                        "default": ""}},
                   required=["path"], result="files-in-output-dir", min_files=1,
                   timeout=300.0),
    ]


def _umodel_operations(game_tag: str, aes_key: str) -> list[dict[str, Any]]:
    extra: list[str] = []
    if game_tag:
        extra.append("-game=%s" % game_tag)
    if aes_key:
        extra.append("-aes=%s" % aes_key)
    return [
        _operation(name="umodel_list", role="general", builtin_kind="umodel",
                   argv=["-list"] + extra + ["{path}"],
                   description="Objects and classes inside one Unreal package "
                               "(umodel -list)",
                   properties={"path": {"type": "string",
                                        "description": "package file (.upk/.uasset/.pak)"}},
                   required=["path"]),
        _operation(name="umodel_export", role="general", builtin_kind="umodel",
                   argv=["-export", "-out={outputDir}", "-png"] + extra + ["{path}"],
                   description="Export meshes (psk/gltf), animations and textures (png) "
                               "from one Unreal package (umodel -export)",
                   properties={"path": {"type": "string",
                                        "description": "package file (.upk/.uasset/.pak)"}},
                   required=["path"], result="files-in-output-dir", min_files=1,
                   timeout=300.0),
        _operation(name="umodel_export_object", role="general", builtin_kind="umodel",
                   argv=["-export", "-out={outputDir}", "-png"] + extra
                        + ["{path}", "{object}"],
                   description="Export one named object from a package (umodel -export "
                               "<package> <object>)",
                   properties={"path": {"type": "string"},
                               "object": {"type": "string",
                                          "description": "object name inside the package"}},
                   required=["path", "object"], result="files-in-output-dir",
                   min_files=1, timeout=300.0),
    ]


def builtin_unpacker_definition(kind: str, program: str,
                                options: Mapping[str, Any] | None = None, *,
                                enabled: bool = True,
                                offer_to_agent: bool = True) -> dict[str, Any]:
    """The complete tool record for one built-in unpacker: nothing left to discover."""

    spec = builtin_unpacker(kind)
    if spec is None:
        raise ValueError("unknown built-in unpacker: %s" % kind)
    command = str(program or "").strip()
    if not command:
        raise ValueError("%s needs the path to %s" % (spec["title"], spec["programHint"]))
    values = _clean_options(kind, options)
    if kind == "uex":
        operations = _uex_operations(values.get("configPath", ""), values.get("profile", ""))
        values["mode"] = "paks" if values.get("configPath") and values.get("profile") else "unpacked"
    elif kind == "assetstudio":
        operations = _assetstudio_operations(values.get("unityVersion", ""))
    elif kind == "unitypy":
        if values.get("embedded"):
            operations = _unitypy_operations("", prefix=EMBEDDED_UNITYPY_PREFIX)
        else:
            operations = _unitypy_operations(str(materialise_unitypy_script()))
    else:
        operations = _umodel_operations(values.get("gameTag", ""), values.get("aesKey", ""))
    roles = ["asset-reader"]
    if offer_to_agent:
        roles.append("general")
    return {
        "id": builtin_tool_id(kind),
        "title": spec["title"],
        "description": spec["reads"],
        "enabled": bool(enabled),
        "roles": roles,
        "connection": {"kind": "cli", "command": command, "args": [], "env": {}},
        "interface": {
            "source": "builtin:%s" % kind,
            "discoveredAt": _now(),
            "toolCount": len(operations),
            "operations": operations,
            "note": "Pre-configured recipe for %s (%s). Options: %s" % (
                spec["title"], spec["homepage"],
                ", ".join("%s=%s" % (key, value) for key, value in values.items()
                          if value not in ("", False)) or "none"),
        },
        "builtinKind": kind,
        "builtinOptions": values,
        "source": "bundled" if _is_bundled_program(command) else "registered",
    }


# ---------------------------------------------------------------------------
# Bundled tools: the four unpackers travel with the program.
#
# A deployed qrenderdoc carries them under ``<app>/replaylab/tools/unpackers/
# <kind>/``; a source checkout keeps the same layout under
# ``replaylab/third_party/unpackers/``. Registrations name that root with the
# ``%BREAD_TOOLS%`` token, so a copy of the application on another machine
# resolves them where *it* lives, and the AgentHost re-synchronises the
# bundled entries on every start (a bundle that appeared is registered, one
# that vanished is dropped, one that moved is corrected).
# ---------------------------------------------------------------------------
from .bundled_paths import (  # noqa: E402 - the token helpers live in one small module
    APP_ROOT_TOKEN, TOOLS_ROOT_TOKEN, app_root, bundled_tools_root, expand_tool_path, fold_tool_path)

EMBEDDED_UNITYPY_PREFIX = ["unpacker", "unitypy"]
BUNDLED_LAYOUT: dict[str, dict[str, Any]] = {
    # No profiles.json in the bundle: the Host generates the profile from the
    # project's unpacked root. A hand-written unpackers/uex/profiles.json (pak
    # mode, with keys) is honoured when present.
    "uex": {"programs": ["unpackers/uex/uex.exe"],
            "options": {"registerMcp": True}, "optionalConfig": "unpackers/uex/profiles.json"},
    "assetstudio": {"programs": ["unpackers/assetstudio/AssetStudioModCLI.exe"], "options": {}},
    "umodel": {"programs": ["unpackers/umodel/umodel_64.exe", "unpackers/umodel/umodel.exe"],
               "options": {}},
}


def _is_bundled_program(command: str) -> bool:
    return command.startswith(TOOLS_ROOT_TOKEN) or command.startswith(APP_ROOT_TOKEN)


def host_command() -> tuple[str, list[str]]:
    """How to start the AgentHost CLI from a registration (program, argv prefix)."""

    if getattr(sys, "frozen", False):
        return APP_ROOT_TOKEN + "/replaylab-ai.exe", []
    return str(Path(sys.executable)), ["-m", "replaylab_agent.cli"]


def _uex_profile_name(config_path: Path) -> str:
    try:
        import json

        payload = json.loads(config_path.read_text(encoding="utf-8"))
        profiles = payload.get("profiles") if isinstance(payload, dict) else None
        if isinstance(profiles, dict):
            for name in profiles:
                if _PROFILE_RE.fullmatch(str(name)):
                    return str(name)
    except (OSError, ValueError):
        pass
    return ""


def bundled_unpacker_programs() -> dict[str, dict[str, Any]]:
    """Built-in unpackers present in this installation's bundle (token paths)."""

    root = bundled_tools_root()
    found: dict[str, dict[str, Any]] = {}
    for kind, layout in BUNDLED_LAYOUT.items():
        for relative in layout["programs"]:
            candidate = root / relative
            if candidate.is_file():
                options = dict(layout.get("options") or {})
                if kind == "uex":
                    config = root / str(layout.get("optionalConfig") or "")
                    profile = _uex_profile_name(config) if config.is_file() else ""
                    if profile:
                        options["configPath"] = TOOLS_ROOT_TOKEN + "/" + str(layout["optionalConfig"])
                        options["profile"] = profile
                found[kind] = {"program": TOOLS_ROOT_TOKEN + "/" + relative, "options": options,
                               "resolvedProgram": str(candidate)}
                break
    # UnityPy runs inside the AgentHost when the package is importable there.
    try:
        import UnityPy  # noqa: F401  (also tells PyInstaller to bundle it)
    except Exception:  # noqa: BLE001 - optional dependency
        pass
    else:
        program, prefix = host_command()
        found["unitypy"] = {"program": program, "options": {"embedded": True},
                            "argvPrefix": prefix, "resolvedProgram": expand_tool_path(program)}
    return found


def bundled_unpacker_definition(kind: str, entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """The registration for one bundled unpacker, or None when it is not complete."""

    options = dict(entry.get("options") or {})
    definition = builtin_unpacker_definition(kind, str(entry["program"]), options)
    prefix = list(entry.get("argvPrefix") or [])
    if prefix:
        for operation in definition["interface"]["operations"]:
            operation["argv"] = prefix + list(operation["argv"])
    definition["source"] = "bundled"
    return definition


def _recipe_signature(tool: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Compare shipped interfaces without their per-registration proof timestamps."""

    return [{key: value for key, value in operation.items()
             if key not in {"proved", "knownFailures"}}
            for operation in (tool.get("interface") or {}).get("operations") or []]


def sync_bundled_unpackers(path: Path | str | None = None) -> dict[str, Any]:
    """Make the registry reflect the bundle: add what is there, drop what is gone.

    A user's program path and options stay theirs. Shipped interfaces are refreshed
    when their recipes change, including registrations of a user-supplied executable.
    """

    from . import agent_tool_providers as registry

    present = bundled_unpacker_programs()
    registered, unused_diagnostics = registry.load_tools(path)
    added: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    skipped: dict[str, str] = {}
    for index, current in enumerate(registered):
        kind = str(current.get("builtinKind") or "")
        interface = current.get("interface") or {}
        if not builtin_unpacker(kind):
            continue
        # A failed generic probe used to erase bundled recipes completely. Recover
        # those empty records too; leave a separately proved/custom interface alone.
        if interface.get("source") != "builtin:%s" % kind and not (
                current.get("source") == "bundled" and not interface.get("operations")):
            continue
        fresh = builtin_unpacker_definition(
            kind, str(current["connection"]["command"]), current.get("builtinOptions"),
            enabled=bool(current.get("enabled", True)),
            offer_to_agent="general" in current.get("roles", []))
        if current.get("source") == "bundled" and kind in present:
            prefix = list(present[kind].get("argvPrefix") or [])
            for operation in fresh["interface"]["operations"]:
                operation["argv"] = prefix + operation["argv"]
        fresh = registry.validate_tool(fresh)
        if _recipe_signature(current) == _recipe_signature(fresh):
            continue
        refreshed = dict(current)
        refreshed["interface"] = fresh["interface"]
        registry.upsert_tool(refreshed, path)
        registered[index] = refreshed
        updated.append(kind)
    by_kind = {str(tool.get("builtinKind")): tool for tool in registered if tool.get("builtinKind")}
    for kind, entry in present.items():
        definition = bundled_unpacker_definition(kind, entry)
        if definition is None:
            skipped[kind] = "incomplete bundle"
            continue
        current = by_kind.get(kind)
        if current is None:
            registry.upsert_tool(definition, path)
            added.append(kind)
        elif current.get("source") == "bundled" and (
                registry.map_tool_paths(current, fold_tool_path)["connection"]["command"]
                != registry.map_tool_paths(definition, fold_tool_path)["connection"]["command"]
                or registry.map_tool_paths(current, fold_tool_path)["builtinOptions"]
                != registry.map_tool_paths(definition, fold_tool_path)["builtinOptions"]):
            definition["enabled"] = bool(current.get("enabled", True))
            registry.upsert_tool(definition, path)
            if kind not in updated:
                updated.append(kind)
        if kind == "uex" and entry.get("options", {}).get("registerMcp"):
            _sync_uex_mcp_server(str(entry["program"]), str(entry["options"].get("configPath") or ""))
    for kind, tool in by_kind.items():
        if tool.get("source") == "bundled" and kind not in present:
            registry.remove_tool(str(tool["id"]), path)
            removed.append(kind)
    return {"schema": "replaylab-bundled-unpackers/v1", "toolsRoot": str(bundled_tools_root()),
            "present": sorted(present), "added": added, "updated": updated, "removed": removed,
            "skipped": skipped}


def generated_uex_profiles_path() -> Path:
    """The profiles.json the Host writes for unpacked trees (per user, outside the program)."""

    override = os.environ.get("REPLAYLAB_UEX_PROFILES")
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    base = Path(local).expanduser() if local else Path.home() / ".local" / "share"
    return base / "ReplayLab" / "tools" / "uex" / "profiles.json"


def _profiles_file_has_profiles(path: Path) -> bool:
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(payload, dict) and bool(payload.get("profiles"))
    except (OSError, ValueError):
        return False


def _sync_uex_mcp_server(program: str, pak_config: str) -> None:
    """Attach uex's MCP face when it can start: uex refuses a config without profiles,
    so the generated file is used only once the Host has written a profile into it."""

    try:
        from .agent_tools_config import AgentToolsConfig

        config_path = pak_config or ""
        if not config_path:
            generated = generated_uex_profiles_path()
            if not _profiles_file_has_profiles(generated):
                return
            config_path = str(generated)
        config = AgentToolsConfig.load()
        wanted = uex_mcp_server_definition(program, config_path)
        current = next((server for server in config.mcp_servers if server.id == "uex-mcp"), None)
        if current is not None:
            same = (fold_tool_path(str(current.command)) == fold_tool_path(wanted["command"])
                    and [fold_tool_path(str(item)) for item in current.args]
                    == [fold_tool_path(str(item)) for item in wanted["args"]])
            if same:
                return
            wanted["enabled"] = bool(getattr(current, "enabled", True))
        config.upsert_mcp_server(wanted)
        config.save()
    except Exception:  # noqa: BLE001 - the MCP face is a convenience
        pass


def uex_mcp_server_definition(program: str, config_path: str) -> dict[str, Any]:
    """The MCP face of uex: the same executable, started with ``mcp``."""

    return {
        "id": "uex-mcp",
        "title": "uex MCP server (Unreal)",
        "transport": "stdio",
        "command": str(program).strip(),
        "args": ["--config", str(config_path).strip(), "mcp"],
        "env": {},
        "enabled": True,
        "note": "uex over MCP: list_dir, search_paths, preview_asset, preview_texture, "
                "inventories, relations and jobs on the mounted tree; pass the profile "
                "profile_unpacked_asset_directories reports (unpacked-<sha8>)",
    }


def builtin_unpacker_rows(registered: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """What Settings lists: every built-in kind, configured or still waiting for a path."""

    by_id = {str(item.get("id", "")): item for item in registered}
    rows: list[dict[str, Any]] = []
    for spec in BUILTIN_UNPACKERS:
        tool = by_id.get(builtin_tool_id(spec["kind"]))
        rows.append({
            "kind": spec["kind"],
            "id": builtin_tool_id(spec["kind"]),
            "title": spec["title"],
            "engines": list(spec["engines"]),
            "patterns": list(spec["patterns"]),
            "reads": spec["reads"],
            "homepage": spec["homepage"],
            "programHint": spec["programHint"],
            "programNames": list(spec["programNames"]),
            "options": [dict(item) for item in spec["options"]],
            "configured": tool is not None,
            "program": str((tool or {}).get("connection", {}).get("command", "")),
            "enabled": bool((tool or {}).get("enabled", True)),
            "values": dict((tool or {}).get("builtinOptions") or {}),
        })
    return rows


__all__ = [
    "BUILTIN_UNPACKERS",
    "BUILTIN_UNPACKER_KINDS",
    "UNITY_PATTERNS",
    "UNREAL_COOKED_PATTERNS",
    "UNREAL_LEGACY_PATTERNS",
    "builtin_tool_id",
    "builtin_unpacker",
    "builtin_unpacker_definition",
    "builtin_unpacker_rows",
    "materialise_unitypy_script",
    "uex_mcp_server_definition",
    "unitypy_script_path",
]
