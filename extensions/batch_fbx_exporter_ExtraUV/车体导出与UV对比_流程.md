# RenderDoc 车体网格导出（多套 UV FBX）+ UV 差异对比 · 流程手册

> 用途：从多个 `.rdc` 抓帧中，定位同一模型（如车体）的 draw call，导出带**多套 UV** 的 FBX + 贴图，并**逐顶点对比**不同皮肤/版本之间的 UV 差异。
>
> 本手册以「白金猎豹 · 兰博」三个皮肤（原皮 / 金属风暴 / 赛博超域）为实例，但流程可复用到任何模型。

---

## 0. TL;DR（最短路径）

```powershell
# 1) 编辑配置：打开 headless_carbody_export.py，改 OUTPUT_ROOT / CONFIG（rdc 路径 + EventID + 皮肤名）
# 2) 无界面导出（生成 FBX + 贴图 + uv_dump.json）
& "C:\Program Files\GraphicsDebugger\x64\Development\qTinecmaTool.exe" --python `
  "C:\Program Files\GraphicsDebugger\extensions\batch_fbx_exporter_ExtraUV\headless_carbody_export.py"

# 3) 编辑 uv_compare.py 顶部 BASE/SKINS（要对比谁）
# 4) 生成 UV 对比报告 + 散点图（用系统 Python）
py "C:\Program Files\GraphicsDebugger\extensions\batch_fbx_exporter_ExtraUV\uv_compare.py"
```

产物默认在 `E:\GST\白金猎豹\carbody_export\`：每皮肤一个子目录（`carbody.fbx` + `textures\` + `uv_dump.json`），外加 `UV_对比报告.md` 与 `plots\*.png`、`export_log.txt`。

---

## 1. 背景：为什么是「三个工具拼起来」

| 工具 | 运行环境 | 能干什么 | 不能干什么 |
|---|---|---|---|
| **RenderDoc MCP**（`renderdoc-mcp`） | 无界面服务 | 打开 rdc、列 draw、看管线/绑定、导贴图、导网格（**仅坐标**） | 不含 UV 导出；驱动不了 GUI 扩展 |
| **batch_fbx_exporter_ExtraUV** | qrenderdoc 图形界面扩展 | 导出多套 UV/法线/切线的 FBX | 要在界面里点按钮；MCP 调不了 |
| **headless_carbody_export.py**（本流程） | `qTinecmaTool.exe --python` | 复用扩展的导出逻辑，**无界面批量**导出多 UV FBX | —— |

关键点：
- MCP 用来**核实/定位** draw（"这一帧车体是哪个 EventID"）。
- 真正的 **FBX 导出**用 headless 脚本（复用扩展里的纯函数，不拉起 Qt 对话框）。
- **UV 对比**用系统 Python 读脚本 dump 的 JSON 来算。

### headless 原理
`qTinecmaTool.exe`（即 qrenderdoc）支持命令行 `--python <脚本>`：脚本在主界面打开**之前**运行，并被注入全局 `pyrenderdoc`。脚本结尾调用 `sys.exit(0)`（抛 `SystemExit`）时，qrenderdoc 检测到 `pythonExited=True` 就**跳过主界面直接退出**。脚本内用 `renderdoc` 模块（`x64\Development\pymodules\renderdoc.pyd`，Python 3.6）直接 `OpenCaptureFile → OpenCapture` 拿到 `ReplayController`，全程无界面。

---

## 2. 环境与前置

- **RenderDoc/TinecmaTool 构建产物**：`C:\Program Files\GraphicsDebugger\x64\Development\qTinecmaTool.exe`（含 `pymodules\renderdoc.pyd`、`python36.dll`）。
- **系统 Python**：任意 3.x（本机为 3.14），仅用于 `uv_compare.py`。首次跑若无 `pip`：
  ```powershell
  py -m ensurepip --upgrade
  py -m pip install matplotlib
  ```
- **脚本位置**：`C:\Program Files\GraphicsDebugger\extensions\batch_fbx_exporter_ExtraUV\`
  - `headless_carbody_export.py` —— 无界面导出
  - `uv_compare.py` —— UV 对比 + 出图
- **PowerShell 注意**：含空格路径必须双引号；分隔命令用 `;` 不是 `&&`。

---

## 3. 步骤一：用 MCP 定位/核实车体 draw

目的：确认每个 rdc 里"车体"对应的 **EventID**（以及顶点数、绑定贴图），避免导错对象。

RenderDoc MCP 常用工具：

| 工具 | 作用 |
|---|---|
| `open_capture {path}` | 打开一个 rdc（会关掉上一个）。返回 API / 总 draw 数 / 总 event 数 |
| `list_draws {limit}` | 列出所有 draw（含 `eventId`、`numIndices`、flags）—— **按索引数找同一网格的利器** |
| `get_draw_info {eventId}` | 某 draw 的详情（索引数、实例数、输出 RT）。若返回 "not found" 说明该 EventID 不是 draw |
| `get_bindings {eventId}` | 该 draw 各着色器阶段的 CB / 贴图绑定 |
| `get_pipeline_state {eventId}` | 管线状态、RT 格式、视口等 |

**定位技巧**：同一模型在不同抓帧里 **索引数（`numIndices`）通常完全一致**。先在一个 rdc 里确认车体的索引数，再用 `list_draws` 在其它 rdc 里按相同索引数找对应 draw。

> 本实例：车体索引数固定为 **45678**。原皮=EventID 416、金属风暴=427、赛博超域=636。
> ⚠️ 坑：不同抓帧的 EventID **不通用**（赛博超域没有 427 这个 draw，实际车体在 636）。**务必用索引数/贴图核对，别盲信一个 EventID**。

---

## 4. 步骤二：headless 导出 FBX（多套 UV）+ 贴图

### 4.1 改配置
编辑 `headless_carbody_export.py` 顶部：

```python
OUTPUT_ROOT = r"E:\GST\白金猎豹\carbody_export"
CONFIG = [
    {"skin": "原皮",     "rdc": r"E:\GST\白金猎豹\兰博-原皮.rdc",     "eventId": 416},
    {"skin": "金属风暴", "rdc": r"E:\GST\白金猎豹\兰博-金属风暴.rdc", "eventId": 427},
    {"skin": "赛博超域", "rdc": r"E:\GST\白金猎豹\兰博-赛博超域.rdc", "eventId": 636},
]
APPLY_MATRIX = True      # 应用自动查找的世界/视图矩阵（只影响位置/法线，不影响 UV）
TRANSPOSE_MATRIX = True
EXPORT_TEXTURES = True   # 同时导出 PS 绑定贴图为 PNG
```

### 4.2 运行
```powershell
& "C:\Program Files\GraphicsDebugger\x64\Development\qTinecmaTool.exe" --python `
  "C:\Program Files\GraphicsDebugger\extensions\batch_fbx_exporter_ExtraUV\headless_carbody_export.py"
```
（约每帧几秒；不会弹界面。若看不到 stdout，是正常的——日志写在 `OUTPUT_ROOT\export_log.txt`。）

### 4.3 自动属性映射（重点：多套 UV 的来龙去脉）
脚本按**顶点格式**自动推断语义（Vulkan/SPIR-V 里属性名多为 `_inputN`，无语义提示）：

- **POSITION**：第一个 ≥3 分量的属性（优先 float32；否则回退，如打包的 `SNorm16x4`）。
- **UV（可多套）**：所有 **float** 属性都视为纹理坐标——
  - 2 分量 float → 1 套 UV
  - **4 分量 float → 2 套打包 UV**（`.xy` 和 `.zw`）
  - 3 分量 float → 1 套（取 `.xy`）
  - 按顶点布局顺序依次填 `UV / UV2 / UV3 / UV4 / UV5`（最多 5 套）。
- **NORMAL / TANGENT**：剩余的非 float ≥3 分量属性（`SNorm` 打包法线/切线）。
- **COLOR**：4 分量 `UNorm` 字节属性。

> ⚠️ **这是"只有一套 UV"问题的根因**：早期把 4 分量 float 的 `_input5` 误判成法线，导致 FBX 只有 `_input6` 一套。现已修正为「float 属性一律当 UV，4 分量拆两套」。
>
> 本实例最终映射（三帧一致）：
> `UV=_input5.xy`（map1，主图集 0..1）、`UV2=_input5.zw`（map2，第二套 0..1）、`UV3=_input6`（map3，带平铺，范围超 0..1）。
>
> FBX 内会写 3 个 `LayerElementUV` 块 → Blender 的 UV Maps 里显示 3 个图层。

导出时终端/日志会打印 `Inferred mapper:`，**务必核对**推断是否合理（尤其哪几套是真 UV）。

### 4.4 产物
```
OUTPUT_ROOT\
  export_log.txt
  <皮肤>\
    carbody.fbx          # 多套 UV + 法线/切线/顶点色；已应用世界矩阵
    uv_dump.json         # 每顶点每属性原始值（供对比，UV 未翻转）
    textures\tex_*.png   # PS 绑定贴图
```

`uv_dump.json` 字段：`indexCount`、`uniqueVertexCount`、`orderedIndices`、`attributes`（含 `compCount`/`compType`/`isFloat`）、`mapper`、`rawAttrs`（每属性逐唯一顶点值）、`uvChannels`。

### 4.5 校验 FBX 的 UV 套数
```powershell
# 用 ripgrep 数 LayerElementUV 块（= UV 套数）
rg -c "LayerElementUV:" "E:\GST\白金猎豹\carbody_export\原皮\carbody.fbx"
```

---

## 5. 步骤三：UV 差异对比 + 出图

### 5.1 改配置
编辑 `uv_compare.py` 顶部：
```python
OUTPUT_ROOT = r"E:\GST\白金猎豹\carbody_export"
BASE  = "原皮"                      # 基准
SKINS = ["金属风暴", "赛博超域"]     # 要和基准比的皮肤
CHANGE_EPS = 1e-4                    # |ΔUV| 小于此值算"未变"
```

### 5.2 运行
```powershell
py "C:\Program Files\GraphicsDebugger\extensions\batch_fbx_exporter_ExtraUV\uv_compare.py"
```

### 5.3 对比逻辑
- **通道枚举**（`uv_channels()`）：按顶点格式列出**所有** UV 通道（2 分量→1；4 分量→`.xy`+`.zw`；3 分量→`.xy`），**与 mapper 无关**，保证每套 UV 都比到。
- **逐顶点对齐**：三帧拓扑一致时 `orderedIndices` 相同，按同一唯一顶点索引逐点比 `|ΔUV|=√(Δu²+Δv²)`；不一致则退回公共顶点子集。
- 统计每通道：公共顶点数、变化顶点数/占比、平均/中位/p99/最大 `|ΔUV|`、UV 包围盒(U/V 范围+面积)。
- **散点图**：每套 UV 每个对比一张三联图（基准 / 皮肤 / overlay 叠加）。

### 5.4 怎么读结果
报告 `UV_对比报告.md` 每行一个 UV 通道：

| 列 | 含义 |
|---|---|
| 变化顶点 / 变化占比 | 超阈值的顶点数 / 百分比。**0 = 完全没动** |
| 平均/中位/p99/最大 \|ΔUV\| | 偏移量统计，全 0 = UV 逐顶点相同 |
| 结论 | `完全一致` / `局部改动`(<50%) / `整体重排`(>50%) |
| 包围盒行 | U/V 范围 + 面积，看是否被整体缩放/平移 |

散点图看 **overlay**：灰(基准)红(皮肤)完全重合=一致；错位/翻转/搬岛=UV 变了。（Y 轴已翻转、等比，所见即真实 UV 岛屿。）

> 本实例结论：金属风暴、赛博超域相对原皮，**三套 UV 全部逐顶点完全一致**（max |ΔUV|=0）。进一步校验位置/法线/顶点色也逐字节相同 → 三皮肤共用同一网格与同一套 UV，差异只在**贴图/材质**（赛博超域另多一条顶点流 `_input1`）。

---

## 6. 参数 / 命令速查

| 想做什么 | 改哪里 |
|---|---|
| 换模型/换抓帧 | `headless_carbody_export.py` 的 `CONFIG` |
| 换输出目录 | 两个脚本的 `OUTPUT_ROOT`（保持一致） |
| 不要世界变换（导物体空间） | `APPLY_MATRIX = False` |
| 不导贴图 | `EXPORT_TEXTURES = False` |
| 换对比基准/对象 | `uv_compare.py` 的 `BASE` / `SKINS` |
| 调"算不算变"的灵敏度 | `uv_compare.py` 的 `CHANGE_EPS` |
| 手动指定 UV/法线映射 | 修改 `infer_mapper()`（或按需硬编码 `mapper`） |

---

## 7. 常见坑 / FAQ

- **导入 Blender 只有一套 UV**：多半是自动映射把某个 4 分量 float（打包 UV）当成了法线。检查 `export_log.txt` 的 `Inferred mapper`，确认所有 float 属性都归到 `UV/UV2/...`。
- **某 rdc 里 EventID "not found"**：EventID 不跨抓帧通用。用 `list_draws` 按索引数重新定位。
- **`qTinecmaTool.exe --python` 弹出了主界面**：脚本没走到 `sys.exit(0)`（可能中途异常）。看 `export_log.txt` 的 traceback。
- **系统 Python 报 `No module named pip`**：先 `py -m ensurepip --upgrade`。
- **matplotlib 装不上/没图**：脚本会自动尝试 `pip install matplotlib`；仍失败则只出报告不出图。
- **PowerShell 输出中文乱码**：仅显示层问题，写入文件的内容是正常 UTF-8；用 Read/编辑器看文件即可。
- **世界矩阵找错（`auto_find`）**：脚本挑"末列最接近 (0,0,0,1)"的 4x4；可能选到 view 而非 world，但对单个物体的形状/UV 对比无影响。要精确 world 矩阵可在脚本里指定 set/binding/variable。
- **贴图对不上皮肤**：`textures\` 里文件名是资源 ID（`tex_<id>.png`）；不同皮肤 ID 不同，需结合 `get_bindings` 的 bindPoint 判断哪张是 basecolor/normal 等。

---

## 8. 相关文件

- 导出脚本：`extensions/batch_fbx_exporter_ExtraUV/headless_carbody_export.py`
- 对比脚本：`extensions/batch_fbx_exporter_ExtraUV/uv_compare.py`
- 原 GUI 扩展（导出逻辑来源）：`extensions/batch_fbx_exporter_ExtraUV/__init__.py`
- MCP 工具描述符：`~/.cursor/projects/.../mcps/user-renderdoc-mcp/tools/*.json`
