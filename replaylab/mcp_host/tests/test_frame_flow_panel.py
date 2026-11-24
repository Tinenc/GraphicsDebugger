from __future__ import annotations

import ast
from collections import OrderedDict
import copy
import importlib.util
from pathlib import Path
import sys
import types
from unittest import mock

import pytest


MODULE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/project_frame_flow.py"


def load_module():
    package = types.ModuleType("frame_flow_panel_test")
    package.__path__ = [str(MODULE.parent)]
    spec = importlib.util.spec_from_file_location("frame_flow_panel_test.project_frame_flow", MODULE)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"PySide2": None, "frame_flow_panel_test": package}):
        spec.loader.exec_module(module)
    return module


def document(tmp_path):
    return {
        "schema": "bread-frame-render-flow/v1", "flowId": "flow-a",
        "captureSessionId": "session-a", "capturePath": str(tmp_path / "frame.rdc"),
        "captureId": "a" * 64, "title": "Frame flow", "summary": "Three passes",
        "coverage": {"totalActionCount": 3, "coveredActionCount": 3,
                     "omittedActionCount": 0, "complete": True, "sourceComplete": True},
        "stages": [{"id": "lighting", "title": "Lighting", "summary": "Combines light",
                    "confidence": "captured", "startEventId": 8, "endEventId": 12,
                    "eventIds": [8, 10, 12], "representativeEventId": 12,
                    "images": [{"title": "HDR output", "description": "Exposure zero",
                                "eventId": 10, "path": str(tmp_path / "preview.png"),
                                "channel": "RGBA", "format": "RGBA16_FLOAT"}]}],
    }


def test_python_36_and_no_qt_import():
    ast.parse(MODULE.read_text(encoding="utf-8"), feature_version=(3, 6))
    module = load_module()
    assert not module.HAS_QT
    with pytest.raises(RuntimeError, match="PySide2"):
        module.FrameRenderFlowPanel()


def test_document_preserves_capture_order_preview_identity_and_plain_labels(tmp_path):
    module = load_module()
    value = document(tmp_path)
    value["stages"][0]["title"] = "<script>not executable</script>"
    value["stages"][0]["command"] = "not a UI command"
    result = module.normalize_document(value)
    assert result["stages"][0]["title"] == "<script>not executable</script>"
    assert "command" not in result["stages"][0]
    assert result["stages"][0]["images"][0]["eventId"] == 10
    assert result["stages"][0]["images"][0]["presentation"] == "primary"
    assert result["coverage"]["coveredActionCount"] == 3
    result["stages"][0]["eventIds"].append(15)
    assert value["stages"][0]["eventIds"] == [8, 10, 12]
    assert result["stages"][0]["algorithm"] is None


def test_algorithm_keeps_bounded_plain_steps_resource_path_and_cross_stage_destination(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    payload["stages"][0]["algorithm"] = {
        "inputs": [{"eventId": 8, "resourceId": 40, "role": "HDR scene", "producerEventId": 2,
                    "evidence": "Reads the previous color output"}],
        "steps": [{"eventId": 10, "title": "Blur bright pixels", "calculation": "Weighted samples along X",
                   "evidence": "Sample offsets -2..2", "command": "not executable"}],
        "outputs": [{"eventId": 12, "resourceId": 41, "role": "Blurred highlights"}],
        "integration": [{"sourceEventId": 12, "resourceId": 41, "eventId": 18,
                         "targetResourceId": 42, "mode": "downstream",
                         "explanation": "Add the glow to the scene", "evidence": "Color + glow"}]}
    result = module.normalize_document(payload)
    algorithm = result["stages"][0]["algorithm"]
    assert algorithm["integration"][0]["eventId"] == 18
    assert algorithm["inputs"][0]["producerEventId"] == 2
    assert "command" not in algorithm["steps"][0]
    assert module.matching_stage_ids(result, "Weighted samples") == ["lighting"]
    assert module.matching_stage_ids(result, "Blurred highlights") == ["lighting"]
    payload["stages"][0]["algorithm"]["steps"][0]["calculation"] = "x" * 1201
    assert len(module.normalize_document(payload)["stages"][0]["algorithm"]["steps"][0]["calculation"]) == 1200


@pytest.mark.parametrize("algorithm", [[], {"steps": [{}] * 33}, {"steps": [False]},
    {"steps": [{"eventId": 18, "title": "Outside", "calculation": "Wrong stage"}]},
    {"steps": [{"eventId": True, "title": "Bool", "calculation": "Invalid identity"}]},
    {"inputs": [{"eventId": 8, "resourceId": False, "role": "Invalid resource"}]},
    {"integration": [{"sourceEventId": 18, "eventId": 20, "resourceId": 1,
                      "mode": "downstream", "explanation": "Outside producer"}]},
    {"integration": [{"sourceEventId": 8, "eventId": 20, "resourceId": 1,
                      "mode": "execute", "explanation": "Invalid mode"}]}])
def test_algorithm_rejects_unbounded_or_misbound_navigation(tmp_path, algorithm):
    module = load_module()
    payload = document(tmp_path)
    payload["stages"][0]["algorithm"] = algorithm
    with pytest.raises(ValueError):
        module.normalize_document(payload)


@pytest.mark.parametrize("field,value", [("eventIds", [8, 14]), ("representativeEventId", 9),
                                        ("startEventId", True), ("endEventId", 7)])
def test_stage_rejects_misbound_navigation(tmp_path, field, value):
    module = load_module()
    payload = document(tmp_path)
    payload["stages"][0][field] = value
    with pytest.raises(ValueError):
        module.normalize_document(payload)


def test_sequence_does_not_reorder_or_silently_truncate(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    second = copy.deepcopy(payload["stages"][0])
    second["id"] = "second"
    payload["stages"].append(second)
    with pytest.raises(ValueError, match="execution order"):
        module.normalize_document(payload)
    payload["stages"] = payload["stages"] * 129
    with pytest.raises(ValueError, match="256"):
        module.normalize_document(payload)


@pytest.mark.parametrize("path", ["https://host/image.png", "preview.png", "C:/image.svg"])
def test_preview_rejects_url_relative_and_non_png(tmp_path, path):
    module = load_module()
    payload = document(tmp_path)
    payload["stages"][0]["images"][0]["path"] = path
    with pytest.raises(ValueError, match="absolute PNG"):
        module.normalize_document(payload)


def test_preview_must_reference_member_event_and_image_budget_is_explicit(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    payload["stages"][0]["images"][0]["eventId"] = 9
    with pytest.raises(ValueError, match="preview event"):
        module.normalize_document(payload)
    payload["stages"][0]["images"] *= 65
    with pytest.raises(ValueError, match="too many previews"):
        module.normalize_document(payload)


def test_navigation_rejects_same_path_new_session_and_same_session_other_path(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    assert module.binding_matches(payload, payload)
    assert not module.binding_matches(payload, dict(payload, captureSessionId="new-session"))
    assert not module.binding_matches(payload, dict(payload, capturePath=str(tmp_path / "other.rdc")))
    assert not module.binding_matches(payload, None)


def test_search_exact_event_membership_and_semantic_labels_without_changing_coverage(tmp_path):
    module = load_module()
    payload = module.normalize_document(document(tmp_path))
    before = copy.deepcopy(payload)
    for query in ("lighting", "COMBINES", "hdr output", "EID 10", "12"):
        assert module.matching_stage_ids(payload, query) == ["lighting"]
    assert module.matching_stage_ids(payload, "EID 9") == []
    assert module.matching_stage_ids(payload, "bloom") == []
    assert payload == before


def test_execution_order_is_not_numeric_eid_order_and_mapping_is_preserved(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    stage = payload["stages"][0]
    stage.update(startEventId=12, endEventId=10, eventIds=[12, 8, 10])
    stage["images"][0]["displayMapping"] = {
        "rangeMode": "fixed", "blackPoint": 0, "whitePoint": 0.75,
        "mip": 0, "slice": 0, "sample": 1, "attachmentKind": "depth",
        "attachmentIndex": 0, "depthInterpretation": "reversed-z", "unused": "discard"}
    result = module.normalize_document(payload)
    assert result["stages"][0]["eventIds"] == [12, 8, 10]
    mapping = result["stages"][0]["images"][0]["displayMapping"]
    assert mapping["whitePoint"] == "0.75"
    assert mapping["blackPoint"] == mapping["mip"] == mapping["slice"] == "0"
    assert mapping["depthInterpretation"] == "reversed-z"
    assert "unused" not in mapping


def test_verified_display_enums_are_localized_without_hiding_zero_subresources():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    helpers = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and node.name in ("_format", "_mapping_description")]
    calls = []

    def translate(context, source):
        assert context == "ReplayLabProjects"
        calls.append(source)
        return source

    namespace = {"QtCore": types.SimpleNamespace(
        QCoreApplication=types.SimpleNamespace(translate=translate))}
    exec(compile(ast.Module(body=helpers, type_ignores=[]), str(MODULE), "exec"), namespace)
    mapping = {"rangeMode": "native", "blackPoint": 0, "whitePoint": 1,
               "depthInterpretation": "raw-resource-component-0", "mip": 0,
               "slice": 0, "sample": 0, "attachmentKind": "dsv", "attachmentIndex": 0}
    result = namespace["_mapping_description"]({"displayMapping": mapping})
    assert "Original values" in result
    assert "not converted to distance" in result
    assert "MIP 0 · Slice 0 · Sample 0" in result
    assert "Attachment: DSV 0" in result
    mapping.update(rangeMode="subresource-minmax", attachmentKind="rtv")
    result = namespace["_mapping_description"]({"displayMapping": mapping})
    assert "Measured range" in result and "Attachment: RTV 0" in result
    mapping.update(rangeMode="visible-distribution")
    result = namespace["_mapping_description"]({"displayMapping": mapping})
    assert "Visible distribution" in result and "visible-distribution" not in result
    mapping.update(rangeMode="future-mode", depthInterpretation="future-depth")
    result = namespace["_mapping_description"]({"displayMapping": mapping})
    assert "future-mode" in result and "future-depth" in result


def test_mrt_groups_earlier_inputs_and_comparison_are_retained(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    stage = payload["stages"][0]
    stage["images"] = [dict(stage["images"][0], resourceId=str(index)) for index in range(8)]
    stage["images"][6].update(group="channels", channelMeaning="R stores roughness")
    stage["images"][7].update(group="inputs", eventId=2)
    stage["comparisons"] = [{"leftImageIndex": 7, "rightImageIndex": 1,
                             "differentResource": True, "visualComparable": True}]
    stage.update(navigationEventId=10, navigationStatus="last-draw", navigationHasWork=True)
    result = module.normalize_document(payload)["stages"][0]
    assert len(result["images"]) == 8
    assert result["images"][7]["eventId"] == 2
    assert result["comparisons"][0]["differentResource"]
    assert result["navigationEventId"] == 10
    stage["comparisons"][0]["rightImageIndex"] = 20
    with pytest.raises(ValueError, match="existing previews"):
        module.normalize_document(payload)


def test_exact_preview_navigation_converts_verified_mapping_without_losing_zero(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    preview = payload["stages"][0]["images"][0]
    preview.update(resourceId="ResourceId::60", attachmentStage="fragment",
                   displayMapping={"rangeMode": "explicit", "blackPoint": 0,
                                   "whitePoint": 0.25, "mip": 0, "slice": 0,
                                   "sample": 0, "attachmentKind": "rtv", "attachmentIndex": 0})
    result = module.preview_navigation(module.normalize_document(payload)["stages"][0]["images"][0])
    assert result["eventId"] == 10 and result["resourceId"] == "ResourceId::60"
    assert result["attachmentStage"] == "fragment"
    assert result["mip"] == result["slice"] == result["sample"] == result["attachmentIndex"] == 0
    assert result["displayMapping"]["blackPoint"] == 0.0
    assert result["displayMapping"]["whitePoint"] == 0.25


def test_channel_callback_retains_true_channel_files_and_direct_attachment_identity(tmp_path):
    module = load_module()
    parent = module.normalize_document(document(tmp_path))["stages"][0]["images"][0]
    parent.update(resourceId="60", componentCount=4)
    response = [dict(parent, path=str(tmp_path / (channel + ".png")), channel=channel,
                     attachmentKind="rtv", attachmentIndex=2, attachmentStage="fragment")
                for channel in ("a", "b", "r", "g")]
    result = module.normalize_channel_previews(parent, response)
    assert [image["channel"] for image in result] == ["r", "g", "b", "a"]
    assert [Path(image["path"]).name for image in result] == ["r.png", "g.png", "b.png", "a.png"]
    target = module.preview_navigation(result[-1])
    assert (target["attachmentKind"], target["attachmentIndex"], target["attachmentStage"]) == ("rtv", 2, "fragment")
    assert module.channel_previews_complete(parent, result)
    # An opaque RGB preview cannot provide a missing captured alpha channel.
    rgb = [dict(parent, channel="rgb")]
    assert not module.channel_previews_complete(parent, rgb)
    assert module.channel_previews_complete(dict(parent, componentCount=3, channelGroups=[{"channel": "rgb"}]), rgb)
    assert module.channel_previews_complete(dict(parent, componentCount=2), result[:2])


def test_existing_single_component_depth_preview_does_not_request_redundant_channels():
    module = load_module()
    assert module.channel_previews_complete({"componentCount": 1}, [{"channel": "depth"}])
    assert not module.channel_previews_complete({"componentCount": 2}, [{"channel": "depth"}])
    assert not module.channel_previews_complete({"componentCount": 4}, [{"channel": "depth"}])


def channel_view_without_qt(tmp_path, provider=None, existing=()):
    module = load_module()
    clock = [100.0]

    class Popup:
        def __init__(self, _title, channels, _owner):
            self._alive, self.channels, self.loading, self.saved = True, channels, False, False

        def close(self): self._alive = False
        def deleteLater(self): pass
        def set_loading(self): self.loading = True
        def set_channels(self, channels): self.channels, self.loading, self.saved = channels, False, False
        def set_error(self, error): self.error, self.loading = error, False
        def set_incomplete(self): self.incomplete, self.loading = True, False
        def set_saved(self): self.saved, self.loading = True, False

    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    methods = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and node.name in ("_open_channels", "_close_channel_popup", "_channel_revision")]
    namespace = dict(module.__dict__, _ChannelPopup=Popup,
                     time=types.SimpleNamespace(monotonic=lambda: clock[0]))
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(MODULE), "exec"), namespace)
    view_type = type("ChannelView", (), {node.name: namespace[node.name] for node in methods})
    view = view_type()
    parent = module.normalize_document(document(tmp_path))["stages"][0]["images"][0]
    parent.update(resourceId="60", componentCount=4)
    view._stages = {"lighting": types.SimpleNamespace(stage={"title": "Lighting", "images": [parent]},
        _channel_groups={0: [dict(parent, channel=channel) for channel in existing]}, _unassigned_channels=[])}
    view._generation, view._alive = 7, True
    view._channel_cache, view._channel_requests = OrderedDict(), set()
    view._channel_popup, view._channel_popup_key = None, None
    view.channel_revision_provider = provider
    requests = []
    view.channel_request_callback = lambda preview, completion: requests.append((preview, completion))
    return view, requests, parent, clock


def test_channel_cache_replay_revision_discards_late_results_and_refreshes_complete_saved_groups(tmp_path):
    revision = [(1, "cbuffer-a")]
    view, requests, parent, _clock = channel_view_without_qt(tmp_path, lambda: revision[0], ("r", "g", "b", "a"))
    response = {"images": [dict(parent, channel=channel) for channel in ("r", "g", "b", "a")]}
    view._open_channels("lighting", "channels:0")
    assert len(requests) == 1 and view._channel_popup.loading
    old_popup = view._channel_popup
    revision[0] = (2, "cbuffer-b")
    requests[0][1](response)
    assert not view._channel_cache and not view._channel_requests and old_popup._alive
    assert len(old_popup.channels) == 4 and old_popup.saved and not old_popup.loading
    view._open_channels("lighting", "channels:0")
    requests[1][1](response)
    assert len(view._channel_cache) == 1 and not view._channel_popup.loading
    view._open_channels("lighting", "channels:0")
    assert len(requests) == 2
    revision[0] = (2, "cbuffer-c")
    view._open_channels("lighting", "channels:0")
    assert len(requests) == 3 and not view._channel_cache
    view._generation += 1
    requests[2][1](response)
    assert not view._channel_cache and not view._channel_requests


def test_channel_cache_without_provider_preserves_reuse_but_expires_receipts(tmp_path):
    view, requests, parent, clock = channel_view_without_qt(tmp_path)
    response = {"images": [dict(parent, channel=channel) for channel in ("r", "g", "b", "a")]}
    view._open_channels("lighting", "channels:0")
    requests[0][1](response)
    clock[0] += 29
    view._open_channels("lighting", "channels:0")
    assert len(requests) == 1
    clock[0] += 1
    view._open_channels("lighting", "channels:0")
    assert len(requests) == 2 and not view._channel_cache
    saved, unused, _parent, _clock = channel_view_without_qt(tmp_path, existing=("rgb", "a"))
    _parent["channelGroups"] = [{"channel": "rgb"}, {"channel": "a"}]
    saved._open_channels("lighting", "channels:0")
    assert not unused


def test_channel_cache_is_bounded_and_invalidates_capture_generation_and_replay_revision():
    module = load_module()
    cache = OrderedDict(((1, "revision-a", index), (100.0, [index])) for index in range(20))
    module.prune_channel_cache(cache, 1, "revision-a", 110.0)
    assert len(cache) == 16 and next(iter(cache))[-1] == 4
    cache[(0, "revision-a", 90)] = (100.0, [])
    cache[(1, "revision-b", 91)] = (100.0, [])
    cache[(1, "revision-a", 92)] = (80.0, [])
    module.prune_channel_cache(cache, 1, "revision-a", 110.0)
    assert len(cache) == 16 and all(key[:2] == (1, "revision-a") for key in cache)
    assert all(key[-1] < 20 for key in cache)


def test_first_channel_open_replaces_partial_alpha_in_same_popup_and_only_caches_complete_set(tmp_path):
    view, requests, parent, _ = channel_view_without_qt(tmp_path, existing=("a",))
    view._open_channels("lighting", "channels:0")
    popup = view._channel_popup
    assert [image["channel"] for image in popup.channels] == ["a"] and popup.saved and popup.loading and len(requests) == 1
    requests[0][1]({"images": [dict(parent, channel=c) for c in "rgba"], "complete": True})
    assert view._channel_popup is popup and popup._alive
    assert [image["channel"] for image in popup.channels] == list("rgba")
    assert len(view._channel_cache) == 1


def test_partial_channel_response_is_visible_but_retried_not_cached(tmp_path):
    view, requests, parent, _ = channel_view_without_qt(tmp_path, existing=("a",))
    view._open_channels("lighting", "channels:0")
    requests[0][1]({"images": [dict(parent, channel="a")], "complete": False,
                    "errors": [{"channel": "r", "error": "Unavailable"}]})
    assert view._channel_popup.saved
    assert [image["channel"] for image in view._channel_popup.channels] == ["a"]
    assert not view._channel_cache
    view._open_channels("lighting", "channels:0")
    assert len(requests) == 2


def test_failed_refresh_keeps_saved_complete_channels_visible_without_claiming_success(tmp_path):
    view, requests, parent, _ = channel_view_without_qt(tmp_path, existing=tuple("rgba"))
    parent["channelCacheComplete"] = False
    view._open_channels("lighting", "channels:0")
    popup = view._channel_popup
    saved = copy.deepcopy(popup.channels)
    assert len(saved) == 4 and popup.loading and popup.saved
    requests[0][1]({"images": [], "error": "Replay binding unavailable", "complete": False})
    assert popup._alive and popup.channels == saved and popup.saved and not popup.loading
    assert "Replay binding unavailable" in popup.error
    assert not view._channel_cache and not view._channel_requests


def test_partial_new_revision_never_replaces_or_mixes_with_saved_channel_set(tmp_path):
    view, requests, parent, _ = channel_view_without_qt(tmp_path, existing=tuple("rgba"))
    parent["channelCacheComplete"] = False
    view._open_channels("lighting", "channels:0")
    saved = copy.deepcopy(view._channel_popup.channels)
    requests[0][1]({"images": [dict(parent, channel="r", path=str(tmp_path / "new-red.png"))], "complete": False})
    assert view._channel_popup.channels == saved and view._channel_popup.saved
    assert not view._channel_cache


def test_unavailable_revision_or_replay_callback_does_not_block_saved_png_popup(tmp_path):
    def unavailable(): raise RuntimeError("Capture is restoring")
    view, requests, parent, _ = channel_view_without_qt(tmp_path, unavailable, tuple("rgba"))
    view._open_channels("lighting", "channels:0")
    assert len(view._channel_popup.channels) == 4 and view._channel_popup._alive
    assert view._channel_popup.saved and not requests and not view._channel_cache
    assert "Capture is restoring" in view._channel_popup.error
    view.channel_revision_provider = lambda: [3, 0, []]  # JSON-style provider signatures are also safe.
    parent["channelCacheComplete"] = False
    view.channel_request_callback = lambda _parent, _callback: unavailable()
    view._open_channels("lighting", "channels:0")
    assert len(view._channel_popup.channels) == 4 and view._channel_popup.saved
    assert not view._channel_popup.loading and not view._channel_requests and not view._channel_cache


def test_old_capture_session_can_show_saved_receipts_even_if_new_replay_is_unavailable(tmp_path):
    view, requests, parent, _ = channel_view_without_qt(tmp_path, lambda: (1, 0, ()), tuple("rgba"))
    view.document = {"captureSessionId": "restored-session"}
    view._document_channel_revision = (1, 0, ())
    parent.update(channelCacheComplete=True, channelCacheSession="original-session", channelCacheRevision=[99, 0, []])
    view._open_channels("lighting", "channels:0")
    assert len(view._channel_popup.channels) == 4 and not requests


def test_popup_update_failure_does_not_cache_success(tmp_path):
    view, requests, parent, _ = channel_view_without_qt(tmp_path)
    view._open_channels("lighting", "channels:0")
    def fail(_images):
        raise RuntimeError("detached scene failed")
    view._channel_popup.set_channels = fail
    requests[0][1]({"images": [dict(parent, channel=c) for c in "rgba"]})
    assert not view._channel_cache and "detached scene failed" in view._channel_popup.error


def test_channel_groups_preserved_and_groups_not_titles_control_splitting(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    preview = payload["stages"][0]["images"][0]
    preview.update(componentCount=4, channelGroups=[{"channel": "rgb", "label": "Surface normal",
        "description": "Direction", "command": "never execute"}, {"channel": "a", "description": "Coverage"}])
    parent = module.normalize_document(payload)["stages"][0]["images"][0]
    assert "command" not in parent["channelGroups"][0]
    assert module.channel_details_available(parent)
    assert module.channel_previews_complete(parent, [{"channel": "rgb"}, {"channel": "a"}])
    assert not module.channel_previews_complete(parent, [{"channel": channel} for channel in "rgba"])
    assert not module.channel_details_available(dict(parent, componentCount=3))
    assert not module.channel_details_available({"componentCount": 1})
    assert not module.channel_details_available({"componentCount": 4, "channelGroups": []})
    assert module.channel_details_available({"componentCount": 4, "title": "BaseColor"})
    assert [group["channel"] for group in module.channel_plan({"componentCount": 4,
        "channelMeaning": "RGB: lighting; A: coverage"})] == ["rgb", "a"]
    planned = module.planned_channel_previews(parent, [dict(parent, channel="a")])
    assert [image["channel"] for image in planned] == ["rgb", "a"]
    assert planned[0]["path"] == parent["path"] and planned[0]["title"] == "Surface normal"
    assert module.planned_channel_previews(parent, [], allow_parent_rgb=False) == []
    assert [image["channel"] for image in module.planned_channel_previews(parent, [])] == ["rgb"]
    assert module.preview_card_identity(parent) != module.preview_card_identity(dict(parent, channelGroups=[]))


def test_callback_real_three_component_plan_does_not_require_nonexistent_alpha(tmp_path):
    view, requests, parent, _ = channel_view_without_qt(tmp_path)
    parent["componentCount"] = 0
    view._open_channels("lighting", "channels:0")
    requests[0][1]({"images": [dict(parent, channel="rgb")], "componentCount": 3,
                    "channelGroups": [{"channel": "rgb"}], "complete": True})
    assert len(view._channel_cache) == 1
    assert [image["channel"] for image in view._channel_popup.channels] == ["rgb"]


def test_published_channel_receipts_are_shown_immediately_until_replay_mutates(tmp_path):
    revision = [(4, 0, ())]
    view, requests, parent, _ = channel_view_without_qt(tmp_path, lambda: revision[0], ("r", "g", "b", "a"))
    view.document = {"captureSessionId": "active"}
    view._document_channel_revision = revision[0]
    parent.update(channelCacheComplete=True, channelCacheSession="active", channelCacheRevision=[4, 0, []])
    view._open_channels("lighting", "channels:0")
    assert not requests and len(view._channel_popup.channels) == 4
    parent["channelCacheSession"] = "previous-restore-session"
    view._open_channels("lighting", "channels:0")
    assert not requests and len(view._channel_popup.channels) == 4
    revision[0] = (5, 0, ())
    view._open_channels("lighting", "channels:0")
    assert len(requests) == 1 and view._channel_popup.loading and len(view._channel_popup.channels) == 4
    assert view._channel_popup.saved


def test_channel_popup_scene_swap_is_atomic_against_reentrant_scroll_callbacks():
    module = load_module()
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    popup_class = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "_ChannelPopup")
    methods = [node for node in popup_class.body if isinstance(node, ast.FunctionDef) and node.name in ("set_channels", "_load_visible")]
    events, queued = [], []

    class Scene:
        def __init__(self, _view): self.items = []
        def addItem(self, card): self.items.append(card); events.append("add")
        def setSceneRect(self, _x, _y, width, height): self.rect = types.SimpleNamespace(width=lambda: width)
        def sceneRect(self): return self.rect
        def deleteLater(self): events.append("delete-old")

    class Card:
        def __init__(self, preview, *_args, **_kwargs): self.height, self.preview = 100, preview
        def setPos(self, *_args): pass

    available = types.SimpleNamespace(width=lambda: 1600, height=lambda: 1000)
    namespace = dict(module.__dict__, _PreviewItem=Card,
        QtWidgets=types.SimpleNamespace(QGraphicsScene=Scene, QApplication=types.SimpleNamespace(
            desktop=lambda: types.SimpleNamespace(availableGeometry=lambda _owner: available))),
        QtCore=types.SimpleNamespace(QTimer=types.SimpleNamespace(singleShot=lambda _ms, fn: queued.append(fn))))
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(MODULE), "exec"), namespace)
    popup = type("Popup", (), {method.name: namespace[method.name] for method in methods})()
    popup._alive, popup._updating_channels = True, False
    popup.previews = [types.SimpleNamespace(deleted=True)]
    popup.scene = Scene(None)
    popup._owner = types.SimpleNamespace(palette=lambda: None, activate=lambda _: None)
    popup.status = types.SimpleNamespace(setVisible=lambda _: None)
    popup.resize = lambda *_args: None
    popup.isVisible = lambda: False

    def reentrant(*_args):
        assert popup._updating_channels and len(popup.previews) == 4
        popup._load_visible()  # Must return before touching any Qt geometry.
    def commit(scene):
        assert len(scene.items) == 4 and popup.scene is scene
        events.append("commit")
        reentrant()
    popup.view = types.SimpleNamespace(setScene=commit,
        horizontalScrollBar=lambda: types.SimpleNamespace(setValue=reentrant),
        verticalScrollBar=lambda: types.SimpleNamespace(setValue=reentrant),
        viewport=lambda: types.SimpleNamespace(update=lambda: events.append("update")))
    popup.set_channels([{"channel": channel} for channel in "rgba"])
    assert events == ["add"] * 4 + ["commit", "delete-old", "update"]
    assert not popup._updating_channels and len(queued) == 1


def test_grouped_rgb_uses_real_rgba_receipt_but_never_derives_alpha(tmp_path):
    module = load_module()
    parent = module.normalize_document(document(tmp_path))["stages"][0]["images"][0]
    parent.update(resourceId="60", componentCount=4, channelGroups=[{"channel": "rgb"}, {"channel": "a"}])
    rgb = module.normalize_channel_previews(parent, [dict(parent, channel="rgba")])
    assert [image["channel"] for image in rgb] == ["rgb"]
    assert not module.channel_previews_complete(parent, rgb)
    assert [image["channel"] for image in module.planned_channel_previews(parent,
        [dict(parent, channel="rgba"), dict(parent, channel="a")])] == ["rgb", "a"]
    canonical = [dict(parent, channel="rgba"), dict(parent, channel="a")]
    assert module.channel_previews_complete(parent, canonical)
    parent.update(channelCacheComplete=True, channelCacheSession="current", channelCacheRevision=[1, 0, []])
    assert module.saved_channels_current(parent, canonical, "current", (1, 0, ()), (1, 0, ()))


def test_precache_failure_flag_retries_even_when_old_and_new_receipts_cover_every_group(tmp_path):
    view, requests, parent, _ = channel_view_without_qt(tmp_path, existing=("rgba", "a"))
    parent.update(channelGroups=[{"channel": "rgb"}, {"channel": "a"}], channelCacheComplete=False)
    view._open_channels("lighting", "channels:0")
    assert len(requests) == 1 and view._channel_popup.loading
    assert [image["channel"] for image in view._channel_popup.channels] == ["rgb", "a"]
    assert view._channel_popup.saved


def test_legacy_document_does_not_invent_failed_precache_marker(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    parent = module.normalize_document(payload)["stages"][0]["images"][0]
    assert "channelCacheComplete" not in parent
    payload["stages"][0]["images"][0]["channelCacheComplete"] = False
    assert module.normalize_document(payload)["stages"][0]["images"][0]["channelCacheComplete"] is False


@pytest.mark.parametrize("change", [{"eventId": 12}, {"resourceId": "61"},
    {"displayMapping": {"mip": 1}}, {"displayMapping": {"slice": 1}},
    {"displayMapping": {"sample": 1}}, {"path": "https://example.com/a.png"},
    {"channel": "RGBA"}])
def test_channel_callback_rejects_wrong_snapshot_subresource_or_unverified_preview(tmp_path, change):
    module = load_module()
    parent = module.normalize_document(document(tmp_path))["stages"][0]["images"][0]
    parent["resourceId"] = "60"
    with pytest.raises(ValueError):
        module.normalize_channel_previews(parent, [dict(parent, channel="a", **change)]
            if "channel" not in change else [dict(parent, **change)])


def test_channel_callback_does_not_invent_missing_channels(tmp_path):
    module = load_module()
    parent = module.normalize_document(document(tmp_path))["stages"][0]["images"][0]
    parent["resourceId"] = "60"
    result = module.normalize_channel_previews(parent, [dict(parent, channel="a")])
    assert [image["channel"] for image in result] == ["a"]
    assert not module.channel_previews_complete(parent, result)
    with pytest.raises(ValueError, match="repeated"):
        module.normalize_channel_previews(parent, result * 2)


def test_non_image_outputs_and_attachment_level_missing_are_kept(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    payload["stages"][0]["outputCoverage"] = {
        "nonImageOutputs": [{"kind": "uav", "index": 0, "stage": "compute",
                              "resourceId": "99", "resourceType": "buffer"}],
        "missingAttachments": [{"kind": "uav", "index": 1, "stage": "fragment", "resourceId": "99"}]}
    result = module.normalize_document(payload)["stages"][0]["outputCoverage"]
    assert result["nonImageOutputs"][0]["index"] == "0"
    assert result["missingAttachments"][0]["stage"] == "fragment"


def test_channel_groups_bind_to_texture_snapshot_or_explicit_parent(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    base = payload["stages"][0]["images"][0]
    base.update(resourceId="70", displayMapping={"mip": 0, "slice": 0, "sample": 0})
    images = payload["stages"][0]["images"]
    images += [dict(base, group="channels", channel="r"),
               dict(base, group="channels", channel="g", eventId=12),
               dict(base, group="channels", channel="b", displayMapping={"mip": 1}),
               dict(base, group="channels", channel="a", parentImageIndex=0)]
    normalized = module.normalize_document(payload)["stages"][0]["images"]
    groups, unmatched = module.texture_channel_groups(normalized)
    assert [image["channel"] for image in groups[0]] == ["r", "a"]
    assert [image["channel"] for image in unmatched] == ["g", "b"]
    assert all(image["group"] == "channels" for image in unmatched)


def test_channel_detail_order_is_rgba_and_rgb_alpha_stays_logically_grouped(tmp_path):
    module = load_module()
    base = document(tmp_path)["stages"][0]["images"][0]
    images = [dict(base, resourceId="70"), dict(base, resourceId="71")]
    images.extend(dict(base, resourceId="70", group="channels", channel=channel, parentImageIndex=0)
                  for channel in ("b", "a", "r", "g"))
    images.extend(dict(base, resourceId="71", group="channels", channel=channel, parentImageIndex=1)
                  for channel in ("a", "rgb"))
    before = copy.deepcopy(images)
    groups, unmatched = module.texture_channel_groups(images)
    assert [image["channel"] for image in groups[0]] == ["r", "g", "b", "a"]
    assert [image["channel"] for image in groups[1]] == ["rgb", "a"]
    assert not unmatched and images == before


@pytest.mark.parametrize("change", [{"eventId": 12}, {"resourceId": "71"}, {"displayMapping": {"mip": 1}}])
def test_explicit_channel_parent_never_overrides_the_saved_snapshot_identity(tmp_path, change):
    module = load_module()
    parent = dict(document(tmp_path)["stages"][0]["images"][0], resourceId="70")
    child = dict(parent, group="channels", channel="a", parentImageIndex=0, **change)
    grouped, unmatched = module.texture_channel_groups([parent, child])
    assert grouped == {} and unmatched == [child]


def test_detail_presentation_round_trips_without_changing_channel_or_group_semantics(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    payload["stages"][0]["images"][0].update(presentation="detail", sha256="a" * 64)
    preview = module.normalize_document(payload)["stages"][0]["images"][0]
    assert preview["presentation"] == "detail" and preview["group"] == "outputs"
    assert preview["sha256"] == "a" * 64
    payload["stages"][0]["images"][0]["presentation"] = "hidden"
    with pytest.raises(ValueError, match="presentation"):
        module.normalize_document(payload)


def test_explicit_media_suppresses_standalone_duplicates_while_details_fold_together(tmp_path):
    module = load_module()
    base = document(tmp_path)["stages"][0]["images"][0]
    images = [dict(base, presentation="detail"), dict(base),
              dict(base, path=str(tmp_path / "primary.png")),
              dict(base, path=str(tmp_path / "extra.png"), presentation="detail"),
              dict(base, path=str(tmp_path / "input.png"), group="inputs", presentation="detail"),
              dict(base, path=str(tmp_path / "channels.png"), group="channels"),
              dict(base, path=str(tmp_path / "animation.png"), group="animation-frames")]
    groups = module.standalone_preview_groups(images, {0})
    assert groups["outputs"] == [images[2]] and groups["inputs"] == []
    assert groups["details"] == images[3:5]
    assert len(module.standalone_preview_groups([images[2], copy.deepcopy(images[2])], set())["outputs"]) == 1


@pytest.mark.parametrize("changes", [
    {"eventId": 12}, {"resourceId": "other"}, {"channel": "r"}, {"sha256": "b" * 64},
    {"path": "C:/different/preview.png"}, {"displayMapping": {"mip": 1}},
    {"displayMapping": {"slice": 1}}, {"displayMapping": {"sample": 1}},
    {"displayMapping": {"whitePoint": 4}}, {"displayMapping": {"rangeMode": "explicit"}},
    {"title": "Another explanation"}, {"description": "Different use"},
    {"channelMeaning": "Alpha stores coverage"}, {"evidence": "Additional inspected role"}])
def test_duplicate_filter_preserves_time_subresource_exposure_and_distinct_explanations(tmp_path, changes):
    module = load_module()
    base = document(tmp_path)["stages"][0]["images"][0]
    base.update(resourceId="70", sha256="a" * 64)
    other = dict(base, **changes)
    assert len(module.standalone_preview_groups([base, other], set())["outputs"]) == 2
    assert module.standalone_preview_groups([base, other], {0})["outputs"] == [other]


def test_channel_title_uses_only_supplied_role_for_the_selected_channel():
    module = load_module()
    preview = {"title": "Packed material", "channel": "g",
               "channelMeaning": "R: roughness；G: metallic；B: occlusion；A: material flags"}
    assert module.channel_detail_title(preview) == "G: metallic"
    preview.update(channel="a", channelMeaning="coverage", title="A: coverage")
    assert module.channel_detail_title(preview) == "A: coverage"
    preview.update(channel="rgb", channelMeaning="RGB: base color; A: opacity")
    assert module.channel_detail_title(preview) == "RGB: base color"
    preview.update(channel="RGBA", channelMeaning="", title="RGB: base color")
    assert module.channel_detail_title(preview) == "RGB: base color"
    preview.update(channel="b", channelMeaning="R: roughness", title="Measured blue component")
    assert module.channel_detail_title(preview) == "B: Measured blue component"


def test_channel_long_explanation_moves_below_short_heading_without_losing_sentences():
    module = load_module()
    preview = {"title": "Packed material", "channel": "r", "description": "Used by direct lighting.",
               "channelMeaning": "R: Roughness, controls highlight width. Larger values spread reflected light. "
                                 "Also read by indirect lighting; G: Metalness"}
    assert module.channel_detail_title(preview) == "R: Roughness"
    caption = module.channel_detail_caption(preview)
    assert "controls highlight width" in caption
    assert "Larger values spread reflected light" in caption
    assert "Also read by indirect lighting" in caption
    assert "Used by direct lighting" in caption
    assert "Metalness" not in caption
    chinese = dict(preview, channel="a", channelMeaning="A: 透明覆盖率，用于决定颜色和背景的混合比例。后续还会参与边缘抗锯齿。")
    assert module.channel_detail_title(chinese) == "A: 透明覆盖率"
    assert "后续还会参与边缘抗锯齿" in module.channel_detail_caption(chinese)


def test_verbose_channel_title_is_bounded_but_full_prose_remains_in_caption():
    module = load_module()
    prose = "Stores the surface response used when combining the nearby lighting samples"
    preview = {"channel": "b", "channelMeaning": prose}
    assert len(module.channel_detail_title(preview)) <= 31
    assert module.channel_detail_title(preview).endswith("…")
    assert prose in module.channel_detail_caption(preview)
    chinese = "用于控制各个表面在光照合成过程中反射贡献的整体比例"
    preview.update(channelMeaning=chinese)
    assert len(module.channel_detail_title(preview)) <= 19
    assert chinese in module.channel_detail_caption(preview)


@pytest.mark.parametrize("top,height,at_bottom,expected", [
    (0, 500, False, "a"), (700, 500, False, "a"), (1000, 500, False, "b"),
    (1400, 500, False, "c"), (1600, 500, True, "d"), (0, 2500, False, "a")])
def test_directory_tracks_reading_edge_gaps_and_last_short_module(top, height, at_bottom, expected):
    module = load_module()
    bounds = [("a", 20, 1000), ("b", 1050, 1400), ("c", 1450, 1900), ("d", 1950, 2050)]
    assert module.reading_stage_id(bounds, top, height, at_bottom) == expected
    assert module.reading_stage_id([], top, height, at_bottom) == ""


def test_directory_activation_only_scrolls_and_never_calls_event_navigation():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_outline_activated")
    namespace = {"QtCore": types.SimpleNamespace(Qt=types.SimpleNamespace(UserRole=256))}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(MODULE), "exec"), namespace)
    calls = []
    panel = types.SimpleNamespace(_alive=True, view=types.SimpleNamespace(focus_stage=lambda value: calls.append(value)),
                                  _sync_outline=lambda: calls.append("sync"))
    namespace["_outline_activated"](panel, types.SimpleNamespace(data=lambda role: "lighting"))
    assert calls == ["lighting", "sync"]
    panel._alive = False
    namespace["_outline_activated"](panel, types.SimpleNamespace(data=lambda role: "another-capture"))
    assert calls == ["lighting", "sync"]


def test_presentation_omission_keeps_relationship_evidence_and_stage_images(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    stage = payload["stages"][0]
    stage["processGraph"] = branch_graph()
    stage["processGraph"]["nodes"][0].pop("imageIndex")
    stage["visualReview"] = {"graphPresentation": "omit", "command": "not UI code"}
    normalized = module.normalize_document(payload)["stages"][0]
    assert normalized["visualReview"] == {"graphPresentation": "omit"}
    assert [(edge["from"], edge["to"]) for edge in normalized["processGraph"]["edges"]] == [
        (edge["from"], edge["to"]) for edge in stage["processGraph"]["edges"]]
    assert len(normalized["images"]) == 1
    assert module.presented_process_graph(normalized) is None
    normalized["visualReview"] = {}
    assert module.presented_process_graph(normalized) is None
    normalized["processGraph"]["nodes"][0]["imageIndex"] = 0
    assert module.presented_process_graph(normalized) is normalized["processGraph"]


@pytest.mark.parametrize("media", [None, "imageIndex", "comparisonIndex", "animationIndex"])
def test_restored_legacy_diagram_omits_only_all_text_graph_without_losing_data(tmp_path, media):
    module = load_module()
    payload = document(tmp_path)
    stage = payload["stages"][0]
    stage["images"].append(dict(stage["images"][0], eventId=12))
    stage["comparisons"] = [{"leftImageIndex": 0, "rightImageIndex": 1}]
    stage["animations"] = [{"imageIndices": [0, 1]}]
    stage["processGraph"] = branch_graph()
    stage["processGraph"]["nodes"][0].pop("imageIndex")
    if media:
        stage["processGraph"]["nodes"][0][media] = 0
    normalized = module.normalize_document(payload)["stages"][0]
    assert normalized["visualReview"] == {}
    assert len(normalized["processGraph"]["nodes"]) == 4
    assert len(normalized["images"]) == 2
    if media:
        assert module.presented_process_graph(normalized) is normalized["processGraph"]
    else:
        assert module.presented_process_graph(normalized) is None


def _result_flow_stage(tmp_path, events=(10, 12), resources=(70, 70)):
    stage = document(tmp_path)["stages"][0]
    first = stage["images"][0]
    first.update(eventId=events[0], resourceId=str(resources[0]),
                 displayMapping={"mip": 0, "slice": 0, "sample": 0})
    stage["images"] = [first, dict(first, eventId=events[1], resourceId=str(resources[1]),
                                   path=str(tmp_path / "later.png"))]
    stage["processGraph"] = {"nodes": [{"id": "before", "label": "Before", "imageIndex": 0},
        {"id": "after", "label": "After", "imageIndex": 1}],
        "edges": [{"from": "before", "to": "after"}]}
    return stage


@pytest.mark.parametrize("same_resource,input_group", [(True, False), (False, False), (False, True)])
def test_legacy_one_draw_arithmetic_or_mrt_is_an_independent_result_not_pass_flow(tmp_path, same_resource, input_group):
    module = load_module()
    stage = _result_flow_stage(tmp_path, (10, 10), (70, 70 if same_resource else 71))
    if input_group:
        stage["images"][0]["group"] = "inputs"
    stage["visualReview"] = {"graphPresentation": "show"}  # Older review did not distinguish MRT.
    original = copy.deepcopy(stage)
    assert module.presented_process_graph(stage) is None
    groups = module.standalone_preview_groups(stage["images"], set())
    assert len(groups["outputs"]) == (1 if input_group else 2)
    assert stage == original


def test_repeated_exact_snapshot_through_text_nodes_is_not_a_new_processing_stage(tmp_path):
    module = load_module()
    stage = _result_flow_stage(tmp_path, (10, 10))
    stage["images"][1].update(resourceId="ResourceId::70", channel="r",
        displayMapping={"mip": "0", "slice": "0", "sample": "0", "whitePoint": 5})
    stage["processGraph"]["nodes"].insert(1, {"id": "operation", "label": "Calculates", "eventId": 12})
    stage["processGraph"]["edges"] = [{"from": "before", "to": "operation"},
                                       {"from": "operation", "to": "after"}]
    assert module.presented_process_graph(stage) is None
    # Another mip is another version view; do not collapse it by RID alone.
    stage["images"][1]["displayMapping"]["mip"] = 1
    assert module.presented_process_graph(stage) is stage["processGraph"]


def test_same_resource_written_at_distinct_events_retains_its_cross_pass_flow(tmp_path):
    module = load_module()
    stage = _result_flow_stage(tmp_path)
    original = copy.deepcopy(stage)
    assert module.presented_process_graph(stage) is stage["processGraph"]
    assert stage == original


@pytest.mark.parametrize("media", ["comparisonIndex", "animationIndex"])
def test_before_image_may_flow_into_comparison_or_animation_containing_its_real_after_version(tmp_path, media):
    module = load_module()
    stage = _result_flow_stage(tmp_path)
    stage["comparisons"] = [{"leftImageIndex": 0, "rightImageIndex": 1}]
    stage["animations"] = [{"imageIndices": [0, 1]}]
    stage["processGraph"]["nodes"][1] = {"id": "after", "label": "Compare actual versions", media: 0}
    assert module.presented_process_graph(stage) is stage["processGraph"]
    stage["processGraph"]["nodes"][0] = {"id": "before", "label": "Repeated comparison", media: 0}
    assert module.presented_process_graph(stage) is None


def test_parallel_references_to_a_source_do_not_hide_a_valid_fork(tmp_path):
    module = load_module()
    stage = _result_flow_stage(tmp_path, (8, 12), (70, 71))
    stage["processGraph"]["nodes"].append({"id": "parallel", "label": "Parallel", "imageIndex": 1})
    stage["processGraph"]["edges"].append({"from": "before", "to": "parallel"})
    assert module.presented_process_graph(stage) is stage["processGraph"]


def test_unbound_arithmetic_nodes_use_only_explicit_single_draw_document_evidence(tmp_path):
    module = load_module()
    stage = _result_flow_stage(tmp_path, (10, 10))
    stage["processGraph"]["nodes"][0].pop("imageIndex")
    # No evidence that the text calculation occurs in the same draw.
    assert module.presented_process_graph(stage) is stage["processGraph"]
    stage["algorithm"] = {"steps": [{"eventId": 10, "title": "Per-pixel calculation"}]}
    assert module.presented_process_graph(stage) is None


def test_legacy_steps_apply_same_single_draw_rule_and_never_replace_an_omitted_graph(tmp_path):
    module = load_module()
    stage = _result_flow_stage(tmp_path)
    stage["processSteps"] = [{"label": "Input", "imageIndex": 0}, {"label": "Result", "imageIndex": 1}]
    stage["visualReview"] = {"graphPresentation": "omit"}
    assert module.presented_process_steps(stage) == []
    stage.pop("processGraph")
    assert module.presented_process_steps(stage) is stage["processSteps"]
    stage["visualReview"]["processStepsPresentation"] = "omit"
    assert module.presented_process_steps(stage) == []
    stage["visualReview"] = {}
    stage["images"][1]["eventId"] = 10
    assert module.presented_process_steps(stage) == []


def test_visual_review_preserves_both_independent_presentation_choices(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    payload["stages"][0]["visualReview"] = {"graphPresentation": "omit", "processStepsPresentation": "show",
                                          "backendOnly": "not a UI instruction"}
    assert module.normalize_document(payload)["stages"][0]["visualReview"] == {
        "graphPresentation": "omit", "processStepsPresentation": "show"}


def test_internal_semantic_enums_are_hidden_without_erasing_human_channel_explanations():
    module = load_module()
    preview = {"semantic": "interpreted-resource-content", "description": "Stores reflected scene color",
               "channelMeaning": "RGB: reflected light; A: reflection confidence", "channel": "a"}
    caption = module.concise_caption(preview)
    assert "interpreted-resource-content" not in caption
    assert "Stores reflected scene color" in caption and "reflection confidence" in caption
    assert module.channel_detail_title(preview) == "A: reflection confidence"
    assert module.displayed_semantic("screen_space_reflection") == ""
    assert module.displayed_semantic("World-space normal direction") == "World-space normal direction"
    assert module.channel_detail_title({"channel": "r", "semantic": "interpreted-resource-content"}) == "R"


@pytest.mark.parametrize("parent", [True, -1, 7, 1])
def test_channel_parent_rejects_invalid_or_self_reference(tmp_path, parent):
    module = load_module()
    payload = document(tmp_path)
    images = payload["stages"][0]["images"]
    images.append(dict(images[0], group="channels", parentImageIndex=parent))
    with pytest.raises(ValueError, match="channel parent"):
        module.normalize_document(payload)


def test_animation_contract_preserves_evidence_order_and_has_bounded_playback(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    stage = payload["stages"][0]
    stage["images"].append(dict(stage["images"][0], group="animation-frames", eventId=12))
    stage["animations"] = [{"label": "Accumulation", "description": "Each draw adds light",
                             "imageIndices": [0, 1], "frameDurationMs": 400}]
    result = module.normalize_document(payload)["stages"][0]
    assert result["animations"][0]["imageIndices"] == [0, 1]
    assert result["images"][1]["group"] == "animation-frames"
    for indices in ([0], [0, 2], [0, True], list(range(17))):
        stage["animations"][0]["imageIndices"] = indices
        with pytest.raises(ValueError, match="2 to 16"):
            module.normalize_document(payload)
    stage["animations"][0]["imageIndices"] = [0, 1]
    for duration in (True, 0, 99, 5001, 1.25):
        stage["animations"][0]["frameDurationMs"] = duration
        with pytest.raises(ValueError, match="interval"):
            module.normalize_document(payload)


def test_all_host_quality_codes_use_localized_actions_and_unknown_diagnostics_survive():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    helpers = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and node.name in ("_format", "_quality_gap_text")]
    translations = []

    def translate(context, source):
        assert context == "ReplayLabProjects"
        translations.append(source)
        return source

    namespace = {"QtCore": types.SimpleNamespace(
        QCoreApplication=types.SimpleNamespace(translate=translate))}
    exec(compile(ast.Module(body=helpers, type_ignores=[]), str(MODULE), "exec"), namespace)
    host = MODULE.parents[1] / "mcp_host/src/frame_render_flow.py"
    host_trees = [ast.parse(path.read_text(encoding="utf-8")) for path in
                  (host, host.with_name("frame_flow_algorithm.py"))]
    codes = {call.args[0].value for host_tree in host_trees for call in ast.walk(host_tree)
             if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "gap"
             and call.args and isinstance(call.args[0], ast.Constant)}
    assert len(codes) >= 15
    for code in codes:
        result = namespace["_quality_gap_text"]({"code": code, "stageId": "lighting",
            "message": "raw backend diagnostic", "nextAction": "raw backend next step"})
        assert result.startswith("Stage lighting:")
        assert "Diagnostic" not in result and "raw backend" not in result
    unknown = namespace["_quality_gap_text"]({"code": "future-check", "stageId": "depth",
        "message": "actual diagnostic", "nextAction": "next step"})
    assert "Stage depth: Diagnostic future-check: actual diagnostic next step" == unknown
    assert namespace["_quality_gap_text"]({"code": "unrepresented-frame-actions"}) == (
        "Some frame actions are not represented in the diagram.")
    assert namespace["_quality_gap_text"]({"code": "incomplete-action-outline"}) == (
        "The frame action outline is incomplete.")


def test_process_media_references_are_bounded_and_preserved(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    stage = payload['stages'][0]
    stage['images'].append(dict(stage['images'][0], eventId=12))
    stage['comparisons'] = [{'leftImageIndex': 0, 'rightImageIndex': 1}]
    stage['animations'] = [{'imageIndices': [0, 1]}]
    stage['processSteps'] = [{'label': 'Input', 'imageIndex': 0},
        {'label': 'Blur levels', 'animationIndex': 0}, {'label': 'Composite', 'comparisonIndex': 0}]
    result = module.normalize_document(payload)
    assert result['stages'][0]['processSteps'][1]['animationIndex'] == 0
    for invalid in ({'label': 'Bad', 'imageIndex': True}, {'label': 'Bad', 'imageIndex': 9},
                    {'label': 'Bad', 'imageIndex': 0, 'animationIndex': 0}, {'imageIndex': 0}):
        stage['processSteps'][1] = invalid
        with pytest.raises(ValueError, match='process step'):
            module.normalize_document(payload)


def branch_graph():
    return {"nodes": [{"id": "source", "label": "Source", "imageIndex": 0},
            {"id": "blur", "label": "Blur", "eventId": 8},
            {"id": "filter", "label": "Frequency filter", "description": "Keeps selected frequencies"},
            {"id": "merge", "label": "Composite", "eventId": 12}],
        "edges": [{"from": "source", "to": "blur", "label": "Brightness"},
            {"from": "source", "to": "filter"}, {"from": "blur", "to": "merge"},
            {"from": "filter", "to": "merge"}]}


def test_process_graph_keeps_branch_merge_and_searches_algorithm_nodes(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    stage = payload["stages"][0]
    stage["processGraph"] = branch_graph()
    normalized = module.normalize_document(payload)
    graph = normalized["stages"][0]["processGraph"]
    assert module.process_graph_layers(graph) == [["source"], ["blur", "filter"], ["merge"]]
    assert graph["nodes"][0]["imageIndex"] == 0
    assert "eventId" not in graph["nodes"][2]
    assert module.matching_stage_ids(normalized, "frequencies") == ["lighting"]
    locations, bands, height = module.process_graph_layout(graph, {node["id"]: 100 for node in graph["nodes"]})
    assert len(bands) == 1 and height > 224
    assert locations["blur"]["x"] == locations["filter"]["x"]
    assert locations["blur"]["y"] + 100 < locations["filter"]["y"]
    assert locations["source"]["x"] < locations["blur"]["x"] < locations["merge"]["x"]
    assert locations["source"]["y"] == locations["merge"]["y"]
    assert locations["merge"]["x"] + locations["merge"]["width"] <= 1040


@pytest.mark.parametrize("change", ["cycle", "duplicate-edge", "missing-edge-endpoint", "self-edge", "duplicate-node",
    "missing-label", "bad-event", "mismatched-image-event", "bad-index", "bool-index", "multiple-media", "too-many-nodes"])
def test_process_graph_rejects_unsafe_or_ambiguous_connections(tmp_path, change):
    module = load_module()
    payload = document(tmp_path)
    graph = branch_graph()
    if change == "cycle": graph["edges"].append({"from": "merge", "to": "source"})
    elif change == "duplicate-edge": graph["edges"].append(dict(graph["edges"][0]))
    elif change == "missing-edge-endpoint": graph["edges"][0]["to"] = "not-a-node"
    elif change == "self-edge": graph["edges"][0]["to"] = "source"
    elif change == "duplicate-node": graph["nodes"][1]["id"] = "source"
    elif change == "missing-label": graph["nodes"][0].pop("label")
    elif change == "bad-event": graph["nodes"][1]["eventId"] = 900
    elif change == "mismatched-image-event": graph["nodes"][0]["eventId"] = 12
    elif change == "bad-index": graph["nodes"][0]["imageIndex"] = 90
    elif change == "bool-index": graph["nodes"][0]["imageIndex"] = True
    elif change == "multiple-media": graph["nodes"][0]["comparisonIndex"] = 0
    elif change == "too-many-nodes": graph["nodes"] *= 5
    payload["stages"][0]["processGraph"] = graph
    with pytest.raises(ValueError, match="process graph"):
        module.normalize_document(payload)


def test_process_graph_supports_comparison_animation_and_bounded_wrapped_export(tmp_path):
    module = load_module()
    payload = document(tmp_path)
    stage = payload["stages"][0]
    stage["images"].append(dict(stage["images"][0], eventId=12))
    stage["comparisons"] = [{"leftImageIndex": 0, "rightImageIndex": 1}]
    stage["animations"] = [{"imageIndices": [0, 1]}]
    stage["processGraph"] = {"nodes": [
        {"id": "n%d" % index, "label": "Operation %d" % index} for index in range(16)],
        "edges": [{"from": "n%d" % index, "to": "n%d" % (index + 1)} for index in range(15)]}
    stage["processGraph"]["nodes"][0]["comparisonIndex"] = 0
    stage["processGraph"]["nodes"][15]["animationIndex"] = 0
    graph = module.normalize_document(payload)["stages"][0]["processGraph"]
    locations, bands, height = module.process_graph_layout(graph, {node["id"]: 220 for node in graph["nodes"]})
    assert len(bands) == 6 and height == 6 * 320
    assert locations["n3"]["band"] == 1 and locations["n3"]["x"] == 0
    assert all(position["x"] + position["width"] <= 1040 for position in locations.values())
    assert graph["nodes"][0]["comparisonIndex"] == graph["nodes"][-1]["animationIndex"] == 0


def test_legacy_boilerplate_is_hidden_but_real_channel_meanings_remain():
    module = load_module()
    preview = {'format': 'R11G11B10_FLOAT',
        'description': 'R11G11B10_FLOAT，2048×2048；按子资源范围显示。RGBA 不是字节差异或像素贡献证明。',
        'semantic': 'physical-attachment', 'channelMeaning': '保留物理RGBA；各分量精确业务含义未逐项确认。'}
    assert module.concise_caption(preview) == ''
    preview.update(description='Stores surface normals', semantic='Normal buffer',
                   channelMeaning='RGB stores a world-space direction; A stores roughness')
    caption = module.concise_caption(preview)
    assert 'world-space direction' in caption and 'A stores roughness' in caption


def test_export_uses_single_image_then_stage_aligned_bounded_parts(tmp_path):
    module = load_module()
    assert module.export_segments(20000, [5000, 12000, 19980]) == [(0, 20000)]
    segments = module.export_segments(60000, [10000, 22000, 31000, 45000, 59980])
    assert segments == [(0, 22000), (22000, 45000), (45000, 60000)]
    assert all((end-start) * 1120 <= module.MAX_EXPORT_PART_PIXELS for start, end in segments)
    assert module.export_part_paths(str(tmp_path / 'flow'), 1, 1) == [str(tmp_path / 'flow.png')]
    assert [Path(path).name for path in module.export_part_paths(str(tmp_path / 'flow.png'), 3, 1)] == [
        'flow-part001.png', 'flow-part002.png', 'flow-part003.png']
