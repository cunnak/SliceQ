# -*- coding: utf-8 -*-
"""多录像分析队列（阶段 4-D · F15 剩余项）。

## 为什么队列是「串行」的 —— 而且这次有实测数据

直觉上"两条录像同时分析"应该更快。实测（`stage0/probe_resolve/
probe_asr_concurrency.py` + `reports/assets/asr_concurrency_probe.json`）
证明在 GPU 上是**负收益**：

    场景        墙钟      加速     显存峰值
    串行 1 路    82.66s    1.00×    5209 MB
    并发 2 路    73.75s    1.12×    7106 MB
    并发 4 路   144.16s    0.57×    7541 MB   ← 比串行还慢 43%

单片耗时最说明问题：并发 4 路时片 0 从 23s 变成 144s —— 四个 whisper
进程在抢同一块 GPU，总计算量没变，只是多了上下文切换与显存换页。
（结果本身完全一致，md5 相同：并发不改结果，只是白花钱和时间。）

所以队列 = **一条接一条**，不并行。想加速应该调高**单条任务内部**的
粗筛/精析并发（那个是网络 IO 密集，另说）。

## 为什么队列不做持久化

队列只在本次运行有效，关掉程序就没了。三个理由：

1. **重启后的语义说不清**。持久化就要回答"上次退出时正在跑的那一条，
   现在算什么状态"——而那个状态的真相是"跑到一半"，既不能算完成也不能
   算未开始。
2. **代价很低**。pipeline 自己有断点续跑（转录分片缓存 + 粗筛窗口缓存），
   重开程序再点一次「分析」，已经跑过的部分不会重新花钱。
3. **不持久化反而更好懂**。用户下次打开看到"有 2 个任务在排队"却没在跑，
   比看不到队列更困惑。

界面上要**明确写出这一点**，不能让用户以为排了就一定会在某处等着。

## 单条失败不中断整队

与 `export_clips`「单条失败不中断整批」同样的取舍。
但**用户主动取消**是另一回事：那是"我不想跑了"，整队立刻停。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .. import config, pipeline, store
from .pages.clips_page import run_analysis

# 队列项状态
WAITING = "waiting"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

_STATE_LABELS = {
    WAITING: "等待",
    RUNNING: "进行中",
    DONE: "完成",
    FAILED: "失败",
    SKIPPED: "已跳过",
}


@dataclass
class QueueItem:
    """队列里的一项。只存 task_id —— 视频路径在执行时从库里现取，
    避免"加入队列时路径有效、真正跑到时文件已被移走"这种过期快照。"""

    task_id: int
    opts: pipeline.Options
    auto_continue: bool = False
    name: str = ""                  # 展示用快照（执行时以 store 为准）
    state: str = WAITING
    message: str = ""

    @property
    def state_label(self) -> str:
        return _STATE_LABELS.get(self.state, self.state)

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "name": self.name,
                "state": self.state, "state_label": self.state_label,
                "message": self.message}


# ─────────────────────────────────────────────────────────────
# 执行（模块级函数 —— Worker 靠 `fn.__code__.co_varnames` 注入参数）
# ─────────────────────────────────────────────────────────────
def run_queue(items: list[QueueItem], progress=None, should_cancel=None,
              on_item=None) -> list[dict]:
    """在**工作线程**里串行跑完整个队列。

    `progress(done, total, desc)` —— 描述格式为
        `[队列 2/3] [refine] 精析 5/20 · 已花 ¥0.12`
    整队进度把"当前是第几条"和"这条内部跑到哪"合成一个百分比。

    `on_item(index, state, message)` —— 每条状态变化时回调。
    ⚠️ 它在**工作线程**里被调用，只用来记日志，**不要在里面碰 widget**。
       界面侧的更新走 `AnalysisQueue` 的信号（那些在调用线程发）。

    返回每条的 {index, task_id, name, ok, state, message}。
    """
    n = len(items)
    out: list[dict] = []

    for i, it in enumerate(items):
        if should_cancel and should_cancel():
            it.state = SKIPPED
            it.message = "已取消"
            _notify(on_item, i, it)
            out.append(_snapshot(i, it, False))
            # 取消 = 剩下的全跳过，不继续跑
            for j in range(i + 1, n):
                items[j].state = SKIPPED
                items[j].message = "已取消"
                _notify(on_item, j, items[j])
                out.append(_snapshot(j, items[j], False))
            return out

        # 执行时才取路径 —— 见 QueueItem 的说明
        task = store.get_task(it.task_id)
        if not task:
            it.state = FAILED
            it.message = "任务已不存在"
            _notify(on_item, i, it)
            out.append(_snapshot(i, it, False))
            continue
        it.name = str(task.get("name") or it.name)

        video = task.get("source_path")
        if not video or not Path(video).exists():
            # 源文件没了：**不能静默跳过**。用户会以为分析过了。
            it.state = FAILED
            it.message = "源文件不在了（被移动或删除？）"
            _notify(on_item, i, it)
            try:
                store.set_task_status(it.task_id, config.STATUS_ERROR)
            except BaseException:              # noqa: BLE001
                pass
            out.append(_snapshot(i, it, False))
            continue

        it.state = RUNNING
        it.message = "已开始"
        _notify(on_item, i, it)

        # 把"第几条"和"条内进度"合成整队百分比。
        # ⚠️ 描述里**带上任务名** —— 队列跑几条录像时，
        #    "队列 2/3" 只说得出位置，用户还得回头看表格才知道是哪一条。
        def _mk_progress(idx: int, name: str):
            def _p(done: int, total: int, desc: str = "") -> None:
                if not progress:
                    return
                try:
                    inner = max(0.0, min(1.0, float(done) / float(total or 100)))
                except (TypeError, ValueError, ZeroDivisionError):
                    inner = 0.0
                overall = (idx + inner) / max(n, 1)
                progress(int(round(overall * 100)), 100,
                         f"[队列 {idx + 1}/{n}] {name} · {desc}")
            return _p

        try:
            run_analysis(it.task_id, video, it.opts, it.auto_continue, False,
                         progress=_mk_progress(i, it.name),
                         should_cancel=should_cancel)
        except pipeline.Cancelled as exc:
            # 用户主动取消 → 整队停
            it.state = SKIPPED
            it.message = str(exc) or "已取消"
            _notify(on_item, i, it)
            out.append(_snapshot(i, it, False))
            for j in range(i + 1, n):
                items[j].state = SKIPPED
                items[j].message = "已取消"
                _notify(on_item, j, items[j])
                out.append(_snapshot(j, items[j], False))
            return out
        except BaseException as exc:           # noqa: BLE001
            # ★ 单条失败**不中断整队** —— 与 export_clips 同样的取舍。
            #   三条录像排着，第一条因为文件损坏失败，不该让后两条也不跑。
            it.state = FAILED
            it.message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            _notify(on_item, i, it)
            try:
                store.set_task_status(it.task_id, config.STATUS_ERROR)
            except BaseException:              # noqa: BLE001
                pass
            out.append(_snapshot(i, it, False))
            continue

        it.state = DONE
        it.message = "分析完成"
        _notify(on_item, i, it)
        out.append(_snapshot(i, it, True))

    return out


def _notify(on_item, i: int, it: QueueItem) -> None:
    if on_item:
        try:
            on_item(i, it.state, it.message)
        except BaseException:                  # noqa: BLE001
            pass


def _snapshot(i: int, it: QueueItem, ok: bool) -> dict:
    return {"index": i, "task_id": it.task_id, "name": it.name,
            "ok": ok, "state": it.state, "message": it.message}


def summarize(results: list[dict]) -> str:
    """给界面用的一句话总结。"""
    if not results:
        return "队列为空"
    ok = sum(1 for r in results if r["state"] == DONE)
    bad = sum(1 for r in results if r["state"] == FAILED)
    skip = sum(1 for r in results if r["state"] == SKIPPED)
    parts = [f"完成 {ok}/{len(results)}"]
    if bad:
        parts.append(f"失败 {bad}")
    if skip:
        parts.append(f"跳过 {skip}")
    return "，".join(parts)


# ─────────────────────────────────────────────────────────────
# UI 侧的状态容器
# ─────────────────────────────────────────────────────────────
class AnalysisQueue(QObject):
    """队列的状态与信号。**执行本身在 `run_queue` 里**，这里只管界面联动。

    ⚠️ 队列**只在内存里** —— 关程序就没了。理由见模块头注释第 2 节。
       界面必须把这一点说出来，别让用户以为排了就会在某个地方等着。
    """

    added = Signal()                     # 队列内容变化（增/删/清）
    started = Signal(int)                # 整队开始（总条数）
    progress = Signal(int, int, str)     # (完成, 总量, 描述)
    finished = Signal(str)                # 总结文字
    failed = Signal(str, str)            # 整队级别的错误（如素材全不存在）

    def __init__(self, pool, parent=None) -> None:
        super().__init__(parent)
        self.pool = pool
        self._items: list[QueueItem] = []
        self._worker = None
        self._running = False
        self._results: list[dict] = []

    # ── 队列内容 ───────────────────────────────────────
    def items(self) -> list[QueueItem]:
        return list(self._items)

    def count(self) -> int:
        return len(self._items)

    def is_running(self) -> bool:
        return self._running

    def has_task(self, task_id: int) -> bool:
        return any(it.task_id == task_id for it in self._items)

    def add(self, task_id: int, opts: pipeline.Options,
            auto_continue: bool = False) -> bool:
        """加入队尾。已在队列里则返回 False（不重复排队）。"""
        if self.has_task(task_id):
            return False
        task = store.get_task(task_id) or {}
        self._items.append(QueueItem(
            task_id=task_id,
            # ★ 每个任务一份**独立的 Options 副本**，不能共享同一个对象。
            #
            #   `pipeline.run()` 在"超预算 → 用户授权"时会**原地修改**它：
            #       opts.hard_limit_cny *= 2
            #   共享对象的话，第一条任务被授权放宽上限后，
            #   后面**所有**任务的上限也跟着翻倍 —— 用户以为每条都是 ¥2，
            #   实际后排的是 ¥4，而且账单一出来才发现。
            #
            #   这类"改一处、影响一片"在批量场景下几乎不会被察觉，
            #   所以宁可多拷贝一份。
            opts=replace(opts),
            auto_continue=auto_continue,
            name=str(task.get("name") or f"任务 {task_id}")))
        self.added.emit()
        return True

    def remove(self, index: int) -> bool:
        """移出队列。正在跑的那条**不允许**移除 —— 移除它没有任何办法
        真正停下来，只会让界面状态和实际执行对不上。"""
        if not (0 <= index < len(self._items)):
            return False
        it = self._items[index]
        if it.state == RUNNING:
            return False
        self._items.pop(index)
        self.added.emit()
        return True

    def clear(self) -> int:
        """清空**未开始**的项。跑完的留着当结果看。"""
        keep = [it for it in self._items
                if it.state in (RUNNING, DONE, FAILED)]
        removed = len(self._items) - len(keep)
        self._items = keep
        if removed:
            self.added.emit()
        return removed

    def reset(self) -> None:
        """清空全部（含已完成项）。运行中不允许。"""
        if self._running:
            return
        self._items.clear()
        self._results = []
        self.added.emit()

    # ── 执行 ───────────────────────────────────────────
    def start(self) -> bool:
        if self._running or not self._items:
            return False
        pending = [it for it in self._items if it.state in (WAITING,)]
        if not pending:
            return False

        self._running = True
        self._results = []
        total = len(pending)
        self.started.emit(total)

        # 只跑"等待中"的项：已完成/失败的不重复跑
        # （用户想重跑就重新加一次，而不是让队列偷偷替他决定）
        jobs = list(pending)

        # 进度与结果都经 Worker 的 Qt 信号回到主线程。
        # ⚠️ 队列项自身状态（waiting→running→done）在工作线程里变化，
        #    界面**不能**靠读 QueueItem 来刷新（会读到中间态且无通知）——
        #    所以界面在 `finished` / `added` 时重画，中间过程看进度条即可。
        self._worker = self.pool.run(
            run_queue, jobs,
            on_progress=self.progress.emit,
            on_done=self._on_done,
            on_error=self._on_error,
            with_progress=True)
        return True

    def _on_done(self, results: list) -> None:
        self._running = False
        self._results = list(results or [])
        self.finished.emit(summarize(self._results))
        self.added.emit()          # 让界面重画一次（状态标签要更新）

    def _on_error(self, msg: str, detail: str) -> None:
        self._running = False
        self.failed.emit(msg, detail)
        self.added.emit()

    def cancel(self) -> None:
        """请求停止：当前这条也会在下一个检查点退出，后面的全跳过。"""
        if self._worker is not None:
            self._worker.cancel()

    def results(self) -> list[dict]:
        return list(self._results)
