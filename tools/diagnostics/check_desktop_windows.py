import ctypes
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

user32 = ctypes.windll.user32

hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
print("OpenDesktopW:", hdesk)
if hdesk:
    user32.SetThreadDesktop(hdesk)

windows = []
def enum_cb(hwnd, lparam):
    cls_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls_buf, 256)
    length = user32.GetWindowTextLengthW(hwnd)
    title = ""
    if length > 0:
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    vis = user32.IsWindowVisible(hwnd)
    windows.append((pid.value, hwnd, cls_buf.value, title, vis))
    return True

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
user32.EnumDesktopWindows(hdesk, WNDENUMPROC(enum_cb), 0)

print(f"Total windows found: {len(windows)}")
for pid, hwnd, cls, title, vis in windows:
    if pid in (35644, 23168) or any(k in title.lower() for k in ['cod', 'call of duty', 'splash', 'error', 'safe', 'battle', 'steam']):
        print(f"PID {pid:5d} HWND 0x{hwnd:08X} Vis:{vis} Class: '{cls}' Title: '{title}'")
