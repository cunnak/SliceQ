# -*- coding: utf-8 -*-
"""片段标题与简介生成（阶段 4-B · F9）。

给每条候选生成 3 条不同角度的标题 + 1 段发布简介，一键复制。

设计要点：

1. **素材只给"这个片段说过的话"**，不把整片转录丢给模型 ——
   否则模型会拿别处的信息来写这个片段的标题（而且看不出来）。
2. 标题要求"三条不同角度"，实测如果只说"写三个标题"，
   模型会给出三句几乎同义的话，等于只给了一个选项。
3. 成本极低（实测 20 条字幕一次翻译 ¥0.0008 量级），
   所以不做缓存之外的成本控制 —— 但**逐条计费仍要记进流水**，
   保持与 pipeline 的成本可见性一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from . import prompts

ProgressCb = Callable[[float, str], None]
CancelCb = Callable[[], bool]

TITLE_COUNT = 3


@dataclass
class CopyResult:
    clip_id: int = 0
    titles: list[str] = field(default_factory=list)
    description: str = ""
    usage: object | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def cost_cny(self) -> float:
        return getattr(self.usage, "cost_cny", 0.0) or 0.0

    def to_dict(self) -> dict:
        return {"clip_id": self.clip_id, "titles": list(self.titles),
                "description": self.description,
                "cost_cny": round(self.cost_cny, 6),
                "notes": list(self.notes)}


# ─────────────────────────────────────────────────────────────
# 素材准备
# ─────────────────────────────────────────────────────────────
def transcript_of_clip(segments: Iterable, start: float, end: float,
                       *, limit: int = 2000) -> str:
    """取某段时间范围内的转录文本（毫秒时间戳与秒混用都能吃）。

    片段边界外的话不取 —— 标题要贴着这个片段写。
    """
    s_ms, e_ms = start * 1000, end * 1000
    parts: list[str] = []
    for seg in segments or []:
        a = float(getattr(seg, "start_ms", 0) or 0)
        b = float(getattr(seg, "end_ms", 0) or 0)
        # 与片段区间有交集就算（不做严格包含：片段边界常切在句子中间）
        if b < s_ms or a > e_ms:
            continue
        text = str(getattr(seg, "text", "") or "").strip()
        if text:
            parts.append(text)
    joined = "".join(parts)
    return joined[:limit]


# ─────────────────────────────────────────────────────────────
# 生成
# ─────────────────────────────────────────────────────────────
def _ask(transport, prompt: str, system: str, *, max_tokens: int = 1200,
         timeout: int = 90) -> tuple[str, object]:
    res = transport.analyze_text(prompt, system=system, max_tokens=max_tokens,
                                 thinking="off", timeout=timeout)
    return (res.text or "").strip(), res.usage


def write_titles(summary: str, transcript: str, transport, *,
                 count: int = TITLE_COUNT,
                 timeout: int = 90) -> tuple[list[str], object]:
    """生成 count 条标题。返回 (标题列表, usage)。"""
    from .translate import parse_json_array

    text, usage = _ask(transport,
                       prompts.copy_title_prompt(summary, transcript, count),
                       prompts.COPY_TITLE_SYSTEM, timeout=timeout)
    titles = parse_json_array(text) or []
    # 模型偶尔给纯文本行（没包 JSON），兜底按行取
    if not titles:
        titles = [ln.strip(" -·*0123456789.") for ln in text.splitlines()
                  if ln.strip()]
    titles = [t for t in titles if t][:count]
    return titles, usage


def write_description(summary: str, transcript: str, transport, *,
                      timeout: int = 90) -> tuple[str, object]:
    """生成一段简介。"""
    text, usage = _ask(transport,
                       prompts.copy_desc_prompt(summary, transcript),
                       prompts.COPY_DESC_SYSTEM, timeout=timeout)
    # 去掉可能的引号包裹
    text = text.strip().strip('"').strip("“”").strip()
    # 单段：把换行压成空格（简介不该是多行）
    return " ".join(text.split()), usage


def write_for_clip(clip: dict, segments: Iterable, *, transport,
                   with_description: bool = True,
                   count: int = TITLE_COUNT,
                   progress: ProgressCb | None = None,
                   should_cancel: CancelCb | None = None) -> CopyResult:
    """给一条候选生成标题与简介。

    单条失败**不影响其它条**（调用方循环时自己 try），
    这一条失败就返回空结果 + notes，不抛。
    """
    start = float(clip.get("start") or 0)
    end = float(clip.get("end") or 0)
    summary = str(clip.get("summary") or clip.get("hint") or
                  clip.get("title_hint") or "")
    text = transcript_of_clip(segments, start, end)

    res = CopyResult(clip_id=int(clip.get("id") or 0))
    if not text.strip() and not summary.strip():
        res.notes.append("这个片段既没有概要也没有转录文本，无法生成文案。")
        return res

    try:
        if progress:
            progress(0.1, "生成标题…")
        titles, usage = write_titles(summary, text, transport, count=count)
        res.titles = titles
        res.usage = usage
        if len(titles) < count:
            res.notes.append(
                f"只生成出 {len(titles)} 条标题（期望 {count} 条）。")
    except Exception as exc:                     # noqa: BLE001
        res.notes.append(f"标题生成失败：{type(exc).__name__}: {exc}")

    if should_cancel and should_cancel():
        return res

    if with_description:
        try:
            if progress:
                progress(0.6, "生成简介…")
            desc, usage2 = write_description(summary, text, transport)
            res.description = desc
            res.usage = usage2 if res.usage is None else res.usage + usage2
        except Exception as exc:                 # noqa: BLE001
            res.notes.append(f"简介生成失败：{type(exc).__name__}: {exc}")

    if progress:
        progress(1.0, "文案完成")
    return res


def write_for_clips(clips: Sequence[dict], segments: Iterable, *,
                    transport, with_description: bool = True,
                    count: int = TITLE_COUNT,
                    progress: ProgressCb | None = None,
                    should_cancel: CancelCb | None = None) -> list[CopyResult]:
    """批量生成。单条失败不中断整批（与 editor.export_clips 同样的取舍）。"""
    segs = list(segments or [])
    out: list[CopyResult] = []
    total = max(1, len(clips))
    for i, clip in enumerate(clips):
        if should_cancel and should_cancel():
            break
        if progress:
            progress(i / total,
                     f"文案 {i + 1}/{total}："
                     f"{str(clip.get('title_hint') or clip.get('summary') or '')[:16]}")

        def _inner(ratio: float, msg: str, _i: int = i) -> None:
            if progress:
                progress((_i + ratio) / total, msg)

        try:
            out.append(write_for_clip(clip, segs, transport=transport,
                                      with_description=with_description,
                                      count=count, progress=_inner,
                                      should_cancel=should_cancel))
        except Exception as exc:                 # noqa: BLE001
            r = CopyResult(clip_id=int(clip.get("id") or 0))
            r.notes.append(f"文案生成失败：{type(exc).__name__}: {exc}")
            out.append(r)
    if progress:
        progress(1.0, f"文案完成（{len(out)} 条）")
    return out


def to_text(results: Sequence[CopyResult]) -> str:
    """把结果拼成可一键复制的纯文本。"""
    blocks: list[str] = []
    for i, r in enumerate(results, 1):
        lines = [f"【片段 {i}】"]
        for j, t in enumerate(r.titles, 1):
            lines.append(f"  标题{j}：{t}")
        if r.description:
            lines.append(f"  简介：{r.description}")
        for n in r.notes:
            lines.append(f"  ⚠️ {n}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
