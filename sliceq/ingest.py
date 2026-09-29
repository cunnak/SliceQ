# -*- coding: utf-8 -*-
"""导入模块：把「一个本地文件」或「一个 URL」变成库里的一条 task。

两条通道：
  本地 —— 直接 ffprobe 读元信息，不复制文件（省磁盘）
  URL  —— yt-dlp 下载到 %APPDATA%/SliceQ/downloads/ 再走本地流程

⚠️ 所有函数都是**阻塞**的，必须在工作线程里调用（见 ui/workers.py）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from . import config, ffmpeg_tools, subproc, store

ProgressCb = Callable[[int, int, str], None]   # (已下载字节, 总字节, 描述)
CancelCb = Callable[[], bool]                  # 返回 True 表示应中止


class ImportCancelled(Exception):
    """用户主动取消。"""


class ImportError_(Exception):
    """导入失败，message 直接给用户看。"""


# ─────────────────────────────────────────────────────────────
# 元信息读取
# ─────────────────────────────────────────────────────────────
def probe_media(path: str | Path) -> dict:
    """用 ffprobe 读时长/分辨率/帧率/码率。

    读不到不报错 —— 返回空 dict，让上层用兜底值。
    「元信息不全」不该阻断导入。
    """
    path = Path(path)
    ffmpeg = ffmpeg_tools.find_ffmpeg()
    ffprobe = ffmpeg_tools.find_ffprobe(ffmpeg)
    info: dict = {"filesize": path.stat().st_size if path.exists() else None}

    if not ffprobe:
        return info

    cmd = [
        str(ffprobe), "-v", "error",
        "-show_entries", "format=duration,bit_rate",
        "-show_entries", "stream=codec_type,width,height,r_frame_rate",
        "-of", "json", str(path),
    ]
    try:
        raw = subproc.run(cmd, capture_output=True, text=True,
                             timeout=60, encoding="utf-8",
                             errors="replace").stdout
        data = json.loads(raw or "{}")
    except Exception:
        return info

    fmt = data.get("format") or {}
    if fmt.get("duration"):
        try:
            info["duration_sec"] = float(fmt["duration"])
        except ValueError:
            pass
    if fmt.get("bit_rate"):
        try:
            info["bitrate_kbps"] = int(int(fmt["bit_rate"]) / 1000)
        except ValueError:
            pass

    for st in data.get("streams") or []:
        if st.get("codec_type") == "video":
            info.setdefault("width", st.get("width"))
            info.setdefault("height", st.get("height"))
            fps = _parse_fps(st.get("r_frame_rate"))
            if fps:
                info.setdefault("fps", fps)
            break
    return info


def _parse_fps(expr: str | None) -> float | None:
    """'30/1' → 30.0"""
    if not expr or "/" not in expr:
        return None
    try:
        num, den = expr.split("/", 1)
        den_f = float(den)
        return round(float(num) / den_f, 3) if den_f else None
    except Exception:
        return None


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


# ─────────────────────────────────────────────────────────────
# 本地导入
# ─────────────────────────────────────────────────────────────
def import_local(path: str | Path, name: str | None = None) -> int:
    """把一个本地视频登记成 task，返回 task_id。"""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise ImportError_(f"文件不存在：{p}")
    if not p.is_file():
        raise ImportError_(f"不是文件：{p}")
    if p.suffix.lower() not in config.VIDEO_EXTS:
        raise ImportError_(
            f"不支持的格式：{p.suffix}\n"
            f"支持：{'、'.join(sorted(config.VIDEO_EXTS))}"
        )

    info = probe_media(p)
    return store.create_task(
        name=name or p.stem,
        source_path=str(p),
        duration_sec=info.get("duration_sec"),
        width=info.get("width"),
        height=info.get("height"),
        fps=info.get("fps"),
        bitrate_kbps=info.get("bitrate_kbps"),
        filesize=info.get("filesize"),
    )


def import_many(paths: list[str]) -> tuple[list[int], list[str]]:
    """批量导入。返回 (成功的 task_id 列表, 失败的中文说明列表)。"""
    ok: list[int] = []
    errs: list[str] = []
    for raw in paths:
        try:
            ok.append(import_local(raw))
        except ImportError_ as exc:
            errs.append(f"{Path(raw).name}：{exc}")
        except Exception as exc:
            errs.append(f"{Path(raw).name}：未预期错误 {exc}")
    return ok, errs


# ─────────────────────────────────────────────────────────────
# URL 导入（yt-dlp）
# ─────────────────────────────────────────────────────────────
def download_url(url: str,
                 progress: ProgressCb | None = None,
                 should_cancel: CancelCb | None = None,
                 _ydl=None) -> Path:
    """用 yt-dlp 下载到 downloads/，返回本地文件路径。

    ⚠️ 失败信息要翻译成中文 —— yt-dlp 的原始报错对普通用户毫无意义。
    """
    try:
        import yt_dlp
    except ImportError as exc:
        raise ImportError_("缺少 yt-dlp 组件，无法下载 URL。"
                           "请运行：pip install yt-dlp") from exc

    config.ensure_dirs()
    outtmpl = str(config.DOWNLOADS_DIR / "%(title).80s.%(ext)s")

    def hook(d: dict) -> None:
        if should_cancel and should_cancel():
            raise ImportCancelled("用户取消")
        if not progress:
            return
        status = d.get("status")
        if status == "downloading":
            progress(d.get("downloaded_bytes") or 0,
                     d.get("total_bytes") or d.get("total_bytes_estimate") or 0,
                     "下载中")
        elif status == "finished":
            progress(1, 1, "转码中")

    opts = {
        "outtmpl": outtmpl,
        "progress_hooks": [hook],
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # B 站回放常见为 m3u8，合并成 mp4 更方便后续 ffmpeg 处理
        "merge_output_format": "mp4",
        "format": "bv*+ba/b",
    }

    ydl = _ydl or yt_dlp.YoutubeDL(opts)
    try:
        info = ydl.extract_info(url, download=True)
    except ImportCancelled:
        raise
    except yt_dlp.utils.DownloadError as exc:
        raise ImportError_(_translate_ytdlp_error(str(exc))) from exc
    except Exception as exc:
        raise ImportError_(f"下载失败：{exc}") from exc

    if info is None:
        raise ImportError_("下载失败：未能解析该链接")

    if info.get("entries"):          # 万一拿到的是播放列表
        info = info["entries"][0]

    path = info.get("requested_downloads", [{}])[0].get("filepath")
    if not path:
        path = Path(ydl.prepare_filename(info))
    p = Path(path)
    if not p.exists():
        # 合并后扩展名可能变了，兜底扫一遍
        cand = list(config.DOWNLOADS_DIR.glob(p.stem + ".*"))
        if cand:
            p = cand[0]
    if not p.exists():
        raise ImportError_("下载完成但找不到输出文件")
    return p


def _translate_ytdlp_error(msg: str) -> str:
    """把 yt-dlp 的报错翻成人话。"""
    low = msg.lower()
    if "unsupported url" in low or "no suitable" in low:
        return "这个链接不支持。请确认是 B 站回放/视频页面的完整链接。"
    if "404" in low or "not found" in low:
        return "链接失效或视频已被删除（404）。"
    if "403" in low or "forbidden" in low:
        return "服务器拒绝访问（403）。可能需要登录或该视频有权限限制。"
    if "timed out" in low or "timeout" in low:
        return "网络超时。请检查网络后重试。"
    if "connection" in low or "resolve" in low:
        return "网络连接失败。请检查网络或代理设置。"
    return f"下载失败：{msg[:300]}"


def import_url(url: str,
               progress: ProgressCb | None = None,
               should_cancel: CancelCb | None = None) -> int:
    """下载 + 登记成 task，返回 task_id。"""
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ImportError_("链接必须以 http:// 或 https:// 开头")

    p = download_url(url, progress=progress, should_cancel=should_cancel)
    if progress:
        progress(1, 1, "读取元信息")
    info = probe_media(p)
    return store.create_task(
        name=p.stem,
        source_path=str(p),
        source_url=url,
        duration_sec=info.get("duration_sec"),
        width=info.get("width"),
        height=info.get("height"),
        fps=info.get("fps"),
        bitrate_kbps=info.get("bitrate_kbps"),
        filesize=info.get("filesize"),
    )
