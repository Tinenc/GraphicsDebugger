"""
RenderDoc MCP Server — 37 tools, communicates via file IPC with RenderDoc extension.

Architecture:
  [WorkBuddy/AI] <-stdio-> [This MCP Server] <-file IPC-> [RenderDoc extension]
"""

from __future__ import annotations
import json, logging, os, subprocess, time, shutil
from typing import Any
from mcp.server.fastmcp import FastMCP
from .ipc_client import IPCClient

logger = logging.getLogger("renderdoc-mcp")
logging.basicConfig(level=logging.INFO, format="%(name)s | %(levelname)s | %(message)s")

mcp = FastMCP("renderdoc-mcp", instructions="Full-featured RenderDoc GPU capture analysis with shader reverse-engineering")
ipc = IPCClient(timeout=30.0)

def _ok(d: Any) -> str:
    return json.dumps(d, ensure_ascii=False, default=str)

def _err(e: Exception) -> str:
    return json.dumps({"error": str(e), "type": type(e).__name__})

def _call(method: str, params: dict = None, timeout: float = None) -> str:
    try:
        return _ok(ipc.call(method, params or {}, timeout=timeout))
    except Exception as e:
        return _err(e)


# ==================== 1. CONNECTION ====================

@mcp.tool()
def ping() -> str:
    """Check if the RenderDoc bridge is running and responsive.

    Returns 'ok' if RenderDoc is running with the MCP bridge extension active.
    If this fails, make sure:
    1. RenderDoc (qrenderdoc) is open
    2. A capture file is loaded
    3. The MCP Bridge extension is enabled (Tools > MCP Bridge > Status)
    """
    return _call("ping")

@mcp.tool()
def get_capture_status() -> str:
    """Check if a capture is loaded in RenderDoc and get API info."""
    return _call("get_capture_status")


# ==================== 2. CAPTURE MANAGEMENT ====================

@mcp.tool()
def list_captures(directory: str) -> str:
    """List all .rdc capture files in a directory.

    Args:
        directory: Directory path to search for .rdc files.
    """
    return _call("list_captures", {"directory": directory})

@mcp.tool()
def open_capture(capture_path: str) -> str:
    """Open a capture file in the running RenderDoc instance.

    Args:
        capture_path: Absolute path to the .rdc file.
    """
    return _call("open_capture", {"capture_path": capture_path})


@mcp.tool()
def debug_vulkan_bindings(event_id: int, stage: str = "fragment") -> str:
    """Debug Vulkan descriptor set bindings - dump raw data structures to diagnose texture binding resolution.

    Args:
        event_id: The event ID of the draw call.
        stage: Shader stage (default "fragment").
    """
    return _call("debug_vulkan_bindings", {"event_id": event_id, "stage": stage}, timeout=60.0)


@mcp.tool()
def analyze_lighting(event_id: int) -> str:
    """Analyze lighting setup from a draw call's shader and constant buffers.

    Extracts all lighting-related information:
    - Main directional light (direction, color, intensity)
    - Point/spot lights (position, color, range, attenuation)
    - Shadow maps (resolution, format, cascade info)
    - Environment lighting (IBL cubemap, SH coefficients, ambient color)
    - Lighting model detection (PBR/Blinn-Phong/Lambert from shader code)
    - All cbuffer variables with semantic classification

    This is the PRIMARY tool for understanding how a scene is lit.

    Args:
        event_id: The event ID of the draw call to analyze.
    """
    return _call("analyze_lighting", {"event_id": event_id}, timeout=60.0)


@mcp.tool()
def identify_drawcalls(
    event_id_min: int = 0,
    event_id_max: int = 999999,
    output_dir: str = "",
    render_target: int = 0,
) -> str:
    """Identify what each DrawCall renders by analyzing RT output, shader groups, bound textures, and screen-space bounding boxes.

    This is the PRIMARY tool for understanding what each DrawCall draws (eye, hair, armor, weapon, etc.)
    Instead of guessing by polygon count, it provides:
    - Per-DrawCall RT thumbnails (showing what was rendered at that point)
    - Shader grouping (same shader = same material type)
    - Bound texture list with resource IDs (check albedo to identify the part)
    - Screen-space bounding box (where on screen this part appears)
    - Screen coverage percentage (how much of the screen this part covers)

    Args:
        event_id_min: Start of event range (default 0).
        event_id_max: End of event range (default 999999).
        output_dir: Directory to save per-DrawCall RT thumbnails as PNG.
        render_target: Override render target resource ID (auto-detected if 0).
    """
    return _call("identify_drawcalls", {
        "event_id_min": event_id_min,
        "event_id_max": event_id_max,
        "output_dir": output_dir,
        "render_target": render_target,
    }, timeout=120.0)


@mcp.tool()
def launch_renderdoc(capture_path: str, renderdoc_path: str = "") -> str:
    """Launch RenderDoc application and open a .rdc capture file.

    Use this when RenderDoc is not yet running or you want to open a new
    RenderDoc instance with a specific capture file. This will:
    1. Find the RenderDoc executable (qrenderdoc)
    2. Launch it with the specified .rdc file
    3. Wait for the MCP bridge to become responsive

    Wait time is automatically scaled by file size:
    - <50MB: up to 40 seconds
    - 50-200MB: up to 60 seconds
    - >200MB: up to 90 seconds

    If the bridge is not ready within the timeout, the function still returns
    success (launched=True, bridgeReady=False). Use ping() to check later.

    If RenderDoc is already running with the MCP bridge active, prefer
    using open_capture() instead — it's faster since it reuses the
    existing instance.

    Args:
        capture_path: Absolute path to the .rdc capture file to open.
        renderdoc_path: Optional path to qrenderdoc executable. If empty,
                        auto-detects from common install locations.
    """
    import tempfile

    # Validate capture file
    if not capture_path:
        return _ok({"error": "capture_path is required"})
    if not os.path.isfile(capture_path):
        return _ok({"error": "File not found: %s" % capture_path})
    if not capture_path.lower().endswith(".rdc"):
        return _ok({"error": "Not an .rdc file: %s" % capture_path})

    # Find qrenderdoc executable
    exe = _find_renderdoc_exe(renderdoc_path)
    if not exe:
        return _ok({
            "error": "Could not find qrenderdoc executable. "
                     "Please provide renderdoc_path or install RenderDoc to a standard location.",
            "searchedPaths": _get_renderdoc_search_paths(),
        })

    # Check if bridge is already alive — if so, just use open_capture
    if ipc.is_bridge_alive():
        logger.info("RenderDoc bridge already running, using open_capture instead")
        result = _call("open_capture", {"capture_path": capture_path})
        try:
            parsed = json.loads(result)
            parsed["method"] = "open_capture (bridge was already running)"
            return _ok(parsed)
        except Exception:
            return result

    # Start the UI first, wait for the extension bridge, then ask the bridge to
    # open the capture. Passing the capture on the command line can block v1.44
    # before extensions begin polling.
    logger.info("Launching RenderDoc: %s", exe)
    try:
        child_env = dict(os.environ)
        # Do not let a globally enabled capture layer inject the Development
        # runtime back into the Release UI process.
        child_env.pop("ENABLE_VULKAN_TINECMATOOL_CAPTURE", None)
        child_env["DISABLE_VULKAN_TINECMATOOL_CAPTURE_1_44"] = "1"
        proc = subprocess.Popen(
            [exe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception as e:
        return _ok({"error": "Failed to launch RenderDoc: %s" % str(e), "exe": exe})

    # Wait for the MCP bridge to come online
    # Dynamic timeout based on file size: bigger files take longer to load
    file_size_mb = os.path.getsize(capture_path) / (1024 * 1024)
    if file_size_mb > 200:
        max_wait = 90  # Very large captures (>200MB)
    elif file_size_mb > 50:
        max_wait = 60  # Large captures (50-200MB)
    else:
        max_wait = 40  # Normal captures (<50MB)

    poll_interval = 2.0
    elapsed = 0.0
    bridge_ready = False

    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval
        if ipc.is_bridge_alive():
            bridge_ready = True
            break

    if bridge_ready:
        open_result = ipc.call("open_capture", {"capture_path": capture_path}, timeout=30.0)
        return _ok({
            "launched": True,
            "exe": exe,
            "capture": capture_path,
            "pid": proc.pid,
            "bridgeReady": True,
            "openResult": open_result,
            "waitTime": "%.1fs" % elapsed,
            "fileSizeMB": round(file_size_mb, 1),
        })
    else:
        # RenderDoc launched successfully, but bridge not yet responsive.
        # This is NORMAL for large captures — NOT an error.
        return _ok({
            "launched": True,
            "exe": exe,
            "capture": capture_path,
            "pid": proc.pid,
            "bridgeReady": False,
            "waitTime": "%.1fs" % max_wait,
            "fileSizeMB": round(file_size_mb, 1),
            "status": "RenderDoc launched successfully. The capture file (%.0fMB) is still loading. "
                      "Use ping() to check when the bridge becomes ready." % file_size_mb,
        })


def _find_renderdoc_exe(user_path: str = "") -> str:
    """Find the qrenderdoc executable."""
    executable_names = ["qTinecmaTool.exe", "qrenderdoc.exe", "qrenderdoc"]

    # 1. User-provided path
    if user_path:
        if os.path.isfile(user_path):
            return user_path
        # Maybe it's a directory
        for name in executable_names:
            candidate = os.path.join(user_path, name)
            if os.path.isfile(candidate):
                return candidate

    # 2. Environment variable
    env_path = os.environ.get("RENDERDOC_PATH", "") or os.environ.get("RENDERDOC_MODULE_PATH", "")
    if env_path:
        if os.path.isfile(env_path):
            return env_path
        for name in executable_names:
            candidate = os.path.join(env_path, name)
            if os.path.isfile(candidate):
                return candidate

    # 3. PATH
    for name in ["qTinecmaTool", "qrenderdoc"]:
        which = shutil.which(name)
        if which:
            return which

    # 4. Common install locations
    for path in _get_renderdoc_search_paths():
        if os.path.isfile(path):
            return path

    return ""


def _get_renderdoc_search_paths() -> list:
    """Get common RenderDoc install paths to search."""
    paths = []
    # Windows
    for drive in ["C", "D"]:
        paths.append("%s:\\Program Files\\RenderDoc\\qrenderdoc.exe" % drive)
        paths.append("%s:\\Program Files (x86)\\RenderDoc\\qrenderdoc.exe" % drive)
    # User-local installs on Windows
    appdata = os.environ.get("LOCALAPPDATA", "")
    if appdata:
        paths.append(os.path.join(appdata, "RenderDoc", "qrenderdoc.exe"))
    # Linux
    paths.extend([
        "/usr/bin/qrenderdoc",
        "/usr/local/bin/qrenderdoc",
        "/opt/renderdoc/bin/qrenderdoc",
    ])
    # macOS
    paths.append("/Applications/RenderDoc.app/Contents/MacOS/qrenderdoc")
    return paths


# ==================== 3. DRAWCALLS ====================

@mcp.tool()
def get_draw_calls(include_children: bool = True, marker_filter: str = "", only_actions: bool = False, event_id_min: int = 0, event_id_max: int = 0) -> str:
    """Get all draw calls/actions in the currently loaded capture.

    Args:
        include_children: Include child actions in hierarchy.
        marker_filter: Only return actions under markers matching this string.
        only_actions: Only return actual draw/dispatch actions, skip markers.
        event_id_min: Filter by minimum event ID (0 = no filter).
        event_id_max: Filter by maximum event ID (0 = no filter).
    """
    params = {"include_children": include_children, "only_actions": only_actions}
    if marker_filter: params["marker_filter"] = marker_filter
    if event_id_min > 0: params["event_id_min"] = event_id_min
    if event_id_max > 0: params["event_id_max"] = event_id_max
    return _call("get_draw_calls", params)

@mcp.tool()
def get_frame_summary() -> str:
    """Get a summary of the current capture frame (total draws, dispatches, markers)."""
    return _call("get_frame_summary")

@mcp.tool()
def get_draw_call_details(event_id: int) -> str:
    """Get detailed information about a specific draw call.

    Args:
        event_id: The event ID of the draw call.
    """
    return _call("get_draw_call_details", {"event_id": event_id})


# ==================== 4. PIPELINE STATE ====================

@mcp.tool()
def get_pipeline_state(event_id: int) -> str:
    """Get the full pipeline state at a specific event.

    Returns bound shaders, render targets, textures, samplers, viewport, etc.

    Args:
        event_id: The event ID to inspect.
    """
    return _call("get_pipeline_state", {"event_id": event_id})


# ==================== 5. SHADER ====================

@mcp.tool()
def get_shader_info(event_id: int, stage: str = "fragment") -> str:
    """Get shader information for a specific stage at an event.

    Returns shader reflection data, disassembly, bound resources, etc.

    Args:
        event_id: The event ID of the draw call.
        stage: Shader stage - "vertex", "fragment"/"pixel", "geometry", "compute", "hull"/"tess_ctrl", "domain"/"tess_eval".
    """
    return _call("get_shader_info", {"event_id": event_id, "stage": stage})

@mcp.tool()
def reverse_shader(event_id: int, stage: str = "fragment") -> str:
    """Reverse-engineer a shader: get disassembly, reflection, bound textures with role detection, and live cbuffer values.

    This is the one-stop tool for understanding what a shader does.
    Returns ALL available info including HLSL/GLSL decompilation if available.

    Args:
        event_id: The event ID of the draw call.
        stage: Shader stage - "vertex", "fragment"/"pixel", "geometry", "compute", "hull"/"tess_ctrl", "domain"/"tess_eval".
    """
    return _call("reverse_shader", {"event_id": event_id, "stage": stage})

@mcp.tool()
def get_bound_textures(event_id: int, stage: str = "fragment") -> str:
    """Get all textures bound to a shader stage with auto-detected roles.

    For each texture slot, shows shader variable name, actual bound texture,
    and inferred role (albedo, normal, roughness, metallic, ao, emissive, etc.)

    Args:
        event_id: The event ID of the draw call.
        stage: Shader stage - "vertex", "fragment"/"pixel", "geometry", "compute", "hull"/"tess_ctrl", "domain"/"tess_eval".
    """
    return _call("get_bound_textures", {"event_id": event_id, "stage": stage})


# ==================== 6. SEARCH ====================

@mcp.tool()
def find_draws_by_shader(shader_name: str, stage: str = "") -> str:
    """Find all draw calls that use a shader with the given name (partial match).

    Useful for finding which draw calls render a specific material/effect.

    Args:
        shader_name: Partial shader name to search for.
        stage: Optional stage filter ("vertex", "fragment", etc.).
    """
    params = {"shader_name": shader_name}
    if stage: params["stage"] = stage
    return _call("find_by_shader", params)

@mcp.tool()
def find_draws_by_texture(texture_name: str) -> str:
    """Find all draw calls that use a texture with the given name (partial match).

    Useful for finding which draw calls use a specific texture (e.g. "eye").

    Args:
        texture_name: Partial texture name to search for (e.g. "eye", "skin", "hair").
    """
    return _call("find_by_texture", {"texture_name": texture_name})

@mcp.tool()
def find_draws_by_resource(resource_id: int) -> str:
    """Find all draw calls that use a specific resource ID.

    Args:
        resource_id: The exact resource ID to search for.
    """
    return _call("find_by_resource", {"resource_id": resource_id})


# ==================== 7. RESOURCES ====================

@mcp.tool()
def get_textures() -> str:
    """List all textures alive in the capture (dimensions, format, mips, etc.)."""
    return _call("get_textures")

@mcp.tool()
def get_buffers() -> str:
    """List all buffers alive in the capture (size, name)."""
    return _call("get_buffers")

@mcp.tool()
def get_resources() -> str:
    """List ALL resources in the capture (textures, buffers, shaders, pipelines, etc.)."""
    return _call("get_resources")

@mcp.tool()
def get_texture_info(resource_id: int) -> str:
    """Get texture metadata (dimensions, format, mips, etc.).

    Args:
        resource_id: The texture resource ID.
    """
    return _call("get_texture_info", {"resource_id": resource_id})

@mcp.tool()
def get_texture_data(resource_id: int, mip: int = 0, slice: int = 0) -> str:
    """Get texture pixel data.

    Args:
        resource_id: The texture resource ID.
        mip: Mip level (default 0).
        slice: Array slice (default 0).
    """
    return _call("get_texture_data", {"resource_id": resource_id, "mip": mip, "slice": slice})

@mcp.tool()
def get_buffer_contents(resource_id: int, offset: int = 0, length: int = 256) -> str:
    """Get buffer data.

    Args:
        resource_id: The buffer resource ID.
        offset: Byte offset to start reading.
        length: Number of bytes to read (0 = all).
    """
    return _call("get_buffer_data", {"resource_id": resource_id, "offset": offset, "length": length})


# ==================== 8. TEXTURE OPS ====================

@mcp.tool()
def pick_pixel(resource_id: int, x: int, y: int) -> str:
    """Read the RGBA value of a specific pixel from a texture.

    Args:
        resource_id: The texture resource ID.
        x: Pixel X coordinate.
        y: Pixel Y coordinate.
    """
    return _call("pick_pixel", {"resource_id": resource_id, "x": x, "y": y})

@mcp.tool()
def get_texture_minmax(resource_id: int) -> str:
    """Get the min and max pixel values in a texture.

    Args:
        resource_id: The texture resource ID.
    """
    return _call("get_texture_minmax", {"resource_id": resource_id})

@mcp.tool()
def save_texture(resource_id: int, output_path: str, mip: int = 0, slice: int = -1) -> str:
    """Save a texture to disk. Supports PNG, JPG, BMP, TGA, HDR, EXR, DDS.

    Auto-detects CubeMap textures (arraysize=6) and handles them appropriately:
    - DDS format: saves all 6 faces in a single native cubemap file
    - PNG/JPG/etc: saves 6 individual face files (e.g. texture_face0_posX.png through face5_negZ.png)
    - Use slice=0..5 to export a specific face only

    CubeMap face order: 0=+X(Right), 1=-X(Left), 2=+Y(Up), 3=-Y(Down), 4=+Z(Front), 5=-Z(Back)

    Args:
        resource_id: The texture resource ID.
        output_path: File path to save to (format inferred from extension).
        mip: Mip level (default 0).
        slice: For CubeMap: -1=all faces (default), 0-5=specific face. Ignored for 2D textures.
    """
    return _call("save_texture", {
        "resource_id": resource_id,
        "output_path": output_path,
        "mip": mip,
        "slice": slice,
    })


# ==================== 9. PIXEL HISTORY ====================

@mcp.tool()
def pixel_history(resource_id: int, x: int, y: int) -> str:
    """Get the full modification history of a pixel across the frame.

    Args:
        resource_id: The render target texture resource ID.
        x: Pixel X coordinate.
        y: Pixel Y coordinate.
    """
    return _call("pixel_history", {"resource_id": resource_id, "x": x, "y": y})


# ==================== 10. SHADER DEBUG ====================

@mcp.tool()
def debug_pixel(x: int, y: int) -> str:
    """Debug the pixel shader execution at a specific pixel. Must set_event first.

    Args:
        x: Pixel X coordinate.
        y: Pixel Y coordinate.
    """
    return _call("debug_pixel", {"x": x, "y": y})

@mcp.tool()
def debug_vertex(vertex_id: int, instance_id: int = 0) -> str:
    """Debug the vertex shader execution for a specific vertex. Must set_event first.

    Args:
        vertex_id: The vertex ID to debug.
        instance_id: Instance ID (default 0).
    """
    return _call("debug_vertex", {"vertex_id": vertex_id, "instance_id": instance_id})


# ==================== 11. PERF COUNTERS ====================

@mcp.tool()
def enumerate_counters() -> str:
    """List all available GPU performance counters for the current capture."""
    return _call("enumerate_counters")

@mcp.tool()
def fetch_counters(counter_ids: str) -> str:
    """Fetch values for specific GPU performance counters.

    Args:
        counter_ids: Comma-separated list of counter IDs (e.g. "1,2,3").
    """
    ids = [int(x.strip()) for x in counter_ids.split(",")]
    return _call("fetch_counters", {"counter_ids": ids})


# ==================== 12. MESH & DEBUG MSGS ====================

@mcp.tool()
def get_post_vs_data() -> str:
    """Get post-vertex-shader mesh data for the current draw call."""
    return _call("get_post_vs_data")

@mcp.tool()
def get_debug_messages() -> str:
    """Get API validation/debug messages from the graphics driver."""
    return _call("get_debug_messages")

@mcp.tool()
def get_action_timings(event_ids: str = "", marker_filter: str = "") -> str:
    """Get GPU timing information for actions.

    Args:
        event_ids: Comma-separated event IDs (empty = all).
        marker_filter: Only actions under matching markers.
    """
    params = {}
    if event_ids: params["event_ids"] = [int(x.strip()) for x in event_ids.split(",")]
    if marker_filter: params["marker_filter"] = marker_filter
    return _call("get_action_timings", params)


# ==================== 13. EXPORT ====================

@mcp.tool()
def export_drawcall(event_id: int, output_dir: str) -> str:
    """One-shot export of a draw call: shaders + textures + cbuffer values to disk.

    Exports shader disassembly, source code, all bound textures as PNG, and a summary JSON.

    Args:
        event_id: The event ID of the draw call to export.
        output_dir: Directory to save all exported files.
    """
    return _call("export_drawcall", {"event_id": event_id, "output_dir": output_dir}, timeout=60)


@mcp.tool()
def export_to_unity(event_id: int, output_dir: str, mesh_name: str = "exported_mesh") -> str:
    """Export a draw call's mesh and shader data as Unity-ready assets.

    This is the ultimate tool for bringing RenderDoc captures into Unity:
    - Exports the 3D mesh as .obj AND .fbx (positions, normals, UVs, indices)
    - Collects all bound texture info with role detection (use save_texture to export textures separately)
    - Generates a Unity material definition JSON with property mappings
    - Maps shader parameters to Unity Standard/URP/HDRP material properties

    The output folder can be directly copied into a Unity project's Assets folder.

    Args:
        event_id: The event ID of the draw call to export.
        output_dir: Directory to save all exported Unity-ready files.
        mesh_name: Name for the exported mesh and material (default: "exported_mesh").
    """
    return _call("export_to_unity", {
        "event_id": event_id,
        "output_dir": output_dir,
        "mesh_name": mesh_name,
    }, timeout=60)


# ==================== ENTRYPOINT ====================

def main():
    logger.info("Starting RenderDoc MCP Server (IPC bridge)...")
    if ipc.is_bridge_alive():
        logger.info("RenderDoc bridge: CONNECTED")
    else:
        logger.warning("RenderDoc bridge: NOT responding. Start RenderDoc with a capture loaded.")
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
