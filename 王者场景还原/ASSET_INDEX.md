# 李白谪仙 — 场景还原资产索引

由 `李白谪仙.rdc`（Vulkan，121 draws，1920×1080）反解还原。

坐标：顶点已烘入相机空间→Blender 变换 `(x,y,z) -> (-x, z-100.061, y)`，相机位于原点朝 +Y。

## 01_character

| EID | 部件 | 顶点 | 三角面 | UV 套数 | BaseColor | 法线 |
|---|---|---|---|---|---|---|
| 260 | body_robe_main | 8551 | 12710 | 2 | basecolor_slot0_tex_150913.png | normal_slot3_tex_151321.png |
| 279 | sleeve_left | 2242 | 3194 | 2 | basecolor_slot0_tex_150777.png | normal_slot2_tex_151155.png |
| 562 | sash_and_belt | 1865 | 2631 | 2 | basecolor_slot1_tex_150040.png | normal_slot3_tex_150045.png |
| 692 | skirt_lower | 1627 | 2219 | 2 | basecolor_slot0_tex_149993.png | normal_slot3_tex_150057.png |
| 274 | hair_topknot | 1128 | 2048 | 2 | basecolor_slot0_tex_150350.png | - |
| 253 | cloud_fx_ribbon | 1309 | 1963 | 2 | basecolor_slot0_tex_150067.png | normal_slot3_tex_150875.png |
| 246 | collar_shoulder | 543 | 780 | 2 | basecolor_slot0_tex_151428.png | normal_slot3_tex_150305.png |
| 647 | head_face | 3024 | 5388 | 3 | basecolor_slot0_tex_150203.png | normal_slot4_tex_149935.png |
| 267 | hair_strands | 740 | 872 | 3 | basecolor_slot0_tex_150355.png | normal_slot6_tex_151326.png |
| 569 | hair_detail_small | 82 | 114 | 3 | - | - |

## 02_environment

| EID | 部件 | 顶点 | 三角面 | UV 套数 | BaseColor | 法线 |
|---|---|---|---|---|---|---|
| 365 | rock_platform_a | 2124 | 2118 | 2 | - | - |
| 353 | rock_platform_b | 1854 | 1848 | 2 | - | - |
| 334 | rock_platform_c | 1416 | 1412 | 2 | - | - |
| 322 | rock_platform_d | 1236 | 1232 | 2 | - | - |
| 338 | rock_edge_a | 828 | 822 | 2 | - | - |
| 377 | rock_edge_b | 810 | 806 | 2 | - | - |
| 407 | rock_edge_c | 810 | 806 | 2 | - | - |
| 389 | rock_edge_d | 732 | 728 | 2 | - | - |
| 423 | rock_edge_e | 732 | 728 | 2 | - | - |
| 307 | rock_small | 552 | 548 | 2 | - | - |
| 699 | skirt_shadowcaster | 1627 | 2219 | 1 | basecolor_slot0_tex_149993.png | - |
| 429 | fx_card_a | 405 | 704 | 2 | - | - |
| 456 | fx_card_b | 405 | 704 | 2 | - | - |
| 601 | sky_board_a | 244 | 360 | 1 | basecolor_slot0_tex_151094.png | - |
| 719 | sky_board_b | 244 | 360 | 1 | basecolor_slot0_tex_151094.png | - |

## 03_textures_by_role

| 角色 | 数量 | 判据 |
|---|---|---|
| mask_packed | 23 | 存在死通道，或近灰度打包图 |
| basecolor | 14 | 饱和度 ≥ 0.18，且多来自 slot 0/1 |
| normal | 9 | RG≈0.5 且 B≥0.85（切线空间法线，不依赖饱和度） |
| lut_ramp | 8 | 极小尺寸渐变查找表 |

## 04_reference

抓帧自身的渲染目标，用于逐像素比对。

**注意**：RenderDoc 按原始行序导出 Vulkan 图像，所以参考图是上下颠倒的——它不是还原目标的正确朝向。

## 05_scene

- `libai_scene_rebuilt.blend` — 成品场景（25 物件 + 双灯光组 + 相机）
- `preview_render.png` — EEVEE 预览渲染
- `scene.json` / `textures_manifest.json` — 逐 draw 元数据与贴图分类

相机：fovX 50.91° / fovY 29.97°，等效 37.82mm（36mm 片幅），1920×1080。
太阳：方向取自全部 10 个 1024² 阴影 pass 一致的光矩阵。

灯光组：`Light_Showcase`（默认启用，提亮）与 `Light_CaptureNight`（原帧夜景），在大纲视图切换可见性即可。

---

统计：角色部件 10 个，环境部件 15 个，贴图 54 张。