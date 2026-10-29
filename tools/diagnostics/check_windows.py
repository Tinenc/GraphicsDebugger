import win32gui
import win32process

windows = []
def enum_cb(hwnd, extra):
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    title = win32gui.GetWindowText(hwnd)
    cls = win32gui.GetClassName(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    windows.append((pid, hwnd, title, cls, visible))

win32gui.EnumWindows(enum_cb, None)
for pid, hwnd, title, cls, visible in windows:
    if pid in (35644, 23168) or any(k in title.lower() for k in ['cod', 'call of duty', 'splash', 'error', 'safe']):
        print(f"PID {pid:5d} HWND {hwnd:10d} Vis:{int(visible)} Class: '{cls}' Title: '{title}'")
