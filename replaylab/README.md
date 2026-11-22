# Bread AgentHost（RenderDocUltra AI 接入层）

界面与产品品牌统一为 **Bread / Bread AI**。下文命令、协议、程序文件名及已有配置路径中的 `ReplayLab` / `replaylab` 保留兼容，不会重命名用户已有项目或数据目录。

Bread 把 qrenderdoc 的当前捕获上下文、首方 MCP 工具和文件系统 Agent Skills 交给 AI，同时把模型/厂商运行时留在独立进程中。当前架构只有两条路径：

| 连接类型 | 引擎 | 行为 |
|---|---|---|
| Model API | LangChain DeepAgents (`deepagents==0.7.22`) | 主 Agent 直接调用 Bread 工具，可按需委派通用子 Agent |
| ACP / Codex 订阅 | 厂商原生 Agent | 厂商 CLI 自己完成单 Agent 循环，直连任务级 Bread MCP |

不存在 legacy/lean 引擎选择器、Bread 自研 planner/reviewer、WorkflowGuard、强制报告或验收续跑、Custom Agents。AI 自己决定是否加载 Skill、使用工具和安排处理步骤。

API 模式使用 [langchain-ai/deepagents](https://github.com/langchain-ai/deepagents) 的固定版本发行包，由 AgentHost 直接调用 `create_deep_agent`；当前仓库没有维护另一个 DeepAgents 源码克隆或自研替代循环。

用户选择的连接 Profile 决定本轮走 API/DeepAgents 还是 ACP/Codex 厂商 Agent；提问内容本身不会自动切换供应商。API 的通用子 Agent 由主 Agent 按需委派，Skill 是可加载的指导资料，不会自动变成一个专用 Agent。DeepAgents 可在**单次长任务内部**压缩上下文；Bread 的跨轮对话目前由 UI 传递有界历史，每轮新建 Agent 图，较早消息超出边界时标记缺口，尚无持久的语义摘要。ACP/Codex 的任务内上下文由厂商运行时处理，同样不能把一次任务的压缩当成 Bread 的跨轮记忆。

## 模块边界

- `src/replaylab_agent/`：AgentHost、profile/凭据、deepagents 与原生厂商连接、任务事件。
- `mcp_host/src/`：RenderDoc 工具、效果控制、物理写入校验和工具收据。
- `renderdoc_mcp_bridge/`：qrenderdoc 固定加载的首方 Python 桥；不是可扫描插件。
- `../.agents/skills/`：可选的文件系统 Skills；包括 `replaylab-core`，由 AI 按需加载，缺失或禁用不会阻断无关任务。
- `packaging/`：冻结 AgentHost、MCP 和内部服务并安全部署到 qrenderdoc 应用目录。

详细边界见 [ARCHITECTURE.md](ARCHITECTURE.md)，当前进度与外部验收项见 [docs/HANDOFF_2026-09-04.md](docs/HANDOFF_2026-09-04.md)。

## Agent 与 Host 的责任

AI 从当前对话选择目标和方法，可读取 EID、绑定资源与旧源码，按需加载 Skill，
并通过直接工具或能力目录执行操作。当前 UI 选择只是没有更明确目标时的回退信息。
API 与 ACP/Codex 都能读取同一套可选 Skill，并能调用 Bread MCP 工具；API 仅提供
通用子 Agent，不会为每个 Skill 自动创建子 Agent。

Host 负责当前捕获、绑定资源、源码版本/CAS、编译/ABI 和真实回放效果等物理校验，
返回事实、诊断和工具收据。Shader 草稿局部编辑不要求每段都编译或替换；最终发布
或回放替换仍须通过对应校验。Host 不再用报告表单、固定整理阶段、审核轮数或进度
标记决定 AI 的下一步。AI 的最终回答要准确区分草稿、保存源码、编译、项目注册与
回放替换，并说明未完成之处。

## 安装开发环境

AgentHost 要求 Python 3.12。以下命令在 `replaylab` 目录执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[test]" -e ".\mcp_host[bundle]"
```

deepagents、LangGraph、ACP/MCP 和支持的 LangChain provider 是 AgentHost 的正式依赖，不需要再选择 legacy/deep extra。

## Profile 与凭据

查看内置 preset 和当前 profile：

```powershell
.\.venv\Scripts\replaylab-ai.exe profile presets
.\.venv\Scripts\replaylab-ai.exe profile list
```

添加一个 API profile；模型 ID 应替换为供应商实际支持的值：

```powershell
.\.venv\Scripts\replaylab-ai.exe profile add api-main `
  --preset openai --model MODEL_ID --select
```

API 密钥优先写入 Windows Credential Manager，而不是 `profiles.json`：

```powershell
$env:OPENAI_API_KEY | .\.venv\Scripts\replaylab-ai.exe profile credential store `
  --profile api-main --api-key-stdin
```

订阅 profile 使用对应厂商 preset，例如 `codex`、`claude-code-acp`、`copilot-acp` 或 `cursor-acp`。登录、连接测试和进程 containment 由 qrenderdoc 设置 UI 驱动；CLI 的 `--start-gated` 接口是该 UI 的受控内部通道，不应手工绕过。

配置位置：

- profile：`%APPDATA%\ReplayLab\ai\profiles.json`；
- API secret：Windows Credential Manager；
- 引擎运行设置：`%LOCALAPPDATA%\ReplayLab\config\agent-engine.json`；整体超时默认 24 小时，MCP 工具调用次数默认不限制；
- 任务事件/工具收据 sidecar：`%LOCALAPPDATA%\ReplayLab\ai-tasks\`；
- cbuffer 别名：`%LOCALAPPDATA%\ReplayLab\aliases\cbuffer\`。

## 运行与诊断

普通无捕获对话：

```powershell
.\.venv\Scripts\replaylab-ai.exe run --profile api-main --prompt "解释 GPU overdraw"
```

连接当前唯一已加载捕获的 qrenderdoc 实例：

```powershell
.\.venv\Scripts\replaylab-ai.exe run --profile api-main --live-context `
  --prompt "解释当前事件" --json
```

离线诊断不会发起模型生成：

```powershell
.\.venv\Scripts\replaylab-ai.exe doctor --profile api-main
```

CLI 的主要用户命令是 `run`、`profile`、`tools`、`doctor`；`unpacker` 是打包运行时的内部入口。qrenderdoc 正常使用会传入冻结 context file、instance id、task id、取消文件和 MCP 路径；这些参数不建议用户自行拼装。

## 安全边界

- profile 中禁止原始 secret；自定义 Agent 命令是 argv 数组，不经过 shell，并拒绝常见 secret 参数。
- 捕获任务的 MCP 路由绑定 task id、事件根、qrenderdoc instance、目标 scope、工具 allowlist 与效果上限；工具调用次数由 AI 自行决定，不以旧的审核预算打回任务。
- `policy.py` 只定义 read-only/workspace/project/ui/replay 效果；实际调用由 Host 再校验。
- Codex 原生模式可在已验证的只读隔离工作区使用命令、文件查看和图像查看；ACP 厂商因缺少统一的可验证沙箱，通用 shell/文件操作仍不开放。Shader、项目和回放的实际写入都必须走 Bread MCP 的效果检查；Apps、computer-use 和额外 MCP 不会自动获得捕获权限。
- UI 只展示经过脱敏和大小限制的进度；原始 provider 诊断进入本地日志。
- 工具失败以有界 `ERROR: <Host diagnostic>` 返回 Agent，使其可以修正参数；不会把捕获数据当成指令执行。

## 测试

测试默认关闭 pytest 的仓库缓存，并由 `tests/conftest.py` 为每次运行在系统临时目录中创建唯一工作目录；不要把 `--basetemp` 指向仓库：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

如果确实需要指定目录，只能使用仓库外的绝对系统临时路径，例如
`--basetemp="$env:TEMP\renderdoc-ultra-manual-test"`，并在运行后清理。

其中包括真实已安装 deepagents graph 的离线集成测试，以及 ACP/Codex、可选 Skill 加载、工具收据、权限、事件与 qrenderdoc 契约测试。需要真实账号或会产生费用的供应商调用不属于默认测试。

## 打包与 Development 部署

```powershell
.\packaging\tests\Test-ReplayLabPackaging.ps1
.\packaging\tests\Test-ReplayLabRuntimeDeployment.ps1
.\packaging\Build-ReplayLabRuntime.ps1
.\packaging\Deploy-ReplayLabRuntime.ps1 `
  -ApplicationDirectory ..\x64\Development
```

冻结输出在 `output/replaylab/`。部署脚本校验 manifest、大小和 SHA-256，只替换 marker 所有的运行时文件，不接管应用目录中的无关内容。完整说明见 [packaging/README.md](packaging/README.md)。

qrenderdoc 在内存受限 Windows 机器上必须使用 `MSBuild ... /m:2`。发布前还需对 x64 Release 重复冻结运行时部署、翻译完整性和完整应用构建。

## cbuffer 与 Shader Workspace 别名

Buffer Viewer 支持常量块及成员显示别名，按布局哈希跨 shader/捕获共享；用户别名优先于 AI 别名。别名不改变反射、材质字段或 shader 字节。

Shader Workspace 的主编辑缓冲是后续编译/替换的规范源码。`finish_shader_workspace_organization` 默认将当前提案保存为可编辑源码，无须先编译或通过 ABI 校验；保存草稿不等于回放替换。AI 可另行请求验证或替换，替换时会校验当前源码、候选 ABI 与回放目标。
