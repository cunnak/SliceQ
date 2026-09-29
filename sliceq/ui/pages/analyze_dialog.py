# -*- coding: utf-8 -*-
"""分析配置对话框 —— 需求输入 + 筛选强度 + 花费上限预估。

设计依据 TECH-DESIGN §2.5：

  · **启动时给上限，不给典型值**（原则 2：预期要往差的方向偏）。
    预估 ¥0.53 实花 ¥0.30，用户觉得赚；反过来就是被骗感。
  · **「筛选强度」把 20% 硬阀翻译成用户能理解的取舍** ——
    从"隐藏的技术限制"变成"我最多精读多少、付多少钱"。
  · **用 ¥ 说话**，token 数塞进「详情」折叠区（留给想核算的人）。

⚠️ 这里给的是**估算**。BYOK 模式下真实扣费在阿里云后台，
   界面上必须注明「以百炼账单为准」，否则用户发现对不上会失去信任。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QRadioButton, QToolButton, QVBoxLayout,
)

from ... import analyzer, config, prompts, pipeline, settings
from .. import theme

# 筛选强度三档。数字来自 TECH-DESIGN §2.5.5
LEVELS: list[tuple[str, float, str]] = [
    ("轻", 0.10, "只挑最像的，花费最低"),
    ("标准", 0.20, "推荐，多数场景够用"),
    ("广", 0.35, "宁可多找，花费最高"),
]


class AnalyzeDialog(QDialog):
    """返回 (Options, auto_continue)。取消时 exec() 返回 Rejected。"""

    def __init__(self, task: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("分析设置")
        self.setMinimumWidth(560)
        self.task = task
        self.duration = float(task.get("duration_sec") or 0.0)
        self._build()
        self._update_estimate()

    # ─────────────────────────────────────────────────────
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        name = self.task.get("name") or "未命名"
        dur_min = self.duration / 60
        head = QLabel(f"<b>{name}</b><br>"
                      f"<span style='color:{theme.muted()};font-size:12px'>"
                      f"时长 {dur_min:.1f} 分钟</span>")
        head.setTextFormat(Qt.RichText)
        lay.addWidget(head)

        # ── 需求输入 ──────────────────────────────────
        lay.addWidget(QLabel("你想要什么样的片段？"))
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(
            "例：找主播讲得最激动的几段，或者有笑点的地方。\n"
            "写得越具体，挑得越准。")
        self.prompt_edit.setFixedHeight(96)
        lay.addWidget(self.prompt_edit)

        row = QHBoxLayout()
        row.addWidget(QLabel("快速模板："))
        for key in ("游戏", "娱乐", "通用"):
            b = QPushButton(key)
            b.setFixedWidth(64)
            b.clicked.connect(lambda _=False, k=key: self._apply_template(k))
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        # ── 筛选强度 ──────────────────────────────────
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:rgba(128,128,128,0.25);")
        lay.addWidget(line)

        lay.addWidget(QLabel("筛选强度"))
        hint = QLabel("决定最多精读多少内容 —— 也就决定了这一轮花多少钱。")
        hint.setStyleSheet(f"color:{theme.muted()};font-size:12px;")
        lay.addWidget(hint)

        lv_row = QHBoxLayout()
        self._level_group = QButtonGroup(self)
        for i, (label, ratio, desc) in enumerate(LEVELS):
            rb = QRadioButton(f"{label}（{int(ratio * 100)}%）")
            rb.setToolTip(desc)
            self._level_group.addButton(rb, i)
            lv_row.addWidget(rb)
            if i == 1:                      # 标准为默认
                rb.setChecked(True)
        lv_row.addStretch(1)
        lay.addLayout(lv_row)
        self._level_group.idToggled.connect(lambda *_: self._update_estimate())

        # ── 花费预估 ──────────────────────────────────
        box = QFrame()
        box.setStyleSheet(
            "QFrame{background:rgba(55,138,221,0.10);"
            "border:1px solid rgba(55,138,221,0.35);border-radius:10px;}")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(14, 12, 14, 12)
        bl.setSpacing(4)

        self.cost_top = QLabel("—")
        self.cost_top.setStyleSheet("font-size:15px;font-weight:500;border:none;")
        self.cost_top.setTextFormat(Qt.RichText)
        bl.addWidget(self.cost_top)

        self.cost_sub = QLabel("—")
        self.cost_sub.setStyleSheet(
            f"color:{theme.muted()};font-size:12px;border:none;")
        self.cost_sub.setWordWrap(True)
        bl.addWidget(self.cost_sub)

        self.detail_btn = QToolButton()
        self.detail_btn.setText("详情 ▾")
        self.detail_btn.setStyleSheet(
            f"QToolButton{border:none;color:{theme.muted()};font-size:12px;}")
        self.detail_btn.setCheckable(True)
        self.detail_btn.toggled.connect(self._toggle_detail)
        bl.addWidget(self.detail_btn)

        self.detail_lbl = QLabel("")
        self.detail_lbl.setStyleSheet(
            f"color:{theme.muted()};font-size:12px;border:none;")
        self.detail_lbl.setVisible(False)
        bl.addWidget(self.detail_lbl)

        lay.addWidget(box)

        # ── 工作方式 ──────────────────────────────────
        self.auto_cont = QRadioButton("一路跑完（不中途问我）")
        self.ask_first = QRadioButton("先给我看候选清单，我自己决定要不要精析")
        self.ask_first.setChecked(True)
        lay.addWidget(self.auto_cont)
        lay.addWidget(self.ask_first)

        # ── 按钮 ─────────────────────────────────────
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("开始分析")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # ─────────────────────────────────────────────────────
    def _apply_template(self, key: str) -> None:
        self.prompt_edit.setPlainText(prompts.BUILTIN_TEMPLATES[key])

    def _ratio(self) -> float:
        idx = self._level_group.checkedId()
        return LEVELS[idx if 0 <= idx < len(LEVELS) else 1][1]

    def _toggle_detail(self, on: bool) -> None:
        self.detail_lbl.setVisible(on)
        self.detail_btn.setText("详情 ▴" if on else "详情 ▾")

    def _update_estimate(self) -> None:
        """随档位实时更新上限预估。

        ⚠️ 展示的是**最坏情况**，并明说"最多" —— 用户对"最多"的容忍度
           远高于对"大概"的容忍度。
        """
        ratio = self._ratio()
        est = analyzer.estimate_total_upper_bound(self.duration, ratio)
        mins = est["candidate_seconds"] / 60

        self.cost_top.setText(
            f"本轮花费上限 <span style='font-size:18px'>¥{est['total']:.2f}</span>")
        self.cost_sub.setText(
            f"按「最多精读 {mins:.0f} 分钟内容」估算的最坏情况。"
            f"实际通常低于这个数。")

        # 预估时长：转录是本地 GPU 的大头（实测 ≈ 时长的 0.13 倍），
        # 网络调用部分只能给粗略量级
        transcribe_min = self.duration * 0.13 / 60
        total_min = transcribe_min + 5
        self.detail_lbl.setText(
            f"转录（本地，免费）　约 {transcribe_min:.0f} 分钟\n"
            f"粗筛（文本 + 抽帧）　约 ¥{est['screening']:.2f}\n"
            f"精析（{mins:.0f} 分钟候选段）　约 ¥{est['refine']:.2f}\n"
            f"文案　约 ¥{est['caption']:.2f}\n"
            f"合计约 {total_min:.0f} 分钟\n\n"
            f"注：以上为估算，实际扣费以百炼账单为准。")

    # ─────────────────────────────────────────────────────
    def options(self) -> pipeline.Options:
        return pipeline.Options(
            profile=self.prompt_edit.toPlainText().strip(),
            candidate_ratio=self._ratio(),
            # 并发度是"性能偏好"，放设置页；这里读回来即可
            concurrency=int(settings.get("concurrency", 1) or 1),
        )

    def auto_continue(self) -> bool:
        return self.auto_cont.isChecked()
