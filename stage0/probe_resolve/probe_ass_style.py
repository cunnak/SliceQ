# -*- coding: utf-8 -*-
"""阶段 3 前置探针 D — ASS 双样式字幕（样式面板的地基）

样式面板要主/副字幕**完全独立**控制：
  字体 / 字号 / 字间距 / 颜色 / 边框颜色 / 边框粗细 / 垂直位置

SRT + force_style 做不到（force_style 是全局覆盖，对所有字幕统一生效）。
必须走 **ASS**：在 [V4+ Styles] 里定义 Main / Sub 两套样式，
每条字幕按层选择样式，垂直位置由各自的 MarginV 决定。

本探针要回答三件事：
  Q1. ASS 的两套样式能否在同一帧里独立生效？（字体/颜色/大小/位置都对）
  Q2. 中文字体名能否生效？（对比默认、微软雅黑、黑体、不存在字体）
  Q3. 颜色格式 —— ASS 用 &HAABBGGRR（**BGR 反转**），写错就会变色

⚠️ 已知风险：ASS 的 Side 与 SRT 不同，编码必须是 UTF-8；
   且 fontsdir 参数在 Windows 上必须转义（`\\:`）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent.parent
MEDIA = PROJ / "stage0" / "test_media" / "live_clip90.mp4"
WORK = PROJ / "stage0" / "test_media" / "ass_probe"

FFMPEG = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffmpeg.exe")
FONTS = Path(r"C:\Windows\Fonts")


def run(args, timeout=300, cwd=None):
    r = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace",
                       cwd=cwd)
    return r.returncode, r.stdout or "", r.stderr or ""


def esc(p: str) -> str:
    """滤镜参数里的 Windows 路径：双反斜杠转义冒号（已实测有效）"""
    return str(p).replace("\\", "/").replace(":", r"\\:")


def ass_escape(text: str) -> str:
    """ASS 文本里 `{` `}` 有特殊含义，需转义为全角"""
    return text.replace("{", "｛").replace("}", "｝").replace("\n", r"\N")


# 素材是竖屏 720x1280
RES_X, RES_Y = 720, 1280


def build_ass(main_font: str, sub_font: str, *,
              main_color="#00FF00", sub_color="#FFD700",
              main_size=42, sub_size=30,
              outline=2.0, spacing=3.2,
              margin_main=170, margin_sub=110) -> str:
    """生成双语 ASS。颜色按 ASS 规范转成 &HAABBGGRR。"""

    def col(hex_rgb: str, alpha: int = 0) -> str:
        h = hex_rgb.lstrip("#")
        r, g, b = h[0:2], h[2:4], h[4:6]
        # ⚠️ ASS 是 ABGR 顺序（B 与 R 对调），不是 RGB
        return f"&H{alpha:02X}{b}{g}{r}".upper()

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {RES_X}
PlayResY: {RES_Y}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{main_font},{main_size},{col(main_color)},&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,{spacing},0,1,{outline},0,2,20,20,{margin_main},1
Style: Sub,{sub_font},{sub_size},{col(sub_color)},&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0.8,0,1,{outline},0,2,20,20,{margin_sub},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    # 同一时间窗，两套样式各一条 → 这是样式面板要的形态
    pairs = [
        (0.0, 3.0, "必然有些真理永远无法证明", "这是一段用于测试字幕预览和样式设置的长文本内容"),
        (3.0, 6.0, "第二句主字幕短一些", "Second line: another subtitle for preview"),
    ]
    for a, b, main, sub in pairs:
        ts = f"{int(a // 3600)}:{int(a % 3600 // 60):02d}:{a % 60:05.2f}"
        te = f"{int(b // 3600)}:{int(b % 3600 // 60):02d}:{b % 60:05.2f}"
        ev.append(f"Dialogue: 0,{ts},{te},Main,,0,0,0,,{ass_escape(main)}")
        ev.append(f"Dialogue: 0,{ts},{te},Sub,,0,0,0,,{ass_escape(sub)}")
    return header + "\n".join(ev) + "\n"


def burn(ass: Path, out: Path, *, cwd: Path, extra_vf: str = "") -> tuple[int, str]:
    vf = f"subtitles={ass.name}{extra_vf}" if cwd else f"subtitles={esc(str(ass))}{extra_vf}"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-ss", "0", "-t", "6", "-i", str(MEDIA),
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
           "-an", out]
    rc, _, err = run(cmd, cwd=str(cwd) if cwd else None)
    return rc, err


def frame(video: Path, out: Path, *, at: str = "1.0", crop_bottom: bool = True):
    vf = "crop=iw:ih*0.35:0:ih*0.62" if crop_bottom else "null"
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-ss", at, "-i", video, "-frames:v", "1", "-vf", vf, out])


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)

    # ── Q1 + Q3：两套样式独立生效 + 颜色格式 ──────────────
    print("=" * 76)
    print("Q1/Q3 · 双样式 + 颜色格式（主=亮绿 #00FF00，副=金黄 #FFD700）")
    print("=" * 76)

    ass = WORK / "dual.ass"
    ass.write_text(build_ass("Microsoft YaHei", "Microsoft YaHei"),
                   encoding="utf-8")
    out = WORK / "dual.mp4"
    rc, err = burn(ass, out, cwd=WORK)
    print(f"  烧录 rc={rc}  产出 {out.stat().st_size if out.exists() else 0} B")
    if err.strip():
        for ln in err.strip().splitlines()[:4]:
            print("    ", ln[:115])
    if out.exists():
        frame(out, WORK / "shot_dual.png")
        print(f"  抽帧 → shot_dual.png（下 35% 区域）")

    # ── Q2：字体生效性 ──────────────────────────────────
    print()
    print("=" * 76)
    print("Q2 · 字体生效性（同一 ASS，只换字体名）")
    print("=" * 76)

    cases = [
        ("默认(空)", "", ""),
        ("Microsoft YaHei", "Microsoft YaHei", "Microsoft YaHei"),
        ("SimHei 黑体", "SimHei", "SimHei"),
        ("Source Han Sans 思源黑体", "Source Han Sans CN", "Source Han Sans CN"),
        ("*** 不存在的字体 ***", "NoSuchFontXYZ", "NoSuchFontXYZ"),
    ]
    shots = []
    for label, mf, sf in cases:
        a = WORK / f"f_{abs(hash(label)) % 10 ** 6}.ass"
        a.write_text(build_ass(mf, sf), encoding="utf-8")
        o = WORK / f"v_{abs(hash(label)) % 10 ** 6}.mp4"
        rc, err = burn(a, o, cwd=WORK)
        shot = WORK / f"s_{abs(hash(label)) % 10 ** 6}.png"
        if o.exists():
            frame(o, shot)
        print(f"  {'OK  ' if rc == 0 else 'FAIL'} {label:28s} rc={rc} "
              f"帧 {shot.stat().st_size if shot.exists() else 0} B")
        if err.strip():
            for ln in err.strip().splitlines()[:2]:
                print(f"        {ln[:112]}")
        if shot.exists():
            shots.append((label, shot))

    # 拼成一张对照图
    if len(shots) >= 2:
        args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
        for _, s in shots:
            args += ["-i", str(s)]
        args += ["-filter_complex",
                 f"vstack=inputs={len(shots)}", str(WORK / "fonts_grid.png")]
        rc, _, err = run(args)
        print(f"  字体对照图 → fonts_grid.png rc={rc}")
        if err.strip():
            print("    ", err.strip().splitlines()[0][:110])

    # ── Q4：检查系统里有哪些中文字体可选 ──────────────────
    print()
    print("=" * 76)
    print("Q4 · 系统字体目录里的中文字体（样式面板的候选项来源）")
    print("=" * 76)
    hits = []
    for pat in ("msyh*", "simhei*", "simsun*", "simkai*", "Deng*",
                "SourceHan*", "*YaHei*", "FZ*", "LXGW*", "*黑*", "*宋*"):
        for p in FONTS.glob(pat):
            if p.suffix.lower() in (".ttf", ".ttc", ".otf"):
                hits.append(p.name)
    uniq = sorted(set(hits))
    print(f"  命中 {len(uniq)} 个：")
    for n in uniq[:24]:
        print("    ", n)
    if len(uniq) > 24:
        print(f"     ... 另有 {len(uniq) - 24} 个")

    return 0


if __name__ == "__main__":
    sys.exit(main())
