# -*- coding: utf-8 -*-
"""阶段 5 前置实测：剪映草稿 + 达芬奇 OTIO（真实素材 / 真实字幕）。

## 要回答的问题

1. **剪映 11.5.3 能否接受 pyJianYingDraft 生成的草稿？**
   阶段 0 验证过（同版本），但那次是最简结构（3 段合成素材 + 4 条字幕）。
   这次用**真实素材 + 真实中文 SRT + 跨段重映射后的时间轴**，
   结构更贴近阶段 5 的真实输出。

2. **字幕在「拼接时间线」上的落位对不对？**
   这是甲方案最容易出错的一环：漏做重映射，SRT 依然合法、
   剪映也照常接受，**不报任何错**，只是字幕全乱。

3. **OTIO 能否被达芬奇正确导入？**（阶段 0 已验证，这里复核甲方案结构）

## 产出

    stage0/test_drafts/stage5_probe/
      ├─ SliceQ_阶段5实测.otio      达芬奇时间线（甲方案）
      ├─ SliceQ_阶段5实测.srt       重映射后的字幕
      ├─ 导入说明.txt
      └─ subtitle_check.txt         人工核对表（文本 ↔ 源时间 ↔ 时间线时间）

剪映草稿直接写进剪映的草稿目录（剪映不扫任意目录）。

## 验收方式

**脚本自身能判的**：文件产出、结构合法、字幕条数与落位。
**必须由人判的**：剪映里能不能看见草稿、字幕位置对不对
（我不能启动 GUI）——详情见脚本末尾的提示。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq import ffmpeg_tools, subtitle as S              # noqa: E402

SRC = Path(r"C:\未明子录播\260925\[未明子]2026年09月25日00时录播.mp4")
SRT_IN = Path(r"C:\未明子录播\260925\[未明子]2026年09月25日00时录播.srt")

OUT = ROOT / "stage0" / "test_drafts" / "stage5_probe"
JY_ROOT = Path(os.environ["LOCALAPPDATA"]) / "JianyingPro" / "User Data" \
    / "Projects" / "com.lveditor.draft"
DRAFT_NAME = "SliceQ_阶段5实测"

# 三段"高光"（源视频时间，秒）—— **由脚本自动挑选**，规则见 pick_dense()。
# ⚠️ 第一版手工挑的（5400/7200/9000）全错：5400~5420 那 20 秒里只有 1 句字幕，
#    第三段还超出了素材时长（素材实际只有 8958.9s）。
HIGHLIGHTS: list[tuple[float, float]] = []
HIGHLIGHT_SEC = 20.0
TITLES = [
    "高光片段一",
    "高光片段二",
    "高光片段三",
]


def probe_duration(media: Path) -> float:
    """用 ffprobe 取素材总时长（秒）。取不到返回 0。"""
    import subprocess
    probe = ffmpeg_tools.find_ffprobe()
    if not probe:
        return 0.0
    try:
        r = subprocess.run(
            [str(probe), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(media)],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace")
        return float((r.stdout or "").strip() or 0.0)
    except Exception:                                       # noqa: BLE001
        return 0.0


def pick_dense(cues: list, *, win: float, n: int = 3,
               min_gap: float = 90.0, limit: float = 0.0
               ) -> list[tuple[float, float]]:
    """挑出字幕最密集的 n 个互不重叠窗口 —— 模拟「AI 选出的候选高光」。

    ⚠️ 为什么必须"挑"而不是随手定：

       这条 SRT 是 **有语音才有字幕**（不是等间隔铺满），
       密度**很不均匀**。第一次实测我挑了 5400s 附近，
       结果那 20 秒里只有 1 句 —— 1882 条字幕映射完只剩 1 条，
       看起来像"映射逻辑写错了"，其实是**素材选得不对**。

       教训（本项目第二次踩）：**跑效果验证前，先问
       「这个输入里有没有我想要的那个东西」**。

    另外还会把窗口夹进素材真实时长内 —— 第一版挑到 9000s，
    而素材只有 8958.9s，直接让剪映草稿生成抛了异常。
    """
    cands: list[tuple[int, float]] = []
    for c in cues:
        s = c.src_start
        if limit and s + win > limit:
            continue
        cnt = sum(1 for x in cues if s <= x.src_start < s + win)
        cands.append((cnt, s))
    cands.sort(key=lambda t: (-t[0], t[1]))

    picked: list[float] = []
    for cnt, s in cands:
        if cnt < 2:
            break
        if any(abs(s - p) < win + min_gap for p in picked):
            continue
        picked.append(s)
        if len(picked) >= n:
            break
    return [(s, s + win) for s in sorted(picked)]


# ─────────────────────────────────────────────────────────────
# SRT 解析（读回源字幕）
# ─────────────────────────────────────────────────────────────
_TS = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def _sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(path: Path) -> list[S.Cue]:
    """解析 SRT 成源时间轴的 cues。"""
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    out: list[S.Cue] = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 3:
            continue
        m = _TS.search(lines[1])
        if not m:
            continue
        g = m.groups()
        a, b = _sec(*g[:4]), _sec(*g[4:])
        text = "\n".join(lines[2:]).strip()
        if text:
            out.append(S.Cue(start=a, end=b, main=text, sub="",
                             src_start=a, src_end=b))
    return out


def main() -> int:
    if not SRC.exists() or not SRT_IN.exists():
        print(f"❌ 素材不存在：{SRC} / {SRT_IN}")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    print("=" * 76)
    print("阶段 5 前置实测")
    print("=" * 76)

    # ── 1. 素材信息 ────────────────────────────────────
    w, h = ffmpeg_tools.probe_video_size(SRC)
    fps = 30.0
    try:
        info = ffmpeg_tools.probe_media(SRC)
        fps = float(info.get("fps") or 30.0)
    except Exception:                                       # noqa: BLE001
        pass
    print(f"\n[1] 素材")
    print(f"    {SRC.name}")
    print(f"    {w}×{h} @ {fps:g}fps")

    # ── 2. 读源字幕 ────────────────────────────────────
    cues_src = parse_srt(SRT_IN)
    span = (cues_src[0].src_start, cues_src[-1].src_end) if cues_src else (0, 0)
    print(f"\n[2] 源字幕：{len(cues_src)} 条，覆盖 "
          f"{span[0]:.0f}s ~ {span[1]:.0f}s")

    dur = probe_duration(SRC)
    print(f"    素材真实时长：{dur:.3f}s"
          + ("（⚠️ 取不到，用字幕末尾兜底）" if not dur else ""))
    if not dur:
        dur = span[1]

    HIGHLIGHTS.extend(pick_dense(cues_src, win=HIGHLIGHT_SEC, n=3, limit=dur))
    print(f"    自动挑选的高光窗口（按字幕密度）：")
    for a, b in HIGHLIGHTS:
        n_in = sum(1 for c in cues_src if a <= c.src_start < b)
        print(f"      {a:.1f}~{b:.1f}s  含 {n_in} 条字幕")

    # ── 3. ★ 跨段重映射（甲方案的核心）────────────────
    print(f"\n[3] 跨段重映射（remap_to_timeline）")
    print(f"    高光片段：")
    acc = 0.0
    for i, (a, b) in enumerate(HIGHLIGHTS):
        print(f"      片段{i+1}  源 {a:.0f}~{b:.0f}s（{b-a:.0f}s）"
              f" → 时间线 {acc:.0f}~{acc+b-a:.0f}s")
        acc += b - a
    total = acc
    print(f"    时间线总时长：{total:.0f}s")

    cues_tl, dropped = S.remap_to_timeline(cues_src, HIGHLIGHTS)
    print(f"    字幕：{len(cues_src)} 条 → 保留 {len(cues_tl)}，丢弃 {dropped}"
          f"（{len(cues_tl) + dropped} = {len(cues_src)} ✓）")

    if not cues_tl:
        print("    ❌ 一条字幕都没留下 —— 高光区间选错了？")
        return 1

    # 每段的字幕条数（确认三段都有）
    print(f"    落在各片段的字幕数：")
    for i, (a, b) in enumerate(HIGHLIGHTS):
        n = sum(1 for c in cues_tl if a <= c.src_start < b)
        print(f"      片段{i+1}: {n} 条")

    # ── 4. 写重映射后的 SRT ────────────────────────────
    srt_out = OUT / f"{DRAFT_NAME}.srt"
    srt_out.write_text(S.to_srt(cues_tl, bilingual=False), encoding="utf-8")
    print(f"\n[4] 重映射 SRT → {srt_out.name}"
          f"（{srt_out.stat().st_size:,} 字节）")

    # 人工核对表：文本 ↔ 源时间 ↔ 时间线时间
    check = OUT / "subtitle_check.txt"
    lines = ["字幕人工核对表（验收用）",
             "=" * 76,
             "用法：在剪映/达芬奇里定位到「时间线时间」，看画面对不对得上这句话。",
             "      若对不上，说明重映射错了（这是甲方案最容易出错的一环）。",
             "=" * 76, ""]
    for i, c in enumerate(cues_tl[:25], 1):
        lines.append(
            f"[{i:2}] 时间线 {c.start:7.2f}~{c.end:7.2f}s"
            f"　←　源 {c.src_start:8.2f}~{c.src_end:8.2f}s")
        lines.append(f"      {c.main[:60]}")
        lines.append("")
    check.write_text("\n".join(lines), encoding="utf-8")
    print(f"    人工核对表 → {check.name}（前 25 条）")

    # ── 5. 生成 OTIO（甲方案）──────────────────────────
    print(f"\n[5] 生成 OTIO（达芬奇·甲方案）")
    try:
        import opentimelineio as otio

        rate = int(round(fps)) or 30
        # ⚠️ 不要再加 "SliceQ_" 前缀 —— DRAFT_NAME 里已经有了，
        #    第一版拼成 "SliceQ_SliceQ_阶段5实测"。
        tl = otio.schema.Timeline(name=DRAFT_NAME)
        tl.global_start_time = otio.opentime.RationalTime(0, rate)
        track = otio.schema.Track(name="SliceQ_主轨",
                                  kind=otio.schema.TrackKind.Video)

        src_url = "file:///" + str(SRC).replace("\\", "/")
        cursor = otio.opentime.RationalTime(0, rate)
        for i, (a, b) in enumerate(HIGHLIGHTS):
            # ⚠️ 不要叫 `dur` —— 外层那个 `dur` 是素材总时长（float），
            #    同名会把 float 覆盖成 RationalTime，于是第二次迭代
            #    `dur * rate` 变成 RationalTime * int 直接抛 TypeError，
            #    而报错信息只说"类型不支持"，完全看不出是变量被覆盖了。
            seg_dur = otio.opentime.RationalTime((b - a) * rate, rate)
            # ⚠️ available_range **必须显式设**，否则 OTIO 数据模型不完整。
            #    而且要是**素材的真实总时长**（不是 0，也不是片段时长）——
            #    它表示"这个媒体文件总共有多长"，达芬奇据此判断引用是否有效。
            ref = otio.schema.ExternalReference(
                target_url=src_url,
                available_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, rate),
                    duration=otio.opentime.RationalTime(dur * rate, rate)))
            clip = otio.schema.Clip(
                name=f"{i+1}_{TITLES[i]}",
                media_reference=ref,
                source_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(a * rate, rate),
                    duration=seg_dur))
            clip.range_in_parent = otio.opentime.TimeRange(
                start_time=cursor, duration=seg_dur)
            track.append(clip)
            cursor = cursor + seg_dur

        tl.tracks.append(track)

        otio_out = OUT / f"{DRAFT_NAME}.otio"
        otio.adapters.write_to_file(tl, str(otio_out))
        print(f"    → {otio_out.name}（{otio_out.stat().st_size:,} 字节）")
        print(f"    片段数 {len(track)}，时间线总长 "
              f"{otio.opentime.to_seconds(tl.duration()):.1f}s")
        print(f"    起点 {otio.opentime.to_timecode(tl.global_start_time, rate)}")
    except Exception as exc:                                # noqa: BLE001
        print(f"    ❌ OTIO 生成失败：{type(exc).__name__}: {exc}")
        return 1

    # ── 6. 导入说明.txt ────────────────────────────────
    note = OUT / "导入说明.txt"
    note.write_text(f"""SliceQ 导出说明
{'=' * 60}

【达芬奇】导入 {DRAFT_NAME}.otio
  1. 菜单 File > Import > Timeline...
  2. 首次导入会弹「找不到片段」或媒体离线，这是**正常现象** ——
     .otio 只记录切点，不携带视频本身。
     弹窗问是否定位媒体时选【是】，然后选择这个文件夹：
         {SRC.parent}
  3. 导入后时间线上应能看到画面。
     ⚠️ 不要移动或重命名源视频，否则链接会断。

【剪映】草稿已直接写入剪映的草稿目录：
     {JY_ROOT / DRAFT_NAME}
  打开剪映 → 首页「本地草稿」里应出现「{DRAFT_NAME}」。
  字幕、标题都在草稿里；**成片需要在剪映里手动导出**（SliceQ 不代劳）。

【素材】
  源视频：{SRC}
  ⚠️ 同名冲突提醒：达芬奇按**文件名**匹配媒体池里的已有条目。
     若你的媒体池里已经有同名文件，可能挂错。必要时先清空媒体池。

【三段时间线对照】
""", encoding="utf-8")
    acc = 0.0
    with note.open("a", encoding="utf-8") as f:
        for i, (a, b) in enumerate(HIGHLIGHTS):
            f.write(f"  片段{i+1}  「{TITLES[i]}」"
                    f"  时间线 {acc:.0f}~{acc+b-a:.0f}s"
                    f"  ←  源 {a:.0f}~{b:.0f}s\n")
            acc += b - a
    print(f"    导入说明.txt 已生成")

    # ── 7. 生成剪映草稿 ────────────────────────────────
    print(f"\n[6] 生成剪映草稿（pyJianYingDraft）")
    import pyJianYingDraft as draft
    from pyJianYingDraft import SEC, trange

    stage_dir = OUT / "jianying_build"
    if stage_dir.exists():
        shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    folder = draft.DraftFolder(str(stage_dir))
    script = folder.create_draft(DRAFT_NAME, width=int(w), height=int(h),
                                 fps=int(round(fps)), allow_replace=True)
    print(f"    草稿已创建：{w}×{h} @ {int(round(fps))}fps")

    vtrack = script.append_track(
        draft.TrackSpec(draft.TrackType.video, name="SliceQ_主轨"))
    material = draft.VideoMaterial(str(SRC))

    cursor = 0.0
    for i, (a, b) in enumerate(HIGHLIGHTS):
        dur = b - a
        seg = draft.VideoSegment(
            material,
            target_timerange=trange(f"{cursor}s", f"{dur}s"),
            source_timerange=trange(f"{a}s", f"{dur}s"))
        script.add_segment(seg, vtrack)
        print(f"      片段{i+1}: 源 {a:.0f}s+{dur:.0f}s → 时间线 {cursor:.0f}s")
        cursor += dur

    # 字幕轨：用**重映射后**的 SRT —— 这一步错了就是字幕全乱
    srt_for_jy = stage_dir / f"{DRAFT_NAME}.srt"
    srt_for_jy.write_text(S.to_srt(cues_tl, bilingual=False), encoding="utf-8")
    try:
        script.import_srt(str(srt_for_jy), track_name="SliceQ_字幕",
                          time_offset=0.0)
        print(f"    字幕轨：导入 {len(cues_tl)} 条（重映射后）")
    except Exception as exc:                                # noqa: BLE001
        print(f"    ❌ 字幕轨失败：{type(exc).__name__}: {exc}")

    # 标题轨：每条高光的标题
    ttrack = script.append_track(
        draft.TrackSpec(draft.TrackType.text, name="SliceQ_标题"))
    cursor = 0.0
    for i, (a, b) in enumerate(HIGHLIGHTS):
        title = draft.TextSegment(
            f"{i+1}. {TITLES[i]}",
            trange(f"{cursor}s", f"{min(4.0, b - a)}s"),
            clip_settings=draft.ClipSettings(transform_y=-0.75))
        script.add_segment(title, ttrack)
        cursor += b - a
    print(f"    标题轨：{len(HIGHLIGHTS)} 条")

    script.save()
    built = stage_dir / DRAFT_NAME
    print(f"    已保存 → {built}")

    # ── 8. 部署到剪映草稿目录 ──────────────────────────
    print(f"\n[7] 部署到剪映草稿目录")
    if not JY_ROOT.exists():
        print(f"    ⚠️ 剪映草稿目录不存在：{JY_ROOT}")
        return 1
    dest = JY_ROOT / DRAFT_NAME
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(built, dest)
    print(f"    → {dest}")

    # 补齐 meta 里默认可能为空的字段（TASKS 提到的坑）
    meta_p = dest / "draft_meta_info.json"
    if meta_p.exists():
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        before = {k: meta.get(k) for k in
                  ("draft_name", "draft_fold_path", "draft_root_path")}
        meta["draft_name"] = DRAFT_NAME
        meta["draft_fold_path"] = str(dest)
        meta["draft_root_path"] = str(JY_ROOT)
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"    meta 补齐：{before}")
        print(f"            → draft_name={meta['draft_name']!r} "
              f"fold_path={meta['draft_fold_path']!r}")

    for f in sorted(dest.iterdir()):
        print(f"      {f.name:<28} {f.stat().st_size:>12,} 字节")

    # ── 9. 汇总 ────────────────────────────────────────
    print("\n" + "=" * 76)
    print("产出汇总")
    print("=" * 76)
    print(f"  达芬奇包  {OUT}")
    print(f"    ├─ {DRAFT_NAME}.otio")
    print(f"    ├─ {DRAFT_NAME}.srt（重映射后）")
    print(f"    ├─ 导入说明.txt")
    print(f"    └─ subtitle_check.txt（人工核对表）")
    print(f"  剪映草稿  {dest}")
    print()
    print("=" * 76)
    print("★ 需要你亲手验证的两件事")
    print("=" * 76)
    print(f"""
【一】打开剪映，看首页「本地草稿」里有没有「{DRAFT_NAME}」
      有 → 点开，检查三件事：
        ① 时间线是 3 段视频（不是 1 段、也不是空）
        ② 字幕轨上有字幕，位置大致对应
        ③ 标题轨上有 3 条文字
      然后关掉剪映（让它写日志）。

【二】达芬奇导入 {DRAFT_NAME}.otio
      File > Import > Timeline，弹「找不到片段」选【是】
      并指向 {SRC.parent}
      检查：媒体池有内容、时间线上**有画面**（不是红色离线）、3 个片段。

剪映那边我能自己验：读 %LOCALAPPDATA%\\JianyingPro\\User Data\\Log 下的日志，
找 suc:true / copy_draft_external 条目。
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
