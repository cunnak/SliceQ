# -*- coding: utf-8 -*-
"""
SliceQ 阶段 0.7 — 字幕/Marker 传递实验

目的：回答三个问题
  Q1  达芬奇导入 OTIO 后，Marker（高光标记）是否保留？
  Q2  达芬奇导入 OTIO 后，非视频轨（字幕/文本轨）是否保留？
  Q3  若 Q2 为否，退路是什么？

策略：一次产出【三代对照样本】，让用户一次性导入对比，效率最高。

  样本 A —— 纯视频轨（当前已验证可导入）
              作为对照组，确认基准能力

  样本 B —— 视频轨 + Marker
              测试 Q1：Marker 是否随导入保留

  样本 C —— 视频轨 + Marker + 标题轨(Track kind="Text")
              测试 Q2：非视频轨是否被保留

另附样本 D —— 视频轨 + 字幕作为 Clip 放在第二个 Track (kind="Video")
              备选方案：把字幕做成"画面片段"而非"文本轨"
"""
import os
import opentimelineio as otio

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE0 = os.path.dirname(HERE)
MEDIA_DIR = os.path.join(STAGE0, "test_media")
OUT_DIR = os.path.join(STAGE0, "test_drafts", "otio_matrix")
MEDIA = os.path.join(MEDIA_DIR, "source.mp4")

FPS = 30.0
SRC_TOTAL_FRAMES = 360
CLIPS = [
    (0.0, 3.0, "切片1"),
    (3.0, 3.0, "切片2"),
    (6.0, 3.0, "切片3"),
]
MARKERS = [
    (0.0, "这一段是模型找到的第一个候选高光"),
    (3.0, "主播在这里有一波关键操作"),
    (6.0, "弹幕在这几秒出现了明显峰值"),
]
# 从 subtitle.srt 解析出的真实字幕（时间码已转秒）
SUBTITLES = [
    (0.2, 2.0, "这一段是模型找到的第一个候选高光"),
    (2.2, 4.0, "主播在这里有一波关键操作"),
    (4.2, 6.0, "弹幕在这几秒出现了明显峰值"),
]


def to_url(path):
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + p


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


def add_markers(track):
    for at_s, text in MARKERS:
        track.markers.append(otio.schema.Marker(
            name=text,
            marked_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(int(at_s * FPS), FPS),
                duration=otio.opentime.RationalTime(0, FPS),
            ),
            color=otio.schema.MarkerColor.CYAN,
        ))


def build_sample(label, with_markers=False, subtitle_mode=None):
    """subtitle_mode: None | 'text_track' | 'video_track'"""
    tl = otio.schema.Timeline(name=label)
    tl.global_start_time = otio.opentime.RationalTime(0, FPS)
    vt = add_video_track(tl)

    if with_markers:
        add_markers(vt)

    if subtitle_mode == "text_track":
        # 方案 C：字幕作为独立轨（非 Video）。
        # ⚠️ 实测约束：OTIO 0.18.1 的 TrackKind 只有 Video / Audio 两种，
        #    没有 Text / Subtitle。因此用 Audio 轨来测"非视频轨能否被保留"
        #    这个更一般的问题。字幕文本承载在 Clip.name 上。
        st = otio.schema.Track(name="SliceQ_字幕轨", kind=otio.schema.TrackKind.Audio)
        for start_s, dur_s, text in SUBTITLES:
            c = otio.schema.Clip(
                name=text,
                source_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, FPS),
                    duration=otio.opentime.RationalTime(int(dur_s * FPS), FPS),
                ),
            )
            st.append(c)
        tl.tracks.append(st)

    elif subtitle_mode == "video_track":
        # 方案 D：字幕片段放在另一个 Video 轨（复用同一素材，靠名称承载字幕文本）
        st = otio.schema.Track(name="SliceQ_字幕轨")
        for start_s, dur_s, text in SUBTITLES:
            c = otio.schema.Clip(
                name=f"[字幕] {text}",
                media_reference=build_ref(),
                source_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(int(start_s * FPS), FPS),
                    duration=otio.opentime.RationalTime(int(dur_s * FPS), FPS),
                ),
            )
            st.append(c)
        tl.tracks.append(st)

    return tl


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print("=" * 72)
    print("SliceQ 阶段 0.7 — 字幕/Marker 传递实验：生成对照样本")
    print("=" * 72)
    print(f"素材: {MEDIA}")
    print(f"输出: {OUT_DIR}")
    print("-" * 72)

    samples = [
        ("A_纯视频轨",        dict(with_markers=False, subtitle_mode=None)),
        ("B_视频轨_加Marker", dict(with_markers=True,  subtitle_mode=None)),
        ("C_加Text字幕轨",    dict(with_markers=True,  subtitle_mode="text_track")),
        ("D_字幕放Video轨",   dict(with_markers=True,  subtitle_mode="video_track")),
    ]

    results = []
    for fname, kwargs in samples:
        tl = build_sample(fname, **kwargs)
        path = os.path.join(OUT_DIR, f"{fname}.otio")
        otio.adapters.write_to_file(tl, path)

        # 往返校验
        rt = otio.adapters.read_from_file(path)
        n_tracks = len(rt.tracks)
        n_clips = sum(1 for t in rt.tracks for _ in t)
        n_markers = sum(len(t.markers) for t in rt.tracks)
        kind_info = []
        for t in rt.tracks:
            k = getattr(t, "kind", "Video")
            kind_info.append(f"{t.name}({k})")

        results.append((fname, os.path.getsize(path), n_tracks, n_clips,
                        n_markers, " | ".join(kind_info)))
        print(f"  ✅ {fname}.otio")
        print(f"     轨道={n_tracks} 片段={n_clips} 标记={n_markers}")
        print(f"     轨道构成: {' | '.join(kind_info)}")

    print("-" * 72)
    print("汇总表")
    print("-" * 72)
    print(f"{'样本':<20}{'体积':>10}{'轨':>4}{'片段':>6}{'标记':>6}  轨道构成")
    for r in results:
        print(f"{r[0]:<20}{r[1]:>10,}{r[2]:>4}{r[3]:>6}{r[4]:>6}  {r[5]}")

    print()
    print("=" * 72)
    print("【操作指引】请在达芬奇中依次导入这 4 个文件，逐个填写观察结果：")
    print("=" * 72)
    print("""
  样本 A —— 基准：应该看到 3 个片段。这是对照组。
  样本 B —— 看时间线上有没有【青色标记点】。若有 → Marker 可传递。
  样本 C —— 视频轨 + 一条 Audio 轨（片段名是字幕文本）。
            看达芬奇是否保留了第 2 条轨道、轨道上的片段名是否还在。
            ⚠️ 注意：OTIO 0.18.1 无 Text 轨类型，故用 Audio 轨替代测试。
  样本 D —— 看是否出现 2 条视频轨（第二条片段名以 [字幕] 开头）。

  ⚠️ 导入方式：媒体池右键 → Import → Timeline → 选 .otio 文件
  ⚠️ 建议每个样本单独导入，便于区分。
""")
    print("=" * 72)


if __name__ == "__main__":
    main()
