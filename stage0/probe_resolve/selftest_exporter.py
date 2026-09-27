# -*- coding: utf-8 -*-
"""草稿导出（阶段 5）自测。

判据尽量由程序算出，不手写估算。守的是这几条：

    ① **字幕时间轴是重映射后的**（甲方案命门）——
       所有 cue 必须落在 [0, Σ片段时长] 内，而不是源时间（几百秒）
    ② **OTIO 结构**：global_start_time=0、available_range=素材真实总长、
       每个 Clip 的 source_range 对应高光、时间线总长 == Σ片段时长
    ③ **剪映草稿**：轨道数/字幕条数正确、meta 三个字段被补齐
    ④ **同名草稿不覆盖** —— 用户在剪映里精修过的版本不能被静默抹掉
    ⑤ **降级包**复用阶段 3 的导出（真跑 ffmpeg 切一段验证）

用法：
    python selftest_exporter.py            # 不含降级包（快）
    python selftest_exporter.py --slow     # 含降级包（真切片，慢一些）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    这里尤其重要 —— 剪映草稿目录被隔离到临时目录，
#    否则测试草稿会出现在用户剪映首页的「本地草稿」列表里。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from sliceq import asr, config, exporter as E                        # noqa: E402
from sliceq import subtitle                                            # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="sliceq_exp_"))
VIDEO = ROOT / "stage0" / "test_media" / "live_clip90.mp4"
SEG_CACHE = ROOT / "stage0" / "test_media" / "editor_selftest" / "segments.json"

PASS = FAIL = 0


def ck(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK  ] {name}" + (f"  — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  — {detail}" if detail else ""))
    return cond


def head(t: str) -> None:
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def load_segments() -> list:
    return [asr.Segment(**d)
            for d in json.loads(SEG_CACHE.read_text(encoding="utf-8"))]


# subtitle 模块没有反向解析（只写不读），测试里自己解一次
_SRT_T = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def srt_spans(path: Path) -> list[tuple[float, float]]:
    """读回 SRT，返回 [(起, 止)] 秒。"""
    out: list[tuple[float, float]] = []
    for m in _SRT_T.finditer(Path(path).read_text(encoding="utf-8")):
        n = [int(x) for x in m.groups()]
        out.append((n[0] * 3600 + n[1] * 60 + n[2] + n[3] / 1000,
                    n[4] * 3600 + n[5] * 60 + n[6] + n[7] / 1000))
    return out


# 两段高光（源视频时间）
HL = [E.Highlight(10.0, 25.0, "第一段"), E.Highlight(50.0, 65.0, "第二段")]
HL_TOTAL = sum(h.duration for h in HL)          # 30.0


# ─────────────────────────────────────────────────────────────
def t_media() -> None:
    head("[1] 素材探测")
    m = E.probe_media(VIDEO)
    ck("读到了尺寸", m.ok, f"{m.width}×{m.height}")
    ck("读到了时长", m.duration > 0, f"{m.duration:.3f}s")
    ck("读到了帧率", m.fps > 0, f"{m.fps:g}fps")
    # 尺寸与既有探测函数一致（说明没另写一套旋转处理）
    w2, h2 = __import__("sliceq.ffmpeg_tools", fromlist=["x"]).probe_video_size(VIDEO)
    ck("尺寸与 ffmpeg_tools.probe_video_size 一致", (m.width, m.height) == (w2, h2),
       f"{m.width}×{m.height} vs {w2}×{h2}")


def t_highlights() -> None:
    head("[2] 高光规划（含时长夹取）")
    # ⚠️ 注意这些用例都在素材时长（140s）**之内**，除了最后一条 ——
    #    第一版把"无标题"那段写成 200~210，它本身超界、被正确丢掉，
    #    于是"保留 3 段"这条断言失败。**是测试用例写错了，不是实现错了。**
    clips = [{"start": 10, "end": 20, "title_hint": "A"},
             {"start": 100, "end": 115, "title_hint": "B"},
             {"start": 20, "end": 30},                      # 无标题 → 自动补
             {"start": 30, "end": 30},                      # 零长度 → 丢
             {"start": 40, "end": 35},                      # 倒挂 → 丢
             {"start": "坏", "end": 5},                     # 非数字 → 丢
             {"start": 200, "end": 210, "title_hint": "超界"}]  # 超素材 → 丢
    hs = E.plan_highlights(clips, media_duration=140.0)
    ck("保留 3 段有效片段", len(hs) == 3, str([(h.start, h.end) for h in hs]))
    ck("★ 超出素材时长的片段被丢弃（否则剪映会抛 ValueError）",
       all(h.end <= 140.0 for h in hs), f"各段终点 {[h.end for h in hs]}")
    ck("无标题的片段自动补了默认名", len(hs) > 2 and hs[2].title == "片段 3",
       hs[2].title if len(hs) > 2 else "（段数不足）")
    ck("零长度 / 倒挂 / 非数字都被丢掉", len(hs) == 3)
    ck("空输入返回空", E.plan_highlights([], media_duration=10) == [])
    ck("未给素材时长时不做夹取",
       len(E.plan_highlights([{"start": 5000, "end": 5100}])) == 1)


def t_unique_name() -> None:
    head("[3] ★ 唯一命名（防覆盖用户已精修的草稿）")
    root = WORK / "names"
    root.mkdir(parents=True, exist_ok=True)
    a = E._unique_name(root, "我的草稿")
    (root / a).mkdir()
    b = E._unique_name(root, "我的草稿")
    (root / b).mkdir()
    c = E._unique_name(root, "我的草稿")
    ck("首次用原名", a == "我的草稿", a)
    ck("★ 第二次自动避让（不覆盖）", b == "我的草稿 (2)", b)
    ck("第三次继续避让", c == "我的草稿 (3)", c)
    ck("非法字符被清洗", "/" not in E._unique_name(root, "a/b:c*d?"),
       E._unique_name(root, "a/b:c*d?"))
    ck("空名有兜底", E._unique_name(root, "   ") != "", "")


def t_version() -> None:
    head("[4] 剪映版本探测（R11 缓解）")
    ver, verified = E.detect_jianying_version()
    ck("探测到版本号", bool(ver), ver)
    ck("当前版本在已验证列表内", verified,
       f"{ver} ∈ {config.JIANYING_VERIFIED_VERSIONS}")
    ready, why = E.jianying_ready()
    ck("剪映通道就绪", ready, why or "OK")


def t_subtitles() -> None:
    head("[5] ★ 字幕跨段重映射（甲方案命门）")
    segs = load_segments()
    info = E.build_timeline_subtitles(segs, HL, out_path=WORK / "tl.srt",
                                      use_llm=False)
    ck("产出了 SRT", bool(info["srt"]) and Path(info["srt"]).exists(),
       info["srt"])
    ck("字幕条数 > 0", info["cue_count"] > 0,
       f"{info['cue_count']} 条（源 {info['source_cues']} 条）")

    # ★ 关键判据：所有 cue 落在 [0, Σ片段时长] 内
    spans = srt_spans(Path(info["srt"]))
    ck("SRT 能被解出时间行", len(spans) == info["cue_count"],
       f"解出 {len(spans)} 行 / 声称 {info['cue_count']} 条")
    last = max(b for _a, b in spans)
    ck(f"★ 所有字幕落在时间线范围 0~{HL_TOTAL:.1f}s 内（不是源时间几百秒）",
       last <= HL_TOTAL + 0.5, f"最晚一条结束于 {last:.2f}s")
    ck("★ 没有字幕落在源时间轴上（>100s 就说明没重映射）",
       last < 100.0, f"最晚 {last:.2f}s")

    # 两段的字幕都要有（否则说明只有第一段被映射）
    in_first = sum(1 for _a, b in spans if b <= HL[0].duration + 0.5)
    in_second = sum(1 for a, _b in spans if a >= HL[0].duration - 0.5)
    ck("第一段内有字幕", in_first > 0, f"{in_first} 条")
    ck("第二段内有字幕（说明跨段累加正确）", in_second > 0, f"{in_second} 条")
    ck("★ 计数守恒（保留 + 丢弃 == 源条数）",
       info["cue_count"] + info["dropped"] == info["source_cues"],
       f"{info['cue_count']} + {info['dropped']} = "
       f"{info['cue_count'] + info['dropped']}（源 {info['source_cues']}）")


def t_otio() -> None:
    head("[6] 达芬奇 OTIO（甲方案结构）")
    import opentimelineio as otio

    out = WORK / "davinci"
    m = E.probe_media(VIDEO)
    cr = E.export_davinci(VIDEO, HL, out_dir=out, media=m,
                          timeline_srt=WORK / "tl.srt", name="结构测试")
    ck("导出成功", cr.ok, cr.error or cr.message)
    if not cr.ok:
        return

    tl_path = out / "结构测试.otio"
    ck("otio 文件存在", tl_path.exists(), f"{tl_path.stat().st_size} B")
    tl = otio.adapters.read_from_file(str(tl_path))

    ck("★ global_start_time == 0（否则达芬奇起点会变 01:00:00:00）",
       tl.global_start_time.value == 0, str(tl.global_start_time))
    total = otio.opentime.to_seconds(tl.duration())
    ck(f"★ 时间线总长 == Σ片段时长（{HL_TOTAL:.1f}s）",
       abs(total - HL_TOTAL) < 0.05, f"{total:.3f}s")

    track = tl.tracks[0]
    ck("片段数正确", len(track) == len(HL), str(len(track)))
    for i, (clip, h) in enumerate(zip(track, HL)):
        sr = clip.source_range
        a = otio.opentime.to_seconds(sr.start_time)
        d = otio.opentime.to_seconds(sr.duration)
        ck(f"片段{i+1} 的 source_range 对应高光 {h.start:.0f}~{h.end:.0f}s",
           abs(a - h.start) < 0.05 and abs(d - h.duration) < 0.05,
           f"实得 {a:.2f}+{d:.2f}")
        # ⚠️ available_range 必须是**素材真实总时长**
        ar = clip.media_reference.available_range
        avail = otio.opentime.to_seconds(ar.duration) if ar else 0
        ck(f"片段{i+1} 的 available_range == 素材真实总长",
           abs(avail - m.duration) < 0.1, f"{avail:.2f}s vs {m.duration:.2f}s")

    ck("clip 名字带上了标题（Marker 不保留，改走 Clip.name）",
       HL[0].title in track[0].name, track[0].name)
    ck("target_url 是 file:/// 形式",
       str(track[0].media_reference.target_url).startswith("file:///"),
       str(track[0].media_reference.target_url)[:60])
    ck("★ 导出了 导入说明.txt", (out / "导入说明.txt").exists())
    guide = (out / "导入说明.txt").read_text(encoding="utf-8")
    ck("说明里写了「找不到片段」是正常现象 + 要选素材文件夹",
       "找不到片段" in guide and str(VIDEO.parent) in guide, "")
    ck("说明里写了同名冲突提醒（R23）", "同名" in guide, "")
    ck("说明里写明了字幕样式不跟随导出", "字幕样式" in guide, "")
    ck("SRT 并行交付", (out / "结构测试.srt").exists(), "")

    # ── ★★ 音频轨（2026-09-27 真机验收发现的缺陷）────────────
    # 背景：只写视频轨时，达芬奇会建一条**空的**音频轨 → 时间线上有画面没声音。
    # 实测证据：项目库 Sm2TiTrack 有 2 行，但 Sm2TiItem 的片段全挂在视频轨上。
    ck("★★ 时间线有 2 条轨（视频 + 音频）—— 少了音频轨达芬奇里没声音",
       len(tl.tracks) == 2, f"实得 {len(tl.tracks)} 条")
    if len(tl.tracks) == 2:
        tv, ta = tl.tracks[0], tl.tracks[1]
        ck("轨 0 = Video，轨 1 = Audio",
           tv.kind == otio.schema.TrackKind.Video
           and ta.kind == otio.schema.TrackKind.Audio,
           f"{tv.kind!r} / {ta.kind!r}")
        ck("★ 音频轨片段数 == 视频轨片段数",
           len(ta) == len(tv), f"{len(ta)} vs {len(tv)}")
        # ⚠️ range_in_parent 是**方法**（读回来的对象上是类方法）——
        #    注意：导出侧那行 `clip.range_in_parent = TimeRange(...)` 是给实例赋属性，
        #    会 shadow 掉方法；好在它**不写进 .otio 文件**，位置由 Track 自己按
        #    source_range 时长累加算出。这里验的正是"Track 算出来的位置"。
        same_pos = all(
            abs(otio.opentime.to_seconds(a.range_in_parent().start_time)
                - otio.opentime.to_seconds(v.range_in_parent().start_time)) < 1e-6
            and abs(otio.opentime.to_seconds(a.source_range.start_time)
                    - otio.opentime.to_seconds(v.source_range.start_time)) < 1e-6
            and abs(otio.opentime.to_seconds(a.source_range.duration)
                    - otio.opentime.to_seconds(v.source_range.duration)) < 1e-6
            for v, a in zip(tv, ta))
        ck("★ 音频轨逐段的「时间线位置 + 源内起点 + 时长」与视频轨一致", same_pos, "")
        # 位置还应等于前面各段时长累加（验证 Track 的自动排布）
        ends = []
        acc = 0.0
        for v in tv:
            ends.append(abs(otio.opentime.to_seconds(v.range_in_parent().start_time) - acc) < 1e-6)
            acc += otio.opentime.to_seconds(v.source_range.duration)
        ck("片段在轨上首尾相接（位置 == 前面各段时长累加）", all(ends), "")
        ck("★ 音视频是**不同的 Clip 对象**（同一个对象会互相覆盖 range_in_parent）",
           all(v is not a for v, a in zip(tv, ta)), "")
        ck("音频轨引用同一个源文件",
           all(str(a.media_reference.target_url)
               == str(tv[0].media_reference.target_url) for a in ta), "")
        ck("★ 音频轨每段也带 available_range（== 素材真实总长）",
           all(a.media_reference.available_range is not None for a in ta), "")
        ck("两条轨都带了可识别名字",
           "SliceQ" in (tv.name or "") and "SliceQ" in (ta.name or ""),
           f"{tv.name!r} / {ta.name!r}")

    # ── 导入说明新增的两条（2026-09-27 真机踩到）────────────
    ck("★ 说明里警告「不要直接拖 SRT」（拖拽会丢首条偏移、字幕整体提前）",
       "不要直接把 .srt 拖到时间线" in guide, "")
    ck("★ 说明里给了「按时间码插入」的正确做法",
       "Using Timecode" in guide, "")
    ck("★ 说明里写了时间线起始时间码 / 项目帧率的关系（R20）",
       "时间线起始时间码" in guide and "帧率" in guide, "")
    ck("说明里给出了兜底做法（手动改起始时间码）",
       "00:00:00:00" in guide, "")


def t_jianying() -> None:
    head("[7] 剪映草稿")
    root = WORK / "jy_draft"
    root.mkdir(parents=True, exist_ok=True)
    m = E.probe_media(VIDEO)

    cr = E.export_jianying(VIDEO, HL, media=m, timeline_srt=WORK / "tl.srt",
                           draft_root=root, name="草稿测试",
                           work_dir=WORK / "jy_build")
    ck("导出成功", cr.ok, cr.error or cr.message)
    if not cr.ok:
        return
    ck("落到了指定的草稿根目录下", cr.out_dir.parent == root, str(cr.out_dir))

    content = json.loads((cr.out_dir / "draft_content.json")
                         .read_text(encoding="utf-8"))
    tracks = content.get("tracks", [])
    kinds = [t.get("type") for t in tracks]
    ck("有视频轨", "video" in kinds, str(kinds))
    ck("有字幕轨（text）", kinds.count("text") >= 1, str(kinds))
    ck("★ 字幕轨条数 > 0",
       any(len(t.get("segments", [])) > 0
           for t in tracks if t.get("type") == "text"), "")

    canvas = content.get("canvas_config", {})
    ck("画布尺寸 == 素材尺寸",
       (canvas.get("width"), canvas.get("height")) == (m.width, m.height),
       f"{canvas.get('width')}×{canvas.get('height')} vs {m.width}×{m.height}")

    # ★ 字幕轨的时间必须在**重映射后**的范围内
    cue_total = 0
    latest = 0.0
    for t in tracks:
        if t.get("type") != "text":
            continue
        segs = t.get("segments", [])
        if len(segs) <= len(HL):        # 标题轨只有几段，跳过
            continue
        cue_total += len(segs)
        for s in segs:
            tr = s.get("target_timerange", {})
            latest = max(latest, (tr.get("start", 0) + tr.get("duration", 0)) / 1e6)
    ck(f"★ 字幕轨时间落在 0~{HL_TOTAL:.0f}s（重映射后）",
       0 < latest <= HL_TOTAL + 0.5, f"最晚 {latest:.2f}s")

    # ★ meta 三个字段必须被补齐（实测坑）
    meta = json.loads((cr.out_dir / "draft_meta_info.json")
                      .read_text(encoding="utf-8"))
    ck("★ draft_name 已补齐", meta.get("draft_name") == "草稿测试",
       repr(meta.get("draft_name")))
    ck("★ draft_fold_path 已补齐", str(cr.out_dir) in str(meta.get("draft_fold_path")),
       str(meta.get("draft_fold_path")))
    ck("★ draft_root_path 已补齐", str(root) in str(meta.get("draft_root_path")),
       str(meta.get("draft_root_path")))


def t_no_overwrite() -> None:
    head("[8] ★ 同名草稿不覆盖（用户在剪映里精修过的东西不能丢）")
    root = WORK / "jy_overwrite"
    root.mkdir(parents=True, exist_ok=True)
    m = E.probe_media(VIDEO)

    r1 = E.export_jianying(VIDEO, HL, media=m, timeline_srt=WORK / "tl.srt",
                           draft_root=root, name="同名", work_dir=WORK / "b1")
    ck("第一次导出成功", r1.ok, r1.error or "")
    if not r1.ok:
        return
    # 往第一次的草稿里塞一个"用户精修痕迹"，验证它不会被抹掉
    marker = r1.out_dir / "用户精修痕迹.txt"
    marker.write_text("用户在剪映里改过的东西", encoding="utf-8")
    first_content = (r1.out_dir / "draft_content.json").read_bytes()

    r2 = E.export_jianying(VIDEO, HL, media=m, timeline_srt=WORK / "tl.srt",
                           draft_root=root, name="同名", work_dir=WORK / "b2")
    ck("第二次导出成功", r2.ok, r2.error or "")
    ck("★ 第二次换了个名字（没有覆盖）", r1.out_dir != r2.out_dir,
       f"{r1.out_dir.name} vs {r2.out_dir.name}")
    ck("★ 第一次的草稿还在", r1.out_dir.exists() and marker.exists())
    ck("★ 第一次的草稿内容未被改动",
       (r1.out_dir / "draft_content.json").read_bytes() == first_content)


def t_subtitle_empty() -> None:
    head("[9] 边界：没有转录分段")
    info = E.build_timeline_subtitles([], HL, out_path=WORK / "empty.srt")
    ck("不抛异常", isinstance(info, dict), str(info))
    ck("条数为 0", info["cue_count"] == 0, str(info))
    ck("没有产出文件", not Path(info["srt"]).exists() if info["srt"] else True)


def t_fallback() -> None:
    head("[10] 降级包（复用阶段 3 的导出）")
    if "--slow" not in sys.argv:
        print("  [跳过] 加 --slow 才跑（要真切 ffmpeg 片段）")
        return
    clips = [{"id": 1, "start": h.start, "end": h.end, "title_hint": h.title}
             for h in HL]
    cr = E.export_fallback(
        VIDEO, clips, out_dir=WORK / "fallback", task_name="降级测试",
        segments=load_segments(), use_llm_split=False, mode="exact",
        width=0, height=0)
    ck("导出成功", cr.ok, cr.error or cr.message)
    if not cr.ok:
        return
    mp4s = list(cr.out_dir.glob("*.mp4"))
    srt = list(cr.out_dir.glob("*.srt"))
    ck("每条候选一个 mp4", len(mp4s) == len(HL), f"{len(mp4s)} 个")
    ck("★ 有转录就产出 SRT（不烧字幕也给）", len(srt) > 0, f"{len(srt)} 个")
    for f in sorted(cr.out_dir.iterdir()):
        print(f"        {f.name:<34} {f.stat().st_size:>10,} B")


def t_end_to_end() -> None:
    head("[11] 端到端：export_drafts 主入口")
    clips = [{"id": 1, "start": h.start, "end": h.end, "title_hint": h.title}
             for h in HL]
    prog: list[str] = []
    r = E.export_drafts(VIDEO, clips, task_id=0, task_name="端到端测试",
                        out_base=WORK / "e2e", want_jianying=True,
                        want_davinci=True, segments=load_segments(),
                        use_llm_split=False,
                        draft_root=WORK / "e2e_jy",
                        progress=lambda d, t, m: prog.append(m))
    ck("整体成功", r.ok, r.error or str(r.ok_channels()))
    ck("两段高光", len(r.highlights) == 2, str(len(r.highlights)))
    ck("字幕条数 > 0", r.cue_count > 0, f"{r.cue_count} 条")
    ck("剪映通道成功", r.channels.get("jianying", E.ChannelResult()).ok,
       r.channels.get("jianying", E.ChannelResult()).error or "")
    ck("达芬奇通道成功", r.channels.get("davinci", E.ChannelResult()).ok,
       r.channels.get("davinci", E.ChannelResult()).error or "")
    ck("进度回调被调用过", len(prog) > 0, f"{len(prog)} 次")
    ck("★ 提示里有「字幕样式不会跟随导出」",
       any("字幕样式" in n for n in r.notes), str(r.notes)[:80])
    ck("★ 提示里说明了要手动完成最后一步",
       any("手动" in n for n in r.notes), "")
    print()
    print("  ── format_result 输出 ──")
    for ln in E.format_result(r).splitlines():
        print(f"    {ln}")


def t_bad_input() -> None:
    head("[12] 边界：坏输入要给出可读错误")
    r = E.export_drafts(VIDEO, [], task_name="空候选")
    ck("空候选 → 失败但消息可读", not r.ok and "没有可导出的片段" in r.error,
       r.error[:60])
    r2 = E.export_drafts(WORK / "不存在.mp4", [{"start": 1, "end": 2}],
                         task_name="坏素材")
    ck("素材不存在 → 不崩、消息可读", not r2.ok and bool(r2.error),
       r2.error.splitlines()[0][:60] if r2.error else "")


def main() -> int:
    print("=" * 74)
    print("草稿导出自测（阶段 5）")
    print("=" * 74)
    print(f"素材：{VIDEO.name}　高光：{[(h.start, h.end) for h in HL]}"
          f"（共 {HL_TOTAL:.0f}s）")

    if not VIDEO.exists():
        print(f"缺少素材 {VIDEO}")
        return 1
    if not SEG_CACHE.exists():
        print(f"缺少分段缓存 {SEG_CACHE}")
        return 1

    t_media()
    t_highlights()
    t_unique_name()
    t_version()
    t_subtitles()
    t_otio()
    t_jianying()
    t_no_overwrite()
    t_subtitle_empty()
    t_fallback()
    t_end_to_end()
    t_bad_input()

    print("\n" + "=" * 74)
    print(f"通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    print("=" * 74)
    shutil.rmtree(WORK, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
