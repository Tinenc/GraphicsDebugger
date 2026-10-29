# Batch FBX Exporter (Auto Matrix + ExtraUV)

This is the merged, canonical package for the two local exporter variants:

- `batch_fbx_exporter_removed_20260910`: Auto Matrix baseline.
- `batch_fbx_exporter_ExtraUV`: the same baseline plus selectable UV component swizzles.

The merged package keeps all baseline behavior and adds the ExtraUV mapping syntax. Attribute mapping fields accept ordinary names such as `TEXCOORD0` and UV channel selections such as `TEXCOORD0.xy` or `TEXCOORD0.zw`.

## Included functionality

- Batch export by EventID range.
- Per-DrawCall transform matrix reading, auto-detection, and transpose control.
- Position, normal, tangent, binormal, color, and UV1-UV5 export.
- Selectable UV components using `xyzw` or `rgba` suffixes.
- Texture export and optional SPIR-V to Unity shader conversion.
- Bundled `spirv-cross.exe` for shader conversion.

The historical `*_removed_*` directory is only a local backup and is not a second extension package.
