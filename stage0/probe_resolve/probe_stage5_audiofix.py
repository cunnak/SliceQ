# -*- coding: utf-8 -*-
"""OTIO 音频轨修复 —— 真机验证材料生成器。

背景（2026-09-27 真机验收发现的缺陷）
====================================
`export_davinci()` 原先**只写一条 Video 轨**。达芬奇导入后会自己建一条音频轨，
但那条轨是**空的** —— 结果是：时间线上有画面、**没有声音**。

实测证据（达芬奇项目库 `Project.db`）：
    Sm2TiTrack  有 2 行：视频轨（name='SliceQ_主轨'）+ 音频轨（Type=1）
    Sm2TiItem   3 个片段**全部**挂在视频轨上，音频轨 0 个片段
⇒ 达芬奇**不会**从 OTIO 的视频片段里自动推断出音频，必须在 OTIO 里显式给音频轨。

修复
====
在 OTIO 里追加一条 `kind=Audio` 的轨，切点与视频轨逐段一致，
两个轨道各建**独立**的 Clip 对象（同一个对象 append 两次会互相覆盖 `range_in_parent`）。

本脚本做什么
============
1. 用真实录播（2.5h / 1882 条字幕）导出**与上一份材料完全相同的高光段**
   （3 段 × 45s，起点 1493/4479/7465s），便于新旧直接对照；
2. 产出后**自己解析 .otio 并断言音频轨存在**（不靠肉眼）；
3. 打印真机验证要点。

用法：
    python probe_stage5_audiofix.py

产出：
    %APPDATA%/SliceQ/export/audio_fix_check/<名字>/
        ├─ <名字>.otio
        ├─ <名字>.srt
        └─ 导入说明.txt
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq import asr, config, exporter as E                    # noqa: E402

SRC_DIR = Path(r"C:\未明子录播\260925")
VIDEO = SRC_DIR / "[未明子]2026年09月25日00时录播.mp4"
SRT = SRC_DIR / "[未明子]2026年09月25日00时录播.srt"
WIN = 45.0
N_SEG = 3

FAIL: list[str] = []


def ck(name: str, cond: bool, detail: str = "") -> None:
    tag = "OK  " if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    t = re.compile(r"(\d+):(\d+):(\d+)[,.．](\d+)\s*-->\s*"
                   r"(\d+):(\d+):(\d+)[,.．](\d+)")
    out: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8",
                                                    errors="replace")):
        m = t.search(block)
        if not m:
            continue
        n = [int(x) for x in m.groups()]
        a = n[0] * 3600 + n[1] * 60 + n[2] + n[3] / 1000
        b = n[4] * 3600 + n[5] * 60 + n[6] + n[7] / 1000
        body = "\n".join(ln for ln in block.splitlines()
                         if ln.strip() and not t.search(ln)
                         and not ln.strip().isdigit())
        if body.strip():
            out.append((a, b, body.strip().replace("\n", " ")))
    return out


def main() -> int:
    print("=" * 78)
    print("OTIO 音频轨修复 —— 真机验证材料")
    print("=" * 78)
    if not VIDEO.exists():
        print(f"❌ 缺少素材：{VIDEO}")
        return 1

    import opentimelineio as otio

    media = E.probe_media(VIDEO)
    print(f"\n[1] 素材 {media.width}×{media.height} @ {media.fps:g}fps，"
          f"{media.duration:.1f}s")
    ck("素材信息完整", media.ok and media.duration > 0)

    cues = parse_srt(SRT)
    print(f"\n[2] 真实字幕 {len(cues)} 条")
    segs = [asr.Segment(start_ms=int(a * 1000), end_ms=int(b * 1000), text=txt)
            for a, b, txt in cues]

    # 与上一份材料（trail_run / 规模验证_20260927_1851）**完全相同**的高光段，
    # 便于新旧直接对照：均匀铺开取 3 段，与 probe_stage5_scaled 的算法一致。
    hl: list[E.Highlight] = []
    for i in range(N_SEG):
        frac = (i + 0.5) / N_SEG
        start = max(0.0, min(media.duration - WIN - 1, frac * media.duration))
        hl.append(E.Highlight(round(start, 3), round(start + WIN, 3),
                              f"高光{i + 1}"))
    print(f"\n[3] 高光 {len(hl)} 段（每段 {WIN:.0f}s）—— 与旧材料同一批时间点：")
    for h in hl:
        print(f"      {h.start:8.3f} ~ {h.end:8.3f}s")

    out = config.EXPORT_DIR / "audio_fix_check"
    print(f"\n[4] 导出到 {out}")
    clips = [{"start": h.start, "end": h.end, "title_hint": h.title}
             for h in hl]
    r = E.export_drafts(VIDEO, clips, task_name="字幕对位验证",
                        out_base=out,
                        want_jianying=False,      # ← 只出达芬奇，不碰剪映草稿目录
                        want_davinci=True,
                        segments=segs, use_llm_split=False)
    ck("导出成功", r.ok, r.error or f"通道 {r.ok_channels()}")
    if not r.ok:
        return 1
    dv = r.channels["davinci"]
    ck("达芬奇通道成功", dv.ok, dv.error or dv.message)

    # ── ★ 自己解析产出的 .otio，验证音频轨真的写进去了 ────────
    otio_path = Path(dv.out_dir) / f"{Path(dv.files[0]).stem}.otio"
    if not otio_path.exists():
        cands = list(Path(dv.out_dir).glob("*.otio"))
        otio_path = cands[0] if cands else otio_path
    print(f"\n[5] 复核产出的 {otio_path.name}")
    ck("otio 文件存在", otio_path.exists(), f"{otio_path.stat().st_size} B"
       if otio_path.exists() else "")
    if not otio_path.exists():
        return 1

    tl = otio.adapters.read_from_file(str(otio_path))
    ck("★★ 时间线有 2 条轨（视频 + 音频）", len(tl.tracks) == 2,
       f"实得 {len(tl.tracks)} 条")
    if len(tl.tracks) != 2:
        return 1
    tv, ta = tl.tracks[0], tl.tracks[1]
    ck("轨 0 = Video", tv.kind == otio.schema.TrackKind.Video, str(tv.kind))
    ck("轨 1 = Audio", ta.kind == otio.schema.TrackKind.Audio, str(ta.kind))
    ck(f"视频轨 {len(hl)} 段", len(tv) == len(hl), str(len(tv)))
    ck(f"★★ 音频轨也有 {len(hl)} 段（= 有声音）", len(ta) == len(hl),
       str(len(ta)))
    ck("音视频是不同对象（不共用 Clip）",
       all(v is not a for v, a in zip(tv, ta)))
    ck("音频轨引用同一个源文件",
       all(str(a.media_reference.target_url)
           == str(tv[0].media_reference.target_url) for a in ta))
    same = all(
        abs(otio.opentime.to_seconds(a.source_range.start_time)
            - otio.opentime.to_seconds(v.source_range.start_time)) < 1e-6
        for v, a in zip(tv, ta))
    ck("音频轨逐段源内起点与视频轨一致", same)
    ck("global_start_time == 0", tl.global_start_time.value == 0,
       str(tl.global_start_time))

    guide = Path(dv.out_dir) / "导入说明.txt"
    ck("导出了 导入说明.txt", guide.exists())
    if guide.exists():
        g = guide.read_text(encoding="utf-8")
        ck("★ 说明里有「起始时间码」小节（R20）", "起始时间码" in g)
        ck("★ 说明里给了确切入口（右键时间线 → 时间线设置）",
           "右键那条时间线" in g and "时间线设置" in g)
        ck("★★ 说明里警告「每导入一次 OTIO 都会新建时间线、起点回到默认」",
           "每导入一次" in g and "自动带过来" in g)
        ck("★ 说明里有「用哪份 SRT」对照表", "_达芬奇默认时间码.srt" in g)
        ck("★ 说明里提醒哨兵占位条不要删", "占位" in g and "不要删" in g)
        ck("说明里写了本素材帧率", f"{media.fps:g}" in g)

    # ★ 两份 SRT 都要在原位，且时间码相差 3600s
    import re as _re
    _z = Path(dv.out_dir) / f"{otio_path.stem}.srt"
    _d = Path(dv.out_dir) / f"{otio_path.stem}_达芬奇默认时间码.srt"
    ck("★ 零基 SRT 已产出", _z.exists())
    ck("★ 达芬奇默认时间码 SRT 已产出（+1h）", _d.exists())
    if _z.exists() and _d.exists():
        def _s(t):
            return [int(a)*3600+int(b)*60+int(c)+int(d)/1000 for a, b, c, d
                    in _re.findall(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", t)]
        _a = _s(_z.read_text(encoding="utf-8"))
        _b = _s(_d.read_text(encoding="utf-8"))
        ck("★★ 两份 SRT 时间码逐条相差恰好 3600s",
           len(_a) == len(_b) and len(_a) > 0
           and all(abs(y - x - 3600) < 1e-6 for x, y in zip(_a, _b)),
           f"{len(_a)} vs {len(_b)} 条")

    print("\n" + "=" * 78)
    if FAIL:
        print(f"❌ 失败 {len(FAIL)} 项：{FAIL}")
        return 1
    print("✅ 全部通过 —— 音频轨已写入，请做真机确认")
    print("=" * 78)
    print(f"\n【请你验证】\n  达芬奇 → File > Import > Timeline → 选")
    print(f"      {otio_path}")
    print("  媒体弹窗选【是】并指向 C:\\未明子录播\\260925")
    print("  预期：")
    print("    · 时间线上**有画面**（旧版也有）")
    print("    · ★ **有声音**（这是本次修复的目标 —— 旧版这里是静音）")
    print("    · 音频轨上有 3 个片段（不是空轨）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
