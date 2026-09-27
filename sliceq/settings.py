# -*- coding: utf-8 -*-
"""非敏感的偏好设置（JSON 落盘）。

与 secrets.py 的分工：
  - secrets.py  → 敏感（API key），DPAPI 加密
  - settings.py → 非敏感（默认导出目录、ffmpeg 路径、模型选择），明文 JSON

⚠️ 不要把 API key 写进这里。
"""
from __future__ import annotations

import json
from typing import Any

from . import config

_DEFAULTS: dict[str, Any] = {
    "ffmpeg_path": "",                      # 用户手动指定的 ffmpeg
    "whisper_model": config.WHISPER_MODEL_DEFAULT,
    "export_dir": str(config.EXPORT_DIR),
    "base_url": config.DASHSCOPE_BASE_URL,  # 允许改成中转站
    "model": config.DASHSCOPE_MODEL_DEFAULT,
    "enable_thinking": False,               # 默认关（阶段 0 实测：思考吃 65~75% 预算）
    # 粗筛/精析的并发路数。**默认串行** —— 并发会同时发出多个请求，
    # 更容易撞服务端限流；新功能不该默认改变所有老用户的行为。
    "concurrency": 1,
    "last_dir": "",                         # 上次打开的文件目录
}

_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    data = dict(_DEFAULTS)
    if config.SETTINGS_PATH.exists():
        try:
            raw = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except Exception:
            pass    # 配置损坏就用默认值，不要崩
    _cache = data
    return data


def _save() -> None:
    config.ensure_dirs()
    config.SETTINGS_PATH.write_text(
        json.dumps(_load(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get(key: str, default: Any = None) -> Any:
    return _load().get(key, _DEFAULTS.get(key, default))


def set(key: str, value: Any) -> None:
    _load()[key] = value
    _save()


def all_items() -> dict[str, Any]:
    return dict(_load())


def reset() -> None:
    global _cache
    _cache = dict(_DEFAULTS)
    _save()
