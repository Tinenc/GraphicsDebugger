"""Gated, bounded subscription login and no-generation connection tests."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import native_vendor
from ..errors import ConfigurationError, ConnectorError
from ..process_env import (
    agent_command_argv,
    ensure_cursor_profile_config,
    ensure_cursor_workspace,
    external_agent_environment,
    resolve_agent_executable,
    resolve_known_vendor_executable,
)
from ..profiles import Profile
from .acp import ACPConnector
from .claude_auth import check_claude_auth_status
from .codex_app_server import CodexAppServerConnector

EventSink = Callable[[dict[str, Any]], None]

_MAX_CAPTURED_OUTPUT_BYTES = 1024 * 1024
_MAX_CAPTURED_LINE_BYTES = 64 * 1024
_MAX_MODELS = 200
_POST_EXIT_DRAIN_SECONDS = 1.0
_WORKSPACE_DELETE_RETRY_DELAYS = (0.05, 0.10, 0.20, 0.40)
_WORKSPACE_TRANSIENT_WINERRORS = frozenset({5, 32, 33, 145})
_WORKSPACE_TRANSIENT_ERRNOS = frozenset(
    {errno.EACCES, errno.EBUSY, errno.ENOTEMPTY, errno.EPERM}
)
_WORKSPACE_TOMBSTONE_RE = re.compile(
    r"^\.delete-run-[A-Za-z0-9_-]{1,64}-[0-9a-f]{32}$"
)
_MAX_WORKSPACE_TOMBSTONES_PER_PASS = 4
_URL_RE = re.compile(r"https?://[^\s<>\"']{1,4096}", re.IGNORECASE)
_DASHED_CODE_RE = re.compile(r"\b[A-Z0-9]{3,12}(?:-[A-Z0-9]{3,12})+\b")
_LABELLED_CODE_RE = re.compile(
    r"(?i)\b(?:user|device|verification|activation)?\s*code\s*(?:is|:|：)\s*([A-Z0-9-]{4,32})\b"
)


class SubscriptionCancelled(ConnectorError):
    """The qrenderdoc-owned control pipe cancelled a subscription operation."""


@dataclass(frozen=True, slots=True)
class SubscriptionResult:
    authenticated: bool
    models: tuple[str, ...] = ()
    account: dict[str, Any] | None = None
    message: str = ""
    terminal_opened: bool = False
    message_id: str = ""
    message_args: tuple[str, ...] = ()


class _SubscriptionWorkspaceOwner:
    """Idempotent, non-throwing owner for one isolated Agent working directory.

    ``tempfile.TemporaryDirectory`` on Python 3.10 recursively re-enters its
    Windows error callback when a child process still owns the directory.  A
    subscription close must not turn that short-lived lock into an unbounded
    traceback, so cleanup is detached and bounded here instead.
    """

    def __init__(self, workspace: Path, root: Path) -> None:
        self._workspace: Path | None = workspace
        self._root = root

    def cleanup(self) -> None:
        workspace = self._workspace
        self._workspace = None
        if workspace is None:
            return
        with contextlib.suppress(OSError, ConfigurationError):
            _detach_and_delete_subscription_workspace(workspace, self._root)


def _runtime_argv(profile: Profile) -> list[str]:
    return agent_command_argv(profile)


def detect_subscription(
    profile: Profile, *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Resolve approved install locations without launching a vendor process."""

    profile.validate()
    provider = profile.provider
    if provider.kind != "agent" or provider.protocol not in {"codex-app-server", "acp"}:
        raise ConfigurationError("subscription commands require an Agent subscription profile")
    argv = _runtime_argv(profile)
    command = argv[0] if argv else ""
    executable = resolve_agent_executable(profile, environ=environ) if command else ""
    installed = bool(executable)
    if installed:
        message = f"{provider.title} CLI detected; sign-in state is checked only after approval."
        message_id = "subscription.cli-detected"
        message_args = [provider.title]
    else:
        message = provider.install_hint or f"Required provider CLI was not found: {command}"
        message_id = "subscription.cli-missing"
        message_args = [command, provider.install_hint]
    return {
        "kind": "subscriptionStatus",
        "status": "installed" if installed else "missing",
        "profile": profile.id,
        "preset": profile.preset,
        "protocol": provider.protocol,
        "message": message,
        "messageId": message_id,
        "messageArgs": message_args,
        "installed": installed,
        # Detection is process-free, so account state is deliberately unknown.
        "authenticated": None,
        "authUrl": "",
        "userCode": "",
        "interactiveTerminalRequired": provider.subscription_login_mode == "terminal",
        "terminalOpened": False,
        "models": [],
        "testCompleted": False,
        "installHint": provider.install_hint,
    }


class SubscriptionBackend:
    """Backend used only after qrenderdoc releases the stdin start gate."""

    def __init__(
        self,
        profile: Profile,
        *,
        timeout: float,
        emit: EventSink,
        environ: Mapping[str, str] | None = None,
        codex_factory: Callable[[Profile, float, Mapping[str, str]], Any] | None = None,
        acp_factory: Callable[[Profile, float, Mapping[str, str]], Any] | None = None,
        popen_factory: Callable[..., Any] | None = None,
    ) -> None:
        if timeout <= 0 or timeout > 3600:
            raise ConfigurationError("subscription timeout must be between 0 and 3600 seconds")
        self.profile = profile
        self.timeout = float(timeout)
        self.emit = emit
        self.environ = environ
        self._codex_factory = codex_factory or self._make_codex_connector
        self._acp_factory = acp_factory or self._make_acp_connector
        self._popen_factory = popen_factory or subprocess.Popen
        self.detection = detect_subscription(profile, environ=environ)
        self.workspace, self._workspace_owner = _subscription_workspace(environ)

    def close(self) -> None:
        """Remove the empty per-operation workspace; provider credentials live elsewhere."""

        owner = self._workspace_owner
        self._workspace_owner = None
        if owner is not None:
            owner.cleanup()

    def event(self, kind: str, status: str, message: str, **fields: Any) -> dict[str, Any]:
        payload = {
            "kind": kind,
            "status": status,
            "profile": self.profile.id,
            "preset": self.profile.preset,
            "protocol": self.profile.provider.protocol,
            "message": message,
            "installed": bool(self.detection["installed"]),
            "authenticated": fields.pop("authenticated", None),
            "authUrl": fields.pop("authUrl", ""),
            "userCode": fields.pop("userCode", ""),
            "interactiveTerminalRequired": (
                self.profile.provider.subscription_login_mode == "terminal"
            ),
            "terminalOpened": fields.pop("terminalOpened", False),
            "models": fields.pop("models", []),
            "testCompleted": fields.pop("testCompleted", False),
        }
        payload.update(fields)
        self.emit(payload)
        return payload

    def _resolved_profile(self) -> Profile:
        argv = _runtime_argv(self.profile)
        if not argv:
            raise ConnectorError("subscription profile has no runtime command")
        resolved = resolve_agent_executable(self.profile, environ=self.environ)
        if not resolved:
            raise ConnectorError(self.detection["message"])
        fixed = replace(self.profile, command=[resolved, *argv[1:]])
        fixed.validate()
        return fixed

    def _environment(self, profile: Profile) -> dict[str, str]:
        if profile.preset == "cursor-acp":
            ensure_cursor_profile_config(profile, environ=self.environ)
            ensure_cursor_workspace(self.workspace)
        native_vendor.prepare_workspace(profile, self.workspace)
        return external_agent_environment(profile, environ=self.environ)

    def _make_codex_connector(
        self,
        profile: Profile, timeout: float, environment: Mapping[str, str]
    ) -> CodexAppServerConnector:
        return CodexAppServerConnector(
            profile.argv,
            cwd=self.workspace,
            timeout=timeout,
            env=environment,
        )

    def _make_acp_connector(
        self,
        profile: Profile, timeout: float, environment: Mapping[str, str]
    ) -> ACPConnector:
        return ACPConnector(
            native_vendor.runtime_argv(
                profile,
                profile.argv,
                self.workspace,
            ),
            cwd=self.workspace,
            timeout=timeout,
            env=environment,
            authentication_method=profile.provider.acp_auth_method,
            provider_id=profile.preset,
            session_meta=native_vendor.session_meta(profile),
        )

    async def test(self, cancel: asyncio.Event) -> SubscriptionResult:
        try:
            profile = self._resolved_profile()
            if profile.provider.protocol == "codex-app-server":
                return await self._test_codex(profile, cancel)
            if profile.options.get("allow_unsandboxed_agent") is not True:
                raise ConfigurationError(
                    "ACP connection testing requires allow_unsandboxed_agent=true for this trusted CLI"
                )
            if profile.preset == "claude-code-acp":
                account = await _await_or_cancel(
                    check_claude_auth_status(
                        profile, environ=self.environ, timeout=min(self.timeout, 15.0),
                        cwd=self.workspace),
                    cancel,
                )
                if not account.logged_in:
                    return SubscriptionResult(
                        False, message=account.detail, message_id=account.message_id,
                        message_args=account.message_args,
                    )
            connector = self._acp_factory(
                profile, self.timeout, self._environment(profile)
            )
            details = await _await_or_cancel(connector.test_connection(), cancel)
            models = _bounded_models(details.get("models", []))
            if profile.preset == "claude-code-acp":
                message = "Claude sign-in status and ACP session handshake succeeded without sending a prompt."
                message_id = "subscription.claude-sign-in-test-succeeded"
                message_args = ()
            elif models:
                message = (
                    "ACP initialize/session handshake succeeded without sending a prompt; "
                    f"the session exposed {len(models)} model choice(s)."
                )
                message_id = "subscription.acp-test-models"
                message_args = (str(len(models)),)
            else:
                message = (
                    "ACP initialize/session handshake succeeded without sending a prompt. "
                    "This Agent did not expose a standard model list; leave the model unset to "
                    "use its configured default, or enter an exact model only if that CLI "
                    "supports ACP model selection."
                )
                message_id = "subscription.acp-test-no-models"
                message_args = ()
            return SubscriptionResult(
                True,
                tuple(models),
                message=message,
                message_id=message_id,
                message_args=message_args,
            )
        finally:
            self.close()

    async def connect(self, flow: str, cancel: asyncio.Event) -> SubscriptionResult:
        try:
            if flow not in {"browser", "device"}:
                raise ConfigurationError("subscription flow must be browser or device")
            profile = self._resolved_profile()
            mode = profile.provider.subscription_login_mode
            if profile.provider.protocol == "codex-app-server":
                return await self._connect_codex(profile, flow, cancel)
            if profile.options.get("allow_unsandboxed_agent") is not True:
                raise ConfigurationError(
                    "ACP connection requires allow_unsandboxed_agent=true for this trusted CLI"
                )
            terminal_opened = False
            if mode == "captured":
                await self._run_captured_login(profile, flow, cancel)
            elif mode == "terminal":
                terminal_opened = True
                await self._run_terminal_login(profile, cancel)
            elif profile.preset == "custom-acp":
                self.event(
                    "loginSkipped",
                    "testing",
                    "Custom ACP has no guessed login command; running only the configured handshake test.",
                    messageId="subscription.custom-login-skipped",
                    messageArgs=[],
                )
            else:
                raise ConfigurationError("this subscription preset has no supported login flow")
            tested = await self.test(cancel)
            return SubscriptionResult(
                tested.authenticated,
                tested.models,
                tested.account,
                tested.message,
                terminal_opened,
                tested.message_id,
                tested.message_args,
            )
        finally:
            self.close()

    async def _test_codex(
        self, profile: Profile, cancel: asyncio.Event
    ) -> SubscriptionResult:
        connector = self._codex_factory(
            profile, self.timeout, self._environment(profile)
        )
        async with connector:
            account_result = await _await_or_cancel(connector.account_read(), cancel)
            account = account_result.get("account")
            if not isinstance(account, dict) or not account:
                return SubscriptionResult(
                    False,
                    message="Codex is signed out.",
                    message_id="subscription.codex-signed-out",
                )
            details = await _await_or_cancel(connector.list_models(), cancel)
            models = _model_ids(details)
            return SubscriptionResult(
                True,
                tuple(models),
                _account_summary(account),
                "Codex account and model catalog are available.",
                message_id="subscription.codex-account-ready",
            )

    async def _connect_codex(
        self, profile: Profile, flow: str, cancel: asyncio.Event
    ) -> SubscriptionResult:
        connector = self._codex_factory(
            profile, self.timeout, self._environment(profile)
        )
        async with connector:
            account_result = await _await_or_cancel(connector.account_read(), cancel)
            account = account_result.get("account")
            if not isinstance(account, dict) or not account:
                started = await _await_or_cancel(connector.login_start(flow=flow), cancel)
                auth_url = started.get("authUrl", started.get("verificationUrl", ""))
                user_code = started.get("userCode", "")
                self.event(
                    "loginStarted",
                    "waitingForUser",
                    "Complete Codex sign-in in the browser.",
                    messageId="subscription.codex-browser-complete",
                    messageArgs=[],
                    authUrl=str(auth_url or ""),
                    userCode=str(user_code or ""),
                    flow=flow,
                )
                login_id = str(started.get("loginId", ""))
                if not login_id:
                    raise ConnectorError("Codex app-server login returned no login id")

                async def cancel_login() -> None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(connector.cancel_login(login_id), timeout=5.0)

                completed = await _await_or_cancel(
                    connector.wait_login(login_id, timeout=self.timeout),
                    cancel,
                    on_cancel=cancel_login,
                )
                success = bool(completed.get("success"))
                from ..agent_diagnostics import redact
                failure = completed.get("error", "")
                if isinstance(failure, dict):
                    failure = failure.get("message", failure.get("code", ""))
                failure = redact(str(failure or ""))[:1600]
                failure_id = ("subscription.codex-login-failed-detail" if failure
                              else "subscription.codex-login-failed")
                self.event(
                    "loginCompleted",
                    "authenticated" if success else "failed",
                    "Codex login completed." if success else "Codex login failed.",
                    messageId=(
                        "subscription.codex-login-completed"
                        if success
                        else failure_id
                    ),
                    messageArgs=[failure] if not success and failure else [],
                    authenticated=success,
                )
                if not success:
                    return SubscriptionResult(
                        False,
                        message="Codex login failed." + (" " + failure if failure else ""),
                        message_id=failure_id,
                        message_args=(failure,) if failure else (),
                    )
                account_result = await _await_or_cancel(connector.account_read(), cancel)
                account = account_result.get("account")
            if not isinstance(account, dict) or not account:
                return SubscriptionResult(
                    False,
                    message="Codex account is not authenticated.",
                    message_id="subscription.codex-account-unauthenticated",
                )
            details = await _await_or_cancel(connector.list_models(), cancel)
            return SubscriptionResult(
                True,
                tuple(_model_ids(details)),
                _account_summary(account),
                "Codex sign-in and model catalog test succeeded.",
                message_id="subscription.codex-sign-in-test-succeeded",
            )

    async def _run_captured_login(
        self, profile: Profile, flow: str, cancel: asyncio.Event
    ) -> None:
        login = list(profile.provider.subscription_login)
        if profile.preset == "copilot-acp":
            login.append("--device-code" if flow == "device" else "--web-flow")
        if not login:
            raise ConfigurationError("the provider catalog has no login command")
        resolved = resolve_known_vendor_executable(
            profile.preset, login[0], environ=self.environ
        )
        if not resolved:
            raise ConnectorError(profile.provider.install_hint or "provider CLI is missing")
        argv = [resolved, *login[1:]]
        environment = self._environment(profile)
        if profile.preset == "cursor-acp":
            # Cursor documents this switch for embedding/headless clients. Keep
            # the browser under qrenderdoc's visible HTTPS-link gate while the
            # official CLI retains and validates its own subscription token.
            environment["NO_OPEN_BROWSER"] = "1"
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": str(self.workspace),
            "env": environment,
            "limit": _MAX_CAPTURED_LINE_BYTES,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            kwargs["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(*argv, **kwargs)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise ConnectorError(f"cannot start {profile.provider.title} login: {exc}") from exc
        self.event(
            "loginStarted",
            "waitingForUser",
            f"Waiting for {profile.provider.title} browser/device authorization.",
            messageId="subscription.browser-waiting",
            messageArgs=[profile.provider.title],
        )
        authorization = {"url": "", "code": ""}

        def authorization_progress(url: str, code: str) -> None:
            # Vendors commonly print the verification URL and device code on
            # different lines or different streams.  Retain each bounded field
            # so every later event is directly actionable by the native UI.
            if url:
                authorization["url"] = url
            if code:
                authorization["code"] = code
            self.event(
                "loginProgress",
                "waitingForUser",
                "Complete provider authorization in the browser.",
                messageId="subscription.browser-complete",
                messageArgs=[],
                authUrl=authorization["url"],
                userCode=authorization["code"],
            )

        await _wait_captured_login(
            process,
            cancel,
            timeout=self.timeout,
            on_authorization=authorization_progress,
        )
        self.event(
            "loginCompleted",
            "completed",
            f"{profile.provider.title} login command completed; testing ACP now.",
            messageId="subscription.acp-login-completed",
            messageArgs=[profile.provider.title],
            authenticated=None,
        )

    async def _run_terminal_login(
        self, profile: Profile, cancel: asyncio.Event
    ) -> None:
        if os.name != "nt":
            raise ConnectorError(
                "interactive provider login currently requires a Windows console; run the official CLI in a terminal"
            )
        login = list(profile.provider.subscription_login)
        if not login:
            raise ConfigurationError("the provider catalog has no terminal login command")
        resolved = resolve_known_vendor_executable(
            profile.preset, login[0], environ=self.environ
        )
        if not resolved:
            raise ConnectorError(profile.provider.install_hint or "provider CLI is missing")
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        try:
            process = self._popen_factory(
                [resolved, *login[1:]],
                cwd=str(self.workspace),
                env=self._environment(profile),
                close_fds=True,
                creationflags=flags,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise ConnectorError(f"cannot open {profile.provider.title} login terminal: {exc}") from exc
        self.event(
            "terminalOpened",
            "waitingForUser",
            profile.provider.subscription_instruction
            or "Complete authentication in the provider terminal, then exit it.",
            messageId="subscription.terminal-opened",
            messageArgs=[profile.provider.title],
            terminalOpened=True,
        )
        await _wait_popen(process, cancel, timeout=self.timeout)
        self.event(
            "loginCompleted",
            "completed",
            f"{profile.provider.title} terminal exited; testing ACP now.",
            messageId="subscription.terminal-login-completed",
            messageArgs=[profile.provider.title],
            authenticated=None,
            terminalOpened=True,
        )


async def _await_or_cancel(
    awaitable: Awaitable[Any],
    cancel: asyncio.Event,
    *,
    on_cancel: Callable[[], Awaitable[None]] | None = None,
) -> Any:
    if cancel.is_set():
        if hasattr(awaitable, "close"):
            awaitable.close()  # type: ignore[attr-defined]
        raise SubscriptionCancelled("subscription operation cancelled")
    operation = asyncio.create_task(awaitable)
    cancelled = asyncio.create_task(cancel.wait())
    cancellation_started = False

    async def stop() -> None:
        nonlocal cancellation_started
        cancellation_started = True
        try:
            if on_cancel is not None:
                await on_cancel()
        finally:
            # A failed vendor cancel request must not leave the local request
            # running after its owner has returned or deleted its workspace.
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    try:
        done, _ = await asyncio.wait(
            {operation, cancelled}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancelled in done:
            await stop()
            raise SubscriptionCancelled("subscription operation cancelled")
        return await operation
    except asyncio.CancelledError:
        if not cancellation_started:
            await stop()
        raise
    finally:
        if not cancelled.done():
            cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)


def _bounded_models(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    result: list[str] = []
    for value in values:
        model = str(value or "").strip()
        if model and len(model) <= 512 and model not in result:
            result.append(model)
        if len(result) >= _MAX_MODELS:
            break
    return result


def _model_ids(details: Any) -> list[str]:
    if not isinstance(details, list):
        return []
    values: list[str] = []
    for item in details:
        if not isinstance(item, dict):
            continue
        value = item.get("id", item.get("model", ""))
        if isinstance(value, str):
            values.append(value)
    return _bounded_models(values)


def _account_summary(account: Mapping[str, Any]) -> dict[str, Any]:
    """Expose only non-secret account routing metadata to qrenderdoc JSONL."""

    result: dict[str, Any] = {}
    for key in ("type", "planType"):
        value = account.get(key)
        if isinstance(value, str) and 0 < len(value) <= 128:
            result[key] = value
    return result


def _authorization_fields(text: str) -> tuple[str, str]:
    url = ""
    for match in _URL_RE.findall(text):
        candidate = match.rstrip(".,);]}")
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() == "https" and parsed.hostname:
            url = candidate
            break
    code = ""
    # Never interpret a hostname or URL path such as ``device-login-auth`` as
    # a device code.  Login URLs are returned separately above.
    code_text = _URL_RE.sub(" ", text).upper()
    labelled = _LABELLED_CODE_RE.search(code_text)
    dashed = _DASHED_CODE_RE.search(code_text)
    if labelled:
        code = labelled.group(1)
    elif dashed:
        code = dashed.group(0)
    return url, code


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse)


def _assert_no_link_components(path: Path) -> None:
    current = path
    while True:
        if _is_link_or_reparse(current):
            raise ConfigurationError(
                "isolated subscription workspace base contains a link or reparse point"
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _is_transient_workspace_delete_error(exc: OSError) -> bool:
    return (
        getattr(exc, "winerror", None) in _WORKSPACE_TRANSIENT_WINERRORS
        or exc.errno in _WORKSPACE_TRANSIENT_ERRNOS
    )


def _retry_workspace_delete(operation: Callable[[], None]) -> None:
    for attempt in range(len(_WORKSPACE_DELETE_RETRY_DELAYS) + 1):
        try:
            operation()
            return
        except OSError as exc:
            if not _is_transient_workspace_delete_error(exc) or attempt == len(
                _WORKSPACE_DELETE_RETRY_DELAYS
            ):
                raise
            time.sleep(_WORKSPACE_DELETE_RETRY_DELAYS[attempt])


def _delete_subscription_workspace_tree(workspace: Path, root: Path) -> None:
    if workspace.parent != root:
        raise ConfigurationError("subscription workspace deletion escaped its root")
    lexical_workspace = Path(os.path.abspath(workspace))

    def make_writable_and_retry(function, path, exc_info) -> None:
        error = exc_info[1]
        if not isinstance(error, OSError):
            raise error
        failed = Path(os.path.abspath(path))
        try:
            failed.relative_to(lexical_workspace)
        except ValueError as exc:
            raise ConfigurationError(
                "subscription workspace deletion escaped its tombstone"
            ) from exc
        if _is_link_or_reparse(failed):
            # Never chmod through a symlink/junction. shutil.rmtree treats it
            # as a leaf; an unlink/rmdir sharing failure is retried outside.
            raise error
        try:
            details = failed.lstat()
            os.chmod(failed, details.st_mode | stat.S_IWRITE)
        except OSError:
            raise error
        function(path)

    def remove_once() -> None:
        if not workspace.exists():
            return
        if _is_link_or_reparse(workspace):
            raise ConfigurationError(
                "refusing to remove a linked/reparse subscription workspace"
            )
        shutil.rmtree(workspace, onerror=make_writable_and_retry)

    _retry_workspace_delete(remove_once)


def _detach_and_delete_subscription_workspace(workspace: Path, root: Path) -> None:
    if workspace.parent != root or not workspace.name.startswith("run-"):
        raise ConfigurationError("subscription workspace target is not managed")
    _assert_no_link_components(root)
    if not workspace.exists():
        return
    if _is_link_or_reparse(workspace):
        raise ConfigurationError(
            "refusing to remove a linked/reparse subscription workspace"
        )
    tombstone = root / f".delete-{workspace.name}-{uuid.uuid4().hex}"
    if tombstone.parent != root or not _WORKSPACE_TOMBSTONE_RE.fullmatch(
        tombstone.name
    ):
        raise ConfigurationError("subscription workspace tombstone is invalid")
    _retry_workspace_delete(lambda: workspace.rename(tombstone))
    # A persistent child-process cwd or antivirus handle may outlive close().
    # Leave only the inert tombstone; the next workspace creation reaps it.
    _delete_subscription_workspace_tree(tombstone, root)


def _reap_subscription_workspace_tombstones(root: Path) -> None:
    """Best-effort bounded cleanup of previously detached workspaces."""

    try:
        entries = list(root.iterdir())
    except OSError:
        return
    checked = 0
    for candidate in entries:
        if checked >= _MAX_WORKSPACE_TOMBSTONES_PER_PASS:
            break
        if not _WORKSPACE_TOMBSTONE_RE.fullmatch(candidate.name):
            continue
        checked += 1
        if candidate.parent != root or _is_link_or_reparse(candidate):
            continue
        with contextlib.suppress(OSError, ConfigurationError):
            _delete_subscription_workspace_tree(candidate, root)


def _subscription_workspace(
    environ: Mapping[str, str] | None,
) -> tuple[Path, _SubscriptionWorkspaceOwner]:
    source = os.environ if environ is None else environ
    base = source.get("LOCALAPPDATA") or source.get("TEMP") or tempfile.gettempdir()
    base_path = Path(base).expanduser()
    if not base_path.is_absolute():
        raise ConfigurationError("isolated subscription workspace base must be absolute")
    try:
        if not base_path.is_dir():
            raise ConfigurationError(
                "isolated subscription workspace base is not an existing directory"
            )
        _assert_no_link_components(base_path)
        resolved_base = base_path.resolve(strict=True)
        root = resolved_base / "ReplayLab" / "ai" / "subscription-workspaces"
        root.mkdir(parents=True, exist_ok=True)
        _assert_no_link_components(root)
        resolved_root = root.resolve(strict=True)
        resolved_root.relative_to(resolved_base)
        _reap_subscription_workspace_tombstones(resolved_root)
    except OSError as exc:
        raise ConfigurationError(
            f"cannot create isolated subscription workspace: {exc}"
        ) from exc
    except ValueError as exc:
        raise ConfigurationError(
            "isolated subscription workspace escaped its trusted base"
        ) from exc

    workspace = Path(tempfile.mkdtemp(prefix="run-", dir=str(resolved_root)))
    owner = _SubscriptionWorkspaceOwner(workspace, resolved_root)
    try:
        _assert_no_link_components(workspace)
        resolved = workspace.resolve(strict=True)
        resolved.relative_to(resolved_root)
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise ConfigurationError(
                "isolated subscription workspace is not a new empty directory"
            )
        return resolved, owner
    except Exception:
        owner.cleanup()
        raise


async def _drain_login_stream(
    stream: asyncio.StreamReader | None,
    budget: list[int],
    seen: set[tuple[str, str]],
    on_authorization: Callable[[str, str], None],
) -> None:
    if stream is None:
        return
    pending = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        budget[0] += len(chunk)
        if budget[0] > _MAX_CAPTURED_OUTPUT_BYTES:
            raise ConnectorError("provider login output exceeded the 1 MiB safety limit")
        pending.extend(chunk)
        if len(pending) > _MAX_CAPTURED_LINE_BYTES and b"\n" not in pending:
            raise ConnectorError("provider login emitted a line over the 64 KiB safety limit")
        while b"\n" in pending:
            raw, _, remainder = pending.partition(b"\n")
            pending = bytearray(remainder)
            if len(raw) > _MAX_CAPTURED_LINE_BYTES:
                raise ConnectorError(
                    "provider login emitted a line over the 64 KiB safety limit"
                )
            fields = _authorization_fields(raw.decode("utf-8", errors="replace"))
            if fields != ("", "") and fields not in seen:
                seen.add(fields)
                on_authorization(*fields)
    if pending:
        if len(pending) > _MAX_CAPTURED_LINE_BYTES:
            raise ConnectorError(
                "provider login emitted a line over the 64 KiB safety limit"
            )
        fields = _authorization_fields(pending.decode("utf-8", errors="replace"))
        if fields != ("", "") and fields not in seen:
            seen.add(fields)
            on_authorization(*fields)


async def _wait_captured_login(
    process: asyncio.subprocess.Process,
    cancel: asyncio.Event,
    *,
    timeout: float,
    on_authorization: Callable[[str, str], None],
) -> None:
    budget = [0]
    seen: set[tuple[str, str]] = set()
    readers = {
        asyncio.create_task(_drain_login_stream(process.stdout, budget, seen, on_authorization)),
        asyncio.create_task(_drain_login_stream(process.stderr, budget, seen, on_authorization)),
    }
    waiter = asyncio.create_task(process.wait())
    cancelled = asyncio.create_task(cancel.wait())
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await _terminate_async_process_tree(process)
                raise ConnectorError("provider login timed out")
            done, _ = await asyncio.wait(
                {waiter, cancelled, *readers},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await _terminate_async_process_tree(process)
                raise ConnectorError("provider login timed out")
            if cancelled in done:
                await _terminate_async_process_tree(process)
                raise SubscriptionCancelled("subscription login cancelled")
            failed_reader = next(
                (task for task in done & readers if task.exception() is not None), None
            )
            if failed_reader is not None:
                await _terminate_async_process_tree(process)
                raise failed_reader.exception()  # type: ignore[misc]
            readers.difference_update(done)
            if waiter in done:
                # A spawned browser or helper may inherit stdout/stderr even
                # after the official login process exits.  Continue accepting
                # final bounded output briefly, but keep cancellation live and
                # never wait indefinitely for inherited pipe handles to close.
                drain_deadline = min(
                    deadline,
                    asyncio.get_running_loop().time() + _POST_EXIT_DRAIN_SECONDS,
                )
                while readers:
                    drain_remaining = (
                        drain_deadline - asyncio.get_running_loop().time()
                    )
                    if drain_remaining <= 0:
                        break
                    drained, _ = await asyncio.wait(
                        {cancelled, *readers},
                        timeout=drain_remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not drained:
                        break
                    if cancelled in drained:
                        raise SubscriptionCancelled("subscription login cancelled")
                    failed_reader = next(
                        (
                            task
                            for task in drained & readers
                            if task.exception() is not None
                        ),
                        None,
                    )
                    if failed_reader is not None:
                        raise failed_reader.exception()  # type: ignore[misc]
                    readers.difference_update(drained)
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                readers.clear()
                if int(waiter.result()) != 0:
                    raise ConnectorError(
                        f"provider login exited with code {int(waiter.result())}"
                    )
                return
    except asyncio.CancelledError:
        await _terminate_async_process_tree(process)
        raise
    finally:
        for task in {waiter, cancelled, *readers}:
            if not task.done():
                task.cancel()
        await asyncio.gather(waiter, cancelled, *readers, return_exceptions=True)


async def _terminate_async_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        taskkill = system_root / "System32" / "taskkill.exe"
        if taskkill.is_file():
            with contextlib.suppress(Exception):
                killer = await asyncio.create_subprocess_exec(
                    str(taskkill),
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                await asyncio.wait_for(killer.wait(), timeout=5.0)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=5.0)


async def _wait_popen(
    process: subprocess.Popen[Any], cancel: asyncio.Event, *, timeout: float
) -> None:
    waiter = asyncio.create_task(asyncio.to_thread(process.wait))
    cancelled = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait(
            {waiter, cancelled}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if cancelled in done:
            await asyncio.to_thread(_terminate_popen_tree, process)
            raise SubscriptionCancelled("subscription terminal login cancelled")
        if waiter not in done:
            await asyncio.to_thread(_terminate_popen_tree, process)
            raise ConnectorError("interactive provider login timed out")
        if int(waiter.result()) != 0:
            raise ConnectorError(
                f"interactive provider login exited with code {int(waiter.result())}"
            )
    except asyncio.CancelledError:
        await asyncio.to_thread(_terminate_popen_tree, process)
        raise
    finally:
        for task in (waiter, cancelled):
            if not task.done():
                task.cancel()
        await asyncio.gather(waiter, cancelled, return_exceptions=True)


def _terminate_popen_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        taskkill = system_root / "System32" / "taskkill.exe"
        if taskkill.is_file():
            with contextlib.suppress(Exception):
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5.0,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    if process.poll() is None:
        with contextlib.suppress(Exception):
            process.kill()
    with contextlib.suppress(Exception):
        process.wait(timeout=5.0)


__all__ = [
    "SubscriptionBackend",
    "SubscriptionCancelled",
    "SubscriptionResult",
    "detect_subscription",
]
