# -*- coding: utf-8 -*-
"""应用入口：python -m sliceq

启动顺序有讲究：
  1. 先建目录、初始化数据库 —— 后面所有模块都假定它们存在
  2. 再起 QApplication
  3. 最后开主窗口

日志在 QApplication 之前配好，这样启动期的错误也能落盘。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import config


def _setup_logging() -> None:
    config.ensure_dirs()
    log_file = config.LOGS_DIR / "sliceq.log"

    handler = RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # 控制台也输出一份，方便开发期看
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(console)


def main() -> int:
    _setup_logging()
    log = logging.getLogger("sliceq")
    log.info("SliceQ v%s 启动 | 数据目录 %s", config.VERSION, config.APP_ROOT)

    from . import store
    store.init_db()
    log.info("数据库就绪：%s", config.DB_PATH)

    # ── 启动自检：上次被打断留下的「进行中」状态是谎话 ──────────
    # 程序被强杀 / 卡死后结束 / 断电时，pipeline 的 except 根本来不及跑，
    # 状态就永远停在 transcribing 之类。用户下次看到「转录中」却不动，
    # 只会一头雾水。这里回退到有数据支撑的那一档。
    # 清理失败不能影响启动 —— 状态只是记账。
    try:
        for tid, old, new in store.reset_stale_inflight_statuses():
            log.info("任务 %s 的状态 %s → %s（上次运行被中断，实际没在跑）",
                     tid, old, new)
    except BaseException:                      # noqa: BLE001
        log.exception("清理中断残留状态时出错（已忽略，不影响启动）")

    from PySide6.QtWidgets import QApplication
    from .ui.main_window import MainWindow
    from .ui import theme

    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setApplicationDisplayName(config.APP_DISPLAY_NAME)

    # ★ 主题必须在**创建任何窗口之前**套上。
    #   各页面里有 90+ 处 `setStyleSheet(f"color:{...}")` 是构造时求值的 ——
    #   先建窗口再换主题，那些颜色就停在旧主题上了。
    try:
        from . import settings
        mode = settings.get("ui_theme", "system")
    except Exception:                          # noqa: BLE001
        log.exception("读取主题设置失败（回退到跟随系统）")
        mode = "system"
    actual = theme.apply(app, mode)
    log.info("界面主题：%s（设置 %s）", theme.MODE_LABELS.get(actual, actual), mode)

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
