# -*- coding: utf-8 -*-
"""
Build 极品UV分析.xlsx summarizing the UV sampling path of texture _65 (res65)
at EventID 403 of 兰博-金属风暴.rdc, combined with the car-body UV documentation.

Data was gathered via the RenderDoc MCP (get_draw_info / get_bindings /
get_shader ps+vs disasm) and the earlier UV export/compare workflow.

Run: py build_uv_xlsx.py
"""

import os
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

OUT = r"C:\Users\joyworkshop\Desktop\极品UV分析.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="305496")
HEAD_FONT = Font(bold=True, color="FFFFFF")
TITLE_FONT = Font(bold=True, size=13, color="1F3864")
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def style_header(ws, row, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = WRAP
        cell.border = BORDER


def put_table(ws, start_row, headers, rows, widths=None):
    for j, h in enumerate(headers, 1):
        ws.cell(row=start_row, column=j, value=h)
    style_header(ws, start_row, len(headers))
    r = start_row + 1
    for row in rows:
        for j, val in enumerate(row, 1):
            cell = ws.cell(row=r, column=j, value=val)
            cell.alignment = WRAP
            cell.border = BORDER
        r += 1
    if widths:
        for j, w in enumerate(widths, 1):
            ws.column_dimensions[chr(64 + j)].width = w
    return r


wb = Workbook()

# ============ Sheet 1: 概览 ============
ws = wb.active
ws.title = "概览"
ws["A1"] = "金属风暴 EventID 403 · 贴图 _65 UV 采样路径分析"
ws["A1"].font = TITLE_FONT
info = [
    ("抓帧文件", r"E:\GST\白金猎豹\兰博-金属风暴.rdc"),
    ("API", "Vulkan (SPIR-V, ANGLE 生成)"),
    ("EventID", "403"),
    ("Draw", "Indexed|Instanced, numIndices=16497, numInstances=1（车体同材质的一个部件，非 45678 车体主体）"),
    ("渲染目标", "4×MRT: R11G11B10F(albedo) / R16F / R8G8 / R32F（G-Buffer）"),
    ("VS Shader", "ResourceId::31808090"),
    ("PS Shader", "ResourceId::31808091（与 45678 车体 draw 427 同一 PS）"),
    ("目标贴图 _65", "反射名 res65；绑定 DescriptorSet=2, Binding=6；类型 SampledImage<float, 2D>"),
    ("_65 采样次数", "3（均为 ImageSampleExplicitLod）"),
    ("采样 UV 来源", "PS 输入 _81.xy = 顶点属性 _input5.xy（= FBX map1 / 主 UV）"),
    ("_65 作用", "UV 间接图/掩码：RG 通道当作二级 UV 去采样细节图集 _66；B/A 通道作为混合掩码"),
]
r = 3
for k, v in info:
    ws.cell(row=r, column=1, value=k).font = Font(bold=True)
    ws.cell(row=r, column=1).alignment = TOP
    c = ws.cell(row=r, column=2, value=v)
    c.alignment = WRAP
    r += 1
ws.column_dimensions["A"].width = 18
ws.column_dimensions["B"].width = 90

# ============ Sheet 2: _65 UV 采样路径 ============
ws2 = wb.create_sheet("_65_UV采样路径")
ws2["A1"] = "_65 (res65, Set2/Binding6) 的三处采样"
ws2["A1"].font = TITLE_FONT
headers = ["采样点", "UV 来源(PS输入)", "顶点属性", "UV 表达式", "LOD", "采样指令", "结果去向 / 用途"]
rows = [
    ["#1  (_302→_303)", "_81.xy", "_input5.xy", "_81.xy * 0.5 + (0.5, 0.0)", "-1 (explicit)",
     "ImageSampleExplicitLod", "R,G(_303.xy)→除以 _106._child1[7].xy 后作为 UV 采样细节图集 _66；B(_303.z)→掩码 _356/_362"],
    ["#2  (_365→_366)", "_81.xy", "_input5.xy", "_81.xy * 0.5", "-1 (explicit)",
     "ImageSampleExplicitLod", "R,G(_366.xy)→作为 UV 采样细节图集 _66；B(_366.z)→掩码 _383/_389"],
    ["#3  (_496→_497)", "_81.xy", "_input5.xy", "_81.xy （原样，无缩放/偏移）", "0 (explicit)",
     "ImageSampleExplicitLod", "A(_497.w=_500)→基础色分层混合掩码 _503；(1-A)=_508 参与后续混合"],
]
r = put_table(ws2, 3, headers, rows, widths=[16, 14, 12, 30, 14, 22, 60])
ws2.cell(row=r + 1, column=1,
         value="小结：_65 是一张“UV/掩码指示图”。三次采样都用主 UV _input5.xy（两次做 0.5 缩放的象限查找 + 一次原样），"
               "取回的 RG 当作二级 UV 去 _66 图集取细节，B/A 当作分层混合掩码。UV 本身不带平铺，落在 [0,1]。")
ws2.cell(row=r + 1, column=1).alignment = WRAP
ws2.merge_cells(start_row=r + 1, start_column=1, end_row=r + 1, end_column=7)

# ============ Sheet 3: UV 输入路由 (VS→PS) ============
ws3 = wb.create_sheet("UV输入路由(VS→PS)")
ws3["A1"] = "顶点属性 → VS 输出 → PS 输入 的 UV 路由（EventID 403）"
ws3["A1"].font = TITLE_FONT
headers3 = ["顶点属性 (VS Location)", "内容/格式", "VS 输出 (Location)", "PS 输入变量", "PS 中用途"]
rows3 = [
    ["_input0 (Loc0)", "位置 SNorm16x4", "参与 morph/skinning，变换后写 _38(Loc0)", "_79 (Loc0)", "世界位置；DPdx/DPdy 求屏幕导数"],
    ["_input2 (Loc2)", "法线/TBN SNorm8x4", "经 TBN 变换写 _38(Loc0)", "_79 (Loc0)", "世界法线"],
    ["_input3 (Loc3)", "索引 uint4", "×3 作为 _53 morph/骨骼缓冲索引", "—", "顶点动画/变形（不作 UV）"],
    ["_input5 (Loc5)", "UV pack float4", "_40(Loc2) = (_input5.x, _input5.y, 位置派生.x, 位置派生.y)", "_81 (Loc2)",
     "★ 主 UV：_81.xy=_input5.xy 采样 _63/_65/_67 等；_81.zw 为位置派生坐标（非 _input5.zw）"],
    ["_input6 (Loc6)", "UV float2", "_41(Loc3) = (_input6.x, _input6.y, 位置z派生, 0)", "_82 (Loc3)",
     "_82.xy=_input6 用于 _64 区域合成、_106 视差偏移"],
    ["_input13 (Loc13)", "顶点色 float4", "_39(Loc1) = _input13.zyxw", "_80 (Loc1)", "顶点色/材质参数"],
]
r3 = put_table(ws3, 3, headers3, rows3, widths=[20, 18, 46, 14, 52])
ws3.cell(row=r3 + 1, column=1,
         value="注意：本 PS 只消费了 _input5.xy 与 _input6；_input5.zw（FBX map2）在 EventID 403 的这套着色器里未被使用。"
               "另外 _81.zw 是 VS 用位置算出的坐标，并不是 _input5.zw。")
ws3.cell(row=r3 + 1, column=1).alignment = WRAP
ws3.merge_cells(start_row=r3 + 1, start_column=1, end_row=r3 + 1, end_column=5)

# ============ Sheet 4: 文档信息(UV通道对照) ============
ws4 = wb.create_sheet("文档信息(UV通道对照)")
ws4["A1"] = "结合车体导出/对比文档的 UV 通道对照"
ws4["A1"].font = TITLE_FONT
headers4 = ["FBX UV Map", "顶点来源", "UV 范围 (U / V)", "本 PS(403) 是否使用", "说明"]
rows4 = [
    ["map1 (UV)", "_input5.xy", "U[0.00, 0.98]  V[0.28, 1.00]", "是（_63/_65/_67 等）", "主图集 UV；_65 采样即用它"],
    ["map2 (UV2)", "_input5.zw", "U[0.00, 1.00]  V[0.08, 1.00]", "否", "第二套 UV（AO/细节），本 draw 未用"],
    ["map3 (UV3)", "_input6", "U[-0.78, 1.74]  V[-0.14, 1.79]", "是（_64/_106 视差）", "带平铺的细节/区域 UV"],
]
r4 = put_table(ws4, 3, headers4, rows4, widths=[14, 14, 28, 22, 40])

notes = [
    "",
    "关联结论（见 UV_对比报告.md / 车体导出与UV对比_流程.md）：",
    "· 车体网格共 3 套 UV：_input5.xy、_input5.zw、_input6；FBX 导出为 map1/map2/map3。",
    "· 原皮/金属风暴/赛博超域 三皮肤的 3 套 UV 逐顶点完全一致（max|ΔUV|=0），差异只在贴图/材质。",
    "· 车体主体 draw：原皮=EventID416、金属风暴=427、赛博超域=636（唯一顶点9291/索引45678）。",
    "· 本表分析的 EventID 403 是金属风暴中同材质(PS 31808091)的另一部件(索引16497)，顶点布局/ UV 语义与车体一致。",
    "",
    "数据来源：RenderDoc MCP —— open_capture / get_draw_info(403) / get_bindings(403) / get_shader(ps,vs disasm)。",
]
rr = r4 + 1
for line in notes:
    cell = ws4.cell(row=rr, column=1, value=line)
    cell.alignment = WRAP
    if line.startswith("关联结论") or line.startswith("数据来源"):
        cell.font = Font(bold=True)
    ws4.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=5)
    rr += 1

os.makedirs(os.path.dirname(OUT), exist_ok=True)
wb.save(OUT)
print("Saved:", OUT)
