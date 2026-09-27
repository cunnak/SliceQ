# -*- coding: utf-8 -*-
"""阶段 4-C 自测：并发执行器（F15）。

并发代码的缺陷属于"偶发、难复现"那一类，所以这里不测"跑起来对不对"，
而是测四条**不变量**：

  ① 并发不改变结果（与串行逐项对照）
  ② 单个任务失败不影响其它任务
  ③ **预算闸门在并发下仍然生效**（窗口化提交的核心价值）
  ④ 回调在调用线程执行（这样成本累计/落库都不需要加锁）

运行：python stage0/probe_resolve/selftest_concurrency.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
#    注意：**间接写入也算**（例如调 bridge.set_manual_path() 会写 settings.json）
#    —— 所以默认给所有自测都隔离，不做"看起来不碰数据"的判断。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from sliceq import concurrency  # noqa: E402

PASS = FAIL = 0


def ck(tag: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {tag}" + (f" :: {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {tag}" + (f" :: {detail}" if detail else ""))
    return ok


def head(t: str) -> None:
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


# ─────────────────────────────────────────────────────────────
def t_workers() -> None:
    head("1. 并发度收敛（用户填什么都不能炸）")
    e = concurrency.effective_workers
    ck("正常值原样通过", e(3, total=10) == 3, str(e(3, total=10)))
    ck("0 / 负数 → 串行", e(0, total=10) == 1 and e(-5, total=10) == 1,
       f"{e(0, total=10)} / {e(-5, total=10)}")
    ck("非数字 → 串行（保守）", e("abc", total=10) == 1, str(e("abc", total=10)))
    ck("None → 串行", e(None, total=10) == 1, str(e(None, total=10)))
    ck("超过任务数 → 收敛到任务数", e(99, total=3) == 3, str(e(99, total=3)))
    ck("有上限（防手滑填 1000）", e(1000, total=1000) <= 8,
       str(e(1000, total=1000)))
    ck("任务数为 0 时不出错", e(4, total=0) == 1, str(e(4, total=0)))
    ck("describe 给界面用", "串行" in concurrency.describe(1, 5)
       and "并发" in concurrency.describe(3, 5),
       f"{concurrency.describe(1, 5)} / {concurrency.describe(3, 5)}")


def t_same_result() -> None:
    head("2. ★ 并发不改变结果（与串行逐项对照）")
    tasks = list(range(24))

    def work(i: int, t: int):
        time.sleep(0.004)                  # 制造交错，暴露竞争
        return {"idx": i, "value": t * 2, "sq": t * t}

    baseline: dict[int, dict] = {}
    concurrency.run_tasks(tasks, work, workers=1,
                          on_result=lambda i, r: baseline.__setitem__(i, r))
    ck("串行：全部完成", len(baseline) == len(tasks), str(len(baseline)))

    for w in (2, 4, 8):
        got: dict[int, dict] = {}
        concurrency.run_tasks(tasks, work, workers=w,
                              on_result=lambda i, r: got.__setitem__(i, r))
        ck(f"workers={w}：条数一致", len(got) == len(tasks), str(len(got)))
        ck(f"workers={w}：每个结果与串行**逐项相同**", got == baseline,
           "" if got == baseline else
           f"差异 {[k for k in baseline if got.get(k) != baseline[k]][:3]}")
        ck(f"workers={w}：索引与值没有错位",
           all(got[i]["value"] == i * 2 for i in range(len(tasks))), "")


def t_error_isolation() -> None:
    head("3. 单个任务失败不影响其它（与 export_clips 同样的取舍）")
    tasks = list(range(12))

    def work(i: int, t: int):
        if i % 4 == 0:
            raise RuntimeError(f"boom-{i}")
        return i

    ok: dict[int, int] = {}
    errs: dict[int, str] = {}
    done = concurrency.run_tasks(
        tasks, work, workers=4,
        on_result=lambda i, r: ok.__setitem__(i, r),
        on_error=lambda i, e: errs.__setitem__(i, str(e)))

    ck("成功的都拿到了", len(ok) == 9, f"ok={len(ok)}")
    ck("失败的都被逐个报出", len(errs) == 3, f"errs={sorted(errs)}")
    ck("返回值只算成功数", done == 9, str(done))
    ck("成功项的值正确", all(v == k for k, v in ok.items()), "")
    ck("没有因为异常而中断整批", len(ok) + len(errs) == len(tasks),
       f"{len(ok)}+{len(errs)}")

    # 串行路径也要一样
    ok2: dict[int, int] = {}
    errs2: dict[int, str] = {}
    concurrency.run_tasks(tasks, work, workers=1,
                          on_result=lambda i, r: ok2.__setitem__(i, r),
                          on_error=lambda i, e: errs2.__setitem__(i, str(e)))
    ck("串行路径的异常处理与并发的结论一致",
       set(ok2) == set(ok) and set(errs2) == set(errs), "")


def t_budget_gate() -> None:
    head("4. ★★ 预算闸门在并发下必须仍然生效")
    print("     （这是'窗口化提交'的核心价值：一次性全提交会让闸门失效）")
    spent = [0.0]
    ran: list[int] = []

    def work(i: int, t: int):
        time.sleep(0.02)
        return 1.0                      # 每个任务花 1 元

    def on_result(i: int, r: float) -> None:
        spent[0] += r
        ran.append(i)

    def gate() -> bool:
        return spent[0] >= 3.0          # 花到 3 元就不再提交

    total = 30
    concurrency.run_tasks(list(range(total)), work, workers=3,
                          should_cancel=gate, on_result=on_result)

    ck("闸门生效：没有把 30 个全跑完", len(ran) < total, f"跑了 {len(ran)}/{total}")
    ck("没有超跑太多（最多多出 workers-1 个在途）", len(ran) <= 3 + 3,
       f"{len(ran)} 个（并发 3）")
    ck("确实跑够了下限", len(ran) >= 3, f"{len(ran)} 个")
    print(f"     实测：花了 {spent[0]:.0f} 元后停下，共跑 {len(ran)} 个任务")

    # 对照：串行路径下闸门同样生效
    spent2 = [0.0]
    ran2: list[int] = []
    concurrency.run_tasks(list(range(total)), work, workers=1,
                          should_cancel=lambda: spent2[0] >= 3.0,
                          on_result=lambda i, r: (spent2.__setitem__(0, spent2[0] + r),
                                                  ran2.append(i)))
    ck("串行路径同样在 3 元处停下", len(ran2) == 3, f"{len(ran2)} 个")


def t_thread_affinity() -> None:
    head("5. ★ 回调必须在调用线程（否则成本累计要到处加锁）")
    main_id = threading.get_ident()
    work_threads: set[int] = set()
    result_threads: set[int] = set()
    error_threads: set[int] = set()

    def work(i: int, t: int):
        work_threads.add(threading.get_ident())
        if i == 2:
            raise RuntimeError("x")
        return i

    concurrency.run_tasks(
        list(range(8)), work, workers=4,
        on_result=lambda i, r: result_threads.add(threading.get_ident()),
        on_error=lambda i, e: error_threads.add(threading.get_ident()))

    ck("工作确实换了线程", any(t != main_id for t in work_threads),
       f"{len(work_threads)} 个不同线程")
    ck("on_result 全在主线程", result_threads == {main_id},
       str(result_threads))
    ck("on_error 全在主线程", error_threads <= {main_id}, str(error_threads))


def t_cancel() -> None:
    head("6. 取消语义：不再提交新任务，跑着的让它跑完")
    started: list[int] = []

    def work(i: int, t: int):
        started.append(i)
        time.sleep(0.01)
        return i

    finished: list[int] = []
    concurrency.run_tasks(list(range(50)), work, workers=2,
                          should_cancel=lambda: len(started) >= 4,
                          on_result=lambda i, r: finished.append(i))

    ck("取消后停止提交（远小于 50）", len(started) < 50, f"启动 {len(started)}")
    ck("启动的都跑完了（不做半途中断）", len(finished) == len(started),
       f"启动 {len(started)} / 完成 {len(finished)}")


def t_edge_cases() -> None:
    head("7. 边界")
    ck("空任务列表不报错",
       concurrency.run_tasks([], lambda i, t: t, workers=4) == 0)
    ck("单任务 + 高并发也能跑完", concurrency.run_tasks(
        [7], lambda i, t: t * 3, workers=8,
        on_result=lambda i, r: None) == 1)
    got: list = []
    concurrency.run_tasks([1, 2, 3], lambda i, t: t, workers=8,
                          on_result=lambda i, r: got.append(r))
    ck("tasks 少于并发度时结果仍齐全", sorted(got) == [1, 2, 3], str(sorted(got)))
    ck("串行 + 立即取消 = 一个都不跑",
       concurrency.run_tasks([1, 2, 3], lambda i, t: t, workers=1,
                             should_cancel=lambda: True) == 0)


def main() -> int:
    t_workers()
    t_same_result()
    t_error_isolation()
    t_budget_gate()
    t_thread_affinity()
    t_cancel()
    t_edge_cases()
    print("\n" + "=" * 68)
    print(f"通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
