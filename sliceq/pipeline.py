# -*- coding: utf-8 -*-
"""流水线编排 —— 转录 → 粗筛 →（确认点）→ 精析 → 落库。

═══════════════════════════════════════════════════════════════
成本可见性的落点（TECH-DESIGN §2.5）
═══════════════════════════════════════════════════════════════
本模块是"花费上限"这套设计真正落地的地方。四层里有两层在这里：

  ① 启动时给**上限**（`estimate_total_upper_bound`）—— 给用户最坏预期
  ② **唯一的确认点**：粗筛完成后、精析开始前。
     精析占成本 72%，而它的精确报价只有粗筛完成后才算得出
     （要知道候选段总时长）。这是唯一同时满足"信息最充分"和"钱还没花"的时刻。
  ③ 跑中实时累计 + 超硬上限暂停（`ConfirmRequest(kind="over_budget")`）
  ④ 完成后实际 vs 预估（调用方用 `RunResult` 里的字段展示）

⚠️ 实际花费按 API 返回的 `usage` 算，是估算而非账单。
   BYOK 模式下真实扣费在阿里云后台 —— **界面必须注明"以百炼账单为准"**。

═══════════════════════════════════════════════════════════════
断点续跑
═══════════════════════════════════════════════════════════════
转录：asr.py 内部按分片缓存（`chunk_*.wav` + `*.jsonl`）
粗筛：每窗口完成即追加到 `screen.jsonl`
精析：每段完成即写 `refine/clip_XXXX.json`

重跑时逐层读缓存跳过已完成的单元。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from . import (analyzer, asr, concurrency, config, ffmpeg_tools,
               prompts, screening, store)
from .analyzer import Transport, Usage
from .screening import Candidate

log = logging.getLogger(__name__)


class PipelineError(Exception):
    """message 直接给用户看。"""


class Cancelled(Exception):
    """用户主动取消。"""


# ─────────────────────────────────────────────────────────────
# 参数与结果
# ─────────────────────────────────────────────────────────────
@dataclass
class Options:
    profile: str = ""
    user_prompt: str = ""
    candidate_ratio: float = 0.20        # 筛选强度：候选段总时长占全片比例
    window_sec: float = 600.0            # 粗筛窗口
    hard_limit_cny: float = 2.0          # 硬上限，超了暂停询问
    refine_thinking: str = "auto"
    refine_max_tokens: int = 900
    refine_pad_sec: float = 30.0         # 精析副本前后各扩多少秒
    max_candidates: int = 80             # 精析段数上限（防异常素材）
    # 粗筛/精析的并发路数（1 = 串行）。默认 1 —— 并发是显式加法，
    # 不能让老用户重跑时行为凭空变化；界面里可调到 2~4。
    # ⚠️ 提高并发会同时发出多个请求，**更容易撞服务端限流**。
    concurrency: int = 1


@dataclass
class Progress:
    stage: str            # transcribe / screen / refine / done
    ratio: float          # 0~1
    message: str
    spent_cny: float      # 到目前为止的实际花费（估算）
    est_total_cny: float  # 本轮上限（不含已花）
    candidates: int = 0   # 精析阶段：已处理段数 / 总段数放在 message 里


@dataclass
class ConfirmRequest:
    """需要用户点头的时刻。两种：进入精析前、超硬上限时。"""
    kind: str                    # "before_refine" | "over_budget"
    message: str
    candidate_count: int = 0
    candidate_seconds: float = 0.0
    refine_estimate: float = 0.0
    spent_cny: float = 0.0
    hard_limit_cny: float = 0.0


@dataclass
class RunResult:
    task_id: int
    candidates: list[Candidate] = field(default_factory=list)
    clips: list[dict] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    spent_cny: float = 0.0
    estimated_upper_bound: float = 0.0
    trimmed_seconds: float = 0.0
    degraded: list[str] = field(default_factory=list)
    finished: bool = False       # False 表示在确认点被跳过或被取消


ProgressCb = Callable[[Progress], None]
ConfirmCb = Callable[[ConfirmRequest], bool]
CancelCb = Callable[[], bool]


# ─────────────────────────────────────────────────────────────
# 副本压制参数（按 transport 分档，TECH-DESIGN §2.2）
# ─────────────────────────────────────────────────────────────
# ── 分析副本的体积自适应（2026-09-28 事故后新增）──────────────
#
# 起因：精析副本按**固定码率**切，从不检查体积。而通道 B（兼容模式 HTTP）
# 必须把副本 base64 内联，上限 20M 字符（`analyzer.B64_SAFE_LIMIT`）。
# 实测事故：两个候选的副本分别 90.6MB / 46.3MB，base64 120.8M / 61.7M 字符
# —— 超限 6.3 倍 / 3.2 倍 ⇒ **两个候选都在发请求前被拒**，
# 用户看到的是"完成：0 个切片段"，而原因只写在一句用户看不到的提示里。
#
# 所以：切副本前先**估算体积**，超了就自动降档（先降码率、再降分辨率），
# 降到最低档还不行才缩短时长 —— 并且把降级事实如实带给用户。
#
# 档位按"高画质优先"排列：体积够用就不降，能不降就不降。
_ANALYSIS_LADDER: tuple[tuple[str, int, int], ...] = (
    # (缩放高度, 视频 kbps, 音频 kbps)
    ("720", 1000, 96),
    ("480", 500, 96),
    ("360", 280, 64),
    ("240", 150, 48),
)

# 缩到比这更短就没有分析价值了（模型看不到完整的一句话）。
MIN_ANALYSIS_SEC = 30.0

# 体积估算留的余量：宁可多降一档，也不要贴着上限走。
# （估算本身有误差：实际码率受画面复杂度影响，可能略高于设定值。）
_SIZE_SAFETY = 0.9


def est_mp4_bytes(duration_sec: float, v_kbps: int, a_kbps: int) -> int:
    """按码率估算 mp4 体积（字节）。故意略微**高估** —— 见 `_SIZE_SAFETY`。"""
    return int(duration_sec * (v_kbps + a_kbps) * 1000 / 8 * 1.03) + 4096


def plan_analysis_clip(duration_sec: float, max_bytes: int,
                       max_duration: float
                       ) -> tuple[str, int, int, float, str]:
    """挑一个能塞进 `max_bytes` 的编码档位。

    返回 `(缩放高度, 视频kbps, 音频kbps, 实际时长, 说明)`。
    「说明」为空表示无需降级；**非空必须转达给用户**（静默降级等于隐瞒）。

    `max_bytes <= 0` 表示不做体积约束（通道 A 本地直传时用）。
    `max_duration <= 0` 表示不做时长约束。
    """
    dur = max(0.1, duration_sec)
    notes: list[str] = []

    if max_duration > 0 and dur > max_duration + 0.5:
        notes.append(
            f"候选段 {duration_sec:.0f} 秒超过单次分析上限 "
            f"{max_duration:.0f} 秒，本次只分析了前 {max_duration:.0f} 秒")
        dur = max_duration

    if max_bytes <= 0:
        h, v, a = _ANALYSIS_LADDER[0]
        return h, v, a, dur, "；".join(notes)

    for h, v, a in _ANALYSIS_LADDER:
        if est_mp4_bytes(dur, v, a) <= max_bytes:
            return h, v, a, dur, "；".join(notes)

    # 最低档仍超 ⇒ 只能缩短时长（最后手段，必须告知）
    h, v, a = _ANALYSIS_LADDER[-1]
    per_sec = (v + a) * 1000 / 8 * 1.03
    shrunk = max(MIN_ANALYSIS_SEC, max_bytes / per_sec)
    if shrunk < dur:
        notes.append(
            f"副本已降到最低画质（{h}p/{v}kbps）仍超过体积上限，"
            f"时长缩到 {shrunk:.0f} 秒")
        dur = shrunk
    return h, v, a, dur, "；".join(notes)


def clip_size_cap(transport: Transport) -> int:
    """该通道单次能送出去的最大**原始**体积（已含安全余量）。

    返回 0 表示不限制（通道 A 走本地路径直传时用它表达"随便"）。
    """
    fn = getattr(transport, "max_video_bytes", None)
    if not callable(fn):
        return 0
    try:
        cap = int(fn())
    except Exception:                                       # noqa: BLE001
        return 0
    return int(cap * _SIZE_SAFETY) if cap > 0 else 0


def cut_clip(video: str | Path, out: str | Path, start: float, end: float,
             height: str = "720", bitrate: str = "1000k",
             audio_bitrate: str = "96k",
             should_cancel: CancelCb | None = None) -> Path:
    """切一段低码率分析副本（精析用）。"""
    ff = ffmpeg_tools.find_ffmpeg()
    if not ff:
        raise PipelineError("未找到 FFmpeg")
    o = Path(out)
    o.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.1, end - start)
    cmd = [str(ff), "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}",
           "-i", str(video),
           "-vf", f"scale=-2:{height}",
           "-c:v", "libx264", "-preset", "veryfast", "-b:v", bitrate,
           "-c:a", "aac", "-b:a", audio_bitrate,
           str(o)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"切副本超时（{start:.0f}s~{end:.0f}s）") from exc
    if r.returncode != 0 or not o.exists():
        raise PipelineError(f"切副本失败：{(r.stderr or '')[:200]}")
    return o


# ─────────────────────────────────────────────────────────────
# 缓存（断点续跑）
# ─────────────────────────────────────────────────────────────
def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────
# 精析单段
# ─────────────────────────────────────────────────────────────
_JSON_OBJ = None


def parse_refine(text: str) -> dict | None:
    """解析精析返回的单个 JSON 对象。"""
    import re
    if not text:
        return None
    raw = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if m:
        raw = m.group(1).strip()
    if not raw.startswith("{"):
        i, j = raw.find("{"), raw.rfind("}")
        if i >= 0 and j > i:
            raw = raw[i:j + 1]
    try:
        d = json.loads(raw)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def refine_one(video: str | Path, cand: Candidate, profile: str,
               transport: Transport, work: Path, idx: int,
               opts: Options, should_cancel: CancelCb | None = None
               ) -> tuple[dict | None, Usage, list[str]]:
    """精析一个候选段。返回 (结果字典, usage, 降级事件)。

    结果里的 start/end 已经**换算回整片时间轴** —— 这是必须的，
    因为模型看到的是副本，它给的秒数是相对副本的。
    """
    clip_start = max(0.0, cand.start - opts.refine_pad_sec)
    clip_end = cand.end + opts.refine_pad_sec

    clip_path = work / "refine" / f"clip_{idx:04d}.mp4"
    plan_notes: list[str] = []

    # ★ 副本要按**通道能送出的体积**来切，而不是按固定码率。
    #   通道上限（`clip_size_cap`）：通道 B 约 13.5MB、通道 A 约 94MB。
    cap = clip_size_cap(transport)
    # 时长上限与 screening 的候选上限对齐（候选 + 两侧留白）。
    max_dur = screening.MAX_CANDIDATE_SEC + 2 * opts.refine_pad_sec

    need_cut = True
    if clip_path.exists() and clip_path.stat().st_size > 1024:
        # ⚠️⚠️ 已存在的副本**不能无条件复用**。
        #   旧版本（≤v0.1.1）按固定码率切、从不看体积，产出的副本可能
        #   远超通道上限（实测 90.6MB / 46.3MB）。若无脑复用，
        #   **用户重跑多少次都还是同一个失败** —— 修复等于没修。
        #   所以：超过上限的旧副本必须重切。
        old = clip_path.stat().st_size
        if cap > 0 and old > cap:
            plan_notes.append(
                f"已有副本 {old / 1048576:.1f}MB 超过通道上限，已重新压制")
        else:
            need_cut = False

    if need_cut:
        height, v_kbps, a_kbps, keep, note = plan_analysis_clip(
            clip_end - clip_start, cap, max_dur)
        if note:
            plan_notes.append(note)
        cut_clip(video, clip_path, clip_start, clip_start + keep,
                 height=height, bitrate=f"{v_kbps}k",
                 audio_bitrate=f"{a_kbps}k",
                 should_cancel=should_cancel)
        # 副本规格必须进日志：体积超限是本项目最容易踩、又最难看见的坑
        # （它在发请求前就失败，网络层什么都看不到）。
        try:
            mb = clip_path.stat().st_size / 1048576
        except OSError:
            mb = 0.0
        log.info("候选 %s 分析副本：%.1f 秒 @ %sp/%skbps → %.1fMB"
                 "（通道上限 %.1fMB）%s",
                 idx + 1, keep, height, v_kbps, mb, cap / 1048576,
                 f"｜{note}" if note else "")

    cache = work / "refine" / f"clip_{idx:04d}.json"
    if cache.exists():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            # ⚠️ 缓存命中 = 本次**没有真实调用** = 本次消耗为 0。
            #
            # 曾经这里返回的是缓存里的历史 usage —— 那会让"重跑一次花费翻倍"，
            # 而且用户重跑几次就可能撞上硬上限，**明明一分钱没花**。
            # 这是"数字看起来合理、语义完全错"的典型，故记一笔。
            #
            # 代价：分两次续跑时，`spent_cny` 只反映本次运行的花费。
            # 界面若要显示"这条任务的累计花费"，应从 usage 日志累计，而不是这里。
            #
            # 但**本次**发生的副本重切仍要报（plan_notes）—— 那是这一轮
            # 真实做过的事，与"上次调用了什么"无关。
            return saved.get("result"), Usage(), plan_notes
        except Exception:
            pass

    prompt = prompts.refine_prompt(profile, clip_start, clip_end, cand.hint)
    res = transport.analyze(
        clip_path, prompt, system=prompts.REFINE_SYSTEM,
        max_tokens=opts.refine_max_tokens, thinking=opts.refine_thinking)

    parsed = parse_refine(res.text)
    result: dict | None = None
    if parsed:
        try:
            rel_start = float(parsed.get("start", 0))
            rel_end = float(parsed.get("end", clip_end - clip_start))
        except (TypeError, ValueError):
            rel_start, rel_end = 0.0, clip_end - clip_start
        # ⚠️ 相对副本 → 整片时间轴。漏了这一步，所有切片都会错位。
        result = {
            "start": round(clip_start + max(0.0, rel_start), 2),
            "end": round(clip_start + max(0.0, rel_end), 2),
            "kind": str(parsed.get("kind") or "")[:20],
            "score": int(float(parsed.get("score", 0) or 0)),
            "summary": str(parsed.get("summary") or "")[:80],
            "title": str(parsed.get("title") or "")[:40],
            "refined": True,
        }

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({
        "result": result,
        "usage": {"input_tokens": res.usage.input_tokens,
                  "output_tokens": res.usage.output_tokens,
                  "cache_hit_tokens": res.usage.cache_hit_tokens,
                  "reasoning_tokens": res.usage.reasoning_tokens},
        "degraded": plan_notes + list(res.degraded),
    }, ensure_ascii=False), encoding="utf-8")
    return result, res.usage, plan_notes + list(res.degraded)


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
# 阶段 → 任务状态。状态落库在流水线内部做，见 run() 的 docstring。
_STAGE_STATUS = {
    "transcribe": config.STATUS_TRANSCRIBING,
    "screen": config.STATUS_ROUGHING,
    "refine": config.STATUS_REFINING,
}


def _mark(task_id: int, status: str) -> None:
    """写任务状态。

    ⚠️ **落库失败不能影响分析** —— 状态只是记账，记不上不该让整条流水线挂掉。
       （与 `save_transcript_segs` 同样的取舍。）
    """
    try:
        store.set_task_status(task_id, status)
    except BaseException:                      # noqa: BLE001
        pass


def run(task_id: int,
        video: str | Path,
        opts: Options,
        transport: Transport,
        *,
        on_progress: ProgressCb | None = None,
        on_confirm: ConfirmCb | None = None,
        should_cancel: CancelCb | None = None,
        work_dir: str | Path | None = None,
        skip_transcribe: list[asr.Segment] | None = None) -> RunResult:
    """跑完整条流水线。

    `on_confirm` 为 None 时视为"总是自动继续"（用户关掉了确认点）。

    ── ⚠️ 关于任务状态：落库在这里，不在界面回调里（v1.17 修正）──

    原先 `store.set_task_status` 全写在 `clips_page` 的 UI 回调里。
    加上分析队列后这个安排暴露了问题：队列走 `run_analysis`，
    **不经过那些回调**，于是任务状态永远停在「已导入」——
    而且**不报任何错**，只能靠盯着表格才发现。

    状态是**任务的属性**，该由执行者维护，不该由显示者维护。
    放在这里，所有调用方（单任务 / 队列 / 将来的 CLI）行为一致。

    取消（`Cancelled`）**不写 ERROR** —— 用户按了取消然后看到"出错"，
    会以为程序坏了。
    """
    # ★ 提示必须落库（2026-09-28）。
    #   原先 degraded 只活在内存里、界面只显示个数，用户点不开、
    #   进程一退就没了 —— 那次"0 个切片段"就是这样：原因明明有
    #   （"分析副本过大：base64 约 120.8M 字符，超过安全上限 20M"），
    #   却谁也没看到，只能靠翻 work 目录反推。
    #   用一个外部列表当 sink，_run_pipeline 往里追加，
    #   这样**异常中断时也能拿到已有的提示**（那正是最需要提示的时刻）。
    hint_sink: list[str] = []
    try:
        result = _run_pipeline(
            task_id, video, opts, transport,
            on_progress=on_progress, on_confirm=on_confirm,
            should_cancel=should_cancel, work_dir=work_dir,
            skip_transcribe=skip_transcribe, _hint_sink=hint_sink)
        _save_hints(task_id, hint_sink)
        return result
    except Cancelled:
        # 用户主动取消 —— 标「已取消」而不是「出错」。
        # 混用会让用户以为自己把程序搞坏了。
        _mark(task_id, config.STATUS_CANCELLED)
        _save_hints(task_id, hint_sink)
        raise
    except BaseException as exc:               # noqa: BLE001
        _mark(task_id, config.STATUS_ERROR)
        hint_sink.append(f"分析中断：{type(exc).__name__}: {str(exc)[:300]}")
        _save_hints(task_id, hint_sink)
        raise


def _save_hints(task_id: int, hints: list[str]) -> None:
    """把提示落库。**绝不抛异常** —— 落库失败不该掩盖真正的错误。"""
    try:
        store.replace_run_hints(task_id, hints)
    except Exception:                                       # noqa: BLE001
        log.warning("提示落库失败（不影响分析本身）", exc_info=True)


def _run_pipeline(task_id: int,
                  video: str | Path,
                  opts: Options,
                  transport: Transport,
                  *,
                  on_progress: ProgressCb | None = None,
                  on_confirm: ConfirmCb | None = None,
                  should_cancel: CancelCb | None = None,
                  work_dir: str | Path | None = None,
                  skip_transcribe: list[asr.Segment] | None = None,
                  _hint_sink: list[str] | None = None) -> RunResult:
    """流水线主体。

    ⚠️ **不要直接调用它** —— 用 `run()`。直接调会绕过异常时的
       状态落库与**提示落库**（任务会永远停在中间状态，提示也没了）。

    `_hint_sink`：外部传入的提示收集器。传进来时，本函数产生的所有
    降级提示都会同步到它 —— 这样即使中途抛异常，调用方（`run()`）
    也能把已经产生的提示落库。不传则用本地列表。
    """
    video = Path(video)
    if not video.exists():
        raise PipelineError(f"文件不存在：{video}")

    task = store.get_task(task_id)
    if not task:
        raise PipelineError(f"任务不存在：{task_id}")

    duration = float(task.get("duration_sec") or 0.0)
    work = Path(work_dir) if work_dir else (config.APP_ROOT / "work" / video.stem)
    work.mkdir(parents=True, exist_ok=True)

    usage_total = Usage()
    # 提示收集器：外部传了就用它（异常时调用方还能落库），否则本地建一个。
    degraded: list[str] = _hint_sink if _hint_sink is not None else []
    _status_now = [""]                 # 已写库的状态（避免重复 UPDATE）
    _logged_stage = [""]               # 已记过日志的阶段（避免刷屏）

    def report(stage: str, ratio: float, msg: str, candidates: int = 0) -> None:
        # 阶段变化时把状态写库 —— 见 `run()` 的 docstring（v1.17 修正）
        st = _STAGE_STATUS.get(stage, "")
        if st and st != _status_now[0]:
            _status_now[0] = st
            _mark(task_id, st)
        # 阶段变化时也记一条日志。
        # 为什么只记阶段变化而不记每次进度：进度回调一秒能来好几条，
        # 全记会把日志冲成噪声 —— 而日志的用途恰恰是**事后翻查**
        # （2026-09-28 那次"0 个切片段"，分析全程零日志，
        #   只能靠翻 work 目录反推，代价是好几轮往返）。
        if st and st != _logged_stage[0]:
            _logged_stage[0] = st
            log.info("阶段 %s：%s（已花 ¥%.4f）", st, msg,
                     usage_total.cost_cny)
        if on_progress:
            on_progress(Progress(
                stage=stage, ratio=max(0.0, min(1.0, ratio)), message=msg,
                spent_cny=round(usage_total.cost_cny, 4),
                est_total_cny=estimates["total"], candidates=candidates))

    # ── ① 花费上限（给用户最坏预期）────────────────
    estimates = analyzer.estimate_total_upper_bound(duration, opts.candidate_ratio)
    log.info("流水线开始：任务 %s，素材 %s（%.1f 秒），"
             "候选预算 %.0f%%，并发 %s",
             task_id, video.name, duration, opts.candidate_ratio * 100,
             opts.concurrency)

    # ── ② 转录（本地，免费）────────────────────────
    if skip_transcribe is not None:
        segments = list(skip_transcribe)
    else:
        report("transcribe", 0.0, "提取音频")
        try:
            segments = asr.transcribe(
                video,
                work_dir=work,
                progress=lambda p, m: report("transcribe", p * 0.30, m),
                should_cancel=should_cancel)
        except asr.AsrCancelled as exc:
            raise Cancelled(str(exc)) from exc
        except asr.AsrError as exc:
            raise PipelineError(str(exc)) from exc

    # 转录分段落库。
    # ⚠️ 必须存 —— 导出阶段要用它生成字幕。只在 asr 的分片缓存里的话，
    #    导出侧拿不到，就得为了一次导出重新转录整条素材（3 小时要 20 分钟）。
    # 落库失败不影响本次分析（候选清单仍然有用），所以只记不抛。
    try:
        store.save_transcript_segs(task_id, segments)
    except BaseException:                          # noqa: BLE001
        pass

    # ── ③ 粗筛（文本 + 抽帧）──────────────────────
    report("screen", 0.30, "粗筛中")
    cache_path = work / "screen.jsonl"
    cached = {int(r["window"]): r["candidates"] for r in _load_jsonl(cache_path)
              if "window" in r}

    def on_window(i: int, cands: list[Candidate]) -> None:
        if i not in cached:
            _append_jsonl(cache_path, {"window": i,
                                       "candidates": [c.as_dict() for c in cands]})

    profile = opts.profile or opts.user_prompt or prompts.BUILTIN_TEMPLATES["通用"]

    # 已缓存的窗口直接跳过 —— 不重复调模型，这才是"断点续跑"省钱的实质
    if cached:
        report("screen", 0.32, f"复用 {len(cached)} 个已完成的粗筛窗口")

    try:
        scr = screening.screen(
            video, duration, segments, profile, transport,
            window_sec=opts.window_sec, candidate_ratio=opts.candidate_ratio,
            work_dir=work,
            progress=lambda p, m: report("screen", 0.30 + p * 0.20, m),
            should_cancel=should_cancel,
            on_window_done=on_window,
            workers=opts.concurrency,
            skip_windows=set(cached))
    except screening.ScreeningError as exc:
        raise Cancelled(str(exc)) from exc
    except analyzer.AnalyzerError as exc:
        raise PipelineError(str(exc)) from exc

    usage_total = usage_total + scr.usage
    degraded.extend(scr.degraded)

    # ★ 粗筛「全部窗口失败」绝不能伪装成「没找到候选」。
    #
    #   实测踩过（2026-09-27）：端点配成了需要 `dashscope` 包的通道，
    #   而那个包没装 → 唯一一个窗口就失败了 → 候选 0 段。
    #   但用户看到的是：
    #
    #     "没有找到符合条件的候选段。可以试试：① 把需求描述写得更具体；
    #      ② 调高「筛选强度」档位；③ 换一段素材验证。"
    #
    #   **三条建议全都在引导他改无关的东西**，而真实原因（缺少 dashscope 包）
    #   被埋在 degraded 列表里。用户会去改需求描述、换素材，白折腾一圈。
    #
    #   判据：`windows_failed > 0` 且 `windows_done == 0`
    #   —— 一个都没成功 = 没分析成，不是"分析完发现没有"。
    if scr.windows_failed and not scr.windows_done:
        first = scr.degraded[0] if scr.degraded else "原因未记录"
        raise PipelineError(
            f"粗筛的 {scr.windows_failed} 个窗口全部调用失败，"
            f"没有任何一个分析成功。\n"
            f"原始原因：{first}\n\n"
            # ⚠️ 这里**不要**猜病因。
            #    上一版写的是"这不是素材的问题 —— 多半是 API Key、端点地址或网络"，
            #    当天就被实测打脸：真实原因是**内容审核拦截**（口语素材里的
            #    激烈用词），它既不是 Key/端点/网络，也**恰恰和素材有关**。
            #    猜测写在报错里，就等于把误导固化进产品。
            f"请以上面的「原始原因」为准排查。")

    # 把缓存里的候选也合进来（重跑时 screen 会跳过已缓存窗口，
    # 但那些窗口的候选必须补回结果里，否则重跑会"越跑越少"）
    all_cands = list(scr.candidates)
    if cached:
        extra = [Candidate(**c) for cs in cached.values() for c in cs]
        all_cands = screening.merge_candidates(all_cands + extra)
        all_cands, _ = screening.enforce_ratio_budget(
            all_cands, duration, opts.candidate_ratio)

    candidates = all_cands[:opts.max_candidates]
    cand_seconds = sum(c.duration for c in candidates)
    # 候选规模是排查"精析为什么慢 / 为什么失败"的第一手信息。
    # 特别是**最长候选** —— 副本体积与它成正比，而体积超限是最隐蔽的失败。
    log.info("候选就绪：%s 段、共 %.0f 秒（合并裁剪前 %s 段）",
             len(candidates), cand_seconds, len(all_cands))
    if candidates:
        _longest = max(candidates, key=lambda c: c.duration)
        log.info("最长候选 %.1f 秒：%s", _longest.duration,
                 (_longest.hint or "")[:40])
    if not candidates:
        # 空结果不算失败，但必须告诉用户下一步能做什么，
        # 否则他只会看到"0 个切片"然后以为软件坏了。
        if scr.windows_failed:
            # ⚠️ 与"确实没有"要分开说：这里有一部分窗口根本没分析成功，
            #    漏掉的内容很可能就在失败的窗口里。给"改需求描述"这种
            #    建议是错的 —— 应该建议重跑。
            degraded.append(
                f"没有找到候选段，但有 {scr.windows_failed} 个窗口"
                f"（共 {scr.windows_total} 个）没能分析成功 —— "
                f"漏掉的内容可能就在其中。建议先重跑一次；"
                f"若反复失败，请看上面的原始原因。")
        else:
            degraded.append(
                "没有找到符合条件的候选段。可以试试：① 把需求描述写得更具体；"
                "② 调高「筛选强度」档位；③ 换一段素材验证。")

    # ── ④ 确认点（唯一一次停顿）───────────────────
    report("refine", 0.50, f"找到 {len(candidates)} 个候选段", len(candidates))
    if on_confirm and candidates:
        req = ConfirmRequest(
            kind="before_refine",
            message=(f"找到 {len(candidates)} 个候选段，共 "
                     f"{cand_seconds / 60:.1f} 分钟。"),
            candidate_count=len(candidates),
            candidate_seconds=cand_seconds,
            refine_estimate=analyzer.estimate_refine_cost(cand_seconds),
            spent_cny=round(usage_total.cost_cny, 4),
            hard_limit_cny=opts.hard_limit_cny)
        if not on_confirm(req):
            # 停在确认点：状态标「待精析」而不是「已完成」，
            # 否则用户以为跑完了 —— 这正是设计里那唯一一次停顿。
            _mark(task_id, config.STATUS_SCREENED)
            return RunResult(
                task_id=task_id, candidates=candidates, usage=usage_total,
                # ⚠️ 不在这里 round —— 数据层保留精度，显示格式是 UI 的责任。
                # 在数据层圆整会让"累计/比较"类操作累积误差。
                spent_cny=usage_total.cost_cny,
                estimated_upper_bound=estimates["total"],
                trimmed_seconds=scr.trimmed_seconds, degraded=degraded,
                finished=False)

    # ── ⑤ 精析（贵，只打候选点）───────────────────
    clips: list[dict] = []
    n = max(1, len(candidates))
    done_refine = 0

    def _stop_refining() -> bool:
        """提交下一个候选前的闸门：取消 / 超预算。

        ⚠️ 并发下这个闸门必须是**每次提交都过一遍**，不能只在开头查一次 ——
           否则用户设的花费上限会形同虚设（见 concurrency.run_tasks 的注释）。
        """
        if should_cancel and should_cancel():
            raise Cancelled("用户取消")
        if usage_total.cost_cny > opts.hard_limit_cny:
            if on_confirm:
                req = ConfirmRequest(
                    kind="over_budget",
                    message=(f"已花 ¥{usage_total.cost_cny:.2f}，"
                             f"超过设定的上限 ¥{opts.hard_limit_cny:.2f}。"),
                    spent_cny=round(usage_total.cost_cny, 4),
                    hard_limit_cny=opts.hard_limit_cny)
                if not on_confirm(req):
                    return True
                opts.hard_limit_cny *= 2      # 授权后放宽一倍
            else:
                return True
        return False

    def _do_refine(i: int, c):
        """工作线程：只做调用，**不碰任何共享状态**。"""
        return refine_one(video, c, profile, transport, work, i, opts,
                          should_cancel=should_cancel)

    def _finish_refine(i: int, c, payload) -> None:
        """★ 调用线程：累计花费、落库、报进度。"""
        nonlocal usage_total, done_refine
        result, u, deg = payload
        usage_total = usage_total + u
        degraded.extend(deg)
        done_refine += 1
        report("refine", 0.50 + 0.48 * (done_refine / n),
               f"精析 {done_refine}/{len(candidates)}", done_refine)
        if not result:
            degraded.append(f"候选 {i + 1} 返回无法解析")
            return
        clip_id = store.add_clip(
            task_id, result["start"], result["end"],
            type=result.get("kind", ""), score=result.get("score", 0),
            summary=result.get("summary", ""),
            title_hint=result.get("title", ""),
            hint=c.hint, confidence=c.confidence,
            selected=(result.get("score", 0) >= 70))
        clips.append({**result, "id": clip_id, "hint": c.hint,
                      "confidence": c.confidence})

    def _fail_refine(i: int, c, exc: BaseException) -> None:
        # ★ 失败原因必须进日志。这是事后唯一能查的证据链 ——
        #   2026-09-28 那次"0 个切片段"，原因（副本体积超限）只写在
        #   内存里的 degraded，日志里一个字都没有，只能靠翻 work 目录反推。
        log.warning("候选 %s 精析失败（源内 %.1f~%.1f 秒）：%s: %s",
                    i + 1, c.start, c.end, type(exc).__name__, exc)
        if isinstance(exc, analyzer.AnalyzerError):
            degraded.append(f"候选 {i + 1} 精析失败：{exc}")
        elif isinstance(exc, PipelineError):
            degraded.append(f"候选 {i + 1} 切副本失败：{exc}")
        else:
            degraded.append(f"候选 {i + 1} 精析异常："
                            f"{type(exc).__name__}: {exc}")

    _refine_workers = concurrency.effective_workers(
        opts.concurrency, total=len(candidates))
    if _refine_workers > 1 and len(candidates) > 1:
        degraded.append(f"精析并发 {_refine_workers} 路"
                        f"（可在设置里调整；并发越高越容易撞限流）。")
    log.info("开始精析 %s 个候选（%s 路并发，花费上限 ¥%.2f）",
             len(candidates), _refine_workers, opts.hard_limit_cny)

    concurrency.run_tasks(
        candidates,
        lambda i, c: _do_refine(i, c),
        workers=_refine_workers,
        should_cancel=_stop_refining,
        on_result=lambda i, r: _finish_refine(i, candidates[i], r),
        on_error=lambda i, e: _fail_refine(i, candidates[i], e))

    log.info("流水线结束：%s 个切片段，花费 ¥%.4f，提示 %s 条",
             len(clips), usage_total.cost_cny, len(degraded))
    report("refine", 1.0, f"完成，{len(clips)} 个切片段", len(clips))
    _mark(task_id, config.STATUS_READY)
    return RunResult(
        task_id=task_id, candidates=candidates, clips=clips,
        usage=usage_total,
        spent_cny=usage_total.cost_cny,        # 保留精度，不圆整
        estimated_upper_bound=estimates["total"],
        trimmed_seconds=scr.trimmed_seconds, degraded=degraded, finished=True)


# 「重新分析」只清这些 —— **不含转录**（理由见 clear_cache 的 docstring）
_REANALYZE_CLEAR = ("screen.jsonl", "refine", "frames")


def clear_cache(video: str | Path, *, keep_transcript: bool = True) -> None:
    """清掉某个素材的中间产物（用于「重新分析」）。

    `keep_transcript=True`（默认）**保留转录相关的一切**，只删
    「粗筛 / 精析」的产物：`screen.jsonl`、`refine/`、`frames/`。

    ★ 为什么默认保留转录（2026-09-28 修）：
      「重新分析」的语义是"换一版**筛选结果**"，而转录是**本地免费但极慢**
      的一步（84 分钟素材约 40 分钟），与筛选强度、需求描述**完全无关**。
      原先这里无条件 `rmtree(work)` ⇒ 用户想调一下筛选强度重看候选，
      要连 40 分钟转录一起重付，而界面上完全看不出来会这样。
      （更隐蔽的是：`transcribe_chunk` 当时还会无条件删掉已有的
        `chunk_*.speech.jsonl` ⇒ 转录结果**本来就不是缓存**。
        两处都已修。）

    `keep_transcript=False` 留给真正该重跑转录的场合：
    换了识别模型想强制重塑，或怀疑转录结果本身有问题。
    """
    work = config.APP_ROOT / "work" / Path(video).stem
    if not work.exists():
        return
    if not keep_transcript:
        shutil.rmtree(work, ignore_errors=True)
        return
    for name in _REANALYZE_CLEAR:
        p = work / name
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except OSError:
            log.warning("清理中间产物失败：%s", p, exc_info=True)
