# 来源与本地差异

- 上游：[Hengle/RenderDocMCP2](https://github.com/Hengle/RenderDocMCP2)
- 基准提交：`4e8581b3674723ce8306fbad52f2b140fdc7b7c9`
- 基准提交日期：2026-04-14；服务端 pyproject 版本：1.5.0。
- GUI extension.json 的版本仍为上游的 1.0.0，不能据此判断服务端版本。
- 许可证：MIT，保留本目录 LICENSE 中的原版权声明。
- 同步日期：2026-10-15。

本地变更：

1. `pyproject.toml`：依赖范围约束为 `mcp[cli]>=1.0.0,<2`。当前已验证环境为 Python 3.12 / mcp 1.30.0；并未声称整个版本范围全部测试通过。
2. `server.py`：支持 qTinecmaTool 名称；RENDERDOC_PATH 可为 EXE 文件或目录；先启动 GUI，等待扩展 bridge 就绪，再通过 IPC 打开 capture；启动 GUI 子进程时去掉启用全局 Vulkan 捕获的变量并设置当前品牌层禁用变量，避免捕获运行库误加载到回放 GUI。
3. `bridge/__init__.py`：优先使用 1.44 的 `LoadCapture(path, rd.ReplayOptions(), path, False, True)`，TypeError 时保留旧签名回退。

源文件均取自当前实际运行的本地安装，不包含虚拟环境、缓存、上游 .git 或机器个人配置。发布前核对过实际部署在 qTinecmaTool 用户扩展目录中的 __init__.py，与源快照内容一致。

为清晰区分组件，本仓库把上游 `renderdoc-mcp/` 放到 `server/`，把 `renderdoc_mcp_bridge/` 放到 `bridge/renderdoc_mcp/`。`patches/local-compatibility.patch` 仍使用上游文件路径，用于审阅差异；本目录已经包含补丁，安装时不要再次应用。
