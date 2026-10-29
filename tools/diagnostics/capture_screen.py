import ctypes, struct, os, subprocess
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

hdesk = user32.OpenDesktopA(b"Default", 0, False, 0x01FF)
if hdesk:
    user32.SetThreadDesktop(hdesk)

cod_hwnd = None
def cb(h, _):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    if pid.value == 28784 and user32.IsWindowVisible(h):
        rect = wintypes.RECT()
        user32.GetWindowRect(h, ctypes.byref(rect))
        w = rect.right - rect.left
        h_len = rect.bottom - rect.top
        if w > 300 and h_len > 300:
            global cod_hwnd
            cod_hwnd = h
            print(f"Found COD Window HWND {h} (PID {pid.value}), size: {w}x{h_len}")
            return False
    return True

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
user32.EnumDesktopWindows(hdesk, WNDPROC(cb), 0)

if not cod_hwnd:
    print("Could not find COD window!")
    exit(1)

hwnd = cod_hwnd
rect = wintypes.RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
w = rect.right - rect.left
h = rect.bottom - rect.top

hdc_screen = user32.GetDC(0)
hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
hbm = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
gdi32.SelectObject(hdc_mem, hbm)
gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, rect.left, rect.top, 0x00CC0020)

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)
    ]

bmi = BITMAPINFOHEADER()
bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
bmi.biWidth = w
bmi.biHeight = -h
bmi.biPlanes = 1
bmi.biBitCount = 32
bmi.biCompression = 0

buf = ctypes.create_string_buffer(w * h * 4)
gdi32.GetDIBits(hdc_mem, hbm, 0, h, buf, ctypes.byref(bmi), 0)

bmp_header = struct.pack("<2sIHHI", b"BM", 54 + w * h * 4, 0, 0, 54)
bmi_header = struct.pack("<IiiHHIIIIII", 40, w, -h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)

out_bmp = r"C:\Users\Administrator\Desktop\cod_live_screen.bmp"
out_jpg = r"C:\Users\Administrator\.gemini\antigravity\brain\2eae6a80-3a78-444f-b06b-6539db3c27a5\cod_live_screen.jpg"

with open(out_bmp, "wb") as f:
    f.write(bmp_header)
    f.write(bmi_header)
    f.write(buf.raw)
print("Saved bmp to", out_bmp)

# Convert to jpg
subprocess.run(["ffmpeg", "-y", "-i", out_bmp, "-q:v", "3", out_jpg], capture_output=True)
print("Converted to jpg at", out_jpg)
