# Trusted asset providers

`src/symbols/providers.py` 是“固定程序负责可重复解析、AI 只处理不能确定的推导”中的
固定程序执行边界。它已通过专用 capability 对象接入 `scanner.py`、`service.py` 和 MCP
符号解析流程：只有用户事先登记并锁定哈希的 provider，才有资格生成可进入程序验证流程的
候选数据；普通资产 JSON 不能进入这条信任通道。

## 信任边界

- 生产注册表固定在当前用户配置区，不放进游戏资产目录。Windows 默认位置是
  `%LOCALAPPDATA%\RenderDocUltra\trusted_providers.v1.json`；Linux/macOS 使用
  `$XDG_CONFIG_HOME/renderdoc-ultra/` 或 `~/.config/renderdoc-ultra/`。
- 调用方只用 `providerId` 选择程序。可执行文件、参数模板、支持的输出 schema、build
  范围和超时全部来自本地注册表。现有 MCP 接口不接受任意 `executable`、`args`、
  `env` 或输出路径。
- 每次启动前和结束后都会重新计算 provider 可执行文件以及固定文件的 SHA-256；运行期间
  注册表改变也会使结果失败。
- `shell=False`，工作目录和输出目录是注册表目录下新建的独立临时目录。子进程只能收到
  少量系统启动变量、该临时目录的 `TEMP/TMP` 和固定的 Python UTF-8 变量；调用进程中的
  token、代理、用户 HOME、PATH 等变量不会继承。
- stdout、stderr、manifest 和读取到内存的 artifact 都有硬上限。超时或 stdout/stderr
  超限会终止 provider；非零退出码不会产生受信结果。
- manifest 的 JSON 语法和 v1 顶层 envelope 必须严格有效：
  `renderdoc-ultra-symbols/v1`、整数 `schemaVersion: 1`。重复 JSON key、NaN/Infinity、
  未知顶层字段、非法 Shader hash、build/provider 版本不一致都会失败。当前核心还会递归检查
  路径和保留的信任字段，但不会在执行层宣称已经完整验证每个嵌套反射/CBuffer 字段的
  schema；那部分由专用 scanner ingest 按 ABI 规则继续验证。
- `blobPath`、`containerPath` 和 artifact 路径必须是临时输出目录内的安全相对路径；禁止
  绝对路径、`..`、空路径分量、Windows drive/ADS、symlink/junction 逃逸。逻辑资产路径也
  不允许绝对路径或 `..`。
- provider JSON 不能写 `trusted`、`verified`、`hashVerified`、`programVerified`、
  `attestation` 或任何 `_` 开头的内部字段。这些字段即使值为 `true` 也会被拒绝，而不是
  被降级为普通提示。

这是一条身份、完整性和执行策略边界，不是 OS 沙箱。登记的程序仍以当前用户权限运行，
因此只能登记经过审查的本地二进制。超时和完成清理使用 Windows kill-on-close Job Object
（不可用时退回系统 `taskkill /T`）或 POSIX 独立进程组，尽力避免 provider 遗留子进程；
Windows 在 `Popen` 后挂接 Job Object，存在极短的 child-escape 竞态，这不等于权限隔离或
OS 沙箱。若未来需要运行不可信第三方 extractor，应另加低权限进程、Windows AppContainer
或容器隔离。

## 注册表 v1

```json
{
  "schema": "renderdoc-ultra-provider-registry/v1",
  "schemaVersion": 1,
  "providers": [
    {
      "providerId": "unity-2022.3-dxbc",
      "version": "1.4.0",
      "executable": "D:\\RenderDocUltra\\providers\\unity-symbols.exe",
      "sha256": "64位小写十六进制SHA-256",
      "argsTemplate": [
        "extract",
        "--asset-root",
        "{assetRoot}",
        "--manifest",
        "{manifestPath}",
        "--build",
        "{buildId}"
      ],
      "supportedSchemas": ["renderdoc-ultra-symbols/v1"],
      "buildPatterns": ["2022.3.*-win64-d3d11"],
      "timeoutSeconds": 90,
      "fixedFileHashes": {}
    }
  ]
}
```

字段规则：

- `providerId` 是唯一、稳定的 ASCII 标识；不能通过资产 manifest 临时新增 provider。
- `version` 是被审查的 provider 版本，并会写入服务端证明。provider 输出如声明版本，必须
  与这里完全相同。
- `executable` 必须是绝对、普通、非 symlink 文件；`sha256` 每次执行都重算。
- 禁止 `cmd`、PowerShell、`sh`/`bash` 等 shell 以及 `.bat`/`.cmd`/`.ps1` 入口。
  Python/Node 等解释器禁止 `-c`、`-m`、`-e`/`--eval` 等 inline/module 模式（包括
  `-cCODE`、`-mmodule` 紧凑形式），并且必须先出现已写入 `fixedFileHashes` 的绝对脚本文件，
  所有可替换 placeholder 只能位于该脚本之后。
- `argsTemplate` 只能使用 `{assetRoot}`、`{outputDir}`、`{manifestPath}`、`{buildId}`。
  替换后仍作为 argv 数组直接交给进程，不经过 shell。
- `supportedSchemas` 在 v1 只能包含 `renderdoc-ultra-symbols/v1`。
- `buildPatterns` 使用区分大小写的 glob。没有任何 pattern 命中时禁止执行。
- `timeoutSeconds` 必须是有限数值，范围为 0.05–300 秒；NaN/Infinity 会 fail closed。
- `fixedFileHashes` 用于解释器脚本、`.jar`、托管程序集、规则数据库等固定代码或配置。
  键为绝对路径、值为 SHA-256。参数模板中任何已经存在的绝对普通文件都必须出现在这里，
  防止只锁定 `python.exe`/`dotnet.exe` 而遗漏真正运行的 provider 文件。

注册表本身是用户侧信任根。修改 provider 或升级版本时，应由安装/管理工具在用户确认后
重新计算哈希并原子更新注册表；不要让 AI、资产扫描结果或游戏目录内脚本直接写该文件。

## Provider 输出 v1

provider 必须把唯一入口 manifest 写到传入的 `{manifestPath}`。当前固定文件名为
`symbols.v1.json`。最小输出如下：

```json
{
  "schema": "renderdoc-ultra-symbols/v1",
  "schemaVersion": 1,
  "buildId": "2022.3.51f1-win64-d3d11",
  "provider": {
    "providerId": "unity-2022.3-dxbc",
    "version": "1.4.0"
  },
  "shaders": [
    {
      "path": "Assets/Shaders/Hero.shader",
      "name": "Hero/Forward",
      "stage": "pixel",
      "entryPoint": "main",
      "blobPath": "blobs/7f....dxbc",
      "rawSha256": "blob文件实际字节的SHA-256"
    }
  ]
}
```

如有 `blobPath`/`containerPath`，provider 必须把文件复制到本次输出目录，runner 会重新读取、
计算哈希并和 `rawSha256` 比较。通过验证的文件以有界 `ProviderArtifact.data` 留在内存中；
临时目录在 `run_provider` 返回前清理，因此后续代码不能保存或重新打开 provider 给出的路径。

顶层可选字段限于 `engine`、`platform`、`api`、`generatedAt`、`metadata`、`artifacts`。
`artifacts` 项只允许 `path`、`sha256`、`size`。当前执行核心对 Shader 记录强制检查对象类型、
稳定 hash、路径安全和禁止自报信任；反射、CBuffer、资源与 variant 的嵌套字段暂时作为数据
保留，不在这里做完整 schema/ABI 验证。每条 Shader 至少要有合法的 `rawSha256` 或
`codeSha256`，后续 scanner 仍必须独立验证 DXBC/DXIL 字节和 ABI。

## Service-owned attestation

`run_provider()` 成功返回 `TrustedProviderResult`，而不是把 manifest 标记后重新写成 JSON。
结果包括：

- 规范化 manifest 字节及其 SHA-256；
- 已验证 artifact 的路径、大小、SHA-256 和内存字节；
- provider ID/version/binary hash；
- registry hash、规范化资产根 hash、build ID、run ID、耗时和固定 provenance；
- 由当前服务进程随机密钥生成的 HMAC。

`verify_attestation(result)` 同时复核 manifest、artifact 集和 HMAC。密钥不会持久化，证明只在
生成它的服务进程内有效；把 attestation 字段复制进资产 JSON、数据库或下一次进程都不能获得
信任。这正是“资产不能自报 trusted”的能力边界。

主要 API：

```python
registry = load_registry()  # 可用于只读检查默认用户配置
result = run_provider(
    "unity-2022.3-dxbc",
    asset_root,
    build_id="2022.3.51f1-win64-d3d11",
)
assert verify_attestation(result)
manifest = result.manifest  # 每次返回新的副本
```

`run_provider` 没有 registry path/object、executable、args 或 env 覆盖参数，并且始终重新读取
默认用户配置位置。`load_registry(path)` 的显式路径只供管理工具和隔离测试解析/检查配置，不能
把返回对象传给 `run_provider` 执行。这样 MCP 即使能控制普通函数参数，也只能选择已有 ID。

输出验证现在是 runner 的私有 tokenized helper，不在 `__all__` 中，也不能由 scanner 或 MCP
直接调用。scanner 的唯一接缝是先对 `TrustedProviderResult` 调用 `verify_attestation()`。

## scanner/service/MCP 接入

已实现的接入顺序如下：

1. `SymbolService.scan(..., provider_id=...)` 从默认用户注册表运行 provider，MCP 只传已有 ID；
2. 服务得到仍在同一进程中的 `TrustedProviderResult`，调用
   `ingest_trusted_provider_result()`；入口第一项操作就是 `verify_attestation`，伪造对象在创建
   DB 或解析资产路径之前失败；
3. ingest 复核 attestation 绑定的规范化资产根 hash 和 build ID，禁止把 A 目录的结果用于 B；
4. 每条 Shader 必须实际引用内存中的 `ProviderArtifact`，artifact 必须独立解析为恰好一个完整
   DXBC/DXIL 容器，raw/code hash 必须与重新计算结果一致；
5. ingest 独立重算 interface/layout hash，并要求完整的 IO、资源、CB binding 和逐字段
   offset/type/stride/row-major/bit-field 结构。只有全部检查通过才写
   `_symbolMetadataTrusted=true` 和 `trusted-provider-artifact` provenance；
6. 缺 blob、容器损坏、hash 不符、接口不完整或布局缺字段的记录仍可进入候选数据库，但固定为
   `trusted-provider-provisional`，matcher 不会把它当作物理程序证据；
7. provider ingest 会在相同 `scanId` 上使用普通资产重扫的同一 fail-closed scan lock，并在该锁
   内完成重扫、provider rows 写入和 prune，避免两个扫描互相删除记录；
8. scan summary 及 SQLite `asset_scans.summary_json` 保存 provider ID/version/binary hash、
   registry/manifest/artifact hash、run ID、provenance、可信/临时记录数量和验证错误。进程 HMAC
   不持久化为跨进程权限；
9. matcher 只有对 `trusted-provider-artifact` 才承认物理 asset hash。真正生成 CB/字段 alias 时
   还会与 capture 的反射 layout 逐项比较，attestation 不替代 capture 侧 ABI 验证；
10. AI 只能查看 provisional 候选和证据、提出映射建议。AI 不能登记 provider、更新哈希、调用
    私有输出验证 helper 伪造来源，或把自己的建议写成 program-verified。

MCP 使用示例：

```text
scan_asset_symbols(
  asset_root="D:\\UnpackedGame",
  build_id="2022.3.51f1-win64-d3d11",
  provider_id="unity-2022.3-dxbc"
)

resolve_capture_symbols(
  asset_root="D:\\UnpackedGame",
  build_id="2022.3.51f1-win64-d3d11",
  provider_id="unity-2022.3-dxbc"
)
```

`scan_asset_symbols`、`match_capture_assets` 和 `resolve_capture_symbols` 都没有 registry、
executable、argv、command 或 env 参数。`match_capture_assets` 只在 `rescan=true` 时运行所选
provider（或没有提供可复用 `scan_id`、因此必须扫描时）；`rescan=false` 且复用现有
`scan_id` 时不会偷偷重跑程序。

这样可以把确定性链路固定为：

`用户注册固定程序 → 双重哈希校验 → 受限执行 → 输出 envelope 校验 → 进程内证明 → artifact/ABI 重验 → capture ABI 对照`

只有这条链路无法唯一确定的资产关系，才交给 AI 生成“待确认候选”。
