# D3D11/D3D12 事件级材质参数会话

该 MVP 把抓帧中的一个现有常量缓冲区绑定变成可审计、可回滚的参数会话。它不会全局
替换 Buffer，也不会修改捕获文件；shadow CBuffer 只在指定 `EventId + ShaderStage + b-slot`
的 Draw/Dispatch 执行期间绑定，随后恢复捕获状态。

## 最短流程

### qrenderdoc 原生材质面板

1. 完成项目的初次捕获分析，使当前帧取得稳定的 `captureId`；这一步也会载入已经验证的
   CBuffer/字段显示别名。
2. 在 Pipeline State 中打开目标 CBuffer，在变量树中右键一个值，选择
   `ReplayLab -> Expose as Material Parameter`。
3. `ReplayLab Material Parameters` 会停靠在 Pipeline State 旁。有效的标量/向量文本修改
   经过 180ms 合并后立即应用并重放精确 EID，也可按 `Apply` 明确提交。
4. 用每个 CBuffer 下的 `Rollback this CBuffer` 或底部
   `Rollback all material overrides` 恢复捕获值并释放 snapshot。手动关闭 Dock 不会在 Qt
   析构期间进入 replay；会话会保留，重开面板后仍可查看和回滚。扩展卸载前会先回滚。

右键 payload 由 qrenderdoc 的当前变量模型产生，包含真实 EID、stage、反射块索引、Shader/
Buffer ResourceId、绑定窗口和所选字段偏移。面板不直接信任这些值：它会重新建立 bridge
snapshot，并逐项核对后只保留 snapshot 签发的 `leafId`。用户、MCP 和 AI 都没有向这个
入口提交任意 offset/type/register 的接口。当前支持本地 D3D11，以及本地 D3D12 的
CBV-backed Root CBV/descriptor-table CBV；remote replay、D3D12 root constants、ray state
object、`ExecuteIndirect` 子事件中可能被 command signature 重写的 Root CBV、无运行时
backing memory 以及不支持的类型会在右键菜单或面板中明确拒绝。

面板与外部 MCP `prepare_material_session` 共用同一个 bridge snapshot 所有权表。同一
`EventId + stage + b-slot` 同时只能有一个所有者；若外部 Host 已建立会话，应先回滚该会话，
反之亦然，面板不会静默接管另一进程的 snapshot。

### 已有真实名称或只需要物理字段

1. 先用 `export_capture_symbol_catalog` 导出目标事件，取得 `captureId`。
2. 调用
   `prepare_material_session(event_id, constant_block_index, stage, expected_capture_id)`。
3. 从结果的 `parameters` 取得 `parameterId`、`displayName`、类型和当前值。
4. 把返回的 `parameterId` 作为 `parameter` 调用
   `update_material_parameter(session_id, parameter, value)`；每次调用都会重放精确事件。
5. 更新本身只修改工作区/回放状态，不会隐式写项目。只有任务明确授权项目持久化时，才用
   `register_active_project_material_session(session_id, parameter_ids)` 保存选中的、由 bridge
   签发的参数 ID；这是一条独立的 project-effect 工具调用。
6. 调试结束调用 `rollback_material_session(session_id)`。

`constant_block_index` 是当前阶段 `ShaderReflection.constantBlocks` 数组中的索引，**不是**
由用户猜测的 `b#`。结果会同时返回固定程序从反射得到的实际 `slot`。在同一阶段存在多个
常量块时，先让 Agent 读取当前 EID 的 pipeline/reflection 并列出候选，不要凭名称选择。

### 先从解包目录恢复名称

1. 打开捕获并选中目标 Draw/Dispatch。
2. 调用 `resolve_capture_symbols(asset_root, event_ids="...")`。它会依次执行：
   原生 Shader/布局导出、资产树扫描、Hash/接口/布局匹配、已验证别名表生成、当前捕获重载。
3. 只会自动载入 `ExactBlobUnique`、`ExecutableAndLayoutUnique` 或经过同等物理复验的规则。
   返回的 `NeedsAI`/歧义项仅供审阅，不会改名。
4. 再调用 `prepare_material_session`。返回的 `constantBlockDisplayName` 和参数
   `displayName` 会使用已验证别名；`path`、`parameterId`、offset、类型和布局 Hash 仍保留
   RenderDoc 的物理身份。

可直接对 Agent 说：

> 用 `D:\\UnpackedGame` 恢复当前捕获 Shader/CBuffer 名称，只应用程序验证匹配，列出其余候选；
> 然后为当前 EID 的像素阶段第 0 个反射常量块创建材质会话并列出可编辑参数。

更新时可说：

> 把 `Roughness — PS b2 space0 +0x30` 调到 `0.65`。

Agent 应使用返回的唯一 `parameterId` 提交，而不是从这句话推导 offset。需要恢复时说“回滚当前
材质会话”，或显式调用 `rollback_material_session`。

通用扫描器能识别独立/嵌入的 DXBC、DXIL 和仓库规定的 Symbol Manifest。Unity、Unreal 或
其他专有容器若没有直接暴露这些字节与名称关系，需要实现一个受限、可测试的 provider；AI
可以根据候选路径、材质依赖和反汇编提出对应关系，但 `AIProvisional` 不能自动应用。

若 MCP Host 异常退出而丢失了内存中的 `sessionId`，重新连接后显式调用
`clear_all_material_sessions(expected_capture_id)`；它会清除当前捕获生命周期的全部 event CBuffer
shadow 与 snapshot，再允许创建新会话。该恢复操作不会自动接管其他进程的会话。

`list_material_sessions` 与 `get_material_session_status` 只报告当前捕获生命周期仍有效的
会话。切换或关闭捕获后，旧会话 ID 立即失效。

## 固定程序安全边界

- `constant_block_index` 只用于 `ShaderReflection.constantBlocks[index]` 和
  `PipeState.GetConstantBlock(stage, index, 0)`；实际 D3D11/D3D12 slot/space 必须来自反射中的
  `ConstantBlock.fixedBindNumber`，用户不能提交 slot。
- snapshot 保存真实 RenderDoc `ResourceId`、Shader 字节 Hash、Buffer ResourceId、
  `byteOffset`、`byteSize`、完整有效绑定窗口和 layout Hash。D3D11.1 ring-buffer offset
  不会归零；D3D12 descriptor CBV 保留捕获的 SizeInBytes，Root CBV 则只复制反射可见窗口，
  不会把大型 upload ring 的剩余部分当成材质。相同 Buffer ResourceId 在不同事件/范围会
  产生不同 snapshot。
- MCP 更新参数不接受 offset、类型、register、packed bytes 或整块 raw bytes。Host 只按
  bridge 签发且自行复算的 `parameterId` 打包数值；bridge 再从不可变 baseline 与累计
  patch manifest 独立重建整块 shadow bytes，拒绝其他字节变化。
- 每次 apply 都重新定位 Event，验证 API、Shader、entry、CBuffer ResourceId/范围、
  layout、baseline 和捕获 session，并用严格递增 revision 防止旧请求覆盖新状态。
- D3D12 只在 `WithoutDraw` 后安装 replay-owned upload shadow，执行一次 `OnlyDraw` 后立即
  恢复原 root/heap state；descriptor-table 路径完整克隆 shader-visible heap，只改克隆中的
  目标 CBV。shadow/heap 被重写或释放前会等待 GPU idle，因此安全优先，但当前连续更新会有
  一次同步开销。
- 支持有限且明确：32-bit `Float`、`SInt`、`UInt` 的 scalar/vec2/vec3/vec4，以及反射
  提供明确数组 stride 的这些叶节点。矩阵、结构体本体、bitfield、bool、16/64-bit、
  指针和布局不明确的数组不会进入 `parameters`，因此不可编辑；当前 bridge 不逐项导出
  被过滤字段，`unsupportedFields` 预留给后续可解释诊断。
- float 拒绝 NaN、正负无穷和 float32 溢出；整数拒绝 bool、非整数和 32-bit 越界。
  名称或显示别名重复时，必须使用唯一 `parameterId`。

## 当前没有实现

- 根据语义范围自动生成的原生滑条、颜色选择器、VS/Rider 面板和跨进程持久会话。当前
  qrenderdoc 面板提供类型化 scalar/vec 文本编辑、180ms 合并、立即重放与 rollback；外部
  MCP 的 `update_material_parameter` 仍是一次离散更新与重放。Replay 工作串行执行，后续
  滑条仍必须合并鼠标事件，只提交最新稳定值。
- 自动判断“粗糙度/颜色/曝光”等材质语义；AI 可以基于只读字段和值提出标签，但不能
  改写物理布局或直接构造更新请求中的 offset/type/register。
- 新增自定义 CBuffer。D3D11 replay core 已能为明确的空 b-slot 创建 shadow binding，
  但当前严格 Shader ABI Gate 会拒绝新增 binding；在显式 ABI 增量 manifest、空槽复验
  和 replacement shader 联合事务完成前，不把它声明为可用功能。
- remote replay、Vulkan/OpenGL CBuffer mutation、D3D12 root constants/ray state object、
  `ExecuteIndirect` Root-CBV mutation 和全局 Buffer `ReplaceResource`。
