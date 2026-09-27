# -*- coding: utf-8 -*-
"""
阶段 0.5 探针 3：base64 视频体积上限探测

背景：探针 2 已证实中转站不支持本地路径传参，只剩 base64 与公网 URL 两条路。
      架构能否成立，取决于「精析副本能否压到 base64 可承载的体积」。
      官方文档称 Base64 编码后单文件 ≤10MB（原始 <7MB）——中转站是否同规则？必须实测。
"""
import base64
import json
import os
import subprocess
import sys
import time

import requests

KEY = os.environ["PROBE_KEY"]
CHAT = "https://api.inferera.com/v1/chat/completions"
MODEL = "qwen3.8-omni-flash"
BASE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(BASE, "test_media")

s = requests.Session()
s.trust_env = False
s.headers.update({"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})


def make_video(out, duration, bitrate):
    """按指定码率生成测试视频，得到可控的文件体积"""
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=15:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-b:v", bitrate, "-c:a", "aac", "-b:a", "32k", "-shortest",
        out,
    ]
    subprocess.run(cmd, capture_output=True)
    return os.path.getsize(out) if os.path.exists(out) else 0


def probe(path, label):
    raw = os.path.getsize(path)
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    b64_bytes = len(b64)
    payload_chars = b64_bytes + 200
    print(f"\n{'=' * 74}")
    print(f"{label}")
    print(f"  原始 {raw:,} bytes | base64 {b64_bytes:,} bytes ({b64_bytes / 1024 / 1024:.2f} MB)"
          f" | 请求体约 {payload_chars / 1024 / 1024:.2f} MB")
    print(f"{'=' * 74}")
    payload = {
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}},
                {"type": "text", "text": "用一句话说明这个视频里有什么。"},
            ],
        }],
        "max_tokens": 400,
        "stream": False,
        "enable_thinking": False,
    }
    t0 = time.time()
    try:
        r = s.post(CHAT, json=payload, timeout=300)
        dt = time.time() - t0
        try:
            b = r.json()
        except Exception:
            b = {}
        u = b.get("usage") or {}
        txt = ""
        try:
            txt = b["choices"][0]["message"]["content"] or ""
        except Exception:
            pass
        print(f"  → HTTP {r.status_code}  {dt:.1f}s  prompt_tokens={u.get('prompt_tokens')}")
        if r.status_code != 200:
            msg = json.dumps(b.get("error", b), ensure_ascii=False)[:300]
            print(f"  → 错误: {msg}")
        else:
            print(f"  → 正文 {len(txt)} 字符: {txt[:130]}")
        return r.status_code, u.get("prompt_tokens"), dt
    except requests.exceptions.ReadTimeout:
        print(f"  → [ReadTimeout] 超过 300s（可能因体积过大导致服务端处理超时）")
        return None, None, 0
    except requests.exceptions.RequestException as e:
        print(f"  → [{type(e).__name__}] {e}")
        return None, None, 0


# 构造阶梯体积：约 1MB / 3MB / 5MB / 7MB / 9MB 原始
plan = [
    ("v_1mb.mp4", 20, "400k", "~1MB 原始"),
    ("v_3mb.mp4", 30, "800k", "~3MB 原始"),
    ("v_5mb.mp4", 40, "1M", "~5MB 原始"),
    ("v_7mb.mp4", 50, "1.2M", "~7MB 原始"),
    ("v_9mb.mp4", 60, "1.4M", "~9MB 原始"),
]

files = []
for name, dur, br, desc in plan:
    p = os.path.join(MEDIA, name)
    size = make_video(p, dur, br)
    files.append((p, f"{desc} ({size:,} bytes)"))
    print(f"生成 {name}: {size:,} bytes")

print("\n\n" + "#" * 74)
print("# base64 体积阶梯测试")
print("#" * 74)
out = []
for p, label in files:
    sc, pt, dt = probe(p, label)
    out.append((label, sc, pt, dt))

print("\n\n" + "=" * 74)
print("汇总")
print("=" * 74)
print(f"{'体积档':<34} {'HTTP':<8} {'prompt_tok':<12} {'耗时'}")
for label, sc, pt, dt in out:
    print(f"{label:<34} {str(sc):<8} {str(pt):<12} {dt:.1f}s")
