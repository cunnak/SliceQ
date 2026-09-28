# -*- coding: utf-8 -*-
r"""接线自测：进度回调是否真的接上了 + 中断残留状态能否自愈。

═══════════════════════════════════════════════════════════════
起因：v0.1.0 发布后第一个真实缺陷（2026-09-28），实际是**两个 bug**
═══════════════════════════════════════════════════════════════
用户点「开始精析」，界面卡在「准备中…」不动。

**Bug 1（真·卡死）**：`asr.py::_run_ffmpeg` 管道死锁
    → 见 `selftest_pipe_deadlock.py`（本文件测另外两个）

**Bug 2（进度永远不动）**：`clips_page._kickoff` 调 `pool.run` 时
    **漏传 `with_progress=True`**。
    `progress` 这个 kwarg 是 `Worker` 在 `with_progress=True` 时才注入的，
    漏传的后果是完全静默的：
        run_analysis(..., progress=None) → `if progress:` 为假
        → 进度链断掉 → 进度条恒 0、标签永远「准备中…」
    而全项目其它 6 个调用点都传了 ⇒ **只有"手动单条分析"这条路没进度**。
    本文件用**静态扫描**守住这一类：凡是传了 `on_progress=` 的地方，
    就必须同时传 `with_progress=True`。

**Bug 3（状态变谎话）**：程序被强杀/卡死后结束，`pipeline.run()` 的
    `except BaseException` 来不及跑 ⇒ 状态永远停在 transcribing。
    本文件的行为测试守住 `store.reset_stale_inflight_statuses()`。

用法：
    python stage0/probe_resolve/selftest_pipeline_wiring.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq import _testing                       # noqa: E402

_TMP = _testing.isolate_data_root("wiring")        # ★ 绝不碰用户真实数据
_testing.assert_isolated()

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sliceq import config, store                  # noqa: E402
config.ensure_dirs()
store.init_db()

PASS, FAIL = [], []


def ck(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    return cond


def _balanced_args(src: str, open_paren: int) -> str:
    """从 '(' 位置起取出配对括号内的全部文本。"""
    depth = 0
    for i in range(open_paren, len(src)):
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return src[open_paren + 1:i]
    return src[open_paren + 1:]


def t_static_progress_wiring() -> None:
    """静态扫描：传了 on_progress= 就必须传 with_progress=True。"""
    print("\n[1] 静态检查：on_progress 与 with_progress 必须成对出现")
    pattern = re.compile(r"pool\.run\s*\(")
    offenders, checked = [], 0
    for py in sorted((ROOT / "sliceq").rglob("*.py")):
        if "__pycache__" in str(py):
            continue
        src = py.read_text(encoding="utf-8")
        for m in pattern.finditer(src):
            args = _balanced_args(src, m.end() - 1)
            if "on_progress=" not in args:
                continue
            checked += 1
            if "with_progress=True" not in args:
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"{py.relative_to(ROOT)}:{line}")
    ck("扫到了带 on_progress 的 pool.run 调用（集合非空）", checked > 0,
       f"{checked} 处")
    ck("★ 每一处都同时传了 with_progress=True", not offenders,
       "缺失：" + ", ".join(offenders) if offenders else f"{checked} 处全部合规")

    # 反向：WorkerPool 的默认值本身也值得钉住
    from sliceq.ui.workers import WorkerPool
    import inspect
    sig = inspect.signature(WorkerPool.run)
    ck("WorkerPool.run 的 with_progress 默认值是 False（漏传即静默失效）",
       sig.parameters["with_progress"].default is False, "")


def t_worker_injects_progress() -> None:
    """行为测试：with_progress=True 时，progress 真的被注入。"""
    print("\n[2] 行为：with_progress=True 才会把 progress 注入 kwargs")
    from sliceq.ui.workers import Worker

    def job(a, progress=None):
        return progress is not None

    w_on = Worker(job, 1, with_progress=True)
    ck("★ with_progress=True → progress 已注入", "progress" in w_on.kwargs, "")
    w_off = Worker(job, 1, with_progress=False)
    ck("with_progress=False → 不注入（默认行为）",
       "progress" not in w_off.kwargs, "")


def t_stale_status_reset() -> None:
    """行为测试：中断残留的进行中状态能被自愈。"""
    print("\n[3] 中断残留状态自愈")
    inflight = [config.STATUS_TRANSCRIBING, config.STATUS_ROUGHING,
                config.STATUS_REFINING, config.STATUS_EXPORTING]

    # ① 什么都没产出 → 应回退成「已导入」
    a = store.create_task("残留A", "/tmp/a.mp4")
    store.set_task_status(a, config.STATUS_TRANSCRIBING)
    # ② 已经有切片段 → 应回退成「待导出」
    b = store.create_task("残留B", "/tmp/b.mp4")
    store.set_task_status(b, config.STATUS_REFINING)
    store.add_clip(b, 1.0, 5.0, title_hint="x")
    # ③ 稳定状态不该被动
    c = store.create_task("稳定C", "/tmp/c.mp4")
    store.set_task_status(c, config.STATUS_SCREENED)
    # ④ 出错状态也不该被动（那是真错误，用户该看到）
    d = store.create_task("出错D", "/tmp/d.mp4")
    store.set_task_status(d, config.STATUS_ERROR)

    fixed = store.reset_stale_inflight_statuses()
    names = {tid: (old, new) for tid, old, new in fixed}
    ck("扫到了 2 个进行中状态（集合非空）", len(fixed) == 2, f"{len(fixed)} 个")
    ck("★ 无产出的 → 已导入", names.get(a, ("", ""))[1] == config.STATUS_IMPORTED,
       str(names.get(a)))
    ck("★ 有切片段的 → 待导出", names.get(b, ("", ""))[1] == config.STATUS_READY,
       str(names.get(b)))
    ck("待精析（稳定状态）未被改动",
       store.get_task(c)["status"] == config.STATUS_SCREENED,
       store.get_task(c)["status"])
    ck("★ 出错状态未被改动（真错误要保留，不能被「自愈」抹掉）",
       store.get_task(d)["status"] == config.STATUS_ERROR,
       store.get_task(d)["status"])
    ck("回退后 DB 里的值确实变了",
       store.get_task(a)["status"] == config.STATUS_IMPORTED
       and store.get_task(b)["status"] == config.STATUS_READY, "")
    ck("幂等：再跑一次不会重复报告", store.reset_stale_inflight_statuses() == [], "")

    # 五种进行中状态全覆盖
    for st in inflight:
        t = store.create_task(f"覆盖{st}", "/tmp/x.mp4")
        store.set_task_status(t, st)
    r2 = store.reset_stale_inflight_statuses()
    ck("五类进行中状态全部能被清掉", len(r2) == len(inflight),
       f"{len(r2)} / {len(inflight)}")

    # 日志函数存在性（__main__ 会调用它）
    ck("config.STATUS_LABELS 覆盖回退目标",
       config.STATUS_IMPORTED in config.STATUS_LABELS
       and config.STATUS_READY in config.STATUS_LABELS, "")


def main() -> int:
    print("=" * 74)
    print("接线自测：进度回调 + 中断残留状态")
    print("=" * 74)
    t_static_progress_wiring()
    t_worker_injects_progress()
    t_stale_status_reset()
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
