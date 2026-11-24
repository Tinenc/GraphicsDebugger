# RenderDoc MCP — v1.5 历史分享稿（Legacy）

> **历史资料，不再作为当前安装、工具数量或能力边界的依据。** 当前使用说明请看
> [README_CN.md](README_CN.md)，真实实现状态与限制请看
> [IMPLEMENTATION_STATUS.md](../docs/IMPLEMENTATION_STATUS.md)。当前 ReplayLab 1.8 源码注册
> 89 个 MCP 入口；高级语言源码只在捕获内嵌源码或受信外部工具成功重建时存在，不保证把
> 任意 DXBC、DXIL 或 SPIR-V 恢复为可编译 HLSL/GLSL。

> **历史定位：** 下文保留 v1.5 阶段的案例、早期文件 IPC 架构和 37 工具快照，用于说明
> 项目演进，不代表当前便携 ReplayLab Host、Codex 接入方式或 Shader 编辑能力。

---

## 一、项目背景 & 动机

### 痛点：手工分析 GPU 截帧太慢了

做手游渲染分析（如王者荣耀角色还原）时，传统流程是：

1. **打开 RenderDoc** → 手动翻 DrawCall 列表
2. **逐个点击** → 看 Pipeline State → 对着几十个纹理槽位猜"这个是什么"
3. **手动导出** → 右键保存贴图 → 手动重命名 → 手动判断用途
4. **Shader 分析** → 翻 SPIR-V 汇编 → 猜灯光模型 → 手抄参数
5. **导出模型** → 第三方插件 → 格式转换 → 导入 Unity → 手动配材质

一个角色的完整逆向分析，**少则 2-3 小时，多则半天**。

### 思路：把 RenderDoc 接到 AI 上

**MCP (Model Context Protocol)** 是 Anthropic 提出的开放协议，让 AI 可以调用外部工具。

如果我们把 RenderDoc 的分析操作封装成 MCP 工具，AI 就可以：
- "分析事件 73 的 Shader" → **读取实际可用的反汇编/反射 + 识别 PBR 材质 + 提取参数**
- "导出这个角色的模型和贴图" → **自动生成 OBJ/FBX + PNG + Unity 材质**
- "这帧画了什么" → **逐 DrawCall 截图 + 自动标注内容**

**从"手工翻截帧"到"对话式分析"，效率提升 10 倍以上。**

---

## 二、系统架构

```
┌─────────────┐                ┌──────────────────┐                ┌──────────────────┐
│ 早期 AI 客户端 │    stdio      │  MCP Server       │   File IPC     │  RenderDoc 扩展    │
│  （历史方案）  │ ◄──────────► │  (Python 3.10+)   │ ◄────────────► │  (Python 3.6)     │
│              │    JSON-RPC   │  server.py        │  JSON 文件交换   │  __init__.py      │
└─────────────┘                └──────────────────┘                └──────────────────┘
                                       │                                    │
                                 早期 37 个入口快照                   pyrenderdoc API
                                                                   (renderdoc.dll)
                                                                        │
                                                                  ┌─────┴──────┐
                                                                  │  GPU 截帧   │
                                                                  │ (.rdc 文件) │
                                                                  └────────────┘
```

### 为什么不能直接调用 pyrenderdoc？

RenderDoc 内置 Python 3.6，而现代 MCP Server 需要 Python 3.10+。两者**无法在同一个进程中运行**。

**解决方案：文件 IPC 桥接**

- Server 端写 `request.json` 到共享目录
- 扩展端（运行在 RenderDoc 内部）轮询、处理、写回 `response.json`
- 完全无端口、无网络，纯本地文件交换

这也是整个项目最大的工程难点之一——需要在两个 Python 版本之间保持 API 兼容性。

---

## 三、v1.5 工具快照（当前完整 89 入口见 README_CN）

| 类别 | 工具数 | 代表性工具 | 能力 |
|------|--------|-----------|------|
| 📦 截帧管理 | 6 | `launch_renderdoc` | 自动启动 RenderDoc、打开截帧、按文件大小动态等待 |
| 📊 帧信息 | 2 | `get_frame_info` | API 类型、帧号、Draw/Dispatch 统计 |
| 🎨 DrawCall | 2 | `set_event` | 在帧时间线上"跳转"到指定事件 |
| 🗂️ 资源检查 | 4 | `get_textures` | 列出所有纹理/缓冲区/资源，追踪使用方式 |
| 🔧 管线状态 | 1 | `get_pipeline_state` | 完整的 GPU 管线快照 |
| 💎 Shader 分析 | 4 | `reverse_shader` | 检查 RenderDoc 实际提供的反汇编/嵌入源码（如有）+ cbuffer 值 + 贴图角色识别；不保证高级语言反编译 |
| 🎯 贴图绑定 | 3 | `get_bound_textures` | 自动匹配 Albedo/Normal/Roughness 等角色 |
| 📤 导出 | 2 | `export_to_unity` | OBJ+FBX 模型 + Unity 材质映射 JSON |
| 🔍 智能识别 | 2 | `identify_drawcalls` | RT 累积截图 + Shader 分组精确标注 |
| 💡 灯光分析 | 1 | `analyze_lighting` | PBR 模型检测 + cbuffer 参数提取 |
| 🖼️ 纹理操作 | 3 | `save_texture` | 导出 PNG/DDS，CubeMap 自动拆面 |
| 🐛 调试 | 4 | `debug_pixel` / `pixel_history` | 逐步跟踪 Shader 执行 |
| ⚡ 性能 | 3 | `fetch_counters` | GPU 性能计数器 |

---

## 四、核心亮点 & 技术突破

### 4.1 Vulkan Descriptor Binding 解析 ⭐

这是整个项目最难的技术挑战。

**问题**：Vulkan/ANGLE 截帧中，标准 API `GetReadOnlyResources()` 返回的 `UsedDescriptor` 对象的 `.resource` 属性，`int()` 转换后得到的是 **SWIG 指针地址**（~3万亿），而不是 ResourceId。

**诊断过程**：
1. 写了 `_debug_vulkan_bindings` 诊断工具，深度 dump SWIG 对象的所有属性
2. 发现 `_introspect()` 函数把 SWIG 对象直接转了 int，掩盖了真实结构
3. 修复后，发现正确路径是 `binding.descriptor.resource`

**修复**（一行代码的差别）：
```python
# ❌ 旧代码（对 Vulkan 返回 SWIG 指针地址 ~3万亿）
rid = int(binding.resource)

# ✅ 新代码（正确路径：UsedDescriptor → Descriptor → resource: ResourceId）
if hasattr(binding, 'descriptor'):
    rid = int(binding.descriptor.resource)
```

**影响**：修复后，`get_bound_textures`、`export_drawcall`、`export_to_unity` 在 Vulkan 截帧上全部可用。

---

### 4.2 DrawCall 智能识别 ⭐

**传统方式**：AI 只能通过三角面数猜测 DrawCall 的内容——"722 三角形可能是眼球"——准确率极低。

**新方案**：`identify_drawcalls` 工具

核心原理：对每个 DrawCall **执行后截取 RT**（Render Target），对比相邻截图直接"看到"画了什么。

```
SetFrameEvent(eid_66) → SaveTexture(RT) → 只有脸（眼眶镂空）→ 头部皮肤
SetFrameEvent(eid_73) → SaveTexture(RT) → 红色虹膜填入       → 眼球 ✅
SetFrameEvent(eid_87) → SaveTexture(RT) → 黑色发丝出现       → 头发
```

**配合 Shader 分组**：相同 VS+FS shader ID = 同一材质类型，这比面数更可靠。

**验证结果**（王者荣耀关羽C皮肤）：
- 25 个 DrawCall → 25 张 RT 累积截图 → 100% 准确标注每个部件
- 自动区分：头部皮肤 / 眼球 / 头发 / 8个金色饰品 / 后发 / 后处理

---

### 4.3 模型导出：VBuffer 直读 + FBX + 坐标系修复 ⭐

**旧方案**：使用 `GetPostVSData(VSOut)` 读取变换后的顶点数据。Vulkan 下不稳定，经常读不到。

**v1.3.0 改进**：参考 Model Extractor 插件，改用 `GetVBuffers() + GetIBuffer() + GetVertexInputs()` 直接从 GPU 缓冲区读原始顶点数据。

| | 旧方案 (PostVS) | 新方案 (VBuffer) |
|---|---|---|
| 数据源 | 变换后的顶点输出 | GPU 顶点/索引缓冲区原始数据 |
| Vulkan 兼容性 | ❌ 不稳定 | ✅ 直接读缓冲区 |
| 格式支持 | 仅 Float32 | Float16/32, UNorm, SNorm, UInt/SInt |
| 输出格式 | 仅 OBJ | **OBJ + FBX 7.4 ASCII** |
| 导入兼容 | 需要额外转换 | Unity/Unreal/Blender 直接导入 |

**v1.5.0 质量修复**（6 个关键 bug）：

| # | 问题 | 修复 |
|---|------|------|
| 1 | 多余顶点（如 3543 vs 3346） | `baseVertex` + `indexOffset` 正确偏移 + 索引重映射 |
| 2 | UV 上下翻转 | `flip_uv_v` 默认关闭（DX11 → Unity 不需要翻转） |
| 3 | 模型左右镜像 | DX 左手 → 右手坐标系转换（X 取负 + 缠绕反转） |
| 4 | 法线完全错乱 | 压缩编码 tangent frame 自动检测并丢弃 |
| 5 | 顶点属性跨步错误 | 始终按原始 compCount 读取再截取 |
| 6 | R10G10B10A2 格式不支持 | 新增 10+10+10+2 位打包解码 |

**验证**：D3D11 抓帧 EID=320（3346 唯一顶点）→ OBJ + FBX 导出，UV/模型/法线在 Unity 中完全正确 ✅

---

### 4.4 灯光分析

`analyze_lighting` 工具从两个维度提取灯光信息：

**1. Shader 代码分析**：从 RenderDoc 实际提供的反汇编（该案例为 SPIR-V）中匹配关键词，启发式推断光照模型
- `GGX` / `Schlick` / `BRDF` → PBR
- `pow(NdotH, ...)` → Blinn-Phong
- `shadow_mapping` / `environment_reflection` / `subsurface_scattering` → 特性检测

**2. CBuffer 结构分析**：
- 提取所有 uniform 变量名、类型、数组大小
- 对 ANGLE/Vulkan 匿名变量 `_childN` 做启发式分类：
  - `float4[6]` → light_array
  - `float4[8]` → shadow_matrix
  - `float4[4]` → ambient_params

---

### 4.5 CubeMap 纹理自动拆面

修复前：`save_texture` 对 CubeMap 只导出第 0 面（+X），丢失 5/6 的数据。

修复后：
- **DDS 格式**：6 面合并为单文件（RenderDoc 原生支持）
- **PNG/JPG 等**：自动拆为 6 个面文件 `cubemap_face0_posX.png` ~ `face5_negZ.png`
- 新增 `slice` 参数：指定导出单面

---

## 五、实战演示：一句话分析关羽

### 场景：对一个王者荣耀角色做完整的渲染逆向分析

```
用户: "打开关羽的截帧，告诉我眼球用了什么 Shader 和贴图"
```

**AI 自动执行的操作链**：

```
1. launch_renderdoc("关羽截帧.rdc")     → 启动 RenderDoc + 加载截帧
2. identify_drawcalls()                  → 逐 DrawCall 截图 → 识别出眼球是 EID=73
3. reverse_shader(73, "fragment")        → 读取该捕获可用的 SPIR-V 反汇编/嵌入源码 → 10 个纹理 slot 映射
4. get_bound_textures(73, "fragment")    → 所有纹理 ResourceId + 角色自动标注
5. analyze_lighting(73)                  → PBR 光照模型 + 3 种 Shader 特性
6. export_to_unity(73, "output/eye")     → OBJ + FBX + 材质 JSON
7. save_texture(对每个纹理)              → 导出 Albedo/Normal/AO 等 PNG
```

**整个流程不到 1 分钟**。手动操作同样的事情需要 1-2 小时。

---

## 六、踩过的坑 & 经验教训

### 6.1 Python 版本鸿沟
RenderDoc 内置 Python 3.6，MCP 框架需要 3.10+。文件 IPC 桥接虽然简单但高效——比嵌入 RPC 框架（3.6 的 gRPC 已停止维护）稳定得多。

### 6.2 SWIG 对象陷阱
pyrenderdoc 的 Python 绑定基于 SWIG，很多对象的 `int()` 转换返回的是 **C++ 指针地址**而不是逻辑值。遇到"异常大的数字"时要警觉。

### 6.3 API 参数不一致
同一个函数在不同 RenderDoc 版本中参数个数不同：
- `LoadCapture`: 4 参数 vs 5 参数
- `GetCBufferVariableContents`: 6 参数 vs 8 参数
- `GetDisassemblyTargets()` vs `GetDisassemblyTargets(True)`

全靠 `try/except` 适配。

### 6.4 Vulkan/ANGLE 特殊性
通过 ANGLE 层截取的 Vulkan 截帧（如安卓模拟器）有很多独特问题：
- 所有纹理匿名（没有资源名称）
- Descriptor Set 绑定结构和 D3D11 完全不同
- CBuffer 变量名被混淆为 `_child0`, `_child1`...

需要启发式方法来推断语义。

### 6.5 DX 左手→右手坐标系（v1.5.0 新增）
DX11 使用左手坐标系，OBJ/FBX/Unity 使用右手坐标系。不做转换模型会在 X 轴镜像（左右反了）。
- 位置 X 取负、法线 X 取负
- 每个三角面缠绕方向反转（交换 v1/v2），否则面法线全部朝内
- **所有格式**（OBJ/FBX/PLY/glTF/CSV）都必须同步处理

### 6.6 NORMAL 语义不一定是法线（v1.5.0 新增）
很多游戏在 NORMAL 语义中存储**压缩编码的 tangent frame**——一个 uint32 打包 3×10 位数据，顶点着色器用位操作解码。直接当 float3 法线读，值会是巨大的整数，光照完全错乱。
- 解法：读取后检查范围，超出 [-1.5, 1.5] 就丢弃，让 Unity 自行计算

---

## 七、数据说话

| 指标 | 数值 |
|------|------|
| MCP 工具总数 | **历史 v1.5 快照为 37 个；当前源码为 89 个入口** |
| Python 代码行数 | 扩展端 ~2500 行 + Server 端 ~800 行 |
| 支持的图形 API | D3D11, D3D12, Vulkan, OpenGL |
| 支持的导出格式 | OBJ, FBX, PNG, JPG, BMP, TGA, HDR, EXR, DDS |
| 纹理角色自动识别 | Albedo, Normal, Roughness, Metallic, AO, Emissive, Depth, CubeMap |
| 模型导出方式 | VBuffer 直读（Vulkan 兼容）+ PostVS 回退 |
| 完整角色分析耗时 | <1 分钟（手动 1-2 小时） |

---

## 八、后续展望

1. **批量分析**：自动遍历帧内所有角色，批量导出模型 + 贴图 + 材质
2. **Shader 翻译实验**：在明确目标环境并通过编译、ABI 与 live replay gate 后评估转换；不承诺通用 SPIR-V → HLSL/ShaderLab 还原
3. **对比分析**：两帧之间的差异对比（A/B 测试渲染效果）
4. **实时预览**：在 AI 对话中嵌入 3D 模型预览

---

## 九、总结

RenderDoc MCP 把**手动翻截帧**变成了**对话式分析**。

核心价值：
- 🚀 **效率提升 10x+**：一句话完成过去 1-2 小时的分析工作
- 🔧 **历史兼容成果**：v1.5 的 Vulkan/ANGLE/D3D11 分析与导出路径修复；当前支持矩阵以实现状态文档为准
- 📦 **一键导出**：OBJ + FBX + PNG + Unity 材质 JSON，直接拖进引擎
- 🧠 **AI 赋能**：不只是工具调用，AI 可以理解渲染管线并给出分析结论

**让渲染分析从"技术活"变成"对话"。**

---

*历史归档：renderdoc-mcp v1.5.0 / 37 工具快照；当前版本与 89 个入口见 README_CN.md。*
