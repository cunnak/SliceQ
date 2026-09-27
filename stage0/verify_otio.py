# -*- coding: utf-8 -*-
"""
阶段 0 验证脚本 C：三端草稿导出的第二条通道 —— OpenTimelineIO → FCP7 XML
目的：验证能否构建与剪映草稿等价的时间线，并导出 PR / 达芬奇可导入的 XML
"""
import os

import opentimelineio as otio

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "test_media", "source.mp4").replace("\\", "/")
OUT = os.path.join(BASE, "test_drafts")
os.makedirs(OUT, exist_ok=True)

print("=" * 70)
print("OTIO 版本:", otio.__version__)
print("=" * 70)

# --- 1. 构建时间线：与剪映草稿相同的 3 段切片 ---
rate = 30
tl = otio.schema.Timeline(name="SliceQ_Stage0_Test")
tl.global_start_time = otio.opentime.RationalTime(0, rate)
track = otio.schema.Track(name="SliceQ_主轨", kind=otio.schema.TrackKind.Video)
tl.tracks.append(track)

SRC_DUR = 12.0  # 源片总时长，用于给媒体引用标注可用区间

cuts = [(0.0, 3.0), (5.0, 3.0), (9.0, 3.0)]
for i, (src_start, dur) in enumerate(cuts):
    ref = otio.schema.ExternalReference(
        target_url="file:///" + SRC,
        available_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, rate),
            duration=otio.opentime.RationalTime(SRC_DUR * rate, rate),
        ),
    )
    clip = otio.schema.Clip(
        name=f"切片{i + 1}",
        media_reference=ref,
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(src_start * rate, rate),
            duration=otio.opentime.RationalTime(dur * rate, rate),
        ),
    )
    track.append(clip)
    print(f"  片段{i + 1}: 源 {src_start}s + {dur}s")

# --- 2. 字幕以「时间线标记」携带（FCP XML 无独立字幕轨，标记是通用做法） ---
subs = [
    ("这一段是模型找到的第一个候选高光", 0.2),
    ("主播在这里有一波关键操作", 2.2),
    ("弹幕在这几秒出现了明显峰值", 4.2),
]
for text, start in subs:
    track.markers.append(
        otio.schema.Marker(
            name=text,
            marked_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(start * rate, rate),
                duration=otio.opentime.RationalTime(1.8 * rate, rate),
            ),
            color=otio.schema.MarkerColor.CYAN,
        )
    )
print(f"  字幕标记: {len(subs)} 条（附 SRT 供导入）")

# --- 3. 校验 ---
print(f"\n  时间线总长: {tl.duration().to_seconds():.2f}s")
print(f"  轨道数: {len(tl.tracks)}")

# --- 4. 导出 FCP7 XML（PR / 达芬奇通用，需 otio-fcp-adapter） ---
fcp_path = os.path.join(OUT, "SliceQ_Stage0_Test.xml")
otio.adapters.write_to_file(tl, fcp_path, "fcp_xml")
print(f"\n[导出] FCP7 XML -> {fcp_path} ({os.path.getsize(fcp_path):,} bytes)")

# 附带 OTIO 原生格式（无损留档）
otio_path = os.path.join(OUT, "SliceQ_Stage0_Test.otio")
otio.adapters.write_to_file(tl, otio_path, "otio_json")
print(f"[导出] OTIO 原生 -> {otio_path} ({os.path.getsize(otio_path):,} bytes)")

# --- 5. 回读验证（往返一致性） ---
print("\n=== 往返读取验证 ===")
back = otio.adapters.read_from_file(fcp_path)
print(f"  回读时间线: {back.name}")
print(f"  回读轨道数: {len(back.tracks)}")
for t in back.tracks:
    print(f"    - {t.name} ({t.kind}) 片段数={len(t)}")
print(f"  回读总长: {back.duration().to_seconds():.2f}s")

# --- 6. 检查 XML 里媒体引用是否为绝对路径 ---
print("\n=== XML 媒体引用检查 ===")
xml = open(fcp_path, encoding="utf-8", errors="replace").read()
import re
paths = re.findall(r"<pathurl>([^<]+)</pathurl>", xml)
print(f"  pathurl 条目数: {len(paths)}")
for p in set(paths):
    print(f"    {p[:110]}")

print("\n=== 验证脚本 C 执行完毕 ===")
