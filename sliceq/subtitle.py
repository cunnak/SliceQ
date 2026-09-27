# -*- coding: utf-8 -*-
"""字幕管线 —— 转录文本 → 可交付的字幕。

流程：

    转录分段（源时间轴）
      → 语义断句（LLM 优先，规则兜底）
      → 校正（标点/去赘词）
      → ★ 时间轴重映射（源时间轴 → 成片时间轴）
      → 输出 ASS（烧录用）/ SRT（交付用）

## ★ 时间轴重映射 —— 本项目最容易出的静默缺陷

转录的时间戳对齐的是**源视频的绝对时间轴**；
而导出的是**若干不连续片段的拼接**。两者时间轴不同。

举例（乙方案：每条候选一个独立文件）：

    高光片段位于源视频 600s ~ 645s
    源视频 612s 处的一句话
    → 在该段成片里应落在 12s 处     （612 - 600）

**这个减法漏做的后果**：视频切好了、字幕也烧上去了、格式完全合法、
一句错都不报 —— 但**字幕整体错位**。用户看到的是"字幕全乱"，无从排查。
属典型的「功能正常、结果全错」缺陷（TECH-DESIGN §6.4.3）。

因此本模块提供**唯一**的映射函数 `remap_to_clip()`，
`editor.py` 烧录时必须调用它，**禁止各写一份**（否则 SRT 版与烧录版会不一致）。

## 为什么重映射这么简单（乙方案的红利）

设计初期（甲方案：OTIO 引用原始源视频 + 多段拼接）时，
重映射需要跨段累加：`t - seg[i].start + Σ(前 i 段时长)`，
累加项一多就变成 bug 高发区。

产品路线定为**乙方案（每条候选导出独立文件）**后，
时间轴只是"整体平移一个偏移量"，退化成一个减法。
这是乙方案在技术风险上的额外收益。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import prompts, subtitle_style

ProgressCb = Callable[[float, str], None]


# ─────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────
@dataclass
class Cue:
    """一条字幕。

    start/end 是**成片时间轴**（秒）。
    src_start/src_end 保留它在源视频上的位置 —— 用于人工核对
    「字幕文本 ↔ 画面对应关系」时能一秒定位回原片（验收要用）。
    """

    start: float
    end: float
    main: str = ""
    sub: str = ""
    src_start: float = 0.0
    src_end: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def is_empty(self) -> bool:
        return not (self.main.strip() or self.sub.strip())


# ─────────────────────────────────────────────────────────────
# 文本规范化（断句定位用）
# ─────────────────────────────────────────────────────────────
# 参与匹配时要去掉的字符：标点、空白、常见连接符号
_STRIP_CHARS = set(
    "，。！？、；：""''《》〈〉【】〔〕（）,.!?;:\"'()[]{}<>…—－-·~～ 　\n\r\t"
    "“”‘’"
)


def normalize(text: str) -> str:
    """规范化：去标点空白。

    用途：LLM 断句后要在原文里定位每句的位置 —— 但模型常常会补标点，
    直接字符串匹配会失败。去掉标点后再匹配，命中率高得多。
    """
    return "".join(ch for ch in (text or "") if ch not in _STRIP_CHARS).strip()


# ─────────────────────────────────────────────────────────────
# 断句
# ─────────────────────────────────────────────────────────────
# 单条字幕的长度约束（中文按字符数）
MIN_LEN = 5
MAX_LEN = 20
# 单条字幕的时长上限 —— 太长的字幕会一直挂在画面上，观感差
MAX_CUE_SECONDS = 8.0

# 句末标点（优先在这里断）
_SENT_END = re.compile(r"[。！？!?；;…]+")
# 次级断点（句子太长时退到这里）
_CLAUSE_END = re.compile(r"[，,、：:]+")


def split_by_rules(text: str, *, max_len: int = MAX_LEN,
                   min_len: int = MIN_LEN) -> list[str]:
    """规则断句 —— 免费、可预测、无外部依赖。

    策略：句末标点 → 逗号类 → 字数硬切。
    作为 LLM 断句的**兜底**：模型不可用时不能整条导出失败。
    """
    t = (text or "").strip()
    if not t:
        return []

    pieces: list[str] = []
    for sent in _SENT_END.split(t):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= max_len:
            pieces.append(sent)
            continue
        # 太长 → 按逗号切
        buf = ""
        for clause in _CLAUSE_END.split(sent):
            clause = clause.strip()
            if not clause:
                continue
            if not buf:
                buf = clause
            elif len(buf) + len(clause) + 1 <= max_len:
                buf = f"{buf}，{clause}"
            else:
                pieces.append(buf)
                buf = clause
        if buf:
            pieces.append(buf)

    # 仍超长的按字数硬切（口语里没有标点的长串）
    out: list[str] = []
    for p in pieces:
        rest = p
        while len(rest) > max_len:
            cut = _find_cut(rest, max_len)
            head = rest[:cut].strip()
            if head:
                out.append(head)
            rest = rest[cut:].lstrip("，,、：: ")
        if rest:
            out.append(rest)

    # 合并过短的碎片（避免出现"的""了"这种单字字幕）
    # 双向判：**前一条短**或**当前条短**都要尝试合并。
    # 只看前一条的话，硬切留下的 2 字尾块会原样留下来当一条字幕。
    merged: list[str] = []
    for p in out:
        can_merge = (
            bool(merged)
            and (len(merged[-1]) < min_len or len(p) < min_len)
            and len(merged[-1]) + len(p) <= max_len
        )
        if can_merge:
            merged[-1] = merged[-1] + p
        else:
            merged.append(p)

    # 尾条过短 → 并回前一条。
    # 实测硬切常留下"来做饭"这种 3 字尾巴；让它略超 max_len 也比
    # 在画面上闪一条 3 字字幕好。上限放到 1.3 倍避免真的超长。
    if len(merged) >= 2 and len(merged[-1]) < min_len:
        tail = merged.pop()
        if len(merged[-1]) + len(tail) <= int(max_len * 1.3):
            merged[-1] = merged[-1] + tail
        else:
            merged.append(tail)
    return merged


# 硬切时优先落在这类字符**之后**（标点与空白）
_BREAK_HINTS = set("，,、。！？!?；;：:… \t")


def _find_cut(text: str, target: int, *, window: int = 6) -> int:
    """在 target 附近找一个更自然的切点，尽量不把词切成两半。

    ⚠️ 这只是**缓解**，不是解决。中文没有词间空格，不引入分词器就无法
       可靠地找到词边界。真正的语义切分靠 LLM 断句（`split_by_llm`），
       本函数的定位是让**兜底路径**尽量体面 —— 目标是"不崩"，不是"好"。
    """
    n = len(text)
    if target >= n:
        return n
    hi = min(n - 1, target + window)
    lo = max(1, target - window)
    # 先向后找（宁可这一条短一点，也别切在词中间）
    for i in range(target, hi + 1):
        if text[i - 1] in _BREAK_HINTS:
            return i
    for i in range(target, lo - 1, -1):
        if i > 0 and text[i - 1] in _BREAK_HINTS:
            return i
    return target


def split_by_llm(text: str, transport, *, timeout: int = 120) -> list[str] | None:
    """LLM 语义断句。失败返回 None，由调用方退回规则断句。

    ⚠️ 会**校验模型是否改了原文** —— 模型改写文字会让时间戳定位全错，
       而这种错不会报错，只会"字幕和画面对不上"。
    """
    try:
        res = transport.analyze_text(
            prompts.split_prompt(text), system=prompts.SPLIT_SYSTEM,
            max_tokens=2000, thinking="off", timeout=timeout)
    except Exception:
        return None

    raw = (res.text or "").strip()
    if not raw:
        return None

    # 容忍模型套了 markdown 代码块
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", raw, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(data, list):
        return None
    items = [str(x).strip() for x in data if str(x).strip()]
    if not items:
        return None

    # ── 校验：拼接后的长度不能和原文差太多 ────────────────
    # 模型若改写/概括了内容，字符数会明显变化。宁可退回规则断句，
    # 也不能带着"对不上原文"的断句去做时间戳定位。
    a, b = len(normalize("".join(items))), len(normalize(text))
    if b == 0 or abs(a - b) / b > 0.15:
        return None
    return items


def split_text(text: str, *, transport=None, use_llm: bool = True,
               max_len: int = MAX_LEN) -> tuple[list[str], str]:
    """切分一段文本。返回 (短句列表, 实际使用的方式)。

    方式取值：`llm` / `rules` / `rules(llm-mismatch)` / `rules(llm-failed)`，
    便于在报告里统计"有多少条实际走了兜底"。
    """
    if use_llm and transport is not None and text.strip():
        got = split_by_llm(text, transport)
        if got:
            return got, "llm"
        return split_by_rules(text, max_len=max_len), "rules(llm-failed)"
    return split_by_rules(text, max_len=max_len), "rules"


# ─────────────────────────────────────────────────────────────
# 时间戳分配（字数比例插值）
# ─────────────────────────────────────────────────────────────
def locate_in_source(full_norm: str, piece_norm: str,
                     cursor: int) -> tuple[int, int]:
    """在规范化后的原文里定位一句话，返回 (起, 止) 字符索引。

    按 `cursor` 顺序找，保证多句的时间戳单调递增 ——
    否则模型给的顺序稍有出入，字幕就会在时间轴上跳跃。
    找不到时退化为"从 cursor 起按长度占位"，而不是抛错。
    """
    if not piece_norm:
        return cursor, cursor
    pos = full_norm.find(piece_norm, cursor)
    if pos < 0:
        # 退一步：只匹配前若干字（模型可能只改了尾部）
        for keep in (8, 5, 3):
            if len(piece_norm) >= keep:
                pos = full_norm.find(piece_norm[:keep], cursor)
                if pos >= 0:
                    return pos, pos + len(piece_norm)
        return cursor, min(len(full_norm), cursor + len(piece_norm))
    return pos, pos + len(piece_norm)


def distribute_times(start_ms: int, end_ms: int, texts: Sequence[str],
                     full_text: str) -> list[tuple[int, int, str]]:
    """把 [start_ms, end_ms] 按**字数比例**分配给切好的短句。

    依据：中文口播的语速近似均匀，按字符数分配时间的误差在几十毫秒量级
    （TECH-DESIGN §4.5 明确接受这个误差，换来不必取词级时间戳）。
    """
    full = normalize(full_text)
    n = len(full)
    span = max(0, end_ms - start_ms)
    out: list[tuple[int, int, str]] = []
    cursor = 0

    for t in texts:
        pn = normalize(t)
        a, b = locate_in_source(full, pn, cursor)
        cursor = max(cursor, b)
        if n <= 0:
            s = start_ms
            e = end_ms
        else:
            s = start_ms + int(span * a / n)
            e = start_ms + int(span * b / n)
        out.append((s, e, t))

    # 保证时长单调、不重叠、不越界
    fixed: list[tuple[int, int, str]] = []
    for i, (s, e, t) in enumerate(out):
        s = max(start_ms, min(s, end_ms))
        e = max(s, min(e, end_ms))
        if i + 1 < len(out):
            e = min(e, out[i + 1][0])      # 不侵占下一条
        if e <= s:
            e = min(end_ms, s + 80)        # 兜底给个最短可见时间
        fixed.append((s, e, t))
    return fixed


# ─────────────────────────────────────────────────────────────
# 组装
# ─────────────────────────────────────────────────────────────
def split_segments(segments: Iterable, *, transport=None,
                   use_llm: bool = True, max_len: int = MAX_LEN,
                   progress: ProgressCb | None = None
                   ) -> tuple[list[Cue], dict]:
    """把转录分段整批切成字幕条目（**仍是源时间轴**）。

    返回 (cues, stats)。stats 里记录各类短路次数，便于验收时核对
    "LLM 断句实际生效了多少条" —— 只看"跑通了"是看不出来的。
    """
    segs = list(segments)
    total = len(segs)
    cues: list[Cue] = []
    stats = {"segments": total, "llm": 0, "rules": 0,
             "rules_fallback": 0, "too_long_split": 0}

    for i, seg in enumerate(segs):
        text = (seg.text or "").strip()
        if not text:
            continue
        if progress and total:
            progress(i / total, f"断句 {i + 1}/{total}")

        texts, how = split_text(text, transport=transport, use_llm=use_llm,
                                max_len=max_len)
        if not texts:
            continue
        if how == "llm":
            stats["llm"] += 1
        elif how == "rules":
            stats["rules"] += 1
        else:
            stats["rules_fallback"] += 1

        # 单条时长过长 → 再按时间等分（避免字幕长时间停留）
        span_s = (seg.end_ms - seg.start_ms) / 1000.0
        if span_s > MAX_CUE_SECONDS and len(texts) == 1:
            texts = split_by_rules(text, max_len=max(6, max_len // 2))
            stats["too_long_split"] += 1

        for s_ms, e_ms, t in distribute_times(
                seg.start_ms, seg.end_ms, texts, text):
            cues.append(Cue(
                start=s_ms / 1000.0, end=e_ms / 1000.0, main=t,
                src_start=s_ms / 1000.0, src_end=e_ms / 1000.0))

    if progress:
        progress(1.0, "断句完成")
    return cues, stats


# ─────────────────────────────────────────────────────────────
# ★ 时间轴重映射（**唯一实现，editor.py 必须复用**）
# ─────────────────────────────────────────────────────────────
# 一句话落在片段内的比例低于此值时丢弃 —— 只露半句比不显示更糟
_MIN_KEEP_RATIO = 0.55
# 即使是完整句子，短于这个时长也不值得单独显示
_MIN_CUE_VISIBLE = 0.25


def remap_to_clip(cues: Sequence[Cue], clip_start: float, clip_end: float
                  ) -> tuple[list[Cue], int]:
    """把「源时间轴」的字幕映射到「单段成片时间轴」。

    乙方案（每条候选导出独立文件）下就是一个平移：

        timeline_t = source_t - clip_start
        source_t   = timeline_t + clip_start

    ⚠️ **必须与 editor.py 共用本函数**（TECH-DESIGN §4.5 的硬要求）。
       SRT 导出和字幕烧录各写一份的话，两者会随时间悄悄不一致 ——
       而用户拿到的 SRT 与看到的画面就会对不上。

    ⚠️ **甲方案（多条高光拼成一条时间线：OTIO / 剪映草稿）不要用本函数** ——
       那种场景要跨段累加偏移，用 `remap_to_timeline()`。
       两者都吃「源时间轴的 cues」，但输出落在**完全不同**的时间轴上；
       传错**不会报错**，只会得到一份时间戳合法、位置全错的字幕。

    返回 (映射后的 cues, 被丢弃的条数)。

    边界处理：一句话跨越片段边界时，看它落在片段内的**比例**：
      - ≥ `_MIN_KEEP_RATIO` → 保留，时间裁到片段边界
      - <  该比例          → 丢弃（宁可没有，也不要只显示半句）
    """
    span = max(0.0, clip_end - clip_start)
    out: list[Cue] = []
    dropped = 0

    for c in cues:
        a = max(c.src_start, clip_start)
        b = min(c.src_end, clip_end)
        if b <= a:
            # ⚠️ 完全落在段外 —— 也要计数。
            #    之前这里直接 continue，导致「保留数 + 丢弃数 ≠ 输入数」，
            #    报告里报出去的就是假数字（明明丢了 3 条却说丢 2 条）。
            dropped += 1
            continue

        ratio = (b - a) / max(1e-6, c.src_end - c.src_start)
        if ratio < _MIN_KEEP_RATIO or (b - a) < _MIN_CUE_VISIBLE:
            dropped += 1
            continue

        out.append(Cue(
            start=a - clip_start,
            end=b - clip_start,
            main=c.main, sub=c.sub,
            src_start=c.src_start, src_end=c.src_end,
        ))

    return tidy(out, limit=span), dropped


def remap_to_timeline(cues: Sequence[Cue],
                      highlights: Sequence[tuple[float, float]]
                      ) -> tuple[list[Cue], int]:
    """把「源时间轴」的字幕映射到**多段拼接时间线**（甲方案专用）。

    ## 它与 `remap_to_clip()` 是两件事，**不要混用**

        remap_to_clip(源 cues, clip_start, clip_end)
            乙方案：一条候选导出一个独立 mp4 → 单段平移

        remap_to_timeline(源 cues, [(start,end), ...])
            甲方案：多条高光拼成**一条时间线**（OTIO / 剪映草稿）→ 跨段累加

    ⚠️ 两者都吃「源时间轴的 cues」，但输出落在**完全不同**的时间轴上。
       传错**不会报错** —— 只会得到一份时间戳合法、位置全错的字幕。

    ## 公式

        timeline_t = source_t - highlights[i].start
                     + Σ(highlights[0..i-1].duration)

    `highlights` 的顺序**就是时间线顺序**。

    ## 算法：分段映射 → 合并 → 按**总占比**判定

    ① 把一条 cue 落在**每个**片段里的部分分别映射到时间线
    ② 时间线上连续/重叠的段**合并**成一条
    ③ 保留判据用**保留总时长 / cue 总时长**

    ⚠️ **第 ② 步是必须的**，第一版漏了它（当时用「归属唯一」：整条 cue
       判给重叠最多的那个片段，再裁到该片段边界），后果是：

           片段 = 源 10~25 与 25~40（**源视频上相邻**，时间线连续 0~15 / 15~30）
           cue  = 源 24~27（跨 25s 接缝）
           期望：时间线 14~17（完整一句）
           实得：时间线 15~17（**前半句 14~15 被砍掉了**）

       而且 `cue 24.5~25.5`（接缝两侧各半）会被**整条丢弃** ——
       虽然它在时间线上是完整连续的。

       **片段在源视频上相邻时，时间线上也相邻，接缝处不该断开。**
       这在真实场景里会出现：主播连续讲了有价值的一段，
       AI 把它切成 2~3 条相邻候选，用户全选上。

    ## 计数语义（注意）

    `dropped` = **完全没产出任何字幕**的输入 cue 数。

    一条 cue 若跨越**多个不相邻**的片段，可能产出**多条**（文本相同）。
    所以 `len(out) + dropped` **可能大于**输入数 —— 正常输入下相等。
    （不相邻的情况需要 cue 长过中间整个片段，现实中极罕见。）

    返回 (映射后的 cues, 被丢弃的条数)。
    """
    # 每个片段在时间线上的起点 = 前面所有片段的时长之和
    offsets: list[float] = []
    acc = 0.0
    for (hs, he) in highlights:
        offsets.append(acc)
        acc += max(0.0, he - hs)
    total = acc

    out: list[Cue] = []
    dropped = 0

    for c in cues:
        dur = max(1e-6, c.src_end - c.src_start)

        # ── ① 分段映射 ──
        pieces: list[tuple[float, float]] = []
        for i, (hs, he) in enumerate(highlights):
            a = max(c.src_start, hs)
            b = min(c.src_end, he)
            if b - a <= 0:
                continue
            off = offsets[i]
            pieces.append((a - hs + off, b - hs + off))

        if not pieces:
            dropped += 1          # 落在所有片段之外
            continue

        # ── ② 时间线上连续/重叠的段合并（相邻片段在此合为一条）──
        pieces.sort()
        merged: list[list[float]] = [[pieces[0][0], pieces[0][1]]]
        for s0, e0 in pieces[1:]:
            if s0 <= merged[-1][1] + 1e-6:
                merged[-1][1] = max(merged[-1][1], e0)
            else:
                merged.append([s0, e0])

        # ── ③ 保留判据：保留总时长占 cue 总时长的比例 ──
        kept = sum(e - s for s, e in merged)
        if kept / dur < _MIN_KEEP_RATIO:
            dropped += 1
            continue

        made = False
        for s0, e0 in merged:
            if e0 - s0 < _MIN_CUE_VISIBLE:
                continue
            out.append(Cue(
                start=s0, end=e0,
                main=c.main, sub=c.sub,
                # ⚠️ 保留源时间轴位置 —— 验收时要靠它一秒定位回原片核对
                src_start=c.src_start, src_end=c.src_end))
            made = True
        if not made:
            dropped += 1

    # limit 传时间线总时长：越界字幕会被夹回来
    return tidy(out, limit=total), dropped


def tidy(cues: list[Cue], *, limit: float | None = None,
         min_duration: float = 0.7, gap: float = 0.04) -> list[Cue]:
    """收尾整理：排序 → 消除重叠 → 保证最短可见时长 → 夹到边界。

    不做这一步会出现两种情况，且都不会报错：
      1. 相邻字幕重叠 → 画面上下两条字幕同时出现
      2. 极短字幕（0.1s）→ 一闪而过，用户以为漏了
    """
    items = sorted((c for c in cues if not c.is_empty()),
                   key=lambda c: (c.start, c.end))
    if limit is not None:
        items = [c for c in items if c.start < limit]

    for i, c in enumerate(items):
        c.start = max(0.0, c.start)
        c.end = max(c.start, c.end)
        if limit is not None:
            c.start = min(c.start, limit)
            c.end = min(c.end, limit)

        # 最短可见时长：向后延，但不侵入下一条
        if c.end - c.start < min_duration:
            cap = items[i + 1].start - gap if i + 1 < len(items) else (
                limit if limit is not None else c.start + min_duration)
            c.end = min(max(c.end, c.start + min_duration), cap)
            if c.end <= c.start:
                c.end = c.start + min(min_duration, 0.1)

        # 与下一条保持间隔
        if i + 1 < len(items) and c.end > items[i + 1].start - gap:
            c.end = max(c.start, items[i + 1].start - gap)

    return [c for c in items if c.end > c.start]


# ─────────────────────────────────────────────────────────────
# 输出
# ─────────────────────────────────────────────────────────────
def _srt_time(seconds: float) -> str:
    """SRT 时间码 `HH:MM:SS,mmm`（**逗号**分隔毫秒，不是点）。"""
    if seconds < 0:
        seconds = 0.0
    ms_total = int(round(seconds * 1000))
    h, rem = divmod(ms_total, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: Sequence[Cue], *, bilingual: bool = True) -> str:
    """生成 SRT 文本。

    ⚠️ 传入的 cues 必须**已经过 `remap_to_clip()`** —— 本函数不做映射，
       以便调用方明确知道自己处在哪条时间轴上。
    """
    lines: list[str] = []
    for i, c in enumerate(cues, 1):
        body = c.main.strip()
        if bilingual and c.sub.strip():
            body = f"{body}\n{c.sub.strip()}" if body else c.sub.strip()
        if not body:
            continue
        lines.append(str(i))
        lines.append(f"{_srt_time(c.start)} --> {_srt_time(c.end)}")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def write_srt(cues: Sequence[Cue], path: str | Path, *,
              bilingual: bool = True) -> Path:
    """写 SRT 文件。

    **编码必须是 UTF-8 无 BOM** —— 带 BOM 时部分播放器/软件会把
    首个时间码前的 BOM 当正文，导致第一条字幕解析异常。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(to_srt(cues, bilingual=bilingual), encoding="utf-8")
    return p


def write_ass(cues: Sequence[Cue], path: str | Path,
              preset: subtitle_style.SubtitlePreset, *,
              width: int, height: int, title: str = "SliceQ") -> Path:
    """写 ASS 文件（烧录用）。

    ⚠️ width/height 必须与**成片实际尺寸**一致，否则字号缩放会走样。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [(c.start, c.end, c.main, c.sub) for c in cues]
    p.write_text(preset.to_ass(payload, width=width, height=height,
                               title=title), encoding="utf-8")
    return p


# ─────────────────────────────────────────────────────────────
# 全流程
# ─────────────────────────────────────────────────────────────
def build_for_clip(segments: Iterable, clip_start: float, clip_end: float,
                   *, out_dir: str | Path,
                   width: int, height: int,
                   preset: subtitle_style.SubtitlePreset | None = None,
                   transport=None, use_llm: bool = True,
                   bilingual: bool = False,
                   translate_transport=None,
                   target_lang: str = "en",
                   translate_reflection: bool = True,
                   stem: str = "clip",
                   progress: ProgressCb | None = None
                   ) -> dict:
    """为**一条**候选段生成字幕（ASS + SRT）。

    这是 editor.py 调用的入口 —— 把「断句 → 重映射 → （翻译）→ 写文件」串起来，
    确保两条时间轴的处理只在本函数里发生一次。

    返回 dict：ass / srt 路径、条数、丢弃数、翻译统计。

    ⚠️ **两个 transport 参数不是一回事，别传混**：
       `transport`            —— 断句用（LLM 语义切分）
       `translate_transport`  —— 翻译用（填 `cue.sub`）
       两者可以相同，但语义独立：断句可以关掉（`use_llm=False`）而翻译照做，
       反之亦然。踩过：只传了 `transport` 却开了 `bilingual=True`，
       结果一句译文都没有，看起来像"双语 ASS 没生成 Sub 行"。

    ⚠️ **翻译发生在 `remap_to_clip` 之后**：只翻成片里真正会出现的字幕。
       在映射前翻会把片段边界外、最终被丢掉的条目也翻一遍 —— 白花钱。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    preset = preset or subtitle_style.builtin_default()

    all_cues, stats = split_segments(segments, transport=transport,
                                     use_llm=use_llm, progress=progress)
    # ★ 唯一的映射调用点
    cues, dropped = remap_to_clip(all_cues, clip_start, clip_end)

    # ── 翻译（可选）────────────────────────────────────────
    trans: dict = {}
    if bilingual and cues:
        if translate_transport is None:
            # 说了要双语却翻不了 —— 必须说出来，不能悄悄退成单语
            trans = {
                "translate_notes": [
                    "未配置 API key，无法生成译文，本条按单语字幕导出。"],
                "translated": 0,
            }
        else:
            try:
                from . import translate as _tr
                tres = _tr.apply_to_cues(
                    cues, transport=translate_transport,
                    target_lang=target_lang, reflection=translate_reflection,
                    progress=progress)
                trans = {
                    "translated": len(cues) - tres.failed,
                    "translate_failed": tres.failed,
                    "translate_cost": round(tres.cost_cny, 6),
                    "translate_source": tres.source,
                    "translate_notes": list(tres.notes),
                }
                # 失败条数的说明由 translate_lines 自己写进 notes（含原因），
                # 这里不再重复加一条，免得界面上出现两遍同一句话。
            except Exception as exc:             # noqa: BLE001
                trans = {"translate_notes": [
                    f"翻译失败（已按单语导出）：{type(exc).__name__}: {exc}"]}

    ass_path = write_ass(cues, out / f"{stem}.ass", preset,
                         width=width, height=height, title=stem)
    srt_path = write_srt(cues, out / f"{stem}.srt", bilingual=bilingual)

    return {
        "ass": str(ass_path),
        "srt": str(srt_path),
        "cue_count": len(cues),
        "dropped_at_boundary": dropped,
        "clip_start": clip_start,
        "clip_end": clip_end,
        **stats,
        **trans,
    }
