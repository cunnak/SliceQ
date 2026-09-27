# -*- coding: utf-8 -*-
"""
阶段 0.5 探针 1：端点连通性与模型可用性
遵循 openai-compat-endpoint-debug 方法论：
  - 先拿 /models 清单纠正模型名拼写
  - 清单里有 != 你能用，必须真跑一次 chat/completions
  - 强制直连（trust_env=False），不让 shell 代理污染结论
  - enable_thinking 三态：先不发送，遇 400 再摘
"""
import json
import os
import sys
import time

import requests

KEY = os.environ["PROBE_KEY"]
BASES = [
    "https://api.inferera.com/v1",
    "https://api.inferera.com",
]

session = requests.Session()
session.trust_env = False          # 钉死：不继承环境代理
session.headers.update({
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
})

print("=" * 74)
print("A. 环境对照（判断是否被 shell 代理污染）")
print("=" * 74)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
    print(f"   {k} = {os.environ.get(k)}")
print(f"   session.trust_env = {session.trust_env}  ← 已强制直连")

print()
print("=" * 74)
print("B. 探测 base_url 正确路径")
print("=" * 74)
chosen = None
for base in BASES:
    url = base.rstrip("/") + "/models"
    t0 = time.time()
    try:
        r = session.get(url, timeout=30)
        dt = time.time() - t0
        print(f"   [{r.status_code}] {url}  ({dt:.2f}s)")
        body = r.text[:200]
        print(f"        {body}")
        if r.status_code == 200 and chosen is None:
            chosen = base
    except requests.exceptions.ConnectTimeout:
        print(f"   [ConnectTimeout] {url}  ← 连不上（可能 DNS 污染）")
    except requests.exceptions.ProxyError as e:
        print(f"   [ProxyError] {url}  ← {e}")
    except requests.exceptions.Timeout:
        print(f"   [Timeout] {url}")
    except requests.exceptions.RequestException as e:
        print(f"   [{type(e).__name__}] {url}  ← {e}")

if not chosen:
    print("\n   ✗ 两个路径都没通，无法继续")
    sys.exit(1)

print(f"\n   ✓ 采用 base_url = {chosen}")

print()
print("=" * 74)
print("C. 拉取模型清单，查找 omni / qwen 相关")
print("=" * 74)
r = session.get(chosen.rstrip("/") + "/models", timeout=30)
ids = [m.get("id") for m in r.json().get("data", [])]
print(f"   清单共 {len(ids)} 个模型")
hits = [i for i in ids if i and ("omni" in i.lower() or "qwen" in i.lower())]
print(f"   含 qwen/omni 的: {len(hits)}")
for i in sorted(hits):
    mark = "  ★" if "omni" in i.lower() else "   "
    print(f"   {mark} {i}")

# 逐字比对目标模型名
TARGET = "qwen3.8-omni-flash"
print(f"\n   目标模型 {TARGET}: {'✓ 清单中存在' if TARGET in ids else '✗ 清单中不存在'}")
if TARGET not in ids:
    near = [i for i in ids if i and i.lower().replace("-", "").replace(".", "") == TARGET.lower().replace("-", "").replace(".", "")]
    print(f"   相近候选: {near}")

print()
print("=" * 74)
print("D. 最小请求对照（确认 key 真的能用这个模型）")
print("=" * 74)
chat_url = chosen.rstrip("/") + "/chat/completions"
th = 0


def try_chat(model, extra=None, tx_chars=8, max_tokens=64, timeout=180, label=""):
    text = "hi" if tx_chars <= 8 else "请复述：" + ("测试" * (tx_chars // 2))
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": max_tokens,
        "stream": False,
    }
    if extra:
        payload.update(extra)
    t0 = time.time()
    try:
        rr = session.post(chat_url, json=payload, timeout=timeout)
        dt = time.time() - t0
        body = {}
        try:
            body = rr.json()
        except Exception:
            pass
        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        reasoning = details.get("reasoning_tokens") or 0
        content = ""
        try:
            content = body["choices"][0]["message"]["content"] or ""
        except Exception:
            pass
        err = ""
        if "error" in body:
            err = json.dumps(body["error"], ensure_ascii=False)[:200]
        print(f"   [{rr.status_code}] {label or model}  {dt:.1f}s  "
              f"completion={usage.get('completion_tokens')} reasoning={reasoning} "
              f"content_len={len(content)}")
        if err:
            print(f"        error: {err}")
        if content and len(content) < 300:
            print(f"        content: {content[:200]}")
        return rr.status_code, body, dt
    except requests.exceptions.ReadTimeout:
        print(f"   [ReadTimeout] {label or model}  超过 {timeout}s 未返回（连上了但不说话）")
        return None, {}, 0
    except requests.exceptions.RequestException as e:
        print(f"   [{type(e).__name__}] {label or model}  {e}")
        return None, {}, 0


print("   -- 对照组：最小请求（8 字符）--")
sc, body, dt = try_chat(TARGET, label="最小请求", tx_chars=8, max_tokens=64, timeout=180)

if sc == 404 or (sc and "model" in json.dumps(body).lower() and "not" in json.dumps(body).lower()):
    print("\n   → 模型名可能不对，从清单里取 omni 候选逐个试")
    for cand in (hits or [])[:5]:
        try_chat(cand, label=f"候选 {cand}", max_tokens=64, timeout=120)

print("\n=== 探针 1 结束 ===")
