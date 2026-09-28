# -*- coding: utf-8 -*-
"""主窗口：左侧导航 + 右侧内容区。

布局说明（对应 PRD 7.1）：
  PRD 写的是「左侧任务列表、右侧主区」。实际实现里，**任务列表做成了右侧
  内容区的一个页面**（含拖放区 + URL 输入 + 任务表格），左侧只放导航。
  原因：任务列表本身就需要"导入入口 + 表格详情"两块空间，
  塞进 260px 的侧栏会两头都局促。
  ⚠️ 若与 PRD 预期不符，阶段 1 验收时提出，改回去成本很低。

全局拖放：**整个窗口都接受拖入**，不只是虚线框。
用户有一半概率会拖到别处，只支持框内会让程序看起来"没反应"。
"""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QStackedWidget, QVBoxLayout, QWidget,
)

from .. import config, store
from .pages.clips_page import ClipsPage
from .pages.settings_page import SettingsPage
from .pages.style_page import StylePage
from .pages.tasks_page import TasksPage
from .queue import AnalysisQueue
from .workers import WorkerPool, guard_ui


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{config.APP_DISPLAY_NAME} v{config.VERSION}")
        # 初始尺寸**不要超过屏幕可用区域**（2026-09-28 干净虚拟机验证发现）：
        # 固定写 1180x720 时，在 1024x768 这类小屏上窗口右边/下边会落到屏幕外，
        # 而 Windows 不允许把标题栏拖出上边界 ⇒ 那部分内容用户**永远够不到**。
        from PySide6.QtWidgets import QApplication
        scr = QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else None
        w0, h0 = 1180, 720
        if avail is not None:
            w0 = min(w0, max(760, avail.width() - 40))
            h0 = min(h0, max(560, avail.height() - 40))
        self.resize(w0, h0)
        self.setAcceptDrops(True)

        self.pool = WorkerPool(max_threads=4)
        # 分析队列由主窗口持有 —— 页面被重建时队列不该跟着丢。
        # ⚠️ 它**只在内存里**（关程序就清空），理由见 ui/queue.py 头注释。
        self.analysis_queue = AnalysisQueue(self.pool, self)
        self._build()
        self._refresh_nav()

    # ─────────────────────────────────────────────────────
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── 左侧导航 ──
        side = QWidget()
        side.setFixedWidth(200)
        side.setObjectName("sidebar")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(12, 18, 12, 18)
        sl.setSpacing(6)

        brand = QLabel("SliceQ")
        brand.setStyleSheet("font-size:18px;font-weight:600;padding:0 6px;")
        sl.addWidget(brand)

        version = QLabel(f"v{config.VERSION}")
        version.setStyleSheet("color:#888780;font-size:11px;padding:0 6px 10px 6px;")
        sl.addWidget(version)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.addItem("任务")
        self.nav.addItem("候选清单")
        self.nav.addItem("字幕样式")
        self.nav.addItem("设置")
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self._on_nav)
        sl.addWidget(self.nav, 1)

        self.env_hint = QLabel("")
        self.env_hint.setWordWrap(True)
        self.env_hint.setStyleSheet("color:#888780;font-size:11px;padding:6px;")
        sl.addWidget(self.env_hint)

        lay.addWidget(side)

        # ── 右侧内容 ──
        self.stack = QStackedWidget()
        self.tasks_page = TasksPage(self.pool, on_analyze=self._start_analysis,
                                     queue=self.analysis_queue)
        self.clips_page = ClipsPage(self.pool)
        self.style_page = StylePage(self.pool)
        self.settings_page = SettingsPage(self.pool)
        self.stack.addWidget(self.tasks_page)      # 0
        self.stack.addWidget(self.clips_page)      # 1
        self.stack.addWidget(self.style_page)      # 2
        self.stack.addWidget(self.settings_page)   # 3
        # 任务页双击 / 右键「查看候选清单」→ 切到清单页
        self.tasks_page.task_opened.connect(self._open_clips)
        lay.addWidget(self.stack, 1)

        self.setStyleSheet("""
            QWidget#sidebar { background: rgba(128,128,128,0.06); }
            QListWidget#nav { border: none; background: transparent; font-size: 14px; }
            QListWidget#nav::item { padding: 9px 10px; border-radius: 8px; }
            QListWidget#nav::item:selected { background: rgba(128,128,128,0.18); }
        """)

        self._probe_env()

        # ── 诊断开关（生产环境不设置 = 完全不执行）─────────────────
        # 用途：发布版是 `--windowed` 打包，在别人机器上出现"界面少画东西"
        #       时，用它区分「文案压根没设上」与「控件没被画出来」。
        # 用法：启动前设 SLICEQ_DIAG=1，然后读 logs/sliceq.log 里 [DIAG] 行。
        if os.environ.get("SLICEQ_DIAG"):
            from PySide6.QtCore import QTimer
            QTimer.singleShot(4000, self._diag_hint)

    def _diag_hint(self) -> None:
        """把侧栏提示控件的**真实状态**写进日志（仅 SLICEQ_DIAG=1 时调用）。"""
        h = self.env_hint
        logging.getLogger("sliceq.ui").info(
            "[DIAG] env_hint text=%r visible=%s geo=%s sizeHint=%s "
            "| sidebar=%s | window=%s",
            h.text(), h.isVisible(), h.geometry(), h.sizeHint(),
            h.parentWidget().size() if h.parentWidget() else None, self.size())

    # ─────────────────────────────────────────────────────
    @guard_ui
    def _on_nav(self, row: int) -> None:
        self.stack.setCurrentIndex(max(0, min(row, self.stack.count() - 1)))
        if row == 3:
            self.settings_page.refresh_all()
        elif row == 2:
            self._sync_style_canvas()
            self.style_page.refresh_all()
        elif row == 1:
            self.clips_page.refresh()
        elif row == 0:
            self.tasks_page.refresh()
            self._refresh_nav()

    def _sync_style_canvas(self) -> None:
        """让样式预览的画布尺寸跟随当前任务素材的真实像素。

        字号在 ASS 里是按画布高度比例定位的 —— 画布尺寸不对，
        预览里看到的字号大小就和成片不一样，预览也就没意义了。
        """
        try:
            tid = self.clips_page.current_task_id()
            task = store.get_task(tid) if tid else None
            if task and task.get("width") and task.get("height"):
                self.style_page.set_canvas_from_task(int(task["width"]),
                                                     int(task["height"]))
        except Exception:
            pass

    @guard_ui
    def _open_clips(self, task_id: int) -> None:
        """任务页 → 候选清单页。"""
        self.nav.setCurrentRow(1)
        self.clips_page.show_task(task_id)

    @guard_ui
    def _start_analysis(self, task_id: int, opts, auto_continue: bool) -> None:
        """分析设置对话框确认后，切页并启动流水线。"""
        self.nav.setCurrentRow(1)
        self.clips_page.start_analysis(task_id, opts, auto_continue)

    @guard_ui
    def _refresh_nav(self) -> None:
        """侧栏底部显示任务数，顺手做一次轻量环境提示。"""
        n = len(store.list_tasks())
        self.env_hint.setText(f"共 {n} 个任务" if n else "还没有任务\n拖个视频进来试试")

    @guard_ui
    def _probe_env(self) -> None:
        """启动时后台体检环境，把结果放进侧栏提示。"""
        from .. import ffmpeg_tools

        def done(info: dict) -> None:
            if info.get("ok"):
                self.env_hint.setText(self.env_hint.text() + "\nFFmpeg 就绪")
            else:
                self.env_hint.setText(self.env_hint.text()
                                      + "\n⚠️ FFmpeg 未就绪\n去「设置」安装")

        self.pool.run(ffmpeg_tools.probe_environment, on_done=done,
                      on_error=lambda m, d: None)

    # ─────────────────────────────────────────────────────
    # 全局拖放：拖到窗口任意位置都能导入
    # ─────────────────────────────────────────────────────
    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.nav.setCurrentRow(0)
            self.tasks_page._on_files(paths)
            e.acceptProposedAction()

    def closeEvent(self, e) -> None:
        # 队列在跑的时候关程序 = 中断一条正在跑的流水线。
        # 不拦的话，进程被强杀会留下"状态是 refining 但实际没在跑"的任务，
        # 用户下次打开看到那个状态会一头雾水。
        if self.analysis_queue.is_running():
            from PySide6.QtWidgets import QMessageBox
            ok = QMessageBox.question(
                self, "还在分析",
                "分析队列正在运行。现在关闭会中断当前这条，\n"
                "已经跑完的步骤会保留（下次重新分析不必重跑）。\n\n"
                "确定要关闭吗？")
            if ok != QMessageBox.Yes:
                e.ignore()
                return
            self.analysis_queue.cancel()
            self.pool.wait(8000)      # 给取消检查点一点时间
            store.close()
            super().closeEvent(e)
            return

        self.pool.wait(3000)
        store.close()
        super().closeEvent(e)
