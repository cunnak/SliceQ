# -*- coding: utf-8 -*-
"""阶段 4-B 前置实测：译文来源到底通不通。

双语字幕的整条渲染链路（`subtitle_style.to_ass` 已接受
`(start, end, main, sub)` 四元组）在阶段 3 就铺好了，
**唯一缺的是"译文从哪来"**。

免费网页翻译接口是这一环里最不稳的东西 —— 随时可能改版、加校验、被墙。
所以先测连通性与可用性，再决定架构：能通就做成"免费优先 + LLM 兜底"，
不通就老实只做 LLM 精翻（并把费用告诉用户）。

运行：python stage0/probe_resolve/probe_translate_sources.py
"""
from __future__ import annotations

import json
import time
import urllib.parse

import requests

TIMEOUT = 8
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SAMPLE_ZH = "你拿个二维码过来叫我扫一下我付钱，结果扫完了以后他说没收到。"
SAMPLE_EN = "He told me to scan a QR code to pay, but after I paid he said he never got it."

RESULT: list[tuple[str, str, str]] = []


def rec(tag: str, ok: bool, detail: str) -> None:
    RESULT.append((tag, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {tag} :: {detail}")


def try_get(name: str, url: str, *, headers: dict | None = None,
            parse=None) -> None:
    t0 = time.time()
    try:
        r = requests.get(url, timeout=TIMEOUT,
                         headers={**({"User-Agent": UA} if headers is None else headers)})
    except Exception as exc:                       # noqa: BLE001
        rec(name, False, f"{type(exc).__name__}: {str(exc)[:90]}")
        return
    ms = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        rec(name, False, f"HTTP {r.status_code}（{ms}ms）")
        return
    text = r.text[:400]
    if parse:
        try:
            out = parse(r)
        except Exception as exc:                   # noqa: BLE001
            rec(name, False, f"解析失败 {type(exc).__name__}: {str(exc)[:70]}｜体={text[:80]}")
            return
        rec(name, bool(out), f"{ms}ms → {out!r}")
    else:
        rec(name, True, f"HTTP 200（{ms}ms）{text[:70]!r}")


# ─────────────────────────────────────────────────────────────
# 1. Google 免费端点（translate_a/single，gtx client）
# ─────────────────────────────────────────────────────────────
def parse_google(r) -> str:
    data = r.json()
    # 形如 [[["译文","原文",...],...], ...]
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def test_google() -> None:
    print("\n=== 1. Google 免费端点 ===")
    q = urllib.parse.quote(SAMPLE_ZH)
    for host in ("translate.googleapis.com", "translate.google.com"):
        url = (f"https://{host}/translate_a/single"
               f"?client=gtx&sl=zh-CN&tl=en&dt=t&q={q}")
        try_get(f"Google/{host}", url, parse=parse_google)


# ─────────────────────────────────────────────────────────────
# 2. MyMemory（免费额度，无需 key）
# ─────────────────────────────────────────────────────────────
def test_mymemory() -> None:
    print("\n=== 2. MyMemory ===")
    url = ("https://api.mymemory.translated.net/get?q="
           + urllib.parse.quote(SAMPLE_ZH) + "&langpair=zh-CN|en")

    def parse(r) -> str:
        d = r.json()
        return (d.get("responseData") or {}).get("translatedText", "")

    try_get("MyMemory", url, parse=parse)


# ─────────────────────────────────────────────────────────────
# 3. LibreTranslate 公共实例
# ─────────────────────────────────────────────────────────────
def test_libre() -> None:
    print("\n=== 3. LibreTranslate 公共实例 ===")
    for host in ("libretranslate.com", "translate.terraprint.co",
                 "libretranslate.de"):
        url = f"https://{host}/translate"
        try:
            r = requests.post(url, timeout=TIMEOUT,
                              json={"q": SAMPLE_ZH, "source": "zh",
                                    "target": "en", "format": "text"},
                              headers={"User-Agent": UA})
            if r.status_code == 200:
                out = (r.json() or {}).get("translatedText", "")
                rec(f"LibreTranslate/{host}", bool(out), repr(out)[:100])
            else:
                rec(f"LibreTranslate/{host}", False, f"HTTP {r.status_code}")
        except Exception as exc:                   # noqa: BLE001
            rec(f"LibreTranslate/{host}", False, f"{type(exc).__name__}: {str(exc)[:70]}")


# ─────────────────────────────────────────────────────────────
# 4. 必应：需要先拿页面里的 IG / IID，再调 ttranslatev3
# ─────────────────────────────────────────────────────────────
def test_bing() -> None:
    print("\n=== 4. 必应（两段式：先取 token 再翻译）===")
    import re
    try:
        page = requests.get("https://cn.bing.com/translator", timeout=TIMEOUT,
                            headers={"User-Agent": UA})
    except Exception as exc:                       # noqa: BLE001
        rec("Bing/取页面", False, f"{type(exc).__name__}: {str(exc)[:90]}")
        return
    if page.status_code != 200:
        rec("Bing/取页面", False, f"HTTP {page.status_code}")
        return
    html = page.text
    ig = re.search(r'IG:"([^"]+)"', html)
    iid = re.search(r'data-iid="([^"]+)"', html)
    params = re.search(r"params_AbusePreventionHelper\s*=\s*\[([0-9]+),\"([^\"]+)\"", html)
    rec("Bing/取页面", bool(ig), f"IG={'有' if ig else '无'} "
                                f"IID={'有' if iid else '无'} "
                                f"AbuseParams={'有' if params else '无'}")
    if not ig:
        return
    try:
        r = requests.post(
            "https://cn.bing.com/ttranslatev3"
            f"?isVertical=1&IG={ig.group(1)}&IID={iid.group(1) if iid else ''}",
            timeout=TIMEOUT,
            headers={"User-Agent": UA,
                     "Referer": "https://cn.bing.com/translator"},
            data={"fromLang": "zh-Hans", "text": SAMPLE_ZH, "to": "en"})
        if r.status_code == 200:
            d = r.json()
            out = d[0]["translations"][0]["text"] if isinstance(d, list) else str(d)[:80]
            rec("Bing/ttranslatev3", bool(out), repr(out)[:110])
        else:
            rec("Bing/ttranslatev3", False, f"HTTP {r.status_code} {r.text[:90]}")
    except Exception as exc:                       # noqa: BLE001
        rec("Bing/ttranslatev3", False, f"{type(exc).__name__}: {str(exc)[:90]}")


# ─────────────────────────────────────────────────────────────
# 5. 基础网络对照（排除"整个网都不通"）
# ─────────────────────────────────────────────────────────────
def test_baseline() -> None:
    print("\n=== 5. 网络基线对照 ===")
    for name, url in (("必应首页", "https://cn.bing.com"),
                      ("GitHub", "https://api.github.com")):
        t0 = time.time()
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
            rec(f"基线/{name}", r.status_code < 500,
                f"HTTP {r.status_code}（{int((time.time() - t0) * 1000)}ms）")
        except Exception as exc:                   # noqa: BLE001
            rec(f"基线/{name}", False, f"{type(exc).__name__}: {str(exc)[:70]}")


def main() -> int:
    print("样本：" + SAMPLE_ZH)
    test_baseline()
    test_google()
    test_mymemory()
    test_libre()
    test_bing()

    print("\n" + "=" * 70)
    ok = [r for r in RESULT if r[1] == "PASS"]
    print(f"可用 {len(ok)} / 共 {len(RESULT)}")
    for tag, st, detail in RESULT:
        print(f"{st:4} | {tag:34} | {detail[:80]}")
    print("\n结论：")
    if ok:
        print("  → 至少一个免费通道可用，可以做成「免费优先 + LLM 兜底」")
    else:
        print("  → 免费通道全不可用，双语字幕只能走 LLM 精翻（须向用户明示费用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
