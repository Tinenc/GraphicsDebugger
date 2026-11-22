"""Host-owned response-language selection and prompt guidance.

ReplayLab currently ships English and Simplified Chinese user interfaces.  A
clear current-user question takes precedence over that interface preference;
identifier-only or otherwise language-neutral requests fall back to the locale
supplied by qrenderdoc.
"""

from __future__ import annotations

import locale
import os
import re
from collections.abc import Iterable
from typing import Literal

ResponseLanguage = Literal["en", "zh_CN"]

_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_QUOTED_LITERAL_RE = re.compile(r'(?:"[^"\r\n]*"|“[^”\r\n]*”|‘[^’\r\n]*’)')
_URL_RE = re.compile(r"(?:https?|file)://\S+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"(?<!\w)(?:[A-Za-z]:[\\/]|\\\\)[^\s<>\"']+")
_POSIX_PATH_RE = re.compile(r"(?<![\w:])/(?:[^/\s]+/)*[^/\s]*")
_LONG_HEX_RE = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_IDENTIFIER_TOKEN_RE = re.compile(
    r"(?<!\w)[A-Za-z][A-Za-z0-9]*(?:[_.:-][A-Za-z0-9]+)+(?!\w)"
)
_LANGUAGE_SEGMENT_RE = re.compile(r"(?:[\r\n]+|[。！？!?]+\s*|(?<=\.)\s+)")
_INTERNAL_PROTOCOL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"child-(?:assistance|context|continuation|evidence|lifecycle|receipt|result|task)"
    r"(?:-[a-z0-9]+)*|"
    r"event-(?:delegation|scope|workflow)(?:-[a-z0-9]+)*|"
    r"host-(?:admitted|validated|validation)(?:-[a-z0-9]+)*"
    r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_INTERNAL_JARGON_RE = re.compile(
    r"\b(?:child\s+agent|host\s+(?:admission|validation)|"
    r"(?:failure|completion)\s+receipt|continuation\s+ledger|"
    r"failure[_ -]disposition)\b",
    re.IGNORECASE,
)
_CHINESE_LANGUAGE_NAME = r"(?:简体中文|中文(?:\s*[/／或]\s*简体中文)?|汉语)"
_ENGLISH_LANGUAGE_NAME = r"(?:英文|英语|English)"
_RESPONSE_LANGUAGE_NAME = rf"(?:{_CHINESE_LANGUAGE_NAME}|{_ENGLISH_LANGUAGE_NAME}|Chinese|Simplified\s+Chinese)"
_EXPLICIT_RESPONSE_LANGUAGE_PATTERNS = (
    re.compile(
        rf"""
        (?:请|请你|麻烦|务必|必须|需要|希望|要)?\s*
        (?:全程|最后|最终|统一|只|仅)?\s*
        (?:用|使用|采用|改用|切换(?:成|到|为)|以)\s*
        (?P<language>{_RESPONSE_LANGUAGE_NAME})\s*(?:来)?\s*
        (?:回答|回复|答复|作答|输出|总结)
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    re.compile(
        rf"""
        (?:回答|回复|答复|输出|总结)(?:的)?(?:语言)?\s*
        (?:请|务必|必须|需要|要)?\s*(?:统一|只|仅)?\s*
        (?:用|使用|采用|设为|设置为|改为|切换为)\s*
        (?P<language>{_RESPONSE_LANGUAGE_NAME})
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    re.compile(
        rf"""
        \b(?:please\s+)?(?:answer|reply|respond|write|output)\s+
        (?:only\s+)?(?:in|using)\s+
        (?P<language>{_RESPONSE_LANGUAGE_NAME})\b
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
    re.compile(
        rf"""
        \b(?:please\s+)?(?:use|switch\s+to)\s+(?:only\s+)?
        (?P<language>{_RESPONSE_LANGUAGE_NAME})\b
        (?:\s+(?:for|in)\s+(?:the\s+|your\s+|this\s+)?
        (?:answer|reply|response|output|summary))?
        """,
        re.IGNORECASE | re.VERBOSE,
    ),
)
_NEGATED_DIRECTIVE_PREFIX_RE = re.compile(
    r"(?:do\s+not|don't|dont|never|不要|别|请勿|不可|无需|不用|不)\s*$",
    re.IGNORECASE,
)
_TECHNICAL_LATIN_TOKENS = frozenset(
    {
        "api",
        "cbuffer",
        "compute",
        "cs",
        "dsv",
        "dxbc",
        "dxil",
        "eid",
        "fragment",
        "glsl",
        "hlsl",
        "main",
        "mainps",
        "option",
        "pixel",
        "ps",
        "qrenderdoc",
        "rdc",
        "renderdoc",
        "replaylab",
        "resourceid",
        "rtv",
        "shader",
        "srv",
        "spirv",
        "uav",
        "vertex",
        "vs",
    }
)


def _prose_for_language_detection(text: str) -> str:
    """Remove content that should not influence prose-language decisions."""

    prose = _FENCED_CODE_RE.sub(" ", str(text or ""))
    prose = _INLINE_CODE_RE.sub(" ", prose)
    prose = _QUOTED_LITERAL_RE.sub(" ", prose)
    prose = _URL_RE.sub(" ", prose)
    prose = _WINDOWS_PATH_RE.sub(" ", prose)
    prose = _POSIX_PATH_RE.sub(" ", prose)
    prose = _IDENTIFIER_TOKEN_RE.sub(" ", prose)
    return _LONG_HEX_RE.sub(" ", prose)


def _natural_latin_words(text: str) -> tuple[str, ...]:
    words: list[str] = []
    for word in _LATIN_WORD_RE.findall(text):
        if len(word) < 2 or word.casefold() in _TECHNICAL_LATIN_TOKENS:
            continue
        # ALL_CAPS and mixed-case tokens such as EID/MainPS are normally API or
        # resource identifiers, not reliable natural-language evidence.
        if word.islower() or (word[:1].isupper() and word[1:].islower()):
            words.append(word)
    return tuple(words)


def _language_signals(text: str) -> tuple[int, int]:
    prose = _prose_for_language_detection(text)
    return len(_HAN_RE.findall(prose)), len(_natural_latin_words(prose))


def _directive_language(value: str) -> ResponseLanguage:
    normalized = re.sub(r"\s+", " ", value.strip()).casefold()
    if "english" in normalized or "英文" in normalized or "英语" in normalized:
        return "en"
    return "zh_CN"


def explicit_response_language(user_request: str) -> ResponseLanguage | None:
    """Return the last non-negated language explicitly requested by the user."""

    request = str(user_request or "")
    matches: list[tuple[int, ResponseLanguage]] = []
    for pattern in _EXPLICIT_RESPONSE_LANGUAGE_PATTERNS:
        for match in pattern.finditer(request):
            prefix = request[max(0, match.start() - 16) : match.start()]
            if _NEGATED_DIRECTIVE_PREFIX_RE.search(prefix):
                continue
            matches.append(
                (match.start(), _directive_language(match.group("language")))
            )
    if not matches:
        return None
    # When a request corrects itself, the last explicit instruction is the one
    # nearest the actual task and therefore owns the response-language choice.
    return max(matches, key=lambda item: item[0])[1]


def normalize_response_locale(value: str | None) -> ResponseLanguage:
    """Reduce a qrenderdoc/system locale to one shipped response language."""

    normalized = str(value or "").strip().replace("-", "_").casefold()
    if (normalized == "zh" or normalized.startswith("zh_")
            or normalized.startswith("chinese")):
        return "zh_CN"
    return "en"


def configured_interface_locale() -> ResponseLanguage:
    """Use qrenderdoc's resolved UI language, falling back to the OS locale.

    qrenderdoc exports REPLAYLAB_RESPONSE_LOCALE after resolving its ``system``
    setting. Standalone API/ACP runs may not have that environment variable.
    This preference is independent of the language of a particular chat reply.
    """

    configured = os.environ.get("REPLAYLAB_RESPONSE_LOCALE", "").strip()
    if configured and configured.casefold() != "system":
        return normalize_response_locale(configured)
    return normalize_response_locale(locale.getlocale()[0] or os.environ.get("LANG"))


def infer_response_language(
    user_request: str,
    *,
    fallback_locale: str | None = "en",
) -> ResponseLanguage:
    """Prefer an explicit request, then clear prose, then the UI/system locale.

    Technical identifiers, URLs, and code are deliberately weak language
    signals.  This keeps a request such as ``EID 11329 MainPS`` aligned with the
    configured interface language while still treating ordinary English prose
    as English.
    """

    explicit = explicit_response_language(user_request)
    if explicit is not None:
        return explicit

    han_count, latin_word_count = _language_signals(user_request)

    if han_count:
        # A complete English phrase may legitimately contain a short Chinese
        # resource/user-data name. Two or more non-technical English words win
        # only when the Han segment is correspondingly short; otherwise Chinese
        # prose with embedded API terms remains Chinese.
        if latin_word_count >= 2 and han_count <= latin_word_count:
            return "en"
        return "zh_CN"
    if latin_word_count:
        return "en"

    return normalize_response_locale(fallback_locale)


def response_text_matches_language(text: str, language: str) -> bool:
    """Return whether visible prose consistently uses the selected language.

    Code, URLs, paths, hashes, and established technical identifiers are weak
    signals and are ignored. Language-neutral output therefore matches either
    language. A short embedded term is tolerated, while an obvious sentence or
    paragraph in the other language is rejected.
    """

    resolved = normalize_response_locale(language)
    prose = _prose_for_language_detection(text)
    segments = [
        segment for segment in _LANGUAGE_SEGMENT_RE.split(prose) if segment.strip()
    ]
    if not segments:
        return True

    for segment in segments:
        han_count = len(_HAN_RE.findall(segment))
        latin_word_count = len(_natural_latin_words(segment))
        if resolved == "zh_CN":
            # Any ordinary Latin prose is a visible language switch. Technical
            # identifiers, code, paths, URLs, hashes, and quoted source values
            # were already removed or classified as neutral above.
            if latin_word_count:
                return False
        else:
            if han_count:
                return False
    return True


def response_choice_text_matches_language(
    question: str,
    choices: Iterable[tuple[str, str]],
    language: str,
) -> bool:
    """Validate clarification prose without misclassifying exact choice identities.

    Choice labels frequently contain source-controlled Shader stages, entry points,
    resource names, or other identifiers. Those same exact tokens may be repeated in
    a localized description. Remove only label-derived Latin identifiers from the
    description copy used for language detection; the caller must still run the
    original complete visible text through :func:`response_text_is_user_safe`.
    """

    prose = [str(question or "")]
    for label, description in choices:
        localized_description = str(description or "")
        identifiers = set(
            re.findall(r"[A-Za-z][A-Za-z0-9_]*", str(label or ""))
        )
        for identifier in sorted(identifiers, key=len, reverse=True):
            localized_description = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(identifier)}"
                rf"(?![A-Za-z0-9_])",
                " ",
                localized_description,
                flags=re.IGNORECASE,
            )
        if localized_description:
            prose.append(localized_description)
    return response_text_matches_language("\n".join(prose), language)


def response_text_is_user_safe(text: str) -> bool:
    """Reject Host-internal protocol terms from a user-visible answer.

    The language gate deliberately ignores identifier-shaped tokens because
    graphics APIs use many of them. Internal workflow codes need a separate,
    narrow check so values such as ``child-continuation-failed`` can never leak
    through merely because the surrounding prose is in the right language.
    """

    visible = _FENCED_CODE_RE.sub(" ", str(text or ""))
    visible = _INLINE_CODE_RE.sub(" ", visible)
    return not (
        _INTERNAL_PROTOCOL_TOKEN_RE.search(visible)
        or _INTERNAL_JARGON_RE.search(visible)
    )


def repair_response_presentation(
    text: str,
    language: str,
    *,
    fallback: str,
) -> str:
    """Return safe localized prose without assigning workflow semantics.

    Language and internal-jargon checks are presentation concerns.  Keeping
    this helper deliberately unaware of terminal states, checkpoints, option
    identifiers, or tool evidence makes it impossible for a localization
    failure to demote or terminate otherwise valid work.
    """

    value = str(text or "").strip()
    if (
        value
        and response_text_matches_language(value, language)
        and response_text_is_user_safe(value)
    ):
        return value
    return str(fallback or "").strip()


def repair_choice_presentation(
    question: str,
    choices: Iterable[tuple[str, str]],
    language: str,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Repair only the visible fields of a structured clarification card.

    The caller owns option IDs, ordering, selection mode, continuation state,
    and checkpoint identity.  This function accepts and returns only visible
    strings so presentation validation cannot mutate any of those control-plane
    values.
    """

    resolved = normalize_response_locale(language)
    values = tuple(
        (str(label or ""), str(description or ""))
        for label, description in choices
    )
    if resolved == "zh_CN":
        fallback_question = "请选择一个选项以继续当前任务。"
        fallback_description = "选择此项后，将从当前检查点继续任务。"

        def fallback_label(index: int) -> str:
            return f"选项 {index}"

    else:
        fallback_question = "Choose an option to continue the current task."
        fallback_description = (
            "Select this option to continue the task from the current checkpoint."
        )

        def fallback_label(index: int) -> str:
            return f"Option {index}"

    repaired_question = repair_response_presentation(
        question,
        resolved,
        fallback=fallback_question,
    )
    repaired_choices: list[tuple[str, str]] = []
    for index, (label, description) in enumerate(values, start=1):
        repaired_label = repair_response_presentation(
            label,
            resolved,
            fallback=fallback_label(index),
        )
        description_matches = response_choice_text_matches_language(
            "",
            ((label, description),),
            resolved,
        )
        repaired_description = (
            description.strip()
            if description.strip()
            and description_matches
            and response_text_is_user_safe(description)
            else fallback_description
        )
        repaired_choices.append((repaired_label, repaired_description))

    return repaired_question, tuple(repaired_choices)


def response_language_instruction(language: str) -> str:
    """Return the mandatory user-visible language and style contract."""

    resolved = normalize_response_locale(language)
    if resolved == "zh_CN":
        return (
            "The selected response language is Simplified Chinese. Write every user-visible "
            "sentence, heading, question, status explanation, and final summary in clear, "
            "concise Simplified Chinese. Do not mix English prose into the Chinese answer. "
            "Keep only necessary technical identifiers and source tokens unchanged, including "
            "RenderDoc, Bread, API, EID, DXIL, HLSL, CBuffer, graphics-stage abbreviations, "
            "paths, code, hashes, resource names, and protocol IDs. Preserve ReplayLab only "
            "when it is part of a legacy path, executable, module, class, environment variable, "
            "schema, protocol ID, or exact MCP tool spelling. Translate Shader as 着色器 "
            "in ordinary prose, preserving it only inside code or an exact source/resource name. "
            "Lead with the practical outcome. Do not expose "
            "internal workflow, child-Agent, receipt, ledger, retry-history, or blocker-code "
            "jargon unless one exact internal fact is essential to explain the result."
        )
    return (
        "The selected response language is English. Write every user-visible sentence, "
        "heading, question, status explanation, and final summary in clear, concise English. "
        "Do not mix prose from another language into the English answer; preserve only quoted "
        "user data and necessary technical identifiers or source tokens. Lead with the practical "
        "outcome. Do not expose internal workflow, child-Agent, receipt, ledger, retry-history, "
        "or blocker-code jargon unless one exact internal fact is essential to explain the result."
    )


def contains_han(text: str) -> bool:
    """Return whether text contains Chinese Han prose candidates."""

    return _HAN_RE.search(str(text or "")) is not None


__all__ = [
    "ResponseLanguage",
    "contains_han",
    "explicit_response_language",
    "infer_response_language",
    "normalize_response_locale",
    "configured_interface_locale",
    "repair_choice_presentation",
    "repair_response_presentation",
    "response_language_instruction",
    "response_choice_text_matches_language",
    "response_text_is_user_safe",
    "response_text_matches_language",
]
