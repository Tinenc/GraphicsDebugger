"""Safe deterministic core for external RenderDoc shader workspaces."""

from .abi import (
    AbiComparison,
    ShaderAbi,
    build_candidate_reflection_request,
    compare_shader_abi,
    parse_shader_abi,
    validate_candidate_reflection_response,
)
from .catalog import (
    ShaderKey,
    list_exact_shader_family_uses,
    select_unique_shader_for_use,
)
from .errors import (
    AbiValidationError,
    CatalogValidationError,
    ManifestValidationError,
    ShaderWorkspaceError,
    ToolConfigurationError,
    ToolExecutionError,
    ToolchainRoutingError,
    WorkspaceWatchConflictError,
    WorkspaceWatchError,
)
from .watcher import ShaderWorkspaceWatchManager
from .workspace import ShaderWorkspaceService
from .toolchains import (
    FormatDetection,
    RouteCapability,
    ToolchainCatalog,
    ValidatorCapability,
    detect_shader_file,
    detect_shader_format,
)
from .toolchain_config import generate_official_toolchain_configs
from .profiles import TargetProfile, extract_target_profile, validate_target_profile
from .representations import REPRESENTATION_LANES, REPRESENTATION_SCHEMA
from .diagnostics import DIAGNOSTIC_SCHEMA, classify_shader_failure
from .organization import (
    ORGANIZATION_GATE_SCHEMA,
    ORGANIZATION_POLICY_VERSION,
    ORGANIZATION_SESSION_SCHEMA,
    ShaderOrganizationError,
    ShaderOrganizationManager,
)
from .flow_graph import (
    FLOW_GRAPH_SCHEMA,
    FLOW_GRAPH_VERSION,
    MODULE_DESCRIPTIONS_SCHEMA,
    MODULE_DESCRIPTIONS_VERSION,
    ShaderFlowGraphError,
    build_shader_flow_graph,
    load_workspace_flow_graph_status,
    validate_shader_flow_graph,
)
from .adapters.dxil_access_signature import (
    DXIL_CBUFFER_ACCESS_SCHEMA,
    DxilAccessSignatureError,
    DxilCBufferAccess,
    DxilCBufferAccessBlock,
    DxilCBufferAccessSignature,
    extract_dxil_cbuffer_access_signature,
    extract_dxil_unannotated_cbuffer_bindings,
)

__all__ = [
    "AbiComparison",
    "AbiValidationError",
    "CatalogValidationError",
    "ManifestValidationError",
    "ShaderKey",
    "list_exact_shader_family_uses",
    "ShaderAbi",
    "ShaderWorkspaceError",
    "ShaderOrganizationError",
    "ShaderOrganizationManager",
    "ShaderWorkspaceService",
    "ShaderWorkspaceWatchManager",
    "FormatDetection",
    "RouteCapability",
    "ToolchainCatalog",
    "ValidatorCapability",
    "TargetProfile",
    "REPRESENTATION_LANES",
    "REPRESENTATION_SCHEMA",
    "DIAGNOSTIC_SCHEMA",
    "ORGANIZATION_GATE_SCHEMA",
    "ORGANIZATION_POLICY_VERSION",
    "ORGANIZATION_SESSION_SCHEMA",
    "FLOW_GRAPH_SCHEMA",
    "FLOW_GRAPH_VERSION",
    "MODULE_DESCRIPTIONS_SCHEMA",
    "MODULE_DESCRIPTIONS_VERSION",
    "ShaderFlowGraphError",
    "DXIL_CBUFFER_ACCESS_SCHEMA",
    "DxilAccessSignatureError",
    "DxilCBufferAccess",
    "DxilCBufferAccessBlock",
    "DxilCBufferAccessSignature",
    "ToolConfigurationError",
    "ToolExecutionError",
    "ToolchainRoutingError",
    "WorkspaceWatchError",
    "WorkspaceWatchConflictError",
    "build_candidate_reflection_request",
    "compare_shader_abi",
    "parse_shader_abi",
    "select_unique_shader_for_use",
    "validate_candidate_reflection_response",
    "detect_shader_file",
    "detect_shader_format",
    "generate_official_toolchain_configs",
    "extract_target_profile",
    "validate_target_profile",
    "classify_shader_failure",
    "build_shader_flow_graph",
    "load_workspace_flow_graph_status",
    "validate_shader_flow_graph",
    "extract_dxil_cbuffer_access_signature",
    "extract_dxil_unannotated_cbuffer_bindings",
]
