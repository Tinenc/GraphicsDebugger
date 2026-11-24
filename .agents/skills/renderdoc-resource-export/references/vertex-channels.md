# Vertex channels from capture to Unity

Start with the selected draw's vertex layout and Shader interface. Keep the
capture/EID/instance, input attribute index, semantic and semantic index,
location/register when available, buffer binding, format, component count,
byte offset, stride and instance rate together. Reflection order, buffer slot,
shader location and export attribute index are different identities. The
source bundle retains raw bytes; canonical values are a derived representation.

Read the vertex Shader and follow each used component through unpacking,
swizzles, transforms and downstream use. `analyze_vertex_input_roles` supplies
bounded source/disassembly use sites with line numbers and source hashes. Its
matches are lexical, not complete dataflow analysis. A normalize or cross call
does not prove a normal/tangent; COLOR may hold masks or an extrusion direction,
and TEXCOORD may hold several UV sets, a scalar ID or unrelated data. Check the
mesh/texture and matched original asset where they resolve ambiguity. Do not
normalize, recolor or regenerate these channels just to match a familiar name.

Map only established roles to POSITION/NORMAL/TANGENT. Preserve other values
and all their components. The transfer specification accepts
`attributeMappings: [{source: {attributeIndex: n}, target: "_CUSTOM"}]`; an index
means the inspected capture layout entry, never the reflection ordinal.
For a Unity custom channel, add `unity.vertexChannels: {"_CUSTOM": "TEXCOORD_3"}`
and use the matching HLSL input (for example `float4 packed : TEXCOORD3`). Choose
an unused supported destination, preserve dimensions and update the derived
Shader together. Do not assume that a glTF `_ATTRn` is a Unity Shader semantic.

glTF's standard TEXCOORD is two-component; Unity can carry up to four. Bread
preserves wider/scalar captured TEXCOORD as a glTF custom attribute with a
source-to-target receipt, and the managed importer restores its intended Unity
channel. `vertexBindings` records the source layout and the actual Shader
semantic/type; the Editor receipt also reports the installed vertex layout.
Check these against every input used by the derived Shader. Packed channels
need component-aware UV changes: a global V flip must not alter a mask or omit
the second UV pair. Preserve by default, then adapt verified components once.

The managed canonical path stores decoded Float32 values, including normalized
formats, without repeating their UNorm/SNorm decode in the Unity Shader. Integer
IDs/bitfields that cannot be represented exactly must use preserved raw bytes
with an integer vertex format or a matching buffer-read adapter; do not convert
large integers through float or reinterpret float bits as the original integer.
Packed/special formats, per-instance streams, skinning, procedural inputs and
missing channels require their appropriate target route, not a claim that a
static Mesh import restored them. Keep their successful exports and repair the
specific missing adapter through the connected Unity project tools.

After import, inspect the actual channel dimensions/values, compile the derived
vertex Shader and compare the selected draw's output in matching camera/pose.
Use a channel diagnostic Shader if needed to isolate a wrong attribute,
swizzle, normalization, winding or transform. Export/compile success alone does
not prove a faithful vertex interface or matching pixels.

Official references: Unity [vertex inputs](https://docs.unity3d.com/6000.0/Documentation/Manual/SL-VertexProgramInputs.html),
[vertex descriptors](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/Rendering.VertexAttributeDescriptor.html),
and [explicit mesh buffers](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/Mesh.SetVertexBufferParams.html).
