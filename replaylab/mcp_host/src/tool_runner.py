"""Running a registered extractor as if it were an MCP tool, because that is enough.

An unpacker is a program with a command line. Wrapping one in an MCP server to make it
callable would be a second process and a second protocol for something already
executable, so instead its *declaration* borrows MCP's shape -- a name, a description
and an ``inputSchema``. Most execution uses plain argv; supported UEX previews
reuse its JSONL server so the same asset directory is mounted once. An Agent sees
``uex_extract_texture(assetPath=..., outputDir=...)`` and never learns that underneath
is an .exe rather than a server.

What makes that safe is that the argv is a template with named slots, and every slot
must be a parameter the operation declared. Arguments are type-checked against the
schema, substituted into a list, and executed without a shell; nothing a caller supplies
can become a new argument, a redirection or a second command. Output goes to a directory
the Host chose, the process has a timeout, and what comes back is bounded.

The three built-in parsers are declared here in exactly the same shape. They read
containers in-process and need no program, so they are complete from the start rather
than waiting for someone to discover their interface.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .tool_providers import (
    _PLACEHOLDER_RE, ToolConfigError, operation_placeholders, validate_input_schema,
)

SCHEMA = "replaylab-tool-invocation/v1"

MAX_OUTPUT_TEXT = 64 * 1024
MAX_OUTPUT_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_LISTED_FILES = 256
MAX_ARGUMENT_LENGTH = 4096
DEFAULT_TIMEOUT_SECONDS = 60.0

# Declared exactly like a registered operation, so the catalog the Agent reads has one
# shape whether the work happens in this process or in somebody's extractor.
BUILTIN_OPERATIONS: tuple[dict[str, Any], ...] = (
    {
        "toolId": "unreal-package",
        "name": "read_unreal_package",
        "kind": "builtin",
        "role": "asset-reader",
        "title": "Unreal cooked packages",
        "description": (
            "Read a cooked Unreal package's header, name table, imports and exports: "
            "the declared class of each object, the object paths it references, and "
            "texture platform data. Recognised by leading bytes, not by extension."),
        "purpose": (
            "Answers what an asset *is* and what it points at. It does not decode "
            "pixels, vertex buffers or material parameter values -- a registered "
            "extractor is needed for those."),
        "patterns": ["*.uasset", "*.umap", "*.uexp", "*.pak", "*.utoc"],
        "inputSchema": {
            "type": "object",
            "properties": {
                "assetPath": {"type": "string",
                              "description": "Path to the package, under the "
                                             "configured unpacked-asset root."},
            },
            "required": ["assetPath"],
        },
        "result": {"kind": "stdout-text", "successWhen": {"exitCode": 0, "minFiles": 0}},
        "limits": "no pixel data, no vertex buffers, no material parameter values",
    },
    {
        "toolId": "unity-serialized",
        "name": "read_unity_serialized",
        "kind": "builtin",
        "role": "asset-reader",
        "title": "Unity serialized files and bundles",
        "description": (
            "Read a Unity serialized file or bundle header and its object class ids -- "
            "Texture2D, Mesh, Material, AnimationClip and the rest."),
        "purpose": (
            "Classification only: it says which objects a file contains. Pixels, "
            "meshes and parameter values need AssetStudio, UnityPy or an equivalent "
            "registered as an extractor."),
        "patterns": ["*.assets", "*.bundle", "*.unity3d", "*.resS", "*.split*"],
        "inputSchema": {
            "type": "object",
            "properties": {
                "assetPath": {"type": "string",
                              "description": "Path to the serialized file or bundle."},
            },
            "required": ["assetPath"],
        },
        "result": {"kind": "stdout-text", "successWhen": {"exitCode": 0, "minFiles": 0}},
        "limits": "classification only; pixels, meshes and parameters need an extractor",
    },
    {
        "toolId": "standalone-containers",
        "name": "read_standalone_container",
        "kind": "builtin",
        "role": "asset-reader",
        "title": "Standalone containers",
        "description": (
            "Read a standalone image, texture or model container -- DDS, PNG, JPEG, "
            "EXR, HDR, FBX, glTF -- recognised by its leading bytes rather than its "
            "extension, so a mislabelled file is still identified."),
        "purpose": (
            "The fallback that always applies: format, dimensions, mip and format "
            "flags where the container states them."),
        "patterns": ["*.dds", "*.png", "*.jpg", "*.jpeg", "*.exr", "*.hdr", "*.tga",
                     "*.ktx", "*.fbx", "*.gltf", "*.glb"],
        "inputSchema": {
            "type": "object",
            "properties": {
                "assetPath": {"type": "string", "description": "Path to the file."},
            },
            "required": ["assetPath"],
        },
        "result": {"kind": "stdout-text", "successWhen": {"exitCode": 0, "minFiles": 0}},
        "limits": "",
    },
)


class ToolRunError(RuntimeError):
    """An invocation that was refused or that failed while running."""


def _coerce(name: str, value: Any, definition: Mapping[str, Any]) -> str:
    kind = str(definition.get("type", "string"))
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ToolRunError("%s must be true or false" % name)
        return "true" if value else "false"
    if kind in {"integer", "number"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolRunError("%s must be a number" % name)
        return str(int(value) if kind == "integer" else value)
    text = str(value)
    if len(text) > MAX_ARGUMENT_LENGTH:
        raise ToolRunError("%s is longer than %d characters" % (name, MAX_ARGUMENT_LENGTH))
    if "\x00" in text or "\n" in text or "\r" in text:
        raise ToolRunError("%s may not contain newlines or null bytes" % name)
    choices = definition.get("enum")
    if choices and text not in choices:
        raise ToolRunError("%s must be one of: %s" % (name, ", ".join(choices)))
    return text


def resolve_arguments(operation: Mapping[str, Any],
                      arguments: Mapping[str, Any]) -> dict[str, str]:
    """Type-check what the caller supplied against what the operation declared.

    An argument that was not declared is refused rather than ignored: a silently
    dropped parameter produces a command that runs, succeeds, and did the wrong thing.
    """

    schema = validate_input_schema(operation.get("inputSchema"))
    properties = schema["properties"]
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ToolRunError(
            "this operation declares no parameter called %s; it takes: %s"
            % (", ".join(unknown), ", ".join(sorted(properties)) or "(none)"))
    resolved: dict[str, str] = {}
    for name, definition in properties.items():
        if name in arguments and arguments[name] is not None:
            resolved[name] = _coerce(name, arguments[name], definition)
        elif "default" in definition:
            resolved[name] = _coerce(name, definition["default"], definition)
    missing = [name for name in schema["required"] if name not in resolved]
    if missing:
        raise ToolRunError("missing required parameter(s): " + ", ".join(missing))
    return resolved


# Slots the Host fills itself, never the caller: the program, the run's output
# directory, the file an asset-reader is describing, and the uex facts derived
# from the active project's unpacked root (its generated profile and the file's
# path relative to the mounted directory).
HOST_SLOTS = ("program", "outputDir", "path", "assetRoot", "uexConfig", "uexProfile", "uexPath")


def host_slots_needed(operation: Mapping[str, Any]) -> list[str]:
    """Which Host-supplied slots this operation's argv actually mentions."""

    return [name for name in operation_placeholders(operation.get("argv"))
            if name in HOST_SLOTS and name not in ("program", "outputDir", "path")]


def build_argv(operation: Mapping[str, Any], arguments: Mapping[str, Any], *,
               program: str, output_dir: str, declared_program: str = "",
               host_slots: Mapping[str, str] | None = None) -> list[str]:
    """The exact argument list to execute: a list, never a string, never a shell.

    Substitution is one pass over the template, so a value that happens to contain
    ``{something}`` is data and cannot introduce another slot. ``host_slots`` are
    the values the Host derived for the slots in ``HOST_SLOTS`` (a caller cannot
    pass them: they are not declared parameters).
    """

    resolved = dict(resolve_arguments(operation, arguments))
    for name, value in dict(host_slots or {}).items():
        if name in HOST_SLOTS:
            resolved[name] = str(value)
    resolved["program"] = str(program)
    resolved["outputDir"] = str(output_dir)
    missing = [name for name in operation_placeholders(operation.get("argv"))
               if name not in resolved]
    if missing:
        if "path" in missing:
            # `path` is the asset-reader slot: the Host fills it while describing a
            # file, so such a recipe is not something to call by name here. Say that
            # instead of asking for a parameter the schema never declared.
            raise ToolRunError(
                "this is an asset-reader recipe -- the Host runs it itself while "
                "identifying a file (describe_unpacked_asset), so it has no 'path' "
                "to pass here")
        raise ToolRunError("argv needs values for: " + ", ".join(sorted(missing)))
    # One regex pass per entry, not one replace() per parameter: replacing in turn
    # re-scans text that substitution just produced, so a value containing "{outputDir}"
    # would have been expanded a second time -- the exact injection this is meant to
    # prevent. A single sub() never looks at what it wrote.
    argv = [_PLACEHOLDER_RE.sub(
        lambda found: resolved.get(found.group(1), found.group(0)), str(entry))
        for entry in list(operation.get("argv") or [])]
    if not argv:
        raise ToolRunError("this operation has no argv to run")
    # The registered program is the only thing this may execute. A template that is
    # arguments only -- the shape the Host records for readers, where it supplies the
    # command itself -- would otherwise run its own first argument as a program: at
    # best a confusing failure, at worst something other than the tool the user
    # registered.
    if _names_program(argv[0], str(declared_program or program), resolved["program"]):
        # It names the program, so make it *be* the program: a bare "uex" left in
        # place would run whatever PATH resolves, not the binary the user registered.
        argv[0] = resolved["program"]
    else:
        argv.insert(0, resolved["program"])
    return argv


def _names_program(first: str, declared: str, resolved: str) -> bool:
    """Whether argv[0] already is the registered program, however it was written.

    A recipe recorded from a working command line often starts with the bare name
    the user typed ("uex"), while `resolved` is the absolute path `which` found.
    Comparing the two as strings would prepend the program a second time.
    """

    import os

    candidate = str(first or "").strip().strip('"')
    if not candidate:
        return False
    for known in (resolved, declared):
        known = str(known or "").strip().strip('"')
        if not known:
            continue
        if os.path.normcase(candidate) == os.path.normcase(known):
            return True
        # "uex" against "C:/tools/uex.exe", and the reverse.
        if os.path.normcase(os.path.splitext(os.path.basename(candidate))[0]) == \
                os.path.normcase(os.path.splitext(os.path.basename(known))[0]):
            return True
    return False


def _program_digest(program: str) -> str:
    try:
        digest = hashlib.sha256()
        with open(program, "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return ""


def validate_headless_argv(argv: Sequence[str]) -> None:
    """Reject UE Viewer's GUI fallback before creating the process.

    CREATE_NO_WINDOW hides a console; it cannot stop umodel's package chooser.
    Its batch commands need an existing package file and an explicit batch mode.
    """

    if not argv or Path(str(argv[0])).stem.lower() not in {"umodel", "umodel_64", "ueviewer"}:
        return
    arguments = [str(item) for item in argv[1:]]
    if len(arguments) == 1 and arguments[0].lower() in {"-help", "-taglist", "-version"}:
        return
    flags = {item.lower() for item in arguments if item.startswith("-")}
    if flags & {"-gui", "-view"} or not flags & {"-list", "-export", "-save", "-dump", "-check", "-pkginfo"}:
        raise ToolRunError("umodel requires an explicit batch command; graphical viewer mode is disabled")
    positional = [item for item in arguments if item and not item.startswith("-")]
    if not positional or not Path(positional[0]).is_file():
        raise ToolRunError("umodel requires an existing package file, not an empty path or directory")
    if Path(positional[0]).suffix.lower() not in {".upk", ".u", ".ukx", ".utx", ".usx", ".uax", ".xxx"}:
        raise ToolRunError("use uex for cooked Unreal assets; automated umodel calls are limited to legacy packages")


def background_process_kwargs() -> dict[str, Any]:
    """No interactive stdin or console windows for batch extractor processes."""

    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def validate_operation_proof(operation: Mapping[str, Any], program: str) -> str:
    """Verify a recorded program content binding; return the current SHA-256.

    Unproved discovery calls remain possible. This does not create a proof or
    establish that a sample produced valid material data.
    """

    resolved = shutil.which(program) or program
    proof = operation.get("proof") or operation.get("proved")
    expected = str(proof.get("programSha256") or "") if isinstance(proof, Mapping) else ""
    current = _program_digest(resolved)
    if expected and expected != current:
        raise ToolRunError(
            "the program has changed since this operation was proved; re-probe the new "
            "version on its sample before running the recorded recipe")
    return current


def run_operation(operation: Mapping[str, Any], arguments: Mapping[str, Any], *,
                  program: str, output_root: str | Path,
                  timeout_seconds: float | None = None,
                  host_slots: Mapping[str, str] | None = None,
                  cancel_event: threading.Event | None = None) -> dict[str, Any]:
    """Execute one declared operation and return a receipt of what happened.

    The receipt is the same whether the program succeeded or failed, because a failure
    that is described is evidence -- it is what ``knownFailures`` is written from --
    and a failure that raises is just a dead end.
    """

    resolved_program = shutil.which(program) or program
    if not Path(resolved_program).is_file():
        raise ToolRunError("the registered program is not there: %s" % program)
    program_sha256 = validate_operation_proof(operation, resolved_program)
    output_dir = Path(output_root) / ("run-%s" % os.urandom(8).hex())
    output_dir.mkdir(parents=True, exist_ok=False)
    # The registered spelling travels too: a launcher whose file name differs from
    # what the user typed in the recipe is still the program, not an argument.
    argv = build_argv(operation, arguments, program=resolved_program,
                      output_dir=str(output_dir), declared_program=str(program),
                      host_slots=host_slots)
    validate_headless_argv(argv)
    limit = float(timeout_seconds or operation.get("timeoutSeconds")
                  or DEFAULT_TIMEOUT_SECONDS)
    started = time.monotonic()
    timed_out = False
    transport_fields: dict[str, Any] = {}
    try:
        from . import uex_transport
        spec = uex_transport.adapt(operation, argv)
        if spec is not None:
            reply = uex_transport.POOL.run(spec, resolved_program, program_sha256,
                                          max(1.0, min(limit, 600.0)), cancel_event)
            exit_code, stdout, stderr = reply["exitCode"], reply["stdout"], reply["stderr"]
            timed_out = reply["timedOut"]
            transport_fields = {"transport": reply["transport"],
                                "cancelled": reply.get("cancelled", False)}
        else:
            completed = subprocess.run(  # noqa: S603 - argv list, shell=False, no user string
                argv, capture_output=True, timeout=max(1.0, min(limit, 600.0)),
                cwd=str(output_dir), shell=False, **background_process_kwargs())
            exit_code, stdout, stderr = (completed.returncode, completed.stdout,
                                         completed.stderr)
    except subprocess.TimeoutExpired as expired:
        timed_out = True
        exit_code = -1
        stdout, stderr = (expired.stdout or b""), (expired.stderr or b"")
    except OSError as error:
        raise ToolRunError("the program could not be started: %s" % error) from error

    produced = []
    for path in sorted(output_dir.rglob("*"))[:MAX_LISTED_FILES]:
        if path.is_file():
            produced.append({"path": str(path), "byteSize": path.stat().st_size})
    expectations = operation.get("result") or {}
    wanted = expectations.get("successWhen") or {}
    succeeded = (not timed_out
                 and exit_code == int(wanted.get("exitCode", 0))
                 and len(produced) >= int(wanted.get("minFiles", 0)))
    stdout_text = stdout.decode("utf-8", "replace")
    stdout_artifact: dict[str, Any] = {
        "stdoutTruncated": len(stdout_text) > MAX_OUTPUT_TEXT,
        "stdoutByteSize": len(stdout),
        "stdoutSha256": hashlib.sha256(stdout).hexdigest(),
        "stdoutFileTruncated": len(stdout) > MAX_OUTPUT_ARTIFACT_BYTES,
    }
    if stdout:
        # Tool responses may be compacted again by a transport. Keep the original
        # bytes even below the inline limit, so a JSON reader never parses a prose
        # preview. This Host receipt file is not an extractor-produced artifact
        # and must not satisfy a recipe's minFiles success condition.
        stdout_path = output_dir / ("host-stdout-%s.txt" % os.urandom(8).hex())
        retained = stdout[:MAX_OUTPUT_ARTIFACT_BYTES]
        with stdout_path.open("xb") as stream:
            stream.write(retained)
        stdout_artifact.update({
            "stdoutPath": str(stdout_path),
            "stdoutFileByteSize": len(retained),
            "stdoutFileSha256": hashlib.sha256(retained).hexdigest(),
        })
    return {
        "schema": SCHEMA,
        "schemaVersion": 1,
        "operation": str(operation.get("name", "")),
        "argv": argv,
        "programSha256": program_sha256,
        "exitCode": exit_code,
        "timedOut": timed_out,
        "durationSeconds": round(time.monotonic() - started, 3),
        "stdout": stdout_text[:MAX_OUTPUT_TEXT],
        **stdout_artifact,
        "stderr": stderr.decode("utf-8", "replace")[:MAX_OUTPUT_TEXT],
        "outputDirectory": str(output_dir),
        "producedFiles": produced,
        "producedFileCount": len(produced),
        "succeeded": succeeded,
        **transport_fields,
    }


async def run_operation_async(operation: Mapping[str, Any], arguments: Mapping[str, Any], *,
                              program: str, output_root: str | Path,
                              timeout_seconds: float | None = None,
                              host_slots: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Keep reader work off the event loop and propagate cancellation to its pipe."""
    cancel = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(
        run_operation, operation, arguments, program=program, output_root=output_root,
        timeout_seconds=timeout_seconds, host_slots=host_slots, cancel_event=cancel))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel.set()
        # Shielding retains the worker so its process is reaped, even when the
        # MCP caller goes away. A queued cancellation never kills another call.
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=5.0)
        except (asyncio.CancelledError, Exception):
            # Retrieve late failures; do not leave an unobserved Task exception.
            worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        raise


def declared_operations(tools: Sequence[Mapping[str, Any]], *,
                        include_builtins: bool = True,
                        proved_only: bool = True) -> list[dict[str, Any]]:
    """Everything callable, in one MCP-shaped list.

    Built-ins first because they always work; then each registered tool's operations.
    ``proved_only`` is the default because an operation nobody has watched succeed is a
    guess, and offering a guess as a callable tool is how an Agent ends up reporting
    that an extractor "failed" when it was never invoked correctly.
    """

    catalog: list[dict[str, Any]] = []
    if include_builtins:
        for operation in BUILTIN_OPERATIONS:
            entry = dict(operation)
            entry["proved"] = True
            entry["source"] = "built-in"
            catalog.append(entry)
    for tool in tools or []:
        if not tool.get("enabled", True):
            continue
        connection = tool.get("connection") or {}
        for operation in (tool.get("interface") or {}).get("operations") or []:
            if proved_only and not operation.get("proved"):
                continue
            entry = dict(operation)
            entry["toolId"] = tool.get("id", "")
            entry["title"] = tool.get("title", "")
            # The tool's own one-line description, which is what Settings writes and
            # what the user typed to say what this program is for. `note` is the
            # interface block's field and is always absent here.
            entry["purpose"] = tool.get("description", "")
            entry["program"] = connection.get("command", "")
            entry["source"] = str(connection.get("kind", ""))
            # Preserve the content-bound receipt when projecting the public bool.
            # Execution rechecks it immediately before creating the process.
            proof = operation.get("proved")
            entry["proof"] = dict(proof) if isinstance(proof, Mapping) else None
            entry["proved"] = bool(operation.get("proved"))
            entry["proofStatus"] = ("program-bound" if (entry["proof"] or {}).get("programSha256")
                                    else "program-unbound" if entry["proved"] else "unproved")
            catalog.append(entry)
    return catalog


def stale_operations(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Proved operations whose program is not the one they were proved against.

    Extractors change their arguments between versions. A recipe proved against an
    older build is not wrong so much as unverified, and saying so is better than
    letting it fail in a way nobody connects to the upgrade.
    """

    stale: list[dict[str, Any]] = []
    for tool in tools or []:
        program = (tool.get("connection") or {}).get("command", "")
        if not program:
            continue
        current = _program_digest(shutil.which(program) or program)
        for operation in (tool.get("interface") or {}).get("operations") or []:
            proved = operation.get("proved") or {}
            recorded = str(proved.get("programSha256", "") or "")
            if proved and recorded and current and recorded != current:
                stale.append({
                    "toolId": tool.get("id", ""),
                    "operation": operation.get("name", ""),
                    "provedAgainst": recorded[:16],
                    "programNow": current[:16],
                    "reason": ("the program has changed since this was proved; "
                               "re-run it against its sample before trusting it"),
                })
    return stale


__all__ = [
    "BUILTIN_OPERATIONS",
    "DEFAULT_TIMEOUT_SECONDS",
    "HOST_SLOTS",
    "SCHEMA",
    "ToolRunError",
    "build_argv",
    "declared_operations",
    "host_slots_needed",
    "resolve_arguments",
    "run_operation",
    "run_operation_async",
    "validate_operation_proof",
    "stale_operations",
]
