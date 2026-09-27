# F16 验收报告 —— VideoCaptioner 联动

> 日期：2026-09-26
> 触发：用户提供了安装路径 `C:\VideoCaptioner`（此前本轮结论是"本机未安装，暂缓"）
> 产出：`sliceq/bridge.py`、设置页「外部工具」组
> 自测：`selftest_bridge.py`（**30 项全过**，含一条 AST 级 GPL 合规检查）
> 状态：✅ 完成 — **但功能形态与原计划不同（见下）**

---

## 1. 结论速查

| # | 问题 | 结论 |
|---|---|---|
| 1 | 它有没有 CLI | ❌ **没有**。`app/` 是纯 PyQt GUI 结构，无任何命令行入口 |
| 2 | 那"一键调用"还能做吗 | ❌ **不能**。功能改为「检测 + 报告能力 + 引导打开 + 文件交接」 |
| 3 | 检测方式能用注册表吗 | ❌ 不能。`reg.exe` 与 COM **都会被安全策略阻止** |
| 4 | 有没有额外收获 | ✅ 它自带 `ffmpeg.exe` / `whisper-cpp.exe`，可作为 SliceQ 缺 ffmpeg 时的备选 |
| 5 | GPL 合规怎么办 | ✅ 不 import / 不复制 / 不打包；只用 `Popen` 启动独立进程 |

---

## 2. 实测到的真实形态

安装于 `C:\VideoCaptioner`（pystand 打包）：

```
VideoCaptioner.exe        1.6 MB 启动器
_pystand_static.int       pystand 标记文件
app/                      明文 Python 源码
  ├ __init__.py  config.py
  ├ common/ components/ core/ thread/ view/    ← 纯 PyQt GUI 结构
  └ **没有任何 argparse / click / sys.argv 入口**
runtime/                  内嵌 Python 运行时（python.exe / pythonw.exe）
resource/bin/             **ffmpeg.exe、whisper-cpp.exe、
                            Faster-Whisper-XXL、aria2c.exe、7z.exe**
resource/subtitle_style/  字幕样式资源
work-dir/<素材名>/         工作产出目录（其下有 subtitle/）
AppData/{cache,logs,models}
unins000.exe              卸载程序
```

**关键判据**：`app/` 下**没有 `cli.py` / `__main__.py`，也没有 argparse/click/sys.argv
的任何引用**（实测 grep 无命中）。它是标准 PyQt GUI 应用。

而 `work-dir/` 里已经有 `[未明子]2026年09月04日23时录播` 与
`[未明子]2026年09月10日00时录播` —— **用户确实在用它处理同一批素材**，
这也印证了联动的实际价值。

---

## 3. 功能形态：为什么不做"一键调用"

TASKS 原来的设想是"检测 + 调 CLI"。**实测证伪后，这个设想不能执行** ——
不是技术上偷懒，而是**承诺一个做不到的功能比不提供更糟**：

> 用户点"用 VideoCaptioner 精修"→ 程序去调 CLI → 没有 CLI → 失败。
> 他不知道为什么，只会认为 SliceQ 坏了。

所以定为四件事：

| 能力 | 说明 |
|---|---|
| **检测** | 手动路径 > 环境变量 `VIDEOCAPTIONER_PATH` > 常见位置扫描 |
| **报告能力** | 在设置页显示：找到了什么、什么形态、工作目录在哪、自带哪些东西 |
| **引导打开** | `subprocess.Popen` + **DETACHED_PROCESS** 启动它（SliceQ 退出不会带走它）|
| **文件交接** | 按素材主文件名推算它在 `work-dir` 里的对应子目录（只推算，不创建、不复制）|

界面文案里明确写了：**"它是图形界面程序，没有命令行接口，所以只能由你在它自己的界面里操作"**。

---

## 4. 检测方式：注册表这条路被堵死了

| 手段 | 实测结果 |
|---|---|
| `where videocaptioner` | ❌ 不命中 |
| `pip show videocaptioner` | ❌ 未安装 |
| **`reg.exe` 查注册表** | ❌ **被安全策略阻止**（程序黑名单）|
| **COM 实例化（解析 `.lnk`）** | ❌ **同样被阻止** |
| 文件系统扫描 | ✅ 可用（本次就是靠它找到的）|

### 一条重要认识：**"数据目录存在" ≠ "程序已安装"**

第一轮排查时本机表现为：`%APPDATA%\VideoCaptioner` **存在**、
桌面 `VideoCaptioner.lnk` **存在**，但**可执行文件找不到**
（当时判断是"已卸载留残留"，实际是装在 `C:\VideoCaptioner` 这个非标准位置）。

⇒ 所以探测顺序改成：**手动路径 > 环境变量 > 常见位置扫描**，
并**允许用户手动指定**（`settings.videocaptioner_path`）。

---

## 5. 合规：GPL-3.0 的边界怎么守

VideoCaptioner 是 GPL-3.0，SliceQ 是 MIT。越线的后果是**整个 SliceQ 必须改成 GPL**。

允许的（GPL FAQ 的「聚合」）：

```
❌ 不 import 它的模块        ❌ 不复制它的源码
❌ 不把它打包进 exe          ❌ 不改写它的代码进我们这边
✅ 文件系统层面交接           ✅ subprocess.Popen 启动独立进程
```

**自测里有一条 AST 级检查**专门守这条线（`t_compliance`）：

```python
tree = ast.parse(bridge.py)
# ① 没有任何 videocaptioner 相关的 import
# ② 没有任何 read_text / read_bytes / readlines（读文件内容 = 抄源码）
# ③ 没有把它的目录加进 sys.path
```

> ⚠️ 第一版这条检查用**正则扫全文**，结果把模块头 docstring 里
> "`app/` 是源码目录"这句**说明文字**也匹配成了"读了对方的源码"，
> 报了个假阳性。**检查代码行为要用 AST，不能用正则扫文本** ——
> 源文件里既有代码也有文档，正则分不开。

---

## 6. 顺带发现的两件事

### 6.1 它自带一份 ffmpeg（可作为 SliceQ 的备选）

```
C:\VideoCaptioner\resource\bin\ffmpeg.exe
```

SliceQ 在缺 ffmpeg 时，界面会提示"VideoCaptioner 里有一份 ffmpeg，
可以在设置里指向它"。这比让用户去 gyan.dev 下载（国内 20KB/s，161MB 要两小时）实际得多。

> ⚠️ 但**不自动使用** —— 它的版本可能较旧或构建参数不同。
> 只作提示，由用户决定。

### 6.2 界面文案里的 Markdown 星号

第一版文案写成 `**没有命令行接口**`，而 QLabel **不渲染 Markdown**，
星号原样显示出来了。已去掉，并在代码里留了注释。
自测里加了一条断言守着它（`文案里不能有 Markdown 星号`）。

---

## 7. 本轮抓到的缺陷

| # | 缺陷 | 性质 |
|---|---|---|
| 1 | **手动路径填错时，只要自动发现在别处找到了程序就不报错** | 真缺陷。用户填的错路径一直留在输入框里，他会以为用的就是那一份（实际用的是自动发现的那份）。修法：去掉 `and not info["ok"]` 条件 —— **"用户填了什么"与"实际用了哪份"必须分开讲** |
| 2 | 合规检查用正则扫全文，把 docstring 误判成代码 | 测试判据写错（假阳性）。改用 AST |

---

## 8. 验证

| 脚本 | 项数 | 结果 |
|---|---|---|
| `selftest_bridge.py` | 30 | **30 / 30** |
| 其余回归（concurrency / translate / enhance / editor / export_chain） | 227 | 全过 |

自测用**临时目录构造假的安装结构**（GUI 形态 / CLI 形态 / 认不出来三种），
所以**不依赖本机是否安装**。真机路径只作为附加信息打印，不作断言依据。

覆盖：
- 形态判定依据目录结构（不是硬编码）
- 手动路径（填目录 / 填 exe）都能认
- 环境变量
- **填错必须报出来**
- 文件交接目录推算（不创建目录）
- 文案无 Markdown 星号
- **AST 级合规检查**

---

## 9. 未做 / 待办

| 项 | 说明 |
|---|---|
| 真机启动验证 | 沙箱里不能真启动 GUI 进程；`launch()` 的代码路径**未在真机上点过**。需要用户手动点一次「打开」确认 |
| 「把成片交给它」的一键动作 | 目前只推算交接目录，没有"把成片复制进去"的动作。**是否需要，取决于你的工作流** —— 你在 VideoCaptioner 里通常是直接打开原始录播，还是打开切好的成片？ |
| 若官方未来出 CLI | `has_cli()` 会自动认出来（形态判定不是写死的），但**参数表需要另行查证**，不要猜 |
