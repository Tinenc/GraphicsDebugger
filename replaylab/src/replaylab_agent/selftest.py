"""Deterministic, credential-free protocol compatibility self-test.

The harness exercises ReplayLab's production HTTP, Codex app-server and ACP
connectors against loopback/fake peers.  It never loads profiles, reads an OS
credential vault, consults vendor account caches, or contacts the public
internet.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Literal
from urllib.parse import urlsplit

from .connectors.codex_app_server import CodexAppServerConnector
from .errors import AuthenticationError, ConfigurationError, ConnectorError
from .probe import discover_models
from .profiles import Profile
from .runtime_dependencies import verify_deepagents_version


SelfTestStatus = Literal["ok", "warning", "error"]
_SELF_TEST_TOKEN = "replaylab-offline-self-test-token"
_ITEM_ORDER = (
    "api.openai-compatible",
    "api.anthropic-compatible",
    "api.google-genai",
    "subscription.codex-app-server",
    "subscription.acp",
)


@dataclass(frozen=True, slots=True)
class SelfTestItem:
    name: str
    status: SelfTestStatus
    detail: str

    def to_wire(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


class _OptionalRuntimeMissing(Exception):
    pass


@dataclass(slots=True)
class _RecordedRequest:
    path: str
    query: str
    authorization: str
    anthropic_key: str
    anthropic_version: str
    google_key: str


@dataclass(slots=True)
class _LoopbackState:
    role: str
    redirect_target: str = ""
    requests: list[_RecordedRequest] | None = None

    def __post_init__(self) -> None:
        self.requests = []


class _LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: _LoopbackState) -> None:
        self.state = state
        super().__init__(("127.0.0.1", 0), _LoopbackHandler)


class _LoopbackHandler(BaseHTTPRequestHandler):
    server: _LoopbackHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        record = _RecordedRequest(
            path=parsed.path,
            query=parsed.query,
            authorization=self.headers.get("Authorization", ""),
            anthropic_key=self.headers.get("x-api-key", ""),
            anthropic_version=self.headers.get("anthropic-version", ""),
            google_key=self.headers.get("x-goog-api-key", ""),
        )
        assert self.server.state.requests is not None
        self.server.state.requests.append(record)

        if self.server.state.role == "sink":
            self._send_json(200, {"unexpected": True})
            return
        if parsed.query:
            self._send_json(400, {"error": "query-not-allowed"})
            return
        if parsed.path in {"/authfail401/models", "/authfail403/models"}:
            status = int(parsed.path.removeprefix("/authfail")[:3])
            self._send_json(status, {"error": "expected-self-test-auth-failure"})
            return
        if parsed.path == "/redirect/models":
            self.send_response(302)
            self.send_header("location", self.server.state.redirect_target)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        if parsed.path == "/redirect/v1/models":
            self._send_json(404, {"error": "expected-self-test-fallback-end"})
            return
        if parsed.path == "/models":
            self._send_json(404, {"error": "v1-required"})
            return
        if parsed.path == "/v1/models":
            if record.authorization != f"Bearer {_SELF_TEST_TOKEN}":
                self._send_json(403, {"error": "bad-authorization"})
                return
            self._send_json(200, {"data": [{"id": "openai-self-test"}]})
            return
        if parsed.path == "/anthropic/v1/models":
            if (
                record.anthropic_key != _SELF_TEST_TOKEN
                or record.anthropic_version != "2023-06-01"
            ):
                self._send_json(403, {"error": "bad-anthropic-headers"})
                return
            self._send_json(200, {"data": [{"id": "claude-self-test"}]})
            return
        if parsed.path == "/google/v1beta/models":
            if record.google_key != _SELF_TEST_TOKEN:
                self._send_json(403, {"error": "bad-google-header"})
                return
            self._send_json(200, {"models": [{"name": "models/gemini-self-test"}]})
            return
        self._send_json(404, {"error": "unknown-self-test-path"})


@contextmanager
def _loopback_server(
    role: str = "api", *, redirect_target: str = ""
) -> Iterator[tuple[str, _LoopbackState]]:
    state = _LoopbackState(role=role, redirect_target=redirect_target)
    server = _LoopbackHTTPServer(state)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        name=f"replaylab-self-test-{role}",
        daemon=True,
    )
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@contextmanager
def _hostile_proxy_environment(proxy_url: str) -> Iterator[None]:
    """Force HTTP clients to choose between the loopback origin and a proxy sink."""

    overrides = {
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "NO_PROXY": "",
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "all_proxy": proxy_url,
        "no_proxy": "",
    }
    previous = {name: os.environ.get(name) for name in overrides}
    try:
        os.environ.update(overrides)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _assert(condition: bool, detail: str) -> None:
    if not condition:
        raise ConnectorError(detail)


def _safe_failure(exc: Exception) -> str:
    detail = str(exc).replace(_SELF_TEST_TOKEN, "<self-test-token>")
    detail = " ".join(detail.split())
    return f"{type(exc).__name__}: {detail[:500]}"


async def _run_item(
    name: str, operation: Callable[[], Awaitable[str]]
) -> SelfTestItem:
    try:
        return SelfTestItem(name, "ok", await operation())
    except _OptionalRuntimeMissing as exc:
        return SelfTestItem(name, "warning", str(exc))
    except Exception as exc:
        return SelfTestItem(name, "error", _safe_failure(exc))


async def _check_openai(timeout: float) -> str:
    with _loopback_server("sink") as (sink_origin, sink), _loopback_server(
        redirect_target=sink_origin + "/credential-must-not-arrive"
    ) as (origin, api), _hostile_proxy_environment(sink_origin):
        matrix = (
            "",
            "/v1",
            "/models",
            "/v1/models",
            "/responses",
            "/v1/responses",
            "/chat/completions",
            "/v1/chat/completions",
        )
        for index, suffix in enumerate(matrix):
            profile = Profile(
                f"self-openai-{index}",
                "custom-openai",
                "openai-self-test",
                base_url=origin + suffix,
            )
            endpoint, models, _ = await discover_models(
                profile,
                timeout=timeout,
                api_key=f"Bearer {_SELF_TEST_TOKEN}",
            )
            _assert(endpoint == origin + "/v1", f"unexpected normalized endpoint for {suffix!r}")
            _assert(models == ["openai-self-test"], "OpenAI model list parsing failed")

        assert api.requests is not None
        before_query = len(api.requests)
        try:
            await discover_models(
                Profile(
                    "self-openai-query",
                    "custom-openai",
                    "openai-self-test",
                    base_url=origin + "/v1?api_key=must-not-be-used",
                ),
                timeout=timeout,
                api_key=_SELF_TEST_TOKEN,
            )
        except ConfigurationError:
            pass
        else:
            raise ConnectorError("credential-bearing endpoint query was accepted")
        _assert(len(api.requests) == before_query, "query URL reached the network")

        for status in (401, 403):
            auth_start = len(api.requests)
            try:
                await discover_models(
                    Profile(
                        f"self-openai-auth-stop-{status}",
                        "custom-openai",
                        "openai-self-test",
                        base_url=origin + f"/authfail{status}",
                    ),
                    timeout=timeout,
                    api_key=_SELF_TEST_TOKEN,
                )
            except AuthenticationError:
                pass
            else:
                raise ConnectorError(f"HTTP {status} did not stop model discovery")
            auth_paths = [item.path for item in api.requests[auth_start:]]
            _assert(
                auth_paths == [f"/authfail{status}/models"],
                f"credential was retried after HTTP {status}",
            )

        try:
            await discover_models(
                Profile(
                    "self-openai-redirect",
                    "custom-openai",
                    "openai-self-test",
                    base_url=origin + "/redirect",
                ),
                timeout=timeout,
                api_key=_SELF_TEST_TOKEN,
            )
        except ConnectorError:
            pass
        else:
            raise ConnectorError("redirecting model endpoint unexpectedly succeeded")
        assert sink.requests is not None
        _assert(not sink.requests, "credential-bearing redirect was followed across origin")
        _assert(
            all(not item.query for item in api.requests),
            "self-test emitted a credential-bearing query",
        )
        _assert(
            all(
                item.authorization in {"", f"Bearer {_SELF_TEST_TOKEN}"}
                for item in api.requests
            ),
            "Bearer credential normalization failed",
        )
    return (
        "loopback model discovery passed for service root, /v1, /models and operation URLs; "
        "environment-proxy bypass, Bearer normalization, query rejection, redirect refusal and "
        "401/403 stop verified"
    )


async def _check_anthropic(timeout: float) -> str:
    with _loopback_server("sink") as (sink_origin, sink), _loopback_server() as (
        origin,
        api,
    ), _hostile_proxy_environment(sink_origin):
        endpoint, models, attempts = await discover_models(
            Profile(
                "self-anthropic",
                "anthropic",
                "claude-self-test",
                base_url=origin + "/anthropic",
            ),
            timeout=timeout,
            api_key=f"Bearer {_SELF_TEST_TOKEN}",
        )
        _assert(endpoint == origin + "/anthropic", "Anthropic endpoint changed unexpectedly")
        _assert(models == ["claude-self-test"], "Anthropic model list parsing failed")
        _assert([item["status"] for item in attempts] == [200], "Anthropic request failed")
        assert api.requests is not None
        _assert(len(api.requests) == 1, "Anthropic compatibility check made extra requests")
        request = api.requests[0]
        _assert(not request.query, "Anthropic credential was placed in the URL query")
        _assert(request.anthropic_key == _SELF_TEST_TOKEN, "Anthropic API key header failed")
        assert sink.requests is not None
        _assert(not sink.requests, "Anthropic credential reached an environment proxy")
    return (
        "loopback proxy bypass, x-api-key/version headers and Anthropic model-list parsing passed"
    )


async def _check_google(timeout: float) -> str:
    with _loopback_server("sink") as (sink_origin, sink), _loopback_server() as (
        origin,
        api,
    ), _hostile_proxy_environment(sink_origin):
        endpoint, models, attempts = await discover_models(
            Profile(
                "self-google",
                "gemini",
                "gemini-self-test",
                base_url=origin + "/google",
            ),
            timeout=timeout,
            api_key=f"Bearer {_SELF_TEST_TOKEN}",
        )
        _assert(endpoint == origin + "/google", "Google endpoint changed unexpectedly")
        _assert(models == ["gemini-self-test"], "Google model list parsing failed")
        _assert([item["status"] for item in attempts] == [200], "Google request failed")
        assert api.requests is not None
        _assert(len(api.requests) == 1, "Google compatibility check made extra requests")
        request = api.requests[0]
        _assert(not request.query, "Google credential was placed in the URL query")
        _assert(request.google_key == _SELF_TEST_TOKEN, "Google API key header failed")
        assert sink.requests is not None
        _assert(not sink.requests, "Google credential reached an environment proxy")
    return "loopback proxy bypass, x-goog-api-key header and Google model-list parsing passed"


def _child_environment() -> dict[str, str]:
    # Do not inherit HOME/CODEX_HOME, API keys, vendor tokens or arbitrary Python
    # startup hooks.  The one fixed PYTHONPATH below points only at this installed
    # package/source root, so the fake peer also works in a PYTHONPATH-based test
    # checkout after its cwd has intentionally moved to an empty temporary folder.
    allowed = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result["PYTHONIOENCODING"] = "utf-8"
    result["PYTHONUTF8"] = "1"
    result["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return result


async def _check_codex(timeout: float) -> str:
    with tempfile.TemporaryDirectory(prefix="replaylab-self-test-codex-") as temporary:
        connector = CodexAppServerConnector(
            [sys.executable, "-m", "replaylab_agent.selftest", "--fake-codex"],
            cwd=Path(temporary),
            timeout=timeout,
            env=_child_environment(),
        )
        async with connector:
            account = await connector.account_read()
            _assert(
                account.get("account") == {"type": "self-test", "planType": "offline"},
                "Codex account/read parsing failed",
            )
            browser = await connector.login_start(flow="browser")
            _assert(
                browser.get("authUrl") == "https://self-test.invalid/codex/browser",
                "Codex browser auth URL parsing failed",
            )
            completed = await connector.wait_login(str(browser.get("loginId")), timeout=timeout)
            _assert(completed.get("success") is True, "Codex browser login event parsing failed")
            device = await connector.login_start(flow="device")
            _assert(
                device.get("verificationUrl") == "https://self-test.invalid/codex/device"
                and device.get("userCode") == "SELF-TEST",
                "Codex device login URL/code parsing failed",
            )
            completed = await connector.wait_login(str(device.get("loginId")), timeout=timeout)
            _assert(completed.get("success") is True, "Codex device login event parsing failed")
            models = await connector.list_models()
            _assert(
                [item.get("id") for item in models] == ["codex-self-test"],
                "Codex model/list parsing failed",
            )
    return (
        "fake app-server initialize, account/read, browser/device login URLs and "
        "model/list passed"
    )


async def _check_acp(timeout: float) -> str:
    if importlib.util.find_spec("acp") is None:
        raise _OptionalRuntimeMissing(
            "optional agent-client-protocol runtime is not installed; ACP handshake skipped"
        )
    from .connectors.acp import ACPConnector
    from .acp_session import ACPMainAgentSession

    with tempfile.TemporaryDirectory(prefix="replaylab-self-test-acp-") as temporary:
        command = [sys.executable, "-m", "replaylab_agent.selftest", "--fake-acp"]
        connector = ACPConnector(
            command,
            cwd=Path(temporary),
            timeout=timeout,
            env=_child_environment(),
            provider_id="codebuddy-acp",
        )
        details = await connector.test_connection()
        model = await connector.test_model()
        _assert(model.get("ok") is True, "ACP model response failed")
        async with ACPMainAgentSession(command=command, cwd=Path(temporary),
                env=_child_environment(), allowed_tools=frozenset(),
                provider_id="codebuddy-acp", timeout=timeout) as session:
            for prompt in ("first offline turn", "second offline turn"):
                chunks: list[str] = []
                response = await session.prompt(prompt, on_delta=chunks.append)
                _assert(response.text == "ACP: " + prompt, "ACP chat response differs")
                _assert("".join(chunks) == response.text, "ACP chat stream differs")
                _assert(response.stop_reason == "end_turn", "ACP chat turn did not complete")
                _assert(response.tool_calls == 0, "ACP notification became a tool call")
    _assert(details.get("sessionId") == "self-test-session", "ACP session/new parsing failed")
    _assert(
        details.get("agentInfo") == {"name": "replaylab-self-test", "version": "1"},
        "ACP initialize agentInfo parsing failed",
    )
    return ("fake ACP initialize and no-prompt session/new handshake, model response, "
            "two-turn streaming chat and extension compatibility passed")


async def run_protocol_self_test(*, timeout: float = 10.0) -> list[SelfTestItem]:
    """Run five fixed protocol-family checks without real profiles or accounts."""

    if timeout <= 0 or timeout > 60:
        raise ConfigurationError("self-test timeout must be between 0 and 60 seconds")
    operations: tuple[tuple[str, Callable[[], Awaitable[str]]], ...] = (
        (_ITEM_ORDER[0], lambda: _check_openai(timeout)),
        (_ITEM_ORDER[1], lambda: _check_anthropic(timeout)),
        (_ITEM_ORDER[2], lambda: _check_google(timeout)),
        (_ITEM_ORDER[3], lambda: _check_codex(timeout)),
        (_ITEM_ORDER[4], lambda: _check_acp(timeout)),
    )
    # Keep ordering and process ownership deterministic. These checks are short;
    # parallelism would only make diagnostics and loopback teardown less clear.
    return [await _run_item(name, operation) for name, operation in operations]


def _send_jsonl(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _fake_codex_main() -> int:
    initialized = False
    login_sequence = 0
    for raw in sys.stdin:
        message = json.loads(raw)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            initialized = True
            _send_jsonl({"id": request_id, "result": {"userAgent": "replaylab-self-test"}})
            continue
        if method == "initialized":
            continue
        if not initialized:
            _send_jsonl({"id": request_id, "error": {"message": "initialize required"}})
            continue
        if method == "account/read":
            _send_jsonl(
                {
                    "id": request_id,
                    "result": {
                        "account": {"type": "self-test", "planType": "offline"},
                        "requiresOpenaiAuth": True,
                    },
                }
            )
        elif method == "account/login/start":
            login_sequence += 1
            login_id = f"self-test-login-{login_sequence}"
            flow = message.get("params", {}).get("type")
            if flow == "chatgpt" and message.get("params") != {"type": "chatgpt"}:
                _send_jsonl(
                    {
                        "id": request_id,
                        "error": {"message": "browser login must use the local success page"},
                    }
                )
                continue
            result: dict[str, Any] = {"type": flow, "loginId": login_id}
            if flow == "chatgpt":
                result["authUrl"] = "https://self-test.invalid/codex/browser"
            elif flow == "chatgptDeviceCode":
                result["verificationUrl"] = "https://self-test.invalid/codex/device"
                result["userCode"] = "SELF-TEST"
            else:
                _send_jsonl({"id": request_id, "error": {"message": "invalid login flow"}})
                continue
            _send_jsonl({"id": request_id, "result": result})
            _send_jsonl(
                {
                    "method": "account/login/completed",
                    "params": {"loginId": login_id, "success": True, "error": None},
                }
            )
            _send_jsonl(
                {
                    "method": "account/updated",
                    "params": {"authMode": "chatgpt", "planType": "offline"},
                }
            )
        elif method == "model/list":
            _send_jsonl(
                {
                    "id": request_id,
                    "result": {
                        "data": [
                            {
                                "id": "codex-self-test",
                                "model": "codex-self-test",
                                "displayName": "Codex Self Test",
                                "hidden": False,
                                "isDefault": True,
                            }
                        ],
                        "nextCursor": None,
                    },
                }
            )
        else:
            _send_jsonl({"id": request_id, "error": {"message": "unsupported self-test method"}})
    return 0


def _fake_acp_main() -> int:
    try:
        from acp import PROTOCOL_VERSION, PromptResponse, run_agent
        from acp import schema
        from acp.exceptions import RequestError
    except ImportError:
        return 3

    class _FakeACPAgent:
        def on_connect(self, connection: Any) -> None:
            self.connection = connection

        async def initialize(self, protocol_version: int, **kwargs: Any) -> Any:
            del kwargs
            return schema.InitializeResponse(
                protocolVersion=min(protocol_version, PROTOCOL_VERSION),
                agentInfo=schema.Implementation(name="replaylab-self-test", version="1"),
            )

        async def new_session(self, cwd: str, mcp_servers: Any = None, **kwargs: Any) -> Any:
            del kwargs
            if not cwd or mcp_servers:
                raise RuntimeError("self-test ACP expected an empty MCP list")
            await self.connection.ext_notification("codebuddy.ai/system_init", {})
            return schema.NewSessionResponse(sessionId="self-test-session")

        async def prompt(self, session_id: str, prompt: Any, **kwargs: Any) -> Any:
            del kwargs
            for method in ("codebuddy.ai/command", "com.example/future_notification"):
                await self.connection.ext_notification(method, {
                    "sessionId": session_id, "command": "must-not-execute",
                    "text": "must-not-be-answer-text", "status": "completed",
                })
            try:
                await self.connection.ext_method("com.example/unsupported", {})
            except RequestError as error:
                _assert(error.code == -32601, "ACP extension rejection is not Method not found")
            else:
                raise RuntimeError("ACP unsupported extension was accepted")
            for text in ("ACP: ", prompt[0].text):
                await self.connection.session_update(session_id, schema.AgentMessageChunk(
                    content=schema.TextContentBlock(type="text", text=text),
                    sessionUpdate="agent_message_chunk"))
            return PromptResponse(stopReason="end_turn")

        async def cancel(self, session_id: str, **kwargs: Any) -> None:
            del session_id, kwargs

    asyncio.run(run_agent(_FakeACPAgent()))
    return 0


def _external_mcp_import_smoke() -> int:
    """Import the exact client surface required by user-configured MCPs.

    This deliberately lives behind the frozen internal self-test dispatcher so
    runtime packaging can catch a missing PyInstaller dependency before the
    artifacts are published.  Distinct exit codes keep the check diagnosable
    without printing exception text that could contain configuration secrets.
    """

    checks = (
        lambda: __import__("anyio"),
        lambda: __import__("httpx"),
        lambda: __import__("mcp.types", fromlist=("JSONRPCMessage",)),
        lambda: __import__("anyio.streams.text", fromlist=("TextReceiveStream",)),
        lambda: __import__(
            "mcp", fromlist=("ClientSession", "StdioServerParameters")
        ),
        lambda: __import__(
            "mcp.client.stdio",
            fromlist=(
                "PROCESS_TERMINATION_TIMEOUT",
                "_create_platform_compatible_process",
                "_get_executable_command",
                "_terminate_process_tree",
                "get_default_environment",
            ),
        ),
        lambda: __import__("mcp.shared.message", fromlist=("SessionMessage",)),
        lambda: __import__("deepagents", fromlist=("create_deep_agent",)),
        lambda: __import__("langchain_core", fromlist=("tools",)),
    )
    for index, check in enumerate(checks, start=1):
        try:
            check()
        except ImportError as exc:
            missing = str(getattr(exc, "name", "") or "unknown-module")
            sys.stderr.write(
                f"external-mcp-import-smoke:{index}:{missing}\n"
            )
            sys.stderr.flush()
            return 30 + index
    try:
        verify_deepagents_version()
    except ConfigurationError as exc:
        sys.stderr.write(f"external-mcp-import-smoke:deepagents-version:{exc}\n")
        sys.stderr.flush()
        return 40
    return 0


def _child_entry(argv: list[str]) -> int:
    if argv == ["--acp-chat-smoke"]:
        item = asyncio.run(_run_item("subscription.acp", lambda: _check_acp(20.0)))
        print(json.dumps(item.to_wire(), ensure_ascii=False))
        return 0 if item.status == "ok" else 1
    if argv == ["--fake-codex"]:
        return _fake_codex_main()
    if argv == ["--fake-acp"]:
        return _fake_acp_main()
    if argv == ["--external-mcp-import-smoke"]:
        return _external_mcp_import_smoke()
    raise SystemExit("selftest.py is an internal packaging and protocol-test helper")


if __name__ == "__main__":
    raise SystemExit(_child_entry(sys.argv[1:]))


__all__ = ["SelfTestItem", "run_protocol_self_test"]
