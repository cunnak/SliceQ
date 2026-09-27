# -*- coding: utf-8 -*-
r"""真实端到端验证 —— 用真模型跑完整流水线。

这是阶段 2 第一次跑真实调用。要验的：
  V-D 模型返回的 JSON 是否符合提示词要求（数组/对象、字段齐全）
  V-E **粗筛的语义质量**（最关键的未知）
  V-F 真实成本是否落在预估范围内
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from sliceq import analyzer, config, pipeline, prompts, store

VIDEO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    "stage0/test_media/live_clip90.mp4")
VIDEO_SEC = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0
TASK_NAME = "真实分析测试"
PROFILE = ("这是一段哲学/时政类直播。找主播情绪最激动、语速明显加快、"
           "或者讲得特别精彩、有金句的片段。")


def main() -> int:
    # ⚠️ 自测必须自己把环境准备好，不能依赖"库里已经有表"。
    #    实测（2026-09-27）：隔离到空库后直接
    #    `sqlite3.OperationalError: no such table: task`。
    #    同类问题在 `selftest_stage2.py` 上先出现过一次 ——
    #    隔离会把这类"隐式依赖外部既有状态"的测试**逐个照出来**。
    config.ensure_dirs()
    store.init_db()

    if not VIDEO.exists():
        print(f"缺少素材 {VIDEO}")
        return 1

    print("=" * 78)
    print("真实端到端验证")
    print("=" * 78)

    # 清掉可能存在的旧缓存 —— 否则会命中 FakeTransport 时代的产物
    pipeline.clear_cache(VIDEO)

    t = analyzer.make_transport()
    print(f"  transport : {type(t).__name__}")
    print(f"  base_url  : {getattr(t, 'base_url', '')}")
    print(f"  model     : {getattr(t, 'model', '')}")
    print(f"  素材      : {VIDEO.name}  {VIDEO.stat().st_size / 1048576:.2f} MB")
    print()

    task_id = store.create_task(TASK_NAME, source_path=str(VIDEO),
                                duration_sec=VIDEO_SEC, width=1280, height=720)
    opts = pipeline.Options(
        profile=PROFILE,
        candidate_ratio=0.35,
        window_sec=600.0,
        refine_pad_sec=2.0,
        refine_thinking="off",     # 精析要结构化输出，不需要长推理
    )

    seen: list[str] = []
    confirms: list[str] = []

    def on_progress(p: pipeline.Progress) -> None:
        line = f"  [{p.stage:10s}] {p.ratio * 100:5.1f}%  {p.message}"
        line += f"　已花 ¥{p.spent_cny:.4f}"
        print(line, flush=True)
        seen.append(p.stage)

    def on_confirm(req: pipeline.ConfirmRequest) -> bool:
        confirms.append(req.kind)
        print(f"  ★ 确认点[{req.kind}]：{req.message}")
        print(f"     精析报价 ¥{req.refine_estimate:.4f}　"
              f"已花 ¥{req.spent_cny:.4f}　上限 ¥{req.hard_limit_cny:.2f}")
        return True                      # 一路跑完

    print(f"  时长      : {VIDEO_SEC}s\n")
    t0 = time.time()
    try:
        res = pipeline.run(task_id, VIDEO, opts, t,
                           on_progress=on_progress,
                           on_confirm=on_confirm)
    except Exception as exc:
        print(f"\n  ❌ 流水线失败：{type(exc).__name__}")
        for ln in str(exc).splitlines()[:10]:
            print("     ", ln)
        store.delete_task(task_id)
        return 1
    dt = time.time() - t0

    print()
    print("=" * 78)
    print("结果")
    print("=" * 78)
    print(f"  总耗时      : {dt:.1f}s")
    print(f"  候选段      : {len(res.candidates)}")
    print(f"  切片段      : {len(res.clips)}")
    print(f"  已确认的停顿: {confirms}")
    print(f"  实际花费    : ¥{res.spent_cny:.4f}")
    print(f"  启动时上限  : ¥{res.estimated_upper_bound:.2f}")
    print(f"  token 用量  : in={res.usage.input_tokens} "
          f"out={res.usage.output_tokens} "
          f"reasoning={res.usage.reasoning_tokens} "
          f"cache={res.usage.cache_hit_tokens}")
    if res.trimmed_seconds:
        print(f"  硬阀裁掉    : {res.trimmed_seconds:.1f}s")
    if res.degraded:
        print(f"  降级/提示（{len(res.degraded)} 条）：")
        for d in res.degraded[:8]:
            print(f"     - {d[:110]}")

    print()
    print("── 候选段（粗筛产出）──")
    for c in res.candidates:
        print(f"  {c.start:6.1f}→{c.end:6.1f}s  置信 {c.confidence:3d}  "
              f"[{c.source}]  {c.hint[:40]}")

    print()
    print("── 切片段（精析产出）──")
    for c in res.clips:
        print(f"  {c['start']:6.1f}→{c['end']:6.1f}s  {c.get('score', 0):3d}  "
              f"{c.get('kind', ''):8s}  {c.get('title', '')[:18]}")
        print(f"      {c.get('summary', '')[:70]}")

    # 落库核对
    print()
    print("── 落库核对 ──")
    for row in store.list_clips(task_id):
        print(f"  id={row['id']} {row['start']:.1f}~{row['end']:.1f} "
              f"type={row.get('type')!r} score={row.get('score')} "
              f"hint={(row.get('hint') or '')[:20]!r}")

    store.delete_task(task_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
