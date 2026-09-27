# -*- coding: utf-8 -*-
r"""真实直播素材的中文转录质量对比。

目的：回答一个问题 —— **ffmpeg 的 whisper 滤镜，在中文直播场景下能不能用？**

背景：阶段 2 前置实测发现，原有的"测试素材"其实没有中文语音
（`subtitle.srt` 是给切片演示手写的示例文案，不是转录标准答案）。
所以中文质量一直是个空白。

本脚本用**真实直播录像**（用户提供）截取的 60 秒音频，
跑 tiny / base / small 三档模型，对比质量与耗时。

用法：
    python probe_live_asr_quality.py <音频文件> [更多音频...]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

FF = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffmpeg.exe")
MODEL_DIR = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\models")

MODELS = ["ggml-tiny.bin", "ggml-base.bin", "ggml-small.bin"]


def escape_filter_path(p: str | Path) -> str:
    r"""Windows 路径 → 可放进 ffmpeg 滤镜参数的安全写法。

    ⚠️ 冒号必须转成 **双** 反斜杠：ffmpeg 的滤镜串要穿过两层解析
       （先按 ,; 分 filtergraph，再按 : 分参数），单反斜杠在第一层就被消费。
    """
    return str(p).replace("\\", "/").replace(":", r"\\:")


def transcribe(media: Path, model: Path, dest: Path,
               language: str = "zh", use_gpu: bool = False,
               timeout: int = 1800) -> tuple[list[dict], float]:
    """返回 (分段列表, 耗时秒数)。

    ⚠️ 输出是 **JSON Lines**，不是 JSON 文档；start/end 单位是**毫秒**。
    """
    import subprocess

    af = (
        f"whisper=model={escape_filter_path(model)}"
        f":language={language}"
        f":format=json"
        f":destination={escape_filter_path(dest)}"
        f":use_gpu={'true' if use_gpu else 'false'}"
    )
    cmd = [str(FF), "-hide_banner", "-loglevel", "error",
           "-i", str(media), "-vn", "-af", af, "-f", "null", "-"]

    t0 = time.time()
    subprocess.run(cmd, capture_output=True, text=True,
                   timeout=timeout, encoding="utf-8", errors="replace")
    dt = time.time() - t0

    segs: list[dict] = []
    if dest.exists():
        for line in dest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    import json
                    segs.append(json.loads(line))
                except Exception:
                    pass
    return segs, dt


def audio_duration(path: Path) -> float:
    """读音频时长。"""
    import json
    import subprocess
    probe = FF.with_name("ffprobe.exe")
    out = subprocess.run(
        [str(probe), "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace").stdout
    try:
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return 0.0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    audios = [Path(a) for a in sys.argv[1:]]
    for a in audios:
        if not a.exists():
            print(f"找不到音频：{a}")
            return 1

    print("=" * 78)
    print("真实直播素材 · 中文转录质量对比")
    print("=" * 78)

    results: dict[str, dict] = {}

    for audio in audios:
        dur = audio_duration(audio)
        print(f"\n音频：{audio.name}   时长 {dur:.1f}s")
        print("-" * 78)

        for mname in MODELS:
            model = MODEL_DIR / mname
            if not model.exists():
                print(f"  [跳过] {mname} 未安装")
                continue

            dest = audio.parent / f"{audio.stem}__{mname}.jsonl"
            dest.unlink(missing_ok=True)

            segs, dt = transcribe(audio, model, dest)
            rtf = dt / dur if dur else 0

            n_chars = sum(len(s.get("text", "").strip()) for s in segs)
            print(f"\n  ┌─ {mname}")
            print(f"  │  耗时 {dt:.1f}s  （{rtf:.2f}× 实时率）"
                  f"   段数 {len(segs)}   字符数 {n_chars}")
            print(f"  └─ 转录内容：")
            for s in segs:
                st = s.get("start", 0) / 1000
                en = s.get("end", 0) / 1000
                txt = s.get("text", "").strip()
                print(f"       [{st:6.1f}s → {en:6.1f}s]  {txt}")

            results.setdefault(mname, {})[audio.name] = {
                "segments": segs, "time": dt, "rtf": rtf,
                "chars": n_chars,
            }

    # ── 汇总 ──────────────────────────────────────
    print("\n" + "=" * 78)
    print("汇总：速度与产出")
    print("=" * 78)
    for mname, per in results.items():
        for aname, r in per.items():
            print(f"  {mname:20s} {r['rtf']:.2f}× 实时   "
                  f"{r['chars']:5d} 字   {r['time']:6.1f}s")

    print("\n" + "=" * 78)
    print("解读提示")
    print("=" * 78)
    print("""
  实时率（×实时）的含义：3 小时直播要转录多久。
    1.0×  = 需要 3 小时   0.3× = 需要 54 分钟   0.1× = 需要 18 分钟

  请人工判读转录内容：
    · 语义是否连贯、是否像人话
    · 专业术语/人名是否离谱
    · 有没有大段幻觉（无中生有）
    · 有没有重复循环（whisper 的典型故障）
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
