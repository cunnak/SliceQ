"""端到端验收：走**真实业务函数**，确认屏幕上不再冒出终端窗口。

为什么必须是"真实业务函数"
--------------------------
前面几个探针都是**直接** `Popen(ffmpeg …)`，那只证明"这个手法有效"，
不证明"**产品里每一处调用都改到了**"。

本脚本调用产品自己的函数，覆盖会启动子进程的主要路径：

    ffmpeg_tools.probe_environment()   ← 启动时必跑（环境体检）
    ingest.probe_media()               ← 拖入视频时跑
    ffmpeg_tools.probe_video_size()    ← 生成字幕前跑
    ffmpeg_tools.has_audio_stream()    ← 配乐前跑
    asr.extract_audio()                ← 转录第 1 步
    asr.find_silences()                ← 转录第 2 步
    pipeline.cut_clip()                ← 精析切片

★ 同时跑**对照组**：用**旧的裸 `subprocess.run`** 再走一遍。
  这一组**必须**抓到窗口 —— 否则说明钩子本身失效，
  "第二组 0 事件"就不能算证据（本项目硬约束：回归测试要有牙）。

用法（必须用 pythonw，才等于发布版的无控制台环境）：
    pythonw.exe probe_no_window_e2e.py
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

ROOT = Path(r"C:\Users\b1596\WorkBuddy\2026-09-23-13-06-47\SliceQ")
sys.path.insert(0, str(ROOT))

OUT = Path(r"C:\temp\no_window_e2e_result.json")

from sliceq import _testing                      # noqa: E402

TMP = Path(_testing.isolate_data_root("nowin_e2e"))
_testing.assert_isolated()

from sliceq import (asr, config, ffmpeg_tools, ingest, pipeline,  # noqa: E402
                    subproc)

config.ensure_dirs()

# ─────────────────────────────────────────────────────────────
# 事件钩子（系统级，捕获所有进程新建的控制台窗口）
# ─────────────────────────────────────────────────────────────
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
EVENT_OBJECT_CREATE = 0x8000
WINEVENT_OUTOFCONTEXT = 0x0000
WM_QUIT = 0x0012
CONSOLE_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"}

T0 = time.time()
events: list[dict] = []
phase = ["准备"]
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
        with _lock:
            events.append({"t": round(time.time() - T0, 3),
                           "phase": phase[0], "class": buf.value})


_cb = WinEventProc(on_event)
hook = user32.SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_CREATE,
                              None, _cb, 0, 0, WINEVENT_OUTOFCONTEXT)
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
MAIN_TID = kernel32.GetCurrentThreadId()
_init = wintypes.MSG()
user32.PeekMessageW(ctypes.byref(_init), None, 0, 0, 0)

steps: list[dict] = []


def note(name: str, ok: bool, detail: str = "") -> None:
    steps.append({"步骤": name, "成功": ok, "细节": detail})


def count_in(p: str) -> int:
    with _lock:
        return sum(1 for e in events if e["phase"] == p)


def worker() -> None:
    # ⚠️ 不要写 `global phase` 再直接赋值 —— 那会把 `phase` 从**列表**重绑成
    #    **字符串**，而回调里读的是 `phase[0]`，于是标签退化成单个字符
    #    'A' / 'B'，`count_in()` 全部落空。
    #    本脚本第一次跑就栽在这：明明抓到了 3 个事件，却被判成"钩子失效"。
    #    ⇒ 原地改列表元素。
    ff = ffmpeg_tools.find_ffmpeg()
    fp = ffmpeg_tools.find_ffprobe(ff)
    video = TMP / "e2e_src.mp4"
    wav = TMP / "e2e_audio.wav"
    clip = TMP / "e2e_clip.mp4"

    # ── 阶段 0：造素材（钩子已装，但这是准备，不计入结论）──
    try:
        r = subproc.run([str(ff), "-hide_banner", "-loglevel", "error",
                      "-nostdin", "-y",
                      "-f", "lavfi", "-i",
                      "testsrc=duration=20:size=320x240:rate=15",
                      "-f", "lavfi", "-i",
                      "aevalsrc=if(lt(mod(t\\,3)\\,1.5)\\,0.4*sin(2*PI*440*t)\\,0)"
                      ":s=16000:d=20",
                      "-t", "20", "-shortest",
                      "-c:v", "libx264", "-preset", "ultrafast",
                      "-c:a", "aac", str(video)],
                     capture_output=True, timeout=180)
        note("准备：生成测试素材", r.returncode == 0 and video.exists(),
             f"rc={r.returncode}, {video.stat().st_size if video.exists() else 0} 字节")
    except Exception as exc:                      # noqa: BLE001
        note("准备：生成测试素材", False, f"{type(exc).__name__}: {exc}")

    time.sleep(0.5)

    # ══════════════════════════════════════════════════════════
    # 阶段 A：对照组 —— 旧的裸调用（模拟改造前的代码）
    # ══════════════════════════════════════════════════════════
    phase[0] = "A_对照组(裸调用)"
    # ⚠️ 用**慢任务**（3 秒转码），不要用 `ffmpeg -version`——
    #    `-version` 几十毫秒就退出，窗口一闪，钩子未必来得及投递，
    #    会把对照组成"0 事件"从而让整次验证失去意义（有牙的对照才叫对照）。
    ctl_out = TMP / "e2e_control.mp4"
    for i in range(3):
        try:
            subprocess.run(
                [str(ff), "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-y", "-f", "lavfi", "-i",
                 "testsrc=duration=3:size=160x120:rate=10",
                 "-t", "3", str(ctl_out)],
                capture_output=True, timeout=60)
        except Exception:                          # noqa: BLE001
            pass
        time.sleep(0.3)
    ctl_out.unlink(missing_ok=True)
    print(f"对照组完成，事件 {count_in('A_对照组(裸调用)')} 个")

    # ══════════════════════════════════════════════════════════
    # 阶段 B：走真实的业务函数（应当 0 事件）
    # ══════════════════════════════════════════════════════════
    phase[0] = "B_真实业务函数"

    # ① 启动时的环境体检
    try:
        info = ffmpeg_tools.probe_environment()
        note("ffmpeg_tools.probe_environment()", bool(info.get("ok")),
             str(info.get("message", ""))[:60])
    except Exception as exc:                       # noqa: BLE001
        note("ffmpeg_tools.probe_environment()", False, str(exc)[:80])

    # ② 导入时读元信息
    try:
        meta = ingest.probe_media(video)
        note("ingest.probe_media()",
             bool(meta.get("duration_sec")),
             f"时长={meta.get('duration_sec')} 帧率={meta.get('fps')}")
    except Exception as exc:                       # noqa: BLE001
        note("ingest.probe_media()", False, str(exc)[:80])

    # ③ 读画面尺寸 / 是否含音轨（字幕与配乐的前置）
    try:
        wh = ffmpeg_tools.probe_video_size(video)
        note("ffmpeg_tools.probe_video_size()", wh == (320, 240), f"{wh}")
    except Exception as exc:                       # noqa: BLE001
        note("ffmpeg_tools.probe_video_size()", False, str(exc)[:80])
    try:
        has_a = ffmpeg_tools.has_audio_stream(video)
        note("ffmpeg_tools.has_audio_stream()", has_a is True, f"{has_a}")
    except Exception as exc:                       # noqa: BLE001
        note("ffmpeg_tools.has_audio_stream()", False, str(exc)[:80])

    # ④ 转录路径：提取音频 → 静音检测
    try:
        asr.extract_audio(video, wav)
        note("asr.extract_audio()", wav.exists(),
             f"{wav.stat().st_size if wav.exists() else 0} 字节")
    except Exception as exc:                       # noqa: BLE001
        note("asr.extract_audio()", False, f"{type(exc).__name__}: {exc}"[:80])
    try:
        sil = asr.find_silences(wav)
        note("asr.find_silences()", isinstance(sil, list),
             f"检出 {len(sil)} 段静音")
    except Exception as exc:                       # noqa: BLE001
        note("asr.find_silences()", False, f"{type(exc).__name__}: {exc}"[:80])

    # ⑤ 精析路径：切低码率副本
    try:
        pipeline.cut_clip(video, clip, 2.0, 8.0, height="240",
                          bitrate="200k")
        note("pipeline.cut_clip()", clip.exists(),
             f"{clip.stat().st_size if clip.exists() else 0} 字节")
    except Exception as exc:                       # noqa: BLE001
        note("pipeline.cut_clip()", False, f"{type(exc).__name__}: {exc}"[:80])

    time.sleep(0.5)
    print(f"业务函数完成，事件 {count_in('B_真实业务函数')} 个")
    user32.PostThreadMessageW(MAIN_TID, WM_QUIT, 0, 0)


th = threading.Thread(target=worker, daemon=True)
th.start()
msg = wintypes.MSG()
while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))
user32.UnhookWinEvent(hook)

a = count_in("A_对照组(裸调用)")
b = count_in("B_真实业务函数")
result = {
    "解释器": sys.executable,
    "钩子安装成功": bool(hook),
    "步骤": steps,
    "A_对照组(裸调用)_事件数": a,
    "B_真实业务函数_事件数": b,
    "全部事件": events,
    "★ 对照组有事件（证明钩子有牙）": a > 0,
    "★ 业务函数零事件（证明修复有效）": b == 0,
    "★ 结论": ("✅ 通过：旧写法会弹、新写法不弹"
               if a > 0 and b == 0 else
               ("★ 钩子失效（对照组都没抓到）—— 本结果无效" if a == 0 else
                "★ 仍有弹窗，修复不完整")),
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2),
               encoding="utf-8")
