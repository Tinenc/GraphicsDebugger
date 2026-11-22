// Generated replacement for the D3D12_Shared_Buffer correctness fixture.
cbuffer Shared : register(b0) { float4 tint; };
float4 main(float4 p : SV_Position, float4 c : COLOR0, float2 uv : TEXCOORD0) : SV_Target0
{
  return tint * float4(0.5, 0.75, 0.5, 1.0);
}
