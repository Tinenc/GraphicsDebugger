import ast
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


BRIDGE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge"


def namespace():
    tree = ast.parse((BRIDGE / "__init__.py").read_text(encoding="utf-8"))
    wanted = {"_captured_shader_identity_for_context", "_captured_original_from_shared_owner"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    original = SimpleNamespace(entryPoint="MainPS", rawBytes=b"captured")
    owner = {"captureSessionId": "a" * 32, "captureToken": "capture-token", "api": "d3d12",
             "originalResource": 1000000000000000723, "expectedOriginalResourceId": 1000000000000000723,
             "expectedOriginalSha256": hashlib.sha256(b"captured").hexdigest(), "entryPoint": "MainPS",
             "stageName": "fragment", "candidateRawSha256": hashlib.sha256(b"candidate").hexdigest(),
             "capturedOriginalReflection": original}
    result = {"hashlib": hashlib, "_shared_shader_replacements": {1000000000000000723: owner},
              "_capture_session_id": "a" * 32, "_current_capture_token": lambda: "capture-token",
              "_shared_shader_replacement_capture_token": "capture-token", "_shader_api_token": lambda ctrl: "d3d12",
              "_resource_id_int": int, "_shader_raw_bytes": lambda reflection: reflection.rawBytes}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BRIDGE / "__init__.py"), "exec"), result)
    return result, owner


def test_context_keeps_active_bytes_and_proves_retained_captured_identity():
    ns, owner = namespace()
    current = SimpleNamespace(entryPoint="MainPS", rawBytes=b"candidate")
    identity = ns["_captured_shader_identity_for_context"](None, current, 1000000000000000723, 11384, "fragment", "MainPS")
    assert identity["rawSha256"] == owner["expectedOriginalSha256"]
    assert current.rawBytes == b"candidate"
    spec = importlib.util.spec_from_file_location("context_original_test", BRIDGE / "ai_context.py")
    context = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(context)
    value = context._normalise_shader({"resourceId": 1000000000000000723, "stage": "fragment", "entryPoint": "MainPS",
        "rawSha256": owner["candidateRawSha256"], "capturedOriginalIdentity": identity})
    assert value["capturedOriginalIdentity"]["resourceId"] == "1000000000000000723"
    assert value["rawSha256"] == owner["candidateRawSha256"]


@pytest.mark.parametrize("field,value", [("captureSessionId", "b" * 32), ("captureToken", "other"),
    ("expectedOriginalSha256", "c" * 64), ("candidateRawSha256", "d" * 64), ("stageName", "vertex")])
def test_context_rejects_changed_owner_or_replacement(field, value):
    ns, owner = namespace()
    owner[field] = value
    with pytest.raises(RuntimeError):
        ns["_captured_shader_identity_for_context"](None, SimpleNamespace(entryPoint="MainPS", rawBytes=b"candidate"),
            1000000000000000723, 11384, "fragment", "MainPS")


@pytest.mark.parametrize("api", ["d3d11", "vulkan", "opengl"])
def test_captured_reflection_is_valid_for_native_apis_that_preserve_creation_records(api):
    ns, owner = namespace()
    owner["api"] = api
    ns["_shader_api_token"] = lambda ctrl: api
    identity = ns["_captured_shader_identity_for_context"](
        None, SimpleNamespace(entryPoint="MainPS", rawBytes=b"captured"),
        1000000000000000723, 11384, "fragment", "MainPS")
    assert identity["rawSha256"] == owner["expectedOriginalSha256"]
    with pytest.raises(RuntimeError, match="changed outside Bread"):
        ns["_captured_shader_identity_for_context"](
            None, SimpleNamespace(entryPoint="MainPS", rawBytes=b"foreign"),
            1000000000000000723, 11384, "fragment", "MainPS")


def test_d3d12_still_requires_the_active_candidate_reflection():
    ns, _ = namespace()
    with pytest.raises(RuntimeError, match="changed outside Bread"):
        ns["_captured_shader_identity_for_context"](
            None, SimpleNamespace(entryPoint="MainPS", rawBytes=b"captured"),
            1000000000000000723, 11384, "fragment", "MainPS")
