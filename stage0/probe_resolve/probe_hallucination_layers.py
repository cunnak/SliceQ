# -*- coding: utf-8 -*-
r"""幻觉治理的三层防线 —— 逐层验证效果。

═══════════════════════════════════════════════════════════════
背景（2026-09-25 实测链条）
═══════════════════════════════════════════════════════════════
1. ffmpeg whisper 滤镜的 `vad_model` **不能消除静音区幻觉**
   （源码 af_whisper.c：VAD 只决定何时切分，转录起点永远是 buffer 第 0 样本）
   实测：30 秒纯数字静音 → 无 VAD 出 11 段幻觉，有 VAD 也是 11 段。

2. **静音预切除有效**：140s 素材（53.6s 静音）幻觉 23 → 6（-74%）

3. 但剩下的 6 段里，5 段是同一句话的重复 —— whisper 的典型幻觉形态。

═══════════════════════════════════════════════════════════════
三层防线
═══════════════════════════════════════════════════════════════
  第 1 层 · 静音预切除     silencedetect → 只喂语音区间给 whisper
  第 2 层 · 重复文本剔除   同一句逐字重复 ≥N 次 → 全丢（真实讲话不会这样）
  第 3 层 · 特征词剔除     感谢订阅/字幕制作/小铃铛… 等模板文案

⚠️ 第 2 层的关键约束：**只对足够长的文本做重复判定**。
   短应答词（"好"、"啊"、"嗯"、"对"）重复出现是正常的，判重会误杀。
"""
from __future__ import annotations

import importlib.util
import json
import re
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("sc", HERE / "probe_silence_cut.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

WORK = sc.WORK
SRC = WORK / "live_silence.wav"
SPLICED = WORK / "live_silence_speech.wav"

MIN_REPEAT_LEN = 8      # 少于 8 字的文本不参与重复判定（保护"好""啊"这类）
REPEAT_THRESHOLD = 3    # 逐字重复 ≥3 次即判定为幻觉


def norm(text: str) -> str:
    """归一化：去空白，用于重复比对。"""
    return re.sub(r"\s+", "", (text or "").strip())


def drop_repeated(segs: list[dict]) -> tuple[list[dict], list[dict]]:
    """剔除"短时间窗内逐字重复"的文本。返回 (保留, 剔除)。"""
    counts = Counter(
        norm(s.get("text", "")) for s in segs
        if len(norm(s.get("text", ""))) >= MIN_REPEAT_LEN)
    dropped, kept = [], []
    for s in segs:
        key = norm(s.get("text", ""))
        if len(key) >= MIN_REPEAT_LEN and counts[key] >= REPEAT_THRESHOLD:
            dropped.append(s)
        else:
            kept.append(s)
    return kept, dropped


def count_hallu(segs: list[dict]) -> int:
    return sum(1 for s in segs if sc.is_hallucination(s.get("text", "")))


def show(label: str, segs: list[dict]) -> None:
    print(f"  {label:38s} 段数 {len(segs):3d}   幻觉 {count_hallu(segs):3d}")


def main() -> int:
    for p in (SRC, SPLICED):
        if not p.exists():
            print(f"缺少 {p}")
            return 1

    total = sc.wav_duration(SRC)
    sils = sc.detect_silences(SRC)
    ranges = sc.speech_segments(sils, total, pad=0.25)

    print("=" * 78)
    print("幻觉治理三层防线 — 逐层效果")
    print("=" * 78)
    print(f"素材 {SRC.name}  {total:.1f}s，静音 {sum(e-s for s,e in sils):.1f}s")
    print()

    # ── 第 0 层：基准 ─────────────────────────────
    raw = sc.transcribe(SRC, WORK / "_layer0.jsonl")
    show("第 0 层 · 不做任何处理", raw)
    print()

    # ── 第 1 层：静音预切除 ───────────────────────
    cut = sc.transcribe(SPLICED, WORK / "_layer1.jsonl")
    show("第 1 层 · + 静音预切除", cut)

    # ── 第 2 层：重复剔除 ─────────────────────────
    kept, dropped = drop_repeated(cut)
    show("第 2 层 · + 重复文本剔除", kept)
    if dropped:
        print(f"       剔除 {len(dropped)} 段，样例：")
        for s in dropped[:3]:
            print(f"         {s.get('start',0)/1000:6.1f}s  {norm(s.get('text',''))[:42]}")

    # ── 第 3 层：特征词剔除 ───────────────────────
    final = [s for s in kept if not sc.is_hallucination(s.get("text", ""))]
    show("第 3 层 · + 特征词剔除", final)

    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    print(f"  基准 {len(raw)} 段 / 幻觉 {count_hallu(raw)}")
    print(f"  终态 {len(final)} 段 / 幻觉 {count_hallu(final)}")
    print(f"  段数变化：{len(raw)} → {len(final)}（剔掉 {len(raw)-len(final)} 段）")

    # 时间轴映射验证
    print()
    print("  时间轴映射抽查（终态前 6 条）：")
    for s in final[:6]:
        st = sc.map_back(s.get("start", 0) / 1000, ranges)
        print(f"    拼接 {s.get('start',0)/1000:6.1f}s → 原 {st:6.1f}s  "
              f"{(s.get('text') or '').strip()[:36]}")

    # 落盘
    out = WORK / "final_segments.json"
    payload = [{"orig_start": round(sc.map_back(s.get("start", 0) / 1000, ranges), 3),
                "orig_end": round(sc.map_back(s.get("end", 0) / 1000, ranges), 3),
                "text": (s.get("text") or "").strip()}
               for s in final]
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  终态已落盘：{out.name}（{len(payload)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
