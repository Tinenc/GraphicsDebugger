# Managed package and validation

The Host manages the artifact root, staging, immutable publication and request
identity. Use `prepare_resource_export`, `finalize_resource_export`,
`get_resource_export_status` and `validate_resource_export` according to
the selected route. Their operation receipts, not a filename or free-form
success message, establish effects. A changed capture or UI selection does
not authorize retargeting an existing request.

The manifest ties the package to its capture/session/EID, source resource and
Shader hashes, target/coordinate profile, source-to-output mappings,
dependencies, relative artifact paths, byte lengths and content hashes.
Custom vertex data or unsupported target features may use a sidecar or be
recorded as explicit loss. A relative path must stay inside the package.
An uncertain operation should be resolved from its status before retrying;
successful published artifacts should not be overwritten or regenerated
because another item failed.

Validation has separate meanings:

- **Structural:** manifest, paths, hashes, resource sizes and internal
  mesh/texture/material references agree.
- **Source fidelity:** topology and inputs, texture view/color/sampler
  behavior, CBuffer mapping, coordinate conversion and dependencies match
  captured evidence or declare an approximation.
- **Target:** an available, selected Unity or Blender validator confirms
  import/compile behavior for that target. If none ran, target validation is
  unavailable, not passed.

For Unity visual comparison, retain the actual graphics API, active color
space, UV/depth conventions and per-texture import/sampler/mip settings from
the receipt. Import success or a recognized `_ST` expression alone does not
verify image orientation or pixel equivalence.

After an operation changes replay state, use its restoration receipt to
confirm the original capture and EID. Report an uncertain restoration as an
open problem. A partial candidate package is not a published validated
package; report accepted artifacts and remaining gaps accurately.
