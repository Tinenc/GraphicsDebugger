"""Task-local checkpoints for continuation, without time-travel history."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver


class LatestCheckpointSaver(InMemorySaver):
    """Retain current/parent snapshots and the delta history they depend on.

    Bread resumes the current graph only. Keeping every historical version of
    the full message channel would duplicate multi-megabyte Shader reads on
    every model/tool step. The immediate parent and its pending writes remain
    available while a new checkpoint is being settled. DeltaChannel ancestors
    retain only their delta writes/seed blobs, not old full ordinary channels.
    """

    def put(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        result = super().put(config, checkpoint, metadata, new_versions)
        thread = config["configurable"]["thread_id"]
        namespace = config["configurable"].get("checkpoint_ns", "")
        versions = self.storage[thread][namespace]
        heads = set(sorted(versions)[-2:])
        keep = set(heads)
        referenced = set()
        delta_writes: dict[str, set[str]] = {}
        for identity in heads:
            packed, packed_metadata, parent = versions[identity]
            state = self.serde.loads_typed(packed)
            referenced.update((thread, namespace, channel, version)
                              for channel, version in state["channel_versions"].items())
            meta = self.serde.loads_typed(packed_metadata)
            needed = set(meta.get("counters_since_delta_snapshot") or {})
            # An unchanged delta channel can already reference its seed blob.
            needed = {channel for channel in needed if self.blobs.get(
                (thread, namespace, channel, state["channel_versions"].get(channel)), ("empty", b""))[0] == "empty"}
            while needed and parent in versions:
                keep.add(parent)
                delta_writes.setdefault(parent, set()).update(needed)
                ancestor, _meta, next_parent = versions[parent]
                ancestor_state = self.serde.loads_typed(ancestor)
                for channel in tuple(needed):
                    key = (thread, namespace, channel, ancestor_state["channel_versions"].get(channel))
                    if self.blobs.get(key, ("empty", b""))[0] != "empty":
                        referenced.add(key)
                        needed.remove(channel)
                parent = next_parent
        for identity in tuple(versions):
            if identity not in keep:
                del versions[identity]
        for key in tuple(self.blobs):
            if key[:2] == (thread, namespace) and key not in referenced:
                del self.blobs[key]
        for key in tuple(self.writes):
            if key[:2] == (thread, namespace) and key[2] not in keep:
                del self.writes[key]
            elif key[:2] == (thread, namespace) and key[2] not in heads:
                channels = delta_writes.get(key[2], set())
                self.writes[key] = {identity: value for identity, value in self.writes[key].items()
                                    if value[1] in channels}
        return result
