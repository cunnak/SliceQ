# -*- coding: utf-8 -*-
"""并发执行的小工具（阶段 4-C · F15）。

为什么单独一个模块：粗筛（按窗口）和精析（按候选）都要同一套语义。
两处各写一遍必然漂移 —— 而并发代码的漂移属于"偶发、难复现"那一类，
正是最不该有两份实现的地方。

## 三条设计约束

**① 回调必须在调用线程发生。**
成本累计、进度上报、落库都靠回调 —— 如果在 worker 线程里直接做这些，
就得给每个共享状态加锁，而"某个累加忘了加锁"这种缺陷是偶发且极难查的
（表现为"钱偶尔少算一点"，几乎不可能复现）。
让 worker 只负责**取数据**，所有状态变更回到调用线程做。

**② `workers <= 1` 走完全独立的串行路径，行为与并发前一致。**
这样"并发有没有改变结果"可以被直接对照验证，
而不是"看起来差不多"。也保证默认配置下零风险。

**③ 取消 = 不再提交新任务**，已在跑的让它跑完（结果照常回调）。
Python 里硬中断线程做不到；假装能做到只会制造半途状态
（发了半截请求、落了一半库）。宁可多等几秒。

## 不处理什么

不负责重试、不负责限流。这两件事在 `analyzer.py` 里已经有一份实现
（指数退避 + 429 处理），并发只是让它同时发生多次。
提高并发度会增加撞限流的概率 —— 所以默认值是保守的 2。
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable, Sequence

CancelCb = Callable[[], bool]
ResultCb = Callable[[int, Any], None]
ErrorCb = Callable[[int, BaseException], None]


def effective_workers(value: Any, *, total: int, cap: int = 8) -> int:
    """把用户填的并发度收敛到合理范围。

    - 非数字/负数/0 → 1（保守：宁可慢，不要意外的并发）
    - 超过任务数 → 收敛到任务数（开一堆空转线程没意义）
    - 超过 cap → 收敛到 cap（再高只会更容易撞限流）
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    if n < 1:
        return 1
    return max(1, min(n, cap, max(1, total)))


def run_tasks(tasks: Sequence, fn: Callable[[int, Any], Any], *,
              workers: int = 1,
              should_cancel: CancelCb | None = None,
              on_result: ResultCb | None = None,
              on_error: ErrorCb | None = None) -> int:
    """把 `tasks[i]` 交给 `fn(i, task)`，返回成功完成的个数。

    `fn` 抛出的 `Exception` 会交给 `on_error(i, exc)`，**不中断其余任务**
    （与 export_clips 的"单条失败不中断整批"同样的取舍）。

    ⚠️ `fn` 不要在里面改共享状态 —— 那是 `on_result` 的职责（它在调用线程跑）。
    """
    total = len(tasks)
    done = 0
    if total == 0:
        return 0

    workers = effective_workers(workers, total=total)

    # ── 串行路径（默认；也与并发路径可对照）──────────────
    if workers == 1:
        for i, t in enumerate(tasks):
            if should_cancel and should_cancel():
                break
            try:
                r = fn(i, t)
            except Exception as exc:             # noqa: BLE001
                if on_error:
                    on_error(i, exc)
                continue
            done += 1
            if on_result:
                on_result(i, r)
        return done

    # ── 并发路径 ────────────────────────────────────────
    #
    # ⚠️ **必须"窗口化提交"：保持最多 workers 个在途，每完成一个再补一个。**
    #    不能"先把 N 个任务一次性提交给线程池" —— 那样 `should_cancel`
    #    只在最开始被检查一次，之后所有任务都已经在队列里了。
    #    后果很实际：精析的**成本闸门**（花费超上限就停下问用户）
    #    是在 `should_cancel` 里做的 —— 一次性提交会让它**彻底失效**，
    #    用户设定的上限形同虚设，钱照花。
    #    窗口化提交让闸门在每个提交点都有机会生效。
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="sliceq-job") as ex:
        it = iter(range(total))
        pending: dict[Any, int] = {}

        def _submit_next() -> bool:
            """补一个任务进去（提交前过闸门）。返回是否真的提交了。"""
            for j in it:
                if should_cancel and should_cancel():
                    return False
                pending[ex.submit(fn, j, tasks[j])] = j
                return True
            return False

        for _ in range(workers):          # 先把并发度填满
            if not _submit_next():
                break

        while pending:
            done_set, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for fut in done_set:
                i = pending.pop(fut)
                try:
                    r = fut.result()
                except Exception as exc:     # noqa: BLE001
                    if on_error:
                        on_error(i, exc)
                else:
                    done += 1
                    if on_result:
                        # ★ 在调用线程执行 —— 成本累计/进度/落库都在这一侧
                        on_result(i, r)
                _submit_next()               # 完成一个补一个
    return done


def describe(workers: Any, total: int) -> str:
    """给界面用的一句话说明（"并发 2 路"这种）。"""
    n = effective_workers(workers, total=total)
    return "串行" if n == 1 else f"并发 {n} 路"
