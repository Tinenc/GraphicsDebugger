from __future__ import annotations

import ast
import asyncio
import errno
import os
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from replaylab_agent.cli import _parser, _subscription_command
from replaylab_agent.catalog import get_preset
from replaylab_agent.errors import ConfigurationError, ConnectorError
from replaylab_agent.profiles import Profile, ProfileStore


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_known_subscription_message_ids_are_covered_by_qrenderdoc() -> None:
    production_sources = [
        REPOSITORY_ROOT / "replaylab" / "src" / "replaylab_agent" / "cli.py",
        REPOSITORY_ROOT / "replaylab" / "src" / "replaylab_agent" / "connectors"
        / "subscription.py",
        REPOSITORY_ROOT / "replaylab" / "src" / "replaylab_agent" / "connectors"
        / "claude_auth.py",
    ]
    python_ids = set()
    for path in production_sources:
        python_ids.update(
            re.findall(
                r"[\"'](subscription\.[a-z0-9-]+)[\"']",
                path.read_text(encoding="utf-8"),
            )
        )

    assistant_source = (
        REPOSITORY_ROOT / "qrenderdoc" / "Windows" / "AI" / "AIAssistant.cpp"
    ).read_text(encoding="utf-8")
    table_start = assistant_source.index(
        "static const ProviderRuntimeTranslation providerRuntimeTranslations[]"
    )
    table_end = assistant_source.index("};", table_start)
    native_ids = set(
        re.findall(
            r'\{"(subscription\.[a-z0-9-]+)"',
            assistant_source[table_start:table_end],
        )
    )

    assert python_ids
    assert python_ids <= native_ids, sorted(python_ids - native_ids)


def test_qrenderdoc_exposes_agent_catalog_and_preserves_acp_save_contract() -> None:
    from replaylab_agent.catalog import PRESETS

    assistant_source = (
        REPOSITORY_ROOT / "qrenderdoc" / "Windows" / "AI" / "AIAssistant.cpp"
    ).read_text(encoding="utf-8")
    filter_start = assistant_source.index("bool supportedQRenderDocConnection(")
    filter_end = assistant_source.index("QString internalConnectionId()", filter_start)
    filter_source = assistant_source[filter_start:filter_end]
    translation_start = assistant_source.index(
        "static const ProviderCatalogTranslation providerCatalogTranslations[]"
    )
    translation_end = assistant_source.index("};", translation_start)
    translation_source = assistant_source[translation_start:translation_end]
    entry_pattern = re.compile(
        r'\{"(?P<preset>[^"]+)",\s*"(?P<field>[^"]+)",\s*'
        r'QT_TRANSLATE_NOOP3\(\s*"AIAssistant",\s*'
        r'(?P<source>(?:"(?:\\.|[^"\\])*"\s*)+),\s*'
        r'"(?P<disambiguation>[^"]+)"\)\},',
        re.DOTALL,
    )
    translated_catalog = {}
    for match in entry_pattern.finditer(translation_source):
        translated_catalog[(match.group("preset"), match.group("field"))] = "".join(
            ast.literal_eval(token)
            for token in re.findall(r'"(?:\\.|[^"\\])*"', match.group("source"))
        )

    assert 'const QString acpProtocol = lit("acp")' in filter_source
    assert "protocol == acpProtocol" in filter_source
    for preset in (item for item in PRESETS if item.kind == "agent"):
        assert f'{{"{preset.id}", "title"' in translation_source
    for preset in PRESETS:
        for field in ("title", "note", "install_hint", "subscription_instruction"):
            source = getattr(preset, field)
            if source:
                assert translated_catalog[(preset.id, field)] == source

    assert 'tr("Agent Subscription (ACP / app-server)")' in assistant_source
    assert "QPlainTextEdit *agentCommand" in assistant_source
    assert "QCheckBox *trustACP" in assistant_source
    assert 'preset.value(lit("subscriptionLoginMode"))' in assistant_source
    assert "connectSubscription->setEnabled(!editing || savedProviderSelected)" in assistant_source
    assert "if(!selectedProviderMatchesEditingProfile())" in assistant_source
    assert "state->presetId = selectedPresetId()" in assistant_source
    assert "agentProvider->setEnabled(false)" in assistant_source
    assert "modelsBox->setEnabled(false)" in assistant_source
    assert "profileExists && selectedProviderMatchesEditingProfile()" in assistant_source
    assert "if(index < 0 && selectedProviderMatchesEditingProfile())" in assistant_source
    assert 'QLatin1String("subscription.terminal-opened")' in assistant_source
    assert 'lit("subscription_instruction"), fallback, &matched' in assistant_source
    assert 'lit("allow_unsandboxed_agent=true")' in assistant_source
    assert 'arguments << lit("--command-json")' in assistant_source
    assert 'arguments << lit("--command-json") << lit("[]")' in assistant_source
    assert "exact non-editable command" in assistant_source
    assert "read-only command" not in assistant_source

    editor_start = assistant_source.index(
        "std::function<void(const QJsonObject &)> editConnection"
    )
    dialog_start = assistant_source.index("QDialog dialog(page);", editor_start)
    assert "runCLISync(" not in assistant_source[editor_start:dialog_start]

    credential_start = assistant_source.index(
        "auto refreshCredentialStatus = [&]() {", dialog_start
    )
    credential_end = assistant_source.index(
        "auto deleteStoredCredential", credential_start
    )
    credential_source = assistant_source[credential_start:credential_end]
    assert "runCLISync(" not in credential_source
    assert "(*profileCommandRunner)(" in credential_source

    auth_start = assistant_source.index("auto updateAuthStatus = [&]() {", dialog_start)
    auth_end = assistant_source.index("connect(agentProvider,", auth_start)
    auth_source = assistant_source[auth_start:auth_end]
    assert "runCLISync(" not in auth_source
    assert "(*profileCommandRunner)(" in auth_source


def test_known_subscription_events_require_ids_except_raw_exception_diagnostics() -> None:
    allowed_raw_exceptions = {"SubscriptionCancelled", "ReplayLabAIError"}
    checks = [
        (
            REPOSITORY_ROOT / "replaylab" / "src" / "replaylab_agent" / "cli.py",
            "backend",
        ),
        (
            REPOSITORY_ROOT / "replaylab" / "src" / "replaylab_agent" / "connectors"
            / "subscription.py",
            "self",
        ),
    ]
    violations = []
    for path, receiver in checks:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "event"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == receiver
            ):
                continue
            if any(keyword.arg == "messageId" for keyword in node.keywords):
                continue
            ancestor = parents.get(node)
            handler = None
            while ancestor is not None:
                if isinstance(ancestor, ast.ExceptHandler):
                    handler = ancestor
                    break
                ancestor = parents.get(ancestor)
            exception_name = (
                handler.type.id
                if handler is not None and isinstance(handler.type, ast.Name)
                else ""
            )
            if exception_name not in allowed_raw_exceptions:
                violations.append((str(path), node.lineno, exception_name))

    assert violations == []


class FakeSubscriptionBackend:
    def __init__(self, profile, *, timeout, emit, installed=True):
        self.profile = profile
        self.timeout = timeout
        self.emit = emit
        self.detection = {
            "installed": installed,
            "message": "detected" if installed else "install official CLI",
            "installHint": "https://example.invalid/install",
        }
        self.calls: list[tuple[str, str]] = []

    def event(self, kind, status, message, **fields):
        payload = {
            "kind": kind,
            "status": status,
            "profile": self.profile.id,
            "message": message,
            "installed": self.detection["installed"],
            "authenticated": fields.pop("authenticated", None),
            "authUrl": fields.pop("authUrl", ""),
            "userCode": fields.pop("userCode", ""),
            "terminalOpened": fields.pop("terminalOpened", False),
            "models": fields.pop("models", []),
            "testCompleted": fields.pop("testCompleted", False),
        }
        payload.update(fields)
        self.emit(payload)
        return payload

    async def connect(self, flow, cancel):
        assert cancel.is_set() is False
        self.calls.append(("connect", flow))
        return SimpleNamespace(
            authenticated=True,
            models=("model-a", "model-b"),
            account={"planType": "plus"},
            message="connected and tested",
            terminal_opened=False,
        )

    async def test(self, cancel):
        assert cancel.is_set() is False
        self.calls.append(("test", ""))
        return SimpleNamespace(
            authenticated=True,
            models=("model-a",),
            account=None,
            message="tested",
            terminal_opened=False,
        )


def _store(tmp_path, profile=None):
    store = ProfileStore(tmp_path / "profiles.json")
    store.add(profile or Profile("codex-main", "codex"))
    return store


def test_subscription_parser_requires_gate_and_preserves_codex_flow() -> None:
    parser = _parser()
    status = parser.parse_args(
        ["profile", "subscription", "status", "--profile", "codex-main", "--json"]
    )
    assert status.subscription_command == "status"
    connect = parser.parse_args(
        [
            "profile",
            "subscription",
            "connect",
            "--profile",
            "codex-main",
            "--flow",
            "device",
            "--start-gated",
            "--json",
        ]
    )
    assert connect.flow == "device"
    assert connect.start_gated is True
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["profile", "subscription", "connect", "--profile", "codex-main", "--json"]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["profile", "subscription", "test", "--profile", "codex-main", "--json"]
        )


@pytest.mark.asyncio
async def test_subscription_status_only_detects_and_never_constructs_backend(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli
    import replaylab_agent.connectors.subscription as subscription

    emitted: list[dict] = []
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)
    monkeypatch.setattr(
        subscription,
        "detect_subscription",
        lambda profile: {
            "kind": "subscriptionStatus",
            "status": "installed",
            "profile": profile.id,
            "message": "detected without spawning",
            "installed": True,
            "authenticated": None,
            "authUrl": "",
            "userCode": "",
            "terminalOpened": False,
            "models": [],
            "testCompleted": False,
        },
    )
    args = _parser().parse_args(
        ["profile", "subscription", "status", "--profile", "codex-main", "--json"]
    )

    def forbidden_backend(*args, **kwargs):
        raise AssertionError("status must not construct a spawning backend")

    assert await _subscription_command(
        args, _store(tmp_path), backend_factory=forbidden_backend
    ) == 0
    assert len(emitted) == 1
    assert emitted[0]["authenticated"] is None


@pytest.mark.asyncio
async def test_gate_cancel_is_structured_and_never_calls_vendor(tmp_path, monkeypatch) -> None:
    import replaylab_agent.cli as cli

    emitted: list[dict] = []
    created: list[FakeSubscriptionBackend] = []
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)

    def cancel_gate(loop, start, cancel):
        loop.call_soon(cancel.set)

    monkeypatch.setattr(cli, "_start_subscription_control_reader", cancel_gate)

    def factory(profile, *, timeout, emit):
        backend = FakeSubscriptionBackend(profile, timeout=timeout, emit=emit)
        created.append(backend)
        return backend

    args = _parser().parse_args(
        [
            "profile",
            "subscription",
            "connect",
            "--profile",
            "codex-main",
            "--start-gated",
            "--json",
        ]
    )
    assert await _subscription_command(args, _store(tmp_path), factory) == 1
    assert created[0].calls == []
    assert [item["kind"] for item in emitted] == [
        "waitingForStart",
        "subscriptionCompleted",
    ]
    assert emitted[-1]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_connect_and_test_emit_stable_terminal_events(tmp_path, monkeypatch) -> None:
    import replaylab_agent.cli as cli

    emitted: list[dict] = []
    backends: list[FakeSubscriptionBackend] = []
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)

    def start_gate(loop, start, cancel):
        loop.call_soon(start.set)

    monkeypatch.setattr(cli, "_start_subscription_control_reader", start_gate)

    def factory(profile, *, timeout, emit):
        backend = FakeSubscriptionBackend(profile, timeout=timeout, emit=emit)
        backends.append(backend)
        return backend

    connect = _parser().parse_args(
        [
            "profile",
            "subscription",
            "connect",
            "--profile",
            "codex-main",
            "--flow",
            "device",
            "--start-gated",
            "--json",
        ]
    )
    assert await _subscription_command(connect, _store(tmp_path), factory) == 0
    assert backends[-1].calls == [("connect", "device")]
    assert [item["kind"] for item in emitted] == [
        "waitingForStart",
        "connectStarted",
        "testCompleted",
        "subscriptionCompleted",
    ]
    assert emitted[-1]["models"] == ["model-a", "model-b"]
    assert emitted[-1]["authenticated"] is True
    assert emitted[-1]["testCompleted"] is True

    emitted.clear()
    test = _parser().parse_args(
        [
            "profile",
            "subscription",
            "test",
            "--profile",
            "codex-main",
            "--start-gated",
            "--json",
        ]
    )
    assert await _subscription_command(test, _store(tmp_path / "test"), factory) == 0
    assert backends[-1].calls == [("test", "")]
    assert [item["kind"] for item in emitted] == [
        "waitingForStart",
        "testStarted",
        "testCompleted",
    ]


@pytest.mark.asyncio
async def test_first_codex_browser_login_reaches_both_terminal_events(
    tmp_path, monkeypatch
) -> None:
    """Exercise login -> account/read -> model/list through the CLI contract."""

    import replaylab_agent.cli as cli
    import replaylab_agent.connectors.subscription as subscription
    from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector

    emitted: list[dict] = []
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)
    monkeypatch.setattr(
        cli,
        "_start_subscription_control_reader",
        lambda loop, start, cancel: loop.call_soon(start.set),
    )
    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: sys.executable,
    )

    fixture = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"

    def factory(profile, *, timeout, emit):
        return subscription.SubscriptionBackend(
            profile,
            timeout=timeout,
            emit=emit,
            environ=dict(os.environ) | {"LOCALAPPDATA": str(tmp_path)},
            codex_factory=lambda resolved, bounded_timeout, environment: (
                CodexAppServerConnector(
                    [sys.executable, str(fixture), "login-race"],
                    cwd=tmp_path,
                    timeout=bounded_timeout,
                    env=environment,
                )
            ),
        )

    args = _parser().parse_args(
        [
            "profile",
            "subscription",
            "connect",
            "--profile",
            "codex-main",
            "--flow",
            "browser",
            "--timeout",
            "5",
            "--start-gated",
            "--json",
        ]
    )

    assert await _subscription_command(args, _store(tmp_path), factory) == 0
    assert [item["kind"] for item in emitted] == [
        "waitingForStart",
        "connectStarted",
        "loginStarted",
        "loginCompleted",
        "testCompleted",
        "subscriptionCompleted",
    ]
    assert emitted[-2]["authenticated"] is True
    assert emitted[-2]["testCompleted"] is True
    assert emitted[-1]["authenticated"] is True
    assert emitted[-1]["testCompleted"] is True
    assert emitted[-1]["models"] == ["codex-test"]


@pytest.mark.asyncio
async def test_missing_cli_returns_structured_completion_after_gate(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.cli as cli

    emitted: list[dict] = []
    monkeypatch.setattr(cli, "_print_machine_json", emitted.append)
    monkeypatch.setattr(
        cli,
        "_start_subscription_control_reader",
        lambda loop, start, cancel: loop.call_soon(start.set),
    )

    def factory(profile, *, timeout, emit):
        return FakeSubscriptionBackend(
            profile, timeout=timeout, emit=emit, installed=False
        )

    args = _parser().parse_args(
        [
            "profile",
            "subscription",
            "test",
            "--profile",
            "codex-main",
            "--start-gated",
            "--json",
        ]
    )
    assert await _subscription_command(args, _store(tmp_path), factory) == 2
    assert emitted[-1]["kind"] == "testCompleted"
    assert emitted[-1]["installed"] is False
    assert emitted[-1]["status"] == "missing"


@pytest.mark.asyncio
async def test_codex_backend_reuses_account_login_and_model_list(monkeypatch) -> None:
    import replaylab_agent.connectors.subscription as subscription

    events: list[dict] = []

    class FakeCodex:
        def __init__(self):
            self.account_reads = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def account_read(self):
            self.account_reads += 1
            if self.account_reads == 1:
                return {"account": None}
            return {
                "account": {
                    "type": "chatgpt",
                    "planType": "plus",
                    "email": "private@example.test",
                }
            }

        async def login_start(self, *, flow):
            assert flow == "device"
            return {
                "loginId": "login-1",
                "verificationUrl": "https://auth.example.test/device",
                "userCode": "ABCD-EFGH",
            }

        async def wait_login(self, login_id, *, timeout):
            assert login_id == "login-1"
            assert timeout == 5
            return {"loginId": login_id, "success": True}

        async def cancel_login(self, login_id):
            raise AssertionError("successful login must not be cancelled")

        async def list_models(self):
            return [{"id": "codex-a"}, {"model": "codex-b"}]

    fake = FakeCodex()
    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: "C:/trusted/codex.exe",
    )
    backend = subscription.SubscriptionBackend(
        Profile("codex-main", "codex", command=["ignored-user-command"]),
        timeout=5,
        emit=events.append,
        environ={},
        codex_factory=lambda profile, timeout, environment: fake,
    )
    result = await backend.connect("device", asyncio.Event())
    assert result.authenticated is True
    assert result.models == ("codex-a", "codex-b")
    assert result.account == {"type": "chatgpt", "planType": "plus"}
    assert result.message_id == "subscription.codex-sign-in-test-succeeded"
    assert events[0]["kind"] == "loginStarted"
    assert events[0]["messageId"] == "subscription.codex-browser-complete"
    assert events[0]["authUrl"] == "https://auth.example.test/device"
    assert events[0]["userCode"] == "ABCD-EFGH"
    assert events[1]["kind"] == "loginCompleted"
    assert events[1]["messageId"] == "subscription.codex-login-completed"


@pytest.mark.asyncio
async def test_codex_signed_out_result_has_stable_runtime_message_id(monkeypatch) -> None:
    import replaylab_agent.connectors.subscription as subscription

    class SignedOutCodex:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def account_read(self):
            return {"account": None}

    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: "C:/trusted/codex.exe",
    )
    backend = subscription.SubscriptionBackend(
        Profile("codex-main", "codex"),
        timeout=5,
        emit=lambda event: None,
        environ={},
        codex_factory=lambda profile, timeout, environment: SignedOutCodex(),
    )

    result = await backend.test(asyncio.Event())

    assert result.authenticated is False
    assert result.message == "Codex is signed out."
    assert result.message_id == "subscription.codex-signed-out"


def test_known_subscription_detection_uses_catalog_command_not_profile_override(
    monkeypatch,
) -> None:
    import replaylab_agent.connectors.subscription as subscription

    commands: list[str] = []

    def resolve(profile, *, environ=None):
        commands.append(subscription._runtime_argv(profile)[0])
        return "C:/trusted/codex.exe"

    monkeypatch.setattr(subscription, "resolve_agent_executable", resolve)
    status = subscription.detect_subscription(
        Profile("codex-main", "codex", command=["untrusted-override"]), environ={}
    )
    assert status["installed"] is True
    assert status["messageId"] == "subscription.cli-detected"
    assert status["messageArgs"] == ["Codex / ChatGPT subscription"]
    assert commands == ["codex"]


def test_missing_subscription_preserves_localizable_official_install_hint(
    monkeypatch,
) -> None:
    import replaylab_agent.connectors.subscription as subscription

    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: "",
    )
    status = subscription.detect_subscription(Profile("codex-main", "codex"), environ={})

    assert status["installed"] is False
    assert status["messageId"] == "subscription.cli-missing"
    assert status["messageArgs"][0] == "codex"
    assert status["messageArgs"][1] == status["installHint"]
    assert "https://developers.openai.com/codex/cli/" in status["messageArgs"][1]

    assistant_source = (
        REPOSITORY_ROOT / "qrenderdoc" / "Windows" / "AI" / "AIAssistant.cpp"
    ).read_text(encoding="utf-8")
    assert 'localizeProviderCatalogText(presetId, lit("install_hint"), value)' in assistant_source


def test_subscription_backend_uses_dedicated_workspace(tmp_path, monkeypatch) -> None:
    import replaylab_agent.connectors.subscription as subscription

    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: "C:/trusted/codex.exe",
    )
    backend = subscription.SubscriptionBackend(
        Profile("codex-main", "codex"),
        timeout=5,
        emit=lambda event: None,
        environ={"LOCALAPPDATA": str(tmp_path), "PATH": ""},
    )

    expected_parent = (
        tmp_path / "ReplayLab" / "ai" / "subscription-workspaces"
    ).resolve()
    assert backend.workspace.parent == expected_parent
    assert backend.workspace.name.startswith("run-")
    assert backend.workspace.is_dir()
    assert list(backend.workspace.iterdir()) == []
    assert backend.workspace != Path.cwd().resolve()
    workspace = backend.workspace
    backend.close()
    assert not workspace.exists()


def test_subscription_workspace_close_clears_read_only_files(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.connectors.subscription as subscription

    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: "C:/trusted/codex.exe",
    )
    backend = subscription.SubscriptionBackend(
        Profile("codex-main", "codex"),
        timeout=5,
        emit=lambda event: None,
        environ={"LOCALAPPDATA": str(tmp_path), "PATH": ""},
    )
    workspace = backend.workspace
    locked = workspace / "provider.lock"
    locked.write_bytes(b"temporary")
    locked.chmod(stat.S_IREAD)

    backend.close()

    assert not workspace.exists()


def test_subscription_workspace_close_is_bounded_and_reaps_tombstone(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.connectors.subscription as subscription

    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: "C:/trusted/codex.exe",
    )
    backend = subscription.SubscriptionBackend(
        Profile("codex-main", "codex"),
        timeout=5,
        emit=lambda event: None,
        environ={"LOCALAPPDATA": str(tmp_path), "PATH": ""},
    )
    workspace = backend.workspace
    (workspace / "child-process-cwd").write_text("busy", encoding="utf-8")
    real_rmtree = subscription.shutil.rmtree
    attempts = 0
    sleeps: list[float] = []

    def always_busy(path, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise PermissionError(errno.EACCES, "simulated child cwd lock", str(path))

    monkeypatch.setattr(subscription.shutil, "rmtree", always_busy)
    monkeypatch.setattr(subscription.time, "sleep", sleeps.append)

    # Python 3.10 TemporaryDirectory.cleanup recursively re-entered here.
    # close() must instead return after a finite number of attempts.
    backend.close()
    backend.close()

    root = workspace.parent
    tombstones = list(root.glob(".delete-run-*"))
    assert not workspace.exists()
    assert len(tombstones) == 1
    assert attempts == len(subscription._WORKSPACE_DELETE_RETRY_DELAYS) + 1
    assert sleeps == list(subscription._WORKSPACE_DELETE_RETRY_DELAYS)

    # A later connection reaps the inert tombstone without touching a live
    # run-* workspace.
    monkeypatch.setattr(subscription.shutil, "rmtree", real_rmtree)
    sleeps.clear()
    next_backend = subscription.SubscriptionBackend(
        Profile("codex-next", "codex"),
        timeout=5,
        emit=lambda event: None,
        environ={"LOCALAPPDATA": str(tmp_path), "PATH": ""},
    )
    assert not tombstones[0].exists()
    assert next_backend.workspace.is_dir()
    next_backend.close()


def test_subscription_workspace_rejects_relative_base(monkeypatch) -> None:
    import replaylab_agent.connectors.subscription as subscription

    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: "C:/trusted/codex.exe",
    )
    with pytest.raises(ConfigurationError, match="base must be absolute"):
        subscription.SubscriptionBackend(
            Profile("codex-main", "codex"),
            timeout=5,
            emit=lambda event: None,
            environ={"LOCALAPPDATA": "relative-cache", "PATH": ""},
        )


@pytest.mark.asyncio
async def test_captured_login_aggregates_split_url_and_device_code(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.connectors.subscription as subscription

    stdout = asyncio.StreamReader()
    stdout.feed_data(b"Open https://auth.example.test/device\n")
    stdout.feed_eof()
    stderr = asyncio.StreamReader()
    stderr.feed_data(b"Device code: ABCD-EFGH\n")
    stderr.feed_eof()

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self):
            self.stdout = stdout
            self.stderr = stderr

        async def wait(self):
            return 0

    launch: dict[str, object] = {}

    async def fake_create_subprocess_exec(*argv, **kwargs):
        launch["argv"] = argv
        launch["cwd"] = kwargs.get("cwd")
        return FakeProcess()

    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: "C:/trusted/kimi.exe",
    )
    monkeypatch.setattr(
        subscription,
        "resolve_known_vendor_executable",
        lambda preset, command, *, environ=None: "C:/trusted/kimi.exe",
    )
    monkeypatch.setattr(
        subscription.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    events: list[dict] = []
    backend = subscription.SubscriptionBackend(
        Profile(
            "kimi-main",
            "kimi-acp",
            options={"allow_unsandboxed_agent": True},
        ),
        timeout=5,
        emit=events.append,
        environ={"LOCALAPPDATA": str(tmp_path), "PATH": ""},
    )

    try:
        await backend._run_captured_login(
            backend._resolved_profile(), "device", asyncio.Event()
        )

        progress = [event for event in events if event["kind"] == "loginProgress"]
        assert progress
        assert progress[-1]["authUrl"] == "https://auth.example.test/device"
        assert progress[-1]["userCode"] == "ABCD-EFGH"
        assert launch["argv"] == ("C:/trusted/kimi.exe", "login")
        assert launch["cwd"] == str(backend.workspace)
        completed = [event for event in events if event["kind"] == "loginCompleted"]
        assert completed[-1]["status"] == "completed"
        assert completed[-1]["authenticated"] is None
    finally:
        backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow", "login_flag"),
    [("browser", "--web-flow"), ("device", "--device-code")],
)
async def test_copilot_login_uses_official_flow_flag(
    flow, login_flag, tmp_path, monkeypatch
) -> None:
    import replaylab_agent.connectors.subscription as subscription

    launches: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(*argv, **kwargs):
        launches.append(argv)
        stdout = asyncio.StreamReader()
        stdout.feed_eof()
        stderr = asyncio.StreamReader()
        stderr.feed_eof()

        class FakeProcess:
            pid = 12345
            returncode = 0

            async def wait(self):
                return 0

        process = FakeProcess()
        process.stdout = stdout
        process.stderr = stderr
        return process

    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: "C:/trusted/copilot.exe",
    )
    monkeypatch.setattr(
        subscription,
        "resolve_known_vendor_executable",
        lambda preset, command, *, environ=None: "C:/trusted/copilot.exe",
    )
    monkeypatch.setattr(
        subscription.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    backend = subscription.SubscriptionBackend(
        Profile(
            "copilot-main",
            "copilot-acp",
            options={"allow_unsandboxed_agent": True},
        ),
        timeout=5,
        emit=lambda event: None,
        environ={"LOCALAPPDATA": str(tmp_path), "PATH": ""},
    )
    try:
        profile = backend._resolved_profile()
        await backend._run_captured_login(profile, flow, asyncio.Event())

        assert launches == [("C:/trusted/copilot.exe", "login", login_flag)]
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_cursor_login_uses_captured_official_browser_flow(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.connectors.subscription as subscription

    launch: dict[str, object] = {}

    async def fake_create_subprocess_exec(*argv, **kwargs):
        launch["argv"] = argv
        launch["env"] = kwargs.get("env")
        stdout = asyncio.StreamReader()
        stdout.feed_data(b"Open https://cursor.com/login/test\n")
        stdout.feed_eof()
        stderr = asyncio.StreamReader()
        stderr.feed_eof()

        class FakeProcess:
            pid = 12345
            returncode = 0

            async def wait(self):
                return 0

        process = FakeProcess()
        process.stdout = stdout
        process.stderr = stderr
        return process

    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: "C:/trusted/cursor-agent.exe",
    )
    monkeypatch.setattr(
        subscription,
        "resolve_known_vendor_executable",
        lambda preset, command, *, environ=None: "C:/trusted/cursor-agent.exe",
    )
    monkeypatch.setattr(
        subscription.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    events: list[dict] = []
    backend = subscription.SubscriptionBackend(
        Profile(
            "cursor-main",
            "cursor-acp",
            options={"allow_unsandboxed_agent": True},
        ),
        timeout=5,
        emit=events.append,
        environ={
            "LOCALAPPDATA": str(tmp_path),
            "PATH": "",
            "OPENAI_API_KEY": "must-not-leak",
        },
    )
    try:
        await backend._run_captured_login(
            backend._resolved_profile(), "browser", asyncio.Event()
        )

        assert launch["argv"] == ("C:/trusted/cursor-agent.exe", "login")
        environment = launch["env"]
        assert isinstance(environment, dict)
        assert environment["NO_OPEN_BROWSER"] == "1"
        cursor_config = Path(environment["CURSOR_CONFIG_DIR"])
        assert cursor_config == (
            tmp_path / "ReplayLab" / "cursor" / "cursor-main"
        ).resolve()
        assert environment["HOME"] == str(cursor_config)
        assert environment["USERPROFILE"] == str(cursor_config)
        assert (cursor_config / "cli-config.json").is_file()
        assert "OPENAI_API_KEY" not in environment
        progress = [event for event in events if event["kind"] == "loginProgress"]
        assert progress[-1]["authUrl"] == "https://cursor.com/login/test"
        assert progress[-1]["messageId"] == "subscription.browser-complete"
        started = [event for event in events if event["kind"] == "loginStarted"]
        assert started[-1]["messageId"] == "subscription.browser-waiting"
        completed = [event for event in events if event["kind"] == "loginCompleted"]
        assert completed[-1]["messageId"] == "subscription.acp-login-completed"
    finally:
        backend.close()


def test_device_authorization_parser_emits_only_url_and_code() -> None:
    from replaylab_agent.connectors.subscription import _authorization_fields

    assert _authorization_fields(
        "Visit https://auth.kimi.example/device and enter code ABCD-EFGH; token=secret"
    ) == ("https://auth.kimi.example/device", "ABCD-EFGH")
    assert _authorization_fields(
        "Visit https://device-login-auth.example.com/activate"
    ) == ("https://device-login-auth.example.com/activate", "")


@pytest.mark.skipif(os.name != "nt", reason="Windows console flags are platform-specific")
@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["gemini-acp", "qwen-acp", "codebuddy-acp"])
async def test_terminal_login_uses_catalog_command_and_contained_workspace(
    preset, tmp_path, monkeypatch
) -> None:
    import replaylab_agent.connectors.subscription as subscription

    launches: list[tuple[list[str], dict]] = []

    class FakeProcess:
        pid = 12345

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(argv, **kwargs):
        launches.append((list(argv), dict(kwargs)))
        return FakeProcess()

    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: (
            f"C:/trusted/{profile.preset.removesuffix('-acp')}.cmd"
        ),
    )
    monkeypatch.setattr(
        subscription,
        "resolve_known_vendor_executable",
        lambda preset, command, *, environ=None: f"C:/trusted/{command}.cmd",
    )
    backend = subscription.SubscriptionBackend(
        Profile(preset, preset, options={"allow_unsandboxed_agent": True}),
        timeout=5,
        emit=lambda event: None,
        environ={
            "LOCALAPPDATA": str(tmp_path),
            "PATH": "C:/trusted",
            "OPENAI_API_KEY": "must-not-leak",
        },
        popen_factory=fake_popen,
    )
    try:
        await backend._run_terminal_login(
            backend._resolved_profile(), asyncio.Event()
        )
        argv, kwargs = launches[0]
        assert argv == [
            f"C:/trusted/{preset.removesuffix('-acp')}.cmd",
            *get_preset(preset).subscription_login[1:],
        ]
        assert kwargs["cwd"] == str(backend.workspace)
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_CONSOLE
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        assert "OPENAI_API_KEY" not in kwargs["env"]
        if preset == "codebuddy-acp":
            assert kwargs["env"]["CODEBUDDY_CONFIG_DIR"] == str(
                (tmp_path / "ReplayLab" / "codebuddy" / preset).resolve()
            )
    finally:
        backend.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows console flags are platform-specific")
@pytest.mark.asyncio
async def test_claude_login_and_acp_share_the_profile_scoped_state_root(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.connectors.subscription as subscription

    launches: list[tuple[list[str], dict]] = []

    class FakeProcess:
        pid = 12345

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(argv, **kwargs):
        launches.append((list(argv), dict(kwargs)))
        return FakeProcess()

    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: "C:/trusted/claude-agent-acp.cmd",
    )
    monkeypatch.setattr(
        subscription,
        "resolve_known_vendor_executable",
        lambda preset, command, *, environ=None: "C:/trusted/claude.cmd",
    )
    backend = subscription.SubscriptionBackend(
        Profile(
            "claude-main",
            "claude-code-acp",
            options={"allow_unsandboxed_agent": True},
        ),
        timeout=5,
        emit=lambda event: None,
        environ={
            "LOCALAPPDATA": str(tmp_path),
            "PATH": "C:/trusted",
            "CLAUDE_CONFIG_DIR": str(tmp_path / "global-claude"),
            "OPENAI_API_KEY": "must-not-leak",
        },
        popen_factory=fake_popen,
    )
    try:
        await backend._run_terminal_login(
            backend._resolved_profile(), asyncio.Event()
        )
        argv, kwargs = launches[0]
        expected = (tmp_path / "ReplayLab" / "claude" / "claude-main").resolve()
        assert argv == ["C:/trusted/claude.cmd", "auth", "login"]
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(expected)
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] != str(tmp_path / "global-claude")
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert (backend.workspace / ".claude" / "settings.json").is_file()
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_terminal_wait_cancel_terminates_process_tree(monkeypatch) -> None:
    import replaylab_agent.connectors.subscription as subscription

    release = threading.Event()
    terminated: list[int] = []

    class BlockingProcess:
        pid = 12345

        def wait(self, timeout=None):
            release.wait(timeout)
            return -9

        def poll(self):
            return None if not release.is_set() else -9

    def terminate(process):
        terminated.append(process.pid)
        release.set()

    monkeypatch.setattr(subscription, "_terminate_popen_tree", terminate)
    cancel = asyncio.Event()
    task = asyncio.create_task(
        subscription._wait_popen(BlockingProcess(), cancel, timeout=5)
    )
    await asyncio.sleep(0.05)
    cancel.set()
    with pytest.raises(subscription.SubscriptionCancelled):
        await task
    assert terminated == [12345]


@pytest.mark.asyncio
async def test_captured_login_output_has_cumulative_bound() -> None:
    from replaylab_agent.connectors.subscription import _drain_login_stream

    stream = asyncio.StreamReader()
    for _ in range(260):
        stream.feed_data((b"x" * 4095) + b"\n")
    stream.feed_eof()
    with pytest.raises(ConnectorError, match="1 MiB"):
        await _drain_login_stream(stream, [0], set(), lambda url, code: None)


@pytest.mark.asyncio
async def test_captured_login_rejects_one_oversized_line_even_with_newline() -> None:
    from replaylab_agent.connectors.subscription import _drain_login_stream

    stream = asyncio.StreamReader()
    stream.feed_data((b"x" * (64 * 1024 + 1)) + b"\n")
    stream.feed_eof()
    with pytest.raises(ConnectorError, match="64 KiB"):
        await _drain_login_stream(stream, [0], set(), lambda url, code: None)


@pytest.mark.asyncio
async def test_captured_login_cancel_remains_live_after_parent_exit() -> None:
    import replaylab_agent.connectors.subscription as subscription

    stdout = asyncio.StreamReader()

    class ExitedParentWithInheritedPipe:
        pid = 12345
        returncode = 0
        stderr = None

        def __init__(self):
            self.stdout = stdout

        async def wait(self):
            return 0

    cancel = asyncio.Event()
    task = asyncio.create_task(
        subscription._wait_captured_login(
            ExitedParentWithInheritedPipe(),
            cancel,
            timeout=5,
            on_authorization=lambda url, code: None,
        )
    )
    await asyncio.sleep(0.05)
    cancel.set()
    with pytest.raises(subscription.SubscriptionCancelled):
        await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_operation_cancel_runs_cleanup_and_cancels_inflight_work() -> None:
    from replaylab_agent.connectors.subscription import (
        SubscriptionCancelled,
        _await_or_cancel,
    )

    cancel = asyncio.Event()
    cleanup_called = False
    operation_closed = False

    async def operation():
        nonlocal operation_closed
        try:
            await asyncio.Event().wait()
        finally:
            operation_closed = True

    async def cleanup():
        nonlocal cleanup_called
        cleanup_called = True

    task = asyncio.create_task(_await_or_cancel(operation(), cancel, on_cancel=cleanup))
    await asyncio.sleep(0)
    cancel.set()
    with pytest.raises(SubscriptionCancelled):
        await task
    assert cleanup_called is True
    assert operation_closed is True


@pytest.mark.asyncio
async def test_outer_task_cancellation_also_cleans_subscription_operation() -> None:
    from replaylab_agent.connectors.subscription import _await_or_cancel
    started = asyncio.Event()
    closed = asyncio.Event()
    cleanup = []

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def cancel_vendor():
        cleanup.append(True)

    task = asyncio.create_task(_await_or_cancel(operation(), asyncio.Event(), on_cancel=cancel_vendor))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert cleanup == [True]


@pytest.mark.asyncio
async def test_failed_vendor_cancel_still_stops_local_subscription_operation() -> None:
    from replaylab_agent.connectors.subscription import _await_or_cancel
    started = asyncio.Event()
    closed = asyncio.Event()
    cancel = asyncio.Event()

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def cancel_vendor():
        raise RuntimeError("vendor cancel failed")

    task = asyncio.create_task(_await_or_cancel(operation(), cancel, on_cancel=cancel_vendor))
    await started.wait()
    cancel.set()
    with pytest.raises(RuntimeError, match="vendor cancel failed"):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_acp_connection_test_performs_handshake_without_prompt(tmp_path) -> None:
    pytest.importorskip("acp")
    from replaylab_agent.connectors.acp import ACPConnector

    script = (
        __import__("pathlib").Path(__file__).parent
        / "fixtures"
        / "fake_acp_agent.py"
    )
    connector = ACPConnector(
        [sys.executable, str(script), "no-mcp"], cwd=tmp_path, timeout=5, env={}
    )
    details = await connector.test_connection()
    assert details["sessionId"] == "session-test"
    assert details["agentInfo"]["name"] == "fake-acp"


@pytest.mark.asyncio
async def test_subscription_backend_explains_missing_standard_model_list(
    tmp_path, monkeypatch
) -> None:
    import replaylab_agent.connectors.subscription as subscription

    class FakeACP:
        async def test_connection(self):
            return {"sessionId": "session-1", "models": []}

    monkeypatch.setattr(
        subscription,
        "resolve_agent_executable",
        lambda profile, *, environ=None: "C:/trusted/gemini.cmd",
    )
    backend = subscription.SubscriptionBackend(
        Profile(
            "gemini-main",
            "gemini-acp",
            options={"allow_unsandboxed_agent": True},
        ),
        timeout=5,
        emit=lambda event: None,
        environ={"LOCALAPPDATA": str(tmp_path), "PATH": ""},
        acp_factory=lambda profile, timeout, environment: FakeACP(),
    )

    result = await backend.test(asyncio.Event())

    assert result.authenticated is True
    assert result.models == ()
    assert "did not expose a standard model list" in result.message
    assert result.message_id == "subscription.acp-test-no-models"
