# Ninja Ripper 2.13 - Dual UV 导入修改记录

## 目标

修改 Ninja Ripper 2.13 的 3ds Max 导入工具，支持同时导入两套 UV。

## 背景

- NR 导入工具路径：`C:\Program Files (x86)\Ninja Ripper 2.13\importers\3dsmax\`
- 截帧游戏：`SpeedGame-Win64-Shipping.exe`（UE 引擎）
- 截帧数据路径：`c:\Users\Administrator\AppData\Roaming\Ninja Ripper\2026.03.19_00.56.36_SpeedGame-Win64-Shipping.exe_56044\frame_0\`

## NR 文件顶点属性布局

所有大型 mesh 文件布局一致（14 个属性）：

| NR索引 | SemIdx | Shader输入 | 类型 | 分量 | 用途 |
|---|---|---|---|---|---|
| [0] | 0 | _input0 | Float | 3 | Position |
| [1] | 1 | _input1 | Float | 4 | Normal |
| [2] | 2 | _input2 | Float | 4 | Tangent |
| **[3]** | **5** | **_input5** | **Float** | **4** | **UV1(xy) + UV2(zw)** |
| [4] | 7 | _input7 | Float | 2 | 非UV（负值） |
| [5] | 6 | _input6 | Float | 2 | 非UV（与[4]相同） |
| [6] | 8 | _input8 | Uint16 | 4 | Bone Indices |
| [7] | 13 | _input13 | Float | 4 | Vertex Color |
| [8] | 14 | _input14 | Uint8 | 4 | ... |
| [9] | 3 | _input3 | Uint8 | 4 | ... |
| [10] | 15 | _input15 | Float | 4 | ... |
| [11] | 4 | _input4 | Float | 4 | ... |
| [12] | 12 | _input12 | Float | 3 | ... |
| [13] | 11 | _input11 | Float | 3 | ... |

### 关键发现

1. **NR 属性索引按顶点缓冲区物理顺序编号**，不等于 Shader 的 `_inputN` 编号
2. `_input5`（SemIdx=5）实际在 NR 索引 **[3]**，不是 [5]
3. 两套 UV **打包在同一个 4 分量属性中**：UV1 = comp 0,1；UV2 = comp 2,3
4. [4] 和 [5] 虽然是 2 分量 Float，但数据为负值且相同，不是 UV

## 已修改的文件（共 4 个）

### 1. `import_nr\nrimp.py` — 数据模型

- `TexcoordLoadMode` 新增 `DualComp = 5`
- `TexCoords` 类新增 `u2AttrComp` / `v2AttrComp` 字段存储第二组 UV
- 更新 `TexcoordLoadModeToStr` 和 `TexCoords.__str__` 支持新模式

### 2. `import_nr\nrtools.py` — 逻辑层

- 新增 `_createTexCoordDualComp()` 函数，同时创建两组 `TexCoordAttrComp`（UV1 和 UV2）
- `createTexCoordList()` 中添加对 `DualComp` 模式的分发

### 3. `import_nr\nrqtgui.py` — GUI

- 新增 `TexcoordLoadDualUV` 控件类，提供 4 行设置：
  - UV1: U-Attr/Comp（默认 3/0）
  - UV1: V-Attr/Comp（默认 3/1）
  - UV2: U-Attr/Comp（默认 3/2）
  - UV2: V-Attr/Comp（默认 3/3）
- `PostVsImportGui` 和 `PreVsImportGui` 的 Texcoord 下拉框都新增第 6 项 **"Dual UV (Attr/Comp)"**
- 两个 `texcoordLoadComboChangeSlot` 都添加了 `elif 5 == idx` 分支

### 4. `import_nr\nr3dsimp.py` — Bug 修复

修复 pymxs 版 `Importer_pymxs._createTexCoords` 的 bug：

**原始代码问题**：对非默认 UV 通道（UV2、UV3...）只调用了 `setMapVert`，缺少：
- `meshop.setNumMapVerts` — 未分配 map 顶点缓冲区
- `meshop.setMapFace` — 未设置 map 面索引

**修复方案**：在 `setMapSupport` 之后，对每个 UV 通道都完整执行：
```
meshop.setNumMapVerts → meshop.setMapVert → meshop.setMapFace
```

> 注：MaxPlus 版本不受影响，它对每个通道都正确调用了 `SetNumTextureVertices` + `SetTextureVertex` + `SetTextureFace`

## 使用方式

1. **重启 3ds Max**（必须，Python 模块有内存缓存，不重启不生效）
2. 运行 `import_local.ms`
3. Texcoord Load 下拉选 **"Dual UV (Attr/Comp)"**
4. 默认值已预设好，直接点 Import
5. 导入后 Map Channel 1 = UV1，Map Channel 2 = UV2
6. 在 Unwrap UVW 修改器中切换 Map Channel 可查看不同 UV

## Dual UV 设置对照表

| 设置项 | Attr | Comp | 对应数据 |
|---|---|---|---|
| UV1: U-Attr/Comp | 3 | 0 | _input5.x |
| UV1: V-Attr/Comp | 3 | 1 | _input5.y |
| UV2: U-Attr/Comp | 3 | 2 | _input5.z |
| UV2: V-Attr/Comp | 3 | 3 | _input5.w |

如果其他游戏的 UV 在两个独立属性中（各 2 分量），改为：
- UV1: Attr=X, Comp=0/1
- UV2: Attr=Y, Comp=0/1
