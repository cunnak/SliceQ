# -*- coding: utf-8 -*-
"""字幕样式实时预览 —— 所见即所得。

## 为什么用 ffmpeg + libass 渲染，而不是自己画字

烧录成片用的就是 libass（ffmpeg 的 `subtitles` 滤镜）。
如果用 PIL / QPainter 自己画一行文字来"预览"，换行位置、描边粗细、
字间距都会有细微差别 —— 预览很好看、成片却不一样，是最让人恼火的一类问题。

所以这里**走完整链路**：生成真 ASS → 交给真滤镜 → 出一张真帧。

## 性能与体验

单帧渲染约 0.3~0.8 秒（含一次 ffmpeg 进程启动）。
用户拖动滑块时会连续触发，所以：

  - **防抖**：改动后延迟 ~280ms 才真渲染
  - **异步**：在工作线程里跑，绝不卡 UI
  - **序号校验**：渲染完发现已有更新的请求 → 丢弃结果，
    避免"慢的那一次后回来把新的覆盖掉"
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from .. import config, ffmpeg_tools, subtitle_style

# 预览用的示例文字。
# 长文本刻意选得够长，好让用户一眼看出"会自动换行、不会溢出被裁"。
TEXT_LONG_MAIN = "必然有些真理永远无法证明"
TEXT_LONG_SUB = "这是一段用于测试字幕预览和样式设置的长文本内容"
TEXT_SHORT_MAIN = "这是短文本"
TEXT_SHORT_SUB = "Short text"

# 没有素材可参考时的默认画布
DEFAULT_PORTRAIT = (720, 1280)
DEFAULT_LANDSCAPE = (1280, 720)

_DEBOUNCE_MS = 280


class PreviewError(Exception):
    pass


def _diag(msg: str) -> None:
    """临时诊断输出（设 SILQ_DIAG=1 才打印）。"""
    import os
    import sys
    if os.environ.get("SILQ_DIAG"):
        print(f"    [preview] {msg}", file=sys.stderr, flush=True)


# ─────────────────────────────────────────────────────────────
# 渲染（纯函数，可在工作线程里跑）
# ─────────────────────────────────────────────────────────────
def render_preview(preset: subtitle_style.SubtitlePreset, *,
                   width: int, height: int,
                   text_mode: str = "long",
                   background: str | Path | None = None,
                   out_dir: Path | None = None,
                   stamp: str = "preview") -> Path:
    """渲染一张预览帧，返回 PNG 路径。

    画布尺寸 = ASS 的 PlayRes 尺寸，**必须与真实成片一致** ——
    否则字号相对画面的比例不对，预览就失去意义了。
    （显示时再缩放图片，不改渲染尺寸。）
    """
    ff = ffmpeg_tools.find_ffmpeg()
    if not ff:
        raise PreviewError("未找到 FFmpeg，无法生成预览。请先在「环境」里安装。")

    width = max(160, int(width))
    height = max(160, int(height))
    work = (Path(out_dir) if out_dir else (config.APP_ROOT / "preview")).resolve()
    work.mkdir(parents=True, exist_ok=True)

    # ⚠️ 下面所有路径都要 **resolve()**：字幕滤镜走「相对路径 + cwd」，
    #    而 cwd 一旦改变，其它相对路径就不再相对项目根，而是相对 work ——
    #    输出路径会指到不存在的目录，报 `Error submitting a packet to the
    #    muxer: No such file or directory`（看不出是路径基准的问题）。

    # ── 1. 生成 ASS ────────────────────────────────────────
    if text_mode == "short":
        main_text, sub_text = TEXT_SHORT_MAIN, TEXT_SHORT_SUB
    else:
        main_text, sub_text = TEXT_LONG_MAIN, TEXT_LONG_SUB

    # 预览时长：只要能让两行都出现在同一帧里即可
    cues = [(0.0, 4.0, main_text, sub_text)]
    body = preset.to_ass(cues, width=width, height=height, title="SliceQ preview")
    ass = (work / f"{stamp}.ass").resolve()
    ass.write_text(body, encoding="utf-8")

    out = (work / f"{stamp}.png").resolve()
    # ⚠️ 不要 unlink 旧文件。
    #    两个原因：
    #    1. ffmpeg 带 `-y` 本来就会直接覆盖，删是多余的；
    #    2. 在受管环境里删文件可能被安全策略拦截 —— 而且拦下来抛的是
    #       SystemExit 而非 OSError，`except OSError` 抓不住，会一路穿到
    #       线程池外面（实测踩到：界面永远停在"渲染中"）。
    #    改用 mtime 判断本次是否真的产出了新图，同样能防"读到旧图"。
    before_mtime = out.stat().st_mtime if out.exists() else 0.0

    # ── 2. 组 ffmpeg 命令 ─────────────────────────────────
    # 字幕路径走「相对路径 + cwd」，规避滤镜参数的冒号转义问题
    vf_arg, cwd = subtitle_style.ass_filter_arg(ass, cwd=work)

    argv: list[str] = [str(ff), "-hide_banner", "-loglevel", "error",
                       "-nostdin", "-y"]

    bg = Path(background).resolve() if background else None
    if bg and bg.exists():
        # 背景图：等比缩放 + 居中补边，避免拉伸变形
        argv += ["-i", str(bg)]
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x1a1c20,"
              f"{vf_arg}")
    else:
        # 无背景图：纯深灰底。字幕是亮色+描边，在这个底色上对比度足够，
        # 而且不会喧宾夺主（用户要看的是文字效果，不是背景）。
        argv += ["-f", "lavfi", "-i", f"color=c=0x26282c:s={width}x{height}"]
        vf = vf_arg

    argv += ["-vf", vf, "-frames:v", "1", str(out)]

    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=60, cwd=str(cwd) if cwd else None,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise PreviewError("预览渲染超时（超过 60 秒）。") from exc

    if r.returncode != 0:
        detail = (r.stderr or "").strip()[-400:]
        raise PreviewError(_explain_preview_error(r.returncode, detail))
    if not out.exists() or out.stat().st_size < 512:
        raise PreviewError("预览渲染没有产出有效图片。")
    if out.stat().st_mtime <= before_mtime:
        # 没更新 = ffmpeg 其实没写成（被占用 / 权限 / 静默失败）。
        # 不检查的话会把上一次的旧图当成本次结果显示出来。
        raise PreviewError("预览图未更新（文件可能被占用或没有写入权限）。")

    return out


def _explain_preview_error(code: int, detail: str) -> str:
    low = detail.lower()
    if "fontconfig" in low:
        return ("字体库配置异常，预览无法渲染。\n"
                "（这是本机 ffmpeg 构建的已知问题：drawtext 滤镜会崩，"
                "subtitles 滤镜正常。若字幕烧录正常，此处可忽略。）\n"
                f"{detail[:200]}")
    if "unable to open" in low and ".ass" in low:
        return ("预览用的字幕文件没能被 ffmpeg 读到（工作目录问题）。\n"
                f"{detail[:200]}")
    return f"预览渲染失败（退出码 {code}）：\n{detail[:300]}"


# ─────────────────────────────────────────────────────────────
# 预览控件
# ─────────────────────────────────────────────────────────────
class StylePreview(QWidget):
    """显示字幕样式的实时预览；参数一变就重渲染。"""

    render_failed = Signal(str)

    def __init__(self, pool=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pool = pool
        self._preset: subtitle_style.SubtitlePreset | None = None
        self._text_mode = "long"
        self._orientation = "portrait"
        self._background: Path | None = None
        self._canvas: tuple[int, int] | None = None
        self._seq = 0
        self._busy = False
        self._pending = False

        self._build()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_DEBOUNCE_MS)
        self._timer.timeout.connect(self._do_render)

    # ── 构建 ──────────────────────────────────────────────
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.view = QLabel("预览准备中…")
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setMinimumSize(180, 240)
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.view.setStyleSheet(
            "background:#1a1c20;border:1px solid #3a3d42;"
            "border-radius:6px;color:#8b8f96;font-size:12px;")
        lay.addWidget(self.view)

        self.info = QLabel("")
        self.info.setStyleSheet("color:#8b8f96;font-size:11px;padding:2px 4px;")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)

    # ── 外部接口 ───────────────────────────────────────────
    def set_preset(self, preset: subtitle_style.SubtitlePreset) -> None:
        self._preset = preset
        self.refresh()

    def set_text_mode(self, mode: str) -> None:
        if mode not in ("long", "short"):
            return
        self._text_mode = mode
        self.refresh()

    def set_orientation(self, orientation: str) -> None:
        if orientation not in ("portrait", "landscape"):
            return
        self._orientation = orientation
        self.refresh()

    def set_background(self, path: str | Path | None) -> None:
        self._background = Path(path) if path else None
        self.refresh()

    def set_canvas(self, width: int, height: int) -> None:
        """指定预览画布尺寸（通常取素材的真实像素尺寸）。"""
        if width > 0 and height > 0:
            self._canvas = (int(width), int(height))
            self.refresh()

    def canvas_size(self) -> tuple[int, int]:
        if self._canvas:
            return self._canvas
        return DEFAULT_PORTRAIT if self._orientation == "portrait" \
            else DEFAULT_LANDSCAPE

    def refresh(self, immediate: bool = False) -> None:
        """请求重渲染（默认防抖）。"""
        if self._preset is None:
            return
        if immediate:
            self._do_render()
        else:
            self._timer.start()

    # ── 渲染调度 ──────────────────────────────────────────
    def _do_render(self) -> None:
        _diag("_do_render 进入 busy=%s preset=%s" % (self._busy, self._preset is not None))
        if self._preset is None:
            return
        if self._busy:
            # 正在渲染 → 记下"还有一个待处理"，等这次结束再补一次，
            # 而不是并发开一堆 ffmpeg 把机器拖慢。
            self._pending = True
            return

        self._seq += 1
        seq = self._seq
        preset = self._preset
        w, h = self.canvas_size()
        self._busy = True
        self.info.setText("渲染中…")

        kwargs = dict(width=w, height=h, text_mode=self._text_mode,
                      background=self._background,
                      out_dir=config.APP_ROOT / "preview",
                      # 固定文件名 + ffmpeg 的 -y 覆盖：
                      # 不累积垃圾文件，也不需要 unlink（见 render_preview 注释）
                      stamp="preview")

        def job():
            _diag("job 开始 seq=%d" % seq)
            try:
                r = render_preview(preset, **kwargs)
                _diag("job 成功 %s" % r)
                return r
            except BaseException:
                import traceback as _tb
                _diag("job 抛错，完整栈：\n" + _tb.format_exc())
                raise

        def done(path):
            _diag("done 回调 path=%s" % path)
            self._busy = False
            if seq != self._seq:
                self._after_render()
                return          # 已有更新的请求，丢弃这张过期图
            self._show(Path(path))
            self._after_render()

        def failed(msg: str, detail: str = ""):
            _diag("failed 回调 msg=%s" % str(msg)[:120])
            self._busy = False
            if seq == self._seq:
                self.view.setText("预览不可用")
                self.info.setText(str(msg).splitlines()[0][:80])
                self.render_failed.emit(str(msg))
            self._after_render()

        if self._pool is not None:
            _diag("提交到线程池 pool=%r" % type(self._pool).__name__)
            try:
                self._pool.run(job, on_done=done, on_error=failed)
                _diag("已提交")
            except BaseException as e:
                _diag("提交失败 %r" % e)
                failed(str(e))
        else:
            try:
                done(job())
            except Exception as exc:              # noqa: BLE001
                failed(str(exc))

    def _after_render(self) -> None:
        if self._pending:
            self._pending = False
            self._timer.start(60)

    def _show(self, png: Path) -> None:
        pm = QPixmap(str(png))
        if pm.isNull():
            self.view.setText("预览图读取失败")
            return
        w, h = self.canvas_size()
        scaled = pm.scaled(self.view.size(), Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
        self.view.setPixmap(scaled)
        self.info.setText(f"预览画布 {w}×{h}px　·　"
                          f"与成片字幕按同一套渲染器（libass）")

    def resizeEvent(self, event) -> None:        # noqa: N802  (Qt 回调)
        super().resizeEvent(event)
        # 只是重新缩放已有图片，不重新渲染
        if self.view.pixmap() is not None and not self.view.pixmap().isNull():
            self.view.setPixmap(self.view.pixmap().scaled(
                self.view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
