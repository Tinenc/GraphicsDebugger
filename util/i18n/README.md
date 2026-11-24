# QRenderDoc 简体中文维护流程

此目录把“英语源码 + Qt TS/QM 语言包”作为唯一翻译来源。不要直接改写程序内部枚举、API 标识符或 `ToStr()` 的序列化结果。

## 日常命令

需要 Qt 5 Linguist 工具和 Python 3。本项目验证过的 Windows 工具路径为：

```powershell
$qtBin = "C:\Qt\5.15.2\msvc2019_64\bin"
./util/i18n/update_translations.ps1 -QtBin $qtBin
./util/i18n/build_translations.ps1 -QtBin $qtBin
```

脚本也会依次尝试 `QTDIR`、`QT_ROOT_DIR`、`PATH`、`qmake -query QT_INSTALL_BINS` 和本机 `C:\Qt`。`update_translations.ps1 -Check` 不改正式 TS，只验证源码与语言包是否同步。`build_translations.ps1` 先执行质量检查，然后生成 `qrenderdoc_zh_CN.qm`，并在可用时复制 Qt 自带的 `qt_zh_CN.qm`，从而覆盖标准按钮和对话框。

`qrenderdoc/Translations/renderdoc_zh_CN.qph` 可在 Qt Linguist 中作为 Phrase Book 加载；详细术语和不可翻译 token 以 `glossary.zh_CN.yaml` 为准。该 YAML 使用 JSON 子集，因此质量脚本不依赖第三方 Python 包。

`source_manifest.json` 是 `lupdate` 与源码裸文本门禁共用的首方源码清单。它会自动纳入 `qrenderdoc` 下新建的首方目录和受支持的 C++/Designer 文件，同时排除一般第三方代码，仅重新包含明确维护的 `toolwindowmanager` 与 `flowlayout`。不要在更新脚本和 linter 中另建不同的目录列表。

## 内存占用约束

运行时只嵌入并加载编译后的 `.qm`：简体中文加载应用目录与 Qt 标准控件目录，英语不安装任何翻译器；体积更大的 `.ts`、术语库和检查脚本只在开发/CI 中使用。语言变更在重启后应用，因此不会为了即时重建全部窗口而同时保留两套界面对象和目录。

更新、检查和生成目录的脚本均为串行流程。内存受限的 Windows 开发机在最终验证工程时建议限制 MSBuild 并行度，例如：

```powershell
$solutionDir = (Resolve-Path .).Path.TrimEnd('\') + '\'
MSBuild qrenderdoc/qrenderdoc_local.vcxproj /m:2 /t:Build /p:Configuration=Development /p:Platform=x64 "/p:SolutionDir=$solutionDir"
```

## 新 UI 自动进入语言包

- Qt Designer 的 `.ui` 属性保持默认的可翻译 `<string>`；保存后运行更新脚本即可自动进入 TS。不要随意设置 `notr="true"`。
- `QObject` 派生类中的动态 UI 文本使用 `tr("Source text")`。
- 全局函数或无稳定翻译上下文的位置使用 `QCoreApplication::translate("StableContext", "Source text")`。
- 不要把英文常量藏在 `uiText("...")` 这类自定义包装函数里；`lupdate` 无法识别这类调用。应直接使用受支持的翻译调用，或先用 `QT_TRANSLATE_NOOP("StableContext", "Source text")` 显式登记，再以完全相同的上下文翻译。
- 后端或运行时目录提供的可见文本必须同时提供稳定消息/preset ID。qrenderdoc 根据该 ID 和已登记原文进行本地化；未知值可作为诊断信息原样保留，但不能把后端英文直接作为常规按钮、标签或帮助文字。
- 同一个英文词有多种图形学含义时，使用消歧参数和译者注释。例如：

  ```cpp
  //: A vector perpendicular to a mesh surface.
  label->setText(tr("Normal", "mesh.attribute.normal"));
  ```

- 静态表中的延迟翻译字符串用 `QT_TRANSLATE_NOOP("StableContext", "Source text")` 保存，显示时再调用同一上下文的 `QCoreApplication::translate()`。
- `VS`、`PS`、资源格式、shader semantic、API 函数、路径、用户资源名等标识符保持原样。确实不面向用户的 `lit("...")` 可在本行或上一行注明 `i18n-ignore: 具体理由`。
- 管线流程图中的紧凑阶段节点始终显示 API 缩写，不随界面语言展开或翻译。D3D 使用 `IA`、`VS`、`HS`、`DS`、`GS`、`RS`、`PS`、`OM`、`CS`；OpenGL/Vulkan 同理保留 `VTX`、`TCS`、`TES`、`FS`、`FB`、`TS`、`MS`、`AS` 等阶段缩写。完整阶段名称仍可在页签、工具提示、无障碍说明和解释性文字中按上下文翻译。

ReplayLab 的 Python bridge 规范源位于本仓库 `replaylab/renderdoc_mcp_bridge`。它是 qrenderdoc 的首方内置集成，不是可发现、可启停或可由用户目录覆盖的 Python 扩展。构建/部署副本固定放在 `x64/<Configuration>/integrations/replaylab/python/renderdoc_mcp_bridge`；qrenderdoc 不扫描 `extensions/`，不读取 `%APPDATA%` 下的同名包，也不使用 `extension.json`。根目录的 `integration.json` 只描述首方包版本和入口，不参与扩展发现。

Python 创建的 UI 仍必须直接调用 `QtCore.QCoreApplication.translate("ReplayLabProjects", "English source")`，复用 qrenderdoc 已安装的翻译器，不加载第二份目录。`update_translations.ps1` 会从仓库内规范源自动执行 AST 提取：递归发现 `project_*.py`，并扫描 `__init__.py` 中 `replaylab-project-i18n-begin` / `replaylab-project-i18n-end` 标记内的项目 UI；生成 `qrenderdoc/Translations/ReplayLabProjects.cpp` 供 Qt 5 `lupdate` 扫描，再将 `replaylab_zh_CN.json` 中经过审校的译文同步到主 TS。新增 Python 按钮、标题、列表项、页签、菜单、提示或状态若直接使用英文/中文、通过局部常量间接绕过、使用错误 context，或尚无审校译文，更新/检查都会失败。Python 提取暂不支持 Qt numerus，禁止使用 `%n`，应使用完整句子与 `%1`、`%2` 等位置参数。

生成 `.qm` 后还必须重新链接 qrenderdoc，因为语言包嵌入在资源中。工程构建会把首方 bridge 放入固定集成目录；`deploy_replaylab_integration.ps1` 提供相同布局的显式本地校验/部署入口。完整的低内存本地发布流程为：

```powershell
$qtBin = "C:\Qt\5.15.2\msvc2019_64\bin"
./util/i18n/update_translations.ps1 -QtBin $qtBin
./util/i18n/update_translations.ps1 -QtBin $qtBin -Check
./util/i18n/build_translations.ps1 -QtBin $qtBin -RequireComplete
$solutionDir = (Resolve-Path .).Path.TrimEnd('\') + '\'
MSBuild qrenderdoc/qrenderdoc_local.vcxproj /m:2 /t:Build /p:Configuration=Development /p:Platform=x64 "/p:SolutionDir=$solutionDir"
./util/i18n/deploy_replaylab_integration.ps1 -Configuration Development
```

关闭 qrenderdoc 后再显式部署，完成后重启。部署脚本会递归同步规范源的全部 Python 模块并保留相对目录，同时同步根目录 `integration.json`；`extension.json`、`__pycache__` 和其他构建缓存不会进入运行目录。所有目标路径均限制在固定集成包根目录内，并逐文件校验 SHA-256。以后新增子包或嵌套 `project_*.py` 会自动进入发行包与本地化提取。规范源、审校映射、生成注册表和 TS 现在同属一个提交，CI 必须精确匹配，不再使用跨仓库 source-subset 兼容模式。

这些紧凑节点的 API 序列、`SO`/`XFB` 动态替换和绘制字段由 `glossary.zh_CN.yaml` 的 `compact_pipeline_flows` 契约检查。新增管线查看器或阶段时必须同步更新该契约；不要用 `tr("Vertex Shader")` 等完整名称替换 `setStages()` 第一组中的 `lit("VS")`。

CI 会做三层检查：重新提取并比较 TS 消息键、检查 TS 的占位符/HTML/文件过滤器/受保护 token/术语规则、全树检查绕过翻译提取的裸 `lit()` 或 UI setter 字符串。`source_literals_baseline.json` 只记录门禁启用前的遗留指纹；不要为新代码扩充它。新增按钮或菜单若没有进入翻译流程，合并检查会直接失败；新技术标识应写明 `i18n-ignore` 理由。

质量检查还会逐字保护 HTML 属性和 CSS/代码块、反引号代码、命令行选项、换行、首尾空白以及 Qt 快捷键标记。发布构建应使用 `build_translations.ps1 -RequireComplete`，确保没有任何活动条目仍为 `unfinished`。

## 人工审校规则

只有人工确认过上下文的译文才应移除 `type="unfinished"`。机器翻译可以作为草稿，但不能直接标为完成。`Normal` 等多义词必须结合控件、管线阶段和 API 判断；例如网格属性译为“法线”，进程正常退出译为“正常”，常规模式可能译为“常规”，`NORMAL0` semantic 则保持不变。

计算、光线追踪或网格着色器的 `Dispatch` 统一译为“调度”；`Drawcall` 统一译为“绘制调用”。这些术语以及 RenderDoc、Qt、API 名称和着色器阶段缩写由术语库和 CI 共同约束。
