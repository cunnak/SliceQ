# -*- coding: utf-8 -*-
"""阶段 4-A 真实素材验证：ducking 参数在**真实直播人声**上怎么标定。

为什么必须做这一步
------------------
自测用 440Hz 正弦当"人声" —— 那是能量高度集中的理想信号，压缩器一碰就触发。
真实直播人声能量散在 100~4000Hz，峰值低得多。
第一轮把合成信号上"有效"的 thr=0.05 搬到真实素材，结果是
**BGM 被压掉 23.8dB 且全程都在压**（最弱窗也压 22dB）——
因为真实人声中位 -11.9dB ≈ 线性 0.25，thr=0.05 远低于它，任何时刻都远超阈值。

实验设计（受控）
----------------
不用 silencedetect 去猜"哪段没人说话"（未明子直播全程有声，各阈值都抓不到）。
改为**人工构造**一段两头明确的素材：

    第 1 段（0~T）  真实直播人声       ← 应该压
    第 2 段（T~2T）  纯静音（anullsrc） ← 应该恢复

这样"压得动"和"会恢复"两件事都能被直接量出来，而且不依赖素材本身有没有停顿。

再看 BGM 侧：**单独输出 BGM 轨**（bgm_raw / bgm_ducked），
两条轨都不含人声，电平差就是纯粹的压缩量。
⚠️ `[bgm]` 被 sidechaincompress 消费后不能再作输出，必须 `asplit` 分流。

运行：python stage0/probe_resolve/probe_real_duck.py [素材路径| -] [起始秒] [段长]
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sliceq import ffmpeg_tools  # noqa: E402

FFMPEG = ffmpeg_tools.find_ffmpeg()
WORK = Path(tempfile.mkdtemp(prefix="sliceq_realduck_"))

DEFAULT_SRC = r"C:\未明子录播\260925\【卡卡】[未明子]2026年09月25日21时录播.mp4"
CLIP_START, SEG = 300.0, 15.0     # 每段 15s（真人声 15s + 静音 15s）
SR = 48000


def run(args, *, loglevel="error", timeout=600):
    return subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", loglevel,
                           "-nostdin", "-y", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def level(audio: Path, start: float, dur: float) -> float | None:
    r = run(["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(audio),
             "-af", "volumedetect", "-f", "null", "-"], loglevel="info")
    for line in (r.stderr or "").splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].strip().split()[0])
            except Exception:  # noqa: BLE001
                return None
    return None


def stats_of(audio: Path) -> dict:
    """一次拿到 RMS 与峰值电平（决定 threshold 该往哪扫）。"""
    r = run(["-i", str(audio), "-af", "astats=metadata=1", "-f", "null", "-"],
            loglevel="info")
    out: dict = {}
    for line in (r.stderr or "").splitlines():
        for key, name in (("RMS level dB", "rms"), ("Peak level dB", "peak")):
            if key in line:
                try:
                    out[name] = float(line.split(":")[-1].strip())
                except Exception:  # noqa: BLE001
                    pass
    return out


def build_source(src: Path, out: Path, start: float, seg: float) -> None:
    """构造受控素材：前 seg 秒真实人声，后 seg 秒静音。"""
    r = run(["-ss", f"{start:.3f}", "-t", f"{seg:.3f}", "-i", str(src),
             "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=mono:d={seg:.3f}",
             "-filter_complex",
             f"[0:a]aresample={SR},aformat=channel_layouts=mono,asetpts=N/SR/TB[a];"
             f"[1:a]asetpts=N/SR/TB[b];[a][b]concat=n=2:v=0:a=1[o]",
             "-map", "[o]", "-c:a", "pcm_s16le", str(out)])
    if r.returncode != 0:
        raise SystemExit("构造素材失败：" + (r.stderr or "")[-400:])


def build_tracks(voice: Path, thr: float, ratio: float, level_sc: float,
                 length: float, bgm_vol: float = -15.0) -> tuple[Path, Path]:
    tag = f"{thr}_{ratio}_{level_sc}".replace(".", "p")
    raw, dug = WORK / f"raw_{tag}.wav", WORK / f"dug_{tag}.wav"
    sc = f":level_sc={level_sc:g}" if level_sc and abs(level_sc - 1.0) > 1e-6 else ""
    fc = (f"[0:a]aresample={SR},volume={bgm_vol:g}dB,atrim=0:{length:.3f},"
          f"asplit=2[bgm_out][bgm_sc];"
          f"[1:a]aresample={SR},atrim=0:{length:.3f}[voice];"
          f"[bgm_sc][voice]sidechaincompress=threshold={thr:g}:ratio={ratio:g}:"
          f"attack=20:release=300:makeup=1{sc}[ducked]")
    r = run(["-f", "lavfi", "-i", f"anoisesrc=c=pink:r={SR}:d={length:.3f}",
             "-i", str(voice), "-filter_complex", fc,
             "-map", "[bgm_out]", "-c:a", "pcm_s16le", str(raw),
             "-map", "[ducked]", "-c:a", "pcm_s16le", str(dug)])
    if r.returncode != 0:
        print("   渲染失败：" + (r.stderr or "").strip().splitlines()[-1][:160])
    return raw, dug


def main() -> int:
    raw_src = sys.argv[1] if len(sys.argv) > 1 else ""
    src = Path(raw_src) if raw_src and raw_src != "-" else Path(DEFAULT_SRC)
    start = float(sys.argv[2]) if len(sys.argv) > 2 else CLIP_START
    seg = float(sys.argv[3]) if len(sys.argv) > 3 else SEG
    if not src.exists():
        print(f"素材不存在：{src}")
        return 1

    print(f"素材：{src.name}")
    print(f"构造：{start:.0f}s 起取 {seg:.0f}s 真人声 + 后接 {seg:.0f}s 静音")
    print(f"workdir = {WORK}\n")

    voice = WORK / "voice.wav"
    build_source(src, voice, start, seg)
    total = seg * 2

    st = stats_of(voice)
    print(f"真实人声电平：RMS {st.get('rms')}dB / 峰值 {st.get('peak')}dB")
    lin = 10 ** (st["peak"] / 20) if st.get("peak") is not None else None
    if lin:
        print(f"  → 峰值线性幅度 ≈ {lin:.3f}"
              f"（threshold 应取在这个量级附近，而不是合成正弦上的 0.05）")
    print()

    # 扫 threshold：围绕真实人声的峰值量级
    combos = [(0.05, 8.0, 1.0), (0.10, 8.0, 1.0), (0.20, 8.0, 1.0),
              (0.30, 8.0, 1.0), (0.50, 8.0, 1.0), (0.20, 4.0, 1.0)]
    hdr = (f"{'thr':>6} {'ratio':>6} | {'人声段压低':>10} {'静音段压低':>10} "
           f"{'判定':>6}")
    print("=" * len(hdr))
    print(hdr)
    print("=" * len(hdr))

    rows = []
    for thr, ratio, sc in combos:
        raw, dug = build_tracks(voice, thr, ratio, sc, total)
        if not (raw.exists() and dug.exists()):
            continue
        sp_r, sp_d = level(raw, 0.3, seg - 0.6), level(dug, 0.3, seg - 0.6)
        si_r, si_d = level(raw, seg + 0.3, seg - 0.6), level(dug, seg + 0.3, seg - 0.6)
        if None in (sp_r, sp_d, si_r, si_d):
            print(f"{thr:>6} {ratio:>6} | 测量失败")
            continue
        d_speech, d_sil = sp_r - sp_d, si_r - si_d
        if d_speech >= 3.0 and abs(d_sil) <= 1.5:
            verdict = "★ 好"
        elif d_speech >= 2.0 and abs(d_sil) <= 3.0:
            verdict = "可用"
        elif d_speech < 1.5:
            verdict = "太弱"
        else:
            verdict = "过压" if abs(d_sil) > 3.0 else "可用"
        print(f"{thr:>6} {ratio:>6} | {d_speech:>9.2f}dB {d_sil:>9.2f}dB {verdict:>6}")

        rows.append((thr, ratio, sc, d_speech, d_sil, verdict))

    print("=" * len(hdr))
    print("\n判读：")
    print("  人声段压低 —— 人说话时 BGM 降了多少（3~10dB 是背景音乐的正常范围）")
    print("  静音段压低 —— 没人说话时 BGM 应**回到原音量**，接近 0 才对。")
    print("                这一列大 = 压缩器一直咬着不放，听感上 BGM 永远闷着。")

    good = [r for r in rows if r[5] == "★ 好"] or [r for r in rows if r[5] == "可用"]
    print()
    if good:
        best = max(good, key=lambda r: r[3] - abs(r[4]))
        print(f"推荐：threshold={best[0]:g} ratio={best[1]:g} level_sc={best[2]:g}")
        print(f"  → 人声段压低 {best[3]:.2f}dB，静音段仅变化 {best[4]:.2f}dB")
    else:
        print("⚠️ 没有一组达标 —— 合成信号上的标定确实不能用于真实素材")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
