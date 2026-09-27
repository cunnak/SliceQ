# -*- coding: utf-8 -*-
r"""whisper 滤镜传参探针 —— 已定论版。

═══════════════════════════════════════════════════════════════
结论（2026-09-25 实测）
═══════════════════════════════════════════════════════════════
ffmpeg 滤镜参数用 `:` 分隔，Windows 路径的 `C:` 会撞车。

**ffmpeg 的滤镜串要穿过两层解析：**
  第一层：按 `,` `;` 切分 filtergraph
  第二层：在单个 filter 内按 `:` 切分参数

⇒ **单个反斜杠 `\:` 在第一层就被吃掉，到不了第二层。**
⇒ **必须写两个反斜杠 `\\:`**。

实测对照（同一份模型、同一段素材）：

    model=C:/Users/...      ❌ No option name near '/Users/...'
    model=C\:/Users/...     ❌ 同上（单反斜杠不够）
    model='C:/Users/...'    ❌ 同上（引号不管用）
    model=C\\:/Users/...    ✅ 成功

**所有路径型滤镜参数都要这样处理**，不只是 model，destination 也一样。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

FF = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffmpeg.exe")
MODEL = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\models\ggml-tiny.bin")
MEDIA = Path(r"C:\Users\<user>\WorkBuddy\2026-09-23-13-06-47\SliceQ\stage0\test_media\speech_video.mp4")
WORK = MEDIA.parent / "_whisper_probe"
WORK.mkdir(exist_ok=True)


def escape_filter_path(p: str | Path) -> str:
    """把 Windows 路径转成可安全放进 ffmpeg 滤镜参数的写法。

    两步：
      1. 反斜杠 → 正斜杠（ffmpeg 两种都吃，正斜杠更安全）
      2. **冒号 → 双反斜杠 + 冒号**（单个反斜杠过不了第一层解析）
    """
    return str(p).replace("\\", "/").replace(":", r"\\:")


def transcribe(media: Path, model: Path, dest: Path,
               language: str = "zh", use_gpu: bool = False,
               timeout: int = 600) -> list[dict]:
    """用 ffmpeg 的 whisper 滤镜转录。

    ⚠️ 输出是 **JSON Lines**（每行一个 {start,end,text} 对象），
       不是标准 JSON 文档 —— 直接 json.loads() 会报 "Extra data"。
       且 start/end 单位是**毫秒**（整数），不是秒。
    """
    af = (
        f"whisper=model={escape_filter_path(model)}"
        f":language={language}"
        f":format=json"
        f":destination={escape_filter_path(dest)}"
        f":use_gpu={'true' if use_gpu else 'false'}"
    )
    cmd = [str(FF), "-hide_banner", "-loglevel", "error",
           "-i", str(media), "-vn", "-af", af, "-f", "null", "-"]

    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    if r.stderr.strip():
        print("   stderr:", r.stderr.strip()[:300])

    if not dest.exists():
        return []

    segs: list[dict] = []
    for line in dest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            segs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return segs


def main() -> int:
    for p, n in ((FF, "ffmpeg"), (MODEL, "模型")):
        if not p.exists():
            print(f"缺少{n}：{p}")
            return 1

    print("=" * 74)
    print("whisper 滤镜实测（转义写法已定论）")
    print("=" * 74)
    print(f"模型: {MODEL.name}")
    print()

    cases = [
        (MEDIA, "speech_video.mp4（英文语音，4.2s）", "en"),
        (MEDIA.parent / "source.mp4", "source.mp4（12s，对照 subtitle.srt）", "zh"),
    ]

    import time
    for media, label, lang in cases:
        if not media.exists():
            print(f"--- 跳过 {label}（文件不存在）---\n")
            continue

        dest = WORK / (media.stem + f"_{lang}.json")
        dest.unlink(missing_ok=True)

        print("=" * 74)
        print(f"素材：{label}")
        print("=" * 74)
        t0 = time.time()
        try:
            segs = transcribe(media, MODEL, dest, language=lang)
        except Exception as exc:
            print(f"   ❌ 异常：{exc}\n")
            continue
        dt = time.time() - t0

        if not segs:
            print("   ❌ 未产出结果\n")
            continue

        print(f"   ✅ 成功  耗时 {dt:.1f}s  段数 {len(segs)}")
        for s in segs:
            print(f"      [{s.get('start'):>6} → {s.get('end'):>6} ms] "
                  f"{s.get('text', '').strip()}")
        print()

    # 对照标准答案
    srt = MEDIA.parent / "subtitle.srt"
    if srt.exists():
        print("=" * 74)
        print("标准答案 subtitle.srt（用于对照 source.mp4）")
        print("=" * 74)
        print(srt.read_text(encoding="utf-8").strip())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
