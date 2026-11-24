# RenderDoc Ultra development rules

> **EID performance worktree:** When working in `renderdocUltra-eid-perf`, read [`WORKTREE_ISOLATION.md`](./WORKTREE_ISOLATION.md) before building, deploying, or running it. Keep that worktree's build and test output isolated. Explicitly requested source integration into the main branch must preserve the main checkout's existing work; these isolation rules do not prohibit that integration.

> **Read BOTH this file and [`CLAUDE.md`](./CLAUDE.md) before making changes — together they are one unified rule set.** OpenAI Codex and other agents read `AGENTS.md`; Claude Code reads `CLAUDE.md`; each points at the other so neither agent misses a rule. They are complementary, not duplicates: **`AGENTS.md`** (this file) is canonical for the UI/Bread **localization (i18n) rules** below; **`CLAUDE.md`** is canonical for project structure, the build/release pipeline, upstream sync, and pitfalls. If a rule's home is the other file, follow it there and keep the two in sync.

## Tooling

- Prefer command-line, API, MCP, and background workflows. Avoid controlling desktop applications unless no reliable non-interactive path exists.
- Let pytest use its operating-system temporary directory. Never pass a repository-relative or repository-contained `--basetemp`; when a named scratch path is genuinely needed, place it under the absolute system temp directory and clean it after the run. Do not create `.pytest-*`, `.tmp-pytest-*`, `.test-tmp-*`, `_pytest_*`, or `.tmp-test-*` directories in a checkout.

## Release packaging entry point (mandatory)

Build a release package only when the user explicitly authorizes packaging for
this task. A local Development build or deployment is a separate operation.

For any packaging, portable ZIP, release, or distribution request (including “打包”/“发布”),
read [CLAUDE.md — Release Build Guide](./CLAUDE.md#release-build-guide),
[`replaylab/packaging/RELEASE_REQUIREMENTS.md`](./replaylab/packaging/RELEASE_REQUIREMENTS.md),
[`replaylab/packaging/README.md`](./replaylab/packaging/README.md), and
[`replaylab/dependencies/README.md`](./replaylab/dependencies/README.md) before building or
assembling artifacts. These checked-in files carry the standing packaging requirements;
do not depend on another chat, a previous release directory, or the AI's memory.
Use the current package configuration and sealed dependency inputs. Preserve the complete
standard feature inventory unless the user explicitly selects a smaller edition. Complete
the documented artifact checks before delivery; packaging does not authorize overwriting
or stopping a running application. Keep release rules in those canonical documents and
update their links here when the workflow changes.

For releases consumed by the in-app updater, follow the
[automatic update release contract](./replaylab/packaging/RELEASE_REQUIREMENTS.md#自动更新发布约定)
and the configured release metadata in the package file's autoUpdate section.

## Branch and release completion (mandatory)

Keep work under development on its local task branch with changes uncommitted.
Implement, test and report the reviewable result while retaining the branch.
Passing checks, completing development, and updating the local Development
application do not grant approval to submit or release the work.

Wait for the user to confirm the work and explicitly authorize each intended
operation before committing, pushing, opening a PR, merging, deleting a branch,
building a release package, tagging, uploading or publishing. Approval for one
operation covers only that operation unless the user explicitly authorizes a
bundle of steps. This rule also applies to documentation changes. An explicitly
requested rollback authorizes the targeted correction only.

Use [CLAUDE.md — Branch and release completion](./CLAUDE.md#branch-and-release-completion)
and [the release completion requirements](./replaylab/packaging/RELEASE_REQUIREMENTS.md#8-github-主分支合并与分支清理)
for authorized steps. Preserve unrelated dirty checkouts, other tasks' branches
and published tags.

## Product name and compatibility

- Routine Development updates follow [CLAUDE.md — Development update delivery](./CLAUDE.md#development-update-delivery), including the standing authorization to deploy, restart and restore selection without repeated confirmation.

- Persistent-data upgrades must follow [CLAUDE.md — Persistent data compatibility](./CLAUDE.md#persistent-data-compatibility), including old-format recovery, path identity and preservation of user data.

- **RenderDocBread** is the application/release name; **Bread** is its product family and **Bread AI** is the assistant. Menus, titles, help, diagnostics, new default project names, and product documentation must use this brand.
- Retain legacy `ReplayLab` identifiers only where compatibility requires them: existing storage paths and project data, protocol/schema values, environment variables, executable/module/class names, and the `ReplayLabProjects` translation context. These technical identifiers are not a UI brand and must not be renamed by a presentation-only change.
- Preserve the names and paths of existing user projects. Only defaults for newly created projects use Bread.

## UI localization (mandatory)

These rules apply to every new or modified qrenderdoc user interface. English source text remains the canonical source; Simplified Chinese is supplied by Qt TS/QM catalogs.

- Before changing qrenderdoc UI, read `util/i18n/README.md` and `qrenderdoc/Translations/glossary.zh_CN.yaml`.
- Every user-visible C++ string must use `tr()`, `QCoreApplication::translate()`, or the documented deferred-translation pattern. Qt Designer `.ui` strings must remain translatable. Never build a sentence from separately translated fragments.
- Do not hide source literals behind convenience wrappers such as `uiText("...")`; Qt `lupdate` cannot discover those calls. Use a directly recognized translation call, or register every deferred source with `QT_TRANSLATE_NOOP` and translate it later under the identical stable context.
- Backend/runtime data shown in the UI must carry a stable message or preset ID. Localize known messages in qrenderdoc through an explicitly extractable ID/source mapping and preserve unknown values as diagnostics; do not make raw backend English the normal label, hint, or button path.
- Non-user-facing protocol values, JSON keys, CSS/HTML implementation text, API identifiers, paths, resource names, and serialization values must not be translated. A new literal that intentionally bypasses extraction must have a specific `i18n-ignore` reason; do not expand `util/i18n/source_literals_baseline.json` to hide new findings.
- Compact graphics-pipeline nodes are identifiers and must always use their API abbreviations. For D3D use `IA`, `VS`, `HS`, `DS`, `GS`, `RS`, `PS`, `OM`, and `CS`; preserve the corresponding OpenGL/Vulkan abbreviations such as `VTX`, `TCS`, `TES`, `FS`, `FB`, `TS`, `MS`, and `AS`. Do not translate or expand these node labels. Full stage names may be translated in tabs, tooltips, accessible descriptions, help, and explanatory prose.
- Preserve established technical tokens such as `SRV`, `UAV`, `CBV`, `RTV`, `DSV`, API names, shader semantics, format names, and function names unless the surrounding prose explicitly explains them.
- Translate ambiguous graphics terms by context. In particular, `Normal` is “法线” for geometry/vector data, “常规” for a mode or setting, and “正常” only for status/health. Add a translator comment or disambiguation context when the English source alone is ambiguous.
- Preserve placeholders, rich-text structure, keyboard accelerators, line breaks with semantic meaning, and code/command text. Prefer complete, natural Chinese sentences over word-for-word substitution.
- When a UI feature is added or changed, update and review the catalog, then run the synchronization and quality gates below. New entries may not remain `unfinished` in a distributable build.

```powershell
$qtBin = "C:\Qt\5.15.2\msvc2019_64\bin"
./util/i18n/update_translations.ps1 -QtBin $qtBin
./util/i18n/update_translations.ps1 -QtBin $qtBin -Check
./util/i18n/build_translations.ps1 -QtBin $qtBin -RequireComplete
python ./util/i18n/lint_ts.py ./qrenderdoc/Translations/qrenderdoc_zh_CN.ts --glossary ./qrenderdoc/Translations/glossary.zh_CN.yaml --source-root . --source-baseline ./util/i18n/source_literals_baseline.json --require-complete
```

- On memory-constrained Windows machines, build qrenderdoc with `MSBuild ... /m:2`; do not run multiple full builds concurrently.

## Bread built-in Python integration localization (mandatory)

- The canonical Bread bridge source is `replaylab/renderdoc_mcp_bridge/` in this repository. It is packaged at `x64/<Configuration>/integrations/replaylab/python/renderdoc_mcp_bridge/` and loaded by qrenderdoc from that fixed first-party path. It must not be discovered through the Python extension scanner, shadowed from `%APPDATA%`, or given an `extension.json` manifest.
- Python-created user-visible text must call `QtCore.QCoreApplication.translate("ReplayLabProjects", "English source")` directly. Do not hide source literals behind a custom translation wrapper. Use positional placeholders such as `%1` and `%2`, then insert project names, paths, diagnostics, API values, and identifiers after translation. Bread's Python extractor does not support Qt numerus, so `%n` is forbidden until that pipeline explicitly implements plural catalogs.
- Bread reuses qrenderdoc's installed translator; do not install a second `QTranslator` or infer the active language from the raw `UILanguage` value.
- `qrenderdoc/Translations/ReplayLabProjects.cpp` is generated by `util/i18n/extract_replaylab_translations.py`. Never edit it by hand. Reviewed Simplified Chinese text is maintained in `util/i18n/replaylab_zh_CN.json` and synchronized into the main TS catalog.
- Put split project-UI modules under the canonical bridge as `project_*.py`; they are discovered recursively for extraction and packaged automatically. Project UI kept in `__init__.py` must remain inside the `replaylab-project-i18n-begin` / `replaylab-project-i18n-end` markers.
- New or changed Bread project UI must pass `util/i18n/update_translations.ps1 -Check`; the AST gate rejects raw English or Chinese labels, buttons, titles, item/tab text, menus, tooltips, file-dialog text, status text, message-box text, indirect local constants, unsupported `%n`, and the wrong translation context.
- Preserve `Bread`, `RenderDoc`, `RDC`, `CBuffer`, `EID`, `API`, `ABI`, paths, project names, file names, format names, and shader/API identifiers. Translate `Shader` as “着色器” in prose and keep compact graphics-stage abbreviations unchanged.
- A distributable change is not complete after `lrelease`: relink qrenderdoc with `/m:2` and an explicit `SolutionDir`, verify the canonical Python integration at its fixed application path, and restart qrenderdoc:

```powershell
$solutionDir = (Resolve-Path .).Path.TrimEnd('\') + '\'
MSBuild qrenderdoc/qrenderdoc_local.vcxproj /m:2 /t:Build /p:Configuration=Development /p:Platform=x64 "/p:SolutionDir=$solutionDir"
./util/i18n/deploy_replaylab_integration.ps1 -Configuration Development
```
