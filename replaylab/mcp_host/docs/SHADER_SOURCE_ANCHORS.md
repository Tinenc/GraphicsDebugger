# Bread Shader source anchors

Debug snapshots, the rendering directory and function-flow annotations resolve
against the current source. A line number is display information, not identity.
Snapshots continue to store a recipe and display settings, never rendered pixels
or compiled Shader replacements.

## Resolution order

1. While a buffer is open, the existing native UTF-8 range handles follow editor
   operations, including undo/redo and supported cut/paste moves. A known native
   deletion cannot be repaired by searching for another same-name variable.
2. Existing exact token/context matching remains available for legacy snapshots.
3. The shared `shader_structural_anchors.py` matcher first associates a function
   and a statement, then an expression within that statement. It compares lexical
   scopes, declarations, parameter types, assignment targets and neighboring
   statements. Selected call arguments have distinct structural roles even when
   the same variable occurs more than once.
4. Compatible declaration changes can establish a unique local, parameter or
   function rename. Numeric edits and assignment-operator edits do not by
   themselves destroy statement identity. No match is selected by nearest line.

An example is selecting the second `a` in `mix(a, a, t)`: after changing `t` to
`t + 0.25`, it remains the second argument. A saved `result.albedo` statement can
follow an inserted preceding assignment and `=` becoming `+=`; the native
handle takes priority while editing. If two externally edited statements remain
indistinguishable, navigation stays unresolved.

This is a bounded, conservative shader syntax subset, not a compiler or complete
HLSL/GLSL grammar. Unsupported declarators, macros and transformations do not gain
invented semantic certainty. Existing lexical matching remains available where
the structural subset is inapplicable. Function processing is limited to 8,192
tokens, 1,024 statements and expression depth 96; small parsed-function caches
are bounded. Expensive matching and disk access run on the source-flow worker.

## Reopening and external edits

New semantic-overview v1 records may carry optional `navigationBodies` token
history. Legacy v1 records remain valid without it, and old reviewed receipts
retain their original digest. Old snapshot v1 records already contain function
tokens and require no format migration.

The private `shader_navigation` cache stores a separate checkpoint, bound to the
workspace, path, stage, original Shader and original annotation metadata digest.
It advances only when the worker's source equals the committed file. UTF-8 BOM
and CRLF/LF differences are normalized for that comparison. This prevents an
unsaved or discarded buffer from changing the next session's baseline.

Writes use the existing OS locking and atomic-replace discipline. Cache records
are limited to 512 KiB; missing, corrupt, stale or incompatible records are
disposable. Temporary failure retains evidence for later repair; confirmed
native deletion retains a tombstone. Annotations' titles/descriptions and original
review/source hashes are never rewritten by a navigation checkpoint. Successful
relocation is not a new semantic review and does not approve replay or ABI checks.

## Boundaries and validation

No source-only algorithm can recover original identity after arbitrary deletion
and replacement with indistinguishable copies without edit history. Removing the
target, duplicating candidates, changing declaration types or moving to an
incompatible scope may therefore remain unresolved. This is intentional: a wrong
debug location is worse than a visible unresolved location.

The matching and persistence layers have no GPU or graphics-API dependency. They
are shared by desktop hosts, including Android remote-replay sessions. Embedded
bridge modules remain Python 3.6 compatible. CPU regression coverage includes
Windows and Linux, Unicode offsets, local/parameter/function renames, call
argument roles, moves, changed expressions, saved/unsaved boundaries, external
edits, reopening, ambiguous/deleted targets and async worker publication. These
tests do not constitute Android-device or GPU replay validation.

Run from `replaylab/mcp_host` (pytest's normal OS temporary directory):

```text
python -m pytest tests/test_shader_structural_anchors.py tests/test_shader_visual_snapshots.py tests/test_shader_snapshot_live_anchors.py tests/test_shader_outline_live_anchors.py tests/test_shader_source_flow_async.py tests/test_shader_semantic_overview.py tests/test_shader_semantic_overview_review.py -p no:cacheprovider -q
```

## Design references

- [GumTree](https://github.com/GumTreeDiff/gumtree): syntax-aware correspondence
  across edits, moves and renames.
- [Tree-sitter incremental parsing](https://tree-sitter.github.io/tree-sitter/using-parsers/3-advanced-parsing.html):
  applying edits to source trees; node positions alone are not persistent identity.
- [Clang AST introduction](https://clang.llvm.org/docs/IntroductionToTheClangAST.html):
  distinguishing a declaration from its individual expression references.
- [ProseMirror mapping guide](https://github.com/ProseMirror/website/blob/master/markdown/guide/transform.md):
  mapping positions through edit operations.

These are design references. This change does not embed those projects, install
a new parser dependency or claim their full semantic capabilities.
