# -*- coding: utf-8 -*-
r"""asr.py 端到端验收 —— 静音切除 + 三层幻觉治理。

覆盖三种素材，其中后两种是**边界**：
  1. live_silence.wav  —— 140s，含 53.6s 长静音（主用例）
  2. pure_silence.wav  —— 30s 纯静音，语音区间为空（边界：应产出 0 段）
  3. live_3600.wav     —— 60s 语音密集，几乎无静音（边界：不应误切）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ⚠️ 数据隔离：本测试只写临时目录，**绝不碰用户数据**。
#    本项目曾因自测写生产数据丢过用户的 API key（见 sliceq/_testing.py）。
#    注意：**间接写入也算**（例如调 bridge.set_manual_path() 会写 settings.json）
#    —— 所以默认给所有自测都隔离，不做"看起来不碰数据"的判断。
from sliceq._testing import assert_isolated, isolate_data_root        # noqa: E402
isolate_data_root()
assert_isolated()

from sliceq import asr

VAD_DIR = Path("stage0/test_media/vad_test")
LIVE_DIR = Path("stage0/test_media/live_test")

CASES = [
    ("含 53.6s 长静音", VAD_DIR / "live_silence.wav"),
    ("30s 纯静音（边界）", VAD_DIR / "pure_silence.wav"),
    ("60s 语音密集（边界）", LIVE_DIR / "live_3600.wav"),
]

ok = fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"    [OK]   {name}" + (f"  — {detail}" if detail else ""))
    else:
        fail += 1
        print(f"    [FAIL] {name}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    print("=" * 78)
    print("asr.py 端到端验收 · 静音切除 + 三层幻觉治理")
    print("=" * 78)
    print(f"  CUDA: {asr.has_cuda()}\n")

    for label, src in CASES:
        if not src.exists():
            print(f"  跳过（找不到 {src}）\n")
            continue

        print(f"── {label} · {src.name} " + "─" * 30)
        dur = asr._wav_duration(src)

        msgs: list[str] = []
        work = VAD_DIR / f"e2e_{src.stem}"
        segs = asr.transcribe(
            src,
            work_dir=work,
            progress=lambda p, m: msgs.append(m) if m not in msgs else None,
        )

        sils = asr.find_silences(src)
        ranges = asr.speech_ranges(sils, dur)
        hallu = [s for s in segs if asr.looks_like_hallucination(s.text)]

        print(f"    时长 {dur:.1f}s｜静音 {len(sils)} 段 "
              f"（{sum(e - s for s, e in sils):.1f}s）｜语音区间 {len(ranges)} 段")
        print(f"    产出 {len(segs)} 段｜幻觉 {len(hallu)} 段")
        print(f"    末次进度：{msgs[-1] if msgs else '(无)'}")

        # 断言
        check("无幻觉残留", len(hallu) == 0,
              f"剩 {len(hallu)} 段" if hallu else "全部剔除")
        check("时间戳非负且升序",
              all(s.start_ms >= 0 and s.end_ms >= s.start_ms for s in segs)
              and all(segs[i].start_ms <= segs[i + 1].start_ms
                      for i in range(len(segs) - 1)))
        check("时间戳不超出素材长度",
              all(s.end_ms <= dur * 1000 + 1500 for s in segs),
              f"最长 {max((s.end_ms for s in segs), default=0) / 1000:.1f}s")

        if ranges:
            # 所有分段起点都应落在某个语音区间里（映射正确性的关键判据）
            outside = [s for s in segs
                       if not any(a - 0.6 <= s.start_ms / 1000 <= b + 0.6
                                  for a, b in ranges)]
            check("分段起点全部落在语音区间内", len(outside) == 0,
                  f"{len(outside)} 段越界" if outside else "映射正确")
        else:
            check("纯静音应产出 0 段", len(segs) == 0, f"实际 {len(segs)} 段")

        print()
        if segs[:3]:
            print("    前 3 段：")
            for s in segs[:3]:
                print(f"      {s.start_ms/1000:7.1f}→{s.end_ms/1000:7.1f}s  "
                      f"{s.text[:40]}")

    print("=" * 78)
    print(f"结果：{ok} 项通过，{fail} 项失败")
    print("=" * 78)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
