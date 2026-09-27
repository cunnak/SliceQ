# -*- coding: utf-8 -*-
"""阶段 4-A 自测：成片增强（淡入淡出 / 配乐 ducking / 封面）。

判据原则（来自阶段 3 异常注入测试的教训）：
  ① 不只看"没报错"，要看**结果对不对**（像素/电平都是算出来的）；
  ② 期望值由程序算出，不手写估算；
  ③ 边界两侧都要覆盖。

运行：python stage0/probe_resolve/selftest_enhance.py
"""
from __future__ import annotations

import json
import math
import shutil
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

from sliceq import config, editor, enhance, ffmpeg_tools, subtitle_style  # noqa: E402

FFMPEG = ffmpeg_tools.find_ffmpeg()
FFPROBE = ffmpeg_tools.find_ffprobe()
WORK = Path(tempfile.mkdtemp(prefix="sliceq_e4_"))
VOICE_HZ = 300     # "人声"频率
BGM_HZ = 4000      # BGM 频率（与 VOICE_HZ 拉开，避免滤波泄漏污染测量）
BGM_W = 200        # 测 BGM 用的带通宽度（Hz）
PASS = 0
FAIL = 0
NOTES: list[str] = []


def ck(tag: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {tag}" + (f" :: {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {tag}" + (f" :: {detail}" if detail else ""))
    return cond


def run(args, *, cwd=None, timeout=180, loglevel="error"):
    """⚠️ 默认 `-loglevel error` 会**屏蔽 volumedetect 的输出** ——
    它走的是 `AV_LOG_INFO`，一压到 error 级就什么都读不到，
    表现为"测量函数返回 None"，看起来像断言失败，其实是量具坏了。
    凡是读日志的测量必须显式把 loglevel 放回 info。
    """
    return subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", loglevel,
                           "-nostdin", "-y", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          cwd=str(cwd) if cwd else None, timeout=timeout)


def dur_of(p: Path) -> float:
    r = subprocess.run([str(FFPROBE), "-v", "error", "-show_entries",
                        "format=duration", "-of", "json", str(p)],
                       capture_output=True, text=True, timeout=30)
    try:
        return round(float(json.loads(r.stdout)["format"]["duration"]), 3)
    except Exception:  # noqa: BLE001
        return -1.0


def size_of(p: Path) -> tuple[int, int]:
    ffprobe = FFPROBE
    r = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "json",
                        str(p)], capture_output=True, text=True, timeout=30)
    try:
        s = json.loads(r.stdout)["streams"][0]
        return int(s["width"]), int(s["height"])
    except Exception:  # noqa: BLE001
        return 0, 0


def brightness(p: Path, at: float) -> float | None:
    """取某一时刻的画面平均亮度（0~255）。

    用 `metadata=print:file=-` 把统计**写到 stdout**，
    避开 loglevel 的影响（这是上面那条注释踩过的坑）。
    """
    r = run(["-ss", f"{at:.3f}", "-i", str(p), "-frames:v", "1",
             "-vf", "scale=160:90,format=gray,signalstats,metadata=print:file=-",
             "-f", "null", "-"])
    for line in (r.stdout or "").splitlines():
        if "lavfi.signalstats.YAVG" in line and "=" in line:
            try:
                return float(line.split("=")[-1].strip())
            except Exception:  # noqa: BLE001
                return None
    return None


def rms_band(p: Path, at: float, dur: float, freq: int, width: int = 60) -> float | None:
    """测某个窄频带的平均电平（dB）。用来把 BGM 从混音里"隔离"出来。

    ⚠️ volumedetect 走 `AV_LOG_INFO`，必须 `loglevel=info` 才读得到。
    """
    r = run(["-ss", f"{at:.3f}", "-t", f"{dur:.3f}", "-i", str(p),
             "-af", f"bandpass=f={freq}:width_type=h:w={width},volumedetect",
             "-f", "null", "-"], loglevel="info")
    for line in (r.stderr or "").splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].strip().split()[0])
            except Exception:  # noqa: BLE001
                return None
    return None


# ─────────────────────────────────────────────────────────────
# 造素材
# ─────────────────────────────────────────────────────────────
def make_fixtures() -> dict[str, Path]:
    fx: dict[str, Path] = {}
    # 源：3 秒，含"人声"（0~1.5s 440Hz）+ 静音（1.5~3s），画面彩条
    #
    # ⚠️ 人声用 `aevalsrc` 显式给幅度 0.9，**不能用 `sine`**：
    #    ffmpeg 的 `sine` 滤镜默认振幅是 **0.125（-18dBFS）**，不是满刻度。
    #    实测对照：
    #        裸 sine         峰值 -18.06dB（线性 0.125）
    #        真实直播人声     峰值  -0.74dB（线性 0.92）  ← 差 7.4 倍
    #    拿 sine 当"人声"标定压缩器阈值，会得到一个**低 18dB 的假阈值**，
    #    搬到真实素材上就直接把 BGM 压死。这个坑真实发生过。
    src = WORK / "src.mp4"
    r = run(["-f", "lavfi", "-i", "smptebars=size=640x360:rate=25:duration=3",
             "-f", "lavfi", "-i",
             f"aevalsrc=exprs=0.9*sin(2*PI*{VOICE_HZ}*t):s=48000:d=1.5",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1.5",
             "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt",
             "yuv420p", "-c:a", "aac", "-shortest", str(src)])
    if r.returncode != 0:
        raise SystemExit("造素材失败：" + (r.stderr or "")[-400:])
    fx["src"] = src

    # 无声源（验证 has_audio_stream 与"只有 BGM"分支）
    nosnd = WORK / "nosnd.mp4"
    run(["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(nosnd)])
    fx["nosnd"] = nosnd

    # BGM 用 **4kHz**，与 300Hz 的"人声"拉开 13 倍频程。
    # ⚠️ 不能让人声与 BGM 靠太近：测量靠 `bandpass` 从混音里抠 BGM，
    #    而 bandpass 只有 ~12dB/倍频程的滚降 ——
    #    人声一旦到了正常音量（线性 0.95），它在 BGM 频带的**滤波泄漏
    #    会直接盖过 BGM 本身**（实测：BGM 真实电平 -28dB，
    #    被 440Hz 人声泄漏抬到 -18.8dB），ducking 的效果就测不出来了。
    #    第一版把 BGM 放在 220Hz、人声 440Hz（只差 1 个倍频程）就栽在这。
    # 也**不能用 `sine`** 造 BGM：它默认振幅只有 0.125（-18dBFS）。
    bgm_short = WORK / "bgm_short.wav"
    run(["-f", "lavfi", "-i",
         f"aevalsrc=exprs=0.9*sin(2*PI*{BGM_HZ}*t):s=48000:d=1",
         "-c:a", "pcm_s16le", str(bgm_short)])
    fx["bgm_short"] = bgm_short

    bgm = WORK / "bgm_ok.wav"
    run(["-f", "lavfi", "-i",
         f"aevalsrc=exprs=0.9*sin(2*PI*{BGM_HZ}*t):s=48000:d=5",
         "-c:a", "pcm_s16le", str(bgm)])
    fx["bgm"] = bgm
    return fx


# ─────────────────────────────────────────────────────────────
# 1. 纯逻辑
# ─────────────────────────────────────────────────────────────
def t_logic() -> None:
    print("\n[1] 纯逻辑（不跑 ffmpeg）")

    o = enhance.EnhanceOptions(enabled=True, fade_in=0.5, fade_out=0.8,
                               bgm="x", bgm_volume_db=-18.0, duck_ratio=8)
    o2 = enhance.EnhanceOptions.from_dict(o.to_dict())
    ck("EnhanceOptions 序列化往返", o2 == o, f"{o2.fade_in}/{o2.bgm_volume_db}/{o2.duck_ratio}")

    # 默认值必须"什么都不做"（防止误开增强）
    ck("默认 EnhanceOptions 是 no-op", enhance.EnhanceOptions().is_noop())
    ck("enabled 但无内容 仍是 no-op",
       enhance.EnhanceOptions(enabled=True).is_noop())
    ck("enabled+fade 不再是 no-op",
       not enhance.EnhanceOptions(enabled=True, fade_in=0.5).is_noop())

    o = enhance.EnhanceOptions(enabled=True, fade_in=0.5, fade_out=1.0)
    v = enhance.video_chain(10.0, o, "subtitles=a.ass")
    ck("video_chain 顺序：字幕在 fade 之前（淡出要盖住字幕）",
       v.index("subtitles") < v.index("fade=t=in"), v)
    ck("video_chain 淡出起点 = dur - fade_out",
       "st=9.000" in v, v)
    ck("video_chain 无字幕时也成立",
       enhance.video_chain(4.0, o).startswith("fade=t=in"), enhance.video_chain(4.0, o))
    ck("无任何项时返回空串", enhance.video_chain(4.0, enhance.EnhanceOptions()) == "")

    o = enhance.EnhanceOptions(enabled=True, bgm="b")
    g, lab = enhance.audio_filter_complex(10.0, o, has_voice=True, bgm_input=1)
    ck("有原声+BGM：走 sidechaincompress", "sidechaincompress" in g and "amix" in g)
    ck("有原声+BGM：输出标签正确", lab == "[aout]", lab)
    ck("duck 关闭时不含 sidechaincompress",
       "sidechaincompress" not in enhance.audio_filter_complex(
           10.0, enhance.EnhanceOptions(enabled=True, bgm="b", duck=False),
           has_voice=True, bgm_input=1)[0])
    g2, lab2 = enhance.audio_filter_complex(10.0, o, has_voice=False, bgm_input=1)
    ck("无原声时直接输出 BGM（不做 ducking）",
       "sidechaincompress" not in g2 and lab2 == "[bgm]", lab2)
    g3, lab3 = enhance.audio_filter_complex(10.0, o, has_voice=True, bgm_input=None)
    ck("无 BGM 时只做 afade", "sidechaincompress" not in g3 and lab3 == "[voice]", lab3)
    ck("BGM 被 atrim 到片段时长", "atrim=0:10.000" in g,
       [x for x in g.split(";") if "atrim" in x][0] if "atrim" in g else g)

    # 换行 / 字号自适应
    st = enhance.CoverStyle(size=100)
    f1, w1 = enhance.fit_cover_size("短标题", st)
    ck("短标题用原始字号", f1 == 100, f"size={f1}")
    long_title = "这是一条特别特别长的标题" * 3
    f2, w2 = enhance.fit_cover_size(long_title, st)
    ck("超长标题自动降字号", f2 < 100, f"size={f2}")
    ck("超长标题行数受控（<=4）", w2.count(r"\N") + 1 <= 4,
       f"lines={w2.count(chr(92)+'N') + 1}")
    ck("标题文本无丢失（去换行后等长）",
       w2.replace(r"\N", "") == long_title, f"{len(w2.replace(chr(92)+'N',''))} vs {len(long_title)}")

    ass = enhance.build_cover_ass("测试", enhance.CoverStyle())
    ck("封面 ASS 含必要段", all(s in ass for s in
                            ("[Script Info]", "[V4+ Styles]", "[Events]",
                             "PlayResX: 1280", "PlayResY: 720")), "")
    ck("封面 ASS 的 PlayRes 与封面尺寸一致",
       "PlayResX: 1280" in ass and "PlayResY: 720" in ass)
    # 颜色必须 ABGR：纯红 #FF0000 → &H000000FF
    ass_r = enhance.build_cover_ass("x", enhance.CoverStyle(color="#FF0000"))
    ck("封面颜色走 rgb_to_ass（ABGR，红蓝对调）",
       "&H000000FF" in ass_r, [l for l in ass_r.splitlines() if l.startswith("Style")][0][:90])

    fnt = enhance.pick_cover_font("绝对不存在的字体XYZ")
    ck("不可用字体被换成可用字体", bool(fnt) and fnt != "绝对不存在的字体XYZ", fnt)
    ck("挑出的字体确实可用", subtitle_style.font_available(fnt), fnt)

    ck("CoverStyle 序列化往返",
       enhance.CoverStyle.from_dict(
           enhance.CoverStyle(size=77, position="top").to_dict()).size == 77)


def t_bgm(fx: dict[str, Path]) -> None:
    print("\n[2] BGM 目录与解析")
    d = enhance.bgm_dir()
    note = enhance.ensure_bgm_readme()
    ck("BGM 目录已创建", d.is_dir(), str(d))
    ck("说明文件已写出", note.exists(), note.name)

    # 把测试 BGM 临时放进真实 BGM 目录，测完删掉
    placed = d / "_selftest_tone.wav"
    shutil.copy2(fx["bgm"], placed)
    try:
        names = [x["name"] for x in enhance.list_bgm()]
        ck("list_bgm 能列出文件", "_selftest_tone" in names, str(names))
        ck("resolve_bgm 用文件名", enhance.resolve_bgm("_selftest_tone") == placed)
        ck("resolve_bgm 用带扩展名", enhance.resolve_bgm("_selftest_tone.wav") == placed)
        ck("resolve_bgm 用绝对路径", enhance.resolve_bgm(fx["bgm"]) == fx["bgm"])
        ck("resolve_bgm 找不到返回 None",
           enhance.resolve_bgm("绝不存在的曲子") is None)
        ck("resolve_bgm 空输入返回 None",
           enhance.resolve_bgm("") is None and enhance.resolve_bgm(None) is None)
    finally:
        placed.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────
# 3. 真实执行
# ─────────────────────────────────────────────────────────────
def t_fade(fx: dict[str, Path]) -> None:
    print("\n[3] 淡入淡出（真实执行）")
    src = fx["src"]
    base = WORK / "fade_base.mp4"
    run(["-ss", "0", "-t", "3", "-i", str(src), *editor.EXACT_VIDEO_ARGS,
         *editor.EXACT_AUDIO_ARGS, str(base)])
    b0 = brightness(base, 0.05)
    b1 = brightness(base, 1.5)
    ck("基线：片头不黑（证明后面测到的黑是淡入造成的）",
       b0 is not None and b0 > 40, f"YAVG={b0}")

    opt = enhance.EnhanceOptions(enabled=True, fade_in=1.0, fade_out=1.0)
    out = WORK / "fade_out.mp4"
    argv, cwd = editor.build_cut_cmd(src, 0.0, 3.0, out, mode="exact", enhance=opt)
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=cwd, timeout=180)
    ck("带淡入淡出导出成功", r.returncode == 0 and out.exists(),
       (r.stderr or "")[-200:])
    if out.exists():
        d = dur_of(out)
        ck("时长未被改变（fade 不改时长）", abs(d - 3.0) < 0.15, f"{d}s")
        f_in = brightness(out, 0.02)
        mid = brightness(out, 1.5)
        # ⚠️ 不能取 2.97s：25fps 下最后一帧在 2.96s，`-ss` 落到帧后就没有输出了，
        #    测量函数会返回 None —— 看起来像"淡出没生效"，其实是取帧越界。
        f_out = brightness(out, 2.88)
        ck("片头确实变暗", f_in is not None and f_in < (b0 or 100) * 0.6,
           f"淡入首帧 {f_in} vs 基线 {b0}")
        ck("中段亮度保持", mid is not None and mid > 40, f"YAVG={mid}")
        ck("片尾确实变暗", f_out is not None and f_out < (mid or 100) * 0.6,
           f"淡出末帧 {f_out} vs 中段 {mid}")

    # 快速模式 + 增强 → 必须升档为 exact（-c copy 加不了滤镜）
    argv2, _ = editor.build_cut_cmd(src, 0.0, 1.0, WORK / "x.mp4", mode="fast",
                                    enhance=opt)
    ck("fast 模式遇增强自动升档（argv 不含 -c copy）",
       "-c" not in argv2 or "copy" not in argv2, " ".join(argv2[:8]))


def t_bgm_mix(fx: dict[str, Path]) -> None:
    print("\n[4] 配乐与 ducking（真实执行，用频带分离测量）")
    src = fx["src"]
    # ⚠️ 测量时必须把 `bgm_fade` 关掉。
    #    BGM 默认自带 1s 淡入淡出（避免切口突兀），但那会让
    #    「人声段」正好落在 BGM 淡入区、「静音段」落在淡出区 ——
    #    两个窗口的 BGM 基准电平本来就不同，测出来的"压低量"是假的。
    #    （第一版就是这么测的：对照组反而出现 -2.1dB 的"负压低"。）
    common = dict(bgm=str(fx["bgm"]), bgm_volume_db=-10.0, bgm_fade=0.0)
    # 用**默认档位**（而不是硬编码参数）—— 要测的正是"装上就能用"的配置。
    # 人声幅度已按真实素材对齐（aevalsrc 0.9），所以这里的期望值
    # 可以直接照真实标定结果来（标准档 ≈5dB），不需要放宽。
    opt = enhance.apply_duck_level(
        enhance.EnhanceOptions(enabled=True, **common), "normal")
    ck("档位写入参数正确",
       opt.duck and opt.duck_threshold == 0.2 and opt.duck_ratio == 8.0,
       f"thr={opt.duck_threshold} ratio={opt.duck_ratio}")
    ck("档位能被反查回来", enhance.duck_level_of(opt) == "normal",
       enhance.duck_level_of(opt))
    out = WORK / "duck.mp4"
    argv, cwd = editor.build_cut_cmd(src, 0.0, 3.0, out, mode="exact", enhance=opt)
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=cwd, timeout=180)
    ck("配乐导出成功", r.returncode == 0 and out.exists(), (r.stderr or "")[-250:])
    if not out.exists():
        return

    ck("成片时长正确", abs(dur_of(out) - 3.0) < 0.2, f"{dur_of(out)}s")
    # BGM = 220Hz；人声 = 440Hz。用窄带滤波把 BGM 单独抠出来测。
    loud = rms_band(out, 0.3, 1.0, BGM_HZ, BGM_W)     # 人声在响（测 BGM 频带）
    quiet = rms_band(out, 1.8, 0.9, BGM_HZ, BGM_W)    # 静音段
    if loud is None or quiet is None:
        ck("BGM 频带测量成功", False, f"loud={loud} quiet={quiet}")
        return
    # 单独测"不加 ducking"的对照（同一素材、同一窗口，只差 ducking 一个变量）
    opt2 = enhance.EnhanceOptions(enabled=True, duck=False, **common)
    out2 = WORK / "noduck.mp4"
    argv2, cwd2 = editor.build_cut_cmd(src, 0.0, 3.0, out2, mode="exact", enhance=opt2)
    subprocess.run(argv2, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", cwd=cwd2, timeout=180)
    l2 = rms_band(out2, 0.3, 1.0, BGM_HZ, BGM_W)
    q2 = rms_band(out2, 1.8, 0.9, BGM_HZ, BGM_W)

    ck("BGM 确实混进了成片", loud > -60, f"人声段 BGM 电平 {loud}dB")
    # ⚠️ 判据必须**只让 ducking 一个变量变**：比较"同一窗口（人声段）"
    #    在有/无 ducking 下的电平。用"人声段 vs 静音段"比会被污染 ——
    #    人声的残余频谱会漏进 BGM 的测量频带，让静音段显得更轻
    #    （实测漏进 1.2dB，足以把"没压"误判成"压了"）。
    drop = (l2 - loud) if l2 is not None else None
    keeps = abs(quiet - q2) if (q2 is not None) else None
    ck("静音段不受 ducking 影响（人声走了 BGM 要恢复）",
       keeps is not None and keeps < 1.0, f"静音段差 {keeps}dB")
    ck("ducking 生效（人声段 BGM 被压低）",
       drop is not None and drop >= 4.5,
       f"压低 {drop:.2f}dB（无duck {l2} → 有duck {loud}）")
    NOTES.append(f"ducking 实测：thr={opt.duck_threshold:g} ratio={opt.duck_ratio:g} "
                 f"level_sc={opt.duck_level_sc:g} ⇒ 人声段压低 {drop:.2f}dB，"
                 f"静音段变化 {keeps:.2f}dB")

    # 短 BGM（1s）配 3s 片段 → 必须循环填充，不能后 2 秒没声音
    opt3 = enhance.EnhanceOptions(enabled=True, duck=False,
                                  bgm=str(fx["bgm_short"]),
                                  bgm_volume_db=-10.0, bgm_fade=0.0)
    out3 = WORK / "loop.mp4"
    argv3, cwd3 = editor.build_cut_cmd(src, 0.0, 3.0, out3, mode="exact", enhance=opt3)
    subprocess.run(argv3, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", cwd=cwd3, timeout=180)
    late = rms_band(out3, 2.4, 0.4, BGM_HZ, BGM_W) if out3.exists() else None
    ck("短 BGM 循环填充（末尾仍有声）",
       late is not None and late > -60, f"2.4s 处 BGM 电平 {late}dB")
    ck("循环后时长仍正确", out3.exists() and abs(dur_of(out3) - 3.0) < 0.2,
       f"{dur_of(out3) if out3.exists() else 'N/A'}s")


def t_no_audio(fx: dict[str, Path]) -> None:
    print("\n[5] 无声源 + BGM（边界）")
    ck("has_audio_stream 对有音轨源判 True",
       ffmpeg_tools.has_audio_stream(fx["src"]))
    ck("has_audio_stream 对无声源判 False",
       not ffmpeg_tools.has_audio_stream(fx["nosnd"]))

    opt = enhance.EnhanceOptions(enabled=True, bgm=str(fx["bgm"]),
                                 bgm_volume_db=-10.0, duck=True)
    out = WORK / "nosnd_out.mp4"
    argv, cwd = editor.build_cut_cmd(fx["nosnd"], 0.0, 3.0, out, mode="exact",
                                     enhance=opt)
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=cwd, timeout=180)
    ck("无声源配乐不报错", r.returncode == 0 and out.exists(), (r.stderr or "")[-250:])
    if out.exists():
        lvl = rms_band(out, 1.0, 1.0, BGM_HZ, BGM_W)
        ck("无声源成片里 BGM 存在", lvl is not None and lvl > -60, f"{lvl}dB")

    # 无声源 + 无 BGM + 只有 fade：不该去引用 [0:a]
    opt2 = enhance.EnhanceOptions(enabled=True, fade_in=0.5)
    out2 = WORK / "nosnd_fade.mp4"
    argv2, cwd2 = editor.build_cut_cmd(fx["nosnd"], 0.0, 2.0, out2, mode="exact",
                                       enhance=opt2)
    r2 = subprocess.run(argv2, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", cwd=cwd2, timeout=180)
    ck("无声源 + 仅 fade 不报错（不回退到 -an 之外）",
       r2.returncode == 0 and out2.exists(), (r2.stderr or "")[-250:])


def t_cover(fx: dict[str, Path]) -> None:
    print("\n[6] 封面（真实执行）")
    out = WORK / "cover.jpg"
    info = enhance.make_cover(fx["src"], 0.0, 3.0, "扫码过来叫我扫一下我付钱",
                              out, style=enhance.CoverStyle(size=92))
    ck("封面生成成功", info["ok"] and out.exists(), str(info.get("note") or ""))
    if out.exists():
        w, h = size_of(out)
        ck("封面尺寸 = 1280x720", (w, h) == (1280, 720), f"{w}x{h}")
        ck("封面文件非空", out.stat().st_size > 8000, f"{out.stat().st_size} B")
        ck("封面用的是可用字体",
           subtitle_style.font_available(info["font"]), info["font"])

    # 字体不可用时必须**告知**（libass 自己会静默换字体）
    info2 = enhance.make_cover(fx["src"], 0.0, 3.0, "标题", WORK / "cover2.jpg",
                               style=enhance.CoverStyle(font="完全不存在的字体QQQ"))
    ck("字体不可用时有明确提示", bool(info2["note"]), info2["note"][:70])

    # 超长标题不能崩
    try:
        info3 = enhance.make_cover(fx["src"], 0.0, 3.0, "很长的标题" * 12,
                                   WORK / "cover3.jpg")
        ck("超长标题仍能生成封面", info3["ok"])
    except Exception as exc:  # noqa: BLE001
        ck("超长标题仍能生成封面", False, f"{type(exc).__name__}: {exc}")

    # 找不到 BGM 时 export_one 必须留提示（不能静默当没配）
    item = editor.export_one(fx["src"], {"id": 1, "start": 0.0, "end": 2.0},
                             out_dir=WORK / "exp", index=1, stem="01_t",
                             mode="exact",
                             enhance=enhance.EnhanceOptions(enabled=True, bgm="不存在的曲子"))
    ck("BGM 缺失时给出可读提示",
       any("找不到 BGM" in n for n in item.notes), str(item.notes))
    ck("BGM 缺失不阻断成片导出", item.ok, item.error)


def t_integration(fx: dict[str, Path]) -> None:
    print("\n[7] 与 editor 集成")
    out_dir = WORK / "batch"
    res = editor.export_clips(
        fx["src"],
        [{"id": 1, "start": 0.0, "end": 2.0, "title_hint": "第一条"},
         {"id": 2, "start": 1.0, "end": 3.0, "title_hint": "第二条"}],
        out_base=out_dir, task_name="自测", mode="exact",
        enhance=enhance.EnhanceOptions(enabled=True, fade_in=0.3, fade_out=0.3,
                                       bgm=str(fx["bgm"]), bgm_volume_db=-12.0),
        make_covers=True)
    ck("批量导出成功 2 条", res.ok_count == 2, res.summary())
    ck("增强摘要写入结果", "配乐" in res.enhance_summary, res.enhance_summary)
    ck("mode 被正确记录为 exact", res.mode == "exact", res.mode)
    covers = [i.cover for i in res.items if i.cover]
    ck("封面逐条产出", len(covers) == 2, f"{len(covers)} 张")
    man = res.out_dir / "导出清单.txt"
    ck("导出清单存在", man.exists())
    if man.exists():
        txt = man.read_text(encoding="utf-8")
        ck("清单含增强信息", "增强" in txt and "配乐" in txt,
           [l for l in txt.splitlines() if "增强" in l])
        ck("清单含封面文件名", "封面：" in txt)
    ck("_work 中间目录已清理", not (res.out_dir / "_work").exists())

    # 不启用增强时，行为必须与阶段 3 一致（回归）
    res2 = editor.export_clips(
        fx["src"], [{"id": 3, "start": 0.0, "end": 1.5}],
        out_base=WORK / "batch2", task_name="回归", mode="fast")
    ck("不启用增强时快速模式仍走 -c copy（未回归）",
       res2.mode == "fast" and res2.ok_count == 1, res2.summary())


def main() -> int:
    print(f"workdir = {WORK}")
    print(f"ffmpeg  = {FFMPEG}")
    fx = make_fixtures()

    t_logic()
    t_bgm(fx)
    t_fade(fx)
    t_bgm_mix(fx)
    t_no_audio(fx)
    t_cover(fx)
    t_integration(fx)

    print("\n" + "=" * 66)
    print(f"通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    for n in NOTES:
        print("实测：" + n)
    print("=" * 66)
    print(f"产物目录（可人工检查）：{WORK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
