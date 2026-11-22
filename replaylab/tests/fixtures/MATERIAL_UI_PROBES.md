# Shared Material UI native probe

Run `qrenderdoc_material_shared_smoke.py` with `qrenderdoc.exe --ui-python` in an
isolated process, after coordinating GPU ownership. The script can open the explicit
capture. Run separately with the D3D11 and OpenGL/Vulkan shared-buffer fixtures.

Required environment variables use the prefix `REPLAYLAB_MATERIAL_SMOKE_`:

| Suffix | Meaning |
| --- | --- |
| `CAPTURE` | Absolute path to the selected capture |
| `OUTPUT` | Absolute JSON receipt path; an adjacent catalog directory is created |
| `EVENT` | Draw where the native CBuffer field will be exposed |
| `AWAY_EVENT` | A distinct draw consuming the same original buffer range |
| `STAGE` | Bridge stage name, default `fragment` |
| `BLOCK`, `ARRAY` | Reflection block index and descriptor array element, default `0` |
| `FIELD` | Exact reflected leaf path; omitted selects the first evidenced Float leaf |
| `VALUE` | JSON scalar/vector value; omitted halves the first nonzero component, or sets zero to 0.5 |
| `SECONDS` | Continuous text input duration, default `1.0` |
| `MAX_HEARTBEAT_MS` | Maximum allowed GUI timer gap during continuous input, default `250` |
| `QUIT` | Set `1` to close this test process after writing the receipt |

The probe exports a real one-event/stage catalog, reads actual `GetConstantBlock`
and reflection metadata, opens the native BufferViewer and dispatches its real
context-menu QAction. C++ generates the callback payload. It then edits the actual
Material textbox through MiniQt, exercises continuous input at 10 ms timer ticks,
and clears through the same asynchronous model queue.

Acceptance checks exact field bytes and all bound 2D color/depth mip/slice/sample
hashes at both EIDs, their restoration after clear, multiple completed publications
while input continues, the final pending value, and GUI heartbeat gaps. A field
that changes no output fails as inconclusive; it does not count as a pixel pass.
Every observed continuous publication must carry `previewPublished=true` and the
actual replayed EID; native mutation completion alone cannot satisfy this guard.
The read limit is 512 MiB and the deadline is 300 seconds. Failure cleanup removes
owned field edits, restores the original EID, and closes the temporary CBuffer view.

This is real Qt callback and replay evidence. Programmatic Qt signal/action/text
dispatch does not claim physical mouse input. Publication means a successful native
setter plus GUI pipeline publication and a completed viewer replay queue drain,
rather than scanout timing.
The initial capture load, catalog export, native exposure and explicit EID navigation
are excluded from the continuous-input heartbeat interval.
