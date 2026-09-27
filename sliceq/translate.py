# -*- coding: utf-8 -*-
"""字幕翻译（阶段 4-B · F14）。

## 为什么只走 LLM，不接免费网页翻译

实测（`probe_translate_sources.py`，2026-09-26）：

| 通道 | 结果 |
|---|---|
| Google `translate_a/single` | 两个域名**全部 ReadTimeout**（被墙）|
| 必应 `ttranslatev3` | **401 `{"ShowCaptcha":false}`** —— 带上 IG/IID/AbuseParams 也一样，该接口已对非浏览器会话封禁 |
| LibreTranslate 公共实例 | 全 403 |
| MyMemory | 可用，但**质量不可用**：「在床路崎岖中流浪」→「wandering in the rugged bed road」|

而 LLM 精翻的成本实测（`probe_llm_translate.py`）：**255 字 ¥0.0008**，
外推 1 分钟字幕约 ¥0.0008。**便宜到没必要为省这点钱去接一个质量不可控的接口。**

⇒ v1 **只走 LLM**；MyMemory 仅作"没配 key 时的兜底"，并在结果里标明质量降级。

## ★ 本模块最大的风险：静默错位

字幕是按行对应画面的。模型只要**少输出一行**，从那一行开始
**所有字幕都会错位**，而且不产生任何报错。
所以这里的校验是硬性的：

    解析出的条数必须 == 期望条数，否则：
        ① 退一步按"纯行数"再解析一次（不信编号，信行数）
        ② 仍不符 → 重试同一批
        ③ 仍不符 → **拆成单条逐条翻译**（单条不可能错位）
        ④ 单条也失败 → 该条**保留原文**，并记进 notes

绝不"尽力对齐" —— 宁可有几行没翻，也不能整体错位。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import requests

from . import prompts

DEFAULT_BATCH = 20
MAX_TOKENS_PER_BATCH = 2000
# 单行字符上限 —— **只作兜底保护，不是排版依据**。
#
# 排版由 `subtitle_style.wrap_for_canvas()` 按**画布宽度**决定（它知道字号和分辨率）。
# 这里防的是"一行 100 个字符"这种明显失控的情况。
#
# ⚠️ 原为 42，来源是"中文一行 20 字 × 2 宽"的换算 —— 但英文一个字符
#    只有约 0.55 全角宽，42 个英文字符 ≈ 23 个中文字，**对英文明显过严**。
#    实测（probe_translate_quality.py）：53 条样本里 18 条超 42，
#    而它们在画布上并不溢出行宽。把 42 写进提示词还会逼模型**为凑字数丢信息**。
#    ⇒ 提示词里已改为"保持简洁但不许丢信息"，这里放宽到 56 只作兜底。
MAX_LINE_CHARS = 56

ProgressCb = Callable[[float, str], None]
CancelCb = Callable[[], bool]


@dataclass
class TranslateResult:
    lines: list[str] = field(default_factory=list)
    usage: object | None = None
    batches: int = 0
    retries: int = 0
    single_pass: int = 0            # 走了逐条翻译的条数
    failed: int = 0                 # 最终仍保留原文的条数
    source: str = "llm"             # llm / mymemory / none
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.lines)

    @property
    def cost_cny(self) -> float:
        return getattr(self.usage, "cost_cny", 0.0) or 0.0


# ─────────────────────────────────────────────────────────────
# 解析
# ─────────────────────────────────────────────────────────────
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")
_NUM_PREFIX = re.compile(r"^\s*(\d{1,4})\s*[.、。:：)）]\s*(.+?)\s*$")


def _clean(raw: str) -> str:
    """去掉代码块围栏与首尾空白。"""
    text = (raw or "").strip()
    text = _FENCE.sub("", text)
    return text.strip()


def parse_numbered(raw: str, expect: int) -> list[str] | None:
    """解析 `编号. 译文` 格式。条数必须等于 expect，否则返回 None。

    两级解析：
      ① 严格按编号取（能容忍乱序、容忍多出的**无编号说明行**）
      ② 编号不可靠时，退化为"取所有非空行"（但要行数正好等于 expect）

    ⚠️ **编号超出 1..expect 时直接判失败**，不能"过滤掉就完事"。
       实测踩过：`1. a / 2. b / 3. c` 配 expect=2，
       早先的实现把编号 3 那行当噪声丢掉、返回 [a, b]，看上去很对 ——
       但如果模型其实是因为**把两条拆成了三条**才多出来的，
       这个"过滤"就正好把错位伪装成了正常。
       多出来的一行到底是噪声还是错位，从文本上看不出来，
       所以一律按失败处理，交给重试 / 逐条降级去解决。
    """
    text = _clean(raw)
    if not text:
        return None

    lines = [ln for ln in text.splitlines() if ln.strip()]
    by_num: dict[int, str] = {}
    plain: list[str] = []
    for ln in lines:
        m = _NUM_PREFIX.match(ln)
        if m:
            n = int(m.group(1))
            if not (1 <= n <= expect):
                return None                      # 编号越界 = 条数不对
            if n not in by_num:
                by_num[n] = m.group(2).strip()
            else:
                plain.append(m.group(2).strip())  # 编号重复（少见）当纯文本看
        else:
            plain.append(ln.strip())

    # ① 编号齐全
    if len(by_num) == expect:
        return [by_num[i] for i in range(1, expect + 1)]

    # ② 编号不全：如果去掉编号后的行数正好对得上，就用行序
    if len(by_num) + len(plain) == expect and plain:
        merged: list[str] = []
        for ln in lines:
            m = _NUM_PREFIX.match(ln)
            merged.append(m.group(2).strip() if m else ln.strip())
        return merged if len(merged) == expect else None

    # ③ 全是纯文本行
    if len(lines) == expect and not by_num:
        return [ln.strip() for ln in lines]

    return None


def parse_json_array(raw: str) -> list[str] | None:
    """解析 `["a","b"]` 这类输出（标题生成用）。"""
    import json

    text = _clean(raw)
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:                            # noqa: BLE001
        pass
    # 退路：抓第一个 [...] 块
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:                        # noqa: BLE001
            return None
    return None


def clip_long(translations: Sequence[str], *, indent: str = "") -> list[str]:
    """把超长译文按词断开（兜底上限）。

    ⚠️ 这里只做**上限保护**，不做排版。真正的换行由
       `subtitle_style.wrap_for_canvas()` 按画布宽度做（它知道字号与分辨率）。
       这里防的是"一行 100 个字符"这种明显失控的情况。
    """
    out: list[str] = []
    for t in translations:
        t = t.strip()
        if len(t) <= MAX_LINE_CHARS:
            out.append(t)
            continue
        words, cur, acc = t.split(" "), "", []
        for w in words:
            if cur and len(cur) + 1 + len(w) > MAX_LINE_CHARS:
                acc.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            acc.append(cur)
        # ⚠️ 必须用 **换行** 连接，不能用空格拼回一行。
        #
        #   早先的实现写的是 `" ".join(acc)` —— 断完又拼回去，
        #   输出和输入一样长，**整个"超长保护"从来没生效过**，
        #   而且不报错（长译文照样进字幕，只是靠 wrap_for_canvas 兜底）。
        #   这个 bug 被一条我自己写的恒真断言（`... or True`）掩盖了。
        #
        #   下游两处都能正确消化 `\n`：
        #     · ASS 路径 → `ass_escape()` 会把它转成 `\N`（ASS 的硬换行）
        #     · SRT 路径 → SRT 本身支持多行块
        out.append("\n".join(acc) if acc else t)
    return out


# ─────────────────────────────────────────────────────────────
# 单批翻译
# ─────────────────────────────────────────────────────────────
def _ask(transport, prompt: str, system: str, timeout: int):
    """发一次纯文本请求。返回 (文本, usage)。"""
    res = transport.analyze_text(prompt, system=system,
                                 max_tokens=MAX_TOKENS_PER_BATCH,
                                 thinking="off", timeout=timeout)
    return (res.text or "").strip(), res.usage


def translate_batch(lines: Sequence[str], transport, *,
                    target_lang: str, reflection: bool = True,
                    timeout: int = 120) -> tuple[list[str] | None, object]:
    """翻一批。条数对不上就返回 None（**不做任何"尽力对齐"**）。"""
    if not lines:
        return [], None
    system = prompts.TRANSLATE_SYSTEM if reflection else prompts.TRANSLATE_SYSTEM_FAST
    prompt = prompts.translate_prompt(list(lines), target_lang)
    text, usage = _ask(transport, prompt, system, timeout)
    parsed = parse_numbered(text, len(lines))
    return parsed, usage


# ─────────────────────────────────────────────────────────────
# 免费兜底（质量差，只在 LLM 不可用时用）
# ─────────────────────────────────────────────────────────────
def mymemory(text: str, target_lang: str = "en", *,
             timeout: int = 15) -> str | None:
    """MyMemory 免费接口。

    ⚠️ **质量不足以做交付**（实测「在床路崎岖中流浪」→
       「wandering in the rugged bed road」）。只在没有 API key 时兜底，
       且调用方必须把"机翻质量"这件事告诉用户。
    """
    try:
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": f"zh-CN|{target_lang}"},
            timeout=timeout, headers={"User-Agent": "SliceQ/0.1"})
        data = r.json()
        out = (data.get("responseData") or {}).get("translatedText") or ""
        return out.strip() or None
    except Exception:                            # noqa: BLE001
        return None


# ─────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────
def translate_lines(lines: Iterable[str], *,
                    transport=None,
                    target_lang: str = "en",
                    batch: int = DEFAULT_BATCH,
                    reflection: bool = True,
                    allow_free_fallback: bool = True,
                    progress: ProgressCb | None = None,
                    should_cancel: CancelCb | None = None,
                    timeout: int = 120) -> TranslateResult:
    """逐行翻译。返回的 `lines` 与输入**一一对应**（长度必然相等）。

    长度相等是硬保证：失败的那一行保留原文，而不是丢弃 ——
    丢弃会让后面的行整体前移，字幕就错位了。
    """
    src = [str(x or "").strip() for x in lines]
    result = TranslateResult(lines=list(src))
    if not src:
        return result

    # 空行不送模型（浪费 token，而且会打乱编号对应）
    idx = [i for i, t in enumerate(src) if t]
    todo = [src[i] for i in idx]
    if not todo:
        return result

    out: list[str] = [""] * len(src)
    batch = max(1, int(batch))

    if transport is None:
        return _free_fallback(src, out, idx, todo, result, target_lang,
                              allow_free_fallback, progress)

    done = 0
    for start in range(0, len(todo), batch):
        if should_cancel and should_cancel():
            break
        chunk = todo[start:start + batch]

        if progress:
            progress(done / len(todo), f"翻译 {done}/{len(todo)}")

        # ⚠️ 单批失败**不能中断整批**（与 export_clips 同样的取舍）。
        #    而且失败原因必须记下来 —— 早先的实现没有 try 包住，
        #    一次 API 异常（实测：百炼欠费返回 400）会让整个翻译直接抛出，
        #    上层只知道"翻译失败"，不知道是欠费、限流还是网络。
        usage = None
        try:
            got, usage = translate_batch(chunk, transport,
                                         target_lang=target_lang,
                                         reflection=reflection, timeout=timeout)
        except Exception as exc:                 # noqa: BLE001
            result.notes.append(
                f"翻译请求失败（{type(exc).__name__}）："
                f"{(str(exc) or '').splitlines()[0][:140]}")
            got = None
        result.batches += 1
        if usage is not None:
            result.usage = usage if result.usage is None else result.usage + usage

        if got is None:
            # 重试一次（同参数）
            try:
                got, usage2 = translate_batch(chunk, transport,
                                              target_lang=target_lang,
                                              reflection=reflection,
                                              timeout=timeout)
            except Exception:                    # noqa: BLE001
                got, usage2 = None, None
            result.batches += 1
            result.retries += 1
            if usage2 is not None:
                result.usage = result.usage + usage2 if result.usage else usage2

        if got is None:
            # ★ 降级为逐条 —— 单条不可能错位；单条也失败则保留原文
            got = []
            for one in chunk:
                if should_cancel and should_cancel():
                    got.append("")
                    continue
                try:
                    single, u = translate_batch([one], transport,
                                                target_lang=target_lang,
                                                reflection=reflection,
                                                timeout=timeout)
                except Exception:                # noqa: BLE001
                    single, u = None, None
                result.batches += 1
                result.single_pass += 1
                if u is not None:
                    result.usage = result.usage + u if result.usage else u
                got.append(single[0] if single else "")
            if any(got):
                result.notes.append(
                    f"有一批（{len(chunk)} 条）译文条数对不上，已改为逐条翻译"
                    f"以免字幕整体错位。")

        for j, text in enumerate(got):
            out[idx[start + j]] = text or ""

        done += len(chunk)
        if progress:
            progress(done / len(todo), f"翻译 {done}/{len(todo)}")

    # 落回原位；空着的保留原文（不能丢，丢了就错位）
    for i in idx:
        if not out[i].strip():
            out[i] = src[i]
            result.failed += 1
    if result.failed:
        result.notes.append(
            f"有 {result.failed} 条没能翻译，已保留原文占位"
            f"（丢弃会让后面的字幕整体错位）。")
    result.lines = clip_long(out)
    result.source = "llm"
    if progress:
        progress(1.0, f"翻译完成（{len(todo) - result.failed}/{len(todo)}）")
    return result


def _free_fallback(src: list[str], out: list[str], idx: list[int],
                   todo: list[str], result: TranslateResult, target_lang: str,
                   allow: bool, progress: ProgressCb | None) -> TranslateResult:
    """没有 transport 时的兜底（MyMemory）。质量差，必须告知。"""
    if not allow:
        result.source = "none"
        result.notes.append("未配置 API key，且已禁用免费通道，无法翻译。")
        return result

    result.notes.append(
        "当前未配置 API key，使用的是免费机翻（MyMemory），"
        "译文仅供大致参考，质量明显低于模型精翻。建议到「设置」里填百炼 key。")
    for n, one in enumerate(todo):
        if progress:
            progress(n / max(1, len(todo)), f"机翻 {n}/{len(todo)}")
        out[idx[n]] = mymemory(one, target_lang) or ""
    for i in idx:
        if not out[i].strip():
            out[i] = src[i]
            result.failed += 1
    result.source = "mymemory"
    result.lines = clip_long(out)
    return result


# ─────────────────────────────────────────────────────────────
# 接到字幕管线上
# ─────────────────────────────────────────────────────────────
def apply_to_cues(cues: Sequence, *, transport=None,
                  target_lang: str = "en",
                  reflection: bool = True,
                  progress: ProgressCb | None = None,
                  should_cancel: CancelCb | None = None) -> TranslateResult:
    """把译文填进每条 cue 的 `sub` 字段（就地修改）。

    只翻 `main` 非空的条目 —— 空条目不该占一行字幕。
    """
    mains = [getattr(c, "main", "") or "" for c in cues]
    res = translate_lines(mains, transport=transport, target_lang=target_lang,
                          reflection=reflection, progress=progress,
                          should_cancel=should_cancel)
    for cue, translated in zip(cues, res.lines):
        cue.sub = translated if translated != (getattr(cue, "main", "") or "") else ""
    return res
