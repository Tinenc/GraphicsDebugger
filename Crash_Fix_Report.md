# TinecmaTools 崩溃修复分析与修改文档

## 1. 问题现象描述

在使用 `TinecmaTools`（基于 RenderDoc 定制的图形调试工具）进行 Vulkan 抓帧文件（Capture）解析或回放时，程序发生严重崩溃。

通过分析崩溃日志（例如 `TinecmaTools_app_2026.03.11_01.03.25.log`），发现以下关键错误信息：

1. **回放初始化错误（核心诱因）**：
   ```text
   core.cpp( 625) - Error - Initialising replay within non-replaying app. Did you properly export replay marker in host executable or library, or are you trying to replay directly with a self-hosted capture build?
   ```
2. **资源管理器断言失败（直接崩溃原因）**：
   ```text
   resource_manager.h( 838) - Error - Assertion failed: 'm_ResourceRecords.empty()' 
   vk_manager.h( 183) - Error - Assertion failed: 'm_CurrentResourceMap.empty()'
   ```
3. **资源引用丢失风暴**：
   断言失败后，日志中出现数百条 `vk_serialise.cpp` 报出的警告，提示丢失了各种 Vulkan 资源（如 `VkDevice`, `VkImage`, `VkBuffer` 等）的引用，最终导致程序彻底崩溃。

---

## 2. 根本原因分析

### 2.1 状态判断错误
日志中的 `Initialising replay within non-replaying app` 错误表明，程序在尝试初始化回放核心时，当前进程并没有被正确识别为“回放程序”（即内部函数 `IsReplayApp()` 返回了 `false`）。这导致了后续的资源管理器状态机发生严重混乱，未能正确清理资源，从而触发了断言失败。

### 2.2 标记检测机制失效
在 RenderDoc 的架构中，为了区分“被注入的抓帧宿主”和“执行回放的工具程序”，回放程序（如 `qrenderdoc` 或 `renderdoccmd`）会通过宏 `REPLAY_PROGRAM_MARKER()` 导出一个特定的标记函数。动态库（DLL/SO）在加载时，会检测进程中是否存在这个导出函数，如果存在，则将自身状态设置为回放模式（`SetReplayApp(true)`）。

### 2.3 宏定义与检测逻辑不匹配
在定制化 `TinecmaTools` 的过程中，项目的基础名称（`RDOC_BASE_NAME`）在 `CMakeLists.txt` 中被从 `renderdoc` 修改为了 `TinecmaTools`。

这导致了以下冲突：
1. **检测逻辑**（`win32_libentry.cpp` / `posix_libentry.cpp`）：
   代码使用 `LibraryHooks::Detect(STRINGIZE(RDOC_BASE_NAME) "__replay__marker")` 进行检测。由于宏展开，它实际上在寻找名为 `TinecmaTools__replay__marker` 的函数。
2. **导出逻辑**（`renderdoc_replay.h`）：
   `REPLAY_PROGRAM_MARKER()` 宏内部**硬编码**了导出函数名为 `renderdoc__replay__marker`。

**结论**：由于导出函数名为 `renderdoc__replay__marker`，而检测时寻找的是 `TinecmaTools__replay__marker`，导致匹配失败。动态库加载时未能检测到回放标记，`SetReplayApp(true)` 未被执行，最终引发了一系列状态错误和崩溃。

---

## 3. 修复方案

为了解决名称不匹配的问题，同时兼顾定制名称和原版硬编码名称，我们修改了动态库入口文件中的检测逻辑。使其在检测时，既检查基于 `RDOC_BASE_NAME` 拼接的名称，也检查硬编码的 `renderdoc__replay__marker`。

### 3.1 修改的文件

1. `renderdoc\os\win32\win32_libentry.cpp`
2. `renderdoc\os\posix\posix_libentry.cpp`

### 3.2 代码修改详情

**修改前：**
```cpp
// search for an exported symbol with this name, typically renderdoc__replay__marker
if(LibraryHooks::Detect(STRINGIZE(RDOC_BASE_NAME) "__replay__marker"))
{
    RDCDEBUG("Not creating hooks - in replay app");
    RenderDoc::Inst().SetReplayApp(true);
    // ...
}
```

**修改后：**
```cpp
// search for an exported symbol with this name, typically renderdoc__replay__marker
if(LibraryHooks::Detect(STRINGIZE(RDOC_BASE_NAME) "__replay__marker") ||
   LibraryHooks::Detect("renderdoc__replay__marker"))
{
    RDCDEBUG("Not creating hooks - in replay app");
    RenderDoc::Inst().SetReplayApp(true);
    // ...
}
```

---

## 4. 后续操作指南

1. **重新编译**：请重新编译整个 `TinecmaTools` 解决方案（包括 DLL、EXE 等所有相关模块）。
2. **验证测试**：运行重新编译后的 `TinecmaTools`，再次尝试加载或回放之前崩溃的 Vulkan 抓帧文件。
3. **预期结果**：程序应能正确识别回放标记，不再输出 `Initialising replay within non-replaying app` 错误，资源管理器能够正常工作，崩溃问题得到解决。
