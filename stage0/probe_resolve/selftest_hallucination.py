# -*- coding: utf-8 -*-
"""幻觉治理的自测（阶段 4-D 补强）。

## 为什么要补

阶段 4 的真实端到端里发现：**短幻觉完全逃过治理**。
一段 90s 素材里 `感谢观看`（4 字）独立成段出现 **5 次**，
而 `_REPEAT_MIN_LEN = 8` 让它根本不参与判重。

修复引入了两档门槛（长句 3 次 / 短句 5 次）。这个改动**风险很高** ——
判重是"删内容"的操作，门槛设低了会把主播真说的话删掉，
而且**删掉的内容不会有人发现**（转录少了几句，谁看得出来）。

所以这里要双向验证：

    该清的清了（短幻觉、长幻觉、平台模板词）
    不该清的**一条都不能少**（真实口语重复、极短应答词）

⚠️ 第二类断言比第一类重要得多。只测"幻觉被清了"是不够的 ——
   把门槛降到 2 次也能让那几条通过。
"""
from __future__ import annotations

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

from sliceq import asr                                    # noqa: E402

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
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def S(text: str, start: float = 0.0, end: float = 1.0):
    return asr.Segment(start_ms=int(start * 1000), end_ms=int(end * 1000),
                       text=text)


def texts(segs) -> list[str]:
    return [s.text for s in segs]


def main() -> int:
    # ── 1. 常量自洽 ────────────────────────────────────
    head("1. 门槛常量本身要自洽")
    ck("短句门槛低于长句门槛（否则短句档永远不会生效）",
       asr._REPEAT_SHORT_LEN < asr._REPEAT_MIN_LEN,
       f"{asr._REPEAT_SHORT_LEN} vs {asr._REPEAT_MIN_LEN}")
    ck("短句要求比长句更多次（这是不误杀的代价）",
       asr._REPEAT_SHORT_COUNT > asr._REPEAT_MIN_COUNT,
       f"{asr._REPEAT_SHORT_COUNT} vs {asr._REPEAT_MIN_COUNT}")
    ck("极短句门槛 ≥ 4（不能把 2~3 字的应答词卷进来）",
       asr._REPEAT_SHORT_LEN >= 4, str(asr._REPEAT_SHORT_LEN))

    # ── 2. 短幻觉（这次修的）───────────────────────────
    head("2. 短幻觉：4 字句重复 5 次要清掉（实测样本）")
    segs = [S("感谢观看") for _ in range(5)]
    kept, dropped = asr.drop_repeated(segs)
    ck("★ 5 次「感谢观看」全部被清", dropped == 5 and len(kept) == 0,
       f"剔除 {dropped}，剩 {len(kept)}")

    segs = [S("感谢观看") for _ in range(4)]
    kept, dropped = asr.drop_repeated(segs)
    ck("4 次不判（保守：未达短句门槛就不动）",
       dropped == 0 and len(kept) == 4, f"剔除 {dropped}")

    # ── 3. 长幻觉门槛没被改坏 ──────────────────────────
    head("3. 长句仍按 3 次门槛（不能因为改短句档把长句档弄坏）")
    long_h = "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"
    segs = [S(long_h) for _ in range(3)]
    kept, dropped = asr.drop_repeated(segs)
    ck("★ 长句重复 3 次被清", dropped == 3 and len(kept) == 0,
       f"剔除 {dropped}（长度 {len(long_h.replace(' ', ''))} 字）")
    segs = [S(long_h) for _ in range(2)]
    kept, dropped = asr.drop_repeated(segs)
    ck("长句重复 2 次不判", dropped == 0, f"剔除 {dropped}")

    # ── 4. ★ 不误杀：这是最重要的一组 ──────────────────
    head("4. ★ 不能误杀真实内容")
    segs = [S("好"), S("啊"), S("对"), S("好"), S("对"), S("嗯"),
            S("好"), S("对"), S("好"), S("对")]
    kept, dropped = asr.drop_repeated(segs)
    ck("★ 极短应答词（1 字）重复多次全部保留",
       dropped == 0 and len(kept) == 10, f"剔除 {dropped}")

    segs = [S("我知道了"), S("我知道了"), S("我知道了")]
    kept, dropped = asr.drop_repeated(segs)
    ck("★ 真实口语短句重复 3 次保留（旧门槛的意图被守住了）",
       dropped == 0 and len(kept) == 3, f"剔除 {dropped}")

    segs = [S("我知道了") for _ in range(5)]
    kept, dropped = asr.drop_repeated(segs)
    ck("同一句 5 次才判 —— 这是刻意的取舍（宁可留一点幻觉也不删真话）",
       dropped == 5, f"剔除 {dropped}")

    # 长素材里的正常内容
    normal = [S("我今天讲一个问题"), S("这个问题很重要"),
              S("大家想一想"), S("对不对"), S("这就是我说的")]
    kept, dropped = asr.drop_repeated(list(normal))
    ck("★ 一段正常内容零剔除", dropped == 0 and len(kept) == 5,
       f"剔除 {dropped}")

    # ── 5. 混合场景 ────────────────────────────────────
    head("5. 混合：只清幻觉，一句真话都不能少")
    mixed = [
        S("感谢观看"),                       # 幻觉 ×1
        S("我认为这件事的本质是激励问题"),      # 真实
        S("感谢观看"),                       # ×2
        S("你想想他为什么这么做"),             # 真实
        S("感谢观看"),                       # ×3
        S("因为他的收益结构就是这个样子"),      # 真实
        S("感谢观看"),                       # ×4
        S("所以你不要指望他改变"),             # 真实
        S("感谢观看"),                       # ×5 → 到这里判为幻觉
        S("这是结构决定的"),                  # 真实
    ]
    kept, dropped = asr.drop_repeated(mixed)
    kept_texts = texts(kept)
    ck("★ 5 条幻觉全清", dropped == 5, f"剔除 {dropped}")
    ck("★ 5 条真实内容一条不少",
       all(t in kept_texts for t in
           ["我认为这件事的本质是激励问题", "你想想他为什么这么做",
            "因为他的收益结构就是这个样子", "所以你不要指望他改变",
            "这是结构决定的"]),
       f"剩下：{kept_texts}")

    # 空白归一化：带空格的同句应视为同一句
    segs = [S("感谢 观看"), S("感谢观看"), S("感谢 观看"), S("感谢观看"),
            S("感谢观看")]
    kept, dropped = asr.drop_repeated(segs)
    ck("忽略空白差异（`感谢 观看` 与 `感谢观看` 算同一句）",
       dropped == 5, f"剔除 {dropped}")

    # ── 6. 特征词 ──────────────────────────────────────
    head("6. 特征词表")
    ck("★ 「优优独播剧场」命中（这条是本次实测抓到的）",
       asr.looks_like_hallucination(
           "优优独播剧场——YoYo Television Series Exclusive"), "")
    ck("「谢谢观看」命中", asr.looks_like_hallucination("谢谢观看"), "")
    ck("「请订阅频道」命中", asr.looks_like_hallucination("请订阅我的频道"), "")
    ck("「(字幕:XXX)」命中", asr.looks_like_hallucination("(字幕:J Chong)"), "")
    ck("「下次見」命中", asr.looks_like_hallucination("下次見"), "")

    ck("★ 「感谢观看」**不**靠特征词判（避免误杀主播真说的话）",
       not asr.looks_like_hallucination("感谢观看"),
       "它走重复判据")
    ck("★ 正常句子不命中",
       not any(asr.looks_like_hallucination(t) for t in
               ["我认为这件事的本质是激励问题", "你想想他为什么这么做",
                "感谢大家一直以来的支持与陪伴", "谢谢老师讲解"]),
       "")
    ck("空文本判为幻觉（避免空段进入字幕）",
       asr.looks_like_hallucination("") and
       asr.looks_like_hallucination("   "), "")

    # ── 7. 边界 ────────────────────────────────────────
    head("7. 边界情况")
    ck("空列表不崩", asr.drop_repeated([]) == ([], 0),
       str(asr.drop_repeated([])))
    ck("全空文本不崩", asr.drop_repeated([S(""), S("   ")])[1] == 0,
       "（空串长度 0，不参与判重）")
    segs = [S("x" * 8) for _ in range(3)]
    ck("正好 8 字的句子按长句门槛（3 次）",
       asr.drop_repeated(segs)[1] == 3, "")
    segs = [S("x" * 7) for _ in range(3)]
    ck("7 字的句子按短句门槛（3 次不判）",
       asr.drop_repeated(segs)[1] == 0, "")

    print(f"\n{'=' * 72}")
    print(f"通过 {PASS} / {PASS + FAIL}"
          + (f"　失败 {FAIL}" if FAIL else "　全部通过"))
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
