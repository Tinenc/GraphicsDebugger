"""Cheap project-window invalidation; no JSON validation or replay operations.

The GUI submits snapshots to its existing background executor, so network/ACL
checks never block the Qt thread. Python 3.6 compatible.
"""
import os


def metadata_paths(projects, environment):
    paths = set()
    for key in ("RENDERDOC_ULTRA_PROJECT_REGISTRY", "RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE"):
        if environment.get(key):
            paths.add(os.path.abspath(environment[key]))
    for project in projects:
        root = project.get("projectRoot")
        if isinstance(root, str) and os.path.isabs(root):
            for name in ("workspace.json", "capture-library.json"):
                paths.add(os.path.join(root, ".replaylab", name))
    return tuple(sorted(paths))


def snapshot(paths):
    values = []
    for path in paths:
        try:
            stat = os.lstat(path)
            stamp = (stat.st_dev, stat.st_ino, stat.st_size,
                     getattr(stat, "st_mtime_ns", stat.st_mtime),
                     getattr(stat, "st_ctime_ns", stat.st_ctime))
        except OSError as error:
            stamp = ("unavailable", getattr(error, "winerror", None), error.errno)
        values.append((path, stamp))
    return tuple(values)
