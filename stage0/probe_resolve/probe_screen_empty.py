# -*- coding: utf-8 -*-
r"""粗筛返回空 —— 是"真没找到"还是"被思考挤掉"？

线索：上一次真实调用 `out=173, reasoning=168` —— 思考占了 97%，
正文只剩 5 个 token（很可能就是 `[]`）。

本脚本做三件事：
  1. 打印**完整原始响应**（含 finish_reason —— 它能区分"正常结束"与"被截断"）
  2. 对照 `thinking=auto` vs `thinking=off`
  3. 顺便看看本地转录到底产出了什么（判断素材本身有没有内容）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sliceq import analyzer, prompts, screening

VIDEO = Path("stage0/test_media/source.mp4")
PROFILE = "找主播讲得有意思的片段，或者有情绪的地方"


def show_raw(res, label: str) -> None:
    """把响应的关键字段摊开。"""
    print(f"  ── {label} ──")
    print(f"  usage: in={res.usage.input_tokens} out={res.usage.output_tokens} "
          f"reasoning={res.usage.reasoning_tokens}")
    raw = res.raw if isinstance(res.raw, dict) else {}
    choices = raw.get("choices") or [{}]
    ch = choices[0] if choices else {}
    print(f"  finish_reason: {ch.get('finish_reason')!r}")
    msg = ch.get("message") or {}
    print(f"  content 长度 : {len(msg.get('content') or '')}")
    print(f"  content      : {repr((msg.get('content') or '')[:200])}")
    if msg.get("reasoning_content"):
        rc = msg["reasoning_content"]
        print(f"  reasoning 长度: {len(rc)}")
        print(f"  reasoning 摘录: {repr(rc[:300])}")
    print()


def main() -> int:
    print("=" * 78)
    print("粗筛返回空的原因诊断")
    print("=" * 78)

    # ── 1. 本地转录产出了什么 ────────────────────────
    work = Path("stage0/test_media/source")
    wav = work / "audio.wav"
    if wav.exists():
        import subprocess
        r = subprocess.run(
            [str(analyzer.Path) if False else
             r"C:\Users\<user>\AppData\Roaming\SliceQ\bin\ffprobe.exe",
             "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(wav)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(f"── 素材：{VIDEO.name}，音频时长 {r.stdout.strip()}s ──")

    # 转录结果（用模块自带的解析，保证与 pipeline 一致）
    from sliceq import asr
    try:
        segs = asr.transcribe(VIDEO, work_dir=work)
        print(f"  本地转录 {len(segs)} 段：")
        for s in segs[:10]:
            print(f"    {s.start_s:6.1f}→{s.end_s:6.1f}s  {s.text[:46]}")
        transcript = "".join(s.text for s in segs)
    except Exception as exc:
        print(f"  转录失败：{type(exc).__name__}: {exc}")
        transcript = ""
    print()

    # ── 2. 造一张拼贴图 ──────────────────────────────
    sheet = Path("stage0/test_media/source") / "frames" / "diag_sheet.png"
    got = screening.build_contact_sheet(VIDEO, sheet, 0.0, 12.0)
    print(f"── 拼贴图：{'生成成功 ' + str(got) if got else '生成失败'} ──")
    if got:
        print(f"  体积 {got.stat().st_size / 1024:.0f} KB")
    print()

    prompt = prompts.screen_prompt(PROFILE, 0.0, 12.0, transcript,
                                   has_frames=bool(got))

    t = analyzer.make_transport()
    print(f"── transport: {type(t).__name__} ──\n")

    # ── 3. 两种思考模式对照 ──────────────────────────
    for mode in ("auto", "off"):
        print(f"══ thinking = {mode} ══")
        try:
            res = t.analyze(got, prompt, system=prompts.SCREEN_SYSTEM,
                            thinking=mode, timeout=300)
            show_raw(res, f"thinking={mode}")
            cands = screening.parse_candidates(res.text, 0.0, 12.0, "frame")
            print(f"  → 解析出候选 {len(cands)} 段\n")
        except Exception as exc:
            print(f"  ❌ {type(exc).__name__}: {str(exc)[:200]}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
