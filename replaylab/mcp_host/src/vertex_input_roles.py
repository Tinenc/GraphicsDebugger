"""Bounded source-use evidence, without assigning guessed vertex roles."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping


def analyze_inputs(payload: Mapping[str, Any], event_id: int, limit: int) -> dict[str, Any]:
    shader = payload.get("shader", payload)
    if not isinstance(shader, Mapping):
        raise ValueError("vertex Shader inspection omitted its reflection")
    documents = []
    for item in shader.get("sourceFiles", []) or []:
        if isinstance(item, Mapping) and isinstance(item.get("contents"), str):
            documents.append((str(item.get("filename", "embedded-source")), item["contents"], False))
    for key in ("source", "hlsl", "decompiledSource", "text"):
        if isinstance(shader.get(key), str) and shader[key].strip():
            documents.append((key, shader[key], False))
    if not documents:
        disassembly = shader.get("disassembly", {})
        if isinstance(disassembly, str):
            disassembly = {"disassembly": disassembly}
        if isinstance(disassembly, Mapping):
            documents.extend((str(name), text, True) for name, text in disassembly.items()
                             if isinstance(text, str) and text.strip())
    inputs = shader.get("inputs", shader.get("inputSignature", [])) or []
    rows = []
    for index, entry in enumerate(inputs):
        if not isinstance(entry, Mapping):
            continue
        semantic = str(entry.get("semanticName", entry.get("semantic", "")) or "")
        semantic_index = entry.get("semanticIndex", entry.get("idx", 0))
        names = [str(entry[key]) for key in ("variableName", "varName", "semanticIdxName", "name")
                 if entry.get(key)]
        if semantic:
            # A bare TEXCOORD label refers to a family, not every member of it.
            names = [name for name in names if name != semantic]
            names.append(semantic + str(semantic_index))
            if semantic_index == 0:
                names.append(semantic)
        names = list(dict.fromkeys(names))
        uses, signals, matched_count = [], set(), 0
        register = entry.get("regIndex", entry.get("registerIndex"))
        for filename, text, assembly in documents:
            candidates = names + (["v" + str(register)] if assembly and type(register) is int else [])
            pattern = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(n) for n in candidates) +
                                 r")(?![A-Za-z0-9_])", re.IGNORECASE) if candidates else None
            # Preserve line coordinates while removing comments from matching.
            cleaned = re.sub(r"/\*.*?\*/", lambda m: "\n" * m[0].count("\n"), text, flags=re.S)
            for number, line in enumerate(cleaned.splitlines(), 1):
                code = line.split("//", 1)[0]
                if not pattern or not pattern.search(code):
                    continue
                matched_count += 1
                for operation in ("normalize", "cross", "dot", "mul", "transpose", "float3x3"):
                    if re.search(r"\b" + operation + r"\s*\(", code):
                        signals.add(operation + " call on a matching source line; inspect dataflow")
                if len(uses) < limit:
                    uses.append({"document": filename, "line": number, "text": code.strip()[:600],
                                 "lineTruncated": len(code.strip()) > 600})
        rows.append({"signatureIndex": index, "attributeIndex": None, "names": names,
                     "semantic": semantic, "semanticIndex": semantic_index, "registerIndex": register,
                     "components": entry.get("compCount", entry.get("components", entry.get("count"))),
                     "componentType": entry.get("type", entry.get("compType")),
                     "usedLines": [use["text"] for use in uses], "sourceUses": uses,
                     "matchedLineCount": matched_count, "truncated": matched_count > len(uses),
                     "signals": sorted(signals), "role": "requires-dataflow-analysis"})
    return {"schema": "replaylab-vertex-input-roles/v1", "schemaVersion": 1, "eventId": event_id,
            "sourceAvailable": bool(documents), "inputs": rows,
            "shaderIdentity": {key: shader.get(key) for key in
                               ("resourceId", "rawSha256", "entryPoint", "stage", "captureSessionId")},
            "documents": [{"name": name, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                           "kind": "disassembly" if assembly else "source"} for name, text, assembly in documents],
            "howToDecide": "These are lexical use sites, not proven roles. Follow component swizzles, unpacking, "
                "transforms and downstream uses. normalize/cross alone does not establish normal or tangent. "
                "Join reflection semantics/location to inspect_resource_export vertex layout before using its "
                "attributeIndex; signature order is not vertex layout order. Keep masks, IDs, packed UVs and "
                "custom data unchanged; explicitly map them to Unity attributes and matching Shader input types.",
            "applied": False}
