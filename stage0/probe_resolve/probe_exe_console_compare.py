"""真机对照：**打包后的 exe** 启动时，屏幕上会不会冒出终端窗口。

为什么还要在 exe 上再验一次
--------------------------
前面的探针都是在 **Python 解释器**里跑（`pythonw.exe` + `subproc`）。
解释器和 PyInstaller 打的 exe 有两处不同：

  · exe 是 onefile，有**引导进程 + 子进程**两层；
  · exe 的 `sys.stdout/stderr` 是 `None`，句柄状态更接近真实 GUI。

所以必须**在真产品上**再验一遍 —— 这符合本项目一贯的"用户实际走的那条路
才算数"。

测什么
------
`SliceQ.exe` 启动时会做**环境体检**（`ffmpeg_tools.probe_environment()`），
它会跑 `ffmpeg -version` 和 `ffmpeg -filters` —— 也就是**启动就会启动 ffmpeg**，
正好是个天然的对照点：

    v0.1.3（未加 CREATE_NO_WINDOW）→ 应当抓到控制台窗口
    v0.1.4（统一走 subproc）        → 应当 0 个

用法：
    python probe_exe_console_compare.py <exe路径> <标签> <输出json>
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

exe_path = Path(sys.argv[1])
label = sys.argv[2] if len(sys.argv) > 2 else exe_path.stem
out_path = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(
    rf"C:\temp\exe_console_{label}.json")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
EVENT_OBJECT_CREATE = 0x8000
WINEVENT_OUTOFCONTEXT = 0x0000
WM_QUIT = 0x0012
CONSOLE_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"}

T0 = time.time()
events: list[dict] = []
_lock = threading.Lock()

WinEventProc = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)


def on_event(_h, _e, hwnd, _a, _b, _c, _d):
    if not hwnd:
        return
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    if buf.value in CONSOLE_CLASSES:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        with _lock:
            events.append({"t": round(time.time() - T0, 3),
                           "pid": pid.value,
                           "class": buf.value,
                           "visible": bool(user32.IsWindowVisible(hwnd))})


_cb = WinEventProc(on_event)
hook = user32.SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_CREATE,
                              None, _cb, 0, 0, WINEVENT_OUTOFCONTEXT)
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
MAIN_TID = kernel32.GetCurrentThreadId()
_init = wintypes.MSG()
user32.PeekMessageW(ctypes.byref(_init), None, 0, 0, 0)

# 隔离的 APPDATA —— 不碰用户的真实数据
sandbox = Path(rf"C:\temp\sq_execheck_{label}")
if sandbox.exists():
    import shutil
    shutil.rmtree(sandbox, ignore_errors=True)
sandbox.mkdir(parents=True, exist_ok=True)

env = dict(os.environ)
env["APPDATA"] = str(sandbox)
env["LOCALAPPDATA"] = str(sandbox)

procs: list[int] = []


def running_pids() -> set[int]:
    """当前所有 SliceQ 进程的 PID（用来区分"我们启动的"和"用户开着的"）。"""
    r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_path.name}",
                        "/FO", "CSV", "/NH"],
                       capture_output=True, text=True, encoding="gbk",
                       errors="replace")
    out = set()
    for ln in (r.stdout or "").splitlines():
        parts = [x.strip('"') for x in ln.split('","')]
        if len(parts) >= 2:
            try:
                out.add(int(parts[1]))
            except ValueError:
                pass
    return out


# ⚠️ 只杀**本次新增**的进程 —— 用户可能自己开着 SliceQ，不能误伤
baseline = running_pids()


def worker():
    p = subprocess.Popen([str(exe_path)], env=env, cwd=str(exe_path.parent))
    procs.append(p.pid)
    # 给足时间：onefile 解压 + 启动 + 后台环境体检（会跑 ffmpeg）
    time.sleep(22)
    for pid in running_pids() - baseline:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True)
    time.sleep(1)
    user32.PostThreadMessageW(MAIN_TID, WM_QUIT, 0, 0)


th = threading.Thread(target=worker, daemon=True)
th.start()
msg = wintypes.MSG()
while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))
user32.UnhookWinEvent(hook)

# 收尾：同样只清本次新增的，不动用户的实例
for pid in running_pids() - baseline:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   capture_output=True)

log = sandbox / "SliceQ" / "logs" / "sliceq.log"
result = {
    "标签": label,
    "exe": str(exe_path),
    "exe 大小": exe_path.stat().st_size if exe_path.exists() else None,
    "钩子安装成功": bool(hook),
    "★ 控制台窗口事件数": len(events),
    "事件明细": events,
    "启动日志": log.read_text(encoding="utf-8", errors="replace")[:600]
                if log.exists() else "（无日志）",
}
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8")
print(f"[{label}] 事件数 = {len(events)}")
