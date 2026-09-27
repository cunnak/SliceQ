# -*- coding: utf-8 -*-
"""
阶段 0.5 探针 2：视频输入方式兼容性（决定 SliceQ 架构的生死题）

要回答的问题：
  Q1 中转站是否接受 base64 data URI 视频？      ← 若可，小片段可行
  Q2 中转站是否接受「本地文件路径」？            ← DashScope 官方 SDK 的能力，中转站大概率没有
  Q3 中转站是否接受公网 URL 视频？
  Q4 模型能否理解视频里的【音轨】（R12 核心问题）？
  Q5 enable_thinking=false 是否被该端点接受？
"""
import base64
import json
import os
import time

import requests

KEY = os.environ["PROBE_KEY"]
BASE = "https://api.inferera.com/v1"
MODEL = "qwen3.8-omni-flash"
VIDEO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_media", "speech_video.mp4")

s = requests.Session()
s.trust_env = False
s.headers.update({"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
CHAT = BASE + "/chat/completions"

# 参照 httpbin 之类的公网小视频；用阿里官方文档示例视频（稳定可访问）
PUBLIC_VIDEO = "https://help-static-aliyun-doc.aliyuncs.com/file-manage-files/zh-CN/20241115/cqqkru/1.mp4"

Q_AUDIO = "这个视频里的语音（人声）说了什么？请逐字复述你听到的内容，特别是数字。如果完全没有听到人声，请明确说'没有听到人声'。"
Q_VISUAL = "这个视频画面里有什么？请描述你看到的图像内容。"


def call(label, content_parts, extra=None, timeout=300, max_tokens=1500):
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": content_parts}],
        "max_tokens": max_tokens,
        "stream": False,
    }
    if extra:
        payload.update(extra)
    t0 = time.time()
    try:
        r = s.post(CHAT, json=payload, timeout=timeout)
        dt = time.time() - t0
        try:
            b = r.json()
        except Exception:
            b = {}
        u = b.get("usage") or {}
        det = u.get("completion_tokens_details") or {}
        rt = det.get("reasoning_tokens") or 0
        txt = ""
        try:
            txt = b["choices"][0]["message"]["content"] or ""
        except Exception:
            pass
        print(f"\n{'=' * 74}\n{label}\n{'=' * 74}")
        print(f"   HTTP {r.status_code}   {dt:.1f}s")
        print(f"   tokens: prompt={u.get('prompt_tokens')} completion={u.get('completion_tokens')} reasoning={rt}")
        if "error" in b:
            print(f"   ERROR: {json.dumps(b['error'], ensure_ascii=False)[:400]}")
        if txt:
            print(f"   正文({len(txt)} 字符):")
            for line in txt.strip().splitlines()[:14]:
                print(f"     {line}")
        elif r.status_code == 200:
            print("   ⚠ 正文为空")
            print(f"   原始响应片段: {r.text[:400]}")
        return r.status_code, b, txt, dt
    except requests.exceptions.ReadTimeout:
        print(f"\n{label}\n   [ReadTimeout] 超过 {timeout}s 未返回")
        return None, {}, "", 0
    except requests.exceptions.RequestException as e:
        print(f"\n{label}\n   [{type(e).__name__}] {e}")
        return None, {}, "", 0


raw = open(VIDEO, "rb").read()
b64 = base64.b64encode(raw).decode()
print(f"测试视频: {len(raw):,} bytes → base64 {len(b64):,} 字符")

results = {}

# ---------- T1: base64 data URI ----------
sc, b, txt, dt = call(
    "T1 · base64 data URI 视频 + 问音轨",
    [
        {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}},
        {"type": "text", "text": Q_AUDIO},
    ],
)
results["T1_base64_audio"] = (sc, txt, dt)

# ---------- T2: 本地文件路径（DashScope SDK 独有能力）----------
sc, b, txt, dt = call(
    "T2 · 本地文件路径传参（预期：中转站不支持）",
    [
        {"type": "video_url", "video_url": {"url": "file:///" + VIDEO.replace("\\", "/")}},
        {"type": "text", "text": Q_VISUAL},
    ],
)
results["T2_localpath"] = (sc, txt, dt)

# ---------- T3: 纯 Windows 路径形态 ----------
sc, b, txt, dt = call(
    "T3 · Windows 绝对路径传参（预期：中转站不支持）",
    [
        {"type": "video_url", "video_url": {"url": VIDEO}},
        {"type": "text", "text": Q_VISUAL},
    ],
)
results["T3_winpath"] = (sc, txt, dt)

# ---------- T4: 公网 URL ----------
sc, b, txt, dt = call(
    "T4 · 公网 URL 视频",
    [
        {"type": "video_url", "video_url": {"url": PUBLIC_VIDEO}},
        {"type": "text", "text": Q_VISUAL},
    ],
)
results["T4_public_url"] = (sc, txt, dt)

# ---------- T5: enable_thinking: false 是否被接受 ----------
sc, b, txt, dt = call(
    "T5 · enable_thinking=false 兼容性",
    [{"type": "text", "text": "回答一个字：好"}],
    extra={"enable_thinking": False},
    max_tokens=200,
    timeout=120,
)
results["T5_thinking_flag"] = (sc, txt, dt)

# ---------- T6: 画面理解对照（同一视频，问画面）----------
sc, b, txt, dt = call(
    "T6 · 同一 base64 视频，改问画面内容（用于区分音轨/画面是否都被理解）",
    [
        {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}},
        {"type": "text", "text": Q_VISUAL},
    ],
)
results["T6_base64_visual"] = (sc, txt, dt)

print("\n\n" + "=" * 74)
print("汇总")
print("=" * 74)
for k, (sc, txt, dt) in results.items():
    flag = "✓" if (sc == 200 and txt) else ("○" if sc == 200 else "✗")
    print(f"  {flag} {k:<22} HTTP={sc}  正文={len(txt)}字符  {dt:.1f}s")
