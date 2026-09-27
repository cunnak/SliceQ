# -*- coding: utf-8 -*-
"""阶段 3 自测 —— 剪辑执行与字幕烧录

验证链：转录 → 断句 → 重映射 → 切片 → 烧字幕 → 产物检查

★ 关键验收：**抽 3 条字幕核对「字幕文本 ↔ 画面」的对应关系**。
   只看时间戳数字不够 —— 时间戳全对、画面全错的组合是可能的
   （这正是时间轴重映射漏做时的表现）。

用法：
    python selftest_editor.py [源视频] [--llm]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent.parent
sys.path.insert(0, str(PROJ))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
#    注意：**间接写入也算**（例如调 bridge.set_manual_path() 会写 settings.json）
#    —— 所以默认给所有自测都隔离，不做"看起来不碰数据"的判断。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from sliceq import asr, editor, subtitle, subtitle_style, ffmpeg_tools  # noqa: E402

MEDIA = PROJ / "stage0" / "test_media" / "live_clip90.mp4"
WORK = PROJ / "stage0" / "test_media" / "editor_selftest"
OUT_BASE = WORK / "out"

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


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_llm = "--llm" in sys.argv
    media = Path(args[0]) if args else MEDIA

    WORK.mkdir(parents=True, exist_ok=True)
    OUT_BASE.mkdir(parents=True, exist_ok=True)

    if not media.exists():
        print(f"素材不存在：{media}")
        return 2

    ff = ffmpeg_tools.find_ffmpeg()
    print(f"素材     : {media.name}")
    print(f"FFmpeg   : {ff}")
    print(f"LLM 断句 : {'开' if use_llm else '关（规则断句）'}")

    # ══════════════════════════════════════════════════════
    head("[1] 转录（若已有缓存则复用）")

    cache = WORK / "segments.json"
    if cache.exists():
        segs = [asr.Segment(**d) for d in
                json.loads(cache.read_text(encoding="utf-8"))]
        print(f"  复用缓存：{len(segs)} 段")
    else:
        t0 = time.time()
        segs = asr.transcribe(media, chunk_seconds=900,
                              work_dir=WORK / "asr")
        print(f"  转录完成：{len(segs)} 段，耗时 {time.time() - t0:.0f}s")
        cache.write_text(json.dumps([s.to_dict() for s in segs],
                                    ensure_ascii=False), encoding="utf-8")

    for s in segs[:6]:
        print(f"    {s.start_s:6.1f}→{s.end_s:6.1f}s  {s.text[:44]}")
    check("转录有结果", len(segs) > 0, f"{len(segs)} 段")

    # ══════════════════════════════════════════════════════
    head("[2] 断句 + 重映射（不依赖 ffmpeg 的部分）")

    transport = None
    if use_llm:
        from sliceq import analyzer, secrets
        transport = analyzer.make_transport()
        print(f"  transport: {type(transport).__name__}")

    t0 = time.time()
    all_cues, stats = subtitle.split_segments(
        segs, transport=transport, use_llm=use_llm)
    print(f"  断句：{len(all_cues)} 条，耗时 {time.time() - t0:.1f}s")
    print(f"  统计：{stats}")
    check("断句有产出", len(all_cues) > 0, f"{len(all_cues)} 条")

    lens = [len(c.main) for c in all_cues]
    if lens:
        over = sum(1 for l in lens if l > 26)
        shortest = min(all_cues, key=lambda c: len(c.main))
        longest = max(all_cues, key=lambda c: len(c.main))
        print(f"  字数：最短 {min(lens)} / 最长 {max(lens)} / 平均 "
              f"{sum(lens) / len(lens):.1f}  超 26 字 {over} 条")
        print(f"  最短一条：「{shortest.main}」")
        print(f"  最长一条：「{longest.main}」")
        # 1 字碎片是断句失败；2 字则可能是转录本身就是短句（"对吧"）。
        check("没有 1 字碎片", min(lens) >= 2, f"最短 {min(lens)}")

    # 候选段：取两段（第二段刻意跨越字幕边界）
    total_s = max(s.end_s for s in segs) if segs else 0.0
    cands = [
        {"id": 1, "start": 2.0, "end": 20.0,
         "title_hint": "第一段_扫码支付"},
        {"id": 2, "start": 25.0, "end": 42.0,
         "title_hint": "第二段|含非法字符"},
    ]
    print(f"\n  候选 {len(cands)} 条（素材有效范围 0~{total_s:.1f}s）")

    # 验证重映射：落到段内的条数应大于 0
    for c in cands:
        mapped, dropped = subtitle.remap_to_clip(all_cues, c["start"], c["end"])
        print(f"    候选 {c['start']:.0f}~{c['end']:.0f}s → 字幕 {len(mapped)} 条"
              f"，边界丢弃 {dropped} 条")
        check(f"候选 {c['id']} 有字幕", len(mapped) > 0)
        check(f"候选 {c['id']} 时间都在段内",
              all(-0.01 <= m.start and m.end <= (c["end"] - c["start"]) + 0.01
                  for m in mapped))
        check(f"候选 {c['id']} 计数守恒",
              len(mapped) + dropped == len(all_cues),
              f"{len(mapped)}+{dropped} vs {len(all_cues)}")

    # ══════════════════════════════════════════════════════
    head("[3] 精确模式导出（重编码 + 烧字幕）")

    notes: list[str] = []
    prog_marks: list[float] = []

    def on_prog(ratio: float, msg: str, done: int) -> None:
        prog_marks.append(ratio)
        if len(prog_marks) % 2 == 0 or ratio >= 0.999:
            print(f"    [{ratio * 100:5.1f}%] {msg}")

    preset = subtitle_style.builtin_default()
    t0 = time.time()
    res = editor.export_clips(
        media, cands, out_base=OUT_BASE, task_id=0, task_name="阶段3自测",
        mode="exact", burn_subtitles=True, segments=segs, preset=preset,
        transport=transport, use_llm_split=use_llm,
        progress=on_prog)
    print(f"\n  {res.summary()}")
    print(f"  输出目录：{res.out_dir}")

    check("两条都成功", res.ok_count == 2, f"成功 {res.ok_count}/失败 {res.fail_count}")
    check("进度回调被调用", len(prog_marks) >= 3, f"{len(prog_marks)} 次")
    check("进度单调不减",
          all(prog_marks[i] <= prog_marks[i + 1] + 1e-6
              for i in range(len(prog_marks) - 1)))
    check("模式为精确", res.mode == "exact")
    check("清单文件生成", (res.out_dir / "导出清单.txt").exists())

    for it in res.items:
        if not it.ok:
            print(f"    失败：{it.title} — {it.error[:150]}")
            continue
        v = Path(it.video)
        print(f"    {v.name}  {v.stat().st_size / 1048576:.2f} MB  "
              f"字幕 {it.cues} 条  边界丢弃 {it.dropped_cues}")
        check(f"产物存在且非空 {v.name}", v.stat().st_size > 10240)
        check(f"SRT 已交付 {v.name}", bool(it.srt) and Path(it.srt).exists())

    # ══════════════════════════════════════════════════════
    head("[4] 快速模式（-c copy）与模式升降")

    fast_res = editor.export_clips(
        media, cands[:1], out_base=OUT_BASE, task_name="快速模式自测",
        mode="fast", burn_subtitles=False, segments=segs)
    print(f"  {fast_res.summary()}  模式={fast_res.mode}")
    check("快速模式成功", fast_res.ok_count == 1)

    notes2: list[str] = []
    up_res = editor.export_clips(
        media, cands[:1], out_base=OUT_BASE, task_name="升降档自测",
        mode="fast", burn_subtitles=True, segments=segs,
        mode_note=notes2)
    print(f"  快速+烧字幕 → 实际模式={up_res.mode}")
    print(f"  提示：{notes2}")
    check("烧字幕时自动升到精确模式", up_res.mode == "exact")
    check("升降档有提示", len(notes2) > 0)

    # ══════════════════════════════════════════════════════
    head("[5] 产物技术核验")

    if res.ok_count:
        first = Path(res.items[0].video)
        ffp = ffmpeg_tools.find_ffprobe()
        import subprocess
        r = subprocess.run(
            [str(ffp), "-v", "error", "-show_entries",
             "format=duration:stream=codec_name,width,height",
             "-of", "json", str(first)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        info = json.loads(r.stdout or "{}")
        dur = float(info.get("format", {}).get("duration", 0) or 0)
        want = cands[0]["end"] - cands[0]["start"]
        print(f"  时长 {dur:.3f}s（期望 {want:.3f}s，偏差 {dur - want:+.3f}s）")
        check("时长精确（|偏差| < 0.2s）", abs(dur - want) < 0.2)
        for s in info.get("streams", []):
            print(f"  流：{s.get('codec_name')} {s.get('width')}x{s.get('height')}")
        sw, sh = ffmpeg_tools.probe_video_size(media)
        print(f"  源视频显示尺寸：{sw}x{sh}"
              f"（editor 生成字幕时会用它做 ASS 的 PlayRes）")
        check("分辨率与源视频一致",
              bool(sw) and any(s.get("width") == sw and s.get("height") == sh
                               for s in info.get("streams", [])))

    # ══════════════════════════════════════════════════════
    head("[6] ★ 人工核对材料（字幕 ↔ 画面）")

    print("  下面为每条字幕生成「该时间点的画面帧」，供人工核对：")
    print("  判据：画面上烧的字幕文本，应该与该时刻真实说的话一致。")
    print("  （时间戳数字对但内容对不上，说明重映射错了。）")
    print()

    if res.ok_count:
        first_item = res.items[0]
        srt_path = Path(first_item.srt) if first_item.srt else None
        if srt_path and srt_path.exists():
            raw = srt_path.read_text(encoding="utf-8").strip().split("\n\n")
            picks = []
            if len(raw) >= 3:
                picks = [raw[0], raw[len(raw) // 2], raw[-1]]
            elif raw:
                picks = [raw[0]]
            for i, blk in enumerate(picks):
                lines = blk.strip().splitlines()
                if len(lines) < 3:
                    continue
                tc, text = lines[1], " ".join(lines[2:])
                print(f"    第 {i + 1} 个核对点：{tc}")
                print(f"      字幕文本：{text[:60]}")
            print()
            print(f"  完整 SRT：{srt_path}")
            print(f"  成片目录：{res.out_dir}")
            print()
            print("  核对方法（在播放器里打开成片）：")
            for i, blk in enumerate(picks):
                lines = blk.strip().splitlines()
                if len(lines) >= 3:
                    print(f"    {lines[1].split(' --> ')[0]}  应显示「{lines[2][:40]}」")

    # ══════════════════════════════════════════════════════
    head("结果")
    print(f"  通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL:
        print("  ⚠️ 有失败项，需排查")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
