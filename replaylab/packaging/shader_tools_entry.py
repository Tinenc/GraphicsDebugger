"""Independent, fixed shader adapters; deliberately no Host/service imports."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments[0] if arguments else ""
    if command == "adapter-hlsl":
        from adapters.hlsl_decompiler_adapter import main as adapter
    elif command == "adapter-dxbc-assembly":
        from adapters.dxbc_assembly_adapter import main as adapter
    elif command == "adapter-d3dcompile":
        from adapters.d3dcompiler_adapter import main as adapter
    elif command == "adapter-dxil-assemble":
        from adapters.dxil_assembler_adapter import main as adapter
    elif command == "adapter-spirv-cross":
        from adapters.spirv_cross_adapter import main as adapter
    elif command == "adapter-renodx":
        from adapters.renodx_decompiler_adapter import main as adapter
    else:
        parser = argparse.ArgumentParser(description="Bread fixed shader tools")
        parser.add_argument("command", choices=(
            "adapter-hlsl", "adapter-dxbc-assembly", "adapter-d3dcompile",
            "adapter-dxil-assemble", "adapter-spirv-cross", "adapter-renodx",
        ))
        parser.parse_args(arguments)
        return 2
    return int(adapter(arguments[1:]))


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")
    raise SystemExit(main())
