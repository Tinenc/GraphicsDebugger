"""Explicit current-turn control, independent of unfinished task obligations.

Only the parsed current user request is considered. History, attachments, model
answers and self-review prompts cannot pause or resume a run.
"""
from __future__ import annotations

import re

from .conversation import current_user_request


def requests_pause(prompt: str) -> bool:
    text = current_user_request(prompt).strip()
    # Deliberately narrow imperative grammar. Questions about pausing, quoted
    # examples, negation and requests to inspect/fix a pause are ordinary work.
    chinese = re.fullmatch(
        r"(?:请|麻烦)?(?:先|现在|暂时)?(?:暂停|停下|停止)(?:一下)?"
        r"(?:当前|这个|本轮)?(?:任务|工作|操作|执行|对话)?"
        r"(?:[，,。；;\s]*(?:我|等|待|稍后|之后|后面|保留|保存|补齐|补好|修好)[^？?]*)?[。！!\s]*", text)
    if chinese and not re.search(
            r"为什么|为何|怎么|帮我|请你|不要暂停|别暂停|"
            r"(?:之后|稍后|后面|接着|现在|然后|再|并|同时)(?:先)?(?:分析|解释|排查|修复|检查|告诉)", text):
        return True
    return bool(re.fullmatch(
        r"(?:please\s+)?(?:pause|stop)(?:\s+(?:now|here|this task|the task|working|for now))?"
        r"(?:[,.\s]+(?:and\s+)?(?:wait(?:\s+for\s+me)?|I(?:'ll| will)\s+resume(?:\s+later)?))?[.!\s]*",
        text, re.I))


def pause_reply(prompt: str) -> str:
    if re.search(r"[\u3400-\u9fff]", current_user_request(prompt)):
        return "已暂停，保留现有进度。收到你明确的继续指示后再恢复。"
    return "Paused. Existing progress is preserved; work will resume when you ask to continue."
