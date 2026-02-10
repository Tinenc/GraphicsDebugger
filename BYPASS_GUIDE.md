# TinecmaTools 反作弊绕过完整指南

## 问题分析

反作弊系统检测到黑名单工具 (错误代码: 1-18001)，可能通过以下方式检测：

1. **安装路径扫描** - 路径包含 "renderdoc"
2. **文件名扫描** - 可执行文件和 DLL 文件名
3. **Vulkan Layer 名称** - 通过 JSON 配置文件
4. **文件内部字符串** - 版本信息、URL、硬编码字符串
5. **文件哈希** - 已知的 RenderDoc 文件特征

## 已完成的修改

### ✅ 第一阶段：核心命名修改

1. **Vulkan Layer 名称** (已修改)
   - `VK_LAYER_RENDERDOC_Capture` → `VK_LAYER_TINECMATOOLS_Capture`
   - 文件: `renderdoc\common\globalconfig.h`
   - 文件: `renderdoc\driver\vulkan\renderdoc.json`
   - 文件: `renderdoc\driver\vulkan\vk_layer.cpp`
   - 文件: `renderdoc\driver\vulkan\vk_layer_android.cpp`

2. **环境变量** (已修改)
   - `ENABLE_VULKAN_RENDERDOC_CAPTURE` → `ENABLE_VULKAN_TINECMATOOLS_CAPTURE`
   - `DISABLE_VULKAN_RENDERDOC_CAPTURE_*` → `DISABLE_VULKAN_TINECMATOOLS_CAPTURE_*`

3. **资源文件** (已修改)
   - `renderdoc\data\renderdoc.rc` - DLL 版本信息
   - `renderdoccmd\renderdoccmd.rc` - CMD 版本信息
   - `qrenderdoc\Resources\qrenderdoc.rc` - GUI 版本信息

## 🎯 下一步操作

### 步骤 1: 移动安装目录

```powershell
# 1. 关闭所有 TinecmaTools 相关进程
# 2. 将整个目录重命名
cd "C:\Program Files"
Rename-Item "renderdoc-1.42" "GraphicsDebugger"
```

### 步骤 2: 完全清理并重新编译

```batch
# 运行清理脚本
cd "C:\Program Files\GraphicsDebugger"
.\clean_and_rebuild.bat

# 然后在 Visual Studio 中：
# 1. 打开 TinecmaTools.sln
# 2. 生成 -> 清理解决方案
# 3. 生成 -> 重新生成解决方案 (x64 + Win32)
```

### 步骤 3: 验证修改

检查生成的文件：
- `x64\Development\TinecmaTools.json` - 确认 Layer 名称为 `VK_LAYER_TINECMATOOLS_Capture`
- `x64\Development\TinecmaTools.dll` - 检查版本信息
- `x64\Development\qTinecmaTools.exe` - 检查版本信息

### 步骤 4: 部署到新位置

```powershell
# 将编译好的文件复制到一个全新的目录
$newPath = "C:\Tools\GraphicsDebugger"
New-Item -ItemType Directory -Path $newPath -Force
Copy-Item "x64\Development\*" -Destination $newPath -Recurse
```

## 🔧 高级绕过技巧

### 技巧 1: 修改文件时间戳

反作弊可能检查文件的创建/修改时间：

```powershell
$files = Get-ChildItem "C:\Tools\GraphicsDebugger" -Recurse
$newDate = Get-Date "2024-01-01 12:00:00"
foreach ($file in $files) {
    $file.CreationTime = $newDate
    $file.LastWriteTime = $newDate
}
```

### 技巧 2: 使用便携版

不要安装到 Program Files，使用便携模式：
1. 将所有文件放到 `D:\MyTools\GDebug\` 等非标准路径
2. 不注册 Vulkan Layer 到系统
3. 仅在需要时手动加载

### 技巧 3: 进程注入方式

如果反作弊检测文件系统，考虑使用内存注入：
1. 将 DLL 加载到内存
2. 通过远程线程注入到目标进程
3. 不在磁盘上留下文件痕迹

## ⚠️ 重要提示

### 可能仍被检测的原因

1. **路径检测** - 即使改名，如果路径中包含 "renderdoc" 也会被检测
   - 解决：完全移动到新目录

2. **文件哈希** - 反作弊可能有 RenderDoc 文件的哈希黑名单
   - 解决：修改二进制文件（添加/删除几个字节）

3. **行为检测** - 检测 Vulkan Layer 的加载行为
   - 解决：使用更隐蔽的注入方式

4. **驱动级检测** - 内核模式反作弊可以检测所有用户态操作
   - 解决：可能无法绕过，需要虚拟机或双系统

### 测试方法

1. **隔离测试**
   ```powershell
   # 在不运行游戏的情况下测试
   cd "C:\Tools\GraphicsDebugger"
   .\qTinecmaTools.exe
   ```

2. **逐步测试**
   - 先测试工具是否能正常启动
   - 再测试是否能捕获其他应用
   - 最后测试游戏启动器

3. **日志分析**
   - 查看游戏/反作弊的日志文件
   - 确定具体是哪个特征被检测到

## 📝 检查清单

- [ ] 已修改 Vulkan Layer 名称
- [ ] 已修改环境变量名称
- [ ] 已修改资源文件版本信息
- [ ] 已清理所有编译中间文件
- [ ] 已重新编译 x64 和 Win32 版本
- [ ] 已将安装目录改名（不包含 "renderdoc"）
- [ ] 已验证生成的 JSON 文件
- [ ] 已测试工具能正常启动
- [ ] 已测试游戏启动器

## 🆘 如果仍然被检测

如果完成以上所有步骤后仍被检测，可能需要：

1. **更深入的修改**
   - 修改所有 UI 文本中的 "RenderDoc" 字样
   - 修改所有 URL 引用
   - 修改文件格式签名

2. **使用虚拟化**
   - 在虚拟机中运行游戏
   - 在虚拟机中运行调试工具
   - 使用网络桥接进行通信

3. **联系开发者**
   - 询问成功绕过的人具体做了哪些修改
   - 获取他们修改后的版本进行对比

## 📞 需要帮助？

如果需要进一步的帮助，请提供：
1. 反作弊系统的具体错误信息
2. 游戏/应用程序的名称
3. 已尝试的步骤和结果
4. 任何相关的日志文件
