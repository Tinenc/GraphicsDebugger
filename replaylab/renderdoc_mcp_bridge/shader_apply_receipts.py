"""Local, content-bound proof of a successfully applied native Shader.

Python 3.6 compatible. Receipts contain no replay resource or process handles.
The private signing key stays outside project/capture data, so an imported
memo cannot declare itself validated. A new machine simply takes the full path.
"""

import ast
import hashlib
import hmac
import json
import os
import stat
import importlib.util


SCHEMA = "replaylab-validated-shader-restore/v1"
MAX_RECEIPT_BYTES = 256 * 1024
MAX_DXIL_EVIDENCE_BLOCKS = 256
MAX_DXIL_EVIDENCE_ACCESSES = 4096
PHYSICAL_VALIDATION_SCHEMA = "replaylab-shader-physical-validation-binding/v1"
PHYSICAL_ABI_RULES_VERSION = 1


class _BCryptFileHash:
    """Use Windows' accelerated SHA256 with the bundled Python 3.6 runtime."""

    def __init__(self):
        import ctypes
        self.ctypes = ctypes
        self.api = ctypes.WinDLL(os.path.join(
            os.environ.get("SystemRoot", "C:\\Windows"), "System32", "bcrypt.dll"))
        pointer, ulong = ctypes.c_void_p, ctypes.c_ulong
        signatures = {
            "BCryptOpenAlgorithmProvider": [ctypes.POINTER(pointer), ctypes.c_wchar_p,
                                             ctypes.c_wchar_p, ulong],
            "BCryptGetProperty": [pointer, ctypes.c_wchar_p, pointer, ulong,
                                  ctypes.POINTER(ulong), ulong],
            "BCryptCreateHash": [pointer, ctypes.POINTER(pointer), pointer, ulong,
                                 pointer, ulong, ulong],
            "BCryptHashData": [pointer, pointer, ulong, ulong],
            "BCryptFinishHash": [pointer, pointer, ulong, ulong],
            "BCryptDestroyHash": [pointer],
            "BCryptCloseAlgorithmProvider": [pointer, ulong],
        }
        for name, arguments in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, ctypes.c_long
        self.provider, self.handle = pointer(), pointer()
        try:
            self._check(self.api.BCryptOpenAlgorithmProvider(
                ctypes.byref(self.provider), "SHA256", None, 0))
            size, returned = ulong(), ulong()
            self._check(self.api.BCryptGetProperty(
                self.provider, "ObjectLength", ctypes.byref(size),
                ctypes.sizeof(size), ctypes.byref(returned), 0))
            if returned.value != ctypes.sizeof(size) or not 0 < size.value <= 1024 * 1024:
                raise OSError("Windows SHA256 provider returned invalid object storage")
            self.storage = ctypes.create_string_buffer(size.value)
            self._check(self.api.BCryptCreateHash(
                self.provider, ctypes.byref(self.handle), self.storage,
                size.value, None, 0, 0))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(status):
        if status < 0:
            raise OSError("Windows SHA256 failed: 0x%08x" % (status & 0xffffffff))

    def update(self, data):
        self._check(self.api.BCryptHashData(self.handle, data, len(data), 0))

    def hexdigest(self):
        output = self.ctypes.create_string_buffer(32)
        self._check(self.api.BCryptFinishHash(self.handle, output, 32, 0))
        return output.raw.hex()

    def close(self):
        if self.handle:
            self.api.BCryptDestroyHash(self.handle)
            self.handle = self.ctypes.c_void_p()
        if self.provider:
            self.api.BCryptCloseAlgorithmProvider(self.provider, 0)
            self.provider = self.ctypes.c_void_p()


def _file_hasher():
    if os.name == "nt":
        try:
            return _BCryptFileHash()
        except (ImportError, AttributeError, OSError):
            # Provider initialization can fail on an unsupported environment.
            # Choose the fallback before reading any bytes; mid-stream failures
            # must abort verification and never publish a partial digest.
            pass
    return hashlib.sha256()


def _stream_sha256(stream):
    digest = _file_hasher()
    try:
        while True:
            chunk = stream.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        close = getattr(digest, "close", None)
        if close is not None:
            close()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def restore_dxil_evidence(value, protocol):
    """Rehydrate only bounded plain evidence, after authenticating its receipt.

    The enclosing signature binds this evidence to the original native bytes,
    physical ABI and validation policy. No native handles or printer text are
    restored; the original bytes are checked again in the current capture.
    """
    def integer(number, maximum=1 << 30):
        if type(number) is not int or not 0 <= number <= maximum:
            raise ValueError("Shader restore DXIL evidence integer is invalid")
        return number

    if not isinstance(value, dict) or set(value) != {"accessSignature", "layoutEvidence"}:
        raise ValueError("Shader restore captured DXIL evidence is missing")
    signature, rows = value["accessSignature"], value["layoutEvidence"]
    if not isinstance(signature, dict) or not isinstance(rows, list):
        raise ValueError("Shader restore DXIL evidence is invalid")
    blocks = signature.get("blocks")
    if (not isinstance(blocks, list) or len(blocks) > MAX_DXIL_EVIDENCE_BLOCKS or
            len(rows) > MAX_DXIL_EVIDENCE_BLOCKS):
        raise ValueError("Shader restore DXIL evidence has too many blocks")
    source_sha256 = signature.get("sourceSha256", "")
    if (not isinstance(source_sha256, str) or len(source_sha256) != 64 or
            any(character not in "0123456789abcdef" for character in source_sha256)):
        raise ValueError("Shader restore DXIL evidence source hash is invalid")
    restored_blocks, access_count, seen = [], 0, set()
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("accesses"), list):
            raise ValueError("Shader restore DXIL access block is invalid")
        access_count += len(block["accesses"])
        if access_count > MAX_DXIL_EVIDENCE_ACCESSES:
            raise ValueError("Shader restore DXIL evidence has too many accesses")
        key = (integer(block["space"]), integer(block["bind"]))
        if key in seen:
            raise ValueError("Shader restore DXIL evidence has duplicate blocks")
        seen.add(key)
        accesses = []
        for item in block["accesses"]:
            if item["returnType"] not in ("i16", "f16", "i32", "f32", "i64", "f64"):
                raise ValueError("Shader restore DXIL access scalar is invalid")
            accesses.append(protocol.DxilCBufferAccess(
                integer(item["byteOffset"]), integer(item["byteWidth"], 8), item["returnType"]))
        restored_blocks.append(protocol.DxilCBufferAccessBlock(
            key[0], key[1], integer(block["bindArraySize"]), integer(block["byteSize"]),
            integer(block["accessEnvelope"]["byteOffset"]),
            integer(block["accessEnvelope"]["byteEnd"]), accesses))
    restored = protocol.DxilCBufferAccessSignature(
        source_sha256, integer(signature.get("sourceByteSize"), protocol.MAX_IR_BYTES), restored_blocks)
    if restored.to_dict() != signature:
        raise ValueError("Shader restore DXIL access signature does not match")
    layout = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"space", "bind", "evidence"}:
            raise ValueError("Shader restore DXIL layout row is invalid")
        key, marker = (integer(row["space"]), integer(row["bind"])), row["evidence"]
        if (key in layout or not isinstance(marker, dict) or
                set(marker) != {"kind", "typeAnnotation", "sourceSha256", "accessSignatureSha256"} or
                marker["kind"] != "dxc-dxil-buffer-definition" or
                marker["typeAnnotation"] not in ("absent", "unavailable") or
                marker["sourceSha256"] != restored.source_sha256 or
                marker["accessSignatureSha256"] != restored.signature_sha256):
            raise ValueError("Shader restore DXIL layout evidence does not match its source")
        layout[key] = dict(marker)
    return restored, layout


def capture_dxil_evidence(access, layout, protocol):
    if access is None or not isinstance(layout, dict):
        raise ValueError("Captured-original DXIL evidence is unavailable for Shader restore")
    value = {
        "accessSignature": access.to_dict(),
        "layoutEvidence": [
            {"space": key[0], "bind": key[1], "evidence": marker}
            for key, marker in sorted(layout.items())],
    }
    restore_dxil_evidence(value, protocol)
    if len(_canonical(value)) > MAX_RECEIPT_BYTES:
        raise ValueError("Captured-original DXIL evidence exceeds Shader restore proof limit")
    return value


def _regular(path):
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or
            getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("Shader restore proof path is not a regular file")
    return info


def validation_policy_sha256(paths):
    """Bind validation code and available native replay binaries, once at load."""
    records = []
    for path in paths:
        _regular(path)
        with open(path, "rb") as stream:
            digest = _stream_sha256(stream)
        records.append([os.path.basename(path), digest])
    return hashlib.sha256(_canonical(records)).hexdigest()


def _semantic_ast(value):
    """Stable code facts across embedded 3.6 and newer development runtimes."""
    if isinstance(value, list):
        return [_semantic_ast(item) for item in value]
    if not isinstance(value, ast.AST):
        return value
    kind = type(value).__name__
    if kind == "Constant":
        literal = value.value
    elif kind == "Str":
        literal = value.s
    elif kind == "Bytes":
        literal = value.s
    elif kind == "Num":
        literal = value.n
    elif kind == "NameConstant":
        literal = value.value
    elif kind == "Ellipsis":
        literal = Ellipsis
    else:
        literal = NotImplemented
    if literal is not NotImplemented:
        if isinstance(literal, bytes):
            return ["literal", "bytes", literal.hex()]
        if literal is Ellipsis:
            return ["literal", "ellipsis"]
        return ["literal", type(literal).__name__, literal]
    if type(value).__name__ == "Index":
        return _semantic_ast(value.value)
    fields = []
    for name, child in ast.iter_fields(value):
        if name in ("type_comment", "type_params", "type_ignores", "posonlyargs") and not child:
            continue
        if name == "body" and isinstance(child, list) and child:
            first = child[0]
            if (isinstance(value, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and isinstance(first, ast.Expr)
                    and (type(first.value).__name__ == "Str"
                         or (type(first.value).__name__ == "Constant"
                             and isinstance(first.value.value, str)))):
                child = child[1:]
        fields.append([name, _semantic_ast(child)])
    return [type(value).__name__, fields]


def _algorithm_code_facts(path, roots, terminal_functions=()):
    _regular(path)
    with open(path, "r", encoding="utf-8-sig") as stream:
        tree = ast.parse(stream.read(), filename=os.path.basename(path))
    definitions, constants = {}, {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.upper() == target.id:
                    constants[target.id] = node.value
    selected, pending = {}, list(definitions if roots is None else roots)
    terminals = set(terminal_functions)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        parts = name.split(".")
        node = definitions.get(parts[0])
        for part in parts[1:]:
            matches = [item for item in ast.walk(node) if isinstance(
                item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and item.name == part] if node is not None else []
            if len(matches) != 1:
                raise ValueError("Physical Shader validation code root is missing or ambiguous: %s" % name)
            node = matches[0]
        if node is None:
            raise ValueError("Physical Shader validation code root is missing: %s" % name)
        selected[name] = _semantic_ast(node)
        # Direct calls exclude an ordinary local variable whose name happens
        # to match a module function. Parameters/local bindings cannot become
        # callable policy dependencies merely by sharing a global name.
        local_names = {item.arg for item in ast.walk(node) if isinstance(item, ast.arg)}
        local_names.update(item.id for item in ast.walk(node)
                           if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store))
        global_names = {item for statement in ast.walk(node) if isinstance(statement, ast.Global)
                        for item in statement.names}
        local_names.difference_update(global_names)
        for item in ast.walk(node):
            if isinstance(item, ast.Name) and item.id in constants:
                selected["constant:" + item.id] = _semantic_ast(constants[item.id])
            if (name not in terminals and isinstance(item, ast.Call)
                    and isinstance(item.func, ast.Name) and item.func.id in definitions
                    and item.func.id not in local_names):
                pending.append(item.func.id)
    return [[name, selected[name]] for name in sorted(selected)]


def physical_validation_binding(code_sources, native_paths, abi_rules_version=PHYSICAL_ABI_RULES_VERSION):
    """Bind only physical validation algorithms and actual native binaries.

    Presentation/version/workspace routing remains provenance. Code sources
    declare semantic roots and infrastructure traversal boundaries; no file
    text, line numbers or comments accidentally become ABI policy.
    """
    if type(abi_rules_version) is not int or abi_rules_version <= 0:
        raise ValueError("Physical Shader ABI rules version is invalid")
    algorithms = []
    for role, path, roots, terminals in sorted(code_sources):
        facts = _algorithm_code_facts(path, roots, terminals)
        algorithms.append({"role": role, "codeSha256": hashlib.sha256(_canonical(facts)).hexdigest()})
    binaries = []
    for path in sorted(set(native_paths), key=lambda value: os.path.basename(value)):
        _regular(path)
        with open(path, "rb") as stream:
            binaries.append({"name": os.path.basename(path), "sha256": _stream_sha256(stream)})
    return {
        "schema": PHYSICAL_VALIDATION_SCHEMA, "schemaVersion": 1,
        "abiRulesVersion": abi_rules_version,
        "algorithms": algorithms, "nativeBinaries": binaries,
    }


def physical_validation_binding_sha256(binding):
    if (not isinstance(binding, dict)
            or set(binding) != {"schema", "schemaVersion", "abiRulesVersion", "algorithms", "nativeBinaries"}
            or binding.get("schema") != PHYSICAL_VALIDATION_SCHEMA
            or binding.get("schemaVersion") != 1
            or type(binding.get("abiRulesVersion")) is not int or binding["abiRulesVersion"] <= 0):
        raise ValueError("Physical Shader validation binding is invalid")
    for field, identity in (("algorithms", "role"), ("nativeBinaries", "name")):
        rows = binding[field]
        if not isinstance(rows, list) or len(rows) > 64 or (field == "algorithms" and not rows):
            raise ValueError("Physical Shader validation binding records are invalid")
        seen = set()
        for row in rows:
            digest_key = "codeSha256" if field == "algorithms" else "sha256"
            if (not isinstance(row, dict) or set(row) != {identity, digest_key}
                    or not isinstance(row[identity], str) or not row[identity]
                    or len(row[identity]) > 256 or row[identity] in seen
                    or not isinstance(row[digest_key], str) or len(row[digest_key]) != 64
                    or any(character not in "0123456789abcdef" for character in row[digest_key])):
                raise ValueError("Physical Shader validation binding identity is invalid")
            seen.add(row[identity])
    return hashlib.sha256(_canonical(binding)).hexdigest()


# Reviewed migrations of replay scheduling only. Both source and destination
# are exact algorithm hashes; a future validator edit is never accepted by this
# list automatically. ABI rules, every other algorithm and native binary stay
# identical. These are not release-version comparisons.
_RESTORE_SCHEDULING_UPGRADES = (
    # Same immutable originals, signed candidate bytes and checked driver
    # pipeline creation. Same-RDC warm restore confirms the native mapping
    # instead of replaying an anchor separately for each restored stage.
    ("f67a38aa325d67137a43386b4eeb0e23b5734939b121d5fe2149ec653ecc2ecd", "1e47068ad84fd05c2f1340722249f8a6e5997c7cef4fabd2de5bf6c549e8f0c2"),
    ("bdd37c457a5993373376f158c6708b1732207b1fb5eb99c6d4c673dab5189a9a", "70b658b3a0e94e3f1012b8c5d90b2f9ab065d4d07fe9b50aaa945ebdb00ef4f8"),
    ("46feff54c4aec6c9670ea985d45cbedf52b2bd3d6f8351644759be4da9065e4c", "70b658b3a0e94e3f1012b8c5d90b2f9ab065d4d07fe9b50aaa945ebdb00ef4f8"),
)

# Exact, reviewed core + Bridge transition: batching changes only automatic
# replay/display scheduling; replay-content scope excludes editor annotations.
# Original/candidate byte checks, driver creation and physical ABI stay checked.
# Never infer this list from a release number or accept an unknown binary.
_RESTORE_VALIDATION_UPGRADES = (
    ("99c8dda939eab64e93e8a022e2b5408ca868a3c768f236c796e2a2c02dace755",
     "9e120caae218ae16c0e1c6a6aa4542e8333a93dfe70e7c5f1c47f32ba5daf55e"),
)


def compatible_validation_binding(previous, current):
    """Admit one reviewed scheduling refactor after authenticating the receipt."""
    old_digest = physical_validation_binding_sha256(previous)
    new_digest = physical_validation_binding_sha256(current)
    if (old_digest, new_digest) in _RESTORE_VALIDATION_UPGRADES:
        return True
    if any(previous[field] != current[field] for field in (
            "schema", "schemaVersion", "abiRulesVersion", "nativeBinaries")):
        return False
    old = {row["role"]: row["codeSha256"] for row in previous["algorithms"]}
    new = {row["role"]: row["codeSha256"] for row in current["algorithms"]}
    role = "bridge-physical-shader-validation"
    if set(old) != set(new) or any(old[key] != new[key] for key in old if key != role):
        return False
    return (old.get(role), new.get(role)) in _RESTORE_SCHEDULING_UPGRADES


def compatible_validation_policies(current):
    """Bounded Host hints; every use still authenticates bytes/signature here."""
    if current is None:
        return []
    physical_validation_binding_sha256(current)
    role = "bridge-physical-shader-validation"
    algorithms = {row["role"]: row["codeSha256"] for row in current["algorithms"]}
    current_digest = physical_validation_binding_sha256(current)
    result = [source for source, destination in _RESTORE_VALIDATION_UPGRADES if destination == current_digest]
    for source, destination in _RESTORE_SCHEDULING_UPGRADES:
        if algorithms.get(role) != destination:
            continue
        previous = dict(current)
        previous["algorithms"] = [dict(row, codeSha256=source) if row["role"] == role else dict(row)
                                  for row in current["algorithms"]]
        result.append(physical_validation_binding_sha256(previous))
    return result


class ReceiptStore:
    def __init__(self, directory=None):
        self.directory = directory or os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "ReplayLab", "validation-keys")
        self.path = os.path.join(self.directory, "shader-restore-v1.key")

    def _key(self, create=False):
        if create:
            os.makedirs(self.directory, exist_ok=True)
        info = os.lstat(self.directory)
        if (not stat.S_ISDIR(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("Shader restore key directory is redirected")
        if create:
            try:
                with open(self.path, "xb") as stream:
                    stream.write(os.urandom(32))
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                pass
        if _regular(self.path).st_size != 32:
            raise ValueError("Shader restore signing key is invalid")
        with open(self.path, "rb") as stream:
            key = stream.read(33)
        if len(key) != 32:
            raise ValueError("Shader restore signing key changed")
        return key

    def seal(self, payload):
        raw = _canonical(payload)
        if payload.get("schema") != SCHEMA or len(raw) > MAX_RECEIPT_BYTES:
            raise ValueError("Shader restore proof is invalid or oversized")
        signature = hmac.new(self._key(create=True), raw, hashlib.sha256).hexdigest()
        return {"payload": json.loads(raw.decode("ascii")), "signature": signature}

    def verify(self, receipt):
        if not isinstance(receipt, dict) or set(receipt) != {"payload", "signature"}:
            raise ValueError("Shader restore proof envelope is invalid")
        payload, signature = receipt["payload"], receipt["signature"]
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise ValueError("Shader restore proof schema is invalid")
        raw = _canonical(payload)
        if len(raw) > MAX_RECEIPT_BYTES or not isinstance(signature, str):
            raise ValueError("Shader restore proof is invalid or oversized")
        expected = hmac.new(self._key(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Shader restore proof signature does not match")
        return json.loads(raw.decode("ascii"))


def upgrade_receipts_replay_scope(store, receipts, capture_path, session_id="scope-upgrade"):
    """Derive annotation-independent proofs only from authenticated exact bytes.

    This adds a file-content identity, never a new validation or compiler claim.
    Every old receipt must authenticate and match the full current RDC first.
    The caller retains the old snapshot when atomically saving this accelerator.
    """
    if not isinstance(receipts, list) or not receipts or len(receipts) > 4096:
        raise ValueError("Shader restore receipt batch is invalid")
    payloads = [store.verify(receipt) for receipt in receipts]
    cache = CaptureDigestCache()
    full_digest = cache.digest(capture_path, session_id)
    if any(payload.get("captureSha256") != full_digest for payload in payloads):
        raise ValueError("Original RDC bytes do not match the authenticated receipt")
    before = cache.key
    replay_digest = cache.replay_digest(capture_path, session_id)
    cache.digest(capture_path, session_id)
    if cache.key != before:
        raise ValueError("RDC changed while deriving replay identity")
    result = []
    for payload in payloads:
        previous = payload.get("captureReplaySha256")
        if previous is not None and (previous != replay_digest or
                payload.get("captureReplayIdentitySchema") != "bread-rdc-replay-content/v1"):
            raise ValueError("Authenticated replay identity is inconsistent")
        payload.update(captureReplaySha256=replay_digest,
                       captureReplayIdentitySchema="bread-rdc-replay-content/v1")
        result.append(store.seal(payload))
    return result


class CaptureDigestCache:
    """Hash once per loaded capture session, invalidating changed file facts."""

    def __init__(self):
        self.key = None
        self.value = None
        self.replay_key = None
        self.replay_value = None

    @staticmethod
    def _identity(info):
        # On Windows path-stat ctime is creation time while fd-stat can expose
        # change time. They are not comparable identities for a stable file.
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    def digest(self, path, session_id):
        path = os.path.normcase(os.path.abspath(path))
        before = _regular(path)
        key = (path, session_id, self._identity(before), before.st_ctime_ns)
        if self.key == key:
            return self.value
        with open(path, "rb") as stream:
            if self._identity(os.fstat(stream.fileno())) != self._identity(before):
                raise ValueError("RDC changed before hashing")
            digest = _stream_sha256(stream)
            if self._identity(os.fstat(stream.fileno())) != self._identity(before):
                raise ValueError("RDC changed while hashing")
        if self._identity(_regular(path)) != self._identity(before):
            raise ValueError("RDC changed after hashing")
        self.key, self.value = key, digest
        return self.value

    def replay_digest(self, path, session_id):
        """Keep annotations out of capture identity; retain original GPU bytes."""
        path = os.path.normcase(os.path.abspath(path))
        before = _regular(path)
        key = (path, session_id, self._identity(before), before.st_ctime_ns)
        if key == self.replay_key:
            return self.replay_value
        spec = importlib.util.spec_from_file_location('_bread_rdc_replay_identity',
            os.path.join(os.path.dirname(__file__), 'rdc_replay_identity.py'))
        protocol = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(protocol)
        with open(path, 'rb') as stream:
            if self._identity(os.fstat(stream.fileno())) != self._identity(before):
                raise ValueError('RDC changed before replay hashing')
            digest = _file_hasher()
            try:
                value = protocol.replay_digest(stream, hasher=digest)
            finally:
                close = getattr(digest, 'close', None)
                if close is not None:
                    close()
            if self._identity(os.fstat(stream.fileno())) != self._identity(before):
                raise ValueError('RDC changed during replay hashing')
        if self._identity(_regular(path)) != self._identity(before):
            raise ValueError('RDC changed after replay hashing')
        self.replay_key, self.replay_value = key, value
        return value
