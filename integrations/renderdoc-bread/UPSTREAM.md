# 上游与移植边界

## 2026-11-24 更新

当前集成版本为 `1.46.142`，固定上游提交 `57a71d58e37fde5b915625a0dd760162c7256f25`。相对首次集成提交只有 10 个文件变化：版本元数据、Codex app-server 的 node_repl 隔离修复及对应测试，已按文件核对原始基线并应用。用户先前的 DF 修改保持原样，Git HEAD 未移动。

本次通过浏览器实际读取了[RenderDocBread AI新功能—哪里不会圈哪里](https://zhuanlan.zhihu.com/p/2102853489424713312)，作者稻草人，页面发布日期 2026-11-10。文章介绍 RT 圈选提问、可视化证据回复以及 Mesh/UV 查看器升级。这些实现已存在于首次移植的 `1.46.141`；本次没有重复实现或声称它们由 1.46.142 首次引入，而是升级对应运行程序并补充专项验收与使用说明。

源码来源仍为 [Straw1997/renderdocBread](https://github.com/Straw1997/renderdocBread)。本次验证及操作说明见[专项文档](../../docs/Bread_DF_圈选分析与网格查看器_2026-11-24.md)。以下保留首次移植的来源记录。

## 2026-11-16 首次集成

- 原作者：稻草人 / GitHub `Straw1997`。
- 源码：[Straw1997/renderdocBread](https://github.com/Straw1997/renderdocBread)。固定提交：`943d83c5ecdce036b788bf2ea1a338ea4e10740b`，`v1.x`，版本 `1.46.141`。
- 上游 README 直接引用用户提供的[第一篇文章](https://zhuanlan.zhihu.com/p/2098534987763131428)。对应标题为“RenderDocBread 史诗升级(内嵌AI) — 把它当作你的渲染器来调试游戏吧”。
- 用户的[第二篇文章](https://zhuanlan.zhihu.com/p/2011851609727059384)在本环境返回 403，未取得正文。本次功能范围以已确认的原作者仓库及其固定提交为依据；不声称逐段核对过两篇文章。
- DF 基线：本地 `DF研究` 分支 `32de33ad2`；共同祖先 `9b79944258e56946ccc141e023fa5887fdb8f192`。
- 本次保留未提交的 D3D11 相机诊断三文件修改和 `win32_hook.cpp` 修改。未创建提交、推送或发布。

采用完整源码移植，保留上游的 Qt 界面、ReplayLab、回放 API、Shader 编辑、中文翻译、单元测试和封存依赖。分支适配保留 DF 的抓帧/启动后端、`TinecmaTool.dll`、`qTinecmaTool.exe`、命令行与 shim 名称，增加 `qTinecmaTool` Python 模块别名并延续原用户配置目录。

本次额外修复：构建 Python 选择优先级；运行时部署器识别 qTinecmaTool；3DMigoto 非 main 入口恢复；固定 D3D 编译路径保留入口调试信息；Windows 任务日志锁初始化竞争；与上游新 API 不一致的测试夹具。详见验证报告。

上游根 `LICENSE.md`/许可证文件和第三方版权声明随源码保留。`upstream/` 下保存原作者的 AGENTS/CLAUDE 说明供追溯；它们不设置本分支的发布身份。上游发布 ZIP、自动升级 manifest 和 `system_load.dll` 抓帧后端不作为本地 DF Development 的部署入口；本地使用 `build_bread.ps1`。
