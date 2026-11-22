"""Storage-resource evidence for numeric Shader comparison (Python 3.6).

Storage descriptors are not necessarily writable: Vulkan readonly SSBOs still
appear in RenderDoc's readWriteResources. Only native binary decorations may
prove that distinction. Otherwise every bound resource is observed, not omitted.
"""
import re
import struct


def glsl_unbound_storage_reason(raw):
    """Check exact driver-returned source for addressing outside bound descriptors.

    This is not a source-level readonly proof. Ordinary GLSL storage always
    requires full resource A/B/A observations, regardless of readonly qualifiers.
    """
    if not raw or len(raw) > 4 * 1024 * 1024 or b"\x00" in raw:
        return "GLSL storage evidence requires bounded nonempty source without NUL"
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "GLSL storage evidence requires exact UTF-8 source"
    # Join preprocessing continuations before removing comments so split
    # extension names/directives cannot hide unbound addressing.
    source = re.sub(r"\\\r?\n", "", source)
    source = re.sub(r"/\*.*?\*/|//[^\r\n]*", lambda m: " " + "\n" * m.group().count("\n"), source, flags=re.S)
    if re.search(r"^\s*#\s*(?:include|import)\b", source, flags=re.M):
        return "GLSL external source requires resolved storage addressing evidence"
    for match in re.finditer(r"^\s*#\s*extension\s+([^\r\n]+)", source, flags=re.M):
        directive = match.group(1).strip()
        parts = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(require|enable|warn|disable)\s*", directive)
        if not parts:
            return "GLSL storage extension directive is unresolved"
        extension, behavior = parts.groups()
        if behavior == "disable":
            continue
        # NV_shader_buffer_store has no independent name string: the Khronos
        # extension specification makes it implicit in GL_NV_gpu_shader5.
        if (extension in ("all", "GL_NV_gpu_shader5") or any(word in extension.lower() for word in (
                "bindless", "buffer_reference", "shader_buffer_load", "shader_buffer_store"))):
            return "GLSL device-address or bindless storage needs resolved memory ranges"
    # Native GL reflection exposes atomic counters in readWriteResources and
    # resolves their separate binding namespace through the descriptor table.
    return ""


def unbound_storage_reason(raw):
    """Identify native address/heap access which bound-resource readback cannot cover."""
    if len(raw) >= 20 and len(raw) % 4 == 0 and raw[:4] == b"\x03\x02\x23\x07":
        words = struct.unpack("<%dI" % (len(raw) // 4), raw)
        offset = 5
        while offset < len(words):
            size, opcode = words[offset] >> 16, words[offset] & 0xffff
            if size < 1 or offset + size > len(words):
                return "malformed native storage instructions"
            args = words[offset + 1:offset + size]
            if ((opcode == 17 and args and args[0] in (5347, 5390))
                    or (opcode == 14 and args and args[0] == 5348)
                    or (opcode == 32 and len(args) >= 2 and args[1] == 5349)):
                return "device-address or bindless-handle storage needs resolved memory ranges"
            offset += size
    elif len(raw) >= 32 and raw[:4] == b"DXBC":
        count = struct.unpack_from("<I", raw, 28)[0]
        if count > (len(raw) - 32) // 4:
            return "invalid native container storage metadata"
        for index in range(count):
            offset = struct.unpack_from("<I", raw, 32 + 4 * index)[0]
            if offset + 8 > len(raw):
                return "invalid native container storage metadata"
            size = struct.unpack_from("<I", raw, offset + 4)[0]
            if offset + 8 + size > len(raw):
                return "invalid native container storage metadata"
            if raw[offset:offset + 4] == b"SFI0" and size >= 8:
                if struct.unpack_from("<Q", raw, offset + 8)[0] & 0x2000000:
                    return "direct descriptor-heap access needs complete heap coverage"
    else:
        return "native storage addressing proof requires SPIR-V or DXBC/DXIL bytecode"
    return ""


def spirv_readonly_bindings(raw):
    """Return binding keys proven NonWritable by the exact SPIR-V module.

Unsupported/malformed decoration forms return no proof. This is deliberately
independent of decompiled text, names, comments, or the source editor.
"""
    if (len(raw) < 20 or len(raw) % 4 or len(raw) > 16 * 1024 * 1024
            or raw[:4] != b"\x03\x02\x23\x07"):
        return set()
    words = struct.unpack("<%dI" % (len(raw) // 4), raw)
    decorations, member_decorations, types, variables = {}, {}, {}, []
    offset = 5
    while offset < len(words):
        size, opcode = words[offset] >> 16, words[offset] & 0xffff
        if size < 1 or offset + size > len(words):
            return set()
        args = words[offset + 1:offset + size]
        if opcode in (73, 74, 75, 332):  # grouped/ID decorations need another decoder
            return set()
        if opcode == 71:
            if len(args) < 2:
                return set()
            key = (args[0], args[1])
            if key in decorations and decorations[key] != args[2:]:
                return set()
            decorations[key] = args[2:]
        elif opcode == 72:
            if len(args) < 3:
                return set()
            key = (args[0], args[1], args[2])
            if key in member_decorations and member_decorations[key] != args[3:]:
                return set()
            member_decorations[key] = args[3:]
        elif opcode in (28, 29, 30, 32):
            if not args or args[0] in types:
                return set()
            types[args[0]] = (opcode, args[1:])
        elif opcode == 59:
            if len(args) < 3:
                return set()
            variables.append(args[:3])
        offset += size

    def readonly_type(type_id, visiting):
        if (type_id, 24) in decorations:
            return decorations[(type_id, 24)] == ()
        if type_id in visiting or len(visiting) >= 32:
            return False
        opcode, args = types.get(type_id, (0, ()))
        visiting = visiting | {type_id}
        if opcode in (28, 29) and args:
            return readonly_type(args[0], visiting)
        if opcode == 30 and args:
            return all(member_decorations.get((type_id, index, 24)) == ()
                       or readonly_type(child, visiting)
                       for index, child in enumerate(args))
        return False

    bindings = {}
    for pointer_id, variable_id, storage in variables:
        if storage not in (0, 2, 12):  # UniformConstant, Uniform, StorageBuffer
            continue
        binding = decorations.get((variable_id, 33))
        descriptor_set = decorations.get((variable_id, 34), (0,))
        if binding is None or len(binding) != 1 or len(descriptor_set) != 1:
            continue
        opcode, args = types.get(pointer_id, (0, ()))
        valid_pointer = opcode == 32 and len(args) == 2 and args[0] == storage
        proven = valid_pointer and (decorations.get((variable_id, 24)) == ()
                                    or readonly_type(args[1], set()))
        key = (descriptor_set[0], binding[0])
        bindings[key] = bindings.get(key, True) and proven
    return {key for key, proven in bindings.items() if proven}


def readonly_storage_proof(bridge, reflection):
    resources = list(getattr(reflection, "readWriteResources", []) or [])
    try:
        raw = bridge._shader_raw_bytes(reflection)
        is_glsl = str(getattr(reflection, "encoding", "")).lower().replace("shaderencoding.", "") == "glsl"
        uncovered = glsl_unbound_storage_reason(raw) if is_glsl else unbound_storage_reason(raw)
        if uncovered:
            return {"complete": False, "bindingCount": len(resources),
                    "proof": "uncovered-addressing", "uncovered": uncovered}
        if not resources:
            return {"complete": True, "bindingCount": 0, "proof": "no-storage-bindings"}
        if is_glsl:
            return {"complete": False, "bindingCount": len(resources), "proof": "glsl-bound-storage"}
        proven = spirv_readonly_bindings(raw)
        keys = {(int(resource.fixedBindSetOrSpace), int(resource.fixedBindNumber))
                for resource in resources}
    except Exception:
        return {"complete": False, "bindingCount": len(resources), "proof": "unresolved",
                "uncovered": "native storage instructions unavailable"}
    return {"complete": bool(keys) and keys.issubset(proven),
            "bindingCount": len(keys), "proof": "spirv-nonwritable",
            "bindings": [{"space": space, "bind": bind, "readOnly": (space, bind) in proven}
                         for space, bind in sorted(keys)]}


def storage_outputs(bridge, ctrl, stage, output_limit, total_limit, reflected_resources):
    """Read all bound writable-descriptor storage, including counter buffers.

No sampling, cropping, alpha conversion or float tolerance is used here. Full
resource bytes cover aliases and descriptor views; each replay leg must expose
the same resources/subresources. Large fixtures fail before allocating them.
"""
    method = getattr(ctrl.GetPipelineState(), "GetReadWriteResources", None)
    if not callable(method):
        raise ValueError("storage comparison requires native bound-resource inspection")
    try:
        bound = method(stage, False)
    except TypeError:
        bound = method(stage)
    # Access feedback can omit descriptor-array elements, and it is not refreshed
    # by the lightweight full-replay analysis path. Admit only complete static
    # single-descriptor bindings until full heap/array enumeration is available.
    if not reflected_resources or any(int(getattr(r, "bindArraySize", 0)) != 1
                                      for r in reflected_resources):
        raise ValueError("storage descriptor arrays need complete binding-range coverage")
    resources = {}
    covered = set()
    for item in bound or []:
        access = getattr(item, "access", None)
        index = int(getattr(access, "index", -1))
        element = int(getattr(access, "arrayElement", -1))
        if index < 0 or index >= len(reflected_resources) or element != 0:
            raise ValueError("storage access is outside the verified static binding table")
        descriptor = getattr(item, "descriptor", item)
        if not bridge._resource_id_int(getattr(descriptor, "resource", None)):
            raise ValueError("storage binding has no resolved resource")
        if (int(getattr(descriptor, "flags", 0)) & 0x6
                and not bridge._resource_id_int(getattr(descriptor, "secondary", None))):
            raise ValueError("storage append/counter descriptor has no observable counter resource")
        covered.add(index)
        # Descriptor.secondary is the native counter resource. Reading that
        # resource refreshes hidden D3D11 counters as well; the cached
        # bufferStructCount value must not be used for an analysis replay.
        for field in ("resource", "secondary"):
            resource = getattr(descriptor, field, None)
            rid = bridge._resource_id_int(resource)
            if rid:
                resources[rid] = resource
    if covered != set(range(len(reflected_resources))):
        raise ValueError("storage binding table is incomplete; missing descriptor readback coverage")
    if not resources:
        raise ValueError("storage comparison has no resolved bound resources")
    buffers = {bridge._resource_id_int(value.resourceId): value for value in ctrl.GetBuffers()}
    textures = {bridge._resource_id_int(value.resourceId): value for value in ctrl.GetTextures()}
    outputs, total = [], 0

    def append(descriptor, raw):
        nonlocal total
        total += len(raw)
        if not raw or len(raw) > output_limit or total > total_limit:
            raise ValueError("storage readback exceeds the bounded observation budget")
        outputs.append((descriptor, raw))

    for rid, resource in sorted(resources.items()):
        description = buffers.get(rid) or textures.get(rid)
        size = int(getattr(description, "length", getattr(description, "byteSize", 0)))
        if size <= 0 or size > output_limit or total + size > total_limit:
            raise ValueError("storage resource %d exceeds the bounded observation budget" % rid)
        if rid in buffers:
            raw = bytes(ctrl.GetBufferData(resource, 0, size))
            if len(raw) != size:
                raise ValueError("storage buffer readback is incomplete")
            append({"kind": "storage-buffer", "resourceId": rid, "byteSize": size}, raw)
        elif rid in textures:
            texture = textures[rid]
            mips = int(getattr(texture, "mips", 1))
            layers = int(getattr(texture, "arraysize", 1))
            samples = int(getattr(texture, "msSamp", 1))
            if min(mips, layers, samples) < 1 or mips * layers * samples > 1024:
                raise ValueError("storage image subresource range is unsupported")
            for mip in range(mips):
                for layer in range(layers):
                    for sample in range(samples):
                        raw = bytes(ctrl.GetTextureData(resource, bridge.rd.Subresource(mip, layer, sample)))
                        append({"kind": "storage-image", "resourceId": rid,
                                "mip": mip, "slice": layer, "sample": sample,
                                "byteSize": len(raw)}, raw)
        else:
            raise ValueError("storage descriptor resource %d cannot be read back" % rid)
    return outputs
