"""Project captured CBuffer display aliases into verified replacement reflection."""
from __future__ import annotations

from collections.abc import Mapping

from . import aliases

_BYTES = {"float": 4, "int": 4, "uint": 4, "bool": 4, "double": 8,
          "slong": 8, "ulong": 8, "half": 2, "short": 2, "ushort": 2}


class DisplayAliasRestoreCache:
    """Shared metadata snapshot; cached labels never authorise replay or compilation."""

    def __init__(self, project_root):
        from pathlib import Path
        self.project = Path(project_root).resolve()
        self.root = self.project / "shared/dictionaries/cbuffer-aliases"
        self.files = {}

    def _stamp(self, status):
        import hashlib
        import json
        from ..version import SERVICE_VERSION
        from .alias_field_storage import _path as field_page_path, page_descriptors, usage_page_descriptors
        original = (status.get("shader") or {}).get("rawSha256")
        candidate = (status.get("latestCompiled") or {}).get("rawSha256")
        scope = {original, candidate}
        relevant = []
        present = set()
        for path in sorted(self.root.glob("*.json")):
            if path.name.startswith("."):
                continue
            st = path.stat()
            signature = (st.st_mtime_ns, st.st_size)
            present.add(path)
            cached = self.files.get(path)
            if cached is None or cached[0] != signature:
                if not 0 < st.st_size <= 16 * 1024 * 1024:
                    raise ValueError("CBuffer display metadata exceeds its bounded snapshot")
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("CBuffer display metadata is not an object")
                cached = self.files[path] = (signature, value)
            value = cached[1]
            selected = {}
            for key in ("aliases", "blockAliases", "semanticViews"):
                nodes = value.get(key) or []
                selected[key] = [node for node in nodes if isinstance(node, dict)
                    and (node.get("shaderScope") or {}).get("shaderRawSha256") in scope]
            if not any(selected.values()):
                continue
            pages = []
            for view in selected["semanticViews"]:
                chunks = page_descriptors(view) if "fieldChunks" in view else []
                if isinstance(view.get("usageReview"), Mapping):
                    chunks += usage_page_descriptors(view["usageReview"])
                for page in chunks:
                    target = field_page_path(self.root, page["sha256"])
                    stat = target.stat()
                    pages.append((page["sha256"], stat.st_mtime_ns, stat.st_size))
            relevant.append((path.name, value.get("block"), value.get("members"), selected, pages))
        self.files = {path: entry for path, entry in self.files.items() if path in present}
        key = {"version": SERVICE_VERSION, "project": str(self.project), "original": original,
               "candidate": candidate, "stage": status.get("stage"),
               "entryPoint": status.get("entryPoint"), "source": status.get("source"),
               "dependencies": (status.get("latestCompiled") or {}).get("sourceDependencies"),
               "aliases": relevant}
        return hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _path(workspace):
        from pathlib import Path
        return Path(workspace) / "state/display-alias-restore-cache.json"

    def read(self, workspace, status):
        import json
        try:
            path = self._path(workspace)
            if path.stat().st_size > 65536:
                return None
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record.get("schema") == "bread-display-alias-restore-cache/v1"
                    and record.get("stamp") == self._stamp(status)
                    and isinstance(record.get("result"), dict)
                    and record["result"].get("status") in ("restored", "current", "partial")):
                previous = record["result"]
                return {**previous, "status": "partial" if previous["status"] == "partial" else "current",
                        "writtenRows": 0, "writtenBlocks": 0, "metadataCacheHit": True}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return None

    def remember(self, workspace, status, result):
        import json
        import os
        import uuid
        try:
            record = {"schema": "bread-display-alias-restore-cache/v1",
                      "stamp": self._stamp(status), "result": result}
            path = self._path(workspace)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".pending-" + uuid.uuid4().hex)
            try:
                temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            # A dispensable metadata cache cannot turn a successful Shader restore into failure.
            pass


def current_display_identity(host, workspace, status, capture_session_id):
    """One read-only native owner/reflection observation, without ABI/catalog work."""
    ipc = getattr(host, "ipc", None)
    if ipc is None or not callable(getattr(ipc, "call", None)):
        # Injectable legacy adapter, also used by offline restore integrations.
        return host._shader_organization_cbuffer_alias_identity(str(workspace), status=status)
    shader = status.get("shader") or {}
    latest = status.get("latestCompiled") or {}
    params = {"expected_capture_session_id": capture_session_id,
              "expected_original_resource_id": shader.get("resourceId"),
              "expected_original_sha256": shader.get("rawSha256"),
              "expected_candidate_sha256": latest.get("rawSha256"),
              "stage": status.get("stage"), "entry_point": status.get("entryPoint", "")}
    identity = ipc.call("get_shader_cbuffer_alias_identity", params, timeout=30.0)
    if (not isinstance(identity, Mapping) or identity.get("replayMutation") is not False
            or identity.get("physicalAbiValidated") is not False
            or identity.get("replayRead") is not True
            or str(identity.get("shaderResourceId")) != str(shader.get("resourceId"))):
        raise RuntimeError("CBuffer display identity returned invalid read-only ownership evidence")
    return identity


def _portable_name_origins(host, status):
    """Follow verified project source lineage, never a recycled RDC ResourceId."""
    receipt = (status.get("source") or {}).get("projectSource")
    baseline = status.get("baseline") or {}
    original = (status.get("shader") or {}).get("rawSha256")
    if (not isinstance(receipt, Mapping)
            or receipt.get("schema") != "replaylab-verified-project-shader-source/v1"
            or receipt.get("originalRawSha256") != original
            or not baseline.get("codeSha256") or not baseline.get("abiSha256")
            or receipt.get("baselineAbiSha256") != baseline["abiSha256"]):
        return []
    replay = host._active_project_service().replay_service()
    manifest = replay._load()
    if manifest.get("projectId") != receipt.get("projectId"):
        return []
    knowledge = replay._workspace_knowledge_from_manifest(manifest, receipt.get("knowledgeId"))
    if (knowledge.get("workspaceId") != receipt.get("originWorkspaceId")
            or knowledge.get("sourceSha256") != receipt.get("sourceSha256")
            or knowledge.get("stage") != status.get("stage")
            or knowledge.get("entryPoint", "") != status.get("entryPoint", "")
            or knowledge.get("targetProfile") != status.get("targetProfile")
            or knowledge.get("codeSha256") != baseline["codeSha256"]
            or knowledge.get("baselineAbiSha256") != baseline["abiSha256"]):
        return []
    # Program bytes (without container metadata), physical ABI and profile are
    # all equal. The destination's actual typed blocks are still checked below.
    origin = str(knowledge.get("rawSha256") or "")
    return [origin] if aliases._SHA256_RE.fullmatch(origin) and origin != original else []


def _leaf_signature(leaf: Mapping) -> tuple | None:
    offset, size = leaf.get("offset"), leaf.get("byteSize")
    scalar = str(leaf.get("baseType") or "").lower().split(".")[-1]
    scalar = {"sint": "int"}.get(scalar, scalar)
    width = leaf.get("componentByteSize", _BYTES.get(scalar))
    if (type(offset) is not int or type(size) is not int or offset < 0 or size <= 0
            or scalar not in _BYTES or width != _BYTES[scalar]
            or leaf.get("semanticMappingAvailable") is False):
        return None
    components = leaf.get("componentOffsets", list(range(0, size, width)))
    if (not isinstance(components, list) or not components
            or any(type(value) is not int or value < 0 or value % width
                   or value + width > size for value in components)
            or len(set(components)) != len(components)):
        return None
    return offset, size, scalar, width, tuple(components)


def _block_signature(block: Mapping) -> tuple | None:
    if (block.get("memberMappingComplete", True) is not True
            or block.get("memberMappingIssueCount", 0) or block.get("memberMappingIssues")):
        return None
    size = block.get("byteSize")
    if type(size) is not int or size <= 0:
        return None
    components = {}
    for leaf in block.get("leaves") or []:
        signature = _leaf_signature(leaf)
        if signature is None:
            return None
        offset, unused_span, scalar, width, relative = signature
        for component in relative:
            start = offset + component
            if start + width > size or any(byte in components for byte in range(start, start + width)):
                return None
            for byte in range(start, start + width):
                components[byte] = (start, scalar, width)
    return (size, tuple(sorted(components.items()))) if components else None


def append_display_alias_mirrors(identity: Mapping, publications: list[dict]) -> list[dict]:
    """Mirror labels only; each draw still owns its bound ResourceId/range/values.

    A renamed or regrouped replacement layout must not inherit a source path or
    a label for a different type just because it shares one live Shader ID.
    Ordinary aliases name whole reflected leaves; split/grouped components use
    the separate semantic-field projection instead.
    """
    original = str(identity.get("rawSha256") or "")
    active = str(identity.get("activeDisplayShaderRawSha256") or "")
    if not active or active == original:
        return []
    skipped = []
    for publication in list(publications):
        if ((publication.get("shaderScope") or {}).get("shaderRawSha256") != original
                or not (publication.get("entries") or publication.get("blockAlias"))):
            continue
        key = (publication["bind"], publication.get("space", 0))
        source = [block for block in identity.get("blocks", [])
                  if (block.get("bind"), block.get("space", 0)) == key]
        targets = [block for block in identity.get("activeDisplayBlocks", [])
                   if (block.get("bind"), block.get("space", 0)) == key]
        if len(source) != 1 or len(targets) != 1:
            skipped.append({"bind": key[0], "space": key[1], "reason": "no-unique-active-block"})
            continue
        source_block, target_block = source[0], targets[0]
        target_leaves = target_block.get("leaves") or []
        source_by_identity, target_by_signature = {}, {}
        for leaf in source_block.get("leaves") or []:
            source_by_identity.setdefault((leaf.get("path"), leaf.get("offset")), []).append(leaf)
        for leaf in target_leaves:
            signature = _leaf_signature(leaf)
            if signature is not None:
                target_by_signature.setdefault(signature, []).append(leaf)
        entries = []
        for entry in publication.get("entries") or []:
            sources = source_by_identity.get((entry.get("path"), entry.get("offset")), [])
            signature = _leaf_signature(sources[0]) if len(sources) == 1 else None
            matches = target_by_signature.get(signature, [])
            if (len(matches) != 1 or not matches[0].get("path")
                    or target_block.get("memberMappingComplete", True) is not True):
                skipped.append({"bind": key[0], "space": key[1], "path": entry.get("path"),
                                "reason": "active-leaf-physical-shape-mismatch"})
                continue
            entries.append({"path": matches[0]["path"], "offset": matches[0]["offset"], "alias": entry["alias"]})
        block_alias = publication.get("blockAlias")
        if block_alias and (_block_signature(source_block) is None
                            or _block_signature(source_block) != _block_signature(target_block)):
            skipped.append({"bind": key[0], "space": key[1], "reason": "active-block-physical-layout-mismatch"})
            block_alias = None
        if not entries and not block_alias:
            continue
        members = [{"name": member.get("name"), "offset": member.get("offset")}
                   for member in target_block.get("members", [])]
        publications.append({"bind": key[0], "space": key[1], "block": target_block.get("block", ""),
            "members": members, "entries": entries, "blockAlias": block_alias, "displayMirror": True,
            "semanticBindingBasis": "verified-active-reflection",
            "shaderScope": {"shaderRawSha256": active,
                "shaderResourceId": str(identity.get("shaderResourceId") or ""),
                "stage": identity.get("stage", ""), "bind": key[0], "space": key[1],
                "layoutSha256": aliases.layout_sha256(target_block.get("block", ""), members)}})
    return skipped


def _restored_semantic_mirrors(identity: Mapping, status: Mapping, name_origins=None) -> tuple[list[dict], list[dict]]:
    """Keep parameter names across code edits; usage claims need unchanged source.

    Names describe the captured data's typed byte ranges. Editing calculations
    does not erase that knowledge. Preserve the naming source/evidence, rather
    than claiming it was reviewed against the new source. In contrast, an old
    unused-component review cannot describe a changed program.
    """
    import re
    from .cbuffer_semantics import normalize_fields
    from .semantic_usage import _typed_lanes, clean_usage_review

    original = str(identity.get("rawSha256") or "")
    candidate = str(identity.get("activeDisplayShaderRawSha256") or "")
    source_sha = str((status.get("source") or {}).get("currentSha256") or "")
    latest = status.get("latestCompiled") or {}
    if (not re.fullmatch(r"[0-9a-f]{64}", source_sha)
            or latest.get("sourceSha256") != source_sha or candidate != latest.get("rawSha256")):
        return [], []
    publications, skipped = [], []
    origins = None
    for source in identity.get("blocks") or []:
        key = (source.get("bind"), source.get("space", 0))
        source_members = [{"name": m.get("name"), "offset": m.get("offset")} for m in source.get("members", [])]
        source_layout = aliases.layout_sha256(source.get("block", ""), source_members)
        scope = {"shaderRawSha256": original, "stage": identity["stage"], "bind": key[0],
                 "space": key[1], "layoutSha256": source_layout}
        view = aliases.read_semantic_view(source_layout, shader_scope=scope)
        if view is None and callable(name_origins):
            if origins is None:
                origins = name_origins()
            inherited = [aliases.read_semantic_view(source_layout,
                         shader_scope={**scope, "shaderRawSha256": origin}) for origin in origins]
            inherited = [candidate for candidate in inherited if candidate is not None]
            # A historical knowledge link can supply exactly one authoritative
            # origin; conflicting captures never overwrite each other's names.
            view = inherited[0] if len(inherited) == 1 else None
        if view is None or view.get("source") != "ai":
            continue
        targets = [block for block in identity.get("activeDisplayBlocks", [])
                   if (block.get("bind"), block.get("space", 0)) == key]
        sources = [block for block in identity.get("blocks", [])
                   if (block.get("bind"), block.get("space", 0)) == key]
        if len(sources) != 1 or len(targets) != 1:
            skipped.append({"bind": key[0], "space": key[1], "reason": "semantic-active-block-not-unique"})
            continue
        target = targets[0]
        members = [{"name": m.get("name"), "offset": m.get("offset")} for m in target.get("members", [])]
        layout = aliases.layout_sha256(target.get("block", ""), members)
        target_scope = {**scope, "shaderRawSha256": candidate, "layoutSha256": layout,
                        "shaderResourceId": str(identity.get("shaderResourceId") or "")}
        # Never replace an existing user or newer candidate view with a source
        # view merely because its reflected fields are physically compatible.
        if aliases.read_semantic_view(layout, shader_scope=target_scope) is not None:
            continue
        # Main-source equality alone cannot establish unchanged include bytes;
        # legacy views do not carry a compiled include closure digest.
        dependencies = latest.get("sourceDependencies") or {}
        same_usage_source = (view.get("sourceSha256") == source_sha and
                             len(dependencies.get("documents") or []) <= 1)
        try:
            source_shape, target_shape = _block_signature(source), _block_signature(target)
            source_bytes = dict(source_shape[1]) if source_shape is not None else {}
            if (source_shape is None or target_shape is None or target_shape[0] > source_shape[0]
                    or any(source_bytes.get(offset) != lane for offset, lane in target_shape[1])):
                raise ValueError("semantic-physical-layout-changed")
            # Compilers may omit unused members (and shrink the reflected block)
            # without moving any surviving lane. Only that typed subset is safe;
            # new bytes, reinterpreted types and ambiguous mappings remain errors.
            target_bytes = dict(target_shape[1])
            # Logical matrix component order matters even when the occupied
            # bytes and scalar types happen to be identical after recompiling.
            source_matrices = {_leaf_signature(leaf) for leaf in source.get("leaves", []) if "componentOffsets" in leaf}
            target_matrices = {_leaf_signature(leaf) for leaf in target.get("leaves", []) if "componentOffsets" in leaf}
            if (not target_matrices.issubset(source_matrices) or any(
                    offset + component in target_bytes
                    for offset, span, scalar, width, relative in source_matrices - target_matrices
                    for component in relative)):
                raise ValueError("semantic-matrix-packing-changed")
            fields = view["fields"]
            if fields:
                requested = [{**field, "bind": key[0], "space": key[1]} for field in fields]
                normalize_fields({"blocks": [source]}, requested)
            original_lanes, active_lanes = _typed_lanes(source), _typed_lanes(target)
            for field in fields:
                scalar = re.sub(r"[234]$", "", field["type"])
                if any(original_lanes.get(component["offset"]) != scalar for component in field["components"]):
                    raise ValueError("semantic-component-type-changed")
            retained = [field for field in fields if all(
                active_lanes.get(component["offset"]) == re.sub(r"[234]$", "", field["type"])
                for component in field["components"])]
            if len(retained) != len(fields):
                skipped.append({"bind": key[0], "space": key[1],
                    "reason": "semantic-fields-not-fully-reflected", "fieldCount": len(fields) - len(retained)})
            fields = retained
            if fields:
                normalize_fields({"blocks": [target]}, [{**field, "bind": key[0], "space": key[1]}
                                                        for field in fields])
            usage = (clean_usage_review(view["usageReview"])
                     if same_usage_source and source_shape == target_shape and "usageReview" in view else None)
            if usage is not None and any(original_lanes.get(component["offset"]) != component["type"]
                    or active_lanes.get(component["offset"]) != component["type"] for component in usage["unusedComponents"]):
                raise ValueError("semantic-unused-component-type-changed")
        except ValueError as error:
            skipped.append({"bind": key[0], "space": key[1], "reason": str(error)[:200]})
            continue
        if not fields and usage is None:
            # A wholly-unused old view contains no reusable names. Do not
            # create an empty candidate view that would hide later knowledge.
            continue
        publications.append({"bind": key[0], "space": key[1], "block": target.get("block", ""),
            "members": members, "entries": [], "blockAlias": None, "displayMirror": True,
            "shaderScope": target_scope, "semanticFields": fields,
            "semanticSourceSha256": str(view.get("sourceSha256") or ""),
            "semanticCapturedShaderSha256": original, "semanticBindingBasis": "verified-active-reflection",
            **({"semanticUsageReview": usage} if usage is not None else {})})
    return publications, skipped


def restore_display_alias_mirrors(host, workspace, status, capture_session_id, project_root, *, active_project=None, metadata_cache=None):
    """Backfill old AI names after verified project restore, without replaying values.

    Older versions published ordinary names only for captured reflection. Read
    those exact content scopes, then project missing names into the currently
    owned replacement. Existing candidate/user labels always take precedence.
    """
    import hashlib
    import json
    from pathlib import Path
    import uuid
    from contextlib import nullcontext
    from .cbuffer_alias_publication import publish_transaction

    loader = getattr(host, "_shader_organization_cbuffer_alias_identity", None)
    if not callable(loader):
        return {"status": "unavailable", "reason": "no-verified-reflection-route"}
    original = str((status.get("shader") or {}).get("rawSha256") or "")
    candidate = str((status.get("latestCompiled") or {}).get("rawSha256") or "")
    if not original or not candidate or candidate == original:
        return {"status": "not-required", "writtenRows": 0, "writtenBlocks": 0}
    cached = metadata_cache.read(workspace, status) if metadata_cache is not None else None
    if cached is not None:
        return cached
    active_service = host._active_project_service()
    snapshot_factory = getattr(active_service, "snapshot_active_project", None)
    fast_selection = (isinstance(active_project, Mapping) and callable(snapshot_factory)
                      and callable(getattr(active_service, "commit_active_project_snapshot", None)))
    active = dict(active_project) if fast_selection else active_service.status()
    if Path(active["projectRoot"]).resolve() != Path(project_root).resolve():
        raise RuntimeError("CBuffer display backfill changed its active project")
    selection = (snapshot_factory(active["workspaceId"], active["projectRoot"]) if fast_selection
                 else active_service.pin_active_project(active["workspaceId"], active["projectRoot"]))
    with selection as project_snapshot:
        def identity_now():
            # The editor batch caches transport identity before any catalog is
            # necessarily published. This loader promotes the verified catalog,
            # so its subsequent identity checks must observe that new catalog,
            # not the batch's earlier (possibly blank) captureId. Retain the
            # pinned instance/session and validate its owner before bypassing
            # only this request-local read cache; restore it on every exit.
            editor_context = getattr(host, "_RESTORED_SHADER_EDITOR_CONTEXT", None)
            admitted = editor_context.get() if editor_context is not None else None
            token = None
            if admitted is not None:
                admitted[1]()
                token = editor_context.set(None)
            try:
                identity = current_display_identity(host, workspace, status, capture_session_id)
            finally:
                if token is not None:
                    editor_context.reset(token)
            same_capture = identity.get("captureId") == (status.get("capture") or {}).get("captureId")
            session_bound = (not identity.get("captureId") and
                             identity.get("captureIdentityBasis") == "session-original-bytes")
            if (identity.get("captureSessionId") != capture_session_id
                    or not (same_capture or session_bound)
                    or identity.get("rawSha256") != original
                    or identity.get("activeReplacementShaderRawSha256") != candidate
                    or identity.get("stage") != status.get("stage")
                    or identity.get("entryPoint", "") != status.get("entryPoint", "")):
                raise RuntimeError("CBuffer display backfill no longer owns the applied Shader")
            selected = aliases.active_project_root()
            if selected is None or selected.resolve() != Path(project_root).resolve():
                raise RuntimeError("CBuffer display backfill changed its alias store")
            return identity

        identity = identity_now()
        if identity.get("activeDisplayEphemeral") is True:
            # Native debug owns its transient view. Never persist a temporary
            # instrumented program as a new project naming revision.
            return {"status": "not-required", "writtenRows": 0, "writtenBlocks": 0,
                    "reason": "transient-visual-debug"}
        if identity.get("activeDisplayShaderRawSha256") != candidate:
            return {"status": "not-required", "writtenRows": 0, "writtenBlocks": 0}
        publications = []
        for block in identity.get("blocks") or []:
            members = [{"name": m.get("name"), "offset": m.get("offset")} for m in block.get("members", [])]
            layout = aliases.layout_sha256(block.get("block", ""), members)
            scope = {"shaderRawSha256": original, "stage": identity["stage"],
                     "bind": block["bind"], "space": block.get("space", 0), "layoutSha256": layout}
            rows = [item for item in aliases.read_aliases(layout, shader_scope=scope)
                    if item.get("source") == "ai" and isinstance(item.get("shaderScope"), Mapping)]
            name = aliases.read_block_alias(layout, shader_scope=scope)
            name = name["alias"] if name and name.get("source") == "ai" and name.get("shaderScope") else None
            if rows or name:
                publications.append({"bind": scope["bind"], "space": scope["space"],
                    "block": block.get("block", ""), "members": members,
                    "entries": rows, "blockAlias": name, "shaderScope": scope})
        skipped = append_display_alias_mirrors(identity, publications)
        semantic_mirrors, semantic_skipped = _restored_semantic_mirrors(
            identity, status, lambda: _portable_name_origins(host, status))
        publications.extend(semantic_mirrors)
        skipped.extend(semantic_skipped)
        pending = []
        for item in publications:
            if not item.get("displayMirror"):
                continue
            scope = item["shaderScope"]
            current = aliases.read_aliases(scope["layoutSha256"], shader_scope=scope)
            occupied = {row["path"] for row in current}
            item["entries"] = [row for row in item["entries"] if row["path"] not in occupied]
            if aliases.read_block_alias(scope["layoutSha256"], shader_scope=scope):
                item["blockAlias"] = None
            if item["entries"] or item["blockAlias"] or "semanticFields" in item or "semanticUsageReview" in item:
                pending.append(item)
        if not pending:
            result = {"status": "partial" if skipped else "current", "writtenRows": 0,
                      "writtenBlocks": 0, "skipped": skipped[:64]}
            if metadata_cache is not None:
                metadata_cache.remember(workspace, status, result)
            return result
        request = {"original": original, "candidate": candidate, "publications": pending}
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf8")).hexdigest()
        def before_apply():
            # Do not wait on replay/UI while the alias-file transaction is
            # locked. These labels are scoped to immutable Shader bytes and
            # exact typed layouts; a later owner switch cannot change them.
            if fast_selection:
                active_service.assert_active_project_snapshot(project_snapshot)
            else:
                host._require_same_active_project(active)
            selected = aliases.active_project_root()
            if selected is None or selected.resolve() != Path(project_root).resolve():
                raise RuntimeError("CBuffer display backfill changed its alias store")
            # Another naming transaction may have won between planning and
            # this locked commit. Keep that view, including a newer AI review.
            store = aliases.store_root()
            for publication in pending:
                if "semanticFields" not in publication:
                    continue
                scope = publication["shaderScope"]
                if aliases._read_semantic_views(
                        aliases.layout_alias_path(scope["layoutSha256"], store),
                        fields_root=store, expand_fields=False, shader_scope=scope):
                    raise RuntimeError("current candidate semantic view changed during display backfill")
        def result(receipts):
            return {"status": "partial" if skipped else "restored",
                    "writtenRows": sum(len(row.get("written", [])) for row in receipts),
                    "writtenBlocks": sum(row.get("blockAliasWritten") is True for row in receipts),
                    "skipped": skipped[:64]}
        commit = (active_service.commit_active_project_snapshot(project_snapshot)
                  if fast_selection else nullcontext())
        with commit:
            receipt = publish_transaction(workspace_root=Path(workspace), operation_id="display-mirror-" + uuid.uuid4().hex,
                request_sha256=digest, caller_request_sha256=digest, publications=pending,
                build_result=result, before_apply=before_apply)
        result = {key: receipt[key] for key in ("status", "writtenRows", "writtenBlocks", "skipped")}
        if metadata_cache is not None:
            metadata_cache.remember(workspace, status, result)
        return result
