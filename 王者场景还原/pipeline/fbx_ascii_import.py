# -*- coding: utf-8 -*-
"""
Minimal ASCII-FBX reader for the meshes produced by
batch_fbx_exporter_ExtraUV / headless_carbody_export.py.

Blender's bundled FBX addon refuses ASCII FBX outright
("ASCII FBX files are not supported"), so we parse the handful of blocks
our own exporter writes and build the mesh with the bpy API directly.
Nothing is lost: because these meshes come straight out of a GPU index
buffer, every unique index already carries exactly one position / normal /
UV set, so a per-vertex read is bit-exact and immune to face reordering.

Blocks understood:
    Vertices            *N  float3 per unique index
    PolygonVertexIndex  *N  triangles, last index of each tri is ~i
    LayerElementNormal      Normals, ByPolygonVertex / Direct
    LayerElementUV: <n>     UV (IndexToDirect) + UVIndex, Name: "mapN"
    LayerElementColor       Colors + ColorIndex
    Model: ..., "Model::<name>", "Mesh"

Public API:
    load_ascii_fbx(path, name=None) -> bpy.types.Object | None
"""

import os
import re

import bpy

# "Name: *Count { a: v0,v1,... }" - the payload never contains a brace,
# so a non-greedy stop at the first '}' is safe.
_ARRAY_RE = r'{0}:\s*\*(\d+)\s*\{{\s*a:\s*([^}}]*)\}}'
_MODEL_RE = re.compile(r'Model:\s*[-\d]+,\s*"Model::([^"]*)"')


# ---------------------------------------------------------------- parsing

def _find_array(text, key, start=0):
    """Return (list_of_str_tokens, end_offset) for the first `key` array."""
    m = re.compile(_ARRAY_RE.format(key)).search(text, start)
    if not m:
        return None, start
    raw = m.group(2)
    toks = [t for t in raw.replace('\n', ' ').split(',') if t.strip()]
    return toks, m.end()


def _floats(text, key, start=0):
    toks, end = _find_array(text, key, start)
    if toks is None:
        return None, start
    return [float(t) for t in toks], end


def _ints(text, key, start=0):
    toks, end = _find_array(text, key, start)
    if toks is None:
        return None, start
    return [int(t) for t in toks], end


def parse_ascii_fbx(path):
    """Parse one exporter-generated ASCII FBX into plain python data."""
    with open(path, 'r') as f:
        text = f.read()

    if 'FBX 7' not in text.split('\n', 1)[0] and 'Objects:' not in text:
        raise ValueError('not an exporter ASCII FBX: %s' % path)

    flat, _ = _floats(text, 'Vertices')
    if not flat:
        raise ValueError('no Vertices block in %s' % path)
    verts = [(flat[i], flat[i + 1], flat[i + 2]) for i in range(0, len(flat) - 2, 3)]

    pvi, _ = _ints(text, 'PolygonVertexIndex')
    if not pvi:
        raise ValueError('no PolygonVertexIndex block in %s' % path)
    # every third entry is stored as ~i (one's complement) to close the polygon
    pvi = [(~i if i < 0 else i) for i in pvi]

    normals, _ = _floats(text, 'Normals')

    # ---- UV layers: one per "LayerElementUV: <typedIndex>" block
    uv_layers = []
    for m in re.finditer(r'LayerElementUV:\s*(\d+)\s*\{', text):
        seg_start = m.end()
        nm = re.compile(r'Name:\s*"([^"]*)"').search(text, seg_start)
        layer_name = nm.group(1) if nm else 'map%d' % (int(m.group(1)) + 1)
        uvs, after = _floats(text, 'UV', seg_start)
        if not uvs:
            continue
        uvidx, _ = _ints(text, 'UVIndex', seg_start)
        uv_layers.append({
            'typed': int(m.group(1)),
            'name': layer_name,
            'uv': uvs,
            'index': uvidx,
        })
    uv_layers.sort(key=lambda d: d['typed'])

    colors, _ = _floats(text, 'Colors')

    mm = _MODEL_RE.search(text)
    model_name = mm.group(1) if mm else os.path.splitext(os.path.basename(path))[0]

    return {
        'name': model_name,
        'verts': verts,
        'pvi': pvi,
        'normals': normals,
        'uv_layers': uv_layers,
        'colors': colors,
    }


# ---------------------------------------------------------------- building

def _triangles(pvi, nverts):
    """Split the flat index list into valid triangles."""
    faces = []
    dropped = 0
    for i in range(0, len(pvi) - 2, 3):
        a, b, c = pvi[i], pvi[i + 1], pvi[i + 2]
        if a >= nverts or b >= nverts or c >= nverts or min(a, b, c) < 0:
            dropped += 1
            continue
        if a == b or b == c or a == c:          # degenerate tri from the GPU buffer
            dropped += 1
            continue
        faces.append((a, b, c))
    return faces, dropped


def _per_vertex(values, comps, pvi, nverts):
    """Fold a ByPolygonVertex/Direct stream down to one value per vertex.

    Legitimate here (and lossless) because each unique GPU index owns exactly
    one normal / tangent - the mesh is already split at every hard edge.
    """
    out = [None] * nverts
    n = len(pvi)
    for i in range(n):
        v = pvi[i]
        if v < 0 or v >= nverts or out[v] is not None:
            continue
        o = i * comps
        if o + comps <= len(values):
            out[v] = tuple(values[o:o + comps])
    return out


def load_ascii_fbx(path, name=None):
    """Parse + build. Returns the created object, or None on failure."""
    data = parse_ascii_fbx(path)
    verts = data['verts']
    nverts = len(verts)

    faces, dropped = _triangles(data['pvi'], nverts)
    if not faces:
        print('[fbxascii] no usable triangles in %s' % os.path.basename(path))
        return None

    obj_name = name or data['name'] or 'mesh'
    me = bpy.data.meshes.new(obj_name)
    me.from_pydata(verts, [], faces)
    me.validate(verbose=False)
    me.update()

    loops = me.loops
    nloops = len(loops)

    # ---- UV sets (UV array is indexed by vertex index -> reorder-proof)
    for layer in data['uv_layers']:
        uvs = layer['uv']
        try:
            ul = me.uv_layers.new(name=layer['name'], do_init=False)
        except TypeError:
            ul = me.uv_layers.new(name=layer['name'])
        flat = [0.0] * (nloops * 2)
        for li in range(nloops):
            v = loops[li].vertex_index
            o = v * 2
            if o + 1 < len(uvs):
                flat[li * 2] = uvs[o]
                flat[li * 2 + 1] = uvs[o + 1]
        ul.data.foreach_set('uv', flat)

    # ---- vertex colours
    if data['colors']:
        cols = data['colors']
        # Colors stream is ByPolygonVertex; fold to per-vertex like normals
        per_v = _per_vertex(cols, 4, data['pvi'], nverts)
        try:
            cl = me.color_attributes.new(name='colorSet1', type='FLOAT_COLOR',
                                         domain='CORNER')
            flat = [0.0] * (nloops * 4)
            for li in range(nloops):
                c = per_v[loops[li].vertex_index]
                if c:
                    flat[li * 4:li * 4 + 4] = list(c[:4])
                else:
                    flat[li * 4:li * 4 + 4] = [1.0, 1.0, 1.0, 1.0]
            cl.data.foreach_set('color', flat)
        except Exception as e:
            print('[fbxascii] vertex colour skipped: %s' % e)

    # ---- custom split normals
    if data['normals']:
        per_v = _per_vertex(data['normals'], 3, data['pvi'], nverts)
        loop_normals = []
        ok = True
        for li in range(nloops):
            nrm = per_v[loops[li].vertex_index]
            if nrm is None:
                ok = False
                break
            loop_normals.append(nrm)
        if ok:
            try:
                for p in me.polygons:
                    p.use_smooth = True
                me.normals_split_custom_set(loop_normals)
            except Exception as e:
                print('[fbxascii] custom normals skipped: %s' % e)
    else:
        for p in me.polygons:
            p.use_smooth = True

    obj = bpy.data.objects.new(obj_name, me)
    print('[fbxascii] %s: %d verts, %d tris (%d dropped), %d uv set(s)' % (
        os.path.basename(path), nverts, len(me.polygons), dropped,
        len(data['uv_layers'])))
    return obj
