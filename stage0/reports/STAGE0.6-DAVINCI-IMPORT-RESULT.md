# SliceQ 阶段 0.6 — 达芬奇导入格式实测结果

**结论：达芬奇原生支持导入 OTIO（``.otio`）；FCP7 XML（`xmeml v4`）实测失败。导出策略改为 OTIO 直出。**

- 验证日期：2026-09-24
- 验证目标：确认「放弃脚本直连后」达芬奇能用什么格式接收 SliceQ 的时间线
- 环境：DaVinci Resolve **21.0.0.48**（`C:\davinci`）
- 最终判定：**达芬奇通道 = OTIO 直出**；FCP7 XML 降级为兼容选项

---

## 一、实测对照结果

| 格式 | 文件 | 导入方式 | 结果 |
|------|------|----------|------|
| **OTIO** | `SliceQ_Stage0_Test.otio`（10,347 B） | 媒体池右键 → Import → Timeline | ✅ **成功** |
| **FCP7 XML** | `SliceQ_Stage0_Test.davinci.xml`（5,167 B） | 同上 | ❌ **无法添加** |

**用户操作路径（两次一致）**：媒体页面 → 媒体池右键 → `Import` → `Timeline...`

> 排除操作失误：日志显示两次操作都进入了媒体页面（`Main view page is changed to 2`），用户确认操作位置正确。

---

## 二、OTIO 成功的证据链

### 2.1 达芬奇日志（权威证据）

来源：`%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\logs\davinci_resolve.log`，第 1843–1864 行

```
22:50:28 | SmLogger           | INFO | Import Log (Info) - ???????????? C:/Users/<user>/WorkBuddy/2026-09-23-13-06-47/SliceQ/stage0/test_drafts/SliceQ_Stage0_Test.otio
22:50:37 | SmLogger           | INFO | Import Log (Fallback) - ?????? V1 ??????????????? 00:00 ... source.mp4 ... 1
22:50:37 | SmLogger           | INFO | Import Log (Fallback) - ?????? V1 ??????????????? 03:00 ... source.mp4 ... 2
22:50:37 | SmLogger           | INFO | Import Log (Fallback) - ?????? V1 ??????????????? 06:00 ... source.mp4 ... 3
22:50:37 | SyManager.Signals  | INFO | Current timeline (SliceQ_Stage0_Test) is changed
22:50:41 | SyManager.Signals  | INFO | Current timeline (SliceQ_Stage0_Test) is changed
```

> 中文部分被达芬奇的日志系统替换为 `?`（写入时即丢失编码），**无法还原**。但结构性信息完整可读。

**三条独立证据**：

| # | 证据 | 说明 |
|---|------|------|
| 1 | `Import Log (Info) - .../SliceQ_Stage0_Test.otio` | 文件**被读取** |
| 2 | 三行 `Import Log (Fallback)`，时间点 `00:00` / `03:00` / `06:00`，编号 `1`/`2`/`3` | **三条片段**被正确识别，各 3 秒 |
| 3 | `Current timeline (SliceQ_Stage0_Test) is changed` | **时间线创建成功**，名称正确 |

### 2.2 ⚠️ 关键发现：走的是 `Fallback` 路径

日志标记为 **`Import Log (Fallback)`**，而非 `Import Log (Info)`。

**含义**：达芬奇对 OTIO 采用的是**回退转换路径**，而不是一等公民的原生解析。三条片段能被读出，说明基础结构被理解；但**回退路径通常意味着元数据可能被丢弃**。

**必须验证的隐患**：OTIO 中的 3 个 `Marker`（CYAN 青色，内容为高光说明）**是否随导入保留**。见 §四 待验项 V-A。

### 2.3 FCP7 XML 失败的证据

| # | 证据 | 说明 |
|---|------|------|
| 1 | 日志**无任何 XML 读取记录** | 文件从未进入解析流程 |
| 2 | `Project.db` 时间戳停在 `09-24 10:30` | 导入尝试期间**零数据库写入** |
| 3 | 媒体池 `MpVideoClip` 计数 = **0** | 确认未导入任何片段 |

**失败层次判定**：日志无解析错误、无异常、无记录 → 说明**达芬奇在文件选择阶段即拒绝**，未进入解析。属于**格式不被该入口接受**，而非内容错误。

> 也就是说：**我们的 XML 内容有没有问题，这次实验无法证明**——因为根本没被读到。但既然 OTIO 直出可用，**XML 路线已无继续排查的必要**。

---

## 三、根因分析：为什么会走这条弯路

### 3.1 决策链条回溯

```
阶段 0 决策：需要给 PR / 达芬奇导出时间线
      ↓
假设（未验证）："PR 和达芬奇都用 FCP7 XML"
      ↓
选型：otio-fcp-adapter → 生成 xmeml v4 XML
      ↓
后果：适配器引入两处缺陷（重复 <rate> / 空 <format/>）
      ↓
补救：额外写 patch_fcp7_xml.py 修补
      ↓
实测：达芬奇 GUI 无法导入该 XML
```

### 3.2 真正的根因

**我们用"目标软件的兼容格式"去猜，而没有先查"目标软件原生支持什么"。**

查证发现——达芬奇官方 README（`Scripting/README.txt` 第 231 行）明确列出支持的导入格式：

```
ImportTimelineFromFile(filePath, {importOptions})
  # Creates timeline based on parameters within given file
  # (AAF/EDL/XML/FCPXML/DRT/ADL/OTIO)
```

**`OTIO` 在列表里。** 而且我们手里**本来就有** OTIO 原始文件——它是我们生成时间线的**源头**，XML 只是它的翻译产物。

**结论**：我们绕开原生通道，去走了一层老格式翻译，还额外承担了翻译损耗和修补成本。

### 3.3 三条教训

1. **先查目标软件的支持矩阵，再决定输出格式。** 不要先选工具，再假设目标兼容。
2. **不要为迁就第三方适配器的输出而绕开原生通道。** 适配器是补丁，不是通路。
3. **中间产物越少越好。** OTIO 直出 = 零翻译 = 零格式缺陷。XML 路线 = 翻译 + 修补 + 仍然失败。

---

## 四、待验证项（后续阶段）

| # | 待验项 | 为何重要 | 建议验证方式 |
|---|--------|----------|--------------|
| **V-A** | **OTIO 的 `Marker` 是否随导入保留** | 决定字幕传承方案。若保留，则字幕/Marker 可随 OTIO 一并传递，**优于 XML 路线的"标记 + 附 SRT"变通方案** | 达芬奇时间线上查看是否有 CYAN 标记 |
| **V-B** | 时间线是否含**音频轨** | 当前 OTIO 的音频轨道信息是否被识别 | 查看时间线轨道数 |
| **V-C** | 媒体池中片段是否**在线**（非离线红字） | 验证 `target_url` 绝对路径被正确解析 | 查看媒体池缩略图 |
| **V-D** | **Premiere 是否支持 OTIO 导入** | 决定 PR 通道能否统一到 OTIO | 需装 PR 实测 |
| V-E | `Fallback` 路径是否会丢帧精度 | 时间点 `00:00/03:00/06:00` 看起来准确，但需核对帧级精度 | 对比时间线帧数与预期 |

---

## 五、对项目的影响

### 5.1 导出通道重新定义

| 目标软件 | 原方案 | **新方案** |
|----------|--------|-----------|
| 剪映 | 剪映草稿（`pyJianYingDraft`） | 不变（阶段 0 已验证） |
| **达芬奇** | ~~FCP7 XML~~ | **OTIO 直出** ✅ |
| PR | FCP7 XML | **待定**（需实测 PR 是否支持 OTIO） |
| 通用兜底 | 片段 mp4 + SRT | 不变 |

### 5.2 实现简化

**达芬奇通道不再需要 `otio-fcp-adapter`**：

```python
# 旧（FCP7 XML 路线）——需第三方适配器 + 后处理修补
import otio_fcp_adapter          # 需额外 pip install
otio.adapters.write_to_file(timeline, "out.xml")
# + 必须调用 patch_fcp7_xml.py 修补两处缺陷

# 新（OTIO 直出）——主包内置，零依赖，零修补
import opentimelineio as otio    # 主包自带 otio_json
otio.adapters.write_to_file(timeline, "out.otio")
```

**仍然必须遵守**：
- `ExternalReference` **必须设 `available_range`**（OTIO 数据模型完整性要求，与适配器无关）
- `target_url` 用绝对路径，实测写法：`file:///C:/Users/.../source.mp4`

### 5.3 文档更新状态

| 文档 | 变更 |
|------|------|
| `TECH-DESIGN.md` → **v1.5** | §6.2 重写：新增 §6.2.2（OTIO 实测通过）/ §6.2.3（格式选择结论）/ §6.2.4（待验项）；R16 关闭，新增 R17（Fallback 丢元数据）/ R18（PR 未验证）；环境速查表更新 |
| `PRD-SliceQ.md` | F13 需更新为 OTIO 直出（**待办**） |
| `TASKS.md` | 阶段 5 交接提示词需改写为 OTIO 导出（**待办**） |

---

## 六、产物清单

| 文件 | 说明 |
|------|------|
| `test_drafts/SliceQ_Stage0_Test.otio` | ✅ **达芬奇导入成功**（本次验证的可用产物） |
| `test_drafts/SliceQ_Stage0_Test.davinci.xml` | ❌ 达芬奇导入失败（保留作证据） |
| `test_drafts/SliceQ_Stage0_Test.original.xml` | 阶段 0 原始 XML（备份） |
| `probe_resolve/patch_fcp7_xml.py` | XML 修补脚本（仅 FCP7 兼容通道需要） |

---

*报告生成：2026-09-24 | 阶段：0.6 | 结论：**OTIO 直出可用，FCP7 XML 失败** | 关键动作：导出通道改为 OTIO*
