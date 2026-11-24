"""Engine-aware light extractors for unpacked asset trees.

Each extractor reads one cooked/serialized asset with a small, format-tolerant
parser and returns the same normalized shape the generic asset catalog stores:
``(metadata, references)``.  The goal is retrieval evidence — object class,
real names, referenced assets, shader type names — not a complete engine
loader.  Deep layouts (frozen shader maps, compressed shader archives) stay
with admitted providers; when they are absent the retriever and the AI still
have names and relations to work from.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from . import unreal_package

ENGINE_UNKNOWN = "unknown"
ENGINE_UNREAL = "unreal"
ENGINE_UNITY = "unity"
ENGINE_GODOT = "godot"

_UNITY_MARKERS = {"globalgamemanagers", "resources.assets", "sharedassets0.assets", "level0"}
_UNITY_SUFFIXES = {".assets", ".unity3d", ".bundle", ".resS"}
_GODOT_SUFFIXES = {".pck", ".tscn", ".tres", ".gd"}


class EngineContext:
    """What the scan learned about the tree before parsing individual files."""

    def __init__(self, engine: str, details: Mapping[str, Any], mounts: Mapping[str, str],
                 stem_index: Mapping[str, str] | None = None):
        self.engine = engine
        self.details = dict(details)
        self.mounts = dict(mounts)
        # stem (casefolded) -> unique relative path; lets the generic probe turn
        # a bare name found inside any file into a relation, on any engine.
        self.stem_index = dict(stem_index or {})

    def to_dict(self) -> dict[str, Any]:
        return {"engine": self.engine, "details": dict(self.details), "mounts": dict(self.mounts)}


def detect_engine(relative_paths: Iterable[str]) -> EngineContext:
    """Classify the tree from file names alone (cheap, deterministic)."""

    paths = [PurePosixPath(str(item)) for item in relative_paths]
    stem_index: dict[str, str] = {}
    stem_dirs: dict[str, str] = {}
    ambiguous: set[str] = set()
    preferred = {".uasset", ".mat", ".material", ".prefab", ".asset", ".json", ".png", ".dds", ".tga"}
    for path in paths:
        stem = path.stem.casefold()
        if len(stem) < 6 or stem in ambiguous:
            continue
        directory = path.parent.as_posix().casefold()
        if stem in stem_dirs and stem_dirs[stem] != directory:
            # the same name in two places cannot be resolved by name alone
            stem_index.pop(stem, None)
            stem_dirs.pop(stem, None)
            ambiguous.add(stem)
            continue
        stem_dirs[stem] = directory
        if stem not in stem_index or path.suffix.casefold() in preferred:
            stem_index[stem] = path.as_posix()
    suffix_counts: dict[str, int] = {}
    uprojects: list[PurePosixPath] = []
    uplugins: list[PurePosixPath] = []
    engine_content = False
    unity_markers = 0
    godot_project = False
    for path in paths:
        suffix = path.suffix.casefold()
        suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1
        if suffix == ".uproject":
            uprojects.append(path)
        elif suffix == ".uplugin":
            uplugins.append(path)
        elif path.name == "project.godot":
            godot_project = True
        if path.name.casefold() in _UNITY_MARKERS:
            unity_markers += 1
        if len(path.parts) >= 2 and path.parts[-2:] == ("Engine", "Content") or (
            "Engine" in path.parts and "Content" in path.parts and path.parts.index("Engine") + 1 < len(path.parts)
            and path.parts[path.parts.index("Engine") + 1] == "Content"
        ):
            engine_content = True
    uasset_count = suffix_counts.get(".uasset", 0) + suffix_counts.get(".umap", 0)
    unity_score = unity_markers + sum(suffix_counts.get(item, 0) for item in _UNITY_SUFFIXES)
    godot_score = (3 if godot_project else 0) + sum(suffix_counts.get(item, 0) for item in _GODOT_SUFFIXES)
    if uprojects or uasset_count >= 8:
        mounts = unreal_package.detect_mounts(paths, uprojects, uplugins)
        return EngineContext(
            ENGINE_UNREAL,
            {
                "uassetCount": uasset_count,
                "uprojects": [item.as_posix() for item in uprojects[:8]],
                "pluginCount": len(uplugins),
                "engineContentPresent": engine_content,
                "shaderArchiveCount": suffix_counts.get(".ushaderbytecode", 0),
            },
            mounts,
            stem_index,
        )
    if unity_score >= 2:
        return EngineContext(ENGINE_UNITY, {"unityScore": unity_score}, {}, stem_index)
    if godot_score >= 3:
        return EngineContext(ENGINE_GODOT, {"godotScore": godot_score}, {}, stem_index)
    return EngineContext(ENGINE_UNKNOWN, {"suffixCounts": dict(sorted(suffix_counts.items())[:64])}, {}, stem_index)


def parse_engine_asset(
    context: EngineContext | None, relative: str, data: bytes, sibling_class: str = ""
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Dispatch one file to the engine extractor; None means 'not handled'.

    ``sibling_class`` is the class of the package header that owns a cooked
    export file (known because the ``.uasset`` sorts before its ``.uexp``);
    it selects the texture fingerprint path for Texture2D exports.
    """

    suffix = PurePosixPath(relative).suffix.casefold()
    if suffix in {".uasset", ".umap"}:
        mounts = context.mounts if context is not None else {}
        return unreal_package.parse_package(data, relative, mounts)
    if suffix == ".uexp":
        return unreal_package.parse_export_data(data, relative, sibling_class=sibling_class)
    return None


def engine_kind_for_suffix(suffix: str) -> str | None:
    if suffix in {".uasset", ".umap", ".uexp"}:
        return "package"
    return None
