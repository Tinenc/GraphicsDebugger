#!/usr/bin/env python3
"""Apply and verify reviewed ReplayLab translations in qrenderdoc's TS catalog."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape


CONTEXT_RE = re.compile(
    r"<context>\s*<name>ReplayLabProjects</name>.*?</context>", re.DOTALL
)
MESSAGE_RE = re.compile(r"<message>.*?</message>", re.DOTALL)
TRANSLATION_RE = re.compile(r"<translation(?:\s+[^>]*)?>.*?</translation>", re.DOTALL)


def element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext())


def load_mapping(path: pathlib.Path) -> tuple[str, dict[str, str]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    context = value.get("context")
    translations = value.get("translations")
    if context != "ReplayLabProjects" or not isinstance(translations, dict):
        raise ValueError(f"{path}: invalid ReplayLab translation mapping")
    if any(not isinstance(source, str) or not isinstance(target, str) or not target
           for source, target in translations.items()):
        raise ValueError(f"{path}: every ReplayLab translation must be non-empty text")
    return context, translations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", type=pathlib.Path)
    parser.add_argument("--translations", required=True, type=pathlib.Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    try:
        unused_context, translations = load_mapping(args.translations)
        catalog_text = args.catalog.read_text(encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(exc, file=sys.stderr)
        return 1

    context_match = CONTEXT_RE.search(catalog_text)
    if context_match is None:
        print("ReplayLabProjects context is missing from the TS catalog", file=sys.stderr)
        return 1

    found: set[str] = set()
    errors: list[str] = []

    def update_message(match: re.Match[str]) -> str:
        block = match.group(0)
        try:
            message = ET.fromstring(block)
        except ET.ParseError as exc:
            errors.append(f"Cannot parse ReplayLab TS message: {exc}")
            return block
        source = element_text(message.find("source"))
        if source not in translations:
            errors.append(f"Missing reviewed ReplayLab translation: {source!r}")
            return block
        found.add(source)
        expected = translations[source]
        translation = message.find("translation")
        actual = element_text(translation)
        unfinished = translation is None or translation.get("type") == "unfinished"
        if args.check:
            if unfinished or actual != expected:
                errors.append(
                    f"ReplayLab translation is stale for {source!r}: {actual!r}"
                )
            return block
        replacement = f"<translation>{escape(expected)}</translation>"
        if not TRANSLATION_RE.search(block):
            errors.append(f"ReplayLab TS message lacks a translation element: {source!r}")
            return block
        return TRANSLATION_RE.sub(replacement, block, count=1)

    updated_context = MESSAGE_RE.sub(update_message, context_match.group(0))
    extras = sorted(set(translations) - found)
    if extras:
        errors.extend(f"Reviewed translation has no extracted source: {source!r}" for source in extras)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    if not args.check:
        updated = (
            catalog_text[:context_match.start()]
            + updated_context
            + catalog_text[context_match.end():]
        )
        import os
        import tempfile
        descriptor, temporary = tempfile.mkstemp(prefix=args.catalog.name + ".", suffix=".tmp", dir=str(args.catalog.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(updated)
            os.replace(temporary, args.catalog)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(f"ReplayLab catalog: {len(found)} reviewed translations current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
