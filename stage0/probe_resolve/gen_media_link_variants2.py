# -*- coding: utf-8 -*-
r"""
SliceQ 阶段 0.8-B — 媒体链接格式对照实验（洁净版）

═══════════════════════════════════════════════════════════════════
为什么要重做（v1 实验的设计缺陷）
═══════════════════════════════════════════════════════════════════
v1 实验的 5 个样本全部引用**同一个文件名** `source.mp4`（只是路径不同）。
结果在达芬奇项目库里发现：

    媒体对象 A (test_media\source.mp4)        被 21 个时间线项引用（7 条时间线）
    媒体对象 B (media_link_test\source.mp4)   被  6 个时间线项引用（2 条时间线）

预期应是 4 : 5（4 条 v2 样本 + 5 条 V 样本），实际是 7 : 2 —— 多出 3 条。
**强提示：达芬奇在路径解析失败时，会退回"按文件名在媒体池里找"，
于是把 V3/V4/V5 静默链到了池中更早的同名文件（test_media 那份）。**

⇒ **v1 实验无法区分"路径被解析"与"按文件名蒙对"，因为文件名全一样。**

═══════════════════════════════════════════════════════════════════
v2（洁净版）设计
═══════════════════════════════════════════════════════════════════
**每个变体用独一无二的媒体文件名**，彻底排除"按文件名蒙对"的可能：

    V1 → media_V1.mp4   （file:///C:/... 三斜杠 URL）
    V2 → media_V2.mp4   （file://localhost/C:/...）
    V3 → media_V3.mp4   （纯路径 正斜杠）
    V4 → media_V4.mp4   （纯路径 反斜杠）
    V5 → media_V5.mp4   （相对路径，同目录）

判定逻辑变得清晰：

    时间线上**看得到画面**      → 该写法被解析成功 ✅
    弹「找不到片段」/画面离线   → 该写法解析失败 ❌
      （因为文件名唯一，"蒙对"已不可能）

**这才是能给出确定结论的实验。**
"""
import os
import shutil
import opentimelineio as otio

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE0 = os.path.dirname(HERE)
SRC_MEDIA = os.path.join(STAGE0, "test_media", "source.mp4")
OUT_DIR = os.path.join(STAGE0, "test_drafts", "media_link_test2")

FPS = 30.0
TOTAL_FRAMES = 360
CLIPS = [(0.0, 3.0, "切片1"), (3.0, 3.0, "切片2"), (6.0, 3.0, "切片3")]


def build_variants(media_abs):
    """(代号, 说明, target_url)  —— 每个变体引用自己独有的媒体文件"""
    name = os.path.basename(media_abs)
    fwd = media_abs.replace("\\", "/")
    return [
        ("V1_file3slash",    "file:///C:/...（三斜杠 URL，当前实现用法）", "file:///" + fwd.lstrip("/")),
        ("V2_fileLocalhost", "file://localhost/C:/...",                  "file://localhost/" + fwd.lstrip("/")),
        ("V3_plainFwd",      "纯路径 · 正斜杠",                            fwd),
        ("V4_plainBack",     "纯路径 · 反斜杠（Windows 原生）",             media_abs),
        ("V5_relative",      "相对路径（媒体与 otio 同目录）",              name),
    ]


def build_timeline(url, label):
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
    print("=" * 76)
    print("SliceQ 阶段 0.8-B — 媒体链接格式对照实验（洁净版）")
    print("=" * 76)
    print(f"输出目录: {OUT_DIR}")
    print("-" * 76)

    rows = []
    for code, desc, _ in build_variants(
        os.path.join(OUT_DIR, "media_V1.mp4")   # 仅用于取变体列表
    ):
        idx = code[1]                            # '1'..'5'
        media_name = f"media_V{idx}.mp4"
        dst = os.path.join(OUT_DIR, media_name)
        if not os.path.exists(dst):
            shutil.copy2(SRC_MEDIA, dst)
        # 用该变体自己的媒体文件重新计算 URL
        _, _, url = [v for v in build_variants(dst) if v[0] == code][0]
        tl = build_timeline(url, code)
        path = os.path.join(OUT_DIR, f"{code}.otio")
        otio.adapters.write_to_file(tl, path)
        rows.append((code, desc, media_name, url, os.path.getsize(path)))
        print(f"  [OK] {code}.otio")
        print(f"       媒体: {media_name}")
        print(f"       target_url: {url}")

    readme = os.path.join(OUT_DIR, "测试说明.md")
    with open(readme, "w", encoding="utf-8") as f:
        f.write("# SliceQ 阶段 0.8-B — 媒体链接格式对照实验（洁净版）\n\n")
        f.write("## 为什么重做\n\n")
        f.write("v1 实验的 5 个样本引用了**同一个文件名** `source.mp4`，\n")
        f.write("导致无法区分「路径被解析成功」与「按文件名蒙对」。\n\n")
        f.write("本次每个样本引用**独一无二**的媒体文件名，判定逻辑变干净。\n\n")
        f.write("## 样本\n\n| 文件 | 媒体文件 | target_url 写法 |\n|---|---|---|\n")
        for code, desc, media, url, _ in rows:
            f.write(f"| `{code}.otio` | `{media}` | {desc} |\n")
        f.write("\n## 操作\n\n")
        f.write("⚠️ **请新建一个全新项目**再开始，避免旧项目里的媒体池干扰。\n\n")
        f.write("依次导入每个 `.otio`，对每一个记录：\n\n")
        f.write("1. 有没有弹「找不到片段」？\n")
        f.write("2. 时间线上**看得到画面**吗（还是红色离线条）？\n\n")
        f.write("## 回填\n\n```\n")
        for code, _, media, _, _ in rows:
            f.write(f"{code} ({media})  弹窗:(是/否)  画面可见:(是/否)\n")
        f.write("```\n")

    print("-" * 76)
    print(f"[说明卡] {readme}")
    print()
    print("=" * 76)
    print("[操作指引]")
    print("=" * 76)
    print("""
  ⚠️ 请【新建一个全新项目】再开始，避免旧项目媒体池干扰。

  依次导入 V1~V5，每个记录两件事：
    ① 有没有弹「找不到片段」？
    ② 时间线上看得到画面吗（还是红色离线条）？

  这次每个样本引用【独一无二】的媒体文件名，
  所以「画面可见」= 路径被解析成功，不可能是蒙对。
""")
    print("=" * 76)


if __name__ == "__main__":
    main()
