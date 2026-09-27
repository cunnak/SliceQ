# -*- coding: utf-8 -*-
"""阶段 3 自测 —— 导出链路（v4 转录分段 → 导出 → 字幕）

验证「分析阶段落库的转录分段」能被导出阶段正确取用并生成字幕。
这条链路断在哪一环，导出都会变成"没有字幕的视频"或"根本没有字幕文件"，
而且**不会报错**。

用法：
    QT_QPA_PLATFORM=offscreen python selftest_export_chain.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from sliceq import asr, config, editor, store, subtitle_style  # noqa: E402

PROJ = ROOT
MEDIA = PROJ / "stage0" / "test_media" / "live_clip90.mp4"
SEG_CACHE = PROJ / "stage0" / "test_media" / "editor_selftest" / "segments.json"
WORK = PROJ / "stage0" / "test_media" / "export_chain"

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK  ] {name}" + (f"  — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  — {detail}" if detail else ""))
    return ok


def head(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    store.init_db()

    head("[1] schema 迁移")
    import sqlite3
    con = sqlite3.connect(str(config.DB_PATH))
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    # ⚠️ 用 `store.SCHEMA_VERSION`，**不要硬编码数字**。
    #    踩过：schema 从 v4 升到 v5 后，这里的 `ver == 4` 直接变成失败 ——
    #    而它测的是"迁移有没有跑"，不是"版本号恰好是几"。
    #    硬编码常量的测试会随实现演进而腐烂，而且看起来像真缺陷。
    check(f"schema 版本为 {store.SCHEMA_VERSION}",
          ver == store.SCHEMA_VERSION, f"实际 {ver}")
    check("transcript_seg 表已建", "transcript_seg" in tables)

    head("[2] 转录分段落库 / 读回")
    if not SEG_CACHE.exists():
        print(f"  缺少分段缓存 {SEG_CACHE}，先跑 selftest_editor.py 生成")
        return 2
    segs = [asr.Segment(**d) for d in
            json.loads(SEG_CACHE.read_text(encoding="utf-8"))]
    print(f"  分段数：{len(segs)}")

    task = store.create_task(
        name="导出链路自测", source_path=str(MEDIA), duration_sec=90.0,
        width=406, height=720)
    tid = int(task) if isinstance(task, int) else int(task.get("id"))
    n = store.save_transcript_segs(tid, segs)
    check("落库条数一致", n == len(segs), f"{n} vs {len(segs)}")

    back = store.list_transcript_segs(tid)
    check("读回条数一致", len(back) == len(segs))
    check("时间戳保真",
          back[0]["start_ms"] == segs[0].start_ms
          and back[-1]["end_ms"] == segs[-1].end_ms,
          f"{back[0]['start_ms']} / {back[-1]['end_ms']}")
    check("文本保真", back[0]["text"] == segs[0].text,
          back[0]["text"][:30])

    # 重存应覆盖而非追加
    store.save_transcript_segs(tid, segs[:3])
    check("重存为覆盖语义", len(store.list_transcript_segs(tid)) == 3,
          f"{len(store.list_transcript_segs(tid))} 条")
    store.save_transcript_segs(tid, segs)      # 还原

    head("[3] 从库读分段 → 导出（含字幕）")
    clips = [
        {"id": 1, "start": 2.0, "end": 20.0, "title_hint": "第一段"},
        {"id": 2, "start": 25.0, "end": 42.0, "title_hint": "第二段/带斜杠"},
    ]
    for c in clips:
        store.add_clip(tid, c["start"], c["end"],
                       title_hint=c["title_hint"], selected=True)

    # 模拟导出对话框的取数路径
    lib_segs = [asr.Segment(start_ms=r["start_ms"], end_ms=r["end_ms"],
                            text=r["text"])
                for r in store.list_transcript_segs(tid)]
    sel = [c for c in store.list_clips(tid) if c.get("selected")]
    check("勾选片段可取到", len(sel) == 2, f"{len(sel)} 条")
    check("分段可转回 Segment", len(lib_segs) == len(segs))

    out_base = WORK / "out"
    res = editor.export_clips(
        MEDIA, sel, out_base=out_base, task_id=tid, task_name="导出链路自测",
        mode="exact", burn_subtitles=True, segments=lib_segs,
        preset=subtitle_style.builtin_default())

    print(f"  {res.summary()}")
    print(f"  输出：{res.out_dir}")
    check("两条都导出成功", res.ok_count == 2, f"{res.ok_count}/2")
    check("字幕已烧录", res.subtitles_burned)
    check("清单文件存在", (res.out_dir / "导出清单.txt").exists())

    for it in res.items:
        if it.ok:
            v = Path(it.video)
            print(f"    {v.name}  {v.stat().st_size / 1024:.0f} KB  "
                  f"字幕 {it.cues} 条（边界丢弃 {it.dropped_cues}）")
            check(f"{v.name} 有字幕条目", it.cues > 0)
            check(f"{v.name} 有 SRT", bool(it.srt) and Path(it.srt).exists())
        else:
            print(f"    失败：{it.title} — {it.error[:100]}")

    head("[4] 落库与产物一致性")
    exps = store.list_exports(tid)
    print(f"  export 表记录 {len(exps)} 条")
    check("导出记录已落库", len(exps) >= 2, f"{len(exps)} 条")

    head("[5] 字幕时间轴落在片段范围内")
    if res.ok_count:
        srt = Path(res.items[0].srt)
        text = srt.read_text(encoding="utf-8")
        last = [b for b in text.strip().split("\n\n") if b][-1]
        last_tc = last.splitlines()[1]
        end_str = last_tc.split(" --> ")[1]
        hh, mm, rest = end_str.split(":")
        ss, ms = rest.split(",")
        last_s = int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000
        dur = 20.0 - 2.0
        print(f"  最后一条字幕结束于 {last_s:.3f}s（片段时长 {dur:.1f}s）")
        check("无字幕超出片段时长", last_s <= dur + 0.05,
              f"{last_s:.3f}s vs {dur:.1f}s")
        check("有字幕落在后段（非全部挤在开头）", last_s > dur * 0.5,
              f"{last_s:.2f}s")

    store.delete_task(tid)
    print()
    print(f"  通过 {PASS} 项，失败 {FAIL} 项")
    sys.stdout.flush()
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
