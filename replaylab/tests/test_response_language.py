from __future__ import annotations

import pytest

from replaylab_agent.response_language import (
    configured_interface_locale,
    infer_response_language,
    normalize_response_locale,
    response_choice_text_matches_language,
    response_language_instruction,
    response_text_is_user_safe,
    response_text_matches_language,
)


@pytest.mark.parametrize(
    ("user_text", "fallback_locale", "expected"),
    [
        (
            "请检查 EID 11329 的 fragment MainPS，并用 DXIL 证据说明结果。",
            "en",
            "zh_CN",
        ),
        (
            "Please inspect EID 11329 fragment MainPS and explain the DXIL result.",
            "zh_CN",
            "en",
        ),
    ],
)
def test_question_prose_outranks_technical_identifiers(
    user_text: str,
    fallback_locale: str,
    expected: str,
) -> None:
    assert (
        infer_response_language(user_text, fallback_locale=fallback_locale)
        == expected
    )


@pytest.mark.parametrize(
    ("user_text", "fallback_locale", "expected"),
    [
        ("请用英文回答：分析 EID 11329 的 MainPS。", "zh_CN", "en"),
        ("Please answer in English: analyze EID 11329 MainPS.", "zh_CN", "en"),
        ("请用简体中文回答：分析 EID 11329 的 MainPS。", "en", "zh_CN"),
        (
            "Please answer in Simplified Chinese: analyze EID 11329 MainPS.",
            "en",
            "zh_CN",
        ),
    ],
)
def test_explicit_response_language_overrides_question_language_and_ui_fallback(
    user_text: str,
    fallback_locale: str,
    expected: str,
) -> None:
    assert (
        infer_response_language(user_text, fallback_locale=fallback_locale)
        == expected
    )


def test_negated_and_corrected_language_instructions_are_resolved_safely() -> None:
    assert (
        infer_response_language(
            "不要用英文回答，请用中文回答。",
            fallback_locale="en",
        )
        == "zh_CN"
    )
    assert (
        infer_response_language(
            "Please answer in Chinese. Actually, answer in English.",
            fallback_locale="zh_CN",
        )
        == "en"
    )


@pytest.mark.parametrize(
    ("fallback_locale", "expected"),
    [
        ("zh_CN", "zh_CN"),
        ("zh-CN", "zh_CN"),
        ("en", "en"),
        ("en_US", "en"),
    ],
)
def test_identifier_only_request_uses_ui_or_system_locale_fallback(
    fallback_locale: str,
    expected: str,
) -> None:
    assert (
        infer_response_language(
            "EID 11329 fragment MainPS DXIL CBuffer",
            fallback_locale=fallback_locale,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("zh", "zh_CN"),
        ("zh-CN", "zh_CN"),
        ("zh_Hans_CN", "zh_CN"),
        ("Chinese (Simplified)_China", "zh_CN"),
        ("en", "en"),
        ("en-US", "en"),
        ("system", "en"),
        ("", "en"),
    ],
)
def test_response_locale_normalization_matches_shipped_languages(
    locale: str,
    expected: str,
) -> None:
    assert normalize_response_locale(locale) == expected


@pytest.mark.parametrize(
    ("ui_locale", "system_locale", "expected"),
    [
        ("zh_CN", "en_US", "zh_CN"),
        ("en_US", "zh_CN", "en"),
        ("system", "zh_CN", "zh_CN"),
        ("", "en_US", "en"),
    ],
)
def test_source_comment_locale_uses_renderdoc_ui_then_system(
    monkeypatch: pytest.MonkeyPatch,
    ui_locale: str,
    system_locale: str,
    expected: str,
) -> None:
    monkeypatch.setenv("REPLAYLAB_RESPONSE_LOCALE", ui_locale)
    monkeypatch.setattr("replaylab_agent.response_language.locale.getlocale",
                        lambda: (system_locale, "UTF-8"))
    assert configured_interface_locale() == expected


def test_response_instructions_require_one_plain_concise_language() -> None:
    chinese = response_language_instruction("zh_CN")
    english = response_language_instruction("en")

    assert "clear, concise Simplified Chinese" in chinese
    assert "Do not mix English prose" in chinese
    assert "RenderDoc, Bread, API" in chinese
    assert "Preserve ReplayLab only" in chinese
    assert "Translate Shader as 着色器" in chinese
    assert "clear, concise English" in english
    assert "Do not mix prose from another language" in english


def test_response_language_gate_ignores_code_but_rejects_mixed_prose() -> None:
    chinese_with_code = """分析完成。

```hlsl
float4 main() : SV_Target { return 1.0; }
```

资源绑定正常。"""
    assert response_text_matches_language(chinese_with_code, "zh_CN")
    assert not response_text_matches_language(
        "分析完成。 The workflow remains incomplete because validation failed.",
        "zh_CN",
    )
    assert not response_text_matches_language(
        "The inspection completed. 资源绑定仍未确认。",
        "en",
    )


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Continue?", "zh_CN"),
        ("## Summary", "zh_CN"),
        ("检查完成，但 Summary 尚未生成。", "zh_CN"),
        ("失败。", "en"),
    ],
)
def test_response_language_gate_rejects_even_short_language_switches(
    text: str,
    language: str,
) -> None:
    assert not response_text_matches_language(text, language)


def test_response_language_gate_preserves_technical_and_quoted_source_tokens() -> None:
    assert response_text_matches_language(
        "已确认 EID 11329、MainPS、DXIL 和 CBuffer。",
        "zh_CN",
    )
    assert response_text_matches_language(
        'The resource named “角色” uses MainPS and DXIL.',
        "en",
    )


def test_choice_language_gate_ignores_repeated_entry_points_but_not_english_prose() -> None:
    question = "当前事件绑定了两个不同阶段的着色器。请选择要反编译的目标："
    assert response_choice_text_matches_language(
        question,
        [
            (
                "像素着色器 MainPS",
                "反编译 fragment 阶段、入口点 MainPS、资源 1000000000000000860。",
            ),
            (
                "顶点着色器 Main",
                "反编译 vertex 阶段、入口点 Main、资源 1000000000000000722。",
            ),
        ],
        "zh_CN",
    )
    assert not response_choice_text_matches_language(
        question,
        [("像素着色器 MainPS", "Use MainPS because it is usually the final material stage.")],
        "zh_CN",
    )


@pytest.mark.parametrize(
    "text",
    [
        "已完成着色器检查，但 Child Agent evidence did not pass Host validation。",
        "已完成检查，内部状态为 child-continuation-failed。",
        "The Host validation receipt was rejected.",
    ],
)
def test_user_visible_safety_gate_rejects_internal_protocol_jargon(text: str) -> None:
    assert not response_text_is_user_safe(text)


def test_user_visible_safety_gate_ignores_source_code() -> None:
    assert response_text_is_user_safe(
        "检查完成。\n\n```text\nchild-continuation-failed\n```"
    )
