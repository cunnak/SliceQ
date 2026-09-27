# -*- coding: utf-8 -*-
r"""VAD 参数生效性验证。

═══════════════════════════════════════════════════════════════
要回答的问题
═══════════════════════════════════════════════════════════════
实测发现：在含 53.6 秒长静音的素材上，VAD 三个阈值（0.3/0.4/0.5）
**都没能减少静音区的幻觉**（23 段 → 23/23/22 段），但耗时涨了 4 倍。

在得出"VAD 无效"这个结论之前，必须先排除一个更基础的可能：
**`vad_model` / `vad_threshold` 参数根本没被 ffmpeg 解析。**

鉴别方法：
  1. 传一个**不存在的 vad_model 路径** —— 若不报错，说明参数被静默忽略
  2. 传一个**非法文件**（音频文件当模型）—— 同上
  3. 传**极端阈值**（0.001 / 0.999）—— 若段数与默认无异，说明阈值没起效
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FF = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffmpeg.exe")
MODELS = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\models")
WAV = Path(r"C:\Users\<user>\WorkBuddy\2026-09-23-13-06-47\SliceQ"
           r"\stage0\test_media\vad_test\live_silence.wav")
OUT = WAV.parent / "_param_probe.jsonl"
# 转义后的音频路径，用于"故意传非法文件"这一组
def esc(p) -> str:
    """Windows 路径 → ffmpeg 滤镜参数。

    ⚠️ 冒号必须是**双**反斜杠：滤镜串要穿过两层解析
    （先按 `,;` 分，再按 `:` 分），单反斜杠在到达第二层前就被吃掉了。
    这是 2026-09-25 实测踩过的坑，注释在此防止回退。
    """
    return str(p).replace("\\", "/").replace(":", "\\\\:")


WAV_ESC = esc(WAV)


BASE = (f"whisper=model={esc(MODELS / 'ggml-large-v3-turbo.bin')}"
        ":language=zh:format=json:use_gpu=true")
VAD6 = esc(MODELS / "ggml-silero-v6.2.0.bin")

CASES: dict[str, str] = {
    "1 基准（无 VAD）": BASE,
    "2 vad_model ← 不存在的文件": BASE + ":vad_model=" + esc(r"C:\nonexistent\fake.bin"),
    "3 vad_model ← 音频文件（非法）": BASE + ":vad_model=" + WAV_ESC,
    "4 vad_threshold = 0.001（极低）": BASE + f":vad_model={VAD6}:vad_threshold=0.001",
    "5 vad_threshold = 0.999（极高）": BASE + f":vad_model={VAD6}:vad_threshold=0.999",
    "6 vad_threshold = 0.4（本次取值）": BASE + f":vad_model={VAD6}:vad_threshold=0.4",
}


def count_segments() -> int:
    if not OUT.exists():
        return 0
    return sum(1 for ln in OUT.read_text(encoding="utf-8").splitlines() if ln.strip())


def main() -> int:
    for p, n in ((FF, "ffmpeg"), (WAV, "音频"), (MODELS / "ggml-large-v3-turbo.bin", "主模型")):
        if not p.exists():
            print(f"缺少 {n}: {p}")
            return 1

    print("=" * 78)
    print("VAD 参数生效性验证")
    print("=" * 78)
    print(f"音频: {WAV.name}（静音区间 34.0~87.7s，共 53.6s）")
    print("-" * 78)

    for label, af in CASES.items():
        OUT.unlink(missing_ok=True)
        cmd = [str(FF), "-hide_banner", "-loglevel", "error",
               "-i", str(WAV), "-vn", "-af", af, "-f", "null", "-"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=900, encoding="utf-8", errors="replace")
            rc = r.returncode
            err = (r.stderr or "").strip().splitlines()
        except subprocess.TimeoutExpired:
            rc, err = -1, ["超时"]

        n = count_segments()
        print(f"\n{label}")
        print(f"  段数 {n:4d}   returncode {rc}")
        if err:
            for line in err[:3]:
                print(f"  stderr: {line[:120]}")

    print()
    print("=" * 78)
    print("判读方法")
    print("=" * 78)
    print("  第 2 / 3 组若 rc=0 且无报错 → 参数被**静默忽略**，VAD 从未生效")
    print("  第 4 / 5 组若段数与第 6 组相同 → 阈值**未生效**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
