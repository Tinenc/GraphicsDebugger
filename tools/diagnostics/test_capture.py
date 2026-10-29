import os
import sys
import time
import glob
import subprocess
import ctypes
from ctypes import wintypes

DESKTOP = r"C:\Users\Administrator\Desktop"
PROXY_LOG = os.path.join(DESKTOP, "dxgi_proxy.log")
DONE_FILE = os.path.join(DESKTOP, "capture_completed.txt")
TINECMATOOL_CMD = r"C:\Program Files\GraphicsDebugger-Steam-Cod\x64\Development\TinecmaToolcmd.exe"

def dismiss_dialogs():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    BM_CLICK = 0x00F5
    WM_CLOSE = 0x0010

    def enum_windows_callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

            if "安全模式" in title or "Safe Mode" in title:
                print(f"    [Dialog Auto-Dismiss] Handling '{title}'...", flush=True)
                def enum_child_callback(child_hwnd, _):
                    c_len = user32.GetWindowTextLengthW(child_hwnd)
                    if c_len > 0:
                        c_buf = ctypes.create_unicode_buffer(c_len + 1)
                        user32.GetWindowTextW(child_hwnd, c_buf, c_len + 1)
                        text = c_buf.value
                        if "否" in text or "No" in text or "Cancel" in text:
                            print(f"    [Dialog Auto-Dismiss] Clicking '{text}' on dialog '{title}'...", flush=True)
                            user32.SendMessageW(child_hwnd, BM_CLICK, 0, 0)
                            return False
                    return True
                user32.EnumChildWindows(hwnd, WNDENUMPROC(enum_child_callback), 0)

            elif "错误" in title or "Error" in title or "Crash" in title or "Report" in title:
                print(f"    [Dialog Auto-Dismiss] Dismissing '{title}'...", flush=True)
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

        return True

    user32.EnumWindows(WNDENUMPROC(enum_windows_callback), 0)

def get_running_processes():
    cmd = ["powershell", "-Command", "Get-Process -Name 'cod', 'sp26-cod', 'bootstrapper', 'steam' -ErrorAction SilentlyContinue | Select-Object -Property Id, ProcessName | ConvertTo-Json"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if not res.stdout.strip():
        return {}
    import json
    try:
        data = json.loads(res.stdout)
        if isinstance(data, dict):
            data = [data]
        return {f"{item['ProcessName'].lower()}.exe": str(item['Id']) for item in data}
    except Exception:
        return {}

def print_log_tail(log_path, lines=20):
    if not os.path.exists(log_path):
        print(f"    [!] Log file not found: {log_path}", flush=True)
        return
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) >= lines else all_lines
        for line in tail:
            print(f"    {line.rstrip()}", flush=True)
    except Exception as e:
        print(f"    [!] Failed to read log: {e}", flush=True)

def main():
    print("==================================================", flush=True)
    print("  Call of Duty D3D12 RenderDoc / PIX Capture Runner", flush=True)
    print("==================================================", flush=True)

    # 1. Clean previous triggers & completion flags
    for f in glob.glob(os.path.join(DESKTOP, "trigger_capture*")) + [DONE_FILE]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass

    # 2. Check running processes
    procs = get_running_processes()
    print("[1] Current relevant processes:", flush=True)
    for p in ["steam.exe", "cod.exe", "sp26-cod.exe", "bootstrapper.exe"]:
        print(f"    - {p}: {'RUNNING (PID ' + procs[p] + ')' if p in procs else 'NOT running'}", flush=True)

    if "steam.exe" not in procs:
        print("[!] Steam is not running! Launching Steam via interactive scheduled task...", flush=True)
        subprocess.run('schtasks /create /tn LaunchSteamInteractive /tr "C:\\PROGRA~2\\Steam\\steam.exe" /sc once /st 00:00 /ru Administrator /it /f', shell=True, check=True)
        subprocess.run('schtasks /run /tn LaunchSteamInteractive', shell=True, check=True)
        subprocess.run('schtasks /delete /tn LaunchSteamInteractive /f', shell=True, check=True)
        time.sleep(10)

    # 3. Launch game via Steam if not already running
    t0 = time.time()
    if "cod.exe" not in procs and "sp26-cod.exe" not in procs:
        print("\n[2] Launching Call of Duty HQ (AppID 1938090) via Steam command...", flush=True)
        subprocess.Popen([r"C:\Program Files (x86)\Steam\steam.exe", "-applaunch", "1938090"])
    else:
        print("\n[2] Game process already running, observing...", flush=True)

    # 4. Wait for game renderer (cod.exe / sp26-cod.exe)
    print("\n[3] Monitoring game startup for renderer (up to 120s)...", flush=True)
    seen = {}
    target_proc = None
    target_pid = None

    while time.time() - t0 < 120:
        dismiss_dialogs()
        procs = get_running_processes()
        for k in ["bootstrapper.exe", "cod.exe", "sp26-cod.exe"]:
            if k in procs and k not in seen:
                seen[k] = time.time()
                print(f"    [+{int(time.time() - t0)}s] Detected {k} (PID {procs[k]})", flush=True)

        if "cod.exe" in procs and (time.time() - seen.get("cod.exe", time.time()) > 30):
            target_proc = "cod.exe"
            target_pid = procs["cod.exe"]
            print(f"    >>> cod.exe is live and rendering (PID {target_pid})", flush=True)
            break
        elif "sp26-cod.exe" in procs and (time.time() - seen.get("sp26-cod.exe", time.time()) > 30):
            target_proc = "sp26-cod.exe"
            target_pid = procs["sp26-cod.exe"]
            print(f"    >>> sp26-cod.exe is live (PID {target_pid})", flush=True)
            break

        time.sleep(2)

    if not target_pid:
        procs = get_running_processes()
        for k in ["cod.exe", "sp26-cod.exe"]:
            if k in procs:
                target_proc = k
                target_pid = procs[k]
                break
        if not target_pid:
            print("[!] No game process found within 120s! Aborting.", flush=True)
            return 1

    # 5. Allow D3D12 device & pipeline state to stabilize
    print(f"\n[4] Active target process: {target_proc} (PID {target_pid}).", flush=True)
    print("    Waiting 50 seconds for full D3D12 pipeline initialization...", flush=True)
    for i in range(50):
        time.sleep(1)
        if (i + 1) % 5 == 0:
            dismiss_dialogs()
        if (i + 1) % 15 == 0:
            procs = get_running_processes()
            cur = [f"{k}:{procs[k]}" for k in ["cod.exe", "sp26-cod.exe"] if k in procs]
            print(f"    [+{i+1}s] Alive processes: {', '.join(cur)}", flush=True)

    # 6. Check proxy log status before trigger
    print("\n[5] Current dxgi_proxy.log status:", flush=True)
    print_log_tail(PROXY_LOG, 25)

    # 7. Issue capture triggers to all active renderers
    print("\n[6] Issuing GPU frame capture request...", flush=True)
    trigger_generic = os.path.join(DESKTOP, "trigger_capture")
    trigger_cod = os.path.join(DESKTOP, "trigger_capture_cod")
    trigger_sp26 = os.path.join(DESKTOP, "trigger_capture_sp26-cod")

    for tf in [trigger_generic, trigger_cod, trigger_sp26]:
        with open(tf, "w") as f:
            f.write("capture")
    print(f"    - Created trigger files: generic, cod, and sp26-cod", flush=True)

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    VK_F12 = 0x7B
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(VK_F12, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_F12, 0, KEYEVENTF_KEYUP, 0)
    print("    - Sent F12 key press event", flush=True)

    # 8. Watch Desktop and real-time logs for capture completion
    print("\n[7] Watching Desktop & real-time log for capture serialization...", flush=True)
    t1 = time.time()
    capture_found = None
    last_activity_time = time.time()

    rdoc_log = os.path.join(DESKTOP, "rdoc_debug_cod.log")
    rdoc_pos = 0
    if os.path.exists(rdoc_log):
        rdoc_pos = os.path.getsize(rdoc_log)

    max_wait = 240  # up to 4 minutes
    while time.time() - t1 < max_wait:
        # Check completion file
        if os.path.exists(DONE_FILE):
            try:
                with open(DONE_FILE, "r", encoding="utf-8") as f:
                    cap_path = f.read().strip()
                if cap_path and os.path.exists(cap_path):
                    print(f"\n    >>> CAPTURE COMPLETED VIA PROXY API: {cap_path}", flush=True)
                    capture_found = cap_path
                    break
            except Exception:
                pass

        # Stream new log lines from rdoc_debug_cod.log
        if os.path.exists(rdoc_log):
            try:
                cur_sz = os.path.getsize(rdoc_log)
                if cur_sz > rdoc_pos:
                    with open(rdoc_log, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(rdoc_pos)
                        new_content = f.read()
                        rdoc_pos = f.tell()
                    for line in new_content.splitlines():
                        if any(k in line for k in ["Finished capture", "Serialising", "Checking", "Flushing", "Captured D3D12", "Finishing write", "Chunk estimated"]):
                            print(f"    [RDoc Log] {line.strip()}", flush=True)
                            last_activity_time = time.time()
            except Exception:
                pass

        # Check for RDC files that have grown large and stabilized
        rdc_files = glob.glob(os.path.join(DESKTOP, "COD_RDC_*.rdc"))
        if rdc_files:
            rdc_files.sort(key=os.path.getmtime, reverse=True)
            newest = rdc_files[0]
            if os.path.getmtime(newest) >= t0:
                cur_sz = os.path.getsize(newest)
                if cur_sz > 200 * 1024:  # Bigger than thumbnail header
                    last_sz = cur_sz
                    stable_cnt = 0
                    for _ in range(15):
                        time.sleep(1)
                        now_sz = os.path.getsize(newest)
                        if now_sz == last_sz and now_sz > 0:
                            stable_cnt += 1
                            if stable_cnt >= 4:
                                capture_found = newest
                                break
                        else:
                            stable_cnt = 0
                        last_sz = now_sz
                    if capture_found:
                        print(f"\n    >>> Capture file stabilized on disk: {capture_found} ({os.path.getsize(capture_found)/(1024*1024):.2f} MB)", flush=True)
                        break

        # Check if game process is alive
        procs = get_running_processes()
        if "cod.exe" not in procs and "sp26-cod.exe" not in procs:
            print("    [!] Game processes exited.", flush=True)
            break

        # If active serialization is printing progress, keep wait alive
        if time.time() - last_activity_time < 15 and (time.time() - t1 > max_wait - 30):
            print("    [Note] Serialization actively progressing, extending wait window by 60s...", flush=True)
            max_wait += 60

        time.sleep(2)

    # 9. Final log review
    print("\n[8] Final dxgi_proxy.log contents:", flush=True)
    print_log_tail(PROXY_LOG, 25)

    if os.path.exists(rdoc_log):
        print(f"\n[8b] RenderDoc internal log ({rdoc_log}) tail:", flush=True)
        print_log_tail(rdoc_log, 25)

    print("\n==================================================", flush=True)
    if capture_found:
        print(f"  [RESULT] Capture SUCCESSFUL! File: {capture_found}", flush=True)
        if capture_found.endswith(".rdc") and os.path.exists(TINECMATOOL_CMD):
            thumb_path = capture_found.replace(".rdc", "_thumb.png")
            print(f"  [Replay Test] Generating thumbnail via TinecmaToolcmd to {thumb_path}...", flush=True)
            res = subprocess.run([TINECMATOOL_CMD, "thumb", f"--out={thumb_path}", capture_found], capture_output=True, text=True)
            if res.returncode == 0 and os.path.exists(thumb_path):
                print(f"  [Replay Test] SUCCESS! Thumbnail generated: {thumb_path} ({os.path.getsize(thumb_path)} bytes)", flush=True)
            else:
                print(f"  [Replay Test] Note: thumb rc={res.returncode}, out={res.stdout}, err={res.stderr}", flush=True)
    else:
        print("  [RESULT] Capture pending or frame not yet presented.", flush=True)
    print("==================================================", flush=True)

    # Clean shutdown
    print("\n[9] Allowing flushes before stopping processes...", flush=True)
    time.sleep(5)
    subprocess.run(["powershell", "-Command", "Stop-Process -Name 'sp26-cod', 'cod' -Force -ErrorAction SilentlyContinue"], capture_output=True)
    print("    Done.", flush=True)
    return 0

if __name__ == "__main__":
    main()
