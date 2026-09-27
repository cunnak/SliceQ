# -*- coding: utf-8 -*-
"""阶段 3 前置探针 C — 快速/精确模式的真实切点误差 + concat 行为

上一轮探针因 run() 缺 cwd 参数中断，拼接结果没拿到。这里补完。

要回答的问题：
  1. 精确模式（-ss 前置 + 重编码）的时长误差 → 决定重映射公式是否可靠
  2. 快速模式（-c copy）的时长误差 → 决定 UI 上"±2s"这个说法对不对
  3. concat demuxer 拼接后总时长 == 各段累加？→ 决定重映射公式的分母
  4. 各段时长若与源区间不等，重映射会累积误差 —— 量化这个误差

⚠️ 时长相等 ≠ 内容对齐。所以还要做一次**内容级**验证：
   在源视频某个已知时间点做标记，切出来后查标记是否落在预期位置。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent.parent
MEDIA = PROJ / "stage0" / "test_media" / "live_clip90.mp4"
WORK = PROJ / "stage0" / "test_media" / "editor_probe3"

FFMPEG = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffmpeg.exe")
FFPROBE = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffprobe.exe")


def run(args, timeout=300, cwd=None):
    r = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace",
                       cwd=cwd)
    return r.returncode, r.stdout or "", r.stderr or ""


def dur(p: Path) -> float:
    _, out, _ = run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", p])
    try:
        return float(out.strip())
    except Exception:
        return -1.0


def head(t):
    print()
    print("=" * 76)
    print(t)
    print("=" * 76)


SPANS = [(0.0, 10.0), (30.0, 45.0), (60.0, 66.0)]


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    total_want = sum(b - a for a, b in SPANS)
    print(f"素材 {MEDIA.name} ({dur(MEDIA):.2f}s)")
    print(f"计划切 3 段: {SPANS}   累加应为 {total_want:.3f}s")

    # ══════════════════════════════════════════════════════
    head("问 1 · 精确模式（-ss 前置 + 重编码）")

    exact: list[Path] = []
    for i, (a, b) in enumerate(SPANS):
        p = WORK / f"ex{i}.mp4"
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{a}", "-t", f"{b - a}", "-i", MEDIA,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
             "-c:a", "aac", "-b:a", "64k", p])
        g, w = dur(p), b - a
        print(f"  段{i}  {a:6.1f}→{b:6.1f}  要 {w:5.1f}s  得 {g:7.3f}s  "
              f"误差 {g - w:+.3f}s")
        exact.append(p)
    print(f"  ── 累加 {sum(dur(p) for p in exact):.3f}s "
          f"(计划 {total_want:.3f}s, 误差 "
          f"{sum(dur(p) for p in exact) - total_want:+.3f}s)")

    # ══════════════════════════════════════════════════════
    head("问 2 · 快速模式（-c copy）")

    fast: list[Path] = []
    for i, (a, b) in enumerate(SPANS):
        p = WORK / f"fa{i}.mp4"
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{a}", "-t", f"{b - a}", "-i", MEDIA,
             "-c", "copy", p])
        g, w = dur(p), b - a
        print(f"  段{i}  {a:6.1f}→{b:6.1f}  要 {w:5.1f}s  得 {g:7.3f}s  "
              f"误差 {g - w:+.3f}s")
        fast.append(p)
    print(f"  ── 累加 {sum(dur(p) for p in fast):.3f}s "
          f"(误差 {sum(dur(p) for p in fast) - total_want:+.3f}s)")

    # 关键帧间隔（决定 copy 的精度上限）
    _, out, _ = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "packet=pts_time,flags",
                     "-of", "csv=p=0", "-read_intervals", "25%+30", MEDIA])
    kfs = []
    for ln in out.strip().splitlines():
        parts = ln.split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                kfs.append(float(parts[0]))
            except ValueError:
                pass
    if len(kfs) >= 2:
        gaps = [round(kfs[i + 1] - kfs[i], 2) for i in range(len(kfs) - 1)]
        print(f"  25~55s 关键帧: {[round(k, 1) for k in kfs[:8]]}")
        print(f"  间隔: {gaps}  ← copy 模式的切点误差上限就是这个量级")

    # ══════════════════════════════════════════════════════
    head("问 3 · concat 拼接（重映射公式的分母）")

    for tag, pieces in (("精确", exact), ("快速", fast)):
        lst = WORK / f"{tag}.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in pieces),
                       encoding="utf-8")
        out = WORK / f"cat_{tag}.mp4"
        rc, _, err = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                          "-f", "concat", "-safe", "0", "-i", lst.name,
                          "-c", "copy", out.name], cwd=str(WORK))
        g = dur(out) if out.exists() else -1
        s = sum(dur(p) for p in pieces)
        print(f"  {tag}: 各段累加 {s:7.3f}s → 拼接后 {g:7.3f}s  "
              f"差 {g - s:+.3f}s  rc={rc}")
        if err.strip():
            for ln in err.strip().splitlines()[:3]:
                print("       ", ln[:110])

    # ══════════════════════════════════════════════════════
    head("问 4 · 内容级验证：切点处画面是否对齐")

    # 在源视频 30.0s 处抽一帧，再在「段1（源30→45）的第 0 秒」抽一帧
    # 两帧应几乎相同（同一画面）。这是唯一能证明"切点准确"的证据。
    src_frame = WORK / "src_at30.png"
    seg_frame = WORK / "seg1_at0.png"
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-ss", "30", "-i", MEDIA, "-frames:v", "1", src_frame])
    p1 = WORK / "ex1.mp4"
    if p1.exists():
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", "0", "-i", p1, "-frames:v", "1", seg_frame])

    def gray_sig(p: Path) -> str:
        """用 signalstats 取平均亮度 + 帧哈希，作为\"是不是同一画面\"的粗判据"""
        rc, out, _ = run([FFMPEG, "-hide_banner", "-nostats", "-i", p,
                          "-vf", "signalstats,metadata=print",
                          "-f", "null", "-"])
        vals = {}
        for ln in out.splitlines():
            for k in ("lavfi.signalstats.YAVG", "lavfi.signalstats.SATAVG"):
                if k in ln and "=" in ln:
                    try:
                        vals[k.split(".")[-1]] = float(ln.split("=")[-1])
                    except ValueError:
                        pass
        return f"YAVG={vals.get('YAVG')} SATAVG={vals.get('SATAVG')}"

    print(f"  源 30.0s 处      : {gray_sig(src_frame)}")
    print(f"  段1(源30→45) 第0s: {gray_sig(seg_frame)}")
    print("  （两项接近 ⇒ 切点对齐；差得远 ⇒ 切点漂移）")

    # 对照：源 35s 处（应明显不同于 30s 处）
    ctrl = WORK / "src_at35.png"
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-ss", "35", "-i", MEDIA, "-frames:v", "1", ctrl])
    print(f"  对照 源 35.0s 处 : {gray_sig(ctrl)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
