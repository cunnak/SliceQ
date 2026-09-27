# -*- coding: utf-8 -*-
"""候选清单页 —— 分析结果的展示与操作。

对应 TECH-DESIGN §2.5 的成本可见性：

  ① 启动时给上限        → AnalyzeDialog（在点「开始分析」时）
  ② **唯一确认点**       → 本页。粗筛完成后停下来，用户看到清单再决定
  ③ 跑中实时累计        → 顶部进度条旁
  ④ 完成后实际 vs 预估   → 底部对账条

═══════════════════════════════════════════════════════════════
为什么确认点不用"工作线程弹窗"
═══════════════════════════════════════════════════════════════
在流水线跑的时候弹一个模态框问"要不要继续"，需要跨线程等待
（工作线程阻塞、主线程弹窗、再回传结果）—— 那套机制容易出死锁。

这里换了个更简单的路子，**利用已经实现好的断点续跑**：

  第一次 run：`on_confirm` 返回 False → 停在粗筛后、返回候选清单
  用户点「开始精析」
  第二次 run：粗筛窗口全部命中缓存（0 次模型调用、0 花费）→ 直接进精析

代价是多启动一次流水线，好处是没有跨线程等待，且**每一步都落盘可续**。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ... import analyzer, config, ffmpeg_tools, pipeline, store
from ..workers import guard_ui
from .copy_dialog import CopyDialog

# ⚠️ 这里**不该**有"阶段 → 任务状态"的映射（v1.17 删掉了）。
#    状态落库在 `pipeline.run()` 里做 —— 状态是任务的属性，由执行者维护。
#    原先放在本页的 UI 回调里，导致分析队列路径（不经过这些回调）
#    任务状态永远停在「已导入」，而且不报错。详见 pipeline.run 的 docstring。


# ⚠️ 必须是**模块级**函数：Worker 用 `fn.__code__.co_varnames` 判断
#    要不要注入 progress / should_cancel —— 闭包对象没有这个属性。
#
# 公开（不加下划线）是因为 `ui/queue.py` 的批量队列也要用它 ——
# 队列不该复制一份"构造 transport + 决定确认点"的逻辑，
# 那两份实现迟早会漂移，而漂移的后果是"单任务和队列跑出来不一样"。
def run_analysis(task_id: int, video: str, opts: pipeline.Options,
                 auto_continue: bool, user_started_refine: bool,
                 progress=None, should_cancel=None):
    """在工作线程里跑流水线。"""
    transport = analyzer.make_transport()

    def on_prog(p: pipeline.Progress) -> None:
        if progress:
            # 把 stage 编进描述里 —— Worker 的 progress 信号只有 (int,int,str)，
            # 不带就会丢掉阶段信息，UI 只能靠中文关键词去猜。
            progress(int(p.ratio * 100), 100,
                     f"[{p.stage}] {p.message}　·　已花 ¥{p.spent_cny:.3f}")

    def on_confirm(req: pipeline.ConfirmRequest) -> bool:
        if req.kind == "before_refine":
            return bool(auto_continue or user_started_refine)
        # over_budget：一律停下交给用户决定。
        # 硬上限的意义就是"异常时不要继续烧钱"，自动放宽等于没有上限。
        return False

    return pipeline.run(task_id, Path(video), opts, transport,
                        on_progress=on_prog, on_confirm=on_confirm,
                        should_cancel=should_cancel)


def fmt_ts(sec: float) -> str:
    s = max(0, int(sec))
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{ss:02d}" if h else f"{m:02d}:{ss:02d}"


class ClipsPage(QWidget):
    def __init__(self, pool, parent=None) -> None:
        super().__init__(parent)
        self.pool = pool
        self.task_id: int | None = None
        self._opts: pipeline.Options | None = None
        self._auto_continue = False
        self._last: pipeline.RunResult | None = None
        self._worker = None
        self._build()

    # ─────────────────────────────────────────────────────
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        # ── 标题行 ────────────────────────────────────
        head = QHBoxLayout()
        self.title = QLabel("候选清单")
        self.title.setStyleSheet("font-size:16px;font-weight:500;")
        head.addWidget(self.title)
        head.addStretch(1)

        self.refine_btn = QPushButton("开始精析")
        self.refine_btn.setVisible(False)
        self.refine_btn.clicked.connect(self._on_refine_clicked)
        head.addWidget(self.refine_btn)

        self.export_btn = QPushButton("导出成片")
        self.export_btn.clicked.connect(self._on_export)
        head.addWidget(self.export_btn)

        self.copy_btn = QPushButton("生成文案")
        self.copy_btn.setToolTip("为勾选的片段生成候选标题与发布简介")
        self.copy_btn.clicked.connect(self._on_copy)
        head.addWidget(self.copy_btn)

        self.reanalyze_btn = QPushButton("重新分析")
        self.reanalyze_btn.clicked.connect(self._on_reanalyze)
        head.addWidget(self.reanalyze_btn)
        lay.addLayout(head)

        self.subtitle = QLabel("从「任务」页选一个任务，点「分析」开始。")
        self.subtitle.setStyleSheet("color:#888780;font-size:12px;")
        self.subtitle.setWordWrap(True)
        lay.addWidget(self.subtitle)

        # ── 进度区 ────────────────────────────────────
        self.progress_box = QFrame()
        pl = QVBoxLayout(self.progress_box)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(4)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        pl.addWidget(self.bar)
        self.progress_lbl = QLabel("")
        self.progress_lbl.setStyleSheet("color:#888780;font-size:12px;")
        pl.addWidget(self.progress_lbl)
        self.progress_box.setVisible(False)
        lay.addWidget(self.progress_box)

        # ── 表格 ─────────────────────────────────────
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["", "时间点", "时长", "类型", "评分", "描述", "标题候选"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemDoubleClicked.connect(self._on_double_click)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 34)
        for c in (1, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.Stretch)
        hh.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        lay.addWidget(self.table, 1)

        # ── 对账条 ────────────────────────────────────
        self.footer = QLabel("")
        self.footer.setStyleSheet("color:#888780;font-size:12px;")
        self.footer.setWordWrap(True)
        lay.addWidget(self.footer)

    # ─────────────────────────────────────────────────────
    # 外部入口
    # ─────────────────────────────────────────────────────
    def current_task_id(self) -> int | None:
        """当前正在看的任务 id。供别的页面（如样式页）取素材尺寸用。"""
        return self.task_id

    def show_task(self, task_id: int) -> None:
        self.task_id = task_id
        task = store.get_task(task_id) or {}
        self.title.setText(task.get("name") or "候选清单")
        self.refresh()

    @guard_ui
    def start_analysis(self, task_id: int, opts: pipeline.Options,
                       auto_continue: bool) -> None:
        self.task_id = task_id
        self._opts = opts
        self._auto_continue = auto_continue
        self._last = None
        self.refine_btn.setVisible(False)
        self._kickoff(user_started_refine=False)
        self.show_task(task_id)

    # ─────────────────────────────────────────────────────
    def _kickoff(self, user_started_refine: bool) -> None:
        task = store.get_task(self.task_id or -1)
        if not task:
            self._tip("任务不存在")
            return
        video = task.get("source_path")
        if not video or not Path(video).exists():
            self._tip("源文件不在了 —— 请确认它没被移动或删除")
            return

        self.progress_box.setVisible(True)
        self.bar.setValue(0)
        self.progress_lbl.setText("准备中…")
        self.refine_btn.setEnabled(False)
        self.reanalyze_btn.setEnabled(False)

        self._worker = self.pool.run(
            run_analysis, self.task_id, video, self._opts,
            self._auto_continue, user_started_refine,
            on_progress=self._on_progress,
            on_done=self._on_done,
            on_error=self._on_error)

    @guard_ui
    def _on_progress(self, done: int, total: int, desc: str) -> None:
        self.bar.setValue(max(0, min(100, done)))

        # 去掉 "[stage] " 前缀再显示 —— 那是流水线内部的阶段标记，
        # 不是给用户看的文案
        if desc.startswith("["):
            end = desc.find("]")
            if end > 0:
                desc = desc[end + 1:].strip()
        self.progress_lbl.setText(desc)
        # 任务状态落库**不在这里** —— 见文件头注释（v1.17 移到 pipeline 里）

    @guard_ui
    def _on_done(self, result) -> None:
        self._last = result
        self.bar.setValue(100)
        self.refine_btn.setEnabled(True)
        self.reanalyze_btn.setEnabled(True)

        if isinstance(result, pipeline.RunResult):
            if not result.finished:
                # 停在确认点 —— 这正是设计里那唯一一次停顿。
                # 状态标成「待精析」而不是「已完成」，否则用户以为跑完了。
                self.refine_btn.setVisible(True)
                secs = sum(c.duration for c in result.candidates)
                self.progress_lbl.setText(
                    f"粗筛完成：{len(result.candidates)} 个候选段，"
                    f"共 {secs / 60:.1f} 分钟 —— 决定权在你"
                    f"（点「开始精析」继续，或「先看清单」直接勾选导出）")
                # 状态（待精析 / 待导出）由 pipeline 落库，不在这里写
            else:
                self.refine_btn.setVisible(False)
                if result.candidates:
                    self.progress_lbl.setText(
                        f"完成：{len(result.clips)} 个切片段")
                else:
                    self.progress_lbl.setText("没有找到候选段")
            self._show_footer(result)

        self.refresh()

    @guard_ui
    def _on_error(self, msg: str, detail: str) -> None:
        self.progress_box.setVisible(False)
        self.refine_btn.setEnabled(True)
        self.reanalyze_btn.setEnabled(True)
        # 状态（出错 / 已取消）由 pipeline 落库，不在这里写 ——
        # 那边能区分"用户取消"与"真出错"，这里只能看到字符串
        self._tip(f"分析失败：{msg}")
        print(detail)

    # ─────────────────────────────────────────────────────
    @guard_ui
    def _on_refine_clicked(self) -> None:
        """用户看完清单，决定花钱精析。"""
        if not self.task_id:
            return
        self.progress_lbl.setText("继续精析（粗筛结果复用，不重复花费）…")
        self.refine_btn.setVisible(False)
        self._kickoff(user_started_refine=True)

    @guard_ui
    def _on_export(self) -> None:
        """导出勾选的片段。

        乙方案：每条候选一个独立 mp4（而非拼成一个合集）。
        """
        task = store.get_task(self.task_id or -1)
        if not task:
            self._tip("还没有选中任务。")
            return
        clips = [c for c in store.list_clips(self.task_id) if c.get("selected")]
        if not clips:
            self._tip("请先勾选要导出的片段（表格最左侧那一列）。")
            return
        dlg = ExportDialog(task=task, clips=clips, pool=self.pool, parent=self)
        dlg.exec()

    @guard_ui
    def _on_copy(self) -> None:
        """为勾选的片段生成标题与简介（阶段 4-B · F9）。"""
        task = store.get_task(self.task_id or -1)
        if not task:
            self._tip("还没有选中任务。")
            return
        clips = [c for c in store.list_clips(self.task_id) if c.get("selected")]
        if not clips:
            self._tip("请先勾选要写文案的片段（表格最左侧那一列）。")
            return
        dlg = CopyDialog(task=task, clips=clips, pool=self.pool, parent=self)
        dlg.exec()

    @guard_ui
    def _on_reanalyze(self) -> None:
        """清掉中间产物重跑（包括候选缓存）。"""
        task = store.get_task(self.task_id or -1)
        if not task:
            return
        src = task.get("source_path") or ""
        if src:
            pipeline.clear_cache(src)
        # ⚠️ 必须连旧切片一起清 —— 否则新旧结果堆在同一张表里，
        #    用户看到的是两次结果的叠加（时间点重复、评分混杂），
        #    而且完全看不出是哪里不对。
        store.clear_clips(self.task_id)
        self.progress_lbl.setText("已清空缓存，重新分析…")
        self._kickoff(user_started_refine=False)

    # ─────────────────────────────────────────────────────
    # 表格
    # ─────────────────────────────────────────────────────
    def refresh(self) -> None:
        if not self.task_id:
            return
        clips = store.list_clips(self.task_id)
        self.table.blockSignals(True)
        self.table.setRowCount(len(clips))
        for r, c in enumerate(clips):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked if c.get("selected") else Qt.Unchecked)
            chk.setData(Qt.UserRole, c["id"])
            self.table.setItem(r, 0, chk)

            item = QTableWidgetItem(fmt_ts(c["start"]))
            item.setData(Qt.UserRole, c["id"])
            self.table.setItem(r, 1, item)
            self.table.setItem(r, 2, QTableWidgetItem(
                f"{c['end'] - c['start']:.1f}s"))
            self.table.setItem(r, 3, QTableWidgetItem(c.get("type") or ""))
            score = c.get("score") or 0
            si = QTableWidgetItem(str(score))
            if score >= 85:
                si.setForeground(Qt.red)
            self.table.setItem(r, 4, si)
            self.table.setItem(r, 5, QTableWidgetItem(c.get("summary") or ""))
            self.table.setItem(r, 6, QTableWidgetItem(c.get("title_hint") or ""))
        self.table.blockSignals(False)

        n_sel = sum(1 for c in clips if c.get("selected"))
        if clips:
            self.subtitle.setText(
                f"{len(clips)} 个切片段，已勾选 {n_sel} 个。"
                f"双击任意行可在系统播放器里预览。")
        elif self.task_id:
            self.subtitle.setText(
                "还没有切片段。点「开始精析」，或用「重新分析」换一版结果。")

    @guard_ui
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        cid = item.data(Qt.UserRole)
        if cid:
            store.set_clip_selected(int(cid), item.checkState() == Qt.Checked)
            n_sel = sum(1 for c in store.list_clips(self.task_id)
                        if c.get("selected"))
            n = len(store.list_clips(self.task_id))
            self.subtitle.setText(
                f"{n} 个切片段，已勾选 {n_sel} 个。双击任意行可在系统播放器里预览。")

    @guard_ui
    def _on_double_click(self, item: QTableWidgetItem) -> None:
        """双击预览：切一小段无损副本，用系统播放器打开。

        为什么不内置播放器：QtMultimedia 在各平台的编解码支持不一致，
        容器/编码稍有出入就是黑屏或无声 —— 而且那类问题**不报错**。
        切一段 `-c copy` 的副本（几乎瞬时）交给系统播放器更可靠。
        """
        row = item.row()
        cid_item = self.table.item(row, 0)
        if not cid_item:
            return
        cid = cid_item.data(Qt.UserRole)
        clips = [c for c in store.list_clips(self.task_id) if c["id"] == cid]
        if not clips:
            return
        c = clips[0]
        task = store.get_task(self.task_id) or {}
        src = task.get("source_path")
        if not src or not Path(src).exists():
            self._tip("源文件不在了，无法预览")
            return

        ff = ffmpeg_tools.find_ffmpeg()
        if not ff:
            self._tip("需要 FFmpeg 才能预览，请到「设置」页安装")
            return

        out_dir = config.APP_ROOT / "work" / Path(src).stem / "preview"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"preview_{cid}.mp4"
        if not (out.exists() and out.stat().st_size > 1024):
            dur = max(1.0, c["end"] - c["start"])
            r = subprocess.run(
                [str(ff), "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", f"{c['start']:.3f}", "-t", f"{dur:.3f}",
                 "-i", str(src), "-c", "copy", "-avoid_negative_ts", "make_zero",
                 str(out)],
                capture_output=True, text=True, timeout=180,
                encoding="utf-8", errors="replace")
            if r.returncode != 0 or not out.exists():
                # `-c copy` 依赖关键帧对齐，切点不巧就会失败 —— 退回重编码
                subprocess.run(
                    [str(ff), "-hide_banner", "-loglevel", "error", "-y",
                     "-ss", f"{c['start']:.3f}", "-t", f"{dur:.3f}",
                     "-i", str(src), "-c:v", "libx264", "-preset", "veryfast",
                     "-crf", "28", "-c:a", "aac", str(out)],
                    capture_output=True, timeout=300)
        try:
            os.startfile(str(out))          # noqa: S606  (Windows 专用)
        except Exception as exc:
            self._tip(f"打开预览失败：{exc}")

    # ─────────────────────────────────────────────────────
    def _show_footer(self, r: pipeline.RunResult) -> None:
        """对账：实际 vs 预估（成本可见性第 ④ 层）。"""
        parts = [f"本次实际花费 ¥{r.spent_cny:.4f}"]
        if r.estimated_upper_bound:
            parts.append(f"（启动时给的上限 ¥{r.estimated_upper_bound:.2f}）")
        if r.trimmed_seconds:
            parts.append(
                f"· 因「筛选强度」上限，另有 {r.trimmed_seconds / 60:.1f} 分钟"
                f"内容未进入候选")
        parts.append("· 实际扣费以百炼账单为准")
        self.footer.setText("　".join(parts))

        if r.degraded:
            # 降级事件必须让用户看到 —— 静默降级等于隐瞒
            self.progress_lbl.setText(
                self.progress_lbl.text() + f"　⚠️ {len(r.degraded)} 条提示")

    def _tip(self, text: str) -> None:
        self.subtitle.setText(text)
