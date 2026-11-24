"""Getting whatever a tool produced -- a picture, a document, a sound -- to the model.

Native Codex and ACP agents receive Host image blocks through the task MCP proxy.
A path alone is not proof of visual access: a vendor may lack a file-image tool or
permission to read that path. API agents receive images through ``deep_engine``'s
provider-specific tool-result conversion here; unsupported routes are explicit.

What was checked, by reading the installed packages rather than guessing:

* ``langchain_core.tools.base``: a tool that returns a ``list`` keeps it as
  ``ToolMessage`` content only when every element's ``type`` is in
  ``TOOL_MESSAGE_BLOCK_TYPES`` (``text``, ``image_url``, ``image``, ``json``,
  ``search_result``, ``custom_tool_call_output``, ``document``, ``file``). Anything
  else is JSON-stringified, so a block shape outside that list is silently lost.
* ``langchain_anthropic.chat_models._merge_messages``: a list-content ``ToolMessage``
  becomes ``HumanMessage([{"type": "tool_result", "content": <verbatim>}])``, and the
  block walk in ``_format_messages`` only converts the *top* level. Nested content is
  passed through untranslated, so it must already be Anthropic's own shape. The
  Anthropic SDK's ``ToolResultBlockParam.Content`` accepts text, image and document
  blocks, and ``Base64PDFSourceParam`` is ``application/pdf`` only.
* ``langchain_openai.chat_models.base._ensure_valid_tool_message_content``: for the
  Responses API a list survives only if every block is one of ``input_text``,
  ``input_image``, ``input_file``, ``text``, ``image_url`` or ``file``; the chat-style
  ``image_url`` / ``file`` shapes are then converted to ``input_image`` / ``input_file``.
  Those two are also in langchain-core's list, so they are the shapes that survive
  both layers.
* ``openai.types.chat.ChatCompletionToolMessageParam.content`` is a string or text
  parts only. The Chat Completions API cannot carry a picture in a tool result at all.
  For explicitly vision-enabled Chat connections, ``tool_image_middleware`` instead
  projects images into reference-only user image parts after the completed tool
  batch. This changes only the model request, never the stored user conversation.

Nothing here is a policy about what the model *should* look at. It answers one
question -- can this connection carry this kind of content -- and when the answer is
no it says so in the result rather than dropping it silently, because a model told
nothing about a missing picture will describe it anyway.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping, Sequence

# What each provider's API can carry inside a tool result, by media kind. Absent means
# "not on this connection"; the model is told, and can convert the file with a tool it
# has registered instead. Protocol ids come from the connection profile.
PROTOCOL_MEDIA: dict[str, dict[str, frozenset[str]]] = {
    "anthropic-messages": {
        "image": frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"}),
        "document": frozenset({"application/pdf", "text/plain"}),
    },
    "openai-responses": {
        "image": frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"}),
        "document": frozenset({"application/pdf"}),
    },
    "openai-chat": {},
    # Chat Completions underneath, so text-only for the same reason as openai-chat.
    "azure-openai": {},
    # langchain-google-genai folds a ToolMessage's content into a function_response
    # JSON payload rather than into image Parts, so an inlined picture would arrive as
    # a base64 string the model reads as text -- tokens spent, nothing seen. Until
    # that is verified against a live Gemini turn, the honest answer is the note.
    "google-genai": {},
    "google-cloud": {},
}

# Anthropic refuses an image over 5 MB and OpenAI a file over 10 MB, so one item is
# capped below the lower bound, and the whole result below what a turn should spend.
# There is no count limit: bytes are the real cost, and a count would just be a second
# rule to trip over.
MAX_INLINE_ITEM_BYTES = 3_500_000
MAX_INLINE_TOTAL_BYTES = 6_000_000

_IMAGE_ALIASES = {"image/jpg": "image/jpeg"}


def protocol_media(protocol: str) -> dict[str, frozenset[str]]:
    """What this connection can carry in a tool result, by kind."""

    return PROTOCOL_MEDIA.get(str(protocol or "").strip().lower(), {})


def carries(protocol: str, kind: str, media_type: str) -> bool:
    """Whether this exact media type reaches the model on this connection."""

    return media_type in protocol_media(protocol).get(kind, frozenset())


def _kind_for(media_type: str) -> str:
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("audio/"):
        return "audio"
    if media_type.startswith("video/"):
        return "video"
    return "document"


def _entry(media_type: str, data: Any, name: str) -> dict[str, Any] | None:
    media_type = _IMAGE_ALIASES.get(str(media_type or "").strip().lower(),
                                    str(media_type or "").strip().lower())
    if not media_type or data in (None, ""):
        return None
    encoded = base64.b64encode(data).decode("ascii") if isinstance(data, bytes) else str(data)
    return {
        "kind": _kind_for(media_type),
        "mediaType": media_type,
        "base64": encoded,
        "byteSize": (len(encoded) * 3) // 4,
        "name": name,
    }


def collect_media(result: Any) -> list[dict[str, Any]]:
    """Everything in an MCP tool result that is not plain text, as raw base64.

    Images, audio and embedded binary resources all arrive here. Whether any of them
    can be shown is a separate question, answered per connection by ``build_tool_result``.
    """

    media: list[dict[str, Any]] = []
    for block in list(getattr(result, "content", ()) or ()):
        block_type = str(getattr(block, "type", ""))
        if block_type in {"image", "audio"}:
            entry = _entry(
                str(getattr(block, "mimeType", "") or getattr(block, "mime_type", "")),
                getattr(block, "data", None),
                "",
            )
        elif block_type == "resource":
            resource = getattr(block, "resource", None)
            blob = getattr(resource, "blob", None)
            if blob is None:
                continue
            uri = str(getattr(resource, "uri", "") or "")
            entry = _entry(
                str(getattr(resource, "mimeType", "") or getattr(resource, "mime_type", "")),
                blob,
                uri.rsplit("/", 1)[-1],
            )
        else:
            continue
        if entry is not None:
            media.append(entry)
    return media


def _anthropic_block(item: Mapping[str, Any]) -> dict[str, Any]:
    if item["kind"] == "image":
        return {"type": "image",
                "source": {"type": "base64", "media_type": item["mediaType"],
                           "data": item["base64"]}}
    if item["mediaType"] == "text/plain":
        # Anthropic's plain-text document source carries the text itself, not
        # base64 (Base64PDFSourceParam is PDF-only); a base64 text/plain block is a
        # 400 for the whole turn.
        text = base64.b64decode(item["base64"]).decode("utf-8", errors="replace")
        block: dict[str, Any] = {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": text},
        }
    else:
        block = {
            "type": "document",
            "source": {"type": "base64", "media_type": item["mediaType"], "data": item["base64"]},
        }
    if item.get("name"):
        block["title"] = str(item["name"])[:200]
    return block


def _openai_block(item: Mapping[str, Any]) -> dict[str, Any]:
    url = "data:%s;base64,%s" % (item["mediaType"], item["base64"])
    if item["kind"] == "image":
        # The chat-style shape: langchain-core keeps it, langchain-openai turns it
        # into the Responses API's input_image. The Responses shape does neither.
        return {"type": "image_url", "image_url": {"url": url}}
    file_block: dict[str, Any] = {"file_data": url}
    file_block["filename"] = str(item.get("name") or "document.pdf")[:200]
    return {"type": "file", "file": file_block}


_BUILDERS = {
    "anthropic-messages": _anthropic_block,
    "openai-responses": _openai_block,
}


def _unsendable_reason(item: Mapping[str, Any], protocol: str) -> str:
    """Why one item is not being shown -- said in terms the model can act on."""

    kind, media_type = item["kind"], item["mediaType"]
    if int(item.get("byteSize") or 0) > MAX_INLINE_ITEM_BYTES:
        return ("%s is %.1f MB, over the %.1f MB this connection will carry in one tool "
                "result" % (media_type, item["byteSize"] / 1e6, MAX_INLINE_ITEM_BYTES / 1e6))
    allowed = protocol_media(protocol).get(kind, frozenset())
    if not allowed:
        return "this connection cannot carry %s content in a tool result" % kind
    return ("this connection takes %s as %s, not %s"
            % (kind, ", ".join(sorted(allowed)), media_type))


def build_tool_result(text: str, media: Sequence[Mapping[str, Any]],
                      protocol: str, *, chat_image_companion: bool = False) -> str | list[dict[str, Any]]:
    """The tool result to hand back, inlining what this connection can actually carry.

    Returns a plain string when there is nothing to inline, so a text-only result is
    byte-for-byte what it always was. Otherwise returns provider-shaped content blocks,
    plus a line naming anything that had to be left out and why -- never a silent drop.
    """

    if not media:
        return text

    protocol = str(protocol or "").strip().lower()
    companion = chat_image_companion and protocol in {"openai-chat", "azure-openai"}
    # Chat image blocks are retained internally until model middleware moves them
    # into a reference-only user message after the complete tool-result batch.
    # They must never be serialized inside ChatCompletionToolMessageParam.
    builder = _openai_block if companion else _BUILDERS.get(protocol)
    usable: list[Mapping[str, Any]] = []
    skipped: list[str] = []
    spent = 0
    for item in media:
        size = int(item.get("byteSize") or 0)
        supported = (item["kind"] == "image" and item["mediaType"] in
                     PROTOCOL_MEDIA["openai-responses"]["image"]) if companion else carries(
                         protocol, item["kind"], item["mediaType"])
        if builder is None or not supported:
            skipped.append("%s: %s" % (item["mediaType"], _unsendable_reason(item, protocol)))
            continue
        if size > MAX_INLINE_ITEM_BYTES or spent + size > MAX_INLINE_TOTAL_BYTES:
            skipped.append("%s: %s" % (item["mediaType"],
                                       "the result's inline budget is spent"
                                       if size <= MAX_INLINE_ITEM_BYTES
                                       else _unsendable_reason(item, protocol)))
            continue
        usable.append(item)
        spent += size

    note = ""
    if skipped:
        note = ("\n\n[%d attachment(s) were produced but not shown -- %s. Do not describe "
                "content you have not received. Host modelImage.included only records MCP "
                "inclusion, not delivery on this connection. For a byte/format limit, request "
                "a smaller preview or convert it with a tool you have registered. If this connection cannot carry "
                "images, repeating the image tool cannot help: use an image-capable connection "
                "for visual review, and continue available Shader/binding analysis separately.]"
                % (len(skipped), "; ".join(skipped[:4])))
    if not usable:
        return text + note

    blocks: list[dict[str, Any]] = [{"type": "text", "text": text + note}]
    blocks.extend(builder(item) for item in usable)  # type: ignore[misc]
    return blocks


__all__ = [
    "MAX_INLINE_ITEM_BYTES",
    "MAX_INLINE_TOTAL_BYTES",
    "PROTOCOL_MEDIA",
    "build_tool_result",
    "carries",
    "collect_media",
    "protocol_media",
]
