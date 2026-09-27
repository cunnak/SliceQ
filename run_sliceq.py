# -*- coding: utf-8 -*-
"""PyInstaller 打包入口。

为什么需要这个文件而不是直接用 `sliceq/__main__.py`：
    `__main__.py` 里是**相对导入**（`from . import config`），
    PyInstaller 把它当顶层脚本执行时会抛
    `ImportError: attempted relative import with no known parent package`。
    这里用绝对导入把包当库拉进来，是唯一稳的入口写法。

开发时直接 `python -m sliceq` 即可，本文件只服务于打包。
"""
from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    # 打包后如果有任何子进程（ffmpeg 走 subprocess，不经这里），
    # 这一行是 Windows 上的标准保险，避免子进程重复拉起 GUI。
    multiprocessing.freeze_support()

    from sliceq.__main__ import main

    sys.exit(main())
