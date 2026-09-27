# -*- coding: utf-8 -*-
"""分析队列的 GUI 层自测（阶段 4-D）。

覆盖界面与 `AnalysisQueue` 的联动，以及几个"看起来对但会咬人"的细节：

  · 队列面板默认隐藏，有内容才出现（不白占版面）
  · 多选能真正取到多个 task_id
  · **状态列的同步不能重建表格** —— 重建会丢掉用户的多选
    （队列跑着的时候他很可能正在挑下一批），也会把滚动位置弹回去
  · 加入队列的前置检查：无选中 / 都已在队列 / 源文件缺失
  · 停止与清空按钮的可用状态跟着队列状态走
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from PySide6.QtCore import Qt                              # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox     # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from sliceq import config, pipeline, store                  # noqa: E402
from sliceq.ui.main_window import MainWindow                # noqa: E402

PASS = 0
FAIL = 0
WORK = Path(tempfile.mkdtemp(prefix="sliceq_qgui_"))

# 模态弹窗在离屏下会阻塞 —— 全部拦掉并记账
POPUPS: list[tuple] = []
QMessageBox.information = staticmethod(
    lambda *a, **k: POPUPS.append(("info", a[2] if len(a) > 2 else "")))
QMessageBox.warning = staticmethod(
    lambda *a, **k: POPUPS.append(("warn", a[2] if len(a) > 2 else "")))
QMessageBox.question = staticmethod(
    lambda *a, **k: (POPUPS.append(("ask", a[2] if len(a) > 2 else "")),
                     QMessageBox.Yes)[1])


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


def make_task(name: str, *, exists: bool = True) -> int:
    p = WORK / f"{name}.mp4"
    if exists:
        p.write_bytes(b"x" * 2048)
    return store.create_task(name, source_path=str(p), duration_sec=600)


def main() -> int:
    store.init_db()
    config.ensure_dirs()
    w = MainWindow()
    w.resize(1280, 880)
    w.show()
    app.processEvents()
    tp = w.tasks_page
    q = w.analysis_queue

    created: list[int] = []

    # ── 1. 面板初始状态 ────────────────────────────────
    head("1. 队列面板的初始状态")
    ck("队列为空时面板隐藏", not tp.queue_box.isVisible())
    ck("停止按钮初始不可用", not tp.queue_stop_btn.isEnabled())
    ck("表格是多选模式",
       tp.table.selectionMode().name == "ExtendedSelection" or
       tp.table.selectionMode() == 2, str(tp.table.selectionMode()))

    # ── 2. 多选取 id ───────────────────────────────────
    head("2. 表格多选能取到多个 task_id")
    ids = [make_task(f"多选{i}") for i in range(3)]
    created += ids
    tp.refresh()
    app.processEvents()
    sm = tp.table.selectionModel()
    for r in range(min(3, tp.table.rowCount())):
        sm.select(tp.table.model().index(r, 0),
                  sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
    app.processEvents()
    got = tp._selected_ids()
    ck("选中 3 行取到 3 个 id", len(got) == 3, f"{got}")
    ck("取到的 id 都在刚建的这三个里", set(got).issubset(set(ids)), f"{got}")

    # ── 3. 加入队列 → 面板出现 ─────────────────────────
    head("3. 加入队列后面板出现，项与状态正确")
    for i in ids:
        q.add(i, pipeline.Options())
    app.processEvents()
    ck("面板已显示", tp.queue_box.isVisible())
    ck("列表项数 == 队列长度", tp.queue_list.count() == 3,
       f"{tp.queue_list.count()}")
    first = tp.queue_list.item(0).text()
    ck("列表项文字含任务名与状态", "多选" in first, first)
    ck("清空按钮可用（有未开始的项）", tp.queue_clear_btn.isEnabled())
    ck("队列没在跑时停止按钮不可用", not tp.queue_stop_btn.isEnabled())

    # ── 4. 进度信号驱动界面 ────────────────────────────
    head("4. 进度信号：状态文字 + 进度条 + 状态列同步")
    q.started.emit(3)
    app.processEvents()
    ck("开始后停止按钮可用", tp.queue_stop_btn.isEnabled())
    ck("状态文字说明了总数", "3" in tp.queue_status.text(),
       tp.queue_status.text())

    q.progress.emit(33, 100, "[队列 1/3] 多选0 · [transcribe] 转录中 5/20")
    app.processEvents()
    ck("进度条被更新", tp.queue_bar.value() == 33, str(tp.queue_bar.value()))
    ck("状态文字显示的是队列描述",
       tp.queue_status.text().startswith("[队列 1/3]"),
       tp.queue_status.text()[:50])

    # ★ 状态列同步不能重建表格
    tp.table.clearSelection()
    sm.select(tp.table.model().index(1, 0),
              sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
    app.processEvents()
    rows_before = tp.table.rowCount()
    cells_before = [tp.table.item(r, 0).data(Qt.UserRole)
                    for r in range(rows_before)]
    tp._last_col_sync = 0.0              # 绕过节流
    tp._sync_table_status()
    app.processEvents()
    rows_after = tp.table.rowCount()
    cells_after = [tp.table.item(r, 0).data(Qt.UserRole)
                   for r in range(rows_after)]
    ck("★ 状态列同步没有重建表格（行数不变）", rows_before == rows_after,
       f"{rows_before} → {rows_after}")
    ck("★ 状态列同步后 task_id 顺序没变（没换过行）",
       cells_before == cells_after, f"{cells_before[:4]} vs {cells_after[:4]}")
    ck("★ 状态列同步后用户的选择还在（没被清掉）",
       len(tp._selected_ids()) == 1, str(tp._selected_ids()))

    # ── 5. 完成信号 ────────────────────────────────────
    head("5. 队列结束：进度满、按钮复位")
    for it in q.items():
        it.state = "done"
    q.finished.emit("完成 3/3")
    app.processEvents()
    ck("进度条到 100", tp.queue_bar.value() == 100, str(tp.queue_bar.value()))
    ck("状态文字有总结", "完成 3/3" in tp.queue_status.text(),
       tp.queue_status.text())
    ck("停止按钮复位为不可用", not tp.queue_stop_btn.isEnabled())

    # ── 6. 前置检查：没有选中 ──────────────────────────
    head("6. 加入队列的前置检查")
    tp.table.clearSelection()
    app.processEvents()
    POPUPS.clear()
    tp._add_to_queue()
    app.processEvents()
    ck("没选中时给提示，不开对话框",
       any("选中" in str(p[1]) for p in POPUPS) or "选中" in tp.tip.text(),
       tp.tip.text()[:60] or str(POPUPS))

    # 都已在队列
    sm.select(tp.table.model().index(0, 0),
              sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
    app.processEvents()
    t0 = tp._selected_ids()[0]
    ck("这一条确实已在队列里", q.has_task(t0), str(t0))
    POPUPS.clear()
    tp._add_to_queue()
    app.processEvents()
    ck("重复加入被拦下并说明",
       "都已经在队列里" in tp.tip.text() or any(
           "队列" in str(p[1]) for p in POPUPS),
       tp.tip.text()[:60] or str(POPUPS))

    # ── 7. 源文件缺失的剔除 ────────────────────────────
    head("7. 源文件不在的任务在排队前就被剔除")
    q.reset()
    app.processEvents()
    t_missing = make_task("已删除的素材", exists=False)
    created.append(t_missing)
    tp.refresh()
    app.processEvents()
    for r in range(tp.table.rowCount()):
        if tp.table.item(r, 0).data(Qt.UserRole) == t_missing:
            sm.clear()
            sm.select(tp.table.model().index(r, 0),
                      sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
    app.processEvents()
    q.reset()
    POPUPS.clear()
    before = q.count()
    # 只有这一条被选中且文件不在 → 应该"全部剔除"，不进对话框
    tp._add_to_queue()
    app.processEvents()
    ck("★ 缺文件的没被排进队列", q.count() == before, f"{before} → {q.count()}")
    ck("并且给了可读的说明",
       "源文件" in tp.tip.text() or "源文件" in str(POPUPS),
       (tp.tip.text() or str(POPUPS))[:80])

    # ── 8. 移除 / 清空 ─────────────────────────────────
    head("8. 移除与清空")
    q.reset()
    tb = make_task("待清空")
    created.append(tb)
    q.add(tb, pipeline.Options())
    app.processEvents()
    ck("加入后列表可见", tp.queue_list.count() == 1)
    ck("清空返回移除了 1 项", q.clear() == 1)
    app.processEvents()
    ck("清空后面板隐藏", not tp.queue_box.isVisible())

    # ── 9. 关程序时的保护 ──────────────────────────────
    head("9. 队列运行时关闭窗口要先问过用户")
    tc = make_task("跑着呢")
    created.append(tc)
    q.add(tc, pipeline.Options())
    q._running = True                    # 假装在跑（不真启动）
    POPUPS.clear()

    class _Ev:
        def __init__(self):
            self.ignored = False

        def ignore(self):
            self.ignored = True

        def accept(self):
            pass
    ev = _Ev()
    QMessageBox.question = staticmethod(
        lambda *a, **k: (POPUPS.append(("ask", "关闭确认")),
                         QMessageBox.No)[1])
    w.closeEvent(ev)
    ck("★ 队列在跑时关窗口会先弹确认", bool(POPUPS), str(POPUPS))
    ck("★ 用户选「否」时窗口不关（事件被 ignore）", ev.ignored, str(ev.ignored))
    q._running = False
    q.reset()

    # ── 清理 ──────────────────────────────────────────
    try:
        w.close()
    except BaseException:                # noqa: BLE001
        pass
    for i in created:
        try:
            store.delete_task(i)
        except BaseException:            # noqa: BLE001
            pass

    print(f"\n{'=' * 74}")
    print(f"通过 {PASS} / {PASS + FAIL}"
          + (f"　失败 {FAIL}" if FAIL else "　全部通过"))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
