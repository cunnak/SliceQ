# SliceQ 阶段 1-B — 首次启动流程验证（干净环境）

- 日期：2026-09-25
- 状态：✅ **通过 12 项 / 失败 0 项**
- 结论：**首次启动的自动下载链路可用**，但过程中暴露出 **2 个产品级缺陷 + 1 个环境事实**

---

## 一、为什么必须做这个测试

阶段 1 的 33 项自测，是在**一台已经装好 ffmpeg 的机器**上跑的。

⇒ **"下载 ffmpeg" 和 "下载模型" 两条分支一次都没执行过。**

而 SliceQ 的整个价值前提是 **BYOK + 单 exe 分发给别的切片 man**：
那些人的机器上没有 ffmpeg、没配过 hf-mirror。
**分发故事里最脆的一环，恰恰是唯一没跑过的那一环。**

本测试模拟"干净机器"：
1. 从 `PATH` 里摘掉所有含 `ffmpeg.exe` 的目录 → `shutil.which()` 找不到
2. 清掉手动指定的 ffmpeg 路径
3. 此时探测应报"缺失"
4. 走自动下载 → 解压 → **强制验证 whisper 滤镜**
5. 再次探测应报"就绪"

脚本：`sliceq/selftest_first_launch.py`

---

## 二、缺陷一：**"full build" 这个名字不可信**（会被直接毁掉产品）

### 现象

第二轮测试：

```
[FAIL] FFmpeg 自动下载成功 —
  下载的 FFmpeg 缺少 whisper 滤镜
  （来自 https://github.com/BtbN/FFmpeg-Builds/... win64-gpl.zip）
```

### 根因

**BtbN/FFmpeg-Builds 的 `ffmpeg-master-latest-win64-gpl.zip` 不含 `--enable-whisper`。**

尽管：
- 文件名写着 `gpl`（完整许可构建）
- 构建描述像 full build
- 体积 186.9MB，不小

**但 `ffmpeg -filters` 里就是没有 `whisper`。**

### 这个缺陷有多严重

**它不会在阶段 1 暴露。** 下载成功、解压成功、`ffmpeg -version` 能跑、
拖视频进去元信息也能读 —— **一切看起来正常**。

直到**阶段 2 要转录时**才炸，而那时的报错是"滤镜不存在"，
排查者会以为是代码问题，而不是"半年前下的那份 ffmpeg 是错的"。

### 修正

**引入"下载后必须实际验证"的强制关卡：**

```python
for url, kind, label in _build_sources():
    download + extract
    if not has_whisper(ff):        # 实际跑 ffmpeg -filters
        problems.append(f"{label}：缺少 whisper 滤镜")
        _clean_installed()          # 清掉，别留下占位
        continue                    # 换下一个源
    return ff
```

**关键点：验证不通过不是"抛错结束"，而是"换下一个源"。**
并且把不合格的 ffmpeg **删掉** —— 否则 `find_ffmpeg()` 会选中它，
用户永远卡在"检测到 ffmpeg 但没 whisper"的状态。

---

## 三、缺陷二：**唯一带 whisper 的源，在国内慢到不可用**

### 现象

第一轮测试：`gyan.dev` 下载 10 分钟只走了 14MB。

### 实测速度对比

| 源 | 格式 | 实测速度 | 161MB 预计耗时 |
|---|------|---------|---------------|
| gyan.dev 官网 | `.7z` | **20.7 KB/s** | **≈ 2 小时** |
| GitHub（BtbN） | `.zip` | 18 MB/s | ≈ 10 秒 |
| hf-mirror | `.bin` | 4.2 MB/s | — |

**慢的恰好是唯一带 whisper 的那个源。** 作为"首次启动自动安装"的体验，**完全不可用**。

### 修正：改用 Gyan 发布在 GitHub 上的官方构建

调查发现：**Gyan 自己在 GitHub 上也发布构建**（仓库 `GyanD/codexffmpeg`）。

```
ffmpeg-9.0.2-full_build.zip     245.8 MB    ← 就是它
ffmpeg-9.0.2-full_build.7z      161.1 MB
ffmpeg-9.0.2-full_build-shared.zip  95.3 MB
ffmpeg-9.0.2-essentials_build.zip  109.5 MB
```

`ffmpeg-<版本>-full_build.zip` 同时满足三个条件：

| 条件 | 满足 |
|------|------|
| Gyan 官方 full build（**含 whisper**） | ✅ |
| 托管在 **GitHub**（快） | ✅ |
| **`.zip`**（标准库可直接解，不需 `py7zr`） | ✅ |

**实现上通过 GitHub API 动态解析最新版本**，而不是硬编码版本号：

```python
_GYAN_GH_API = "https://api.github.com/repos/GyanD/codexffmpeg/releases/latest"
# 找到 name.endswith("-full_build.zip") 的 asset
```

这样上游发新版时自动跟上，不需要改代码。

### 源优先级（最终）

| # | 源 | 说明 |
|---|---|---|
| 1 | `GyanD/codexffmpeg` 的 `*-full_build.zip` | **主源**：快 + 含 whisper + zip |
| 2 | gyan.dev 官网 `ffmpeg-release-full.7z` | 兜底：内容等价但国内慢 |
| 3 | BtbN `win64-gpl.zip` | 最后兜底：快但**会被验证挡下** |

---

## 四、环境事实：下载源本身不可靠，代码必须扛住

实测中观察到：**同一个 URL，前一分钟 18MB/s，下一分钟直接连不上**（HTTP 000 / 502）。

这不是特例，而是"从国内访问境外源"的常态。

### 修正：加**重试 + 断点续传**

```python
def _download(url, dest, progress, attempts=3):
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, attempts + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        # ... 续传；失败则退避 2s / 4s / 6s 后重试，已下载部分保留
```

**设计意图**：
- 让"**慢但不断**"的源（gyan.dev 20KB/s）最终能下完
- 让"**快但抖**"的源（GitHub）在重试后恢复
- 两者共用同一份 `.part` 文件，不浪费已下载的流量

---

## 五、最终结果

```
[0] 环境现状                    应用目录已有 bin/ 与 models/（空）
[1] 屏蔽系统 ffmpeg 后探测      ✅ 正确报告"未检测到 FFmpeg"
[2] 自动下载 FFmpeg             ✅ 245.8MB / 18 秒
[3] 下载后重新探测              ✅ 就绪，含 whisper 滤镜
    ffmpeg 可实际执行           ✅ v9.0.2-full_build-www.gyan.dev
    ffprobe 同目录存在          ✅
[4] whisper 模型下载            ✅ 74.1MB / 15 秒
    文件大小合理                ✅
    is_installed() 判定正确     ✅
[5] 用新装的 ffmpeg 读元信息    ✅ 12.0s / 1280×720
─────────────────────────────────────────────
通过 12 项 / 失败 0 项
```

---

## 六、这次测试值多少钱

| 若没做这个测试 | 后果 |
|---|---|
| 缺陷一（whisper 缺失） | 一直埋到**阶段 2 转录失败**才暴露。届时会误判为"代码问题"，排查成本是现在的几十倍 |
| 缺陷二（源太慢） | 用户首次启动卡在"下载中"两小时，**大概率直接卸载** |
| 环境事实（源不稳） | 偶发下载失败，用户重试即崩，无断点续传 |

**三条都不会在"33 项自测全过"里暴露** —— 因为那 33 项跑在一台**不需要下载**的机器上。

---

## 七、对后续阶段的启示

**"在开发机上通过" ≠ "在用户机上通过"。**

本次两条缺陷的共同特征：
- **都只在"缺少前置条件的环境"里出现**
- **都不会在开发机上复现**（开发机什么都有）
- **症状都延迟暴露**（一条延到阶段 2，一条延到用户首次启动）

⇒ **凡是"用户环境与我不同"的分支，必须显式模拟。**
具体做法：把这类测试写成脚本（本报告用的 `selftest_first_launch.py`），
**可以重复跑**，而不是靠人记得去试。

---

*报告生成：2026-09-25 | 阶段 1-B | 关联：`sliceq/ffmpeg_tools.py`、`sliceq/selftest_first_launch.py`*
