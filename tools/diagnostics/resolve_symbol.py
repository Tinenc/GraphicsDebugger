import ctypes
from ctypes import wintypes
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

dbghelp = ctypes.windll.dbghelp
kernel32 = ctypes.windll.kernel32

hProcess = kernel32.GetCurrentProcess()

dbghelp.SymSetOptions(0x00000010 | 0x00000004 | 0x00000002) # SYMOPT_LOAD_LINES | SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS
if not dbghelp.SymInitializeW(hProcess, None, False):
    print("SymInitialize failed:", kernel32.GetLastError())
    sys.exit(1)

dll_path = r"C:\Program Files\GraphicsDebugger-Steam-Cod\x64\Development\TinecmaTool.dll"
base_addr = 0x00007FFA70C40000
crash_addr = 0x00007FFA71480E02

mod_base = dbghelp.SymLoadModuleExW(
    hProcess, None, dll_path, None,
    ctypes.c_uint64(base_addr), 0, None, 0
)

print(f"SymLoadModuleExW: mod_base = 0x{mod_base:016X}")

# Define SYMBOL_INFOW structure
class SYMBOL_INFOW(ctypes.Structure):
    _fields_ = [
        ("SizeOfStruct", wintypes.ULONG),
        ("TypeIndex", wintypes.ULONG),
        ("Reserved", ctypes.c_uint64 * 2),
        ("Index", wintypes.ULONG),
        ("Size", wintypes.ULONG),
        ("ModBase", ctypes.c_uint64),
        ("Flags", wintypes.ULONG),
        ("Value", ctypes.c_uint64),
        ("Address", ctypes.c_uint64),
        ("Register", wintypes.ULONG),
        ("Scope", wintypes.ULONG),
        ("Tag", wintypes.ULONG),
        ("NameLen", wintypes.ULONG),
        ("MaxNameLen", wintypes.ULONG),
        ("Name", wintypes.WCHAR * 512),
    ]

sym_info = SYMBOL_INFOW()
sym_info.SizeOfStruct = ctypes.sizeof(SYMBOL_INFOW)
sym_info.MaxNameLen = 512

displacement = ctypes.c_uint64()
if dbghelp.SymFromAddrW(hProcess, ctypes.c_uint64(crash_addr), ctypes.byref(displacement), ctypes.byref(sym_info)):
    print(f"Symbol at 0x{crash_addr:016X}: {sym_info.Name} + 0x{displacement.value:X}")
else:
    print(f"SymFromAddrW failed for 0x{crash_addr:016X}: {kernel32.GetLastError()}")

# Also check source line
class IMAGEHLP_LINEW64(ctypes.Structure):
    _fields_ = [
        ("SizeOfStruct", wintypes.DWORD),
        ("Key", ctypes.c_void_p),
        ("LineNumber", wintypes.DWORD),
        ("FileName", wintypes.LPWSTR),
        ("Address", ctypes.c_uint64),
    ]

line_info = IMAGEHLP_LINEW64()
line_info.SizeOfStruct = ctypes.sizeof(IMAGEHLP_LINEW64)
disp_line = wintypes.DWORD()
if dbghelp.SymGetLineFromAddrW64(hProcess, ctypes.c_uint64(crash_addr), ctypes.byref(disp_line), ctypes.byref(line_info)):
    print(f"Source file: {line_info.FileName}:{line_info.LineNumber} (+0x{disp_line.value:X})")
else:
    print(f"SymGetLineFromAddrW64 failed: {kernel32.GetLastError()}")
