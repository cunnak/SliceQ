# -*- coding: utf-8 -*-
"""文案生成对话框（阶段 4-B · F9）。

给勾选的候选生成 3 条不同角度的标题 + 1 段发布简介，一键复制。

几个界面上的取舍：

1. **结果用只读文本框整块展示**，而不是每条一个卡片 ——
   用户真正的动作是"复制到发布框里"，中间不该有排版负担。
2. **费用要明说**。虽然实测一条片段约 ¥0.001，但"生成文案会调模型"
   这件事必须让用户先知道（BYOK 模式下这是他的钱）。
3. **逐条失败只影响那一条**，在结果里留一行说明，不中断整批。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QTextEdit, QVBoxLayout,
)

from ... import analyzer, asr, copywriter, store
from ..workers import guard_ui
from .. import theme

class CopyDialog(QDialog):
    """为勾选的候选生成标题与简介。"""

    def __init__(self, *, task: dict, clips: list[dict],
                 pool=None, parent=None) -> None:
        super().__init__(parent)
        self.task = task or {}
        self.clips = list(clips or [])
        self.pool = pool
        self._worker = None
        self._results: list[copywriter.CopyResult] = []

        self.setWindowTitle("生成标题与简介")
        self.setMinimumSize(640, 480)
        self._build()

    # ─────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        title = QLabel("生成标题与简介")
        title.setStyleSheet("font-size:16px;font-weight:600;")
        root.addWidget(title)

        n = len(self.clips)
        self.summary = QLabel(
            f"为勾选的 {n} 条片段各生成 "
            f"{copywriter.TITLE_COUNT} 条标题 + 1 段简介。"
            "生成过程会调用模型，费用极低（实测每条约 ¥0.001，"
            "以百炼账单为准）。")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"color:{theme.muted()};")
        root.addWidget(self.summary)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        root.addWidget(self.bar)

        self.status = QLabel("准备就绪")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color:{theme.muted()};")
        root.addWidget(self.status)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlaceholderText("生成结果会出现在这里，可整块复制。")
        self.text.setLineWrapMode(QTextEdit.WidgetWidth)
        root.addWidget(self.text, 1)

        btns = QHBoxLayout()
        self.copy_btn = QPushButton("复制全部")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy_all)
        btns.addWidget(self.copy_btn)
        btns.addStretch(1)
        self.start_btn = QPushButton("开始生成")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._start)
        btns.addWidget(self.start_btn)
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.reject)
        btns.addWidget(self.close_btn)
        root.addLayout(btns)

    # ─────────────────────────────────────────────────────
    @guard_ui
    def _start(self) -> None:
        if not self.clips:
            self.status.setText("没有勾选任何片段。")
            return
        task_id = int(self.task.get("id") or 0)

        segs = [asr.Segment(start_ms=r["start_ms"], end_ms=r["end_ms"],
                            text=r["text"])
                for r in store.list_transcript_segs(task_id)]

        self._set_running(True)
        self.text.clear()

        def job(progress=None, should_cancel=None):
            # 没配 key 时说清楚，而不是抛一个看不懂的异常
            try:
                tport = analyzer.make_transport()
            except Exception as exc:             # noqa: BLE001
                raise RuntimeError(
                    "无法初始化模型调用：" + str(exc) +
                    "\n请到「设置」页检查 API key 与端点。") from exc
            if not getattr(tport, "api_key", ""):
                raise RuntimeError(
                    "未配置 API key，无法生成文案。\n"
                    "请到「设置」页填写百炼 key（BYOK，费用记在你的账号上）。")
            return copywriter.write_for_clips(
                self.clips, segs, transport=tport,
                progress=progress, should_cancel=should_cancel)

        self._worker = self.pool.run(
            job, with_progress=True,
            on_done=self._done, on_error=self._failed,
            on_progress=self._progress)

    @guard_ui
    def _progress(self, done: int, total: int, desc: str) -> None:
        self.bar.setValue(max(0, min(100, int(done))))
        if desc:
            self.status.setText(desc)
            self.status.setStyleSheet(f"color:{theme.muted()};")

    @guard_ui
    def _done(self, results) -> None:
        self._set_running(False)
        if not isinstance(results, list):
            self.status.setText("生成结束，但返回结果异常。")
            return

        self._results = [r for r in results if isinstance(r, copywriter.CopyResult)]
        self.text.setPlainText(copywriter.to_text(self._results))
        self.bar.setValue(100)
        self.copy_btn.setEnabled(bool(self.text.toPlainText().strip()))

        cost = sum(r.cost_cny for r in self._results)
        notes = [n for r in self._results for n in r.notes]
        msg = (f"完成：{len(self._results)} 条｜本次花费约 ¥{cost:.4f}"
               f"（以百炼账单为准）")
        if notes:
            msg += f"\n注意：{'；'.join(dict.fromkeys(notes))[:200]}"
            self.status.setStyleSheet(f"color:{theme.warn()};")
        else:
            self.status.setStyleSheet(f"color:{theme.ok()};")
        self.status.setText(msg)

    @guard_ui
    def _failed(self, msg: str, detail: str = "") -> None:
        self._set_running(False)
        self.status.setText(msg.splitlines()[0][:200])
        self.status.setStyleSheet(f"color:{theme.danger()};")

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.start_btn.setText("生成中…" if running else "开始生成")
        self.close_btn.setEnabled(not running)

    @guard_ui
    def _copy_all(self) -> None:
        body = self.text.toPlainText()
        if not body.strip():
            return
        QApplication.clipboard().setText(body)
        self.status.setText("已复制到剪贴板。")
        self.status.setStyleSheet(f"color:{theme.ok()};")

    def reject(self) -> None:                      # noqa: D102  (Qt 回调)
        # 生成中不许关掉 —— 会把请求留在后台（也白花钱）
        if self.start_btn.isEnabled():
            super().reject()
        else:
            self.status.setText("正在生成，请等它结束。")
            self.status.setStyleSheet(f"color:{theme.warn()};")
