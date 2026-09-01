# 品牌统一：`rendertest` / `gfxdiag` → `TinecmaTool`

> 提交：`c6636e48e0`（2026-06-22）
> 分支：`endfield/import` @ https://github.com/Tinenc/GraphicsDebuggerRdcTools
> 历史背景：见 [`ENDFIELD_CHANGES.md`](ENDFIELD_CHANGES.md) 第七章

---

## 1. 背景

`butteruni/renderdoc` 的 `Endfield` 分支在改名过程中留下了**两套不一致**的品牌名：

| 层 | 名字 |
|---|---|
| 5 个 `vcxproj` 的 `<ProjectName>` / `<TargetName>` | `rendertest*` |
| C++ 源码硬编码（路径、Vulkan layer、env、event、INI、symbol cache、注册表备份…） | `gfxdiag*` |

结果是 UI 启动时找不到注入器二进制：

```
Couldn't start global hook.
Internal error: Can't launch renderdoccmd from
  '...\x64\Development\gfxdiagcmd.exe' (err 2)
```

之前只能每次编译完后**手动复制** `rendertest*` → `gfxdiag*` 来 hot-patch。

本次提交把所有品牌字串统一为 **`TinecmaTool`**，从根上修掉这个 bug。

---

## 2. 命名 / 大小写规则（修改代码时务必遵守）

| 场景 | 形式 | 示例 |
|---|---|---|
| 文件名 / `<ProjectName>` / `<TargetName>` / 显示名 / 标识符 | **PascalCase** `TinecmaTool` | `TinecmaTool.dll`、`qTinecmaTool.exe`、`TinecmaToolGlobalHookData64` |
| 宏 / 事件名 / 环境变量 / Vulkan 导出 / linker version 通配 / C API 入口 | **ALLCAPS** `TINECMATOOL_*` | `TINECMATOOL_CRASHHANDLE`、`ENABLE_VULKAN_TINECMATOOL_CAPTURE`、`VK_LAYER_TINECMATOOL_Capture`、`TINECMATOOL_GetAPI` |
| `sys_win32_hooks.cpp` 注入黑名单（走 `strlower()` 比较） | **lowercase** | `tinecmatoolcmd.exe`、`qtinecmatool.exe` |
| Android Java 包名（惯例全小写） | **lowercase** | `org.tinecmatool.tinecmatoolcmd` |

---

## 3. 完整映射表

### 3.1 二进制产物（Visual Studio 编译输出）

| vcxproj | `<ProjectName>` | 输出 |
|---|---|---|
| `renderdoc/renderdoc.vcxproj` | `TinecmaTool` | `TinecmaTool.dll` |
| `renderdoccmd/renderdoccmd.vcxproj` | `TinecmaToolcmd` | `TinecmaToolcmd.exe` |
| `renderdocshim/renderdocshim.vcxproj` | `TinecmaToolshim` | `TinecmaToolshim64.dll` / `TinecmaToolshim32.dll`（`TargetName` 后缀 32/64） |
| `qrenderdoc/qrenderdoc_local.vcxproj` | `qTinecmaTool` | `qTinecmaTool.exe` |
| `qrenderdoc/renderdocui_stub.vcxproj` | `TinecmaToolui_stub` | `TinecmaToolui.lib` |

### 3.2 跨平台 / 子系统

| 类别 | 旧值（rendertest 或 gfxdiag 任一） | 新值 |
|---|---|---|
| Android SO | `libVkLayer_GLES_GfxDiag.so` | `libVkLayer_GLES_TinecmaTool.so` |
| Android 包名 | `org.gfxdiag.gfxdiagcmd` | `org.tinecmatool.tinecmatoolcmd` |
| Vulkan layer 名 | `VK_LAYER_RTCAP_Capture` | `VK_LAYER_TINECMATOOL_Capture` |
| Vulkan 导出符号一族 | `VK_LAYER_RTCAP_*` | `VK_LAYER_TINECMATOOL_*` |
| Vulkan env (enable) | `ENABLE_VULKAN_RTCAP_CAPTURE` | `ENABLE_VULKAN_TINECMATOOL_CAPTURE` |
| Vulkan env (disable) | `DISABLE_VULKAN_RTCAP_CAPTURE_*` | `DISABLE_VULKAN_TINECMATOOL_CAPTURE_*` |
| 公共 C API 入口 | `RTCAP_GetAPI` / `pRTCAP_GetAPI` | `TINECMATOOL_GetAPI` / `pTINECMATOOL_GetAPI` |
| Linker version script | `RTCAP_*` | `TINECMATOOL_*` |
| Crash handler 事件名 | `RENDERTEST_CRASHHANDLE` / `GFXDIAG_CRASHHANDLE` | `TINECMATOOL_CRASHHANDLE` |
| Replay marker 符号 | `rendertest__replay__marker` / `gfxdiag__replay__marker` | `TinecmaTool__replay__marker` |
| Global hook 共享数据 | `RenderTestGlobalHookData64/32` | `TinecmaToolGlobalHookData64/32` |
| Shim DLL 名 (header 端) | `rendertestshim64.dll` / `rendertestshim32.dll` | `TinecmaToolshim64.dll` / `TinecmaToolshim32.dll` |
| Registry 备份文件 | `GfxDiag_RestoreGlobalHook.reg` | `TinecmaTool_RestoreGlobalHook.reg` |
| Symbol 缓存路径 | `%APPDATA%\gfxdiag\symbols` | `%APPDATA%\TinecmaTool\symbols` |
| INI 节名 | `[gfxdiag]` | `[TinecmaTool]` |
| BugReport zip 名 | `gfxdiag_report_*.zip` | `TinecmaTool_report_*.zip` |
| WGL 窗口类 | `gfxdiagGLclass` | `TinecmaToolGLclass` |
| PE 资源 `InternalName` / `OriginalFilename` / `ProductName` | `gfxdiag` / `GfxDiag` / `RenderTest` | `TinecmaTool`（exe 对应 `TinecmaToolcmd.exe`） |

---

## 4. 修改文件清单（共 35 个）

### 4.1 注入流程 / Windows 平台层

- `renderdoc/os/win32/win32_process.cpp` ⭐ 核心：路径拼接 `\\x64\\Development\\TinecmaToolcmd.exe` 等
- `renderdoc/os/win32/sys_win32_hooks.cpp` ⭐ 黑名单（lowercase 比较）
- `renderdoc/os/win32/win32_callstack.cpp` symbol cache + INI section
- `renderdoc/os/win32/win32_stringio.cpp`
- `renderdoc/os/win32/win32_libentry.cpp`
- `renderdoc/os/posix/posix_libentry.cpp`
- `renderdoc/core/crash_handler.h` 事件名
- `renderdoccmd/renderdoccmd_win32.cpp` 事件名（对端）

### 4.2 Vulkan layer

- `renderdoc/driver/vulkan/vk_layer.cpp` 导出符号 / 描述
- `renderdoc/driver/vulkan/vk_layer_android.cpp` Android 描述
- `renderdoc/driver/vulkan/renderdoc.json` layer manifest
- `renderdoc/common/globalconfig.h` 4 个核心 #define

### 4.3 公共 C API（**Breaking ABI**）

- `renderdoc/api/app/renderdoc_app.h` typedef `pTINECMATOOL_GetAPI`
- `renderdoc/api/replay/renderdoc_replay.h`
- `renderdoc/replay/app_api.cpp` 导出实现
- `renderdoc/replay/entry_points.cpp` 调用方
- `renderdoc/renderdoc.version` linker version script
- `renderdoc/rdocself.version` linker version script
- `util/test/demos/renderdoc_app.h`、`util/test/demos/test_common.cpp` 客户端示例同步

### 4.4 vcxproj / 资源 / OpenGL / UI

- `renderdoc/renderdoc.vcxproj`
- `renderdoccmd/renderdoccmd.vcxproj`
- `renderdocshim/renderdocshim.vcxproj`
- `qrenderdoc/qrenderdoc_local.vcxproj`
- `qrenderdoc/renderdocui_stub.vcxproj`
- `renderdoc/data/renderdoc.rc`（UTF-8）
- `qrenderdoc/Resources/qrenderdoc.rc`（UTF-8）
- `renderdoccmd/renderdoccmd.rc`（**UTF-16 LE**，单独处理）
- `renderdocshim/renderdocshim.h` `TinecmaToolGlobalHookData*`
- `renderdoc/driver/gl/wgl_platform.cpp` 窗口类
- `qrenderdoc/Code/qrenderdoc.cpp`
- `qrenderdoc/Windows/MainWindow.cpp`
- `qrenderdoc/renderdocui_stub.cpp`

### 4.5 文档 / 记忆

- `ENDFIELD_CHANGES.md`（追加第七章）
- `.cursor/rules/project-overview.mdc`（全部重写为新现状）
- `BRANDING_TINECMATOOL.md`（本文件）

---

## 5. 编译后期望

直接 build `renderdoc.sln`（VS 2019/2022，Debug/Release × x64/Win32），输出位于：

```
x64\Development\TinecmaTool.dll
x64\Development\TinecmaToolcmd.exe
x64\Development\TinecmaToolshim64.dll
x64\Development\qTinecmaTool.exe
Win32\Development\TinecmaToolshim32.dll
```

UI 启动 `qTinecmaTool.exe`，注入器查找的就是同目录的 `TinecmaToolcmd.exe`，路径完全自洽。

**不再需要 hot-patch 复制步骤。**

---

## 6. 向后兼容性破坏

外部调用方必须同步更新：

| 调用方 | 旧 | 新 |
|---|---|---|
| `GetProcAddress(hModule, ...)` 取 in-app API | `"RTCAP_GetAPI"` | `"TINECMATOOL_GetAPI"` |
| Vulkan layer enumeration | `VK_LAYER_RTCAP_Capture` | `VK_LAYER_TINECMATOOL_Capture` |
| 启用 / 禁用 capture 的环境变量 | `*_RTCAP_*` | `*_TINECMATOOL_*` |
| Android 已安装包 | `org.gfxdiag.gfxdiagcmd` | `org.tinecmatool.tinecmatoolcmd`（需先卸载老包） |
| Symbol 缓存目录 | `%APPDATA%\gfxdiag\symbols` | `%APPDATA%\TinecmaTool\symbols`（首次会重建） |
| `renderdoc.ini` 节 | `[gfxdiag]` | `[TinecmaTool]`（旧配置需要迁移） |

---

## 7. 实施手法（便于将来复盘）

1. 用 ripgrep 在仓库根（排除 `3rdparty`）扫描所有 `rendertest|gfxdiag|GfxDiag|RTCAP|GFXDIAG|RenderTest` 出现位置，拿到 30+ 文件清单。
2. 按文件类型分组：
   - **UTF-8 源码 / vcxproj / .rc**：PowerShell 字面替换，规则按**长度逆序**排列避免子串误伤（`rendertestcmd.exe` 在 `rendertest` 前替换等）。
   - **UTF-16 LE 资源 `renderdoccmd.rc`**：单独用 `[System.Text.Encoding]::Unicode` 读写。
   - **大小写敏感点**：`sys_win32_hooks.cpp` 注入黑名单单独用 lowercase 串改写。
3. 用 `-i`（case-insensitive）二次扫描，发现并修掉一处遗漏：`renderdoccmd_win32.cpp` 中的 `"RENDERTEST_CRASHHANDLE"` 事件名（必须与 `crash_handler.h` 中 `CreateEventA("TINECMATOOL_CRASHHANDLE")` 同步）。
4. 验证 `<ProjectName>` / `<TargetName>` / `<RootNamespace>` 全部已迁移。
5. 提交 + 推送 `endfield/import` 分支（不触及 `main`）。
