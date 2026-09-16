# -*- coding: utf-8 -*-
"""
Rebuild the 李白谪仙 (Li Bai / Zhexian skin) showcase frame inside Blender.

Run either way:
    blender.exe --python blender_rebuild.py
    # or paste/exec through the BlenderMCP execute_code bridge

Inputs (produced by the earlier steps of this pipeline):
    scene_export/scene.json            per-draw mapper + transform + fbx name
    scene_export/textures_manifest.json  per-texture role classification
    scene_export/<group>/*.fbx          ASCII FBX from batch_fbx_exporter_ExtraUV

=============================  COORDINATE SPACE  =============================
The exported vertices are in the capture's VIEW (camera) space, because
export_scene.py baked the vertex shader's model-view matrix (cb0._child1) into
them. The projection matrix read from EID 260 is, column-major:

    col0 = ( 2.101,  0,      0,        0     )
    col1 = ( 0,      3.736,  0,        0     )
    col2 = ( 0,      0,      1.000,    1.000 )
    col3 = (-0.006, -0.949, -100.171, -100.061)

so  w_clip = z - 100.061.  Consequences:

  1. The eye sits at z = +100.061 and looks towards +Z. It is NOT at the
     origin looking down -Z. (The hero geometry sits at z ~ 106, i.e. ~6
     units in front of the eye - which matches a portrait framing.)
  2. fovX = 2*atan(1/2.101) = 50.91 deg, fovY = 2*atan(1/3.736) = 29.97 deg,
     aspect = 3.736/2.101 = 1.7782 ~= 1920/1080. Good.
  3. View-space +Y is the character's UP.

About (3) - this cost two wrong iterations, so it is worth spelling out fully.

RenderDoc's SaveTexture writes the Vulkan image in raw row order, so the
reference PNG (scene_export/_reference/eid260_rt0.png) comes out UPSIDE-DOWN -
the character's feet are at the top. That flipped image is NOT the target.

The map must satisfy two things at once:
  (a) view-space up must end up as Blender +Z (character standing), and
  (b) determinant must be +1, or the character is MIRRORED (left/right hands
      swapped) - a plain axis swap like (x,y,z)->(x,z,y) has det = -1 and is
      therefore wrong even though it looks upright.

The rigid transform that does both is "rotate -90 deg about X, then translate":

    x_b =  x
    y_b = -(z - EYE_Z)  ... no. Keep depth positive-forward instead:

Concretely we use

    x_b = -x
    y_b =  z - EYE_Z        (view depth becomes Blender forward +Y)
    z_b =  y                (view up stays up)

det = (-1) * (axis swap y<->z, det -1) = +1. Good: no mirroring.
Negating X is what keeps it a true rotation - it corresponds to the 180 deg
turn that also puts the camera on the correct side, and it is consistent with
the capture looking down +Z while Blender's camera looks down +Y with +X right.
=============================================================================
"""

import json
import math
import os
import sys

import bpy
import mathutils

_HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() \
    else r"E:\GST\libai_scene"
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fbx_ascii_import as FA          # our ASCII-FBX reader (Blender rejects ASCII FBX)

try:
    import importlib
    importlib.reload(FA)
except Exception:
    pass


# ============================ CONFIG ============================

EXPORT_ROOT = r"E:\GST\libai_scene\scene_export"
SCENE_JSON = os.path.join(EXPORT_ROOT, "scene.json")
TEX_MANIFEST = os.path.join(EXPORT_ROOT, "textures_manifest.json")
SAVE_BLEND = r"E:\GST\libai_scene\libai_scene_rebuilt.blend"
RENDER_PREVIEW = r"E:\GST\libai_scene\out\preview.png"

# --- camera, read from cb0._child3 of EID 260 (see the header) ---
PROJ_M00 = 2.101          # 1/tan(fovX/2)
PROJ_M11 = 3.736          # 1/tan(fovY/2)
EYE_Z = 100.061           # eye position along view-space Z
CAM_FOV_X_DEG = 2.0 * math.degrees(math.atan(1.0 / PROJ_M00))   # 50.91
CAM_FOV_Y_DEG = 2.0 * math.degrees(math.atan(1.0 / PROJ_M11))   # 29.97

# --- sun, from the light matrix shared by ALL 10 shadow passes ---
# cb0._child3 in every 1024x1024 shadow pass agrees on this direction, which is
# what makes it the real directional light rather than a per-object matrix.
SUN_FORWARD_VIEW = (-0.179, 0.175, -0.968)

# Two lighting rigs live in the same .blend so either look is one click away:
#   "Showcase"  - brightened, reads like the in-game skin preview  (ENABLED)
#   "CaptureNight" - faithful to the dim night-sky capture         (disabled)
LIGHTING_SHOWCASE = {
    "sun_energy": 5.0,
    "sun_angle_deg": 3.0,
    "fill_energy": 900.0,
    "rim_energy": 700.0,
    "world_color": (0.045, 0.055, 0.105, 1.0),
    "world_strength": 1.15,
}
LIGHTING_NIGHT = {
    "sun_energy": 1.6,
    "sun_angle_deg": 3.0,
    "fill_energy": 150.0,
    "rim_energy": 120.0,
    "world_color": (0.012, 0.016, 0.045, 1.0),
    "world_strength": 1.0,
}
ACTIVE_RIG = "Showcase"          # which collection is visible on open

RENDER_W, RENDER_H = 1920, 1080

CLEAR_SCENE = True
ROOT_NAME = "LiBai_ZheXian"

LOG_PATH = r"E:\GST\libai_scene\out\rebuild_log.txt"
_LINES = []


def log(msg):
    m = "[rebuild] {0}".format(msg)
    _LINES.append(m)
    print(m)


def flush_log():
    try:
        d = os.path.dirname(LOG_PATH)
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(_LINES))
    except Exception as e:
        print("[rebuild] could not write log: {0}".format(e))


# ==================== coordinate transform ====================

def view_to_blender(p):
    """Capture view-space point -> Blender world point. det = +1. See header."""
    x, y, z = p
    return (-x, z - EYE_Z, y)


def view_dir_to_blender(d):
    """Same map, without the translation (for directions)."""
    x, y, z = d
    return mathutils.Vector((-x, z, y))


# ==================== scene helpers ====================

def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.images,
                  bpy.data.cameras, bpy.data.lights, bpy.data.node_groups):
        for item in list(block):
            if item.users == 0:
                try:
                    block.remove(item)
                except Exception:
                    pass
    for coll in list(bpy.data.collections):
        try:
            bpy.data.collections.remove(coll)
        except Exception:
            pass


def get_or_make_collection(name, parent=None):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
    target = parent or bpy.context.scene.collection
    if coll.name not in [c.name for c in target.children]:
        try:
            target.children.link(coll)
        except Exception:
            pass
    return coll


def load_manifest():
    """{eid: {'basecolor': path, 'normal': path, 'mask': [paths]}}"""
    if not os.path.isfile(TEX_MANIFEST):
        log("no textures_manifest.json - materials will be untextured")
        return {}
    entries = json.load(open(TEX_MANIFEST, encoding="utf-8"))
    per_eid = {}
    for e in entries:
        role = e.get("role")
        path = e.get("path")
        if not path or not os.path.isfile(path):
            continue
        slot = e.get("slot", 99)
        for eid in e.get("eids", []):
            d = per_eid.setdefault(int(eid),
                                   {"basecolor": [], "normal": [], "mask": []})
            if role == "basecolor":
                d["basecolor"].append((slot, path))
            elif role == "normal":
                d["normal"].append((slot, path))
            else:
                d["mask"].append((slot, path))
    out = {}
    for eid, d in per_eid.items():
        pick = {}
        for k in ("basecolor", "normal"):
            if d[k]:
                pick[k] = sorted(d[k])[0][1]      # lowest slot = primary map
        pick["mask"] = [p for _, p in sorted(d["mask"])]
        out[eid] = pick
    return out


def load_image(path, non_color=False):
    name = os.path.basename(path)
    img = bpy.data.images.get(name)
    if img is None:
        try:
            img = bpy.data.images.load(path, check_existing=True)
        except Exception as e:
            log("image load failed {0}: {1}".format(name, e))
            return None
    if non_color:
        try:
            img.colorspace_settings.name = 'Non-Color'
        except Exception:
            pass
    return img


def build_material(name, tex):
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (620, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (280, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    def set_in(key, val):
        try:
            bsdf.inputs[key].default_value = val
        except Exception:
            pass

    set_in("Roughness", 0.5)
    set_in("Metallic", 0.0)

    bc = tex.get("basecolor")
    if bc:
        img = load_image(bc)
        if img:
            n = nt.nodes.new("ShaderNodeTexImage")
            n.image = img
            n.location = (-180, 200)
            n.label = "BaseColor"
            nt.links.new(n.outputs["Color"], bsdf.inputs["Base Color"])
            # hair / cloth / fx cards rely on the alpha channel for cutout
            try:
                nt.links.new(n.outputs["Alpha"], bsdf.inputs["Alpha"])
                if hasattr(mat, "blend_method"):
                    mat.blend_method = 'HASHED'
                if hasattr(mat, "shadow_method"):
                    mat.shadow_method = 'HASHED'
            except Exception:
                pass

    nm = tex.get("normal")
    if nm:
        img = load_image(nm, non_color=True)
        if img:
            n = nt.nodes.new("ShaderNodeTexImage")
            n.image = img
            n.location = (-180, -170)
            n.label = "Normal"
            nmap = nt.nodes.new("ShaderNodeNormalMap")
            nmap.location = (90, -170)
            nt.links.new(n.outputs["Color"], nmap.inputs["Color"])
            nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    # park the remaining maps in the graph, unconnected, for manual hookup
    y = -430
    for p in tex.get("mask", [])[:4]:
        img = load_image(p, non_color=True)
        if not img:
            continue
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image = img
        n.location = (-520, y)
        n.label = "spare: " + os.path.basename(p)
        y -= 300
    return mat


# ==================== main ====================

def main():
    if not os.path.isfile(SCENE_JSON):
        log("FATAL: scene.json not found at {0}".format(SCENE_JSON))
        flush_log()
        return

    scene = json.load(open(SCENE_JSON, encoding="utf-8"))
    texmap = load_manifest()
    log("manifest covers {0} draw(s)".format(len(texmap)))
    log("camera: fovX={0:.2f} fovY={1:.2f} aspect={2:.4f} eyeZ={3:.3f}".format(
        CAM_FOV_X_DEG, CAM_FOV_Y_DEG, PROJ_M11 / PROJ_M00, EYE_Z))

    if CLEAR_SCENE:
        clear_scene()

    root_coll = get_or_make_collection(ROOT_NAME)

    total = 0
    failed = []
    for group, items in sorted((scene.get("groups") or {}).items()):
        gcoll = get_or_make_collection(group, root_coll)
        gdir = os.path.join(EXPORT_ROOT, group)
        for it in items:
            eid = int(it["eventId"])
            fbx = it.get("fbx")
            if not fbx:
                continue
            path = os.path.join(gdir, fbx)
            if not os.path.isfile(path):
                log("missing {0}".format(path))
                failed.append(eid)
                continue

            try:
                obj = FA.load_ascii_fbx(path, name="eid{0}".format(eid))
            except Exception as e:
                log("parse failed eid{0}: {1}".format(eid, e))
                failed.append(eid)
                continue
            if obj is None:
                failed.append(eid)
                continue

            gcoll.objects.link(obj)

            # bake the view-space -> Blender map straight into the mesh, so the
            # objects keep an identity transform and stay easy to edit.
            # view_to_blender is a proper rotation (det = +1), so the triangle
            # winding stays valid - no flip_normals() needed.
            me = obj.data
            had_custom = bool(getattr(me, "has_custom_normals", False))
            old_normals = None
            if had_custom:
                # grab loop normals BEFORE moving verts, then map them as pure
                # directions (no translation) and re-apply.
                old_normals = [tuple(l.normal) for l in me.loops]

            for v in me.vertices:
                v.co = view_to_blender((v.co.x, v.co.y, v.co.z))

            if old_normals:
                try:
                    rotated = [(-n[0], n[2], n[1]) for n in old_normals]
                    me.normals_split_custom_set(rotated)
                except Exception as e:
                    log("  normal remap skipped eid{0}: {1}".format(eid, e))
            me.update()

            tex = texmap.get(eid, {})
            mat = build_material("mat_eid{0}".format(eid), tex)
            me.materials.clear()
            me.materials.append(mat)

            total += 1
            log("eid{0}: v={1} tri={2} uv={3} bc={4} nm={5}".format(
                eid, len(me.vertices), len(me.polygons),
                len(me.uv_layers),
                os.path.basename(tex.get("basecolor", "-")),
                os.path.basename(tex.get("normal", "-"))))

    log("built {0} object(s); failed={1}".format(total, failed or "none"))

    # Background / ground pieces stay in their own collections (03_props for the
    # rock, 04_bg_board for the sky + fx cards) so the whole backdrop can be
    # switched off with one click while the 10 character parts in 01_hero /
    # 02_hero_extraUV stay put. They are left VISIBLE by default.
    for name in ("03_props", "04_bg_board"):
        c = bpy.data.collections.get(name)
        if c:
            c.color_tag = 'COLOR_03' if name == "03_props" else 'COLOR_05'

    # ---------------- lighting rigs ----------------
    # Both rigs are built; only ACTIVE_RIG is left visible. Toggle the eye /
    # camera icon on the "Light_Showcase" / "Light_CaptureNight" collections in
    # the outliner to switch looks.
    d = view_dir_to_blender(SUN_FORWARD_VIEW).normalized()
    log("sun dir(blender)=({0:.3f},{1:.3f},{2:.3f})".format(d.x, d.y, d.z))

    def build_rig(tag, cfg):
        rc = get_or_make_collection("Light_" + tag, root_coll)

        sun_data = bpy.data.lights.new(name="Sun_" + tag, type='SUN')
        sun_data.energy = cfg["sun_energy"]
        try:
            sun_data.angle = math.radians(cfg["sun_angle_deg"])
        except Exception:
            pass
        sun = bpy.data.objects.new("Sun_" + tag, sun_data)
        sun.rotation_euler = d.to_track_quat('-Z', 'Y').to_euler()
        sun.location = -d * 15.0
        rc.objects.link(sun)

        # key-side fill, camera-left, keeps the shadow side readable
        fd = bpy.data.lights.new(name="Fill_" + tag, type='AREA')
        fd.energy = cfg["fill_energy"]
        fd.size = 6.0
        fill = bpy.data.objects.new("Fill_" + tag, fd)
        fill.location = (-4.0, 1.5, 3.0)
        aim = (mathutils.Vector((0.0, 6.0, 0.2)) - fill.location).normalized()
        fill.rotation_euler = aim.to_track_quat('-Z', 'Y').to_euler()
        rc.objects.link(fill)

        # rim/back light to separate the silhouette from the night sky
        rd_ = bpy.data.lights.new(name="Rim_" + tag, type='AREA')
        rd_.energy = cfg["rim_energy"]
        rd_.size = 4.0
        rim = bpy.data.objects.new("Rim_" + tag, rd_)
        rim.location = (3.5, 10.0, 4.0)
        aim = (mathutils.Vector((0.0, 6.0, 0.5)) - rim.location).normalized()
        rim.rotation_euler = aim.to_track_quat('-Z', 'Y').to_euler()
        rc.objects.link(rim)

        return rc

    rigs = {
        "Showcase": build_rig("Showcase", LIGHTING_SHOWCASE),
        "CaptureNight": build_rig("CaptureNight", LIGHTING_NIGHT),
    }

    # hide the inactive rig in both viewport and render
    vl = bpy.context.view_layer
    for tag, rc in rigs.items():
        on = (tag == ACTIVE_RIG)
        rc.hide_render = not on
        try:
            lc = None

            def find_lc(layer_coll):
                if layer_coll.collection == rc:
                    return layer_coll
                for ch in layer_coll.children:
                    r = find_lc(ch)
                    if r:
                        return r
                return None

            lc = find_lc(vl.layer_collection)
            if lc:
                lc.hide_viewport = not on
        except Exception as e:
            log("  rig visibility {0}: {1}".format(tag, e))
    log("lighting rigs built; active = {0}".format(ACTIVE_RIG))

    cfg = LIGHTING_SHOWCASE if ACTIVE_RIG == "Showcase" else LIGHTING_NIGHT

    # ---------------- camera ----------------
    cam_data = bpy.data.cameras.new("Camera_capture")
    cam_data.sensor_fit = 'HORIZONTAL'
    cam_data.sensor_width = 36.0
    cam_data.lens = 18.0 / math.tan(math.radians(CAM_FOV_X_DEG) / 2.0)
    cam = bpy.data.objects.new("Camera_capture", cam_data)
    # After view_to_blender the eye is exactly at the origin. The scene now sits
    # towards +Y, so the camera looks down +Y: rotate +90 deg about X (which
    # aims a Blender camera from -Z to +Y). No extra Z spin is needed - the
    # X negation inside view_to_blender already accounts for the handedness.
    cam.location = (0.0, 0.0, 0.0)
    cam.rotation_euler = (math.radians(90.0), 0.0, 0.0)
    root_coll.objects.link(cam)
    bpy.context.scene.camera = cam
    log("camera lens={0:.2f}mm at origin looking +Y".format(cam_data.lens))

    rs = bpy.context.scene.render
    rs.resolution_x = RENDER_W
    rs.resolution_y = RENDER_H
    rs.resolution_percentage = 100

    world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    try:
        bg = world.node_tree.nodes.get("Background")
        if bg:
            bg.inputs[0].default_value = cfg["world_color"]
            bg.inputs[1].default_value = cfg["world_strength"]
    except Exception:
        pass

    # ---------------- report framing ----------------
    # sanity: how much of the frame does the hero fill?
    hero = [o for o in bpy.data.objects
            if o.type == 'MESH' and o.name.startswith("eid260")]
    if hero:
        me = hero[0].data
        ys = [v.co.y for v in me.vertices]
        zs = [v.co.z for v in me.vertices]
        log("hero eid260: forward(y) {0:.2f}..{1:.2f}, up(z) {2:.2f}..{3:.2f}".format(
            min(ys), max(ys), min(zs), max(zs)))

    try:
        bpy.ops.wm.save_as_mainfile(filepath=SAVE_BLEND)
        log("saved {0}".format(SAVE_BLEND))
    except Exception as e:
        log("save failed: {0}".format(e))

    # ---------------- preview render ----------------
    # A render is the only honest check that the framing/orientation is right,
    # so always produce one when running headless.
    if os.environ.get("LIBAI_RENDER", "1") != "0":
        try:
            rs.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in \
                rs.bl_rna.properties['engine'].enum_items.keys() else 'BLENDER_EEVEE'
        except Exception:
            pass
        try:
            rs.resolution_percentage = 50          # 960x540 is plenty to judge
            rs.image_settings.file_format = 'PNG'
            rs.filepath = RENDER_PREVIEW
            d = os.path.dirname(RENDER_PREVIEW)
            if not os.path.isdir(d):
                os.makedirs(d)
            bpy.ops.render.render(write_still=True)
            log("preview rendered -> {0} (engine={1})".format(
                RENDER_PREVIEW, rs.engine))
        except Exception as e:
            log("preview render failed: {0}".format(e))
        finally:
            rs.resolution_percentage = 100

    log("DONE: {0} draws + sun + fill + camera".format(total))
    flush_log()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("FATAL\n" + traceback.format_exc())
        flush_log()
