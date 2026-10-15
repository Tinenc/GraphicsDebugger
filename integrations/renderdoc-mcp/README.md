# RenderDoc MCP：本仓库可复用快照

## 摘要

这里保存本次 DF 正式客户端 RDC 验收实际使用的 **Python MCP 服务端 + RenderDoc GUI 扩展**。服务端来自 Hengle/RenderDocMCP2 1.5.0，保留本地的 TinecmaTool 命名、GUI 启动顺序及 RenderDoc 1.44 LoadCapture 签名适配。源码可独立安装，不依赖原开发机的 vendor_imports 目录。

这套 MCP 用于连接 GUI 并分析已经取得的 RDC。DF 设备延迟导入与 DXGI factory 修复位于本仓库的 C++ 捕获实现，不能用 MCP 安装代替。根目录的旧 `renderdoc-mcp-0.3.1.zip` 及旧 ABI 适配报告属于另一套实现，不是本目录服务端。

上游来源、提交及本地补丁见 [UPSTREAM.md](UPSTREAM.md)，完整安装、配置和排查见 [使用文档](../../docs/DF_RenderDoc_MCP_部署与使用.md)。

## 目录

- `server/`：Python 3.10+ 的 stdio MCP 服务端，入口 `src.server:main`。
- `bridge/renderdoc_mcp/`：安装到 GUI 扩展目录的桥接扩展，包含 extension.json 和 __init__.py。
- `examples/mcp.json`：可替换路径的客户端配置示例。
- `patches/local-compatibility.patch`：相对上游提交的三个文件补丁，补丁路径使用上游原始布局。
- `verification.json`：从本目录源码执行的语法、工具注册、IPC 与已加载抓帧核验结果。
- `verify.py`：可复用核验脚本；默认只检查源码与工具注册，`--live` 增加只读 IPC 查询。
- `LICENSE`：上游 MIT 许可证及版权声明。

## 最短安装路径

在仓库根目录的 PowerShell 中执行（外部 Python 需为 3.10+）：

```powershell
py -3 -m venv .\integrations\renderdoc-mcp\.venv
.\integrations\renderdoc-mcp\.venv\Scripts\python.exe -m pip install -e .\integrations\renderdoc-mcp\server
```

随后将 `bridge/renderdoc_mcp` 目录安装到当前 GUI 的用户扩展目录，并在 GUI 扩展管理中启用 RenderDoc MCP。自定义 qTinecmaTool 的本机配置目录为 `%APPDATA%\qTinecmaTool\extensions`；官方 qrenderdoc 通常使用 `%APPDATA%\qrenderdoc\extensions`。请以所运行 GUI 的实际配置目录为准。

客户端 command 指向新环境中的 `renderdoc-mcp.exe`，RENDERDOC_PATH 指向当前可运行的 GUI EXE。无需上传或复用本机 `.venv`，也不要把整个个人 MCP 配置复制进仓库。
