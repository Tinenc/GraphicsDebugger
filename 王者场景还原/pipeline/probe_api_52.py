"""Probe Blender 5.2 API surface used by the libai_scene pipeline.

Run headless:
  blender.exe --background --python probe_api_52.py
Writes the report to out/api_probe_52.txt (and stdout, which may be swallowed).
"""
import os
import sys

import bpy

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)
REPORT = os.path.join(OUT_DIR, "api_probe_52.txt")

lines = []


def log(msg):
    lines.append(str(msg))
    print(str(msg))


log("blender version = {0}".format(bpy.app.version_string))
log("version tuple   = {0}".format(bpy.app.version))
log("")

# --- render engines -------------------------------------------------------
rs = bpy.context.scene.render
engines = list(rs.bl_rna.properties['engine'].enum_items.keys())
log("available engines: {0}".format(engines))
log("current engine   : {0}".format(rs.engine))
log("")

# --- mesh APIs ------------------------------------------------------------
me = bpy.data.meshes.new("probe")
me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
me.validate()

checks = [
    ("mesh.normals_split_custom_set", hasattr(me, "normals_split_custom_set")),
    ("mesh.color_attributes", hasattr(me, "color_attributes")),
    ("mesh.uv_layers.new", hasattr(me.uv_layers, "new")),
    ("mesh.use_auto_smooth", hasattr(me, "use_auto_smooth")),
    ("mesh.shade_smooth", hasattr(me, "shade_smooth")),
    ("mesh.attributes", hasattr(me, "attributes")),
]
for name, ok in checks:
    log("{0:<34} {1}".format(name, "OK" if ok else "GONE"))

# actually exercise custom normals
try:
    me.normals_split_custom_set([(0.0, 0.0, 1.0)] * len(me.loops))
    log("{0:<34} {1}".format("normals_split_custom_set() call", "OK"))
except Exception as exc:
    log("{0:<34} FAILED: {1}".format("normals_split_custom_set() call", exc))

# vertex colour attribute
try:
    ca = me.color_attributes.new(name='colorSet1', type='FLOAT_COLOR',
                                 domain='CORNER')
    log("{0:<34} OK (name={1})".format("color_attributes.new()", ca.name))
except Exception as exc:
    log("{0:<34} FAILED: {1}".format("color_attributes.new()", exc))

# multiple uv layers
try:
    for nm in ("map1", "map2", "map3"):
        me.uv_layers.new(name=nm)
    log("{0:<34} OK ({1})".format("uv_layers.new() x3",
                                  [l.name for l in me.uv_layers]))
except Exception as exc:
    log("{0:<34} FAILED: {1}".format("uv_layers.new() x3", exc))
log("")

# --- material APIs --------------------------------------------------------
mat = bpy.data.materials.new("probe_mat")
mat.use_nodes = True
mat_checks = [
    ("material.blend_method", hasattr(mat, "blend_method")),
    ("material.shadow_method", hasattr(mat, "shadow_method")),
    ("material.surface_render_method", hasattr(mat, "surface_render_method")),
    ("material.use_transparent_shadow", hasattr(mat, "use_transparent_shadow")),
]
for name, ok in mat_checks:
    log("{0:<34} {1}".format(name, "OK" if ok else "GONE"))

if hasattr(mat, "blend_method"):
    log("  blend_method enum : {0}".format(
        list(mat.bl_rna.properties['blend_method'].enum_items.keys())))
if hasattr(mat, "surface_render_method"):
    log("  surface_render_method enum : {0}".format(
        list(mat.bl_rna.properties['surface_render_method'].enum_items.keys())))

bsdf = mat.node_tree.nodes.get("Principled BSDF")
if bsdf:
    names = [s.name for s in bsdf.inputs]
    log("  Principled inputs : {0}".format(names))
    for want in ("Base Color", "Normal", "Alpha", "Emission Color",
                 "Emission Strength", "Metallic", "Roughness",
                 "Specular IOR Level"):
        log("    {0:<22} {1}".format(want, "OK" if want in names else "MISSING"))
else:
    log("  Principled BSDF node NOT FOUND")
log("")

# --- light / camera -------------------------------------------------------
lt = bpy.data.lights.new("probe_light", type='SUN')
log("{0:<34} {1}".format("light.energy", hasattr(lt, "energy")))
log("{0:<34} {1}".format("light.angle(SUN)", hasattr(lt, "angle")))
area = bpy.data.lights.new("probe_area", type='AREA')
log("{0:<34} {1}".format("area.size / shape",
                         hasattr(area, "size") and hasattr(area, "shape")))
cam = bpy.data.cameras.new("probe_cam")
log("{0:<34} {1}".format("camera.lens / sensor_width",
                         hasattr(cam, "lens") and hasattr(cam, "sensor_width")))
log("")

# --- collections / view layer -------------------------------------------
col = bpy.data.collections.new("probe_col")
bpy.context.scene.collection.children.link(col)
log("{0:<34} {1}".format("collection.color_tag", hasattr(col, "color_tag")))
lc = None
for child in bpy.context.view_layer.layer_collection.children:
    if child.name == "probe_col":
        lc = child
log("{0:<34} {1}".format("layer_collection found", lc is not None))
if lc is not None:
    log("{0:<34} {1}".format("  hide_viewport/hide_render",
                             hasattr(lc, "hide_viewport") and
                             hasattr(lc, "exclude")))

# --- FBX importer (should still refuse ASCII) ---------------------------
log("")
log("{0:<34} {1}".format("bpy.ops.import_scene.fbx",
                         hasattr(bpy.ops.import_scene, "fbx")))

with open(REPORT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print("report -> " + REPORT)
