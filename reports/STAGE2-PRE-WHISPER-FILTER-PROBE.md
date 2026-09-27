# SliceQ 阶段 2 前置实测 — ffmpeg whisper 滤镜

- 日期：2026-09-25
- 目的：**在动手写 asr.py 之前，先把转录引擎的真实行为摸清**
- 探针：`stage0/probe_resolve/probe_whisper_args.py`（可重复跑）
- 模型：`ggml-tiny.bin`（74MB）、`ggml-base.bin`（141MB）

---

## 一、结论速览

| # | 发现 | 性质 |
|---|------|------|
| 1 | **传参必须用 `\\:` 双反斜杠转义 Windows 盘符冒号** | 🔴 不这样写直接跑不通 |
| 2 | **输出是 JSON Lines**（每行一个对象），不是 JSON 文档 | 🟡 解析方式不同 |
| 3 | 字段：`start` / `end`（**毫秒**）/ `text`；分段会**轻微重叠** | 🟡 与"秒"的直觉不符 |
| 4 | **对无语音音频会产生幻觉**，而非返回空 | 🔴 影响粗筛判据 |
| 5 | **中文转录质量未验证**（缺中文测试素材） | ⚠️ **最大的未知** |

---

## 二、发现 1：滤镜参数里的 Windows 路径必须双重转义

### 现象

```python
af = f"whisper=model={MODEL}:language=zh:format=json:..."
# MODEL = C:\Users\...\ggml-tiny.bin
```

```
[AVFilterGraph] No option name near '/Users/<user>/...'
Error parsing filterchain 'whisper=model=C:/Users/...'
Error opening output files: Invalid argument
```

### 根因

ffmpeg 的滤镜参数用 `:` 分隔，而 Windows 盘符 `C:` 的冒号与分隔符撞车 ——
解析器在 `model=C` 处断开，把后面的路径当成了"选项名"。

### 四种写法的实测对照

| 写法 | 结果 |
|------|------|
| `model=C:/Users/...` | ❌ |
| `model=C\:/Users/...`（单反斜杠） | ❌ |
| `model='C:/Users/...'`（单引号） | ❌ |
| **`model=C\\:/Users/...`（双反斜杠）** | ✅ |

### 为什么单反斜杠不够

**ffmpeg 的滤镜串要穿过两层解析**：

1. 第一层：按 `,` `;` 切分 filtergraph
2. 第二层：在单个 filter 内按 `:` 切分参数

单个 `\:` 在**第一层就被消费掉**，到不了第二层。必须 `\\:`。

### 实现要求

```python
def escape_filter_path(p) -> str:
    """Windows 路径 → 可放进 ffmpeg 滤镜参数的安全写法。"""
    return str(p).replace("\\", "/").replace(":", r"\\:")
```

**所有路径型滤镜参数都要这样处理**——不只是 `model`，`destination` 也一样。
（本次探针第一次失败就是因为只转义了 `model` 忘了 `destination`。）

---

## 三、发现 2/3：输出格式是 JSONL，时间戳是毫秒

### 原始输出

```json
{"start":0,"end":3000,"text":"sliceq audio test, the secret number is"}
{"start":2944,"end":4064,"text":"7 4 2 1."}
```

### 三个必须注意的点

1. **是 JSON Lines，不是 JSON 文档** —— 直接 `json.loads()` 会报
   `JSONDecodeError: Extra data: line 2 column 1`。必须逐行解析。
2. **`start` / `end` 单位是毫秒**（整数），不是秒，也不是 `HH:MM:SS`。
3. **相邻分段会重叠**：前段 `end=3000`，后段 `start=2944`。
   ⇒ 下游做时间轴运算时不能假设 `prev.end == next.start`。

---

## 四、发现 4：无语音音频会产生幻觉（影响粗筛设计）

### 实测

| 素材 | 音频特征 | tiny 输出 | base 输出 |
|------|---------|-----------|-----------|
| `speech_video.mp4`（英语语音） | mean −16.3dB / max −5.1dB | `Slice Q audio test, the secret number is...` / `7-4-1` | `Slice Q audio test. The secret number is` / `741.` |
| `source.mp4`（**非语音**） | mean −21.1dB / max −13.0dB | `(你不想想)` / `(你不想去)` ×3 | `(字幕:J Chong)` ×4 |

**`source.mp4` 的两份输出完全不同，且都不是真实内容 ⇒ 典型的幻觉。**

### 为什么这件事重要

**不能把"转录出了文本"当成"这段有语音"的证据。**

粗筛环节若按"转录文本的语义密度"排序，幻觉文本会污染排序结果 ——
把没有语音的片段推成候选，白白烧掉精析的钱。

⇒ **`vad_model`（静音检测）不是"可选优化"，而是正确性要求。**
TECH-DESIGN §2.1 已提到 `vad_model`，但当时的理由是"加速"；
**现在要升级为"防幻觉"**。

---

## 五、发现 5：中文质量未验证（**当前最大的未知**）

### 我原本的验证方案是错的

`test_media/subtitle.srt` 的内容是：

```
这一段是模型找到的第一个候选高光
主播在这里有一波关键操作
弹幕在这几秒出现了明显峰值
```

**这不是转录标准答案** —— 它是为"切片演示"手写的示例字幕。
我一开始拿它当 ground truth 去对照 `source.mp4` 的转录，**参照物找错了**。

### 现状

| 项 | 状态 |
|---|------|
| 英文转录质量 | ✅ 已验证（tiny 可用，base 更好） |
| **中文转录质量** | ❌ **完全没有验证** |
| 中文模型档位选择（tiny/base/small） | ❌ 无法判断 |

**而 SliceQ 的目标用户全是中文直播切片。**

### 需要什么才能验证

一段**已知内容的中文语音**（30 秒以上），最好是直播腔调。
可用的获取方式：

1. 用 TTS 合成一段已知文本（Windows 自带 TTS / 在线 TTS）
2. 从一段真实中文视频里截 30 秒 + 人工记下说了什么
3. 用已有素材：**本机 `downloads/` 或任意中文视频**

**在验证之前，不应把"默认模型"定死在 `ggml-base`。**

---

## 六、其他实测数据

| 项 | 值 |
|---|---|
| base 模型下载 | 141.1 MB / **6 秒**（≈23MB/s，hf-mirror） |
| tiny 转录 4.2s 英文 | 1.1s |
| base 转录 4.2s 英文 | 1.6s |
| tiny 转录 12s 素材 | 19.4s（**异常慢**，怀疑首次运行/模型加载开销） |
| base 转录 12s 素材 | 3.2s |

> ⚠️ tiny 比 base 慢 6 倍这点不合常理，**怀疑是首次加载/缓存效应**，
> 需要在阶段 2 用长素材重新测，才能得出可用的"速度 vs 模型档位"结论。

---

## 七、对阶段 2 的直接影响

| 环节 | 受影响的点 |
|---|---|
| `asr.py` 接口 | 必须内建 `escape_filter_path()`；解析 JSONL；毫秒时间戳 |
| **默认模型选择** | **暂不能定**，取决于中文质量验证结果 |
| `vad_model` | **从"可选优化"升级为"必需"**（防幻觉） |
| 粗筛判据 | 不能仅凭"有转录文本"判定有语音；需结合 VAD 结果 |
| 时间轴运算 | 不能假设分段首尾相接（有重叠） |

---

*生成时间：2026-09-25 | 阶段：2-前置 | 探针：`probe_resolve/probe_whisper_args.py`*
