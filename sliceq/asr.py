# -*- coding: utf-8 -*-
"""转录抽象层 —— 阶段 2 的地基。

═══════════════════════════════════════════════════════════════
设计要点（全部来自阶段 2 前置实测，见 reports/STAGE2-PRE-*.md）
═══════════════════════════════════════════════════════════════
1. **传参转义**：Windows 路径的 `C:` 会撞上 ffmpeg 滤镜的 `:` 分隔符，
   必须转成 `C\\:`（**双**反斜杠——滤镜串要穿过两层解析）。
2. **输出是 JSONL**，不是 JSON 文档；`start`/`end` 单位是**毫秒**。
3. **分段会重叠**，不能假设 `prev.end == next.start`。
4. **必须用 GPU**：CPU 下 3 小时直播要 2.6 小时，GPU 只要 23 分钟。
5. **不要用 `vad_model`** —— 实测**无效**。
   ffmpeg whisper 滤镜的 VAD 只决定"何时切分 buffer"，
   转录起点永远是 buffer 第 0 个样本（源码 `af_whisper.c:filter_frame()`），
   所以开头静音照样进 whisper。纯静音实验：无 VAD 出 11 段幻觉，有 VAD 也是 11 段。
   而耗时增加约 4 倍。
6. **幻觉治理改用三层防线**（实测 23 段 → 0 段，见
   `reports/STAGE2-PRE-HALLUCINATION-CONTROL.md`）：
     ① 静音预切除（silencedetect 扫静音 → 只喂语音区间给 whisper）
     ② 逐字重复剔除（同一句重复 ≥3 次 → 全丢）
     ③ 特征词剔除（感谢订阅 / 字幕制作 等模板文案）
7. **分片转录**：单次调用几十分钟没有进度反馈，且崩了就全丢。
   按静音点切分 → 可报进度、可断点续跑。

⚠️ 静音切除会**改变时间轴**，必须用 `map_time()` 映射回原轴。
   漏了这一步，字幕会整体错位，而且**不会报任何错**。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Iterable

from . import asr_models, config, ffmpeg_tools

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], None]   # (0~1 的进度, 描述)
CancelCb = Callable[[], bool]               # 返回 True 表示应中止
ChunkDoneCb = Callable[[int, list["Segment"]], None]  # (分片序号, 该片结果)


class AsrError(Exception):
    """转录失败，message 直接给用户看。"""


class AsrCancelled(Exception):
    """用户主动取消。"""


# ─────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────
@dataclass
class Segment:
    start_ms: int
    end_ms: int
    text: str

    @property
    def start_s(self) -> float:
        return self.start_ms / 1000.0

    @property
    def end_s(self) -> float:
        return self.end_ms / 1000.0

    def to_dict(self) -> dict:
        return asdict(self)


# 已知的 whisper 幻觉特征（用于过滤）
# 实测：无语音片段上会产出 `謝謝收看!`、`(字幕:XXX)` 之类的模板文案。
_HALLUCINATION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"^\(?\s*字幕[:：]",           # (字幕:J Chong)
        r"字幕(製作|制作|組|组)",
        r"^\s*[\(（]?\s*(謝謝|谢谢)(收看|觀看|观看|大家)",
        r"(訂閱|订阅).*(頻道|频道)",
        r"(小鈴鐺|小铃铛)",
        r"^\s*(MBC|SBS|KBS)\s*$",
        r"^\s*[\(（]?\s*(無法|无法|忘記了|忘记了)",
        r"^\s*(下次見|下次见)\s*$",
        r"^\s*[\(（][^）)]{0,20}[\)）]\s*$",   # 整段被括号包住
        # 电视台/平台的固定用语。直播里出现必然是幻觉
        # （实测：90s 素材里吐出 `优优独播剧场——YoYo Television Series Exclusive`）。
        r"(獨播劇場|独播剧场)",
        # ⚠️ 注意这里**故意没有** `感谢观看` / `谢谢观看` 这类。
        #    主播下播时真会说"感谢观看"，按词剔除会误杀。
        #    它们靠 `drop_repeated()` 的短句门槛清（有"重复"作客观判据），
        #    而不是靠词表猜。
    )
]


def looks_like_hallucination(text: str) -> bool:
    """粗判一句是否是幻觉。宁可漏判，不要误杀真实内容。"""
    t = text.strip()
    if not t:
        return True
    return any(p.search(t) for p in _HALLUCINATION_PATTERNS)


# ─────────────────────────────────────────────────────────────
# ffmpeg 调用基础设施
# ─────────────────────────────────────────────────────────────
def escape_filter_path(p: str | Path) -> str:
    r"""把 Windows 路径转成能放进 ffmpeg 滤镜参数的写法。

    ⚠️ 冒号必须转成 **双** 反斜杠：滤镜串要穿过两层解析
       （先按 `,;` 分 filtergraph，再按 `:` 分参数），
       单反斜杠在第一层就被消费掉，到不了第二层。

    实测：单反斜杠、单引号都不行，只有 `\\:` 有效。
    """
    return str(p).replace("\\", "/").replace(":", r"\\:")


def _ffmpeg() -> Path:
    ff = ffmpeg_tools.find_ffmpeg()
    if not ff:
        raise AsrError("未找到 FFmpeg。请先到「设置」页安装或指定。")
    return ff


def _run_ffmpeg(args: list[str], timeout: int = 3600,
                should_cancel: CancelCb | None = None) -> subprocess.CompletedProcess:
    """跑一次 ffmpeg，支持取消。

    ⚠️⚠️ **必须"边跑边排空管道"**（2026-09-28 实测死锁，release v0.1.0 中招）。

    原实现是「`while True: proc.poll()` 空转 + 结束后才 `communicate()`」——
    **循环里一个字节都不读**。ffmpeg 把管道缓冲区写满（Windows 上约 64KB）后
    就**永久阻塞在写上**，而父进程在等它退出 ⇒ 互相等，
    表现为：**进程活着、CPU 0%、不报错、不结束**。

    实测数据（84 分钟音频，`find_silences` 那条命令）：
      · 单独跑 **0.8 秒**就能结束
      · 却往 stderr 写了 **95,662 字节** > 65,536（缓冲区）
      ⇒ 必然死锁。**静音段越多，stderr 越大 —— 所以只在特定素材上发作。**

    更危险的是第 458 行那条 whisper 转录：它的 stdout 是 JSONL，**动辄几 MB**，
    同样会堵。

    ✅ `editor.py::_run_ffmpeg` 早已按「stderr 落临时文件」修过同一个坑
    （见那里的注释），**这里是漏掉的那一处**。

    修法：`communicate(timeout=...)` 会**用后台线程并发读两个管道**；
    超时抛 `TimeoutExpired`，但**已读到的数据不会丢**，可以继续 communicate。
    """
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace")
    t0 = time.time()
    while True:
        try:
            # ★ 关键：communicate 在等待期间持续排空 stdout+stderr
            out, err = proc.communicate(timeout=0.5)
            return subprocess.CompletedProcess(args, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            if should_cancel and should_cancel():
                proc.kill()
                proc.communicate()
                raise AsrCancelled("用户取消")
            if time.time() - t0 > timeout:
                proc.kill()
                proc.communicate()
                raise AsrError(f"FFmpeg 超时（>{timeout}s）")


# ─────────────────────────────────────────────────────────────
# 音频准备
# ─────────────────────────────────────────────────────────────
def extract_audio(media: str | Path, out_wav: str | Path,
                  should_cancel: CancelCb | None = None) -> Path:
    """提取 16kHz 单声道 WAV —— whisper 的原生输入格式。

    ⚠️ 即使 ffmpeg 会自动重采样，显式转一次也值得：
       后续分片、静音检测都在这个统一格式上做，不用反复解码视频。
    """
    out = Path(out_wav)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        str(_ffmpeg()), "-hide_banner", "-loglevel", "error",
        "-y", "-i", str(media),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(out),
    ], should_cancel=should_cancel)
    if not out.exists():
        raise AsrError("音频提取失败（FFmpeg 未产出文件）")
    return out


def find_silences(wav: str | Path, noise_db: int = -35,
                  min_duration: float = 1.0,
                  should_cancel: CancelCb | None = None) -> list[tuple[float, float]]:
    """扫静音区间，返回 [(起, 止)] 秒。

    实测：扫 2.5 小时音频只要 10 秒 —— 比 whisper 的 VAD 便宜得多。
    用途：① 选分片边界（切在静音处不会切断句子）② 可选跳过静音。
    """
    r = _run_ffmpeg([
        str(_ffmpeg()), "-hide_banner", "-nostats",
        "-i", str(wav), "-vn",
        "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
        "-f", "null", "-",
    ], should_cancel=should_cancel)

    silences: list[tuple[float, float]] = []
    cur: float | None = None
    for line in (r.stderr or "").splitlines():
        if "silence_start:" in line:
            try:
                cur = float(line.split("silence_start:")[1].strip().split()[0])
            except Exception:
                cur = None
        elif "silence_end:" in line and cur is not None:
            try:
                end = float(line.split("silence_end:")[1].strip().split()[0])
                silences.append((cur, end))
            except Exception:
                pass
            cur = None
    return silences


def plan_chunks(duration_s: float, silences: list[tuple[float, float]],
                target_s: float = 900.0) -> list[tuple[float, float]]:
    """把全长切成若干片，**边界尽量落在静音中点**。

    为什么不用等分：等分会把句子从中间切断，导致首尾各丢半个词。
    切在静音里，两侧都是完整的句子。

    target_s 默认 900（15 分钟）：
      - 太小 → FFmpeg 每次启动开销占比高（模型加载是固定成本）
      - 太大 → 进度反馈太钝、崩了丢太多
    """
    if duration_s <= 0:
        return [(0.0, 0.0)]
    if duration_s <= target_s * 1.2:
        return [(0.0, duration_s)]

    # 候选切点 = 各静音区间的中点
    candidates = [(a + b) / 2 for a, b in silences if b > a]

    chunks: list[tuple[float, float]] = []
    start = 0.0
    while True:
        ideal = start + target_s
        if duration_s - ideal <= target_s * 0.6:   # 尾巴太短就并进去
            chunks.append((start, duration_s))
            break
        # 找离 ideal 最近、且落在 (start+target*0.5, start+target*1.5) 的静音中点
        lo, hi = start + target_s * 0.5, start + target_s * 1.5
        near = [c for c in candidates if lo <= c <= hi]
        cut = min(near, key=lambda c: abs(c - ideal)) if near else ideal
        chunks.append((start, cut))
        start = cut
    return chunks


# ─────────────────────────────────────────────────────────────
# 静音预切除（幻觉治理第 1 层）
#
# 为什么不用 ffmpeg 的 vad_model：它只做分片，不滤除非语音，
# 转录起点永远是 buffer 第 0 样本 —— 开头静音照样被转录。
# 实测：30 秒纯静音，无 VAD 出 11 段幻觉，有 VAD 也是 11 段，
# 而耗时 ×4。详见 reports/STAGE2-PRE-HALLUCINATION-CONTROL.md §3。
# ─────────────────────────────────────────────────────────────
def speech_ranges(silences: list[tuple[float, float]], total: float,
                  pad: float = 0.25,
                  min_silence: float = 2.0) -> list[tuple[float, float]]:
    """静音区间的**补集** = 语音区间（两侧留 pad），并合并重叠。

    参数：
      pad          —— 两侧各留多少秒。避免从词中间切断。
                      0.25 是实测取值：残留静音短到不足以诱发幻觉，
                      又足以保住词边界。
      min_silence  —— 短于这个长度的静音**不算静音**，不切。
                      语音中间的短暂停顿（<2s）切了会断句，
                      而且实测短停顿基本不诱发幻觉。

    ⚠️ 返回空列表是合法情况（整段都是静音），调用方必须特判 ——
       空区间不能直接交给 ffmpeg，见 `extract_speech_only()`。
    """
    if total <= 0:
        return []
    sil = sorted((a, b) for a, b in silences if b - a >= min_silence)

    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in sil:
        if a > cursor:
            speech.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < total:
        speech.append((cursor, total))

    padded = [(max(0.0, a - pad), min(total, b + pad)) for a, b in speech]

    merged: list[list[float]] = []
    for a, b in padded:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a > 0.05]


def extract_speech_only(wav: str | Path, out: str | Path,
                        ranges: list[tuple[float, float]],
                        should_cancel: CancelCb | None = None) -> float:
    """把语音区间拼成一条连续音频，返回新时长（秒）。

    用 `aselect` 表达式而不是 `atrim`+`concat`：区间多时前者紧凑得多，
    不会撞上命令行的长度上限。

    ⚠️⚠️ ranges 为空时**必须直接返回 0，不能把空表达式交给 ffmpeg**。
       实测 `aselect=''` 不报错、不警告，**直接输出整段原音频** ——
       等于静音一条没切，幻觉全部漏过去，而且没有任何迹象。
       （这就是"没切"和"切了但没效果"长得一模一样的那个坑。）
    """
    if not ranges:
        return 0.0
    expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges)
    o = Path(out)
    o.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg([
        str(_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(wav),
        "-af", f"aselect='{expr}',asetpts=N/SR/TB",
        "-c:a", "pcm_s16le", str(o),
    ], should_cancel=should_cancel)
    return _wav_duration(o)


def map_time(t: float, ranges: list[tuple[float, float]]) -> float:
    """把「拼接后的时间」映射回「原音频时间」。

    ⚠️ 这一步是静音切除的**必要配套**。漏了它，整条时间轴会偏移，
       而且不报错 —— 属于"每个环节都成功、端到端结果是错的"那类缺陷。
    """
    acc = 0.0
    for a, b in ranges:
        dur = b - a
        if t < acc + dur:
            return a + (t - acc)
        acc += dur
    return ranges[-1][1] if ranges else t


# ─────────────────────────────────────────────────────────────
# 重复文本剔除（幻觉治理第 2 层）
# ─────────────────────────────────────────────────────────────
_REPEAT_MIN_LEN = 8      # ≥8 字：重复 3 次即判为幻觉
_REPEAT_MIN_COUNT = 3
# ⚠️ 短句必须单独一档。
#
#   实测（2026-09-26，90s 素材）：`感谢观看`（**4 字**）独立成段出现 5 次，
#   因为达不到 8 字门槛，**根本没参与判重**，5 段全部留下。
#   这类短幻觉恰恰最常见（"感谢观看""谢谢大家"），而长句门槛对它们完全无效。
#
#   但短句也不能直接套用 3 次门槛 —— 真实口语里"我知道了"（4 字）重复
#   3 次完全可能。所以短句要求**更多次**：客观判据（"真实讲话不会逐字
#   重复 5 遍"）仍然成立，只是把门槛抬高来覆盖误杀风险。
_REPEAT_SHORT_LEN = 4
_REPEAT_SHORT_COUNT = 5


def drop_repeated(segments: list[Segment]) -> tuple[list[Segment], int]:
    """剔除「逐字重复」的句子。返回 (保留, 剔除数)。

    为什么有效：whisper 在无语音片段上会反复吐同一句模板文案
    （实测 140s 素材里，`请不吝点赞 订阅 转发 打赏支持明镜与点点栏目`
    在 5 个不同位置被原样吐出）。真实讲话不会逐字重复这么多遍。

    ⚠️ 必须**全局**判重，不能按分片做 —— 重复会跨片出现。
    ⚠️ 门槛分两档（见上面常量）：长句 3 次、短句 5 次。
       统一用 3 次会误杀真实口语；只留 8 字门槛则漏掉最常见的短幻觉。
    ⚠️ 低于 `_REPEAT_SHORT_LEN` 的极短句**一律不判** ——
       "好""啊""对"重复出现是正常的，清掉它们等于毁掉转录。
    """
    def key(s: Segment) -> str:
        return re.sub(r"\s+", "", s.text or "")

    def threshold(k: str) -> int | None:
        """这段文本要重复几次才判为幻觉？None = 不参与判重。"""
        if len(k) >= _REPEAT_MIN_LEN:
            return _REPEAT_MIN_COUNT
        if len(k) >= _REPEAT_SHORT_LEN:
            return _REPEAT_SHORT_COUNT
        return None

    counts: dict[str, int] = {}
    for s in segments:
        k = key(s)
        if threshold(k) is not None:
            counts[k] = counts.get(k, 0) + 1

    if not counts:
        return list(segments), 0

    kept: list[Segment] = []
    dropped = 0
    for s in segments:
        k = key(s)
        t = threshold(k)
        if t is not None and counts.get(k, 0) >= t:
            dropped += 1
        else:
            kept.append(s)
    return kept, dropped


# ─────────────────────────────────────────────────────────────
# 单片转录
# ─────────────────────────────────────────────────────────────
def _parse_jsonl(path: Path) -> list[Segment]:
    """解析 whisper 滤镜的 JSONL 输出。

    ⚠️ 是 **JSON Lines**（每行一个 {start,end,text}），
       不是 JSON 文档 —— 直接 json.loads 整个文件会报 Extra data。
    """
    segs: list[Segment] = []
    if not path.exists():
        return segs
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            segs.append(Segment(int(d["start"]), int(d["end"]),
                                str(d.get("text", "")).strip()))
        except Exception:
            continue
    return segs


def _chunk_fingerprint(model_path: Path, language: str, use_gpu: bool) -> str:
    """转录缓存的**参数指纹** —— 决定一个已存在的分片结果还能不能复用。

    ⚠️ 为什么必须有指纹：转录结果一旦被缓存复用，
       **换了模型/档位就不会生效**（用户会以为"换了模型没变化"）。
       这是"缓存没有内容指纹"的典型形状。
       指纹里放三样够用的东西：模型文件身份、语言、是否 GPU。
       模型身份用 (文件名, 大小, mtime) —— 换挡位必然换文件，
       同名覆盖也会改变 size/mtime。
    """
    try:
        st = model_path.stat()
        ident = f"{model_path.name}:{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        ident = model_path.name
    return f"{ident}|{language}|{int(bool(use_gpu))}"


def _cached_chunk(dest: Path, fingerprint: str) -> list[Segment] | None:
    """若该分片已有**参数一致**的转录结果，直接返回；否则 None。"""
    if not (dest.exists() and dest.stat().st_size > 0):
        return None
    meta = dest.with_suffix(dest.suffix + ".meta")
    if meta.exists():
        try:
            if meta.read_text(encoding="utf-8").strip() != fingerprint:
                log.info("分片 %s 的识别参数已变化，重跑", dest.name)
                return None
        except OSError:
            return None
    else:
        # 老缓存（v0.1.3 之前）没有指纹文件 ⇒ 按"可用"处理并补写。
        # 风险：用户在生成它之后换过模型的话，会拿到旧结果。
        # 但代价对比悬殊 —— 不复用意味着每次重跑整条素材的转录
        # （84 分钟素材约 40 分钟），远大于这个低概率风险。
        log.debug("分片缓存无指纹，按可用处理：%s", dest.name)
    return _parse_jsonl(dest)


def transcribe_chunk(wav: str | Path, dest: Path,
                     model: str | Path | None = None,
                     vad: str | Path | None = None,
                     vad_threshold: float = 0.5,
                     language: str = "zh",
                     use_gpu: bool = True,
                     timeout: int = 7200,
                     should_cancel: CancelCb | None = None) -> list[Segment]:
    """转录一个音频文件，返回分段列表（毫秒，相对该文件起点）。

    ⚠️ `vad` / `vad_threshold` 参数**保留但不要在生产路径使用**：
       实测（2026-09-25）它不能过滤非语音 —— 滤镜只在"何时切分 buffer"上
       用 VAD，转录起点永远是 buffer 第 0 个样本。纯静音实验：无 VAD 出 11 段
       幻觉，有 VAD 也是 11 段，而耗时增加约 4 倍。
       `transcribe()` 已不再传这两个参数；保留签名仅为将来复查留口子。
    """
    model_path = Path(model) if model else asr_models.model_path()
    if not model_path.exists():
        raise AsrError(
            f"模型不存在：{model_path.name}\n"
            "请到「设置」页下载转录模型。")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # ★ 缓存复用（2026-09-28 新增）
    #
    # 原先这里无条件 `dest.unlink(missing_ok=True)` 然后重跑 whisper ——
    # 意味着**转录结果从来没有被当作缓存**。而转录是全流程最慢的一步
    # （84 分钟素材约 40 分钟），并且**与"换一版筛选结果"完全无关**：
    # 用户只是想调一下筛选强度重看候选，也要重付这 40 分钟。
    # 更糟的是 GUI 的「重新分析」会先 rmtree 整个 work 目录，连
    # audio.wav / chunk_*.speech.wav 都一起没，代价还要更大。
    #
    # 缓存的正确性依赖两点，都已处理：
    #   ① 识别参数变化 → `_chunk_fingerprint` 比对（模型/语言/GPU）
    #   ② 输入音频变化 → 上游 `chunk_*.speech.wav` 自己也有存在性检查
    fingerprint = _chunk_fingerprint(model_path, language, use_gpu)
    reused = _cached_chunk(dest, fingerprint)
    if reused is not None:
        return reused

    dest.unlink(missing_ok=True)

    af = (
        f"whisper=model={escape_filter_path(model_path)}"
        f":language={language}"
        f":format=json"
        f":destination={escape_filter_path(dest)}"
        f":use_gpu={'true' if use_gpu else 'false'}"
    )
    if vad:
        af += (f":vad_model={escape_filter_path(vad)}"
               f":vad_threshold={vad_threshold}")

    r = _run_ffmpeg([
        str(_ffmpeg()), "-hide_banner", "-loglevel", "error",
        "-i", str(wav), "-vn", "-af", af, "-f", "null", "-",
    ], timeout=timeout, should_cancel=should_cancel)

    if r.returncode != 0 and not dest.exists():
        tail = (r.stderr or "").strip().splitlines()
        raise AsrError("转录失败：" + (tail[-1] if tail else "未知错误"))

    # 记下指纹 —— 下次才知道这份结果是不是"当前这套参数"产生的
    try:
        dest.with_suffix(dest.suffix + ".meta").write_text(
            fingerprint, encoding="utf-8")
    except OSError:
        pass
    return _parse_jsonl(dest)


# ─────────────────────────────────────────────────────────────
# 完整流程
# ─────────────────────────────────────────────────────────────
def transcribe(media: str | Path,
               model: str | Path | None = None,
               language: str = "zh",
               use_gpu: bool = True,
               chunk_seconds: float = 900.0,
               work_dir: str | Path | None = None,
               progress: ProgressCb | None = None,
               should_cancel: CancelCb | None = None,
               on_chunk_done: ChunkDoneCb | None = None,
               drop_hallucinations: bool = True,
               skip_silence: bool = True,
               min_silence_to_cut: float = 2.0) -> list[Segment]:
    """把「一个媒体文件」变成「全部分段」。时间戳相对整个媒体起点。

    分片策略：按静音点切，逐片转录，结果按片偏移平移后合并。
    这样可报进度、可断点续跑（每片完成回调一次，调用方自行落库）。

    幻觉治理（三层，实测 23 段 → 0 段，见
    `reports/STAGE2-PRE-HALLUCINATION-CONTROL.md`）：
      ① `skip_silence`  —— 片内静音预切除，只喂语音给 whisper
      ② `drop_hallucinations` —— 特征词剔除（逐片）+ 逐字重复剔除（全局，最后）
      ③ 同上

    ⚠️ **已移除 `enable_vad` / `vad_threshold` 参数**：实测 ffmpeg whisper 滤镜的
       VAD 不能消除静音幻觉（只做分片），且耗时 ×4。不要再加回来。

    ⚠️ `on_chunk_done` 回调给的是**该片已做特征词过滤、但尚未全局判重**的结果。
       **终态以返回值为准** —— 判重剔除在全部转录完成后统一进行。
    """
    media = Path(media)
    if not media.exists():
        raise AsrError(f"文件不存在：{media}")

    use_gpu = use_gpu and has_cuda()
    work = Path(work_dir) if work_dir else (config.APP_ROOT / "work" / media.stem)
    work.mkdir(parents=True, exist_ok=True)

    def report(p: float, msg: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, p)), msg)

    # ── 1. 提取音频 ─────────────────────────────────
    report(0.0, "提取音频")
    wav = work / "audio.wav"
    if not (wav.exists() and wav.stat().st_size > 1024):
        wav = extract_audio(media, wav, should_cancel=should_cancel)
    duration = _wav_duration(wav)

    # ── 2. 规划分片 ─────────────────────────────────
    report(0.02, "分析静音位置")
    silences = find_silences(wav, should_cancel=should_cancel)
    chunks = plan_chunks(duration, silences, target_s=chunk_seconds)

    # ── 3. 逐片转录 ─────────────────────────────────
    all_segs: list[Segment] = []
    n = len(chunks)
    cut_total = 0.0
    for i, (a, b) in enumerate(chunks):
        if should_cancel and should_cancel():
            raise AsrCancelled("用户取消")

        base = 0.05 + 0.93 * (i / max(n, 1))
        report(base, f"转录中 {i + 1}/{n}")

        piece = work / f"chunk_{i:04d}.wav"
        if not piece.exists():
            _run_ffmpeg([
                str(_ffmpeg()), "-hide_banner", "-loglevel", "error",
                "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}",
                "-i", str(wav), "-c", "copy", str(piece),
            ], should_cancel=should_cancel)

        # ── 片内静音预切除（幻觉治理第 1 层）─────────
        src, ranges = piece, None
        dest = work / f"chunk_{i:04d}.jsonl"
        if skip_silence:
            # 把全局静音区间裁到本片坐标系
            local = [(max(0.0, s - a), min(b - a, e - a))
                     for s, e in silences if e > a and s < b]
            cand = speech_ranges(local, b - a, min_silence=min_silence_to_cut)
            kept = sum(e - s for s, e in cand)

            # ⚠️ 整片都是静音 —— 直接跳过，不要交给 whisper。
            # 否则等于把整段静音喂进去，换来一堆幻觉。
            if not cand:
                cut_total += (b - a)
                if on_chunk_done:
                    on_chunk_done(i, [])
                continue

            # 只有真的切掉了可观的部分才走拼接路径，否则白多一次 ffmpeg 调用
            if kept < (b - a) * 0.98:
                speech_wav = work / f"chunk_{i:04d}.speech.wav"
                if not (speech_wav.exists() and speech_wav.stat().st_size > 1024):
                    extract_speech_only(piece, speech_wav, cand,
                                        should_cancel=should_cancel)
                src = speech_wav
                dest = work / f"chunk_{i:04d}.speech.jsonl"
                ranges = cand
                cut_total += (b - a) - kept

        segs = transcribe_chunk(src, dest, model=model,
                                language=language, use_gpu=use_gpu,
                                should_cancel=should_cancel)

        # ── 时间轴映射 + 片偏移 ──────────────────────
        # ⚠️ 顺序不能反：先映射回「片内原轴」，再加「片在全局的偏移」。
        for s in segs:
            if ranges:
                s.start_ms = int(round(map_time(s.start_ms / 1000.0, ranges) * 1000))
                s.end_ms = int(round(map_time(s.end_ms / 1000.0, ranges) * 1000))
            s.start_ms += int(a * 1000)
            s.end_ms += int(a * 1000)

        if drop_hallucinations:
            segs = [s for s in segs if not looks_like_hallucination(s.text)]

        all_segs.extend(segs)
        if on_chunk_done:
            on_chunk_done(i, segs)

    # ── 4. 全局判重（幻觉治理第 2 层）────────────────
    # ⚠️ 必须放在全部片合并之后：重复会跨分片出现，按片判重抓不到。
    dropped = 0
    if drop_hallucinations:
        all_segs, dropped = drop_repeated(all_segs)

    msg = "转录完成"
    if cut_total > 1:
        msg += f"（跳过静音 {cut_total:.0f}s"
        msg += f"，剔除重复 {dropped} 段）" if dropped else "）"
    elif dropped:
        msg += f"（剔除重复 {dropped} 段）"
    report(1.0, msg)
    return all_segs


def _wav_duration(wav: Path) -> float:
    """读 WAV 时长（用 ffprobe）。"""
    probe = ffmpeg_tools.find_ffprobe(ffmpeg_tools.find_ffmpeg())
    if not probe:
        return 0.0
    try:
        out = subprocess.run(
            [str(probe), "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(wav)],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace").stdout
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return 0.0


def has_cuda() -> bool:
    """探测本机能否用 CUDA 加速。

    实测：GPU 与 CPU 差 6~9 倍 —— CPU 下 3 小时直播要 2.6 小时，
    所以这个探测直接决定"能不能用"。
    """
    import os
    import shutil
    if os.name != "nt":
        return False
    if shutil.which("nvidia-smi"):
        return True
    return Path(r"C:\Windows\System32\nvidia-smi.exe").exists()


# ─────────────────────────────────────────────────────────────
# 输出
# ─────────────────────────────────────────────────────────────
def to_plain_text(segments: Iterable[Segment], joiner: str = "") -> str:
    return joiner.join(s.text for s in segments)


def to_srt(segments: Iterable[Segment]) -> str:
    """生成 SRT 字符串。

    ⚠️ 时间戳必须是 `HH:MM:SS,mmm`（逗号，不是点）。
    ⚠️ 编码由调用方负责写 **UTF-8 无 BOM**（达芬奇拒收带 BOM 的）。
    """
    lines: list[str] = []
    for i, s in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_ts(s.start_ms)} --> {_ts(s.end_ms)}")
        lines.append(s.text)
        lines.append("")
    return "\n".join(lines)


def _ts(ms: int) -> str:
    if ms < 0:
        ms = 0
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    sec, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{milli:03d}"


def save_srt(segments: Iterable[Segment], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ utf-8 而不是 utf-8-sig —— BOM 会被达芬奇拒收
    p.write_text(to_srt(segments), encoding="utf-8")
    return p
