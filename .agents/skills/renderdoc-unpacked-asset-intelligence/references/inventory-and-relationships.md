# Project asset identities and relationships

The active project's configured roots and provider profiles bound file access.
A search term, filename or returned path is evidence, not permission to scan
unrelated roots or execute that path. An index is useful for broad reuse, but
a direct authorized read may answer a narrow question without indexing.

A stable asset observation identifies its project/provider, snapshot when
used, normalized virtual path, asset kind, content or provider hash when
available, parser coverage and missing/unsupported fields. Timestamp is only
a change hint. An index snapshot can become stale; do not merge records by
basename or present a removed historical asset as current.

Relations such as mesh-material, material-texture, Skeleton-animation and
capture-unpacked links should carry both endpoint identities and their
evidence. An observed or deterministic relation is stronger than a ranked
heuristic. Directory proximity, label similarity and filename suffix remain
candidate hints. Resolve an exact asset by its provider and normalized
virtual path; check relevant hashes, dimensions, Shader identity or binding
data when the question needs stronger matching.

`query_active_shader_asset_evidence` supplies project-backed Shader/asset
associations. `inspect_resource_export` supplies capture-side resource
evidence. `prepare_active_capture_project` is an effectful operation used
only when the requested project correlation requires it. Correlation does
not itself authorize exporting the capture.

Reuse a current compatible snapshot or completed job when it answers the
question. Report the identity, source evidence, coverage and ambiguity
needed to assess the match; a failed parser is a capability limit, not a
negative asset finding.
