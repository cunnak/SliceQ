# -*- coding: utf-8 -*-
r"""ffmpeg whisper 滤镜的 VAD 语义判别实验。

═══════════════════════════════════════════════════════════════
背景
═══════════════════════════════════════════════════════════════
实测发现：在含 53.6 秒长静音的素材上，VAD 三个阈值（0.3/0.4/0.5）
都没能减少静音区的幻觉（23 段 → 23/23/22 段），但耗时涨了 4 倍。

已排除的可能性：
  - 参数没被解析 → 排除（传非法模型路径时 ffmpeg 会报
    `failed to open VAD model` / `invalid model data (bad magic)`）

剩下的假设：
  H1: VAD 是"跳过非语音"的过滤器 → 纯静音应产出 0 段
  H2: VAD 只用于"辅助切分 / 改善时间戳" → 纯静音仍会产出幻觉

═══════════════════════════════════════════════════════════════
实验设计
═══════════════════════════════════════════════════════════════
用 `anullsrc` 生成一段 30 秒**纯数字静音**（绝对无语音），
分别在「无 VAD」「有 VAD」下转录，对比段数。

这是最干净的判据：纯静音若产出任何文本，只能是幻觉。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

FF = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffmpeg.exe")
MODELS = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\models")
WORK = Path(r"C:\Users\<user>\WorkBuddy\2026-09-23-13-06-47\SliceQ"
            r"\stage0\test_media\vad_test")
SILENCE_WAV = WORK / "pure_silence.wav"


def esc(p) -> str:
    """⚠️ 冒号必须**双**反斜杠——滤镜串要穿过两层解析。"""
    return str(p).replace("\\", "/").replace(":", "\\\\:")


def make_pure_silence() -> None:
    """生成 30 秒纯静音（16kHz 单声道，与 whisper 输入格式一致）。"""
    cmd = [str(FF), "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
           "-t", "30", "-c:a", "pcm_s16le", "-y", str(SILENCE_WAV)]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"已生成纯静音：{SILENCE_WAV.name}  "
          f"{SILENCE_WAV.stat().st_size / 1024:.0f} KB / 30.0s\n")


def transcribe(media: Path, tag: str, vad: Path | None = None,
               threshold: float = 0.5,
               min_sil: float | None = None) -> int:
    """跑一次转录，返回段数。"""
    dest = WORK / f"_sem_{tag}.jsonl"
    dest.unlink(missing_ok=True)

    af = (f"whisper=model={esc(MODELS / 'ggml-large-v3-turbo.bin')}"
          ":language=zh:format=json:use_gpu=true"
          f":destination={esc(dest)}")
    if vad:
        af += f":vad_model={esc(vad)}:vad_threshold={threshold}"
        if min_sil is not None:
            af += f":vad_min_silence_duration={min_sil}"

    cmd = [str(FF), "-hide_banner", "-loglevel", "error",
           "-i", str(media), "-vn", "-af", af, "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=900, encoding="utf-8", errors="replace")

    lines = []
    if dest.exists():
        lines = [l for l in dest.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"  {tag:38s} 段数 {len(lines):3d}   rc={r.returncode}")
    if lines:
        sample = lines[0][:100]
        print(f"       首段: {sample}")
    err = (r.stderr or '').strip()
    if err:
        print(f"       stderr: {err.splitlines()[0][:100]}")
    return len(lines)


def main() -> int:
    if not SILENCE_WAV.exists():
        make_pure_silence()

    VAD6 = MODELS / "ggml-silero-v6.2.0.bin"

    print("=" * 78)
    print("实验一：纯静音 30 秒 —— VAD 能否跳过？")
    print("=" * 78)
    n_off = transcribe(SILENCE_WAV, "silence_no_vad")
    n_on = transcribe(SILENCE_WAV, "silence_vad04", vad=VAD6, threshold=0.4)
    print()
    print(f"  判读：无VAD={n_off} 段，有VAD={n_on} 段")
    if n_on == 0 and n_off > 0:
        print("  ⇒ H1 成立：VAD **确实**跳过了非语音（纯静音下有效）")
    elif n_on > 0:
        print("  ⇒ H2 成立：VAD **不跳过**非语音，纯静音仍产出幻觉")
    else:
        print("  ⇒ 两组都为 0，需换素材重测")

    print()
    print("=" * 78)
    print("实验二：含静音真实素材 —— vad_min_silence_duration 是否影响？")
    print("=" * 78)
    REAL = WORK / "live_silence.wav"
    if REAL.exists():
        transcribe(REAL, "real_no_vad")
        transcribe(REAL, "real_vad04_default", vad=VAD6, threshold=0.4)
        transcribe(REAL, "real_vad04_minsil2", vad=VAD6, threshold=0.4, min_sil=2.0)
        transcribe(REAL, "real_vad08_minsil2", vad=VAD6, threshold=0.8, min_sil=2.0)
    else:
        print(f"  跳过（找不到 {REAL}）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
