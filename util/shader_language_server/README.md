# Bread semantic Shader navigation

The application uses the official Slang compiler's `slangd` language server for
HLSL/GLSL/Slang definition navigation. `provider.json` pins the release archive,
binary hashes and license. The provider runs separately from the UI process and
does not compile or apply replay replacements.

Download the official archive URL in the manifest, then deploy it without adding
anything to PATH or changing the user's language-server setting:

```powershell
python util/shader_language_server/deploy_provider.py --archive C:/absolute/path/slang-2026.20-windows-x86_64.zip --application x64/Development
```

The script checks the complete archive before writing and installs only the
semantic runtime (`slangd.exe`, `slang-compiler.dll`) and the release licenses.
The resulting `integrations/shader-language-server/provider.json` lists hashes
for all shipped files. Include this entire directory in the application package.
The Windows x64 footprint is about 27 MB. Neither the 84 MB LLVM backend nor shader codegen
tools are needed by this feature.

Normal x64 native builds run the independent `DeployShaderLanguageServer` target,
including builds with `SkipReplayLabRuntime=true`. The target verifies an existing
installation first, then repairs missing files using the pinned local archive at
`%LOCALAPPDATA%/Bread/provider-cache/` or `BREAD_SHADER_LANGUAGE_SERVER_ARCHIVE`.
It does not download during a build. The ZIP packager verifies all runtime and
license hashes. The MSI harvest includes the full `integrations/` tree, covering
both this provider and the canonical built-in Python bridge.

Run the real protocol probe after a provider update:

```powershell
python replaylab/tests/fixtures/probe_shader_language_server.py x64/Development/integrations/shader-language-server/slangd.exe --output C:/absolute/temp/provider-probe.json
```

The native UI counterpart is documented in
`replaylab/tests/fixtures/SHADER_UI_PROBES.md`. An explicit application setting or
`RENDERDOC_SHADER_LANGUAGE_SERVER` still takes precedence. Missing providers are
reported as limited local navigation, and empty semantic responses never turn
into a fallback match by name. Slang currently exposes definition queries but no
native references endpoint. Bread therefore checks a bounded number of candidate
locations against compiler-resolved declaration identity, caches by document
revision and reports the opened-document scope of that fallback.
