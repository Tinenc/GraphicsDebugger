"""uex profiles generated from the project's unpacked asset root.

uex (CUE4Parse) needs a *profile*: a game version, a directory to mount, and --
for shipped ``.pak`` / IoStore containers -- AES keys and ``.usmap`` mappings.
None of the last part applies here. The tree Bread AI works on is already
unpacked: decrypted, decompressed ``.uasset`` / ``.uexp`` / ``.ubulk`` files on
disk. CUE4Parse mounts such a directory directly (every known-extension file
becomes an ``OsGameFile`` keyed by its path relative to the mounted directory),
so the only per-project facts are *which directory* and *which engine version*.

Nobody should have to write that by hand. This module keeps one machine-wide
``profiles.json`` (per user, outside the program directory, which may be
read-only or shared) with one profile per unpacked mount root, named
``unpacked-<sha8>``; the Host fills ``{uexConfig}`` / ``{uexProfile}`` /
``{uexPath}`` in a uex recipe from it, and uex's own ``discover`` picks the
best version among the candidates written here. Once ``discover`` reports a
strong result the winning version is pinned so later invocations mount once
instead of trying every candidate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "replaylab-uex-profiles/v1"
PROFILE_PREFIX = "unpacked-"
ENV_PROFILES_PATH = "REPLAYLAB_UEX_PROFILES"
MAX_MOUNT_DEPTH = 3
# Fallback order when nothing could be read from a package header: newest first,
# because a cook that cannot be identified is far more likely to be recent.
GENERIC_VERSION_CANDIDATES = ("latest", "GAME_UE5_4", "GAME_UE5_1", "GAME_UE4_27", "GAME_UE4_25")

# The engine version is not guessed and not brute-forced: every cooked package says
# it in its own header. FPackageFileSummary carries the UE4 and UE5 object versions,
# and CUE4Parse maps each EGame to exactly one such pair (EGame.GetVersion). These
# tables are that mapping read backwards -- observed version -> the newest EGame
# that writes it. Ranges, because a version number is bumped mid-release and every
# engine release between two bumps writes the same number.
_UE5_VERSION_TO_GAME: tuple[tuple[int, int, str], ...] = (
    (1000, 1002, "GAME_UE5_0"),      # early access cooks
    (1003, 1004, "GAME_UE5_0"),
    (1005, 1008, "GAME_UE5_1"),
    (1009, 1009, "GAME_UE5_3"),      # 5.2 and 5.3 share it; the newer one is the better guess
    (1010, 1012, "GAME_UE5_4"),
    (1013, 1016, "GAME_UE5_5"),
    (1017, 1019, "GAME_UE5_6"),
)
_UE4_VERSION_TO_GAME: dict[int, str] = {
    342: "GAME_UE4_0", 352: "GAME_UE4_1", 363: "GAME_UE4_2", 382: "GAME_UE4_3",
    385: "GAME_UE4_4", 401: "GAME_UE4_5", 413: "GAME_UE4_6", 434: "GAME_UE4_7",
    451: "GAME_UE4_8", 482: "GAME_UE4_10", 498: "GAME_UE4_11", 504: "GAME_UE4_12",
    505: "GAME_UE4_13", 508: "GAME_UE4_14", 510: "GAME_UE4_15", 513: "GAME_UE4_17",
    514: "GAME_UE4_18", 516: "GAME_UE4_20", 517: "GAME_UE4_23", 518: "GAME_UE4_25",
    522: "GAME_UE4_27",
}
MAX_VERSION_SAMPLES = 6
MAX_VERSION_SAMPLE_BYTES = 256 * 1024
_GAME_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,63}$")
_SKIP_DIRS = {"engine", "_to_delete", "_rl_samples", "analysis", "blender", ".git", "__pycache__"}


def profiles_path() -> Path:
    """Where the generated profiles live: per user, never inside the program."""

    override = os.environ.get(ENV_PROFILES_PATH)
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    base = Path(local).expanduser() if local else Path.home() / ".local" / "share"
    return base / "ReplayLab" / "tools" / "uex" / "profiles.json"


def profile_name_for(mount_root: Path | str) -> str:
    key = os.path.normcase(os.path.normpath(str(mount_root)))
    return PROFILE_PREFIX + hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:8]


def locate_mount_root(asset_root: Path) -> Path:
    """The directory CUE4Parse should mount. Normally the project's own unpacked path.

    That path is the answer whenever it already looks like a mount point -- its
    children are ``<Game>/Content``. It is not when the unpack put the tree one
    level down (extract-raw writes ``raw/``, FModel writes ``Exports/``), and
    then mounting the configured path would leave every ``/Game/...`` reference
    unresolvable, because CUE4Parse takes the game name from the first segment
    before ``Content/``. So: the configured path if it works, otherwise the
    shallowest directory under it that does.
    """

    root = Path(asset_root)
    best: tuple[int, int, Path] | None = None
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        directory, depth = queue.pop(0)
        try:
            children = [child for child in directory.iterdir() if child.is_dir() and not child.is_symlink()]
        except OSError:
            continue
        names = {child.name.casefold(): child for child in children}
        game_dirs = [child for child in children
                     if child.name.casefold() not in _SKIP_DIRS and (child / "Content").is_dir()]
        if game_dirs:
            score = (depth, 0 if "engine" in names else 1)
            if best is None or score < best[:2]:
                best = (score[0], score[1], directory)
        if depth < MAX_MOUNT_DEPTH:
            for child in children:
                if child.name.casefold() in _SKIP_DIRS or child.name.startswith("."):
                    continue
                queue.append((child, depth + 1))
    return best[2] if best is not None else root


def guess_game_name(mount_root: Path) -> str:
    """The project name CUE4Parse would derive (``<Name>/Content``), or ``""``."""

    try:
        candidates = sorted(
            child.name for child in Path(mount_root).iterdir()
            if child.is_dir() and child.name.casefold() not in _SKIP_DIRS and (child / "Content").is_dir())
    except OSError:
        return ""
    for name in candidates:
        if _GAME_NAME_RE.fullmatch(name):
            return name
    return ""


def version_candidates(game_name: str, engine_hint: str = "") -> list[str]:
    """Ordered EGame candidates for uex discover; invalid names are skipped by uex.

    Most specific first: the project's own EGame if CUE4Parse has one, then what
    the package headers said, then newest-to-oldest generic versions.
    """

    found: list[str] = []
    if game_name:
        # CUE4Parse ships game-specific versions named after the project
        # (GAME_SilverPalace, GAME_Palworld, ...) that carry custom serialization.
        # An unknown one costs one rejected attempt, never a wrong mount.
        found.append("GAME_" + game_name)
    hint = str(engine_hint or "").strip()
    if hint and hint not in found:
        found.append(hint)
    for item in GENERIC_VERSION_CANDIDATES:
        if item not in found:
            found.append(item)
    return found


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"profiles": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
        return {"profiles": {}}
    return payload


def _save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["schema"] = SCHEMA
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    temporary = path.with_name(path.name + ".tmp-%d" % os.getpid())
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def ensure_profile(asset_root: Path | str, *, engine_hint: str = "",
                   path: Path | None = None, detect: bool = True) -> dict[str, Any]:
    """Make sure a profile exists for this unpacked tree and return its facts.

    The two per-project facts are taken, not asked for: the mount directory comes
    from the project's configured unpacked path, and the engine version is read
    out of a cooked package header. An existing profile keeps its pinned version
    and anything the user added (``usmap``, ``keys``); only a changed mount root
    is rewritten.
    """

    root = Path(asset_root).resolve()
    mount = locate_mount_root(root)
    name = profile_name_for(mount)
    config = Path(path) if path is not None else profiles_path()
    payload = _load(config)
    profiles: dict[str, Any] = payload["profiles"]
    game_name = guess_game_name(mount)
    existing = profiles.get(name) if isinstance(profiles.get(name), dict) else None
    work_dir = config.parent / name
    detection: dict[str, Any] = {}
    if existing is None or os.path.normcase(str(existing.get("paksDir", ""))) != os.path.normcase(str(mount)):
        detection = detect_engine_version(mount) if detect else {}
        candidates = version_candidates(game_name, engine_hint or str(detection.get("version") or ""))
        record: dict[str, Any] = {
            "game": candidates[0],
            "versions": candidates[1:],
            "paksDir": str(mount),
            "usmap": None,
            "keys": [],
            "outputDir": str(work_dir / "export"),
            "analysisDir": str(work_dir / "analysis"),
            "assetRoot": str(root),
            "gameName": game_name,
            "pinned": False,
            "detectedVersion": str(detection.get("version") or ""),
            "detectedFrom": detection.get("evidence") or [],
        }
        profiles[name] = record
        _save(config, payload)
        existing = record
    return {
        "configPath": str(config),
        "profile": name,
        "mountRoot": str(mount),
        "mountRootSource": ("configured" if Path(mount) == root else "found under the configured root"),
        "assetRoot": str(root),
        "gameName": str(existing.get("gameName") or game_name),
        "pinned": bool(existing.get("pinned")),
        "game": str(existing.get("game") or ""),
        "versions": list(existing.get("versions") or []),
        "detectedVersion": str(existing.get("detectedVersion") or ""),
        "detection": detection,
    }


def set_version(profile: str, version: str, *, path: Path | None = None,
                pinned: bool = True) -> dict[str, Any]:
    """Use this engine version and stop trying others (an AI's or the user's answer)."""

    wanted = str(version or "").strip()
    if not wanted or not re.fullmatch(r"[A-Za-z0-9._]{1,64}", wanted):
        raise ValueError("engine version must be a CUE4Parse EGame name (GAME_UE5_4), "
                         "a number (5.4) or 'latest'")
    config = Path(path) if path is not None else profiles_path()
    payload = _load(config)
    record = payload["profiles"].get(profile)
    if not isinstance(record, dict):
        raise ValueError("unknown uex profile: %s" % profile[:64])
    record["game"] = wanted
    record["versions"] = []
    record["pinned"] = bool(pinned)
    _save(config, payload)
    return {"profile": profile, "game": wanted, "pinned": bool(pinned),
            "configPath": str(config)}


def record_discovery(profile: str, discovery: Mapping[str, Any] | str,
                     *, path: Path | None = None) -> dict[str, Any]:
    """Pin the version uex discover found strong; forget the other candidates.

    ``discovery`` is uex's JSON (``{"version": ..., "confidence": ...}``) or the
    raw stdout that contains it. Anything else leaves the profile untouched.
    """

    config = Path(path) if path is not None else profiles_path()
    if isinstance(discovery, str):
        try:
            start = discovery.index("{")
            parsed = json.loads(discovery[start:])
        except (ValueError, TypeError):
            return {"pinned": False, "reason": "no discovery JSON"}
    else:
        parsed = dict(discovery)
    if not isinstance(parsed, dict):
        return {"pinned": False, "reason": "no discovery JSON"}
    version = str(parsed.get("version") or "").strip()
    confidence = str(parsed.get("confidence") or "").strip().lower()
    if not version or confidence != "strong":
        return {"pinned": False, "version": version, "confidence": confidence}
    payload = _load(config)
    record = payload["profiles"].get(profile)
    if not isinstance(record, dict):
        return {"pinned": False, "reason": "unknown profile"}
    if record.get("pinned") and record.get("game") == version:
        return {"pinned": True, "version": version, "changed": False}
    record["game"] = version
    record["versions"] = []
    record["pinned"] = True
    _save(config, payload)
    return {"pinned": True, "version": version, "changed": True}


def game_for_package_versions(file_version_ue4: int, file_version_ue5: int) -> str:
    """The EGame that writes these object versions, or ``""`` when nothing matches."""

    ue5 = int(file_version_ue5 or 0)
    if ue5 > 0:
        for low, high, game in _UE5_VERSION_TO_GAME:
            if low <= ue5 <= high:
                return game
        return "GAME_UE5_LATEST" if ue5 > _UE5_VERSION_TO_GAME[-1][1] else ""
    ue4 = int(file_version_ue4 or 0)
    if ue4 <= 0:
        return ""
    if ue4 in _UE4_VERSION_TO_GAME:
        return _UE4_VERSION_TO_GAME[ue4]
    # An unlisted number still places the cook: take the newest listed one below it.
    below = [value for value in sorted(_UE4_VERSION_TO_GAME) if value <= ue4]
    return _UE4_VERSION_TO_GAME[below[-1]] if below else ""


def detect_engine_version(mount_root: Path | str, *,
                          samples: int = MAX_VERSION_SAMPLES) -> dict[str, Any]:
    """Read the engine version out of a few cooked package headers.

    This is the "try one asset" answer, done deterministically: a ``.uasset``
    states its object versions in its own summary, so a handful of files decide
    the version with no mounting and no trial and error. Disagreement is kept in
    the receipt rather than averaged away -- a tree with two cooks is something
    the caller should see.
    """

    from .symbols.engines import unreal_package

    root = Path(mount_root)
    seen: dict[str, int] = {}
    evidence: list[dict[str, Any]] = []
    read = 0
    for path in _sample_packages(root, samples):
        try:
            with path.open("rb") as stream:
                summary = unreal_package.read_summary(stream.read(MAX_VERSION_SAMPLE_BYTES))
        except (OSError, unreal_package.UnrealPackageError, ValueError):
            continue
        read += 1
        game = game_for_package_versions(summary.get("fileVersionUE4", 0),
                                         summary.get("fileVersionUE5", 0))
        if not game:
            continue
        seen[game] = seen.get(game, 0) + 1
        if len(evidence) < 4:
            evidence.append({
                "file": _relative_text(root, path),
                "fileVersionUE4": summary.get("fileVersionUE4", 0),
                "fileVersionUE5": summary.get("fileVersionUE5", 0),
                "game": game,
            })
    if not seen:
        return {"version": "", "readPackages": read, "evidence": evidence,
                "reason": ("no cooked package header could be read here; the files may be "
                           "IoStore/Zen packages, still packed, or this is not an Unreal tree")}
    ordered = sorted(seen.items(), key=lambda item: (-item[1], item[0]))
    return {
        "version": ordered[0][0],
        "agreement": "%d/%d" % (ordered[0][1], sum(seen.values())),
        "alsoSeen": [name for name, unused in ordered[1:]],
        "readPackages": read,
        "evidence": evidence,
    }


def _sample_packages(root: Path, limit: int) -> list[Path]:
    """A few ``.uasset`` files from different folders, cheaply."""

    found: list[Path] = []
    directories = 0
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = sorted(name for name in directory_names
                                    if name.casefold() not in _SKIP_DIRS and not name.startswith("."))
        directories += 1
        for name in sorted(file_names):
            if name.casefold().endswith((".uasset", ".umap")):
                found.append(Path(directory) / name)
                break  # one per folder: two files in one folder say the same thing
        if len(found) >= limit or directories > 4096:
            break
    return found[:limit]


def _relative_text(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def uex_relative_path(mount_root: Path | str, file_path: Path | str) -> str:
    """The key CUE4Parse gives a loose file: its path relative to the mounted directory."""

    try:
        relative = Path(file_path).resolve().relative_to(Path(mount_root).resolve())
    except (OSError, ValueError):
        return PurePosixPath(str(file_path).replace("\\", "/")).name
    return relative.as_posix()


def has_profiles(path: Path | None = None) -> bool:
    config = Path(path) if path is not None else profiles_path()
    return bool(_load(config)["profiles"]) if config.is_file() else False


__all__ = [
    "ENV_PROFILES_PATH",
    "detect_engine_version",
    "game_for_package_versions",
    "set_version",
    "PROFILE_PREFIX",
    "SCHEMA",
    "ensure_profile",
    "guess_game_name",
    "has_profiles",
    "locate_mount_root",
    "profile_name_for",
    "profiles_path",
    "record_discovery",
    "uex_relative_path",
    "version_candidates",
]
