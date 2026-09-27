# -*- coding: utf-8 -*-
"""阶段 4-B 补测：翻译质量**系统评估**（不是抽样看一眼）。

为什么单独做
------------
阶段 4-B 交付时的"质量验证"只是**抽样判读**（译文自然、无明显错译），
样本还混着歌词。那只能回答"它能翻"，回答不了"它翻得够不够好、在哪类内容上会翻车"。

所以这里做一次分层评估：

    ① 分层采样  按句长与内容特征分层，覆盖不同难度 —— 而不是"取前 40 条"
    ② 客观指标  条数对齐 / 长度合规 / 未翻译 / **回译相似度**
    ③ LLM 审校  让模型扮演审校员，逐条找漏译·错译·翻译腔·术语不一致
    ④ 人工判读  输出中英对照表（报告里），供人过目

为什么用「回译相似度」当客观指标
--------------------------------
没有参考译文（reference translation），BLEU 之类的指标用不了。
回译（英 → 中）能测**信息保真度**：译文 A 回译成中文后若与原意接近，
说明关键信息没丢。它测不出文采，但能抓出"漏译/改意"这类硬伤 ——
而这正是字幕翻译最不可接受的。

运行：python stage0/probe_resolve/probe_translate_quality.py [--limit 12]
"""
from __future__ import annotations

import difflib
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq import analyzer, prompts, translate  # noqa: E402

SRT = Path(r"C:\未明子录播\260925\[未明子]2026年09月25日00时录播.srt")
PER_BUCKET = 12

REVIEW_SYSTEM = (
    "你是字幕翻译的**审校员**。给你的是一份中英对照的字幕。\n"
    "请逐条检查，只报告**确实有问题**的条目，不要为了凑数而挑刺。\n"
    "问题分类（严格用这四个之一）：\n"
    "  · 漏译 —— 原文有的信息在译文里没了\n"
    "  · 错译 —— 意思翻反了或明显不对\n"
    "  · 翻译腔 —— 意思对，但不像英语母语者会说的话\n"
    "  · 术语不一致 —— 同一个人名/概念在不同条目里译法不同\n"
    "输出 JSON 数组，每项：\n"
    '  {"n": 条目编号, "type": "类别", "why": "10~25 字的说明", '
    '"suggest": "建议译法（可选，可为空字符串）"}\n'
    "没有任何问题时输出 []。不要输出其他文字。"
)

# ★ 主判据：让模型给"忠实度"打分，同时**标出原文本身是否可理解**。
#
# 为什么必须分开标"原文质量"
# ------------------------
# 第一版评估用**字符级回译相似度**当客观指标，结果一片惨淡
# （53 条均值 0.476，16 条 < 0.35）。逐条看下去才发现指标本身是错的：
#
#     「操了」→「Damn it.」→「该死」        相似度 0.00，但翻译完全正确
#     「我操」→「Fuck.」  →「该死」        相似度 0.00，但脏话分级很地道
#     「装模作样的，假模假样的」→「Just putting on an act, all fake.」
#                                         相似度 0.11，意思却完全一致
#
# 中译英时口语、同义反复、脏话必然换一种说法，回译中文又是第三种说法 ——
# **字面重合度低不代表信息丢了**。这个指标测的是"字面"，不是"语义"。
#
# 更关键的是另一件事：抽样里大量条目**原文本身就是垃圾**
# （ASR 同音错字：「Engine回经键」应为 Enter 回车键；「调子里面」应为局子里面；
#   夹杂西里尔乱码；还有成段的歌词）。
# 在"原文不可解"的输入上，翻译再怎么努力也必然出错 ——
# 把这类错误算进翻译质量，等于让翻译替上游的 ASR 背锅。
#
# ⇒ 改为两步：先判原文可理解性，再在**原文可解的子集**上评翻译。
SCORE_SYSTEM = (
    "你是字幕翻译质量评估员。下面每条给出一句中文原文与它的英文译文。\n"
    "请对每条输出两个判断：\n"
    "  src  —— 原文本身是否可理解：\n"
    "          clear   = 意思清楚，能正常翻译\n"
    "          dubious = 大概能猜，但有明显问题（同音错字、缺字、语义跳跃）\n"
    "          broken  = 基本不可解（乱码、外语夹生、纯歌词碎片、单字无语境）\n"
    "  score —— 0~100：**英文是否忠实且自然地传达了中文的意思**\n"
    "          90+ 准确自然／70~89 基本达意但略生硬／"
    "50~69 有偏差或明显翻译腔／<50 意思丢了或翻了反\n"
    "          ⚠️ 若 src 是 broken，score 请按「在原文不可解的前提下，"
    "译文是否做了合理的推测」来给，并在 note 里写明。\n"
    "  note  —— 15 字内说明（没问题就留空）\n"
    "严格输出 JSON 数组："
    '[{"n":1,"src":"clear","score":88,"note":""}]\n'
    "不要输出其他文字。"
)


def score_pairs(pairs: list[tuple[str, str, str]], transport,
                batch: int = 20) -> tuple[list[dict], float]:
    """让模型逐条打分 + 判原文可理解性。返回 (评分列表, 花费)。"""
    import json
    out: list[dict] = []
    cost = 0.0
    for start in range(0, len(pairs), batch):
        chunk = pairs[start:start + batch]
        body = "\n".join(f"{i}. 中：{zh}\n   英：{en}"
                         for i, (zh, en, _) in enumerate(chunk, start + 1))
        res = transport.analyze_text(
            f"评估以下 {len(chunk)} 条：\n\n{body}", system=SCORE_SYSTEM,
            max_tokens=2500, thinking="off", timeout=180)
        cost += getattr(res.usage, "cost_cny", 0.0) or 0.0
        text = translate._clean(res.text or "")   # noqa: SLF001
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            continue
        try:
            data = json.loads(m.group(0))
        except Exception:                          # noqa: BLE001
            continue
        for d in data:
            if isinstance(d, dict) and "n" in d:
                try:
                    out.append({"n": int(d["n"]),
                                "src": str(d.get("src") or "clear"),
                                "score": int(d.get("score") or 0),
                                "note": str(d.get("note") or "")})
                except (TypeError, ValueError):
                    continue
    return out, cost


# ─────────────────────────────────────────────────────────────
# 采样
# ─────────────────────────────────────────────────────────────
def load_srt_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if not s or s.isdigit() or "-->" in s:
            continue
        out.append(s)
    return out


def bucket_of(text: str) -> str:
    """给一条字幕分类。分层是为了**覆盖不同难度**，而不是随机抽样。"""
    n = len(text)
    if re.search(r"\d", text) and re.search(r"[A-Za-z]{2,}", text):
        return "数字+英文"
    if re.search(r"\d", text):
        return "含数字"
    if re.search(r"[A-Za-z]{3,}", text):
        return "含英文"
    if n <= 8:
        return "短句≤8"
    if n <= 20:
        return "中句9-20"
    return "长句>20"


def sample(lines: list[str], per_bucket: int) -> dict[str, list[str]]:
    """按桶均匀取样。同一句只进一个桶。"""
    buckets: dict[str, list[str]] = {}
    for t in lines:
        buckets.setdefault(bucket_of(t), []).append(t)

    picked: dict[str, list[str]] = {}
    for name, items in buckets.items():
        if len(items) <= per_bucket:
            picked[name] = items
            continue
        # 均匀跨整个素材取样（避免全落在开头，那里多半是开场歌）
        step = len(items) / per_bucket
        picked[name] = [items[int(i * step)] for i in range(per_bucket)]
    return picked


# ─────────────────────────────────────────────────────────────
# 指标
# ─────────────────────────────────────────────────────────────
def sim(a: str, b: str) -> float:
    """字符级相似度（去标点空白）。"""
    strip = lambda s: re.sub(r"[\s\W]+", "", s or "")  # noqa: E731
    x, y = strip(a), strip(b)
    if not x and not y:
        return 1.0
    if not x or not y:
        return 0.0
    return difflib.SequenceMatcher(None, x, y).ratio()


def metrics(pairs: list[tuple[str, str, str]]) -> dict:
    """pairs = [(原文, 译文, 回译)]"""
    total = len(pairs)
    if not total:
        return {}
    too_long = [e for _, e, _ in pairs if len(e) > translate.MAX_LINE_CHARS]
    untranslated = [zh for zh, e, _ in pairs if sim(zh, e) > 0.85 or not e.strip()]
    sims = [sim(zh, back) for zh, _, back in pairs]
    return {
        "total": total,
        "over_length": len(too_long),
        "untranslated": len(untranslated),
        "rt_mean": round(sum(sims) / len(sims), 3),
        "rt_min": round(min(sims), 3),
        "rt_low": sum(1 for s in sims if s < 0.35),
    }


# ─────────────────────────────────────────────────────────────
def llm_review(pairs: list[tuple[str, str, str]], transport) -> list[dict]:
    body = "\n".join(f"{i}. 中：{zh}\n   英：{en}"
                     for i, (zh, en, _) in enumerate(pairs, 1))
    prompt = (f"以下是 {len(pairs)} 条中英对照字幕，请审校：\n\n{body}\n\n"
              "输出问题清单 JSON 数组。")
    res = transport.analyze_text(prompt, system=REVIEW_SYSTEM,
                                 max_tokens=2000, thinking="off", timeout=180)
    return _parse_issues(res.text or "")


def _parse_issues(raw: str) -> list[dict]:
    import json
    text = translate._clean(raw)          # noqa: SLF001
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:                     # noqa: BLE001
        return []
    out: list[dict] = []
    for d in data:
        if isinstance(d, dict) and "n" in d:
            out.append({"n": int(d.get("n") or 0),
                        "type": str(d.get("type") or "未分类"),
                        "why": str(d.get("why") or ""),
                        "suggest": str(d.get("suggest") or "")})
    return out


def main() -> int:
    limit = PER_BUCKET
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = max(3, int(sys.argv[i + 1]))

    all_lines = load_srt_lines(SRT)
    if not all_lines:
        print(f"读不到字幕：{SRT}")
        return 1

    picked = sample(all_lines, limit)
    flat: list[tuple[str, str]] = []      # (桶名, 原文)
    for name in sorted(picked):
        for t in picked[name]:
            flat.append((name, t))

    print(f"字幕库：{len(all_lines)} 条")
    print("分层采样：")
    for name in sorted(picked):
        print(f"  {name:10} {len(picked[name]):>3} 条")
    print(f"合计 {len(flat)} 条\n")

    transport = analyzer.make_transport()
    t0 = time.time()

    print("[1] 翻译（产品代码路径）")
    srcs = [t for _, t in flat]
    res = translate.translate_lines(srcs, transport=transport, batch=20)
    print(f"    条数 {len(res.lines)}/{len(srcs)}｜失败 {res.failed}｜"
          f"¥{res.cost_cny:.5f}｜{time.time() - t0:.1f}s")

    print("[2] 回译（英 → 中），用于测信息保真")
    back = translate.translate_lines(res.lines, transport=transport,
                                     target_lang="zh", batch=20)
    print(f"    条数 {len(back.lines)}/{len(res.lines)}｜{time.time() - t0:.1f}s")

    pairs = [(srcs[i], res.lines[i], back.lines[i]) for i in range(len(srcs))]

    print("\n[3] 客观指标")
    m = metrics(pairs)
    print(f"    条数对齐        {m['total']}/{m['total']}（{len(res.lines)} vs {len(srcs)}）")
    print(f"    超长(>{translate.MAX_LINE_CHARS}字符)   {m['over_length']} 条")
    print(f"    未翻译/空译     {m['untranslated']} 条")
    print(f"    回译相似度均值  {m['rt_mean']}（最低 {m['rt_min']}）"
          f"  ← ⚠️ 仅供参考，见下方说明")

    print("\n[4] LLM 审校（找具体问题）")
    issues = llm_review(pairs, transport)
    by_type: dict[str, int] = {}
    for it in issues:
        by_type[it["type"]] = by_type.get(it["type"], 0) + 1
    print(f"    发现 {len(issues)} 条问题：{by_type or '无'}")

    print("\n[5] ★ 分组评分（把「原文本身不可解」单独摘出来）")
    scores, sc_cost = score_pairs(pairs, transport)
    said = {s["n"]: s for s in scores}
    clear = [s for s in scores if s["src"] == "clear"]
    notclear = [s for s in scores if s["src"] != "clear"]
    avg = lambda xs: round(sum(x["score"] for x in xs) / len(xs), 1) if xs else None  # noqa: E731
    print(f"    评到 {len(scores)}/{len(pairs)} 条")
    print(f"    原文 clear    {len(clear):>3} 条｜平均分 {avg(clear)}")
    print(f"    原文可疑/不可解 {len(notclear):>3} 条｜平均分 {avg(notclear)}"
          f"  ← 这部分不该记在翻译头上")
    dist = {"90+": 0, "70-89": 0, "50-69": 0, "<50": 0}
    for s in clear:
        k = ("90+" if s["score"] >= 90 else "70-89" if s["score"] >= 70
             else "50-69" if s["score"] >= 50 else "<50")
        dist[k] += 1
    print(f"    clear 子集分布：{dist}")

    print("\n[5b] 关于回译相似度为什么不能当主判据")
    print("    「操了」→「Damn it.」→「该死」  相似度 0.00，翻译却完全正确；")
    print("    口语、同义反复、脏话在跨语言时必然换说法，再回译又是第三种说法。")
    print("    字面重合度低 ≠ 信息丢了。所以主判据改用上面的分组评分。")

    print("\n[6] 中英对照（供人判读）")
    print("=" * 96)
    for i, (name, (zh, en, bk)) in enumerate(zip([n for n, _ in flat], pairs), 1):
        s = said.get(i)
        flag = ""
        bad = [x for x in issues if x["n"] == i]
        if bad:
            flag += f"  ⚠️ {bad[0]['type']}：{bad[0]['why']}"
        if s:
            flag += f"  ｜原文 {s['src']}｜评分 {s['score']}"
            if s["note"]:
                flag += f"（{s['note']}）"
        print(f"[{i:2d}] ({name}) {zh}")
        print(f"     → {en}{flag}")
        print(f"     ← {bk}")
    print("=" * 96)

    # 落一份机器可读结果，供写报告用
    import json
    out = ROOT / "reports" / "assets" / "translate_quality_eval.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "samples": len(pairs), "buckets": {k: len(v) for k, v in picked.items()},
        "metrics": m,
        "scores": scores,
        "avg_clear": avg(clear), "avg_notclear": avg(notclear),
        "src_dist": {k: sum(1 for s in scores if s["src"] == k)
                     for k in ("clear", "dubious", "broken")},
        "issues": issues,
        "cost_cny": res.cost_cny + back.cost_cny + sc_cost,
        "pairs": [{"bucket": n, "zh": zh, "en": en, "back": bk,
                   "rt_sim": round(sim(zh, bk), 3),
                   "src": (said.get(i) or {}).get("src"),
                   "score": (said.get(i) or {}).get("score")}
                  for i, ((n, _x), (zh, en, bk))
                  in enumerate(zip(flat, pairs), 1)],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已落盘：{out}")
    print(f"总花费 ¥{res.cost_cny + back.cost_cny + sc_cost:.5f}｜"
          f"总耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
