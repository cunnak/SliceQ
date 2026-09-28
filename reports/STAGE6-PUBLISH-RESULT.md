# 阶段 6 · 打包与开源发布 —— 完成报告

> 日期：2026-09-28｜仓库：**https://github.com/cunnak/SliceQ**（Public）
> Release：**https://github.com/cunnak/SliceQ/releases/tag/v0.1.0**
> 关联：`docs/TASKS.md` 阶段 6、`reports/STAGE6-CLEAN-VM-VERIFICATION.md`

---

## 1. 结论

**阶段 6 完成。SliceQ v0.1.0 已公开发布。**

| 验收项 | 结果 |
|---|---|
| 打包产物不含 ffmpeg（GPL 红线） | ✅ exe 的 **271 个 TOC 条目**逐个检查，`ffmpeg`/`ffprobe` **0 命中** |
| 体积在预算内（≤150MB） | ✅ **60.2 MB**（63,166,168 字节） |
| 冻结后可正常启动 | ✅ 隔离 `APPDATA` + offscreen：进程持续运行 + 建库 + 日志"数据库就绪" |
| 干净机器验证 | ✅ 全新 Win11 25H2（1024×768）实测通过（见另一份报告） |
| README 含全部必需声明 | ✅ 中英双语 + 独立 `DISCLAIMER.md` |
| 仓库公开、Release 附件可下载、版本号规范 | ✅ 见 §3 校验数据 |

---

## 2. 发布产物

| 项 | 值 |
|---|---|
| 文件 | `SliceQ.exe` |
| 大小 | **63,166,168 字节（60.2 MB）** |
| SHA256 | `cc99a6e40c3a02377639f4075733d73786eb993caecdf6ec521fcc94fc1c98cb` |
| 构建 | PyInstaller 6.22.3 onefile / `--windowed`，见 `SliceQ.spec`（入库，相对路径） |
| 附件状态 | `state: uploaded`；直链实测 **HTTP 206**（支持断点续传） |

---

## 3. 校验数据（全部实测）

```
仓库      cunnak/SliceQ   PUBLIC   default_branch=main
本地 HEAD  7a7fb0f014aaf1c1757ae4cf3ec525763471b604
远端 HEAD  7a7fb0f014aaf1c1757ae4cf3ec525763471b604   ← 一致
Release   tag=v0.1.0  draft=false  prerelease=false
Asset     SliceQ.exe  63166168 B  state=uploaded
直链      https://github.com/cunnak/SliceQ/releases/download/v0.1.0/SliceQ.exe → HTTP 206
```

入库内容：**156 个文件 / 约 2.4MB**（原始工作区 384MB，排除了 316MB 测试素材、
66MB 测试草稿、含个人数据的索引备份）。

---

## 4. 发布前处理（已完成）

- 排除测试素材（含第三方录播）与含个人数据的索引备份 ⇒ 384MB → **2.4MB**
- 批量脱敏本机用户名路径（19 文件 / 31 处 → `<user>`）
- 发布前机密扫描：真 key / JWT / Bearer / 学号邮箱 / 绝对路径 **全部 0 命中**
  （唯一命中是 `selftest_stage1.py` 里的测试占位符 `sk-test-1234…`）
- `LICENSE`(MIT) / `.gitignore` / `.gitattributes` / `DISCLAIMER.md`
- `requirements.txt` 改 `PySide6-Essentials`（避免拉进 1.2GB Addons）
- README 中英双语（`README.md` / `README.en.md`）

---

## 5. 本轮踩到的两个发布侧坑（已写进技能 `github-publish-win`）

### 5.1 `gh repo create --push`：仓库建好了，推送失败，**但退出码是 0**

```
https://github.com/cunnak/SliceQ        ← 仓库已建
fatal: unable to access '...': Empty reply from server
failed to run git: exit status 128
```

原因是 `gh` 与 `git` **各走各的代理配置**。给 git 单独配上再推即成功：

```bash
git config --local http.proxy  http://127.0.0.1:7897
git config --local https.proxy http://127.0.0.1:7897
git config --local http.postBuffer 524288000
```

### 5.2 ★ Release 附件（60MB）三次 EOF —— 元凶是 **HTTP/2**

```
Post ".../releases/<id>/assets?name=SliceQ.exe": EOF
```

`gh` 是 Go 写的、**默认 HTTP/2**，而代理对大流量 HTTP/2 上传支持不好 ⇒ 断流。

**解法一条命令：**

```bash
GODEBUG=http2client=0 gh release upload v0.1.0 dist/SliceQ.exe --clobber
```

**一次成功。**

> 连带教训：`gh release create <tag> <大文件>` 是**一步式**的 ——
> 附件传失败，**Release 也一起没建成**。所以大附件要**先建壳再传文件**。

---

## 6. 仍未完成 / 需要用户的事

| 项 | 说明 |
|---|---|
| **exe 图标（.ico）** | 现在是 PyInstaller 默认图标。需要一张图标，我才能加进构建 |
| **README 截图** | 需要在真实 GUI 里操作获取；目前 README **刻意不放占位图**（避免死链） |
| **仓库 topics** | 未设置（如 `video-editing` / `whisper` / `qwen` / `davinci-resolve` / `jianying`） |
| **完整业务链路在干净机器上跑通** | 未验证（需 GUI 交互 + 首次下载 1.8GB 模型） |
| **SmartScreen** | 首次运行会弹「Windows 已保护你的电脑」（无代码签名）；已写进 Release 说明 |

### 已知不足（如实记录，不阻塞发布）

- `work/` 目录两处缺陷（删任务不清 work —— 隐私；work 按 `video.stem` 命名会串缓存）
- `selftest_style_page.py` 退出阶段偶发段错误（结论正确打印，影响 CI 返回码）
- 粗筛缓存无内容指纹；CPU 路径转录并发未实测；多进程同写 SQLite 需单实例锁
- 剪映未验证版本只能提示"兼容性未验证"
- 字幕样式不跟随导出（刻意）

---

## 7. 一句话总结

从"阶段 0 只读调研"到今天公开 v0.1.0，这个项目最值钱的产出**不是代码**，
而是 `reports/` 里那一整条**实测证据链** —— 包括**我自己的六次误判**。
公开它是有代价的（用户能看到我们走过的弯路），但也是这个仓库可信度的来源。
