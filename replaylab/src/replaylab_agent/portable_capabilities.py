"""Application-owned capabilities shared by API and subscription connections."""
from __future__ import annotations

import json
from functools import lru_cache
import os
from pathlib import Path
import sys

from .bundled_paths import app_root


def bundle_root() -> Path:
    override = os.environ.get("BREAD_CAPABILITIES_ROOT")
    if override:
        return Path(override).resolve()
    try:
        policy = json.loads((app_root() / "bread-distribution.json").read_text(encoding="utf-8-sig"))
        relative = policy.get("capabilitiesDirectory")
        if isinstance(relative, str):
            target = (app_root() / relative).resolve()
            if not Path(relative).is_absolute() and target.is_relative_to(app_root().resolve()):
                return target
    except (OSError, ValueError):
        pass
    if getattr(sys, "frozen", False):
        return app_root() / "tools/capabilities"
    source = Path(__file__).resolve().parents[2]
    try:
        lock = json.loads((source / "dependencies/inputs.lock.json").read_text(encoding="utf-8"))
        record = next(r for r in lock["archives"] if r["id"] == "capabilities")
        digest = record["sha256"]
        restored_root = source / "output/build-inputs/expanded/capabilities"
        for generation in (digest, digest[:20]):
            restored = restored_root / generation
            if (restored / "bread-capabilities.json").is_file():
                return restored
    except (OSError, ValueError, KeyError, StopIteration):
        pass
    # Retain development installs prepared by the former import command.
    return source / "output/portable-capabilities"


def manifest() -> dict:
    # Keep old installed bytes available for rollback without re-enabling extras.
    # A user-configured MCP may opt in explicitly with its own capabilities root.
    if not os.environ.get("BREAD_CAPABILITIES_ROOT"):
        try:
            policy = json.loads((app_root() / "bread-distribution.json").read_text(encoding="utf-8-sig"))
            if policy.get("profile") == "core":
                return {}
        except (OSError, ValueError):
            pass
    path = bundle_root() / "bread-capabilities.json"
    try:
        stat = path.stat()
        return _manifest(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return {}


def require_components(*names: str) -> None:
    from .capability_store import ensure_component
    root, metadata = bundle_root(), manifest()
    for name in names:
        ensure_component(root, metadata, name)


def prepare_server(server: dict) -> None:
    """Expand a bundled native client only when it is actually attached."""
    root = bundle_root().resolve()
    command = Path(str(server.get("command", ""))).resolve()
    if command == root / "node/bin/node.exe":
        require_components("node")
    elif command == root / "provider-runtime/node.exe":
        require_components("provider-runtime")


@lru_cache(maxsize=4)
def _manifest(path: str, modified: int, size: int) -> dict:
    del modified, size
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return result if result.get("schema") == "bread-portable-capabilities/v1" else {}


def skill_roots() -> tuple[Path, ...]:
    root = bundle_root().resolve()
    values = []
    for relative in manifest().get("skillRoots", []):
        candidate = (root / relative).resolve()
        if candidate.is_relative_to(root) and candidate.is_dir():
            values.append(candidate)
    # Bread adapters replace host-private entrypoints while original plugin
    # resources and manuals remain in the bundle for progressive loading.
    adapters = app_root() / "portable-skills" if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2] / "portable_skills"
    if values and manifest().get("features", {}).get("portableTools", True) and adapters.is_dir():
        values.append(adapters)
    return tuple(values)


def server_definition(*, execution_enabled: bool = False) -> dict | None:
    if not manifest() or manifest().get("features", {}).get("portableTools") is False:
        return None
    command = str(Path(sys.executable).resolve())
    args = ["-I", "-m", "replaylab_agent.portable_tools"] if getattr(sys, "frozen", False) else ["-m", "replaylab_agent.portable_tools"]
    env = {"PYINSTALLER_RESET_ENVIRONMENT": "1", "BREAD_CAPABILITIES_ROOT": str(bundle_root())}
    if not getattr(sys, "frozen", False):
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return {"id": "bread-portable", "transport": "stdio", "command": command,
            "args": args + (["--allow-code"] if execution_enabled else []), "env": env,
            "source": "application-bundle", "startup_timeout_sec": 30, "tool_timeout_sec": 180,
            "approvalRules": {"default": "approve", "tools": {}}}


def public_status() -> dict:
    root = bundle_root()
    value = manifest()
    if value.get("distributionProfile") == "rdc":
        return {"present": True, "ready": True, "missing": [], "plugins": 0,
                "packagedComponents": [], "features": value["features"],
                "retainedCompatibilitySkills": value["retainedCompatibilitySkills"]}
    required = ["python/python.exe", "node/bin/node.exe",
                "python/Lib/site-packages/docx/__init__.py", "python/Lib/site-packages/openpyxl/__init__.py",
                "python/Lib/site-packages/pptx/__init__.py", "python/Lib/site-packages/pypdf/__init__.py",
                "python/Lib/site-packages/playwright/__init__.py"]
    if value.get("features", {}).get("office", True):
        required.append("office/program/soffice.exe")
    from .capability_store import declared_file
    missing = [name for name in required if not declared_file(root, value, name)]
    browsers = [r["path"] for r in value.get("files", []) if r["path"].startswith("browsers/chromium-")
                and r["path"].endswith("/chrome-win/chrome.exe")]
    if value.get("features", {}).get("browser", True) and not any((root / "browsers").glob("chromium-*/chrome-win/chrome.exe")) and not any(
            declared_file(root, value, name) for name in browsers):
        missing.append("browsers/chromium-*/chrome-win/chrome.exe")
    return {"present": bool(value), "ready": bool(value) and not missing,
            "missing": missing, "plugins": len(value.get("plugins", [])),
            "python": str(root / "python/python.exe"), "node": str(root / "node/bin/node.exe"),
            "originalPlugins": str(root / ("p" if (root / "p").is_dir() else "plugins")),
            "packagedComponents": [r["id"] for r in value.get("componentArchives", [])],
            "office": str(root / "office/program/soffice.exe"),
            "browser": str(root / "browsers"),
            "features": value.get("features", {"office": True}),
            "scriptExecutionRequires": "explicit-local-execution-preference",
            "nativeServices": "require-live-provider-handshake; provider-name-alone-is-not-readiness"}


def provider_servers(*, excluded_ids=(), policies=None) -> tuple[dict, ...]:
    """Activate a bundled native client only when its live service is advertised.

    A provider login alone cannot manufacture a desktop service/IPC pipe.
    Environment values are per-process connection state and never serialized.
    """
    root = bundle_root()
    excluded = {str(value).replace("_", "-").casefold() for value in excluded_ids}
    policies = {str(key).replace("_", "-").casefold(): value for key, value in (policies or {}).items()}
    plugins = {item["id"].split("@")[0]: root / item["path"] for item in manifest().get("plugins", [])}
    servers = []
    pipe = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH", "")
    code = plugins.get("codex-app-tools", root / "missing") / "server.mjs"
    if pipe and code.is_file() and "codex-app" not in excluded:
        # Retain the plugin's action-specific approval contract. A bundled
        # client is not permission to create chats, send messages or schedule
        # work through a bridge with no live approval broker.
        rules = {"default": "approve", "tools": {name: {"approval_mode": "prompt"} for name in (
            "automation_update", "create_thread", "send_message_to_thread", "fork_thread",
            "handoff_thread", "move_thread_to_project")}}
        servers.append({"id": "codex-app", "transport": "stdio", "command": str(root / "node/bin/node.exe"),
                        "args": [str(code)], "env": {"CODEX_APP_TOOLS_PIPE_PATH": pipe},
                        "source": "bundled-provider-client", "approvalRules": policies.get("codex-app", rules)})
    pipe = os.environ.get("SKY_CUA_NATIVE_PIPE_DIRECTORY", "")
    code = root / "provider-runtime/node_modules/@oai/cua-repl/bin/cua-repl.mjs"
    from .capability_store import declared_file
    if pipe and declared_file(root, manifest(), code.relative_to(root).as_posix()) and "cua-repl" not in excluded:
        preloader = plugins.get("unified-computer-use", root / "missing") / "require-browser-access.mjs"
        env = {key: value for key, value in os.environ.items() if key.startswith(("BROWSER_USE_ACCESS_", "CUA_REPL_"))}
        env.update({"SKY_CUA_NATIVE_PIPE_DIRECTORY": pipe, "SKY_CUA_NATIVE_PIPE": "1",
                    "NODE_REPL_NODE_PATH": str(root / "provider-runtime/node.exe"),
                    "NODE_REPL_NODE_MODULE_DIRS": str(root / "provider-runtime/node_modules"),
                    "CUA_REPL_NODE_REPL_PATH": str(root / "provider-runtime/node_repl.exe")})
        servers.append({"id": "cua-repl", "transport": "stdio", "command": str(root / "provider-runtime/node.exe"),
                        "args": (["--import", preloader.as_uri()] if preloader.is_file() else []) + [str(code)],
                        "env": env, "source": "bundled-provider-client",
                        "approvalRules": policies.get("cua-repl", {"default": "approve", "tools": {}})})
    return tuple(servers)
