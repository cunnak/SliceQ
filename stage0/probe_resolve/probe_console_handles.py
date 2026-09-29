"""精确模拟「双击 exe 启动的 GUI 进程」，测 ffmpeg 会不会被新建控制台窗口。

为什么需要这一版
----------------
前两版探针都失败了，原因是同一个：
  · 从 bash 启动的 python.exe → StdOut 句柄有效（0xb4）
  · 用 pythonw.exe 启动      → StdOut 句柄**仍然有效**（0x2d0）
    —— 因为 bash 总会把子进程的 std 句柄接成管道。

而**双击 exe 启动的 GUI 进程，标准句柄是 NULL/INVALID**。
这个差别可能正是"弹黑框"的分水岭，所以必须精确模拟。

模拟方法
--------
`SetStdHandle(-10/-11/-12, NULL)` 把三个标准句柄记录清成 NULL，
再做子进程继承 —— 这与 Explorer 启动 GUI 进程的实际状态一致。

变量矩阵
--------
  J 句柄=NULL + 不重定向（子进程继承 NULL 句柄）★ 最接近真实场景
  K 句柄=NULL + 不重定向 + CREATE_NO_WINDOW
  L 句柄=NULL + 重定向管道（本项目现状）
  M 句柄=NULL + 重定向管道 + CREATE_NO_WINDOW

判据
----
EnumWindows 找属于该 ffmpeg 进程的 `ConsoleWindowClass` 顶层窗口。
另外记录 **conhost.exe 子进程**是否出现（控制台的宿主进程）。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, r"C:\Users\b1596\WorkBuddy\2026-09-23-13-06-47\SliceQ")
from sliceq import ffmpeg_tools  # noqa: E402

OUT = Path(r"C:\temp\console_handle_result.json")
OUT.parent.mkdir(parents=True, exist_ok=True)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32.GetConsoleWindow.restype = wintypes.HWND
kernel32.GetStdHandle.restype = wintypes.HANDLE
kernel32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
kernel32.SetStdHandle.restype = wintypes.BOOL

INVALID = 0xFFFFFFFFFFFFFFFF
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def hstate() -> dict:
    """记录三个标准句柄的状态。NULL 时 GetStdHandle 返回 None。"""
    r = {}
    for n, c in (("stdin", -10), ("stdout", -11), ("stderr", -12)):
        h = kernel32.GetStdHandle(c)
        if h is None:
            r[n] = "NULL"
            continue
        hv = int(h) & INVALID
        r[n] = "NULL" if hv in (0, INVALID) else f"0x{hv:x}"
    return r


def windows_of(pid: int) -> list[dict]:
    found: list[dict] = []

    def cb(hwnd, _l):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            t = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, t, 512)
            found.append({"class": cls.value,
                          "visible": bool(user32.IsWindowVisible(hwnd)),
                          "title": t.value})
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return found


def all_console_windows() -> list[dict]:
    """系统里**所有** ConsoleWindowClass 顶层窗口（不限 PID）。"""
    out: list[dict] = []

    def cb(hwnd, _l):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value == "ConsoleWindowClass":
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            t = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, t, 512)
            out.append({"pid": wpid.value,
                        "visible": bool(user32.IsWindowVisible(hwnd)),
                        "title": t.value})
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return out


def conhost_children_of(pid: int) -> list[int]:
    """该进程的子进程里有没有 conhost.exe（控制台宿主）。"""
    import subprocess as sp
    r = sp.run(["wmic", "process", "where", f"ParentProcessId={pid}",
                "get", "ProcessId,Name", "/format:csv"],
               capture_output=True, text=True, encoding="utf-8",
               errors="replace")
    kids = []
    for ln in (r.stdout or "").splitlines():
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) >= 3 and "conhost" in parts[1].lower():
            kids.append(parts[2])
    return kids


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def spawn(label: str, *, redirect: bool, no_window: bool) -> dict:
    ff = ffmpeg_tools.find_ffmpeg()
    if not ff:
        return {"label": label, "error": "找不到 ffmpeg"}
    out = Path(os.environ["TEMP"]) / f"_ch_{label}.mp4"
    cmd = [str(ff), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "lavfi", "-i", "testsrc=duration=12:size=320x240:rate=10",
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
           "-t", "12", "-shortest", str(out)]
    kw: dict = {}
    if no_window:
        kw["creationflags"] = CREATE_NO_WINDOW
    if redirect:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p = subprocess.Popen(cmd, **kw)
    time.sleep(2.0)
    wins = windows_of(p.pid)
    cons = [w for w in wins if w["class"] == "ConsoleWindowClass"]
    system_cons = all_console_windows()
    p.kill()
    try:
        p.communicate(timeout=5)
    except Exception:
        pass
    out.unlink(missing_ok=True)
    return {
        "label": label, "redirect": redirect, "create_no_window": no_window,
        "该进程的窗口": wins,
        "该进程的控制台窗口": cons,
        "★ 弹出了控制台窗口": bool(cons),
        "同时全系统的控制台窗口数": len(system_cons),
        "全系统明细": system_cons[:6],
    }


result: dict = {
    "解释器": sys.executable,
    "句柄_清空前": hstate(),
}

# ★ 清空三个标准句柄 —— 模拟 Explorer 启动的 GUI 进程
for code in (-10, -11, -12):
    kernel32.SetStdHandle(code, None)
time.sleep(0.3)
result["句柄_清空后"] = hstate()
result["有控制台吗"] = bool(kernel32.GetConsoleWindow())
result["清空前全系统控制台窗口"] = all_console_windows()[:6]

result["cases"] = []
for label, redirect, nw in (
    ("J_句柄NULL_裸调用", False, False),
    ("K_句柄NULL_裸调用+NO_WINDOW", False, True),
    ("L_句柄NULL_管道(现状)", True, False),
    ("M_句柄NULL_管道+NO_WINDOW", True, True),
):
    try:
        result["cases"].append(spawn(label, redirect=redirect, no_window=nw))
    except Exception as exc:                       # noqa: BLE001
        result["cases"].append({"label": label,
                                "错误": f"{type(exc).__name__}: {exc}"})

OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2),
               encoding="utf-8")
