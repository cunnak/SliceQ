# -*- coding: utf-8 -*-
"""
SliceQ 阶段 0.8 — 媒体链接格式对照实验

═══════════════════════════════════════════════════════════════════
问题
═══════════════════════════════════════════════════════════════════
达芬奇导入 .otio 后弹出「找不到片段」，时间线建好了但媒体没链接。
素材文件确实存在于 OTIO 所写路径，但达芬奇不认。

官方文档（Resolve 18.5 New Features Guide）说：
  .otio  → 不含媒体，需用户自行重链接
  .otioz → 含媒体，自动链接

但「素材在原位却不链接」这件事官方没解释。
怀疑是 target_url 的**写法**问题。

═══════════════════════════════════════════════════════════════════
实验设计
═══════════════════════════════════════════════════════════════════
控制变量：所有样本引用**同一个** source.mp4（本目录下的副本），
          只改 target_url 的字符串写法。

  V1  file:///C:/...          三斜杠 URL（我们当前用的）
  V2  file://localhost/C:/... localhost 形式 URL
  V3  C:/...                  纯路径 · 正斜杠
  V4  C:\\...                 纯路径 · 反斜杠（Windows 原生）
  V5  source.mp4              相对路径（素材与 otio 同目录）

唯一能自动链接的那一个，就是达芬奇真正接受的写法。
"""
import os
import shutil
import opentimelineio as otio

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE0 = os.path.dirname(HERE)
SRC_MEDIA = os.path.join(STAGE0, "test_media", "source.mp4")
OUT_DIR = os.path.join(STAGE0, "test_drafts", "media_link_test")
MEDIA_NAME = "source.mp4"

FPS = 30.0
TOTAL_FRAMES = 360
CLIPS = [(0.0, 3.0, "切片1"), (3.0, 3.0, "切片2"), (6.0, 3.0, "切片3")]


def variants(media_abs):
    """返回 [(代号, 说明, target_url)]"""
    fwd = media_abs.replace("\\", "/")          # C:/Users/...
    return [
        ("V1_file3slash",    "file:///C:/...（三斜杠 URL，当前用法）", "file:///" + fwd.lstrip("/")),
        ("V2_fileLocalhost", "file://localhost/C:/...",                "file://localhost/" + fwd.lstrip("/")),
        ("V3_plainFwd",      "纯路径 · 正斜杠",                          fwd),
        ("V4_plainBack",     "纯路径 · 反斜杠（Windows 原生）",           media_abs),
        ("V5_relative",      "相对路径（素材与 otio 同目录）",            MEDIA_NAME),
    ]


def build(url, label):
    tl = otio.schema.Timeline(name=label)
    tl.global_start_time = otio.opentime.RationalTime(0, FPS)
    track = otio.schema.Track(name="SliceQ_主轨")
    for start_s, dur_s, cname in CLIPS:
        ref = otio.schema.ExternalReference(target_url=url)
        ref.available_range = otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, FPS),
            duration=otio.opentime.RationalTime(TOTAL_FRAMES, FPS),
        )
        clip = otio.schema.Clip(
            name=cname,
            media_reference=ref,
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(int(start_s * FPS), FPS),
                duration=otio.opentime.RationalTime(int(dur_s * FPS), FPS),
            ),
        )
        track.append(clip)
    tl.tracks.append(track)
    return tl


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # 1. 把素材复制到测试目录（V5 相对路径需要；其他变体也统一指向这里）
    dst_media = os.path.join(OUT_DIR, MEDIA_NAME)
    if not os.path.exists(dst_media):
        shutil.copy2(SRC_MEDIA, dst_media)
        print(f"[复制素材] {SRC_MEDIA} -> {dst_media}")
    else:
        print(f"[素材已存在] {dst_media}")

    print("=" * 74)
    print("SliceQ 阶段 0.8 — 媒体链接格式对照实验：生成样本")
    print("=" * 74)
    print(f"输出目录: {OUT_DIR}")
    print(f"引用素材: {dst_media}")
    print("-" * 74)

    rows = []
    for code, desc, url in variants(dst_media):
        tl = build(url, code)
        path = os.path.join(OUT_DIR, f"{code}.otio")
        otio.adapters.write_to_file(tl, path)
        rows.append((code, desc, url, os.path.getsize(path)))
        print(f"  [OK] {code}.otio  ({os.path.getsize(path):,} B)")
        print(f"       target_url = {url}")

    # 附一张说明卡
    readme = os.path.join(OUT_DIR, "测试说明.md")
    with open(readme, "w", encoding="utf-8") as f:
        f.write("# SliceQ 阶段 0.8 — 媒体链接格式对照实验\n\n")
        f.write("## 目的\n\n同一份素材（本目录 `source.mp4`），只改 OTIO 里 `target_url` 的写法，\n")
        f.write("看达芬奇能自动链接哪一种。\n\n")
        f.write("## 样本\n\n| 文件 | target_url 写法 | 说明 |\n|---|---|---|\n")
        for code, desc, url, _ in rows:
            f.write(f"| `{code}.otio` | `{url}` | {desc} |\n")
        f.write("\n## 操作\n\n依次导入每个 .otio，观察**是否弹出「找不到片段」**：\n\n")
        f.write("- **不弹窗** → 这种写法达芬奇能自动认 → **就是我们要的**\n")
        f.write("- **弹窗** → 这种写法不行 → 点「取消」跳过\n\n")
        f.write("> 建议每个样本在**全新项目**里导入，避免互相干扰。\n\n")
        f.write("## 回填\n\n```\n")
        for code, _, _, _ in rows:
            f.write(f"{code}: 自动链接？(是/否/弹窗)\n")
        f.write("```\n")

    print("-" * 74)
    print(f"[说明卡] {readme}")
    print()
    print("=" * 74)
    print("[操作指引]")
    print("=" * 74)
    print("""
  依次导入 V1~V5 五个 .otio，**看哪个不弹出「找不到片段」**。

    · 不弹窗  → 该写法达芬奇能自动认 → 这就是 SliceQ 要采用的格式
    · 弹窗    → 该写法不行，点「取消」跳过，继续下一个

  ⚠️ 每个样本建议在【全新项目】里导入，避免上一轮残留媒体干扰判断。
  ⚠️ 五个样本引用的是同一份素材，唯一差别只有 URL 写法。
""")
    print("=" * 74)


if __name__ == "__main__":
    main()
