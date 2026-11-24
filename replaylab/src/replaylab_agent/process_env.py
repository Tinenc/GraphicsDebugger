"""Minimal, explicit environments for vendor Agent subprocesses."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import AuthenticationError, ConfigurationError

if TYPE_CHECKING:
    from .profiles import Profile

SAFE_EXTERNAL_ENV = frozenset(
    {
        "APPDATA",
        "CODEX_HOME",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NODE_EXTRA_CA_CERTS",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)

# Fixed local provider and managed-state locations are admitted only to
# ReplayLabMCP.  They are
# deliberately excluded from ``SAFE_EXTERNAL_ENV`` so vendor Agent subprocesses
# do not inherit a profiles-database path or executable/tooling layout merely
# because the Host needs it.  Values are paths, never AES material; provider
# code remains responsible for returning only redacted fingerprints.
MCP_HOST_PROVIDER_ENV = frozenset(
    {
        "BREAD_UNREAL_EDITOR_PYTHON",
        "REPLAYLAB_INSTANCE_ID",
        "REPLAYLAB_RUNTIME_DIR",
        "REPLAYLAB_INTERNAL_RUNTIME_DIR",
        "RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE",
        "RENDERDOC_ULTRA_PROJECT_REGISTRY",
        "RENDERDOC_ULTRA_PROJECTS_HOME",
        "RENDERDOC_ULTRA_UNPACKED_ASSET_JOB_ROOT",
        "RENDERDOC_ULTRA_RESOURCE_EXPORT_ROOT",
        "RENDERDOC_ULTRA_UEX",
        "RENDERDOC_ULTRA_UEX_PROFILES",
        "RENDERDOC_ULTRA_BLENDER",
        "RENDERDOC_ULTRA_GLTF_VALIDATOR",
        "RENDERDOC_ULTRA_UNITY_VALIDATOR",
        "RENDERDOC_ULTRA_UEX_BLENDER_WRAPPER",
        "RENDERDOC_ULTRA_UEX_BLENDER_CONTINUATION_DRIVER",
        "RENDERDOC_ULTRA_UEFORMAT_ADDON",
        "RENDERDOC_ULTRA_UEX_PYTHON",
    }
)

# A model credential must never reuse an execution-, filesystem-, trust-, or ReplayLab-control
# variable. qrenderdoc injects the selected API-key value into AgentHost's environment; accepting
# one of these names could turn a secret into PATH/HOME/CODEX_HOME or propagate it to a supposedly
# credential-free MCP/vendor process.
REPLAYLAB_CONTROL_ENV = frozenset(
    {
        "RENDERDOC_ULTRA_SHADER_LIVE_EDIT_REGISTRY",
        "REPLAYLAB_AI_CLI",
        "REPLAYLAB_ALLOWED_TOOLS",
        "REPLAYLAB_INSTANCE_ID",
        "REPLAYLAB_MCP_HOST",
    "REPLAYLAB_MCP_HOST_ARGS",
        "REPLAYLAB_TOOL_AUDIT",
        "REPLAYLAB_TOOL_SCOPE",
    }
)
RESERVED_CREDENTIAL_ENV = (
    SAFE_EXTERNAL_ENV
    | MCP_HOST_PROVIDER_ENV
    | REPLAYLAB_CONTROL_ENV
    | frozenset(
        {
            "CLAUDE_CONFIG_DIR",
            "ALL_PROXY",
            "CODEBUDDY_CONFIG_DIR",
            "COPILOT_HOME",
            "CURSOR_CONFIG_DIR",
            "CURL_CA_BUNDLE",
            "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "GEMINI_CLI_HOME",
            "GEMINI_CLI_TRUST_WORKSPACE",
            "KIMI_CODE_HOME",
            "KIMI_DISABLE_TELEMETRY",
            "LD_LIBRARY_PATH",
            "LD_PRELOAD",
            "NO_PROXY",
            "PYTHONHOME",
            "PYTHONINSPECT",
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "QT_PLUGIN_PATH",
            "QT_QPA_PLATFORM_PLUGIN_PATH",
            "QWEN_CODE_FORCE_ENCRYPTED_FILE_STORAGE",
            "REQUESTS_CA_BUNDLE",
            "VIRTUAL_ENV",
        }
    )
)

# Only these audited catalog presets receive installation-layout fallbacks.  A
# custom ACP command remains explicit and is resolved only from its absolute
# path or PATH, exactly as before.
_KNOWN_VENDOR_COMMANDS = {
    "codex": "codex",
    "copilot-acp": "copilot",
    "cursor-acp": "cursor-agent",
    "gemini-acp": "gemini",
    "qwen-acp": "qwen",
    "kimi-acp": "kimi",
    "codebuddy-acp": "codebuddy",
    "claude-code-acp": "claude-agent-acp",
}
# These vendors publish official npm shims. Cursor instead uses its own install
# root (``~/.local/bin`` on Unix-like systems and ``%LOCALAPPDATA%/cursor-agent``
# on Windows), so accepting an unrelated ``%APPDATA%/npm/agent.cmd`` would widen
# the trusted built-in command beyond Cursor's documented layout.
_NPM_SHIMS = {
    preset: f"{_KNOWN_VENDOR_COMMANDS[preset]}.cmd"
    for preset in (
        "codex",
        "copilot-acp",
        "gemini-acp",
        "qwen-acp",
        "kimi-acp",
        "codebuddy-acp",
        "claude-code-acp",
    )
}
_CODEX_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_CODEX_BIN_ENTRIES = 64
_MIN_CODEX_EXE_BYTES = 64 * 1024
_MAX_CODEX_EXE_BYTES = 1024 * 1024 * 1024
_MAX_NPM_SHIM_BYTES = 1024 * 1024
_MIN_CURSOR_EXE_BYTES = 64 * 1024
_MAX_CURSOR_EXE_BYTES = 1024 * 1024 * 1024
_WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_TRANSIENT_DELETE_ERRORS = frozenset({5, 32, 33, 145})
_TRANSIENT_DELETE_ERRNOS = frozenset(
    {errno.EACCES, errno.EBUSY, errno.ENOTEMPTY, errno.EPERM}
)
# Antivirus scanners and git may briefly retain a pack-file handle after the
# vendor process exits. Keep this bounded: removal is a UI operation and must
# never wait indefinitely for an unrelated process.
_DELETE_RETRY_DELAYS = (0.05, 0.10, 0.20, 0.40, 0.80, 1.00, 1.00)

# Every ReplayLab machine protocol is UTF-8. Windows otherwise chooses the
# active ANSI code page for a Python process whose stdio is redirected to a
# pipe (for example CP936 on a Chinese installation). qrenderdoc, MCP and ACP
# correctly decode those pipes as UTF-8, so allowing the locale default here
# turns localized errors and non-ASCII paths into mojibake. These values are
# fixed by ReplayLab rather than inherited, and therefore cannot be repurposed
# as profile credentials or execution controls.
_UTF8_PROCESS_ENV = {
    "PYTHONIOENCODING": "utf-8:strict",
    "PYTHONUTF8": "1",
}


def external_agent_environment(
    profile: Profile,
    extra: Mapping[str, str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    model: str = "",
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    result = {
        key: value for key, value in source.items() if key.upper() in SAFE_EXTERNAL_ENV
    }
    for name in profile.agent_env:
        value = source.get(name)
        if value is None:
            raise AuthenticationError(
                f"external Agent profile '{profile.id}' requires environment variable {name}"
            )
        result[name] = value
    if extra:
        result.update({str(key): str(value) for key, value in extra.items()})
    result.update(_UTF8_PROCESS_ENV)
    if profile.preset == "codex":
        # A subscription login and Codex configuration are both rooted at
        # CODEX_HOME.  Reusing the user's global home would also import global
        # AGENTS.md, MCP servers, Apps, hooks and memories into ReplayLab.  Keep
        # a durable, profile-scoped home so browser login remains persistent
        # while ReplayLab's Agent contract stays isolated.
        home = codex_profile_home(profile, environ=source)
        result["CODEX_HOME"] = str(home)
        result["REPLAYLAB_MANAGED_CODEX_HOME"] = "1"
    elif profile.preset == "cursor-acp":
        # Cursor's global CLI directory can contain account state, permissions,
        # MCP registrations and preferences.  Keep ReplayLab on a durable,
        # profile-scoped directory and always override any inherited value.
        home = cursor_profile_home(profile, environ=source)
        result["CURSOR_CONFIG_DIR"] = str(home)
        # Cursor ACP explicitly loads user-level ~/.cursor/mcp.json. Redirect
        # both home conventions as well as cli-config.json so a ReplayLab turn
        # cannot discover or start the user's editor MCP servers.
        result["HOME"] = str(home)
        result["USERPROFILE"] = str(home)
        if home.drive:
            result["HOMEDRIVE"] = home.drive
            result["HOMEPATH"] = str(home)[len(home.drive) :] or "\\"
    elif profile.preset == "codebuddy-acp":
        # CodeBuddy officially supports a relocatable configuration root. Keep
        # subscription/account state durable for this ReplayLab connection while
        # excluding the user's ambient settings, hooks, rules, agents and plugins.
        result["CODEBUDDY_CONFIG_DIR"] = str(codebuddy_profile_home(profile, environ=source))
    elif profile.preset == "copilot-acp":
        # Copilot documents COPILOT_HOME as the root for login, settings,
        # permissions and session state.  A ReplayLab connection therefore
        # never imports the user's ordinary ~/.copilot approvals or MCP state.
        result["COPILOT_HOME"] = str(managed_vendor_profile_home(profile, environ=source))
    elif profile.preset == "gemini-acp":
        # GEMINI_CLI_HOME relocates Gemini CLI's durable account/configuration
        # state. Redirect the OS-home conventions as well so generic
        # ~/.agents instructions cannot leak into this isolated Agent turn.
        home = managed_vendor_profile_home(profile, environ=source)
        result["GEMINI_CLI_HOME"] = str(home)
        result["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        _redirect_process_home(result, home)
    elif profile.preset == "qwen-acp":
        # Qwen Code stores state below ~/.qwen and currently publishes no
        # separate data-root override. The managed OS home isolates account,
        # MCP, Skills, instruction and trust files together.
        home = managed_vendor_profile_home(profile, environ=source)
        _redirect_process_home(result, home)
        result["QWEN_CODE_FORCE_ENCRYPTED_FILE_STORAGE"] = "true"
    elif profile.preset == "kimi-acp":
        # KIMI_CODE_HOME relocates login credentials, configuration, plugins,
        # Skills and sessions. The extra HOME redirect also excludes Kimi's
        # documented cross-tool ~/.agents instruction/agent directories.
        home = managed_vendor_profile_home(profile, environ=source)
        result["KIMI_CODE_HOME"] = str(home)
        result["KIMI_DISABLE_TELEMETRY"] = "1"
        _redirect_process_home(result, home)
    elif profile.preset == "claude-code-acp":
        # The Claude Code ACP adapter takes the model from the environment, so it is
        # selected out-of-band through the Claude Code CLI's ANTHROPIC_MODEL
        # variable (it takes precedence over ~/.claude/settings.json and works
        # with a Pro/Max subscription). Driving it from the profile lets the model
        # be chosen in ReplayLab's UI with no terminal; an unset model falls back
        # to Claude Code's own configured default. Accepts a full model id
        # (claude-sonnet-5) or an alias (opus, sonnet). The value is trimmed and
        # lower-cased so stray spaces or capitalization from manual entry do not
        # break it (Claude model ids and aliases are lower-case).
        # The model the user picked for *this run*, not the one saved on the profile.
        # This adapter takes the model only from the environment, and its ACP session
        # exposes no model option, so a per-run pick that stopped here ran the whole
        # task on the saved model while the panel reported the picked one.
        normalized_model = (str(model or "").strip() or profile.model.strip()).lower()
        if normalized_model:
            result["ANTHROPIC_MODEL"] = normalized_model
        result["CLAUDE_CONFIG_DIR"] = str(
            managed_vendor_profile_home(profile, environ=source)
        )
    return result


_MANAGED_VENDOR_STATE_DIRS = {
    "copilot-acp": "copilot",
    "gemini-acp": "gemini",
    "qwen-acp": "qwen",
    "kimi-acp": "kimi",
    "claude-code-acp": "claude",
}
_MANAGED_VENDOR_STATE_LABELS = {
    "copilot-acp": "Copilot",
    "gemini-acp": "Gemini",
    "qwen-acp": "Qwen",
    "kimi-acp": "Kimi",
    "claude-code-acp": "Claude",
}


def _redirect_process_home(environment: dict[str, str], home: Path) -> None:
    """Point both POSIX and Windows home conventions at one managed root."""

    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    if home.drive:
        environment["HOMEDRIVE"] = home.drive
        environment["HOMEPATH"] = str(home)[len(home.drive) :] or "\\"


def managed_vendor_profile_home(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> Path:
    """Return the durable ReplayLab-owned state root for a non-legacy ACP vendor."""

    source = os.environ if environ is None else environ
    directory = _MANAGED_VENDOR_STATE_DIRS.get(profile.preset)
    if directory is None:
        raise ConfigurationError(
            "a managed vendor home requires a supported ACP subscription preset"
        )
    profile.validate()
    return (_managed_vendor_profiles_root(source, directory) / profile.id).resolve()


def _managed_vendor_profiles_root(source: Mapping[str, str], directory: str) -> Path:
    local = source.get("LOCALAPPDATA") or source.get("XDG_DATA_HOME")
    data_root = Path(local).expanduser() if local else Path.home() / ".local" / "share"
    if not data_root.is_absolute():
        raise ConfigurationError("Bread managed vendor state root must be absolute")
    return data_root / "ReplayLab" / directory


def codex_profile_home(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> Path:
    """Return ReplayLab's durable, profile-scoped Codex state root."""

    source = os.environ if environ is None else environ
    if profile.preset != "codex":
        raise ConfigurationError("a managed Codex home requires the Codex preset")
    # Profile.validate() constrains ids to a single traversal-free component.
    profile.validate()
    root = _codex_profiles_root(source)
    # Canonicalizing an existing denied directory can fail before the CLI can
    # diagnose/recover it. The validated single-component ID already fixes its
    # lexical owner; the spawn boundary validates the actual directory.
    from .managed_state import selected_codex_home
    return selected_codex_home(root / profile.id, source)


def _codex_profiles_root(source: Mapping[str, str]) -> Path:
    local = source.get("LOCALAPPDATA") or source.get("XDG_DATA_HOME")
    if local:
        data_root = Path(local).expanduser()
    else:
        data_root = Path.home() / ".local" / "share"
    if not data_root.is_absolute():
        raise ConfigurationError("Bread Codex home root must be absolute")
    return data_root / "ReplayLab" / "codex"


def cursor_profile_home(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> Path:
    """Return ReplayLab's durable, profile-scoped Cursor CLI config root."""

    source = os.environ if environ is None else environ
    if profile.preset != "cursor-acp":
        raise ConfigurationError("a managed Cursor home requires the Cursor ACP preset")
    profile.validate()
    root = _cursor_profiles_root(source)
    return root / profile.id


def _cursor_profiles_root(source: Mapping[str, str]) -> Path:
    local = source.get("LOCALAPPDATA") or source.get("XDG_DATA_HOME")
    if local:
        data_root = Path(local).expanduser()
    else:
        data_root = Path.home() / ".local" / "share"
    if not data_root.is_absolute():
        raise ConfigurationError("Bread Cursor home root must be absolute")
    return data_root / "ReplayLab" / "cursor"


def codebuddy_profile_home(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> Path:
    """Return ReplayLab's durable, profile-scoped CodeBuddy state root."""

    source = os.environ if environ is None else environ
    if profile.preset != "codebuddy-acp":
        raise ConfigurationError(
            "a managed CodeBuddy home requires the CodeBuddy ACP preset"
        )
    profile.validate()
    return _codebuddy_profiles_root(source) / profile.id


def _codebuddy_profiles_root(source: Mapping[str, str]) -> Path:
    local = source.get("LOCALAPPDATA") or source.get("XDG_DATA_HOME")
    if local:
        data_root = Path(local).expanduser()
    else:
        data_root = Path.home() / ".local" / "share"
    if not data_root.is_absolute():
        raise ConfigurationError("Bread CodeBuddy home root must be absolute")
    return data_root / "ReplayLab" / "codebuddy"


_CURSOR_DENIED_PERMISSIONS = (
    "Shell(*)",
    "Read(**)",
    "Write(**)",
    "WebFetch(*)",
    # Delegated agents inherit the parent's MCP server but carry no caller role.
    # Without this deny a child can submit the main Agent's bounded report form.
    "Agent(*)",
    "Task(*)",
    "Subagent(*)",
)


def _cursor_permissions(workspace: Path | None = None, *, writable: bool = False) -> dict[str, list[str]]:
    # Cursor's allow-list is the outer transport boundary, not ReplayLab's
    # workflow policy.  The per-turn MCP proxy exposes only the compact gateway
    # and independently enforces the selected Host operation's frozen scope,
    # requested effects, postconditions and rollback.  Allow that first-party
    # namespace while ambient shell/file/web/delegation stays denied below.
    if workspace is None:
        return {"allow": ["Mcp(Bread:*)"], "deny": list(_CURSOR_DENIED_PERMISSIONS)}
    from .owned_workspace import OwnedWorkspace
    root = OwnedWorkspace(Path(workspace)).root.as_posix()
    denied = [rule for rule in _CURSOR_DENIED_PERMISSIONS if rule not in {"Read(**)", "Write(**)"}]
    allowed = ["Mcp(Bread:*)", "Read(%s/**)" % root]
    if writable:
        allowed.append("Write(%s/**)" % root)
    else:
        denied.append("Write(**)")
    for operation in ("Read", "Write"):
        denied.extend(["%s(%s/**/.*)" % (operation, root), "%s(%s/**/.*/**)" % (operation, root)])
        for name in ("mcp.json", "settings.json", "config.toml", "AGENTS.md", "CLAUDE.md", "GEMINI.md"):
            denied.append("%s(%s/**/%s*)" % (operation, root, name))
    return {"allow": allowed, "deny": denied}


def _write_cursor_json(path: Path, payload: Mapping[str, object]) -> None:
    if _is_reparse_or_symlink(path):
        raise ConfigurationError(
            "refusing to replace managed Cursor configuration through a link/reparse point"
        )
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    except OSError as exc:
        raise ConfigurationError(
            f"cannot prepare managed Cursor CLI configuration ({_delete_error_detail(exc)})"
        ) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_cursor_profile_config(
    profile: Profile, *, environ: Mapping[str, str] | None = None,
    workspace: Path | None = None, writable: bool = False,
) -> Path:
    """Atomically enforce ReplayLab's isolated Cursor CLI permission policy."""

    source = os.environ if environ is None else environ
    root = _cursor_profiles_root(source)
    target = root / profile.id
    data_value = source.get("LOCALAPPDATA") or source.get("XDG_DATA_HOME")
    data_root = (
        Path(data_value).expanduser()
        if data_value
        else Path.home() / ".local" / "share"
    )
    for component in (data_root / "ReplayLab", root, target):
        if _is_reparse_or_symlink(component):
            raise ConfigurationError(
                f"refusing to prepare managed Cursor state through a link/reparse point: {component}"
            )
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigurationError(
            f"cannot create managed Cursor state ({_delete_error_detail(exc)})"
        ) from exc
    if (
        not target.is_dir()
        or _is_reparse_or_symlink(target)
        or target.resolve().parent != root.resolve()
    ):
        raise ConfigurationError("managed Cursor state target escaped its profile root")
    if os.name != "nt":
        os.chmod(target, stat.S_IRWXU)

    config_path = target / "cli-config.json"
    cursor_home = target / ".cursor"
    if _is_reparse_or_symlink(cursor_home):
        raise ConfigurationError(
            "refusing to prepare managed Cursor MCP state through a link/reparse point"
        )
    try:
        cursor_home.mkdir(exist_ok=True)
    except OSError as exc:
        raise ConfigurationError(
            f"cannot create managed Cursor MCP state ({_delete_error_detail(exc)})"
        ) from exc
    if not cursor_home.is_dir() or _is_reparse_or_symlink(cursor_home):
        raise ConfigurationError("managed Cursor MCP state is not an unlinked directory")
    if os.name != "nt":
        os.chmod(cursor_home, stat.S_IRWXU)
    payload = {
        "version": 1,
        "editor": {"vimMode": False},
        "approvalMode": "allowlist",
        "permissions": _cursor_permissions(workspace, writable=writable),
    }
    if config_path.is_file():
        try:
            old = json.loads(config_path.read_text(encoding="utf-8"))
            previous = (old.get("permissions") or {}).get("deny", [])
            # Preserve added user denials. The legacy blanket file rules were
            # controller-generated defaults, replaced by this run's exact grant.
            extra = [rule for rule in previous if isinstance(rule, str) and rule not in _CURSOR_DENIED_PERMISSIONS
                     and rule not in payload["permissions"]["deny"]]
            payload["permissions"]["deny"].extend(extra)
        except (OSError, ValueError, TypeError, AttributeError):
            raise ConfigurationError("cannot preserve managed Cursor permission denials")
    _write_cursor_json(config_path, payload)
    _write_cursor_json(cursor_home / "mcp.json", {"mcpServers": {}})
    return config_path


def ensure_cursor_workspace(workspace: Path | str, *, writable: bool | None = None) -> Path:
    """Pin one Cursor cwd to ReplayLab-owned project policy and no file MCPs."""

    root = Path(workspace)
    if not root.is_absolute() or not root.is_dir() or _is_reparse_or_symlink(root):
        raise ConfigurationError("Cursor workspace must be an existing unlinked directory")
    cursor_dir = root / ".cursor"
    if _is_reparse_or_symlink(cursor_dir):
        raise ConfigurationError(
            "refusing to prepare Cursor workspace through a link/reparse point"
        )
    try:
        cursor_dir.mkdir(exist_ok=True)
    except OSError as exc:
        raise ConfigurationError(
            f"cannot create Cursor workspace policy ({_delete_error_detail(exc)})"
        ) from exc
    if not cursor_dir.is_dir() or _is_reparse_or_symlink(cursor_dir):
        raise ConfigurationError("Cursor workspace policy is not an unlinked directory")
    if cursor_dir.resolve().parent != root.resolve():
        raise ConfigurationError("Cursor workspace configuration escaped its root")
    if os.name != "nt":
        os.chmod(cursor_dir, stat.S_IRWXU)
    _write_cursor_json(
        cursor_dir / "cli.json",
        {"permissions": _cursor_permissions(root, writable=writable) if writable is not None else _cursor_permissions()},
    )
    _write_cursor_json(cursor_dir / "mcp.json", {"mcpServers": {}})
    return root


def release_cursor_managed_tool_restrictions(profile: Profile) -> None:
    """Migrate the identifiable old Bread-generated denial list only.

    Native tools keep Cursor's approval mode and all user-added rules. An
    unrecognised/custom configuration is left unchanged, never rewritten into
    blanket shell approval merely because integration inheritance is enabled.
    """
    target = cursor_profile_home(profile) / "cli-config.json"
    if not target.is_file() or _is_reparse_or_symlink(target):
        return
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    permissions = payload.get("permissions") or {}
    allowed = permissions.get("allow") or []
    denied = permissions.get("deny") or []
    if (payload.get("version") != 1 or payload.get("editor") != {"vimMode": False}
            or payload.get("approvalMode") != "allowlist" or "Mcp(Bread:*)" not in allowed
            or not isinstance(denied, list)):
        return
    known = set(_CURSOR_DENIED_PERMISSIONS)
    retained = [rule for rule in denied if rule not in known]
    if retained != denied:
        permissions["deny"] = retained
        _write_cursor_json(target, payload)


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _is_transient_delete_error(exc: OSError) -> bool:
    return (
        getattr(exc, "winerror", None) in _WINDOWS_TRANSIENT_DELETE_ERRORS
        or exc.errno in _TRANSIENT_DELETE_ERRNOS
    )


def _delete_error_detail(exc: OSError) -> str:
    """Return an ASCII-only Windows-safe detail for the qrenderdoc dialog."""

    winerror = getattr(exc, "winerror", None)
    code = f"Windows error {winerror}" if winerror is not None else f"errno {exc.errno}"
    filename = getattr(exc, "filename", None)
    return f"{code} at {filename}" if filename else code


def _retry_transient_delete(operation) -> None:
    """Retry a bounded filesystem mutation for Windows sharing/AV races."""

    for attempt in range(len(_DELETE_RETRY_DELAYS) + 1):
        try:
            operation()
            return
        except OSError as exc:
            if not _is_transient_delete_error(exc) or attempt == len(
                _DELETE_RETRY_DELAYS
            ):
                raise
            time.sleep(_DELETE_RETRY_DELAYS[attempt])


def _rmtree_with_windows_retries(target: Path, state_label: str = "Codex") -> None:
    """Delete ``target`` without following links and tolerate short locks.

    Python 3.8+ treats Windows junctions as leaves in ``shutil.rmtree`` and on
    other supported platforms directory symlinks are unlinked rather than
    traversed. The callback deliberately refuses to chmod a link/reparse point,
    because doing so could mutate its external target.
    """

    lexical_root = Path(os.path.abspath(target))

    def make_writable_and_retry(function, path, exc_info) -> None:
        error = exc_info[1]
        if not isinstance(error, OSError):
            raise error
        failed = Path(os.path.abspath(path))
        try:
            failed.relative_to(lexical_root)
        except ValueError as exc:
            raise ConfigurationError(
                f"managed {state_label} state deletion escaped its tombstone"
            ) from exc
        if _is_reparse_or_symlink(failed):
            # Never chmod through a link. A transient unlink/rmdir failure is
            # handled by the bounded outer retry instead.
            raise error
        try:
            details = failed.lstat()
            os.chmod(failed, details.st_mode | stat.S_IWRITE)
        except OSError:
            # Preserve the original, more useful deletion failure.
            raise error
        function(path)

    def remove_once() -> None:
        if not target.exists():
            return
        if _is_reparse_or_symlink(target):
            raise ConfigurationError(
                f"refusing to remove managed {state_label} state through a link/reparse point"
            )
        shutil.rmtree(target, onerror=make_writable_and_retry)

    _retry_transient_delete(remove_once)


def remove_codex_profile_home(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> None:
    """Remove only ReplayLab's exact profile-scoped Codex state directory.

    This is intentionally stricter than ordinary cache cleanup because the
    directory can contain a vendor-managed subscription credential.  Refuse
    symlinks, Windows reparse points, path escapes and unexpected file targets
    instead of risking traversal into the user's global Codex state.
    """

    source = os.environ if environ is None else environ
    _remove_managed_profile_home(
        profile,
        source=source,
        expected_preset="codex",
        root=_codex_profiles_root(source),
        state_label="Codex",
    )


def remove_cursor_profile_home(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> None:
    """Remove only ReplayLab's exact profile-scoped Cursor CLI state directory."""

    source = os.environ if environ is None else environ
    _remove_managed_profile_home(
        profile,
        source=source,
        expected_preset="cursor-acp",
        root=_cursor_profiles_root(source),
        state_label="Cursor",
    )


def remove_codebuddy_profile_home(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> None:
    """Remove only ReplayLab's exact profile-scoped CodeBuddy state directory."""

    source = os.environ if environ is None else environ
    _remove_managed_profile_home(
        profile,
        source=source,
        expected_preset="codebuddy-acp",
        root=_codebuddy_profiles_root(source),
        state_label="CodeBuddy",
    )


def remove_managed_vendor_profile_home(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> None:
    """Remove one exact ReplayLab-owned ACP vendor state directory."""

    source = os.environ if environ is None else environ
    directory = _MANAGED_VENDOR_STATE_DIRS.get(profile.preset)
    label = _MANAGED_VENDOR_STATE_LABELS.get(profile.preset)
    if directory is None or label is None:
        raise ConfigurationError(
            "managed vendor state can be removed only for a supported ACP subscription profile"
        )
    _remove_managed_profile_home(
        profile,
        source=source,
        expected_preset=profile.preset,
        root=_managed_vendor_profiles_root(source, directory),
        state_label=label,
    )


def _remove_managed_profile_home(
    profile: Profile,
    *,
    source: Mapping[str, str],
    expected_preset: str,
    root: Path,
    state_label: str,
) -> None:
    profile.validate()
    if profile.preset != expected_preset:
        raise ConfigurationError(
            f"managed {state_label} state can be removed only for a {state_label} profile"
        )
    target = root / profile.id
    if target.parent != root or target.name != profile.id:
        raise ConfigurationError(
            f"managed {state_label} state target escaped its profile root"
        )

    data_value = source.get("LOCALAPPDATA") or source.get("XDG_DATA_HOME")
    data_root = Path(data_value).expanduser() if data_value else Path.home() / ".local" / "share"
    resolved_data = data_root.resolve()
    resolved_root = root.resolve()
    try:
        resolved_root.relative_to(resolved_data)
    except ValueError as exc:
        raise ConfigurationError(
            f"managed {state_label} state root escaped the local data directory"
        ) from exc

    # Refuse any managed component that redirects traversal, even when it
    # happens to resolve back below the local data root today.
    for component in (data_root / "ReplayLab", root, target):
        if _is_reparse_or_symlink(component):
            raise ConfigurationError(
                f"refusing to remove managed {state_label} state through a link/reparse point: {component}"
            )
    if not target.exists():
        return
    if not target.is_dir():
        raise ConfigurationError(f"managed {state_label} state target is not a directory")
    resolved_target = target.resolve()
    if resolved_target.parent != resolved_root or resolved_target.name != profile.id:
        raise ConfigurationError(
            f"managed {state_label} state target escaped its profile root"
        )
    # Detach the state directory first. This prevents a still-running vendor
    # helper from creating new files in the active profile directory while
    # cleanup is walking it, and makes each attempt operate on a stable tree.
    # The tombstone is a random sibling below the already-validated root, so it
    # cannot name the vendor's global state directory.
    tombstone = root / f".delete-{profile.id}-{uuid.uuid4().hex}"
    if tombstone.parent != root or tombstone.exists():
        raise ConfigurationError(f"managed {state_label} state tombstone is invalid")
    try:
        _retry_transient_delete(lambda: target.rename(tombstone))
        _rmtree_with_windows_retries(tombstone, state_label)
    except (ConfigurationError, OSError) as exc:
        # Keep the profile and its state path consistent when a genuinely
        # long-lived process still owns a handle. rmtree may have removed cache
        # files before failing, but credentials that remain stay attached to the
        # still-visible profile instead of becoming an orphaned secret.
        restore_error: OSError | None = None
        if tombstone.exists() and not target.exists():
            try:
                _retry_transient_delete(lambda: tombstone.rename(target))
            except OSError as restore_exc:
                restore_error = restore_exc
        if restore_error is not None:
            original_detail = (
                str(exc)
                if isinstance(exc, ConfigurationError)
                else _delete_error_detail(exc)
            )
            raise ConfigurationError(
                f"cannot remove managed {state_label} state and could not restore its "
                f"profile directory: {original_detail}; restore failed: "
                f"{_delete_error_detail(restore_error)}"
            ) from exc
        if isinstance(exc, ConfigurationError):
            raise
        raise ConfigurationError(
            f"cannot remove managed {state_label} state after bounded retries; stop the current "
            f"AI task and retry ({_delete_error_detail(exc)})"
        ) from exc


def host_process_environment(
    extra: Mapping[str, str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a credential-free environment for ReplayLabMCP itself.

    In particular, model-provider API keys and unrelated Agent credentials are
    never inherited by the MCP host.  The fixed local provider/managed-state
    variables are Host-only; the caller may additionally add qrenderdoc
    instance bindings and isolated managed-state paths.
    """

    source = os.environ if environ is None else environ
    result = {
        key: value
        for key, value in source.items()
        if key.upper() in SAFE_EXTERNAL_ENV | MCP_HOST_PROVIDER_ENV
    }
    if extra:
        result.update({str(key): str(value) for key, value in extra.items()})
    result.update(_UTF8_PROCESS_ENV)
    return result


def resolve_executable(
    command: str, *, environ: Mapping[str, str] | None = None
) -> str:
    """Resolve a command without ever consulting the task working directory."""

    source = os.environ if environ is None else environ
    expanded = Path(command).expanduser()
    if expanded.is_absolute():
        return str(expanded.resolve()) if expanded.is_file() else ""
    if any(separator in command for separator in ("/", "\\")):
        # Relative executable paths make an untrusted task cwd executable.
        return ""
    extensions = [""]
    if os.name == "nt" and not expanded.suffix:
        # Windows installs of npm-published CLIs drop both a bare POSIX shim (a
        # shell script with no extension) and a real launcher (``.cmd``/``.exe``)
        # of the same name into the same PATH directory. CreateProcess rejects
        # the bare shim with "[WinError 193] %1 is not a valid Win32 application",
        # so try the PATHEXT launchers first and keep the empty extension only as
        # a last resort. This mirrors how cmd.exe itself resolves a bare command.
        pathext = [
            item.lower()
            for item in source.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
            if item
        ]
        extensions = pathext + [""]
    for directory in source.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        directory_path = Path(directory).expanduser()
        if not directory_path.is_absolute():
            # Relative PATH entries (including '.') would consult the task cwd.
            continue
        for extension in extensions:
            candidate = directory_path / f"{command}{extension}"
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate.resolve())
    return ""


def _is_windows_platform() -> bool:
    return os.name == "nt"


def is_packaged_windows_executable(path: str) -> bool:
    """Identify WindowsApps aliases that are not reliable child-process CLIs."""

    if not _is_windows_platform():
        return False
    normalized = os.path.abspath(path).replace("/", "\\").casefold()
    return "\\program files\\windowsapps\\" in normalized or (
        "\\appdata\\local\\microsoft\\windowsapps\\" in normalized
    )


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _safe_directory(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not _is_reparse(info)


def _safe_base(raw: str) -> Path | None:
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() or not _safe_directory(path):
        return None
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def _is_contained_path(parent: Path, candidate: Path) -> bool:
    """Compare resolved paths with Windows case-folding and no prefix tricks."""

    try:
        common = os.path.commonpath((str(parent), str(candidate)))
    except (OSError, ValueError):
        return False
    return os.path.normcase(common) == os.path.normcase(str(parent))


def _safe_literal_directory(parent: Path, name: str) -> Path | None:
    candidate = parent / name
    if not _safe_directory(candidate):
        return None
    try:
        resolved = candidate.resolve(strict=True)
        if not _is_contained_path(parent, resolved):
            return None
        return resolved
    except (OSError, ValueError):
        return None


def _safe_regular_file(
    candidate: Path,
    *,
    minimum_size: int,
    maximum_size: int,
) -> os.stat_result | None:
    try:
        info = os.lstat(candidate)
    except OSError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or _is_reparse(info)
        or not minimum_size <= int(info.st_size) <= maximum_size
    ):
        return None
    return info


def _bundled_codex_executable(source: Mapping[str, str]) -> str:
    """Resolve one bounded official Codex desktop installation layout.

    The only enumeration is the direct child list of the literal
    ``OpenAI/Codex/bin`` directory, capped at 64 entries.  No recursive or
    drive-wide search is performed.
    """

    local = _safe_base(str(source.get("LOCALAPPDATA", "")))
    if local is None:
        return ""
    current = local
    for component in ("OpenAI", "Codex", "bin"):
        child = _safe_literal_directory(current, component)
        if child is None:
            return ""
        current = child
    bin_root = current
    candidates: list[tuple[int, str, str]] = []
    try:
        with os.scandir(bin_root) as entries:
            count = 0
            for entry in entries:
                count += 1
                if count > _MAX_CODEX_BIN_ENTRIES:
                    return ""
                if not _CODEX_VERSION_RE.fullmatch(entry.name):
                    continue
                try:
                    directory_info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISDIR(directory_info.st_mode) or _is_reparse(directory_info):
                    continue
                version_dir = Path(entry.path)
                executable = version_dir / "codex.exe"
                file_info = _safe_regular_file(
                    executable,
                    minimum_size=_MIN_CODEX_EXE_BYTES,
                    maximum_size=_MAX_CODEX_EXE_BYTES,
                )
                if file_info is None:
                    continue
                try:
                    resolved = executable.resolve(strict=True)
                    if not _is_contained_path(bin_root, resolved):
                        continue
                    with resolved.open("rb") as stream:
                        if stream.read(2) != b"MZ":
                            continue
                except (OSError, ValueError):
                    continue
                candidates.append(
                    (int(getattr(file_info, "st_mtime_ns", 0)), entry.name, str(resolved))
                )
    except OSError:
        return ""
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][2]


def _npm_vendor_shim(preset_id: str, source: Mapping[str, str]) -> str:
    shim_name = _NPM_SHIMS.get(preset_id)
    if not shim_name:
        return ""
    appdata = _safe_base(str(source.get("APPDATA", "")))
    if appdata is None:
        return ""
    npm = _safe_literal_directory(appdata, "npm")
    if npm is None:
        return ""
    candidate = npm / shim_name
    if (
        _safe_regular_file(candidate, minimum_size=1, maximum_size=_MAX_NPM_SHIM_BYTES)
        is None
    ):
        return ""
    try:
        resolved = candidate.resolve(strict=True)
        if not _is_contained_path(npm, resolved):
            return ""
        with resolved.open("rb") as stream:
            if b"\0" in stream.read(4096):
                return ""
        return str(resolved)
    except (OSError, ValueError):
        return ""


def _installed_cursor_executable(source: Mapping[str, str]) -> str:
    """Resolve Cursor's fixed official Windows installation root without searching."""

    local = _safe_base(str(source.get("LOCALAPPDATA", "")))
    if local is None:
        return ""
    root = _safe_literal_directory(local, "cursor-agent")
    if root is None:
        return ""
    for filename, minimum_size, maximum_size, require_pe in (
        ("cursor-agent.exe", _MIN_CURSOR_EXE_BYTES, _MAX_CURSOR_EXE_BYTES, True),
        ("cursor-agent.cmd", 1, _MAX_NPM_SHIM_BYTES, False),
    ):
        candidate = root / filename
        if (
            _safe_regular_file(
                candidate,
                minimum_size=minimum_size,
                maximum_size=maximum_size,
            )
            is None
        ):
            continue
        try:
            resolved = candidate.resolve(strict=True)
            if not _is_contained_path(root, resolved):
                continue
            with resolved.open("rb") as stream:
                prefix = stream.read(4096)
            if require_pe and not prefix.startswith(b"MZ"):
                continue
            if not require_pe and b"\0" in prefix:
                continue
            return str(resolved)
        except (OSError, ValueError):
            continue
    return ""


def resolve_known_vendor_executable(
    preset_id: str,
    command: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve an audited catalog CLI with tightly bounded Windows fallbacks."""

    source = os.environ if environ is None else environ
    resolved = resolve_executable(command, environ=source)
    if resolved and not is_packaged_windows_executable(resolved):
        return resolved
    expected = _KNOWN_VENDOR_COMMANDS.get(preset_id)
    if expected is None or command.casefold() != expected.casefold() or not _is_windows_platform():
        return ""
    if preset_id == "codex":
        resolved = _bundled_codex_executable(source)
        if resolved:
            return resolved
    if preset_id == "cursor-acp":
        resolved = _installed_cursor_executable(source)
        if resolved:
            return resolved
    return _npm_vendor_shim(preset_id, source)


def agent_command_argv(profile: Profile) -> list[str]:
    """Return audited catalog argv for known vendors, explicit argv otherwise."""

    expected = _KNOWN_VENDOR_COMMANDS.get(profile.preset)
    if expected is None:
        return profile.argv
    argv = list(profile.provider.command)
    if not argv or argv[0].casefold() != expected.casefold():
        raise ConfigurationError(f"catalog command mismatch for provider preset: {profile.preset}")
    return argv


def resolve_agent_executable(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> str:
    argv = agent_command_argv(profile)
    if not argv:
        return ""
    if profile.preset in _KNOWN_VENDOR_COMMANDS:
        return resolve_known_vendor_executable(
            profile.preset, argv[0], environ=environ
        )
    return resolve_executable(argv[0], environ=environ)


def resolved_agent_argv(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> list[str]:
    argv = agent_command_argv(profile)
    resolved = resolve_agent_executable(profile, environ=environ)
    if not resolved:
        raise ConfigurationError(
            f"external Agent executable is missing or not in an approved installation location: "
            f"{argv[0] if argv else '<empty>'}"
        )
    return [resolved, *argv[1:]]


__all__ = [
    "SAFE_EXTERNAL_ENV",
    "MCP_HOST_PROVIDER_ENV",
    "REPLAYLAB_CONTROL_ENV",
    "RESERVED_CREDENTIAL_ENV",
    "agent_command_argv",
    "codebuddy_profile_home",
    "codex_profile_home",
    "cursor_profile_home",
    "ensure_cursor_profile_config",
    "ensure_cursor_workspace",
    "external_agent_environment",
    "managed_vendor_profile_home",
    "remove_codex_profile_home",
    "remove_codebuddy_profile_home",
    "remove_cursor_profile_home",
    "remove_managed_vendor_profile_home",
    "host_process_environment",
    "is_packaged_windows_executable",
    "resolve_agent_executable",
    "resolve_executable",
    "resolve_known_vendor_executable",
    "resolved_agent_argv",
]
