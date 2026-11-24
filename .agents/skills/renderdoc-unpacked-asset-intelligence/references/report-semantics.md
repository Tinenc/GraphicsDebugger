# UEX asset report fields

This reference applies only to a requested UEX asset report. The Host route
`start_unpacked_asset_report` currently requires a concrete committed Strong,
complete, untruncated Inventory and a project-specific grouping specification.
It does not infer missing grouping rules from a sample game. If those rules are
materially unknown, resolve them from authorized project evidence or ask for
the missing choice. Reporting reads the selected Inventory; it should not
reopen paks to invent absent cooked metrics.

The grouping specification defines character parts, variants, scene scope
and exclusions. Apply its rules to actual virtual paths. Do not make
`Combat`, `Default/Show` or another game's layout a universal convention.
Keep unmatched in-scope content visible in the appropriate fallback category
rather than silently discarding it.

For reports requesting Role and Role+Weapon, aggregate the intended component
meshes per character/variant/state. Sum available triangles and vertices for
the selected LOD. Do not sum a shared Skeleton's bones once per mesh: group
by exact Skeleton reference, take the maximum active or reference bone count
per distinct Skeleton, then sum across distinct Skeletons. Associate
animations by exact Skeleton reference. Missing frame counts or durations
remain missing, not zero.

Follow mesh-material-parent-texture references for texture counts and
deduplicate exact texture assets before grouping by resolution. Scene
categories use the configured scope and exclusions. For available LOD0
triangle values, nearest-rank P90 is the sorted one-based value at
`ceil(0.90 * N)`; report how many values were missing from that population.

A completed report includes `asset-report.html`, `asset-report.json`,
`asset-summary.csv` and `asset-report-manifest.json`. The manifest binds
the selected Inventory, output names, sizes and hashes. A lone HTML preview
or job still running is not a completed report. Report grouping diagnostics,
coverage and missing values alongside the result.
