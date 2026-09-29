"""加 CREATE_NO_WINDOW 之后，子进程的输出会不会丢？

为什么必须测
------------
`CREATE_NO_WINDOW` 的语义是「不要给新进程分配控制台」。
已知的风险是：**如果子进程依赖控制台句柄做输出，它可能失效。**

而本项目的核心逻辑**全靠子进程的 stderr/stdout 文本**：
  · `find_silences()` 解析 stderr 里的 `silence_start/silence_end`
  · whisper 转录读 **stdout 的数兆字节 JSONL**
  · `list_filters()` 解析 stdout 拿滤镜名
  · `_run_ffmpeg` 的报错信息来自 stderr 尾部

⇒ 若加 flag 后输出丢失，就是**拿一个静默 bug 换另一个静默 bug**。
   所以必须实测：**同一命令，有/无 flag，比对输出字节数与关键内容**。

测试项
------
  1. 大体积 stderr（silencedetect，实测约 95KB —— 也是当年管道死锁的素材）
  2. 大体积 stdout（模拟 whisper 的 JSONL，直接让 python 打印 3MB）
  3. 二进制/中文内容是否完好（编码不被打断）
  4. 退出码是否一致
  5. 同时确认：**没有**控制台窗口事件（用事件钩子）
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

OUT = Path(r"C:\temp\no_window_output_result.json")
OUT.parent.mkdir(parents=True, exist_ok=True)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
EVENT_OBJECT_CREATE = 0x8000
WINEVENT_OUTOFCONTEXT = 0x0000
WM_QUIT = 0x0012
CONSOLE_CLASSES = {"ConsoleWindowClass", "CASCADING_HOSTING_WINDOW_CLASS",
                   "CASCADIA_HOSTING_WINDOW_CLASS"}

events: list[dict] = []
T0 = time.time()

WinEventProc = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)


def on_event(_h, _e, hwnd, _a, _b, _c, _d):
    if not hwnd:
        return
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    if buf.value in CONSOLE_CLASSES:
        events.append({"t": round(time.time() - T0, 3), "class": buf.value})


_cb = WinEventProc(on_event)
hook = user32.SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_CREATE,
                              None, _cb, 0, 0, WINEVENT_OUTOFCONTEXT)
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
MAIN_TID = kernel32.GetCurrentThreadId()
_init = wintypes.MSG()
user32.PeekMessageW(ctypes.byref(_init), None, 0, 0, 0)

results: list[dict] = []


def run_both(label: str, cmd: list[str], **kw) -> None:
    """同一命令跑两次：无 flag vs CREATE_NO_WINDOW，比对输出。"""
    row: dict = {"用例": label}
    for tag, extra in (("无flag", {}),
                       ("CREATE_NO_WINDOW", {"creationflags": CREATE_NO_WINDOW})):
        k = dict(kw)
        k.update(extra)
        t = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=300, **k)
            row[tag] = {
                "rc": r.returncode,
                "stdout 字节": len(r.stdout or b""),
                "stderr 字节": len(r.stderr or b""),
                "耗时": round(time.time() - t, 2),
                "_stderr": r.stderr,          # 内部比对用，最后删掉
                "_stdout": r.stdout,
            }
        except Exception as exc:               # noqa: BLE001
            row[tag] = {"错误": f"{type(exc).__name__}: {exc}"}
    results.append(row)


def worker() -> None:
    ff = ffmpeg_tools.find_ffmpeg()
    fp = ffmpeg_tools.find_ffprobe(ff)
    tmp = Path(os.environ["TEMP"])

    # ① 大体积 stderr：silencedetect（当年 95,662 字节那条）
    wav = tmp / "_nw_silence.wav"
    subprocess.run([str(ff), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i",
                    "anullsrc=r=16000:cl=mono:d=180", str(wav)],
                   capture_output=True)
    run_both("① silencedetect（大 stderr）", [
        str(ff), "-hide_banner", "-nostats", "-i", str(wav), "-vn",
        "-af", "silencedetect=noise=-35dB:d=1.0", "-f", "null", "-"])

    # ② 大体积 stdout：模拟 whisper 的 JSONL（3MB）
    py = sys.executable.replace("pythonw.exe", "python.exe")
    run_both("② 大 stdout（3MB JSONL）", [
        py, "-c",
        "import sys; sys.stdout.write('{\"t\":1}\\n'*300000); "
        "sys.stderr.write('done\\n')"],
        text=False)

    # ③ 中文 + 多行（编码完好性）
    zh_code = ("import sys\n"
               "sys.stderr.write('带比较好的核显的CPU主板\\n')\n"
               "sys.stderr.write('第二行\\n第三行\\n')\n"
               "sys.stdout.write('stdout 里的中文\\n')\n")
    run_both("③ 中文与多行输出", [py, "-c", zh_code], text=False)

    # ④ ffprobe 的 JSON（结构化输出）
    run_both("④ ffprobe JSON", [
        str(fp), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "json", str(wav)], text=False)

    # ⑤ whisper 转录那条路：stdout 是 JSONL
    run_both("⑤ ffmpeg -filters（stdout 长文本）", [
        str(ff), "-hide_banner", "-filters"])

    wav.unlink(missing_ok=True)
    time.sleep(0.5)
    user32.PostThreadMessageW(MAIN_TID, WM_QUIT, 0, 0)


th = threading.Thread(target=worker, daemon=True)
th.start()
msg = wintypes.MSG()
while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))
user32.UnhookWinEvent(hook)

# ── 汇总 ────────────────────────────────────────────────────
summary = []
for row in results:
    a, b = row.get("无flag", {}), row.get("CREATE_NO_WINDOW", {})
    same_out = a.get("stdout 字节") == b.get("stdout 字节")
    same_err = a.get("stderr 字节") == b.get("stderr 字节")
    same_rc = a.get("rc") == b.get("rc")
    identical = (a.get("_stdout") == b.get("_stdout")
                 and a.get("_stderr") == b.get("_stderr"))
    summary.append({
        "用例": row["用例"],
        "无flag": {k: v for k, v in a.items() if not k.startswith("_")},
        "CREATE_NO_WINDOW": {k: v for k, v in b.items() if not k.startswith("_")},
        "stdout 字节一致": same_out,
        "stderr 字节一致": same_err,
        "退出码一致": same_rc,
        "★ 输出逐字节相同": identical,
    })

OUT.write_text(json.dumps({
    "汇总": summary,
    "★ 是否有任何用例输出不一致": [
        s["用例"] for s in summary if not s["★ 输出逐字节相同"]],
    "控制台窗口事件数": len(events),
    "控制台窗口事件": events[:10],
}, ensure_ascii=False, indent=2), encoding="utf-8")
