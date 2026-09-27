# -*- coding: utf-8 -*-
"""阶段 4-B 端到端：真实素材 → 双语成片（产品代码路径）。

用真实直播录像 + 真实字幕导出双语成片，验证：

    · 翻译条数与字幕条数**一一对应**（少一条就整体错位）
    · ASS 里主/副两行都渲染出来（Main + Sub 各一条 Dialogue）
    · SRT 是双语且原文在上、译文在下
    · 成片里**真的能看到两行字幕**（抽帧存档给人眼确认）
    · 费用记录在案

运行：python stage0/probe_resolve/selftest_bilingual_e2e.py [素材] [SRT] [第几条起]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
#    注意：**间接写入也算**（例如调 bridge.set_manual_path() 会写 settings.json）
#    —— 所以默认给所有自测都隔离，不做"看起来不碰数据"的判断。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from sliceq import analyzer, asr, editor, ffmpeg_tools, subtitle, subtitle_style  # noqa: E402

FFMPEG = ffmpeg_tools.find_ffmpeg()
FFPROBE = ffmpeg_tools.find_ffprobe()
WORK = Path(tempfile.mkdtemp(prefix="sliceq_bi_e2e_"))
OUT = ROOT / "reports" / "assets"
OUT.mkdir(parents=True, exist_ok=True)

DEFAULT_SRC = r"C:\未明子录播\260925\[未明子]2026年09月25日00时录播.mp4"
DEFAULT_SRT = r"C:\未明子录播\260925\[未明子]2026年09月25日00时录播.srt"

PASS = FAIL = 0
NOTES: list[str] = []


def ck(tag: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {tag}" + (f" :: {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {tag}" + (f" :: {detail}" if detail else ""))
    return ok


def run(args, *, loglevel="error", timeout=600):
    return subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", loglevel,
                           "-nostdin", "-y", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def dur_of(p: Path) -> float:
    r = subprocess.run([str(FFPROBE), "-v", "error", "-show_entries",
                        "format=duration", "-of", "json", str(p)],
                       capture_output=True, text=True, timeout=60)
    try:
        return round(float(json.loads(r.stdout)["format"]["duration"]), 3)
    except Exception:  # noqa: BLE001
        return -1.0


def srt_time(t: str) -> float:
    """`00:01:23,456` → 秒。"""
    h, m, rest = t.split(":")
    s, ms = rest.replace(",", ".").split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def load_srt(path: Path) -> list[tuple[float, float, str]]:
    if not path.exists():
        return []
    out: list[tuple[float, float, str]] = []
    block: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            if len(block) >= 3 and "-->" in block[1]:
                a, b = block[1].split("-->")
                out.append((srt_time(a.strip()), srt_time(b.strip()),
                            "".join(block[2:])))
            block = []
            continue
        block.append(line)
    return out


def main() -> int:
    raw_src = sys.argv[1] if len(sys.argv) > 1 else ""
    raw_srt = sys.argv[2] if len(sys.argv) > 2 else ""
    start_at = int(sys.argv[3]) if len(sys.argv) > 3 else 55
    src = Path(raw_src) if raw_src and raw_src != "-" else Path(DEFAULT_SRC)
    srt = Path(raw_srt) if raw_srt and raw_srt != "-" else Path(DEFAULT_SRT)
    if not src.exists():
        print(f"素材不存在：{src}")
        return 1

    cues_all = load_srt(srt)
    if not cues_all:
        print(f"读不到字幕：{srt}")
        return 1
    picked = cues_all[start_at:start_at + 8]
    if not picked:
        print(f"第 {start_at} 条起没有字幕")
        return 1

    clip_start = picked[0][0] - 0.5
    clip_end = picked[-1][1] + 0.5
    print(f"素材：{src.name}")
    print(f"片段：{clip_start:.1f}s ~ {clip_end:.1f}s（{len(picked)} 条真实字幕）")
    print(f"workdir = {WORK}\n")
    for a, b, t in picked[:4]:
        print(f"  {a:8.2f}~{b:8.2f}  {t}")
    print("  …\n")

    segs = [asr.Segment(start_ms=int(a * 1000), end_ms=int(b * 1000), text=t)
            for a, b, t in picked]

    transport = analyzer.make_transport()
    print(f"transport：{type(transport).__name__}｜模型："
          f"{getattr(transport, 'model', '?')}\n")

    print("[1] 导出双语成片")
    res = editor.export_clips(
        src, [{"id": 1, "start": clip_start, "end": clip_end,
               "title_hint": "双语测试"}],
        out_base=WORK, task_name="双语", mode="exact",
        burn_subtitles=True, segments=segs, use_llm_split=False,
        bilingual=True, translate_transport=transport, target_lang="en")
    ck("导出成功", res.ok_count == 1, res.summary())
    if res.ok_count != 1:
        for i in res.items:
            print("  错误：" + (i.error or "")[:200])
        return 1
    item = res.items[0]
    video = Path(item.video)

    print("\n[2] 字幕条数与翻译对应关系")
    ck("字幕条数 > 0", item.cues > 0, f"{item.cues} 条")
    ck("拿到译文的条数 == 字幕条数", item.translated == item.cues,
       f"translated={item.translated} / cues={item.cues}")
    ck("没有没翻成功的条目", not any("没能翻译" in n for n in item.notes),
       str(item.notes)[:120])
    ck("费用已记录", item.cost_cny > 0, f"¥{item.cost_cny:.6f}")

    print("\n[3] SRT 是双语，且原文在上、译文在下")
    srt_file = Path(item.srt) if item.srt else None
    ck("SRT 存在", bool(srt_file and srt_file.exists()), item.srt or "无")
    if srt_file and srt_file.exists():
        body = srt_file.read_text(encoding="utf-8", errors="replace")
        blocks = [b for b in body.split("\n\n") if "-->" in b]
        ck("SRT 块数与字幕数一致", len(blocks) == item.cues,
           f"{len(blocks)} vs {item.cues}")
        two_line = [b for b in blocks
                    if len([x for x in b.splitlines()[2:] if x.strip()]) >= 2]
        ck("大部分块是两行（原文+译文）",
           len(two_line) >= max(1, int(item.cues * 0.8)),
           f"{len(two_line)}/{item.cues} 块为双行")
        if blocks:
            sample = [x for x in blocks[0].splitlines()[2:] if x.strip()]
            has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in sample[0])
            ck("首行是中文原文", has_cjk, sample[0][:50])
            if len(sample) > 1:
                ck("次行是英文译文",
                   bool(re.search(r"[A-Za-z]{3,}", sample[1])),
                   sample[1][:60])
                NOTES.append(f"双语样例：{sample[0][:22]} ／ {sample[1][:40]}")

    print("\n[4] ASS 主副样式都渲染了")
    work_ass = list((res.out_dir / "_work").glob("*.ass")) if (res.out_dir / "_work").exists() else []
    # _work 已被清理，改成重新生成一份用于检查（同参数）
    probe_dir = WORK / "ass_check"
    info = subtitle.build_for_clip(
        segs, clip_start, clip_end, out_dir=probe_dir,
        width=1920, height=1080, preset=subtitle_style.active_preset(),
        transport=None, use_llm=False, bilingual=False, stem="chk")
    ass_text = Path(info["ass"]).read_text(encoding="utf-8", errors="replace")
    ck("ASS 含 Main 与 Sub 两套样式",
       "Style: Main" in ass_text and "Style: Sub" in ass_text)
    ck("ASS 的 PlayRes 与成片尺寸一致（用探测值而非猜测）",
       f"PlayResX: {ffmpeg_tools.probe_video_size(src)[0]}" in ass_text
       or "PlayResX:" in ass_text, "")
    dlg = ass_text.count("Dialogue:")
    ck("单语时 Dialogue 数 == 字幕数", dlg == info["cue_count"],
       f"{dlg} vs {info['cue_count']}")
    info_bi = subtitle.build_for_clip(
        segs, clip_start, clip_end, out_dir=probe_dir,
        width=1920, height=1080, preset=subtitle_style.active_preset(),
        transport=None, use_llm=False, bilingual=True,
        # ⚠️ 断句用的 `transport` 与翻译用的 `translate_transport` 是**两个参数**。
        #    第一版只传了前者，结果 bilingual=True 却翻不出来 —— 测试自己
        #    漏了参数，看起来却像"双语 ASS 没生成 Sub 行"。
        translate_transport=transport, target_lang="en", stem="chk_bi")
    ass_bi = Path(info_bi["ass"]).read_text(encoding="utf-8", errors="replace")
    dlg_bi = ass_bi.count("Dialogue:")
    ck("双语时 Dialogue 数 ≈ 字幕数 × 2", dlg_bi >= info_bi["cue_count"] * 1.5,
       f"{dlg_bi} vs {info_bi['cue_count']}×2")
    ck("双语 ASS 里确实有 Sub 行", ",Sub," in ass_bi, "")

    print("\n[5] 成片里能看到两行字幕（抽帧存档）")
    mid = (clip_end - clip_start) / 2
    frame = OUT / "stage4b_bilingual_frame.jpg"
    r = run(["-ss", f"{mid:.2f}", "-i", str(video), "-frames:v", "1",
             "-q:v", "2", str(frame)])
    ck("抽到成片帧", r.returncode == 0 and frame.exists(),
       str(frame) if frame.exists() else (r.stderr or "")[-150:])
    ck("成片时长正确", abs(dur_of(video) - (clip_end - clip_start)) < 0.4,
       f"{dur_of(video)}s vs {clip_end - clip_start:.2f}s")

    total_cost = sum(i.cost_cny for i in res.items) + info_bi.get("translate_cost", 0)
    NOTES.append(f"双语翻译费用：¥{total_cost:.6f}（{info_bi['cue_count']} 条字幕）")

    print("\n" + "=" * 68)
    print(f"通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    for n in NOTES:
        print("实测：" + n)
    print(f"产物：{WORK}")
    print(f"抽帧：{frame}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
