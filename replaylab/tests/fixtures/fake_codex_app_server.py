from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# The real app-server wire is UTF-8 even on a non-UTF-8 Windows desktop.
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


mode = sys.argv[1] if len(sys.argv) > 1 else "success"
login_completed = False
post_login_account_reads = 0
mcp_status_reads = 0
thread_config = {}
turn_count = 0
account_reads = 0
trace_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None


def send(value: dict) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def mcp_status(name: str, *, runtime_status: str, with_tools: bool) -> dict:
    tools = {}
    if with_tools:
        tools["inspect_event"] = {
            "name": "inspect_event",
            "description": "Inspect one event",
            "inputSchema": {"type": "object"},
        }
    return {
        "authStatus": "unsupported",
        "name": name,
        "pluginId": None,
        "resourceTemplates": [],
        "resources": [],
        "runtimeStatus": runtime_status,
        "serverInfo": {
            "name": name,
            "version": "test",
            "description": None,
            "icons": None,
            "title": None,
            "websiteUrl": None,
        }
        if runtime_status == "connected"
        else None,
        "tools": tools,
    }


for line in sys.stdin:
    message = json.loads(line)
    if trace_path is not None:
        with trace_path.open("a", encoding="utf-8") as trace:
            trace.write(json.dumps(message) + "\n")
    method = message.get("method")
    request_id = message.get("id")
    if method is None and mode == "approval" and request_id == 900:
        assert message["result"] == {"decision": "decline"}
        send(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-test",
                    "turnId": "turn-test",
                    "delta": "continued with admitted tools",
                },
            }
        )
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-test",
                    "turnId": "turn-test",
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "continued with admitted tools",
                    },
                },
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-test",
                    "turn": {
                        "id": "turn-test",
                        "status": "completed",
                        "error": None,
                    },
                },
            }
        )
    elif method == "fixture/echo":
        send({"id": request_id, "result": message["params"]})
    elif method == "initialize":
        if mode == "exit-initialize":
            raise SystemExit(17)
        # ReplayLab's current permission contract always uses
        # runtimeWorkspaceRoots, which is experimental-gated by app-server.
        assert message["params"]["capabilities"]["experimentalApi"] is True
        if mode == "legacy-initialize-protocol":
            send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32600,
                        "message": "Invalid request: unknown field capabilities.experimentalApi",
                    },
                }
            )
            continue
        send({"id": request_id, "result": {"userAgent": "fake"}})
    elif method == "account/read":
        account_reads += 1
        if mode.startswith('account-read-'):
            fail = mode != 'account-read-routing-once' or account_reads == 1
            if fail:
                code, text = {
                    'account-read-auth': (-32001, 'Unauthorized: sign in again'),
                    'account-read-invalid': (-32602, 'invalid account/read params'),
                    'account-read-internal': (-32603, 'unrelated internal failure'),
                }.get(mode, (-32603, 'workspace routing discovery timed out'))
                send({'id': request_id, 'error': {'code': code, 'message': text}})
                continue
        if mode in {"login-race", "login-update-first"}:
            if login_completed:
                post_login_account_reads += 1
            # Reproduce the real first-login race: login/completed has fired,
            # but one immediately following account/read can still be signed
            # out while account/updated is in the notification queue.
            authenticated = login_completed and post_login_account_reads >= 2
        else:
            authenticated = not mode.startswith("signed-out")
        account = (
            {
                "type": "chatgpt",
                "email": "test@example.invalid",
                "planType": "plus",
            }
            if authenticated
            else None
        )
        result = {"account": account}
        if mode != "signed-out-no-flag":
            result["requiresOpenaiAuth"] = mode != "signed-out-false"
        send({"id": request_id, "result": result})
    elif method == "model/list":
        send(
            {
                "id": request_id,
                "result": {
                    "data": [
                        {
                            "id": "codex-test",
                            "model": "codex-test",
                            "displayName": "Codex Test",
                            "hidden": False,
                            "isDefault": True,
                        }
                    ],
                    "nextCursor": None,
                },
            }
        )
    elif method == "account/login/start":
        flow = message["params"]["type"]
        if flow == "chatgpt":
            # ReplayLab must use app-server's local callback success page, not
            # a hosted branded redirect that can open a new Codex/ChatGPT chat.
            assert message["params"] == {"type": "chatgpt"}
        result = {"type": flow, "loginId": "login-test"}
        if flow == "chatgpt":
            result["authUrl"] = "https://chatgpt.com/fake-login"
        else:
            result["verificationUrl"] = "https://auth.openai.com/codex/device"
            result["userCode"] = "TEST-CODE"
        send({"id": request_id, "result": result})
        login_completed = True
        completion_notification = {
            "method": "account/login/completed",
            "params": {
                "loginId": "login-test",
                "success": True,
                "error": None,
            },
        }
        account_notification = {
            "method": "account/updated",
            "params": {"authMode": "chatgpt", "planType": "plus"},
        }
        if mode == "login-update-first":
            send(account_notification)
            send(completion_notification)
        else:
            send(completion_notification)
            send(account_notification)
    elif method == "account/login/cancel":
        send({"id": request_id, "result": {}})
        send(
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": message["params"]["loginId"],
                    "success": False,
                    "error": "cancelled",
                },
            }
        )
    elif method == "account/logout":
        send({"id": request_id, "result": {}})
    elif method == "permissionProfile/list":
        assert message["params"]["cwd"]
        assert message["params"]["limit"] == 100
        if mode == "legacy-permission-protocol":
            send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "Method not found: permissionProfile/list",
                    },
                }
            )
            continue
        allowed = mode != "read-only-profile-unavailable"
        send(
            {
                "id": request_id,
                "result": {
                    "data": [
                        {"id": ":read-only", "allowed": allowed},
                        {"id": ":workspace", "allowed": mode != "workspace-profile-denied"},
                    ],
                    "nextCursor": None,
                },
            }
        )
    elif method == "thread/start":
        assert not mode.startswith("signed-out"), "signed-out run must fail before thread/start"
        assert message["params"]["approvalPolicy"] == "never"
        assert message["params"]["ephemeral"] is True
        # Real current app-server ignores the legacy sandbox/config combination
        # and resolves it as readOnly. Require the explicit narrow named profile.
        native_write = message["params"].get("permissions", "").startswith("bread-owned-")
        thread_config = message["params"].get("config", {})
        if native_write:
            owned_profile = message["params"]["permissions"]
            assert thread_config.get(f"permissions.{owned_profile}") == {
                "filesystem": {":root": "read", ":workspace_roots": "write"},
                "network": {"enabled": False}}
            if mode == "custom-profile-unsupported":
                send({"id": request_id, "error": {"code": -32602,
                     "message": "Unsupported permissions profile"}})
                continue
        else:
            assert message["params"]["permissions"] == ":read-only"
        assert message["params"]["runtimeWorkspaceRoots"] == [message["params"]["cwd"]]
        assert "sandbox" not in message["params"]
        assert "dynamicTools" not in message["params"]
        thread = {"id": "thread-test"}
        if mode == "ambient-instructions":
            thread["instructionSources"] = [{"path": "C:/ambient/AGENTS.md"}]
        active_profile = {"id": owned_profile, "extends": None} if native_write else (
            {"id": ":workspace", "extends": None}
            if mode == "unverified-read-scope"
            else {"id": ":read-only", "extends": None}
        )
        roots = list(message["params"]["runtimeWorkspaceRoots"])
        if mode == "unverified-read-scope":
            roots.append("C:/ambient")
        sandbox = ({"type": "workspaceWrite", "writableRoots": [],
                    "networkAccess": False, "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}
                   if native_write else {"type": "readOnly", "networkAccess": False})
        if native_write and mode == "unverified-write-scope":
            sandbox["writableRoots"].append("C:/ambient")
        if native_write and mode == "ambient-tmp-write-scope":
            sandbox["excludeTmpdirEnvVar"] = False
        if native_write and mode == "legacy-write-ignored":
            active_profile = None
            sandbox = {"type": "readOnly", "networkAccess": False}
        send(
            {
                "id": request_id,
                "result": {
                    "thread": thread,
                    "activePermissionProfile": active_profile,
                    "runtimeWorkspaceRoots": roots,
                    "sandbox": sandbox,
                    "approvalPolicy": "never",
                },
            }
        )
    elif method == "mcpServerStatus/list":
        mcp_status_reads += 1
        # Like the real app-server, config reload has no thread/start request's
        # explicit permissions argument. A named table needs a config default.
        if thread_config and thread_config.get("default_permissions") != owned_profile:
            send({"id": request_id, "error": {"code": -32603,
                "message": "failed to reload config: config defines `[permissions]` profiles but does not set `default_permissions`"}})
            continue
        entries = []
        if mode in {"extra-mcp", "wrong-tools"}:
            entries.append(
                mcp_status("ambient", runtime_status="connected", with_tools=True)
            )
        elif mode == "native-mcp":
            entries.append(
                mcp_status("Bread", runtime_status="connected", with_tools=True)
            )
        elif mode == "starting-mcp":
            connected = mcp_status_reads >= 3
            entries.append(
                mcp_status(
                    "Bread",
                    runtime_status="connected" if connected else "starting",
                    with_tools=connected,
                )
            )
        elif mode == "stuck-starting-mcp":
            entries.append(
                mcp_status("Bread", runtime_status="starting", with_tools=False)
            )
        elif mode == "failed-mcp":
            entries.append(
                mcp_status("Bread", runtime_status="failed", with_tools=False)
            )
        elif mode == "empty-mcp-tools":
            entries.append(
                mcp_status("Bread", runtime_status="connected", with_tools=False)
            )
        send(
            {
                "id": request_id,
                "result": {
                    "data": entries,
                    "nextCursor": None,
                },
            }
        )
    elif method == "app/installed":
        apps = [{"id": "ambient-app", "callable": True}] if mode == "callable-app" else []
        send({"id": request_id, "result": {"apps": apps}})
    elif method == "turn/start":
        if mode == "starting-mcp":
            assert mcp_status_reads >= 3, "turn started before Bread MCP was ready"
        native_write = message["params"].get("permissions", "").startswith("bread-owned-")
        if native_write:
            assert message["params"]["permissions"] == owned_profile
            assert thread_config["default_permissions"] == owned_profile
        else:
            assert message["params"]["permissions"] == ":read-only"
        assert message["params"]["runtimeWorkspaceRoots"] == [message["params"]["cwd"]]
        assert "sandboxPolicy" not in message["params"]
        if mode.startswith("capacity-"):
            turn_count += 1
            turn_id = f"turn-capacity-{turn_count}"
            if mode == "capacity-recovery-ack-wait" and turn_count > 1:
                # The provider has accepted work but has not returned its id.
                (Path(message["params"]["cwd"]) / "accepted-recovery").write_text(turn_id)
                continue
            if mode == "capacity-request-rejected":
                send({"id": request_id, "error": {"code": -32603,
                    "message": "Selected model is at capacity. Please try a different model."}})
                continue
            send({"id": request_id, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
            if turn_count > 1:
                assert len(message["params"]["input"]) == 1
                assert message["params"]["input"][0]["type"] == "text"
                assert "Continue the unfinished user task in this same thread" in message["params"]["input"][0]["text"]
                if mode == "capacity-recovery-wait":
                    continue
            event_base = {"threadId": "thread-test", "turnId": turn_id}
            item = {"id": "one-write", "type": "fileChange", "status": "inProgress"}
            if turn_count == 1 and mode in {"capacity-after-tool", "capacity-pending-tool"}:
                send({"method": "item/started", "params": {**event_base, "item": item}})
                if mode == "capacity-after-tool":
                    # A real durable fixture effect must happen once across recovery.
                    target = Path(message["params"]["cwd"]) / "effect-count.txt"
                    target.write_text(str(int(target.read_text()) + 1) if target.exists() else "1")
                    send({"method": "item/completed", "params": {**event_base,
                        "item": {**item, "status": "completed"}}})
            if mode == "capacity-provider-retry":
                send({"method": "error", "params": {**event_base, "willRetry": True,
                    "error": {"message": "Selected model is at capacity.", "codexErrorInfo": "serverOverloaded"}}})
            fail = mode != "capacity-provider-retry" and (turn_count == 1 or mode == "capacity-exhausted")
            if fail:
                info = mode.removeprefix("capacity-tag-") if mode.startswith("capacity-tag-") else "serverOverloaded"
                error = {"message": "Selected model is at capacity. Please try a different model.",
                         "codexErrorInfo": info}
                if mode == "capacity-legacy":
                    error.pop("codexErrorInfo")
                turn = {"id": turn_id, "status": "interrupted" if mode == "capacity-interrupted" else "failed", "error": error}
                if mode == "capacity-snapshot-pending":
                    turn["items"] = [item]
                send({"method": "turn/completed", "params": {"threadId": "thread-test", "turn": turn}})
            else:
                send({"method": "item/completed", "params": {**event_base,
                    "item": {"type": "agentMessage", "phase": "final_answer", "text": "continued existing work"}}})
                send({"method": "turn/completed", "params": {"threadId": "thread-test",
                    "turn": {"id": turn_id, "status": "completed", "error": None}}})
            continue
        if mode == "pre-ack-large-notifications":
            event = {"threadId": "thread-test", "turnId": "turn-test"}
            # Multiple full lifecycle records cross the default 16 MiB watermark
            # before the turn/start response; no notification consumer exists yet.
            for index in range(3):
                item = {"id": f"pre-ack-{index}", "type": "mcpToolCall", "server": "Bread",
                        "tool": "inspect_event", "status": "inProgress",
                        "arguments": {"large": "x" * (5 * 1024 * 1024)}}
                send({"method": "item/started", "params": {**event, "item": item}})
                item["status"] = "completed"
                send({"method": "item/completed", "params": {**event, "item": item}})
        send(
            {
                "id": request_id,
                "result": {"turn": {"id": "turn-test", "status": "inProgress"}},
            }
        )
        if mode == "unexpected-host-tool":
            send(
                {
                    "id": 900,
                    "method": "item/tool/call",
                    "params": {
                        "threadId": "thread-test",
                        "turnId": "turn-test",
                        "callId": "removed-direct-tool-channel",
                        "tool": "inspect_event",
                        "arguments": {"event_id": 7},
                    },
                }
            )
            continue
        if mode == "invalid-jsonl":
            sys.stdout.flush()
            sys.stdout.buffer.write(b"{not-json}\n")
            sys.stdout.buffer.flush()
            raise SystemExit(18)
        if mode == "invalid-utf8":
            sys.stdout.flush()
            sys.stdout.buffer.write(b"\xff\xfe\n")
            sys.stdout.buffer.flush()
            raise SystemExit(19)
        if mode == "hang-turn":
            time.sleep(60)
        if mode == "slow-status":
            send(
                {
                    "method": "error",
                    "params": {
                        "threadId": "thread-test",
                        "turnId": "turn-test",
                        "message": "request timed out; retrying",
                    },
                }
            )
            time.sleep(0.05)
        if mode == "approval":
            send(
                {
                    "id": 900,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": "thread-test",
                        "turnId": "turn-test",
                        "itemId": "item-test",
                    },
                }
            )
        else:
            if mode == "activity-events":
                send(
                    {
                        "method": "turn/plan/updated",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "explanation": "Inspect the capture safely",
                            "plan": [
                                {"step": "Read pipeline state", "status": "inProgress"},
                                {"step": "token=plan-secret", "status": "pending"},
                            ],
                        },
                    }
                )
                send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {"id": "plan-1", "type": "plan", "text": ""},
                        },
                    }
                )
                send(
                    {
                        "method": "item/plan/delta",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "itemId": "plan-1",
                            "delta": "Inspect resources, then explain the draw.",
                        },
                    }
                )
                send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "plan-1",
                                "type": "plan",
                                "text": "Inspect resources, then explain the draw.",
                            },
                        },
                    }
                )
                send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "reasoning-1",
                                "type": "reasoning",
                                "summary": [],
                                "content": ["RAW_REASONING_STARTED_MUST_NOT_APPEAR"],
                            },
                        },
                    }
                )
                send(
                    {
                        "method": "item/reasoning/textDelta",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "itemId": "reasoning-1",
                            "delta": "RAW_REASONING_DELTA_MUST_NOT_APPEAR token=raw-secret",
                        },
                    }
                )
                for delta in (
                    "Checking the selected draw. ",
                    "Comparing its public bindings; password=summary-secret",
                ):
                    send(
                        {
                            "method": "item/reasoning/summaryTextDelta",
                            "params": {
                                "threadId": "thread-test",
                                "turnId": "turn-test",
                                "itemId": "reasoning-1",
                                "summaryIndex": 0,
                                "delta": delta,
                            },
                        }
                    )
                send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "reasoning-1",
                                "type": "reasoning",
                                "summary": [{"text": "Public reasoning summary complete."}],
                                "content": ["RAW_REASONING_COMPLETED_MUST_NOT_APPEAR"],
                            },
                        },
                    }
                )
                send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "command-1",
                                "type": "commandExecution",
                                "command": (
                                    "probe --token cmd-secret password=hunter2 "
                                    "OPENAI_API_KEY=env-secret Authorization: Bearer auth-secret"
                                ),
                                "cwd": "/safe/workspace",
                                "status": "inProgress",
                            },
                        },
                    }
                )
                for delta in ("tok", "en=stream-secret\nvisible output\n" + ("x" * 20_000)):
                    send(
                        {
                            "method": "item/commandExecution/outputDelta",
                            "params": {
                                "threadId": "thread-test",
                                "turnId": "turn-test",
                                "itemId": "command-1",
                                "delta": delta,
                            },
                        }
                    )
                send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "command-1",
                                "type": "commandExecution",
                                "command": (
                                    "probe --token cmd-secret password=hunter2 "
                                    "OPENAI_API_KEY=env-secret Authorization: Bearer auth-secret"
                                ),
                                "cwd": "/safe/workspace",
                                "status": "completed",
                                "aggregatedOutput": "api_key=final-output-secret\nfinal output ok",
                                "exitCode": 0,
                                "durationMs": 23,
                            },
                        },
                    }
                )
                send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "mcp-1",
                                "type": "mcpToolCall",
                                "server": "replaylab",
                                "tool": "get_pipeline_state",
                                "status": "inProgress",
                                "arguments": {
                                    "eventId": 7,
                                    "api_key": "mcp-secret",
                                    "clientSecret": "camel-secret-value",
                                },
                            },
                        },
                    }
                )
                send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {
                                "id": "mcp-1",
                                "type": "mcpToolCall",
                                "server": "replaylab",
                                "tool": "get_pipeline_state",
                                "status": "completed",
                                "arguments": {
                                    "eventId": 7,
                                    "api_key": "mcp-secret",
                                    "clientSecret": "camel-secret-value",
                                },
                                "result": {"text": "Bearer mcp-result-secret", "ok": True},
                                "durationMs": 11,
                            },
                        },
                    }
                )
                for item in (
                    {"id": "web-1", "type": "webSearch", "query": "RenderDoc docs"},
                    {"id": "image-1", "type": "imageView", "path": "/safe/frame.png"},
                    {"id": "compact-1", "type": "contextCompaction"},
                ):
                    send(
                        {
                            "method": "item/started",
                            "params": {
                                "threadId": "thread-test",
                                "turnId": "turn-test",
                                "item": item,
                            },
                        }
                    )
                    send(
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": "thread-test",
                                "turnId": "turn-test",
                                "item": item,
                            },
                        }
                    )
            if mode == "large-tool-record":
                # app-server sends complete native tool argument/result items,
                # not only the small public activity text Bread displays.
                event = {"threadId": "thread-test", "turnId": "turn-test"}
                item = {"id": "large-tool", "type": "mcpToolCall", "server": "Bread",
                        "tool": "publish_cbuffer_aliases", "status": "inProgress",
                        "arguments": {"semantic_fields": "x" * (5 * 1024 * 1024)}}
                send({"method": "item/started", "params": {**event, "item": item}})
                item.update(status="completed", result={"ok": True, "content": [
                    {"type": "image", "mimeType": "image/png", "data": "a" * (5 * 1024 * 1024)}]})
                send({"method": "item/completed", "params": {**event, "item": item}})
            if mode.startswith("snapshot-"):
                phase = mode.removeprefix("snapshot-")
                text = "x" * (5 * 1024 * 1024) if phase == "oversized" else "Snapshot answer"
                if phase == "oversized":
                    phase = "final_answer"
                send({"method": "turn/completed", "params": {"threadId": "thread-test",
                      "turn": {"id": "turn-test", "status": "completed", "error": None,
                               "items": [{"type": "agentMessage", "phase": phase, "text": text}]}}})
                continue
            answer = ("x" * (5 * 1024 * 1024) if mode == "large-agent-record" else
                      "x" * 100_000 if mode == "large-line" else "evidence answer")
            deltas = ["x" * 8192] * 129 if mode == "many-deltas" else ["evidence "]
            for delta in deltas:
                send(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "delta": delta,
                        },
                    }
                )
            send(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-test",
                        "turnId": "turn-test",
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": answer,
                        },
                    },
                }
            )
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-test",
                        "turn": {"id": "turn-test", "status": "completed", "error": None},
                    },
                }
            )
    elif method == "turn/interrupt":
        send({"id": request_id, "result": {}})
