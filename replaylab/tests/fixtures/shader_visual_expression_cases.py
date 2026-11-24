"""Shared constant-output expression cases for real compiler/GPU acceptance."""


def cases(language, gles=False):
    hlsl = language == "hlsl"
    v3, v4 = ("float3", "float4") if hlsl else ("vec3", "vec4")
    prefix = "" if hlsl else ("#version 320 es\nprecision highp float;\nprecision highp int;\n" if gles else "#version 450\n")
    output = "" if hlsl else "layout(location=0) out vec4 color;\n"
    header = "float4 main() : SV_Target0" if hlsl else "void main()"
    def final(expr):
        return ("return " if hlsl else "color = ") + v4 + "(" + expr + ",0.0,0.0,1.0);"
    def case(name, helpers, body, expected, iteration=0, member=""):
        source = prefix + output + helpers + "\n" + header + " {" + body + "}\n"
        start = source.index("/*observe*/") + len("/*observe*/")
        return {"name": name, "source": source, "byteStart": len(source[:start].encode("utf-8")),
                "language": language, "entryPoint": "main", "iteration": iteration,
                "member": member, "expected": list(expected)}

    result = [
        case("helper-return-expression", "float helper(float x){/*observe*/return x*2.0+0.1;}",
             final("helper(0.25)"), (.6, 0, 0, 1)),
        case("helper-local-return", "float helper(float x){float local=x*2.0;return /*observe*/local;}",
             final("helper(0.25)"), (.5, 0, 0, 1)),
        case("helper-parameter-return", "float helper(float x){return /*observe*/x;}",
             final("helper(0.25)"), (.25, 0, 0, 1)),
        case("helper-second-invocation", "float helper(float x){return /*observe*/x;}",
             "float first=helper(0.2);float second=helper(0.6);" + final("first+second"),
             (.6, 0, 0, 1), iteration=1),
        case("helper-unreached-occurrence", "float helper(float x){return /*observe*/x;}",
             final("helper(0.25)"), (1, 0, 1, 1), iteration=1),
        case("helper-call-return", "float helper(float x){return x*2.0;}",
             final("/*observe*/helper(0.25)"), (.5, 0, 0, 1)),
        case("index-postincrement-once", "", "float values[2]=" +
             ("{0.2,0.6}" if hlsl else "float[2](0.2,0.6)") +
             ";int index=0;" + final("/*observe*/values[index++]"), (.2, 0, 0, 1)),
        case("short-circuit-skipped-rhs", "", "bool gate=false;float value=0.25;"
             "if(gate && /*observe*/value>0.0){" + final("1.0") + "}" + final("0.0"),
             (.25, 0, 0, 1) if hlsl else (1, 0, 1, 1)),
        case("short-circuit-taken-rhs", "", "bool gate=true;float value=0.25;"
             "if(gate && /*observe*/value>0.0){" + final("1.0") + "}" + final("0.0"),
             (.25, 0, 0, 1)),
        case("short-circuit-call-skipped", "float helper(){return 0.25;}",
             "bool gate=false;if(gate && /*observe*/helper()>0.0){" + final("1.0") + "}" + final("0.0"),
             (.25, 0, 0, 1) if hlsl else (1, 0, 1, 1)),
        case("short-circuit-call-taken", "float helper(){return 0.25;}",
             "bool gate=true;if(gate && /*observe*/helper()>0.0){" + final("1.0") + "}" + final("0.0"),
             (.25, 0, 0, 1)),
    ]
    intrinsic = "lerp" if hlsl else "mix"
    result += [
        case("integer-literal-sin-result", "", final("/*observe*/sin(1)"), (.841470985, 0, 0, 1)),
        case("mixed-clamp-result", "", final("/*observe*/clamp(1,0.0,0.25)"), (.25, 0, 0, 1)),
        case("integer-endpoints-interpolation", "", final("/*observe*/" + intrinsic + "(0,1,0.25)"), (.25, 0, 0, 1)),
        case("sign-overload-return-type", "float choose(int x){return 0.2;}" + v3 +
             " choose(float x){return " + v3 + "(0.6,0.7,0.8);}",
             v3 + " result=/*observe*/choose(sign(0.5));" +
             ("return " if hlsl else "color=") + v4 + "(result,1.0);",
             (.2, 0, 0, 1) if hlsl else (.6, .7, .8, 1)),
        case("intrinsic-argument", "", "float a=0.2,b=0.8,t=0.25;" +
             final(intrinsic + "(a,b,/*observe*/t)"), (.25, 0, 0, 1)),
        case("intrinsic-call-result", "", "float a=0.2,b=0.8,t=0.25;" +
             final("/*observe*/" + intrinsic + "(a,b,t)"), (.35, 0, 0, 1)),
    ]
    if hlsl:
        result.append(case("interpolation-vector-weight", "", "float3 result=/*observe*/lerp(0.0,1.0,float3(0.2,0.4,0.6));return float4(result,1.0);",
                           (.2, .4, .6, 1)))
    structure = "struct Payload {" + v3 + " tint;float roughness;};\n"
    make = "Payload make(){Payload p;p.tint=" + v3 + "(0.2,0.4,0.6);p.roughness=0.25;return p;}\n"
    result += [
        case("struct-helper-local-return", structure +
             "Payload make(){Payload p;p.tint=" + v3 + "(0.2,0.4,0.6);p.roughness=0.25;return /*observe*/p;}\n",
             "Payload p=make();" + final("p.roughness"), (.2, .4, .6, 1), member=".tint"),
        case("struct-call-result", structure + make,
             "Payload p=/*observe*/make();" + final("p.roughness"), (.2, .4, .6, 1), member=".tint"),
        case("struct-call-member", structure + make,
             final("/*observe*/make().roughness"), (.25, 0, 0, 1)),
    ]
    counter = ("static " if hlsl else "") + "float counter=0.0;\n"
    result.append(case("side-effect-call-second-hit", counter +
                       "float touch(){counter+=0.2;return counter;}\n",
                       "float sum=0.0;for(int i=0;i<3;i++){sum+=/*observe*/touch();}" + final("sum"),
                       (.4, 0, 0, 1), iteration=1))
    opaque = ("float unused(Texture2D<float4> textureArg,SamplerState samplerArg,float2 uv)"
              "{return textureArg.Sample(samplerArg,uv).x;}\n" if hlsl else
              "float unused(sampler2D textureArg,vec2 uv){return texture(textureArg,uv).x;}\n")
    result.append(case("unrelated-opaque-helper-parameter", opaque +
                       "float helper(float x){return /*observe*/x;}",
                       final("helper(0.25)"), (.25, 0, 0, 1)))
    texture = case("texture-sample-expression",
                   "Texture2D<float4> image:register(t0);SamplerState sampling:register(s0);" if hlsl else
                   "layout(binding=0) uniform sampler2D image;",
                   ("float4 sampled=/*observe*/image.SampleLevel(sampling,float2(0.5,0.5),0.0);return sampled;" if hlsl else
                    "vec4 sampled=/*observe*/textureLod(image,vec2(0.5),0.0);color=sampled;"),
                   (0, 0, 0, 1))
    texture["compilerOnly"] = True
    result.append(texture)
    if hlsl:
        array_source = ("struct Input {float4 position:SV_Position;float4 uv[2]:TEXCOORD0;};\n"
                        "float4 main(Input input):SV_Target0 {float value=input.uv[1].x;"
                        "return float4(/*observe*/value,0,0,1);}")
        position = array_source.index("/*observe*/") + len("/*observe*/")
        result.append({"name": "input-semantic-array-pass-through", "source": array_source,
                       "byteStart": len(array_source[:position].encode("utf-8")),
                       "language": "hlsl", "entryPoint": "main", "iteration": 0,
                       "member": "", "expected": [0, 0, 0, 1], "compilerOnly": True})
    if gles:
        # ESSL forbids the implicit int-to-float intrinsic conversions that
        # desktop GLSL permits. These original inputs do not compile in ESSL;
        # they are exercised on the four compiler routes where they are legal.
        result = [item for item in result if item["name"] not in {
            "integer-literal-sin-result", "mixed-clamp-result", "integer-endpoints-interpolation"}]
    return result
