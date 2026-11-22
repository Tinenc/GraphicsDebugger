# EID 性能开发独立工作区

- 分支：`codex/eid-interactive-replay`
- 本工作区：`D:\Study\GitHub\renderdocUltra-eid-perf`
- 原工作区：`D:\Study\GitHub\renderdocUltra`，仍为 `v1.x`
- 源码快照提交：`26ddeea4bbcad098a42afcc0d660321d7706ac46`
- 起始已提交版本：`9d93e827dea5122984c6f064a12a994109631156`

本工作区建立于 2026-09-08。以原工作区当时的源码状态为共同基础，复制了 310 个已有文件变更及 263 个新增源码/文档文件，包含已有 EID、Shader 与共同基础设施修改。这是隔离今后开发，不是把既有混合改动从原工程剥离。原工程的文件、分支和暂存内容已经核对保持不变。

未复制输出目录、虚拟环境、已构建程序、临时抓帧和临时对象文件。本次未构建、部署、启动服务或执行性能测试；不要把源码隔离完成理解为运行环境已验收。

## 并行开发约定

1. 本分支的一切源码修改、生成文件、翻译、编译与部署都使用本工作区的绝对路径。原工作区只可作为历史证据的只读来源，不向其 `x64`、`output` 或集成目录写入。
2. 构建时显式将 `SolutionDir` 指向本工作区。依赖可以使用已安装工具链，但不要共享可写的对象目录、构建目录或运行时部署目录。先审查已有脚本中的绝对路径；不要原样执行原工程 `output/` 内的旧脚本。
3. 两边可以同时编辑源码和执行不争用外部状态的轻量检查。按照项目规则不同时启动两个全量原生构建，单次使用 `/m:2`。性能测量时避免另一边 GPU 回放或重负载编译。
4. 同时运行两套 GUI 前，先验证 UI 配置、日志、端口、服务发现、会话与可写项目的隔离。源码支持 `REPLAYLAB_INTERNAL_RUNTIME_DIR`、`REPLAYLAB_INTERNAL_PORT`、`REPLAYLAB_RUNTIME_DIR`、`REPLAYLAB_MCP_PORT`，但仅设置这些变量不等于所有全局状态都已隔离。此项尚未在本次设置中实施或验收。
5. 原始 RDC 可以按只读输入共享；保存 Shader、项目索引及编辑状态应使用独立测试项目，避免两边共同写用户的 ReplayLab Project。
6. 进程按本工作区的绝对程序路径、PID 和启动时间归属；不能按程序名终止另一边的同名进程。不要改全机捕获层注册来替代独立运行设置。
7. 新的性能工作从源码快照之后单独提交。今后整合时审阅并选择需要的后续提交；不要把初始混合快照无差别覆盖到仍在开发的主工作区。Git worktree 分开文件，分支历史和对象库仍共享。

## 继续前的参考

已有设计在 `docs/CONTRIBUTING/Interactive-Replay.md`。历史性能实测与复盘仍位于原工程的 `output/interactive-replay-20260906/`，可只读查阅，其中暂停记录反映当时状态。新工作区是否启动功能开发，以用户在对应任务中的最新明确指令为准；本次设置没有自动启动旧测试计划。

工作区快照收据保存在本 worktree 的 Git 管理目录中：`eid-source-snapshot.json`，可用 `git rev-parse --absolute-git-dir` 定位。

## 2026-09-08 运行隔离进展

上述“尚未实施”是建立 worktree 时的记录。性能分支现有自己的 core、Python binding、CLI 和
`x64/Development/q_render_soco_doc.exe`，通过 `util/perf/build_eid_isolated.ps1 -IncludeGui`
构建；全部对象、输出和第一方 bridge 均留在本工作区。

GUI 测试使用 `util/perf/run_eid_isolated_gui.py` 派生全新配置、临时目录、ReplayLab IPC 与项目根，
并核验实际运行库。`RENDERDOC_CONFIG_DIR` 同时控制原生配置与 GUI 配置/对话/扩展扫描；只双击
exe 不会自动设置这个隔离环境。原分支 GUI 可以继续运行，测试只关闭自己启动的实例。

`output/gui-isolation/probe-v12b` 的无 capture 探针和 `presentation-v12b`、`presentation-v13`
的真实 Qt 纹理控件验收已通过，记录的原用户配置哈希未变。此次只验 GUI/bridge 隔离；
未打包的 Shader/Agent sidecar 服务不在此验收范围。两实例仍共享物理 GPU，不能把并行测试
的耗时当作独占 GPU 性能验收。完整命令及限制见 `util/perf/ISOLATED_REPLAY.md`。
