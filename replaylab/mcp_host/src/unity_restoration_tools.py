"""MCP orchestration for real Unity restore/check/repair/render operations."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
import uuid

from .unity_restoration import RestorationStore, package_bundle
from .unity_shader_sources import collect_organized_sources
from .shader_workspace.artifact_reader import ShaderWorkspaceArtifactReader


def _data(result: dict) -> dict:
    if result.get("success") is False or result.get("error"):
        raise ValueError("Unity operation failed: " + str(result.get("error") or result.get("message") or result)[:2000])
    data = result.get("data", result)
    if not isinstance(data, dict):
        raise ValueError("Unity operation did not return structured evidence")
    return data


def register_tools(mcp: Any, host: Any) -> dict:
    """Use the Host's capture/workspace services and ordinary instrumented tools."""
    store = RestorationStore()
    functions = {}
    from .unity_sequence import UnitySequenceStore
    sequences = UnitySequenceStore(store)

    def tool(function):
        wrapped = mcp.tool()(function)
        functions[function.__name__] = wrapped
        return wrapped

    def failure(restoration_id, name, error):
        try:
            # Keep the persistent evidence honest even when these registered
            # tools run without the Agent task-ledger instrumentation.
            if name == "review_unity_restoration":
                store.invalidate_review(restoration_id)
            else:
                store.invalidate(restoration_id, "latest-restoration-operation-failed")
            store.record_failure(restoration_id, name, error)
        except (OSError, ValueError):
            pass
        return host._err(error)

    def reference_identity_loader():
        # Saved-source reads must not wait for an absent/busy application or
        # rebind to an unrelated foreground capture just to obtain display names.
        client = getattr(host, "ipc", None)
        if client is not None:
            owner = client.bound_instance_state()
            if not owner or not owner.get("alive") or not owner.get("heartbeatFresh"):
                return None
        return getattr(host, "_shader_organization_cbuffer_alias_identity", None)

    @tool
    async def plan_unity_restoration_sequence(project_path: str, event_ids: list[int] | None = None,
                                               event_ranges: list[list[int]] | None = None) -> str:
        """List the actual draw/dispatch EIDs in requested inclusive ranges, in capture order.

        Optional progress aid, never an execution lock. Reuses existing per-EID
        tasks. The Agent chooses order/dependencies and may use raw export/project
        APIs directly. No compiler, import or visual validation is run here.
        """
        try:
            context, _, _, _ = await asyncio.to_thread(host._resource_export_live_identity)
            actions = await asyncio.to_thread(host.ipc.call, "get_draw_calls", {"only_actions": True, "include_children": True})
            return host._ok_compact(await asyncio.to_thread(sequences.create, context["capture"], project_path,
                actions, event_ids or [], event_ranges or []))
        except Exception as error:
            return host._err(error)

    @tool
    async def get_unity_restoration_step(sequence_id: str, event_id: int = 0, include_dependencies: bool = False) -> str:
        """Read one selected EID's actual bindings/state and optional resource-use graph.

        event_id=0 suggests the next unannotated step; any EID in the scope can
        be read at any time. No previous-step acceptance, version, compilation
        or binding receipt is required. Use dependency reads for postprocessing;
        verify actual prior writes, subresources, depth and blending yourself.
        """
        try:
            plan = await asyncio.to_thread(sequences.status, sequence_id)
            selected = next((row for row in plan["actions"] if row["eventId"] == event_id), None) if event_id else plan["current"]
            if selected is None:
                return host._ok_compact({"plan": plan, "diagnostic": "No pending EID; select any listed EID to inspect again"})
            # Read APIs already freeze their live request session. Historical
            # sequence/session identifiers are not supplied as replay authority.
            inspection = json.loads(await asyncio.to_thread(host.inspect_resource_export, selected["eventId"]))
            pipeline = json.loads(await asyncio.to_thread(host.get_pipeline_state, selected["eventId"]))
            result = {"sequenceId": sequence_id, "step": selected, "inspection": inspection,
                "pipelineState": pipeline,
                "orderIsAdvisory": True, "nextEventIds": [row["eventId"] for row in plan["actions"] if row["eventId"] > selected["eventId"]],
                "suggestedActions": ["Read this EID's render state and captured inputs", "Reuse unchanged Shader/assets/proofs",
                    "Prepare in Bread where feasible; continue in Unity if tools are unavailable", "Import/adapt, render and adjust in the shared scene", "Annotate real results and remaining gaps; choose the next EID"]}
            if include_dependencies:
                result["resourceGraph"] = json.loads(await asyncio.to_thread(host.get_event_resource_graph, selected["eventId"], 2, 32))
            return host._ok_compact(result)
        except Exception as error:
            return host._err(error)

    @tool
    async def annotate_unity_restoration_step(sequence_id: str, event_id: int, status: str,
                                                note: str, evidence: list[str] | None = None) -> str:
        """Save the Agent's progress/evidence note; no automatic acceptance is claimed.

        status: pending|working|ready-for-next|deferred. Record missing checks and
        real output paths so continuation reuses work. This does not run checks
        or require passing managed restoration gates to move to another EID.
        """
        try:
            return host._ok_compact(await asyncio.to_thread(sequences.annotate, sequence_id, event_id, status, note, evidence or []))
        except Exception as error:
            return host._err(error)

    @tool
    async def plan_unity_restoration_recovery(restoration_id: str, refresh_target: bool = True) -> str:
        """Choose executable alternatives for missing tools, platform APIs or captured inputs.

        Reads actual Editor capabilities when connected. Returns version-specific
        compute/raster/CPU/native/input-history routes with authoring, execution,
        inspection and research steps. A missing built-in route is not a task stop.
        Inferred data keeps its provenance; this never marks failed code or images valid.
        """
        try:
            from .unity_integration import shared_service
            state = store.load(restoration_id)
            diagnostic = None
            if refresh_target:
                try:
                    caps = _data(await shared_service().call_tool(state["projectPath"], "bread_inspect_rendering", {}))
                    with store.transaction(restoration_id) as current:
                        current["targetCapabilities"] = caps
                except Exception as error:
                    diagnostic = str(error)[:2000]
                    store.record_failure(restoration_id, "bread_inspect_rendering", error)
            result = await asyncio.to_thread(store.status, restoration_id)
            if diagnostic:
                result["capabilityInspectionError"] = diagnostic
            return host._ok_compact(result)
        except Exception as error:
            return failure(restoration_id, "plan_unity_restoration_recovery", error)

    @tool
    async def apply_unity_recovery_assets(restoration_id: str, assets: list[dict[str, Any]], purpose: str,
                                          expected_revision: str | None = None,
                                          provenance: dict[str, Any] | None = None,
                                          route_id: str = "custom-versioned-adapter", refresh_editor: bool = True) -> str:
        """Write real project-local Shader/ComputeShader/C#/input files, then refresh Unity.

        Works even when a third-party MCP lacks file-authoring tools. Assets use
        relative path and content, contentBase64, or sourcePath+sha256. Revisions
        protect concurrent edits. Generated/history inputs must state their inference.
        provenance.classification uses ported-code, platform-adapter, inferred-input,
        history-reconstruction or generated-input; include a concrete rationale.
        Follow compiler diagnostics with a concrete repair, configure the component
        or pass, check and render. Written files alone do not complete restoration.
        """
        try:
            from .unity_recovery import write_recovery_assets
            from .unity_integration import shared_service
            state = await asyncio.to_thread(store.status, restoration_id)
            plan = state.get("recoveryPlan") or {}
            if route_id not in {row["id"] for row in plan.get("routes", [])}:
                raise ValueError("Read the current recovery plan and choose an available route id")
            result = await asyncio.to_thread(write_recovery_assets, state["projectPath"], restoration_id,
                assets, purpose, expected_revision, provenance)
            if result.get("changedFiles"):
                await asyncio.to_thread(store.invalidate, restoration_id, "authored-recovery-implementation")
            failed = False
            if refresh_editor:
                try:
                    result["editorRefresh"] = _data(await shared_service().call_tool(state["projectPath"], "bread_refresh_assets", {}))
                except Exception as error:
                    failed = True
                    result["error"] = str(error)[:2000]
                    store.record_failure(restoration_id, "bread_refresh_assets", error)
            current = await asyncio.to_thread(store.record_recovery_attempt, restoration_id, route_id,
                plan.get("contextFingerprint", ""), result, failed=failed)
            current["assetAuthoring"] = result
            return host._ok_compact(current)
        except Exception as error:
            return failure(restoration_id, "apply_unity_recovery_assets", error)

    @tool
    async def record_unity_reconstruction(restoration_id: str, records: list[dict[str, Any]]) -> str:
        """Bind inferred history/layout/input or Unity-native alternatives to actual implementation.

        Each record needs id, exact sourceGapIds from current status, classification
        (inferred-history/inferred-layout/generated-input/unity-native/platform-adapter),
        reason, evidence strings and absolute implementationFiles under project Assets.
        verification selects pass (passId), runtime-binding (passId,name), component
        (passId,type), or unity-interface (interface,sourcePath). A subsequent real
        render and review decision with reconstructionId must validate the replacement.
        Known parameter corruption, compile failure and image failures cannot be waived.
        When original gaps are actually repaired, retire obsolete entries separately
        with {id,retire:true,reason}; current checks/render/review must pass without them.
        """
        try:
            return host._ok_compact(await asyncio.to_thread(store.record_reconstruction, restoration_id, records))
        except Exception as error:
            return failure(restoration_id, "record_unity_reconstruction", error)

    @tool
    async def adopt_unity_restoration_render_plan(restoration_id: str) -> str:
        """Adopt the platform-adapted plan observed by check_unity_restoration.

        Requires valid resource/implementation descriptors and current project files.
        Preserves the source plan and requires a fresh render of every replacement pass.
        """
        try:
            return host._ok_compact(await asyncio.to_thread(store.adopt_observed_plan, restoration_id))
        except Exception as error:
            return failure(restoration_id, "adopt_unity_restoration_render_plan", error)

    @tool
    async def use_unity_visual_fidelity_goal(restoration_id: str) -> str:
        """Use functional visual fidelity for a task whose goal permits native/inferred inputs.

        For a user-requested approximate restoration, replaces a legacy pixel-exact
        objective with the fixed visual-quality policy and requires new validation.
        Repeated calls do not loosen its limits or edit images. Keep strict-pixel
        when exact reproduction was explicitly requested.
        """
        try:
            return host._ok_compact(await asyncio.to_thread(store.use_visual_goal, restoration_id))
        except Exception as error:
            return failure(restoration_id, "use_unity_visual_fidelity_goal", error)

    @tool
    async def get_unity_shader_transfer_sources(workspace_paths: list[str], require_replay_validation: bool = False,
                                                organization_ids: dict[str, str] | None = None,
                                                draft_sha256: dict[str, str] | None = None) -> str:
        """Prefer prepared Bread sources; retain reference data for a Unity fallback.

        Reuse saved sources and semantic channel groups; this does not reorganize,
        rename, compile or apply anything. The default workflow first decompiles,
        organizes, compiles and validates in Bread, reusing current receipts.
        This read prefers existing proof and reports precise missing checks.
        A concrete application/tool limitation permits reference-only transfer;
        record that diagnostic and complete missing work in Unity. Known compiler
        or numeric failures remain explicit defects to repair, never passed checks.
        organization_ids maps workspace paths to exact retained organization IDs
        when a failed/unpublished proposal should be transferred without promotion.
        For a proposal edited since its session receipt, read the actual file and
        pin that observed hash in draft_sha256. This changes no workspace receipt.
        Strict validation is an optional hard requirement, not the default gate.
        Put these workspace/source identities in transfer_spec.shader.organizedSources.
        """
        try:
            return host._ok_compact(await asyncio.to_thread(collect_organized_sources, workspace_paths, ShaderWorkspaceArtifactReader().status,
                reference_identity_loader(), require_replay_validation=require_replay_validation,
                organization_ids=organization_ids, draft_sha256=draft_sha256))
        except Exception as error:
            return host._err(error)

    @tool
    async def fork_resource_export(operation_id: str, expected_bundle_sha256: str,
                                   revision_key: str = "") -> str:
        """Reuse verified captured assets for a corrected immutable transfer specification.

        Forks a new operation without replaying/re-extracting the capture or replacing
        the old package. Read its template, fix the source/parameter/pass mappings and
        finalize using the returned operationId and bundleSha256. Then bind the same
        restoration to that operation. Reuse revision_key when retrying this fork.
        """
        try:
            result = await asyncio.to_thread(host._RESOURCE_EXPORT_MANAGER.fork_capture,
                operation_id, expected_bundle_sha256, revision_key)
            return host._ok_compact(result)
        except Exception as error:
            return host._err(error)

    @tool
    async def connect_unity_project_mcp(project_path: str, connection: dict[str, Any],
                                       identity_tool: str = "bread_connection_identity",
                                       identity_arguments: dict[str, Any] | None = None,
                                       required_tools: list[str] | None = None) -> str:
        """Connect an existing Unity MCP, verify its actual project/version and reuse it.

        connection uses mcp-stdio or mcp-http. An existing provider must expose a
        genuine project identity tool. No packages are installed by this operation.
        Use start_unity_project_connection for managed background setup when absent.
        """
        try:
            from .unity_integration import shared_service
            result = await shared_service().connect_existing_mcp(project_path, connection,
                identity_tool=identity_tool, identity_arguments=identity_arguments, required_tools=required_tools)
            return host._ok_compact(result)
        except Exception as error:
            return host._err(error)

    @tool
    async def begin_unity_restoration(event_id: int, project_path: str, reference: dict[str, Any],
                                      acceptance: dict[str, Any] | None = None,
                                      connect: bool = True, editor_path: str = "",
                                      target_output: dict[str, Any] | None = None) -> str:
        """Start a resumable functional EID-to-Unity restoration with check/repair gates.

        reference identifies the actual comparison output: event_id, attachment_kind,
        attachment_index and optional stage/mip/slice/sample. Use native or a shared
        explicit display range. No cropping/downsampling or automatic normalization.
        acceptance defaults to mode=visual-fidelity: core algorithm and visible effect,
        moderate native-rendering differences, no severe structural/local mismatches.
        Use strict-pixel only for an exact goal. The task cannot complete from import/compile alone. Reuse organized
        Shader workspaces, export then bind_unity_restoration, check, repair and render.
        Connection runs in the background and never changes an existing render pipeline.
        """
        try:
            from .unity_integration import shared_service
            context, capture_id, session, api = await asyncio.to_thread(host._resource_export_live_identity)
            capture = {"captureId": capture_id, "captureSessionId": session, "api": api,
                       "path": context["capture"].get("path", "")}
            result = await asyncio.to_thread(store.begin, capture, event_id, project_path, reference, acceptance, target_output)
            if connect:
                try:
                    result["connectionJob"] = shared_service().prepare_project(project_path, editor_path or None, True)
                except Exception as connection_error:
                    result["connectionError"] = str(connection_error)[:2000]
                    result["nextTools"] = ["start_unity_project_connection", *result["nextTools"]]
            return host._ok_compact(result)
        except Exception as error:
            return host._err(error)

    @tool
    async def get_unity_restoration_status(restoration_id: str) -> str:
        """Read current restoration gaps and evidence freshness without starting Editors.

        An unchanged needs-repair receipt is not a reason to re-export everything.
        Repair its specific missing source, parameter, pass, scene or image evidence.
        """
        try:
            return host._ok_compact(await asyncio.to_thread(store.status, restoration_id))
        except Exception as error:
            return host._err(error)

    @tool
    def write_restoration_document(path: str, content: str) -> str:
        """Write Unicode Markdown/HTML/text directly as UTF-8 BOM and verify its bytes.

        Avoid console/codepage conversion. Reports damaged question-mark runs;
        regenerate such text from original Unicode rather than claiming an
        encoding switch recovered it. Does not alter scene/model assets.
        """
        try:
            from .unity_fidelity_tools import write_document
            return host._ok_compact(write_document(path, content))
        except Exception as error:
            return host._err(error)

    @tool
    def open_restoration_document(path: str) -> str:
        """Open local Markdown/HTML/text inside Bread, with native flow diagrams."""
        return host._call("open_document", {"path": path})

    @tool
    def localize_unity_effect_difference(comparison: dict[str, Any], pass_graph: list[dict[str, Any]] | None = None,
            inputs: list[dict[str, Any]] | None = None, isolation: list[dict[str, Any]] | None = None) -> str:
        """Reuse a compare_unity_effect_layers report to locate discrepancy boundaries.

        pass_graph names each observation and actual dependsOn inputs. inputs gives
        name/kind/reference/rendered/consumers (actual CBuffer, matrices, texture
        view/mip/sampler/format, render state etc). Reports exact differing field
        paths, spatial bounds, upstream propagation and shared module candidates.
        isolation gives effect/fullOutput/bypassOutput/effectOutput report names,
        sameInputs/sameEnabledRemainder/singleEffectIntervention/restored. Reference
        origin is rdc-original or honestly labeled rdc-controlled-isolation.
        Matching remainder + differing isolated effect narrows the suspect; it is
        not proof of a root cause. Temporary bypass must be restored, never offered
        as a fidelity solution. Reuses reports without image IO or Unity changes.
        """
        try:
            from .unity_difference_diagnosis import localize_difference
            return host._ok_compact(localize_difference(comparison,pass_graph,inputs,isolation))
        except Exception as error:
            return host._err(error)

    @tool
    async def review_unity_temporal_stability(samples: list[dict[str, Any]], absolute_tolerance: float = 0.001,
            relative_tolerance: float = 0.001, changed_pixel_fraction: float = 0.001, minimum_samples: int = 8) -> str:
        """Observe multi-frame final-output variation without changing Unity.

        Use bread_capture_view_sequence sidecars. Each sample additionally labels
        scenario/poseId, controlledInputs and inputStateKey only after actual
        animation/light/exposure/input controls. Group stationary holds at each
        camera angle separately, exclude explicitly labeled warmup frames, and
        collect resize wide/tall/return holds. Moving poses are not pixelwise
        comparable. Reports local flicker, nonfinite values, A/B/A alternation,
        history readiness and missing proof; never accepts or locks a task.
        """
        try:
            from .unity_temporal_validation import review_stability
            return host._ok_compact(await asyncio.to_thread(review_stability,samples,absolute_tolerance,
                relative_tolerance,changed_pixel_fraction,minimum_samples))
        except Exception as error:
            return host._err(error)

    @tool
    def review_unity_restoration_handoff(effects: list[dict[str, Any]], comparisons: list[dict[str, Any]],
            attempts: list[dict[str, Any]], delivery: dict[str, Any], no_progress_budget: int = 3,
            diagnosis: dict[str, Any] | None = None, checkpoint_budget: int = 12) -> str:
        """Review effect limits, unchanged no-progress work and concrete handoff gaps.

        Effects: name/state(matched|approximation|accepted-difference|unresolved), explanation,
        dependencyKind, runtimeHistoryInvestigated, concreteExternalDataLimit and
        optional actual userDecision. Legacy disabled records remain readable,
        but disabling is a diagnostic intervention, not a suggested delivery choice.
        diagnosis reuses localize_unity_effect_difference; accept a residual only
        after isolating its difference and proving the equivalent remainder, with
        the diagnostic bypass restored. Attempts: effect/strategy/inputFingerprint/
        materialProgress. For a disabled-effect baseline supply matching
        referenceEffects/renderedEffects/disabledEffects, referenceOrigin=rdc-original,
        matched/sameInputs and actual evidencePaths. Never compare Unity-only
        disabled effects against an enabled full RDC image as a matching baseline.
        Delivery annotates dualViewFinalOutputs, resizeAndTemporalChecks,
        assetLayoutReviewed, temporaryDiagnosticsRemoved, remainingIndependentWorkComplete.
        Supply absolute projectPath, artifactInventoryReviewed=true and the complete
        generated artifactInventory [{path, role:temporary|final|runtime, reason,
        requiredByFinalAssets}]. Paths may be project-relative or absolute. Only
        runtime assets need a reason. This read-only report observes remaining
        temporary files/.meta and missing final files; it never deletes anything.
        sceneDelivery carries scenePath (a saved .unity under Assets), sceneSha256
        from the bytes actually reopened, saved=true,
        reopenedFromDisk=true, requiresSetupCommand=false, missingScripts=0,
        missingReferences=[], compileErrors=[], finalOutputsReviewed=true and
        absolute evidencePaths from actual post-cleanup normal scene reopening.
        Remove generated builders, validators and comparison captures, retain
        required render/runtime scripts, and serialize configuration before reopening.
        Evidence belongs in Bread task storage, outside the delivered Unity tree.
        Effects may record resolutionAssessment {status, remainingFeasibleRoutes,
        attemptedAlternatives, reason, evidencePaths}. Exhausted-supported-recovery
        or external-user-input-required requires a concrete reason and saved
        evidence; remaining executable routes take precedence. Counters alone
        never suggest a user question: replan and pursue feasible repairs first.
        Budgets suggest asking the user, not stopping independent repairs or
        accepting a lower fidelity target automatically. No Unity/project writes.
        checkpoint_budget defaults to 12 distinct attempts, including improving
        iterations, as an internal strategy checkpoint. Improving work continues
        without user budget extensions. budgetWindowId can separate internal
        strategy windows without deleting history. Neither budget is an ending,
        permission or import/execution gate. Ask only for a real unresolved limit.
        """
        try:
            from .unity_handoff_review import review_handoff
            return host._ok_compact(review_handoff(effects,comparisons,attempts,delivery,no_progress_budget,diagnosis,checkpoint_budget))
        except Exception as error:
            return host._err(error)

    @tool
    def review_unity_view_evidence(observations: dict[str, Any], captures: list[dict[str, Any]],
                                   comparisons: list[dict[str, Any]], experiments: list[dict[str, Any]],
                                   required_effects: list[str] | None = None, stability: dict[str, Any] | None = None) -> str:
        """Review Game/Scene observations, final captures and isolation evidence.

        Read observations from bread_inspect_camera_rendering and capture both
        views with bread_capture_view_outputs. Capture sidecarPath binds actual
        camera IDs/sequences; do not relabel a module RT as a final output.
        Compare each final capture against original RDC under recorded matched
        pose/extent/color/history conditions; label referenceOrigin=rdc-original
        and alignmentRecorded=true only with actual evidence. Old Unity kernels
        provide regression proof, not an independent original reference.
        Experiments retain scenario/performed/evidencePaths/observations for
        scene-motion-only, game-motion-only, different-extents, history-reset,
        unfocused-scene, resize-wide-tall-return and multi-angle-holds. Supply
        resizeCaptures={original: capture, wide: capture, tall: capture, return: capture}
        for the Scene resize experiment, with one Scene camera's four real final-output
        sidecars, unchanged worldToCamera, actual cameraPixelWidth/Height,
        nonJitteredProjection, rowOrigin, controlledInputs=true and inputStateKey.
        Wide and tall must change aspect, not merely 640x360 to 800x450.
        The report checks projection/viewport agreement and samples pixel
        correspondence predicted by the real projections; constant images or
        text-only declarations do not prove no stretching. Set absoluteTolerance
        explicitly for normal resampling/noise, and inspect per-pass differences
        if the final presentation differs. Original evidence is never resized.
        bread_resize_scene_view can request/restore existing window bounds
        without focus; docking can constrain them, so observe actual extents.
        Game independently supplies game-resize-wide-tall-return with the same
        four actual Game camera captures and freeAspectRatio=true from an
        observed Game view mode. bread_resize_game_view lists existing windows,
        requests verified Free Aspect/bounds without focus and restores saved
        mode/bounds in finally. Preview Zoom/Scale is not render resolution.
        Each observed Game/Scene camera needs its own resize evidence; Scene
        evidence cannot stand in for Game. Actual scaledPixelWidth/Height may
        explain render-scale extents, but projection, RTs, matrices, sampling
        and history must agree within each current view.
        Supply stability from review_unity_temporal_stability; combine its groups/findings
        across separately reviewed held poses. Reports gaps only; never locks
        import or accepts a task. Original fidelity uses capture-aligned inputs;
        other angles establish dynamic stability, not equality to another pose.
        Also supply scene-camera-orbit/scene-camera-translation, scene-light-direction,
        scene-light-color-intensity and motion-settle-return experiments with
        beforeCapture/afterCapture sidecars, outputComparison layer, expectedOutputChange
        and expectationReason. Light tests include lightingBefore/lightingAfter;
        return tests include middleCapture. Changed moving images are expected;
        final holds and actual image review establish stability, not frozen pixels.
        motionSamples (3..16 ordered Scene sidecars including endpoints) and
        motionComparisons (adjacent numerical layers) check finite outputs during
        the movement too. Two endpoint images alone do not establish stable motion.
        """
        try:
            from .unity_view_validation import review_views
            return host._ok_compact(review_views(observations,captures,comparisons,experiments,required_effects or [],stability))
        except Exception as error:
            return host._err(error)

    @tool
    def plan_unity_asset_layout(assets: list[dict[str, Any]], asset_root: str = "Assets/BreadRestoration") -> str:
        """Plan a simple typed asset layout with semantic EID names.

        Each asset needs path/kind/role/eventIds. Optional contentSha256 and
        samplingIdentity identify shared textures; nameOverride resolves real
        semantic collisions. No opaque hash suffix, write or overwrite occurs.
        Apply the returned plan through bread_apply_asset_layout in the Editor.
        Kinds: model/material/prefab/texture/scene/shader/runtime/editor. Create
        only needed folders; runtime/editor are required effect/inspector code,
        not temporary construction, setup, validation or screenshot helpers.
        Review relative Shader includes, Resources/Addressables paths and asmdef
        scope before moves; preserving GUIDs alone does not preserve those links.
        """
        try:
            from .unity_fidelity_tools import asset_layout
            return host._ok_compact(asset_layout(assets, asset_root))
        except Exception as error:
            return host._err(error)

    @tool
    def trace_unity_effect_dependencies(event_ids: list[int], max_depth: int = 12, max_nodes: int = 256) -> str:
        """Follow current RDC inputs to prior writers and possible live-history updates.

        Returns actual graphics/compute producer candidates, bindings, dimensions,
        formats and sampling questions. Does not infer staticness from 3D type,
        call a frozen RT a live effect, require a flow publication or judge
        portability. Inspect producer shaders, subresources and coordinates.
        """
        try:
            from .unity_fidelity_tools import trace_dependencies
            context = host._active_live_context(allow_missing_capture_id=True)
            session = context["capture"]["captureSessionId"]
            def read(name, params):
                result = json.loads(host._call(name, params, timeout=60.0))
                if result.get("error"):
                    raise RuntimeError(str(result["error"]))
                return result
            resources = {}
            def info(rid):
                if rid not in resources:
                    resources[rid] = read("get_effect_resource_info", {"resource_id": str(rid), "expected_capture_session_id": session})
                return resources[rid]
            result = trace_dependencies(event_ids,
                lambda eid: read("inspect_frame_render_stage", {"event_id": eid, "expected_capture_session_id": session}),
                lambda rid: info(rid)["usage"], lambda rid: info(rid)["texture"],
                max(1,min(64,max_depth)), max(1,min(512,max_nodes)))
            result["captureSessionId"] = session
            return host._ok_compact(result)
        except Exception as error:
            return host._err(error)

    def _export_texture_data(event_id, resource_id, output_directory, name, sampling, require_volume, slice_index=0, sample_index=0):
        """Export a captured 3D texture's exact raw mip chain for bread_import_volume_texture.

        Reads the requested EID, preserves source format/precision/mips and
        restores the selected replay event. Supply captured sampling controls;
        absent sampler information remains explicitly unresolved. This exports
        data and does not decide whether the volume is static or view-dependent.
        """
        try:
            import base64, hashlib
            context = host._active_live_context(allow_missing_capture_id=True)
            session = context["capture"]["captureSessionId"]
            directory = Path(output_directory)
            if not directory.is_absolute():
                raise ValueError("Use an absolute output directory")
            directory.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^\w.-]", "_", name).strip(".") or "IndirectVolume"
            mips = []; first = None; epoch = None
            for mip in range(32):
                offset = 0; chunks = []
                while True:
                    result = json.loads(host._call("get_event_texture_bytes", dict(event_id=event_id,
                        resource_id=resource_id, mip=mip, offset=offset, slice=slice_index, sample=sample_index,
                        expected_capture_session_id=session), timeout=60.0))
                    if result.get("error"):
                        raise RuntimeError(str(result["error"]))
                    if require_volume and "3d" not in str(result["dimension"]).lower():
                        raise ValueError("Requested resource is not a 3D texture")
                    if first is None:
                        first = result; epoch = result["mutationEpoch"]
                    if result["captureSessionId"] != session or result["mutationEpoch"] != epoch:
                        raise RuntimeError("Texture inputs changed during readback")
                    raw = base64.b64decode(result["base64"]); chunks.append(raw); offset += len(raw)
                    if offset >= result["length"]:
                        break
                    if not raw:
                        raise RuntimeError("Texture readback made no progress")
                raw = b"".join(chunks)
                if hashlib.sha256(raw).hexdigest() != result["sha256"]:
                    raise RuntimeError("Texture mip changed during readback")
                path = directory / (safe_name + "_Mip%d.bin" % mip)
                if path.exists() and path.read_bytes() != raw:
                    raise ValueError("Choose another output directory to preserve existing volume evidence")
                path.write_bytes(raw)
                record = {"path": str(path), "width": result["width"], "height": result["height"],
                    "depth": result["depth"], "byteLength": len(raw), "sha256": result["sha256"]}
                fmt = str(result["format"]).upper(); bits = re.search(r"[RD](16|32)", fmt)
                if bits and "FLOAT" in fmt and "PACK" not in fmt:
                    channels = max(1,len(re.findall(r"[RGBA](?:16|32)",fmt)))
                    expected_bytes = result["width"]*result["height"]*result["depth"]*channels*(int(bits.group(1))//8)
                    if expected_bytes == len(raw):
                        record["encoding"] = "float" + bits.group(1) + "le"; record["channels"] = channels
                        if result["depth"] == 1:
                            Path(str(path)+".json").write_text(json.dumps(record,indent=2),encoding="utf-8")
                mips.append(record)
                if mip + 1 >= first["mips"]:
                    break
            is_volume = "3d" in str(first["dimension"]).lower()
            manifest = {"schema": "bread-volume-texture/v1" if is_volume else "bread-texture-subresources/v1", "name": name, "eventId": event_id,
                "resourceId": resource_id, "captureSessionId": session, "sourceFormat": first["format"],
                "width": first["width"], "height": first["height"], "depth": first["depth"],
                "mips": mips, "sampling": sampling or {}, "samplingStatus": "provided" if sampling else "unresolved",
                "sliceIndex": slice_index, "sampleIndex": sample_index, "sourceSamples": first.get("samples",1),
                "colorSpace": "preserved source bytes; choose matching graphicsFormat for Unity", "diagnosticsOnly": True}
            path = directory / (safe_name + (".volume.json" if is_volume else ".texture.json")); path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
            return host._ok_compact({"manifestPath": str(path), "mipCount": len(mips), "sourceFormat": first["format"],
                "nextAction": "Import with bread_import_volume_texture using an exact supported graphicsFormat; retain world-to-volume transforms and original sampling functions." if is_volume else
                    "Use uncompressed float mip sidecars for identical-input pass comparison. Explicitly decode packed/compressed formats and align array slice/MSAA sample/display mapping."})
        except Exception as error:
            return host._err(error)

    @tool
    def export_unity_volume_texture(event_id: int, resource_id: str, output_directory: str,
                                   name: str = "IndirectVolume", sampling: dict[str, Any] | None = None) -> str:
        """Export exact captured volume mip bytes for bread_import_volume_texture.

        Supply source sampling, format and world-to-volume mapping. The export
        does not decide that the volume is static, screen-independent or relit.
        """
        return _export_texture_data(event_id,resource_id,output_directory,name,sampling,True)

    @tool
    def export_unity_texture_data(event_id: int, resource_id: str, output_directory: str,
                                 name: str = "PassOutput", slice_index: int = 0, sample_index: int = 0) -> str:
        """Export exact EID texture/slice/sample mip bytes for HDR pass comparison.

        Common uncompressed float formats get comparison JSON sidecars. Packed
        and compressed data require explicit decode, never silent PNG conversion.
        Array/cube slices and MSAA samples are explicit; volume depth is retained.
        The current EID is restored. No Shader compilation or export ledger needed.
        """
        return _export_texture_data(event_id,resource_id,output_directory,name,None,False,slice_index,sample_index)

    @tool
    async def compare_unity_effect_layers(layers: list[dict[str, Any]]) -> str:
        """Measure separate ported-core, native-engine, post-process and full-frame outputs.

        Each layer has name/category/reference/rendered. File records need an
        absolute path; raw .rgba32f/.rgba16f or float32le records need dimensions
        or a JSON sidecar. ROI, per-layer absolute/relative tolerance, module and
        sharedShader group common failures and locate differenceBounds/worst tiles.
        referenceBefore/renderedBefore additionally compare after-minus-before
        contributions without assuming an effect is additive or proving causality.
        Label referenceOrigin=rdc-original or rdc-controlled-isolation honestly.
        Reuse reports with localize_unity_effect_difference to inspect the graph's
        first differing boundary rather than mistaking list order for causality. Keep
        dataRange fixed. Optional inputFixture {referencePath, renderedPath}
        identifies a shared input RT for isolated pass checks. Reports numerical
        errors and fast global SSIM without resize, recoloring, accepting a task
        or changing scene settings. The Agent chooses appropriate error budgets.
        """
        try:
            from .unity_layered_validation import compare_layers
            return host._ok_compact(await asyncio.to_thread(compare_layers, layers))
        except Exception as error:
            return host._err(error)

    @tool
    async def bind_unity_restoration(restoration_id: str, operation_id: str,
                                     workspace_paths: list[str] | None = None,
                                     editor_path: str = "", organization_ids: dict[str, str] | None = None,
                                     draft_sha256: dict[str, str] | None = None) -> str:
        """Bind a finalized capture-data export and optional reference sources, then import.

        Verifies exact capture/stage hashes and saved source bytes. Original-API
        replay validation is independent of Unity compilation and visual review.
        Raw native Shader/reflection evidence in the package is sufficient when
        no readable workspace exists. The finalized transfer spec must contain
        any selected reference sources. Returns
        the managed connection/import job alongside the persistent restoration ID.
        """
        try:
            from .unity_restore import prepare_unity_import
            from .unity_integration import shared_service
            state = store.load(restoration_id)
            exported = await asyncio.to_thread(host._RESOURCE_EXPORT_MANAGER.status, operation_id)
            if exported.get("status") != "finalized" or not exported.get("packagePath"):
                raise ValueError("Finalize the selected resource export before binding it")
            bundle, spec = await asyncio.to_thread(package_bundle, exported["packagePath"])
            paths = workspace_paths if workspace_paths is not None else [s["workspacePath"] for s in (spec.get("shader") or {}).get("organizedSources", [])]
            drafts = organization_ids if organization_ids is not None else {
                s["workspacePath"]: s["sourceSelection"]["organizationId"]
                for s in (spec.get("shader") or {}).get("organizedSources", [])
                if (s.get("sourceSelection") or {}).get("kind") == "organization-draft"
                and Path(s["workspacePath"]).resolve() in {Path(path).resolve() for path in paths}}
            hashes = draft_sha256 if draft_sha256 is not None else {
                s["workspacePath"]: s["sourceSha256"] for s in (spec.get("shader") or {}).get("organizedSources", [])
                if (s.get("sourceSelection") or {}).get("kind") == "organization-draft"
                and s["workspacePath"] in drafts
                and drafts[s["workspacePath"]] == s["sourceSelection"]["organizationId"]}
            organized = await asyncio.to_thread(collect_organized_sources, paths, ShaderWorkspaceArtifactReader().status,
                reference_identity_loader(), require_replay_validation=False,
                organization_ids=drafts, draft_sha256=hashes)
            if paths and not organized["ready"]:
                return host._ok_compact({**store.status(restoration_id), "sourcePreparation": organized})
            prepared = await asyncio.to_thread(prepare_unity_import, exported["packagePath"], state["projectPath"])
            result = await asyncio.to_thread(store.bind, restoration_id, exported, prepared, organized["sources"])
            result["importJob"] = await shared_service().start_import(state["projectPath"], prepared["requestPath"], editor_path or None)
            return host._ok_compact(result)
        except Exception as error:
            return failure(restoration_id, "bind_unity_restoration", error)

    @tool
    async def check_unity_restoration(restoration_id: str, repair_configuration: bool = False) -> str:
        """Inspect actual imported assets, parameters, passes, scene and pipeline.

        repair_configuration reapplies the recorded configuration and authoritative
        parameter bindings, then inspects again. It does not change shader algorithms
        or loosen comparison tolerances. Use project tools for diagnosed source/feature
        repairs, then check again; only changed evidence invalidates a prior review.
        """
        try:
            from .unity_integration import shared_service
            state = store.load(restoration_id)
            if not state.get("binding"):
                return host._ok_compact(store.status(restoration_id))
            args = {"request_path": state["binding"]["requestPath"]}
            if repair_configuration:
                store.invalidate(restoration_id, "reapply-recorded-configuration")
                _data(await shared_service().call_tool(state["projectPath"], "bread_apply_capture_configuration", args))
            observed = _data(await shared_service().call_tool(state["projectPath"], "bread_inspect_capture", args))
            return host._ok_compact(await asyncio.to_thread(store.inspect, restoration_id, observed))
        except Exception as error:
            return failure(restoration_id, "check_unity_restoration", error)

    @tool
    async def render_unity_restoration(restoration_id: str, refresh_reference: bool = False) -> str:
        """Render the actual Unity camera and compare against this task's captured output.

        Captures its exact EID/reference on first use, with fixed display settings.
        Renders at reference resolution through bread_render_capture, checks actual
        scene/dependency hashes, and stores full-resolution errors and iteration progress.
        Inspect both images and changed bounds; repair the causal configuration or
        computation with project tools, check and render again. No image recoloring,
        threshold widening or synthetic success is performed.
        """
        try:
            from .unity_integration import shared_service
            state = store.load(restoration_id)
            if not state.get("binding"):
                return host._ok_compact(store.status(restoration_id))
            if not state.get("reference") or refresh_reference:
                context, _, session, _ = await asyncio.to_thread(host._resource_export_live_identity)
                await asyncio.to_thread(store.observe_capture, restoration_id,
                    context.get("capture") or {"captureSessionId": session})
                receipt = json.loads(await asyncio.to_thread(host._capture_event_texture_evidence_receipt, **state["referenceSelection"]))
                if receipt.get("error"):
                    raise ValueError(str(receipt["error"]))
                await asyncio.to_thread(store.reference, restoration_id, receipt)
                state = store.load(restoration_id)
            image = state["reference"]["image"]
            output = Path(state["projectPath"]) / "Library/Bread/Renders" / ("unity-" + uuid.uuid4().hex + ".png")
            args = {"request_path": state["binding"]["requestPath"], "width": image["width"],
                    "height": image["height"], "output_path": str(output),
                    "display_mapping": state["displayMapping"], "target_output": state["targetOutput"]}
            rendered = _data(await shared_service().call_tool(state["projectPath"], "bread_render_capture", args))
            return host._ok_compact(await asyncio.to_thread(store.compare, restoration_id, rendered))
        except Exception as error:
            return failure(restoration_id, "render_unity_restoration", error)

    @tool
    async def review_unity_restoration(restoration_id: str, state_fingerprint: str,
                                 assessments: dict[str, str], decisions: list[dict[str, Any]],
                                 limitations: list[str] | None = None) -> str:
        """Review the current port after inspection/rendering; failed checks still block completion.

        assessments covers geometry_and_coordinates, material_and_constants,
        lighting_and_indirect, render_states_and_passes, scene_and_dependencies,
        readable_source, core_algorithm and visible_effect. decisions gives module/source/target/evidence plus disposition
        ported|unity-native|captured-data|approximation for actual computation modules.
        Each system constant additionally needs a unity-native decision with its exact
        fieldId and target from binding.systemInterfaces. Inspect the derived source's
        space, matrix order and actual Unity interface; never bind captured camera data
        as a material property just to hide an unresolved interface conversion.
        Fix feasible gaps now. This records Agent judgment, not automatic proof that
        an algorithm or future frame is equivalent. Source/scene changes stale it.
        """
        try:
            return host._ok_compact(await asyncio.to_thread(store.review, restoration_id, state_fingerprint, assessments, decisions, limitations or []))
        except Exception as error:
            return failure(restoration_id, "review_unity_restoration", error)

    return functions
