# -*- coding: utf-8 -*-
r"""找出这把百炼 key **实际被授权**的模型。

═══════════════════════════════════════════════════════════════
为什么不能只看 /models 清单
═══════════════════════════════════════════════════════════════
实测：`/api/v1/models` 返回 **518 个**模型 —— 那是**全平台目录**，
不代表这把 key 的授权范围。清单里有 ≠ 你能用。

**判断"能不能用"的唯一可靠办法**：逐个发一次最小请求，
看返回的是 200 / 403(access_denied) / 404(model_not_found)。

  200                → 可用
  403 access_denied  → **名字对，但没授权**（要去控制台开）
  404 model_not_found→ 名字错

本脚本产出的是"**这把 key 能用哪些模型**"，而不是"平台有哪些模型"。
"""
from __future__ import annotations

import json
import os
import time

import requests

KEY = os.environ.get("DS_KEY", "").strip()
COMPAT = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 关注的关键词：本项目需要能"看视频"的多模态模型
WANTED = ("omni", "vl", "video", "audio")


def probe(name: str, s: requests.Session, headers: dict) -> tuple[int, str]:
    """发一次最小请求，返回 (状态码, 错误码)。"""
    try:
        r = s.post(f"{COMPAT}/chat/completions", headers=headers, timeout=60,
                   data=json.dumps({
                       "model": name,
                       "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 8,
                   }).encode())
    except Exception as exc:
        return -1, f"{type(exc).__name__}"
    code = ""
    if r.status_code != 200:
        try:
            code = str((r.json().get("error") or {}).get("code") or "")
        except Exception:
            code = r.text[:60]
    return r.status_code, code


def main() -> int:
    print("=" * 78)
    print("找出这把 key 实际被授权的模型")
    print("=" * 78)
    print(f"KEY 前缀 = {KEY[:12]}…（{len(KEY)} 字符）")

    s = requests.Session()
    s.trust_env = False
    headers = {"Authorization": f"Bearer {KEY}",
               "Content-Type": "application/json"}

    # ── 1. 拉清单 ────────────────────────────────────
    print("\n── 1. 拉模型清单 ──")
    all_ids: list[str] = []
    try:
        r = s.get(f"{COMPAT}/models", headers=headers, timeout=30)
        if r.status_code == 200:
            all_ids = [m.get("id", "") for m in r.json().get("data", [])]
            print(f"  兼容模式 /models 返回 {len(all_ids)} 个")
        else:
            print(f"  /models HTTP {r.status_code}")
    except Exception as exc:
        print(f"  失败：{exc}")

    try:
        r2 = s.get("https://dashscope.aliyuncs.com/api/v1/models",
                   headers=headers, timeout=30)
        if r2.status_code == 200:
            out = r2.json().get("output") or {}
            more = [m.get("model", "") for m in out.get("models", [])]
            print(f"  原生 /api/v1/models 返回 {out.get('total')} 个（本页 {len(more)}）")
            all_ids += more
    except Exception:
        pass

    all_ids = sorted(set(i for i in all_ids if i))
    rel = [i for i in all_ids if any(w in i.lower() for w in WANTED)]
    print(f"  合并去重 {len(all_ids)} 个；含 {'/'.join(WANTED)} 的 {len(rel)} 个")
    print("  （下面逐个真跑，找被授权的）\n")

    # ── 2. 优先试"本项目要用的那类" ──────────────────
    print("── 2. 逐模型实测（只测多模态相关的）──")
    usable: list[str] = []
    denied: list[str] = []
    notfound: list[str] = []

    # 先把最可能的排前面
    priority = ["qwen3.8-omni-flash", "qwen3.8-omni-flash-realtime",
                "qwen-omni-turbo", "qwen-omni-turbo-latest"]
    todo = priority + [i for i in rel if i not in priority]
    # 清单里没有的也试一下（清单未必全）
    for name in todo[:24]:
        code, err = probe(name, s, headers)
        tag = "✅" if code == 200 else "❌"
        print(f"  {name:38s} {tag} {code}  {err}")
        if code == 200:
            usable.append(name)
        elif err == "access_denied":
            denied.append(name)
        elif code == 404:
            notfound.append(name)
        time.sleep(0.3)

    print()
    print("=" * 78)
    print("结论")
    print("=" * 78)
    print(f"  ✅ 可用（{len(usable)}）：{usable or '（无）'}")
    print(f"  ⚠️ 名字对但未授权（{len(denied)}）：{denied[:6] or '（无）'}")
    print(f"  ❓ 名字不存在（{len(notfound)}）：{notfound[:6] or '（无）'}")
    if denied and not usable:
        print()
        print("  ⇒ key 有效，但**所有测试的模型都未授权**。")
        print("     需要到百炼控制台给这个工作空间/账号开通模型权限。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
