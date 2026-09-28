# -*- coding: utf-8 -*-
"""数据完整性自测：删任务必须清干净（阶段 4-D）。

## 为什么会有这个测试

清理测试残留任务时发现：删掉 14 个任务后，`transcript_seg` 表里
**还剩 659 行孤儿数据**。

根因：这张表建的时候漏了 `REFERENCES task(id) ON DELETE CASCADE`
（其它 4 张子表都有）。`delete_task()` 只有一句 `DELETE FROM task`，
级联全靠外键 —— 漏一张表就漏一份数据。

这不只是"数据多一点"：

  · **隐私**：用户删任务的意思是"这些素材我不要了"，
    但素材里说过的话**还留在库里**
  · 数据无上限膨胀：转录是几 MB 文本，每次分析都写

已用 schema v5 迁移修掉（重建表 + 顺带清孤儿）。这个测试守两件事：

    ① **同类问题不再犯** —— 任何引用 `task(id)` 的表都必须带 CASCADE
       （这条断言在加新表时会自动覆盖到）
    ② 级联删除**真的**对每张子表生效（不是只检查 DDL 文本）
"""
from __future__ import annotations

import sqlite3
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

from sliceq import config, store                            # noqa: E402

PASS = 0
FAIL = 0


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


def raw() -> sqlite3.Connection:
    return sqlite3.connect(str(config.DB_PATH))


def child_tables() -> list[str]:
    """所有引用 task(id) 的表。**从 schema 里查**，不写死列表 ——
    写死的话，将来加一张新的子表，这个测试就漏掉它了。

    ⚠️ 比对前先把空白全部去掉（DDL 里换行、缩进都不固定）。
       踩过：这里去了空格却拿带空格的模式去匹配 ⇒ 返回空列表，
       于是下游"所有表都没有孤儿""所有表都被清空"**全部假通过**
       （`all([])` 恒为 True —— 空集合上的全称断言永远成立）。
    """
    conn = raw()
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    out = []
    for name, ddl in rows:
        flat = "".join((ddl or "").split())
        if "REFERENCEStask(id)" in flat:
            out.append(name)
    return out


def main() -> int:
    store.init_db()

    # ── 1. schema 版本 ─────────────────────────────────
    head("1. schema 版本")
    conn = raw()
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    ck(f"user_version == {store.SCHEMA_VERSION}",
       ver == store.SCHEMA_VERSION, f"实际 {ver}")

    # ── 2. ★ 每张子表都必须带 CASCADE（防再犯）────────
    head("2. ★ 引用 task 的表都要有 ON DELETE CASCADE")
    tables = child_tables()
    ck("找到了若干张子表（不是空列表）", len(tables) >= 4, str(tables))
    missing = []
    for t in tables:
        conn = raw()
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0]
        conn.close()
        flat = (ddl or "").replace(" ", "").replace("\n", "")
        if "REFERENCEStask(id)ONDELETECASCADE" not in flat:
            missing.append(t)
    ck("★ 所有子表都带 CASCADE（漏一张就漏一份数据）",
       not missing, f"缺 CASCADE 的：{missing}" if missing else f"{len(tables)} 张都合规")
    ck("transcript_seg 在子表清单里（就是这次修的那张）",
       "transcript_seg" in tables, str(tables))

    # ── 3. 没有孤儿数据 ────────────────────────────────
    head("3. 库里的数据要自洽（没有指向已删任务的孤儿）")
    conn = raw()
    orphans: dict[str, int] = {}
    for t in tables:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE task_id NOT IN (SELECT id FROM task)"
        ).fetchone()[0]
        if n:
            orphans[t] = n
    conn.close()
    ck("★ 任何子表都没有孤儿行", not orphans,
       str(orphans) if orphans else "干净")

    # ── 4. 级联删除真的生效（逐表实测）────────────────
    head("4. ★ 逐表实测：删任务后每条子表都要清空")
    tid = store.create_task("级联实测", source_path="x", duration_sec=10)

    class S:
        def __init__(self, a, b, txt):
            self.start_ms, self.end_ms, self.text = a, b, txt

    store.save_transcript_segs(tid, [S(0, 1000, "甲"), S(1000, 2000, "乙")])
    store.add_clip(tid, 0.0, 1.0, type="demo", score=80, summary="x",
                   title_hint="t")
    store.add_export(tid, "clip_mp4", "a.mp4")
    store.add_subtitle(tid, 0, 1000, "字幕")
    store.add_marker(tid, 500, "标记")
    # run_hint（schema v6 新增）—— 这张表是**动态查出来的**，
    # 所以加表时忘了在这里造数据，本测试会立刻报"某张子表没有数据"。
    # 这正是它该有的行为：漏一张表就漏一份数据（见文件头注释）。
    store.replace_run_hints(tid, ["提示甲", "提示乙"])

    counts_before: dict[str, int] = {}
    conn = raw()
    for t in tables:
        counts_before[t] = conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE task_id=?", (tid,)).fetchone()[0]
    conn.close()
    # ⚠️ 先断言"有东西可测"。否则 tables 为空时下面两条
    #    `all(...)` / `not {...}` 都会**假通过** —— 空集合上的全称断言恒真。
    ck("待测子表非空（否则本节全部假通过）", bool(counts_before),
       f"待测 {len(counts_before)} 张")
    ck("删除前每条子表都有数据（否则测不出级联）",
       bool(counts_before) and all(n > 0 for n in counts_before.values()),
       str(counts_before))

    store.delete_task(tid)

    counts_after: dict[str, int] = {}
    conn = raw()
    for t in tables:
        counts_after[t] = conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE task_id=?", (tid,)).fetchone()[0]
    conn.close()
    left = {t: n for t, n in counts_after.items() if n}
    ck("★ 删任务后所有子表都清空了", not left,
       f"残留：{left}" if left else f"{len(tables)} 张表全清")

    # ── 5. 插入顺序不该影响（先写转录再写任务？不行）────
    head("5. 边界：写入的转录能被查回来")
    tid2 = store.create_task("读回实测", source_path="y", duration_sec=5)
    store.save_transcript_segs(tid2, [S(0, 500, "一"), S(500, 1000, "二")])
    segs = store.list_transcript_segs(tid2)
    ck("写两段能读回两段", len(segs) == 2, str(len(segs)))
    ck("内容与顺序正确",
       [s["text"] for s in segs] == ["一", "二"],
       str([s["text"] for s in segs]))
    store.delete_task(tid2)

    print(f"\n{'=' * 72}")
    print(f"通过 {PASS} / {PASS + FAIL}"
          + (f"　失败 {FAIL}" if FAIL else "　全部通过"))
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
