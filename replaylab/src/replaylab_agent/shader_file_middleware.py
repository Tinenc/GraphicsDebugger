"""Keep an explicitly requested, bounded first-party Shader file inline."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from .tool_output import is_complete_shader_file_output


def shader_file_middleware(backend: Any, *, first_party_reader: bool) -> Any:
    from deepagents.middleware.filesystem import FilesystemMiddleware

    class ShaderFileMiddleware(FilesystemMiddleware):
        # Replace the framework's stock instance in-place. All filesystem tools,
        # permission checks and ordinary result eviction remain inherited.
        @property
        def name(self) -> str:
            return "FilesystemMiddleware"

        def _complete_read(self, request: Any) -> bool:
            call = request.tool_call
            name, args = call.get("name"), call.get("args", {})
            if name == "invoke_bread_tool" and isinstance(args, dict):
                name, args = args.get("name"), args.get("arguments", {})
            return bool(first_party_reader and name == "read_shader_workspace_file"
                        and isinstance(args, dict) and args.get("mode") == "complete")

        def wrap_tool_call(self, request: Any, handler: Any) -> Any:
            token = complete_read.set(self._complete_read(request))
            try:
                return super().wrap_tool_call(request, handler)
            finally:
                complete_read.reset(token)

        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            token = complete_read.set(self._complete_read(request))
            try:
                return await super().awrap_tool_call(request, handler)
            finally:
                complete_read.reset(token)

        def _verified(self, message: Any) -> bool:
            return (complete_read.get() and isinstance(message.content, str)
                    and is_complete_shader_file_output("read_shader_workspace_file", message.content))

        def _process_large_message(self, message: Any, resolved_backend: Any) -> Any:
            if self._verified(message):
                return message, False
            return super()._process_large_message(message, resolved_backend)

        async def _aprocess_large_message(self, message: Any, resolved_backend: Any) -> Any:
            if self._verified(message):
                return message, False
            return await super()._aprocess_large_message(message, resolved_backend)

    complete_read: ContextVar[bool] = ContextVar("bread_complete_shader_read", default=False)
    return ShaderFileMiddleware(backend=backend)
