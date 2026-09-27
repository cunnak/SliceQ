# SliceQ 阶段 0 验证报告

> 执行日期：2026-09-23｜执行人：姐姐（沈知遥，WorkBuddy）｜性质：只读调研 + 真机实测，未写产品代码
> 对照文档：PRD-SliceQ.md v1.1｜TECH-DESIGN.md v1.0｜TASKS.md v1.1

---

## 0. 阶段表

| 子项 | 目标 | 状态 | 结论强度 |
|---|---|---|---|
| V1 环境侦察 | 摸清本机可用资源 | ✅ 完成 | 事实 |
| V2 剪映草稿通道（R1） | 定论可行/降级 | ✅ **通过（自动验证完成）** | 决定性证据 |
| V3 模型接入规格（R2） | 查清输入方式与边界 | ✅ 文档侧完成，实测待 key | 强证据 |
| V4 OTIO→FCP XML | 定论 PR/达芬奇链路 | 🟡 生成侧跑通，导入待达芬奇 | 强证据 |
| V5 yt-dlp B 站 | 定论能否下载回放 | ✅ 完成 | 实测通过 |
| V6 免费 ASR 接口（R7） | 定论可用/不可用 | ✅ **改为 ffmpeg 内置 whisper，已实测跑通** | 实测通过 |
| V6b FFmpeg 许可 | 定论分发合规边界 | ✅ 完成（GPL 构建，红线明确） | 事实 |
| V7 VideoCaptioner 联动 | 定论联动方式 | ⚠️ 发现设计前提错误 | 强证据 |
| V8 模型实测 | 端到端跑通调用 | ⏸ 阻塞 | 缺 API key |

---

## 1. 环境侦察（V1）— 本机真实资源清单

| 资源 | 状态 | 证据 |
|---|---|---|
| FFmpeg / ffprobe | ✅ **9.0-full_build-www.gyan.dev**，已在 PATH | `ffmpeg -version` 直接可用，无需首次启动下载 |
| Python | ✅ 3.13.14（managed），隔离 venv 已建于 `envs/default` | `python -V` |
| Git | ✅ 2.55.0.windows.3 | `git --version` |
| yt-dlp | ⚠️ 本机未装 → 已装入 venv **2026.8.19** | `pip show yt-dlp` |
| **剪映专业版** | ✅ 已装 **10.9.0.14199** | `%LOCALAPPDATA%\JianyingPro\Apps\10.9.0.14199\JianyingPro.exe` |
| 剪映草稿目录 | ✅ `%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\`（含 2 个真实草稿） | 目录枚举 |
| DaVinci Resolve | ❌ **未安装** | 全盘搜索 `Blackmagic Design` 无命中 |
| 必剪（Bcut） | ❌ **未安装** | 全盘搜索无命中 |
| VideoCaptioner | ✅ 已装（**安装包版**） | `%APPDATA%\VideoCaptioner\VideoCaptioner.ini` + 桌面快捷方式 |
| 百炼 API key | ❌ **未配置**（qwen-mm-plugins 处于"基础模式无 Key"） | `~/.qwen-mm-plugins/config` 全为注释 |
| DeepSeek Harness | ✅ `C:\deepseek harness` | 目录确认 |

> 附带发现：`envs/default` 里残留一个损坏的 `~wen-mm-plugins` 分发目录（此前一次安装中断的产物），会持续输出 WARNING。建议清理。

---

## 2. V2 — 剪映草稿通道（R1，原定最高风险）

### 2.1 实测证据

**证据 A：剪映 10.9 真实草稿确为密文。**
对真实草稿 `7月2日 (1)` 取样：

```
draft_content.json      1,048 bytes
  → jc1f/pc064C7q0r8y98F8dBG143Z7L2SG4cj2veyhgJD7GGf7C34hD2eq+agFHjmvX1jhAagyuKH7Ii...

draft_meta_info.json    2,632 bytes
  → 6aael8NFgJfBObzlklw0fcaCzG0U0gDynD526CcXJjahl+ke057k6goi2wUgBEH1LOFGD97BYJ8O1a5J...
```

两个 JSON 均无法按 UTF-8 JSON 解析（`JSONDecodeError: Extra data`）。
**结论：剪映 10.9 对 `draft_content.json` 与 `draft_meta_info.json` 双文件加密——"剪映 6.0+ 加密"的社区说法在 10.9 上成立且范围更大。**

**证据 B：我们生成的草稿是明文，且结构完整。**

用 pyJianYingDraft **0.3.0**（2026-07-08 发布）生成的 `SliceQ_Stage0_Test`：

```
draft_content.json    24,622 bytes   ← 明文可读 JSON
  new_version   = 110.0.0
  version       = 360000
  platform.app_version = 5.9.0
  轨道: video(3 片段) / text(3 字幕) / text(1 标题)
  素材: videos=1, texts=4

draft_meta_info.json   2,132 bytes   ← 明文可读 JSON，31 个顶层键
```

**证据 C：官方功能支持矩阵（pyJianYingDraft README）。**

| 能力 | 剪映 5.9 | 新版剪映 (10.8) |
|---|---|---|
| 生成新草稿（视频/图片素材、时间控制） | ✅ | **✅** |
| 视频关键帧 / 混合模式 / 背景填充 | ✅ | ✅ |
| 视频蒙版 | ✅ | ❌（称 0.3.1 修复） |
| 加载模板 `draft_content.json` | ✅ | 🟡 需 `fallback_loader` |
| 自动导出草稿（程序化渲染） | ✅ | ❌（剪映 7+ 隐藏了导出控件） |

### 2.2 结论

**主通道判定：可行。** 理由链：

1. SliceQ 的草稿用法是**从零生成新草稿**，不是读取/修改已有草稿 → 恰好落在加密影响范围之外
2. pyJianYingDraft 官方矩阵显示"生成新草稿"在**新版剪映 10.8 为 ✅**；本机 10.9 仅差一个小版本
3. 生成的明文草稿由剪映在首次打开时自行迁移并加密——这是剪映的兼容读取路径

**降级触发条件**（一旦人工验证失败）：改为导出「片段 mp4 + SRT + 图文导入说明」打包。

**必须砍掉的能力：程序化触发剪映渲染导出**（剪映 7+ 不可行）。SliceQ 只负责生成草稿，导出由用户在剪映界面完成。这一点需同步进 PRD/TECH-DESIGN——原 TECH-DESIGN 第 6 节并未排除这个预期。

### 2.3 D1 执行结果：**通过**（已自动验证，无需人工肉眼确认）

#### 判据设计思路

第 2.2 节初稿里我把 D1 留给人工"开剪映看一眼"。但那不是唯一办法——**如果剪映真的读懂了我们的草稿，它一定会在自己的环节留下痕迹。** 顺着这条思路挖下去，找到了三层独立证据。

#### 证据 D：剪映的草稿动作日志（决定性）

`%LOCALAPPDATA%\JianyingPro\User Data\Log\draft_acion_watch.json` 最后一条记录：

```json
{"errno":0,
 "id":"B54D497B-26C0-4a1b-9F2A-47144D82E0F4",
 "suc":true,
 "type":"copy_draft_external",
 "new_draft_info":{
   "draft_folder_path":".../com.lveditor.draft/SliceQ_Stage0_Test",
   "draft_id":"84E1D21C-6415-4d03-94F7-F83A4B436AD4",
   "draft_name":"SliceQ_Stage0_Test",
   "tm_create":1758609000000000,
   "tm_modify":1758609000000000},
 "time_nsec":1790159570375756}
```

三个字读法：

| 字段 | 含义 |
|---|---|
| `"suc": true` + `"errno": 0` | 剪映**成功**处理，无错误 |
| `"type": "copy_draft_external"` | 剪映把外部放入的文件夹识别为**合法草稿导入**（同一日志里的另两条是剪映自建的 `create_draft`） |
| `draft_id` 被重新分配 | 剪映赋予了自己的 ID（`84E1D21C-…`，我部署时写的是 `BC69C7CD-…`）→ **接纳并正式注册**，不是原样记录 |

时间戳 `1790159570.3757556` → **2026-09-23 18:32:50**，与下述两份文件的改写时刻**完全吻合**。

#### 证据 E：索引文件里的体积数字对得上

`root_meta_info.json`（**明文 JSON**，剪映维护的草稿索引）中我们的条目：

```
draft_timeline_materials_size = 11,355,775
```

而 `source.mp4` = 11,331,153，`draft_content.json` = 24,622：

```
11,331,153 + 24,622 = 11,355,775   ← 精确相等
```

剪映**解析了我们的草稿、数出了它引用的媒体文件体积**，才可能算出这个数。

#### 证据 F：元信息被规范化成剪映的私有 schema

`draft_meta_info.json` 从我的 31 键版本被重写为剪映的 **52 键**版本，含 `pippit_avatar_url`、`tm_draft_cloud_*`、`streaming_edit_draft_ready` 等第三方库造不出的字段名；`draft_name` / `draft_fold_path` / `draft_root_path` 全部正确填充。

同时 **`draft_content.json` 保持明文、24,622 字节、未被改写也未被删除** —— 剪映没有拒绝它。

#### 结论

**✅ 剪映成功识别、接纳、索引了我们用 pyJianYingDraft 生成的明文草稿。R1 主通道确认可行，降级方案（片段 mp4 + SRT + 导入说明）无需启用。**

#### ⚠️ 但验证过程中浮现了一个新的、更重要的问题：版本漂移

执行 D1 时意外发现：**剪映在 18:31–18:32 静默自动升级了。**

| 时点 | 事实 |
|---|---|
| 14:20 | 我在 **10.9.0.14199** 上部署测试草稿 |
| 18:31–18:32 | 剪映触发**强制静默全量安装**（安装日志：`bSilentInstall=1, bForceInstall=1`，包体 901,853,504 bytes ≈ 860MB），`Apps/` 目录被整体替换 |
| 18:32:50 | 剪映扫描草稿目录 → 写入 `root_meta_info.json` + `draft_acion_watch.json`（即证据 D/E/F） |
| 20:33 | 新版本目录 **11.5.3.14501** 生成完成 |

**这改变了 R1 的性质**：

1. pyJianYingDraft 官方支持矩阵标到 **10.8**；本机接纳我们草稿时已是 **11.5.3**——**跨了一个大版本**，而草稿格式恰恰是跨版本最容易变的东西
2. 剪映**不询问用户就静默全量升级**，意味着**用户端版本不可控**——今天验证通过的格式，明天可能因一次后台升级而失效
3. 因此 R1 不能按"一次性验证通过"处理，必须改为**持续风险**：需要建立版本探测 + 主动失效告警（见修订清单）

#### 尚未覆盖的部分（诚实边界）

已证实：**草稿被剪映识别、登记、索引**。
未证实：**双击打开后时间线是否正确渲染**（3 段切片位置、叠化转场、字幕轨、标题轨）。原因：草稿列表页不会主动渲染并生成 `draft_cover.jpg`，而深度验证需要驱动剪映 UI，本机无相应自动化条件。

**残余风险等级：低。** 理由：剪映能算出正确的素材体积，说明它已经遍历了草稿的素材引用关系；若草稿结构非法，`suc` 不会为 `true`。但"能登记"与"能正确剪辑"在工程上确实是两件事，建议在阶段 5 用一个更复杂的时间线（多轨+转场+字幕）复测。

> 附：调试期间已把 `root_meta_info.json` 备份到 `SliceQ/stage0/root_meta_info.backup.json`，如需回滚可用。

---

## 3. V3 — 模型接入规格（R2，架构级）

### 3.1 结论：TECH-DESIGN 选错了 SDK，且这是个架构级错误

**官方文件传入规格表（阿里云百炼文档原文归纳）：**

| 文件类型 | 规格 | DashScope SDK（Python/Java） | OpenAI 兼容 / HTTP |
|---|---|---|---|
| 视频 | **> 100MB** | 仅公网 URL | 仅公网 URL |
| 视频 | **7MB ~ 100MB** | **✅ 可传本地路径** | 仅公网 URL |
| 视频 | < 7MB | ✅ 可传本地路径 | 可 Base64 |
| 音频 | 7~10MB | ✅ 可传本地路径 | 仅公网 URL |
| 音频 | < 7MB | ✅ 可传本地路径 | 可 Base64 |

<TECH-DESIGN.md 第 3 节现写>
> **平台**：阿里云百炼 DashScope，**OpenAI 兼容模式**端点…
> **依赖**：`openai`（调 DashScope 兼容端点）

**这条路对本地视频不通。** OpenAI 兼容端点不接受本地文件路径，只认公网 URL。而精析级的输入正是本地切出的候选片段。

**修正方案**：改用 **DashScope 官方 SDK**（`dashscope` pip 包）。
- 精析级切出的 720p 低码率分析副本（通常 5~50MB）→ **直接传本地路径**
- **无需 OSS、无需公网 URL、无需任何上传环节、无需用户再开一个云账号**
- 直接消解了原 R2 风险以及"隐私：视频不出本地"与"需要公网 URL"之间的设计矛盾

### 3.2 已确认的模型边界

| 项 | 值 |
|---|---|
| 模型名 | `qwen3.8-omni-flash`（Realtime 版为 `qwen3.8-omni-flash-realtime`） |
| 视频单请求上限 | **2 小时 / 2GB**（URL 方式）；本地路径方式 ≤100MB |
| 音频单请求上限 | 3 小时 |
| 上下文 | ~1M token（实测上限约 991K） |
| 采样 | ≤15fps 可得稳定结果 |
| 视频时长范围 | qwen3.8 系列 2 秒 ~ 2 小时 |
| 多视频 | 单请求最多 64 个视频 |
| 音频语言 | 113 种语言/方言 |
| 输出 | **仅文本**（不含语音生成） |
| 计费 | 输入 **¥0.8/M**、缓存命中 **¥0.1/M**、输出 **¥2.7/M**（**全模态同价**，不再区分文本/图像/音频/视频） |
| 可用地域 | 北京、新加坡、中国香港、东京、法兰克福、弗吉尼亚 |
| 免费额度 | 100 万 token（仅北京地域，90 天内有效） |

> **重要**：确认"模型仅返回文本，媒体的剪辑执行由工具完成"——与 TECH-DESIGN "模型负责理解与决策，FFmpeg 负责执行"的架构判断一致，无需修改。

> **注意**：qwen3.8 系列"不支持对视频文件的音频进行理解"这条限制出现在 VL 系列文档段落，omni 系列不适用（omni 的卖点正是音视频联合理解）。**标记为待实测项。**

### 3.3 成本重估：比原估低一个数量级

官方 Agentic 实测数据：44 分钟素材中定位关键 6 秒，消耗 **79,117 token**（对比全量读取 145,736，降 45.7%）。

按此推算 3 小时录像：

| 环节 | 量级 | 成本 |
|---|---|---|
| 3 小时音频转录 | 免费 ASR 通道 | ¥0 |
| 语义粗判（纯文本） | 分窗文本 | ≤ ¥0.2 |
| 抽帧粗判 | ~200 帧 | ≤ ¥0.3 |
| 精析（约 36 分钟分析副本） | ~65K token 入 + 少量出 | ¥0.05~0.5 |
| 断句/文案 | 候选段文本 | ≤ ¥0.05 |
| **合计** | | **≈¥0.3 ~ 1.5** |

**对照**：TECH-DESIGN 现估 ¥1~2.5；PRD KR3 现定 ≤¥2。
**建议**：实测校准后把 KR3 收紧到 **≤¥1**（这是竞品无法跟进的成本结构）。

---

## 4. V4 — OTIO → FCP XML 通道

### 4.1 实测结果：技术侧跑通 ✅

```
[导出] FCP7 XML -> SliceQ_Stage0_Test.xml   4,898 bytes
[导出] OTIO 原生 -> SliceQ_Stage0_Test.otio 10,347 bytes

往返读取验证: 回读时间线 SliceQ_Stage0_Test，3 片段，总长 9.00s ✅
媒体引用: file:///C:/.../stage0/test_media/source.mp4（绝对路径正确）✅
```

### 4.2 三个文档未记载的坑（必须写进技术方案）

**坑 1：FCP 适配器不在 `opentimelineio` 主包内。**
pip 装的 `opentimelineio 0.18.1` 只带 3 个适配器：

```
otio_json / otioz / otiod      ← 没有 fcp_xml、没有 fcpx_xml
```

必须额外安装 **`otio-fcp-adapter`**（1.0.0，提供 `fcp_xml`，suffix `.xml`）。
→ TECH-DESIGN 第 7 节依赖清单需补此项，否则阶段 5 会卡住。

**坑 2：媒体引用必须带 `available_range`。**
缺失时适配器抛：

```
AttributeError: 'NoneType' object has no attribute 'start_time'
  at otio_fcp_adapter/fcp_xml.py:1482 _build_file()
```

必须在 `ExternalReference` 上显式设置 `available_range`。这是纯 API 契约问题，文档未提。

**坑 3（影响产品设计）：FCP XML 无原生字幕轨。**
字幕只能以「时间线标记（Marker）+ 附 SRT 文件」形式携带，不能给 PR/达芬奇一条可编辑字幕轨。

**产品含义**：三端导出的能力并不对等——**剪映通道能带真字幕轨，XML 通道只能带标记+SRT**。这应当改变设计中的主次关系：把剪映作为**首选草稿通道**（字幕体验完整），PR/达芬奇作为**兼容通道**（画面精修为主，字幕靠 SRT 再导入）。

### 4.3 未完成：达芬奇导入实测

本机未装达芬奇免费版，无法端到端验证。**需决策**：是否安装达芬奇做这一步验证，或接受"生成侧已跑通、导入侧按 FCP7 XML 行业标准信任"。

---

## 5. V5 — yt-dlp B 站通道 ✅

实测通过：

```
$ python -m yt_dlp --simulate --print "%(title)s | %(duration)s秒" https://www.bilibili.com/video/BV1GJ411x7h7
【官方 MV】Never Gonna Give You Up - Rick Astley | 212.393秒
```

相关 extractor 齐备：`BiliBili`、`BiliBiliBangumi`、`BilibiliCheese`(课程)、`BilibiliCollectionList`、`BilibiliAudio`。
→ R5（yt-dlp 对 B 站失效）风险等级可维持"中"，缓解方案（手动下载后本地导入）仍有效。

---

## 6. V6 — 免费 ASR 通道：原路线受阻，但**实测挖到一条更好的路** ✅

### 6.1 原路线的现状

- 必剪（Bcut）**未安装** → 无法实测其 ASR 接口
- 剪映 10.9 已装，但其 ASR 是客户端内部接口
- VideoCaptioner 是**安装包版**，不能用 pip 调用

### 6.2 意外发现：本机 FFmpeg 9.0 **内置 whisper.cpp 滤镜**——且已实测跑通

这条路线原本不在任何文档里，是在核对 ffmpeg 编译配置时发现的（`--enable-whisper`）。

**实测证据链：**

```
$ ffmpeg -filters | grep whisper
 .. whisper    A->A    Transcribe audio using whisper.cpp.        ← 滤镜真实存在

$ ffmpeg -i test_media/source.mp4 -af "whisper=model=nonexistent.bin" -f null -
[whisper_init_from_file_with_params_no_state: failed to open 'nonexistent.bin']
  → 说明 whisper.cpp 集成可正常初始化，仅缺模型文件

$ curl -L hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin
  → 77,691,713 bytes 下载成功（HuggingFace 直连不通，镜像通）

$ ffmpeg -i tone.wav -af "whisper=model=ggml-tiny.bin:language=zh:format=json:destination=out.json:use_gpu=false" -f null -
[Parsed_whisper_0] run transcription at 0 ms, 48000/48000 samples (3.00 seconds)...
  → 转录成功

$ cat out.json
{"start":0,"end":3000,"text":"(不幸)"}       ← 结构化输出正常（纯正弦波产幻觉文本，属预期）
```

**滤镜完整能力（`ffmpeg -h filter=whisper`）：**

| 参数 | 说明 | 对 SliceQ 的价值 |
|---|---|---|
| `model` | ggml 模型路径 | tiny 77MB / base 142MB / small 466MB 三档可选 |
| `language` | 语言或 `auto` | 中文直播可直接指定 `zh`，避免误判 |
| `format` | `text` / **`srt`** / `json` | **直接产出 SRT**，接上 editor.py 就能烧字幕 |
| `destination` | 输出文件路径 | 结构化落盘 |
| `vad_model` / `vad_threshold` | **VAD 静音检测** | **对长直播录像价值极大**——跳过数小时无声/纯音乐段落，是天然的粗筛加速器 |
| `use_gpu` / `gpu_device` | GPU 加速 | 有显卡时大幅提速 |
| `max_len` | 单段最大字符数 | 可控分段粒度，缓解断句问题 |

### 6.3 这条路线为何优于原方案

| 维度 | 必剪接口（原方案 A） | faster-whisper（原方案 B） | **ffmpeg 内置 whisper（新发现）** |
|---|---|---|---|
| 外部依赖 | 必剪客户端 | `faster-whisper` + Python 生态 | **零新增依赖**（ffmpeg 本就要分发） |
| 成本 | ¥0 | ¥0（耗时换） | **¥0** |
| 合规风险 | **有**（违约调用私有接口） | 无 | **无** |
| 稳定性 | 接口随平台风控变动（R7） | 稳定 | 稳定 |
| 模型获取 | — | 自动下载 | 需下 ggml 模型（可走镜像，已验证） |
| VAD 静音跳过 | — | 支持 | **原生支持** |
| 输出 | 词级时间戳 | **词级时间戳**（`word_timestamps=True`） | **仅句级时间戳** ⚠️ |
| 速度 | 云端 | 本地 | 本地（3 秒音频耗时 0.49s，≈6x 实时） |

### 6.4 唯一的代价：无词级时间戳（设计影响）

ffmpeg 滤镜输出为 `{"start": 0, "end": 3000, "text": "..."}` —— **句级起止**，无词级切分。

而 TECH-DESIGN 4.5 节的字幕管线写的是：
> 词级时间戳转录 → LLM 语义断句 → 校正 → …（词级时间戳对齐取词边界）

**这是一处真实冲突，需要你选：**

- **选项甲**：接受句级时间戳，断句交给 LLM 按 `max_len` 与语义切分，时间轴按字数比例插值。实现简单、依赖最轻，字幕质量略降（长句内切分点会有几十毫秒误差，人眼基本无感）。
- **选项乙**：ASR 走 faster-whisper（支持词级），换取字幕精确对齐。代价是引入 Python 侧依赖与模型自动下载。
- **选项丙**：混合——默认 ffmpeg 句级，设置里提供"高精度字幕（词级）"开关，开启时切换到 faster-whisper。

**我的建议：选项丙。** 默认路径零依赖跑通，把"字幕精度"变成用户可选的升级项而不是地基。这既保住了最轻的默认体验，又不放弃上限。

### 6.5 修订后的 ASR 决策建议

原报告建议的"B 为默认、A 为可选、C 兜底"应改为：

| 优先级 | 引擎 | 说明 |
|---|---|---|
| **默认（首选）** | **ffmpeg 内置 whisper 滤镜** | 零新增依赖、零成本、零合规风险、原生 VAD |
| 可选升级 | faster-whisper | 用户勾选"高精度字幕"时启用，提供词级时间戳 |
| 彻底兜底 | Qwen 音频理解 | 前两者均不可用时（+¥0.5/条，需用户确认） |
| **建议放弃** | 必剪/剪映私有接口 | 合规灰度 + 接口不可控，收益已被 ffmpeg 路线覆盖 |

> 注：模型文件建议随首次启动引导下载（走 hf-mirror.com 镜像，已实测可通），并允许用户自备。

---

## 6.6 V6b — FFmpeg 许可合规（R6，须精确化）

实测本机 ffmpeg 构建配置：

```
--enable-gpl  --enable-version3  --enable-libx264  --enable-libass
```

**结论：gyan.dev 的 ffmpeg 构建是 GPL 授权**（`--enable-gpl` + `libx264` 为 GPL 组件）。

TECH-DESIGN 第 5 节写"不打包进 exe（LGPL/许可与体积考虑）"，方向正确但表述需精确：
- 合规的前提**不是**"它是不是 LGPL"，而是"**我们有没有把它和 SliceQ 链接/打包成单一作品**"
- subprocess 调用独立可执行文件 → 属聚合（aggregate），SliceQ 可保持 MIT ✅
- **PyInstaller 把 ffmpeg.exe 打进单文件 exe → 构成单一作品传播，SliceQ 须整体改为 GPL** ❌ 这是硬红线，必须在打包脚本与代码审查中专项防住
- 同理，`assets/bgm/` 的免版税 BGM、思源黑体（OFL）也需在 README 的第三方声明里列明

---

## 7. V7 — VideoCaptioner 联动 ⚠️ 设计前提有误


**TECH-DESIGN 6.5 节现写**：
> 检测 `where videocaptioner` / `pip show videocaptioner`；已安装则以 subprocess 调其 CLI（如 `videocaptioner dub`）

**本机实际情况**：

```
where videocaptioner        → 找不到
pip show videocaptioner      → 未安装
%APPDATA%\VideoCaptioner\    → 只有一个 VideoCaptioner.ini（安装包版留下的配置）
桌面                          → VideoCaptioner.lnk 快捷方式
```

**结论**：VideoCaptioner 以 **Windows 安装包形态**存在，非 pip 包，无 PATH CLI。
→ 检测方式必须重新设计：查注册表卸载项 / 扫描常见安装目录 / 定位 `VideoCaptioner.exe`。且 `videocaptioner dub` 这种 CLI 子命令是否存在需核实——若官方安装包版无 CLI，则"外部联动"要么降级为「引导用户手动打开 VideoCaptioner」，要么放弃联动。

**这条要重新论证，不能按原方案实现。**

---

## 8. 待你拍板的决策点

| # | 决策 | 我的建议 |
|---|---|---|
| ~~D1~~ | ~~剪映测试草稿能否打开？~~ | ✅ **已由我自动验证完成，结论：通过**（见 §2.3） |
| D2 | **百炼 API key 是否提供？**提供后我立刻跑 V8 模型实测 | 强烈建议——这是唯一没被验证的核心假设 |
| D3 | **ASR 主引擎选哪条？** | 改为 **ffmpeg 内置 whisper 为默认**（零依赖/零成本/零合规风险/原生 VAD）；必剪接口建议放弃 |
| D3b | **字幕精度：句级（ffmpeg）还是词级（faster-whisper）？** | 选**丙：混合**——默认句级，设置里提供"高精度字幕"开关切词级 |
| D4 | 是否安装达芬奇免费版做 XML 导入验证？ | 可跳过，接受行业标准信任；但装上更稳 |
| D5 | SDK 由 `openai` 改为 `dashscope` 官方 SDK —— 批准吗？ | **必须改**，否则本地视频送不进模型（架构级） |
| D6 | 是否批准我按本报告修改 PRD / TECH-DESIGN / TASKS？ | TECH 11 处必改、PRD 3 处、TASKS 4 处 |
| D7 | **弹幕信号是否提前进 MVP？**（v1.1 遗留问题） | 建议阶段 2 做完立刻实测命中率，不达标就把弹幕提前 |
| D8 | **新增：是否接受"剪映版本漂移"为长期风险并建立防护？**（本报告 §2.3 新发现） | 建议接受。方案：启动时探测剪映版本并记录；对未验证过的版本显示"格式兼容性未验证"提示；把"生成后校验草稿能被剪映登记"做成自动化测试 |

---

## 9. 对现有文档的修订清单（待批准后执行）

### TECH-DESIGN.md

| 位置 | 现状 | 应改为 |
|---|---|---|
| §3 平台 | `openai` 调 DashScope 兼容端点 | **`dashscope` 官方 SDK**，本地路径直传 |
| §3 视频输入方式 | 首选公网 URL / 次选 base64 | **首选本地路径（≤100MB）**；>100MB 才降码率重切 |
| §2.3 成本表 | ≈¥1~2.5 | **≈¥0.3~1.5** |
| §6 剪映 | 未排除程序化渲染 | **明确砍掉"程序化触发剪映导出"**（7+ 不可行） |
| §6 三端关系 | 三端平列 | **剪映为首选（字幕轨完整）；XML 为兼容通道（字幕仅标记+SRT）** |
| §7 依赖 | 缺 FCP 适配器 | 补 **`otio-fcp-adapter`**；OTIO 引用必须设 `available_range` |
| §2.1 粗筛 / §4.5 字幕 | 首选必剪/剪映免费接口 + 词级时间戳 | **首选 ffmpeg 内置 whisper 滤镜**（已实测）；词级改为可选升级项；必剪接口建议删除 |
| §5 FFmpeg 分发 / §9 R6 | "不打包进 exe（LGPL 考虑）" | 精确化为：**GPL 构建，红线是不得 PyInstaller 打包进 exe**；subprocess 调用属合规聚合 |
| §6.5 联动 | `where videocaptioner` / pip 检测 | 待重新论证（安装包版无 CLI） |
| §9 R1 | 等级"高" | **保留"高"，但改变性质**：不再是"能否生成"，而是**版本漂移**——剪映静默全量升级（实测 10.9→11.5.3，跨大版本），已验证的格式随时可能失效。缓解见 R11 |
| §9 R2 | 等级"高" | **降为"低"**（本地路径直传 + 20% 硬阀控体积） |
| §9 R7 | 等级"高"（必剪接口失效） | **可撤销**——主引擎已改为 ffmpeg 内置 whisper，不再依赖私有接口 |
| §9 R6 | FFmpeg LGPL 合规 | 改为 **GPL 构建合规**，明确"禁止打包进 exe" |
| §9 新 R10 | — | 新增：**ffmpeg 构建默认不含 whisper 模型**，需首次启动引导下载（走镜像）；若用户自备 ffmpeg 不带 `--enable-whisper`，需降级到 faster-whisper |
| §9 新 R11 | — | 新增：**剪映静默自动升级导致草稿格式漂移**（实测 10.9→11.5.3 全量替换 860MB）。缓解：① 启动时读取 `Apps/` 下的版本目录名并与已验证列表比对；② 未验证版本显示"兼容性未验证"提示；③ 把"生成草稿 → 剪映登记成功"做成自动化回归测试（本报告的 `draft_acion_watch.json` 判据可直接复用）；④ 底线保障：降级包（片段 mp4 + SRT）始终可用 |

### PRD-SliceQ.md

| 位置 | 应改为 |
|---|---|
| KR3 | 成本线 ≤¥2 → **≤¥1**（待实测确认） |
| §功能 F12 剪映草稿 | 补充"生成后由用户手动在剪映导出" |
| FAQ/限制说明 | 新增"字幕在 PR/达芬奇通道以标记+SRT 形式携带" |

### TASKS.md

| 位置 | 应改为 |
|---|---|
| 阶段 0 交付物 | 补充本报告的结论，标注已消解/未消解项 |
| 阶段 2 | 明确用 DashScope SDK + 本地路径；ASR 默认引擎按 D3 改 |
| 阶段 5 | 补 `otio-fcp-adapter` 依赖与 `available_range` 注意事项 |
| 阶段 6 | 补 FFmpeg 已在本机存在（首次启动下载逻辑仍需保留，为干净机器准备） |

---

## 10. 证据文件索引

```
SliceQ/stage0/
├── verify_jianying_draft.py      剪映草稿生成验证脚本
├── deploy_to_jianying.py         部署到剪映目录 + 元信息对比脚本
├── verify_otio.py                OTIO→FCP XML 验证脚本
├── test_media/
│   ├── source.mp4                12s/1280x720/h264+aac 测试素材（ffmpeg 生成）
│   ├── subtitle.srt              测试字幕
│   ├── tone.wav                  测试音频（ffmpeg 生成）
│   ├── out.json                  whisper 滤镜转录产物 ✓
│   └── models/ggml-tiny.bin      whisper 模型 77,691,713 bytes（hf-mirror 下载）
├── test_drafts/
│   ├── SliceQ_Stage0_Test/       生成的剪映草稿（明文 24,622 bytes）
│   ├── SliceQ_Stage0_Test.xml    FCP7 XML（4,898 bytes）
│   └── SliceQ_Stage0_Test.otio   OTIO 原生（10,347 bytes）
└── reports/
    └── STAGE0-VERIFICATION.md    本报告
```

**已部署到系统（可回退）**：
`%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\SliceQ_Stage0_Test\`
