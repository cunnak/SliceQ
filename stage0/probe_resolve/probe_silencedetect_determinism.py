"""① 号用例「逐字节不同」到底是 flag 引起的，还是 ffmpeg 自身不确定？

为什么要单独查
--------------
上一轮结果里，`silencedetect` 的 stderr **字节数完全一样（1009/1009）**
但内容**逐字节不同**。有两种解释，必须区分：

  H1  CREATE_NO_WINDOW 改变了子进程环境 ⇒ 输出真的变了（严重，要放弃该方案）
  H2  ffmpeg 的这次输出**本来就不确定**（与 flag 无关）

**区分方法（本项目硬约束 #27：先数清变了几个变量）**：
  同一命令跑两次，**两次都不加 flag**。
  · 若这两次也不同 ⇒ 支持 H2（ffmpeg 非确定性），flag 清白
  · 若这两次完全相同 ⇒ 支持 H1，必须换方案

顺带把差异的**具体内容**打出来（而不是只看一个布尔值）。
"""
from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\b1596\WorkBuddy\2026-09-23-13-06-47\SliceQ")
from sliceq import ffmpeg_tools  # noqa: E402

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
OUT = Path(r"C:\temp\silencedetect_determinism.json")

ff = ffmpeg_tools.find_ffmpeg()
tmp = Path(os.environ["TEMP"])
wav = tmp / "_det_silence.wav"

# 造一个"有内容也有静音"的音频，让 silencedetect 输出更多行
subprocess.run([str(ff), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i",
                "aevalsrc=if(lt(mod(t\\,4)\\,2)\\,0.5*sin(2*PI*440*t)\\,0)"
                ":s=16000:d=40", str(wav)],
               capture_output=True)

CMD = [str(ff), "-hide_banner", "-nostats", "-i", str(wav), "-vn",
       "-af", "silencedetect=noise=-35dB:d=1.0", "-f", "null", "-"]


def run(flags: int) -> tuple[int, bytes]:
    kw = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if flags:
        kw["creationflags"] = flags
    r = subprocess.run(CMD, timeout=120, **kw)
    return r.returncode, r.stderr or b""


runs: dict[str, list] = {}
runs["无flag_第1次"] = run(0)
runs["无flag_第2次"] = run(0)
runs["NO_WINDOW_第1次"] = run(CREATE_NO_WINDOW)


def lines(b: bytes) -> list[str]:
    return b.decode("utf-8", "replace").splitlines()


report = {
    "字节数": {k: len(v[1]) for k, v in runs.items()},
    "退出码": {k: v[0] for k, v in runs.items()},
}

# ── 关键对照 1：两次都不加 flag，是否相同？────────────────
a, b = runs["无flag_第1次"][1], runs["无flag_第2次"][1]
report["★ 对照1_无flag两次是否相同"] = (a == b)
if a != b:
    diff = [ln for ln in difflib.unified_diff(
        lines(a), lines(b), "第1次", "第2次", lineterm="", n=1)]
    report["★ 对照1_差异行"] = diff[:20]

# ── 关键对照 2：无 flag vs 有 flag ───────────────────────
c = runs["NO_WINDOW_第1次"][1]
report["★ 对照2_无flag与NO_WINDOW是否相同"] = (a == c)
if a != c:
    diff = [ln for ln in difflib.unified_diff(
        lines(a), lines(c), "无flag", "NO_WINDOW", lineterm="", n=1)]
    report["★ 对照2_差异行"] = diff[:20]

report["输出样例"] = lines(a)[:8]
report["★ 结论"] = (
    "ffmpeg 自身非确定性（与 flag 无关）—— flag 清白"
    if not report["★ 对照1_无flag两次是否相同"]
    else ("★ flag 真的改变了输出 —— 方案需重新考虑"
          if not report["★ 对照2_无flag与NO_WINDOW是否相同"]
          else "完全确定，无差异")
)

wav.unlink(missing_ok=True)
OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
               encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2)[:2000])
