# -*- coding: utf-8 -*-
"""阶段 4.0 前置实测：封面实现方案 / xfade / ducking

三个待钉死的不确定点：
  P1  封面：R26 记录 drawtext 段错误。测三条候选路径，选活的那条。
  P2  xfade：乙方案下每条候选是独立 mp4，"片段间转场"是否还有意义。
  P3  ducking：sidechaincompress 的实际压低效果能否被测量、参数怎么取。

运行：python stage0/probe_resolve/probe_stage4_enhance.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq import ffmpeg_tools as ft  # noqa: E402

FFMPEG = ft.find_ffmpeg()
WORK = Path(tempfile.mkdtemp(prefix="sliceq_p4_"))
RESULT: list[tuple[str, str, str]] = []


def rec(tag: str, ok: bool, detail: str) -> None:
    RESULT.append((tag, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {tag} :: {detail}")


def run(args: list[str], *, cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(FFMPEG), "-hide_banner", "-nostdin", "-y", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd) if cwd else None, timeout=timeout,
    )


# ---------------------------------------------------------------- 素材

def make_sources() -> dict[str, Path]:
    """合成素材：两段彩色视频（带音轨）+ 一段正弦人声 + 一段正弦 BGM。"""
    out: dict[str, Path] = {}
    a = WORK / "a.mp4"
    r = run(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=3",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(a)])
    out["a"] = a if r.returncode == 0 else None  # type: ignore

    b = WORK / "b.mp4"
    r = run(["-f", "lavfi", "-i", "smptebars=size=640x360:rate=30:duration=3",
             "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(b)])
    out["b"] = b if r.returncode == 0 else None  # type: ignore

    # "人声"：440Hz 断续（前 1.5s 有声、后 1.5s 静音）——用来触发 ducking
    voice = WORK / "voice.wav"
    run(["-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1.5",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[o]",
         "-map", "[o]", "-c:a", "pcm_s16le", str(voice)])
    out["voice"] = voice

    bgm = WORK / "bgm.wav"
    run(["-f", "lavfi", "-i", "sine=frequency=220:duration=3", "-c:a", "pcm_s16le", str(bgm)])
    out["bgm"] = bgm

    frame = WORK / "frame.png"
    run(["-f", "lavfi", "-i", "testsrc2=size=1280x720:duration=1", "-frames:v", "1", str(frame)])
    out["frame"] = frame
    return out


# ---------------------------------------------------------------- P1 封面

COVER_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cover,{font},96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,3,5,60,60,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:10.00,Cover,,0,0,0,,标题测试 ABC
"""

COVER_SRT = """1
00:00:00,000 --> 00:00:10,000
标题测试 ABC

"""


def p1_cover(sources: dict[str, Path]) -> None:
    frame = sources["frame"]

    # --- A: drawtext（复现 R26）---
    try:
        r = run(["-i", str(frame), "-vf",
                 "drawtext=text='测试ABC':fontsize=72:fontcolor=white:"
                 "x=(w-text_w)/2:y=h-th-80:box=1:boxcolor=black@0.5",
                 "-frames:v", "1", str(WORK / "cover_drawtext.jpg")])
        crash = r.returncode not in (0, 1) or "Segmentation" in (r.stderr or "")
        rec("P1-A drawtext", r.returncode == 0 and not crash,
            f"rc={r.returncode} " + (r.stderr or "").strip().splitlines()[-1][:120] if r.stderr else f"rc={r.returncode}")
    except subprocess.TimeoutExpired:
        rec("P1-A drawtext", False, "超时（疑似卡死）")

    # --- B: libass subtitles 渲标题（做成 ASS 再合成）---
    fonts = _px_fonts()
    ass = WORK / "cover.ass"
    ass.write_text(COVER_ASS.format(font=fonts[0] if fonts else "Arial"), encoding="utf-8")
    try:
        r = run(["-i", str(frame), "-vf", f"subtitles={ass.name}", "-frames:v", "1",
                 str(WORK / "cover_ass.jpg")], cwd=WORK)
        img_ok = r.returncode == 0 and (WORK / "cover_ass.jpg").exists()
        rec("P1-B libass subtitles", img_ok,
            f"rc={r.returncode} size={_size(WORK / 'cover_ass.jpg')} " + (r.stderr or "").strip().splitlines()[-1][:100] if not img_ok else f"出图 {_size(WORK / 'cover_ass.jpg')}")
    except subprocess.TimeoutExpired:
        rec("P1-B libass subtitles", False, "超时")

    # --- C: PIL 画字 ---
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        pil_ok = True
    except Exception as exc:  # noqa: BLE001
        pil_ok = False
        rec("P1-C Pillow", False, f"未安装：{exc}")
    if pil_ok:
        rec("P1-C Pillow", True, "可用（已安装）")

    # --- D: 中文字体可用性（决定方案可行性）---
    rec("P1-D 中文字体", bool(fonts), f"找到 {len(fonts)} 个：{fonts[:3]}")


def _px_fonts() -> list[str]:
    try:
        r = subprocess.run([str(FFMPEG), "-hide_banner", "-list_fonts"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        names = []
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line or line.startswith("Fonts"):
                continue
            name = line.split(",")[0].strip()
            if name and any("\u4e00" <= ch <= "\u9fff" for ch in name):
                names.append(name)
        return names
    except Exception:  # noqa: BLE001
        return []


def _size(p: Path) -> str:
    if not p.exists():
        return "缺失"
    return f"{p.stat().st_size} B"


# ---------------------------------------------------------------- P2 xfade

def p2_xfade(sources: dict[str, Path]) -> None:
    a, b = sources.get("a"), sources.get("b")
    if not a or not b:
        rec("P2 xfade", False, "缺素材")
        return

    # xfade 要求两路输入分辨率/像素格式/帧率一致；音频需 acrossfade
    out = WORK / "xfade.mp4"
    try:
        r = run(["-i", str(a), "-i", str(b),
                 "-filter_complex",
                 "[0:v][1:v]xfade=transition=fade:duration=0.5:offset=2.5[v];"
                 "[0:a][1:a]acrossfade=d=0.5[a]",
                 "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", str(out)])
        dur = _probe_dur(out)
        # 预期总时长 = 3 + 3 - 0.5 = 5.5，再减去 audio 交叉
        ok = r.returncode == 0 and dur and abs(dur - 5.5) < 0.35
        rec("P2-A xfade 视频", bool(ok), f"rc={r.returncode} 时长={dur} (预期≈5.5) " +
            ("" if ok else (r.stderr or "").strip().splitlines()[-1][:120]))
    except subprocess.TimeoutExpired:
        rec("P2-A xfade 视频", False, "超时")

    # 分辨率不一致会怎样？—— 这是乙方案/合集模式的真实风险
    c = WORK / "c.mp4"
    run(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=660:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(c)])
    r = run(["-i", str(a), "-i", str(c),
             "-filter_complex", "[0:v][1:v]xfade=transition=fade:duration=0.5:offset=2.5[v]",
             "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(WORK / "xfade_bad.mp4")])
    msg = (r.stderr or "")
    bad = r.returncode != 0
    rec("P2-B 尺寸不一致时 xfade", True,
        ("报错（可预期）：" + msg.strip().splitlines()[-1][:110]) if bad else "竟然通过（需注意静默错配）")

    # 单文件淡入淡出（乙方案下的真实用法）
    out2 = WORK / "fade.mp4"
    r = run(["-i", str(a), "-vf", "fade=t=in:st=0:d=0.5,fade=t=out:st=2.5:d=0.5",
             "-af", "afade=t=in:st=0:d=0.5,afade=t=out:st=2.5:d=0.5",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out2)])
    rec("P2-C 单文件 fade in/out", r.returncode == 0, f"rc={r.returncode} 时长={_probe_dur(out2)}")


def _probe_dur(p: Path) -> float | None:
    probe = ft.find_ffprobe()
    if not probe or not p.exists():
        return None
    try:
        r = subprocess.run([str(probe), "-v", "error", "-show_entries", "format=duration",
                            "-of", "json", str(p)], capture_output=True, text=True, timeout=30)
        return round(float(json.loads(r.stdout)["format"]["duration"]), 3)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- P3 ducking

def p3_ducking(sources: dict[str, Path]) -> None:
    voice, bgm = sources.get("voice"), sources.get("bgm")
    if not voice or not bgm:
        rec("P3 ducking", False, "缺素材")
        return

    out = WORK / "duck.wav"
    fc = ("[1:a]volume=1.0[bgm];"
          "[bgm][0:a]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300:makeup=1[ducked];"
          "[0:a][ducked]amix=inputs=2:duration=longest:normalize=0[out]")
    r = run(["-i", str(voice), "-i", str(bgm), "-filter_complex", fc,
             "-map", "[out]", "-c:a", "pcm_s16le", str(out)])
    if r.returncode != 0:
        rec("P3-A ducking 执行", False, (r.stderr or "").strip().splitlines()[-1][:140])
        return
    rec("P3-A ducking 执行", True, f"rc=0 产出 {_size(out)}")

    # 量化：分别测「有声段(0.3~1.2s)」与「无声段(1.8~2.8s)」的 BGM 电平
    loud = _rms(out, 0.3, 1.2)
    quiet = _rms(out, 1.8, 2.8)
    if loud is None or quiet is None:
        rec("P3-B ducking 量化", False, "RMS 测量失败")
        return
    delta = quiet - loud
    rec("P3-B ducking 量化", delta > 1.0,
        f"有声段 {loud:.2f}dB / 无声段 {quiet:.2f}dB ⇒ BGM 被压低 {delta:.2f}dB")


def _rms(path: Path, start: float, dur: float) -> float | None:
    r = run(["-ss", str(start), "-t", str(dur), "-i", str(path),
             "-af", "volumedetect", "-f", "null", "-"])
    for line in (r.stderr or "").splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].strip().split()[0])
            except Exception:  # noqa: BLE001
                return None
    return None


# ---------------------------------------------------------------- main

def main() -> int:
    print(f"workdir = {WORK}")
    print(f"ffmpeg  = {FFMPEG}\n")

    print("=== 滤镜可用性 ===")
    filters = ft.list_filters(FFMPEG, timeout=30)
    need = ["xfade", "acrossfade", "sidechaincompress", "amix", "drawtext",
            "subtitles", "ass", "volume", "afade", "fade", "concat", "overlay", "scale"]
    missing = [f for f in need if f not in filters]
    rec("F0 滤镜可用性", not missing, f"缺失：{missing or '无'}")

    print("\n=== 生成素材 ===")
    src = make_sources()
    t0 = time.time()

    print("\n=== P1 封面方案 ===")
    p1_cover(src)

    print("\n=== P2 转场 ===")
    p2_xfade(src)

    print("\n=== P3 配乐 ducking ===")
    p3_ducking(src)

    print(f"\n总耗时 {time.time() - t0:.1f}s")

    print("\n" + "=" * 64)
    print("汇总")
    print("=" * 64)
    bad = 0
    for tag, st, detail in RESULT:
        if st == "FAIL":
            bad += 1
        print(f"{st:4} | {tag:26} | {detail}")
    print(f"\n失败 {bad} 项 / 共 {len(RESULT)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
