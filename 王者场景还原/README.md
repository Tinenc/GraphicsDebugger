# 王者场景还原 — 李白谪仙

从 `李白谪仙.rdc`（Vulkan 抓帧，121 draws，1920×1080）反解并在 Blender 5.2 LTS 中还原完整场景。

![预览](preview_render.png)

## 成果

| 项目 | 结果 |
|---|---|
| 网格物件 | **25 个**（10 角色部件 + 15 环境部件），全部带 2-3 套 UV |
| 贴图 | **54 张**，按角色分为 basecolor 14 / normal 9 / mask_packed 23 / lut_ramp 8 |
| 相机 | fovX 50.91° / fovY 29.97°，等效 37.82mm，宽高比 1.7782（≈1920/1080 自检通过） |
| 平行光 | 方向 `(-0.179, 0.175, -0.968)`，取自全部 10 个 1024² 阴影 pass 一致的光矩阵 |
| 成品 | `libai_scene_rebuilt.blend`（双灯光组可切换） |

## 流程

脚本都在 `pipeline/`，除 Blender 侧的两个外均通过
`qTinecmaTool.exe --python <脚本>` 驱动。

| 顺序 | 脚本 | 作用 |
|---|---|---|
| 1 | `scene_survey.py` | 枚举 121 个 draw，按索引数/顶点属性/RT 尺寸分类，挑出 mesh_asset |
| 2 | `probe_matrices.py` | 找 VS 常量缓冲里的仿射矩阵，判定哪个是 model-view（本例 `cb0._child1`） |
| 3 | `probe_verts.py` | 确认原始顶点在物体空间（每网格居中、extent 0.1–4） |
| 4 | `probe_light_cam.py` | 反推相机投影矩阵与太阳方向 |
| 5 | `export_scene.py` | 导出 FBX + PS 绑定贴图（复用 `batch_fbx_exporter_ExtraUV`） |
| 6 | `classify_textures.py` | 自写 PNG 解码器统计 RGB，给贴图定角色 |
| 7 | `export_reference.py` | 导出原始帧作比对基准 + 投影自检 |
| 8 | `blender_rebuild.py` | Blender 内组装（网格/材质/灯光/相机）并渲染预览 |
| 9 | `organize_assets.py` | 整理交付目录 |

`fbx_ascii_import.py` 是 8 的依赖；`打开场景_MCP.cmd` 双击可打开带 MCP 的成品场景。

## 三个关键技术判断

### 这是「角色展示帧」，不是大世界场景

用 `cb0._child1` 变换后，7 个主要部件的几何中心**全部重合在 (0,0,106)**；
换用 `_child2` 则散布成球面。这说明它是皮肤预览界面的展示帧，不存在可反推的世界空间。
→ 按原帧展示布局还原。

### 顶点属性必须显式映射

Vulkan/SPIR-V 反射出的属性名是无语义的 `_input0.._inputN`，而本例**全部是 float**。
通用的"float 就是 UV"启发式会把法线误判成 UV。改为按分量数与出现顺序显式映射
（第一个 ≥3 分量 float → POSITION，下一个 3 分量 → NORMAL，4 分量 → TANGENT，剩余 2 分量按序 → UV/UV2/UV3）。

### 坐标系换算的行列式必须为 +1

顶点已烘入相机空间，转 Blender 用：

```
x_b = -x
y_b =  z - 100.061
z_b =  y
```

两个易错点：
- `(x,y,z)->(x,z,y)` 这种纯轴交换 det = −1，是**镜像**，角色左右手会互换——看起来正立但已经错了。靠 `-x` 拉回真旋转。
- RenderDoc 按 Vulkan 原始行序导出图像，所以 `_reference/` 里的参考图**上下颠倒**（脚在上）。它不是还原目标，误当目标会把 Y 再翻一次。

## 已知环境坑

- **renderdoc-cli / renderdoccmd 在 agent 沙箱内必挂**（`0xC0000409`，故障模块 `tsbx.dll`），官方版同样挂 → 只能走 `qTinecmaTool --python`。
- **中文路径读不到 rdc**（ANSI 编码丢字符）→ 先复制到 ASCII 路径。
- **RenderDoc 1.44 用 `st.code != rd.ResultCode.Succeeded`**，旧的 `rd.ReplayStatus` 会 AttributeError 秒退且无输出。
- **Blender 不支持 ASCII FBX 导入** → 用 `fbx_ascii_import.py` 在 Blender 内直接解析（网格出自 GPU 索引缓冲，逐顶点读取无损）。
- **不要从 agent 会话内启动 GUI Blender**，进程会随命令返回被回收 → 用 `.cmd` 双击启动。

## Blender 版本要求：5.2 LTS

**必须用 `E:\blender-5.2.0-windows-x64\blender.exe`** —— BlenderMCP addon 只支持 5.0+，
4.5 装上 addon 也连不上 9876。脚本已同时兼容 4.x / 5.x，但 4.5 下会在日志里打 WARNING。

5.2 相对 4.5 的实测 API 差异（`pipeline/probe_api_52.py` 可复跑验证）：

| 项 | 4.5 | 5.2 | 脚本处理 |
|---|---|---|---|
| 渲染引擎 enum | `BLENDER_EEVEE_NEXT` | **只有 `BLENDER_EEVEE`** | `pick_render_engine()` 按可用项挑 |
| `Material.shadow_method` | 有 | **已移除** | `_set_alpha_cutout()` 改用 `use_transparent_shadow` + `surface_render_method='DITHERED'` |
| `Mesh.use_auto_smooth` | 有 | 已移除 | 未使用，无影响（走自定义法线） |
| `normals_split_custom_set` / `color_attributes` / `uv_layers.new` | OK | OK | 不变 |
| `use_nodes` | OK | OK 但告警将于 6.0 移除 | 暂留，6.0 前需改 |

5.2 重建结果与 4.5 一致：25/25 物件、多套 UV 保留、`hero eid260 forward(y) 5.52..6.27`。

### MCP 桥接（已烘进场景）

addon 的 `register()` **不开端口**——真正的开关是场景属性 `blendermcp_auto_start_server`，
且必须存进 `.blend`。已用 `pipeline/bake_mcp_autostart.py` 烘好，所以现在**双击
`打开场景_MCP.cmd` 打开即通 9876，不用点任何按钮**。

- 算子命名空间是 `bpy.ops.blendermcp.*`（无下划线），`bpy.ops.blender_mcp.*` 不存在
- `pipeline/check_mcp_addon.py` 可复验 addon 在当前 Blender 上能否 enable
  （注意 `--background` 下没有事件循环，端口不会真的监听，headless 验不了连通性）
- MCP 报 `[WinError 10053] Connection to Blender lost` 基本就是 Blender 没在跑

完整可复用流程已存为 skill：`~/.workbuddy/skills/rdc-scene-to-blender/`
