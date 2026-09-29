# -*- coding: utf-8 -*-
"""字幕样式设置页 —— 主/副字幕的完整样式面板 + 实时预览。

按产品设计稿复刻：

    字幕排布    （布局：译文在上/下、垂直间距）
    主字幕样式  （字体、字号、间距、颜色、边框颜色、边框大小）
    副字幕样式  （同上）
    预览设置    （预览文字长短、预览方向、预览背景图）
    样式管理    （选择 / 新建 / 删除 / 打开文件夹 / 恢复默认）

三个设计取舍：

1. **改动即时预览、自动落盘**（防抖 1 秒）。
   这个面板参数多，"改完忘了点保存"是最常见的抱怨；
   自动保存比多一个按钮更省事。顶部会显示保存状态，不让用户猜。

2. **字体可用性必须显式提示。**
   libass 在字体缺失时**静默 fallback**（实测：给了个不存在的字体名，
   渲染照样成功，只是用了默认字体）。用户看到的是"我改了字体但没变化" ——
   必须在界面上直接告诉他"这个字体没装"。

3. **预览画布尺寸跟随素材真实像素。**
   字号是按画布高度比例定位的，画布尺寸不对，字号观感就不对，
   预览也就失去了参考价值。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from ... import config, settings, subtitle_style
from ..style_preview import StylePreview
from ..workers import guard_ui
from .. import theme

# 自动保存延迟：用户连续拖动滑块时不要每次都写盘
_SAVE_DELAY_MS = 1000

class _ColorButton(QPushButton):
    """一个显示当前颜色的方块按钮，点击选色。"""

    def __init__(self, color: str, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(84, 28)
        self.setCursor(Qt.PointingHandCursor)
        self._color = subtitle_style.normalize_hex(color)
        self._apply()

    def _apply(self) -> None:
        self.setStyleSheet(
            f"background:{{self._color}};border:1px solid {theme.border()};"
            "border-radius:4px;")
        self.setToolTip(self._color)

    def color(self) -> str:
        return self._color

    def set_color(self, value: str) -> None:
        self._color = subtitle_style.normalize_hex(value)
        self._apply()

class StylePage(QWidget):
    """字幕样式设置页。"""

    def __init__(self, pool=None, parent=None) -> None:
        super().__init__(parent)
        self.pool = pool
        self._preset = subtitle_style.active_preset()
        self._loading = False
        self._bg_path: Path | None = None
        self._canvas: tuple[int, int] | None = None

        self._build()
        # 先把预设下拉填上 —— 漏了这一步的话，下拉是空的，
        # 用户看到"当前样式"一行什么都没有（实测踩到过）。
        self._refresh_preset_combo(self._preset.name)
        self._load_preset(self._preset, refresh_preview=True)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(_SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self._autosave)

    # ─────────────────────────────────────────────────────
    # 构建
    # ─────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        root.addLayout(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self._build_form_side(), 5)
        body.addWidget(self._build_preview_side(), 6)
        root.addLayout(body, 1)

    # ── 顶部：样式选择与保存状态 ──────────────────────────
    def _build_header(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(10)

        title = QLabel("字幕样式")
        title.setStyleSheet("font-size:20px;font-weight:600;")
        bar.addWidget(title)
        bar.addSpacing(12)

        bar.addWidget(QLabel("当前样式"))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(160)
        self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
        bar.addWidget(self.preset_combo)

        self.new_btn = QPushButton("新建样式")
        self.new_btn.clicked.connect(self._new_preset)
        bar.addWidget(self.new_btn)

        self.del_btn = QPushButton("删除")
        self.del_btn.clicked.connect(self._delete_preset)
        bar.addWidget(self.del_btn)

        self.open_dir_btn = QPushButton("打开样式文件夹")
        self.open_dir_btn.clicked.connect(self._open_styles_dir)
        bar.addWidget(self.open_dir_btn)

        self.reset_btn = QPushButton("恢复默认")
        self.reset_btn.clicked.connect(self._reset_preset)
        bar.addWidget(self.reset_btn)

        bar.addStretch(1)

        self.save_state = QLabel("")
        self.save_state.setStyleSheet(f"color:{theme.muted()};font-size:12px;")
        bar.addWidget(self.save_state)
        return bar

    # ── 左：参数表单 ──────────────────────────────────────
    def _build_form_side(self) -> QWidget:
        holder = QWidget()
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 8, 0)
        inner.setSpacing(12)

        inner.addWidget(self._build_layout_group())
        inner.addWidget(self._build_layer_group("main", "主字幕样式"))
        inner.addWidget(self._build_layer_group("sub", "副字幕样式"))
        inner.addWidget(self._build_preview_group())
        inner.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(holder)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return scroll

    def _build_layout_group(self) -> QGroupBox:
        box = QGroupBox("字幕排布")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)

        self.translate_combo = QComboBox()
        self.translate_combo.addItem("译文在下", False)
        self.translate_combo.addItem("译文在上", True)
        self.translate_combo.currentIndexChanged.connect(self._on_change)
        form.addRow("字幕布局", self.translate_combo)

        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(0, 400)
        self.gap_spin.setSuffix(" px")
        self.gap_spin.valueChanged.connect(self._on_change)
        form.addRow("垂直间距", self.gap_spin)

        hint = QLabel("「译文在下」时主字幕在上方；选「译文在上」则反过来。"
                      "垂直间距为两层之间的空隙。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.muted()};font-size:11px;")
        form.addRow("", hint)
        return box

    def _build_layer_group(self, key: str, title: str) -> QGroupBox:
        """构建一层字幕（主/副）的样式组。

        两层控件结构完全一样，只是绑定的数据不同 —— 所以用同一个
        构造函数生成，避免"主字幕改了某处、副字幕忘了改"这类不一致。
        """
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)

        prefix = "主字幕" if key == "main" else "副字幕"

        font = QComboBox()
        font.setEditable(True)          # 允许输入列表外的字体名
        font.setInsertPolicy(QComboBox.NoInsert)
        font.setMaxVisibleItems(22)
        font.addItems(subtitle_style.list_fonts())
        font.currentTextChanged.connect(self._on_change)
        form.addRow(f"{prefix}字体", font)

        font_warn = QLabel("")
        font_warn.setWordWrap(True)
        font_warn.setStyleSheet(f"color:{theme.warn()};font-size:11px;")
        font_warn.setVisible(False)
        form.addRow("", font_warn)

        size = QSpinBox()
        size.setRange(8, 200)
        size.valueChanged.connect(self._on_change)
        form.addRow(f"{prefix}字号", size)

        spacing = QDoubleSpinBox()
        spacing.setRange(-10.0, 40.0)
        spacing.setSingleStep(0.2)
        spacing.setDecimals(1)
        spacing.valueChanged.connect(self._on_change)
        form.addRow(f"{prefix}间距", spacing)

        color = _ColorButton(subtitle_style.DEFAULT_COLOR)
        color.clicked.connect(lambda _=False, k=key, b=color:
                              self._pick_color(k, "color", b))
        form.addRow(f"{prefix}颜色", color)

        outline_color = _ColorButton(subtitle_style.DEFAULT_OUTLINE_COLOR)
        outline_color.clicked.connect(
            lambda _=False, k=key, b=outline_color:
            self._pick_color(k, "outline_color", b))
        form.addRow(f"{prefix}边框颜色", outline_color)

        outline = QDoubleSpinBox()
        outline.setRange(0.0, 12.0)
        outline.setSingleStep(0.5)
        outline.setDecimals(1)
        outline.valueChanged.connect(self._on_change)
        form.addRow(f"{prefix}边框大小", outline)

        bold = QComboBox()
        bold.addItem("正常", False)
        bold.addItem("加粗", True)
        bold.currentIndexChanged.connect(self._on_change)
        form.addRow(f"{prefix}字重", bold)

        setattr(self, f"_{key}_font", font)
        setattr(self, f"_{key}_font_warn", font_warn)
        setattr(self, f"_{key}_size", size)
        setattr(self, f"_{key}_spacing", spacing)
        setattr(self, f"_{key}_color", color)
        setattr(self, f"_{key}_outline_color", outline_color)
        setattr(self, f"_{key}_outline", outline)
        setattr(self, f"_{key}_bold", bold)
        return box

    def _build_preview_group(self) -> QGroupBox:
        box = QGroupBox("预览设置")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)

        self.text_mode_combo = QComboBox()
        self.text_mode_combo.addItem("长文本", "long")
        self.text_mode_combo.addItem("短文本", "short")
        self.text_mode_combo.currentIndexChanged.connect(
            self._on_preview_option)
        form.addRow("预览文字", self.text_mode_combo)

        self.orient_combo = QComboBox()
        self.orient_combo.addItem("竖屏", "portrait")
        self.orient_combo.addItem("横屏", "landscape")
        self.orient_combo.currentIndexChanged.connect(self._on_preview_option)
        form.addRow("预览方向", self.orient_combo)

        bg_row = QHBoxLayout()
        self.bg_btn = QPushButton("选择图片")
        self.bg_btn.clicked.connect(self._pick_background)
        bg_row.addWidget(self.bg_btn)
        self.bg_clear_btn = QPushButton("清除")
        self.bg_clear_btn.clicked.connect(self._clear_background)
        bg_row.addWidget(self.bg_clear_btn)
        bg_row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(bg_row)
        form.addRow("预览背景", wrap)

        self.bg_label = QLabel("未选择（用纯色背景）")
        self.bg_label.setWordWrap(True)
        self.bg_label.setStyleSheet(f"color:{theme.muted()};font-size:11px;")
        form.addRow("", self.bg_label)

        note = QLabel("预览画布尺寸跟随素材的真实像素；"
                      "没有素材时按所选方向使用默认尺寸。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.muted()};font-size:11px;")
        form.addRow("", note)
        return box

    # ── 右：预览 ──────────────────────────────────────────
    def _build_preview_side(self) -> QWidget:
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        cap = QLabel("预览")
        cap.setStyleSheet("font-weight:600;")
        lay.addWidget(cap)

        self.preview = StylePreview(self.pool)
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay.addWidget(self.preview, 1)
        return holder

    # ─────────────────────────────────────────────────────
    # 数据绑定
    # ─────────────────────────────────────────────────────
    def _load_preset(self, preset: subtitle_style.SubtitlePreset,
                     *, refresh_preview: bool = True) -> None:
        """把预设写进控件。用 _loading 抑制回调，避免"加载即触发保存"。"""
        self._loading = True
        try:
            self._preset = preset
            self.translate_combo.setCurrentIndex(
                1 if preset.translate_on_top else 0)
            self.gap_spin.setValue(int(preset.vertical_gap))
            for key in ("main", "sub"):
                st = getattr(preset, key)
                getattr(self, f"_{key}_font").setCurrentText(st.font)
                getattr(self, f"_{key}_size").setValue(int(st.size))
                getattr(self, f"_{key}_spacing").setValue(float(st.spacing))
                getattr(self, f"_{key}_color").set_color(st.color)
                getattr(self, f"_{key}_outline_color").set_color(
                    st.outline_color)
                getattr(self, f"_{key}_outline").setValue(float(st.outline))
                getattr(self, f"_{key}_bold").setCurrentIndex(
                    1 if st.bold else 0)
                self._check_font(key)
        finally:
            self._loading = False

        if refresh_preview:
            self.preview.set_preset(preset)
            self.preview.refresh(immediate=True)

    def _collect(self) -> subtitle_style.SubtitlePreset:
        """从控件读回一个预设（不落盘）。"""
        p = self._preset

        def layer(key: str) -> subtitle_style.LayerStyle:
            return subtitle_style.LayerStyle(
                font=getattr(self, f"_{key}_font").currentText().strip(),
                size=getattr(self, f"_{key}_size").value(),
                spacing=getattr(self, f"_{key}_spacing").value(),
                color=getattr(self, f"_{key}_color").color(),
                outline_color=getattr(self, f"_{key}_outline_color").color(),
                outline=getattr(self, f"_{key}_outline").value(),
                bold=bool(getattr(self, f"_{key}_bold").currentData()),
            )

        p.translate_on_top = bool(self.translate_combo.currentData())
        p.vertical_gap = self.gap_spin.value()
        p.main = layer("main")
        p.sub = layer("sub")
        return p

    @guard_ui
    def _on_change(self, *_) -> None:
        if self._loading:
            return
        self._preset = self._collect()
        for key in ("main", "sub"):
            self._check_font(key)
        self.preview.set_preset(self._preset)
        self.save_state.setText("有改动，稍后自动保存…")
        self.save_state.setStyleSheet(f"color:{theme.warn()};font-size:12px;")
        self._save_timer.start()

    def _check_font(self, key: str) -> None:
        """字体缺失时给出明确提示。

        ⚠️ 这条提示是**必需的**：libass 找不到字体时不会报错，
           会静默换成默认字体渲染。没有提示的话，用户会觉得
           "我明明改了字体，怎么没反应"，然后怀疑整个功能坏了。
        """
        combo = getattr(self, f"_{key}_font")
        warn = getattr(self, f"_{key}_font_warn")
        name = combo.currentText().strip()
        if not name or subtitle_style.font_available(name):
            warn.setVisible(False)
            return
        suggestion = subtitle_style.suggest_font(name)
        warn.setText(f"⚠️ 系统里没有「{name}」这个字体，"
                     f"渲染时会自动换成「{suggestion}」。"
                     "请从下拉列表里选一个已安装的字体。")
        warn.setVisible(True)

    @guard_ui
    def _autosave(self) -> None:
        try:
            subtitle_style.save_as_active(self._preset)
            self.save_state.setText(f"已保存到「{self._preset.name}」")
            self.save_state.setStyleSheet(f"color:{theme.ok()};font-size:12px;")
        except Exception as exc:                       # noqa: BLE001
            self.save_state.setText(f"保存失败：{exc}")
            self.save_state.setStyleSheet(f"color:{theme.danger()};font-size:12px;")

    # ─────────────────────────────────────────────────────
    # 交互
    # ─────────────────────────────────────────────────────
    def _pick_color(self, key: str, field: str, button: _ColorButton) -> None:
        cur = QColor(button.color())
        c = QColorDialog.getColor(cur, self, "选择颜色")
        if not c.isValid():
            return
        button.set_color(c.name())
        self._on_change()

    def _on_preview_option(self, *_) -> None:
        self.preview.set_text_mode(self.text_mode_combo.currentData())
        self.preview.set_orientation(self.orient_combo.currentData())
        if self._canvas:
            self.preview.set_canvas(*self._canvas)

    def _pick_background(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择预览背景图", str(Path.home()),
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp)")
        if not path:
            return
        self._bg_path = Path(path)
        self.bg_label.setText(f"已选择：{self._bg_path.name}")
        self.preview.set_background(self._bg_path)

    def _clear_background(self) -> None:
        self._bg_path = None
        self.bg_label.setText("未选择（用纯色背景）")
        self.preview.set_background(None)

    @guard_ui
    def _on_preset_changed(self, name: str) -> None:
        if self._loading or not name:
            return
        # 切样式前先把当前改动落盘，否则切过去就丢了
        self._save_timer.stop()
        subtitle_style.save_as_active(self._preset)

        loaded = subtitle_style.load_preset(name)
        subtitle_style.set_active_preset(name)
        self._load_preset(loaded, refresh_preview=True)
        self.save_state.setText(f"已切换到「{name}」")
        self.save_state.setStyleSheet(f"color:{theme.ok()};font-size:12px;")

    @guard_ui
    def _new_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "新建样式", "样式名称：")
        if not ok or not name.strip():
            return
        name = subtitle_style.safe_name(name)
        if name in subtitle_style.list_presets():
            QMessageBox.warning(self, "已存在", f"样式「{name}」已存在，"
                                                "换个名字吧。")
            return
        # 从当前样式复制一份 —— 用户多半是想"在现有基础上微调"
        new_preset = self._collect().copy_as(name)
        subtitle_style.save_as_active(new_preset)
        self._refresh_preset_combo()
        self.preset_combo.setCurrentText(name)
        self._load_preset(new_preset, refresh_preview=True)
        QMessageBox.information(self, "已创建",
                                f"样式「{name}」已创建并设为当前样式。")

    @guard_ui
    def _delete_preset(self) -> None:
        name = self.preset_combo.currentText()
        if subtitle_style.safe_name(name) == "default":
            QMessageBox.information(self, "不可删除", "内置样式 default 不能删除。")
            return
        ok = QMessageBox.question(
            self, "删除样式", f"确定删除样式「{name}」？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ok != QMessageBox.Yes:
            return
        self._save_timer.stop()
        subtitle_style.delete_preset(name)
        self._refresh_preset_combo()
        self.preset_combo.setCurrentText("default")
        self._load_preset(subtitle_style.load_preset("default"),
                          refresh_preview=True)

    @guard_ui
    def _reset_preset(self) -> None:
        name = self.preset_combo.currentText()
        ok = QMessageBox.question(
            self, "恢复默认",
            f"把「{name}」恢复成出厂默认值？当前调整会丢失。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ok != QMessageBox.Yes:
            return
        self._save_timer.stop()
        preset = subtitle_style.reset_preset(name)
        self._load_preset(preset, refresh_preview=True)
        self._autosave()

    def _open_styles_dir(self) -> None:
        """在文件管理器里打开样式目录。"""
        config.ensure_dirs()
        path = config.STYLES_DIR
        try:
            import os
            os.startfile(str(path))          # Windows 专用；其他平台会抛错
        except Exception:                              # noqa: BLE001
            QMessageBox.information(self, "样式文件夹",
                                    f"样式存放在：\n{path}")

    # ─────────────────────────────────────────────────────
    # 外部接口
    # ─────────────────────────────────────────────────────
    def set_canvas_from_task(self, width: int, height: int) -> None:
        """让预览画布跟随某个素材的真实像素尺寸。"""
        if width > 0 and height > 0:
            self._canvas = (int(width), int(height))
            self.preview.set_canvas(*self._canvas)

    def refresh_all(self) -> None:
        """重新读当前预设（外部改了样式，或首次进入本页时调用）。"""
        name = subtitle_style.safe_name(
            str(settings.get("subtitle_preset", "default") or "default"))
        self._refresh_preset_combo(name)
        self._load_preset(subtitle_style.load_preset(name),
                          refresh_preview=True)

    def _refresh_preset_combo(self, select: str | None = None) -> None:
        """重建预设下拉列表。

        `select` 是期望选中的名字；缺省时保持当前选中项。
        整个过程在 `_loading` 保护下进行，避免"重建列表"被
        当成"用户切换了样式"而触发一次无意义的加载。
        """
        self._loading = True
        try:
            cur = select or self.preset_combo.currentText() or self._preset.name
            self.preset_combo.clear()
            self.preset_combo.addItems(subtitle_style.list_presets())
            if cur:
                self.preset_combo.setCurrentText(cur)
        finally:
            self._loading = False
