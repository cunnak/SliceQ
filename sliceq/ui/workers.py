# -*- coding: utf-8 -*-
"""线程基础设施：让长任务不阻塞 UI。

⚠️ PySide6 的硬规矩：
   1. **绝不在工作线程里碰任何 widget** —— 只发信号，UI 侧接。
   2. **QRunnable 本身不能带信号**（不是 QObject），所以要单独挂一个
      QObject 来承载信号。这是新手最常踩的坑。
   3. sqlite3 连接是**线程私有**的 —— store.py 用 threading.local()
      给每个线程各自开连接，所以工作线程里直接调 store 是安全的。
"""
from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class WorkerSignals(QObject):
    """信号载体。QRunnable 不是 QObject，信号必须挂在别的对象上。"""

    finished = Signal(object)          # 返回值
    error = Signal(str, str)           # (给用户看的中文说明, 技术细节)
    progress = Signal(int, int, str)   # (已完成, 总量, 描述)
    message = Signal(str)              # 阶段性文字提示


class Worker(QRunnable):
    """把一个可调用对象丢到线程池里跑。

    用法：
        w = Worker(fn, arg1, kw=1)
        w.signals.finished.connect(on_ok)
        w.signals.error.connect(on_err)
        pool.start(w)

    fn 可以接收一个 progress 回调：fn(..., progress=callable)
    """

    def __init__(self, fn: Callable, *args, with_progress: bool = False,
                 **kwargs) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self._cancelled = False
        self.setAutoDelete(True)

        if with_progress:
            self.kwargs["progress"] = self._emit_progress

    def _emit_progress(self, done: int, total: int, desc: str = "") -> None:
        self.signals.progress.emit(int(done), int(total), desc)

    def _should_cancel(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:                      # noqa: D102  (Qt 回调)
        # ⚠️ 这里必须捕 **BaseException**，不能只捕 Exception。
        #
        # SystemExit / KeyboardInterrupt 是 BaseException 的子类，不是 Exception。
        # 只捕 Exception 的话，它们会直接穿透 run()，于是：
        #     signals.finished 永远不会发出
        #     调用方（界面）永远停在"进行中"状态，而且没有任何错误提示
        #
        # 实测踩到：渲染线程里一次 unlink() 被环境的安全策略拦下，
        # 抛了 SystemExit(1)，界面上就永远显示"渲染中…"，无从排查。
        #
        # 保证"信号一定会发"比"区分异常类型"重要 —— 界面卡死是最糟的结果。
        try:
            # 把取消查询交给业务函数（如果它接受）
            if "should_cancel" in getattr(self.fn, "__code__", object()).co_varnames:
                self.kwargs.setdefault("should_cancel", self._should_cancel)
            result = self.fn(*self.args, **self.kwargs)
        except BaseException as exc:                # noqa: BLE001
            detail = traceback.format_exc()
            self.signals.error.emit(str(exc) or type(exc).__name__, detail)
        else:
            self.signals.finished.emit(result)


class WorkerPool:
    """线程池薄封装 —— 统一管理，方便退出时等待。

    ⚠️ **内部必须持有运行中 worker 的 Python 引用**（`_active`）。

       `Worker` 设了 `setAutoDelete(True)`，所有权在 C++ 侧。若 Python 包装
       对象在此刻被 GC，shiboken 可能连带销毁底层 C++ 对象，而线程池还在
       用它 —— 结果是**访问违例（段错误）**，而且崩溃栈里看不到任何业务代码，
       极难定位。

       所以调用方**不需要**再自己保存返回值；本类统一兜住。
       （实测踩到：调用方写 `pool.run(job, ...)` 而不用返回值，
        渲染过程中进程直接段错误。）
    """

    def __init__(self, max_threads: int = 4) -> None:
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(max_threads)
        self._active: set[Worker] = set()

    def run(self, fn: Callable, *args, on_done: Callable | None = None,
            on_error: Callable | None = None,
            on_progress: Callable | None = None,
            on_message: Callable | None = None,
            with_progress: bool = False, **kwargs) -> Worker:
        w = Worker(fn, *args, with_progress=with_progress, **kwargs)
        self._active.add(w)

        def _release(*_args: Any) -> None:
            # 运行结束（无论成败）才松手 —— 信号回调本身持有 w 的引用，
            # 保证在 C++ 侧销毁之前 Python 对象一直活着。
            self._active.discard(w)

        w.signals.finished.connect(_release)
        w.signals.error.connect(_release)

        if on_done:
            w.signals.finished.connect(on_done)
        if on_error:
            w.signals.error.connect(on_error)
        if on_progress:
            w.signals.progress.connect(on_progress)
        if on_message:
            w.signals.message.connect(on_message)
        self.pool.start(w)
        return w

    def wait(self, ms: int = 5000) -> bool:
        return self.pool.waitForDone(ms)


def guard_ui(fn: Callable) -> Callable:
    """装饰器：把 UI 回调里的异常记录下来而不是崩掉。

    Qt 的信号槽里抛异常默认会被吞或让进程直接挂掉，
    这里至少保证不会静默失败。

    ⚠️ **必须同时写进日志**（不能只 print_exc）：
    发布版是 `--windowed` 打包，双击启动时 **sys.stderr 是 None**，
    `traceback.print_exc()` 会写进一个不存在的地方 ⇒ 用户的崩溃
    在我们这边完全看不到。2026-09-28 干净虚拟机验证时踩到：
    侧栏环境提示条整块空白，而日志里一条错误都没有。
    """
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:
            traceback.print_exc()
            logging.getLogger("sliceq.ui").exception(
                "UI 回调 %s 抛异常（已吞，界面可能少画东西）",
                getattr(fn, "__qualname__", fn))
            return None
    return wrapper
