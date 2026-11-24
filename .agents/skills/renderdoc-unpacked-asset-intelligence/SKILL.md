---
name: renderdoc-unpacked-asset-intelligence
description: Search and relate authorized unpacked project assets, or produce a requested UEX report or exact cooked SkeletalMesh Blender delivery.
metadata:
  replaylab-category: unpacked-asset-intelligence
  replaylab-version: "31"
---

# Unpacked project assets

The Agent chooses the appropriate search or export method within the active
project's authorized roots. A failed reader means that format may be
unavailable; it does not prove the asset is absent. Stable observations,
source hashes and exact virtual paths are stronger evidence than names or
heuristic rankings.

Choose readers by the source engine, requested capability and configured project
route; the eventual Unity destination does not imply a Unity source reader.
Reuse the current index and completed reads. Use a fallback for a concrete
unsupported-format or reader failure, not merely because a guessed name did not
match, and do not run every enabled reader against the same resource. Missing
readers leave asset identity unresolved; Shader analysis can continue from the
capture with algorithm-based inferred names and export can use captured data.
Keep stable source identity and evidence provenance when readers disagree; do
not silently replace a stronger association with the last returned name.

For direct lookup, use `search_unpacked_assets`,
`list_unpacked_asset_directory` or `read_unpacked_asset`. For reusable catalog
queries, use `get_active_unpacked_asset_index_status`,
`index_active_unpacked_assets` and `query_active_unpacked_assets` as needed.
`verify_unpacked_asset_readers` reports reader availability. For a selected
capture resource, `query_active_shader_asset_evidence` and
`inspect_resource_export` can supply matching evidence; a candidate name or
score alone is not an exact link.

For an EID-to-material investigation, begin with the draw's bound textures or
mesh, then follow their asset dependencies/referrers and material-instance parent
chain. Start with a few distinctive anchors; matching every bound texture is not
required. Use proven anchors to narrow dependencies and inspect the next resource
only when it resolves ambiguity. This route applies across engines/platforms
through the corresponding asset reader and captured-layout adapter, not a
mandatory UEX/Unreal operation for every project.
Inspect content coverage separately from directory/index completion. An
index with no texture thumbnails has not tested texture similarity: select the
small candidate set and use the configured texture reader. A successful
`uex_preview_texture` operation registers its current, source-bound preview for
subsequent catalog comparisons; inspect its `texturePreviewIndex` receipt and
resolve a reported indexing gap instead of treating missing thumbnails as a
negative match. Reuse those decoded results while their inputs remain current.
Let the Host reuse its mounted reader session instead of starting a fresh reader
or rescanning the unpacked directory per texture. Unmatched unrelated bindings
do not invalidate supported links; shared/default textures alone do not identify
the material. Keep the associated material's parameter names, values, overrides
and unresolved locations in `materialSemanticContext`. The Agent can combine
this vocabulary with observed calculations to interpret Shader functions and
tentative source names; it is not authority to publish physical CBuffer aliases.
Reuse the retained context instead of rerunning the search during organization.
After organizing the Shader, refine parameter roles from its consumers and
publish semantic fields with confirmed or inferred provenance. Known physical
lanes can support inferred roles without original names; regroup display lanes
by meaning without changing capture layout. Source renaming and panel publication
are separate effects: a parameter dictionary proves neither. Reuse this
investigation for both.
Read [the Shader naming guide](../renderdoc-shader-workbench/references/asset-naming.md)
for physical CBuffer layouts, continuous-value windows and post-process routes.

`start_unpacked_asset_report` requires the Host-supported UEX report inputs;
a Strong Inventory matters for that report, not for every asset question.
`start_unpacked_asset_blender_export` uses exact cooked virtual SkeletalMesh
paths. Their terminal job, manifest and validator receipts describe which
artifacts actually completed. Do not claim that an approximate Blender
material reproduces the original Unreal Shader graph.

Compare the request with receipts and record coverage, gaps and approximations
through `record_task_review`. Reuse lookup/job/manifest/validator evidence. Resolve
missing deliverables within authorized roots without repeating successful exports;
reassess affected items. When blocked, name the actual reader/access/format limit
and unmet requirement. No supported match does not mean an unavailable reader
proved the asset absent.
Read only relevant detail in
`references/inventory-and-relationships.md`, `references/report-semantics.md`,
`references/uex-blender-delivery.md`. These references describe formats and
correctness boundaries, not a required tool order.
