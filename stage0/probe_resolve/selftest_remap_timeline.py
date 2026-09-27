# -*- coding: utf-8 -*-
"""`remap_to_timeline` 自测（阶段 5-A）。

## 为什么这个函数值得单独一套测试

它是甲方案（OTIO / 剪映草稿）的**命门**。
漏做或做错的表现是：**SRT 依然合法、剪映和达芬奇照常接受、不报任何错**，
只是字幕整体错位。属于「功能正常、结果全错」那一类 ——
没有任何自动化的东西会告诉你出事了，只能靠人一句句核对。

所以这层防线必须是自动的。

## 测试策略

**金标准对照**：另写一个朴素实现（`naive_map_point`，按点逐个判断），
用它算出期望值，再和被测函数比。**期望值由程序算，不手写估算。**

**双向断言**：不只测"该保留的保留了"，也测"该丢的丢了"、
"计数守恒"、"不该动的字段没被动"。

**随机化**：随机生成几百组 cue 和 highlights，验证不变量
（守恒、单调、越界、顺序）。
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
#    注意：**间接写入也算**（例如调 bridge.set_manual_path() 会写 settings.json）
#    —— 所以默认给所有自测都隔离，不做"看起来不碰数据"的判断。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from sliceq import subtitle as S                          # noqa: E402

PASS = 0
FAIL = 0


def ck(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK]   {name}" + (f"　—　{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}　—　{detail}")


def head(t: str) -> None:
    print(f"\n{'=' * 74}\n{t}\n{'=' * 74}")


def cue(a: float, b: float, main: str = "字幕", sub: str = "") -> S.Cue:
    return S.Cue(start=0.0, end=0.0, main=main, sub=sub,
                 src_start=a, src_end=b)


# ─────────────────────────────────────────────────────────────
# 金标准：朴素参考实现（慢，但逻辑一眼可验）
# ─────────────────────────────────────────────────────────────
def naive_map_point(t: float, highlights) -> float | None:
    """把一个**源时间点**映射到时间线时间。不在任何片段内返回 None。

    这是"按定义直译"的版本，不含任何优化，用来当对照标准。
    """
    acc = 0.0
    for (a, b) in highlights:
        if a <= t < b:
            return acc + (t - a)
        acc += max(0.0, b - a)
    return None


def reference_map(cues, highlights):
    """金标准：独立实现一遍映射，返回应当得到的结果（list[Cue]）。

    规则与实现语义一致，但**独立写一遍**：
      · 分段：cue 落在每个片段里的部分分别映射
      · 合并：时间线上连续/重叠的段合成一条
      · 判定：保留总时长 / cue 总时长 < 阈值 → 丢
      · 偏移 = 前面所有片段的时长之和（现算，不用预计算数组）

    ⚠️ **最后要过一遍 `S.tidy()`**，而且必须过。
       它是「排版规整」（排序 / 防重叠 / 最短可见时长），
       不属于本测试要独立验证的「映射公式」，复用它不算抄实现。

       第一版忘了调，于是：一条跨边界被裁到 0.68s 的 cue
       被 tidy 延长到 0.7s（`min_duration`），金标准却说 0.68 ——
       报了一个**假失败**。教训：对照实现必须和被测实现
       **覆盖到同一条流水线的终点**，否则比对的是两个不同的东西。
    """
    out: list[S.Cue] = []
    for c in cues:
        dur = max(1e-6, c.src_end - c.src_start)
        pieces: list[tuple[float, float]] = []
        for i, (hs, he) in enumerate(highlights):
            a, b = max(c.src_start, hs), min(c.src_end, he)
            if b - a <= 0:
                continue
            # 偏移用「前面片段时长之和」现算（实现里是预计算的 offsets 数组）
            off = sum(max(0.0, e - s) for s, e in highlights[:i])
            pieces.append((a - hs + off, b - hs + off))
        if not pieces:
            continue

        pieces.sort()
        merged: list[list[float]] = [[pieces[0][0], pieces[0][1]]]
        for s0, e0 in pieces[1:]:
            if s0 <= merged[-1][1] + 1e-6:
                merged[-1][1] = max(merged[-1][1], e0)
            else:
                merged.append([s0, e0])

        if sum(e - s for s, e in merged) / dur < S._MIN_KEEP_RATIO:
            continue
        for s0, e0 in merged:
            if e0 - s0 < S._MIN_CUE_VISIBLE:
                continue
            out.append(S.Cue(start=s0, end=e0, main=c.main, sub=c.sub,
                             src_start=c.src_start, src_end=c.src_end))

    total = sum(max(0.0, e - s) for s, e in highlights)
    return S.tidy(out, limit=total)


def main() -> int:
    # ── 1. 教科书例子（TECH-DESIGN §6.4.3）─────────────
    head("1. 教科书例子：高光 10~25 / 100~115，源 12s → 时间线 2s")
    HL = [(10.0, 25.0), (100.0, 115.0)]
    exp1 = naive_map_point(12.0, HL)
    ck("金标准自身给出 2.0", exp1 == 2.0, f"{exp1}")
    got, drop = S.remap_to_timeline([cue(12.0, 13.0)], HL)
    ck("实现一致", len(got) == 1 and abs(got[0].start - 2.0) < 1e-6,
       f"start={got[0].start if got else None}")
    exp2 = naive_map_point(102.0, HL)
    got2, _ = S.remap_to_timeline([cue(102.0, 103.0)], HL)
    ck("第二段（跨过 90s 空隙）：源 102s → 时间线 17s",
       len(got2) == 1 and abs(got2[0].start - exp2) < 1e-6,
       f"期望 {exp2}，实得 {got2[0].start if got2 else None}")

    # ── 2. ★ 与金标准逐条对照（随机化）─────────────────
    head("2. ★ 金标准逐条对照：随机 300 组，位置必须一致")
    rng = random.Random(20260927)
    mismatches: list[str] = []
    total_cues = 0
    for trial in range(300):
        # 随机生成 1~4 个不重叠片段
        hl, cur = [], rng.uniform(0, 300)
        for _ in range(rng.randint(1, 4)):
            dur = rng.uniform(15, 60)
            hl.append((round(cur, 3), round(cur + dur, 3)))
            cur += dur + rng.uniform(5, 200)
        # 随机 cue：每条与后续 cue 留足间隔，避免 tidy 的 gap 调整干扰比对
        cues, t = [], 0.0
        for _ in range(rng.randint(3, 12)):
            a = round(t + rng.uniform(0, 40), 3)
            b = round(a + rng.uniform(1.0, 3.0), 3)     # ≥1s，避开 min_duration
            cues.append(cue(a, b, f"c{trial}_{len(cues)}"))
            t = b + 1.0
        total_cues += len(cues)

        want = reference_map(cues, hl)
        got, dropped = S.remap_to_timeline(cues, hl)

        if len(got) != len(want):
            mismatches.append(f"试{trial}: 条数 {len(got)} != {len(want)}")
            continue
        for g, w in zip(got, want):
            if abs(g.start - w.start) > 1e-4 or abs(g.end - w.end) > 1e-4:
                mismatches.append(
                    f"试{trial}: {w.main} 期望 {w.start:.4f}~{w.end:.4f}，"
                    f"实得 {g.start:.4f}~{g.end:.4f}")
                break
    ck(f"★ {total_cues} 条 cue 全部与金标准一致",
       not mismatches, mismatches[0] if mismatches else "300 组全过")

    # ── 3. ★ 单片段时与 remap_to_clip 等价（交叉验证）──
    head("3. ★ 交叉验证：单片段时两个映射函数必须等价")
    bad = []
    for trial in range(60):
        a = round(rng.uniform(0, 500), 3)
        b = a + round(rng.uniform(20, 80), 3)
        cues = []
        t = a
        while t < b - 1.0:
            ln = round(rng.uniform(1.0, 3.0), 3)
            cues.append(cue(round(t, 3), round(min(t + ln, b), 3)))
            t += ln + rng.uniform(0.2, 1.0)
        tw, _ = S.remap_to_timeline(cues, [(a, b)])
        tc, _ = S.remap_to_clip(cues, a, b)
        if len(tw) != len(tc):
            bad.append(f"试{trial}: 条数 {len(tw)} vs {len(tc)}")
            continue
        for x, y in zip(tw, tc):
            if abs(x.start - y.start) > 1e-4 or abs(x.end - y.end) > 1e-4:
                bad.append(f"试{trial}: {x.start:.4f} vs {y.start:.4f}")
                break
    ck("★ 单片段（时间线 = 单段平移）两者结果一致",
       not bad, bad[0] if bad else "60 组全过")

    # ── 4. 计数守恒（任意输入）─────────────────────────
    head("4. ★ 计数守恒：保留 + 丢弃 == 输入（覆盖全部输入）")
    HL2 = [(100.0, 130.0), (500.0, 520.0), (900.0, 915.0)]
    cues = [cue(float(t), float(t) + 2.0, f"x{t}")
            for t in range(0, 1000, 3)]            # 333 条，覆盖所有区间
    got, dropped = S.remap_to_timeline(cues, HL2)
    # 注意：这里用 `==` 是成立的，因为本用例的片段互不相邻、
    # cue 又都短于片段 → 一条 cue 只会产出至多一条。
    # 若 cue 跨越多个不相邻片段，一条可能产出多条，等式会变成 `≥`。
    ck("★ 守恒成立", len(got) + dropped == len(cues),
       f"{len(got)} + {dropped} = {len(got) + dropped}（输入 {len(cues)}）")
    ck("确实有保留也有丢弃（不是恒 0 的假通过）",
       0 < len(got) < len(cues), f"保留 {len(got)}，丢弃 {dropped}")

    # 保留条数应等于"落在片段内且够长的 cue 数"（由参考实现算）
    want_n = len(reference_map(cues, HL2))
    ck("保留条数与金标准一致", len(got) == want_n,
       f"实得 {len(got)}，金标准 {want_n}")

    # ── 5. 边界：落在片段外 / 跨边界 ────────────────────
    head("5. 边界处理")
    HL3 = [(100.0, 120.0)]
    g, d = S.remap_to_timeline([cue(50.0, 52.0)], HL3)
    ck("完全在片段之前 → 丢弃并计数", not g and d == 1, f"g={g} d={d}")
    g, d = S.remap_to_timeline([cue(200.0, 202.0)], HL3)
    ck("完全在片段之后 → 丢弃并计数", not g and d == 1, f"g={g} d={d}")

    # 跨右边界：落在片段内 1.0s / 总 4.0s = 0.25 < 0.55 → 丢
    g, d = S.remap_to_timeline([cue(119.0, 123.0)], HL3)
    ck("跨右边界且占比不足 → 丢弃", not g and d == 1,
       f"占比 {(120-119)/4:.2f}")

    # 跨右边界：落在片段内 3.0s / 总 4.0s = 0.75 ≥ 0.55 → 保留并裁剪
    g, d = S.remap_to_timeline([cue(117.0, 121.0)], HL3)
    ck("跨右边界且占比足够 → 保留并裁到边界",
       len(g) == 1 and abs(g[0].end - 20.0) < 1e-6,
       f"end={g[0].end if g else None}（片段时长 20s）")

    # 恰好落在片段起点
    g, _ = S.remap_to_timeline([cue(100.0, 102.0)], HL3)
    ck("从片段起点开始 → 映射到 0s",
       len(g) == 1 and abs(g[0].start) < 1e-6, f"start={g[0].start if g else None}")

    # ── 5b. ★ 相邻片段：接缝处不能断开 ──────────────────
    head("5b. ★ 源视频上相邻的两个片段：接缝处必须连续")
    HL_adj = [(10.0, 25.0), (25.0, 40.0)]      # 时间线 0~15 / 15~30

    g, d = S.remap_to_timeline([cue(24.0, 27.0)], HL_adj)
    ck("★ 跨接缝的 cue 保留**完整**（14~17，不是 15~17）",
       len(g) == 1 and abs(g[0].start - 14.0) < 1e-6
       and abs(g[0].end - 17.0) < 1e-6,
       f"{g[0].start if g else None}~{g[0].end if g else None}")

    g, d = S.remap_to_timeline([cue(24.5, 25.5)], HL_adj)
    ck("★ 接缝两侧各半的 cue **不该被丢**",
       len(g) == 1 and abs(g[0].start - 14.5) < 1e-6
       and abs(g[0].end - 15.5) < 1e-6,
       (f"{g[0].start:.2f}~{g[0].end:.2f}" if g else f"被丢弃 d={d}"))

    # 对照组：不相邻的片段，同一条 cue 仍应丢
    g, d = S.remap_to_timeline([cue(24.0, 27.0)], [(10.0, 25.0), (100.0, 115.0)])
    ck("★ 对照组：片段不相邻时，跨接缝的 cue 仍然丢弃",
       not g and d == 1, f"g={g} d={d}")

    # 时间线连续推论：相邻片段的映射结果应当首尾相接
    g, _ = S.remap_to_timeline([cue(20.0, 22.0), cue(30.0, 32.0)], HL_adj)
    ck("相邻片段里各自的 cue 落点正确（10~12 / 20~22）",
       len(g) == 2 and abs(g[0].start - 10.0) < 1e-6
       and abs(g[1].start - 20.0) < 1e-6,
       str([(round(x.start, 2), round(x.end, 2)) for x in g]))

    # ── 6. 字段保真 ────────────────────────────────────
    head("6. 字段保真：源时间戳 / 双语 / 正文都不能丢")
    c = cue(105.0, 107.0, main="中文原文", sub="English translation")
    g, _ = S.remap_to_timeline([c], HL3)
    ck("★ src_start/src_end 原样保留（验收要用来核对回原片）",
       len(g) == 1 and g[0].src_start == 105.0 and g[0].src_end == 107.0,
       f"{g[0].src_start if g else None}~{g[0].src_end if g else None}")
    ck("main 保留", g and g[0].main == "中文原文", g[0].main if g else "")
    ck("★ sub（译文）保留 —— 双语字幕不能在这一步丢",
       g and g[0].sub == "English translation", g[0].sub if g else "")
    ck("start/end 是时间线时间（≠ 源时间）",
       g and abs(g[0].start - 5.0) < 1e-6, f"{g[0].start if g else None}")

    # ── 7. 时间线总长与越界 ────────────────────────────
    head("7. 时间线总长：所有字幕都落在 [0, 总时长] 内")
    HL4 = [(200.0, 230.0), (400.0, 415.0), (800.0, 812.0)]
    total = sum(b - a for a, b in HL4)
    cues4 = [cue(float(t), float(t) + 2.0) for t in range(195, 815, 2)]
    g4, _ = S.remap_to_timeline(cues4, HL4)
    ck("没有负时间", all(x.start >= -1e-9 for x in g4),
       f"min={min((x.start for x in g4), default=0):.4f}")
    ck("没有超过时间线总长（含 tidy 的边界夹取）",
       all(x.end <= total + 1e-6 for x in g4),
       f"最大 end={max((x.end for x in g4), default=0):.4f}，总长 {total}")
    ck("字幕按时间排序", all(
        a.start <= b.start for a, b in zip(g4, g4[1:])), "")

    # ── 8. 顺序即时间线顺序 ────────────────────────────
    head("8. highlights 的顺序就是时间线顺序（换个顺序结果必须变）")
    segs = [(100.0, 110.0), (200.0, 210.0)]
    one = cue(205.0, 207.0)                      # 属于第二个片段
    g_fwd, _ = S.remap_to_timeline([one], segs)
    g_rev, _ = S.remap_to_timeline([one], list(reversed(segs)))
    ck("正序：第二段在时间线 10s 起",
       len(g_fwd) == 1 and abs(g_fwd[0].start - 15.0) < 1e-6,
       f"start={g_fwd[0].start if g_fwd else None}")
    ck("★ 逆序：同一 cue 落到 5s（证明顺序真的生效）",
       len(g_rev) == 1 and abs(g_rev[0].start - 5.0) < 1e-6,
       f"start={g_rev[0].start if g_rev else None}")

    # ── 9. 空 / 退化输入 ───────────────────────────────
    head("9. 空与退化输入不能崩")
    ck("空 cues → 空结果、0 丢弃", S.remap_to_timeline([], HL3) == ([], 0))
    g, d = S.remap_to_timeline([cue(105.0, 107.0)], [])
    ck("空 highlights → 全丢弃", not g and d == 1, f"d={d}")
    # 零长片段：不会匹配任何 cue，但也不能崩
    g, d = S.remap_to_timeline([cue(10.0, 12.0)], [(10.0, 10.0)])
    ck("零长片段不崩", isinstance(d, int), f"d={d}")
    # 极短 cue（< _MIN_CUE_VISIBLE）→ 丢
    g, d = S.remap_to_timeline([cue(105.0, 105.1)], HL3)
    ck("极短 cue（0.1s < 阈值 0.25s）→ 丢弃", not g and d == 1, f"d={d}")

    # ── 10. 相邻片段交界处（tidy 不得制造重叠）──────────
    head("10. 相邻片段交界：字幕不得重叠")
    HL5 = [(0.0, 10.0), (20.0, 30.0)]           # 时间线 0~10 / 10~20
    near = [cue(9.0, 10.0), cue(20.0, 21.0)]    # 一条贴左段末尾，一条贴右段开头
    g5, _ = S.remap_to_timeline(near, HL5)
    ck("两条都保留（各自完整落在片段内）", len(g5) == 2, f"{len(g5)}")
    if len(g5) == 2:
        ck("★ 交界处没有重叠（tidy 的 gap 生效）",
           g5[0].end <= g5[1].start + 1e-9,
           f"第一条 end={g5[0].end:.4f}，第二条 start={g5[1].start:.4f}")
        ck("第二条落点正确（10s = 第一段总长）",
           abs(g5[1].start - 10.0) < 1e-6, f"{g5[1].start:.4f}")

    print(f"\n{'=' * 74}")
    print(f"通过 {PASS} / {PASS + FAIL}"
          + (f"　失败 {FAIL}" if FAIL else "　全部通过"))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
