# -*- coding: utf-8 -*-
"""
SliceQ — OTIO 导出（达芬奇通道首选格式）

阶段 0.6 实测结论（2026-09-24）：
  ✅ OTIO 直出 → 达芬奇 21.0.0.48 导入成功
  ❌ FCP7 XML  → 达芬奇导入失败（日志无读取记录）

关键要点：
  1. 用 opentimelineio 主包内置的 otio_json 适配器，**不需要 otio-fcp-adapter**
  2. ExternalReference 必须设置 available_range（OTIO 数据模型完整性要求）
  3. target_url 用绝对路径：file:///C:/... （三斜杠 + 正斜杠），实测被达芬奇接受
  4. Marker 建议用 OTIO 原生 Marker 对象（达芬奇是否保留待验证 —— 见 R17）

用法：
    python export_otio_demo.py
"""
import os
import opentimelineio as otio

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(os.path.dirname(HERE), "test_media", "source.mp4")
OUT = os.path.join(os.path.dirname(HERE), "test_drafts", "SliceQ_Stage0_Test.from_script.otio")

FPS = 30.0
SRC_TOTAL_FRAMES = 360       # source.mp4 = 12.0s @30fps
CLIPS = [                    # (起点秒, 时长秒, 名称)
    (0.0, 3.0, "切片1"),
    (3.0, 3.0, "切片2"),
    (6.0, 3.0, "切片3"),
]
MARKERS = [                  # (时间线时间点秒, 名称)
    (6.0, "这一段是模型找到的第一个候选高光"),
    (0.0, "主播在这里有一波关键操作"),
]


def to_url(path):
    """Windows 绝对路径 → file:/// URL（实测被达芬奇接受的写法）"""
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p          # C:/... → /C:/...
    return "file://" + p     # → file:///C:/...


def build_reference():
    """媒体引用 —— available_range 必须设置，否则 OTIO 数据模型不完整"""
    ref = otio.schema.ExternalReference(target_url=to_url(MEDIA))
    ref.available_range = otio.opentime.TimeRange(
        start_time=otio.opentime.RationalTime(0, FPS),
        duration=otio.opentime.RationalTime(SRC_TOTAL_FRAMES, FPS),
    )
    return ref


def main():
    print("=" * 70)
    print("SliceQ — OTIO 导出（达芬奇通道）")
    print("=" * 70)

    if not os.path.isfile(MEDIA):
        print("  ERROR: 素材不存在:", MEDIA)
        return
    print("  素材:", MEDIA)
    print("  URL :", to_url(MEDIA))

    timeline = otio.schema.Timeline(name="SliceQ_Stage0_Test")
    timeline.global_start_time = otio.opentime.RationalTime(0, FPS)

    track = otio.schema.Track(name="SliceQ_主轨")
    timeline.tracks.append(track)

    for start_s, dur_s, name in CLIPS:
        start_f = int(round(start_s * FPS))
        dur_f = int(round(dur_s * FPS))
        clip = otio.schema.Clip(
            name=name,
            media_reference=build_reference(),
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(start_f, FPS),
                duration=otio.opentime.RationalTime(dur_f, FPS),
            ),
        )
        track.append(clip)
        print(f"  + 片段 {name}: 源起帧 {start_f}, 时长 {dur_f} 帧 ({dur_s}s)")

    # 高光标记（待验证达芬奇是否保留 —— R17）
    for at_s, text in MARKERS:
        at_f = int(round(at_s * FPS))
        track.markers.append(otio.schema.Marker(
            name=text,
            marked_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(at_f, FPS),
                duration=otio.opentime.RationalTime(0, FPS),
            ),
            color=otio.schema.MarkerColor.CYAN,
        ))
        print(f"  + 标记 @{at_s}s: {text}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    otio.adapters.write_to_file(timeline, OUT)

    # ---- 往返读取校验 ----
    rt = otio.adapters.read_from_file(OUT)
    n_clips = sum(len(t) for t in rt.tracks if isinstance(t, otio.schema.Track))
    dur = rt.duration()
    print("-" * 70)
    print(f"  写出: {OUT}")
    print(f"  体积: {os.path.getsize(OUT):,} bytes")
    print(f"  往返校验: 片段数 = {n_clips}, 总时长 = {dur.to_seconds()}s")
    expected_s = sum(c[1] for c in CLIPS)
    ok = (n_clips == len(CLIPS) and abs(dur.to_seconds() - expected_s) < 0.001)
    print(f"  校验结果: {'✅ 通过' if ok else '❌ 不一致'}"
          f"（期望 {len(CLIPS)} 片段 / {expected_s}s）")
    print("=" * 70)
    print()
    print("下一步：达芬奇 → 媒体池右键 → Import → Timeline → 选该 .otio 文件")


if __name__ == "__main__":
    main()
