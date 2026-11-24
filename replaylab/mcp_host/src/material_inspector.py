"""Optional presentation metadata; never alter captured values or bindings."""
import math

SCHEMA = "bread-material-inspector/v1"


def build_material_inspector(spec, properties, textures):
    mappings = {item.get("target"): item for item in spec.get("constantBuffers", [])
                if isinstance(item, dict) and item.get("classification") == "material"}
    result, diagnostics = [], []
    for item in [*textures, *properties]:
        name = item.get("property") or item.get("name")
        if not isinstance(name, str) or not name or len(name) > 512:
            continue
        source = item if item in textures else mappings.get(name, {})
        annotation = source.get("inspector", {})
        if not isinstance(annotation, dict):
            diagnostics.append(name + ": invalid inspector annotation; using Shader property UI")
            continue
        entry = {"name": name}
        for key in ("label", "group", "tooltip"):
            value = annotation.get(key)
            if isinstance(value, str) and value.strip() and len(value) <= 1024 and "\x00" not in value:
                entry[key] = value.strip()
        control = annotation.get("control", "default")
        scalar = item.get("type") in ("float", "int", "uint")
        vector_color = (item.get("type") == "vector" and isinstance(item.get("value"), list)
                        and len(item["value"]) in (3, 4))
        if control == "color" and (item.get("type") == "color" or vector_color):
            entry["control"] = "color"
            entry["alpha"] = annotation.get("alpha") is True
            entry["hdr"] = annotation.get("hdr") is True
        elif control == "slider" and scalar:
            low, high = annotation.get("min"), annotation.get("max")
            if (type(low) in (int, float) and type(high) in (int, float)
                    and math.isfinite(low) and math.isfinite(high) and low < high
                    and abs(low) <= 3.4e38 and abs(high) <= 3.4e38):
                entry.update(control="slider", min=low, max=high)
            else:
                diagnostics.append(name + ": invalid slider bounds; using numeric field")
        elif control not in ("default", None):
            diagnostics.append(name + ": incompatible control; using Shader property UI")
        result.append(entry)
    return {"schema": SCHEMA, "properties": result, "diagnostics": diagnostics}
