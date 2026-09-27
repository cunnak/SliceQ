# -*- coding: utf-8 -*-
"""阶段 5 真实规模验证：长录播 + 多段高光 + 真实字幕。

前置实测（STAGE5-PRE-DRAFT-VERIFICATION.md）用的是 3 段 × 20 秒。
真实场景是 2.5 小时录播、十几段高光、上千条字幕 —— 本探针补这一段。

验的是"规模上去之后还对不对"：
    · 时间线总长 == Σ片段时长（段数多了之后累计误差会不会漂）
    · 字幕全部落在时间线范围内，且**每一段都有字幕**（不是只有前几段）
    · available_range 仍是素材真实总时长（不是被某个片段覆盖）
    · 剪映草稿的轨道/条数与预期一致

用法（默认导出到系统临时目录，**不碰你的剪映草稿目录**）：
    python probe_stage5_scaled.py                 # 5 段
    python probe_stage5_scaled.py --segments 12   # 12 段
    python probe_stage5_scaled.py --to-jianying   # 真的写进剪映草稿目录
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq._testing import assert_isolated, isolate_data_root   # noqa: E402

# ⚠️ 隔离：本探针默认只写临时目录。
#    `--to-jianying` 时才关掉隔离、写真实剪映目录（要人明确要求）。
_TO_JY = "--to-jianying" in sys.argv
if not _TO_JY:
    isolate_data_root(prefix="sliceq_scale_")
    assert_isolated()

from sliceq import asr, config, exporter as E, ffmpeg_tools      # noqa: E402

SRC_DIR = Path(r"C:\未明子录播\260925")
VIDEO = SRC_DIR / "[未明子]2026年09月25日00时录播.mp4"
SRT = SRC_DIR / "[未明子]2026年09月25日00时录播.srt"

_SEG = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--segments=")),
                5))
_WIN = 45.0          # 每段高光时长（秒）


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    t = re.compile(r"(\d+):(\d+):(\d+)[,.．](\d+)\s*-->\s*"
                   r"(\d+):(\d+):(\d+)[,.．](\d+)")
    text = path.read_text(encoding="utf-8", errors="replace")
    out: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", text):
        m = t.search(block)
        if not m:
            continue
        n = [int(x) for x in m.groups()]
        a = n[0] * 3600 + n[1] * 60 + n[2] + n[3] / 1000
        b = n[4] * 3600 + n[5] * 60 + n[6] + n[7] / 1000
        body = "\n".join(
            ln for ln in block.splitlines()
            if ln.strip() and not t.search(ln)
            and not ln.strip().isdigit())
        if body.strip():
            out.append((a, b, body.strip().replace("\n", " ")))
    return out


def main() -> int:
    print("=" * 78)
    print("阶段 5 真实规模验证")
    print("=" * 78)
    if not VIDEO.exists():
        print(f"缺少素材：{VIDEO}")
        return 1
    print(f"素材：{VIDEO.name}（{VIDEO.stat().st_size / 1048576:.1f} MB）")
    print(f"高光段数：{_SEG}（每段 {_WIN:.0f}s）")

    # ── 素材信息 ────────────────────────────────────────
    media = E.probe_media(VIDEO)
    print(f"\n[1] 素材：{media.width}×{media.height} @ {media.fps:g}fps"
          f"，时长 {media.duration:.1f}s（{media.duration / 60:.1f} 分钟）")
    if not media.ok or media.duration <= 0:
        print("❌ 素材信息不完整")
        return 1

    # ── 真实字幕 → Segment ─────────────────────────────
    cues_src = parse_srt(SRT)
    print(f"\n[2] 源字幕 {len(cues_src)} 条"
          f"，覆盖 {cues_src[0][0]:.0f}~{cues_src[-1][1]:.0f}s"
          f"（SRT 自身的条目数；进流水线后还会被断句细分）")
    segs = [asr.Segment(start_ms=int(a * 1000), end_ms=int(b * 1000), text=txt)
            for a, b, txt in cues_src]

    # ── 挑 N 段高光（分散在整条视频上，模拟真实候选分布）──
    total = media.duration
    hl: list[E.Highlight] = []
    for i in range(_SEG):
        frac = (i + 0.5) / _SEG                 # 均匀铺开
        start = max(0.0, min(total - _WIN - 1, frac * total))
        hl.append(E.Highlight(round(start, 3), round(start + _WIN, 3),
                              f"高光{i + 1}"))
    hl_total = sum(h.duration for h in hl)
    print(f"\n[3] 高光 {len(hl)} 段，共 {hl_total:.1f}s"
          f"（源起点 {hl[0].start:.0f}s ~ {hl[-1].start:.0f}s）")
    for h in hl:
        n_in = sum(1 for a, _b, _t in cues_src if h.start <= a < h.end)
        print(f"      {h.start:8.1f}~{h.end:8.1f}s  源字幕 {n_in:3} 条")

    # ── 导出 ────────────────────────────────────────────
    out = Path(tempfile.mkdtemp(prefix="sliceq_scale_out_")) if not _TO_JY \
        else (config.EXPORT_DIR / "trail_run")
    print(f"\n[4] 导出到 {out}")
    clips = [{"start": h.start, "end": h.end, "title_hint": h.title} for h in hl]
    r = E.export_drafts(VIDEO, clips, task_name="规模验证",
                        out_base=out, want_jianying=True, want_davinci=True,
                        segments=segs, use_llm_split=False,
                        draft_root=(None if _TO_JY else out / "jy_draft"))

    print(f"\n[5] 结果")
    print(f"    整体：{'成功' if r.ok else '失败'}  "
          f"通道 {r.ok_channels()}")
    print(f"    高光 {len(r.highlights)} 段｜字幕 {r.cue_count} 条"
          f"（边界丢弃 {r.dropped_cues}）")
    print(f"    素材真实时长 {media.duration:.1f}s（不是片段时长）")
    for n in r.notes:
        print(f"    提示：{n[:96]}")
    if r.error:
        print(f"    ❌ {r.error}")

    fails: list[str] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        print(f"    [{'OK  ' if ok else 'FAIL'}] {name}"
              + (f"  — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    # ── 核验 OTIO ───────────────────────────────────────
    print("\n[6] OTIO 核验")
    import opentimelineio as otio
    dav = r.channels.get("davinci")
    otio_file = None
    if dav and dav.ok:
        otio_file = next(iter(dav.out_dir.glob("*.otio")), None)
    if otio_file:
        tl = otio.adapters.read_from_file(str(otio_file))
        total_tl = otio.opentime.to_seconds(tl.duration())
        ck(f"时间线总长 == Σ片段时长（{hl_total:.1f}s，{len(hl)} 段）",
           abs(total_tl - hl_total) < 0.2,
           f"实得 {total_tl:.2f}s，偏差 {abs(total_tl - hl_total):.3f}s")
        ck("片段数正确", len(tl.tracks[0]) == len(hl),
           f"{len(tl.tracks[0])} vs {len(hl)}")
        avail = otio.opentime.to_seconds(
            tl.tracks[0][0].media_reference.available_range.duration)
        ck("available_range == 素材真实总时长（不是片段时长）",
           abs(avail - media.duration) < 0.5,
           f"{avail:.1f}s vs {media.duration:.1f}s")
        # 逐段核对 source_range（段数多了最容易累计漂移）
        bad = []
        for i, (clip, h) in enumerate(zip(tl.tracks[0], hl), 1):
            a = otio.opentime.to_seconds(clip.source_range.start_time)
            if abs(a - h.start) > 0.05:
                bad.append(f"第{i}段 {a:.2f}≠{h.start:.2f}")
        ck("★ 每段的 source_range 都对得上（无累计漂移）", not bad, str(bad[:3]))
    else:
        ck("OTIO 产出存在", False, "没找到 .otio 文件")

    # ── 核验字幕落位 ────────────────────────────────────
    print("\n[7] 字幕落位核验")
    if dav and dav.ok:
        srt_out = next(iter(dav.out_dir.glob("*.srt")), None)
        if srt_out:
            spans = parse_srt(srt_out)
            ck("字幕条数 > 0", len(spans) > 0, f"{len(spans)} 条")
            last = max(b for _a, b, _t in spans)
            ck(f"全部落在 0~{hl_total:.0f}s 内", last <= hl_total + 0.5,
               f"最晚 {last:.2f}s")
            # ★ 每一段都要有字幕（段数多了最容易"只有前面几段有"）
            acc = 0.0
            empty = []
            for i, h in enumerate(hl, 1):
                lo, hi = acc, acc + h.duration
                n = sum(1 for a, _b, _t in spans
                        if lo - 0.5 <= a < hi + 0.5)
                if n == 0:
                    empty.append(f"第{i}段")
                acc += h.duration
            ck("★ 每一段都有字幕（不是只有前几段）", not empty,
               f"空段：{empty}" if empty else f"{len(hl)} 段都有")
        else:
            ck("SRT 产出存在", False, "")

    # ── 核验剪映草稿 ────────────────────────────────────
    print("\n[8] 剪映草稿核验")
    jy = r.channels.get("jianying")
    if jy and jy.ok:
        content = json.loads((jy.out_dir / "draft_content.json")
                             .read_text(encoding="utf-8"))
        vids = [t for t in content.get("tracks", []) if t.get("type") == "video"]
        n_seg = sum(len(t.get("segments", [])) for t in vids)
        ck("视频轨段数 == 高光段数", n_seg == len(hl), f"{n_seg} vs {len(hl)}")
        canvas = content.get("canvas_config", {})
        ck("画布 == 素材尺寸",
           (canvas.get("width"), canvas.get("height")) == (media.width, media.height),
           f"{canvas.get('width')}×{canvas.get('height')}")
        meta = json.loads((jy.out_dir / "draft_meta_info.json")
                          .read_text(encoding="utf-8"))
        ck("meta 三字段已补齐",
           bool(meta.get("draft_name")) and bool(meta.get("draft_fold_path"))
           and bool(meta.get("draft_root_path")),
           f"{meta.get('draft_name')} @ {str(meta.get('draft_fold_path'))[-30:]}")
        ck("草稿落在预期目录", str(jy.out_dir).startswith(str(out))
           or _TO_JY, str(jy.out_dir))
    else:
        ck("剪映草稿产出", False, (jy.error if jy else "无该通道"))

    print("\n" + "=" * 78)
    print(f"结论：{'全部通过' if not fails else f'{len(fails)} 项失败：{fails}'}")
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
