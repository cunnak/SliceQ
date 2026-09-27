# -*- coding: utf-8 -*-
"""任务状态落库的自测（阶段 4-D）。

## 为什么单独测这个

状态落库原来是写在 `clips_page` 的 UI 回调里的。加分析队列后暴露了问题：
队列走 `run_analysis`、**不经过那些回调**，于是任务状态永远停在「已导入」，
而且**不报任何错**。v1.17 把它下沉到 `pipeline.run()`。

四条路径里有三条**很难在真实端到端里构造**（要真出错、真取消、
真撞上确认点），所以用 mock 精确注入：

    正常返回        → 不该写错误状态（写状态是 e2e 覆盖的）
    抛异常          → 写「出错」
    抛 Cancelled    → 写「已取消」，**不是**出错
    落库本身失败    → 不能把主流程带崩

最后一条容易被忽略但很实际：状态只是记账，记不上不该让分析挂掉。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from sliceq import config, pipeline, store                  # noqa: E402

PASS = 0
FAIL = 0
WORK = Path(tempfile.mkdtemp(prefix="sliceq_status_"))


def ck(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK]   {name}" + (f"　—　{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}　—　{detail}")


def head(t: str) -> None:
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def make_task(name: str) -> tuple[int, str]:
    p = WORK / f"{name}.mp4"
    p.write_bytes(b"x" * 2048)
    return store.create_task(name, source_path=str(p), duration_sec=600), str(p)


def run(*a, **k):
    """调用 pipeline.run（参数在测试里是固定的）。"""
    return pipeline.run(*a, **k)


def main() -> int:
    store.init_db()
    config.ensure_dirs()
    created: list[int] = []
    orig_impl = pipeline._run_pipeline

    try:
        # ── 1. 常量与映射 ──────────────────────────────
        head("1. 阶段映射与状态常量")
        ck("_STAGE_STATUS 覆盖三个阶段",
           set(pipeline._STAGE_STATUS) == {"transcribe", "screen", "refine"},
           str(pipeline._STAGE_STATUS))
        ck("已取消是独立状态（不与出错混用）",
           config.STATUS_CANCELLED != config.STATUS_ERROR,
           f"{config.STATUS_CANCELLED} vs {config.STATUS_ERROR}")
        ck("已取消有中文标签",
           config.STATUS_LABELS.get(config.STATUS_CANCELLED) == "已取消",
           str(config.STATUS_LABELS.get(config.STATUS_CANCELLED)))

        # ── 2. _mark 本身 ──────────────────────────────
        head("2. _mark：写库 / 写库失败都不能出事")
        tid, video = make_task("mark测试")
        created.append(tid)
        ck("初始状态是已导入",
           (store.get_task(tid) or {}).get("status") == config.STATUS_IMPORTED,
           (store.get_task(tid) or {}).get("status"))
        pipeline._mark(tid, config.STATUS_REFINING)
        ck("_mark 能把状态写进去",
           (store.get_task(tid) or {}).get("status") == config.STATUS_REFINING,
           (store.get_task(tid) or {}).get("status"))

        real_set = store.set_task_status
        store.set_task_status = lambda *a, **k: (
            _ for _ in ()).throw(OSError("模拟：磁盘写不了"))

        def _call_mark() -> bool:
            try:
                pipeline._mark(tid, config.STATUS_READY)
                return True
            except BaseException:               # noqa: BLE001
                return False
        ck("★ 落库失败时 _mark 不抛异常（记账失败不该拖垮分析）",
           _call_mark() is True, "")
        store.set_task_status = real_set

        # ── 3. 抛异常 → 出错 ───────────────────────────
        head("3. 流水线抛异常 → 状态写「出错」")
        tid2, video2 = make_task("异常路径")
        created.append(tid2)

        def boom(*a, **k):
            raise RuntimeError("模拟：素材损坏")
        pipeline._run_pipeline = boom
        got_exc = ""
        try:
            run(tid2, video2, pipeline.Options(), object())
        except RuntimeError as exc:
            got_exc = str(exc)
        ck("异常照常向上抛（不吞掉）", "素材损坏" in got_exc, got_exc)
        ck("★ 状态被写成 error",
           (store.get_task(tid2) or {}).get("status") == config.STATUS_ERROR,
           (store.get_task(tid2) or {}).get("status"))

        # ── 4. 取消 → 已取消（不是出错）────────────────
        head("4. 用户取消 → 状态写「已取消」，不是「出错」")
        tid3, video3 = make_task("取消路径")
        created.append(tid3)

        def cancelled(*a, **k):
            raise pipeline.Cancelled("用户取消")
        pipeline._run_pipeline = cancelled
        try:
            run(tid3, video3, pipeline.Options(), object())
        except pipeline.Cancelled:
            pass
        st = (store.get_task(tid3) or {}).get("status")
        ck("★ 状态是 cancelled", st == config.STATUS_CANCELLED, str(st))
        ck("★ 不是 error（用户取消不该显示成程序出错）",
           st != config.STATUS_ERROR, str(st))
        ck("界面标签是「已取消」",
           config.STATUS_LABELS.get(st) == "已取消",
           str(config.STATUS_LABELS.get(st)))

        # ── 5. 正常返回不改状态 ────────────────────────
        head("5. 正常返回：不写错误状态（写作由流水线内部按阶段完成）")
        tid4, video4 = make_task("正常路径")
        created.append(tid4)
        pipeline._mark(tid4, config.STATUS_REFINING)      # 假装跑到了精析

        def ok_impl(task_id, *a, **k):
            return pipeline.RunResult(task_id=task_id, finished=True)
        pipeline._run_pipeline = ok_impl
        r = run(tid4, video4, pipeline.Options(), object())
        ck("返回值透传", isinstance(r, pipeline.RunResult) and r.finished is True, "")
        ck("★ 包装层没有把状态改成 error/failed",
           (store.get_task(tid4) or {}).get("status") not in
           (config.STATUS_ERROR,), (store.get_task(tid4) or {}).get("status"))

        # ── 6. 直接调 _run_pipeline 会被文档警告 ────────
        head("6. 主体函数的文档要点")
        # ⚠️ 先恢复真函数 —— 上面几节把它换成 mock 了。
        #    踩过：直接读 `pipeline._run_pipeline.__doc__` 读到的是 mock 的
        #    （mock 没有 docstring），于是报"文档缺失"，而其实是测试读错了对象。
        pipeline._run_pipeline = orig_impl
        doc = pipeline._run_pipeline.__doc__ or ""
        ck("★ _run_pipeline 的 docstring 明确写了「不要直接调用」",
           "不要直接调用" in doc,
           (doc.strip().splitlines() or ["（空）"])[0][:60])
        ck("run 的 docstring 说明了状态落库下沉的理由",
           "队列" in (pipeline.run.__doc__ or ""), "")
        ck("run 的 docstring 说明了取消不写出错",
           "取消" in (pipeline.run.__doc__ or ""), "")

    finally:
        pipeline._run_pipeline = orig_impl
        for i in created:
            try:
                store.delete_task(i)
            except BaseException:               # noqa: BLE001
                pass

    print(f"\n{'=' * 72}")
    print(f"通过 {PASS} / {PASS + FAIL}"
          + (f"　失败 {FAIL}" if FAIL else "　全部通过"))
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
