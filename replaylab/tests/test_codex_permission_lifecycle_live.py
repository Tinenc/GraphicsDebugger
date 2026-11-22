"""Real installed app-server, isolated auth, loopback-only synthetic responses."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from replaylab_agent.agent_context import ProviderContext, harden_codex_argv
from replaylab_agent.agent_report import TaskReportStore
from replaylab_agent.connectors.codex_app_server import CodexAppServerConnector
from replaylab_agent.native_mcp import build_native_mcp_route, native_codex_argv
from replaylab_agent.profiles import Profile


@pytest.mark.asyncio
@pytest.mark.parametrize("include_default,with_bread,writable",
                         [(False, False, True), (True, False, True), (True, True, True), (True, False, False)],
                         ids=["missing-thread-default", "no-mcp", "bread-mcp", "read-only"])
@pytest.mark.skipif(os.environ.get("REPLAYLAB_TEST_REAL_CODEX") != "1" or not shutil.which("codex"),
                    reason="opt-in installed Codex lifecycle check; no remote model calls")
async def test_real_codex_named_permissions_survive_mcp_reload_and_followup(tmp_path: Path, include_default: bool, with_bread: bool, writable: bool):
    requests = []
    seen_tools, tool_outputs, tool_result_text = [], [], []

    class ResponsesHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            # Retain no request body or headers. Only synthetic keys can be used
            # because both CODEX_HOME and provider env_key are isolated below.
            requests.append((self.path, payload.get("model")))
            index = len(requests)
            text, mid, rid = "local handshake complete", f"msg_{index}", f"resp_{index}"
            item = {"id": mid, "type": "message", "role": "assistant", "phase": "final_answer", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
            for tool in payload.get("tools", []):
                if isinstance(tool, dict):
                    seen_tools.append(str(tool.get("name", "")))
                    if tool.get("type") == "namespace":
                        seen_tools.extend(str(tool.get("name", "")) + "." + str(child.get("name", ""))
                                          for child in tool.get("tools", []) if isinstance(child, dict))
            for entry in payload.get("input", []):
                if isinstance(entry, dict) and entry.get("type") == "function_call_output":
                    tool_outputs.append(entry.get("call_id"))
                    tool_result_text.append(str(entry.get("output", ""))[:1200])
            if with_bread and index == 1:
                # Exercise the real task proxy and durable receipt handler,
                # without any capture/Host instance or user project access.
                name = next(name for name in seen_tools if name.endswith("record_task_review"))
                namespace, separator, function_name = name.rpartition(".")
                item = {"id": "fc_review", "type": "function_call", "name": name,
                    "call_id": "call_bread_review", "arguments": json.dumps({
                        "verdict": "complete", "criteria": [{"requirement": "Return the synthetic explanation",
                        "status": "satisfied", "evidence": "The prepared answer is local handshake complete."}],
                        "findings": [], "next_actions": [], "blocker": ""}), "status": "completed"}
                if separator:
                    item.update(name=function_name, namespace=namespace)
            events = [
                ("response.created", {"response": {"id": rid, "status": "in_progress"}}),
                ("response.output_item.added", {"output_index": 0,
                    "item": {**item, "status": "in_progress", "content": []}}),
                ("response.output_text.delta", {"item_id": mid, "output_index": 0,
                    "content_index": 0, "delta": text}),
                ("response.output_item.done", {"output_index": 0, "item": item}),
                ("response.completed", {"response": {"id": rid, "status": "completed",
                    "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}),
            ]
            if item["type"] == "function_call":
                events = [events[0], ("response.output_item.added", {"output_index": 0,
                    "item": {**item, "status": "in_progress"}}), *events[-2:]]
            body = "".join("event: " + kind + "\ndata: " + json.dumps({"type": kind, **value}) + "\n\n"
                           for kind, value in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    owned, profile = tmp_path / "owned", tmp_path / "codex-home"
    owned.mkdir()
    profile.mkdir()
    store = None
    route = None
    if with_bread:
        store = TaskReportStore("real-codex-mcp", tmp_path / "events")
        store.begin_task_review("a" * 64)
        context = ProviderContext(profile=Profile(id="native", preset="codex"), task_id="real-codex-mcp",
                                  model="", instance_id="", context={}, intent=None)
        route = build_native_mcp_route(context, workspace=owned, event_root=tmp_path / "events", allowed_effects=())
        assert route is not None and "--host" not in route.args
    (profile / "auth.json").write_text(json.dumps({
        "auth_mode": "apikey", "OPENAI_API_KEY": "sk-bread-local-test-not-a-real-key",
    }), encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    argv = (native_codex_argv([shutil.which("codex"), "app-server"], route) if route is not None
            else harden_codex_argv([shutil.which("codex"), "app-server"]))
    settings = [
        'cli_auth_credentials_store="file"',
        'model_provider="bread_local_test"',
        'model_providers.bread_local_test.name="Bread local test"',
        f'model_providers.bread_local_test.base_url="http://127.0.0.1:{server.server_port}/v1"',
        'model_providers.bread_local_test.wire_api="responses"',
        'model_providers.bread_local_test.env_key="BREAD_LOCAL_TEST_KEY"',
        'model_providers.bread_local_test.requires_openai_auth=true',
        'model_providers.bread_local_test.supports_websockets=false',
        'model_providers.bread_local_test.request_max_retries=0',
        'model_providers.bread_local_test.stream_max_retries=0',
    ]
    index = argv.index("app-server")
    argv[index:index] = [value for setting in settings for value in ("-c", setting)]
    connector = CodexAppServerConnector(argv, cwd=owned, workspace_write=writable, timeout=20,
        expected_mcp_servers=frozenset({"Bread"}) if with_bread else frozenset(),
        env={**os.environ, "CODEX_HOME": str(profile), "BREAD_LOCAL_TEST_KEY": "synthetic-test-key",
             "OPENAI_API_KEY": "sk-bread-local-test-not-a-real-key", "CODEX_API_KEY": "sk-bread-local-test-not-a-real-key"})
    if not include_default:
        profile_config = connector._owned_workspace_profile_config
        connector._owned_workspace_profile_config = lambda: {
            key: value for key, value in profile_config().items() if key != "default_permissions"
        }
    calls, scopes, timeline = [], [], []
    request = connector._transport.request

    async def observed_request(method, params, **kwargs):
        result = await request(method, params, **kwargs)
        calls.append(method)
        if method == "thread/start":
            scopes.append(result)
        return result

    connector._transport.request = observed_request
    try:
        # The process layer also pins the default/profile table. Config reloads
        # and older turn/start implementations must retain that verified scope,
        # even if the thread request supplies no default of its own.
        # Use a bundled public tool-capable descriptor with this synthetic
        # API-key provider; no subscription model catalog is loaded by the test.
        first = await connector.run("reply to synthetic probe", model="gpt-5.4", keep_thread=True,
                                    on_timeline=timeline.append)
        await connector._verify_tool_isolation(first.thread_id)
        second = await connector.run("continue synthetic probe", model="gpt-5.4", keep_thread=True,
                                     on_timeline=timeline.append)
        await connector._verify_tool_isolation(second.thread_id)
        assert first.text == second.text == "local handshake complete", (first, second, requests, calls)
        assert first.thread_id == second.thread_id
        assert connector.workspace_write is writable and not connector.workspace_capability_note
        assert calls.count("thread/start") == 1 and calls.count("turn/start") == 2
        assert calls.count("mcpServerStatus/list") >= 3
        assert len(requests) == (3 if with_bread else 2) and all(path == "/v1/responses" for path, _ in requests)
        connector._verify_thread_read_scope(scopes[0], [str(owned)])
        if with_bread:
            assert "call_bread_review" in tool_outputs, (seen_tools, tool_outputs)
            completed = [event for event in timeline if event.get("activityKind") == "mcpToolCall"
                         and event.get("lifecycle") == "completed"
                         and event.get("activityTarget", "").endswith("record_task_review")]
            assert len(completed) == 1 and completed[0]["status"] == "completed"
            assert store is not None
            state = store.task_review_state()
            assert state["fresh"] is True and state["review"]["verdict"] == "complete", (seen_tools, tool_result_text)
    finally:
        await connector.close()
        server.shutdown()
        server.server_close()
        server_thread.join()
