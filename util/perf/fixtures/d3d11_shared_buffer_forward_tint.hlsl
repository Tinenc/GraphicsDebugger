// Matches util/test/demos/d3d11/d3d11_shared_buffer.cpp's captured PS interface.
// Checked shader replacement must approve the compiled ABI before this can be used.
cbuffer Shared : register(b0)
{
  float4 padding[16];
  float4 tint;
};

float4 main(float4 p : SV_Position, float4 c : COLOR0, float2 uv : TEXCOORD0) : SV_Target0
{
  return float4(1.0 - tint.rgb, tint.a);
}
