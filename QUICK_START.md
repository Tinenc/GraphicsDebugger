# TinecmaTool 快速上手指南

> 🚀 **5 分钟快速掌握 D3D12 游戏与《使命召唤》图形抓帧全流程**

---

## 📋 环境前置要求

- ✅ **操作系统**：Windows 10 / Windows 11 (64-bit)
- ✅ **权限**：管理员权限（用于反作弊服务管理与 DLL 代理部署）
- ✅ **运行库**：DirectX 12 兼容显卡与最新图形驱动

---

## 🎮 方法一：图形控制中心一键运行（强烈推荐）

项目已将所有复杂的代理配置、反作弊免杀、游戏拉起与抓帧触发整合为可视化图形控制中心。

### 步骤 1：打开启动向导
双击运行：
```text
C:\Program Files\GraphicsDebugger-Steam-Cod\tools\TinecmaTool启动向导.bat
```
*(或在命令行以管理员身份执行 `tools\launch_tinecmatool.bat`)*

### 步骤 2：选择目标游戏与抓帧模式
控制面板将自动检测系统中的游戏安装路径：
- **现代战争 2019 (战网: MW2019 / ModernWarfare.exe)**：
  - 自动定位 `D:\Call of Duty Modern Warfare`；
  - 自动备份过时的 Win7 运行时为 `d3d12_win7.dll.bak`；
  - 自动部署免杀双代理。
- **Call of Duty HQ (Steam: cod.exe / sp26-cod.exe)**：
  - 自动定位 Steam 游戏库；
  - 自动部署双代理至根目录及 `sp26\` 子目录。
- **自定义 D3D12 游戏**：
  - 点击“浏览”选择任意 64 位 D3D12 游戏的主程序目录。

抓帧后端请勾选 **`(*) TinecmaTool (RenderDoc v1.44) [推荐]`**。

### 步骤 3：部署并拉起游戏
1. 保持勾选 **`[x] 启动后保持控制面板开启 (推荐)`**；
2. 点击 **`🚀 部署代理并启动游戏`**；
3. 游戏启动后，左上角将呈现稳定的调试 HUD（如 `Capturing D3D12...`）。

### 步骤 4：触发抓帧与实时预览
进入游戏 3D 渲染场景（如主菜单展示、对局或战役画面）后：
1. **立即抓帧**：在控制面板点击 **`📸 立即抓帧 (F12)`**（或在游戏窗口按 F12）；
2. **预览画面**：点击 **`🖼️ 预览最新截帧缩略图`**，秒级确认画面是否抓取完整；
3. **深入调试**：点击 **`🔍 在 qTinecmaTool 中打开`**，即刻载入生成的 `.rdc` 截帧文件，查看所有 DrawCall、管线状态及 HLSL 着色器。

---

## 🛠️ 方法二：纯双代理手动部署（无面板模式）

如果你希望将代理 DLL 部署到游戏后，直接通过战网客户端或 Steam 客户端原生启动：

### 步骤 1：编译代理 DLL（如尚未编译）
```cmd
cd /d "C:\Program Files\GraphicsDebugger-Steam-Cod\tools\proxy"
build.bat
```
生成 `dxgi.dll` (25KB) 与 `d3d12.dll` (16KB)。

### 步骤 2：复制到游戏目录
将以下文件复制到游戏主可执行程序所在目录（如 `D:\Call of Duty Modern Warfare\`）：
1. `tools\proxy\dxgi.dll` -> `游戏目录\dxgi.dll`
2. `tools\proxy\d3d12.dll` -> `游戏目录\d3d12.dll`
*(如果是 MW2019，请先将游戏自带的 1.9MB `d3d12.dll` 重命名为 `d3d12_win7.dll.bak`)*

### 步骤 3：配置代理模式
在游戏目录下创建或编辑 `dxgi_proxy.ini`：
```ini
mode=renderdoc
```

### 步骤 4：启动游戏并抓帧
1. 正常通过战网或 Steam 启动游戏；
2. 游戏加载后会自动加载 TinecmaTool 调试器核心并抹除 PEB 链表；
3. 按 F12 触发抓帧，捕获文件将自动保存至桌面 `COD_RDC_*.rdc`。

---

## ⚠️ 避坑提醒

1. **绝对不要在黑屏阶段抓帧**：
   游戏刚启动黑屏（Frame 0）以及着色器预编译期间，管线未就绪，严禁盲发抓帧信号，否则反作弊线程看门狗会因 GPU 停顿判定崩溃（`0x00001337`）。
2. **必须使用双代理**：
   单独放置 `dxgi.dll` 会导致 `Creating swap chain with non-hooked device` 报错。必须同时放置 `dxgi.dll` 和 `d3d12.dll`。
3. **遇到反作弊冲突**：
   在控制面板点击 **`🧹 一键清理反作弊/环境`**，自动停止残留服务。
