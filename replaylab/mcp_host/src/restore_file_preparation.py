"""Bounded parallel file inspection. No replay/Qt calls or workspace writes."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context


def artifact_statuses(factory, paths, checkpoint=None, *, collect_errors=False):
    paths = list(paths)
    def inspect(path):
        try:
            return factory().status(str(path))
        except Exception as error:
            if not collect_errors:
                raise
            return error
    if len(paths) < 8:
        return [inspect(path) for path in paths]
    results = []
    # Only four reads may be queued. Never parallelize compiler launches,
    # replacement transactions, aliases or current-capture event selection.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="bread-restore-files") as pool:
        for offset in range(0, len(paths), 4):
            if checkpoint:
                checkpoint("preparing independent Shader artifacts")
            batch = [pool.submit(copy_context().run, inspect, path) for path in paths[offset:offset+4]]
            results.extend(future.result() for future in batch)
    return results
