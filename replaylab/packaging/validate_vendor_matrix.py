"""Opt-in real-provider checks using the production engines and existing profiles.

No login, profile save, model fallback, installation, or scenario retry occurs.
Run inventory first; each capture invocation selects exactly one profile.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
import json
from pathlib import Path
import re
import sys
import threading
import time
import uuid

from langchain_core.callbacks import BaseCallbackHandler

from replaylab_agent.agent_contracts import OrchestratorConfig, RunRequest
from replaylab_agent.agent_diagnostics import close_logging, redact
from replaylab_agent.cli import _catalog, _read_context, _validate_frozen_context
from replaylab_agent.connectors.langchain_models import build_chat_model
from replaylab_agent.connectors.subscription import SubscriptionBackend
from replaylab_agent.deep_engine import DeepAgentsEngine
from replaylab_agent.errors import ConfigurationError
from replaylab_agent.native_engine import NativeAgentEngine
from replaylab_agent.probe import probe_profile
from replaylab_agent.process_env import (
    codebuddy_profile_home, codex_profile_home, cursor_profile_home, managed_vendor_profile_home,
    resolve_agent_executable,
)
from replaylab_agent.profiles import ProfileStore


class ModelCallBudget(BaseCallbackHandler):
    """Reject before HTTP when the shared main/child model exceeds its budget."""

    raise_error = True

    def __init__(self, maximum: int = 6):
        self.maximum = maximum
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._lock = threading.Lock()

    def on_chat_model_start(self, serialized, messages, **kwargs):
        with self._lock:
            if self.calls >= self.maximum:
                raise ConfigurationError("validation model-call budget exhausted")
            self.calls += 1

    def on_llm_end(self, response, **kwargs):
        with self._lock:
            for group in response.generations:
                # One completion per request. Do not count alternate candidates.
                if group:
                    usage = getattr(getattr(group[0], "message", None), "usage_metadata", None) or {}
                    self.input_tokens += int(usage.get("input_tokens", 0))
                    self.output_tokens += int(usage.get("output_tokens", 0))


def safe_text(value: object) -> str:
    """Diagnostic artifacts omit credentials and all endpoint/account URLs."""
    return re.sub(r"https?://[^\s<>\"']+", "<url omitted>", redact(str(value)))


def safe_value(value: object) -> object:
    """Redact nested diagnostic values while preserving their JSON structure."""
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, Mapping):
        return {str(key): safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(item) for item in value]
    return value


def inventory(store: ProfileStore) -> list[dict]:
    rows = []
    for profile in store.profiles.values():
        row = {"id": profile.id, "kind": profile.provider.kind,
               "preset": profile.preset, "model": profile.model}
        if profile.provider.kind == "model":
            row["runtimeInstalled"] = True
            row["credentialAvailable"] = (
                None if profile.provider.auth in {"adc", "managed"}
                else profile.provider.auth == "none" or bool(profile.resolve_api_key())
            )
        else:
            row["runtimeInstalled"] = bool(resolve_agent_executable(profile))
            try:
                home = (codex_profile_home(profile) if profile.preset == "codex"
                        else cursor_profile_home(profile) if profile.preset == "cursor-acp"
                        else codebuddy_profile_home(profile) if profile.preset == "codebuddy-acp"
                        else managed_vendor_profile_home(profile))
            except ConfigurationError:
                # Custom ACP agents have no first-party credential-cache contract.
                row["credentialCachePresent"] = None
            else:
                row["credentialCachePresent"] = any((home / name).is_file() for name in (
                    "auth.json", ".credentials.json", "credentials.json", "oauth_creds.json",
                    ".qwen/oauth_creds.json", ".gemini/oauth_creds.json", ".copilot/config.json",
                    "cli-config.json", "config.json",
                ))
            row["credentialAvailable"] = None  # Cache existence is not authentication proof.
        rows.append(row)
    return rows


async def handshake(profile) -> dict:
    if profile.provider.kind == "model":
        report = await probe_profile(profile, timeout=20)
        return {"status": report.status, "selectedModelAvailable": report.selected_model_available,
                "modelCount": len(report.models), "generationRequested": False}
    result = await SubscriptionBackend(profile, timeout=30, emit=lambda event: None).test(asyncio.Event())
    return {"status": "handshake-succeeded", "sessionCreated": True,
            "storedAccountValidated": (result.authenticated
                                       if profile.provider.protocol == "codex-app-server" else None),
            "modelCount": len(result.models), "generationRequested": False}


def read_bound_context(args):
    raw = json.loads(args.context_file.read_text(encoding="utf-8"))
    if raw.get("schema") != "renderdoc-ultra-current-context/v1":
        raise ConfigurationError("validation requires a frozen RenderDoc context")
    _validate_frozen_context(raw)
    if raw.get("capture", {}).get("captureSessionId") != args.capture_session:
        raise ConfigurationError("validation capture session does not match the supplied snapshot")
    return _read_context(argparse.Namespace(
        context_file=args.context_file, live_context=False, instance_id=args.instance_id))


async def run_capture(profile, args) -> dict:
    context, instance_id, capture_path = read_bound_context(args)
    task_id = f"matrix-{profile.preset}-{uuid.uuid4().hex[:12]}"
    root = args.output / task_id
    root.mkdir(parents=True)
    budget = ModelCallBudget(6)
    if profile.provider.kind == "model":
        profile = replace(profile, options={**profile.options, "max_output_tokens": 1200,
                                           "timeout_seconds": 60})

    def model_factory(selected_profile, selected_model):
        model = build_chat_model(selected_profile, model=selected_model, max_retries=0)
        model.callbacks = [budget]
        return model

    config = OrchestratorConfig(main_timeout=180, allowed_effects=frozenset(),
                                event_root=root / "events", log_root=root / "logs",
                                workspace_base=root / "workspaces")
    kwargs = {"catalog": _catalog(), "config": config}
    engine = (DeepAgentsEngine(**kwargs, model_factory=model_factory)
              if profile.provider.kind == "model" else NativeAgentEngine(**kwargs))
    request = RunRequest(
        prompt=(f"这是一次有界只读供应商验收。用户指定目标 EID {args.event_id}，并非快照当前的 Present。"
                f"只调用 inspect_event(event_id={args.event_id}) 一次取得证据，简述 action、绑定阶段与输出。"
                "可以按需加载相关 Skill。说明观察到的事实，至多 250 个中文字；禁止改变当前事件、修改项目或工作区、"
                "编译、替换着色器、写文件和生成图或纹理；无需拓展整个帧或调用子任务。"
                "若有错误或需要授权则如实结束，不重试模型请求，不自行切换模型。"),
        profile=profile, task_id=task_id, model=profile.resolve_model(), context=context,
        instance_id=instance_id, capture_path=capture_path,
        intent={"kind": "explain-current-event", "arguments": {"eventId": args.event_id}},
        skill_name="renderdoc-explain-event", mcp_executable=args.mcp_host,
    )
    started = time.monotonic()
    summary = {"profile": profile.id, "preset": profile.preset, "model": request.model,
               "taskId": task_id, "instanceId": instance_id, "eventId": args.event_id,
               "captureSessionId": args.capture_session, "snapshotId": context["snapshotId"],
               "limits": {"seconds": 180, "toolCalls": 8,
                          "apiModelCalls": 6, "apiOutputTokensPerCall": 1200, "apiSdkRetries": 0}}
    try:
        result = await asyncio.wait_for(engine.run(request), timeout=180)
        summary.update({"terminalStatus": result.terminal_status, "engine": result.core,
                        "error": safe_text(result.error), "text": safe_text(result.text)})
    except Exception as error:
        summary.update({"terminalStatus": "failed", "exceptionType": type(error).__name__,
                        "error": safe_text(error)[:600]})
    finally:
        close_logging()
    summary.update({"seconds": round(time.monotonic() - started, 3),
                    "apiModelCalls": budget.calls, "apiInputTokens": budget.input_tokens,
                    "apiOutputTokens": budget.output_tokens})
    events = root / "events" / f"{task_id}.jsonl"
    if events.is_file():
        records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines() if line]
        summary["eventCounts"] = dict(Counter(row.get("kind", "") for row in records))
        summary["tools"] = [row.get("tool", "") for row in records if row.get("kind") == "tool-started"]
    state_path = root / "events" / f"{task_id}.report-state.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        summary["toolReceipts"] = {key: state.get(key) for key in (
            "toolsUsed", "toolFacts", "artifactPaths")}
    path = root / "result.json"
    safe_summary = safe_value(summary)
    path.write_text(json.dumps(safe_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {key: safe_summary[key] for key in (
        "profile", "preset", "taskId", "terminalStatus", "seconds", "apiModelCalls", "apiOutputTokens"
    )} | {"resultPath": str(path), "error": safe_summary.get("error", "")}


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inventory", "handshake", "capture"))
    parser.add_argument("--profile")
    parser.add_argument("--output", type=Path, default=Path("output/vendor-matrix-20260905"))
    parser.add_argument("--context-file", type=Path)
    parser.add_argument("--instance-id")
    parser.add_argument("--capture-session")
    parser.add_argument("--event-id", type=int)
    parser.add_argument("--mcp-host", type=Path)
    args = parser.parse_args(argv)
    store = ProfileStore().load()
    if args.mode == "inventory":
        value = inventory(store)
    else:
        if not args.profile or args.profile not in store.profiles:
            raise ConfigurationError("choose an exact configured profile ID")
        profile = store.profiles[args.profile]
        profile.validate()
        if args.mode == "handshake":
            started = time.monotonic()
            try:
                value = await asyncio.wait_for(handshake(profile), timeout=45)
            except Exception as error:
                value = {"status": "failed", "exceptionType": type(error).__name__,
                         "generationRequested": False, "error": safe_text(error)[:600]}
            value.update({"profile": profile.id, "preset": profile.preset, "model": profile.model,
                          "seconds": round(time.monotonic() - started, 3)})
            args.output.mkdir(parents=True, exist_ok=True)
            path = args.output / f"handshake-{profile.preset}-{uuid.uuid4().hex[:12]}.json"
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            if not all((args.context_file, args.instance_id, args.capture_session, args.event_id)):
                raise ConfigurationError("capture requires context, instance, session and event")
            value = await run_capture(profile, args)
    print(json.dumps(value, ensure_ascii=False))
    return value


if __name__ == "__main__":
    asyncio.run(main())
