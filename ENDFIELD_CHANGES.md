# Endfield 分支与原生 RenderDoc 的差异

> 与上游 [`baldurk/renderdoc`](https://github.com/baldurk/renderdoc) v1.x 的完整对比与修改清单。

## 基准

| 项 | 值 |
|---|---|
| **上游基线** | `baldurk/renderdoc` v1.x，合并基点 `76cc0c3b29`（2026-04-06） |
| **Fork 远程** | https://github.com/butteruni/renderdoc |
| **Fork 独有 commit** | 3 个 |
| **覆盖文件** | 26 个源文件，约 +400/-150 行净增 |
| **作者** | butteruni \<butteruni@gmail.com\> |
| **跨度** | 2026-04-06 至 2026-04-08（3 天） |

---

## 一、行为性改动（真正改变 hook/inject 逻辑）

### 1. `renderdoc/os/win32/win32_process.cpp` — 注入流程加固（+133 行）

- **`OpenProcess` 权限精确化**：从粗放申请改为最小集
  `PROCESS_CREATE_THREAD | QUERY_INFORMATION | VM_OPERATION | VM_WRITE | VM_READ | SYNCHRONIZE`
- **空句柄保护**：所有 `WaitForSingleObject` / `CloseHandle` 调用前先 `if(hProcess)`；原版裸调用对受保护进程会 access denied 然后崩溃
- **失败路径细化错误**：注入失败后用 `SET_ERROR_RESULT` 上报
  `"Couldn't reopen process %u after injection"`，便于诊断
- **`InjectFunctionCall` 套入 `if(hProcess)` 块**：原流程是一旦 `OpenProcess` 失败仍继续走 inject，这里收口

### 2. `renderdoc/os/win32/sys_win32_hooks.cpp` — 注入黑名单（+8 行）

- 子进程枚举时跳过的进程：
  - 原版：`renderdoccmd.exe / qrenderdoc.exe`
  - 改为：`gfxdiagcmd.exe / qgfxdiag.exe / **platformprocess.exe**`
- `platformprocess.exe` 是**鸣潮 Wuwa** 启动器派生的子进程；注入它会导致游戏崩溃或反作弊触发，所以加入豁免

> 这就是分支名 "Endfield" 的真正含义——为支持**鸣潮**这类用 PlatformProcess 子进程模型的游戏做 hook 行为修正。

---

## 二、品牌伪装改名（绕过基础字符串特征检测，**不改行为**）

两轮重命名后，命名空间彻底脱离 `renderdoc` 字样：

| 类别 | 原版 (baldurk) | Commit 1 之后 | Commit 2 之后（最终） |
|---|---|---|---|
| 主 DLL | `renderdoc.dll` | `rendertest.dll` | `gfxdiag.dll` |
| 命令行 | `renderdoccmd.exe` | `rendertestcmd.exe` | `gfxdiagcmd.exe` |
| Shim | `renderdocshim64.dll` | `rendertestshim64.dll` | `gfxdiagshim64.dll` |
| UI | `qrenderdoc.exe` | `qrendertest.exe` | `qgfxdiag.exe` |
| Vulkan layer 名 | `VK_LAYER_RENDERDOC_Capture` | （未改） | `VK_LAYER_RTCAP_Capture` |
| Vulkan 导出符号 | `VK_LAYER_RENDERDOC_*` 一族 | （未改） | `VK_LAYER_RTCAP_*` 一族 |
| Vulkan 环境变量 | `ENABLE_VULKAN_RENDERDOC_CAPTURE` | （未改） | `ENABLE_VULKAN_RTCAP_CAPTURE` |
| Android SO | `libVkLayer_GLES_RenderDoc.so` | （未改） | `libVkLayer_GLES_GfxDiag.so` |
| Android 包名 | `org.renderdoc.renderdoccmd` | （未改） | `org.gfxdiag.gfxdiagcmd` |
| Replay marker 符号 | `renderdoc__replay__marker` | `rendertest__replay__marker` | `gfxdiag__replay__marker` |
| 共享内存 hook 名 | `RenderDocGlobalHookData64` | `RenderTestGlobalHookData64` | （未改） |
| Crash 事件名 | `RenderDoc_CRASHHANDLE` | `RenderTest_CRASHHANDLE` | `GFXDIAG_CRASHHANDLE` |
| Registry 备份文件 | `RenderDoc_RestoreGlobalHook.reg` | （未改） | `GfxDiag_RestoreGlobalHook.reg` |
| Symbol 缓存路径 | `\renderdoc\symbols` | （未改） | `\gfxdiag\symbols` |
| INI 节名 | `[renderdoc]` | （未改） | `[gfxdiag]` |
| BugReport zip 名 | `renderdoc_report_*.zip` | （未改） | `gfxdiag_report_*.zip` |
| 已安装 DisplayName | `"RenderDoc"` | （未改） | `"GfxDiag"` |
| Vulkan layer 描述 | `"... layer for RenderDoc"` | （未改） | `"... layer for GfxDiag"` |
| WGL 窗口类 | `renderdocGLclass` | （未改） | `gfxdiagGLclass` |
| PE 资源 InternalName | `renderdoc` | （未改） | `gfxdiag` |
| 8 个 vcxproj `<ProjectName>` | `renderdoc / cmd / shim / ui` | **`rendertest*`** | **未跟进改名 ← Bug** |

---

## 三、⚠ Fork 自身的不一致（仓库 Bug）

Commit 1 改了一遍 `renderdoc → rendertest`（含 vcxproj），Commit 2 把**代码里的字符串**继续推进到 `gfxdiag`，**但 vcxproj 的 `<ProjectName>` 留在 rendertest 阶段没动**。结果：

- 编译出来：`rendertest.dll`、`rendertestcmd.exe`、`qrendertest.exe`
- 代码硬编码寻找：`gfxdiag.dll`、`gfxdiagcmd.exe`、`qgfxdiag.exe`

→ 启动 Global Hook 时直接 `err 2`（文件不存在），表现为 UI 弹窗：

> `Couldn't start global hook. Internal error: Can't launch renderdoccmd from '...\x64\Development\gfxdiagcmd.exe' (err 2)`

### 临时救急方案

编译后把 `x64/Win32` × `Development/Release` 四个产物目录里 `rendertest*` / `qrendertest.exe` **复制**一份改名为对应的 `gfxdiag*` / `qgfxdiag.exe`（保留原文件以维持 PE Import 依赖）。

### 根治方案

把 4 个 vcxproj 的 `<ProjectName>` 改成与源码硬编码对齐：

| 文件 | 现在 | 应为 |
|---|---|---|
| `renderdoc/renderdoc.vcxproj` | `rendertest` | `gfxdiag` |
| `renderdoccmd/renderdoccmd.vcxproj` | `rendertestcmd` | `gfxdiagcmd` |
| `renderdocshim/renderdocshim.vcxproj` | `rendertestshim` | `gfxdiagshim` |
| `qrenderdoc/qrenderdoc_local.vcxproj` | `qrendertest` | `qgfxdiag` |

清理产物后重新构建。

---

## 四、文件清单

- **新增**：无（除本 `ENDFIELD_CHANGES.md`）
- **删除**：无
- **PE 资源 `.rc` 二进制改动**：`renderdoc.rc`、`renderdoccmd.rc`、`qrenderdoc.rc` 内嵌图标/版本信息字符串改名

---

## 五、Fork 独有 commit 清单（按时间逆序）

| Commit | 日期 | 主题 | 实际含义 |
|---|---|---|---|
| `fe6331dd74` | 2026-04-08 | avoid inject in platform.exe | 加 Wuwa 启动器子进程到黑名单 |
| `b0b8800a14` | 2026-04-08 | update | 第二轮品牌改名 `rendertest → gfxdiag`（漏改了 vcxproj） |
| `052d9a0a14` | 2026-04-06 | try to inject Wuwa | 第一轮改名 `renderdoc → rendertest` + win32_process.cpp 注入流程加固 |

---

## 六、概括

> **本 fork 在原生 RenderDoc 之上做了两件事：**
> **(1) 为鸣潮 Wuwa 加固了 Windows 注入流程，并把启动器子进程加入黑名单；**
> **(2) 把所有 `renderdoc` 字样改成 `gfxdiag` 以躲过基础字符串检测。**
> **除此之外没有任何功能增减；底层抓帧/回放能力与上游完全一致。**
