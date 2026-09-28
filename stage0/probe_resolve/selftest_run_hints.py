# -*- coding: utf-8 -*-
"""降级提示的落库与可见性（v0.1.2）。

背景（2026-09-28 实测事故）：
  用户跑完分析看到「完成：0 个切片段　⚠️ 2 条提示」——
  但**提示点不开**，他不知道为什么，我们也拿不到证据。
  失败原因（"分析副本过大：base64 约 120.8M 字符，超过安全上限 20M"）
  只活在内存里的 `RunResult.degraded`，进程一退就没了。

本测试守四件事：
  ① 提示写库/读回/替换语义正确
  ② 删任务时提示被级联清掉（隐私：失败原因里可能带素材路径）
  ③ **分析异常中断时也落库**（那正是最需要提示的时刻）
  ④ 界面上有提示时按钮可见、无提示时隐藏，且内容与库一致
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq import _testing                                   # noqa: E402

_testing.isolate_data_root("runhints")
_testing.assert_isolated()

from sliceq import analyzer, config, pipeline, store          # noqa: E402

PASS = FAIL = 0


def ck(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK]   {name}" + (f"　—　{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"　—　{detail}" if detail else ""))


def head(t: str) -> None:
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


store.init_db()

# ─────────────────────────────────────────────────────────
head("1. schema 与基本读写")
# ─────────────────────────────────────────────────────────
ck("SCHEMA_VERSION >= 6（run_hint 表是 v6 加的）", store.SCHEMA_VERSION >= 6,
   str(store.SCHEMA_VERSION))
ver = store._connect().execute("PRAGMA user_version").fetchone()[0]
ck("隔离库已迁移到该版本", int(ver) == store.SCHEMA_VERSION,
   f"{ver} vs {store.SCHEMA_VERSION}")
tables = [r[0] for r in store._connect().execute(
    "SELECT name FROM sqlite_master WHERE type='table'")]
ck("run_hint 表存在", "run_hint" in tables, str(sorted(tables)))

tid = store.create_task("提示测试", source_path="x.mp4", duration_sec=10.0)
ck("新任务读回为空列表", store.get_run_hints(tid) == [], "")

store.replace_run_hints(tid, ["第一条", "第二条", "第三条"])
got = store.get_run_hints(tid)
ck("写入三条能按顺序读回", got == ["第一条", "第二条", "第三条"], str(got))

store.replace_run_hints(tid, ["第四条", "第五条"])
got = store.get_run_hints(tid)
ck("★ 是替换而不是追加（重跑不该留着上一轮的原因）",
   got == ["第四条", "第五条"], str(got))

store.replace_run_hints(tid, [])
ck("★ 传空列表 = 清空（成功的一轮要抹掉上次失败的原因）",
   store.get_run_hints(tid) == [], str(store.get_run_hints(tid)))

store.replace_run_hints(tid, ["甲", "   ", "", "乙"])
got = store.get_run_hints(tid)
ck("空串/纯空白被过滤", got == ["甲", "乙"], str(got))

long_text = "超长" * 500
store.replace_run_hints(tid, [long_text])
ck("长文本可完整存回（截断会丢掉真正的报错）",
   store.get_run_hints(tid) == [long_text], f"{len(store.get_run_hints(tid)[0])} 字")

# ─────────────────────────────────────────────────────────
head("2. 级联删除（隐私）")
# ─────────────────────────────────────────────────────────
# 失败原因里会带素材路径、模型原始报错 —— 用户删任务就是"这些我不要了"，
# 提示也必须一起走。
store.replace_run_hints(tid, ["含路径的报错：C:\\某素材\\a.mp4 副本过大"])
prev = store.get_run_hints(tid)
ck("删除前确实有数据（否则下一条会假通过）", len(prev) > 0, str(len(prev)))
store.delete_task(tid)
left = store._connect().execute(
    "SELECT COUNT(*) FROM run_hint WHERE task_id=?", (tid,)).fetchone()[0]
ck("★ 删任务后提示被级联清空", left == 0, f"残留 {left} 行")

# ─────────────────────────────────────────────────────────
head("3. 异常中断也必须落库")
# ─────────────────────────────────────────────────────────
# 这是本功能的核心价值：分析**失败**的时候，提示才是最需要的。
# 原来 `run()` 的 except 分支只标状态、不落提示 —— 用户看到的只有"出错"。
tid2 = store.create_task("中断测试", source_path="不存在的文件.mp4",
                         duration_sec=10.0)
opts = pipeline.Options()
raised = False
try:
    pipeline.run(tid2, "绝对不存在的文件.mp4", opts,
                 analyzer.HTTPTransport("sk-dry-run"))
except BaseException:                                          # noqa: BLE001
    raised = True
ck("无效输入确实抛了异常（前提成立）", raised)
hints = store.get_run_hints(tid2)
print(f"  落库的提示：{hints}")
ck("★ 异常路径也落了提示", len(hints) > 0, str(hints))
ck("★ 提示里含'分析中断'与异常类型",
   any("分析中断" in h for h in hints), str(hints))
ck("★ 提示里含原始原因（不只是'出错了'）",
   any("不存在" in h or "PipelineError" in h for h in hints), str(hints))
st = store.get_task(tid2)
ck("状态被标成 error", st and st.get("status") == config.STATUS_ERROR,
   str(st.get("status") if st else None))

# ─────────────────────────────────────────────────────────
head("4. _save_hints 自身出错不得掩盖真正的错误")
# ─────────────────────────────────────────────────────────
_orig = store.replace_run_hints


def _boom(*a, **k):
    raise RuntimeError("模拟数据库故障")


store.replace_run_hints = _boom                          # type: ignore[assignment]
try:
    pipeline._save_hints(12345, ["x"])                   # 不应抛
    ok = True
except BaseException:                                     # noqa: BLE001
    ok = False
finally:
    store.replace_run_hints = _orig                      # type: ignore[assignment]
ck("★ 落库失败被吞掉（否则会掩盖分析本身的错误）", ok)

# ─────────────────────────────────────────────────────────
head("5. 界面：提示按钮的可见性与内容")
# ─────────────────────────────────────────────────────────
import os                                                    # noqa: E402
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication                   # noqa: E402
from sliceq.ui.pages.clips_page import ClipsPage             # noqa: E402

app = QApplication.instance() or QApplication([])
page = ClipsPage(None)

tid3 = store.create_task("界面测试", source_path="x.mp4", duration_sec=10.0)
page.show_task(tid3)
ck("无提示时按钮隐藏", page.hint_btn.isHidden(), "")

store.replace_run_hints(tid3, ["候选 1 精析失败：分析副本过大", "候选 2 精析失败：同上"])
page.show_task(tid3)
ck("★ 有提示时按钮可见（否则用户永远看不到原因）",
   not page.hint_btn.isHidden())
print(f"  按钮文字：{page.hint_btn.text()!r}")
ck("★ 按钮文字含条数", "2" in page.hint_btn.text(), page.hint_btn.text())
ck("★ 按钮文字给出可点击的暗示",
   "点" in page.hint_btn.text() or "查看" in page.hint_btn.text(),
   page.hint_btn.text())
ck("★ 提示内容与库一致", page._hints == store.get_run_hints(tid3),
   str(page._hints))

store.replace_run_hints(tid3, [])
page.show_task(tid3)
ck("提示被清空后按钮自动隐藏", page.hint_btn.isHidden(), "")

# 切到另一个任务，提示应跟着换
tid4 = store.create_task("另一个任务", source_path="y.mp4", duration_sec=10.0)
store.replace_run_hints(tid4, ["只有一个提示"])
page.show_task(tid4)
ck("切换任务后提示随之更新",
   page._hints == ["只有一个提示"] and "1" in page.hint_btn.text(),
   f"{page._hints} / {page.hint_btn.text()!r}")

# 弹窗内容能拼出来（不 exec，避免阻塞；只验拼装逻辑）
hints_txt = "\n\n".join(f"[{i}] {t}"
                       for i, t in enumerate(page._hints, 1))
ck("弹窗正文拼装含编号与全文",
   hints_txt.startswith("[1] 只有一个提示"), repr(hints_txt[:40]))

page._hints.clear()                      # 人为清空，模拟"没有提示"
_r = page._show_hints()                  # 不应弹窗、不应抛
ck("★ 无提示时 _show_hints 直接返回（不弹空窗）", _r is None, repr(_r))

store.delete_task(tid2)      # 第 3 节那个中断任务（它的提示也要一起走）
store.delete_task(tid3)
store.delete_task(tid4)
left = store._connect().execute("SELECT COUNT(*) FROM run_hint").fetchone()[0]
ck("清理后无残留提示", left == 0, str(left))

print("\n" + "=" * 72)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 72)
raise SystemExit(1 if FAIL else 0)
