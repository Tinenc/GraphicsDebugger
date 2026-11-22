"""Expected display values shared by real compiler and GPU checks."""


def cases(language, gles=False):
    hlsl = language == "hlsl"
    def vector(base, width):
        if width == 1:
            return base
        return base + str(width) if hlsl else {"float": "vec", "uint": "uvec", "int": "ivec", "double": "dvec", "bool": "bvec"}[base] + str(width)
    result = []
    def add(name, base, values, expected, mode="custom", channel=-1, components=4, kind="float", **options):
        typename = vector(base, len(values))
        initial = values[0] if len(values) == 1 else typename + "(" + ",".join(values) + ")"
        outtype = vector(kind, components)
        if hlsl:
            source = outtype + " main():SV_Target0 {" + typename + " probe=" + initial + ";return (" + outtype + ")0;}"
        else:
            prefix = "#version 320 es\nprecision highp float;precision highp int;\n" if gles else "#version 450\n"
            source = prefix + "layout(location=0)out " + outtype + " color;void main(){" + typename + " probe=" + initial + ";color=" + outtype + "(0);}" 
        result.append(dict(name=name, source=source, byteStart=source.index("probe="), language=language,
                           member="", iteration=0, mode=mode, channel=channel, expected=list(expected),
                           targetComponents=components, targetType=kind, **options))
    add("custom-scalar", "float", ["0.25"], [.25,.25,.25,1])
    add("custom-float2", "float", ["0.2","0.4"], [.2,.4,0,1])
    add("custom-float3", "float", ["0.2","0.4","0.6"], [.2,.4,.6,1])
    add("custom-float4", "float", ["0.2","0.4","0.6","0.8"], [.2,.4,.6,.8])
    add("custom-bool", "bool", ["true"], [1,1,1,1])
    add("custom-int", "int", ["-7"], [-7,-7,-7,1])
    if not gles:
        add("custom-double", "double", ["0.125"], [.125,.125,.125,1])
    for channel in range(4):
        value = [.2,.4,.6,.8][channel]
        add("raw-channel-" + "rgba"[channel], "float", ["0.2","0.4","0.6","0.8"], [value,value,value,1], mode="raw", channel=channel)
    add("custom-channel-b", "float", ["0.2","0.4","0.6"], [.6,.6,.6,1], channel=2)
    add("grayscale-alpha", "float", ["0.2","0.4","0.6","0.8"], [.8,.8,.8,1], mode="grayscale", channel=3)
    add("grayscale-rgb-luminance", "float", ["0.2","0.4","0.6"], [.37192,.37192,.37192,1], mode="grayscale")
    add("normal-channel", "float", ["0.2","0.4","0.6"], [.8,.8,.8,1], mode="normal", channel=2)
    add("range-channel", "float", ["0.2","0.4","0.6"], [.5,.5,.5,1], mode="range", channel=1, rangeMin=0, rangeMax=.8)
    add("narrow-custom-uint", "uint", ["4294967295u"], [4294967295], components=1, kind="uint")
    add("narrow-custom-uint2", "uint", ["4294967295u","16777217u"], [4294967295,16777217], components=2, kind="uint")
    add("narrow-raw-uint-alpha", "uint", ["17u","23u","29u","4000000001u"], [4000000001], mode="raw", channel=3, components=1, kind="uint")
    add("narrow-grayscale-uint", "uint", ["4000000001u"], [4000000001], mode="grayscale", components=1, kind="uint")
    return result
