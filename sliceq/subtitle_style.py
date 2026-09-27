# -*- coding: utf-8 -*-
"""字幕样式模型 —— 样式面板的数据层与 ASS 生成器。

## 为什么是 ASS 而不是 SRT

样式面板要「主字幕」与「副字幕」的**字体、字号、字间距、颜色、边框颜色、
边框粗细全部独立可调**。SRT 做不到：

  - `subtitles=x.srt:force_style=...` 里的 `force_style` 是**全局覆盖**，
    对文件里所有字幕统一生效，无法分层。
  - SRT 本身也不携带样式信息。

ASS 可以：[V4+ Styles] 里定义 `Main` / `Sub` 两套样式，
每条字幕按需选用，各自的字体/颜色/边框/垂直位置互不干扰。

⇒ **烧录用 ASS；SRT 仍作为「交付物」单独产出**（给达芬奇导入用，
   见 TECH-DESIGN §6.3 的 C 路线三件套）。

## 三个必须记住的实现坑（均已实测）

1. **颜色是 `&HAABBGGRR`，B 与 R 对调** —— 不是 RGB。
   写错不会报错，只会颜色错（比如红蓝互换），非常容易漏。
2. **滤镜参数里的 Windows 路径必须双反斜杠转义**（`C\\:/...`），
   否则 `subtitles=C:/...` 会被解析成"滤镜 subtitles + 参数 C"，
   报 `Unable to parse "original_size"`。
   （根治办法见 `ass_filter_arg()`：用相对路径 + cwd，路径里没有冒号。）
3. **字体名不存在时 libass 静默 fallback，不报错**。
   用户在面板里选了没装的字体，看到的是"改了没反应" ——
   必须在 UI 层校验并提示，不能指望渲染层报错。

## 与 editor.py 的关系

本模块只负责「把样式 + 字幕内容 → ASS 文本」，
不负责切视频、不负责调 ffmpeg 烧录。烧录在 editor.py。
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config

# ─────────────────────────────────────────────────────────────
# 默认值（取自产品设计稿的字幕面板）
# ─────────────────────────────────────────────────────────────
DEFAULT_FONT = "Microsoft YaHei"
DEFAULT_MAIN_SIZE = 42
DEFAULT_SUB_SIZE = 30
DEFAULT_MAIN_SPACING = 3.2
DEFAULT_SUB_SPACING = 0.8
DEFAULT_COLOR = "#FFFFFF"          # 白字黑边（行业惯例）
# ↑ 原为 "#00FF00"（亮绿，早期设计稿配色）。2026-09-26 用户确认改为白字：
#   亮绿在浅色画面上可读性差，且白字黑边是字幕的通行做法。
#   注意：改这里只影响**出厂值**。磁盘上已存在的 default.json 会覆盖它，
#   所以变更出厂配色时要一并 reset 掉旧文件（见 reports/STAGE4B… 的配色条目）。
DEFAULT_OUTLINE_COLOR = "#000000"
DEFAULT_OUTLINE = 2.0
DEFAULT_VERTICAL_GAP = 32

# 底部整体边距（设计稿未暴露此参数，按画布高度比例给一个稳妥值）
_MARGIN_BOTTOM_RATIO = 0.07
# 左右安全边距（写进 ASS 的 MarginL / MarginR）。
# ⚠️ 必须与 wrap_for_canvas 用同一个值 —— 否则"按多少宽度换行"和
#    "实际可用宽度"会对不上，换行算得再好也会溢出。
_SIDE_MARGIN = 20
# 行高估算系数：ASS 字号 → 实际占位高度
_LINE_HEIGHT_FACTOR = 1.35


# ─────────────────────────────────────────────────────────────
# 颜色
# ─────────────────────────────────────────────────────────────
def rgb_to_ass(color: str, *, alpha: int = 0) -> str:
    """`#RRGGBB` → ASS 的 `&HAABBGGRR`。

    ⚠️ **ASS 的颜色是 BGR 顺序**，不是 RGB。直接照抄 RGB 会得到
       红蓝互换的结果 —— 而且不会有任何报错。

    alpha: 0 = 完全不透明，255 = 完全透明（ASS 的 alpha 语义是反的）。
    """
    h = (color or "").strip().lstrip("#")
    if len(h) == 8:                 # 容忍 #AARRGGBB 写法，取后 6 位
        h = h[2:]
    if len(h) == 3:                 # 容忍 #RGB 简写
        h = "".join(c * 2 for c in h)
    if len(h) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        h = DEFAULT_COLOR.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha & 0xFF:02X}{b}{g}{r}".upper()


def ass_to_rgb(value: str) -> str:
    """ASS 颜色 → `#RRGGBB`（反向转换，供读回已保存样式用）。"""
    h = (value or "").strip().lstrip("&H&h")
    if len(h) == 8:
        h = h[2:]                   # 去掉 AA
    if len(h) != 6:
        return DEFAULT_COLOR
    b, g, r = h[0:2], h[2:4], h[4:6]
    return f"#{r}{g}{b}".upper()


def normalize_hex(color: str) -> str:
    """把任意输入规范成 `#RRGGBB`（UI 取值用）。"""
    h = (color or "").strip().lstrip("#")
    if len(h) == 8:
        h = h[2:]
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h or ""):
        return DEFAULT_COLOR
    return f"#{h.upper()}"


# ─────────────────────────────────────────────────────────────
# 样式模型
# ─────────────────────────────────────────────────────────────
@dataclass
class LayerStyle:
    """单层字幕的样式（主字幕或副字幕）。"""

    font: str = DEFAULT_FONT
    size: int = DEFAULT_MAIN_SIZE
    spacing: float = DEFAULT_MAIN_SPACING
    color: str = DEFAULT_COLOR
    outline_color: str = DEFAULT_OUTLINE_COLOR
    outline: float = DEFAULT_OUTLINE
    bold: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "LayerStyle":
        """宽松解析：缺字段用默认值，多余字段忽略，类型不对则纠正。"""
        d = d or {}
        c = cls()

        def _f(key: str, default):
            v = d.get(key, default)
            try:
                return type(default)(v)
            except (TypeError, ValueError):
                return default

        return cls(
            font=str(d.get("font") or c.font),
            size=max(8, min(200, _f("size", c.size))),
            spacing=max(-20.0, min(50.0, float(d.get("spacing", c.spacing)))),
            color=normalize_hex(d.get("color") or c.color),
            outline_color=normalize_hex(d.get("outline_color") or c.outline_color),
            outline=max(0.0, min(20.0, float(d.get("outline", c.outline)))),
            bold=bool(d.get("bold", c.bold)),
        )


@dataclass
class SubtitlePreset:
    """一套完整的字幕样式预设。"""

    name: str = "default"
    # 布局：译文是否在上方
    translate_on_top: bool = False
    # 主/副字幕之间的垂直间距（px，按画布高度）
    vertical_gap: int = DEFAULT_VERTICAL_GAP
    main: LayerStyle = field(default_factory=lambda: LayerStyle(
        size=DEFAULT_MAIN_SIZE, spacing=DEFAULT_MAIN_SPACING, bold=True))
    sub: LayerStyle = field(default_factory=lambda: LayerStyle(
        size=DEFAULT_SUB_SIZE, spacing=DEFAULT_SUB_SPACING, bold=False))

    # ── 序列化 ────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "translate_on_top": self.translate_on_top,
            "vertical_gap": self.vertical_gap,
            "main": self.main.to_dict(),
            "sub": self.sub.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "SubtitlePreset":
        d = d or {}
        base = cls()
        return cls(
            name=str(d.get("name") or base.name),
            translate_on_top=bool(d.get("translate_on_top", base.translate_on_top)),
            vertical_gap=max(0, min(400, int(d.get("vertical_gap", base.vertical_gap)))),
            main=LayerStyle.from_dict(d.get("main")),
            sub=LayerStyle.from_dict(d.get("sub")),
        )

    def copy_as(self, name: str) -> "SubtitlePreset":
        """基于当前样式新建一份（用于「新建样式」）。"""
        d = self.to_dict()
        d["name"] = name
        return SubtitlePreset.from_dict(d)

    # ── 垂直布局计算 ──────────────────────────────────────────
    def margins(self, *, height: int) -> dict[str, int]:
        """按画布高度算出两层各自的 MarginV（距底部距离）。

        语义：**下方那层贴底边距，上方那层 = 底边距 + 下层高度 + 间距**。

        ⚠️ 这里不能把 MarginV 写死成像素常量 —— ASS 的 MarginV 是相对
           PlayResY 的，不同分辨率下同样的 90 会得到不同的视觉位置。
           必须按画布高度比例算，预览与成片才会一致。
        """
        margin_bottom = max(10, int(height * _MARGIN_BOTTOM_RATIO))

        # 视觉上在下方的层，取决于「译文在上」开关
        if self.translate_on_top:
            lower, upper = "main", "sub"      # 主字幕在下，译文在上
        else:
            lower, upper = "sub", "main"

        lower_style: LayerStyle = getattr(self, lower)
        upper_style: LayerStyle = getattr(self, upper)

        lower_mv = margin_bottom
        lower_h = int(lower_style.size * _LINE_HEIGHT_FACTOR)
        upper_mv = margin_bottom + lower_h + max(0, int(self.vertical_gap))

        return {lower: lower_mv, upper: upper_mv}

    # ── ASS 生成 ──────────────────────────────────────────────
    def to_ass(self, cues, *, width: int, height: int,
               title: str = "SliceQ") -> str:
        """把字幕条目渲染成完整 ASS 文本。

        cues: 可迭代的 (start_s, end_s, main_text, sub_text)。
              main_text 为空则跳过主字幕行，sub_text 同理。

        width/height: **必须与实际成片一致**，否则字号与边框的缩放会走样。
        """
        mv = self.margins(height=height)

        def style_line(name: str, st: LayerStyle, margin_v: int) -> str:
            return (
                f"Style: {name},{st.font},{st.size},"
                f"{rgb_to_ass(st.color)},&H000000FF,"
                f"{rgb_to_ass(st.outline_color)},&H64000000,"
                f"{'-1' if st.bold else '0'},0,0,0,"
                f"100,100,{st.spacing:g},0,1,{st.outline:g},0,2,"
                f"{_SIDE_MARGIN},{_SIDE_MARGIN},{margin_v},1"
            )

        head = [
            "[Script Info]",
            f"; {title}",
            "ScriptType: v4.00+",
            f"PlayResX: {int(width)}",
            f"PlayResY: {int(height)}",
            "; WrapStyle 0 = 智能换行（长行自动折行）。",
            "; 副字幕常是长文本，必须允许换行 —— 设 2（不换行）会被裁掉。",
            "WrapStyle: 0",
            "; 边框随分辨率缩放，否则换画布尺寸时边框粗细会不对",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding",
            style_line("Main", self.main, mv["main"]),
            style_line("Sub", self.sub, mv["sub"]),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text",
        ]

        events: list[str] = []
        for cue in cues:
            start, end, main_text, sub_text = cue
            if end <= start:
                continue
            ts, te = _ass_time(start), _ass_time(end)
            # 主字幕在下时用更低的 Layer 值让副字幕不被覆盖；
            # 用一个固定顺序即可，两条不会重叠（MarginV 不同）
            if main_text:
                body = wrap_for_canvas(ass_escape(main_text),
                                       width=int(width), size=self.main.size,
                                       margin=_SIDE_MARGIN)
                events.append(
                    f"Dialogue: 0,{ts},{te},Main,,0,0,0,,{body}")
            if sub_text:
                body = wrap_for_canvas(ass_escape(sub_text),
                                       width=int(width), size=self.sub.size,
                                       margin=_SIDE_MARGIN)
                events.append(
                    f"Dialogue: 0,{ts},{te},Sub,,0,0,0,,{body}")

        return "\n".join(head + events) + "\n"


def _ass_time(seconds: float) -> str:
    """秒 → ASS 的 `H:MM:SS.cc`（**百分秒**，不是毫秒）。"""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_escape(text: str) -> str:
    """ASS 正文里 `{` `}` 会被当作样式覆盖块，必须转成全角。

    不转的话，用户字幕里出现花括号会**吞掉后面的整段文字**，
    而且不报错 —— 典型的"内容少了一截但看起来正常"。
    """
    t = (text or "").replace("{", "｛").replace("}", "｝")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    # ASS 里 \\N 是硬换行
    return t.replace("\n", r"\N").strip()


def _char_units(ch: str) -> float:
    """字符的**可见宽度**（以"一个全角字"为单位）。

    中日韩字符按 1.0 算，其余（拉丁字母/数字/标点）按 0.55 算 ——
    用字符**个数**判断换行会让"ABC123 混在中文里"的那几行明显偏短。
    """
    o = ord(ch)
    if o > 0x2E80:          # CJK 区及以后
        return 1.0
    if o == 0x20:           # 空格
        return 0.5
    return 0.55


def wrap_for_canvas(text: str, *, width: int, size: int,
                    margin: int = _SIDE_MARGIN, max_lines: int = 2) -> str:
    """按画布宽度给字幕文本插入 `\\N` 换行。

    ⚠️ **必须自己做，不能依赖 libass 的自动换行（WrapStyle）。**
       中文没有空格，libass 会把整串当一个不可断的"词"，
       放不下时不是换行而是**溢出画布 —— 超出部分直接被裁掉**，
       而且不产生任何警告。

       实测：18 字的字幕在 406px 宽、字号 42 的画面上，
       首尾各少了两个字（「拿个二维码过来叫我扫一下我付20块钱」
       渲染成「维码过来叫我扫一下我付2」）。

    **分行策略：先算需要几行，再按行数均匀分配**，而不是"填满一行再起一行"。
    机械填充会产出 `我上次去店里面吃 / 饭` 这种一个字独占一行的结果 ——
    同样是"能看但不体面"。

    `max_lines` 是**期望值**不是硬上限：放不下就多一行。
    宁可多一行，也不能丢字 —— 丢掉的是用户说的话。
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    avail = max(4.0, (width - margin * 2) / max(8, size))
    out_lines: list[str] = []

    for seg in raw.split(r"\N"):
        seg = seg.strip()
        if not seg:
            continue

        units = [_char_units(ch) for ch in seg]
        total_w = sum(units)
        if total_w <= avail:
            out_lines.append(seg)
            continue

        # ── ① 贪心分行：硬保证每行视觉宽度 <= avail ──────────
        greedy: list[str] = []
        cur, cur_w = "", 0.0
        for ch, w in zip(seg, units):
            if cur and cur_w + w > avail:
                greedy.append(cur)
                cur, cur_w = ch, w
            else:
                cur += ch
                cur_w += w
        if cur:
            greedy.append(cur)

        # ── ② 同样行数下做均匀分配（避免"最后一行只剩一个字"）──
        n = len(greedy)
        if n > 1:
            per = total_w / n
            even: list[str] = []
            cur, cur_w, made = "", 0.0, 0
            for ch, w in zip(seg, units):
                if cur and made < n - 1 and cur_w + w > per:
                    even.append(cur)
                    made += 1
                    cur, cur_w = ch, w
                else:
                    cur += ch
                    cur_w += w
            if cur:
                even.append(cur)
            # ③ 只有**每行都不超宽**才采用均匀版 ——
            #    均匀分配是"更好看"，不超宽是"不能丢字"，后者优先。
            if all(sum(_char_units(c) for c in ln) <= avail + 0.01
                   for ln in even):
                greedy = even

        out_lines.extend(greedy)

    return r"\N".join(out_lines)


def ass_filter_arg(ass_path: str | Path, *, cwd: str | Path | None = None,
                   fontsdir: str | Path | None = None) -> tuple[str, str | None]:
    """构造 `subtitles` 滤镜参数，返回 (vf 片段, 需要的工作目录)。

    **优先走「相对路径 + cwd」**：路径里没有冒号，就不需要任何转义。
    退路是绝对路径 + 双反斜杠转义（已实测有效，但容易写漏）。

    返回的第二个值是调用方应该传给 subprocess 的 cwd（可能为 None）。
    """
    p = Path(ass_path)
    use_cwd: str | None = None

    if cwd is not None:
        try:
            rel = p.resolve().relative_to(Path(cwd).resolve())
            arg = rel.as_posix()
            use_cwd = str(cwd)
        except ValueError:
            arg = _esc_filter_path(p)
    else:
        arg = _esc_filter_path(p)

    parts = [f"subtitles={arg}"]
    if fontsdir:
        parts.append(f"fontsdir={_esc_filter_path(fontsdir)}")
    return ":".join(parts), use_cwd


def _esc_filter_path(p: str | Path) -> str:
    """Windows 路径放进 ffmpeg 滤镜参数：冒号要 **双** 反斜杠。

    滤镜串要穿过两层解析（先按 `,;` 分 filtergraph，再按 `:` 分参数），
    单反斜杠在第一层就被消费掉了，到不了第二层。
    实测：单反斜杠、单引号都不行，只有 `\\\\:` 有效。
    """
    return str(p).replace("\\", "/").replace(":", r"\\:")


# ─────────────────────────────────────────────────────────────
# 系统字体
# ─────────────────────────────────────────────────────────────
# 已知中文字体关键字（用于在字体列表里把它们排前面）
_CJK_HINTS = (
    "yahei", "simhei", "simsun", "simkai", "kaiti", "fangsong", "deng",
    "source han", "sourcehan", "noto sans cjk", "noto serif cjk", "msjh",
    "mingliu", "lisu", "youyuan", "stkaiti", "stheiti", "fz", "方正",
    "黑体", "宋体", "楷体", "仿宋", "雅黑", "微软", "思源", "仓耳",
    "舟方", "圆体",
)

_FONT_CACHE: list[str] | None = None
_FONT_CJK: set[str] = set()


def is_cjk_font(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in _CJK_HINTS)


def list_fonts(refresh: bool = False) -> list[str]:
    """返回系统可用字体名列表，中文字体排在前面。

    Windows 上读注册表（比解析 ttf 文件头可靠，也快得多）：
      HKLM/HKCU\\...\\CurrentVersion\\Fonts 下的值名形如
      "SimHei (TrueType)" 或 "微软雅黑 & Microsoft YaHei UI (TrueType)"。
    取 `&` 前的主名、去掉 " (TrueType)" 后缀即为 libass 认识的 family name。
    """
    global _FONT_CACHE, _FONT_CJK
    if _FONT_CACHE is not None and not refresh:
        return list(_FONT_CACHE)

    names: set[str] = set()
    if sys.platform == "win32":
        try:
            import winreg
        except ImportError:
            winreg = None          # type: ignore[assignment]
        if winreg is not None:
            sub = (r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, sub) as key:
                        n = winreg.QueryInfoKey(key)[1]
                        for i in range(n):
                            try:
                                vname, _, _ = winreg.EnumValue(key, i)
                            except OSError:
                                continue
                            names.add(_clean_font_name(vname))
                except OSError:
                    continue

    # 注册表读不到（或非 Windows）时退回目录扫描
    if not names:
        for d in (Path(r"C:\Windows\Fonts"), Path.home() / ".fonts"):
            if d.is_dir():
                names.update(p.stem for p in d.glob("*.tt[fc]"))
                names.update(p.stem for p in d.glob("*.otf"))

    names.discard("")
    ordered = sorted(names, key=lambda s: (not is_cjk_font(s), s.lower()))
    _FONT_CACHE = ordered
    _FONT_CJK = {n for n in ordered if is_cjk_font(n)}
    return list(ordered)


def _clean_font_name(value_name: str) -> str:
    """注册表值名 → family name。"""
    s = re.sub(r"\s*\((?:TrueType|OpenType|All res)\)\s*$", "",
               value_name or "", flags=re.IGNORECASE)
    # "微软雅黑 & Microsoft YaHei UI" → 取第一个
    s = s.split("&")[0].strip()
    return s


def font_available(name: str) -> bool:
    """字体是否可用。

    ⚠️ 主要用于**给用户提示**：libass 在字体缺失时静默 fallback，
       不会报错也不会警告 —— 用户会以为"样式没生效"。
    """
    if not name:
        return False
    return name in set(list_fonts())


def suggest_font(name: str) -> str:
    """给一个不可用的字体名找一个兜底字体（优先中文字体）。"""
    fonts = list_fonts()
    if not fonts:
        return DEFAULT_FONT
    for cand in ("Microsoft YaHei", "SimHei", "SimSun", DEFAULT_FONT):
        if cand in fonts:
            return cand
    for f in fonts:
        if is_cjk_font(f):
            return f
    return fonts[0]


# ─────────────────────────────────────────────────────────────
# 预设存取
# ─────────────────────────────────────────────────────────────
_SAFE_NAME = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_\- ()（）]+")


def safe_name(name: str) -> str:
    """预设名 → 安全文件名（防路径穿越与非法字符）。"""
    s = _SAFE_NAME.sub("_", (name or "").strip())[:64].strip(" .")
    return s or "unnamed"


def preset_path(name: str) -> Path:
    return config.STYLES_DIR / f"{safe_name(name)}.json"


def builtin_default() -> SubtitlePreset:
    """**出厂**默认预设。

    与 `load_preset("default")` 的区别：
      - 本函数永远返回出厂值，用于「恢复默认」按钮
      - `load_preset("default")` 会先看磁盘上有没有用户改过的 default.json
        （用户在面板里调了默认样式，理应被记住）
    """
    return SubtitlePreset(name="default")


def list_presets() -> list[str]:
    """可用预设名列表，`default` 恒在首位。"""
    names: list[str] = ["default"]
    try:
        for p in sorted(config.STYLES_DIR.glob("*.json")):
            n = p.stem
            if n and n not in names:
                names.append(n)
    except OSError:
        pass
    return names


def load_preset(name: str) -> SubtitlePreset:
    """读一个预设。文件缺失/损坏退回出厂默认值（不抛异常）。"""
    p = preset_path(name or "default")
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            preset = SubtitlePreset.from_dict(data)
            if not preset.name:
                preset.name = name
            return preset
        except (OSError, json.JSONDecodeError):
            # 文件坏了不能让界面打不开 —— 退回出厂值，原文件留给用户排查
            return builtin_default()
    return builtin_default()


def save_preset(preset: SubtitlePreset) -> Path:
    """保存预设到磁盘。包含 `default` —— 用户改的默认样式要能记住。"""
    config.ensure_dirs()
    p = preset_path(preset.name)
    p.write_text(json.dumps(preset.to_dict(), ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def reset_preset(name: str) -> SubtitlePreset:
    """把预设恢复成出厂值（删掉磁盘上的覆盖）。"""
    delete_preset(name)
    return builtin_default() if safe_name(name) == "default" \
        else SubtitlePreset(name=name)


def delete_preset(name: str) -> bool:
    """删除一个预设文件。删除失败返回 False，**不抛异常**。

    ⚠️ 这里捕 BaseException 而不是 OSError：某些受管/安全策略环境下，
       删除操作会被拦截，且拦下来抛的是 SystemExit —— 它会让整个进程退出，
       界面直接消失。删不掉只是"没删掉"，不该升级成崩溃。
    """
    p = preset_path(name)
    try:
        if p.exists():
            p.unlink()
            return True
    except BaseException:                          # noqa: BLE001
        return False
    return False


def active_preset() -> SubtitlePreset:
    """取「当前生效」的预设。

    存的是**名字**在 settings.json 里；样式内容本身存在 styles/*.json。
    这样用户改样式时改的是预设文件，不会把 settings 撑大。
    """
    from . import settings
    name = str(settings.get("subtitle_preset", "default") or "default")
    return load_preset(name)


def set_active_preset(name: str) -> None:
    from . import settings
    settings.set("subtitle_preset", safe_name(name) or "default")


def save_as_active(preset: SubtitlePreset) -> None:
    """把当前样式存盘并设为生效预设。

    改的是哪套就存哪套 —— 包括 `default`：
    用户在面板里调了默认样式，下次打开必须还在，
    否则"调完一关就丢"会被当成 bug 报上来。
    """
    save_preset(preset)
    set_active_preset(preset.name)
