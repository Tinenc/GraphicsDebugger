"""Typed errors raised by the deterministic shader workspace."""

from __future__ import annotations


class ShaderWorkspaceError(RuntimeError):
    """Base class for user-facing workspace failures."""


class CatalogValidationError(ShaderWorkspaceError):
    """The capture catalog or selected shader record is not trustworthy."""


class CatalogUseNotCoveredError(CatalogValidationError):
    """A valid catalog has no row for the requested event and stage."""


class ToolConfigurationError(ShaderWorkspaceError):
    """The user-owned external tool registry is invalid or changed."""


class ToolExecutionError(ShaderWorkspaceError):
    """An external tool failed, timed out, or produced unusable output."""


class ToolchainRoutingError(ShaderWorkspaceError):
    """No trusted, format-compatible external shader route can be selected."""


class ManifestValidationError(ShaderWorkspaceError):
    """A workspace manifest is missing, corrupt, or internally inconsistent."""


class AbiValidationError(ShaderWorkspaceError):
    """A shader ABI snapshot or replay-reflection result is incomplete or invalid."""


class WorkspaceWatchError(ShaderWorkspaceError):
    """A bounded external-editor save watcher could not be operated safely."""


class WorkspaceWatchConflictError(WorkspaceWatchError):
    """The tracked source no longer matches an editor's expected bytes."""
