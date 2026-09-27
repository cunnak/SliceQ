# -*- coding: utf-8 -*-
r"""百炼 key 可用性探测。

key 从环境变量 DS_KEY 读（不落盘、不写进脚本）。

先看格式：标准 DashScope key 是 `sk-` + 32 位十六进制；
本次这把是 `sk-ws-` 开头、后面是 `x.y.z` 三段式（JWT 样式）。
所以**先别假设它属于哪个端点**，把几个常见入口都试一遍，
看哪个返回"认证通过"而不是"key 无效"。
"""
from __future__ import annotations

import json
import os
import socket
import time

import requests

KEY = os.environ.get("DS_KEY", "").strip()

# 候选入口：(说明, URL, 方法)
ENDPOINTS = [
    ("官方兼容模式 /models",
     "https://dashscope.aliyuncs.com/compatible-mode/v1/models", "GET"),
    ("官方 API /models",
     "https://dashscope.aliyuncs.com/api/v1/models", "GET"),
    ("官方兼容模式 chat",
     "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "POST"),
    ("官方原生 API models（不带 v1）",
     "https://dashscope.aliyuncs.com/api/models", "GET"),
]


def mask(k: str) -> str:
    return f"{k[:12]}…{k[-6:]}（{len(k)} 字符）" if k else "(空)"


def main() -> int:
    print("=" * 78)
    print("百炼 key 可用性探测")
    print("=" * 78)
    print(f"KEY = {mask(KEY)}")
    print()

    # 格式速判
    body = KEY[6:] if KEY.startswith("sk-ws-") else KEY[3:]
    parts = body.split(".")
    print("── 格式速判 ──")
    print(f"  前缀        : {KEY[:6] if KEY.startswith('sk-ws-') else KEY[:3]}")
    print(f"  点分段数    : {len(parts)}"
          f"{'（JWT 样式：header.payload.signature）' if len(parts) == 3 else ''}")
    print(f"  是否为标准百炼格式（sk- + 32 位十六进制）: ", end="")
    std = (KEY.startswith("sk-") and not KEY.startswith("sk-ws-")
           and len(KEY) == 35)
    print("是" if std else "否")
    print()

    # 网络可达性（官方端点此前实测可通）
    try:
        ip = socket.gethostbyname("dashscope.aliyuncs.com")
        print(f"── DNS：dashscope.aliyuncs.com → {ip} ──")
    except Exception as exc:
        print(f"── DNS 解析失败：{exc} ──")
    print()

    s = requests.Session()
    s.trust_env = False          # 显式直连；官方端点本网络可通（实测）

    headers = {"Authorization": f"Bearer {KEY}",
               "Content-Type": "application/json"}

    print("── 逐入口测试 ──")
    for label, url, method in ENDPOINTS:
        t0 = time.time()
        try:
            if method == "GET":
                r = s.get(url, headers=headers, timeout=30)
            else:
                r = s.post(url, headers=headers, timeout=60,
                           data=json.dumps({
                               "model": "qwen-omni-turbo",
                               "messages": [{"role": "user", "content": "hi"}],
                               "max_tokens": 8,
                           }).encode())
            dt = time.time() - t0
            snippet = r.text[:170].replace("\n", " ")
            print(f"  {label:34s} HTTP {r.status_code}  {dt:5.1f}s")
            print(f"      {snippet}")
        except Exception as exc:
            print(f"  {label:34s} 异常 {type(exc).__name__}: {str(exc)[:80]}")
    print()

    print("=" * 78)
    print("判读")
    print("=" * 78)
    print("  401 / invalid_api_key      → key 不属于这个端点（或已失效）")
    print("  403 / Access denied        → key 有效但无权限")
    print("  200                        → 可用")
    print("  404 且正文是 HTML/其他      → 路径不对，与 key 无关")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
