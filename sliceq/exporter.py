# -*- coding: utf-8 -*-
"""三端草稿导出（阶段 5）：剪映草稿 / 达芬奇 OTIO / 降级包。

## 产出形态

    剪映草稿   %LOCALAPPDATA%/JianyingPro/User Data/Projects/com.lveditor.draft/<名字>/
                 视频轨（引用源片 + 源内裁剪）+ 字幕轨 + 标题轨
    达芬奇     <名字>.otio（甲方案：引用源片 + source_range）
                 + <名字>.srt（字幕并行交付）+ 导入说明.txt
    降级包     每条候选一个 mp4 + SRT + 导入说明.txt
                 （剪映通道不可用或失败时自动退回）

## ★ 字幕时间轴必须重映射（本模块最容易错、且错了不报错的地方）

剪映与达芬奇都是「**多条高光拼成一条时间线**」，与阶段 3 的「乙方案」不同 ——
乙方案是每条候选一个独立 mp4，所以那时时间轴只需一次减法
（`subtitle.remap_to_clip`）。这里是**跨段累加**：

    timeline_t = source_t - highlights[i].start + Σ(highlights[0..i-1].duration)

用 `subtitle.remap_to_timeline()`。**漏做不会报错**，只是字幕整体错位
（SRT 依然合法、剪映照常接受）—— 属"功能正常、结果全错"类缺陷。

## ⚠️ 字幕**样式**不跟随导出（必须在 UI 明说）

阶段 3/4 的字幕样式系统（字体 / 白字黑边 / 双语排版）在这两条通道里**都不生效**：

    · 剪映  —— `import_srt` 用剪映自己的默认字幕样式
    · 达芬奇 —— OTIO **根本不承载字幕**（OTIO 0.18.1 无 Text 轨类型，R19），
                字幕靠并行的 SRT 单独导入

## 实测约束（都踩过，勿改）

1. **片段时长必须夹进素材真实时长** —— 超界会让剪映的 `VideoSegment`
   直接抛 `ValueError`（实测：选到 9000s 而素材只有 8958.9s）。
2. **`ExternalReference.available_range` 要写素材真实总时长** ——
   不是 0、也不是片段时长；它表示"这个媒体文件总共有多长"，
   达芬奇据此判断引用是否有效。
3. **`draft_meta_info.json` 的 `draft_name`/`draft_fold_path`/`draft_root_path`
   默认是空串**，必须手动补。
4. **草稿名不要重复加前缀**（第一版拼出 `SliceQ_SliceQ_阶段5实测`）。
5. **中文草稿名可用**，无需转拼音；剪映只扫自己的草稿目录，不扫任意路径。
6. **不用 `allow_replace=True`** —— 它会静默覆盖用户的既有草稿，
   把用户在剪映里精修过的版本抹掉。改用 `_unique_name()` 自动避让。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import config, ffmpeg_tools, subtitle, subtitle_style

ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]


class ExporterError(Exception):
    """导出失败（消息面向用户，中文）。"""


# ─────────────────────────────────────────────────────────────
# 数据对象
# ─────────────────────────────────────────────────────────────
@dataclass
class Highlight:
    """时间线上的一段。`title` 会进剪映标题轨与 OTIO 的 Clip.name。"""

    start: float
    end: float
    title: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class MediaInfo:
    width: int = 0
    height: int = 0
    fps: float = config.DEFAULT_FPS
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.width > 0 and self.height > 0


@dataclass
class ChannelResult:
    """一个通道的产出。"""

    name: str = ""
    ok: bool = False
    out_dir: Path | None = None
    files: list[str] = field(default_factory=list)
    message: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok,
                "out_dir": str(self.out_dir) if self.out_dir else "",
                "files": list(self.files), "message": self.message,
                "error": self.error}


@dataclass
class DraftResult:
    ok: bool = False
    channels: dict[str, ChannelResult] = field(default_factory=dict)
    out_dir: Path | None = None
    highlights: list[Highlight] = field(default_factory=list)
    cue_count: int = 0
    dropped_cues: int = 0
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def ok_channels(self) -> list[str]:
        return [k for k, v in self.channels.items() if v.ok]


# ─────────────────────────────────────────────────────────────
# 素材探测
# ─────────────────────────────────────────────────────────────
def probe_media(media: str | Path) -> MediaInfo:
    """探测像素尺寸 / 帧率 / 总时长。

    尺寸复用 `ffmpeg_tools.probe_video_size()`（它**已处理旋转元数据** ——
    用编码尺寸去设画布会让竖屏素材的字幕按横屏排版）。

    fps 与时长单独取一次。两者都要：
      · fps   → OTIO 的 rate 与剪映草稿的 fps
      · 时长  → 夹取片段（超界会让剪映抛 ValueError）
    """
    media = Path(media)
    info = MediaInfo()
    try:
        info.width, info.height = ffmpeg_tools.probe_video_size(media)
    except Exception:                                       # noqa: BLE001
        pass

    ffprobe = ffmpeg_tools.find_ffprobe()
    if not ffprobe:
        return info
    try:
        r = subprocess.run(
            [str(ffprobe), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-show_entries", "format=duration",
             "-of", "json", str(media)],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
        data = json.loads(r.stdout or "{}")
        streams = data.get("streams") or []
        if streams:
            rate = str(streams[0].get("r_frame_rate") or "")
            if "/" in rate:
                num, _, den = rate.partition("/")
                if float(den or 0) > 0:
                    info.fps = round(float(num) / float(den), 3) or info.fps
        dur = (data.get("format") or {}).get("duration")
        if dur:
            info.duration = float(dur)
    except Exception:                                       # noqa: BLE001
        pass
    return info


# ─────────────────────────────────────────────────────────────
# 剪映环境
# ─────────────────────────────────────────────────────────────
def jianying_draft_root() -> Path:
    return Path(config.JIANYING_DRAFT_ROOT)


def detect_jianying_version() -> tuple[str, bool]:
    """返回 (版本号, 是否在已验证列表内)。

    读 `Apps/` 下的版本目录名（阶段 0 定的方法，不依赖注册表 ——
    受管机器上 `reg.exe` 会被安全策略阻止）。
    """
    apps = Path(config.JIANYING_APPS_DIR)
    if not apps.is_dir():
        return "", False
    versions: list[str] = []
    for d in apps.iterdir():
        if d.is_dir() and re.fullmatch(r"\d+(\.\d+){1,3}", d.name or ""):
            versions.append(d.name)
    if not versions:
        return "", False

    def key(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split(".") if x.isdigit())

    newest = max(versions, key=key)
    verified = newest in tuple(config.JIANYING_VERIFIED_VERSIONS)
    if not verified:
        # 也接受"同主版本+次版本"（例如 11.5.3.14501 的补丁号变了）
        stem = ".".join(newest.split(".")[:3])
        verified = any(v.startswith(stem) for v in config.JIANYING_VERIFIED_VERSIONS)
    return newest, verified


def jianying_ready() -> tuple[bool, str]:
    """剪映通道是否可用，返回 (可用, 原因)。"""
    try:
        import pyJianYingDraft                                  # noqa: F401
    except ImportError:
        return False, ("未安装 pyJianYingDraft，无法生成剪映草稿。\n"
                       "→ 执行 `pip install pyJianYingDraft` 后重试。")
    root = jianying_draft_root()
    if not root.parent.exists():
        return False, (f"没有找到剪映的数据目录：\n  {root}\n"
                       f"→ 请确认已安装剪映并至少打开过一次。")
    return True, ""


# ─────────────────────────────────────────────────────────────
# 高光规划 / 唯一命名
# ─────────────────────────────────────────────────────────────
def plan_highlights(clips: Sequence[dict], *,
                    media_duration: float = 0.0) -> list[Highlight]:
    """把候选片段规范成时间线片段（含时长夹取）。

    ⚠️ **必须夹进素材真实时长**。超界会让剪映的 `VideoSegment` 抛
       `ValueError`（实测：挑到 9000s 而素材只有 8958.9s）。
       好消息是它抛错而不是静默出错，但不该让用户碰到。
    """
    out: list[Highlight] = []
    for i, c in enumerate(clips or [], 1):
        try:
            a = float(c.get("start") or 0.0)
            b = float(c.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        if b <= a:
            continue
        if media_duration > 0:
            a = max(0.0, min(a, media_duration))
            b = max(0.0, min(b, media_duration))
            if b <= a:
                continue
        title = str(c.get("title_hint") or c.get("title") or "").strip()
        if not title:
            title = f"片段 {i}"
        out.append(Highlight(start=round(a, 3), end=round(b, 3), title=title))
    return out


def _unique_name(root: Path, base: str, *, limit: int = 99) -> str:
    """在 `root` 下找一个不与既有目录冲突的名字。

    ⚠️ **不用 `allow_replace=True`**：它会静默覆盖同名草稿 ——
       用户在剪映里精修过的版本会凭空消失，而且没有任何提示。
       （前置实测报告 §5.3 记的就是这个遗留问题。）
    """
    safe = re.sub(r'[\\/:*?"<>|]+', "_", (base or "SliceQ草稿").strip()) or "SliceQ草稿"
    safe = safe.strip(". ") or "SliceQ草稿"
    if not (root / safe).exists():
        return safe
    for i in range(2, limit + 1):
        cand = f"{safe} ({i})"
        if not (root / cand).exists():
            return cand
    raise ExporterError(f"草稿目录下同名项太多，无法为「{safe}」找到可用名字。")


# ─────────────────────────────────────────────────────────────
# 字幕（重映射）
# ─────────────────────────────────────────────────────────────
def build_timeline_subtitles(segments: Iterable | None,
                             highlights: Sequence[Highlight],
                             *, out_path: Path,
                             transport=None, use_llm: bool = True,
                             bilingual: bool = False,
                             progress: ProgressCb | None = None) -> dict:
    """断句 → **跨段重映射** → 写 SRT。返回统计。

    这是全模块唯一一处把源时间轴字幕搬到时间线时间轴的地方 ——
    剪映的字幕轨、达芬奇并行的 SRT、降级包的 SRT 全都用它。
    **不要在这里另写一份映射。**
    """
    segs = list(segments or [])
    if not segs:
        return {"srt": "", "cue_count": 0, "dropped": 0, "source_cues": 0}

    all_cues, stats = subtitle.split_segments(
        segs, transport=transport, use_llm=use_llm, progress=progress)
    spells = [(h.start, h.end) for h in highlights]
    # ★ 唯一的映射调用点（甲方案 · 跨段累加）
    cues_tl, dropped = subtitle.remap_to_timeline(all_cues, spells)

    srt = subtitle.write_srt(cues_tl, out_path, bilingual=bilingual)
    return {"srt": str(srt), "cue_count": len(cues_tl), "dropped": dropped,
            "source_cues": len(all_cues), **stats}


# ─────────────────────────────────────────────────────────────
# 通道 1：剪映草稿
# ─────────────────────────────────────────────────────────────
def export_jianying(video: str | Path, highlights: Sequence[Highlight], *,
                    media: MediaInfo,
                    timeline_srt: Path | None = None,
                    draft_root: str | Path | None = None,
                    name: str = "",
                    work_dir: str | Path | None = None,
                    make_titles: bool = True) -> ChannelResult:
    """生成剪映草稿并部署到剪映的草稿目录。"""
    res = ChannelResult(name="剪映草稿")
    ok, why = jianying_ready()
    if not ok:
        res.error = why
        return res

    try:
        import pyJianYingDraft as draft
        from pyJianYingDraft import trange
    except ImportError as exc:                              # noqa: BLE001
        res.error = f"导入 pyJianYingDraft 失败：{exc}"
        return res

    video = Path(video).resolve()
    root = Path(draft_root) if draft_root else jianying_draft_root()
    root.mkdir(parents=True, exist_ok=True)
    draft_name = _unique_name(root, name or video.stem)

    stage = (Path(work_dir) if work_dir else config.APP_ROOT / "work" / video.stem)
    stage = stage / "jianying_build"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)

    try:
        folder = draft.DraftFolder(str(stage))
        # allow_replace=False：**不覆盖**同名草稿（名字已由 _unique_name 保证唯一）
        script = folder.create_draft(
            draft_name, width=int(media.width or 1080),
            height=int(media.height or 1920),
            fps=int(round(media.fps or 30)), allow_replace=False)

        # ── 视频主轨：引用源片 + 源内裁剪 ──────────────
        vtrack = script.append_track(
            draft.TrackSpec(draft.TrackType.video, name="SliceQ_主轨"))
        material = draft.VideoMaterial(str(video))
        cursor = 0.0
        for h in highlights:
            seg = draft.VideoSegment(
                material,
                target_timerange=trange(f"{cursor}s", f"{h.duration}s"),
                source_timerange=trange(f"{h.start}s", f"{h.duration}s"))
            script.add_segment(seg, vtrack)
            cursor += h.duration
        res.files.append(f"视频轨 {len(highlights)} 段")

        # ── 字幕轨：用**重映射后**的 SRT ───────────────
        #    这一步传错（拿源时间轴的 SRT）不会报错，只是字幕整体错位。
        if timeline_srt and Path(timeline_srt).exists():
            try:
                script.import_srt(str(timeline_srt), track_name="SliceQ_字幕",
                                  time_offset=0.0)
                res.files.append("字幕轨已导入")
            except Exception as exc:                        # noqa: BLE001
                res.message += f"（字幕轨导入失败：{exc}）"

        # ── 标题轨 ────────────────────────────────────
        if make_titles and highlights:
            ttrack = script.append_track(
                draft.TrackSpec(draft.TrackType.text, name="SliceQ_标题"))
            cursor = 0.0
            for i, h in enumerate(highlights, 1):
                title = draft.TextSegment(
                    f"{i}. {h.title}",
                    trange(f"{cursor}s", f"{min(4.0, h.duration)}s"),
                    clip_settings=draft.ClipSettings(transform_y=-0.75))
                script.add_segment(title, ttrack)
                cursor += h.duration
            res.files.append(f"标题轨 {len(highlights)} 条")

        script.save()
    except Exception as exc:                                # noqa: BLE001
        res.error = f"生成剪映草稿失败：{type(exc).__name__}: {exc}"
        return res

    # ── 部署到剪映草稿目录 ────────────────────────────
    built = stage / draft_name
    if not built.exists():
        res.error = f"草稿构建产物不存在：{built}"
        return res
    dest = root / draft_name
    if dest.exists():                                       # 理论上不会，名字已避让
        shutil.rmtree(dest, ignore_errors=True)
    try:
        shutil.copytree(built, dest)
    except Exception as exc:                                # noqa: BLE001
        res.error = f"写入剪映草稿目录失败：{exc}"
        return res

    # ── 补齐 meta 里默认为空的三个字段（实测坑）────────
    meta_p = dest / "draft_meta_info.json"
    if meta_p.exists():
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            meta["draft_name"] = draft_name
            meta["draft_fold_path"] = str(dest)
            meta["draft_root_path"] = str(root)
            meta_p.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as exc:                            # noqa: BLE001
            res.message += f"（draft_meta_info 字段补齐失败：{exc}）"

    res.ok = True
    res.out_dir = dest
    res.message = (f"草稿「{draft_name}」已写入剪映草稿目录。"
                   f"打开剪映首页即可看到。")
    return res


# ─────────────────────────────────────────────────────────────
# 通道 2：达芬奇 OTIO
# ─────────────────────────────────────────────────────────────
def export_davinci(video: str | Path, highlights: Sequence[Highlight], *,
                   out_dir: str | Path, media: MediaInfo,
                   timeline_srt: Path | None = None,
                   name: str = "",
                   with_guide: bool = True) -> ChannelResult:
    """生成 OTIO（甲方案）+ 并行 SRT + 导入说明.txt。"""
    res = ChannelResult(name="达芬奇 OTIO")
    try:
        import opentimelineio as otio
    except ImportError:
        res.error = ("未安装 opentimelineio，无法生成 OTIO。\n"
                     "→ 执行 `pip install opentimelineio` 后重试。")
        return res

    video = Path(video).resolve()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = _safe_stem(name or video.stem)

    rate = int(round(media.fps or 30)) or 30
    # ⚠️ 素材真实总时长（不是 0、也不是片段时长）
    total = media.duration if media.duration > 0 else max(
        (h.end for h in highlights), default=0.0)

    try:
        timeline = otio.schema.Timeline(name=base)
        # ★ 必须写 0 —— 否则达芬奇会把时间线起点当成 01:00:00:00
        #   ⚠️ 2026-09-27 实测补充：该字段**只在 OTIO 的 rate 与达芬奇项目帧率一致时才生效**。
        #      项目帧率不一致时达芬奇回退到项目自己的起始时间码（默认 01:00:00:00）。
        #      ⇒ 仍然要写，但**不能只靠它**，`导入说明.txt` 里的手动设置步骤是必需的。
        timeline.global_start_time = otio.opentime.RationalTime(0, rate)

        track_v = otio.schema.Track(name="SliceQ_主轨",
                                    kind=otio.schema.TrackKind.Video)
        # ★★ 音频轨：**必须有**（2026-09-27 真机验收发现的缺陷）
        #   只写视频轨时，达芬奇会建一条**空的**音频轨 —— 时间线上有画面、没声音。
        #   实测证据：项目库 `Sm2TiTrack` 有 2 行（视频轨 + 音频轨），
        #   但 `Sm2TiItem` 的 3 个片段全部挂在视频轨上，音频轨 0 个片段。
        #   达芬奇**不会**从 OTIO 的视频片段里自动推断出音频。
        track_a = otio.schema.Track(name="SliceQ_主音轨",
                                    kind=otio.schema.TrackKind.Audio)

        src_url = "file:///" + str(video).replace("\\", "/")
        cursor = otio.opentime.RationalTime(0, rate)
        for i, h in enumerate(highlights, 1):
            # ⚠️ 变量名不要叫 `dur` —— 外层有同名 float，会被静默覆盖成
            #    RationalTime，第二次迭代就抛"类型不支持"（看不出是覆盖）
            seg_dur = otio.opentime.RationalTime(h.duration * rate, rate)
            # 视频/音频两条轨各建**独立**的 Clip 与 ExternalReference：
            # 同一个 Clip 对象 append 到两条轨会互相覆盖 `range_in_parent`，
            # 表现为其中一条轨的片段位置错乱（且不报错）。
            for tr in (track_v, track_a):
                ref = otio.schema.ExternalReference(
                    target_url=src_url,
                    # ★ 必须显式设，且是**素材真实总时长**
                    available_range=otio.opentime.TimeRange(
                        start_time=otio.opentime.RationalTime(0, rate),
                        duration=otio.opentime.RationalTime(total * rate, rate)))
                clip = otio.schema.Clip(
                    name=f"{i}_{h.title}",
                    media_reference=ref,
                    source_range=otio.opentime.TimeRange(
                        start_time=otio.opentime.RationalTime(h.start * rate, rate),
                        duration=seg_dur))
                clip.range_in_parent = otio.opentime.TimeRange(
                    start_time=cursor, duration=seg_dur)
                tr.append(clip)
            cursor = cursor + seg_dur

        timeline.tracks.append(track_v)
        timeline.tracks.append(track_a)
        otio_path = out / f"{base}.otio"
        otio.adapters.write_to_file(timeline, str(otio_path))
    except Exception as exc:                                # noqa: BLE001
        res.error = f"生成 OTIO 失败：{type(exc).__name__}: {exc}"
        return res

    res.files.append(otio_path.name)

    # 字幕并行交付（OTIO 不承载字幕）
    # ★ 走 write_davinci_srt：必要时插一条「哨兵」占位条，
    #   否则达芬奇会把首条对齐到时间线第 0 帧、丢掉 0.543s 那种首条偏移。
    #
    # ★★ 2026-09-28 起**产出两份**，因为达芬奇的时间码基准是个"二选一"：
    #    达芬奇**新建时间线**的起始时间码默认是 `01:00:00:00`
    #    （实测：项目库 `Sm2Sequence.MediaExtents=[3600.0, 时长]`，三个新建时间线全都如此），
    #    而我们的 SRT 是零基的。两者不一致时，整份 SRT 会落在时间线起点**之前**
    #    ⇒ 达芬奇**静默忽略整个插入操作**（点了没反应、不报错）——
    #    用户为此连试了五轮。让用户去改设置不如我们直接适配：
    #      ① `xxx.srt`                    零基（标准件；起始时间码 = 0 的项目用）
    #      ② `xxx_达芬奇默认时间码.srt`    整份 +1h（达芬奇默认项目**开箱即用**）
    srt_note = ""
    if timeline_srt and Path(timeline_srt).exists():
        try:
            dst0 = out / f"{base}.srt"
            write_davinci_srt(timeline_srt, dst0, offset=0.0)
            res.files.append(dst0.name)

            dst1 = out / f"{base}_达芬奇默认时间码.srt"
            write_davinci_srt(timeline_srt, dst1,
                              offset=DAVINCI_DEFAULT_OFFSET)
            res.files.append(dst1.name)
            srt_note = "；字幕 SRT 两份（零基 + 达芬奇默认）"
        except Exception:                                   # noqa: BLE001
            srt_note = "；字幕 SRT 生成失败"

    if with_guide:
        try:
            guide = _write_guide(out / "导入说明.txt", video=video,
                                 base=base, highlights=highlights,
                                 draft_dir=None,
                                 jianying_name="",
                                 fps=media.fps)
            res.files.append(guide.name)
        except Exception as exc:                            # noqa: BLE001
            res.message += f"（导入说明生成失败：{exc}）"

    res.ok = True
    res.out_dir = out
    res.message = (f"{base}.otio（{len(highlights)} 段，"
                   f"时间线 {cursor.to_seconds():.1f}s）{srt_note}")
    return res


def _safe_stem(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip())
    return s.strip(". ") or "SliceQ时间线"


# ─────────────────────────────────────────────────────────────
# 达芬奇 SRT 的「哨兵」占位
# ─────────────────────────────────────────────────────────────
# ⚠️ 为什么需要它（2026-09-28 真机实测，四轮排查才定位）：
#
#   达芬奇导入外部 SRT 时 —— **无论用"拖到字幕轨"还是菜单
#   「将所选字幕插入到… > 使用时间码的时间线」** —— 行为都一样：
#     把 SRT 的**第一条对齐到时间线第 0 帧**，其余条按**相对间隔**排列。
#     ⇒ **第一条字幕自身的起始偏移被丢掉。**
#
#   实测数据（3 段 / 135s，SRT 首条 0.543s，项目 24fps）：
#     原 SRT Start:  0.543, 5.608, 9.448, 13.903
#     达芬奇落位:    0,     122,   214,   321      （帧）
#     应有值:        13,    135,   227,   334
#     ⇒ **全表恒定提前 13 帧（0.543s）**，且**不报错**。
#
#   已排除的手段（都无效）：改项目起始时间码、用「使用时间码的时间线」、
#   找字幕素材的"片段属性 → 时间码"（根本没这个菜单）。
#
#   解法：在真首条**之前**插一条**内容为空、起始为 0** 的极短字幕。
#   达芬奇会把这条"哨兵"对齐到第 0 帧，真首条便被推回正确位置：
#     哨兵 0.000 → 落位 0；真首条 0.543 → 落位 0.543×24 = 13 ✓
#
# ⚠️ 只在**达芬奇通道**这样做。剪映草稿与降级包仍用原样 SRT ——
#    「字幕时间轴映射只有一处调用点」（硬约束 #23）没有被破坏：
#    这里是在**交付层**追加一条占位，**不重新做任何时间轴映射**。
_SENTINEL_TEXT = " "        # 单个空格：SRT 要求每条至少有文本行，空串可能被判非法

# 达芬奇**新建时间线**的默认起始时间码 = 01:00:00:00（实测：项目库里
# `Sm2Sequence.MediaExtents = [3600.0, 时长]`，三个新建时间线全都是 3600）。
# 我们的 SRT 是零基的，直接导入会**整份落在时间线起点之前一小时** ⇒
# 达芬奇静默忽略整个插入操作（点了没反应，也不报错）。
DAVINCI_DEFAULT_OFFSET = 3600.0

_TC_RANGE_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})")


def _fmt_tc(seconds: float) -> str:
    """秒 → SRT 时间码 `HH:MM:SS,mmm`（自动进位，不为负）。"""
    total = max(0.0, seconds)
    h = int(total // 3600)
    m = int(total % 3600 // 60)
    s = int(total % 60)
    ms = int(round((total - int(total)) * 1000))
    if ms >= 1000:                       # 四舍五入可能顶到 1000
        ms -= 1000
        s += 1
        if s >= 60:
            s -= 60
            m += 1
        if m >= 60:
            m -= 60
            h += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _shift_tc_line(line: str, offset: float) -> str:
    """把一行里的 `开始 --> 结束` 时间码整体平移 offset 秒。"""
    m = _TC_RANGE_RE.search(line)
    if not m:
        return line

    def to_sec(tc: str) -> float:
        h, mi, rest = tc.split(":")
        ss, mss = rest.split(",")
        return int(h) * 3600 + int(mi) * 60 + int(ss) + int(mss) / 1000.0

    new = f"{_fmt_tc(to_sec(m.group(1)) + offset)} --> " \
          f"{_fmt_tc(to_sec(m.group(2)) + offset)}"
    return line[:m.start()] + new + line[m.end():]


def write_davinci_srt(src: Path | str, dst: Path | str,
                      offset: float = 0.0) -> tuple[bool, str]:
    """把 SRT 交付给达芬奇；必要时在最前面插一条「哨兵」占位字幕。

    `offset` —— 整份时间码平移的秒数。**必须与达芬奇该项目/时间线的
        「起始时间码」一致**，否则整份落在范围外，达芬奇会静默忽略插入：
          · 达芬奇**新建**时间线（默认）   → `offset=3600`（01:00:00:00）
          · 用户已把起始时间码改成 0        → `offset=0`

    返回 `(是否插入哨兵, 说明文本)`，**永不抛异常**（失败时退化为原样复制）。
    """
    src, dst = Path(src), Path(dst)
    try:
        text = src.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return False, f"读不到源 SRT：{exc}"

    # 沿用源文件的行尾风格 —— 别让 Path.write_text 在 Windows 上擅自转成 CRLF
    # （转了就与源文件不一致；某些软件的 SRT 解析对行尾敏感）
    nl = "\r\n" if "\r\n" in text else "\n"

    blocks = _srt_blocks(text)
    if not blocks:
        dst.write_text(text, encoding="utf-8", newline="")
        return False, "源 SRT 为空，原样复制"

    first = _srt_first_start_sec(text)
    need_sentinel = first is not None and first > 0.0005

    out: list[list[str]] = []
    if need_sentinel:
        # 哨兵占住「时间线第一帧」：达芬奇会把 SRT 的第一条对齐到时间线起点，
        # 让哨兵去顶，真首条才能保住它自己的起始偏移。
        out.append(["1", f"{_fmt_tc(offset)} --> {_fmt_tc(offset + 0.001)}",
                    _SENTINEL_TEXT])

    start_no = 2 if need_sentinel else 1
    for i, blk in enumerate(blocks, start=start_no):
        body = list(blk)
        # block[0] 是序号行 —— 丢掉它并按顺序重排，免得源文件编号不连续
        if body and re.fullmatch(r"\s*\d+\s*", body[0]):
            body = body[1:]
        if offset:
            body = [_shift_tc_line(ln, offset) for ln in body]
        out.append([str(i), *body])

    # ⚠️ 块之间是**空行**（两个换行）—— SRT 靠空行分块，不能只用一个换行
    dst.write_text((nl + nl).join(nl.join(b) for b in out) + nl,
                   encoding="utf-8", newline="")
    if not need_sentinel and not offset:
        return False, "首条已从 0 开始，无需哨兵"
    why = (f"已插入哨兵占位条（源首条 {first:.3f}s）" if need_sentinel
           else "无需哨兵")
    if offset:
        why += f"；时间码整体 +{offset:.0f}s"
    return need_sentinel, why


def _srt_blocks(text: str) -> list[list[str]]:
    norm = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return [b.split("\n") for b in re.split(r"\n\s*\n", norm) if b.strip()]


def _srt_first_start_sec(text: str) -> float | None:
    m = re.search(r"(\d+):(\d+):(\d+)[,.．](\d+)\s*-->", text)
    if not m:
        return None
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


def _write_guide(path: Path, *, video: Path, base: str,
                 highlights: Sequence[Highlight],
                 draft_dir: Path | None,
                 jianying_name: str,
                 fps: float = 0.0) -> Path:
    """写 `导入说明.txt`。

    ⚠️ 这一步**不能省**：`.otio` 只记录切点、不携带视频本身，
       达芬奇首次导入**必然**弹「找不到片段」。不告诉用户该点什么，
       他会以为文件坏了。

    ⚠️ 2026-09-27 真机验收补入两条**用户实测踩到**的说明：
       ① 时间线起始时间码 / 项目帧率的关系（R20）
       ② SRT 必须"按时间码插入"，直接拖会让字幕整体提前约 0.5s
    """
    fps_txt = f"{fps:g}" if fps else "（与素材一致）"
    lines = [
        "SliceQ 导出说明",
        "=" * 60,
        "",
        "★★★ 先读这一段：字幕插不插得进去，只看一件事 —— 时间码基准 ★★★",
        "",
        "达芬奇**新建时间线**的起始时间码默认是 01:00:00:00，",
        "而 SRT 的时间码是**从 0 开始**的。两者对不上时，整份 SRT 会落在",
        "时间线起点**之前**，达芬奇就会**静默忽略整个插入** ——",
        "表现为「点了没反应，也不报错」。",
        "",
        "所以本目录给了**两份** SRT，按你的情况挑一份：",
        "",
        "   你的时间线起始时间码        用哪一份",
        "   ────────────────────────  ─────────────────────────────────",
        f"   01:00:00:00（达芬奇默认）   ▸ {base}_达芬奇默认时间码.srt",
        f"   00:00:00:00（被你改成了0）  ▸ {base}.srt",
        "",
        "查起始时间码：在**媒体池**里**右键那条时间线** →「时间线设置」→「起始时间码」。",
        "",
        "⚠️⚠️ **每导入一次 .otio，达芬奇都会新建一条时间线，起始时间码会",
        "     回到默认的 01:00:00:00。** 你上次改成 0 的那条**不会**自动带过来，",
        "     换了新时间线必须重新确认一次。",
        "",
        "想一劳永逸：进「偏好设置」找**新建时间线**的默认起始时间码，改成",
        "     00:00:00:00（设置项在不同版本里位置/叫法有出入，找不到就按上面照做）。",
        "✅ 改起始时间码**只改时间码显示，不动画面内容**，可放心改。",
        "",
        "【达芬奇】导入 " + base + ".otio",
        "  1. 菜单 File > Import > Timeline...，选这个 .otio 文件",
        "  2. **首次导入会弹「找不到片段」或提示媒体离线，这是正常现象** ——",
        "     .otio 只记录切点，不携带视频本身。",
        "     弹窗问是否定位媒体时选【是】，然后选择下面这个文件夹：",
        f"         {video.parent}",
        "  3. 导入后时间线上应能看到**画面和声音**。",
        "     ⚠️ 不要移动或重命名源视频，否则链接会断。",
        "",
        f"  补充：本素材帧率 = {fps_txt} fps，而达芬奇新建项目默认 24fps。",
        "  帧率不一致**不影响**片段位置与时长（达芬奇会自动换算）。",
        "",
    ]
    if jianying_name:
        lines += [
            f"【剪映】草稿「{jianying_name}」已写入剪映的草稿目录：",
            f"     {draft_dir}",
            "  打开剪映 → 首页「本地草稿」里应出现它。",
            "  字幕、标题都在草稿里；**成片需要在剪映里手动导出**"
            "（SliceQ 不代劳）。",
            "",
        ]
    lines += [
        f"【素材】源视频：{video}",
        "  ⚠️ 同名冲突提醒：达芬奇按**文件名**匹配媒体池里的已有条目。",
        "     若你的媒体池里已经有同名文件，可能挂错。必要时先清空媒体池。",
        "",
        "【字幕】导入字幕（★ 两份 SRT，用本文件最上面那张表里对应的那一份）",
        "  导入方式：File > Import > Subtitle... 选 .srt，",
        "     再在**媒体池**里**右键那个字幕条目** →",
        "     「将所选字幕插入到… > 使用时间码的时间线」。",
        "     ⚠️ 点完**没反应**，基本就是时间码基准不对 —— 回到本文件最上面那段。",
        "",
        "  ✅ 两份 SRT 都已**自动补偿**：第一条是一条**空的**占位字幕（时长 0.001s）。",
        "     🚫 **请不要删掉它** —— 达芬奇可能把 SRT 的第一条强行对齐到",
        "        时间线起点；让这条空字幕去顶，后面的字幕才会落到正确位置。",
        "  自检：第一条**有内容**的字幕不应该正好压在时间线起点上。",
        "",
        "【⚠️ 关于字幕样式】",
        "  导出通道**不会带上**你在 SliceQ 里设的字幕样式"
        "（字体 / 颜色 / 描边 / 双语排版）：",
        "    · 剪映  用剪映自己的默认字幕样式",
        "    · 达芬奇 用并行导出的 .srt，样式在达芬奇里自行设置",
        "  想统一外观的话，请在目标软件里调整一次字幕样式。",
        "",
        "【时间线对照】",
    ]
    acc = 0.0
    for i, h in enumerate(highlights, 1):
        lines.append(f"  片段{i}  「{h.title}」"
                     f"  时间线 {acc:.0f}~{acc + h.duration:.0f}s"
                     f"  ←  源 {h.start:.0f}~{h.end:.0f}s")
        acc += h.duration
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────
# 通道 3：降级包
# ─────────────────────────────────────────────────────────────
def export_fallback(video: str | Path, clips: Sequence[dict], *,
                    out_dir: str | Path, task_id: int = 0,
                    task_name: str = "任务",
                    segments: Iterable | None = None,
                    preset=None, transport=None, use_llm_split: bool = True,
                    mode: str = "exact", burn_subtitles: bool = False,
                    bilingual: bool = False, target_lang: str = "en",
                    width: int = 0, height: int = 0,
                    progress: ProgressCb | None = None,
                    should_cancel: CancelCb | None = None) -> ChannelResult:
    """降级包 = 阶段 3 的成片导出（每条候选一个 mp4 + SRT）+ 导入说明。

    复用 `editor.export_clips` —— 不另写一套切片逻辑。
    """
    from . import editor                                     # 局部导入避免环

    res = ChannelResult(name="降级包（片段 + 字幕）")
    out = Path(out_dir)
    try:
        r = editor.export_clips(
            video, clips, out_base=out, task_id=task_id,
            task_name=task_name, mode=mode,
            burn_subtitles=burn_subtitles, segments=segments,
            preset=preset, transport=transport, use_llm_split=use_llm_split,
            bilingual=bilingual, target_lang=target_lang,
            width=width, height=height,
            progress=progress, should_cancel=should_cancel)
    except Exception as exc:                                # noqa: BLE001
        res.error = f"导出片段失败：{exc}"
        return res

    if not r.ok_count:
        res.error = "没有成功导出任何片段。"
        res.out_dir = r.out_dir
        return res

    res.ok = True
    res.out_dir = r.out_dir
    res.files = [Path(i.video).name for i in r.items if i.ok]
    res.message = f"{r.ok_count} 个片段（含 SRT 字幕文件）"
    return res


# ─────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────
def export_drafts(video: str | Path, clips: Sequence[dict], *,
                  task_id: int = 0, task_name: str = "任务",
                  out_base: str | Path | None = None,
                  want_jianying: bool = True,
                  want_davinci: bool = True,
                  segments: Iterable | None = None,
                  preset: subtitle_style.SubtitlePreset | None = None,
                  transport=None, use_llm_split: bool = True,
                  bilingual: bool = False, target_lang: str = "en",
                  fallback_mode: str = "exact",
                  burn_subtitles: bool = False,
                  width: int = 0, height: int = 0,
                  draft_root: str | Path | None = None,
                  progress: ProgressCb | None = None,
                  should_cancel: CancelCb | None = None) -> DraftResult:
    """导出草稿（剪映 + 达芬奇），失败自动退回降级包。

    `progress(done, total, 描述)`：total 固定为 100 的分段计数。
    """
    video = Path(video).resolve()
    result = DraftResult()

    def report(pct: int, msg: str) -> None:
        if progress:
            progress(max(0, min(100, int(pct))), 100, msg)

    try:
        report(2, "探测素材")
        media = probe_media(video)
        if not media.ok:
            raise ExporterError(
                "读不出素材的尺寸，无法生成草稿。\n"
                "→ 确认视频文件完好，或先到「设置 → 环境」检查 ffprobe。")

        highlights = plan_highlights(clips, media_duration=media.duration)
        result.highlights = highlights
        skipped = len([c for c in (clips or [])]) - len(highlights)
        if not highlights:
            raise ExporterError("没有可导出的片段（候选列表为空，或时间范围无效）。")
        if skipped > 0:
            result.notes.append(
                f"有 {skipped} 条候选的时间范围无效或超出素材时长，已跳过。")

        stamp = _timestamp()
        base = f"{_safe_stem(task_name)}_{stamp}"
        out = Path(out_base) if out_base else (config.EXPORT_DIR / base)
        out.mkdir(parents=True, exist_ok=True)
        result.out_dir = out

        # ── 字幕：断句 + **跨段重映射** ────────────────
        report(10, "整理字幕（跨段时间轴重映射）")
        srt_path: Path | None = None
        if segments:
            info = build_timeline_subtitles(
                segments, highlights, out_path=out / f"{base}.srt",
                transport=transport, use_llm=use_llm_split,
                bilingual=bilingual,
                progress=lambda p, m: report(10 + int(p * 20), m))
            result.cue_count = info["cue_count"]
            result.dropped_cues = info["dropped"]
            if info["srt"]:
                srt_path = Path(info["srt"])
                if not result.cue_count:
                    # 不报错但必须说 —— 否则用户以为"字幕导出了"
                    result.notes.append(
                        "字幕为空：候选时段内没有可用的转录文本"
                        "（可能是段边界把字幕都裁掉了）。")

        # ── 通道 2：达芬奇 ─────────────────────────────
        davinci_dir = out / "达芬奇"
        if want_davinci and not (should_cancel and should_cancel()):
            report(35, "生成达芬奇时间线（OTIO）")
            cr = export_davinci(video, highlights, out_dir=davinci_dir,
                                media=media, timeline_srt=srt_path,
                                name=base)
            result.channels["davinci"] = cr
            if not cr.ok:
                result.notes.append(f"达芬奇通道失败：{cr.error.splitlines()[0]}")

        # ── 通道 1：剪映 ───────────────────────────────
        jy_name = ""
        if want_jianying and not (should_cancel and should_cancel()):
            report(50, "生成剪映草稿")
            ready, why = jianying_ready()
            if not ready:
                result.channels["jianying"] = ChannelResult(
                    name="剪映草稿", ok=False, error=why)
                result.notes.append("剪映通道不可用，已改为导出降级包。")
            else:
                ver, verified = detect_jianying_version()
                cr = export_jianying(
                    video, highlights, media=media, timeline_srt=srt_path,
                    draft_root=draft_root, name=base,
                    work_dir=(out / "_build"))
                result.channels["jianying"] = cr
                if cr.ok:
                    jy_name = cr.out_dir.name if cr.out_dir else base
                    if ver and not verified:
                        result.notes.append(
                            f"⚠️ 本机剪映版本 {ver} 未在已验证列表内"
                            f"（已验证：{'、'.join(config.JIANYING_VERIFIED_VERSIONS)}）。"
                            f"草稿格式兼容性未经验证，若打不开请反馈。")
                else:
                    result.notes.append(
                        f"剪映通道失败：{cr.error.splitlines()[0]}")

        # ── 降级包（剪映失败或不可用时）────────────────
        need_fallback = want_jianying and not (
            result.channels.get("jianying", ChannelResult()).ok)
        if need_fallback:
            report(65, "剪映通道不可用，导出降级包")
            fb = export_fallback(
                video, clips, out_dir=out / "降级包", task_id=task_id,
                task_name=task_name, segments=segments, preset=preset,
                transport=transport, use_llm_split=use_llm_split,
                mode=fallback_mode, burn_subtitles=burn_subtitles,
                bilingual=bilingual, target_lang=target_lang,
                width=width, height=height,
                progress=lambda d, t, m: report(65 + int(d * 25), m),
                should_cancel=should_cancel)
            result.channels["fallback"] = fb
            if fb.ok:
                result.notes.append(
                    "已生成降级包：每条候选一个 mp4 + SRT，可直接拖进任意剪辑软件。")

        # ── 达芬奇的导入说明（拿不到剪映名字时也要有）──
        if srt_path and not (davinci_dir / "导入说明.txt").exists():
            try:
                _write_guide(davinci_dir / "导入说明.txt", video=video,
                             base=base, highlights=highlights,
                             draft_dir=(result.channels.get("jianying").out_dir
                                        if result.channels.get("jianying")
                                        and result.channels["jianying"].ok
                                        else None),
                             jianying_name=jy_name,
                             fps=media.fps)
            except Exception:                               # noqa: BLE001
                pass

        # ── 收尾提示 ───────────────────────────────────
        result.notes.append(
            "字幕样式不会跟随导出：剪映用它的默认样式，达芬奇用并行的 .srt 文件。")
        if want_jianying or want_davinci:
            result.notes.append(
                "导出后需要手动完成最后一步：剪映里渲染成片；"
                "达芬奇里导入 .otio 并按「导入说明.txt」定位素材。")

        result.ok = bool(result.ok_channels())
        report(100, "完成")
        return result

    except ExporterError as exc:
        result.error = str(exc)
        return result
    except Exception as exc:                                # noqa: BLE001
        result.error = f"导出草稿失败：{type(exc).__name__}: {exc}"
        return result


def _timestamp() -> str:
    import time
    return time.strftime("%Y%m%d_%H%M")


def format_result(result: DraftResult) -> str:
    """给 UI 用的多行汇总。"""
    lines: list[str] = []
    if result.error:
        lines.append(f"[失败] {result.error}")
        return "\n".join(lines)
    lines.append(f"时间线：{len(result.highlights)} 段，"
                 f"字幕 {result.cue_count} 条"
                 + (f"（边界丢弃 {result.dropped_cues} 条）"
                    if result.dropped_cues else ""))
    for key, label in (("jianying", "剪映草稿"),
                       ("davinci", "达芬奇 OTIO"),
                       ("fallback", "降级包")):
        cr = result.channels.get(key)
        if not cr:
            continue
        if cr.ok:
            lines.append(f"[成功] {label}：{cr.message}")
            for f in cr.files[:6]:
                lines.append(f"        {f}")
        else:
            lines.append(f"[失败] {label}：{cr.error.splitlines()[0] if cr.error else '未知'}")
    for n in result.notes:
        lines.append(f"提示：{n}")
    if result.out_dir:
        lines.append(f"输出目录：{result.out_dir}")
    return "\n".join(lines)
