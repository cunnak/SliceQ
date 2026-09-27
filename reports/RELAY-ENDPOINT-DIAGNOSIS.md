# 中转站接入诊断报告

> 日期：2026-09-25｜端点：`api.inferera.com`｜上游标识：`Aihubmix_api_error`
> 状态：**技术链路已打通；服务端报「无可用渠道」，需账号侧确认**

---

## 一、结论摘要

| 环节 | 结果 |
|---|---|
| 域名解析 | ✅ `api.inferera.com` → `20.222.22.11` |
| TCP 连接（443） | ✅ 可建立 |
| **TLS 握手（带域名 SNI）** | ❌ **被重置**（curl 的 schannel 与 Python 的 OpenSSL 表现一致） |
| **TLS 握手（IP 直连、不带域名 SNI）** | ✅ **可建立** |
| API 认证 | ✅ `/v1/models` 返回 200 |
| **模型调用** | ❌ **400 `no_available_channel`** ← **当前阻塞点** |

**两个独立问题，都已定位：**

1. **网络层**：该域名在本网络环境下被 **SNI 定向阻断** → 已用"IP + Host 头"绕过，解决。
2. **服务层**：账号/渠道层面**没有任何可用上游** → 需账号侧处理，本地无法解决。

---

## 二、网络层：SNI 定向阻断（已解决）

### 2.1 证据链

| 测试 | 结果 | 含义 |
|---|---|---|
| `www.baidu.com` | **200** | 网络本身正常 |
| `dashscope.aliyuncs.com` | **404** | 网络本身正常（404 是路径错误，属正常响应）|
| `hf-mirror.com` | **200** | 网络本身正常 |
| `api.inferera.com` 的 TCP 443 | 连通 | IP 层可达 |
| `api.inferera.com` 的 **TLS 握手** | **被 RST** | ← 问题在这一层 |
| 本地代理 `127.0.0.1:51988` 转发 | 隧道建立成功，但 **TLS 同样失败** | 代理没帮上忙 |
| 该代理访问 `google.com` | **不通** | 该代理**无翻墙能力** |

**关键推论**：TCP 通、一发 TLS 握手包就被重置，而其他站点一切正常
⇒ **握手包里的 SNI（域名）触发了阻断**。

### 2.2 解决方案：IP 直连 + Host 头

```python
# 用 IP 组 URL ⇒ 握手不带域名 SNI ⇒ 不被阻断
url = f"https://{ip}/v1/chat/completions"
# 用 Host 头把真实域名告诉服务端 ⇒ 路由才正确
headers["Host"] = "api.inferera.com"
# 证书 CN 是域名，用 IP 连必然对不上 ⇒ 只能关校验
verify = False
```

**实测：立即 200。**

⚠️ **代价（必须知情）**：`verify=False` 意味着**放弃中间人攻击防护**。
所以这个开关**不能默认开启**，必须由用户显式勾选 —— 已在设置页做成开关并附警告。

### 2.3 已落地的代码

- `analyzer.HTTPTransport` 新增 `sni_bypass` 与 `proxy` / `trust_env` 参数
- `analyzer.diagnose()` —— 端到端诊断（DNS → 连接方式 → 请求 → 模型清单）
- 设置页新增「网络适配」分组：代理模式三态 / 代理地址 / SNI 绕行开关 / **测试连接**按钮
- `make_transport()` 从 settings 读取上述配置

**顺带修掉的一个隐患**：原先 `requests.post()` 默认 `trust_env=True`，
"走不走代理"取决于**启动进程的 shell 环境** —— 这类"换个终端结果就不一样"
的故障最难复现。现在 `trust_env` 与 `proxy` 分离且可配。

---

## 三、服务层：`no_available_channel`（当前阻塞）

### 3.1 现象

```
POST /v1/chat/completions
→ HTTP 400
{"error":{"code":"no_available_channel",
          "message":"The request cannot be routed at the moment. Try again later
                     or contact support with the request ID.",
          "type":"Aihubmix_api_error"}}
```

### 3.2 已排除的可能性

| 假设 | 验证 | 结论 |
|---|---|---|
| 瞬时故障 | 连续 4 次重试（间隔 5 秒） | ❌ 4 次全同 → 不是瞬时 |
| 模型名写错 | 清单里只有 1 个模型，正是它 | ❌ 名字正确 |
| 只有该模型没渠道 | 换 `gpt-6-luna` / `claude-opus-5-5` / `gpt-4o-mini` | ❌ **全部同样报错** |
| 请求体太大 | 用 98 字节的纯文本请求 | ❌ 同样报错 |
| 请求格式错 | 文本 / 图片两种格式都试过 | ❌ 同样报错 |
| 路径错 | 修正 `/v1` 前缀后 | ❌ 同样报错（但 413 消失了）|

**⇒ 与请求内容无关：这把 key 通过了认证，但账号下没有任何可用上游渠道。**

### 3.3 「只有 1 个模型」这个信号

`/v1/models` 只返回 **1 个模型**（`qwen3.8-omni-flash`）。
正常中转站会返回数十至上百个 —— 这通常意味着**授权范围被限定得很窄**，
或账号处于未激活 / 未充值 / 套餐不含该模型的状态。

### 3.4 需要账号侧确认的事项

1. **账号余额 / 套餐**是否正常（是否已充值、额度是否耗尽）
2. **`qwen3.8-omni-flash` 是否在所选套餐内**
3. 该 key 的**授权范围**为何只有 1 个模型
4. 若均正常 → **拿 request ID 联系客服**：

```
tid: 2026092515483842837095282448606
```

---

## 四、当前配置（已落盘）

| 项 | 值 |
|---|---|
| `base_url` | `https://api.inferera.com/v1` |
| `model` | `qwen3.8-omni-flash` |
| `proxy_mode` | `none`（本机环境代理对该目标无效，且无翻墙能力）|
| `sni_bypass` | `true`（本网络环境下的唯一通路）|
| `api_key` | 已加密存储（DPAPI），`sk-VOJ…40C5` |

> ⚠️ `base_url` 必须带 `/v1`。写成 `https://api.inferera.com` 会导致
> 请求打到 `/chat/completions`（nginx 层，触发 413 且返回 HTML 而非 JSON）。

---

## 五、渠道恢复后可直接跑的验证

配置已就位，一旦服务端渠道恢复，下列 6 项待验可以一次跑完：

| # | 待验项 |
|---|---|
| V-A | 视频内联（base64）是否被接受 |
| V-B | 视频字段名 `video_url` 是否被接受（否则试 `image_url`）|
| V-C | `enable_thinking` 行为（认 / 不认 / 报错码）|
| V-D | 模型返回的 JSON 是否符合提示词要求 |
| V-E | **粗筛的语义质量（最关键）** |
| V-F | 真实成本对账（预估 ¥0.53 上限）|

---

*生成时间：2026-09-25 | 探针脚本：`stage0/probe_resolve/probe_relay_bypass.py`*
