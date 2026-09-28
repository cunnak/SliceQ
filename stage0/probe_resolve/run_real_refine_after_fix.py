# -*- coding: utf-8 -*-
"""用**修复后的代码**重跑用户那条真实任务的分析（复用已有转录与粗筛缓存）。

⚠️⚠️ 这个脚本**故意不做数据隔离** —— 它要操作的正是用户的真实数据库
     （目标就是让用户在界面上看到结果）。运行前必须：
       ① 确认 SliceQ 没在运行（否则写库冲突）
       ② 备份 sliceq.db

为什么不用 GUI 的「重新分析」按钮：
   那个按钮调 `pipeline.clear_cache()`，会把**整个 work 目录**删掉 ——
   包括 84 分钟素材的转录中间产物。而 `transcribe_chunk()` 里
   `dest.unlink(missing_ok=True)` 意味着转录结果**本来就没有被当作缓存复用**，
   所以点一次「重新分析」要重付约 40 分钟的转录。
   本脚本直接从库里读回上次的 1825 条转录分段（`skip_transcribe=`），
   跳过转录，也复用 `screen.jsonl`（0 次粗筛调用、0 花费），
   只把**精析**这一步真跑一遍 —— 这正是 v0.1.2 修的地方。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\b1596\WorkBuddy\2026-09-23-13-06-47\SliceQ")

from sliceq import analyzer, asr, config, pipeline, store   # noqa: E402

TASK_ID = 219
RATIO = 0.20            # 筛选强度（默认档，最省）
HARD_LIMIT = 0.46       # 贴近用户界面显示的预期上限

store.init_db()
task = store.get_task(TASK_ID)
assert task, "任务不存在"
video = Path(task["source_path"])
print(f"任务    : {TASK_ID} {task['name'][:40]}")
print(f"素材    : {video}")
print(f"存在    : {video.exists()}（{video.stat().st_size / 1048576:.0f} MB）")
print(f"时长    : {task['duration_sec']:.1f} 秒")
print(f"筛选强度: {RATIO}    硬上限: ¥{HARD_LIMIT}")

# ── 从库里读回上次的转录（等价于"转录已完成"）──────────────
segs = store.list_transcript_segs(TASK_ID)
print(f"\n库中转录分段: {len(segs)} 条")
assert segs, "库里没有转录分段，无法跳过转录"
segments = [asr.Segment(int(r["start_ms"]), int(r["end_ms"]), str(r["text"]))
            for r in segs]
print(f"  首条: {segments[0].start_s:.3f}s {segments[0].text[:26]!r}")
print(f"  末条: {segments[-1].end_s:.3f}s {segments[-1].text[:26]!r}")

# ── 组装与 GUI 一致的调用 ───────────────────────────────────
transport = analyzer.make_transport()
print(f"\ntransport: {type(transport).__name__}（通道上限 "
      f"{pipeline.clip_size_cap(transport) / 1048576:.1f} MB）")

opts = pipeline.Options(
    profile=str(task.get("profile") or ""),
    user_prompt=str(task.get("user_prompt") or ""),
    candidate_ratio=RATIO,
    hard_limit_cny=HARD_LIMIT,
    concurrency=1,
)

t0 = time.time()
last = [""]


def on_prog(p: pipeline.Progress) -> None:
    line = f"[{p.stage}] {p.message}　花费 ¥{p.spent_cny:.4f}"
    if line != last[0]:
        print(f"  {time.time() - t0:6.1f}s  {line}", flush=True)
        last[0] = line


def on_confirm(req: pipeline.ConfirmRequest) -> bool:
    print(f"  ⏸ 确认点 {req.kind}: {req.message}", flush=True)
    if req.kind == "before_refine":
        return True                       # 用户的目标就是出切片，自动继续
    return False                           # 超预算一律停（尊重上限）


try:
    result = pipeline.run(
        TASK_ID, video, opts, transport,
        on_progress=on_prog, on_confirm=on_confirm,
        skip_transcribe=segments)
except BaseException as exc:                # noqa: BLE001
    print(f"\n★ 分析中断：{type(exc).__name__}: {exc}")
    print(f"  落库的提示：{store.get_run_hints(TASK_ID)}")
    raise

print("\n" + "=" * 72)
print("结果")
print("=" * 72)
print(f"  精析出切片 : {len(result.clips)} 条")
for c in result.clips:
    print(f"    {c['start']:8.2f} ~ {c['end']:8.2f}s  "
          f"评分 {c.get('score')}  {c.get('title') or c.get('summary', '')[:24]}")
print(f"  实际花费   : ¥{result.spent_cny:.4f}")
print(f"  提示       : {len(result.degraded)} 条")
for h in result.degraded:
    print(f"    · {h}")
print(f"  总耗时     : {time.time() - t0:.1f} 秒")
print(f"  数据库 clip: {len(store.list_clips(TASK_ID))} 行")
