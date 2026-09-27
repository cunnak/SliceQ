# -*- coding: utf-8 -*-
r"""GPU vs CPU 的 whisper 转录速度对比。

背景：真实直播素材实测发现，**CPU 下 small 模型 0.88× 实时率** ——
      3 小时直播要转 2.6 小时，比实时还慢，直接动摇"本地免费转录做粗筛"的前提。

      TECH-DESIGN 里 `use_gpu` 默认是 `true`，但从未验证过。
      本脚本就是来定这一个数：**GPU 能快多少？**

用法：
    python probe_asr_gpu_speed.py <音频> [模型1 模型2 ...]
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "q", HERE / "probe_live_asr_quality.py")
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    wav = Path(sys.argv[1])
    models = sys.argv[2:] or ["ggml-base.bin", "ggml-small.bin"]

    if not wav.exists():
        print(f"找不到音频：{wav}")
        return 1

    dur = q.audio_duration(wav)
    print("=" * 78)
    print("whisper 转录：GPU vs CPU 速度对比")
    print("=" * 78)
    print(f"音频：{wav.name}   时长 {dur:.1f}s")
    print(f"模型：{', '.join(models)}")
    print("-" * 78)

    rows: list[tuple] = []

    for mname in models:
        model = q.MODEL_DIR / mname
        if not model.exists():
            print(f"[跳过] {mname} 未安装")
            continue

        for gpu in (True, False):
            dest = wav.parent / f"speed_{mname}_{'gpu' if gpu else 'cpu'}.jsonl"
            dest.unlink(missing_ok=True)

            t0 = time.time()
            try:
                segs, dt = q.transcribe(wav, model, dest,
                                        use_gpu=gpu, timeout=1800)
            except Exception as exc:
                print(f"  {mname:20s} {'GPU' if gpu else 'CPU'}  异常: {exc}")
                continue

            rtf = dt / dur if dur else 0
            tag = "GPU" if gpu else "CPU"
            n = len(segs)
            chars = sum(len(s.get("text", "").strip()) for s in segs)
            rows.append((mname, tag, dt, rtf, n, chars))
            print(f"  {mname:20s} {tag}   {dt:7.1f}s   "
                  f"实时率 {rtf:5.2f}x   段数 {n:3d}   字数 {chars}")

    # ── 推算 3 小时直播的耗时 ─────────────────
    print()
    print("=" * 78)
    print("推算：3 小时直播要转录多久")
    print("=" * 78)
    print(f"  {'模型':22s}{'设备':6s}{'实时率':>9s}{'3小时需':>12s}{'可接受?':>10s}")
    for mname, tag, dt, rtf, n, chars in rows:
        hours = 3 * rtf
        verdict = "✅" if hours <= 0.5 else ("⚠️" if hours <= 1.5 else "❌")
        print(f"  {mname:22s}{tag:6s}{rtf:>8.2f}x"
              f"{hours:>10.2f}h{verdict:>12s}")

    print("""
  判据（供参考）：
    ✅ ≤ 0.5h   用户能接受（3 小时视频等半小时）
    ⚠️ ≤ 1.5h   勉强（要去泡杯茶）
    ❌ > 1.5h   不可接受，等于"转完天亮了"
""")

    # ── GPU 与 CPU 的产出是否一致 ──────────────
    print("=" * 78)
    print("检查：GPU 与 CPU 的输出是否一致（GPU 不该降低质量）")
    print("=" * 78)
    for mname in models:
        g = wav.parent / f"speed_{mname}_gpu.jsonl"
        c = wav.parent / f"speed_{mname}_cpu.jsonl"
        if not (g.exists() and c.exists()):
            continue
        tg = "".join(s.get("text", "") for s in _read(g))
        tc = "".join(s.get("text", "") for s in _read(c))
        same = tg == tc
        print(f"  {mname:22s} {'完全一致' if same else '有差异'}"
              f"   GPU {len(tg)} 字 / CPU {len(tc)} 字")
        if not same:
            print(f"      GPU: {tg[:100]}")
            print(f"      CPU: {tc[:100]}")

    return 0


def _read(p: Path) -> list[dict]:
    import json
    out = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


if __name__ == "__main__":
    raise SystemExit(main())
