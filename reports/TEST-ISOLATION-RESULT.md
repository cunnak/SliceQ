# 自测数据隔离与三个真缺陷 —— 验收报告

> 日期：2026-09-27｜范围：`sliceq/_testing.py`（新增）、24 个自测的隔离接入、
> `stage0/probe_resolve/check_isolation.py`（新增）、三个产品缺陷修复
> 关联文档：TECH-DESIGN §10（隔离约定）、§2.6（粗筛报错分层）

---

## 0. 这一轮的起因：一次真实的用户数据损失

上一轮跑回归时触发了 `selftest_stage1.py`，它直接操作生产路径：

| 代码 | 后果 |
|---|---|
| `secrets.set_api_key(fake)` | **覆盖了用户的真 API key** |
| `secrets.clear_all()` | **删掉了凭据文件** —— key 就此丢失 |
| `settings.set("whisper_model", "ggml-base.bin")` | 把转录模型从 large-v3-turbo **降级成 base**，而**用户不会知道转录为什么变差** |

`selftest_first_launch.py` 同类（清掉用户手动指定的 ffmpeg 路径）。

**性质**：这两个测试当时**全部通过、没有任何报错**。
用户是等到几天后另一个依赖 key 的测试报 `AuthError` 才发现的。

> 这决定了本轮的判据：不是"测试有没有报错"，而是"**有没有动过用户的文件**"。

### 用户数据的恢复情况

| 项 | 状态 |
|---|---|
| `whisper_model` | ✅ 已恢复为 `ggml-large-v3-turbo.bin` |
| API key | ⚠️ **无法自动恢复**（DPAPI 加密无明文）→ 用户重新提供后已写入并验证 |
| 用户任务列表 | ✅ 清理了 1 条测试残留（`阶段2自测`），列表现在为空 |
| `work/` 残留 | ✅ 清理 5 个孤儿目录，释放 **175 MB** |

新 key 写入后**当场做了真实调用验证**（不靠"写进去了"当结论）：

```
翻译 2 条 → ['Hello, world.', 'This is a test.']   费用 ¥0.000362
```

---

## 1. 根治：`sliceq/_testing.py`

### 1.1 设计：重定向，而不是"测完恢复"

"测完恢复"要写对每一处、中途不能崩、恢复本身也可能失败 ——
而一旦出问题，污染是**静默**的。

重定向是从根上让"写生产数据"**不可能发生**：所有落盘位置指向临时目录，
`atexit` 整个删掉，测试写得多脏都不碰用户分毫。

### 1.2 ★ 哪些隔离、哪些**故意不隔离**

**判据：写下去会不会损坏用户的既有数据。**

| 路径 | 处理 | 理由 |
|---|---|---|
| `DB_PATH` / `SECRET_PATH` / `SETTINGS_PATH` | 隔离（凭据与设置**复制**副本）| 写一次就是真损失 |
| `STYLES_DIR` | 隔离 | 用户手改的字幕预设 —— **阶段 3 的注入测试往这里留过两个垃圾预设（`损坏测试`/`类型错误`），它们直接出现在用户的下拉框里** |
| `EXPORT_DIR` / `LOGS_DIR` / `BGM_DIR` / `work/` | 隔离 | 测试产物不该混进用户目录 |
| `MODELS_DIR` / `BIN_DIR` | **❌ 不隔离** | 是**下载缓存**（whisper 1549MB + ffmpeg 245MB）。隔离会让依赖模型的测试被迫重新下载 1.8GB；而"补下载"对用户是**有益**的，不是破坏 |

> 一句话：**区别在于"改了就是损坏"还是"没有就补上"。**

### 1.3 ★ v2 修正：settings.json 从「不复制」改为「复制」

第一版**不**复制设置，理由是"怕用户配置影响测试断言"。**这个判断是错的**，
而且当场付出了代价：

```
隔离后测试读到的是**出厂默认**端点 .../api/v1（原生 SDK 通道），
而用户在设置里配的是 .../compatible-mode/v1（HTTP 通道）。
两个端点域名相同、**通道不同**：前者需要 `dashscope` 包，而它没装
⇒ 粗筛一个窗口都没能成功 ⇒ 端到端测试报告「0 条切片段」
```

也就是说：**测试跑的根本不是用户真实走的路径**，这个端到端测试失去了意义。

"怕影响断言"这个担心用错了地方 —— 要测出厂默认值，测试应该**自己显式清空**
（`with_settings=False` 或 `settings.reset()`），而不是让所有测试都读不到用户配置。

### 1.4 把"忘了隔离"变成硬失败

```python
from sliceq._testing import assert_isolated, isolate_data_root
isolate_data_root()
assert_isolated()      # 未隔离 → 抛异常，而不是静默写用户数据
```

**覆盖**：全部 **24 个自测**（含 `sliceq/selftest_*.py`）已接入。

---

## 2. 三个真缺陷

### 2.1 🔴 默认端点导致"装完就用不起来"

```python
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"   # 原生 API 通道
```

而 `requirements.txt` **不含** `dashscope`。
⇒ **全新用户填了 key、点了分析 → 粗筛全部失败。**

实测（模拟全新用户，`with_settings=False`）：

| | 修复前 | 修复后 |
|---|---|---|
| 默认端点 | `/api/v1` | `/compatible-mode/v1` |
| 选中通道 | `DashScopeTransport`（需要未安装的包）| `HTTPTransport` |
| 真实调用 | ❌ `缺少 dashscope 包` | ✅ 成功 |

### 2.2 🔴 报错把病因替换掉了（最严重的一个）

粗筛全部窗口失败时，用户看到的是：

```
没有找到符合条件的候选段。可以试试：① 把需求描述写得更具体；
② 调高「筛选强度」档位；③ 换一段素材验证。
```

**真实原因是「缺少 dashscope 包」** —— 这三条建议全在引导用户改**无关的东西**。

它不只是"没帮助"，而是**主动误导**：用户会去改需求描述、换素材，白折腾一圈，
而且**不会意识到自己被骗了**。`degraded` 列表里其实有真原因，但被这句友好话术盖住了。

**修法**：`ScreeningResult` 加 `windows_failed` 计数，分三种成因报错：

| 成因 | 报法 | 用户该做什么 |
|---|---|---|
| 全部窗口失败 | 抛 `PipelineError` + 原始原因 + "**这不是素材的问题**" | 修配置 |
| 部分窗口失败 | 继续跑 + 提示"漏掉的内容可能在其中" | 先重跑 |
| 真的没有 | 原话术（调需求 / 换素材）| 调需求 |

**判据也升级了**：`selftest_fault_injection.py` 的 `scenario()` 新增
`expect_in` / `expect_not_in` —— 断言**该说的说到了、不该说的没说**。

> 仅"抛了中文自定义错误"远远不够：**通顺友好的话也可能是错的**。

### 2.3 `selftest_style_page.py` 的潜伏 bug 把测试挂死

```python
def main():
    # 没有 global PASS, FAIL
    FAIL += 1                       # → FAIL 成了 main 的局部变量
    def _finalize():
        print(f"...失败 {FAIL} 项")  # ← free variable，尚未绑定 → NameError
```

异常把 `app.quit()` 拦掉 ⇒ **事件循环永不退出** ⇒ 测试挂死，
**而且没有任何错误输出**（实测等了 25 分钟才被外部杀掉）。

修：main 声明 `global PASS, FAIL`；`on_timeout` 加
`try/except/finally` —— 异常**自己打出来** + 无论如何都 `quit`。

### 2.4 🔴 内容审核被误报成"配置问题"（本轮修复时自己踩出来）

修完 2.2 之后，`selftest_real_e2e.py` 立刻用真实数据把**我这版新报错**打脸了：

```
粗筛的 1 个窗口全部调用失败，没有任何一个分析成功。
原始原因：HTTP 400 InternalError.Algo.DataInspectionFailed:
          Output data may contain inappropriate content.
这不是素材的问题 —— 请按上面的原因排查（多半是 API Key、端点地址或网络）。
                                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                 三条全错
```

真实原因是**服务端内容审核**拦下了模型的输出 —— 而它：

- 既不是 Key 问题、也不是端点或网络问题；
- 而且**恰恰和素材有关**（口语直播里的激烈用词被模型复述后判违规）。

我在修 2.2 时，为了"帮用户缩小范围"加了一句猜测 —— **这就是把误导又写了一遍**。
真实原因明明就在同一段文字的上方。

**两处修正**：

| 项 | 修复 |
|---|---|
| `pipeline.py` | 删掉猜测，改为"请以上面的「原始原因」为准排查" |
| `analyzer._explain_http_error` | 新增 `DataInspectionFailed` 识别，给出**准确**措辞：说明被拦的是「模型的输出」还是「送入的内容」、**明说"改设置不会有帮助"**、并给出真正可行的两条路（换素材 / 把提示词写中性） |

**并加了 5 项测试守住它**（`selftest_stage2.py`），其中一项的写法本身又踩了个坑：

```python
# ❌ 第一版：断言"不出现 Key / 端点 / 网络 这些词"
#    被自己的文案打脸 —— 正解里有一句"这**不是**配置、Key 或网络的问题"，
#    子串匹配把它判成了"提到了 Key/网络"。
#    **否定式断言不能用子串匹配**（"不是 A" 里就含 A）。
check(..., not any(k in txt for k in ("API Key", "端点地址", "网络")))

# ✅ 改成断言"不出现**猜测性的建议措辞**" —— 这才是要守的东西
check(..., not any(k in txt for k in ("多半是", "大概是")))
```

> **泛化教训**：报错里**不要塞猜测**。猜测一旦写进产品就把误导固化，
> 而用户不会知道哪句是事实、哪句是作者的推断。
> 让"原始原因"自己说话，最多补一句"这不是配置问题"这类**有把握**的判断。

### 2.5 内容审核是**偶发**的（实测）

同一素材、同一请求，两次跑的结果不同：

| 次 | 结果 |
|---|---|
| 第 1 次 | `Output data may contain inappropriate content` → 粗筛全失败 |
| 第 2 次 | 正常：粗筛 1 个候选（置信 90）、精析 1 条切片（`爆梗` 75 分）、花费 ¥0.0070 |

即：**是否触发审核取决于模型这一轮输出的措辞**，不是稳定的。
这意味着口语素材上"偶尔失败一次"是可预期的 —— 报错说得准，
用户才知道该重试而不是去改配置。

---

## 3. 附带收获：隔离照出"隐式依赖外部状态"的测试

隔离到**空环境**后，一批测试立刻失败 —— **那不是隔离的 bug，是它照出的旧问题**：

| 测试 | 现象 | 说明 |
|---|---|---|
| `selftest_stage2.py` | `no such table: task` | **从未调用 `store.init_db()`** —— 一直靠"生产库里已经有表"才跑得起来 |
| `selftest_real_e2e.py` | 同上 | 同因（修复后在真实素材上跑通：粗筛 1 候选 → 精析 1 切片，¥0.0070）|

这类测试意味着更糟的事：**在一个全新的环境（CI、新同事的机器）里跑它必然失败**，
只是之前没人发现。两处都已补 `config.ensure_dirs() + store.init_db()`。

---

## 4. 验收：不靠读代码，靠比指纹

### 4.1 为什么不能靠代码审查

排查"哪个测试会写用户数据"时，我 grep 的是
**"测试文件里的 `settings.set`"** —— 因此**整轮都漏了 `selftest_bridge.py`**：

```
selftest_bridge.py 自身一处 settings.set 都没有，
但它调用 bridge.set_manual_path()，
而那个产品函数内部会 settings.set("videocaptioner_path", ...)
→ 直接改写了用户的 settings.json
```

最后是靠 **mtime 变了** 才逮到：

```
md5 相同（内容一模一样），但 mtime 变了
→ 文件被重写过，只是碰巧写了同值
```

**这仍然算问题** —— 今天写的是同值，明天就可能是别的值。
只比 md5 会漏掉写入者，从而漏掉需要修的测试。

### 4.2 扫描工具与结果

`stage0/probe_resolve/check_isolation.py`：逐个跑测试，
跑前跑后比生产数据指纹（md5 + 大小 + **mtime** + 各表行数 + 目录清单 + `work/`）。

**全量扫描（24 个套件）结果**：

```
✅ 全部测试都没有改动用户数据 —— 隔离完备
✅ 扫描结束后基线完全未变
```

（扫描中出现的两条"变化"均为**我手工清理 work 残留时与扫描重叠**造成的假阳性，
已复跑确认 —— 复跑时 `work/` 为空，全部测试保持"干净"。）

### 4.3 回归

**714 项全过**（21 个套件）：

| 套件 | 项数 | 套件 | 项数 |
|---|---|---|---|
| `selftest_translate` | 78 | `selftest_fault_injection` | 27 |
| `selftest_stage2` | 80 | `selftest_hallucination` | 26 |
| `selftest_enhance` | 70 | `selftest_editor` | 23 |
| `selftest_queue` | 44 | `selftest_queue_e2e` | 20 |
| `selftest_export_enhance_gui` | 39 | `selftest_export_chain` | 19 |
| `selftest_stage1` | 38 | `selftest_pipeline_status` | 16 |
| `selftest_concurrency` | 37 | `selftest_store_integrity` | 10 |
| `selftest_fault_quality` | 35 | `selftest_bilingual_e2e` | 17 |
| `selftest_remap_timeline` | 33 | `selftest_asr_e2e` | 12 |
| `selftest_queue_gui` | 30 | `selftest_style_page` | 30 |
| `selftest_bridge` | 30 | | |

### 4.4 最干净的一条证据：隔离前后逐字节对比

"全部隔离机制正确"的最终验证 —— 挑 4 个**曾经写用户数据**的测试
（含元凶 `selftest_bridge`、以及会 `clear_all()` 的 `selftest_stage1`）
跑一遍，跑前跑后比指纹：

```
跑之前                                   跑之后
settings.json   6374202c91/426B/…7631    6374202c91/426B/…7631   ← 含 mtime 也一致
credentials.bin 5f71c563cb/358B/…5254    5f71c563cb/358B/…5254
db/sliceq.db    21521143a8/114688B/…6681 21521143a8/114688B/…6681
styles/         []                        []

★★★ 完全一致（含 mtime）
```

> 注意 `settings.json` 的 **mtime 也没变** —— 那正是当初逮到 `bridge`
> 的证据（md5 相同、mtime 变了）。现在它纹丝不动。

### 4.5 最终指纹对比（19 个套件跑完）

```
settings.json    md5 一致 · mtime 一致
credentials.bin  md5 一致 · mtime 一致
db/sliceq.db     md5 一致 · 全部表行数一致（均为 0）
styles/          空 → 空
work/            空 → 空
★ 用户数据一字节未变
```

---

## 5. 遗留问题（如实记录）

### 5.1 `selftest_style_page.py` 退出阶段偶发段错误（未修）

单独跑 3 次：`rc=139`（段错误）、`rc=139`、`rc=0`。
观察：**段错误发生在测试结论打印之后**（有一次输出是"通过 30，失败 0"）。

- 性质：**退出阶段的偶发段错误**，不影响测试结论，但会让进程返回码非 0，
  对 CI 不友好；
- 该测试已经用 `os._exit()` 跳过解释器清理来规避挂死，段错误仍在
  `os._exit()` 之前发生；
- 疑似与 Qt 预览渲染的 native 资源在退出时释放有关。
- **未深挖**：它是测试基础设施问题，不是产品缺陷；且偶发、需要 Qt 内部排查。

### 5.2 两个产品缺陷（已立项，本轮未修）

| # | 缺陷 | 性质 |
|---|---|---|
| 31 | **删任务不清 `work/` 目录** | **隐私**：`chunk_*.jsonl` 里是同一份转录文本。用户在 UI 里删任务 = "这些素材我不要了"，但素材里说过的话仍在磁盘上。与 v5 那次（`transcript_seg` 漏外键）**同一类**。实测残留 175 MB |
| 32 | **`work/` 按 `video.stem` 命名** | 同名不同路径的视频**共用工作目录** ⇒ `screen.jsonl`（粗筛缓存）会被复用 ⇒ B 任务读到 A 任务的候选时间点，**位置整体错乱且不报错**。与 R29「缓存无内容指纹」同处 |

### 5.3 ★ 扫描工具的覆盖边界（探针脚本没被覆盖）

`check_isolation.py` 扫的是 **`selftest_*.py`**。
**`stage0/probe_resolve/probe_*.py`（7 个）不在自动扫描范围内**：

- 它们大多需要命令行参数（素材路径、时间段），不带参数跑会立刻退出 ——
  **测不到它真实的写入行为**；
- 我 grep 过它们对生产路径常量的引用，结果是**空的**
  （它们不引用 `APP_ROOT` / `STYLES_DIR` / `DB_PATH` 等）；
- **但这不构成"已覆盖"** —— 本轮正是靠 grep 排查的，
  而 `selftest_bridge` 恰恰就是 grep 查不出来的那一个
  （它调产品函数，产品函数内部写 settings）。

⇒ **如实记为未覆盖**。要补的话，需要带参数逐个跑一遍并比指纹。

### 5.4 隔离策略的已知局限

- `MODELS_DIR` / `BIN_DIR` 仍指向生产路径（测试会读它们）。这是**刻意的**，
  但意味着"测试只读、绝不写这两个目录"这个前提**没有被断言守住**。
- 若将来用户改走中转站端点，隔离复制的 settings 会如实带过去（这是对的），
  但**测试结果就依赖用户当前的端点配置**。要测出厂默认路径，
  需显式用 `with_settings=False`。

---

## 6. 本轮新增/改动的文件

| 文件 | 变化 |
|---|---|
| `sliceq/_testing.py` | **新增** —— 隔离实现 + 自检 |
| `stage0/probe_resolve/check_isolation.py` | **新增** —— 隔离完备性扫描 |
| `sliceq/config.py` | 默认端点改为兼容模式（附理由注释）|
| `sliceq/screening.py` | `ScreeningResult.windows_failed` 计数 |
| `sliceq/pipeline.py` | 粗筛失败三分支报错 |
| `selftest_fault_injection.py` | `scenario()` 支持 `expect_in`/`expect_not_in`；新增 F1 场景 |
| `selftest_style_page.py` | 修 `global` 声明 + 回调异常兜底 |
| `selftest_stage2.py` | 补 `init_db()`；新增 5 项内容审核报错测试 |
| `selftest_real_e2e.py` | 补 `init_db()` + `config` 导入 |
| `sliceq/analyzer.py` | 新增内容审核（`DataInspectionFailed`）识别 |
| 其余 22 个自测 | 接入隔离 |
| `docs/TECH-DESIGN.md` | v1.20：§10 隔离约定、§2.6 报错分层 |
| `docs/TASKS.md` | v1.18 |
