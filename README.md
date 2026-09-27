# SliceQ · 直播切片工具

基于多模态大模型（Qwen-Omni 系列）的**直播切片辅助工具**：
把一场几小时的直播录像，自动找出高光片段、切好、配上字幕，导出成可直接发布的成片，
以及可以继续精修的时间线。

**当前状态**：`v0.1.0` · 阶段 1 完成（应用骨架与导入）· 仍在开发中

---

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动
python -m sliceq
```

首次启动后，去**设置**页：
1. 填百炼 API Key（加密存在本地，只绑定当前用户）
2. 确认 FFmpeg 状态 —— 缺失或不是 full build 时点「自动下载安装」
3. 下载一个 whisper 转录模型

然后回**任务**页，把视频拖进来。

---

## 目录结构

```
sliceq/
├── config.py          全局路径与常量（所有模块从这里取，不硬编码）
├── secrets.py         API Key —— Windows DPAPI 加密
├── settings.py        非敏感偏好（JSON）
├── store.py           SQLite 数据层（WAL + 线程私有连接）
├── ffmpeg_tools.py    FFmpeg 定位 / 校验 / 下载
├── asr_models.py      whisper ggml 模型下载（走 hf-mirror）
├── ingest.py          导入：本地文件 + URL（yt-dlp）
├── selftest_stage1.py 自测脚本
└── ui/
    ├── workers.py     QThreadPool 基础设施（长任务不卡 UI）
    ├── main_window.py 主窗口
    └── pages/
        ├── tasks_page.py     拖放 + URL + 任务表格
        └── settings_page.py  Key / FFmpeg / 模型
```

文档在 `docs/`：`PRD-SliceQ.md`、`TECH-DESIGN.md`、`TASKS.md`。
各阶段实测证据在 `stage0/reports/` 与 `reports/`。

---

## 运行自测

```bash
python sliceq/selftest_stage1.py
```

不启动 GUI，验证数据层、密钥加密、元信息读取。当前 33 项全部通过。

---

## 合规说明

本项目采用 **MIT License**。

**FFmpeg 不打包进程序。** FFmpeg 是 GPL 构建，打进 MIT 项目的 exe 会造成许可冲突。
本程序只在**运行时**把 ffmpeg 下载到用户目录，并通过 `subprocess` 以独立进程调用 ——
两个独立程序经命令行交互，属于 GPL FAQ 允许的"聚合"（aggregate）。

同理，`ffmpeg.exe` 与 whisper 模型文件**不在 `requirements.txt` 里**，
它们在首次使用时下载到 `%APPDATA%/SliceQ/`。

---

## 数据存放位置

全部在 `%APPDATA%/SliceQ/` 下：

```
SliceQ/
├── db/sliceq.db          任务、片段、字幕、标记
├── credentials.bin       API Key（DPAPI 加密，换机器无法解密）
├── settings.json         偏好设置
├── logs/sliceq.log       运行日志（按 2MB 滚动）
├── bin/                  自动下载的 ffmpeg
├── models/               whisper 模型
├── downloads/            URL 导入的源视频
└── export/               默认导出目录
```

---

## 阶段性成果

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 系列 | 可行性验证（模型、达芬奇、剪映、媒体链接） | ✅ 完成 |
| 1 | 应用骨架与导入 | ✅ 完成 |
| 2 | 分析引擎与候选清单 | 待开始 |
| 3 | 剪辑执行与 MP4 导出 | 待开始 |
| 4 | AI 全自动增强 | 待开始 |
| 5 | 三端草稿导出 | 待开始 |
| 6 | 打包与开源发布 | 待开始 |
