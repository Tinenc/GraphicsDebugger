# Selected UEX SkeletalMesh delivery

Use this reference for an explicitly selected cooked SkeletalMesh or modular
assembly. Keep the selected exact virtual asset paths and variant coherent;
a folder or similar name is not the requested asset. The Host-managed
`start_unpacked_asset_blender_export` returns one operation identity; query
`get_unpacked_asset_job_status` for its terminal result. A slow running job
is not a reason to start the same export again.

Each completed bundle has a `uex.blender-bundle.v1` manifest with exact
source asset, geometry/LOD, Skeleton, morph, material, texture and warning
facts. The `uex.blender-assembly.v1` manifest binds the selected bundles,
final `.blend`, hashes and validation. Manifest paths remain relative to
the managed artifact root.

If modular parts or LODs share one armature, their bones, parent hierarchy,
rest transforms and mesh weights must be compatible. Do not silently
replace native weights, merge incompatible rigs or claim pose fidelity from
a plausible rest pose alone. A GLB fallback can be useful but may lose
rig detail; identify its geometry source in the result.

Blender materials derived from cooked parameters are review
approximations. A filename suffix alone does not prove a Base Color or normal
binding. Cooked Shader bytecode is not the original Unreal material graph;
unsupported custom shading, layer and animated behavior remain explicit
fidelity gaps.

Treat a nonzero job or validator result as a failed delivery even if a
partial `.blend` exists. Validation should cover meshes, skinned armature
references, assigned weights, visible intended LOD and referenced images
for the requested assembly. A partial result can be continued with
`continue_unpacked_asset_blender_export` using the exact parent operation
and request/result hashes; completed bundles should be retained rather than
exported again. Report the final `.blend`, manifests, validator receipt
and remaining limitations together.
