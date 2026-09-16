# -*- coding: utf-8 -*-
"""
Open the rebuilt scene in the Blender GUI and start the BlenderMCP server, so
the agent can keep working on the scene through the MCP bridge.

Run:  blender.exe libai_scene_rebuilt.blend --python autostart_mcp.py
"""

import bpy


def enable_addon():
    import addon_utils
    for name in ("blender_mcp", "bl_ext.user_default.blender_mcp"):
        try:
            addon_utils.enable(name, default_set=True, persistent=True)
            print("[autostart] enabled addon: {0}".format(name))
            return name
        except Exception as e:
            print("[autostart] enable {0} failed: {1}".format(name, e))
    return None


def start_server():
    # the addon exposes an operator to start listening on localhost:9876
    candidates = [
        getattr(getattr(bpy.ops, "blendermcp", None), "start_server", None),
        getattr(getattr(bpy.ops, "blender_mcp", None), "start_server", None),
    ]
    for op in candidates:
        if op is None:
            continue
        try:
            op()
            print("[autostart] MCP server started")
            return True
        except Exception as e:
            print("[autostart] start_server failed: {0}".format(e))

    # fall back to poking the scene property the addon watches
    for prop in ("blendermcp_server_running", "blender_mcp_server_running"):
        if hasattr(bpy.context.scene, prop):
            try:
                setattr(bpy.context.scene, prop, True)
                print("[autostart] set scene.{0} = True".format(prop))
                return True
            except Exception as e:
                print("[autostart] set {0} failed: {1}".format(prop, e))
    return False


def frame_camera():
    """Put the viewport on the capture camera so the framing is obvious."""
    try:
        for area in bpy.context.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for space in area.spaces:
                if space.type != 'VIEW_3D':
                    continue
                space.region_3d.view_perspective = 'CAMERA'
                space.shading.type = 'MATERIAL'
                print("[autostart] viewport -> camera view, material shading")
    except Exception as e:
        print("[autostart] viewport setup skipped: {0}".format(e))


enable_addon()
started = start_server()
frame_camera()
print("[autostart] done (server_started={0})".format(started))
