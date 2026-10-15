# TinecmaTools 快速开始指南

## 🚀 5分钟快速上手

### 前置要求

- ✅ Windows 10/11 操作系统
- ✅ RenderDoc 已安装
- ✅ Python 3.7 或更高版本
- ✅ PowerShell 5.0 或更高版本
- ✅ 管理员权限

---

## 方法一: 全自动一键运行 (推荐)

### 步骤 1: 准备环境

```powershell
# 检查 Python 版本
python --version

# 应显示: Python 3.x.x
```

### 步骤 2: 运行自动化工具

```powershell
# 进入工具目录
cd "C:\Program Files\renderdoc-1.42"

# 运行自动绕过工具
python tools\auto_bypass.py ^
  --renderdoc "C:\Program Files\RenderDoc" ^
  --game "C:\Games\YourGame\game.exe" ^
  --output "C:\Temp\TinecmaTools" ^
  --delay 10
```

### 步骤 3: 等待完成

工具将自动完成以下步骤:
1. ✅ 复制并重命名 RenderDoc 文件
2. ✅ 修改二进制文件中的特征字符串
3. ✅ 启动游戏进程
4. ✅ 等待 10 秒后注入 DLL
5. ✅ 启动 TinecmaTools UI

---

## 方法二: 手动分步执行

如果自动化工具失败,可以手动执行每个步骤:

### 步骤 1: 重命名文件

```powershell
# 运行重命名工具
.\tools\rename_tool.ps1 -NewName "TinecmaTools"

# 输出示例:
# [+] 重命名: renderdoc.dll -> TinecmaTools.dll
# [+] 重命名: renderdoc.exe -> TinecmaTools.exe
# [+] 重命名: qrenderdoc.exe -> TinecmaToolsUI.exe
```

### 步骤 2: 修改特征字符串

```powershell
# 修改 DLL 文件
python tools\string_replacer.py "C:\Program Files\RenderDoc\TinecmaTools.dll"

# 输出示例:
# [+] 在偏移 0x00012345 处替换: 'RenderDoc' -> 'TinecmaTls'
# [完成] 共替换 15 处
```

### 步骤 3: 启动游戏

```powershell
# 手动启动游戏
Start-Process "C:\Games\YourGame\game.exe"
```

### 步骤 4: 注入 DLL

```powershell
# 等待游戏完全启动后(约10秒)
# 运行注入工具
.\tools\inject_dll.ps1 ^
  -ProcessName "game" ^
  -DllPath "C:\Program Files\RenderDoc\TinecmaTools.dll" ^
  -Delay 0

# 输出示例:
# [+] 找到进程 PID: 12345
# [*] 开始注入 DLL...
# [+] DLL 注入成功!
```

### 步骤 5: 启动 UI

```powershell
# 启动 TinecmaTools UI
Start-Process "C:\Program Files\RenderDoc\TinecmaToolsUI.exe"
```

---

## 🎯 使用场景示例

### 场景 1: 单机游戏优化分析

```powershell
# 分析游戏渲染性能
python tools\auto_bypass.py ^
  --renderdoc "C:\Program Files\RenderDoc" ^
  --game "C:\Games\MyGame\game.exe" ^
  --delay 15
```

### 场景 2: 自己开发的游戏调试

```powershell
# 调试自己的 Unity/Unreal 项目
python tools\auto_bypass.py ^
  --renderdoc "C:\Program Files\RenderDoc" ^
  --game "D:\Projects\MyGame\Build\MyGame.exe" ^
  --output "D:\Debug\TinecmaTools" ^
  --delay 5
```

### 场景 3: 仅准备工具,不自动启动

```powershell
# 只准备文件,不启动游戏
.\tools\rename_tool.ps1 -NewName "TinecmaTools"
python tools\string_replacer.py renderdoc.dll

# 稍后手动使用 TinecmaTools
```

---

## 📋 参数说明

### auto_bypass.py 参数

| 参数 | 必需 | 默认值 | 说明 |
|------|------|--------|------|
| `--renderdoc` | ✅ | 无 | RenderDoc 安装路径 |
| `--game` | ✅ | 无 | 游戏可执行文件路径 |
| `--output` | ❌ | `C:\Temp\TinecmaTools` | 输出目录 |
| `--delay` | ❌ | `10` | 注入延迟秒数 |
| `--no-ui` | ❌ | `False` | 不启动 UI 界面 |

### rename_tool.ps1 参数

| 参数 | 必需 | 默认值 | 说明 |
|------|------|--------|------|
| `-RenderDocPath` | ❌ | `C:\Program Files\RenderDoc` | RenderDoc 路径 |
| `-NewName` | ❌ | `TinecmaTools` | 新的工具名称 |

### inject_dll.ps1 参数

| 参数 | 必需 | 默认值 | 说明 |
|------|------|--------|------|
| `-ProcessName` | ✅ | 无 | 目标进程名 |
| `-DllPath` | ✅ | 无 | DLL 完整路径 |
| `-Delay` | ❌ | `5` | 执行前延迟秒数 |

---

## ⚠️ 常见问题

### Q1: "权限不足" 错误

**解决方案**: 以管理员身份运行 PowerShell

```powershell
# 右键点击 PowerShell -> "以管理员身份运行"
```

### Q2: "找不到进程" 错误

**解决方案**: 
1. 确认游戏已经启动
2. 使用正确的进程名(不带 .exe)
3. 增加 `--delay` 参数值

### Q3: 注入成功但游戏崩溃

**解决方案**:
1. 确认 RenderDoc 版本与游戏兼容
2. 尝试增加注入延迟
3. 检查是否有其他反作弊软件冲突

### Q4: UI 无法连接到游戏进程

**解决方案**:
1. 确认 DLL 已成功注入
2. 在 UI 中手动刷新进程列表
3. 重新启动游戏和工具

---

## 🔧 验证工具是否生效

### 方法 1: 检查进程列表

```powershell
# 查看进程名是否已更改
Get-Process | Where-Object {$_.ProcessName -like "*Tinecma*"}

# 应显示: TinecmaTools, TinecmaToolsUI 等
```

### 方法 2: 查看文件名

```powershell
# 列出重命名后的文件
Get-ChildItem "C:\Temp\TinecmaTools"

# 应看到:
# TinecmaTools.dll
# TinecmaTools.exe
# TinecmaToolsUI.exe
# TinecmaToolsCmd.exe
```

### 方法 3: 检查游戏是否正常运行

- ✅ 游戏能正常启动
- ✅ 无反作弊警告
- ✅ TinecmaTools UI 能连接到游戏
- ✅ 能正常捕获帧数据

---

## 📞 获取帮助

如果遇到问题:

1. 查看详细文档: `bypass_anticheat_guide.md`
2. 查看故障排除: `README.md` 的故障排除章节
3. 检查更新日志: `CHANGELOG.md`

---

## ⚡ 高级技巧

### 技巧 1: 批量处理多个游戏

```powershell
# 创建批处理脚本
$games = @(
    "C:\Games\Game1\game1.exe",
    "C:\Games\Game2\game2.exe"
)

foreach ($game in $games) {
    python tools\auto_bypass.py --renderdoc "C:\Program Files\RenderDoc" --game $game
}
```

### 技巧 2: 自定义字符串替换

编辑 `tools\string_replacer.py` 中的 `replacements` 字典:

```python
replacements = {
    "RenderDoc": "YourName",
    "renderdoc": "yourname",
    "RENDERDOC": "YOURNAME",
}
```

### 技巧 3: 永久重命名 RenderDoc

如果不想每次都运行工具,可以直接在安装目录重命名:

```powershell
cd "C:\Program Files\RenderDoc"
.\tools\rename_tool.ps1 -NewName "TinecmaTools"
python tools\string_replacer.py renderdoc.dll
```

---

**祝您使用愉快!** 🎉
