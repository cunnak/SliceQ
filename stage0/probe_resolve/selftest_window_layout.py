# -*- coding: utf-8 -*-
r"""主窗口布局自测：**小屏可用性**。

═══════════════════════════════════════════════════════════════
为什么需要这个测试
═══════════════════════════════════════════════════════════════
2026-09-28，在干净 Windows 11 虚拟机（1024x768）里验证发布版时发现：
侧栏底部的"环境提示条"**整块不可见**，日志里却一条错误都没有。

真因（靠 SLICEQ_DIAG=1 诊断 + 控件最小尺寸测量定位）：
    设置页的 5 个分组叠起来自然高度 ≈973px，而它**没有滚动区** ⇒
    `QStackedWidget` 把所有页面的最大最小尺寸当成自己的最小尺寸 ⇒
    主窗口最小高度被顶到 973、最小宽度顶到 1058。
    代码里的 `resize(1180, 720)` 因此**完全无效**。
    在 768p 屏（或 1080p @125% 缩放）上窗口比屏幕还大，
    **底部内容被裁到屏幕之外，而且拖不回来** ——
    用户够不到「FFmpeg 下载」入口，偏偏那是干净机器上必做的第一步。

⇒ 这类缺陷在开发者的大屏机器上**永远看不出来**，必须量最小尺寸。
   本自测就是把"窗口最小尺寸必须能塞进常见小屏"变成硬断言。

用法：
    python stage0/probe_resolve/selftest_window_layout.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sliceq import _testing                       # noqa: E402

_TMP = _testing.isolate_data_root("winlayout")    # ★ 绝不碰用户真实数据
_testing.assert_isolated()

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QScrollArea   # noqa: E402

from sliceq import config, store                   # noqa: E402
config.ensure_dirs()
store.init_db()

from sliceq.ui.main_window import MainWindow       # noqa: E402

PASS, FAIL = [], []


def ck(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    return cond


def main() -> int:
    print("=" * 74)
    print("主窗口布局自测（小屏可用性）")
    print("=" * 74)

    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.show()
    for _ in range(30):
        app.processEvents()

    msh = w.minimumSizeHint()
    print(f"\n[1] 最小尺寸")
    print(f"     MainWindow.minimumSizeHint() = {msh}")
    print(f"     实际 size                     = {w.size()}")

    # 768p 屏：可用高度约 768-48(任务栏) = 720；宽度 1024
    ck("★ 最小高度 ≤ 700（能放进 768p 屏，不留出屏）",
       msh.height() <= 700, f"实际 {msh.height()}")
    ck("★ 最小宽度 ≤ 1024（能放进 1024 宽的屏）",
       msh.width() <= 1024, f"实际 {msh.width()}")

    print("\n[2] 每个页面的最小尺寸（任何一个超标都会顶起整窗）")
    for i in range(w.stack.count()):
        pg = w.stack.widget(i)
        pm = pg.minimumSizeHint()
        print(f"     [{i}] {type(pg).__name__:14s} {pm}")
        ck(f"页面 {type(pg).__name__} 最小高度 ≤ 700",
           pm.height() <= 700, f"实际 {pm.height()}")

    print("\n[3] 页面内容必须可滚动（长表单不能撑高窗口）")
    ck("★ 设置页内容被 QScrollArea 包住",
       w.settings_page.findChild(QScrollArea) is not None, "")
    ck("设置页最小高度明显小于其自然高度",
       w.settings_page.minimumSizeHint().height()
       < w.settings_page.sizeHint().height(),
       f"min={w.settings_page.minimumSizeHint().height()} "
       f"hint={w.settings_page.sizeHint().height()}")

    print("\n[4] 侧栏环境提示条必须在窗口可见区域内")
    h = w.env_hint
    print(f"     text={h.text()!r}")
    print(f"     geo={h.geometry()}  window={w.size()}")
    ck("提示条有文案", bool(h.text().strip()), repr(h.text()[:40]))
    ck("提示条可见", h.isVisible(), "")
    ck("★ 提示条底边在窗口高度内（否则落在屏幕外）",
       h.geometry().bottom() <= w.size().height(),
       f"bottom={h.geometry().bottom()} window_h={w.size().height()}")
    ck("★ 提示条顶边不为负", h.geometry().top() >= 0,
       f"top={h.geometry().top()}")

    print("\n" + "=" * 74)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)} / 共 {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("   -", f)
    print("=" * 74)
    print("隔离目录:", _TMP)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
