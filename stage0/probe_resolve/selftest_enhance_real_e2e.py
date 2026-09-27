# -*- coding: utf-8 -*-
"""阶段 4-A 端到端：真实直播素材 + 产品代码路径，跑完整增强链路。

前面几个自测用的是合成素材（验证链路通不通）。
**合成素材测不出"效果好不好"** —— 这条在阶段 2 已经吃过一次亏。
所以这里用真实直播录像走一遍 `editor.export_clips`，对比"无增强"与
"有增强"两个成片，把效果量出来：

    · 淡入淡出 → 测首尾帧亮度是否真的掉下来（基线对照，排除"本来就黑"）
    · 配乐     → 测音轨整体电平是否抬升（BGM 混进去了）
    · 封面     → 测尺寸与文件有效性
    · 清单     → 测增强信息有没有写进去

运行：python stage0/probe_resolve/selftest_enhance_real_e2e.py [素材] [起始秒] [时长]
"""
from __future__ import annotations

import json
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

from sliceq import asr, editor, enhance, ffmpeg_tools  # noqa: E402

FFMPEG = ffmpeg_tools.find_ffmpeg()
FFPROBE = ffmpeg_tools.find_ffprobe()
WORK = Path(tempfile.mkdtemp(prefix="sliceq_e2e4_"))

DEFAULT_SRC = r"C:\未明子录播\260925\【卡卡】[未明子]2026年09月25日21时录播.mp4"
START, LEN = 300.0, 20.0

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


def size_of(p: Path) -> tuple[int, int]:
    r = subprocess.run([str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "json",
                        str(p)], capture_output=True, text=True, timeout=60)
    try:
        s = json.loads(r.stdout)["streams"][0]
        return int(s["width"]), int(s["height"])
    except Exception:  # noqa: BLE001
        return 0, 0


def brightness(p: Path, at: float) -> float | None:
    r = run(["-ss", f"{at:.3f}", "-i", str(p), "-frames:v", "1",
             "-vf", "scale=160:90,format=gray,signalstats,metadata=print:file=-",
             "-f", "null", "-"])
    for line in (r.stdout or "").splitlines():
        if "lavfi.signalstats.YAVG" in line:
            try:
                return float(line.split("=")[-1].strip())
            except Exception:  # noqa: BLE001
                return None
    return None


def mean_volume(p: Path) -> float | None:
    r = run(["-i", str(p), "-af", "volumedetect", "-f", "null", "-"],
            loglevel="info")
    for line in (r.stderr or "").splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].strip().split()[0])
            except Exception:  # noqa: BLE001
                return None
    return None


def band_level(p: Path, freq: int, width: int = 100) -> float | None:
    """测某个窄带的电平。

    ⚠️ 不能用整段 mean_volume 判断"BGM 有没有加进去"：
       人声（-11dB 左右）主导了总电平，BGM 叠上去只抬高 0.1dB，
       判据会误判成"没加上"。要测的是 **BGM 的特征频率**。
    """
    r = run(["-i", str(p), "-af",
             f"bandpass=f={freq}:width_type=h:w={width},volumedetect",
             "-f", "null", "-"], loglevel="info")
    for line in (r.stderr or "").splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].strip().split()[0])
            except Exception:  # noqa: BLE001
                return None
    return None


def make_fake_bgm(path: Path, seconds: float = 8.0) -> None:
    """造一段"像音乐"的测试 BGM（A 大三和弦 + 缓起缓落）。

    ⚠️ 只用它做端到端连通性验证，**不放进用户的 BGM 目录** ——
    真正的配乐要用户自备（版权问题，见 enhance.BGM_README）。
    """
    expr = ("0.35*sin(2*PI*220*t)+0.28*sin(2*PI*277.18*t)"
            "+0.22*sin(2*PI*329.63*t)+0.12*sin(2*PI*880*t)")
    r = run(["-f", "lavfi", "-i",
             f"aevalsrc=exprs={expr}:s=48000:d={seconds:.2f}",
             "-af", "afade=t=in:st=0:d=1,afade=t=out:st="
                    f"{seconds - 1:.2f}:d=1",
             "-c:a", "pcm_s16le", str(path)])
    if r.returncode != 0:
        raise SystemExit("造 BGM 失败：" + (r.stderr or "")[-300:])


def main() -> int:
    raw_src = sys.argv[1] if len(sys.argv) > 1 else ""
    src = Path(raw_src) if raw_src and raw_src != "-" else Path(DEFAULT_SRC)
    start = float(sys.argv[2]) if len(sys.argv) > 2 else START
    length = float(sys.argv[3]) if len(sys.argv) > 3 else LEN
    if not src.exists():
        print(f"素材不存在：{src}")
        return 1

    print(f"素材：{src.name}")
    print(f"片段：{start:.0f}s ~ {start + length:.0f}s（{length:.0f} 秒）")
    print(f"workdir = {WORK}\n")

    bgm = WORK / "test_bgm.wav"
    make_fake_bgm(bgm)
    clips = [{"id": 1, "start": start, "end": start + length,
              "title_hint": "扫码支付的荒唐事"}]
    # 转录分段（覆盖本片段区间）—— 没有它就不可能有 SRT 交付物
    segs = [
        asr.Segment(start_ms=int((start + 1) * 1000),
                    end_ms=int((start + 5) * 1000),
                    text="我跟你说这个事情就很离谱"),
        asr.Segment(start_ms=int((start + 6) * 1000),
                    end_ms=int((start + 11) * 1000),
                    text="你拿个二维码过来叫我扫一下我付钱"),
        asr.Segment(start_ms=int((start + 12) * 1000),
                    end_ms=int((start + 18) * 1000),
                    text="结果扫完了以后他说没收到"),
    ]

    # ── 基线：不增强 ──
    print("[1] 基线导出（无增强，不烧字幕 —— 但要拿到 SRT 交付物）")
    base = editor.export_clips(
        src, clips, out_base=WORK / "base", task_name="基线", mode="exact",
        burn_subtitles=False, segments=segs, use_llm_split=False)
    ck("基线导出成功", base.ok_count == 1, base.summary())
    b_video = Path(base.items[0].video)

    # ── 增强：淡入淡出 + 配乐 + 封面 ──
    print("\n[2] 增强导出（淡入淡出 0.8s + 配乐 + 封面）")
    # BGM 音量故意取 -3dB（比默认的 -15dB 响得多）：这里要验证的是
    # "配乐链路通不通"，不是"默认音量好不好"。
    # 默认 -15dB 在真实人声（-11dB）面前只让 880Hz 频带抬 1.1dB，
    # 够真实但不足以做成一条稳定的断言。
    opt = enhance.apply_duck_level(
        enhance.EnhanceOptions(enabled=True, fade_in=0.8, fade_out=0.8,
                               bgm=str(bgm), bgm_volume_db=-3.0),
        "normal")
    en = editor.export_clips(
        src, clips, out_base=WORK / "enh", task_name="增强", mode="exact",
        burn_subtitles=True, segments=segs, use_llm_split=False,
        enhance=opt, make_covers=True)
    ck("增强导出成功", en.ok_count == 1, en.summary())
    e_video = Path(en.items[0].video)

    print("\n[3] 时长必须一致（淡入淡出与配乐都不改时长）")
    db, de = dur_of(b_video), dur_of(e_video)
    ck("两条时长接近", abs(db - de) < 0.2, f"基线 {db}s / 增强 {de}s")
    ck("时长≈设定值", abs(de - length) < 0.3, f"{de}s vs {length}s")
    NOTES.append(f"真实素材 {length:.0f}s：基线 {db}s / 增强 {de}s")

    print("\n[4] 淡入淡出确实作用在画面上")
    b_head, b_mid, b_tail = (brightness(b_video, 0.05),
                             brightness(b_video, length / 2),
                             brightness(b_video, length - 0.3))
    e_head, e_mid, e_tail = (brightness(e_video, 0.05),
                             brightness(e_video, length / 2),
                             brightness(e_video, length - 0.3))
    ck("基线与增强的中段亮度接近（同一素材同一时刻）",
       b_mid is not None and e_mid is not None and abs(b_mid - e_mid) < 12,
       f"基线 {b_mid} / 增强 {e_mid}")
    ck("片头被压暗", e_head is not None and b_head is not None
       and e_head < b_head * 0.75, f"增强 {e_head} < 基线 {b_head}")
    ck("片尾被压暗", e_tail is not None and b_tail is not None
       and e_tail < b_tail * 0.75, f"增强 {e_tail} < 基线 {b_tail}")
    ck("中段没有被误伤", e_mid is not None and e_mid > 20, f"YAVG={e_mid}")
    NOTES.append(f"亮度：片头 {b_head}→{e_head}，片尾 {b_tail}→{e_tail}")

    print("\n[5] 配乐确实混进了音轨")
    vb, ve = mean_volume(b_video), mean_volume(e_video)
    ck("两条音轨都没丢（不是静音）",
       vb is not None and ve is not None and vb > -60 and ve > -60,
       f"基线 {vb}dB / 增强 {ve}dB")
    # ⚠️ 不能拿整段总电平当判据：人声主导，BGM 叠上去只抬 0.1dB。
    #    测 BGM 独有的特征频率（测试 BGM 里 880Hz 是明显的分量）。
    b_pk, e_pk = band_level(b_video, 880), band_level(e_video, 880)
    ck("BGM 特征频率处能量明显上升",
       b_pk is not None and e_pk is not None and e_pk > b_pk + 4.0,
       f"880Hz：基线 {b_pk}dB → 增强 {e_pk}dB（+{e_pk - b_pk:.2f}dB）")
    NOTES.append(f"音轨电平：{vb}dB → {ve}dB；880Hz 频带 {b_pk} → {e_pk} dB")

    print("\n[6] 封面")
    item = en.items[0]
    ck("封面文件已产出", bool(item.cover) and Path(item.cover).exists(),
       item.cover or "无")
    if item.cover and Path(item.cover).exists():
        w, h = size_of(Path(item.cover))
        ck("封面 1280x720", (w, h) == (1280, 720), f"{w}x{h}")
        ck("封面非空", Path(item.cover).stat().st_size > 8000,
           f"{Path(item.cover).stat().st_size} B")

    print("\n[7] 导出清单要把增强写清楚")
    man = en.out_dir / "导出清单.txt"
    ck("清单存在", man.exists())
    if man.exists():
        txt = man.read_text(encoding="utf-8")
        ck("含增强摘要", "增强" in txt and "配乐" in txt,
           [x.strip() for x in txt.splitlines() if "增强" in x][:1])
        ck("含封面文件名", "封面" in txt)
        ck("含淡入淡出信息", "淡入" in txt, "")
    ck("SRT 交付物仍在", bool(item.srt), item.srt or "无")
    ck("烧了字幕时 ASS 也参与了渲染（cues 数 > 0）", item.cues > 0,
       f"{item.cues} 条")

    print("\n[8] 界面的承诺必须兑现：不烧字幕也要给 SRT")
    b_item = base.items[0]
    ck("不烧字幕时仍然产出 .srt 文件",
       bool(b_item.srt) and Path(b_item.srt).exists(),
       b_item.srt or "无（界面却承诺会给 —— 这就是言行不一）")
    if b_item.srt and Path(b_item.srt).exists():
        body = Path(b_item.srt).read_text(encoding="utf-8", errors="replace")
        ck("SRT 内容非空且有条目", "-->" in body, f"{len(body)} 字符")
    ck("不烧字幕时成片里没有字幕痕迹（subtitles_burned=False）",
       not base.subtitles_burned)

    print("\n" + "=" * 68)
    print(f"通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    for n in NOTES:
        print("实测：" + n)
    print(f"产物：{WORK}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
