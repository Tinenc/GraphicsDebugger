# 项目长期记忆 — GraphicsDebugger-Wuthering-Waves

> 详细过程见 `.workbuddy/memory/2026-10-2x.md`；方案见 `WUTHERING_WAVES_CAPTURE.md`

## 项目
- RenderDoc v1.44 fork，品牌 **TinecmaTool**；分支 `Wuthering-Waves`
- remote `https://github.com/Tinenc/GraphicsDebugger.git`
- 目标：PC 端给《鸣潮》抓帧（同套件也用于《三角洲行动》DF）

## ✅ 已跑通的最小组合（勿随意改动）
1. **注入**：Early Bird APC —— `QueueUserAPC(LoadLibraryW)` + ResumeThread，全程无远程线程。
   ⚠️ 不要用 `SetThreadContext` 直接改 RIP：CREATE_SUSPENDED 时初始线程还在
   `LdrInitializeThunk`，此时 LoadLibraryW 会让目标 `0xc0000005`（已实测踩过）
2. **子进程参数**：`TCM_CHILD_ARGS=-krqlv=hd`（hd/sd/uhd 必须与 `Client/Content/` 实际档位一致）
3. **子注入开关**：`TCM_FORCE_CHILD_INJECT=1`（APC 让目标先跑，捕获选项会晚到）
4. **黑名单**：ACE / CrashSight 进程不得注入，否则 ACE 重装驱动杀游戏
5. **一键**：`tools\launch_rdc.bat [hd|sd|uhd]`；GUI 里 Launch 选**壳** `Wuthering Waves.exe`，
   勾 Capture Child Processes，不勾 Global hook

## 事实速查
- 鸣潮启动链：`launcher.exe`（bootstrap，秒退）→ `3.0.1.0\launcher_main.exe`（UI 常驻）
  → `Wuthering Waves.exe`（484KB 转发壳）→ `Client-Win64-Shipping.exe`（渲染本体）
- `kuro: Use launcher to start game!` = **缺 `-krqlv`**，不是反作弊/父进程链；壳里无 `krqlv` 字样
- Client 直启必失败（保护层拒绝），只能 Launch 壳
- ACE 锁进程后 `OpenProcess` 返 5，运行中注入不可用
- 日志：`%TEMP%\TinecmaTool\RenderDoc_*.log`（UI 侧）、`RenderDoc_app_*.log`（目标侧）

## 环境坑（务必记住）
- **MSBuild 被安全策略拦**；语法自检用 `cl /Zs` 手工设 INCLUDE（不走 vcvars，它调 reg.exe 被黑名单）
- **我起的进程都会被注入 `tsbx.dll`** → 游戏/注入类实验必须用户自己跑，我的数据是污染数据
- 仓库 `remote.origin.fetch` 只有 main 一条 refspec → 新分支要 `git config --add` 补 refspec 才有 tracking ref
- 本仓库 git commit 正常（不像 `GraphicsDebuggerRdcTools` 那样写不进嵌套 ref）

## 代码约定
- 品牌：PascalCase `TinecmaTool` / ALLCAPS `TINECMATOOL_` / 注入黑名单用小写
- `rdcwstr` 私有继承 `rdcarray<wchar_t>`，**没有** `empty/push_back/+=` → 用 `std::wstring`
- `cmdline` 解析库无 `--` 结束符语义；`capture` 子命令成功时把 ident 当退出码返回
- `.bat` 必须 CRLF + ASCII；PS 脚本不要写中文注释
- 不要用 `tasklist | find`（git-bash 的 find 会抢先命中），改用 taskkill 的 errorlevel

## 待办 / 后续方向
- 仍有 4 个 `INTERNAL_*` 调用走 `CreateRemoteThread`（可改 APC 握手，但会让 frame0 抓帧更复杂）
- 不支持"启动即抓帧"（queue capture frame 0）
- `TinecmaTool.dll` 导出表 53 个 `RENDERDOC_*` + 7 个 `VK_LAYER_TINECMATOOL_*` 未清 —— 若再次被模块级扫描杀死，先动这里
