# Buffer delta acceptance

This guide separates correct live edits, reduced invalidation, retained checkpoint
reuse, and measured latency. Passing one does not establish the others. Capture
IDs, offsets, and checkpoint positions must come from that capture's actual native
bindings; `1.rdc` is a test case, not a source of production rules.

## Run ownership and CPU regression

Run from the performance worktree. Follow [ISOLATED_REPLAY.md](ISOLATED_REPLAY.md)
for a recorded local build and a fresh `run_eid_isolated_python.ps1 -RunLabel` for
each GPU case. Pass `-RequireIdleReplay`; `-AllowForeignGui` preserves independent
branch development, but shared-GPU elapsed times are observations, not exclusive
performance evidence. Do not deploy these tests over another checkout.

Pure CPU regression needs ordinary Python and no GPU runner. From `util/perf`:

```powershell
python -B -m unittest test_eid_buffer_delta_profile test_eid_shared_buffer_profile test_eid_portable_forward_edit_profile test_capture_api_smoke -v
```

These tests exercise validators and deliberate false positives with fake controllers.
They do not establish native GPU correctness or UI responsiveness.

## Scalar cutoff and actual middle checkpoint reuse

Use `eid_buffer_delta_profile.py` with `--capture`, `--output`, `--early-event`,
`--late-event`, `--fixed-event`, and both `--early-buffer/--early-offset` and
`--late-buffer/--late-offset`. Offsets are absolute buffer bytes: bound view offset
plus reflected member/component offset. The probe verifies a bound float32 component.
Keep the early value constant and supply at least three distinct `--late-values`.

| Case | Required evidence |
| --- | --- |
| Two buffers, delta enabled | The unchanged early buffer remains patched; only the late buffer changes. Native `firstAffected` moves to `--expected-cutoff`. |
| Same buffer, different offsets | Add `--same-buffer-ranges`. The early bound view must exclude the late scalar. Every supported API's positive result requires `rangeIndexKnown=1`. A conservative fallback may preserve correctness, but does not pass this optimization gate. |
| Delta disabled control | Repeat in a fresh process with `--disable-delta`; native `firstAffected` must equal `--expected-disabled-cutoff`, normally the early consumer. Editing and image correctness must still pass. |

The probe checks the actual `*BufferPatch op=set` timing record and controller
generation, current EID, patch count, and byte count. `deltaKnown`, `deltaEnabled`,
`changedRanges`, `changedBytes`, and `changedBuffers` must match the real byte delta.
A float32 edit need not change all four bytes. Expected command-line numbers alone
are not evidence that native invalidation shrank.

For reuse acceptance, supply `--checkpoint-event` strictly between the early consumer
and the enabled cutoff, sufficient `--budget-mib`/`--max-checkpoints`, and
`--require-checkpoint-reuse`. The probe prepares the point through normal navigation.
For at least two consecutive later setters, that point must already exist before the
setter, survive afterward, be the reported `lastResumeEvent`, increase native hits,
and cause **zero new checkpoint builds**. A newly rebuilt point at the same EID,
an anchor hit, or a smaller cutoff alone does not pass. The disabled control should
invalidate the previously prepared middle point; inspect its control acceptance too.

## Immediate image correctness and shader combinations

After each setter, read the buffer window and every bound color/depth output before
any forced reference replay. Reading must itself perform no replay. Reject raw timing
records for `MaterializeEvent`, `ReplayLog`, or typed `D3D11`, `D3D12`, `OpenGL`,
`Vulkan`, `ReplayDriverBoundary`, or `ReplayChunk` replay work, including `OnlyDraw`
and `WithoutDraw`. A genuine display `RenderTexture` operation is permitted. Correct
pixels after implicit repair, or unchanged checkpoint counters alone, cannot pass.
Compare the saved immediate witnesses with two independent forced references,
including all output bytes and the bounded buffer window. Require a visible effect
when claiming responsive parameter rendering; unchanged pixels prove less.

Use `eid_portable_forward_edit_profile.py` for an explicitly supplied compatible
shader plus shared-CBuffer edits. While the shader replacement is active, exercise
two distinct parameter values without changing EID. Undoing the CBuffer must match
the shader-only baseline; removing the shader must match the original baseline.
Both comparisons include full bound output bytes and the buffer window. This tests
the native checked replacement transaction, **not the full Shader Workbench ABI
gate**; that separate gate remains required for workbench acceptance.

Windows D3D11/D3D12/OpenGL/OpenGL ES/Vulkan results establish those API paths on the tested
driver. They do not establish Linux/Android/other OS or vendor coverage. Keep capture,
OS, GPU, driver, build receipt, and unsupported conservative paths in the report.

For the D3D12 fixture whose four draws use separate submissions, use
`eid_d3d12_shader_buffer_profile.py --capture <capture.rdc> --events <A,B,C,D>
--shader-source util/perf/fixtures/shared_d3d12_edited.hlsl --output <fresh.json>`
through the isolated runner. It discovers the original buffer, offset and shader;
the fixture's observed EIDs and resource IDs are not production constants. This
separate correctness probe requires real D3D12 prefix/target timing evidence,
disables checkpoints/prepared replay, and does not require or claim live-forward
hits. It leaves the original portable probe's positive hit gate intact. Its pure
regression module is `test_eid_d3d12_shader_buffer_profile`.

## D3D12 upload object reuse

Use `eid_shared_buffer_profile.py` with its required `--capture`, `--events`,
`--buffer`, `--offset`, `--data-hex`, `--output`, and `--fixed-values-hex` containing
4–16 distinct byte sequences of the same length. `--require-fast` requires steady
native setter checkpoint hits; it does not impose a frame-time target. Enable strict
unchanged-neighbor/identity checks only for fixtures where captured GPU writes cannot
legitimately change them.

Compare fresh processes with the same build/capture and delta settings. The upload
reuse control is `RENDERDOC_D3D12_DISABLE_BUFFER_PATCH_UPLOAD_REUSE=1`; clear that
override for the enabled case. Scope the variable to these runs and restore it after.
Count only samples whose phase matches `^fixedValues\.value\d+\.set$`, and report
steady values 3 onward separately from the two warmup values. Inside each setter,
inspect the unique native `api=D3D12BufferPatch op=set` record: `uploadReused` should
distinguish enabled/control cases, and `uploadWrittenBytes` measures patch payload
written to the upload object, not checkpoint RT restore bytes or just changed bytes.

All immediate fixed-value witnesses precede the later forced references. Exclude
`fixedValues.referenceValueN.install`, forced references, readbacks, and cleanup from
slider setter timings. Reference installs have separate `referenceGeneration` values;
compare their identical byte configurations with the saved immediate witnesses.
GPU synchronization remains necessary before reusing upload storage, so allocation
reuse alone cannot establish that total replay latency has become small.

## GL/GLES compact upload reuse and captured CPU writes

GL/GLES has its own independent upload control,
`RENDERDOC_GL_DISABLE_BUFFER_PATCH_UPLOAD_REUSE=1`. Keep delta invalidation enabled
in both processes. Add `--expect-upload-reuse compact` for the enabled run and
`--expect-upload-reuse full` for the control; these gates reject missing or incorrect
native copy evidence. For a stable two-scalar layout, the later setters in
`eid_buffer_delta_profile.py` should report `uploadReused=1` and
`uploadWrittenBytes=changedBytes`; the control should report `uploadReused=0`
and the full active patch payload. These are upload-object writes, not all replay
copies. Layout changes and undo may allocate a new compact upload. Compare both
processes' immediate outputs and neighbor bytes with their forced references.

Captured application CPU writes are a separate path from user parameter uploads.
Capture the `D3D12_Buffer_CPU_Dirty_Ranges` fixture and discover its actual events
and buffer with `discover_buffer_delta_fixture.py`. Run `eid_checkpoint_profile.py`
with every fixture draw, `--steady-rounds 2`, and
`--extra-buffer CPUWriteParameterBuffer`. This reads all bytes of the buffer as
well as every bound output. Repeat with
`RENDERDOC_D3D12_DISABLE_CPU_WRITE_DIRTY_RANGES=1` in a fresh process.

Inspect `D3D12DirtyRestore` records in `steadyDisabled.replay`, where checkpoints
are off but ordinary dirty restoration still runs. Exclude the first canonical
restore after configuration. Require actual initial-copy reduction in the enabled
case, equal full-buffer/output witnesses, and full-copy behavior in the control.
`D3D12CPUWriteBaseline` diagnostics prove the loading-time comparison against
original initial bytes; they alone do not prove a later copy was omitted. The
comparison budget is bounded, and unknown/GPU-writable/aliased storage falls back.
