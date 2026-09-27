# -*- coding: utf-8 -*-
"""阶段 3 前置探针 B — 找出 `subtitles` 滤镜接受路径的正确写法

背景：`subtitles=C:/Users/...` 报 `Unable to parse "original_size"`。
      这是 Windows 路径冒号撞 ffmpeg 滤镜参数分隔符的经典问题，
      与 whisper 滤镜同源（那一处已用 `\\:` 解决）。

但 subtitles 有一个额外难点：它出现在 **filterchain** 里，
解析层数可能比 whisper 更多（filterchain 按 `,;` 分，再按 `:` 分参数）。

同时验证「相对路径 + cwd」这条绕行路线 —— 如果可行，它是**根治方案**：
路径里根本没有冒号，就不需要任何转义。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent.parent
MEDIA = PROJ / "stage0" / "test_media" / "live_clip90.mp4"
WORK = PROJ / "stage0" / "test_media" / "editor_probe2"

FFMPEG = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffmpeg.exe")
FONTS = Path(r"C:\Windows\Fonts")


def run(args: list[str], timeout: int = 180, cwd: str | None = None):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace", cwd=cwd)
    return r.returncode, r.stdout or "", r.stderr or ""


def esc_double(p: str) -> str:
    """双反斜杠（asr.py 已实测有效的那种）"""
    return str(p).replace("\\", "/").replace(":", r"\\:")


def esc_single(p: str) -> str:
    return str(p).replace("\\", "/").replace(":", r"\:")


def try_case(label: str, vf: str, *, cwd: str | None = None,
             out_name: str = "") -> bool:
    out = WORK / (out_name or f"o_{abs(hash(vf)) % 10 ** 6}.mp4")
    if out.exists():
        out.unlink()
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
           "-ss", "0", "-t", "4", "-i", str(MEDIA),
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
           "-an", str(out)]
    try:
        rc, _, err = run(cmd, cwd=cwd)
    except Exception as exc:
        print(f"  FAIL  {label:44s} 异常 {exc}")
        return False
    ok = rc == 0 and out.exists() and out.stat().st_size > 1000
    print(f"  {'OK  ' if ok else 'FAIL'}  {label:44s} rc={rc} "
          f"{out.stat().st_size if out.exists() else 0} B")
    if not ok and err.strip():
        first = [ln for ln in err.strip().splitlines()
                 if "Error" in ln or "Unable" in ln or "No option" in ln]
        for ln in (first or err.strip().splitlines())[:2]:
            print(f"        {ln[:118]}")
    return ok


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    srt = WORK / "t.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n这是中文字幕测试\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n第二行 ABC 123\n",
        encoding="utf-8")
    posix = srt.as_posix()
    print(f"SRT: {posix}")
    print(f"字体目录: {FONTS}")
    print()

    print("── 组 1：绝对路径的三种转义（无 fontsdir）──")
    try_case("A 原样           subtitles=C:/...", f"subtitles={posix}")
    try_case("B 单反斜杠       subtitles=C\\:/...", f"subtitles={esc_single(posix)}")
    try_case("C 双反斜杠       subtitles=C\\\\:/...", f"subtitles={esc_double(posix)}")

    print()
    print("── 组 2：双反斜杠 + fontsdir（两种转义组合）──")
    try_case("D fontsdir 双转义",
             f"subtitles={esc_double(posix)}:fontsdir={esc_double(str(FONTS))}")
    try_case("E fontsdir 单转义",
             f"subtitles={esc_double(posix)}:fontsdir={esc_single(str(FONTS))}")

    print()
    print("── 组 3：相对路径 + cwd（**根治方案**：路径里没有冒号）──")
    try_case("F 相对路径 + cwd",
             "subtitles=t.srt", cwd=str(WORK), out_name="rel.mp4")
    try_case("G 相对路径 + fontsdir 绝对(双转义)",
             f"subtitles=t.srt:fontsdir={esc_double(str(FONTS))}",
             cwd=str(WORK), out_name="rel_fonts.mp4")

    print()
    print("── 组 4：验证渲染结果（抽查是否有中文像素）──")
    for name in ("o_" + str(abs(hash(f"subtitles={esc_double(posix)}")) % 10 ** 6) + ".mp4",
                 "rel.mp4", "rel_fonts.mp4"):
        p = WORK / name
        if not p.exists():
            continue
        # 抽一帧，看底部区域是否有接近纯白的像素（字幕是白字）
        frame = WORK / f"f_{name}.png"
        rc, _, _ = run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", "1", "-i", str(p), "-frames:v", "1",
                        "-vf", "crop=iw:ih/4:0:ih*3/4", str(frame)])
        if frame.exists():
            # 用 ffmpeg 的 signalstats 粗判亮度
            rc2, out2, _ = run([str(FFMPEG), "-hide_banner", "-nostats",
                                "-i", str(frame), "-vf", "signalstats",
                                "-f", "null", "-"])
            ymax = [ln for ln in out2.splitlines() if "YMAX" in ln]
            print(f"  {name:12s} 底部裁剪帧 {frame.stat().st_size} B  "
                  f"{ymax[0].strip()[-32:] if ymax else '（无亮度信息）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
