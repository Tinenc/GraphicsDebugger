# Editable material presentation

For generated Bread-owned ShaderLab shaders, declare
`CustomEditor "Bread.Editor.BreadRestoredShaderGUI"` at Shader scope. The
bundled Capture SDK provides this opt-in inspector. Keep a required existing
custom ShaderGUI and integrate the same layout into it; do not replace URP,
HDRP or third-party inspectors globally. An import receipt's
`material_inspector_not_connected` gap identifies an adaptation still needing
this connection. Presentation gaps do not block source import or rendering.

Put textures first, with previews, dimensions including volume/array depth,
format, mip count, sampling color space and filter/wrap information. Use
readable semantic ShaderLab display names. Preserve uniform names and physical
CBuffer bindings; original names remain visible in control tooltips.

Use ShaderLab `Color` (and `[HDR]` when warranted) for real color controls,
`Range(min,max)` for a physically/artistically justified finite range, and
native enum/toggle attributes for keywords. Group surface, lighting and detail
controls by purpose. Direction, position, UV/packed data are vectors, not
colors. Unknown numeric domains stay numeric; do not infer a 0–1 slider from
one captured value. Technical matrices, screen/history sizes and engine-bound
constants stay script-controlled rather than artist controls.

For a captured vector that must retain its vector ShaderLab declaration,
annotate its material `constantBuffers` transfer mapping, e.g.
`"inspector":{"label":"主色","group":"表面","control":"color","hdr":true,"alpha":false}`.
For a float, an explicitly justified slider uses
`"inspector":{"label":"粗糙度","group":"表面","control":"slider","min":0,"max":1}`.
Texture mappings accept `inspector.label/group/tooltip` too. Optional metadata
is stored in the material's namespaced importer data; old requests without it
retain native property controls. Invalid optional annotations fall back to
native controls and report diagnostics. They never change captured values.

Check that opening/repainting the inspector leaves values, keywords and render
state unchanged, including HDR colors and values outside suggested ranges.
Exercise edits, Undo, mixed multi-material values and a texture rebind, then
confirm the scene's rendering. This complements final independent Scene/Game
render comparisons; panel checks alone do not establish effect equivalence.
