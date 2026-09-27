# -*- coding: utf-8 -*-
"""
SliceQ 阶段 0.7-v2 — C 路线命门验证（OTIO 交付通道）

═══════════════════════════════════════════════════════════════════════
背景变更（2026-09-24）
═══════════════════════════════════════════════════════════════════════
v1 的目标是"验证字幕能否通过 OTIO 传递"。
但产品路线已定为 **C｜混合**：
    · 字幕 → 走 SRT 并行交付（已查证：达芬奇免费版原生支持 SRT 导入）
    · OTIO → 只负责传递"切点 + 高光标记"，不再承载字幕

因此 v1 的样本 C / D（字幕轨测试）**目的已消失**，予以停用。
（保留 v1 备份于 gen_otio_matrix_v1.bak.py，供追溯）

═══════════════════════════════════════════════════════════════════════
v2 要回答的真问题
═══════════════════════════════════════════════════════════════════════
  Q1 ★  OTIO 的 Marker 能否完整保留？—— 名称、颜色、位置是否丢失？
        （这决定"高光说明"能否随 OTIO 交付，是 C 路线的核心价值）

  Q2 ★  时间码起点陷阱能否规避？
        达芬奇默认时间线从 01:00:00:00 开始，SRT 是零基的。
        若在 OTIO 里显式设置 global_start_time = 0，导入后达芬奇
        的时间线起点是否也是 0？（这决定 SRT 与 OTIO 能否自动对齐）

  Q3    超长 Marker 名称是否被截断？
        （决定高光摘要写多少字，是 UI 设计的输入）

═══════════════════════════════════════════════════════════════════════
样本设计
═══════════════════════════════════════════════════════════════════════
  A  基准对照：纯视频轨 3 片段，无标记，start=0
  B  Marker 测试：3 片段 + 3 个青色标记（短名称）
  E  start_time 测试：3 片段 + 标记，但 global_start_time = 00:00:00:00
  F  长文本测试：3 片段 + 3 个标记，名称为 120 字中文长摘要
"""
import os
import opentimelineio as otio

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE0 = os.path.dirname(HERE)
MEDIA_DIR = os.path.join(STAGE0, "test_media")
OUT_DIR = os.path.join(STAGE0, "test_drafts", "otio_matrix_v2")
MEDIA = os.path.join(MEDIA_DIR, "source.mp4")

FPS = 30.0
SRC_TOTAL_FRAMES = 360

CLIPS = [
    (0.0, 3.0, "切片1_开场高光"),
    (3.0, 3.0, "切片2_关键操作"),
    (6.0, 3.0, "切片3_弹幕峰值"),
]

MARKERS_SHORT = [
    (0.5, "候选高光：开场包袱"),
    (3.5, "候选高光：极限操作"),
    (6.5, "候选高光：弹幕峰值"),
]

# 120 字长摘要 —— 测达芬奇是否截断 Marker 名称
LONG_TEXT = (
    "候选高光：这一段是模型综合弹幕密度、主播音量变化和画面运动幅度三项指标"
    "判定出的高光时刻，建议作为切片开头使用，因为此处观众的即时反馈最密集，"
    "平台推荐算法的完播率预测也最高，适合作为短视频的前三秒抓取注意力。"
)
MARKERS_LONG = [
    (0.5, LONG_TEXT),
    (3.5, LONG_TEXT),
    (6.5, LONG_TEXT),
]


def to_url(path):
    """Windows 路径 → OTIO 接受的 file:///C:/... 形式"""
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p          # C:/... → /C:/...
    return "file://" + p     # → file:///C:/...


def build_ref():
    ref = otio.schema.ExternalReference(target_url=to_url(MEDIA))
    ref.available_range = otio.opentime.TimeRange(
        start_time=otio.opentime.RationalTime(0, FPS),
        duration=otio.opentime.RationalTime(SRC_TOTAL_FRAMES, FPS),
    )
    return ref


def add_video_track(timeline, name="SliceQ_主轨"):
    track = otio.schema.Track(name=name)
    for start_s, dur_s, cname in CLIPS:
        clip = otio.schema.Clip(
            name=cname,
            media_reference=build_ref(),
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(int(start_s * FPS), FPS),
                duration=otio.opentime.RationalTime(int(dur_s * FPS), FPS),
            ),
        )
        track.append(clip)
    timeline.tracks.append(track)
    return track


def add_markers(track, markers):
    for at_s, text in markers:
        track.markers.append(otio.schema.Marker(
            name=text,
            marked_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(int(at_s * FPS), FPS),
                duration=otio.opentime.RationalTime(0, FPS),
            ),
            color=otio.schema.MarkerColor.CYAN,
        ))


def build_sample(label, start_seconds=0.0, markers=None):
    """
    start_seconds —— 时间线起始时间（相对 0）
                     Q2 的关键变量：设为 0，看达芬奇是否保留
    markers       —— None 表示不加标记
    """
    tl = otio.schema.Timeline(name=label)
    tl.global_start_time = otio.opentime.RationalTime(
        int(start_seconds * FPS), FPS
    )
    vt = add_video_track(tl)
    if markers:
        add_markers(vt, markers)
    return tl


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print("=" * 74)
    print("SliceQ 阶段 0.7-v2 — C 路线命门验证：生成对照样本")
    print("=" * 74)
    print(f"素材: {MEDIA}")
    print(f"输出: {OUT_DIR}")
    print("-" * 74)

    samples = [
        # 名称,              起始秒,  标记
        ("A_基准_无标记",     0.0,    None),
        ("B_短Marker",        0.0,    MARKERS_SHORT),
        ("E_起始时间码为0",   0.0,    MARKERS_SHORT),
        ("F_长Marker_120字",  0.0,    MARKERS_LONG),
    ]

    results = []
    for fname, start_s, markers in samples:
        tl = build_sample(fname, start_seconds=start_s, markers=markers)
        path = os.path.join(OUT_DIR, f"{fname}.otio")
        otio.adapters.write_to_file(tl, path)

        # ── 往返校验（只验 OTIO 自身读写无损，不代表达芬奇行为）
        rt = otio.adapters.read_from_file(path)
        n_tracks = len(rt.tracks)
        n_clips = sum(1 for t in rt.tracks for _ in t)
        n_markers = sum(len(t.markers) for t in rt.tracks)
        gst = rt.global_start_time
        gst_str = f"{gst.value:.0f}f@{gst.rate:.0f} ({gst.to_seconds():.2f}s)"
        marker_names = [m.name for t in rt.tracks for m in t.markers]
        longest = max((len(n) for n in marker_names), default=0)

        results.append((
            fname, os.path.getsize(path), n_tracks,
            n_clips, n_markers, gst_str, longest
        ))
        print(f"  [OK] {fname}.otio")
        print(f"       轨道={n_tracks} 片段={n_clips} 标记={n_markers} "
              f"起始={gst_str} 最长标记名={longest}字")

    print("-" * 74)
    print("汇总表")
    print("-" * 74)
    hdr = f"{'样本':<20}{'体积':>10}{'轨':>4}{'片段':>6}{'标记':>6}{'最长名':>8}"
    print(hdr)
    for r in results:
        print(f"{r[0]:<20}{r[1]:>10,}{r[2]:>4}{r[3]:>6}{r[4]:>6}{r[6]:>7}字")

    print()
    print("=" * 74)
    print("[操作指引] 请在达芬奇中依次导入这 4 个文件，逐个填写观察结果：")
    print("=" * 74)
    print("""
  样本 A —— 基准：应看到 3 个片段，无标记。若 A 都失败，先排查环境。

  样本 B —— ★核心：时间线上应出现 3 个青色标记点。
            重点看：①有没有标记 ②标记名显示什么 ③颜色是否青色
            ④标记位置是否在 0.5s / 3.5s / 6.5s 处

  样本 E —— ★核心：与 B 内容相同，但 OTIO 里显式写了起始时间=0。
            导入后立刻看【时间线起始时间码】是多少：
              · 若显示 00:00:00:00  → 时间码陷阱可用 OTIO 规避，SRT 能自动对齐
              · 若显示 01:00:00:00  → 达芬奇忽略该字段，必须在用户文档里提示手改

  样本 F —— 看长标记名（120 字）是否被截断，截断到多少字。
            这是 UI 设计输入：右上角高光摘要栏能写多少字。

  ⚠️ 导入方式：媒体池右键 → Import → Timeline → 选 .otio 文件
  ⚠️ 建议每个样本单独导入，便于区分。
""")
    print("=" * 74)


if __name__ == "__main__":
    main()
