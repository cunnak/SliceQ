# -*- coding: utf-8 -*-
"""异常注入 · 第二轮：**看结果，不只看崩没崩**

第一轮只判"有没有抛异常"，那是不够的 —— 一个函数完全可以
"不抛异常地返回一堆错结果"。这一轮逐个检查实际产物。

要回答的问题：
  A6  缺字段的条目，有没有被记成"失败"并带原因？（而不是静默跳过）
  A7  空列表导出，"成功 0/失败 0"这种返回是否明确？
  A5  越界时间，报的错是否指向真实原因？
  B3  分段倒挂，生成的字幕长什么样？
  B4  200 字长文本，换行对不对？有没有丢字？
  B5  `{` `}` 有没有被转义？（不转的话会吞掉后面整段文字）
  D1/D2 取消后有没有留下半截文件？
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

from sliceq import asr, editor, store, subtitle                         # noqa: E402
from sliceq import subtitle_style                                       # noqa: E402

PROJ = ROOT
MEDIA = PROJ / "stage0" / "test_media" / "live_clip90.mp4"
SEG_CACHE = PROJ / "stage0" / "test_media" / "editor_selftest" / "segments.json"
WORK = PROJ / "stage0" / "test_media" / "fault_quality"

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
    print("=" * 74)
    print(t)
    print("=" * 74)


def out(tag: str) -> Path:
    p = WORK / tag
    p.mkdir(parents=True, exist_ok=True)
    return p


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    store.init_db()
    segs = ([asr.Segment(**d) for d in
             json.loads(SEG_CACHE.read_text(encoding="utf-8"))]
            if SEG_CACHE.exists() else [])

    # ══════════════════════════════════════════════════
    head("[A] 缺字段的条目 —— 必须被记成失败并带原因")

    res = editor.export_clips(
        MEDIA,
        [{"id": 1, "title_hint": "没有时间字段"},
         {"id": 2, "start": 2.0, "end": 10.0, "title_hint": "正常的一条"}],
        out_base=out("q_a6"))

    print(f"  {res.summary()}")
    for it in res.items:
        mark = "成功" if it.ok else "失败"
        print(f"    [{mark}] {it.title}｜{it.error.splitlines()[0][:60] if it.error else ''}")

    bad = [i for i in res.items if not i.ok]
    check("缺字段条目被记为失败（不是静默跳过）", len(bad) == 1, f"{len(bad)} 条失败")
    if bad:
        check("失败原因可读且指向正确原因",
          "缺少起止时间" in bad[0].error,
          bad[0].error.splitlines()[0][:60])
    check("同批的正常条目仍然成功",
          any(i.ok for i in res.items), f"{res.ok_count} 条成功")

    # ══════════════════════════════════════════════════
    head("[B] 空候选列表")

    res2 = editor.export_clips(MEDIA, [], out_base=out("q_a7"))
    check("空列表返回明确的零结果",
          res2.ok_count == 0 and res2.fail_count == 0,
          res2.summary())
    check("空列表不是异常路径", True, "返回正常结果对象，UI 可据此提示")

    # ══════════════════════════════════════════════════
    head("[C] 越界时间 —— 错误信息要指向真实原因")

    res3 = editor.export_clips(
        MEDIA, [{"id": 1, "start": 999_000.0, "end": 999_060.0,
                 "title_hint": "越界"}], out_base=out("q_a5"))
    fail = [i for i in res3.items if not i.ok]
    msg = fail[0].error if fail else ""
    print(f"    错误信息：{msg.splitlines()[0][:100]}")
    check("越界被记为失败", len(fail) == 1)
    check("错误信息提到文件/时间问题",
          any(k in msg for k in ("产出", "切点", "时间", "文件")),
          msg.splitlines()[0][:70])

    # ══════════════════════════════════════════════════
    head("[D] 分段倒挂（end < start）—— 看字幕结果")

    res4 = editor.export_clips(
        MEDIA, [{"id": 1, "start": 2.0, "end": 12.0, "title_hint": "倒挂"}],
        out_base=out("q_b3"), burn_subtitles=True,
        segments=[asr.Segment(start_ms=9000, end_ms=4000, text="倒挂的一句"),
                  asr.Segment(start_ms=3000, end_ms=6000, text="正常的一句")],
        width=406, height=720)

    item = res4.items[0] if res4.items else None
    if item and item.srt and Path(item.srt).exists():
        srt = Path(item.srt).read_text(encoding="utf-8")
        print("    SRT 内容：")
        for ln in srt.strip().splitlines()[:12]:
            print(f"      {ln}")
        check("倒挂分段被丢弃、正常分段保留",
              "倒挂" not in srt and "正常的一句" in srt)
    else:
        check("产出了 SRT", False, "没有 SRT 可检查")

    # ══════════════════════════════════════════════════
    head("[E] 200 字长文本 —— 换行 + 不丢字")

    long_text = "很长的一句话" * 25
    res5 = editor.export_clips(
        MEDIA, [{"id": 1, "start": 2.0, "end": 12.0, "title_hint": "长文本"}],
        out_base=out("q_b4"), burn_subtitles=True,
        segments=[asr.Segment(start_ms=2000, end_ms=11000, text=long_text)],
        width=406, height=720)

    item5 = res5.items[0] if res5.items else None
    if item5 and item5.srt and Path(item5.srt).exists():
        srt5 = Path(item5.srt).read_text(encoding="utf-8")
        body = "".join(ln for ln in srt5.splitlines()
                       if ln and not ln.isdigit() and "-->" not in ln)
        print(f"    原文 {len(long_text)} 字 → 字幕正文 {len(body)} 字")
        check("长文本没被丢字", body == long_text,
              f"{len(body)} vs {len(long_text)}")

    # 直接检查换行结果（不依赖 ffmpeg）
    wrapped = subtitle_style.wrap_for_canvas(long_text, width=406, size=42)
    lines = wrapped.split(r"\N")
    widths = [sum(subtitle_style._char_units(c) for c in ln) for ln in lines]
    avail = (406 - 40) / 42
    check("换行后拼接 == 原文", "".join(lines) == long_text)
    check("每一行都不超宽", all(w <= avail + 0.02 for w in widths),
          f"最宽 {max(widths):.2f} / 可用 {avail:.2f}，共 {len(lines)} 行")

    # ══════════════════════════════════════════════════
    head("[F] ASS 特殊字符 { } —— 必须转义，否则吞掉后面的字")

    danger = "前面{这是危险内容}后面还有字"
    ass = subtitle_style.builtin_default().to_ass(
        [(0.0, 3.0, danger, "")], width=406, height=720)
    dialog = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    body = dialog[0].split(",,", 1)[-1] if dialog else ""
    print(f"    ASS 正文：{body}")
    check("裸 { 已转义", "{" not in body and "｛" in body,
          "用全角替代，避免被当成样式块")
    check("转义后文字没丢", "前面" in body and "后面还有字" in body)

    # ══════════════════════════════════════════════════
    head("[G] 取消 —— 不留半截文件")

    res6 = editor.export_clips(
        MEDIA, [{"id": i, "start": 2.0 + i, "end": 12.0 + i,
                 "title_hint": f"第{i}条"} for i in range(1, 5)],
        out_base=out("q_d2"), should_cancel=lambda: True)

    print(f"    {res6.summary()}　cancelled={res6.cancelled}")
    check("立即取消时没有产出", res6.ok_count == 0)
    check("取消状态被标记", res6.cancelled)

    d2 = out("q_d2")
    mp4s = list(d2.rglob("*.mp4"))
    print(f"    目录里的 mp4：{[p.name for p in mp4s]}")
    check("没有残留半截 mp4", len(mp4s) == 0, f"{len(mp4s)} 个")

    # ══════════════════════════════════════════════════
    head("[H] 重映射 —— 阈值两侧（期望值由程序算出，不手写）")

    # ⚠️ 上一版这里的手写标签是错的（"跨起点80%"实际相交比只有 37.5%）。
    #    判据必须**算出来**，否则测的是我的算术，不是代码。
    #    这与"肉眼估幻觉率"是同一个坑。
    clip = (600.0, 620.0)
    cases = [
        (600.0, 610.0),     # 完全在内
        (598.0, 610.0),     # 跨起点
        (596.0, 606.0),     # 相交 60% —— 阈值上侧的样本
        (595.5, 605.5),     # 相交**正好 55%** —— 卡在边界上
        (595.0, 605.0),     # 相交 50% —— 阈值下侧
        (594.0, 604.0),     # 跨起点，低于阈值
        (604.0, 616.0),     # 完全在内（后段）
        (616.0, 626.0),     # 跨终点
        (700.0, 710.0),     # 完全在外
    ]
    boundary_hit = {"above": False, "below": False}
    for a, b in cases:
        inter = max(0.0, min(b, clip[1]) - max(a, clip[0]))
        ratio = inter / (b - a)
        expect_keep = ratio >= 0.55 and inter >= 0.25
        cues = [subtitle.Cue(src_start=a, src_end=b, start=a, end=b,
                             main=f"{a}~{b}")]
        kept, dropped = subtitle.remap_to_clip(cues, *clip)
        got_keep = bool(kept)
        tag = "保留" if expect_keep else "丢弃"
        check(f"{a:.1f}~{b:.1f} 相交 {ratio * 100:5.1f}% → 应{tag}",
              got_keep == expect_keep,
              f"实得 {'保留' if got_keep else '丢弃'}")
        check("  计数守恒", len(kept) + dropped == 1)
        if 0.55 <= ratio < 0.65:
            boundary_hit["above"] = got_keep
        if 0.45 < ratio < 0.55:
            boundary_hit["below"] = not got_keep

    check("★ 阈值两侧都测到了", boundary_hit["above"] and boundary_hit["below"],
          f"阈值上侧保留={boundary_hit['above']}，下侧丢弃={boundary_hit['below']}")

    # ══════════════════════════════════════════════════
    head("结果")
    print(f"  通过 {PASS} 项，失败 {FAIL} 项")
    sys.stdout.flush()
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
