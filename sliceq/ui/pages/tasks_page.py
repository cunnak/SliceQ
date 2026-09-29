# -*- coding: utf-8 -*-
"""任务列表页：导入入口 + 任务表格 + 分析队列。

导入三条路：
  1. 拖文件进来（最主要的路径）
  2. 点"选择文件"
  3. 粘 URL

⚠️ 拖放要同时支持"拖到窗口任意位置"和"拖到虚线框上"——
   只支持后者的话，用户有一半的概率拖错地方然后以为程序没反应。

## 分析队列（阶段 4-D）

表格改为**多选**，可以一次把几条录像排进队列，一条接一条自动分析。
队列**串行**执行 —— 实测（`probe_asr_concurrency.py`）GPU 上并发转录
是负收益（4 路比串行慢 43%），所以队列不做并行。

⚠️ 队列**不持久化**（关程序就没了）。面板上必须说出来，
   不然用户以为排了就会在某个地方等着。
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ... import config, ingest, secrets, settings, store
from ...ingest import ImportError_
from ..workers import guard_ui
from .analyze_dialog import AnalyzeDialog
from .. import theme

class DropZone(QLabel):
    """虚线框，接受文件拖入。"""

    files_dropped = Signal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setText("把视频文件拖到这里\n或点击「选择文件」")
        self.setAlignment(Qt.AlignCenter)
        self.setAcceptDrops(True)
        self.setMinimumHeight(110)
        self.setStyleSheet(f"""
            QLabel {{
                border: 2px dashed {theme.muted()};
                border-radius: 12px;
                color: {theme.muted()};
                font-size: 14px;
                padding: 16px;
            }}
        """)

    # 拖进来
    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.setStyleSheet(self.styleSheet().replace(theme.muted(), f"{theme.accent()}"))

    def dragLeaveEvent(self, e) -> None:
        self.setStyleSheet(self.styleSheet().replace(f"{theme.accent()}", theme.muted()))

    def dropEvent(self, e) -> None:
        self.setStyleSheet(self.styleSheet().replace(f"{theme.accent()}", theme.muted()))
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            e.acceptProposedAction()

class TasksPage(QWidget):
    task_opened = Signal(int)

    COLS = ["名称", "时长", "分辨率", "状态", "来源"]

    def __init__(self, pool, on_analyze=None, queue=None, parent=None) -> None:
        super().__init__(parent)
        self.pool = pool
        # 由 MainWindow 注入：负责"切到候选清单页 + 启动分析"。
        # 做成回调而不是直接持有页面引用 —— 页面之间不该互相认识。
        self.on_analyze = on_analyze
        # 分析队列（也是注入的；页面自己不该造一个 —— 否则关掉页面就丢了）
        self.queue = queue
        self._last_col_sync = 0.0        # 状态列的节流时间戳
        self._build()
        self._wire_queue()
        self.refresh()
        self._refresh_queue()

    # ─────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        head = QHBoxLayout()
        title = QLabel("任务")
        title.setStyleSheet("font-size:20px;font-weight:600;")
        head.addWidget(title)
        head.addStretch(1)

        self.analyze_btn = QPushButton("分析")
        self.analyze_btn.setToolTip("对选中的任务做 AI 分析，找出可切片的片段")
        self.analyze_btn.clicked.connect(lambda: self._analyze())
        head.addWidget(self.analyze_btn)

        self.queue_btn = QPushButton("加入队列")
        self.queue_btn.setToolTip(
            "可以按住 Ctrl / Shift 一次选多个任务，排队后一条接一条自动分析")
        self.queue_btn.clicked.connect(self._add_to_queue)
        head.addWidget(self.queue_btn)

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self.refresh_btn)
        root.addLayout(head)

        root.addWidget(self._build_queue_box())

        self.drop = DropZone()
        self.drop.files_dropped.connect(self._on_files)
        root.addWidget(self.drop)

        pick_row = QHBoxLayout()
        pick = QPushButton("选择文件…")
        pick.clicked.connect(self._pick_files)
        pick_row.addWidget(pick)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("粘贴 B 站回放 / 视频链接，回车下载")
        self.url_edit.returnPressed.connect(self._on_url)
        pick_row.addWidget(self.url_edit, 1)

        self.url_btn = QPushButton("下载")
        self.url_btn.clicked.connect(self._on_url)
        pick_row.addWidget(self.url_btn)
        root.addLayout(pick_row)

        self.bar = QProgressBar()
        self.bar.setVisible(False)
        root.addWidget(self.bar)

        self.tip = QLabel("")
        self.tip.setWordWrap(True)
        self.tip.setVisible(False)
        root.addWidget(self.tip)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # 多选（Ctrl/Shift）—— 批量入队要靠它
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._ctx_menu)
        self.table.doubleClicked.connect(self._open_current)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(self.COLS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        root.addWidget(self.table, 1)

    # ─────────────────────────────────────────────────────
    # 分析队列面板
    # ─────────────────────────────────────────────────────
    def _build_queue_box(self) -> QGroupBox:
        """队列面板。默认隐藏 —— 没排队的时候不该占地方。"""
        box = QGroupBox("分析队列")
        lay = QVBoxLayout(box)
        lay.setSpacing(6)

        head = QHBoxLayout()
        self.queue_status = QLabel("")
        self.queue_status.setWordWrap(True)
        head.addWidget(self.queue_status, 1)

        self.queue_stop_btn = QPushButton("停止")
        self.queue_stop_btn.setToolTip("当前这条跑完当前步骤后停下，后面的全部跳过")
        self.queue_stop_btn.clicked.connect(self._stop_queue)
        head.addWidget(self.queue_stop_btn)

        self.queue_clear_btn = QPushButton("清空")
        self.queue_clear_btn.clicked.connect(self._clear_queue)
        head.addWidget(self.queue_clear_btn)
        lay.addLayout(head)

        self.queue_list = QListWidget()
        self.queue_list.setMaximumHeight(96)
        self.queue_list.setAlternatingRowColors(True)
        lay.addWidget(self.queue_list)

        self.queue_bar = QProgressBar()
        self.queue_bar.setRange(0, 100)
        self.queue_bar.setTextVisible(False)
        self.queue_bar.setMaximumHeight(6)
        lay.addWidget(self.queue_bar)

        # ⚠️ 队列不持久化，必须说出来
        note = QLabel("队列只在本次运行有效，关掉程序就清空"
                      "（已分析过的部分有缓存，重排不会重复花钱）。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.muted()};font-size:11px;")
        lay.addWidget(note)

        self.queue_box = box
        box.setVisible(False)
        return box

    def _wire_queue(self) -> None:
        if self.queue is None:
            return
        self.queue.added.connect(self._refresh_queue)
        self.queue.started.connect(self._on_queue_started)
        self.queue.progress.connect(self._on_queue_progress)
        self.queue.finished.connect(self._on_queue_finished)
        self.queue.failed.connect(self._on_queue_failed)

    @guard_ui
    def _refresh_queue(self) -> None:
        """重画队列面板（列表 + 可见性 + 按钮状态）。"""
        if self.queue is None:
            return
        items = self.queue.items()
        self.queue_box.setVisible(bool(items))
        self.queue_list.clear()
        for i, it in enumerate(items, 1):
            text = f"{i}. {it.name}　[{it.state_label}]"
            if it.message and it.state != "done":
                text += f"　{it.message}"
            row = QListWidgetItem(text)
            # 颜色区分状态 —— 一眼能看出哪条挂了
            color = {"done": theme.ok(), "failed": theme.danger(),
                     "running": f"{theme.accent()}", "skipped": theme.muted()}.get(it.state, theme.muted())
            row.setData(Qt.UserRole, i - 1)
            self.queue_list.addItem(row)
            self._color_item(row, color)

        running = self.queue.is_running()
        self.queue_stop_btn.setEnabled(running)
        self.queue_clear_btn.setEnabled(
            any(it.state == "waiting" for it in items))
        self.queue_btn.setEnabled(True)
        if not running and not items:
            self.queue_status.setText("")

    def _color_item(self, item: QListWidgetItem, color: str) -> None:
        from PySide6.QtGui import QBrush, QColor
        item.setForeground(QBrush(QColor(color)))

    @guard_ui
    def _on_queue_started(self, total: int) -> None:
        self.queue_bar.setValue(0)
        self.queue_status.setText(f"开始处理 {total} 个任务…")
        self.queue_status.setStyleSheet(f"font-size:12px;color:{theme.muted()};")
        self.queue_stop_btn.setEnabled(True)

    @guard_ui
    def _on_queue_progress(self, done: int, total: int, desc: str) -> None:
        self.queue_bar.setValue(max(0, min(100, done)))
        self.queue_status.setText(desc)
        self.queue_status.setStyleSheet(f"font-size:12px;color:{theme.muted()};")
        self._sync_table_status()

    def _sync_table_status(self) -> None:
        """只更新表格的「状态」列，**不重建表格**。

        重建会丢掉用户的多选（队列跑着的时候他可能正在挑下一批），
        而且每来一次进度就重建一遍表格，滚动位置也会跳。
        所以这里按 task_id 找到对应行，只改那一个单元格。
        """
        now = time.time()
        if now - self._last_col_sync < 1.2:      # 进度信号很密，节流
            return
        self._last_col_sync = now
        rows = {self.table.item(r, 0).data(Qt.UserRole): r
                for r in range(self.table.rowCount())
                if self.table.item(r, 0)}
        for t in store.list_tasks():
            r = rows.get(t["id"])
            if r is None:
                continue
            cell = self.table.item(r, 3)
            text = config.STATUS_LABELS.get(t["status"], t["status"])
            if cell is not None and cell.text() != text:
                cell.setText(text)

    @guard_ui
    def _on_queue_finished(self, summary: str) -> None:
        self.queue_bar.setValue(100)
        self.queue_status.setText(f"队列结束：{summary}")
        self.queue_status.setStyleSheet(f"font-size:12px;color:{theme.ok()};")
        self.queue_stop_btn.setEnabled(False)
        self.refresh()
        self._refresh_queue()
        # 失败项要说得具体一点，不能只说"失败 1"
        bad = [r for r in self.queue.results() if r["state"] == "failed"]
        if bad:
            lines = "\n".join(f"· {b['name']}：{b['message']}" for b in bad[:5])
            QMessageBox.warning(
                self, "队列结束",
                f"{summary}\n\n以下几项没跑成：\n{lines}\n\n"
                "可以单独选中它们重新加入队列。")

    @guard_ui
    def _on_queue_failed(self, msg: str, detail: str) -> None:
        self.queue_stop_btn.setEnabled(False)
        self.queue_status.setText(f"队列中断：{msg}")
        self.queue_status.setStyleSheet(f"font-size:12px;color:{theme.danger()};")
        self._refresh_queue()
        QMessageBox.warning(self, "队列中断", msg)

    @guard_ui
    def _stop_queue(self) -> None:
        if self.queue is None or not self.queue.is_running():
            return
        self.queue.cancel()
        self.queue_status.setText("正在停止…（当前这一步跑完就停）")
        self.queue_status.setStyleSheet(f"font-size:12px;color:{theme.warn()};")

    @guard_ui
    def _clear_queue(self) -> None:
        if self.queue is None:
            return
        n = self.queue.clear()
        if n:
            self._show_tip(f"已从队列移除 {n} 个未开始的任务", warn=False)
        self._refresh_queue()

    # ─────────────────────────────────────────────────────
    # 加入队列
    # ─────────────────────────────────────────────────────
    def _selected_ids(self) -> list[int]:
        ids: list[int] = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if item:
                tid = item.data(Qt.UserRole)
                if tid is not None:
                    ids.append(int(tid))
        return ids

    @guard_ui
    def _add_to_queue(self) -> None:
        """把选中的任务排进队列。多选时用一次对话框设定共同选项。"""
        if self.queue is None:
            return
        ids = self._selected_ids()
        if not ids:
            self._show_tip("先在列表里选中一个或多个任务"
                           "（按住 Ctrl / Shift 可多选）", warn=True)
            return

        # 已经在队列里的不重复排队（不管队列是否在跑）
        dup = [i for i in ids if self.queue.has_task(i)]
        cand = [i for i in ids if i not in dup]

        if not cand:
            self._show_tip("选中的任务都已经在队列里了", warn=True)
            return

        # 源文件不在的直接剔除并告知 —— 排队排到一半才发现文件没了
        # 是最差的结果（前面几条已经跑完了）
        alive, missing = [], []
        for tid in cand:
            t = store.get_task(tid)
            src = (t or {}).get("source_path")
            (alive if src and Path(src).exists() else missing).append(tid)
        if missing:
            names = "、".join((store.get_task(i) or {}).get("name", str(i))
                             for i in missing)
            self._show_tip(f"以下任务的源文件不在了，已跳过：{names}", warn=True)
        if not alive:
            return

        if not (secrets.get_api_key() or "").strip():
            QMessageBox.information(
                self, "还差一步",
                "分析需要调用大模型，请先到「设置」页填写百炼 API Key。\n\n"
                "（转录是本地免费的，但粗筛与精析要联网调模型）")
            return

        # 多选时用**总时长**做成本估算 —— 用第一条的时长会低估
        tasks = [store.get_task(i) or {} for i in alive]
        total_dur = sum(float(t.get("duration_sec") or 0.0) for t in tasks)
        if len(alive) == 1:
            dlg_task = tasks[0]
        else:
            dlg_task = {"name": f"{len(alive)} 个任务（共 "
                                f"{total_dur / 60:.0f} 分钟）",
                        "duration_sec": total_dur}

        dlg = AnalyzeDialog(dlg_task, self)
        if dlg.exec() != QDialog.Accepted:
            return
        opts = dlg.options()
        auto = dlg.auto_continue()

        added = 0
        for tid in alive:
            if self.queue.add(tid, opts, auto):
                added += 1
        if added:
            self._show_tip(
                f"已加入队列 {added} 个任务"
                + (f"（{len(dup)} 个已在队列中，未重复添加）" if dup else ""),
                warn=False)
        # 加入后如果队列没在跑，直接开始 —— 用户点「加入队列」就是想跑
        if not self.queue.is_running():
            self.queue.start()

    # ─────────────────────────────────────────────────────
    # 导入
    # ─────────────────────────────────────────────────────
    @guard_ui
    def _pick_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(config.VIDEO_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择视频文件", settings.get("last_dir", ""),
            f"视频文件 ({exts});;所有文件 (*)")
        if paths:
            settings.set("last_dir", str(Path(paths[0]).parent))
            self._on_files(paths)

    @guard_ui
    def _on_files(self, paths: list) -> None:
        self._show_tip("正在读取元信息…", warn=False)
        self.pool.run(
            ingest.import_many, list(paths),
            on_done=self._on_imported,
            on_error=lambda m, d: self._show_tip(f"导入出错：{m}", warn=True),
        )

    @guard_ui
    def _on_imported(self, result) -> None:
        ok, errs = result
        self.refresh()
        if errs:
            self._show_tip("；".join(errs), warn=True)
        elif ok:
            self._show_tip(f"已导入 {len(ok)} 个任务", warn=False)

    @guard_ui
    def _on_url(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            return
        self.url_btn.setEnabled(False)
        self.bar.setVisible(True)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self._show_tip("开始下载…", warn=False)

        self.pool.run(
            ingest.import_url, url,
            on_done=self._on_url_done,
            on_error=self._on_url_error,
            on_progress=self._on_url_progress,
            with_progress=True,
        )

    @guard_ui
    def _on_url_progress(self, done: int, total: int, desc: str) -> None:
        if total > 0 and total != 1:
            self.bar.setValue(int(done * 100 / total))
            self.bar.setFormat(f"{desc} %p%  "
                               f"({done / 1048576:.1f}/{total / 1048576:.1f} MB)")
        else:
            self.bar.setRange(0, 0)     # 不确定进度
            self.bar.setFormat(desc or "处理中…")

    @guard_ui
    def _on_url_done(self, task_id) -> None:
        self.bar.setVisible(False)
        self.url_btn.setEnabled(True)
        self.url_edit.clear()
        self.refresh()
        self._show_tip("下载完成并已入库", warn=False)

    @guard_ui
    def _on_url_error(self, msg: str, detail: str) -> None:
        self.bar.setVisible(False)
        self.url_btn.setEnabled(True)
        self._show_tip(msg, warn=True)
        QMessageBox.warning(self, "下载失败", msg)

    # ─────────────────────────────────────────────────────
    # 表格
    # ─────────────────────────────────────────────────────
    @guard_ui
    def refresh(self) -> None:
        tasks = store.list_tasks()
        self.table.setRowCount(0)
        for t in tasks:
            row = self.table.rowCount()
            self.table.insertRow(row)

            name_item = QTableWidgetItem(t["name"])
            name_item.setData(Qt.UserRole, t["id"])
            self.table.setItem(row, 0, name_item)

            self.table.setItem(row, 1, QTableWidgetItem(
                ingest.format_duration(t["duration_sec"])))

            if t["width"] and t["height"]:
                res = f"{t['width']}×{t['height']}"
                if t["fps"]:
                    res += f" @{t['fps']:g}"
            else:
                res = "—"
            self.table.setItem(row, 2, QTableWidgetItem(res))

            status = t["status"]
            self.table.setItem(row, 3, QTableWidgetItem(
                config.STATUS_LABELS.get(status, status)))

            src = "本地" if t["source_path"] and not t["source_url"] else (
                "链接" if t["source_url"] else "—")
            self.table.setItem(row, 4, QTableWidgetItem(src))

    def _current_task_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    @guard_ui
    def _open_current(self) -> None:
        tid = self._current_task_id()
        if tid:
            self.task_opened.emit(tid)

    @guard_ui
    def _analyze(self, task_id: int | None = None) -> None:
        """打开分析设置对话框，确认后交给 MainWindow 启动。

        ⚠️ 缺 key 要在**这里**拦住，而不是等流水线跑到粗筛才报错 ——
           转录是本地免费的、能跑十几分钟，等它跑完才说"没有 key"，
           用户白等一场。
        """
        tid = task_id or self._current_task_id()
        if not tid:
            self._show_tip("先在列表里选中一个任务", warn=True)
            return
        task = store.get_task(tid)
        if not task:
            return
        src = task.get("source_path")
        if not src or not Path(src).exists():
            self._show_tip("源文件不在了（被移动或删除？）", warn=True)
            return

        if not (secrets.get_api_key() or "").strip():
            QMessageBox.information(
                self, "还差一步",
                "分析需要调用大模型，请先到「设置」页填写百炼 API Key。\n\n"
                "（转录是本地免费的，但粗筛与精析要联网调模型）")
            return

        dlg = AnalyzeDialog(task, self)
        if dlg.exec() != QDialog.Accepted:
            return
        if self.on_analyze:
            self.on_analyze(tid, dlg.options(), dlg.auto_continue())

    @guard_ui
    def _ctx_menu(self, pos) -> None:
        tid = self._current_task_id()
        if not tid:
            return
        menu = QMenu(self)
        menu.addAction("分析…", lambda: self._analyze(tid))
        menu.addAction("查看候选清单", lambda: self.task_opened.emit(tid))
        menu.addSeparator()
        menu.addAction("打开所在文件夹", lambda: self._open_folder(tid))
        menu.addSeparator()
        menu.addAction("删除任务", lambda: self._delete(tid))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    @guard_ui
    def _open_folder(self, task_id: int) -> None:
        import os
        t = store.get_task(task_id)
        if not t or not t["source_path"]:
            return
        p = Path(t["source_path"])
        if p.exists():
            os.startfile(str(p.parent))     # noqa: S606  Windows 专用

    @guard_ui
    def _delete(self, task_id: int) -> None:
        t = store.get_task(task_id)
        if not t:
            return
        ok = QMessageBox.question(
            self, "删除任务",
            f"确定删除「{t['name']}」？\n（只删记录，不删源文件）")
        if ok == QMessageBox.Yes:
            store.delete_task(task_id)
            self.refresh()

    # ─────────────────────────────────────────────────────
    def _show_tip(self, text: str, warn: bool = False) -> None:
        self.tip.setText(text)
        self.tip.setStyleSheet(f"color:{theme.danger() if warn else theme.muted()};font-size:12px;")
        self.tip.setVisible(True)
