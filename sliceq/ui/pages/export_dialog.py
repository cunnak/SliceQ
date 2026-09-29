# -*- coding: utf-8 -*-
"""导出对话框 —— 把勾选的候选片段导成带字幕的成片。

## 产出形态（乙方案）

每条候选 → **一个独立的 mp4**。切片 man 要的是能单独发布的一条条短视频，
而不是一个几十分钟的合集。

副作用是时间轴重映射退化成"整体平移一个偏移量"（见 `subtitle.remap_to_clip`），
少了一整类"不报错但字幕全错位"的风险。

## 三件必须在界面上讲清的事

1. **快速模式（流复制）不能烧字幕** —— 烧字幕必须重编码像素。
   两个都选时自动升档到精确模式，并**明确告知**，而不是悄悄把字幕丢掉。
2. **两种模式的精度差异**（都是实测值）：
   精确 → 时长偏差 0.000s；快速 → 偏差 0.10~0.15s，但画面内容与源逐像素一致。
3. **导出目录会新建一个带时间戳的子目录**，不往用户选的目录里倒一堆文件 ——
   连续导出几次就分不清哪批是哪批了。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QRadioButton, QVBoxLayout,
)

from ... import (analyzer, asr, config, editor, enhance, exporter, prompts,
                 secrets, settings, store, subtitle_style)
from ..workers import guard_ui
from .. import theme

class ExportDialog(QDialog):
    """导出对话框。"""

    def __init__(self, *, task: dict, clips: list[dict],
                 pool=None, parent=None) -> None:
        super().__init__(parent)
        self.task = task or {}
        self.clips = list(clips or [])
        self.pool = pool
        self._worker = None
        self._result: editor.ExportResult | None = None
        self._draft_result: exporter.DraftResult | None = None
        # 「打开输出文件夹」要打开哪个目录 —— 只出草稿时不能仍指向成片目录
        self._out_path: Path | None = None
        # 控件的可用状态有两个独立来源：① 是不是正在导出（运行态）
        # ② 业务上允不允许（比如没有转录文本时压根不能烧字幕）。
        # 必须分开记 —— 否则「导出结束」时无条件重新启用，
        # 会把业务层的禁用悄悄冲掉，用户就能勾一个根本做不到的选项。
        self._running = False
        self._burn_available = True

        self.setWindowTitle("导出成片")
        self.setMinimumWidth(560)
        self._build()
        self._update_summary()

    # ─────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        title = QLabel("导出")
        title.setStyleSheet("font-size:17px;font-weight:600;")
        root.addWidget(title)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"color:{theme.muted()};")
        root.addWidget(self.summary)

        # ── 导出内容 ──────────────────────────────────
        #
        # ⚠️ 这一组决定**后面几组的适用性**：不勾「成片片段」时，
        #    切片模式 / 增强 / 双语那几组在业务上完全无关，
        #    必须一起置灰 —— 否则用户设了半天增强却发现根本没生效。
        content_box = QGroupBox("导出内容")
        content_lay = QVBoxLayout(content_box)

        self.clips_check = QCheckBox("成片片段（每条候选一个 mp4 + 字幕文件）")
        self.clips_check.setChecked(True)          # 默认保持既有行为
        self.clips_check.setToolTip(
            "发布用的成品：切好的视频，可直接上传。")
        content_lay.addWidget(self.clips_check)

        self.jy_check = QCheckBox("剪映草稿（拿去精修）")
        self.jy_check.setToolTip(
            "把切点 / 字幕 / 标题写进剪映草稿，在剪映里继续精修。\n"
            "⚠️ 草稿不带 SliceQ 里的字幕样式；成片要在剪映里手动导出。")
        content_lay.addWidget(self.jy_check)

        self.dv_check = QCheckBox("达芬奇时间线（拿去精修）")
        self.dv_check.setToolTip(
            "导出 .otio 时间线 + 并行的 .srt 字幕文件。\n"
            "⚠️ 首次导入达芬奇会提示「找不到片段」，按附带的"
            "「导入说明.txt」操作即可。")
        content_lay.addWidget(self.dv_check)

        self.content_note = QLabel("")
        self.content_note.setWordWrap(True)
        self.content_note.setStyleSheet(f"color:{theme.muted()};font-size:12px;")
        content_lay.addWidget(self.content_note)
        root.addWidget(content_box)

        for w in (self.clips_check, self.jy_check, self.dv_check):
            w.stateChanged.connect(self._on_content_changed)

        # ── 输出位置 ──
        out_box = QGroupBox("输出位置")
        out_lay = QHBoxLayout(out_box)
        self.out_edit = QLabel("")
        self.out_edit.setWordWrap(True)
        self.out_edit.setStyleSheet(f"color:{theme.border()};")
        out_lay.addWidget(self.out_edit, 1)
        pick = QPushButton("选择目录")
        pick.clicked.connect(self._pick_dir)
        out_lay.addWidget(pick)
        root.addWidget(out_box)

        # ── 模式 ──
        mode_box = QGroupBox("切片模式")
        mode_lay = QVBoxLayout(mode_box)
        self.rb_exact = QRadioButton("精确（重新编码，时长精确到 0.000 秒）")
        self.rb_fast = QRadioButton("快速（流复制，秒出；切点有 0.1~0.15 秒偏差）")
        self.rb_exact.setChecked(True)
        self.rb_exact.toggled.connect(self._on_mode_changed)
        mode_lay.addWidget(self.rb_exact)
        mode_lay.addWidget(self.rb_fast)

        self.burn_check = QCheckBox("烧录字幕（把字幕印进画面）")
        self.burn_check.setChecked(True)
        self.burn_check.toggled.connect(self._on_mode_changed)
        mode_lay.addWidget(self.burn_check)

        self.mode_note = QLabel("")
        self.mode_note.setWordWrap(True)
        self.mode_note.setStyleSheet(f"color:{theme.warn()};font-size:12px;")
        mode_lay.addWidget(self.mode_note)
        root.addWidget(mode_box)

        # ── 成片增强（阶段 4）──
        # 默认全部"不做"，所以不勾任何项时行为与阶段 3 完全一致 ——
        # 增强是加法，不能悄悄改变老用户已经习惯的导出结果。
        enh_box = QGroupBox("成片增强（可选）")
        enh_lay = QVBoxLayout(enh_box)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("背景音乐"))
        self.bgm_combo = QComboBox()
        self.bgm_combo.setMinimumWidth(180)
        self.bgm_combo.currentIndexChanged.connect(self._on_enhance_changed)
        row1.addWidget(self.bgm_combo, 1)
        self.bgm_dir_btn = QPushButton("打开音乐目录")
        self.bgm_dir_btn.clicked.connect(self._open_bgm_dir)
        row1.addWidget(self.bgm_dir_btn)
        enh_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("配乐音量"))
        self.bgm_vol_combo = QComboBox()
        for db in (-6, -9, -12, -15, -18, -24):
            self.bgm_vol_combo.addItem(f"{db} dB", db)
        self.bgm_vol_combo.setCurrentIndex(3)          # -15 dB
        row2.addWidget(self.bgm_vol_combo)
        row2.addSpacing(12)
        row2.addWidget(QLabel("让位强度"))
        self.duck_combo = QComboBox()
        for key, cfg in enhance.DUCK_LEVELS.items():
            self.duck_combo.addItem(cfg["label"], key)
            # 术语留在提示里，标题上只用「让位强度」这种听得懂的说法
            idx = self.duck_combo.count() - 1
            self.duck_combo.setItemData(idx, cfg["desc"], Qt.ToolTipRole)
        self.duck_combo.setCurrentIndex(
            list(enhance.DUCK_LEVELS).index(enhance.DUCK_LEVEL_DEFAULT))
        row2.addWidget(self.duck_combo)
        row2.addStretch(1)
        enh_lay.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("片头淡入"))
        self.fade_in_combo = QComboBox()
        for v in (0.0, 0.3, 0.5, 1.0, 1.5):
            self.fade_in_combo.addItem("不做" if v == 0 else f"{v:g} 秒", v)
        row3.addWidget(self.fade_in_combo)
        row3.addSpacing(12)
        row3.addWidget(QLabel("片尾淡出"))
        self.fade_out_combo = QComboBox()
        for v in (0.0, 0.3, 0.5, 1.0, 1.5):
            self.fade_out_combo.addItem("不做" if v == 0 else f"{v:g} 秒", v)
        # ⚠️ 默认**全部是「不做」**。片尾淡出 0.5s 虽然好看，但不能是默认值：
        #    那会让老用户重导出时画面莫名多出一段黑场，
        #    违背"增强是显式加法"这条原则。想加就自己勾。
        row3.addWidget(self.fade_out_combo)
        row3.addStretch(1)
        enh_lay.addLayout(row3)

        self.cover_check = QCheckBox("同时生成封面图（1280×720，标题取片段摘要）")
        enh_lay.addWidget(self.cover_check)

        self.enh_note = QLabel("")
        self.enh_note.setWordWrap(True)
        self.enh_note.setStyleSheet(f"color:{theme.muted()};font-size:12px;")
        enh_lay.addWidget(self.enh_note)
        root.addWidget(enh_box)
        self._fill_bgm()

        # ── 双语字幕（阶段 4-B）──
        # 译文只走模型精翻：实测免费通道撑不起交付（Google 被墙 / 必应 401 /
        # MyMemory 质量不可用），而模型翻译 255 字才 ¥0.0008。
        # 但**仍要告诉用户会产生费用**，哪怕金额很小。
        bi_box = QGroupBox("双语字幕（可选）")
        bi_lay = QVBoxLayout(bi_box)
        bi_row = QHBoxLayout()
        self.bi_check = QCheckBox("生成双语字幕（原文 + 译文）")
        self.bi_check.toggled.connect(self._on_enhance_changed)
        bi_row.addWidget(self.bi_check)
        bi_row.addSpacing(12)
        bi_row.addWidget(QLabel("译文语言"))
        self.lang_combo = QComboBox()
        for code, name in prompts.LANG_NAMES.items():
            if code == "zh":
                continue                      # 把中文当译文没有意义
            self.lang_combo.addItem(name, code)
        bi_row.addWidget(self.lang_combo)
        bi_row.addStretch(1)
        bi_lay.addLayout(bi_row)
        self.bi_note = QLabel("")
        self.bi_note.setWordWrap(True)
        self.bi_note.setStyleSheet(f"color:{theme.muted()};font-size:12px;")
        bi_lay.addWidget(self.bi_note)
        root.addWidget(bi_box)

        # ── 进度 ──
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        root.addWidget(self.bar)

        self.status = QLabel("准备就绪")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color:{theme.muted()};")
        root.addWidget(self.status)

        # ── 按钮 ──
        btns = QHBoxLayout()
        self.open_btn = QPushButton("打开输出文件夹")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self._open_folder)
        btns.addWidget(self.open_btn)
        btns.addStretch(1)
        self.start_btn = QPushButton("开始导出")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._start)
        btns.addWidget(self.start_btn)
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.reject)
        btns.addWidget(self.close_btn)
        root.addLayout(btns)

        default_dir = str(settings.get("export_dir", "") or config.EXPORT_DIR)
        self._out_base = Path(default_dir)

    # ─────────────────────────────────────────────────────
    def _update_summary(self) -> None:
        n = len(self.clips)
        total = sum(float(c.get("end") or 0) - float(c.get("start") or 0)
                    for c in self.clips)
        segs = []
        try:
            segs = store.list_transcript_segs(int(self.task.get("id") or 0))
        except Exception:
            pass

        self.summary.setText(
            f"任务「{self.task.get('name', '未命名')}」　·　"
            f"已勾选 {n} 条候选　·　时长合计 {total / 60:.1f} 分钟")
        self.out_edit.setText(f"{self._out_base}")

        if not segs:
            # 没有转录分段 = 做不出字幕。这不该发生（分析阶段已落库），
            # 但真出现时必须**明确告知**，而不是导出一堆没字幕的视频。
            self._burn_available = False
            self.burn_check.setChecked(False)
        else:
            self._burn_available = True
        self._sync_enabled()
        self._on_mode_changed()

    @guard_ui
    def _on_mode_changed(self, *_) -> None:
        fast = self.rb_fast.isChecked()
        burn = self.burn_check.isChecked()
        enh = self._current_enhance()
        enhanced = not enh.is_noop()

        # ⚠️ 「没有转录文本」的警告优先级最高，必须**先返回**。
        #    否则下面那些常规提示会把它覆盖掉 ——
        #    用户会看到"不烧字幕：会另外导出 .srt"这种话，
        #    而真正的问题（这个任务根本没有转录数据）一个字都看不到。
        if not self._burn_available:
            self.mode_note.setText(
                "⚠️ 这个任务没有保存转录文本，无法生成字幕。"
                "若要带字幕导出，请先重新跑一次分析。"
                "（淡入淡出与配乐不受影响，仍可正常使用。）")
            return
        # 多个"必须重编码"的理由同时成立时，要说全，不能只说最上面那条
        reasons = []
        if fast and burn:
            reasons.append(
                "烧字幕需要重新编码画面，已自动改用「精确」模式。"
                "（想更快的话，取消勾选「烧录字幕」——"
                "导出的 SRT 文件仍可用于其它软件。）")
        if fast and enhanced:
            reasons.append(
                "淡入淡出与配乐属于滤镜处理，流复制做不到，已自动改用「精确」模式。")

        if reasons:
            self.mode_note.setText(" ".join(reasons))
        elif fast:
            self.mode_note.setText(
                "快速模式不重新编码，几秒完成。切点可能有 0.1~0.15 秒偏差，"
                "但画面内容与源片完全一致。")
        elif not burn:
            self.mode_note.setText(
                "不烧字幕：会另外导出一个 .srt 字幕文件，"
                "方便在剪映/达芬奇等软件里自行加载。")
        else:
            self.mode_note.setText("")

    # ─────────────────────────────────────────────────────
    # 成片增强
    # ─────────────────────────────────────────────────────
    @guard_ui
    def _fill_bgm(self) -> None:
        """填充 BGM 下拉。空库时要**说清为什么空**，而不是给个光秃秃的下拉。"""
        cur = self.bgm_combo.currentData() if self.bgm_combo.count() else ""
        self.bgm_combo.blockSignals(True)
        self.bgm_combo.clear()
        self.bgm_combo.addItem("不配乐", "")
        items = enhance.list_bgm()
        for it in items:
            self.bgm_combo.addItem(it["name"], it["name"])
        if cur:
            i = self.bgm_combo.findData(cur)
            if i >= 0:
                self.bgm_combo.setCurrentIndex(i)
        self.bgm_combo.blockSignals(False)

        if not items:
            self.enh_note.setText(
                "背景音乐目录目前是空的。点「打开音乐目录」把音乐文件放进去后，"
                "重新打开本对话框即可选用。\n"
                "SliceQ 不内置音乐 —— 音频版权不像字体那样附一份许可就能随包分发，"
                "所以请自备（目录里的《使用说明》列了几个合法来源）。")
        else:
            self._on_enhance_changed()

    @guard_ui
    def _open_bgm_dir(self) -> None:
        d = enhance.bgm_dir()
        try:
            enhance.ensure_bgm_readme()
            import os
            os.startfile(str(d))
        except BaseException:                      # noqa: BLE001
            self.enh_note.setText(f"音乐目录：{d}")

    def _current_enhance(self) -> enhance.EnhanceOptions:
        opts = enhance.EnhanceOptions(
            enabled=True,
            fade_in=float(self.fade_in_combo.currentData() or 0),
            fade_out=float(self.fade_out_combo.currentData() or 0),
            bgm=str(self.bgm_combo.currentData() or ""),
            bgm_volume_db=float(self.bgm_vol_combo.currentData() or -15),
        )
        enhance.apply_duck_level(opts, str(self.duck_combo.currentData()
                                           or enhance.DUCK_LEVEL_DEFAULT))
        return opts

    @guard_ui
    def _on_enhance_changed(self, *_) -> None:
        opts = self._current_enhance()
        if opts.is_noop():
            self.enh_note.setText("")
        elif opts.bgm:
            lvl = enhance.DUCK_LEVELS.get(
                enhance.duck_level_of(opts), enhance.DUCK_LEVELS["normal"])
            self.enh_note.setText(
                f"已选音乐「{opts.bgm}」，音量 {opts.bgm_volume_db:g}dB，"
                f"{lvl['label']}：{lvl['desc']}。"
                "音乐会循环填充到片段长度。")
        else:
            self.enh_note.setText("只做淡入淡出，不加背景音乐。")

        # 双语字幕的说明：既要讲清效果，也要讲清成本与前置条件
        if self.bi_check.isChecked():
            has_key = bool(secrets.get_api_key())
            lang = self.lang_combo.currentText()
            if has_key:
                self.bi_note.setText(
                    f"译文由模型精翻并逐条与原文对齐，输出成 {lang}。"
                    "翻译会产生少量 API 费用（实测每分钟字幕约 ¥0.001，"
                    "以百炼账单为准）。字幕会包含原文与译文两行，"
                    "两者样式可在「字幕样式」页分别调整。")
            else:
                self.bi_note.setText(
                    "⚠️ 未配置 API key，无法调用模型翻译。"
                    "可用的免费网页翻译接口实测已全部失效（或质量不可用），"
                    "因此双语字幕需要先在「设置」里填写百炼 key。")
        else:
            self.bi_note.setText("")
        self._on_mode_changed()

    @guard_ui
    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择导出目录",
                                             str(self._out_base))
        if d:
            self._out_base = Path(d)
            self.out_edit.setText(str(self._out_base))

    # ─────────────────────────────────────────────────────
    @guard_ui
    def _start(self) -> None:
        if not self.clips:
            self.status.setText("没有勾选任何候选片段。")
            return

        task_id = int(self.task.get("id") or 0)
        video = self.task.get("source_path") or ""
        if not video or not Path(video).exists():
            self.status.setText(
                f"源视频不在了：{video}\n"
                "导出需要读源片。请确认它没被移动或删除。")
            self.status.setStyleSheet(f"color:{theme.danger()};")
            return

        # 读转录分段（落库的那份），转成 asr.Segment
        segs = [asr.Segment(start_ms=r["start_ms"], end_ms=r["end_ms"],
                            text=r["text"])
                for r in store.list_transcript_segs(task_id)]

        settings.set("export_dir", str(self._out_base))

        mode = "fast" if self.rb_fast.isChecked() else "exact"
        burn = self.burn_check.isChecked()
        w = int(self.task.get("width") or 0)
        h = int(self.task.get("height") or 0)
        enh_opts = self._current_enhance()
        make_covers = self.cover_check.isChecked()
        bilingual = self.bi_check.isChecked()
        target_lang = str(self.lang_combo.currentData() or "en")

        want_clips = self.clips_check.isChecked()
        want_jy = self.jy_check.isChecked()
        want_dv = self.dv_check.isChecked()
        if not (want_clips or want_jy or want_dv):
            self.status.setText("至少勾选一项导出内容。")
            self.status.setStyleSheet(f"color:{theme.warn()};")
            return

        self._set_running(True)
        self.result_note: list[str] = []

        def job(progress=None, should_cancel=None):
            # transport 在**工作线程里**装配：它会读配置/解密凭据，
            # 放在 UI 线程做会卡一下（虽然很短，但没理由）。
            tport = None
            if bilingual or want_jy or want_dv:
                try:
                    tport = analyzer.make_transport()
                except Exception:                # noqa: BLE001
                    tport = None
            out: dict = {}

            # ── 成片（占进度 0~60）────────────────────
            if want_clips:
                def _p1(done=0, total=100, desc="", **_kw):
                    if progress:
                        progress(int(done * 0.6), 100, desc)
                out["clips"] = editor.export_clips(
                    video, self.clips, out_base=self._out_base,
                    task_id=task_id,
                    task_name=str(self.task.get("name") or "任务"),
                    mode=mode, burn_subtitles=burn, segments=segs,
                    preset=subtitle_style.active_preset(),
                    width=w, height=h,
                    enhance=enh_opts, make_covers=make_covers,
                    bilingual=bilingual, translate_transport=tport,
                    target_lang=target_lang,
                    progress=_p1, should_cancel=should_cancel,
                    mode_note=self.result_note)

            # ── 草稿（占进度 60~100）──────────────────
            if want_jy or want_dv:
                def _p2(done=0, total=100, desc="", **_kw):
                    if progress:
                        progress(60 + int(done * 0.4), 100, desc)
                out["drafts"] = exporter.export_drafts(
                    video, self.clips, task_id=task_id,
                    task_name=str(self.task.get("name") or "任务"),
                    out_base=(Path(self._out_base) / "草稿导出"),
                    want_jianying=want_jy, want_davinci=want_dv,
                    segments=segs, preset=subtitle_style.active_preset(),
                    transport=tport, bilingual=bilingual,
                    target_lang=target_lang,
                    fallback_mode=mode, burn_subtitles=burn,
                    width=w, height=h,
                    progress=_p2, should_cancel=should_cancel)
            return out

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
    def _done(self, result) -> None:
        self._set_running(False)
        self.bar.setValue(100)
        self.open_btn.setEnabled(True)

        # ⚠️ job 现在返回 dict（成片 / 草稿两种产出可能同时存在）。
        #    仍兼容单结果 —— 别处调用可能只传一个 ExportResult。
        if isinstance(result, dict):
            clips_res = result.get("clips")
            drafts_res = result.get("drafts")
        else:
            clips_res, drafts_res = result, None

        parts: list[str] = []
        all_ok = True
        if isinstance(clips_res, editor.ExportResult):
            self._result = clips_res
            parts.append(self._clips_text(clips_res))
            all_ok = all_ok and not clips_res.fail_count and not clips_res.cancelled
        if isinstance(drafts_res, exporter.DraftResult):
            self._draft_result = drafts_res
            parts.append(self._drafts_text(drafts_res))
            all_ok = all_ok and drafts_res.ok
            if drafts_res.out_dir:
                self._out_path = drafts_res.out_dir
        elif isinstance(clips_res, editor.ExportResult):
            self._out_path = clips_res.out_dir

        if not parts:
            self.status.setText("导出结束，但没有任何产出。")
            self.status.setStyleSheet(f"color:{theme.warn()};")
            return
        self.status.setText("\n".join(p for p in parts if p))
        self.status.setStyleSheet(
            f"color:{theme.ok() if all_ok else theme.warn()};")

    def _clips_text(self, result: editor.ExportResult) -> str:
        """成片导出的结果文本。"""
        notes = ""
        if result.items and any(not i.ok for i in result.items):
            fails = [i for i in result.items if not i.ok]
            notes = "\n失败：" + "；".join(
                f"{f.title}（{f.error.splitlines()[0][:40]}）" for f in fails[:3])

        # 逐条的非致命提示（字体被替换 / BGM 找不到…）必须浮上来，
        # 否则用户只会觉得"怎么没效果"，而不知道发生了什么
        warns = [n for i in result.items for n in i.notes]
        if warns:
            uniq = list(dict.fromkeys(warns))
            notes += "\n提示：" + "；".join(uniq[:2])
            if len(uniq) > 2:
                notes += f"（另有 {len(uniq) - 2} 条，见导出清单）"

        extra = ""
        if result.enhance_summary:
            extra = f"\n增强：{result.enhance_summary}"
        covers = sum(1 for i in result.items if i.cover)
        if covers:
            extra += f"\n封面：{covers} 张"

        if result.cancelled:
            return ("已取消。" + (
                f"已导出 {result.ok_count} 条。" if result.ok_count else ""))
        if result.fail_count:
            return f"{result.summary()}　→　{result.out_dir}{extra}{notes}"
        return f"完成！{result.summary()}{extra}\n{result.out_dir}{notes}"

    def _drafts_text(self, r: exporter.DraftResult) -> str:
        """草稿导出的结果文本。"""
        if r.error:
            return f"草稿导出失败：{r.error.splitlines()[0]}"
        lines = [f"草稿导出：时间线 {len(r.highlights)} 段，字幕 {r.cue_count} 条"
                 + (f"（边界丢弃 {r.dropped_cues} 条）" if r.dropped_cues else "")]
        for key, label in (("jianying", "剪映草稿"),
                           ("davinci", "达芬奇时间线"),
                           ("fallback", "降级包")):
            cr = r.channels.get(key)
            if not cr:
                continue
            if cr.ok:
                lines.append(f"　✓ {label}：{cr.message}")
            else:
                first = cr.error.splitlines()[0] if cr.error else "失败"
                lines.append(f"　✗ {label}：{first}")
        for n in r.notes[:4]:
            lines.append(f"　提示：{n}")
        return "\n".join(lines)

    @guard_ui
    def _failed(self, msg: str, detail: str = "") -> None:
        self._set_running(False)
        self.status.setText(f"导出失败：{msg.splitlines()[0][:160]}")
        self.status.setStyleSheet(f"color:{theme.danger()};")

    def _sync_enabled(self) -> None:
        """按「运行态 × 业务可用性」两个来源统一刷新控件状态。

        别在 `_set_running` 里逐个 setEnabled(True) ——
        那会把业务层的禁用一起打开（实测踩过：导出结束后
        「烧录字幕」在没有字幕数据的任务上又变回可勾选了）。

        现在多一个来源：**成片相关的设置只在勾了「成片片段」时才适用**。
        """
        on = not self._running
        want_clips = self.clips_check.isChecked()
        for w in (self.rb_exact, self.rb_fast,
                  self.bgm_combo, self.bgm_vol_combo, self.duck_combo,
                  self.fade_in_combo, self.fade_out_combo, self.cover_check,
                  self.bgm_dir_btn):
            w.setEnabled(on and want_clips)
        self.burn_check.setEnabled(on and want_clips and self._burn_available)
        # 没有字幕数据 → 也没有可翻译的东西
        for w in (self.bi_check, self.lang_combo):
            w.setEnabled(on and want_clips and self._burn_available)
        for w in (self.clips_check, self.jy_check, self.dv_check):
            w.setEnabled(on)

    @guard_ui
    def _on_content_changed(self, *_) -> None:
        """导出内容变化 → 刷新提示与控件可用性。"""
        want_clips = self.clips_check.isChecked()
        want_jy = self.jy_check.isChecked()
        want_dv = self.dv_check.isChecked()

        if not (want_clips or want_jy or want_dv):
            self.content_note.setText(
                "⚠️ 三项都没勾 —— 点了导出不会有任何产出。")
            self.content_note.setStyleSheet(
                f"color:{theme.warn()};font-size:12px;")
        elif want_jy or want_dv:
            bits: list[str] = []
            if want_jy:
                ver, verified = exporter.detect_jianying_version()
                if not ver:
                    bits.append("⚠️ 没探测到本机剪映，剪映通道会失败并自动"
                                "改为导出降级包。")
                elif not verified:
                    bits.append(
                        f"⚠️ 本机剪映版本 {ver} 不在已验证列表内"
                        f"（已验证：{'、'.join(config.JIANYING_VERIFIED_VERSIONS)}），"
                        f"草稿兼容性未经验证。")
            bits.append("字幕样式不会跟随导出；导完之后还需要在目标软件里"
                        "手动完成最后一步。")
            self.content_note.setText("　".join(bits))
            self.content_note.setStyleSheet(f"color:{theme.muted()};font-size:12px;")
        else:
            self.content_note.setText("")
        self._sync_enabled()

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.start_btn.setEnabled(not running)
        self.start_btn.setText("导出中…" if running else "开始导出")
        self.close_btn.setEnabled(not running)
        self._sync_enabled()

    @guard_ui
    def _open_folder(self) -> None:
        target = (self._out_path
                  or (self._result.out_dir if self._result else self._out_base))
        try:
            import os
            os.startfile(str(target))
        except BaseException:                      # noqa: BLE001
            self.status.setText(f"输出目录：{target}")

    def reject(self) -> None:                      # noqa: D102  (Qt 回调)
        # 导出过程中不许直接关掉 —— 会把 ffmpeg 留成孤儿进程
        if self.start_btn.isEnabled():
            super().reject()
        else:
            self.status.setText("正在导出，请等它结束（或等待当前片段完成）。")
            self.status.setStyleSheet(f"color:{theme.warn()};")
