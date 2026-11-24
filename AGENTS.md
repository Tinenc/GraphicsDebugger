# DF + RenderDocBread development

This is the DF研究 fork, not Straw1997's release checkout. The original author instructions are archived in integrations/renderdoc-bread/upstream for provenance; their repository identity and publishing instructions do not configure this fork.

- Follow the user's TinecmaTools / GraphicsDebugger integration instructions. Use renderdoc-analyzer and the global renderdoc MCP; begin live work with ping.
- Preserve the existing DF capture/injection backend, qTinecmaTool.exe identity, qTinecmaTool user configuration, and installed renderdoc_mcp compatibility changes.
- Local full builds use build_bread.ps1. Default output is x64/BreadDevelopment; build_windows.ps1 remains the original native-only entry point.
- Use the locked Python 3.12 build environment and .NET SDK 10.0.302, and /m:2 for native builds. The GUI's embedded Python remains 3.6; its bridge files must stay 3.6 compatible.
- Do not overwrite binaries or bridge files used by a running application. Preserve local edits and avoid modifying installed shared extensions as part of ordinary builds.
- New UI strings use English tr() and reviewed Chinese translations. Run util/i18n/update_translations.ps1 -Check and build_translations.ps1 -RequireComplete with the locked Qt Linguist input.
- Keep pytest temporary output outside the checkout. For long-path-sensitive Windows tests use RENDERDOC_ULTRA_TEST_TEMP pointing to a short OS temporary path.
- Never commit credentials, local profiles, game captures or generated caches. Keep the upstream MIT license and bundled dependency notices.
- Source, unit tests, real replay tests, model/provider login tests and game-specific verification are different evidence levels. Report them separately.
- Do not infer authorization to commit, push, publish or replace the DF build with upstream automatic updates from a local build request.

See docs/Bread_DF_工程架构.md, docs/Bread_DF_工具使用说明书.md and docs/Bread_DF_验证报告_2026-11-16.md.
