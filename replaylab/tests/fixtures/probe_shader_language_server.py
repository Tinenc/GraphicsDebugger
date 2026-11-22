"""Exercise a real HLSL language server through stdio, without an IDE or RDC.

Usage: python probe_shader_language_server.py /absolute/path/slangd.exe --output receipt.json
The scratch translation unit lives in the operating-system temporary directory.
"""
from __future__ import annotations

import argparse
import json
import queue
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote


class Client:
    def __init__(self, program: str, directory: Path):
        self.process = subprocess.Popen(
            [program, "--stdio"], cwd=directory, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.messages: queue.Queue = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                length = 0
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        raise EOFError("language server exited")
                    if line in (b"\r\n", b"\n"):
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                self.messages.put(json.loads(self.process.stdout.read(length)))
        except Exception as exc:
            self.messages.put(exc)

    def send(self, value):
        body = json.dumps(dict(jsonrpc="2.0", **value)).encode("utf-8")
        self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.process.stdin.flush()

    def notify(self, method, params):
        self.send(dict(method=method, params=params))

    def request(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self.send(dict(id=request_id, method=method, params=params))
        while True:
            response = self.messages.get(timeout=30)
            if isinstance(response, Exception):
                raise response
            if "method" in response:
                if "id" in response:
                    result = ([None for _ in response.get("params", {}).get("items", [])]
                              if response["method"] == "workspace/configuration" else None)
                    self.send(dict(id=response["id"], result=result))
                continue
            if response.get("id") == request_id:
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response.get("result")

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def position(source, needle, offset=0):
    start = source.index(needle) + offset
    prefix = source[:start]
    return dict(line=prefix.count("\n"),
                character=len(prefix.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2)


def target(result):
    value = result[0] if isinstance(result, list) and result else result
    if not value:
        return None
    return dict(uri=unquote(value.get("targetUri", value.get("uri"))),
                **value.get("targetSelectionRange", value.get("range", {})).get("start", {}))


def probe(program):
    with tempfile.TemporaryDirectory(prefix="bread-hlsl-semantic-") as scratch:
        root = Path(scratch)
        include = root / "shared.hlsli"
        include_source = "float Included(float value) { return value * 2.0; }\n"
        include.write_text(include_source, encoding="utf-8")
        source = '''#include "shared.hlsli"
float Overloaded(float value) { return value; }
int Overloaded(int value) { return value + 1; }
struct First { float amount; };
struct Second { float amount; };
float4 MainPS(float4 input : TEXCOORD0) : SV_Target0
{
    float shared = input.x;
    First first;
    Second second;
    /* 法线 😀 */ float semanticAnnotated = input.z;
    first.amount = 2.0;
    second.amount = 3.0;
    float total = Overloaded(shared);
    int integerTotal = Overloaded(3);
    {
        float shared = input.y;
        total += Included(shared);
    }
    return total + shared + first.amount + second.amount + integerTotal;
}
'''
        file = root / "main.hlsl"
        file.write_text(source, encoding="utf-8")
        unrelated = root / "unrelated.hlsl"
        unrelated.write_text("float shared; float Included(float x) { return -x; }\n", encoding="utf-8")
        client = Client(program, root)
        try:
            initialize = client.request("initialize", dict(
                processId=None, rootUri=root.as_uri(),
                capabilities=dict(workspace=dict(configuration=True), textDocument=dict(
                    definition={}, semanticTokens=dict(
                        requests=dict(full=True), formats=["relative"],
                        tokenTypes=["type", "variable", "parameter", "function", "property",
                                    "namespace", "enumMember", "macro", "keyword", "string"],
                        tokenModifiers=[], overlappingTokenSupport=False,
                        multilineTokenSupport=False)))))
            client.notify("initialized", {})
            for path, text in [(file, source), (unrelated, unrelated.read_text())]:
                client.notify("textDocument/didOpen", dict(textDocument=dict(
                    uri=path.as_uri(), languageId="hlsl", version=1, text=text)))

            def definition(needle, expected_path, expected_needle, offset=0, expected_source=None):
                result = target(client.request("textDocument/definition", dict(
                    textDocument=dict(uri=file.as_uri()), position=position(source, needle, offset))))
                expected = dict(uri=expected_path.as_uri(), **position(
                    source if expected_source is None else expected_source, expected_needle))
                return dict(ok=result == expected, actual=result, expected=expected)

            checks = {
                "floatOverload": definition("Overloaded(shared)", file, "Overloaded(float"),
                "intOverload": definition("Overloaded(3)", file, "Overloaded(int"),
                "includeIsolation": definition("Included(shared)", include, "Included(", expected_source=include_source),
                "shadowedLocal": definition("Included(shared)", file, "shared = input.y", offset=len("Included(")),
                "outerLocal": definition("total + shared", file, "shared = input.x", offset=len("total + ")),
                "memberOwner": definition("first.amount +", file, "amount; };", offset=len("first.")),
            }
            # Exercise the same full-document semantic coloring used by the native editor.
            # Compiler categories must reach declarations and uses, including include calls.
            legend = initialize["capabilities"]["semanticTokensProvider"]["legend"]["tokenTypes"]
            semantic_result = client.request("textDocument/semanticTokens/full", dict(
                textDocument=dict(uri=file.as_uri())))
            token_map = {}
            token_line = token_column = 0
            data = semantic_result["data"]
            for index in range(0, len(data), 5):
                delta_line, delta_column, length, kind, _ = data[index:index + 5]
                token_line += delta_line
                token_column = delta_column if delta_line else token_column + delta_column
                token_map[(token_line, token_column)] = (legend[kind], length)

            def semantic_kind(needle, expected, offset=0):
                at = position(source, needle, offset)
                actual = token_map.get((at["line"], at["character"]))
                kinds = expected if isinstance(expected, list) else [expected]
                return dict(ok=actual is not None and actual[0] in kinds,
                            actual=actual, expected=expected, position=at)

            checks.update({
                "colorFunctionDeclaration": semantic_kind("Overloaded(float", "function"),
                "colorFunctionCall": semantic_kind("Overloaded(shared)", "function"),
                "colorStructType": semantic_kind("First first", "type"),
                "colorParameter": semantic_kind("float4 input", "parameter", len("float4 ")),
                "colorLocal": semantic_kind("shared = input.x", "variable"),
                # Slang currently classifies struct fields as variables; other providers
                # may report the more specific standard property category.
                "colorStructMember": semantic_kind(
                    "first.amount +", ["variable", "property"], len("first.")),
                "colorIncludeFunction": semantic_kind("Included(shared)", "function"),
                "colorAfterUnicodeComment": semantic_kind("semanticAnnotated =", "variable"),
            })
            # A changed overload argument must use the unsaved editor buffer, not disk.
            previous_source = source
            source = source.replace("float shared = input.x;", "int shared = (int)input.x;")
            sync = initialize["capabilities"].get("textDocumentSync", 1)
            sync_kind = sync.get("change", 1) if isinstance(sync, dict) else sync
            change = dict(text=source)
            if sync_kind == 2:
                change["range"] = dict(start=dict(line=0, character=0), end=dict(
                    line=previous_source.count("\n"), character=len(
                        previous_source.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2))
            client.notify("textDocument/didChange", dict(textDocument=dict(uri=file.as_uri(), version=2),
                                                        contentChanges=[change]))
            checks["unsavedOverload"] = definition("Overloaded(shared)", file, "Overloaded(int")
            declaration = position(source, "shared = input.y")
            expected_reference = position(source, "Included(shared)", len("Included("))
            actual_references = []
            for match in re.finditer(r"\bshared\b", source):
                candidate = position(source, source[match.start():], 0)
                resolved = target(client.request("textDocument/definition", dict(
                    textDocument=dict(uri=file.as_uri()), position=candidate)))
                if resolved == dict(uri=file.as_uri(), **declaration) and candidate != declaration:
                    actual_references.append(candidate)
            checks["semanticReferencesExcludeShadowedNames"] = dict(
                ok=actual_references == [expected_reference], actual=actual_references,
                expected=[expected_reference])
            return dict(ok=all(x["ok"] for x in checks.values()), provider=program,
                        capabilities=initialize.get("capabilities"), checks=checks)
        finally:
            client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("program")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    receipt = probe(args.program)
    payload = json.dumps(receipt, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    raise SystemExit(0 if receipt["ok"] else 1)
