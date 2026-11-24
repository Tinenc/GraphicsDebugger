#!/usr/bin/env python3
"""Quality gates for QRenderDoc Qt translations and newly-added UI literals.

The glossary file uses JSON syntax, which is a strict subset of YAML. Keeping the
parser in Python's standard library makes this check usable on developer machines
and in CI without installing PyYAML.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import html.parser
import json
import os
import pathlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Iterable, Sequence


PLACEHOLDER_RE = re.compile(r"%(?:L?[1-9][0-9]*|L?n)")
FILE_GLOB_RE = re.compile(r"\*\.[A-Za-z0-9?*]+")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[/\\]")
IGNORE_RE = re.compile(r"i18n-ignore:\s*(\S.{5,})", re.IGNORECASE)
BARE_LIT_RE = re.compile(r'\blit\s*\(\s*"(?:[^"\\]|\\.)*[A-Za-z]')
RAW_UI_CALL_RE = re.compile(
    r'\b(?:setText|setToolTip|setStatusTip|setWhatsThis|setWindowTitle|'
    r'setPlaceholderText|setTitle|addItem|insertItem|setHeaderData)\s*\(\s*"'
)
QSTRING_UI_CALL_RE = re.compile(
    r'\b(?:setText|setToolTip|setStatusTip|setWhatsThis|setWindowTitle|'
    r'setPlaceholderText|setTitle|addItem|insertItem|setHeaderData)\s*\(\s*QStringLiteral\s*\('
)
UI_NOTR_RE = re.compile(r'<string\b[^>]*\bnotr\s*=\s*["\']true["\'][^>]*>\s*[^<\s]', re.I)
SET_STAGES_CALL_RE = re.compile(
    r"\bui->pipeFlow->setStages\s*\(\s*\{(?P<abbreviations>[^{}]*)\}\s*,\s*"
    r"\{(?P<names>[^{}]*)\}\s*\)\s*;",
    re.S,
)
SET_STAGES_ANY_RE = re.compile(r"\bui->pipeFlow->setStages\s*\(")
STAGE_LITERAL_RE = re.compile(r'\blit\s*\(\s*"([A-Z][A-Z0-9.]*)"\s*\)')
TRANSLATED_STAGE_NAME_RE = re.compile(
    r'\btr\s*\(\s*"((?:[^"\\]|\\.)*)"\s*'
    r'(?:,\s*"(?:[^"\\]|\\.)*"\s*)?\)'
)
SET_STAGE_NAME_RE = re.compile(
    r'\bui->pipeFlow->setStageName\s*\(\s*[^,\r\n]+\s*,\s*'
    r'lit\s*\(\s*"(?P<abbreviation>[A-Z][A-Z0-9.]*)"\s*\)\s*,\s*'
    r'tr\s*\(\s*"(?P<name>(?:[^"\\]|\\.)*)"\s*'
    r'(?:,\s*"(?:[^"\\]|\\.)*"\s*)?\)\s*\)\s*;',
    re.S,
)
SET_STAGE_NAME_ANY_RE = re.compile(r"\bui->pipeFlow->setStageName\s*\(")
CPP_COMMENT_RE = re.compile(r"//[^\r\n]*|/\*.*?\*/", re.S)
PIPELINE_ABBREVIATION_DRAW_RE = re.compile(
    r"const\s+QString\s*&\s*(?P<variable>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"m_StageAbbrevs\s*\[\s*i\s*\]\s*;.{0,1200}?"
    r"\bp\.drawText\s*\(\s*boxrect\s*,\s*(?P=variable)\s*,",
    re.S,
)
HTML_MARKUP_RE = re.compile(
    r"</?(?:html|head|body|p|br|span|a|b|i|u|strong|em|style|script|code|pre|"
    r"table|tr|td|th|ul|ol|li|div|meta|byte)\b",
    re.I,
)
PROTECTED_FRAGMENT_PATTERNS = (
    ("HTML style block", re.compile(r"<style\b[^>]*>.*?</style>", re.I | re.S)),
    ("HTML script block", re.compile(r"<script\b[^>]*>.*?</script>", re.I | re.S)),
    ("HTML code block", re.compile(r"<code\b[^>]*>.*?</code>", re.I | re.S)),
    ("HTML preformatted block", re.compile(r"<pre\b[^>]*>.*?</pre>", re.I | re.S)),
    (
        "HTML monospace span",
        re.compile(
            r'<span\b(?=[^>]*\bfont-family\s*:\s*["\']?(?:monospace|[^;"\']*Courier))[^>]*>.*?</span>',
            re.I | re.S,
        ),
    ),
    ("backtick code", re.compile(r"`[^`\r\n]+`")),
    ("command-line option", re.compile(r"(?<![A-Za-z0-9_-])--[A-Za-z0-9][A-Za-z0-9_-]*")),
)


@dataclasses.dataclass(frozen=True)
class Message:
    context: str
    source: str
    comment: str
    translations: tuple[str, ...]
    translation_type: str
    numerus: bool
    locations: tuple[str, ...]

    @property
    def active(self) -> bool:
        return self.translation_type not in {"obsolete", "vanished"}

    @property
    def finished(self) -> bool:
        return self.active and self.translation_type != "unfinished"

    @property
    def key(self) -> tuple[str, str, str, bool]:
        return (self.context, self.source, self.comment, self.numerus)


@dataclasses.dataclass
class Result:
    errors: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    if not list(element):
        return element.text or ""
    pieces = [element.text or ""]
    for child in element:
        pieces.append(ET.tostring(child, encoding="unicode"))
    return "".join(pieces)


def parse_catalog(path: pathlib.Path) -> tuple[ET.Element, list[Message]]:
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"cannot parse TS catalog {path}: {exc}") from exc

    root = tree.getroot()
    if root.tag != "TS":
        raise ValueError(f"{path}: expected <TS> root, found <{root.tag}>")

    messages: list[Message] = []
    for context_element in root.findall("context"):
        context = context_element.findtext("name", default="")
        for message_element in context_element.findall("message"):
            source = element_text(message_element.find("source"))
            comment = element_text(message_element.find("comment"))
            translation = message_element.find("translation")
            translation_type = "missing" if translation is None else translation.get("type", "finished")
            numerus = message_element.get("numerus") == "yes"
            if translation is None:
                forms: tuple[str, ...] = ()
            elif numerus:
                forms = tuple(element_text(form) for form in translation.findall("numerusform"))
            else:
                forms = (element_text(translation),)
            locations = tuple(
                location.get("filename", "") for location in message_element.findall("location")
                if location.get("filename")
            )
            messages.append(
                Message(context, source, comment, forms, translation_type, numerus, locations)
            )
    return root, messages


def load_glossary(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot parse glossary {path}: {exc}") from exc


def _validated_repo_relative(value: object, label: str, *, directory: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty repository-relative path")
    normalized = value.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        raise ValueError(f"{label} must be a non-empty repository-relative path")
    pure = pathlib.PurePosixPath(normalized)
    if pure.is_absolute() or WINDOWS_ABSOLUTE_RE.match(normalized) or ".." in pure.parts:
        raise ValueError(f"{label} must stay inside the repository: {value!r}")
    if directory and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def load_source_manifest(path: pathlib.Path) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot parse i18n source manifest {path}: {exc}") from exc

    if manifest.get("schema_version") != 1:
        raise ValueError(f"{path}: unsupported source manifest schema")

    root = _validated_repo_relative(manifest.get("root"), "source manifest root")
    extensions = manifest.get("extensions")
    if not isinstance(extensions, list) or not extensions:
        raise ValueError(f"{path}: source manifest extensions must be a non-empty list")
    normalized_extensions: list[str] = []
    for extension in extensions:
        if not isinstance(extension, str) or not re.fullmatch(r"\.[A-Za-z0-9+]+", extension):
            raise ValueError(f"{path}: invalid source extension {extension!r}")
        normalized_extensions.append(extension.lower())

    def prefixes(name: str) -> list[str]:
        values = manifest.get(name, [])
        if not isinstance(values, list):
            raise ValueError(f"{path}: {name} must be a list")
        return [
            _validated_repo_relative(value, f"source manifest {name} entry", directory=True)
            for value in values
        ]

    return {
        "schema_version": 1,
        "root": root,
        "extensions": tuple(dict.fromkeys(normalized_extensions)),
        "excluded_prefixes": tuple(prefixes("excluded_prefixes")),
        "included_prefixes": tuple(prefixes("included_prefixes")),
    }


def discover_source_files(repo: pathlib.Path, manifest: dict) -> list[pathlib.Path]:
    root = repo / manifest["root"]
    if not root.is_dir():
        raise ValueError(f"i18n source root does not exist: {root}")

    extensions = set(manifest["extensions"])
    excluded = manifest["excluded_prefixes"]
    included = manifest["included_prefixes"]
    files: list[pathlib.Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        relative = path.relative_to(repo).as_posix()
        is_excluded = any(relative.startswith(prefix) for prefix in excluded)
        is_reincluded = any(relative.startswith(prefix) for prefix in included)
        if is_excluded and not is_reincluded:
            continue
        files.append(path)
    return sorted(files, key=lambda candidate: candidate.relative_to(repo).as_posix())


class HtmlAudit(html.parser.HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.counts: collections.Counter[tuple[str, str]] = collections.Counter()
        self.attributes: collections.Counter[tuple[str, str, str]] = collections.Counter()
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self.counts[("tag", tag)] += 1
        # Markup attributes are program data, not natural-language prose. Translating CSS,
        # URLs, element names, dimensions, or other attribute values can silently break a rich
        # text label while leaving its tag structure superficially valid.
        for name, value in attrs:
            self.attributes[(tag, name.lower(), value or "")] += 1
        if tag not in self.VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        tag = tag.lower()
        if tag not in self.VOID_TAGS and self.stack and self.stack[-1] == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.VOID_TAGS:
            return
        if not self.stack:
            self.errors.append(f"unexpected closing </{tag}>")
            return
        expected = self.stack.pop()
        if expected != tag:
            self.errors.append(f"closing </{tag}> does not match <{expected}>")

    def finish(self) -> None:
        if self.stack:
            self.errors.append("unclosed tag(s): " + ", ".join(f"<{tag}>" for tag in self.stack))


def html_audit(text: str) -> HtmlAudit:
    audit = HtmlAudit()
    try:
        audit.feed(text)
        audit.close()
    except Exception as exc:  # HTMLParser subclasses can surface malformed entities.
        audit.errors.append(str(exc))
    audit.finish()
    return audit


def message_label(message: Message) -> str:
    source = message.source.replace("\n", "\\n")
    if len(source) > 72:
        source = source[:69] + "..."
    suffix = f" / {message.comment}" if message.comment else ""
    return f"[{message.context}] {source!r}{suffix}"


def token_count(text: str, token: str) -> int:
    expression = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])")
    return len(expression.findall(text))


def qt_accelerator_count(text: str) -> int:
    without_escaped = text.replace("&&", "")
    # RenderDoc's compact Source&Dest resource label is a conjunction, not a mnemonic. Do not
    # generalise this by word length: real menu mnemonics include forms such as `Sa&ve`.
    without_escaped = without_escaped.replace("Source&Dest", "SourceDest")
    # A conjunction written as "A & B" is ordinary prose and may naturally become “和/与”.
    # Mnemonics have a non-whitespace character immediately after the ampersand (`&Open`,
    # `E&xit`).
    accelerators = len(re.findall(r"&(?![A-Za-z0-9#]+;)(?=\S)", without_escaped))
    return accelerators


def lint_finished_translation(message: Message, translated: str, glossary: dict, result: Result) -> None:
    label = message_label(message)
    if not translated.strip():
        result.error(f"{label}: finished translation is empty")
        return

    source_placeholders = collections.Counter(PLACEHOLDER_RE.findall(message.source))
    translated_placeholders = collections.Counter(PLACEHOLDER_RE.findall(translated))
    if source_placeholders != translated_placeholders:
        result.error(
            f"{label}: placeholder mismatch: source={dict(source_placeholders)}, "
            f"translation={dict(translated_placeholders)}"
        )

    if message.source.count("\n") != translated.count("\n"):
        result.error(f"{label}: newline count differs from source")

    source_edges = (
        re.match(r"^[ \t]*", message.source).group(0),
        re.search(r"[ \t]*$", message.source).group(0),
    )
    translated_edges = (
        re.match(r"^[ \t]*", translated).group(0),
        re.search(r"[ \t]*$", translated).group(0),
    )
    if source_edges != translated_edges:
        result.error(f"{label}: leading/trailing horizontal whitespace differs from source")

    source_is_html = HTML_MARKUP_RE.search(message.source) is not None
    translated_is_html = HTML_MARKUP_RE.search(translated) is not None
    source_html = html_audit(message.source)
    translated_html = html_audit(translated)
    if source_is_html or translated_is_html:
        if source_html.errors:
            result.warn(
                f"{label}: source has incomplete/malformed HTML: {'; '.join(source_html.errors)}"
            )
        if translated_html.errors and translated_html.errors != source_html.errors:
            result.error(f"{label}: translation has malformed HTML: {'; '.join(translated_html.errors)}")
        if source_html.counts != translated_html.counts:
            result.error(
                f"{label}: HTML tag set/count differs from source "
                f"({dict(source_html.counts)} != {dict(translated_html.counts)})"
            )
        if source_html.attributes != translated_html.attributes:
            result.error(f"{label}: HTML attributes must be preserved exactly")

    # Ampersands in rich text may be HTML entities or URL query separators. For ordinary Qt
    # labels they encode keyboard accelerators (`&Open`) or a literal ampersand (`&&`), and both
    # forms must remain functional after translation.
    if not source_is_html and qt_accelerator_count(message.source) != qt_accelerator_count(translated):
        result.error(f"{label}: Qt accelerator count differs from source")

    for fragment_name, pattern in PROTECTED_FRAGMENT_PATTERNS:
        source_fragments = collections.Counter(pattern.findall(message.source))
        if not source_fragments:
            continue
        translated_fragments = collections.Counter(pattern.findall(translated))
        if source_fragments != translated_fragments:
            result.error(f"{label}: {fragment_name} must be preserved exactly")

    for token in glossary.get("protected_tokens", []):
        source_count = token_count(message.source, token)
        if source_count and token_count(translated, token) != source_count:
            result.error(f"{label}: protected token {token!r} must be preserved ({source_count} occurrence(s))")

    for pattern_spec in glossary.get("protected_patterns", []):
        try:
            pattern = re.compile(pattern_spec["regex"])
        except (KeyError, re.error) as exc:
            result.error(f"glossary protected pattern is invalid: {pattern_spec!r}: {exc}")
            continue
        source_matches = collections.Counter(pattern.findall(message.source))
        translated_matches = collections.Counter(pattern.findall(translated))
        if source_matches != translated_matches:
            result.error(
                f"{label}: protected {pattern_spec.get('name', 'pattern')} token(s) differ: "
                f"source={dict(source_matches)}, translation={dict(translated_matches)}"
            )

    source_globs = collections.Counter(FILE_GLOB_RE.findall(message.source))
    if source_globs:
        translated_globs = collections.Counter(FILE_GLOB_RE.findall(translated))
        if source_globs != translated_globs:
            result.error(
                f"{label}: file-filter wildcards differ: source={dict(source_globs)}, "
                f"translation={dict(translated_globs)}"
            )
        if message.source.count(";;") != translated.count(";;"):
            result.error(f"{label}: file-filter ';;' separator count differs from source")

    for rule in glossary.get("lint_terms", []):
        if message.source == rule.get("source") and translated not in rule.get("allowed", []):
            result.error(
                f"{label}: glossary requires one of {rule.get('allowed', [])!r}, got {translated!r}"
            )

    for rule in glossary.get("forbidden_rules", []):
        try:
            source_matches = re.fullmatch(rule.get("source_regex", r"(?!)"), message.source) is not None
            context_matches = re.search(rule.get("context_regex", r".*"), message.context) is not None
            translation_matches = re.search(rule.get("translation_regex", r"(?!)"), translated) is not None
        except re.error as exc:
            result.error(f"glossary forbidden rule is invalid: {rule!r}: {exc}")
            continue
        if source_matches and context_matches and translation_matches:
            result.error(f"{label}: {rule.get('message', 'forbidden glossary translation')}")


def lint_catalog(path: pathlib.Path, glossary: dict, require_complete: bool) -> tuple[Result, list[Message]]:
    result = Result()
    try:
        root, messages = parse_catalog(path)
    except ValueError as exc:
        result.error(str(exc))
        return result, []

    if root.get("language") != glossary.get("locale"):
        result.error(
            f"{path}: TS language={root.get('language')!r}, expected {glossary.get('locale')!r}"
        )
    if root.get("sourcelanguage") not in {"en", "en_US", "en_GB"}:
        result.error(f"{path}: TS sourcelanguage must be English, got {root.get('sourcelanguage')!r}")

    seen: set[tuple[str, str, str, bool]] = set()
    unfinished = 0
    finished = 0
    for message in messages:
        if not message.active:
            continue
        if message.key in seen:
            result.error(f"{message_label(message)}: duplicate active context/source/comment key")
        seen.add(message.key)
        for location in message.locations:
            if pathlib.PurePath(location).is_absolute() or WINDOWS_ABSOLUTE_RE.match(location):
                result.error(f"{message_label(message)}: absolute TS location is not reproducible: {location}")
        if message.translation_type == "missing":
            result.error(f"{message_label(message)}: missing <translation> element")
            continue
        if not message.finished:
            unfinished += 1
            continue
        finished += 1
        if not message.translations:
            result.error(f"{message_label(message)}: finished translation has no form")
            continue
        for translated in message.translations:
            lint_finished_translation(message, translated, glossary, result)

    if require_complete and unfinished:
        result.error(f"{path}: {unfinished} active message(s) are still unfinished")
    result.warnings.append(
        f"catalog stats: {len(seen)} active, {finished} finished, {unfinished} unfinished"
    )
    return result, messages


def active_keys(messages: Iterable[Message]) -> set[tuple[str, str, str, bool]]:
    return {message.key for message in messages if message.active}


def compare_catalogs(current: Sequence[Message], generated_path: pathlib.Path, result: Result) -> None:
    try:
        _, generated = parse_catalog(generated_path)
    except ValueError as exc:
        result.error(str(exc))
        return
    current_keys = active_keys(current)
    generated_keys = active_keys(generated)
    added = sorted(generated_keys - current_keys)
    removed = sorted(current_keys - generated_keys)
    if not added and not removed:
        return
    result.error(
        f"translation catalog is out of date: {len(added)} source message(s) missing and "
        f"{len(removed)} stale message(s); run util/i18n/update_translations.ps1"
    )
    for prefix, values in (("missing", added), ("stale", removed)):
        for context, source, comment, _numerus in values[:20]:
            detail = f" / {comment}" if comment else ""
            result.error(f"  {prefix}: [{context}] {source!r}{detail}")
        if len(values) > 20:
            result.error(f"  ... {len(values) - 20} more {prefix} message(s)")


def _initializer_entries(block: str, pattern: re.Pattern[str]) -> tuple[list[str], str]:
    without_comments = CPP_COMMENT_RE.sub("", block)
    values = [match.group(1) for match in pattern.finditer(without_comments)]
    remainder = pattern.sub("", without_comments)
    remainder = re.sub(r"[\s,]", "", remainder)
    return values, remainder


def lint_compact_pipeline_flows(
    repo: pathlib.Path, glossary: dict, manifest: dict, result: Result
) -> None:
    contract = glossary.get("compact_pipeline_flows")
    if not isinstance(contract, dict):
        result.error("glossary is missing compact_pipeline_flows source contract")
        return

    protected = set(glossary.get("protected_tokens", []))
    try:
        renderer_relative = _validated_repo_relative(
            contract.get("renderer"), "compact pipeline renderer"
        )
    except ValueError as exc:
        result.error(str(exc))
        return

    renderer = repo / renderer_relative
    try:
        renderer_text = renderer.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result.error(f"cannot read compact pipeline renderer {renderer}: {exc}")
    else:
        if not PIPELINE_ABBREVIATION_DRAW_RE.search(renderer_text):
            result.error(
                f"{renderer_relative}: compact pipeline nodes must be drawn from "
                "m_StageAbbrevs, never translated stage names"
            )
        if re.search(r"\bp\.drawText\s*\([^;]*m_StageNames", renderer_text, re.S):
            result.error(
                f"{renderer_relative}: m_StageNames must not be passed to drawText for compact nodes"
            )

    viewers = contract.get("viewers")
    if not isinstance(viewers, list) or not viewers:
        result.error("glossary compact_pipeline_flows.viewers must be a non-empty list")
        return

    configured_paths: set[str] = set()
    for viewer in viewers:
        if not isinstance(viewer, dict):
            result.error(f"invalid compact pipeline viewer contract: {viewer!r}")
            continue
        try:
            relative = _validated_repo_relative(
                viewer.get("path"), "compact pipeline viewer path"
            )
        except ValueError as exc:
            result.error(str(exc))
            continue
        if relative in configured_paths:
            result.error(f"duplicate compact pipeline viewer contract: {relative}")
            continue
        configured_paths.add(relative)

        expected_sequences_raw = viewer.get("stage_sequences")
        expected_dynamic_raw = viewer.get("dynamic_abbreviations", [])
        if (
            not isinstance(expected_sequences_raw, list)
            or not expected_sequences_raw
            or not all(isinstance(sequence, list) and sequence for sequence in expected_sequences_raw)
            or not isinstance(expected_dynamic_raw, list)
        ):
            result.error(f"{relative}: invalid compact pipeline stage contract")
            continue
        expected_sequences = [tuple(str(token) for token in sequence) for sequence in expected_sequences_raw]
        expected_dynamic = [str(token) for token in expected_dynamic_raw]
        contract_tokens = {token for sequence in expected_sequences for token in sequence}
        contract_tokens.update(expected_dynamic)
        missing_protection = sorted(contract_tokens - protected)
        if missing_protection:
            result.error(
                f"{relative}: compact stage abbreviation(s) missing from protected_tokens: "
                f"{', '.join(missing_protection)}"
            )

        path = repo / relative
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            result.error(f"cannot read compact pipeline viewer {path}: {exc}")
            continue

        calls = list(SET_STAGES_CALL_RE.finditer(text))
        raw_call_count = len(SET_STAGES_ANY_RE.findall(text))
        if len(calls) != raw_call_count:
            result.error(
                f"{relative}: every pipeFlow->setStages call must use a literal abbreviation "
                "initializer followed by a translated full-name initializer"
            )

        actual_sequences: list[tuple[str, ...]] = []
        for call_index, call in enumerate(calls, 1):
            abbreviations, bad_abbreviations = _initializer_entries(
                call.group("abbreviations"), STAGE_LITERAL_RE
            )
            names, bad_names = _initializer_entries(call.group("names"), TRANSLATED_STAGE_NAME_RE)
            if bad_abbreviations:
                result.error(
                    f"{relative}: setStages call {call_index} contains a non-literal compact "
                    f"abbreviation expression: {bad_abbreviations!r}"
                )
            if bad_names:
                result.error(
                    f"{relative}: setStages call {call_index} contains a stage name that is not "
                    f"wrapped in tr(): {bad_names!r}"
                )
            if len(abbreviations) != len(names):
                result.error(
                    f"{relative}: setStages call {call_index} has {len(abbreviations)} abbreviation(s) "
                    f"but {len(names)} translated name(s)"
                )
            for abbreviation, name in zip(abbreviations, names):
                if name == abbreviation or name in protected:
                    result.error(
                        f"{relative}: compact node {abbreviation} needs a translated explanatory "
                        "stage name in the second initializer"
                    )
            actual_sequences.append(tuple(abbreviations))

        if actual_sequences != expected_sequences:
            result.error(
                f"{relative}: compact stage sequence differs from glossary contract: "
                f"actual={actual_sequences!r}, expected={expected_sequences!r}"
            )

        dynamic_calls = list(SET_STAGE_NAME_RE.finditer(text))
        raw_dynamic_count = len(SET_STAGE_NAME_ANY_RE.findall(text))
        if len(dynamic_calls) != raw_dynamic_count:
            result.error(
                f"{relative}: every pipeFlow->setStageName call must use lit(\"ABBREV\") "
                "and tr(\"Full Name\")"
            )
        actual_dynamic = [match.group("abbreviation") for match in dynamic_calls]
        for match in dynamic_calls:
            if match.group("name") == match.group("abbreviation") or match.group("name") in protected:
                result.error(
                    f"{relative}: dynamic compact node {match.group('abbreviation')} needs a "
                    "translated explanatory stage name"
                )
        if actual_dynamic != expected_dynamic:
            result.error(
                f"{relative}: dynamic compact stage abbreviations differ from glossary contract: "
                f"actual={actual_dynamic!r}, expected={expected_dynamic!r}"
            )

    discovered_viewers: set[str] = set()
    try:
        source_files = discover_source_files(repo, manifest)
    except ValueError as exc:
        result.error(str(exc))
        return
    for path in source_files:
        if path.suffix.lower() not in {".cpp", ".cc", ".cxx"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if SET_STAGES_ANY_RE.search(text):
            discovered_viewers.add(path.relative_to(repo).as_posix())
    unconfigured = sorted(discovered_viewers - configured_paths)
    if unconfigured:
        result.error(
            "compact pipeline viewer(s) missing from glossary contract: " + ", ".join(unconfigured)
        )


def git_diff(repo: pathlib.Path, base: str | None, staged: bool) -> str:
    command = ["git", "-C", str(repo), "diff", "--no-ext-diff", "--unified=0", "--diff-filter=ACMR"]
    if staged:
        command.append("--cached")
    elif base:
        command.append(base)
    command.extend(["--", "qrenderdoc"])
    completed = subprocess.run(command, check=False, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode:
        raise ValueError(f"git diff failed ({completed.returncode}): {completed.stderr.strip()}")
    return completed.stdout


def added_diff_lines(diff: str) -> Iterable[tuple[str, int, str]]:
    current_file = ""
    new_line = 0
    for raw_line in diff.splitlines():
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]
            continue
        if raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw_line)
            if match:
                new_line = int(match.group(1))
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            yield current_file, new_line, raw_line[1:]
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            continue
        elif current_file and not raw_line.startswith(("diff ", "index ", "--- ")):
            new_line += 1


def has_ignore_reason(repo: pathlib.Path, filename: str, line_number: int, line: str) -> bool:
    if IGNORE_RE.search(line):
        return True
    path = repo / filename
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    if line_number > 1 and line_number - 2 < len(lines):
        return IGNORE_RE.search(lines[line_number - 2]) is not None
    return False


def source_line_reasons(path: pathlib.Path, line: str) -> list[str]:
    reasons: list[str] = []
    suffix = path.suffix.lower()
    if suffix in {".cpp", ".cc", ".cxx", ".h", ".hpp"}:
        if BARE_LIT_RE.search(line):
            reasons.append("bare lit(\"...\")")
        if RAW_UI_CALL_RE.search(line):
            reasons.append("raw string passed to a UI text setter")
        if QSTRING_UI_CALL_RE.search(line):
            reasons.append("QStringLiteral passed to a UI text setter")
    elif suffix == ".ui" and UI_NOTR_RE.search(line):
        reasons.append(".ui string marked notr=true")
    return reasons


def source_fingerprint(relative_path: str, line: str, reasons: Sequence[str]) -> str:
    normalized_line = re.sub(r"\s+", " ", line.strip())
    payload = "\0".join((relative_path.replace("\\", "/"), normalized_line, "|".join(reasons)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scan_source_tree(
    repo: pathlib.Path, manifest: dict | None = None
) -> dict[str, list[tuple[str, int, str, tuple[str, ...]]]]:
    if manifest is None:
        manifest = load_source_manifest(repo / "util" / "i18n" / "source_manifest.json")
    findings: dict[str, list[tuple[str, int, str, tuple[str, ...]]]] = collections.defaultdict(list)
    for path in discover_source_files(repo, manifest):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        relative_path = path.relative_to(repo).as_posix()
        for index, line in enumerate(lines):
            reasons = source_line_reasons(path, line)
            if not reasons:
                continue
            previous = lines[index - 1] if index else ""
            if IGNORE_RE.search(line) or IGNORE_RE.search(previous):
                continue
            fingerprint = source_fingerprint(relative_path, line, reasons)
            findings[fingerprint].append((relative_path, index + 1, line.strip(), tuple(reasons)))
    return findings


def write_source_baseline(
    repo: pathlib.Path, path: pathlib.Path, manifest: dict | None = None
) -> None:
    findings = scan_source_tree(repo, manifest)
    data = {
        "schema_version": 1,
        "description": (
            "Legacy untranslated source literals present when the i18n gate was introduced. "
            "Do not add entries for new code; use translation APIs or an i18n-ignore reason."
        ),
        "entries": {fingerprint: len(items) for fingerprint, items in sorted(findings.items())},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path}: {sum(data['entries'].values())} legacy occurrence(s)")


def lint_source_tree(
    repo: pathlib.Path,
    baseline_path: pathlib.Path,
    result: Result,
    manifest: dict | None = None,
) -> None:
    try:
        baseline_data = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline_entries = collections.Counter(
            {str(key): int(value) for key, value in baseline_data.get("entries", {}).items()}
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        result.error(f"cannot parse source-literal baseline {baseline_path}: {exc}")
        return

    try:
        findings = scan_source_tree(repo, manifest)
    except ValueError as exc:
        result.error(str(exc))
        return
    current_entries = collections.Counter({fingerprint: len(items) for fingerprint, items in findings.items()})
    new_count = 0
    for fingerprint, count in current_entries.items():
        surplus = count - baseline_entries.get(fingerprint, 0)
        if surplus <= 0:
            continue
        new_count += surplus
        for filename, line_number, _line, reasons in findings[fingerprint][-surplus:]:
            result.error(
                f"{filename}:{line_number}: {', '.join(reasons)} bypasses translation extraction; "
                "use tr()/QCoreApplication::translate(), or add `i18n-ignore: <specific reason>` "
                "on this or the preceding line for a non-user-facing identifier"
            )
    legacy_count = sum(min(count, baseline_entries.get(key, 0)) for key, count in current_entries.items())
    removed_count = sum((baseline_entries - current_entries).values())
    result.warnings.append(
        f"source-literal gate: {legacy_count} baselined legacy occurrence(s), "
        f"{new_count} new, {removed_count} legacy occurrence(s) removed"
    )


def lint_added_source_literals(repo: pathlib.Path, base: str | None, staged: bool, result: Result) -> None:
    try:
        diff = git_diff(repo, base, staged)
    except ValueError as exc:
        result.error(str(exc))
        return

    for filename, line_number, line in added_diff_lines(diff):
        if not filename:
            continue
        reasons = source_line_reasons(pathlib.Path(filename), line)
        if reasons and not has_ignore_reason(repo, filename, line_number, line):
            result.error(
                f"{filename}:{line_number}: {', '.join(reasons)} bypasses translation extraction; "
                "use tr()/QCoreApplication::translate(), or add `i18n-ignore: <specific reason>` "
                "on this or the preceding line for a non-user-facing identifier"
            )


def print_result(result: Result) -> None:
    for warning in result.warnings:
        print(f"warning: {warning}")
    for error in result.errors:
        print(f"error: {error}", file=sys.stderr)


def default_repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    repo = default_repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "catalog",
        nargs="?",
        type=pathlib.Path,
        default=repo / "qrenderdoc" / "Translations" / "qrenderdoc_zh_CN.ts",
    )
    parser.add_argument(
        "--glossary",
        type=pathlib.Path,
        default=repo / "qrenderdoc" / "Translations" / "glossary.zh_CN.yaml",
    )
    parser.add_argument("--compare-catalog", type=pathlib.Path, help="compare active message keys with lupdate output")
    parser.add_argument("--require-complete", action="store_true", help="fail if any active translation is unfinished")
    parser.add_argument("--source-root", type=pathlib.Path, help="repository root used for added-source checks")
    parser.add_argument(
        "--source-manifest",
        type=pathlib.Path,
        help="source discovery manifest (defaults to util/i18n/source_manifest.json under --source-root)",
    )
    parser.add_argument("--diff-base", help="git revision to compare against when checking newly-added source lines")
    parser.add_argument("--diff-staged", action="store_true", help="check staged source additions instead of a revision")
    parser.add_argument("--source-baseline", type=pathlib.Path, help="scan the full source tree and reject literals beyond this legacy baseline")
    parser.add_argument("--write-source-baseline", type=pathlib.Path, help="explicitly regenerate the legacy source-literal baseline")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        glossary = load_glossary(args.glossary.resolve())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    catalog_result, messages = lint_catalog(args.catalog.resolve(), glossary, args.require_complete)
    if args.compare_catalog:
        compare_catalogs(messages, args.compare_catalog.resolve(), catalog_result)
    if args.source_root:
        source_root = args.source_root.resolve()
        manifest_path = (
            args.source_manifest.resolve()
            if args.source_manifest
            else source_root / "util" / "i18n" / "source_manifest.json"
        )
        try:
            manifest = load_source_manifest(manifest_path)
        except ValueError as exc:
            catalog_result.error(str(exc))
        else:
            lint_compact_pipeline_flows(source_root, glossary, manifest, catalog_result)
            if args.write_source_baseline:
                try:
                    write_source_baseline(
                        source_root, args.write_source_baseline.resolve(), manifest=manifest
                    )
                except ValueError as exc:
                    catalog_result.error(str(exc))
            if args.source_baseline:
                lint_source_tree(
                    source_root,
                    args.source_baseline.resolve(),
                    catalog_result,
                    manifest=manifest,
                )
            if args.diff_base or args.diff_staged:
                lint_added_source_literals(source_root, args.diff_base, args.diff_staged, catalog_result)
    elif (
        args.diff_base
        or args.diff_staged
        or args.source_baseline
        or args.write_source_baseline
        or args.source_manifest
    ):
        catalog_result.error("source diff/baseline options require --source-root")

    print_result(catalog_result)
    return 1 if catalog_result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
