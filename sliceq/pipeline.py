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
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from . import (analyzer, asr, concurrency, config, ffmpeg_tools,
               prompts, screening, store)
from .analyzer import Transport, Usage
from .screening import Candidate


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
def preset_for(transport: Transport) -> tuple[str, str]:
    """返回 (scale 高度, 码率)。

    通道 A 可传 100MB 本地文件 ⇒ 放宽到 720p/1Mbps；
    通道 B 要 base64 内联、上限约 15MB ⇒ 压到 480p/0.5Mbps。
    """
    if getattr(transport, "name", "") == "dashscope":
        return "720", "1000k"
    return "480", "500k"


def cut_clip(video: str | Path, out: str | Path, start: float, end: float,
             height: str = "720", bitrate: str = "1000k",
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
           "-c:a", "aac", "-b:a", "96k",
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
    height, bitrate = preset_for(transport)

    clip_path = work / "refine" / f"clip_{idx:04d}.mp4"
    if not (clip_path.exists() and clip_path.stat().st_size > 1024):
        cut_clip(video, clip_path, clip_start, clip_end,
                 height=height, bitrate=bitrate, should_cancel=should_cancel)

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
            return saved.get("result"), Usage(), []
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
        "degraded": res.degraded,
    }, ensure_ascii=False), encoding="utf-8")
    return result, res.usage, list(res.degraded)


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
    try:
        return _run_pipeline(
            task_id, video, opts, transport,
            on_progress=on_progress, on_confirm=on_confirm,
            should_cancel=should_cancel, work_dir=work_dir,
            skip_transcribe=skip_transcribe)
    except Cancelled:
        # 用户主动取消 —— 标「已取消」而不是「出错」。
        # 混用会让用户以为自己把程序搞坏了。
        _mark(task_id, config.STATUS_CANCELLED)
        raise
    except BaseException:                      # noqa: BLE001
        _mark(task_id, config.STATUS_ERROR)
        raise


def _run_pipeline(task_id: int,
                  video: str | Path,
                  opts: Options,
                  transport: Transport,
                  *,
                  on_progress: ProgressCb | None = None,
                  on_confirm: ConfirmCb | None = None,
                  should_cancel: CancelCb | None = None,
                  work_dir: str | Path | None = None,
                  skip_transcribe: list[asr.Segment] | None = None) -> RunResult:
    """流水线主体。

    ⚠️ **不要直接调用它** —— 用 `run()`。直接调会绕过异常时的
       状态落库（任务会永远停在中间状态）。
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
    degraded: list[str] = []
    _status_now = [""]                 # 已写库的状态（避免重复 UPDATE）

    def report(stage: str, ratio: float, msg: str, candidates: int = 0) -> None:
        # 阶段变化时把状态写库 —— 见 `run()` 的 docstring（v1.17 修正）
        st = _STAGE_STATUS.get(stage, "")
        if st and st != _status_now[0]:
            _status_now[0] = st
            _mark(task_id, st)
        if on_progress:
            on_progress(Progress(
                stage=stage, ratio=max(0.0, min(1.0, ratio)), message=msg,
                spent_cny=round(usage_total.cost_cny, 4),
                est_total_cny=estimates["total"], candidates=candidates))

    # ── ① 花费上限（给用户最坏预期）────────────────
    estimates = analyzer.estimate_total_upper_bound(duration, opts.candidate_ratio)

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

    concurrency.run_tasks(
        candidates,
        lambda i, c: _do_refine(i, c),
        workers=_refine_workers,
        should_cancel=_stop_refining,
        on_result=lambda i, r: _finish_refine(i, candidates[i], r),
        on_error=lambda i, e: _fail_refine(i, candidates[i], e))

    report("refine", 1.0, f"完成，{len(clips)} 个切片段", len(clips))
    _mark(task_id, config.STATUS_READY)
    return RunResult(
        task_id=task_id, candidates=candidates, clips=clips,
        usage=usage_total,
        spent_cny=usage_total.cost_cny,        # 保留精度，不圆整
        estimated_upper_bound=estimates["total"],
        trimmed_seconds=scr.trimmed_seconds, degraded=degraded, finished=True)


def clear_cache(video: str | Path) -> None:
    """清掉某个素材的全部中间产物（用于"重新分析"）。"""
    import shutil
    work = config.APP_ROOT / "work" / Path(video).stem
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
