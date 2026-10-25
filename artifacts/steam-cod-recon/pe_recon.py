#!/usr/bin/env python3
"""Read-only PE recon for Call of Duty HQ (cod.exe).

Only reads DOS/NT headers, section table and directory tables via seek(),
so a 220 MB+ image is inspected without loading it into memory.

Output: JSON summary + human readable digest on stdout.
"""
import json
import struct
import sys
import os
import hashlib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def rva_to_off(sections, rva):
    for s in sections:
        if s["va"] <= rva < s["va"] + max(s["vsize"], s["rawsize"]):
            return s["rawptr"] + (rva - s["va"])
    return None


def cstr(f, off, limit=512):
    f.seek(off)
    buf = f.read(limit)
    end = buf.find(b"\x00")
    return buf[: end if end >= 0 else len(buf)].decode("latin-1", "replace")


def read_imports(f, sections, rva):
    out = []
    off = rva_to_off(sections, rva)
    if off is None:
        return out
    while True:
        f.seek(off)
        desc = f.read(20)
        if len(desc) < 20:
            break
        oft, tds, fwd, name_rva, ft = struct.unpack("<IIIII", desc)
        if name_rva == 0:
            break
        dll = cstr(f, rva_to_off(sections, name_rva))
        thunk_rva = oft or ft
        funcs = []
        toff = rva_to_off(sections, thunk_rva) if thunk_rva else None
        if toff is not None:
            idx = 0
            while idx < 4096:
                f.seek(toff + idx * 8)
                raw = f.read(8)
                if len(raw) < 8:
                    break
                val = struct.unpack("<Q", raw)[0]
                if val == 0:
                    break
                if val & (1 << 63):
                    funcs.append("#%d" % (val & 0xFFFF))
                else:
                    nm = rva_to_off(sections, val & 0x7FFFFFFF)
                    funcs.append(("+" + cstr(f, nm + 2)) if nm is not None else "?")
                idx += 1
        out.append({"dll": dll, "count": len(funcs), "funcs": funcs})
        off += 20
    return out


def thunk_names(f, sections, thunk_rva, limit=4096):
    """Walk an INT/IAT array (8-byte entries) and resolve names or ordinals."""
    funcs = []
    toff = rva_to_off(sections, thunk_rva) if thunk_rva else None
    if toff is None:
        return funcs
    idx = 0
    while idx < limit:
        f.seek(toff + idx * 8)
        raw = f.read(8)
        if len(raw) < 8:
            break
        val = struct.unpack("<Q", raw)[0]
        if val == 0:
            break
        if val & (1 << 63):
            funcs.append("#%d" % (val & 0xFFFF))
        else:
            nm = rva_to_off(sections, val & 0x7FFFFFFF)
            funcs.append(("+" + cstr(f, nm + 2)) if nm is not None else "?")
        idx += 1
    return funcs


def read_delay_imports(f, sections, rva, image_base):
    out = []
    off = rva_to_off(sections, rva)
    if off is None:
        return out
    while True:
        f.seek(off)
        desc = f.read(32)
        if len(desc) < 32:
            break
        (attrs, dll_name, hmod, iat, int_, biat, uit, tds) = struct.unpack("<8I", desc)
        if dll_name == 0:
            break
        rva_based = bool(attrs & 1)
        conv = (lambda v: v) if rva_based else (lambda v: v - image_base if v else 0)
        try:
            dll = cstr(f, rva_to_off(sections, conv(dll_name)))
        except Exception:
            dll = "<unreadable>"
        funcs = thunk_names(f, sections, conv(int_ or iat))
        out.append({"dll": dll, "rva_based": rva_based, "attributes": attrs, "funcs": funcs})
        off += 32
    return out


def section_entropy(f, s):
    """Sample up to 1 MiB per section to estimate entropy (packer heuristic)."""
    import math
    n = min(s["rawsize"], 1 << 20)
    if n <= 0:
        return 0.0
    f.seek(s["rawptr"])
    data = f.read(n)
    if not data:
        return 0.0
    freq = [0] * 256
    for b in data:
        freq[b] += 1
    ent = 0.0
    for c in freq:
        if c:
            p = c / len(data)
            ent -= p * math.log2(p)
    return round(ent, 2)


def read_exports(f, sections, rva, cap=400):
    off = rva_to_off(sections, rva)
    if off is None:
        return {"count": 0, "names": []}
    f.seek(off)
    hdr = f.read(40)
    if len(hdr) < 40:
        return {"count": 0, "names": []}
    (_ch, _ts, _maj, _min, name_rva, base, nfuncs,
     nnames, af_rva, an_rva, ao_rva) = struct.unpack("<IIHHIIIIIII", hdr)
    names = []
    noff = rva_to_off(sections, an_rva)
    if noff is not None:
        for i in range(min(nnames, cap)):
            f.seek(noff + i * 4)
            nr = struct.unpack("<I", f.read(4))[0]
            p = rva_to_off(sections, nr)
            if p is not None:
                names.append(cstr(f, p, 256))
    return {"dll_name": cstr(f, rva_to_off(sections, name_rva)) if rva_to_off(sections, name_rva) else "",
            "count": nfuncs, "named": nnames, "names": names}


def main(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(0x400)
        if head[:2] != b"MZ":
            raise SystemExit("not an MZ image")
        e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
        f.seek(e_lfanew)
        nt = f.read(0x108)
        if nt[:4] != b"PE\x00\x00":
            raise SystemExit("no PE signature at e_lfanew=%#x" % e_lfanew)
        machine, nsec, tds = struct.unpack_from("<HHI", nt, 4)
        opt_size = struct.unpack_from("<H", nt, 20)[0]
        opt = nt[24:24 + opt_size]
        magic = struct.unpack_from("<H", opt, 0)[0]
        pe32plus = magic == 0x20B
        entry = struct.unpack_from("<I", opt, 16)[0]
        image_base = struct.unpack_from("<Q", opt, 24)[0] if pe32plus else struct.unpack_from("<I", opt, 28)[0]
        subsystem = struct.unpack_from("<H", opt, 68)[0]
        dll_chars = struct.unpack_from("<H", opt, 70)[0]
        nrva_and_size_off = 108 if pe32plus else 92
        nrva = struct.unpack_from("<I", opt, nrva_and_size_off)[0]
        dd_off = nrva_and_size_off + 4
        dirs = []
        for i in range(min(nrva, 16)):
            rva, sz = struct.unpack_from("<II", opt, dd_off + i * 8)
            dirs.append((rva, sz))
        sec_off = e_lfanew + 24 + opt_size
        f.seek(sec_off)
        raw_secs = f.read(nsec * 40)
        sections = []
        for i in range(nsec):
            b = raw_secs[i * 40:(i + 1) * 40]
            name = b[:8].rstrip(b"\x00").decode("latin-1", "replace")
            vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", b, 8)
            chars = struct.unpack_from("<I", b, 36)[0]
            sections.append({
                "name": name, "va": va, "vsize": vsize,
                "rawptr": rawptr, "rawsize": rawsize,
                "chars": "%08x" % chars,
                "writable": bool(chars & 0x80000000),
                "executable": bool(chars & 0x20000000),
            })
        for s in sections:
            s["entropy"] = section_entropy(f, s)

        imports = read_imports(f, sections, dirs[1][0]) if dirs[1][0] else []
        delay = read_delay_imports(f, sections, dirs[13][0], image_base) if dirs[13][0] else []
        exports = read_exports(f, sections, dirs[0][0]) if dirs[0][0] else {"count": 0, "names": []}

    result = {
        "path": path,
        "size": size,
        "machine": "%04x" % machine,
        "arch": {0x8664: "x64", 0x14c: "x86", 0xaa64: "arm64"}.get(machine, "?"),
        "sections_count": nsec,
        "timestamp": tds,
        "optional_magic": "%04x" % magic,
        "image_base": hex(image_base),
        "entry_point_rva": hex(entry),
        "subsystem": subsystem,
        "is_dll": bool(dll_chars & 0x2000),
        "data_dirs": {
            "export": dirs[0], "import": dirs[1], "resource": dirs[2],
            "exception": dirs[3], "security": dirs[4], "reloc": dirs[5],
            "debug": dirs[6], "tls": dirs[9], "load_config": dirs[10],
            "iat": dirs[12], "delay_import": dirs[13],
            "clr": dirs[14],
        },
        "sections": sections,
        "imports": [{"dll": i["dll"], "count": i["count"], "funcs": i["funcs"]} for i in imports],
        "delay_imports": delay,
        "exports": exports,
    }
    return result


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else r"F:\SteamLibrary\steamapps\common\Call of Duty HQ\cod.exe"
    r = main(target)

    print("=" * 72)
    print("FILE      %s" % r["path"])
    print("SIZE      %.2f MB" % (r["size"] / 1048576.0))
    print("ARCH      %s (machine=%s)  dll=%s  subsystem=%d" % (r["arch"], r["machine"], r["is_dll"], r["subsystem"]))
    print("IMAGEBASE %s   ENTRY %s   SECTIONS %d" % (r["image_base"], r["entry_point_rva"], r["sections_count"]))
    print("-" * 72)
    print("DATA DIRECTORIES")
    for k, (rva, sz) in r["data_dirs"].items():
        print("  %-14s rva=%-10s size=%d" % (k, hex(rva), sz))
    print("-" * 72)
    print("SECTIONS")
    for s in r["sections"]:
        flags = ("W" if s["writable"] else "-") + ("X" if s["executable"] else "-")
        print("  %-10s va=%-9s vsize=%-9d rawsize=%-9d %s ent=%.2f"
              % (s["name"], hex(s["va"]), s["vsize"], s["rawsize"], flags, s["entropy"]))
    print("-" * 72)
    print("IMPORTS (%d dlls)" % len(r["imports"]))
    for i in r["imports"]:
        print("  %-32s %4d funcs" % (i["dll"], i["count"]))
    print("-" * 72)
    print("DELAY IMPORTS (%d dlls)" % len(r["delay_imports"]))
    for d in r["delay_imports"]:
        print("  %-32s rva_based=%s attrs=%#x" % (d["dll"], d["rva_based"], d["attributes"]))
        for fn in d["funcs"]:
            print("      %s" % fn)
    print("-" * 72)
    print("EXPORTS total=%d named=%d" % (r["exports"]["count"], r["exports"].get("named", 0)))
    for n in r["exports"]["names"][:60]:
        print("  %s" % n)
    print("=" * 72)

    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "cod-exe-recon.json")
    with open(out, "w", encoding="utf-8") as fp:
        json.dump(r, fp, indent=2)
    print("JSON ->", out)
