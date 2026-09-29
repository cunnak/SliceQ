# -*- coding: utf-8 -*-
r"""首次启动流程自测（干净环境模拟）。

═══════════════════════════════════════════════════════════════
为什么需要这个测试
═══════════════════════════════════════════════════════════════
阶段 1 的自测是在**这台已经装好 ffmpeg 的机器**上跑的，
所以"下载 ffmpeg / 下载模型"这两条分支**一次都没执行过**。

而 SliceQ 的整个价值前提是 **BYOK + 单 exe 分发给别人**：
那些人的机器上没有 ffmpeg，也没配过 hf-mirror。
**分发故事里最脆的一环，恰恰是从未跑过的那一环。**

本脚本模拟"干净机器"：
  1. 清空 PATH 里所有含 ffmpeg 的目录 → 让 shutil.which() 找不到
  2. 清掉手动指定的 ffmpeg 路径
  3. 此时探测应该报"缺失"
  4. 走自动下载 → 解压 → **强制验证 whisper 滤镜**
  5. 再次探测应报"就绪"

用法：
    python sliceq/selftest_first_launch.py            # 全流程
    python sliceq/selftest_first_launch.py --no-dl    # 只验证"缺失检测"，不真下载
    python sliceq/selftest_first_launch.py --model ggml-tiny.bin
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# ⚠️ 本文件是**当脚本直接跑**的（`python sliceq/selftest_first_launch.py`），
#    `__package__` 为 None ⇒ 只能用**绝对导入**，且必须在 sys.path 补好之后。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS, FAIL, SKIPPED = [], [], []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    return cond


def make_env_clean() -> list[str]:
    """从 PATH 里摘掉所有含 ffmpeg 的目录，返回被摘掉的目录列表。"""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    kept, removed = [], []
    for p in parts:
        if not p:
            continue
        try:
            names = {f.lower() for f in os.listdir(p)}
        except Exception:
            names = set()
        if any(n.startswith("ffmpeg") or n.startswith("ffprobe")
               for n in names):
            removed.append(p)
        else:
            kept.append(p)
    os.environ["PATH"] = os.pathsep.join(kept)
    return removed


def fmt_mb(n: int) -> str:
    return f"{n / 1048576:.1f}MB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-dl", action="store_true",
                    help="只验证缺失检测，不真的下载")
    ap.add_argument("--model", default="", help="要下载的 whisper 模型名")
    args = ap.parse_args()

    print("=" * 72)
    print("SliceQ 首次启动流程自测（干净环境模拟）")
    print("=" * 72)

    from sliceq import config, ffmpeg_tools, asr_models, settings

    # ── 0. 记录原始状态 ────────────────────────────────
    # ⚠️ **先隔离设置的落盘位置**。下面要 `settings.set("ffmpeg_path", "")`
    #    来模拟"用户没手动指定过"—— 如果写真实文件，就会**清掉用户
    #    手动指定的 ffmpeg 路径**。（同类问题：`selftest_stage1` 曾覆盖
    #    用户的 API key、并把凭据文件整个删掉。）
    #
    #    自测绝不该写生产数据：把 SETTINGS_PATH 指到临时文件即可根治，
    #    比"测完再恢复"更可靠（中途崩了也不留污染）。
    from sliceq._testing import isolate_data_root

    _tmp = isolate_data_root(prefix="sliceq_fl_")

    print("\n[0] 环境现状")
    print(f"       应用目录: {config.APP_ROOT}")
    print(f"       自带 ffmpeg: {ffmpeg_tools.app_ffmpeg_path() or '（无）'}")
    print(f"       模型目录内容: {asr_models.list_installed() or '（空）'}")
    print(f"       设置已隔离到: {config.SETTINGS_PATH}")
    settings.set("ffmpeg_path", "")          # 清掉手动指定（只作用于临时文件）
    print("       已清除手动指定的 ffmpeg 路径（未触碰用户真实设置）")

    # ── 1. 探测：应为"缺失" ────────────────────────────
    print("\n[1] 屏蔽系统 ffmpeg 后探测")
    removed = make_env_clean()
    print(f"       已从 PATH 摘掉 {len(removed)} 个目录：")
    for p in removed:
        print(f"         - {p}")
    if not removed:
        print("       ⚠️ 没找到含 ffmpeg 的 PATH 目录 —— 本机本来就干净")

    import importlib
    importlib.reload(ffmpeg_tools)           # 让 which() 重读 PATH

    info = ffmpeg_tools.probe_environment()
    print(f"       探测结果: {info.get('message')}")
    # ⚠️ 这一项要验的是「**全新用户**第一次打开时，程序能不能正确地说
    #    "还没装 ffmpeg"」。本机早就下载过 ffmpeg（在应用目录里），
    #    摘 PATH 摘不掉它 —— 于是探测必然是"就绪"，这一项**必然假失败**。
    #
    #    它不是产品缺陷。所以检测到这种情况就**显式跳过并说明原因**，
    #    而不是留一个红叉让后来的人去查（实测已经骗过我一次）。
    bundled = config.BIN_DIR / "ffmpeg.exe"
    if bundled.exists():
        SKIPPED.append("干净环境下正确报告 FFmpeg 缺失")
        print(f"  [跳过] 干净环境下正确报告 FFmpeg 缺失 — "
              f"{bundled} 已存在，本机不是干净环境")
        print("          （要验证它请先移走应用目录里的 ffmpeg）")
    else:
        check("干净环境下正确报告 FFmpeg 缺失",
              not info.get("ok") and info.get("reason") == "missing",
              f"reason={info.get('reason')}")

    if args.no_dl:
        print("\n（--no-dl：跳过下载，结束）")
        return _summary()

    # ── 2. 下载 ffmpeg ─────────────────────────────────
    print("\n[2] 自动下载 FFmpeg（约 160MB，可能要几分钟）")
    t0 = time.time()
    last = [0.0]

    def prog(done: int, total: int, desc: str) -> None:
        now = time.time()
        if now - last[0] < 3:                # 每 3 秒报一次，别刷屏
            return
        last[0] = now
        if total:
            print(f"       {desc}  {fmt_mb(done)}/{fmt_mb(total)}"
                  f"  ({done * 100 // total}%)")
        else:
            print(f"       {desc}")

    try:
        path = ffmpeg_tools.download_ffmpeg(progress=prog)
    except Exception as exc:
        check("FFmpeg 自动下载成功", False, str(exc)[:200])
        print("\n       ⚠️ FFmpeg 下载失败，但继续验证模型下载分支（不提前退出）")
        path = None
    else:
        dt = time.time() - t0
        check("FFmpeg 自动下载成功", path.exists(), f"{path}  耗时 {dt:.0f}s")
        check("下载到应用目录内",
              str(path).startswith(str(config.BIN_DIR)), str(config.BIN_DIR))

    # ── 3. 再探测：应"就绪" ────────────────────────────
    if path:
        print("\n[3] 下载后重新探测")
        info2 = ffmpeg_tools.probe_environment()
        print(f"       {info2.get('message')}")
        print(f"       {info2.get('version')}")
        check("下载后探测为就绪", bool(info2.get("ok")))
        check("**whisper 滤镜存在**", bool(info2.get("whisper")),
              "这是阶段 2 转录的硬前提")

        # 实际跑一次 ffmpeg，确认不是"能列出滤镜但不能用"
        from sliceq import subproc          # 绝对导入（见文件头的说明）
        try:
            out = subproc.run([str(path), "-hide_banner", "-version"],
                           capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace").stdout
            check("ffmpeg 可实际执行", "ffmpeg version" in out,
                  out.splitlines()[0] if out else "")
        except Exception as exc:
            check("ffmpeg 可实际执行", False, str(exc))

        check("ffprobe 同目录存在",
              ffmpeg_tools.find_ffprobe(path) is not None,
              str(ffmpeg_tools.find_ffprobe(path)))
    else:
        print("\n[3] 跳过（FFmpeg 未就绪）")

    # ── 4. 下载 whisper 模型 ───────────────────────────
    model = args.model or "ggml-tiny.bin"
    print(f"\n[4] 下载 whisper 模型 {model}")
    meta = asr_models.MODELS.get(model, {})
    print(f"       预期体积约 {meta.get('size_mb', '?')}MB")

    t0 = time.time()
    last[0] = 0.0

    def prog2(done: int, total: int, desc: str) -> None:
        now = time.time()
        if now - last[0] < 3:
            return
        last[0] = now
        if total:
            print(f"       {desc}  {fmt_mb(done)}/{fmt_mb(total)}"
                  f"  ({done * 100 // total}%)")

    try:
        mp = asr_models.download_model(model, progress=prog2)
    except Exception as exc:
        check("whisper 模型下载成功", False, str(exc)[:200])
        return _summary()

    dt = time.time() - t0
    check("whisper 模型下载成功", mp.exists(),
          f"{mp.name}  {asr_models.installed_size_mb(model)}MB  耗时 {dt:.0f}s")
    check("模型文件大小合理（>1MB）", mp.stat().st_size > 1024 * 1024)
    check("is_installed() 判定为已安装", asr_models.is_installed(model))

    # ── 5. 元信息读取（用下载的模型对应的素材无关，只验 ffprobe）──
    print("\n[5] 用新装的 ffmpeg 读素材元信息")
    from sliceq import ingest
    media = Path(__file__).resolve().parents[1] / "stage0" / "test_media" / "source.mp4"
    if media.exists():
        m = ingest.probe_media(media)
        check("能读出时长", bool(m.get("duration_sec")),
              f"{m.get('duration_sec')}s")
        check("能读出分辨率", bool(m.get("width")),
              f"{m.get('width')}x{m.get('height')}")
    else:
        print("       [跳过] 测试素材不存在")

    return _summary()


def _summary() -> int:
    print("\n" + "=" * 72)
    line = f"通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项"
    if SKIPPED:
        line += f" / 跳过 {len(SKIPPED)} 项"
    print(line)
    if SKIPPED:
        print("跳过项（前置条件不成立，不是失败）："
              + "、".join(SKIPPED))
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
