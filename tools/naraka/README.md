# Naraka 抓帧启动器

《永劫无间》与《永劫无间手游》的 TinecmaTool 抓帧入口。**不往游戏安装目录写任何
文件**——启动游戏的 starter，渲染进程一出现就注入核心，靠导出表重定向拿到设备创建。

> 完整背景、实现细节、排查表见
> [`docs/NARAKA_抓帧指南_2026-11-02.md`](../../docs/NARAKA_抓帧指南_2026-11-02.md)。

## 两个目标

| | PC | 手游 |
|---|---|---|
| 目录 | `F:\Naraka\program` | `F:\NarakaMobile\game` |
| 启动器 | `StartGame.exe` | `StartGame_l22.exe` |
| 渲染进程 | `NarakaBladepoint.exe` | `NarakaBladepointMobile.exe` |

界面里在「1. 目标」切换；命令行用第二个参数，或直接把目标当第一个参数：

```
run-naraka.bat inject mobile     等价于  run-naraka.bat mobile
run-naraka.bat status mobile
```

两者共用同一套核心与同一条路线。手游也是 x64 / Unity，它的 `UnityPlayer.dll` 同样不
静态导入 `dxgi`/`d3d11`/`d3d12`（运行时按名解析），所以导出表重定向照样命中。

## 用法

```
Naraka-Launcher.bat      双击，打开图形控制中心（正常用法）
```

命令行等价：

```
run-naraka.bat           启动游戏，只观察它起来
run-naraka.bat inject    启动 + 首次出现即注入（抓帧用这个）
run-naraka.bat capture   写触发文件，等 .rdc 落盘
run-naraka.bat tool      只打开 TinecmaTool 分析器
run-naraka.bat status    环境自检，什么都不启动
run-naraka.bat gui       打开图形界面
```

启动游戏时会把分析器（`qTinecmaTool.exe`）一起拉起来，已经在跑就不重复开。

**抓帧按 F12**（RenderDoc 默认捕获键，不需要重编核心）。**先点一下游戏窗口再按**——内核会
比对 `GetForegroundWindow()`，不在前台直接忽略，这也是脚本发按键必须先
`SetForegroundWindow` 的原因。界面上的「抓帧」按钮写的是触发文件，那条路要核心里有监视线程
（`5c2c7436c` 之后编译的），当前 DLL 没有，所以按钮按了没反应属正常，按 F12 即可。

**手游当前发不起来**：启动器检测到版本落后，强制更新 62.8 GB（实测 ~686 KB/s）。
`StartGame_l22.exe` 不能单独启动，必须等启动器放行。详见
[`docs/NARAKA_抓帧指南_2026-11-02.md`](../../docs/NARAKA_抓帧指南_2026-11-02.md) §0。

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

**抢在设备创建之前注入。** 渲染进程约在 `StartGame` 之后 8–12 秒出现，设备在其后十几秒
创建（19:08 实测：进程 19:08:38 → `wrapped device` 19:08:54）。错过就这一局没救——
RenderDoc 只能包装它亲眼看到创建的那个设备。所以等待和注入放在 `wait-and-inject.ps1`
里，用单进程 100ms 轮询；批处理的 `tasklist` + `ping` 光发现进程就要 10 秒。

**分析器也必须经 explorer 启动。** 从提权进程直接 `Start-Process qTinecmaTool.exe`，
窗口会起来几秒后自己消失：日志停在驱动枚举完成那一行，事件日志里没有崩溃记录、退出码
也不是异常值。同一份 exe 经 explorer 以中等完整性启动就稳定。所以 `Start-Viewer` 在
检测到提权时会绕一趟 explorer，和启动游戏用同一个办法。

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
