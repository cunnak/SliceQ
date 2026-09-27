# -*- coding: utf-8 -*-
"""粗筛级 —— 从全片里找出"可能值得细看"的候选段（TECH-DESIGN §2.1）。

═══════════════════════════════════════════════════════════════
为什么粗筛必须便宜
═══════════════════════════════════════════════════════════════
全片喂模型不可接受：3 小时 = 10800 秒 × 228 token/秒 ≈ 2.46M token
≈ ¥1.97 —— **比精析还贵**，整个两级设计就失去意义了。

所以粗筛只用两种**廉价信号**：

  ① 文本通道 —— 转录文本（转录本身是本地免费的）
     3 小时转录约 4 万 token，成本 ¥0.03~0.10
  ② 抽帧通道 —— **拼贴图**（contact sheet），不是逐帧送
     每窗一张图（16 帧拼成），图片 token 远低于视频 token

两路信号合起来，3 小时录像的粗筛成本约 ¥0.08~0.15。

═══════════════════════════════════════════════════════════════
时间基准（最容易错的地方）
═══════════════════════════════════════════════════════════════
模型只看得到"窗口内"的内容，但候选段时间戳必须是**整片时间轴**的。
所以提示词里必须显式给出窗口起点，且解析回来的时间要再校验一次
（落在窗口外的一律丢弃）。

漏掉这个校验不会报错，只会让候选清单整体偏移 —— 属于
"每个环节都成功、端到端结果是错的"那类缺陷。
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

from . import asr, concurrency, config, ffmpeg_tools, prompts
from .analyzer import AnalyzeResult, Transport, Usage

ProgressCb = Callable[[float, str], None]
CancelCb = Callable[[], bool]


class ScreeningError(Exception):
    """粗筛失败，message 直接给用户看。"""


# ─────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────
@dataclass
class Candidate:
    """一个候选时间段（时间戳是整片时间轴的秒数）。"""
    start: float
    end: float
    hint: str = ""
    confidence: int = 0
    source: str = ""        # "text" / "frame" —— 哪个通道提出的

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def as_dict(self) -> dict:
        return {"start": round(self.start, 2), "end": round(self.end, 2),
                "hint": self.hint, "confidence": self.confidence,
                "source": self.source}


@dataclass
class ScreeningResult:
    candidates: list[Candidate] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    windows_total: int = 0
    windows_done: int = 0
    # ⚠️ 必须单独计数。只靠 `degraded` 里的文字无法区分
    #    「全部窗口都失败了」和「个别窗口失败」，而这两者对用户
    #    是完全不同的处境：前者是"没分析成功"，后者是"结果可能不完整"。
    windows_failed: int = 0
    degraded: list[str] = field(default_factory=list)
    # 硬阀裁掉的总秒数（用于告知用户"因为上限，有内容没进候选"）
    trimmed_seconds: float = 0.0


# ─────────────────────────────────────────────────────────────
# ① 转录分窗
# ─────────────────────────────────────────────────────────────
def chunk_segments(segments: Iterable[asr.Segment],
                   window_sec: float) -> list[tuple[float, float, str]]:
    """把转录分段按时间窗归并，返回 [(起, 止, 文本), ...]。

    ⚠️ 窗口范围是**等分**的（0~600、600~1200…），不按内容切 ——
       因为要和抽帧窗口对齐。两者错开会让提示词里的时间基准说不清楚。
    """
    segs = sorted(segments, key=lambda s: s.start_ms)
    if not segs:
        return []

    buckets: dict[int, list[asr.Segment]] = {}
    for s in segs:
        idx = int(s.start_ms / 1000 / window_sec)
        buckets.setdefault(idx, []).append(s)

    out: list[tuple[float, float, str]] = []
    for idx in sorted(buckets):
        text = "".join(x.text for x in buckets[idx])
        out.append((idx * window_sec, (idx + 1) * window_sec, text))
    return out


def all_windows(duration: float, window_sec: float) -> list[tuple[float, float]]:
    """全片的等分窗口（即使某个窗口没有转录文本，也要过一遍 —— 画面类高光可能没有语音）。"""
    if duration <= 0:
        return []
    out: list[tuple[float, float]] = []
    t = 0.0
    while t < duration:
        out.append((t, min(t + window_sec, duration)))
        t += window_sec
    return out


# ─────────────────────────────────────────────────────────────
# ② 抽帧拼贴（把 N 帧拼成一张图，而不是逐帧送）
# ─────────────────────────────────────────────────────────────
def build_contact_sheet(video: str | Path, out: str | Path,
                        start: float, end: float,
                        cols: int = 4, rows: int = 4,
                        tile_width: int = 320,
                        should_cancel: CancelCb | None = None) -> Path | None:
    """把 [start, end) 区间均匀采样的 cols*rows 帧，拼成一张图。

    为什么拼贴：逐帧送模型，3 小时要 360~1080 次图片输入；
    拼贴后同样覆盖全片只需 20 张左右的图。**这是粗筛能便宜下来的关键。**
    """
    ff = ffmpeg_tools.find_ffmpeg()
    if not ff:
        raise ScreeningError("未找到 FFmpeg")
    n = cols * rows
    dur = end - start
    if dur <= 0.1:
        return None

    o = Path(out)
    o.parent.mkdir(parents=True, exist_ok=True)
    # fps = 需要的帧数 / 时长；tile 收满一屏即输出一帧，用 -frames:v 1 只取第一张
    vf = (f"fps={n / dur:.6f},scale={tile_width}:-2,"
          f"tile={cols}x{rows}:padding=2:color=black")
    cmd = [str(ff), "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
           "-i", str(video), "-vf", vf, "-frames:v", "1", str(o)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or not o.exists() or o.stat().st_size < 512:
        return None
    return o


# ─────────────────────────────────────────────────────────────
# ③ 解析模型返回
# ─────────────────────────────────────────────────────────────
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_candidates(text: str, window_start: float, window_end: float,
                     source: str) -> list[Candidate]:
    """解析模型返回的候选 JSON。

    ⚠️ 两道防线，缺一不可：
       1. 模型常把 JSON 包在 markdown 代码块里 —— 先剥壳
       2. **校验时间戳落在窗口内** —— 越界的丢弃。
          这是防"模型算错基准"的唯一手段，漏了会让候选清单整体偏移。
    """
    if not text:
        return []

    raw = text.strip()
    m = _JSON_BLOCK.search(raw)
    if m:
        raw = m.group(1).strip()
    # 兜底：截取第一个 '[' 到最后一个 ']'
    if not raw.startswith("["):
        i, j = raw.find("["), raw.rfind("]")
        if i >= 0 and j > i:
            raw = raw[i:j + 1]

    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []

    out: list[Candidate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            st = float(item.get("start"))
            en = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if en <= st:
            continue
        # 越界校验：允许 1 秒容差
        if st < window_start - 1 or en > window_end + 1:
            continue
        try:
            conf = int(float(item.get("confidence", 0)))
        except (TypeError, ValueError):
            conf = 0
        out.append(Candidate(
            start=max(st, 0.0), end=en,
            hint=str(item.get("hint") or "")[:60],
            confidence=max(0, min(100, conf)), source=source))
    return out


# ─────────────────────────────────────────────────────────────
# ④ 合并与硬阀
# ─────────────────────────────────────────────────────────────
def merge_candidates(cands: Iterable[Candidate],
                     gap: float = 8.0) -> list[Candidate]:
    """合并重叠或邻近的候选段。

    gap=8s：中间隔不到 8 秒的两个候选，多半是同一件事被切成了两半。
    合并时保留更高的 confidence 与更长的 hint。
    """
    items = sorted((c for c in cands if c.duration > 0),
                   key=lambda c: (c.start, -c.confidence))
    if not items:
        return []

    merged: list[Candidate] = [replace(items[0])]
    for c in items[1:]:
        last = merged[-1]
        if c.start <= last.end + gap:
            last.end = max(last.end, c.end)
            if c.confidence > last.confidence:
                last.confidence = c.confidence
                last.hint = c.hint or last.hint
            if c.source and c.source not in last.source:
                last.source = "+".join(sorted(set(last.source.split("+")) | {c.source}))
        else:
            merged.append(replace(c))
    return merged


def enforce_ratio_budget(cands: list[Candidate], duration: float,
                         ratio: float = 0.20,
                         min_budget_sec: float = 120.0,
                         min_keep_sec: float = 20.0
                         ) -> tuple[list[Candidate], float]:
    """硬性成本阀：候选段总时长 ≤ max(全片 × ratio, min_budget_sec)。

    超限时**按 confidence 贪心保留**，而不是简单截断时间轴 ——
    后者会把所有高光都砍掉一半尾巴。

    ⚠️ 预算必须有**绝对下限**（默认 120 秒），不能纯按比例。
       纯比例的后果：12 秒素材 × 20% = 2.4 秒预算，
       任何合理候选（几秒到几十秒）都会被砍光，用户看到"0 个候选"。
       而硬阀的真正目的是**控制成本** —— 2 分钟候选段的精析成本约 ¥0.05，
       低于这个量级根本不值得裁。**比例是长视频的约束，下限是短视频的保护。**

    返回 (保留的候选, 被裁掉的秒数)。被裁掉的秒数要如实告知用户：
    "因为筛选强度上限，还有 N 分钟的内容没进候选" —— 让用户知道
    这是**他选的档位**造成的，而不是软件漏了东西。
    """
    if not cands or duration <= 0:
        return list(cands), 0.0
    budget = max(duration * ratio, min_budget_sec)
    total = sum(c.duration for c in cands)
    if total <= budget:
        return list(cands), 0.0

    ordered = sorted(cands, key=lambda c: (-c.confidence, c.start))
    kept: list[Candidate] = []
    acc = 0.0
    dropped = 0.0
    for c in ordered:
        room = budget - acc
        if c.duration <= room:
            kept.append(c)
            acc += c.duration
        elif room >= min_keep_sec:
            kept.append(replace(c, end=c.start + room))
            acc = budget
            dropped += c.duration - room
        else:
            dropped += c.duration
    kept.sort(key=lambda c: c.start)
    return kept, round(dropped, 2)


# ─────────────────────────────────────────────────────────────
# ⑤ 主流程
# ─────────────────────────────────────────────────────────────
def screen(video: str | Path,
           duration: float,
           segments: Iterable[asr.Segment],
           profile: str,
           transport: Transport,
           *,
           window_sec: float = 600.0,
           candidate_ratio: float = 0.20,
           extract_frames: bool = True,
           work_dir: str | Path | None = None,
           progress: ProgressCb | None = None,
           should_cancel: CancelCb | None = None,
           on_window_done: Callable[[int, list[Candidate]], None] | None = None,
           skip_windows: set[int] | None = None,
           workers: int = 1
           ) -> ScreeningResult:
    """跑完粗筛，返回候选段列表。

    逐窗口处理：每窗一次模型调用（文本 + 一张拼贴图），
    每窗完成即回调 —— 调用方可以据此落库，实现断点续跑。

    ⚠️ `skip_windows` 是断点续跑真正省钱的开关。
       只靠"不重复写缓存"是不够的 —— 那只是不重复落盘，
       **模型调用照样发生、钱照样花**。必须在这里跳过。

    `workers` > 1 时并发跑窗口。并发下的三条保证（见 concurrency.py）：
      · 成本累计 / 落库都在**调用线程**做（在工作线程里只取数据）
      · 进度按**完成数**报，不按窗口序号（并发完成顺序本来就不固定）
      · 取消 = 不再提交新窗口，跑着的让它跑完
    """
    video = Path(video)
    work = Path(work_dir) if work_dir else (config.APP_ROOT / "work" / video.stem)
    frames_dir = work / "frames"

    seg_list = list(segments)
    by_window = {int(a / window_sec): t for a, _, t in
                 chunk_segments(seg_list, window_sec)}
    windows = all_windows(duration, window_sec)
    skipped = skip_windows or set()

    result = ScreeningResult(windows_total=len(windows))
    raw_cands: list[Candidate] = []
    total = len(windows)

    # ── 单窗口处理（纯函数：只取数据，**不碰共享状态**）──────
    # 这一段在串行时跑在调用线程、并发时跑在工作线程，行为必须一样，
    # 所以它不能去改 result / raw_cands / progress ——
    # 那些统统交给下面的 _finish（永远在调用线程）。
    def _do_window(i: int, ab: tuple[float, float]):
        a, b = ab
        text = by_window.get(i, "")
        sheet: Path | None = None
        if extract_frames:
            sheet = build_contact_sheet(
                video, frames_dir / f"sheet_{i:04d}.png", a, b,
                should_cancel=should_cancel)
        window_prompt = prompts.screen_prompt(
            profile, a, b, text, has_frames=bool(sheet))
        try:
            if sheet:
                # ⚠️ 粗筛一律 `thinking="off"`。
                # 实测同一输入：auto 模式 out=207（其中 reasoning=202），
                # off 模式 out=2 —— **输出 token 差 100 倍**。
                res = transport.analyze(
                    sheet, window_prompt, system=prompts.SCREEN_SYSTEM,
                    thinking="off", timeout=300)
            else:
                res = analyze_text_only(transport, window_prompt,
                                        system=prompts.SCREEN_SYSTEM)
        finally:
            # 拼贴图用完即删 —— 这些 PNG 可能上百 KB，攒几百张会撑爆工作目录。
            # 放在 finally 里：调用失败时也要清掉，否则失败几窗就攒一堆。
            if sheet and sheet.exists():
                sheet.unlink(missing_ok=True)
        found = parse_candidates(res.text, a, b, "frame" if sheet else "text")
        return found, res.usage, list(res.degraded)

    def _finish(i: int, payload) -> None:
        """★ 永远在调用线程执行 —— 所有共享状态变更都在这里。"""
        found, usage, degraded = payload
        result.usage = result.usage + usage
        result.degraded.extend(degraded)
        raw_cands.extend(found)
        result.windows_done += 1
        if on_window_done:
            on_window_done(i, found)
        if progress:
            progress(0.05 + 0.90 * (result.windows_done / max(total, 1)),
                     f"粗筛 {result.windows_done}/{total}")

    def _fail(i: int, exc: BaseException) -> None:
        # 单窗失败不阻塞全局 —— 记录后继续，最后可以重跑这一段。
        # 但**要计数**：全部失败与个别失败是两回事，调用方需要区分。
        result.windows_failed += 1
        result.degraded.append(f"窗口 {i + 1} 调用失败：{exc}")
        if on_window_done:
            (on_window_done)(i, [])

    todo = [(i, w) for i, w in enumerate(windows) if i not in skipped]

    # 保持"串行路径"与并发路径共用同一套回调，行为一致
    if workers and int(workers) > 1:
        concurrency.run_tasks(todo, _do_window, workers=workers,
                              should_cancel=should_cancel,
                              on_result=_finish, on_error=_fail)
    else:
        for i, w in todo:
            if should_cancel and should_cancel():
                raise ScreeningError("用户取消")
            if progress:
                progress(0.05 + 0.90 * (result.windows_done / max(total, 1)),
                         f"粗筛 {result.windows_done + 1}/{total}")
            try:
                _finish(i, _do_window(i, w))
            except Exception as exc:             # noqa: BLE001
                _fail(i, exc)

    merged = merge_candidates(raw_cands)
    kept, trimmed = enforce_ratio_budget(merged, duration, candidate_ratio)
    result.candidates = kept
    result.trimmed_seconds = trimmed

    if progress:
        progress(1.0, f"粗筛完成，候选 {len(kept)} 段")
    return result


def analyze_text_only(transport: Transport, prompt: str,
                      system: str | None = None) -> AnalyzeResult:
    """纯文本调用（粗筛在无画面可用时的降级路径）。

    用 1×1 的透明 PNG 占位而不是伪造一个"视频" —— 因为两条 transport
    的视频字段是必填的。1×1 图片的 token 开销可忽略。
    """
    import tempfile

    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
           b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
        tmp = Path(f.name)
    try:
        return transport.analyze(tmp, prompt, system=system, timeout=180)
    finally:
        tmp.unlink(missing_ok=True)
