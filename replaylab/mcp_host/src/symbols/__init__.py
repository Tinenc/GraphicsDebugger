"""Deterministic asset/capture symbol matching for RenderDoc MCP.

The public functions in this package intentionally keep inference separate from
verification.  Exact hashes and ABI-compatible layouts may be applied
automatically; heuristic or AI supplied decisions remain provisional until the
same validators accept them.
"""

from .service import SymbolService

__all__ = ["SymbolService"]
