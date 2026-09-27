# -*- coding: utf-8 -*-
"""ggml whisper 模型的获取。

⚠️ HuggingFace 国内直连不通，必须走镜像 hf-mirror.com。
   这一点在阶段 0 已实测（TECH-DESIGN §5.1）。

为什么用 ggml 格式：FFmpeg 内置的 whisper 滤镜直接吃 ggml 模型文件，
不需要额外的 Python 推理框架 —— 这是"零额外依赖做转录"的关键。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import config

ProgressCb = Callable[[int, int, str], None]

# 可选模型。体积是实测值（HEAD 请求确认），给用户做选择依据。
#
# ⚠️ 默认档位不是拍脑袋定的，依据是阶段 2 前置实测
#    （reports/STAGE2-PRE-LIVE-ASR-QUALITY.md）：
#      质量排序（60 秒真实中文直播）：
#        tiny  ❌ 错字连篇、语义不通，不可用
#        base  ⚠️ 大意对、细节错（"二维码"→"二维马"）
#        small ✅ 基本正确
#        large-v3-turbo  —— 用户指定为默认（上游质量最强的一档可用型号）
MODELS: dict[str, dict] = {
    "ggml-tiny.bin": {
        "label": "tiny", "size_mb": 74,
        "note": "最快，但中文质量不可用（实测）"},
    "ggml-base.bin": {
        "label": "base", "size_mb": 141,
        "note": "快，中文大意对、细节错"},
    "ggml-small.bin": {
        "label": "small", "size_mb": 465,
        "note": "中文基本正确，速度与质量平衡"},
    "ggml-large-v3-turbo-q5_0.bin": {
        "label": "large-v3-turbo (量化 q5_0)", "size_mb": 547,
        "note": "体积仅完整版 1/3，质量损失通常很小"},
    "ggml-large-v3-turbo.bin": {
        "label": "large-v3-turbo", "size_mb": 1549,
        "note": "默认。上游质量最强的一档可用型号"},
    "ggml-large-v3.bin": {
        "label": "large-v3", "size_mb": 2952,
        "note": "最准但最慢，比 turbo 慢数倍"},
}


def model_path(name: str | None = None) -> Path:
    name = name or config.WHISPER_MODEL_DEFAULT
    return config.MODELS_DIR / name


def is_installed(name: str | None = None) -> bool:
    p = model_path(name)
    # 小于 1MB 视为下载不全
    return p.exists() and p.stat().st_size > 1024 * 1024


def installed_size_mb(name: str | None = None) -> float:
    p = model_path(name)
    if not p.exists():
        return 0.0
    return round(p.stat().st_size / 1024 / 1024, 1)


def model_url(name: str) -> str:
    return f"{config.WHISPER_MODEL_BASE_URL}/{name}"


def download_model(name: str | None = None,
                   progress: ProgressCb | None = None,
                   mirror: str | None = None) -> Path:
    """下载模型到 %APPDATA%/SliceQ/models/。

    支持断点续传（HTTP Range）—— 模型动辄几百 MB，
    半路断掉重来对用户是灾难。
    """
    import requests

    name = name or config.WHISPER_MODEL_DEFAULT
    dest = model_path(name)
    dest.parent.mkdir(parents=True, exist_ok=True)

    base = mirror or config.HF_MIRROR
    url = f"{base}/ggerganov/whisper.cpp/resolve/main/{name}"

    tmp = dest.with_suffix(dest.suffix + ".part")
    have = tmp.stat().st_size if tmp.exists() else 0

    headers = {"Range": f"bytes={have}-"} if have else {}
    mode = "ab" if have else "wb"

    with requests.get(url, stream=True, timeout=60,
                      headers=headers, allow_redirects=True) as r:
        if have and r.status_code == 200:
            # 服务端不支持 Range → 从头来
            have, mode = 0, "wb"
        r.raise_for_status()

        total = int(r.headers.get("Content-Length") or 0) + have
        done = have
        with open(tmp, mode) as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total, f"下载 {name}")

    tmp.replace(dest)
    return dest


def delete_model(name: str | None = None) -> None:
    p = model_path(name)
    if p.exists():
        p.unlink()


def list_installed() -> list[str]:
    if not config.MODELS_DIR.exists():
        return []
    return sorted(p.name for p in config.MODELS_DIR.glob("ggml-*.bin"))


# ─────────────────────────────────────────────────────────────
# VAD 模型（语音活动检测）
# ─────────────────────────────────────────────────────────────
# ⚠️ VAD 模型**不在** ggerganov/whisper.cpp 仓库里，
#    而在独立仓库 **ggml-org/whisper-vad** —— 找模型时最容易找错的地方。
VAD_BASE_URL = f"{config.HF_MIRROR}/ggml-org/whisper-vad/resolve/main"

VAD_MODELS: dict[str, dict] = {
    "ggml-silero-v6.2.0.bin": {"label": "silero v6.2.0", "size_kb": 864,
                               "note": "较新"},
    "ggml-silero-v5.1.2.bin": {"label": "silero v5.1.2", "size_kb": 864,
                               "note": "经典版本"},
}


def vad_model_path(name: str | None = None) -> Path:
    return config.MODELS_DIR / (name or config.VAD_MODEL_DEFAULT)


def is_vad_installed(name: str | None = None) -> bool:
    p = vad_model_path(name)
    return p.exists() and p.stat().st_size > 100 * 1024


def download_vad(name: str | None = None,
                 progress: ProgressCb | None = None) -> Path:
    """下载 VAD 模型（约 864KB，很快）。"""
    import requests

    name = name or config.VAD_MODEL_DEFAULT
    dest = vad_model_path(name)
    dest.parent.mkdir(parents=True, exist_ok=True)

    url = f"{VAD_BASE_URL}/{name}"
    tmp = dest.with_suffix(dest.suffix + ".part")

    with requests.get(url, stream=True, timeout=60,
                      allow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total, f"下载 {name}")
    tmp.replace(dest)
    return dest


def ensure_vad(progress: ProgressCb | None = None) -> Path:
    """确保 VAD 模型就位，返回路径。

    ⚠️ **已弃用于转录路径**（2026-09-25 实测）。

    ffmpeg whisper 滤镜的 `vad_model` **不能过滤非语音** —— 它只决定"何时切分
    buffer"，而转录起点永远是 buffer 第 0 个样本（源码 `af_whisper.c:filter_frame()`）。
    纯静音实验：无 VAD 出 11 段幻觉，有 VAD 也是 11 段；耗时增加约 4 倍。

    静音处理改用 `asr.speech_ranges()` + `asr.extract_speech_only()`。
    详见 `reports/STAGE2-PRE-HALLUCINATION-CONTROL.md`。

    保留本函数仅为：① 记录 VAD 模型的实际来源仓库（`ggml-org/whisper-vad`，
       **不在 `ggerganov/whisper.cpp` 里**，找模型时极易找错地方）；
       ② 将来若 ffmpeg 改变了 VAD 语义，需要一个重新验证的入口。
    """
    if is_vad_installed():
        return vad_model_path()
    return download_vad(progress=progress)
