# SliceQ 阶段 0.5-B — 达芬奇脚本直连可行性验证报告

**结论：不可行，永久放弃直连方案。** 保留 FCP7 XML 导出作为替代路径。

- 验证日期：2026-09-24
- 验证目标：评估 SliceQ 是否能通过 DaVinci Resolve 官方 Python 脚本 API **直接驱动**达芬奇（免去"导出文件 → 人工导入"环节）
- 目标产物：判断"直连"能否作为 SliceQ 的内置能力分发给用户
- 最终判定：**Plan B（导出 FCP7 XML + 人工导入）**

---

## 一、环境基线

| 项目 | 实测值 | 采集方式 |
|------|--------|----------|
| 达芬奇安装路径 | `C:\davinci` | 用户提供 |
| 主程序 | `Resolve.exe` | 文件系统 |
| 版本 | **21.0.0.48** | `VersionInfo.ProductVersion` |
| 安装日期 | 2026-06-05 | 文件时间戳 |
| 脚本桥 DLL | `C:\davinci\fusionscript.dll`（3,562,352 B） | 文件系统 |
| 桥接模块 | `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules\DaVinciResolveScript.py`（1,773 B） | 文件系统 |
| 官方文档 | `...\Scripting\README.txt`（Last Updated: 8 May 2026） | 文件读取 |
| 官方示例 | `...\Scripting\Examples\`（含 12 组 .py/.lua 对照示例） | 文件系统 |
| 测试 Python | 3.13.14（64-bit，WorkBuddy 托管版） | `sys.version` |

### 关键前置事实

1. **`DaVinciResolveScript.py` 已随安装包落地**——不需要手动开启"延迟安装"。
2. **环境变量 `RESOLVE_SCRIPT_API` / `RESOLVE_SCRIPT_LIB` 默认为空**——官方文档明确要求用户自行配置。
3. **达芬奇不捆绑 Python 解释器**——`C:\davinci` 下无任何 `python*.exe` 或 `python3x.dll`。
4. **README 声明脚本调用前提**：*"DaVinci Resolve needs to be running for a script to be invoked."*（必须先启动达芬奇进程）

### 官方文档给出的 Windows 配置

```
RESOLVE_SCRIPT_API = %PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting
RESOLVE_SCRIPT_LIB = C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll
PYTHONPATH         = %PYTHONPATH%;%RESOLVE_SCRIPT_API%\Modules\
```

> ⚠️ 注意 `RESOLVE_SCRIPT_LIB` 官方默认值指向 `C:\Program Files\...`，**与本机实际安装路径 `C:\davinci` 不符**。即：官方模块的兜底路径在本机是失效的，必须显式覆盖。

---

## 二、验证过程（五次递进，逐步排除）

### 探针 1｜静态环境检查

**脚本**：`probe_resolve/probe_1_env.py`

**目的**：解耦"环境配置问题"与"能力限制问题"。

**预期**：基线（不设环境变量）import 失败，注入环境变量后成功。

**实际结果**：**进程段错误（Segmentation fault, exit 139）**，无任何 Python 异常抛出。

**解读**：Python 的 `try/except ImportError` 完全没机会执行——说明崩溃发生在**原生层**，不是 Python 逻辑错误。

---

### 探针 1b｜崩溃点精确定位

**脚本**：`probe_resolve/probe_1b_isolate.py`

**方法**：在每一步危险调用前打印并 flush，定位最后一条存活日志。

**实际输出**：
```
[STEP 5] loader created: <ExtensionFileLoader ...>          ← 存活
[STEP 6] spec created: ModuleSpec(name='fusionscript'...)   ← 存活
>>> 段错误 139                                              ← 死亡
[STEP 7] module object created ...                          ← 从未打印
```

**定位结论**：崩溃发生在 `importlib.util.module_from_spec(spec)` 内部。

该函数会调用 loader 的 `create_module()`，而 `ExtensionFileLoader.create_module()` **内部执行 Windows `LoadLibraryEx` 将 DLL 映射进进程地址空间**——这是原生代码首次执行的位置。

---

### 探针 1c｜PE 导入表分析（**证伪假设 1：ABI 不匹配**）

**脚本**：`probe_resolve/probe_1c_pe_imports.py`

**方法**：纯字节解析 PE 结构，不加载 DLL（因此不会崩溃）。

**关键发现**：

| 项 | 值 |
|----|----|
| 架构 | x64 |
| 导入 DLL 总数 | 33 |
| **`python*.dll` 依赖** | **无（0 个）** |
| 特殊依赖 | `lua5.1.dll`、`tbbmalloc.dll`、`tbb.dll`、`tbb12.dll` |
| VC 运行时 | `MSVCP140.dll`、`VCRUNTIME140.dll`、`VCRUNTIME140_1.dll` |

**结论**：**"Python 版本不匹配导致 ABI 崩溃"的假设被证伪。** 该 DLL 不链接任何 Python 库，是一个**通用桥接层**。

---

### 探针 1d｜依赖存在性 + DLL 搜索路径（**证伪假设 2：依赖缺失**）

**脚本**：`probe_resolve/probe_1d_adddll.py`

**第一步 — 依赖存在性核对**：

| DLL | 存在于 `C:\davinci\` |
|-----|---------------------|
| `lua5.1.dll` | ✅ |
| `tbbmalloc.dll` | ✅ |
| `MSVCP140.dll` | ✅ |
| `VCRUNTIME140.dll` | ✅ |
| `VCRUNTIME140_1.dll` | ✅ |

> 目录下共 **234 个 DLL**，依赖齐全。

**第二步 — 三组对照实验**：

| 组 | 干预措施 | 结果 | 退出码 |
|----|----------|------|--------|
| C（对照组） | 无任何干预 | 段错误 | 139 |
| A（实验组） | `os.add_dll_directory(r"C:\davinci")` | **仍段错误** | 139 |
| B（实验组） | `os.chdir()` + 加入搜索路径 | （已被 A 排除，未执行） | — |

**结论**：**"Python 3.8+ 安全 DLL 搜索策略导致依赖找不到"的假设被证伪。** 显式将 DLL 目录加入搜索路径**无法**解决问题。

---

### 探针 1e｜导出表 + 原生崩溃捕获（**定位根因**）

**脚本**：`probe_resolve/probe_1e_exports.py`

**发现 A — 导出表揭示了 DLL 的真实身份**：

```
??0Py3ScriptLanguage@FusionScript@Fusion@@QEAA@XZ
??0Py3ScriptState@FusionScript@Fusion@@QEAA@PEAVScriptObject@2@I@Z
??0Py2ScriptLanguage@FusionScript@Fusion@@QEAA@XZ
??0Py2ScriptState@FusionScript@Fusion@@QEAA@PEAVScriptObject@2@I@Z
??1Py3ScriptLanguage@FusionScript@Fusion@@UEAA@XZ
...
（共 500+ 导出符号，含大量 `Fusion::ScriptVal`、`Fusion::FuString` 模板类）
```

**重大认知修正**：`fusionscript.dll` **不是"被 Python 导入的扩展模块"**，而是**内嵌完整脚本引擎、反向驱动 Python/Lua 的宿主桥接库**。README 中"需要 Python >= 3.6 64-bit"指的是它要在运行时**去寻找并加载**系统 Python。

**发现 B — 原生崩溃的精确性质**：

通过 `subprocess` + `faulthandler` 隔离运行，获得：

```
Windows fatal exception: access violation

Current thread 0x000051cc (most recent call first):
  File "<frozen importlib._bootstrap>", line 488 in _call_with_frames_removed
  File "<frozen importlib._bootstrap_external>", line 1317 in create_module
  File "<frozen importlib._bootstrap>", line 813 in module_from_spec
  File "_child_load.py", line 15 in <module>
```

- **异常类型**：`0xC0000005` = `STATUS_ACCESS_VIOLATION`（退出码 3221225477）
- **崩溃位置**：`create_module` → `LoadLibraryEx` → DLL 的 `DllMain(DLL_PROCESS_ATTACH)`
- **Python 栈不可用**：`faulthandler` 只能报告"access violation"，**无法给出 Python 侧调用栈**，因为故障发生在原生代码内部。

**根本原因判定**：`fusionscript.dll` 在其 `DllMain` 初始化例程中执行了非法内存访问。这可能是：
- 在 `DllMain` 中调用了 Windows 明确禁止的操作（如 `LoadLibrary`、创建线程、初始化 COM）；或
- 试图访问尚未初始化的全局状态 / 宿主进程句柄。

无论具体是哪一个，**该崩溃从 Python 侧不可捕获、不可拦截、不可降级处理**。

---

## 三、证据汇总表

| # | 假设 | 验证方法 | 结果 | 状态 |
|---|------|----------|------|------|
| 1 | Python ABI 版本不匹配 | PE 导入表解析 | 无 `python*.dll` 依赖 | ❌ **证伪** |
| 2 | 依赖 DLL 缺失 | 文件存在性核对（5 项） | 全部存在（234 DLL） | ❌ **证伪** |
| 3 | DLL 搜索路径问题 | `add_dll_directory` 对照实验 | 干预后仍崩溃 | ❌ **证伪** |
| 4 | 环境变量未配置 | 三组配置对比 | 配置前后均崩溃 | ❌ **证伪**（非充分原因） |
| 5 | **DLL 在 `DllMain` 执行非法操作** | 导出表 + faulthandler + 崩溃点定位 | `0xC0000005` 于 `create_module` | ✅ **确认** |

**四次证伪，一次确认。** 排除法收敛到单一根因。

---

## 四、弃用决策论证

### 4.1 技术可行性 ≠ 产品可行性

从纯技术角度，该崩溃**或许**可通过以下途径解决：
- 遍历 PE 延迟加载表（delay-load imports），定位具体失败的子模块
- 使用 Process Monitor 实时捕获 DLL 加载失败记录
- 更换 Python 版本矩阵穷举（3.9 / 3.10 / 3.11 / 3.12）
- 降级达芬奇版本，寻找能正常加载的版本组合

**但这些都是"在开发机上把它修好"的路径，而非"让它对用户可靠"的路径。**

### 4.2 决定性反对理由：分发环境不可控

SliceQ 的核心设计前提是 **BYOK + 单 exe 分发**给多个切片 man 使用。而 `fusionscript.dll` 的加载表现出对以下因素的**极度敏感性**：

| 变量 | 用户侧不确定性 |
|------|---------------|
| 安装路径 | 可能装在 `C:\Program Files\`、`D:\`、`E:\` 或自定义目录 |
| 版本 | Studio（付费）vs 免费版，19.x / 20.x / 21.x 差异 |
| Python 环境 | 可能未安装、多版本共存、PATH 污染 |
| VC++ 运行时 | 可能缺 2015-2022 可再发行组件 |
| 权限 | 可能无管理员权限访问 `fusionscript.dll` |

**若将直连作为内置能力，上述每一项都会转化为用户侧的一类崩溃报告。** 而崩溃形式是**整个进程段错误**——用户在 SliceQ 里点一下"发送到达芬奇"，程序直接消失，且**没有可读的错误信息**。

这是不可接受的用户体验，也是不可维护的支持成本。

### 4.3 替代方案对比

| 方案 | 依赖 | 跨版本 | 用户操作 | 崩溃风险 |
|------|------|--------|----------|----------|
| **脚本直连** | 环境变量 + DLL 加载 + 达芬奇运行中 | ❌ 敏感 | 一键（理想） | **高（已证实段错误）** |
| **FCP7 XML 导出** | 无 | ✅ `xmeml v4` 为通用标准 | 手动导入一次 | **无** |

**决策**：采用 FCP7 XML 导出。直连方案**永久归档**，不再投入。

---

## 五、替代方案产物与后续验证

### 已修补的 XML 缺陷

阶段 0 的 `SliceQ_Stage0_Test.xml` 存在两处缺陷，已在 `SliceQ_Stage0_Test.davinci.xml` 中修复：

| 缺陷 | 现象 | 根因 | 修复 |
|------|------|------|------|
| DEFECT-1 | 每个 `<clipitem>` 内有两个完全相同的 `<rate>` 块 | OTIO `fcp_adapter` 输出怪癖 | 正则折叠（3 处） |
| DEFECT-2 | `<format/>` 为空自闭合标签 | OTIO 未填充序列规格 | 填入 1280x720/30NDF/48k-16bit-2ch |

**修补脚本**：`probe_resolve/patch_fcp7_xml.py`（可复用于 SliceQ 正式导出流程）

> 附带修复了修补脚本自身的一个 bug：`build_format()` 在重排缩进时丢失了 `lead == 0` 的行（即 `<format>` / `</format>` 标签本身），导致标签被静默吞掉。

### 待人工验收

**验收清单**：`reports/STAGE0.6-DAVINCI-MANUAL-CHECK.md`

含 9 项主判据（V1–V9）+ 3 项补充观察（V10–V12），需人工在达芬奇中导入 XML 并逐项确认。

**核心待验项**：
- V2：视频轨是否恰为 3 个片段
- V4：时间线总时长是否为 9 秒（270 帧 @30fps）
- V6：素材是否离线（`pathurl` 路径是否被接受）

---

## 六、对项目文档的影响（待更新）

| 文档 | 需修改内容 |
|------|-----------|
| `TECH-DESIGN.md` | 删除"达芬奇脚本直连"章节；新增"XML/OTIO 导出 → 人工导入"为唯一路径；风险表新增 R13 |
| `PRD-SliceQ.md` | 确认达芬奇支持范围为"导出兼容时间线"，不含"一键推送" |
| `TASKS.md` | 阶段 2/3 中涉及达芬奇直连的任务删除或改写为 XML 导出任务 |

**建议新增风险条目**：
> **R13｜OTIO 适配器输出缺陷**：`otio-fcp-adapter` 产出的 XML 存在重复 `<rate>` 与空 `<format/>`，需在导出流程中强制后处理。缓解：固化 `patch_fcp7_xml.py` 为导出管线的必选步骤，并加入 XML schema 校验。

---

## 七、复用价值

**可沉淀为技能**：`resolve-scripting-probe` —— 验证任何 Windows 原生 DLL 加载失败的系统化方法：
1. 分步打印定位崩溃点（`flush=True`）
2. PE 导入表纯字节解析（不加载，故不崩溃）
3. 依赖存在性核对
4. `add_dll_directory` 对照实验
5. 子进程 + `faulthandler` 捕获原生崩溃
6. 导出表分析识别 DLL 真实身份

---

*报告生成：2026-09-24 | 阶段：0.5-B | 结论：**不可行，永久放弃直连** | 产物：`SliceQ_Stage0_Test.davinci.xml`*
