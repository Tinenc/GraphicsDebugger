"""Bounded shader failure diagnostics without prescribing the agent's workflow."""

from __future__ import annotations

import re
from typing import Any

from .errors import (
    AbiValidationError,
    CatalogValidationError,
    ManifestValidationError,
    ToolConfigurationError,
    ToolExecutionError,
    ToolchainRoutingError,
)


DIAGNOSTIC_SCHEMA = "renderdoc-ultra-shader-recovery-diagnostic/v1"
_MAX_MESSAGE = 4096


def _bounded_message(error: BaseException | str) -> str:
    text = str(error).replace("\x00", "�")
    return text if len(text) <= _MAX_MESSAGE else text[:_MAX_MESSAGE] + "…"


def _contains(message: str, *patterns: str) -> bool:
    return any(re.search(pattern, message, flags=re.IGNORECASE) for pattern in patterns)


def _is_organization_error(error: BaseException | str) -> bool:
    if not isinstance(error, BaseException):
        return False
    return any(
        base.__name__ == "ShaderOrganizationError"
        and base.__module__.endswith("shader_workspace.organization")
        for base in type(error).__mro__
    )


def _direct_cause(error: BaseException | str) -> BaseException | None:
    if not isinstance(error, BaseException):
        return None
    cause = error.__cause__
    if cause is None and not error.__suppress_context__:
        cause = error.__context__
    return cause if isinstance(cause, BaseException) and cause is not error else None


def _classify_organization_failure(
    error: BaseException, message: str
) -> tuple[str, bool, bool]:
    """Classify an organization error without choosing a recovery procedure."""

    cause = _direct_cause(error)
    if _contains(
        message,
        r"capture session changed",
        r"active renderdoc capture does not match",
        r"source changed after",
        r"source changed concurrently",
        r"workspace (?:source |)binding changed",
        r"workspace source hash changed",
        r"not awaiting (?:proposal )?review",
    ):
        return "organization-conflict", True, False
    if _contains(
        message,
        r"organized hlsl proposal",
        r"organization analysis",
        r"organization flowchart",
        r"plain safe mermaid",
        r"analysis must not embed code fences",
        r"proposal source path",
    ):
        return "organization-proposal-invalid", True, True
    if isinstance(cause, AbiValidationError) or _contains(
        message,
        r"abi validation",
        r"replay abi",
        r"shader reflection",
        r"reflection evidence",
        r"compiled interface",
    ):
        return "organization-abi-failed", False, True
    if isinstance(cause, (ToolConfigurationError, ToolExecutionError)) or _contains(
        message,
        r"compile returned",
        r"compile was not bound",
        r"compiler produced",
        r"produced unstable .*executable code",
    ):
        return "organization-compile-failed", False, True
    return "organization-evidence-invalid", False, False


def classify_shader_failure(
    error: BaseException | str,
    *,
    phase: str = "unknown",
) -> dict[str, Any]:
    """Return a stable recovery diagnostic for one failed operation."""

    message = _bounded_message(error)
    kind = type(error).__name__ if isinstance(error, BaseException) else "Diagnostic"
    organization_phase = str(phase or "").startswith("organization-")
    category = "unknown"
    retryable = False
    ai_may_author_source = False

    if isinstance(error, ManifestValidationError) and _contains(
        message,
        r"route catalog bytes changed after creation",
        r"route plan tool registry hash mismatch",
        r"route capability changed after creation",
        r"route hop binding is inconsistent",
    ):
        category = "workspace-toolchain-upgrade-required"
        retryable = True
    elif isinstance(error, (CatalogValidationError, ManifestValidationError)):
        category = "provenance-invalid"
    elif isinstance(error, ToolchainRoutingError):
        if _contains(message, r"unsupported shader format", r"unsupported target api"):
            category = "unsupported-format-or-api"
        elif _contains(message, r"ambiguous", r"equal.*route", r"choose an explicit"):
            category = "route-ambiguous"
        else:
            category = "route-unavailable"
            ai_may_author_source = True
    elif _is_organization_error(error):
        category, retryable, ai_may_author_source = (
            _classify_organization_failure(error, message)
        )
    elif organization_phase and isinstance(
        error, (ToolConfigurationError, ToolExecutionError)
    ):
        category = "organization-compile-failed"
        ai_may_author_source = isinstance(error, ToolExecutionError) and _contains(
            message, r"exited with code", r"process.*failed"
        )
    elif organization_phase and (
        isinstance(error, AbiValidationError)
        or _contains(message, r"abi", r"reflection", r"compiled interface")
    ):
        category = "organization-abi-failed"
        ai_may_author_source = True
    elif (
        str(phase or "").startswith("workspace-prepare")
        and not isinstance(error, ToolExecutionError)
        and _contains(
            message,
            r"workspace_path is not the active project's deterministic path",
            r"deterministic shader workspace escaped the active project",
            r"existing workspace is outside the active project workspace root",
        )
    ):
        category = "workspace-path-policy-violation"
    elif (
        str(phase or "").startswith("workspace-prepare")
        and not isinstance(error, ToolExecutionError)
        and _contains(
            message,
            r"\[errno 2\].*no such file or directory.*[\\/]blobs[\\/].*\.tmp",
            r"filename or extension is too long.*[\\/]blobs[\\/]",
            r"path too long.*[\\/]blobs[\\/]",
        )
    ):
        # A catalog export can reach the embedded Python/Win32 MAX_PATH ABI
        # before a decompiler or compiler is launched.  This is a recoverable
        # workspace-placement failure: keep the exact Shader binding, ask the
        # Host for its compact managed path, and retry without user input.
        category = "workspace-path-conflict"
        retryable = True
    elif (
        str(phase or "").startswith("workspace-prepare")
        and not isinstance(error, ToolExecutionError)
        and _contains(message, r"workspace (?:path )?already exists")
    ):
        category = "workspace-path-conflict"
        retryable = True
    elif (
        str(phase or "").startswith("workspace-prepare")
        and not isinstance(error, ToolExecutionError)
        and _contains(
            message,
            r"renderdoc bridge returned an invalid catalog result",
            r"renderdoc bridge did not return catalogpath",
        )
    ):
        category = "capture-catalog-export-failed"
        retryable = True
    elif (
        str(phase or "").startswith("workspace-prepare")
        and not isinstance(error, ToolExecutionError)
        and _contains(
            message,
            r"selected shader resourceid does not match",
            r"selected shader entry point does not match",
            r"selected shader bytes do not match",
            r"selected shader catalog does not match",
            r"selected shader lifecycle does not match",
            r"active full-catalog binding (?:disappeared|changed)",
            r"existing workspace does not match the exact selected shader",
        )
    ):
        category = "capture-or-shader-identity-conflict"
    elif _contains(
        message,
        r"ray.*(?:state.?object|shader.?group|sbt)",
        r"(?:state.?object|shader.?group|sbt).*ray",
        r"library.*(?:export|association|state.?object)",
    ):
        category = "ray-library-replacement-unavailable"
    elif not isinstance(error, ToolExecutionError) and _contains(
        message,
        r"capture session changed",
        r"capture identity",
        r"active capture.*(?:changed|different|match)",
        r"shader identity.*(?:changed|different|match)",
    ):
        category = "capture-or-shader-identity-conflict"
    elif not isinstance(error, ToolExecutionError) and _contains(
        message,
        r"foreign renderdoc replacement",
        r"replacement.*(?:different workspace|no longer owned)",
        r"captured shader is owned by",
    ):
        category = "replacement-ownership-conflict"
    elif not isinstance(error, ToolExecutionError) and _contains(
        message,
        r"checked .* shader replacement failed",
        r"derived.*pipeline.*(?:failed|failure|rejected)",
        r"(?:pipeline|program).*rebuild.*(?:failed|failure|rejected)",
        r"program.*link.*(?:failed|failure|rejected)",
    ):
        category = "backend-replacement-rejected"
        ai_may_author_source = True
    elif not isinstance(error, ToolExecutionError) and _contains(
        message, r"root signature", r"rootsignature"
    ):
        category = "root-signature-contract"
    elif isinstance(error, ToolConfigurationError) or (
        not isinstance(error, ToolExecutionError)
        and _contains(
            message, r"integrity", r"sha-?256", r"fixed file", r"registry.*changed"
        )
    ):
        category = "tool-integrity-or-configuration"
    elif isinstance(error, AbiValidationError) or (
        not isinstance(error, ToolExecutionError)
        and _contains(message, r"abi", r"reflection", r"interface")
    ):
        if _contains(message, r"unavailable", r"incomplete", r"opaque"):
            category = "baseline-interface-unavailable"
        else:
            category = "abi-mismatch"
            ai_may_author_source = True
    elif isinstance(error, ToolExecutionError) and _contains(
        message, r"baseline compile probe failed"
    ):
        category = "baseline-compile-probe-failed"
        retryable = True
    elif isinstance(error, ToolExecutionError):
        if _contains(message, r"timed out"):
            category = "tool-timeout"
            retryable = True
        elif _contains(message, r"launch", r"not found", r"cannot start"):
            category = "tool-launch-failed"
        elif _contains(message, r"exited with code", r"process.*failed"):
            category = "tool-rejected-input"
            ai_may_author_source = True
        elif _contains(
            message,
            r"declared .* but output detected",
            r"non-utf",
            r"containing nul",
            r"did not create",
            r"not exactly one valid",
        ):
            category = "malformed-tool-output"
        elif _contains(message, r"root signature", r"rootsignature"):
            category = "root-signature-contract"
        else:
            category = "tool-execution-failed"

    result = {
        "schema": DIAGNOSTIC_SCHEMA,
        "schemaVersion": 1,
        "phase": str(phase or "unknown")[:128],
        "category": category,
        "exceptionType": kind,
        "message": message,
        "retryable": retryable,
        "availableCapabilities": (
            ["repair_shader_workspace_source_abi"]
            if ai_may_author_source and category in {
                "organization-abi-failed", "abi-mismatch", "backend-replacement-rejected"
            } else []
        ),
    }
    cause = _direct_cause(error)
    if cause is not None:
        cause_diagnostic = classify_shader_failure(
            cause,
            phase=("underlying-" + str(phase or "unknown")[:108]),
        )
        result["cause"] = cause_diagnostic
        if (
            category == "organization-compile-failed"
            and cause_diagnostic["category"]
            in {
                "tool-integrity-or-configuration",
                "tool-launch-failed",
                "tool-timeout",
                "malformed-tool-output",
            }
        ):
            result["availableCapabilities"] = []
    return result


# ---------------------------------------------------------------------------
# Shared compiler-diagnostic parsing
#
# Both the live workspace watcher (post-save compile) and the interactive
# compile-check path (unsaved-buffer live diagnostics) turn raw DXC/FXC
# stderr text into line/column-anchored records for the editor.  Keeping the
# regexes and the parser here — in the low-level diagnostics module — lets
# workspace.py reuse them without importing watcher.py (which would form an
# import cycle, since watcher.py imports ShaderWorkspaceService from
# workspace.py).
# ---------------------------------------------------------------------------

# DXC/FXC emit either "file:line:col: severity: message" (clang-style) or
# "file(line,col): severity: message" (MSVC-style).  Match both shapes.
_COMPILER_COLON_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<column>\d+):)?\s*"
    r"(?P<severity>fatal\s+error|error|warning|note)\s*:?[ \t]*"
    r"(?P<message>.*)$",
    flags=re.IGNORECASE,
)
_COMPILER_PAREN_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+)(?:\s*,\s*(?P<column>\d+))?\)\s*:"
    r"\s*(?P<severity>fatal\s+error|error|warning|note)\b\s*:?[ \t]*"
    r"(?P<message>.*)$",
    flags=re.IGNORECASE,
)
_COMPILER_GLSL_DIAGNOSTIC = re.compile(
    r"^(?P<severity>fatal\s+error|error|warning|note)\s*:\s*"
    r"(?P<file>.+?):(?P<line>\d+):(?:(?P<column>\d+):)?\s*"
    r"(?P<message>.*)$", re.IGNORECASE,
)
_COMPILER_MESA_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)\((?P<column>\d+)\)\s*:\s*"
    r"(?P<severity>fatal\s+error|error|warning|note)\s*:\s*"
    r"(?P<message>.*)$", re.IGNORECASE,
)


def parse_compiler_diagnostics(
    text: str,
    input_name: str,
    max_diagnostics: int = 128,
) -> list[dict[str, Any]]:
    """Parse compiler stdout/stderr into de-duplicated source locations.

    ``input_name`` is the basename the compiler saw for the source being
    checked (e.g. ``input.hlsl``); any diagnostic whose file component equals
    that name is reported with its original 1-based line/column so the editor
    can anchor a squiggle.  Diagnostics referring to *other* files (system
    headers, includes) keep their reported file but are still surfaced.

    The parser never raises: unparseable lines are ignored, and an empty list
    means "nothing actionable was recognised" — the caller decides whether that
    is a clean compile or an opaque failure.
    """

    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _COMPILER_COLON_DIAGNOSTIC.match(line)
        if match is None:
            match = _COMPILER_MESA_DIAGNOSTIC.match(line)
        if match is None:
            match = _COMPILER_GLSL_DIAGNOSTIC.match(line)
        if match is None:
            match = _COMPILER_PAREN_DIAGNOSTIC.match(line)
        if match is None:
            continue
        file_value = match.group("file").strip().strip('"')
        # Native GLSL compilers number their source strings. Only the main
        # string has a known editable document; other strings remain distinct.
        if file_value == "0" or re.fullmatch(r"source0\.(?:glsl|vert|frag|comp|geom|tesc|tese)", file_value, re.I):
            file_value = input_name
        severity = match.group("severity").lower().replace("fatal error", "error")
        # "note" lines annotate a prior error; keep them as hints so the editor
        # can show the follow-up context without escalating severity.
        if severity == "note":
            severity = "hint"
        message = _bounded_message(match.group("message"))
        try:
            line_number = int(match.group("line"))
        except (TypeError, ValueError):
            continue
        try:
            column_number = int(match.group("column") or 1)
        except (TypeError, ValueError):
            column_number = 1
        record = {
            "file": file_value,
            "line": line_number,
            "column": column_number,
            "severity": severity,
            "message": message,
        }
        key = (file_value, line_number, column_number, severity, message)
        if key in seen:
            continue
        seen.add(key)
        diagnostics.append(record)
        if len(diagnostics) >= max_diagnostics:
            break
    return diagnostics
