"""Verify the BlenderMCP addon registers and opens 9876 on Blender 5.2.

Headless check -- proves the addon is 5.2-compatible before asking the user
to launch the GUI. Writes out/mcp_addon_check.txt.

  blender.exe --background --python check_mcp_addon.py
"""
import os
import socket
import sys

import bpy

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT, exist_ok=True)
REPORT = os.path.join(OUT, "mcp_addon_check.txt")

lines = []


def log(m):
    lines.append(str(m))
    print(str(m))


log("blender = {0}".format(bpy.app.version_string))

import addon_utils

# what does Blender see?
found = []
for mod in addon_utils.modules():
    nm = getattr(mod, "__name__", "?")
    if "mcp" in nm.lower():
        found.append(nm)
log("addon modules matching 'mcp': {0}".format(found or "NONE"))

enabled_as = None
for name in ("blender_mcp", "bl_ext.user_default.blender_mcp"):
    try:
        addon_utils.enable(name, default_set=True, persistent=True)
        enabled_as = name
        log("enabled OK as: {0}".format(name))
        break
    except Exception as exc:
        log("enable '{0}' failed: {1}".format(name, exc))

if not enabled_as:
    log("RESULT: addon could NOT be enabled on this build")
else:
    # what operators / props did it expose?
    for ns in ("blendermcp", "blender_mcp"):
        grp = getattr(bpy.ops, ns, None)
        if grp is None:
            log("bpy.ops.{0}: absent".format(ns))
            continue
        try:
            ops = [o for o in dir(grp) if not o.startswith("_")]
        except Exception:
            ops = []
        log("bpy.ops.{0}: {1}".format(ns, ops))

    sc = bpy.context.scene
    props = [p for p in dir(sc) if "mcp" in p.lower()]
    log("scene props matching 'mcp': {0}".format(props or "NONE"))

    # is the port actually listening after registration?
    def port_open(port=9876):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except Exception:
            return False
        finally:
            s.close()

    log("9876 listening right after enable: {0}".format(port_open()))

    # try the explicit start path too
    started = False
    for ns in ("blendermcp", "blender_mcp"):
        grp = getattr(bpy.ops, ns, None)
        op = getattr(grp, "start_server", None) if grp else None
        if op is None:
            continue
        try:
            op()
            started = True
            log("called bpy.ops.{0}.start_server() OK".format(ns))
        except Exception as exc:
            log("bpy.ops.{0}.start_server() failed: {1}".format(ns, exc))
    if not started:
        for p in ("blendermcp_server_running", "blender_mcp_server_running"):
            if hasattr(sc, p):
                try:
                    setattr(sc, p, True)
                    started = True
                    log("set scene.{0} = True".format(p))
                except Exception as exc:
                    log("set scene.{0} failed: {1}".format(p, exc))

    log("9876 listening after start attempt: {0}".format(port_open()))
    log("RESULT: addon enabled on {0}; explicit-start path used={1}".format(
        bpy.app.version_string, started))

with open(REPORT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print("report -> " + REPORT)
