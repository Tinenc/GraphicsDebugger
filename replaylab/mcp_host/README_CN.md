# ReplayLab MCP Server

一个面向 RenderDocUltra 的 [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) 服务器：让 AI 智能体分析 GPU 截帧、检查渲染状态、提取 Shader 证据、调用宿主预登记的外部 Shader 工具、自动匹配贴图并分析性能。反编译源码只是候选表达，不保证恢复原始 HLSL/GLSL。

本目录是 ReplayLab 的 Python 3.10+ 边车 Host，不是 qrenderdoc 插件。
RenderDocUltra 由原生 C++ 管理 UI 与生命周期，并从固定应用目录
`integrations/replaylab/python/renderdoc_mcp_bridge` 加载第一方 Python Bridge。
Bridge 通过源码和构建系统固定选择，直接调用 `start`/`stop`，不再经由
`extension.json` 或用户扩展目录发现。

qrenderdoc 项目 UI 只使用经过认证的 `ReplayLabInternal.exe` 高层服务。本目录负责的
项目管理、恢复与 Shader 保存如果无法访问内部服务，会直接失败关闭，绝不回退为启动本
可执行文件。普通 qrenderdoc 默认持有一个 `ReplayLabMCP.exe` 回环服务，供外部 Codex、
IDE 或自动化客户端连接；设置 `REPLAYLAB_EXTERNAL_MCP_AUTOSTART=0`（也接受 `false`、
`no`、`off`）只会关闭这项外部服务。ACP/Cursor 只为当前任务启动一个受过滤的 stdio
适配器。所有带捕获的 AgentHost 工作（包括 deepagents 子 Agent、ACP 和 Codex
app-server）都通过受过滤的任务级 MCP 路由调用同一个 Host。

[English](README.md) | **中文**

---

## 功能概览（141 个工具）

### 📦 截帧管理
| 工具 | 说明 |
|------|------|
| `load_capture` | 加载 `.rdc` 截帧文件 |
| `close_capture` | 关闭已加载的截帧 |
| `list_captures` | 列出所有已加载的截帧 |
| `switch_capture` | 在多个截帧之间切换 |
| `launch_renderdoc` | 🆕 **启动 RenderDoc 并打开 .rdc 文件**：自动查找 qrenderdoc，启动后等待 MCP 桥接就绪 |
| `debug_vulkan_bindings` | 🆕 **诊断 Vulkan descriptor 绑定**：dump 原始 descriptor set 数据，排查纹理导出问题 |

### 📊 帧信息 & 图形 API
| 工具 | 说明 |
|------|------|
| `get_api_properties` | 图形 API 类型（D3D11/D3D12/Vulkan/GL）、渲染器、降级状态 |
| `get_frame_info` | 帧号、时间戳、Draw/Dispatch/调用统计 |

### 🎨 绘制调用
| 工具 | 说明 |
|------|------|
| `get_actions` | 获取帧内所有绘制调用、Dispatch、Marker（支持层级结构） |
| `set_event` | 将回放定位到指定事件，后续所有查询反映该事件后的状态 |

### 🗂️ 资源检查
| 工具 | 说明 |
|------|------|
| `get_textures` | 列出所有纹理（尺寸、格式、Mip 级别、数组大小等） |
| `get_buffers` | 列出所有缓冲区（大小、创建标志） |
| `get_resources` | 列出**全部**资源（纹理、缓冲区、着色器、管线对象等） |
| `get_resource_usage` | 追踪某个资源在帧内的使用方式 |

### 🔧 管线状态
| 工具 | 说明 |
|------|------|
| `get_pipeline_state` | 查看当前绑定的着色器、渲染目标、深度目标、视口 |
| `analyze_current_frame_flow` | 有界的整帧时间线、工作量、物理 I/O、参与状态、绘制状态和确定资源交接清单 |
| `capture_event_texture_evidence` | 将 exact EID 的一个 RTV 合成/单通道视图或 raw DSV 深度视图导出为 Host 管理且带哈希的 PNG/manifest；调用方不能指定路径 |
| `render_current_frame_flow_diagram` | 从 Host 帧证据绘制固定风格的资源流、实际参与阶段或 Pass/GBuffer SVG；调用方不能提交图数据或样式 |

两个 Event 可视化工具均属于可逆的 `workspace` 操作。纹理证据每次只导出一个
视图，绑定精确 mip/slice/sample、受固定预算约束，并在结束前恢复原可见 EID；DSV
只显示 raw depth，不冒充线性深度。流程图只使用 Host 当前帧事实和固定
`evidence-neutral-v1` 风格，截帧文本不能注入 CSS、链接、HTML、Mermaid 指令或输出
路径。GBuffer 卡片只陈述物理附件，通道语义仍需经过验证的 Shader 证据。

### 💎 着色器基础检查
| 工具 | 说明 |
|------|------|
| `get_shader_reflection` | 着色器反射信息（输入/输出、常量缓冲区、纹理、采样器） |
| `disassemble_shader` | 获取着色器反汇编（DXBC、SPIR-V、GLSL 等） |
| `get_all_disassembly_targets` | 列出所有可用的反汇编语言目标 |

### 🔮 着色器逆向还原
| 工具 | 说明 |
|------|------|
| `reverse_shader` | **一站式着色器检查**：RenderDoc 能提供的反汇编/嵌入源码、反射数据、**常量缓冲区实时值**、贴图角色与采样器；不保证能够反编译为 HLSL/GLSL |
| `get_shader_source` | 仅在 RenderDoc 暴露嵌入源码或高级表示时返回内容；结果可能为空 |

### 🧪 外部 Shader Workspace
| 工具 | 说明 |
|------|------|
| `get_shader_toolchain_capabilities` | 重新校验已配置工具哈希，并返回路线/验证器诊断矩阵 |
| `plan_shader_toolchain_route` | 检测输入并唯一规划“二进制 → 可编辑文本 → 原二进制格式”；歧义时拒绝 |
| `plan_shader_recovery` | 将失败分类为检测、反编译、编译、ABI 等固定阶段，并只返回哈希绑定的未尝试路线配对供 AI 选择 |
| `run_shader_toolchain_round_trip_smoke` | 用固定路线、物理解析出的精确 profile、格式检查与验证器执行 smoke，不应用到 RenderDoc |
| `get_current_shader_representation` | 为 RenderDoc 能反射的任意 Shader API 生成非空、Hash 绑定的当前事件解释产物；解释/二进制 lane 永远不可编译或应用 |
| `prepare_shader_workspace` | 导出单个事件/阶段，确认唯一物理 Shader，默认只返回绑定工作区及可用路线，不运行反编译；显式传入 `attempt_reconstruction=true` 才启用原有的一步式重建和 RenderDoc 回退 |
| `create_shader_workspace` | 从已有 catalog 和精确 `shaderKey` 创建 workspace |
| `migrate_shader_workspace_toolchain` | 当 catalog/registry 合法升级时，以旧路线和当前源码 SHA 为 CAS，在新工作区原样保留源码并重新绑定当前工具链；不导入旧候选/ABI |
| `try_shader_workspace_recovery_candidate` | 只按原 workspace 的哈希绑定 recovery plan 候选索引，在新的 sibling workspace 中尝试另一条已注册路线；不能提供命令、路径或 executable，失败证据不会覆盖 |
| `decompile_shader_workspace` | 调用 workspace 绑定的反编译器，且不会覆盖已编辑源码 |
| `get_shader_workspace_representation` | 返回可执行路由文本，否则物化 RenderDoc IR 或精确二进制回退；不会再次运行已记录失败的路线，也不会静默改变用户请求的可编辑格式 |
| `promote_shader_workspace_explanation` | 显式提升经 Hash 复核的 `explanation/promoted.hlsl`；仍必须重新编译、ABI 校验并获得 Apply 授权 |
| `compile_shader_workspace` | 把路由绑定的 HLSL/汇编编译为可审计候选；**不会应用到 RenderDoc** |
| `get_shader_workspace_status` | 校验哈希并报告源码、修订和候选状态 |
| `begin_shader_workspace_organization` | 从当前源码创建或恢复可编辑整理草稿；此步不编译、不做 ABI 校验、不替换回放 |
| `finish_shader_workspace_organization` | 默认将当前提案保存为着色器工作区的可编辑源码；可选完整验证或编译并应用到回放，应用时重新检查 ABI 和当前目标；成功保存后可选生成流程图 |
| `validate_shader_workspace_candidate` | 重新反射合法组合的精确候选（D3D11/DXBC；D3D12/DXBC 或 DXIL），并要求完整 ABI 完全一致 |
| `apply_shader_workspace_candidate` | 重新校验 ABI、当前捕获/事件/原始字节后，应用 D3D11 事件级替换 |
| `rollback_shader_workspace_override` | 清除一个事件级替换并释放其持有的 target shader |
| `list_shader_workspace_family_uses` | 分页列出使用同一捕获 Shader RID 与 ABI 的可信事件 |
| `apply_shader_workspace_candidate_family` | 对共享同一捕获 Shader RID 的多个事件事务化应用一个共享 target |
| `rollback_shader_workspace_override_family` | 事务化回滚一组事件引用，并仅在最后一个引用移除后释放 target |
| `apply_shader_workspace_shared_resource` | 默认编辑范围：构建一次并替换原始 Shader RID 的所有捕获引用 |
| `rollback_shader_workspace_shared_resource` | 撤销并注销工具拥有的资源替换，仅释放新建 target，保留原 Shader |
| `fingerprint_event_outputs` | 在精确 EID 对全部 MRT/depth 子资源做字节哈希并采集有界确定性像素网格；仅作输出 A/B 观察证据，不宣称语义证明 |
| `start_shader_workspace_watch` | 只监听 manifest 绑定的源码/IR（如 `shader.hlsl`、`shader.dxbc.asm`、`shader.dxil.ll`），消抖并编译未应用候选 |
| `poll_shader_workspace_watch` | 读取有界监听/审计状态以及是否已追上最新源码 |
| `stop_shader_workspace_watch` | 停止安排新编译；已运行的编译器会安全完成 |
| `start_shader_workspace_live_edit` | 启动“稳定保存 → 编译 → 新鲜 ABI → 共享 Shader 替换”闭环；失败保留最后一个成功 replacement |
| `poll_shader_workspace_live_edit` | 返回保存、编译、ABI、Apply、恢复及 last-good replacement 的有界审计状态 |
| `list_shader_workspace_live_edits` | 列出活动会话、持久授权及 Host 重启恢复结果 |
| `stop_shader_workspace_live_edit` | 停止自动处理新保存，并可按参数回滚该会话持有的 replacement |

命令和路径只能来自进程所有者配置的 registry，详见
[外部 Shader Workspace](docs/SHADER_WORKSPACE.md)。编译与监听仍保持未应用；只有显式或
进程所有者授权的受支持 apply 才会在重新执行严格 ABI/实时事件校验后修改 replay。
默认共享模式支持本机 D3D11/native DXBC 与 D3D12/native DXBC|DXIL，并保持同一捕获
Shader RID 在所有 Draw 中的共享关系。D3D12 会用捕获的 Root Signature 预构建并校验所有
受影响 graphics/compute PSO，任一失败都不会提交 replacement。事件/族 Shader 替换仍仅
支持 D3D11；shadow-CBuffer/material session 支持本地 D3D11，以及本地 D3D12 的受支持
Root CBV/descriptor-table CBV。原 Shader 始终保留用于回滚。
监听请求不能提供 command、executable、args、registry 或 environment。
Live-edit 启动是绑定精确 workspace/capture/shader 身份的持久授权；单例 Host 重启后会恢复，
显式 stop 会撤销。旧 catalog/registry 改变时原工作区仍失败关闭，必须迁移到新的 revision-0
工作区，不能原地改 manifest。
精确 profile 提取、DXBC/DXIL/SPIR-V 失败即关闭的自动路由、可信配置生成、验证器和
round-trip smoke 报告见 [Shader 工具链路由](docs/SHADER_TOOLCHAINS.md)。任何路线都不承诺
恢复原始高级语言源码。可选的便携 `d3dcompiler_47.dll` 固定适配器能在目标机没有
SDK/`fxc.exe` 时把现有 HLSL 编译成 DXBC，但不提供反编译、include 或 ABI 豁免。
默认不再把“想编辑 Shader”等同于“必须得到 HLSL”：程序先尝试高级语言完整往返。
Shader Model 6 DXIL 可优先使用本地源码构建并哈希固定的 RenoDX `decomp.exe` 直接重建
可读 HLSL；社区 `HLSL-Decompiler` 的 DXIL 路线本质上是
`dxil-spirv -> SPIRV-Cross`，只作为独立候选后备，不能因为输出可编译就宣称算法一致。
若没有完整 DXIL→HLSL 往返，而 DXC dump 与官方 `IDxcAssembler` 都通过哈希校验，程序会
采用 `shader.dxil.ll` 可执行 IR 通道。已选反编译路线在运行时失败时，程序会自动按顺序
执行 Hash 绑定的有限 recovery candidate 路线对；每个替代工具都在新的 workspace 中只尝试
一次并保留失败证据，不能提供命令或在原 workspace 中偷换路线。所有固定反编译路线均失败
但仍有编译路线时，会基于 RenderDoc 原生 IR/精确二进制发布 provisional 源码入口，供 Agent
重建后进行 Hash 提升、编译和新鲜 replay ABI 校验，而不是直接结束任务。所有候选仍保持
`semanticEquivalenceVerified=false`，直到另有足够的语义证据。SPIR-V 汇编要等 workspace
实际接通固定工具与 validator 后才会进入自动 native-IR 回退。

### 🖥️ ReplayLab 通用控制台

`Tools > ReplayLab > Console` 聚合工程、捕获、Pass、材质、资源、工具链及 Shader 的本地
运行消息，并提供普通、警告、错误三类计数和独立筛选。“清理所有消息”仅清空 UI 视图，
不删除持久授权、安全审计或追加日志。真实编译诊断保留可点击的文件/行/列；没有源码位置的
ABI、回放和文件持久化错误不会伪造第 1 行。Console 的遥测写入始终是 best-effort，失败不能
改变业务结果；Live-edit 的权威审计仍独立执行原子写入和 fail-closed 校验。

`CBuffer` 出现在反射面板，只能证明绑定号、总大小或某些展示字段可见，并不自动证明每个
成员的物理 offset/type/stride 都完整可靠。字段缺失、DXC 明确输出
`type annotation not present`，或展示字段与 DXIL 实际 load 类型矛盾时，布局标记为
`ambiguous`；这不是“名字不可信”，而是“成员类型/偏移没有充分物理证据”。CBuffer 与字段名
可在候选 HLSL 中自定义，但 register/space、总字节数、packoffset、标量类型/宽度和实际访问
必须保持。程序会优先用 DXIL 物理访问签名（space/bind、字节访问区间、类型、次数）做严格
ABI 对齐，不能证明的语义名仍保持 provisional，AI 不得虚构成员。完整捕获目录可用
`get_active_capture_catalog_binding_status` 只读确认精确 `catalogSha256` 绑定。

### 🗃️ 项目级多帧复用

普通空闲启动 ReplayLab/qrenderdoc 时会先显示 `ReplayLab Projects` 项目首页；命令行或文件
关联直接打开 `.rdc` 时不会被拦截。也可随时从 `File -> ReplayLab Projects…` 返回。项目首页
支持在指定路径新建项目、添加已有项目、只移除本机项目记录而保留目录、配置解包资产目录、
添加/移除多个 RDC 引用，并直接双击带内嵌缩略图的帧卡片打开。RDC 保留在原路径，不会被
复制或删除；打开前
会先切换同一个 active-project selection，因此 Shader workspace、CBuffer 别名、资产匹配、
材质/Pass 知识和流程图继续使用同一项目边界。新建项目会自动以不可变 workspace ID 作为内部
项目边界，因此新建窗口不再要求 game/build/platform/API；捕获目录、Shader 接口/布局和新鲜
ABI 校验仍是项目内物理复用的严格门禁。

项目列表、新建/导入/设置/删除记录/添加 RDC，以及打开帧后的项目自动关联，都在有界后台
工作池中执行；窗口关闭或当前帧变化后，迟到结果会被丢弃。添加 RDC 时只登记绝对路径、
引用 ID 与文件名，然后立即排队读取容器内嵌缩略图，不再流式读取整个 RDC。外部直接打开时
仅在该绝对路径只属于一个已登记项目时自动切换；零个或多个匹配都会保持当前项目。旧的内容
身份记录仍可读取，但不再参与关联。新建项目会先在隐藏 staging 中完整生成再原子发布；
selection 提交失败时会恢复旧选择并隔离半成品，项目列表不会看到它。

| 工具 | 说明 |
|------|------|
| `create_replay_project` | 创建一个明确的游戏/构建/平台/API 知识边界 |
| `get_replay_project_status` | 校验内部 Hash 并汇总项目清单 |
| `import_replay_project_capture` | 导入严格 capture catalog，生成跨帧候选 |
| `register_replay_project_workspace` | 内容寻址保存 HLSL 与可选的解释性 `ai/analysis.md` |
| `register_replay_project_alias_table` | 把已验证、绑定捕获的别名保存为候选 |
| `save_replay_project_material_knowledge` | 从实时材质会话保存显式选择的类型化值 |
| `get_replay_project_reuse_plan` | 按 raw hash，或 code + 完整 interface + layout 匹配 |
| `seed_shader_workspace_from_replay_project` | 只把匹配 HLSL 写入新且未编辑的 workspace；不编译、不应用 |

项目绝不复用旧 workspace manifest、ABI 报告、replay/session 句柄或 apply
授权。`captureId` 是 capture path/API/Shader 字节集合的“捕获族”身份，不是完整反射目录
内容哈希；同一族的每次完整目录以规范化 `catalogSha256` 保存为项目自有、不可变修订。
managed workspace 路径采用 `v2/<workspaceBindingSha256>`；该摘要一次性绑定 capture、
catalog 修订、route binding、stage、资源 ID 与 Shader 原始哈希，避免 Windows 工具无法打开
多个完整摘要串接出的超长路径。`routeBindingSha256` 仍单独绑定精确路线目录、工具 registry
与自动格式策略，配置变化不会静默复用旧 workspace。manifest、别名和知识注册也绑定精确 catalog 修订；旧修订重导
不会暗中切换当前 head，多修订但缺少 revision provenance 的注册会以歧义失败关闭。完整流程与边界见
[项目级多帧复用](docs/REPLAY_PROJECTS.md)。

### 🎯 贴图自动对应
| 工具 | 说明 |
|------|------|
| `get_bound_textures` | **自动映射**着色器纹理槽位 → 实际绑定的纹理，并**智能推断贴图用途**（漫反射/法线/金属度/粗糙度/AO/自发光等） |
| `get_bound_samplers` | 获取每个采样器槽位的状态（滤波模式、寻址模式） |

### 📋 常量缓冲区实时值
| 工具 | 说明 |
|------|------|
| `get_cbuffer_contents` | 读取着色器常量缓冲区的**实际运行时数值**（浮点数、整数、矩阵、颜色等） |

### 🎚️ D3D11/D3D12 事件级材质参数
| 工具 | 说明 |
|------|------|
| `prepare_material_session` | 从精确事件/阶段/反射块建立不可变 CBuffer snapshot；保留 ring-buffer offset 与完整绑定窗口 |
| `list_material_sessions` | 列出当前捕获生命周期仍有效的材质会话 |
| `get_material_session_status` | 查看参数 ID、类型、当前值、revision 与 Hash |
| `update_material_parameter` | 只接受参数 ID/唯一名称和类型化数值；重建完整 shadow CBuffer 并立即重放该事件 |
| `rollback_material_session` | 清除事件级 shadow binding 并释放 snapshot |
| `clear_all_material_sessions` | 显式清除当前捕获的全部 shadow/snapshot，用于 Host 崩溃或重启后的恢复 |

偏移、类型、register 和原始 Buffer 字节不属于 MCP 参数；完整安全约束与当前限制见
[D3D11/D3D12 事件级材质参数会话](docs/MATERIAL_SESSIONS.md)。

qrenderdoc 现已提供原生 MVP：打开 Pipeline State 的 CBuffer 变量视图，右键一个受支持
字段并选择 `ReplayLab -> Expose as Material Parameter`。停靠面板显示完整物理身份，
类型化 scalar/vec 编辑在短暂合并后立即重放精确事件，并提供逐 CBuffer/全部 rollback。
目前不是带语义范围的滑条或颜色选择器。D3D12 root constants、ray state object 与
`ExecuteIndirect` Root CBV 会失败关闭；remote replay 也不支持。

### 📤 受管资源导出
| 工具 | 说明 |
|------|------|
| `get_resource_export_capabilities` | 报告固定 glTF、Unity URP、Blender、可选 FBX 与校验器能力，不会临时安装工具 |
| `inspect_resource_export` | 无写入地检查一个精确 EID/instance/view |
| `prepare_resource_export` | 在进程自有目录中快照完整网格输入、贴图、采样器、CBuffer 与 Shader 证据 |
| `finalize_resource_export` | 执行类型化迁移规格，原子发布 glTF、目标适配器和证据旁车 |
| `get_resource_export_status` | 查看稳定 operation 及其哈希 |
| `validate_resource_export` | 复核路径约束、文件哈希、网格/包结构和可用目标校验 |

`export_drawcall`、`export_to_unity` 与调用方路径版 `save_texture` 仍是 legacy
诊断工具，不会授予资源导出专员；它们不能保留全部顶点输入、精确 texture
view/sampler/CBuffer 证据、显式坐标策略、原子发布和目标验收。详见
[资源导出工具链](../docs/RESOURCE_EXPORT_TOOLCHAIN.md)。

### Unity 项目连接

`start_unity_project_connection` 准备随包的 MCP/Editor 包，打开或复用对应的图形化
Editor。`call_unity_project_tool` 自动恢复缺少或中断的项目连接；按返回的任务继续
观察，写入丢失回复时先检查实际结果。详见 [Unity 连接准备与恢复](docs/UNITY_CONNECTION.md)。

### 🧭 解包资产知识、UEX 报告与 Blender 交付
| 工具 | 说明 |
|------|------|
| `get_unpacked_asset_index_capabilities` | 报告固定的通用/UEX 感知解析、身份、关系和扫描上限 |
| `get_active_unpacked_asset_index_status` | 不扫描目录，只读取项目自有资产目录快照 |
| `index_active_unpacked_assets` | 对活动项目已配置根执行原子全量/增量索引；调用方不能传数据库路径 |
| `query_active_unpacked_assets` | 有界查询资产身份、哈希、格式、元数据及已验证/未解析关系 |
| `resolve_active_unpacked_asset` | 对目录中选中的资产重新哈希并返回绑定快照版本的交付回执 |
| `get_unpacked_asset_provider_capabilities` | 报告注册的 UEX、profiles、包装器、Blender、UEFormat 指纹和能力缺口 |
| `get_unpacked_asset_provider_decisions` | 从已提交 UEX 库读取有界、去敏的 profile 名和 Host 拥有的 Strong/完整/截断 Inventory 决策 |
| `query_unpacked_asset_inventory_assets` | 从精确 Inventory 有界查询安全资产身份与已验证导出包路径 |
| `start_unpacked_asset_report` | 用 Host 规范化并暂存的 v1 分组规格与 Host 验证的 Strong、完整、未截断 Inventory 启动 UEX 报告 |
| `start_unpacked_asset_blender_export` | 将有界的精确 cooked SkeletalMesh 集合导出为 UEX Bundle 和可选 Blender 装配 |
| `continue_unpacked_asset_blender_export` | 绑定一个终态 partial 回执，保留已验证父 Bundle，仅重试失败/未开始项，再合并装配并验证；不接受调用方旧路径 |
| `get_unpacked_asset_job_status` | 轮询幂等报告/导出作业并验证终态产物 |

资产目录持久化在活动项目的共享资产字典中，明确区分确定性关系、启发式候选与未解析引用。
报告必须通过 HTML/JSON/CSV/manifest 四件套及精确哈希验收；Blender 交付必须验证
`USkeletalMesh` 身份、LOD0、材质/贴图文件、`.uemodel` 或显式 GLB 路由、装配绑定和
验证回执。UEX/Blender 路径、profiles、秘密与输出根均由进程所有者配置，模型不可传入。
详见[解包资产知识专员](../docs/UNPACKED_ASSET_INTELLIGENCE.md)。

### 🔍 智能识别
| 工具 | 说明 |
|------|------|
| `identify_drawcalls` | 🆕 **智能识别每个 DrawCall 的渲染内容**：逐 DrawCall 截取 RT 缩略图、Shader 分组、纹理绑定、屏幕空间包围盒和覆盖率 |
| `analyze_lighting` | 🆕 **灯光分析**：检测 PBR/Blinn-Phong 光照模型、提取 cbuffer 灯光参数、识别阴影贴图和环境 CubeMap |

### 🖼️ 纹理操作
| 工具 | 说明 |
|------|------|
| `pick_pixel` | 读取纹理上指定像素的 RGBA 值 |
| `get_texture_minmax` | 获取纹理中的最小/最大像素值 |
| `save_texture` | 导出纹理为 PNG/JPG/BMP/TGA/HDR/EXR/DDS 格式 |

### 📝 缓冲区数据
| 工具 | 说明 |
|------|------|
| `get_buffer_data` | 读取缓冲区原始数据（十六进制 + Base64） |

### 🔍 像素历史
| 工具 | 说明 |
|------|------|
| `pixel_history` | 查看截至当前选中事件的像素修改历史（不自动等于完整整帧） |

### 🐛 着色器调试
| 工具 | 说明 |
|------|------|
| `debug_pixel` | 逐步跟踪像素着色器执行过程 |
| `debug_vertex` | 逐步跟踪顶点着色器执行过程 |

### ⚡ 性能计数器
| 工具 | 说明 |
|------|------|
| `enumerate_counters` | 列出可用的 GPU 性能计数器 |
| `fetch_counters` | 获取指定计数器的值 |

### 📐 网格数据
| 工具 | 说明 |
|------|------|
| `get_post_vs_data` | 获取顶点着色器后的网格输出信息 |

### 🚨 调试消息
| 工具 | 说明 |
|------|------|
| `get_debug_messages` | 获取图形驱动的验证错误/警告/信息 |

---

## 前置条件

1. **RenderDocUltra v1.44**（内置集成声明的最低版本）
2. 使用发行包内的 `ReplayLabMCP.exe` 时不需要另装 Python
3. 只有源码开发外部 MCP 才需要 **Python 3.10+**；文件 IPC 隔离了运行时，
   因此不必与 RenderDoc 内置 Python 版本一致

## 推荐安装：独立 RenderDoc AI 产品

发行包已经包含固定路径的内置 Bridge、`replaylab-ai.exe`、
`ReplayLabInternal.exe` 与 `ReplayLabMCP.exe`，无需运行额外安装脚本。RenderDoc AI 只
读取自己的 Settings、profiles 与凭据，不会读取或修改 `~/.codex/config.toml`，也不
要求安装外部 Codex skill。项目管理、恢复与 Shader 保存走 `ReplayLabInternal.exe`；
只有外部 Codex、IDE 或自动化客户端才需要显式连接 `ReplayLabMCP.exe stdio`。

从源码构建或部署发行运行时时，请使用 `replaylab/packaging/` 中受测试约束的
`Build-ReplayLabRuntime.ps1` 与 `Deploy-ReplayLabRuntime.ps1`，具体参数见该目录的
`README.md`。

若只做源码开发，再使用：

```powershell
cd replaylab/mcp_host
pip install -e .
```

## 源码开发配置（发行包用户可跳过）

设置 `RENDERDOC_MODULE_PATH` 环境变量，指向 RenderDoc Python 模块所在目录：

```bash
# Windows（典型安装路径）
set RENDERDOC_MODULE_PATH=C:\Program Files\RenderDoc

# Linux
export RENDERDOC_MODULE_PATH=/usr/lib/renderdoc
```

### 外部 MCP 客户端配置（可选）

添加到 MCP 配置文件（如 `~/.workbuddy/mcp.json`）：

```json
{
  "mcpServers": {
    "replaylab": {
      "command": "python",
      "args": ["-m", "src.server"],
      "cwd": "/path/to/renderdocUltra/replaylab/mcp_host",
      "env": {
        "RENDERDOC_MODULE_PATH": "C:\\Program Files\\RenderDoc"
      }
    }
  }
}
```

或者使用安装后的脚本：

```json
{
  "mcpServers": {
    "replaylab": {
      "command": "renderdoc-mcp",
      "env": {
        "RENDERDOC_MODULE_PATH": "C:\\Program Files\\RenderDoc"
      }
    }
  }
}
```

---

## 使用示例

使用 Codex 时不必手写下面的工具调用。打开一帧、在 Event Browser 里选中 EID 后，可以
直接说“解释当前绘制”“反编译并在外部工作区打开当前 PS”或“列出当前材质可安全调节的
参数”。Codex 会先读取当前窗口、capture、EID 和 stage 上下文，再调用相应工具。下面的
显式调用主要用于自动化和排错。

### 基础流程
```
1. load_capture("path/to/capture.rdc")     → 加载截帧
2. get_actions()                             → 查看所有绘制调用
3. set_event(142)                            → 定位到第 142 号事件
4. get_pipeline_state()                      → 查看绑定了哪些着色器和渲染目标
5. get_textures()                            → 列出所有纹理
6. pick_pixel(texture_id=5, x=100, y=200)   → 读取像素值
7. pixel_history(texture_id=3, x=50, y=50)  → 查看像素修改历史
8. save_texture(texture_id=5, output_path="rt0.png")  → 导出纹理
```

### 🔮 着色器逆向还原
```
1. set_event(142)                            → 定位到目标绘制调用
2. reverse_shader(142, "fragment")           → 检查像素着色器：
                                                - 可用源码/反汇编（不保证存在高级语言源码）
                                                - 所有绑定贴图 + 自动识别的角色
                                                - 常量缓冲区实际值（矩阵、颜色等）
                                                - 完整反射数据
3. get_shader_source(142, "vertex")          → 仅在 RenderDoc 提供时获取源码/高级表示
4. get_bound_textures(142, "fragment")       → 查看：槽位 0 = 漫反射, 槽位 1 = 法线, 槽位 2 = PBR 贴图...
5. get_cbuffer_contents(142, "fragment", 0)  → 读取材质参数（颜色、粗糙度、金属度的实际值）
```

### 📤 一键导出 DrawCall
```
1. export_drawcall(142, "C:/output/drawcall_142")
   → 自动创建文件夹，包含：
      fragment_HLSL.txt                      （仅当存在高级语言目标时）
      fragment_DXBC.txt                      （字节码反汇编）
      vertex_HLSL.txt                        （仅当存在高级语言目标时）
      fragment_slot0__MainTex_albedo.png     （漫反射贴图）
      fragment_slot1__BumpMap_normal.png     （法线贴图）
      fragment_slot2__MetallicMap_metallic.png （金属度贴图）
      fragment_slot3__OcclusionMap_ao.png    （环境光遮蔽贴图）
      ... 以及 JSON 格式的常量缓冲区值和管线状态
```

### 🎮 一键导出到 Unity
```
1. export_to_unity(73, "C:/output/eye_model", "eye_ball")
   → 自动创建文件夹，包含：
      eye_ball.obj                            （3D模型：OBJ格式）
      eye_ball.fbx                            （3D模型：FBX 7.4 ASCII，Unity/Unreal/Blender通用）
      eye_ball_material.json                  （Unity材质定义：属性映射Standard/URP/HDRP）
      eye_ball_UnityImport.cs                 （C#编辑器脚本：一键导入+自动赋材质）
      eye_ball_unity_export.json              （完整导出摘要）
   → 同时返回纹理绑定信息（含 resourceId），例如：
      slot0: albedo  → save_texture(resource_id=268775, output_path="eye_ball_albedo.png")
      slot1: normal  → save_texture(resource_id=270039, output_path="eye_ball_normal.png")

2. 导出纹理（按需）：
   - 对返回的每个纹理调用 save_texture(resource_id=XXX, output_path=...)
   - 或使用 export_drawcall 一次性导出全部纹理

3. 在Unity中使用：
   - 将导出文件夹复制到 Assets/ 目录
   - 将 eye_ball_UnityImport.cs 放入 Assets/Editor/
   - 菜单栏选择 RenderDoc > Import eye_ball
   - 自动导入模型+创建材质+赋贴图！
```

### 🎯 贴图角色自动识别

`get_bound_textures` 和 `reverse_shader` 会根据着色器变量名和纹理格式自动推断贴图用途：

| 识别关键词 | 推断角色 |
|-----------|---------|
| `_MainTex`、`diffuse`、`baseColor`、`albedo` | `albedo`（漫反射/基础色） |
| `_BumpMap`、`normalMap`、`_NRM` | `normal`（法线贴图） |
| `_MetallicMap`、`metalness` | `metallic`（金属度） |
| `_Roughness`、`smoothness` | `roughness`（粗糙度） |
| `_OcclusionMap`、`ao` | `ambient_occlusion`（环境光遮蔽） |
| `_EmissionMap`、`emissive`、`glow` | `emissive`（自发光） |
| `specular`、`spec` | `specular`（高光） |
| `height`、`displacement`、`parallax` | `height`（高度图） |
| `opacity`、`alpha`、`mask` | `opacity`（透明度） |
| `shadow`、`shadowmap` | `shadow_map`（阴影贴图） |
| `env`、`cubemap`、`skybox`、`reflection` | `environment`（环境贴图） |
| `lightmap`、`lm_` | `lightmap`（光照贴图） |
| BC5/RG 格式纹理 | `normal`（格式启发式推断） |
| BC6H/HDR 格式纹理 | `environment`（格式启发式推断） |

### 性能分析
```
1. enumerate_counters()                      → 查看可用的 GPU 计数器
2. fetch_counters("1,2,3")                   → 获取每个绘制调用的计数器值
```

---

## 项目结构

```
replaylab/
├── renderdoc_mcp_bridge/ # 第一方内置 Python Bridge 源码
│   ├── integration.json  # 内置集成元数据
│   └── __init__.py       # start/stop 与类型化原生意图 API
└── mcp_host/
    ├── pyproject.toml          # 项目配置 + 依赖声明
    ├── README.md               # 英文文档
    ├── README_CN.md            # 中文文档（本文件）
    └── src/
        ├── __init__.py
        ├── server.py           # MCP 工具定义（141 个工具，基于 FastMCP）
        ├── rd_wrapper.py       # RenderDoc API 封装层（本地直连备用方案）
        └── ipc_client.py       # 文件 IPC 客户端

构建输出：
  <qrenderdoc-app>/integrations/replaylab/python/renderdoc_mcp_bridge/
```

**架构**：
```
[AI/IDE] ←stdio/HTTP→ [ReplayLab 边车 (Python 3.10+)] ←文件IPC→ [qrenderdoc 内置集成]
```

- **`server.py`** — 注册 141 个 MCP 工具，通过 stdio 与 AI 客户端通信
- **`ipc_client.py`** — 文件 IPC 桥接 Python 3.10+ 与 RenderDoc 内置 Python 3.6
- **`rd_wrapper.py`** — RenderDoc API 直连封装（备用方案）
- **内置 Bridge** — 在 RenderDoc 内部采集 UI 上下文/按钮意图，并通过 IPC 执行有界 replay 调用

## 注意事项

- **多 API 读写**：现有分析路径已测试 D3D11 ✅ 与 Vulkan/ANGLE ✅；D3D12/OpenGL 读取 fallback 不是完整验证声明。默认 shared Shader replacement 支持本机 D3D11/DXBC 与 checked D3D12/DXBC|DXIL；event/family Shader override 仍仅支持 D3D11。CBuffer/material 修改支持本地 D3D11 与受限的本地 D3D12 CBV 路径，其他 API、remote replay、root constants/ray state object 与 `ExecuteIndirect` Root CBV 均失败关闭。
- **Tessellation 着色器**：完整支持 Hull/Domain 着色器阶段（D3D11/D3D12）
- 支持**同时加载多个截帧**——加载多个后可随时切换
- 所有资源 ID 都是数字整型，方便在不同工具间传递
- 纹理导出支持 PNG、JPG、BMP、TGA、HDR、EXR、DDS 格式
- 着色器调试返回逐步变量追踪（限制为 50 步以控制输出大小）
- 缓冲区数据同时以十六进制和 Base64 格式返回
- 服务器通过**文件 IPC** 与 RenderDoc 通信——无 Python 版本冲突
- Shader 检查只返回 RenderDoc 实际提供的表示；可编辑 HLSL 必须配置用户登记的外部反编译器，重建源码不等于原始源码。
- 贴图角色推断基于着色器变量命名约定和纹理格式分析，覆盖主流游戏引擎的命名习惯
- **模型导出质量提升**（v1.5.0）：DX11 左手坐标系 → 右手坐标系自动转换（X 轴翻转 + 缠绕方向反转）、`baseVertex` + `indexOffset` 正确偏移、索引重映射精确提取 DrawCall 顶点、压缩编码法线自动检测与丢弃（让导入工具重算）、UV 翻转可控

## 许可证

MIT
