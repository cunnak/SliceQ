# -*- coding: utf-8 -*-
r"""界面主题自测（深色 / 浅色 / 跟随系统）。

═══════════════════════════════════════════════════════════════
这个自测守什么
═══════════════════════════════════════════════════════════════
**A 静态**：`sliceq/ui/` 下**不得再有硬编码颜色**。
   改造前有 91 处 `setStyleSheet` 散在 9 个文件里，
   `MUTED = "#888780"` 这种常量在 **6 个文件里各写了一遍**。
   深色主题做不成的根本原因就是这个 —— 颜色没有单一来源。
   ⇒ 守住这条，以后新增界面代码才会自动跟随主题。

**B 行为**：`theme.apply()` 是否真的换掉了调色板与 style；
   `color()` 是否跟随当前主题；`resolve()` 的 system 分支是否正确。

**C ★ 渲染**：把**真实的 MainWindow** 渲染出来，用像素统计判断
   "深色下有没有大块亮区 / 浅色下有没有大块暗区"。
   —— 光看代码判断不了"有没有控件漏了主题"，必须看像素。
   **并且带对照组**：不套主题时判据必须**报告异常**（证明它有牙）。

用法：
    python stage0/probe_resolve/selftest_theme.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sliceq import _testing                       # noqa: E402

_TMP = _testing.isolate_data_root("theme")         # ★ 不碰用户真实数据
_testing.assert_isolated()

from sliceq import config, store                  # noqa: E402
config.ensure_dirs()
store.init_db()

from PySide6.QtGui import QFont, QFontDatabase    # noqa: E402
from PySide6.QtWidgets import QApplication        # noqa: E402

from sliceq.ui import theme                       # noqa: E402
from sliceq.ui.main_window import MainWindow      # noqa: E402

PASS, FAIL = [], []


def ck(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    return cond


# ═══════════════════════════════════════════════════════════════
# A. 静态：ui/ 下不得有硬编码颜色
# ═══════════════════════════════════════════════════════════════
print("\n=== A 静态检查：界面代码里不得有硬编码颜色 ===")

#: 允许出现颜色字面量的地方（都必须有理由）
ALLOW = {
    # 色板本身 —— 这里就是颜色的唯一来源
    "theme.py",
    # 字幕预览的深色底板：它模拟的是**视频画面**，不是窗口面板，刻意不跟主题
    "style_preview.py",
}

HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


def scan_hardcoded(ui_dir: Path) -> list[tuple[str, int, str]]:
    bad = []
    for f in sorted(ui_dir.rglob("*.py")):
        if f.name in ALLOW:
            continue
        src = f.read_text(encoding="utf-8")
        for m in HEX.finditer(src):
            ln = src[:m.start()].count("\n") + 1
            line = src.split("\n")[ln - 1].strip()
            if line.startswith("#"):        # 纯注释
                continue
            bad.append((f.as_posix(), ln, line[:90]))
    return bad


hits = scan_hardcoded(ROOT / "sliceq" / "ui")
for path, ln, line in hits:
    print(f"        ★ {path}:{ln}  {line}")
ck("★ sliceq/ui/ 下没有硬编码颜色（除色板与预览底板）",
   not hits, f"残留 {len(hits)} 处")

# 对照组：造一个假的 ui/ 目录，硬编码必须被检出、白名单必须被跳过
import tempfile                                  # noqa: E402

with tempfile.TemporaryDirectory() as _td:
    _fake = Path(_td)
    (_fake / "fake_page.py").write_text(
        'lbl.setStyleSheet("color:#123456;")\n', encoding="utf-8")
    (_fake / "theme.py").write_text('A = "#123456"\n', encoding="utf-8")
    _ctl = scan_hardcoded(_fake)

ck("★ 对照组：硬编码被检出（证明检查有牙）",
   len(_ctl) == 1 and _ctl[0][0].endswith("fake_page.py"),
   f"检出 {len(_ctl)} 处：{[c[0] for c in _ctl]}")
ck("对照组：白名单文件被跳过（theme.py 不报）",
   all("theme.py" not in p for p, _, _ in _ctl), "")

# 反证：确认确实有大量地方在用 theme（否则可能"颜色被删光了"）
n_theme_refs = sum(
    len(re.findall(r"\btheme\.\w+\(", f.read_text(encoding="utf-8")))
    for f in (ROOT / "sliceq" / "ui").rglob("*.py"))
ck("★ 确有大量代码通过 theme 取色", n_theme_refs >= 40, f"{n_theme_refs} 处")


# ═══════════════════════════════════════════════════════════════
# B. 行为
# ═══════════════════════════════════════════════════════════════
print("\n=== B 行为：apply / resolve / color ===")


def _ensure_cjk(app: QApplication) -> None:
    """offscreen 不加载系统字体（中文会变方块）。够用即可。"""
    if QFontDatabase.families():
        return
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc"):
        if os.path.exists(p):
            QFontDatabase.addApplicationFont(p)
    app.setFont(QFont("Microsoft YaHei UI", 9))


app = QApplication.instance() or QApplication(sys.argv[:1])
_ensure_cjk(app)

a1 = theme.apply(app, "dark")
ck("apply('dark') 返回 dark", a1 == "dark", a1)
def _real_style() -> str:
    """底层 style 类名。

    ⚠️ **不能**读 `app.style()` —— 设过样式表之后它返回的是 Qt 自动包的
       代理 `QStyleSheetStyle`，且 PySide6 下没有 `baseStyle()` 可穿透。
       `theme.apply()` 会在 `setStyleSheet` 之前把真实值记下来。
    """
    return theme.applied_style()


def _style_name() -> str:
    return f"记录={_real_style()!r} / 现在读到={app.style().metaObject().className()}"


ck("dark 下 style 是 Fusion（原生绘制不理会 CSS）",
   "Fusion" in _real_style(), _style_name())
ck("dark 下窗口底色确实变深",
   app.palette().color(app.palette().ColorRole.Window).lightness() < 80,
   str(app.palette().color(app.palette().ColorRole.Window).name()))
ck("dark 下文字色是浅色（不会白底白字）",
   app.palette().color(app.palette().ColorRole.WindowText).lightness() > 150,
   str(app.palette().color(app.palette().ColorRole.WindowText).name()))
ck("dark 下 color('muted') 与浅色不同",
   theme.muted() != theme.LIGHT.muted, theme.muted())

a2 = theme.apply(app, "light")
ck("apply('light') 返回 light", a2 == "light", a2)
ck("light 下窗口底色是浅色",
   app.palette().color(app.palette().ColorRole.Window).lightness() > 200,
   str(app.palette().color(app.palette().ColorRole.Window).name()))
ck("★ 深浅切换时 style 保持一致（用户选了「统一风格」）",
   "Fusion" in _real_style(), _style_name())

ck("resolve('system') 落在 light/dark 里",
   theme.resolve("system") in ("light", "dark"), theme.resolve("system"))
ck("resolve 未知值回退到 light", theme.resolve("nonsense") == "light", "")
ck("三档模式与中文标签齐备",
   set(theme.MODES) == {"system", "light", "dark"}
   and all(m in theme.MODE_LABELS for m in theme.MODES), str(theme.MODES))

# 两套色板字段必须一一对应（漏一个字段会导致某处取不到色）
ck("★ 两套色板字段完全一致",
   set(vars(theme.LIGHT)) == set(vars(theme.DARK)),
   str(set(vars(theme.LIGHT)) ^ set(vars(theme.DARK))))
# 语义色在深色下必须比浅色亮（否则深底上发闷）
for k in ("ok", "warn", "danger", "accent", "muted"):
    from PySide6.QtGui import QColor as _QC
    l = _QC(getattr(theme.LIGHT, k)).lightness()
    d = _QC(getattr(theme.DARK, k)).lightness()
    ck(f"语义色 {k} 在深色下更亮（浅 {l} → 深 {d}）", d > l, f"{l} → {d}")


# ═══════════════════════════════════════════════════════════════
# C. 渲染：真实窗口的像素判据
# ═══════════════════════════════════════════════════════════════
print("\n=== C 渲染：真实 MainWindow 的像素检查 ===")

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

OUT = _TMP / "shots"
OUT.mkdir(parents=True, exist_ok=True)
PAGES = [(0, "任务"), (3, "设置")]


def shoot(tag: str) -> dict[str, Path]:
    win = MainWindow()
    win.resize(1180, 760)
    win.show()
    app.processEvents()
    paths = {}
    for idx, name in PAGES:
        win.nav.setCurrentRow(idx)
        for _ in range(3):
            app.processEvents()
        p = OUT / f"{tag}_{idx}.png"
        win.grab().save(str(p))
        paths[name] = p
    win.close()
    win.deleteLater()
    app.processEvents()
    return paths


def big_ratio(path: Path, bright: bool, thr: int = 180) -> float:
    """缩小 8 倍（平均池化）后统计极端像素占比。

    文字细笔画会被池化掉，剩下的 = **大块区域**
    ⇒ 深色下若有大块亮区，说明有控件没跟随主题。
    """
    im = Image.open(path).convert("L")
    sm = im.resize((im.width // 8, im.height // 8), Image.BOX)
    d = list(sm.get_flattened_data())
    if bright:
        return sum(1 for v in d if v > thr) / len(d)
    return sum(1 for v in d if v < 75) / len(d)


if not _HAS_PIL:
    ck("PIL 可用（渲染判据需要）", False, "未安装 Pillow")
else:
    theme.apply(app, "dark")
    dark_paths = shoot("dark")
    for name, p in dark_paths.items():
        r = big_ratio(p, bright=True)
        ck(f"★ 深色「{name}」页无大块亮区", r < 0.01, f"亮区 {r*100:.2f}%")

    theme.apply(app, "light")
    light_paths = shoot("light")
    for name, p in light_paths.items():
        r = big_ratio(p, bright=False)
        ck(f"★ 浅色「{name}」页无大块暗区", r < 0.01, f"暗区 {r*100:.2f}%")

    # ── ★ 对照组：不套主题时，判据必须报异常（否则判据没牙）──
    app.setStyleSheet("")
    app.setStyle("Fusion")
    app.setPalette(app.style().standardPalette())     # 回到"没主题"的样子
    theme.set_current(theme.LIGHT)                    # 让取色退化成浅色
    base_paths = shoot("baseline")
    base_r = big_ratio(base_paths["设置"], bright=True)
    ck("★ 对照组：无主题（浅底）被判定为「有大块亮区」",
       base_r > 0.5, f"亮区 {base_r*100:.2f}%（应 > 50%，否则判据没区分度）")

# ═══════════════════════════════════════════════════════════════
# D. 切换流程：设置页改主题 ⇒ 主窗口重建
# ═══════════════════════════════════════════════════════════════
print("\n=== D 切换流程：改主题 ⇒ 重建界面 ===")

from PySide6.QtWidgets import QMessageBox        # noqa: E402

from sliceq import settings                       # noqa: E402

theme.apply(app, "light")
settings.set("ui_theme", "light")

win = MainWindow()
win.resize(1100, 700)
win.show()
win.nav.setCurrentRow(2)                          # 停在「字幕样式」页
app.processEvents()
old_central = win.centralWidget()
old_sub = win.clips_page.subtitle.styleSheet()

combo = win.settings_page.theme_combo
combo.setCurrentIndex(combo.findData("dark"))
app.processEvents()

ck("改主题后写入了设置", settings.get("ui_theme") == "dark",
   repr(settings.get("ui_theme")))
ck("★ 界面被重建（centralWidget 换成新对象）",
   win.centralWidget() is not old_central, "")
ck("★ 当前页索引被保留（用户不会被弹回第一页）",
   win.nav.currentRow() == 2, str(win.nav.currentRow()))
new_sub = win.clips_page.subtitle.styleSheet()
ck("★ 页面里的局部样式跟着换了（否则会半新半旧、看得出花屏）",
   new_sub != old_sub and theme.DARK.muted in new_sub,
   f"{old_sub!r} → {new_sub!r}")

win.close()
win.deleteLater()
app.processEvents()

# ── 忙碌时必须拒绝切换（否则进度显示会凭空消失）──
theme.apply(app, "light")
settings.set("ui_theme", "light")
win2 = MainWindow()
win2.show()
app.processEvents()
old2 = win2.centralWidget()

_orig_info = QMessageBox.information
QMessageBox.information = staticmethod(lambda *a, **k: None)   # 别弹窗卡住测试
try:
    win2._is_busy = lambda: True                  # 打桩：假装有任务在跑
    c2 = win2.settings_page.theme_combo
    c2.setCurrentIndex(c2.findData("dark"))
    app.processEvents()
finally:
    QMessageBox.information = _orig_info

ck("★ 有任务在跑时拒绝重建（否则进度会凭空消失）",
   win2.centralWidget() is old2, "")
ck("★ 被拒后下拉框退回已保存值（界面不说谎）",
   win2.settings_page.theme_combo.currentData() == "light",
   str(win2.settings_page.theme_combo.currentData()))
ck("被拒时也不该把设置写进去",
   settings.get("ui_theme") == "light", repr(settings.get("ui_theme")))

win2.close()
win2.deleteLater()
app.processEvents()


# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("  失败项：")
    for n in FAIL:
        print(f"    - {n}")
sys.exit(1 if FAIL else 0)
