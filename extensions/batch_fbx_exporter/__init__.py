# -*- coding: utf-8 -*-
"""
Batch FBX Exporter for RenderDoc
Combines batch export functionality with FBX format output
"""

from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

__author__ = "Maek"
__date__ = "2025-11-11"

import os
import sys
import time
import struct
import inspect
from textwrap import dedent
from functools import partial
from collections import defaultdict

from PySide2 import QtWidgets, QtCore

import qrenderdoc
import renderdoc as rd

from .batch_dialog import BatchExportDialog
from .progress_dialog import MProgressDialog
from .export_window import ExportLogWindow

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


# ==================== Data Unpacking Functions ====================

class MeshData(rd.MeshFormat):
    """Extended MeshFormat with additional properties"""
    indexOffset = 0
    name = ''


def unpackData(fmt, data):
    """Unpack vertex data from bytes based on format"""
    formatChars = {}
    #                                 012345678
    formatChars[rd.CompType.UInt]  = "xBHxIxxxL"
    formatChars[rd.CompType.SInt]  = "xbhxixxxl"
    formatChars[rd.CompType.Float] = "xxexfxxxd"  # only 2, 4 and 8 are valid

    # These types have identical decodes, but we might post-process them
    formatChars[rd.CompType.UNorm] = formatChars[rd.CompType.UInt]
    formatChars[rd.CompType.UScaled] = formatChars[rd.CompType.UInt]
    formatChars[rd.CompType.SNorm] = formatChars[rd.CompType.SInt]
    formatChars[rd.CompType.SScaled] = formatChars[rd.CompType.SInt]

    # We need to fetch compCount components
    vertexFormat = str(fmt.compCount) + formatChars[fmt.compType][fmt.compByteWidth]

    # Unpack the data
    value = struct.unpack_from(vertexFormat, data, 0)

    # If the format needs post-processing such as normalisation, do that now
    if fmt.compType == rd.CompType.UNorm:
        divisor = float((2 ** (fmt.compByteWidth * 8)) - 1)
        value = tuple(float(i) / divisor for i in value)
    elif fmt.compType == rd.CompType.SNorm:
        maxNeg = -float(2 ** (fmt.compByteWidth * 8)) / 2
        divisor = float(-(maxNeg-1))
        value = tuple((float(i) if (i == maxNeg) else (float(i) / divisor)) for i in value)

    # If the format is BGRA, swap the two components
    if fmt.BGRAOrder():
        value = tuple(value[i] for i in [2, 1, 0, 3])

    return value


def getMeshInputs(controller, draw):
    """Get vertex and index buffer information for a draw call"""
    state = controller.GetPipelineState()

    # Get the index & vertex buffers, and fixed vertex inputs
    ib = state.GetIBuffer()
    vbs = state.GetVBuffers()
    attrs = state.GetVertexInputs()
    
    meshInputs = []
    
    for attr in attrs:
        # We don't handle instance attributes
        if attr.perInstance:
            continue
        
        meshInput = MeshData()
        meshInput.indexResourceId = ib.resourceId
        meshInput.indexByteOffset = ib.byteOffset
        meshInput.indexByteStride = ib.byteStride
        meshInput.baseVertex = draw.baseVertex
        meshInput.indexOffset = draw.indexOffset
        meshInput.numIndices = draw.numIndices

        # If the draw doesn't use an index buffer, don't use it even if bound
        if not (draw.flags & rd.ActionFlags.Indexed):
            meshInput.indexResourceId = rd.ResourceId.Null()

        # The total offset is the attribute offset from the base of the vertex
        meshInput.vertexByteOffset = attr.byteOffset + vbs[attr.vertexBuffer].byteOffset + draw.vertexOffset * vbs[attr.vertexBuffer].byteStride
        meshInput.format = attr.format
        meshInput.vertexResourceId = vbs[attr.vertexBuffer].resourceId
        meshInput.vertexByteStride = vbs[attr.vertexBuffer].byteStride
        meshInput.name = attr.name

        meshInputs.append(meshInput)

    return meshInputs


def getIndices(controller, mesh):
    """Extract indices from index buffer"""
    # Get the character for the width of index
    indexFormat = 'B'
    if mesh.indexByteStride == 2:
        indexFormat = 'H'
    elif mesh.indexByteStride == 4:
        indexFormat = 'I'

    # Duplicate the format by the number of indices
    indexFormat = str(mesh.numIndices) + indexFormat

    # If we have an index buffer
    if mesh.indexResourceId != rd.ResourceId.Null():
        # Fetch the data
        ibdata = controller.GetBufferData(mesh.indexResourceId, mesh.indexByteOffset, 0)
        # Unpack all the indices, starting from the first index to fetch
        offset = mesh.indexOffset * mesh.indexByteStride
        indices = struct.unpack_from(indexFormat, ibdata, offset)

        # Apply the baseVertex offset
        return [i + mesh.baseVertex for i in indices]
    else:
        # With no index buffer, just generate a range
        return tuple(range(mesh.numIndices))


def saveTexture(resourceId, eventId, output_folder, controller):
    """Save a single texture to PNG"""
    texsave = rd.TextureSave()
    texsave.resourceId = resourceId
    if texsave.resourceId == rd.ResourceId.Null():
        return False

    filename = str(int(texsave.resourceId))
    texsave.mip = 0
    texsave.slice.sliceIndex = 0
    texsave.alpha = rd.AlphaMapping.Preserve
    texsave.destType = rd.FileType.PNG
    
    event_folder = os.path.join(output_folder, str(eventId))
    if not os.path.exists(event_folder):
        os.makedirs(event_folder)

    outTexPath = os.path.join(event_folder, filename + ".png")
    controller.SaveTexture(texsave, outTexPath)
    print("Saved texture: {0}".format(outTexPath))
    return True


# ==================== FBX Export Functions ====================

# Mapping from swizzle character to component index
_SWIZZLE_INDEX = {"x": 0, "y": 1, "z": 2, "w": 3,
                  "r": 0, "g": 1, "b": 2, "a": 3}


def parse_attr_spec(spec):
    """Parse an attribute mapping like '_input5.zw' into (base_name, swizzle).

    Args:
        spec: mapper string, e.g. '_input5', '_input5.xy', '_input5.zw', 'TEXCOORD0.yz'

    Returns:
        (base_name, swizzle_indices) where swizzle_indices is a tuple of ints
        (e.g. (0, 1) for '.xy', (2, 3) for '.zw') or None if no suffix was provided.
        Returns ('', None) for empty/None input.
    """
    if not spec:
        return "", None

    spec = spec.strip()
    if "." not in spec:
        return spec, None

    base, _, suffix = spec.rpartition(".")
    # Only treat as swizzle if all suffix characters are valid channel letters
    if base and suffix and all(ch in _SWIZZLE_INDEX for ch in suffix.lower()):
        indices = tuple(_SWIZZLE_INDEX[ch] for ch in suffix.lower())
        return base, indices

    # Not a recognizable swizzle - treat the whole spec as the attribute name
    return spec, None


def export_fbx(save_path, mapper, data, attr_list, controller):
    """Export mesh data to FBX format"""
    if not data:
        return False

    # Get filename without extension (e.g., "1" from "1.fbx")
    base_name = os.path.basename(os.path.splitext(save_path)[0])
    # Format as mesh_N for FBX internal naming
    save_name = "mesh_{0}".format(base_name)

    # We'll decode the first three indices making up a triangle
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
        "LayerElementNormal": "",
        "LayerElementNormalInsert": "",
        "LayerElementBiNormal": "",
        "LayerElementBiNormalInsert": "",
        "LayerElementTangent": "",
        "LayerElementTangentInsert": "",
        "LayerElementColor": "",
        "LayerElementColorInsert": "",
        "LayerElementUV": "",
        "LayerElementUVInsert": "",
        "LayerElementUV2": "",
        "LayerElementUV2Insert": "",
        "LayerElementUV3": "",
        "LayerElementUV3Insert": "",
        "LayerElementUV4": "",
        "LayerElementUV4Insert": "",
        "LayerElementUV5": "",
        "LayerElementUV5Insert": "",
    }

    # Parse each mapper entry into (base_name, swizzle_indices).
    # For non-UV attributes we currently ignore any swizzle (existing behavior),
    # but resolving the base name lets users optionally add channel hints
    # like ".xyz" in the UI without breaking the export.
    POSITION, _POSITION_SW = parse_attr_spec(mapper.get("POSITION", ""))
    NORMAL, _NORMAL_SW = parse_attr_spec(mapper.get("NORMAL", ""))
    BINORMAL, _BINORMAL_SW = parse_attr_spec(mapper.get("BINORMAL", ""))
    TANGENT, _TANGENT_SW = parse_attr_spec(mapper.get("TANGENT", ""))
    COLOR, _COLOR_SW = parse_attr_spec(mapper.get("COLOR", ""))
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
            """ % {
                "normals": ",".join(normals),
                "normals_num": len(normals),
            }
            ARGS["LayerElementNormalInsert"] = """
                LayerElement:  {
                        Type: "LayerElementNormal"
                    TypedIndex: 0
                }
            """

        def run_binormals(self):
            if not BINORMAL or not vertex_data.get(BINORMAL):
                return
            binormals = [str(-v) for values in value_dict[BINORMAL] for v in values[:3]]

            ARGS["LayerElementBiNormal"] = """
                LayerElementBinormal: 0 {
                    Version: 101
                    Name: "map1"
                    MappingInformationType: "ByVertice"
                    ReferenceInformationType: "Direct"
                    Binormals: *%(binormals_num)s {
                        a: %(binormals)s
                    } 
                    BinormalsW: *%(binormalsW_num)s {
                        a: %(binormalsW)s
                    } 
                }
            """ % {
                "binormals": ",".join(binormals),
                "binormals_num": len(binormals),
                "binormalsW": ",".join(["1" for i in range(idx_len)]),
                "binormalsW_num": idx_len,
            }
            ARGS["LayerElementBiNormalInsert"] = """
                LayerElement:  {
                        Type: "LayerElementBinormal"
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
            """ % {
                "tangents": ",".join(tangents),
                "tangents_num": len(tangents),
            }

            ARGS["LayerElementTangentInsert"] = """
                    LayerElement:  {
                        Type: "LayerElementTangent"
                        TypedIndex: 0
                    }
            """

        def run_color(self):
            if not COLOR or not vertex_data.get(COLOR):
                return

            colors = [
                str(v)
                for values in value_dict[COLOR]
                for i, v in enumerate(values, 1)
            ]

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
                "colors": ",".join(colors),
                "colors_num": len(colors),
                "colors_indices": ",".join([str(i) for i in range(idx_len)]),
                "colors_indices_num": idx_len,
            }
            ARGS["LayerElementColorInsert"] = """
                LayerElement:  {
                    Type: "LayerElementColor"
                    TypedIndex: 0
                }
            """

        def run_uv(self):
            if not UV or not vertex_data.get(UV):
                return

            uvs_indices = ",".join([str(idx) for idx in idx_list])
            # If a swizzle like '.zw' was supplied, pick exactly those 2 channels;
            # otherwise default to the first two components (.xy).
            sw = UV_SW if UV_SW else (0, 1)
            uvs = [
                # NOTE flip y axis (only for the V component, index 1 in the output pair)
                str((1 - (values[sw[i]] if sw[i] < len(values) else 0.0)) if i == 1 else (values[sw[i]] if sw[i] < len(values) else 0.0))
                for idx, values in sorted(vertex_data[UV].items())
                for i in range(min(2, len(sw)))
            ]

            ARGS["LayerElementUV"] = """
                LayerElementUV: 0 {
                    Version: 101
                    Name: "map1"
                    MappingInformationType: "ByPolygonVertex"
                    ReferenceInformationType: "IndexToDirect"
                    UV: *%(uvs_num)s {
                        a: %(uvs)s
                    } 
                    UVIndex: *%(uvs_indices_num)s {
                        a: %(uvs_indices)s
                    } 
                }
            """ % {
                "uvs": ",".join(uvs),
                "uvs_num": len(uvs),
                "uvs_indices": uvs_indices,
                "uvs_indices_num": idx_len,
            }

            ARGS["LayerElementUVInsert"] = """
                LayerElement:  {
                    Type: "LayerElementUV"
                    TypedIndex: 0
                }
            """

        def run_uv2(self):
            if not UV2 or not vertex_data.get(UV2):
                return

            uvs_indices = ",".join([str(idx) for idx in idx_list])
            sw = UV2_SW if UV2_SW else (0, 1)
            uvs = [
                # NOTE flip y axis (only for the V component)
                str((1 - (values[sw[i]] if sw[i] < len(values) else 0.0)) if i == 1 else (values[sw[i]] if sw[i] < len(values) else 0.0))
                for idx, values in sorted(vertex_data[UV2].items())
                for i in range(min(2, len(sw)))
            ]

            ARGS["LayerElementUV2"] = """
                LayerElementUV: 1 {
                    Version: 101
                    Name: "map2"
                    MappingInformationType: "ByPolygonVertex"
                    ReferenceInformationType: "IndexToDirect"
                    UV: *%(uvs_num)s {
                        a: %(uvs)s
                    } 
                    UVIndex: *%(uvs_indices_num)s {
                        a: %(uvs_indices)s
                    } 
                }
            """ % {
                "uvs": ",".join(uvs),
                "uvs_num": len(uvs),
                "uvs_indices": uvs_indices,
                "uvs_indices_num": idx_len,
            }

            ARGS["LayerElementUV2Insert"] = """
                LayerElement:  {
                    Type: "LayerElementUV"
                    TypedIndex: 1
                }
            """

        def run_uv3(self):
            if not UV3 or not vertex_data.get(UV3):
                return

            uvs_indices = ",".join([str(idx) for idx in idx_list])
            sw = UV3_SW if UV3_SW else (0, 1)
            uvs = [
                # NOTE flip y axis (only for the V component)
                str((1 - (values[sw[i]] if sw[i] < len(values) else 0.0)) if i == 1 else (values[sw[i]] if sw[i] < len(values) else 0.0))
                for idx, values in sorted(vertex_data[UV3].items())
                for i in range(min(2, len(sw)))
            ]

            ARGS["LayerElementUV3"] = """
                LayerElementUV: 2 {
                    Version: 101
                    Name: "map3"
                    MappingInformationType: "ByPolygonVertex"
                    ReferenceInformationType: "IndexToDirect"
                    UV: *%(uvs_num)s {
                        a: %(uvs)s
                    } 
                    UVIndex: *%(uvs_indices_num)s {
                        a: %(uvs_indices)s
                    } 
                }
            """ % {
                "uvs": ",".join(uvs),
                "uvs_num": len(uvs),
                "uvs_indices": uvs_indices,
                "uvs_indices_num": idx_len,
            }

            ARGS["LayerElementUV3Insert"] = """
                LayerElement:  {
                    Type: "LayerElementUV"
                    TypedIndex: 2
                }
            """

        def run_uv4(self):
            if not UV4 or not vertex_data.get(UV4):
                return

            uvs_indices = ",".join([str(idx) for idx in idx_list])
            sw = UV4_SW if UV4_SW else (0, 1)
            uvs = [
                # NOTE flip y axis (only for the V component)
                str((1 - (values[sw[i]] if sw[i] < len(values) else 0.0)) if i == 1 else (values[sw[i]] if sw[i] < len(values) else 0.0))
                for idx, values in sorted(vertex_data[UV4].items())
                for i in range(min(2, len(sw)))
            ]

            ARGS["LayerElementUV4"] = """
                LayerElementUV: 3 {
                    Version: 101
                    Name: "map4"
                    MappingInformationType: "ByPolygonVertex"
                    ReferenceInformationType: "IndexToDirect"
                    UV: *%(uvs_num)s {
                        a: %(uvs)s
                    } 
                    UVIndex: *%(uvs_indices_num)s {
                        a: %(uvs_indices)s
                    } 
                }
            """ % {
                "uvs": ",".join(uvs),
                "uvs_num": len(uvs),
                "uvs_indices": uvs_indices,
                "uvs_indices_num": idx_len,
            }

            ARGS["LayerElementUV4Insert"] = """
                LayerElement:  {
                    Type: "LayerElementUV"
                    TypedIndex: 3
                }
            """

        def run_uv5(self):
            if not UV5 or not vertex_data.get(UV5):
                return

            uvs_indices = ",".join([str(idx) for idx in idx_list])
            sw = UV5_SW if UV5_SW else (0, 1)
            uvs = [
                # NOTE flip y axis (only for the V component)
                str((1 - (values[sw[i]] if sw[i] < len(values) else 0.0)) if i == 1 else (values[sw[i]] if sw[i] < len(values) else 0.0))
                for idx, values in sorted(vertex_data[UV5].items())
                for i in range(min(2, len(sw)))
            ]

            ARGS["LayerElementUV5"] = """
                LayerElementUV: 4 {
                    Version: 101
                    Name: "map5"
                    MappingInformationType: "ByPolygonVertex"
                    ReferenceInformationType: "IndexToDirect"
                    UV: *%(uvs_num)s {
                        a: %(uvs)s
                    } 
                    UVIndex: *%(uvs_indices_num)s {
                        a: %(uvs_indices)s
                    } 
                }
            """ % {
                "uvs": ",".join(uvs),
                "uvs_num": len(uvs),
                "uvs_indices": uvs_indices,
                "uvs_indices_num": idx_len,
            }

            ARGS["LayerElementUV5Insert"] = """
                LayerElement:  {
                    Type: "LayerElementUV"
                    TypedIndex: 4
                }
            """

    handler = ProcessHandler()
    handler.run()

    fbx = FBX_ASCII_TEMPLATE % ARGS

    with open(save_path, "w") as f:
        f.write(dedent(fbx).strip())
    
    return True


# ==================== Matrix Transform Functions ====================

def is_valid_transform_matrix(matrix):
    """Check if matrix is a valid transformation matrix (last column is 0,0,0,1)
    
    Args:
        matrix: tuple/list of 16 floats (row-major)
    
    Returns:
        bool: True if last column is (0, 0, 0, 1)
    """
    # Last column indices (in row-major): [3], [7], [11], [15]
    return (matrix[3] == 0.0 and 
            matrix[7] == 0.0 and 
            matrix[11] == 0.0 and 
            matrix[15] == 1.0)


def auto_find_best_matrix(controller, log_func=None):
    """Automatically find the best transformation matrix by searching all CBs
    
    Args:
        controller: RenderDoc controller
        log_func: Optional logging function
    
    Returns:
        tuple: (matrix, set, binding, variable_name) or (None, None, None, None) if not found
    """
    def log(msg):
        if log_func:
            log_func(msg)
        else:
            print(msg)
    
    try:
        import struct
        
        log("=== AUTO-FINDING BEST MATRIX ===")
        log("Searching through all Descriptor Sets, Bindings, and Variables...")
        
        state = controller.GetPipelineState()
        if not state:
            log("Error: No pipeline state")
            return (None, None, None, None)
        
        shader_refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
        if not shader_refl:
            log("Error: No shader reflection")
            return (None, None, None, None)
        
        runtime_cbs = state.GetConstantBlocks(rd.ShaderStage.Vertex)
        
        best_matrix = None
        best_set = None
        best_binding = None
        best_variable = None
        best_score = float('inf')
        
        # Search through all constant blocks
        for cb_index, cb_refl in enumerate(shader_refl.constantBlocks):
            if not hasattr(cb_refl, 'fixedBindSetOrSpace') or not hasattr(cb_refl, 'fixedBindNumber'):
                continue
            
            cb_set = cb_refl.fixedBindSetOrSpace
            cb_binding = cb_refl.fixedBindNumber
            
            log("Checking CB: Set={0}, Binding={1}, Name='{2}'".format(cb_set, cb_binding, cb_refl.name))
            
            # Get runtime CB
            runtime_cb = None
            if cb_index < len(runtime_cbs):
                runtime_cb = runtime_cbs[cb_index]
            
            if not runtime_cb or not hasattr(runtime_cb, 'descriptor'):
                continue
            
            desc = runtime_cb.descriptor
            if not hasattr(desc, 'resource'):
                continue
            
            res_id = desc.resource
            buf_offset = desc.byteOffset if hasattr(desc, 'byteOffset') else 0
            buf_size = desc.byteSize if hasattr(desc, 'byteSize') else cb_refl.byteSize
            
            # Read buffer data
            buffer_data = controller.GetBufferData(res_id, buf_offset, buf_size)
            
            # Search through all variables
            for var in cb_refl.variables:
                if not hasattr(var, 'name'):
                    continue
                
                var_name = var.name
                var_offset = 0
                
                if hasattr(var, 'byteOffset'):
                    var_offset = var.byteOffset
                elif hasattr(var, 'offset'):
                    var_offset = var.offset
                
                # Check if we have enough data for a matrix (64 bytes)
                if len(buffer_data) < var_offset + 64:
                    continue
                
                # Extract matrix
                try:
                    matrix = struct.unpack('16f', buffer_data[var_offset:var_offset+64])
                    
                    # Calculate score: how close is last column to (0, 0, 0, 1)?
                    score = abs(matrix[3]) + abs(matrix[7]) + abs(matrix[11]) + abs(matrix[15] - 1.0)
                    
                    if score < best_score:
                        best_score = score
                        best_matrix = matrix
                        best_set = cb_set
                        best_binding = cb_binding
                        best_variable = var_name
                        
                        log("  → Found candidate: Variable='{0}', offset={1}, score={2:.6f}".format(
                            var_name, var_offset, score))
                        log("    Last column: [{0:.5f}, {1:.5f}, {2:.5f}, {3:.5f}]".format(
                            matrix[3], matrix[7], matrix[11], matrix[15]))
                    
                except Exception as e:
                    continue
        
        if best_matrix:
            log("=== BEST MATRIX FOUND ===")
            log("Set={0}, Binding={1}, Variable='{2}'".format(best_set, best_binding, best_variable))
            log("Score: {0:.6f} (0.0 = perfect match)".format(best_score))
            log("Last column: [{0:.5f}, {1:.5f}, {2:.5f}, {3:.5f}]".format(
                best_matrix[3], best_matrix[7], best_matrix[11], best_matrix[15]))
            
            # Transpose the matrix
            transposed = (
                best_matrix[0], best_matrix[4], best_matrix[8], best_matrix[12],
                best_matrix[1], best_matrix[5], best_matrix[9], best_matrix[13],
                best_matrix[2], best_matrix[6], best_matrix[10], best_matrix[14],
                best_matrix[3], best_matrix[7], best_matrix[11], best_matrix[15]
            )
            return (transposed, best_set, best_binding, best_variable)
        else:
            log("=== NO VALID MATRIX FOUND ===")
            return (None, None, None, None)
        
    except Exception as e:
        log("Error during auto-find: {0}".format(str(e)))
        return (None, None, None, None)


def read_matrix_from_renderdoc(controller, target_set, target_binding, variable_name, log_func=None, auto_find=False, transpose=True):
    """Read transformation matrix from RenderDoc constant buffer
    
    Args:
        controller: RenderDoc controller
        target_set: Descriptor set number
        target_binding: Binding number
        variable_name: Variable name in constant buffer
        log_func: Optional logging function
        auto_find: Whether to auto-find best matrix if validation fails
        transpose: Whether to transpose the matrix (column-major to row-major)
    
    Returns:
        tuple of 16 floats (4x4 matrix) or None if failed
    """
    def log(msg):
        if log_func:
            log_func(msg)
        else:
            print(msg)
    
    def apply_transpose(matrix, should_transpose):
        """Apply transpose to matrix if needed"""
        if should_transpose:
            log("Applying transpose (column-major → row-major)")
            return (
                matrix[0], matrix[4], matrix[8], matrix[12],
                matrix[1], matrix[5], matrix[9], matrix[13],
                matrix[2], matrix[6], matrix[10], matrix[14],
                matrix[3], matrix[7], matrix[11], matrix[15]
            )
        else:
            log("No transpose (using original matrix)")
            return matrix
    
    try:
        import struct
        
        # Get pipeline state
        state = controller.GetPipelineState()
        if not state:
            log("Error: No pipeline state")
            return None
        
        # Get shader reflection
        shader_refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
        if not shader_refl:
            log("Error: No shader reflection")
            return None
        
        # Get runtime constant blocks
        runtime_cbs = state.GetConstantBlocks(rd.ShaderStage.Vertex)
        
        # Search through constant blocks in shader reflection
        for cb_index, cb_refl in enumerate(shader_refl.constantBlocks):
            # Check if this CB matches our target set and binding
            if not hasattr(cb_refl, 'fixedBindSetOrSpace') or not hasattr(cb_refl, 'fixedBindNumber'):
                continue
            
            cb_set = cb_refl.fixedBindSetOrSpace
            cb_binding = cb_refl.fixedBindNumber
            
            if cb_set == target_set and cb_binding == target_binding:
                log("=== Found matching CB: Set={0}, Binding={1}, Name='{2}' ===".format(cb_set, cb_binding, cb_refl.name))
                
                # List all variables in this CB for debugging
                log("Variables in this CB:")
                for idx, var in enumerate(cb_refl.variables):
                    var_name_str = var.name if hasattr(var, 'name') else "<no name>"
                    # Try multiple attribute names for offset
                    var_offset_str = None
                    if hasattr(var, 'byteOffset'):
                        var_offset_str = var.byteOffset
                    elif hasattr(var, 'offset'):
                        var_offset_str = var.offset
                    elif hasattr(var, 'reg'):
                        var_offset_str = "reg.{}".format(var.reg.vec if hasattr(var.reg, 'vec') else '?')
                    else:
                        var_offset_str = "<no offset attribute>"
                    
                    # Debug: print all attributes
                    attrs = [a for a in dir(var) if not a.startswith('_')]
                    log("  [{0}] name='{1}', offset={2}, attrs={3}".format(idx, var_name_str, var_offset_str, attrs[:10]))
                
                # Search for the variable
                var_found = False
                var_offset = 0
                
                log("Searching for variable: '{0}'".format(variable_name))
                for var in cb_refl.variables:
                    if hasattr(var, 'name'):
                        if var.name == variable_name:
                            var_found = True
                            # Try multiple attribute names for offset
                            if hasattr(var, 'byteOffset'):
                                var_offset = var.byteOffset
                                log(">>> Found offset via 'byteOffset' attribute")
                            elif hasattr(var, 'offset'):
                                var_offset = var.offset
                                log(">>> Found offset via 'offset' attribute")
                            else:
                                log(">>> WARNING: No offset attribute found! Defaulting to 0")
                                log(">>> Available attributes: {0}".format([a for a in dir(var) if not a.startswith('_')]))
                            log(">>> MATCH FOUND! Variable '{0}' at offset {1}".format(variable_name, var_offset))
                            break
                
                if not var_found:
                    log(">>> ERROR: Variable '{0}' NOT FOUND in buffer {1}".format(variable_name, cb_refl.name))
                    log(">>> Available variables: {0}".format([v.name for v in cb_refl.variables if hasattr(v, 'name')]))
                    return None
                
                log(">>> Using Variable '{0}' at offset {1}".format(variable_name, var_offset))
                
                # Find matching runtime CB
                # Strategy 1: Try to match by index first (with bounds check)
                runtime_cb = None
                if cb_index < len(runtime_cbs):
                    runtime_cb = runtime_cbs[cb_index]
                    log("Using runtime CB at index {0}".format(cb_index))
                else:
                    log("Warning: CB index {0} out of range (runtime_cbs has {1} entries)".format(cb_index, len(runtime_cbs)))
                    
                    # Strategy 2: Try to match by name
                    log("Attempting to match by CB name: '{0}'".format(cb_refl.name))
                    for idx, rt_cb in enumerate(runtime_cbs):
                        # Try to get the shader reflection for this runtime CB
                        if hasattr(rt_cb, 'bindArraySize'):
                            log("  Runtime CB[{0}]: bindArraySize={1}".format(idx, rt_cb.bindArraySize))
                        # For now, try first available CB as fallback
                        if not runtime_cb and idx == 0:
                            runtime_cb = rt_cb
                            log("Using first runtime CB as fallback")
                            break
                
                if not runtime_cb:
                    log("Error: No runtime CB available")
                    return None
                
                if not hasattr(runtime_cb, 'descriptor'):
                    log("Error: Runtime CB has no descriptor")
                    return None
                
                desc = runtime_cb.descriptor
                if not hasattr(desc, 'resource'):
                    log("Error: Descriptor has no resource")
                    return None
                
                res_id = desc.resource
                buf_offset = desc.byteOffset if hasattr(desc, 'byteOffset') else 0
                buf_size = desc.byteSize if hasattr(desc, 'byteSize') else cb_refl.byteSize
                
                # Read buffer data
                buffer_data = controller.GetBufferData(res_id, buf_offset, buf_size)
                
                if len(buffer_data) < var_offset + 64:
                    log("Error: Not enough data in buffer")
                    return None
                
                # Extract matrix at variable offset
                matrix = struct.unpack('16f', buffer_data[var_offset:var_offset+64])
                
                log("Extracted matrix (before transpose):")
                log("  Last column: [{0:.5f}, {1:.5f}, {2:.5f}, {3:.5f}]".format(
                    matrix[3], matrix[7], matrix[11], matrix[15]))
                
                # Validate matrix: check if last column is (0, 0, 0, 1)
                if is_valid_transform_matrix(matrix):
                    log("✓ Matrix validation PASSED: Last column is (0, 0, 0, 1)")
                    return apply_transpose(matrix, transpose)
                else:
                    log("✗ Matrix validation FAILED: Last column is NOT (0, 0, 0, 1)")
                    
                    if auto_find:
                        log("Auto-find is ENABLED, searching for best matrix...")
                        result = auto_find_best_matrix(controller, log_func)
                        if result[0]:
                            log("✓ Auto-find succeeded! Using found matrix.")
                            log("   → Found at: Set={0}, Binding={1}, Variable='{2}'".format(
                                result[1], result[2], result[3]))
                            # Auto-find result is already transposed, need to handle based on transpose setting
                            if not transpose:
                                # If user doesn't want transpose, we need to transpose back
                                log("User disabled transpose, inverting auto-find transpose...")
                                return apply_transpose(result[0], True)  # Transpose back
                            return result[0]
                        else:
                            log("✗ Auto-find failed: No valid matrix found")
                            log("Falling back to original matrix")
                            return apply_transpose(matrix, transpose)
                    else:
                        log("Auto-find is DISABLED, using original matrix anyway")
                        return apply_transpose(matrix, transpose)
        
        log("No constant buffer found with Set={0}, Binding={1}".format(target_set, target_binding))
        
        # If CB not found and auto-find is enabled, try auto-find
        if auto_find:
            log("Auto-find is ENABLED, searching for best matrix...")
            result = auto_find_best_matrix(controller, log_func)
            if result[0]:
                log("✓ Auto-find succeeded!")
                return result[0]
        
        return None
        
    except Exception as e:
        log("Error reading matrix: {0}".format(str(e)))
        return None


def transform_vertices_with_matrix(data, matrix, mapper):
    """Apply 4x4 transform matrix to vertex data
    
    Args:
        data: Dictionary with vertex data, keys are actual attribute names (e.g., "_input0")
        matrix: 4x4 transformation matrix (16 floats)
        mapper: Dictionary mapping semantic names to actual attribute names
                e.g., {'POSITION': '_input0', 'NORMAL': '_input1'}
    """
    if not matrix:
        return data
    
    # Get actual attribute names from mapper
    position_key = mapper.get('POSITION', '')
    normal_key = mapper.get('NORMAL', '')
    tangent_key = mapper.get('TANGENT', '')
    binormal_key = mapper.get('BINORMAL', '')
    
    # Transform POSITION
    if position_key and position_key in data:
        transformed_positions = []
        for pos in data[position_key]:
            x, y, z = pos[:3] if len(pos) >= 3 else (pos[0] if len(pos) >= 1 else 0, pos[1] if len(pos) >= 2 else 0, 0)
            
            # Matrix multiplication: pos_world = Matrix × pos_local
            # Matrix is in row-major order: [m00,m01,m02,m03, m10,m11,m12,m13, ...]
            x_new = matrix[0]*x + matrix[1]*y + matrix[2]*z  + matrix[3]
            y_new = matrix[4]*x + matrix[5]*y + matrix[6]*z  + matrix[7]
            z_new = matrix[8]*x + matrix[9]*y + matrix[10]*z + matrix[11]
            
            transformed_positions.append((x_new, y_new, z_new))
        
        data[position_key] = transformed_positions
    
    # Transform NORMAL (rotation only, no translation)
    if normal_key and normal_key in data:
        transformed_normals = []
        for norm in data[normal_key]:
            x, y, z = norm[:3] if len(norm) >= 3 else (norm[0] if len(norm) >= 1 else 0, norm[1] if len(norm) >= 2 else 0, 0)
            
            # Use rotation part only (3x3 upper-left)
            x_new = matrix[0]*x + matrix[1]*y + matrix[2]*z
            y_new = matrix[4]*x + matrix[5]*y + matrix[6]*z
            z_new = matrix[8]*x + matrix[9]*y + matrix[10]*z
            
            # Normalize
            length = (x_new*x_new + y_new*y_new + z_new*z_new) ** 0.5
            if length > 0.0001:
                transformed_normals.append((x_new/length, y_new/length, z_new/length))
            else:
                transformed_normals.append(norm)
        
        data[normal_key] = transformed_normals
    
    # Transform TANGENT (rotation only)
    if tangent_key and tangent_key in data:
        transformed_tangents = []
        for tang in data[tangent_key]:
            x, y, z = tang[:3] if len(tang) >= 3 else (tang[0] if len(tang) >= 1 else 0, tang[1] if len(tang) >= 2 else 0, 0)
            
            x_new = matrix[0]*x + matrix[1]*y + matrix[2]*z
            y_new = matrix[4]*x + matrix[5]*y + matrix[6]*z
            z_new = matrix[8]*x + matrix[9]*y + matrix[10]*z
            
            length = (x_new*x_new + y_new*y_new + z_new*z_new) ** 0.5
            if length > 0.0001:
                transformed_tangents.append((x_new/length, y_new/length, z_new/length))
            else:
                transformed_tangents.append(tang)
        
        data[tangent_key] = transformed_tangents
    
    # Transform BINORMAL (rotation only)
    if binormal_key and binormal_key in data:
        transformed_binormals = []
        for binorm in data[binormal_key]:
            x, y, z = binorm[:3] if len(binorm) >= 3 else (binorm[0] if len(binorm) >= 1 else 0, binorm[1] if len(binorm) >= 2 else 0, 0)
            
            x_new = matrix[0]*x + matrix[1]*y + matrix[2]*z
            y_new = matrix[4]*x + matrix[5]*y + matrix[6]*z
            z_new = matrix[8]*x + matrix[9]*y + matrix[10]*z
            
            length = (x_new*x_new + y_new*y_new + z_new*z_new) ** 0.5
            if length > 0.0001:
                transformed_binormals.append((x_new/length, y_new/length, z_new/length))
            else:
                transformed_binormals.append(binorm)
        
        data[binormal_key] = transformed_binormals
    
    return data


# ==================== Batch Export Logic ====================

def export_draw_call(controller, draw, mapper, output_folder, log_window=None, draw_index=None, matrix_config=None):
    """Export a single draw call to FBX and textures
    
    Args:
        controller: RenderDoc controller
        draw: Draw call to export
        mapper: Attribute mapping dictionary
        output_folder: Output directory
        log_window: Optional log window for output
        draw_index: DEPRECATED - no longer used, eventId is used instead
        matrix_config: Optional dict with 'set', 'binding', 'variable' for real-time matrix reading
    """
    def log(msg):
        if log_window:
            log_window.log(msg)
        print(msg)
    
    try:
        log("[EventID {0}] Processing...".format(draw.eventId))
        
        # Move to that draw
        controller.SetFrameEvent(draw.eventId, True)
        
        # Get mesh inputs
        meshInputs = getMeshInputs(controller, draw)
        
        if not meshInputs:
            log("[EventID {0}] ✗ No mesh inputs found, skipping".format(draw.eventId))
            return False
        
        # Extract mesh data
        indices = getIndices(controller, meshInputs[0])
        log("[EventID {0}] Found {1} indices".format(draw.eventId, len(indices)))
        
        data = defaultdict(list)
        attr_list = set()
        
        # Store indices
        data["IDX"] = indices
        
        # ✅ 性能优化：批量读取顶点缓冲区（而不是逐个顶点读取）
        # 旧方法：每个顶点调用一次GetBufferData，1536个顶点×5个属性 = 7680次API调用
        # 新方法：每个属性只调用一次GetBufferData，5次API调用
        # 性能提升：约1000-1500倍！
        
        log("[EventID {0}] Reading vertex buffers...".format(draw.eventId))
        
        for attr in meshInputs:
            if not attr.format.Special():
                attr_list.add(attr.name)
                
                # 计算需要读取的缓冲区大小
                max_idx = max(indices) if indices else 0
                buffer_size = (max_idx + 1) * attr.vertexByteStride
                
                # ✅ 一次性读取整个缓冲区（关键优化）
                try:
                    full_buffer = controller.GetBufferData(
                        attr.vertexResourceId, 
                        attr.vertexByteOffset, 
                        buffer_size
                    )
                    
                    # 在内存中解析每个顶点（非常快）
                    for idx in indices:
                        offset_in_buffer = idx * attr.vertexByteStride
                        value = unpackData(attr.format, full_buffer[offset_in_buffer:])
                        data[attr.name].append(value)
                        
                except Exception as e:
                    log("[EventID {0}] Warning: Failed to read {1}: {2}".format(
                        draw.eventId, attr.name, str(e)))
                    # 如果批量读取失败，回退到逐个读取
                    for idx in indices:
                        try:
                            offset = attr.vertexByteOffset + attr.vertexByteStride * idx
                            buffer_data = controller.GetBufferData(attr.vertexResourceId, offset, 0)
                            value = unpackData(attr.format, buffer_data)
                            data[attr.name].append(value)
                        except:
                            pass
        
        log("[EventID {0}] Extracted attributes: {1}".format(draw.eventId, ", ".join(sorted(attr_list))))
        
        # Read and apply transform matrix in real-time if config provided
        if matrix_config:
            log("[EventID {0}] Reading transform matrix from RenderDoc...".format(draw.eventId))
            log("[EventID {0}]   Set: {1}, Binding: {2}, Variable: {3}".format(
                draw.eventId, matrix_config['set'], matrix_config['binding'], matrix_config['variable']))
            
            # Read matrix for this specific EventID
            auto_find = matrix_config.get('auto_find', False)  # 获取auto_find配置
            transpose = matrix_config.get('transpose', True)  # 获取transpose配置
            transform_matrix = read_matrix_from_renderdoc(
                controller,
                matrix_config['set'],
                matrix_config['binding'],
                matrix_config['variable'],
                log,
                auto_find,
                transpose
            )
            
            if transform_matrix:
                # 显示矩阵值
                log("[EventID {0}] Matrix: [{1:.2f}, {2:.2f}, {3:.2f}, {4:.2f}]".format(
                    draw.eventId, transform_matrix[0], transform_matrix[1], transform_matrix[2], transform_matrix[3]))
                log("[EventID {0}]         [{1:.2f}, {2:.2f}, {3:.2f}, {4:.2f}]".format(
                    draw.eventId, transform_matrix[4], transform_matrix[5], transform_matrix[6], transform_matrix[7]))
                log("[EventID {0}]         [{1:.2f}, {2:.2f}, {3:.2f}, {4:.2f}]".format(
                    draw.eventId, transform_matrix[8], transform_matrix[9], transform_matrix[10], transform_matrix[11]))
                log("[EventID {0}]         [{1:.2f}, {2:.2f}, {3:.2f}, {4:.2f}]".format(
                    draw.eventId, transform_matrix[12], transform_matrix[13], transform_matrix[14], transform_matrix[15]))
                
                # Get the actual position attribute name from mapper
                position_key = mapper.get('POSITION', '')
                log("[EventID {0}] Position attribute mapped to: '{1}'".format(draw.eventId, position_key))
                
                # 显示变换前的第一个顶点
                if position_key and position_key in data and len(data[position_key]) > 0:
                    first_pos = data[position_key][0]
                    log("[EventID {0}] First vertex before: ({1:.3f}, {2:.3f}, {3:.3f})".format(
                        draw.eventId, first_pos[0], first_pos[1], first_pos[2]))
                
                # Apply transformation
                data = transform_vertices_with_matrix(data, transform_matrix, mapper)
                
                # 显示变换后的第一个顶点
                if position_key and position_key in data and len(data[position_key]) > 0:
                    first_pos = data[position_key][0]
                    log("[EventID {0}] First vertex after:  ({1:.3f}, {2:.3f}, {3:.3f})".format(
                        draw.eventId, first_pos[0], first_pos[1], first_pos[2]))
                
                log("[EventID {0}] ✓ Transform applied".format(draw.eventId))
            else:
                log("[EventID {0}] ⚠ Failed to read matrix, using original coordinates".format(draw.eventId))
        else:
            log("[EventID {0}] ℹ No transform config provided (using original coordinates)".format(draw.eventId))
        
        # Export textures
        state = controller.GetPipelineState()
        texture_count = 0
        
        # Export fragment shader textures
        try:
            usedDescriptors = state.GetReadOnlyResources(rd.ShaderStage.Fragment)
            for usedDescriptor in usedDescriptors:
                res = usedDescriptor.descriptor.resource
                if saveTexture(res, draw.eventId, output_folder, controller):
                    texture_count += 1
        except:
            pass
        
        # Export render target textures
        try:
            for output in draw.outputs:
                if saveTexture(output, draw.eventId, output_folder, controller):
                    texture_count += 1
        except:
            pass
        
        if texture_count > 0:
            log("[EventID {0}] Exported {1} texture(s)".format(draw.eventId, texture_count))
        
        # Export FBX
        event_folder = os.path.join(output_folder, str(draw.eventId))
        if not os.path.exists(event_folder):
            os.makedirs(event_folder)
        
        # Use eventId for filename (e.g., 832.fbx)
        # This ensures FBX internal mesh name will be mesh_832
        fbx_filename = "{0}.fbx".format(draw.eventId)
        fbx_path = os.path.join(event_folder, fbx_filename)
        success = export_fbx(fbx_path, mapper, data, attr_list, controller)
        
        if success:
            log("[EventID {0}] ✓ Exported to: {1}".format(draw.eventId, fbx_path))
        else:
            log("[EventID {0}] ✗ Failed to export FBX".format(draw.eventId))
        
        return success
        
    except Exception as e:
        log("[EventID {0}] ✗ Error: {1}".format(draw.eventId, str(e)))
        import traceback
        log(traceback.format_exc())
        return False


def find_draws_in_range(controller, start_index, end_index):
    """Find all draw calls within the specified index range"""
    draws_in_range = []
    
    def recursive_search(draw):
        if start_index <= draw.eventId <= end_index:
            # Check if it's an actual draw call (has vertices)
            if draw.numIndices > 0 or draw.numInstances > 0:
                draws_in_range.append(draw)
        
        for child in draw.children:
            recursive_search(child)
    
    for root_draw in controller.GetRootActions():
        recursive_search(root_draw)
    
    return draws_in_range


# ==================== Extension Registration ====================

def error_log(func):
    """Decorator for error logging"""
    def wrapper(pyrenderdoc, data):
        manager = pyrenderdoc.Extensions()
        try:
            func(pyrenderdoc, data)
        except:
            import traceback
            manager.MessageDialog("Batch FBX Export Failed\n%s" % traceback.format_exc(), "Error")
    return wrapper


@error_log
def prepare_batch_export(pyrenderdoc, data):
    """Prepare and show the batch export dialog"""
    manager = pyrenderdoc.Extensions()
    
    mqt = manager.GetMiniQtHelper()
    dialog = BatchExportDialog(mqt, pyrenderdoc)
    
    # Show configuration dialog
    if not mqt.ShowWidgetAsDialog(dialog.init_ui()):
        return
    
    # Create log window
    log_window = ExportLogWindow(
        dialog.start_index,
        dialog.end_index,
        dialog.output_folder
    )
    
    # Create worker thread
    from .export_worker import ExportWorker
    
    # Get matrix config (may be None if widget failed to load or disabled)
    matrix_config = getattr(dialog, 'matrix_config', None)
    
    # Get export shader option
    export_shader = getattr(dialog, 'export_shader', False)
    
    worker = ExportWorker(
        pyrenderdoc,
        dialog.start_index,
        dialog.end_index,
        dialog.output_folder,
        dialog.mapper,
        find_draws_in_range,   # Pass function reference
        export_draw_call,      # Pass function reference
        matrix_config,          # Pass matrix config for real-time reading
        export_shader           # Pass shader export option
    )
    
    # Connect worker signals to log window
    worker.log_signal.connect(log_window.append_log)
    worker.progress_signal.connect(log_window.update_progress)
    worker.finished_signal.connect(log_window.on_export_finished)
    
    # Connect cancel button to worker with confirmation
    def cancel_export():
        reply = QtWidgets.QMessageBox.question(
            log_window, 
            'Cancel Export',
            'Are you sure you want to cancel the export?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )
        if reply == QtWidgets.QMessageBox.Yes:
            worker.cancel()
            log_window.on_cancel()
    
    log_window.cancel_button.clicked.disconnect()
    log_window.cancel_button.clicked.connect(cancel_export)
    
    # Show window (non-modal)
    log_window.show()
    log_window.raise_()
    log_window.activateWindow()
    log_window.setWindowState(log_window.windowState() & ~QtCore.Qt.WindowMinimized | QtCore.Qt.WindowActive)
    
    log_window.append_log("Initializing export process...")
    log_window.append_log("Starting background worker thread...")
    
    # Start worker thread (non-blocking!)
    worker.start()
    
    log_window.append_log("✓ Worker thread started successfully")
    log_window.append_log("UI will remain responsive during export\n")


# ============================================================================
# Shader Export Functions (Integrated from RenderDocShaderExporter)
# ============================================================================

def get_shader_spirv(controller, shader_refl):
    """Get SPIR-V bytecode from shader reflection"""
    try:
        if not shader_refl or not shader_refl.resourceId:
            return None
        
        # Primary method: Get from shader reflection rawBytes
        # This is where Vulkan SPIR-V is stored
        if hasattr(shader_refl, 'rawBytes') and shader_refl.rawBytes:
            try:
                bytes_data = bytes(shader_refl.rawBytes)
                if len(bytes_data) >= 4:
                    magic = struct.unpack('I', bytes_data[:4])[0]
                    if magic == 0x07230203:  # SPIR-V magic number
                        return bytes_data
            except:
                pass
        
        return None
        
    except Exception as e:
        return None


def extract_shader_info(state, vs_refl, ps_refl):
    """Extract shader information for Unity Shader generation"""
    info = {
        "vertex_inputs": [],
        "constant_buffers": [],
        "textures": [],
        "samplers": []
    }
    
    try:
        # Extract vertex inputs
        if hasattr(vs_refl, 'inputSignature'):
            for inp in vs_refl.inputSignature:
                input_info = {
                    "name": inp.semanticName if hasattr(inp, 'semanticName') else "unknown",
                    "semantic": inp.semanticName if hasattr(inp, 'semanticName') else "unknown",
                    "semantic_index": inp.semanticIndex if hasattr(inp, 'semanticIndex') else 0
                }
                # Safely get format/type info
                if hasattr(inp, 'compType'):
                    input_info["format"] = str(inp.compType)
                elif hasattr(inp, 'varType'):
                    input_info["format"] = str(inp.varType)
                else:
                    input_info["format"] = "unknown"
                
                info["vertex_inputs"].append(input_info)
    except Exception as e:
        pass
    
    try:
        # Extract constant buffers (VS and PS)
        all_cbs = []
        if hasattr(vs_refl, 'constantBlocks'):
            all_cbs.extend(vs_refl.constantBlocks)
        if hasattr(ps_refl, 'constantBlocks'):
            all_cbs.extend(ps_refl.constantBlocks)
        
        for cb in all_cbs:
            if hasattr(cb, 'bufferBacked') and cb.bufferBacked:
                cb_info = {
                    "name": cb.name if hasattr(cb, 'name') else "unknown",
                    "size": cb.byteSize if hasattr(cb, 'byteSize') else 0,
                    "variables": []
                }
                if hasattr(cb, 'variables'):
                    for var in cb.variables:
                        cb_info["variables"].append({
                            "name": var.name if hasattr(var, 'name') else "unknown",
                            "offset": var.offset if hasattr(var, 'offset') else 0
                        })
                info["constant_buffers"].append(cb_info)
    except Exception as e:
        pass
    
    try:
        # Extract textures
        if hasattr(ps_refl, 'readOnlyResources'):
            for tex in ps_refl.readOnlyResources:
                if hasattr(tex, 'isTexture') and tex.isTexture:
                    tex_info = {
                        "name": tex.name if hasattr(tex, 'name') else "unknown",
                        "binding": tex.bindPoint if hasattr(tex, 'bindPoint') else 0
                    }
                    # Add resourceId for texture file matching
                    if hasattr(tex, 'resourceId'):
                        tex_info["resourceId"] = str(tex.resourceId)
                    info["textures"].append(tex_info)
    except Exception as e:
        pass
    
    try:
        # Extract samplers
        if hasattr(ps_refl, 'samplers'):
            for samp in ps_refl.samplers:
                info["samplers"].append({
                    "name": samp.name if hasattr(samp, 'name') else "unknown",
                    "binding": samp.bindPoint if hasattr(samp, 'bindPoint') else 0
                })
    except Exception as e:
        pass
    
    return info


def find_spirv_cross():
    """Find spirv-cross executable"""
    import subprocess
    
    # Check in PATH
    try:
        result = subprocess.run(['spirv-cross', '--version'], 
                              stdout=subprocess.PIPE, 
                              stderr=subprocess.PIPE)
        if result.returncode == 0:
            return 'spirv-cross'
    except:
        pass
    
    # Check in same directory as plugin
    plugin_dir = os.path.dirname(__file__)
    local_spirv = os.path.join(plugin_dir, 'spirv-cross.exe')
    if os.path.exists(local_spirv):
        return local_spirv
    
    # Check in tools folder
    tools_spirv = os.path.join(plugin_dir, 'tools', 'spirv-cross.exe')
    if os.path.exists(tools_spirv):
        return tools_spirv
    
    return None


def convert_spirv_to_hlsl(spirv_cross_exe, spv_path, hlsl_path):
    """Convert SPIR-V to HLSL using spirv-cross"""
    import subprocess
    
    cmd = [
        spirv_cross_exe,
        spv_path,
        '--output', hlsl_path,
        '--hlsl',
        '--shader-model', '50',
        '--hlsl-enable-compat'
    ]
    
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        error_msg = result.stderr.decode('utf-8') if result.stderr else "Unknown error"
        raise Exception("spirv-cross failed: {}".format(error_msg))


def generate_properties(shader_info):
    """Generate Unity Shader Properties from shader info"""
    properties = []
    
    # Add textures as properties
    for i, tex in enumerate(shader_info.get("textures", [])):
        tex_name = tex["name"]
        # Clean up name
        clean_name = tex_name.replace(".", "_")
        properties.append('        {} ("Texture {}", 2D) = "white" {{}}'.format(clean_name, i))
    
    # Add default properties if no textures
    if not properties:
        properties.append('        _MainTex ("Texture", 2D) = "white" {}')
    
    return "\n".join(properties)


def adapt_hlsl_to_unity(hlsl_code, prefix=""):
    """Adapt HLSL code to Unity shader syntax - Rename cbuffer AND variables
    
    Unity HLSL lifts cbuffer variables to global scope even with original cbuffer syntax.
    We must rename BOTH the cbuffer AND its variables to avoid conflicts.
    
    Args:
        hlsl_code: HLSL code string
        prefix: Prefix to add (e.g., "VS_" or "PS_")
    """
    import re
    
    # First pass: collect all cbuffer names and their variable mappings
    cbuffer_info = {}  # {original_cbuffer_name: {old_var: new_var}}
    current_cbuffer = None
    lines = hlsl_code.split('\n')
    
    for line in lines:
        stripped = line.strip()
        
        # Detect cbuffer start
        if 'cbuffer' in stripped and ':' in stripped and 'register' in stripped:
            match = re.search(r'cbuffer\s+(\w+)', line)
            if match:
                current_cbuffer = match.group(1)
                cbuffer_info[current_cbuffer] = {}
                continue
        
        # Detect cbuffer end
        if current_cbuffer and (stripped == '};' or stripped == '}'):
            current_cbuffer = None
            continue
        
        # Inside cbuffer: collect variable names
        if current_cbuffer:
            # Match variable declarations: type varname; or type varname[size];
            var_match = re.search(r'^\s*(\w+(?:<[^>]+>)?)\s+(\w+)(\[[^\]]*\])?\s*;', line)
            if var_match:
                var_type = var_match.group(1)
                var_name = var_match.group(2)
                var_array = var_match.group(3) if var_match.group(3) else ""
                # Create new variable name with prefix
                new_var_name = prefix + var_name
                cbuffer_info[current_cbuffer][var_name] = new_var_name
    
    # Second pass: replace cbuffer names and variable names
    adapted = []
    current_cbuffer = None
    in_cbuffer = False
    
    for line in lines:
        stripped = line.strip()
        
        # Replace cbuffer declaration
        if 'cbuffer' in stripped and ':' in stripped and 'register' in stripped:
            match = re.search(r'cbuffer\s+(\w+)', line)
            if match:
                original_name = match.group(1)
                prefixed_name = prefix + original_name
                line = line.replace('cbuffer ' + original_name, 'cbuffer ' + prefixed_name)
                current_cbuffer = original_name
                in_cbuffer = True
                adapted.append(line)
                continue
        
        # Keep opening brace
        if in_cbuffer and stripped == '{':
            adapted.append(line)
            continue
        
        # Detect cbuffer end
        if in_cbuffer and (stripped == '};' or stripped == '}'):
            adapted.append(line)
            in_cbuffer = False
            current_cbuffer = None
            continue
        
        # Inside cbuffer: rename variables and remove packoffset
        if in_cbuffer and current_cbuffer:
            # Remove packoffset first
            if ': packoffset' in line:
                line = re.sub(r'\s*:\s*packoffset\([^)]+\)', '', line)
            
            # Replace variable names
            var_match = re.search(r'^\s*(\w+(?:<[^>]+>)?)\s+(\w+)(\[[^\]]*\])?\s*;', line)
            if var_match and current_cbuffer in cbuffer_info:
                var_type = var_match.group(1)
                old_var_name = var_match.group(2)
                var_array = var_match.group(3) if var_match.group(3) else ""
                
                if old_var_name in cbuffer_info[current_cbuffer]:
                    new_var_name = cbuffer_info[current_cbuffer][old_var_name]
                    # Replace variable name (word boundary to avoid partial matches)
                    line = re.sub(r'\b' + re.escape(old_var_name) + r'\b', new_var_name, line)
            
            adapted.append(line)
            continue
        
        # Outside cbuffer: replace variable references
        if not in_cbuffer:
            # Replace all variable references that were in cbuffers
            for cb_name, var_map in cbuffer_info.items():
                for old_var, new_var in var_map.items():
                    # Use word boundary to avoid partial matches
                    line = re.sub(r'\b' + re.escape(old_var) + r'\b', new_var, line)
        
        adapted.append(line)
    
    return '\n'.join(adapted)


def find_hlsl_entry_point(hlsl_code):
    """Find the entry point function name in HLSL code
    
    Looks for functions with SV_Position (VS) or SV_Target (PS) output
    Returns the function name or 'main' as default
    """
    import re
    
    # Look for function with SV_Position or SV_Target in return type
    # Pattern: FunctionName identifier(...) or struct FunctionName identifier(...)
    lines = hlsl_code.split('\n')
    
    for i, line in enumerate(lines):
        # Check if this line or nearby lines have SV_Position or SV_Target
        context = '\n'.join(lines[max(0, i-5):min(len(lines), i+10)])
        
        if 'SV_Position' in context or 'SV_Target' in context:
            # Look for function definition: Type FunctionName(...)
            match = re.search(r'\b(\w+)\s*\(.*?\)\s*$', line)
            if match:
                func_name = match.group(1)
                # Exclude common keywords
                if func_name not in ['if', 'for', 'while', 'switch', 'return']:
                    return func_name
    
    # Fallback: look for "main" function
    if re.search(r'\bmain\s*\(', hlsl_code):
        return 'main'
    
    # Default fallback
    return 'main'


def generate_hlsl_code(event_id, vs_entry, ps_entry):
    """Generate Unity HLSL code with direct includes
    
    Simply includes the complete HLSL files with actual entry points.
    """
    
    code = []
    code.append("            // ===============================================")
    code.append("            // Direct HLSL Include Method")
    code.append("            // ===============================================")
    code.append("            // Complete HLSL code from SPIR-V conversion")
    code.append("            // Entry points: VS={}, PS={}".format(vs_entry, ps_entry))
    code.append("            // ===============================================")
    code.append("")
    code.append("            #include \"UnityCG.cginc\"")
    code.append("")
    code.append("            // Include complete vertex shader")
    code.append("            #include \"{}_vs.hlsl\"".format(event_id))
    code.append("")
    code.append("            // Include complete fragment/pixel shader")
    code.append("            #include \"{}_ps.hlsl\"".format(event_id))
    
    return "\n".join(code)


def generate_unity_shader(output_path, vs_hlsl_path, ps_hlsl_path, shader_info, event_id):
    """Generate Unity Shader with direct HLSL includes"""
    
    import os
    import re
    
    # Read HLSL code
    with open(vs_hlsl_path, 'r', encoding='utf-8') as f:
        vs_hlsl = f.read()
    with open(ps_hlsl_path, 'r', encoding='utf-8') as f:
        ps_hlsl = f.read()
    
    # Find entry points in the HLSL code
    vs_entry = find_hlsl_entry_point(vs_hlsl)
    ps_entry = find_hlsl_entry_point(ps_hlsl)
    
    print("  ├─ VS Entry Point: {}".format(vs_entry))
    print("  ├─ PS Entry Point: {}".format(ps_entry))
    
    # Generate Properties
    properties = generate_properties(shader_info)
    
    # Generate main shader code with direct includes
    hlsl_code = generate_hlsl_code(event_id, vs_entry, ps_entry)
    
    # Save complete HLSL files (for #include) - no modifications, just copy
    output_dir = os.path.dirname(output_path)
    vs_include_path = os.path.join(output_dir, "{}_vs.hlsl".format(event_id))
    ps_include_path = os.path.join(output_dir, "{}_ps.hlsl".format(event_id))
    
    # Remove packoffset from HLSL (Unity doesn't need it)
    vs_hlsl_clean = re.sub(r'\s*:\s*packoffset\([^)]+\)', '', vs_hlsl)
    ps_hlsl_clean = re.sub(r'\s*:\s*packoffset\([^)]+\)', '', ps_hlsl)
    
    with open(vs_include_path, 'w', encoding='utf-8') as f:
        f.write("// Vertex Shader (Complete HLSL from SPIR-V)\n\n")
        f.write(vs_hlsl_clean)
    
    with open(ps_include_path, 'w', encoding='utf-8') as f:
        f.write("// Fragment/Pixel Shader (Complete HLSL from SPIR-V)\n\n")
        f.write(ps_hlsl_clean)
    
    # Unity Shader template with actual entry points
    shader_template = """Shader "RenderDoc/Event{event_id}"
{{
    Properties
    {{
{properties}
    }}
    
    SubShader
    {{
        Tags {{ "RenderType"="Opaque" }}
        LOD 100
        
        Pass
        {{
            HLSLPROGRAM
            #pragma vertex {vs_entry}
            #pragma fragment {ps_entry}
            #pragma target 3.5
            
{hlsl_code}
            
            ENDHLSL
        }}
    }}
    
    FallBack "Diffuse"
}}
""".format(event_id=event_id, properties=properties, hlsl_code=hlsl_code, 
           vs_entry=vs_entry, ps_entry=ps_entry)
    
    # Write to file
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(shader_template)
    
    print("  ├─ Unity Shader: {}".format(os.path.basename(output_path)))
    print("  ├─ VS Include: {}_vs.hlsl".format(event_id))
    print("  └─ PS Include: {}_ps.hlsl".format(event_id))


def export_single_event_shader(controller, event_id, output_dir):
    """Export shader for a single event (called during FBX export loop)
    
    Returns: (success, shader_info_dict) tuple
    """
    import json
    
    try:
        # Set to current event
        controller.SetFrameEvent(event_id, True)
        
        # Get pipeline state
        state = controller.GetPipelineState()
        
        # Check if Vulkan
        if not state.IsCaptureVK():
            return (False, None)
        
        # Get shader reflections
        vs_refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
        ps_refl = state.GetShaderReflection(rd.ShaderStage.Fragment)
        if not ps_refl:
            ps_refl = state.GetShaderReflection(rd.ShaderStage.Pixel)
        
        if not vs_refl or not ps_refl:
            return (False, None)
        
        # Get SPIR-V bytecode
        vs_spirv = get_shader_spirv(controller, vs_refl)
        ps_spirv = get_shader_spirv(controller, ps_refl)
        
        if not vs_spirv or not ps_spirv:
            return (False, None)
        
        # Create output directory if needed
        os.makedirs(output_dir, exist_ok=True)
        
        # Save SPIR-V files (.spv)
        vs_spv_path = os.path.join(output_dir, '{}_vs.spv'.format(event_id))
        ps_spv_path = os.path.join(output_dir, '{}_ps.spv'.format(event_id))
        
        with open(vs_spv_path, 'wb') as f:
            f.write(vs_spirv)
        with open(ps_spv_path, 'wb') as f:
            f.write(ps_spirv)
        
        # Find spirv-cross
        spirv_cross_exe = find_spirv_cross()
        if not spirv_cross_exe:
            print("WARNING: spirv-cross.exe not found! Only .spv files saved.")
            print("To generate Unity Shader, please:")
            print("  1. Download spirv-cross.exe from Vulkan SDK")
            print("  2. Place it in: {}".format(os.path.dirname(__file__)))
            print("  3. Or add it to system PATH")
            return (False, None)  # Return False to indicate incomplete export
        
        # Convert SPIR-V to HLSL with auto-binding
        # Command: spirv-cross shader.spv --hlsl --hlsl-auto-binding --output shader.hlsl
        vs_hlsl_path = os.path.join(output_dir, '{}_vs.hlsl'.format(event_id))
        ps_hlsl_path = os.path.join(output_dir, '{}_ps.hlsl'.format(event_id))
        
        # Run spirv-cross for VS and PS
        import subprocess
        try:
            print("  ├─ Converting VS: spirv-cross --hlsl --hlsl-auto-binding")
            subprocess.run([spirv_cross_exe, vs_spv_path, '--hlsl', '--hlsl-auto-binding', '--output', vs_hlsl_path],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            
            print("  ├─ Converting PS: spirv-cross --hlsl --hlsl-auto-binding")
            subprocess.run([spirv_cross_exe, ps_spv_path, '--hlsl', '--hlsl-auto-binding', '--output', ps_hlsl_path],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as e:
            print("ERROR: spirv-cross failed: {}".format(str(e)))
            return (False, None)
        
        # Extract shader info
        shader_info = extract_shader_info(state, vs_refl, ps_refl)
        
        # Generate Unity Shader
        unity_shader_path = os.path.join(output_dir, '{}.shader'.format(event_id))
        generate_unity_shader(unity_shader_path, vs_hlsl_path, ps_hlsl_path, shader_info, event_id)
        
        # Save shader_info.json for Unity material generation
        shader_info_path = os.path.join(output_dir, 'shader_info.json')
        with open(shader_info_path, 'w', encoding='utf-8') as f:
            json.dump(shader_info, f, indent=2)
        
        print("  └─ Export complete!")
        return (True, shader_info)
        
    except Exception as e:
        print("Warning: Shader export failed for EventID {}: {}".format(event_id, str(e)))
        import traceback
        traceback.print_exc()
        return (False, None)


# ============================================================================


def register(version, pyrenderdoc):
    """Register the extension with RenderDoc"""
    print("Registering Batch FBX Exporter extension for RenderDoc {}".format(version))
    pyrenderdoc.Extensions().RegisterWindowMenu(
        qrenderdoc.WindowMenu.Window, 
        ["Batch FBX Exporter"], 
        prepare_batch_export
    )


def unregister():
    """Unregister the extension"""
    print("Unregistering Batch FBX Exporter extension")

