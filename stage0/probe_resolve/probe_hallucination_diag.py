# -*- coding: utf-8 -*-
r"""诊断：切除静音后幻觉为何仍是 23 段？

前一轮结果有一处矛盾：
  - 拼接后音频 86.88s（静音 53.6s 确实被切掉了，时长对得上）
  - 但幻觉数 23 → 23，与未切除时**完全相同**

可能：
  A. 静音区的幻觉被切掉了，但语音区**重新产生**了等量幻觉
  B. 拼接产生的音频在接缝处异常，诱发了新幻觉
  C. 统计口径有误（脚本 bug）

本脚本打印两组的**全部条目**，人工核对。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("sc", HERE / "probe_silence_cut.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

WORK = sc.WORK
SRC = WORK / "live_silence.wav"
SPLICED = WORK / "live_silence_speech.wav"

# 已知静音区
SIL = [(34.03, 87.67)]


def dump(label: str, items: list[dict], offset_map=None) -> None:
    print(f"\n{'=' * 78}")
    print(f"{label}   共 {len(items)} 段")
    print("=" * 78)
    hallu_n = 0
    for i, s in enumerate(items):
        st, en = s.get("start", 0) / 1000, s.get("end", 0) / 1000
        txt = (s.get("text") or "").strip()
        mark = sc.is_hallucination(txt)
        if mark:
            hallu_n += 1
        orig = f"→原{offset_map(st):7.1f}s" if offset_map else ""
        tag = "  ★幻觉" if mark else ""
        print(f"  {i:3d} {st:7.1f}→{en:7.1f}s {orig}  {txt[:46]}{tag}")
    print(f"\n  合计：{len(items)} 段，其中判定为幻觉 {hallu_n} 段")


def main() -> int:
    for p in (SRC, SPLICED):
        if not p.exists():
            print(f"缺少 {p}")
            return 1

    total = sc.wav_duration(SRC)
    sils = sc.detect_silences(SRC)
    segs = sc.speech_segments(sils, total, pad=0.25)
    print(f"原时长 {total:.1f}s；静音 {sils}；语音区间 {segs}")

    # ── A 组：原音频 ──────────────────────────────
    a = sc.transcribe(SRC, WORK / "_diag_raw.jsonl")
    dump("A 组 · 原音频（140s，含 53.6s 静音）", a)

    # ── B 组：拼接后 ──────────────────────────────
    b = sc.transcribe(SPLICED, WORK / "_diag_spliced.jsonl")
    dump("B 组 · 切除静音后（86.9s）", b, offset_map=lambda t: sc.map_back(t, segs))

    # ── 交叉核对 ──────────────────────────────────
    print(f"\n{'=' * 78}")
    print("交叉核对")
    print("=" * 78)
    a_in_sil = [s for s in a
                if any(lo <= s.get("start", 0) / 1000 <= hi for lo, hi in SIL)]
    print(f"  A 组中起点落在静音区 {SIL[0][0]}~{SIL[0][1]}s 的条目：{len(a_in_sil)}")
    a_h = [s for s in a if sc.is_hallucination(s.get("text", ""))]
    print(f"  A 组幻觉 {len(a_h)} 段，其中在静音区内 "
          f"{len([s for s in a_h if any(lo <= s.get('start',0)/1000 <= hi for lo,hi in SIL)])} 段")
    b_h = [s for s in b if sc.is_hallucination(s.get("text", ""))]
    print(f"  B 组幻觉 {len(b_h)} 段")
    print()
    print("  B 组幻觉的映射后位置：")
    for s in b_h:
        t = sc.map_back(s.get("start", 0) / 1000, segs)
        in_sil = "★落在静音区" if any(lo <= t <= hi for lo, hi in SIL) else ""
        print(f"    {t:7.1f}s  {in_sil}  {(s.get('text') or '')[:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
