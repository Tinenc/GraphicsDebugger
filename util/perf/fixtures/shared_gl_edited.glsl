#version 410 core
// Generated replacement for the GL_Shared_Buffer correctness fixture.
layout(std140) uniform Shared { vec4 tint; };
layout(location = 0) out vec4 color;
void main() { color = tint * vec4(0.5, 0.75, 0.5, 1.0); }
