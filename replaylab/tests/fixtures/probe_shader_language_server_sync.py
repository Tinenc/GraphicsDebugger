"""Exercise HLSL document replacement and real diagnostic recovery in a bundled LSP.

This probe uses stdio and a system-temp fixture; --source reads a real Shader without changing it.
It intentionally inserts a missing identifier into the LSP buffer, then repairs the buffer.
"""
from __future__ import annotations

import argparse
import json
import queue
import tempfile
import time
from pathlib import Path

from probe_shader_language_server import Client


def end_position(source):
    return dict(line=source.count("\n"),
                character=len(source.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2)


class DiagnosticClient(Client):
    def __init__(self, program, directory):
        super().__init__(program, directory)
        self.diagnostics = []

    def receive(self, timeout):
        message = self.messages.get(timeout=timeout)
        if isinstance(message, Exception):
            raise message
        if message.get("method") == "textDocument/publishDiagnostics":
            self.diagnostics = message["params"]["diagnostics"]
        if "method" in message and "id" in message:
            values = ([None for _ in message.get("params", {}).get("items", [])]
                      if message["method"] == "workspace/configuration" else None)
            self.send(dict(id=message["id"], result=values))
        return message

    def call(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self.send(dict(id=request_id, method=method, params=params))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            message = self.receive(max(0.01, deadline - time.monotonic()))
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result")
        raise TimeoutError(method)

    def settle(self):
        # Slang publishes diagnostics on its 1 second background interval. Semantic tokens
        # below also force the current translation unit to be analyzed before this drain.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                self.receive(max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break


def probe(program, source_path=None):
    with tempfile.TemporaryDirectory(prefix="bread-hlsl-sync-") as scratch:
        if source_path is None:
            source_path = Path(scratch) / "sync.hlsl"
            source_path.write_text(
                "// 法线 😀\nstruct FrameData { float3 normal; };\n"
                "float4 MainPS(float4 input : TEXCOORD0) : SV_Target0\n"
                "{ FrameData frame; frame.normal = input.xyz; return float4(frame.normal, 1); }\n",
                encoding="utf-8")
        source = source_path.read_text(encoding="utf-8-sig")
        client = DiagnosticClient(program, source_path.parent)
        try:
            capabilities = client.call("initialize", dict(
                processId=None, rootUri=source_path.parent.as_uri(),
                capabilities=dict(workspace=dict(configuration=True),
                                  textDocument=dict(publishDiagnostics={})))).get("capabilities", {})
            sync = capabilities.get("textDocumentSync", 1)
            sync_kind = sync.get("change", 1) if isinstance(sync, dict) else sync
            client.notify("initialized", {})
            client.settle()  # Consume configuration before opening/changing the document.
            uri = source_path.as_uri()
            document = dict(uri=uri)
            client.notify("textDocument/didOpen", dict(textDocument=dict(
                uri=uri, languageId="hlsl", version=1, text=source)))
            synchronized = source
            version = 1
            observations = []

            def analyze(label):
                client.call("textDocument/semanticTokens/full", dict(textDocument=document))
                client.settle()
                errors = [d for d in client.diagnostics if d.get("severity", 1) == 1]
                observations.append(dict(label=label, diagnostics=len(client.diagnostics),
                                         errors=len(errors), messages=[d["message"] for d in errors[:4]]))
                return errors

            def replace(text):
                nonlocal synchronized, version
                version += 1
                final_line = synchronized.rsplit("\n", 1)[-1]
                if (sync_kind == 0 or (sync_kind == 2 and Path(program).stem.lower() == "slangd"
                                      and not final_line.isascii())):
                    # Match CodeEditorService's Slang Unicode EOF compatibility path.
                    client.notify("textDocument/didClose", dict(textDocument=document))
                    client.notify("textDocument/didOpen", dict(textDocument=dict(
                        uri=uri, languageId="hlsl", version=version, text=text)))
                    synchronized = text
                    return
                change = dict(text=text)
                if sync_kind == 2:
                    change["range"] = dict(start=dict(line=0, character=0), end=end_position(synchronized))
                client.notify("textDocument/didChange", dict(
                    textDocument=dict(uri=uri, version=version), contentChanges=[change]))
                synchronized = text

            checks = {"initialValid": not analyze("initial")}
            for index in range(3):
                # Grow and shrink the unsaved document, including non-ASCII final lines.
                replace(source + ("// repeated update 法线 😀" if index % 2 == 0 else ""))
                checks["replacement%d" % index] = not analyze("replacement%d" % index)
            replace(source + "\nfloat BreadDiagnosticProbe() { return BreadMissingDiagnosticSymbol; }\n")
            errors = analyze("intentionalError")
            checks["realErrorDetected"] = any("BreadMissingDiagnosticSymbol" in d["message"] for d in errors)
            replace(source)
            checks["repairedErrorCleared"] = not analyze("repaired")
            return dict(ok=all(checks.values()), provider=program, source=str(source_path),
                        syncKind=sync_kind, checks=checks, observations=observations)
        finally:
            client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("program")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    receipt = probe(args.program, args.source)
    payload = json.dumps(receipt, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    raise SystemExit(0 if receipt["ok"] else 1)

