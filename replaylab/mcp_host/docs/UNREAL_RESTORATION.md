# 在公版 Unreal Engine 中还原捕获效果

Bread 复用已有捕获导出、Shader 整理和 CBuffer 证据。UE 负责生成可编辑材质、关卡和独立渲染证据。批处理可以将同一抓帧的多个静态 Draw 组合为一个关卡。骨骼、特殊顶点输入、引擎内部资源和几何外的 Dispatch 需要相应的输入和实现。

本文是工具与回执说明。效果还原、阶段划分和交付要求以现有的[UE 还原规范](../../../.agents/skills/renderdoc-resource-export/references/unreal-restoration.md)为准；这里不另设 AI 行为规范。

## 按目标工程选路线

选择用户指定的 .uproject。Bread 用它关联的本地引擎启动，不要求升级到某个 UE5 小版本。先用 `inspect_unreal_project` 看安装和可用提供方，再用 `probe_unreal_rendering` 查询该编辑器实际暴露的材质、灯光、捕获接口与项目渲染设置。探测结果用来选实现，最终能力还要由实际编译和出图确认。UE4 暂不纳入此工作流。

- 捕获公式可移植时，使用可编辑的材质节点和材质函数。只移植几项时，可把这些项接入原生 Lit 或实际可用的 Toon 材质。计算完整辐亮度时，用 Unlit 直接输出。
- 材质还需要 UE 原生受光和投影时，按实际可用模板和光照路径选择 Default Lit 或 Toon。用下文的诊断变体测量受光差异，再选接近原效果的方案。无需为使用 Toon 改掉现有工程的渲染配置。
- 光源类型、混合位置和中间图格式都有版本及渲染路径差异。接口缺失时更换路线并保留具体缺口，避免由版本号推断成功。比如前向渲染时可改用 Unlit 诊断材质观察法线，探测结果不会提供延迟 GBuffer 读回或 Lumen 作为可选路线。 [Lumen 技术限制](https://dev.epicgames.com/documentation/unreal-engine/lumen-technical-details-in-unreal-engine)

## 材质输出

| 捕获算法的输出 | UE 接口 | 对照方式 |
| --- | --- | --- |
| 反照率、法线、粗糙度、金属度 | Default Lit 或 Toon BSDF 的对应输入 | 单独比 Base Color、法线等材质属性 |
| 脸部 SDF、Ramp 取样、遮罩、自定义高光 | 材质函数，保留可编辑输入、接线和注释 | 从材质 Pass 单独输出，比较中间值 |
| 已算完的表面辐亮度 | Unlit 的 Emissive Color 或 Substrate Unlit BSDF | 比较线性场景颜色，另校对曝光、Bloom 和色调映射 |
| 叠加发光或边缘光 | Lit/Toon 的 Emissive 输入 | 检查加法、曝光和 Lumen 的发光贡献 |
| 依赖场景颜色、深度和遮罩的屏幕效果 | Post Process Material，可配 Custom Depth、Stencil 和 User Scene Textures | 在合成前后分别对照 |

安装版本若暴露 Toon BSDF，还需检查对应 GBuffer、项目配置与实际编译结果；节点存在不代表当前工程可用。把已计算的光照接到 Base Color 会让它再次参与引擎光照。直接输出到 Unlit 则需要另行验证接收 GI、遮挡和发光对环境的影响。具体模板通过 `probe_unreal_rendering` 的实际能力选择，不由固定小版本推断。

### 保留漫反射，隔离原生高光

Lit 或 Toon 材质可显式选择 `nativeSpecular: matte-diffuse`。生成器将 `Metallic=0`、`Specular=0`、`Roughness=1` 作为有注释的材质输入。它保留该着色模型的漫反射路径。Base Color 仍提供反照率，已经乘过灯光的辐亮度应单独合成。粗糙度主要改变高光宽度，金属度为零仍有电介质反射，所以要单独控制 Specular。目标版本若提供灯光的 Specular Scale，也可对选定灯设为零做诊断。斜视角、环境反射、Clear Coat 等额外响应需用线性图检查残差。

希望保留 UE 阴影和间接漫反射时，可用这套 Lit 底材质，捕获高光和边光经各自的材质函数叠加到 Emissive。需要受阴影控制的叠加项要显式使用对应遮挡因子，并检查 Lumen 是否把该发光再贡献给环境。若原效果要求完整控制每盏灯的公式，可以用 Unlit 将漫反射、高光、边光、环境项在 Shader 中合成，再接项目 RT 提供的遮挡。 [Epic 材质输入](https://dev.epicgames.com/documentation/unreal-engine/material-inputs-in-unreal-engine) · [UE 着色模型](https://dev.epicgames.com/documentation/unreal-engine/shading-models-in-unreal-engine)

## 阴影、GI、描边和后处理

- **多光源。** `lights` 可创建方向光、点光、聚光和矩形光。先比主光，再逐盏加入填充光、轮廓光，分别匹配位置/方向、颜色、单位、衰减范围、软阴影尺寸和高光响应。原版自定义每盏灯的公式时，把光参数显式接到材质函数，用同一像素的逐灯输出验证，再复核合成。
- **阴影和 GI。** 分开明暗交界、脸部美术阴影、投影与间接光。原生路线使用 UE 的阴影、GI 和带场景捕获或立方贴图的 Sky Light，校准直射/间接光比例、暗部色偏和软硬程度。原版已知的环境常量或 SH 系数可以直接进入材质函数。Lumen 或屏幕空间 GI 的替代效果从背光区和接触区域验证，另测视角变化。Toon Profile 可控制间接漫反射强度和间接高光 Ramp。原公式需要逐像素遮挡时，用光源视角深度 RT，由材质函数做投影和深度比较。按原效果填 Bias、遮挡物列表和矩阵。
- **诊断图的 GI。** SceneCapture 有独立的后处理与光追设置。Bread 为诊断相机设置当前关卡的 GI、反射方法和可用的项目光追选项，并在回执记录。`scene-color` 是 Raw SceneColor，可能遗漏原生 GI；`final-hdr` 保留引擎后处理管线，但仍需对齐曝光等设置并实测目标区域的 GI 响应。它不自动等价于主视图。重复捕获或预热不能充当连续运行帧。

- **外扩描边。** 原版是反向外壳时，单独建立反面网格或反向剔除组件，使用平滑法线和 World Position Offset 材质函数，比较轮廓、粗细和随距离变化。屏幕描边可用深度、法线和 Custom Stencil 构造 Post Process Material。批处理可生成后处理材质，反向外壳的几何、剔除和 WPO 图需要在工程里配置。
- **后处理与 AA。** 先对齐前级线性结果，再调曝光、Bloom、色调曲线和 AA。原版时域方案与 UE TSR/TAA 的差异用相同镜头运动验收，检查轮廓稳定性、拖影和细节。重复调用 SceneCapture 不等于推进真实游戏时间。
- **插入绘制和 RT。** 批处理支持保存到关卡的 SceneCapture 与可供材质直接采样的 RT，也支持用于观察的独立材质绘制链。多级后处理可用 User Scene Textures。要加 Compute 或精确控制注入点时，可在项目插件中使用 Scene View Extension、Global Shader 和 RDG。选取已公开的 hook 及资源，逐灯 VSM 和 Lumen 内部数据通常不属于普通 Surface 材质输入。

[SceneCapture API](https://dev.epicgames.com/documentation/unreal-engine/API/Runtime/Engine/USceneCaptureComponent2D) · [多 Pass 后处理](https://dev.epicgames.com/documentation/unreal-engine/post-process-material-user-scene-textures-in-unreal-engine) · [Global Shader 插件](https://dev.epicgames.com/documentation/unreal-engine/creating-a-new-global-shader-as-a-plugin-in-unreal-engine)

要对照 Lumen 或 TSR 自己的内部结果时，可以另外用 UE 自带的 DumpGPU 导出 RDG 纹理、缓冲和 Pass 参数。使用 r.DumpGPU.Root 限定需要的阶段，减少导出量。这些文件用于诊断，材质仍通过项目明确传入的 RT 采样。 [Epic GPUDump 文档](https://dev.epicgames.com/documentation/unreal-engine/gpudump-viewer-tool-in-unreal-engine)

## 工作流

1. 对所需 EID 完成 `gltf` 资源导出并验证，核对目标 Shader、贴图、CBuffer 和输入布局。按证据填写 UE 厘米制位置、相机、光向、纹理色域和采样器。不同的资源视图和 Mip 链记录为缺口。
2. 用 `inspect_unreal_project` 检查目标 .uproject，再用 `probe_unreal_rendering` 观察当前编辑器能力。用 `get_unreal_restoration_recipe_schema` 读取材质模板、接线、分层、灯光和 Pass 的结构。`operator` 是原生节点入口；`code` 是底层 Custom 接口，不代表可把完整 Shader 放入 Custom，其适用范围仍按现有还原规范判断。用材质实例保存参数。
3. 用 `import_resource_export_to_unreal` 导入，轮询 `get_unreal_integration_job`。通过 `additionalMeshes` 为其他 Draw 指定导出操作、材质和变换，纹理通过可选的 `operationId` 从同一组导出中取值。每个导出的 EID 和包哈希写入回执。变体里的材质替换和参数调节作用于主网格，灯光和摄像机调节作用于整个关卡。批处理使用安装版 UnrealEditor-Cmd.exe，在目标项目关闭时执行。结果保存后可直接打开 Restoration.umap。网格、灯光、相机、PostProcessVolume 和实时 SceneCapture 都是关卡对象。
4. 用 `start_unreal_project_connection` 设置连接，`interactive=true` 可在公版 Python 提供方启动常驻、项目专属的编辑器连接。`call_unreal_project_tool` 先发现实际 toolset/schema，再进行材质、函数、实例和蓝图编辑。后续任务及 Bread 重启可重连，不需每次退出 UE。不会接管或关闭无关 Editor。批量导入、能力探测和 `render_unreal_restoration` 仍是需要目标工程关闭的独立后台路线，不能将其与常驻连接混用。
5. 编辑后可调用 `preflight_unreal_materials` 检查选定材质/实例，或 `include_level=true` 纳入关卡网格及后处理材质。该操作复用常驻连接；目标工程关闭时使用后台 Editor。它会触发编译，不保存资产。轮询任务后检查回执内 `result.passed`；未通过时任务状态为 `material-preflight-blocked`。材质函数通过实际消费它的材质检查，不能用空函数预览代替。批量 RT 捕获也内置同一前置检查，在分配诊断 RT 前阻止失败材质出图。
6. 分项比对并交付。要替换未被手工编辑的旧版生成资产，用 `replace_job_id` 指定上一次结果，继续使用同一个导入目录。旧文件保存在 Saved/Bread。如果已手工编辑资产，可以重新渲染当前关卡，自动替换会保护手工改动。

结构与 Unity 交付对应。Content 放关卡使用的资源，Saved 放日志和验证图，原始证据仍由 Bread 保存。

    Content/BreadImports/R_<operation>_<recipe>/
      Maps/Restoration.umap
      Meshes/SM_CapturedDraw.uasset
      Textures/
        RenderTargets/RT_*.uasset
      Materials/M_*.uasset
      Materials/Functions/MF_*.uasset
      Materials/Instances/MI_*.uasset
    Saved/Bread/Restorations/<job>/
      recipe.json
      receipt.json
      editor.log
      *.png / *.exr / *.rgba32f

## 分项验收

新任务在 `layers` 逐项声明捕获依据、所用材质/函数、移植或替代路线、观察 Pass 和要调的参数，并为基线指定最终外观检查。已有任务可继续使用单项比较。相机角度、灯光强度、诊断材质或参数变化写在 `variants`，在临时视图中采样，保存的正式关卡不继承诊断改动。每个变体拥有自己的 RT 依赖链。主光单独开启、只移除填充光、只切换单盏灯的 Specular Scale、暂时只输出高光、改变视角检查边光，都能用变体表达。

`review_unreal_restoration_layers` 按执行回执逐项验证当前资产、观察和参考图，给出每层缺失、错误、偏差与调节位置。单项可用 `compare_unreal_restoration`。对齐 Capture/EID、摄像机、分辨率、Pass 含义、编码、显示变换和时域状态。变体比对还需要相同的变体 ID。参考来自同一捕获；如果某个中间量由别的 Draw 或 Pass 生产，在该层的 sourceEvents 中给观察项标注对应 EID。资产或渲染配置改变后，重新观察当前关卡。

| 组件 | 比较模式 | 验收 |
| --- | --- | --- |
| 公式、纹理通道、法线、遮罩和关键中间计算 | exact | 同编码浮点图逐像素、逐通道比较，按源格式和公式约定容差 |
| 原生阴影、GI、曝光、AA 或管线替代 | replacement | 记录替代原因和差异，先比可观测的线性 RT，再比最终画面的局部误差、轮廓和对比度。最后验证多视角和动态表现 |
| 缺失输入或无法访问的原版 Pass | 缺口 | 明确缺失证据及其影响范围 |

先让线性受光层匹配，再调色、Bloom、曝光和 AA。最终外观通过也要保留中间层的差异说明。动态验证需补相机、灯光或角色运动，批处理静帧只能验证记录的状态。

最终画面用 PNG，线性 RT 使用 RGBA32F 或无压缩单层扫描线 EXR，保留数值。指定 FP16 RT 时，FP32 文件也不会提高 RT 的原有精度。SceneDepth 默认 UE 单位，需要对齐编码才可与原 Device Depth 比较。普通 SceneCapture 的 GBuffer 与原渲染器内部照明 Pass 有不同语义。

### 区域 HDR 接口

`get_unreal_validation_schema` 返回数值比对选项和连续帧清单的实际 schema。`compare_unreal_restoration.numeric_options` 与分层引用的 `numericOptions` 使用同一结构，例如：

```json
{
  "regions": [{
    "id": "Face",
    "bounds": [800, 200, 1200, 700],
    "mask": {"path": "C:/Evidence/face.png", "sha256": "<64位SHA256>", "channel": "R", "origin": "top-left"},
    "limits": {"maxAbsoluteError": 0.01, "p95AbsoluteError": 0.002, "p99AbsoluteError": 0.005}
  }],
  "tileSize": 64,
  "worstCount": 8
}
```

上述阈值仅示范字段，不是默认效果标准。`bounds` 是左闭右开的完整图像像素坐标；可单独使用矩形、覆盖掩码或两者交集。掩码必须为同分辨率的 8 位 L/RGB/RGBA PNG，校验哈希，不缩放、不自动翻转；空覆盖报错。全局 `limits` 可被各区域覆盖。

每个区域报告覆盖数、逐通道均值/最大值、P95/P99、超差比例、超差边界、最差像素及最差分块。像素尾差取各通道绝对差的最大值，分位数用 type-7 线性插值。HDR 不裁剪到0–1，不转为显示 PNG。NaN/Inf 单独计数并使受影响区域失败。指定区域时 `withinTolerance` 按所有区域判断，`wholeFrameWithinTolerance` 和整图统计只作并列诊断；未指定区域的旧调用保留整图行为。数值通过仍不等于视觉、动态或依赖审查通过。

### 连续帧接口

`review_unreal_hdr_sequence` 只读取已经导出的文件，不启动 UE、不修改关卡。输入是有 SHA256 的 `bread-unreal-hdr-sequence/v1` JSON 清单，完整字段由 schema 工具提供：

- 工程、分辨率、数值编码、Capture/EID、视图、Pass 含义及显示变换。
- `capture` 中的生产方式、同一次运行的 `runId`、预热帧数、世界时间是否推进、是否逐帧重置历史及空间/时域采样数。
- `mode` 为 `steady` 或 `motion`；`frames` 每项记录实际 `frame`、`timeSeconds`、相对文件路径和 SHA256。

工具先检查所有文件存在、哈希、格式和尺寸，再计算统计。帧号断裂/重复、时间间隔不符、重复引用同一路径、数量不足、逐帧清历史或重复截图等会阻止连续稳态验收。输出相邻帧差、稳态逐像素全序列最大最小范围，以及首尾差；均支持同一套区域、分位数和尾差限制。中途闪烁即使首尾相同也会保留。`motion` 输出运动范围，不能作为静止画面稳定性通过。

清单还可记录工程相对路径的 `savedAssetHashes` 和 `renderConfigHashes`，用于检查当前资产、配置是否与采集时一致；未提供时对应 `assetStateVerified` / `renderConfigStateVerified` 为 false，不将旧图冒充当前资产证据。

`required_frames` 默认8，可显式选择；阈值由调用者根据既有验收要求提供。多次空间/时域采样平均不能认证单帧稳定性。清单的运行方式与时间戳由生产者声明，工具验证文件及元数据一致性，**不独立证明它们确实来自连续游戏帧**。真实采集仍须保留生产端证据；这里不会把一个完成回调升级为采集成功。当前统计支持至多256帧、每帧四百万像素，支持 FP32 原始文件和无压缩扫描线 EXR。

### 编译前置检查的证据边界

检查使用实际消费材质的同步资源统计，并读取本轮新产生的编译/默认材质回退日志。缺少 API、无有效像素 Shader 资源、日志不可读或明确编译失败均阻止捕获。回执只覆盖当前 Editor Shader 平台和消费实例，不能据此宣布其他平台或最终外观通过。新版采集图绑定前置报告哈希；旧回执仍可数值比对，但标记 `materialPreflightStatus: legacy-unverified`，不会补写成已通过编译检查。

同一批次基线按父材质去重重编译；后续灯光、相机、动态参数变体继续检查资源和新增日志，不重复主动重编译全部父材质。当前固定前置检查使用 Bread Python 提供方；仅有原生 MCP 会话而没有项目 Python 通道时会报告能力缺口，不接管或关闭该会话。

## MCP 选择

`inspect_unreal_project` 检查安装中实际存在的提供方。具备所需 Epic MCP 插件时使用其发现接口；缺少时使用公版 Python Editor 插件。Python 提供方既支持单次后台任务，也支持 `interactive=true` 的常驻连接。任务已授予本地脚本执行时，`BreadEditorTools.run_python` 可在目标 Editor 主线程处理材质图、蓝图及项目资源；保存与编译/读回分别有明确结果。不会因完成一条命令自动退出常驻 Editor。

早期调研的第三方候选包括 [chongdashu/unreal-mcp](https://github.com/chongdashu/unreal-mcp)、[db-lyon/ue-mcp](https://github.com/db-lyon/ue-mcp) 和 [Monolith](https://github.com/tumourlove/monolith)。这些链接不代表已安装或验收；当前内置实现使用实际安装的 Epic 接口或 Bread Python 连接。 [Epic MCP 文档](https://dev.epicgames.com/documentation/unreal-engine/unreal-mcp-in-unreal-editor)
