"""
Run this directly: python wa_debug.py
Keep WhatsApp open before running.
It will print exactly what psutil and win32 see.
"""
import psutil
import win32gui
import win32process

print("\n=== PSUTIL: all processes with 'whats' in name ===")
for p in psutil.process_iter(["pid", "name"]):
    try:
        if "whats" in p.info["name"].lower():
            print(f"  PID={p.info['pid']}  name='{p.info['name']}'")
    except Exception:
        pass

print("\n=== WIN32: all visible windows owned by whatsapp PID ===")
wa_pids = set()
for p in psutil.process_iter(["pid", "name"]):
    try:
        if "whats" in p.info["name"].lower():
            wa_pids.add(p.info["pid"])
    except Exception:
        pass

print(f"  WhatsApp PIDs found: {wa_pids}")

def _cb(hwnd, _):
    if not win32gui.IsWindowVisible(hwnd):
        return
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        title  = win32gui.GetWindowText(hwnd).strip()
        if pid in wa_pids and title:
            print(f"  HWND={hwnd}  PID={pid}  title='{title}'")
    except Exception:
        pass

win32gui.EnumWindows(_cb, None)

print("\n=== ALL visible windows containing 'whats' (any process) ===")
def _cb2(hwnd, _):
    if not win32gui.IsWindowVisible(hwnd):
        return
    title = win32gui.GetWindowText(hwnd).strip()
    if "whats" in title.lower():
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pname  = psutil.Process(pid).name()
        except Exception:
            pname = "?"
        print(f"  HWND={hwnd}  PID={pid}  proc='{pname}'  title='{title}'")

win32gui.EnumWindows(_cb2, None)
print("\nDone. Paste output back.")