# -*- coding: utf-8 -*-
"""剪辑执行 —— 把候选片段变成带字幕的成片（全部通过 FFmpeg）。

## 产出形态：乙方案（每条候选一个独立文件）

产品决策为**乙方案**：勾选 N 条候选 → 导出 **N 个** mp4。
（对比甲方案"拼成一个大文件"：切片 man 要的是能单独发布的一条条短视频，
  而不是一个几十分钟的合集。）

这个选择在技术上带来一个**很大的红利**：

    时间轴重映射从「跨段累加」
    退化成「整体平移一个偏移量」——
    timeline_t = source_t - clip.start

甲方案（OTIO 引用原片 + 多段拼接）需要 `t - seg[i].start + Σ(前 i 段时长)`，
累加项一多就成了 bug 高发区。而"少算一段"这类错误**不报错**，
只表现为"字幕整体错位"。

## 三种模式

| 模式 | 视频编码 | 能否烧字幕 | 实测精度 |
|------|---------|-----------|---------|
| `exact` 精确 | 重编码 libx264 | ✅ | 时长误差 **0.000s** |
| `fast`  快速 | `-c copy` | ❌ | 时长误差 +0.10~0.15s，**但内容对齐逐像素一致** |

⚠️ **`-c copy` 无法烧字幕**（烧字幕必然要重编码像素）。
   用户若同时勾了"快速"和"烧字幕"，本模块会自动升到 `exact` 并如实告知，
   而不是悄悄丢掉字幕。

## 已实测的两条关键事实（别再重新踩）

1. **`-ss` 前置时，ASS 的时间基准是「段内相对时间」**
   （实测：切源 30~45s，ASS 里 0~3s 的字幕出现在输出第 1 秒的画面里；
     12 秒处无字幕）。所以 `subtitle.remap_to_clip()` 产出的时间正好可直接用。
2. **`subtitles` 滤镜的路径必须双反斜杠转义，或走 cwd + 相对路径。**
   本模块用后者（`subtitle_style.ass_filter_arg`），因为路径里没有冒号
   就不需要转义 —— 少一个出错点。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import config, enhance as enhance_mod, ffmpeg_tools, subproc, store, subtitle, subtitle_style

ProgressCb = Callable[[float, str, int], None]     # ratio, message, done_count
CancelCb = Callable[[], bool]


class EditorError(Exception):
    """剪辑/导出过程中的可读错误。"""


# ─────────────────────────────────────────────────────────────
# 编码参数
# ─────────────────────────────────────────────────────────────
# 精确模式：CRF 20 在"肉眼无损"和"体积可接受"之间。
# 不用 preset slow —— 切片场景动辄几十分钟，用户等不起。
EXACT_VIDEO_ARGS = ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-pix_fmt", "yuv420p"]
EXACT_AUDIO_ARGS = ["-c:a", "aac", "-b:a", "128k"]
# moov 前置：发到平台/网页播放时不必等整个文件下完
FASTSTART_ARGS = ["-movflags", "+faststart"]

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_filename(name: str, fallback: str = "clip", limit: int = 60) -> str:
    """文件名安全化。

    Windows 的非法字符会让 open() 直接抛 OSError —— 而候选标题来自模型，
    完全可能带 `/` `|` 这类字符（模型喜欢写 "扫码支付 | 乱象"）。
    """
    s = _ILLEGAL.sub("_", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" ._")
    if len(s) > limit:
        s = s[:limit].rstrip(" ._")
    return s or fallback


# ─────────────────────────────────────────────────────────────
# 结果
# ─────────────────────────────────────────────────────────────
@dataclass
class ClipExport:
    """单条候选的导出结果。"""

    clip_id: int = 0
    index: int = 0
    title: str = ""
    ok: bool = False
    video: str = ""
    srt: str = ""
    cover: str = ""                # 封面图路径（阶段 4）
    cues: int = 0
    dropped_cues: int = 0
    translated: int = 0            # 拿到译文的字幕条数（双语时）
    duration: float = 0.0
    src_start: float = 0.0
    src_end: float = 0.0
    cost_cny: float = 0.0          # 本条产生的 API 花费（翻译/文案）
    error: str = ""
    notes: list[str] = field(default_factory=list)   # 非致命提示（如字体被替换）

    def to_dict(self) -> dict:
        return {
            "clip_id": self.clip_id, "index": self.index, "title": self.title,
            "ok": self.ok, "video": self.video, "srt": self.srt,
            "cover": self.cover,
            "cues": self.cues, "dropped_cues": self.dropped_cues,
            "translated": self.translated,
            "duration": round(self.duration, 3),
            "src_start": self.src_start, "src_end": self.src_end,
            "cost_cny": round(self.cost_cny, 6),
            "error": self.error, "notes": list(self.notes),
        }


@dataclass
class ExportResult:
    out_dir: Path
    items: list[ClipExport] = field(default_factory=list)
    mode: str = "exact"
    subtitles_burned: bool = False
    enhance_summary: str = ""
    elapsed: float = 0.0
    cancelled: bool = False

    @property
    def ok_count(self) -> int:
        return sum(1 for i in self.items if i.ok)

    @property
    def fail_count(self) -> int:
        return sum(1 for i in self.items if not i.ok)

    @property
    def total_duration(self) -> float:
        return sum(i.duration for i in self.items if i.ok)

    def summary(self) -> str:
        return (f"成功 {self.ok_count} / 失败 {self.fail_count}，"
                f"时长合计 {self.total_duration / 60:.1f} 分钟，"
                f"耗时 {self.elapsed:.0f} 秒")


# ─────────────────────────────────────────────────────────────
# ffmpeg 执行（带进度解析与取消）
# ─────────────────────────────────────────────────────────────
def _parse_time_us(line: str) -> float | None:
    """解析 `-progress` 输出里的时间，返回**秒**。

    ⚠️ ffmpeg 的 `out_time_ms` 实际单位是**微秒**（历史遗留的命名错误），
       所以优先读 `out_time_us`；两个都没有时退回 `out_time=HH:MM:SS.xxx`。
    """
    if "=" not in line:
        return None
    key, _, val = line.partition("=")
    val = val.strip()
    if key in ("out_time_us", "out_time_ms"):
        try:
            return int(val) / 1_000_000
        except ValueError:
            return None
    if key == "out_time":
        m = re.match(r"(\d+):(\d\d):(\d\d(?:\.\d+)?)", val)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return None


def run_ffmpeg(cmd: Sequence[str], *,
               total_seconds: float = 0.0,
               on_progress: Callable[[float], None] | None = None,
               should_cancel: CancelCb | None = None,
               cwd: str | Path | None = None) -> None:
    """跑一次 ffmpeg，边跑边报进度，支持取消。

    `cwd` 是**必须传的**：字幕走「相对路径 + 工作目录」来规避
    ffmpeg 滤镜参数的冒号转义问题（见 `subtitle_style.ass_filter_arg`）。
    不传 cwd 的话，滤镜里写的是相对文件名，ffmpeg 在进程默认目录下
    找不到它 —— 报 `Unable to open xxx.ass`，而错误信息不会提示
    "是工作目录不对"。

    ⚠️ stderr 必须被**持续消费**。只读 stdout 而不读 stderr 时，
       ffmpeg 写满管道缓冲区后会永久阻塞 —— 表现为"卡住不动"，
       而且不会有任何报错。这里把 stderr 导到临时文件，两边都不堵。
    """
    argv = list(cmd)
    if on_progress and total_seconds > 0 and "-progress" not in argv:
        argv += ["-progress", "pipe:1", "-nostats"]

    errf = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    try:
        proc = subproc.popen(
            argv, stdout=subprocess.PIPE, stderr=errf,
            cwd=str(cwd) if cwd else None,
            text=True, encoding="utf-8", errors="replace", bufsize=1)

        cancelled = False
        try:
            if proc.stdout is not None:
                for raw in proc.stdout:
                    line = raw.strip()
                    if line == "progress=end":
                        break
                    sec = _parse_time_us(line)
                    if sec is not None and total_seconds > 0 and on_progress:
                        on_progress(max(0.0, min(1.0, sec / total_seconds)))
                    if should_cancel and should_cancel():
                        cancelled = True
                        proc.terminate()
                        break
            proc.wait(timeout=30 if not cancelled else 10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        finally:
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

        if cancelled:
            raise EditorError("已取消")

        errf.seek(0)
        tail = errf.read()[-1500:].strip()
        if proc.returncode != 0:
            raise EditorError(_explain_ffmpeg_error(proc.returncode, tail, argv))
    finally:
        try:
            errf.close()
        except Exception:
            pass


def _explain_ffmpeg_error(code: int, stderr_tail: str, argv: Sequence[str]) -> str:
    """把 ffmpeg 的报错翻成能照着做的中文提示。"""
    low = (stderr_tail or "").lower()
    if "no such file or directory" in low:
        return (f"FFmpeg 找不到文件（退出码 {code}）。\n"
                "请确认源视频还在原位置、没有被移动或删除。\n\n"
                f"{stderr_tail[-400:]}")
    if "invalid data found" in low or "moov atom not found" in low:
        return (f"源文件损坏或格式不支持（退出码 {code}）。\n"
                f"试着用播放器打开确认它完整。\n\n{stderr_tail[-400:]}")
    if "cannot load default config file" in low:
        return (f"FFmpeg 字体配置缺失（退出码 {code}）。\n"
                "这是已知问题：本机 ffmpeg 构建缺 fontconfig 配置，\n"
                "`drawtext` 滤镜会段错误、`subtitles` 滤镜正常。\n"
                "字幕功能不受影响；若你在用封面标题功能，请换用其它实现。\n\n"
                f"{stderr_tail[-400:]}")
    if "disk full" in low or "no space left" in low:
        return f"磁盘空间不足（退出码 {code}）。请清理后重试。"
    if "permission denied" in low:
        return (f"没有写入权限（退出码 {code}）。\n"
                "输出目录可能被占用或只读，换一个目录试试。")
    return f"FFmpeg 失败（退出码 {code}）：\n{stderr_tail[-600:]}"


# ─────────────────────────────────────────────────────────────
# 命令构造
# ─────────────────────────────────────────────────────────────
def build_cut_cmd(src: str | Path, start: float, end: float,
                  out: str | Path, *, mode: str = "exact",
                  ass: str | Path | None = None,
                  work_dir: str | Path | None = None,
                  enhance: "enhance_mod.EnhanceOptions | None" = None,
                  has_audio: bool | None = None) -> tuple[list[str], str | None]:
    """构造切片命令。返回 (argv, cwd)。

    `-ss` / `-t` 都放在 `-i` **之前**（实测时长误差 0.000s，精确模式）。
    这样 `subtitles` 滤镜看到的时间从 0 开始，
    正好等于 `subtitle.remap_to_clip()` 产出的「段内相对时间」。

    四种组合：

        enhance 生效      → filter_complex（视频滤镜 + 音频混音一次编码完成）
        ass 存在          → -vf 烧字幕，必须重编码
        mode=fast 且无 ass → -c copy，最快且内容逐像素一致
        其余              → 重编码

    ⚠️ 增强**不能**走「先切片再增强」的两步走 —— 那会让画面二次编码，
       无谓地多损失一代画质。所以有 BGM 时改用 `-filter_complex`，
       把 fade / 字幕 / 混音压进同一遍编码。
    """
    dur = max(0.0, end - start)
    ff = ffmpeg_tools.find_ffmpeg()
    if not ff:
        raise EditorError(
            "未找到 FFmpeg。请到「设置 → 环境」里安装，或手动指定路径。")

    argv: list[str] = [str(ff), "-hide_banner", "-loglevel", "error",
                       "-nostdin", "-y",
                       "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                       "-i", str(src)]
    cwd: str | None = None

    bgm_path: Path | None = None
    if enhance is not None and not enhance.is_noop():
        bgm_path = enhance_mod.resolve_bgm(enhance.bgm)

    # ── 分支 1：增强（滤镜图统一走 filter_complex）────────────
    if enhance is not None and not enhance.is_noop():
        if mode == "fast":
            # -c copy 加不了任何滤镜，只能升到重编码
            mode = "exact"
        if has_audio is None:
            has_audio = ffmpeg_tools.has_audio_stream(src)
        if bgm_path is not None and not has_audio:
            # 原片无声 + 有 BGM：直接放 BGM，不需要 ducking（没人声可 duck）
            pass

        ass_arg = None
        if ass:
            ass_arg, cwd = subtitle_style.ass_filter_arg(ass, cwd=work_dir)

        v = enhance_mod.video_chain(dur, enhance, ass_arg) or "null"
        bgm_input = 1 if bgm_path is not None else None
        a_graph, a_label = enhance_mod.audio_filter_complex(
            dur, enhance, has_voice=bool(has_audio), bgm_input=bgm_input)

        fc = f"[0:v]{v}[vout]"
        if a_graph:
            fc = f"{fc};{a_graph}"

        if bgm_path is not None:
            # -stream_loop -1：BGM 短于片段时循环填充。
            # 滚无限长的输入必须靠输出侧的 -t 收口（见下）。
            argv += (["-stream_loop", "-1"] if enhance.bgm_loop else [])
            argv += ["-i", str(Path(bgm_path).resolve())]

        argv += ["-filter_complex", fc, "-map", "[vout]"]
        if a_graph:
            argv += ["-map", a_label]
        else:
            argv += ["-an"]
        argv += EXACT_VIDEO_ARGS
        if a_graph:
            argv += EXACT_AUDIO_ARGS
        argv += FASTSTART_ARGS
        if bgm_path is not None:
            argv += ["-t", f"{dur:.3f}"]
        return argv + [str(out)], cwd

    # ── 分支 2~4：无增强（保持阶段 3 已验证的路径不动）────────
    if ass:
        vf, cwd = subtitle_style.ass_filter_arg(ass, cwd=work_dir)
        argv += ["-vf", vf]
        argv += EXACT_VIDEO_ARGS + EXACT_AUDIO_ARGS
    elif mode == "fast":
        argv += ["-c", "copy"]
    else:
        argv += EXACT_VIDEO_ARGS + EXACT_AUDIO_ARGS

    return argv + FASTSTART_ARGS + [str(out)], cwd


# ─────────────────────────────────────────────────────────────
# 单条导出
# ─────────────────────────────────────────────────────────────
def export_one(src: str | Path, clip: dict, *,
               out_dir: Path, index: int, stem: str,
               mode: str = "exact",
               burn_subtitles: bool = False,
               segments: Iterable | None = None,
               preset: subtitle_style.SubtitlePreset | None = None,
               transport=None, use_llm_split: bool = True,
               bilingual: bool = False,
               translate_transport=None,
               target_lang: str = "en",
               translate_reflection: bool = True,
               width: int = 0, height: int = 0,
               enhance: "enhance_mod.EnhanceOptions | None" = None,
               cover_title: str | None = None,
               cover_style: "enhance_mod.CoverStyle | None" = None,
               on_progress: Callable[[float], None] | None = None,
               should_cancel: CancelCb | None = None) -> ClipExport:
    """导出一条候选：切 + 烧字幕 + 写 SRT。"""
    start_raw, end_raw = clip.get("start"), clip.get("end")
    if start_raw is None or end_raw is None:
        # 区分「数据缺字段」与「时间确实不合理」—— 两者的处理动作完全不同：
        # 前者要重新分析，后者要用户改时间。混在一句里会让人无从下手。
        raise EditorError(
            "这一条缺少起止时间（数据不完整）。\n"
            "多半来自旧版本留下的记录 —— 重新分析一次即可。")
    start = float(start_raw)
    end = float(end_raw)
    if end <= start:
        raise EditorError(
            f"结束时间不晚于开始时间（{start:.1f}s → {end:.1f}s），无法切片。\n"
            "请到「候选清单」页把这一条的起止时间改对。")

    title = (clip.get("title_hint") or clip.get("summary")
             or f"片段{index:02d}")
    stem = safe_filename(stem or title, fallback=f"clip{index:02d}")
    item = ClipExport(clip_id=int(clip.get("id") or 0), index=index,
                      title=title, src_start=start, src_end=end,
                      duration=end - start)

    out_dir = Path(out_dir).resolve()
    src_path = Path(src).resolve()
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)

    # 单独调用本函数时也要保证尺寸正确（export_clips 通常已探好并传入）
    if burn_subtitles and not (width and height):
        width, height = ffmpeg_tools.probe_video_size(src_path)

    # ── 1. 字幕 ────────────────────────────────────────────
    # ⚠️ **有转录就产出 SRT，与"烧不烧字幕"无关。**
    #    界面在"不烧字幕"时明确承诺"会另外导出一个 .srt 字幕文件"，
    #    早先的实现却把它绑在 `burn_subtitles` 上 —— 承诺了却不给，
    #    用户按提示去找文件会发现根本没有。烧字幕决定的是**画面**，
    #    不是"要不要这份字幕交付物"（C 路线三件套之一）。
    segs_list = list(segments) if segments is not None else []
    ass_path: Path | None = None
    if segs_list:
        info = subtitle.build_for_clip(
            segs_list, start, end,
            out_dir=work, width=width or 1080, height=height or 1920,
            preset=preset, transport=transport, use_llm=use_llm_split,
            bilingual=bilingual, translate_transport=translate_transport,
            target_lang=target_lang, translate_reflection=translate_reflection,
            stem=stem)
        if burn_subtitles:
            ass_path = Path(info["ass"])
        item.cues = int(info["cue_count"])
        item.dropped_cues = int(info["dropped_at_boundary"])
        item.translated = int(info.get("translated") or 0)
        # 翻译身上的提示（没 key / 有条目没翻成）要浮上来
        for n in (info.get("translate_notes") or []):
            item.notes.append(str(n))
        item.cost_cny += float(info.get("translate_cost") or 0)
        # SRT 作为交付物留在输出目录（用户可能要拿去别的软件用）
        try:
            shutil.copy2(info["srt"], out_dir / f"{stem}.srt")
            item.srt = str(out_dir / f"{stem}.srt")
        except BaseException:      # SRT 只是附加交付物，拿不到不该中断导出
            pass

    # ── 2. 切片（+ 烧字幕 + 增强）───────────────────────────
    out_video = (out_dir / f"{stem}.mp4").resolve()
    eff = enhance if (enhance is not None and not enhance.is_noop()) else None
    if eff is not None and eff.bgm and enhance_mod.resolve_bgm(eff.bgm) is None:
        # 说了要配乐却找不到文件 —— 必须说出来，不能静默当没配
        item.notes.append(
            f"找不到 BGM「{eff.bgm}」，本条未配乐。"
            f"请把音乐放进 {enhance_mod.bgm_dir()} 后重新导出。")
    argv, cwd = build_cut_cmd(src_path, start, end, out_video, mode=mode,
                              ass=ass_path, work_dir=work, enhance=eff)
    # ⚠️ cwd 必须原样传给 ffmpeg —— 字幕滤镜用的是相对路径
    run_ffmpeg(argv, total_seconds=item.duration, on_progress=on_progress,
               should_cancel=should_cancel, cwd=cwd)

    if not out_video.exists() or out_video.stat().st_size < 1024:
        raise EditorError("导出没有产出有效文件（可能是切点超出片长）")

    item.ok = True
    item.video = str(out_video)

    # ── 3. 封面（可选；失败不该毁掉已完成的成片）──────────────
    if cover_title:
        try:
            info = enhance_mod.make_cover(
                src_path, start, end, cover_title,
                out_dir / f"{stem}_封面.jpg", style=cover_style, stem=stem)
            item.cover = info.get("path", "")
            if info.get("note"):
                item.notes.append(str(info["note"]))
        except Exception as exc:                # noqa: BLE001
            item.notes.append(
                f"封面生成失败（成片不受影响）：{type(exc).__name__}: {exc}")

    return item


# ─────────────────────────────────────────────────────────────
# 批量导出
# ─────────────────────────────────────────────────────────────
def make_out_dir(base: str | Path, task_name: str,
                 stamp: str | None = None) -> Path:
    """为本批导出建一个独立子目录。

    不直接往用户选的目录里倒一堆文件 —— 连续导出几次就分不清哪批是哪批了。
    """
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    name = safe_filename(f"{task_name}_{stamp}", fallback=f"SliceQ_{stamp}")
    out = Path(base) / name
    out.mkdir(parents=True, exist_ok=True)
    return out


def export_clips(src: str | Path, clips: Sequence[dict], *,
                 out_base: str | Path,
                 task_id: int = 0, task_name: str = "任务",
                 mode: str = "exact",
                 burn_subtitles: bool = False,
                 segments: Iterable | None = None,
                 preset: subtitle_style.SubtitlePreset | None = None,
                 transport=None, use_llm_split: bool = True,
                 bilingual: bool = False,
                 translate_transport=None,
                 target_lang: str = "en",
                 translate_reflection: bool = True,
                 width: int = 0, height: int = 0,
                 enhance: "enhance_mod.EnhanceOptions | None" = None,
                 make_covers: bool = False,
                 cover_style: "enhance_mod.CoverStyle | None" = None,
                 progress: ProgressCb | None = None,
                 should_cancel: CancelCb | None = None,
                 mode_note: list[str] | None = None) -> ExportResult:
    """批量导出。

    设计取舍：**单条失败不中断整批**。
    用户勾了 5 条，第 3 条因为源文件某段损坏失败 —— 不应该让后两条也导不出来。
    失败原因逐条记在结果里，最后汇总展示。
    """
    # ⚠️ 两个入口路径都 resolve() —— 因为烧字幕时 ffmpeg 会带 cwd 运行，
    #    相对路径的基准随之改变（源码 / 输出都会指错位置）。
    src_path = Path(src).resolve()
    if not src_path.exists():
        raise EditorError(f"源视频不存在：{src_path}")

    out_dir = make_out_dir(Path(out_base).resolve(), task_name)
    segs = list(segments) if segments is not None else []
    preset = preset or subtitle_style.active_preset()

    # ── 尺寸：ASS 的 PlayRes 必须匹配**实际成片像素尺寸** ──────
    # 不匹配的后果：字号与定位坐标落在错误的画布上 —— 画面是竖的、
    # 字幕却按横的排版，且不报任何错。所以这里自己探测，
    # 不接受"调用方猜一个"（实测踩过：按源片标称尺寸传 720x1280，
    # 而实际素材是 406x720，字幕全走样）。
    if burn_subtitles and not (width and height):
        width, height = ffmpeg_tools.probe_video_size(src_path)
        if not (width and height):
            if mode_note is not None:
                mode_note.append(
                    "⚠️ 无法探测源视频尺寸，字幕字号与位置可能不准确。")
            width, height = 1080, 1920

    # 烧字幕必须重编码：用户选了快速模式就自动升档，并**如实记下来**
    eff_mode = mode
    if burn_subtitles and mode == "fast":
        eff_mode = "exact"
        if mode_note is not None:
            mode_note.append(
                "烧字幕需要重新编码像素，已从「快速」自动切换到「精确」模式。")

    result = ExportResult(out_dir=out_dir, mode=eff_mode,
                          subtitles_burned=bool(burn_subtitles and segs))
    if enhance is not None and not enhance.is_noop():
        if eff_mode == "fast":
            # 滤镜加不进 -c copy，必须升档；且要如实告知
            eff_mode = "exact"
            result.mode = "exact"
            if mode_note is not None:
                mode_note.append(
                    "启用增强后无法使用「快速」模式（流复制加不了滤镜），已切换为「精确」模式。")
        bits = []
        if enhance.fade_in or enhance.fade_out:
            bits.append(f"淡入 {enhance.fade_in:g}s / 淡出 {enhance.fade_out:g}s")
        if enhance.bgm:
            bgm_ok = enhance_mod.resolve_bgm(enhance.bgm)
            bits.append(f"配乐「{enhance.bgm}」"
                        + (f"（{enhance.bgm_volume_db:g}dB"
                           + ("，人声触发压低" if enhance.duck else "")
                           + "）" if bgm_ok else "（**未找到文件**）"))
        result.enhance_summary = "；".join(bits)
    t0 = time.time()
    total = len(clips)

    for i, clip in enumerate(clips, 1):
        if should_cancel and should_cancel():
            result.cancelled = True
            break

        title = (clip.get("title_hint") or clip.get("summary") or f"片段{i:02d}")
        stem = f"{i:02d}_{safe_filename(title, fallback='clip')}"

        if progress:
            progress((i - 1) / max(total, 1),
                     f"导出 {i}/{total}：{title[:20]}", i - 1)

        # 条内进度映射到整批进度区间
        # ⚠️ i 和 title 都要绑成默认参数固定住 —— 闭包会捕获变量本身，
        #    不固定的话下一轮循环改了 i/title，这里跟着变。
        def _inner(ratio: float, _i: int = i, _t: str = title) -> None:
            if progress:
                progress((_i - 1 + ratio) / max(total, 1),
                         f"导出 {_i}/{total}：{_t[:20]}", _i - 1)

        try:
            item = export_one(
                src_path, clip, out_dir=out_dir, index=i, stem=stem,
                mode=eff_mode, burn_subtitles=burn_subtitles,
                segments=segs, preset=preset, transport=transport,
                use_llm_split=use_llm_split, bilingual=bilingual,
                translate_transport=translate_transport,
                target_lang=target_lang,
                translate_reflection=translate_reflection,
                width=width, height=height,
                enhance=enhance, cover_title=title if make_covers else None,
                cover_style=cover_style,
                on_progress=_inner, should_cancel=should_cancel)
        except EditorError as exc:
            msg = str(exc)
            if msg == "已取消":
                result.cancelled = True
                break
            item = ClipExport(clip_id=int(clip.get("id") or 0), index=i,
                              title=title, src_start=float(clip.get("start") or 0),
                              src_end=float(clip.get("end") or 0), error=msg)
        except Exception as exc:                      # 兜底，避免整批崩掉
            item = ClipExport(clip_id=int(clip.get("id") or 0), index=i,
                              title=title, error=f"{type(exc).__name__}: {exc}")

        result.items.append(item)

        # 成功的落库 —— 失败的没有产出文件，记了也没用
        if item.ok and task_id:
            try:
                store.add_export(task_id, "clip_mp4", item.video)
                if item.srt:
                    store.add_export(task_id, "clip_srt", item.srt)
                if item.cover:
                    store.add_export(task_id, "clip_cover", item.cover)
            except BaseException:                  # noqa: BLE001
                pass

    # 工作目录（中间 ASS 等）清掉，不留垃圾给用户
    # 中间产物清理：删不掉就算了（某些受管环境会拦截删除，
    # 而且拦下来抛的是 SystemExit 而非 OSError —— 只捕 Exception 会穿透）
    try:
        shutil.rmtree(out_dir / "_work", ignore_errors=True)
    except BaseException:                          # noqa: BLE001
        pass

    _write_manifest(result, src_path, task_name,
                    mode_note=mode_note or [])

    result.elapsed = time.time() - t0
    if progress:
        progress(1.0, f"导出完成：{result.summary()}", len(result.items))
    return result


def _write_manifest(result: ExportResult, src: Path, task_name: str,
                    *, mode_note: list[str] | None = None) -> None:
    """写一份导出清单。

    为什么每个导出目录都要有：用户过几天回来只会看到一堆
    `01_xxx.mp4`，想不起来是从哪个原片的哪些时间点切的。
    这份清单让导出结果**自解释**。
    """
    lines = [
        "SliceQ 导出清单",
        "=" * 46,
        f"任务　　：{task_name}",
        f"源视频　：{src}",
        f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"模式　　：{'精确（重编码，时长精确）' if result.mode == 'exact' else '快速（流复制，切点有 0.1~0.15s 偏差）'}",
        f"烧字幕　：{'是' if result.subtitles_burned else '否'}",
        f"增强　　：{result.enhance_summary or '无'}",
        f"结果　　：{result.summary()}",
        "",
    ]
    for n in (mode_note or []):
        lines.append(f"提示　　：{n}")
    if mode_note:
        lines.append("")

    lines += [
        "文件列表",
        "-" * 46,
    ]
    for it in result.items:
        if it.ok:
            lines.append(
                f"[成功] {Path(it.video).name}")
            lines.append(
                f"       源 {it.src_start:.2f}s ~ {it.src_end:.2f}s"
                f"（{it.duration:.1f} 秒）｜字幕 {it.cues} 条"
                + (f"｜边界丢弃 {it.dropped_cues} 条" if it.dropped_cues else ""))
            if it.cover:
                lines.append(f"       封面：{Path(it.cover).name}")
            for n in it.notes:
                lines.append(f"       ⚠️ {n}")
        else:
            lines.append(f"[失败] {it.title}")
            lines.append(f"       原因：{it.error.splitlines()[0] if it.error else '未知'}")
            for n in it.notes:
                lines.append(f"       ⚠️ {n}")

    lines += [
        "",
        "⚠️ 请保留原始素材。本清单里的时间点都指向源视频，",
        "   原片被移动或删除后，无法再按这些时间点重新导出。",
    ]
    try:
        (result.out_dir / "导出清单.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
    except BaseException:                          # noqa: BLE001
        pass
