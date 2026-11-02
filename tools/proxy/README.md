# Naraka proxy layer（dxgi.dll / d3d11.dll / d3d12.dll）

> **状态（2026-11-02 实测）：不要往游戏目录部署。**
> 抓帧链路本身完全跑通 —— 设备被包装、`Graphics first present` 都出现了，
> 见 [`docs/NARAKA_代理层实测_2026-11-02.md`](../../docs/NARAKA_代理层实测_2026-11-02.md)。
> 但三个 DLL 躺在安装目录里被反作弊看见：进入游戏时弹出提示，随后游戏拆除图形设备退出。
> `Deploy-NarakaProxy.ps1` 因此默认拒绝部署，只有显式 `-ForceProtected` 才会装。
>
> **保留的是核心侧的 D3D12 factory 桥接**（`TINECMATOOL_CreateDXGIFactory*` 等），
> 那部分在 `TinecmaTool.dll` 内部，不需要动游戏目录。代理与测试留作该桥接的验证手段。

用**成为游戏的导入项**代替**改写游戏的导入表**。

## 为什么

Naraka 的每一条注入路线都堵死了，前三条是实测结论，见
[`docs/NARAKA_挂载方案_StartGame_2026-10-24.md`](../../docs/NARAKA_挂载方案_StartGame_2026-10-24.md)：

| 路线 | 结果 |
|---|---|
| 注入 `LauncherGame.exe` | x86 .NET 引导壳，1 秒内自行退出，子进程全丢 |
| 注入 `StartGame.exe` | 注入成功，但 Neac 起不来，游戏永不启动（§9，A/B 已证） |
| 事后 attach 游戏本体 | 设备已存在，无从包装 |
| 进程内改 IAT | VMProtect 解包时重写导入表，补丁在第一次调用前就失效（§7，`Graphics function resolved` 计数为 0） |

Windows 加载器优先搜索可执行文件所在目录，而：

- `UnityPlayer.dll` **静态导入** `dxgi.dll!CreateDXGIFactory`
- `D3D12\D3D12Core.dll`（Agility SDK）**静态导入** `dxgi.dll!CreateDXGIFactory2`
- `d3d11.dll` / `d3d12.dll` 由 Unity **运行时按名 LoadLibrary**，同样先命中 exe 目录

所以把同名 DLL 放进 `F:\Naraka\program\`，游戏会自己加载我们。**游戏镜像一个字节都没改**，加壳器没有东西可以还原。

## 结构

```
naraka_proxy_common.h   日志 / ini / 进程白名单 / PEB 断链 / 触发线程 / 通用转发
dxgi_proxy.cpp          20 个导出：拦截 CreateDXGIFactory{,1,2}，其余直通
d3d11_proxy.cpp         51 个导出：拦截 D3D11CreateDevice{,AndSwapChain}，其余直通
d3d12_proxy.cpp         19 个导出：拦截 D3D12CreateDevice，其余直通
gen_defs.py             从本机 System32 的 dumpbin 输出生成上面三个 .def
verify_exports.py       逐条比对代理与系统 DLL 的 (序号, 名字) 集合
build.cmd               手工设 INCLUDE/LIB 直接调 cl.exe（vcvars 与 MSBuild 在本机被策略拦住）
```

导出表**必须完整**：宿主进程按名字导入任何一个我们没导出的符号，加载器会直接让整个进程加载失败。三个 DLL 在本机分别有 20 / 51 / 19 个导出，`.def` 由 `gen_defs.py` 从真实二进制生成，不手抄。

内部实现函数统一叫 `NarakaImpl_*`，由 `.def` 别名成真实导出名。原因是 SDK 头文件已经用真实原型声明了 `CreateDXGIFactory` / `D3D11CreateDevice` 等名字，再用代理用的指针形状去定义同名函数会直接撞车（C2375 / C2733）。

## 双代理为什么必须成对

`WrappedIDXGIFactory::CreateSwapChain` 会校验传进来的设备是不是 `WrappedID3D12Device`。只换 DXGI 不换 D3D12 时，游戏传的是微软原生设备，工厂直接拒挂：

```
dxgi_wrapped.cpp - Error - Creating swap chain with non-hooked device!
```

表现是游戏左上角不出现 HUD、F12 无反应。`d3d12.dll` 的作用就是把 `D3D12CreateDevice` 导向 `TINECMATOOL_D3D12CreateDevice`，让握手两侧都是包装对象。

## 与核心的契约

代理通过 `TINECMATOOL_*` 桥接导出进入核心：

| 导出 | 由谁实现 |
|---|---|
| `TINECMATOOL_CreateDXGIFactory` / `1` / `2` | `renderdoc/driver/dxgi/dxgi_hooks.cpp` |
| `TINECMATOOL_D3D11CreateDevice` / `...AndSwapChain` | `renderdoc/driver/d3d11/d3d11_hooks.cpp` |
| `TINECMATOOL_D3D12CreateDevice` | `renderdoc/driver/d3d12/d3d12_hooks.cpp` |

代理加载核心前会做两件事：

1. **偷看导出表**（`LoadLibraryEx` + `DONT_RESOLVE_DLL_REFERENCES`，不跑 DllMain）。没有桥接就干脆**不加载**核心——一个没有桥接的核心只能转发，却会在受保护进程里驻留并安装自己的钩子，纯属增加风险。
2. **设 `TCM_SKIP_GRAPHICS_IAT=1`**，让核心不要注册自己的图形 IAT 钩子。否则代理和核心会从两个方向钩同一批入口：宿主 `GetProcAddress` 拿到核心的钩子 → 钩子去找"真实函数"→ 从 System32 再加载一份 `d3d11.dll` → 触发新一轮挂钩扫描 → 再次解析同一个函数。2026-11-02 实测就是一个 `Graphics function resolved` 无限刷屏，设备永远建不出来。

> 核心侧用 `GetEnvironmentVariableA` 读这个变量，**不能用 `getenv`**：CRT 在进程启动时对环境做快照，紧接着 `SetEnvironmentVariableA` 写进去的值对 `getenv` 不可见。

## 构建

```cmd
cd /d "C:\Program Files\GraphicsDebugger-Naraka\tools\proxy"
build.cmd
python verify_exports.py
```

`build.cmd` 故意不调 `vcvars64.bat`（它内部调 `reg.exe`，本机被拦），也不调 `MSBuild.exe`（被判为 Known Windows LOLBin）。直接手工设 `INCLUDE`/`LIB` 调 `cl.exe`。

> 三个代理 DLL **不入库**：`.def` 是按本机 System32 的真实导出表生成的，换一台 Windows 版本就不一样了，提交二进制反而会误导。必须在本机构建一次。

核心本身仍需 MSBuild 重编，且必须由人在 x64 Native Tools 提示符下执行：

```cmd
msbuild renderdoc.sln /m /p:Configuration=Development /p:Platform=x64
```

## 部署与抓帧

> 这一节记录的是已跑通但**被反作弊识别**的部署方式，默认不可执行。
> 完整实测结论见 [`docs/NARAKA_代理层实测_2026-11-02.md`](../../docs/NARAKA_代理层实测_2026-11-02.md)。
>
> 部署脚本 `Deploy-NarakaProxy.ps1` 已于 2026-11-02 随代理路线一起删除。下面是它原本
> 的用法，仅作记录；要重新启用请从 git 历史取回该脚本（提交 `585e096c6` 之前）。

```powershell
cd "C:\Program Files\GraphicsDebugger-Naraka\tools\naraka"

# 默认会拒绝部署，并在拒绝时说明原因
.\Deploy-NarakaProxy.ps1

# 确认要承担提示与封禁风险才继续
.\Deploy-NarakaProxy.ps1 -ForceProtected -Apply

# 完全还原
.\Deploy-NarakaProxy.ps1 -Undeploy -Apply
```

另外两条实操结论（与代理无关，但会让人误判成代理有问题）：

- **必须用普通权限启动游戏**。从提权会话 `Start-Process` 拉起 `StartGame.exe`，
  `NeacClient` 会起但 `NarakaBladepoint.exe` 永不出现、`Player.log` 不更新；
  经 `explorer.exe`（中等完整性）拉起则正常进游戏。
- 游戏内录制（`CCRecord`）会与我们的 DXGI 钩子抢交换链，抓帧前先关掉。

然后**正常从启动器启动游戏**，进到 3D 画面后按 F12，或：

```powershell
New-Item "$env:LOCALAPPDATA\Temp\Naraka\proxy\captures\trigger_capture_narakabladepoint"
```

产物在 `%LOCALAPPDATA%\Temp\Naraka\proxy\captures`，代理的完整日志在
`%LOCALAPPDATA%\Temp\Naraka\proxy\naraka_proxy.log`。

**不要手工改写 `tempdx12.txt` / `ExtraVars.unityargs`**。部署脚本不碰它们，改动会被 `StartGame` 判为 forged。

## 配置

`<游戏 program 目录>\naraka_proxy.ini`，或退到 `%LOCALAPPDATA%\Temp\Naraka\proxy\naraka_proxy.ini`：

```ini
[naraka]
mode=renderdoc          ; renderdoc | pix | both | off
target_process=narakabladepoint.exe
rdoc_dll=<TinecmaTool.dll 绝对路径>   ; 留空则依次尝试 exe 目录、%ProgramFiles%\GraphicsDebugger-Naraka\x64\Development
capture_dir=<抓帧输出目录>
log_dir=<日志目录>
```

`mode=off` 只转发、不加载核心。这是代理透明性测试用的模式，也是"怀疑代理影响性能"时的对照。

## 测试

```cmd
cd /d "...\artifacts\naraka-fixture"
build-fixture.cmd
python run-proxy-test.py
```

五个用例：

| 用例 | 断言 |
|---|---|
| `control` | 不部署代理时 `dxgi.dll` 解析到 System32（反向对照，证明检测本身有效） |
| `passthrough` | 代理在场且 `mode=off`，设备仍能创建、帧仍能呈现 |
| `gate-off` | 代理在场但不匹配 `target_process`，行为等同 passthrough |
| `proxy-d3d11` | 桥接被调用、`.rdc` 落盘 |
| `proxy-d3d12` | 同上，额外覆盖 factory/device 握手 |

后两个用例在核心未重编时标记为 `SKIP`（不是 PASS），并断言代理确实拒绝了旧核心。

`thumb` 只校验「工具说写了缩略图 + 输出文件大小合理」，**不看退出码**：这个 fork 的
`TinecmaToolcmd` 连 `version` / `--help` 都返回非零（经管道看是 127，经 CreateProcess 看是
`0xC0000409`），拿它当成功判据等于拿一个常数当判据。缩略图内容本身仍然是可以核对的——
D3D11 夹具抓到的就是它清屏时那个 `(0.1, 0.2, 0.3)` 深藏青。

## 已知边界

- 代理只对 `narakabladepoint.exe` 生效；`StartGame.exe` 虽同目录，但只走直通、不加载核心。
- 游戏内录制（`CCRecord`）会与我们的 DXGI 钩子抢交换链，抓帧前先关掉游戏内录制。
- PEB 断链只摘除核心模块的链表登记，不动句柄、线程或内存保护——再往后就不是"研究"了。
- 不碰 ACE/Neac 服务、不脱壳、不改签名、不伪造 `tempdx12.txt`。越界即停，按
  `docs/DF_AUTHORIZED_CAPTURE_RESEARCH.md` 记 `status: "unsupported-protected"`。
