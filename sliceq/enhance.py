# -*- coding: utf-8 -*-
"""SliceQ 阶段 4-A：成片增强（淡入淡出 / 配乐 ducking / 封面）。

本模块只负责**构造参数与滤镜串**，不直接执行 ffmpeg ——
执行统一交给 `editor.run_ffmpeg`（它已经有进度解析、取消、错误中文解释）。

────────────────────────────────────────────────────────────────
三条硬实测结论（stage0/probe_resolve/probe_stage4_enhance.py
                + probe_stage4_fontconfig.py）
────────────────────────────────────────────────────────────────

1. **封面不能用 `drawtext`**（R26 关闭）。
   本机 ffmpeg（gyan.dev 9.0.2 full_build）执行 drawtext 直接
   `rc=3221225477 (0xC0000005)`，崩溃前一行是
   `Fontconfig error: Cannot load default config file`。
   试过三种 `fonts.conf` 写法 + `FONTCONFIG_FILE` + `FONTCONFIG_PATH`，
   **全部无效且 `FONTCONFIG_DEBUG=1` 无任何输出** —— 说明该构建的
   fontconfig 压根不读环境变量，属**构建级缺陷，用户端不可修**。
   ⇒ 封面改走 **libass（`subtitles` 滤镜）**：实测能出图、中文正常。

2. **libass 不依赖 fontconfig 也能匹配到系统字体**。
   用 SimHei / SimSun / Microsoft YaHei 渲染同一句中文，
   三张输出图的 MD5 **互不相同** ⇒ 字体设置**确实生效**，
   不存在"静默 fallback 到某个内建字体"的问题。
   ⚠️ 反过来说：早期用 `ffmpeg -list_fonts` 测出的"0 个中文字体"
   **是测量错误** —— 该选项在本构建上根本不存在
   （`Error splitting the argument list: Option not found`），
   解析器把 stderr 当 stdout 读了。**别再引用那个数字。**

3. **乙方案下没有"片段间转场"**。
   阶段 3 定了「每条候选一个独立 mp4」，独立文件之间不存在相邻关系，
   `xfade` 无从施展。本模块的淡入淡出因此定义为
   **单条片段自己的首尾 fade**（`fade` / `afade`），实测可用。
   `xfade` 仅留给可选的「合集模式」，且它要求两路输入分辨率**完全一致**，
   不一致时直接 `Conversion failed!`（实测）。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config, ffmpeg_tools, subtitle_style

# ─────────────────────────────────────────────────────────────
# BGM 曲库
# ─────────────────────────────────────────────────────────────
BGM_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}

BGM_README = """SliceQ 背景音乐目录
====================

把你自己的背景音乐放进这个目录即可在导出时选用，支持：
    mp3 / m4a / aac / wav / flac / ogg / opus

为什么这里默认是空的
--------------------
SliceQ 不随包分发任何音乐文件。原因不是偷懒：

  音频的版权许可**不能像字体那样随便附一份 LICENSE 就完事**——
  免版税（royalty-free）不等于免署名、更不等于可再分发。
  把来源不明的音频打进安装包，风险直接转嫁到使用者身上。

所以请自行准备 BGM。几个可以合法取得素材的地方：

  · 你自己录的 / 自己做的
  · YouTube 音频库（studio.youtube.com，需自行确认条款）
  · Free Music Archive（freemusicarchive.org，按 CC 许可逐个确认）
  · Pixabay Music（pixabay.com/music，Pixabay 许可）

⚠️ 无论从哪里拿，**发布成片前请自行确认该音乐允许你的用途**。

建议
----
· 纯音乐（无人声）——有人声的 BGM 会和原片人声打架
· 循环友好（首尾能接上），因为 SliceQ 会循环填充不足的时长
· 时长 2~4 分钟最省事
"""


def bgm_dir() -> Path:
    return config.BGM_DIR


def list_bgm() -> list[dict]:
    """列出可用 BGM。返回 [{name, path, size, ext}]，按文件名排序。"""
    d = bgm_dir()
    if not d.is_dir():
        return []
    items: list[dict] = []
    for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
        if p.is_file() and p.suffix.lower() in BGM_EXTS:
            items.append({"name": p.stem, "path": str(p),
                          "size": p.stat().st_size, "ext": p.suffix.lower()})
    return items


def resolve_bgm(name_or_path: str | Path | None) -> Path | None:
    """把用户填的 BGM 名字/路径解析成真实文件。找不到返回 None。"""
    if not name_or_path:
        return None
    p = Path(name_or_path)
    if p.is_file():
        return p
    cand = bgm_dir() / p.name
    if cand.is_file():
        return cand
    # 允许省略扩展名
    for ext in BGM_EXTS:
        c = bgm_dir() / f"{p.name}{ext}"
        if c.is_file():
            return c
    d = bgm_dir()
    if d.is_dir():
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() in BGM_EXTS and f.stem == p.name:
                return f
    return None


def ensure_bgm_readme() -> Path:
    """确保 BGM 目录存在并带一份说明（首次使用写出，之后不覆盖）。"""
    d = bgm_dir()
    d.mkdir(parents=True, exist_ok=True)
    note = d / "使用说明.txt"
    if not note.exists():
        note.write_text(BGM_README, encoding="utf-8")
    return note


# ─────────────────────────────────────────────────────────────
# 增强选项
# ─────────────────────────────────────────────────────────────
@dataclass
class EnhanceOptions:
    """一条成片的增强参数。全部为 0/空 = 不做任何增强。"""

    enabled: bool = False
    fade_in: float = 0.0          # 秒，片头淡入
    fade_out: float = 0.0         # 秒，片尾淡出
    bgm: str = ""                 # BGM 名字或路径；空 = 不配乐
    bgm_volume_db: float = -15.0  # BGM 相对原声的音量
    bgm_loop: bool = True         # BGM 短于片段时循环
    bgm_fade: float = 1.0         # BGM 自身的淡入淡出，避免切口突兀
    duck: bool = True             # 人声出现时压低 BGM

    # ── ducking 参数：**用真实直播人声标定** ─────────────────
    #
    # ⚠️ 不能用合成信号标定。第一版拿 440Hz 正弦测出"thr=0.05 + level_sc=3"，
    #    搬到真实素材上直接把 BGM 压掉 23.8dB 且**全程咬着不放** ——
    #    因为真实人声中位 -11.9dB（线性 ≈0.25），thr=0.05 远低于它，
    #    任何时刻都远超阈值。合成正弦幅度 1.0，能量又集中在单一频率，
    #    压缩器一碰就触发，两边根本不是一回事。
    #
    # 真实素材受控实验（15s 直播人声 + 15s 纯静音，见 probe_real_duck.py）：
    #   真实人声 RMS -14.2dB / 峰值 -0.56dB ⇒ 峰值线性 ≈ 0.94
    #
    #   thr   ratio | 人声段压低 | 静音段压低
    #   0.05    8   |   15.40dB  |   0.00dB
    #   0.10    8   |   10.20dB  |   0.00dB
    #   0.20    8   |    5.00dB  |   0.00dB   ← 默认
    #   0.30    8   |    2.30dB  |   0.00dB
    #   0.50    8   |    0.60dB  |   0.00dB
    #   0.20    4   |    4.30dB  |   0.00dB
    #
    # 静音段恒为 0.00dB ⇒ 压缩器在没人声时**完全恢复**，
    # 即它确实在跟随人声，而不是一直压着（这一列才是真正的验收指标）。
    #
    # `level_sc`（sidechain 增益）在真实素材上**不需要**：人声峰值本来就够强，
    # 默认 1.0 关闭。合成信号"需要"它只是因为正弦不真实。
    duck_threshold: float = 0.2
    duck_ratio: float = 8.0
    duck_attack: int = 20
    duck_release: int = 300
    duck_level_sc: float = 1.0    # 1.0 = 关闭；真实素材上保持 1.0

    def is_noop(self) -> bool:
        return not (self.enabled and (
            self.fade_in > 0 or self.fade_out > 0 or bool(self.bgm)))

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}  # type: ignore[attr-defined]

    @classmethod
    def from_dict(cls, d: dict | None) -> "EnhanceOptions":
        d = d or {}
        out = cls()
        for k in out.__dataclass_fields__:  # type: ignore[attr-defined]
            if k in d and d[k] is not None:
                setattr(out, k, d[k])
        return out


# ─────────────────────────────────────────────────────────────
# 「配乐强度」—— 把 threshold/ratio 翻译成用户看得懂的东西
# ─────────────────────────────────────────────────────────────
# 界面**不要**暴露 threshold / ratio：那是压缩器术语，
# 用户只想知道"BGM 会让开多少"。所以给四档，档位背后才是参数。
# 数值来自真实人声标定表（见 EnhanceOptions 注释）。
DUCK_LEVELS: dict[str, dict] = {
    "off":    {"label": "不压低", "duck": False,
               "desc": "BGM 恒定音量，人声会被盖住一部分"},
    "light":  {"label": "轻柔", "duck": True,
               "duck_threshold": 0.30, "duck_ratio": 4.0,
               "desc": "人声出现时 BGM 略让（约 3dB），音乐存在感更强"},
    "normal": {"label": "标准", "duck": True,
               "duck_threshold": 0.20, "duck_ratio": 8.0,
               "desc": "人声出现时 BGM 明显让开（约 5dB），推荐"},
    "strong": {"label": "强力", "duck": True,
               "duck_threshold": 0.10, "duck_ratio": 8.0,
               "desc": "人声出现时 BGM 几乎退场（约 10dB），适合口播"},
}
DUCK_LEVEL_DEFAULT = "normal"


def apply_duck_level(opts: EnhanceOptions, level: str) -> EnhanceOptions:
    """按档位设置 ducking 参数（就地修改并返回）。"""
    cfg = DUCK_LEVELS.get(level) or DUCK_LEVELS[DUCK_LEVEL_DEFAULT]
    opts.duck = bool(cfg["duck"])
    if cfg["duck"]:
        opts.duck_threshold = float(cfg["duck_threshold"])
        opts.duck_ratio = float(cfg["duck_ratio"])
    return opts


def duck_level_of(opts: EnhanceOptions) -> str:
    """反查当前参数最接近哪一档（用于 UI 回显）。"""
    if not opts.duck:
        return "off"
    for key, cfg in DUCK_LEVELS.items():
        if not cfg["duck"]:
            continue
        if (abs(opts.duck_threshold - float(cfg["duck_threshold"])) < 1e-6
                and abs(opts.duck_ratio - float(cfg["duck_ratio"])) < 1e-6):
            return key
    return "custom"


# ─────────────────────────────────────────────────────────────
# 滤镜串
# ─────────────────────────────────────────────────────────────
def video_chain(duration: float, opts: EnhanceOptions,
                ass_arg: str | None = None) -> str:
    """视频滤镜链。

    顺序：**先烧字幕、再淡入淡出** —— 让淡出盖住字幕，
    否则结尾画面黑了字幕还亮着，像没关掉的灯。
    """
    parts: list[str] = []
    if ass_arg:
        parts.append(ass_arg)
    if opts.fade_in > 0:
        parts.append(f"fade=t=in:st=0:d={opts.fade_in:.3f}")
    if opts.fade_out > 0:
        st = max(0.0, duration - opts.fade_out)
        parts.append(f"fade=t=out:st={st:.3f}:d={opts.fade_out:.3f}")
    return ",".join(parts)


def _sc_has_level_sc() -> bool:
    """本机 ffmpeg 的 sidechaincompress 是否支持 `level_sc`。

    `level_sc` 是较新版本才有的参数（旧版写了会直接报
    `Option 'level_sc' not found`，而且这个报错**不告诉你参数是什么时候加的**，
    看上去像是自己写错了）。探测一次并缓存，不支持就退回纯 threshold/ratio。
    """
    global _SC_LEVEL_CACHE
    if _SC_LEVEL_CACHE is not None:
        return _SC_LEVEL_CACHE
    ff = ffmpeg_tools.find_ffmpeg()
    if not ff:
        _SC_LEVEL_CACHE = False
        return False
    try:
        r = subprocess.run([str(ff), "-hide_banner", "-h", "filter=sidechaincompress"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        _SC_LEVEL_CACHE = "level_sc" in (r.stdout or "") + (r.stderr or "")
    except Exception:      # noqa: BLE001
        _SC_LEVEL_CACHE = False
    return _SC_LEVEL_CACHE


_SC_LEVEL_CACHE: bool | None = None


def audio_filter_complex(duration: float, opts: EnhanceOptions, *,
                         has_voice: bool, bgm_input: int | None) -> tuple[str, str]:
    """构造音频部分的 filter_complex，返回 (滤镜串, 输出标签)。

    `bgm_input` 是 BGM 在 ffmpeg 输入列表里的序号（第一路输入是片源，从 0 起，
    所以 BGM 通常是 1）；None 表示没有 BGM。

    三种形态：
      原声 + BGM + ducking   → sidechaincompress 用原声当 key 压 BGM，再 amix
      只有 BGM              → 直接当输出音轨（原片无声）
      只有原声              → 仅加 afade
    """
    out = "[aout]"
    fade_in = f"afade=t=in:st=0:d={opts.fade_in:.3f}," if opts.fade_in > 0 else ""
    fo_st = max(0.0, duration - opts.fade_out)
    fade_out = f",afade=t=out:st={fo_st:.3f}:d={opts.fade_out:.3f}" if opts.fade_out > 0 else ""

    if not has_voice and bgm_input is None:
        # 既无声也无 BGM：调用方不该走到这里（由 build 命令时判空）
        return "", "[aout]"

    if has_voice:
        voice = f"[0:a]aresample=48000,{fade_in}volume=1.0{fade_out}[voice]"
    else:
        voice = ""

    if bgm_input is None:
        return voice, "[voice]"

    # BGM 自身淡入淡出（避免"啪"地开始、"啪"地切断）
    bf = opts.bgm_fade
    bgm_fades = ""
    if bf > 0:
        bgm_fades = (f",afade=t=in:st=0:d={bf:.3f}"
                     f",afade=t=out:st={max(0.0, duration - bf):.3f}:d={bf:.3f}")
    bgm = (f"[{bgm_input}:a]aresample=48000,volume={opts.bgm_volume_db:.1f}dB"
           f"{bgm_fades}"
           f",atrim=0:{duration:.3f},asetpts=N/SR/TB[bgm]")

    if not has_voice:
        return bgm, "[bgm]"

    if not opts.duck:
        mix = (f"{voice};{bgm};[voice][bgm]amix=inputs=2:duration=first:"
               f"normalize=0{out}")
        return mix, out

    # level_sc 是"提升检测灵敏度"的那一味 —— 见 EnhanceOptions 里的实测表
    sc_extra = ""
    if opts.duck_level_sc and abs(opts.duck_level_sc - 1.0) > 1e-6:
        if _sc_has_level_sc():
            sc_extra = f":level_sc={opts.duck_level_sc:g}"

    duck = (f"[bgm][voice]sidechaincompress="
            f"threshold={opts.duck_threshold:g}:ratio={opts.duck_ratio:g}:"
            f"attack={opts.duck_attack}:release={opts.duck_release}:makeup=1"
            f"{sc_extra}[ducked]")
    mix = (f"{voice};{bgm};{duck};"
           f"[voice][ducked]amix=inputs=2:duration=first:normalize=0{out}")
    return mix, out


# ─────────────────────────────────────────────────────────────
# 封面
# ─────────────────────────────────────────────────────────────
COVER_W, COVER_H = 1280, 720


@dataclass
class CoverStyle:
    font: str = ""                    # 空 = 自动挑中文字体
    size: int = 92
    color: str = "#FFFFFF"
    outline: int = 8
    outline_color: str = "#000000"
    position: str = "bottom"          # bottom / center / top
    margin_v: int = 70
    shadow: int = 4
    bar: bool = False                 # 半透明背景条（压住花哨画面）
    bar_alpha: int = 96               # 0~255，越大越透明

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}  # type: ignore[attr-defined]

    @classmethod
    def from_dict(cls, d: dict | None) -> "CoverStyle":
        d = d or {}
        out = cls()
        for k in out.__dataclass_fields__:  # type: ignore[attr-defined]
            if k in d and d[k] is not None:
                setattr(out, k, d[k])
        return out


_ALIGN = {"bottom": 2, "center": 5, "top": 8}
_MAX_COVER_LINES = 3


def pick_cover_font(explicit: str = "") -> str:
    """挑一个可用字体。优先用户指定，其次常见中文黑体。

    ⚠️ libass 在字体缺失时**静默 fallback** —— 不报错、不警告，
       用户只会觉得"标题怎么变样了"。所以这里必须校验。
    """
    if explicit and subtitle_style.font_available(explicit):
        return explicit
    preferred = ["Source Han Sans CN", "思源黑体 CN", "Microsoft YaHei",
                 "微软雅黑", "SimHei", "黑体", "Deng", "等线",
                 "Microsoft YaHei UI", "SimSun", "宋体", "Arial"]
    for name in preferred:
        if subtitle_style.font_available(name):
            return name
    return "Arial"


def fit_cover_size(title: str, style: CoverStyle, *, width: int = COVER_W,
                   height: int = COVER_H, margin: int = 70) -> tuple[int, str]:
    """给标题找一个放得下的字号，返回 (字号, 已换行的文本)。

    字号从 `style.size` 往下试，直到行数 <= 3。
    中文没有空格，libass 不会自动换行（会直接溢出裁切），
    所以这里必须先算出每行放几个字。
    """
    size = max(24, int(style.size))
    floor = max(24, int(size * 0.45))
    text = (title or "").strip()
    while size >= floor:
        wrapped = subtitle_style.wrap_for_canvas(
            text, width=width, size=size, margin=margin,
            max_lines=_MAX_COVER_LINES)
        if wrapped.count(r"\N") + 1 <= _MAX_COVER_LINES:
            return size, wrapped
        size = int(size * 0.9)
    return floor, subtitle_style.wrap_for_canvas(
        text, width=width, size=floor, margin=margin, max_lines=4)


def build_cover_ass(title: str, style: CoverStyle, *,
                    width: int = COVER_W, height: int = COVER_H,
                    duration: float = 60.0) -> str:
    """生成封面用的 ASS（就一条居中的大标题）。"""
    font = pick_cover_font(style.font)
    size, wrapped = fit_cover_size(title, style, width=width, height=height)
    align = _ALIGN.get(style.position, 2)

    color = subtitle_style.rgb_to_ass(style.color)
    outline_c = subtitle_style.rgb_to_ass(style.outline_color)
    back = f"&H{style.bar_alpha:02X}000000" if style.bar else "&H80000000"
    border = 3 if style.bar else 1     # BorderStyle 3 = 不透明背景框
    margin_v = int(style.margin_v) + (size // 2 if style.position == "bottom" else 0)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 2\n"                # 2 = 不自动换行（我们已手工插 \N）
        "ScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cover,{font},{size},{color},{color},{outline_c},{back},"
        f"-1,0,0,0,100,100,2,0,{border},{style.outline},{style.shadow},"
        f"{align},70,70,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    body = (f"Dialogue: 0,{_ass_time(0)},{_ass_time(duration)},Cover,,0,0,0,,"
            f"{subtitle_style.ass_escape(wrapped)}\n")
    return header + body


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


# ── 抽帧选帧 ────────────────────────────────────────────────

def _pil_available() -> bool:
    global _PIL_CACHE
    if _PIL_CACHE is None:
        try:
            from PIL import Image  # noqa: F401
            _PIL_CACHE = True
        except ImportError:
            _PIL_CACHE = False
    return _PIL_CACHE


_PIL_CACHE: bool | None = None


def _score_frame(path: Path) -> float:
    """给一帧打"耐看程度"分。

    指标 = 边缘方差（画面细节多少；纯色/黑屏接近 0）× 亮度惩罚。
    **不引入 numpy** —— Pillow 的 FIND_EDGES + ImageStat 足够，
    而且少一个依赖就少一个打包体积和兼容问题。

    Pillow 缺失时返回常数 1.0 ⇒ 选帧退化为"取第一个采样点"。
    这个降级**会被 `make_cover` 如实报出来**，不静默。
    """
    try:
        from PIL import Image, ImageFilter, ImageStat
    except ImportError:
        return 1.0
    try:
        im = Image.open(path).convert("L").resize((320, 180))
    except Exception:      # noqa: BLE001 — 坏帧不该让整个导出失败
        return 0.0
    edges = im.filter(ImageFilter.FIND_EDGES)
    detail = ImageStat.Stat(edges).var[0]
    mean = ImageStat.Stat(im).mean[0]
    # 过暗（黑屏）或过亮（白闪）都判为废帧
    factor = 1.0 if 28 < mean < 228 else 0.15
    return detail * factor


def pick_best_frame(video: str | Path, start: float, end: float,
                    out_dir: str | Path, *, samples: int = 8,
                    stem: str = "cover") -> Path:
    """从片段里挑一帧作封面底图。

    策略：均匀采样 N 帧 → 打分 → 取最高分那一帧的全尺寸图。
    避开首尾各 8%（片头常有转场/黑场，片尾常有结束动画）。

    ⚠️ "最高评分帧"在 PRD 里对应候选的 `score`，但那个分数是**整段**
       的评分，不是逐帧的。逐帧评分模型不在 v1 范围，这里用
       "画面信息量最高"作代理指标 —— 比"随机取第一帧"强得多，
       且不需要额外模型调用。
    """
    ff = ffmpeg_tools.find_ffmpeg()
    if not ff:
        raise RuntimeError("未找到 FFmpeg")

    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = max(0.2, float(end) - float(start))
    # 避开首尾
    lo = float(start) + dur * 0.08
    hi = float(end) - dur * 0.08
    if hi <= lo:
        lo, hi = float(start), float(end)

    times = [lo + (hi - lo) * (i + 0.5) / samples for i in range(samples)]

    # 一次调用抽 N 张小图（比 N 次调用快得多）
    tmp_dir = out_dir / "_probe_frames"
    tmp_dir.mkdir(exist_ok=True)
    for old in tmp_dir.glob("s_*.png"):
        old.unlink(missing_ok=True)

    for i, t in enumerate(times):
        subprocess.run(
            [str(ff), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-ss", f"{t:.3f}", "-i", str(video),
             "-vf", "scale=480:-2", "-frames:v", "1",
             str(tmp_dir / f"s_{i:02d}.png")],
            capture_output=True, timeout=90)

    shots = sorted(tmp_dir.glob("s_*.png"))
    if not shots:
        raise RuntimeError("抽帧失败：没有产出任何帧（片段可能超出片长）")

    best_i, best_score = 0, -1.0
    for p in shots:
        sc = _score_frame(p)
        if sc > best_score:
            best_i, best_score = int(p.stem.split("_")[1]), sc

    # 用选中的时刻抽全尺寸帧
    full = out_dir / f"{stem}_base.png"
    r = subprocess.run(
        [str(ff), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-ss", f"{times[best_i]:.3f}", "-i", str(video),
         "-frames:v", "1", str(full)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0 or not full.exists():
        raise RuntimeError((r.stderr or "抽帧失败").strip()[-300:])
    return full


def build_cover_cmd(base_frame: str | Path, ass_path: str | Path,
                    out_path: str | Path, *, width: int = COVER_W,
                    height: int = COVER_H,
                    work_dir: str | Path | None = None) -> tuple[list[str], str | None]:
    """构造「底图 + 标题 → jpg」的命令。

    用 `subtitles` 而不是 `drawtext` —— 见模块头注释第 1 条。
    PlayRes 与封面尺寸一致（1280×720），所以字幕坐标能直接对上。
    """
    ff = ffmpeg_tools.find_ffmpeg()
    if not ff:
        raise RuntimeError("未找到 FFmpeg")

    scale = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black")
    ass_arg, cwd = subtitle_style.ass_filter_arg(ass_path, cwd=work_dir)
    vf = f"{scale},{ass_arg}"
    argv = [str(ff), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(Path(base_frame).resolve()),
            "-vf", vf, "-frames:v", "1", "-q:v", "2", str(out_path)]
    return argv, cwd


def make_cover(video: str | Path, start: float, end: float,
               title: str, out_path: str | Path, *,
               style: CoverStyle | None = None,
               width: int = COVER_W, height: int = COVER_H,
               stem: str = "cover") -> dict:
    """一步产出封面 jpg。返回 {ok, path, base, frame_time, font, size, note}。"""
    style = style or CoverStyle()
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent / "_work"
    work.mkdir(parents=True, exist_ok=True)

    info: dict = {"ok": False, "path": "", "font": "", "size": 0, "note": ""}
    base = pick_best_frame(video, start, end, work, stem=stem)
    info["base"] = str(base)

    font = pick_cover_font(style.font)
    info["font"] = font
    notes: list[str] = []
    if style.font and style.font != font:
        notes.append(f"字体「{style.font}」在本机不可用，已用「{font}」代替"
                     f"（libass 遇到缺失字体会静默换掉，不会报错）")
    if not _pil_available():
        # 降级要说出来，否则用户只会觉得"封面怎么老是这一帧"
        notes.append("未安装 Pillow，封面只能取固定位置的一帧；"
                     "装上后可以自动挑出画面最好的一帧。")
    if notes:
        info["note"] = "；".join(notes)

    ass = work / f"{stem}_cover.ass"
    ass.write_text(build_cover_ass(title, CoverStyle.from_dict(
        {**style.to_dict(), "font": font}), width=width, height=height),
        encoding="utf-8")

    from . import editor
    argv, cwd = build_cover_cmd(base, ass, out_path, width=width, height=height,
                                work_dir=work)
    editor.run_ffmpeg(argv, cwd=cwd)
    if not out_path.exists():
        raise RuntimeError("封面没有产出文件")
    info["ok"] = True
    info["path"] = str(out_path)
    info["size"] = out_path.stat().st_size
    return info
