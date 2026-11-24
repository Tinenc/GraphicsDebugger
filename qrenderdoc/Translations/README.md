# QRenderDoc translation catalogs

`qrenderdoc_zh_CN.ts` is the reviewed Simplified Chinese source catalog. Edit the TS file (or use
Qt Linguist); never edit `qrenderdoc_zh_CN.qm` by hand. The QM file is a generated runtime artifact.

`qt_zh_CN.qm` supplies translations for standard Qt widgets and dialogs. The build helper copies it
from the selected Qt 5 installation. It originates from Qt's `qttranslations` module and remains
subject to the applicable upstream Qt licensing; RenderDoc's existing Qt acknowledgement is in
`docs/credits_acknowledgements.rst`.

From the repository root, update and build catalogs with:

```powershell
./util/i18n/update_translations.ps1
./util/i18n/build_translations.ps1 -RequireComplete
```

The complete terminology, protected-token, source-extraction, and review policy is documented in
`util/i18n/README.md` and `glossary.zh_CN.yaml`.
