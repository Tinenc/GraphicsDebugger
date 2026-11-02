# Naraka 抓帧启动器

《永劫无间》的 TinecmaTool 抓帧入口。**不往游戏安装目录写任何文件**——启动
`StartGame.exe`，`NarakaBladepoint.exe` 一出现就注入核心，靠导出表重定向拿到设备创建。

## 用法

```
Naraka-Launcher.bat      双击，打开图形控制中心（正常用法）
```

命令行等价：

```
run-naraka.bat           启动游戏，只观察它起来
run-naraka.bat inject    启动 + 首次出现即注入（抓帧用这个）
run-naraka.bat capture   写触发文件，等 .rdc 落盘
run-naraka.bat status    环境自检，什么都不启动
run-naraka.bat gui       打开图形界面
```

## 文件

| 文件 | 作用 |
|---|---|
| `Naraka-Launcher.bat` | 薄壳，只负责把控制权交给 GUI |
| `Naraka-Gui.ps1` | WinForms 控制中心（`-SmokeTest` 可无窗口自检） |
| `run-naraka.bat` | 命令行入口，GUI 调的也是它 |
| `wait-and-inject.ps1` | 100ms 紧轮询等渲染进程，出现即注入 |

## 两条硬约束（都是实测的，别改）

**不提权。** 从高完整性父进程拉起 `StartGame.exe`，`NeacClient` 会起来但
`NarakaBladepoint.exe` 永不出现、`Player.log` 不更新。脚本发现自己被提权时会把启动
交给 `explorer.exe`，退回中等完整性。GUI 里同一套逻辑。

**抢在设备创建之前注入。** 渲染进程约在 `StartGame` 之后 12 秒出现，设备在其后几秒
创建。错过就这一局没救——RenderDoc 只能包装它亲眼看到创建的那个设备。所以等待和注入
放在 `wait-and-inject.ps1` 里，用单进程 100ms 轮询；批处理的 `tasklist` + `ping` 光
发现进程就要 10 秒。

## 触发目录为什么还叫 proxy

`%LOCALAPPDATA%\Temp\Naraka\proxy\captures` 这个名字是历史遗留。**核心自己解析这个
路径**（`renderdoc/os/win32/win32_libentry.cpp` 的触发监视线程），改名会直接断掉，
所以保留。日志也在这里：`launcher.log`。

## 抓帧不出产物时

1. `run-naraka.bat status` —— 看桥接导出是否在、安装目录是否干净、进程是否活着
2. 看 `%LOCALAPPDATA%\Temp\TinecmaTool\RenderDoc_app_*.log` 里有没有
   `Export redirect`：没有就说明导出表没被改写，设备创建不会被拦到
3. 有 `created wrapped device` 但没有 `Graphics first present`，是交换链还没呈现
4. 触发文件只对**含触发监视线程的核心**生效（提交 `5c2c7436c` 之后）；老 DLL 需要
   重新编译 x64

## 编译前提

抓帧依赖 `x64\Development\TinecmaTool.dll`，改动核心后必须重编：

```
msbuild renderdoc.sln /m /p:Configuration=Development /p:Platform=x64
```

游戏运行时 DLL 被占用，链接会失败——先关游戏。这一条在本机没法自动化，
`MSBuild.exe` 被安全策略判为 Known Windows LOLBin。
