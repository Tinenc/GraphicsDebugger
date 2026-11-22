# Optional draw-intent search vocabulary

Use this page only when the capture suggests a draw purpose and extra search
terms would help locate unpacked assets. These words are not a classification,
algorithm checklist or expected Shader implementation. Prefer names supplied
by the user, capture markers and observed bindings. If source or asset evidence
disagrees with a term, discard it.

| Possible purpose | Search terms |
| --- | --- |
| Character surface | skin, hair, eye, cloth, sheen |
| Stylized rendering | toon, ramp, outline, matcap |
| Scene pass | depth, shadow, base pass, GBuffer, decal |
| Screen-space pass | bloom, blur, tone map, ambient occlusion, temporal |
| Effects and environment | particles, water, sky, cloud |
| Interface or transfer | UI, font, atlas, copy, blit |

A draw can combine purposes or fit none of these terms. Determine its actual
behavior from the capture and source rather than this table.
