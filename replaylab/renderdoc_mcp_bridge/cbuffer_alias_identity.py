"""Read original reflection identity for the existing external alias store.

This is display identity, not the normalized physical ABI projection. Keeping
the original top-level names is necessary to match qrenderdoc's layout key.

Leaves are the rows the constant-buffer panel can alias, keyed exactly the way
``BufferViewer::UI_AddFixedVariables`` keys them: an array element is
``name[e]`` at ``byteOffset + arrayByteStride * e``. Stripped DXIL can reflect
a block as ``unknown[N]`` placeholders; those rows are
what the user sees and types aliases on, so they are mapped whenever their
shape is physically sane (a real offset and size, inside the block, not
overlapping another row), even though they are not source annotations.
"""

_COMPONENT_BYTES = {"double": 8, "slong": 8, "ulong": 8, "half": 2, "short": 2, "ushort": 2}
MAX_ALIAS_LEAVES = 65536
MAX_ALIAS_FIELD_VISITS = 131072
MAX_MAPPING_ISSUES = 64


def _int(value, default=0):
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _element_byte_size(field):
    """Bytes of one array element (or of the whole field when it is not an array)."""

    elements = max(_int(field.get("elements", 1), 1), 1)
    total = _int(field.get("byteSize", 0))
    rows = max(_int(field.get("rows", 1), 1), 1)
    columns = max(_int(field.get("columns", 1), 1), 1)
    base = str(field.get("baseType", "") or "").lower()
    component = 4
    for token, size in _COMPONENT_BYTES.items():
        if token in base:
            component = size
            break
    matrix_stride = _int(field.get("matrixByteStride", 0))
    if rows > 1 and matrix_stride > 0:
        major, minor = (rows, columns) if field.get("rowMajor") else (columns, rows)
        return matrix_stride * (major - 1) + component * minor
    natural = rows * columns * component
    if elements > 1:
        stride = _int(field.get("arrayByteStride", 0))
        if stride > 0:
            return natural
        if total > 0:
            return total // elements
    return total if total > 0 else natural


def _leaves_for(fields, block_size, diagnostics=None):
    leaves = []
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    diagnostics.update(issues=[], issueCount=0, issuesTruncated=False)
    budget = {"visits": 0, "stopped": False}

    def omit(path, reason):
        diagnostics["issueCount"] += 1
        if len(diagnostics["issues"]) < MAX_MAPPING_ISSUES:
            diagnostics["issues"].append({"path": str(path)[:512], "reason": reason})
        else:
            diagnostics["issuesTruncated"] = True

    def spend(path):
        if budget["stopped"]:
            return False
        budget["visits"] += 1
        if budget["visits"] > MAX_ALIAS_FIELD_VISITS or len(leaves) >= MAX_ALIAS_LEAVES:
            omit(path, "mapping-expansion-limit")
            budget["stopped"] = True
            return False
        return True

    def collect(values, byte_shift=0, path_replacements=(), depth=0,
                lower=0, upper=0, parent_path=""):
        if depth > 32:
            omit(parent_path, "nested-layout-depth-limit")
            return
        for field in values:
            if not spend(parent_path):
                return
            if not isinstance(field, dict):
                omit(parent_path, "invalid-field")
                continue
            children = field.get("members") or []
            count = _int(field.get("elements", 1), -1)
            if count < 0:
                omit(field.get("path", parent_path), "invalid-array-count")
                continue
            elements = max(count, 1)
            path = str(field.get("path", "") or "")
            for original, replacement in path_replacements:
                if path == original or path.startswith(original + "."):
                    path = replacement + path[len(original):]
            if parent_path and not path.startswith(parent_path + "."):
                omit(path, "field-path-outside-parent")
                continue
            raw_base = _int(field.get("absoluteByteOffset", -1), -1)
            base = raw_base + byte_shift
            if children:
                if not isinstance(children, (list, tuple)):
                    omit(path, "invalid-members")
                    continue
                if elements > 1:
                    # Child absolute offsets describe element zero. Carry each
                    # outer array's stride and indexed path through nested arrays;
                    # never infer a missing stride from parameter order.
                    stride = _int(field.get("arrayByteStride", 0))
                    if not path or stride <= 0 or raw_base < 0:
                        omit(path, "missing-array-layout")
                        continue
                    # The final struct need not own trailing stride padding.
                    # Check each real member against its own element interval.
                    if (upper <= lower or base < lower or
                            base + stride * (elements - 1) >= upper):
                        omit(path, "array-outside-parent")
                        continue
                    for index in range(elements):
                        if budget["stopped"]:
                            return
                        element_base = base + stride * index
                        collect(children, byte_shift + stride * index,
                                path_replacements + ((path, "%s[%d]" % (path, index)),), depth + 1,
                                element_base, min(element_base + stride, upper),
                                "%s[%d]" % (path, index))
                else:
                    size = _int(field.get("byteSize", 0))
                    child_lower, child_upper = lower, upper
                    if raw_base >= 0:
                        child_lower = max(lower, base)
                        if size > 0:
                            child_upper = min(upper, base + size)
                    collect(children, byte_shift, path_replacements, depth + 1,
                            child_lower, child_upper, path)
                continue
            if raw_base < 0:
                omit(path, "missing-field-offset")
                continue
            size = _element_byte_size(field)
            if size <= 0:
                omit(path, "invalid-field-size")
                continue
            if not path:
                omit(parent_path, "missing-field-path")
                continue
            base_type = str(field.get("baseType", "") or "").lower().split(".")[-1]
            base_type = {"sint": "int"}.get(base_type, base_type)
            metadata = {}
            if base_type:
                metadata = {"baseType": base_type,
                            "componentByteSize": _COMPONENT_BYTES.get(base_type, 4)}
            rows = _int(field.get("rows", 1), 1)
            columns = _int(field.get("columns", 1), 1)
            if rows > 1:
                stride = _int(field.get("matrixByteStride", 0))
                row_major = field.get("rowMajor")
                minor = columns if row_major else rows
                if (metadata.get("componentByteSize") == 4 and type(row_major) is bool
                        and 1 < rows <= 4 and 1 <= columns <= 4
                        and stride >= minor * 4 and stride % 4 == 0):
                    # Keep the whole-matrix alias identity, but publish only
                    # real component positions for semantic regrouping. Stride
                    # padding is never a parameter, including matrix arrays.
                    metadata["componentOffsets"] = [
                        row * stride + column * 4 if row_major else column * stride + row * 4
                        for row in range(rows) for column in range(columns)]
                else:
                    metadata["semanticMappingAvailable"] = False
            if (_int(field.get("bitFieldOffset", 0)) != 0 or
                    _int(field.get("bitFieldSize", 0)) != 0):
                # Bitfields do not own a whole 32-bit lane.
                metadata["semanticMappingAvailable"] = False
            if elements <= 1:
                if upper <= lower or base < lower or base + size > upper:
                    omit(path, "field-outside-parent")
                    continue
                leaves.append(dict({"path": path, "offset": base, "byteSize": size}, **metadata))
                continue
            stride = _int(field.get("arrayByteStride", 0))
            if stride <= 0:
                omit(path, "missing-array-layout")
                continue
            if size > stride:
                omit(path, "array-stride-smaller-than-element")
                continue
            if (upper <= lower or base < lower or
                    base + stride * (elements - 1) + size > upper):
                omit(path, "array-outside-parent")
                continue
            for index in range(elements):
                if not spend(path):
                    return
                leaves.append(dict({"path": "%s[%d]" % (path, index),
                                    "offset": base + stride * index, "byteSize": size}, **metadata))

    collect(fields, upper=block_size)
    # Physically sane only: inside the block and not on top of another row.
    # A placeholder set that overlaps at offset 0 maps nothing, by design.
    sane, cluster, cluster_end = [], [], -1

    def finish_cluster():
        if len(cluster) == 1:
            sane.append(cluster[0])
        elif cluster:
            for leaf in cluster:
                omit(leaf["path"], "overlapping-field")

    for leaf in sorted(leaves, key=lambda item: item["offset"]):
        if leaf["offset"] >= cluster_end:
            finish_cluster()
            cluster = []
        cluster.append(leaf)
        cluster_end = max(cluster_end, leaf["offset"] + leaf["byteSize"])
    finish_cluster()
    return sane


def reflection_alias_blocks(reflection, fields_for_constants, annotations_complete):
    blocks = []
    for index, block in enumerate(list(getattr(reflection, "constantBlocks", []) or [])):
        variables = list(getattr(block, "variables", []) or [])
        members = [{"name": str(getattr(item, "name", "")),
                    "offset": int(getattr(item, "byteOffset", -1))}
                   for item in variables]
        fields = fields_for_constants(variables)
        byte_size = int(getattr(block, "byteSize", 0))
        complete = bool(fields and annotations_complete(fields, byte_size))
        diagnostics = {}
        leaves = _leaves_for(fields, byte_size, diagnostics) if fields else []
        blocks.append({
            "block": str(getattr(block, "name", "")),
            "bind": int(getattr(block, "fixedBindNumber", index)),
            "space": int(getattr(block, "fixedBindSetOrSpace", 0)),
            "byteSize": byte_size,
            "members": members,
            "leaves": leaves,
            "memberMappingAvailable": bool(leaves),
            "memberMappingComplete": bool(fields) and not diagnostics.get("issueCount", 0),
            "memberMappingIssues": diagnostics.get("issues", []),
            "memberMappingIssueCount": diagnostics.get("issueCount", 0),
            "memberMappingIssuesTruncated": diagnostics.get("issuesTruncated", False),
            # Source annotations (a debug build) versus placeholder rows the
            # panel shows for stripped bytecode; both are aliasable.
            "memberMappingKind": "annotated" if complete else ("placeholder" if leaves else "none"),
        })
    return blocks
