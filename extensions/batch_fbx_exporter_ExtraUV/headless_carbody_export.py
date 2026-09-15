# -*- coding: utf-8 -*-
"""
Headless car-body FBX exporter for RenderDoc / TinecmaTool.

Run with:
    qTinecmaTool.exe --python headless_carbody_export.py

It opens each capture listed in CONFIG headlessly (via the `renderdoc`
module), locates the given car-body draw call, exports a multi-UV FBX plus
the bound pixel-shader textures, and dumps per-vertex UV data to JSON for
later comparison. Calls sys.exit(0) at the end so the qrenderdoc main UI is
never shown.

The mesh/FBX/matrix helper functions are copied verbatim from the
batch_fbx_exporter_ExtraUV extension so we do not pull in its PySide2 / Qt
dialog dependencies.
"""

from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import os
import sys
import json
import struct
import inspect
from textwrap import dedent
from collections import defaultdict

import renderdoc as rd


# ==================== CONFIG ====================

OUTPUT_ROOT = r"E:\GST\白金猎豹\carbody_export"

CONFIG = [
    {"skin": "原皮",     "rdc": r"E:\GST\白金猎豹\兰博-原皮.rdc",     "eventId": 416},
    {"skin": "金属风暴", "rdc": r"E:\GST\白金猎豹\兰博-金属风暴.rdc", "eventId": 427},
    {"skin": "赛博超域", "rdc": r"E:\GST\白金猎豹\兰博-赛博超域.rdc", "eventId": 636},
]

# Apply a world/view transform matrix found automatically in the VS CBs.
APPLY_MATRIX = True
TRANSPOSE_MATRIX = True
EXPORT_TEXTURES = True


# ==================== FBX ASCII template ====================

FBX_ASCII_TEMPLATE = """
    ; FBX 7.3.0 project file
    ; ----------------------------------------------------

    ; Object definitions
    ;------------------------------------------------------------------

    Definitions:  {

        ObjectType: "Geometry" {
            Count: 1
            PropertyTemplate: "FbxMesh" {
                Properties70:  {
                    P: "Primary Visibility", "bool", "", "",1
                }
            }
        }

        ObjectType: "Model" {
            Count: 1
            PropertyTemplate: "FbxNode" {
                Properties70:  {
                    P: "Visibility", "Visibility", "", "A",1
                }
            }
        }
    }

    ; Object properties
    ;------------------------------------------------------------------

    Objects:  {
        Geometry: 2035541511296, "Geometry::", "Mesh" {
            Vertices: *%(vertices_num)s {
                a: %(vertices)s
            } 
            PolygonVertexIndex: *%(polygons_num)s {
                a: %(polygons)s
            } 
            GeometryVersion: 124
            %(LayerElementNormal)s
            %(LayerElementBiNormal)s
            %(LayerElementTangent)s
            %(LayerElementColor)s
            %(LayerElementUV)s
            %(LayerElementUV2)s
            %(LayerElementUV3)s
            %(LayerElementUV4)s
            %(LayerElementUV5)s
            Layer: 0 {
                Version: 100
                %(LayerElementNormalInsert)s
                %(LayerElementBiNormalInsert)s
                %(LayerElementTangentInsert)s
                %(LayerElementColorInsert)s
                %(LayerElementUVInsert)s
                
            }
            Layer: 1 {
                Version: 100
                %(LayerElementUV2Insert)s
            }
            Layer: 2 {
                Version: 100
                %(LayerElementUV3Insert)s
            }
            Layer: 3 {
                Version: 100
                %(LayerElementUV4Insert)s
            }
            Layer: 4 {
                Version: 100
                %(LayerElementUV5Insert)s
            }
        }
        Model: 2035615390896, "Model::%(model_name)s", "Mesh" {
            Properties70:  {
                P: "DefaultAttributeIndex", "int", "Integer", "",0
            }
        }
    }

    ; Object connections
    ;------------------------------------------------------------------

    Connections:  {
        
        ;Model::pCube1, Model::RootNode
        C: "OO",2035615390896,0
        
        ;Geometry::, Model::pCube1
        C: "OO",2035541511296,2035615390896

    }

    """


# ==================== Data Unpacking (from extension) ====================

class MeshData(rd.MeshFormat):
    indexOffset = 0
    name = ''


def unpackData(fmt, data):
    formatChars = {}
    formatChars[rd.CompType.UInt]  = "xBHxIxxxL"
    formatChars[rd.CompType.SInt]  = "xbhxixxxl"
    formatChars[rd.CompType.Float] = "xxexfxxxd"

    formatChars[rd.CompType.UNorm] = formatChars[rd.CompType.UInt]
    formatChars[rd.CompType.UScaled] = formatChars[rd.CompType.UInt]
    formatChars[rd.CompType.SNorm] = formatChars[rd.CompType.SInt]
    formatChars[rd.CompType.SScaled] = formatChars[rd.CompType.SInt]

    vertexFormat = str(fmt.compCount) + formatChars[fmt.compType][fmt.compByteWidth]
    value = struct.unpack_from(vertexFormat, data, 0)

    if fmt.compType == rd.CompType.UNorm:
        divisor = float((2 ** (fmt.compByteWidth * 8)) - 1)
        value = tuple(float(i) / divisor for i in value)
    elif fmt.compType == rd.CompType.SNorm:
        maxNeg = -float(2 ** (fmt.compByteWidth * 8)) / 2
        divisor = float(-(maxNeg-1))
        value = tuple((float(i) if (i == maxNeg) else (float(i) / divisor)) for i in value)

    if fmt.BGRAOrder():
        value = tuple(value[i] for i in [2, 1, 0, 3])

    return value


def getMeshInputs(controller, draw):
    state = controller.GetPipelineState()

    ib = state.GetIBuffer()
    vbs = state.GetVBuffers()
    attrs = state.GetVertexInputs()

    meshInputs = []

    for attr in attrs:
        if attr.perInstance:
            continue

        meshInput = MeshData()
        meshInput.indexResourceId = ib.resourceId
        meshInput.indexByteOffset = ib.byteOffset
        meshInput.indexByteStride = ib.byteStride
        meshInput.baseVertex = draw.baseVertex
        meshInput.indexOffset = draw.indexOffset
        meshInput.numIndices = draw.numIndices

        if not (draw.flags & rd.ActionFlags.Indexed):
            meshInput.indexResourceId = rd.ResourceId.Null()

        meshInput.vertexByteOffset = attr.byteOffset + vbs[attr.vertexBuffer].byteOffset + draw.vertexOffset * vbs[attr.vertexBuffer].byteStride
        meshInput.format = attr.format
        meshInput.vertexResourceId = vbs[attr.vertexBuffer].resourceId
        meshInput.vertexByteStride = vbs[attr.vertexBuffer].byteStride
        meshInput.name = attr.name

        meshInputs.append(meshInput)

    return meshInputs


def getIndices(controller, mesh):
    indexFormat = 'B'
    if mesh.indexByteStride == 2:
        indexFormat = 'H'
    elif mesh.indexByteStride == 4:
        indexFormat = 'I'

    indexFormat = str(mesh.numIndices) + indexFormat

    if mesh.indexResourceId != rd.ResourceId.Null():
        ibdata = controller.GetBufferData(mesh.indexResourceId, mesh.indexByteOffset, 0)
        offset = mesh.indexOffset * mesh.indexByteStride
        indices = struct.unpack_from(indexFormat, ibdata, offset)
        return [i + mesh.baseVertex for i in indices]
    else:
        return tuple(range(mesh.numIndices))


# ==================== Attribute mapping ====================

_SWIZZLE_INDEX = {"x": 0, "y": 1, "z": 2, "w": 3,
                  "r": 0, "g": 1, "b": 2, "a": 3}


def parse_attr_spec(spec):
    if not spec:
        return "", None
    spec = spec.strip()
    if "." not in spec:
        return spec, None
    base, _, suffix = spec.rpartition(".")
    if base and suffix and all(ch in _SWIZZLE_INDEX for ch in suffix.lower()):
        indices = tuple(_SWIZZLE_INDEX[ch] for ch in suffix.lower())
        return base, indices
    return spec, None


def is_float_type(comp_type):
    return comp_type in (rd.CompType.Float,)


def infer_mapper(meshInputs, log):
    """Infer a semantic->attribute-name mapping purely from vertex formats.

    Vulkan/SPIR-V captures usually expose generic names like _input0.._inputN,
    so we classify by component count / type instead of by name.
    """
    attrs = []
    for mi in meshInputs:
        fmt = mi.format
        if fmt.Special():
            continue
        attrs.append({
            "name": mi.name,
            "compCount": int(fmt.compCount),
            "compType": str(fmt.compType),
            "byteWidth": int(fmt.compByteWidth),
            "isFloat": is_float_type(fmt.compType),
        })

    log("  Vertex attributes ({0}):".format(len(attrs)))
    for a in attrs:
        log("    {name}: {compCount}x {compType} ({byteWidth}B)".format(**a))

    mapper = {k: "" for k in
              ["POSITION", "NORMAL", "BINORMAL", "TANGENT", "COLOR",
               "UV", "UV2", "UV3", "UV4", "UV5"]}

    used = set()

    # POSITION: first float attribute with >=3 components.
    for a in attrs:
        if a["name"] in used:
            continue
        if a["compCount"] >= 3 and a["isFloat"] and a["byteWidth"] >= 4:
            mapper["POSITION"] = a["name"]
            used.add(a["name"])
            break
    # Fallback: any >=3 component attribute.
    if not mapper["POSITION"]:
        for a in attrs:
            if a["compCount"] >= 3 and a["name"] not in used:
                mapper["POSITION"] = a["name"]
                used.add(a["name"])
                break

    # UV sets: float attributes carry texcoords. A 2-component float attr is one
    # UV set; a 4-component float attr is TWO packed UV sets (.xy and .zw); a
    # 3-component float attr is treated as one set (.xy). Assigned in vertex
    # layout order. Non-float (SNorm/UNorm/UInt) attrs are normals/tangents/color,
    # never UVs.
    uv_slots = ["UV", "UV2", "UV3", "UV4", "UV5"]
    uv_i = 0
    for a in attrs:
        if a["name"] in used or not a["isFloat"]:
            continue
        if a["compCount"] == 2:
            specs = [a["name"]]
        elif a["compCount"] >= 4:
            specs = [a["name"] + ".xy", a["name"] + ".zw"]
        elif a["compCount"] == 3:
            specs = [a["name"] + ".xy"]
        else:
            continue
        for sp in specs:
            if uv_i < len(uv_slots):
                mapper[uv_slots[uv_i]] = sp
                uv_i += 1
        used.add(a["name"])

    # NORMAL / TANGENT: remaining non-UV 3/4-component attributes (packed SNorm/etc).
    remaining = [a for a in attrs if a["name"] not in used and a["compCount"] >= 3]
    if remaining:
        mapper["NORMAL"] = remaining[0]["name"]
        used.add(remaining[0]["name"])
    if len(remaining) > 1:
        mapper["TANGENT"] = remaining[1]["name"]
        used.add(remaining[1]["name"])

    # COLOR: a 4-component UNorm/byte attribute if still free.
    for a in attrs:
        if a["name"] in used:
            continue
        if a["compCount"] == 4 and ("UNorm" in a["compType"] or a["byteWidth"] == 1):
            mapper["COLOR"] = a["name"]
            used.add(a["name"])
            break

    log("  Inferred mapper:")
    for k in ["POSITION", "NORMAL", "TANGENT", "BINORMAL", "COLOR",
              "UV", "UV2", "UV3", "UV4", "UV5"]:
        if mapper[k]:
            log("    {0:9s} -> {1}".format(k, mapper[k]))

    return mapper, attrs


# ==================== FBX export (from extension) ====================

def export_fbx(save_path, mapper, data, attr_list):
    if not data:
        return False

    base_name = os.path.basename(os.path.splitext(save_path)[0])
    save_name = "mesh_{0}".format(base_name)

    idx_dict = data["IDX"]
    value_dict = defaultdict(list)
    vertex_data = defaultdict(dict)

    for i, idx in enumerate(idx_dict):
        for attr in attr_list:
            if attr in data:
                value = data[attr][i]
                value_dict[attr].append(value)
                if idx not in vertex_data[attr]:
                    vertex_data[attr][idx] = value

    ARGS = {
        "model_name": save_name,
        "LayerElementNormal": "", "LayerElementNormalInsert": "",
        "LayerElementBiNormal": "", "LayerElementBiNormalInsert": "",
        "LayerElementTangent": "", "LayerElementTangentInsert": "",
        "LayerElementColor": "", "LayerElementColorInsert": "",
        "LayerElementUV": "", "LayerElementUVInsert": "",
        "LayerElementUV2": "", "LayerElementUV2Insert": "",
        "LayerElementUV3": "", "LayerElementUV3Insert": "",
        "LayerElementUV4": "", "LayerElementUV4Insert": "",
        "LayerElementUV5": "", "LayerElementUV5Insert": "",
    }

    POSITION, _ = parse_attr_spec(mapper.get("POSITION", ""))
    NORMAL, _ = parse_attr_spec(mapper.get("NORMAL", ""))
    BINORMAL, _ = parse_attr_spec(mapper.get("BINORMAL", ""))
    TANGENT, _ = parse_attr_spec(mapper.get("TANGENT", ""))
    COLOR, _ = parse_attr_spec(mapper.get("COLOR", ""))
    UV, UV_SW = parse_attr_spec(mapper.get("UV", ""))
    UV2, UV2_SW = parse_attr_spec(mapper.get("UV2", ""))
    UV3, UV3_SW = parse_attr_spec(mapper.get("UV3", ""))
    UV4, UV4_SW = parse_attr_spec(mapper.get("UV4", ""))
    UV5, UV5_SW = parse_attr_spec(mapper.get("UV5", ""))

    if not vertex_data.get(POSITION):
        return False

    min_poly = min(idx_dict)
    idx_list = [idx - min_poly for idx in idx_dict]
    idx_len = len(idx_list)

    class ProcessHandler(object):
        def run(self):
            for name, func in inspect.getmembers(self, inspect.isroutine):
                if name.startswith("run_"):
                    func()

        def run_vertices(self):
            vertices = [str(v) for idx, values in sorted(vertex_data[POSITION].items()) for v in values[:3]]
            ARGS["vertices"] = ",".join(vertices)
            ARGS["vertices_num"] = len(vertices)

        def run_polygons(self):
            polygons = [str(idx ^ -1 if i % 3 == 2 else idx) for i, idx in enumerate(idx_list)]
            ARGS["polygons"] = ",".join(polygons)
            ARGS["polygons_num"] = len(polygons)

        def run_normals(self):
            if not NORMAL or not vertex_data.get(NORMAL):
                return
            normals = [str(v) for values in value_dict[NORMAL] for v in values[:3]]
            ARGS["LayerElementNormal"] = """
                LayerElementNormal: 0 {
                    Version: 101
                    Name: ""
                    MappingInformationType: "ByPolygonVertex"
                    ReferenceInformationType: "Direct"
                    Normals: *%(normals_num)s {
                        a: %(normals)s
                    } 
                }
            """ % {"normals": ",".join(normals), "normals_num": len(normals)}
            ARGS["LayerElementNormalInsert"] = """
                LayerElement:  {
                        Type: "LayerElementNormal"
                    TypedIndex: 0
                }
            """

        def run_tangents(self):
            if not TANGENT or not vertex_data.get(TANGENT):
                return
            tangents = [str(v) for values in value_dict[TANGENT] for v in values[:3]]
            ARGS["LayerElementTangent"] = """
                LayerElementTangent: 0 {
                    Version: 101
                    Name: "map1"
                    MappingInformationType: "ByPolygonVertex"
                    ReferenceInformationType: "Direct"
                    Tangents: *%(tangents_num)s {
                        a: %(tangents)s
                    } 
                }
            """ % {"tangents": ",".join(tangents), "tangents_num": len(tangents)}
            ARGS["LayerElementTangentInsert"] = """
                    LayerElement:  {
                        Type: "LayerElementTangent"
                        TypedIndex: 0
                    }
            """

        def run_color(self):
            if not COLOR or not vertex_data.get(COLOR):
                return
            colors = [str(v) for values in value_dict[COLOR] for i, v in enumerate(values, 1)]
            ARGS["LayerElementColor"] = """
                LayerElementColor: 0 {
                    Version: 101
                    Name: "colorSet1"
                    MappingInformationType: "ByPolygonVertex"
                    ReferenceInformationType: "IndexToDirect"
                    Colors: *%(colors_num)s {
                        a: %(colors)s
                    } 
                    ColorIndex: *%(colors_indices_num)s {
                        a: %(colors_indices)s
                    } 
                }
            """ % {
                "colors": ",".join(colors), "colors_num": len(colors),
                "colors_indices": ",".join([str(i) for i in range(idx_len)]),
                "colors_indices_num": idx_len,
            }
            ARGS["LayerElementColorInsert"] = """
                LayerElement:  {
                    Type: "LayerElementColor"
                    TypedIndex: 0
                }
            """

        def _emit_uv(self, uv_name, uv_sw, layer_key, insert_key, typed_index, map_name):
            if not uv_name or not vertex_data.get(uv_name):
                return
            uvs_indices = ",".join([str(idx) for idx in idx_list])
            sw = uv_sw if uv_sw else (0, 1)
            uvs = [
                str((1 - (values[sw[i]] if sw[i] < len(values) else 0.0)) if i == 1 else (values[sw[i]] if sw[i] < len(values) else 0.0))
                for idx, values in sorted(vertex_data[uv_name].items())
                for i in range(min(2, len(sw)))
            ]
            ARGS[layer_key] = ("""
                LayerElementUV: %(typed)s {
                    Version: 101
                    Name: "%(map)s"
                    MappingInformationType: "ByPolygonVertex"
                    ReferenceInformationType: "IndexToDirect"
                    UV: *%(uvs_num)s {
                        a: %(uvs)s
                    } 
                    UVIndex: *%(uvs_indices_num)s {
                        a: %(uvs_indices)s
                    } 
                }
            """) % {
                "typed": typed_index, "map": map_name,
                "uvs": ",".join(uvs), "uvs_num": len(uvs),
                "uvs_indices": uvs_indices, "uvs_indices_num": idx_len,
            }
            ARGS[insert_key] = """
                LayerElement:  {
                    Type: "LayerElementUV"
                    TypedIndex: %s
                }
            """ % typed_index

        def run_uv(self):
            self._emit_uv(UV, UV_SW, "LayerElementUV", "LayerElementUVInsert", 0, "map1")

        def run_uv2(self):
            self._emit_uv(UV2, UV2_SW, "LayerElementUV2", "LayerElementUV2Insert", 1, "map2")

        def run_uv3(self):
            self._emit_uv(UV3, UV3_SW, "LayerElementUV3", "LayerElementUV3Insert", 2, "map3")

        def run_uv4(self):
            self._emit_uv(UV4, UV4_SW, "LayerElementUV4", "LayerElementUV4Insert", 3, "map4")

        def run_uv5(self):
            self._emit_uv(UV5, UV5_SW, "LayerElementUV5", "LayerElementUV5Insert", 4, "map5")

    handler = ProcessHandler()
    handler.run()

    fbx = FBX_ASCII_TEMPLATE % ARGS

    with open(save_path, "w") as f:
        f.write(dedent(fbx).strip())

    return True


# ==================== Matrix helpers (from extension) ====================

def is_valid_transform_matrix(matrix):
    return (matrix[3] == 0.0 and matrix[7] == 0.0 and
            matrix[11] == 0.0 and matrix[15] == 1.0)


def auto_find_best_matrix(controller, log):
    try:
        state = controller.GetPipelineState()
        if not state:
            return (None, None, None, None)
        shader_refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
        if not shader_refl:
            return (None, None, None, None)

        runtime_cbs = state.GetConstantBlocks(rd.ShaderStage.Vertex)

        best_matrix = None
        best_set = best_binding = best_variable = None
        best_score = float('inf')

        for cb_index, cb_refl in enumerate(shader_refl.constantBlocks):
            if not hasattr(cb_refl, 'fixedBindSetOrSpace') or not hasattr(cb_refl, 'fixedBindNumber'):
                continue
            cb_set = cb_refl.fixedBindSetOrSpace
            cb_binding = cb_refl.fixedBindNumber

            runtime_cb = runtime_cbs[cb_index] if cb_index < len(runtime_cbs) else None
            if not runtime_cb or not hasattr(runtime_cb, 'descriptor'):
                continue
            desc = runtime_cb.descriptor
            if not hasattr(desc, 'resource'):
                continue
            res_id = desc.resource
            buf_offset = desc.byteOffset if hasattr(desc, 'byteOffset') else 0
            buf_size = desc.byteSize if hasattr(desc, 'byteSize') else cb_refl.byteSize
            buffer_data = controller.GetBufferData(res_id, buf_offset, buf_size)

            for var in cb_refl.variables:
                if not hasattr(var, 'name'):
                    continue
                var_offset = getattr(var, 'byteOffset', getattr(var, 'offset', 0))
                if len(buffer_data) < var_offset + 64:
                    continue
                try:
                    matrix = struct.unpack('16f', buffer_data[var_offset:var_offset+64])
                    score = abs(matrix[3]) + abs(matrix[7]) + abs(matrix[11]) + abs(matrix[15] - 1.0)
                    if score < best_score:
                        best_score = score
                        best_matrix = matrix
                        best_set, best_binding, best_variable = cb_set, cb_binding, var.name
                except Exception:
                    continue

        if best_matrix and best_score < 0.001:
            transposed = (
                best_matrix[0], best_matrix[4], best_matrix[8], best_matrix[12],
                best_matrix[1], best_matrix[5], best_matrix[9], best_matrix[13],
                best_matrix[2], best_matrix[6], best_matrix[10], best_matrix[14],
                best_matrix[3], best_matrix[7], best_matrix[11], best_matrix[15],
            )
            log("  Auto-matrix: set={0} binding={1} var='{2}' score={3:.6f}".format(
                best_set, best_binding, best_variable, best_score))
            return (transposed, best_set, best_binding, best_variable)

        log("  Auto-matrix: no valid affine matrix found (best score={0})".format(best_score))
        return (None, None, None, None)
    except Exception as e:
        log("  Auto-matrix error: {0}".format(str(e)))
        return (None, None, None, None)


def transform_vertices_with_matrix(data, matrix, mapper):
    if not matrix:
        return data

    position_key = mapper.get('POSITION', '')
    normal_key = mapper.get('NORMAL', '')
    tangent_key = mapper.get('TANGENT', '')
    binormal_key = mapper.get('BINORMAL', '')

    if position_key and position_key in data:
        out = []
        for pos in data[position_key]:
            x = pos[0] if len(pos) >= 1 else 0.0
            y = pos[1] if len(pos) >= 2 else 0.0
            z = pos[2] if len(pos) >= 3 else 0.0
            x_new = matrix[0]*x + matrix[1]*y + matrix[2]*z + matrix[3]
            y_new = matrix[4]*x + matrix[5]*y + matrix[6]*z + matrix[7]
            z_new = matrix[8]*x + matrix[9]*y + matrix[10]*z + matrix[11]
            out.append((x_new, y_new, z_new))
        data[position_key] = out

    for key in (normal_key, tangent_key, binormal_key):
        if key and key in data:
            out = []
            for n in data[key]:
                x = n[0] if len(n) >= 1 else 0.0
                y = n[1] if len(n) >= 2 else 0.0
                z = n[2] if len(n) >= 3 else 0.0
                x_new = matrix[0]*x + matrix[1]*y + matrix[2]*z
                y_new = matrix[4]*x + matrix[5]*y + matrix[6]*z
                z_new = matrix[8]*x + matrix[9]*y + matrix[10]*z
                length = (x_new*x_new + y_new*y_new + z_new*z_new) ** 0.5
                if length > 0.0001:
                    out.append((x_new/length, y_new/length, z_new/length))
                else:
                    out.append(n)
            data[key] = out

    return data


# ==================== Texture export ====================

def save_textures(controller, draw, tex_dir, log):
    count = 0
    try:
        os.makedirs(tex_dir, exist_ok=True)
    except Exception:
        pass
    state = controller.GetPipelineState()
    seen = set()
    try:
        used = state.GetReadOnlyResources(rd.ShaderStage.Fragment)
        for ud in used:
            try:
                res = ud.descriptor.resource
            except Exception:
                continue
            if res == rd.ResourceId.Null() or int(res) in seen:
                continue
            seen.add(int(res))
            texsave = rd.TextureSave()
            texsave.resourceId = res
            texsave.mip = 0
            texsave.slice.sliceIndex = 0
            texsave.alpha = rd.AlphaMapping.Preserve
            texsave.destType = rd.FileType.PNG
            out = os.path.join(tex_dir, "tex_{0}.png".format(int(res)))
            try:
                controller.SaveTexture(texsave, out)
                count += 1
            except Exception as e:
                log("    texture {0} failed: {1}".format(int(res), str(e)))
    except Exception as e:
        log("  texture export error: {0}".format(str(e)))
    return count


# ==================== Per-capture export ====================

def find_action(controller, event_id):
    result = [None]

    def rec(actions):
        for a in actions:
            if a.eventId == event_id:
                result[0] = a
                return True
            if a.children and rec(a.children):
                return True
        return False

    rec(controller.GetRootActions())
    return result[0]


def process_capture(entry, log):
    skin = entry["skin"]
    rdc_path = entry["rdc"]
    event_id = entry["eventId"]

    out_dir = os.path.join(OUTPUT_ROOT, skin)
    os.makedirs(out_dir, exist_ok=True)

    log("")
    log("=" * 60)
    log("[{0}] {1}  EventID={2}".format(skin, rdc_path, event_id))
    log("=" * 60)

    cap = rd.OpenCaptureFile()
    status = cap.OpenFile(rdc_path, '', None)
    if status.code != rd.ResultCode.Succeeded:
        log("  ERROR: OpenFile failed: {0}".format(str(status.code)))
        return False
    if cap.LocalReplaySupport() != rd.ReplaySupport.Supported:
        log("  ERROR: local replay not supported")
        cap.Shutdown()
        return False

    status, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    if status.code != rd.ResultCode.Succeeded:
        log("  ERROR: OpenCapture failed: {0}".format(str(status.code)))
        cap.Shutdown()
        return False

    ok = False
    try:
        draw = find_action(controller, event_id)
        if draw is None:
            log("  ERROR: EventID {0} not found".format(event_id))
            return False

        controller.SetFrameEvent(event_id, True)

        meshInputs = getMeshInputs(controller, draw)
        if not meshInputs:
            log("  ERROR: no mesh inputs")
            return False

        mapper, attr_meta = infer_mapper(meshInputs, log)

        indices = getIndices(controller, meshInputs[0])
        log("  indices={0}  unique_verts~{1}".format(len(indices), len(set(indices))))

        # Read every non-special attribute once and unpack per index.
        data = defaultdict(list)
        attr_list = set()
        max_idx = max(indices) if indices else 0
        for attr in meshInputs:
            if attr.format.Special():
                continue
            attr_list.add(attr.name)
            buffer_size = (max_idx + 1) * attr.vertexByteStride
            try:
                full = controller.GetBufferData(attr.vertexResourceId, attr.vertexByteOffset, buffer_size)
                for idx in indices:
                    off = idx * attr.vertexByteStride
                    data[attr.name].append(unpackData(attr.format, full[off:]))
            except Exception as e:
                log("    attr {0} read failed: {1}".format(attr.name, str(e)))

        data["IDX"] = indices

        # ---- Dump data BEFORE any transform (UVs/attrs are transform-invariant here) ----
        # Build one canonical ordered unique-index list shared by every attribute so
        # the three captures can be compared per-vertex (identical topology/indices).
        first_row = {}
        for i, idx in enumerate(indices):
            if idx not in first_row:
                first_row[idx] = i
        ordered_idx = sorted(first_row.keys())

        uv_dump = {
            "skin": skin,
            "rdc": rdc_path,
            "eventId": event_id,
            "indexCount": len(indices),
            "uniqueVertexCount": len(ordered_idx),
            "attributes": attr_meta,
            "mapper": mapper,
            "orderedIndices": ordered_idx,
            "rawAttrs": {},
            "uvChannels": {},
        }

        # Raw per-unique-vertex values for EVERY non-special attribute (all comps).
        # This lets the comparator inspect potential extra-UV attrs (e.g. _input5).
        for attr_name in sorted(attr_list):
            if attr_name not in data:
                continue
            per_vertex = []
            for idx in ordered_idx:
                vals = data[attr_name][first_row[idx]]
                per_vertex.append([float(x) for x in vals])
            uv_dump["rawAttrs"][attr_name] = per_vertex

        # Convenience: the mapped UV channels.
        for slot in ["UV", "UV2", "UV3", "UV4", "UV5"]:
            attr_name = mapper.get(slot, "")
            if not attr_name or attr_name not in data:
                continue
            uvs = []
            for idx in ordered_idx:
                vals = data[attr_name][first_row[idx]]
                uvs.append([float(vals[0]) if len(vals) > 0 else 0.0,
                            float(vals[1]) if len(vals) > 1 else 0.0])
            uv_dump["uvChannels"][slot] = {"attr": attr_name, "count": len(uvs), "uvs": uvs}

        with open(os.path.join(out_dir, "uv_dump.json"), "w", encoding="utf-8") as f:
            json.dump(uv_dump, f, ensure_ascii=False)
        log("  UV dump: {0} mapped channel(s), {1} raw attrs -> uv_dump.json".format(
            len(uv_dump["uvChannels"]), len(uv_dump["rawAttrs"])))

        # ---- World transform (positions/normals only) ----
        if APPLY_MATRIX:
            m, s, b, v = auto_find_best_matrix(controller, log)
            if m:
                data = transform_vertices_with_matrix(data, m, mapper)
                log("  transform applied")
            else:
                log("  transform skipped (object space)")

        # ---- Textures ----
        if EXPORT_TEXTURES:
            n = save_textures(controller, draw, os.path.join(out_dir, "textures"), log)
            log("  textures exported: {0}".format(n))

        # ---- FBX ----
        fbx_path = os.path.join(out_dir, "carbody.fbx")
        if export_fbx(fbx_path, mapper, data, attr_list):
            log("  FBX -> {0}".format(fbx_path))
            ok = True
        else:
            log("  ERROR: FBX export failed")
    finally:
        try:
            controller.Shutdown()
        except Exception:
            pass
        try:
            cap.Shutdown()
        except Exception:
            pass

    return ok


def main():
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    log_path = os.path.join(OUTPUT_ROOT, "export_log.txt")
    log_lines = []

    def log(msg):
        print(msg)
        log_lines.append(str(msg))

    log("Headless car-body export starting")
    results = {}
    for entry in CONFIG:
        try:
            ok = process_capture(entry, log)
        except Exception as e:
            import traceback
            log("  FATAL for {0}: {1}".format(entry["skin"], str(e)))
            log(traceback.format_exc())
            ok = False
        results[entry["skin"]] = ok

    log("")
    log("Summary:")
    for skin, ok in results.items():
        log("  {0}: {1}".format(skin, "OK" if ok else "FAILED"))

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))


if __name__ == "__main__":
    main()
    # Prevent qrenderdoc from opening its main UI after the script.
    sys.exit(0)
