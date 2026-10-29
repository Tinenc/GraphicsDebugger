import ctypes
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
user32 = ctypes.windll.user32

hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
if hdesk:
    user32.SetThreadDesktop(hdesk)

hwnd = 0x01B61AFA
print("Testing HWND:", hex(hwnd))

def enum_child_cb(child, lparam):
    cls_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(child, cls_buf, 256)
    length = user32.GetWindowTextLengthW(child)
    buff = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(child, buff, length + 1)
    ctrl_id = user32.GetDlgCtrlID(child)
    vis = user32.IsWindowVisible(child)
    print(f"  Child HWND: 0x{child:08X} ID: {ctrl_id:6d} Vis:{vis} Class: '{cls_buf.value}' Text: '{buff.value}'")
    return True

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
user32.EnumChildWindows(hwnd, WNDENUMPROC(enum_child_cb), 0)
