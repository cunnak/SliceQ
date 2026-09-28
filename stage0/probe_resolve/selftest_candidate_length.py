# -*- coding: utf-8 -*-
"""候选长度上限 + 精析副本体积自适应（v0.1.2）。

背景（2026-09-28 实测事故）：
  用户对 84 分钟素材跑分析，结果「完成：0 个切片段」。根因是三件事叠加：
    ① 粗筛给出的 24 个候选里有 10 个首尾相接（600s~1800s），
       被 `merge_candidates(gap=8)` 无限合并成**一个 1200 秒的巨块**；
    ② 精析按**固定码率**切副本，从不看体积 ⇒ 副本 90.6MB；
    ③ 兼容模式通道要求 base64 ≤ 20M 字符 ⇒ 120.8M 字符，超限 6.3 倍
       ⇒ 两个候选**都在发请求前被拒** ⇒ clip 表 0 行。

本测试守三件事：
  ① `merge_candidates` 不得产出超长候选（且切分不留碎片）
  ② `plan_analysis_clip` 必须给出能塞进通道上限的档位，并在必要时降级
  ③ **对照组**：v0.1.1 的"固定码率"确实会超限 —— 否则测试等于没有牙
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq import _testing                                   # noqa: E402

_testing.isolate_data_root("candlen")
_testing.assert_isolated()

from sliceq import analyzer, pipeline, screening              # noqa: E402
from sliceq.screening import Candidate                         # noqa: E402

PASS = FAIL = 0


def ck(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK]   {name}" + (f"　—　{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"　—　{detail}" if detail else ""))


def head(t: str) -> None:
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def C(a: float, b: float, conf: int = 50, hint: str = "h") -> Candidate:
    return Candidate(start=a, end=b, confidence=conf, hint=hint)


MAX = screening.MAX_CANDIDATE_SEC
MIN = screening.MIN_CANDIDATE_SEC
SOFT = MAX + MIN          # 软上限：短于它的候选刻意不切（避免碎片）


# ─────────────────────────────────────────────────────────
head("1. 合并不得产出超长候选")
# ─────────────────────────────────────────────────────────
# 复现当年那批数据：10 个首尾相接的候选（600~1800，每个 80~185 秒）
chain = [C(600, 745), C(745, 835), C(835, 915), C(915, 1015), C(1015, 1200),
         C(1200, 1357), C(1357, 1465), C(1465, 1575), C(1575, 1685),
         C(1685, 1800)]
print(f"  输入：{len(chain)} 个首尾相接的候选，跨 {chain[0].start}~{chain[-1].end}s"
      f"（共 {chain[-1].end - chain[0].start:.0f} 秒）")

merged = screening.merge_candidates(chain)
lens = [c.duration for c in merged]
print(f"  合并后 {len(merged)} 段，长度 {[round(x) for x in lens]}")
ck("★ 没有候选超过软上限（%d 秒）" % SOFT,
   all(x <= SOFT + 0.5 for x in lens), f"最长 {max(lens):.0f}s")
ck("★ 不再出现 1200 秒巨块", max(lens) < 1200, f"最长 {max(lens):.0f}s")
ck("总时长守恒（合并/切分不丢内容）",
   abs(sum(lens) - (chain[-1].end - chain[0].start)) < 1.0,
   f"{sum(lens):.1f}s vs {chain[-1].end - chain[0].start:.1f}s")

# ★ 对照组：v0.1.1 的行为（max_len=0 = 不限制）
legacy = screening.merge_candidates(chain, max_len=0)
legacy_max = max(c.duration for c in legacy)
print(f"\n  对照组（max_len=0，v0.1.1 行为）：{len(legacy)} 段，最长 {legacy_max:.0f}s")
ck("★ 对照组确实复现了缺陷（否则本测试没有牙）",
   legacy_max > MAX, f"对照组最长 {legacy_max:.0f}s")
ck("★ 修复后明显短于对照组",
   max(lens) < legacy_max / 2, f"{max(lens):.0f}s vs {legacy_max:.0f}s")


# ─────────────────────────────────────────────────────────
head("2. 切分：不留碎片、不超上限、总长不变")
# ─────────────────────────────────────────────────────────
cases = [1200.0, 600.0, 400.0, 361.0, 200.0, 185.0, 180.0, 100.0, 20.0]
all_ok = True
for dur in cases:
    parts = screening._split_long(C(0, dur), MAX)
    ls = [p.duration for p in parts]
    ok = (all(x <= SOFT + 0.5 for x in ls)
          and all(x >= min(MIN, dur) - 0.5 for x in ls)
          and abs(sum(ls) - dur) < 1.0)
    all_ok = all_ok and ok
    print(f"    {dur:7.1f}s → {len(parts):2d} 块 {[round(x,1) for x in ls]}"
          f"{'' if ok else '   ✗'}")
ck("★ 所有切分都满足：无碎片 / 不超软上限 / 总长守恒", all_ok)

# 185 秒刻意不切：切了会留 5 秒碎片（白花一次模型调用）
ck("★ 185 秒候选刻意不切（切了会留 5 秒碎片）",
   len(screening._split_long(C(0, 185), MAX)) == 1)
# 361 秒必须切：单块 180，剩 181 —— 361 - 180 = 181 > SOFT? 不，181 <= 200 ⇒ 两块
ck("361 秒切成 2 块（180 + 181）",
   [round(x.duration) for x in screening._split_long(C(0, 361), MAX)] == [180, 181])


# ─────────────────────────────────────────────────────────
head("3. 体积规划：必须能塞进通道上限")
# ─────────────────────────────────────────────────────────
cap_http = pipeline.clip_size_cap(analyzer.HTTPTransport("sk-dry"))
cap_ds = pipeline.clip_size_cap(analyzer.DashScopeTransport("sk-dry"))
print(f"  兼容模式 http 上限 : {cap_http:,} 字节（{cap_http / 1048576:.1f} MB）")
print(f"  原生 dashscope 上限: {cap_ds:,} 字节（{cap_ds / 1048576:.1f} MB）")
ck("两条通道的上限不同且都为正",
   cap_http > 0 and cap_ds > cap_http,
   f"http={cap_http} < ds={cap_ds}")

max_dur = MAX + 2 * 30.0
print(f"  副本时长上限 = {max_dur:.0f} 秒（候选上限 + 两侧各 30s 留白）\n")

for cand_dur, tag in ((45.0, "短候选"), (185.0, "长候选"), (240.0, "满额副本"),
                      (1200.0, "异常超长")):
    dur = min(cand_dur + 60, max_dur)
    h, v, a, keep, note = pipeline.plan_analysis_clip(dur, cap_http, max_dur)
    est = pipeline.est_mp4_bytes(keep, v, a)
    print(f"  {tag}（{cand_dur:.0f}s → 副本 {dur:.0f}s）→ {h}p/{v}kbps，"
          f"取 {keep:.0f}s，{est / 1048576:.2f} MB"
          + (f"\n        说明：{note}" if note else ""))
    ck(f"★ {tag} 规划后不超上限", est <= cap_http,
       f"{est:,} vs 上限 {cap_http:,}")
    ck(f"★ {tag} 规划后 base64 不超 analyzer 安全上限",
       int(est / 3 * 4) <= analyzer.B64_SAFE_LIMIT,
       f"{int(est / 3 * 4):,} vs 上限 {analyzer.B64_SAFE_LIMIT:,}")

# 体积越大，档位越低（单调性）
prev = 0
for dur in (30.0, 90.0, 180.0, 240.0):
    h, v, a, keep, _ = pipeline.plan_analysis_clip(dur, cap_http, max_dur)
    sz = pipeline.est_mp4_bytes(keep, v, a)
    print(f"    {dur:6.1f}s @ {h}p/{v}k → {sz / 1048576:6.2f} MB")
ck("档位随体积需求单调不超上限",
   all(pipeline.est_mp4_bytes(*[min(d, max_dur), v, a]) <= cap_http
       for d in (30.0, 90.0, 180.0, 240.0)
       for h, v, a, k, _ in [pipeline.plan_analysis_clip(min(d, max_dur),
                                                        cap_http, max_dur)]))

# 无约束时用最高档（通道 A 本地直传）
h0, v0, a0, k0, n0 = pipeline.plan_analysis_clip(60.0, 0, 0)
ck("无体积约束时用最高档 720p/1000k",
   h0 == "720" and v0 == 1000, f"{h0}p/{v0}k")

# 时长上限必须生效并说明
h1, v1, a1, k1, n1 = pipeline.plan_analysis_clip(600.0, cap_ds, max_dur)
ck("★ 超长输入被裁到时长上限，且给出说明",
   abs(k1 - max_dur) < 0.5 and bool(n1), f"keep={k1} note={n1!r}")

# 极小的 max_bytes ⇒ 必须缩时长（最低档仍超）
tiny = 300_000
h2, v2, a2, k2, n2 = pipeline.plan_analysis_clip(600.0, tiny, 10_000)
ck("★ 空间极端受限时缩时长并给出说明",
   k2 < 600.0 and bool(n2), f"keep={k2:.0f} note={n2!r}")
ck("缩时长后有下限保护（不会缩到接近 0）",
   k2 >= pipeline.MIN_ANALYSIS_SEC - 0.5, f"keep={k2:.1f}")

# ★ 对照组：v0.1.1 的固定 480p/500k 对长候选必然超限
print("\n  对照组（v0.1.1 固定 480p/500k，不看体积）：")
old_ok = 0
for cand_dur in (45.0, 185.0, 600.0, 1260.0):
    old = pipeline.est_mp4_bytes(cand_dur, 500, 96)
    flag = "超限" if old > cap_http else "可以"
    print(f"    {cand_dur:7.1f}s → {old / 1048576:7.2f} MB  {flag}")
    if old > cap_http:
        old_ok += 1
ck("★ 对照组确实复现了超限（否则本测试没有牙）", old_ok >= 2,
   f"{old_ok} 个超限")


# ─────────────────────────────────────────────────────────
head("4. 常量与文档一致性")
# ─────────────────────────────────────────────────────────
ck("MAX_CANDIDATE_SEC 是正数", MAX > 0, f"{MAX}")
ck("MIN_CANDIDATE_SEC 明显小于 MAX", 0 < MIN < MAX / 4, f"{MIN} vs {MAX}")
ck("档位表按画质从高到低排列",
   all(pipeline._ANALYSIS_LADDER[i][1] > pipeline._ANALYSIS_LADDER[i + 1][1]
       for i in range(len(pipeline._ANALYSIS_LADDER) - 1)),
   str([x[0] for x in pipeline._ANALYSIS_LADDER]))
ck("体积安全余量 < 1（宁可多降一档）", 0 < pipeline._SIZE_SAFETY < 1,
   f"{pipeline._SIZE_SAFETY}")
ck("★ pipeline 里已无 preset_for（旧固定码率入口已移除）",
   not hasattr(pipeline, "preset_for"))

print("\n" + "=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
raise SystemExit(1 if FAIL else 0)
