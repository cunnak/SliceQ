# -*- coding: utf-8 -*-
"""模型接入层 —— 双 transport 抽象（TECH-DESIGN §3）。

═══════════════════════════════════════════════════════════════
为什么要分两条通道
═══════════════════════════════════════════════════════════════
端点形态决定"视频怎么送出去"，这不是实现细节，是架构分叉：

  通道 A · DashScope 官方 SDK
      ✅ 本地文件路径**直传**（`file://` 前缀），无需上传、无需 OSS
      ✅ 副本 ≤ 100MB
      ❌ 需要 dashscope 包

  通道 B · OpenAI 兼容 HTTP（含各类中转站）
      ✅ 只需 requests，门槛低
      ❌ 服务端物理上访问不到用户磁盘 ⇒ 只能 base64 内联或公网 URL
      ⚠️ base64 字符串硬上限 28,000,000 字符（实测撞墙值），
         工程上取 ≤20,000,000（原始 ≤15MB）

按用户填的 base_url 自动判定，允许手动覆盖。

═══════════════════════════════════════════════════════════════
⚠️ 未验证部分（缺 API key，2026-09-25）
═══════════════════════════════════════════════════════════════
"能构造出正确的 payload" 与 "端点真的接受它" 是两件事。
本模块把 **payload 构造** 拆成纯函数（`build_*_payload`），
这些函数可以在没有 key 的情况下单元测试；真正发请求的部分是薄薄一层。
拿到 key 后需要验证的是：
  1. DashScope 的本地路径写法（`file://` + 绝对路径）是否被接受
  2. 兼容端点的视频内联字段名（`video_url` vs `image_url`）
  3. `enable_thinking` 参数在两端点各自的行为
"""
from __future__ import annotations

import base64
import json
import mimetypes
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from . import config, secrets, settings

# ─────────────────────────────────────────────────────────────
# 定价（TECH-DESIGN §2.3 实测定价，单位：元 / 百万 token）
# ─────────────────────────────────────────────────────────────
PRICE_INPUT_PER_M = 0.8       # 输入
PRICE_CACHE_HIT_PER_M = 0.1   # 缓存命中
PRICE_OUTPUT_PER_M = 2.7      # 输出

# 实测基准：视频输入的 token 消耗 ≈ 228 token / 秒视频，
# 与文件体积无关（1MB 与 10.8MB 的同长度视频 token 相同）。
TOKENS_PER_SECOND_VIDEO = 228

# base64 体积上限（通道 B）
B64_HARD_LIMIT = 28_000_000        # 实测撞墙值
B64_SAFE_LIMIT = 20_000_000        # 工程保守值，留 30% 余量
# 本地路径上限（通道 A）
LOCAL_PATH_LIMIT = 100 * 1024 * 1024

RETRY_MAX = 5
# 思考参数会吃掉 65%~75% 的输出预算，给结构化 JSON 留足余量
MAX_TOKENS_MIN = 800
MAX_TOKENS_DEFAULT = 900


class AnalyzerError(Exception):
    """模型调用失败，message 直接给用户看。"""


class AuthError(AnalyzerError):
    """key 缺失或无效 —— 这类错误应该引导用户去设置页，而不是重试。"""


class RateLimitError(AnalyzerError):
    """限流 / 服务端错误 —— 可重试。"""


# ─────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────
@dataclass
class Usage:
    """单次调用的 token 用量。用于成本核算。"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def cost_cny(self) -> float:
        """按实测定价换算成人民币。

        ⚠️ 这是**估算**。BYOK 模式下真实扣费以百炼账单为准，
           界面上必须同时声明这一点（TECH-DESIGN §2.5.4）。
        """
        billed_input = max(0, self.input_tokens - self.cache_hit_tokens)
        return round(
            billed_input / 1e6 * PRICE_INPUT_PER_M
            + self.cache_hit_tokens / 1e6 * PRICE_CACHE_HIT_PER_M
            + self.output_tokens / 1e6 * PRICE_OUTPUT_PER_M,
            6,
        )

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_hit_tokens=self.cache_hit_tokens + other.cache_hit_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


@dataclass
class AnalyzeResult:
    text: str
    usage: Usage = field(default_factory=Usage)
    transport: str = ""
    model: str = ""
    elapsed_s: float = 0.0
    # 降级事件：静默降级等于隐瞒，这些必须能上报到 UI
    degraded: list[str] = field(default_factory=list)
    raw: Any = None


# ─────────────────────────────────────────────────────────────
# payload 构造（纯函数，无网络 —— 这部分可以在没有 key 的情况下测）
# ─────────────────────────────────────────────────────────────
def detect_channel(base_url: str) -> str:
    """按 base_url 判定走哪条通道。

    ⚠️ 只判断**域名**是不够的 —— 阿里云官方同时提供两种端点：

        https://dashscope.aliyuncs.com/api/v1              → 原生 API（通道 A，SDK）
        https://dashscope.aliyuncs.com/compatible-mode/v1  → 兼容模式（通道 B，HTTP）

    两者域名相同、能力不同：**只有原生 API 那条支持本地文件路径直传**，
    兼容模式仍然必须 base64 内联。
    所以这里要连路径一起看，否则用户填了兼容模式地址会被塞进
    需要 dashscope SDK 的通道 A，报一个和真实原因无关的错。
    """
    u = (base_url or "").strip().lower()
    if "dashscope.aliyuncs.com" in u and "compatible-mode" not in u:
        return "dashscope"
    return "http"


def build_dashscope_messages(video: Path, prompt: str,
                             system: str | None = None) -> list[dict]:
    """构造 DashScope 的多模态 messages。

    本地文件用 `file://` + 绝对路径 —— 这是 SDK 能力，不是协议能力，
    所以通道 B 用不了（服务端读不到用户磁盘）。

    ⚠️ 与 HTTP 通道同理：媒体 key 要按类型选（`image` / `video`）。
       粗筛送的是拼贴 PNG，用 `video` 会被拒。
    """
    p = video.resolve()
    url = "file:///" + str(p).replace("\\", "/").lstrip("/")
    mime = mimetypes.guess_type(str(video))[0] or "video/mp4"
    key = "image" if mime.startswith("image/") else "video"

    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": [{"text": system}]})
    msgs.append({"role": "user", "content": [{key: url}, {"text": prompt}]})
    return msgs


def build_http_messages(video: Path, prompt: str,
                        system: str | None = None) -> list[dict]:
    """构造 OpenAI 兼容格式的 messages，媒体走 base64 data URI。

    ⚠️⚠️ **字段名必须按媒体类型选**：
       图片 → `image_url`
       视频 → `video_url`
       实测把 PNG 塞进 `video_url` 会被服务端直接拒：
       `InternalError.Algo.InvalidParameter: Invalid video file.`
       —— 而且这条错误信息不会提示"字段名错了"，容易误判成文件损坏。

    本函数收到的可能是**视频**（精析送的副本），也可能是**图片**
    （粗筛送的拼贴联系表），所以按 MIME 自动分派。
    """
    mime = mimetypes.guess_type(str(video))[0] or "video/mp4"
    b64 = base64.b64encode(video.read_bytes()).decode("ascii")
    field = "image_url" if mime.startswith("image/") else "video_url"

    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({
        "role": "user",
        "content": [
            {"type": field, field: {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": prompt},
        ],
    })
    return msgs


def b64_size_of(video: Path) -> int:
    """估算某个视频转成 base64 后的字符数（不真正编码，省内存）。

    base64 体积 = ceil(n/3)*4，取 4/3 上界。
    """
    n = video.stat().st_size
    return (n + 2) // 3 * 4


def thinking_kwargs(mode: str) -> dict:
    """思考参数三态。

    auto → 不发这个参数（默认）
    off  → enable_thinking=False（精析要结构化定位，不需要长推理）
    on   → enable_thinking=True

    做成三态是因为 `enable_thinking` 是 Qwen 系私有参数，
    不是所有端点都认 —— 不认时报 400/422，会自动摘除重试。
    """
    if mode == "off":
        return {"enable_thinking": False}
    if mode == "on":
        return {"enable_thinking": True}
    return {}


def parse_usage(d: Any) -> Usage:
    """从响应里解析 usage。字段名在不同端点上略有差异，这里都兜住。"""
    if not isinstance(d, dict):
        return Usage()
    details = d.get("completion_tokens_details") or {}
    return Usage(
        input_tokens=int(d.get("input_tokens") or d.get("prompt_tokens") or 0),
        output_tokens=int(d.get("output_tokens") or d.get("completion_tokens") or 0),
        cache_hit_tokens=int(
            (d.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
    )


# ─────────────────────────────────────────────────────────────
# 成本预估（供 UI 用，TECH-DESIGN §2.5）
# ─────────────────────────────────────────────────────────────
def estimate_refine_cost(candidate_seconds: float) -> float:
    """预估"精析"这一步的花费（元）。按 token/秒 实测线性关系换算。"""
    tokens = candidate_seconds * TOKENS_PER_SECOND_VIDEO
    return round(tokens / 1e6 * PRICE_INPUT_PER_M
                 + (candidate_seconds / 120 * 200) / 1e6 * PRICE_OUTPUT_PER_M, 2)


def estimate_total_upper_bound(duration_sec: float, candidate_ratio: float = 0.20
                               ) -> dict:
    """算「本轮花费上限」——按候选段打满给定比例的最坏情况。

    ⚠️ 给用户看的是**上限**而不是典型值（TECH-DESIGN §2.5.1 原则 2）：
       预估 ¥0.5 实花 ¥0.3，用户觉得赚；反过来就是被骗感。
    """
    cand = duration_sec * candidate_ratio
    refine = estimate_refine_cost(cand)
    # 粗筛（语义 + 抽帧）与文案的量级来自 §2.3 实测
    screening = round(0.03 + 0.05, 3)
    caption = 0.05
    return {
        "candidate_seconds": cand,
        "refine": refine,
        "screening": screening,
        "caption": caption,
        "total": round(refine + screening + caption, 2),
    }


# ─────────────────────────────────────────────────────────────
# Transport 抽象
# ─────────────────────────────────────────────────────────────
class Transport(Protocol):
    """模型调用的抽象。两条通道（官方 SDK / 兼容 HTTP）都实现它。

    实现这个协议就等于可以被 pipeline 使用 —— 测试时可以塞 Fake 进来，
    从而在**没有 API key 的环境里**验证整条流水线。

    两个方法的分工：
      - `analyze()`      带媒体（视频/图片），用于粗筛与精析
      - `analyze_text()` 纯文本，用于字幕断句、翻译
    拆成两个是因为纯文本不需要"文件直传"能力，也不该走 base64 体积检查。
    """
    name: str

    def analyze(self, video: Path, prompt: str, *,
                system: str | None = None,
                max_tokens: int = MAX_TOKENS_DEFAULT,
                thinking: str = "auto",
                timeout: int = 600) -> AnalyzeResult: ...

    def analyze_text(self, prompt: str, *,
                     system: str | None = None,
                     max_tokens: int = MAX_TOKENS_DEFAULT,
                     thinking: str = "auto",
                     timeout: int = 180) -> AnalyzeResult: ...


def _retry_sleep(attempt: int) -> None:
    """指数退避：1, 2, 4, 8, 16 秒。"""
    time.sleep(min(2 ** attempt, 32))


class DashScopeTransport:
    """通道 A —— 官方 SDK，本地文件路径直传。"""

    name = "dashscope"

    def __init__(self, api_key: str, model: str = "", base_url: str = ""):
        self.api_key = api_key
        self.model = model or config.DASHSCOPE_MODEL_DEFAULT
        self.base_url = base_url or config.DASHSCOPE_BASE_URL

    @staticmethod
    def max_video_bytes() -> int:
        return LOCAL_PATH_LIMIT

    def analyze(self, video: Path, prompt: str, *,
                system: str | None = None,
                max_tokens: int = MAX_TOKENS_DEFAULT,
                thinking: str = "auto",
                timeout: int = 600) -> AnalyzeResult:
        if not self.api_key:
            raise AuthError("未配置 API key。请到「设置」页填写百炼 API Key。")
        try:
            import dashscope
            from dashscope import MultiModalConversation
        except ImportError as exc:
            raise AnalyzerError(
                "缺少 dashscope 包。请执行 `pip install dashscope`，"
                "或在设置页把端点改为兼容中转站（走 HTTP 通道）。") from exc

        messages = build_dashscope_messages(video, prompt, system)
        kw: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "api_key": self.api_key,
            "max_tokens": max(MAX_TOKENS_MIN, max_tokens),
        }
        kw.update(thinking_kwargs(thinking))
        if self.base_url and self.base_url != config.DASHSCOPE_BASE_URL:
            kw["base_address"] = self.base_url

        degraded: list[str] = []
        last_err: Exception | None = None
        for attempt in range(RETRY_MAX):
            t0 = time.time()
            try:
                rsp = MultiModalConversation.call(**kw)
                dt = time.time() - t0
            except Exception as exc:
                last_err = exc
                # 私有参数不被识别 → 摘掉重试（只摘一次）
                if kw.get("enable_thinking") is not None:
                    kw.pop("enable_thinking", None)
                    degraded.append("端点不认 enable_thinking，已摘除重试")
                    continue
                if attempt < RETRY_MAX - 1:
                    _retry_sleep(attempt)
                    continue
                raise AnalyzerError(f"调用失败：{exc}") from exc

            code = getattr(rsp, "status_code", 200)
            if code in (401, 403):
                raise AuthError(f"鉴权失败（HTTP {code}）：请检查 API Key 是否正确、是否已开通。")
            if code == 429 or code >= 500:
                last_err = AnalyzerError(f"HTTP {code}")
                if attempt < RETRY_MAX - 1:
                    _retry_sleep(attempt)
                    continue
                raise RateLimitError(f"服务端限流或异常（HTTP {code}），已重试 {RETRY_MAX} 次。")

            text, usage = _extract_dashscope(rsp)
            # ── 空返回自检（TECH-DESIGN §3.3）─────────────
            if not text and usage.reasoning_tokens > 0 and kw.get("enable_thinking"):
                kw["enable_thinking"] = False
                degraded.append(
                    "思考吃光了输出预算（reasoning>0 且正文为空），已关闭思考重试")
                continue

            return AnalyzeResult(
                text=text, usage=usage, transport=self.name, model=self.model,
                elapsed_s=dt, degraded=degraded, raw=rsp)

        raise AnalyzerError(f"调用失败：{last_err}")

    # ── 纯文本调用（字幕断句 / 翻译用）────────────────────
    def analyze_text(self, prompt: str, *,
                     system: str | None = None,
                     max_tokens: int = MAX_TOKENS_DEFAULT,
                     thinking: str = "auto",
                     timeout: int = 180) -> AnalyzeResult:
        """纯文本调用 —— 委托给 HTTP 兼容通道。

        为什么不走 SDK：SDK 在这里唯一不可替代的能力是**本地文件路径直传**
        （见 `analyze`），纯文本用不上它。走兼容接口意味着这套调用与
        `HTTPTransport` 共用同一份实现，只有一处需要维护。

        ⚠️ base_url 若是原生 API 地址（`.../api/v1`），需要换成兼容模式地址 ——
           两者域名相同、路径不同，填错会打到不存在的接口上。
        """
        http = HTTPTransport(self.api_key, self.model,
                             _to_compat_url(self.base_url))
        return http.analyze_text(prompt, system=system, max_tokens=max_tokens,
                                 thinking=thinking, timeout=timeout)


def _to_compat_url(base_url: str) -> str:
    """把 DashScope 的 base_url 规范成 **兼容模式** 地址。

    原生 API 与兼容模式域名相同、路径不同：

        https://dashscope.aliyuncs.com/api/v1              → 原生（SDK）
        https://dashscope.aliyuncs.com/compatible-mode/v1  → 兼容（HTTP）

    纯文本调用统一走兼容模式；已是兼容模式地址时原样返回。
    """
    b = (base_url or "").rstrip("/")
    if not b:
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"
    if "compatible-mode" in b:
        return b
    if "dashscope.aliyuncs.com" in b:
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"
    return b


def _extract_dashscope(rsp: Any) -> tuple[str, Usage]:
    """从 DashScope 响应里取正文与 usage。"""
    text_parts: list[str] = []
    try:
        out = getattr(rsp, "output", None) or {}
        choices = out.get("choices") or []
        for ch in choices:
            msg = ch.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("text"):
                        text_parts.append(c["text"])
    except Exception:
        pass

    usage = Usage()
    raw_usage = getattr(rsp, "usage", None)
    if raw_usage is not None:
        d = raw_usage if isinstance(raw_usage, dict) else getattr(
            raw_usage, "__dict__", {})
        usage = parse_usage(d)
    return "".join(text_parts).strip(), usage


class HTTPTransport:
    """通道 B —— OpenAI 兼容 HTTP，视频走 base64 data URI。

    两个网络适配开关，都是 2026-09-25 实测踩出来的，**不是可选的优雅设计**：

    `proxy` / `trust_env`
        默认 `trust_env=True` 会让请求行为取决于**启动进程的 shell 环境**
        （`HTTP_PROXY` 等）。实测：同一份配置，从带代理的终端启动就走代理，
        换个方式启动就直连 —— 这类"换个终端结果就不一样"的故障最难复现。
        所以这里把两件事分开：`trust_env` 决定要不要读环境，
        `proxy` 决定要不要显式指定。

    `sni_bypass`
        实测某中转站在本网络环境下**被 SNI 定向阻断**：TCP 握手成功、
        TLS 握手时被 RST（curl 的 schannel 与 Python 的 OpenSSL 两套栈表现一致）。
        改用 **IP 直连 + Host 头**（握手不带域名 SNI）即可 200。
        ⚠️ 代价：证书 CN 是域名，用 IP 连必然校验失败 ⇒ 只能 `verify=False`，
        **等于放弃中间人防护**。所以必须由用户显式开启，不能默认。
    """

    name = "http"

    def __init__(self, api_key: str, model: str = "", base_url: str = "",
                 *, proxy: str | None = None, trust_env: bool = True,
                 sni_bypass: bool = False):
        self.api_key = api_key
        self.model = model or config.DASHSCOPE_MODEL_DEFAULT
        self.base_url = (base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
                         ).rstrip("/")
        self.proxy = proxy or None
        self.trust_env = trust_env and not self.proxy
        self.sni_bypass = sni_bypass

    # ── 网络适配 ─────────────────────────────────────
    def _session(self):
        import requests
        s = requests.Session()
        # proxy 显式给了就只认它；否则按 trust_env 决定读不读环境变量。
        # 不让"走不走代理"交给运气。
        s.trust_env = self.trust_env
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    def _endpoint(self) -> tuple[str, dict, bool]:
        """返回 (chat_url, 附加请求头, verify)。

        ⚠️ 常规分支**必须直接用 `self.base_url`**，不能拿 `urlparse` 的 path
           再拼一次 —— base_url 里已经含路径（如 `/compatible-mode/v1`），
           再拼会变成 `.../compatible-mode/v1/compatible-mode/v1/...`，
           报一个看不出原因的 404。（这个错犯过一次。）
        """
        u = urlparse(self.base_url)
        path = (u.path or "").rstrip("/")
        host = u.hostname or ""

        if self.sni_bypass and host:
            try:
                ip = socket.gethostbyname(host)
                # 用 IP 组 URL ⇒ TLS 握手不带域名 SNI ⇒ 不被阻断；
                # 再用 Host 头把真实域名告诉服务端，路由才正确。
                return f"{u.scheme}://{ip}{path}/chat/completions", {"Host": host}, False
            except Exception:
                pass        # 解析失败就退回常规方式，让报错更直观
        return f"{self.base_url.rstrip('/')}/chat/completions", {}, True

    @staticmethod
    def max_video_bytes() -> int:
        """通道 B 的体积上限由 base64 字符串长度反推。

        base64 约为原始体积的 4/3 ⇒ 原始上限 ≈ 20M * 3/4 = 15MB。
        """
        return int(B64_SAFE_LIMIT * 3 / 4)

    def analyze(self, video: Path, prompt: str, *,
                system: str | None = None,
                max_tokens: int = MAX_TOKENS_DEFAULT,
                thinking: str = "auto",
                timeout: int = 600) -> AnalyzeResult:
        if not self.api_key:
            raise AuthError("未配置 API key。请到「设置」页填写。")

        est = b64_size_of(video)
        if est > B64_SAFE_LIMIT:
            raise AnalyzerError(
                f"分析副本过大：base64 约 {est / 1e6:.1f}M 字符，"
                f"超过安全上限 {B64_SAFE_LIMIT / 1e6:.0f}M。\n"
                "请调低副本码率或缩短候选段。")

        messages = build_http_messages(video, prompt, system)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max(MAX_TOKENS_MIN, max_tokens),
        }
        payload.update(thinking_kwargs(thinking))

        return self._chat_payload(payload, timeout)

    # ── 纯文本调用（字幕断句 / 翻译用）────────────────────
    def analyze_text(self, prompt: str, *,
                     system: str | None = None,
                     max_tokens: int = MAX_TOKENS_DEFAULT,
                     thinking: str = "auto",
                     timeout: int = 180) -> AnalyzeResult:
        """纯文本调用 —— 不带任何媒体。

        用途：字幕断句、翻译这类只需文本进出的场合。
        与 `analyze()` 共用同一套请求/重试/降级逻辑（`_chat_payload`），
        避免为纯文本另养一条调用路径 —— 否则"思考吃光预算"之类的
        降级逻辑会出现"只修在一个地方"的情况。
        """
        if not self.api_key:
            raise AuthError("未配置 API key。请到「设置」页填写。")

        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": max(MAX_TOKENS_MIN, max_tokens),
        }
        payload.update(thinking_kwargs(thinking))
        return self._chat_payload(payload, timeout)

    # ── 共用的请求逻辑 ────────────────────────────────────
    def _chat_payload(self, payload: dict, timeout: int) -> AnalyzeResult:
        """发一次 chat/completions，含重试与自动降级。

        `analyze()`（带媒体）与 `analyze_text()`（纯文本）都走这里。
        """
        url, extra_h, verify = self._endpoint()
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json", **extra_h}
        if not verify:
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
        sess = self._session()

        degraded: list[str] = []
        last_err: Exception | None = None
        for attempt in range(RETRY_MAX):
            t0 = time.time()
            try:
                r = sess.post(url, headers=headers,
                              data=json.dumps(payload).encode("utf-8"),
                              timeout=timeout, verify=verify)
            except Exception as exc:
                last_err = exc
                if attempt < RETRY_MAX - 1:
                    _retry_sleep(attempt)
                    continue
                raise AnalyzerError(_explain_network_error(exc, url)) from exc
            dt = time.time() - t0

            if r.status_code in (401, 403):
                raise AuthError(
                    f"鉴权失败（HTTP {r.status_code}）：请检查 API Key 与端点是否正确。")
            # 私有参数被拒 → 摘除重试
            #
            # ⚠️ 必须**先确认这个 400 真的是因为这个参数**，不能"看到 400 且
            #    payload 里有 enable_thinking 就摘掉重试"。
            #    实测踩过：百炼欠费时也返回 400（"Access denied ...
            #    overdue-payment"），早先的实现会顺手把 enable_thinking 摘掉、
            #    重试一次，然后把错误信息写成"端点拒绝 enable_thinking" ——
            #    **真正的病因（欠费）被替换成了一句误导**，用户会去反复改配置。
            if (r.status_code in (400, 422) and "enable_thinking" in payload
                    and "thinking" in (r.text or "").lower()):
                payload.pop("enable_thinking", None)
                degraded.append(
                    f"端点拒绝 enable_thinking（HTTP {r.status_code}），已摘除重试")
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last_err = AnalyzerError(f"HTTP {r.status_code}: {r.text[:200]}")
                if attempt < RETRY_MAX - 1:
                    _retry_sleep(attempt)
                    continue
                raise RateLimitError(
                    f"服务端限流或异常（HTTP {r.status_code}），已重试 {RETRY_MAX} 次。")
            if r.status_code >= 400:
                raise AnalyzerError(_explain_http_error(r))

            data = r.json()
            text, usage = _extract_openai(data)
            if not text and usage.reasoning_tokens > 0 and payload.get("enable_thinking"):
                payload["enable_thinking"] = False
                degraded.append(
                    "思考吃光了输出预算（reasoning>0 且正文为空），已关闭思考重试")
                continue

            return AnalyzeResult(
                text=text, usage=usage, transport=self.name, model=self.model,
                elapsed_s=dt, degraded=degraded, raw=data)

        raise AnalyzerError(f"调用失败：{last_err}")


def _extract_openai(data: dict) -> tuple[str, Usage]:
    """从 OpenAI 兼容响应里取正文与 usage。"""
    text = ""
    try:
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text = "".join(x.get("text", "") for x in c if isinstance(x, dict))
    except Exception:
        pass
    return text.strip(), parse_usage(data.get("usage") or {})


# ─────────────────────────────────────────────────────────────
# 网络错误的人话解释
# ─────────────────────────────────────────────────────────────
def _explain_network_error(exc: Exception, url: str) -> str:
    """把 requests 的裸异常翻成能照着做的建议。

    ⚠️ catch 顺序有讲究：`ConnectTimeout` 是 `Timeout` 的子类，
       如果先判 Timeout，连接超时会被吃掉、给出"关掉思考"这种
       完全不相干的建议。所以这里按"最具体优先"逐个判。
    """
    name = type(exc).__name__
    detail = str(exc)[:200]

    # 只在 TLS 握手阶段被重置 —— 典型 SNI 阻断
    if "SSL" in detail or "UNEXPECTED_EOF" in detail or "schannel" in detail:
        return (f"TLS 握手失败（{name}）。\n"
                "如果用的是中转站，多半是**域名被 SNI 定向阻断**：\n"
                "TCP 能连、一发握手包就被重置。\n"
                "→ 到「设置 → 网络」打开「绕过 SNI 阻断」后重试。\n"
                f"原始信息：{detail}")
    if "ProxyError" in name or "ProxyError" in detail:
        return (f"代理连接失败（{name}）。这**不是模型服务的问题**，\n"
                f"是请求被送进了一个连不到目标的代理。\n"
                f"→ 到「设置 → 网络」把代理模式改为「直连」或换一个代理。\n"
                f"原始信息：{detail}")
    if "ConnectTimeout" in name:
        return (f"连接超时（{name}）—— 连都没连上。\n"
                "可能是域名解析被污染，或该地址不可达。\n"
                f"→ 检查网络，或改走代理。\n原始信息：{detail}")
    if "Timeout" in name:
        return (f"读取超时（{name}）—— 连上了但服务端在超时内没返回。\n"
                "如果模型带思考能力，可能是思考把时间吃光了。\n"
                "→ 到「设置」把思考模式改为「强制关闭」后重试。\n"
                f"原始信息：{detail}")
    if "ConnectionReset" in name or "10054" in detail:
        return (f"连接被重置（{name}）。\n"
                f"若发生在 TLS 阶段，见「绕过 SNI 阻断」；\n"
                f"若一开始就被重置，检查是否有安全软件拦截。\n原始信息：{detail}")
    return f"网络请求失败（{name}）：{detail}"


def _explain_http_error(r) -> str:
    """把 HTTP 错误体翻成用户能理解的说明。"""
    code = ""
    msg = ""
    try:
        body = r.json()
        err = body.get("error") or {}
        code = str(err.get("code") or "")
        msg = str(err.get("message") or "")
    except Exception:
        msg = (r.text or "")[:200]

    if code == "no_available_channel":
        return (f"中转站报「无可用渠道」（HTTP {r.status_code}）。\n"
                "含义：**这把 key 通过了认证，但当前没有任何上游渠道能承接这个模型。**\n"
                "常见原因：账号余额/套餐不足、该模型未在套餐内、或上游临时下线。\n"
                "→ 请到中转站后台确认账号状态与模型授权；这不是本机配置问题。\n"
                f"原始信息：{msg[:180]}")
    if code in ("model_not_found", "Model not exist"):
        return (f"模型名不存在（HTTP {r.status_code}）。\n"
                f"→ 到「设置」核对模型名，或点「测试连接」拉取可用清单。\n"
                f"原始信息：{msg[:180]}")
    if code == "invalid_api_key":
        return (f"API Key 无效（HTTP {r.status_code}）。\n"
                f"→ 到「设置」重新填写。\n原始信息：{msg[:180]}")

    # 账号状态异常（**最常见的原因是欠费**）
    #
    # 实测：百炼欠费时返回 HTTP 400 + 一段英文
    #   "Access denied, please make sure your account is in good standing.
    #    For details, see: .../error-code#overdue-payment"
    # 这段话里没有一个字提示"钱不够"，用户很容易误判成 key 失效或网络问题，
    # 然后去反复改配置 —— 而真正要做的是去控制台充值。
    low = (msg or "").lower()
    # ★ 内容审核拦截（DataInspectionFailed）
    #
    # 这一条**必须单独说**：它的成因与"配置/网络"完全无关，
    # 而且对口语化素材（直播、播客、访谈）是**常见**情况：
    #
    #     Input data may contain inappropriate content   → 送入的内容被判不合适
    #     Output data may contain inappropriate content  → **模型输出**被判不合适
    #
    # 后者更常见：模型复述了素材里的激烈用词（脏话、攻击性表达），被判违规。
    #
    # ⚠️ 绝不能把它归进"请检查 API Key / 端点 / 网络"那一类 ——
    #    用户会去改一堆无关的配置，改完发现毫无用处，然后以为软件坏了。
    #    （实测踩过：直播素材触发 Output 审核，报错却说"多半是 Key/端点/网络"。）
    if "datainspectionfailed" in low or "inappropriate content" in low:
        where = "模型的输出" if "output data" in low else "送入的内容"
        return (
            f"服务端内容审核拦下了这次请求（HTTP {r.status_code}）。\n"
            f"→ 被拦的是「{where}」—— 素材里的用词、或模型复述的原话\n"
            f"  被判定为不合适。\n"
            f"→ 这不是配置、Key 或网络的问题，改设置不会有帮助。\n"
            f"→ 可以试试：换一段用词平和的素材；或把需求描述写得更中性\n"
            f"  （让模型少复述原话、多作概括）。\n"
            f"原始信息：{msg[:180]}")

    if ("overdue" in low or "in good standing" in low or "access denied" in low
            or code in ("AccessDenied", "Arrearage", "Overdue", "InvalidApiKeyOverdue")):
        return (f"账号状态异常，当前无法调用模型（HTTP {r.status_code}）。\n"
                "含义：**这把 key 本身是有效的，是账号当前不可用** —— "
                "最常见的原因是**欠费**（错误原文里的 overdue-payment 就是在说这个）。\n"
                "→ 请到模型服务商控制台检查账户余额与账单；充值后无需改配置即可恢复。\n"
                "→ 这不是本机配置或网络问题，改设置不会有用。\n"
                f"原始信息：{msg[:180]}")

    return f"HTTP {r.status_code}：{msg[:300]}"


# ─────────────────────────────────────────────────────────────
# 连通性诊断（供设置页的「测试连接」用）
# ─────────────────────────────────────────────────────────────
def diagnose(base_url: str, api_key: str, *, proxy: str | None = None,
             trust_env: bool = True, sni_bypass: bool = False,
             timeout: int = 20) -> dict:
    """一步步排查到"能不能真的发起一次调用"。

    顺序按 openai-compat-endpoint-debug：先把网络层排除，再看模型层。
    返回 {"ok": bool, "steps": [...], "suggestion": str, "models": [...]}。
    """
    import requests

    steps: list[dict] = []
    models: list[str] = []
    u = urlparse(base_url)
    host = u.hostname or ""

    def step(name: str, ok: bool, detail: str) -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})

    # ① 代理环境
    import os
    env_proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "")
    step("代理环境", True,
         f"环境变量代理 = {env_proxy or '（无）'}；本次使用 = "
         f"{proxy or ('跟随环境' if trust_env else '直连')}")

    if not api_key:
        step("API Key", False, "未配置")
        return {"ok": False, "steps": steps, "models": [],
                "suggestion": "先到「设置」页填写 API Key。"}
    step("API Key", True, f"{len(api_key)} 字符")

    # ② DNS
    ip = ""
    try:
        ip = socket.gethostbyname(host)
        step("DNS 解析", True, f"{host} → {ip}")
    except Exception as exc:
        step("DNS 解析", False, str(exc))

    # ③ 组请求方式（⚠️ 常规分支直接用 base_url，不重组 path，避免路径重复）
    if sni_bypass and ip:
        path = (u.path or "").rstrip("/") + "/models"
        url = f"{u.scheme}://{ip}{path}"
        extra = {"Host": host}
        verify = False
        mode = f"IP+Host（绕过 SNI）→ {ip}"
    else:
        url = f"{base_url.rstrip('/')}/models"
        extra = {}
        verify = True
        mode = "常规域名直连"
    step("连接方式", True, mode)

    if not verify:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

    sess = requests.Session()
    sess.trust_env = trust_env and not proxy
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}

    # ④ 真发一次请求
    try:
        r = sess.get(url, headers={"Authorization": f"Bearer {api_key}", **extra},
                     timeout=timeout, verify=verify)
    except Exception as exc:
        step("发起请求", False, f"{type(exc).__name__}")
        return {"ok": False, "steps": steps, "models": [],
                "suggestion": _explain_network_error(exc, url)}

    if r.status_code != 200:
        step("发起请求", False, f"HTTP {r.status_code}")
        return {"ok": False, "steps": steps, "models": [],
                "suggestion": _explain_http_error(r)}
    step("发起请求", True, f"HTTP 200，{len(r.content)} 字节")

    # ⑤ 模型清单
    try:
        models = [m.get("id", "") for m in r.json().get("data", [])]
        step("模型清单", bool(models), f"共 {len(models)} 个：{', '.join(models[:8])}"
             + ("…" if len(models) > 8 else ""))
    except Exception as exc:
        step("模型清单", False, f"解析失败：{exc}")

    suggestion = "网络与鉴权都正常。"
    if models and len(models) <= 2:
        suggestion += (f"\n注意：这把 key 只看到 {len(models)} 个模型，"
                       "通常说明授权范围被限定得比较窄。")
    return {"ok": True, "steps": steps, "models": models,
            "suggestion": suggestion}


# ─────────────────────────────────────────────────────────────
# 工厂
# ─────────────────────────────────────────────────────────────
def make_transport(base_url: str | None = None, api_key: str | None = None,
                   model: str | None = None,
                   force_channel: str | None = None) -> Transport:
    """按配置装配 transport。

    优先级：显式参数 > settings > secrets > 默认值。
    `force_channel` 供设置页的"手动覆盖"用。
    """
    key = api_key if api_key is not None else secrets.get_api_key()
    burl = base_url if base_url is not None else str(
        settings.get("base_url", "") or config.DASHSCOPE_BASE_URL)
    mdl = model if model is not None else str(
        settings.get("model", "") or config.DASHSCOPE_MODEL_DEFAULT)

    # 网络适配（见 HTTPTransport 的 docstring）
    sni = bool(settings.get("sni_bypass", False))
    mode = str(settings.get("proxy_mode", "system"))
    proxy = str(settings.get("proxy_url", "") or "") if mode == "manual" else None
    trust_env = (mode == "system")

    channel = force_channel or detect_channel(burl)
    if channel == "dashscope":
        return DashScopeTransport(key, mdl, burl)
    return HTTPTransport(key, mdl, burl, proxy=proxy, trust_env=trust_env,
                         sni_bypass=sni)
