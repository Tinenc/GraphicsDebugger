"""AI connector factories.

Connector modules keep optional SDK imports lazy so profile management and the
connection wizard remain usable without installing every provider extra.
"""

from .langchain_models import build_chat_model

__all__ = ["build_chat_model"]
