"""Load UEX's bundled codecs and exercise native zlib without a game or network."""
import ctypes
from pathlib import Path
import sys
import zlib


def verify(root: Path):
    for name, symbol in (("Detex.dll", "detexDecompressTextureLinear"),
                         ("oodle-data-shared.dll", "OodleLZ_Decompress")):
        library = ctypes.CDLL(str(root / name))
        getattr(library, symbol)
    library = ctypes.CDLL(str(root / "zlib-ng2.dll"))
    native = getattr(library, "zng_uncompress", None)
    length_type = ctypes.c_size_t
    if native is None:
        native = library.uncompress
        length_type = ctypes.c_ulong
    original = b"Bread UEX offline native codec roundtrip\n" * 20
    compressed = zlib.compress(original)
    output = ctypes.create_string_buffer(len(original))
    source = ctypes.create_string_buffer(compressed)
    size = length_type(len(original))
    native.argtypes = [ctypes.c_void_p, ctypes.POINTER(length_type), ctypes.c_void_p, length_type]
    native.restype = ctypes.c_int
    result = native(output, ctypes.byref(size), source, len(compressed))
    if result != 0 or output.raw[:size.value] != original:
        raise ValueError("UEX's bundled native zlib failed a real decompression roundtrip")
    print("UEX native codec loading and zlib roundtrip passed")


if __name__ == "__main__":
    verify(Path(sys.argv[1]).resolve())
