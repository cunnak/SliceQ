# -*- coding: utf-8 -*-
"""预演：修复后点「重新分析」，用户会得到什么。

用**用户那次失败时的真实粗筛缓存**跑新逻辑，给出候选数、副本规格、预估花费。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\b1596\WorkBuddy\2026-09-23-13-06-47\SliceQ")

from sliceq import _testing                                   # noqa: E402

_testing.isolate_data_root("rehearsal")
_testing.assert_isolated()

from sliceq import analyzer, pipeline, screening              # noqa: E402
from sliceq.screening import Candidate                         # noqa: E402

SCREEN = Path(os.path.expandvars(
    r"%APPDATA%\SliceQ\work\【实事求是】如何在历史铁律和社会张力的网络中过一种政治的斗争的生活\screen.jsonl"))
DURATION = 5075.543447

if not SCREEN.exists():
    print("缓存不在，无法预演")
    raise SystemExit(2)

raw: list[Candidate] = []
for ln in SCREEN.read_text(encoding="utf-8").splitlines():
    if ln.strip():
        raw.extend(Candidate(**c) for c in json.loads(ln)["candidates"])

merged = screening.merge_candidates(raw)
print(f"原始候选      : {len(raw)} 段，总 {sum(c.duration for c in raw):.0f} 秒")
print(f"合并后（新版）: {len(merged)} 段，总 {sum(c.duration for c in merged):.0f} 秒，"
      f"最长 {max(c.duration for c in merged):.0f} 秒")

old = screening.merge_candidates(raw, max_len=0)
print(f"合并后（旧版）: {len(old)} 段，最长 {max(c.duration for c in old):.0f} 秒")

cap = pipeline.clip_size_cap(analyzer.HTTPTransport("sk-dry"))
maxdur = screening.MAX_CANDIDATE_SEC + 2 * 30.0
print(f"\n通道上限      : {cap / 1048576:.1f} MB ／ 副本时长上限 {maxdur:.0f} 秒")

for ratio in (0.20, 0.30, 0.50):
    kept, trimmed = screening.enforce_ratio_budget(merged, DURATION, ratio)
    if not kept:
        print(f"\n筛选强度 {ratio:.2f}：0 段（预算 {max(DURATION * ratio, 120):.0f} 秒）")
        continue
    secs = sum(c.duration for c in kept)
    specs = set()
    for c in kept:
        h, v, a, keep, _ = pipeline.plan_analysis_clip(
            min(c.duration + 60, maxdur), cap, maxdur)
        specs.add(f"{h}p/{v}kbps")
    print(f"\n筛选强度 {ratio:.2f}（预算 {max(DURATION * ratio, 120):.0f} 秒）")
    print(f"  最终候选    : {len(kept)} 段，共 {secs:.0f} 秒"
          f"（裁掉 {trimmed:.0f} 秒）")
    print(f"  最长候选    : {max(c.duration for c in kept):.0f} 秒")
    print(f"  副本规格    : {sorted(specs)}")
    print(f"  精析预估花费: ¥{analyzer.estimate_refine_cost(secs):.4f}")
