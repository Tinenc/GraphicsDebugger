"""Per-application routing; shared project library, independent active selection.

Compatible with qrenderdoc's embedded Python 3.6. No provider login or user data
is copied. Only a small legacy selection hint is seeded into the private IPC root.
"""
import json
import os


def configure(environment, instance_id, ipc_directory):
    local = environment.get("LOCALAPPDATA") or environment.get("XDG_DATA_HOME")
    data = local or os.path.join(os.path.expanduser("~"), ".local", "share")
    config = os.path.join(data, "ReplayLab", "config")
    previous = environment.get("BREAD_ROUTING_INSTANCE")
    if previous == instance_id:
        return
    os.makedirs(ipc_directory, exist_ok=True)
    selection = os.path.join(ipc_directory, "active-project.json")
    legacy = environment.get("RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE") or os.path.join(config, "active-project.json")
    if not os.path.exists(selection) and not previous:
        try:
            if os.path.getsize(legacy) < 65536 and not os.path.islink(legacy):
                with open(legacy, "r", encoding="utf-8") as stream:
                    hint = json.load(stream)
                if isinstance(hint, dict):
                    with open(selection, "x", encoding="utf-8") as stream:
                        json.dump(hint, stream)
        except (OSError, ValueError):
            pass
    environment.update({
        "REPLAYLAB_INSTANCE_ID": instance_id,
        "BREAD_ROUTING_INSTANCE": instance_id,
        "RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE": selection,
        "RENDERDOC_ULTRA_PROJECT_REGISTRY": environment.get("RENDERDOC_ULTRA_PROJECT_REGISTRY") or os.path.join(config, "project-registry.json"),
        "REPLAYLAB_INTERNAL_RUNTIME_DIR": os.path.join(ipc_directory, "internal-runtime"),
        "REPLAYLAB_RUNTIME_DIR": os.path.join(ipc_directory, "mcp-runtime"),
    })
