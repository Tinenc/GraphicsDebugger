#!/usr/bin/env python3
"""List export ordinal -> name mapping of a PE (read-only).

Usage: export_ordinals.py <path-to-dll> [filter-substr]
"""
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main(path, filt=""):
    with open(path, "rb") as f:
        head = f.read(0x400)
        if head[:2] != b"MZ":
            raise SystemExit("not MZ: " + path)
        e = struct.unpack_from("<I", head, 0x3C)[0]
        f.seek(e)
        nt = f.read(0x118)
        if nt[:4] != b"PE\x00\x00":
            raise SystemExit("not PE: " + path)
        nsec = struct.unpack_from("<H", nt, 6)[0]
        optsz = struct.unpack_from("<H", nt, 20)[0]
        opt = nt[24:24 + optsz]
        p32 = struct.unpack_from("<H", opt, 0)[0] == 0x20B
        dd = (108 if p32 else 92) + 4
        exp_rva, exp_sz = struct.unpack_from("<II", opt, dd + 0)
        f.seek(e + 24 + optsz)
        raw = f.read(nsec * 40)
        secs = []
        for i in range(nsec):
            b = raw[i * 40:(i + 1) * 40]
            vs, va, rs, rp = struct.unpack_from("<IIII", b, 8)
            secs.append((va, max(vs, rs), rp))

        def off(rva):
            for va, sz, rp in secs:
                if va <= rva < va + sz:
                    return rp + (rva - va)
            return None

        def rd(rva, n):
            o = off(rva)
            if o is None:
                return b""
            f.seek(o)
            return f.read(n)

        if not exp_rva:
            print("no export directory")
            return
        h = rd(exp_rva, 40)
        base = struct.unpack_from("<I", h, 16)[0]
        nf = struct.unpack_from("<I", h, 20)[0]
        nn = struct.unpack_from("<I", h, 24)[0]
        an = struct.unpack_from("<I", h, 32)[0]
        ao = struct.unpack_from("<I", h, 36)[0]
        print("%s  base=%d functions=%d named=%d" % (path, base, nf, nn))
        rows = []
        for i in range(nn):
            nr = struct.unpack("<I", rd(an + i * 4, 4))[0]
            o2 = off(nr)
            f.seek(o2)
            s = b""
            while True:
                c = f.read(1)
                if c in (b"\x00", b""):
                    break
                s += c
            name = s.decode("latin-1")
            ordi = struct.unpack("<H", rd(ao + i * 2, 2))[0]
            rows.append((base + ordi, name))
        for o, n in sorted(rows):
            if not filt or filt.lower() in n.lower():
                print("  %-5d %s" % (o, n))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
