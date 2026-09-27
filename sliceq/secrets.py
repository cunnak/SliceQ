# -*- coding: utf-8 -*-
"""API key 的本地加密存储。

用 Windows 的 DPAPI（CryptProtectData / CryptUnprotectData）：
密钥由操作系统按【当前用户账户】派生，加密后的文件拷到别的机器或
别的用户下都无法解密。不需要我们自己管主密码，也不需要联网。

⚠️ 不要把这里换成"自己写个 XOR / base64"——那是假加密。
"""
from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes
from pathlib import Path

from . import config


# ─────────────────────────────────────────────────────────────
# DPAPI 绑定（仅 Windows 可用）
# ─────────────────────────────────────────────────────────────
class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _blob_from_bytes(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_to_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi_encrypt(plain: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _blob_from_bytes(plain)
    blob_out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("DPAPI 加密失败（CryptProtectData 返回 0）")
    try:
        return _blob_to_bytes(blob_out)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_decrypt(cipher: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _blob_from_bytes(cipher)
    blob_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("DPAPI 解密失败（换机器或换用户账户后无法解密）")
    try:
        return _blob_to_bytes(blob_out)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ─────────────────────────────────────────────────────────────
# 对外接口：一个极小的"加密的 key-value 小仓库"
# ─────────────────────────────────────────────────────────────
def _load_all() -> dict[str, str]:
    path: Path = config.SECRET_PATH
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
        plain = _dpapi_decrypt(raw) if _dpapi_available() else raw
        data = json.loads(plain.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # 解密失败（换机器 / 文件损坏）视作"没有存过"，不要崩
        return {}


def _save_all(data: dict[str, str]) -> None:
    config.ensure_dirs()
    plain = json.dumps(data, ensure_ascii=False).encode("utf-8")
    blob = _dpapi_encrypt(plain) if _dpapi_available() else plain
    config.SECRET_PATH.write_bytes(blob)


def get_secret(name: str, default: str = "") -> str:
    return _load_all().get(name, default)


def set_secret(name: str, value: str) -> None:
    data = _load_all()
    if value:
        data[name] = value
    else:
        data.pop(name, None)   # 空串 = 删除
    _save_all(data)


def clear_all() -> None:
    if config.SECRET_PATH.exists():
        config.SECRET_PATH.unlink()


# 便捷封装 —— 目前只需要一个 key
KEY_API = "dashscope_api_key"


def get_api_key() -> str:
    return get_secret(KEY_API)


def set_api_key(value: str) -> None:
    set_secret(KEY_API, value.strip())


def mask(key: str) -> str:
    """给 UI 显示用：sk-abc...xyz"""
    if not key:
        return "（未设置）"
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:6]}...{key[-4:]}"
