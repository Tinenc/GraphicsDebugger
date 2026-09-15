# -*- coding: utf-8 -*-
"""
Compare the car-body UVs of the two special skins (金属风暴 / 赛博超域)
against the base skin (原皮), using the uv_dump.json files produced by
headless_carbody_export.py.

Run with the system Python:
    py uv_compare.py

Outputs:
    <OUTPUT_ROOT>/UV_对比报告.md
    <OUTPUT_ROOT>/plots/*.png   (if matplotlib is available)
"""

import os
import sys
import json
import math

OUTPUT_ROOT = r"E:\GST\白金猎豹\carbody_export"
BASE = "原皮"
SKINS = ["金属风暴", "赛博超域"]

# Romanized labels so matplotlib does not need a CJK font.
ROMAN = {"原皮": "YuanPi (base)", "金属风暴": "JinShuFengBao", "赛博超域": "SaiBoChaoYu"}

CHANGE_EPS = 1e-4  # UV delta below this is considered "unchanged"


def load(skin):
    path = os.path.join(OUTPUT_ROOT, skin, "uv_dump.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def attr_is_float(dump, attr):
    for a in dump.get("attributes", []):
        if a["name"] == attr:
            return a.get("isFloat", False), a.get("compCount", 0)
    return False, 0


def uv_channels(dump):
    """Return ALL UV-like channels for a dump, from the vertex format itself.

    Every float vertex attribute carries texcoords:
      - 2-component float  -> one UV set (.xy)
      - 4-component float  -> two packed UV sets (.xy and .zw)
      - 3-component float  -> one UV set (.xy)
    Enumerated in vertex-layout order so the label/order is identical across
    captures. This is independent of the FBX mapper so every UV set is compared.
    """
    chans = []
    for a in dump.get("attributes", []):
        attr = a["name"]
        if attr not in dump.get("rawAttrs", {}):
            continue
        if not a.get("isFloat", False):
            continue
        cc = a.get("compCount", 0)
        if cc == 2:
            chans.append(("{0}".format(attr), attr, (0, 1)))
        elif cc >= 4:
            chans.append(("{0}.xy".format(attr), attr, (0, 1)))
            chans.append(("{0}.zw".format(attr), attr, (2, 3)))
        elif cc == 3:
            chans.append(("{0}.xy".format(attr), attr, (0, 1)))
    return chans


def get_uv_map(dump, attr, ci, cj):
    """idx -> (u, v) for the requested attribute/components."""
    idxs = dump["orderedIndices"]
    rows = dump["rawAttrs"][attr]
    out = {}
    for k, idx in enumerate(idxs):
        r = rows[k]
        u = r[ci] if ci < len(r) else 0.0
        v = r[cj] if cj < len(r) else 0.0
        out[idx] = (u, v)
    return out


def bbox_area(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if not xs:
        return (0, 0, 0, 0, 0.0)
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    return (minx, maxx, miny, maxy, (maxx - minx) * (maxy - miny))


def compare_channel(base_map, skin_map):
    common = sorted(set(base_map.keys()) & set(skin_map.keys()))
    deltas = []
    changed = 0
    for idx in common:
        bu, bv = base_map[idx]
        su, sv = skin_map[idx]
        d = math.hypot(su - bu, sv - bv)
        deltas.append(d)
        if d > CHANGE_EPS:
            changed += 1
    deltas.sort()
    n = len(deltas)
    if n == 0:
        return None
    mean = sum(deltas) / n
    mx = deltas[-1]
    p99 = deltas[min(n - 1, int(0.99 * n))]
    median = deltas[n // 2]
    return {
        "common": n,
        "changed": changed,
        "pct_changed": 100.0 * changed / n,
        "mean": mean,
        "median": median,
        "p99": p99,
        "max": mx,
        "identical": changed == 0,
    }


def try_matplotlib():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except Exception:
        try:
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "matplotlib"], check=True)
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            return plt
        except Exception as e:
            print("matplotlib unavailable, skipping plots:", e)
            return None


def scatter_plot(plt, plot_path, base_pts, skin_pts, base_label, skin_label, title):
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    bx = [p[0] for p in base_pts]; by = [p[1] for p in base_pts]
    sx = [p[0] for p in skin_pts]; sy = [p[1] for p in skin_pts]

    axes[0].scatter(bx, by, s=1, c="#888888")
    axes[0].set_title(base_label)
    axes[1].scatter(sx, sy, s=1, c="#cc3333")
    axes[1].set_title(skin_label)
    axes[2].scatter(bx, by, s=1, c="#888888", label=base_label, alpha=0.5)
    axes[2].scatter(sx, sy, s=1, c="#cc3333", label=skin_label, alpha=0.5)
    axes[2].set_title("overlay")
    axes[2].legend(markerscale=6, loc="upper right")
    for ax in axes:
        ax.set_aspect("equal", "box")
        ax.invert_yaxis()
        ax.grid(True, linewidth=0.3, alpha=0.3)
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(plot_path, dpi=90)
    plt.close(fig)


def main():
    dumps = {}
    for skin in [BASE] + SKINS:
        dumps[skin] = load(skin)

    base = dumps[BASE]
    plots_dir = os.path.join(OUTPUT_ROOT, "plots")
    os.makedirs(plots_dir, exist_ok=True)
    plt = try_matplotlib()

    lines = []
    def w(s=""):
        lines.append(s)

    w("# 兰博车体 UV 对比报告")
    w()
    w("对比对象：**金属风暴** / **赛博超域** 相对 **原皮**（base）。数据取自各抓帧车体 draw 的顶点输入（post-transform 前的原始 UV，翻转与否不影响对比）。")
    w()

    # ---- Topology / format overview ----
    w("## 1. 拓扑与顶点格式")
    w()
    w("| 皮肤 | EventID | 索引数 | 唯一顶点 | 顶点属性 | 主UV属性 |")
    w("|---|---|---|---|---|---|")
    for skin in [BASE] + SKINS:
        d = dumps[skin]
        attrs = ",".join(a["name"] for a in d["attributes"])
        w("| {0} | {1} | {2} | {3} | {4} | {5} |".format(
            skin, d["eventId"], d["indexCount"], d["uniqueVertexCount"], attrs,
            d["mapper"].get("UV", "")))
    w()

    base_idx = base["orderedIndices"]
    for skin in SKINS:
        same_topo = (dumps[skin]["orderedIndices"] == base_idx)
        w("- {0} vs 原皮：唯一顶点索引集合 **{1}**。".format(
            skin, "完全一致（可逐顶点对比）" if same_topo else "不一致（改用公共顶点子集对比）"))
    w()

    # ---- Per-channel UV comparison ----
    w("## 2. UV 通道差异")
    w()
    w("对每个 UV 候选通道，计算每顶点 UV 的欧氏偏移 |ΔUV|（阈值 {0} 视为未变）。".format(CHANGE_EPS))
    w()

    base_chans = {c[0]: c for c in uv_channels(base)}

    for skin in SKINS:
        w("### {0} vs 原皮".format(skin))
        w()
        w("| UV通道 | 公共顶点 | 变化顶点 | 变化占比 | 平均|ΔUV| | 中位 | p99 | 最大 | 结论 |")
        w("|---|---|---|---|---|---|---|---|---|")
        skin_chans = {c[0]: c for c in uv_channels(dumps[skin])}
        common_labels = [lab for lab in base_chans if lab in skin_chans]
        for lab in common_labels:
            _, ba, (bi, bj) = base_chans[lab]
            _, sa, (si, sj) = skin_chans[lab]
            base_map = get_uv_map(base, ba, bi, bj)
            skin_map = get_uv_map(dumps[skin], sa, si, sj)
            r = compare_channel(base_map, skin_map)
            if r is None:
                continue
            verdict = "完全一致" if r["identical"] else (
                "整体重排" if r["pct_changed"] > 50 else "局部改动")
            w("| {0} | {1} | {2} | {3:.1f}% | {4:.5f} | {5:.5f} | {6:.5f} | {7:.5f} | {8} |".format(
                lab, r["common"], r["changed"], r["pct_changed"],
                r["mean"], r["median"], r["p99"], r["max"], verdict))

            # bbox comparison
            bpts = list(base_map.values())
            spts = list(skin_map.values())
            bb = bbox_area(bpts); sb = bbox_area(spts)
            w()
            w("  - {0} UV 包围盒：base U[{1:.3f},{2:.3f}] V[{3:.3f},{4:.3f}] 面积{5:.3f} → {6} U[{7:.3f},{8:.3f}] V[{9:.3f},{10:.3f}] 面积{11:.3f}".format(
                lab, bb[0], bb[1], bb[2], bb[3], bb[4], skin, sb[0], sb[1], sb[2], sb[3], sb[4]))
            w()

            if plt is not None:
                safe = lab.replace("[", "_").replace("]", "").replace(".", "_").replace(" ", "")
                pname = "{0}_vs_原皮_{1}.png".format(skin, safe)
                ppath = os.path.join(plots_dir, pname)
                scatter_plot(plt, ppath, bpts, spts, ROMAN[BASE], ROMAN[skin],
                             "{0} vs base  {1}".format(ROMAN[skin], lab))
                w("  ![{0}](plots/{1})".format(pname, pname))
                w()
        w()

    # ---- Console summary ----
    print("\n".join(lines[:60]))

    report_path = os.path.join(OUTPUT_ROOT, "UV_对比报告.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\nReport written:", report_path)
    if plt is not None:
        print("Plots in:", plots_dir)


if __name__ == "__main__":
    main()
