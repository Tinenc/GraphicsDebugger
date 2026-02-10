# TinecmaTools - 工具目录

本目录包含 TinecmaTools 的所有核心工具。

## 🐍 Python 工具

### auto_bypass.py ⭐ (推荐)
**自动化绕过工具 - 一键完成所有步骤**

```bash
python auto_bypass.py --renderdoc "C:\Program Files\RenderDoc" --game "C:\Games\YourGame\game.exe"
```

**功能**:
- ✅ 自动复制并重命名文件
- ✅ 修改二进制特征字符串
- ✅ 启动游戏进程
- ✅ 延迟注入 DLL
- ✅ 启动调试 UI

**参数**:
- `--renderdoc PATH` : RenderDoc 安装路径 (必需)
- `--game PATH` : 游戏可执行文件路径 (必需)
- `--output PATH` : 输出目录 (默认: C:\Temp\TinecmaTools)
- `--delay SECONDS` : 注入延迟秒数 (默认: 10)
- `--no-ui` : 不启动 UI 界面

---

### string_replacer.py
**二进制字符串替换工具**

```bash
python string_replacer.py renderdoc.dll
```

**功能**:
- ✅ 查找文件中的特征字符串
- ✅ 批量替换为 TinecmaTools
- ✅ 自动备份原文件

**特性**:
- 支持多种编码
- 保持文件长度不变
- 自动填充空字符

---

## 📜 PowerShell 工具

### rename_tool.ps1
**文件批量重命名工具**

```powershell
.\rename_tool.ps1 -NewName "TinecmaTools"
```

**功能**:
- ✅ 批量重命名 RenderDoc 文件
- ✅ 自动创建时间戳备份
- ✅ 支持自定义新名称

**参数**:
- `-RenderDocPath` : RenderDoc 路径 (默认: C:\Program Files\RenderDoc)
- `-NewName` : 新工具名称 (默认: TinecmaTools)

**重命名映射**:
```
renderdoc.dll     → TinecmaTools.dll
renderdoc.exe     → TinecmaTools.exe
qrenderdoc.exe    → TinecmaToolsUI.exe
renderdoccmd.exe  → TinecmaToolsCmd.exe
```

---

### inject_dll.ps1
**DLL 手动注入工具**

```powershell
.\inject_dll.ps1 -ProcessName "game" -DllPath "C:\Path\To\TinecmaTools.dll" -Delay 5
```

**功能**:
- ✅ 查找目标进程
- ✅ 分配远程内存
- ✅ 注入 DLL 文件
- ✅ 支持延迟注入

**参数**:
- `-ProcessName` : 目标进程名(不含.exe) (必需)
- `-DllPath` : DLL 完整路径 (必需)
- `-Delay` : 执行前延迟秒数 (默认: 5)

**注意事项**:
- 需要管理员权限
- 目标进程必须正在运行
- DLL 路径必须是绝对路径

---

## 🔧 C++ 源码

### hook_process_list.cpp
**进程枚举 API Hook 源码**

```bash
# 编译命令
cl /LD hook_process_list.cpp
```

**功能**:
- Hook CreateToolhelp32Snapshot
- Hook Process32First/Next
- 隐藏 TinecmaTools 进程

**使用方法**:
1. 编译为 DLL
2. 注入到目标进程
3. 调用导出函数安装 Hook

**注意**:
- 需要配合 Detours 或 MinHook 库
- 本文件仅为示例代码
- 需要根据实际情况调整

---

## 🚀 快速使用指南

### 场景 1: 首次使用 (自动化)

```powershell
# 最简单的方式
python auto_bypass.py --renderdoc "C:\Program Files\RenderDoc" --game "C:\Games\YourGame\game.exe"
```

### 场景 2: 手动控制每个步骤

```powershell
# 步骤 1: 重命名文件
.\rename_tool.ps1

# 步骤 2: 修改特征字符串
python string_replacer.py "C:\Program Files\RenderDoc\TinecmaTools.dll"

# 步骤 3: 启动游戏
Start-Process "C:\Games\YourGame\game.exe"

# 步骤 4: 等待10秒后注入
Start-Sleep -Seconds 10
.\inject_dll.ps1 -ProcessName "game" -DllPath "C:\Program Files\RenderDoc\TinecmaTools.dll"
```

### 场景 3: 仅准备工具,稍后使用

```powershell
# 只准备文件,不启动游戏
.\rename_tool.ps1
python string_replacer.py renderdoc.dll

# 稍后手动使用 TinecmaToolsUI.exe
```

---

## 📋 工具对比

| 工具 | 语言 | 自动化 | 难度 | 推荐度 |
|-----|------|--------|------|--------|
| auto_bypass.py | Python | ✅ 完全自动 | ⭐ 简单 | ⭐⭐⭐⭐⭐ |
| rename_tool.ps1 | PowerShell | ⚠️ 半自动 | ⭐⭐ 中等 | ⭐⭐⭐⭐ |
| inject_dll.ps1 | PowerShell | ❌ 手动 | ⭐⭐⭐ 较难 | ⭐⭐⭐ |
| string_replacer.py | Python | ❌ 手动 | ⭐⭐ 中等 | ⭐⭐⭐⭐ |
| hook_process_list.cpp | C++ | ❌ 需编译 | ⭐⭐⭐⭐⭐ 困难 | ⭐⭐ |

---

## ⚠️ 注意事项

1. **权限要求**: 所有工具都需要管理员权限运行
2. **路径格式**: 使用绝对路径,包含空格的路径要用引号
3. **备份机制**: rename_tool.ps1 和 string_replacer.py 会自动创建备份
4. **进程名称**: inject_dll.ps1 的进程名不包含 .exe 扩展名
5. **延迟设置**: 根据游戏启动速度调整 --delay 参数

---

## 🔧 故障排除

### 问题: Python 脚本无法运行
```powershell
# 检查 Python 版本
python --version

# 应显示 3.7 或更高版本
```

### 问题: PowerShell 脚本被阻止
```powershell
# 临时允许执行
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process

# 或在运行时指定
powershell -ExecutionPolicy Bypass -File script.ps1
```

### 问题: 找不到进程
```powershell
# 查看所有进程
Get-Process | Where-Object {$_.ProcessName -like "*game*"}

# 使用正确的进程名(不含 .exe)
```

---

## 📚 相关文档

- **完整文档**: 返回上级目录查看 `README.md`
- **快速开始**: 查看 `QUICK_START.md`
- **技术细节**: 查看 `bypass_anticheat_guide.md`
- **项目结构**: 查看 `PROJECT_STRUCTURE.md`

---

**工具版本**: v1.0  
**更新时间**: 2026-02-08  
**品牌名称**: TinecmaTools
