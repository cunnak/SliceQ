# -*- coding: utf-8 -*-
"""FFmpeg 的定位、校验与获取。

⚠️ 合规红线（TECH-DESIGN §5.1）：
   **绝不允许把 ffmpeg.exe 打进 SliceQ 的 exe**。
   FFmpeg 是 GPL 构建，打包进 MIT 项目会造成许可冲突。
   本模块只在运行时把 ffmpeg 下载到用户目录，并用 subprocess 调用 ——
   两个独立程序经命令行交互，属 GPL FAQ 允许的"聚合"。

⚠️ 关键约束：必须用 **full build**。essentials 构建**不含 whisper 滤镜**，
   而 whisper 滤镜是阶段 2 的默认转录引擎。下载后必须**实际验证**。

⚠️⚠️ 实测教训（2026-09-25）：**"full build" 这个名字不可信。**
   - gyan.dev `ffmpeg-release-full.7z`           → ✅ 含 whisper
   - BtbN `ffmpeg-master-latest-win64-gpl.zip`   → ❌ **不含 whisper**
   两者文件名都像完整构建，但只有前者带 whisper 滤镜。
   **因此本模块的策略是：下载 → 实际跑 `-filters` 验证 → 不通过就换源。**
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Callable

from . import config, subproc

ProgressCb = Callable[[int, int, str], None]   # (已下载字节, 总字节, 阶段描述)

# ─────────────────────────────────────────────────────────────
# 下载源
# ─────────────────────────────────────────────────────────────
# ⚠️⚠️ 下载源的三条实测结论（2026-09-25，共花约 350MB 流量换来）：
#
#  1. **"full build" 这个名字不可信**
#     BtbN/FFmpeg-Builds 的 `ffmpeg-master-latest-win64-gpl.zip`
#     **不含 whisper 滤镜**，尽管文件名看起来像完整构建。
#
#  2. **唯一确认含 whisper 的是 gyan.dev 的 full build**
#     （本机系统里那个 `Gyan.FFmpeg full_build` 实测带 whisper）
#
#  3. **但 gyan.dev 官网在国内极慢** —— 实测约 30 KB/s，
#     下 161MB 要 1.5 小时，完全不能作为"首次启动自动安装"的体验。
#     而 GitHub 同一份内容实测 18 MB/s。
#
# ⇒ 最优解：**用 Gyan 发布在 GitHub 上的官方构建**。
#    仓库 `GyanD/codexffmpeg` 的每个 release 都带
#    `ffmpeg-<版本>-full_build.zip` —— 同样是 Gyan 的 full build（含 whisper），
#    但走 GitHub（快），且是 .zip（标准库可直接解压，连 py7zr 都不需要）。

_GYAN_GH_REPO = "GyanD/codexffmpeg"
_GYAN_GH_API = f"https://api.github.com/repos/{_GYAN_GH_REPO}/releases/latest"

# 兜底：BtbN 的 zip（快，但**可能缺 whisper**——反正会被验证挡下）
_FALLBACK_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/"
                 "latest/ffmpeg-master-latest-win64-gpl.zip")

# 解析结果缓存，避免一次下载里重复请求 API
_resolved_cache: str | None = None


def resolve_gyan_github_zip(timeout: int = 25) -> str | None:
    """查 GyanD/codexffmpeg 最新 release，返回 `*-full_build.zip` 的下载地址。

    查不到返回 None（网络问题 / API 限流），调用方应继续尝试其它源。
    """
    global _resolved_cache
    if _resolved_cache:
        return _resolved_cache
    try:
        import requests
        r = requests.get(_GYAN_GH_API, timeout=timeout,
                         headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    for asset in data.get("assets") or []:
        name = (asset.get("name") or "").lower()
        # 要 full_build（含 whisper），不要 essentials，不要 shared
        if name.endswith("-full_build.zip"):
            url = asset.get("browser_download_url")
            if url:
                _resolved_cache = url
                return url
    return None


def _build_sources() -> list[tuple[str, str, str]]:
    """返回 [(url, 压缩格式, 给用户看的名字)]，按优先级排序。

    每个源下载后都会**实际验证 whisper 滤镜**，不通过就换下一个。
    """
    out: list[tuple[str, str, str]] = []

    gh = resolve_gyan_github_zip()
    if gh:
        out.append((gh, "zip", "gyan.dev full build（GitHub 官方发布）"))

    # 官网 .7z —— 内容等价但国内很慢，只在 GitHub 那条路不通时用
    out.append((config.FFMPEG_RELEASE_ZIP, "7z", "gyan.dev 官网 full build"))

    # 最后兜底：快，但可能缺 whisper（会被验证挡下来）
    out.append((_FALLBACK_URL, "zip", "BtbN 构建"))

    return out


# ─────────────────────────────────────────────────────────────
# 定位
# ─────────────────────────────────────────────────────────────
def _exe_name() -> str:
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


def _probe_name() -> str:
    return "ffprobe.exe" if os.name == "nt" else "ffprobe"


def app_ffmpeg_path() -> Path | None:
    """应用自己下载的 ffmpeg（在 %APPDATA%/SliceQ/bin/ 下）。"""
    p = config.BIN_DIR / _exe_name()
    return p if p.exists() else None


def find_ffmpeg() -> Path | None:
    """按优先级找 ffmpeg：应用目录 → 用户手动指定 → 系统 PATH。"""
    manual = get_manual_path()
    if manual and Path(manual).exists():
        return Path(manual)

    p = app_ffmpeg_path()
    if p:
        return p

    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def find_ffprobe(ffmpeg: Path | None = None) -> Path | None:
    """ffprobe 通常与 ffmpeg 同目录。"""
    if ffmpeg:
        cand = ffmpeg.with_name(_probe_name())
        if cand.exists():
            return cand
    found = shutil.which("ffprobe")
    return Path(found) if found else None


def probe_video_size(media: str | Path,
                     ffmpeg: Path | None = None) -> tuple[int, int]:
    """探测视频的**显示尺寸**，返回 (width, height)；失败返回 (0, 0)。

    ⚠️ 为什么不能直接用 ffprobe 的 width/height：
       手机与竖屏直播的 mp4 常把画面**横向编码**，靠 `rotate=90`
       元数据在播放时转正。ffprobe 报的是编码尺寸，不是用户看到的尺寸；
       而 ffmpeg 默认会自动应用旋转（autorotate），导出后的实际尺寸
       是转正后的。

    用编码尺寸去设 ASS 的 `PlayResX/Y`，字幕的定位坐标就落在
    错误的画布上 —— **画面是竖的、字幕却按横的排版**，
    而且不报任何错。所以必须连旋转一起读。

    用途：`editor.py` 生成字幕前调用，确保 ASS 的 PlayRes
    与成片实际像素尺寸一致。
    """
    ffprobe = find_ffprobe(ffmpeg or find_ffmpeg())
    if not ffprobe:
        return 0, 0
    try:
        r = subproc.run(
            [str(ffprobe), "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=width,height:stream_side_data=rotation",
             "-of", "json", str(media)],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace")
        data = json.loads(r.stdout or "{}")
        st = (data.get("streams") or [{}])[0]
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)

        rotation = 0
        for sd in (st.get("side_data_list") or []):
            if isinstance(sd, dict) and "rotation" in sd:
                try:
                    rotation = int(float(sd["rotation"]))
                except (TypeError, ValueError):
                    pass
        if abs(rotation) % 180 == 90:
            w, h = h, w
        return w, h
    except Exception:
        return 0, 0


def has_audio_stream(media: str | Path,
                     ffmpeg: Path | None = None) -> bool:
    """媒体是否含音频轨。

    阶段 4 配乐要判：原片无声时不能去引用 `[0:a]`，
    否则 filter_complex 直接报 "Stream specifier ':a' matches no streams"
    —— 这个错误信息完全看不出是"素材本来就没声音"。

    探测失败时**返回 True**（乐观）：宁可让 ffmpeg 报出真实错误，
    也不要因为探测抖动而悄悄把用户的音轨丢掉。
    """
    ffprobe = find_ffprobe(ffmpeg or find_ffmpeg())
    if not ffprobe:
        return True
    try:
        r = subproc.run(
            [str(ffprobe), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "json", str(media)],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace")
        data = json.loads(r.stdout or "{}")
        return bool(data.get("streams"))
    except Exception:
        return True


# 用户手动指定路径（存在 settings.json，非敏感）
def get_manual_path() -> str:
    from . import settings
    return settings.get("ffmpeg_path", "")


def set_manual_path(path: str) -> None:
    from . import settings
    settings.set("ffmpeg_path", path)


# ─────────────────────────────────────────────────────────────
# 校验
# ─────────────────────────────────────────────────────────────
def list_filters(ffmpeg: Path, timeout: int = 20) -> set[str]:
    """取 ffmpeg 支持的滤镜名集合。失败返回空集。"""
    try:
        out = subproc.run(
            [str(ffmpeg), "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        ).stdout
    except Exception:
        return set()

    names: set[str] = set()
    for line in out.splitlines():
        # 形如: " ... whisper           A->A  Transcribe audio ..."
        parts = line.split()
        if len(parts) >= 3 and "->" in parts[2]:
            names.add(parts[1])
    return names


def has_whisper(ffmpeg: Path) -> bool:
    return "whisper" in list_filters(ffmpeg)


def ffmpeg_source() -> dict:
    """说明当前用的是哪个 ffmpeg、以及**手动指定的路径是否被忽略了**。

    ⚠️ 为什么要单独做这件事：`find_ffmpeg()` 在手动路径无效时会**静默回退**
       到应用目录或系统 PATH。这个回退本身是对的（用户填错了不该让整个
       程序不可用），但**不说出来就有问题** ——

       用户特意指定了某个 ffmpeg（比如他换成了带 whisper 的那份），
       路径写错一个字符，程序默默用了另一个。他会一直以为自己"设置过了"，
       然后在转录时撞上莫名其妙的失败，完全想不到是设置没生效。

    返回：{path, source, manual_invalid, manual_path}
    """
    manual = get_manual_path()
    manual_exists = bool(manual) and Path(manual).exists()
    manual_invalid = bool(manual) and not manual_exists

    if manual_exists:
        return {"path": str(Path(manual)), "source": "手动指定",
                "manual_invalid": False, "manual_path": manual}

    p = app_ffmpeg_path()
    if p:
        return {"path": str(p), "source": "应用目录（自动下载的）",
                "manual_invalid": manual_invalid, "manual_path": manual}

    found = shutil.which("ffmpeg")
    return {"path": str(found) if found else "", "source": "系统 PATH",
            "manual_invalid": manual_invalid, "manual_path": manual}


def probe_environment() -> dict:
    """一次性给出环境体检结果，供设置页显示。"""
    ff = find_ffmpeg()
    if not ff:
        return {"ok": False, "reason": "missing", "ffmpeg": None,
                "message": "未检测到 FFmpeg"}
    try:
        ver = subproc.run(
            [str(ff), "-hide_banner", "-version"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        ).stdout.splitlines()
        version_line = ver[0] if ver else ""
    except Exception as exc:
        return {"ok": False, "reason": "broken", "ffmpeg": str(ff),
                "message": f"FFmpeg 无法运行：{exc}"}

    whisper = has_whisper(ff)
    src = ffmpeg_source()

    message = ("FFmpeg 就绪（含 whisper 滤镜）" if whisper
               else "FFmpeg 可用但**缺少 whisper 滤镜**"
                    "—— 该构建不是 full build，阶段 2 转录不可用")
    if src.get("manual_invalid"):
        # 这条必须显式说出来：用户设的路径没生效，他有权知道
        message += (f"\n⚠️ 你在设置里指定的路径不存在："
                    f"{src.get('manual_path')}，已自动改用"
                    f"「{src.get('source')}」。")

    return {
        "ok": whisper,
        "reason": "ok" if whisper else "no_whisper",
        "ffmpeg": str(ff),
        "version": version_line,
        "whisper": whisper,
        "source": src.get("source", ""),
        "manual_invalid": src.get("manual_invalid", False),
        "message": message,
    }


# ─────────────────────────────────────────────────────────────
# 下载
# ─────────────────────────────────────────────────────────────
def _download(url: str, dest: Path, progress: ProgressCb | None,
              attempts: int = 3) -> None:
    """带**断点续传**与**重试**的下载。

    为什么必须这样做（2026-09-25 实测）：
      同一个 URL，前一分钟 18MB/s，下一分钟连不上（HTTP 000）；
      gyan.dev 官网则稳定在 20KB/s（161MB 要 2 小时）。
      配合 `.part` 文件续传，即可让"慢但不断"的源最终下完，
      也能让"快但抖"的源在重试后恢复。

    参数 attempts：总尝试次数（含首次）。每次失败退避 2s/4s/6s。
    """
    import time

    import requests

    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err: Exception | None = None

    for attempt in range(1, attempts + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        mode = "ab" if have else "wb"

        try:
            # (连接超时, 读取超时) —— 读取超时给足，慢源也能续
            with requests.get(url, stream=True, timeout=(15, 120),
                              headers=headers, allow_redirects=True) as r:
                if have and r.status_code == 200:
                    # 服务端不支持 Range：只能从头来
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
                            progress(done, total, "下载中")

            tmp.replace(dest)
            return
        except Exception as exc:
            last_err = exc
            if attempt < attempts:
                time.sleep(2 * attempt)     # 退避后重试（已下的部分保留）

    raise last_err if last_err else RuntimeError("下载失败")


def _extract_zip(archive: Path, target: Path) -> None:
    """解压 zip 并把它内部的 bin/ffmpeg.exe 提到 target 下。"""
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(target / "_tmp")
    _hoist(target / "_tmp", target)


def _extract_7z(archive: Path, target: Path) -> None:
    try:
        import py7zr
    except ImportError as exc:
        raise RuntimeError(
            "解压 .7z 需要 py7zr（pip install py7zr）。"
            "或改用手动安装：见设置页说明。"
        ) from exc
    target.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(archive, mode="r") as z:
        z.extractall(target / "_tmp")
    _hoist(target / "_tmp", target)


def _hoist(src: Path, target: Path) -> None:
    """把解压结果里的 ffmpeg/ffprobe 提到 target 根目录，然后清掉临时目录。"""
    for want in (_exe_name(), _probe_name()):
        hit = next(src.rglob(want), None)
        if hit:
            shutil.copy2(hit, target / want)
    shutil.rmtree(src, ignore_errors=True)


def download_ffmpeg(progress: ProgressCb | None = None) -> Path:
    """下载并安装 ffmpeg 到应用目录，返回 ffmpeg.exe 路径。

    逐个源尝试，**每个源下载后都实际验证 whisper 滤镜**：
    不通过的会被丢弃并换下一个源，而不是直接失败。

    ⚠️ 为什么必须"验证后再接受"而不是"下完就用"：
       文件名写着 full build 不代表真的带 whisper（已实测踩过）。
       若接受了一份缺滤镜的 ffmpeg，用户会一路用到阶段 2 转录失败才发现。
    """
    config.ensure_dirs()
    archive_dir = config.BIN_DIR / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    problems: list[str] = []
    sources = _build_sources()

    for url, kind, label in sources:
        name = url.rsplit("/", 1)[-1]
        archive = archive_dir / name

        # ── 下载 + 解压 ──────────────────────────────
        try:
            if progress:
                progress(0, 0, f"下载 {label}")
            _download(url, archive, progress)
            if progress:
                progress(0, 0, "解压中")
            if kind == "zip":
                _extract_zip(archive, config.BIN_DIR)
            else:
                _extract_7z(archive, config.BIN_DIR)
            archive.unlink(missing_ok=True)
        except Exception as exc:
            problems.append(f"{label}：下载或解压失败（{exc}）")
            _clean_installed()
            continue

        # ── 验证 ────────────────────────────────────
        ff = app_ffmpeg_path()
        if not ff:
            problems.append(f"{label}：解压后未找到 ffmpeg.exe")
            _clean_installed()
            continue

        if not has_whisper(ff):
            problems.append(
                f"{label}：**缺少 whisper 滤镜**（文件名像 full build，实际不是）")
            _clean_installed()          # 别留下一个不能用的 ffmpeg 占位
            continue

        return ff

    raise RuntimeError(_manual_hint(problems))


def _clean_installed() -> None:
    """清掉刚装上但不可用的 ffmpeg —— 否则 find_ffmpeg() 会选中它，
    用户就永远卡在"检测到 ffmpeg 但没有 whisper"的状态。"""
    for n in (_exe_name(), _probe_name()):
        p = config.BIN_DIR / n
        if p.exists():
            p.unlink(missing_ok=True)


def _manual_hint(problems: list[str]) -> str:
    return (
        "自动安装 FFmpeg 失败。\n\n"
        "各下载源的问题：\n  - " + "\n  - ".join(problems) + "\n\n"
        "请手动处理（任选其一）：\n"
        "  1. 下载 full build 后，把 ffmpeg.exe 与 ffprobe.exe 放到：\n"
        f"       {config.BIN_DIR}\n"
        "     下载页：https://www.gyan.dev/ffmpeg/builds/\n"
        "     ⚠️ 必须选 full build —— essentials 及部分第三方构建不含 whisper 滤镜\n"
        "  2. 或在「设置」页点「手动指定」，指向你已有的 ffmpeg.exe\n\n"
        "提示：若系统里已装好 ffmpeg，程序会自动使用它，无需重复下载。"
    )
