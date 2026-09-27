# -*- coding: utf-8 -*-
"""阶段 4-B 前置实测：LLM 精翻的质量与成本（决定双语字幕的架构）。

背景：免费网页翻译通道实测只剩 MyMemory 一个可用
（Google 被墙、必应 ttranslatev3 已对非浏览器会话返回 401、
LibreTranslate 公共实例全 403）。见 probe_translate_sources.py。

所以要重新回答一个问题：**原设计"免费初翻 + LLM 反思审校"还成立吗？**
如果免费初翻质量太差，它不但省不下钱，还会把错误引给 LLM。

本探针对比三种做法，用**真实直播字幕**（口语、梗、脏话密集）作样本：

    A. MyMemory 免费直翻
    B. LLM 一次直翻
    C. LLM 反思式（一次调用内要求：先直译 → 对照原文审校 → 定稿）

输出译文供人眼判读，并量出 token 与费用。

运行：python stage0/probe_resolve/probe_llm_translate.py
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from sliceq import analyzer  # noqa: E402

SRT = Path(r"C:\未明子录播\260925\[未明子]2026年09月25日00时录播.srt")
TIMEOUT = 60


def load_srt_lines(path: Path, limit: int = 6, skip: int = 0) -> list[str]:
    """从真实 SRT 里取若干条字幕文本（跳过序号与时间行）。"""
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        if skip > 0:
            skip -= 1
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


DIRECT_SYS = (
    "你是专业的影视字幕翻译。把用户给的中文台词翻译成英文。\n"
    "要求：\n"
    "1. 保持口语的自然语感，不要翻译腔；\n"
    "2. 单行不超过 42 个字符（字幕一行放不下会溢出被裁）；\n"
    "3. 只输出译文本身，不要解释、不要引号、不要原文。"
)

REFLECT_SYS = (
    "你是资深的影视字幕译者，正在做**精翻**。\n"
    "请在心里走完这三步，然后只输出最终定稿：\n"
    "  ① 直译：先求意思准确；\n"
    "  ② 审校：对照中文原文检查有没有漏译、错译、语气走样（口语、脏话、"
    "网络梗要找到英文里等价的表达，不要生硬直译）；\n"
    "  ③ 定稿：读一遍英文，改掉翻译腔，确保像母语者说的话。\n"
    "约束：单行不超过 42 个字符；只输出定稿译文，不要输出①②的过程。"
)


def mymemory(text: str) -> tuple[str, float]:
    t0 = time.time()
    url = ("https://api.mymemory.translated.net/get?q="
           + urllib.parse.quote(text) + "&langpair=zh-CN|en")
    r = requests.get(url, timeout=15,
                     headers={"User-Agent": "SliceQ/0.1"})
    d = r.json()
    return (d.get("responseData") or {}).get("translatedText", ""), time.time() - t0


def llm(transport, text: str, system: str, thinking: str = "off"):
    t0 = time.time()
    res = transport.analyze_text(text, system=system, max_tokens=800,
                                 thinking=thinking, timeout=TIMEOUT)
    return res.text.strip(), time.time() - t0, res.usage


def main() -> int:
    # 跳过开头的歌词（直播开场在放歌），取更像台词的中段
    lines = load_srt_lines(SRT, 20, skip=60)
    if not lines:
        print(f"读不到样本字幕：{SRT}")
        return 1

    print(f"样本来源：{SRT.name}")
    print(f"取第 61 条起的 {len(lines)} 条真实台词\n")
    for i, t in enumerate(lines[:6], 1):
        print(f"  原文{i}：{t}")
    print()

    # 拼成一次请求（真实使用里就是一批一批翻的）
    joined = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(lines))

    transport = analyzer.make_transport()
    print(f"通道：{type(transport).__name__}｜模型：{getattr(transport, 'model', '?')}\n")

    print("=" * 78)
    print("A. MyMemory 免费直翻")
    print("=" * 78)
    try:
        txt, dt = mymemory(joined)
        print(f"[{dt:.2f}s / 免费]\n{txt[:600]}\n")
    except Exception as exc:                       # noqa: BLE001
        print(f"失败：{type(exc).__name__}: {exc}\n")

    results = {}
    for key, sys_prompt, label in (
            ("B", DIRECT_SYS, "B. LLM 一次直翻"),
            ("C", REFLECT_SYS, "C. LLM 反思式精翻（单次调用内自审）")):
        print("=" * 78)
        print(label)
        print("=" * 78)
        try:
            txt, dt, usage = llm(transport, joined, sys_prompt)
            print(f"[{dt:.2f}s｜in={usage.input_tokens} out={usage.output_tokens} "
                  f"reasoning={getattr(usage, 'reasoning_tokens', 0)}"
                  f"｜¥{usage.cost_cny:.5f}]\n{txt[:700]}\n")
            results[key] = (txt, dt, usage)
        except Exception as exc:                   # noqa: BLE001
            print(f"失败：{type(exc).__name__}: {exc}\n")

    print("=" * 78)
    print("汇总")
    print("=" * 78)
    print(f"样本规模：{len(lines)} 条 / {len(joined)} 字")
    for k, (txt, dt, u) in results.items():
        per_char = u.cost_cny / max(1, len(joined))
        print(f"  {k}: ¥{u.cost_cny:.5f}｜{dt:.1f}s｜"
              f"单字 ¥{per_char:.7f}｜外推 1 分钟字幕(≈250字) ¥{per_char * 250:.5f}")
    print("\n判读要点：")
    print("  · 译文有没有漏译 / 语义跑偏（尤其口语与梗）")
    print("  · 是否有翻译腔（像不像母语者说的话）")
    print("  · 单行是否超 42 字符（超了会在成片里溢出被裁）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
