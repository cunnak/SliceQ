# -*- coding: utf-8 -*-
"""前置实测：转录分片并发的真实收益（阶段 4-D · F15 剩余项）。

## 要回答的问题

`asr.transcribe()` 现在逐片串行跑。改成并发，**在 GPU 上到底有没有收益？**

先验判断（可能错）：多进程同时用同一块 GPU，
- 显存不够 → 直接 OOM 失败，或者退化成 CPU（那会慢 6~9 倍）
- 够 → CUDA 时间片调度，可能只是把同样的计算摊开，总时间不降反升

这是"看起来该做、实际未必有益"的典型。所以在动手改代码前先量。

## 实验设计

从真实录播抽 N 片等长音频（模拟 chunk），每片走**产品代码**
`asr.transcribe_chunk()`，对比：

    A 串行 1 路        —— 基线
    B 并发 2 路
    C 并发 4 路

每个场景都记录：总墙钟、单片耗时、段数、文本指纹（验证结果一致）。

⚠️ 关键：**文本必须逐片比对**。并发如果让结果变了（比如 GPU 抢占导致
   解码不稳定），那"更快"是没有意义的。

## 显存监测

后台起一个 `nvidia-smi` 采样线程，记录场景期间的峰值显存占用。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq import asr, asr_models, ffmpeg_tools          # noqa: E402

SRC = Path(r"C:\未明子录播\260925\[未明子]2026年09月25日00时录播.mp4")
WORK = Path(r"C:\Users\<user>\AppData\Local\Temp\sliceq_asrconc")
SEG_SEC = 180.0          # 每片 3 分钟
N_SEG = 4
START = 3600.0           # 从 1 小时处开始（避开开场放歌）


# ─────────────────────────────────────────────────────────────
# 显存采样
# ─────────────────────────────────────────────────────────────
class VramSampler:
    def __init__(self, interval: float = 0.5) -> None:
        self.interval = interval
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                r = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    self.samples.append(float(r.stdout.strip().splitlines()[0]))
            except Exception:                       # noqa: BLE001
                pass
            self._stop.wait(self.interval)

    def start(self) -> None:
        self.samples.clear()
        self._stop.clear()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self) -> tuple[float, float]:
        self._stop.set()
        if self._t:
            self._t.join(timeout=3)
        if not self.samples:
            return -1.0, -1.0
        return max(self.samples), sum(self.samples) / len(self.samples)


# ─────────────────────────────────────────────────────────────
# 素材准备
# ─────────────────────────────────────────────────────────────
def prepare() -> list[Path]:
    WORK.mkdir(parents=True, exist_ok=True)
    ff = str(ffmpeg_tools.find_ffmpeg())
    out: list[Path] = []
    for i in range(N_SEG):
        p = WORK / f"piece_{i}.wav"
        if p.exists() and p.stat().st_size > 1024:
            out.append(p)
            continue
        a = START + i * SEG_SEC
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{a:.3f}", "-t", f"{SEG_SEC:.3f}",
                        "-i", str(SRC), "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", str(p)],
                       capture_output=True, timeout=600)
        out.append(p)
    return out


USE_GPU = True
MAXW = 4                 # 最大并发场景（CPU 模式很慢，可以调小省时间）


def run_case(pieces: list[Path], workers: int, tag: str) -> dict:
    """跑一个场景，返回统计。"""
    model = asr_models.model_path()
    per: dict[int, float] = {}
    segs_by_idx: dict[int, list] = {}
    errs: list[str] = []

    def job(i: int) -> list:
        dest = WORK / f"{tag}_{i}.jsonl"
        t0 = time.time()
        try:
            s = asr.transcribe_chunk(pieces[i], dest, model=model,
                                     language="zh", use_gpu=USE_GPU, timeout=1800)
        except BaseException as exc:                # noqa: BLE001
            per[i] = time.time() - t0
            errs.append(f"片 {i}: {type(exc).__name__}: {exc}")
            return []
        per[i] = time.time() - t0
        return s

    vram = VramSampler()
    vram.start()
    t0 = time.time()
    if workers <= 1:
        for i in range(len(pieces)):
            segs_by_idx[i] = job(i)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(job, i): i for i in range(len(pieces))}
            for f, i in futs.items():
                segs_by_idx[i] = f.result()
    wall = time.time() - t0
    peak, mean = vram.stop()

    total_segs = sum(len(v) for v in segs_by_idx.values())
    text = "".join(s.text for i in range(len(pieces))
                   for s in segs_by_idx.get(i, []))
    return {
        "tag": tag, "workers": workers, "wall": round(wall, 2),
        "per": {k: round(v, 2) for k, v in sorted(per.items())},
        "segs": [len(segs_by_idx.get(i, [])) for i in range(len(pieces))],
        "total_segs": total_segs,
        "text_len": len(text),
        "text_md5": __import__("hashlib").md5(text.encode()).hexdigest()[:12],
        "text": text[:160],
        "vram_peak": peak, "vram_mean": round(mean, 1),
        "errors": errs,
    }


def main() -> int:
    global USE_GPU, SEG_SEC, N_SEG, START, MAXW
    args = sys.argv[1:]
    if "--cpu" in args:                 # 测 CPU 路径（慢很多，用短片）
        USE_GPU = False
    for a in args:
        if a.startswith("--seg="):
            SEG_SEC = float(a.split("=", 1)[1])
        elif a.startswith("--n="):
            N_SEG = int(a.split("=", 1)[1])
        elif a.startswith("--start="):
            START = float(a.split("=", 1)[1])
        elif a.startswith("--maxw="):
            MAXW = int(a.split("=", 1)[1])
    if not SRC.exists():
        print(f"素材不存在：{SRC}")
        return 1
    print("=" * 84)
    print("转录分片并发实测 —— 真实素材 / 真实 GPU / 走产品代码 asr.transcribe_chunk")
    print("=" * 84)
    print(f"素材：{SRC.name}")
    print(f"分片：{N_SEG} × {SEG_SEC:.0f}s，起点 {START:.0f}s")
    print(f"模型：{asr_models.model_path().name}　"
          f"GPU={'开' if USE_GPU else '关（CPU 模式）'}")

    print("\n[0] 准备音频分片…")
    pieces = prepare()
    for i, p in enumerate(pieces):
        print(f"    片 {i}  {p.stat().st_size / 1048576:.1f} MB")

    base = None
    results = []
    cases = [(1, "serial"), (2, "w2"), (4, "w4")]
    if MAXW < 4:
        cases = [(w, t) for w, t in cases if w <= MAXW]
    for workers, tag in cases:
        print(f"\n[{tag}] 并发 {workers} 路 …")
        r = run_case(pieces, workers, tag)
        results.append(r)
        print(f"    墙钟 {r['wall']:.2f}s　单片 {r['per']}")
        print(f"    段数 {r['segs']}（共 {r['total_segs']}）"
              f"　文本 {r['text_len']} 字　md5 {r['text_md5']}")
        print(f"    显存 峰值 {r['vram_peak']:.0f}MB / 均值 {r['vram_mean']:.0f}MB")
        if r["errors"]:
            for e in r["errors"]:
                print(f"    ❌ {e[:150]}")
        if base is None:
            base = r
        else:
            speed = base["wall"] / r["wall"] if r["wall"] else 0
            same = r["text_md5"] == base["text_md5"]
            print(f"    → 相对串行加速 {speed:.2f}×　"
                  f"结果与串行{'完全一致' if same else '**不一致**'}")

    print("\n" + "=" * 84)
    print("汇总")
    print("=" * 84)
    print(f"{'场景':<8}{'墙钟':>9}{'加速':>8}{'段数':>7}{'md5':>15}{'显存峰值':>10}")
    for r in results:
        sp = f"{base['wall'] / r['wall']:.2f}×" if r["wall"] else "—"
        print(f"{r['tag']:<8}{r['wall']:>8.2f}s{sp:>8}"
              f"{r['total_segs']:>7}{r['text_md5']:>15}{r['vram_peak']:>9.0f}M")

    out = ROOT / "reports" / "assets" / "asr_concurrency_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"seg_sec": SEG_SEC, "n_seg": N_SEG,
                               "results": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已落盘：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
