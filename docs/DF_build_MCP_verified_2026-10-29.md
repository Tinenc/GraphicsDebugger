# DF研究 构建与 MCP 验收（2026-10-29）

源码基准：`1726b53b4cb1b20e3aaed4c36a31dc56adda3c76`，分支 `DF研究`。拉取完整 Git 历史，非浅克隆，无子模块和 LFS 文件。

该分支已包含 Hengle/RenderDocMCP2 的 `4e8581b3674723ce8306fbad52f2b140fdc7b7c9`（当前上游 HEAD），无需重复引入。保留现有兼容补丁和 MIT 许可证，详见 integrations/renderdoc-mcp/UPSTREAM.md。

## 构建

Windows / Visual Studio 2022 Professional / MSBuild v143，Development x64。在仓库根目录执行：

```powershell
.\build_windows.ps1
```

本次构建通过，0 错误，1 条 MSB8065 警告：SWIG 自定义步骤声明的 qrenderdoc.py 输出未生成。qrenderdoc.pyd 和 GUI 实际生成成功，并通过下述运行验证。

产物位于 `x64/Development/`：qTinecmaTool.exe、TinecmaTool.dll、TinecmaToolcmd.exe，以及 pymodules/renderdoc.pyd、pymodules/qrenderdoc.pyd。运行时需要同目录 Qt、Python、PySide2 等依赖，不能仅复制 EXE。编译时须关闭使用该目录 DLL 的 GUI。PySide2 构建依赖的安装方式见 qrenderdoc/README.md。

## 连通验证

新 GUI 的运行日志确认版本提交为上述 DF研究 基准，并自动加载 renderdoc_mcp 扩展、启动 IPC poller。已安装桥接 __init__.py 与仓库源码 SHA256 一致。

等待 GUI 完成初始化后执行：

```powershell
.\integrations\renderdoc-mcp\.venv\Scripts\python.exe .\integrations\renderdoc-mcp\verify.py --live --stdio --output .\integrations\renderdoc-mcp\verification-2026-10-29.json
```

结果：5 个 Python 文件语法检查通过；注册 37 个工具；直接 IPC ping 返回 status=ok；MCP SDK 客户端通过 stdio 完成 initialize、list_tools、call_tool(ping)，返回 status=ok。Python 3.14.2 / mcp 1.30.0。详见 JSON 验收记录。

本次未加载 RDC（loaded=false），验证范围为构建与 MCP 连通，不包含抓帧、回放和所有分析工具。GUI 首次启动会清理旧 IPC 请求，应在扩展初始化完成后执行验收。客户端配置示例已指向本次标准构建目录。

## 最新分支复验（2026-10-29 20:44，UTC+8）

针对分支提交 `18c3f7f70c8a60e0f7cfdc70d63a9c550dd7b3e9` 重新浅克隆并构建 Development x64。干净检出需按 `qrenderdoc/README.md` 安装官方 `qrenderdoc_3rdparty.zip`，再运行 `build_windows.ps1`，以启用 PySide2。构建成功；SWIG 仍报告 MSB8065（声明的 qrenderdoc.py 未生成），实际 qrenderdoc.pyd 和 GUI 已生成。

启动新构建的 GUI，并使用独立 TEMP 目录避免与其他正在运行的实例共用 IPC。Python 3.12.14、MCP SDK 1.30.0：语法检查通过，注册 37 个工具，直接 IPC ping 和 MCP stdio 客户端 ping 均返回 `status: ok`。本次没有加载 RDC，因此未验证回放和分析工具。结果见 `integrations/renderdoc-mcp/verification-recheck-2026-10-29.json`。
