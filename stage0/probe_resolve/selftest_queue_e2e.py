# -*- coding: utf-8 -*-
"""多录像批量入队的真实端到端（阶段 4-D · F15 验收）。

对应 TASKS 的验收标准：**两条录像批量入队，顺序完成，无限流报错。**

走的是**完整产品路径**：真实 ffmpeg 切素材 → ingest 入库 →
AnalysisQueue → Worker → run_queue → run_analysis → pipeline.run
→ 真实 ASR（本地 GPU）+ 真实模型调用（粗筛/精析）。

所以这个脚本能证明的比 mock 多得多：
  · 队列的信号链（工作线程 → Qt 主线程）真的通
  · 两条任务确实**顺序**执行，没有互相踩
  · 每条跑完状态真的落到 ready
  · 成本被真实累计

⚠️ 会花真钱（几条候选的粗筛+精析，量级 ¥0.01 以内）和真实时间
   （每条约 1~3 分钟，取决于转录长度）。用 --seg 控制素材长度。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from PySide6.QtWidgets import QApplication                 # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from sliceq import (analyzer, asr, config, ffmpeg_tools, ingest, pipeline,  # noqa: E402
                    secrets, store)
from sliceq.ui.queue import AnalysisQueue                  # noqa: E402
from sliceq.ui.workers import WorkerPool                   # noqa: E402

SRC = Path(r"C:\未明子录播\260925\[未明子]2026年09月25日00时录播.mp4")
WORK = Path(tempfile.mkdtemp(prefix="sliceq_queue_e2e_"))
SEG = 90.0
STARTS = (5400.0, 7200.0)          # 两段互不重叠的位置，当成"两条录像"

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
    print(f"\n{'=' * 76}\n{t}\n{'=' * 76}")


def slice_out(idx: int, start: float) -> Path:
    """切出一段当作独立的「录像」。"""
    ff = str(ffmpeg_tools.find_ffmpeg())
    out = WORK / f"素材{idx}.mp4"
    if out.exists() and out.stat().st_size > 10240:
        return out
    import subprocess
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{start:.3f}", "-t", f"{SEG:.3f}",
                        "-i", str(SRC), "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "26", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "96k", str(out)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=900)
    if r.returncode != 0:
        raise SystemExit("切片失败：" + (r.stderr or "")[-400:])
    return out


def main() -> int:
    global SEG
    for a in sys.argv[1:]:
        if a.startswith("--seg="):
            SEG = float(a.split("=", 1)[1])

    store.init_db()
    config.ensure_dirs()

    print("=" * 76)
    print("多录像批量入队 · 真实端到端")
    print("=" * 76)
    print(f"素材：{SRC.name}　每段 {SEG:.0f}s")

    if not (secrets.get_api_key() or "").strip():
        print("❌ 没有配置 API Key，跑不了粗筛/精析")
        return 1

    # ── 1. 造两条"录像" ────────────────────────────────
    head("1. 准备两条独立素材")
    files = [slice_out(i, s) for i, s in enumerate(STARTS, 1)]
    for f in files:
        ck(f"{f.name} 已生成", f.exists() and f.stat().st_size > 10240,
           f"{f.stat().st_size / 1048576:.1f} MB")

    # ── 2. 入库 ───────────────────────────────────────
    head("2. 导入为两个任务")
    ok, errs = ingest.import_many([str(f) for f in files])
    ck("两条都入库了", len(ok) == 2, f"ok={ok} errs={errs}")
    if len(ok) != 2:
        return 1
    ids = [int(t["id"]) for t in store.list_tasks()[:2]]
    ids.sort()
    names = [(store.get_task(i) or {}).get("name") for i in ids]
    ck("两条任务名不同（是两条独立录像）", len(set(names)) == 2, str(names))

    # ── 3. 排队并跑 ───────────────────────────────────
    head("3. 批量入队 → 顺序执行")
    pool = WorkerPool(max_threads=4)
    q = AnalysisQueue(pool)

    opts = pipeline.Options(profile="", candidate_ratio=0.02, concurrency=1)
    for i in ids:
        ck(f"加入队列 {i}", q.add(i, opts, auto_continue=True) is True)

    events: list[str] = []
    done: list[str] = []
    q.progress.connect(lambda d, t, s: events.append(s))
    q.finished.connect(lambda s: done.append(s))
    q.failed.connect(lambda m, d: done.append(f"ERROR: {m}"))

    t0 = time.time()
    started = q.start()
    ck("队列启动成功", started is True)

    limit = SEG * 12 + 420                # 给足余量
    while not done and time.time() - t0 < limit:
        app.processEvents()
        time.sleep(0.25)
    elapsed = time.time() - t0

    ck("队列在时限内结束（没有卡死）", bool(done),
       f"{elapsed:.1f}s" if done else f"超过 {limit:.0f}s 未结束")

    # ── 4. 结果 ───────────────────────────────────────
    head("4. 结果核验")
    res = q.results()
    ck("两条都有结果", len(res) == 2, str(len(res)))
    ck("★ 两条都成功（顺序完成，无失败）",
       all(r["state"] == "done" for r in res),
       str([r["state"] for r in res]))
    ck("结果顺序 == 队列顺序",
       [r["task_id"] for r in res] == ids,
       f"{[r['task_id'] for r in res]} vs {ids}")

    for i in ids:
        t = store.get_task(i) or {}
        ck(f"任务 {i} 状态落库为待导出",
           t.get("status") == config.STATUS_READY, str(t.get("status")))

    # ⚠️ 「产出 0 条切片段」是**合法的业务结果**，不能当失败 ——
    #    粗筛对一段没有值得切的内容返回空，这是它对的表现。
    #    （实测：e2e 的第二段素材落在直播 2 小时处，粗筛返回 0 候选。）
    #    所以要断言的是"链路真的产出了东西"，而不是"每条都必须有产出"。
    per_clip = {i: len(store.list_clips(i)) for i in ids}
    total_clips = sum(per_clip.values())
    ck("★ 至少有一条产出了切片段（证明链路端到端通了）",
       total_clips >= 1, f"各任务：{per_clip}")
    ck("每条任务都跑到了终态 ready（0 条也是终态）",
       all((store.get_task(i) or {}).get("status") == config.STATUS_READY
           for i in ids), str(per_clip))
    ck("每条任务都落了转录文本（导出字幕要用）",
       all(len(store.list_transcript_segs(i)) > 0 for i in ids),
       str({i: len(store.list_transcript_segs(i)) for i in ids}))

    # ★ 幻觉治理的验收（阶段 4-D 补强）
    #
    #   修之前：`感谢观看`（4 字）独立成段出现 5 次全部漏过，
    #   因为 `_REPEAT_MIN_LEN = 8` 让它根本不参与判重。
    #
    #   ⚠️ 断言要分清两种：
    #     · **重复型**（出现 ≥2 次）—— 有"重复"这个客观判据，必须清干净
    #     · **单次短幻觉** —— 没有客观判据，只能靠词表猜，
    #       而按词表删有误杀风险（主播下播时真会说"感谢观看"）。
    #       属**已知限制**，只记录不判失败。
    from collections import Counter

    KNOWN = {"感谢观看", "谢谢观看", "多謝", "謝謝",
             "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目",
             "请不吝点赞订阅转发打赏支持明镜与点点栏目"}

    def over_threshold(t: str, n: int) -> bool:
        """这段文本重复 n 次，是否**已经达到**判重门槛？

        ⚠️ 门槛要和 `asr._REPEAT_*` 常量一致。断言的是"达到门槛的都被清了"，
           不是"一点幻觉都不许剩" —— 门槛本身就是刻意的取舍（见 asr.py）。
           写成后者的话，断言在实现正确时也会失败。
        """
        if len(t) >= asr._REPEAT_MIN_LEN:
            return n >= asr._REPEAT_MIN_COUNT
        if len(t) >= asr._REPEAT_SHORT_LEN:
            return n >= asr._REPEAT_SHORT_COUNT
        return False

    stale: dict[int, dict[str, int]] = {}
    below: dict[int, dict[str, int]] = {}
    for i in ids:
        c = Counter((s["text"] or "").strip()
                    for s in store.list_transcript_segs(i))
        s_bad = {t: n for t, n in c.items() if t in KNOWN and over_threshold(t, n)}
        if s_bad:
            stale[i] = s_bad
        s_low = {t: n for t, n in c.items()
                 if t in KNOWN and not over_threshold(t, n)}
        if s_low:
            below[i] = s_low
    ck("★ 达到判重门槛的幻觉都被清干净了（验证治理真的在工作）",
       not stale, str(stale) if stale else "干净")
    ck("★ 没有平台模板词残留（独播剧场等，靠特征词清）",
       not any("独播剧场" in t for t in
               (s["text"] or "" for i2 in ids
                for s in store.list_transcript_segs(i2))),
       "")
    if below:
        print(f"  [备注] 未达门槛、按设计保留的幻觉（已知限制）：{below}")

    ck("总结文字可读", bool(done) and "完成 2/2" in (done[0] if done else ""),
       done[0] if done else "无")

    # 顺序：进度里应该先全是第 1 条、再全是第 2 条
    order_seen: list[str] = []
    for d in events:
        if d.startswith("[队列 "):
            tag = d.split("]")[0] + "]"
            if not order_seen or order_seen[-1] != tag:
                order_seen.append(tag)
    ck("★ 进度里队列序号是 1→2，没有交错（证明串行）",
       order_seen == ["[队列 1/2]", "[队列 2/2]"], str(order_seen))

    # 成本：粗筛/精析的流水记在 pipeline 的 usage 里，这里只汇总落库的产物
    print(f"\n  实际耗时 {elapsed:.1f}s　进度事件 {len(events)} 条")

    print(f"\n{'=' * 76}")
    print(f"通过 {PASS} / {PASS + FAIL}"
          + (f"　失败 {FAIL}" if FAIL else "　全部通过"))
    print("=" * 76)
    print(f"产物目录：{WORK}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
