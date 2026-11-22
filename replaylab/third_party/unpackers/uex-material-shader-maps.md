# Explicit inline material ShaderMap evidence

The bundled UEX source patch adds this command without changing ordinary preview:

```text
uex --config <profiles.json> material-shader-maps --profile <name> --max-bytes 8388608 <exact-relative-material.uasset>
```

`uex-material-shader-maps.patch` is the retained source. The managed `_src/uex`
clone is ignored by the parent repository and may be refreshed by the fetch script.
`Fetch-Unpackers.ps1` applies the patch before building that clone, accepts an
exact already-applied patch, and stops if upstream changes prevent clean application.
It does not patch an explicit external `-UexSource` checkout or a prebuilt `-UexExe`.
Those routes must provide and prove the command themselves.

The implementation is `src/Uex/Core/MaterialShaderMapEvidence.cs`, the CLI binding
is in `src/Uex/Program.cs`, and tests are in
`tests/Uex.Tests/MaterialShaderMapEvidenceTests.cs`. The patch does not change
CUE4Parse, default preview flags, provider discovery, serve mode, or GUI tools.

The operation accepts one exact loose `.uasset` plus existing same-stem `.uexp`,
`.ubulk`, and `.uptnl` files. It admits no other files and does not initialize,
mount, or scan the provider tree. It requires an explicit `profile.game` EGame
and optional explicit `profile.usmap`; it does not choose version or mapping
candidates. The input limit is 16 MiB in total, with at most 256 package exports
and 64 resources per material. A private provider enables `ReadShaderMaps` and
disables referenced texture loading. Archive-only inputs return `unsupported`.

Stdout is one UTF-8 JSON document using `bread-material-shader-evidence/v1`.
`readShaderMaps.attempted` records the package read starting; `completed` and
`coverage.complete` are true only after parsing and input hash rechecks succeed
without reader warnings/errors. CUE4Parse exceptions that are internally caught
and logged still fail completion. Each material also reports whether inline
reading is applicable to that export. A successful empty result describes only
the exact material read; it does not establish the absence of compiled metadata
elsewhere. Complete success exits 0. Failed, unsupported, or over-budget reads
exit 1 with valid diagnostic JSON. Output above the requested budget is replaced
by a small failed envelope, never a truncated or partly accepted layout.

Each material also carries a deliberately small `materialPreview` projection from
that same exact package: export type/name, the raw imported parent object path,
and scalar/vector parameter overrides. Parent reconstruction reads only the
package import table; it never resolves the parent or referenced textures. The
Host can therefore follow a material-instance parent chain with one exact
`material-shader-maps` read per package instead of invoking ordinary preview,
which mounts and inventories the entire unpacked tree. Older external UEX builds
without this field remain compatible through the Host's ordinary-preview fallback.
Texture parameter values are represented by their exact import-table object paths;
the evidence reader does not load the referenced textures.

Each variant retains platform, feature/quality, typed cooked/resource hash
identities, numeric and legacy parameters, preshader headers/fields, full base64
preshader bytecode with SHA-256, default-value storage, serialized uniform layout,
and resource bindings. UE5.8's eight-byte hash wrapper is labeled opaque-64,
not SHA1. `DefaultValueOffset` is not GPU storage; raw preshader `BufferOffset`
also requires format/type interpretation before conversion to byte offsets.
Serialized resource layout size is separate from the preshader numeric extent.
This operation does not evaluate bytecode, choose a material variant, establish
ShaderArchive-to-capture identity, or apply aliases. Its receipt always sets
`rawBytecodeIdentityVerified` false.

Validation on 2026-09-13 used the existing `GAME_SilverPalace` profile with no
mapping override, and only the exact `MI_MuninDefault_C_Hat01_A` and
`M_CharacterPBR` packages. Both returned all eight variants, including SM5/SM6
and raw quality values Low/Medium/Epic/Num. Numeric parameters, fields, headers,
layout, preshader bytecode, and variant identity matched the earlier isolated
harness exactly. The final publish read the MI in 0.281 seconds. The same publish
also successfully decoded the BC7 `T_Drip_07` texture, preserving the previous
Detex initialization repair. Evidence and the isolated publish are under
`output/bread-common-textures-20260913/uex-material-cli-*`.

Targeted UEX tests: 33 passed, including invalid/corrupt inputs, path bounds,
explicit profile selection, swallowed parser errors, secret redaction, complete
UTF-8 output budgets, parameter/layout preservation, and hash type distinctions.
The parent Host's operation and CLI contracts are tested separately. Runtime
deployment preserves profiles and `.uex-cache`; the audited runtime differences
are listed in `uex-material-cli-runtime-diff.json` in that evidence directory.
