"""把**真实的 MainWindow** 按不同主题渲染成图片，用来看效果。

为什么要渲染真窗口而不是搭个假 demo
----------------------------------
界面改动"看描述不靠谱"。用一个自制的示例窗口截图，看到的是"我以为的样子"；
用**真的 MainWindow** 截图，看到的才是用户会看到的东西
（含侧栏、导航选中态、各页面的真实控件组合）。

用法：
    python stage0/probe_resolve/probe_theme_render.py
输出：
    C:\\temp\\sliceq_theme\\<mode>_<page>.png
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sliceq import _testing                       # noqa: E402

_TMP = _testing.isolate_data_root("themerender")  # ★ 不碰用户真实数据
_testing.assert_isolated()

from sliceq import config, store                  # noqa: E402
config.ensure_dirs()
store.init_db()

from PySide6.QtGui import QFont, QFontDatabase    # noqa: E402
from PySide6.QtWidgets import QApplication        # noqa: E402

from sliceq.ui import theme                       # noqa: E402
from sliceq.ui.main_window import MainWindow      # noqa: E402


def _ensure_cjk_font(app: QApplication) -> None:
    """offscreen 平台**不加载任何系统字体**（实测 `families()` 返回 0），
    中文会全渲染成方块 □。

    对本探针来说，装个假窗口截图是没用的 —— 必须能看见中文。
    办法：直接 `addApplicationFont` 把 Windows 字体文件注册进来。
    （**只在本探针里做**；产品运行时系统字体是正常的。）
    """
    if QFontDatabase.families():
        return                                     # 有字体就说明不是 offscreen
    for p in (r"C:\Windows\Fonts\msyh.ttc",
              r"C:\Windows\Fonts\msyhbd.ttc",
              r"C:\Windows\Fonts\simsun.ttc"):
        if os.path.exists(p):
            QFontDatabase.addApplicationFont(p)
    # Win11 中文界面实际用的就是这个 9pt
    app.setFont(QFont("Microsoft YaHei UI", 9))

OUT = Path(r"C:\temp\sliceq_theme")
OUT.mkdir(parents=True, exist_ok=True)

PAGES = [(0, "任务"), (1, "候选清单"), (2, "字幕样式"), (3, "设置")]

app = QApplication.instance() or QApplication(sys.argv[:1])
_ensure_cjk_font(app)

print(f"  隔离数据根: {_TMP}")
print(f"  字体族 {len(QFontDatabase.families())} 个 / 当前 {app.font().family()!r}")
print(f"  输出目录  : {OUT}")
print()

def shoot(tag: str, quiet: bool = False) -> None:
    """用当前的应用样式渲染一遍所有页面。"""
    win = MainWindow()
    win.resize(1180, 760)
    win.show()
    app.processEvents()
    for idx, name in PAGES:
        win.nav.setCurrentRow(idx)
        for _ in range(3):                     # 让布局/滚动稳定
            app.processEvents()
        pix = win.grab()
        path = OUT / f"{tag}_{idx}_{name}.png"
        pix.save(str(path))
        if not quiet:
            print(f"   {name:6s} -> {path.name}  {pix.width()}x{pix.height()}")
    win.close()
    win.deleteLater()
    app.processEvents()


# ⚠️ baseline **必须放在最前面** —— 一旦被主题改过 palette，就再也回不到
#    "纯系统默认"了。（第一版把它放在最后、只清了 QSS 和 style，
#    结果它带着前一组的深色 palette，与浅色比出 218/255 的假差异。）
print("=== 基线：完全未套主题（= v0.1.4 现状）===")
shoot("baseline")

# 记录系统原始调色板，供后续恢复
_ORIG_PALETTE = app.palette()

for mode in ("light", "dark"):
    actual = theme.apply(app, mode)
    print(f"\n=== 模式 {mode}（实际生效 {actual}）===")
    shoot(mode)

app.setPalette(_ORIG_PALETTE)
print("\n完成。")
