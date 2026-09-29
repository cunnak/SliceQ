"""启动真实的 SliceQ.exe，盯住系统里所有新出现的控制台窗口。

前一轮实验已证明：无控制台父进程启动 ffmpeg **不会**弹黑框（4 种组合全不弹）。
那用户看到的终端是哪来的？本脚本直接对**真实产品**取证：

  1. 启动 SliceQ.exe（GUI，console=False）
  2. 高频轮询（每 80ms）枚举全系统的 `ConsoleWindowClass` 顶层窗口
  3. 记录任何**新出现**的控制台窗口（PID / 进程名 / 标题 / 可见性）
  4. 顺带记录 SliceQ 的**进程树**（onefile 会有引导进程 + 子进程）

跑完自动关掉 SliceQ。
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

OUT = Path(r"C:\temp\sliceq_console_result.json")
OUT.parent.mkdir(parents=True, exist_ok=True)

user32 = ctypes.WinDLL("user32", use_last_error=True)
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

EXE = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "SliceQ" / "SliceQ.exe"


def console_windows() -> dict[tuple, dict]:
    """全系统 ConsoleWindowClass 顶层窗口，key = (pid, hwnd)。"""
    out: dict[tuple, dict] = {}

    def cb(hwnd, _l):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value == "ConsoleWindowClass":
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            t = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, t, 512)
            out[(pid.value, int(hwnd))] = {
                "pid": pid.value,
                "hwnd": int(hwnd),
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "title": t.value,
            }
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return out


def proc_names() -> dict[int, str]:
    r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True,
                       encoding="gbk", errors="replace")
    names = {}
    for ln in (r.stdout or "").splitlines():
        parts = [p.strip('"') for p in ln.split('","')]
        if len(parts) >= 2:
            try:
                names[int(parts[1])] = parts[0]
            except ValueError:
                pass
    return names


def sliceq_tree() -> list[dict]:
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq SliceQ.exe",
                        "/FO", "CSV", "/NH"],
                       capture_output=True, text=True,
                       encoding="gbk", errors="replace")
    out = []
    for ln in (r.stdout or "").splitlines():
        parts = [p.strip('"') for p in ln.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("sliceq"):
            out.append({"name": parts[0], "pid": parts[1],
                        "mem": parts[4] if len(parts) > 4 else ""})
    return out


result: dict = {"steps": []}

before = console_windows()
result["基线控制台窗口数"] = len(before)
result["基线明细"] = list(before.values())[:10]

print("启动 SliceQ.exe …")
p = subprocess.Popen([str(EXE)], cwd=str(EXE.parent))

seen = set(before.keys())
newly: list[dict] = []
deadline = time.time() + 20.0
while time.time() < deadline:
    cur = console_windows()
    for k, v in cur.items():
        if k not in seen:
            names = proc_names()
            v = dict(v)
            v["进程名"] = names.get(v["pid"], "?")
            v["首次出现"] = time.strftime("%H:%M:%S")
            newly.append(v)
            seen.add(k)
    time.sleep(0.08)

result["★ 新出现的控制台窗口"] = newly
result["SliceQ 进程树"] = sliceq_tree()

# 也看看有没有别的可疑子进程
result["ffmpeg 进程"] = subprocess.run(
    ["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe", "/FO", "CSV", "/NH"],
    capture_output=True, text=True, encoding="gbk",
    errors="replace").stdout.strip()[:300]

# 关掉
try:
    p.terminate()
    time.sleep(1.5)
    subprocess.run(["taskkill", "/F", "/IM", "SliceQ.exe"],
                   capture_output=True)
except Exception:
    pass

OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2),
               encoding="utf-8")
print("结果已写入", OUT)
