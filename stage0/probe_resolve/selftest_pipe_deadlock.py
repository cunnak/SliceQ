# -*- coding: utf-8 -*-
r"""子进程管道死锁自测（回归测试）。

═══════════════════════════════════════════════════════════════
起因：v0.1.0 发布后收到的第一个真实缺陷（2026-09-28）
═══════════════════════════════════════════════════════════════
用户点「开始精析」，界面卡在「准备中…」不动。

取证：任务状态停在 `transcribing`，工作目录里 `audio.wav` 已写完
（162,417,152 字节 = 84 分钟 × 16kHz × 16bit 单声道，分毫不差），
但**没有任何转录产物**。进程表里有一个 ffmpeg：

    CPU 时间 0:00:00       ← 活了 6 分钟，一点 CPU 都没用
    CMD = ffmpeg ... -af silencedetect=noise=-35dB:d=1.0 -f null -

真因：`asr.py::_run_ffmpeg` 当时是「`while True: proc.poll()` 空转 +
结束后才 `communicate()`」—— **循环里一个字节都不读**。
ffmpeg 写满管道缓冲区（Windows 约 64KB）后永久阻塞在写上，
父进程在等它退出 ⇒ 互相等。

实测（84 分钟音频）：那条命令单独跑 **0.8 秒**就结束，
却往 stderr 写了 **95,662 字节** > 65,536 ⇒ 必然死锁。

⚠️ **为什么 100+ 项自测全过却没抓到**：
    是否超缓冲区取决于**素材的静音段多少** —— 静音越多，stderr 越大。
    之前的测试素材没这么多静音，所以从没触发。
    ⇒ 这正是"只会在真实用户素材上发作"的那类缺陷。

本自测**直接针对机制本身**（不依赖任何素材）：
让子进程往两个管道各写超量数据，看 `_run_ffmpeg` 会不会挂死。
旧实现会**永远挂住**；新实现会正常返回。

用法：
    python stage0/probe_resolve/selftest_pipe_deadlock.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sliceq import _testing                       # noqa: E402

_TMP = _testing.isolate_data_root("pipedeadlock")  # ★ 绝不碰用户真实数据
_testing.assert_isolated()

from sliceq import asr                            # noqa: E402

PASS, FAIL = [], []
PY = sys.executable


def ck(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    return cond


def run_with_cap(fn, cap_s: float = 25.0):
    """在守护线程里跑 fn；超过 cap_s 未返回则判定为死锁。

    ★ 必须用守护线程 —— 否则一旦回归成死锁，测试进程自己也会永远挂住，
       CI 只会看到"超时"，看不到是哪一条断言失败。
    """
    box: dict = {}

    def _w():
        try:
            box["result"] = fn()
        except BaseException as e:                    # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=_w, daemon=True)
    t0 = time.time()
    t.start()
    t.join(cap_s)
    return (not t.is_alive()), time.time() - t0, box


def main() -> int:
    print("=" * 74)
    print("子进程管道死锁自测（回归）")
    print("=" * 74)
    print(f"  解释器: {PY}")

    # ── [1] stderr 超量：**这就是真实触发的那条路** ────────────
    print("\n[1] 子进程往 stderr 写 300KB（旧实现在这里必挂）")
    N = 300_000
    script = f"import sys; sys.stderr.write('E'*{N}); sys.stderr.flush()"
    ok, dt, box = run_with_cap(
        lambda: asr._run_ffmpeg([PY, "-c", script], timeout=60))
    if ok and "result" in box:
        r = box["result"]
        ck("★ 未死锁（在 25 秒内返回）", True, f"{dt:.2f} 秒")
        ck("stderr 完整收全（没被截断）", len(r.stderr or "") == N,
           f"{len(r.stderr or '')} / {N}")
        ck("退出码为 0", r.returncode == 0, str(r.returncode))
    else:
        ck("★ 未死锁（在 25 秒内返回）", False,
           f"★ 挂死！等了 {dt:.1f} 秒仍未返回"
           + (f"（{box.get('error')}）" if box.get("error") else ""))

    # ── [2] stdout 超量：whisper 转录那条路（JSONL 动辄几 MB）──
    print("\n[2] 子进程往 stdout 写 2MB（whisper 转录的路）")
    M = 2_000_000
    script2 = (f"import sys\n"
               f"for _ in range(2000): sys.stdout.write('J'*1000 + chr(10))\n"
               f"sys.stdout.flush()\n"
               f"sys.stderr.write('done')\n")
    ok2, dt2, box2 = run_with_cap(
        lambda: asr._run_ffmpeg([PY, "-c", script2], timeout=60))
    if ok2 and "result" in box2:
        r2 = box2["result"]
        ck("★ 未死锁（在 25 秒内返回）", True, f"{dt2:.2f} 秒")
        ck("stdout 完整收全", len(r2.stdout or "") >= M,
           f"{len(r2.stdout or ''):,} / {M:,}")
    else:
        ck("★ 未死锁（在 25 秒内返回）", False,
           f"★ 挂死！等了 {dt2:.1f} 秒仍未返回")

    # ── [3] 两个管道**同时**超量 ─────────────────────────────
    print("\n[3] stdout 与 stderr 同时超量（最恶劣情形）")
    script3 = ("import sys\n"
               "sys.stdout.write('O'*500000)\n"
               "sys.stderr.write('E'*500000)\n"
               "sys.stdout.flush(); sys.stderr.flush()\n")
    ok3, dt3, box3 = run_with_cap(
        lambda: asr._run_ffmpeg([PY, "-c", script3], timeout=60))
    if ok3 and "result" in box3:
        r3 = box3["result"]
        ck("★ 未死锁（在 25 秒内返回）", True, f"{dt3:.2f} 秒")
        ck("两侧都完整收全",
           len(r3.stdout or "") == 500_000 and len(r3.stderr or "") == 500_000,
           f"{len(r3.stdout or ''):,} / {len(r3.stderr or ''):,}")
    else:
        ck("★ 未死锁（在 25 秒内返回）", False,
           f"★ 挂死！等了 {dt3:.1f} 秒仍未返回")

    # ── [4] 取消仍然有效（改完之后别把取消功能弄坏）───────────
    print("\n[4] 取消回调仍然生效（改完后不能丢功能）")
    stop = threading.Event()
    ok4, dt4, box4 = run_with_cap(
        lambda: asr._run_ffmpeg([PY, "-c", "import time; time.sleep(120)"],
                                should_cancel=stop.is_set), cap_s=15)
    ck("没取消时：**不**返回（仍在跑）", ok4 is False or "result" not in box4,
       f"{dt4:.1f} 秒")
    # 现在真取消：
    stop.set()
    ok5, dt5, box5 = run_with_cap(
        lambda: asr._run_ffmpeg([PY, "-c", "import time; time.sleep(120)"],
                                should_cancel=stop.is_set), cap_s=15)
    ck("★ 触发取消后被正确中断（抛 AsrCancelled）",
       "error" in box5 and isinstance(box5["error"], asr.AsrCancelled),
       f"{type(box5.get('error')).__name__}，{dt5:.1f} 秒")

    # ── [5] 超时仍然有效 ────────────────────────────────────
    print("\n[5] 超时保护仍然有效")
    ok6, dt6, box6 = run_with_cap(
        lambda: asr._run_ffmpeg([PY, "-c", "import time; time.sleep(120)"],
                                timeout=2), cap_s=20)
    ck("★ 超时后抛 AsrError",
       "error" in box6 and isinstance(box6["error"], asr.AsrError),
       f"{type(box6.get('error')).__name__}，{dt6:.1f} 秒")

    print("\n" + "=" * 74)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)} / 共 {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("   -", f)
    print("=" * 74)
    print("隔离目录:", _TMP)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
