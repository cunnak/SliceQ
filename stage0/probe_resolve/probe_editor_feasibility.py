# -*- coding: utf-8 -*-
"""阶段 3 前置探针 — 剪辑执行的两个真实风险点

问 1：`subtitles` 滤镜在 Windows + gyan.dev 构建上能否加载字体并渲染中文？
      （libass 依赖 fontconfig，Windows 上没有系统级 fontconfig，
        构建是否内置字体搜索、能否找到中文字体，必须实测）

问 2：多片段 concat 后，字幕的时间轴是否与「累加时长」一致？
      （如果不一致，§6.4.3 的重映射公式就是错的）

问 3：快速模式（-c copy）的实际切点误差有多大？
      （设计文档写"±2s"，但那是估计值，没有实测过）
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent.parent
MEDIA = PROJ / "stage0" / "test_media" / "live_clip90.mp4"
WORK = PROJ / "stage0" / "test_media" / "editor_probe"

FFMPEG = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffmpeg.exe")
FFPROBE = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffprobe.exe")


def run(args: list[str], timeout: int = 300) -> tuple[int, str, str]:
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    return r.returncode, r.stdout or "", r.stderr or ""


def dur_of(p: Path) -> float:
    _, out, _ = run([str(FFPROBE), "-v", "error",
                     "-show_entries", "format=duration",
                     "-of", "csv=p=0", str(p)])
    try:
        return float(out.strip())
    except Exception:
        return -1.0


def head(t: str) -> None:
    print()
    print("=" * 76)
    print(t)
    print("=" * 76)


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)

    print(f"素材 : {MEDIA.name}  ({MEDIA.stat().st_size / 1048576:.2f} MB, "
          f"{dur_of(MEDIA):.2f}s)")

    # ══════════════════════════════════════════════════════════
    head("问 1 · subtitles 滤镜能否渲染（Windows 字体问题）")

    # 造一个最小 SRT
    srt = WORK / "t.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:03,000\n这是中文字幕测试\n\n"
        "2\n00:00:03,000 --> 00:00:06,000\n第二行 ABC 123\n",
        encoding="utf-8")

    out1 = WORK / "sub_test.mp4"
    # libass 的 fontconfig 需要显式 dir；先试默认（不给 fontsdir）
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "warning", "-y",
           "-ss", "0", "-t", "6", "-i", str(MEDIA),
           "-vf", f"subtitles={srt.as_posix()}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
           "-c:a", "copy", str(out1)]
    t0 = time.time()
    rc, _, err = run(cmd, timeout=300)
    print(f"  不给 fontsdir: rc={rc}  耗时 {time.time() - t0:.1f}s")
    if err.strip():
        for line in err.strip().splitlines()[:6]:
            print("    ", line[:110])
    print(f"  产出: {'存在 ' + str(out1.stat().st_size) + ' B' if out1.exists() else '**无**'}")

    # 再用 fontsdir 指向系统字体目录
    if rc != 0:
        fonts = Path(r"C:\Windows\Fonts")
        out2 = WORK / "sub_test_fontsdir.mp4"
        cmd2 = [str(FFMPEG), "-hide_banner", "-loglevel", "warning", "-y",
                "-ss", "0", "-t", "6", "-i", str(MEDIA),
                "-vf", f"subtitles={srt.as_posix()}:fontsdir={fonts.as_posix()}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-c:a", "copy", str(out2)]
        rc2, _, err2 = run(cmd2, timeout=300)
        print(f"  给 fontsdir : rc={rc2}")
        for line in (err2 or "").strip().splitlines()[:6]:
            print("    ", line[:110])

    # ══════════════════════════════════════════════════════════
    head("问 2 · 多片段切分的实际时长（决定重映射公式是否成立）")

    # 切三段，源区间刻意不连续
    spans = [(0.0, 10.0), (30.0, 45.0), (60.0, 66.0)]
    pieces: list[Path] = []
    for i, (a, b) in enumerate(spans):
        p = WORK / f"seg{i}.mp4"
        cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{a}", "-t", f"{b - a}", "-i", str(MEDIA),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
               "-c:a", "aac", "-b:a", "64k", str(p)]
        rc, _, err = run(cmd)
        got = dur_of(p) if p.exists() else -1
        want = b - a
        print(f"  段{i}: 源 {a:6.1f}→{b:6.1f} (要 {want:5.1f}s)  "
              f"实际 {got:6.3f}s  偏差 {got - want:+.3f}s  rc={rc}")
        if err.strip():
            print("       ", err.strip().splitlines()[0][:100])
        pieces.append(p)

    print()
    print(f"  三段累加 = {sum(dur_of(p) for p in pieces):.3f}s")

    # concat 拼接
    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in pieces), encoding="utf-8")
    joined = WORK / "joined.mp4"
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(lst),
           "-c", "copy", str(joined)]
    rc, _, err = run(cmd, cwd=str(WORK))
    print(f"  拼接: rc={rc}  产出 {dur_of(joined) if joined.exists() else -1:.3f}s")
    if err.strip():
        for line in err.strip().splitlines()[:4]:
            print("    ", line[:110])

    # ══════════════════════════════════════════════════════════
    head("问 3 · 快速模式（-c copy）的切点误差")

    a, b = 30.0, 45.0
    p = WORK / "fast.mp4"
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{a}", "-t", f"{b - a}", "-i", str(MEDIA),
           "-c", "copy", str(p)]
    rc, _, err = run(cmd)
    got = dur_of(p) if p.exists() else -1
    print(f"  源 {a}→{b}（要 {b - a:.1f}s）  快速模式实际 {got:.3f}s  "
          f"偏差 {got - (b - a):+.3f}s  rc={rc}")
    if err.strip():
        for line in err.strip().splitlines()[:4]:
            print("    ", line[:110])

    # 关键帧位置（决定 copy 模式的精度上限）
    _, out, _ = run([str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "packet=pts_time,flags",
                     "-of", "csv=p=0", "-read_intervals", "25%+25",
                     str(MEDIA)])
    kfs = [ln.split(",")[0] for ln in out.strip().splitlines()
           if "K" in ln]
    print(f"  25s~50s 区间内的关键帧: {kfs[:8]}")
    if len(kfs) >= 2:
        try:
            gaps = [float(kfs[i + 1]) - float(kfs[i]) for i in range(len(kfs) - 1)]
            print(f"  关键帧间隔: {[round(g, 2) for g in gaps]}  平均 "
                  f"{sum(gaps) / len(gaps):.2f}s")
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
