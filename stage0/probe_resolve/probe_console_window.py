"""验证：什么条件下，SliceQ 启动 ffmpeg 会给它弹出（新建）一个控制台窗口。

背景
----
SliceQ 用 `--windowed` 打包（`SliceQ.spec: console=False`）⇒ 主进程**没有控制台**。
`ffmpeg.exe` / `ffprobe.exe` 是 **console 子系统**程序。
Windows CreateProcess 的规则决定了：某些组合下系统会**新建**一个控制台 ⇒ 屏幕弹黑框。

⚠️ 两版探针走过的弯路（都要记住）：
   ① 我原以为「无控制台 + 无 flag」必弹 —— 用普通 python.exe 测，**不弹**；
   ② 再看才发现普通 python.exe 在这个环境里 StdOut 句柄是**有效的**（`0xb4`），
      它根本不是"真·无控制台 GUI 进程"，模拟不成立。
   ⇒ **必须用 `pythonw.exe`**（GUI 子系统解释器）才等于发布版的条件。

用法
----
    <interpreter-dir>/pythonw.exe probe_console_window.py
（用 pythonw 跑，所以**没有任何 stdout**，结果只写 JSON 文件。）

变量矩阵（父进程 = pythonw，真无控制台）
----------------------------------------
  D 裸调用（不重定向，继承父进程句柄）
  E 裸调用 + CREATE_NO_WINDOW
  F 重定向到管道（本项目现在的写法）        ← 对照
  G 重定向 + CREATE_NO_WINDOW
  H DEVNULL
  I DETACHED_PROCESS

判据
----
`EnumWindows` 枚举顶层窗口，找属于该 ffmpeg 进程的 `ConsoleWindowClass`。
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

OUT = Path(r"C:\temp\console_window_result.json")
OUT.parent.mkdir(parents=True, exist_ok=True)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32.GetConsoleWindow.restype = wintypes.HWND

_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
INVALID = 0xFFFFFFFFFFFFFFFF


def windows_of(pid: int) -> list[dict]:
    found: list[dict] = []

    def cb(hwnd, _l):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            found.append({"class": cls.value,
                          "visible": bool(user32.IsWindowVisible(hwnd)),
                          "title": title.value})
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return found


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)


def std_handle_state() -> dict:
    out = {}
    for name, code in (("stdin", -10), ("stdout", -11), ("stderr", -12)):
        h = kernel32.GetStdHandle(code) & INVALID
        out[name] = ("无效" if h == INVALID else f"0x{h:x}")
    return out


def spawn(label: str, mode: str) -> dict:
    """mode: bare | bare_nw | pipe | pipe_nw | null | detached"""
    ff = ffmpeg_tools.find_ffmpeg()
    if not ff:
        return {"label": label, "error": "找不到 ffmpeg"}
    out = Path(os.environ["TEMP"]) / f"_cw_{label}.mp4"
    cmd = [str(ff), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "lavfi", "-i", "testsrc=duration=12:size=320x240:rate=10",
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
           "-t", "12", "-shortest", str(out)]
    kw: dict = {}
    if mode in ("bare_nw", "pipe_nw"):
        kw["creationflags"] = CREATE_NO_WINDOW
    elif mode == "detached":
        kw["creationflags"] = DETACHED_PROCESS
    if mode in ("pipe", "pipe_nw"):
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    elif mode == "null":
        kw.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    p = subprocess.Popen(cmd, **kw)
    time.sleep(2.0)
    wins = windows_of(p.pid)
    cons = [w for w in wins if w["class"] == "ConsoleWindowClass"]
    p.kill()
    try:
        p.communicate(timeout=5)
    except Exception:
        pass
    out.unlink(missing_ok=True)
    return {"label": label, "mode": mode,
            "all_windows": wins, "console_windows": cons,
            "弹出控制台窗口": bool(cons),
            "可见的控制台窗口": any(w["visible"] for w in cons)}


result: dict = {
    "解释器": sys.executable,
    "父进程有控制台吗": bool(kernel32.GetConsoleWindow()),
    "父进程标准句柄": std_handle_state(),
}

result["cases"] = []
for label, mode in (
    ("D_裸调用", "bare"),
    ("E_裸调用+NO_WINDOW", "bare_nw"),
    ("F_管道(现状)", "pipe"),
    ("G_管道+NO_WINDOW", "pipe_nw"),
    ("H_DEVNULL", "null"),
    ("I_DETACHED_PROCESS", "detached"),
):
    try:
        result["cases"].append(spawn(label, mode))
    except Exception as exc:                       # noqa: BLE001
        result["cases"].append({"label": label, "错误": f"{type(exc).__name__}: {exc}"})

OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2),
               encoding="utf-8")
