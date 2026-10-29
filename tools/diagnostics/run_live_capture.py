import os
import sys
import time
import subprocess
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

desktop = r"C:\Users\Administrator\Desktop"
dxgi_log = os.path.join(desktop, "dxgi_proxy.log")
rdoc_log = os.path.join(desktop, "rdoc_debug_cod.log")
trigger_file = os.path.join(desktop, "trigger_capture")
done_file = os.path.join(desktop, "capture_completed.txt")

def set_desktop():
    hdesk = user32.OpenDesktopA(b"Default", 0, False, 0x01FF)
    if hdesk:
        user32.SetThreadDesktop(hdesk)
    return hdesk

def dismiss_popups():
    # Kill PickerHost if running (firewall popup)
    subprocess.run(["powershell", "-NoProfile", "-Command", "Stop-Process -Name PickerHost -Force -ErrorAction SilentlyContinue"], capture_output=True)

    hdesk = set_desktop()
    # Check for Safe Mode dialog
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value
        if "安全模式" in title or "Safe Mode" in title:
            print(f"[Automation] Found Safe Mode dialog: {title} (HWND {hwnd})")
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.1)
            # Send 'N' key (0x4E) for No
            user32.keybd_event(0x4E, 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(0x4E, 0, 2, 0)
            print("[Automation] Sent 'N' to dismiss Safe Mode")
        return True

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumDesktopWindows(hdesk, WNDPROC(cb), 0)

def find_cod_hwnd():
    hdesk = set_desktop()
    cod_hwnd = []
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value
        if "Call of Duty" in title:
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 500 and h > 500:
                cod_hwnd.append((hwnd, title, w, h))
        return True

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumDesktopWindows(hdesk, WNDPROC(cb), 0)
    return cod_hwnd

def activate_and_interact(hwnd):
    hdesk = set_desktop()
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.1)
    # Click center
    user32.SetCursorPos(1720, 720)
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0) # DOWN
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0) # UP
    time.sleep(0.1)
    # Press Space
    user32.keybd_event(0x20, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(0x20, 0, 2, 0)

print("=" * 60)
print("  Call of Duty HQ - TinecmaTool Live Capture Automation")
print("=" * 60)

# 1. Clean previous triggers & files
for f in [trigger_file, done_file]:
    if os.path.exists(f):
        try: os.remove(f)
        except: pass

# 2. Launch game via Steam URI
print("[1] Launching Call of Duty HQ via Steam (AppID 1938090)...")
subprocess.run(["cmd.exe", "/c", "start steam://rungameid/1938090"], shell=True)

# 3. Monitor startup
cod_pid = None
cod_started_time = None
d3d12_ready = False
trigger_sent = False

print("[2] Waiting for cod.exe to start and initialize D3D12...")
start_wait = time.time()
while time.time() - start_wait < 180:
    time.sleep(2)
    dismiss_popups()

    # Check process
    if not cod_pid:
        res = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Process cod -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"], capture_output=True, text=True)
        out = res.stdout.strip()
        if out and out.isdigit():
            cod_pid = int(out)
            cod_started_time = time.time()
            print(f"[2.1] Detected cod.exe (PID {cod_pid})!")

    if cod_pid:
        # Check dxgi_proxy.log for D3D12 init
        if os.path.exists(dxgi_log):
            try:
                with open(dxgi_log, "r", encoding="utf-8", errors="ignore") as f:
                    log_text = f.read()
                    if "Routing D3D12CreateDevice to TinecmaTool" in log_text and "Init Complete" in log_text:
                        d3d12_ready = True
            except:
                pass

        windows = find_cod_hwnd()
        if d3d12_ready and windows:
            hwnd, title, w, h = windows[0]
            print(f"[2.2] COD D3D12 renderer active! HWND: {hwnd} ({w}x{h})")
            # Dismiss any firewall
            dismiss_popups()
            time.sleep(2)
            activate_and_interact(hwnd)
            break

if not d3d12_ready:
    print("[ERROR] Timed out waiting for COD D3D12 initialization.")
    sys.exit(1)

print("[3] Arming trigger for frame capture...")
# Wait a moment for rendering to stabilize on splash screen
for i in range(5):
    dismiss_popups()
    time.sleep(1)

# Write trigger file
print(f"[3.1] Creating trigger file: {trigger_file}")
with open(trigger_file, "w") as f:
    f.write("CAPTURE\n")

# Interact to prompt Present()
windows = find_cod_hwnd()
if windows:
    hwnd = windows[0][0]
    print(f"[3.2] Interacting with COD window {hwnd} to trigger Present()...")
    for _ in range(3):
        activate_and_interact(hwnd)
        time.sleep(0.5)

print("[4] Monitoring frame capture and disk serialization...")
capture_start = time.time()
capture_file = None
last_log_line = ""

while time.time() - capture_start < 240:
    dismiss_popups()
    time.sleep(2)

    # Check if capture completed signal exists
    if os.path.exists(done_file):
        try:
            with open(done_file, "r") as f:
                capture_file = f.read().strip()
            print(f"\n[4.1] >>> Capture completed signal received! File: {capture_file}")
            break
        except:
            pass

    # Check rdoc_debug_cod.log for progress
    if os.path.exists(rdoc_log):
        try:
            with open(rdoc_log, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                for line in reversed(lines[-20:]):
                    line_s = line.strip()
                    if ("Serialising" in line_s or "Written to disk" in line_s or "Finished capture" in line_s or "Flushing" in line_s) and line_s != last_log_line:
                        print(f"    [RDOC] {line_s}")
                        last_log_line = line_s
                        break
                    if "Written to disk" in line_s:
                        # Extract filename
                        parts = line_s.split("Written to disk:")
                        if len(parts) > 1:
                            capture_file = parts[1].strip()
                            break
        except:
            pass

    if capture_file and os.path.exists(capture_file):
        # Verify file size is not zero
        sz = os.path.getsize(capture_file)
        if sz > 100000:
            print(f"\n[4.2] Verified capture file exists on disk: {capture_file} (Size: {sz:,} bytes)")
            break

    # Look for any new COD_RDC_cod_frame*.rdc with size > 1MB
    for item in os.listdir(desktop):
        if item.startswith("COD_RDC_cod") and item.endswith(".rdc"):
            full_p = os.path.join(desktop, item)
            try:
                sz = os.path.getsize(full_p)
                if sz > 1024 * 1024: # > 1MB
                    print(f"\n[4.3] Detected complete RDC capture: {full_p} (Size: {sz:,} bytes)")
                    capture_file = full_p
                    break
            except:
                pass
    if capture_file:
        break

if not capture_file:
    print("[ERROR] Timed out waiting for capture file generation.")
    sys.exit(1)

print("\n" + "=" * 60)
print(f"[5] Capture Successful! Output file: {capture_file}")
print("=" * 60)

# 5. Extract thumbnail using TinecmaToolcmd.exe
thumb_out = os.path.join(desktop, "COD_thumb_final.png")
cmd = [
    r"C:\Program Files\GraphicsDebugger-Steam-Cod\x64\Development\TinecmaToolcmd.exe",
    "thumb",
    f"--out={thumb_out}",
    capture_file
]
print(f"[6] Extracting thumbnail with TinecmaToolcmd: {' '.join(cmd)}")
res = subprocess.run(cmd, capture_output=True, text=True)
print("Stdout:", res.stdout)
print("Stderr:", res.stderr)

if os.path.exists(thumb_out) and os.path.getsize(thumb_out) > 0:
    print(f"[SUCCESS] Thumbnail extracted successfully to: {thumb_out} ({os.path.getsize(thumb_out)} bytes)")
else:
    print("[NOTE] Thumbnail extraction returned code:", res.returncode)
