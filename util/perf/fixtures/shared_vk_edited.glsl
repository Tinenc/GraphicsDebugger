#version 450 core
// Generated replacement for the VK_Shared_Buffer correctness fixture.
layout(set = 0, binding = 0, std140) uniform SharedParameters
{
  vec4 beforeTint;
  vec4 tint;
  vec4 afterTint;
};
layout(location = 0) out vec4 color;
void main() { color = tint * vec4(0.5, 0.75, 0.5, 1.0); }
