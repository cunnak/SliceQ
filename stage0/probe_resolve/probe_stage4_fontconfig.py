# -*- coding: utf-8 -*-
"""阶段 4.0 前置实测（第二轮）：fontconfig 修复 / libass 字体生效性 / ducking 精确测量

第一轮暴露的三个问题：
  P4  drawtext 崩溃根因 = fontconfig 找不到 fonts.conf（rc=0xC0000005）
      → 自建 fonts.conf 能否修好？修好后 drawtext 是否可用？
  P5  `ffmpeg -list_fonts` 返回 0 个中文字体 —— 那么 libass 渲染中文时
      究竟用了哪个字体？**用户在样式面板选的字体到底生效了没有？**
      （若两字体渲染结果逐像素相同 ⇒ 字体设置无效 = 静默 fallback）
  P6  第一轮 ducking 测量把「人声+BGM 混合信号」当成了「BGM」，
      有声段自然更响 ⇒ 判据本身错误。改为只测 ducked 后的 BGM 单轨。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq import ffmpeg_tools as ft  # noqa: E402

FFMPEG = ft.find_ffmpeg()
WORK = Path(tempfile.mkdtemp(prefix="sliceq_p4b_"))
OUT = ROOT / "reports" / "assets"
OUT.mkdir(parents=True, exist_ok=True)
RESULT: list[tuple[str, str, str]] = []

SENTENCE = "扫码过来叫我扫一下我付钱"

ASS_TMPL = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cover,{font},72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,{bold},0,0,0,100,100,0,0,1,4,2,2,60,60,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:10.00,Cover,,0,0,0,,{text}
"""

FONTS_CONF = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <dir>{fonts_dir}</dir>
  <cachedir>{cache_dir}</cachedir>
  <config></config>
</fontconfig>
"""


def rec(tag: str, ok: bool, detail: str) -> None:
    RESULT.append((tag, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {tag} :: {detail}")


def run(args, *, cwd=None, env=None, timeout=120):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run([str(FFMPEG), "-hide_banner", "-nostdin", "-y", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(cwd) if cwd else None,
                          env=e, timeout=timeout)


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()[:12] if p.exists() else "缺失"


# ------------------------------------------------------------ P4 fontconfig

def p4_fontconfig() -> dict[str, str]:
    fonts_dir = Path("C:/Windows/Fonts")
    conf_dir = WORK / "fc"
    conf_dir.mkdir(exist_ok=True)
    cache = WORK / "fc-cache"
    cache.mkdir(exist_ok=True)
    conf = conf_dir / "fonts.conf"
    conf.write_text(FONTS_CONF.format(fonts_dir=fonts_dir.as_posix(),
                                      cache_dir=cache.as_posix()), encoding="utf-8")
    env = {"FONTCONFIG_FILE": str(conf), "FONTCONFIG_PATH": str(conf_dir)}

    # --- 自建 fonts.conf 后，-list_fonts 能否列出中文字体 ---
    r = subprocess.run([str(FFMPEG), "-hide_banner", "-list_fonts"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env={**os.environ, **env}, timeout=60)
    cjk = []
    for line in (r.stdout or "").splitlines():
        name = line.strip().split(",")[0].strip()
        if name and any("\u4e00" <= ch <= "\u9fff" for ch in name):
            cjk.append(name)
    rec("P4-A fonts.conf 生效", len(cjk) > 0,
        f"列出中文字体 {len(cjk)} 个：{cjk[:5]}" if cjk else
        f"仍为 0。stderr={((r.stderr or '').strip().splitlines() or [''])[-1][:100]}")

    # --- 带 fonts.conf 后 drawtext 是否还崩 ---
    frame = WORK / "f.png"
    run(["-f", "lavfi", "-i", "testsrc2=size=1280x720:duration=1", "-frames:v", "1", str(frame)])
    r = run(["-i", str(frame), "-vf",
             "drawtext=text='测试ABC':fontsize=72:fontcolor=white:"
             "x=(w-text_w)/2:y=h-th-80:box=1:boxcolor=black@0.5",
             "-frames:v", "1", str(WORK / "dt_fc.jpg")], env=env)
    ok = r.returncode == 0 and (WORK / "dt_fc.jpg").exists()
    rec("P4-B drawtext + fonts.conf", ok,
        f"rc={r.returncode} 出图={_size(WORK / 'dt_fc.jpg')}" if ok
        else f"rc={r.returncode} {(r.stderr or '').strip().splitlines()[-1][:120]}")
    return env


def _size(p: Path) -> str:
    return f"{p.stat().st_size}B" if p.exists() else "缺失"


# ------------------------------------------------------------ P5 字体生效性

def p5_font_effective(env: dict[str, str]) -> None:
    frame = WORK / "f.png"
    if not frame.exists():
        run(["-f", "lavfi", "-i", "testsrc2=size=1280x720:duration=1", "-frames:v", "1", str(frame)])

    # 用系统里确实存在的两组字体：黑体(SimHei) / 宋体(SimSun)
    pairs = [("SimHei", "SimSun", False), ("Microsoft YaHei", "SimHei", True)]
    for fa, fb, bold in pairs:
        outs = []
        for f in (fa, fb):
            ass = WORK / f"fc_{f.replace(' ', '_')}.ass"
            ass.write_text(ASS_TMPL.format(font=f, text=SENTENCE, bold="-1" if bold else "0"),
                           encoding="utf-8")
            img = WORK / f"fc_{f.replace(' ', '_')}.jpg"
            r = run(["-i", str(frame), "-vf", f"subtitles={ass.name}", "-frames:v", "1",
                     str(img)], cwd=WORK, env=env)
            outs.append(img if r.returncode == 0 else None)
        if not all(outs):
            rec(f"P5 字体生效[{fa} vs {fb}]", False, "渲染失败")
            continue
        same = md5(outs[0]) == md5(outs[1])
        rec(f"P5 字体生效[{fa} vs {fb}]", not same,
            f"MD5 {md5(outs[0])} vs {md5(outs[1])} ⇒ " +
            ("**两字体输出完全相同 = 字体设置未生效（静默 fallback）**" if same
             else "输出不同 = 字体确实生效"))

    # 存档一张封面图供判读中文是否渲染正常（不是豆腐块）
    ass = WORK / "fc_SimHei.ass"
    if ass.exists():
        final = OUT / "probe_cover_cjk.jpg"
        r = run(["-i", str(frame), "-vf", f"subtitles={ass.name}", "-frames:v", "1",
                 str(final)], cwd=WORK, env=env)
        rec("P5 存档封面样张", r.returncode == 0 and final.exists(),
            f"{final}" if final.exists() else "未产出")


# ------------------------------------------------------------ P6 ducking

def p6_ducking() -> None:
    voice = WORK / "voice.wav"
    run(["-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1.5",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[o]", "-map", "[o]",
         "-c:a", "pcm_s16le", str(voice)])
    bgm = WORK / "bgm.wav"
    run(["-f", "lavfi", "-i", "sine=frequency=220:duration=3", "-c:a", "pcm_s16le", str(bgm)])

    base_loud = _rms(bgm, 0.3, 1.2)
    base_quiet = _rms(bgm, 1.8, 2.8)

    best = None
    for thr, ratio in ((0.3, 8), (0.1, 20), (0.05, 8)):
        ducked = WORK / f"ducked_{thr}_{ratio}.wav"
        fc = ("[1:a]volume=1.0[b];"
              f"[b][0:a]sidechaincompress=threshold={thr}:ratio={ratio}:"
              "attack=5:release=250:makeup=1[o]")
        r = run(["-i", str(voice), "-i", str(bgm), "-filter_complex", fc,
                 "-map", "[o]", "-c:a", "pcm_s16le", str(ducked)])
        if r.returncode != 0:
            continue
        loud = _rms(ducked, 0.3, 1.2)   # 人声在响 → BGM 应被压低
        quiet = _rms(ducked, 1.8, 2.8)  # 静音 → BGM 恢复原音量
        if loud is None or quiet is None:
            continue
        drop = quiet - loud
        print(f"   thr={thr} ratio={ratio}: 人声段 {loud:.2f}dB / 静音段 {quiet:.2f}dB"
              f" ⇒ 压低 {drop:.2f}dB")
        if best is None or drop > best[0]:
            best = (drop, thr, ratio, loud, quiet)

    rec("P6-A BGM 基线电平一致", base_loud is not None and base_quiet is not None
        and abs((base_loud or 0) - (base_quiet or 0)) < 0.5,
        f"原始 BGM 两段 {base_loud:.2f} / {base_quiet:.2f} dB（应基本相等，证明测量可比）")

    if best:
        drop, thr, ratio, loud, quiet = best
        rec("P6-B ducking 有效", drop >= 4.0,
            f"最佳 thr={thr} ratio={ratio} ⇒ 压低 {drop:.2f}dB"
            f"（人声段 {loud:.2f} / 静音段 {quiet:.2f}）" +
            ("" if drop >= 4.0 else "  ← 抑制不足，需调参"))
        (WORK / "duck_best.json").write_text(
            json.dumps({"threshold": thr, "ratio": ratio, "drop_db": round(drop, 2)}),
            encoding="utf-8")


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


def main() -> int:
    print(f"workdir = {WORK}\n")
    print("=== P4 fontconfig 修复 ===")
    env = p4_fontconfig()
    print("\n=== P5 libass 字体生效性 ===")
    p5_font_effective(env)
    print("\n=== P6 ducking 精确测量 ===")
    p6_ducking()

    print("\n" + "=" * 68)
    bad = [r for r in RESULT if r[1] == "FAIL"]
    for tag, st, detail in RESULT:
        print(f"{st:4} | {tag:30} | {detail}")
    print(f"\n失败 {len(bad)} / 共 {len(RESULT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
