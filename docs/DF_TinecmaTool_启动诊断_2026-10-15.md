# DF 启动后无 RenderDoc Overlay 诊断

> 历史启动诊断记录。最终正式客户端抓帧结果与修复说明见 [实现总结](DF_正式客户端截帧_实现总结_2026-10-15.md)；以下早期归因不代表最终结论。

日期：2026-10-15
工具：qTinecmaTool / RenderDoc 1.44 Development
启动入口：`C:\Program Files (x86)\WeGame\wegame.exe`

## 结论

RenderDoc GUI 已正常启动。实际故障是 `DeltaForceClient-Win64-Shipping.exe` 虽然加载了捕获模块并建立了控制连接，但图形 API 钩子未完成，因此界面保持 `API: None`，游戏内没有 Overlay，抓帧按钮不可用。

## 日志证据

日志文件：

`C:\Users\joyworkshop\AppData\Local\Temp\TinecmaTool\RenderDoc_2026.10.15_14.16.16.log`

1. WeGame 注入及控制握手成功（第 30–56 行）。
2. 32 位 WeGame 启动 64 位游戏时已正确切换到 x64 辅助程序（第 1954–1960 行），不是位数不匹配。
3. 捕获模块已加载到 `DeltaForceClient-Win64-Shipping.exe`，并建立子进程控制连接（第 2189–2207 行）。
4. 游戏进程只记录了 D3D11、D3D12、DXGI 等钩子注册，没有出现 `New D3D12 device`、`Adding ... frame capturer`、SwapChain 或 Present 被包装的成功记录。
5. ACE/SGuard 相关进程随后启动（第 2804、2828、2852 行）。
6. 游戏进程之后反复报告 `Failed to make IAT entry writeable`（从第 3030 行开始），说明函数入口修改没有完成。

## 启动链路

`WeGame 注入成功 → 子进程传播成功 → 游戏模块加载成功 → 图形函数入口挂钩失败 → API: None → 无 Overlay/无法抓帧`

## 判断

高概率原因是游戏进程的内存保护或安全组件阻止了 IAT 修改。日志可以确认“入口不可写”，但不能仅凭当前记录确定是 ACE 的哪个具体模块实施了阻止。

截图当前连接的是 `wegame.exe`，所以显示的是启动器状态；切换到游戏子进程连接页可以查看其独立状态，但按照日志，游戏进程也不会进入有效的图形捕获状态。

## 后续验证

- 使用自有、无反作弊的 UE/D3D12 测试程序验证同样的多级子进程启动链路。
- 确认测试程序中能出现 D3D12 device、SwapChain/Present 和 frame capturer 的成功日志。
- 保持 GUI、命令行辅助程序和捕获 DLL 使用同一版本、同一构建配置，避免 Development/Release 混用。
- 正式游戏环境仅使用厂商许可的调试或测试方案，不尝试规避反作弊保护。
