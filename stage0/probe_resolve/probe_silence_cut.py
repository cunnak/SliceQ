# -*- coding: utf-8 -*-
r"""静音预切除方案 —— 替代 VAD 的正确解法。

═══════════════════════════════════════════════════════════════
为什么需要它
═══════════════════════════════════════════════════════════════
实测确认（2026-09-25）：

1. ffmpeg whisper 滤镜的 `vad_model` **不能消除静音区幻觉**。
   - 源码 `af_whisper.c:filter_frame()`：VAD 只决定"何时切分 buffer"，
     而转录起点**永远是 buffer 第 0 个样本** —— 开头静音照样进 whisper。
   - 实测：30 秒纯数字静音，无 VAD 出 11 段幻觉，有 VAD 也是 11 段。
   - 而耗时增加 4 倍。

2. 静音区幻觉的规模不小：140 秒素材里 53.6 秒静音，
   产生 23 段幻觉（占全部 51 段的 45%）。

═══════════════════════════════════════════════════════════════
本方案
═══════════════════════════════════════════════════════════════
    ① `silencedetect` 扫出静音区间（实测 2.5 小时音频仅需 ~5 秒，零成本）
    ② 取补集 = 语音区间，两侧各留 padding
    ③ 用 `aselect` + `asetpts` 把语音区间拼成一条连续音频
    ④ 转录这条音频 —— whisper 全程只见语音，**没有静音可供幻觉**
    ⑤ 用区间表把拼接后的时间戳映射回原时间轴

⑤ 是关键：拼接改变了时间轴，字幕/切点必须映射回去。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

FF = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffmpeg.exe")
MODELS = Path(r"C:\Users\<user>\AppData\Roaming\SliceQ\models")
WORK = Path(r"C:\Users\<user>\WorkBuddy\2026-09-23-13-06-47\SliceQ"
            r"\stage0\test_media\vad_test")

HALLUCINATION_MARKERS = [
    "謝謝收看", "谢谢收看", "謝謝觀看", "谢谢观看", "謝謝大家", "谢谢大家",
    "字幕:", "字幕：", "字幕製作", "字幕制作", "中文字幕",
    "小鈴鐺", "小铃铛", "請按", "请按", "訂閱", "订阅", "下次見", "下次见",
    "J Chong", "貝爾", "贝尔", "點贊", "点赞", "不吝", "打赏", "明镜",
]


def esc(p) -> str:
    r"""⚠️ 冒号必须**双**反斜杠：滤镜串要穿过两层解析。"""
    return str(p).replace("\\", "/").replace(":", "\\\\:")


def is_hallucination(text: str) -> bool:
    t = text.strip()
    return any(m in t for m in HALLUCINATION_MARKERS) if t else False


# ────────────────────────────────────────────────────────────
# ① 静音检测
# ────────────────────────────────────────────────────────────
def detect_silences(wav: Path, noise_db: float = -35.0,
                    min_dur: float = 1.0) -> list[tuple[float, float]]:
    """返回静音区间 [(start_s, end_s), ...]。"""
    cmd = [str(FF), "-hide_banner", "-nostats", "-i", str(wav),
           "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
           "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=1800, encoding="utf-8", errors="replace")
    log = r.stderr or ""

    sil: list[tuple[float, float]] = []
    cur_start: float | None = None
    for line in log.splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            cur_start = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and cur_start is not None:
            sil.append((cur_start, float(m.group(1))))
            cur_start = None
    return sil


def speech_segments(silences: list[tuple[float, float]], total: float,
                    pad: float = 0.25
                    ) -> list[tuple[float, float]]:
    """静音区间的补集 = 语音区间（两侧加 pad），并合并重叠。"""
    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in sorted(silences):
        if s > cursor:
            speech.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < total:
        speech.append((cursor, total))

    # 加 padding 并裁剪到边界
    padded = [(max(0.0, s - pad), min(total, e + pad)) for s, e in speech]

    # 合并重叠（padding 可能造成）
    merged: list[list[float]] = []
    for s, e in padded:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


# ────────────────────────────────────────────────────────────
# ③ 拼接
# ────────────────────────────────────────────────────────────
def build_select_expr(segments: list[tuple[float, float]]) -> str:
    """生成 aselect 表达式（比 atrim+concat 紧凑得多）。"""
    parts = [f"between(t,{s:.3f},{e:.3f})" for s, e in segments]
    return "+".join(parts)


def extract_speech(wav: Path, out_wav: Path,
                   segments: list[tuple[float, float]]) -> float:
    """把语音区间拼成连续音频，返回新时长（秒）。"""
    expr = build_select_expr(segments)
    af = f"aselect='{expr}',asetpts=N/SR/TB"
    cmd = [str(FF), "-hide_banner", "-loglevel", "error", "-i", str(wav),
           "-af", af, "-c:a", "pcm_s16le", "-y", str(out_wav)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"拼接失败: {(r.stderr or '')[:300]}")

    # 读新时长
    r2 = subprocess.run([str(FF).replace("ffmpeg.exe", "ffprobe.exe"),
                         "-v", "error", "-show_entries", "format=duration",
                         "-of", "csv=p=0", str(out_wav)],
                        capture_output=True, text=True, timeout=60)
    return float((r2.stdout or "0").strip() or 0)


# ────────────────────────────────────────────────────────────
# ⑤ 时间轴映射
# ────────────────────────────────────────────────────────────
def map_back(t: float, segments: list[tuple[float, float]]) -> float:
    """把"拼接后时间"映射回"原音频时间"。"""
    acc = 0.0
    for s, e in segments:
        dur = e - s
        if t < acc + dur:
            return s + (t - acc)
        acc += dur
    return segments[-1][1] if segments else t


# ────────────────────────────────────────────────────────────
# 转录
# ────────────────────────────────────────────────────────────
def transcribe(wav: Path, dest: Path, vad: Path | None = None,
               threshold: float = 0.4) -> list[dict]:
    dest.unlink(missing_ok=True)
    af = (f"whisper=model={esc(MODELS / 'ggml-large-v3-turbo.bin')}"
          ":language=zh:format=json:use_gpu=true"
          f":destination={esc(dest)}")
    if vad:
        af += f":vad_model={esc(vad)}:vad_threshold={threshold}"
    cmd = [str(FF), "-hide_banner", "-loglevel", "error",
           "-i", str(wav), "-vn", "-af", af, "-f", "null", "-"]
    subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                   encoding="utf-8", errors="replace")
    out = []
    if dest.exists():
        for line in dest.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def wav_duration(wav: Path) -> float:
    r = subprocess.run([str(FF).replace("ffmpeg.exe", "ffprobe.exe"),
                        "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(wav)],
                       capture_output=True, text=True, timeout=60)
    return float((r.stdout or "0").strip() or 0)


def summarise(label: str, segs: list[dict], dt: float) -> None:
    h = [s for s in segs if is_hallucination(s.get("text", ""))]
    chars = sum(len(s.get("text", "").strip()) for s in segs)
    print(f"  {label:32s} 段数 {len(segs):3d}  幻觉 {len(h):3d}  "
          f"字符 {chars:5d}  耗时 {dt:5.1f}s")


def main() -> int:
    for src, name in ((WORK / "live_silence.wav", "含长静音真实素材"),
                      (WORK / "pure_silence.wav", "纯静音")):
        if not src.exists():
            print(f"缺少 {src}")
            continue

        print("=" * 78)
        print(f"素材：{name}  ({src.name})")
        print("=" * 78)

        total = wav_duration(src)
        print(f"  原时长 {total:.1f}s")

        # ── ① 静音检测 ───────────────────────────────
        t0 = time.time()
        sils = detect_silences(src)
        detect_dt = time.time() - t0
        silsum = sum(e - s for s, e in sils)
        print(f"  静音检测：{len(sils)} 段，共 {silsum:.1f}s "
              f"({silsum / total * 100:.1f}%)，耗时 {detect_dt:.1f}s")

        # ── ② 语音区间 ───────────────────────────────
        segs_range = speech_segments(sils, total, pad=0.25)
        print(f"  语音区间：{len(segs_range)} 段，共 "
              f"{sum(e - s for s, e in segs_range):.1f}s")

        # ── ③ 拼接 ──────────────────────────────────
        spliced = WORK / f"{src.stem}_speech.wav"
        new_dur = extract_speech(src, spliced, segs_range)
        print(f"  拼接后：{spliced.name}  {new_dur:.1f}s  "
              f"（压缩 {total - new_dur:.1f}s）")

        print()
        print("  ── 转录对比 ──")

        t0 = time.time()
        a = transcribe(src, WORK / "_cmp_raw.jsonl",
                       vad=MODELS / "ggml-silero-v6.2.0.bin", threshold=0.4)
        summarise("A 原音频 + VAD@0.4", a, time.time() - t0)

        t0 = time.time()
        b = transcribe(src, WORK / "_cmp_spliced.jsonl")
        summarise("B 切除静音后（无 VAD）", b, time.time() - t0)

        # ── ⑤ 映射回原时间轴（仅对有静音的素材有意义）──
        if sils:
            mapped = []
            for s in b:
                st = map_back(s.get("start", 0) / 1000.0, segs_range)
                en = map_back(s.get("end", 0) / 1000.0, segs_range)
                mapped.append((st, en, s.get("text", "").strip()))
            print()
            print("  ── 映射回原时间轴（前 8 条）──")
            for st, en, txt in mapped[:8]:
                print(f"    {st:7.1f} → {en:7.1f}s  {txt[:44]}")
            in_sil = [m for m in mapped
                      if any(s <= m[0] <= e for s, e in sils)]
            print(f"    落在静音区内的条目：{len(in_sil)}  "
                  f"（越少越好，理想为 0）")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
