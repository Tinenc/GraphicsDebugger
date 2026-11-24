"""Real channel extraction, exact ownership and off-UI cache regressions."""
import copy
import importlib
from pathlib import Path
import tempfile
import threading
import types
import unittest

import test_bridge_event_texture_evidence as evidence_fixture


def fixture():
    bridge, controller = evidence_fixture.EventTextureEvidenceBridgeTests()._fixture()
    controller.GetHistogram = lambda *args: [1] * 256
    module = importlib.import_module(bridge.__name__ + ".frame_flow_channels")
    replay = types.SimpleNamespace(BlockInvoke=lambda fn: fn(controller),
                                   GetForegroundInvokeGeneration=lambda: 3)
    bridge._frame_render_flow_generation = 5
    snapshot = {"session": "a" * 32, "originalEvent": 7, "replay": replay,
                "flowGeneration": 5, "foregroundGeneration": 3,
                "mutationEpoch": module.mutation_identity(bridge)}
    parent = {"eventId": 41, "resourceId": "101", "title": "Surface properties",
              "description": "Used by lighting", "sha256": "b" * 64,
              "channelMeaning": "",
              "displayMapping": {"mip": "1", "slice": "2", "sample": "1",
                                 "attachmentKind": "rtv", "attachmentIndex": "0"}}
    return bridge, controller, module, snapshot, parent


class Executor:
    def __init__(self):
        self.jobs, self.cancelled = [], []

    def submit(self, owner, operation, callback):
        self.jobs.append((owner, operation, callback))

    def run(self):
        owner, operation, callback = self.jobs.pop(0)
        outcome = []
        def work():
            try:
                outcome.append((operation(), ""))
            except Exception as error:
                outcome.append((None, str(error)))
        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
        if owner not in self.cancelled:
            callback(*outcome[0])

    def cancel_owner(self, owner):
        self.cancelled.append(owner)


class FrameFlowChannelTests(unittest.TestCase):
    def test_orientation_separates_channel_cache_and_survives_all_exported_channels(self):
        bridge, controller, module, snapshot, parent = fixture()
        native = module.request_identity(parent)
        parent["displayMapping"]["flipY"] = False
        self.assertEqual(native, module.request_identity(parent))
        parent["displayMapping"]["flipY"] = True
        self.assertTrue(module.request_identity(parent)["flip_y"])
        self.assertNotEqual(native, module.request_identity(parent))
        with tempfile.TemporaryDirectory() as root:
            images = self.capture(bridge, controller, module, snapshot, parent, root)
        self.assertEqual(len(images), 4)
        self.assertTrue(all(image["displayMapping"]["flipY"] is True for image in images))
        for invalid in (1, 0, "true", None):
            parent["displayMapping"]["flipY"] = invalid
            with self.assertRaisesRegex(ValueError, "orientation"):
                module.request_identity(parent)

    def test_value_decode_is_part_of_channel_request_and_cache_identity(self):
        _, _, module, _, parent = fixture()
        native = module.request_identity(parent)
        parent["displayMapping"]["valueDecode"] = "float32-bits"
        decoded = module.request_identity(parent)
        self.assertNotEqual(native, decoded)
        self.assertEqual(decoded["value_decode"], "float32-bits")
        parent["displayMapping"]["valueDecode"] = "guess-depth"
        with self.assertRaises(ValueError):
            module.request_identity(parent)

    def capture(self, bridge, controller, module, snapshot, parent, root, semantics=None, cancelled=lambda: False):
        bridge._frame_flow_artifact_root = lambda: Path(root)
        # Accessing Qt from the worker would fail, including the legacy visible
        # EID getter. Batch mode must use the UI-thread snapshot instead.
        bridge._ctx.CurEvent = lambda: self.fail("worker consulted Qt CurEvent")
        return module.capture_channels(bridge, parent, snapshot, semantics or {}, cancelled)["images"]

    def test_rgba_batch_selects_and_restores_once_and_keeps_semantics(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        meanings = {"a": {"title": "Surface roughness", "description": "Blurs reflections",
                          "channelMeaning": "Roughness", "evidence": "consumer shader sample.a"}}
        with tempfile.TemporaryDirectory() as root:
            images = self.capture(bridge, ctrl, module, snapshot, parent, root, meanings)
            self.assertEqual([image["channel"] for image in images], list("rgba"))
            self.assertEqual(ctrl.events, [(41, True), (7, True)])
            self.assertEqual([item[0].channelExtract for item in ctrl.saved], [0, 1, 2, 3])
            self.assertEqual(images[3]["description"], "Blurs reflections")
            self.assertEqual(images[3]["title"], "Surface roughness")
            self.assertEqual(images[3]["evidence"], "consumer shader sample.a")
            self.assertEqual(images[0]["channelMeaning"], "")
            for image in images:
                module._verify_png(image)
                self.assertEqual(image["displayMapping"]["sample"], 1)
                self.assertEqual(image["componentCount"], 4)

    def test_actual_rgb_format_does_not_export_or_invent_alpha(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        textures = ctrl.GetTextures()
        textures[0].format.compCount = 3
        ctrl.GetTextures = lambda: textures
        parent["componentCount"] = 4  # stale metadata cannot override the native format
        with tempfile.TemporaryDirectory() as root:
            images = self.capture(bridge, ctrl, module, snapshot, parent, root)
            self.assertEqual([image["channel"] for image in images], list("rgb"))
            self.assertEqual(len(ctrl.saved), 3)

    def test_depth_uses_one_real_depth_channel(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        parent.update(resourceId="202", displayMapping={"mip": "0", "slice": "0", "sample": "0",
                      "attachmentKind": "dsv", "attachmentIndex": "0"})
        with tempfile.TemporaryDirectory() as root:
            images = self.capture(bridge, ctrl, module, snapshot, parent, root)
            self.assertEqual([image["channel"] for image in images], ["depth"])
            self.assertEqual(images[0]["componentCount"], 1)
            self.assertEqual(ctrl.events, [(41, True), (7, True)])

    def test_flat_channel_is_still_a_real_capture(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        ctrl.GetMinMax = lambda *args: (types.SimpleNamespace(floatValue=[1.0] * 4),) * 2
        ctrl.GetHistogram = lambda *args: [0] * 255 + [32]
        with tempfile.TemporaryDirectory() as root:
            images = self.capture(bridge, ctrl, module, snapshot, parent, root)
            self.assertEqual(len(images), 4)
            self.assertTrue(all(image["displayMapping"]["blackPoint"] == 0 for image in images))
            self.assertTrue(all(image["displayMapping"]["whitePoint"] == 1 for image in images))
            self.assertEqual(ctrl.events, [(41, True), (7, True)])

    def test_wrong_binding_rejects_before_save_and_restores(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        ctrl.color.resource = 202
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                self.capture(bridge, ctrl, module, snapshot, parent, root)
            self.assertFalse(ctrl.saved)
            self.assertEqual(ctrl.events, [(41, True), (7, True)])

    def test_session_change_mid_batch_cannot_publish_partial_images(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        save = ctrl.SaveTexture
        def changing_save(*args):
            result = save(*args)
            bridge._capture_session_id = "c" * 32
            return result
        ctrl.SaveTexture = changing_save
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, "capture session changed|superseded"):
                self.capture(bridge, ctrl, module, snapshot, parent, root)
            self.assertEqual(len(ctrl.saved), 1)
            self.assertEqual(ctrl.events[-1], (7, True))

    def test_cancelled_owner_does_not_select_event_or_touch_artifact_root(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        bridge._frame_flow_artifact_root = lambda: self.fail("cancelled request touched disk")
        with self.assertRaisesRegex(RuntimeError, "superseded"):
            module.capture_channels(bridge, parent, snapshot, {}, lambda: True)
        self.assertFalse(ctrl.events)

    def test_mid_batch_cancel_restores_without_publishing_partial_channels(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        cancelled = threading.Event()
        save = ctrl.SaveTexture
        def cancelling_save(*args):
            result = save(*args)
            cancelled.set()
            return result
        ctrl.SaveTexture = cancelling_save
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                self.capture(bridge, ctrl, module, snapshot, parent, root, cancelled=cancelled.is_set)
            self.assertEqual(len(ctrl.saved), 1)
            self.assertEqual(ctrl.events[-1], (7, True))

    def test_restore_failure_cannot_return_success(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        select = ctrl.SetFrameEvent
        def select_with_restore_failure(eid, force):
            if eid == 7:
                raise RuntimeError("restore failed")
            select(eid, force)
        ctrl.SetFrameEvent = select_with_restore_failure
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, "restore failed"):
                self.capture(bridge, ctrl, module, snapshot, parent, root)

    def test_shader_or_cbuffer_mutation_invalidates_queued_capture(self):
        for kind in ("shader", "cbuffer", "release"):
            with self.subTest(kind=kind):
                bridge, ctrl, module, snapshot, parent = fixture()
                if kind == "shader":
                    bridge._shared_shader_mutation_epoch += 1
                elif kind == "cbuffer":
                    bridge._event_cbuffer_snapshots["test"] = {"revision": 1, "currentRawSha256": "f" * 64}
                else:
                    bridge._event_cbuffer_release_serial += 1
                with tempfile.TemporaryDirectory() as root:
                    with self.assertRaisesRegex(RuntimeError, "superseded"):
                        self.capture(bridge, ctrl, module, snapshot, parent, root)
                    self.assertFalse(ctrl.events)

    def test_mutation_after_worker_completion_rejects_old_cache_on_ui(self):
        bridge, _, module, snapshot, parent = fixture()
        executor, results = Executor(), []
        def capture(*args):
            bridge._shared_shader_mutation_epoch += 1
            return [{"channel": "r"}]
        loader = module.ChannelPreviewLoader(executor, capture, lambda: bridge._capture_session_id,
                                             current_mutation=lambda: module.mutation_identity(bridge))
        loader.request(parent, snapshot, {}, results.append)
        executor.run()
        self.assertEqual(results[0]["images"], [])
        self.assertIn("changed", results[0]["error"])
        self.assertFalse(loader.cache)

    def test_user_event_change_while_queued_does_not_restore_stale_eid(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        def invoke(operation):
            snapshot["replay"].GetForegroundInvokeGeneration = lambda: 4
            operation(ctrl)
        snapshot["replay"].BlockInvoke = invoke
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                self.capture(bridge, ctrl, module, snapshot, parent, root)
            self.assertFalse(ctrl.events)

    def test_semantics_are_taken_from_exact_parent_not_other_eid(self):
        _, _, module, _, parent = fixture()
        alpha = dict(copy.deepcopy(parent), channel="a", group="channels", parentImageIndex=0,
                     title="Roughness", description="Surface scattering")
        wrong = dict(copy.deepcopy(alpha), eventId=42, title="Wrong")
        meanings = module.existing_channel_semantics(
            {"stages": [{"images": [parent, alpha, wrong]}]}, parent)
        self.assertEqual(meanings["a"]["title"], "Roughness")

    def test_worker_cache_rechecks_hash_deduplicates_and_completes_on_ui(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        executor, calls, results = Executor(), [], []
        ui_thread = threading.get_ident()
        with tempfile.TemporaryDirectory() as root:
            def capture(*args):
                calls.append(threading.get_ident())
                return self.capture(bridge, ctrl, module, snapshot, parent, root)
            loader = module.ChannelPreviewLoader(executor, capture, lambda: bridge._capture_session_id)
            def complete(value):
                self.assertEqual(threading.get_ident(), ui_thread)
                results.append(value)
            loader.request(parent, snapshot, {}, complete)
            loader.request(parent, snapshot, {}, complete)
            self.assertEqual(len(executor.jobs), 1)
            self.assertFalse(ctrl.saved)
            executor.run()
            self.assertEqual(len(results), 2)
            self.assertNotEqual(calls[0], ui_thread)
            loader.request(parent, snapshot, {}, complete)
            executor.run()
            self.assertEqual(len(calls), 1)
            Path(results[0]["images"][0]["path"]).write_bytes(b"\x89PNG\r\n\x1a\ntampered")
            loader.request(parent, snapshot, {}, complete)
            executor.run()
            self.assertEqual(len(calls), 2)
            self.assertFalse(results[-1]["error"])

    def test_owner_cancel_drops_queued_work_and_callbacks(self):
        _, _, module, snapshot, parent = fixture()
        executor = Executor()
        loader = module.ChannelPreviewLoader(executor, lambda *args: self.fail("cancelled capture ran"),
                                             lambda: snapshot["session"])
        loader.request(parent, snapshot, {}, lambda value: self.fail("cancelled completion ran"))
        loader.close()
        executor.run()
        self.assertFalse(loader.cache)

    def test_lru_cache_is_bounded_and_resource_request_keys_are_separate(self):
        _, _, module, snapshot, parent = fixture()
        executor, results = Executor(), []
        loader = module.ChannelPreviewLoader(executor, lambda *args: [], lambda: snapshot["session"], maximum=2)
        for eid in (41, 42, 43):
            loader.request(dict(parent, eventId=eid), snapshot, {}, results.append)
            executor.run()
        self.assertEqual(len(loader.cache), 2)
        self.assertEqual(len(results), 3)

    def test_cache_does_not_survive_capture_change(self):
        _, _, module, snapshot, parent = fixture()
        session, calls = [snapshot["session"]], []
        executor = Executor()
        loader = module.ChannelPreviewLoader(executor, lambda *args: self.fail("stale capture ran"), lambda: session[0])
        loader.request(parent, snapshot, {}, calls.append)
        session[0] = "c" * 32
        executor.run()
        self.assertFalse(calls)
        self.assertFalse(loader.cache)

    def test_same_image_with_changed_meaning_has_distinct_cache_entry(self):
        _, _, module, snapshot, parent = fixture()
        executor, meanings = Executor(), []
        def capture(image, *args):
            meanings.append(image["channelMeaning"])
            return []
        loader = module.ChannelPreviewLoader(executor, capture, lambda: snapshot["session"])
        loader.request(parent, snapshot, {}, lambda value: None)
        executor.run()
        updated = dict(parent, channelMeaning="R: Visibility; G: History weight")
        loader.request(updated, snapshot, {}, lambda value: None)
        executor.run()
        self.assertEqual(meanings, [parent["channelMeaning"], updated["channelMeaning"]])

    def test_plan_distinguishes_color_and_packed_components_and_partial_a(self):
        _, _, module, _, parent = fixture()
        rgb = dict(parent, componentCount=4, channelGroups=[
            {"channel": "rgb", "label": "Base color"}, {"channel": "a", "label": "Coverage"}])
        self.assertEqual([group["channel"] for group in module.channel_plan(rgb)], ["rgb", "a"])
        packed = dict(parent, componentCount=4, channelGroups=[{"channel": "a", "label": "Roughness"}])
        self.assertEqual([group["channel"] for group in module.channel_plan(packed)], list("rgba"))
        self.assertEqual(module.channel_plan(dict(rgb, channelGroups=[])), [])
        self.assertEqual(module.channel_plan(dict(parent, componentCount=1)), [{"channel": "r"}])
        self.assertEqual([group["channel"] for group in module.channel_plan(rgb, 3)], ["rgb"])
        with self.assertRaisesRegex(ValueError, "overlap"):
            module.channel_plan(dict(parent, channelGroups=[{"channel": "rgb"}, {"channel": "r"}]))

    def test_semantic_rgb_capture_uses_real_opaque_rgb_and_independent_alpha(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        parent["channelGroups"] = [{"channel": "rgb", "label": "Normal", "description": "Surface orientation"},
                                   {"channel": "a", "label": "Roughness"}]
        with tempfile.TemporaryDirectory() as root:
            images = self.capture(bridge, ctrl, module, snapshot, parent, root)
            self.assertEqual([image["channel"] for image in images], ["rgb", "a"])
            self.assertEqual([item[0].channelExtract for item in ctrl.saved], [-1, 3])
            self.assertEqual(ctrl.saved[0][0].alpha, "discard")
            self.assertEqual(images[0]["description"], "Surface orientation")
            self.assertEqual(ctrl.events, [(41, True), (7, True)])

    def test_partial_generation_retains_success_and_retry_only_captures_missing(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        executor, results = Executor(), []
        save = ctrl.SaveTexture
        fail_once = [True]
        def save_with_failure(spec, path):
            if spec.channelExtract == 1 and fail_once[0]:
                fail_once[0] = False
                raise RuntimeError("transient export failure")
            return save(spec, path)
        ctrl.SaveTexture = save_with_failure
        with tempfile.TemporaryDirectory() as root:
            bridge._frame_flow_artifact_root = lambda: Path(root)
            loader = module.ChannelPreviewLoader(executor,
                lambda image, state, semantics, cancel: module.capture_channels(bridge, image, state, semantics, cancel),
                lambda: bridge._capture_session_id)
            loader.request(parent, snapshot, {}, results.append)
            executor.run()
            self.assertEqual([image["channel"] for image in results[-1]["images"]], ["r", "b", "a"])
            self.assertFalse(results[-1]["complete"])
            self.assertEqual(results[-1]["errors"][0]["channel"], "g")
            loader.request(parent, snapshot, {}, results.append)
            executor.run()
            self.assertTrue(results[-1]["complete"])
            self.assertEqual([image["channel"] for image in results[-1]["images"]], list("rgba"))
            self.assertEqual([item[0].channelExtract for item in ctrl.saved], [0, 2, 3, 1])
            self.assertEqual(results[-1]["componentCount"], 4)

    def test_host_batch_returns_restored_receipts_and_plan_for_persistence(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        bridge._request_capture_session_id = lambda: snapshot["session"]
        bridge._request_capture_loaded = lambda: True
        bridge._request_visible_event_id = lambda unused=0: 7
        bridge._foreground_replay_manager = lambda: snapshot["replay"]
        parent["channelGroups"] = [{"channel": "rgb", "description": "Normal"}, {"channel": "a", "description": "Roughness"}]
        with tempfile.TemporaryDirectory() as root:
            requests = []
            for ordinal, channel in enumerate(("rgb", "r", "g", "b", "a"), 1):
                request_id = ("%032x" % ordinal)
                directory = Path(root) / ("eid-41-" + request_id)
                directory.mkdir()
                requests.append({"channel": channel, "request_id": request_id, "artifact_dir": str(directory)})
            result = bridge._capture_frame_flow_channels({"expected_capture_session_id": snapshot["session"],
                "items": [{"key": "light:0", "preview": parent, "requests": requests}]})
            item = result["items"][0]
            self.assertEqual([receipt["channel"] for receipt in item["receipts"]], ["rgb", "a"])
            self.assertEqual(item["receipts"][0]["receipt"]["artifact"]["display"]["channel"], "rgba")
            self.assertTrue(all(receipt["receipt"]["restoration"] == {"originalEventId": 7, "restored": True}
                                for receipt in item["receipts"]))
            self.assertTrue(item["complete"])
            self.assertEqual(result["replayRevision"], module.mutation_identity(bridge))
            self.assertEqual(ctrl.events, [(41, True), (7, True)])

    def test_host_single_group_and_no_detail_plans_do_not_replay(self):
        for count, groups in ((1, None), (3, [{"channel": "rgb"}]), (4, [])):
            with self.subTest(count=count, groups=groups):
                bridge, ctrl, module, snapshot, parent = fixture()
                textures = ctrl.GetTextures()
                textures[0].format.compCount = count
                ctrl.GetTextures = lambda: textures
                if groups is not None:
                    parent["channelGroups"] = groups
                value = module.capture_channel_batch(bridge, parent, snapshot, {}, lambda: False, requests={})
                self.assertEqual(value["componentCount"], count)
                self.assertTrue(value["complete"])
                self.assertFalse(value["receipts"])
                self.assertFalse(ctrl.events)

    def test_generated_semantic_groups_survive_capture_bundle_save_and_load(self):
        bridge, ctrl, module, snapshot, parent = fixture()
        persistence = importlib.import_module(bridge.__name__ + ".frame_flow_persistence")
        parent["channelGroups"] = [{"channel": "rgb", "description": "Base color"}, {"channel": "a", "description": "Coverage"}]
        with tempfile.TemporaryDirectory() as root:
            capture = Path(root) / "frame.rdc"
            capture.write_bytes(b"capture fixture")
            images = self.capture(bridge, ctrl, module, snapshot, parent, root)
            outline = {"schema": "bread-frame-render-flow-outline/v1", "captureSessionId": snapshot["session"],
                "capturePath": str(capture), "actions": [{"eventId": 41, "executionIndex": 0}],
                "summary": {"complete": True, "totalActionCount": 1, "omittedActionCount": 0}}
            document = {"schema": "bread-frame-render-flow/v1", "captureSessionId": snapshot["session"],
                "snapshotId": persistence.digest(persistence.canonical_outline(outline)),
                "stages": [{"images": images}]}
            saved = persistence.save(str(capture), document, outline, Path(root))
            self.assertTrue(saved["saved"])
            for image in images:
                Path(image["path"]).unlink()
            restored = persistence.load(str(capture), Path(root))["document"]["stages"][0]["images"]
            self.assertEqual([image["channel"] for image in restored], ["rgb", "a"])
            self.assertEqual(restored[1]["channelMeaning"], "Coverage")
            for image in restored:
                self.assertTrue(Path(image["path"]).is_file())
                self.assertIn("frame.rdc.bread", image["path"])
                module._verify_png(image)


if __name__ == "__main__":
    unittest.main()
