"""UI progress events shared by ReplayLab Agent engines.

Emits ``replaylab-ai-task-event/v1`` records shared by qrenderdoc's result view
and Agent Monitor.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Mapping, Sequence

from .events import TaskEventWriter
from .agent_protocol import ClarificationRequest
from .run_lifetime import current_run_lifetime

_CHUNK = 8 * 1024
_MAX_TEXT = 1024 * 1024
_MAX_DETAIL = 1000


def _bounded(text: str, limit: int = _MAX_DETAIL) -> str:
    text = str(text or "").replace("\r", "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ProgressReporter:
    def __init__(
        self,
        writer: TaskEventWriter,
        *,
        profile_id: str,
        backend: str,
        strategy: str = "deepagents/v1",
    ) -> None:
        self.writer = writer
        self.profile_id = profile_id
        self.backend = backend
        self.strategy = strategy
        self._child_seq = 0
        self._main_text_chars = 0
        self._child_item_seq: dict[str, int] = {}

    # ------------------------------------------------------------- lifecycle
    def queued(self, *, prompt_chars: int, preset: str, profile_title: str, model: str,
               conversation_id: str = "") -> None:
        lifetime = current_run_lifetime()
        self.writer.emit(
            "queued",
            backend=self.backend,
            profile=self.profile_id,
            summary="request queued",
            data={
                "promptChars": int(prompt_chars),
                "attempt": 1,
                "preset": preset,
                "profileTitle": profile_title,
                "model": model,
                **({"conversationId": conversation_id} if conversation_id else {}),
                "activityTitleId": "task.queued",
                "activityTitleArgs": [],
                **(lifetime.metadata() if lifetime is not None else {}),
            },
        )

    def _running(
        self,
        *,
        phase: str,
        status: str,
        title_id: str,
        title_args: Sequence[Any] = (),
        summary: str = "",
        detail: str = "",
        plan_task_id: str = "",
        skill: str = "",
        activity_id: str = "",
        activity_kind: str = "",
        progress: tuple[int, int] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "phase": phase,
            "status": status,
            "planTaskId": plan_task_id,
            "skill": skill,
            "activitySummary": _bounded(summary),
            "activityTitleId": title_id,
            "activityTitleArgs": [_bounded(str(a)) for a in title_args][:8],
            "activityDetailId": "task.detail.public-summary" if detail else "",
            "activityDetailArgs": [_bounded(detail)] if detail else [],
            "strategy": self.strategy,
        }
        if activity_id:
            data["activityId"] = activity_id
        if activity_kind:
            data["activityKind"] = activity_kind
        if progress is not None:
            data["progressCurrent"], data["progressTotal"] = progress
        if extra:
            data.update(extra)
        self.writer.emit(
            "running",
            backend=self.backend,
            profile=self.profile_id,
            summary=_bounded(summary or detail),
            data=data,
        )

    def main_started(self, summary: str = "Main Agent is analysing the request") -> None:
        self._running(
            phase="main",
            status="started",
            title_id="task.main.started",
            summary=summary,
            progress=(0, 1),
        )

    def main_heartbeat(self, *, elapsed_seconds: int, children: int) -> None:
        minutes, seconds = divmod(int(elapsed_seconds), 60)
        clock = f"{minutes}:{seconds:02d}"
        detail = f"Main Agent is working ({clock}"
        if children:
            detail += f", {children} child task(s) so far"
        detail += ")"
        self._running(
            phase="main",
            status="running",
            title_id="task.main.started",
            summary=detail,
            detail=detail,
            progress=(0, 1),
        )

    def main_completed(self, summary: str) -> None:
        self._running(
            phase="main",
            status="completed",
            title_id="task.main.completed",
            summary=summary,
            progress=(1, 1),
        )

    def main_message(self, text: str) -> None:
        """Stream the main Agent's own text into the live answer area."""

        if not text:
            return
        if self._main_text_chars >= _MAX_TEXT:
            return
        text = text[: _MAX_TEXT - self._main_text_chars]
        self._main_text_chars += len(text)
        for offset in range(0, len(text), _CHUNK):
            self.writer.emit_delta(
                "message-delta",
                profile=self.profile_id,
                text=text[offset : offset + _CHUNK],
                data={"stream": "provider", "reset": False},
            )

    def discard_main_message(self) -> None:
        self._main_text_chars = 0
        self.writer.emit("message-delta", profile=self.profile_id, text="",
                         data={"stream": "provider", "reset": True})

    def main_reasoning(self, payload: Mapping[str, Any]) -> None:
        data = dict(payload)
        data.setdefault("agentId", "main")
        data.setdefault("agentRole", "main")
        text = str(data.pop("text", "") or "")
        for offset in range(0, min(len(text), _MAX_TEXT), _CHUNK):
            self.writer.emit_delta(
                "reasoning-delta",
                profile=self.profile_id,
                text=text[offset : offset + _CHUNK],
                data=data,
            )

    # --------------------------------------------------------------- children
    def new_child_id(self) -> str:
        self._child_seq += 1
        return f"child-{self._child_seq}"

    def child_started(self, child_id: str, *, skill: str, agent_id: str, summary: str) -> None:
        self._running(
            phase="child",
            status="started",
            title_id="task.child.started",
            title_args=[skill or agent_id],
            summary=summary,
            detail=summary,
            plan_task_id=child_id,
            skill=skill,
            progress=(0, 1),
        )

    def child_message(self, child_id: str, text: str, *, skill: str = "") -> None:
        if not text:
            return
        item = self._child_item_seq.setdefault(child_id, 1)
        bounded = text[:_MAX_TEXT]
        chunks = 0
        for chunks, offset in enumerate(range(0, len(bounded), _CHUNK), 1):
            self.writer.emit_delta(
                "agent-message-delta",
                profile=self.profile_id,
                text=bounded[offset : offset + _CHUNK],
                data={
                    "source": self.backend,
                    "channel": "message",
                    "stream": "provider-observation",
                    "agentId": child_id,
                    "agentRole": "child",
                    "planTaskId": child_id,
                    "skill": skill,
                    "phase": "commentary",
                    "itemId": f"{child_id}:msg:{item + chunks - 1}",
                    "reset": False,
                },
            )
        self._child_item_seq[child_id] = item + chunks

    def child_reasoning(self, child_id: str, payload: Mapping[str, Any]) -> None:
        data = dict(payload)
        data["agentId"] = child_id
        data["agentRole"] = "child"
        data["planTaskId"] = child_id
        text = str(data.pop("text", "") or "")
        for offset in range(0, min(len(text), _MAX_TEXT), _CHUNK):
            self.writer.emit_delta(
                "reasoning-delta",
                profile=self.profile_id,
                text=text[offset : offset + _CHUNK],
                data=data,
            )

    def child_tool_call(self, child_id: str, name: str, *, call_id: str, arguments: Mapping[str, Any] | None) -> None:
        """Record a Host-executed ReplayLab tool start."""

        self.writer.emit(
            "tool-started",
            profile=self.profile_id,
            tool=name,
            summary=name,
            data={
                "activityId": f"{child_id}:{call_id}",
                "planTaskId": child_id,
                "argumentNames": sorted(str(k) for k in (arguments or {}).keys())[:16],
            },
        )

    def child_tool_result(self, child_id: str, name: str, *, call_id: str, failed: bool, detail: str = "") -> None:
        self.writer.emit(
            "tool-completed",
            profile=self.profile_id,
            tool=name,
            summary=_bounded(detail or name),
            data={
                "activityId": f"{child_id}:{call_id}",
                "planTaskId": child_id,
                "status": "failed" if failed else "completed",
            },
        )

    def child_completed(self, child_id: str, *, skill: str, agent_id: str, summary: str, tool_calls: int) -> None:
        self._running(
            phase="child",
            status="completed",
            title_id="task.child.completed",
            title_args=[skill or agent_id],
            summary=summary,
            detail=f"{summary} (tool calls: {tool_calls})",
            plan_task_id=child_id,
            skill=skill,
            progress=(1, 1),
        )

    def child_failed(self, child_id: str, *, skill: str, agent_id: str, error: str) -> None:
        self._running(
            phase="child",
            status="failed",
            title_id="task.child.failed",
            title_args=[skill or agent_id],
            summary=f"Child failed: {_bounded(error, 300)}",
            detail=error,
            plan_task_id=child_id,
            skill=skill,
            progress=(1, 1),
        )

    # --------------------------------------------------------------- terminal
    def final_answer(self, text: str) -> None:
        bounded = str(text or "")
        if len(bounded) > _MAX_TEXT:
            bounded = bounded[:_MAX_TEXT] + "\n\n[Result truncated at the TaskEvent safety limit.]"
        if not bounded:
            bounded = ""
        offset = 0
        first = True
        while True:
            chunk = bounded[offset : offset + _CHUNK]
            self.writer.emit(
                "message-delta",
                profile=self.profile_id,
                text=chunk,
                data={"stream": "final", "reset": first},
            )
            first = False
            offset += _CHUNK
            if offset >= len(bounded):
                break

    def completed(
        self,
        *,
        text: str,
        terminal_status: str,
        clarification: ClarificationRequest | None = None,
        context: Mapping[str, Any] | None = None,
        instance_id: str = "",
    ) -> str:
        # Completion marks the provider turn's lifecycle. The Agent judges
        # task acceptance from the user's goal, Skill guidance and actual tools;
        # the Host does not invent a semantic pass or force another model round.
        data: dict[str, Any] = {
            "terminalStatus": terminal_status,
            "completionScope": "agent-turn",
            "acceptanceAuthority": "agent",
        }
        if clarification is not None:
            payload = clarification.event_payload()
            if context and instance_id:
                payload["originContextSnapshot"] = dict(context)
                payload["originContextInstanceId"] = instance_id
            data["clarification"] = payload
        delivery_id = "agent-" + hashlib.sha256(
            f"{self.writer.task_id}:{terminal_status}:{time.time_ns()}".encode()
        ).hexdigest()[:32]
        self.writer.emit(
            "completed",
            delivery_id=delivery_id,
            backend=self.backend,
            profile=self.profile_id,
            summary=_bounded(text, 500),
            data=data,
        )
        return delivery_id

    def failed(self, error: str, *, cause: BaseException | None = None) -> None:
        from .provider_diagnostics import provider_failure_metadata
        metadata = provider_failure_metadata(cause) if cause is not None else {}
        self.writer.emit(
            "failed",
            backend=self.backend,
            profile=self.profile_id,
            summary=_bounded(error, 1000),
            data={"terminalStatus": "failed", "error": _bounded(error, 4000), **metadata},
        )

    def cancelled(self) -> None:
        lifetime = current_run_lifetime()
        owner_exited = lifetime is not None and lifetime.cancellation_reason == "owner-process-exited"
        self.writer.emit(
            "cancelled",
            backend=self.backend,
            profile=self.profile_id,
            summary="The application exited; the unfinished task was cancelled." if owner_exited else "cancelled by user",
            data={
                "terminalStatus": "cancelled", "reason": "owner-process-exited",
                "activityTitleId": "task.owner-exited", "activityTitleArgs": [],
                **lifetime.metadata(),
            } if owner_exited else {},
        )


__all__ = ["ProgressReporter"]
