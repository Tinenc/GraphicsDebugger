from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile

import pytest

from src.shader_workspace import (
    ManifestValidationError,
    ShaderKey,
    ShaderWorkspaceService,
    ToolExecutionError,
    ToolchainCatalog,
    ToolConfigurationError,
    ToolchainRoutingError,
)
from src.shader_workspace.binary_formats import fingerprint_shader_executable
from src.shader_workspace.catalog import capture_identity
from src.shader_workspace.io import atomic_write_json
from src.shader_workspace.toolchains import (
    route_record,
    toolchain_document,
    validator_record,
)
from src.shader_workspace.tools import ToolRegistry
import src.shader_workspace.workspace as workspace_module


FAKE_SPIRV_TOOL = r'''from pathlib import Path
import shutil
import sys

mode = sys.argv[1]
source = Path(sys.argv[2])
target = Path(sys.argv[3]) if len(sys.argv) > 3 else None
if mode == "decompile":
    target.write_text("#version 450\nlayout(location=0) out vec4 color;\nvoid main(){color=vec4(1);}\n", encoding="utf-8")
elif mode == "compile":
    shutil.copyfile(Path(sys.argv[4]), target)
elif mode == "disassemble":
    target.write_text(
        "; SPIR-V\nOpCapability Shader\nOpMemoryModel Logical GLSL450\n",
        encoding="utf-8",
    )
elif mode in {"assemble", "assemble-invalid"}:
    payload = Path(sys.argv[4]).read_bytes()
    if mode == "assemble-invalid" or b"INVALID" in source.read_bytes():
        payload += b"\xff\xff\x01\x00"
    target.write_bytes(payload)
elif mode == "validate":
    if source.read_bytes().endswith(b"\xff\xff\x01\x00"):
        raise SystemExit(9)
elif mode == "validate-fail":
    raise SystemExit(7)
else:
    raise SystemExit(3)
'''


@pytest.fixture()
def spirv_workspace_fixture(tmp_path: Path) -> dict:
    script = tmp_path / "spirv_tool.py"
    script.write_text(FAKE_SPIRV_TOOL, encoding="utf-8")
    executable = Path(sys.executable).resolve()
    identity = {
        "executable": str(executable),
        "executableSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "fixedFileHashes": {
            str(script.resolve()): hashlib.sha256(script.read_bytes()).hexdigest()
        },
    }
    tools_path = tmp_path / "shader-tools.v1.json"
    atomic_write_json(
        tools_path,
        {
            "schema": "renderdoc-ultra-shader-tools/v1",
            "schemaVersion": 1,
            "tools": {
                "fake-spirv-cross": {
                    **identity,
                    "decompile": {
                        "args": [str(script), "decompile", "{input}", "{output}"],
                        "timeoutSeconds": 10,
                    },
                },
                "fake-glslang": {
                    **identity,
                    "compile": {
                        "args": [
                            str(script),
                            "compile",
                            "{input}",
                            "{output}",
                            "{baseline}",
                            "{stage}",
                            "{target_api}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
                "fake-spirv-dis": {
                    **identity,
                    "decompile": {
                        "args": [
                            str(script),
                            "disassemble",
                            "{input}",
                            "{output}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
                "fake-spirv-as": {
                    **identity,
                    "compile": {
                        "args": [
                            str(script),
                            "assemble",
                            "{input}",
                            "{output}",
                            "{baseline}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
                "fake-spirv-as-invalid": {
                    **identity,
                    "compile": {
                        "args": [
                            str(script),
                            "assemble-invalid",
                            "{input}",
                            "{output}",
                            "{baseline}",
                        ],
                        "timeoutSeconds": 10,
                    },
                },
                "fake-spirv-val": {
                    **identity,
                    "validate": {
                        "args": [str(script), "validate", "{input}"],
                        "timeoutSeconds": 10,
                    },
                },
                "fake-spirv-val-fail": {
                    **identity,
                    "validate": {
                        "args": [str(script), "validate-fail", "{input}"],
                        "timeoutSeconds": 10,
                    },
                },
            },
        },
    )
    registry = ToolRegistry(tools_path)
    routes = [
        route_record(
            registry,
            route_id="fake-spirv-glsl",
            tool_id="fake-spirv-cross",
            operation="decompile",
            input_format="spirv",
            output_format="glsl",
            stages=["fragment"],
            fidelity="reconstructed-source",
            priority=10,
            implementation="test SPIR-V source recovery",
            source_url="https://example.invalid/spirv-cross",
            caveats=["Reconstructed GLSL is not original source."],
        ),
        route_record(
            registry,
            route_id="fake-glsl-spirv",
            tool_id="fake-glslang",
            operation="compile",
            input_format="glsl",
            output_format="spirv",
            stages=["fragment"],
            fidelity="native-compile",
            priority=10,
            implementation="test GLSL compiler",
            source_url="https://example.invalid/glslang",
        ),
    ]
    routes_path = tmp_path / "shader-toolchains.v1.json"
    atomic_write_json(routes_path, toolchain_document(tools_path, routes, []))

    # Minimal physically attributable fragment module.  Route selection must
    # resolve an actual OpEntryPoint; a bare header is not evidence that the
    # capture-visible name exists in the module.
    name = b"main\0"
    name += b"\0" * (-len(name) % 4)
    name_words = struct.unpack("<%dI" % (len(name) // 4), name)
    op_entry = [((3 + len(name_words)) << 16) | 15, 4, 1, *name_words]
    blob = struct.pack(
        "<%dI" % (5 + len(op_entry)),
        0x07230203,
        0x00010000,
        0,
        2,
        0,
        *op_entry,
    )
    key = ShaderKey(501, "fragment", "main", hashlib.sha256(blob).hexdigest())
    catalog_root = tmp_path / "catalog"
    (catalog_root / "blobs").mkdir(parents=True)
    (catalog_root / "blobs" / (key.raw_sha256 + ".bin")).write_bytes(blob)
    shader = {
        "shaderKey": key.catalog_key,
        "resourceId": key.resource_id,
        "shaderResourceId": key.resource_id,
        "stage": key.stage,
        "entryPoint": key.entry_point,
        "encoding": "SPIRV",
        "rawPath": "blobs/%s.bin" % key.raw_sha256,
        "rawSize": len(blob),
        "rawSha256": key.raw_sha256,
        "interfaceComplete": True,
        "outputTopology": "Topology.Unknown",
        "dynamicLinkageInterfaceCount": 0,
        "inputSignature": [],
        "outputSignature": [],
        "readOnlyResources": [],
        "readWriteResources": [],
        "samplers": [],
        "constantBlocks": [],
        "threadGroupSize": [],
    }
    catalog = {
        "schema": "renderdoc-ultra-capture-symbols/v1",
        "schemaVersion": 1,
        "capturePath": "D:/captures/vulkan-frame.rdc",
        "api": "Vulkan",
        "shaders": [shader],
        "uses": [
            {
                "eventId": 71,
                "shaderKey": key.catalog_key,
                "resourceId": key.resource_id,
                "stage": key.stage,
                "entryPoint": key.entry_point,
                "rawSha256": key.raw_sha256,
            }
        ],
    }
    catalog["captureId"] = capture_identity(catalog, [key])
    catalog_path = catalog_root / "shader_catalog.v1.json"
    atomic_write_json(catalog_path, catalog)
    return {
        "service": ShaderWorkspaceService(tools_path, routes_path),
        "routesPath": routes_path,
        "catalogPath": catalog_path,
        "key": key,
        "blob": blob,
        "root": tmp_path,
    }


def configure_native_spirv_assembly(
    fixture: dict,
    *,
    shader_format: str = "spirv",
    include_validator: bool = True,
    failing_validator: bool = False,
    invalid_assembly: bool = False,
    duplicate_validator: bool = False,
) -> ShaderWorkspaceService:
    registry = ToolRegistry(fixture["service"].registry.path)
    assembly_format = (
        "opengl-spirv-asm"
        if shader_format == "opengl-spirv"
        else "spirv-asm"
    )
    routes = [
        route_record(
            registry,
            route_id="fake-native-disassemble-" + shader_format,
            tool_id="fake-spirv-dis",
            operation="decompile",
            input_format=shader_format,
            output_format=assembly_format,
            stages=["fragment"],
            fidelity="ir-disassembly",
            priority=1,
            implementation="test SPIRV-Tools disassembler",
            source_url="https://example.invalid/spirv-dis",
        ),
        route_record(
            registry,
            route_id="fake-native-assemble-" + shader_format,
            tool_id=(
                "fake-spirv-as-invalid"
                if invalid_assembly
                else "fake-spirv-as"
            ),
            operation="compile",
            input_format=assembly_format,
            output_format=shader_format,
            stages=["fragment"],
            fidelity="ir-assembly",
            priority=1,
            implementation="test SPIRV-Tools assembler",
            source_url="https://example.invalid/spirv-as",
        ),
    ]
    validators = []
    if include_validator:
        validator_tool = (
            "fake-spirv-val-fail" if failing_validator else "fake-spirv-val"
        )
        validators.append(
            validator_record(
                registry,
                validator_id="fake-spirv-val-" + shader_format,
                tool_id=validator_tool,
                input_format=shader_format,
                stages=["fragment"],
                priority=1,
                implementation="test spirv-val",
                source_url="https://example.invalid/spirv-val",
            )
        )
        if duplicate_validator:
            validators.append(
                validator_record(
                    registry,
                    validator_id="fake-spirv-val-duplicate-" + shader_format,
                    tool_id=validator_tool,
                    input_format=shader_format,
                    stages=["fragment"],
                    priority=1,
                    implementation="test spirv-val",
                    source_url="https://example.invalid/spirv-val",
                )
            )
    atomic_write_json(
        fixture["routesPath"],
        toolchain_document(registry.path, routes, validators),
    )
    return ShaderWorkspaceService(registry.path, fixture["routesPath"])


def test_spirv_glsl_round_trip_workspace_is_eligible_for_replay_abi_gate(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    workspace = fixture["root"] / "workspace"
    service = fixture["service"]
    created = service.create(fixture["catalogPath"], fixture["key"], workspace)
    assert created["routePlan"]["inputFormat"] == "spirv"
    assert created["routePlan"]["editableFormat"] == "glsl"
    assert created["recoveryPlan"]["mode"] == "round-trip"

    decompiled = service.decompile(workspace)
    source_path = Path(decompiled["sourcePath"])
    assert source_path.name == "shader.glsl"
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "// edited\n", encoding="utf-8"
    )
    compiled = service.compile(workspace)
    assert Path(compiled["candidateBlobPath"]).read_bytes() == fixture["blob"]
    status = service.status(workspace)
    assert status["targetProfile"]["apiEnvironment"] == "vulkan"
    assert status["latestCompiled"]["abiGate"]["status"] == "pending-replay-reflection"
    assert status["applied"] is False
    request = service.candidate_reflection_request(workspace)
    assert request["binding"]["api"] == "vulkan"
    assert request["binding"]["encoding"] == "spirv"
    assert request["binding"]["targetProfile"] == "spirv"


def test_spirv_compile_only_recovery_promotes_glsl(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    routes_path = fixture["routesPath"]
    routes = json.loads(routes_path.read_text(encoding="utf-8"))
    routes["routes"] = [
        route for route in routes["routes"] if route["inputFormat"] == "glsl"
    ]
    atomic_write_json(routes_path, routes)
    service = ShaderWorkspaceService(fixture["service"].registry.path, routes_path)
    workspace = fixture["root"] / "compile-only"
    created = service.create(fixture["catalogPath"], fixture["key"], workspace)
    assert created["recoveryPlan"]["mode"] == "compile-only-recovery"
    representation = service.representation(workspace)
    candidate = workspace / "explanation" / "promoted.glsl"
    source = b"#version 450\nvoid main(){}\n"
    candidate.write_bytes(source)
    service.promote_explanatory_source(
        workspace,
        expected_representation_sha256=representation["sha256"],
        expected_source_sha256=hashlib.sha256(source).hexdigest(),
        acknowledge_semantic_equivalence_unverified=True,
    )
    compiled = service.compile(workspace)
    assert Path(compiled["candidateBlobPath"]).read_bytes() == fixture["blob"]


def test_byte_swapped_spirv_fingerprint_is_consistent_with_detection() -> None:
    blob = struct.pack(">6I", 0x07230203, 0x00010000, 0, 1, 0, 0x00010000)
    executable = fingerprint_shader_executable(blob, "swapped fixture")
    assert executable is not None
    assert executable.shader_format == "spirv"
    assert executable.raw_sha256 == hashlib.sha256(blob).hexdigest()


@pytest.mark.parametrize("native_glsl", [False, True])
def test_opengl_spirv_round_trip_uses_environment_specific_formats(
    spirv_workspace_fixture: dict, native_glsl: bool,
) -> None:
    fixture = spirv_workspace_fixture
    registry = fixture["service"].registry
    routes_path = fixture["routesPath"]
    routes = [
        route_record(
            registry,
            route_id="fake-opengl-spirv-glsl",
            tool_id="fake-spirv-cross",
            operation="decompile",
            input_format="opengl-spirv",
            output_format="glsl",
            stages=["fragment"],
            fidelity="reconstructed-source",
            priority=1,
            implementation="test OpenGL SPIR-V recovery",
            source_url="https://example.invalid/opengl-spirv-cross",
            caveats=["Fixture output is reconstructed GLSL."],
        ),
        route_record(
            registry,
            route_id="fake-glsl-opengl-spirv",
            tool_id="fake-glslang",
            operation="compile",
            input_format="glsl",
            output_format="opengl-spirv",
            stages=["fragment"],
            fidelity="native-compile",
            priority=1,
            implementation="test OpenGL GLSL compiler",
            source_url="https://example.invalid/opengl-glslang",
        ),
    ]
    atomic_write_json(
        routes_path,
        toolchain_document(registry.path, routes, []),
    )
    catalog_path = fixture["catalogPath"]
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["api"] = "OpenGL"
    catalog["capturePath"] = "D:/captures/opengl-frame.rdc"
    catalog["captureId"] = capture_identity(catalog, [fixture["key"]])
    atomic_write_json(catalog_path, catalog)

    service = ShaderWorkspaceService(registry.path, routes_path)
    if native_glsl:
        from tests.test_native_shader_compile import response
        service._native_compile_provider = lambda request: response(request, Path(request["candidate_path"]).read_bytes())
    workspace = fixture["root"] / "opengl-workspace"
    created = service.create(catalog_path, fixture["key"], workspace)
    assert created["routePlan"]["inputFormat"] == "opengl-spirv"
    assert created["targetProfile"]["apiEnvironment"] == "opengl"
    decompiled = service.decompile(workspace)
    source_path = Path(decompiled["sourcePath"])
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "// OpenGL edit\n",
        encoding="utf-8",
    )
    compiled = service.compile(workspace)
    if native_glsl:
        assert compiled["kind"] == "GLSL"
        assert Path(compiled["candidateBlobPath"]).read_bytes() == source_path.read_bytes()
        assert service.status(workspace)["latestCompiled"]["kind"] == "GLSL"
        request = service.candidate_reflection_request(workspace)
        assert request["binding"]["encoding"] == "glsl"
        assert request["binding"]["targetProfile"] == "opengl-spirv"
        from tests.test_shader_workspace_routes import ShaderWorkspaceRouteChainTests
        manifest = json.loads((workspace / "manifest.json").read_text())
        plan = ShaderWorkspaceRouteChainTests._project_candidate_plan(
            Path(compiled["candidateBlobPath"]), raw_sha256=compiled["rawSha256"],
            code_sha256=compiled["codeSha256"], byte_size=len(source_path.read_bytes()),
            kind="GLSL", source_sha256=compiled["sourceSha256"], source_revision=compiled["revision"],
            target_profile=manifest["targetProfile"])
        plan["compilerBindingSha256"] = service._project_compiler_binding(manifest)["bindingSha256"]
        assert service.hydrate_project_candidate(workspace, plan)["requiresReplayAbiValidation"]
        assert service.status(workspace)["latestCompiled"]["abiCompatible"] is False
    else:
        assert Path(compiled["candidateBlobPath"]).read_bytes() == fixture["blob"]


@pytest.mark.parametrize(
    ("shader_format", "api_name", "editable_format", "validator_id"),
    [
        ("spirv", "Vulkan", "spirv-asm", "fake-spirv-val-spirv"),
        (
            "opengl-spirv",
            "OpenGL",
            "opengl-spirv-asm",
            "fake-spirv-val-opengl-spirv",
        ),
    ],
)
def test_native_spirv_automatic_fallback_binds_and_revalidates_spirv_val(
    spirv_workspace_fixture: dict,
    shader_format: str,
    api_name: str,
    editable_format: str,
    validator_id: str,
) -> None:
    fixture = spirv_workspace_fixture
    if api_name == "OpenGL":
        catalog = json.loads(
            fixture["catalogPath"].read_text(encoding="utf-8")
        )
        catalog["api"] = "OpenGL"
        catalog["capturePath"] = "D:/captures/opengl-native-frame.rdc"
        catalog["captureId"] = capture_identity(catalog, [fixture["key"]])
        atomic_write_json(fixture["catalogPath"], catalog)
    service = configure_native_spirv_assembly(
        fixture, shader_format=shader_format
    )
    selection = ToolchainCatalog(
        service.registry.path, fixture["routesPath"]
    ).plan_automatic_recovery(
        shader_format,
        "fragment",
        capture_encoding=shader_format,
        target_api=api_name,
    )
    assert selection["editableFormat"] == editable_format
    assert selection["strictNativeIrValidationRequired"] is True
    assert [
        item["validatorId"] for item in selection["requiredValidators"]
    ] == [validator_id]

    workspace = fixture["root"] / ("native-" + shader_format)
    created = service.create(fixture["catalogPath"], fixture["key"], workspace)
    assert created["routePlan"]["schema"].endswith("/v2")
    assert created["routePlan"]["schemaVersion"] == 2
    assert created["routePlan"]["editableFormat"] == editable_format
    assert created["routePlan"]["validators"][0]["validator"][
        "validatorId"
    ] == validator_id

    decompiled = service.decompile(workspace)
    assert len(decompiled["baselineValidatorJobs"]) == 1
    assert len(decompiled["baselineCompileValidatorJobs"]) == 1
    source_path = Path(decompiled["sourcePath"])
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "; reviewed edit\n",
        encoding="utf-8",
    )
    compiled = service.compile(workspace)
    assert len(compiled["baselineValidatorJobs"]) == 1
    assert len(compiled["validatorJobs"]) == 1
    status = service.status(workspace)
    latest = status["latestCompiled"]
    assert latest["rawSha256"] == hashlib.sha256(fixture["blob"]).hexdigest()
    assert latest["baselineValidatorJobs"][0]["passed"] is True
    assert latest["validatorJobs"][0]["passed"] is True
    assert latest["validatorJobs"][0]["inputSha256"] == latest["rawSha256"]
    assert latest["validatorJobs"][0]["validatorBinding"] == created[
        "routePlan"
    ]["validators"][0]

    validator_input = workspace / latest["validatorJobs"][0]["inputPath"]
    validator_input.write_bytes(fixture["blob"] + struct.pack("<I", 0x00010000))
    with pytest.raises(
        ManifestValidationError, match="spirv-val artifact/hash mismatch"
    ):
        service.status(workspace)


@pytest.mark.parametrize(
    ("shader_format", "api_name"),
    [("spirv", "Vulkan"), ("opengl-spirv", "OpenGL")],
)
def test_existing_native_workspace_keeps_validator_when_new_provider_has_higher_priority(
    spirv_workspace_fixture: dict,
    shader_format: str,
    api_name: str,
) -> None:
    fixture = spirv_workspace_fixture
    if api_name == "OpenGL":
        capture_catalog = json.loads(fixture["catalogPath"].read_bytes())
        capture_catalog["api"] = "OpenGL"
        capture_catalog["captureId"] = capture_identity(capture_catalog, [fixture["key"]])
        atomic_write_json(fixture["catalogPath"], capture_catalog)
    original_service = configure_native_spirv_assembly(fixture, shader_format=shader_format)
    workspace = fixture["root"] / ("native-pinned-validator-" + shader_format)
    created = original_service.create(fixture["catalogPath"], fixture["key"], workspace)
    decompiled = original_service.decompile(workspace)
    source_path = Path(decompiled["sourcePath"])
    source_before = source_path.read_bytes()
    original_binding = created["routePlan"]["validators"][0]

    routes = json.loads(fixture["routesPath"].read_bytes())
    new_validator_id = "new-higher-priority-spirv-val-" + shader_format
    routes["validators"].append(
        validator_record(
            original_service.registry,
            validator_id=new_validator_id,
            tool_id="fake-spirv-val-fail",
            input_format=shader_format,
            stages=["fragment"],
            priority=0,
            implementation="test additional spirv-val",
            source_url="https://example.invalid/new-spirv-val",
        )
    )
    atomic_write_json(fixture["routesPath"], routes)

    with pytest.raises(ToolConfigurationError):
        original_service.status(workspace)
    current_catalog = ToolchainCatalog(original_service.registry.path, fixture["routesPath"])
    assert current_catalog.strict_spirv_validator(shader_format, "fragment").validator_id == new_validator_id
    current_service = ShaderWorkspaceService(original_service.registry.path, fixture["routesPath"])
    status = current_service.status(workspace)
    assert status["routePlan"] == created["routePlan"]
    # The new default deliberately fails if invoked. Existing source and
    # candidates must continue through their original exact validator binding.
    compiled = current_service.compile(workspace)
    assert compiled["revision"] == 1
    latest = current_service.status(workspace)["latestCompiled"]
    for job in latest["baselineValidatorJobs"] + latest["validatorJobs"]:
        assert job["passed"] is True
        assert job["validatorBinding"] == original_binding
    assert source_path.read_bytes() == source_before


def test_explicit_native_spirv_assembly_binds_the_same_strict_validator(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    service = configure_native_spirv_assembly(fixture)
    catalog = ToolchainCatalog(service.registry.path, fixture["routesPath"])
    planned = catalog.plan_recovery(
        "spirv",
        "fragment",
        editable_format="spirv-asm",
        target_api="Vulkan",
    )
    assert planned["strictNativeIrValidationRequired"] is True
    assert [
        item["validatorId"] for item in planned["requiredValidators"]
    ] == ["fake-spirv-val-spirv"]

    workspace = fixture["root"] / "explicit-native-spirv"
    created = service.create(
        fixture["catalogPath"],
        fixture["key"],
        workspace,
        editable_format="spirv-asm",
    )
    assert created["routePlan"]["validators"][0]["validator"][
        "validatorId"
    ] == "fake-spirv-val-spirv"


@pytest.mark.parametrize("duplicate_validator", [False, True])
def test_native_spirv_automatic_fallback_requires_one_fixed_spirv_val(
    spirv_workspace_fixture: dict,
    duplicate_validator: bool,
) -> None:
    fixture = spirv_workspace_fixture
    service = configure_native_spirv_assembly(
        fixture,
        include_validator=duplicate_validator,
        duplicate_validator=duplicate_validator,
    )
    catalog = ToolchainCatalog(service.registry.path, fixture["routesPath"])
    with pytest.raises(
        ToolchainRoutingError,
        match=("ambiguous" if duplicate_validator else "spirv-val"),
    ):
        catalog.select_round_trip("spirv", "fragment", target_api="Vulkan")
    with pytest.raises(
        ToolchainRoutingError,
        match=("ambiguous" if duplicate_validator else "spirv-val"),
    ):
        catalog.plan_recovery(
            "spirv",
            "fragment",
            editable_format="spirv-asm",
            target_api="Vulkan",
        )
    with pytest.raises(
        ToolchainRoutingError,
        match=("ambiguous" if duplicate_validator else "spirv-val"),
    ):
        service.create(
            fixture["catalogPath"],
            fixture["key"],
            fixture["root"]
            / (
                "explicit-ambiguous-validator"
                if duplicate_validator
                else "explicit-missing-validator"
            ),
            editable_format="spirv-asm",
        )
    workspace = fixture["root"] / (
        "ambiguous-validator" if duplicate_validator else "missing-validator"
    )
    created = service.create(fixture["catalogPath"], fixture["key"], workspace)
    assert created["routePlan"]["editableFormat"] == "glsl"
    assert created["routePlan"]["decompile"] == []
    assert created["routePlan"]["compile"] == []
    assert created["routePlan"]["validators"] == []
    assert created["recoveryPlan"]["mode"] == "evidence-only"


def test_native_spirv_fallback_fails_before_source_publication_when_validator_fails(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    service = configure_native_spirv_assembly(
        fixture, failing_validator=True
    )
    workspace = fixture["root"] / "baseline-validator-failure"
    service.create(fixture["catalogPath"], fixture["key"], workspace)
    with pytest.raises(ToolExecutionError, match="exited with code 7"):
        service.decompile(workspace)
    assert not (workspace / "source" / "shader.spvasm").exists()
    status = service.status(workspace)
    assert status["source"]["present"] is False
    assert status["latestCompiled"] is None
    assert status["revision"] == 0


def test_native_spirv_fallback_requires_compile_probe_validation(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    service = configure_native_spirv_assembly(
        fixture, invalid_assembly=True
    )
    workspace = fixture["root"] / "compile-probe-validator-failure"
    service.create(fixture["catalogPath"], fixture["key"], workspace)
    with pytest.raises(ToolExecutionError, match="exited with code 9"):
        service.decompile(workspace)
    assert not (workspace / "source" / "shader.spvasm").exists()
    status = service.status(workspace)
    assert status["source"]["present"] is False
    assert status["revision"] == 0


def test_native_spirv_candidate_validation_failure_is_atomic(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    service = configure_native_spirv_assembly(fixture)
    workspace = fixture["root"] / "candidate-validator-failure"
    service.create(fixture["catalogPath"], fixture["key"], workspace)
    decompiled = service.decompile(workspace)
    source_path = Path(decompiled["sourcePath"])
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "; INVALID\n",
        encoding="utf-8",
    )
    with pytest.raises(ToolExecutionError, match="exited with code 9"):
        service.compile(workspace)
    status = service.status(workspace)
    assert status["revision"] == 0
    assert status["latestCompiled"] is None
    assert list((workspace / "revisions").iterdir()) == []


def test_legacy_v1_native_spirv_workspace_is_readable_but_not_executable(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    service = configure_native_spirv_assembly(fixture)
    workspace = fixture["root"] / "legacy-v1-native"
    service.create(fixture["catalogPath"], fixture["key"], workspace)
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan = manifest["routePlan"]
    plan.pop("validators")
    plan["schema"] = "renderdoc-ultra-shader-workspace-route-plan/v1"
    plan["schemaVersion"] = 1
    unsigned = dict(plan)
    unsigned.pop("planSha256")
    plan["planSha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    atomic_write_json(manifest_path, manifest)

    before = service.status(workspace)
    assert before["routePlan"]["schemaVersion"] == 1
    with pytest.raises(ToolchainRoutingError, match="read-only evidence"):
        service.decompile(workspace)
    assert not (workspace / "source" / "shader.spvasm").exists()
    assert manifest_path.read_bytes() == json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8") + b"\n"


def test_legacy_v1_native_spirv_existing_source_cannot_compile(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    service = configure_native_spirv_assembly(fixture)
    workspace = fixture["root"] / "legacy-v1-native-source"
    service.create(fixture["catalogPath"], fixture["key"], workspace)
    service.decompile(workspace)
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan = manifest["routePlan"]
    previous_plan_sha256 = plan["planSha256"]
    plan.pop("validators")
    plan["schema"] = "renderdoc-ultra-shader-workspace-route-plan/v1"
    plan["schemaVersion"] = 1
    unsigned = dict(plan)
    unsigned.pop("planSha256")
    plan["planSha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    source = manifest["source"]
    source["routePlanSha256"] = plan["planSha256"]
    for history in manifest["history"]:
        if history.get("routePlanSha256") == previous_plan_sha256:
            history["routePlanSha256"] = plan["planSha256"]
    source.pop("baselineValidatorJobs")
    source["baselineCompile"].pop("validatorJobs")
    atomic_write_json(manifest_path, manifest)

    assert service.status(workspace)["source"]["present"] is True
    with pytest.raises(ToolchainRoutingError, match="read-only evidence"):
        service.compile(workspace)
    assert service.status(workspace)["revision"] == 0
    assert list((workspace / "revisions").iterdir()) == []


def test_native_spirv_v2_cannot_drop_or_tamper_validator_binding(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    service = configure_native_spirv_assembly(fixture)
    workspace = fixture["root"] / "native-validator-plan-tamper"
    service.create(fixture["catalogPath"], fixture["key"], workspace)
    manifest_path = workspace / "manifest.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))

    missing = json.loads(json.dumps(original))
    missing["routePlan"]["validators"] = []
    unsigned = dict(missing["routePlan"])
    unsigned.pop("planSha256")
    missing["routePlan"]["planSha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    atomic_write_json(manifest_path, missing)
    with pytest.raises(ManifestValidationError, match="exactly one bound"):
        service.status(workspace)

    tampered = json.loads(json.dumps(original))
    tampered["routePlan"]["validators"][0]["validator"][
        "validatorId"
    ] = "forged-spirv-val"
    unsigned = dict(tampered["routePlan"])
    unsigned.pop("planSha256")
    tampered["routePlan"]["planSha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    atomic_write_json(manifest_path, tampered)
    with pytest.raises(
        ManifestValidationError, match="capability changed|binding is inconsistent"
    ):
        service.status(workspace)


def test_native_spirv_validator_integrity_is_rechecked(
    spirv_workspace_fixture: dict,
) -> None:
    fixture = spirv_workspace_fixture
    service = configure_native_spirv_assembly(fixture)
    workspace = fixture["root"] / "native-validator-integrity-tamper"
    service.create(fixture["catalogPath"], fixture["key"], workspace)
    script = fixture["root"] / "spirv_tool.py"
    script.write_text(
        script.read_text(encoding="utf-8") + "\n# validator tamper\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestValidationError, match="spirv-val integrity"):
        service.status(workspace)


@pytest.mark.parametrize("publish_before_raise", [False, True])
def test_routed_decompile_manifest_failure_rolls_back_publication(
    spirv_workspace_fixture: dict,
    monkeypatch: pytest.MonkeyPatch,
    publish_before_raise: bool,
) -> None:
    fixture = spirv_workspace_fixture
    service = fixture["service"]
    workspace = fixture["root"] / (
        "route-manifest-after-publish"
        if publish_before_raise
        else "route-manifest-before-publish"
    )
    service.create(fixture["catalogPath"], fixture["key"], workspace)
    manifest_path = workspace / "manifest.json"
    manifest_before = manifest_path.read_bytes()
    real_write = workspace_module._write_manifest_with_retry
    write_calls = 0

    def fail_manifest_write(path: Path, manifest: dict) -> None:
        nonlocal write_calls
        write_calls += 1
        if publish_before_raise:
            real_write(path, manifest)
        raise OSError("injected manifest commit failure")

    monkeypatch.setattr(
        workspace_module, "_write_manifest_with_retry", fail_manifest_write
    )
    for _attempt in range(2):
        with pytest.raises(OSError, match="injected manifest"):
            service.decompile(workspace)
        assert manifest_path.read_bytes() == manifest_before
        assert not (workspace / "source" / "shader.glsl").exists()
        assert list((workspace / "revisions").iterdir()) == []
        assert write_calls == _attempt + 1


@pytest.mark.parametrize("publish_before_raise", [False, True])
def test_routed_compile_manifest_failure_rolls_back_candidate(
    spirv_workspace_fixture: dict,
    monkeypatch: pytest.MonkeyPatch,
    publish_before_raise: bool,
) -> None:
    fixture = spirv_workspace_fixture
    service = fixture["service"]
    workspace = fixture["root"] / (
        "route-compile-manifest-after-publish"
        if publish_before_raise
        else "route-compile-manifest-before-publish"
    )
    service.create(fixture["catalogPath"], fixture["key"], workspace)
    service.decompile(workspace)
    manifest_path = workspace / "manifest.json"
    manifest_before = manifest_path.read_bytes()
    real_write = workspace_module._write_manifest_with_retry
    write_calls = 0

    def fail_manifest_write(path: Path, manifest: dict) -> None:
        nonlocal write_calls
        write_calls += 1
        if publish_before_raise:
            real_write(path, manifest)
        raise OSError("injected manifest commit failure")

    monkeypatch.setattr(
        workspace_module, "_write_manifest_with_retry", fail_manifest_write
    )
    for attempt in range(2):
        with pytest.raises(OSError, match="injected manifest"):
            service.compile(workspace)
        assert manifest_path.read_bytes() == manifest_before
        assert list((workspace / "revisions").iterdir()) == []
        assert write_calls == attempt + 1


def test_one_way_route_manifest_failure_removes_explanatory_publication(
    spirv_workspace_fixture: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = spirv_workspace_fixture
    routes = json.loads(fixture["routesPath"].read_text(encoding="utf-8"))
    routes["routes"] = [
        route
        for route in routes["routes"]
        if route["operation"] == "decompile"
    ]
    atomic_write_json(fixture["routesPath"], routes)
    service = ShaderWorkspaceService(
        fixture["service"].registry.path, fixture["routesPath"]
    )
    workspace = fixture["root"] / "one-way-route-manifest-failure"
    service.create(fixture["catalogPath"], fixture["key"], workspace)
    manifest_path = workspace / "manifest.json"
    manifest_before = manifest_path.read_bytes()
    real_write = workspace_module._write_manifest_with_retry

    def fail_after_publish(path: Path, manifest: dict) -> None:
        real_write(path, manifest)
        raise OSError("injected manifest commit failure")

    monkeypatch.setattr(
        workspace_module, "_write_manifest_with_retry", fail_after_publish
    )
    with pytest.raises(OSError, match="injected manifest"):
        service.decompile(workspace)
    assert manifest_path.read_bytes() == manifest_before
    assert not (workspace / "explanation").exists()
