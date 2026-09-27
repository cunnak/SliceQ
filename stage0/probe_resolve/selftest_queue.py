# -*- coding: utf-8 -*-
"""分析队列自测（阶段 4-D）。

用 mock 替换 `run_analysis` 来**精确注入**要测的场景，不依赖真实模型调用：

    · 正常完成
    · 单条抛异常  → 不中断整队
    · 用户取消    → 整队停，剩余标 skipped
    · 源文件丢失  → 记失败 + 置 error 状态 + 继续

要守住的不变量（队列最容易出错的地方）：

    ① **串行** —— 任意时刻只能有 1 条在跑。
       实测证明 GPU 上并发转录是负收益，所以"队列真的没并行"是核心承诺。
    ② **顺序** —— 完成顺序 == 加入顺序（结果与界面编号对得上）
    ③ **进度** —— 单调不减、落在 0~100、描述带 `[队列 i/n]`
    ④ **失败隔离** —— 一条挂了后面的照跑
    ⑤ **取消** —— 整队停，不留"半死不活"的 waiting 项
    ⑥ **不重复排队** —— 同一个 task 加两次只有一项
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from PySide6.QtWidgets import QApplication                # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from sliceq import config, pipeline, store                # noqa: E402
from sliceq.ui import queue as Q                          # noqa: E402

PASS = 0
FAIL = 0
NOTES: list[str] = []
WORK = Path(tempfile.mkdtemp(prefix="sliceq_queue_"))


def ck(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK]   {name}" + (f"　—　{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}　—　{detail}")


def head(t: str) -> None:
    print(f"\n{'=' * 74}\n{t}\n{'=' * 74}")


# ─────────────────────────────────────────────────────────────
# mock：把 run_analysis 换成可编排的假实现
# ─────────────────────────────────────────────────────────────
_live = 0
_peak = 0
_lock = threading.Lock()
SCRIPT: dict[int, str] = {}       # task_id -> ok / fail / cancel
ORDER: list[int] = []             # 实际执行顺序
EVENTS: list[tuple] = []          # 进度事件


def fake_run_analysis(task_id, video, opts, auto_continue,
                      user_started_refine, progress=None,
                      should_cancel=None):
    """替身。只做三件事：记并发峰值/顺序、按剧本报进度、按剧本抛异常。"""
    global _live, _peak
    with _lock:
        _live += 1
        _peak = max(_peak, _live)
        ORDER.append(task_id)
    try:
        mode = SCRIPT.get(task_id, "ok")
        for k in range(1, 4):                    # 报三次进度
            if progress:
                progress(int(k / 3 * 100), 100,
                         f"[refine] {task_id} 第 {k}/3 步")
            time.sleep(0.01)
        if mode == "fail":
            raise pipeline.PipelineError(f"任务 {task_id} 故意失败")
        if mode == "cancel":
            raise pipeline.Cancelled("用户取消")
        return pipeline.RunResult(task_id=task_id, finished=True)
    finally:
        with _lock:
            _live -= 1


# ─────────────────────────────────────────────────────────────
def make_task(name: str, *, exists: bool = True) -> int:
    if exists:
        p = WORK / f"{name}.mp4"
        p.write_bytes(b"x" * 2048)
        return store.create_task(name, source_path=str(p), duration_sec=600)
    return store.create_task(name, source_path=str(WORK / f"{name}_missing.mp4"),
                             duration_sec=600)


def reset() -> None:
    global _live, _peak
    with _lock:
        _live = 0
        _peak = 0
        ORDER.clear()
        EVENTS.clear()


def mk_items(ids: list[int]) -> list[Q.QueueItem]:
    return [Q.QueueItem(task_id=i, opts=pipeline.Options(), auto_continue=False)
            for i in ids]


def collect(done, total=100, desc: str = "") -> None:
    """收集进度描述。

    ⚠️ 签名必须是 (done, total, desc) 三个 —— 这是 `run_queue` 的接口约定。
       第一版写成 `collect(desc)` 一个参数，一调用就 TypeError，
       然后被 run_queue 的 `except BaseException` 吞掉、记成"任务失败"。
       这正好演示了异常兜底的副作用：**把"接口用错"伪装成"任务出错"**。
    """
    EVENTS.append(desc)


# ─────────────────────────────────────────────────────────────
def main() -> int:
    Q.run_analysis = fake_run_analysis          # ★ 注入 mock
    store.init_db()
    config.ensure_dirs()

    # ── 1. 三条正常任务 ────────────────────────────────
    head("1. 三条正常任务：串行、按序、进度可达 100")
    reset()
    ids = [make_task(f"正常{i}") for i in range(3)]
    items = mk_items(ids)
    res = Q.run_queue(items, progress=collect)
    ck("三条全部完成", len(res) == 3 and all(r["ok"] for r in res),
       str([r["state"] for r in res]))
    ck("★ 全程串行（峰值同时在跑 = 1）", _peak == 1, f"峰值 {_peak}")
    ck("执行顺序 == 加入顺序", ORDER == ids, f"{ORDER} vs {ids}")
    ck("每条状态都是 done", all(it.state == Q.DONE for it in items),
       str([it.state for it in items]))

    nums = []
    for d in EVENTS:
        if d.startswith("[队列 "):
            nums.append(int(d.split("[队列 ")[1].split("/")[0]))
    ck("进度描述带 [队列 i/n] 前缀", bool(nums), f"首条：{EVENTS[0] if EVENTS else '无'}")
    ck("队列序号按 1,2,3 递增且不倒退", nums == sorted(nums) and
       nums[0] == 1 and nums[-1] == 3, str(sorted(set(nums))))
    ck("描述里带任务名（用户不必回看表格）",
       all("正常" in d for d in EVENTS if d.startswith("[队列 ")),
       EVENTS[0] if EVENTS else "")

    pcts: list[int] = []

    def _p(done, total, desc=""):
        pcts.append(done)

    reset()
    Q.run_queue(mk_items(ids), progress=_p)
    ck("进度百分比非负且不超过 100", all(0 <= p <= 100 for p in pcts),
       f"min={min(pcts)} max={max(pcts)}" if pcts else "无")
    ck("进度百分比单调不减", all(b >= a for a, b in zip(pcts, pcts[1:])),
       f"{pcts[:8]}...")
    ck("三条跑完后进度到过 100", max(pcts) == 100 if pcts else False,
       f"max={max(pcts) if pcts else None}")
    ck("整队进度确实跨过 1/3 与 2/3 这两个分界（不是单条内循环）",
       any(30 <= p <= 40 for p in pcts) and any(60 <= p <= 72 for p in pcts),
       f"{sorted(set(pcts))}")

    # ── 2. 单条失败不中断整队 ───────────────────────────
    head("2. 中间那条失败：后面的照跑（失败隔离）")
    reset()
    ids2 = [make_task(f"隔离{i}") for i in range(3)]
    SCRIPT.clear()
    SCRIPT[ids2[1]] = "fail"
    items2 = mk_items(ids2)
    res2 = Q.run_queue(items2)
    ck("三条都被执行过（没在失败处停住）", ORDER == ids2, str(ORDER))
    ck("第 1 条 done", res2[0]["state"] == Q.DONE, res2[0]["state"])
    ck("第 2 条 failed", res2[1]["state"] == Q.FAILED, res2[1]["state"])
    ck("第 3 条仍然 done", res2[2]["state"] == Q.DONE, res2[2]["state"])
    ck("失败原因被记录（不是空字符串）", bool(res2[1]["message"]),
       res2[1]["message"])
    ck("失败任务的状态被置为 error",
       (store.get_task(ids2[1]) or {}).get("status") == config.STATUS_ERROR,
       (store.get_task(ids2[1]) or {}).get("status"))
    ck("总结文字说清了几成几败",
       "完成 2/3" in Q.summarize(res2) and "失败 1" in Q.summarize(res2),
       Q.summarize(res2))

    # ── 3. 用户取消 ────────────────────────────────────
    head("3. 第 2 条遇到「用户取消」：整队停，剩余标 skipped")
    reset()
    ids3 = [make_task(f"取消{i}") for i in range(4)]
    SCRIPT.clear()
    SCRIPT[ids3[1]] = "cancel"
    items3 = mk_items(ids3)
    res3 = Q.run_queue(items3)
    ck("第 1 条 done", res3[0]["state"] == Q.DONE, res3[0]["state"])
    ck("第 2 条 skipped（不是 failed）", res3[1]["state"] == Q.SKIPPED,
       res3[1]["state"])
    ck("第 3、4 条也 skipped", res3[2]["state"] == Q.SKIPPED
       and res3[3]["state"] == Q.SKIPPED,
       f"{res3[2]['state']},{res3[3]['state']}")
    ck("★ 取消后没有继续执行新的任务", ORDER == ids3[:2], str(ORDER))
    ck("结果条数 == 队列条数（不漏项）", len(res3) == 4, str(len(res3)))

    # ── 4. 源文件丢失 ──────────────────────────────────
    head("4. 源文件不在了：记失败 + 置 error + 不中断整队")
    reset()
    ids4 = [make_task("有文件"), make_task("没文件", exists=False),
            make_task("也有文件")]
    items4 = mk_items(ids4)
    res4 = Q.run_queue(items4)
    ck("丢失的那条 failed", res4[1]["state"] == Q.FAILED, res4[1]["state"])
    ck("★ 原因说的是「源文件」，能看懂",
       "源文件" in res4[1]["message"], res4[1]["message"])
    ck("另外两条正常完成",
       res4[0]["state"] == Q.DONE and res4[2]["state"] == Q.DONE,
       f"{res4[0]['state']},{res4[2]['state']}")
    ck("不会去执行缺文件的那条", ids4[1] not in ORDER, str(ORDER))

    # ── 5. 队列容器行为（不涉及执行）────────────────────
    head("5. 队列容器：去重 / 移除 / 清空")
    q = Q.AnalysisQueue(pool=None)
    t_a, t_b, t_c = (make_task(f"容器{i}") for i in range(3))
    ck("加入成功", q.add(t_a, pipeline.Options()) is True)
    ck("★ 同一个任务加两次只留一项", q.add(t_a, pipeline.Options()) is False
       and q.count() == 1, f"count={q.count()}")
    q.add(t_b, pipeline.Options())
    q.add(t_c, pipeline.Options())
    ck("三条都在队列里", q.count() == 3, str(q.count()))
    ck("has_task 认得出来", q.has_task(t_b) and not q.has_task(99999))
    ck("移除未开始的项成功", q.remove(1) is True and q.count() == 2)
    ck("越界移除返回 False", q.remove(99) is False)
    ck("清空只清未开始的项", q.clear() == 2 and q.count() == 0, str(q.count()))

    # ★ Options 必须是每个任务独立的副本
    #   `pipeline.run()` 在"超预算授权"时会原地改 opts.hard_limit_cny。
    #   共享同一个对象的话，第一条被授权后会污染后面所有任务的上限。
    t_x, t_y = make_task("副本A"), make_task("副本B")
    q4 = Q.AnalysisQueue(pool=None)
    shared = pipeline.Options(hard_limit_cny=2.0)
    q4.add(t_x, shared)
    q4.add(t_y, shared)
    ck("★ 两个任务拿到的是不同对象（不是共享同一个 Options）",
       q4.items()[0].opts is not q4.items()[1].opts, "")
    shared.hard_limit_cny *= 2                 # 模拟 pipeline 的原地修改
    ck("★ 外部对原对象的修改不会串进已入队的项",
       q4.items()[0].opts.hard_limit_cny == 2.0,
       f"{q4.items()[0].opts.hard_limit_cny}")
    ck("两个副本之间也互不影响",
       q4.items()[0].opts.hard_limit_cny == q4.items()[1].opts.hard_limit_cny
       == 2.0,
       f"{q4.items()[0].opts.hard_limit_cny}/{q4.items()[1].opts.hard_limit_cny}")

    # 运行中的项不许移除（移除它没办法真正停下来）
    q2 = Q.AnalysisQueue(pool=None)
    t_r = make_task("运行中")
    q2.add(t_r, pipeline.Options())
    q2._items[0].state = Q.RUNNING
    ck("★ 运行中的项不允许移除（避免界面与实际执行脱节）",
       q2.remove(0) is False and q2.count() == 1)

    # ── 6. 空队列 ──────────────────────────────────────
    head("6. 空队列不能崩")
    r = Q.run_queue([])
    ck("空队列返回空列表", r == [], str(r))
    ck("空队列的总结可读", Q.summarize([]) == "队列为空", Q.summarize([]))
    q3 = Q.AnalysisQueue(pool=None)
    ck("没有项时 start() 返回 False", q3.start() is False)

    # ── 7. 取消标志（worker 注入的那条路）────────────────
    head("7. 外部取消标志：一开始就为真 → 立即全跳过")
    reset()
    ids7 = [make_task(f"标志{i}") for i in range(2)]

    def _cancelled() -> bool:
        return True
    res7 = Q.run_queue(mk_items(ids7), should_cancel=_cancelled)
    ck("全部 skipped", all(r["state"] == Q.SKIPPED for r in res7),
       str([r["state"] for r in res7]))
    ck("一个都没执行", ORDER == [], str(ORDER))

    # ── 8. 队列项不与 store 快照过期 ────────────────────
    head("8. 路径在执行时现取（不依赖加入时的快照）")
    reset()
    t8 = make_task("改名测试")
    it8 = Q.QueueItem(task_id=t8, opts=pipeline.Options())
    # 加入后把库里的名字改掉 —— 执行时应当拿到新名字
    store.rename_task(t8, "改过的名字")
    Q.run_queue([it8])
    ck("★ 用了库里的最新名字（不是加入时的快照）",
       it8.name == "改过的名字", it8.name)

    # ── 清理 ──────────────────────────────────────────
    for i in (ids + ids2 + ids3 + ids4 + [t_a, t_b, t_c, t_r, t8, t_x, t_y]
              + ids7):
        try:
            store.delete_task(i)
        except BaseException:                     # noqa: BLE001
            pass

    print(f"\n{'=' * 74}")
    print(f"通过 {PASS} / {PASS + FAIL}"
          + (f"　失败 {FAIL}" if FAIL else "　全部通过"))
    print("=" * 74)
    for n in NOTES:
        print("·", n)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
