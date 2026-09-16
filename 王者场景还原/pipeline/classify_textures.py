# -*- coding: utf-8 -*-
"""
Classify exported textures into PBR roles and emit a manifest.

Run with the managed system python (NOT the qrenderdoc py36):
    python E:\\GST\\libai_scene\\classify_textures.py

Heuristics for mobile (王者荣耀 / Vulkan) captures:
  * normal map      -> mid-gray-blue average, low saturation, R~G~0.5 B~1.0
  * basecolor       -> largest resolution, colourful, bound to an early slot
  * mask/packed     -> low saturation OR strongly channel-independent
  * lut / ramp      -> tiny or extremely non-square
  * cubemap/env     -> flagged by the exporter
Renames nothing destructively: writes a manifest + a `sorted/` tree of copies
with role-based names so Blender hookup is unambiguous.
"""

import json
import os
import shutil
import struct
import zlib
from collections import defaultdict

ROOT = r"E:\GST\libai_scene\scene_export"
SCENE = os.path.join(ROOT, "scene.json")
SORTED_DIR = os.path.join(ROOT, "_textures_sorted")


# ---------- minimal PNG reader (no PIL dependency) ----------

def read_png(path):
    """Return (w, h, [(r,g,b,a)...]) subsampled, or None."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None

    pos = 8
    w = h = bitd = colort = None
    idat = bytearray()
    while pos + 8 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, bitd, colort = struct.unpack(">IIBB", chunk[:10])
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
        pos += 12 + ln

    if w is None or bitd != 8:
        return None
    nch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colort)
    if nch is None or colort == 3:
        return None

    try:
        raw = zlib.decompress(bytes(idat))
    except Exception:
        return None

    stride = w * nch
    pixels = []
    prev = bytearray(stride)
    p = 0
    # subsample rows to keep this cheap on 2K textures
    row_step = max(1, h // 64)
    for y in range(h):
        if p >= len(raw):
            break
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if len(line) < stride:
            break
        # undo PNG filters
        if ft == 1:
            for i in range(nch, stride):
                line[i] = (line[i] + line[i - nch]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                b = prev[i]
                c = prev[i - nch] if i >= nch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        prev = line

        if y % row_step:
            continue
        col_step = max(1, w // 64)
        for x in range(0, w, col_step):
            o = x * nch
            if nch == 1:
                v = line[o]
                pixels.append((v, v, v, 255))
            elif nch == 2:
                v = line[o]
                pixels.append((v, v, v, line[o + 1]))
            elif nch == 3:
                pixels.append((line[o], line[o + 1], line[o + 2], 255))
            else:
                pixels.append((line[o], line[o + 1], line[o + 2], line[o + 3]))
    return (w, h, pixels)


def stats(px):
    n = len(px)
    if not n:
        return None
    sr = sg = sb = sa = 0
    sat = 0.0
    for r, g, b, a in px:
        sr += r
        sg += g
        sb += b
        sa += a
        mx, mn = max(r, g, b), min(r, g, b)
        if mx:
            sat += (mx - mn) / float(mx)
    return {
        "r": sr / n / 255.0, "g": sg / n / 255.0,
        "b": sb / n / 255.0, "a": sa / n / 255.0,
        "sat": sat / n, "n": n,
    }


def classify(meta, st):
    """Return (role, confidence, reason)."""
    w = meta.get("w", 0) or 0
    h = meta.get("h", 0) or 0
    fmt = (meta.get("fmt") or "").upper()
    slot = meta.get("slot", 99)

    if meta.get("cubemap"):
        return "env_cube", "high", "flagged cubemap"
    if w and h and (w <= 16 or h <= 16):
        return "lut_ramp", "high", "tiny {0}x{1}".format(w, h)
    if w and h and max(w, h) / float(max(1, min(w, h))) >= 8:
        return "lut_ramp", "med", "extreme aspect {0}x{1}".format(w, h)

    if st is None:
        return "unknown", "low", "unreadable png"

    r, g, b, sat = st["r"], st["g"], st["b"], st["sat"]

    # --- channel-packed data texture ---
    # A dead (all-zero) channel next to live ones means the artist packed
    # unrelated scalars into RGB; it is data, not an albedo map. Real basecolour
    # art essentially never has an identically-zero green channel.
    dead = [c for c in (r, g, b) if c < 0.01]
    if len(dead) >= 1 and max(r, g, b) > 0.3:
        return "mask_packed", "high", "dead channel R={0:.2f} G={1:.2f} B={2:.2f}".format(r, g, b)

    # --- normal map ---
    # Tangent-space normals average to R~0.5, G~0.5, B~1.0 (the +Z bias).
    # Do NOT gate this on saturation: because B sits far above R/G, the naive
    # (max-min)/max saturation of a normal map lands around 0.5, which would
    # misclassify it as a colour texture. The R~G~0.5 + high-B signature is
    # what actually identifies it.
    if abs(r - 0.5) < 0.10 and abs(g - 0.5) < 0.10 and b >= 0.85:
        return "normal", "high", "RG~0.5 B={0:.2f} (tangent-space)".format(b)
    # looser band: still clearly a normal (some maps bias G for DirectX-style)
    if abs(r - 0.5) < 0.15 and abs(g - 0.5) < 0.20 and b >= 0.75 and b - max(r, g) > 0.25:
        return "normal", "med", "RG~0.5 B={0:.2f} B-RG={1:.2f}".format(b, b - max(r, g))

    # near-greyscale = mask / roughness / metallic / AO pack
    if sat < 0.12:
        return "mask_packed", "med", "sat={0:.2f} greyscale".format(sat)

    # colourful and large -> basecolor
    if sat >= 0.18:
        conf = "high" if slot <= 2 else "med"
        return "basecolor", conf, "sat={0:.2f} slot={1}".format(sat, slot)

    return "mask_packed", "low", "sat={0:.2f} fallback".format(sat)
    # Tangent-space normals average to R~0.5, G~0.5, B~1.0 (the +Z bias).
    # Do NOT gate this on saturation: because B sits far above R/G, the naive
    # (max-min)/max saturation of a normal map lands around 0.5, which would
    # misclassify it as a colour texture. The R~G~0.5 + high-B signature is
    # what actually identifies it.
    if abs(r - 0.5) < 0.10 and abs(g - 0.5) < 0.10 and b >= 0.85:
        return "normal", "high", "RG~0.5 B={0:.2f} (tangent-space)".format(b)
    # looser band: still clearly a normal (some maps bias G for DirectX-style)
    if abs(r - 0.5) < 0.15 and abs(g - 0.5) < 0.20 and b >= 0.75 and b - max(r, g) > 0.25:
        return "normal", "med", "RG~0.5 B={0:.2f} B-RG={1:.2f}".format(b, b - max(r, g))

    # near-greyscale = mask / roughness / metallic / AO pack
    if sat < 0.12:
        return "mask_packed", "med", "sat={0:.2f} greyscale".format(sat)

    # colourful and large -> basecolor
    if sat >= 0.18:
        conf = "high" if slot <= 2 else "med"
        return "basecolor", conf, "sat={0:.2f} slot={1}".format(sat, slot)

    return "mask_packed", "low", "sat={0:.2f} fallback".format(sat)


def main():
    scene = json.load(open(SCENE, encoding="utf-8"))

    # gather unique textures with the group dir they live in
    entries = {}
    for group, items in scene["groups"].items():
        gdir = os.path.join(ROOT, group)
        for it in items:
            for t in it.get("textures", []):
                path = os.path.join(gdir, "textures", t["file"])
                key = (group, t["file"])
                if key in entries:
                    entries[key]["eids"].append(it["eventId"])
                    entries[key]["slots"].add(t.get("slot", 99))
                    continue
                entries[key] = {
                    "group": group, "file": t["file"], "path": path,
                    "id": t["id"], "w": t.get("w"), "h": t.get("h"),
                    "fmt": t.get("fmt"), "cubemap": t.get("cubemap", False),
                    "slot": t.get("slot", 99),
                    "eids": [it["eventId"]], "slots": set([t.get("slot", 99)]),
                }

    print("analysing {0} unique texture file(s)...".format(len(entries)))
    manifest = []
    for key in sorted(entries):
        e = entries[key]
        if not os.path.isfile(e["path"]):
            e["role"] = "missing"
            continue
        img = read_png(e["path"])
        st = stats(img[2]) if img else None
        if img and not e.get("w"):
            e["w"], e["h"] = img[0], img[1]
        role, conf, why = classify(e, st)
        e["role"], e["conf"], e["why"] = role, conf, why
        e["stats"] = st
        e["sizeKB"] = round(os.path.getsize(e["path"]) / 1024.0, 1)
        e["slots"] = sorted(e["slots"])
        manifest.append(e)

    # copy into role-sorted tree with descriptive names
    if os.path.isdir(SORTED_DIR):
        shutil.rmtree(SORTED_DIR, ignore_errors=True)
    by_role = defaultdict(list)
    for e in manifest:
        by_role[e["role"]].append(e)

    for role, lst in by_role.items():
        d = os.path.join(SORTED_DIR, role)
        os.makedirs(d, exist_ok=True)
        for e in lst:
            eid0 = min(e["eids"])
            newname = "{0}_eid{1}_slot{2}_id{3}_{4}x{5}.png".format(
                role, eid0, e["slot"], e["id"], e.get("w") or 0, e.get("h") or 0)
            try:
                shutil.copy2(e["path"], os.path.join(d, newname))
                e["sortedName"] = newname
            except Exception as ex:
                e["copyError"] = str(ex)

    with open(os.path.join(ROOT, "textures_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1, default=str)

    # report
    lines = []
    lines.append("texture classification ({0} files)".format(len(manifest)))
    lines.append("")
    for role in sorted(by_role, key=lambda r: -len(by_role[r])):
        lst = by_role[role]
        lines.append("{0}  ({1} file(s))".format(role, len(lst)))
        for e in sorted(lst, key=lambda x: (-(x.get("w") or 0), x["id"])):
            lines.append("   id={0:<6} {1:>4}x{2:<4} slot={3:<2} eids={4:<22} {5:<5} {6}".format(
                e["id"], e.get("w") or "?", e.get("h") or "?", e["slot"],
                ",".join(str(x) for x in sorted(set(e["eids"]))[:5]),
                e.get("conf", ""), e.get("why", "")))
        lines.append("")
    txt = "\n".join(lines)
    with open(os.path.join(ROOT, "textures_report.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)
    print("wrote textures_manifest.json / textures_report.txt")
    print("sorted copies -> {0}".format(SORTED_DIR))


if __name__ == "__main__":
    main()
