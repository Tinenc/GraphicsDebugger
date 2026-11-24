"""The UnityPy reader Bread AI runs with the user's Python interpreter.

Kept as a string so the packaged runtime can write it next to the user's data and
run it with whatever ``python`` has ``UnityPy`` installed. Two commands:

* ``describe <file>``: one JSON object on stdout -- the Host's asset-reader
  contract (kind, engine, objects by class, names, containers).
* ``export <file> --out <dir> [--types A,B] [--name text]``: shader text
  (``.shader``), material parameters (``.json``), textures (``.png``), text assets
  and MonoBehaviour dumps into ``<dir>``.
"""

SCRIPT = r'''#!/usr/bin/env python3
"""UnityPy reader for Bread AI (generated; do not edit -- it is rewritten on use)."""
import argparse
import hashlib
import json
import math
import os
import re
import sys


def _fail(message, code=2):
    sys.stderr.write(message + "\n")
    sys.exit(code)


try:
    import UnityPy  # noqa: E402
except Exception as exc:  # pragma: no cover - environment dependent
    _fail("UnityPy is not installed for this interpreter (%s): pip install UnityPy" % exc)

SHADER_TYPES = {"Shader"}
TEXT_TYPES = {"TextAsset"}
IMAGE_TYPES = {"Texture2D", "Sprite"}
MATERIAL_TYPES = {"Material"}
MONO_TYPES = {"MonoBehaviour"}
MESH_TYPES = {"Mesh"}


def _safe_name(value, fallback):
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")
    return (text or fallback)[:120]


def _object_name(obj, data=None):
    try:
        if data is None:
            data = obj.read()
        name = getattr(data, "m_Name", None) or getattr(data, "name", None)
        return str(name or "")
    except Exception:
        return ""


def _containers(env):
    found = {}
    try:
        for path, obj in env.container.items():
            found[(id(getattr(obj, "assets_file", None)), obj.path_id)] = path
    except Exception:
        pass
    return found


def _source_scope(path, obj):
    source = getattr(getattr(obj, "assets_file", None), "name", "")
    return os.path.abspath(path).replace("\\", "/") + ("::" + str(source) if source else "")


def describe(path, limit):
    env = UnityPy.load(path)
    containers = _containers(env)
    by_class = {}
    objects = []
    total = 0
    materials = []
    for obj in env.objects:
        total += 1
        kind = str(obj.type.name)
        by_class[kind] = by_class.get(kind, 0) + 1
        if len(objects) < limit and kind in (SHADER_TYPES | TEXT_TYPES | IMAGE_TYPES |
                                             MATERIAL_TYPES | MONO_TYPES | MESH_TYPES |
                                             {"AnimationClip", "GameObject", "Font"}):
            objects.append({
                "class": kind,
                "name": _object_name(obj),
                "pathId": int(obj.path_id),
                "container": containers.get((id(getattr(obj, "assets_file", None)), obj.path_id), ""),
                "sourceFile": _source_scope(path, obj),
                "byteSize": int(getattr(obj, "byte_size", 0) or 0),
            })
            if kind == "Material" and len(materials) < min(limit, 16):
                try:
                    materials.append(_material_json(obj.read(), source_file=_source_scope(path, obj)))
                except Exception:
                    pass
    kind = "unity-bundle" if path.lower().endswith((".bundle", ".unity3d", ".ab",
                                                   ".unitybundle")) else "unity-serialized"
    print(json.dumps({
        "kind": kind,
        "engine": "unity",
        "reader": "unitypy",
        "unityVersion": ",".join(sorted({str(f.version) for f in env.files.values()
                                         if getattr(f, "version", None)}))
        if hasattr(env, "files") else "",
        "objectCount": total,
        "classes": by_class,
        "objects": objects,
        "hasShaders": bool(by_class.get("Shader")),
        "hasMaterials": bool(by_class.get("Material")),
        "exports": materials,
        "materialsTruncated": int(by_class.get("Material", 0)) > len(materials),
    }, ensure_ascii=False, allow_nan=False))


def _material_json(data, source_file=""):
    props = getattr(data, "m_SavedProperties", None)
    out = {"name": _object_name(None, data), "shader": None, "textures": {}, "floats": {},
           "colors": {}, "keywords": [], "m_Name": _object_name(None, data),
           "sourceFile": source_file,
           "m_SavedProperties": {"m_TexEnvs": [], "m_Floats": [], "m_Colors": [], "m_Ints": []}}
    saved = out["m_SavedProperties"]
    try:
        shader = getattr(data, "m_Shader", None)
        if shader is not None:
            sdata = shader.read() if hasattr(shader, "read") else None
            out["shader"] = _object_name(None, sdata) if sdata is not None else str(shader)
    except Exception:
        pass
    try:
        out["keywords"] = list(getattr(data, "m_ShaderKeywords", "") .split()) \
            if isinstance(getattr(data, "m_ShaderKeywords", ""), str) else \
            [str(k) for k in (getattr(data, "m_ValidKeywords", []) or [])]
    except Exception:
        pass
    if props is None:
        return out

    def _pairs(value):
        if isinstance(value, dict):
            return list(value.items())[:512]
        try:
            return [(item[0], item[1]) for item in value[:512]]
        except Exception:
            return []
    for key, value in _pairs(getattr(props, "m_TexEnvs", [])):
        key = str(getattr(key, "name", key))
        try:
            tex = getattr(value, "m_Texture", None)
            file_id = int(getattr(tex, "m_FileID", getattr(tex, "file_id", 0)))
            path_id = int(getattr(tex, "m_PathID", getattr(tex, "path_id", 0)))
            reference = {"fileId": file_id, "pathId": path_id,
                         "sourceFile": source_file, "isNull": path_id == 0 or file_id < 0}
            texture_name = ""
            if tex is not None and not reference["isNull"]:
                try:
                    texture_name = _object_name(None, tex.read()) if hasattr(tex, "read") else ""
                except Exception:
                    pass  # Missing externals must not discard the PPtr itself.
            entry = {
                "texture": texture_name,
                "objectReference": reference,
                "scale": list(getattr(value, "m_Scale", []) or []) if not hasattr(
                    getattr(value, "m_Scale", None), "x") else
                [value.m_Scale.x, value.m_Scale.y],
                "offset": list(getattr(value, "m_Offset", []) or []) if not hasattr(
                    getattr(value, "m_Offset", None), "x") else
                [value.m_Offset.x, value.m_Offset.y],
            }
            out["textures"][key] = entry
            saved["m_TexEnvs"].append({"first": key, "second": {
                "m_Texture": {"m_FileID": file_id, "m_PathID": path_id, "m_Name": texture_name},
                "m_Scale": entry["scale"], "m_Offset": entry["offset"]}})
        except Exception:
            out["textures"][key] = {"texture": ""}
    for key, value in _pairs(getattr(props, "m_Floats", [])):
        try:
            number = float(value)
            if math.isfinite(number):
                name = str(getattr(key, "name", key))
                out["floats"][name] = number
                saved["m_Floats"].append({"first": name, "second": number})
        except Exception:
            pass
    for key, value in _pairs(getattr(props, "m_Colors", [])):
        try:
            components = [value.r, value.g, value.b, value.a] if hasattr(
                value, "r") else list(value)
            if all(math.isfinite(float(item)) for item in components):
                name = str(getattr(key, "name", key))
                out["colors"][name] = components
                saved["m_Colors"].append({"first": name, "second": components})
        except Exception:
            pass
    for key, value in _pairs(getattr(props, "m_Ints", []) or []):
        if isinstance(value, int) and not isinstance(value, bool):
            saved["m_Ints"].append({"first": str(getattr(key, "name", key)), "second": value})
    return out


def export(path, out_dir, types, name_filter):
    os.makedirs(out_dir, exist_ok=True)
    wanted = {item.strip() for item in types.split(",") if item.strip()} or {"Shader",
                                                                           "Material",
                                                                           "TextAsset"}
    env = UnityPy.load(path)
    written = []
    index = []
    for obj in env.objects:
        kind = str(obj.type.name)
        if kind not in wanted:
            continue
        try:
            data = obj.read()
        except Exception as exc:
            index.append({"class": kind, "pathId": int(obj.path_id), "error": str(exc)[:200]})
            continue
        name = _object_name(obj, data)
        if name_filter and name_filter.lower() not in name.lower():
            continue
        source_file = _source_scope(path, obj)
        scope_key = hashlib.sha256(source_file.encode("utf-8")).hexdigest()[:12]
        base = os.path.join(out_dir, "%s_%s_%s_%d" % (kind, _safe_name(name, kind), scope_key,
                                                      int(obj.path_id)))
        target = None
        try:
            if kind in SHADER_TYPES:
                text = None
                try:
                    from UnityPy.export import ShaderConverter
                    text = ShaderConverter.export_shader(data)
                except Exception:
                    text = None
                if text is None:
                    text = json.dumps(obj.parse_as_dict() if hasattr(obj, "parse_as_dict")
                                      else {}, ensure_ascii=False, indent=1, default=str)
                target = base + ".shader"
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write(text)
            elif kind in MATERIAL_TYPES:
                target = base + ".material.json"
                with open(target, "w", encoding="utf-8") as handle:
                    json.dump(_material_json(data, source_file=source_file), handle,
                              ensure_ascii=False, indent=1, allow_nan=False)
            elif kind in TEXT_TYPES:
                script = getattr(data, "m_Script", b"")
                target = base + ".txt"
                mode = "wb" if isinstance(script, (bytes, bytearray)) else "w"
                with open(target, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) \
                        as handle:
                    handle.write(script)
            elif kind in IMAGE_TYPES:
                target = base + ".png"
                data.image.save(target)
            elif kind in MONO_TYPES:
                target = base + ".json"
                tree = obj.parse_as_dict() if hasattr(obj, "parse_as_dict") else {}
                with open(target, "w", encoding="utf-8") as handle:
                    json.dump(tree, handle, ensure_ascii=False, indent=1, default=str)
            elif kind in MESH_TYPES:
                target = base + ".obj"
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write(data.export())
        except Exception as exc:
            index.append({"class": kind, "name": name, "pathId": int(obj.path_id),
                          "error": str(exc)[:200]})
            continue
        if target:
            written.append(target)
            index.append({"class": kind, "name": name, "pathId": int(obj.path_id),
                          "sourceFile": source_file, "file": os.path.basename(target)})
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as handle:
        json.dump({"source": path, "written": len(written), "entries": index}, handle,
                  ensure_ascii=False, indent=1)
    print(json.dumps({"written": len(written), "outputDir": out_dir}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("describe")
    d.add_argument("path")
    d.add_argument("--limit", type=int, default=500)
    e = sub.add_parser("export")
    e.add_argument("path")
    e.add_argument("--out", required=True)
    e.add_argument("--types", default="Shader,Material,TextAsset")
    e.add_argument("--name", default="")
    args = parser.parse_args()
    if not os.path.exists(args.path):
        _fail("no such file: %s" % args.path)
    if args.command == "describe":
        describe(args.path, max(1, args.limit))
    else:
        export(args.path, args.out, args.types, args.name)


if __name__ == "__main__":
    main()
'''
