# Shader Workspace UI probes

These fixtures exercise the existing native ShaderCodeEditor and the first-party
Shader Workspace panel. Run them with qrenderdoc's `--ui-python` option in an
explicitly selected test instance. Do not inject them into a user's unrelated
capture. Neither fixture starts or stops qrenderdoc itself.

`qrenderdoc_cbuffer_live_edit_smoke.py` exercises the native CBuffer editor in its
normal transient dock. Supply an explicit capture at launch and the
`REPLAYLAB_CBUFFER_EDIT_EVENT`, `_BIND` and `_OUTPUT` environment variables.
`_FIELDS` is a JSON list of actual displayed scalar/vector/colour fields. The
probe drives slider, numeric and colour controls, compares the whole reflected
buffer before/after/reset, and records UI timer gaps. It does not compile or
write shader source. Run in an owned instance, and close it on failure so an
incomplete verification never leaves temporary value overrides in the user app.

## What the editor currently provides

| Capability | Default implementation | Boundary |
| --- | --- | --- |
| Function and variable colours | Local declaration index colours recognised names; comments and strings are excluded. | This is a lightweight index, not semantic token classification. Types and other declarations can share the variable colour. |
| Matching-name highlight | The identifier at the cursor marks equal names in the current document. | Equal names in separate scopes can all be marked; this is not a compiler-resolved reference search. |
| Completion | `Ctrl-Space`, or typing a prefix, suggests language keywords and indexed names from registered source documents. A selected item inserts text. | Local suggestions do not filter every member by its actual type or validate overloads. LSP labels may supplement them when a server is configured. |
| Definition navigation | `F12` queries the configured or bundled compiler-backed language server using the current URI and unsaved source. Requests wait for initialization and obsolete requests are cancelled. | Empty semantic results never fall back to a name match. With no provider available, explicitly limited lexical navigation is restricted to the current file and explicit includes; ambiguous overloads are rejected. The Shader Workspace opens only documents already authorised in its models. |
| Reference navigation | A native `references` provider is preferred. A definition-only provider checks at most 256 token candidates against the exact compiler declaration URI and range, with bounded concurrency, timeout, cancellation and revision-keyed caching. | This fallback scans opened documents in the selected compilation unit, not unopened project files. Token spelling only selects candidates; it never proves a reference. A single reference jumps directly; multiple references open a chooser. |
| Parameter hints | Indexed function signatures and a small built-in signature set are displayed. | This is not complete signature help for every HLSL intrinsic or overload. |
| Checks while editing | Debounced scanning detects unmatched or unclosed brackets, unfinished strings and unfinished block comments. Problems have underlines and a clickable count. | No unsaved-source compiler job is started. Without a language server, undefined names, type errors and resource-binding mistakes may only be diagnosed on save/compile. |
| Compiler errors | The owning workspace supplies the actual saved-source diagnostics. The summary jumps to a reported location. Old compiler locations are cleared when source changes. | UI injection tests prove presentation and navigation only. A real compiler failure needs separate live save evidence. |
| Flow layout | Initial and automatic resize fit preserve aspect ratio. Manual zoom/pan is retained per graph. `Fit` restores automatic fit; `100%` allows readable nodes with scrolling. | A long graph necessarily has smaller labels when fully fitted. It is a navigable algorithm summary, not a complete semantic proof. |

The pinned `slangd` package is discovered at
`integrations/shader-language-server/slangd.exe` before PATH. An explicit setting
or `RENDERDOC_SHADER_LANGUAGE_SERVER` overrides the bundled provider. Source
colouring, completion fallback and matching-name highlights remain lightweight
editor features; semantic navigation does not make those other features a
complete IntelliSense implementation.

`probe_shader_language_server.py` exercises a real provider directly over LSP:
HLSL overload types, scopes, member owners, includes, an unrelated same-name file,
and an unsaved type change. It needs no UI or RDC. The companion isolated
`qrenderdoc_shader_semantic_smoke.py` tests native F12 and compiler-backed reference
navigation; `REPLAYLAB_SHADER_SEMANTIC_OUTPUT` selects its receipt path. Run both
when changing the provider or the native semantic client.

The native fixture also opens a frozen preview under `ai/previews/` beside an
independent canonical editor. `SetIncludeBaseDirectory` must resolve that
preview's literal includes from the original source directory using absolute paths
only in the LSP text. Both buffers retain their distinct real URI, unsaved overload
resolution and real-file callbacks. No source text or extra file may be written to
disk. Relocated macro includes must report their unsupported boundary explicitly.

## Isolated native editor smoke test

Fixture: `qrenderdoc_shader_ide_smoke.py`.

Set `REPLAYLAB_SHADER_IDE_SMOKE_OUTPUT` to an absolute JSON path, then launch an
isolated qrenderdoc instance with `--ui-python <absolute fixture path>` and no RDC.
The fixture creates two temporary in-memory editors and a three-node flow graph.
It writes no HLSL file and performs no compile, replay or replacement.

The JSON contains 18 independent checks:

- initial fit, aspect ratio, resize fit, actual size, preserved manual zoom on
  resize and refresh, restored automatic fit, top-down layout, algorithm-line
  navigation;
- F12 to a function, the last formal parameter, and a hidden second document;
- a real visible completion popup containing two choices and accepted insertion;
- structural error display, injected compiler-error display and navigation,
  and removal of that obsolete diagnostic after a keystroke.

`ok` requires every check. The adjacent PNG is an actual widget capture; it
also provides visual evidence of function/variable colours and the matching-name
highlight. The fixture labels its compiler test `injected-ui-only`, deliberately
separating it from a real Host compile. A stale flow badge in this scratch image
is intentional: refreshing a stale graph exercises preservation of manual zoom.

The Release run on 2026-09-05 passed all 18 checks. Evidence is in
`output/shader-algorithm-20260905/shader-ide-smoke-release.json` and the adjacent
PNG. `release-build-verification.json` records the packaged Bridge comparison.
These receipts identify that Release run; they do not certify unrelated later
changes to shared renderer source files.

## Read-only real delivery probe

Fixture: `qrenderdoc_shader_delivery_probe.py`.

Required environment variable:

- `REPLAYLAB_SHADER_DELIVERY_OUTPUT`: absolute JSON receipt path.

Optional environment variables:

- `REPLAYLAB_SHADER_DELIVERY_WORKSPACE`: exact expected workspace ID;
- `REPLAYLAB_SHADER_DELIVERY_SOURCE_SHA256`: exact expected canonical source SHA;
- `REPLAYLAB_SHADER_DELIVERY_EVENT_ID`: expected selected event, default `11329`;
- `REPLAYLAB_SHADER_DELIVERY_TIMEOUT_SECONDS`: bounded wait, default `240`.

The caller is responsible for loading the intended RDC, selecting the event and
opening the selected editable live document. The fixture waits for that state
using a UI-thread timer. It never chooses a capture or event, opens the workspace
on the caller's behalf, edits source, compiles, applies replacements, or changes
aliases. A receipt that still says `waiting-...`, or contains an error, is not a
successful delivery result. Use `stage == "complete"` and `ok == true`.

The probe reads the actual editor, canonical file and active flow metadata. It
records read-only status, source hashes, node locations with their actual source
lines, graph bounds, view scale and a workspace screenshot. A newline-normalised
comparison proves the text content: Scintilla can project mixed or CRLF line
endings differently from the canonical file. Raw-byte hashes and editor-text
hashes are kept distinct; do not claim they are identical unless they actually
are. The graph is separately checked against the canonical raw-source hash.

The probe waits for a nonempty published algorithm graph, so an empty scene that
happens to fit cannot pass. It opens and shows temporary read-only native CBuffer
viewers for the current reflected blocks; hidden viewers deliberately defer
their reads. Model loading has a separate 60-second bound and a recorded
`allNativeCBufferModelsLoaded` result. Block aliases are displayed in each viewer's **window title**;
member values are read from its actual `fixedVars` model. The alias JSON store is
not used as UI evidence. The expected six block aliases are `GlobalsCB`, `ViewCB`,
`ToonMainLightCB`, `CartoonBasePassCB`, `MaterialCollection0CB` and `MaterialCB`.
Each viewer has its own screenshot and model-row sample. Only viewers newly
created by the probe are closed afterward; existing viewers remain open. The
final receipt also checks that the selected event and capture session did not
change.

This delivery probe proves the shown document, flow and aliases. It does not
prove save/compile/apply, numeric shader equivalence, or cross-RDC reuse; those
require their separate Host and live-workflow receipts. It also does not exercise
every real graph node's mouse event: the smoke test validates navigation wiring,
while the delivery receipt records real source locations for review.
