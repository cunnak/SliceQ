# -*- coding: utf-8 -*-
r"""中转站端点探测 —— 先把"这端点到底能干什么"钉死，再写配置。

═══════════════════════════════════════════════════════════════
按 openai-compat-endpoint-debug 技能的顺序
═══════════════════════════════════════════════════════════════
1. 先看**代理环境** —— requests 默认 trust_env=True，会读 HTTP_PROXY 等。
   "程序走不走代理"实际由启动它的 shell 决定，配置文件里看不出来。
   本脚本显式 trust_env=False，让结果只取决于代码。
2. 试 `base_url` 的两种前缀（带不带 `/v1`）—— 404 与正文 `{"detail":"Not Found"}`
   意味着路径错了，而不是 key 或模型的问题。
3. 拿模型清单（**只用来纠正拼写**；清单里有 ≠ 这把 key 能用）。
4. 逐个真跑一次 `chat/completions`，产出"**这把 key 能用哪些模型**"。

key 从环境变量 RELAY_KEY 读，不落盘、不打印全文。
"""
from __future__ import annotations

import json
import os
import sys
import time

import requests

BASE = "https://api.inferera.com"
KEY = os.environ.get("RELAY_KEY", "").strip()

# 关心的模型关键词（按 TECH-DESIGN：分析靠 qwen omni 系）
WANTED = ("omni", "qwen", "vl", "asr", "paraformer")


def mask(k: str) -> str:
    return f"{k[:8]}…{k[-4:]}（{len(k)} 字符）" if k else "(空)"


def session(use_env_proxy: bool) -> requests.Session:
    s = requests.Session()
    # 对照实验的关键开关：
    #   True  → 跟随环境变量里的代理（用户机器上通常开着代理软件）
    #   False → 强制直连
    # 两种都测，才知道到底是"端点不通"还是"必须走代理"。
    s.trust_env = use_env_proxy
    return s


def probe_routes() -> tuple[requests.Session, str]:
    """先做路由对照：直连 vs 走环境代理。返回可用的 session 与描述。"""
    print("── 0. 路由对照（直连 vs 走环境代理）──")
    results: dict[str, str] = {}

    for label, use_proxy in (("直连", False), ("走代理", True)):
        s = session(use_proxy)
        try:
            r = s.get(f"{BASE}/v1/models",
                      headers={"Authorization": f"Bearer {KEY}"}, timeout=25)
            results[label] = f"HTTP {r.status_code}"
            print(f"  {label:6s} → HTTP {r.status_code}  {r.text[:80]}")
        except Exception as exc:
            results[label] = f"{type(exc).__name__}"
            print(f"  {label:6s} → {type(exc).__name__}: {str(exc)[:90]}")
    print()

    if results.get("走代理", "").startswith("HTTP"):
        print("  ⇒ 走代理可用。**这是环境依赖，不是端点问题。**")
        print("     SliceQ 应当：默认跟随环境代理（trust_env=True），")
        print("     并在设置页提供手动代理配置。")
        print()
        return session(True), "走环境代理"
    if results.get("直连", "").startswith("HTTP"):
        print("  ⇒ 直连可用（代理反而多余）")
        print()
        return session(False), "直连"
    print("  ⇒ 两条路都不通，需要进一步排查（见技能「网络层」一节）")
    print()
    return session(True), "未知"


def main() -> int:
    print("=" * 78)
    print("中转站端点探测")
    print("=" * 78)
    print(f"BASE = {BASE}")
    print(f"KEY  = {mask(KEY)}")
    print()

    print("── 代理环境变量（记录用）──")
    found = False
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy", "NO_PROXY"):
        v = os.environ.get(k)
        if v:
            found = True
            print(f"  {k} = {v}")
    if not found:
        print("  （无代理环境变量）")
    print()

    s, route = probe_routes()
    headers = {"Authorization": f"Bearer {KEY}"}

    # ── 1. 试 base_url 前缀 ─────────────────────────
    print("── 1. 探测 base_url 前缀 ──")
    chat_bases: list[str] = []
    for prefix in ("/v1", ""):
        url = f"{BASE}{prefix}/models"
        try:
            r = s.get(url, headers=headers, timeout=30)
            body = r.text[:160].replace("\n", " ")
            print(f"  GET {url}\n      → HTTP {r.status_code}  {body}")
            if r.status_code == 200:
                chat_bases.append(f"{BASE}{prefix}")
        except Exception as exc:
            print(f"  GET {url}\n      → 异常 {type(exc).__name__}: {exc}")
    print()

    if not chat_bases:
        print("!! 两种前缀都拿不到模型清单，后面的探测无意义。")
        return 1

    base = chat_bases[0]
    chat_url = f"{base}/chat/completions"
    print(f"→ 采用 base_url = {base}")
    print(f"→ 对话端点     = {chat_url}")
    print()

    # ── 2. 模型清单（只用于纠正拼写）───────────────
    print("── 2. 模型清单 ──")
    try:
        r = s.get(f"{base}/models", headers=headers, timeout=30)
        data = r.json()
        ids = [m.get("id", "") for m in data.get("data", [])]
        print(f"  共 {len(ids)} 个模型")
        interesting = [i for i in ids
                       if any(w in i.lower() for w in WANTED)]
        print(f"  与项目相关的（含 {'/'.join(WANTED)}）：")
        for i in sorted(interesting):
            print(f"    {i}")
        if not interesting:
            print("    （一个都没有）—— 下面把全部模型列出来看看")
            for i in sorted(ids)[:80]:
                print(f"    {i}")
    except Exception as exc:
        print(f"  解析失败：{exc}")
        ids = []
    print()

    # ── 3. 逐个真跑，确认"这把 key 能用什么" ────────
    print("── 3. 逐模型最小请求（8 字符输入，判断可用性）──")
    # 候选：先试项目默认，再把清单里相关的都试一遍
    cands = ["qwen3.8-omni-flash"]
    for i in sorted(ids):
        if any(w in i.lower() for w in ("omni",)) and i not in cands:
            cands.append(i)
    cands = cands[:6]

    usable: list[str] = []
    for name in cands:
        payload = {"model": name,
                   "messages": [{"role": "user", "content": "hi"}],
                   "max_tokens": 16}
        t0 = time.time()
        try:
            r = s.post(chat_url, headers=headers, timeout=120,
                       data=json.dumps(payload).encode())
            dt = time.time() - t0
            tag = "✅ 可用" if r.status_code == 200 else f"❌ {r.status_code}"
            print(f"  {name:38s} {tag}  {dt:5.1f}s")
            if r.status_code != 200:
                print(f"        {r.text[:150]}")
            else:
                b = r.json()
                msg = (b.get("choices") or [{}])[0].get("message") or {}
                print(f"        正文长度 {len(msg.get('content') or '')}"
                      f"　usage={b.get('usage')}")
                usable.append(name)
        except Exception as exc:
            print(f"  {name:38s} ❌ 异常 {type(exc).__name__}: {str(exc)[:90]}")
    print()

    print("=" * 78)
    print("结论")
    print("=" * 78)
    print(f"  base_url（存入设置）= {base}")
    print(f"  这把 key 可用模型（最小请求验证过）= {usable or '（无）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
