# ReplayLab MCP Host 配置指南

**适用环境**：Windows 10/11、系统 Python 3.10+
**RenderDoc 侧运行时**：qrenderdoc 内置 Python 3.6

---

## 一、环境信息

| 项目 | 路径 / 值 |
|------|-----------|
| RenderDoc 安装目录 | `C:\Program Files\RenderDoc` |
| RenderDoc 可执行文件 | `C:\Program Files\RenderDoc\qrenderdoc.exe` |
| RenderDoc Native 库 | `C:\Program Files\RenderDoc\renderdoc.dll` |
| RenderDoc 内置 Python | `python36.dll`（Python 3.6） |
| MCP Host 源码目录 | `replaylab/mcp_host`（相对于当前仓库根目录） |
| Bridge 规范源 | `replaylab/renderdoc_mcp_bridge` |
| Bridge 运行目录 | `<qrenderdoc 应用目录>/integrations/replaylab/python/renderdoc_mcp_bridge` |
| Bridge 包元数据 | `replaylab/renderdoc_mcp_bridge/integration.json`（仅描述首方包版本与入口，不参与运行时发现） |
| 系统 Python | 任意受支持的 Python 3.10+ 解释器 |
| MCP 客户端配置 | 由所用 MCP 客户端决定 |

---

## 二、项目结构

```text
renderdocUltra/
├── qrenderdoc/                         # 原生 C++ UI 与集成生命周期
└── replaylab/
    ├── mcp_host/                       # Python 3.10+ 边车 Host（本目录）
    │   ├── pyproject.toml              # 项目构建配置
    │   ├── README.md                   # 英文文档
    │   ├── README_CN.md                # 中文文档
    │   ├── SETUP_GUIDE.md              # 本文件
    │   └── src/
    │       ├── server.py               # MCP Server 入口
    │       ├── rd_wrapper.py           # RenderDoc API 封装层（备用直连方案）
    │       └── ipc_client.py            # 文件 IPC 客户端
    └── renderdoc_mcp_bridge/           # qrenderdoc 首方内置 Bridge 规范源
        ├── integration.json            # 版本/入口元数据，不参与发现或启停
        ├── __init__.py                 # IPC/replay 处理器
        └── ai_context.py               # 当前选择上下文与按钮 intent
```

构建系统将 Bridge 打包到固定应用路径
`integrations/replaylab/python/renderdoc_mcp_bridge`。qrenderdoc 直接装载这一首方包；
用户目录中的同名 Python 包不会覆盖它。

---

## 三、配置步骤

### 步骤 1：确认项目构建配置

`pyproject.toml` 已包含 hatchling 的包路径配置；无需在安装时临时修改。

**文件**：`replaylab/mcp_host/pyproject.toml`

```toml
[project]
name = "renderdoc-mcp"
version = "1.13.0"
description = "ReplayLab sidecar host for RenderDocUltra's built-in AI integration, capture analysis, and shader workflows"
requires-python = ">=3.10"
dependencies = [
    "mcp[cli]>=1.0.0,<2.0.0",
]

[project.scripts]
renderdoc-mcp = "src.server:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src"]
```

这里保留了历史故障的结论：`[tool.hatch.build.targets.wheel]` 与
`packages = ["src"]` 必须随源码提交，不能留作机器上的未跟踪修补。

### 步骤 2：确认 FastMCP 兼容性

当前源码使用 `instructions` 参数，并将 MCP 依赖约束在已支持的 1.x 范围。以下内容是历史兼容性说明；当前检出无需再次手改。

**文件**：`replaylab/mcp_host/src/server.py`

```python
# 修改前（报错）：
mcp = FastMCP(
    "renderdoc-mcp",
    description="Full-featured RenderDoc GPU capture analysis MCP server",
)

# 修改后（正确）：
mcp = FastMCP(
    "renderdoc-mcp",
    instructions="Full-featured RenderDoc GPU capture analysis MCP server with shader reverse-engineering",
)
```

### 步骤 3：安装项目

从当前仓库根目录进入 Host 目录，并在隔离环境中以开发模式安装：

```powershell
Set-Location .\replaylab\mcp_host
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

**输出确认**：
```
Successfully built renderdoc-mcp
Installing collected packages: renderdoc-mcp
Successfully installed renderdoc-mcp-1.13.0
```

依赖 `mcp[cli]>=1.0.0` 及其子依赖（anyio, httpx, pydantic, starlette 等）均已满足。

### 步骤 4：验证工具注册

```powershell
.\.venv\Scripts\python.exe -c "from src.server import mcp; tools = mcp._tool_manager._tools; print('OK: %d tools' % len(tools)); print('\n'.join(sorted(tools.keys())))"
```

**输出确认**：
```
OK: 141 tools
close_capture
debug_pixel
debug_vertex
debug_vulkan_bindings
disassemble_shader
enumerate_counters
export_drawcall
export_to_unity
fetch_counters
find_draws_by_resource
find_draws_by_shader
find_draws_by_texture
get_action_timings
get_actions
get_all_disassembly_targets
get_api_properties
get_bound_samplers
get_bound_textures
get_buffer_data
get_buffers
get_cbuffer_contents
get_debug_messages
get_frame_info
get_pipeline_state
get_post_vs_data
get_resource_usage
get_resources
get_shader_reflection
get_shader_source
get_texture_minmax
get_textures
identify_drawcalls
launch_renderdoc
list_captures
load_capture
pick_pixel
pixel_history
reverse_shader
save_texture
set_event
switch_capture
```

### 步骤 5：配置 MCP 客户端

在所用 MCP 客户端的配置中添加 `replaylab` 条目。下面是字段结构示例；
将 `<REPOSITORY>` 替换为本机当前仓库的绝对路径：

```json
{
  "mcpServers": {
    "replaylab": {
      "command": "<REPOSITORY>\\replaylab\\mcp_host\\.venv\\Scripts\\python.exe",
      "args": ["-m", "src.server"],
      "cwd": "<REPOSITORY>\\replaylab\\mcp_host",
      "env": {
        "RENDERDOC_MODULE_PATH": "C:\\Program Files\\RenderDoc"
      }
    }
  }
}
```

**配置说明**：

| 字段 | 值 | 说明 |
|------|-----|------|
| `command` | `<REPOSITORY>\replaylab\mcp_host\.venv\Scripts\python.exe` | 使用本项目隔离环境启动 |
| `args` | `["-m", "src.server"]` | 以模块方式运行 MCP Server |
| `cwd` | `<REPOSITORY>\replaylab\mcp_host` | 工作目录指向本仓库内的 Host 根目录 |
| `env.RENDERDOC_MODULE_PATH` | `C:\Program Files\RenderDoc` | RenderDoc 模块搜索路径 |

**传输方式**：stdio（由 MCP 客户端管理进程生命周期）

### 步骤 6：重新加载 MCP 客户端配置

配置完成后重启客户端或重新加载 MCP Server 配置，使 `replaylab` 服务生效。

---

## 四、已注册工具一览（当前源码 141 个）

### 截帧管理（6 个）
| 工具 | 功能 |
|------|------|
| `load_capture` | 加载 .rdc 截帧文件 |
| `close_capture` | 关闭截帧 |
| `list_captures` | 列出所有已加载截帧 |
| `switch_capture` | 切换活动截帧 |
| `launch_renderdoc` | 🆕 启动 RenderDoc 并打开 .rdc 文件（自动查找 qrenderdoc，等待桥接就绪） |
| `debug_vulkan_bindings` | 🆕 诊断 Vulkan descriptor set 绑定数据（排查纹理导出问题） |

### 帧信息（2 个）
| 工具 | 功能 |
|------|------|
| `get_api_properties` | 图形 API 类型、渲染器 |
| `get_frame_info` | 帧号、时间、绘制统计 |

### 绘制调用（2 个）
| 工具 | 功能 |
|------|------|
| `get_actions` | 获取所有 DrawCall（层级结构） |
| `set_event` | 定位到指定事件 |

### 资源检查（4 个）
| 工具 | 功能 |
|------|------|
| `get_textures` | 列出所有纹理 |
| `get_buffers` | 列出所有缓冲区 |
| `get_resources` | 列出全部资源 |
| `get_resource_usage` | 追踪资源使用 |

### 管线状态（1 个）
| 工具 | 功能 |
|------|------|
| `get_pipeline_state` | 绑定的着色器、渲染目标、视口 |

### 着色器检查（3 个）
| 工具 | 功能 |
|------|------|
| `get_shader_reflection` | 着色器反射信息 |
| `disassemble_shader` | 着色器反汇编 |
| `get_all_disassembly_targets` | 可用反汇编目标列表 |

### 着色器逆向还原（2 个）
| 工具 | 功能 |
|------|------|
| `reverse_shader` | 一站式着色器逆向分析 |
| `get_shader_source` | 提取着色器源码 |

### 贴图自动对应（2 个）
| 工具 | 功能 |
|------|------|
| `get_bound_textures` | 自动映射着色器槽位→贴图+角色推断 |
| `get_bound_samplers` | 采样器状态 |

### 常量缓冲区（1 个）
| 工具 | 功能 |
|------|------|
| `get_cbuffer_contents` | 读取 CBuffer 实时运行值 |

### 受管资源导出（6 个）
| 工具 | 功能 |
|------|------|
| `get_resource_export_capabilities` | 查询 glTF/Unity URP/Blender/FBX 与校验器能力 |
| `inspect_resource_export` | 只读检查精确 EID/instance/view |
| `prepare_resource_export` | 写入 Host 受管、哈希绑定的 capture bundle |
| `finalize_resource_export` | 按类型化 transfer spec 原子发布目标包 |
| `get_resource_export_status` | 查看同一 operation 的阶段和收据 |
| `validate_resource_export` | 验证 containment、哈希、网格、目标导入/编译状态 |

旧 `export_drawcall` / `export_to_unity` 仅保留兼容性，不能作为资源导出专员的
完成路径；它们没有完整顶点流、texture view/sampler、显式坐标和目标校验合同。

### 智能识别（1 个）
| 工具 | 功能 |
|------|------|
| `identify_drawcalls` | 🆕 识别每个 DrawCall 的渲染内容：RT 累积截图 + Shader 分组 + 屏幕 AABB |
| `analyze_lighting` | 🆕 灯光分析：光照模型检测 + cbuffer 参数提取 + 阴影贴图/环境贴图识别 |

### 纹理操作（3 个）
| 工具 | 功能 |
|------|------|
| `pick_pixel` | 读取像素 RGBA 值 |
| `get_texture_minmax` | 纹理最小/最大值 |
| `save_texture` | 导出纹理为图片 |

### 缓冲区数据（1 个）
| 工具 | 功能 |
|------|------|
| `get_buffer_data` | 读取缓冲区原始数据 |

### 像素历史（1 个）
| 工具 | 功能 |
|------|------|
| `pixel_history` | 像素修改全历史 |

### 着色器调试（2 个）
| 工具 | 功能 |
|------|------|
| `debug_pixel` | 像素着色器逐步调试 |
| `debug_vertex` | 顶点着色器逐步调试 |

### 性能计数器（2 个）
| 工具 | 功能 |
|------|------|
| `enumerate_counters` | 列出 GPU 计数器 |
| `fetch_counters` | 获取计数器值 |

### 网格数据（1 个）
| 工具 | 功能 |
|------|------|
| `get_post_vs_data` | 顶点着色器后网格输出 |

### 调试消息（1 个）
| 工具 | 功能 |
|------|------|
| `get_debug_messages` | 图形驱动验证错误/警告 |

---

## 五、使用方式

配置完成后，可以在 Codex 或其他本地 stdio MCP 客户端中直接通过对话使用 ReplayLab：

```
用户：帮我加载这个截帧文件 D:\captures\game_frame.rdc
AI  ：→ 调用 load_capture("D:\\captures\\game_frame.rdc")

用户：有哪些 DrawCall？
AI  ：→ 调用 get_actions()

用户：看看第 142 号事件的像素着色器
AI  ：→ 调用 set_event(142)
    → 调用 reverse_shader("fragment")
    （返回实际可用的源码/反汇编证据 + 绑定贴图 + 常量缓冲区值；不保证恢复原始 HLSL）

用户：把这个 DrawCall 的所有贴图导出来
AI  ：→ 调用 export_drawcall(142, "D:\\output\\dc142")
    （自动保存着色器代码 + 所有贴图 PNG + 参数值）
```

---

## 六、注意事项

1. **三层架构**：MCP Server 运行在系统 Python（3.10+），通过文件 IPC 与 RenderDoc 内置 Python 3.6 桥接通信
2. **IPC 目录**：`%LOCALAPPDATA%\ReplayLab\ipc-v2\instances\`（按 qrenderdoc 实例与 request-id 隔离）
3. **多 API 支持**：读取路径已实际测试 D3D11 与 Vulkan/ANGLE；D3D12/OpenGL 读取 fallback 不是完整验证声明。默认 shared Shader replacement 支持本机 D3D11/DXBC 与 checked D3D12/DXBC|DXIL；event/family/CBuffer/material 仍仅 D3D11
4. **Tessellation 支持**：Hull/Domain 着色器阶段完整支持（D3D11/D3D12）
5. **Bridge 位置**：规范源在 `replaylab/renderdoc_mcp_bridge`；运行副本固定为 `<appdir>/integrations/replaylab/python/renderdoc_mcp_bridge`
6. **启动顺序**：手动方式需先启动本仓库构建的 RenderDocUltra → 加载截帧 → 确认内置 Bridge 运行 → 再使用 MCP 工具。也可使用 `launch_renderdoc` 工具自动完成启动与加载

---

## 七、故障排查

| 问题 | 解决方法 |
|------|---------|
| MCP 客户端中看不到 ReplayLab 工具 | 重新加载客户端配置，并检查命令、`cwd` 与配置文件语法 |
| `ImportError: No module named 'renderdoc'` | 确认 `RENDERDOC_MODULE_PATH` 指向包含 `renderdoc.pyd` 或 `renderdoc.dll` 的目录 |
| `renderdoc` 模块版本不匹配 | 需要编译与系统 Python 版本匹配的 `renderdoc.pyd`，或改用 IPC 桥接方式 |
| `FastMCP` 参数报错 | 确认 `mcp` 库版本 ≥ 1.26，使用 `instructions` 参数而非 `description` |
| `pip install -e .` 报 hatch 错误 | 确认 `pyproject.toml` 中有 `[tool.hatch.build.targets.wheel]` 段 |
| `LoadCapture expected 5 arguments` | 已自动兼容：内置 Bridge 的 `_open_capture` 会 try/except 适配 4/5 参数签名 |
| `launch_renderdoc` 超时 | ✅ 已优化：根据文件大小动态等待（<50MB→40s, 50-200MB→60s, >200MB→90s），超时后用 `ping` 检查 |
| Vulkan 纹理 resourceId 返回 0 | ✅ 已修复（v1.2.0）：改用 `UsedDescriptor.descriptor.resource` 路径解析 |
| 模型导出镜像/UV 错乱/法线异常 | ✅ 已修复（v1.5.0）：DX→右手坐标系转换 + 索引重映射 + 压缩法线检测 + UV flip 默认关闭 |
