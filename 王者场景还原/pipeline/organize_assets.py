# -*- coding: utf-8 -*-
"""
Assemble the final, tidy deliverable tree from everything the pipeline made.

Run with plain python (no RenderDoc / Blender needed):
    python organize_assets.py

Layout produced under scene_export/_deliverable/ :

    01_character/          10 hero parts, FBX + the textures they use
    02_environment/        rock + sky/fx boards
    03_textures_by_role/   every texture, filed by its classified role
    04_reference/          the capture's own framebuffer, for A/B comparison
    05_scene/              .blend, preview render, scene metadata
    ASSET_INDEX.md         human-readable index of the whole thing
"""

import json
import os
import shutil

ROOT = r"E:\GST\libai_scene"
EXPORT = os.path.join(ROOT, "scene_export")
DELIV = os.path.join(EXPORT, "_deliverable")

CHAR_GROUPS = ["01_hero", "02_hero_extraUV"]
ENV_GROUPS = ["03_props", "04_bg_board"]

# friendly names worked out from the preview render + bbox sizes
PART_NAMES = {
    260: "body_robe_main",
    279: "sleeve_left",
    562: "sash_and_belt",
    692: "skirt_lower",
    274: "hair_topknot",
    253: "cloud_fx_ribbon",
    246: "collar_shoulder",
    647: "head_face",
    267: "hair_strands",
    569: "hair_detail_small",
    365: "rock_platform_a",
    353: "rock_platform_b",
    334: "rock_platform_c",
    322: "rock_platform_d",
    338: "rock_edge_a",
    377: "rock_edge_b",
    407: "rock_edge_c",
    389: "rock_edge_d",
    423: "rock_edge_e",
    307: "rock_small",
    699: "skirt_shadowcaster",
    429: "fx_card_a",
    456: "fx_card_b",
    601: "sky_board_a",
    719: "sky_board_b",
}


def ensure(d):
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def copy(src, dst):
    if os.path.isfile(src):
        shutil.copy2(src, dst)
        return True
    return False


def main():
    if os.path.isdir(DELIV):
        shutil.rmtree(DELIV)
    ensure(DELIV)

    scene = json.load(open(os.path.join(EXPORT, "scene.json"), encoding="utf-8"))
    manifest = json.load(open(os.path.join(EXPORT, "textures_manifest.json"),
                              encoding="utf-8"))

    tex_by_eid = {}
    for e in manifest:
        for eid in e.get("eids", []):
            tex_by_eid.setdefault(int(eid), []).append(e)

    lines = ["# 李白谪仙 — 场景还原资产索引", ""]
    lines.append("由 `李白谪仙.rdc`（Vulkan，121 draws，1920×1080）反解还原。")
    lines.append("")
    lines.append("坐标：顶点已烘入相机空间→Blender 变换 "
                 "`(x,y,z) -> (-x, z-100.061, y)`，相机位于原点朝 +Y。")
    lines.append("")

    counts = {"char": 0, "env": 0, "tex": 0}

    # ---------- 01 character / 02 environment ----------
    for bucket, groups in (("01_character", CHAR_GROUPS),
                           ("02_environment", ENV_GROUPS)):
        bdir = ensure(os.path.join(DELIV, bucket))
        lines.append("## {0}".format(bucket))
        lines.append("")
        lines.append("| EID | 部件 | 顶点 | 三角面 | UV 套数 | BaseColor | 法线 |")
        lines.append("|---|---|---|---|---|---|---|")

        for g in groups:
            for it in (scene.get("groups") or {}).get(g, []):
                eid = int(it["eventId"])
                fbx = it.get("fbx")
                if not fbx:
                    continue
                src = os.path.join(EXPORT, g, fbx)
                nice = PART_NAMES.get(eid, "part")
                newname = "eid{0}_{1}.fbx".format(eid, nice)
                if not copy(src, os.path.join(bdir, newname)):
                    continue

                # the textures this part actually samples, next to the mesh
                tdir = ensure(os.path.join(bdir, "textures"))
                bc = nm = "-"
                for e in sorted(tex_by_eid.get(eid, []),
                                key=lambda x: x.get("slot", 99)):
                    p = e.get("path")
                    if not p or not os.path.isfile(p):
                        continue
                    tn = "{0}_slot{1}_{2}".format(e.get("role", "tex"),
                                                  e.get("slot", 0),
                                                  os.path.basename(p))
                    copy(p, os.path.join(tdir, tn))
                    if e.get("role") == "basecolor" and bc == "-":
                        bc = tn
                    if e.get("role") == "normal" and nm == "-":
                        nm = tn

                mapper = it.get("mapper") or {}
                nuv = len([k for k in ("UV", "UV2", "UV3", "UV4", "UV5")
                           if mapper.get(k)])
                lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} |".format(
                    eid, nice, it.get("uniqueVerts", "?"),
                    int(it.get("numIndices", 0)) // 3, nuv, bc, nm))
                counts["char" if bucket == "01_character" else "env"] += 1
        lines.append("")

    # ---------- 03 textures by role ----------
    tdir = ensure(os.path.join(DELIV, "03_textures_by_role"))
    role_counts = {}
    for e in manifest:
        p = e.get("path")
        if not p or not os.path.isfile(p):
            continue
        role = e.get("role", "unknown")
        rdir = ensure(os.path.join(tdir, role))
        name = e.get("sortedName") or os.path.basename(p)
        if copy(p, os.path.join(rdir, name)):
            role_counts[role] = role_counts.get(role, 0) + 1
            counts["tex"] += 1

    lines.append("## 03_textures_by_role")
    lines.append("")
    lines.append("| 角色 | 数量 | 判据 |")
    lines.append("|---|---|---|")
    why = {
        "basecolor": "饱和度 ≥ 0.18，且多来自 slot 0/1",
        "normal": "RG≈0.5 且 B≥0.85（切线空间法线，不依赖饱和度）",
        "mask_packed": "存在死通道，或近灰度打包图",
        "lut_ramp": "极小尺寸渐变查找表",
    }
    for r, c in sorted(role_counts.items(), key=lambda kv: -kv[1]):
        lines.append("| {0} | {1} | {2} |".format(r, c, why.get(r, "-")))
    lines.append("")

    # ---------- 04 reference ----------
    rdir = ensure(os.path.join(DELIV, "04_reference"))
    refsrc = os.path.join(EXPORT, "_reference")
    if os.path.isdir(refsrc):
        for fn in os.listdir(refsrc):
            copy(os.path.join(refsrc, fn), os.path.join(rdir, fn))
    lines.append("## 04_reference")
    lines.append("")
    lines.append("抓帧自身的渲染目标，用于逐像素比对。")
    lines.append("")
    lines.append("**注意**：RenderDoc 按原始行序导出 Vulkan 图像，"
                 "所以参考图是上下颠倒的——它不是还原目标的正确朝向。")
    lines.append("")

    # ---------- 05 scene ----------
    sdir = ensure(os.path.join(DELIV, "05_scene"))
    for src, dst in (
        (os.path.join(ROOT, "libai_scene_rebuilt.blend"), "libai_scene_rebuilt.blend"),
        (os.path.join(ROOT, "out", "preview.png"), "preview_render.png"),
        (os.path.join(EXPORT, "scene.json"), "scene.json"),
        (os.path.join(EXPORT, "textures_manifest.json"), "textures_manifest.json"),
        (os.path.join(EXPORT, "textures_report.txt"), "textures_report.txt"),
        (os.path.join(ROOT, "out", "rebuild_log.txt"), "rebuild_log.txt"),
    ):
        copy(src, os.path.join(sdir, dst))

    lines.append("## 05_scene")
    lines.append("")
    lines.append("- `libai_scene_rebuilt.blend` — 成品场景（25 物件 + 双灯光组 + 相机）")
    lines.append("- `preview_render.png` — EEVEE 预览渲染")
    lines.append("- `scene.json` / `textures_manifest.json` — 逐 draw 元数据与贴图分类")
    lines.append("")
    lines.append("相机：fovX 50.91° / fovY 29.97°，等效 37.82mm（36mm 片幅），1920×1080。")
    lines.append("太阳：方向取自全部 10 个 1024² 阴影 pass 一致的光矩阵。")
    lines.append("")
    lines.append("灯光组：`Light_Showcase`（默认启用，提亮）与 "
                 "`Light_CaptureNight`（原帧夜景），在大纲视图切换可见性即可。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("统计：角色部件 {0} 个，环境部件 {1} 个，贴图 {2} 张。".format(
        counts["char"], counts["env"], counts["tex"]))

    with open(os.path.join(DELIV, "ASSET_INDEX.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("deliverable at {0}".format(DELIV))
    print("character={char} environment={env} textures={tex}".format(**counts))


if __name__ == "__main__":
    main()
