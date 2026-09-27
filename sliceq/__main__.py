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

    from PySide6.QtWidgets import QApplication
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setApplicationDisplayName(config.APP_DISPLAY_NAME)

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
