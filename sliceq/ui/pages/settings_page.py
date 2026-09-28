# -*- coding: utf-8 -*-
"""设置页：API key / FFmpeg / whisper 模型 三块。

设计原则（PRD 7.x）：
- **每个外部依赖都显示"当前状态 + 一键动作"**，不让用户去猜环境对不对。
- 环境探测（跑 ffmpeg -version、列滤镜）是阻塞操作，**必须走工作线程**。
- 失败信息写中文，并给出下一步该干什么。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from ... import (analyzer, asr_models, bridge, config, ffmpeg_tools, secrets,
                 settings)
from ..workers import guard_ui

OK_COLOR = "#1D9E75"
WARN_COLOR = "#BA7517"
BAD_COLOR = "#E24B4A"
MUTED = "#888780"


class SettingsPage(QWidget):
    def __init__(self, pool, parent=None) -> None:
        super().__init__(parent)
        self.pool = pool
        self._build()
        self.refresh_all()

    # ─────────────────────────────────────────────────────
    # 构建
    # ─────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        title = QLabel("设置")
        title.setStyleSheet("font-size:20px;font-weight:600;")
        root.addWidget(title)

        # ★ 分组必须放进**滚动区**（2026-09-28 干净虚拟机验证发现）
        #
        # 踩的坑：这 5 个分组叠起来的自然高度 ≈973px。`QStackedWidget` 会把
        #   **所有页面的最大最小尺寸** 当成自己的最小尺寸 ⇒ 主窗口最小高度被顶到
        #   973、最小宽度顶到 1058，代码里的 `resize(1180, 720)` 因此完全无效。
        #   在 768p 屏（或 1080p @125% 缩放）上窗口比屏幕还大，
        #   **底部内容被裁到屏幕之外，而且拖不上来** ——
        #   用户够不到「FFmpeg 下载」入口，偏偏那是干净机器上必做的第一步。
        #   ⚠️ 这个缺陷在开发者的大屏机器上完全看不出来。
        holder = QWidget()
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 8, 0)
        inner.setSpacing(16)

        inner.addWidget(self._build_api_group())
        inner.addWidget(self._build_ffmpeg_group())
        inner.addWidget(self._build_model_group())
        inner.addWidget(self._build_perf_group())
        inner.addWidget(self._build_bridge_group())
        inner.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(holder)
        # 纵向按需滚动；横向也用 AsNeeded —— 若某个分组比窗口还宽，
        # 宁可出现横向滚动条，也不能把控件切到够不到。
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        root.addWidget(scroll, 1)

    # ── 外部工具（阶段 4 · F16）───────────────────────────
    def _build_bridge_group(self) -> QGroupBox:
        """与 VideoCaptioner 的联动状态。

        ⚠️ 这里**只能**展示状态与提供"打开"，不能提供"一键调用" ——
           实测 VideoCaptioner 没有命令行接口（详见 bridge.py 头注释），
           承诺一个做不到的按钮比没有按钮更糟。
        """
        box = QGroupBox("外部工具")
        form = QFormLayout(box)

        self.vc_status = QLabel("")
        self.vc_status.setWordWrap(True)
        form.addRow("VideoCaptioner", self.vc_status)

        row = QHBoxLayout()
        self.vc_path_edit = QLineEdit(bridge.get_manual_path())
        self.vc_path_edit.setPlaceholderText(
            "自动找不到时，在这里填安装目录或 exe 路径")
        self.vc_path_edit.editingFinished.connect(self._save_vc_path)
        row.addWidget(self.vc_path_edit, 1)

        pick = QPushButton("选择…")
        pick.clicked.connect(self._pick_vc)
        row.addWidget(pick)

        self.vc_open_btn = QPushButton("打开")
        self.vc_open_btn.setToolTip("用独立进程启动 VideoCaptioner")
        self.vc_open_btn.clicked.connect(self._open_vc)
        row.addWidget(self.vc_open_btn)
        form.addRow("", row)

        self.vc_note = QLabel("")
        self.vc_note.setWordWrap(True)
        self.vc_note.setStyleSheet(f"color:{MUTED};font-size:11px;")
        form.addRow("", self.vc_note)

        self._refresh_vc()
        return box

    @guard_ui
    def _refresh_vc(self, *_):
        info = bridge.find()
        if info.get("ok"):
            self.vc_status.setText(f"已找到（{info['source']}）")
            self.vc_status.setStyleSheet(f"color:{OK_COLOR};")
            self.vc_open_btn.setEnabled(True)
            lines = bridge.handoff_notes()
            self.vc_note.setText("\n".join(lines[1:]) if len(lines) > 1
                                 else "")
        else:
            self.vc_status.setText("未找到")
            self.vc_status.setStyleSheet(f"color:{MUTED};")
            self.vc_open_btn.setEnabled(False)
            msg = ("SliceQ 不会自动去寻找安装在其他位置的程序。"
                   "如果你装了 VideoCaptioner，把它的目录填在左边即可启用联动。")
            if info.get("manual_invalid"):
                msg = ("⚠️ 填写的路径里没有 VideoCaptioner.exe，"
                       "请检查后重填。" + msg)
            self.vc_note.setText(msg)

    @guard_ui
    def _save_vc_path(self):
        bridge.set_manual_path(self.vc_path_edit.text().strip())
        self._refresh_vc()

    @guard_ui
    def _pick_vc(self):
        d = QFileDialog.getExistingDirectory(self, "选择 VideoCaptioner 的安装目录",
                                             bridge.get_manual_path() or "C:/")
        if d:
            self.vc_path_edit.setText(d)
            self._save_vc_path()

    @guard_ui
    def _open_vc(self):
        ok, msg = bridge.launch()
        self.vc_note.setText(msg)
        self.vc_note.setStyleSheet(
            f"font-size:11px;color:{OK_COLOR if ok else BAD_COLOR};")

    # ── 分析性能 ─────────────────────────────────────────
    def _build_perf_group(self) -> QGroupBox:
        """粗筛/精析的并发度。

        为什么不默认开并发：它会同时发出多个请求，**更容易撞服务端限流**，
        而限流的代价（重试等待）可能比串行还慢。新功能不该默认改变
        所有老用户的行为 —— 想快的人自己调。
        """
        box = QGroupBox("分析性能")
        form = QFormLayout(box)

        self.concurrency_combo = QComboBox()
        for n in (1, 2, 3, 4):
            self.concurrency_combo.addItem(
                "串行（最稳，推荐）" if n == 1 else f"并发 {n} 路", n)
        try:
            cur = int(settings.get("concurrency", 1) or 1)
        except (TypeError, ValueError):
            cur = 1
        idx = self.concurrency_combo.findData(cur)
        self.concurrency_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.concurrency_combo.currentIndexChanged.connect(self._save_perf)
        form.addRow("粗筛 / 精析并发度", self.concurrency_combo)

        hint = QLabel(
            "并发让多个窗口/候选同时分析，总耗时更短。\n"
            "代价：同时发出多个请求，更容易触发服务端限流；\n"
            "并发越高，这个风险越大。不确定就保持串行。\n"
            "（只影响粗筛与精析。转录固定串行 —— 实测 GPU 上并发转录"
            "不但不快，4 路还比串行慢 43%。）")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{MUTED};font-size:11px;")
        form.addRow("", hint)
        return box

    @guard_ui
    def _save_perf(self, *_) -> None:
        settings.set("concurrency", int(self.concurrency_combo.currentData() or 1))

    # ── API ──────────────────────────────────────────────
    def _build_api_group(self) -> QGroupBox:
        box = QGroupBox("模型接口")
        form = QFormLayout(box)
        form.setSpacing(10)

        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("sk-...")
        self.key_status = QLabel()
        show_btn = QPushButton("显示")
        show_btn.setCheckable(True)
        show_btn.setFixedWidth(56)
        show_btn.toggled.connect(
            lambda on: self.key_edit.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password))

        key_row = QHBoxLayout()
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(show_btn)
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._save_key)
        key_row.addWidget(save_btn)
        form.addRow("百炼 API Key", key_row)
        form.addRow("", self.key_status)

        self.base_url_edit = QLineEdit(settings.get("base_url"))
        self.base_url_edit.setPlaceholderText(config.DASHSCOPE_BASE_URL)
        form.addRow("接口地址", self.base_url_edit)

        self.model_edit = QLineEdit(settings.get("model"))
        form.addRow("模型名", self.model_edit)

        url_btn = QPushButton("保存接口配置")
        url_btn.clicked.connect(self._save_endpoint)
        form.addRow("", url_btn)

        # ── 网络适配 ─────────────────────────────────
        # 这两项是 2026-09-25 实测踩出来的，**不是可选优化**：
        # 某中转站在本网络下被 SNI 定向阻断（TCP 通、TLS 握手被 RST），
        # 不开「绕过 SNI 阻断」根本连不上。
        net_hint = QLabel("网络适配（连不上中转站时再动）")
        net_hint.setStyleSheet(f"color:{MUTED};font-size:12px;margin-top:6px;")
        form.addRow("", net_hint)

        self.proxy_combo = QComboBox()
        self.proxy_combo.addItem("跟随系统环境变量", "system")
        self.proxy_combo.addItem("直连（不读环境变量）", "none")
        self.proxy_combo.addItem("手动指定代理", "manual")
        idx = self.proxy_combo.findData(str(settings.get("proxy_mode", "system")))
        self.proxy_combo.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("代理模式", self.proxy_combo)

        self.proxy_edit = QLineEdit(str(settings.get("proxy_url", "") or ""))
        self.proxy_edit.setPlaceholderText("http://127.0.0.1:7890")
        form.addRow("代理地址", self.proxy_edit)

        self.sni_check = QCheckBox("绕过 SNI 阻断（用 IP + Host 头连接）")
        self.sni_check.setChecked(bool(settings.get("sni_bypass", False)))
        form.addRow("", self.sni_check)

        sni_warn = QLabel(
            "⚠️ 开启后必须关闭证书校验（证书绑定的是域名，用 IP 连必然对不上），"
            "**会失去中间人防护**。只在域名被定向阻断、且没有可用代理时开启。")
        sni_warn.setWordWrap(True)
        sni_warn.setStyleSheet(f"color:{WARN_COLOR};font-size:12px;")
        form.addRow("", sni_warn)

        test_row = QHBoxLayout()
        test_btn = QPushButton("测试连接")
        test_btn.clicked.connect(self._test_connection)
        test_row.addWidget(test_btn)
        test_row.addStretch(1)
        form.addRow("", test_row)

        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        self.test_result.setStyleSheet(f"color:{MUTED};font-size:12px;")
        form.addRow("", self.test_result)

        note = QLabel("Key 使用 Windows DPAPI 加密，只绑定当前用户账户——"
                      "文件被拷到别的电脑也解不开。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED};font-size:12px;")
        form.addRow("", note)
        return box

    @guard_ui
    def _save_key(self) -> None:
        secrets.set_api_key(self.key_edit.text())
        self.refresh_key()
        QMessageBox.information(self, "已保存", "API Key 已加密保存。")

    def _persist_endpoint(self) -> None:
        """把接口配置落盘。拆出来是为了让"测试连接"能先存再测 ——
        否则测的是旧配置，用户会以为改了没用。"""
        settings.set("base_url", self.base_url_edit.text().strip()
                     or config.DASHSCOPE_BASE_URL)
        settings.set("model", self.model_edit.text().strip()
                     or config.DASHSCOPE_MODEL_DEFAULT)
        settings.set("proxy_mode", self.proxy_combo.currentData())
        settings.set("proxy_url", self.proxy_edit.text().strip())
        settings.set("sni_bypass", self.sni_check.isChecked())

    @guard_ui
    def _save_endpoint(self) -> None:
        self._persist_endpoint()
        QMessageBox.information(self, "已保存", "接口配置已保存。")

    @guard_ui
    def _test_connection(self) -> None:
        """跑一次连通性诊断（工作线程），把每一步结果摊在页面上。

        诊断顺序遵循"先把网络层排除、再看模型层"：网络问题与模型问题的
        修法完全不同，混在一起猜会浪费很多轮。
        """
        self._persist_endpoint()
        self.test_result.setText("测试中…")
        self.test_result.setStyleSheet(f"color:{MUTED};font-size:12px;")

        mode = self.proxy_combo.currentData()
        self.pool.run(
            analyzer.diagnose,
            self.base_url_edit.text().strip() or config.DASHSCOPE_BASE_URL,
            secrets.get_api_key(),
            proxy=(self.proxy_edit.text().strip() if mode == "manual" else None),
            trust_env=(mode == "system"),
            sni_bypass=self.sni_check.isChecked(),
            on_done=self._on_test_done,
            on_error=lambda m, d: (
                self.test_result.setText(f"测试出错：{m}"),
                self.test_result.setStyleSheet(
                    f"color:{BAD_COLOR};font-size:12px;")))

    @guard_ui
    def _on_test_done(self, result: object) -> None:
        if not isinstance(result, dict):
            self.test_result.setText("测试返回了意外的结果")
            return
        lines: list[str] = []
        for s in result.get("steps", []):
            lines.append(f"{'✓' if s.get('ok') else '✗'} {s['name']}：{s['detail']}")
        sug = result.get("suggestion") or ""
        if sug:
            lines.append("")
            lines.append(sug)
        models = result.get("models") or []
        if models:
            shown = ", ".join(models[:6]) + ("…" if len(models) > 6 else "")
            lines.append("")
            lines.append(f"模型（{len(models)} 个）：{shown}")

        self.test_result.setText("\n".join(lines))
        self.test_result.setStyleSheet(
            f"color:{OK_COLOR if result.get('ok') else BAD_COLOR};font-size:12px;")

    def refresh_key(self) -> None:
        key = secrets.get_api_key()
        if key:
            self.key_status.setText(f"当前：{secrets.mask(key)}")
            self.key_status.setStyleSheet(f"color:{OK_COLOR};font-size:12px;")
            if not self.key_edit.text():
                self.key_edit.setText(key)
        else:
            self.key_status.setText("尚未设置 —— 阶段 2 的分析功能需要它")
            self.key_status.setStyleSheet(f"color:{WARN_COLOR};font-size:12px;")

    # ── FFmpeg ───────────────────────────────────────────
    def _build_ffmpeg_group(self) -> QGroupBox:
        box = QGroupBox("FFmpeg")
        lay = QVBoxLayout(box)
        lay.setSpacing(8)

        self.ffmpeg_status = QLabel("检测中…")
        self.ffmpeg_status.setWordWrap(True)
        lay.addWidget(self.ffmpeg_status)

        row = QHBoxLayout()
        self.ffmpeg_install_btn = QPushButton("自动下载安装")
        self.ffmpeg_install_btn.clicked.connect(self._install_ffmpeg)
        row.addWidget(self.ffmpeg_install_btn)

        browse = QPushButton("手动指定…")
        browse.clicked.connect(self._pick_ffmpeg)
        row.addWidget(browse)

        recheck = QPushButton("重新检测")
        recheck.clicked.connect(self.refresh_ffmpeg)
        row.addWidget(recheck)
        row.addStretch(1)
        lay.addLayout(row)

        self.ffmpeg_bar = QProgressBar()
        self.ffmpeg_bar.setVisible(False)
        lay.addWidget(self.ffmpeg_bar)

        note = QLabel(
            "必须使用 full build（含 whisper 滤镜）——"
            "essentials 构建缺少该滤镜，阶段 2 的转录会不可用。"
            "下载到本地后运行，不打包进程序（GPL 合规）。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED};font-size:12px;")
        lay.addWidget(note)
        return box

    @guard_ui
    def refresh_ffmpeg(self) -> None:
        self.ffmpeg_status.setText("检测中…")
        self.pool.run(
            ffmpeg_tools.probe_environment,
            on_done=self._on_ffmpeg_probe,
            on_error=lambda m, d: self.ffmpeg_status.setText(f"检测出错：{m}"),
        )

    @guard_ui
    def _on_ffmpeg_probe(self, info: dict) -> None:
        if info.get("ok"):
            self.ffmpeg_status.setText(f"✅ {info.get('message')}\n{info.get('ffmpeg')}")
            self.ffmpeg_status.setStyleSheet(f"color:{OK_COLOR};")
            self.ffmpeg_install_btn.setEnabled(False)
        else:
            self.ffmpeg_status.setText(f"⚠️ {info.get('message')}")
            self.ffmpeg_status.setStyleSheet(f"color:{WARN_COLOR};")
            self.ffmpeg_install_btn.setEnabled(True)

    @guard_ui
    def _install_ffmpeg(self) -> None:
        self.ffmpeg_install_btn.setEnabled(False)
        self.ffmpeg_bar.setVisible(True)
        self.ffmpeg_bar.setRange(0, 0)      # 不确定进度
        self.ffmpeg_status.setText("正在下载 FFmpeg（约 160MB）…")

        self.pool.run(
            ffmpeg_tools.download_ffmpeg,
            on_done=self._on_ffmpeg_done,
            on_error=self._on_ffmpeg_error,
            on_progress=self._on_dl_progress,
            with_progress=True,
        )

    @guard_ui
    def _pick_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ffmpeg 可执行文件", "",
            "ffmpeg (ffmpeg.exe ffmpeg);;所有文件 (*)")
        if not path:
            return
        ffmpeg_tools.set_manual_path(path)
        self.refresh_ffmpeg()

    # ── whisper 模型 ─────────────────────────────────────
    def _build_model_group(self) -> QGroupBox:
        box = QGroupBox("转录模型（whisper）")
        lay = QVBoxLayout(box)
        lay.setSpacing(8)

        self.model_status = QLabel()
        self.model_status.setWordWrap(True)
        lay.addWidget(self.model_status)

        row = QHBoxLayout()
        row.addWidget(QLabel("模型"))
        self.model_combo = QComboBox()
        for name, meta in asr_models.MODELS.items():
            self.model_combo.addItem(
                f"{meta['label']}（{meta['size_mb']}MB · {meta['note']}）", name)
        cur = settings.get("whisper_model")
        idx = self.model_combo.findData(cur)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        row.addWidget(self.model_combo, 1)

        dl_btn = QPushButton("下载")
        dl_btn.clicked.connect(self._download_model)
        row.addWidget(dl_btn)
        lay.addLayout(row)

        self.model_bar = QProgressBar()
        self.model_bar.setVisible(False)
        lay.addWidget(self.model_bar)

        note = QLabel("走 hf-mirror.com 镜像下载（HuggingFace 国内直连不通）。"
                      "模型存在本地，只在首次使用或换模型时下载。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED};font-size:12px;")
        lay.addWidget(note)
        return box

    @guard_ui
    def refresh_model(self) -> None:
        name = self.model_combo.currentData()
        if asr_models.is_installed(name):
            mb = asr_models.installed_size_mb(name)
            self.model_status.setText(f"✅ 已安装：{name}（{mb}MB）")
            self.model_status.setStyleSheet(f"color:{OK_COLOR};")
        else:
            self.model_status.setText(f"⚠️ 未安装：{name}")
            self.model_status.setStyleSheet(f"color:{WARN_COLOR};")

    @guard_ui
    def _download_model(self) -> None:
        name = self.model_combo.currentData()
        settings.set("whisper_model", name)
        self.model_bar.setVisible(True)
        self.model_bar.setRange(0, 100)
        self.model_status.setText(f"正在下载 {name} …")

        self.pool.run(
            asr_models.download_model, name,
            on_done=lambda _: self._on_model_done(name),
            on_error=lambda m, d: self._on_model_error(m),
            on_progress=self._on_dl_progress_model,
            with_progress=True,
        )

    @guard_ui
    def _on_model_done(self, name: str) -> None:
        self.model_bar.setVisible(False)
        self.refresh_model()

    @guard_ui
    def _on_model_error(self, msg: str) -> None:
        self.model_bar.setVisible(False)
        self.model_status.setStyleSheet(f"color:{BAD_COLOR};")
        self.model_status.setText(f"下载失败：{msg}")
        QMessageBox.warning(self, "下载失败", msg)

    # ── 进度回调 ─────────────────────────────────────────
    @guard_ui
    def _on_dl_progress(self, done: int, total: int, desc: str) -> None:
        if total > 0:
            self.ffmpeg_bar.setRange(0, 100)
            pct = int(done * 100 / total)
            self.ffmpeg_bar.setValue(pct)
            self.ffmpeg_status.setText(
                f"{desc}：{done / 1048576:.1f} / {total / 1048576:.1f} MB")
        else:
            self.ffmpeg_status.setText(desc or "处理中…")

    @guard_ui
    def _on_dl_progress_model(self, done: int, total: int, desc: str) -> None:
        if total > 0:
            self.model_bar.setRange(0, 100)
            self.model_bar.setValue(int(done * 100 / total))
            self.model_status.setText(
                f"{desc}：{done / 1048576:.1f} / {total / 1048576:.1f} MB")
        else:
            self.model_status.setText(desc or "处理中…")

    @guard_ui
    def _on_ffmpeg_done(self, path) -> None:
        self.ffmpeg_bar.setVisible(False)
        self.refresh_ffmpeg()

    @guard_ui
    def _on_ffmpeg_error(self, msg: str, detail: str) -> None:
        self.ffmpeg_bar.setVisible(False)
        self.ffmpeg_install_btn.setEnabled(True)
        self.ffmpeg_status.setStyleSheet(f"color:{BAD_COLOR};")
        self.ffmpeg_status.setText(f"失败：{msg}")
        QMessageBox.warning(self, "FFmpeg 安装失败", msg)

    # ─────────────────────────────────────────────────────
    def refresh_all(self) -> None:
        self.refresh_key()
        self.refresh_ffmpeg()
        self.refresh_model()
