# 阶段 6 · 干净 Windows 虚拟机验证

> 日期：2026-09-28｜执行：WorkBuddy（自动化）＋ 用户（提供 ISO / 决策）
> 对象：`dist/SliceQ.exe`（PyInstaller onefile，v0.1.0）
> 关联：`docs/TASKS.md` 阶段 6、`docs/TECH-DESIGN.md`、`reports/STAGE6-PACKAGING.md`（若有）

---

## 1. 为什么做这一步

阶段 6 的验收标准里有一条：**「干净机器双击 exe 全流程跑通」**。
而此前所有验证都跑在**这台已经装好 ffmpeg、Python、各种依赖的机器**上。

SliceQ 的分发故事是 **BYOK + 单 exe 给陌生人用** —— 那些人的机器上：
没有 Python、没有 ffmpeg、没有 `%APPDATA%\SliceQ`、没有配过 hf-mirror。
**这条路径一次都没在"真的干净"的环境里跑过。**

⇒ 搭一台全新 Windows 11 虚拟机，把 exe 丢进去实测。

---

## 2. 环境与方法

### 2.1 虚拟机

| 项 | 值 |
|---|---|
| 宿主 | Windows 11（10.0.26200） |
| 虚拟化 | VirtualBox **7.2.20 r175154**（winget 安装，52 秒，未弹 UAC） |
| 客户机 | **Windows 11 25H2 专业版 · 中文**（与宿主同版本，少一个变量） |
| 规格 | 8 GB 内存 / 4 vCPU / EFI / **TPM 2.0** / NAT / 无音频 / 80 GB 动态磁盘 |
| 屏幕 | **1024×768**（★ 这个条件后来成为关键） |
| 账户 | `SliceQ`（无人值守安装时创建） |
| ISO | `Win11_25H2_Chinese_Simplified_x64_v2.iso`（7.96 GB，用户提供） |

### 2.2 关键手法：不用在客户机里敲命令

沙箱禁止从 bash 调 `cmd.exe` / `powershell.exe`。改用 **`VBoxManage guestcontrol`
自带能力** —— 传文件、跑程序、取回文件、查文件属性，全程不需要客户机 shell：

```bash
VBoxManage guestcontrol <vm> copyto    --username .. --password .. <本地> <客户机路径>
VBoxManage guestcontrol <vm> copyfrom  --username .. --password .. <客户机路径> <本地>
VBoxManage guestcontrol <vm> stat      --username .. --password .. <客户机路径>
VBoxManage guestcontrol <vm> run/start --exe <客户机 exe> --putenv=K=V \
        --wait-stdout --wait-stderr
```

**headless 不等于瞎** —— `controlvm <vm> screenshotpng` 可随时截屏取证。

### 2.3 两个必须知道的坑

| 坑 | 症状 | 解法 |
|---|---|---|
| **装完增强功能必须重启** | `guestcontrol` 报 `The guest execution service is not ready (yet)`；`showvminfo` 的 facility 列表里**没有 Guest Control**（有 Base Driver / Graphics 也没用） | `controlvm <vm> reset` 重客户机，之后 `copyto` 立刻可用 |
| **截图接口会 E_FAIL** | `TakeScreenShotToArray ... E_FAIL`；此时 `VMState` 仍是 `running` | 客户机屏幕休眠所致。发一次按键 + `controlvm <vm> setvideomodehint 1024 768 32` 即恢复 |

---

## 3. 验证结果（通过的项）

### 3.1 启动与初始化

| 项 | 证据 |
|---|---|
| **exe 在干净环境启动** | 进程持续运行；客户机生成 `%APPDATA%\SliceQ` |
| **GUI 完整渲染** | 截图：标题栏「SliceQ 直播切片 v0.1.0」、侧栏四页、拖放区、按钮行、表格列头（名称/时长/分辨率/状态/来源）全部在位 |
| **无 ffmpeg 也能启动** | `bin/`、`models/` 已建但**为空**（确证客户机没有 ffmpeg），程序照常启动，不崩不卡 |
| **数据目录正确** | `C:\Users\SliceQ\AppData\Roaming\SliceQ`（与 exe 位置无关） |
| **数据库就绪** | `logs/sliceq.log`：`SliceQ v0.1.0 启动 \| 数据目录 …` / `数据库就绪：…\db\sliceq.db` |
| **`PRAGMA user_version = 5`** | 与源码 `store.SCHEMA_VERSION = 5` 一致 |

### 3.2 数据库结构（把 db 取回本地解析）

7 张表（`clip` `export` `marker` `subtitle` `task` `transcript_seg` + `sqlite_sequence`），
全部为 0 行（干净环境，符合预期）。

**5 个子表的外键全部为 `ON DELETE CASCADE`** —— 硬约束 #13 那次
「删任务后 659 行转录残留」的修复，**确认已进入冻结产物**：

```
clip.task_id           → task.id   ON DELETE CASCADE
export.task_id         → task.id   ON DELETE CASCADE
marker.task_id         → task.id   ON DELETE CASCADE
subtitle.task_id       → task.id   ON DELETE CASCADE
transcript_seg.task_id → task.id   ON DELETE CASCADE
```

> 取证细节：直接复制 `sliceq.db` 得到的是 **4096 字节、0 张表** ——
> 因为库跑在 **WAL 模式**，schema 还在 `sliceq.db-wal`（177 KB）里。
> **必须把 `-wal` 一并取回**，否则会误判成"数据库是空的"。

### 3.3 三个网络端点（从客户机内实测）

VM 走 NAT —— **不经过宿主环境变量里那个代理**。用客户机自带的 `curl.exe` 直测：

| 用途 | 端点 | 结果 |
|---|---|---|
| ffmpeg 来源 | `api.github.com/repos/GyanD/codexffmpeg/releases/latest` | **HTTP 200**（0.84 s） |
| 模型来源 | `hf-mirror.com/ggerganov/whisper.cpp/resolve/main/…` | **HTTP 302**（3.1 s，正常跳 CDN） |
| 分析端点 | `dashscope.aliyuncs.com/compatible-mode/v1/models` | **HTTP 401**（0.63 s，未认证 = 端点正常） |

⇒ 干净机器上「首次下载 ffmpeg + 模型」的网络前提成立。

### 3.4 打包合规

| 项 | 结果 |
|---|---|
| 体积 | **61 MB**（预算 150 MB） |
| 直接读 exe 的 TOC（271 条目） | **无任何 ffmpeg / ffprobe**（GPL 红线守住） |

---

## 4. ★ 本次抓到的真缺陷（P1，已修）

### 4.1 症状

侧栏底部的**环境提示条整块不可见**，像素级扫描确认侧栏 y 300~719 **完全空白**，
而日志里**一条错误都没有**。

### 4.2 定位（三步，都是客观证据）

1. **宿主复现**：同源码 + `QT_QPA_PLATFORM=offscreen` 建主窗口并 `grab()` 成 PNG
   ⇒ 三种场景（有/无 ffmpeg、不同窗口尺寸）**全部正常渲染**。
   ⇒ 排除"文案没设上"，问题在别处。

2. **给发布版加诊断开关**（`SLICEQ_DIAG=1`，生产环境惰性），
   在客户机里跑并抓 stdout：

   ```
   [DIAG] env_hint text='还没有任务\n拖个视频进来试试\n⚠️ FFmpeg 未就绪\n去「设置」安装'
          visible=True  geo=QRect(12, 940, 176, 69)
          sidebar=QSize(200, 1027)   window=QSize(1180, 1027)
   ```

   ⇒ 文案在、控件"可见"、**但窗口高 1027，而屏幕只有 768** ⇒ 提示条在屏幕之外。
   ⇒ 而代码里写的是 `resize(1180, 720)` —— **说明窗口被别的东西顶大了**。

3. **量最小尺寸**：

   ```
   SettingsPage.minimumSizeHint() = QSize(858, 973)      ← 元凶
   MainWindow.minimumSizeHint()   = QSize(1058, 973)
   ```

### 4.3 根因链

```
设置页 5 个 QGroupBox 叠起来，自然高度 ≈973px，且**没有滚动区**
        ↓
QStackedWidget 取「所有页面里最大的最小尺寸」当自己的最小尺寸
        ↓
主窗口最小尺寸被顶到 1058 × 973（代码里的 resize(1180, 720) 完全失效）
        ↓
768p 屏（或 1080p @125% 缩放，逻辑高度仅 864）放不下 ⇒ 窗口底部出屏
        ↓
Windows 不允许把标题栏拖出屏幕上边界 ⇒ 出屏部分**永远够不到**
```

**后果不只是提示条看不见**：设置页的下半部分（「分析性能」「外部工具」——
也就是**装 FFmpeg 的入口**）一起被裁到屏幕外，
而这恰恰是干净机器上**必须做的第一步**。

> ⚠️ 这个缺陷在开发者的大屏机器上**永远看不出来**。项目里 600+ 项自测
> 也一条都没覆盖 —— 因为**没人量过"窗口最小尺寸能不能塞进小屏"**。
> 这与硬约束 #29 是同一类：**维度缺失比代码错误更难发现**。

### 4.4 修复

| 文件 | 改动 |
|---|---|
| `sliceq/ui/pages/settings_page.py` | 5 个分组放进 `QScrollArea`（`setWidgetResizable(True)`，纵向/横向均 `AsNeeded`）。照抄项目里 `style_page.py` 已有的正确写法 |
| `sliceq/ui/main_window.py` | 初始尺寸**不再写死 1180×720**，改为按屏幕可用区域夹取 |

**修复前后：**

| | 修复前 | 修复后 |
|---|---|---|
| `SettingsPage` 最小尺寸 | 858 × **973** | 116 × **144** |
| `MainWindow` 最小尺寸 | 1058 × **973** | 960 × **345** |
| VM 里实际窗口 | **1180 × 1027**（> 屏幕） | **984 × 680**（放得下） |
| 侧栏提示条 | y=940，屏幕外 | y=593，底边 662 < 680 ✅ |

**客户机复验截图**：侧栏底部现在显示

```
还没有任务
拖个视频进来试试
⚠️ FFmpeg 未就绪
去「设置」安装
```

⇒ **干净机器上的第一屏，现在会直接告诉用户"缺什么、去哪装"。**

### 4.5 回归自测（新增）

`stage0/probe_resolve/selftest_window_layout.py` — **12 项全过**，把这件事变成硬断言：

- 主窗口最小尺寸必须 ≤ 700 × 1024（能塞进 768p 屏）
- **每一个页面**的最小高度 ≤ 700（任何一个超标都会顶起整窗）
- 设置页必须被 `QScrollArea` 包住，且最小高度 < 自然高度
- 侧栏提示条必须有文案、可见、且**底边落在窗口高度内**

---

## 5. 顺带修掉的第二个缺陷：发布版看不见异常

`ui/workers.py::guard_ui` 原本只做 `traceback.print_exc()`。

**发布版是 `--windowed` 打包**：用户双击启动时 **`sys.stderr` 是 `None`**，
`print_exc()` 写进一个不存在的地方 ⇒ **UI 回调里的任何异常，在我们这边完全看不到**。

这正是本次排查一开始卡住的原因：界面少了东西，日志里却干干净净。

**修复**：`guard_ui` 捕获后**同时写进日志**（`logging.getLogger("sliceq.ui").exception(...)`），
落进 `logs/sliceq.log`。

---

## 6. 本次**没有**验证的（如实列出）

| 项 | 说明 |
|---|---|
| **完整业务链路** | 转录 → 粗筛 → 精析 → 切片 → 导出，**一步都没在 VM 里跑**。需要 GUI 交互（VBoxManage 没有鼠标注入能力）+ API key + 1.8 GB 模型下载 |
| **ffmpeg 自动下载 + 解压** | 只验证了**端点可达**（HTTP 200），没真下载过。该路径在本机由 `selftest_first_launch.py` 覆盖 |
| **模型下载** | 同上 |
| **SmartScreen / MotW** | exe 是通过 Guest Additions 通道拷进去的，**没有「网络来源」标记**，所以不会触发 SmartScreen。真实用户从 GitHub 下载会带 MotW，**大概率弹「Windows 已保护你的电脑」** ⇒ README 应写明「更多信息 → 仍要运行」。**未实测** |
| **杀软拦截** | 新装的 Win11 只有 Defender，未观察到拦截；未测第三方杀软 |
| **多屏幕 / 高 DPI 实机** | 最小尺寸的**逻辑值**已量化并加了断言，但没有在真实 1080p@125% 或 4K 屏上肉眼确认 |
| **音频设备 / 声卡** | VM 配置为无音频，与 GUI 无关 |

> **别把「虚拟机验过了」当成安心**：它盖住的是**环境兼容性**这一块，
> 网络下载、API 连通、真实素材跑通都还在外面。

---

## 7. 可复现命令（备查）

```bash
VB="C:/Program Files/Oracle/VirtualBox/VBoxManage.exe"
VM="SliceQ-Clean-Test"

# —— 建机 ——
"$VB" createvm --name "$VM" --ostype Windows11_64 --register
"$VB" modifyvm "$VM" --memory 8192 --cpus 4 --firmware efi --tpm-type 2.0 \
       --vram 128 --graphicscontroller vboxsvga --nic1 nat --audio-enabled off
"$VB" createmedium disk --filename "…\\$VM.vdi" --size 81920 --format VDI
"$VB" storagectl "$VM" --name SATA --add sata --controller IntelAhci --portcount 4
"$VB" storageattach "$VM" --storagectl SATA --port 0 --device 0 --type hdd \
       --medium "…\\$VM.vdi"

# —— 无人值守装 Windows（★ --install-additions 必须有，否则没法从外部操作）——
"$VB" unattended install "$VM" --iso="<iso 路径>" \
       --user=SliceQ --password='…' --full-user-name=SliceQ \
       --image-index=4 --locale=zh_CN --country=CN --time-zone=Asia/Shanghai \
       --install-additions --start-vm=headless

# —— 装完后必须重启，否则 guestcontrol 不可用 ——
"$VB" controlvm "$VM" reset

# —— 传 exe → 跑 → 截图 → 取回日志 ——
"$VB" guestcontrol "$VM" copyto   --username SliceQ --password '…' \
       "<本地 exe>" 'C:\Users\SliceQ\Desktop\SliceQ.exe'
"$VB" guestcontrol "$VM" run      --exe 'C:\Users\SliceQ\Desktop\SliceQ.exe' \
       --username SliceQ --password '…' --putenv=SLICEQ_DIAG=1 \
       --wait-stdout --wait-stderr
"$VB" controlvm   "$VM" screenshotpng 'C:\temp\vm.png'
"$VB" guestcontrol "$VM" copyfrom --username SliceQ --password '…' \
       'C:\Users\SliceQ\AppData\Roaming\SliceQ\logs\sliceq.log' 'C:\temp\sliceq.log'
```

**注意**：客户机里的 `curl.exe` 可直接用（不受沙箱对 cmd/powershell 的限制），
是"在客户机内测网络"的现成工具。

---

## 8. 结论

| 阶段 6 验收项 | 结论 |
|---|---|
| 干净机器双击 exe **能启动、GUI 正常** | ✅ **通过**（截图 + 日志 + 数据目录 + DB 四重证据） |
| 首次运行所需的三个网络端点可达 | ✅ 通过（客户机内实测） |
| 打包产物不含 ffmpeg | ✅ 通过（271 条目逐个检查） |
| 数据层降级/迁移正确 | ✅ 通过（schema v5 + 5 条级联外键） |
| **小屏（768p）可用性** | ❌ 原本**不通过** → 已修 → ✅ 通过（含 12 项回归断言） |
| 完整业务链路在干净机器上跑通 | ⏳ **未验证**（见 §6） |

**本次最大收获不是"通过"，而是抓到了两件只有真干净环境 + 小屏才会暴露的问题**：
① 窗口最小尺寸顶破屏幕，用户够不到装 FFmpeg 的入口；
② 发布版里所有 UI 异常都是静默的，我们看不到。
两条都恰好落在「**我们实现了什么**」与「**用户实际会遇到什么**」之间的缝里。
