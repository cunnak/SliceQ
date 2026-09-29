"""用 Win32 事件钩子捕获「一闪而过」的控制台窗口。

为什么还需要这一版
------------------
前面的轮询式探针都是「启动 ffmpeg → 等 2 秒 → 枚举窗口」。
**如果窗口只存在几十毫秒**（典型现象："黑框闪一下"），轮询必然漏掉。

本脚本改用 `SetWinEventHook(EVENT_OBJECT_CREATE)` ——
**事件驱动**，窗口一被创建就会回调，不会漏。而且是系统级的，
所以任何进程新建的控制台窗口都会被记下（附带 PID）。

用法
----
    pythonw.exe probe_console_flash.py
（钩子需要消息循环，所以主线程跑 `GetMessage`，子线程负责启动 ffmpeg。）

结果写 JSON（pythonw 无 stdout）。
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

sys.path.insert(0, r"C:\Users\b1596\WorkBuddy\2026-09-23-13-06-47\SliceQ")
from sliceq import ffmpeg_tools  # noqa: E402

OUT = Path(r"C:\temp\console_flash_result.json")
OUT.parent.mkdir(parents=True, exist_ok=True)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

EVENT_OBJECT_CREATE = 0x8000
WINEVENT_OUTOFCONTEXT = 0x0000
WM_QUIT = 0x0012

# 窗口类名 → 是否"就是控制台"
CONSOLE_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"}

events: list[dict] = []
_lock = threading.Lock()
T0 = time.time()

WinEventProc = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)


def on_event(_hook, _event, hwnd, _idobj, _idchild, _thread, _t):
    if not hwnd:
        return
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    cls = buf.value
    if cls in CONSOLE_CLASSES:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        with _lock:
            events.append({
                "相对时刻": round(time.time() - T0, 3),
                "pid": pid.value,
                "class": cls,
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "title": title.value,
            })


# ── 装钩子（系统级，捕获所有进程）───────────────────────────
_cb = WinEventProc(on_event)
hook = user32.SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_CREATE,
                              None, _cb, 0, 0, WINEVENT_OUTOFCONTEXT)
hook_ok = bool(hook)

MAIN_TID = kernel32.GetCurrentThreadId()
ffmpeg_pids: list[int] = []

# ⚠️ PostThreadMessageW 在 **user32**，不在 kernel32（写错了会 AttributeError）
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]


def quit_loop() -> None:
    user32.PostThreadMessageW(MAIN_TID, WM_QUIT, 0, 0)


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)

# 每一轮：名字 → (creationflags, 额外 kwargs)
ROUNDS: list[tuple[str, int, dict]] = [
    ("① 现状（管道 + 无 flag）", 0, {}),
    ("② 管道 + CREATE_NO_WINDOW", CREATE_NO_WINDOW, {}),
    ("③ 管道 + DETACHED_PROCESS", DETACHED_PROCESS, {}),
    ("④ DEVNULL + 无 flag", 0, {"stdout": subprocess.DEVNULL,
                                "stderr": subprocess.DEVNULL}),
]

windows: list[dict] = []      # 每轮的时间窗，用来归属事件


def worker():
    """子线程：逐轮跑短 ffmpeg 任务，并记录每轮的时间窗。"""
    ff = ffmpeg_tools.find_ffmpeg()
    if not ff:
        quit_loop()
        return
    out = Path(os.environ["TEMP"]) / "_flash_probe.mp4"
    for name, flags, extra in ROUNDS:
        t_start = time.time() - T0
        for _ in range(3):
            cmd = [str(ff), "-hide_banner", "-loglevel", "error",
                   "-nostdin", "-y",
                   "-f", "lavfi",
                   "-i", "testsrc=duration=1.0:size=160x120:rate=8",
                   "-t", "1.0", str(out)]
            kw: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
            kw.update(extra)
            if flags:
                kw["creationflags"] = flags
            p = subprocess.Popen(cmd, **kw)
            ffmpeg_pids.append(p.pid)
            p.wait()
            time.sleep(0.35)
        windows.append({"轮次": name,
                        "起": round(t_start, 3),
                        "止": round(time.time() - T0, 3)})
        time.sleep(0.4)
    out.unlink(missing_ok=True)
    time.sleep(0.6)
    quit_loop()


# 主线程先建出消息队列（否则 PostThreadMessage 可能失败）
_init_msg = wintypes.MSG()
user32.PeekMessageW(ctypes.byref(_init_msg), None, 0, 0, 0)

th = threading.Thread(target=worker, daemon=True)
th.start()

# ── 消息循环（钩子回调靠它投递）─────────────────────────────
msg = wintypes.MSG()
while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))

user32.UnhookWinEvent(hook)

# 把事件按时间窗归属到各轮
per_round: list[dict] = []
for w in windows:
    hits = [e for e in events if w["起"] <= e["相对时刻"] <= w["止"]]
    per_round.append({**w, "触发次数": len(hits),
                      "命中": [e["class"] for e in hits]})

result = {
    "钩子安装成功": hook_ok,
    "解释器": sys.executable,
    "ffmpeg 的 pid": ffmpeg_pids,
    "★ 按轮次统计": per_round,
    "全部事件": events,
    "★ 有没有出现控制台窗口": bool(events),
}
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2),
               encoding="utf-8")
