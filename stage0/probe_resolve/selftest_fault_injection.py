# -*- coding: utf-8 -*-
"""异常注入测试 —— 故意制造各种故障，看程序怎么反应。

## 为什么要做这件事

阶段 3 的 70 项自测覆盖的全是**正常流程**。而实际抓到的三个缺陷
（SystemExit 穿透 / worker 引用丢失 / cwd 改变路径基准）
没有一个是正常流程测出来的 —— 都是环境撞出来的。

正常流程全过 ≠ 异常时不会死。这个脚本补的就是这一块。

## 判据（不合格的四种表现）

    ① 静默：返回"成功"但产物不存在/为空，且没有任何说明
    ② 崩溃：段错误 或 未捕获的裸异常穿透
    ③ 卡死：永远等待（超时）
    ④ 误导：消息是裸的 Python 异常（FileNotFoundError 之类），
            用户看不懂，也不知道该干什么

合格 = 抛**自定义错误**且消息**是中文、说要紧的信息**。

用法：
    python selftest_fault_injection.py
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from sliceq import (analyzer, asr, config, editor, ffmpeg_tools,  # noqa: E402
                     pipeline, store, subtitle)
from sliceq import subtitle_style                                    # noqa: E402

PROJ = ROOT
MEDIA = PROJ / "stage0" / "test_media" / "live_clip90.mp4"
SEG_CACHE = PROJ / "stage0" / "test_media" / "editor_selftest" / "segments.json"

WORK = PROJ / "stage0" / "test_media" / "fault_injection"

# 自定义错误类型 —— 抛出这些才说明"翻译过"
KNOWN_ERRORS = (
    editor.EditorError, subtitle_style.PreviewError if hasattr(
        subtitle_style, "PreviewError") else editor.EditorError,
)
try:
    from sliceq.analyzer import AnalyzerError
    KNOWN_ERRORS = KNOWN_ERRORS + (AnalyzerError,)
except Exception:
    pass
try:
    from sliceq.asr import AsrError
    KNOWN_ERRORS = KNOWN_ERRORS + (AsrError,)
except Exception:
    pass

ROWS: list[dict] = []


def _err_text(exc: BaseException) -> str:
    return (str(exc) or type(exc).__name__).strip()


def _is_readable(exc: BaseException) -> tuple[bool, str]:
    """这个异常对用户可读吗？"""
    if isinstance(exc, KNOWN_ERRORS):
        return True, "自定义错误类"
    msg = _err_text(exc)
    # 裸 Python 异常 + 英文消息 = 不可读
    looks_cn = any("\u4e00" <= ch <= "\u9fff" for ch in msg)
    if looks_cn:
        return True, "有中文说明"
    return False, f"裸 {type(exc).__name__}，无中文说明"


def scenario(name: str, fn, *, expect_hint: str = "",
             expect_in: tuple = (), expect_not_in: tuple = ()) -> None:
    """跑一个故障场景并记录判定。

    `expect_in` / `expect_not_in` 断言**错误信息的内容**（v1.18 新增）。

    ⚠️ 为什么需要它：仅"抛了自定义错误 + 是中文"远不够。
       实测（2026-09-27）粗筛全部窗口失败时，程序给出的信息是

           "没有找到符合条件的候选段。可以试试：① 把需求描述写得更具体；
            ② 调高「筛选强度」档位；③ 换一段素材验证。"

       —— 这段话**读起来完全正常、语气也友好**，但它把病因
       （缺少 dashscope 包）替换成了一个不存在的问题，
       用户会照着去改需求描述、换素材，白折腾一圈。

       所以判据要加一条：**该说的说到了，不该说的没说**。
    """
    t0 = time.time()
    outcome = "returned"
    verdict = "?"
    note = ""
    try:
        ret = fn()
        outcome = "returned"
        # 没抛异常 —— 可能是合法降级，也可能是静默失败
        if isinstance(ret, dict):
            ok = ret.get("ok", True)
            note = ret.get("note", "")
            info = ret.get("info", "")
            if ok:
                verdict = "OK-降级"
                note = note or "无异常返回（可能是设计上的容错）"
            else:
                verdict = "静默"
                note = note or "返回失败但没有可读提示"
            if info:
                note = f"{note}｜{info}"
        else:
            verdict = "OK-降级"
            note = f"返回 {type(ret).__name__}"
    except BaseException as exc:                    # noqa: BLE001
        outcome = "raised"
        readable, why = _is_readable(exc)
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            verdict = "崩溃"
            note = f"{type(exc).__name__} 穿透到调用方！{why}"
        elif readable:
            full = _err_text(exc)
            missing = [k for k in expect_in if k not in full]
            misleading = [k for k in expect_not_in if k in full]
            if missing or misleading:
                verdict = "信息不全"
                bits = []
                if missing:
                    bits.append(f"缺少关键信息 {missing}")
                if misleading:
                    bits.append(f"含误导内容 {misleading}")
                note = ("；".join(bits)
                        + f"｜实际：{full.splitlines()[0][:80]}")
            else:
                verdict = "OK-可读错误"
                note = f"[{type(exc).__name__}] {full.splitlines()[0][:96]}"
        else:
            verdict = "误导"
            note = f"[{type(exc).__name__}] {_err_text(exc)[:96]}"

    dt = time.time() - t0
    ROWS.append({"name": name, "verdict": verdict, "note": note,
                 "elapsed": dt, "expect": expect_hint})
    icon = {"OK-可读错误": "✅", "OK-降级": "✅", "静默": "⚠️ ",
            "误导": "⚠️ ", "信息不全": "⚠️ ", "崩溃": "🔴",
            "?": "❓"}.get(verdict, "❓")
    print(f"  {icon} {name}")
    print(f"      {verdict}｜{note}｜{dt:.2f}s")


def head(t: str) -> None:
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


# 空工作目录（每个场景一个，避免互相污染）
def fresh(tag: str) -> Path:
    p = WORK / tag
    p.mkdir(parents=True, exist_ok=True)
    return p


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    store.init_db()

    if not MEDIA.exists():
        print(f"缺少测试素材：{MEDIA}")
        return 2
    segs = []
    if SEG_CACHE.exists():
        segs = [asr.Segment(**d) for d in
                json.loads(SEG_CACHE.read_text(encoding="utf-8"))]

    clips_ok = [{"id": 1, "start": 2.0, "end": 12.0, "title_hint": "正常片段"}]

    # ══════════════════════════════════════════════════════
    head("A · 输入异常")

    scenario("A1 源文件不存在", lambda: editor.export_clips(
        WORK / "不存在.mp4", clips_ok, out_base=fresh("a1")))

    # 伪装成 mp4 的文本文件
    bogus = WORK / "bogus.mp4"
    bogus.write_text("这不是视频文件，只是一段普通文本。" * 20, encoding="utf-8")
    scenario("A2 源文件是伪装成 mp4 的文本", lambda: editor.export_clips(
        bogus, clips_ok, out_base=fresh("a2")))

    scenario("A3 端点为负（end < start）", lambda: editor.export_clips(
        MEDIA, [{"id": 1, "start": 30.0, "end": 10.0, "title_hint": "倒序"}],
        out_base=fresh("a3")))

    scenario("A4 起点为负数", lambda: editor.export_clips(
        MEDIA, [{"id": 1, "start": -50.0, "end": -40.0, "title_hint": "负数"}],
        out_base=fresh("a4")))

    scenario("A5 时间范围远超视频长度", lambda: editor.export_clips(
        MEDIA, [{"id": 1, "start": 999_000.0, "end": 999_050.0,
                 "title_hint": "越界"}], out_base=fresh("a5")))

    scenario("A6 clip 缺 start/end 字段", lambda: editor.export_clips(
        MEDIA, [{"id": 1, "title_hint": "没时间"}], out_base=fresh("a6")))

    scenario("A7 候选列表为空", lambda: editor.export_clips(
        MEDIA, [], out_base=fresh("a7")))

    scenario("A8 输出根目录是个已存在的文件", lambda: _out_is_file())

    # ══════════════════════════════════════════════════════
    head("B · 字幕相关异常")

    scenario("B1 没有任何转录分段（做不出字幕）", lambda: editor.export_clips(
        MEDIA, clips_ok, out_base=fresh("b1"), burn_subtitles=True,
        segments=[], width=406, height=720))

    scenario("B2 分段时间为 0 长度", lambda: editor.export_clips(
        MEDIA, clips_ok, out_base=fresh("b2"), burn_subtitles=True,
        segments=[asr.Segment(start_ms=5000, end_ms=5000, text="零长"),
                  asr.Segment(start_ms=6000, end_ms=8000, text="正常一句")],
        width=406, height=720))

    scenario("B3 分段时间倒挂（end < start）", lambda: editor.export_clips(
        MEDIA, clips_ok, out_base=fresh("b3"), burn_subtitles=True,
        segments=[asr.Segment(start_ms=9000, end_ms=4000, text="倒挂")],
        width=406, height=720))

    scenario("B4 字幕文本极长（单条 200 字）", lambda: editor.export_clips(
        MEDIA, clips_ok, out_base=fresh("b4"), burn_subtitles=True,
        segments=[asr.Segment(start_ms=2000, end_ms=11000, text="很长的一句话" * 25)],
        width=406, height=720))

    scenario("B5 文本含 ASS 特殊字符 { }", lambda: editor.export_clips(
        MEDIA, clips_ok, out_base=fresh("b5"), burn_subtitles=True,
        segments=[asr.Segment(start_ms=2000, end_ms=6000,
                              text="{\\an8}危险文本{乱七八}")],
        width=406, height=720))

    scenario("B6 指定不存在的字体", lambda: _with_bad_font())

    # ══════════════════════════════════════════════════════
    head("C · 外部依赖异常")

    scenario("C1 ffmpeg 路径被设为无效值", lambda: _bad_ffmpeg_path())

    scenario("C2 样式预设文件损坏（坏 JSON）", lambda: _broken_preset())

    scenario("C3 样式预设缺字段 / 类型错", lambda: _bad_preset_types())

    # ══════════════════════════════════════════════════════
    head("D · 取消与并发")

    scenario("D1 一开始就要求取消", lambda: editor.export_clips(
        MEDIA, clips_ok, out_base=fresh("d1"),
        should_cancel=lambda: True))

    def _cancel_mid():
        n = {"i": 0}

        def cancel():
            n["i"] += 1
            return n["i"] > 3          # 前几次进度回调后取消
        return editor.export_clips(
            MEDIA, clips_ok * 3, out_base=fresh("d2"), should_cancel=cancel)

    scenario("D2 导出中途取消", _cancel_mid)

    # ══════════════════════════════════════════════════════
    head("E · 纯函数层的边界（不启动 ffmpeg，快）")

    scenario("E1 重映射：片段完全落在视频外", lambda: _remap_outside())
    scenario("E2 换行：空字符串", lambda: _wrap_empty())
    scenario("E3 换行：单字符", lambda: _wrap_single())
    scenario("E4 换行：极小画布（80px）", lambda: _wrap_tiny())
    scenario("E5 换行：字号大于画布", lambda: _wrap_huge_font())
    scenario("E6 预设：文件名含路径穿越", lambda: _preset_traversal())
    scenario("E7 预设：空名字", lambda: _preset_empty_name())

    # ══════════════════════════════════════════════════════
    head("F 组：错误信息不能把病因替换掉")
    # ══════════════════════════════════════════════════════
    #
    # ⚠️ 这一组测的不是"会不会崩"，而是**说的是不是真话**。
    #    一段读起来通顺、语气友好的错误提示，也可能是错的 ——
    #    用户会照着它去改无关的东西，而且**永远不会察觉自己被骗了**。
    scenario(
        "F1 粗筛全部窗口失败 → 必须说成失败，不能说成「没找到候选」",
        lambda: _screen_all_windows_fail(),
        expect_in=("全部调用失败", "dashscope"),
        expect_not_in=("把需求描述写得更具体",),
        expect_hint="实测原缺陷：端点通道选错导致唯一窗口失败，"
                    "用户看到的是「没找到候选，换个素材试试」")

    # ══════════════════════════════════════════════════════
    head("汇总")
    bad = [r for r in ROWS
           if r["verdict"] in ("静默", "误导", "信息不全", "崩溃", "?")]
    print(f"  共 {len(ROWS)} 个场景")
    print(f"  合格 {len(ROWS) - len(bad)}｜**需处理 {len(bad)}**")
    if bad:
        print()
        print("  需要处理：")
        for r in bad:
            print(f"    [{'⚠️ ' if r['verdict'] in ('静默', '误导') else '🔴'}] "
                  f"{r['name']} — {r['verdict']}")
            print(f"         {r['note'][:110]}")

    out = WORK / "fault_report.json"
    out.write_text(json.dumps(ROWS, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n  明细：{out}")
    return 0


# ─────────────────────────────────────────────────────────
# 各场景实现
# ─────────────────────────────────────────────────────────
def _out_is_file() -> dict:
    """输出根"目录"其实是个文件 —— 模拟"写不进去"。"""
    p = WORK / "not_a_dir"
    p.write_text("我是个文件，不是目录", encoding="utf-8")
    # 让异常自然冒泡 —— 由 scenario() 统一判定可读性
    editor.export_clips(MEDIA, [{"id": 1, "start": 2.0, "end": 6.0,
                                 "title_hint": "x"}], out_base=p)
    return {"ok": False, "note": "把文件当输出目录却返回成功"}


def _with_bad_font() -> dict:
    preset = subtitle_style.builtin_default().copy_as("坏字体")
    preset.main.font = "NoSuchFontXYZ"
    preset.sub.font = "NoSuchFontXYZ"
    try:
        res = editor.export_clips(
            MEDIA, [{"id": 1, "start": 2.0, "end": 8.0, "title_hint": "坏字体"}],
            out_base=fresh("b6"), burn_subtitles=True, segments=[],
            preset=preset, width=406, height=720)
        return {"ok": True,
                "note": f"用不存在的字体导出了 {res.ok_count} 条",
                "info": "libass 静默 fallback —— 用户会以为'改了没反应'，"
                        "须由 UI 层校验（style_page 已实现）"}
    except editor.EditorError:
        raise


def _bad_ffmpeg_path() -> dict:
    """手动指定的 ffmpeg 路径无效 —— 应当**回退**，但必须让 UI 能知道。"""
    from sliceq import settings as st
    old = st.get("ffmpeg_path", "")
    try:
        st.set("ffmpeg_path", str(WORK / "没有这个程序.exe"))
        res = editor.export_clips(MEDIA, [{"id": 1, "start": 2.0, "end": 6.0,
                                           "title_hint": "x"}],
                                  out_base=fresh("c1"))
        src = ffmpeg_tools.ffmpeg_source()

        # ⚠️ 判据修正：回退**本身是对的** —— 用户填错路径不该让整个程序不可用。
        #    有问题的是"回退了却不告诉用户"：他以为自己设置生效了。
        #    所以这里判定的是"能否被检测到"，不是"有没有报错"。
        detected = bool(src.get("manual_invalid"))
        return {"ok": detected,
                "note": f"回退到「{src.get('source')}」，导出仍成功 "
                        f"（{res.ok_count} 条）",
                "info": "回退正确；检测标志 "
                        f"manual_invalid={detected}，probe_environment 会把它说出来"}
    finally:
        st.set("ffmpeg_path", old)


def _broken_preset() -> dict:
    config.ensure_dirs()
    p = config.STYLES_DIR / "损坏测试.json"
    p.write_text("{ 这不是合法 JSON ", encoding="utf-8")
    preset = subtitle_style.load_preset("损坏测试")
    return {"ok": bool(preset and preset.main.size > 0),
            "note": f"读坏文件未抛错，回退到 size={preset.main.size} "
                    f"font={preset.main.font}",
            "info": "关键：预设文件坏掉不能让界面打不开"}


def _bad_preset_types() -> dict:
    config.ensure_dirs()
    p = config.STYLES_DIR / "类型错误.json"
    p.write_text(json.dumps({
        "name": "类型错误",
        "vertical_gap": "不是数字",
        "main": {"size": "很大", "color": 12345, "outline": None},
        "sub": "本该是个对象",
    }, ensure_ascii=False), encoding="utf-8")
    preset = subtitle_style.load_preset("类型错误")
    return {"ok": bool(preset),
            "note": f"脏数据未抛错，解析为 size={preset.main.size} "
                    f"color={preset.main.color} gap={preset.vertical_gap}",
            "info": "类型错乱应被纠正为默认值而不是崩"}


def _remap_outside() -> dict:
    cues = [subtitle.Cue(src_start=100.0, src_end=110.0, start=100.0,
                         end=110.0, main="完全在外")]
    out, dropped = subtitle.remap_to_clip(cues, 0.0, 5.0)
    return {"ok": len(out) == 0 and dropped == 1,
            "note": f"保留 {len(out)}／丢弃 {dropped}",
            "info": "完全落在片段外应全部丢弃且计数正确"}


def _wrap_empty() -> dict:
    r = subtitle_style.wrap_for_canvas("", width=406, size=42)
    return {"ok": r == "", "note": f"返回 {r!r}"}


def _wrap_single() -> dict:
    r = subtitle_style.wrap_for_canvas("好", width=406, size=42)
    return {"ok": r == "好", "note": f"返回 {r!r}"}


def _wrap_tiny() -> dict:
    t = "很长的一句话用于测试极小画布"
    r = subtitle_style.wrap_for_canvas(t, width=80, size=42)
    joined = r.replace(r"\N", "")
    return {"ok": joined == t,
            "note": f"{len(r.split(chr(92) + 'N'))} 行，拼接"
                    f"{'一致' if joined == t else '**丢字**'}"}


def _wrap_huge_font() -> dict:
    t = "字号比画布还大"
    r = subtitle_style.wrap_for_canvas(t, width=100, size=200)
    joined = r.replace(r"\N", "")
    return {"ok": joined == t, "note": f"{len(r.split(chr(92) + 'N'))} 行（未丢字）"}


def _preset_traversal() -> dict:
    """路径穿越：预设名里塞 ../ 之类。"""
    evil = "../../../../evil"
    safe = subtitle_style.safe_name(evil)
    path = subtitle_style.preset_path(evil)
    inside = config.STYLES_DIR in path.parents or path.parent == config.STYLES_DIR
    return {"ok": inside and "/" not in safe and "\\" not in safe,
            "note": f"清洗为 {safe!r}，落在样式目录内：{inside}",
            "info": "防止写到目录外"}


def _preset_empty_name() -> dict:
    safe = subtitle_style.safe_name("")
    p = subtitle_style.preset_path("")
    return {"ok": bool(safe) and p.parent == config.STYLES_DIR,
            "note": f"空名 → {safe!r}"}


class _FailAllTransport:
    """所有模型调用都失败 —— 模拟「端点通道选错 / 依赖缺失」。

    粗筛走 `analyze`（带图），纯文本路径走 `analyze_text`，两个都要覆盖。
    """

    name = "fail-all"

    def __init__(self, msg: str) -> None:
        self.msg = msg
        self.calls = 0

    def analyze(self, *a, **k):
        self.calls += 1
        raise analyzer.AnalyzerError(self.msg)

    def analyze_text(self, *a, **k):
        self.calls += 1
        raise analyzer.AnalyzerError(self.msg)

    def max_video_bytes(self) -> int:
        return 15 * 1024 * 1024


def _screen_all_windows_fail():
    """★ 粗筛「全部窗口失败」必须报失败，不能报「没找到候选」。

    实测原缺陷（2026-09-27）：

        端点被配成需要 `dashscope` 包的通道（那个包没装）
        ⇒ 唯一一个粗筛窗口失败 ⇒ 候选 0 段
        ⇒ 用户看到的是「没有找到符合条件的候选段。
           可以试试：① 把需求描述写得更具体；② 调高筛选强度；
           ③ 换一段素材验证。」

    这个缺陷的性质是：**没分析成 ≠ 分析完发现没有**。
    两者给用户的下一步动作完全不同（前者要修配置，后者要调需求）。
    """
    segs = [asr.Segment(**d)
            for d in json.loads(SEG_CACHE.read_text(encoding="utf-8"))]
    store.init_db()
    tid = store.create_task("粗筛全失败", source_path=str(MEDIA),
                            duration_sec=90.0)
    tp = _FailAllTransport(
        "缺少 dashscope 包。请执行 `pip install dashscope`，"
        "或在设置页把端点改为兼容中转站（走 HTTP 通道）。")
    try:
        pipeline.run(tid, MEDIA, pipeline.Options(), tp,
                     on_confirm=lambda req: True, skip_transcribe=segs)
    finally:
        st = (store.get_task(tid) or {}).get("status")
        print(f"      （任务状态落库 = {st}）")
    # 走不到这里才算对；真走到了说明"全失败"被当成了正常结果
    return {"ok": False,
            "note": "粗筛全部窗口失败却正常返回了 —— 应该抛 PipelineError"}


if __name__ == "__main__":
    sys.exit(main())
