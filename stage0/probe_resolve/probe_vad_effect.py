# -*- coding: utf-8 -*-
r"""VAD 对幻觉的抑制效果实测。

═══════════════════════════════════════════════════════════════
要回答的问题
═══════════════════════════════════════════════════════════════
真实直播素材实测发现：**24 个转录分段里有 7~8 段是幻觉**
（`謝謝收看!`、`(字幕:J Chong)`、`( 忘記了 請按旁邊的小鈴鐺 )` …），
即约三分之一的内容是无中生有。

这是 whisper 在**无语音片段**（静音 / 纯 BGM / 环境音）上的典型故障。

假设：**VAD（语音活动检测）先切掉非语音段，就能消掉这些幻觉。**

本脚本对比 "无 VAD" 与 "有 VAD（不同阈值）" 的产出。

⚠️ VAD 模型来自**另一个仓库**：`ggml-org/whisper-vad`
   （不在 `ggerganov/whisper.cpp` 里，找模型时容易找错地方）
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "q", HERE / "probe_live_asr_quality.py")
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)

MODEL_DIR = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\models")
FF = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffmpeg.exe")

# 已知的幻觉特征词（用于自动统计幻觉条数）
HALLUCINATION_MARKERS = [
    "謝謝收看", "谢谢收看", "謝謝觀看", "谢谢观看", "謝謝大家", "谢谢大家",
    "字幕:", "字幕：", "字幕製作", "字幕制作", "中文字幕",
    "小鈴鐺", "小铃铛", "請按", "请按", "訂閱", "订阅", "下次見", "下次见",
    "J Chong", "貝爾", "贝尔", "點贊", "点赞",
]


def is_hallucination(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    return any(m in t for m in HALLUCINATION_MARKERS)


def run_case(media: Path, model: Path, dest: Path,
             vad: Path | None = None, threshold: float = 0.5,
             use_gpu: bool = True, timeout: int = 1800):
    """跑一次转录，返回 (分段, 耗时)。"""
    af = (
        f"whisper=model={q.escape_filter_path(model)}"
        f":language=zh"
        f":format=json"
        f":destination={q.escape_filter_path(dest)}"
        f":use_gpu={'true' if use_gpu else 'false'}"
    )
    if vad:
        af += (f":vad_model={q.escape_filter_path(vad)}"
               f":vad_threshold={threshold}")

    dest.unlink(missing_ok=True)
    cmd = [str(FF), "-hide_banner", "-loglevel", "error",
           "-i", str(media), "-vn", "-af", af, "-f", "null", "-"]

    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    dt = time.time() - t0

    segs: list[dict] = []
    if dest.exists():
        for line in dest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    segs.append(json.loads(line))
                except Exception:
                    pass
    return segs, dt, (r.stderr or "").strip()


def report(label: str, segs: list[dict], dt: float, err: str) -> None:
    if err:
        print(f"  ⚠️ stderr: {err[:200]}")
    if not segs:
        print("  ❌ 无产出\n")
        return

    hallu = [s for s in segs if is_hallucination(s.get("text", ""))]
    chars = sum(len(s.get("text", "").strip()) for s in segs)
    print(f"  段数 {len(segs)}   疑似幻觉 {len(hallu)} 段 "
          f"({len(hallu)*100//max(len(segs),1)}%)   字符 {chars}   耗时 {dt:.1f}s")
    if hallu:
        print("  幻觉内容：")
        for s in hallu:
            print(f"     {s.get('start',0)/1000:6.1f}s  {s.get('text','')[:50]}")
    print()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    media = Path(sys.argv[1])
    if not media.exists():
        print(f"找不到音频：{media}")
        return 1

    model = MODEL_DIR / "ggml-large-v3-turbo.bin"
    vad6 = MODEL_DIR / "ggml-silero-v6.2.0.bin"
    vad5 = MODEL_DIR / "ggml-silero-v5.1.2.bin"

    for p, n in ((model, "large-v3-turbo"), (vad6, "VAD v6.2.0")):
        if not p.exists():
            print(f"缺少{n}：{p}")
            return 1

    dur = q.audio_duration(media)
    print("=" * 78)
    print("VAD 对幻觉的抑制效果实测")
    print("=" * 78)
    print(f"音频：{media.name}   时长 {dur:.1f}s")
    print(f"模型：large-v3-turbo（GPU）")
    print("-" * 78)

    print("\n【对照组】不使用 VAD")
    dest = media.parent / "vad_off.jsonl"
    segs, dt, err = run_case(media, model, dest, vad=None)
    report("无 VAD", segs, dt, err)

    for th in (0.3, 0.4, 0.5):
        print(f"【实验组】VAD silero-v6.2.0，阈值 {th}")
        dest = media.parent / f"vad_{th}.jsonl"
        segs, dt, err = run_case(media, model, dest, vad=vad6, threshold=th)
        report(f"VAD@{th}", segs, dt, err)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
