# -*- coding: utf-8 -*-
"""验证「精析候选体积超限」的修复（v0.1.2）。

背景（2026-09-28 实测事故）：
  用户对一段 84 分钟的素材跑分析，结果「完成：0 个切片段」。
  根因：粗筛给出的 24 个候选里，有 10 个首尾相接（600s~1800s），
  被 `merge_candidates(gap=8)` 无限合并成**一个 1200 秒（20 分钟）的巨块**；
  另一个候选单段就 600 秒。精析把候选切低码率副本交给模型，
  而兼容模式通道要求 base64 ≤ 20M 字符 ⇒ 副本 90.6MB / 46.3MB、
  base64 120.8M / 61.7M 字符，**超限 6.3 倍 / 3.2 倍** ⇒ 两个候选
  都在发请求前被拒 ⇒ clip 表 0 行。

本探针用**当时那份真实的 screen.jsonl 缓存**（不是构造数据）验证修复：
  ① 候选合并后是否每段都 ≤ MAX_CANDIDATE_SEC
  ② 体积规划是否给出能塞进通道上限的档位
  ③ **真实切一个副本**，量实际体积，确认估算没有偏乐观
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\b1596\WorkBuddy\2026-09-23-13-06-47\SliceQ")

from sliceq import _testing                                   # noqa: E402

_testing.isolate_data_root("size_fix")
_testing.assert_isolated()

from sliceq import analyzer, pipeline, screening              # noqa: E402
from sliceq.screening import Candidate                         # noqa: E402

PASS = FAIL = 0


def ck(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {detail}")


WORK = Path(os.path.expandvars(
    r"%APPDATA%\SliceQ\work\【实事求是】如何在历史铁律和社会张力的网络中过一种政治的斗争的生活"))
VIDEO = Path(r"C:\Users\b1596\Desktop\【实事求是】如何在历史铁律和社会张力的网络中过一种政治的斗争的生活.mp4")
DURATION = 5075.543447
SCREEN = WORK / "screen.jsonl"

print("=" * 76)
print("材料")
print(f"  素材      : {VIDEO.name}")
print(f"  存在      : {VIDEO.exists()}（{VIDEO.stat().st_size / 1073741824:.2f} GB）"
      if VIDEO.exists() else "  素材不存在")
print(f"  粗筛缓存  : {SCREEN}  ({SCREEN.stat().st_size:,} 字节)"
      if SCREEN.exists() else "  缓存不存在")

if not SCREEN.exists():
    print("\n★ 缓存不在，无法用真实数据验证；退出。")
    raise SystemExit(2)

# ── 读出当年那批原始候选 ────────────────────────────────
raw: list[Candidate] = []
for ln in SCREEN.read_text(encoding="utf-8").splitlines():
    ln = ln.strip()
    if ln:
        for c in json.loads(ln)["candidates"]:
            raw.append(Candidate(**c))

print(f"\n原始候选 {len(raw)} 段（未合并）")
durs = sorted(c.duration for c in raw)
print(f"  时长分布：最短 {durs[0]:.0f}s，中位 {durs[len(durs) // 2]:.0f}s，"
      f"最长 {durs[-1]:.0f}s")

# ── ① 合并后是否受限 ────────────────────────────────────
print("\n" + "=" * 76)
print("① 候选合并（MAX_CANDIDATE_SEC = %.0f）" % screening.MAX_CANDIDATE_SEC)

merged = screening.merge_candidates(raw)
print(f"  合并后 {len(merged)} 段")
for c in merged[:8]:
    print(f"    {c.start:8.1f} ~ {c.end:8.1f}  ({c.duration:6.1f}s)")
if len(merged) > 8:
    print(f"    … 共 {len(merged)} 段")

# 软上限 = MAX + MIN：短于 (MAX+MIN) 的候选**刻意不切**（切了会留碎片，
# 见 screening._split_long 的注释）。所以判据是 ≤ 软上限。
_SOFT_CAP = screening.MAX_CANDIDATE_SEC + screening.MIN_CANDIDATE_SEC
over = [c for c in merged if c.duration > _SOFT_CAP + 0.5]
ck(f"合并后没有任何候选超过 {_SOFT_CAP:.0f} 秒（上限 {screening.MAX_CANDIDATE_SEC:.0f}"
   f" + 尾块余量 {screening.MIN_CANDIDATE_SEC:.0f}）",
   not over, f"超限 {len(over)} 段：{[(round(c.start), round(c.duration)) for c in over[:3]]}")
ck("不再出现 20 分钟巨块（旧版实测最长 1200s）",
   max(c.duration for c in merged) <= _SOFT_CAP + 0.5,
   f"最长 {max(c.duration for c in merged):.0f}s")

# 对照：旧行为（max_len 关闭 = 无限合并）
legacy = screening.merge_candidates(raw, max_len=0)
print(f"\n  对照（max_len=0，即 v0.1.1 的行为）：{len(legacy)} 段，"
      f"最长 {max(c.duration for c in legacy):.0f}s")
ck("★ 对照组复现了旧缺陷（证明改动确实起作用）",
   max(c.duration for c in legacy) > screening.MAX_CANDIDATE_SEC,
   "对照组没有超长候选 —— 说明这份数据其实不能复现该缺陷")

# 切分的边界行为（构造数据；重点是"不留碎片"）
print("\n  切分边界（构造数据）")
_splits_ok = True
for dur in (1200.0, 600.0, 400.0, 200.0, 185.0, 180.0, 100.0):
    parts = screening._split_long(
        Candidate(start=0.0, end=dur), screening.MAX_CANDIDATE_SEC)
    lens = [round(p.duration, 1) for p in parts]
    cap = screening.MAX_CANDIDATE_SEC + screening.MIN_CANDIDATE_SEC + 0.5
    ok = (all(ln <= cap for ln in lens)
          and all(ln >= min(screening.MIN_CANDIDATE_SEC, dur) - 0.5 for ln in lens)
          and abs(sum(lens) - dur) < 1.0)
    _splits_ok = _splits_ok and ok
    print(f"    {dur:7.1f}s → {len(parts)} 块 {lens}{'' if ok else '  ✗'}")
ck("★ 切分不留碎片、不超上限、总长不变", _splits_ok)

# ── ② 体积规划 ──────────────────────────────────────────
print("\n" + "=" * 76)
print("② 体积规划（通道上限）")

cap_http = pipeline.clip_size_cap(analyzer.HTTPTransport("sk-dry"))
cap_ds = pipeline.clip_size_cap(analyzer.DashScopeTransport("sk-dry"))
print(f"  兼容模式 HTTP 通道上限 : {cap_http / 1048576:.1f} MB")
print(f"  原生 dashscope 通道上限: {cap_ds / 1048576:.1f} MB")

max_dur = screening.MAX_CANDIDATE_SEC + 2 * 30.0
print(f"  副本时长上限           : {max_dur:.0f} 秒（候选上限 + 两侧各 30s 留白）")

for c in merged[:3]:
    dur = min(c.duration + 60, max_dur)
    h, v, a, keep, note = pipeline.plan_analysis_clip(dur, cap_http, max_dur)
    est = pipeline.est_mp4_bytes(keep, v, a)
    print(f"\n  候选 {c.start:.0f}~{c.end:.0f}（{c.duration:.0f}s）→ 副本 {dur:.0f}s")
    print(f"     档位 {h}p/{v}kbps/{a}kbps，实取 {keep:.0f}s，"
          f"估算 {est / 1048576:.2f} MB")
    if note:
        print(f"     说明：{note}")
    ck(f"候选@ {c.start:.0f}s 的规划不超上限",
       pipeline.est_mp4_bytes(keep, v, a) <= cap_http, "估算超限")

# 旧版对照：固定 480p/500k
old_sizes = [(c, pipeline.est_mp4_bytes(min(c.duration + 60, max_dur), 500, 96))
             for c in merged]
worst_old = max(s for _, s in old_sizes)
print(f"\n  对照（v0.1.1 固定 480p/500k）：最大副本估算 "
      f"{worst_old / 1048576:.1f} MB，超限 "
      f"{worst_old / cap_http:.1f} 倍")
ck("★ 对照组复现'超限'（说明这份数据确实能复现缺陷）",
   worst_old > cap_http, f"最大 {worst_old / 1048576:.1f} MB ≤ 上限")

# ── ③ 真切实测 ──────────────────────────────────────────
print("\n" + "=" * 76)
print("③ 真切实测一个副本（动用真实 ffmpeg 与真实素材）")

if not VIDEO.exists():
    print("  素材不在，跳过（结论：结构验证已完成，实切未做）")
else:
    longest = max(merged, key=lambda c: c.duration)
    dur = longest.duration + 60
    h, v, a, keep, note = pipeline.plan_analysis_clip(dur, cap_http, max_dur)
    root = _testing.isolated_root()
    out = (root or Path(__import__("tempfile").mkdtemp(prefix="sqclip_"))) / "verify_clip.mp4"
    print(f"  切 候选 {longest.start:.0f}~{longest.end:.0f}s 的 {keep:.0f}s @ {h}p/{v}k …")
    pipeline.cut_clip(VIDEO, out, max(0.0, longest.start - 30),
                      max(0.0, longest.start - 30) + keep,
                      height=h, bitrate=f"{v}k", audio_bitrate=f"{a}k")
    real = out.stat().st_size
    est = pipeline.est_mp4_bytes(keep, v, a)
    print(f"  实测体积 : {real / 1048576:.2f} MB")
    print(f"  估算体积 : {est / 1048576:.2f} MB（偏差 "
          f"{(real - est) / max(est, 1) * 100:+.1f}%）")
    print(f"  通道上限 : {cap_http / 1048576:.2f} MB")
    ck("★ 实切副本未超过通道上限", real <= cap_http,
       f"实测 {real / 1048576:.1f} MB > 上限 {cap_http / 1048576:.1f} MB")
    ck("base64 估算未超过 analyzer 安全上限",
       analyzer.b64_size_of(out) <= analyzer.B64_SAFE_LIMIT,
       f"{analyzer.b64_size_of(out):,} vs {analyzer.B64_SAFE_LIMIT:,}")
    print(f"  base64   : {analyzer.b64_size_of(out):,} 字符 "
          f"（安全上限 {analyzer.B64_SAFE_LIMIT:,}）")
    try:
        out.unlink()
    except OSError:
        pass

print("\n" + "=" * 76)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
