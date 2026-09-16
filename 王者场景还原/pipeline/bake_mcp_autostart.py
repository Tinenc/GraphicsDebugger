"""Bake BlenderMCP auto-start into the rebuilt .blend.

The addon does NOT open port 9876 during register() -- it has a scene
property `blendermcp_auto_start_server` that must be True and *saved into
the .blend*. Setting it here means merely opening the file brings the
bridge up, no button click and no --python bootstrap required.

  blender.exe --background --python bake_mcp_autostart.py
"""
import os

import bpy

BLEND = r"E:\GST\libai_scene\libai_scene_rebuilt.blend"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT, exist_ok=True)
REPORT = os.path.join(OUT, "bake_mcp_autostart.txt")

lines = []


def log(m):
    lines.append(str(m))
    print(str(m))


log("blender = {0}".format(bpy.app.version_string))

import addon_utils
try:
    addon_utils.enable("blender_mcp", default_set=True, persistent=True)
    log("addon enabled (and set to load on startup)")
except Exception as exc:
    log("FATAL: could not enable blender_mcp: {0}".format(exc))
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    raise SystemExit(1)

bpy.ops.wm.open_mainfile(filepath=BLEND)
log("opened {0}".format(BLEND))

sc = bpy.context.scene
touched = []
for prop, val in (("blendermcp_auto_start_server", True),
                  ("blendermcp_port", 9876)):
    if hasattr(sc, prop):
        try:
            setattr(sc, prop, val)
            touched.append("{0}={1}".format(prop, getattr(sc, prop)))
        except Exception as exc:
            log("could not set {0}: {1}".format(prop, exc))
    else:
        log("scene has no property {0}".format(prop))
log("set: {0}".format(touched))

# persist the addon-enabled state in user prefs too, so the addon is loaded
# on every launch rather than only when a bootstrap script enables it
try:
    bpy.ops.wm.save_userpref()
    log("user prefs saved (addon will load on every start)")
except Exception as exc:
    log("save_userpref failed: {0}".format(exc))

bpy.ops.wm.save_mainfile(filepath=BLEND)
log("saved {0}".format(BLEND))

with open(REPORT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print("report -> " + REPORT)
