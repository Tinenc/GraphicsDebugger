from __future__ import annotations

from langgraph.checkpoint.base import empty_checkpoint

from replaylab_agent.agent_checkpoint import LatestCheckpointSaver


def test_latest_checkpoint_bounds_full_history_copies_and_keeps_pending_writes():
    saver = LatestCheckpointSaver()
    config = {"configurable": {"thread_id": "main", "checkpoint_ns": ""}}
    source = "x" * (1024 * 1024)
    messages = [source]
    parent = None
    for step in range(24):
        messages.append(f"followup-{step}")
        checkpoint = empty_checkpoint()
        checkpoint["id"] = f"{step:04d}"
        checkpoint["channel_values"] = {"messages": list(messages)}
        checkpoint["channel_versions"] = {"messages": step}
        parent = config
        config = saver.put(config, checkpoint, {"source": "loop", "step": step, "parents": {}}, {"messages": step})
        saver.put_writes(config, [("messages", "pending-tool-result")], task_id=f"tool-{step}")
    assert len(saver.storage["main"][""]) == 2
    assert len(saver.blobs) == 2
    assert len(saver.writes) == 2
    assert sum(len(blob[1]) for blob in saver.blobs.values()) < 3 * len(source)
    latest = saver.get_tuple(config)
    assert latest.checkpoint["channel_values"]["messages"] == messages
    assert latest.pending_writes == [("tool-23", "messages", "pending-tool-result")]
    assert saver.get_tuple(parent).pending_writes == [("tool-22", "messages", "pending-tool-result")]
