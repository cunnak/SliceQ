# -*- coding: utf-8 -*-
"""
阶段 0 验证脚本 A：剪映草稿生成通道实测
目的：验证 pyJianYingDraft 能否为【剪映专业版 11.5.3】生成可打开的草稿（R1 风险项）
      ⚠️ 原写"10.9"是笔误。2026-09-27 用安装时间戳核实：
         剪映 Apps/11.5.3.14501 安装于 2026-09-23 18:32，
         而本脚本的验证是 2026-09-23~24 做的 ⇒ 当时验证的就是 11.5.3。
产出形态模拟 SliceQ 真实输出：多段切片 + 转场 + 字幕轨 + 文本标题轨
"""
import json
import os
import sys

import pyJianYingDraft as draft
from pyJianYingDraft import SEC, Timerange, trange

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "test_media", "source.mp4")
DRAFT_DIR = os.path.join(BASE, "test_drafts")
DRAFT_NAME = "SliceQ_Stage0_Test"
SRT = os.path.join(BASE, "test_media", "subtitle.srt")

os.makedirs(DRAFT_DIR, exist_ok=True)

# --- 1. 写一份字幕文件（模拟粗筛级的转录产物） ---
srt_text = """1
00:00:00,200 --> 00:00:02,000
这一段是模型找到的第一个候选高光

2
00:00:02,200 --> 00:00:04,000
主播在这里有一波关键操作

3
00:00:04,200 --> 00:00:06,000
弹幕在这几秒出现了明显峰值
"""
with open(SRT, "w", encoding="utf-8") as f:
    f.write(srt_text)

# --- 2. 创建草稿 ---
folder = draft.DraftFolder(DRAFT_DIR)
print(f"[1] 草稿目录: {DRAFT_DIR}")
print(f"    已有草稿: {folder.list_drafts()}")

script = folder.create_draft(
    DRAFT_NAME,
    width=1920,
    height=1080,
    fps=30,
    allow_replace=True,
)
print(f"[2] 草稿创建成功: {DRAFT_NAME}")

# --- 3. 视频主轨：3 段切片（模拟 SliceQ 勾选后的输出） ---
video_track = script.append_track(draft.TrackSpec(draft.TrackType.video, name="SliceQ_主轨"))
material = draft.VideoMaterial(SRC)

# 三段切片：从源视频 0-3s / 5-8s / 9-12s 切出，各 3 秒
cuts = [
    (0.0, 3.0),   # 源起, 时长
    (5.0, 3.0),
    (9.0, 3.0),
]
cursor = 0.0
segments = []
for i, (src_start, dur) in enumerate(cuts):
    seg = draft.VideoSegment(
        material,
        target_timerange=trange(f"{cursor}s", f"{dur}s"),
        source_timerange=trange(f"{src_start}s", f"{dur}s"),
    )
    script.add_segment(seg, video_track)
    segments.append(seg)
    print(f"    片段{i + 1}: 源 {src_start}s+{dur}s -> 时间线 {cursor}s")
    cursor += dur

# --- 4. 片段间转场（xfade 0.5s 对应物） ---
try:
    for seg in segments[:-1]:
        seg.add_transition(draft.TransitionType.叠化, duration=0.5 * SEC)
    print("[3] 转场已添加: 叠化 0.5s")
except Exception as e:
    print(f"[3] 转场添加失败（不影响主结论）: {type(e).__name__}: {e}")

# --- 5. 字幕轨：从 SRT 导入 ---
try:
    script.import_srt(SRT, track_name="SliceQ_字幕", time_offset=0.0)
    print("[4] 字幕轨导入成功")
except Exception as e:
    print(f"[4] 字幕导入失败: {type(e).__name__}: {e}")

# --- 6. 标题文案轨（模拟模型生成的标题） ---
try:
    title_track = script.append_track(draft.TrackSpec(draft.TrackType.text, name="SliceQ_标题"))
    title = draft.TextSegment(
        "SliceQ 阶段0 验证草稿",
        trange("0s", "3s"),
        clip_settings=draft.ClipSettings(transform_y=-0.75),
    )
    script.add_segment(title, title_track)
    print("[5] 标题轨添加成功")
except Exception as e:
    print(f"[5] 标题轨添加失败: {type(e).__name__}: {e}")

# --- 7. 保存 ---
script.save()
print(f"[6] 草稿已保存")

# --- 8. 检查产出文件与关键版本字段（决定剪映能否识别） ---
draft_path = os.path.join(DRAFT_DIR, DRAFT_NAME)
print(f"\n=== 产出文件清单 ===")
for root, _dirs, files in os.walk(draft_path):
    for fn in files:
        fp = os.path.join(root, fn)
        print(f"  {os.path.relpath(fp, draft_path):<40} {os.path.getsize(fp):>10,} bytes")

content_json = os.path.join(draft_path, "draft_content.json")
print(f"\n=== draft_content.json 关键字段 ===")
if os.path.exists(content_json):
    with open(content_json, "r", encoding="utf-8") as f:
        raw = f.read()
    print(f"  文件可读(明文 JSON): 是")
    print(f"  文件大小: {len(raw):,} 字符")
    data = json.loads(raw)
    for k in ["app_version", "new_version", "version", "platform", "last_modified_platform"]:
        if k in data:
            print(f"  {k}: {data[k]}")
    print(f"  轨道数: {len(data.get('tracks', []))}")
    for t in data.get("tracks", []):
        print(f"    - type={t.get('type')} segments={len(t.get('segments', []))}")
    print(f"  素材数: video={len(data.get('materials', {}).get('videos', []))} "
          f"text={len(data.get('materials', {}).get('texts', []))}")
else:
    print("  draft_content.json 不存在")
    print(f"  目录实际内容: {os.listdir(draft_path)}")

meta_json = os.path.join(draft_path, "draft_meta_info.json")
if os.path.exists(meta_json):
    with open(meta_json, "r", encoding="utf-8") as f:
        m = json.load(f)
    print(f"\n=== draft_meta_info.json ===")
    for k in ["draft_name", "draft_fold_path", "tm_draft_create", "draft_removable_storage_device"]:
        if k in m:
            print(f"  {k}: {str(m[k])[:100]}")
    if "draft_materials" in m:
        print(f"  draft_materials 段数: {len(m['draft_materials'])}")

print("\n=== 验证脚本 A 执行完毕 ===")
