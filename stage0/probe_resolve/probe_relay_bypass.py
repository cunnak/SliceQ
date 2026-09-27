# -*- coding: utf-8 -*-
r"""中转站探测 · 第二版 —— 已定位到 SNI 阻断，改用 IP+Host 绕行。

═══════════════════════════════════════════════════════════════
诊断结论（2026-09-25 实测）
═══════════════════════════════════════════════════════════════
现象：`https://api.inferera.com` 完全无法建立 TLS 会话。
取证：

  | 测试                        | 结果 |
  |-----------------------------|------|
  | 百度 / 阿里云端点            | 200 / 404（网络本身正常）|
  | TCP 443 端口                | 连通 |
  | **TLS 握手（域名 SNI）**     | **被 RST**（curl 与 Python 两套 TLS 栈表现一致）|
  | **IP 直连 + Host 头**        | **200 ✅** |
  | 本地代理 127.0.0.1:51988    | 隧道能建，但 TLS 同样失败；且它访问 google 也不通 |

⇒ **SNI 定向阻断**：握手包里带 `api.inferera.com` 就被打断，
  不带（SNI 为空或为 IP）就通。

═══════════════════════════════════════════════════════════════
⚠️ 绕行的代价（必须让用户知道）
═══════════════════════════════════════════════════════════════
IP 直连时证书链上的 CN 是域名，校验必然失败，只能 `verify=False`。
**这意味着失去中间人攻击防护。** 对"用户自己选的中转站"这个场景可以接受，
但不能默认开启、不能悄悄启用 —— 必须由用户在设置页显式打开，并写明代价。
"""
from __future__ import annotations

import json
import os
import socket
import time

import requests
from urllib3.exceptions import InsecureRequestWarning

HOST = "api.inferera.com"
SCHEME = "https"
KEY = os.environ.get("RELAY_KEY", "").strip()

WANTED = ("omni", "qwen", "vl", "asr", "paraformer", "audio", "voice")


def mask(k: str) -> str:
    return f"{k[:8]}…{k[-4:]}（{len(k)} 字符）" if k else "(空)"


def resolve(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except Exception as exc:
        print(f"  DNS 解析失败：{exc}")
        return ""


def make(ip: str) -> tuple[requests.Session, str, dict, bool]:
    """返回 (session, base_url, extra_headers, verify)。

    关键就在这三处：
      · 用 **IP** 组 URL → TLS 的 SNI 不含域名 → 不被阻断
      · 用 **Host 头** 告诉服务端真实域名 → 路由到正确站点
      · `verify=False` → 证书 CN 是域名，用 IP 连必然对不上
    """
    s = requests.Session()
    s.trust_env = False          # 显式直连：环境里的那个代理反而 TLS 失败
    return s, f"{SCHEME}://{ip}", {"Host": HOST}, False


def main() -> int:
    print("=" * 78)
    print("中转站探测 · IP+Host 绕行模式")
    print("=" * 78)
    print(f"HOST = {HOST}")
    print(f"KEY  = {mask(KEY)}")

    ip = resolve(HOST)
    if not ip:
        return 1
    print(f"解析 = {ip}")
    print()

    try:
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    except Exception:
        pass

    s, base, hh, verify = make(ip)
    headers = {"Authorization": f"Bearer {KEY}", **hh}

    # ── 1. 确认绕行可用 ─────────────────────────────
    print("── 1. 绕行连通性 ──")
    try:
        r = s.get(f"{base}/v1/models", headers=headers, timeout=30, verify=verify)
        print(f"  GET /v1/models → HTTP {r.status_code}  {len(r.content)} 字节")
    except Exception as exc:
        print(f"  失败：{type(exc).__name__}: {exc}")
        return 1
    if r.status_code != 200:
        print(f"  正文：{r.text[:200]}")
        return 1
    print()

    # ── 2. 模型清单 ─────────────────────────────────
    print("── 2. 模型清单 ──")
    ids: list[str] = []
    try:
        for m in r.json().get("data", []):
            ids.append(m.get("id", ""))
    except Exception as exc:
        print(f"  解析失败：{exc}")
    print(f"  共 {len(ids)} 个模型")
    rel = sorted(i for i in ids if any(w in i.lower() for w in WANTED))
    print(f"  与本项目相关（含 {'/'.join(WANTED)}）：")
    for i in rel:
        print(f"    {i}")
    if not rel:
        print("    （无）—— 全部模型：")
        for i in sorted(ids):
            print(f"    {i}")
    print()

    # ── 3. 纯文本调用（验 key 真的能用）─────────────
    print("── 3. 纯文本最小调用 ──")
    chat = f"{base}/v1/chat/completions"
    text_ok: list[str] = []
    for name in ["qwen3.8-omni-flash"] + [i for i in rel if "omni" in i][:4]:
        if name in text_ok:
            continue
        payload = {"model": name,
                   "messages": [{"role": "user", "content": "只回复两个字：收到"}],
                   "max_tokens": 32}
        t0 = time.time()
        try:
            rr = s.post(chat, headers=headers, timeout=180, verify=verify,
                        data=json.dumps(payload).encode())
            dt = time.time() - t0
            if rr.status_code == 200:
                b = rr.json()
                msg = (b.get("choices") or [{}])[0].get("message") or {}
                usage = b.get("usage") or {}
                print(f"  {name:34s} ✅ {dt:5.1f}s  正文={msg.get('content')!r}")
                print(f"        usage={usage}")
                text_ok.append(name)
            else:
                print(f"  {name:34s} ❌ HTTP {rr.status_code}  {rr.text[:120]}")
        except Exception as exc:
            print(f"  {name:34s} ❌ {type(exc).__name__}: {str(exc)[:80]}")
    print()

    # ── 4. 多模态调用（视频输入）—— 本项目的真命门 ──
    print("── 4. 多模态：图片内联（验 base64 通路）──")
    png_1x1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
               "nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
    for name in (text_ok[:1] or ["qwen3.8-omni-flash"]):
        payload = {
            "model": name,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{png_1x1}"}},
                    {"type": "text", "text": "这张图是什么颜色？一个字回答"},
                ]}],
            "max_tokens": 32,
        }
        t0 = time.time()
        try:
            rr = s.post(chat, headers=headers, timeout=180, verify=verify,
                        data=json.dumps(payload).encode())
            dt = time.time() - t0
            print(f"  {name:34s} HTTP {rr.status_code}  {dt:5.1f}s")
            if rr.status_code == 200:
                b = rr.json()
                msg = (b.get("choices") or [{}])[0].get("message") or {}
                print(f"        正文={msg.get('content')!r}  usage={b.get('usage')}")
            else:
                print(f"        {rr.text[:220]}")
        except Exception as exc:
            print(f"  {name:34s} ❌ {type(exc).__name__}: {str(exc)[:80]}")

    print()
    print("=" * 78)
    print("结论")
    print("=" * 78)
    print(f"  接入方式 = IP({ip}) + Host 头 + verify=False")
    print(f"  可用模型（纯文本验证过）= {text_ok or '（无）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
