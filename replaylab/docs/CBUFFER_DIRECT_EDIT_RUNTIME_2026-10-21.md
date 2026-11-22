# CBuffer direct-edit runtime audit

The original `BufferViewer` can edit a reflected parameter without compiling or
replacing the Shader. The direct path is `IReplayController::SetSharedBufferPatch`:
it updates only the addressed bytes, replays the selected event, and displays the
outputs. UI publication must then use `CaptureContext::RefreshReplayStatus`, which
copies the already-replayed pipeline and waits for viewer publication without a
second replay. A superseded publication is not a failed mutation and must not
cause the accepted edit to be resent.

## Scope and supported backends

Shared edits belong to an original captured buffer and absolute byte range. Every
replayed consumer of that range sees the edit, including another EID that binds the
same storage. The reflected member offset must be added to the binding's base
offset. Semantic grouping never changes that identity or permits writing through
unrelated gaps between lanes.

Native implementations exist for D3D11, D3D12, OpenGL and Vulkan. Their ordinary
constant/uniform-buffer paths are supported; driver checks still reject storage
that cannot be restored correctly, such as unsupported GPU writes, aliasing or
external memory. Inline/push constants and compile-time constants are not editable
buffer storage. This is distinct from `SetEventCBufferOverride`, whose API supports
only D3D11/D3D12 and has event-local binding restrictions.

Repeated edits already reuse upload/scratch resources and calculate changed-byte
ranges. Proven checkpoints before the first affected consumer can remain valid.
D3D12 still waits for GPU completion before replacing upload bytes, and the
necessary affected frame portion still has to replay. Consequently the UI can be
nonblocking and coalesce slider changes, but the capture's replay cost determines
how quickly a new preview appears; it is not a guaranteed 60 FPS material editor.

## Coexistence with the earlier material panel

The shared controller generation is an ownership witness. An accepted edit from
the native viewer invalidates older Python material-panel snapshots. The revised
bridge revokes those snapshots without deleting native patches. The panel removes
the expired UI capability and its queued values, so reselecting a parameter can
acquire fresh current bytes without reopening the capture. Stale values are never
automatically retried.

When no foreign edits predate a session, resetting a stable owned range can clear
that range. After another editor has changed the configuration, a fresh session's
reset instead writes back the bytes observed when that session was opened. The
range-only native API cannot prove whether clearing that exact range would remove
another editor's previous override. This operation means **restore the values at
the start of editing**, not restore captured values at every EID or remove all
shared overlays. A later external generation change also revokes reset authority.

## Focused validation

`test_shared_cbuffer.py`, `test_material_edit_queue.py` and
`test_native_material_panel.py`: 49 passed. Regression coverage includes initial
native edits before opening a material session, stale reset rejection, preservation
of the other editor's same-range values, pending-value cancellation, and successful
reopening after a generation change. These are CPU contracts; they do not claim
live GPU coverage for all four graphics backends.

## Actual-capture admission failure found during native acceptance

The first native check on `1.rdc`, EID 61408, PS CB5 reached the replay driver and
was rejected by the existing shared-patch admission check. The physical allocation
is buffer 10125244 (4 MiB); this CBuffer starts at byte 3663872 and has 1372 reflected
bytes. The root binding reports a 530432-byte allocation remainder, which is not
the size of this material's constants.

The captured allocation has 9953 usage records: 6226 PS constant reads, 2731 VS
constant reads, 567 CS constant reads, 28 TS constant reads, 400 vertex-buffer
reads, and one CPU write at EID 6076. There are no reported RW-resource, indirect,
or query uses. The prior admission whitelist treated the unrelated vertex-buffer
reads in this shared upload allocation as an unsupported write.

The D3D12 fix admits vertex/index-buffer and read-only SRV consumers while retaining
all existing checks against GPU writes, indirect arguments, unsupported copy
commands, resource replacements, overlapping allocations, and non-restorable
storage. These additional read uses also participate in the earliest-consumer
calculation, so an earlier geometry/SRV consumer cannot leave a stale checkpoint.
The native byte-range index remains optional: incomplete range evidence falls
back to the conservative whole-prefix replay. Errors now identify the rejected
usage and EID. New native tests cover read admission, forbidden uses and earliest
consumer selection; the completed verification is recorded below.

## Final installed-build validation

Bread integration 1.13.45 was compiled and deployed successfully. Native UI tests
passed 170 assertions in 6 cases; core replay tests passed 113 assertions in 6
cases. The direct editor remains inside the original, normally docked CBuffer
viewer, with no standalone overlay.

The D3D12 live acceptance run (PID 30036, `1.rdc`, EID 61408, PS CB5) passed all four
interactions:

| Interaction | Observed update |
| --- | --- |
| Scientific numeric input | `normalIntensity`: `1.5` to `1e-20` |
| Continuous slider | 20 slider events coalesced into 2 native mutations, ending at `2.1` |
| Two-component field | `trailSize`: `[15, 5]` to `[15.125, 5.25]` |
| Color picker | `baseTint`: `[1, 1, 1]` to `[0.2, 0.4, 0.6]` |

For each interaction, the complete 1372-byte reflected block matched the requested
field update and every byte outside the selected field was unchanged. Reset then
restored the complete starting block. The viewer exposed 153 rows. A 20 ms UI timer
observed a maximum interval of 157 ms during this run; this does not establish a
60 FPS guarantee. The 95.7-second total includes project restoration and loading,
so it is not an edit-latency measurement.

Evidence: `C:/Users/YC/AppData/Local/Temp/bread-cbuffer-live-acceptance.json` and
`C:/Users/YC/AppData/Local/Temp/bread-cbuffer-live-acceptance.png`. The actual-capture
GPU acceptance covered D3D12 only. Other backend support is based on their existing
implementations and CPU contracts, not a claim of live validation in this task.

These parameter changes belong to the current capture session. Direct editing does
not automatically save a persistent material preset for a future capture or
application restart.

The acceptance instance was closed normally after all values were reset. A normal
launch without the test script (PID 36072) restored all four project Shaders and
enabled all four editors using `verified-rdc-restore`, with zero warning/error
console events. The acceptance run recorded no Shader operations after project
restore, and the target source SHA-256 stayed
`a4430c84c5c84d8f18e93d7187155ee72dbf6437282450aa1641f9e89534e068`.
