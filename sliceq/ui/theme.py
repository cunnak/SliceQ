"""界面主题（浅色 / 深色 / 跟随系统）。


## 为什么需要这个模块

界面原来的颜色是**分散在 9 个文件、91 处 `setStyleSheet` 里的硬编码**，
而且 `MUTED = "#888780"` 这类常量在 **6 个文件里各写了一遍**。
没有统一入口 ⇒ 想加深色，必须先有一个"色板"。


## 两个必须知道的事实（实测，2026-09-29）

### ① 光设置 QSS **不够** —— 必须同时设置 QPalette

本项目**完全没用过 `QPalette`**。而有些控件**不吃 CSS**：
`QScrollBar` 的滚动条、`QMenu` 的右键菜单、`QTableWidget` 的空白区、
`QMessageBox`、`QToolTip`、`QSpinBox` 的上下箭头……
它们的颜色来自 **QPalette**。只改 QSS 的话，深色界面上会到处露出浅色块。

### ② 深色模式要切到 `Fusion` style

Qt 6.11 在 Windows 11 上默认 style 是 **`windows11`**（原生 Fluent 绘制）。
原生绘制的部分**不理会 CSS**，深色下会花。
`Fusion` 是 Qt 自绘风格，**完全跟随 QPalette + QSS**，
原生控件（勾选框、下拉箭头、滚动条）会自动变深，不需要手写 QSS 去模拟。

| 模式 | style | 说明 |
|---|---|---|
| 浅色 | `windows11` | **保持现状**，零观感回归 |
| 深色 | `Fusion` | 必须换，否则原生控件露白 |
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

#: 主题模式（用户可选项）
MODES = ("system", "light", "dark")

MODE_LABELS = {
    "system": "跟随系统",
    "light": "浅色",
    "dark": "深色",
}


@dataclass(frozen=True)
class Palette:
    """一套语义色板。

    ⚠️ 命名按**用途**，不按颜色值 —— 调用方只说"我要次要文字色"，
       具体是浅灰还是深灰由主题决定。
    """
    name: str
    # 背景层次（从外到内）
    window: str          # 窗口底
    panel: str           # 面板 / 侧栏 / 卡片
    field: str           # 输入框、表格底
    border: str
    # 文字
    text: str            # 主文字
    muted: str           # 次要文字（原 MUTED #888780）
    disabled: str
    # 语义色（提示/状态）
    accent: str          # 主色（链接、运行中）
    ok: str
    warn: str
    warn_hover: str      # 警告色的悬停/强调变体（原来硬编码 #8A5200）
    danger: str
    # 交互态
    hover: str           # 悬停叠加（半透明）
    selected: str        # 选中叠加（半透明）
    header: str          # 表头底

    def qcolor(self, key: str) -> QColor:
        v = getattr(self, key)
        return QColor(v)


# ─────────────────────────────────────────────────────────────
# 两套色值
# ─────────────────────────────────────────────────────────────
#: 浅色 —— 取值**刻意贴近现状**（白底、浅灰面板、原语义色），
#: 目标是"看起来和现在几乎一样"，避免主题化引入观感回归。
LIGHT = Palette(
    name="light",
    window="#ffffff",
    panel="#f6f6f7",
    field="#ffffff",
    border="#e3e3e5",
    text="#1a1c20",
    muted="#888780",
    disabled="#b8b8b4",
    accent="#378add",
    ok="#1d9e75",
    warn="#ba7517",
    warn_hover="#8a5200",
    danger="#e24b4a",
    hover="rgba(128,128,128,0.10)",
    selected="rgba(128,128,128,0.18)",
    header="#f2f2f3",
)

#: 深色 —— 中性深灰（不是纯黑）。
#: 语义色在原值基础上**提亮**：原 #1d9e75 / #e24b4a / #ba7517
#: 是给白底设计的，直接放到深底上对比度不足、发闷。
DARK = Palette(
    name="dark",
    window="#1e1f22",
    panel="#26282c",
    field="#1a1b1e",
    border="#3a3d42",
    text="#e3e4e6",
    muted="#9a9da4",
    disabled="#5c5f66",
    accent="#4c9eff",
    ok="#2fbf8f",
    warn="#e0a44c",
    warn_hover="#f5c274",
    danger="#f2625f",
    hover="rgba(255,255,255,0.07)",
    selected="rgba(255,255,255,0.13)",
    header="#2b2d31",
)

THEMES = {"light": LIGHT, "dark": DARK}


# ─────────────────────────────────────────────────────────────
# 组装
# ─────────────────────────────────────────────────────────────
def build_palette(p: Palette) -> QPalette:
    """QSS 覆盖不到的部分由它兜底（滚动条、菜单、表格空白、提示框…）。"""
    pal = QPalette()
    role = QPalette.ColorRole
    grp = QPalette.ColorGroup

    pal.setColor(role.Window, p.qcolor("window"))
    pal.setColor(role.WindowText, p.qcolor("text"))
    pal.setColor(role.Base, p.qcolor("field"))
    pal.setColor(role.AlternateBase, p.qcolor("panel"))
    pal.setColor(role.Text, p.qcolor("text"))
    pal.setColor(role.Button, p.qcolor("panel"))
    pal.setColor(role.ButtonText, p.qcolor("text"))
    pal.setColor(role.BrightText, p.qcolor("danger"))
    pal.setColor(role.Highlight, p.qcolor("accent"))
    pal.setColor(role.HighlightedText, "#ffffff")
    pal.setColor(role.ToolTipBase, p.qcolor("panel"))
    pal.setColor(role.ToolTipText, p.qcolor("text"))
    pal.setColor(role.PlaceholderText, p.qcolor("disabled"))
    pal.setColor(role.Link, p.qcolor("accent"))
    pal.setColor(role.Mid, p.qcolor("border"))
    pal.setColor(role.Dark, p.qcolor("border"))
    pal.setColor(role.Shadow, "#000000")

    # 禁用态要单独设 —— 否则 Fusion 会用一套奇怪的灰
    for r in (role.Text, role.WindowText, role.ButtonText):
        pal.setColor(grp.Disabled, r, p.qcolor("disabled"))
    pal.setColor(grp.Disabled, role.Base, p.qcolor("panel"))
    pal.setColor(grp.Disabled, role.Button, p.qcolor("panel"))
    return pal


def build_qss(p: Palette) -> str:
    """全局样式表。

    ⚠️ **刻意不覆盖** `QCheckBox::indicator` / `QComboBox::down-arrow` 这类
       原生绘制的图形 —— 用 `Fusion` + `QPalette` 让它们自动适配，
       手写 QSS 去模拟反而容易画歪。
       这里只管"面板色、文字色、边框、圆角"。
    """
    return f"""
    QWidget {{
        color: {p.text};
    }}
    QMainWindow, QDialog {{
        background: {p.window};
    }}
    QScrollArea, QStackedWidget {{
        background: {p.window};
    }}

    /* ── 分组框 ── */
    QGroupBox {{
        border: 1px solid {p.border};
        border-radius: 8px;
        margin-top: 10px;
        padding: 12px 10px 10px 10px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {p.muted};
    }}

    /* ── 按钮 ── */
    QPushButton {{
        background: {p.panel};
        border: 1px solid {p.border};
        border-radius: 6px;
        padding: 6px 14px;
    }}
    QPushButton:hover   {{ background: {p.header}; }}
    QPushButton:pressed {{ background: {p.selected}; }}
    QPushButton:disabled {{
        color: {p.disabled};
        background: {p.panel};
        border-color: {p.border};
    }}

    /* ── 输入类 ── */
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {{
        background: {p.field};
        border: 1px solid {p.border};
        border-radius: 6px;
        padding: 4px 6px;
        selection-background-color: {p.accent};
        selection-color: #ffffff;
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
    QSpinBox:focus, QDoubleSpinBox:focus {{
        border-color: {p.accent};
    }}
    QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {{
        color: {p.disabled};
        background: {p.panel};
    }}

    /* ── 下拉框 ── */
    QComboBox {{
        background: {p.field};
        border: 1px solid {p.border};
        border-radius: 6px;
        padding: 4px 8px;
    }}
    QComboBox:focus {{ border-color: {p.accent}; }}
    QComboBox QAbstractItemView {{
        background: {p.panel};
        border: 1px solid {p.border};
        selection-background-color: {p.accent};
        selection-color: #ffffff;
        outline: none;
    }}

    /* ── 表格 ── */
    QTableWidget, QTableView {{
        background: {p.field};
        alternate-background-color: {p.panel};
        border: 1px solid {p.border};
        border-radius: 6px;
        gridline-color: {p.border};
        selection-background-color: {p.selected};
        selection-color: {p.text};
        outline: none;
    }}
    QHeaderView::section {{
        background: {p.header};
        color: {p.muted};
        border: none;
        border-right: 1px solid {p.border};
        border-bottom: 1px solid {p.border};
        padding: 6px 8px;
    }}
    QTableCornerButton::section {{
        background: {p.header};
        border: none;
    }}

    /* ── 列表 ── */
    QListWidget {{
        background: transparent;
        border: none;
        outline: none;
    }}

    /* ── 进度条 ── */
    QProgressBar {{
        background: {p.panel};
        border: 1px solid {p.border};
        border-radius: 6px;
        text-align: center;
        color: {p.muted};
    }}
    QProgressBar::chunk {{
        background: {p.accent};
        border-radius: 5px;
    }}

    /* ── 菜单 / 提示 ── */
    QMenu {{
        background: {p.panel};
        border: 1px solid {p.border};
        border-radius: 6px;
        padding: 4px;
    }}
    QMenu::item {{ padding: 5px 22px 5px 12px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {p.selected}; }}
    QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 6px; }}

    QToolTip {{
        background: {p.panel};
        color: {p.text};
        border: 1px solid {p.border};
        padding: 3px 6px;
    }}

    /* ── 滚动区 ── */
    QScrollArea {{ border: none; }}
    """


# ─────────────────────────────────────────────────────────────
# 应用
# ─────────────────────────────────────────────────────────────
def system_mode() -> str:
    """当前系统是深色还是浅色。取不到就按浅色。"""
    try:
        from PySide6.QtGui import QGuiApplication
        scheme = QGuiApplication.styleHints().colorScheme()
        # Qt.ColorScheme.Dark == 1
        return "dark" if int(scheme) == 1 else "light"
    except Exception:                                  # noqa: BLE001
        return "light"


def resolve(mode: str) -> str:
    """把用户选项（含 system）解析成 'light' / 'dark'。"""
    if mode == "system":
        return system_mode()
    return mode if mode in THEMES else "light"


#: 本项目统一使用 Qt 自绘的 `Fusion` 风格。
#:
#: 为什么不用 Windows 11 原生的 `windows11` 风格：
#:   原生风格里**有一部分是系统绘制的，不理会 CSS**（下拉箭头、勾选框、
#:   滚动条、进度条槽…）。后果有两个：
#:     ① 深色下这些地方**会露白**，必须手写 QSS 去逐個模拟；
#:     ② 就算模拟出来，浅色（原生）与深色（自绘）**切换时观感会跳变**。
#:   ⇒ 统一 Fusion：**一套绘制逻辑跟随 QPalette**，浅深切换完全一致，
#:      而且原生控件会自动适配，不需要为深色单独造轮子。
#:      代价是浅色不再"长得很像原生 Windows 程序"（这是明确的取舍）。
STYLE_NAME = "Fusion"


# ─────────────────────────────────────────────────────────────
# 当前生效的色板 + 取色接口
# ─────────────────────────────────────────────────────────────
#: 当前生效的色板（模块级单例）
_current: Palette = LIGHT


def current() -> Palette:
    return _current


def color(key: str) -> str:
    """按语义键取当前主题的颜色，例如 `color("muted")`。

    ⚠️ **必须"用到时再取"，不能写成模块级常量。**
       反例（本项目原有的写法）：
           MUTED = "#888780"                 # 模块 import 时求值一次
           label.setStyleSheet(f"color:{MUTED}")
       模块只会 import 一次 ⇒ 切主题后这些值**永远停在旧主题**。
       正解：
           label.setStyleSheet(f"color:{theme.color('muted')}")
       （这也是为什么界面必须在切主题后**重建**：见 `needs_rebuild`。）
    """
    try:
        return getattr(_current, key)
    except AttributeError as exc:
        raise KeyError(f"色板里没有 '{key}'") from exc


#: 语义色快捷方式 —— 调用方读起来更顺
def text() -> str:      return color("text")
def muted() -> str:     return color("muted")
def accent() -> str:    return color("accent")
def ok() -> str:        return color("ok")
def warn() -> str:      return color("warn")
def warn_hover() -> str: return color("warn_hover")
def danger() -> str:    return color("danger")
def border() -> str:    return color("border")
def panel() -> str:     return color("panel")
def field() -> str:     return color("field")
def header() -> str:    return color("header")
def hover() -> str:     return color("hover")
def selected() -> str:  return color("selected")


def set_current(pal: Palette) -> None:
    global _current
    _current = pal


#: 最近一次实际设上去的**底层** style 类名（如 `QFusionStyle`）。
#: 供自测断言用 —— 因为查不到"当前正在用哪个 style"，只能记下来（见 `apply`）。
_applied_style: str = ""


def applied_style() -> str:
    """上一次 `apply()` 实际设上的底层 style 类名。"""
    return _applied_style


def apply(app: QApplication, mode: str = "system") -> str:
    """把主题套到整个应用上。返回**实际生效**的模式（'light' / 'dark'）。

    ⚠️ 调用之后，**已经创建好的窗口需要重建**才能完全生效 ——
       各页面里有大量 `setStyleSheet(f"color:{...}")` 是**构造时求值**的，
       全局 QSS 换掉不会回头改它们。
       见 `needs_rebuild()` 的说明。
    """
    global _applied_style
    actual = resolve(mode)
    pal = THEMES[actual]

    # ★ 先**卸掉**样式表再换 style。
    #   实测：设过 QSS 之后 `app.style()` 已经被 Qt 的代理 `QStyleSheetStyle`
    #   包住，此时再 `setStyle()` **不会真正替换底层 style**（第二次调用后
    #   读到的仍是 QStyleSheetStyle）。清空样式表再换才生效。
    #   同一函数内立刻重设新 QSS，中间不会渲染，用户看不到闪烁。
    app.setStyleSheet("")
    app.setStyle(STYLE_NAME)
    # ★ 在这一刻读一次并记下 —— 之后再想读就只剩代理了：
    #    `QStyleSheetStyle` 是个**新对象**，且 PySide6 下**没有 baseStyle()**
    #    可以穿透 ⇒ 事后无法判断"Fusion 到底生效了没有"。
    _applied_style = app.style().metaObject().className()

    app.setPalette(build_palette(pal))
    app.setStyleSheet(build_qss(pal))
    set_current(pal)
    return actual


def needs_rebuild() -> str:
    """给调用方的提醒文案（同时也是设计说明）。

    为什么切主题要重建窗口：
        本项目各页面有 **91 处** `setStyleSheet(...)`，颜色是构造时写死的。
        全局 QSS 只覆盖"没被局部样式覆盖过"的部分 ⇒ 切换后会出现
        "一半新主题、一半旧主题"的花屏。
        重建窗口是最可靠、也最省事的做法（代价 = 需要记住当前页索引）。
    """
    return "切换主题后需重建界面"
