# -*- coding: utf-8 -*-
"""SliceQ 全局配置：路径、常量、外部资源地址。

所有模块都从这里取路径，不要在各处硬编码 —— 打包成单 exe 后
写入位置必须可预测（%APPDATA%/SliceQ），否则用户数据会散落。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# 应用标识
# ─────────────────────────────────────────────────────────────
APP_NAME = "SliceQ"
APP_DISPLAY_NAME = "SliceQ 直播切片"
VERSION = "0.1.5"


def _app_root() -> Path:
    """用户数据根目录。

    Windows: %APPDATA%/SliceQ
    其他平台: ~/.sliceq（开发用，正式只支持 Windows）
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
    else:
        base = str(Path.home())
        return Path(base) / ".sliceq"
    return Path(base) / APP_NAME


APP_ROOT: Path = _app_root()

# 各子目录（首次使用时自动创建，见 ensure_dirs）
LOGS_DIR = APP_ROOT / "logs"
DB_DIR = APP_ROOT / "db"
DOWNLOADS_DIR = APP_ROOT / "downloads"      # URL 导入下载的源视频
MODELS_DIR = APP_ROOT / "models"            # ggml whisper 模型
BIN_DIR = APP_ROOT / "bin"                  # 自动下载的 ffmpeg
EXPORT_DIR = APP_ROOT / "export"            # 默认导出目录
STYLES_DIR = APP_ROOT / "styles"            # 字幕样式预设（*.json）
STYLE_ASSETS_DIR = STYLES_DIR / "assets"    # 预览背景图等素材
BGM_DIR = APP_ROOT / "bgm"                  # 背景音乐（用户自备，见目录内说明）

DB_PATH = DB_DIR / "sliceq.db"
SECRET_PATH = APP_ROOT / "credentials.bin"   # DPAPI 加密后的 API key
SETTINGS_PATH = APP_ROOT / "settings.json"   # 非敏感的偏好项


def ensure_dirs() -> None:
    """创建所有必要目录。启动时调用一次即可。"""
    for d in (APP_ROOT, LOGS_DIR, DB_DIR, DOWNLOADS_DIR,
              MODELS_DIR, BIN_DIR, EXPORT_DIR,
              STYLES_DIR, STYLE_ASSETS_DIR, BGM_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────
# 剪映（阶段 5 草稿导出）
# ─────────────────────────────────────────────────────────────
# ⚠️ 剪映**只扫自己的草稿目录**，不扫任意路径（阶段 0 实测）。
#    所以草稿必须落在 JIANYING_DRAFT_ROOT 下才能被剪映看到。
def _jianying_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "JianyingPro"
    return Path.home() / ".local" / "share" / "JianyingPro"


JIANYING_ROOT = _jianying_root()
JIANYING_DRAFT_ROOT = JIANYING_ROOT / "User Data" / "Projects" / "com.lveditor.draft"
JIANYING_APPS_DIR = JIANYING_ROOT / "Apps"
# 剪映扫描草稿后写的日志（阶段 0 定的可复现验收判据：
# 出现 {"suc":true,"type":"copy_draft_external"} 即承认了外部草稿）
JIANYING_WATCH_LOG = JIANYING_ROOT / "User Data" / "Log" / "draft_acion_watch.json"

# 已实测验证过草稿兼容性的剪映版本。
# 阶段 0（2026-09-23）与阶段 5.0（2026-09-27）两次验证都在 11.5.3 上完成，
# 且用的是同一个安装（Apps/11.5.3.14501，装于 09-23 18:32）。
# ⚠️ 剪映 6.0+ 对**已有草稿**做了加密，但我们走「从零生成新草稿」路线，
#    不在加密影响范围内（阶段 0 已验证）。
JIANYING_VERIFIED_VERSIONS = ("11.5.3.14501",)


# ─────────────────────────────────────────────────────────────
# 外部资源地址
# ─────────────────────────────────────────────────────────────
# HuggingFace 国内不通，走镜像。whisper 模型统一放这里下载。
HF_MIRROR = "https://hf-mirror.com"

# ffmpeg 官方建议的 Windows 构建（gyan.dev）
# ⚠️ 必须用 full build —— essentials 不含 whisper 滤镜（TECH-DESIGN §5.1）
FFMPEG_DOWNLOAD_PAGE = "https://www.gyan.dev/ffmpeg/builds/"
FFMPEG_RELEASE_ZIP = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full.7z"

# whisper ggml 模型（faster-whisper / whisper.cpp 兼容格式）
#
# 默认档位依据阶段 2 前置实测（reports/STAGE2-PRE-LIVE-ASR-QUALITY.md）：
#   60 秒真实中文直播上，tiny 不可用、base 细节错、small 基本正确。
#   用户指定 large-v3-turbo 为默认 —— 上游可用的最强型号（turbo 是蒸馏版，
#   比 large-v3 快数倍而质量接近）。
WHISPER_MODEL_DEFAULT = "ggml-large-v3-turbo.bin"
WHISPER_MODEL_BASE_URL = (
    f"{HF_MIRROR}/ggerganov/whisper.cpp/resolve/main"
)

# VAD 模型 —— **必需，不是可选优化**
# 实测：无语音片段上 whisper 会产生大量幻觉（24 段里 7~8 段），
# 约三分之一的内容是无中生有。必须用 VAD 先切掉非语音段。
VAD_MODEL_DEFAULT = "ggml-silero-v5.1.2.bin"

# 百炼（DashScope）默认端点。用户可在设置里覆盖为中转站。
#
# ⚠️ **默认值是「兼容模式」，不是原生 API** —— 这一点很容易改错。
#
# 阿里云同一个域名下有两种端点，能力不同（见 TECH-DESIGN §2.2）：
#
#   /api/v1              原生 API（通道 A）→ 需要 `dashscope` 包，
#                        但支持本地文件路径直传（≤100MB）
#   /compatible-mode/v1  兼容模式（通道 B）→ 纯 HTTP，**零额外依赖**，
#                        走 base64 内联（≤15MB）
#
# 为什么默认选通道 B：
#   ① `requirements.txt` **不含** `dashscope`，选通道 A 等于"装完就用不了"；
#   ② 通道 B 零额外依赖，是真正的开箱可用路径；
#   ③ 精析副本本来就按通道自适应压制（通道 B 压到 ≤15MB），通道 B 是被完整支持的。
#
# 实测踩过（2026-09-27）：端到端测试在隔离环境里读到这个默认值 ⇒ 走通道 A
# ⇒ `缺少 dashscope 包` ⇒ 粗筛一个窗口都没成功 ⇒ 报告"没有找到候选段"。
# 用户装了程序、填了 key、点了分析，得到的是"没找到内容，换个素材试试"。
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_MODEL_DEFAULT = "qwen3.8-omni-flash"


# ─────────────────────────────────────────────────────────────
# 业务常量（均来自阶段 0 实测，改动前先看 TECH-DESIGN）
# ─────────────────────────────────────────────────────────────
# 候选段总时长硬上限：全片的 20%
CANDIDATE_RATIO_CAP = 0.20

# 兼容端点 base64 内联的字符硬上限（实测撞墙值 28,000,000）
# 工程上保守取 20,000,000，约合原始 15MB
BASE64_CHAR_LIMIT = 20_000_000

# 官方 SDK 本地路径直传的副本体积上限
SDK_UPLOAD_MB_LIMIT = 100

# 精析副本参数
CLIP_TARGET_HEIGHT = 720          # 目标高度
CLIP_TARGET_FPS = 5               # 抽帧后等效帧率，压 token

# 默认帧率（无元信息时的兜底）
DEFAULT_FPS = 30.0

# 支持导入的本地视频扩展名
VIDEO_EXTS = {".mp4", ".mkv", ".flv", ".mov", ".avi", ".ts", ".webm", ".m4v"}

# 任务状态机（与 store.task.status 取值一致）
STATUS_IMPORTED = "imported"
STATUS_TRANSCRIBING = "transcribing"
STATUS_ROUGHING = "roughing"
STATUS_SCREENED = "screened"
STATUS_REFINING = "refining"
STATUS_READY = "ready"
STATUS_EXPORTING = "exporting"
STATUS_DONE = "done"
STATUS_ERROR = "error"
# 用户主动取消 ≠ 出错。混在一起会让用户看到"出错"以为程序坏了。
STATUS_CANCELLED = "cancelled"

STATUS_LABELS = {
    STATUS_IMPORTED: "已导入",
    STATUS_TRANSCRIBING: "转录中",
    STATUS_ROUGHING: "粗筛中",
    STATUS_SCREENED: "待精析",
    STATUS_REFINING: "精析中",
    STATUS_READY: "待导出",
    STATUS_EXPORTING: "导出中",
    STATUS_DONE: "已完成",
    STATUS_ERROR: "出错",
    STATUS_CANCELLED: "已取消",
}
