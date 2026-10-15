# TinecmaTools：修改 RenderDoc 特征码绕过反作弊检测完整指南

## 前言

通过模拟器截帧虽然是常见方案，但部分游戏在移动端与 PC 端使用了不同的 Shader，因此需要直接在 PC 环境下使用 RenderDoc 截取 Shader 进行分析。

本文记录了基于 RenderDoc v1.x 定制 **TinecmaTools** 的完整特征码替换方案，目标是将二进制中所有可被扫描的 `renderdoc` 特征字符串替换为 `TinecmaTools`，同时修复定制化过程中引入的崩溃问题。

---

## 检测机制分析

反作弊系统对 RenderDoc 的检测主要集中在以下几个维度：

1. **导出符号扫描**：扫描进程中加载的 DLL 是否导出 `renderdoc__replay__marker` 符号
2. **进程名检测**：黑名单匹配 `renderdoccmd.exe`、`qrenderdoc.exe`
3. **内存字符串扫描**：扫描已加载模块中的特征字符串（如 `RenderDoc`、`renderdoc.dll`）
4. **内核对象名检测**：匹配命名管道、共享内存等内核对象名（如 `RenderDocBreakpadServer`、`RenderDocGlobalHookData`）
5. **注册表路径检测**：检查 `RenderDoc.RDCCapture.1` 等注册表项
6. **注入方式检测**：CrashSight 等系统对 CreateRemoteThread 注入方式有额外监控

---

## 前期准备

- 源码：[https://github.com/baldurk/renderdoc/tree/v1.x](https://github.com/baldurk/renderdoc/tree/v1.x)
- 编译工具：VS2022，需额外安装 v140 (VS2015) 工具集组件，或在项目属性中手动更新平台工具集
- 在 CMakeLists.txt 中将 `RDOC_BASE_NAME` 从 `renderdoc` 修改为 `TinecmaTools`

---

## 一、核心 DLL — Replay Marker 导出符号

这是**最核心的修改**。RenderDoc 通过 `REPLAY_PROGRAM_MARKER()` 宏在所有回放可执行文件中导出一个特定符号，DLL 加载时通过动态查找该符号来判断当前进程是回放工具还是目标游戏进程。

若宏硬编码导出 `renderdoc__replay__marker`，而检测逻辑按照 `RDOC_BASE_NAME` 拼接寻找 `TinecmaTools__replay__marker`，两者不匹配将导致 DLL 误判为游戏进程并注入 GPU 钩子，引发崩溃。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `renderdoc/api/replay/renderdoc_replay.h` | 52 | 函数名修改 | `renderdoc__replay__marker()` | `TinecmaTools__replay__marker()` |

---

## 二、Replay Marker 检测逻辑

DLL 入口处检测当前进程是否为回放程序，需与上面导出的符号名一致。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `renderdoc/os/win32/win32_libentry.cpp` | 53 | 去除冗余兜底 | `Detect(STRINGIZE(RDOC_BASE_NAME) "__replay__marker") \|\| Detect("renderdoc__replay__marker")` | `Detect(STRINGIZE(RDOC_BASE_NAME) "__replay__marker")` |
| `renderdoc/os/posix/posix_libentry.cpp` | 34 | 去除冗余兜底 | 同上 | 同上 |

---

## 三、进程创建与注入 — 可执行文件路径硬编码

DLL 在注入子进程时需要定位同目录下的命令行工具，这些路径均为硬编码字符串。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `renderdoc/os/win32/win32_process.cpp` | 738 | 字符串替换 | `\\Win32\\Development\\renderdoccmd.exe` | `\\Win32\\Development\\TinecmaToolscmd.exe` |
| `renderdoc/os/win32/win32_process.cpp` | 751 | 字符串替换 | `\\Win32\\Release\\renderdoccmd.exe` | `\\Win32\\Release\\TinecmaToolscmd.exe` |
| `renderdoc/os/win32/win32_process.cpp` | 766 | 字符串替换 | `\\x86\\renderdoccmd.exe` | `\\x86\\TinecmaToolscmd.exe` |
| `renderdoc/os/win32/win32_process.cpp` | 778 | 字符串替换 | `\\x64\\Development\\renderdoccmd.exe` | `\\x64\\Development\\TinecmaToolscmd.exe` |
| `renderdoc/os/win32/win32_process.cpp` | 791 | 字符串替换 | `\\x64\\Release\\renderdoccmd.exe` | `\\x64\\Release\\TinecmaToolscmd.exe` |
| `renderdoc/os/win32/win32_process.cpp` | 811 | 字符串替换 | `\\renderdoccmd.exe` | `\\TinecmaToolscmd.exe` |
| `renderdoc/os/win32/win32_process.cpp` | 929 | 错误信息替换 | `32-bit renderdoccmd to capture` | `32-bit TinecmaToolscmd to capture` |
| `renderdoc/os/win32/win32_process.cpp` | 933 | 错误信息替换 | `32-bit renderdoccmd to capture` | `32-bit TinecmaToolscmd to capture` |
| `renderdoc/os/win32/win32_process.cpp` | 966 | 错误信息替换 | `32-bit renderdoccmd returned` | `32-bit TinecmaToolscmd returned` |
| `renderdoc/os/win32/win32_process.cpp` | 1501 | 字符串替换 | `\\renderdoccmd.exe` | `\\TinecmaToolscmd.exe` |
| `renderdoc/os/win32/win32_process.cpp` | 1520 | 字符串替换 | `\\Win32\\Development\\renderdoccmd.exe` | `\\Win32\\Development\\TinecmaToolscmd.exe` |
| `renderdoc/os/win32/win32_process.cpp` | 1531 | 字符串替换 | `\\Win32\\Release\\renderdoccmd.exe` | `\\Win32\\Release\\TinecmaToolscmd.exe` |
| `renderdoc/os/win32/win32_process.cpp` | 1539 | 字符串替换 | `\\x86\\renderdoccmd.exe` | `\\x86\\TinecmaToolscmd.exe` |

---

## 四、Shim DLL 路径硬编码

Shim DLL 负责在目标进程启动时完成钩子注入，路径同样为硬编码字符串。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `renderdoc/os/win32/win32_process.cpp` | 1510 | 字符串拼接 | `\\renderdocshim64.dll` | `\\TinecmaToolsshim64.dll` |
| `renderdoc/os/win32/win32_process.cpp` | 1519 | 字符串拼接 | `\\Win32\\Development\\renderdocshim32.dll` | `\\Win32\\Development\\TinecmaToolsshim32.dll` |
| `renderdoc/os/win32/win32_process.cpp` | 1530 | 字符串拼接 | `\\Win32\\Release\\renderdocshim32.dll` | `\\Win32\\Release\\TinecmaToolsshim32.dll` |
| `renderdoc/os/win32/win32_process.cpp` | 1538 | 字符串拼接 | `\\x86\\renderdocshim32.dll` | `\\x86\\TinecmaToolsshim32.dll` |
| `renderdoc/os/win32/win32_process.cpp` | 1545 | 字符串拼接 | `\\renderdocshim32.dll` | `\\TinecmaToolsshim32.dll` |

---

## 五、进程白名单 — 避免注入到 TinecmaTools 自身

Global Hook 模式下，钩子会对系统所有新进程生效，需要将自身程序加入白名单以避免自我注入。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `renderdoc/os/win32/sys_win32_hooks.cpp` | 340 | 字符串替换 | `renderdoccmd.exe` \|\| `qrenderdoc.exe` | `tinecmatoolscmd.exe` \|\| `qtinecmatools.exe` |
| `renderdoc/os/win32/sys_win32_hooks.cpp` | 349 | 字符串替换 | `renderdoccmd.exe` \|\| `qrenderdoc.exe` | `tinecmatoolscmd.exe` \|\| `qtinecmatools.exe` |

---

## 六、崩溃处理 — 命名内核对象

Breakpad 崩溃处理模块创建命名管道和转储目录，这些名称均为可扫描的特征字符串。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `renderdoc/core/crash_handler.h` | 62 | 路径替换 | `RenderDoc\\dumps\\a` | `TinecmaTools\\dumps\\a` |
| `renderdoc/core/crash_handler.h` | 168 | 管道名替换 | `RenderDocBreakpadServer%llu` | `TinecmaToolsBreakpadServer%llu` |
| `renderdoccmd/renderdoccmd_win32.cpp` | 149 | 窗口类名 | `renderdoccmd` | `TinecmaToolscmd` |
| `renderdoccmd/renderdoccmd_win32.cpp` | 196 | 窗口类名 | `renderdoccmd` | `TinecmaToolscmd` |
| `renderdoccmd/renderdoccmd_win32.cpp` | 586 | 转储路径 | `RenderDoc` | `TinecmaTools` |
| `renderdoccmd/renderdoccmd_win32.cpp` | 818 | 模块句柄 | `GetModuleHandleA("renderdoc.dll")` | `GetModuleHandleA("TinecmaTools.dll")` |
| `renderdoccmd/renderdoccmd_win32.cpp` | 927 | 窗口类名 | `renderdoccmd` | `TinecmaToolscmd` |

---

## 七、文件路径与注册表

UI 程序在查找关联文件、写入日志、注册文件类型时使用的路径和注册表键名。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `renderdoc/os/win32/win32_stringio.cpp` | 289 | 路径拼接 | `/qrenderdoc.exe` | `/qTinecmaTools.exe` |
| `renderdoc/os/win32/win32_stringio.cpp` | 300 | 路径拼接 | `/../qrenderdoc.exe` | `/../qTinecmaTools.exe` |
| `renderdoc/os/win32/win32_stringio.cpp` | 316 | 注册表路径 | `RenderDoc.RDCCapture.1\\DefaultIcon` | `TinecmaTools.RDCCapture.1\\DefaultIcon` |
| `renderdoc/os/win32/win32_stringio.cpp` | 358 | 日志路径 | `RenderDoc\\%ls_...` | `TinecmaTools\\%ls_...` |
| `renderdoc/os/win32/win32_stringio.cpp` | 367 | 日志路径 | `RenderDoc\\%ls_...` | `TinecmaTools\\%ls_...` |
| `renderdoc/os/win32/win32_process.cpp` | 1382 | 注册表备份文件名 | `RenderDoc_RestoreGlobalHook.reg` | `TinecmaTools_RestoreGlobalHook.reg` |

---

## 八、OpenGL 窗口类名

OpenGL 驱动创建的隐藏窗口使用该类名，属于可扫描的用户态特征。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `renderdoc/driver/gl/wgl_platform.cpp` | 28 | 宏定义 | `L"renderdocGLclass"` | `L"TinecmaToolsGLclass"` |

---

## 九、Global Hook 共享内存名称

Global Hook 通过命名共享内存在注入器与各进程间通信，该名称会出现在内核对象列表中。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `renderdocshim/renderdocshim.h` | 36 | 宏定义 | `"RenderDocGlobalHookData64"` | `"TinecmaToolsGlobalHookData64"` |
| `renderdocshim/renderdocshim.h` | 39 | 宏定义 | `"RenderDocGlobalHookData32"` | `"TinecmaToolsGlobalHookData32"` |

---

## 十、资源文件

PE 资源版本信息中的字符串会被部分检测工具枚举。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `renderdoc/data/renderdoc.rc` | 86 | 资源字符串 | `"CompanyName", "RenderDoc"` | `"CompanyName", "TinecmaTools"` |
| `renderdoc/data/renderdoc.rc` | 87 | 资源字符串 | `"FileDescription", "Core DLL for RenderDoc"` | `"FileDescription", "Core DLL for TinecmaTools"` |
| `renderdoc/data/renderdoc.rc` | 89 | 资源字符串 | `"InternalName", "renderdoc"` | `"InternalName", "TinecmaTools"` |
| `renderdoc/data/renderdoc.rc` | 90 | 资源字符串 | `"LegalCopyright", "Copyright ... RenderDoc"` | `"LegalCopyright", "Copyright 2025 TinecmaTools"` |
| `renderdoc/data/renderdoc.rc` | 91 | 资源字符串 | `"OriginalFilename", "renderdoc.dll"` | `"OriginalFilename", "TinecmaTools.dll"` |
| `renderdoc/data/renderdoc.rc` | 92 | 资源字符串 | `"ProductName", "RenderDoc"` | `"ProductName", "TinecmaTools"` |

---

## 十一、Qt UI 层

UI 程序自身输出的日志、翻译上下文、Python 模块名等均以字符串形式存在于二进制中。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `qrenderdoc/Code/qrenderdoc.cpp` | 171 | 翻译上下文 | `translate("qrenderdoc", ...)` | `translate("qTinecmaTools", ...)` |
| `qrenderdoc/Code/qrenderdoc.cpp` | 196 | 日志输出 | `"QRenderDoc initialising."` | `"QTinecmaTools initialising."` |
| `qrenderdoc/Code/qrenderdoc.cpp` | 265 | 会话名 | `"QRenderDoc"` | `"QTinecmaTools"` |
| `qrenderdoc/Code/qrenderdoc.cpp` | 391 | 版本输出 | `"QRenderDoc v%s"` | `"QTinecmaTools v%s"` |
| `qrenderdoc/Code/ReplayManager.cpp` | 469 | 日志输出 | `"QRenderDoc - renderer created for"` | `"QTinecmaTools - renderer created for"` |
| `qrenderdoc/Code/QRDUtils.cpp` | 273、345、3402、3414 | 翻译上下文 | `translate("qrenderdoc", ...)` | `translate("qTinecmaTools", ...)` |
| `qrenderdoc/Code/pyrenderdoc/PythonContext.cpp` | 113 | Python 程序名 | `L"qrenderdoc"` | `L"qTinecmaTools"` |
| `qrenderdoc/Code/pyrenderdoc/PythonContext.cpp` | 236 | Python 模块注册 | `AppendInittab("qrenderdoc", ...)` | `AppendInittab("qTinecmaTools", ...)` |
| `qrenderdoc/Code/pyrenderdoc/PythonContext.cpp` | 295 | Python 模块导入 | `AddObject(..., "qrenderdoc", ...)` | `AddObject(..., "qTinecmaTools", ...)` |
| `qrenderdoc/Code/pyrenderdoc/PythonContext.cpp` | 532 | Python 模块遍历 | `{"renderdoc", "qrenderdoc"}` | `{"renderdoc", "qTinecmaTools"}` |
| `qrenderdoc/Windows/MainWindow.ui` | 14 | UI 窗口标题 | `"QRenderDoc"` | `"QTinecmaTools"` |
| `qrenderdoc/Windows/MainWindow.cpp` | 1215 | 窗口标题拼接 | `lit("RenderDoc ")` | `lit("TinecmaTools ")` |

---

## 崩溃根因补充说明

在修改 `RDOC_BASE_NAME` 为 `TinecmaTools` 之后，`renderdoc_replay.h` 中的 `REPLAY_PROGRAM_MARKER()` 宏仍硬编码导出 `renderdoc__replay__marker`，而 `win32_libentry.cpp` 的检测逻辑按宏拼接寻找 `TinecmaTools__replay__marker`，导致名字不匹配，`IsReplayApp()` 返回 `false`，触发以下崩溃链：

```
core.cpp(625) - Initialising replay within non-replaying app.
resource_manager.h(838) - Assertion failed: 'm_ResourceRecords.empty()'
vk_manager.h(183) - Assertion failed: 'm_CurrentResourceMap.empty()'
```

正确修复方式是将 `renderdoc_replay.h` 第 52 行的导出符号名改为 `TinecmaTools__replay__marker`，使导出与检测保持一致，而非在检测侧加兜底。

---

## 十二、Analytics 分析字段名

Analytics 模块将程序版本等信息序列化为 JSON 上报，字段名以宽字符串形式存在于 `qTinecmaTools.exe` 中，需要与 `qrenderdoc` 一同替换。该字段涉及头文件、实现文件、调用点三处，必须保持一致否则编译报错。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `qrenderdoc/Code/Interface/Analytics.h` | 148 | 结构体成员 | `QString RenderDocVersion;` | `QString TinecmaToolsVersion;` |
| `qrenderdoc/Code/Interface/Analytics.cpp` | 235 | 宏定义字段名 | `DOCUMENT_ANALYTIC(RenderDocVersion, "The RenderDoc build version...")` | `DOCUMENT_ANALYTIC(TinecmaToolsVersion, "The TinecmaTools build version...")` |
| `qrenderdoc/Code/Interface/Analytics.cpp` | 267 | 描述字符串 | `"Did the user employ RenderDoc as an image..."` | `"Did the user employ TinecmaTools as an image..."` |
| `qrenderdoc/Code/Interface/Analytics.cpp` | 343 | 序列化字段 | `ANALYTIC_SERIALISE(Metadata.RenderDocVersion)` | `ANALYTIC_SERIALISE(Metadata.TinecmaToolsVersion)` |
| `qrenderdoc/Code/qrenderdoc.cpp` | 650 | 赋值调用 | `ANALYTIC_SET(Metadata.RenderDocVersion, ...)` | `ANALYTIC_SET(Metadata.TinecmaToolsVersion, ...)` |

---

## 十三、Python 绑定 — SWIG 模块名与类型前缀

RenderDoc 使用 SWIG 为 Qt UI 层生成 Python 绑定。SWIG 接口文件中声明的模块名会被硬编码进所有生成的 Python 类型名（如 `qrenderdoc.CaptureSettings`），在编译后以 ASCII 字符串形式存在于 `qTinecmaTools.exe` 中。

此外，`interface_check.h` 中有一处运行时前缀剥离逻辑，专门识别 `qrenderdoc.` 开头的类型名，需要同步更新。

| 文件路径 | 行号 | 修改类型 | 修改前 | 修改后 |
|---|---|---|---|---|
| `qrenderdoc/Code/pyrenderdoc/qrenderdoc.i` | 2 | SWIG 模块声明 | `%module(...) qrenderdoc` | `%module(...) qTinecmaTools` |
| `qrenderdoc/Code/pyrenderdoc/interface_check.h` | 81 | 前缀剥离逻辑 | `beginsWith("qrenderdoc.")` → `erase(0, 11)` | `beginsWith("qTinecmaTools.")` → `erase(0, 14)` |

**说明**：修改 `qrenderdoc.i` 后必须触发 SWIG 重新生成（正常编译会自动触发），否则生成的 `.cxx` 文件中的类型名仍为 `qrenderdoc.XXX`。编译后可在 `obj/.../generated/qrenderdoc_module_python.cxx` 中确认类型名已变为 `qTinecmaTools.CaptureSettings` 等。

---

## 编译验证结果

在 VS2022 x64 Release 配置下完整编译后，对输出二进制进行字节级扫描的结果：

| 二进制文件 | 检测项 | 旧特征 | 新特征 |
|---|---|---|---|
| `TinecmaTools.dll` | Replay Marker 导出符号 | ✅ 已清除 | ✅ 存在 |
| `TinecmaTools.dll` | OpenGL 窗口类名 | ✅ 已清除 | ✅ 存在 |
| `TinecmaTools.dll` | 进程路径硬编码 | ✅ 已清除 | ✅ 存在 |
| `TinecmaToolsshim64.dll` | 全局 Hook 共享内存名 | ✅ 已清除 | ✅ 存在 |
| `TinecmaToolscmd.exe` | cmd 窗口/进程名 | ✅ 已清除 | ✅ 存在 |
| `qTinecmaTools.exe` | Qt UI 日志/标题字符串 | ✅ 已清除 | ✅ 存在 |
| `qTinecmaTools.exe` | Python 类型前缀 | ✅ 已清除 | ✅ 存在 |
| `TinecmaTools.dll` | Breakpad 管道名 | ⚪ 此构建未启用 | — |

**注意**：源码目录本身名为 `qrenderdoc`，MSVC 会将源文件路径内嵌至二进制的异常展开表（`.pdata`/`.xdata`）中，形成如 `...\qrenderdoc\Code\...` 的路径字符串。这类字符串位于调试段，并非功能性特征，常规扫描工具不会针对该区域。若需彻底消除，需将整个 `qrenderdoc` 源码目录重命名并同步更新所有 `vcxproj` 引用。
