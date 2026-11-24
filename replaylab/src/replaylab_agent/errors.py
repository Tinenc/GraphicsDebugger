"""Small public error taxonomy for ReplayLab AgentHost."""


class ReplayLabAIError(RuntimeError):
    """Base error suitable for display in the embedded assistant panel."""


class ConfigurationError(ReplayLabAIError):
    """A provider profile or local executable is invalid."""


class UnknownModelContextError(ConfigurationError):
    """Optional context metadata is absent; provider requests remain usable."""


class ProviderCapabilityError(ConfigurationError):
    """A provider lacks one required, non-retryable protocol capability."""

    retryable = False

    def __init__(self, message: str, *, error_code: str, capability: str) -> None:
        if not error_code or not capability:
            raise ValueError("provider capability diagnostics require stable identifiers")
        super().__init__(message)
        self.error_code = error_code
        self.capability = capability


class ConfigurationConflictError(ConfigurationError):
    """Optimistic configuration state changed before an atomic mutation."""


class UnsafeConfigurationMutationError(ConfigurationError):
    """A requested configuration write crossed the metadata boundary."""


class AuthenticationError(ReplayLabAIError):
    """A required credential is unavailable or rejected."""


class ConnectorError(ReplayLabAIError):
    """A model or external-agent connector failed."""


class ProviderRequestError(ConnectorError):
    """A classified provider failure; retry policy remains with its connector."""

    def __init__(self, message: str, *, error_code: str, retryable: bool = False,
                 retry_after_seconds: float | None = None) -> None:
        if not error_code:
            raise ValueError("provider failures require a stable error code")
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class ProviderIncidentError(ConnectorError):
    """A retryable provider transport incident interrupted an Agent turn.

    ``partial_text`` contains only assistant text emitted before the provider
    diagnostic.  The diagnostic itself is deliberately represented by the
    stable ``incident_code`` rather than being folded into the answer stream.
    """

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        incident_code: str,
        partial_text: str = "",
        in_flight_tools: tuple[str, ...] = (),
    ) -> None:
        if not incident_code:
            raise ValueError("provider incidents require a stable code")
        super().__init__(message)
        self.incident_code = incident_code
        self.partial_text = str(partial_text or "")
        self.in_flight_tools = tuple(
            dict.fromkeys(str(name) for name in in_flight_tools if str(name).strip())
        )


class ContextOverflowError(ConnectorError):
    """Immutable request data cannot fit in the selected context window."""


class LifecycleCleanupError(ConnectorError):
    """An owned child transport may still be alive after cleanup."""


class BridgeError(ReplayLabAIError):
    """The qrenderdoc IPC bridge rejected or timed out a request."""


class PolicyError(ReplayLabAIError):
    """A tool or permission request exceeded its deterministic scope."""


__all__ = [
    "AuthenticationError",
    "BridgeError",
    "ConfigurationConflictError",
    "ConfigurationError",
    "UnknownModelContextError",
    "ConnectorError",
    "ContextOverflowError",
    "LifecycleCleanupError",
    "PolicyError",
    "ProviderIncidentError",
    "ProviderRequestError",
    "ProviderCapabilityError",
    "ReplayLabAIError",
    "UnsafeConfigurationMutationError",
]
